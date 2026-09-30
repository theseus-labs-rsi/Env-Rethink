"""Native agent hooks that inject bounded path-local workspace context.

Ported from the source project's ``workspace_env/codex_event_hook.py`` and
generalized from Codex-only to the two harnesses this repository runs:

* ``codex`` — Codex's native ``PostToolUse`` hook engine, whose only path-bearing
  tool is the native ``Bash`` wrapper (``tool_input.command``);
* ``claude_code`` — Claude Code's ``PostToolUse`` hook (``~/.claude/settings.json``),
  which additionally reports a file per built-in tool (``Read`` →
  ``tool_input.file_path``, ``Grep``/``Glob`` → ``tool_input.path``).

Both harnesses feed the same payload shape on stdin and accept the same response
shape on stdout::

    {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                            "additionalContext": "..."}}

so this module is one implementation with a per-harness *input adapter* rather
than two hooks.  The adapter only decides *which workspace paths the just-run
tool touched*; everything after that (history rendering, collection-card lookup,
token caps, deduplication, audit) is identical.

Deliberately separate from the MCP sidecar: the hook reads the same
runner-staged agent-visible artifacts (``manifest_staging`` names) and emits the
same bounded plain text as ``event_search`` / ``workspace_search``, but it never
opens the runtime SQLite search index for enumeration, never reads canonical or
private logs, and can only *append* context — it cannot rewrite the tool call,
the file, or the workspace.

Two rules the adapter must keep, both learned the hard way upstream:

1. **Never derive paths from command output.**  ``rg --files .`` and ``find .``
   print the whole workspace; scoping history to their output turns one local
   exploration step into a global event-history dump.  Only paths *explicitly
   named in the command* count, and a command with no concrete path is skipped.
2. **Never inject for a path with no match.**  A miss is audited as ``skipped``.

This module pulls in ``tiktoken`` / ``pydantic`` (through ``event_search`` and
``collection_map``), and those only exist inside the sandbox *after* the sidecar
has installed its task-private dependencies.  Anything that must be importable
before that — the sandbox driver writing the hook's entry script — belongs in
``hook_launcher``, which is stdlib-only for exactly this reason.  Importing this
module from there kills the whole batch with ``ModuleNotFoundError``; it did.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import tiktoken

from .collection_map import (
    WORKSPACE_COLLECTION_SET_V3_FORMAT,
    CollectionMapError,
    WorkspaceCollectionSet,
    WorkspaceCollectionSetV3,
    bounded_collection_text,
    load_json_model,
    sha256_file,
    visible_workspace_collection_summary_text,
    visible_workspace_collection_text,
)
from .event_search import (
    EventContext,
    EventSearchError,
    VisibleEventLog,
    detail_available_paths,
    model_text_token_count,
    render_event_context,
)
from .manifest import canonical_json
from .manifest_staging import COLLECTION_SET_NAME, EVENT_LOG_NAME

#: Hook events this module answers.  ``PostToolUse`` is the one wired by default;
#: ``PreToolUse`` is accepted so a condition can move the injection earlier
#: without changing this file (the response shape is identical, and the response
#: echoes back whichever event arrived).
HOOK_EVENT_NAMES = frozenset({"PostToolUse", "PreToolUse"})
HOOK_AUDIT_SCHEMA_VERSION = 1
HOOK_DEDUPLICATION_STATE_VERSION = 1
MAX_SEEN_CONTEXT_FINGERPRINTS = 256

# History and collection navigation keep separate budgets: a large collection
# card must not consume the history budget that explains why the current file
# matters.  The rendered sections stay bounded to 2,816 tokens in total.
HOOK_EVENT_CONTEXT_TOKEN_CAP = 2_048
HOOK_COLLECTION_CONTEXT_TOKEN_CAP = 768

#: The companion index the runtime stages beside the public v3 map.
WORKSPACE_COLLECTION_INDEX_NAME = "workspace-collection-map.search.sqlite"
#: Fallback index name, as written by the map generator.
WORKSPACE_COLLECTION_MEMBER_INDEX_NAME = "workspace-collection-map.members.sqlite"

NATIVE_EXPLORATION_COMMANDS = frozenset(
    {
        "rg",
        "grep",
        "find",
        "fd",
        "fdfind",
        "ls",
        "tree",
        "cat",
        "bat",
        "sed",
        "head",
        "tail",
        "less",
        "more",
        "awk",
    }
)

#: Claude Code built-in tools that name a concrete file or narrow directory.
#: ``Grep``/``Glob`` are included for their ``path`` scoping argument; a call
#: without one is treated as unscoped and skipped, exactly like ``rg --files``.
CLAUDE_CODE_PATH_TOOLS = frozenset({"Read", "Grep", "Glob"})
CLAUDE_CODE_TOOL_INPUT_PATH_KEYS = ("file_path", "path")
NATIVE_SHELL_TOOL_NAMES = frozenset({"Bash", "PowerShell"})


def _json_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _truncate_hook_text(text: str, *, token_cap: int, hint: str) -> tuple[str, bool]:
    """Token-bound text while retaining an actionable reason for truncation."""

    encoding = tiktoken.get_encoding("cl100k_base")
    token_ids = encoding.encode(text, disallowed_special=())
    if len(token_ids) <= token_cap:
        return text, False
    marker = "\n[…上下文已截断；" + hint + "…]"
    marker_ids = encoding.encode(marker, disallowed_special=())
    if len(marker_ids) >= token_cap:
        return encoding.decode(token_ids[:token_cap]).rstrip(), True
    return encoding.decode(token_ids[: token_cap - len(marker_ids)]).rstrip() + marker, True


def _append_audit(path: str, record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        raw = canonical_json(record).encode("utf-8") + b"\n"
        with os.fdopen(descriptor, "ab", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(target, 0o600)


def _claim_context_fingerprint(state_path: str, fingerprint: str) -> bool:
    """Return whether this visible context has not already been injected.

    Every hook invocation is a separate process, so the small state file is
    locked rather than relying on process-local state.  It is private to the
    task's service area and never enters the task workspace.
    """

    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=False) as handle:
            raw = handle.read()
            try:
                state = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                state = {}
            seen = state.get("seen_context_fingerprints") if isinstance(state, dict) else None
            if not isinstance(seen, list) or not all(isinstance(item, str) for item in seen):
                seen = []
            if fingerprint in seen:
                return False
            seen.append(fingerprint)
            state = {
                "schema_version": HOOK_DEDUPLICATION_STATE_VERSION,
                "seen_context_fingerprints": seen[-MAX_SEEN_CONTEXT_FINGERPRINTS:],
            }
            handle.seek(0)
            handle.truncate()
            handle.write(canonical_json(state))
            handle.flush()
            os.fsync(handle.fileno())
        return True
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        os.chmod(target, 0o600)


# ── payload adapters ───────────────────────────────────────────────────


def _string_field(value: object, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    found = value.get(key)
    return found if isinstance(found, str) and found.strip() else None


def _command_from_input(value: object) -> str | None:
    """Codex and Claude Code both use ``command`` for their shell tool."""

    return _string_field(value, "command")


def _claude_code_path_from_input(value: object) -> str | None:
    """Claude Code names the subject of a built-in tool in ``file_path``/``path``.

    ``file_path`` comes first because ``Read``/``Write``/``Edit`` use it and a
    ``Grep`` may carry both (``path`` = scope, ``pattern`` = the search).
    """

    for key in CLAUDE_CODE_TOOL_INPUT_PATH_KEYS:
        candidate = _string_field(value, key)
        if candidate is not None:
            return candidate
    return None


# ── native shell path extraction (shared by both harnesses) ────────────


def _shell_segments(command: str) -> list[list[str]]:
    """Return simple command token segments from a harness's shell wrapper."""

    def tokenize(source: str) -> list[str]:
        lexer = shlex.shlex(source, posix=True, punctuation_chars="|&;")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)

    try:
        outer = tokenize(command)
    except ValueError:
        return []
    if outer and os.path.basename(outer[0]) in {"bash", "sh", "zsh"}:
        for index, token in enumerate(outer[:-1]):
            if token in {"-c", "-lc", "-cl"}:
                try:
                    outer = tokenize(outer[index + 1])
                except ValueError:
                    return []
                break
    segments: list[list[str]] = []
    current: list[str] = []
    for token in outer:
        if token in {"&&", "||", ";", "|"}:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _canonical_path_candidate(value: str) -> str | None:
    candidate = value.strip().strip("'\"")
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if (
        not candidate
        or candidate == "."
        or candidate == "-"
        or len(candidate) > 4096
        or "\x00" in candidate
        or "\\" in candidate
        or any(character in candidate for character in "*?[]{}|")
    ):
        return None
    parsed = PurePosixPath(candidate)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        return None
    return parsed.as_posix()


