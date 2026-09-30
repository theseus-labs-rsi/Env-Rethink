from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .cast import CastOptions, validate_cast_options


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "trace_viz.yaml"

_CAST_KEYS = {
    "columns",
    "rows",
    "tail_hold_ms",
}
_TYPING_KEYS = {
    "command_cps",
    "user_cps",
    "assistant_cps",
    "text_cps",
    "tool_input_cps",
    "tool_output_cps",
    "max_animated_chars",
    "inter_event_ms",
}


def load_cast_options(config_path: str | Path | None = None) -> CastOptions:
    path = resolve_config_path(config_path)
    if path is None:
        return CastOptions()

    root = _load_simple_yaml(path)
    unknown_sections = set(root) - {"cast", "typing"}
    if unknown_sections:
        raise ValueError(
            f"unknown trace_viz config section(s): {', '.join(sorted(unknown_sections))}"
        )

    cast = _mapping(root.get("cast"), section="cast")
    typing = _mapping(root.get("typing"), section="typing")
    _reject_unknown_keys(cast, _CAST_KEYS, section="cast")
    _reject_unknown_keys(typing, _TYPING_KEYS, section="typing")

    values: dict[str, Any] = {}
    for key in _CAST_KEYS:
        if key in cast:
            values[key] = _integer(cast[key], field=f"cast.{key}")
    for key in _TYPING_KEYS:
        if key in typing:
            values[key] = _integer(typing[key], field=f"typing.{key}")

    options = replace(CastOptions(), **values)
    validate_cast_options(options)
    return options


def resolve_config_path(config_path: str | Path | None = None) -> Path | None:
    candidate = config_path
    if candidate is None:
        candidate = os.environ.get("TRACE_VIZ_CONFIG")
    if candidate is None:
        return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else None

    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"trace_viz config not found: {path}")
    return path


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ValueError(f"{path}:{line_number}: tabs are not allowed")

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        content = raw_line.strip()
        if ":" not in content:
            raise ValueError(f"{path}:{line_number}: expected 'key: value'")
        key, value_text = content.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise ValueError(f"{path}:{line_number}: invalid key {key!r}")

        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        value_text = value_text.strip()
        if not value_text:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value_text)

    return root


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?[0-9]+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)", value):
        return float(value)
    if value.startswith(("\"", "'")):
        if value.startswith("\""):
            return json.loads(value)
        if value.endswith("'"):
            return value[1:-1].replace("''", "'")
    return value


def _mapping(value: Any, *, section: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"trace_viz config section {section!r} must be a mapping")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any],
    allowed: set[str],
    *,
    section: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"unknown trace_viz config key(s) in {section}: "
            f"{', '.join(sorted(unknown))}"
        )


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"trace_viz config {field} must be an integer")
    return value
