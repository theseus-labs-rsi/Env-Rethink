import json
import base64
import http.server
import io
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from api_retry import (
    RetryRequestError,
    request_with_backoff,
    retry_policy_from_config,
)
from provider_auth import (
    first_provider_value,
    load_dotenv,
    provider_auth_type,
    resolve_provider_api_key,
)

Json = Any


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jsonl_events(text: str) -> List[Dict[str, Json]]:
    out: List[Dict[str, Json]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _expand_provider_value(value: Json) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    fallback_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:-]-(\$\{[A-Za-z_][A-Za-z0-9_]*\}|[^}]*)\}")
    while True:
        m = fallback_re.search(s)
        if not m:
            break
        primary = os.environ.get(m.group(1), "")
        fallback = m.group(2)
        repl = primary if primary else os.path.expandvars(fallback)
        s = s[: m.start()] + repl + s[m.end() :]
    s = os.path.expandvars(s).strip()
    # Treat unresolved placeholders from YAML templates as missing.
    if not s or re.search(r"\$\{[^}]+\}", s):
        return None
    return s


def _first_config_value(*values: Json) -> Optional[str]:
    return first_provider_value(*values)


def _load_dotenv() -> None:
    eval_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    load_dotenv(os.path.join(eval_root, ".env"), os.path.join(os.getcwd(), ".env"))


def _codex_sandbox_mode(api_provider: Optional[Dict[str, Json]] = None) -> str:
    provider = api_provider if isinstance(api_provider, dict) else {}
    configured = _first_config_value(
        provider.get("codexSandboxMode"),
        provider.get("codex_sandbox_mode"),
    )
    if configured in {"read-only", "workspace-write", "danger-full-access"}:
        return configured
    mode = str(os.environ.get("CODEX_SANDBOX_MODE") or "workspace-write").strip()
    if mode in {"read-only", "workspace-write", "danger-full-access"}:
        return mode
    return "workspace-write"


def _materialize_codex_home(sandbox_dir: str) -> Optional[str]:
    """Copy image-installed Codex skills into a writable per-run home."""
    template = str(
        os.environ.get("WORKSPACE_BENCH_CODEX_HOME_TEMPLATE")
        or os.environ.get("CODEX_HOME")
        or ""
    ).strip()
    if not template or not os.path.isdir(template):
        return None
    destination = os.path.join(
        os.path.abspath(sandbox_dir),
        "codex_home",
        ".codex",
    )
    try:
        shutil.copytree(template, destination, dirs_exist_ok=True)
    except OSError:
        return None
    return destination


def _normalize_base_url(base_url: Optional[str]) -> str:
    if isinstance(base_url, str) and base_url.strip():
        return base_url.strip().rstrip("/")
    return "https://api.openai.com/v1"


def _normalize_usage(obj: Json) -> Dict[str, Json]:
    if not isinstance(obj, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}

    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
    prompt_tokens = usage.get("input_tokens")
    if not isinstance(prompt_tokens, int):
        prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("output_tokens")
    if not isinstance(completion_tokens, int):
        completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")

    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    total_tokens = int(total_tokens or (prompt_tokens + completion_tokens))

    input_details = (
        usage.get("input_tokens_details")
        if isinstance(usage.get("input_tokens_details"), dict)
        else (
            usage.get("input_token_details")
            if isinstance(usage.get("input_token_details"), dict)
            else {}
        )
    )
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = (
        usage.get("output_tokens_details")
        if isinstance(usage.get("output_tokens_details"), dict)
        else (
            usage.get("completion_tokens_details")
            if isinstance(usage.get("completion_tokens_details"), dict)
            else {}
        )
    )
    normalized = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_read": int(
            usage.get("cached_input_tokens")
            or input_details.get("cached_tokens")
            or input_details.get("cache_read")
            or prompt_details.get("cached_tokens")
            or 0
        ),
        "cache_write": int(usage.get("reasoning_output_tokens") or completion_details.get("reasoning_tokens") or 0),
    }
    if isinstance(usage, dict):
        normalized["raw"] = json.loads(json.dumps(usage, ensure_ascii=False))
    return normalized


def _add_usage(dst: Dict[str, Json], usage: Dict[str, Json]) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read", "cache_write"):
        dst[k] = int(dst.get(k) or 0) + int(usage.get(k) or 0)


def _text_from_content(content: Json) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("text") or item.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join([x for x in parts if x])
    if isinstance(content, dict):
        txt = content.get("text") or content.get("content")
        if isinstance(txt, str):
            return txt
    return ""


def _extract_tool_payload(evt: Dict[str, Json]) -> Optional[Dict[str, Json]]:
    for key in ("tool_call", "toolCall", "call", "item", "data", "payload"):
        val = evt.get(key)
        if isinstance(val, dict):
            name = val.get("tool") or val.get("tool_name") or val.get("name")
            args = val.get("arguments") or val.get("args") or val.get("input")
            if name or args:
                return {
                    "tool": name if isinstance(name, str) else None,
                    "callID": val.get("call_id") or val.get("callId") or val.get("id"),
                    "input": args if isinstance(args, dict) else {},
                    "output": val.get("output") if "output" in val else val.get("result"),
                    "exitCode": val.get("exit_code") or val.get("exitCode"),
                }
    return None


