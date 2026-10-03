#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml


Json = Any

ROLE_DIRS = {
    "产品人员": "chanpin",
    "开发人员": "kaifa",
    "研究人员": "research",
    "运营人员": "yunying",
    "行政/后勤人员": "houqin",
}

# 网关上的 Hy3 路由名由环境变量提供，不写死在仓库里。
HY3_MODEL_ID = os.environ.get("HY3_MODEL", "")

MODEL_ALIASES = {
    "hy3": ("Hy3", HY3_MODEL_ID, "HY3"),
    "gpt-5.4": ("GPT-5.4", "gpt-5.4", "GPT54"),
    "gpt-5.6-luna": (
        "GPT-5.6-Luna",
        "api_azure_openai_gpt-5.6-luna",
        "GPT56LUNA",
    ),
    "gpt-5.6-terra": (
        "GPT-5.6-Terra",
        "api_azure_openai_gpt-5.6-terra",
        "GPT56TERRA",
    ),
    "deepseek-v4-flash": (
        "DeepSeek-V4-Flash",
        "api_deepseek_deepseek-v4-flash",
        "DEEPSEEKV4FLASH",
    ),
    "deepseek-v4-pro": (
        "DeepSeek-V4-Pro",
        "api_deepseek_deepseek-v4-pro",
        "DEEPSEEKV4PRO",
    ),
    "gemini-3.1-pro": ("Gemini-3.1-Pro", "gemini-3.1-pro-preview", "GEMINI31PRO"),
    "kimi-k2.5": ("Kimi-K2.5", "kimi-k2.5", "KIMIK25"),
    "glm-5.1": ("GLM-5.1", "glm-5.1", "GLM51"),
    "minimax-m2.7": ("MiniMax-M2.7", "MiniMax-M2.7", "MINIMAXM27"),
    "grok-4.3": ("Grok-4.3", "x-ai/grok-4.3", "GROK43"),
    "qwen-3.6": ("Qwen-3.6", "qwen/qwen3.6-35b-a3b", "QWEN36"),
    "qwen3.8-max": ("Qwen3.8-Max", "qwen3.8-max", "QWEN38MAX"),
}

APP_CREDENTIAL_BASE_URL = os.environ.get(
    "APP_CREDENTIAL_BASE_URL"
) or "${WS_MODEL_BASE_URL:-}"
WS_MODEL_BASE_URL = "${WS_MODEL_BASE_URL:-}"

def _safe_slug(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return s.strip("-").lower() or "custom"


def _display_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()) or "Custom"


def _normalize_task_ids(values: list[str] | None) -> list[str]:
    task_ids: list[str] = []
    for value in values or []:
        task_ids.extend(part.strip() for part in str(value).split(",") if part.strip())
    if not task_ids:
        return []
    invalid = [task_id for task_id in task_ids if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id)]
    if invalid:
        raise SystemExit(f"invalid task id(s): {', '.join(invalid)}")
    duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
    if duplicates:
        raise SystemExit(f"duplicate task id(s): {', '.join(duplicates)}")
    return task_ids


def _selection_suffix(*, task_ids: list[str], persona: str | None) -> tuple[str, str]:
    if task_ids:
        joined = "-".join(task_ids)
        if len(task_ids) <= 3 and len(joined) <= 48:
            return f"tasks-{_safe_slug(joined)}", f"Tasks-{_display_slug(joined)}"
        digest = hashlib.sha256("\0".join(task_ids).encode("utf-8")).hexdigest()[:10]
        return f"tasks-{len(task_ids)}-{digest}", f"Tasks-{len(task_ids)}-{digest}"
    if persona:
        slug = _safe_slug(persona)[:60]
        return f"persona-{slug}", f"Persona-{_display_slug(persona)[:60]}"
    return "", ""


def _normalize_harness(value: str) -> str:
    mapping = {
        "codex": "Codex",
        "claudecode": "ClaudeCode",
        "claude-code": "ClaudeCode",
    }
    key = value.strip().lower()
    if key not in mapping:
        raise SystemExit(f"unsupported harness: {value}")
    return mapping[key]


def _model_info(model: str, model_id: str | None, model_name: str | None, env_prefix: str | None) -> tuple[str, str, str, str]:
    key = model.strip().lower()
    default_name, default_id, default_env = MODEL_ALIASES.get(
        key,
        (_display_slug(model), model, re.sub(r"[^A-Za-z0-9]+", "_", model).upper()),
    )
    display_name = model_name or default_name
    llm_model = model_id or default_id
    env = env_prefix or default_env
    return key, display_name, llm_model, env


