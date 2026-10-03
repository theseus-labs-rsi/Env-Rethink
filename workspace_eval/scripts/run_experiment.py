#!/usr/bin/env python3
"""Create and run reproducible Workspace-Bench experiments from one YAML file.

With no arguments, this script writes a commented ``experiment.yaml`` template.
Pass that file back with ``--config`` to stage a fresh local runtime under
``/tmp``, run every case in a strict allowlist container, judge the outputs,
and persist compact terminal artifacts under ``evaluation/experiments``.

The task list intentionally preserves duplicates.  For example,
``task_ids: [124, 124, 129]`` creates two independent Task 124 cases that may
run concurrently.  A mapping form such as ``{id: 124, repeat: 3}`` is also
supported.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml


Json = Any
SCRIPT_PATH = Path(__file__).resolve()
EVAL_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = EVAL_ROOT.parent

if str(EVAL_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT / "src"))
import runtime_backends  # noqa: E402  —— 运行后端注册表（内置 local，其余走插件）
from judge_results import is_valid_judge_result  # noqa: E402
EXPECTED_IMAGE = "workspace-bench:local"
DEFAULT_IMAGE_ID = (
    "sha256:b32758de8da63061a4db4ecd7f31669797992d9552e757b99c498ff0a36a6046"
)
ROLE_WORKSPACE_NAMES = {
    "行政/后勤人员": "houqin_raw",
    "研究人员": "research_raw",
    "运营人员": "yunying_raw",
    "开发人员": "kaifa_raw",
    "产品人员": "chanpin_raw",
    # DataSeed 生产任务：空角色基线（workspace = per-case 整树物化）
    "dataseed": "dataseed_raw",
}
# 允许的运行条件（决定 workspace 里有什么、需要哪些镜像）：
#   clean      — 空角色工作区 + 仅 input_role==standard 的任务输入；
#   noise      — 角色 noisy workspace 快照 + manifest 全量输入（需角色层/镜像）；
#   curated    — 角色 noisy workspace 快照 + 选集 manifest（selection-only，
#                任务目录须带 curation.json）；
#   task_files — 只跑任务文件：空角色工作区 + manifest 原样全量输入。
#                workspace 只含任务自带文件（与 envgen 生成时的任务文件池
#                同款视图），不物化任何角色工作区、不需要角色镜像。
CONDITIONS = ("clean", "noise", "curated", "task_files")
# 需要角色 noisy workspace 作为基座的条件（其余条件用空角色种子 / common 镜像）。
ROLE_WORKSPACE_CONDITIONS = ("noise", "curated")
REASONING_EFFORTS = {
    "no_think",
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}
RUNTIME_DIRS = (
    "src",
    "baselines",
    "bin",
    "node_modules",
    "vendor",
    "docker",
    "runs",
    "scripts",
)
RUNTIME_FILES = ("pyproject.toml", "uv.lock", ".python-version")
SUMMARY_FIELDS = (
    "case_id",
    "task_id",
    "repeat",
    "status",
    "passed",
    "failed",
    "total",
    "pass_rate",
    "pass_rate_pct",
    "duration_seconds",
    "return_code",
)

DEFAULT_TEMPLATE = """\
# Workspace-Bench 通用严格隔离实验配置
version: 1
name: example-hard-v2

# 相对路径按仓库根目录解析，也可以填写绝对路径。
task_dir: evaluation/tasks_hard_v2

# 重复的 task id 会成为独立 case，可并发运行。
# 也支持映射写法：{id: 124, repeat: 3}
task_ids:
  - 94
  - 124
  - 129

# 可选：把列表中的每个条目重复多少次，默认 1。
# 若单个条目使用 {id: 124, repeat: 3}，则该条目的 repeat 优先。
repeat: 1

# case 级并发数；重复运行同一 task 也占一个并发槽位。
parallelism: 3

# noise: 使用角色 noisy workspace；clean: 空 workspace + 仅 standard 输入；
# curated: 角色 noisy workspace 基底 + curate_workspace.py 产出的选集 manifest
# （task_dir 必须是 preprocessed 根，每个任务目录带 curation.json）；
# task_files: 只跑任务文件——空 workspace + manifest 原样全量输入，不物化角色
# 工作区（envgen 任务文件池同款视图）。
# 四种模式使用相同 task、Agent、Judge 和严格白名单容器流程。
condition: noise

agent:
  model: gpt-5.6-sol
  # 内置 local runner 支持 Codex 或 ClaudeCode。
  harness: Codex
  reasoning_effort: max
  timeout_seconds: 7200
  max_output_tokens: 32768
  attempts: 3
  api_retry_max_attempts: 6
  api_retry_total_timeout_seconds: 600
  api_request_timeout_seconds: 600
  upstream_timeout_seconds: 600
  upstream_stream: false
  # 以下字段通常保持 null，只有使用自定义模型/网关时才覆盖。
  model_id: null
  display_name: null
  base_url: null
  auth_type: null
  provider_type: null
  wire_api: null
  parallel_tool_calls: null
  # 自定义 bearer 模型可指定环境变量名，例如 OPENAI_API_KEY；不要填写明文密钥。
  api_key_env: null

judge:
  model: gpt-5.6-sol
  reasoning_effort: medium
  timeout_seconds: 1800
  max_output_tokens: 32768
  attempts: 3
  max_retries: 2
  max_string_length: 8000
  max_trace_items: 60
  max_output_files: 10
  model_id: null
  display_name: null
  base_url: null
  auth_type: null
  provider_type: null
  wire_api: null
  parallel_tool_calls: null
  api_key_env: null

runtime:
  # 每次运行会自动追加唯一 UTC 时间戳，不复用历史输出。
  local_root: /tmp
  persistent_root: evaluation/experiments
  env_file: evaluation/.env
  keep_local_runtime: true
  minimum_free_gb: 10

  # 可指定已经冻结并校验的本地 snapshot 根目录；其下应包含
  # kaifa_raw、research_raw、yunying_raw、houqin_raw 等角色目录。
  # 为 null 时，从下面 role_sources 创建新的本地 snapshot。
  workspace_snapshot_root: null
  role_sources:
    "行政/后勤人员": evaluation/filesys/houqin_raw
    "研究人员": evaluation/filesys/research_raw
    "运营人员": evaluation/filesys/yunying_raw
    "开发人员": evaluation/filesys/kaifa_raw
    "产品人员": evaluation/filesys/chanpin_raw

  image: workspace-bench:local
  expected_image_id: sha256:b32758de8da63061a4db4ecd7f31669797992d9552e757b99c498ff0a36a6046
  resources:
    cpus: "2"
    memory_mb: 8192
    pids: 512
    storage_mb: 20480
