#!/usr/bin/env python3
"""校验变体的事件日志脚手架是否符合 Workspace-Bench canonical Event Log v2。

用法：
    python3 tools/validate_events.py variants/<task>/<vid>/events.private.jsonl

做四件事：
  1. 逐行对 canonical schema（本仓 `schema/context-event-log-canonical-schema.json`）做 jsonschema 校验；
  2. 检查 canonical_sequence 严格递增、唯一；
  3. 检查 session 完整性：每个 session 恰好一个 session.start（首）与一个 session.end（尾）；
  4. 检查因果链：每条非 session.start 事件至少一条指向「同 session 更早事件」的 causal_link。

任一项失败即非零退出，并把失败明细打到 stdout（供 gate_report.md 直接引用）。
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# canonical schema 是**本仓 vendor 的副本**（schema/README.md 有来源与更新方式）。
# 以前读的是 env-evolve 仓里的硬编码绝对路径 —— 跨仓读一个文件会把"跑一次管线"
# 变成"依赖另一个仓的目录布局"，而且仓一挪就断。
DEFAULT_SCHEMA = Path(
    os.environ.get("WB_EVENT_SCHEMA", str(REPO / "schema" / "context-event-log-canonical-schema.json"))
)


def load_rows(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"FAIL line {lineno}: 不是合法 JSON：{exc}")
    if not rows:
        raise SystemExit("FAIL: 事件日志为空")
    return rows


def check_schema(rows: list[dict], schema_path: Path) -> list[str]:
    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = []
    for row in rows:
        eid = row.get("event", {}).get("event_id", "<no-event_id>")
        try:
            jsonschema.validate(row, schema)
        except jsonschema.ValidationError as exc:
            loc = "/".join(str(p) for p in exc.absolute_path)
            errors.append(f"{eid}: schema 失败 @ {loc}: {exc.message[:200]}")
    return errors


def check_sequence(rows: list[dict]) -> list[str]:
    errors = []
    seqs = [row.get("canonical_sequence") for row in rows]
    if any(not isinstance(s, int) for s in seqs):
        return ["canonical_sequence 必须全是整数"]
    if len(set(seqs)) != len(seqs):
        errors.append("canonical_sequence 有重复")
    if seqs != sorted(seqs):
        errors.append("canonical_sequence 未按时间递增排列（文件顺序应与 sequence 一致）")
    return errors


def check_sessions(rows: list[dict]) -> list[str]:
    errors = []
    by_session: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        ev = row["event"]
        by_session[ev["session_id"]].append(ev)

    for sid, events in sorted(by_session.items()):
        starts = [e for e in events if e["action"] == "session.start"]
        ends = [e for e in events if e["action"] == "session.end"]
        if len(starts) != 1 or len(ends) != 1:
            errors.append(f"{sid}: 需要恰好 1 个 session.start 与 1 个 session.end（实得 {len(starts)}/{len(ends)}）")
            continue
        if events[0]["action"] != "session.start":
            errors.append(f"{sid}: 首条事件不是 session.start（是 {events[0]['action']}）")
        if events[-1]["action"] != "session.end":
            errors.append(f"{sid}: 末条事件不是 session.end（是 {events[-1]['action']}）")
    return errors


def check_causal_links(rows: list[dict]) -> list[str]:
    errors = []
    order = {row["event"]["event_id"]: i for i, row in enumerate(rows)}
    session_of = {row["event"]["event_id"]: row["event"]["session_id"] for row in rows}

    for i, row in enumerate(rows):
        ev = row["event"]
        links = row.get("causal_links") or []
        if ev["action"] == "session.start":
            continue
        if not links:
            errors.append(f"{ev['event_id']}: 非 session.start 事件缺少 causal_links")
            continue
        same_session_earlier = False
        for link in links:
            target = link.get("event_id")
            if target not in order:
                errors.append(f"{ev['event_id']}: causal_link 指向不存在的事件 {target}")
                continue
            if order[target] >= i:
                errors.append(f"{ev['event_id']}: causal_link 指向不更早的事件 {target}")
                continue
            if session_of[target] == ev["session_id"]:
                same_session_earlier = True
        if not same_session_earlier:
            errors.append(f"{ev['event_id']}: 缺少同 session 的更早因果链接（v7 起的硬规则）")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    log_path = Path(sys.argv[1])
    schema_path = DEFAULT_SCHEMA
    if not schema_path.exists():
        raise SystemExit(f"找不到 schema：{schema_path}（可用 WB_EVENT_SCHEMA 覆盖）")

    rows = load_rows(log_path)
    checks = {
        "schema": check_schema(rows, schema_path),
        "sequence": check_sequence(rows),
        "sessions": check_sessions(rows),
        "causal_links": check_causal_links(rows),
    }

    sessions = {row["event"]["session_id"] for row in rows}
    actions: dict[str, int] = defaultdict(int)
    for row in rows:
        actions[row["event"]["action"]] += 1

    print(f"事件日志：{log_path}")
    print(f"  事件 {len(rows)} 条 / session {len(sessions)} 个")
    print("  actions：" + ", ".join(f"{k}×{v}" for k, v in sorted(actions.items())))

    failed = False
    for name, errors in checks.items():
        if errors:
            failed = True
            print(f"  [FAIL] {name} —— {len(errors)} 项")
            for err in errors:
                print(f"         - {err}")
        else:
            print(f"  [PASS] {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
