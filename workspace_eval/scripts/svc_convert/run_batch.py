#!/usr/bin/env python3
"""svc_convert.run_batch —— 批量并发跑 svc_convert 各阶段（不跑 远程后端 e2e）。

用法示例（宿主并发；迁移用 luna，含 rework）：
  cd evaluation
  # 先把 远程后端 迁移产出的 conversion_design.json 放进各任务 run_root/design/
  python scripts/svc_convert/run_batch.py --ids 258,269,281 --phase migrate \
      --model api_azure_openai_gpt-5.6-luna --max-rework 2 --parallel 3
  # migrate 全过的再并发内容 judge：
  python scripts/svc_convert/run_batch.py --ids 258,269,281 --phase judge --parallel 3

每个任务独立 run_task/judge_gate 子进程，日志写到任务 run_root/batch_<phase>.log；
结果汇总打印并写 run_root/batch_<phase>_report.json。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    EVAL_ROOT,
    load_lite_metadata,
    role_workspace_root,
    role_of,
    task_run_root,
    write_json,
)


def _python() -> str:
    return str(EVAL_ROOT / ".venv" / "bin" / "python")


def _runner() -> Path:
    return EVAL_ROOT / "scripts" / "svc_convert"


def run_one(task_id: str, phase: str, model: str, max_rework: int, role_ws: Path, out: Path) -> dict:
    if phase == "full":
        cmd = [ _python(), str(_runner() / "run_task.py"), "--task-id", task_id,
                "--stage", "full", "--model", model, "--max-rounds", str(max(1, max_rework)),
                "--role-workspace", str(role_ws) ]
    else:
        cmd = [ _python(), str(_runner() / "run_task.py"), "--task-id", task_id,
                "--stage", "migrate", "--model", model, "--max-rework", str(max_rework),
                "--role-workspace", str(role_ws) ]
    log = out.parent / f"batch_{phase}.log"
    with log.open("w", encoding="utf-8") as fh:
        result = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)
    return {"task_id": task_id, "phase": phase, "rc": result.returncode,
            "passed": result.returncode == 0}


def run_judge(task_id: str, out: Path) -> dict:
    cmd = [ _python(), str(_runner() / "judge_gate.py"), "--task-id", task_id ]
    log = out.parent / "batch_judge.log"
    with log.open("w", encoding="utf-8") as fh:
        result = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)
    return {"task_id": task_id, "phase": "judge", "rc": result.returncode,
            "passed": result.returncode == 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", required=True)
    parser.add_argument("--phase", choices=["migrate", "judge", "full"], required=True)
    parser.add_argument("--model", default="api_azure_openai_gpt-5.6-luna")
    parser.add_argument("--max-rework", type=int, default=2)
    parser.add_argument("--parallel", type=int, default=3)
    args = parser.parse_args()
    ids = [x.strip() for x in args.ids.split(",") if x.strip()]

    def fn(task_id: str):
        meta = load_lite_metadata(task_id)
        run_root = task_run_root(role_of(meta), task_id)
        run_root.mkdir(parents=True, exist_ok=True)
        if args.phase == "judge":
            return run_judge(task_id, run_root)
        role_ws = role_workspace_root(role_of(meta))
        return run_one(task_id, args.phase, args.model, args.max_rework, role_ws, run_root)

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = {pool.submit(fn, tid): tid for tid in ids}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"task_id": tid, "phase": args.phase,
                                "rc": 1, "error": f"{type(exc).__name__}: {exc}"})
    results.sort(key=lambda r: r["task_id"])
    for r in results:
        print(f"[{r.get('phase')}] task{r['task_id']} -> {'ok' if r.get('rc') == 0 else 'FAIL'}"
              + (f" ({r['error']})" if r.get("error") else ""))
    write_json(EVAL_ROOT / ".generated" / "svc_convert" / f"batch_{args.phase}_report.json", results)
    return 0 if all(r.get("rc") == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