def _relative_to_workspace(
    value: str,
    *,
    workspace_root: str | None,
    cwd: str | None,
) -> str | None:
    """Map a tool-reported path onto the workspace-relative form used by the map.

    The map, the member index and the event log all speak workspace-relative
    POSIX paths (``输入材料/a.docx``), while Claude Code reports absolute paths
    (``/workspace/strict/workspace/输入材料/a.docx``).  An absolute path outside
    the workspace is not ours to explain, so it is dropped rather than guessed.
    """

    raw = value.strip()
    if not raw.startswith("/"):
        return raw
    for base in (workspace_root, cwd):
        if not base:
            continue
        try:
            relative = os.path.relpath(raw, base)
        except ValueError:  # different drive / impossible on POSIX, kept for safety
            continue
        if relative == ".." or relative.startswith(".." + os.sep):
            continue
        return relative.replace(os.sep, "/")
    return None


def _native_command_kind(command: str) -> str | None:
    for segment in _shell_segments(command):
        if not segment:
            continue
        executable = os.path.basename(segment[0])
        if executable in NATIVE_EXPLORATION_COMMANDS:
            return executable
    return None


def _non_option_tokens(tokens: list[str], *, options_with_value: frozenset[str] = frozenset()) -> list[str]:
    """Return positional tokens while conservatively skipping known option values."""

    positional: list[str] = []
    after_separator = False
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            after_separator = True
            continue
        if not after_separator and token in options_with_value:
            skip_next = True
            continue
        if not after_separator and token.startswith("-"):
            continue
        positional.append(token)
    return positional


