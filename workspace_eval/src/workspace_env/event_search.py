"""Read-only, condition-blind search over an agent-visible event stream."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import tiktoken

from .event_log import EventAction, PublicEvent, validate_visible_events


MAX_EVENT_LOG_BYTES = 64 * 1024 * 1024
MAX_RETURNED_EVENTS = 25
MAX_RESPONSE_EVENT_CHARS = 48_000
MAX_SESSION_CONTEXT_EVENTS = 8
MAX_SESSION_RELATED_EVENTS = 25
MAX_EVENT_SUMMARY_CHARS = 480
MAX_EVENT_SCOPE_PATHS = 3
MAX_EVENT_SCOPE_PATH_CHARS = 240
MAX_KEYWORDS = 12
# The raw-public-event cap above protects the sidecar.  This independent cap
# protects the model context after records have been rendered as prose.
MAX_MODEL_CONTEXT_CHARS = 9_000
MAX_MATCHED_CONTEXT_CHARS = 4_000
MAX_SESSION_CONTEXT_CHARS = 1_200
MAX_RELATED_CONTEXT_CHARS = 2_600
MAX_DETAIL_MODEL_TOKENS = 4_096
MAX_DETAIL_RETURNED_EVENTS = 8
# Keep enough room for the fixed heading, truncation guidance and opaque
# continuation cursor after detail blocks have been selected.
DETAIL_RENDER_OVERHEAD_TOKEN_RESERVE = 512
TRUNCATION_ADVICE = "Results were truncated. Search again with a narrower path, keyword, action class, or time range to retrieve more relevant history."


class EventSearchError(ValueError):
    """A safe error caused by event-log input or tool arguments."""


class ActionClass(StrEnum):
    """Natural, model-facing operation categories."""

    READ = "read"
    CREATE = "create"
    MODIFY = "modify"
    WRITE = "write"
    COPY = "copy"
    MOVE = "move"
    RENAME = "rename"
    DELETE = "delete"
    DOWNLOAD = "download"
    IMPORT = "import"
    EXPORT = "export"
    EXTRACT = "extract"
    RESTORE = "restore"
    SHELL = "shell"
    SESSION = "session"


ACTION_CLASS_EVENTS: dict[ActionClass, frozenset[EventAction]] = {
    ActionClass.READ: frozenset({EventAction.FILE_OPEN, EventAction.FILE_PREVIEW, EventAction.FILE_READ}),
    ActionClass.CREATE: frozenset({EventAction.FOLDER_CREATE, EventAction.FILE_CREATE}),
    ActionClass.MODIFY: frozenset({EventAction.FILE_WRITE, EventAction.FILE_SAVE}),
    ActionClass.WRITE: frozenset({EventAction.FILE_CREATE, EventAction.FILE_WRITE, EventAction.FILE_SAVE}),
    ActionClass.COPY: frozenset({EventAction.FILE_COPY, EventAction.FILE_LINK}),
    ActionClass.MOVE: frozenset({EventAction.FOLDER_MOVE, EventAction.FILE_MOVE}),
    ActionClass.RENAME: frozenset({EventAction.FOLDER_RENAME, EventAction.FILE_RENAME}),
    ActionClass.DELETE: frozenset({EventAction.FOLDER_DELETE, EventAction.FILE_DELETE}),
    ActionClass.DOWNLOAD: frozenset({EventAction.FILE_DOWNLOAD}),
    ActionClass.IMPORT: frozenset({EventAction.FILE_IMPORT}),
    ActionClass.EXPORT: frozenset({EventAction.FILE_EXPORT}),
    ActionClass.EXTRACT: frozenset({EventAction.FILE_EXTRACT}),
    ActionClass.RESTORE: frozenset({EventAction.FILE_RESTORE}),
    ActionClass.SHELL: frozenset({EventAction.SHELL_COMMAND}),
    ActionClass.SESSION: frozenset({EventAction.SESSION_START, EventAction.SESSION_END}),
}


@lru_cache(maxsize=1)
def _model_encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def model_text_token_count(text: str) -> int:
    """Count model-facing text with the runtime's deterministic tokenizer."""

    return len(_model_encoding().encode(text, disallowed_special=()))