def _provider_config(
    harness: str,
    provider_type: str,
    env_prefix: str,
    llm_model: str,
    *,
    auth_type: str,
    auth_provider: str,
    auth_timeout_sec: int,
    base_url: str | None,
    reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
    api_retry_max_attempts: int = 6,
    api_retry_initial_delay_sec: float = 1.0,
    api_retry_max_delay_sec: float = 30.0,
    api_retry_total_timeout_sec: float = 600.0,
) -> dict[str, Json]:
    retry_config = {
        "apiRetryMaxAttempts": max(1, min(int(api_retry_max_attempts), 20)),
        "apiRetryInitialDelaySec": max(
            0.0, float(api_retry_initial_delay_sec)
        ),
        "apiRetryMaxDelaySec": max(0.0, float(api_retry_max_delay_sec)),
        "apiRetryTotalTimeoutSec": max(
            1.0, float(api_retry_total_timeout_sec)
        ),
    }
    if auth_type == "anthropic_app_credentials":
        if harness != "ClaudeCode":
            raise SystemExit(
                "DeepSeek models use native Anthropic Messages; "
                "select --harness claudecode"
            )
        return {
            "provider_type": "anthropic",
            "baseUrl": base_url or WS_MODEL_BASE_URL,
            "model": llm_model,
            "authType": "anthropic_app_credentials",
            "appId": "${APP_ID}",
            "appKey": "${APP_KEY}",
            "authTimeoutSec": max(1, int(auth_timeout_sec)),
            "wireApi": "anthropic_messages",
            **retry_config,
        }
    if auth_type == "cached_app_credentials":
        if harness != "Codex":
            raise SystemExit(
                "Standard OpenAI-compatible models use the Codex harness; "
                "select --harness codex"
            )
        if provider_type != "openai":
            raise SystemExit(
                "cached_app_credentials requires the OpenAI-compatible provider type"
            )
        uses_chat_completions = bool(HY3_MODEL_ID) and llm_model == HY3_MODEL_ID
        config: dict[str, Json] = {
            "provider_type": "openai",
            "baseUrl": base_url or WS_MODEL_BASE_URL,
            "model": llm_model,
            "authType": "cached_app_credentials",
            "appId": "${APP_ID}",
            "appKey": "${APP_KEY}",
            "authTimeoutSec": max(1, int(auth_timeout_sec)),
            "wireApi": (
                "chat_completions" if uses_chat_completions else "responses"
            ),
            "codexSandboxMode": "danger-full-access",
            **retry_config,
        }
        if reasoning_effort:
            config["reasoningEffort"] = reasoning_effort
        if max_completion_tokens is not None:
            token_key = (
                "maxCompletionTokens"
                if uses_chat_completions
                else "maxOutputTokens"
            )
            config[token_key] = max(
                1, min(int(max_completion_tokens), 128_000)
            )
        return config
    if auth_type == "app_credentials":
        if llm_model == "qwen3.8-max" and harness != "Codex":
            raise SystemExit(
                "qwen3.8-max uses the Responses protocol; "
                "select --harness codex"
            )
        if harness == "ClaudeCode" or provider_type == "anthropic":
            raise SystemExit(
                "app_credentials authentication requires an OpenAI Responses-compatible harness"
            )
        config = {
            "provider_type": "openai",
            "baseUrl": base_url or APP_CREDENTIAL_BASE_URL,
            "model": llm_model,
            "authType": "app_credentials",
            "appId": "${APP_ID}",
            "appKey": "${APP_KEY}",
            "authProvider": auth_provider,
            "authModel": llm_model,
            "authTimeoutSec": max(1, int(auth_timeout_sec)),
            "wireApi": "responses",
            "codexSandboxMode": "danger-full-access",
            **retry_config,
        }
        if reasoning_effort:
            config["reasoningEffort"] = reasoning_effort
        if max_completion_tokens is not None:
            config["maxOutputTokens"] = max(
                1, min(int(max_completion_tokens), 131_072)
            )
        return config
    if harness == "ClaudeCode" or provider_type == "anthropic":
        return {
            "provider_type": "anthropic",
            "baseUrl": f"${{{env_prefix}_ANTHROPIC_BASE_URL:-${{{env_prefix}_BASE_URL}}}}",
            "model": f"${{{env_prefix}_ANTHROPIC_MODEL:-{llm_model}}}",
            "apiKey": f"${{{env_prefix}_API_KEY}}",
            **retry_config,
        }
    return {
        "provider_type": provider_type,
        "baseUrl": f"${{{env_prefix}_BASE_URL}}",
        "model": llm_model,
        "apiKey": f"${{{env_prefix}_API_KEY}}",
        **retry_config,
    }


