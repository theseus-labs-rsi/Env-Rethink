#!/usr/bin/env python3
"""retry_downstream_case.py — 对单个 (模型配置, 任务) 做定向补救跑。

两类需要补救的情况：
  * **判分假阴性**：judge evidence 出现「candidate_output 目录不存在」/「Autocompact is
    thrashing」——判分器自身失败，会**静默记 0 分**。识别信号：gt < base 或 base/ca
    出现异常的 0 分。
  * **agent 秒退**：`status=error turns=0`、耗时 <1s —— 模型网关瞬时问题，重跑一般即好。

用法:
    python3 scripts/retry_downstream_case.py --yaml <源 yaml> --task 108 --tag sol-base-108
    python3 scripts/retry_downstream_case.py --yaml x.yaml --task 108,357 --tag mix --dry-run

产物：<源yaml同目录>/<stem>-r<task>.yaml，然后自动后台启动。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

EVAL = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", required=True, help="源实验配置")
    ap.add_argument("--task", required=True, help="任务号，逗号分隔")
    ap.add_argument("--tag", required=True, help="日志/命名用标签")
    ap.add_argument("--parallelism", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.yaml)
    if not src.is_file():
        src = EVAL / args.yaml
    if not src.is_file():
        sys.exit(f"找不到配置: {args.yaml}")
    tasks = [t.strip() for t in args.task.split(",") if t.strip()]

    img = subprocess.run(
        ["docker", "images", "--no-trunc", "--format", "{{.ID}}", "workspace-bench:local"],
        capture_output=True, text=True).stdout.strip()
    if not img:
        sys.exit("拿不到 workspace-bench:local 镜像 ID")

    c = yaml.safe_load(src.read_text(encoding="utf-8"))
    base_name = c.get("name", src.stem)
    c["name"] = f"{base_name}-r{'-'.join(tasks)}"
    c["task_ids"] = tasks
    c["parallelism"] = args.parallelism
    rt = c.get("runtime") or {}
    if rt.get("expected_image_id") != img:
        rt["expected_image_id"] = img          # 这个坑踩过 4 次
    c["runtime"] = rt

    out = src.parent / f"{src.stem}-r{'-'.join(tasks)}.yaml"
    out.write_text(yaml.safe_dump(c, allow_unicode=True, sort_keys=False,
                                  default_flow_style=False), encoding="utf-8")
    print(f"写出 {out}")
    print(f"  model={c.get('agent', {}).get('model')} harness={c.get('agent', {}).get('harness')} "
          f"tasks={tasks} par={args.parallelism} image={img[:20]}")
    if args.dry_run:
        return 0

    log = Path("/tmp") / f"retry_{args.tag}.log"
    with log.open("w") as fh:
        subprocess.Popen(
            ["python3", "-u", "scripts/run_experiment.py", "--config", str(out)],
            cwd=EVAL, stdout=fh, stderr=subprocess.STDOUT,
            env=dict(os.environ), start_new_session=True,
        )
    print(f"启动，日志 {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