def parse_codex_jsonl(stdout_text: str, *, prompt: str, started_at: float, base_url: Optional[str], model: Optional[str]) -> Dict[str, Json]:
    events = _jsonl_events(stdout_text)
    thread_id = next(
        (
            str(evt.get("thread_id"))
            for evt in events
            if str(evt.get("type") or "") == "thread.started"
            and evt.get("thread_id")
        ),
        None,
    )
    execution_trace: List[Dict[str, Json]] = [
        {"type": "text", "role": "user", "content": str(prompt or ""), "timestamp": _iso_from_ts(started_at)}
    ]
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}
    raw_usage_events: List[Dict[str, Json]] = []
    last_text = ""
    turns = 0

    for idx, evt in enumerate(events):
        typ = str(evt.get("type") or evt.get("event") or "")
        item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        ts = _iso_from_ts(started_at + ((idx + 1) / 1000.0))

        usage = None
        if isinstance(evt.get("usage"), dict):
            usage = _normalize_usage(evt.get("usage"))
        elif isinstance(evt.get("usageTotal"), dict):
            usage = _normalize_usage(evt.get("usageTotal"))
        elif isinstance(evt.get("usage_total"), dict):
            usage = _normalize_usage(evt.get("usage_total"))
        elif isinstance(item.get("usage"), dict):
            usage = _normalize_usage(item.get("usage"))
        if usage:
            _add_usage(usage_total, usage)
            if isinstance(usage.get("raw"), dict):
                raw_usage_events.append(usage["raw"])

        text = ""
        if typ in {"item.completed", "item.started"} and item_type == "agent_message":
            text = _text_from_content(item.get("text") or item.get("message") or item.get("content"))
        elif typ in {"agent_message", "agent_reasoning", "message", "turn_complete", "task_complete"}:
            text = _text_from_content(evt.get("message") or evt.get("content") or evt.get("text") or evt.get("last_agent_message"))
        elif typ in {"response.output_text.delta", "output_text.delta", "text"}:
            text = _text_from_content(evt.get("delta") or evt.get("text") or evt.get("content"))
        elif typ in {"result", "response.completed"}:
            text = _text_from_content(evt.get("result") or evt.get("output") or evt.get("message"))

        if text.strip():
            turns += 1
            last_text = text.strip()
            execution_trace.append(
                {
                    "type": "text",
                    "role": "assistant",
                    "content": text,
                    "timestamp": ts,
                    "turn": turns,
                    "llm": {
                        "provider": "codex",
                        "baseUrl": base_url,
                        "model": model,
                        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0},
                        "stopReason": evt.get("stop_reason") or evt.get("finish_reason"),
                        "errorMessage": evt.get("error") if isinstance(evt.get("error"), str) else None,
                    },
                }
            )

        tool_payload = _extract_tool_payload(evt)
        if typ in {"item.completed", "item.started"} and item_type in {"command_execution", "mcp_tool_call"}:
            tool_payload = {
                "tool": item_type,
                "callID": item.get("id"),
                "input": {"command": item.get("command")} if isinstance(item.get("command"), str) else {},
                "output": item.get("aggregated_output") if "aggregated_output" in item else item.get("output"),
                "exitCode": item.get("exit_code"),
            }
        if tool_payload and (
            "tool" in typ
            or typ in {"item.completed", "item.started", "exec_command_begin", "exec_command_end", "mcp_tool_call_begin", "mcp_tool_call_end"}
        ):
            is_done = typ in {"item.completed", "exec_command_end", "mcp_tool_call_end"} or typ.endswith("_end") or typ.endswith(".end")
            execution_trace.append(
                {
                    "type": "tool",
                    "role": "tool",
                    "tool": tool_payload.get("tool"),
                    "callID": tool_payload.get("callID"),
                    "timestamp": ts,
                    "startedAt": ts,
                    "finishedAt": ts if is_done else None,
                    "durationMs": None,
                    "status": "completed" if is_done else "in_progress",
                    "exitCode": tool_payload.get("exitCode"),
                    "input": tool_payload.get("input") if isinstance(tool_payload.get("input"), dict) else {},
                    "output": tool_payload.get("output"),
                    "turn": turns or None,
                }
            )

    return {
        "threadId": thread_id,
        "executionTrace": execution_trace,
        "lastText": last_text,
        "usageTotal": usage_total,
        "usageRaw": raw_usage_events,
        "turns": turns,
        "events": len(events),
    }


def _toml_str(value: str) -> str:
    return json.dumps(str(value))


def _provider_config_arg(*, base_url: str) -> str:
    return "{name=\"ripbench\", base_url=" + _toml_str(base_url) + ", env_key=\"CODEX_API_KEY\", wire_api=\"responses\"}"


def _should_use_chat_adapter(model: str, api_provider: Optional[Dict[str, Json]] = None) -> bool:
    provider = api_provider if isinstance(api_provider, dict) else {}
    upstream_wire_api = str(
        _first_config_value(
            provider.get("upstreamWireApi"),
            provider.get("upstream_wire_api"),
        )
        or ""
    ).strip().lower()
    if upstream_wire_api in {
        "chat",
        "chat_completions",
        "chat-completions",
    }:
        return True
    wire_api = str(
        _first_config_value(provider.get("wireApi"), provider.get("wire_api")) or ""
    ).strip().lower()
    if wire_api in {"responses", "response"}:
        return False
    if wire_api in {"chat", "chat_completions", "chat-completions"}:
        return True
    mode = (os.environ.get("CODEX_CHAT_ADAPTER") or "auto").strip().lower()
    if mode in {"0", "false", "no", "never", "off"}:
        return False
    if mode in {"1", "true", "yes", "always", "on"}:
        return True
    return not str(model or "").lower().startswith("gpt-")