def _sed_input_paths(tokens: list[str]) -> list[str]:
    """Return sed input files, excluding its program and program-source files."""

    inputs: list[str] = []
    program_supplied = False
    after_separator = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not after_separator and token == "--":
            after_separator = True
            index += 1
            continue
        if not after_separator and token in {"-e", "--expression", "-f", "--file"}:
            program_supplied = True
            index += 2
            continue
        if not after_separator and (token.startswith("-e") or token.startswith("-f")) and len(token) > 2:
            program_supplied = True
            index += 1
            continue
        if not after_separator and token.startswith("-"):
            index += 1
            continue
        if not program_supplied:
            # The first positional argument is sed's script, including after
            # ``--``.  It is never an input path.
            program_supplied = True
        else:
            inputs.append(token)
        index += 1
    return inputs


_AWK_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]+\])?=")


def _awk_input_paths(tokens: list[str]) -> list[str]:
    """Return awk input files, excluding program, assignments, and option values."""

    inputs: list[str] = []
    program_supplied = False
    after_separator = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not after_separator and token == "--":
            after_separator = True
            index += 1
            continue
        if not after_separator and token in {"-F", "--field-separator", "-v", "--assign"}:
            index += 2
            continue
        if not after_separator and token in {"-f", "--file", "--source"}:
            program_supplied = True
            index += 2
            continue
        if not after_separator and token.startswith(("-F", "-v")) and len(token) > 2:
            index += 1
            continue
        if not after_separator and token.startswith(("-f", "--file=", "--source=")):
            program_supplied = True
            index += 1
            continue
        if not after_separator and token.startswith(("--field-separator=", "--assign=")):
            index += 1
            continue
        if not after_separator and token.startswith("-"):
            index += 1
            continue
        if not program_supplied:
            # The first positional argument is awk's program.  A program
            # supplied through -f/--file/--source was marked above.
            program_supplied = True
        elif not _AWK_ASSIGNMENT.match(token):
            inputs.append(token)
        index += 1
    return inputs


