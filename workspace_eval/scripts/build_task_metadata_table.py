#!/usr/bin/env python3
"""把任务目录里的 ``metadata.json`` 汇总成一张扁平 CSV。

兄弟数据集（``Workspace-Bench`` / ``Workspace-Bench-Lite``）每份语料都带一张
``*_metadata_table.csv``，供筛选与分析。本脚本产出同款，但有两点不同：

1. **列是超集**。兄弟仓的 ``metadata.json`` 正好等于它们 CSV 的 11 列；本语料的
   ``metadata.json`` 还多 ``file_system`` / ``job`` / ``user_profit`` /
   ``input_file_summary``。其中 ``file_system`` 是 ``run_experiment.py`` 的必需
   字段，丢掉就没法从 CSV 重建可跑的任务，所以一并写入。
   标准 11 列排在最前，按老列序解析的消费者不受影响。
2. **编码带 BOM**（``utf-8-sig``）。读端 ``download_hf_assets.py`` 用的就是
   ``utf-8-sig``，Excel 直接打开也不会乱码。

用法：
    python3 workspace_eval/scripts/build_task_metadata_table.py \\
        --task-dir data/tasks/workspace-bench-hard \\
        --output data/tasks/task_hard_cn_metadata_table.csv

注意输出**不要写进任务目录内部**：上传时任务目录整体作为 ``task_hard_cn/``，
而 CSV 在数据集仓根，与兄弟仓布局一致。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from pathlib import Path

from typing import Any

#: 兄弟仓的标准列，顺序照抄，保证排在最前。
STANDARD_COLUMNS = (
    "absolute_id",
    "language",
    "persona",
    "task",
    "task_diff",
    "output_files",
    "rubrics",
    "rubric_types",
    "file_dep_graph",
    "data_manifest",
    "tested_capabilities",
)

#: 本语料多出来的列，接在标准列之后。
EXTRA_COLUMNS = (
    "file_system",
    "job",
    "user_profit",
    "input_file_summary",
)

COLUMNS = STANDARD_COLUMNS + EXTRA_COLUMNS

#: 这些键按 JSON 写进单元格（读端 ``_load_jsonish`` 会解析回来）。
JSON_KEYS = {
    "output_files",
    "rubrics",
    "rubric_types",
    "file_dep_graph",
    "data_manifest",
    "tested_capabilities",
    "input_file_summary",
}


def _ordered_task_dirs(task_dir: Path) -> list[Path]:
    """按任务号数值排序（``100`` 排在 ``94`` 之后，而不是字典序）。"""
    dirs = [p for p in task_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]

    def sort_key(p: Path) -> tuple[int, float | str]:
        try:
            return (0, int(p.name))
        except ValueError:
            return (1, p.name)

    return sorted(dirs, key=sort_key)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task-dir", required=True, help="含 <id>/metadata.json 的任务根目录")
    parser.add_argument("--output", required=True, help="产出的 CSV 路径")
    args = parser.parse_args()

    task_dir = Path(args.task_dir).resolve()
    if not task_dir.is_dir():
        raise SystemExit(f"task dir not found: {task_dir}")

    rows: list[dict[str, str]] = []
    missing: list[str] = []
    for child in _ordered_task_dirs(task_dir):
        meta_path = child / "metadata.json"
        if not meta_path.is_file():
            missing.append(child.name)
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        row: dict[str, str] = {}
        for column in COLUMNS:
            value = meta.get(column)
            if column in JSON_KEYS and value is None:
                # 缺失的 JSON 列写成空，而不是 "null"
                row[column] = ""
            else:
                row[column] = _cell(value)
        # absolute_id 一定写出来：CSV 靠它对齐任务号
        if not row.get("absolute_id"):
            row["absolute_id"] = child.name
        rows.append(row)

    if missing:
        print(
            f"跳过 {len(missing)} 个没有 metadata.json 的目录: {', '.join(missing[:5])}",
            file=sys.stderr,
        )
    if not rows:
        raise SystemExit(f"no tasks found under {task_dir}")

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    print(f"[ok] 写入 {len(rows)} 行 -> {output}")
    print(f"     列: {', '.join(COLUMNS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