def _responses_text(content: Json) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join([x for x in parts if x])
    return ""


def _compact_data_image(
    url: str,
    *,
    max_encoded_chars: int,
) -> Optional[str]:
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return url if isinstance(url, str) else None
    try:
        header, encoded = url.split(",", 1)
        raw = base64.b64decode(encoded)
        if len(url) <= max_encoded_chars:
            return url
        from PIL import Image

        image = Image.open(io.BytesIO(raw))
        if image.mode not in {"RGB", "L"}:
            if "A" in image.mode:
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")
        max_edge = 1280
        if max(image.size) > max_edge:
            image.thumbnail((max_edge, max_edge))
        for quality in (70, 60, 50, 40, 30):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            compact = (
                "data:image/jpeg;base64,"
                + base64.b64encode(buffer.getvalue()).decode("ascii")
            )
            if len(compact) <= max_encoded_chars:
                return compact
            image.thumbnail(
                (
                    max(320, int(image.width * 0.8)),
                    max(320, int(image.height * 0.8)),
                )
            )
        return None
    except Exception:
        return None


def _chat_image_url(block: Dict[str, Json]) -> Optional[str]:
    image_url = block.get("image_url")
    if isinstance(image_url, str):
        return image_url
    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
        return str(image_url["url"])
    if isinstance(block.get("url"), str):
        return str(block["url"])
    source = block.get("source")
    if isinstance(source, dict):
        if isinstance(source.get("url"), str):
            return str(source["url"])
        data = source.get("data")
        media_type = source.get("media_type") or source.get("mediaType")
        if isinstance(data, str) and isinstance(media_type, str):
            return f"data:{media_type};base64,{data}"
    return None


def _responses_content_to_chat(
    content: Json,
    *,
    image_refs: List[Dict[str, Json]],
    max_image_chars: int,
) -> Json:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _responses_text(content)
    blocks: List[Dict[str, Json]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        url = _chat_image_url(item)
        if url:
            compact = _compact_data_image(
                url,
                max_encoded_chars=max_image_chars,
            )
            if compact:
                image_block = {
                    "type": "image_url",
                    "image_url": {"url": compact, "detail": "low"},
                }
                blocks.append(image_block)
                image_refs.append(image_block)
            else:
                blocks.append(
                    {
                        "type": "text",
                        "text": "[image output unavailable]",
                    }
                )
    if not blocks:
        return ""
    return blocks


def _enforce_chat_image_budget(
    _messages: List[Dict[str, Json]],
    image_refs: List[Dict[str, Json]],
    *,
    max_total_chars: int,
) -> None:
    total = sum(
        len(str(block.get("image_url", {}).get("url") or ""))
        for block in image_refs
    )
    if total <= max_total_chars:
        return
    for block in image_refs:
        if total <= max_total_chars:
            break
        url = str(block.get("image_url", {}).get("url") or "")
        block.clear()
        block.update(
            {
                "type": "text",
                "text": "[older image omitted to fit request size]",
            }
        )
        total -= len(url)


def _responses_input_to_chat(
    input_items: Json,
    *,
    max_image_chars: int = 100_000,
    max_total_image_chars: int = 500_000,
) -> List[Dict[str, Json]]:
    messages: List[Dict[str, Json]] = []
    pending_tool_calls: List[Dict[str, Json]] = []
    image_refs: List[Dict[str, Json]] = []
    deferred_images: List[Dict[str, Json]] = []
    if not isinstance(input_items, list):
        return messages
    for item in input_items:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "message":
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            messages.append(
                {
                    "role": role,
                    "content": _responses_content_to_chat(
                        item.get("content"),
                        image_refs=image_refs,
                        max_image_chars=max_image_chars,
                    ),
                }
            )
        elif typ == "function_call":
            args = item.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {}, ensure_ascii=False)
            pending_tool_calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or f"call_{len(pending_tool_calls)}"),
                    "type": "function",
                    "function": {"name": str(item.get("name") or "unknown"), "arguments": args},
                }
            )
        elif typ == "function_call_output":
            if pending_tool_calls:
                messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
                pending_tool_calls = []
            output_content = _responses_content_to_chat(
                item.get("output"),
                image_refs=deferred_images,
                max_image_chars=max_image_chars,
            )
            output_text = _responses_text(item.get("output"))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": output_text or (
                        "[tool returned image output]"
                        if deferred_images
                        else str(item.get("output") or "")
                    ),
                }
            )
    if pending_tool_calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
    if deferred_images:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Images returned by the preceding tools:"},
                    *deferred_images,
                ],
            }
        )
        image_refs.extend(deferred_images)
    _enforce_chat_image_budget(
        messages,
        image_refs,
        max_total_chars=max_total_image_chars,
    )
    return messages


def _responses_tools_to_chat(tools: Json) -> List[Dict[str, Json]]:
    out: List[Dict[str, Json]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {"type": "object", "properties": {}},
                },
            }
        )
    return [x for x in out if x["function"]["name"]]


