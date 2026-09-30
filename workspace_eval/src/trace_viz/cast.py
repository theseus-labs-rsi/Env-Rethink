from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .model import ReplayEvent, ReplayTrace
from .sanitize import display_text, sanitize_text, truncate_utf8


CAST_VERSION = 2
ANSI_RESET = "\x1b[0m"
ANSI_DIM = "\x1b[2m"
ANSI_BOLD = "\x1b[1m"
ANSI_RED = "\x1b[31m"
ANSI_GREEN = "\x1b[32m"
ANSI_YELLOW = "\x1b[33m"
ANSI_BLUE = "\x1b[34m"
ANSI_MAGENTA = "\x1b[35m"
ANSI_CYAN = "\x1b[36m"
ANSI_WHITE = "\x1b[37m"


@dataclass(frozen=True)
class CastOptions:
    columns: int = 120
    rows: int = 36
    payload_limit: int = 200 * 1024
    tail_hold_ms: int = 1_200
    command_cps: int = 45
    user_cps: int = 55
    assistant_cps: int = 55
    text_cps: int = 80
    tool_input_cps: int = 160
    tool_output_cps: int = 240
    max_animated_chars: int = 4_000
    inter_event_ms: int = 80


def validate_cast_options(options: CastOptions) -> None:
    if options.columns < 20:
        raise ValueError("columns must be at least 20")
    if options.rows < 5:
        raise ValueError("rows must be at least 5")
    if options.payload_limit < 0:
        raise ValueError("payload_limit must be non-negative")
    if options.tail_hold_ms < 0:
        raise ValueError("tail_hold_ms must be non-negative")
    if options.command_cps <= 0:
        raise ValueError("command_cps must be positive")
    if options.user_cps <= 0:
        raise ValueError("user_cps must be positive")
    if options.assistant_cps <= 0:
        raise ValueError("assistant_cps must be positive")
    if options.text_cps <= 0:
        raise ValueError("text_cps must be positive")
    if options.tool_input_cps <= 0:
        raise ValueError("tool_input_cps must be positive")
    if options.tool_output_cps <= 0:
        raise ValueError("tool_output_cps must be positive")
    if options.max_animated_chars < 0:
        raise ValueError("max_animated_chars must be non-negative")
    if options.inter_event_ms < 0:
        raise ValueError("inter_event_ms must be non-negative")


def replay_to_cast(
    trace: ReplayTrace,
    *,
    options: CastOptions | None = None,
    timestamp: int | None = None,
) -> str:
    cast_options = options or CastOptions()
    validate_cast_options(cast_options)

    frames: list[list[Any]] = [[0.0, "o", "\x1b[2J\x1b[H"]]
    previous_time = 0.0
    for event in trace.events:
        event_time = max(
            event.offset_ms / 1_000,
            previous_time
            + (cast_options.inter_event_ms / 1_000 if event.replay_index else 0),
        )
        frames.append([round(event_time, 6), "m", _marker_label(event)])
        event_frames, event_end = _event_frames(event, event_time, cast_options)
        for frame_time, code, text in event_frames:
            frame_time = max(previous_time, frame_time)
            frames.append([round(frame_time, 6), code, text])
            previous_time = frame_time
        previous_time = max(previous_time, event_end)

    hold_time = previous_time + cast_options.tail_hold_ms / 1_000
    frames.append([round(hold_time, 6), "o", ""])

    header = {
        "version": CAST_VERSION,
        "width": cast_options.columns,
        "height": cast_options.rows,
        "timestamp": timestamp if timestamp is not None else _source_timestamp(trace),
        "duration": round(hold_time, 6),
        "idle_time_limit": None,
        "command": "workspace-bench trace replay",
        "title": _cast_title(trace),
        "env": {
            "SHELL": "/bin/bash",
            "TERM": "xterm-256color",
        },
        "workspace_bench": {
            "contract": trace.contract,
            "source_sha256": trace.source.source_sha256,
            "case_id": trace.source.case_id,
        },
    }

    return "\n".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for item in [header, *frames]
    ) + "\n"


