"""把 agentkit 接到 envgen-kit 的 Codex runner 契约上。

envgen-kit **刻意不带 runner**（它跟具体 CLI 版本、凭据、沙箱方式强绑定）；
这个文件就是那个口子 —— 用本仓的 `agentkit` 在一个 docker 容器里跑 codex。

    export ENVGEN_RUNNER=workspace_env.agentkit_runner:run
    # 或 --runner workspace_env.agentkit_runner:run

## 与契约的对应

契约（`workspace_env/runner.py`）要求返回：

    status                      "ok" 才算成功
    trace.collection.complete   **必须是 True** —— 不完整的 JSONL trace 绝不允许变成成功候选
    trace.executionTrace        规范化工具事件（每项含 type/tool/status）
    durationMs

编排器还有两条我们自己要守的：
  · runner **不得写 work_dir**（编排器在调用前后对 WORKSPACE_ROOT / TASK_INPUT_ROOT 做哈希）——
    所以容器里只挂这两棵子树为只读，`output/` 仍可写。
  · `status != "ok"` 或 `collection.complete is not True` 会让整个角色中止，
    所以**宁可报失败，也不要把不完整的 trace 报成成功**。

## 模型连接

从 `api_provider` 取 `model` / `baseUrl`，凭据从环境变量取
（`TB_API_KEY`，兼容 `CODEX_API_KEY` / `OPENAI_API_KEY`）。本仓不内置任何端点。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from pathlib import Path
from typing import Any

# 容器内挂载点
WORK_MOUNT = "/work"
SANDBOX_MOUNT = "/sandbox"
READONLY_SUBTREES = ("WORKSPACE_ROOT", "TASK_INPUT_ROOT")


def _resolve_connection(api_provider: dict[str, Any] | None) -> tuple[str, str, str, str]:
    """(base_url, api_key, model, reasoning_effort) —— provider 优先，其次环境变量。"""
    provider = api_provider or {}
    runtime_cfg = provider.get("__codex_runtime__") or {}
    base_url = (provider.get("baseUrl") or os.environ.get("TB_BASE_URL") or "").strip()
    model = (provider.get("model") or os.environ.get("TB_MODEL") or "").strip()
    effort = str(runtime_cfg.get("reasoning_effort") or os.environ.get("TB_REASONING_EFFORT") or "")
    api_key = ""
    for name in ("TB_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            api_key = value.strip()
            break
    missing = [n for n, v in (("base_url", base_url), ("model", model), ("api_key", api_key)) if not v]
    if missing:
        raise RuntimeError(
            f"缺模型连接：{', '.join(missing)}。"
            "设 TB_BASE_URL / TB_API_KEY / TB_MODEL（或让 api_provider 带上 baseUrl/model）。"
        )
    return base_url, api_key, model, effort


def _normalise_trace(stdout_jsonl: str) -> tuple[list[dict[str, Any]], str | None, bool]:
    """把 codex 的 `--json` 事件流规范化成 executionTrace。

    返回 (events, thread_id, completed)。`completed` 只有见到 `turn.completed` 才算 ——
    少了它就是"trace 不完整"，必须让编排器中止这一轮。
    """
    events: list[dict[str, Any]] = []
    thread_id: str | None = None
    completed = False
    for line in (stdout_jsonl or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if thread_id is None:
            for key in ("thread_id", "threadId", "session_id"):
                value = evt.get(key) or (evt.get("msg") or {}).get(key)
                if isinstance(value, str) and value:
                    thread_id = value
                    break
        typ = evt.get("type") or (evt.get("msg") or {}).get("type") or ""
        if typ == "turn.completed":
            completed = True
        item = evt.get("item") or (evt.get("msg") or {}).get("item")
        if isinstance(item, dict):
            events.append({
                "type": str(item.get("type") or typ or "item"),
                "tool": str(item.get("name") or item.get("tool") or item.get("type") or "unknown"),
                "status": str(item.get("status") or "completed"),
            })
    return events, thread_id, completed


async def _run_async(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider, agent_id) -> dict[str, Any]:
    from agentkit import AGENT_BASE_IMAGE_NAME, DockerRuntime, build_harness, resolve_gateway

    base_url, api_key, model, effort = _resolve_connection(api_provider)
    image = os.environ.get("ENVGEN_AGENT_IMAGE") or AGENT_BASE_IMAGE_NAME
    gateway = resolve_gateway(
        base_url=base_url, api_key=api_key, model=model,
        protocol="openai-responses", reasoning_effort=effort,
    )

    Path(sandbox_dir).mkdir(parents=True, exist_ok=True)
    name = f"envgen-{str(agent_id or 'role').replace('/', '-')}-{int(time.time())}"

    # 只读挂载被哈希的两棵子树；work_dir 其余部分（output/ 等）仍可写。
    mounts: list[tuple[str, str]] = [(str(work_dir), WORK_MOUNT), (str(sandbox_dir), SANDBOX_MOUNT)]
    readonly: list[tuple[str, str]] = []
    for sub in READONLY_SUBTREES:
        local = Path(work_dir) / sub
        if local.is_dir():
            readonly.append((str(local), f"{WORK_MOUNT}/{sub}:ro"))
    # DockerRuntime 的 mounts 是 (host, container)；只读用容器侧的 ":ro" 后缀表达
    all_mounts = [(h, c) for h, c in mounts] + [(h, c) for h, c in readonly]

    started = time.monotonic()
    rt = await DockerRuntime.start(
        image=image, name=name, workdir=WORK_MOUNT, network="host",
        mounts=all_mounts,
        env={"CODEX_HOME": f"{SANDBOX_MOUNT}/codex-home"},
    )
    try:
        harness = build_harness(
            "codex", gateway=gateway, workdir=WORK_MOUNT,
            timeout=int(timeout_s) if timeout_s else 3600,
            sandbox_mode="danger-full-access",
        )
        result = await harness.run(rt, prompt)
        log = await rt.read_text(f"/logs/agent/codex.log")
    finally:
        await rt.stop()

    events, thread_id, completed = _normalise_trace(log or "")
    ok = result.status == "ok" and completed
    return {
        "status": "ok" if ok else "error",
        "errorMessage": "" if ok else (
            result.error or
            ("codex 没有产出完整的 trace（未见到 turn.completed）" if result.status == "ok" else result.status)
        ),
        "trace": {
            # 契约硬要求：不完整的 trace 不许变成成功候选
            "collection": {"complete": bool(completed), "threadId": thread_id},
            "executionTrace": events,
        },
        "durationMs": int((time.monotonic() - started) * 1000),
        "agentId": agent_id,
        "runner": "agentkit",
        "model": model,
    }


def run(
    *,
    prompt: str,
    work_dir: str,
    sandbox_dir: str,
    timeout_s: float,
    api_provider: dict[str, Any] | None = None,
    agent_id: str = "role",
    **extra: Any,
) -> dict[str, Any]:
    """实现 `workspace_env.runner.CodexRunner` 契约（同步入口，内部起事件循环）。"""
    try:
        return asyncio.run(_run_async(
            prompt=prompt, work_dir=work_dir, sandbox_dir=sandbox_dir,
            timeout_s=timeout_s, api_provider=api_provider, agent_id=agent_id,
        ))
    except Exception as exc:  # noqa: BLE001
        # 契约：任何失败都要以 status != "ok" 返回，让编排器 fail closed
        return {
            "status": "error",
            "errorMessage": f"{type(exc).__name__}: {exc}",
            "trace": {"collection": {"complete": False, "threadId": None}, "executionTrace": []},
            "durationMs": 0,
            "agentId": agent_id,
            "runner": "agentkit",
        }