"""

MODEL_PRESETS: dict[str, dict[str, Json]] = {
    "gpt-5.6-sol": {
        "display_name": "GPT-5.6-Sol",
        "model_id": "${GPT56SOL_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        "provider_type": "openai",
        "auth_type": "cached_app_credentials",
        "wire_api": "responses",
        "parallel_tool_calls": True,
    },
    "gpt-5.6-luna": {
        "display_name": "GPT-5.6-Luna",
        "model_id": "${GPT56LUNA_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        "provider_type": "openai",
        "auth_type": "cached_app_credentials",
        "wire_api": "responses",
        "parallel_tool_calls": False,
    },
    "gpt-5.6-terra": {
        "display_name": "GPT-5.6-Terra",
        "model_id": "${GPT56TERRA_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        "provider_type": "openai",
        "auth_type": "cached_app_credentials",
        "wire_api": "responses",
        "parallel_tool_calls": False,
    },
    "qwen3.8-max": {
        "display_name": "Qwen3.8-Max",
        "model_id": "${QWEN38MAX_MODEL:-qwen3.8-max}",
        "base_url": "${QWEN38MAX_BASE_URL:-}",
        "provider_type": "openai",
        "auth_type": "app_credentials",
        "auth_provider": "ali",
        "wire_api": "responses",
        "parallel_tool_calls": False,
    },
    # DeepSeek V4 models ride the 网关standard gateway; the provider consumes
    # DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY directly (Bearer ${APP_ID}:${APP_KEY}).
    "deepseek-v4-flash": {
        "display_name": "DeepSeek-V4-Flash",
        "model_id": "${DEEPSEEKV4FLASH_MODEL:-deepseek/deepseek-v4-flash}",
        "base_url": "${DEEPSEEK_BASE_URL:-}",
        "provider_type": "deepseek",
        "auth_type": "bearer",
        "wire_api": "chat_completions",
        "parallel_tool_calls": False,
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "deepseek-v4-pro": {
        "display_name": "DeepSeek-V4-Pro",
        "model_id": "${DEEPSEEKV4PRO_MODEL:-deepseek/deepseek-v4-pro}",
        "base_url": "${DEEPSEEK_BASE_URL:-}",
        "provider_type": "deepseek",
        "auth_type": "bearer",
        "wire_api": "chat_completions",
        "parallel_tool_calls": False,
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "gemini-3.7-flash": {
        "display_name": "Gemini-3.7-Flash",
        "model_id": "${GEMINI37FLASH_MODEL:-google/gemini-3.7-flash}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        # The the gateway exposes native Anthropic Messages for Gemini with
        # working tool-result round-trips (the /responses endpoint drops
        # function_call_output for Gemini, which loops the judge forever).
        # anthropic_app_credentials routes Claude Code direct via a probe.
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
    "glm-5.3": {
        "display_name": "GLM-5.3",
        "model_id": "${GLM53_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        # Live-tested 2026-08-29: the gateway's native Anthropic Messages
        # endpoint handles Claude Code's full request shape (tools, multi-round
        # tool_result, streaming, beta headers) without a bridge.
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
    "kimi-k3": {
        "display_name": "Kimi-K3",
        "model_id": "${KIMIK3_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        # Live-tested 2026-08-29 on the standard-protocol Anthropic Messages
        # endpoint (thinking block + tool round-trips verified).
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
    "grok-4.6": {
        "display_name": "Grok-4.6",
        "model_id": "${GROK46_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        # Live-tested 2026-08-29: Anthropic Messages (thinking + tool
        # round-trips), Chat Completions (api_xai_grok-4.6, reasoning_content),
        # and Responses pass-through (gateway :8080, model grok-4.6) all
        # work; reasoning cannot be disabled (low/medium/high/xhigh).
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
    "muse-spark-1.2": {
        "display_name": "Muse-Spark-1.2",
        "model_id": "${MUSESPARK12_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        # Live-tested 2026-08-29: Anthropic Messages works (tool round-trips
        # verified without tool_choice — the gateway rejects tool_choice
        # entirely, and Claude Code never sends it).  max_tokens must be
        # large: reasoning tokens share the budget and small values yield
        # empty content with stop_reason=max_tokens.  Responses pass-through
        # (provider=meta) also verified; platform rate limit 30 RPM.
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
    "qwen3.8-max-cc": {
        "display_name": "Qwen3.8-Max",
        "model_id": "${QWEN38MAX_CC_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        # Live-tested 2026-08-30: standard-protocol Anthropic Messages works
        # end-to-end through Claude Code (thinking blocks, tool round-trips,
        # harness probe compatible).  Same upstream as the Codex route
        # (ali/DashScope qwen3.8-max) via the llm-api anthropic bridge.
        # DashScope enforces a concurrent-request limit — keep parallelism
        # modest or expect 429 "Too many concurrent requests".
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
    # qwen3.8-flash 与 -max-cc 同源（ali/DashScope），走**同一条** AI Hub
    # Anthropic Messages 标准协议。此前 flash 用的是 chat_completions +
    # cached_app_credentials，会被推去本地 bridge、模型被 CLI 当成「自定义模型」，
    # 上下文窗口回落到默认 200K —— 大工作区（task108 的 88 文件）因此撑爆
    # （Autocompact is thrashing）。对齐成 anthropic_messages 后 CLI 拿到原生
    # Anthropic 协议，行为与 -max-cc 一致。
    "qwen3.8-flash": {
        "display_name": "Qwen3.8-Flash",
        "model_id": "${QWEN38FLASH_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
    "claude-opus-5": {
        "display_name": "Claude-Opus-5",
        "model_id": "${OPUS5_MODEL:-}",
        "base_url": "${WS_MODEL_BASE_URL:-}",
        # Live-tested 2026-08-30: standard-protocol Anthropic Messages via the
        # aws_third (Bedrock) marker works end-to-end (thinking blocks, tool
        # round-trips, output_config.effort tiers, full cache usage fields —
        # real Anthropic backend, unlike the third-party gateways).  The
        # anthropic (原厂) provider route returns PlatformNoAvailableAccount
        # on this gateway; aws_third is the working route.
        "provider_type": "openai",
        "auth_type": "anthropic_app_credentials",
        "wire_api": "anthropic_messages",
        "parallel_tool_calls": False,
    },
}
MODEL_ALIASES = {
    "sol": "gpt-5.6-sol",
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "qwen38max": "qwen3.8-max",
    "dsv4flash": "deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "dsv4pro": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "gemini37flash": "gemini-3.7-flash",
    "gemini-3.7-flash": "gemini-3.7-flash",
    "glm53": "glm-5.3",
    "glm-5.3": "glm-5.3",
    "kimik3": "kimi-k3",
    "kimi-k3": "kimi-k3",
    "grok46": "grok-4.6",
    "grok-4.6": "grok-4.6",
    "musespark12": "muse-spark-1.2",
    "muse-spark-1.2": "muse-spark-1.2",
    "qwen38maxcc": "qwen3.8-max-cc",
    "qwen3.8-max-cc": "qwen3.8-max-cc",
    "qwen38flash": "qwen3.8-flash",
    "qwen3.8-flash": "qwen3.8-flash",
    "opus5": "claude-opus-5",
    "claude-opus-5": "claude-opus-5",
}


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    task_id: str
    repeat: int


@dataclass
class PreparedRun:
    config: dict[str, Json]
    config_path: Path
    run_id: str
    runtime_root: Path
    persistent_root: Path
    runtime_eval_root: Path
    runtime_task_root: Path
    suite_root: Path
    env_file: Path | None
    cases: list[CaseSpec]
    judge_config_path: Path
    image: str
    image_id: str


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_slug(value: object, *, limit: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip()).strip("-")
    return (slug or "experiment")[:limit]


def _resolve_path(value: object, *, base: Path = REPO_ROOT) -> Path:
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _read_yaml(path: Path) -> dict[str, Json]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid experiment YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"experiment YAML must contain a mapping: {path}")
    return value


def _write_yaml(path: Path, value: dict[str, Json]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_json(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_dotenv(path: Path | None) -> None:
    if path is None or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _normalize_cases(raw_tasks: object, global_repeat: object = 1) -> list[CaseSpec]:
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise SystemExit("task_ids must be a non-empty list")
    try:
        default_repeat = int(global_repeat)
    except (TypeError, ValueError) as exc:
        raise SystemExit("repeat must be an integer") from exc
    if default_repeat < 1:
        raise SystemExit("repeat must be at least 1")

    expanded: list[str] = []
    for index, item in enumerate(raw_tasks):
        if isinstance(item, dict):
            raw_id = item.get("id")
            raw_repeat = item.get("repeat", default_repeat)
        else:
            raw_id = item
            raw_repeat = default_repeat
        task_id = str(raw_id).strip()
        if not task_id or not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
            raise SystemExit(f"invalid task id at task_ids[{index}]: {raw_id!r}")
        try:
            repeat = int(raw_repeat)
        except (TypeError, ValueError) as exc:
            raise SystemExit(
                f"invalid repeat for task {task_id}: {raw_repeat!r}"
            ) from exc
        if repeat < 1:
            raise SystemExit(f"repeat for task {task_id} must be at least 1")
        expanded.extend([task_id] * repeat)

    totals = {task_id: expanded.count(task_id) for task_id in set(expanded)}
    seen: dict[str, int] = {}
    cases: list[CaseSpec] = []
    for task_id in expanded:
        seen[task_id] = seen.get(task_id, 0) + 1
        repeat = seen[task_id]
        case_id = (
            f"task{task_id}"
            if totals[task_id] == 1
            else f"task{task_id}-r{repeat:02d}"
        )
        cases.append(CaseSpec(case_id=case_id, task_id=task_id, repeat=repeat))
    return cases


def _model_config(raw: object, *, default_effort: str) -> dict[str, Json]:
    if not isinstance(raw, dict):
        raise SystemExit("agent and judge must be mappings")
    alias = str(raw.get("model") or "").strip().lower()
    if not alias:
        raise SystemExit("agent.model and judge.model are required")
    alias = MODEL_ALIASES.get(alias, alias)
    preset = dict(MODEL_PRESETS.get(alias, {}))
    preset.setdefault("display_name", str(raw.get("model") or "").strip())
    preset.setdefault("model_id", str(raw.get("model") or "").strip())
    preset.setdefault("base_url", "${OPENAI_BASE_URL}")
    preset.setdefault("provider_type", "openai")
    preset.setdefault("auth_type", "bearer")
    preset.setdefault("wire_api", "responses")
    preset.setdefault("parallel_tool_calls", False)

    aliases = {
        "model_id": "model_id",
        "display_name": "display_name",
        "base_url": "base_url",
        "provider_type": "provider_type",
        "auth_type": "auth_type",
        "wire_api": "wire_api",
        "parallel_tool_calls": "parallel_tool_calls",
        "auth_provider": "auth_provider",
    }
    for source_key, target_key in aliases.items():
        value = raw.get(source_key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            preset[target_key] = value

    effort = str(raw.get("reasoning_effort") or default_effort).strip().lower()
    if effort not in REASONING_EFFORTS:
        raise SystemExit(
            f"unsupported reasoning effort {effort!r}; "
            f"choose one of {', '.join(sorted(REASONING_EFFORTS))}"
        )
    preset["alias"] = alias
    preset["reasoning_effort"] = effort
    preset["max_output_tokens"] = max(
        1, int(raw.get("max_output_tokens") or 32768)
    )
    # 模型真实上下文窗口（token）。自建/第三方端点必须显式声明：这些网关会在流式
    # 响应里丢掉 cache usage 字段，Claude Code 的上下文核算只看到未缓存的那一段，
    # 于是 autocompact 永不触发、对话一直涨到模型的真实上限才 500/400。
    # 0/缺省 = 不声明（走 claudecode.py 里的按模型名兜底表）。
    preset["claude_context_tokens"] = max(
        0, int(raw.get("claude_context_tokens") or 0)
    )
    # ClaudeCode harness 的轮次上限（model 侧默认 200）。工作区很大时 agent
    # 会探索到上限而报 "Reached maximum number of turns"；需要时按实验抬到更大。
    preset["claude_max_turns"] = max(0, int(raw.get("claude_max_turns") or 0))
    preset["timeout_seconds"] = max(
        60, int(raw.get("timeout_seconds") or 7200)
    )
    preset["attempts"] = max(1, int(raw.get("attempts") or 3))
    preset["api_retry_max_attempts"] = max(
        1, min(int(raw.get("api_retry_max_attempts") or 6), 20)
    )
    preset["api_retry_total_timeout_seconds"] = max(
        1.0, float(raw.get("api_retry_total_timeout_seconds") or 600.0)
    )
    preset["api_request_timeout_seconds"] = max(
        1.0, float(raw.get("api_request_timeout_seconds") or 600.0)
    )
    preset["upstream_timeout_seconds"] = max(
        1, int(raw.get("upstream_timeout_seconds") or 600)
    )
    preset["upstream_stream"] = bool(raw.get("upstream_stream", False))
    preset["auth_timeout_seconds"] = max(
        1,
        int(
            raw.get("auth_timeout_seconds")
            or preset["upstream_timeout_seconds"]
        ),
    )
    api_key_env = str(
        raw.get("api_key_env") or preset.get("api_key_env") or "OPENAI_API_KEY"
    ).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise SystemExit(f"invalid api_key_env: {api_key_env!r}")
    preset["api_key_env"] = api_key_env
    if raw.get("api_key") not in (None, ""):
        raise SystemExit(
            "do not put a literal api_key in experiment YAML; "
            "use api_key_env and runtime.env_file"
        )
    return preset


def _agent_harness(config: dict[str, Json]) -> str:
    agent = config.get("agent")
    raw = agent.get("harness") if isinstance(agent, dict) else None
    raw = raw or config.get("harness") or "Codex"
    aliases = {
        "codex": "Codex",
        "claudecode": "ClaudeCode",
        "claude-code": "ClaudeCode",
        "claude_code": "ClaudeCode",
    }
    selected = aliases.get(str(raw).strip().lower())
    if selected is None:
        raise SystemExit(
            f"unsupported local agent harness: {raw!r}; choose Codex or ClaudeCode"
        )
    return selected


def _api_provider(model: dict[str, Json]) -> dict[str, Json]:
    config: dict[str, Json] = {
        "provider_type": model["provider_type"],
        "baseUrl": model["base_url"],
        "model": model["model_id"],
        "authType": model["auth_type"],
        "wireApi": model["wire_api"],
        "reasoningEffort": model["reasoning_effort"],
        "maxOutputTokens": model["max_output_tokens"],
        "parallelToolCalls": bool(model["parallel_tool_calls"]),
        "apiRetryMaxAttempts": model["api_retry_max_attempts"],
        "apiRetryInitialDelaySec": 1.0,
        "apiRetryMaxDelaySec": 30.0,
        "apiRetryTotalTimeoutSec": model[
            "api_retry_total_timeout_seconds"
        ],
        "apiRequestTimeoutSec": model["api_request_timeout_seconds"],
        "upstreamTimeoutSec": model["upstream_timeout_seconds"],
        "upstreamStream": model["upstream_stream"],
        "codexSandboxMode": "danger-full-access",
        # 声明真实上下文窗口 → claudecode.py 会据此设 CLAUDE_CODE_MAX_CONTEXT_TOKENS，
        # 让 CLI 的 autocompact 阈值按真实窗口计算（否则永不触发）。
        "claudeContextTokens": (
            str(model["claude_context_tokens"])
            if model.get("claude_context_tokens")
            else ""
        ),
        "claudeMaxTurns": (
            int(model["claude_max_turns"]) if model.get("claude_max_turns") else 0
        ),
    }
    auth_type = str(model["auth_type"])
    if auth_type in {
        "cached_app_credentials",
        "app_credentials",
        "anthropic_app_credentials",
    }:
        config.update(
            {
                "appId": "${APP_ID}",
                "appKey": "${APP_KEY}",
                "authTimeoutSec": model["auth_timeout_seconds"],
            }
        )
        if auth_type == "app_credentials":
            config["authProvider"] = model.get("auth_provider") or "ali"
            config["authModel"] = model["model_id"]
    else:
        config["apiKey"] = f"${{{model['api_key_env']}}}"
    return config


def _judge_yaml(model: dict[str, Json], raw: dict[str, Json]) -> dict[str, Json]:
    config = _api_provider(model)
    config.pop("codexSandboxMode", None)
    config["model_name"] = model["display_name"]
    config["judgeTimeoutSec"] = model["timeout_seconds"]
    return config


def _dataset_name(task_root: Path, config: dict[str, Json]) -> str:
    explicit = str(config.get("dataset") or "").strip()
    if explicit:
        return explicit
    mapping = {
        "tasks_lite": "lite",
        "tasks": "full",
        "tasks_new": "tasks-new",
        "tasks_hard": "tasks-hard",
        "tasks_hard_v2": "tasks-hard-v2",
        "tasks_hard_wyk": "tasks-hard-wyk",
        "tasks_hard_all": "tasks-hard-all",
    }
    return mapping.get(task_root.name, "custom")


def _strict_runner_dataset(task_root: Path, config: dict[str, Json]) -> str:
    """Return the dataset flag used for strict-runner special validation."""
    dataset = _dataset_name(task_root, config)
    # run_strict_task_config only gives special meaning to WYK datasets.
    # Other custom task roots can safely use the generic value.
    return dataset if dataset in {"tasks-hard-wyk", "tasks-hard-all"} else "custom"


def _validate_task_metadata(
    task_root: Path,
    cases: Iterable[CaseSpec],
) -> tuple[dict[str, dict[str, Json]], set[str]]:
    metadata_by_id: dict[str, dict[str, Json]] = {}
    roles: set[str] = set()
    for task_id in dict.fromkeys(case.task_id for case in cases):
        task_dir = task_root / task_id
        metadata_path = task_dir / "metadata.json"
        if not metadata_path.is_file():
            raise SystemExit(f"task metadata not found: {metadata_path}")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"invalid metadata {metadata_path}: {exc}") from exc
        if not isinstance(metadata, dict):
            raise SystemExit(f"metadata must be an object: {metadata_path}")
        rubrics = metadata.get("rubrics")
        rubric_types = metadata.get("rubric_types")
        if not isinstance(rubrics, list) or not rubrics:
            raise SystemExit(f"task {task_id} has no rubrics")
        if not isinstance(rubric_types, list) or len(rubrics) != len(rubric_types):
            raise SystemExit(
                f"task {task_id}: len(rubrics) != len(rubric_types)"
            )
        manifest = metadata.get("data_manifest")
        if not isinstance(manifest, list):
            raise SystemExit(f"task {task_id}: data_manifest must be a list")
        targets: set[str] = set()
        for index, item in enumerate(manifest):
            if not isinstance(item, dict):
                raise SystemExit(
                    f"task {task_id}: data_manifest[{index}] must be an object"
                )
            relpath = str(item.get("stored_relpath") or "")
            target = str(item.get("target_path") or "")
            if not relpath or not (task_dir / relpath).is_file():
                raise SystemExit(
                    f"task {task_id}: missing manifest source {relpath!r}"
                )
            if not target or target in targets or Path(target).is_absolute():
                raise SystemExit(
                    f"task {task_id}: invalid/duplicate target_path {target!r}"
                )
            targets.add(target)
        role = str(metadata.get("file_system") or "").strip()
        if not role:
            raise SystemExit(f"task {task_id}: missing file_system role")
        roles.add(role)
        metadata_by_id[task_id] = metadata
    return metadata_by_id, roles


def _assert_safe_symlinks(root: Path) -> None:
    root_resolved = root.resolve()
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = os.readlink(path)
        if os.path.isabs(target):
            raise SystemExit(f"absolute symlink rejected: {path} -> {target}")
        try:
            (path.parent / target).resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise SystemExit(f"escaping symlink rejected: {path} -> {target}") from exc


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    require_reflink: bool = False,
) -> None:
    if not source.is_dir():
        raise SystemExit(f"source directory not found: {source}")
    _assert_safe_symlinks(source)
    destination.mkdir(parents=True, exist_ok=False)
    result = subprocess.run(
        [
            "cp",
            "-a",
            "--reflink=always" if require_reflink else "--reflink=auto",
            "--no-preserve=ownership",
            f"{source}/.",
            str(destination),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        raise SystemExit(
            result.stderr
            or result.stdout
            or f"failed to stage {source} to {destination}"
        )
    _assert_safe_symlinks(destination)


def _tree_summary(root: Path) -> dict[str, Json]:
    files = 0
    total = 0
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(f"L\0{relative}\0{os.readlink(path)}\n".encode())
        elif path.is_file():
            size = path.stat().st_size
            files += 1
            total += size
            digest.update(
                (
                    f"F\0{relative}\0{size}\0"
                    f"{_file_sha256(path)}\n"
                ).encode()
            )
        elif path.is_dir():
            digest.update(f"D\0{relative}\n".encode())
    return {
        "files": files,
        "bytes": total,
        "tree_sha256": digest.hexdigest(),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_free_space(root: Path, minimum_gb: float) -> None:
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    minimum = int(float(minimum_gb) * (1024**3))
    if free < minimum:
        raise SystemExit(
            f"insufficient free space under {root}: "
            f"{free / (1024**3):.1f} GiB available, "
            f"{minimum_gb} GiB required"
        )


def _copy_runtime_code(runtime_root: Path) -> Path:
    runtime_eval = runtime_root / "evaluation"
    runtime_eval.mkdir(parents=True)
    # Optional local cache (WORKSPACE_BENCH_RUNTIME_CACHE=<prev runtime
    # evaluation dir>): the ceph source tree is slow to copy (8k+ small
    # node_modules files), while /tmp is a local xfs — reflink from a
    # previous runtime on the same filesystem makes repeats near-instant.
    cache_root = os.environ.get("WORKSPACE_BENCH_RUNTIME_CACHE")
    cache_root = Path(cache_root) if cache_root and Path(cache_root).is_dir() else None
    for name in RUNTIME_DIRS:
        source = EVAL_ROOT / name
        if not source.exists():
            continue
        cached = (cache_root / name) if cache_root else None
        if cached is not None and cached.is_dir():
            _copy_tree(cached, runtime_eval / name, require_reflink=True)
            continue
        _copy_tree(source, runtime_eval / name)
    for name in RUNTIME_FILES:
        source = EVAL_ROOT / name
        if source.is_file():
            shutil.copy2(source, runtime_eval / name)
    email_skill = REPO_ROOT / "skills" / "email"
    if email_skill.is_dir():
        (runtime_root / "skills").mkdir()
        _copy_tree(email_skill, runtime_root / "skills" / "email")
    return runtime_eval


def _safe_model_manifest(model: dict[str, Json]) -> dict[str, Json]:
    return {
        key: value
        for key, value in model.items()
        if key not in {"api_key", "appId", "appKey"}
    }


def _source_input_digest(
    source_root: Path,
    metadata_by_id: dict[str, dict[str, Json]],
) -> str:
    digest = hashlib.sha256()
    for task_id in sorted(metadata_by_id):
        metadata = metadata_by_id[task_id]
        manifest = metadata.get("data_manifest")
        for item in manifest if isinstance(manifest, list) else []:
            if not isinstance(item, dict):
                continue
            relpath = str(item.get("stored_relpath") or "")
            target = str(item.get("target_path") or "")
            source = source_root / task_id / relpath
            digest.update(
                (
                    f"{task_id}\0{relpath}\0{target}\0"
                    f"{source.stat().st_size}\0{_file_sha256(source)}\n"
                ).encode()
            )
    return digest.hexdigest()


def _stage_tasks(
    *,
    source_root: Path,
    destination_root: Path,
    metadata_by_id: dict[str, dict[str, Json]],
    condition: str,
) -> dict[str, dict[str, Json]]:
    destination_root.mkdir(parents=True)
    staged: dict[str, dict[str, Json]] = {}
    for task_id, metadata in metadata_by_id.items():
        destination = destination_root / task_id
        _copy_tree(source_root / task_id, destination)
        staged_metadata = json.loads(json.dumps(metadata))
        if condition == "clean":
            # 只有 clean 才裁掉 noise 输入；noise / curated / task_files 都按
            # manifest 原样保留（task_files 的差别只在 workspace 是否带角色
            # 工作区，见 _stage_workspaces）。
            manifest = staged_metadata.get("data_manifest")
            assert isinstance(manifest, list)
            staged_metadata["data_manifest"] = [
                item
                for item in manifest
                if not isinstance(item, dict)
                or str(item.get("input_role") or "standard") == "standard"
            ]
            if isinstance(staged_metadata.get("input_file_summary"), dict):
                staged_metadata["input_file_summary"] = {
                    "standard": len(staged_metadata["data_manifest"]),
                    "noise": 0,
                }
        (destination / "metadata.json").write_text(
            json.dumps(staged_metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staged[task_id] = staged_metadata
    return staged


def _stage_workspaces(
    *,
    config: dict[str, Json],
    roles: set[str],
    runtime_root: Path,
    condition: str,
) -> tuple[Path, dict[str, dict[str, Json]]]:
    workspace_root = runtime_root / "workspace_snapshots"
    workspace_root.mkdir()
    summaries: dict[str, dict[str, Json]] = {}
    if condition not in ROLE_WORKSPACE_CONDITIONS:
        # clean / task_files：从空角色种子开始，任务输入由 manifest 物化。
        for role in sorted(roles):
            workspace_name = ROLE_WORKSPACE_NAMES.get(role, _safe_slug(role))
            path = workspace_root / workspace_name
            path.mkdir()
            summary = _tree_summary(path)
            summary["source"] = "empty-role-seed"
            summaries[workspace_name] = summary
        return workspace_root, summaries

    runtime = config["runtime"]
    assert isinstance(runtime, dict)
    snapshot_root_raw = runtime.get("workspace_snapshot_root")
    snapshot_root = (
        _resolve_path(snapshot_root_raw)
        if snapshot_root_raw not in (None, "")
        else None
    )
    role_sources = runtime.get("role_sources")
    if role_sources is None:
        role_sources = {}
    if not isinstance(role_sources, dict):
        raise SystemExit("runtime.role_sources must be a mapping")

    selected_sources: list[tuple[str, str, Path, dict[str, Json]]] = []
    for role in sorted(roles):
        workspace_name = ROLE_WORKSPACE_NAMES.get(role)
        if workspace_name is None:
            raise SystemExit(
                f"unsupported role {role!r}; add it to ROLE_WORKSPACE_NAMES"
            )
        source = (
            snapshot_root / workspace_name
            if snapshot_root is not None
            else None
        )
        if source is None:
            raw_source = role_sources.get(role)
            if not isinstance(raw_source, str) or not raw_source.strip():
                raise SystemExit(
                    f"runtime.role_sources has no source for role {role!r}"
                )
            source = _resolve_path(raw_source)
        if not source.is_dir():
            raise SystemExit(
                f"workspace source for role {role!r} not found: {source}"
            )
        source_summary = _tree_summary(source)
        selected_sources.append(
            (role, workspace_name, source, source_summary)
        )

    # A frozen snapshot already on the same local XFS filesystem can and
    # should be cloned with mandatory reflinks. Its logical byte count must
    # not be charged against free space as though it were a physical copy.
    # Requiring reflinks also prevents a silent fallback to a full copy.
    use_reflink_snapshot = bool(snapshot_root) and all(
        source.stat().st_dev == runtime_root.stat().st_dev
        for _, _, source, _ in selected_sources
    )
    required_bytes = (
        0
        if use_reflink_snapshot
        else sum(
            int(summary["bytes"])
            for _, _, _, summary in selected_sources
        )
    )
    minimum_free = int(
        float(runtime.get("minimum_free_gb") or 10) * (1024**3)
    )
    available = shutil.disk_usage(runtime_root).free
    if available < required_bytes + minimum_free:
        raise SystemExit(
            "insufficient free space for local workspace snapshots: "
            f"{available / (1024**3):.1f} GiB available, "
            f"{required_bytes / (1024**3):.1f} GiB source data plus "
            f"{minimum_free / (1024**3):.1f} GiB reserve required; "
            "refusing to fall back to direct source execution"
        )

    for role, workspace_name, source, source_summary in selected_sources:
        destination = workspace_root / workspace_name
        _copy_tree(
            source,
            destination,
            require_reflink=use_reflink_snapshot,
        )
        destination_summary = _tree_summary(destination)
        if source_summary != destination_summary:
            raise SystemExit(
                f"workspace snapshot verification failed for {role!r}: "
                f"{source_summary} != {destination_summary}"
            )
        destination_summary["source"] = str(source)
        summaries[workspace_name] = destination_summary
    return workspace_root, summaries


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _git_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode != 0 or bool(result.stdout.strip())


def _inspect_image(image: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or f"image not found: {image}")
    return result.stdout.strip()


def _write_case_configs(
    prepared: PreparedRun,
    metadata_by_id: dict[str, dict[str, Json]],
    workspace_root: Path,
) -> None:
    agent_raw = prepared.config["agent"]
    runtime_raw = prepared.config["runtime"]
    assert isinstance(agent_raw, dict) and isinstance(runtime_raw, dict)
    agent = _model_config(agent_raw, default_effort="max")
    harness = _agent_harness(prepared.config)
    resources = runtime_raw.get("resources") or {}
    if not isinstance(resources, dict):
        raise SystemExit("runtime.resources must be a mapping")
    resources = {
        "cpus": str(resources.get("cpus") or "2"),
        "memory_mb": max(1, int(resources.get("memory_mb") or 8192)),
        "pids": max(1, int(resources.get("pids") or 512)),
        "storage_mb": max(1, int(resources.get("storage_mb") or 20480)),
    }
    configs_dir = prepared.suite_root / "configs"
    maps_dir = prepared.suite_root / "fs_maps"
    configs_dir.mkdir(parents=True, exist_ok=True)
    maps_dir.mkdir(exist_ok=True)
    condition = str(prepared.config["condition"])

    for case in prepared.cases:
        metadata = metadata_by_id[case.task_id]
        role = str(metadata["file_system"])
        workspace_name = ROLE_WORKSPACE_NAMES.get(role, _safe_slug(role))
        workspace = workspace_root / workspace_name
        fs_map = {
            "raw_work_dir": {role: str(workspace)},
            "standard_work_dir": {role: str(workspace)},
            "work_dir": {role: str(workspace)},
        }
        fs_map_path = maps_dir / f"{case.case_id}.json"
        _write_json(fs_map_path, fs_map)
        display_with_effort = (
            f"{agent['display_name']}-{str(agent['reasoning_effort']).capitalize()}"
        )
        run_name = (
            f"{_safe_slug(prepared.config['name'])}-"
            f"{case.case_id}-{condition}-{_safe_slug(display_with_effort)}"
        )
        run_config = {
            "agent_name": harness,
            "model_name": display_with_effort,
            "run_name": run_name,
            "task_path": str(prepared.runtime_task_root),
            "task_ids": [case.task_id],
            "output_dir": str(
                prepared.suite_root / "agent_output" / case.case_id
            ),
            "fs_map_file": str(fs_map_path),
            "prompt_language": "auto",
            "prompt_head": None,
            "prompt_tail": None,
            "prompt_head_by_language": {"en": None, "cn": None},
            "prompt_tail_by_language": {"en": None, "cn": None},
            "timeout_sec": agent["timeout_seconds"],
            "task_target_output_dir": "model_output",
            "task_parallel": False,
            "task_parallel_workers": 1,
            "task_workdir_cleanup": "failed",
            "task_workspace_profile": "role-isolated",
            "task_workdir_isolation": True,
            "task_workdir_materialization": "copy",
            "task_isolation": "container",
            "task_resources": resources,
            "eval_while_running": False,
            "eval_yaml": "/workspace/strict/judge.yaml",
            "api_provider": _api_provider(agent),
        }
        _write_yaml(configs_dir / f"{case.case_id}.yaml", run_config)


def _prepare(config_path: Path) -> PreparedRun:
    config = _read_yaml(config_path)
    if int(config.get("version") or 0) != 1:
        raise SystemExit("unsupported config version; expected version: 1")
    name = str(config.get("name") or "experiment").strip()
    task_root = _resolve_path(config.get("task_dir") or "")
    cases = _normalize_cases(config.get("task_ids"), config.get("repeat", 1))
    condition = str(config.get("condition") or "noise").strip().lower()
    if condition not in CONDITIONS:
        raise SystemExit("condition must be one of: " + ", ".join(CONDITIONS))
    parallelism = max(1, int(config.get("parallelism") or 1))
    config["name"] = name
    config["condition"] = condition
    config["parallelism"] = parallelism
    metadata_by_id, roles = _validate_task_metadata(task_root, cases)
    if condition == "curated":
        # curated: task_dir 必须是 curate_workspace.py 产出的 preprocessed 根
        # (选集 manifest + curation.json 档案);防止把原始任务集误标为 curated。
        missing = [
            task_id
            for task_id in sorted(metadata_by_id)
            if not (task_root / str(task_id) / "curation.json").is_file()
        ]
        if missing:
            raise SystemExit(
                "condition=curated requires <task_dir>/<task_id>/curation.json "
                "(curate_workspace.py output) for task(s): " + ", ".join(missing)
            )
    _model_config(config.get("agent"), default_effort="max")
    _agent_harness(config)
    judge_raw = config.get("judge")
    judge = _model_config(judge_raw, default_effort="medium")
    assert isinstance(judge_raw, dict)

    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        raise SystemExit("runtime must be a mapping")
    local_root = _resolve_path(runtime.get("local_root") or "/tmp")
    persistent_base = _resolve_path(
        runtime.get("persistent_root") or "evaluation/experiments"
    )
    env_file_raw = runtime.get("env_file")
    env_file = _resolve_path(env_file_raw) if env_file_raw else None
    _check_free_space(local_root, float(runtime.get("minimum_free_gb") or 10))
    image = str(runtime.get("image") or EXPECTED_IMAGE)
    if image != EXPECTED_IMAGE:
        raise SystemExit(
            f"runtime.image must be {EXPECTED_IMAGE!r}; got {image!r}"
        )
    image_id = _inspect_image(image)
    expected_image_id = str(
        runtime.get("expected_image_id") or DEFAULT_IMAGE_ID
    ).strip()
    if expected_image_id and image_id != expected_image_id:
        raise SystemExit(
            f"expected image {expected_image_id}, got {image_id}"
        )

    run_id = (
        f"workspace-bench-{_safe_slug(name)}-{_git_commit()[:8]}-"
        f"{_utc_timestamp()}"
    )
    runtime_root = local_root / run_id
    persistent_root = persistent_base / run_id.removeprefix("workspace-bench-")
    if runtime_root.exists() or persistent_root.exists():
        raise SystemExit(
            f"refusing to reuse existing run: {runtime_root} / {persistent_root}"
        )
    runtime_root.mkdir(parents=True)
    persistent_root.mkdir(parents=True)
    runtime_eval = _copy_runtime_code(runtime_root)
    runtime_task_root = runtime_eval / task_root.name
    staged_metadata = _stage_tasks(
        source_root=task_root,
        destination_root=runtime_task_root,
        metadata_by_id=metadata_by_id,
        condition=condition,
    )
    source_input_digest = _source_input_digest(task_root, staged_metadata)
    if _source_input_digest(runtime_task_root, staged_metadata) != source_input_digest:
        raise SystemExit("staged task inputs do not match source task inputs")
    workspace_root, workspace_summaries = _stage_workspaces(
        config=config,
        roles=roles,
        runtime_root=runtime_root,
        condition=condition,
    )
    suite_root = runtime_eval / "experiments" / "generic_suite"
    suite_root.mkdir(parents=True)
    judge_config_path = suite_root / "configs" / "judge.yaml"
    judge_config_path.parent.mkdir(parents=True)
    _write_yaml(judge_config_path, _judge_yaml(judge, judge_raw))

    prepared = PreparedRun(
        config=config,
        config_path=config_path,
        run_id=run_id,
        runtime_root=runtime_root,
        persistent_root=persistent_root,
        runtime_eval_root=runtime_eval,
        runtime_task_root=runtime_task_root,
        suite_root=suite_root,
        env_file=env_file if env_file is not None and env_file.is_file() else None,
        cases=cases,
        judge_config_path=judge_config_path,
        image=image,
        image_id=image_id,
    )
    _write_case_configs(prepared, staged_metadata, workspace_root)

    task_details = {}
    for task_id, metadata in staged_metadata.items():
        metadata_path = runtime_task_root / task_id / "metadata.json"
        manifest = metadata.get("data_manifest") or []
        task_details[task_id] = {
            "metadata_sha256": _file_sha256(metadata_path),
            "rubric_count": len(metadata.get("rubrics") or []),
            "manifest_inputs": len(manifest),
            "standard_inputs": sum(
                isinstance(item, dict)
                and str(item.get("input_role") or "standard") == "standard"
                for item in manifest
            ),
            "noise_inputs": sum(
                isinstance(item, dict) and item.get("input_role") == "noise"
                for item in manifest
            ),
            "role": metadata.get("file_system"),
        }
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at_utc": _utc_iso(),
        "git_commit": _git_commit(),
        "working_tree_dirty": _git_dirty(),
        "source_config": str(config_path),
        "runtime_root": str(runtime_root),
        "persistent_root": str(persistent_root),
        "task_root": str(task_root),
        "runtime_task_root": str(runtime_task_root),
        "task_input_tree_sha256": source_input_digest,
        "dataset": _dataset_name(task_root, config),
        "condition": condition,
        "parallelism": parallelism,
        "cases": [case.__dict__ for case in cases],
        "agent": _safe_model_manifest(
            _model_config(config["agent"], default_effort="max")
        ),
        "agent_harness": _agent_harness(config),
        "judge": _safe_model_manifest(judge),
        "image": image,
        "image_id": image_id,
        "isolation": {
            "mode": "strict-allowlist",
            "repository_root_mounted": False,
            "runtime_storage": str(runtime_root),
        },
        "workspace_snapshots": workspace_summaries,
        "tasks": task_details,
    }
    _write_json(runtime_root / "experiment_manifest.json", manifest)
    _write_json(suite_root / "experiment_manifest.json", manifest)
    _write_json(persistent_root / "experiment_manifest.json", manifest)
    safe_config = json.loads(json.dumps(config))
    for section in ("agent", "judge"):
        raw_section = safe_config.get(section)
        if isinstance(raw_section, dict):
            raw_section.pop("api_key", None)
    _write_yaml(persistent_root / "experiment.yaml", safe_config)
    _write_yaml(runtime_root / "experiment.yaml", safe_config)
    return prepared


def _read_json(path: Path) -> dict[str, Json]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _case_run_root(prepared: PreparedRun, case: CaseSpec) -> Path:
    config = _read_yaml(
        prepared.suite_root / "configs" / f"{case.case_id}.yaml"
    )
    return (
        Path(str(config["output_dir"]))
        / (
            f"{config['agent_name']}--{config['model_name']}--"
            f"{config['run_name']}"
        )
        / case.task_id
    )


def _case_output_root(prepared: PreparedRun, case: CaseSpec) -> Path:
    config = _read_yaml(
        prepared.suite_root / "configs" / f"{case.case_id}.yaml"
    )
    return Path(str(config["output_dir"]))


def _replace_copy(source: Path, destination: Path) -> None:
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _persist_case(
    prepared: PreparedRun,
    case: CaseSpec,
    agent_case: Path | None,
    judge_case: Path | None,
    status: dict[str, Json],
) -> None:
    destination = prepared.persistent_root / "cases" / case.case_id
    destination.mkdir(parents=True, exist_ok=True)
    if agent_case is not None and agent_case.is_dir():
        for name in ("metadata.json", "agent.json", "agent.log"):
            source = agent_case / name
            if source.is_file():
                _replace_copy(source, destination / "agent" / name)
        for name in ("input_source", "output"):
            source = agent_case / name
            if source.is_dir():
                _replace_copy(source, destination / "agent" / name)
        for name in (
            "container-isolation.json",
            "runner_stderr.txt",
            "runner_stdout.txt",
        ):
            source = agent_case / "raw" / name
            if source.is_file():
                _replace_copy(source, destination / "agent" / "raw" / name)
        report = agent_case.parent / "agent_runner_report.json"
        if report.is_file():
            _replace_copy(report, destination / "agent_runner_report.json")
    if judge_case is not None and judge_case.is_dir():
        for source in judge_case.glob("rubrics_judge--*.json"):
            _replace_copy(source, destination / "judge" / source.name)
    _write_json(destination / "status.json", status)


def _prepare_judge_case(agent_case: Path, judge_case: Path) -> None:
    if judge_case.exists():
        shutil.rmtree(judge_case)
    judge_case.mkdir(parents=True)
    (judge_case / "raw").mkdir()
    for name in ("metadata.json", "agent.json", "agent.log"):
        source = agent_case / name
        if source.is_file():
            shutil.copy2(source, judge_case / name)
    if (agent_case / "input_source").is_dir():
        shutil.copytree(agent_case / "input_source", judge_case / "input_source")
    shutil.copytree(agent_case / "output", judge_case / "output")


def _judge_command(
    prepared: PreparedRun,
    case: CaseSpec,
    judge_case: Path,
) -> list[str]:
    judge_raw = prepared.config["judge"]
    assert isinstance(judge_raw, dict)
    resources = prepared.config["runtime"].get("resources") or {}
    memory_mb = max(1, int(resources.get("memory_mb") or 8192))
    cpus = str(resources.get("cpus") or "2")
    pids = max(1, int(resources.get("pids") or 512))
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=8g,mode=1777",
        "--tmpfs",
        "/home:rw,size=1g,mode=1777",
        "--pids-limit",
        str(pids),
        "--memory",
        f"{memory_mb}m",
        "--cpus",
        cpus,
    ]
    if prepared.env_file is not None:
        command.extend(["--env-file", str(prepared.env_file)])
    judge_model = _model_config(judge_raw, default_effort="medium")
    provider_environment = {
            "APP_ID",
            "APP_KEY",
            "WS_MODEL_BASE_URL",
            "GPT56SOL_MODEL",
            "GPT56LUNA_MODEL",
            "GPT56TERRA_MODEL",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "JUDGE_MODEL",
            "JUDGE_BASE_URL",
        }
    provider_environment.add(str(judge_model["api_key_env"]))
    for key in sorted(provider_environment):
        if key in os.environ:
            command.extend(["-e", key])
    # Keep image-installed SDK dependencies visible on a fresh checkout.
    # Docker's -v creates missing host paths as empty directories, hiding the
    # image's node_modules; only mount an actual local dependency tree.
    node_modules = prepared.runtime_eval_root / "node_modules"
    if node_modules.is_dir():
        command.extend(
            ["-v", f"{node_modules}:/workspace/Workspace-Bench/evaluation/node_modules:ro"]
        )
    command.extend(
        [
            "-e",
            "HOME=/tmp/home",
            "-e",
            "XDG_CACHE_HOME=/tmp/cache",
            "-v",
            (
                f"{prepared.runtime_eval_root / 'src'}:"
                "/workspace/Workspace-Bench/evaluation/src:ro"
            ),
            "-v",
            (
                f"{prepared.runtime_eval_root / 'baselines'}:"
                "/workspace/Workspace-Bench/evaluation/baselines:ro"
            ),
            "-v",
            f"{prepared.judge_config_path}:/workspace/strict/judge.yaml:ro",
            "-v",
            f"{judge_case.parent}:/judge-results:rw",
            "-w",
            f"/judge-results/{case.task_id}",
            "--entrypoint",
            "/bin/bash",
            prepared.image,
            "-lc",
            (
                "set -euo pipefail; mkdir -p /tmp/home /tmp/cache; "
                "python3 /workspace/Workspace-Bench/evaluation/src/"
                "agent_as_a_judge.py "
                f"--task-dir /judge-results/{case.task_id} "
                "--eval-yaml /workspace/strict/judge.yaml --overwrite "
                f"--max-retries {max(0, int(judge_raw.get('max_retries') or 2))} "
                f"--max-str-len {max(1, int(judge_raw.get('max_string_length') or 8000))} "
                f"--max-trace-items {max(1, int(judge_raw.get('max_trace_items') or 60))} "
                f"--max-output-files {max(1, int(judge_raw.get('max_output_files') or 10))}"
            ),
        ]
    )
    return command


def _run_case(prepared: PreparedRun, case: CaseSpec) -> dict[str, Json]:
    return _run_case_with_updates(prepared, case)


def _run_case_with_updates(
    prepared: PreparedRun,
    case: CaseSpec,
    on_status: Callable[[dict[str, Json]], None] | None = None,
) -> dict[str, Json]:
    started = time.monotonic()
    case_log = prepared.persistent_root / "logs" / f"{case.case_id}.log"
    case_log.parent.mkdir(parents=True, exist_ok=True)
    agent_raw = prepared.config["agent"]
    judge_raw = prepared.config["judge"]
    assert isinstance(agent_raw, dict) and isinstance(judge_raw, dict)
    agent_attempts = max(1, int(agent_raw.get("attempts") or 3))
    judge_attempts = max(1, int(judge_raw.get("attempts") or 3))
    status: dict[str, Json] = {
        "case_id": case.case_id,
        "task_id": case.task_id,
        "repeat": case.repeat,
        "status": "running_agent",
        "phase": "agent",
        "started_at": _utc_iso(),
        "agent_attempts": 0,
        "judge_attempts": 0,
        "image_id": prepared.image_id,
    }

    def publish_status() -> None:
        if on_status is not None:
            on_status(dict(status))

    publish_status()
    agent_case: Path | None = None
    judge_case = (
        prepared.suite_root / "judge_input" / case.case_id / case.task_id
    )
    env = dict(os.environ)
    python = prepared.runtime_eval_root / ".venv" / "bin" / "python"
    if not python.is_file():
        python = Path(sys.executable)

    with case_log.open("a", encoding="utf-8") as log:
        for attempt in range(1, agent_attempts + 1):
            status["agent_attempts"] = attempt
            status["status"] = "running_agent"
            status["phase"] = "agent"
            publish_status()
            output_root = _case_output_root(prepared, case)
            if output_root.exists():
                attempt_root = (
                    prepared.suite_root
                    / "attempts"
                    / case.case_id
                    / f"agent-{attempt:02d}-{_utc_timestamp()}"
                )
                attempt_root.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(output_root), str(attempt_root))
            command = [
                str(python),
                str(
                    prepared.runtime_eval_root
                    / "scripts"
                    / "run_strict_task_config.py"
                ),
                "--run-config",
                str(
                    prepared.suite_root
                    / "configs"
                    / f"{case.case_id}.yaml"
                ),
                "--task-root",
                str(prepared.runtime_task_root),
                "--task-id",
                case.task_id,
                "--dataset",
                _strict_runner_dataset(
                    prepared.runtime_task_root,
                    prepared.config,
                ),
                "--expected-image-id",
                prepared.image_id,
            ]
            log.write(
                f"[{_utc_iso()}] agent attempt={attempt} "
                f"command={json.dumps(command)}\n"
            )
            log.flush()
            result = subprocess.run(
                command,
                cwd=prepared.runtime_eval_root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            candidate = _case_run_root(prepared, case)
            agent = _read_json(candidate / "agent.json")
            if (
                candidate.is_dir()
                and (candidate / "output").is_dir()
                and agent.get("status") == "passed"
            ):
                agent_case = candidate
                break
            log.write(
                f"[{_utc_iso()}] agent attempt={attempt} "
                f"failed return_code={result.returncode}\n"
            )
            log.flush()
            if attempt < agent_attempts:
                time.sleep(30)

        if agent_case is None:
            status.update(
                {
                    "status": "failed",
                    "phase": "agent",
                    "return_code": 1,
                    "finished_at": _utc_iso(),
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            _persist_case(prepared, case, None, None, status)
            return status

        judge_model = _model_config(judge_raw, default_effort="medium")
        expected_total = len(
            _read_json(agent_case / "metadata.json").get("rubrics") or []
        )
        judge_result: Path | None = None
        for attempt in range(1, judge_attempts + 1):
            status["judge_attempts"] = attempt
            status["status"] = "running_judge"
            status["phase"] = "judge"
            publish_status()
            _prepare_judge_case(agent_case, judge_case)
            command = _judge_command(prepared, case, judge_case)
            log.write(
                f"[{_utc_iso()}] judge attempt={attempt} "
                f"model={judge_model['display_name']}\n"
            )
            log.flush()
            result = subprocess.run(
                command,
                cwd=prepared.runtime_eval_root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            candidates = sorted(judge_case.glob("rubrics_judge--*.json"))
            if candidates:
                value = _read_json(candidates[-1])
                if (
                    result.returncode == 0
                    and is_valid_judge_result(value, expected_total)
                ):
                    judge_result = candidates[-1]
                    break
            log.write(
                f"[{_utc_iso()}] judge attempt={attempt} "
                f"failed return_code={result.returncode}\n"
            )
            log.flush()
            if attempt < judge_attempts:
                time.sleep(30)

    if judge_result is None:
        status.update(
            {
                "status": "failed",
                "phase": "judge",
                "return_code": 1,
                "finished_at": _utc_iso(),
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        )
        _persist_case(prepared, case, agent_case, judge_case, status)
        return status

    judge_value = _read_json(judge_result)
    summary = judge_value["summary"]
    status.update(
        {
            "status": "judged",
            "phase": "complete",
            "return_code": 0,
            "finished_at": _utc_iso(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "judge_summary": summary,
            "judge_result": str(judge_result),
        }
    )
    _persist_case(prepared, case, agent_case, judge_case, status)
    return status


def _summary_row(status: dict[str, Json]) -> dict[str, str]:
    summary = status.get("judge_summary")
    passed = failed = total = ""
    rate = rate_pct = ""
    if isinstance(summary, dict):
        try:
            passed_int = int(summary["passed"])
            total_int = int(summary["total"])
            failed_int = int(summary.get("failed", total_int - passed_int))
            passed, failed, total = map(str, (passed_int, failed_int, total_int))
            if total_int > 0:
                value = passed_int / total_int
                rate = f"{value:.6f}"
                rate_pct = f"{value * 100:.2f}%"
        except (KeyError, TypeError, ValueError):
            pass
    return {
        "case_id": str(status.get("case_id") or ""),
        "task_id": str(status.get("task_id") or ""),
        "repeat": str(status.get("repeat") or ""),
        "status": str(status.get("status") or "unknown"),
        "passed": passed,
        "failed": failed,
        "total": total,
        "pass_rate": rate,
        "pass_rate_pct": rate_pct,
        "duration_seconds": str(status.get("duration_seconds") or ""),
        "return_code": str(status.get("return_code") or ""),
    }


def _write_summary(
    persistent_root: Path,
    statuses: Iterable[dict[str, Json]],
) -> None:
    rows = [_summary_row(status) for status in statuses]
    rows.sort(
        key=lambda row: (
            int(row["task_id"]) if row["task_id"].isdigit() else sys.maxsize,
            row["task_id"],
            int(row["repeat"]) if row["repeat"].isdigit() else 0,
        )
    )
    path = persistent_root / "summary.tsv"
    temporary = path.with_suffix(".tsv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=SUMMARY_FIELDS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        judged = [row for row in rows if row["total"]]
        if judged:
            passed = sum(int(row["passed"]) for row in judged)
            total = sum(int(row["total"]) for row in judged)
            failed = total - passed
            rate = passed / total if total else 0.0
            writer.writerow(
                {
                    "case_id": "MICRO",
                    "status": "aggregate",
                    "passed": str(passed),
                    "failed": str(failed),
                    "total": str(total),
                    "pass_rate": f"{rate:.6f}",
                    "pass_rate_pct": f"{rate * 100:.2f}%",
                }
            )
    temporary.replace(path)
    _write_json(persistent_root / "summary.json", rows)


def _write_checksums(root: Path) -> None:
    rows = []
    checksum_path = root / "SHA256SUMS"
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_file() and path != checksum_path:
            rows.append(f"{_file_sha256(path)}  {path.relative_to(root).as_posix()}")
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _verify_checksums(root: Path) -> None:
    checksum_path = root / "SHA256SUMS"
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        path = root / relative
        if not path.is_file() or _file_sha256(path) != expected:
            raise SystemExit(f"persisted artifact checksum mismatch: {path}")


def run_experiment(config_path: Path) -> int:
    prepared = _prepare(config_path)
    _load_dotenv(prepared.env_file)
    statuses: dict[str, dict[str, Json]] = {}
    summary_lock = threading.Lock()
    print(f"run_id: {prepared.run_id}")
    print(f"runtime: {prepared.runtime_root}")
    print(f"persistent: {prepared.persistent_root}")
    print(
        f"cases: {len(prepared.cases)}; "
        f"parallelism: {prepared.config['parallelism']}"
    )
    for case in prepared.cases:
        statuses[case.case_id] = {
            "case_id": case.case_id,
            "task_id": case.task_id,
            "repeat": case.repeat,
            "status": "queued",
            "return_code": "",
        }
    _write_summary(prepared.persistent_root, statuses.values())

    def update_status(status: dict[str, Json]) -> None:
        with summary_lock:
            statuses[str(status["case_id"])] = status
            _write_summary(prepared.persistent_root, statuses.values())

    with ThreadPoolExecutor(
        max_workers=min(
            int(prepared.config["parallelism"]),
            len(prepared.cases),
        )
    ) as executor:
        futures = {
            executor.submit(
                _run_case_with_updates,
                prepared,
                case,
                update_status,
            ): case
            for case in prepared.cases
        }
        for future in as_completed(futures):
            case = futures[future]
            try:
                status = future.result()
            except Exception as exc:
                status = {
                    "case_id": case.case_id,
                    "task_id": case.task_id,
                    "repeat": case.repeat,
                    "status": "failed",
                    "phase": "launcher",
                    "return_code": 1,
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": _utc_iso(),
                }
                _persist_case(prepared, case, None, None, status)
            with summary_lock:
                statuses[case.case_id] = status
                _write_summary(prepared.persistent_root, statuses.values())
            summary = status.get("judge_summary")
            score = ""
            if isinstance(summary, dict):
                score = f" {summary.get('passed')}/{summary.get('total')}"
            print(f"[{case.case_id}] {status.get('status')}{score}", flush=True)

    final_statuses = [
        statuses.get(
            case.case_id,
            {
                "case_id": case.case_id,
                "task_id": case.task_id,
                "repeat": case.repeat,
                "status": "missing",
                "return_code": 1,
            },
        )
        for case in prepared.cases
    ]
    _write_summary(prepared.persistent_root, final_statuses)
    _write_checksums(prepared.persistent_root)
    _verify_checksums(prepared.persistent_root)
    success = all(status.get("status") == "judged" for status in final_statuses)
    keep_runtime = bool(
        prepared.config["runtime"].get("keep_local_runtime", True)
    )
    if not keep_runtime and success:
        shutil.rmtree(prepared.runtime_root)
    return 0 if success else 1


def write_default_config(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(
            f"config already exists: {path}; use --force to overwrite"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
    print(f"created: {path}")
    print(f"run with: {sys.executable} {SCRIPT_PATH} --config {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or run a YAML-driven Workspace-Bench experiment."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Run the experiment described by this YAML file.",
    )
    parser.add_argument(
        "--init",
        type=Path,
        help="Write a commented default YAML to this path and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --init to overwrite an existing YAML file.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate task/model selections without staging or running Docker.",
    )
    args = parser.parse_args()

    if args.config and args.init:
        parser.error("--config and --init are mutually exclusive")
    if not args.config:
        write_default_config(
            (args.init or Path("experiment.yaml")).resolve(),
            force=args.force,
        )
        return 0

    config_path = args.config.resolve()
    config = _read_yaml(config_path)
    runtime_config = config.get("runtime")
    provider = (
        str(runtime_config.get("provider") or "") if isinstance(runtime_config, dict) else ""
    )
    if not runtime_backends.is_builtin(provider):
        # 非内置 provider → 交给插件后端（本仓库只内置 local；远程沙盒等在插件里）
        backend = runtime_backends.load_backend(provider)
        if backend is None:
            raise SystemExit(runtime_backends.search_report(provider))
        return int(backend(config_path, validate_only=args.validate_only))
    cases = _normalize_cases(config.get("task_ids"), config.get("repeat", 1))
    task_root = _resolve_path(config.get("task_dir") or "")
    condition = str(config.get("condition") or "noise").strip().lower()
    if condition not in CONDITIONS:
        raise SystemExit("condition must be one of: " + ", ".join(CONDITIONS))
    metadata_by_id, _ = _validate_task_metadata(task_root, cases)
    if condition == "curated":
        # 与远程后端同款守卫:preprocessed 根必须带 curation.json
        missing = [
            task_id
            for task_id in sorted(metadata_by_id)
            if not (task_root / str(task_id) / "curation.json").is_file()
        ]
        if missing:
            raise SystemExit(
                "condition=curated requires <task_dir>/<task_id>/curation.json "
                "(curate_workspace.py output) for task(s): " + ", ".join(missing)
            )
    _model_config(config.get("agent"), default_effort="max")
    _agent_harness(config)
    _model_config(config.get("judge"), default_effort="medium")
    if args.validate_only:
        print(
            json.dumps(
                {
                    "config": str(config_path),
                    "task_dir": str(task_root),
                    "cases": [case.__dict__ for case in cases],
                    "parallelism": max(1, int(config.get("parallelism") or 1)),
                    "condition": condition,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return run_experiment(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
