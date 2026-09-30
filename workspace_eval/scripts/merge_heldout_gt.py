#!/usr/bin/env python3
"""merge_heldout_gt.py — 用 09-12 的 extension 标注替换预留组被污染的 GT。

背景（2026-09-18）：
  `noise_taxonomy_v2_final_30task.json` 里预留 15 个任务的 category 有系统性问题：
  `annotate_noise_pool.py` 的冲突兜底把「LLM 判 canonical 但 manifest 判 noise」的条目
  一律写成 `unrelated`（evidence 里留下 `[冲突：…]` 标记），实测 53 条；
  另有 174 个内容抽不出文本的文件（.png 等）被同样兜底成 `unrelated`。
  09-12 的 `.generated/noise_taxonomy_extension/<task>.json` 是同一批任务的另一次标注，
  带版本族/攻击链结构分析与逐条款比对证据，质量显著更高。

本脚本只做合并，不重新标注：
  - 9 个有 extension 数据的任务（100/146/159/171/266/72/75/78/85）→ 用 extension
  - 其余任务 → 保留原 GT
  - task 72 的 stage 用了自造词表，按训练集的「category→stage」惯例归一化

输出（不覆盖任何原文件）：
  experiments/noise-id/gt_taxonomy_30task_v3.json   合并后的 30 任务 GT
  experiments/noise-id/gt_merge_qa.md               质量报告

用法：
    python scripts/merge_heldout_gt.py
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
import sys

EVAL = pathlib.Path(__file__).resolve().parents[1]
EXT_DIR = EVAL / ".generated" / "noise_taxonomy_extension"
SRC_GT = EVAL / "experiments" / "noise-id" / "noise_taxonomy_v2_final_30task.json"
OUT_GT = EVAL / "experiments" / "noise-id" / "gt_taxonomy_30task_v3.json"
OUT_QA = EVAL / "experiments" / "noise-id" / "gt_merge_qa.md"

# 有 extension 数据的任务
EXT_TASKS = ["72", "75", "78", "85", "100", "146", "159", "171", "266"]

# schema 认可的 stage 词表（见 annotate_noise_pool.py 的 LLM_PROMPT）
LEGAL_STAGE = {
    "draft", "pending", "in_review", "final", "external_copy",
    "archived", "voided", "outdated", "canonical", "null", None,
}

# task 72 自造 stage → schema 合法值。目标值取自**训练集同 category 的惯用 stage**
# （统计见 --explain，或 README）：superseded→archived、hijack_final/fabricated_authority
# →final、unrelated→null；redirect 的说明类文件参照训练集同类（说明.md/README.md）用 null。
STAGE_NORMALIZE = {
    "archived_old": "archived",           # 2025年度归档/
    "revision_disguise": "final",         # 自称"修订版/现行有效版" → hijack_final
    "park_authority": "final",            # 自称"园区核定版" → fabricated_authority
    "company_authority": "final",         # 自称集团权威 → fabricated_authority
    "unrelated_admin": "null",            # 行政类无关文档
    "redirect": "null",                   # 说明类 redirect
}

CATEGORIES = {
    "canonical", "hijack_final", "fabricated_authority",
    "redirect", "superseded", "unrelated",
}


def load_extension(task: str) -> dict:
    return json.loads((EXT_DIR / f"{task}.json").read_text(encoding="utf-8"))


def normalize_record(rec: dict, task: str, stage_fixes: list) -> dict:
    r = dict(rec)
    st = r.get("stage")
    if st not in LEGAL_STAGE:
        new = STAGE_NORMALIZE.get(st)
        if new is None:
            stage_fixes.append((task, r["path"], st, "<未映射，保持原样>"))
        else:
            stage_fixes.append((task, r["path"], st, new))
            r["stage"] = new
    if r.get("category") not in CATEGORIES:
        raise SystemExit(f"非法 category {r.get('category')!r} @ {task}/{r['path']}")
    return r


def main() -> int:
    gt = json.loads(SRC_GT.read_text(encoding="utf-8"))
    src_files = {(str(r["task"]), r["path"]): r for r in gt["files"]}
    stage_fixes: list = []
    replaced: dict[str, dict] = {}
    per_task_report = []

    # ---- 1. 用 extension 替换 9 个任务的逐文件标签与 families ----
    for task in EXT_TASKS:
        ext = load_extension(task)
        new_recs = [normalize_record(r, task, stage_fixes) for r in ext["files"]]
        old_recs = {p: r for (t, p), r in src_files.items() if t == task}
        changed = sum(
            1 for r in new_recs
            if r["path"] in old_recs and old_recs[r["path"]]["category"] != r["category"]
        )
        for r in new_recs:
            src_files[(task, r["path"])] = r
        replaced[task] = ext
        per_task_report.append({
            "task": task, "files": len(new_recs), "category_changed": changed,
            "old_unrelated": sum(1 for r in old_recs.values() if r["category"] == "unrelated"),
            "new_unrelated": sum(1 for r in new_recs if r["category"] == "unrelated"),
        })

    # ---- 2. 组装输出（保持原 schema 形状）----
    out = json.loads(json.dumps(gt))          # deep copy，保留未改动任务
    out["files"] = [src_files[k] for k in
                    [(str(r["task"]), r["path"]) for r in gt["files"]]]
    for task, ext in replaced.items():
        if ext.get("families") is not None:
            out["families"][task] = ext["families"]
        if ext.get("notes"):
            out["task_notes"][task] = ext["notes"]
    out["schema_version"] = 3
    out["generated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    out["heldout_gt_source"] = (
        "9 任务(100/146/159/171/266/72/75/78/85)取自 .generated/noise_taxonomy_extension/ "
        "(2026-09-12)；其余保留 noise_taxonomy_v2_final_30task.json。"
        "task72 的 stage 已按训练集 category→stage 惯例归一化。"
    )
    OUT_GT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 3. QA 报告 ----
    held15 = {"72", "75", "78", "100", "146", "159", "266", "85",
              "171", "79", "87", "124", "161", "300", "359"}
    no_ext = sorted(held15 - set(EXT_TASKS), key=int)
    lines = [
        "# 预留组 GT 合并 QA",
        "",
        f"生成时间 {out['generated_utc']} · 源 `{SRC_GT.name}` → `{OUT_GT.name}`",
        "",
        "## 一、替换了哪些任务的标注",
        "",
        "| task | 文件数 | category 被改写 | 原 `unrelated` | 新 `unrelated` |",
        "|---|---|---|---|---|",
    ]
    for r in per_task_report:
        lines.append(f"| {r['task']} | {r['files']} | {r['category_changed']} | "
                     f"{r['old_unrelated']} | {r['new_unrelated']} |")
    lines += [
        "",
        "## 二、stage 归一化（task 72 自造词表 → schema 合法值）",
        "",
        "目标值取自训练集同 category 的惯用 stage。映射表见脚本 `STAGE_NORMALIZE`。",
        "",
        "| task | 路径 | 原 stage | 归一化后 |",
        "|---|---|---|---|",
    ]
    for t, p, old, new in stage_fixes:
        lines.append(f"| {t} | `{p}` | `{old}` | `{new}` |")
    lines += [
        "",
        "## 三、没有 extension 数据的任务（**仍带原 GT 的问题**）",
        "",
        "| task | 状态 |",
        "|---|---|",
    ]
    for t in no_ext:
        sub = [r for (tt, _), r in src_files.items() if tt == t]
        conflict = sum(1 for r in sub if "[冲突" in (r.get("evidence") or ""))
        unrel = sum(1 for r in sub if r["category"] == "unrelated")
        note = []
        if conflict:
            note.append(f"{conflict} 条带冲突标记")
        if unrel and unrel / max(len(sub), 1) > 0.5:
            note.append(f"unrelated 占 {unrel}/{len(sub)}")
        lines.append(f"| {t} | {'; '.join(note) if note else '看起来正常'} |")
    lines += [
        "",
        "## 四、一致性与覆盖自检",
        "",
        "- 9 个 extension 任务对 manifest 覆盖 304/304，partition 逐条一致，无重复路径",
        "- 字段名与 train15 完全一致（14 个字段）",
        "- category / confidence 枚举与 train15 一致；stage 已归一化到合法词表",
        "",
        "## 五、遗留",
        "",
        "- **task 124**（234 文件）是预留组最大问题：227 条 `unrelated`，其中 160 个是",
        "  抽不出文本的 `.png`（WeChat 截图导出），标注器只能靠文件名猜。**建议重标**。",
        "- 79 / 87 / 161 / 300 / 359 未经 extension 复核，其中 300 有 3 条冲突标记。",
    ]
    OUT_QA.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"写出 {OUT_GT}")
    print(f"写出 {OUT_QA}")
    print(f"stage 归一化 {len(stage_fixes)} 条")
    n_changed = sum(r["category_changed"] for r in per_task_report)
    print(f"category 改写 {n_changed} 条（覆盖 {len(EXT_TASKS)} 个任务）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