def _fs_map(
    eval_root: Path,
    harness: str,
    model_name: str,
    *,
    dataset: str,
) -> dict[str, dict[str, str]]:
    if dataset in {"tasks-new", "tasks-hard", "tasks-hard-wyk"}:
        # Role-isolated datasets start from the same role workspace profiles as the
        # original benchmark. The raw role directory is treated as an
        # immutable standard base and copied/reflinked into each case-local
        # writable workdir by agent_runner.
        role_workspaces = {
            role: str(eval_root / "filesys" / f"{prefix}_raw")
            for role, prefix in ROLE_DIRS.items()
        }
        return {
            "raw_work_dir": dict(role_workspaces),
            "standard_work_dir": dict(role_workspaces),
            "work_dir": dict(role_workspaces),
        }
    suffix = f"{harness}_{_display_slug(model_name)}"
    return {
        "raw_work_dir": {role: f"filesys/{prefix}_raw" for role, prefix in ROLE_DIRS.items()},
        "standard_work_dir": {role: f"filesys/{prefix}_standard" for role, prefix in ROLE_DIRS.items()},
        "work_dir": {role: f"filesys/{prefix}_workdir_{suffix}" for role, prefix in ROLE_DIRS.items()},
    }


def build_config(args: argparse.Namespace) -> Path:
    eval_root = Path(args.eval_root).resolve()
    harness = _normalize_harness(args.harness)
    model_key, model_name, llm_model, env_prefix = _model_info(
        args.model,
        args.model_id,
        args.model_name,
        args.env_prefix,
    )
    requested_auth_type = str(
        getattr(args, "auth_type", "auto") or "auto"
    ).strip().lower()
    auth_type = (
        "cached_app_credentials"
        if requested_auth_type == "auto"
        and model_key in {"hy3", "gpt-5.6-luna", "gpt-5.6-terra"}
        else (
            "anthropic_app_credentials"
            if requested_auth_type == "auto"
            and model_key in {"deepseek-v4-flash", "deepseek-v4-pro"}
            else (
                "app_credentials"
                if requested_auth_type == "auto"
                and model_key == "qwen3.8-max"
                else (
                    "bearer"
                    if requested_auth_type == "auto"
                    else requested_auth_type
                )
            )
        )
    )
    raw_auth_timeout = getattr(args, "auth_timeout_sec", None)
    auth_timeout_sec = (
        max(1, int(raw_auth_timeout))
        if raw_auth_timeout is not None
        else (
            120
            if auth_type
            in {"cached_app_credentials", "anthropic_app_credentials"}
            else 60
        )
    )
    requested_reasoning_effort = str(
        getattr(args, "reasoning_effort", "") or ""
    ).strip()
    if not requested_reasoning_effort:
        requested_reasoning_effort = (
            "high"
            if model_key == "hy3"
            else (
                "none"
                if model_key == "gpt-5.6-luna"
                else ("xhigh" if model_key == "qwen3.8-max" else "")
            )
        )
    if (
        model_key == "gpt-5.6-luna"
        and requested_reasoning_effort in {"minimal", "max"}
    ):
        raise SystemExit(
            "gpt-5.6-luna reasoning effort must be one of "
            "none/low/medium/high/xhigh"
        )

    dataset = args.dataset.strip().lower()
    if dataset not in {
        "smoke",
        "lite",
        "full",
        "tasks-new",
        "tasks-hard",
        "tasks-hard-wyk",
    }:
        raise SystemExit(f"unsupported dataset: {args.dataset}")

    task_ids = _normalize_task_ids(getattr(args, "task_ids", None))
    persona_value = getattr(args, "persona", None)
    persona = str(persona_value).strip() if persona_value is not None else None
    if persona_value is not None and not persona:
        raise SystemExit("--persona must not be empty")
    task_limit = getattr(args, "task_limit", None)
    selected = sum([task_limit is not None, bool(task_ids), persona is not None])
    if selected > 1:
        raise SystemExit("--task-limit, --task-ids, and --persona are mutually exclusive")

    selection_slug, selection_name = _selection_suffix(task_ids=task_ids, persona=persona)
    default_run_name = {
        "smoke": "Smoke",
        "lite": "Lite",
        "full": "Full",
        "tasks-new": "Tasks-New",
        "tasks-hard": "Tasks-Hard",
        "tasks-hard-wyk": "Tasks-Hard-WYK",
    }[dataset]
    run_name = args.run_name or (
        f"{default_run_name}-{selection_name}" if selection_name else default_run_name
    )
    task_path = (
        eval_root / "tasks_new"
        if dataset == "tasks-new"
        else (
            eval_root / "tasks_hard"
            if dataset == "tasks-hard"
            else (
                eval_root / "tasks_hard_wyk"
                if dataset == "tasks-hard-wyk"
                else eval_root / ("tasks" if dataset == "full" else "tasks_lite")
            )
        )
    )
    if task_limit is None and not task_ids and persona is None and dataset == "smoke":
        task_limit = 1
    task_parallel = not bool(args.no_task_parallel)
    task_parallel_workers = max(1, int(args.task_parallel_workers or 10))
    task_isolation = str(getattr(args, "task_isolation", "process") or "process").strip().lower()
    if task_isolation not in {"process", "container"}:
        raise SystemExit("--task-isolation must be process or container")
    task_resources = {
        "cpus": str(getattr(args, "task_cpus", "2")),
        "memory_mb": max(1, int(getattr(args, "task_memory_mb", 8192))),
        "pids": max(1, int(getattr(args, "task_pids", 512))),
        "storage_mb": max(1, int(getattr(args, "task_storage_mb", 20480))),
    }

    generated_root = eval_root / ".generated" / "run_configs"
    runs_dir = generated_root / "runs"
    fs_map_dir = generated_root / "fs_map"
    runs_dir.mkdir(parents=True, exist_ok=True)
    fs_map_dir.mkdir(parents=True, exist_ok=True)

    config_slug = f"{harness.lower()}-{_safe_slug(args.model)}-{dataset}"
    if selection_slug:
        config_slug = f"{config_slug}-{selection_slug}"
    elif args.run_name:
        config_slug = f"{config_slug}-{_safe_slug(args.run_name)}"
    fs_map_suffix = (
        f"{harness}_{_display_slug(model_name)}_"
        f"{'Tasks_New' if dataset == 'tasks-new' else ('Tasks_Hard_WYK' if dataset == 'tasks-hard-wyk' else 'Tasks_Hard')}"
        if dataset in {"tasks-new", "tasks-hard", "tasks-hard-wyk"}
        else f"{harness}_{_display_slug(model_name)}"
    )
    fs_map_path = fs_map_dir / f"fs_map_{fs_map_suffix}.json"
    fs_map_path.write_text(
        json.dumps(
            _fs_map(eval_root, harness, model_name, dataset=dataset),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    config: dict[str, Json] = {
        "agent_name": harness,
        "model_name": model_name,
        "run_name": run_name,
        "task_path": str(task_path),
        "output_dir": str(eval_root / "output"),
        "fs_map_file": str(fs_map_path),
        "prompt_language": "auto",
        "prompt_head": None,
        "prompt_tail": None,
        "prompt_head_by_language": {"en": None, "cn": None},
        "prompt_tail_by_language": {"en": None, "cn": None},
        "timeout_sec": float(args.timeout_sec),
        "task_target_output_dir": "model_output",
        "task_parallel": task_parallel,
        "task_parallel_workers": task_parallel_workers,
        "task_workdir_cleanup": "failed",
        "task_workspace_profile": (
            "role-isolated"
            if dataset in {"tasks-new", "tasks-hard", "tasks-hard-wyk"}
            else "legacy"
        ),
        "task_workdir_isolation": dataset in {
            "tasks-new",
            "tasks-hard",
            "tasks-hard-wyk",
        },
        "task_workdir_materialization": (
            "linked"
            if dataset == "tasks-new" and task_isolation == "container"
            else "copy"
        ),
        "task_isolation": task_isolation,
        "task_resources": task_resources,
        "eval_while_running": False,
        "eval_yaml": args.eval_yaml,
        "api_provider": _provider_config(
            harness,
            args.provider_type,
            env_prefix,
            llm_model,
            auth_type=auth_type,
            auth_provider=str(getattr(args, "auth_provider", "ali") or "ali"),
            auth_timeout_sec=auth_timeout_sec,
            base_url=(
                str(getattr(args, "base_url", "") or "").strip() or None
            ),
            reasoning_effort=requested_reasoning_effort or None,
            max_completion_tokens=getattr(args, "max_completion_tokens", None),
            api_retry_max_attempts=int(
                getattr(args, "api_retry_max_attempts", 6) or 6
            ),
            api_retry_initial_delay_sec=float(
                getattr(args, "api_retry_initial_delay_sec", 1.0)
            ),
            api_retry_max_delay_sec=float(
                getattr(args, "api_retry_max_delay_sec", 30.0)
            ),
            api_retry_total_timeout_sec=float(
                getattr(args, "api_retry_total_timeout_sec", 600.0)
            ),
        ),
    }
    if task_limit is not None:
        config["task_limit"] = int(task_limit)
    elif task_ids:
        config["task_ids"] = task_ids
    elif persona is not None:
        config["persona"] = persona

    config_path = runs_dir / f"{config_slug}.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Workspace-Bench run config from parameters.")
    parser.add_argument(
        "--harness",
        required=True,
        help="Codex or ClaudeCode",
    )
    parser.add_argument("--model", required=True, help="Model alias or custom model id")
    parser.add_argument(
        "--dataset",
        default="lite",
        choices=[
            "smoke",
            "lite",
            "full",
            "tasks-new",
            "tasks-hard",
            "tasks-hard-wyk",
        ],
    )
    parser.add_argument("--eval-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--provider-type", default="openai", choices=["openai", "anthropic"])
    parser.add_argument(
        "--auth-type",
        choices=[
            "auto",
            "bearer",
            "app_credentials",
            "cached_app_credentials",
            "anthropic_app_credentials",
        ],
        default="auto",
        help="Authentication scheme; auto selects the correct internal protocol for known models",
    )
    parser.add_argument(
        "--auth-provider",
        default="ali",
        help="Provider query value used by app_credentials authentication",
    )
    parser.add_argument(
        "--auth-timeout-sec",
        type=int,
        help="Authorization timeout; defaults to 120 for the gateway and 60 for compatible-mode",
    )
    parser.add_argument(
        "--base-url",
        help="Explicit provider base URL; app_credentials defaults to the internal compatible-mode endpoint",
    )
    parser.add_argument("--model-id", help="LLM provider model id; defaults from --model")
    parser.add_argument("--model-name", help="Display name used in output directory")
    parser.add_argument("--env-prefix", help="Environment variable prefix for BASE_URL/API_KEY")
    parser.add_argument(
        "--reasoning-effort",
        choices=[
            "no_think",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ],
        help="Reasoning effort for compatible Responses/Chat providers",
    )
    parser.add_argument(
        "--max-completion-tokens",
        type=int,
        default=16384,
        help="Maximum visible plus reasoning tokens for the selected provider",
    )
    parser.add_argument(
        "--api-retry-max-attempts",
        type=int,
        default=6,
    )
    parser.add_argument(
        "--api-retry-initial-delay-sec",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--api-retry-max-delay-sec",
        type=float,
        default=30.0,
    )
    parser.add_argument(
        "--api-retry-total-timeout-sec",
        type=float,
        default=600.0,
    )
    parser.add_argument(
        "--run-name",
        help="Output run name; defaults to Smoke/Lite/Full/Tasks-New",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--task-limit", type=int, help="Run the first N tasks in deterministic order")
    selection.add_argument(
        "--task-ids",
        nargs="+",
        help="Run exact task IDs; accepts spaces or comma-separated values",
    )
    selection.add_argument("--persona", help="Run every task whose metadata persona exactly matches this value")
    parser.add_argument("--timeout-sec", type=float, default=2000.0)
    parser.add_argument("--task-parallel-workers", type=int, help="Number of isolated task-level workers; defaults to 10")
    parser.add_argument("--no-task-parallel", action="store_true", help="Disable isolated task-level parallelism")
    parser.add_argument(
        "--task-isolation",
        choices=["process", "container"],
        default="process",
        help="Execution boundary recorded in the run config; the isolated Docker launcher requires container",
    )
    parser.add_argument("--task-cpus", default="2", help="Per-task CPU quota for the containerized protocol")
    parser.add_argument("--task-memory-mb", type=int, default=8192, help="Per-task memory limit in MiB")
    parser.add_argument("--task-pids", type=int, default=512, help="Per-task PID limit")
    parser.add_argument("--task-storage-mb", type=int, default=20480, help="Per-task writable storage limit in MiB")
    parser.add_argument("--eval-yaml", default="runs/judge.yaml")
    args = parser.parse_args()
    print(build_config(args))


if __name__ == "__main__":
    main()
