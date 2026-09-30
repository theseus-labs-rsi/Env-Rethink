#!/usr/bin/env python3
"""svc_convert.run_task —— 单个任务的转换状态机编排。

stage:
  design    -> 跑 design worker，产出 design/conversion_design.json
  build     -> build_fixture 物化到 run_root/task/
  validate  -> contract.run_checks 确定性门，写 run_root/validation.json
  publish   -> (显式) 把验收通过任务拷贝到 evaluation/tasks_svc/<id>/

产物目录：evaluation/.generated/svc_convert/<role_slug>/<task_id>/
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    TASKS_SVC_ROOT,
    load_lite_metadata,
    read_json,
    role_of,
    task_run_root,
    write_json,
)
from build_fixture import build_task  # noqa: E402
from contract import run_checks  # noqa: E402
from design_agent import build_input_profiles, run_design  # noqa: E402
from judge_gate import run_judge  # noqa: E402


def ensure_task_dir(task_id: str) -> Path:
    meta = load_lite_metadata(task_id)
    role = role_of(meta)
    return task_run_root(role, task_id)


def stage_design(task_id: str, model: str, timeout: int) -> Path:
    run_root = ensure_task_dir(task_id)
    design_path = run_root / "design" / "conversion_design.json"
    if design_path.is_file():
        print(f"[skip] design exists: {design_path}")
        return design_path
    return run_design(task_id, model=model, timeout=timeout)


def stage_build(task_id: str) -> Path:
    run_root = ensure_task_dir(task_id)
    design_path = run_root / "design" / "conversion_design.json"
    if not design_path.is_file():
        raise SystemExit(f"design missing: {design_path}; run design first")
    design = read_json(design_path)
    task_dir = run_root / "task"
    if task_dir.exists():
        shutil.rmtree(task_dir)
    original_metadata_path = run_root / "task_metadata.json"
    if not original_metadata_path.is_file():
        # 沙盒迁移路径不会经过本地 run_design，这里补 scaffold
        write_json(original_metadata_path, load_lite_metadata(task_id))
    original_metadata = read_json(original_metadata_path)
    build_task(
        design=design,
        run_root=run_root,
        task_dir=task_dir,
        original_metadata=original_metadata,
    )
    # 防泄漏硬字段：full_move 的 input_remove_paths 用宿主确定性扫描的角色工作区
    # 同内容副本覆盖（沙盒/LLM 产出的该字段不可信，常有 data/<hash> 相对路径错误）。
    if str(design.get("archetype")) == "full_move":
        profiles = build_input_profiles(original_metadata)
        seen: set[str] = set()
        removes: list[str] = []
        for duplicates in (
            profiles.get("duplicates_in_role_workspace") or {}
        ).values():
            for dup in duplicates or []:
                if dup not in seen:
                    seen.add(dup)
                    removes.append(str(dup))
        meta = read_json(task_dir / "metadata.json")
        meta["input_remove_paths"] = removes
        write_json(task_dir / "metadata.json", meta)
    print(f"[ok] build -> {task_dir}")
    return task_dir


def stage_validate(task_id: str, role_workspace: str | None = None) -> int:
    run_root = ensure_task_dir(task_id)
    task_dir = run_root / "task"
    if not (task_dir / "metadata.json").is_file():
        raise SystemExit(f"task dir not built: {task_dir}")
    workspace_root = Path(role_workspace) if role_workspace else None
    result = run_checks(task_dir, role_workspace_root=workspace_root)
    write_json(run_root / "validation.json", result)
    for check in result["checks"]:
        marker = "ok  " if check["passed"] else "FAIL"
        print(f"[{marker}] {check['name']}")
        for problem in check.get("problems", []):
            print(f"        - {problem}")
    print(f"status: {result['status']}")
    return 0 if result["status"] == "passed" else 1


def feedback_from_validation(result: dict, *, limit: int = 12) -> str:
    """把确定性门失败汇总成给 design worker 的简明修正要求。"""
    lines = []
    for check in result.get("checks", []):
        problems = check.get("problems") or []
        if not problems:
            continue
        lines.append(f"- {check['name']}:")
        for problem in problems[:limit]:
            lines.append(f"    {problem}")
        if len(problems) > limit:
            lines.append(f"    ...另有 {len(problems) - limit} 条")
    return "\n".join(lines)


def run_migrate_rework(
    task_id: str,
    *,
    model: str = "api_deepseek_deepseek-v4-pro",
    design_timeout: int = 1800,
    role_workspace: str | None = None,
    max_rework: int = 2,
) -> int:
    """design → build → validate；validate 失败则带 feedback 重出 design，最多 max_rework 轮。"""
    run_root = ensure_task_dir(task_id)
    task_dir = run_root / "task"
    for attempt in range(max_rework + 1):
        if attempt == 0 and not (run_root / "design" / "conversion_design.json").is_file():
            print(f"[design] round={attempt} 首轮生成")
            run_design(task_id, model=model, timeout=design_timeout)
        elif attempt > 0:
            feedback = ""
            if (run_root / "validation.json").is_file():
                feedback = feedback_from_validation(read_json(run_root / "validation.json"))
            print(f"[design] round={attempt} rework")
            # 归档上一版
            history = run_root / "design"
            if (history / "conversion_design.json").is_file():
                import shutil as _shutil

                _shutil.copy2(
                    history / "conversion_design.json",
                    history / f"round_{attempt - 1}.json",
                )
            run_design(task_id, model=model, timeout=design_timeout, feedback=feedback or None)
        stage_build(task_id)
        print(f"[validate] round={attempt}")
        code = stage_validate(task_id, role_workspace=role_workspace)
        if code == 0:
            return 0
    print(f"[rework] exhausted {max_rework} rounds without passing gate")
    return 1


def feedback_from_judge(verdict: dict) -> str:
    lines = []
    for dim in verdict.get("dimensions", []):
        if not dim.get("passed"):
            problems = dim.get("problems") or []
            lines.append(f"- {dim.get('name')}: {'; '.join(str(p) for p in problems[:6])}")
    for issue in verdict.get("blocking_issues", []):
        lines.append(f"- blocking: {issue.get('issue')}")
        for change in issue.get("requested_changes", [])[:4]:
            lines.append(f"    change: {change}")
    return "\n".join(lines)


def run_full(
    task_id: str,
    *,
    model: str = "api_azure_openai_gpt-5.6-luna",
    design_timeout: int = 1800,
    role_workspace: str | None = None,
    max_rounds: int = 3,
) -> int:
    """full：确定性门通过后跑内容 judge；judge 非 passed → 把 judge 反馈喂回
    design 重出（含 validation.json 已保证机械层），build→validate→judge 循环。"""
    run_root = ensure_task_dir(task_id)
    task_dir = run_root / "task"
    feedback = ""
    for attempt in range(max_rounds):
        if attempt == 0 and not (run_root / "design" / "conversion_design.json").is_file():
            print(f"[full] round={attempt} 首轮生成")
            run_design(task_id, model=model, timeout=design_timeout)
        elif attempt > 0:
            print(f"[full] round={attempt} rework（feedback from "
                  f"{'judge' if feedback.startswith('- ') or feedback.startswith('- blocking') else 'static'}）")
            history = run_root / "design"
            if (history / "conversion_design.json").is_file():
                import shutil as _shutil

                _shutil.copy2(
                    history / "conversion_design.json",
                    history / f"round_{attempt - 1}.json",
                )
            run_design(task_id, model=model, timeout=design_timeout, feedback=feedback or None)
        stage_build(task_id)
        print(f"[validate] round={attempt}")
        if stage_validate(task_id, role_workspace=role_workspace) != 0:
            feedback = feedback_from_validation(read_json(run_root / "validation.json"))
            continue
        print(f"[judge] round={attempt}")
        try:
            run_judge(task_id, timeout=design_timeout)
        except Exception as exc:  # noqa: BLE001
            verdict: dict = {"status": "failed",
                             "error": f"{type(exc).__name__}: {exc}"}
            write_json(run_root / "judge.json", verdict)
        else:
            verdict = read_json(run_root / "judge.json")
        if verdict.get("status") == "passed":
            return 0
        feedback = feedback_from_judge(verdict) or (
            f"judge 未给出可行动反馈，请重审以下任务：{verdict.get('error') or ''}")
    print(f"[full] exhausted {max_rounds} rounds")
    return 1


def stage_publish(task_id: str) -> Path:
    run_root = ensure_task_dir(task_id)
    src = run_root / "task"
    if not (src / "metadata.json").is_file():
        raise SystemExit(f"task dir not built: {src}")
    dst = TASKS_SVC_ROOT / str(task_id)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    print(f"[ok] published -> {dst}")
    return dst


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--stage",
        choices=["design", "build", "validate", "publish", "all", "migrate", "full"],
        default="all",
    )
    parser.add_argument("--model", default="api_azure_openai_gpt-5.6-luna")
    parser.add_argument("--design-timeout", type=int, default=1800)
    parser.add_argument("--max-rework", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--role-workspace", default=None,
                        help="角色工作区根（跑防泄漏模拟快照用）")
    args = parser.parse_args()
    task_id = str(args.task_id)

    if args.stage == "full":
        return run_full(
            task_id,
            model=args.model,
            design_timeout=args.design_timeout,
            role_workspace=args.role_workspace,
            max_rounds=args.max_rounds,
        )

    if args.stage in ("all", "migrate"):
        return run_migrate_rework(
            task_id,
            model=args.model,
            design_timeout=args.design_timeout,
            role_workspace=args.role_workspace,
            max_rework=args.max_rework,
        )

    if args.stage == "design":
        stage_design(task_id, model=args.model, timeout=args.design_timeout)
        return 0
    if args.stage == "build":
        stage_build(task_id)
        return 0
    if args.stage == "validate":
        return stage_validate(task_id, role_workspace=args.role_workspace)
    if args.stage == "publish":
        stage_publish(task_id)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
