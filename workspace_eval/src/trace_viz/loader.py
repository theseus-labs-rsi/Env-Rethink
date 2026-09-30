from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class TraceVizInputError(ValueError):
    """Raised when a replay source cannot satisfy the required input schema."""


@dataclass(frozen=True)
class LoadedAgent:
    path: Path
    case_dir: Path
    raw_bytes: bytes
    document: Mapping[str, Any]
    execution_trace: tuple[Any, ...]
    source_sha256: str


def resolve_agent_json(source: str | Path) -> Path:
    path = Path(source).expanduser()
    if path.is_dir():
        path = path / "agent.json"
    if not path.exists():
        raise TraceVizInputError(f"agent.json not found: {path}")
    if not path.is_file():
        raise TraceVizInputError(f"replay source is not a file: {path}")
    return path.resolve()


def load_agent(source: str | Path) -> LoadedAgent:
    path = resolve_agent_json(source)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise TraceVizInputError(f"failed to read {path}: {exc}") from exc

    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise TraceVizInputError(f"agent.json is not valid UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TraceVizInputError(
            f"agent.json is not valid JSON at line {exc.lineno}, column {exc.colno}: {path}"
        ) from exc

    if not isinstance(document, dict):
        raise TraceVizInputError("agent.json root must be an object")

    trace = document.get("trace")
    if not isinstance(trace, dict):
        raise TraceVizInputError("agent.json.trace must be an object")

    execution_trace = trace.get("executionTrace")
    if not isinstance(execution_trace, list):
        raise TraceVizInputError("agent.json.trace.executionTrace must be an array")

    return LoadedAgent(
        path=path,
        case_dir=path.parent,
        raw_bytes=raw_bytes,
        document=document,
        execution_trace=tuple(execution_trace),
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
