from .cast import CastOptions, replay_to_cast
from .config import load_cast_options
from .loader import TraceVizInputError, load_agent
from .model import (
    REPLAY_CONTRACT,
    Diagnostic,
    DisplayProjection,
    ReplayEvent,
    ReplayTrace,
    ScheduleOptions,
)
from .normalize import normalize_loaded_agent
from .plain import render_plain
from .sanitize import project_event, sanitize_text
from .serialize import replay_to_dict, replay_to_json


def load_replay(
    source: str,
    schedule_options: ScheduleOptions | None = None,
) -> ReplayTrace:
    return normalize_loaded_agent(
        load_agent(source),
        schedule_options=schedule_options,
    )


__all__ = [
    "REPLAY_CONTRACT",
    "CastOptions",
    "Diagnostic",
    "DisplayProjection",
    "ReplayEvent",
    "ReplayTrace",
    "ScheduleOptions",
    "TraceVizInputError",
    "load_replay",
    "load_cast_options",
    "project_event",
    "render_plain",
    "replay_to_dict",
    "replay_to_cast",
    "replay_to_json",
    "sanitize_text",
]
