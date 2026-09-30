#!/usr/bin/env python3
"""gen_noise_id_heldout.py — 增量生成一批全新 noise-id 子环境(held-out 评估用)。

与 generate_noise_id_subenvs.py 的区别:
- 换 seed(组合不同);
- 不清库,增量物化进现有 GEN_ROOT;
- sid 用 100 起的偏移段(如 108-101),与原 001-0xx 段区分;
- index.json 合并写入,manifest 带 batch=heldout 标记;
- 生成后检查与「已训练 subenv」的文件组合重叠(Jaccard),>0.8 标记 overlap。

用法:
  python3 scripts/gen_noise_id_heldout.py --per-task 2 [--dry-run]
"""
import argparse
import datetime as _dt
import json
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_noise_id_subenvs as g
from noise_id_common import GEN_ROOT

NEW_SEED = "20260909-heldout"
BATCH_TAG = "heldout-v1"
SID_OFFSET = 100
OVERLAP_WARN = 0.8


def generate_task_fresh(td, n_want, rng, used):
    """与 g.generate_task 同构,但 used 预置了现有全部 subenv 的组合签名
    (置为 SAME_COMBO_MAX_DUP),从源头杜绝与既有环境完全同组合。"""
    nS, nM, nL = g._tier_counts(n_want)
    recipes, attempts = [], 0
    remain = Counter({"S": nS, "M": nM, "L": nL})
    while len(recipes) < n_want and attempts < n_want * 400:
        attempts += 1
        avail = [t for t in ("S", "M", "L") if remain[t] > 0]
        if not avail:
            break
        tier = rng.choice(avail)
        got = g._make_recipe(td, tier, rng)
        if got is None:
            if attempts > n_want * 200:
                tier = rng.choice(["S", "M", "L"])
                got = g._make_recipe(td, tier, rng)
            else:
                continue
        if got is None:
            continue
        files, desc = got
        if not files:
            continue
        sig = g._signature(files, tier)
        if used[sig] >= g.SAME_COMBO_MAX_DUP:
            continue
        used[sig] += 1
        recipes.append({"task": td.task, "tier": tier, "files": sorted(files),
                        "desc": desc,
                        "difficulty": g._difficulty(td, [u for u, *_ in desc["units"]])})
        if remain[tier] > 0:
            remain[tier] -= 1
    return recipes


def existing_used_signatures() -> Counter:
    """现有全部 subenv 的 (tier, 文件集) 签名,全部置满额。"""
    used = Counter()
    idx = json.loads((GEN_ROOT / "index.json").read_text(encoding="utf-8"))
    for sid, man in idx.items():
        lj = GEN_ROOT / sid / "labels.json"
        if not lj.is_file():
            continue
        paths = [f["path"] for f in json.loads(lj.read_text(encoding="utf-8"))["files"]]
        used[g._signature(paths, man.get("tier"))] = g.SAME_COMBO_MAX_DUP
    return used


def clean_batch():
    """删除本脚本物化的全部 heldout 批次 subenv 并还原 index.json。"""
    idx_path = GEN_ROOT / "index.json"
    index = json.loads(idx_path.read_text(encoding="utf-8"))
    removed = [sid for sid, man in index.items() if man.get("batch") == BATCH_TAG]
    for sid in removed:
        shutil.rmtree(GEN_ROOT / sid)
        del index[sid]
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已清理 {len(removed)} 个 {BATCH_TAG} subenv: {removed[:6]}{'...' if len(removed) > 6 else ''}")
    return removed


def trained_sids() -> set:
    sids = set()
    tj = GEN_ROOT / "_sft" / "trajectories.jsonl"
    for line in tj.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sids.add(json.loads(line).get("subenv_id"))
    return sids


