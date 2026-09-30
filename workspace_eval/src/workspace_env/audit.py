from __future__ import annotations

import hashlib
import hmac
import os
import threading
from pathlib import Path
from typing import Any

from .errors import AuditFailure
from .manifest import canonical_json


class RequestIdentity:
    def __init__(self, artifact_root: str) -> None:
        root = Path(artifact_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        secret_path = root / "run_secret.bin"
        if secret_path.exists():
            self._secret = secret_path.read_bytes()
        else:
            self._secret = os.urandom(32)
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, self._secret)
                os.fsync(fd)
            finally:
                os.close(fd)
        if len(self._secret) != 32:
            raise AuditFailure("invalid run identity secret")
        self._counter = 0
        self._lock = threading.Lock()

    @property
    def secret(self) -> bytes:
        return self._secret

    def next(self, tool: str, arguments_hash: str) -> str:
        with self._lock:
            self._counter += 1
            payload = f"{self._counter}:{tool}:{arguments_hash}".encode("utf-8")
            digest = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()[:24]
            return f"req-{self._counter:08d}-{digest}"


class AuditLogger:
    def __init__(self, audit_path: str, raw_audit_path: str) -> None:
        self.audit_path = Path(audit_path)
        self.raw_audit_path = Path(raw_audit_path)
        self._lock = threading.Lock()
        for path in (self.audit_path, self.raw_audit_path):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)

    @staticmethod
    def arguments_hash(arguments: dict[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        line = (canonical_json(record) + "\n").encode("utf-8")
        try:
            fd = os.open(path, os.O_APPEND | os.O_WRONLY)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise AuditFailure("audit logging failed; observation suppressed") from exc

    def record(self, summary: dict[str, Any], *, raw_arguments: dict[str, Any]) -> None:
        raw = {
            "request_id": summary.get("request_id"),
            "tool": summary.get("tool"),
            "arguments": raw_arguments,
        }
        with self._lock:
            self._append(self.raw_audit_path, raw)
            self._append(self.audit_path, summary)
