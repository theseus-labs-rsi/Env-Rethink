#!/usr/bin/env python3
"""整合所有已落地产出的合格轨迹为统一清单（每 env 多 rep/多 run 并入，去重）。

来源：
  - 远程后端 full/full2/fix/augcal（subset_candidates.json 判定可用的 env，每 env 1 条）
  - deepseek many500（_runs/<sid>/repN.status.json 合格）
  - 远程后端 rep1（raw_远程后端_rep1/<sid>/，读全评估 usable）
  - gemini batch（_gemruns/<sid>/gemN.status.json 合格，全部 rep）
  - 远程后端 教师批次（raw_远程后端_<tag>/）：/tmp runtime 或 persistent cases
    双路收割；case 名 task<sid>[-r<NN>]，rep1 落 <sid>/ 平铺（兼容旧结构），
    rep>=2 落 <sid>-r<NN>/；同 tag 的新波次用新 tag 名（如 deepseek2）隔离
    skip-if-exists 语义下的 rep1 覆盖冲突。
产：.generated/noise_id_subenvs/_sft/trajectories.jsonl
"""
import json, os, glob, re, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import noise_id_common as nc
import check_read_fidelity as crf

GEN = nc.GEN_ROOT
EXP_ROOT = nc.EVAL_ROOT / "experiments"
STRONG = {"hijack_final", "fabricated_authority", "redirect"}
OUT = GEN / "_sft" / "trajectories.jsonl"
# case 名 task<sid>（repeat=1）或 task<sid>-r<NN>（repeat>1）
CASE_RE = re.compile(r"^task(?P<sid>.+?)(?:-r(?P<rep>\d+))?$")


def parse_case(case: str):
    m = CASE_RE.match(case)
    if not m:
        return None
    return m.group("sid"), int(m.group("rep") or 1)


def truth(sid):
    return {f["path"]: f for f in json.load(open(GEN / sid / "labels.json", encoding="utf-8"))["files"]}


def usable_via_subset(sid, trace_path, pred_path):
    try:
        aud = crf.audit(sid, trace_path=trace_path)
    except Exception:
        return None
    pred = {}
    if pred_path and pred_path.exists():
        try:
            pred = {f.get("path"): f for f in (json.load(open(pred_path)).get("files") or []) if isinstance(f, dict)}
        except Exception:
            pass
    tr = truth(sid)
    S = []
    for p, v in (aud.get("per_file") or {}).items():
        if not (v.get("read") and v.get("faithful")):
            continue
        if p not in pred or p not in tr:
            continue
        ok = tr[p]["partition"] == "standard" and pred[p].get("partition") == "standard" \
             or tr[p]["partition"] == "noise" and pred[p].get("partition") == "noise" and pred[p].get("category") == tr[p].get("category")
        if ok:
            S.append(p)
    has_std = any(tr[p]["partition"] == "standard" for p in S)
    ts = [p for p in tr if tr[p]["partition"] == "noise" and tr[p].get("category") in STRONG]
    s_ok = (not ts) or any(tr[p].get("category") in STRONG for p in S)
    return (has_std and s_ok and len(S) >= 3, len(S)) if S else (False, 0)


def emit(recs, sid, run, model, source, trace_dir, extra=None):
    d = dict(extra or {})
    d.update({"subenv_id": sid, "run": run, "model": model, "source": source,
              "trace_dir": trace_dir})
    recs.append(d)


