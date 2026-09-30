#!/usr/bin/env python3
"""Build auditable manual refinements on top of immutable task_suite_oracle_map maps."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.collection_map import (  # noqa: E402
    CollectionMapError,
    WorkspaceCollectionSetV3,
    WorkspaceCollectionSummaryCard,
    build_workspace_collection_v3_index,
    load_json_model,
    sha256_file,
    task_input_bundle_from_metadata,
    write_json,
)
from workspace_env.integration import workspace_snapshot_hash  # noqa: E402


SPEC_FORMAT = "workspace-bench.manual-file-discovery-map-spec.v1"
AUDIT_FORMAT = "workspace-bench.manual-file-discovery-map-audit.v1"


def _read_memberships(index_path: Path) -> dict[str, list[str]]:
    connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT card_id, path FROM members ORDER BY card_id, member_index"
        ).fetchall()
    finally:
        connection.close()
    memberships: dict[str, list[str]] = {}
    for card_id, path in rows:
        memberships.setdefault(str(card_id), []).append(str(path))
    return memberships


def _resolve_case_paths(
    case: dict[str, object], *, workspace_root: Path, snapshot_hash: str
) -> tuple[list[str], str]:
    metadata_path = Path(str(case["metadata_path"])).resolve(strict=True)
    bundle = task_input_bundle_from_metadata(
        metadata_path,
        workspace_root=workspace_root,
        workspace_snapshot_hash=snapshot_hash,
    )
    if bundle.unresolved_inputs:
        labels = ", ".join(item.stored_relpath for item in bundle.unresolved_inputs)
        raise CollectionMapError(f"manual refinement has unresolved inputs: {labels}")
    paths = {item.workspace_path for item in bundle.inputs}
    additional_members = case.get("additional_members", [])
    if not isinstance(additional_members, list) or not all(
        isinstance(path, str) and path for path in additional_members
    ):
        raise CollectionMapError("manual case additional_members must be a list of paths")
    for path in additional_members:
        candidate = Path(path)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise CollectionMapError(f"manual case has an unsafe additional member: {path}")
        resolved = (workspace_root / candidate).resolve(strict=True)
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            raise CollectionMapError(f"manual case additional member escapes workspace: {path}") from exc
        if not resolved.is_file():
            raise CollectionMapError(f"manual case additional member is not a file: {path}")
        paths.add(candidate.as_posix())
    paths = sorted(paths)
    if len(paths) < 2:
        raise CollectionMapError("manual failed-case cards require at least two paths")
    return paths, sha256_file(metadata_path)


def build_manual_maps(*, spec_path: Path, output_root: Path) -> dict[str, object]:
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("format") != SPEC_FORMAT:
        raise CollectionMapError("manual refinement spec has the wrong format")
    workspaces = raw.get("workspaces")
    if not isinstance(workspaces, list) or not workspaces:
        raise CollectionMapError("manual refinement spec requires workspaces")
    output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    outputs: list[dict[str, object]] = []
    for workspace_spec in workspaces:
        if not isinstance(workspace_spec, dict):
            raise CollectionMapError("workspace spec must be an object")
        persona = str(workspace_spec["persona"])
        workspace_root = Path(str(workspace_spec["workspace_root"])).resolve(strict=True)
        parent_public = Path(str(workspace_spec["parent_collection_set"])).resolve(strict=True)
        parent_index = Path(str(workspace_spec["parent_member_index"])).resolve(strict=True)
        collection = load_json_model(parent_public, WorkspaceCollectionSetV3)
        assert isinstance(collection, WorkspaceCollectionSetV3)
        snapshot_hash = workspace_snapshot_hash(str(workspace_root))
        if snapshot_hash != collection.workspace_snapshot_hash:
            raise CollectionMapError(f"workspace snapshot disagrees with parent map for {persona}")
        memberships = _read_memberships(parent_index)
        cards = {card.card_id: card for card in collection.cards}
        if set(cards) != set(memberships):
            raise CollectionMapError(f"parent map/index card IDs disagree for {persona}")
        parent_paths = {path for paths in memberships.values() for path in paths}
        case_audits: list[dict[str, object]] = []
        cases = workspace_spec.get("cases")
        if not isinstance(cases, list) or not cases:
            raise CollectionMapError(f"workspace {persona} has no manual cases")
        for case in cases:
            if not isinstance(case, dict):
                raise CollectionMapError("manual case must be an object")
            card_raw = case.get("card")
            if not isinstance(card_raw, dict):
                raise CollectionMapError("manual case requires a public card")
            paths, metadata_hash = _resolve_case_paths(
                case, workspace_root=workspace_root, snapshot_hash=snapshot_hash
            )
            unknown = sorted(set(paths) - parent_paths)
            if unknown:
                raise CollectionMapError(
                    f"manual card contains paths absent from the parent map: {unknown[:3]}"
                )
            card = WorkspaceCollectionSummaryCard(
                card_id=str(card_raw["card_id"]),
                title=str(card_raw["title"]),
                description=str(card_raw["description"]),
                representative_content=[str(item) for item in card_raw.get("representative_content", [])],
                file_count=len(paths),
            )
            action = "replaced" if card.card_id in cards else "added"
            cards[card.card_id] = card
            memberships[card.card_id] = paths
            case_audits.append(
                {
                    "task_id": str(case["task_id"]),
                    "metadata_path": str(Path(str(case["metadata_path"])).resolve()),
                    "metadata_sha256": metadata_hash,
                    "diagnosis": str(case["diagnosis"]),
                    "card_id": card.card_id,
                    "action": action,
                    "member_count": len(paths),
                    "member_paths": paths,
                    "additional_members": [str(path) for path in case.get("additional_members", [])],
                }
            )
        supporting_audits: list[dict[str, object]] = []
        static_cards = workspace_spec.get("static_cards", [])
        if not isinstance(static_cards, list):
            raise CollectionMapError("workspace static_cards must be a list")
        for static_raw in static_cards:
            if not isinstance(static_raw, dict):
                raise CollectionMapError("static card must be an object")
            member_paths = sorted({str(path) for path in static_raw.get("members", [])})
            if not member_paths:
                raise CollectionMapError("static card requires members")
            unknown = sorted(set(member_paths) - parent_paths)
            if unknown:
                raise CollectionMapError(
                    f"static card contains paths absent from the parent map: {unknown[:3]}"
                )
            card = WorkspaceCollectionSummaryCard(
                card_id=str(static_raw["card_id"]),
                title=str(static_raw["title"]),
                description=str(static_raw["description"]),
                representative_content=[
                    str(item) for item in static_raw.get("representative_content", [])
                ],
                file_count=len(member_paths),
            )
            action = "replaced" if card.card_id in cards else "added"
            cards[card.card_id] = card
            memberships[card.card_id] = member_paths
            supporting_audits.append(
                {
                    "card_id": card.card_id,
                    "action": action,
                    "rationale": str(static_raw["rationale"]),
                    "member_count": len(member_paths),
                    "member_paths": member_paths,
                }
            )
        refined = WorkspaceCollectionSetV3(
            workspace_snapshot_hash=snapshot_hash,
            distinct_file_count=collection.distinct_file_count,
            membership_count=sum(len(paths) for paths in memberships.values()),
            cards=sorted(cards.values(), key=lambda item: item.card_id),
        )
        final = output_root / persona.lower().replace(" ", "-") / "final"
        final.mkdir(parents=True, mode=0o700)
        public_path = final / "workspace-collection-set.public.json"
        public_hash = write_json(public_path, refined.model_dump(mode="json"), private=False)
        index_path = final / "workspace-collection-map.members.sqlite"
        index_hash = build_workspace_collection_v3_index(
            refined, memberships=memberships, index_path=index_path
        )
        audit = {
            "format": AUDIT_FORMAT,
            "visibility": "private",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "construction_kind": "manual_targeted_file_discovery_refinement",
            "reporting_boundary": "targeted upper-bound intervention; not a task-independent map",
            "persona": persona,
            "workspace_snapshot_hash": snapshot_hash,
            "spec_path": str(spec_path.resolve()),
            "spec_sha256": sha256_file(spec_path),
            "parent_collection_set": str(parent_public),
            "parent_collection_set_sha256": sha256_file(parent_public),
            "parent_member_index": str(parent_index),
            "parent_member_index_sha256": sha256_file(parent_index),
            "agent_visible_collection_set_sha256": public_hash,
            "member_index_sha256": index_hash,
            "parent_card_count": len(collection.cards),
            "final_card_count": len(refined.cards),
            "parent_membership_count": collection.membership_count,
            "final_membership_count": refined.membership_count,
            "cases": case_audits,
            "supporting_adjustments": supporting_audits,
        }
        audit_path = final / "manual-refinement-audit.private.json"
        write_json(audit_path, audit, private=True)
        result = {
            "persona": persona,
            "workspace_collection_set_path": str(public_path),
            "workspace_collection_set_sha256": public_hash,
            "workspace_collection_member_index_path": str(index_path),
            "workspace_collection_member_index_sha256": index_hash,
            "manual_case_count": len(case_audits),
            "card_count": len(refined.cards),
            "membership_count": refined.membership_count,
        }
        write_json(final / "result.private.json", result, private=True)
        outputs.append(result)
    manifest = {
        "format": "workspace-bench.manual-file-discovery-map-set.v1",
        "spec_path": str(spec_path.resolve()),
        "spec_sha256": sha256_file(spec_path),
        "map_count": len(outputs),
        "outputs": outputs,
    }
    write_json(output_root / "manifest.private.json", manifest, private=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    try:
        result = build_manual_maps(
            spec_path=Path(args.spec).resolve(strict=True),
            output_root=Path(args.output_root).resolve(),
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps({"map_count": result["map_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
