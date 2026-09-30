#!/usr/bin/env python3
"""svc_convert.driver —— 在 dev 容器里跑一次 Claude Code agent 步骤并解析结果。

复用 noise-id agentic 的驱动方式：`docker compose run workspace-bench node
agentic_driver.mjs cfg.json -o report.json`，cfg 的 customProvider 指向 AI Hub
Anthropic Messages。model 含 "deepseek" 时凭据带 timeout query（网关要求）。

约束：cfg.json 含凭据，只写 gitignored 的 .generated 下并 chmod 0600。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import EVAL_ROOT, GEN_ROOT, REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "evaluation" / "scripts"))
import noise_id_common as nc  # noqa: E402


COMPOSE = EVAL_ROOT / "docker" / "docker-compose.yaml"
SERVICE = "workspace-bench"
CTR_REPO = "/workspace/Workspace-Bench"
DRIVER = "/workspace/Workspace-Bench/evaluation/scripts/agentic_driver.mjs"

DEFAULT_TIMEOUT = 1800
DEFAULT_MAX_TURNS = 200


def host2ctr(path: Path) -> str:
    rel = path.resolve().relative_to(REPO_ROOT.resolve())
    return f"{CTR_REPO}/{rel}"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _dotenv_path() -> Path:
    env = EVAL_ROOT / ".env"
    if env.is_file():
        return env
    return REPO_ROOT / ".env"


def agent_json(
    *,
    prompt: str,
    cwd_host: Path,
    model: str,
    step_dir: Path,
    task_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> dict:
    """Run one Claude Code agent step; return parsed finalText JSON.

    Raises RuntimeError when report is missing / non-JSON.
    """
    _load_dotenv(_dotenv_path())
    if not step_dir.exists():
        step_dir.mkdir(parents=True)
    cfg_path = step_dir / "cfg.json"
    cfg = {
        "description": f"svc_convert {task_id}",
        "tasks": [
            {
                "id": task_id,
                "name": task_id,
                "prompt": prompt,
                "cwd": host2ctr(cwd_host),
                "timeout": int(timeout),
                "maxTurns": int(max_turns),
                "provider": "anthropic",
                "model": model,
                "customProvider": {
                    "baseUrl": os.environ.get(
                        "WS_MODEL_BASE_URL", nc.LLM_BASE_URL
                    ),
                    "apiKey": nc.gateway_credential(900, model=model),
                    "modelName": model,
                },
            }
        ],
    }
    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass

    report_host = step_dir / "report.json"
    report_host.unlink(missing_ok=True)
    log_host = step_dir / "run.log"
    cmd = [
        "docker", "compose", "-f", str(COMPOSE),
        "run", "--rm", "--no-deps", "-T", SERVICE,
        "node", DRIVER, host2ctr(cfg_path), "-o", host2ctr(report_host),
    ]
    with log_host.open("wb") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if not report_host.is_file():
        raise RuntimeError(
            f"agent step produced no report (rc={result.returncode}); "
            f"see {log_host}"
        )
    try:
        report = json.loads(report_host.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"agent step report not JSON: {exc}") from exc
    final_text = str(report.get("finalText") or "")
    try:
        parsed = nc.extract_json(final_text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"agent step finalText is not a single JSON object "
            f"({exc}); see {report_host}"
        ) from exc
    return parsed
