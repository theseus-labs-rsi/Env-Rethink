"""Generate auditable synthetic observation logs from a workspace snapshot.

The benchmark ships workspace snapshots, not historical activity logs.  This
module therefore never infers a user's past actions.  It emits a deterministic
``workspace_automation`` session which observes every safe regular file in a
snapshot.  The public records declare ``generation_method=snapshot_observation``
with metadata-only, synthetic provenance; private artifacts preserve the
inventory fingerprint and canonical ordering needed to reproduce deletion
conditions.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .event_log import (
    CanonicalEvent,
    EVENT_LOG_SCHEMA_VERSION,
    PublicEvent,
    build_visible_log_audit,
    opaque_id,
    select_visible_events,
    validate_canonical_events,
    validate_visible_events,
)


EVENT_GENERATOR_VERSION = "snapshot-observation-v1"
SNAPSHOT_FINGERPRINT_KIND = "metadata_inventory_v1"
SYNTHETIC_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
MIME_BY_SUFFIX = {
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class EventGenerationError(ValueError):
    """Raised when a snapshot cannot be safely turned into an event log."""


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """A regular file admitted to a synthetic snapshot-observation log."""

    relative_path: str
    size_bytes: int
    mode: int
    mtime_ns: int

    def metadata(self) -> dict[str, Any]:
        return {
            "entry_kind": "regular_file",
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "path": self.relative_path,
            "size_bytes": self.size_bytes,
        }

    @property
    def metadata_hash(self) -> str:
        return _sha256_json(self.metadata())


@dataclass(frozen=True, slots=True)
class SnapshotInventory:
    """The safe, metadata-only view of a workspace used by the generator."""

    root: Path
    files: tuple[SnapshotFile, ...]
    skipped_entries: int
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class GeneratedEventLog:
    """Canonical and condition-specific public records for one workspace."""

    inventory: SnapshotInventory
    canonical_events: tuple[CanonicalEvent, ...]
    visible_events: tuple[PublicEvent, ...]
    deletion_rate: float
    deletion_seed: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _relative_path(root: Path, candidate: Path) -> str:
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError as exc:  # pragma: no cover - defensive against filesystem races
        raise EventGenerationError("workspace entry escaped the input root") from exc


def inventory_workspace(workspace_root: str | os.PathLike[str]) -> SnapshotInventory:
    """Enumerate safe regular files without following symlinks or reading content.

    The metadata inventory includes skipped non-regular entries in its private
    fingerprint, so a symlink or device changing in a snapshot still invalidates
    its audit provenance without being exposed to the agent.
    """
    root = Path(workspace_root).resolve(strict=True)
    if not root.is_dir():
        raise EventGenerationError("workspace root must be a directory")

    files: list[SnapshotFile] = []
    fingerprint_entries: list[dict[str, Any]] = []
    skipped_entries = 0
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        retained_dirs: list[str] = []
        for name in sorted(dirnames):
            candidate = directory_path / name
            entry = candidate.lstat()
            relative = _relative_path(root, candidate)
            if stat.S_ISDIR(entry.st_mode):
                retained_dirs.append(name)
                fingerprint_entries.append(
                    {"entry_kind": "directory", "mode": stat.S_IMODE(entry.st_mode), "mtime_ns": entry.st_mtime_ns, "path": relative}
                )
            else:
                skipped_entries += 1
                fingerprint_entries.append(
                    {"entry_kind": "skipped", "mode": stat.S_IMODE(entry.st_mode), "mtime_ns": entry.st_mtime_ns, "path": relative}
                )
        dirnames[:] = retained_dirs
        for name in sorted(filenames):
            candidate = directory_path / name
            entry = candidate.lstat()
            relative = _relative_path(root, candidate)
            if stat.S_ISREG(entry.st_mode):
                snapshot_file = SnapshotFile(
                    relative_path=relative,
                    size_bytes=entry.st_size,
                    mode=stat.S_IMODE(entry.st_mode),
                    mtime_ns=entry.st_mtime_ns,
                )
                files.append(snapshot_file)
                fingerprint_entries.append(snapshot_file.metadata())
            else:
                skipped_entries += 1
                fingerprint_entries.append(
                    {"entry_kind": "skipped", "mode": stat.S_IMODE(entry.st_mode), "mtime_ns": entry.st_mtime_ns, "path": relative}
                )
    files.sort(key=lambda item: item.relative_path)
    fingerprint_entries.sort(key=lambda item: (str(item["path"]), str(item["entry_kind"])))
    return SnapshotInventory(
        root=root,
        files=tuple(files),
        skipped_entries=skipped_entries,
        snapshot_hash=_sha256_json({"kind": SNAPSHOT_FINGERPRINT_KIND, "entries": fingerprint_entries}),
    )


def _mime_type(relative_path: str) -> str | None:
    return MIME_BY_SUFFIX.get(Path(relative_path).suffix.lower())


def generate_snapshot_event_log(
    workspace_root: str | os.PathLike[str],
    *,
    deletion_rate: float = 0.0,
    deletion_seed: int = 0,
) -> GeneratedEventLog:
    """Create a deterministic observation session from every safe input file.

    Timestamps use a fixed synthetic epoch plus the lexicographic inventory
    ordinal.  They establish a deterministic order only and must not be read as
    real user activity times.
    """
    inventory = inventory_workspace(workspace_root)
    workspace_id = opaque_id("wrk", inventory.snapshot_hash)
    session_id = opaque_id("ses", inventory.snapshot_hash + ":snapshot-observation")
    events: list[CanonicalEvent] = []

    def add_event(event: dict[str, Any], *, metadata_hash: str | None = None) -> None:
        events.append(
            CanonicalEvent.model_validate(
                {
                    "event": event,
                    "canonical_sequence": len(events) + 1,
                    "causal_links": [],
                    "source_metadata_hash": metadata_hash,
                    "generator_version": EVENT_GENERATOR_VERSION,
                    "validator_status": "passed",
                }
            )
        )

    add_event(
        {
            "schema_version": EVENT_LOG_SCHEMA_VERSION,
            "event_id": opaque_id("evt", inventory.snapshot_hash + ":session.start"),
            "occurred_at": SYNTHETIC_EPOCH,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "actor": "workspace_automation",
            "action": "session.start",
            "payload": {"application_context": ["workspace_snapshot_inventory"]},
            "provenance": {
                "synthetic": True,
                "generation_method": "snapshot_observation",
                "content_basis": "workspace_metadata",
                "transition_basis": "not_applicable",
                "temporal_basis": "synthetic_timestamp",
            },
        }
    )
    for index, source in enumerate(inventory.files, start=1):
        metadata_hash = source.metadata_hash
        add_event(
            {
                "schema_version": EVENT_LOG_SCHEMA_VERSION,
                "event_id": opaque_id("evt", f"{inventory.snapshot_hash}:file.open:{source.relative_path}"),
                "occurred_at": SYNTHETIC_EPOCH + timedelta(seconds=index),
                "workspace_id": workspace_id,
                "session_id": session_id,
                "actor": "workspace_automation",
                "action": "file.open",
                "object": {
                    "object_id": opaque_id("obj", f"{workspace_id}:{source.relative_path}"),
                    "path_at_event": source.relative_path,
                    "mime_type": _mime_type(source.relative_path),
                    "revision_id": opaque_id("rev", metadata_hash),
                },
                "payload": {
                    "application": "workspace_snapshot_inventory",
                    "open_mode": "read_only",
                    "observation": "metadata_only",
                    "size_bytes": source.size_bytes,
                },
                "provenance": {
                    "synthetic": True,
                    "generation_method": "snapshot_observation",
                    "content_basis": "workspace_metadata",
                    "transition_basis": "not_applicable",
                    "temporal_basis": "synthetic_timestamp",
                },
            },
            metadata_hash=metadata_hash,
        )
    add_event(
        {
            "schema_version": EVENT_LOG_SCHEMA_VERSION,
            "event_id": opaque_id("evt", inventory.snapshot_hash + ":session.end"),
            "occurred_at": SYNTHETIC_EPOCH + timedelta(seconds=len(inventory.files) + 1),
            "workspace_id": workspace_id,
            "session_id": session_id,
            "actor": "workspace_automation",
            "action": "session.end",
            "payload": {"status": "completed", "duration_seconds": len(inventory.files) + 1},
            "provenance": {
                "synthetic": True,
                "generation_method": "snapshot_observation",
                "content_basis": "workspace_metadata",
                "transition_basis": "not_applicable",
                "temporal_basis": "synthetic_timestamp",
            },
        }
    )
    canonical = tuple(validate_canonical_events(events))
    visible = tuple(select_visible_events(canonical, deletion_rate=deletion_rate, deletion_seed=deletion_seed))
    return GeneratedEventLog(
        inventory=inventory,
        canonical_events=canonical,
        visible_events=visible,
        deletion_rate=deletion_rate,
        deletion_seed=deletion_seed,
    )


def _write_jsonl(path: Path, rows: Iterable[CanonicalEvent | PublicEvent]) -> None:
    path.write_text(
        "".join(_canonical_json(row.model_dump(mode="json")) + "\n" for row in rows), encoding="utf-8"
    )
    os.chmod(path, 0o600)


def write_generated_event_log(output_root: str | os.PathLike[str], generated: GeneratedEventLog) -> Path:
    """Persist one generated log atomically outside the workspace input root.

    The output directory is deliberately required to be empty.  This prevents a
    regeneration from silently overwriting an artifact with a different snapshot.
    """
    target = Path(output_root).resolve()
    if target.exists():
        if any(target.iterdir()):
            raise EventGenerationError("event-log output directory must not already contain artifacts")
        target.rmdir()
    try:
        target.relative_to(generated.inventory.root)
    except ValueError:
        pass
    else:
        raise EventGenerationError("event-log output directory must be outside the workspace input root")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_root = Path(tempfile.mkdtemp(prefix=".event-log-", dir=target.parent))
    try:
        canonical_path = temp_root / "canonical.private.jsonl"
        public_path = temp_root / "events.public.jsonl"
        audit_path = temp_root / "audit.private.json"
        manifest_path = temp_root / "generation.private.json"
        _write_jsonl(canonical_path, generated.canonical_events)
        _write_jsonl(public_path, generated.visible_events)
        audit = build_visible_log_audit(
            generated.canonical_events,
            generated.visible_events,
            source_snapshot_hash=generated.inventory.snapshot_hash,
            snapshot_fingerprint_kind=SNAPSHOT_FINGERPRINT_KIND,
            deletion_seed=generated.deletion_seed,
            deletion_rate=generated.deletion_rate,
            generator_version=EVENT_GENERATOR_VERSION,
            validator_version="event-log-v2",
        )
        audit_path.write_text(_canonical_json(audit.model_dump(mode="json")) + "\n", encoding="utf-8")
        manifest_path.write_text(
            _canonical_json(
                {
                    "generator_version": EVENT_GENERATOR_VERSION,
                    "snapshot_fingerprint_kind": SNAPSHOT_FINGERPRINT_KIND,
                    "input_root": str(generated.inventory.root),
                    "file_count": len(generated.inventory.files),
                    "skipped_entries": generated.inventory.skipped_entries,
                    "event_count": len(generated.canonical_events),
                    "visible_event_count": len(generated.visible_events),
                    "synthetic_semantics": "metadata-only snapshot observation; not recovered user history",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        for private_file in (audit_path, manifest_path):
            os.chmod(private_file, 0o600)
        os.chmod(temp_root, 0o700)
        os.replace(temp_root, target)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return target
