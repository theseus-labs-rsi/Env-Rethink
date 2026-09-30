#!/usr/bin/env python3
"""Run one real rubric-guided context synthesis loop for each selected task.

This is intentionally an orchestration utility, not an event generator.  It
only copies the selected task's complete rubric list into the *private*
``rubric_contexts`` input of the existing Codex A/B/C synthesis loop.  Codex A
still has to inspect the workspace and author the public event stream; Codex B
and C retain their normal validation and canonicalisation roles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.event_log import validate_visible_events
from workspace_env.integration import workspace_snapshot_hash


Json = Any
TARGETED_AUDIT_FORMAT = "workspace-bench-targeted-context-audit-v1"
DEFAULT_TASK_IDS = ("15", "44", "45", "53", "55", "95", "171", "175", "386", "388")
DEFAULT_PARALLEL_WORKERS = 5
DEFAULT_MAX_B_FAILURES_PER_CANDIDATE = 5
DEFAULT_MAX_CANDIDATE_SLOTS = 3
DEFAULT_RUN_LABEL = "context-event-log-all-rubric-20260724-r2"
WORKSPACE_VIEW_MODES = ("isolated_masked_copy", "shared_readonly_unmasked")


def _canonical_json(value: Json) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


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


def _read_json(path: Path) -> dict[str, Json]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Json]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"expected JSON objects in {path}")
    return rows


def build_base_natural_log_audit(base_public_log: Path | None) -> dict[str, Json]:
    """Describe the natural-history component without inventing one.

    Some workspaces have no independently generated natural event history.  A
    standalone targeted-context run must record that absence explicitly rather
    than borrowing a log from another workspace merely to satisfy an audit
    field.  ``base_natural_log`` remains present in both cases, as required by
    the targeted-context audit schema.
    """
    if base_public_log is None:
        return {
            "available": False,
            "composition": (
                "No natural-history component is available for this workspace. "
                "The batch is composed from validated targeted overlays only."
            ),
        }
    return {
        "available": True,
        "path": str(base_public_log),
        "sha256": _sha256_bytes(base_public_log.read_bytes()),
        "composition": "This overlay is separately validated and is merged only by the batch composition audit.",
    }


def parse_task_ids(raw: str) -> list[str]:
    task_ids = [part.strip() for part in raw.split(",") if part.strip()]
    if not task_ids:
        raise ValueError("task IDs must not be empty")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task IDs must not repeat")
    if any(not task_id.isdigit() for task_id in task_ids):
        raise ValueError("task IDs must be decimal integers")
    return task_ids


def _normalized_input_filename(filename: str) -> str:
    """Normalize the benchmark's collision-avoidance suffix for matching.

    Workspace preparation can rename an imported task input from ``report.xlsx``
    to ``report_.xlsx`` when an identically named file already exists.  This is
    a filesystem-level import convention, not a rubric-derived heuristic.  It
    is used only as a fallback after an exact filename match and only when the
    normalized candidate is unambiguous.
    """
    path = Path(filename)
    stem = path.stem
    while stem.endswith("_"):
        stem = stem[:-1]
    return f"{stem}{path.suffix.lower()}"


@lru_cache(maxsize=None)
def _workspace_filename_indexes(workspace_root: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build one immutable-in-practice filename index per synthesis workspace."""
    by_filename: dict[str, list[str]] = defaultdict(list)
    by_normalized_filename: dict[str, list[str]] = defaultdict(list)
    for path in workspace_root.rglob("*"):
        if path.is_file():
            relative_path = path.relative_to(workspace_root).as_posix()
            by_filename[path.name].append(relative_path)
            by_normalized_filename[_normalized_input_filename(path.name)].append(relative_path)
    return by_filename, by_normalized_filename


