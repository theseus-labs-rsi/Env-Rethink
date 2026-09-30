from __future__ import annotations

from dataclasses import replace

from .model import Diagnostic, ReplayEvent, ScheduleOptions


def apply_schedule(
    events: tuple[ReplayEvent, ...],
    options: ScheduleOptions,
) -> tuple[tuple[ReplayEvent, ...], int, tuple[Diagnostic, ...]]:
    if options.step_ms < 0:
        raise ValueError("step_ms must be non-negative")
    if options.max_gap_ms < 0:
        raise ValueError("max_gap_ms must be non-negative")

    scheduled: list[ReplayEvent] = []
    diagnostics: list[Diagnostic] = []
    trace_start = next((event.start_ms for event in events if event.start_ms is not None), None)
    previous_offset = 0
    previous_trusted_start: int | None = None

    for index, event in enumerate(events):
        source_index = event.source_indexes[0] if event.source_indexes else None
        has_trusted_start = event.start_ms is not None and trace_start is not None

        if options.timing == "step":
            offset = index * options.step_ms
        elif has_trusted_start:
            assert event.start_ms is not None and trace_start is not None
            raw_offset = max(0, event.start_ms - trace_start)
            if previous_trusted_start is not None and event.start_ms < previous_trusted_start:
                diagnostics.append(
                    Diagnostic(
                        code="timestamp_backwards",
                        message=(
                            f"source event {source_index} starts before the previous trusted "
                            "timestamp; source order was preserved"
                        ),
                        source_indexes=event.source_indexes,
                    )
                )

            if options.timing == "exact":
                offset = max(previous_offset, raw_offset)
            else:
                raw_gap = max(0, raw_offset - previous_offset)
                offset = previous_offset + min(raw_gap, options.max_gap_ms)
            previous_trusted_start = event.start_ms
        else:
            offset = 0 if index == 0 else previous_offset + options.step_ms

        scheduled_event = replace(event, replay_index=index, offset_ms=max(0, offset))
        scheduled.append(scheduled_event)
        previous_offset = scheduled_event.offset_ms

    logical_duration_ms = scheduled[-1].offset_ms if scheduled else 0
    return tuple(scheduled), logical_duration_ms, tuple(diagnostics)
