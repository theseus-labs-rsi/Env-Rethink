#!/usr/bin/env python3
"""score_construction_quality.py — 用 gt 作真值，给每个构造器的选集打分。

口径（每个任务一个混淆矩阵）：
    gt 判正本 = task metadata 里 input_role == "standard"
    gt 判噪声 = 其余
    构造器选集 = preprocessed/<curator>/<task>/curation.json 里 selected == true

    tp = 选集 ∩ 正本     (正确保留)
    fn = 正本 \ 选集     (误杀正本, 越少越好)
    fp = 选集 \ 正本     (漏放噪声, 越少越好)
    tn = 噪声 \ 选集     (正确滤除)
    partition = (tp + tn) / (tp + tn + fp + fn)

注意 gt 构造器本身就是真值选集，它的 partition 恒为 100%。

用法:
    python3 scripts/score_construction_quality.py [--curators env-rethink,qwen,gt] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
# curate_workspace 属于模块 ④（curate/），不在本目录下
CURATE = Path(os.environ.get("CURATE_ROOT") or (EVAL.parent / "curate"))
sys.path.insert(0, str(CURATE))  # noqa: E402

from curate_workspace import PREPROCESSED_ROOT, TASK_ROOT, norm_path  # noqa: E402


def load_gt(task_id: str) -> dict[str, bool]:
    """返回 {norm_path: is_standard}，真值来自任务 metadata 的 input_role。"""
    meta = json.loads((TASK_ROOT / task_id / "metadata.json").read_text(encoding="utf-8"))
    return {
        norm_path(e["target_path"]): (e.get("input_role") == "standard")
        for e in meta["data_manifest"]
    }


def load_selection(curator: str, task_id: str) -> dict[str, bool]:
    f = PREPROCESSED_ROOT / curator / task_id / "curation.json"
    if not f.is_file():
        return {}
    d = json.loads(f.read_text(encoding="utf-8"))
    return {norm_path(x["path"]): bool(x["selected"]) for x in d["files"]}


def score_task(curator: str, task_id: str) -> dict | None:
    gt = load_gt(task_id)
    sel = load_selection(curator, task_id)
    if not sel:
        return None
    tp = fn = fp = tn = 0
    for p, is_std in gt.items():
        picked = sel.get(p, False)
        if is_std and picked:
            tp += 1
        elif is_std and not picked:
            fn += 1
        elif not is_std and picked:
            fp += 1
        else:
            tn += 1
    n = tp + fn + fp + tn
    return {
        "task": task_id, "n_files": n,
        "n_gt_standard": tp + fn, "n_gt_noise": fp + tn,
        "n_selected": tp + fp,
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "missed_standard": fn,      # 误杀正本
        "leaked_noise": fp,         # 漏放噪声
        "partition": round((tp + tn) / n, 4) if n else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--curators", default="env-rethink,qwen,gt")
    ap.add_argument("--tasks", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    curators = [c.strip() for c in args.curators.split(",") if c.strip()]
    # 不给 --tasks 时，用第一个 curator 的产物目录反推任务清单
    tasks = ([t.strip() for t in args.tasks.split(",") if t.strip()]
             if args.tasks else sorted(p.name for p in
                                       PREPROCESSED_ROOT.glob(f"{curators[0]}/[0-9]*")))

    out: dict[str, dict] = {}
    print(f"{'curator':>13} {'task':>5} {'files':>6} {'gt_std':>7} {'sel':>5} "
          f"{'误杀std':>8} {'漏放noise':>10} {'partition':>10}")
    for c in curators:
        T = dict(tp=0, fn=0, fp=0, tn=0, n=0, sel=0, std=0, tasks=0)
        rows = []
        for t in tasks:
            r = score_task(c, t)
            if r is None:
                continue
            rows.append(r)
            for k in ("tp", "fn", "fp", "tn"):
                T[k] += r[k]
            T["n"] += r["n_files"]
            T["sel"] += r["n_selected"]
            T["std"] += r["n_gt_standard"]
            T["tasks"] += 1
            print(f"{c:>7} {t:>5} {r['n_files']:>6} {r['n_gt_standard']:>7} "
                  f"{r['n_selected']:>5} {r['fn']:>8} {r['fp']:>10} "
                  f"{r['partition']*100:>9.1f}%")
        if T["tasks"]:
            part = (T["tp"] + T["tn"]) / T["n"]
            print(f"{c:>7} {'ALL':>5} {T['n']:>6} {T['std']:>7} {T['sel']:>5} "
                  f"{T['fn']:>8} {T['fp']:>10} {part*100:>9.1f}%")
            out[c] = {
                "tasks": T["tasks"], "n_files": T["n"], "n_gt_standard": T["std"],
                "n_selected": T["sel"], "tp": T["tp"], "fn": T["fn"],
                "fp": T["fp"], "tn": T["tn"],
                "missed_standard_pct": round(T["fn"] / T["std"] * 100, 1) if T["std"] else 0.0,
                "leaked_noise_pct": round(T["fp"] / (T["n"] - T["std"]) * 100, 1)
                                    if T["n"] > T["std"] else 0.0,
                "partition_pct": round(part * 100, 1),
                "per_task": rows,
            }
        print()

    if args.json:
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"写到 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
