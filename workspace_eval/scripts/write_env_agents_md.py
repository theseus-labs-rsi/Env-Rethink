#!/usr/bin/env python3
"""Write the task-level ``AGENTS.md`` that prescribes the environment-layer flow.

The generated environment layer (collection map + synthesized work history) is
delivered through the ``workspace_env`` MCP tools.  Tasks that ship the layer
also ship one ``AGENTS.md`` at the task root; at run time the runner both
materializes it into the agent's workspace root and prepends its text to the
task prompt (see ``agent_runner._task_agents_md_text``), so the prescribed
flow is in effect whether or not the harness auto-loads workspace docs.

This script is the single source of truth for that text and is idempotent:

    evaluation/.venv/bin/python evaluation/scripts/write_env_agents_md.py
    evaluation/.venv/bin/python evaluation/scripts/write_env_agents_md.py --check

``--check`` exits non-zero when a task's file is missing or stale (use it in
review before publishing a task set).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EVAL_ROOT.parent

DEFAULT_TASK_SETS = (
    EVAL_ROOT / "tasks_hard_v5_after",
    EVAL_ROOT / "tasks_hard_v5_after_v3",
    EVAL_ROOT / "tasks_hard_v5_after_smoke",
)
AGENTS_MD_NAME = "AGENTS.md"

AGENTS_MD = """# 环境层使用说明（任务级 AGENTS.md）

本工作区配套一个只读的「工作区环境层」，由两个数据面组成：

- **集合地图（collection map）**：把工作区文件组织成若干语义集合，每个集合有
  card_id、标题、简述与文件数；
- **工作历史（event log）**：合成的工作记录，记录文件/目录在何时被谁改过、
  产生过哪些版本。

两者通过 MCP 中的三个只读工具访问：`workspace_map`、`workspace_search`、
`event_search`。

## 规定流程

1. **先用集合地图定位文件。**
   先调用 `workspace_map` 拿到全部集合卡（只有 card_id、标题、简述与文件数，
   不含成员路径），据此判断哪些集合与本任务相关；再用 `workspace_search`
   展开：传 `card_id` 列出该集合的成员路径，传 `path` 精确定位某个文件名/路径，
   传 `query` 做关键词兜底检索（三者互斥，续页只传 `cursor`）。
2. **再用工作历史查询来龙去脉。**
   对关键文件、目录或主题调用 `event_search`，可按路径、关键词、操作类别或
   时间范围检索（`detail=true` 才返回公开摘录），用于判断版本沿革、当前版本、
   哪些是历史版本或被替代的文件、以及最近发生过什么。
3. **最后读具体文件并完成任务。**
   地图与历史只是索引和线索，**不能替代文件本身**；动手写交付物之前必须打开
   相关文件核对内容、口径与数值。

   **两者的分工（重要）**：
   - **版本沿革、哪一份是现行版本、哪些已被替代或归档，看历史。** 工作区里
     常有多份同名或近名的材料（草稿/修订/定稿/往届归档/外部参考），文件名本身
     不足以判断哪份该用；历史记录里「先核对了哪些、为什么转向另一份」正是判断
     依据。**不要只凭文件名里的「终版」「定稿」这类字样就认定它是现行版本。**
   - **具体内容与口径以文件本身为准。** 历史是推断出的合成记录，如果它与文件
     内容冲突，以文件内容为准。

## 使用约束

- 三个工具都是只读的，返回内容有 token 上限；同一页用 `cursor` 续页。
- 不要直接读取任务配置目录 `/workspace/strict/tasks/*/services/` 下的
  `fixture.json` 或 `blobs/`——那是服务端配置，不是任务资料。
- 地图/历史里提到的文件如果不在工作区内，说明它不属于本任务可见范围，
  不要凭地图描述臆造其内容。
- 找不到某个文件时，先回到集合地图确认它属于哪个集合、是否在本任务工作区内，
  再用 `event_search` 看它是否被移动或改名过。
"""


def task_dirs(task_set: Path) -> list[Path]:
    return sorted(
        path
        for path in task_set.iterdir()
        if path.is_dir() and (path / "metadata.json").is_file()
    )


def run(task_sets: list[Path], *, check: bool) -> int:
    stale: list[str] = []
    written = 0
    for task_set in task_sets:
        if not task_set.is_dir():
            raise SystemExit(f"task set not found: {task_set}")
        targets = task_dirs(task_set)
        if not targets:
            raise SystemExit(f"task set has no task directories: {task_set}")
        for task_dir in targets:
            path = task_dir / AGENTS_MD_NAME
            current = path.read_text(encoding="utf-8") if path.is_file() else None
            if current == AGENTS_MD:
                continue
            if check:
                stale.append(str(path))
                continue
            path.write_text(AGENTS_MD, encoding="utf-8")
            written += 1
    if check:
        if stale:
            print(f"{len(stale)} task(s) have a missing/stale {AGENTS_MD_NAME}:")
            for item in stale:
                print(f"  {item}")
            return 1
        print(f"all {AGENTS_MD_NAME} files up to date")
        return 0
    print(f"{AGENTS_MD_NAME} written for {written} task(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task-set",
        action="append",
        default=None,
        help="task-set directory (repeatable); defaults to the envgen after sets",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report missing/stale files instead of writing them",
    )
    args = parser.parse_args()
    task_sets = (
        [Path(item).resolve() for item in args.task_set]
        if args.task_set
        else list(DEFAULT_TASK_SETS)
    )
    return run(task_sets, check=args.check)


if __name__ == "__main__":
    sys.exit(main())
