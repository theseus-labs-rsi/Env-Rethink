"""把 agentkit 接到 Workspace-Bench 的 harness 契约上。

`agent_runner.py` 用 `_load_agent_run(name)` 加载 `src/agents/<name>.py` 并调它的 `run()`，
所以把本文件放进 `agents/` 就等于给 Workspace-Bench 加了一个叫 **`agentkit`** 的 harness：

    # 实验 yaml 里
    harness: agentkit

## 它和别的 harness 的区别

别的 harness（`codex` / `claudecode` / …）是**在 harness 容器里直接起子进程**。
这个是**用 agentkit 另起一个一次性容器**跑 agent，把 `work_dir` 挂进去：

  · **每个 case 一个干净容器** —— 任务之间不互相污染；
  · 镜像 = 你给的基础镜像 + `build_overlay()` 叠上的 agent 运行时（`/opt/tb-agent`）；
    所以**任意已有镜像都能带 agent**，不必为它单独打一个 harness 镜像
    （Workspace-Bench 的 Office 镜像里那些 docx/xlsx/libreoffice 依赖照旧可用）；
  · 模型连接走通用三件套，不内置端点。

## 目录怎么给（重要）

**不挂宿主的工作目录。** 容器里给一个**空目录**当 cwd，只把**任务相关的文件**放进去：

  · 宿主那份 `work_dir` 是 runner 的中间态，而且它的**兄弟目录**里就有
    `metadata.json` / `data_manifest`（"哪些文件才算数"的清单）—— 整棵挂进去等于白送题；
  · 跑完把 agent 改过的树**同步回**宿主 `work_dir`，runner 照旧在那儿读交付物；
  · 放行前有一道硬守卫（`_FORBIDDEN_IN_AGENT_VIEW`）：命中清单/真值文件名直接报错，
    不放行、不静默。

## 环境变量

    WS_AGENT_IMAGE       直接用这个镜像（应当已含 agent 运行时）
    WS_AGENT_BASE_IMAGE  没有 WS_AGENT_IMAGE 时：把这个镜像 + agent 层现叠一个
    WS_AGENT_KIND        claude_code | codex（默认按 api_provider.provider_type 推）
    TB_BASE_URL / TB_API_KEY / TB_MODEL   模型连接（api_provider 里有的以它为准）

## 契约

    run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider, agent_id=None) -> dict
    返回 {"status", "paths", "errorMessage", "trace", "metrics", "durationMs"}
    与 `src/agents/codex.py` 同形，`agent_runner.py` 不用改。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from pathlib import Path
from typing import Any, Dict, Optional

Json = Any

# 本文件在 <repo>/workspace_eval/src/agents/ 下 → 仓库根是 parents[3]
_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

WORK_MOUNT = "/work"

#: **绝不允许出现在 agent 可见目录里的东西。**
#:
#: `metadata.json` 里的 `data_manifest` 是"哪些文件才算数"的清单 —— agent 看到它，
#: 题就不用做了（直接照单取件）。`input_gt` 是输入侧真值，同理。
#: 原设计把这个清单放在 `case_dir/metadata.json`（`work_dir` 的**兄弟**），靠 harness
#: 的路径围栏把 agent 关在 cwd 里；本适配器不依赖那层围栏，所以在这里显式拒绝。
_FORBIDDEN_IN_AGENT_VIEW = {
    "metadata.json",
    "data_manifest.json",
    "input_gt",
    "ground_truth.json",
    "rubrics.json",
}


def _assert_no_manifest(paths: list[str]) -> None:
    """放行前逐条检查：任务清单 / 真值绝不能进 agent 可见目录。

    宁可在这里红，也不要让一份清单悄悄进容器 —— 那种题跑出来的分数是假的。
    """
    bad = [p for p in paths if Path(p).name in _FORBIDDEN_IN_AGENT_VIEW]
    if bad:
        raise RuntimeError(
            "拒绝把任务清单/真值放进 agent 可见目录（会让题目白送）：\n  "
            + "\n  ".join(sorted(bad)[:10])
            + "\n如果确实需要 agent 看到某个同名文件，先确认它不含 data_manifest。"
        )


def _pick_kind(api_provider: Dict[str, Json], override: str = "") -> str:
    """用哪个 agent。显式指定 > provider_type 推断。"""
    if override:
        return override
    ptype = str((api_provider or {}).get("provider_type") or "").strip().lower()
    if ptype == "anthropic":
        return "claude_code"
    return "codex"


def _connection(api_provider: Dict[str, Json], kind: str) -> tuple[str, str, str, str]:
    """(base_url, api_key, model, reasoning_effort)。provider 优先，其次环境变量。"""
    provider = api_provider or {}

    def pick(*names: str) -> str:
        for name in names:
            value = provider.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    base_url = pick("baseUrl", "base_url") or os.environ.get("TB_BASE_URL", "").strip()
    model = pick("model") or os.environ.get("TB_MODEL", "").strip()
    effort = pick("reasoning_effort", "reasoningEffort") or os.environ.get("TB_REASONING_EFFORT", "").strip()
    api_key = pick("apiKey", "api_key", "token")
    if not api_key:
        for name in ("TB_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"):
            if os.environ.get(name):
                api_key = os.environ[name].strip()
                break
    missing = [n for n, v in (("base_url", base_url), ("model", model), ("api_key", api_key)) if not v]
    if missing:
        raise ValueError(
            f"缺模型连接：{', '.join(missing)}。"
            "让 api_provider 带上 baseUrl/model/apiKey，或设 TB_BASE_URL/TB_API_KEY/TB_MODEL。"
        )
    return base_url, api_key, model, effort


async def _sync_back(runtime, *, file_item, from_container: str, to_host: str) -> int:
    """把容器里 agent 产出的文件树同步回宿主目录。

    用「先查清单再逐个取」而不是整目录 tar：容器里可能有 agent 生成的临时大文件，
    而且我们只关心文件（目录结构由路径隐含）。
    """
    listing = await runtime.run_command(
        f"find {from_container} -type f -printf '%p\\n' 2>/dev/null | head -20000", timeout=300
    )
    paths = [p for p in (listing.stdout or "").splitlines() if p.strip().startswith(from_container)]
    if not paths:
        return 0
    resp = await runtime.download_file([file_item(path=p, encoding="base64") for p in paths])
    import base64 as _b64

    base = Path(to_host)
    written = 0
    for item in resp.files:
        rel = Path(item.path).relative_to(from_container)
        dst = base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(_b64.b64decode(item.content or ""))
        written += 1
    return written


async def _run_async(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider, agent_id) -> Dict[str, Json]:
    # 注意：本文件自己就叫 agentkit.py，而 runner 按文件名加载它 —— 所以**不要**在这里
    # 做模块级 `import agentkit`（会解析到自己、循环导入）。函数内 import 是安全的：
    # 加载器用的是 spec_from_file_location，本模块注册名不是 `agentkit`。
    from agentkit import (
        AGENT_BASE_IMAGE_NAME, DockerRuntime, FileItem, agent_protocol, build_harness,
        build_overlay, read_local_directory_to_file_items, resolve_gateway,
    )

    kind = _pick_kind(api_provider, os.environ.get("WS_AGENT_KIND", ""))
    base_url, api_key, model, effort = _connection(api_provider, kind)
    gateway = resolve_gateway(
        base_url=base_url, api_key=api_key, model=model,
        protocol=agent_protocol(kind), reasoning_effort=effort,
    )

    # 镜像：优先显式给的；否则把 agent 层叠到 WS_AGENT_BASE_IMAGE 上
    image = os.environ.get("WS_AGENT_IMAGE", "").strip()
    if not image:
        base_image = os.environ.get("WS_AGENT_BASE_IMAGE", "").strip()
        image = build_overlay(base_image, name="wseval") if base_image else AGENT_BASE_IMAGE_NAME

    raw_dir = Path(sandbox_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    name = f"wseval-{str(agent_id or 'case').replace('/', '-')}-{int(time.time())}"

    rt = await DockerRuntime.start(
        image=image, name=name, workdir=WORK_MOUNT, network="host",
    )
    try:
        # **不挂宿主的工作目录**：容器里给一个空目录，只把任务相关的文件放进去。
        # 宿主那份 work_dir 是 runner 的中间态（角色工作区副本 + 任务输入），
        # 而且它的兄弟目录里就有 metadata.json/data_manifest —— 整棵挂进去，
        # 等于把"哪些文件才算数"的清单一起交给 agent。
        await rt.mkdirs(WORK_MOUNT)
        items = read_local_directory_to_file_items(
            local_dir=work_dir, container_base_path=WORK_MOUNT
        )
        _assert_no_manifest([it.path for it in items])
        if items:
            await rt.upload_file(items, overwrite=True)

        harness = build_harness(
            kind, gateway=gateway, workdir=WORK_MOUNT,
            timeout=int(timeout_s) if timeout_s else 3600,
            sandbox_mode="danger-full-access",
        )
        result = await harness.run(rt, prompt)
        stdout_text = await rt.read_text(f"/logs/agent/{kind}.log") or ""

        # 把 agent 改过的树捞回宿主：runner 之后就在 work_dir 里读交付物
        await _sync_back(rt, file_item=FileItem, from_container=WORK_MOUNT, to_host=work_dir)
    finally:
        await rt.stop()

    duration_ms = int((time.monotonic() - started) * 1000)
    (raw_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")

    status = {"ok": "ok", "timeout": "timeout"}.get(result.status, "error")
    extra = result.extra or {}
    return {
        "status": status,
        "paths": [],
        "errorMessage": None if status == "ok" else (result.error or result.status),
        "trace": {
            "runner": "agentkit",
            "agentId": agent_id,
            "rawDir": str(raw_dir),
            "lastText": result.last_message,
            "llm": {"provider": kind, "baseUrl": base_url, "model": model},
            "usageTotal": extra.get("usage") or {},
            "executionTrace": [],
        },
        "metrics": {
            "turns": extra.get("num_turns") or extra.get("events"),
            "promptTokens": None,
            "completionTokens": None,
            "totalTokens": None,
        },
        "durationMs": duration_ms,
    }


def run(
    *,
    prompt: str,
    work_dir: str,
    sandbox_dir: str,
    timeout_s: float,
    api_provider: Dict[str, Json],
    agent_id: Optional[str] = None,
    **extra: Json,
) -> Dict[str, Json]:
    """Workspace-Bench harness 契约（同步入口，内部起事件循环）。"""
    try:
        return asyncio.run(_run_async(
            prompt=prompt, work_dir=work_dir, sandbox_dir=sandbox_dir,
            timeout_s=timeout_s, api_provider=api_provider, agent_id=agent_id,
        ))
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "paths": [],
            "errorMessage": f"{type(exc).__name__}: {exc}",
            "trace": {"runner": "agentkit", "agentId": agent_id, "rawDir": sandbox_dir,
                      "lastText": "", "executionTrace": []},
            "metrics": {"turns": None, "promptTokens": None, "completionTokens": None, "totalTokens": None},
            "durationMs": 0,
        }
