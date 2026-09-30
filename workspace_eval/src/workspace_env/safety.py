from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ErrorCode, WorkspaceEnvError


@dataclass(frozen=True, slots=True)
class SafeFile:
    relative_path: str
    source_path: Path
    artifact_path: Path
    sha256: str
    size_bytes: int


class SafeWorkspace:
    def __init__(self, workspace_root: str, artifact_root: str, max_input_bytes: int) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        self.artifact_root = Path(artifact_root).resolve()
        self.max_input_bytes = max_input_bytes
        self.cache_root = self.artifact_root / "input_cache"
        self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.cache_root, 0o700)

    @staticmethod
    def normalize(relative_path: str) -> str:
        if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
            raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "path must be a non-empty relative workspace path")
        if "\\" in relative_path:
            raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "path must use POSIX separators")
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "path is outside the allowed workspace")
        return pure.as_posix()

    def stage(self, relative_path: str) -> SafeFile:
        rel = self.normalize(relative_path)
        unresolved = self.workspace_root.joinpath(*PurePosixPath(rel).parts)
        try:
            source = unresolved.resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspaceEnvError(ErrorCode.NOT_FOUND, "workspace file was not found") from exc
        try:
            source.relative_to(self.workspace_root)
        except ValueError as exc:
            raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "path is outside the allowed workspace") from exc
        try:
            before = source.stat()
        except OSError as exc:
            raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "workspace file cannot be accessed") from exc
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "path does not reference a regular file")
        if before.st_size == 0:
            raise WorkspaceEnvError(ErrorCode.EMPTY_FILE, "workspace file is empty")
        if before.st_size > self.max_input_bytes:
            raise WorkspaceEnvError(ErrorCode.BUDGET_EXCEEDED, "workspace file exceeds the configured input budget")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(source, flags)
        except OSError as exc:
            raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "workspace file cannot be opened safely") from exc
        digest = hashlib.sha256()
        temp = self.cache_root / f"staging-{os.getpid()}-{threading_id()}"
        try:
            with os.fdopen(fd, "rb", closefd=True) as src, open(temp, "xb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise WorkspaceEnvError(ErrorCode.ACCESS_DENIED, "workspace file changed while it was being read")
            file_hash = digest.hexdigest()
            suffix = source.suffix.lower()
            cached = self.cache_root / f"{file_hash}{suffix}"
            if cached.exists():
                temp.unlink(missing_ok=True)
            else:
                os.replace(temp, cached)
                os.chmod(cached, 0o600)
            return SafeFile(rel, source, cached, "sha256:" + file_hash, before.st_size)
        except WorkspaceEnvError:
            temp.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise WorkspaceEnvError(ErrorCode.INTERNAL_ERROR, "failed to stage workspace file") from exc


def threading_id() -> int:
    # Kept local to avoid exposing process-specific values in response metadata.
    import threading

    return threading.get_ident()
