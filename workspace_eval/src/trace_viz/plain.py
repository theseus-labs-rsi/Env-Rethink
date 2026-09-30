from __future__ import annotations

from .model import ReplayTrace
from .sanitize import project_event


def format_offset(offset_ms: int) -> str:
    minutes, remainder = divmod(max(0, offset_ms), 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def render_plain(trace: ReplayTrace, *, payload_limit: int = 200 * 1024) -> str:
    source = trace.source
    lines = [
        "Workspace-Bench Trace Replay",
        f"contract: {trace.contract}",
        f"source: {source.agent_json_path}",
        f"sha256: {source.source_sha256}",
        f"case: {source.case_id}",
        f"task: {source.task_name or '-'}",
        f"agent/model: {source.agent_name or '-'} / {source.model or '-'}",
        f"status: {source.final_status or '-'}",
        (
            "summary: "
            f"events={len(trace.events)} "
            f"tools={sum(event.kind == 'tool' for event in trace.events)} "
            f"turns={trace.turns if trace.turns is not None else '-'} "
            f"tokens={trace.total_tokens if trace.total_tokens is not None else '-'} "
            f"realMs={trace.real_duration_ms if trace.real_duration_ms is not None else '-'} "
            f"logicalMs={trace.logical_duration_ms}"
        ),
        (
            "schedule: "
            f"timing={trace.schedule.timing} "
            f"stepMs={trace.schedule.step_ms} "
            f"maxGapMs={trace.schedule.max_gap_ms} "
            f"toolDurationMode={trace.schedule.tool_duration_mode}"
        ),
        "",
        "Events",
    ]

    for event in trace.events:
        projection = project_event(event, payload_limit=payload_limit)
        turn = f" turn={event.turn}" if event.turn is not None else ""
        status = f" status={event.status}" if event.status else ""
        duration = (
            f" durationMs={event.duration_ms}"
            if event.duration_ms is not None
            else ""
        )
        warning = " !" if event.diagnostics else ""
        lines.append(
            f"[{event.replay_index:04d}] {format_offset(event.offset_ms)} "
            f"{event.kind.upper():9} {event.label}"
            f"{turn}{status}{duration}{warning}"
        )
        if projection.summary and projection.summary != projection.title:
            lines.append(f"       {projection.summary}")

    if trace.diagnostics:
        lines.extend(["", "Diagnostics"])
        for diagnostic in trace.diagnostics:
            indexes = (
                f" sourceIndexes={list(diagnostic.source_indexes)}"
                if diagnostic.source_indexes
                else ""
            )
            lines.append(
                f"- {diagnostic.level.upper()} {diagnostic.code}: "
                f"{diagnostic.message}{indexes}"
            )

    return "\n".join(lines) + "\n"
