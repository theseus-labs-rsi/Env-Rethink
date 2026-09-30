#!/usr/bin/env python3
"""workspace_env 生成进度的统一统计口径。

三个数字，只算这三个：

    有集合地图的题数       cover/<题>/final/workspace-collection-set.public.json 存在
    有事件日志的题数       history/<题>/final/events.public.jsonl 存在
    两者都有的题数

口径说明（避免歧义）：

* 统计对象是**磁盘上的生成状态**，不是交付快照。交付（``tasks_hard_v5_after_v3``）
  是 run_task 走完那一刻的快照，会过时；生成状态才是实时的。
* 「有」= 该阶段的**终态产物**存在。中间产物不算。
* 两个 work_root 都算（v3g 主运行、v3h 失败题重跑），同一题在任一处有即算「有」。

用法::

    python3 scripts/envgen_status.py
"""

from __future__ import annotations

import os
import sys

from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parents[1]
TASK_DIR = EVAL_ROOT / "tasks_hard_v5"
WORK_ROOTS = (
    Path("/tmp/envgen-work-v5-v3g"),
    Path("/tmp/envgen-work-v5-v3h"),
)
MAP_ARTIFACT = ("cover", "final", "workspace-collection-set.public.json")
LOG_ARTIFACT = ("history", "final", "events.public.jsonl")


def task_ids() -> list[str]:
    return sorted(d.name for d in TASK_DIR.iterdir() if d.is_dir())


def has_artifact(task_id: str, parts: tuple[str, ...]) -> bool:
    return any((root.joinpath(*parts[:1], task_id, *parts[1:])).is_file() for root in WORK_ROOTS)


def main() -> int:
    tasks = task_ids()
    with_map = [t for t in tasks if has_artifact(t, MAP_ARTIFACT)]
    with_log = [t for t in tasks if has_artifact(t, LOG_ARTIFACT)]
    both = [t for t in tasks if t in set(with_map) & set(with_log)]
    total = len(tasks)

    print(f"题集: {TASK_DIR.name}（{total} 题）")
    print(f"  work_root: {', '.join(str(r) for r in WORK_ROOTS)}")
    print()
    print(f"  有集合地图的题数   {len(with_map):>3} / {total}")
    print(f"  有事件日志的题数   {len(with_log):>3} / {total}")
    print(f"  两者都有的题数     {len(both):>3} / {total}")
    print()
    print(f"  有地图无日志: {[t for t in with_map if t not in set(with_log)]}")
    print(f"  有日志无地图: {[t for t in with_log if t not in set(with_map)]}  (应为空)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
