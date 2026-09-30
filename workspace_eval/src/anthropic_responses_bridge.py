from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from provider_auth import resolve_provider_api_key


Json = Any


def _read_json(path: Path) -> Json:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _write_json(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _text_from_anthropic_content(content: Json) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item_type == "tool_result":
            result_content = item.get("content")
            if isinstance(result_content, str):
                parts.append(result_content)
            elif isinstance(result_content, list):
                parts.extend(
                    block["text"]
                    for block in result_content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
    return "\n".join(part for part in parts if part)


VALID_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}


def _reasoning_effort(provider_config: Json) -> Optional[str]:
    """Read reasoningEffort from the provider config, if it names a real level."""

    cfg = provider_config if isinstance(provider_config, dict) else {}
    raw = cfg.get("reasoningEffort")
    if raw is None:
        raw = cfg.get("reasoning_effort")
    value = str(raw or "").strip().lower()
    if value in {"", "no_think"}:
        return None
    return value if value in VALID_REASONING_EFFORTS else None


def _anthropic_to_responses_input(payload: dict[str, Json]) -> list[dict[str, Json]]:
    out: list[dict[str, Json]] = []
    system = payload.get("system")
    system_text = _text_from_anthropic_content(system)
    if system_text:
        out.append({"role": "developer", "content": system_text})
    for message in payload.get("messages") if isinstance(payload.get("messages"), list) else []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        text_parts: list[str] = []
        tool_calls: list[dict[str, Json]] = []
        tool_results: list[dict[str, Json]] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block_type == "tool_use":
                    tool_calls.append(
                        {
                            "type": "function_call",
                            "call_id": str(block.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(
                                block.get("input")
                                if isinstance(block.get("input"), dict)
                                else {},
                                ensure_ascii=False,
                            ),
                        }
                    )
                elif block_type == "tool_result":
                    tool_results.append(
                        {
                            "type": "function_call_output",
                            "call_id": str(block.get("tool_use_id") or ""),
                            "output": _text_from_anthropic_content(
                                block.get("content")
                            ),
                        }
                    )
        if text_parts:
            out.append(
                {
                    "role": "assistant" if role == "assistant" else "user",
                    "content": "\n".join(text_parts),
                }
            )
        # Some gateways (notably litellm's Gemini adapter on /responses)
        # reject histories where a top-level function_call item is followed
        # by another tool round: they map tool outputs onto "the last
        # message with tool_calls" and fail when that message is not the
        # immediately preceding one.  Embedding the call in an assistant
        # message and the output in a user message keeps every round
        # self-contained and works across OpenAI-style backends.
        if tool_calls:
            out.append({"role": "assistant", "content": tool_calls})
        if tool_results:
            out.append({"role": "user", "content": tool_results})
    # Some Claude SDK compaction histories can contain an interrupted or
    # denied tool_use without a corresponding tool_result.  Responses-style
    # gateways (including DeepSeek's compatible endpoint) reject that entire
    # history.  Retain only complete function-call/output pairs so the bridge
    # remains a safe fallback when native Anthropic Messages is unavailable.
    # Calls/outputs may be top-level items or embedded in message content.
    def _collect_items(items: list[Json]) -> tuple[set[str], set[str]]:
        calls: set[str] = set()
        outputs: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                calls.add(str(item.get("call_id") or ""))
            elif item_type == "function_call_output":
                outputs.add(str(item.get("call_id") or ""))
            elif isinstance(item.get("content"), list):
                nested_calls, nested_outputs = _collect_items(
                    item["content"]
                )
                calls |= nested_calls
                outputs |= nested_outputs
        return calls, outputs

    call_ids, output_ids = _collect_items(out)
    complete_call_ids = {call_id for call_id in call_ids & output_ids if call_id}

    def _filter_nested(content: list[Json]) -> list[Json]:
        return [
            block
            for block in content
            if not isinstance(block, dict)
            or block.get("type")
            not in {"function_call", "function_call_output"}
            or str(block.get("call_id") or "") in complete_call_ids
        ]

    filtered: list[Json] = []
    for item in out:
        if not isinstance(item, dict):
            filtered.append(item)
            continue
        item_type = item.get("type")
        if item_type in {"function_call", "function_call_output"}:
            if str(item.get("call_id") or "") in complete_call_ids:
                filtered.append(item)
            continue
        if isinstance(item.get("content"), list):
            item = dict(item)
            item["content"] = _filter_nested(item["content"])
        filtered.append(item)
    return filtered


def _anthropic_tools_to_responses(tools: Json) -> list[dict[str, Json]]:
    out: list[dict[str, Json]] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        out.append(
            {
                "type": "function",
                "name": tool["name"],
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("input_schema")
                if isinstance(tool.get("input_schema"), dict)
                else {"type": "object", "properties": {}},
            }
        )
    return out


def _usage_to_anthropic(usage: Json) -> dict[str, Json]:
    source = usage if isinstance(usage, dict) else {}
    details = (
        source.get("input_tokens_details")
        if isinstance(source.get("input_tokens_details"), dict)
        else {}
    )
    return {
        "input_tokens": int(source.get("input_tokens") or 0),
        "output_tokens": int(source.get("output_tokens") or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": int(details.get("cached_tokens") or 0),
    }


def _responses_to_anthropic(value: dict[str, Json], *, model: str) -> dict[str, Json]:
    content: list[dict[str, Json]] = []
    stop_reason = "end_turn"
    for item in value.get("output") if isinstance(value.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for block in item.get("content") if isinstance(item.get("content"), list) else []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text:
                    content.append({"type": "text", "text": text})
        elif item_type == "function_call":
            arguments = item.get("arguments")
            try:
                parsed_arguments = (
                    json.loads(arguments)
                    if isinstance(arguments, str)
                    else arguments
                )
            except json.JSONDecodeError:
                parsed_arguments = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": str(item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"),
                    "name": str(item.get("name") or ""),
                    "input": parsed_arguments
                    if isinstance(parsed_arguments, dict)
                    else {},
                }
            )
            stop_reason = "tool_use"
    if not content and isinstance(value.get("output_text"), str):
        content.append({"type": "text", "text": value["output_text"]})
    return {
        "id": str(value.get("id") or f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": str(value.get("model") or model),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _usage_to_anthropic(value.get("usage")),
    }


def _sse(event: str, value: dict[str, Json]) -> bytes:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        target_base_url: str,
        provider_config: dict[str, Json],
        usage_log_path: Optional[Path],
    ) -> None:
        self.target_base_url = target_base_url.rstrip("/")
        self.provider_config = provider_config
        self.usage_log_path = usage_log_path
        self.usage_lock = threading.Lock()
        super().__init__(address, BridgeHandler)

    def record_usage(self, value: dict[str, Json]) -> None:
        if self.usage_log_path is None:
            return
        with self.usage_lock:
            with self.usage_log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    # The Claude SDK can emit very large, UTF-8 tool transcripts.  Close each
    # local bridge connection after responding so a malformed/reused keep-alive
    # stream cannot make residual request bytes look like a second HTTP request
    # to BaseHTTPRequestHandler.  This affects only the loopback compatibility
    # bridge, not the judge-visible files or Claude Code's context management.
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, value: dict[str, Json]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_chat_completions(self, payload: dict[str, Json], model: str) -> None:
        """Serve an Anthropic Messages request via the chat-completions API.

        Some gateway/model combinations (notably litellm's Gemini adapter on
        the /responses endpoint) drop function_call_output items, so the model
        never sees tool results and re-issues the same call forever.  The
        chat-completions endpoint handles tool round-trips correctly for
        those models, so wireApi: chat_completions routes through here.
        """
        messages: list[dict[str, Json]] = []
        system_text = _text_from_anthropic_content(payload.get("system"))
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for message in (
            payload.get("messages")
            if isinstance(payload.get("messages"), list)
            else []
        ):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content")
            text_parts: list[str] = []
            tool_calls: list[dict[str, Json]] = []
            tool_results: list[dict[str, Json]] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text" and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
                    elif block_type == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name") or ""),
                                    "arguments": json.dumps(
                                        block.get("input")
                                        if isinstance(block.get("input"), dict)
                                        else {},
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        )
                    elif block_type == "tool_result":
                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(block.get("tool_use_id") or ""),
                                "content": _text_from_anthropic_content(
                                    block.get("content")
                                ),
                            }
                        )
            if role == "assistant":
                entry: dict[str, Json] = {"role": "assistant"}
                if text_parts:
                    entry["content"] = "\n".join(text_parts)
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                    entry.setdefault("content", None)
                messages.append(entry)
            else:
                if text_parts:
                    messages.append({"role": "user", "content": "\n".join(text_parts)})
                messages.extend(tool_results)
        # 与 responses 路径同样的“只保留完整 call/output 对”保护：Anthropic
        # 历史里可能有被拒绝/中断的 tool_use 没有对应 tool_result（例如 MCP 工具
        # 回合被截断、图片读取被 deny），DeepSeek 的 chat-completions 端点会以
        # 400（"assistant message with 'tool_calls' must be followed by tool
        # messages"）整段拒绝 —— 实测 after 两臂因 MCP 调用多而集中中招。
        call_ids: set[str] = set()
        result_ids: set[str] = set()
        for entry in messages:
            if entry.get("role") == "assistant" and isinstance(entry.get("tool_calls"), list):
                for call in entry["tool_calls"]:
                    if isinstance(call, dict):
                        call_ids.add(str(call.get("id") or ""))
            elif entry.get("role") == "tool":
                result_ids.add(str(entry.get("tool_call_id") or ""))
        unpaired = call_ids - result_ids
        if unpaired:
            keep = call_ids & result_ids
            filtered: list[dict[str, Json]] = []
            for entry in messages:
                role = entry.get("role")
                if role == "assistant" and isinstance(entry.get("tool_calls"), list):
                    calls = [
                        call
                        for call in entry["tool_calls"]
                        if isinstance(call, dict) and str(call.get("id") or "") in keep
                    ]
                    if not calls:
                        if entry.get("content"):
                            filtered.append({"role": "assistant", "content": entry["content"]})
                        continue
                    entry = {**entry, "tool_calls": calls}
                elif role == "tool" and str(entry.get("tool_call_id") or "") not in keep:
                    continue
                filtered.append(entry)
            messages = filtered
        upstream_payload: dict[str, Json] = {
            "model": model,
            "messages": messages,
            "max_tokens": int(payload.get("max_tokens") or 16384),
        }
        effort = _reasoning_effort(self.server.provider_config)
        if effort:
            upstream_payload["reasoning_effort"] = effort
        tools = _anthropic_tools_to_responses(payload.get("tools"))
        if tools:
            upstream_payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description") or "",
                        "parameters": tool.get("parameters") or {"type": "object"},
                    },
                }
                for tool in tools
            ]
        credential = resolve_provider_api_key(
            self.server.provider_config,
            model=model,
        )
        if not credential:
            raise ValueError("provider credentials are unavailable")
        request = urllib.request.Request(
            self.server.target_base_url + "/chat/completions",
            data=json.dumps(upstream_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + credential,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            upstream = json.loads(
                response.read().decode("utf-8", errors="ignore") or "{}"
            )
        if not isinstance(upstream, dict):
            raise ValueError("upstream returned a non-object response")
        choice = next(
            (
                c
                for c in upstream.get("choices") or []
                if isinstance(c, dict)
            ),
            {},
        )
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content: list[dict[str, Json]] = []
        stop_reason = "end_turn"
        if isinstance(message.get("content"), str) and message["content"]:
            content.append({"type": "text", "text": message["content"]})
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"),
                    "name": str(function.get("name") or ""),
                    "input": arguments if isinstance(arguments, dict) else {},
                }
            )
            stop_reason = "tool_use"
        usage_raw = upstream.get("usage") if isinstance(upstream.get("usage"), dict) else {}
        result = {
            "id": str(upstream.get("id") or f"msg_{uuid.uuid4().hex}"),
            "type": "message",
            "role": "assistant",
            "model": str(upstream.get("model") or model),
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage_raw.get("prompt_tokens") or 0,
                "output_tokens": usage_raw.get("completion_tokens") or 0,
                "total_tokens": usage_raw.get("total_tokens") or 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": (
                    (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens")
                    if isinstance(usage_raw.get("prompt_tokens_details"), dict)
                    else 0
                )
                or 0,
            },
        }
        self.server.record_usage(
            {
                "timestamp": time.time(),
                "model": upstream.get("model") or model,
                "responseId": upstream.get("id"),
                "status": "completed",
                "usage": {
                    "input_tokens": usage_raw.get("prompt_tokens") or 0,
                    "output_tokens": usage_raw.get("completion_tokens") or 0,
                    "total_tokens": usage_raw.get("total_tokens") or 0,
                },
            }
        )
        if payload.get("stream") is True:
            events: list[bytes] = [
                _sse(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {**result, "content": [], "stop_reason": None},
                    },
                )
            ]
            for index, block in enumerate(result["content"]):
                events.append(
                    _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": (
                                {"type": "text", "text": ""}
                                if block["type"] == "text"
                                else {
                                    "type": "tool_use",
                                    "id": block["id"],
                                    "name": block["name"],
                                    "input": {},
                                }
                            ),
                        },
                    )
                )
                if block["type"] == "text":
                    events.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "text_delta", "text": block["text"]},
                            },
                        )
                    )
                else:
                    events.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": json.dumps(
                                        block["input"], ensure_ascii=False
                                    ),
                                },
                            },
                        )
                    )
                events.append(
                    _sse(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": index},
                    )
                )
            events.extend(
                [
                    _sse(
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": result["stop_reason"],
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": result["usage"]["output_tokens"]},
                        },
                    ),
                    _sse("message_stop", {"type": "message_stop"}),
                ]
            )
            body = b"".join(events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json(200, result)

    def do_POST(self) -> None:
        normalized_path = self.path.split("?", 1)[0].rstrip("/")
        if normalized_path.endswith("/messages/count_tokens"):
            self._send_json(200, {"input_tokens": 0})
            return
        if not normalized_path.endswith("/messages"):
            self._send_json(
                404,
                {"type": "error", "error": {"type": "not_found_error", "message": "not found"}},
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("request payload must be an object")
            model = str(
                self.server.provider_config.get("authModel")
                or self.server.provider_config.get("model")
                or payload.get("model")
                or ""
            )
            if str(
                self.server.provider_config.get("wireApi") or ""
            ).strip().lower() in {"chat_completions", "chat-completions"}:
                self._handle_chat_completions(payload, model)
                return
            upstream_payload: dict[str, Json] = {
                "model": model,
                "input": _anthropic_to_responses_input(payload),
                "max_output_tokens": int(payload.get("max_tokens") or 16384),
            }
            # The Anthropic Messages protocol has no reasoning-effort field, so
            # the caller's configured level would be lost here. Forward it or a
            # judge configured for high/max effort silently runs at the provider
            # default.
            effort = _reasoning_effort(self.server.provider_config)
            if effort:
                upstream_payload["reasoning"] = {"effort": effort}
            tools = _anthropic_tools_to_responses(payload.get("tools"))
            if tools:
                upstream_payload["tools"] = tools
                upstream_payload["tool_choice"] = "auto"
            credential = resolve_provider_api_key(
                self.server.provider_config,
                model=model,
            )
            if not credential:
                raise ValueError("provider credentials are unavailable")
            request = urllib.request.Request(
                self.server.target_base_url + "/responses",
                data=json.dumps(upstream_payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + credential,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                upstream = json.loads(
                    response.read().decode("utf-8", errors="ignore") or "{}"
                )
            if not isinstance(upstream, dict):
                raise ValueError("upstream returned a non-object response")
            result = _responses_to_anthropic(upstream, model=model)
            self.server.record_usage(
                {
                    "timestamp": time.time(),
                    "model": upstream.get("model") or model,
                    "responseId": upstream.get("id"),
                    "status": upstream.get("status"),
                    "usage": upstream.get("usage")
                    if isinstance(upstream.get("usage"), dict)
                    else {},
                }
            )
            if payload.get("stream") is True:
                events: list[bytes] = [
                    _sse(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                **result,
                                "content": [],
                                "stop_reason": None,
                                "usage": {
                                    **result["usage"],
                                    "output_tokens": 0,
                                },
                            },
                        },
                    ),
                ]
                for index, block in enumerate(result["content"]):
                    events.append(
                        _sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": (
                                    {"type": "text", "text": ""}
                                    if block["type"] == "text"
                                    else {
                                        "type": "tool_use",
                                        "id": block["id"],
                                        "name": block["name"],
                                        "input": {},
                                    }
                                ),
                            },
                        )
                    )
                    if block["type"] == "text":
                        events.append(
                            _sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": index,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": block["text"],
                                    },
                                },
                            )
                        )
                    else:
                        events.append(
                            _sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": index,
                                    "delta": {
                                        "type": "input_json_delta",
                                        "partial_json": json.dumps(
                                            block["input"], ensure_ascii=False
                                        ),
                                    },
                                },
                            )
                        )
                    events.append(
                        _sse(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": index},
                        )
                    )
                events.extend(
                    [
                        _sse(
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {
                                    "stop_reason": result["stop_reason"],
                                    "stop_sequence": None,
                                },
                                "usage": {
                                    "output_tokens": result["usage"][
                                        "output_tokens"
                                    ]
                                },
                            },
                        ),
                        _sse("message_stop", {"type": "message_stop"}),
                    ]
                )
                body = b"".join(events)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(200, result)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = str(exc)
            self._send_json(
                int(exc.code),
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": body[:4000]},
                },
            )
        except Exception as exc:
            self._send_json(
                500,
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                },
            )


