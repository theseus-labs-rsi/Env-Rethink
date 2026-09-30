#!/usr/bin/env python3
"""tasks_hard_v4 噪声池标注流水线（noise-id v2，任务无关口径）。

四层流水（优先级：report > llm > rule > manifest）：

1. manifest 层：partition 硬约束（input_role: standard|noise）；
   standard 文件 category=canonical，任何上层不得覆盖；
2. rule 层：文件名/路径状态标记 → stage（观测主张）与 category 预判；
3. report 层：report_overrides.json 中人工摘录的强诱饵标注（最高优先级）；
4. llm 层：对规则层置信不足的文件，由 DeepSeek-V4-Flash 盲读内容
   （不告知 partition）提案 category/stage/date_anchor/subject。

共享常量/网关 client/内容抽取/数据装载见 noise_id_common.py。
标签 schema 与类别定义见 evaluation/docs/noise_id_subenvs.md §2。

用法：
    # 只跑确定性层（manifest+rule），输出覆盖统计
    python annotate_noise_pool.py --rules-only

    # 冒烟 LLM 层（5 个文件）
    python annotate_noise_pool.py --llm --llm-limit 5

    # 全量（LLM 层补齐低置信文件，8 并发）
    source evaluation/.env && python annotate_noise_pool.py --llm --concurrency 8

    # 只做交叉校验
    python annotate_noise_pool.py --validate
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from noise_id_common import (  # noqa: E402
    CATEGORIES, EVAL_ROOT, TASK_ROOT, chat_once, extract_json, extract_text,
    find_override, load_manifest,
)

DEFAULT_OUTPUT = EVAL_ROOT / "experiments" / "noise-id" / "noise_taxonomy_v2.json"
DEFAULT_OVERRIDES = EVAL_ROOT / "experiments" / "noise-id" / "report_overrides.json"

# 12 核心 + 4 修复后备选（129/334/94/161，2026-09-04 并入全量标注）
CORE_TASKS = ["374", "357", "108", "314", "291", "154", "160", "372", "207", "258", "267", "288", "129", "334", "94", "161"]

# ---------------------------------------------------------------- 规则层

# 文件名（而非路径）中的状态标记：stage 为文件“声称”的状态
FILENAME_STAGE_MARKERS = [
    ("external_copy", ["副本", "copy", "外发"]),
    ("voided", ["作废", "已作废"]),
    ("pending", ["待确认"]),
    ("in_review", ["处理中", "复核", "审查中"]),
    ("draft", ["草稿", "初稿", "_draft"]),
    ("archived", ["已归档", "_归档", "归档1", "归档2", "归档3"]),
    ("final", ["_final", "final_", "（最终版）", "最终版", "_Final", "FINAL", "_V7", "_v7"]),
]
# 路径（含文件名）中的结构线索：仅作为 stage 弱信号
PATH_STAGE_HINTS = [
    ("external_copy", ["下载/", "副本/"]),
]
AUTHORITY_MARKERS = ["APPROVED", "批准", "审批通过", "会签", "已核定", "回签"]
REDIRECT_MARKERS = ["README", "口径", "更正说明", "执行说明", "先读"]

STAGE_TO_CATEGORY = {
    "draft": "superseded",
    "pending": "superseded",
    "in_review": "superseded",
    "voided": "superseded",
    "external_copy": "superseded",
    "archived": "superseded",
    "outdated": "superseded",
    # final：噪声文件自称 final，可能是 hijack_final / superseded 副本 / unrelated，
    # 一律交 LLM 判定
}


def apply_rules(entry: dict) -> dict:
    """返回 rule 层提案：{stage, stage_source, category, confidence, escalate}。"""
    filename = entry["filename"]
    path = entry["target_path"]
    proposal = {"stage": None, "stage_source": None, "category": None,
                "confidence": None, "escalate": False}

    stage = None
    for st, markers in FILENAME_STAGE_MARKERS:
        if any(m in filename for m in markers):
            stage = st
            break
    stage_source = "filename"
    if stage is None:
        for st, markers in PATH_STAGE_HINTS:
            if any(m in path for m in markers):
                stage, stage_source = st, "path"
                break
    proposal["stage"] = stage
    proposal["stage_source"] = stage_source

    if entry["partition"] == "standard":
        proposal["stage"] = "canonical"
        proposal["stage_source"] = "manifest"
        proposal["category"] = "canonical"
        proposal["confidence"] = "high"
        return proposal

    if stage in STAGE_TO_CATEGORY:
        proposal["category"] = STAGE_TO_CATEGORY[stage]
        # final 声称 + 副本/下载路径 → 外发副本（superseded），中置信
        if stage == "external_copy" and any(m in filename for m in ("_final", "FINAL", "Final")):
            proposal["confidence"] = "medium"
        else:
            proposal["confidence"] = "high"
        return proposal

    # 未命中状态标记：给低置信预判，交 LLM
    if any(m in filename for m in AUTHORITY_MARKERS) or any(m in path for m in AUTHORITY_MARKERS):
        proposal["category"] = "fabricated_authority"
        proposal["confidence"] = "low"
    elif any(m in filename for m in REDIRECT_MARKERS):
        proposal["category"] = "redirect"
        proposal["confidence"] = "low"
    proposal["escalate"] = True
    return proposal


# ---------------------------------------------------------------- LLM 层

LLM_PROMPT = """你在为“工作区噪声识别”数据集做标注。下面是一份来自模拟办公工作区的文件。
请只依据文件本身（路径、文件名、内容）判断其内在属性，输出 JSON。

