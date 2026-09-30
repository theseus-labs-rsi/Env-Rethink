#!/usr/bin/env python3
"""Resume targeted rework from an existing local-noise agent run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from multi_agent import CodexBackend, NoisePipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--subset-root", type=Path, required=True)
    parser.add_argument("--agent-run-dir", type=Path, required=True)
    parser.add_argument("--planner-provider-config", type=Path, required=True)
    parser.add_argument("--worker-provider-config", type=Path, required=True)
    parser.add_argument("--validator-provider-config", type=Path, required=True)
    parser.add_argument("--worker-fallback-provider-config", type=Path)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--max-rework-rounds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--worker-timeout-seconds", type=float)
    parser.add_argument("--validator-timeout-seconds", type=float)
    parser.add_argument("--worker-parallelism", type=int, default=4)
    parser.add_argument("--min-distractors-per-job", type=int, default=1)
    return parser.parse_args()


def provider(path: Path):
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value["api_provider"] if isinstance(value.get("api_provider"), dict) else value


def main() -> int:
    args = parse_args()
    fallback = CodexBackend(
        provider=provider(args.planner_provider_config),
        timeout_seconds=args.timeout_seconds,
    )
    pipeline = NoisePipeline(
        task_dir=args.task_dir.resolve(),
        subset_root=args.subset_root.resolve(),
        run_dir=args.agent_run_dir.resolve(),
        backend=fallback,
        planner_backend=fallback,
        worker_backend=CodexBackend(
            provider=provider(args.worker_provider_config),
            timeout_seconds=(
                args.worker_timeout_seconds or args.timeout_seconds
            ),
        ),
        validator_backend=CodexBackend(
            provider=provider(args.validator_provider_config),
            timeout_seconds=(
                args.validator_timeout_seconds or args.timeout_seconds
            ),
        ),
        worker_fallback_backend=(
            CodexBackend(
                provider=provider(args.worker_fallback_provider_config),
                timeout_seconds=(
                    args.worker_timeout_seconds or args.timeout_seconds
                ),
            )
            if args.worker_fallback_provider_config
            else None
        ),
        seed=args.seed,
        max_rework_rounds=max(1, args.max_rework_rounds),
        worker_parallelism=max(1, args.worker_parallelism),
        min_distractors_per_job=max(0, args.min_distractors_per_job),
    )
    result = pipeline.resume()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
