#!/usr/bin/env python3
"""Merge validated per-workspace targeted-context batches without mixing workspaces.

The public event-log schema permits exactly one workspace per visible log.  This
utility therefore emits one merged public log per workspace plus a private
all-task composition manifest, rather than producing an invalid cross-workspace
JSONL file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.event_log import validate_visible_events


Json = Any
SOURCE_COMPOSITION_FORMAT = "workspace-bench-event-log-composition-v1"
MERGED_COMPOSITION_FORMAT = "workspace-bench-all-tasks-targeted-context-merge-v1"


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
    return rows


def _write_json(path: Path, value: Json, *, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600 if private else 0o644)
    os.replace(temporary, path)


def _write_jsonl(path: Path, rows: list[dict[str, Json]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(_canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _resolve_batch_artifact_path(batch_root: Path, raw_path: str) -> Path:
    """Resolve an artifact recorded under a different container mount point."""
    recorded = Path(raw_path)
    if recorded.is_file():
        return recorded
    parts = recorded.parts
    matching_indexes = [index for index, part in enumerate(parts) if part == batch_root.name]
    for index in reversed(matching_indexes):
        candidate = batch_root.joinpath(*parts[index + 1 :])
        if candidate.is_file():
            return candidate
    raise ValueError(f"recorded batch artifact is unavailable: {raw_path}")


def parse_batch_spec(raw: str) -> tuple[str, Path]:
    workspace, separator, raw_path = raw.partition("=")
    if not separator or not workspace or not raw_path:
        raise ValueError("--batch must use WORKSPACE=BATCH_ROOT")
    if any(char.isspace() for char in workspace):
        raise ValueError("workspace name in --batch must not contain whitespace")
    return workspace, Path(raw_path).resolve(strict=True)


def _source_batch(batch_root: Path) -> tuple[list[str], list[dict[str, Json]], dict[str, Json]]:
    final_root = batch_root / "final"
    composition_path = final_root / "composition.private.json"
    public_log = final_root / "events.public.jsonl"
    composition = _read_json(composition_path)
    if composition.get("format") != SOURCE_COMPOSITION_FORMAT:
        raise ValueError(f"unsupported source composition format: {composition_path}")
    task_ids = composition.get("included_task_ids")
    # Early standalone-overlay compositions predate ``included_task_ids``.
    # Their component audits remain authoritative and private, so recover the
    # IDs only from those audits rather than inferring them from public events.
    if not isinstance(task_ids, list) or not task_ids or not all(isinstance(task_id, str) and task_id.isdigit() for task_id in task_ids):
        recovered: list[str] = []
        components = composition.get("components")
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, dict):
                    recovered = []
                    break
                if component.get("kind") != "targeted_context_overlay":
                    continue
                if not isinstance(component.get("audit_path"), str):
                    recovered = []
                    break
                try:
                    audit_path = _resolve_batch_artifact_path(batch_root, str(component["audit_path"]))
                except ValueError:
                    recovered = []
                    break
                audit = _read_json(audit_path)
                audit_ids = audit.get("targeted_task_ids")
                if not isinstance(audit_ids, list) or len(audit_ids) != 1 or not isinstance(audit_ids[0], str) or not audit_ids[0].isdigit():
                    recovered = []
                    break
                recovered.append(audit_ids[0])
        if not recovered or len(set(recovered)) != len(recovered):
            raise ValueError(f"source composition has invalid included_task_ids: {composition_path}")
        task_ids = recovered
    rows = _read_jsonl(public_log)
    validate_visible_events(rows)
    expected_hash = composition.get("public_event_log_sha256")
    actual_hash = _sha256_bytes(public_log.read_bytes())
    if expected_hash != actual_hash:
        raise ValueError(f"source public-log hash disagrees with composition: {public_log}")
    if composition.get("event_count") != len(rows):
        raise ValueError(f"source event count disagrees with composition: {public_log}")
    return list(task_ids), rows, composition


def _referenced_base_hashes(composition: dict[str, Json]) -> set[str]:
    """Return explicit public-log hashes inherited by a refinement batch."""
    hashes: set[str] = set()
    components = composition.get("components")
    if not isinstance(components, list):
        return hashes
    for component in components:
        if not isinstance(component, dict):
            continue
        if component.get("kind") != "targeted_context_base":
            continue
        value = component.get("sha256")
        if isinstance(value, str) and value.startswith("sha256:"):
            hashes.add(value)
    return hashes


def merge_batches(
    *,
    batch_specs: list[tuple[str, Path]],
    output_root: Path,
    expected_task_count: int,
) -> dict[str, Json]:
    if output_root.exists():
        raise ValueError(f"output root already exists: {output_root}")
    if expected_task_count < 1:
        raise ValueError("expected_task_count must be positive")

    grouped: dict[str, list[Path]] = defaultdict(list)
    for workspace, batch_root in batch_specs:
        grouped[workspace].append(batch_root)
    if not grouped:
        raise ValueError("at least one --batch is required")

    all_task_ids: set[str] = set()
    task_source_hashes: dict[str, set[str]] = defaultdict(set)
    workspace_reports: list[dict[str, Json]] = []
    for workspace in sorted(grouped):
        rows_by_event_id: dict[str, dict[str, Json]] = {}
        workspace_ids: set[str] = set()
        components: list[dict[str, Json]] = []
        workspace_task_ids: set[str] = set()
        for batch_root in grouped[workspace]:
            task_ids, batch_rows, composition = _source_batch(batch_root)
            public_hash = str(composition["public_event_log_sha256"])
            inherited_hashes = _referenced_base_hashes(composition)
            overlap = all_task_ids.intersection(task_ids)
            invalid_overlap = sorted(
                task_id
                for task_id in overlap
                if not inherited_hashes.intersection(task_source_hashes[task_id])
            )
            if invalid_overlap:
                raise ValueError(
                    "task IDs occur in more than one independent source batch: "
                    f"{invalid_overlap}"
                )
            all_task_ids.update(task_ids)
            workspace_task_ids.update(task_ids)
            for task_id in task_ids:
                task_source_hashes[task_id].add(public_hash)
            added_unique_events = 0
            for row in batch_rows:
                event_id = str(row.get("event_id") or "")
                if not event_id:
                    raise ValueError(f"source batch has an event without event_id: {batch_root}")
                existing = rows_by_event_id.get(event_id)
                if existing is None:
                    rows_by_event_id[event_id] = row
                    added_unique_events += 1
                elif _canonical_json(existing) != _canonical_json(row):
                    raise ValueError(f"event ID has conflicting content across source batches: {event_id}")
            workspace_ids.update(str(row["workspace_id"]) for row in batch_rows)
            composition_path = batch_root / "final" / "composition.private.json"
            public_path = batch_root / "final" / "events.public.jsonl"
            components.append(
                {
                    "kind": "validated_targeted_context_batch",
                    "batch_root": str(batch_root),
                    "source_composition": str(composition_path),
                    "source_composition_sha256": _sha256_bytes(composition_path.read_bytes()),
                    "source_public_log": str(public_path),
                    "source_public_log_sha256": _sha256_bytes(public_path.read_bytes()),
                    "task_ids": task_ids,
                    "source_event_count": len(batch_rows),
                    "added_unique_event_count": added_unique_events,
                    "source_component_count": len(composition.get("components", [])),
                    "referenced_base_hashes": sorted(inherited_hashes),
                }
            )
        if len(workspace_ids) != 1:
            raise ValueError(f"workspace {workspace} does not resolve to exactly one opaque workspace ID")
        merged_rows = sorted(
            rows_by_event_id.values(),
            key=lambda row: (str(row["occurred_at"]), str(row["event_id"])),
        )
        validate_visible_events(merged_rows)
        final_root = output_root / workspace / "final"
        public_path = final_root / "events.public.jsonl"
        _write_jsonl(public_path, merged_rows)
        workspace_composition = {
            "format": SOURCE_COMPOSITION_FORMAT,
            "visibility": "private_only",
            "composition_kind": "merged_validated_targeted_context_batches",
            "components": components,
            "included_task_ids": sorted(workspace_task_ids, key=int),
            "excluded_task_ids": [],
            "event_count": len(merged_rows),
            "public_event_log": "final/events.public.jsonl",
            "public_event_log_sha256": _sha256_bytes(public_path.read_bytes()),
            "ordering": "ascending (occurred_at, event_id)",
            "constraints": [
                "Only final/events.public.jsonl may be staged for Codex.",
                "Task IDs, source compositions, and all targeted-context audits are private-only.",
                "Each source component is a validated synthetic targeted-context batch.",
            ],
        }
        _write_json(final_root / "composition.private.json", workspace_composition, private=True)
        workspace_reports.append(
            {
                "workspace": workspace,
                "workspace_id": next(iter(workspace_ids)),
                "task_count": len(workspace_task_ids),
                "task_ids": sorted(workspace_task_ids, key=int),
                "event_count": len(merged_rows),
                "public_event_log": str(public_path),
                "public_event_log_sha256": workspace_composition["public_event_log_sha256"],
            }
        )

    if len(all_task_ids) != expected_task_count:
        raise ValueError(f"merged task count {len(all_task_ids)} does not equal expected {expected_task_count}")
    output_manifest = {
        "format": MERGED_COMPOSITION_FORMAT,
        "visibility": "private_only",
        "expected_task_count": expected_task_count,
        "merged_task_count": len(all_task_ids),
        "workspaces": workspace_reports,
        "task_ids": sorted(all_task_ids, key=int),
        "constraints": [
            "There is no cross-workspace public event log because the public schema permits one workspace per log.",
            "Only each workspace final/events.public.jsonl may be staged for its matching workspace.",
            "The aggregate composition and all task linkage are private-only.",
        ],
    }
    _write_json(output_root / "all-tasks-composition.private.json", output_manifest, private=True)
    return output_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch",
        action="append",
        required=True,
        help="Repeat WORKSPACE=BATCH_ROOT for every validated source batch.",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-task-count", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = merge_batches(
        batch_specs=[parse_batch_spec(raw) for raw in args.batch],
        output_root=args.output_root.resolve(),
        expected_task_count=args.expected_task_count,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"merge targeted-context batches failed: {error}", file=sys.stderr)
        raise SystemExit(2)
