#!/usr/bin/env python3
"""retry_dead_batches.py — 自动重跑构造中仍无产出的批次，直到全部拿到 labels。

背景：构造有约 10% 的批次因**模型侧偶发**（畸形工具调用 / turns=0 秒回）失败，
与环境无关，只能靠重试兜。每轮约能救回 70-80%。

用法：
  python3 retry_dead_batches.py                 # 算死批 → 建并发配置 → 启动
  python3 retry_dead_batches.py --dry-run       # 只算不跑
  python3 retry_dead_batches.py --parallel 4    # 并发路数（默认 2）

前置：环境变量里配好模型端点（见 README.md），即
`ENV_RETHINK_BASE_URL` / `ENV_RETHINK_API_KEY` 等；且已执行过
`prepare --curator <curator>`，本脚本以它生成的实验 yaml 为模板。

模型是**一个 API 端点**，不是多副本部署，所以并发路数由 `--parallel` 显式给，
不做实例发现。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

EVAL = Path(__file__).resolve().parent            # 本模块（curate/）
BATCH_ROOT = EVAL / ".generated" / "curate_batches"
RUNS = EVAL / "experiments"

sys.path.insert(0, str(EVAL))
from curate_workspace import (  # noqa: E402
    BACKEND,
    MODEL_CURATOR_NAMES,
    MODEL_CURATORS,
    RUN_EXPERIMENT,
)


def collect_exps() -> list[Path]:
    """所有可能含产出的实验目录（顺序无关：有 labels 即算覆盖）。"""
    exps: list[Path] = []
    # 用通配而不是逐个枚举前缀——曾经因为漏写一个前缀导致产出被漏计。
    for d in sorted(RUNS.glob("curate-*")):
        if d.is_dir():
            exps.append(d)
    return exps


def dead_batches() -> list[str]:
    """按**文件覆盖**判断死批，而不是按批次 id。

    一个批次算"已覆盖" = 它的每个 target_path 都能在某个实验目录的产出里找到 label。
    这样把大批次拆成子批次跑也能正确计入（子批次产出同样带 target_path）。
    """
    covered: set[str] = set()
    for e in collect_exps():
        for f in e.glob("cases/task*/agent/output/noise_labels.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for x in d.get("files", []) or []:
                if isinstance(x, dict) and x.get("path"):
                    covered.add(str(x["path"]).lstrip("./"))
    dead = []
    for p in sorted(BATCH_ROOT.glob("*-b*")):
        if not p.is_dir():
            continue
        try:
            man = json.loads((p / "metadata.json").read_text(encoding="utf-8"))["data_manifest"]
        except Exception:  # noqa: BLE001
            continue
        if any(str(e.get("target_path", "")).lstrip("./") not in covered for e in man):
            dead.append(p.name)
    return dead


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--curator", default=MODEL_CURATOR_NAMES[0],
                    choices=MODEL_CURATOR_NAMES)
    ap.add_argument("--parallel", type=int, default=2,
                    help="并发路数（把死批轮转切分，默认 2）")
    ap.add_argument("--attempts", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dead = dead_batches()
    total = len([p for p in BATCH_ROOT.glob("*-b*") if p.is_dir()])
    print(f"批次 {total} | 已有产出 {total - len(dead)} | 死批 {len(dead)}")
    if not dead:
        print("✅ 全部批次均已产出，无需重跑")
        return 0
    if args.dry_run:
        print("死批:", dead)
        return 0

    template = RUNS / f"curate-{args.curator}-{BACKEND}.yaml"
    if not template.is_file():
        sys.exit(f"缺少 {template}；先执行 prepare --curator {args.curator}")
    tmpl = yaml.safe_load(template.read_text(encoding="utf-8"))

    n = max(1, args.parallel)
    groups = [dead[i::n] for i in range(n)]
    launched = []
    for i, g in enumerate(groups, 1):
        if not g:
            continue
        c = json.loads(json.dumps(tmpl))
        # 名字里保留 curator 与 backend 段，status 的 glob 才列得到这批重试跑
        c["name"] = f"curate-{args.curator}-{BACKEND}-retry{i}"
        c["task_ids"] = g
        c["agent"]["attempts"] = args.attempts
        cfg = Path(f"/tmp/curate_retry_{i}.yaml")
        cfg.write_text(yaml.safe_dump(c, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        launched.append((i, len(g)))

    env = dict(os.environ)
    # 端点 host 追加进 EXTRA_NO_PROXY，与 cmd_run 同一套处理（见那边的注释）
    base_url = str(tmpl.get("agent", {}).get("base_url")
                   or MODEL_CURATORS[args.curator]["base_url"])
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host:
        existing = [x for x in env.get("EXTRA_NO_PROXY", "").split(",") if x]
        if host not in existing:
            existing.append(host)
        env["EXTRA_NO_PROXY"] = ",".join(existing)

    for i, k in launched:
        log = open(f"/tmp/curate_retry_{i}.log", "w")
        subprocess.Popen(
            [sys.executable, "-u", str(RUN_EXPERIMENT),
             "--config", f"/tmp/curate_retry_{i}.yaml"],
            cwd=EVAL, stdout=log, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
        print(f"  启动 retry{i}  {k} 批")
    print(f"本轮共启动 {len(launched)} 组，覆盖 "
          f"{sum(k for _, k in launched)} 个死批")
    return 0


if __name__ == "__main__":
    sys.exit(main())