def _event_frames(
    event: ReplayEvent,
    event_time: float,
    options: CastOptions,
) -> tuple[list[tuple[float, str, str]], float]:
    if event.kind == "user":
        content = _payload_text(event.content, options.payload_limit)
        if not content:
            content = "(no visible content recorded)"
        input_frames, cursor = _type_frames(
            content,
            start_time=event_time,
            cps=options.user_cps,
            max_animated_chars=options.max_animated_chars,
            emit_input=True,
            echo_output=False,
            human_jitter=True,
        )
        input_frames.append((cursor, "i", "\r"))
        cursor += 0.46
        input_frames.append(
            (
                cursor,
                "o",
                f"{_event_heading(event)}{content}\r\n\r\n",
            )
        )
        return input_frames, cursor + 0.34

    frames: list[tuple[float, str, str]] = [
        (event_time, "o", _event_heading(event)),
    ]
    cursor = event_time + 0.04

    if event.kind == "tool":
        command = _tool_command(event, options.payload_limit)
        frames.append(
            (
                cursor,
                "o",
                f"{ANSI_GREEN}workspace@bench{ANSI_RESET}:"
                f"{ANSI_BLUE}~{ANSI_RESET}$ ",
            )
        )
        cursor += 0.02
        command_frames, cursor = _type_frames(
            command,
            start_time=cursor,
            cps=options.command_cps,
            max_animated_chars=options.max_animated_chars,
        )
        frames.extend(command_frames)
        frames.append((cursor, "o", "\r\n"))

        input_details = _tool_input_details(event, options.payload_limit)
        if input_details:
            cursor += 0.06
            frames.append(
                (
                    cursor,
                    "o",
                    f"{ANSI_DIM}{ANSI_CYAN}[input]{ANSI_RESET}\r\n",
                )
            )
            input_frames, cursor = _type_frames(
                input_details,
                start_time=cursor + 0.02,
                cps=options.tool_input_cps,
                max_animated_chars=options.max_animated_chars,
            )
            frames.extend(input_frames)
            frames.append((cursor, "o", "\r\n"))

        output = _payload_text(event.output, options.payload_limit)
        if output:
            cursor += 0.12
            frames.append(
                (
                    cursor,
                    "o",
                    f"{ANSI_DIM}{ANSI_GREEN}[output]{ANSI_RESET}\r\n",
                )
            )
            output_frames, cursor = _type_frames(
                output,
                start_time=cursor + 0.02,
                cps=options.tool_output_cps,
                max_animated_chars=options.max_animated_chars,
            )
            frames.extend(output_frames)
            frames.append((cursor, "o", "\r\n"))

        if event.status == "failed" or event.diagnostics:
            cursor += 0.08
            frames.append((cursor, "o", _tool_status(event)))
        else:
            frames.append((cursor, "o", "\r\n"))
        return frames, cursor

    content = _payload_text(event.content, options.payload_limit)
    if not content:
        content = (
            "(tool-only turn; no assistant text recorded)"
            if event.kind == "assistant"
            else "(no visible content recorded)"
        )
    text_frames, cursor = _type_frames(
        content,
        start_time=cursor,
        cps={
            "user": options.user_cps,
            "assistant": options.assistant_cps,
        }.get(event.kind, options.text_cps),
        max_animated_chars=options.max_animated_chars,
    )
    frames.extend(text_frames)
    frames.append((cursor, "o", "\r\n"))
    return frames, cursor


def _type_frames(
    text: str,
    *,
    start_time: float,
    cps: int,
    max_animated_chars: int,
    emit_input: bool = False,
    echo_output: bool = True,
    human_jitter: bool = False,
) -> tuple[list[tuple[float, str, str]], float]:
    if not text:
        return [], start_time

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    animated = normalized[:max_animated_chars]
    remainder = normalized[max_animated_chars:]
    frames: list[tuple[float, str, str]] = []
    cursor = start_time
    delay = 1 / cps

    for index, char in enumerate(animated):
        output = "\r\n" if char == "\n" else char
        if emit_input:
            frames.append((cursor, "i", char))
        if echo_output:
            frames.append((cursor, "o", output))
        if human_jitter:
            jitter = (0.72, 1.18, 0.93, 1.31, 0.84, 1.08)[index % 6]
            cursor += max(0.058 if char == " " else 0, delay * jitter)
        else:
            cursor += delay

    if remainder:
        output = remainder.replace("\n", "\r\n")
        if emit_input:
            frames.append((cursor, "i", remainder))
        if echo_output:
            frames.append((cursor, "o", output))

    return frames, cursor


def _event_heading(event: ReplayEvent) -> str:
    color = {
        "system": ANSI_DIM + ANSI_WHITE,
        "user": ANSI_CYAN,
        "assistant": ANSI_BLUE,
        "tool": ANSI_GREEN,
        "error": ANSI_RED,
        "unknown": ANSI_MAGENTA,
    }.get(event.kind, ANSI_WHITE)
    if event.kind == "tool":
        left = f" {ANSI_DIM}● {sanitize_text(event.label)}{ANSI_RESET}"
        right = _tool_meta(event)
        return _aligned_line(left, right)

    label = {
        "system": "SYSTEM",
        "user": "YOU",
        "assistant": "AGENT",
        "error": "ERROR",
        "unknown": "UNKNOWN",
    }.get(event.kind, event.kind.upper())
    right = _text_meta(event)
    return _aligned_line(
        f" {color}{ANSI_BOLD}{label}{ANSI_RESET}",
        right,
    )


