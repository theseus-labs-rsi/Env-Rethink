from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    EMPTY_FILE = "EMPTY_FILE"
    MISSING_ASSET = "MISSING_ASSET"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    WRONG_VIEWER_TYPE = "WRONG_VIEWER_TYPE"
    ACCESS_DENIED = "ACCESS_DENIED"
    PARSE_FAILED = "PARSE_FAILED"
    INVALID_CURSOR = "INVALID_CURSOR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(slots=True)
class WorkspaceEnvError(Exception):
    code: ErrorCode
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


class AuditFailure(RuntimeError):
    """Raised when an observation cannot be durably audited."""


def success_response(
    *,
    request_id: str,
    path: str,
    view: str,
    content_type: str,
    content: Any,
    original_tokens: int,
    returned_tokens: int,
    truncated: bool,
    next_cursor: str | None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "request_id": request_id,
        "source": {"path": path, "view": view},
        "content_type": content_type,
        "content": content,
        "original_tokens": original_tokens,
        "returned_tokens": returned_tokens,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "warnings": list(warnings or []),
    }


def error_response(*, request_id: str, error: WorkspaceEnvError) -> dict[str, Any]:
    return {
        "ok": False,
        "request_id": request_id,
        "error": {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable,
        },
        "warnings": [],
    }
