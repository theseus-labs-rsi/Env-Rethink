import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from provider_auth import (
    first_provider_value,
    load_dotenv,
    provider_uses_anthropic_app_auth,
    provider_uses_app_credentials,
    resolve_provider_api_key,
)

Json = Any

BASELINES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "baselines"))
CLAUDECODE_JS = os.path.join(BASELINES_DIR, "ClaudeCode.js")


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _materialize_claude_config(sandbox_dir: str) -> Optional[str]:
    """Copy image-installed Claude Code skills to a writable per-run config."""
    template = str(
        os.environ.get("WORKSPACE_BENCH_CLAUDE_CONFIG_DIR")
        or ""
    ).strip()
    if not template or not os.path.isdir(template):
        return None
    destination = os.path.join(
        os.path.abspath(sandbox_dir),
        "claude_config",
        ".claude",
    )
    try:
        import shutil

        shutil.copytree(template, destination, dirs_exist_ok=True)
    except OSError:
        return None
    return destination


def _workspace_mcp_servers() -> Dict[str, Json]:
    """Describe the workspace_env MCP sidecar for Claude Code, when present.

    ``WorkspaceEnvService.environment()`` exports the sidecar's streamable-HTTP
    endpoint and its per-instance bearer token; the token never leaves the
    process (the persisted config keeps only a redacted copy).
    """

    url = str(os.environ.get("WORKSPACE_BENCH_MCP_URL") or "").strip()
    token = str(os.environ.get("WORKSPACE_BENCH_MCP_TOKEN") or "").strip()
    if not url or not token:
        return {}
    return {
        "workspace_env": {
            "type": "http",
            "url": url,
            "headers": {"Authorization": "Bearer " + token},
        }
    }


def _redacted_config(cfg: Json) -> Json:
    """Copy of the run config with live credentials replaced."""

    if not isinstance(cfg, dict):
        return cfg
    redacted = json.loads(json.dumps(cfg))
    for task in redacted.get("tasks", []) if isinstance(redacted.get("tasks"), list) else []:
        servers = task.get("mcpServers") if isinstance(task, dict) else None
        if isinstance(servers, dict):
            for server in servers.values():
                if isinstance(server, dict) and isinstance(server.get("headers"), dict):
                    for header in list(server["headers"]):
                        if header.lower() == "authorization":
                            server["headers"][header] = "Bearer <redacted>"
    return redacted


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _unlink_if_exists(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _safe_json_loads(line: str) -> Optional[Dict[str, Json]]:
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if not isinstance(ms, int):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000.0)) + "Z"
    except Exception:
        return None


def _parse_usage_from_stdout(stdout_text: str) -> Tuple[Dict[str, Json], Optional[str], Optional[str]]:
    usage_total: Dict[str, Json] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "raw": [],
    }
    provider = None
    model = None
    for raw in str(stdout_text or "").splitlines():
        evt = _safe_json_loads(raw.strip())
        if not isinstance(evt, dict):
            continue
        if evt.get("type") != "result":
            continue
        part = evt.get("part")
        if isinstance(part, dict):
            if isinstance(part.get("provider"), str):
                provider = part.get("provider")
            if isinstance(part.get("model"), str):
                model = part.get("model")
        u = evt.get("usage")
        if not isinstance(u, dict):
            u = part.get("usage") if isinstance(part, dict) else None
        if not isinstance(u, dict):
            continue
        usage_total["raw"].append(json.loads(json.dumps(u, ensure_ascii=False)))
        pt = u.get("input_tokens") if isinstance(u.get("input_tokens"), int) else u.get("prompt_tokens")
        ct = u.get("output_tokens") if isinstance(u.get("output_tokens"), int) else u.get("completion_tokens")
        if isinstance(pt, int):
            usage_total["prompt_tokens"] += pt
        if isinstance(ct, int):
            usage_total["completion_tokens"] += ct
    usage_total["total_tokens"] = usage_total["total_tokens"] or (usage_total["prompt_tokens"] + usage_total["completion_tokens"])
    return usage_total, provider, model


def _load_usage_log(path: str) -> List[Dict[str, Json]]:
    if not os.path.isfile(path):
        return []
    out: List[Dict[str, Json]] = []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                value = _safe_json_loads(line.strip())
                if isinstance(value, dict):
                    out.append(value)
    except OSError:
        return []
    return out


