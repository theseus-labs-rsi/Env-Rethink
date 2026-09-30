from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workspace_subset import (
    DEFAULT_COMMON_DIRECTORIES,
    SubsetBudget,
    WorkspaceSubsetError,
    build_workspace_subset,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Construct a deterministic task workspace subset by matching "
            "data_manifest inputs against a role raw workspace."
        )
    )
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--raw-workspace", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--metadata",
        type=Path,
        help="Metadata JSON path; defaults to TASK_DIR/metadata.json.",
    )
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=512 * 1024 * 1024,
    )
    parser.add_argument("--common-dir-files", type=int, default=20)
    parser.add_argument("--min-root-depth", type=int, default=2)
    parser.add_argument(
        "--common-dir",
        action="append",
        dest="common_dirs",
        help=(
            "Top-level common directory to sample. Repeat to provide "
            "multiple directories. Defaults to common desktop/download/"
            "document/archive/share names."
        ),
    )
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help="Also match manifest entries already marked as generated noise.",
    )
    parser.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="Build a partial subset instead of failing on unmatched inputs.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove OUTPUT_ROOT before building.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_workspace_subset(
            task_dir=args.task_dir,
            metadata_path=args.metadata,
            raw_workspace=args.raw_workspace,
            output_root=args.output_root,
            budget=SubsetBudget(
                max_files=args.max_files,
                max_bytes=args.max_bytes,
                common_dir_files=args.common_dir_files,
            ),
            min_root_depth=args.min_root_depth,
            common_directories=(
                args.common_dirs
                if args.common_dirs is not None
                else DEFAULT_COMMON_DIRECTORIES
            ),
            include_generated=args.include_generated,
            strict_matches=not args.allow_unmatched,
            clean=args.clean,
        )
    except (WorkspaceSubsetError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

