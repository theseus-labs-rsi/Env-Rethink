#!/usr/bin/env python3
"""create_eval.py — 把子环境 workspace 导出为评估用伪任务。

挑出 `.generated/noise_id_subenvs/` 下名字以给定后缀结尾的子环境目录，
每个导出一个独立的伪任务（metadata.json + data/）。

用法：
  python3 create_eval.py --suffix <后缀> [--subenvs <目录>] [--out <目录>]

不加 `--suffix` 则取全部子环境。后缀用来圈定某一批子环境（例如某次构造实验
产出的那批），因为子环境目录以 `<sid>-<后缀>` 命名。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent            # 本模块（curate/）

sys.path.insert(0, str(EVAL))
from export_noise_id_pseudo_tasks import (  # noqa: E402
    READ_GUIDE,
    _HINT_MARK,
    strip_teaching,
)
from noise_id_hints import HINTS_DIR  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--subenvs", default=str(EVAL / ".generated" / "noise_id_subenvs"),
                    help="子环境根目录")
    ap.add_argument("--out", default="",
                    help="产物目录（默认 .generated/subenv_eval）")
    ap.add_argument("--suffix", default="",
                    help="只挑名字以此结尾的子环境目录；不给则全取")
    args = ap.parse_args()

    gen = Path(args.subenvs)
    out = Path(args.out) if args.out else EVAL / ".generated" / "subenv_eval"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    hint = (HINTS_DIR / "L1.md").read_text(encoding="utf-8")
    prompt = strip_teaching(hint + "\n\n" + _HINT_MARK + "\n" + READ_GUIDE)

    pattern = f"*-{args.suffix}" if args.suffix else "*"
    made = []
    for d in sorted(gen.glob(pattern)):
        if not d.is_dir():
            continue
        sid = d.name
        ws = d / "workspace"
        task_out = out / sid
        data = task_out / "data"
        data.mkdir(parents=True)
        manifest = []
        for f in sorted(ws.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(ws)
            shutil.copy2(f, data / rel.name)
            manifest.append({
                "filename": rel.name,
                "stored_relpath": "data/" + rel.name,
                "target_path": str(rel),
                "input_role": "noise",
            })
        meta = {
            "id": sid,
            "language": "cn",
            "file_system": "dataseed",
            "task": prompt,
            "output_files": ["noise_labels.json"],
            "rubrics": ["占位"],
            "rubric_types": ["占位"],
            "data_manifest": manifest,
        }
        (task_out / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1)
        )
        made.append(sid)
    print(len(made), "eval tasks ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
