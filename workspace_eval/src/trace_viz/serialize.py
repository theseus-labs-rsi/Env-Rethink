from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .model import Diagnostic, ReplayEvent, ReplayTrace, Usage
from .sanitize import project_event


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _usage_dict(usage: Usage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "promptTokens": usage.prompt_tokens,
        "completionTokens": usage.completion_tokens,
        "totalTokens": usage.total_tokens,
        "cacheRead": usage.cache_read,
        "cacheWrite": usage.cache_write,
    }


def _event_dict(event: ReplayEvent, *, payload_limit: int) -> dict[str, Any]:
    projection = project_event(event, payload_limit=payload_limit)
    return {
        "replayIndex": event.replay_index,
        "sourceIndexes": list(event.source_indexes),
        "kind": event.kind,
        "label": event.label,
        "role": event.role,
        "turn": event.turn,
        "callId": event.call_id,
        "status": event.status,
        "exitCode": event.exit_code,
        "startMs": event.start_ms,
        "endMs": event.end_ms,
        "durationMs": event.duration_ms,
        "hasExplicitEnd": event.has_explicit_end,
        "offsetMs": event.offset_ms,
        "input": event.input,
        "output": event.output,
        "content": event.content,
        "usage": _usage_dict(event.usage),
        "raw": dict(event.raw),
        "display": {
            "title": projection.title,
            "summary": projection.summary,
            "inputText": projection.input_text,
            "outputText": projection.output_text,
            "rawJson": projection.raw_json,
            "truncated": projection.truncated,
            "originalBytes": projection.original_bytes,
        },
        "diagnostics": list(event.diagnostics),
    }


def _diagnostic_dict(diagnostic: Diagnostic) -> dict[str, Any]:
    return {
        "level": diagnostic.level,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "sourceIndexes": list(diagnostic.source_indexes),
    }


def replay_to_dict(
    trace: ReplayTrace,
    *,
    generated_at: str | None = None,
    payload_limit: int = 200 * 1024,
    redact_paths: bool = False,
) -> dict[str, Any]:
    agent_json = (
        "agent.json"
        if redact_paths
        else str(trace.source.agent_json_path)
    )
    return {
        "contract": trace.contract,
        "generatedAt": generated_at or _iso_now(),
        "source": {
            "agentJson": agent_json,
            "sha256": trace.source.source_sha256,
            "caseId": trace.source.case_id,
            "taskName": trace.source.task_name,
            "agent": trace.source.agent_name,
            "model": trace.source.model,
            "status": trace.source.final_status,
            "durationMs": trace.source.duration_ms,
        },
        "summary": {
            "events": len(trace.events),
            "turns": trace.turns,
            "toolCalls": sum(event.kind == "tool" for event in trace.events),
            "totalTokens": trace.total_tokens,
            "realDurationMs": trace.real_duration_ms,
            "logicalDurationMs": trace.logical_duration_ms,
        },
        "schedule": {
            "timing": trace.schedule.timing,
            "stepMs": trace.schedule.step_ms,
            "maxGapMs": trace.schedule.max_gap_ms,
            "toolDurationMode": trace.schedule.tool_duration_mode,
        },
        "events": [
            _event_dict(event, payload_limit=payload_limit) for event in trace.events
        ],
        "diagnostics": [
            _diagnostic_dict(diagnostic) for diagnostic in trace.diagnostics
        ],
    }


def replay_to_json(
    trace: ReplayTrace,
    *,
    generated_at: str | None = None,
    payload_limit: int = 200 * 1024,
    redact_paths: bool = False,
) -> str:
    return json.dumps(
        replay_to_dict(
            trace,
            generated_at=generated_at,
            payload_limit=payload_limit,
            redact_paths=redact_paths,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    ) + "\n"
