from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from .model import DisplayProjection, ReplayEvent


DEFAULT_PAYLOAD_LIMIT = 200 * 1024
DEFAULT_SUMMARY_WIDTH = 120

_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1B
    (?:
        \][^\x07\x1B]*(?:\x07|\x1B\\)
        |
        P[^\x1B]*(?:\x1B\\)
        |
        [\[\]()#;?]*
        (?:
            [0-9]{1,4}(?:;[0-9]{0,4})*
        )?
        [0-9A-PR-TZcf-nq-uy=><~]
    )
    """,
    re.VERBOSE,
)


def sanitize_text(value: str) -> str:
    value = _ANSI_ESCAPE_RE.sub("", value)
    output: list[str] = []
    for char in value:
        if char in {"\n", "\t"}:
            output.append(char)
            continue
        codepoint = ord(char)
        if codepoint == 0x7F or codepoint < 0x20:
            output.append(f"\\x{codepoint:02x}")
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf"}:
            if codepoint <= 0xFFFF:
                output.append(f"\\u{codepoint:04x}")
            else:
                output.append(f"\\U{codepoint:08x}")
            continue
        output.append(char)
    return "".join(output)


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )


def display_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return sanitize_text(value)
    return sanitize_text(stable_json(value))


def truncate_utf8(text: str, limit: int) -> tuple[str, bool, int]:
    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= limit:
        return text, False, original_bytes
    if limit <= 0:
        return "… [truncated]", True, original_bytes

    suffix = "\n… [truncated]"
    suffix_bytes = suffix.encode("utf-8")
    head_limit = max(0, limit - len(suffix_bytes))
    head = encoded[:head_limit].decode("utf-8", errors="ignore")
    return head + suffix, True, original_bytes


def summarize(text: str, *, width: int = DEFAULT_SUMMARY_WIDTH) -> str:
    single_line = " ".join(text.split())
    if len(single_line) <= width:
        return single_line
    return single_line[: max(0, width - 1)].rstrip() + "…"


def project_event(
    event: ReplayEvent,
    *,
    payload_limit: int = DEFAULT_PAYLOAD_LIMIT,
    summary_width: int = DEFAULT_SUMMARY_WIDTH,
) -> DisplayProjection:
    title = event.label
    if event.kind == "tool":
        summary_source = display_text(event.input)
        if not summary_source:
            summary_source = display_text(event.output)
        summary = summarize(summary_source or title, width=summary_width)
    else:
        summary = summarize(sanitize_text(event.content or title), width=summary_width)

    input_text = display_text(event.input)
    output_text = display_text(event.output)
    raw_json = sanitize_text(stable_json(event.raw))

    original_bytes = sum(
        len(text.encode("utf-8"))
        for text in (input_text, output_text, raw_json)
        if text is not None
    )
    truncated = False
    if input_text is not None:
        input_text, was_truncated, _ = truncate_utf8(input_text, payload_limit)
        truncated = truncated or was_truncated
    if output_text is not None:
        output_text, was_truncated, _ = truncate_utf8(output_text, payload_limit)
        truncated = truncated or was_truncated
    raw_json, was_truncated, _ = truncate_utf8(raw_json, payload_limit)
    truncated = truncated or was_truncated

    return DisplayProjection(
        title=title,
        summary=summary,
        input_text=input_text,
        output_text=output_text,
        raw_json=raw_json,
        truncated=truncated,
        original_bytes=original_bytes,
    )
