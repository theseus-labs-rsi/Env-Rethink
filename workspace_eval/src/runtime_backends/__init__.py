"""运行后端：实验"在哪儿跑"的注册与加载。

实验 yaml 里的 `runtime.provider` 决定这一点：

| provider | 去哪儿 |
|---|---|
| 不写 / `local` | **内置**：本地 docker（`scripts/run_experiment.py` 自己） |
| 其它任何名字 | 从**插件**里找：`<plugin>/runtime_backends/<provider>.py` |

本仓库**只内置 `local`**。远程沙盒、集群调度这类后端与具体平台强绑定，
不进公开树；要用就装插件。

## 插件契约

插件目录由 `WSEVAL_RUNTIME_PLUGINS` 指定（冒号分隔的路径列表），
此外还会自动看这几个默认位置：

    <repo>/plugins                      （gitignore，本地放最方便）
    <repo>/../env-rethink-plugins       （仓库外，公开树之外）

目录里放：

    <plugin>/runtime_backends/<provider>.py

模块提供两个函数之一：

    def run(config_path: Path, *, validate_only: bool = False) -> int
        # 跑这个 provider 的实验；返回进程退出码

`run_experiment.py` 只在 `provider` 不是内置值时才加载插件 —— 所以没有插件时，
本地路径完全不受影响。
"""

from __future__ import annotations

import importlib.util
import os
import sys

from pathlib import Path
from types import ModuleType
from typing import Callable, Optional

ENV_VAR = "WSEVAL_RUNTIME_PLUGINS"

#: 内置后端名（不写 provider 也走这条）
BUILTIN = ("", "local", "docker")

# 本文件在 <repo>/workspace_eval/src/runtime_backends/ 下 → 仓库根是 parents[3]
_REPO = Path(__file__).resolve().parents[3]


def plugin_paths() -> list[Path]:
    """插件搜索路径：环境变量在前，默认位置在后。"""
    paths: list[Path] = []
    for chunk in os.environ.get(ENV_VAR, "").split(":"):
        chunk = chunk.strip()
        if chunk:
            paths.append(Path(chunk).expanduser())
    paths.append(_REPO / "plugins")
    paths.append(_REPO.parent / "env-rethink-plugins")
    seen, out = set(), []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def is_builtin(provider: str) -> bool:
    return str(provider or "").strip().lower() in BUILTIN


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"wseval_backend_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载插件模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_backend(provider: str) -> Optional[Callable[..., int]]:
    """按 provider 名找后端的 `run`。找不到返回 None（由调用方报错）。

    找不到时**不抛异常**，因为调用方要给出带搜索路径的可读错误。
    """
    name = str(provider or "").strip().lower().replace("-", "_")
    if not name or is_builtin(name):
        return None
    for root in plugin_paths():
        candidate = root / "runtime_backends" / f"{name}.py"
        if candidate.is_file():
            fn = getattr(_load_module(candidate), "run", None)
            if callable(fn):
                return fn
            raise ImportError(f"{candidate} 里没有 run()（见 runtime_backends 的插件契约）")
    return None


def load_auth_aliases() -> list[str]:
    """加载插件里的凭据方案别名（`<plugin>/provider_auth_aliases.py`）。

    插件文件里写一个映射即可：

        ALIASES = {"platform_auth_name": "canonical_auth_name", ...}

    为什么要有这个：公开树里只保留与厂商无关的规范凭据名（`app_credentials` /
    `anthropic_app_credentials` / `cached_app_credentials` / `api_key`），
    而某个平台的历史取值由插件在运行前注册回来 —— 存量配置因此不用改。

    返回已注册的别名列表（供日志/自检）。
    """
    registered: list[str] = []
    try:
        from provider_auth import register_auth_alias
    except Exception:  # noqa: BLE001  —— 主树被单独引用时不该因此炸
        return registered
    for root in plugin_paths():
        candidate = root / "provider_auth_aliases.py"
        if not candidate.is_file():
            continue
        for alias, canonical in (getattr(_load_module(candidate), "ALIASES", {}) or {}).items():
            register_auth_alias(alias, canonical)
            registered.append(alias)
    return registered


def search_report(provider: str) -> str:
    """给"找不到后端"用的可读说明。"""
    lines = [f"未知的 runtime.provider={provider!r}；本仓库内置的只有 local。",
             f"要跑这个后端，把插件放在下列任一位置："]
    for root in plugin_paths():
        lines.append(f"    {root / 'runtime_backends' / (str(provider).strip().lower().replace('-', '_') + '.py')}")
    lines.append(f"或用 {ENV_VAR} 指定插件目录（冒号分隔）。")
    return "\n".join(lines)
