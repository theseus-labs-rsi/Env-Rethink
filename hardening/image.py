"""题目镜像：从每题的 `environment/Dockerfile` 推导，再叠上 agent 层。

**agent 运行时那一半在 `agentkit/images.py`**（基座镜像 + overlay），这里只负责 TB 题目侧：

- `ensure_task_image()` —— 用 `runtime/make-task-image.py` 从题目自己的 Dockerfile 推导出
  一个镜像 Dockerfile（换 FROM 为底座、烘入判分依赖与参考解依赖、**绝不 COPY tests/**），
  再 docker build。顺带产出 `task-layouts/<task>.yaml`（容器路径映射）。
- `task_agent_image()` —— 题目镜像 + agent 层（调 agentkit 的 `build_overlay`）。

评测必须用后者的产物（agent 要活在题目环境里）；生成不需要 —— 生成只读 /task + /workflow。
"""

from __future__ import annotations

import subprocess

from pathlib import Path

import config as C
from agentkit import build_overlay, image_exists


def _run(cmd: list[str], *, timeout: int = 3600, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
    )


def ensure_task_image(
    task: str,
    *,
    task_dir: str | Path | None = None,
    tag: str = C.AGENT_BASE_TAG,
    build: bool = True,
    name: str | None = None,
) -> str:
    """保证题目镜像存在。

    不经过 `runtime/build-task-images.sh`：那个脚本把构建上下文写死成
    `tasks/<t>/environment`，对 `tasks-tb21/` 和变体的 `build/` 都不对。这里自己拼：

        ① python3 runtime/make-task-image.py --task-dir <src> --name <display>
              → 生成 runtime/task-images/<display>.Dockerfile + task-layouts/<display>.yaml
        ② docker build -f 那个 Dockerfile -t <image> <src>/environment

    `src` 是题目目录（种子题 `tasks-tb21/<task>`，或变体的 `<vid>/build`）。
    """
    display = name or task
    image = C.default_task_image(display, tag)
    if image_exists(image):
        return image
    if not build:
        raise FileNotFoundError(f"题目镜像不存在：{image}（并且 build=False）")

    src = Path(task_dir) if task_dir else (C.tasks_root() / task)
    if not (src / "environment" / "Dockerfile").is_file():
        raise FileNotFoundError(f"题目环境 Dockerfile 不存在：{src / 'environment/Dockerfile'}")

    gen = _run(
        ["python3", str(C.RUNTIME / "make-task-image.py"),
         "--task-dir", str(src), "--name", display],
        timeout=600, cwd=C.HERE,
    )
    if gen.returncode != 0:
        raise RuntimeError(f"make-task-image 推导失败（{display}）：\n{gen.stdout[-1500:]}\n{gen.stderr[-1500:]}")

    df = C.RUNTIME / "task-images" / f"{display}.Dockerfile"
    if not df.is_file():
        raise RuntimeError(f"没有生成 Dockerfile：{df}\n{gen.stdout[-1500:]}")

    proc = _run(
        ["docker", "build", "-f", str(df), "-t", image, str(src / "environment")],
        timeout=3600, cwd=C.HERE,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"题目镜像构建失败（{display}）：\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}\n"
            f"提示：FROM 替换导致 OS/Python 漂移时用 --keep-from 手写 Dockerfile"
        )
    return image


def task_agent_image(
    task: str,
    *,
    task_dir: str | Path | None = None,
    tag: str = C.AGENT_BASE_TAG,
    build: bool = True,
    rebuild: bool = False,
) -> str:
    """评测要用的镜像：题目镜像 + agent 层。"""
    base_task_image = ensure_task_image(task, task_dir=task_dir, tag=tag, build=build)
    return build_overlay(base_task_image, name=task, tag=tag, rebuild=rebuild)
