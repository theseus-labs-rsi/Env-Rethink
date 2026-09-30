"""Shared filesystem/JSON helpers for the local-noise scripts.

Standard-library only. ``integration.py``, ``multi_agent.py``,
``workspace_subset.py`` and ``run_noise_batch.py`` all import from here so the
path-safety and hashing semantics live in exactly one place instead of being
re-implemented in each file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any


class IntegrationError(ValueError):
    """Raised when an input or generated task violates the integration schema."""


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_rel_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrationError(f"{field} must be a non-empty relative path")
    if "\\" in value:
        raise IntegrationError(f"{field} must use '/' separators: {value!r}")
    if value.startswith("/"):
        raise IntegrationError(f"{field} must be relative: {value!r}")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise IntegrationError(f"{field} is unsafe: {value!r}")
    normalized = path.as_posix()
    if normalized != value:
        raise IntegrationError(
            f"{field} must already be normalized: {value!r} != {normalized!r}"
        )
    return normalized


def resolve_inside(root: Path, relative: str, *, field: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise IntegrationError(f"{field} escapes {root}: {relative!r}") from exc
    return candidate


def safe_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise IntegrationError(f"{field} is not a safe identifier: {value!r}")
    return value


def require_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise IntegrationError(f"{field} must be a lowercase SHA-256 digest")
    return value