def load_task_context(
    tasks_root: Path,
    workspace_root: Path,
    task_id: str,
) -> tuple[list[str], str, list[str], list[str]]:
    # ``tasks_lite_updated`` is the sole canonical task/rubric source. Its
    # accepted curation state lives in metadata.json; consulting an adjacent
    # legacy metadata_verified.json would silently reintroduce superseded
    # rubrics into a new targeted-context intervention.
    metadata_path = tasks_root / task_id / "metadata.json"
    metadata = _read_json(metadata_path)
    rubrics = metadata.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        raise ValueError(f"task {task_id} has no rubrics in {metadata_path}")
    if not all(isinstance(rubric, str) and rubric.strip() for rubric in rubrics):
        raise ValueError(f"task {task_id} has blank or non-string rubrics")
    # Keep the source list byte-for-byte meaningful to the intervention. The
    # reviewed task bundle may intentionally retain duplicate rubric wording as
    # separate evaluation rows; silently deduplicating it would change the
    # private Codex A input and its audit digest.
    manifest = metadata.get("data_manifest")
    if not isinstance(manifest, list):
        raise ValueError(f"task {task_id} has no data_manifest")
    by_filename, by_normalized_filename = _workspace_filename_indexes(workspace_root)
    preferred_files: set[str] = set()
    unresolved_filenames: list[str] = []
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError(f"task {task_id} has an invalid data_manifest item")
        filename = item["filename"]
        matches = by_filename.get(filename, [])
        if not matches:
            normalized_matches = by_normalized_filename.get(_normalized_input_filename(filename), [])
            matches = normalized_matches if len(normalized_matches) == 1 else []
        if matches:
            preferred_files.update(matches)
        else:
            unresolved_filenames.append(filename)
    if not preferred_files:
        raise ValueError(f"task {task_id} has no visible source files in the workspace")
    return (
        list(rubrics),
        _sha256_bytes(_canonical_json(rubrics).encode("utf-8")),
        sorted(preferred_files),
        sorted(unresolved_filenames),
    )


def build_run_config(
    *,
    task_id: str,
    rubrics: list[str],
    preferred_files: list[str],
    workspace_root: Path,
    output_root: Path,
    model: str,
    auth_mode: str,
    expected_codex_version: str,
    timeout_seconds: float,
    random_seed: int,
    max_b_failures_per_candidate: int,
    run_label: str,
    max_candidate_slots: int = DEFAULT_MAX_CANDIDATE_SLOTS,
    workspace_view_mode: str = "isolated_masked_copy",
    shared_workspace_snapshot_hash: str | None = None,
) -> dict[str, Json]:
    return {
        "schema_version": 1,
        "run_id": f"{run_label}-task{task_id}",
        "repetition_id": 1,
        "workspace_root": str(workspace_root),
        "output_root": str(output_root),
        "model": model,
        "auth_mode": auth_mode,
        "base_url": None,
        "expected_codex_version": expected_codex_version,
        "timeout_seconds": timeout_seconds,
        "workspace_view_mode": workspace_view_mode,
        "shared_workspace_snapshot_hash": shared_workspace_snapshot_hash,
        "mask_rate": 0.5,
        "target_file_coverage": 0.1,
        "max_b_failures_per_candidate": max_b_failures_per_candidate,
        "max_candidate_slots": max_candidate_slots,
        "random_seed": random_seed,
        "construction_mode": "rubric_context",
        "selection_policy": "targeted_diagnostic",
        "preferred_files": preferred_files,
        "rubric_contexts": rubrics,
        "interference_bridges": [],
        "targeted_task_ids": [task_id],
        "base_natural_event_log": None,
        "stop_after_accepted_candidates": 1,
    }


def write_targeted_audit(
    *,
    task_id: str,
    task_root: Path,
    rubric_count: int,
    rubric_sha256: str,
    preferred_files: list[str],
    unresolved_filenames: list[str],
    base_public_log: Path | None,
) -> Path:
    public_log = task_root / "final" / "events.public.jsonl"
    rows = _read_jsonl(public_log)
    validated = validate_visible_events(rows)
    audit = {
        "format": TARGETED_AUDIT_FORMAT,
        "visibility": "private_only",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "construction_kind": "targeted_context_construction",
        "targeted_task_ids": [task_id],
        "agent_visible_event_log": "final/events.public.jsonl",
        "agent_visible_event_log_sha256": _sha256_bytes(public_log.read_bytes()),
        "base_natural_log": build_base_natural_log_audit(base_public_log),
        "public_content_policy": [
            "Public events are synthetic agent-inferred historical context and do not expose task IDs, rubric IDs, judge results, pass/fail instructions, reference answers, or rubric-to-event mappings.",
            "The complete rubric list is private Codex A input only; public events must remain compatible with the workspace and label inferred history honestly.",
            "Temporary historical documents must be delete-closed so the final workspace state remains unchanged.",
        ],
        "construction_scope": [
            {
                "task_id": task_id,
                "historical_context_type": "all-rubric-guided prior collaboration context",
                "rubric_count": rubric_count,
                "rubric_contexts_sha256": rubric_sha256,
                "preferred_files": preferred_files,
                "unresolved_manifest_filenames": unresolved_filenames,
                "public_event_ids": [event.event_id for event in validated],
            }
        ],
        "reporting_boundary": "This is a full-rubric synthetic targeted-context intervention. It must be reported separately from natural-history, no-history, and partial-targeted-context conditions; neither the audit nor the rubric inputs may reach Codex task execution or the judge.",
    }
    path = task_root / "final" / "targeted-context.private.json"
    _write_json(path, audit, private=True)
    return path


