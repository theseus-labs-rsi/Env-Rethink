#!/usr/bin/env python3
"""D2 合并定稿：把 report 层（种子 + 提取）合并进标注结果，产出最终 taxonomy、
拆分单元清单与 QA 报告。

输入：
  1. annotate_noise_pool.py 全量跑出的 noise_taxonomy_v2.json
     （其中 report 层只烘焙了种子 report_overrides.json）；
  2. report_extractions/task*.json（9 个提取 agent 的结果，优先级高于种子）；
  3. report_overrides.json（人工种子）。

合并规则：
  - report 层：提取 > 种子（提取 agent 读过完整报告）> taxonomy 内已烘焙条目；
  - 其余层（llm/rule/manifest）保持不变；
  - families：提取的 families 整体替换该任务的种子 families。

输出：
  - noise_taxonomy_v2_final.json   最终逐文件标签
  - report_overrides_merged.json   合并后的 report 层（供重放/复现）
  - split_units.json               语义单元清单（D3 生成器输入）
  - qa_report.md                   覆盖率/冲突/待人工复核清单

路径常量 / load_manifest / find_override / STRONG 共享自 noise_id_common.py。

用法：
  python finalize_taxonomy.py [--qa-only]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from noise_id_common import (  # noqa: E402
    NOISE_ID, STRONG, load_manifest,
)
from noise_id_common import find_override  # noqa: E402


def merge_report_layer(seed: dict, extractions: list[dict]) -> dict:
    """提取 > 种子，逐任务合并 files；families 由提取整体替换。"""
    merged = json.loads(json.dumps(seed))  # deep copy
    for ext in extractions:
        task = ext["task"]
        slot = merged.setdefault(task, {})
        slot["_note"] = f"seed: {slot.get('_note', '')} | extraction: 已合并 report_extractions/task{task}.json"
        files = slot.setdefault("files", {})
        for key, ann in ext.get("files", {}).items():
            files[key] = ann  # 提取胜出
        if ext.get("families") is not None:
            slot["families"] = ext["families"]
        if ext.get("lock_chain_evidence"):
            slot["_lock_chain_evidence"] = ext["lock_chain_evidence"]
        # review_notes 汇总
        notes = slot.setdefault("_review_notes", [])
        for n in ext.get("review_notes", []):
            notes.append(n)
    return merged


def reapply_report_layer(taxonomy: dict, merged_ov: dict) -> list[dict]:
    """把合并后的 report 层重放到逐文件记录上（report > 其他层）。"""
    records = taxonomy["files"]
    for rec in records:
        ov = find_override(merged_ov, rec["task"], rec["filename"], rec["path"])
        if not ov:
            continue
        rec.update({k: v for k, v in ov.items() if k in rec and v is not None})
        rec["source"], rec["confidence"] = "report", "high"
        rec["needs_review"] = bool(ov.get("needs_review", False))
    return records


def build_split_units(taxonomy: dict, merged_ov: dict) -> dict:
    """语义单元清单：families（攻击场景/版本族）+ bulk 组（按顶层目录分组）。"""
    # metadata.json 由 load_manifest 进程内缓存，多任务只各解析一次
    fam_member_paths: dict[str, set] = defaultdict(set)
    units_by_task: dict[str, list] = {}

    for task in taxonomy["tasks"]:
        man = load_manifest(task)
        fams = merged_ov.get(task, {}).get("families", []) or []
        task_units = []

        def resolve_member(m: dict) -> dict | None:
            """成员解析：优先 path 直连；否则 filename 唯一映射。"""
            if m.get("path"):
                if m["path"] in man["by_path"]:
                    return {"path": m["path"], **{k: v for k, v in m.items() if k not in ("path", "filename")}}
                return None
            fn = m.get("filename", "")
            if fn in man["by_name"] and len(man["by_name"][fn]) == 1:
                return {"path": man["by_name"][fn][0]["target_path"], **{k: v for k, v in m.items() if k != "filename"}}
            return None

        for fam in fams:
            members = [r for r in (resolve_member(m) for m in fam.get("members", [])) if r]
            fab = [r for r in (resolve_member(m) for m in fam.get("fabricated_members", [])) if r]
            # 范围占位展开（如 临时单据_26.txt … 临时单据_50.txt）
            for m in fam.get("fabricated_members", []):
                fn = m.get("filename", "")
                if "…" in fn:
                    mm = re.match(r"(.+?)_(\d+)\.txt … .+?_(\d+)\.txt", fn)
                    if mm:
                        stem, lo, hi = mm.group(1), int(mm.group(2)), int(mm.group(3))
                        for i in range(lo, hi + 1):
                            cand = f"{stem}_{i}.txt"
                            if cand in man["by_name"] and len(man["by_name"][cand]) == 1:
                                fab.append({"path": man["by_name"][cand][0]["target_path"], "claimed_stage": m.get("claimed_stage"), "contradiction": m.get("contradiction")})
            ext_copies = [r for r in (resolve_member(m) for m in fam.get("external_copies", [])) if r]
            if not members and not fab and not ext_copies:
                continue
            unit_type = "attack_scene" if fab else "version_family"
            unit = {
                "unit_id": fam.get("family_id"),
                "type": unit_type,
                "subject": fam.get("subject"),
                "canonical_paths": [m["path"] for m in members if m.get("is_canonical")],
                "members": members,
                "fabricated_members": fab,
                "external_copies": ext_copies,
            }
            # 场景完整性：攻击单元若无 canonical（指针组/平行链），不能单独
            # 构成子环境，必须与其目标单元（含 canonical 的族）组合出场。
            if unit_type == "attack_scene" and not unit["canonical_paths"]:
                unit["requires_combination"] = True
            task_units.append(unit)
            for m in members + fab + ext_copies:
                fam_member_paths[task].add(m["path"])
        units_by_task[task] = task_units

    # bulk 组：不在任何 family 中的 noise 文件，按顶层目录分组
    bulk_by_task: dict[str, dict[str, list]] = {}
    for rec in taxonomy["files"]:
        task = rec["task"]
        if rec["partition"] != "noise" or rec["path"] in fam_member_paths[task]:
            continue
        bulk_by_task.setdefault(task, {}).setdefault(rec["path"].split("/")[0], []).append(rec["path"])

    units = {}
    for task in taxonomy["tasks"]:
        bulk_groups = [
            {"unit_id": f"{task}-BULK-{i}", "type": "bulk", "subject": top,
             "paths": paths}
            for i, (top, paths) in enumerate(sorted(bulk_by_task.get(task, {}).items()))
        ]
        units[task] = {"semantic_units": units_by_task[task], "bulk_groups": bulk_groups}
    return units


def qa_report(taxonomy: dict, records: list[dict], units: dict) -> str:
    lines = ["# noise-id D2 QA 报告（自动生成）\n",
             f"生成时间：{dt.datetime.now(dt.timezone.utc).isoformat()}\n"]
    lines.append("## 各任务标注来源分布\n")
    lines.append("| task | files | std | noise | report | llm | rule | manifest | needs_review |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    by_task = defaultdict(lambda: Counter())
    for r in records:
        t = by_task[r["task"]]
        t["files"] += 1
        t[r["partition"]] += 1
        t[f"src_{r['source']}"] += 1
        t["review"] += int(r["needs_review"])
    for task in sorted(by_task):
        t = by_task[task]
        lines.append(f"| {task} | {t['files']} | {t['standard']} | {t['noise']} | "
                     f"{t['src_report']} | {t['src_llm']} | {t['src_rule']} | {t['src_manifest']} | {t['review']} |")

    lines.append("\n## 类别分布（noise 文件）\n")
    cat = Counter(r["category"] for r in records if r["partition"] == "noise")
    lines.append(", ".join(f"{k}: {v}" for k, v in cat.most_common()))

    lines.append("\n## 语义单元统计\n")
    lines.append("| task | 攻击场景 | 版本族 | bulk 组 | bulk 文件数 |")
    lines.append("|---|---:|---:|---:|---:|")
    for task, u in sorted(units.items()):
        n_atk = sum(1 for x in u["semantic_units"] if x["type"] == "attack_scene")
        n_fam = sum(1 for x in u["semantic_units"] if x["type"] == "version_family")
        n_bulk_f = sum(len(g["paths"]) for g in u["bulk_groups"])
        lines.append(f"| {task} | {n_atk} | {n_fam} | {len(u['bulk_groups'])} | {n_bulk_f} |")

    # 冲突清单：report 覆盖但 needs_review
    lines.append("\n## 待人工复核（needs_review=true，按任务）\n")
    n = 0
    for r in records:
        if r["needs_review"]:
            n += 1
            lines.append(f"- {r['task']} `{r['path']}` [{r['category']}/{r['stage']}] {(r['evidence'] or '')[:80]}")
    if n == 0:
        lines.append("（无）")
    lines.append(f"\n共 {n} 条。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--taxonomy", default=str(NOISE_ID / "noise_taxonomy_v2.json"))
    ap.add_argument("--overrides", default=str(NOISE_ID / "report_overrides.json"))
    ap.add_argument("--extractions", default=str(NOISE_ID / "report_extractions"))
    ap.add_argument("--out-taxonomy", default=str(NOISE_ID / "noise_taxonomy_v2_final.json"))
    ap.add_argument("--out-overrides", default=str(NOISE_ID / "report_overrides_merged.json"))
    ap.add_argument("--out-units", default=str(NOISE_ID / "split_units.json"))
    ap.add_argument("--out-qa", default=str(NOISE_ID / "qa_report.md"))
    ap.add_argument("--qa-only", action="store_true")
    args = ap.parse_args()

    taxonomy = json.loads(Path(args.taxonomy).read_text(encoding="utf-8"))
    seed = json.loads(Path(args.overrides).read_text(encoding="utf-8"))
    extractions = []
    for p in sorted(Path(args.extractions).glob("task*.json")):
        extractions.append(json.loads(p.read_text(encoding="utf-8")))
    print(f"[load] taxonomy {len(taxonomy['files'])} files; seed tasks {len(seed)}; extractions {len(extractions)}")

    merged_ov = merge_report_layer(seed, extractions)
    if not args.qa_only:
        records = reapply_report_layer(taxonomy, merged_ov)
        taxonomy["files"] = records
        taxonomy["families"] = {t: merged_ov.get(t, {}).get("families", []) for t in taxonomy["tasks"]}
        taxonomy["report_layer_merged_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        units = build_split_units(taxonomy, merged_ov)
        Path(args.out_taxonomy).write_text(json.dumps(taxonomy, ensure_ascii=False, indent=1), encoding="utf-8")
        Path(args.out_overrides).write_text(json.dumps(merged_ov, ensure_ascii=False, indent=1), encoding="utf-8")
        Path(args.out_units).write_text(json.dumps(units, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[out] taxonomy -> {args.out_taxonomy}")
        print(f"[out] merged overrides -> {args.out_overrides}")
        print(f"[out] split units -> {args.out_units}")
    else:
        # --qa-only：不复写 taxonomy/final；仅基于现状重出 QA（build 需 units）
        units = build_split_units(taxonomy, merged_ov)

    report = qa_report(taxonomy, taxonomy["files"], units)
    Path(args.out_qa).write_text(report, encoding="utf-8")
    print(f"[out] qa report -> {args.out_qa}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
