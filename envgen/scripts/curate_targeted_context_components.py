#!/usr/bin/env python3
"""Build one audited runtime log from a natural-history base and selected overlays.

This is a private composition step for screening-approved targeted-context
components.  It never infers task linkage from public events: every selected
component must carry the exact private targeted-context audit required by the
experiment protocol.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.event_log import validate_visible_events


Json = Any
COMPOSITION_FORMAT = "workspace-bench-event-log-composition-v1"
AUDIT_FORMAT = "workspace-bench-targeted-context-audit-v1"
AUDIT_KEYS = {
    "format",
    "visibility",
    "created_at",
    "construction_kind",
    "targeted_task_ids",
    "agent_visible_event_log",
    "agent_visible_event_log_sha256",
    "base_natural_log",
    "public_content_policy",
    "construction_scope",
    "reporting_boundary",
}


def _canonical_json(value: Json) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> dict[str, Json]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Json]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"expected non-empty JSON-object log: {path}")
    validate_visible_events(rows)
    return rows


def _write_json(path: Path, value: Json, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _write_jsonl(path: Path, rows: list[dict[str, Json]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(_canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def parse_component_spec(raw: str) -> tuple[str, Path, Path]:
    task_id, separator, paths = raw.partition("=")
    event_path, path_separator, audit_path = paths.partition(",")
    if (
        not separator
        or not path_separator
        or not task_id.isdigit()
        or not event_path
        or not audit_path
    ):
        raise ValueError("--component must use TASK_ID=EVENT_LOG,AUDIT")
    return task_id, Path(event_path).resolve(strict=True), Path(audit_path).resolve(strict=True)


def _validate_component(*, task_id: str, event_path: Path, audit_path: Path) -> tuple[list[dict[str, Json]], dict[str, Json]]:
    audit = _read_json(audit_path)
    if set(audit) != AUDIT_KEYS:
        raise ValueError(f"targeted audit does not use the exact schema fields: {audit_path}")
    if audit.get("format") != AUDIT_FORMAT or audit.get("visibility") != "private_only":
        raise ValueError(f"invalid targeted audit identity: {audit_path}")
    if audit.get("construction_kind") != "targeted_context_construction":
        raise ValueError(f"invalid targeted construction kind: {audit_path}")
    if audit.get("targeted_task_ids") != [task_id]:
        raise ValueError(f"targeted audit task linkage disagrees with component: {audit_path}")
    if audit_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError(f"targeted audit must be mode 0600: {audit_path}")
    actual_hash = _sha256_bytes(event_path.read_bytes())
    if audit.get("agent_visible_event_log_sha256") != actual_hash:
        raise ValueError(f"targeted audit hash disagrees with event log: {event_path}")
    rows = _read_jsonl(event_path)
    for row in rows:
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"targeted event lacks provenance: {event_path}")
        if provenance.get("synthetic") is not True or provenance.get("generation_method") != "agent_inference":
            raise ValueError(f"targeted event has invalid public provenance: {event_path}")
    return rows, audit


def curate_components(
    *,
    base_log: Path,
    components: list[tuple[str, Path, Path]],
    output_root: Path,
    expected_task_count: int,
) -> dict[str, Json]:
    if output_root.exists():
        raise ValueError(f"output root already exists: {output_root}")
    if expected_task_count < 1:
        raise ValueError("expected_task_count must be positive")
    if not components:
        raise ValueError("at least one selected component is required")

    base_rows = _read_jsonl(base_log)
    source_base_workspace_ids = {str(row["workspace_id"]) for row in base_rows}
    if len(source_base_workspace_ids) != 1:
        raise ValueError("natural-history base does not resolve to one workspace ID")
    validated_components: list[tuple[str, Path, Path, list[dict[str, Json]]]] = []
    overlay_workspace_ids: set[str] = set()
    for task_id, event_path, audit_path in components:
        rows, _audit = _validate_component(task_id=task_id, event_path=event_path, audit_path=audit_path)
        validated_components.append((task_id, event_path, audit_path, rows))
        overlay_workspace_ids.update(str(row["workspace_id"]) for row in rows)
    if len(overlay_workspace_ids) != 1:
        raise ValueError("selected overlays do not resolve to one workspace ID")
    current_workspace_id = next(iter(overlay_workspace_ids))
    source_base_workspace_id = next(iter(source_base_workspace_ids))
    if source_base_workspace_id != current_workspace_id:
        rebound_base_rows = []
        for row in base_rows:
            rebound = copy.deepcopy(row)
            rebound["workspace_id"] = current_workspace_id
            rebound_base_rows.append(rebound)
        base_rows = rebound_base_rows
        validate_visible_events(base_rows)

    rows_by_event_id: dict[str, dict[str, Json]] = {}
    for row in base_rows:
        rows_by_event_id[str(row["event_id"])] = row
    workspace_ids = {str(row["workspace_id"]) for row in base_rows}
    task_ids: set[str] = set()
    private_components: list[dict[str, Json]] = [
        {
            "kind": "natural_history",
            "path": str(base_log),
            "sha256": _sha256_bytes(base_log.read_bytes()),
            "event_count": len(base_rows),
        }
    ]

    for task_id, event_path, audit_path, rows in validated_components:
        task_ids.add(task_id)
        added = 0
        for row in rows:
            workspace_ids.add(str(row["workspace_id"]))
            event_id = str(row["event_id"])
            existing = rows_by_event_id.get(event_id)
            if existing is None:
                rows_by_event_id[event_id] = row
                added += 1
            elif _canonical_json(existing) != _canonical_json(row):
                raise ValueError(f"event ID has conflicting content: {event_id}")
        private_components.append(
            {
                "kind": "targeted_context_overlay",
                "task_ids": [task_id],
                "path": str(event_path),
                "sha256": _sha256_bytes(event_path.read_bytes()),
                "audit_path": str(audit_path),
                "audit_sha256": _sha256_bytes(audit_path.read_bytes()),
                "source_event_count": len(rows),
                "added_unique_event_count": added,
            }
        )

    if len(task_ids) != expected_task_count:
        raise ValueError(
            f"selected task count {len(task_ids)} does not equal expected {expected_task_count}"
        )
    if len(workspace_ids) != 1:
        raise ValueError("base and selected overlays do not resolve to one workspace ID")

    merged_rows = sorted(rows_by_event_id.values(), key=lambda row: (str(row["occurred_at"]), str(row["event_id"])))
    validate_visible_events(merged_rows)
    public_path = output_root / "final" / "events.public.jsonl"
    _write_jsonl(public_path, merged_rows)
    composition = {
        "format": COMPOSITION_FORMAT,
        "visibility": "private_only",
        "composition_kind": "natural_history_plus_screening_approved_targeted_context_overlays",
        "components": private_components,
        "included_task_ids": sorted(task_ids, key=int),
        "excluded_task_ids": [],
        "event_count": len(merged_rows),
        "public_event_log": "final/events.public.jsonl",
        "public_event_log_sha256": _sha256_bytes(public_path.read_bytes()),
        "ordering": "ascending (occurred_at, event_id)",
        "workspace_identity_rebind": {
            "source_workspace_ids": [source_base_workspace_id],
            "current_workspace_id": current_workspace_id,
            "transformation": (
                "For natural-history events only, replace workspace_id with the selected overlays' "
                "current opaque workspace ID; preserve all other public fields."
            ),
        },
        "constraints": [
            "Only final/events.public.jsonl may be staged for Codex.",
            "Task IDs, selection evidence, component audits, and source paths are private-only.",
            "Every selected overlay remains a separately audited synthetic targeted-context intervention.",
        ],
    }
    _write_json(output_root / "final" / "composition.private.json", composition, mode=0o600)
    return composition


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-log", type=Path, required=True)
    parser.add_argument(
        "--component",
        action="append",
        required=True,
        help="Repeat TASK_ID=EVENT_LOG,AUDIT for every approved overlay component.",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-task-count", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = curate_components(
        base_log=args.base_log.resolve(strict=True),
        components=[parse_component_spec(raw) for raw in args.component],
        output_root=args.output_root.resolve(),
        expected_task_count=args.expected_task_count,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