def _usage_from_bridge(rows: List[Dict[str, Json]]) -> Dict[str, Json]:
    total: Dict[str, Json] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "raw": [],
    }
    for row in rows:
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        input_tokens = int(
            usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        )
        output_tokens = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
        input_details = (
            usage.get("input_tokens_details")
            if isinstance(usage.get("input_tokens_details"), dict)
            else (
                usage.get("prompt_tokens_details")
                if isinstance(usage.get("prompt_tokens_details"), dict)
                else {}
            )
        )
        output_details = (
            usage.get("output_tokens_details")
            if isinstance(usage.get("output_tokens_details"), dict)
            else (
                usage.get("completion_tokens_details")
                if isinstance(usage.get("completion_tokens_details"), dict)
                else {}
            )
        )
        total["prompt_tokens"] += input_tokens
        total["completion_tokens"] += output_tokens
        total["total_tokens"] += int(
            usage.get("total_tokens") or (input_tokens + output_tokens)
        )
        total["cache_read"] += int(input_details.get("cached_tokens") or 0)
        total["cache_write"] += int(output_details.get("reasoning_tokens") or 0)
        total["raw"].append(json.loads(json.dumps(usage, ensure_ascii=False)))
    return total


def _declared_context_tokens(api_provider: Dict[str, Json], model: object) -> str:
    """声明模型真实上下文窗口（token 数，字符串）。

    yaml 的 `claude_context_tokens` 优先；否则按模型名回落到厂商已知窗口。
    CLI 侧要**两件事同时做**才生效（cli.js）：
      * `window = Math.min(Hk(model), CLAUDE_CODE_AUTO_COMPACT_WINDOW)`
      * `Hk()` 只认模型名里的 `[1m]` 标记（xW()）或内置 Claude 模型表，
        光设 CLAUDE_CODE_MAX_CONTEXT_TOKENS 没用（除非同时 DISABLE_COMPACT）。
    """
    declared = str(
        (api_provider.get("claudeContextTokens") or "").strip()
        if isinstance(api_provider, dict)
        else ""
    )
    if not declared:
        mid = str(model or "").lower()
        if "grok-4.6" in mid:
            declared = "500000"
        elif any(marker in mid for marker in (
            "glm-5.3", "kimi-k3", "muse-spark-1.2", "deepseek", "qwen3.8-flash",
        )):
            declared = "1000000"
    return declared if declared.isdigit() and int(declared) > 0 else ""


# 文本型后端（chat.completions 网关上的 deepseek/glm/kimi 等）无法消化 Read
# 返回的 image 内容块：网关会以 400 "An assistant message with 'tool_calls' must
# be followed by tool messages responding to each 'tool_call_id'" 终结整个 run
# （2026-09-18 deepseek 三臂实测约 20% 的 case 因此直接失败）。对这些模型族打开
# harness 侧图片读取拒绝，引导改用 pdftotext/OCR 等文本通道。
_TEXT_ONLY_MEDIA_FAMILIES = ("deepseek", "glm", "kimi", "muse", "qwen")


def _text_only_media_model(model: object) -> bool:
    name = str(model or "").lower()
    return any(family in name for family in _TEXT_ONLY_MEDIA_FAMILIES)


def _one_m_marker(tokens: str) -> str:
    """声明了 ≥1M 窗口时返回 `[1m]`，否则空串。"""
    return "[1m]" if tokens and int(tokens) >= 1_000_000 else ""


def _start_app_credential_bridge(
    *,
    api_provider: Dict[str, Json],
    raw_dir: str,
) -> Tuple[subprocess.Popen[bytes], str, str]:
    config_path = os.path.join(raw_dir, "anthropic_responses_bridge_config.json")
    ready_path = os.path.join(raw_dir, "anthropic_responses_bridge_ready.json")
    usage_path = os.path.join(raw_dir, "anthropic_responses_bridge_usage.jsonl")
    _unlink_if_exists(ready_path)
    _unlink_if_exists(usage_path)
    # CLI 用它自己的模型名（customProvider.modelName / ANTHROPIC_MODEL）判定上下文
    # 窗口，1M 要靠模型名里的 `[1m]` 标记（cli.js 的 xW()）。但那个标记**不能**传
    # 到网关——bridge 取模型名的优先级是
    #   authModel > provider_config.model > payload.model
    # 所以这里落盘前把标记剥掉：CLI 看到带标记的名字、上游只看到干净的名字。
    _bridge_cfg = json.loads(json.dumps(api_provider, ensure_ascii=False))
    for _k in ("model", "authModel"):
        _v = _bridge_cfg.get(_k)
        if isinstance(_v, str) and _v.strip().lower().endswith("[1m]"):
            _bridge_cfg[_k] = _v.strip()[:-4].strip()
    _write_json(config_path, _bridge_cfg)
    env = os.environ.copy()
    src_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_root
        if not existing_pythonpath
        else src_root + os.pathsep + existing_pythonpath
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "anthropic_responses_bridge",
            "--config",
            config_path,
            "--ready-file",
            ready_path,
            "--usage-log",
            usage_path,
            "--port",
            "0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = b""
            if process.stderr is not None:
                stderr = process.stderr.read()
            raise RuntimeError(
                "Anthropic Responses bridge exited during startup: "
                + stderr.decode("utf-8", errors="ignore")[:2000]
            )
        if os.path.isfile(ready_path):
            ready = _read_json(ready_path)
            if isinstance(ready, dict) and isinstance(ready.get("baseUrl"), str):
                return process, str(ready["baseUrl"]), usage_path
        time.sleep(0.05)
    process.terminate()
    raise RuntimeError("timed out waiting for Anthropic Responses bridge")


