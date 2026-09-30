#!/usr/bin/env python3
"""svc_convert.publish_passed —— 把 judge+validation 都通过的任务发布到
evaluation/tasks_svc/<id>（供 viz Task Explorer 以 RIP_TASK_ROOT 浏览）。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import GEN_ROOT, ROLES, TASKS_SVC_ROOT, read_json  # noqa: E402


def passed() -> list[tuple[str, Path]]:
    out = []
    slug_set = {m["slug"] for m in ROLES.values()}
    for role_dir in sorted(GEN_ROOT.iterdir()):
        if role_dir.name not in slug_set or not role_dir.is_dir():
            continue  # 只扫角色目录；跳过 migrun 等
        for task_dir in sorted(role_dir.iterdir()):
            if not task_dir.is_dir() or not task_dir.name.isdigit():
                continue
            judge = task_dir / "judge.json"
            validation = task_dir / "validation.json"
            built = task_dir / "task"
            if not (built / "metadata.json").is_file():
                continue
            try:
                if (read_json(judge).get("status") != "passed"
                        or read_json(validation).get("status") != "passed"):
                    continue
            except Exception:
                continue
            out.append((task_dir.name, built))
    out.sort(key=lambda item: int(item[0]))
    return out


def main() -> int:
    items = passed()
    for task_id, src in items:
        dst = TASKS_SVC_ROOT / task_id
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    print(f"published {len(items)} passed tasks -> {TASKS_SVC_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