def create_server(
    *,
    target_base_url: str,
    provider_config: dict[str, Json],
    usage_log_path: Optional[Path] = None,
    port: int = 0,
) -> BridgeServer:
    return BridgeServer(
        ("127.0.0.1", max(0, int(port))),
        target_base_url=target_base_url,
        provider_config=provider_config,
        usage_log_path=usage_log_path,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bridge Anthropic Messages API requests to an OpenAI Responses endpoint."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--usage-log")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = _read_json(config_path)
    if not isinstance(config, dict):
        raise SystemExit("bridge config must be an object")
    target_base_url = str(config.get("baseUrl") or "").strip()
    if not target_base_url:
        raise SystemExit("bridge config is missing baseUrl")
    usage_log_path = Path(args.usage_log).resolve() if args.usage_log else None
    if usage_log_path is not None:
        usage_log_path.parent.mkdir(parents=True, exist_ok=True)
    server = create_server(
        target_base_url=target_base_url,
        provider_config=config,
        usage_log_path=usage_log_path,
        port=args.port,
    )
    ready_path = Path(args.ready_file).resolve()
    _write_json(
        ready_path,
        {
            "schemaVersion": 1,
            "baseUrl": f"http://127.0.0.1:{server.server_address[1]}",
            "pid": os.getpid(),
        },
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
