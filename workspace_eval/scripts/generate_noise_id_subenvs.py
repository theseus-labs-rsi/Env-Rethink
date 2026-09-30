#!/usr/bin/env python3
"""generate_noise_id_subenvs.py — noise-id 子环境生成器（v2.2 语义单元拆分）。

把 split_units.json 的语义单元组合成子环境（S/M/L 分批），满足场景完整性硬规则，
物化到 evaluation/.generated/noise_id_subenvs/<subenv-id>/：

    workspace/<target_path>...   文件实体（复制物理文件到逻辑工作区布局）
    labels.json                   本子环境逐文件标签 + expected_families（评分真值）
    hint.md                       三级 hint 模板（task-free）
    subenv_manifest.json          溯源：父任务/单元组合/seed/批档/难度代理/hint 档/生成时间

数据装载（metadata.json / split_units.json / noise_taxonomy_v2_final.json）经
noise_id_common 缓存，单进程每任务只解析一次。

用法：
    python scripts/generate_noise_id_subenvs.py --validate   # 只算不落盘，打印配额/规模/断言
    python scripts/generate_noise_id_subenvs.py              # 正式生成
    python scripts/generate_noise_id_subenvs.py --reset      # 已有 rollout/agentic 结果时需显式重置

输入（只读）：noise_taxonomy_v2_final.json、split_units.json、plan_v2.yaml（参考快照，
代码为准）、tasks_hard_v4/<id>/metadata.json（data_manifest）。凭据与网络均不需要。
subenv_excludes.json 记录的泄漏物已在标注/单元层剔除（不进 data_manifest/split_units），
生成器只物化 manifest 内文件，不读取该清单（见 AGENTS.md 对应说明）。
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
from noise_id_common import (  # noqa: E402
    GEN_ROOT, NOISE_ID, STRONG, TASK_ROOT, load_manifest, load_split_units,
    load_taxonomy_final,
)

HINTS_DIR = NOISE_ID / "hints"

# ---- 计划（与 plan_v2.yaml 一致；yaml 为参考快照，代码为准）----
SEED = 20260904
BACKGROUND = "none"
MAX_FILES = 80
SAME_COMBO_MAX_DUP = 2
TIER_RATIO = {"S": 0.45, "M": 0.35, "L": 0.20}
HINT_DIST = {"L0": 30, "L1": 100, "L2": 70}

ALLOCATIONS = {
    "374": 20, "357": 20, "372": 20, "154": 18, "314": 18, "258": 16,
    "108": 16, "291": 15, "160": 14, "207": 14, "267": 12, "288": 12,
    "129": 8, "94": 6, "334": 5,
}

# 批档规模带（小任务 M 下限放宽到 8；S 下限 4）
TIER_SIZE = {"S": (4, 12), "M": (8, 30), "L": (1, MAX_FILES)}


# ---------------------------------------------------------------- 数据装载
class TaskData:
    def __init__(self, task):
        self.task = task
        man = load_manifest(task)
        # target_path -> manifest entry
        self.by_path = man["by_path"]
        self.std_paths = sorted(e["target_path"] for e in man["by_path"].values()
                                if e.get("input_role") == "standard")
        u = load_split_units()[task]
        self.units = u["semantic_units"]
        self.bulk_paths = [p for g in u["bulk_groups"] for p in g["paths"]]
        self.bulk_groups = [g["paths"] for g in u["bulk_groups"]]
        # taxonomy 逐文件标签（按 path），taxonomy 文件单进程只解析一次
        tax = load_taxonomy_final()
        self.labels = {r["path"]: r for r in tax["files"] if r["task"] == task}

        def _ufiles(unit):
            out = [mm["path"] for mm in unit.get("members", [])]
            out += [ff["path"] for ff in unit.get("fabricated_members", [])]
            out += [ee["path"] for ee in unit.get("external_copies", [])]
            return out

        self.unit_files = {x["unit_id"]: _ufiles(x) for x in self.units}
        self.by_id = {x["unit_id"]: x for x in self.units}
        self.carriers = [x["unit_id"] for x in self.units if x.get("canonical_paths")]
        self.dependents = [x["unit_id"] for x in self.units if not x.get("canonical_paths")]
        self.canonical_paths = [p for x in self.units for p in x.get("canonical_paths", [])]
        # fabricated members per unit (for subsetting)
        self.fab_paths = {x["unit_id"]: [f["path"] for f in x.get("fabricated_members", [])]
                          for x in self.units}

    def label(self, path):
        return self.labels.get(path)

    def manifest(self, path):
        return self.by_path.get(path)


def _has_standard_anchor(td, files):
    """文件集中是否含 standard 锚点（manifest 硬约束，S/M/L 都必须有）。"""
    return any(td.label(p) and td.label(p)["partition"] == "standard" for p in files)


def _has_noise(td, files):
    """文件集中是否含可识别的噪声文件（纯 standard 是退化环境，S/M 剔除）。"""
    return any(td.label(p) and td.label(p)["partition"] == "noise" for p in files)


def _canon_only_files(td, uid):
    unit = td.by_id[uid]
    cands = [mm["path"] for mm in unit.get("members", []) if mm.get("is_canonical")]
    if cands:
        return cands
    return list(unit.get("canonical_paths", []))


def _unit_selected_files(td, uid, fab_mode, rng):
    """按 fab_mode 返回选中该 unit 的文件集：full=全部; canon=仅 canonical; sub=N个诱饵+canonical+版本链成员。"""
    unit = td.by_id[uid]
    base = [mm["path"] for mm in unit.get("members", [])]
    ext = [ee["path"] for ee in unit.get("external_copies", [])]
    fab = list(td.fab_paths[uid])
    if fab_mode == "canon":
        sel = list(dict.fromkeys(_canon_only_files(td, uid) + ext))
        return sel, []
    if fab_mode == "full" or not fab:
        return list(dict.fromkeys(base + ext + fab)), list(fab)
    # sub: canonical + 版本链成员 + 随机 1..len(fab) 个诱饵
    n = rng.randint(1, len(fab))
    sub = rng.sample(fab, n)
    return list(dict.fromkeys(base + ext + sub)), list(sub)


def _difficulty(td, unit_ids):
    cats = set()
    nver = 0
    for uid in unit_ids:
        unit = td.by_id[uid]
        for f in unit.get("fabricated_members", []):
            lab = td.label(f["path"])
            if lab:
                cats.add(lab.get("category"))
        for mm in unit.get("members", []):
            if mm.get("order"):
                nver += 1
    return len(unit_ids) * max(1, len(cats)) * max(1, nver)


# ---------------------------------------------------------------- 采样
def _bulk_pick(td, rng, n, exclude):
    """跨 bulk 组均匀抽 n 个 bulk 文件（不重复、排除已选）。"""
    pool = [p for p in td.bulk_paths if p not in exclude]
    if not pool:
        return []
    groups = [g for g in td.bulk_groups if any(p not in exclude for p in g)]
    rng.shuffle(groups)
    chosen = []
    gi = 0
    while len(chosen) < n and groups:
        g = groups[gi % len(groups)]
        avail = [p for p in g if p not in exclude and p not in chosen]
        if avail:
            chosen.append(rng.choice(avail))
        else:
            groups.pop(gi % len(groups))
            if not groups:
                break
            continue
        gi += 1
    return chosen


def _make_recipe(td, tier, rng):
    """返回 (files 集合, 描述 dict)；返回 None 表示该次采样失败。"""
    files = set()
    picked = []          # (unit_id, fab_mode, chosen_fab)
    if tier == "L":
        for uid in td.by_id:
            s, cf = _unit_selected_files(td, uid, "full", rng)
            files.update(s)
            picked.append((uid, "full", cf))
        files.update(td.std_paths)          # L = 全部 standard
        budget = MAX_FILES - len(files)
        if budget > 0:
            files.update(_bulk_pick(td, rng, min(budget, 2 * max(1, len(td.bulk_groups))), files))
        if not _has_standard_anchor(td, files):
            return None
        return files, {"units": picked, "std_all": True}

    # S / M
    carriers = td.carriers[:]
    rng.shuffle(carriers)
    if tier == "S":
        if not carriers:
            return None
        ncar = 1
        dep = [d for d in td.dependents if rng.random() < 0.5][:1]
    else:
        ncar = min(len(carriers), rng.randint(2, 3))
        dep = td.dependents[:]
        rng.shuffle(dep)
        dep = dep[:rng.randint(0, min(2, len(dep)))]

    chosen_car = carriers[:ncar]
    for uid in chosen_car:
        fab = td.fab_paths[uid]
        roll = rng.random()
        if tier == "S" and fab:
            # 偏向整链，必要时 sub/canon 做多样性
            mode = "full" if roll < 0.5 else ("canon" if roll < 0.65 else "sub")
        elif fab:
            mode = "full" if roll < 0.8 else "canon"
        else:
            mode = "full"
        s, cf = _unit_selected_files(td, uid, mode, rng)
        files.update(s)
        picked.append((uid, mode, cf))
    for uid in dep:
        s, cf = _unit_selected_files(td, uid, "full", rng)
        files.update(s)
        picked.append((uid, "full", cf))
    if not _has_standard_anchor(td, files):
        return None  # 必须有 standard 锚点

    lo, hi = TIER_SIZE[tier]
    # 填充 bulk 达到规模带
    if len(files) < lo:
        files.update(_bulk_pick(td, rng, lo - len(files), files))
    if len(files) > hi:
        # 超上限：先减 bulk（按倒序随机剔除非单元文件），再接受超限
        extra = sorted(p for p in files if p not in td.unit_files and td.manifest(p)
                       and td.label(p) and td.label(p)["partition"] == "noise")
        rng.shuffle(extra)
        drop = extra[:len(files) - hi]
        files.difference_update(drop)
    # 教学约束：S/M 若纯 standard（无任何噪声可识别）属退化环境，剔除
    if tier != "L" and not _has_noise(td, files):
        return None
    return files, {"units": picked, "std_all": False}


def _signature(files, tier):
    # 去重以「实际文件集」为准：同单元 + 不同 bulk 填充算不同子环境；
    # 完全相同文件集最多重复 2 次（文档 §4.4）。
    return (tier, frozenset(files))


def _tier_counts(N):
    """配额 S/M/L 拆分（S ~45%、L ~20% 且 N≥5 时至少 1；N<5 无 L）。"""
    nS = int(round(N * TIER_RATIO["S"]))
    nL = max(1, round(N * TIER_RATIO["L"])) if N >= 5 else 0
    return nS, N - nS - nL, nL


def generate_task(td, N, rng):
    nS, nM, nL = _tier_counts(N)
    recipes, used, attempts = [], Counter(), 0
    # 剩余档位配额；按成功数递减，保证档位分布贴近计划
    remain = Counter({"S": nS, "M": nM, "L": nL})
    while len(recipes) < N and attempts < N * 400:
        attempts += 1
        avail = [t for t in ("S", "M", "L") if remain[t] > 0]
        if not avail:
            break
        tier = rng.choice(avail)
        got = _make_recipe(td, tier, rng)
        if got is None:
            # 档位频繁失败（多样性耗尽）时允许降级到其他档位补齐配额
            if attempts > N * 200:
                tier = rng.choice(["S", "M", "L"])
                got = _make_recipe(td, tier, rng)
            else:
                continue
        if got is None:
            continue
        files, desc = got
        if not files:
            continue
        sig = _signature(files, tier)
        if used[sig] >= SAME_COMBO_MAX_DUP:
            continue
        used[sig] += 1
        recipes.append({"task": td.task, "tier": tier, "files": sorted(files),
                        "desc": desc, "difficulty": _difficulty(td, [u for u, *_ in desc["units"]])})
        if remain[tier] > 0:
            remain[tier] -= 1
    return recipes


# ---------------------------------------------------------------- 物化
def _has_downstream_results() -> bool:
    """GEN_ROOT 下是否已存在下游 rollout/agentic 结果（默认拒绝覆盖）。"""
    if not GEN_ROOT.exists():
        return False
    for d in GEN_ROOT.iterdir():
        if d.is_dir():
            if (d / "rollout.json").exists() or (d / "agentic").exists():
                return True
        elif d.name in ("scores.jsonl", "agentic_pilot_results.json"):
            return True
    return False


def _write(recipes, td_map, *, reset: bool):
    if GEN_ROOT.exists():
        if _has_downstream_results() and not reset:
            raise SystemExit(
                "GEN_ROOT 已含 rollout/agentic 下游结果，拒绝静默清库。"
                "确认废弃旧结果后显式加 --reset 重建。")
        shutil.rmtree(GEN_ROOT)
    GEN_ROOT.mkdir(parents=True)
    index = {}
    for task, rs in recipes.items():
        for i, rc in enumerate(rs, 1):
            sid = f"{task}-{i:03d}"
            d = GEN_ROOT / sid
            ws = d / "workspace"
            (ws).mkdir(parents=True)
            # 复制物理文件到逻辑布局
            for p in rc["files"]:
                ent = td_map[task].manifest(p)
                if ent is None:
                    continue
                phys = TASK_ROOT / task / ent["stored_relpath"]
                dst = ws / p
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(phys, dst)
            labels = {"schema_version": 2, "subenv_id": sid,
                      "files": [td_map[task].label(p) for p in rc["files"] if td_map[task].label(p)],
                      "expected_families": _expected_families(td_map[task], rc["desc"]["units"])}
            (d / "labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=1))
            hz = rc.get("hint", "L1")
            hint = (HINTS_DIR / f"{hz}.md").read_text(encoding="utf-8")
            (d / "hint.md").write_text(hint, encoding="utf-8")
            man = {"subenv_id": sid, "parent_task": task, "tier": rc["tier"],
                   "hint": hz, "seed": SEED,
                   "units": [(uid, mode) for uid, mode, _ in rc["desc"]["units"]],
                   "n_files": len(rc["files"]), "n_std": sum(1 for p in rc["files"]
                   if td_map[task].label(p) and td_map[task].label(p)["partition"] == "standard"),
                   "difficulty_proxy": rc["difficulty"],
                   "background": BACKGROUND,
                   "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat()}
            (d / "subenv_manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1))
            index[sid] = man
    (GEN_ROOT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))


def _expected_families(td, unit_sels):
    """命中单元的 expected families（评分真值）：members(带 order)/canonical/fabricated 矛盾。"""
    fams = []
    for i, (uid, mode, _cf) in enumerate(unit_sels, 1):
        u = td.by_id[uid]
        members = []
        for mm in u.get("members", []):
            members.append({"path": mm["path"], "stage": mm.get("stage"),
                            "date": None, "order": mm.get("order")})
        fab = []
        if mode != "canon":
            for f in u.get("fabricated_members", []):
                if mode == "full" or f["path"] in _cf:
                    fab.append({"path": f["path"], "contradiction": f.get("contradiction")})
        if not members and not fab:
            continue
        canon = next((mm["path"] for mm in u.get("members", []) if mm.get("is_canonical")),
                     (u.get("canonical_paths") or [None])[0])
        fams.append({"family_id": f"{uid}", "subject": u.get("subject"),
                     "members": members, "canonical": canon, "fabricated_members": fab})
    return fams


# hint 分配：全局近似 L0/L1/L2（按难度代理排序，每任务至少 1 个 L2 供 pilot）
def _assign_hints(recipes_by_task, tasks):
    n_task = len(tasks)
    dist_total = sum(HINT_DIST.values())
    # 每任务难度最高者强制 L2（pilot 用）
    forced = {}
    rest = []
    for task in tasks:
        rs = recipes_by_task[task]
        if not rs:
            continue
        best = max(rs, key=lambda r: r["difficulty"])
        forced[(task, rs.index(best))] = best
        for i, r in enumerate(rs):
            if (task, i) not in forced:
                rest.append((task, i, r))
    total = n_task + len(rest)
    # 按 corpus 比例缩放到实际总量
    tgt_L2 = max(n_task, round(total * HINT_DIST["L2"] / dist_total))
    tgt_L1 = round(total * HINT_DIST["L1"] / dist_total)
    # 先把每任务的 L2 落到 forced
    for (task, i) in forced:
        recipes_by_task[task][i]["hint"] = "L2"
    # 剩余 L2 名额：难度降序给 rest
    rest.sort(key=lambda x: x[2]["difficulty"], reverse=True)
    remaining_L2 = tgt_L2 - n_task
    for pos, (task, i, r) in enumerate(rest):
        if pos < remaining_L2:
            recipes_by_task[task][i]["hint"] = "L2"
    # L1：下一批难度降序
    done_L2 = n_task + remaining_L2
    for pos, (task, i, r) in enumerate(rest):
        if done_L2 <= pos < done_L2 + tgt_L1:
            recipes_by_task[task][i]["hint"] = "L1"
    # 其余 L0
    for task in tasks:
        for r in recipes_by_task[task]:
            r.setdefault("hint", "L0")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--validate", action="store_true", help="只计算校验，不落盘")
    ap.add_argument("--reset", action="store_true",
                    help="GEN_ROOT 已含 rollout/agentic 结果时显式清库重建")
    args = ap.parse_args()

    # canonical ⊂ standard 断言 & 结构盘点
    td_map = {}
    plan = []
    problems = []
    for task, N in ALLOCATIONS.items():
        td = TaskData(task)
        if not set(td.canonical_paths) <= set(td.std_paths):
            problems.append(f"{task}: canonical 超出 standard")
        td_map[task] = td
        plan.append((task, N, _tier_counts(N)))
        if not td.carriers:
            print(f"[warn] {task} 无 carrier 单元，无法保证 canonical 锚点")

    recipes = {}
    sizes = []
    for task, N, (nS, nM, nL) in plan:
        trng = random.Random(f"{SEED}-{task}")
        rs = generate_task(td_map[task], N, trng)
        recipes[task] = rs
        sizes.extend(len(r["files"]) for r in rs)
        got = len(rs)
        if got < N:
            problems.append(f"{task}: 配额 {N} 实际 {got}（-{N-got}）")
        # hard-rule / 规模自检
        for r in rs:
            n = len(r["files"])
            strong = [p for p in r["files"]
                      if (lab := td_map[task].label(p)) and lab.get("category") in STRONG]
            if strong and not _has_standard_anchor(td_map[task], r["files"]):
                problems.append(f"{task}/{r['tier']}: 强诱饵无 canonical")
            if n > MAX_FILES:
                problems.append(f"{task}/{r['tier']}: {n} 文件超上限")

    # 集中分配 hint（含每任务 L2 覆盖，供 pilot）
    _assign_hints(recipes, list(ALLOCATIONS))
    total = sum(len(v) for v in recipes.values())
    print(f"总生成子环境: {total}（配额 {sum(ALLOCATIONS.values())}）")
    hc = Counter(r["hint"] for rs in recipes.values() for r in rs)
    print("hint 分布:", dict(hc))
    no_l2 = [t for t in ALLOCATIONS if not any(r.get("hint") == "L2" for r in recipes[t])]
    if no_l2:
        print("[warn] 无 L2 的任务（pilot 无法取样）:", no_l2)
    else:
        print("[ok] 每任务均有 L2 供 pilot")

    print("per-task: quota/tier(S,M,L)/actual/规模范围")
    for task, N, (nS, nM, nL) in plan:
        rs = recipes[task]
        sizes_ = [len(r["files"]) for r in rs]
        lo = min(sizes_) if sizes_ else 0
        hi = max(sizes_) if sizes_ else 0
        tiers = Counter(r["tier"] for r in rs)
        print(f"  {task}: {N:3d} (S{nS}/M{nM}/L{nL}) -> {len(rs):3d} "
              f"[{lo}..{hi}] 实Tier {dict(tiers)}")
    print("\n规模分布(文件数→count): ",
          dict(sorted(Counter((s - 1) // 10 for s in sizes).items())) if sizes else {})
    if problems:
        print("\n[校验失败]")
        for p in problems:
            print("  ", p)
    else:
        print("\n[校验通过] 无结构违规（强诱饵/噪声必有 canonical 锚点 / 上限内 / canonical⊆standard）")

    if not args.validate:
        _write(recipes, td_map, reset=args.reset)
        print(f"\n已物化到 {GEN_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