def _explicit_paths_for_segment(segment: list[str]) -> list[str]:
    """Return only paths explicitly named in one native exploration command.

    Directory enumeration results are intentionally never treated as path
    inputs.  A command such as ``rg --files .`` can print the entire
    workspace, so deriving history scope from its output turns a local
    exploration step into an effectively global event-history lookup.
    """

    if not segment:
        return []
    command_kind = os.path.basename(segment[0])
    tokens = segment[1:]
    if command_kind == "find":
        paths: list[str] = []
        for token in tokens:
            if token == "--":
                continue
            if token.startswith("-") or token in {"!", "(", ")"}:
                break
            paths.append(token)
        return paths
    if command_kind in {"ls", "tree", "cat", "bat", "less", "more"}:
        return _non_option_tokens(tokens)
    if command_kind in {"head", "tail"}:
        return _non_option_tokens(tokens, options_with_value=frozenset({"-n", "-c", "--lines", "--bytes"}))
    if command_kind == "sed":
        return _sed_input_paths(tokens)
    if command_kind == "awk":
        return _awk_input_paths(tokens)
    if command_kind in {"rg", "grep"}:
        positional = _non_option_tokens(
            tokens,
            options_with_value=frozenset(
                {
                    "-A",
                    "-B",
                    "-C",
                    "-e",
                    "-f",
                    "-g",
                    "-m",
                    "--after-context",
                    "--before-context",
                    "--context",
                    "--encoding",
                    "--exclude",
                    "--exclude-dir",
                    "--file",
                    "--glob",
                    "--iglob",
                    "--include",
                    "--max-count",
                    "--regexp",
                    "--type",
                    "--type-not",
                }
            ),
        )
        if "--files" in tokens:
            return positional
        return positional[1:]
    if command_kind in {"fd", "fdfind"}:
        positional = _non_option_tokens(tokens, options_with_value=frozenset({"-E", "-e", "-t", "--exclude", "--extension", "--type"}))
        return positional[1:]
    return []


def _native_path_candidates(
    command: str,
    *,
    workspace_root: str | None,
    cwd: str | None,
) -> tuple[str | None, list[str]]:
    """Extract explicit, non-root workspace paths from native commands.

    A command without a concrete path is deliberately unscoped.  This keeps
    broad discovery commands (for example ``find .`` and ``rg --files``) from
    receiving arbitrary event history selected from their voluminous output.
    """

    command_kind = _native_command_kind(command)
    if command_kind is None:
        return None, []
    candidates: list[str] = []
    for segment in _shell_segments(command):
        if not segment or os.path.basename(segment[0]) not in NATIVE_EXPLORATION_COMMANDS:
            continue
        for raw_path in _explicit_paths_for_segment(segment):
            relative = _relative_to_workspace(raw_path, workspace_root=workspace_root, cwd=cwd)
            candidate = _canonical_path_candidate(relative) if relative is not None else None
            if candidate is not None and candidate not in candidates:
                candidates.append(candidate)
    return command_kind, candidates


def _adapter_candidates(
    tool_name: str,
    tool_input: object,
    *,
    workspace_root: str | None,
    cwd: str | None,
) -> tuple[str | None, list[str], str | None]:
    """Map one hook payload onto ``(trigger, candidate_paths, skip_reason)``.

    The per-harness difference lives entirely here.  A ``None`` trigger means the
    tool is not an exploration of a concrete workspace path, and the caller
    audits the returned ``skip_reason`` without injecting anything.
    """

    if tool_name in NATIVE_SHELL_TOOL_NAMES:
        command = _command_from_input(tool_input)
        if command is None:
            return None, [], "missing_shell_command"
        command_kind, candidates = _native_path_candidates(
            command, workspace_root=workspace_root, cwd=cwd
        )
        if command_kind is None:
            return None, [], "non_exploration_shell_command"
        if not candidates:
            return None, [], "broad_or_unscoped_native_exploration"
        return f"native_shell:{command_kind}", candidates, None

    if tool_name in CLAUDE_CODE_PATH_TOOLS:
        reported = _claude_code_path_from_input(tool_input)
        if reported is None:
            # ``Grep``/``Glob`` may scope by pattern only; that is an enumeration
            # of an unknown set of files, not access to a named one.
            return None, [], "tool_without_concrete_path"
        relative = _relative_to_workspace(reported, workspace_root=workspace_root, cwd=cwd)
        candidate = _canonical_path_candidate(relative) if relative is not None else None
        if candidate is None:
            return None, [], "path_outside_workspace_or_unsupported"
        return f"builtin_tool:{tool_name}", [candidate], None

    return None, [], "unmatched_tool"


