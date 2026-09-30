#!/usr/bin/env python3
"""Run local-noise generation using prebuilt, task-specific workspace subsets.

This launcher is intended for portable dataset bundles. Unlike
``run_noise_batch.py``, it never needs complete role raw workspaces: every task
must already have a matching subset under ``--subsets-root/<task-id>``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:
    from ._fs import read_json, write_json
except ImportError:
    from _fs import read_json, write_json


Json = Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-root", type=Path, required=True)
    parser.add_argument("--subsets-root", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--planner-provider-config", type=Path, required=True)
    parser.add_argument("--worker-provider-config", type=Path, required=True)
    parser.add_argument("--validator-provider-config", type=Path, required=True)
    parser.add_argument("--worker-fallback-provider-config", type=Path)
    parser.add_argument("--task-ids", nargs="*")
    parser.add_argument("--task-parallelism", type=int, default=1)
    parser.add_argument("--worker-parallelism", type=int, default=2)
    parser.add_argument("--max-versions", type=int, default=3)
    parser.add_argument("--max-rework-rounds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--planner-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--worker-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--validator-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Resume tasks whose pipeline_result is not passed.",
    )
    return parser.parse_args()


def _task_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


def task_ids(tasks_root: Path, requested: list[str] | None) -> list[str]:
    if requested:
        return [str(item) for item in requested]
    return sorted(
        (
            path.parent.name
            for path in tasks_root.glob("*/metadata.json")
            if path.parent.is_dir()
        ),
        key=_task_sort_key,
    )


def _assert_subset(subset_root: Path, task_id: str) -> Path:
    subset = subset_root / task_id
    required = ("subset_manifest.json", "source_path_map.json")
    if not subset.is_dir():
        raise FileNotFoundError(f"task {task_id}: subset directory not found: {subset}")
    missing = [name for name in required if not (subset / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"task {task_id}: subset missing required metadata: "
            + ", ".join(missing)
        )
    return subset


def command_for_task(
    args: argparse.Namespace,
    task_id: str,
) -> tuple[list[str], str]:
    task_dir = args.tasks_root / task_id
    if not (task_dir / "metadata.json").is_file():
        raise FileNotFoundError(f"task {task_id}: metadata.json not found")
    subset = _assert_subset(args.subsets_root, task_id)
    run_root = args.runs_root / task_id / "noise-run"
    agent_run = run_root / "agent_run"
    pipeline_result = agent_run / "pipeline_result.json"
    existing_status = None
    if pipeline_result.is_file():
        existing_status = read_json(pipeline_result).get("status")
    if (
        existing_status in {"passed", "failed"}
        and not args.fresh
        and not args.retry_failed
    ):
        return [], str(existing_status)

    python = sys.executable
    scripts = Path(__file__).resolve().parent
    common = [
        "--task-dir",
        str(task_dir),
        "--seed",
        str(args.seed + int(task_id) if task_id.isdigit() else args.seed),
        "--worker-parallelism",
        str(max(1, args.worker_parallelism)),
        "--max-rework-rounds",
        str(max(0, args.max_rework_rounds)),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
        "--validator-timeout-seconds",
        str(args.validator_timeout_seconds),
    ]
    if agent_run.joinpath("task_plan.json").is_file() and not args.fresh:
        command = [
            python,
            str(scripts / "resume_local_noise_pipeline.py"),
            "--subset-root",
            str(subset),
            "--agent-run-dir",
            str(agent_run),
            "--planner-provider-config",
            str(args.planner_provider_config),
            "--worker-provider-config",
            str(args.worker_provider_config),
            "--validator-provider-config",
            str(args.validator_provider_config),
            *(
                [
                    "--worker-fallback-provider-config",
                    str(args.worker_fallback_provider_config),
                ]
                if args.worker_fallback_provider_config
                else []
            ),
            *common,
        ]
        return command, "resume"

    command = [
        python,
        str(scripts / "run_local_noise_pipeline.py"),
        "--workspace-subset",
        str(subset),
        "--run-dir",
        str(run_root),
        "--provider-config",
        str(args.planner_provider_config),
        "--planner-provider-config",
        str(args.planner_provider_config),
        "--worker-provider-config",
        str(args.worker_provider_config),
        "--validator-provider-config",
        str(args.validator_provider_config),
        *(
            [
                "--worker-fallback-provider-config",
                str(args.worker_fallback_provider_config),
            ]
            if args.worker_fallback_provider_config
            else []
        ),
        "--max-versions",
        str(max(1, args.max_versions)),
        "--planner-timeout-seconds",
        str(args.planner_timeout_seconds),
        *common,
    ]
    return command, "fresh"


def run_one(args: argparse.Namespace, task_id: str) -> dict[str, Json]:
    started = time.time()
    try:
        command, mode = command_for_task(args, task_id)
        if mode in {"passed", "failed"}:
            return {
                "task_id": task_id,
                "mode": "skip",
                "status": mode,
                "duration_seconds": 0,
            }
        log_dir = args.runs_root / task_id / "noise-run" / "batch_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"{mode}.stdout.log"
        stderr_path = log_dir / f"{mode}.stderr.log"
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                command,
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
            )
        result_path = (
            args.runs_root
            / task_id
            / "noise-run"
            / "agent_run"
            / "pipeline_result.json"
        )
        result: dict[str, Json] = {}
        status = "error"
        if result_path.is_file():
            result = read_json(result_path)
            status = str(result.get("status") or "error")
        elif completed.returncode == 0:
            status = "unknown"
        return {
            "task_id": task_id,
            "mode": mode,
            "status": status,
            "returncode": completed.returncode,
            "duration_seconds": round(time.time() - started, 3),
            "pipeline_result": str(result_path),
            "failed_jobs": result.get("failed_jobs", {}),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        }
    except Exception as exc:
        return {
            "task_id": task_id,
            "mode": "setup",
            "status": "error",
            "duration_seconds": round(time.time() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    args = parse_args()
    args.tasks_root = args.tasks_root.resolve()
    args.subsets_root = args.subsets_root.resolve()
    args.runs_root = args.runs_root.resolve()
    selected = task_ids(args.tasks_root, args.task_ids)
    args.runs_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Json]] = []
    with ThreadPoolExecutor(
        max_workers=min(max(1, args.task_parallelism), max(1, len(selected)))
    ) as executor:
        futures = {
            executor.submit(run_one, args, task_id): task_id
            for task_id in selected
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"[{result['task_id']}] {result['status']} "
                f"mode={result['mode']} "
                f"duration={result['duration_seconds']}s",
                flush=True,
            )

    results.sort(key=lambda item: _task_sort_key(str(item["task_id"])))
    summary = {
        "schema_version": 1,
        "tasks_root": str(args.tasks_root),
        "subsets_root": str(args.subsets_root),
        "runs_root": str(args.runs_root),
        "results": results,
    }
    write_json(args.runs_root / "batch_result.json", summary)
    return 0 if all(item["status"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
