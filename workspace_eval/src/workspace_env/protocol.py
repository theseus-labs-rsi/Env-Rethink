from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .manifest import canonical_json


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _request_id(value: Any) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("request_id"), str):
            return value["request_id"]
        for nested in value.values():
            found = _request_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _request_id(nested)
            if found:
                return found
    elif isinstance(value, str):
        try:
            return _request_id(json.loads(value))
        except Exception:
            return None
    return None


class ProtocolAuditMiddleware:
    """Buffer one stateless HTTP exchange until its protocol audit is durable."""

    def __init__(self, app: Callable[..., Awaitable[None]], artifact_root: str) -> None:
        self.app = app
        root = Path(artifact_root)
        self.audit_path = root / "mcp_protocol.jsonl"
        self.raw_path = root / "mcp_protocol_raw.jsonl"
        self.fatal_path = root / "INVALID_AUDIT_FAILURE"
        for path in (self.audit_path, self.raw_path):
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)

    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        fd = os.open(path, os.O_APPEND | os.O_WRONLY)
        try:
            os.write(fd, (canonical_json(record) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[dict]], send: Callable[..., Awaitable[None]]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        request_messages: list[dict[str, Any]] = []
        body = bytearray()
        while True:
            message = await receive()
            request_messages.append(message)
            body.extend(message.get("body") or b"")
            if not message.get("more_body"):
                break
        request_index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal request_index
            if request_index < len(request_messages):
                message = request_messages[request_index]
                request_index += 1
                return message
            return {"type": "http.disconnect"}

        response_messages: list[dict[str, Any]] = []

        async def buffer_send(message: dict[str, Any]) -> None:
            response_messages.append(message)

        status = 500
        error: str | None = None
        try:
            await self.app(scope, replay_receive, buffer_send)
            for message in response_messages:
                if message.get("type") == "http.response.start":
                    status = int(message.get("status") or 500)
                    break
        except Exception as exc:
            error = type(exc).__name__

        try:
            request_obj = json.loads(bytes(body).decode("utf-8")) if body else {}
        except Exception:
            request_obj = {}
        requests = request_obj if isinstance(request_obj, list) else [request_obj]
        response_body = b"".join(
            message.get("body") or b"" for message in response_messages if message.get("type") == "http.response.body"
        )
        try:
            response_obj = json.loads(response_body.decode("utf-8")) if response_body else {}
        except Exception:
            response_obj = {}
        semantic_request_id = _request_id(response_obj)
        try:
            for request in requests:
                request = request if isinstance(request, dict) else {}
                params = request.get("params") if isinstance(request.get("params"), dict) else {}
                self._append(
                    self.raw_path,
                    {
                        "timestamp": _timestamp(),
                        "jsonrpc_id": request.get("id"),
                        "method": request.get("method"),
                        "params": params,
                    },
                )
                self._append(
                    self.audit_path,
                    {
                        "timestamp": _timestamp(),
                        "jsonrpc_id": request.get("id"),
                        "method": request.get("method"),
                        "arguments_hash": "sha256:" + hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest(),
                        "request_id": semantic_request_id,
                        "http_status": status,
                        "status": "ok" if error is None and status < 400 else "error",
                        "error_type": error,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                    },
                )
        except OSError:
            try:
                self.fatal_path.write_text("protocol audit logging failed\n", encoding="utf-8")
                os.chmod(self.fatal_path, 0o600)
            except OSError:
                pass
            response_messages = [
                {"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"application/json")]},
                {"type": "http.response.body", "body": b'{"error":"audit logging failed"}', "more_body": False},
            ]
        if error is not None and not response_messages:
            response_messages = [
                {"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"application/json")]},
                {"type": "http.response.body", "body": b'{"error":"internal MCP failure"}', "more_body": False},
            ]
        for message in response_messages:
            await send(message)
