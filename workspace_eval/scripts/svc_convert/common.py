#!/usr/bin/env python3
"""svc_convert.common —— 路径常量、角色映射与共享装载工具。

把 ``evaluation/tasks_lite`` 的 100 个任务转换为“外部服务形态”（382 式：资料经
任务私有 WeCom/Mail Mock 提供）。本模块是所有 svc_convert 模块共享的常量与
I/O 单点，路径约定在此统一定义。

角色（file_system）= 用户：5 个长期员工，各自一份 committed universe 种子
（``svc_convert/universe/<slug>.json``）。任务 fixture 自包含；跨任务只复用
universe 里的身份/组织/联系人命名，不做跨任务时间线绑定。

运行/隔离机制去风险结论（2026-09-05，stage-0 核实）：
- 严格本地路径（run_strict_task_config -> _prepare_strict_run_view /
  _prepare_task_workspace_source）：宿主先把角色工作区快照拷成 per-case 副本，
  应用 metadata.input_remove_paths（删除非 manifest-target 的文件/目录），再把
  data_manifest 目标文件拷入；容器以只读挂载该副本为 agent workspace。
- 远程后端 路径：run_远程后端_experiment._publish_task_image 把
  input_remove_paths 做成镜像 whiteout，service 任务经 whiteouts 剔除。
- 结论 1（防泄漏前提）：tasks_lite 输入文件大量同 digest 存在于
  filesys/<role>_raw（如 research 363 四份 PDF 在 Download/、桌面/今天论文下载/、
  桌面/论文/memory/ 各一份）。全迁任务必须把“每一处同 digest/同 logical 文件”
  都列进 input_remove_paths（或由 validate 模拟快照后断言无残留），不能只删
  data/ 原始文件。
- 结论 2（混合形态写法）：转换任务统一写成 382 式。全迁 ``data_manifest=[]``；
  需要“保留本地杂乱文件”的 hybrid 任务，把保留文件写进 ``data_manifest`` 且带
  ``target_path``（= 希望出现在工作区的自然路径），运行时由 host 物化到 per-case
  工作区，覆盖同名的旧文件；这类任务不在 input_remove_paths 里删它们。
- 结论 3：agent-visible metadata 由各 runner 剥离 rubric 等字段；本流水线产出的
  metadata.json 即“完整”版本（含 rubrics/rubric_reference/service_expectations/
  input_remove_paths），runner 负责剥可见层。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

Json = Any

SVC_PKG = Path(__file__).resolve()
SCRIPTS_ROOT = SVC_PKG.parents[1]
EVAL_ROOT = SCRIPTS_ROOT.parent
REPO_ROOT = EVAL_ROOT.parent
SRC_ROOT = EVAL_ROOT / "src"
FILESYS_ROOT = EVAL_ROOT / "filesys"

TASKS_LITE_ROOT = EVAL_ROOT / "tasks_lite"
TASKS_SVC_ROOT = EVAL_ROOT / "tasks_svc"          # 验收后发布的目标任务集（untracked）
TASKS_NEW_ROOT = EVAL_ROOT / "tasks_new"          # 参考实现 382 所在
GEN_ROOT = EVAL_ROOT / ".generated" / "svc_convert"  # 所有中间产物（gitignored）

# 每角色在 tasks_lite 里的数量（2026-09 盘点）
ROLE_COUNTS = {
    "运营人员": 32,
    "行政/后勤人员": 30,
    "研究人员": 17,
    "开发人员": 11,
    "产品人员": 10,
}

# file_system(中文角色) -> 稳定元数据
# workspace: filesys 下角色工作区（noise）目录名（同 run_experiment.ROLE_WORKSPACE_NAMES）
# slug: universe 种子文件名
ROLES = {
    "研究人员": {
        "slug": "research",
        "workspace": "research_raw",
        "workspace_standard": "research_standard",
    },
    "运营人员": {
        "slug": "ops",
        "workspace": "yunying_raw",
        "workspace_standard": "yunying_standard",
    },
    "行政/后勤人员": {
        "slug": "admin",
        "workspace": "houqin_raw",
        "workspace_standard": "houqin_standard",
    },
    "开发人员": {
        "slug": "dev",
        "workspace": "kaifa_raw",
        "workspace_standard": "kaifa_standard",
    },
    "产品人员": {
        "slug": "product",
        "workspace": "chanpin_raw",
        "workspace_standard": "chanpin_standard",
    },
}
SLUG_TO_ROLE = {meta["slug"]: cn for cn, meta in ROLES.items()}

# 服务审计操作白名单（与服务端实际发出的事件一致）
WECOM_AUDIT_OPS = {
    "wecom.list_chats",
    "wecom.get_messages",
    "wecom.download_media",
    "wecom.get_document",
}
MAIL_AUDIT_OPS = {
    "mail.login",
    "mail.list",
    "mail.select",
    "mail.search",
    "mail.fetch",
    "mail.store",
    "mail.send",
}


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Json:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def role_of(metadata: Json) -> str:
    role = str((metadata or {}).get("file_system") or "")
    if role not in ROLES:
        raise ValueError(f"unsupported file_system role: {role!r}")
    return role


def role_slug(role_cn: str) -> str:
    try:
        return ROLES[role_cn]["slug"]
    except KeyError as exc:
        raise ValueError(f"unsupported role: {role_cn!r}") from exc


def load_lite_metadata(task_id: str) -> Json:
    path = TASKS_LITE_ROOT / str(task_id) / "metadata.json"
    if not path.is_file():
        raise FileNotFoundError(f"tasks_lite metadata not found: {path}")
    return read_json(path)


def iter_lite_task_ids(role_cn: str | None = None) -> Iterable[str]:
    """按 (可选的) 角色枚举 tasks_lite 任务 id，稳定排序。"""
    wanted = role_cn
    for task_dir in sorted(TASKS_LITE_ROOT.iterdir(), key=lambda p: p.name):
        if not task_dir.is_dir():
            continue
        meta_path = task_dir / "metadata.json"
        if not meta_path.is_file():
            continue
        meta = read_json(meta_path)
        if wanted is not None and str(meta.get("file_system") or "") != wanted:
            continue
        yield task_dir.name


def role_workspace_root(role_cn: str, *, standard: bool = False) -> Path:
    meta = ROLES[role_cn]
    name = meta["workspace_standard" if standard else "workspace"]
    root = FILESYS_ROOT / name
    return root


def task_run_root(role_cn: str, task_id: str) -> Path:
    """中间产物根：evaluation/.generated/svc_convert/<slug>/<task_id>/"""
    return GEN_ROOT / role_slug(role_cn) / str(task_id)
