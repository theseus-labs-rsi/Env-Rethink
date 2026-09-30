"""agent 运行时的镜像：基座 + 把它叠到任意镜像上。

- `build_agent_base()` —— 构建 agent 基座（父镜像 + claude code + codex + 网关整形代理）。
- `build_overlay()`   —— `COPY --from=<基座> /opt/tb-agent /opt/tb-agent`。

**为什么用 overlay 而不是重建**：`COPY --from` 是纯文件复制，不关心目标镜像用什么包管理器、
也不重跑它的 Dockerfile，所以对任意已有镜像都成立，代价是一层而不是一次完整重建。
唯一的隐含约束是 glibc —— 基座与目标镜像同源于一个基础镜像时天然一致。

镜像仓库前缀由 `TB_IMAGE_REPO` 决定（默认空 = 只用本地镜像名，不指向任何 registry）。
"""

from __future__ import annotations

import os
import subprocess

from pathlib import Path

HERE = Path(__file__).resolve().parent

IMAGE_REPO = os.environ.get("TB_IMAGE_REPO", "").rstrip("/")
AGENT_BASE_NAME = os.environ.get("TB_AGENT_BASE_NAME", "tb-agent-base")
AGENT_BASE_TAG = os.environ.get("TB_BASE_TAG", "20260916")


def img(name: str) -> str:
    """给镜像名加上 registry 前缀（没配前缀就是裸名）。"""
    return f"{IMAGE_REPO}/{name}" if IMAGE_REPO else name


def agent_base_image(tag: str = AGENT_BASE_TAG) -> str:
    return f"{AGENT_BASE_NAME}:{tag}"


#: 当前 tag 下的基座镜像名（模块级常量，方便 `from agentkit import AGENT_BASE_IMAGE_NAME`）
AGENT_BASE_IMAGE_NAME = agent_base_image()


def overlay_image_name(name: str, tag: str = AGENT_BASE_TAG) -> str:
    """`<name>` 的 overlay 镜像名（agent 层叠上去之后的产物）。"""
    return f"{img(f'tb-agent-{name}')}:{tag}"


OVERLAY_DOCKERFILE = """# 由 agentkit/images.py 生成：目标镜像 + agent 层
ARG TASK_IMAGE
FROM ${TASK_IMAGE}
ARG AGENT_BASE_IMAGE
COPY --from={agent_base} /opt/tb-agent /opt/tb-agent
ENV PATH=/opt/tb-agent/node/bin:$PATH \\
    IS_SANDBOX=1 \\
    CODEX_HOME=/opt/tb-agent/codex-home \\
    TB_AGENT_HOME=/opt/tb-agent
"""


def _run(cmd: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def image_exists(image: str) -> bool:
    return _run(["docker", "image", "inspect", image], timeout=60).returncode == 0


def build_agent_base(
    *,
    tag: str = AGENT_BASE_TAG,
    base_from: str | None = None,
    claude_version: str = "2.1.187",
    codex_version: str = "0.157.0",
    rebuild: bool = False,
) -> str:
    """构建 agent 基座镜像；已存在且 rebuild=False 时直接复用。

    `base_from` 是父镜像。默认从环境变量 `TB_AGENT_BASE_FROM` 取，再不行由调用方给。
    """
    name = agent_base_image(tag)
    if image_exists(name) and not rebuild:
        return name
    parent = base_from or os.environ.get("TB_AGENT_BASE_FROM", "") or "python:3.13-slim"
    cmd = [
        "docker", "build",
        "-f", str(HERE / "Dockerfile.agent-base"),
        "-t", name,
        "--build-arg", f"TB_AGENT_BASE_FROM={parent}",
        "--build-arg", f"CLAUDE_CODE_VERSION={claude_version}",
        "--build-arg", f"CODEX_VERSION={codex_version}",
        str(HERE),
    ]
    proc = _run(cmd, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"agent 基座镜像构建失败：\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return name


def build_overlay(
    task_image: str,
    *,
    name: str,
    tag: str = AGENT_BASE_TAG,
    agent_base: str | None = None,
    rebuild: bool = False,
) -> str:
    """把 agent 层叠到 `task_image` 上，返回新镜像名。"""
    agent_base = agent_base or build_agent_base(tag=tag)
    out = overlay_image_name(name, tag)
    if image_exists(out) and not rebuild:
        return out

    df = HERE / ".generated" / f"overlay-{name}.Dockerfile"
    df.parent.mkdir(parents=True, exist_ok=True)
    df.write_text(OVERLAY_DOCKERFILE.format(agent_base=agent_base), encoding="utf-8")

    cmd = [
        "docker", "build",
        "-f", str(df),
        "-t", out,
        "--build-arg", f"TASK_IMAGE={task_image}",
        "--build-arg", f"AGENT_BASE_IMAGE={agent_base}",
        str(HERE),
    ]
    proc = _run(cmd, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"overlay 镜像构建失败（{task_image} → {out}）：\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
    return out
