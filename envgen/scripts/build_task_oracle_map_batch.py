#!/usr/bin/env python3
"""Build one private task-suite context and continuation config for OracleMap."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.collection_map import (  # noqa: E402
    CollectionMapError,
    sha256_file,
    task_input_bundle_from_metadata,
    write_json,
)
from workspace_env.integration import workspace_snapshot_hash  # noqa: E402
from workspace_env.workspace_collection_cover import (  # noqa: E402
    OracleExcludedTask,
    OracleTaskRequirement,
    OracleTaskSuiteContext,
    WorkspaceCollectionContinuationConfig,
)


def build_context(
    *, tasks_root: Path, persona: str, workspace_root: Path
) -> OracleTaskSuiteContext:
    snapshot_hash = workspace_snapshot_hash(str(workspace_root))
    requirements: list[OracleTaskRequirement] = []
    excluded: list[OracleExcludedTask] = []
    for metadata_path in sorted(tasks_root.glob("*/metadata.json"), key=lambda path: path.parent.name):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CollectionMapError(f"invalid metadata JSON: {metadata_path}") from exc
        if not isinstance(metadata, dict) or metadata.get("persona") != persona:
            continue
        task_id = metadata_path.parent.name
        description = metadata.get("task")
        if not isinstance(description, str) or not description.strip():
            raise CollectionMapError(f"task {task_id} has no task description")
        metadata_hash = sha256_file(metadata_path)
        bundle = task_input_bundle_from_metadata(
            metadata_path,
            workspace_root=workspace_root,
            workspace_snapshot_hash=snapshot_hash,
        )
        by_input: dict[str, set[str]] = defaultdict(set)
        for item in bundle.inputs:
            by_input[item.stored_relpath].add(item.workspace_path)
        unresolved_labels = [item.stored_relpath for item in bundle.unresolved_inputs]
        if unresolved_labels:
            excluded.append(
                OracleExcludedTask(
                    task_id=task_id,
                    metadata_sha256=metadata_hash,
                    reason="unresolved_input",
                    private_input_labels=sorted(unresolved_labels),
                )
            )
            continue
        # The file-discovery scorer treats multiple byte-identical workspace
        # bindings for one stored input as acceptable alternatives.  Put every
        # candidate in the same oracle card so the map does not privately pick
        # one arbitrary copy and remains aligned with that scoring protocol.
        required_paths = sorted({path for paths in by_input.values() for path in paths})
        requirements.append(
            OracleTaskRequirement(
                task_id=task_id,
                task_description=description.strip(),
                metadata_sha256=metadata_hash,
                required_workspace_paths=required_paths,
            )
        )
    if not requirements:
        raise CollectionMapError(f"no unambiguous tasks found for persona {persona!r}")
    return OracleTaskSuiteContext(
        persona=persona,
        workspace_snapshot_hash=snapshot_hash,
        tasks=sorted(requirements, key=lambda item: item.task_id),
        excluded_tasks=sorted(excluded, key=lambda item: item.task_id),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a new task-oracle map config from an immutable GeneralMap parent."
    )
    parser.add_argument("--tasks-root", default=str(EVALUATION_ROOT / "tasks_lite_updated"))
    parser.add_argument("--persona", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--parent-collection-set", required=True)
    parser.add_argument("--parent-member-index", required=True)
    parser.add_argument("--parent-audit", required=True)
    parser.add_argument("--build-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--auth-mode", choices=("chatgpt", "api"), default="chatgpt")
    parser.add_argument("--base-url")
    parser.add_argument(
        "--reasoning-effort",
        choices=("minimal", "low", "medium", "high", "xhigh"),
        default="xhigh",
    )
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--max-parallel-refinements", type=int, default=5)
    parser.add_argument("--max-rounds", type=int, default=5)
    args = parser.parse_args()

    build_root = Path(args.build_root).resolve()
    if build_root.exists():
        parser.error("--build-root must not already exist")
    build_root.mkdir(parents=True, mode=0o700)
    try:
        context = build_context(
            tasks_root=Path(args.tasks_root).resolve(strict=True),
            persona=args.persona,
            workspace_root=Path(args.workspace_root).resolve(strict=True),
        )
        context_path = build_root / "task-suite-map-context.private.json"
        write_json(context_path, context.model_dump(mode="json"), private=True)
        config = WorkspaceCollectionContinuationConfig(
            run_id=args.run_id,
            workspace_root=str(Path(args.workspace_root).resolve(strict=True)),
            parent_collection_set_path=str(Path(args.parent_collection_set).resolve(strict=True)),
            parent_member_index_path=str(Path(args.parent_member_index).resolve(strict=True)),
            parent_audit_path=str(Path(args.parent_audit).resolve(strict=True)),
            output_root=str(build_root / "oracle-map"),
            model=args.model,
            auth_mode=args.auth_mode,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.timeout_seconds,
            max_parallel_refinements=args.max_parallel_refinements,
            max_additional_coordination_rounds=args.max_rounds,
            stop_on_convergence=False,
            task_input_conditioned=True,
            oracle_task_suite_context_path=str(context_path),
        )
        config_path = build_root / "continuation-config.private.json"
        write_json(config_path, config.model_dump(mode="json"), private=True)
    except (OSError, CollectionMapError, ValueError) as exc:
        print(f"task oracle map config failed: {exc}", file=sys.stderr)
        return 2
    os.chmod(build_root, 0o700)
    print(
        json.dumps(
            {
                "config_path": str(config_path),
                "context_path": str(context_path),
                "output_root": config.output_root,
                "included_task_count": len(context.tasks),
                "excluded_task_count": len(context.excluded_tasks),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
