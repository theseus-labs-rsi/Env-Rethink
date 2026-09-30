#!/usr/bin/env python3
"""Prepare a workspace subset, then run the multi-agent noise pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from multi_agent import CodexBackend, NoisePipeline
from path_relocation import PathRelocator
from workspace_subset import SubsetBudget, build_workspace_subset


Json = Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    workspace_source = parser.add_mutually_exclusive_group(required=True)
    workspace_source.add_argument(
        "--raw-workspace",
        type=Path,
        help=(
            "Complete role workspace used to build a generation subset. "
            "Mutually exclusive with --workspace-subset."
        ),
    )
    workspace_source.add_argument(
        "--workspace-subset",
        type=Path,
        help=(
            "Existing task-specific generation subset. Use this for a "
            "portable noise-generation bundle that does not include the "
            "complete raw workspace."
        ),
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument("--planner-provider-config", type=Path)
    parser.add_argument("--worker-provider-config", type=Path)
    parser.add_argument("--validator-provider-config", type=Path)
    parser.add_argument("--worker-fallback-provider-config", type=Path)
    parser.add_argument(
        "--path-planner-provider-config",
        type=Path,
        help="Provider for the path-relocation planner; defaults to --provider-config.",
    )
    parser.add_argument(
        "--path-auditor-provider-config",
        type=Path,
        help="Provider for the path-relocation auditor; defaults to --validator-provider-config.",
    )
    parser.add_argument(
        "--path-seed",
        type=int,
        default=None,
        help="Seed for path relocation; defaults to --seed.",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--common-dir-files", type=int, default=20)
    parser.add_argument("--max-versions", type=int, default=3)
    parser.add_argument("--max-rework-rounds", type=int, default=3)
    parser.add_argument("--worker-parallelism", type=int, default=1)
    parser.add_argument(
        "--min-distractors-per-job",
        type=int,
        default=1,
        help=(
            "Minimum noise files each standard input must receive. A job below "
            "this count fails the run instead of shipping an undisturbed input."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--planner-timeout-seconds", type=float)
    parser.add_argument("--worker-timeout-seconds", type=float)
    parser.add_argument("--validator-timeout-seconds", type=float)
    parser.add_argument("--clean", action="store_true")
    return parser.parse_args()


def load_provider(path: Path) -> dict[str, Json]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("provider config must be an object")
    provider = (
        value.get("api_provider")
        if isinstance(value.get("api_provider"), dict)
        else value
    )
    return dict(provider)


def main() -> int:
    args = parse_args()
    task_dir = args.task_dir.resolve()
    run_dir = args.run_dir.resolve()
    if args.workspace_subset is not None:
        subset_root = args.workspace_subset.resolve()
        if not subset_root.is_dir():
            raise FileNotFoundError(
                f"workspace subset is not a directory: {subset_root}"
            )
        required_files = (
            "subset_manifest.json",
            "source_path_map.json",
        )
        missing = [
            filename
            for filename in required_files
            if not (subset_root / filename).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "workspace subset is missing required metadata: "
                + ", ".join(missing)
            )
    else:
        subset_root = run_dir / "workspace_subset"
        build_workspace_subset(
            task_dir=task_dir,
            raw_workspace=args.raw_workspace.resolve(),
            output_root=subset_root,
            budget=SubsetBudget(
                max_files=args.max_files,
                max_bytes=args.max_bytes,
                common_dir_files=args.common_dir_files,
            ),
            strict_matches=False,
            clean=args.clean,
        )
    pipeline = NoisePipeline(
        task_dir=task_dir,
        subset_root=subset_root,
        run_dir=run_dir / "agent_run",
        backend=CodexBackend(
            provider=load_provider(args.provider_config),
            timeout_seconds=args.timeout_seconds,
        ),
        planner_backend=(
            CodexBackend(
                provider=load_provider(args.planner_provider_config),
                timeout_seconds=(
                    args.planner_timeout_seconds or args.timeout_seconds
                ),
            )
            if args.planner_provider_config
            else None
        ),
        worker_backend=(
            CodexBackend(
                provider=load_provider(args.worker_provider_config),
                timeout_seconds=(
                    args.worker_timeout_seconds or args.timeout_seconds
                ),
            )
            if args.worker_provider_config
            else None
        ),
        validator_backend=(
            CodexBackend(
                provider=load_provider(args.validator_provider_config),
                timeout_seconds=(
                    args.validator_timeout_seconds or args.timeout_seconds
                ),
            )
            if args.validator_provider_config
            else None
        ),
        worker_fallback_backend=(
            CodexBackend(
                provider=load_provider(args.worker_fallback_provider_config),
                timeout_seconds=(
                    args.worker_timeout_seconds or args.timeout_seconds
                ),
            )
            if args.worker_fallback_provider_config
            else None
        ),
        seed=args.seed,
        max_versions=max(1, args.max_versions),
        max_rework_rounds=max(0, args.max_rework_rounds),
        worker_parallelism=max(1, args.worker_parallelism),
        min_distractors_per_job=max(0, args.min_distractors_per_job),
    )
    result = pipeline.run()

    # Path enhancement runs regardless of the noise pipeline status: a failed
    # noise run still ships a usable task, and relocation only records, never
    # blocks. Files the auditor rejects keep their original location.
    path_planner_config = (
        args.path_planner_provider_config or args.provider_config
    )
    path_auditor_config = (
        args.path_auditor_provider_config or args.validator_provider_config
    )
    path_seed = args.path_seed if args.path_seed is not None else args.seed
    relocator = PathRelocator(
        task_dir=run_dir / "agent_run" / "integrated" / "task",
        generation_dir=run_dir / "agent_run" / "generation",
        planner_backend=(
            CodexBackend(
                provider=load_provider(path_planner_config),
                timeout_seconds=args.timeout_seconds,
            )
            if path_planner_config
            else None
        ),
        auditor_backend=(
            CodexBackend(
                provider=load_provider(path_auditor_config),
                timeout_seconds=args.timeout_seconds,
            )
            if path_auditor_config
            else None
        ),
        seed=path_seed,
    )
    relocation = relocator.run(pipeline_result=result)
    result["path_relocation"] = relocation

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