def _stop_bridge(process: Optional[subprocess.Popen[bytes]]) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


def _native_anthropic_probe_timeout(api_provider: Dict[str, Json]) -> float:
    raw = api_provider.get("nativeAnthropicProbeTimeoutSec")
    if raw is None:
        raw = api_provider.get("native_anthropic_probe_timeout_sec")
    try:
        timeout = float(raw) if raw is not None else 30.0
    except (TypeError, ValueError):
        timeout = 30.0
    return min(max(timeout, 1.0), 120.0)


def _native_anthropic_probe_enabled(api_provider: Dict[str, Json]) -> bool:
    raw = api_provider.get("nativeAnthropicProbe")
    if raw is None:
        raw = api_provider.get("native_anthropic_probe")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _native_anthropic_sdk_base_url(base_url: str) -> str:
    """Normalize an API-style URL for Claude Code's Anthropic client.

    网关's direct compatibility endpoint is conventionally exposed as
    ``.../v1/messages``.  Claude Code appends ``/v1/messages`` itself, so
    giving it ``.../v1`` produces a non-existent ``.../v1/v1/messages`` path.
    """
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized


def _probe_native_anthropic_messages(
    *,
    base_url: str,
    model: str,
    credential: str,
    timeout_s: float,
) -> Dict[str, Json]:
    """Check whether an 网关model supports native Anthropic tool interactions.

    The Claude Agent SDK uses this protocol directly.  We deliberately make a
    tiny tool-use followed by tool-result round trip before launching the
    agent.  A provider that only exposes a Responses-compatible endpoint, or
    cannot maintain Anthropic tool-call history, can then be routed through
    the local bridge instead.  The returned record is safe to persist: it
    never contains credentials or response text.
    """
    endpoint = str(base_url or "").strip().rstrip("/") + "/messages"
    if not endpoint or not str(model or "").strip() or not credential:
        return {
            "compatible": False,
            "endpoint": endpoint,
            "reason": "missing base URL, model, or credential",
        }

    def post_messages(payload: Dict[str, Json]) -> Tuple[int, Dict[str, Json]]:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + credential,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = int(getattr(response, "status", 200) or 200)
            body = response.read().decode("utf-8", errors="ignore")
        parsed = _safe_json_loads(body)
        if not isinstance(parsed, dict):
            raise ValueError("response was not a JSON object")
        return status, parsed

    tool_definition: Dict[str, Json] = {
        "name": "ping",
        "description": "A compatibility probe tool. Call it once when asked.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    }
    first_payload: Dict[str, Json] = {
        "model": str(model).strip(),
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Use the ping tool exactly once with value \"ok\". "
                    "Do not write prose."
                ),
            }
        ],
        "tools": [tool_definition],
        "tool_choice": {"type": "auto"},
    }
    try:
        try:
            first_status, first_response = post_messages(first_payload)
        except urllib.error.HTTPError as exc:
            # Some Anthropic-compatible backends reject `tool_choice` even in
            # its most permissive form (e.g. muse-spark-1.2: 400 "did not
            # match any supported type"), while still supporting tools when
            # the field is simply omitted.  Claude Code itself never sends
            # tool_choice, so probing without it reflects the real traffic.
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 400 and "tool_choice" in detail:
                first_payload.pop("tool_choice", None)
                first_status, first_response = post_messages(first_payload)
            else:
                raise
        first_content = first_response.get("content")
        tool_uses = [
            block
            for block in first_content
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == tool_definition["name"]
            and isinstance(block.get("id"), str)
            and block.get("id")
        ] if isinstance(first_content, list) else []
        if len(tool_uses) != 1:
            return {
                "compatible": False,
                "endpoint": endpoint,
                "httpStatus": first_status,
                "responseType": first_response.get("type"),
                "responseRole": first_response.get("role"),
                "stopReason": first_response.get("stop_reason"),
                "reason": "probe did not return one Anthropic tool_use block",
            }

        tool_use = tool_uses[0]
        second_payload: Dict[str, Json] = {
            "model": str(model).strip(),
            "max_tokens": 2048,
            "messages": [
                first_payload["messages"][0],
                {"role": "assistant", "content": first_content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use["id"],
                            "content": "probe complete",
                        }
                    ],
                },
            ],
            "tools": [tool_definition],
            # tool_choice omitted: mirrors Claude Code's own requests and
            # avoids the tool_choice rejection seen on some backends.
        }
        second_status, second_response = post_messages(second_payload)
        compatible = (
            200 <= first_status < 300
            and 200 <= second_status < 300
            and first_response.get("type") == "message"
            and first_response.get("role") == "assistant"
            and first_response.get("stop_reason") == "tool_use"
            and second_response.get("type") == "message"
            and second_response.get("role") == "assistant"
        )
        return {
            "compatible": compatible,
            "endpoint": endpoint,
            "httpStatus": second_status,
            "firstHttpStatus": first_status,
            "firstStopReason": first_response.get("stop_reason"),
            "responseType": second_response.get("type"),
            "responseRole": second_response.get("role"),
            "stopReason": second_response.get("stop_reason"),
            "reason": (
                None
                if compatible
                else "tool-result response was not an Anthropic message"
            ),
        }
    except urllib.error.HTTPError as exc:
        return {
            "compatible": False,
            "endpoint": endpoint,
            "httpStatus": int(exc.code),
            "reason": f"HTTP {int(exc.code)}",
        }
    except Exception as exc:
        return {
            "compatible": False,
            "endpoint": endpoint,
            "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
        }


