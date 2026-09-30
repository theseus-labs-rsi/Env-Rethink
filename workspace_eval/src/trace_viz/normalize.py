from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping

from .loader import LoadedAgent
from .model import (
    REPLAY_CONTRACT,
    Diagnostic,
    EventKind,
    ReplayEvent,
    ReplaySchedule,
    ReplayTrace,
    ScheduleOptions,
    TraceSource,
    Usage,
)
from .schedule import apply_schedule


@dataclass(frozen=True)
class _ParsedEvent:
    event: ReplayEvent
    diagnostics: tuple[Diagnostic, ...]


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any, *, non_negative: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    integer = int(value)
    if non_negative and integer < 0:
        return None
    return integer


def _parse_timestamp(
    value: Any,
    *,
    field_name: str,
    source_index: int,
) -> tuple[int | None, Diagnostic | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, Diagnostic(
            code="invalid_timestamp",
            message=f"source event {source_index} has an invalid {field_name}",
            source_indexes=(source_index,),
        )
    if isinstance(value, (int, float)):
        return int(value), None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None, None
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000), None
        except ValueError:
            return None, Diagnostic(
                code="invalid_timestamp",
                message=(
                    f"source event {source_index} has an invalid {field_name}: "
                    f"{candidate!r}"
                ),
                source_indexes=(source_index,),
            )
    return None, Diagnostic(
        code="invalid_timestamp",
        message=f"source event {source_index} has an invalid {field_name}",
        source_indexes=(source_index,),
    )


def _usage_from_event(raw: Mapping[str, Any]) -> Usage | None:
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        return None
    usage = llm.get("usage")
    if not isinstance(usage, dict):
        return None

    values = {
        "prompt_tokens": _optional_int(usage.get("prompt_tokens"), non_negative=True),
        "completion_tokens": _optional_int(
            usage.get("completion_tokens"), non_negative=True
        ),
        "total_tokens": _optional_int(usage.get("total_tokens"), non_negative=True),
        "cache_read": _optional_int(usage.get("cache_read"), non_negative=True),
        "cache_write": _optional_int(usage.get("cache_write"), non_negative=True),
    }
    if all(value is None for value in values.values()):
        return None
    return Usage(**values)


def _event_kind(raw: Mapping[str, Any]) -> EventKind:
    event_type = raw.get("type")
    role = raw.get("role")
    if event_type == "tool":
        return "tool"
    if event_type == "text":
        if role in {"system", "user", "assistant"}:
            return role
        return "unknown"
    if event_type == "error":
        return "error"
    return "unknown"


def _label_for_event(raw: Mapping[str, Any], kind: EventKind) -> str:
    if kind == "tool":
        tool = raw.get("tool")
        return tool if isinstance(tool, str) and tool else "tool"
    return kind.upper()


