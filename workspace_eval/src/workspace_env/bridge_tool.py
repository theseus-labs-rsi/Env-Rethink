#!/usr/bin/env python3
"""bridge-fix：让角色 A 自己在沙盒里把 bridge 约束跑通（自查 / 自修 / 复验）。

用法（在角色 A 的工作目录下，与 ``workspace/``、``input/``、``output/`` 同级）::

    python3 tools/bridge-fix.py --check              # 只检查，打印逐桥问题
    python3 tools/bridge-fix.py --fix                # 就地修复后复验（会改写 output/）
    python3 tools/bridge-fix.py --fix --dry-run      # 只打印将要做的事

修复只做**机械整形**，不编造事实：

1. 每条桥收敛到同一个 session（优先选已覆盖最多桥路径的那个 session）；
2. 缺失的 ``file.read`` 用**文件原文的逐字切片**补齐（文本类文件；xlsx/docx/pdf 等
   二进制无法逐字摘录时，报告 ``manual_read_required``，必须由你亲自读一次）；
3. 该 session 内按「先全部诱饵、再全部正确文件」重排，并按序重打时间戳（不倒退）；
4. 桥路径全部并入 ``files_used``（排序去重）。

约束定义与编排器的机械校验保持一致（``workspace_env/event_synthesis.py`` 的
``_interference_bridge_validation_errors``）；本文件刻意不依赖仓库代码，便于随角色
工作目录一起下发。``tests/test_envgen_targeted_bridges.py`` 有一致性回归。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import string
import sys
from pathlib import Path

TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".log", ".xml", ".html", ".ini", ".conf",
}
MIME_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".log": "text/plain",
    ".xml": "application/xml",
    ".html": "text/html",
}
EXCERPT_MAX_CHARS = 700
EXCERPT_MAX_LINES = 20
STEP_SECONDS = 30


def _opaque(prefix: str, seed: str, *, salt: str = "") -> str:
    rng = random.Random(f"{seed}:{salt}")
    body = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(20))
    return f"{prefix}_{body}"


def load_spec(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_candidate(root: Path) -> tuple[dict, list[dict]]:
    task = json.loads((root / "output/candidate-task.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (root / "output/events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return task, events


def save_candidate(root: Path, task: dict, events: list[dict]) -> None:
    (root / "output/candidate-task.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "output/events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _reads_by_session(events: list[dict]) -> dict[str, dict[str, list[int]]]:
    out: dict[str, dict[str, list[int]]] = {}
    for index, event in enumerate(events):
        if event.get("action") != "file.read":
            continue
        session_id = event.get("session_id")
        obj = event.get("object")
        path = obj.get("path_at_event") if isinstance(obj, dict) else None
        if isinstance(session_id, str) and isinstance(path, str):
            out.setdefault(session_id, {}).setdefault(path, []).append(index)
    return out


def check(task: dict, events: list[dict], bridges: list[dict]) -> list[str]:
    """与编排器一致的 bridge 机械校验（同 session、诱饵先于正确、files_used 覆盖、无泄漏）。"""

    errors: list[str] = []
    files_used = {p for p in task.get("files_used", []) if isinstance(p, str)}
    reads = _reads_by_session(events)
    public_text = json.dumps({"task": task, "events": events}, ensure_ascii=False)
    for bridge in bridges:
        required = list(bridge["distractor_files"]) + list(bridge["correct_files"])
        missing_used = sorted(p for p in required if p not in files_used)
        if missing_used:
            errors.append(
                f"interference bridge {bridge['bridge_id']}: files_used is missing "
                + ", ".join(missing_used)
            )
        covering = [sid for sid, paths in reads.items() if set(required) <= set(paths)]
        if not covering:
            errors.append(
                f"interference bridge {bridge['bridge_id']}: one session must file.read every "
                "distractor and correct path"
            )
        elif not any(
            max(i for p in bridge["distractor_files"] for i in reads[sid][p])
            < min(i for p in bridge["correct_files"] for i in reads[sid][p])
            for sid in covering
        ):
            errors.append(
                f"interference bridge {bridge['bridge_id']}: all distractor reads must precede "
                "all correct-file reads in the shared session"
            )
        if bridge["bridge_id"] in public_text:
            errors.append(
                f"interference bridge {bridge['bridge_id']}: private bridge_id leaked into public artifacts"
            )
    for label in ("interference_bridge", "distractor_files", "correct_files", "干扰文件", "正确文件", "目标文件"):
        if label in public_text:
            errors.append(f"private interference-bridge label leaked into public artifacts: {label}")
    return errors


def _verbatim_read(
    *,
    path: str,
    workspace_root: Path,
    session_id: str,
    workspace_id: str,
    occurred_at: str,
    actor: str,
    seed: str,
) -> tuple[dict | None, str | None]:
    target = workspace_root / path
    if not target.is_file() or target.is_symlink():
        return None, f"missing_in_workspace: {path}"
    suffix = target.suffix.casefold()
    if suffix not in TEXT_SUFFIXES:
        return None, f"manual_read_required: {path}（二进制/Office 文件，无法逐字摘录，请亲自读取）"
    lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not lines:
        return None, f"manual_read_required: {path}（空文件，无法构造摘录）"
    end = min(len(lines), EXCERPT_MAX_LINES)
    excerpt = "\n".join(lines[:end])
    while len(excerpt) > EXCERPT_MAX_CHARS and end > 1:
        end -= 1
        excerpt = "\n".join(lines[:end])
    event = {
        "action": "file.read",
        "actor": actor,
        "event_id": _opaque("evt", seed, salt=path),
        "object": {
            "mime_type": MIME_TYPES.get(suffix, "text/plain"),
            "object_id": _opaque("obj", seed, salt=path),
            "path_at_event": path,
        },
        "occurred_at": occurred_at,
        "payload": {
            "excerpt": excerpt,
            "locator": {"kind": "line", "start": 1, "end": end},
            "observation": f"读取并核对 `{target.name}` 的适用范围、记录口径与文件状态。",
            "purpose": "核对材料是否适用于本次工作范围",
        },
        "provenance": {
            "content_basis": "workspace_content",
            "generation_method": "agent_inference",
            "synthetic": True,
            "temporal_basis": "synthetic_timestamp",
            "transition_basis": "agent_inference",
        },
        "schema_version": 2,
        "session_id": session_id,
        "workspace_id": workspace_id,
    }
    return event, None


def repair(
    *,
    task: dict,
    events: list[dict],
    bridges: list[dict],
    workspace_root: Path,
    dry_run: bool = False,
) -> tuple[dict, list[dict], list[str]]:
    """机械整形：每条桥收敛到同一 session、补齐逐字读取、重排顺序、同步 files_used。"""

    notes: list[str] = []
    if not events:
        return task, events, ["no events to repair"]
    reads = _reads_by_session(events)
    by_session: dict[str, list[dict]] = {}
    for event in events:
        by_session.setdefault(str(event.get("session_id")), []).append(event)
    workspace_id = str(events[0].get("workspace_id") or "")
    actor = str(events[0].get("actor") or "workspace_owner")
    seed = str(task.get("candidate_id") or "candidate")

    def session_bounds(session_id: str) -> tuple[str, str]:
        stamps = [e.get("occurred_at") for e in by_session.get(session_id, []) if e.get("occurred_at")]
        stamps.sort()
        return (stamps[0] if stamps else "2025-01-01T09:00:00Z", stamps[-1] if stamps else "2025-01-01T09:00:00Z")

    for bridge in bridges:
        required = list(bridge["distractor_files"]) + list(bridge["correct_files"])
        covering = [sid for sid, paths in reads.items() if set(required) <= set(paths)]
        if covering:
            ordered = None
            for sid in covering:
                session_reads = reads[sid]
                if max(i for p in bridge["distractor_files"] for i in session_reads[p]) < min(
                    i for p in bridge["correct_files"] for i in session_reads[p]
                ):
                    ordered = sid
                    break
            if ordered is not None:
                notes.append(f"{bridge['bridge_id']}: 已满足（session {ordered}）")
                continue
            target = covering[0]
        else:
            # 选“已覆盖最多桥路径”的 session；都没有就新建一个 session。
            best, best_hits = None, 0
            for sid, paths in reads.items():
                hits = len(set(required) & set(paths))
                if hits > best_hits:
                    best, best_hits = sid, hits
            if best is None:
                target = _opaque("ses", seed, salt=bridge["bridge_id"])
                start_at = max((e.get("occurred_at") or "") for e in events) or "2025-01-01T09:00:00Z"
                by_session[target] = [
                    {
                        "action": "session.start",
                        "actor": actor,
                        "event_id": _opaque("evt", seed, salt=target + ":start"),
                        "occurred_at": start_at,
                        "payload": {
                            "application_context": ["文件管理器", "文本编辑器"],
                            "narrative": "开工前先核对材料适用范围，确认采用哪一套记录。",
                            "title": "核对材料适用范围与记录口径",
                        },
                        "provenance": {
                            "content_basis": "not_applicable",
                            "generation_method": "agent_inference",
                            "synthetic": True,
                            "temporal_basis": "synthetic_timestamp",
                            "transition_basis": "agent_inference",
                        },
                        "schema_version": 2,
                        "session_id": target,
                        "workspace_id": workspace_id,
                    },
                    {
                        "action": "session.end",
                        "actor": actor,
                        "event_id": _opaque("evt", seed, salt=target + ":end"),
                        "occurred_at": start_at,
                        "payload": {"status": "closed", "duration_seconds": 0},
                        "provenance": {
                            "content_basis": "not_applicable",
                            "generation_method": "agent_inference",
                            "synthetic": True,
                            "temporal_basis": "synthetic_timestamp",
                            "transition_basis": "agent_inference",
                        },
                        "schema_version": 2,
                        "session_id": target,
                        "workspace_id": workspace_id,
                    },
                ]
                notes.append(f"{bridge['bridge_id']}: 新建 session {target} 并补读取")
            else:
                target = best
                notes.append(f"{bridge['bridge_id']}: 收敛到已有 session {target}")

        session_events = by_session[target]
        existing_paths = {
            (e.get("object") or {}).get("path_at_event")
            for e in session_events
            if e.get("action") == "file.read"
        }
        start_at, _ = session_bounds(target)
        manual: list[str] = []
        added: list[dict] = []
        for path in required:
            if path in existing_paths:
                continue
            event, problem = _verbatim_read(
                path=path,
                workspace_root=workspace_root,
                session_id=target,
                workspace_id=workspace_id,
                occurred_at=start_at,
                actor=actor,
                seed=seed,
            )
            if event is None:
                manual.append(problem or path)
                continue
            added.append(event)
            if not dry_run:
                session_events.append(event)
            notes.append(f"{bridge['bridge_id']}: 补 file.read {path}")
        if manual:
            notes.extend(manual)

        # 该 session 内重排：session.start → 全部诱饵 → 全部正确 → 其余
        if not dry_run:
            start_events = [e for e in session_events if e.get("action") == "session.start"]
            end_events = [e for e in session_events if e.get("action") == "session.end"]
            reads_here = [e for e in session_events if e.get("action") == "file.read"]
            others = [
                e
                for e in session_events
                if e not in start_events and e not in end_events and e not in reads_here
            ]
            ordered_reads: list[dict] = []
            for path in bridge["distractor_files"]:
                ordered_reads.extend(
                    e for e in reads_here if (e.get("object") or {}).get("path_at_event") == path
                )
            for path in bridge["correct_files"]:
                ordered_reads.extend(
                    e for e in reads_here if (e.get("object") or {}).get("path_at_event") == path
                )
            seen = {id(e) for e in ordered_reads}
            ordered_reads.extend(e for e in reads_here if id(e) not in seen)
            session_events = start_events + ordered_reads + others + end_events
            by_session[target] = session_events
            # 重打时间戳（严格递增，不倒退）
            cursor = start_at
            for event in session_events:
                event["occurred_at"] = cursor
                cursor = _advance(cursor, STEP_SECONDS)
            notes.append(f"{bridge['bridge_id']}: session {target} 内重排为诱饵→正确")

    if not dry_run:
        ordered_events: list[dict] = []
        placed: set[str] = set()
        for event in events:
            sid = str(event.get("session_id"))
            if sid in placed:
                continue
            placed.add(sid)
            ordered_events.extend(by_session.get(sid, []))
        events = ordered_events
        files_used = {p for p in task.get("files_used", []) if isinstance(p, str)}
        for bridge in bridges:
            files_used.update(bridge["distractor_files"])
            files_used.update(bridge["correct_files"])
        task = dict(task)
        task["files_used"] = sorted(files_used)
    return task, events, notes


def _advance(stamp: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    try:
        base = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        base = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查，不修改")
    parser.add_argument("--fix", action="store_true", help="就地修复并复验")
    parser.add_argument("--dry-run", action="store_true", help="配合 --fix：只打印将要做的事")
    parser.add_argument("--root", default=".", help="角色 A 工作目录（含 output/ 与 workspace/）")
    parser.add_argument("--spec", default="input/bridge-spec.json", help="bridge 规格文件")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    spec = load_spec((root / args.spec).resolve())
    bridges = spec["bridges"]
    workspace_root = Path(spec["workspace_root"]).resolve()
    if root not in workspace_root.parents and root != workspace_root:
        # spec 里的 workspace_root 可能是沙盒内绝对路径；回退到相对工作目录
        workspace_root = root / "workspace"
    task, events = load_candidate(root)
    errors = check(task, events, bridges)
    if args.check or not args.fix:
        if errors:
            print("FAIL：bridge 机械校验未通过")
            for error in errors:
                print(f"  - {error}")
            print("\n可运行 `python3 tools/bridge-fix.py --fix` 机械修复，然后用 --check 复验。")
            return 1
        print("PASS：bridge 机械校验通过")
        return 0
    task, events, notes = repair(
        task=task, events=events, bridges=bridges, workspace_root=workspace_root, dry_run=args.dry_run
    )
    print("== 修复说明 ==")
    for note in notes:
        print(f"  - {note}")
    if args.dry_run:
        print("（dry-run：未写回文件）")
        return 0
    save_candidate(root, task, events)
    errors = check(task, events, bridges)
    if errors:
        print("\n修复后仍未通过：")
        for error in errors:
            print(f"  - {error}")
        print("\n未通过项多为 binary/Office 文件：请亲自 file.read 这些路径后重新运行 --fix。")
        return 1
    print("\nPASS：修复后 bridge 机械校验通过（已写回 output/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
