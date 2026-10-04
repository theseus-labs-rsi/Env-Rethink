#!/usr/bin/env python3
"""构建 Workspace-Bench 专用 agent 运行时镜像。

复用 agentkit 自己的 ``build_overlay()``（不另写 overlay Dockerfile），
只是把 agent 基座换成与题目镜像同源的那条（见 Dockerfile.agent-base-wb）。

用法（仓库根下）：
    python3 agentkit/build_wb_agent_image.py

产物：``tb-agent-wseval:20260916-wb`` —— 实验配置里用
``WS_AGENT_IMAGE`` 指过去即可（见 workspace_eval/src/agents/agentkit.py）。
"""

from __future__ import annotations

import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agentkit.images import build_overlay  # noqa: E402  必须在 sys.path 调整之后

#: 题目镜像（agent 要在它上面跑，才有 docx/xlsx/libreoffice 那套工具）
TASK_IMAGE = "workspace-bench:local"
#: 与题目镜像同源的 agent 基座（bookworm / glibc 2.36）
AGENT_BASE = "tb-agent-base:20260916-wb"
#: 产物用独立的 tag，避免和 TB 那条链的 20260916 混淆
TAG = "20260916-wb"


def main() -> int:
    out = build_overlay(
        TASK_IMAGE, name="wseval", tag=TAG, agent_base=AGENT_BASE
    )
    print(f"agent 运行时镜像：{out}")
    print(f"    题目镜像：{TASK_IMAGE}")
    print(f"    agent 基座：{AGENT_BASE}")
    print(f"\n实验 YAML 里设：WS_AGENT_IMAGE={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
