"""Author-pinned targeted-context overlays (spec-driven).

The source research repo built these overlays with one bespoke script per task: the
script carried hardcoded reads plus format-specific text extraction, and emitted a
synthetic session whose provenance was labelled ``agent_inference``.

This module keeps exactly that skeleton but makes it reusable:

* the author supplies a **private spec** — sessions whose reads carry the exact
  ``locator`` / ``excerpt`` / ``purpose`` / ``observation`` to publish;
* the builder verifies every read against the real workspace file: path safety,
  pinned size + sha256, and a whitespace-normalised excerpt match inside the
  declared locator range.  A claim that cannot be substantiated aborts the run;
* every emitted event is labelled ``synthetic=true`` /
  ``generation_method=agent_inference`` / ``temporal_basis=synthetic_timestamp``;
* the overlay ships the mandated 11-field private audit (mode ``0600``) and can be
  merged with a validated natural-history base into ``final/events.public.jsonl``.

Nothing is invented here: the author must cite each fact from a workspace file, and
the merge step re-validates the whole public stream.  This intervention is *targeted*
construction — it must be reported separately from natural-history conditions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .event_log import (
    EVENT_LOG_SCHEMA_VERSION,
    MAX_PUBLIC_LABEL_CHARS,
    MAX_PUBLIC_TEXT_CHARS,
    PublicEvent,
    opaque_id,
    validate_visible_events,
)
from .manifest import StrictModel

OVERLAY_SPEC_FORMAT = "envgen-kit.author-pinned-overlay-spec.v1"
AUDIT_FORMAT = "workspace-bench-targeted-context-audit-v1"
COMPOSITION_FORMAT = "workspace-bench-event-log-composition-v1"
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
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".log", ".xml", ".html", ".ini", ".conf"}
SUPPORTED_LOCATOR_KINDS = {"line", "paragraph"}


class TargetedOverlayError(ValueError):
    """Raised when a spec, a source file, or the produced stream is not valid."""


class OverlayRead(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    locator: dict[str, Any]
    excerpt: str = Field(min_length=1, max_length=MAX_PUBLIC_TEXT_CHARS)
    purpose: str = Field(min_length=1, max_length=MAX_PUBLIC_TEXT_CHARS)
    observation: str = Field(min_length=1, max_length=MAX_PUBLIC_TEXT_CHARS)
    mime_type: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_locator(self) -> "OverlayRead":
        kind = self.locator.get("kind")
        if kind not in SUPPORTED_LOCATOR_KINDS:
            raise ValueError(
                f"locator kind {kind!r} is not supported for author-pinned overlays; "
                f"use one of {sorted(SUPPORTED_LOCATOR_KINDS)} (binary formats need a text rendering in the workspace)"
            )
        start, end = self.locator.get("start"), self.locator.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise ValueError("line/paragraph locators need 1-based start <= end")
        return self


class OverlaySession(StrictModel):
    label: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    started_at: datetime
    ended_at: datetime
    actor: Literal["workspace_owner", "workspace_collaborator"] = "workspace_owner"
    title: str = Field(min_length=1, max_length=MAX_PUBLIC_LABEL_CHARS)
    narrative: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)
    application_context: list[str] = Field(min_length=1, max_length=8)
    status: Literal["completed", "abandoned", "interrupted"] = "completed"
    reads: list[OverlayRead] = Field(min_length=1, max_length=64)

    @field_validator("started_at", "ended_at", mode="before")
    @classmethod
    def parse_iso_timestamp(cls, value: Any) -> Any:
        """Accept ISO-8601 strings, which is how the author writes the spec JSON.

        ``StrictModel`` refuses string-to-datetime coercion; the spec format is JSON,
        so the conversion is made explicit here instead of weakening every field.
        """

        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"timestamp must be ISO-8601: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def validate_session(self) -> "OverlaySession":
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("session timestamps must be timezone-aware")
        if self.ended_at < self.started_at:
            raise ValueError("session end must not precede session start")
        if any(not item.strip() for item in self.application_context):
            raise ValueError("application_context entries must be non-empty")
        return self


class OverlaySpec(StrictModel):
    format: Literal[OVERLAY_SPEC_FORMAT] = OVERLAY_SPEC_FORMAT
    run_label: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
    targeted_task_ids: list[str] = Field(min_length=1, max_length=1)
    reporting_note: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)
    sessions: list[OverlaySession] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_spec(self) -> "OverlaySpec":
        labels = [session.label for session in self.sessions]
        if len(labels) != len(set(labels)):
            raise ValueError("session labels must be unique")
        if any(not task_id.strip() for task_id in self.targeted_task_ids):
            raise ValueError("targeted_task_ids must be non-empty")
        return self


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: Any, *, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600 if private else 0o644)
    os.replace(temporary, path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(_canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _safe_workspace_path(workspace: Path, relative: str) -> Path:
    if "\\" in relative or "\x00" in relative:
        raise TargetedOverlayError(f"path must be a POSIX relative path: {relative!r}")
    candidate = (workspace / relative).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise TargetedOverlayError(f"path escapes the workspace: {relative!r}") from exc
    if not candidate.is_file():
        raise TargetedOverlayError(f"source file does not exist: {relative!r}")
    if candidate.is_symlink():
        raise TargetedOverlayError(f"source file must not be a symlink: {relative!r}")
    return candidate


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _verified_excerpt(path: Path, read: OverlayRead) -> str:
    if path.suffix.casefold() not in TEXT_SUFFIXES:
        raise TargetedOverlayError(
            f"{read.path!r} has suffix {path.suffix!r}; author-pinned overlays verify excerpts against text files only"
        )
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start, end = read.locator["start"], read.locator["end"]
    if end > len(lines):
        raise TargetedOverlayError(f"locator {start}-{end} exceeds {read.path!r} ({len(lines)} lines)")
    window = _normalize_text("\n".join(lines[start - 1 : end]))
    if _normalize_text(read.excerpt) not in window:
        raise TargetedOverlayError(
            f"excerpt for {read.path!r} is not contained in lines {start}-{end}; the overlay must cite the file verbatim"
        )
    return window


def _read_provenance() -> dict[str, Any]:
    return {
        "synthetic": True,
        "generation_method": "agent_inference",
        "content_basis": "workspace_content",
        "transition_basis": "agent_inference",
        "temporal_basis": "synthetic_timestamp",
    }


def build_overlay_rows(
    spec: OverlaySpec,
    *,
    workspace: Path,
    workspace_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(public_rows, source_identities)`` after verifying every read."""

    rows: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    for session in spec.sessions:
        session_id = opaque_id("ses", f"{spec.run_label}:{session.label}")
        rows.append(
            {
                "schema_version": EVENT_LOG_SCHEMA_VERSION,
                "event_id": opaque_id("evt", f"{spec.run_label}:{session.label}:start"),
                "occurred_at": session.started_at.astimezone(timezone.utc),
                "workspace_id": workspace_id,
                "session_id": session_id,
                "actor": session.actor,
                "action": "session.start",
                "object": None,
                "payload": {
                    "application_context": session.application_context,
                    "title": session.title,
                    "narrative": session.narrative,
                },
                "provenance": _read_provenance(),
            }
        )
        span = session.ended_at - session.started_at
        for index, read in enumerate(session.reads, start=1):
            source = _safe_workspace_path(workspace, read.path)
            _verified_excerpt(source, read)
            identities.append(
                {
                    "path": read.path,
                    "size_bytes": source.stat().st_size,
                    "sha256": _sha256_file(source),
                }
            )
            occurred_at = session.started_at + span * (index / (len(session.reads) + 1))
            rows.append(
                {
                    "schema_version": EVENT_LOG_SCHEMA_VERSION,
                    "event_id": opaque_id("evt", f"{spec.run_label}:{session.label}:read:{index}:{read.path}"),
                    "occurred_at": occurred_at.astimezone(timezone.utc),
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "actor": session.actor,
                    "action": "file.read",
                    "object": {
                        "object_id": opaque_id("obj", f"{spec.run_label}:{session.label}:{read.path}"),
                        "path_at_event": read.path,
                        "mime_type": read.mime_type,
                        "revision_id": None,
                    },
                    "payload": {
                        "locator": read.locator,
                        "excerpt": read.excerpt,
                        "purpose": read.purpose,
                        "observation": read.observation,
                    },
                    "provenance": _read_provenance(),
                }
            )
        duration = max(0, int(span.total_seconds()))
        rows.append(
            {
                "schema_version": EVENT_LOG_SCHEMA_VERSION,
                "event_id": opaque_id("evt", f"{spec.run_label}:{session.label}:end"),
                "occurred_at": session.ended_at.astimezone(timezone.utc),
                "workspace_id": workspace_id,
                "session_id": session_id,
                "actor": session.actor,
                "action": "session.end",
                "object": None,
                "payload": {"status": session.status, "duration_seconds": duration},
                "provenance": _read_provenance(),
            }
        )
    rows.sort(key=lambda row: (row["occurred_at"], row["event_id"]))
    validated = validate_visible_events(rows)
    return [event.model_dump(mode="json") for event in validated], identities


