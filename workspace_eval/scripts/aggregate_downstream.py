#!/usr/bin/env python3
"""aggregate_downstream.py — 汇总下游实验的 (模型 × 条件 × 任务) 分数。

口径（关键，曾经踩过坑）：
  * 同一个 (模型, 条件, 任务) 可能在**多个 run 目录**里跑过（补跑 / 重跑 / 拆分）。
    必须先跨目录取并集，再按策略选一个值——**绝不能"取任务数最多的那个目录"**。
  * 选择策略 `--policy`：
      latest      取时间戳最新的 run（默认，反映最新工作区）
      max_passed  取 passed 最大的 run（判分失败只会静默记 0、不会加分，
                  故取最大可恢复被判分污染的真实值）
  * `claude-opus-5` 永久排除。

用法:
    python3 scripts/aggregate_downstream.py
    python3 scripts/aggregate_downstream.py --policy max_passed --tasks 72,78
    python3 scripts/aggregate_downstream.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
RUNS = EVAL / "experiments"

SLUG_TO_MODEL = {
    "dshflash": "deepseek-v4-flash",
    "dsv41flash": "deepseek-v4.1-flash",
    "sol": "gpt-5.6-sol",
    "luna": "gpt-5.6-luna",
    "glm53": "glm-5.3",
    "qwen38flash": "qwen3.8-flash",
    "qwen38max": "qwen3.8-max-cc",
    "hy3max": "hy3-max",
    "hy3high": "hy3-high",
    "gemini38flash": "gemini-3.8-flash",
}
EXCLUDED = {"opus5", "opus", "claudeopus5"}      # 用户要求永久排除

# run 目录的前缀（不同批次用了不同前缀，漏一个就会把该批任务当成"没跑"）
RUN_PREFIXES = ("hard-v4-", "ext9-fill-", "ext9-", "ext6-")

CONDITION_PATTERNS = [
    ("curated-gt", "gt"), ("curated-env-rethink", "env-rethink"),
    ("curated-qwen", "qwen"), ("curated-rule", "rule"),
    (r"(?:^|-)noise", "noise"), (r"(?:^|-)clean", "clean"),
]

RUN_NAME_RE = re.compile(
    r"^(?P<slug>[a-z0-9]+?)-(?P<rest>.+?)-(?P<git>[0-9a-f]{8})-"
    r"(?P<ts>20\d{6}T\d{6}Z)$"
)

# 补救跑用了另一套命名，别漏（漏了会把判分污染值当最终结果）：
#   rerun-sol-ca-t72-<git>-<ts>Z       单任务补救
#   rerun2-dshflash-base-t94-<git>-<ts>Z
#   rerun-hard-v4-<slug>-max-<cond>-<git>-<ts>Z   整集补救
RERUN_SINGLE_RE = re.compile(
    r"^rerun\d*-(?P<slug>[a-z0-9]+?)-(?P<cond>[a-z0-9\-]+?)-t(?P<task>\d+)-"
    r"(?P<git>[0-9a-f]{8})-(?P<ts>20\d{6}T\d{6}Z)$"
)


def parse_run_dir(name: str):
    """run 目录名 -> (model, condition, ts, task_hint)。解析不出来返回 None。

    task_hint 非空表示该 run 只覆盖这一个任务。
    """
    # 先剥掉 rerun 前缀，整集补救与正常 run 用同一套解析
    base = re.sub(r"^rerun\d*-", "", name)
    for prefix in RUN_PREFIXES:
        if not base.startswith(prefix):
            continue
        m = RUN_NAME_RE.match(base[len(prefix):])
        if not m:
            continue
        slug = m.group("slug")
        if slug in EXCLUDED:
            return None
        rest = m.group("rest")
        for pat, c in CONDITION_PATTERNS:
            if re.search(pat, rest):
                return SLUG_TO_MODEL.get(slug, slug), c, m.group("ts"), None
        return None

    m = RERUN_SINGLE_RE.match(name)
    if m:
        slug = m.group("slug")
        if slug in EXCLUDED:
            return None
        cond = None
        for pat, c in CONDITION_PATTERNS:
            if re.search(pat, m.group("cond")):
                cond = c
                break
        if cond is None:
            cond = m.group("cond")
        return SLUG_TO_MODEL.get(slug, slug), cond, m.group("ts"), m.group("task")
    return None


def build_all(task_filter: set[str] | None) -> dict:
    """(model, cond, task) -> [(ts, record), ...]（含全部 run，不预先挑选）。"""
    acc: dict[tuple[str, str, str], list] = defaultdict(list)
    dirs = []
    for pat in ("hard-v4-*", "rerun*", "ext9-*", "ext6-*"):
        dirs += list(RUNS.glob(pat))
    for d in sorted(set(dirs)):
        if not d.is_dir():
            continue
        parsed = parse_run_dir(d.name)
        if parsed is None:
            continue
        f = d / "summary.json"
        if not f.is_file():
            continue
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rows, list):
            continue
        model, cond, ts, task_hint = parsed
        for r in rows:
            t = str(r.get("task_id", "")).strip()
            if not t.isdigit() or str(r.get("status")) != "judged":
                continue
            if task_hint is not None and t != task_hint:
                continue
            if task_filter is not None and t not in task_filter:
                continue
            acc[(model, cond, t)].append(
                (ts, {"passed": int(r["passed"]), "total": int(r["total"]),
                      "run": d.name, "run_ts": ts}))
    for k in acc:
        acc[k].sort()
    return acc


def pick(recs: list, policy: str):
    if policy == "max_passed":
        return max(recs, key=lambda x: (x[1]["passed"], x[0]))
    return recs[-1]


def summarise(acc, models, conds, policy) -> dict:
    out = {}
    for model in models:
        out[model] = {}
        for cond in conds:
            tasks = sorted(t for (m, c, t) in acc if m == model and c == cond)
            if not tasks:
                continue
            P = T = 0
            detail = {}
            for t in tasks:
                _, r = pick(acc[(model, cond, t)], policy)
                P += r["passed"]
                T += r["total"]
                detail[t] = {"passed": r["passed"], "total": r["total"],
                             "pct": round(r["passed"] / r["total"] * 100, 1) if r["total"] else 0.0,
                             "run": r["run"], "run_ts": r["run_ts"],
                             "n_runs": len(acc[(model, cond, t)])}
            out[model][cond] = {
                "n_tasks": len(tasks), "passed": P, "total": T,
                "pct": round(P / T * 100, 1) if T else 0.0, "tasks": detail}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="")
    ap.add_argument("--models", default="deepseek-v4-flash,gpt-5.6-sol")
    ap.add_argument("--conds", default="env-rethink,qwen,gt")
    ap.add_argument("--policy", default="latest", choices=["latest", "max_passed"])
    ap.add_argument("--json", default="")
    ap.add_argument("--detail", action="store_true", help="逐任务打印")
    args = ap.parse_args()

    tf = {t.strip() for t in args.tasks.split(",") if t.strip()} or None
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    conds = [c.strip() for c in args.conds.split(",") if c.strip()]

    acc = build_all(tf)
    res = summarise(acc, models, conds, args.policy)

    print(f"策略: {args.policy}")
    print(f"{'model':>20} {'cond':>6} {'tasks':>6} {'passed':>8} {'total':>7} {'pct':>8}")
    for m in models:
        for c in conds:
            v = res.get(m, {}).get(c)
            if not v:
                print(f"{m:>20} {c:>6} {'--':>6}")
                continue
            print(f"{m:>20} {c:>6} {v['n_tasks']:>6} {v['passed']:>8} {v['total']:>7} "
                  f"{v['pct']:>7.1f}%")
        print()
    if args.detail:
        for m in models:
            for c in conds:
                v = res.get(m, {}).get(c)
                if not v:
                    continue
                print(f"--- {m} / {c} ---")
                for t, d in sorted(v["tasks"].items(), key=lambda x: int(x[0])):
                    print(f"   task{t:>4}: {d['passed']:>3}/{d['total']:<4} "
                          f"{d['pct']:>5.1f}%  runs={d['n_runs']}  {d['run_ts']}")
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"写到 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