# ── context rendering ──────────────────────────────────────────────────


def _history_for_paths(event_log: VisibleEventLog, paths: list[str]) -> tuple[EventContext, list[str]]:
    if not paths:
        return (
            EventContext(
                session_events=(),
                matched_events=(),
                related_events=(),
                session_truncated=False,
                matched_truncated=False,
                related_truncated=False,
            ),
            [],
        )
    return event_log.path_context(paths)


def _detail_access_hint(context: EventContext) -> str:
    """Offer detail mode only for public events that actually contain a body."""

    paths = detail_available_paths(context)
    if not paths:
        return ""
    lines = ["如需查看以下文件的历史详情，调用 event_search，path 使用对应路径且 detail=true："]
    lines.extend(f"- <文件> {path}" for path in paths)
    return "\n".join(lines)


def _collection_index_path(collection_set_path: str) -> Path | None:
    """Locate the staged member index beside the public map.

    Both names are accepted because the runtime stages ``search.sqlite`` while
    the generator writes ``members.sqlite``; a run may point the hook at either.
    """

    public_path = Path(collection_set_path)
    for name in (WORKSPACE_COLLECTION_INDEX_NAME, WORKSPACE_COLLECTION_MEMBER_INDEX_NAME):
        candidate = public_path.with_name(name)
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _matching_cards_v3(collection_set_path: str, candidate_paths: list[str]) -> list[str]:
    index_path = _collection_index_path(collection_set_path)
    if index_path is None:
        raise CollectionMapError("v3 workspace collection member index is unavailable")
    connection = sqlite3.connect(index_path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        matching: list[str] = []
        for path in candidate_paths:
            matching.extend(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT card_id FROM members "
                    "WHERE path = ? OR path LIKE ? ORDER BY card_id",
                    (path, path.rstrip("/") + "/%"),
                )
            )
    finally:
        connection.close()
    return sorted(set(matching))


def _matching_cards_v2(collection: WorkspaceCollectionSet, candidate_paths: list[str]) -> list[str]:
    matching: list[str] = []
    for card in collection.cards:
        members = {member.path for member in card.members}
        if any(
            path in members or any(member.startswith(path.rstrip("/") + "/") for member in members)
            for path in candidate_paths
        ):
            matching.append(card.card_id)
    return matching


def _collection_context(
    collection_set_path: str,
    candidate_paths: list[str],
    *,
    pool_prefix: str,
) -> tuple[str, str]:
    """Render the cards containing an accessed path, bounded to its own budget.

    Returns ``(text, public_map_sha256)``; ``text`` is empty when no card matches.
    """

    raw_format = json.loads(Path(collection_set_path).read_text(encoding="utf-8")).get("format")
    if raw_format == WORKSPACE_COLLECTION_SET_V3_FORMAT:
        loaded = load_json_model(collection_set_path, WorkspaceCollectionSetV3)
        assert isinstance(loaded, WorkspaceCollectionSetV3)
        matching = _matching_cards_v3(collection_set_path, candidate_paths)
        render = visible_workspace_collection_summary_text
        continuation_hint = "优先按已显示的 card_id 深入；具体文件用 path；没有清晰候选时再用 query"
    else:
        loaded = load_json_model(collection_set_path, WorkspaceCollectionSet)
        assert isinstance(loaded, WorkspaceCollectionSet)
        matching = _matching_cards_v2(loaded, candidate_paths)
        render = visible_workspace_collection_text
        continuation_hint = "优先按已显示的 card_id 深入；没有清晰候选时再用 query"
    if not matching:
        return "", sha256_file(collection_set_path)
    return (
        bounded_collection_text(
            render(loaded, card_ids=matching),
            token_cap=max(1, HOOK_COLLECTION_CONTEXT_TOKEN_CAP - model_text_token_count(pool_prefix)),
            continuation_hint=continuation_hint,
        ),
        sha256_file(collection_set_path),
    )