def _chat_usage_to_responses(usage: Json) -> Dict[str, Json]:
    if not isinstance(usage, dict):
        usage = {}
    prompt_details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else {}
    )
    completion_details = (
        usage.get("completion_tokens_details")
        if isinstance(usage.get("completion_tokens_details"), dict)
        else {}
    )
    result: Dict[str, Json] = {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    if prompt_details:
        result["input_tokens_details"] = {
            "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
            **(
                {"cache_write_tokens": int(prompt_details["cache_write_tokens"])}
                if isinstance(prompt_details.get("cache_write_tokens"), int)
                else {}
            ),
        }
    if completion_details:
        result["output_tokens_details"] = {
            "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0)
        }
    return result


def _adapter_max_tokens(
    req_obj: Dict[str, Json],
    provider_config: Optional[Dict[str, Json]] = None,
) -> int:
    provider = provider_config if isinstance(provider_config, dict) else {}
    requested = req_obj.get("max_output_tokens")
    configured = (
        provider.get("maxCompletionTokens")
        or provider.get("max_completion_tokens")
        or os.environ.get("CODEX_CHAT_ADAPTER_MAX_TOKENS")
        or 16384
    )
    try:
        configured_value = int(configured)
    except Exception:
        configured_value = 16384
    try:
        requested_value = int(requested) if requested is not None else configured_value
    except Exception:
        requested_value = configured_value
    value = min(requested_value, configured_value)
    return max(1024, min(value, 128_000))


def _extract_text_tool_calls(text: str, valid_tools: List[str]) -> List[Dict[str, Json]]:
    if not isinstance(text, str) or "<invoke" not in text:
        return []
    valid = {name for name in valid_tools if name}
    calls: List[Dict[str, Json]] = []
    for idx, match in enumerate(re.finditer(r"<invoke\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</invoke>", text, flags=re.DOTALL)):
        name = match.group(1).strip()
        if valid and name not in valid:
            continue
        body = match.group(2)
        params: Dict[str, str] = {}
        for pm in re.finditer(r"<parameter\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</parameter>", body, flags=re.DOTALL):
            params[pm.group(1).strip()] = pm.group(2)
        calls.append(
            {
                "id": f"text_call_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(params, ensure_ascii=False)},
            }
        )
    return calls


def _sse_event(event: Dict[str, Json]) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


def _responses_json_to_sse(response: Dict[str, Json]) -> bytes:
    """Convert a complete Responses object into the SSE sequence Codex expects."""
    response_id = str(response.get("id") or f"resp_{int(time.time() * 1000)}")
    events: List[Dict[str, Json]] = [
        {
            "type": "response.created",
            "response": {
                **response,
                "id": response_id,
                "status": "in_progress",
                "output": [],
            },
        }
    ]
    output = response.get("output")
    for output_index, source_item in enumerate(
        output if isinstance(output, list) else []
    ):
        if not isinstance(source_item, dict):
            continue
        item = dict(source_item)
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or f"item_{output_index}")
        item["id"] = item_id
        if item_type == "message":
            content = item.get("content")
            blocks = content if isinstance(content, list) else []
            added_item = {**item, "content": []}
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": added_item,
                }
            )
            for content_index, source_block in enumerate(blocks):
                if not isinstance(source_block, dict):
                    continue
                block = dict(source_block)
                block_type = str(block.get("type") or "")
                if block_type == "output_text":
                    text = str(block.get("text") or "")
                    empty_block = {**block, "text": ""}
                    events.extend(
                        [
                            {
                                "type": "response.content_part.added",
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": content_index,
                                "part": empty_block,
                            },
                            {
                                "type": "response.output_text.delta",
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": content_index,
                                "delta": text,
                            },
                            {
                                "type": "response.output_text.done",
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": content_index,
                                "text": text,
                            },
                            {
                                "type": "response.content_part.done",
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": content_index,
                                "part": block,
                            },
                        ]
                    )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                }
            )
        elif item_type == "function_call":
            arguments = str(item.get("arguments") or "")
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": {**item, "arguments": ""},
                }
            )
            if arguments:
                events.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item_id,
                        "output_index": output_index,
                        "delta": arguments,
                    }
                )
            events.extend(
                [
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "arguments": arguments,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    },
                ]
            )
        else:
            events.extend(
                [
                    {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": item,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    },
                ]
            )
    completed = {**response, "id": response_id}
    events.append({"type": "response.completed", "response": completed})
    return b"".join(_sse_event(event) for event in events) + b"data: [DONE]\n\n"


def _write_jsonl(path: str, value: Dict[str, Json]) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def _responses_stream_validation_error(
    request_body: bytes,
    response_headers: Dict[str, str],
    response_body: bytes,
) -> Optional[str]:
    request_streaming = False
    try:
        request_obj = json.loads(request_body.decode("utf-8", errors="ignore") or "{}")
        request_streaming = request_obj.get("stream") is True
    except Exception:
        pass
    content_type = next(
        (
            value
            for name, value in response_headers.items()
            if name.lower() == "content-type"
        ),
        "",
    ).lower()
    is_stream = request_streaming or "text/event-stream" in content_type
    if not is_stream:
        return None
    if re.search(
        rb'"type"\s*:\s*"response\.(completed|failed|incomplete)"',
        response_body,
    ):
        return None
    return "stream closed before a terminal Responses event"


def _normalize_responses_tool_outputs(request_body: bytes) -> bytes:
    """Add text fallbacks required by stricter Responses-compatible gateways."""
    try:
        request_obj = json.loads(
            request_body.decode("utf-8", errors="ignore") or "{}"
        )
    except Exception:
        return request_body
    input_items = request_obj.get("input")
    if not isinstance(input_items, list):
        return request_body

    changed = False
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        output = item.get("output")
        if not isinstance(output, list):
            continue
        normalized_output: List[Json] = []
        for block in output:
            if isinstance(block, dict):
                text = block.get("text")
                if not isinstance(text, str) or not text:
                    fallback = (
                        block.get("content")
                        if isinstance(block.get("content"), str)
                        else (
                            block.get("output")
                            if isinstance(block.get("output"), str)
                            else json.dumps(block, ensure_ascii=False)
                        )
                    )
                    block = {**block, "text": fallback or "[tool output]"}
                    changed = True
                normalized_output.append(block)
            else:
                normalized_output.append(
                    {
                        "type": "input_text",
                        "text": str(block) or "[tool output]",
                    }
                )
                changed = True
        item["output"] = normalized_output
    if not changed:
        return request_body
    return json.dumps(request_obj, ensure_ascii=False).encode("utf-8")


def _apply_responses_request_limits(
    request_body: bytes,
    provider_config: Optional[Dict[str, Json]] = None,
) -> bytes:
    """Apply configured output and platform-timeout limits to Responses requests."""
    provider = provider_config if isinstance(provider_config, dict) else {}
    raw_timeout = provider.get("upstreamTimeoutSec")
    if raw_timeout is None:
        raw_timeout = provider.get("upstream_timeout_sec")
    try:
        request_obj = json.loads(
            request_body.decode("utf-8", errors="ignore") or "{}"
        )
    except json.JSONDecodeError:
        return request_body
    if not isinstance(request_obj, dict):
        return request_body

    changed = False
    if raw_timeout is not None:
        try:
            request_obj["timeout"] = max(1, int(raw_timeout))
            changed = True
        except (TypeError, ValueError):
            pass

    raw_max_tokens = provider.get("maxOutputTokens")
    if raw_max_tokens is None:
        raw_max_tokens = provider.get("max_output_tokens")
    if raw_max_tokens is not None:
        try:
            configured_max = max(16, int(raw_max_tokens))
            requested = request_obj.get("max_output_tokens")
            requested_max = (
                int(requested) if requested is not None else configured_max
            )
            request_obj["max_output_tokens"] = min(
                requested_max,
                configured_max,
            )
            changed = True
        except (TypeError, ValueError):
            pass

    upstream_stream = provider.get("upstreamStream")
    if upstream_stream is None:
        upstream_stream = provider.get("upstream_stream")
    request_obj["stream"] = bool(upstream_stream)
    changed = True

    if not changed:
        return request_body
    return json.dumps(request_obj, ensure_ascii=False).encode("utf-8")


def _start_responses_retry_proxy(
    *,
    target_base_url: str,
    api_key: str,
    raw_dir: str,
    provider_config: Optional[Dict[str, Json]] = None,
) -> Tuple[socketserver.BaseServer, str]:
    target = target_base_url.rstrip("/")
    log_path = os.path.join(raw_dir, "responses_retry_log.jsonl")
    provider = provider_config if isinstance(provider_config, dict) else {}
    policy = retry_policy_from_config(
        provider,
        request_timeout_default=600.0,
        total_timeout_default=600.0,
    )

    class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            started = time.monotonic()
            length = int(self.headers.get("Content-Length") or 0)
            request_body = self.rfile.read(length)
            downstream_streaming = False
            try:
                downstream_request = json.loads(
                    request_body.decode("utf-8", errors="ignore") or "{}"
                )
                downstream_streaming = (
                    isinstance(downstream_request, dict)
                    and downstream_request.get("stream") is True
                )
            except json.JSONDecodeError:
                pass
            if provider.get("normalizeResponsesToolOutputs") is True:
                request_body = _normalize_responses_tool_outputs(request_body)
            request_body = _apply_responses_request_limits(
                request_body,
                provider,
            )
            upstream_streaming = False
            try:
                upstream_request = json.loads(
                    request_body.decode("utf-8", errors="ignore") or "{}"
                )
                upstream_streaming = (
                    isinstance(upstream_request, dict)
                    and upstream_request.get("stream") is True
                )
            except json.JSONDecodeError:
                pass
            upstream_headers = {
                "Authorization": "Bearer " + api_key,
                "Content-Type": self.headers.get(
                    "Content-Type",
                    "application/json",
                ),
                "Accept": (
                    self.headers.get("Accept", "text/event-stream")
                    if upstream_streaming
                    else "application/json"
                ),
            }
            for header_name in (
                "OpenAI-Beta",
                "User-Agent",
                "X-Client-Request-Id",
                "Idempotency-Key",
            ):
                header_value = self.headers.get(header_name)
                if header_value:
                    upstream_headers[header_name] = header_value

            def request_factory() -> urllib.request.Request:
                return urllib.request.Request(
                    target + self.path,
                    data=request_body,
                    headers=upstream_headers,
                    method="POST",
                )

            def validate_response(
                status: int,
                headers: Dict[str, str],
                body: bytes,
            ) -> Optional[str]:
                if not 200 <= status < 300:
                    return None
                return _responses_stream_validation_error(
                    request_body,
                    headers,
                    body,
                )

            def record_attempt(value: Dict[str, Json]) -> None:
                _write_jsonl(
                    log_path,
                    {
                        "timestamp": time.time(),
                        "path": self.path,
                        **value,
                    },
                )

            try:
                response = request_with_backoff(
                    request_factory,
                    policy=policy,
                    validate_response=validate_response,
                    on_attempt=record_attempt,
                )
            except RetryRequestError as exc:
                status = int(exc.status or 502)
                if exc.body and exc.status is not None:
                    body = exc.body
                    content_type = next(
                        (
                            value
                            for name, value in exc.headers.items()
                            if name.lower() == "content-type"
                        ),
                        "application/json",
                    )
                else:
                    body = json.dumps(
                        {
                            "error": {
                                "type": "api_retry_exhausted",
                                "message": str(exc),
                                "attempts": exc.attempts,
                            }
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    content_type = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
                return

            content_type = next(
                (
                    value
                    for name, value in response.headers.items()
                    if name.lower() == "content-type"
                ),
                "application/json",
            )
            response_body = response.body
            if downstream_streaming and not upstream_streaming:
                try:
                    response_object = json.loads(
                        response.body.decode("utf-8", errors="ignore") or "{}"
                    )
                    if not isinstance(response_object, dict):
                        raise ValueError("upstream response is not an object")
                    response_body = _responses_json_to_sse(response_object)
                    content_type = "text/event-stream"
                except Exception as exc:
                    body = json.dumps(
                        {
                            "error": {
                                "type": "invalid_upstream_response",
                                "message": str(exc),
                            }
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True
                    return
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(response_body)
            self.wfile.flush()
            _write_jsonl(
                log_path,
                {
                    "timestamp": time.time(),
                    "path": self.path,
                    "status": response.status,
                    "attempts": response.attempts,
                    "durationMs": int(
                        (time.monotonic() - started) * 1000
                    ),
                    "responseBytes": len(response.body),
                    "downstreamResponseBytes": len(response_body),
                    "upstreamStream": upstream_streaming,
                },
            )

    server = _ThreadingServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _start_chat_adapter(
    *,
    target_base_url: str,
    api_key: str,
    model: str,
    raw_dir: str,
    provider_config: Optional[Dict[str, Json]] = None,
) -> Tuple[socketserver.BaseServer, str]:
    target = target_base_url.rstrip("/")
    log_path = os.path.join(raw_dir, "chat_adapter_log.jsonl")
    provider = provider_config if isinstance(provider_config, dict) else {}

    class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_log(self, obj: Dict[str, Json]) -> None:
            _ensure_dir(os.path.dirname(log_path))
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

        def do_POST(self) -> None:
            started = time.time()
            try:
                length = int(self.headers.get("Content-Length") or 0)
                req_obj = json.loads(self.rfile.read(length).decode("utf-8", errors="ignore") or "{}")
                messages = _responses_input_to_chat(req_obj.get("input"))
                if not messages:
                    messages = [{"role": "user", "content": ""}]
                chat_body: Dict[str, Json] = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                }
                max_tokens_field = str(
                    _first_config_value(
                        provider.get("chatMaxTokensField"),
                        provider.get("chat_max_tokens_field"),
                    )
                    or "max_tokens"
                ).strip()
                if max_tokens_field not in {"max_tokens", "max_completion_tokens"}:
                    max_tokens_field = "max_tokens"
                chat_body[max_tokens_field] = _adapter_max_tokens(
                    req_obj, provider
                )
                reasoning_effort = _first_config_value(
                    provider.get("reasoningEffort"),
                    provider.get("reasoning_effort"),
                )
                if reasoning_effort in {
                    "no_think",
                    "none",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                }:
                    chat_body["reasoning_effort"] = reasoning_effort
                parallel_tool_calls = provider.get("parallelToolCalls")
                if not isinstance(parallel_tool_calls, bool):
                    parallel_tool_calls = provider.get("parallel_tool_calls")
                if isinstance(parallel_tool_calls, bool):
                    chat_body["parallel_tool_calls"] = parallel_tool_calls
                prompt_cache_key = _first_config_value(
                    provider.get("promptCacheKey"),
                    provider.get("prompt_cache_key"),
                )
                if prompt_cache_key:
                    chat_body["prompt_cache_key"] = prompt_cache_key
                tools = _responses_tools_to_chat(req_obj.get("tools"))
                tool_names = [str(t.get("function", {}).get("name") or "") for t in tools if isinstance(t.get("function"), dict)]
                if tools:
                    chat_body["tools"] = tools
                    chat_body["tool_choice"] = "auto"

                upstream_req = urllib.request.Request(
                    target + "/chat/completions",
                    data=json.dumps(chat_body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                )
                try:
                    client_timeout = float(
                        provider.get("clientTimeoutSec")
                        or provider.get("client_timeout_sec")
                        or 300
                    )
                except (TypeError, ValueError):
                    client_timeout = 300.0
                policy = retry_policy_from_config(
                    provider,
                    request_timeout_default=max(1.0, client_timeout),
                    total_timeout_default=max(300.0, client_timeout),
                )
                response = request_with_backoff(
                    lambda: upstream_req,
                    policy=policy,
                    on_attempt=lambda value: self._write_log(
                        {
                            "event": "api_attempt",
                            "path": "/chat/completions",
                            **value,
                        }
                    ),
                )
                upstream_obj = json.loads(
                    response.body.decode("utf-8", errors="ignore") or "{}"
                )
                choice = (upstream_obj.get("choices") if isinstance(upstream_obj.get("choices"), list) else [{}])[0]
                msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                usage = _chat_usage_to_responses(upstream_obj.get("usage"))
                rid = str(upstream_obj.get("id") or f"resp_{int(time.time() * 1000)}")

                output_items: List[Dict[str, Json]] = []
                events: List[Dict[str, Json]] = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}}]
                tool_calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
                text = msg.get("content")
                if not isinstance(text, str) or not text:
                    text = msg.get("reasoning_content") if isinstance(msg.get("reasoning_content"), str) else ""
                if not tool_calls:
                    tool_calls = _extract_text_tool_calls(text, tool_names)
                if tool_calls:
                    for i, tc in enumerate(tool_calls):
                        func = tc.get("function") if isinstance(tc, dict) and isinstance(tc.get("function"), dict) else {}
                        args = func.get("arguments") if isinstance(func.get("arguments"), str) else "{}"
                        item = {
                            "id": f"fc_{i}",
                            "type": "function_call",
                            "call_id": str(tc.get("id") or f"call_{i}"),
                            "name": str(func.get("name") or ""),
                            "arguments": args,
                        }
                        output_items.append(item)
                        events.append({"type": "response.output_item.added", "output_index": i, "item": {**item, "arguments": ""}})
                        if args:
                            events.append({"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": i, "delta": args})
                        events.append({"type": "response.output_item.done", "output_index": i, "item": item})
                else:
                    item = {"id": "msg_0", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
                    output_items.append(item)
                    events.extend(
                        [
                            {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_0", "type": "message", "role": "assistant", "content": []}},
                            {"type": "response.content_part.added", "item_id": "msg_0", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
                            {"type": "response.output_text.delta", "item_id": "msg_0", "output_index": 0, "content_index": 0, "delta": text},
                            {"type": "response.output_text.done", "item_id": "msg_0", "output_index": 0, "content_index": 0, "text": text},
                            {"type": "response.content_part.done", "item_id": "msg_0", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text}},
                            {"type": "response.output_item.done", "output_index": 0, "item": item},
                        ]
                    )
                events.append({"type": "response.completed", "response": {"id": rid, "status": "completed", "output": output_items, "usage": usage}})

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for ev in events:
                    self.wfile.write(_sse_event(ev))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self._write_log(
                    {
                        "status": 200,
                        "durationMs": int((time.time() - started) * 1000),
                        "model": model,
                        "messages": len(messages),
                        "tools": len(tools),
                        "toolNames": tool_names,
                        "finishReason": choice.get("finish_reason"),
                        "reasoningEffort": chat_body.get("reasoning_effort"),
                        "maxTokensField": max_tokens_field,
                        "contentHead": text[:300] if isinstance(text, str) else "",
                        "outputItems": len(output_items),
                        "apiAttempts": response.attempts,
                    }
                )
            except RetryRequestError as exc:
                body = exc.body.decode("utf-8", errors="ignore") or str(exc)
                status = int(exc.status or 502)
                self._write_log(
                    {
                        "status": status,
                        "error": body[:2000],
                        "attempts": exc.attempts,
                        "durationMs": int((time.time() - started) * 1000),
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Length",
                    str(len(body.encode("utf-8"))),
                )
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            except Exception as e:
                self._write_log({"status": 500, "error": str(e), "durationMs": int((time.time() - started) * 1000)})
                body = json.dumps({"error": {"message": str(e)}}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = _ThreadingServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def run(
    *,
    prompt: str,
    work_dir: str,
    sandbox_dir: str,
    timeout_s: float,
    api_provider: Dict[str, Json],
    agent_id: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    persist_session: bool = False,
) -> Dict[str, Json]:
    started_at = time.time()
    _load_dotenv()
    _ensure_dir(sandbox_dir)
    raw_dir = os.path.join(sandbox_dir, "raw")
    _ensure_dir(raw_dir)

    # The benchmark image installs the upstream CLI as ``codex`` (see
    # evaluation/docker/Dockerfile), which is the supported way to run this
    # harness. ``CODEX_BIN`` allows pointing at a specific build. Vendor
    # wrappers such as ``tcodex`` are deliberately not accepted: they validate
    # ``--model`` against their own allow-list and reject the 网关model ids
    # these run configs use, failing in confusing ways instead of here.
    codex_bin = _first_config_value(os.environ.get("CODEX_BIN")) or shutil.which("codex")
    if not codex_bin:
        return {
            "status": "error",
            "paths": [],
            "errorMessage": (
                "Missing upstream codex CLI. Run inside the benchmark image "
                "(evaluation/docker) or set CODEX_BIN to an upstream build; "
                "vendor wrappers like tcodex are not supported."
            ),
            "trace": {"runner": "codex", "rawDir": raw_dir, "lastText": ""},
            "metrics": {"turns": None, "promptTokens": None, "completionTokens": None, "totalTokens": None},
            "durationMs": int((time.time() - started_at) * 1000),
        }

    base_url = _normalize_base_url(
        _first_config_value(
            api_provider.get("baseUrl") if isinstance(api_provider, dict) else None,
            api_provider.get("base_url") if isinstance(api_provider, dict) else None,
            os.environ.get("CODEX_BASE_URL"),
        )
    )
    model = _first_config_value(api_provider.get("model") if isinstance(api_provider, dict) else None)
    api_key = resolve_provider_api_key(api_provider, model=model)
    if not api_key and provider_auth_type(api_provider) == "bearer":
        api_key = _first_config_value(
            os.environ.get("CODEX_API_KEY"),
            os.environ.get("OPENAI_API_KEY"),
        )
    if not api_key:
        api_key = None
    if not model:
        return {"status": "error", "paths": [], "errorMessage": "Missing model in api_provider"}
    if not api_key:
        return {"status": "error", "paths": [], "errorMessage": "Missing CODEX_API_KEY/apiProvider.apiKey for Codex harness"}

    adapter_server = None
    responses_retry_server = None
    provider_base_url = base_url
    provider_api_key = api_key
    if _should_use_chat_adapter(model, api_provider):
        adapter_server, provider_base_url = _start_chat_adapter(
            target_base_url=base_url,
            api_key=api_key,
            model=model,
            raw_dir=raw_dir,
            provider_config=api_provider,
        )
        provider_api_key = "local-adapter"
    else:
        responses_retry_server, provider_base_url = (
            _start_responses_retry_proxy(
                target_base_url=base_url,
                api_key=api_key,
                raw_dir=raw_dir,
                provider_config=api_provider,
            )
        )
        provider_api_key = "local-responses-retry-proxy"

    last_message_path = os.path.join(raw_dir, "last_message.txt")
    common_exec_args = [
        "--json",
        "--skip-git-repo-check",
        "--output-last-message",
        last_message_path,
        "--model",
        model,
        "-c",
        "model_provider=\"ripbench\"",
        "-c",
        f"model_providers.ripbench={_provider_config_arg(base_url=provider_base_url)}",
    ]
    if resume_session_id:
        cmd = [
            codex_bin,
            "-a",
            "never",
            "exec",
            "resume",
            *common_exec_args,
            str(resume_session_id),
            "-",
        ]
    else:
        cmd = [
            codex_bin,
            "-a",
            "never",
            "exec",
            *common_exec_args,
            *([] if persist_session else ["--ephemeral"]),
            "--cd",
            os.path.abspath(work_dir),
            "--sandbox",
            _codex_sandbox_mode(api_provider),
            "-",
        ]
    _write_json(
        os.path.join(raw_dir, "codex_invocation.json"),
        {
            "cmd": cmd,
            "cwd": os.path.abspath(work_dir),
            "baseUrl": base_url,
            "providerBaseUrl": provider_base_url,
            "model": model,
            "agentId": agent_id,
            "resumeSessionId": resume_session_id,
            "persistSession": bool(persist_session),
            "chatAdapter": bool(adapter_server),
            "responsesRetryProxy": bool(responses_retry_server),
        },
    )

    env = os.environ.copy()
    env["CODEX_API_KEY"] = provider_api_key
    runtime_codex_home = _materialize_codex_home(sandbox_dir)
    if runtime_codex_home:
        env["CODEX_HOME"] = runtime_codex_home
    used_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None
    stdout_text = ""
    stderr_text = ""
    exit_code = 1
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.abspath(work_dir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout_text, stderr_text = proc.communicate(input=str(prompt or ""), timeout=used_timeout)
            exit_code = int(proc.returncode or 0)
        except subprocess.TimeoutExpired:
            exit_code = 124
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                stdout_text, stderr_text = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout_text, stderr_text = proc.communicate()
    except Exception as e:
        stderr_text = str(e)
        exit_code = 1
    finally:
        if adapter_server is not None:
            try:
                adapter_server.shutdown()
                adapter_server.server_close()
            except Exception:
                pass
        if responses_retry_server is not None:
            try:
                responses_retry_server.shutdown()
                responses_retry_server.server_close()
            except Exception:
                pass

    # A case directory can be moved or recreated while other runs are cleaning up.
    # Recreate raw_dir here so one missing path does not crash the whole runner.
    _ensure_dir(raw_dir)
    for name, text in {
        "codex_stdout.jsonl": stdout_text or "",
        "runner_stdout.txt": stdout_text or "",
        "stdout.txt": stdout_text or "",
        "runner_stderr.txt": stderr_text or "",
        "stderr.txt": stderr_text or "",
    }.items():
        with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    last_text_file = _read_text(last_message_path).strip()
    trace_core = parse_codex_jsonl(stdout_text or "", prompt=str(prompt or ""), started_at=started_at, base_url=base_url, model=model)
    if last_text_file:
        trace_core["lastText"] = last_text_file

    status = "ok"
    error_message = None
    if exit_code == 124:
        status = "timeout"
        error_message = f"Timeout after {timeout_s}s"
    elif exit_code != 0:
        status = "error"
        error_message = (stderr_text or stdout_text or "codex runner failed")[:4000]

    usage_total = trace_core.get("usageTotal") if isinstance(trace_core.get("usageTotal"), dict) else {}
    metrics = {
        "turns": trace_core.get("turns") if isinstance(trace_core.get("turns"), int) else None,
        "promptTokens": int(usage_total.get("prompt_tokens") or 0),
        "completionTokens": int(usage_total.get("completion_tokens") or 0),
        "totalTokens": int(usage_total.get("total_tokens") or 0),
    }

    return {
        "status": status,
        "paths": [],
        "errorMessage": error_message,
        "trace": {
            "runner": "codex",
            "agentId": agent_id,
            "threadId": trace_core.get("threadId"),
            "resumedFromThreadId": resume_session_id,
            "rawDir": raw_dir,
            "lastText": str(trace_core.get("lastText") or ""),
            "executionTrace": trace_core.get("executionTrace") if isinstance(trace_core.get("executionTrace"), list) else [],
            "llm": {"provider": "codex", "baseUrl": base_url, "model": model},
            "usageTotal": usage_total,
            "usageRaw": trace_core.get("usageRaw")
            if isinstance(trace_core.get("usageRaw"), list)
            else [],
            "events": trace_core.get("events"),
        },
        "metrics": metrics,
        "durationMs": int((time.time() - started_at) * 1000),
    }