def _optional_positive_int(api_provider: Dict[str, Json], *keys: str) -> Optional[int]:
    for key in keys:
        raw = api_provider.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _build_execution_trace(*, prompt: str, started_ms: int, task_result: Dict[str, Json], usage_total: Dict[str, int], llm_base_url: Optional[str], llm_model: Optional[str], llm_provider: Optional[str]) -> List[Dict[str, Json]]:
    out: List[Dict[str, Json]] = []
    out.append(
        {
            "type": "text",
            "role": "user",
            "content": str(prompt or ""),
            "timestamp": _ms_to_iso(started_ms),
        }
    )

    traj = task_result.get("trajectory")
    if isinstance(traj, list):
        for it in traj:
            if not isinstance(it, dict):
                continue
            typ = it.get("type")
            ts_ms = it.get("timestamp") if isinstance(it.get("timestamp"), int) else None
            ts = _ms_to_iso(ts_ms)
            if typ == "text":
                txt = it.get("text") if isinstance(it.get("text"), str) else ""
                ev: Dict[str, Json] = {
                    "type": "text",
                    "role": "assistant",
                    "content": txt,
                    "timestamp": ts,
                    "turn": None,
                    "llm": {
                        "provider": llm_provider,
                        "baseUrl": llm_base_url,
                        "model": llm_model,
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0},
                        "stopReason": None,
                        "errorMessage": None,
                    },
                }
                out.append(ev)
            elif typ == "tool_call":
                tool = it.get("tool") if isinstance(it.get("tool"), str) else None
                call_id = it.get("callID") if isinstance(it.get("callID"), str) else None
                dur = it.get("durationMs") if isinstance(it.get("durationMs"), int) else None
                exit_code = it.get("exitCode") if isinstance(it.get("exitCode"), int) else None
                state = it.get("state") if isinstance(it.get("state"), str) else None
                status = None
                if state == "completed":
                    status = "completed"
                elif state == "failed":
                    status = "failed"
                ev2: Dict[str, Json] = {
                    "type": "tool",
                    "role": "tool",
                    "tool": tool,
                    "callID": call_id,
                    "timestamp": ts,
                    "startedAt": ts,
                    "finishedAt": None,
                    "durationMs": dur,
                    "status": status,
                    "exitCode": exit_code,
                    "input": it.get("input") if isinstance(it.get("input"), dict) else {},
                    "output": it.get("output") if isinstance(it.get("output"), (dict, list, str)) else None,
                }
                if isinstance(dur, int) and ts_ms is not None:
                    ev2["finishedAt"] = _ms_to_iso(int(ts_ms + dur))
                out.append(ev2)

    for i in range(len(out) - 1, -1, -1):
        ev = out[i]
        if ev.get("type") == "text" and ev.get("role") == "assistant" and isinstance(ev.get("llm"), dict):
            ev["llm"]["usage"] = usage_total
            break

    return out


