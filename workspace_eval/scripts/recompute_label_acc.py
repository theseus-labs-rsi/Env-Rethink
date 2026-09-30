#!/usr/bin/env python3
"""recompute_label_acc.py — 用统一分母重算 ca / base 的标注准确率。

修掉旧报告（gen_label_acc_report.py）的两个口径问题：
  1. **分母不一致**：base 未输出 category 的文件被从分母剔除，ca 没有 —— 等于 base
     只在自己答得出来的子集上计分。本脚本对每个 (curator, 指标) 用**同一分母**，
     未输出记为错。
  2. GT 可切换：默认用 `gt_taxonomy_30task_v3.json`（预留组已修），可 `--gt` 指回旧文件
     做对照。

用法：
    python scripts/recompute_label_acc.py
    python scripts/recompute_label_acc.py --gt experiments/noise-id/noise_taxonomy_v2_final_30task.json
    python scripts/recompute_label_acc.py --exclude-tasks 124
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

EVAL = pathlib.Path(__file__).resolve().parents[1]
PRE = EVAL / ".generated" / "preprocessed"
DEFAULT_GT = EVAL / "experiments" / "noise-id" / "gt_taxonomy_30task_v3.json"

TRAIN15 = [374, 357, 372, 154, 314, 258, 108, 291, 160, 207, 267, 288, 129, 94, 334]
HELD15 = [72, 75, 78, 100, 146, 159, 266, 85, 171, 79, 87, 124, 161, 300, 359]


def load_predictions(curator: str, tasks: set[str]) -> dict:
    out = {}
    for t in sorted(tasks, key=int):
        p = PRE / curator / str(t) / "curation.json"
        if not p.exists():
            continue
        for f in json.loads(p.read_text(encoding="utf-8")).get("files", []):
            out[(str(t), f["path"])] = f
    return out


def score(gt_files: list, tasks: set[str], curator: str) -> dict | None:
    pred = load_predictions(curator, tasks)
    keys = [k for k in gt_files if k[0] in tasks and k in pred]
    if not keys:
        return None
    n_files = len(keys)
    noise = [k for k in keys if gt_files[k]["partition"] == "noise"]
    staged = [k for k in keys if gt_files[k].get("stage") not in (None, "null")]
    res = {}
    for name, subset, field, gtfield in (
        ("partition", keys, "pred_partition", "partition"),
        ("category", noise, "pred_category", "category"),
        ("stage", staged, "pred_stage", "stage"),
    ):
        ok = sum(1 for k in subset if pred[k].get(field) == gt_files[k][gtfield])
        res[name] = (ok, len(subset))
    full = sum(
        1 for k in keys
        if pred[k].get("pred_partition") == gt_files[k]["partition"]
        and pred[k].get("pred_category") == gt_files[k]["category"]
        and pred[k].get("pred_stage") == gt_files[k]["stage"]
    )
    res["full"] = (full, n_files)
    return res


def fmt(v) -> str:
    ok, n = v
    return f"{ok}/{n} ({ok / n * 100:.1f}%)" if n else "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", default=str(DEFAULT_GT))
    ap.add_argument("--curators", default="env-rethink,qwen")
    ap.add_argument("--exclude-tasks", default="")
    args = ap.parse_args()

    gt = json.loads(pathlib.Path(args.gt).read_text(encoding="utf-8"))
    gt_files = {(str(r["task"]), r["path"]): r for r in gt["files"]}
    excl = {t.strip() for t in args.exclude_tasks.split(",") if t.strip()}
    groups = {
        "训练池15": {str(t) for t in TRAIN15},
        "预留15": {str(t) for t in HELD15 if str(t) not in excl},
    }
    if excl:
        print(f"（已排除任务：{', '.join(sorted(excl, key=int))}）")
    print(f"GT: {pathlib.Path(args.gt).name}\n")

    hdr = f"{'组':<10} {'构造器':<7} " + " ".join(f"{k:>22}" for k in
                                                  ("partition", "category", "stage", "full"))
    print(hdr)
    print("-" * len(hdr))
    for gname, tasks in groups.items():
        for cur in args.curators.split(","):
            r = score(gt_files, tasks, cur)
            if not r:
                print(f"{gname:<10} {cur:<7} 无数据")
                continue
            print(f"{gname:<10} {cur:<7} " + " ".join(f"{fmt(r[k]):>22}"
                                                      for k in ("partition", "category", "stage", "full")))
        print()

    # 逐任务（仅预留组）
    print("=== 预留组逐任务 category ===")
    for t in HELD15:
        if str(t) in excl:
            continue
        row = []
        for cur in args.curators.split(","):
            r = score(gt_files, {str(t)}, cur)
            row.append(f"{cur}={fmt(r['category']) if r else '—'}")
        print(f"  task {t:>4}: " + "  ".join(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
