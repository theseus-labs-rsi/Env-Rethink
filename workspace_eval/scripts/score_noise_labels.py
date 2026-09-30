#!/usr/bin/env python3
"""score_noise_labels.py — 对 noise-id 评估交付的 noise_labels.json 做标签级判分。

两种口径：

子环境口径（默认，3.2.1 评估）:
以 .generated/noise_id_subenvs/<task_id>/labels.json 为真值,对实验目录里
cases/task<id>/agent/output/noise_labels.json 逐文件比对:

  partition   standard→standard;noise→noise
  category    仅对 GT noise 文件要求一致(unrelated/superseded/fabricated_authority/
              hijack_final/redirect;GT standard 恒为 canonical)
  stage       GT stage 非 null 时要求一致
  full        partition+category(+(stage 非空时))全对

同时复算 r2 波次的合格判定(usable 规则):
  完全正确文件 ≥3、至少含 1 个 standard、强类别(hijack_final/fabricated_authority/
  redirect)存在时至少对 1 个。

父任务口径(--parent,3.2.2 构造式 workspace 的构造质量):
以 tasks_hard_v4/<id>/metadata.json 的 input_role 为真值(partition)、
noise_taxonomy_v2_final.json 为类别真值,对 curate_workspace.py 产出的
preprocessed/<curator>/<task>/curation.json 的选集判分:

  误杀(killed_std)   GT standard 未入选(逐文件列出)
  漏放(leaked_noise)  GT noise 入选(逐文件列出)
  选集准确率/规模、partition 准确率
  训练内/未见分层(subenv labels path 并集,设计 §5.1)

用法:
  # 子环境口径
  python3 score_noise_labels.py --gt-root <subenvs> \
      --exp sftv2t=<实验目录> [--exp base=<实验目录>] [--out report.json]
  # 父任务口径
  python3 score_noise_labels.py --parent \
      --curated env-rethink=<preprocessed/env-rethink> [qwen=<...>] [rule=<...>] \
      [--task-root tasks_hard_v4] \
      [--taxonomy experiments/noise-id/noise_taxonomy_v2_final.json] \
      [--subenv-root .generated/noise_id_subenvs] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STRONG = {"hijack_final", "fabricated_authority", "redirect"}
NOISE_CATS = {"unrelated", "superseded", "fabricated_authority", "hijack_final", "redirect"}


def norm_path(p: str) -> str:
    p = str(p or "").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def load_truth(gt_root: Path, task_id: str):
    d = load_json(gt_root / task_id / "labels.json")
    return {norm_path(f["path"]): f for f in d.get("files") or []}


def load_pred(exp_dir: Path, task_id: str):
    """返回 (pred dict or None, 缺失原因)."""
    cand = exp_dir / "cases" / f"task{task_id}" / "agent" / "output" / "noise_labels.json"
    if not cand.is_file():
        return None, f"missing {cand.name}"
    try:
        d = load_json(cand)
    except Exception as e:  # noqa: BLE001
        return None, f"bad json: {e}"
    files = d.get("files") or []
    if not isinstance(files, list) or not files:
        return None, "no files[]"
    pred = {}
    for f in files:
        if isinstance(f, dict) and f.get("path"):
            pred[norm_path(f["path"])] = f
    return pred, None


def stage_of(rec):
    v = rec.get("stage")
    if v is None:
        return None
    v = str(v).strip()
    if v.lower() in ("", "null", "none"):
        return None
    return v


def score_case(truth: dict, pred: dict | None):
    """单 case 计分。返回指标 dict。"""
    m = {
        "gt_files": len(truth),
        "covered": 0,
        "part_ok": 0,
        "noise_n": 0,
        "noise_part_ok": 0,
        "cat_ok": 0,
        "stage_n": 0,
        "stage_ok": 0,
        "full_ok": 0,
        "misses": [],
        "wrong": [],
        "extra_n": 0,
        "extra_all_noise": True,
        "qualified": False,
    }
    if pred is None:
        return m
    strong_needed = {
        p for p, f in truth.items()
        if f.get("partition") == "noise" and f.get("category") in STRONG
    }
    strong_hit = False
    full_paths = set()
    for p, gt in truth.items():
        pr = pred.get(p)
        if pr is None:
            m["misses"].append(p)
            continue
        m["covered"] += 1
        g_part = gt.get("partition")
        p_part = pr.get("partition")
        part_ok = g_part == p_part
        if part_ok:
            m["part_ok"] += 1
        if g_part == "noise":
            m["noise_n"] += 1
            if part_ok:
                m["noise_part_ok"] += 1
            if p_part == "noise" and pr.get("category") == gt.get("category"):
                m["cat_ok"] += 1
                if p in strong_needed:
                    strong_hit = True
        g_stage, p_stage = stage_of(gt), stage_of(pr)
        if g_stage is not None:
            m["stage_n"] += 1
            if p_stage == g_stage:
                m["stage_ok"] += 1
        full = part_ok and (
            g_part != "noise" or pr.get("category") == gt.get("category")
        ) and (g_stage is None or stage_of(pr) == g_stage)
        if full:
            m["full_ok"] += 1
            full_paths.add(p)
        elif part_ok or pr.get("path"):
            m["wrong"].append({
                "path": p,
                "gt": {"partition": g_part, "category": gt.get("category"),
                       "stage": gt.get("stage")},
                "pred": {"partition": p_part, "category": pr.get("category"),
                         "stage": pr.get("stage")},
            })
    has_std_ok = any(
        p in full_paths and truth[p].get("partition") == "standard" for p in truth
    )
    m["qualified"] = (
        len(full_paths) >= 3
        and has_std_ok
        and (not strong_needed or strong_hit)
    )
    extra = [p for p in pred if p not in truth]
    m["extra_n"] = len(extra)
    m["extra_all_noise"] = all(
        pred[p].get("partition") == "noise" for p in extra
    ) if extra else True
    return m


def task_ids_of(exp_dir: Path):
    sm = exp_dir / "summary.json"
    if sm.is_file():
        return [r["task_id"] for r in load_json(sm)]
    return sorted(
        p.name[4:] for p in (exp_dir / "cases").glob("task*") if p.is_dir()
    )


# ---------------------------------------------------------------- 父任务口径

def load_taxonomy(path: Path) -> dict[tuple[str, str], dict]:
    """(task_id, norm_path) -> {partition, category, stage}。"""
    if not path.is_file():
        return {}
    tax = load_json(path)
    out = {}
    for f in tax.get("files") or []:
        key = (str(f.get("task")), norm_path(f.get("path", "")))
        out[key] = {
            "partition": f.get("partition"),
            "category": f.get("category"),
            "stage": f.get("stage"),
        }
    return out


def load_seen_paths(subenv_root: Path, task_id: str) -> set[str]:
    """该父任务在训练子环境中出现过的文件路径并集（训练内/未见分层）。"""
    seen: set[str] = set()
    for d in subenv_root.glob(f"{task_id}-*"):
        lp = d / "labels.json"
        if not lp.is_file():
            continue
        for f in load_json(lp).get("files") or []:
            if isinstance(f, dict) and f.get("path"):
                seen.add(norm_path(f["path"]))
    return seen


def score_parent_case(truth: dict, files: list[dict]) -> dict:
    """单 (curator, task) 构造质量计分。

    truth: norm_path -> {"partition", "category", "stage"}
    files: curation.json 的 files[](selected 标记)
    """
    m = {
        "gt_files": len(truth),
        "gt_std": 0,
        "gt_noise": 0,
        "n_selected": 0,
        "killed_std": [],
        "leaked_noise": [],
        "part_ok": 0,
        "missing_curation_rows": 0,
    }
    selected_by_path = {}
    for f in files:
        if isinstance(f, dict) and f.get("path"):
            selected_by_path[norm_path(f["path"])] = bool(f.get("selected"))
    for p, gt in truth.items():
        g_part = gt.get("partition")
        if g_part == "standard":
            m["gt_std"] += 1
        else:
            m["gt_noise"] += 1
        if p not in selected_by_path:
            m["missing_curation_rows"] += 1
        selected = selected_by_path.get(p, False)
        if selected:
            m["n_selected"] += 1
        if g_part == "standard":
            if selected:
                m["part_ok"] += 1
            else:
                m["killed_std"].append(p)
        else:
            if selected:
                m["leaked_noise"].append(p)
            else:
                m["part_ok"] += 1
    m["miskill_rate"] = (
        len(m["killed_std"]) / m["gt_std"] if m["gt_std"] else None
    )
    m["leak_rate"] = (
        len(m["leaked_noise"]) / m["gt_noise"] if m["gt_noise"] else None
    )
    return m


def run_parent(args) -> int:
    task_root = Path(args.task_root)
    taxonomy = load_taxonomy(Path(args.taxonomy))
    subenv_root = Path(args.subenv_root) if args.subenv_root else None

    curated = []
    for spec in args.curated:
        label, _, d = spec.partition("=")
        if not d:
            sys.exit(f"--curated 需要 label=目录 形式: {spec}")
        curated.append((label, Path(d)))

    # 任务集 = 各 curated 根下已产出的任务交集口径（以第一个为基准）
    task_ids = sorted(
        p.name for p in curated[0][1].iterdir()
        if (p / "curation.json").is_file() and p.is_dir()
    )
    if args.exclude:
        exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
        task_ids = [t for t in task_ids if t not in exclude]

    report = {}
    for label, root in curated:
        per_case = {}
        agg = dict(gt_files=0, gt_std=0, gt_noise=0, n_selected=0,
                   killed_std=0, leaked_noise=0, part_ok=0,
                   killed_std_seen=0, killed_std_unseen=0,
                   leaked_seen=0, leaked_unseen=0, missing=0)
        for tid in task_ids:
            cj = root / tid / "curation.json"
            if not cj.is_file():
                per_case[tid] = {"error": "missing curation.json"}
                agg["missing"] += 1
                continue
            cur = load_json(cj)
            meta = load_json(task_root / tid / "metadata.json")
            truth = {
                norm_path(e["target_path"]): taxonomy.get(
                    (tid, norm_path(e["target_path"])),
                    {"partition": e.get("input_role")},
                )
                for e in meta["data_manifest"]
            }
            for p, gt in truth.items():
                gt.setdefault("partition", None)
            m = score_parent_case(truth, cur.get("files") or [])
            if subenv_root is not None:
                seen = load_seen_paths(subenv_root, tid)
                m["killed_std_seen"] = [p for p in m["killed_std"] if p in seen]
                m["killed_std_unseen"] = [
                    p for p in m["killed_std"] if p not in seen]
                m["leaked_seen"] = [p for p in m["leaked_noise"] if p in seen]
                m["leaked_unseen"] = [
                    p for p in m["leaked_noise"] if p not in seen]
            per_case[tid] = m
            agg["gt_files"] += m["gt_files"]
            agg["gt_std"] += m["gt_std"]
            agg["gt_noise"] += m["gt_noise"]
            agg["n_selected"] += m["n_selected"]
            agg["killed_std"] += len(m["killed_std"])
            agg["leaked_noise"] += len(m["leaked_noise"])
            agg["part_ok"] += m["part_ok"]
            for k_seen, k_unseen, src in (
                ("killed_std_seen", "killed_std_unseen", "killed_std"),
                ("leaked_seen", "leaked_unseen", "leaked_noise"),
            ):
                if k_seen in m:
                    agg[k_seen] += len(m[k_seen])
                    agg[k_unseen] += len(m[k_unseen])
        report[label] = {"curated_root": str(root), "per_case": per_case,
                         "aggregate": agg}

    hdr = (f"{'curator':<10}{'gt':>6}{'std':>5}{'noise':>7}{'select':>7}"
           f"{'误杀':>7}{'漏放':>7}{'误杀%':>8}{'漏放%':>8}{'part%':>8}{'miss':>6}")
    print(hdr)
    print("-" * len(hdr))
    for label, _ in curated:
        a = report[label]["aggregate"]
        print(
            f"{label:<10}{a['gt_files']:>6}{a['gt_std']:>5}{a['gt_noise']:>7}"
            f"{a['n_selected']:>7}{a['killed_std']:>7}{a['leaked_noise']:>7}"
            f"{pct(a['killed_std'], a['gt_std']):>8}"
            f"{pct(a['leaked_noise'], a['gt_noise']):>8}"
            f"{pct(a['part_ok'], a['gt_files']):>8}{a['missing']:>6}"
        )
    for label, _ in curated:
        a = report[label]["aggregate"]
        if "killed_std_seen" in a:
            print(
                f"{label}: 误杀 训练内/未见 = {a['killed_std_seen']}/"
                f"{a['killed_std_unseen']}, 漏放 训练内/未见 = "
                f"{a['leaked_seen']}/{a['leaked_unseen']}"
            )
    if args.show_wrong:
        for label, _ in curated:
            print(f"\n== {label} 误杀/漏放明细 ==")
            for tid, m in report[label]["per_case"].items():
                if m.get("error"):
                    print(f"[{tid}] {m['error']}")
                    continue
                for p in m.get("killed_std", []):
                    print(f"[{tid}] 误杀 {p}")
                for p in m.get("leaked_noise", []):
                    print(f"[{tid}] 漏放 {p}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nreport -> {args.out}")
    return 0


def pct(a, b):
    return f"{100.0 * a / b:.1f}" if b else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent", action="store_true",
                    help="父任务口径（构造式 workspace 构造质量）")
    ap.add_argument("--gt-root", required=False,
                    help="子环境口径真值根（默认模式必需）")
    ap.add_argument("--exp", action="append",
                    help="label=实验目录(可多次;子环境口径)")
    ap.add_argument("--curated", action="append",
                    help="label=preprocessed 目录(可多次;父任务口径)")
    ap.add_argument("--task-root", default="tasks_hard_v4",
                    help="父任务真值根（父任务口径）")
    ap.add_argument("--taxonomy",
                    default="experiments/noise-id/noise_taxonomy_v2_final.json",
                    help="类别真值（父任务口径）")
    ap.add_argument("--subenv-root", default=".generated/noise_id_subenvs",
                    help="训练子环境根（训练内/未见分层；传空串关闭）")
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径")
    ap.add_argument("--exclude", default="",
                    help="剔除的 task_id(逗号分隔,如上下文超限等两模型同因失败的环境)")
    ap.add_argument("--show-wrong", action="store_true",
                    help="打印逐文件错判明细")
    args = ap.parse_args()

    if args.parent:
        if not args.curated:
            sys.exit("--parent 需要 --curated label=<preprocessed 目录>")
        return run_parent(args)
    if not args.gt_root or not args.exp:
        sys.exit("默认(子环境)口径需要 --gt-root 与 --exp；父任务口径用 --parent")

    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}

    gt_root = Path(args.gt_root)
    exps = []
    for spec in args.exp:
        label, _, d = spec.partition("=")
        if not d:
            sys.exit(f"--exp 需要 label=目录 形式: {spec}")
        exps.append((label, Path(d)))

    task_ids = [t for t in task_ids_of(exps[0][1]) if t not in exclude]
    if exclude:
        print(f"剔除 {len(exclude)} 个: {sorted(exclude)}")
    print(f"tasks: {len(task_ids)}  (gt_root={gt_root})\n")

    report = {}
    for label, exp_dir in exps:
        per_case = {}
        agg = dict(gt=0, covered=0, part_ok=0, noise_n=0, noise_part_ok=0,
                   cat_ok=0, stage_n=0, stage_ok=0, full_ok=0, extra_n=0,
                   qualified=0, missing=0, extra_all_noise_cases=0)
        for tid in task_ids:
            truth = load_truth(gt_root, tid)
            pred, err = load_pred(exp_dir, tid)
            m = score_case(truth, pred)
            m["error"] = err
            per_case[tid] = m
            agg["gt"] += m["gt_files"]
            for k in ("covered", "part_ok", "noise_n", "noise_part_ok", "cat_ok",
                      "stage_n", "stage_ok", "full_ok", "extra_n"):
                agg[k] += m[k]
            if m["qualified"]:
                agg["qualified"] += 1
            if err:
                agg["missing"] += 1
            if m["extra_all_noise"]:
                agg["extra_all_noise_cases"] += 1

        report[label] = {
            "exp_dir": str(exp_dir),
            "per_case": per_case,
            "aggregate": agg,
        }

    # 汇总表
    hdr = (f"{'model':<10}{'gt_files':>9}{'cover%':>8}{'part%':>8}"
           f"{'noise-part%':>12}{'cat%':>7}{'stage%':>8}{'full%':>8}"
           f"{'qual':>6}{'miss':>6}")
    print(hdr)
    print("-" * len(hdr))
    for label, _ in exps:
        a = report[label]["aggregate"]
        print(
            f"{label:<10}{a['gt']:>9}"
            f"{pct(a['covered'], a['gt']):>8}{pct(a['part_ok'], a['gt']):>8}"
            f"{pct(a['noise_part_ok'], a['noise_n']):>12}"
            f"{pct(a['cat_ok'], a['noise_n']):>7}"
            f"{pct(a['stage_ok'], a['stage_n']):>8}"
            f"{pct(a['full_ok'], a['gt']):>8}"
            f"{a['qualified']:>6}{a['missing']:>6}"
        )
    print()
    for label, _ in exps:
        a = report[label]["aggregate"]
        print(f"{label}: 多判文件(不在GT,多为注入的npm日志等) {a['extra_n']} 个, "
              f"其中全部判为 noise 的 case {a['extra_all_noise_cases']}/{len(task_ids)}")

    if args.show_wrong:
        for label, _ in exps:
            print(f"\n== {label} 错判明细 ==")
            for tid, m in report[label]["per_case"].items():
                if m["error"]:
                    print(f"[{tid}] MISSING: {m['error']}")
                for w in m["wrong"]:
                    print(f"[{tid}] {w['path']}")
                    print(f"    gt  : {w['gt']}")
                    print(f"    pred: {w['pred']}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nreport -> {args.out}")


if __name__ == "__main__":
    main()
