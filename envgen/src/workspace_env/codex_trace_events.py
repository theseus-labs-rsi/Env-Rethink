"""Convert complete Codex execution traces into auditable public event logs.

This adapter intentionally has no command-to-file heuristic.  A Codex
``exec_command`` trace item becomes one ``shell.command`` event with the exact
recorded command and lifecycle fields.  Any other tool type makes conversion
fail closed, so the result is never advertised as a complete trace-derived log
when it omits observed tool activity.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .event_log import (
    CanonicalEvent,
    EVENT_LOG_SCHEMA_VERSION,
    MAX_PUBLIC_TEXT_CHARS,
    PublicEvent,
    build_visible_log_audit,
    opaque_id,
    select_visible_events,
    validate_canonical_events,
    validate_visible_events,
)


CODEX_TRACE_EVENT_GENERATOR_VERSION = "codex-trace-events-v1"


class CodexTraceConversionError(ValueError):
    """Raised when a trace cannot be represented completely and safely."""


@dataclass(frozen=True, slots=True)
class CodexTraceEventLog:
    canonical_events: tuple[CanonicalEvent, ...]
    visible_events: tuple[PublicEvent, ...]
    workspace_snapshot_hash: str
    source_trace_hash: str
    deletion_rate: float
    deletion_seed: int
    source_tool_event_count: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _event_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CodexTraceConversionError("Codex trace tool event is missing its recorded timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CodexTraceConversionError("Codex trace tool event timestamp is invalid") from exc


def _strict_trace_tools(execution_trace: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    tools = [item for item in execution_trace if item.get("type") == "tool"]
    unsupported = sorted(
        {
            str(item.get("tool") or "<missing>")
            for item in tools
            if item.get("tool") != "exec_command"
        }
    )
    if unsupported:
        raise CodexTraceConversionError(
            "trace contains tool activity without an exact public event mapping: " + ", ".join(unsupported)
        )
    return tools


def convert_codex_execution_trace(
    execution_trace: Sequence[dict[str, Any]],
    *,
    workspace_snapshot_hash: str,
    session_material: str,
    duration_ms: int,
    deletion_rate: float = 0.0,
    deletion_seed: int = 0,
) -> CodexTraceEventLog:
    """Convert a complete normalized Codex trace without inferring file reads.

    ``execution_trace`` must come from the collector's complete JSONL parsing.
    The caller must retain the original JSONL separately; private events only
    store hashes of normalized records, never replace the raw evidence.
    """
    if not workspace_snapshot_hash.startswith("sha256:") or len(workspace_snapshot_hash) != 71:
        raise CodexTraceConversionError("workspace snapshot hash must be a sha256 digest")
    if not session_material:
        raise CodexTraceConversionError("Codex session material is required")
    if duration_ms < 0:
        raise CodexTraceConversionError("Codex trace duration must be non-negative")
    trace = [dict(item) for item in execution_trace]
    tools = _strict_trace_tools(trace)
    source_trace_hash = _sha256_json(trace)
    workspace_id = opaque_id("wrk", workspace_snapshot_hash)
    session_id = opaque_id("ses", session_material)
    session_events = [item for item in trace if item.get("eventType") == "harness.prompt"]
    if session_events:
        started_at = _event_timestamp(session_events[0].get("timestamp"))
    elif tools:
        started_at = _event_timestamp(tools[0].get("startedAt") or tools[0].get("timestamp"))
    else:
        raise CodexTraceConversionError("trace contains neither tool activity nor a recorded harness prompt")
    events: list[CanonicalEvent] = []

    def add_event(payload: dict[str, Any], *, source_item: dict[str, Any] | None = None) -> None:
        events.append(
            CanonicalEvent.model_validate(
                {
                    "event": payload,
                    "canonical_sequence": len(events) + 1,
                    "causal_links": [],
                    "source_trace_hash": _sha256_json(source_item) if source_item is not None else source_trace_hash,
                    "generator_version": CODEX_TRACE_EVENT_GENERATOR_VERSION,
                    "validator_status": "passed",
                }
            )
        )

    add_event(
        {
            "schema_version": EVENT_LOG_SCHEMA_VERSION,
            "event_id": opaque_id("evt", session_material + ":session.start"),
            "occurred_at": started_at,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "actor": "codex_agent",
            "action": "session.start",
            "payload": {"application_context": ["codex_cli"]},
            "provenance": {
                "synthetic": False,
                "generation_method": "trace_conversion",
                "content_basis": "trace_record",
                "transition_basis": "trace_record",
                "temporal_basis": "observed_timestamp",
            },
        }
    )
    for tool in tools:
        command = tool.get("input", {}).get("command") if isinstance(tool.get("input"), dict) else None
        if not isinstance(command, str) or not command:
            raise CodexTraceConversionError("Codex exec_command trace item is missing its exact command")
        if len(command) > MAX_PUBLIC_TEXT_CHARS:
            raise CodexTraceConversionError("Codex command exceeds the public event text limit")
        call_id = tool.get("callID")
        if not isinstance(call_id, str) or not call_id:
            raise CodexTraceConversionError("Codex exec_command trace item is missing its call ID")
        status = tool.get("status")
        if status not in {"completed", "failed", "declined"}:
            raise CodexTraceConversionError("Codex exec_command trace item does not have a terminal status")
        exit_code = tool.get("exitCode")
        if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
            raise CodexTraceConversionError("Codex exec_command exit code is invalid")
        duration = tool.get("durationMs")
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 0):
            raise CodexTraceConversionError("Codex exec_command duration is invalid")
        add_event(
            {
                "schema_version": EVENT_LOG_SCHEMA_VERSION,
                "event_id": opaque_id("evt", session_material + ":" + call_id),
                "occurred_at": _event_timestamp(tool.get("startedAt") or tool.get("timestamp")),
                "workspace_id": workspace_id,
                "session_id": session_id,
                "actor": "codex_agent",
                "action": "shell.command",
                "payload": {
                    "command": command,
                    "status": status,
                    "exit_code": exit_code,
                    "duration_ms": duration,
                },
                "provenance": {
                    "synthetic": False,
                    "generation_method": "trace_conversion",
                    "content_basis": "trace_record",
                    "transition_basis": "trace_record",
                    "temporal_basis": "observed_timestamp",
                },
            },
            source_item=tool,
        )
    ended_at = started_at + timedelta(milliseconds=duration_ms)
    add_event(
        {
            "schema_version": EVENT_LOG_SCHEMA_VERSION,
            "event_id": opaque_id("evt", session_material + ":session.end"),
            "occurred_at": ended_at,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "actor": "codex_agent",
            "action": "session.end",
            "payload": {"status": "completed", "duration_seconds": duration_ms // 1000},
            "provenance": {
                "synthetic": False,
                "generation_method": "trace_conversion",
                "content_basis": "trace_record",
                "transition_basis": "trace_record",
                "temporal_basis": "observed_timestamp",
            },
        }
    )
    canonical = tuple(validate_canonical_events(events))
    visible = tuple(select_visible_events(canonical, deletion_rate=deletion_rate, deletion_seed=deletion_seed))
    return CodexTraceEventLog(
        canonical_events=canonical,
        visible_events=visible,
        workspace_snapshot_hash=workspace_snapshot_hash,
        source_trace_hash=source_trace_hash,
        deletion_rate=deletion_rate,
        deletion_seed=deletion_seed,
        source_tool_event_count=len(tools),
    )


def _write_jsonl(path: Path, rows: Iterable[CanonicalEvent | PublicEvent]) -> None:
    path.write_text("".join(_canonical_json(row.model_dump(mode="json")) + "\n" for row in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def write_codex_trace_event_log(output_root: str | os.PathLike[str], event_log: CodexTraceEventLog) -> Path:
    """Atomically write trace-derived event artifacts to a new directory."""
    target = Path(output_root).resolve()
    if target.exists():
        if any(target.iterdir()):
            raise CodexTraceConversionError("Codex trace event-log output directory must be empty")
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".codex-trace-event-log-", dir=target.parent))
    try:
        _write_jsonl(temporary / "canonical.private.jsonl", event_log.canonical_events)
        _write_jsonl(temporary / "events.public.jsonl", event_log.visible_events)
        audit = build_visible_log_audit(
            event_log.canonical_events,
            event_log.visible_events,
            source_snapshot_hash=event_log.workspace_snapshot_hash,
            snapshot_fingerprint_kind="content_inventory_v1",
            deletion_seed=event_log.deletion_seed,
            deletion_rate=event_log.deletion_rate,
            generator_version=CODEX_TRACE_EVENT_GENERATOR_VERSION,
            validator_version="event-log-v2",
        )
        (temporary / "audit.private.json").write_text(
            _canonical_json(audit.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        (temporary / "conversion.private.json").write_text(
            _canonical_json(
                {
                    "generator_version": CODEX_TRACE_EVENT_GENERATOR_VERSION,
                    "source": "complete_codex_execution_trace",
                    "source_trace_hash": event_log.source_trace_hash,
                    "source_tool_event_count": event_log.source_tool_event_count,
                    "public_event_count": len(event_log.visible_events),
                    "conversion_policy": "direct exec_command mapping only; unsupported tool activity fails closed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        for path in (temporary / "audit.private.json", temporary / "conversion.private.json"):
            os.chmod(path, 0o600)
        os.chmod(temporary, 0o700)
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target