def compose_final_log(
    *,
    batch_root: Path,
    base_public_log: Path,
    task_ids: list[str],
    overlay_intervention: str = "all_rubric",
) -> Path:
    if overlay_intervention not in {"all_rubric", "interference_bridge"}:
        raise ValueError(f"unsupported overlay intervention: {overlay_intervention}")
    base_rows = _read_jsonl(base_public_log)
    if not base_rows:
        raise ValueError("base public log is empty")
    components: list[dict[str, Json]] = [
        {
            "kind": "natural_history",
            "path": str(base_public_log),
            "sha256": _sha256_bytes(base_public_log.read_bytes()),
            "event_count": len(base_rows),
        }
    ]
    overlay_rows: list[dict[str, Json]] = []
    workspace_ids: set[str] = set()
    for task_id in task_ids:
        overlay_path = batch_root / "overlays" / f"task-{task_id}" / "final" / "events.public.jsonl"
        rows = _read_jsonl(overlay_path)
        validated = validate_visible_events(rows)
        workspace_ids.update(event.workspace_id for event in validated)
        audit_path = overlay_path.with_name("targeted-context.private.json")
        components.append(
            {
                "kind": "targeted_context_overlay",
                "path": str(overlay_path),
                "sha256": _sha256_bytes(overlay_path.read_bytes()),
                "event_count": len(rows),
                "audit_path": str(audit_path),
            }
        )
        overlay_rows.extend(rows)
    if len(workspace_ids) != 1:
        raise ValueError("all overlay logs must have one shared workspace ID")
    target_workspace_id = next(iter(workspace_ids))
    source_workspace_ids = {str(row.get("workspace_id")) for row in base_rows}
    rebound_base = [{**row, "workspace_id": target_workspace_id} for row in base_rows]
    merged = sorted(rebound_base + overlay_rows, key=lambda row: (row["occurred_at"], row["event_id"]))
    validate_visible_events(merged)
    final_root = batch_root / "final"
    final_log = final_root / "events.public.jsonl"
    _write_jsonl(final_log, merged)
    composition = {
        "format": "workspace-bench-event-log-composition-v1",
        "visibility": "private_only",
        "composition_kind": (
            "natural_history_plus_all_rubric_targeted_context_overlays_with_workspace_identity_rebind"
            if overlay_intervention == "all_rubric"
            else "natural_history_plus_interference_bridge_targeted_context_overlays_with_workspace_identity_rebind"
        ),
        "components": components,
        "event_count": len(merged),
        "public_event_log": "final/events.public.jsonl",
        "public_event_log_sha256": _sha256_bytes(final_log.read_bytes()),
        "ordering": "ascending (occurred_at, event_id)",
        "workspace_identity_rebind": {
            "source_workspace_ids": sorted(source_workspace_ids),
            "current_workspace_id": target_workspace_id,
            "transformation": "For natural-history events only, replace workspace_id with the shared current workspace opaque ID; preserve all other public fields.",
        },
        "constraints": [
            "Only final/events.public.jsonl may be staged for Codex.",
            (
                "The composition audit, individual targeted audits, and all rubric inputs are private-only."
                if overlay_intervention == "all_rubric"
                else "The composition audit, individual targeted audits, task IDs, and private file-role mappings are private-only."
            ),
            (
                "Every overlay is synthetic targeted context and must be reported as a separate intervention."
                if overlay_intervention == "all_rubric"
                else "Every overlay is a synthetic interference-bridge intervention and must be reported separately from rubric-context, natural-history, and no-history conditions."
            ),
        ],
    }
    _write_json(final_root / "composition.private.json", composition, private=True)
    return final_log


