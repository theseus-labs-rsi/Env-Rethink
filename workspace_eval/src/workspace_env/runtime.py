from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .audit import AuditLogger, RequestIdentity
from .collection_map import (
    CollectionMapError,
    EmptyCollectionMapSearch,
    WorkspaceCollectionMapSearch,
    normalize_search_query,
)
from .cursor import CursorStore
from .errors import AuditFailure, ErrorCode, WorkspaceEnvError, error_response
from .event_search import (
    TRUNCATION_ADVICE,
    ActionClass,
    EventSearchError,
    VisibleEventLog,
    model_text_token_count,
    render_event_context,
    render_event_detail_context,
)
from .manifest import ResolvedManifest
from .safety import SafeFile, SafeWorkspace


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WorkspaceRuntime:
    def __init__(self, resolved: ResolvedManifest) -> None:
        self.resolved = resolved
        manifest = resolved.manifest
        self.identity = RequestIdentity(manifest.workspace.artifact_root)
        self.audit = AuditLogger(manifest.logging.audit_path, manifest.logging.raw_audit_path)
        self.event_log = VisibleEventLog.load(
            manifest.context.visible_event_log_path,
            expected_sha256=manifest.context.visible_event_log_sha256,
        )
        data = manifest.data
        if data.workspace_search_backend == "workspace_collection_map":
            assert data.workspace_collection_set_path is not None
            assert data.workspace_collection_set_sha256 is not None
            assert data.workspace_collection_search_index_path is not None
            assert data.workspace_collection_search_index_sha256 is not None
            self.workspace_search_backend = WorkspaceCollectionMapSearch(
                collection_set_path=data.workspace_collection_set_path,
                collection_set_sha256=data.workspace_collection_set_sha256,
                index_path=data.workspace_collection_search_index_path,
                index_sha256=data.workspace_collection_search_index_sha256,
            )
        else:
            self.workspace_search_backend = EmptyCollectionMapSearch()
        self.workspace = SafeWorkspace(
            manifest.workspace.input_root,
            manifest.workspace.artifact_root,
            manifest.tools.max_input_bytes,
        )
        self.cursors = CursorStore(self.identity.secret, resolved.condition_hash)
        self.fatal_marker = Path(manifest.workspace.artifact_root) / "INVALID_AUDIT_FAILURE"

    def close(self) -> None:
        """Discard unconsumed cursor state when the sidecar or run ends."""
        self.cursors.clear()

    def _identity(self, tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
        arguments_hash = self.audit.arguments_hash(arguments)
        return self.identity.next(tool, arguments_hash), arguments_hash

    def _record(
        self,
        *,
        request_id: str,
        tool: str,
        arguments_hash: str,
        raw_arguments: dict[str, Any],
        started: float,
        status: str,
        safe_file: SafeFile | None,
        result: dict[str, Any],
        cursor_used: bool,
    ) -> None:
        manifest = self.resolved.manifest
        summary = {
            "timestamp": _timestamp(),
            "run_id": manifest.run_id,
            "task_id": manifest.task_id,
            "repetition_id": manifest.repetition_id,
            "condition_hash": self.resolved.condition_hash,
            "request_id": request_id,
            "tool": tool,
            "arguments_hash": arguments_hash,
            "path": safe_file.relative_path if safe_file is not None else None,
            "input_file_hash": safe_file.sha256 if safe_file is not None else None,
            "parser_version": self.resolved.components.get("workspace_env"),
            "original_tokens": result.get("original_tokens"),
            "returned_tokens": result.get("returned_tokens"),
            "truncated": result.get("truncated"),
            "returned_event_count": result.get("returned_event_count"),
            "model_text_chars": result.get("model_text_chars"),
            "continuation_available": result.get("next_cursor") is not None,
            "cursor_used": cursor_used,
            "status": status,
            "error_code": (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        try:
            self.audit.record(summary, raw_arguments=raw_arguments)
        except AuditFailure:
            try:
                self.fatal_marker.write_text("audit logging failed\n", encoding="utf-8")
                os.chmod(self.fatal_marker, 0o600)
            except OSError:
                pass
            raise


    def _consume_event_search_cursor(self, cursor: str) -> dict[str, Any]:
        state = self.cursors.consume(cursor)
        if state.get("kind") != "event_search":
            raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "cursor does not belong to an event search")
        return state

    def _issue_event_search_cursor(self, query: dict[str, Any], match_offset: int | None) -> str | None:
        if match_offset is None:
            return None
        return self.cursors.issue({"kind": "event_search", "query": query, "match_offset": match_offset})

    @staticmethod
    def _validate_event_search_cursor_arguments(
        *,
        cursor: str | None,
        path: str | None,
        keywords: list[str] | None,
        action_classes: list[str] | None,
        start_time: str | None,
        end_time: str | None,
        detail: bool | None,
    ) -> None:
        if cursor is not None and any(
            value is not None for value in (path, keywords, action_classes, start_time, end_time, detail)
        ):
            raise WorkspaceEnvError(
                ErrorCode.INVALID_CURSOR,
                "cursor must be used alone when continuing an event search",
            )

    def event_search(
        self,
        path: str | None = None,
        keywords: list[str] | None = None,
        action_classes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        detail: bool | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        arguments = {
            "path": path,
            "keywords": keywords,
            "action_classes": action_classes,
            "start_time": start_time,
            "end_time": end_time,
            "detail": detail,
            "cursor": cursor,
        }
        request_id, arguments_hash = self._identity("event_search", arguments)
        started = time.monotonic()
        try:
            self._validate_event_search_cursor_arguments(
                cursor=cursor,
                path=path,
                keywords=keywords,
                action_classes=action_classes,
                start_time=start_time,
                end_time=end_time,
                detail=detail,
            )
            if cursor is not None:
                state = self._consume_event_search_cursor(cursor)
                state_query = state.get("query")
                if not isinstance(state_query, dict):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "event-search cursor state is invalid")
                query = dict(state_query)
                detail_mode = query.get("detail")
                if not isinstance(detail_mode, bool):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "event-search cursor state is invalid")
                match_offset = state.get("match_offset")
                if isinstance(match_offset, bool) or not isinstance(match_offset, int) or match_offset < 0:
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "event-search cursor state is invalid")
            else:
                if detail is not None and not isinstance(detail, bool):
                    raise WorkspaceEnvError(ErrorCode.INVALID_ARGUMENT, "detail must be a boolean")
                detail_mode = detail is True
                query = {
                    "path": path,
                    "keywords": keywords,
                    "action_classes": action_classes,
                    "start_time": start_time,
                    "end_time": end_time,
                    "detail": detail_mode,
                }
                match_offset = 0
            if detail_mode:
                detail_context = self.event_log.search_detail_context(
                    path=query["path"],
                    keywords=query["keywords"],
                    action_classes=query["action_classes"],
                    start_time=query["start_time"],
                    end_time=query["end_time"],
                    match_offset=match_offset,
                )
                next_cursor = self._issue_event_search_cursor(query, detail_context.next_match_offset)
                content = render_event_detail_context(detail_context, next_cursor=next_cursor)
                matched_event_count = len(detail_context.blocks)
                related_event_count = 0
                returned_event_count = len(detail_context.blocks)
                truncated = detail_context.truncated
            else:
                context = self.event_log.search_context(
                    path=query["path"],
                    keywords=query["keywords"],
                    action_classes=query["action_classes"],
                    start_time=query["start_time"],
                    end_time=query["end_time"],
                    match_offset=match_offset,
                )
                next_cursor = self._issue_event_search_cursor(query, context.next_match_offset)
                content = render_event_context(
                    context,
                    target_paths=[query["path"]] if query["path"] is not None else [],
                    next_cursor=next_cursor,
                )
                matched_event_count = len(context.matched_events)
                related_event_count = len(context.related_events)
                returned_event_count = len(context.event_ids)
                truncated = context.truncated
            response = {
                "ok": True,
                "request_id": request_id,
                "source": {"view": "event_search"},
                "content_type": "text/plain",
                "content": content,
                "detail": detail_mode,
                "matched_event_count": matched_event_count,
                "related_event_count": related_event_count,
                "returned_event_count": returned_event_count,
                "original_tokens": model_text_token_count(content),
                "returned_tokens": model_text_token_count(content),
                "model_text_chars": len(content),
                "truncated": truncated,
                "next_cursor": next_cursor,
                "warnings": [TRUNCATION_ADVICE] if truncated else [],
            }
            status = "ok"
        except WorkspaceEnvError as error:
            response = error_response(request_id=request_id, error=error)
            status = "error"
        except EventSearchError as error:
            response = error_response(
                request_id=request_id,
                error=WorkspaceEnvError(ErrorCode.INVALID_ARGUMENT, str(error)),
            )
            status = "error"
        except Exception:
            response = error_response(
                request_id=request_id,
                error=WorkspaceEnvError(ErrorCode.INTERNAL_ERROR, "internal event-search failure"),
            )
            status = "error"
        self._record(
            request_id=request_id,
            tool="event_search",
            arguments_hash=arguments_hash,
            raw_arguments=arguments,
            started=started,
            status=status,
            safe_file=None,
            result=response,
            cursor_used=cursor is not None,
        )
        return response

    def event_search_text(
        self,
        path: Annotated[
            str | None,
            Field(
                description=(
                    "Known workspace-relative file or folder: exact path, directory prefix, or unambiguous suffix. "
                    "It also includes same-session related-file summaries."
                )
            ),
        ] = None,
        keywords: Annotated[
            list[str] | None,
            Field(
                min_length=1,
                max_length=12,
                description=(
                    "Up to 12 case-insensitive literal phrases as a JSON array. They form an OR union; path, "
                    "action_classes, and time filters use AND."
                ),
            ),
        ] = None,
        action_classes: Annotated[
            list[ActionClass] | None,
            Field(
                description=(
                    "Optional operation categories, combined with other filters using AND. Use enum values such as read "
                    "or write, not internal values such as file.write."
                )
            ),
        ] = None,
        start_time: Annotated[
            str | None,
            Field(description="Optional inclusive ISO 8601 date or timezone-aware timestamp lower bound."),
        ] = None,
        end_time: Annotated[
            str | None,
            Field(description="Optional inclusive ISO 8601 date or timezone-aware timestamp upper bound."),
        ] = None,
        detail: Annotated[
            bool | None,
            Field(
                description=(
                    "Set true only when you need the public read/write excerpt behind a summary. Omit or set false "
                    "for summary-only history. Do not combine with cursor."
                )
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                description=(
                    "Opaque continuation token for a truncated result. Use it alone to retrieve the next non-overlapping page."
                )
            ),
        ] = None,
    ) -> str | CallToolResult:
        """Expose the event-search result as MCP text instead of a JSON envelope."""

        response = self.event_search(
            path=path,
            keywords=keywords,
            action_classes=[str(value) for value in action_classes] if action_classes is not None else None,
            start_time=start_time,
            end_time=end_time,
            detail=detail,
            cursor=cursor,
        )
        if response.get("ok") is True:
            content = response.get("content")
            return content if isinstance(content, str) else "匹配到的公开历史：未找到记录。"
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        code = str(error.get("code") or ErrorCode.INTERNAL_ERROR.value)
        message = str(error.get("message") or "event search failed")
        return CallToolResult(
            content=[TextContent(type="text", text=f"[{code}] {message}")],
            isError=True,
        )

    def workspace_search(
        self,
        query: str | None = None,
        path: str | None = None,
        card_id: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search reviewed workspace collections, or return the stable
        no_collection_map empty result."""

        arguments = {"query": query, "path": path, "card_id": card_id, "cursor": cursor}
        request_id, arguments_hash = self._identity("workspace_search", arguments)
        started = time.monotonic()
        try:
            fragment_card_id: str | None = None
            fragment_token_offset: int | None = None
            if cursor is not None:
                if query is not None or path is not None or card_id is not None:
                    raise WorkspaceEnvError(
                        ErrorCode.INVALID_CURSOR,
                        "query, path, and card_id must be omitted when continuing a workspace search",
                    )
                state = self.cursors.consume(cursor)
                if state.get("kind") != "workspace_search":
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "cursor does not belong to workspace search")
                stored_query = state.get("query")
                stored_path = state.get("path")
                stored_card_id = state.get("card_id")
                offset = state.get("offset")
                if stored_query is not None and not isinstance(stored_query, str):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "workspace-search cursor is invalid")
                if stored_path is not None and not isinstance(stored_path, str):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "workspace-search cursor is invalid")
                if stored_card_id is not None and not isinstance(stored_card_id, str):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "workspace-search cursor is invalid")
                if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "workspace-search cursor is invalid")
                raw_fragment_card_id = state.get("fragment_card_id")
                raw_fragment_token_offset = state.get("fragment_token_offset")
                if raw_fragment_card_id is not None and not isinstance(raw_fragment_card_id, str):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "workspace-search cursor is invalid")
                if raw_fragment_token_offset is not None and (
                    isinstance(raw_fragment_token_offset, bool)
                    or not isinstance(raw_fragment_token_offset, int)
                    or raw_fragment_token_offset < 0
                ):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "workspace-search cursor is invalid")
                if (raw_fragment_card_id is None) != (raw_fragment_token_offset is None):
                    raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "workspace-search cursor is invalid")
                fragment_card_id = raw_fragment_card_id
                fragment_token_offset = raw_fragment_token_offset
                effective_query = stored_query
                effective_path = stored_path
                effective_card_id = stored_card_id
            else:
                # Validate before dispatch so the no_collection_map empty
                # backend and the collection-map backend have indistinguishable
                # argument semantics; only their data differ.
                normalize_search_query(query)
                selected_modes = sum(value is not None for value in (query, path, card_id))
                if selected_modes > 1:
                    raise CollectionMapError("query, path, and card_id are mutually exclusive")
                effective_query = query
                effective_path = path
                effective_card_id = card_id
                offset = 0
            cap = self.resolved.manifest.tools.workspace_search_token_cap
            page_cap = cap
            next_cursor = None
            continuation = ""
            # A cursor only helps if it is present in the model-visible text.
            # Reserve its exact token cost and re-page when necessary.
            # The opaque cursor's base64 payload can vary slightly in tokenizer
            # cost as its signed state changes. Iterate to a stable bounded
            # page rather than assuming three adjustments always suffice.
            for _ in range(8):
                page = self.workspace_search_backend.search(
                    query=effective_query,
                    path=effective_path,
                    card_id=effective_card_id,
                    offset=offset,
                    token_cap=page_cap,
                    fragment_card_id=fragment_card_id,
                    fragment_token_offset=fragment_token_offset,
                )
                next_cursor = None
                continuation = ""
                if page.next_state is not None:
                    next_cursor = self.cursors.issue(
                        {
                            "kind": "workspace_search",
                            "query": effective_query,
                            "path": effective_path,
                            "card_id": effective_card_id,
                            **page.next_state,
                        }
                    )
                    continuation = f'\n\n继续阅读：调用 workspace_search，并且只传入 cursor="{next_cursor}"。'
                returned_tokens = page.returned_tokens + len(
                    self.workspace_search_backend.encoding.encode(continuation, disallowed_special=())
                )
                if returned_tokens <= cap:
                    break
                page_cap = max(1, page_cap - (returned_tokens - cap))
            else:
                raise RuntimeError("workspace-search continuation cannot fit observation budget")
            content = page.content + continuation
            response = {
                "ok": True,
                "request_id": request_id,
                "source": {"view": "workspace_search"},
                "content_type": "text/plain",
                "content": content,
                "original_tokens": page.original_tokens,
                "returned_tokens": returned_tokens,
                "model_text_chars": len(content),
                "truncated": next_cursor is not None,
                "next_cursor": next_cursor,
                "warnings": [],
            }
            status = "ok"
        except AuditFailure:
            raise
        except CollectionMapError as error:
            response = error_response(
                request_id=request_id,
                error=WorkspaceEnvError(ErrorCode.INVALID_ARGUMENT, str(error)),
            )
            status = "error"
        except WorkspaceEnvError as error:
            response = error_response(request_id=request_id, error=error)
            status = "error"
        except Exception:
            response = error_response(
                request_id=request_id,
                error=WorkspaceEnvError(ErrorCode.INTERNAL_ERROR, "internal workspace-search failure"),
            )
            status = "error"
        self._record(
            request_id=request_id,
            tool="workspace_search",
            arguments_hash=arguments_hash,
            raw_arguments=arguments,
            started=started,
            status=status,
            safe_file=None,
            result=response,
            cursor_used=cursor is not None,
        )
        return response

    def workspace_map(self) -> dict[str, Any]:
        """Return the complete card directory without paths or pagination."""

        arguments: dict[str, Any] = {}
        request_id, arguments_hash = self._identity("workspace_map", arguments)
        started = time.monotonic()
        try:
            content = self.workspace_search_backend.full_map()
            tokens = len(
                self.workspace_search_backend.encoding.encode(
                    content,
                    disallowed_special=(),
                )
            )
            response = {
                "ok": True,
                "request_id": request_id,
                "source": {"view": "workspace_map"},
                "content_type": "text/plain",
                "content": content,
                "original_tokens": tokens,
                "returned_tokens": tokens,
                "model_text_chars": len(content),
                "truncated": False,
                "next_cursor": None,
                "warnings": [],
            }
            status = "ok"
        except AuditFailure:
            raise
        except Exception:
            response = error_response(
                request_id=request_id,
                error=WorkspaceEnvError(
                    ErrorCode.INTERNAL_ERROR,
                    "internal workspace-map failure",
                ),
            )
            status = "error"
        self._record(
            request_id=request_id,
            tool="workspace_map",
            arguments_hash=arguments_hash,
            raw_arguments=arguments,
            started=started,
            status=status,
            safe_file=None,
            result=response,
            cursor_used=False,
        )
        return response

    def workspace_map_text(self) -> str | CallToolResult:
        """Expose the complete collection-card directory as MCP plain text."""

        response = self.workspace_map()
        if response.get("ok") is True:
            content = response.get("content")
            return content if isinstance(content, str) else "当前工作区未提供可展示的完整集合地图。"
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        code = str(error.get("code") or ErrorCode.INTERNAL_ERROR.value)
        message = str(error.get("message") or "workspace map failed")
        return CallToolResult(
            content=[TextContent(type="text", text=f"[{code}] {message}")],
            isError=True,
        )

    def workspace_search_text(
        self,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Fallback business or topic keywords used only when workspace_map has no clear candidate. "
                    "Whitespace-separated keywords form an OR union; results are ranked collection summaries, not "
                    "concrete member paths. If the task provides a file name/path, use path; if the map already gives "
                    "a relevant card_id, use card_id directly. Mutually exclusive with path and card_id."
                )
            ),
        ] = None,
        path: Annotated[
            str | None,
            Field(
                description=(
                    "A concrete workspace-relative file name or file path. Use path instead of query when the task "
                    "names a file. The result returns the matching collection card, its overview, each matched "
                    "canonical path, and a compact file overview. Mutually exclusive with query and card_id."
                )
            ),
        ] = None,
        card_id: Annotated[
            str | None,
            Field(
                description=(
                    "Exact card_id returned by a collection-summary result. "
                    "Use it alone to list that collection's member file paths."
                )
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                description=(
                    "Opaque cursor returned by a previous workspace_search call; "
                    "omit query, path, and card_id when using it."
                )
            ),
        ] = None,
    ) -> str | CallToolResult:
        response = self.workspace_search(query=query, path=path, card_id=card_id, cursor=cursor)
        if response.get("ok") is True:
            content = response.get("content")
            return content if isinstance(content, str) else "未找到匹配的工作区集合。"
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        code = str(error.get("code") or ErrorCode.INTERNAL_ERROR.value)
        message = str(error.get("message") or "workspace search failed")
        return CallToolResult(content=[TextContent(type="text", text=f"[{code}] {message}")], isError=True)