@dataclass(frozen=True, slots=True)
class EventContext:
    """Bounded, model-facing history selected solely from public events."""

    session_events: tuple[dict[str, Any], ...]
    matched_events: tuple[dict[str, Any], ...]
    related_events: tuple[dict[str, Any], ...]
    session_truncated: bool
    matched_truncated: bool
    related_truncated: bool
    next_match_offset: int | None = None

    @property
    def truncated(self) -> bool:
        return self.session_truncated or self.matched_truncated or self.related_truncated

    @property
    def event_ids(self) -> list[str]:
        return [
            event_id
            for event in (*self.session_events, *self.matched_events, *self.related_events)
            if isinstance((event_id := event.get("event_id")), str)
        ]


@dataclass(frozen=True, slots=True)
class EventDetailContext:
    """One bounded page of public event-body excerpts.

    Unlike :class:`EventContext`, this deliberately excludes session expansion:
    callers request a specific event path when they want the body of a related
    record.  This keeps a detail page local, predictable, and cursorable.
    """

    blocks: tuple[str, ...]
    matching_event_count: int
    next_match_offset: int | None = None

    @property
    def truncated(self) -> bool:
        return self.next_match_offset is not None


@dataclass(frozen=True, slots=True)
class VisibleEventLog:
    events: tuple[PublicEvent, ...]
    sha256: str | None
    raw_bytes: bytes | None = None

    @classmethod
    def empty(cls) -> "VisibleEventLog":
        return cls(events=(), sha256=None, raw_bytes=None)

    @classmethod
    def load(cls, path: str | None, *, expected_sha256: str | None = None) -> "VisibleEventLog":
        if path is None:
            if expected_sha256 is not None:
                raise EventSearchError("visible event-log hash requires a visible event-log path")
            return cls.empty()

        source = Path(path)
        try:
            before = source.lstat()
        except OSError as exc:
            raise EventSearchError("visible event log is not available") from exc
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise EventSearchError("visible event log must be a regular file")
        if before.st_size > MAX_EVENT_LOG_BYTES:
            raise EventSearchError("visible event log exceeds the configured size limit")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise EventSearchError("visible event log cannot be opened safely") from exc
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                raw = handle.read(MAX_EVENT_LOG_BYTES + 1)
        except OSError as exc:
            raise EventSearchError("visible event log cannot be read") from exc
        if len(raw) > MAX_EVENT_LOG_BYTES:
            raise EventSearchError("visible event log exceeds the configured size limit")
        try:
            after = source.stat()
        except OSError as exc:
            raise EventSearchError("visible event log cannot be verified") from exc
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise EventSearchError("visible event log changed while it was being read")

        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise EventSearchError("visible event log hash does not match the manifest")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise EventSearchError("visible event log must be UTF-8 JSONL") from exc

        parsed: list[PublicEvent] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                parsed.append(PublicEvent.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise EventSearchError(f"visible event log has an invalid public event at line {line_number}") from exc
        try:
            events = tuple(validate_visible_events(parsed))
        except ValueError as exc:
            raise EventSearchError("visible event log violates the public event-stream contract") from exc
        return cls(events=events, sha256=digest, raw_bytes=raw)

    def search(
        self,
        *,
        path: str | None = None,
        keywords: list[str] | None = None,
        action_classes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        matches, _ = self._matching_events(
            path=path,
            keywords=keywords,
            action_classes=action_classes,
            start_time=start_time,
            end_time=end_time,
        )
        returned, truncated = _bounded_records(matches, limit=MAX_RETURNED_EVENTS)
        return returned, truncated

    def search_context(
        self,
        *,
        path: str | None = None,
        keywords: list[str] | None = None,
        action_classes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        match_offset: int = 0,
    ) -> EventContext:
        """Return matching events plus other files accessed in their visible sessions.

        Session expansion only applies to path-scoped requests. It is based only
        on already-visible public records; missing/deleted events are never
        inferred or reconstructed.
        """

        if isinstance(match_offset, bool) or not isinstance(match_offset, int) or match_offset < 0:
            raise EventSearchError("search continuation offset is invalid")
        matches, target_path = self._matching_events(
            path=path,
            keywords=keywords,
            action_classes=action_classes,
            start_time=start_time,
            end_time=end_time,
        )
        return self._build_context(
            matches[match_offset:],
            target_paths=[target_path] if target_path is not None else [],
            match_offset=match_offset,
        )

    def search_detail_context(
        self,
        *,
        path: str | None = None,
        keywords: list[str] | None = None,
        action_classes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        match_offset: int = 0,
    ) -> EventDetailContext:
        """Return one token-bounded page of public read/write excerpts.

        Detail mode intentionally follows the exact same matching filters as
        summary mode but does not expand to neighbouring session files.  A
        history hint names each related file explicitly, so the model can make
        an auditable, path-scoped detail request for that file instead of
        receiving a broad session dump.
        """

        if isinstance(match_offset, bool) or not isinstance(match_offset, int) or match_offset < 0:
            raise EventSearchError("search continuation offset is invalid")
        matches, _ = self._matching_events(
            path=path,
            keywords=keywords,
            action_classes=action_classes,
            start_time=start_time,
            end_time=end_time,
        )
        blocks = [_render_event_detail_block(event.model_dump(mode="json")) for event in matches]
        blocks = [block for block in blocks if block is not None]
        selected, next_match_offset = _bounded_detail_blocks(blocks, match_offset=match_offset)
        return EventDetailContext(
            blocks=tuple(selected),
            matching_event_count=len(blocks),
            next_match_offset=next_match_offset,
        )

    def context_for_paths(self, paths: list[str]) -> EventContext:
        """Build one session-enriched context for one or more exploration paths."""

        context, _ = self.path_context(paths)
        return context

    def path_context(self, paths: list[str]) -> tuple[EventContext, list[str]]:
        """Build history once and report which requested paths matched public events."""

        target_paths: list[str] = []
        for path in paths:
            normalized = _resolve_path_filter(path, self.events)
            if normalized not in target_paths:
                target_paths.append(normalized)
        observed_paths: list[str] = []
        matches: list[PublicEvent] = []
        for event in self.events:
            event_paths = tuple(_event_paths(event))
            matched_targets = [
                target
                for target in target_paths
                if any(_path_matches(candidate, target) for candidate in event_paths)
            ]
            if not matched_targets:
                continue
            matches.append(event)
            for target in matched_targets:
                if target not in observed_paths:
                    observed_paths.append(target)
        matches = _model_relevant_public_events(matches)
        matches.sort(key=lambda event: (event.occurred_at, event.event_id), reverse=True)
        return self._build_context(matches, target_paths=target_paths, match_offset=0), observed_paths

    def _build_context(
        self, matches: list[PublicEvent], *, target_paths: list[str], match_offset: int
    ) -> EventContext:
        matched, matched_truncated = _bounded_records(
            matches,
            limit=MAX_RETURNED_EVENTS,
            model_char_budget=MAX_MATCHED_CONTEXT_CHARS,
        )
        source_ids = {event.get("event_id") for event in matched if isinstance(event.get("event_id"), str)}
        source_events = [event for event in matches if event.event_id in source_ids]
        session_context: list[dict[str, Any]] = []
        session_truncated = False
        related: list[dict[str, Any]] = []
        related_truncated = False
        related_candidates: list[PublicEvent] = []
        if target_paths and source_events:
            session_candidates = self._session_context_events(source_events)
            session_context, session_truncated = _bounded_session_records(session_candidates)
            related_candidates = self._session_related_events(source_events, excluded_paths=target_paths)
            related, related_truncated = _bounded_records(
                related_candidates,
                limit=MAX_SESSION_RELATED_EVENTS,
                model_char_budget=MAX_RELATED_CONTEXT_CHARS,
            )
        return EventContext(
            session_events=tuple(session_context),
            matched_events=tuple(matched),
            related_events=tuple(related),
            session_truncated=session_truncated,
            matched_truncated=matched_truncated,
            related_truncated=related_truncated,
            next_match_offset=match_offset + len(matched) if matched_truncated else None,
        )

    def _session_context_events(self, source_events: list[PublicEvent]) -> list[PublicEvent]:
        source_sessions = {event.session_id for event in source_events}
        starts = [
            event
            for event in self.events
            if event.session_id in source_sessions
            and event.action == EventAction.SESSION_START
            and _render_session_context_block(event.model_dump(mode="json")) is not None
        ]
        return sorted(starts, key=lambda event: (event.occurred_at, event.event_id), reverse=True)

    def _matching_events(
        self,
        *,
        path: str | None,
        keywords: list[str] | None,
        action_classes: list[str] | None,
        start_time: str | None,
        end_time: str | None,
    ) -> tuple[list[PublicEvent], str | None]:
        target_path = _resolve_path_filter(path, self.events) if path is not None else None
        target_keywords = _normalize_keyword_terms(keywords=keywords)
        target_actions = _normalize_action_classes(action_classes)
        start, start_is_date = _parse_time(start_time, name="start_time") if start_time is not None else (None, False)
        end, end_is_date = _parse_time(end_time, name="end_time") if end_time is not None else (None, False)
        if start is not None and end is not None:
            last_inclusive = end if not end_is_date else end - timedelta(microseconds=1)
            if start > last_inclusive:
                raise EventSearchError("start_time must not be later than end_time")
        matches = [
            event
            for event in self.events
            if _matches_event(
                event,
                path=target_path,
                keywords=target_keywords,
                actions=target_actions,
                start=start,
                end=end,
                end_is_date=end_is_date,
            )
        ]
        matches = _model_relevant_public_events(matches)
        matches.sort(key=lambda event: (event.occurred_at, event.event_id), reverse=True)
        return matches, target_path

    def _session_related_events(
        self, source_events: list[PublicEvent], *, excluded_paths: list[str]
    ) -> list[PublicEvent]:
        source_sessions = {event.session_id for event in source_events}
        by_path: dict[str, PublicEvent] = {}
        for event in self.events:
            if event.session_id not in source_sessions or event.object is None:
                continue
            event_path = event.object.path_at_event
            if any(_path_matches(event_path, target) for target in excluded_paths):
                continue
            previous = by_path.get(event_path)
            if previous is None or _event_information_score(event) > _event_information_score(previous):
                by_path[event_path] = event
        return sorted(by_path.values(), key=lambda event: (event.occurred_at, event.event_id), reverse=True)


def _bounded_records(
    events: list[PublicEvent], *, limit: int, model_char_budget: int | None = None
) -> tuple[list[dict[str, Any]], bool]:
    returned: list[dict[str, Any]] = []
    used_chars = 0
    used_model_chars = 0
    for event in events:
        if len(returned) >= limit:
            break
        record = event.model_dump(mode="json")
        record_chars = len(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if used_chars + record_chars > MAX_RESPONSE_EVENT_CHARS:
            break
        estimated_model_chars = _estimated_rendered_event_chars(record)
        if model_char_budget is not None and used_model_chars + estimated_model_chars > model_char_budget:
            break
        returned.append(record)
        used_chars += record_chars
        used_model_chars += estimated_model_chars
    return returned, len(returned) < len(events)


def _bounded_session_records(events: list[PublicEvent]) -> tuple[list[dict[str, Any]], bool]:
    returned: list[dict[str, Any]] = []
    used_chars = 0
    for event in events:
        if len(returned) >= MAX_SESSION_CONTEXT_EVENTS:
            break
        record = event.model_dump(mode="json")
        block = _render_session_context_block(record)
        if block is None:
            continue
        if used_chars + len(block) > MAX_SESSION_CONTEXT_CHARS:
            break
        returned.append(record)
        used_chars += len(block)
    return returned, len(returned) < len(events)


def _compact_text(value: object, *, limit: int = MAX_EVENT_SUMMARY_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _event_path(event: dict[str, Any]) -> str | None:
    obj = event.get("object")
    if not isinstance(obj, dict):
        return None
    path = obj.get("path_at_event")
    return path if isinstance(path, str) and path else None


def _event_note_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("observation", "purpose", "summary", "narrative", "title"):
        note = _compact_text(payload.get(key))
        if note and note not in {"content_available", "metadata_only"}:
            return note
    return ""


def _event_note(event: dict[str, Any]) -> str:
    return _event_note_from_payload(event.get("payload"))


def _public_detail_parts(event: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return only explicitly public, body-bearing fields for detail mode.

    This allowlist is deliberately narrower than a public event payload.  It
    prevents future metadata additions from becoming model-visible merely
    because a caller asks for a detail page.
    """

    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ()
    action = str(event.get("action") or "")
    if action == EventAction.FILE_READ.value:
        excerpt = payload.get("excerpt")
        return (("读取摘录", excerpt),) if isinstance(excerpt, str) and excerpt else ()
    if action == EventAction.FILE_WRITE.value:
        after_excerpt = payload.get("after_excerpt")
        if isinstance(after_excerpt, str) and after_excerpt:
            return (("写入后摘录", after_excerpt),)
        diff_excerpt = payload.get("diff_excerpt")
        if isinstance(diff_excerpt, str) and diff_excerpt:
            return (("写入差异摘录", diff_excerpt),)
    return ()


def _render_detail_locator(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    locator = payload.get("locator")
    if not isinstance(locator, dict):
        return None
    kind = locator.get("kind")
    if kind in {"line", "paragraph", "page", "slide"}:
        start, end = locator.get("start"), locator.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        labels = {"line": "行", "paragraph": "段", "page": "页", "slide": "幻灯片"}
        unit = labels[kind]
        return f"位置：第 {start} {unit}" if start == end else f"位置：第 {start}–{end} {unit}"
    if kind == "sheet_range":
        sheet, cell_range = locator.get("sheet"), locator.get("range")
        if isinstance(sheet, str) and isinstance(cell_range, str):
            return f"位置：工作表 {sheet}，{cell_range}"
    if kind == "whole_file":
        return "位置：全文"
    return None


def _truncate_model_text(text: str, *, token_cap: int, marker: str) -> str:
    """Bound a model-facing string while keeping an explanatory suffix."""

    token_ids = _model_encoding().encode(text, disallowed_special=())
    if len(token_ids) <= token_cap:
        return text
    marker_ids = _model_encoding().encode(marker, disallowed_special=())
    if len(marker_ids) >= token_cap:
        return _model_encoding().decode(token_ids[:token_cap]).rstrip()
    return _model_encoding().decode(token_ids[: token_cap - len(marker_ids)]).rstrip() + marker


def _render_event_detail_block(event: dict[str, Any]) -> str | None:
    detail_parts = _public_detail_parts(event)
    path = _event_path(event)
    if not detail_parts or path is None:
        return None
    header = f"<文件> {_compact_text(path, limit=MAX_EVENT_SCOPE_PATH_CHARS)}"
    metadata = "｜".join(
        [
            str(event.get("occurred_at") or "未知时间"),
            str(event.get("action") or "未知操作"),
        ]
    )
    lines = [header, metadata]
    locator = _render_detail_locator(event.get("payload"))
    if locator is not None:
        lines.append(locator)
    for label, body in detail_parts:
        lines.append(f"{label}：\n{body}")
    return "\n".join(lines)


def _bounded_detail_blocks(blocks: list[str], *, match_offset: int) -> tuple[list[str], int | None]:
    """Select a deterministic detail page under both event and token limits."""

    token_budget = max(1, MAX_DETAIL_MODEL_TOKENS - DETAIL_RENDER_OVERHEAD_TOKEN_RESERVE)
    selected: list[str] = []
    used_tokens = 0
    for block in blocks[match_offset:]:
        if len(selected) >= MAX_DETAIL_RETURNED_EVENTS:
            break
        block_tokens = model_text_token_count(block)
        if selected and used_tokens + block_tokens > token_budget:
            break
        if not selected and block_tokens > token_budget:
            block = _truncate_model_text(
                block,
                token_cap=token_budget,
                marker="\n[…该条公开历史详情已截断；请用关键词或时间缩小检索范围…]",
            )
            block_tokens = model_text_token_count(block)
        selected.append(block)
        used_tokens += block_tokens
    next_offset = match_offset + len(selected)
    return selected, next_offset if next_offset < len(blocks) else None


def detail_available_paths(context: EventContext) -> list[str]:
    """Return a bounded, public path list for an actionable hook hint."""

    paths: list[str] = []
    for event in (*context.matched_events, *context.related_events):
        path = _event_path(event)
        if path is None or path in paths or not _public_detail_parts(event):
            continue
        paths.append(path)
        if len(paths) >= MAX_EVENT_SCOPE_PATHS:
            break
    return paths


def _model_relevant_events(events: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """Skip open/preview bookkeeping when a session has substantive records."""

    informative = tuple(
        event
        for event in events
        if _event_note(event) or str(event.get("action") or "") not in {"file.open", "file.preview"}
    )
    return informative or events


def _model_relevant_public_events(events: list[PublicEvent]) -> list[PublicEvent]:
    """Apply the same bookkeeping suppression before pagination and cursors."""

    informative = [
        event
        for event in events
        if _event_note_from_payload(event.payload) or event.action not in {EventAction.FILE_OPEN, EventAction.FILE_PREVIEW}
    ]
    return informative or events


def _estimated_rendered_event_chars(event: dict[str, Any]) -> int:
    """Conservative per-line estimate used before prose is sent to the model."""

    return (
        8
        + len(str(event.get("occurred_at") or "未知时间"))
        + len(str(event.get("action") or "未知操作"))
        + len(_compact_text(_event_path(event) or "", limit=MAX_EVENT_SCOPE_PATH_CHARS))
        + len(_event_note(event))
    )


def _event_information_score(event: PublicEvent) -> tuple[int, datetime, str]:
    return (
        1 if _event_note_from_payload(event.payload) else 0,
        event.occurred_at,
        event.event_id,
    )


def _scope_kind(path: str, event_paths: set[str]) -> str:
    if path in event_paths or any(event_path.endswith("/" + path) for event_path in event_paths):
        return "文件"
    if any(event_path.startswith(path.rstrip("/") + "/") for event_path in event_paths):
        return "文件夹"
    return "文件/文件夹"


def _render_scope(target_paths: list[str], events: tuple[dict[str, Any], ...]) -> list[str]:
    event_paths = {path for event in events if (path := _event_path(event)) is not None}
    rendered: list[str] = []
    seen: set[str] = set()
    for path in target_paths:
        if len(rendered) >= MAX_EVENT_SCOPE_PATHS:
            break
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        rendered.append(f"<{_scope_kind(path, event_paths)}> {_compact_text(path, limit=MAX_EVENT_SCOPE_PATH_CHARS)}")
    return rendered


def _render_session_context_block(event: dict[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    title = _compact_text(payload.get("title"), limit=160)
    narrative = _compact_text(payload.get("narrative"))
    if not title and not narrative:
        return None
    lines = [f"<历史会话> {title}" if title else "<历史会话>"]
    if narrative:
        lines.append(f"目的：{narrative}")
    return "\n".join(lines)


def _render_event_line(event: dict[str, Any], *, include_path: bool, related: bool = False) -> str:
    fields = [str(event.get("occurred_at") or "未知时间"), str(event.get("action") or "未知操作")]
    path = _event_path(event)
    if include_path and path:
        fields.append(_compact_text(path, limit=MAX_EVENT_SCOPE_PATH_CHARS))
    note = _event_note(event)
    if note:
        fields.append(note)
    prefix = "- "
    if related and path:
        action = str(event.get("action") or "")
        kind = "文件夹" if action.startswith("folder.") else "文件"
        prefix += f"<{kind}> {_compact_text(path, limit=MAX_EVENT_SCOPE_PATH_CHARS)}｜"
        if include_path:
            fields.pop(2)
    return prefix + "｜".join(fields)


def render_event_context(
    context: EventContext, *, target_paths: list[str], next_cursor: str | None = None
) -> str:
    """Render the public event context as bounded model-facing plain text.

    Opaque IDs, provenance, raw excerpts, session/workspace IDs and other MCP
    bookkeeping intentionally remain outside this rendering path.
    """

    lines = _render_scope(target_paths, (*context.matched_events, *context.related_events))

    def append(line: str) -> bool:
        current = "\n".join(lines)
        if len(current) + (1 if current else 0) + len(line) > MAX_MODEL_CONTEXT_CHARS:
            return False
        lines.append(line)
        return True

    displayed_matches = _model_relevant_events(context.matched_events)
    session_blocks = [
        block
        for event in context.session_events
        if (block := _render_session_context_block(event)) is not None
    ]
    if session_blocks and append("相关公开会话："):
        for block in session_blocks:
            if not append(block):
                break
    if displayed_matches:
        direct_paths = {path for event in displayed_matches if (path := _event_path(event)) is not None}
        append("匹配到的公开历史（最新优先）：")
        for event in displayed_matches:
            if not append(_render_event_line(event, include_path=len(direct_paths) > 1)):
                break
    else:
        append("匹配到的公开历史：未找到记录。")
    if context.related_events:
        if append("同一会话中访问的关联文件（最新优先）："):
            for event in context.related_events:
                if not append(_render_event_line(event, include_path=False, related=True)):
                    break
    text = "\n".join(lines)
    if context.truncated:
        suffix = "\n提示：" + TRUNCATION_ADVICE
        if next_cursor is not None:
            call = json.dumps({"cursor": next_cursor}, ensure_ascii=False, separators=(",", ":"))
            suffix += f"\n继续查询：调用 event_search({call}) 获取下一页匹配历史。"
        if len(text) + len(suffix) > MAX_MODEL_CONTEXT_CHARS:
            text = text[: MAX_MODEL_CONTEXT_CHARS - len(suffix)].rstrip()
        text += suffix
    return text


def render_event_detail_context(context: EventDetailContext, *, next_cursor: str | None = None) -> str:
    """Render a token-bounded, public-only event detail page.

    Detail blocks have already been selected under a fixed budget.  The final
    guard keeps cursor and truncation prose inside that same model-facing cap.
    """

    lines = ["匹配到的公开历史详情（仅公开读取/写入摘录）："]
    if context.blocks:
        lines.extend(context.blocks)
    else:
        lines.append("未找到带公开正文摘录的匹配记录。")
    text = "\n\n".join(lines)
    if context.truncated:
        suffix = "\n提示：" + TRUNCATION_ADVICE
        if next_cursor is not None:
            call = json.dumps({"cursor": next_cursor}, ensure_ascii=False, separators=(",", ":"))
            suffix += f"\n继续查询：调用 event_search({call}) 获取下一页历史详情。"
        if model_text_token_count(text + suffix) > MAX_DETAIL_MODEL_TOKENS:
            text = _truncate_model_text(text, token_cap=MAX_DETAIL_MODEL_TOKENS - model_text_token_count(suffix), marker="")
        text += suffix
    return _truncate_model_text(text, token_cap=MAX_DETAIL_MODEL_TOKENS, marker="\n[…历史详情已截断…]")


def _normalize_path_filter(value: str) -> str:
    if not isinstance(value, str):
        raise EventSearchError("path must be a relative POSIX path")
    candidate = value.strip()
    if not candidate or "\x00" in candidate or "\\" in candidate:
        raise EventSearchError("path must be a non-empty relative POSIX path")
    parsed = PurePosixPath(candidate)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise EventSearchError("path must stay within the workspace")
    return parsed.as_posix()


def _resolve_path_filter(value: str, events: Iterable[PublicEvent]) -> str:
    """Allow an exact path, a directory prefix, or one unambiguous suffix."""

    target = _normalize_path_filter(value)
    known_paths = {candidate for event in events for candidate in _event_paths(event)}
    if target in known_paths or any(candidate.startswith(target + "/") for candidate in known_paths):
        return target
    suffix_matches = {candidate for candidate in known_paths if candidate.endswith("/" + target)}
    if len(suffix_matches) > 1:
        raise EventSearchError("path suffix is ambiguous; use a longer workspace-relative path")
    return target


def _normalize_keyword(value: str, *, field: str = "keyword") -> str:
    if not isinstance(value, str):
        raise EventSearchError(f"{field} must be a string")
    candidate = value.strip()
    if not candidate:
        raise EventSearchError(f"{field} must not be empty")
    if len(candidate) > 240:
        raise EventSearchError(f"{field} exceeds the maximum length")
    return candidate.casefold()


def _normalize_keyword_terms(*, keywords: list[str] | None) -> tuple[str, ...]:
    if keywords is None:
        return ()
    if not isinstance(keywords, list):
        raise EventSearchError("keywords must be an array of strings")
    if not keywords:
        raise EventSearchError("keywords must not be empty")
    if len(keywords) > MAX_KEYWORDS:
        raise EventSearchError(f"keywords exceeds the maximum of {MAX_KEYWORDS} entries; split the request")
    normalized: list[str] = []
    for index, value in enumerate(keywords, start=1):
        term = _normalize_keyword(value, field=f"keywords[{index}]")
        if term not in normalized:
            normalized.append(term)
    return tuple(normalized)


def _normalize_action_classes(values: list[str] | None) -> frozenset[EventAction] | None:
    if values is None or not values:
        return None
    if not isinstance(values, list):
        raise EventSearchError("action_classes must be an array")
    if len(values) > len(ActionClass):
        raise EventSearchError("action_classes exceeds the maximum number of supported classes")
    normalized: set[EventAction] = set()
    for value in values:
        if not isinstance(value, str):
            raise EventSearchError("each action class must be a string")
        try:
            action_class = ActionClass(value)
        except ValueError as exc:
            choices = ", ".join(member.value for member in ActionClass)
            raise EventSearchError(f"action_classes contains an unsupported value; use one of: {choices}") from exc
        normalized.update(ACTION_CLASS_EVENTS[action_class])
    return frozenset(normalized)


def _parse_time(value: str, *, name: str) -> tuple[datetime, bool]:
    if not isinstance(value, str):
        raise EventSearchError(f"{name} must be an ISO 8601 date or timestamp")
    candidate = value.strip()
    try:
        if len(candidate) == 10:
            parsed = datetime.fromisoformat(candidate).replace(tzinfo=timezone.utc)
            return parsed + timedelta(days=1) if name == "end_time" else parsed, True
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventSearchError(f"{name} must be an ISO 8601 date or timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventSearchError(f"{name} must include a timezone when a time is specified")
    return parsed.astimezone(timezone.utc), False


def _matches_event(
    event: PublicEvent,
    *,
    path: str | None,
    keywords: tuple[str, ...],
    actions: frozenset[EventAction] | None,
    start: datetime | None,
    end: datetime | None,
    end_is_date: bool,
) -> bool:
    if actions is not None and event.action not in actions:
        return False
    if start is not None and event.occurred_at < start:
        return False
    if end is not None:
        if end_is_date and event.occurred_at >= end:
            return False
        if not end_is_date and event.occurred_at > end:
            return False
    if path is not None and not any(_path_matches(candidate, path) for candidate in _event_paths(event)):
        return False
    if keywords:
        searchable_text = _searchable_text(event).casefold()
        if not any(keyword in searchable_text for keyword in keywords):
            return False
    return True


def _event_paths(event: PublicEvent) -> Iterable[str]:
    if event.object is not None:
        yield event.object.path_at_event
    yield from _payload_paths(event.payload)


def _payload_paths(value: Any, key: str | None = None) -> Iterable[str]:
    if isinstance(value, dict):
        for nested_key, nested_value in value.items():
            yield from _payload_paths(nested_value, nested_key)
    elif isinstance(value, list):
        for nested_value in value:
            yield from _payload_paths(nested_value, key)
    elif isinstance(value, str) and key is not None and (
        key in {"path", "path_before", "path_after", "destination_directory"} or key.endswith("_path")
    ):
        yield value


def _path_matches(candidate: str, target: str) -> bool:
    return candidate == target or candidate.startswith(target + "/") or candidate.endswith("/" + target)


def _searchable_text(event: PublicEvent) -> str:
    value = {
        "action": event.action.value,
        "object": event.object.model_dump(mode="json") if event.object is not None else None,
        "payload": event.payload,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