def completed_overlay_task_ids(*, batch_root: Path, task_ids: list[str]) -> tuple[list[str], list[str]]:
    """Return finalized and excluded task IDs without inspecting private rubric text."""
    included: list[str] = []
    excluded: list[str] = []
    for task_id in task_ids:
        result_path = batch_root / "overlays" / f"task-{task_id}" / "result.json"
        if not result_path.is_file():
            excluded.append(task_id)
            continue
        result = _read_json(result_path)
        if result.get("status") == "TIMELINE_COMPLETE":
            included.append(task_id)
        else:
            excluded.append(task_id)
    return included, excluded


def compose_overlay_only_final_log(
    *,
    batch_root: Path,
    task_ids: list[str],
) -> tuple[Path, list[str], list[str]]:
    """Compose only fully validated overlays for a separately reported condition.

    A partial synthesis batch must not be silently completed with failed or
    unvalidated candidates.  This finalizer deliberately contains no natural
    history component, so its output can be evaluated as an independent
    all-rubric targeted-context intervention.
    """
    included_task_ids, excluded_task_ids = completed_overlay_task_ids(
        batch_root=batch_root,
        task_ids=task_ids,
    )
    if not included_task_ids:
        raise ValueError("no TIMELINE_COMPLETE overlays are available to compose")

    components: list[dict[str, Json]] = []
    overlay_rows: list[dict[str, Json]] = []
    workspace_ids: set[str] = set()
    for task_id in included_task_ids:
        overlay_path = batch_root / "overlays" / f"task-{task_id}" / "final" / "events.public.jsonl"
        audit_path = overlay_path.with_name("targeted-context.private.json")
        if not audit_path.is_file():
            raise ValueError(f"completed task {task_id} has no targeted private audit")
        rows = _read_jsonl(overlay_path)
        validated = validate_visible_events(rows)
        workspace_ids.update(event.workspace_id for event in validated)
        components.append(
            {
                "kind": "targeted_context_overlay",
                "path": str(overlay_path),
                "sha256": _sha256_bytes(overlay_path.read_bytes()),
                "event_count": len(rows),
                "audit_path": str(audit_path),
            }
        )
        overlay_rows.extend(rows)
    if len(workspace_ids) != 1:
        raise ValueError("all completed overlay logs must have one shared workspace ID")

    merged = sorted(overlay_rows, key=lambda row: (row["occurred_at"], row["event_id"]))
    validate_visible_events(merged)
    final_root = batch_root / "final"
    final_log = final_root / "events.public.jsonl"
    _write_jsonl(final_log, merged)
    composition = {
        "format": "workspace-bench-event-log-composition-v1",
        "visibility": "private_only",
        "composition_kind": "all_rubric_targeted_context_overlays_only",
        "components": components,
        "event_count": len(merged),
        "public_event_log": "final/events.public.jsonl",
        "public_event_log_sha256": _sha256_bytes(final_log.read_bytes()),
        "ordering": "ascending (occurred_at, event_id)",
        "excluded_task_ids": excluded_task_ids,
        "constraints": [
            "No natural-history or earlier targeted-context component is included.",
            "Only overlays with TIMELINE_COMPLETE status and a private targeted audit are included.",
            "The composition audit, individual targeted audits, and all rubric inputs are private-only.",
            "Every included overlay is synthetic targeted context and must be reported as a separate intervention.",
        ],
    }
    _write_json(final_root / "composition.private.json", composition, private=True)
    return final_log, included_task_ids, excluded_task_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-ids", default=",".join(DEFAULT_TASK_IDS))
    parser.add_argument("--tasks-root", type=Path, default=EVALUATION_ROOT / "tasks_lite")
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument(
        "--base-public-log",
        type=Path,
        help="Optional natural-history log. Required unless --standalone-overlay-only is set.",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--auth-mode", choices=("api", "chatgpt"), default="chatgpt")
    parser.add_argument("--expected-codex-version", default="0.144.5")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--seed", type=int, default=2026072401)
    parser.add_argument(
        "--run-label",
        default=DEFAULT_RUN_LABEL,
        help="Stable, audit-visible label used to derive distinct per-task synthesis run IDs.",
    )
    parser.add_argument("--parallel-workers", type=int, default=DEFAULT_PARALLEL_WORKERS)
    parser.add_argument(
        "--max-b-failures-per-candidate",
        type=int,
        default=DEFAULT_MAX_B_FAILURES_PER_CANDIDATE,
        help="Maximum Codex B REVISE verdicts for one candidate before it is abandoned.",
    )
    parser.add_argument(
        "--max-candidate-slots",
        type=int,
        default=DEFAULT_MAX_CANDIDATE_SLOTS,
        help="Maximum candidate slots per task; an increase is audited during --resume.",
    )
    parser.add_argument(
        "--workspace-view-mode",
        choices=WORKSPACE_VIEW_MODES,
        default="isolated_masked_copy",
        help=(
            "Workspace view for Codex A/B/C. shared_readonly_unmasked is restricted to "
            "rubric-guided synthesis and requires --workspace-root to be a read-only mount."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--standalone-overlay-only",
        action="store_true",
        help=(
            "Compose only validated targeted overlays. Use this when the workspace has no "
            "independently generated natural-history log; the private audit records that absence."
        ),
    )
    parser.add_argument(
        "--finalize-existing-overlays-only",
        action="store_true",
        help=(
            "Compose already completed overlays into a standalone public log, "
            "excluding incomplete tasks and all prior natural/targeted logs."
        ),
    )
    return parser.parse_args()


def resolve_shared_workspace_snapshot(
    *,
    workspace_root: Path,
    output_root: Path,
    resume: bool,
    workspace_view_mode: str,
) -> str | None:
    """Hash one read-only source once, then reuse that private batch audit.

    The synthesis subprocesses must not each read a multi-gigabyte workspace to
    obtain the same hash.  A shared view is only allowed when the orchestrator
    separately verifies that the supplied workspace root is mounted read-only.
    """
    if workspace_view_mode != "shared_readonly_unmasked":
        return None
    audit_path = output_root / "workspace-snapshot.private.json"
    if resume and audit_path.is_file():
        cached = _read_json(audit_path)
        if cached.get("workspace_view_mode") != workspace_view_mode:
            raise ValueError("stored workspace view mode does not match --workspace-view-mode")
        if cached.get("workspace_root") != str(workspace_root):
            raise ValueError("stored workspace root does not match --workspace-root")
        snapshot_hash = cached.get("source_snapshot_hash")
        if not isinstance(snapshot_hash, str) or not snapshot_hash.startswith("sha256:"):
            raise ValueError("stored shared workspace snapshot hash is invalid")
        return snapshot_hash
    snapshot_hash = workspace_snapshot_hash(str(workspace_root))
    _write_json(
        audit_path,
        {
            "format": "workspace-bench-shared-workspace-snapshot-v1",
            "visibility": "private_only",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "workspace_view_mode": workspace_view_mode,
            "workspace_root": str(workspace_root),
            "source_snapshot_hash": snapshot_hash,
            "hash_method": "workspace_env.integration.workspace_snapshot_hash",
        },
        private=True,
    )
    return snapshot_hash


def run_one_task(
    *,
    position: int,
    item: dict[str, Json],
    args: argparse.Namespace,
    workspace_root: Path,
    base_public_log: Path | None,
    output_root: Path,
    synthesis_script: Path,
) -> dict[str, Json]:
    """Run one isolated real A/B/C synthesis and return its private report."""
    task_id = str(item["task_id"])
    task_root = output_root / "overlays" / f"task-{task_id}"
    # EventSynthesisOrchestrator fail-closes when its output root already
    # exists, so private loop configs must live beside—not inside—the
    # task-specific synthesis artifact.
    config_path = output_root / "configs.private" / f"task-{task_id}.json"
    stored_run_config_path = task_root / "run-config.private.json"
    random_seed = args.seed + position
    if args.resume and stored_run_config_path.is_file():
        stored_run_config = _read_json(stored_run_config_path)
        stored_seed = stored_run_config.get("random_seed")
        if not isinstance(stored_seed, int):
            raise ValueError(f"task {task_id} has an invalid stored random_seed for resume")
        # Subset retries retain their original per-task seed rather than
        # deriving a new one from the retry command's shorter task list.
        random_seed = stored_seed
    config = build_run_config(
        task_id=task_id,
        rubrics=list(item["rubrics"]),
        preferred_files=list(item["preferred_files"]),
        workspace_root=workspace_root,
        output_root=task_root,
        model=args.model,
        auth_mode=args.auth_mode,
        expected_codex_version=args.expected_codex_version,
        timeout_seconds=args.timeout_seconds,
        random_seed=random_seed,
        max_b_failures_per_candidate=args.max_b_failures_per_candidate,
        run_label=args.run_label,
        max_candidate_slots=args.max_candidate_slots,
        workspace_view_mode=getattr(args, "workspace_view_mode", "isolated_masked_copy"),
        shared_workspace_snapshot_hash=getattr(args, "shared_workspace_snapshot_hash", None),
    )
    _write_json(config_path, config, private=True)
    existing_result_path = task_root / "result.json"
    if args.resume and existing_result_path.is_file():
        existing_result = _read_json(existing_result_path)
        if existing_result.get("status") == "TIMELINE_COMPLETE":
            audit_path = task_root / "final" / "targeted-context.private.json"
            if not audit_path.is_file():
                audit_path = write_targeted_audit(
                    task_id=task_id,
                    task_root=task_root,
                    rubric_count=len(config["rubric_contexts"]),
                    rubric_sha256=str(item["rubric_sha256"]),
                    preferred_files=list(item["preferred_files"]),
                    unresolved_filenames=list(item["unresolved_manifest_filenames"]),
                    base_public_log=base_public_log,
                )
            report: dict[str, Json] = {
                "task_id": task_id,
                "rubric_count": len(config["rubric_contexts"]),
                "preferred_file_count": len(config["preferred_files"]),
                "returncode": 0,
                "result": existing_result,
                "resumed_existing_complete": True,
            }
            if audit_path.is_file():
                report["targeted_audit"] = str(audit_path)
            return report
    if args.dry_run:
        return {
            "task_id": task_id,
            "status": "DRY_RUN",
            "rubric_count": len(config["rubric_contexts"]),
            "preferred_file_count": len(config["preferred_files"]),
        }
    command = [sys.executable, str(synthesis_script), "--config", str(config_path)]
    if args.resume and task_root.exists():
        command.append("--resume")
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    report: dict[str, Json] = {
        "task_id": task_id,
        "rubric_count": len(config["rubric_contexts"]),
        "preferred_file_count": len(config["preferred_files"]),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    result_path = task_root / "result.json"
    if completed.returncode == 0 and result_path.is_file():
        result = _read_json(result_path)
        report["result"] = result
        if result.get("status") == "TIMELINE_COMPLETE":
            audit_path = write_targeted_audit(
                task_id=task_id,
                task_root=task_root,
                rubric_count=len(config["rubric_contexts"]),
                rubric_sha256=str(item["rubric_sha256"]),
                preferred_files=list(item["preferred_files"]),
                unresolved_filenames=list(item["unresolved_manifest_filenames"]),
                base_public_log=base_public_log,
            )
            report["targeted_audit"] = str(audit_path)
    return report


def main() -> int:
    args = parse_args()
    task_ids = parse_task_ids(args.task_ids)
    tasks_root = args.tasks_root.resolve(strict=True)
    workspace_root = args.workspace_root.resolve(strict=True)
    if args.base_public_log is None:
        if not args.standalone_overlay_only and not args.finalize_existing_overlays_only:
            raise ValueError("base public log is required unless --standalone-overlay-only is set")
        base_public_log: Path | None = None
    else:
        base_public_log = args.base_public_log.resolve(strict=True)
    output_root = args.output_root.resolve()
    if output_root.exists() and not (args.resume or args.finalize_existing_overlays_only):
        raise ValueError(f"output root already exists: {output_root}")
    if output_root == workspace_root or workspace_root in output_root.parents:
        raise ValueError("output root must be outside the synthesized workspace")
    if args.parallel_workers < 1:
        raise ValueError("parallel workers must be positive")
    if args.max_b_failures_per_candidate < 1:
        raise ValueError("max B failures per candidate must be positive")
    if args.max_candidate_slots < 1:
        raise ValueError("max candidate slots must be positive")
    if not args.run_label or any(char.isspace() for char in args.run_label):
        raise ValueError("run label must be non-empty and contain no whitespace")
    if args.finalize_existing_overlays_only:
        final_log, included_task_ids, excluded_task_ids = compose_overlay_only_final_log(
            batch_root=output_root,
            task_ids=task_ids,
        )
        result = {
            "status": "STANDALONE_PARTIAL_TIMELINE_COMPLETE",
            "final_public_log": str(final_log),
            "included_task_ids": included_task_ids,
            "excluded_task_ids": excluded_task_ids,
        }
        _write_json(output_root / "standalone-final.private.json", result, private=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    args.shared_workspace_snapshot_hash = resolve_shared_workspace_snapshot(
        workspace_root=workspace_root,
        output_root=output_root,
        resume=args.resume,
        workspace_view_mode=args.workspace_view_mode,
    )

    task_inputs: list[dict[str, Json]] = []
    for task_id in task_ids:
        rubrics, rubric_sha256, preferred_files, unresolved_filenames = load_task_context(
            tasks_root,
            workspace_root,
            task_id,
        )
        task_inputs.append(
            {
                "task_id": task_id,
                "rubrics": rubrics,
                "rubric_sha256": rubric_sha256,
                "preferred_files": preferred_files,
                "unresolved_manifest_filenames": unresolved_filenames,
            }
        )
    _write_json(
        output_root / "batch.private.json",
        {
            "format": "workspace-bench-all-rubric-context-loop-v1",
            "visibility": "private_only",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "task_ids": task_ids,
            "task_inputs": task_inputs,
            "base_public_log": str(base_public_log) if base_public_log is not None else None,
            "base_public_log_sha256": _sha256_bytes(base_public_log.read_bytes()) if base_public_log is not None else None,
            "parallel_workers": min(args.parallel_workers, len(task_inputs)),
            "max_b_failures_per_candidate": args.max_b_failures_per_candidate,
            "workspace_view_mode": args.workspace_view_mode,
            "shared_workspace_snapshot_hash": args.shared_workspace_snapshot_hash,
            "run_label": args.run_label,
        },
        private=True,
    )

    synthesis_script = EVALUATION_ROOT / "scripts" / "run_context_event_log_synthesis.py"
    reports_by_task: dict[str, dict[str, Json]] = {}
    worker_count = min(args.parallel_workers, len(task_inputs))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rubric-context") as executor:
        futures = {
            executor.submit(
                run_one_task,
                position=position,
                item=item,
                args=args,
                workspace_root=workspace_root,
                base_public_log=base_public_log,
                output_root=output_root,
                synthesis_script=synthesis_script,
            ): str(item["task_id"])
            for position, item in enumerate(task_inputs)
        }
        for future in as_completed(futures):
            task_id = futures[future]
            try:
                reports_by_task[task_id] = future.result()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                reports_by_task[task_id] = {"task_id": task_id, "returncode": -1, "error": str(error)}
            ordered_partial = [reports_by_task[task_id] for task_id in task_ids if task_id in reports_by_task]
            _write_json(
                output_root / "loop-report.private.json",
                {
                "parallel_workers": worker_count,
                "max_b_failures_per_candidate": args.max_b_failures_per_candidate,
                "workspace_view_mode": args.workspace_view_mode,
                "run_label": args.run_label,
                    "reports": ordered_partial,
                },
                private=True,
            )
    reports = [reports_by_task[task_id] for task_id in task_ids]
    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN", "reports": reports}, ensure_ascii=False))
        return 0
    failures = [
        report
        for report in reports
        if report.get("returncode") != 0 or report.get("result", {}).get("status") != "TIMELINE_COMPLETE"
    ]
    if failures:
        result = {
            "status": "PARTIAL_FAILURE",
            "parallel_workers": worker_count,
            "max_b_failures_per_candidate": args.max_b_failures_per_candidate,
            "run_label": args.run_label,
            "reports": reports,
        }
        _write_json(output_root / "loop-report.private.json", result, private=True)
        print(json.dumps(result, ensure_ascii=False))
        return 2
    if args.standalone_overlay_only:
        final_log, included_task_ids, excluded_task_ids = compose_overlay_only_final_log(
            batch_root=output_root,
            task_ids=task_ids,
        )
    else:
        final_log = compose_final_log(
            batch_root=output_root,
            base_public_log=base_public_log,
            task_ids=task_ids,
        )
        included_task_ids = task_ids
        excluded_task_ids: list[str] = []
    result = {
        "status": "STANDALONE_TIMELINE_COMPLETE" if args.standalone_overlay_only else "TIMELINE_COMPLETE",
        "parallel_workers": worker_count,
        "max_b_failures_per_candidate": args.max_b_failures_per_candidate,
        "run_label": args.run_label,
        "reports": reports,
        "final_public_log": str(final_log),
        "included_task_ids": included_task_ids,
        "excluded_task_ids": excluded_task_ids,
    }
    _write_json(output_root / "loop-report.private.json", result, private=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"all-rubric context loop failed: {error}", file=sys.stderr)
        raise SystemExit(2)
