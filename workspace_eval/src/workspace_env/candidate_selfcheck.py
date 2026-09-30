#!/usr/bin/env python3
"""selfcheck：让角色 A 在**提交之前**自己跑一遍机械校验。

为什么需要它：编排器本来就有一套确定性机械校验（``event_synthesis.py`` 的
``_candidate_validation_errors``），但它跑在**提交之后** —— 写手是拿不到这次
反馈的，只能等角色 B 打回来、再花一整轮去修。实测里相当一部分退回根本不是
「判断错了」，而是「没自查」：

* ``events_path`` 写成了 ``output/events.jsonl``（应为 ``events.jsonl``）
* ``files_used`` 里混进了目录、或带了 ``workspace/`` 前缀
* 某个 session 少了 / 多了 ``session.start``
* ``event_id`` 重复

这些全是机械可判的。把同一套判定搬到提交前，写手就能自己改掉，不必消耗
一整轮候选 —— 候选槽位是有限的（默认 5 个），每次误耗都是真实代价。

覆盖面：本脚本只做**结构性**检查（纯标准库，沙盒不保证有 jsonschema）。
完整 JSON Schema 校验与执行轨迹校验仍由编排器在提交后执行，这里不是替代，
而是把能前移的部分前移。

用法::

    python3 tools/selfcheck.py            # 检查，打印 PASS / FAIL
    python3 tools/selfcheck.py --quiet    # 只打印结论
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path


REQUIRED_TASK_FIELDS = (
    "candidate_id",
    "candidate_index",
    "attempt",
    "mode",
    "title",
    "summary",
    "files_used",
    "events_path",
    "visible_workspace_manifest_hash",
    "author_run_id",
)
REQUIRED_EVENT_FIELDS = (
    "event_id",
    "occurred_at",
    "workspace_id",
    "session_id",
    "actor",
    "action",
    "payload",
    "provenance",
)
#: 必须与 run-spec 逐字一致的身份字段。
IDENTITY_FIELDS = (
    "candidate_id",
    "candidate_index",
    "attempt",
    "mode",
    "previous_candidate_id",
    "visible_workspace_manifest_hash",
    "author_run_id",
)


def _load_spec(root: Path) -> dict:
    path = root / "input" / "candidate-spec.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path, problems: list[str]) -> list[dict]:
    events: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"无法读取 {path.name}：{exc}")
        return events
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"events.jsonl 第 {number} 行不是合法 JSON：{exc}")
            continue
        if not isinstance(value, dict):
            problems.append(f"events.jsonl 第 {number} 行不是 JSON 对象")
            continue
        events.append(value)
    return events


def check_rebuttals(root: Path, problems: list[str]) -> None:
    """rebuttals.json 只在 repair 轮存在；格式错了会干扰审核员，所以顺手校验。"""

    path = root / "output" / "rebuttals.json"
    if not path.is_file():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"rebuttals.json 不是合法 JSON：{exc}")
        return
    if not isinstance(value, list):
        problems.append("rebuttals.json 必须是一个数组（没有要反驳的就写 []）")
        return
    for index, item in enumerate(value, start=1):
        label = f"rebuttals.json 第 {index} 条"
        if not isinstance(item, dict):
            problems.append(f"{label} 必须是对象")
            continue
        if not isinstance(item.get("issue_index"), int):
            problems.append(f"{label} 缺少整数型 issue_index（reviewer-feedback.json 里 issues 的下标）")
        if item.get("stance") not in ("accept", "dispute"):
            problems.append(f"{label} 的 stance 必须是 accept 或 dispute")
        if not isinstance(item.get("reason"), str) or not str(item.get("reason")).strip():
            problems.append(f"{label} 的 reason 必须是非空字符串（要指名哪条 locator/excerpt 支撑）")


def check_task(root: Path, spec: dict, problems: list[str]) -> None:
    path = root / "output" / "candidate-task.json"
    if not path.is_file():
        problems.append("缺少 output/candidate-task.json")
        return
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"candidate-task.json 不是合法 JSON：{exc}")
        return
    if not isinstance(task, dict):
        problems.append("candidate-task.json 不是 JSON 对象")
        return

    for field in REQUIRED_TASK_FIELDS:
        if field not in task:
            problems.append(f"candidate-task.json 缺少必填字段 {field}")
    for field in ("title", "summary"):
        value = task.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"candidate-task.json 的 {field} 必须是非空字符串")

    # 实测最常见的机械错误：把路径写成 output/events.jsonl。
    if task.get("events_path") != "events.jsonl":
        problems.append(
            f"candidate-task.json 的 events_path 必须是 \"events.jsonl\"，"
            f"当前是 {task.get('events_path')!r}"
        )

    for field in IDENTITY_FIELDS:
        if field not in spec:
            continue
        if task.get(field) != spec.get(field):
            problems.append(
                f"candidate-task.json 的 {field} 必须逐字采用 run-spec 的值"
                f"（应为 {spec.get(field)!r}，当前是 {task.get(field)!r}）"
            )

    files_used = task.get("files_used")
    if not isinstance(files_used, list):
        problems.append("candidate-task.json 的 files_used 必须是数组")
        return
    if files_used != sorted(set(x for x in files_used if isinstance(x, str))):
        problems.append("files_used 必须排序且去重")
    workspace = root / "workspace"
    for relative in files_used:
        if not isinstance(relative, str):
            problems.append(f"files_used 的元素必须是字符串，遇到 {relative!r}")
            continue
        if relative.startswith("workspace/") or relative.startswith("/"):
            problems.append(
                f"files_used 必须是相对 Workspace 根的路径，"
                f"不能带 workspace/ 前缀或绝对路径：{relative}"
            )
            continue
        target = workspace / relative
        if not target.is_file() or target.is_symlink():
            problems.append(f"files_used 里的路径不是当前可见的普通文件：{relative}")


def check_events(root: Path, spec: dict, problems: list[str]) -> None:
    path = root / "output" / "events.jsonl"
    if not path.is_file():
        problems.append("缺少 output/events.jsonl")
        return
    events = _read_jsonl(path, problems)
    if not events:
        if not problems:
            problems.append("events.jsonl 里没有任何事件")
        return

    expected_workspace = spec.get("workspace_id")
    seen_ids: set[str] = set()
    for index, event in enumerate(events, start=1):
        label = f"第 {index} 条事件"
        for field in REQUIRED_EVENT_FIELDS:
            if field not in event:
                problems.append(f"{label} 缺少必填字段 {field}")
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen_ids:
                problems.append(f"{label} 的 event_id 与前面某条重复：{event_id}")
            seen_ids.add(event_id)
        if expected_workspace and event.get("workspace_id") != expected_workspace:
            problems.append(
                f"{label} 的 workspace_id 必须逐字采用 run-spec 的值"
                f"（应为 {expected_workspace!r}）"
            )

    sessions: dict[str, list[dict]] = {}
    for event in events:
        session_id = event.get("session_id")
        if isinstance(session_id, str):
            sessions.setdefault(session_id, []).append(event)
    for session_id in sorted(sessions):
        actions = [event.get("action") for event in sessions[session_id]]
        if actions.count("session.start") != 1:
            problems.append(f"session {session_id} 必须恰好包含一个 session.start")
        if actions.count("session.end") != 1:
            problems.append(f"session {session_id} 必须恰好包含一个 session.end")
        if actions and actions[0] != "session.start":
            problems.append(f"session {session_id} 的第一条事件必须是 session.start")
        if actions and actions[-1] != "session.end":
            problems.append(f"session {session_id} 的最后一条事件必须是 session.end")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="角色 A 工作目录（含 output/ 与 workspace/）")
    parser.add_argument("--quiet", action="store_true", help="只打印结论")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    spec = _load_spec(root)
    problems: list[str] = []
    check_task(root, spec, problems)
    check_events(root, spec, problems)
    check_rebuttals(root, problems)

    if problems:
        print(f"FAIL: 发现 {len(problems)} 个机械问题，修完再提交：")
        for item in problems:
            print(f"  - {item}")
        return 1
    if not args.quiet:
        print("PASS: 候选通过全部机械检查（提交后编排器仍会做完整的 schema 与轨迹校验）")
    else:
        print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