类别定义（category，六选一）：
- canonical：可信正本。权威、完整、内部一致的正式版本；
- hijack_final：伪终版劫持。文件名或内容声称 final/最新/当前口径，但与自身内部数据或真实版本链矛盾；
- fabricated_authority：伪造权威证据。成套 FINAL/APPROVED/审批标记但无对应登记支撑；或篡改后伪装成“已核定/官方”的内容；或同对象平行包但权限/范围不成立；
- redirect：指令劫持。README/说明类文件，声称某正本有误、指示改用其他来源；
- superseded：被取代版本。草稿/待确认/处理中/复核/外发副本/归档/作废/旧年度版本——真实但非正本；
- unrelated：无关文档。工作区内其他主题/对象的正常文档。

注意：文件名中的状态词（final/最终版等）只是“主张”，不是事实；必须与内容核验。

输出 JSON（只输出 JSON，不要其他文字）：
{"category": "...", "stage": "draft|pending|in_review|final|external_copy|archived|voided|outdated|canonical|null",
 "date_anchor": "YYYY-MM-DD 或 null", "subject": "文档主题（12字内）",
 "evidence": "判断依据，引用文件内可见证据（60字内）"}

文件路径：{path}
文件名：{filename}
文件内容（可能截断）：
{content}"""


def llm_annotate(entry: dict, *, max_tokens: int = 4096, timeout: int = 180) -> dict:
    """盲标一份文件（不传入 partition）。返回 LLM 提案或错误信息。

    传输/重试由 noise_id_common.chat_once 负责（含 425、Retry-After）。
    """
    content = entry.get("_extracted")
    prompt = (LLM_PROMPT
              .replace("{path}", entry["target_path"])
              .replace("{filename}", entry["filename"])
              .replace("{content}", content if content else "[无法提取文本：二进制或扫描件]"))
    try:
        text = chat_once({"messages": [{"role": "user", "content": prompt}]},
                         max_tokens=max_tokens, timeout_seconds=timeout)
        data = extract_json(text)
        if data.get("category") not in CATEGORIES:
            raise ValueError(f"bad category: {data.get('category')}")
        return {"ok": True, **data}
    except Exception as exc:  # noqa: BLE001  RetryRequestError / 非 JSON / 类别非法统一记为失败
        return {"ok": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------- 主流程

def load_task_entries(task_id: str) -> list[dict]:
    man = load_manifest(task_id)["by_path"]
    entries = []
    for e in man.values():
        entries.append({
            "task": task_id,
            "filename": e["filename"],
            "stored_relpath": e["stored_relpath"],
            "target_path": e["target_path"],
            "partition": "standard" if e.get("input_role") == "standard" else "noise",
            "version_role": e.get("version_role"),
            "_abs": TASK_ROOT / task_id / e["stored_relpath"],
        })
    return entries


def merge_layers(entries: list[dict], overrides: dict, llm_results: dict | None) -> list[dict]:
    out = []
    for e in entries:
        rule = apply_rules(e)
        ov = find_override(overrides, e["task"], e["filename"], e["target_path"])
        llm = (llm_results or {}).get(f"{e['task']}/{e['filename']}")

        rec = {
            "task": e["task"], "filename": e["filename"], "path": e["target_path"],
            "stored_relpath": e["stored_relpath"], "partition": e["partition"],
            "category": None, "stage": None, "date_anchor": None,
            "family_id": None, "evidence": None, "subject": None,
            "source": None, "confidence": None, "needs_review": False,
        }
        # category：report > llm > rule；stage：report > rule > llm
        if ov:
            rec.update({k: v for k, v in ov.items() if k in rec and v is not None})
            rec["source"], rec["confidence"] = "report", "high"
        else:
            cat = None
            if llm and llm.get("ok"):
                cat = llm["category"]
                rec["source"], rec["confidence"] = "llm", "medium"
                rec["stage"] = rule["stage"] or llm.get("stage")
                rec["date_anchor"] = llm.get("date_anchor")
                rec["subject"] = llm.get("subject")
                rec["evidence"] = llm.get("evidence")
            if cat is None:
                cat = rule["category"]
                rec["source"] = "rule" if cat else "manifest"
                rec["confidence"] = rule["confidence"] or "low"
                rec["stage"] = rule["stage"]
            rec["category"] = cat
            if rule["escalate"] and not llm:
                rec["needs_review"] = True

        # 硬约束与冲突标记
        if e["partition"] == "standard":
            if rec["category"] != "canonical":
                rec["category"], rec["stage"] = "canonical", "canonical"
                rec["source"], rec["confidence"] = "manifest", "high"
                rec["needs_review"] = bool(ov or (llm and llm.get("ok")))
                if rec["needs_review"]:
                    rec["evidence"] = (rec["evidence"] or "") + " [冲突：上层标为非 canonical，按 manifest 修正]"
        elif rec["category"] == "canonical":
            rec["category"] = None if not ov else rec["category"]
            if not ov:
                rec["needs_review"] = True
                rec["evidence"] = (rec["evidence"] or "") + " [冲突：LLM 判为 canonical 但 manifest 为 noise]"
        if rec["category"] not in CATEGORIES:
            rec["category"] = "unrelated" if rec["partition"] == "noise" else "canonical"
            if rec["source"] in ("rule", "manifest"):
                rec["confidence"] = "low"
                rec["needs_review"] = True
        out.append(rec)
    return out


def validate(records: list[dict]) -> dict:
    by_task: dict[str, dict] = {}
    for r in records:
        t = by_task.setdefault(r["task"], {"files": 0, "standard": 0, "noise": 0,
                                           "by_source": {}, "by_category": {},
                                           "needs_review": 0, "no_content_category": 0})
        t["files"] += 1
        t[r["partition"]] += 1
        t["by_source"][r["source"]] = t["by_source"].get(r["source"], 0) + 1
        t["by_category"][r["category"]] = t["by_category"].get(r["category"], 0) + 1
        t["needs_review"] += int(r["needs_review"])
    return by_task


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", default=",".join(CORE_TASKS))
    ap.add_argument("--rules-only", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--llm-limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--overrides", default=str(DEFAULT_OVERRIDES),
                    help="report 层标注文件（默认种子；全量跑用 report_overrides_merged.json）")
    args = ap.parse_args()

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    overrides = {}
    if Path(args.overrides).is_file():
        overrides = json.loads(Path(args.overrides).read_text(encoding="utf-8"))

    all_entries = []
    for t in tasks:
        all_entries.extend(load_task_entries(t))

    llm_results = None
    if args.llm and not args.rules_only:
        if "APP_ID" not in os.environ:
            print("APP_ID/APP_KEY not set; source evaluation/.env first", file=sys.stderr)
            return 1
        # 需要 LLM 的文件：无 report 覆盖且（rule 置 escalate 或 category 为空/低置信）
        need = []
        for e in all_entries:
            if find_override(overrides, e["task"], e["filename"], e["target_path"]):
                continue
            rule = apply_rules(e)
            if rule["escalate"] or rule["category"] is None or rule["confidence"] != "high":
                need.append(e)
        if args.llm_limit:
            need = need[: args.llm_limit]
        # 断点续跑：加载增量 sidecar 中已成功的结果（失败条目不缓存，重跑即重试；
        # 同 key 多条时后写覆盖先写）
        sidecar = Path(str(args.output) + ".llm_results.jsonl")
        llm_results = {}
        if sidecar.is_file():
            for line in sidecar.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    if rec["result"].get("ok"):
                        llm_results[rec["key"]] = rec["result"]
            print(f"[llm] resume: {len(llm_results)} cached ok results from {sidecar.name}")
        todo = [e for e in need if f"{e['task']}/{e['filename']}" not in llm_results]
        print(f"[llm] {len(need)} files to annotate (of {len(all_entries)}), {len(todo)} todo")
        # 预抽取内容
        for e in todo:
            e["_extracted"] = extract_text(e["_abs"], loud=True)
        errors = 0
        sidecar_fp = sidecar.open("a", encoding="utf-8")
        lock = threading.Lock()

        def on_done(key: str, result: dict):
            nonlocal errors
            with lock:
                llm_results[key] = result
                if not result.get("ok"):
                    errors += 1
                sidecar_fp.write(json.dumps({"key": key, "result": result}, ensure_ascii=False) + "\n")
                sidecar_fp.flush()
                done = len(llm_results)
                if done % 10 == 0:
                    print(f"[llm] {done}/{len(need)} done, {errors} errors", flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = {}
            for e in todo:
                futs[pool.submit(llm_annotate, e)] = f"{e['task']}/{e['filename']}"
            for fut in concurrent.futures.as_completed(futs):
                on_done(futs[fut], fut.result())
        sidecar_fp.close()
        print(f"[llm] finished: {len(llm_results)} results, {errors} errors")

    records = merge_layers(all_entries, overrides, llm_results)
    report = validate(records)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tasks": tasks,
        "files": records,
        "families": {t: overrides.get(t, {}).get("families", []) for t in tasks},
        "task_notes": {t: overrides.get(t, {}).get("_note") for t in tasks
                       if overrides.get(t, {}).get("_note")},
    }
    if not args.validate:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[out] wrote {len(records)} records -> {out_path}")

    print(f"\n{'task':>5} {'files':>5} {'std':>4} {'noise':>5} | source(report/llm/rule/manifest) | needs_review")
    for t in tasks:
        r = report[t]
        s = r["by_source"]
        print(f"{t:>5} {r['files']:>5} {r['standard']:>4} {r['noise']:>5} | "
              f"{s.get('report',0):>3}/{s.get('llm',0):>4}/{s.get('rule',0):>4}/{s.get('manifest',0):>3} | {r['needs_review']:>3}")
    total_review = sum(r["needs_review"] for r in report.values())
    print(f"\ntotal needs_review: {total_review}/{len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
