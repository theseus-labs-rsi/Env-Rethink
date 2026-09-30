"""容器内的约定路径。各模块共用同一套布局，agent 层才可能跨模块复用。"""

from __future__ import annotations

CONTAINER = {
    # 题目 / 生成侧
    "task": "/task",
    "workflow": "/workflow",
    "out": "/tb/out",
    # 题目环境（TB 约定）
    "app": "/app",
    "shared": "/shared",
    "tests": "/tests",
    "solution": "/solution",
    "workspace": "/workspace",
    # 运行与产物
    "verifier": "/logs/verifier",
    "agent_logs": "/logs/agent",
    "agent_home": "/opt/tb-agent",
    "prompt": "/tmp/tb-prompt.txt",
    "agent_script": "/tmp/tb-run-agent.sh",
}
