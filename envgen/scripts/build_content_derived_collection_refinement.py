#!/usr/bin/env python3
"""Build an auditable task-independent content refinement of a workspace_snapshot_map map."""

from __future__ import annotations

import argparse
import json
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
    write_json,
)
from workspace_env.integration import workspace_snapshot_hash  # noqa: E402


SPEC_FORMAT = "workspace-bench.content-derived-collection-refinement-spec.v1"
AUDIT_FORMAT = "workspace-bench.content-derived-collection-refinement-audit.v1"


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


def _safe_existing_file(workspace_root: Path, relpath: str) -> Path:
    candidate = Path(relpath)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise CollectionMapError(f"unsafe member path: {relpath}")
    resolved = (workspace_root / candidate).resolve(strict=True)
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise CollectionMapError(f"member path escapes workspace: {relpath}") from exc
    if not resolved.is_file():
        raise CollectionMapError(f"member path is not a regular file: {relpath}")
    return resolved


def build_refinement(
    *, parent_collection_set: Path, parent_member_index: Path,
    workspace_root: Path, spec_path: Path, output_root: Path,
) -> dict[str, object]:
    if output_root.exists():
        raise CollectionMapError(f"output root already exists: {output_root}")
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("format") != SPEC_FORMAT:
        raise CollectionMapError("content-derived refinement spec has the wrong format")
    if "task_id" in json.dumps(raw, ensure_ascii=False).lower():
        raise CollectionMapError("content-derived refinement specs must not contain task IDs")

    collection = load_json_model(parent_collection_set, WorkspaceCollectionSetV3)
    assert isinstance(collection, WorkspaceCollectionSetV3)
    snapshot_hash = workspace_snapshot_hash(str(workspace_root))
    if snapshot_hash != collection.workspace_snapshot_hash:
        raise CollectionMapError("workspace snapshot disagrees with the parent collection map")
    memberships = _read_memberships(parent_member_index)
    cards = {card.card_id: card for card in collection.cards}
    if set(cards) != set(memberships):
        raise CollectionMapError("parent map and member index card IDs disagree")

    card_raw = raw.get("card")
    members_raw = raw.get("members")
    remove_from = raw.get("remove_from_cards", [])
    if not isinstance(card_raw, dict):
        raise CollectionMapError("spec requires a public card object")
    if not isinstance(members_raw, list) or not members_raw or not all(
        isinstance(item, str) and item for item in members_raw
    ):
        raise CollectionMapError("spec members must be a non-empty list of paths")
    if not isinstance(remove_from, list) or not all(isinstance(item, str) for item in remove_from):
        raise CollectionMapError("remove_from_cards must be a list of card IDs")

    members = sorted(set(members_raw))
    parent_paths = {path for paths in memberships.values() for path in paths}
    unknown = sorted(set(members) - parent_paths)
    if unknown:
        raise CollectionMapError(f"refinement members are absent from the parent map: {unknown[:3]}")
    source_hashes = {
        path: sha256_file(_safe_existing_file(workspace_root, path)) for path in members
    }
    card_id = str(card_raw["card_id"])
    if card_id in cards and card_id not in remove_from:
        raise CollectionMapError("new card_id already exists and was not declared for replacement")

    removal_set = set(members)
    for source_card_id in remove_from:
        if source_card_id not in cards:
            raise CollectionMapError(f"remove_from_cards references an unknown card: {source_card_id}")
        remaining = [path for path in memberships[source_card_id] if path not in removal_set]
        if not remaining:
            raise CollectionMapError(f"refinement would empty source card: {source_card_id}")
        old = cards[source_card_id]
        representative = [path for path in old.representative_content if path in remaining]
        if not representative:
            representative = remaining[: min(6, len(remaining))]
        cards[source_card_id] = old.model_copy(
            update={"representative_content": representative, "file_count": len(remaining)}
        )
        memberships[source_card_id] = remaining

    new_card = WorkspaceCollectionSummaryCard(
        card_id=card_id,
        title=str(card_raw["title"]),
        description=str(card_raw["description"]),
        representative_content=[str(item) for item in card_raw.get("representative_content", [])],
        file_count=len(members),
    )
    if not set(new_card.representative_content).issubset(set(members)):
        raise CollectionMapError("representative_content must be a subset of members")
    cards[card_id] = new_card
    memberships[card_id] = members

    refined = WorkspaceCollectionSetV3(
        workspace_snapshot_hash=snapshot_hash,
        distinct_file_count=collection.distinct_file_count,
        membership_count=sum(len(paths) for paths in memberships.values()),
        cards=sorted(cards.values(), key=lambda item: item.card_id),
    )
    final = output_root / "final"
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
        "construction_kind": "task_independent_content_derived_refinement",
        "reporting_boundary": "Content organization derived from file identities and contents only; no task prompts, rubrics, reference answers, or judge outputs were used.",
        "workspace_snapshot_hash": snapshot_hash,
        "spec_path": str(spec_path),
        "spec_sha256": sha256_file(spec_path),
        "parent_collection_set": str(parent_collection_set),
        "parent_collection_set_sha256": sha256_file(parent_collection_set),
        "parent_member_index": str(parent_member_index),
        "parent_member_index_sha256": sha256_file(parent_member_index),
        "agent_visible_collection_set_sha256": public_hash,
        "member_index_sha256": index_hash,
        "rationale": str(raw.get("rationale") or ""),
        "card_id": card_id,
        "member_paths": members,
        "member_sha256": source_hashes,
        "remove_from_cards": remove_from,
        "parent_card_count": len(collection.cards),
        "final_card_count": len(refined.cards),
        "parent_membership_count": collection.membership_count,
        "final_membership_count": refined.membership_count,
    }
    audit_path = final / "content-refinement-audit.private.json"
    write_json(audit_path, audit, private=True)
    result = {
        "format": "workspace-bench.content-derived-collection-refinement-result.v1",
        "workspace_collection_set_path": str(public_path),
        "workspace_collection_set_sha256": public_hash,
        "workspace_collection_member_index_path": str(index_path),
        "workspace_collection_member_index_sha256": index_hash,
        "card_count": len(refined.cards),
        "membership_count": refined.membership_count,
        "refined_card_id": card_id,
    }
    write_json(final / "result.private.json", result, private=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-collection-set", type=Path, required=True)
    parser.add_argument("--parent-member-index", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build_refinement(
            parent_collection_set=args.parent_collection_set.resolve(strict=True),
            parent_member_index=args.parent_member_index.resolve(strict=True),
            workspace_root=args.workspace_root.resolve(strict=True),
            spec_path=args.spec.resolve(strict=True),
            output_root=args.output_root.resolve(),
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
