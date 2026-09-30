#!/usr/bin/env python3
"""assemble_sft_candidates.py — 汇总全部 agentic rollout 结果，按放宽阈值筛 SFT 候选集。

数据源：
  1) 本地 agentic（GEN_ROOT/<id>/agentic/agentic_status.json 合格 + score.json + report.finalText）
  2) 远程后端 runs（/tmp/workspace-bench-noise-id-agentic-*/…/agent_output/task<sid>/…/<sid>/
     agent.json + output/noise_labels.json），对不在本地合格的 env 导入到
     GEN_ROOT/<id>/agentic/远程后端_<run>/ 持久化。

保留阈值（放宽版）：读全（coverage=1.0 且 faithful=n_files）且
  划分 F1≥0.85 & standard 误杀==0 & 强诱饵 recall（有则）≥0.8。
产：evaluation/.generated/noise_id_subenvs/_sft/sft_candidates.json + gap 统计打印。
"""
import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_read_fidelity as crf
import noise_id_common as nc
import run_noise_id_rollout as rn

GEN = nc.GEN_ROOT
RUN_GLOBS = [
    "/tmp/workspace-bench-noise-id-agentic-远程后端-full-*",
    "/tmp/workspace-bench-noise-id-agentic-远程后端-medium-*",
    "/tmp/workspace-bench-noise-id-agentic-远程后端-validation-*",
    "/tmp/workspace-bench-noise-id-agentic-远程后端-ocrmock-*",
]
OUT_DIR = GEN / "_sft"
THRESH = {"f1": 0.85, "strong_recall": 0.8}


def relaxed_ok(sc) -> bool:
    p = sc.get("partition", {})
    if p.get("noise_f1") is None:
        return False
    if p["noise_f1"] < THRESH["f1"] or p.get("std_miskill") != 0:
        return False
    sr = sc.get("strong_recall")
    if sr is not None and sr < THRESH["strong_recall"]:
        return False
    return True


def host_meta(sid):
    idx = json.load(open(GEN / "index.json"))
    m = idx.get(sid, {})
    return {"task": m.get("parent_task"), "tier": m.get("tier"),
            "hint": m.get("hint"), "n_files": m.get("n_files")}


def local_candidates():
    """返回 {sid: {sc, qual_read, aud, final_json}}，只看 agentic_status 合格且有 score。"""
    out = {}
    for stp in GEN.glob("*/agentic/agentic_status.json"):
        sid = stp.parent.parent.name
        st = json.load(open(stp))
        if not st.get("qualified"):
            continue
        adir = stp.parent
        scp = adir / "score.json"
        if not scp.exists():
            continue
        sc = json.load(open(scp))
        if "partition" not in sc:
            continue
        out[sid] = {"sc": sc, "origin": ["local"], "adir": str(adir)}
    return out


def 远程后端_candidates():
    """扫描各 run 的 agent_output；返回 {sid: {sc, aud, run, adir}}。"""
    out = {}
    for g in RUN_GLOBS:
        for run in sorted(Path("/tmp").glob(g.split("/")[-1])):
            base = run / "evaluation" / "experiments" / "远程后端_suite" / "agent_output"
            if not base.is_dir():
                continue
            for td in sorted(base.glob("task*-*")):
                sid = td.name[len("task"):]
                subs = sorted(td.glob("*/" + sid))
                if not subs:
                    continue
                d = subs[-1]
                aj = d / "agent.json"
                out_json = d / "output" / "noise_labels.json"
                if not aj.exists() or not out_json.exists():
                    continue
                try:
                    aud = crf.audit(sid, trace_path=aj)
                except Exception:
                    continue
                if not nc.qualified_predicate(aud):
                    continue
                try:
                    parsed = json.load(open(out_json))
                    sc = rn.score_one(sid, GEN / sid, {"parsed": parsed})
                except Exception:
                    continue
                out[sid] = {"sc": sc, "origin": [run.name[:60]],
                            "adir": str(d), "aud": aud}
    return out


def main():
    cand = {}
    for sid, c in local_candidates().items():
        cand[sid] = c
    for sid, c in 远程后端_candidates().items():
        if sid in cand and any(o == "local" for o in cand[sid]["origin"]):
            continue  # 本地已有合格，保留本地稳定路径
        cand[sid] = c

    retained, dropped = [], []
    for sid, c in sorted(cand.items()):
        sc = c["sc"]
        ok = relaxed_ok(sc)
        rec = {**host_meta(sid),
               "subenv_id": sid,
               "origin": c["origin"],
               "quality": {
                   "noise_f1": sc["partition"].get("noise_f1"),
                   "std_miskill": sc["partition"].get("std_miskill"),
                   "strong_hit": sc.get("strong_hit"),
                   "strong_recall": sc.get("strong_recall"),
                   "category_acc": sc.get("category_acc"),
                   "timeline_tau": sc.get("timeline_tau"),
               },
               "trace_dir": c["adir"]}
        if ok:
            retained.append(rec)
        else:
            dropped.append({**host_meta(sid), "subenv_id": sid,
                            "reason": "read_ok_f1%.2f" % (sc["partition"].get("noise_f1") or 0)
                            if sc["partition"].get("std_miskill") == 0
                            else "read_ok_miskill%d" % sc["partition"].get("std_miskill")})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "sft_candidates.json").write_text(
        json.dumps(retained, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"候选(保留) {len(retained)}；读全但质量不达标(丢) {len(dropped)}")
    print("\n按任务保留数：")
    by_t = Counter(r["task"] for r in retained)
    for t in sorted(by_t):
        print(f"  {t}: {by_t[t]}")
    print("\n丢(读全但质量) by task:", dict(Counter(d["task"] for d in dropped)))
    print("\n保留候选：")
    for r in retained:
        q = r["quality"]
        print(f"  {r['subenv_id']:9s} {r['task']:>3s} {r['tier']:1s}/{r['hint']:2s} "
              f"F1={q['noise_f1']} miskill={q['std_miskill']} strong={q['strong_hit']} τ={q['timeline_tau']} {r['origin'][0][:20]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
