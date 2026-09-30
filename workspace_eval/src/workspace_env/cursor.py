from __future__ import annotations

import base64
import hashlib
import hmac
import threading
from collections.abc import Callable
from typing import Any

from .errors import ErrorCode, WorkspaceEnvError
from .manifest import canonical_json


class CursorStore:
    def __init__(
        self,
        secret: bytes,
        scope_hash: str,
        *,
        on_discard: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._secret = secret
        self._scope_hash = scope_hash
        self._on_discard = on_discard
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def issue(self, state: dict[str, Any]) -> str:
        scoped = {"scope": self._scope_hash, "state": state}
        digest = hashlib.sha256(canonical_json(scoped).encode("utf-8")).digest()
        token_bytes = hmac.new(self._secret, digest, hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(token_bytes).decode("ascii").rstrip("=")
        with self._lock:
            self._states[token] = state
        return token

    def consume(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) != 43:
            raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "cursor is invalid or no longer available")
        with self._lock:
            state = self._states.pop(token, None)
        if state is None:
            raise WorkspaceEnvError(ErrorCode.INVALID_CURSOR, "cursor is invalid or no longer available")
        return state

    def clear(self) -> None:
        with self._lock:
            states = list(self._states.values())
            self._states.clear()
        if self._on_discard is not None:
            for state in states:
                self._on_discard(state)