def build_overlay(
    *,
    spec_path: str | Path,
    workspace_root: str | Path,
    output_root: str | Path,
    base_public_log: str | Path | None = None,
) -> dict[str, Any]:
    """Write the overlay artifacts (and the merged public log when a base is given)."""

    workspace = Path(workspace_root).resolve(strict=True)
    if not workspace.is_dir():
        raise TargetedOverlayError("workspace root must be a directory")
    output = Path(output_root).resolve()
    try:
        output.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise TargetedOverlayError("output root must be outside the workspace")
    if output.exists() and any(output.iterdir()):
        raise TargetedOverlayError("output root must be empty")

    raw = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    spec = OverlaySpec.model_validate(raw)

    base_rows: list[dict[str, Any]] = []
    workspace_id: str | None = None
    if base_public_log is not None:
        base_path = Path(base_public_log).resolve(strict=True)
        base_rows = [
            json.loads(line) for line in base_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        validated_base = validate_visible_events(base_rows)
        workspace_id = validated_base[0].workspace_id
    if workspace_id is None:
        from .integration import workspace_snapshot_hash

        workspace_id = opaque_id("wrk", workspace_snapshot_hash(str(workspace)))

    overlay_rows, identities = build_overlay_rows(spec, workspace=workspace, workspace_id=workspace_id)

    overlay_root = output / "overlay"
    overlay_log = overlay_root / "events.public.jsonl"
    _write_jsonl(overlay_log, overlay_rows)
    audit = {
        "format": AUDIT_FORMAT,
        "visibility": "private_only",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "construction_kind": "targeted_context_construction",
        "targeted_task_ids": list(spec.targeted_task_ids),
        "agent_visible_event_log": "events.public.jsonl",
        "agent_visible_event_log_sha256": _sha256_bytes(overlay_log.read_bytes()),
        "base_natural_log": (
            {
                "available": True,
                "path": str(base_public_log),
                "sha256": _sha256_bytes(Path(base_public_log).read_bytes()),
                "composition": "Merged with this overlay only through the composition audit in final/.",
            }
            if base_public_log is not None
            else {
                "available": False,
                "composition": "No natural-history component; this overlay must be composed with a separately audited base.",
            }
        ),
        "public_content_policy": [
            "Public events are synthetic agent-inferred context; they expose no task IDs, rubric IDs, judge results, pass/fail instructions, reference answers, or rubric-to-event mappings.",
            "Every published excerpt was verified verbatim against a primary workspace file at build time.",
            "The overlay creates and deletes no files, so the final workspace state is preserved.",
        ],
        "construction_scope": {
            "run_label": spec.run_label,
            "session_count": len(spec.sessions),
            "read_count": sum(len(session.reads) for session in spec.sessions),
            "source_identities": identities,
            "spec_sha256": _sha256_bytes(Path(spec_path).read_bytes()),
            "reporting_note": spec.reporting_note,
        },
        "reporting_boundary": (
            "Author-pinned synthetic targeted context. Must be reported separately from natural-history, "
            "no-history, and model-generated targeted-context conditions."
        ),
    }
    if set(audit) != AUDIT_KEYS:
        raise TargetedOverlayError("targeted audit does not use the exact schema fields")
    audit_path = overlay_root / "targeted-context.private.json"
    _write_json(audit_path, audit, private=True)

    result: dict[str, Any] = {
        "status": "OK",
        "overlay_event_log": str(overlay_log),
        "overlay_event_log_sha256": audit["agent_visible_event_log_sha256"],
        "targeted_audit": str(audit_path),
        "event_count": len(overlay_rows),
        "session_count": len(spec.sessions),
    }
    if base_public_log is not None:
        merged = sorted(
            base_rows + overlay_rows,
            key=lambda row: (row["occurred_at"], row["event_id"]),
        )
        validated_merged = validate_visible_events(merged)
        final_root = output / "final"
        final_log = final_root / "events.public.jsonl"
        _write_jsonl(final_log, [event.model_dump(mode="json") for event in validated_merged])
        _write_json(
            final_root / "composition.private.json",
            {
                "format": COMPOSITION_FORMAT,
                "visibility": "private_only",
                "composition_kind": "validated_base_plus_author_pinned_targeted_overlay",
                "components": [
                    {"kind": "natural_history_base", "path": str(base_public_log), "sha256": audit["base_natural_log"]["sha256"]},
                    {"kind": "targeted_context_overlay", "path": str(overlay_log), "sha256": audit["agent_visible_event_log_sha256"]},
                ],
                "agent_visible_event_log": "events.public.jsonl",
                "agent_visible_event_log_sha256": _sha256_bytes(final_log.read_bytes()),
                "reporting_boundary": audit["reporting_boundary"],
            },
            private=True,
        )
        result["final_event_log"] = str(final_log)
        result["final_event_count"] = len(validated_merged)
    return result