# ── hook entry point ───────────────────────────────────────────────────


def run_hook(
    payload: object,
    *,
    artifact_root: str,
    audit_log_path: str | None = None,
    deduplication_state_path: str | None = None,
    workspace_root: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return the harness hook output plus a private, compact audit record.

    Spanning policy, decided *before* reading anything from disk: a malformed
    payload, an event we do not answer, a tool that is not a concrete workspace
    access, and a missing staged artifact are all terminal and all audited.
    """

    if not isinstance(payload, dict):
        return None, {
            "schema_version": HOOK_AUDIT_SCHEMA_VERSION,
            "status": "error",
            "error_category": "invalid_hook_input",
        }
    hook_event_name = payload.get("hook_event_name")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    audit_base = {
        "schema_version": HOOK_AUDIT_SCHEMA_VERSION,
        "hook_event_name": hook_event_name if isinstance(hook_event_name, str) else None,
        "tool_name": tool_name if isinstance(tool_name, str) else None,
        "tool_use_id": payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else None,
        "tool_input": tool_input,
        "session_id": payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
    }
    if hook_event_name not in HOOK_EVENT_NAMES:
        return None, {**audit_base, "status": "skipped", "reason": "unexpected_hook_event"}
    if not isinstance(tool_name, str):
        return None, {**audit_base, "status": "error", "error_category": "missing_tool_name"}

    trigger, candidate_paths, skip_reason = _adapter_candidates(
        tool_name,
        tool_input,
        workspace_root=workspace_root,
        cwd=cwd,
    )
    if trigger is None:
        return None, {**audit_base, "status": "skipped", "reason": skip_reason}

    artifact_root_path = Path(artifact_root)
    event_log_path = artifact_root_path / EVENT_LOG_NAME
    collection_set_path = artifact_root_path / COLLECTION_SET_NAME
    scope_paths = candidate_paths
    try:
        event_log = VisibleEventLog.load(str(event_log_path) if event_log_path.is_file() else None)
    except EventSearchError:
        return None, {**audit_base, "status": "error", "error_category": "event_log_unavailable"}
    try:
        context, observed_paths = _history_for_paths(event_log, candidate_paths)
    except EventSearchError:
        # A short suffix that matches several workspace paths is ambiguous; the
        # hook must not guess which history the agent meant.
        return None, {
            **audit_base,
            "status": "error",
            "error_category": "event_log_path_ambiguous",
            "candidate_paths": candidate_paths,
        }
    event_context = render_event_context(context, target_paths=scope_paths or observed_paths)
    event_detail_hint = _detail_access_hint(context)

    collection_context = ""
    collection_sha256: str | None = None
    collection_prefix = "\n\n相关工作区集合：\n" if context.event_ids else "相关工作区集合：\n"
    if collection_set_path.is_file():
        try:
            # The hook needs path-local navigation only; it never enumerates
            # unrelated cards and never reads the private artifact surface.
            collection_context, collection_sha256 = _collection_context(
                str(collection_set_path),
                candidate_paths,
                pool_prefix=collection_prefix,
            )
        except (CollectionMapError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return None, {**audit_base, "status": "error", "error_category": "collection_map_unavailable"}

    event_context_truncated_for_budget = False
    if context.event_ids:
        if event_detail_hint:
            event_context += "\n" + event_detail_hint
        event_context, event_context_truncated_for_budget = _truncate_hook_text(
            event_context,
            token_cap=HOOK_EVENT_CONTEXT_TOKEN_CAP,
            hint=(
                "如需查看当前命中的公开历史详情，调用 event_search，path 使用对应路径且 detail=true"
                if event_detail_hint
                else "可调用 event_search 按当前文件路径、关键词或时间继续筛选历史"
            ),
        )
    additional_context = event_context if context.event_ids else ""
    if collection_context:
        additional_context += collection_prefix + collection_context

    context_fingerprint = _json_sha256(
        {
            "artifact_root": str(artifact_root_path),
            "event_log_sha256": event_log.sha256,
            "event_ids": context.event_ids,
            "collection_set_sha256": collection_sha256,
            "collection_context": collection_context,
        }
    )
    audit_context = {
        **audit_base,
        "status": "pending",
        "trigger": trigger,
        "candidate_paths": candidate_paths,
        "paths": observed_paths,
        "event_log_sha256": event_log.sha256,
        "matched_event_count": len(context.matched_events),
        "related_event_count": len(context.related_events),
        "returned_event_count": len(context.event_ids),
        "truncated": context.truncated,
        "event_ids": context.event_ids,
        "collection_set_sha256": collection_sha256,
        "collection_context_chars": len(collection_context),
        "event_context_tokens": model_text_token_count(event_context) if context.event_ids else 0,
        "collection_context_tokens": (
            model_text_token_count(collection_prefix + collection_context) if collection_context else 0
        ),
        "event_context_truncated_for_budget": event_context_truncated_for_budget,
        "event_context_token_cap": HOOK_EVENT_CONTEXT_TOKEN_CAP if context.event_ids else None,
        "collection_context_token_cap": HOOK_COLLECTION_CONTEXT_TOKEN_CAP if collection_context else None,
        "additional_context_token_cap": (
            HOOK_EVENT_CONTEXT_TOKEN_CAP + HOOK_COLLECTION_CONTEXT_TOKEN_CAP
            if context.event_ids and collection_context
            else (HOOK_EVENT_CONTEXT_TOKEN_CAP if context.event_ids else HOOK_COLLECTION_CONTEXT_TOKEN_CAP)
        ),
        "context_fingerprint": context_fingerprint,
        "additional_context": additional_context,
        "additional_context_chars": len(additional_context),
        "additional_context_tokens": model_text_token_count(additional_context),
    }
    if not context.event_ids and not collection_context:
        return None, {
            **audit_context,
            "status": "skipped",
            "reason": "no_matching_visible_context",
        }
    if deduplication_state_path is not None:
        try:
            newly_claimed = _claim_context_fingerprint(deduplication_state_path, context_fingerprint)
        except OSError:
            return None, {**audit_context, "status": "error", "error_category": "deduplication_state_unavailable"}
        if not newly_claimed:
            return None, {**audit_context, "status": "skipped", "reason": "duplicate_visible_context"}
    response = {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": additional_context,
        }
    }
    return response, {
        **audit_context,
        "status": "injected",
        "hook_output_sha256": _json_sha256(response),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Inject bounded public workspace history and path-local collection "
            "navigation after an agent explores a concrete workspace path"
        )
    )
    parser.add_argument(
        "--artifact-root",
        required=True,
        help="Task artifact root holding the staged map index and visible event log",
    )
    parser.add_argument("--audit-log", required=True)
    parser.add_argument("--deduplication-state")
    parser.add_argument(
        "--workspace-root",
        help="Workspace root used to relativize absolute tool paths (Claude Code)",
    )
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        _append_audit(
            args.audit_log,
            {
                "schema_version": HOOK_AUDIT_SCHEMA_VERSION,
                "status": "error",
                "error_category": "invalid_hook_input",
            },
        )
        return
    response, audit = run_hook(
        payload,
        artifact_root=args.artifact_root,
        deduplication_state_path=args.deduplication_state,
        workspace_root=args.workspace_root,
    )
    _append_audit(args.audit_log, audit)
    if response is not None:
        # stdout carries the JSON response and nothing else: some harnesses treat
        # any leading noise as a parse failure and drop the injection silently.
        sys.stdout.write(canonical_json(response) + "\n")


if __name__ == "__main__":
    main()