def existing_paths_by_task() -> dict:
    """现有全部 subenv 的文件路径集合,按父任务分组。"""
    idx = json.loads((GEN_ROOT / "index.json").read_text(encoding="utf-8"))
    out = {}
    for sid, man in idx.items():
        task = man.get("parent_task")
        if not task:
            continue
        lj = GEN_ROOT / sid / "labels.json"
        if not lj.is_file():
            continue
        paths = frozenset(
            f["path"] for f in json.loads(lj.read_text(encoding="utf-8"))["files"]
        )
        out.setdefault(task, []).append((sid, paths))
    return out


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def materialize(recipes, td_map):
    idx_path = GEN_ROOT / "index.json"
    index = json.loads(idx_path.read_text(encoding="utf-8"))
    made, overlaps = [], []
    for task, rs in recipes.items():
        for i, rc in enumerate(rs, 1):
            sid = f"{task}-{SID_OFFSET + i:03d}"
            d = GEN_ROOT / sid
            if d.exists():
                raise SystemExit(f"sid 冲突: {sid} 已存在")
            ws = d / "workspace"
            ws.mkdir(parents=True)
            for p in rc["files"]:
                ent = td_map[task].manifest(p)
                if ent is None:
                    continue
                phys = g.TASK_ROOT / task / ent["stored_relpath"]
                dst = ws / p
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(phys, dst)
            td = td_map[task]
            labels = {
                "schema_version": 2,
                "subenv_id": sid,
                "files": [td.label(p) for p in rc["files"] if td.label(p)],
                "expected_families": g._expected_families(td, rc["desc"]["units"]),
            }
            (d / "labels.json").write_text(
                json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            (d / "hint.md").write_text(
                (g.HINTS_DIR / f"{rc.get('hint', 'L1')}.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            man = {
                "subenv_id": sid,
                "parent_task": task,
                "tier": rc["tier"],
                "hint": rc.get("hint", "L1"),
                "seed": NEW_SEED,
                "batch": BATCH_TAG,
                "units": [(uid, mode) for uid, mode, _ in rc["desc"]["units"]],
                "n_files": len(rc["files"]),
                "n_std": sum(
                    1 for p in rc["files"]
                    if td.label(p) and td.label(p)["partition"] == "standard"
                ),
                "difficulty_proxy": rc["difficulty"],
                "background": g.BACKGROUND,
                "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
            (d / "subenv_manifest.json").write_text(
                json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            index[sid] = man
            made.append((sid, man))
    idx_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 与已训练 subenv 的组合重叠检查
    tr = trained_sids()
    ex = existing_paths_by_task()
    for sid, man in made:
        lj = GEN_ROOT / sid / "labels.json"
        paths = frozenset(
            f["path"] for f in json.loads(lj.read_text(encoding="utf-8"))["files"]
        )
        best = max(
            ((jaccard(paths, ps), osid) for osid, ps in ex.get(man["parent_task"], [])
             if osid in tr),
            default=(0.0, None),
        )
        if best[0] > OVERLAP_WARN:
            overlaps.append((sid, best[1], round(best[0], 3)))
    return made, overlaps


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-task", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="删除已生成的 heldout 批次 subenv 并还原 index.json")
    args = ap.parse_args()

    if args.clean:
        clean_batch()
        return

    td_map, plan, problems = {}, [], []
    for task in g.ALLOCATIONS:
        td = g.TaskData(task)
        if not set(td.canonical_paths) <= set(td.std_paths):
            problems.append(f"{task}: canonical 超出 standard")
        td_map[task] = td
        plan.append((task, args.per_task, g._tier_counts(args.per_task)))

    used = existing_used_signatures()
    recipes = {}
    for task, N, _tiers in plan:
        trng = random.Random(f"{NEW_SEED}-{task}")
        rs = generate_task_fresh(td_map[task], N, trng, used)
        recipes[task] = rs
        for r in rs:
            n = len(r["files"])
            strong = [
                p for p in r["files"]
                if (lab := td_map[task].label(p))
                and lab.get("category") in g.STRONG
            ]
            if strong and not g._has_standard_anchor(td_map[task], r["files"]):
                problems.append(f"{task}/{r['tier']}: 强诱饵无 canonical")
            if n > g.MAX_FILES:
                problems.append(f"{task}/{r['tier']}: {n} 文件超上限")

    g._assign_hints(recipes, list(g.ALLOCATIONS))
    total = sum(len(v) for v in recipes.values())
    print(f"新 seed={NEW_SEED} | 每任务 {args.per_task} | 总生成 {total}")
    if problems:
        print("[校验失败]")
        for p in problems:
            print("  ", p)
        raise SystemExit(1)
    print("[校验通过] 强诱饵均有 canonical 锚点 / 文件数上限内")

    if args.dry_run:
        for task, rs in recipes.items():
            for i, r in enumerate(rs, 1):
                print(f"  {task}-{SID_OFFSET + i:03d} tier={r['tier']} "
                      f"hint={r.get('hint')} files={len(r['files'])}")
        return

    made, overlaps = materialize(recipes, td_map)
    print(f"已物化 {len(made)} 个新 subenv 到 {GEN_ROOT}:")
    for sid, man in made:
        print(f"  {sid}  tier={man['tier']} hint={man['hint']} "
              f"files={man['n_files']} std={man['n_std']}")
    if overlaps:
        print(f"\n[警告] {len(overlaps)} 个与训练 subenv 组合高度重叠(Jaccard>{OVERLAP_WARN}):")
        for sid, osid, j in overlaps:
            print(f"  {sid} ~ {osid} (J={j}) — 评估选样时剔除")
    else:
        print("\n[ok] 无与训练 subenv 的高重叠组合")


if __name__ == "__main__":
    main()
