from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


REPLAY_CONTRACT = "workspace-bench.replay.v1"

EventKind = Literal["system", "user", "assistant", "tool", "error", "unknown"]
DiagnosticLevel = Literal["warning", "error"]
TimingMode = Literal["recorded", "exact", "step"]
ToolDurationMode = Literal["compressed"]


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    level: DiagnosticLevel = "warning"
    source_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class ScheduleOptions:
    timing: TimingMode = "recorded"
    step_ms: int = 350
    max_gap_ms: int = 2_000
    tool_duration_mode: ToolDurationMode = "compressed"


@dataclass(frozen=True)
class ReplaySchedule:
    timing: TimingMode
    step_ms: int
    max_gap_ms: int
    tool_duration_mode: ToolDurationMode


@dataclass(frozen=True)
class TraceSource:
    agent_json_path: Path
    case_dir: Path
    case_id: str
    task_name: str | None
    agent_name: str | None
    model: str | None
    final_status: str | None
    duration_ms: int | None
    source_sha256: str


@dataclass(frozen=True)
class ReplayEvent:
    replay_index: int
    source_indexes: tuple[int, ...]
    kind: EventKind
    label: str
    role: str | None
    turn: int | None
    call_id: str | None
    status: str | None
    exit_code: int | None
    start_ms: int | None
    end_ms: int | None
    duration_ms: int | None
    has_explicit_end: bool
    offset_ms: int
    input: Any = None
    output: Any = None
    content: str | None = None
    usage: Usage | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayTrace:
    contract: Literal["workspace-bench.replay.v1"]
    source: TraceSource
    events: tuple[ReplayEvent, ...]
    schedule: ReplaySchedule
    total_tokens: int | None
    turns: int | None
    real_duration_ms: int | None
    logical_duration_ms: int
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class DisplayProjection:
    title: str
    summary: str
    input_text: str | None
    output_text: str | None
    raw_json: str
    truncated: bool
    original_bytes: int
