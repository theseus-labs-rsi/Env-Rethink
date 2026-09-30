#!/usr/bin/env python3
"""gen_noise_id_heldout_v2.py — 生成 heldout-v2 批次的 noise-id 子环境。

为什么需要这个驱动：
`gen_noise_id_heldout.py` 是唯一「非破坏性」的子环境生成入口（增量物化、
index.json 合并写、SID 段区分批次）；但它的批次常量（NEW_SEED / BATCH_TAG /
SID_OFFSET）是模块级全局，没有 CLI 开关。本驱动 import 它并覆盖这三个全局后
转调 `main()`——生成逻辑本身完全复用，不重写。

⚠️ 绝不要改用 `generate_noise_id_subenvs.py`：它的 `_has_downstream_results()`
当前为 True，无参数会 SystemExit，加 `--reset` 会 rmtree 整个 GEN_ROOT
（连带 239 个既有子环境、`_sft/trajectories.jsonl` 训练集、`_ocr/cache.json`）。

批次划分（SID 段互不重叠）：
  - 原始批次     : `<task>-001..0NN`（seed 20260904）
  - heldout-v1   : `<task>-101..102`（SID_OFFSET=100）
  - heldout-v2   : `<task>-201..`   （SID_OFFSET=200，本脚本）

用法：
  python3 scripts/gen_noise_id_heldout_v2.py --per-task 4 --dry-run
  python3 scripts/gen_noise_id_heldout_v2.py --per-task 4
  python3 scripts/gen_noise_id_heldout_v2.py --clean        # 只删 heldout-v2 批次
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_noise_id_heldout as h  # noqa: E402

# ---- 批次常量覆盖（main() 在运行期读这些模块全局）----
h.NEW_SEED = "20260915-heldout-v2"
h.BATCH_TAG = "heldout-v2"
h.SID_OFFSET = 200
# g.ALLOCATIONS 保持 15 个原始任务不变（这正是要的评估母集）

if __name__ == "__main__":
    h.main()