def _tool_input_details(event: ReplayEvent, payload_limit: int) -> str:
    raw_input = event.input
    if not isinstance(raw_input, dict):
        return _payload_text(raw_input, payload_limit)
    if any(
        isinstance(raw_input.get(key), str) and raw_input.get(key)
        for key in ("cmd", "command", "script")
    ):
        extras = {
            key: value
            for key, value in raw_input.items()
            if key not in {"cmd", "command", "script"}
        }
        return _payload_text(extras, payload_limit) if extras else ""
    return _payload_text(raw_input, payload_limit)


def _tool_command(event: ReplayEvent, payload_limit: int) -> str:
    raw_input = event.input
    if isinstance(raw_input, dict):
        for key in ("cmd", "command", "script"):
            value = raw_input.get(key)
            if isinstance(value, str) and value.strip():
                command, _, _ = truncate_utf8(sanitize_text(value.strip()), payload_limit)
                return command
        path = raw_input.get("path")
        if event.label in {"read_file", "view_file"} and isinstance(path, str):
            return f"cat {_shell_quote(sanitize_text(path))}"
        if event.label in {"write_file", "create_file"} and isinstance(path, str):
            return f"{event.label} {_shell_quote(sanitize_text(path))}"

    payload = _payload_text(raw_input, payload_limit)
    if payload:
        payload = " ".join(payload.split())
        return f"{sanitize_text(event.label)} {payload}"
    return sanitize_text(event.label)


def _tool_status(event: ReplayEvent) -> str:
    failed = (
        event.status == "failed"
        or event.kind == "error"
        or (event.exit_code is not None and event.exit_code != 0)
    )
    color = ANSI_RED if failed else ANSI_GREEN
    status = event.status or ("failed" if failed else "completed")
    summary = [sanitize_text(status)]
    if event.exit_code is not None:
        summary.append(f"exit {event.exit_code}")
    if event.duration_ms is not None:
        summary.append(f"{event.duration_ms / 1_000:.2f}s")
    if event.diagnostics:
        summary.append("warning")
    icon = "✗" if failed else "✓"
    return f" {color}{icon} {' · '.join(summary)}{ANSI_RESET}\r\n\r\n"


def _payload_text(value: Any, payload_limit: int) -> str:
    text = display_text(value)
    if text is None:
        return ""
    text, _, _ = truncate_utf8(text, payload_limit)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _marker_label(event: ReplayEvent) -> str:
    if event.kind == "tool":
        return f"tool: {sanitize_text(event.label)}"
    return event.kind


def _cast_title(trace: ReplayTrace) -> str:
    return sanitize_text(
        f"{trace.source.task_name or trace.source.case_id} — "
        f"{trace.source.agent_name or 'agent'} / {trace.source.model or 'model'}"
    )


def _source_timestamp(trace: ReplayTrace) -> int:
    start = next(
        (event.start_ms for event in trace.events if event.start_ms is not None),
        None,
    )
    if start is not None:
        return max(0, start // 1_000)
    return int(datetime.now(timezone.utc).timestamp())


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _tool_meta(event: ReplayEvent) -> str:
    parts: list[str] = []
    if event.duration_ms is not None:
        parts.append(f"{event.duration_ms / 1_000:.1f}s")
    if event.exit_code is not None:
        parts.append(f"exit {event.exit_code}")
    if event.turn is not None:
        parts.append(f"turn {event.turn}")
    return " · ".join(parts)


def _text_meta(event: ReplayEvent) -> str:
    parts: list[str] = []
    if event.turn is not None:
        parts.append(f"turn {event.turn}")
    llm = event.raw.get("llm")
    if isinstance(llm, dict):
        model = llm.get("model")
        if isinstance(model, str) and model:
            parts.append(sanitize_text(model))
    if event.usage and event.usage.total_tokens is not None:
        parts.append(f"{event.usage.total_tokens} tok")
    return " · ".join(parts)


def _aligned_line(left: str, right: str) -> str:
    columns = 70
    right_margin = 1
    gap = max(
        1,
        columns
        - _display_width(_strip_ansi(left))
        - _display_width(right)
        - right_margin,
    )
    right_text = f"{ANSI_DIM}{right}{ANSI_RESET}" if right else ""
    return f"{left}{' ' * gap}{right_text}{' ' * right_margin}\r\n"


def _display_width(value: str) -> int:
    width = 0
    for char in value:
        codepoint = ord(char)
        if (
            0x2E80 <= codepoint <= 0x9FFF
            or 0x3000 <= codepoint <= 0x303F
            or 0xFF00 <= codepoint <= 0xFF60
            or 0x2600 <= codepoint <= 0x26FF
        ):
            width += 2
        else:
            width += 1
    return width


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)
