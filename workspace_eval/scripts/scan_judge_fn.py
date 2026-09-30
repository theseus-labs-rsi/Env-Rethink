#!/usr/bin/env python3
"""scan_judge_fn.py — 扫描下游 run 里的「判分假阴性」。

判分器失败时会**静默把 rubric 记成 0 分**，不留失败状态（case 仍是 `judged`），
所以只能从 judge evidence 的措辞反推。

判据（踩过的坑都在这里）：
  * evidence 里出现 `candidate_output` **且**含"不存在/为空/no output/does not exist"
    → 判分器没看到候选产出目录。
    ⚠️ 必须**中英双语**：早期只匹配中文，漏掉了英文的
    "Candidate output directory ... does not exist"，导致一批假阴性没被发现。
    ⚠️ 也不能只匹配整句：同一 case 里措辞会变（"目录不存在" / "目录中不存在输出文件" /
    "目录为空"），所以用「candidate_output + 否定词共现」而不是固定短语。
  * `Autocompact is thrashing` → 判分器自身上下文溢出。

单靠 evidence 还不够，必须交叉**本地产出文件数**：
  有产出 + 命中判分失败特征 → 假阴性；无产出 → 多半是 agent 真的失败了。

用法:
    python3 scripts/scan_judge_fn.py                # 只报告
    python3 scripts/scan_judge_fn.py --emit-retries # 生成补救命令
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

EVAL = pathlib.Path(__file__).resolve().parents[1]

DEFECT = re.compile(
    r"does not exist|no output files? (were|was) delivered|is empty|not found"
    r"|不存在|为空|未找到|Autocompact is thrashing", re.I)


def is_fn(ev: str) -> bool:
    low = ev.lower()
    return ("candidate_output" in low or "candidateoutput" in low) and bool(DEFECT.search(ev))


def latest_valid() -> dict:
    """(model, cond, task) -> 最新 run 是否已有"非判分失败"的结果。

    扫描是逐 run 目录做的，而补救跑写在新目录里；不交叉引用的话，
    已补救成功的 case 会一直出现在清单里（早期版本就有这个毛病）。
    """
    sys.path.insert(0, str(EVAL / "scripts"))
    import aggregate_downstream as A  # noqa: PLC0415
    out = {}
    for (m, c, t), recs in A.build_all(None).items():
        ts, r = A.pick(recs, "latest")
        out[(m, c, t)] = (ts, r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="20260917T030000", help="只看此时间戳之后的 run")
    ap.add_argument("--min-hit", type=float, default=0.25, help="命中率阈值")
    ap.add_argument("--emit-retries", action="store_true")
    args = ap.parse_args()

    rows = []
    freshest = latest_valid()
    for f in sorted(EVAL.glob("experiments/hard-v4-*/cases/task*/status.json")):
        d = f.parent.parent.parent.name
        if "opus5" in d or "ABANDONED" in d:
            continue                      # opus5 用户要求永久排除
        if d[-16:-1] < args.since:
            continue
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if str(s.get("status")) != "judged":
            continue
        # 判分产物优先读 case 自己的 judge/ 目录。status.json 里的 judge_result
        # 指向 /tmp 下的 runtime 路径，runtime 一被回收就读不到了（踩过）。
        jf = s.get("judge_result")
        cands = sorted((case := f.parent).glob("judge/rubrics_judge*.json"))
        if jf:
            cands = [pathlib.Path(jf)] + cands if pathlib.Path(jf).is_file() else cands
        j = None
        for cp in cands:
            p = pathlib.Path(cp)
            if not p.is_file():
                continue
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:  # noqa: BLE001
                continue
        if not j:
            continue
        rub = j.get("rubrics") or []
        if not rub:
            continue
        hits = sum(1 for r in rub if is_fn(str(r.get("evidence", ""))))
        if not hits:
            continue
        case = f.parent
        od = case / "agent" / "output"
        nf = sum(1 for q in od.rglob("*") if q.is_file()) if od.is_dir() else 0
        js = s.get("judge_summary") or {}

        # 已有更新的 run 覆盖了这个 (模型, 条件, 任务) → 这条记录已作废
        import aggregate_downstream as _A  # noqa: PLC0415
        parsed = _A.parse_run_dir(d)
        superseded = False
        if parsed:
            model, cond, ts, _ = parsed
            fresh = freshest.get((model, cond, case.name.replace("task", "")))
            superseded = bool(fresh and fresh[0] > ts)

        rows.append({
            "run": d, "case": case.name, "task": case.name.replace("task", ""),
            "passed": js.get("passed"), "total": js.get("total"),
            "hit_pct": round(hits / len(rub) * 100), "n_files": nf,
            "superseded": superseded,
        })

    rows.sort(key=lambda r: (r["superseded"], -r["hit_pct"]))
    print(f"{'run':<44} {'case':<9} {'得分':>9} {'命中%':>6} {'产出':>5} 判定")
    retry: dict[str, list[str]] = {}
    n_open = 0
    for r in rows:
        strong = r["hit_pct"] >= args.min_hit * 100 and r["n_files"] > 0
        zero = (r["passed"] == 0) and r["n_files"] > 0
        if r["superseded"]:
            verdict = "✅已补救"
        elif strong:
            verdict = "🔴假阴性"
            n_open += 1
        elif zero:
            verdict = "🟠可疑(0分有产出)"
            n_open += 1
        else:
            verdict = "🟡真失败"
        print(f"{r['run'].replace('-ca8366ad','')[:44]:<44} {r['case']:<9} "
              f"{str(r['passed'])+'/'+str(r['total']):>9} {r['hit_pct']:>5}% {r['n_files']:>5} {verdict}")
        if not r["superseded"] and (strong or zero):
            retry.setdefault(r["run"], []).append(r["task"])

    print(f"\n合计 {len(rows)} 个 case；其中待补救 {n_open} 个"
          f"（已补救 {sum(1 for r in rows if r['superseded'])} 个）")
    if args.emit_retries:
        print("\n# 补救命令:")
        for run, tasks in sorted(retry.items()):
            cfg = EVAL / "experiments" / (run.rsplit("-", 2)[0] + ".yaml")
            print(f"python3 scripts/retry_downstream_case.py "
                  f"--yaml {cfg.relative_to(EVAL)} --task {','.join(sorted(set(tasks), key=int))} "
                  f"--tag {run.rsplit('-',2)[0].replace('hard-v4-','')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