def main():
    recs = []
    # 1) subset_candidates（历史每 env 1 条；source 标 subset）
    sub = json.load(open(GEN / "_sft" / "subset_candidates.json", encoding="utf-8"))
    for sid, v in sub.items():
        if not v.get("usable"):
            continue
        emit(recs, sid, v.get("run", "raw"), "deepseek", "subset", None)
    # 2) deepseek many500 (_runs rep 合格)
    for p in glob.glob(str(GEN / "_runs" / "*" / "rep*.status.json")):
        sid = os.path.basename(os.path.dirname(p))
        rep = os.path.basename(p).split(".")[0]
        try:
            ok = json.load(open(p)).get("qualified")
        except Exception:
            ok = False
        if ok:
            emit(recs, sid, rep, "deepseek", "many500", str(GEN / "_runs" / sid))
    # 3) 远程后端 rep1 (raw_远程后端_rep1)
    r1 = GEN / "_sft" / "raw_远程后端_rep1"
    if r1.is_dir():
        for sid in sorted(os.listdir(r1)):
            aj = r1 / sid / "agent.json"
            nj = None
            for nm in ("noise_labels_rep1.json", "noise_labels.json"):
                if (r1 / sid / nm).exists():
                    nj = r1 / sid / nm
                    break
            if not aj.exists():
                continue
            u = usable_via_subset(sid, aj, nj)
            if u and u[0]:
                emit(recs, sid, "rep1", "deepseek", "远程后端_rep1", str(r1 / sid))
    # 4) gemini batch (_gemruns 全部 rep 合格；文件名有 gemN 与 many500 的 repN 两种)
    for p in glob.glob(str(GEN / "_gemruns" / "*" / "*.status.json")):
        sid = os.path.basename(os.path.dirname(p))
        rep = os.path.basename(p).split(".")[0]
        try:
            ok = json.load(open(p)).get("qualified")
        except Exception:
            ok = False
        if ok:
            emit(recs, sid, rep, "gemini-3.7-flash", "gemini_batch", str(GEN / "_gemruns" / sid))
    # 5) 远程后端 教师批次（raw_远程后端_<tag>）：持久化（若未做）并逐文件子集判 usable
    MODEL_MAP = {"deepseek": "deepseek-v4-flash", "dshpro": "deepseek-v4-pro",
                 "gemini": "gemini-3.7-flash", "glm": "glm-5.3", "glmflash": "glm-5.3-flash",
                 # r2 波次（2026-09-08 起，repeat=5，目标累计 2k 合格轨迹）
                 "deepseek2": "deepseek-v4-flash", "dshpro2": "deepseek-v4-pro",
                 "glm2": "glm-5.3", "glmflash2": "glm-5.3-flash",
                 # r3 补轮（2026-09-08 拉起，repeat=2，补 2k 缺口）
                 "deepseek3": "deepseek-v4-flash", "dshpro3": "deepseek-v4-pro"}
    for tag, model in MODEL_MAP.items():
        rdir = GEN / "_sft" / f"raw_远程后端_{tag}"
        # 收割源两路：/tmp runtime agent_output（运行中/未清理时优先、含 attempts）
        # 与 persistent cases（最终产物，/tmp 清理后仍可整合）。
        # merge 语义：按 (sid, rep) 补齐缺失的 agent.json/noise_labels.json，
        # 不因目录已存在而跳过——否则批次未跑完时的部分持久化会永久阻断补齐。
        sources = []  # (sid, rep, agent_case_dir, output_dir)
        for base in sorted(glob.glob(
                f"/tmp/workspace-bench-noise-id-agentic-远程后端-{tag}-*/"
                "evaluation/experiments/远程后端_suite/agent_output")):
            for case in sorted(os.listdir(base)):
                parsed = parse_case(case)
                if not parsed:
                    continue
                sid, rep = parsed
                subs = glob.glob(f"{base}/{case}/*/{sid}")
                if subs:
                    d = Path(subs[-1])
                    sources.append((sid, rep, d, d / "output"))
        for croot in sorted(glob.glob(str(EXP_ROOT / f"noise-id-agentic-远程后端-{tag}-*"))):
            cdir = Path(croot) / "cases"
            if not cdir.is_dir():
                continue
            for case in sorted(os.listdir(cdir)):
                parsed = parse_case(case)
                if not parsed:
                    continue
                sid, rep = parsed
                a = cdir / case / "agent"
                if (a / "agent.json").is_file():
                    sources.append((sid, rep, a, a / "output"))
        for sid, rep, d, outp in sources:
            key = sid if rep == 1 else f"{sid}-r{rep:02d}"
            out = rdir / key
            out.mkdir(parents=True, exist_ok=True)
            if (d / "agent.json").is_file() and not (out / "agent.json").exists():
                shutil.copy(d / "agent.json", out / "agent.json")
            # 输出文件名：优先 <tag> 后缀，再 noise_labels
            if not (out / "noise_labels.json").exists():
                for nm in (f"noise_labels_{tag}.json", "noise_labels.json"):
                    op = outp / nm
                    if op.is_file():
                        shutil.copy(op, out / "noise_labels.json")
                        break
        if not rdir.is_dir():
            continue
        for entry in sorted(os.listdir(rdir)):
            # 条目名：<sid>（r1，旧平铺结构）或 <sid>-r<NN>；用 labels.json 存在性消歧
            m2 = re.fullmatch(r"(.+)-r(\d+)", entry)
            if m2 and (GEN / m2.group(1) / "labels.json").is_file():
                sid, rep = m2.group(1), int(m2.group(2))
            else:
                sid, rep = entry, 1
            aj = rdir / entry / "agent.json"
            nj = rdir / entry / "noise_labels.json"
            if not aj.exists() or not nj.exists():
                continue
            u = usable_via_subset(sid, aj, nj)
            if u and u[0]:
                emit(recs, sid, f"{tag}-r{rep}", model, f"远程后端_{tag}", str(rdir / entry))
    # 写
    with open(OUT, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    by_src = Counter(r["source"] for r in recs)
    by_model = Counter(r["model"] for r in recs)
    by_task = Counter(r["subenv_id"].split("-")[0] for r in recs)
    print("整合轨迹数:", len(recs))
    print("按 source:", dict(by_src))
    print("按 model:", dict(by_model))
    print("按 task:", dict(sorted(by_task.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
