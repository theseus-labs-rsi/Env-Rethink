"""agentkit —— 各模块共用的「在一个容器里跑 agent」的运行时。

    from agentkit import DockerRuntime, FileItem, build_harness, gateway_from_env

分三层：

  · **容器**   `DockerRuntime`（docker exec + tar 管道）、`FileItem`、目录读取
  · **agent**  `build_harness` → `ClaudeCodeHarness` / `CodexHarness`，在容器里非交互跑
  · **接线**   `Gateway` / `gateway_from_env`（模型连接）、`CONTAINER`（容器内路径）、
               `build_agent_base` / `build_overlay`（agent 运行时镜像）

容器内还带一个**网关整形代理**（`gwshim.py`，由 `tb_agent_env.sh` 幂等启动）：
claude code 会请求 `/v1/messages?beta=true`，而部分网关的 Messages 路由带 query 就拒，
代理把 query 丢掉再逐字节透传。网关没这个问题可设 `TB_AGENT_GATEWAY_SHIM=0` 关掉。
"""

from .agents import (
    AgentHarness,
    AgentResult,
    ClaudeCodeHarness,
    CodexHarness,
    build_harness,
)
from .docker_runtime import (
    CommandResult,
    DockerRuntime,
    DownloadResponse,
    FileItem,
    read_local_directory_to_file_items,
)
from .gateway import (
    Gateway,
    agent_protocol,
    canonical_agent,
    gateway_from_env,
    resolve_gateway,
)
from .images import (
    AGENT_BASE_IMAGE_NAME,
    AGENT_BASE_NAME,
    AGENT_BASE_TAG,
    IMAGE_REPO,
    build_agent_base,
    build_overlay,
    image_exists,
    img,
    overlay_image_name,
)
from .paths import CONTAINER

__all__ = [
    # 容器
    "DockerRuntime", "FileItem", "CommandResult", "DownloadResponse",
    "read_local_directory_to_file_items",
    # agent
    "build_harness", "AgentHarness", "AgentResult", "ClaudeCodeHarness", "CodexHarness",
    # 接线
    "Gateway", "resolve_gateway", "gateway_from_env", "agent_protocol", "canonical_agent",
    "CONTAINER",
    # 镜像
    "build_agent_base", "build_overlay", "overlay_image_name", "image_exists",
    "AGENT_BASE_NAME", "AGENT_BASE_TAG", "AGENT_BASE_IMAGE_NAME", "IMAGE_REPO", "img",
]
