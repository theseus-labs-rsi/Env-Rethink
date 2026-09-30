"""Replay completed collection-cover rounds and write the final v3 artefacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.collection_map import (  # noqa: E402
    WorkspaceCatalog,
    WorkspaceCollectionSetV3,
    WorkspaceCollectionSummaryCard,
    build_workspace_collection_v3_index,
    load_json_model,
    sha256_file,
    write_json,
)
from workspace_env.integration import workspace_snapshot_hash  # noqa: E402
from workspace_env.workspace_collection_cover import (  # noqa: E402
    BucketSummary,
    CoordinationPlan,
    DeterministicBucket,
    ProposedCard,
    WorkspaceCollectionCoverConfig,
    WorkspaceCollectionCoverError,
    apply_coordination_plan_to_state,
)


def _state_hash(memberships: dict[str, list[str]]) -> str:
    raw = json.dumps(memberships, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def finalize(
    output_root: Path,
    *,
    completed_rounds: int | None = None,
    recovery_reason: str | None = None,
) -> dict[str, Any]:
    root = output_root.resolve(strict=True)
    stored_config = json.loads((root / "run-config.private.json").read_text(encoding="utf-8"))
    config = WorkspaceCollectionCoverConfig.model_validate(
        {
            key: stored_config[key]
            for key in WorkspaceCollectionCoverConfig.model_fields
            if key in stored_config
        }
    )
    if Path(config.output_root).resolve() != root:
        raise WorkspaceCollectionCoverError("stored output_root does not match the requested run")
    workspace = Path(config.workspace_root).resolve(strict=True)
    snapshot_hash = workspace_snapshot_hash(str(workspace))
    catalog = load_json_model(root / "workspace-catalog.private.json", WorkspaceCatalog)
    assert isinstance(catalog, WorkspaceCatalog)
    if catalog.workspace_snapshot_hash != snapshot_hash:
        raise WorkspaceCollectionCoverError("workspace changed after collection-cover construction")

    buckets = [
        DeterministicBucket.model_validate(item)
        for item in json.loads((root / "buckets.private.json").read_text(encoding="utf-8"))
    ]
    summaries: dict[str, BucketSummary] = {}
    for bucket in buckets:
        summary = load_json_model(
            root
            / "runs.private"
            / "buckets"
            / bucket.bucket_id
            / "workdir"
            / "output"
            / "bucket-summary.json",
            BucketSummary,
        )
        assert isinstance(summary, BucketSummary)
        if summary.bucket_id != bucket.bucket_id:
            raise WorkspaceCollectionCoverError("bucket summary changed its assigned bucket_id")
        summaries[bucket.bucket_id] = summary

    cards = [
        WorkspaceCollectionSummaryCard(
            card_id=bucket.bucket_id,
            title=summaries[bucket.bucket_id].title,
            description=summaries[bucket.bucket_id].description,
            representative_content=summaries[bucket.bucket_id].representative_content,
            file_count=len(bucket.paths),
        )
        for bucket in sorted(buckets, key=lambda item: item.bucket_id)
    ]
    memberships = {
        bucket.bucket_id: bucket.paths
        for bucket in sorted(buckets, key=lambda item: item.bucket_id)
    }
    seen_states = {_state_hash(memberships)}
    rounds_executed = 0
    converged = False
    round_audit: list[dict[str, Any]] = []
    round_limit = config.max_coordination_rounds if completed_rounds is None else completed_rounds
    if not 0 <= round_limit <= config.max_coordination_rounds:
        raise WorkspaceCollectionCoverError("completed_rounds is outside the configured coordination range")
    if (completed_rounds is None) != (recovery_reason is None):
        raise WorkspaceCollectionCoverError(
            "completed_rounds and recovery_reason must be supplied together"
        )
    for round_index in range(1, round_limit + 1):
        round_root = root / "runs.private" / "coordination" / f"round-{round_index:02d}"
        plan_path = round_root / "coordinator" / "workdir" / "output" / "coordination-plan.json"
        if not plan_path.exists():
            raise WorkspaceCollectionCoverError("a required coordination round is incomplete")
        plan = load_json_model(plan_path, CoordinationPlan)
        assert isinstance(plan, CoordinationPlan)
        reduction = apply_coordination_plan_to_state(
            current_cards=cards,
            current_memberships=memberships,
            plan=plan,
        )
        rounds_executed = round_index
        record: dict[str, Any] = {
            "round": round_index,
            "operation_count": len(plan.operations),
            "changed": reduction.changed,
            "affected_card_ids": list(reduction.affected_card_ids),
        }
        if not reduction.changed:
            cards = reduction.cards
            memberships = reduction.memberships
            converged = True
            round_audit.append(record)
            break
        state_hash = _state_hash(reduction.memberships)
        if state_hash in seen_states:
            raise WorkspaceCollectionCoverError("coordination entered a repeated membership state")
        seen_states.add(state_hash)
        refined: dict[str, ProposedCard] = {}
        for card_id in reduction.affected_card_ids:
            summary = load_json_model(
                round_root / "refine" / card_id / "workdir" / "output" / "card-summary.json",
                ProposedCard,
            )
            assert isinstance(summary, ProposedCard)
            if summary.card_id != card_id:
                raise WorkspaceCollectionCoverError("refinement changed its assigned card_id")
            refined[card_id] = summary
        cards = [
            WorkspaceCollectionSummaryCard(
                **(
                    refined[card.card_id].model_dump(mode="json")
                    if card.card_id in refined
                    else {
                        "card_id": card.card_id,
                        "title": card.title,
                        "description": card.description,
                        "representative_content": card.representative_content,
                    }
                ),
                file_count=len(reduction.memberships[card.card_id]),
            )
            for card in reduction.cards
        ]
        memberships = reduction.memberships
        record["refined_card_ids"] = sorted(refined)
        round_audit.append(record)

    collection = WorkspaceCollectionSetV3(
        workspace_snapshot_hash=snapshot_hash,
        distinct_file_count=len(catalog.files),
        membership_count=sum(len(paths) for paths in memberships.values()),
        cards=cards,
    )
    final = root / "final"
    final.mkdir(mode=0o700)
    if any(final.iterdir()):
        raise WorkspaceCollectionCoverError("final directory is not empty")
    public_path = final / "workspace-collection-set.public.json"
    public_hash = write_json(public_path, collection.model_dump(mode="json"), private=False)
    index_path = final / "workspace-collection-map.members.sqlite"
    index_hash = build_workspace_collection_v3_index(
        collection,
        memberships=memberships,
        index_path=index_path,
    )
    finalization_method = (
        "deterministic_replay_after_completed_roles"
        if completed_rounds is None
        else "deterministic_replay_to_last_valid_round_after_role_failure"
    )
    audit = {
        "format": "workspace-bench.workspace-collection-cover-audit.v1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "construction_kind": "workspace_snapshot_exhaustive_collection_map",
        "task_input_conditioned": False,
        "workspace_snapshot_hash": snapshot_hash,
        "workspace_catalog_sha256": sha256_file(root / "workspace-catalog.private.json"),
        "agent_visible_collection_set_sha256": public_hash,
        "member_index_sha256": index_hash,
        "bucket_count": len(buckets),
        "card_count": len(cards),
        "distinct_file_count": collection.distinct_file_count,
        "membership_count": collection.membership_count,
        "coordination_rounds_executed": rounds_executed,
        "coordination_converged": converged,
        "coordination_rounds": round_audit,
        "finalization_method": finalization_method,
        "configured_max_coordination_rounds": config.max_coordination_rounds,
        "recovery_reason": recovery_reason,
    }
    write_json(final / "workspace-collection-map.private.json", audit, private=True)
    result = {
        "status": "PASS",
        "workspace_collection_set_path": str(public_path),
        "workspace_collection_set_sha256": public_hash,
        "workspace_collection_member_index_path": str(index_path),
        "workspace_collection_member_index_sha256": index_hash,
        "bucket_count": len(buckets),
        "card_count": len(cards),
        "distinct_file_count": collection.distinct_file_count,
        "membership_count": collection.membership_count,
        "coordination_rounds_executed": rounds_executed,
        "coordination_converged": converged,
        "finalization_method": finalization_method,
        "configured_max_coordination_rounds": config.max_coordination_rounds,
        "recovery_reason": recovery_reason,
    }
    write_json(final / "result.private.json", result, private=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--completed-rounds", type=int)
    parser.add_argument("--recovery-reason")
    args = parser.parse_args()
    try:
        result = finalize(
            Path(args.output_root),
            completed_rounds=args.completed_rounds,
            recovery_reason=args.recovery_reason,
        )
    except (OSError, ValueError, WorkspaceCollectionCoverError) as exc:
        print(f"workspace collection-cover finalization failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
