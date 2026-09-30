#!/usr/bin/env python3
"""rework_noise_id.py — 对失败 env：审计 agent 分析轨迹 vs 真值，产出定向 fix hint，供重 rollout。

用法：
  python scripts/rework_noise_id.py --limit 10               # 从 fail 集取前 N
  python scripts/rework_noise_id.py --envs 108-007,160-010   # 指定
产物：<sid>/agentic/rework/fix.md（审计分析 + 定向修复提示，可点名本 env 文件）

重 roll 时（exporter/runner）会把 fix.md 追加进 prompt（仅用于该 env 的矫正，不是训练 hint）。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noise_id_common as nc
import check_read_fidelity as crf

STRONG = {"hijack_final", "fabricated_authority", "redirect"}


def load_truth(sid):
    return {f["path"]: f for f in json.load(open(nc.GEN_ROOT / sid / "labels.json", encoding="utf-8"))["files"]}


def pick_fail_envs(limit):
    pool = json.load(open(nc.GEN_ROOT / "_sft" / "subset_candidates.json", encoding="utf-8"))
    fails = sorted(s for s, v in pool.items() if not v.get("usable"))
    return fails if not limit else fails[:limit]


def find_raw(sid):
    """在持久化 raw 中找一个 agent.json（run1/run2/...）。"""
    for d in sorted((nc.GEN_ROOT / "_sft").glob("raw_*/" + sid)):
        aj = d / "agent.json"
        if aj.exists():
            return aj
    return None


def trace_summary(aj):
    """把 executionTrace 压成简洁文本：读过的文件 + 主要动作。"""
    try:
        r = json.load(open(aj, encoding="utf-8"))
    except Exception:
        return "(trace 不可读)"
    ev = r.get("trace", {}).get("executionTrace") or r.get("trajectory") or []
    reads, cmds = [], []
    for e in ev:
        if not isinstance(e, dict) or e.get("type") != "tool":
            continue
        if e.get("tool") == "Read":
            reads.append(str(e.get("input", {}).get("file_path") or "")[-40:])
        elif e.get("tool") in ("Bash", "bash"):
            cmds.append(str(e.get("input", {}).get("command") or "")[:70])
    return f"Read({len(reads)}): {reads[:6]}…\nBash({len(cmds)}): {cmds[:4]}…"


def teacher_output(sid):
    out = nc.GEN_ROOT / "_sft"
    for d in sorted(out.glob("raw_*/" + sid)):
        p = d / "noise_labels.json"
        if p.exists():
            try:
                parsed = json.load(open(p, encoding="utf-8"))
                return {f.get("path"): f for f in (parsed.get("files") or []) if isinstance(f, dict)}
            except Exception:
                pass
    return {}


def audit_and_fix(sid) -> str:
    truth = load_truth(sid)
    aj = find_raw(sid)
    pred = teacher_output(sid)
    lines = []
    lines.append(f"env: {sid}")
    lines.append("真值(部分):")
    for p in sorted(truth)[:40]:
        t = truth[p]
        lines.append(f"  - {p} :: {t['partition']} / {t.get('category')}")
    if aj:
        lines.append("\n轨迹摘要:\n" + trace_summary(aj))
    else:
        lines.append("\n(trace 缺失)")
    lines.append("\nteacher 输出(部分):")
    for p in sorted(pred)[:40]:
        f = pred[p]
        lines.append(f"  - {p} :: {f.get('partition')} / {f.get('category')}")
    # 不一致样例（teacher 判 noise 与真值不同，或没读到的）
    diffs = []
    for p, t in sorted(truth.items()):
        if p in pred and pred[p].get("category") != t.get("category") and t["partition"] == "noise":
            diffs.append(f"判错 {p}: 真={t.get('category')} teacher={pred[p].get('category')}")
    if len(diffs) > 12:
        diffs = diffs[:12] + [f"...共 {len(diffs)} 处"]
    if diffs:
        lines.append("\n主要判错:")
        lines += ["  " + d for d in diffs]
    prompt = "\n".join(lines) + """
\n请以“审计 agent”身份分析这个 env：teacher（DeepSeek agent）的判定为什么与真值不一致，或为什么没有读全。
- 指出具体差距：哪些类型(伪权威/伪终版/被取代/无关/扫描件…)它常判错；是否有文件没读到/没 OCR。
- 输出一段【定向修复提示】（中文，≤240 字，可以点名该 env 的文件/路径，指导：该读哪些、怎么读(如扫描件用 ocr_dump)、强诱饵怎么判）。
只输出修复提示本身，不要其他解释。"""

    resp = nc.chat_once({"messages": [{"role": "user", "content": prompt}]})
    if isinstance(resp, str):
        fix = resp.strip()
    elif isinstance(resp, dict):
        fix = str(resp.get("text") or resp).strip()
    else:
        fix = str(resp).strip()
    if len(fix) > 600:
        fix = fix[:600]
    return fix


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--envs", default="")
    args = ap.parse_args()
    sids = [x.strip() for x in args.envs.split(",") if x.strip()] if args.envs else pick_fail_envs(args.limit)
    print(f"审计 {len(sids)} 个失败 env: {sids}")
    for sid in sids:
        try:
            fix = audit_and_fix(sid)
        except Exception as e:  # noqa: BLE001
            print(f"[err] {sid}: {e}")
            continue
        d = nc.GEN_ROOT / sid / "agentic" / "rework"
        d.mkdir(parents=True, exist_ok=True)
        (d / "fix.md").write_text(f"# {sid} rework fix（审计生成）\n\n{fix}\n", encoding="utf-8")
        print(f"[ok] {sid}: fix={fix[:90]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
