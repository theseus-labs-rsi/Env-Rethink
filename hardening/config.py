"""hardening 的配置：本模块自己的路径 + 复用 agentkit 的运行时接线。

**共享的运行时**（容器操作、两个 agent 的启动、模型连接、agent 运行时镜像）都在
`agentkit/` 里。这里把用得着的那几个**再导出**，好让本模块其它文件继续写 `C.xxx`。
"""

from __future__ import annotations

import os
import sys

from pathlib import Path

# agentkit 在仓库根下
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import agentkit                                                  # noqa: E402
from agentkit import (                                           # noqa: E402,F401
    AGENT_BASE_IMAGE_NAME,
    CONTAINER,
    Gateway,
    agent_protocol,
    build_agent_base,
    build_overlay,
    canonical_agent,
    gateway_from_env,
    image_exists,
    overlay_image_name,
    resolve_gateway,
)

HERE = Path(__file__).resolve().parent           # hardening/
PREFIX = HERE.parent                             # env-rethink/
PROJECT = PREFIX.parent                          # 上层目录

# ── 本模块的目录 ──────────────────────────────────────────────────────
#
#   tools/              装配 / 机械闸 / 事件校验 / 哈希对账 / 答案清单
#   runtime/            题目镜像推导 + 镜像底座 Dockerfile
#   schema/             事件行 canonical schema
#   skills/             加难方法（主 skill + 四条轴）
#   pipelines/hardening/方法档：pipeline.md + noise-taxonomy.md
#   tasks-tb21/         种子题语料（Terminal-Bench 2.1，59 题）
#   runs/               变体与产物（默认 gitignore）
TOOLS = HERE / "tools"
RUNTIME = HERE / "runtime"
SCHEMA = HERE / "schema"
SKILLS = HERE / "skills"
HARDENING = HERE / "pipelines/hardening"
CACHE = HERE / ".cache"                          # 上传 staging / answers-digest 等临时件


def _data_root(env_name: str, default: Path, label: str) -> Path:
    raw = os.environ.get(env_name)
    path = Path(raw).expanduser().resolve() if raw else default
    if label == "题目" and not path.is_dir():
        raise FileNotFoundError(
            f"种子题目录不存在：{path}\n"
            f"用 --tasks-root <path> 或 export {env_name}=<path> 指到题目目录。"
        )
    return path


def tasks_root() -> Path:
    """种子题目录。默认就是本模块自带的 `tasks-tb21/`。"""
    return _data_root("TB_TASKS_ROOT", HERE / "tasks-tb21", "题目")


def runs_root() -> Path:
    """变体与产物目录。默认 `runs/`，不存在就建。"""
    path = _data_root("TB_RUNS_ROOT", HERE / "runs", "产物")
    path.mkdir(parents=True, exist_ok=True)
    return path


def event_schema() -> Path:
    """事件行的 canonical schema。"""
    return Path(os.environ.get("TB_EVENT_SCHEMA") or (SCHEMA / "context-event-log-canonical-schema.json"))


# ── 镜像（TB 题目侧）──────────────────────────────────────────────────
# agent 运行时那部分的镜像名/构建在 agentkit 里；这里只管"题目镜像"。

AGENT_BASE_NAME = agentkit.AGENT_BASE_NAME
AGENT_BASE_TAG = agentkit.AGENT_BASE_TAG
AGENT_BASE_IMAGE = AGENT_BASE_IMAGE_NAME

# 题目镜像的底座（runtime/build-base.sh 的产物）
WB_BASE_IMAGE = os.environ.get("TB_BASE_IMAGE", "") or f"{agentkit.img('wb-tb-base')}:{AGENT_BASE_TAG}"


def default_task_image(task: str, tag: str = AGENT_BASE_TAG) -> str:
    """题目自己的镜像名（runtime/make-task-image.py 的产物）。"""
    return f"{agentkit.img(f'tb-{task}')}:{tag}"
