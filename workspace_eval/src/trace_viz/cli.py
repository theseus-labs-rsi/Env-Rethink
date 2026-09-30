from __future__ import annotations

import argparse
from dataclasses import replace
import sys
from pathlib import Path
from typing import Sequence, TextIO

from . import (
    CastOptions,
    ScheduleOptions,
    TraceVizInputError,
    load_replay,
    load_cast_options,
    render_plain,
    replay_to_cast,
)
from .serialize import replay_to_json


def _add_core_options(
    parser: argparse.ArgumentParser,
    *,
    payload_limit_default: int | None = 200 * 1024,
) -> None:
    parser.add_argument(
        "--timing",
        choices=("recorded", "exact", "step"),
        default="recorded",
    )
    parser.add_argument("--step-ms", type=int, default=350)
    parser.add_argument("--max-gap-ms", type=int, default=2_000)
    parser.add_argument(
        "--payload-limit",
        type=int,
        default=payload_limit_default,
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--config",
        type=Path,
        help="trace replay YAML config (default: evaluation/trace_viz.yaml)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trace_viz.py",
        description="Inspect Workspace-Bench agent replay traces.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="normalize a trace and print the replay contract",
    )
    inspect_parser.add_argument("source")
    _add_core_options(inspect_parser, payload_limit_default=None)
    inspect_parser.add_argument(
        "--format",
        choices=("plain", "json", "cast"),
        default="plain",
    )
    inspect_parser.add_argument("--output", type=Path)
    inspect_parser.add_argument(
        "--generated-at",
        help=argparse.SUPPRESS,
    )
    inspect_parser.add_argument(
        "--redact-paths",
        action="store_true",
        help="remove the absolute source path from JSON output",
    )
    inspect_parser.add_argument("--columns", type=int)
    inspect_parser.add_argument("--rows", type=int)
    inspect_parser.add_argument("--tail-hold-ms", type=int)

    export_parser = subparsers.add_parser(
        "export",
        help="export an asciicast v2 recording",
    )
    export_parser.add_argument("source")
    _add_core_options(export_parser, payload_limit_default=None)
    export_parser.add_argument("--format", choices=("cast",), default="cast")
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--columns", type=int)
    export_parser.add_argument("--rows", type=int)
    export_parser.add_argument("--tail-hold-ms", type=int)
    export_parser.add_argument("--overwrite", action="store_true")
    return parser


def _write_output(text: str, output: Path | None, stdout: TextIO) -> None:
    if output is None:
        stdout.write(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def _validate_common_options(args: argparse.Namespace) -> None:
    if args.step_ms < 0:
        raise ValueError("--step-ms must be non-negative")
    if args.max_gap_ms < 0:
        raise ValueError("--max-gap-ms must be non-negative")
    if args.payload_limit is not None and args.payload_limit < 0:
        raise ValueError("--payload-limit must be non-negative")
    if args.columns is not None and args.columns < 20:
        raise ValueError("--columns must be at least 20")
    if args.rows is not None and args.rows < 5:
        raise ValueError("--rows must be at least 5")
    if args.tail_hold_ms is not None and args.tail_hold_ms < 0:
        raise ValueError("--tail-hold-ms must be non-negative")


def _load_trace_from_args(args: argparse.Namespace):
    return load_replay(
        args.source,
        ScheduleOptions(
            timing=args.timing,
            step_ms=args.step_ms,
            max_gap_ms=args.max_gap_ms,
        ),
    )


def _run_inspect(args: argparse.Namespace, *, stdout: TextIO) -> int:
    _validate_common_options(args)
    trace = _load_trace_from_args(args)
    if args.format == "json":
        text = replay_to_json(
            trace,
            generated_at=args.generated_at,
            payload_limit=args.payload_limit or 200 * 1024,
            redact_paths=args.redact_paths,
        )
    elif args.format == "cast":
        text = replay_to_cast(
            trace,
            options=_cast_options_from_args(
                args,
            ),
        )
    else:
        text = render_plain(
            trace,
            payload_limit=args.payload_limit or 200 * 1024,
        )
    _write_output(text, args.output, stdout)
    return 3 if args.strict and trace.diagnostics else 0


def _run_export(args: argparse.Namespace, *, stdout: TextIO) -> int:
    if args.output.exists() and not args.overwrite:
        raise ValueError(f"output already exists: {args.output}")
    _validate_common_options(args)
    trace = _load_trace_from_args(args)
    text = replay_to_cast(
        trace,
        options=_cast_options_from_args(
            args,
        ),
    )
    _write_output(text, args.output, stdout)
    return 3 if args.strict and trace.diagnostics else 0


def _cast_options_from_args(
    args: argparse.Namespace,
) -> CastOptions:
    options = load_cast_options(args.config)
    overrides: dict[str, int] = {}
    if args.payload_limit is not None:
        overrides["payload_limit"] = args.payload_limit
    if args.columns is not None:
        overrides["columns"] = args.columns
    if args.rows is not None:
        overrides["rows"] = args.rows
    if args.tail_hold_ms is not None:
        overrides["tail_hold_ms"] = args.tail_hold_ms
    return replace(options, **overrides)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            return _run_inspect(args, stdout=stdout)
        if args.command == "export":
            return _run_export(args, stdout=stdout)
        return 1
    except TraceVizInputError as exc:
        stderr.write(f"trace input error: {exc}\n")
        return 2
    except (OSError, ValueError) as exc:
        stderr.write(f"trace_viz error: {exc}\n")
        return 1