def _has_error_flag(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("isError") is True:
        return True
    details = value.get("details")
    return isinstance(details, dict) and details.get("isError") is True


def _derive_status(
    *,
    raw_status: Any,
    output: Any,
    exit_code: int | None,
    end_ms: int | None,
) -> str:
    if isinstance(raw_status, str) and raw_status:
        return raw_status
    if _has_error_flag(output) or (exit_code is not None and exit_code != 0):
        return "failed"
    if end_ms is not None or not _is_empty_value(output):
        return "completed"
    return "unknown"


def _parse_event(raw: Mapping[str, Any], source_index: int) -> _ParsedEvent:
    diagnostics: list[Diagnostic] = []
    kind = _event_kind(raw)
    if kind == "unknown":
        diagnostics.append(
            Diagnostic(
                code="unknown_event_type",
                message=(
                    f"source event {source_index} has unknown type "
                    f"{raw.get('type')!r}"
                ),
                source_indexes=(source_index,),
            )
        )

    timestamp_ms, diagnostic = _parse_timestamp(
        raw.get("timestamp"),
        field_name="timestamp",
        source_index=source_index,
    )
    if diagnostic:
        diagnostics.append(diagnostic)
    started_ms, diagnostic = _parse_timestamp(
        raw.get("startedAt"),
        field_name="startedAt",
        source_index=source_index,
    )
    if diagnostic:
        diagnostics.append(diagnostic)
    finished_ms, diagnostic = _parse_timestamp(
        raw.get("finishedAt"),
        field_name="finishedAt",
        source_index=source_index,
    )
    if diagnostic:
        diagnostics.append(diagnostic)

    start_ms = started_ms if kind == "tool" and started_ms is not None else timestamp_ms
    duration_raw = raw.get("durationMs")
    duration_ms = _optional_int(duration_raw, non_negative=True)
    if (
        duration_raw is not None
        and isinstance(duration_raw, (int, float))
        and not isinstance(duration_raw, bool)
        and duration_raw < 0
    ):
        diagnostics.append(
            Diagnostic(
                code="negative_duration",
                message=f"source event {source_index} has a negative durationMs",
                source_indexes=(source_index,),
            )
        )

    end_ms = finished_ms
    if end_ms is None and start_ms is not None and duration_ms is not None:
        end_ms = start_ms + duration_ms
    if kind != "tool" and end_ms is None and start_ms is not None:
        end_ms = start_ms
    if duration_ms is None and start_ms is not None and end_ms is not None:
        duration_ms = max(0, end_ms - start_ms)

    exit_code = _optional_int(raw.get("exitCode"))
    status = None
    if kind == "tool":
        status = _derive_status(
            raw_status=raw.get("status"),
            output=raw.get("output"),
            exit_code=exit_code,
            end_ms=finished_ms,
        )
        if status == "completed" and exit_code is not None and exit_code != 0:
            diagnostics.append(
                Diagnostic(
                    code="status_exit_code_conflict",
                    message=(
                        f"source event {source_index} is completed but has non-zero "
                        f"exitCode {exit_code}"
                    ),
                    source_indexes=(source_index,),
                )
            )
        if status == "failed" and exit_code == 0:
            diagnostics.append(
                Diagnostic(
                    code="status_exit_code_conflict",
                    message=(
                        f"source event {source_index} is failed but has exitCode 0"
                    ),
                    source_indexes=(source_index,),
                )
            )

    event_diagnostic_codes = tuple(dict.fromkeys(item.code for item in diagnostics))
    event = ReplayEvent(
        replay_index=-1,
        source_indexes=(source_index,),
        kind=kind,
        label=_label_for_event(raw, kind),
        role=_optional_string(raw.get("role")),
        turn=_optional_int(raw.get("turn"), non_negative=True),
        call_id=_optional_string(raw.get("callID")),
        status=status,
        exit_code=exit_code,
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=duration_ms,
        has_explicit_end=finished_ms is not None,
        offset_ms=0,
        input=raw.get("input"),
        output=raw.get("output"),
        content=_optional_string(raw.get("content")),
        usage=_usage_from_event(raw),
        raw=dict(raw),
        diagnostics=event_diagnostic_codes,
    )
    return _ParsedEvent(event=event, diagnostics=tuple(diagnostics))


def _values_compatible(first: Any, second: Any) -> bool:
    if _is_empty_value(first) or _is_empty_value(second):
        return True
    return first == second


def _can_merge(first: ReplayEvent, second: ReplayEvent) -> bool:
    return (
        first.kind == "tool"
        and second.kind == "tool"
        and first.call_id is not None
        and first.call_id == second.call_id
        and first.label == second.label
        and _values_compatible(first.input, second.input)
        and _values_compatible(first.output, second.output)
    )


def _is_empty_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _choose_first_non_none(first: Any, second: Any) -> Any:
    return first if not _is_empty_value(first) else second


def _choose_last_non_none(first: Any, second: Any) -> Any:
    return second if not _is_empty_value(second) else first


def _merge_events(first: ReplayEvent, second: ReplayEvent) -> ReplayEvent:
    start_ms = _choose_first_non_none(first.start_ms, second.start_ms)
    if second.has_explicit_end:
        end_ms = second.end_ms
    elif first.has_explicit_end:
        end_ms = first.end_ms
    else:
        end_ms = _choose_last_non_none(first.end_ms, second.end_ms)
    explicit_duration = _choose_last_non_none(first.duration_ms, second.duration_ms)
    if start_ms is not None and end_ms is not None:
        duration_ms = max(0, end_ms - start_ms)
    else:
        duration_ms = explicit_duration

    raw = {
        "merged": [dict(first.raw), dict(second.raw)],
    }
    return replace(
        first,
        source_indexes=first.source_indexes + second.source_indexes,
        role=_choose_last_non_none(first.role, second.role),
        turn=_choose_last_non_none(first.turn, second.turn),
        status=_derive_status(
            raw_status=_choose_last_non_none(first.status, second.status),
            output=_choose_last_non_none(first.output, second.output),
            exit_code=_choose_last_non_none(first.exit_code, second.exit_code),
            end_ms=end_ms,
        ),
        exit_code=_choose_last_non_none(first.exit_code, second.exit_code),
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=duration_ms,
        has_explicit_end=first.has_explicit_end or second.has_explicit_end,
        input=_choose_first_non_none(first.input, second.input),
        output=_choose_last_non_none(first.output, second.output),
        raw=raw,
        diagnostics=tuple(
            dict.fromkeys(first.diagnostics + second.diagnostics)
        ),
    )


def _merge_duplicate_tools(
    events: list[ReplayEvent],
) -> tuple[list[ReplayEvent], tuple[Diagnostic, ...]]:
    merged: list[ReplayEvent] = []
    diagnostics: list[Diagnostic] = []
    positions_by_call_id: dict[str, list[int]] = {}

    for event in events:
        if event.kind != "tool" or not event.call_id:
            merged.append(event)
            continue

        merged_position: int | None = None
        for position in reversed(positions_by_call_id.get(event.call_id, [])):
            candidate = merged[position]
            if candidate.label != event.label:
                diagnostics.append(
                    Diagnostic(
                        code="duplicate_call_id_tool_mismatch",
                        message=(
                            f"callID {event.call_id!r} is used by both "
                            f"{candidate.label!r} and {event.label!r}; events were not merged"
                        ),
                        source_indexes=candidate.source_indexes + event.source_indexes,
                    )
                )
                continue
            if _can_merge(candidate, event):
                merged_position = position
                break
            diagnostics.append(
                Diagnostic(
                    code="duplicate_call_id_payload_conflict",
                    message=(
                        f"callID {event.call_id!r} has conflicting non-empty input or "
                        "output; events were not merged"
                    ),
                    source_indexes=candidate.source_indexes + event.source_indexes,
                )
            )

        if merged_position is None:
            positions_by_call_id.setdefault(event.call_id, []).append(len(merged))
            merged.append(event)
        else:
            merged[merged_position] = _merge_events(merged[merged_position], event)

    return merged, tuple(diagnostics)


def _source_model(document: Mapping[str, Any]) -> str | None:
    trace = document.get("trace")
    if isinstance(trace, dict):
        llm = trace.get("llm")
        if isinstance(llm, dict) and isinstance(llm.get("model"), str):
            return llm["model"]
        for item in trace.get("executionTrace", []):
            if not isinstance(item, dict):
                continue
            event_llm = item.get("llm")
            if isinstance(event_llm, dict) and isinstance(event_llm.get("model"), str):
                return event_llm["model"]
    return None


def _source_agent_name(document: Mapping[str, Any]) -> str | None:
    for key in ("agent", "agentName", "harness"):
        value = document.get(key)
        if isinstance(value, str) and value:
            return value
    trace = document.get("trace")
    if isinstance(trace, dict):
        llm = trace.get("llm")
        if isinstance(llm, dict):
            provider = llm.get("provider")
            if isinstance(provider, str) and provider:
                return provider
    return None


def _real_duration(events: list[ReplayEvent], document_duration: int | None) -> int | None:
    if document_duration is not None:
        return document_duration
    starts = [event.start_ms for event in events if event.start_ms is not None]
    ends = [event.end_ms for event in events if event.end_ms is not None]
    if not starts or not ends:
        return None
    return max(0, max(ends) - min(starts))


def normalize_loaded_agent(
    loaded: LoadedAgent,
    *,
    schedule_options: ScheduleOptions | None = None,
) -> ReplayTrace:
    options = schedule_options or ScheduleOptions()
    diagnostics: list[Diagnostic] = []
    parsed_events: list[ReplayEvent] = []

    for source_index, raw_event in enumerate(loaded.execution_trace):
        if not isinstance(raw_event, dict):
            diagnostics.append(
                Diagnostic(
                    code="non_object_event",
                    message=f"source event {source_index} is not an object and was skipped",
                    source_indexes=(source_index,),
                )
            )
            continue
        parsed = _parse_event(raw_event, source_index)
        parsed_events.append(parsed.event)
        diagnostics.extend(parsed.diagnostics)

    merged_events, merge_diagnostics = _merge_duplicate_tools(parsed_events)
    diagnostics.extend(merge_diagnostics)
    scheduled_events, logical_duration_ms, schedule_diagnostics = apply_schedule(
        tuple(merged_events),
        options,
    )
    diagnostics.extend(schedule_diagnostics)
    diagnostic_codes_by_source_index: dict[int, list[str]] = {}
    for diagnostic in diagnostics:
        for source_index in diagnostic.source_indexes:
            codes = diagnostic_codes_by_source_index.setdefault(source_index, [])
            if diagnostic.code not in codes:
                codes.append(diagnostic.code)
    scheduled_events = tuple(
        replace(
            event,
            diagnostics=tuple(
                dict.fromkeys(
                    event.diagnostics
                    + tuple(
                        code
                        for source_index in event.source_indexes
                        for code in diagnostic_codes_by_source_index.get(source_index, ())
                    )
                )
            ),
        )
        for event in scheduled_events
    )

    duration_ms = _optional_int(loaded.document.get("durationMs"), non_negative=True)
    source = TraceSource(
        agent_json_path=loaded.path,
        case_dir=loaded.case_dir,
        case_id=str(loaded.document.get("caseId") or loaded.case_dir.name),
        task_name=_optional_string(loaded.document.get("name")),
        agent_name=_source_agent_name(loaded.document),
        model=_source_model(loaded.document),
        final_status=_optional_string(loaded.document.get("status")),
        duration_ms=duration_ms,
        source_sha256=loaded.source_sha256,
    )
    turns = _optional_int(loaded.document.get("turns"), non_negative=True)
    if turns is None:
        event_turns = [
            event.turn for event in scheduled_events if event.turn is not None
        ]
        turns = max(event_turns, default=None)

    total_tokens = _optional_int(
        loaded.document.get("totalTokens"),
        non_negative=True,
    )
    if total_tokens is None:
        usage_totals = [
            event.usage.total_tokens
            for event in scheduled_events
            if event.usage is not None and event.usage.total_tokens is not None
        ]
        total_tokens = sum(usage_totals) if usage_totals else None

    return ReplayTrace(
        contract=REPLAY_CONTRACT,
        source=source,
        events=scheduled_events,
        schedule=ReplaySchedule(
            timing=options.timing,
            step_ms=options.step_ms,
            max_gap_ms=options.max_gap_ms,
            tool_duration_mode=options.tool_duration_mode,
        ),
        total_tokens=total_tokens,
        turns=turns,
        real_duration_ms=_real_duration(merged_events, duration_ms),
        logical_duration_ms=logical_duration_ms,
        diagnostics=tuple(diagnostics),
    )