def run(
    *,
    prompt: str,
    work_dir: str,
    sandbox_dir: str,
    timeout_s: float,
    api_provider: Dict[str, Json],
    agent_id: Optional[str] = None,
) -> Dict[str, Json]:
    started_at = time.time()
    eval_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    load_dotenv(os.path.join(eval_root, ".env"), os.path.join(os.getcwd(), ".env"))
    _ensure_dir(sandbox_dir)
    raw_dir = os.path.join(sandbox_dir, "raw")
    _ensure_dir(raw_dir)

    if not os.path.exists(CLAUDECODE_JS):
        return {"status": "error", "paths": [], "errorMessage": f"Missing ClaudeCode.js: {CLAUDECODE_JS}"}

    provider_type = api_provider.get("provider_type") if isinstance(api_provider, dict) else None
    base_url = api_provider.get("baseUrl") if isinstance(api_provider, dict) else None
    model = api_provider.get("model") if isinstance(api_provider, dict) else None
    api_key = api_provider.get("apiKey") if isinstance(api_provider, dict) else None
    model_name = api_provider.get("model_name") if isinstance(api_provider, dict) else None

    task_id = os.path.basename(os.path.abspath(sandbox_dir))
    cfg_path = os.path.join(raw_dir, "claudecode_config.json")
    report_path = os.path.join(raw_dir, "claudecode_report.json")
    _unlink_if_exists(report_path)

    bridge_process: Optional[subprocess.Popen[bytes]] = None
    bridge_usage_path: Optional[str] = None
    effective_base_url = base_url
    effective_api_key = api_key
    gateway_anthropic_messages = provider_uses_anthropic_app_auth(api_provider)
    native_anthropic_direct = False
    native_anthropic_probe: Optional[Dict[str, Json]] = None

    if gateway_anthropic_messages:
        resolved_base_url = first_provider_value(base_url)
        resolved_model = first_provider_value(model)
        credential = resolve_provider_api_key(
            api_provider,
            model=resolved_model,
        )
        if (
            _native_anthropic_probe_enabled(api_provider)
            and resolved_base_url
            and resolved_model
            and credential
        ):
            native_anthropic_probe = _probe_native_anthropic_messages(
                base_url=resolved_base_url,
                model=resolved_model,
                credential=credential,
                timeout_s=_native_anthropic_probe_timeout(api_provider),
            )
        else:
            native_anthropic_probe = {
                "compatible": False,
                "endpoint": (
                    resolved_base_url.rstrip("/") + "/messages"
                    if resolved_base_url
                    else None
                ),
                "reason": "native Anthropic probe disabled or provider configuration incomplete",
            }

        _write_json(
            os.path.join(raw_dir, "native_anthropic_probe.json"),
            native_anthropic_probe,
        )
        if native_anthropic_probe.get("compatible") is True:
            native_anthropic_direct = True
            provider_type = "anthropic"
            effective_base_url = _native_anthropic_sdk_base_url(
                resolved_base_url
            )
            effective_api_key = credential

    # Native 网关Anthropic providers use /messages directly whenever the
    # probe succeeds.  All legacy app-credential providers, plus native
    # providers whose /messages probe failed, retain the Responses bridge
    # fallback for backward compatibility.
    if provider_uses_app_credentials(api_provider) and not native_anthropic_direct:
        try:
            bridge_process, effective_base_url, bridge_usage_path = (
                _start_app_credential_bridge(
                    api_provider=api_provider,
                    raw_dir=raw_dir,
                )
            )
            effective_api_key = "local-anthropic-bridge"
            provider_type = "anthropic"
        except Exception as exc:
            return {
                "status": "error",
                "paths": [],
                "errorMessage": f"Failed to start ClaudeCode API bridge: {exc}",
                "trace": {
                    "runner": "claudecode",
                    "rawDir": raw_dir,
                    "lastText": "",
                },
                "metrics": {
                    "turns": None,
                    "promptTokens": None,
                    "completionTokens": None,
                    "totalTokens": None,
                },
                "durationMs": int((time.time() - started_at) * 1000),
            }

    configured_max_turns = _optional_positive_int(
        api_provider,
        "claudeMaxTurns",
        "claude_max_turns",
    )
    if configured_max_turns is None and gateway_anthropic_messages:
        # Native Anthropic-compatible providers commonly lack the internal
        # context-budget protections of Claude's first-party backend.  A
        # bounded default avoids unbounded workspace exploration if compacting
        # is unavailable, while providers can still override it explicitly.
        # 2026-08-29: raised from 48 to 200 — gateway streaming responses drop
        # the cache usage fields so autocompact never triggers (see the
        # CLAUDE_CODE_MAX_CONTEXT_TOKENS comment above); with compaction
        # unreliable, a tight turn cap cut off capable models on long
        # exploration tasks (glm/kimi/muse "maximum number of turns" losses).
        # 200 turns + the true context window declaration keeps runaway
        # sessions bounded while letting slow-but-methodical runs finish.
        configured_max_turns = 200
    cfg = {
        "description": f"claudecode run: {task_id}",
        "tasks": [
            {
                "id": task_id,
                "name": task_id,
                "prompt": str(prompt or ""),
                "cwd": os.path.abspath(work_dir),
                "timeout": int(timeout_s) if isinstance(timeout_s, (int, float)) else 300,
                "provider": str(provider_type) if isinstance(provider_type, str) else None,
                "model": (
                    (
                        str(model)
                        if native_anthropic_direct and isinstance(model, str)
                        else (
                            "sonnet"
                            if gateway_anthropic_messages
                            else (
                                str(model_name)
                                if isinstance(model_name, str)
                                else (str(model) if isinstance(model, str) else "")
                            )
                        )
                    )
                    # 声明 ≥1M 窗口时给 CLI 侧的模型名挂 `[1m]` 标记——CLI 靠它
                    # （xW()）才认 1M；bridge 落盘时会剥掉，不会传到网关。
                    + _one_m_marker(
                        _declared_context_tokens(api_provider, model)
                    )
                ),
                "maxTurns": configured_max_turns,
                "includePartialMessages": False,
                "mcpServers": _workspace_mcp_servers(),
                "textOnlyMedia": _text_only_media_model(model),
                "customProvider": {
                    "baseUrl": str(effective_base_url) if isinstance(effective_base_url, str) else None,
                    "apiKey": str(effective_api_key) if isinstance(effective_api_key, str) else None,
                    "modelName": (
                        str(model)
                        if native_anthropic_direct and isinstance(model, str)
                        else (
                            "sonnet"
                            if gateway_anthropic_messages
                            else (
                                str(model)
                                if isinstance(model, str)
                                else (
                                    str(model_name)
                                    if isinstance(model_name, str)
                                    else None
                                )
                            )
                        )
                    ),
                },
            }
        ],
    }
    # The live config carries the MCP bearer token, so it stays in the case's
    # private runtime directory; the copy under raw/ (which reaches the judge
    # case) keeps only the redacted header.
    live_cfg_path = os.path.join(sandbox_dir, ".runtime", "claudecode_config.json")
    _write_json(live_cfg_path, cfg)
    _write_json(cfg_path, _redacted_config(cfg))

    env = os.environ.copy()
    runtime_claude_config = _materialize_claude_config(sandbox_dir)
    if runtime_claude_config:
        env["WORKSPACE_BENCH_CLAUDE_CONFIG_DIR"] = runtime_claude_config
    if isinstance(provider_type, str) and provider_type.strip().lower() == "anthropic":
        if isinstance(effective_api_key, str) and effective_api_key.strip():
            env["ANTHROPIC_AUTH_TOKEN"] = effective_api_key.strip()
            env["ANTHROPIC_API_KEY"] = effective_api_key.strip()
        if isinstance(effective_base_url, str) and effective_base_url.strip():
            env["ANTHROPIC_BASE_URL"] = effective_base_url.strip()
        if isinstance(model, str) and model.strip():
            env["ANTHROPIC_MODEL"] = (
                model.strip()
                if native_anthropic_direct
                else ("sonnet" if gateway_anthropic_messages else model.strip())
            )
            # 自建/第三方端点(非托管网关)——例如自部署推理服务上的模型
            # ——CLI 的模型注册表里没有我们的模型名,必须显式声明为「自定义模型
            # 选项」,否则 CLI 在**发出任何请求之前**就报
            #   There's an issue with the selected model (<id>). It may not
            #   exist or you may not have access to it.
            # (实测:2.4s 失败、promptTokens=0、服务侧 /v1/models 完全正常)。
            # 2026-09-15 镜像重建后 CLI 对自定义模型名的校验变严,此前不设也能过。
            custom_model_option = native_anthropic_direct or (
                not gateway_anthropic_messages
                and isinstance(effective_base_url, str)
                and bool(effective_base_url.strip())
            )
            if custom_model_option:
                # Register the provider model as a Claude Code custom model
                # option.  Without this, Claude Code rejects a valid native
                # Anthropic endpoint merely because the model ID is not a
                # first-party Claude alias.
                env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = model.strip()
                env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = (
                    str(model_name).strip()
                    if isinstance(model_name, str) and model_name.strip()
                    else model.strip()
                )
                env[
                    "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES"
                ] = "tool_use"
                # 后台小模型(会话标题、快速摘要等)默认打
                # `claude-haiku-4-5-20251001`——自建端点上没有该模型,会拿到 404,
                # 而 Claude Code 把 404 归因成「当前选中的模型不可用」:
                #   POST /v1/messages?beta=true -> 404
                #   The model `claude-haiku-4-5-20251001` does not exist.
                #   → "There's an issue with the selected model (<我们的模型>)."
                # 把 haiku 档也指向同一个模型,避免前台被后台拖垮的假故障。
                env["ANTHROPIC_SMALL_FAST_MODEL"] = model.strip()
                env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model.strip()
                env["ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"] = (
                    str(model_name).strip()
                    if isinstance(model_name, str) and model_name.strip()
                    else model.strip()
                )
                env[
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES"
                ] = "tool_use"
                # Reasoning-tier models (GLM-5.3, Kimi-K3) reject requests
                # without an effort tier (upstream error 1210 "该模型始终思考，
                # 不支持关闭思考").  Claude Code only emits output_config.effort
                # when it can resolve an effort level; with a task-local
                # CLAUDE_CONFIG_DIR that has no persisted userID settings it
                # silently omits the field entirely.  CLAUDE_CODE_EFFORT_LEVEL
                # takes precedence over every other effort source, so pin the
                # configured tier here.
                effort = str(
                    (api_provider.get("reasoningEffort") or "").strip()
                    if isinstance(api_provider, dict)
                    else ""
                ).lower()
                if effort:
                    env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
                # Third-party Anthropic gateways drop the cache usage fields
                # from streaming responses (message_delta carries only
                # input/output tokens; message_start has the fields but all
                # zeros), so Claude Code's context accounting only sees the
                # uncached input slice and autocompact never fires — the
                # conversation grows until the model's real context limit
                # 400s (observed: grok-4.6 failing at 832K tokens against
                # its 500K window).  Declare the true window per model so the
                # CLI at least computes the threshold against reality; the
                # per-model values below match the vendor context windows.
                context_env = str(
                    (api_provider.get("claudeContextTokens") or "").strip()
                    if isinstance(api_provider, dict)
                    else ""
                )
                if not context_env:
                    model_id = str(model or "").lower()
                    if "grok-4.6" in model_id:
                        context_env = "500000"
                    elif any(
                        marker in model_id
                        for marker in (
                            "glm-5.3",
                            "kimi-k3",
                            "muse-spark-1.2",
                            "deepseek",
                            "qwen3.8-flash",
                        )
                    ):
                        context_env = "1000000"
                if context_env.isdigit() and int(context_env) > 0:
                    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = context_env
                    # ⚠️ autocompact 看的**不是**上面那个变量,而是
                    # CLAUDE_CODE_AUTO_COMPACT_WINDOW;它的阈值算在
                    # MP6(): warning = window-20000(压缩触发点)、
                    # blocking = window-3000。且 CLI 把该变量**硬钳制在 [100000, 1000000]**
                    # (cli.js: S_7=1e5, WLK=1e6),所以压不到 10 万以下。
                    # 用 context_env 作为唯一来源:它已经是"该模型的真实窗口"
                    # (yaml 显式声明优先,否则取上面的 per-model 默认值)。只设
                    # MAX_CONTEXT_TOKENS 而不设这个,压缩窗口会留在 CLI 默认值上,
                    # 大工作区照样报 "Prompt is too long"(实测:task108 88 文件)。
                    if context_env.isdigit() and int(context_env) > 0:
                        auto_win = max(100000, int(context_env) - 16384)
                        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(auto_win)
                        # ⚠️ 光设上面两个变量**没用**：CLI 的实际阈值是
                        #   window = Math.min(modelWindow, autoCompactWindow)
                        # 而 modelWindow 由 Hk() 给出，它只认三种来源：
                        #   ① DISABLE_COMPACT + MAX_CONTEXT_TOKENS（同时设才算，且会关掉压缩）
                        #   ② **模型名匹配 /\[1m\]/i**（xW()）→ 1e6
                        #   ③ 内置 Claude 模型表 → 否则回落到默认 qh1=200000
                        # 所以声明 1M 窗口必须**同时**给模型名加 `[1m]` 标记。
                        # 安全前提：本地 bridge 取模型名时优先用它自己配置里的
                        # `model`（见 anthropic_responses_bridge.py 的
                        # `provider_config.get("model") or payload.get("model")`），
                        # 所以 payload 里的 `[1m]` 不会被透传到上游网关。
                        if int(context_env) >= 1_000_000:
                            for key in ("ANTHROPIC_MODEL",
                                        "ANTHROPIC_CUSTOM_MODEL_OPTION"):
                                cur = env.get(key)
                                if isinstance(cur, str) and cur.strip() and \
                                        not cur.strip().lower().endswith("[1m]"):
                                    env[key] = cur.strip() + "[1m]"
                # 把配置里的 max_output_tokens 真正接到 CLI 上。
                # 不设的话 CLI 用自带默认值 32000 请求输出,把可用输入预算压到
                # context-32000(实测:131072 上下文的模型在 ~98K 输入时报
                # "maximum context length is 131072 tokens ... you requested
                # 32000 output tokens")。此前 YAML 的 agent.max_output_tokens
                # 只写进 maxOutputTokens 字段、对 CLI 请求毫无影响,是个静默空操作。
                max_out = (
                    api_provider.get("maxOutputTokens")
                    if isinstance(api_provider, dict)
                    else None
                )
                try:
                    max_out_int = int(max_out)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    max_out_int = 0
                if max_out_int > 0:
                    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_out_int)
            if bridge_process is not None and not gateway_anthropic_messages:
                env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = model.strip()
                env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = (
                    str(model_name).strip()
                    if isinstance(model_name, str) and model_name.strip()
                    else model.strip()
                )
                env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"] = (
                    "OpenAI-compatible API via local Anthropic compatibility bridge"
                )
                env[
                    "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES"
                ] = "tool_use"

    cmd = ["node", CLAUDECODE_JS, live_cfg_path, "-o", report_path]
    used_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.abspath(os.path.join(BASELINES_DIR, "..")),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Read bytes and decode leniently below. With text=True Python
            # decodes strictly, so a single multi-byte character straddling an
            # internal read boundary raises UnicodeDecodeError and the entire
            # run output is discarded -- observed as unexplained judge retries
            # on tasks with Chinese output.
            text=False,
        )
        try:
            stdout_text, stderr_text = proc.communicate(timeout=used_timeout)
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
        exit_code = 1
        stdout_text = ""
        stderr_text = str(e)
    finally:
        _stop_bridge(bridge_process)

    if isinstance(stdout_text, (bytes, bytearray)):
        try:
            stdout_text = stdout_text.decode("utf-8", errors="ignore")
        except Exception:
            stdout_text = ""
    if isinstance(stderr_text, (bytes, bytearray)):
        try:
            stderr_text = stderr_text.decode("utf-8", errors="ignore")
        except Exception:
            stderr_text = ""

    with open(os.path.join(raw_dir, "runner_stdout.txt"), "w", encoding="utf-8") as f:
        f.write(stdout_text)
    with open(os.path.join(raw_dir, "runner_stderr.txt"), "w", encoding="utf-8") as f:
        f.write(stderr_text)
    with open(os.path.join(raw_dir, "stdout.txt"), "w", encoding="utf-8") as f:
        f.write(stdout_text)
    with open(os.path.join(raw_dir, "stderr.txt"), "w", encoding="utf-8") as f:
        f.write(stderr_text)

    if not os.path.exists(report_path) or not os.path.isfile(report_path):
        status = "timeout" if exit_code == 124 else "error"
        return {
            "status": status,
            "paths": [],
            "errorMessage": (f"Timeout after {timeout_s}s" if status == "timeout" else (stderr_text[:2000] if isinstance(stderr_text, str) else "claudecode runner failed")),
            "trace": {"runner": "claudecode", "rawDir": raw_dir, "lastText": ""},
            "metrics": {"turns": None, "promptTokens": None, "completionTokens": None, "totalTokens": None},
            "durationMs": int((time.time() - started_at) * 1000),
        }

    report = _read_json(report_path)
    tasks = report.get("tasks") if isinstance(report, dict) else None
    tr = tasks[0] if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else {}

    st = str(tr.get("status") or "").strip().lower()
    status = "ok"
    if exit_code == 124 or st == "timeout":
        status = "timeout"
    elif exit_code != 0 or st != "passed":
        status = "error"

    usage_total, provider2, model2 = _parse_usage_from_stdout(tr.get("stdout") if isinstance(tr.get("stdout"), str) else "")
    bridge_usage = (
        _usage_from_bridge(_load_usage_log(bridge_usage_path))
        if isinstance(bridge_usage_path, str)
        else None
    )
    if isinstance(bridge_usage, dict) and bridge_usage.get("raw"):
        usage_total = bridge_usage
    llm_provider = provider2 or (str(provider_type) if isinstance(provider_type, str) else None)
    llm_model = model2 or (str(model) if isinstance(model, str) else None)

    started_ms = int(started_at * 1000)
    execution_trace = _build_execution_trace(
        prompt=str(prompt or ""),
        started_ms=started_ms,
        task_result=tr if isinstance(tr, dict) else {},
        usage_total=usage_total,
        llm_base_url=str(base_url) if isinstance(base_url, str) else None,
        llm_model=llm_model,
        llm_provider=llm_provider,
    )

    last_text = ""
    tos = tr.get("textOutputs") if isinstance(tr, dict) else None
    if isinstance(tos, list) and tos and isinstance(tos[-1], str):
        last_text = str(tos[-1])

    metrics = {
        "turns": sum(1 for x in execution_trace if isinstance(x, dict) and x.get("type") == "text" and x.get("role") == "assistant"),
        "promptTokens": usage_total.get("prompt_tokens"),
        "completionTokens": usage_total.get("completion_tokens"),
        "totalTokens": usage_total.get("total_tokens"),
    }

    err_msg = None
    if status == "timeout":
        err_msg = f"Timeout after {timeout_s}s"
    elif status == "error":
        em = tr.get("errorMessage") if isinstance(tr, dict) else None
        stderr_message = stderr_text[:2000] if isinstance(stderr_text, str) else ""
        err_msg = (str(em) if isinstance(em, str) and em else stderr_message) or (
            f"ClaudeCode runner exited with code {exit_code}" if exit_code else "claudecode error"
        )

    return {
        "status": status,
        "paths": [],
        "errorMessage": err_msg,
        "trace": {
            "runner": "claudecode",
            "agentId": agent_id,
            "rawDir": raw_dir,
            "lastText": last_text,
            "executionTrace": execution_trace,
            "llm": {"provider": llm_provider, "baseUrl": str(base_url) if isinstance(base_url, str) else None, "model": llm_model},
            "usageTotal": usage_total,
            "usageRaw": usage_total.get("raw")
            if isinstance(usage_total.get("raw"), list)
            else [],
            "apiBridge": "anthropic-to-responses"
            if bridge_process is not None
            else None,
            "apiTransport": (
                "native-anthropic-messages"
                if native_anthropic_direct
                else (
                    "anthropic-to-responses-bridge"
                    if bridge_process is not None
                    else None
                )
            ),
            "nativeAnthropicProbe": native_anthropic_probe,
        },
        "metrics": metrics,
        "durationMs": int((time.time() - started_at) * 1000),
    }
