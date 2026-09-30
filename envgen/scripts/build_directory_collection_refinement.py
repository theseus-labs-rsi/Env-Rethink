#!/usr/bin/env python3
"""Add deterministic directory-navigation cards to a workspace_snapshot_map workspace map."""

from __future__ import annotations

import argparse
import hashlib
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


AUDIT_FORMAT = "workspace-bench.directory-collection-refinement-audit.v1"
RESULT_FORMAT = "workspace-bench.directory-collection-refinement-result.v1"
DEFAULT_EXCLUDED_PARTS = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__"})


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


def _is_safe_regular_file(workspace_root: Path, path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(workspace_root)
    except (OSError, ValueError):
        return False
    return resolved.is_file() and not resolved.is_symlink()


def discover_directory_collections(
    workspace_root: Path,
    *,
    minimum_files: int = 5,
    maximum_files: int = 30,
    excluded_parts: frozenset[str] = DEFAULT_EXCLUDED_PARTS,
) -> dict[str, list[str]]:
    """Return maximal homogeneous-extension directory subtrees.

    Selection depends only on the workspace tree. A qualifying directory has a
    bounded number of regular descendants and every descendant has the same
    lower-cased extension. If an ancestor already qualifies, nested qualifying
    directories are omitted so the refinement does not add redundant cards.
    """
    if minimum_files < 2 or maximum_files < minimum_files:
        raise CollectionMapError("invalid directory collection size bounds")
    workspace_root = workspace_root.resolve(strict=True)
    candidates: dict[str, list[str]] = {}
    directories = [workspace_root, *sorted(path for path in workspace_root.rglob("*") if path.is_dir())]
    for directory in directories:
        relative_directory = directory.relative_to(workspace_root)
        if any(part in excluded_parts for part in relative_directory.parts):
            continue
        files = []
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(workspace_root)
            if any(part in excluded_parts for part in relative.parts):
                continue
            if _is_safe_regular_file(workspace_root, path):
                files.append(relative.as_posix())
        if not minimum_files <= len(files) <= maximum_files:
            continue
        extensions = {Path(path).suffix.lower() for path in files}
        if len(extensions) != 1:
            continue
        relative_text = relative_directory.as_posix()
        if relative_text == ".":
            continue
        candidates[relative_text] = files

    selected: dict[str, list[str]] = {}
    for directory, files in sorted(candidates.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
        if any(Path(directory).is_relative_to(Path(parent)) for parent in selected):
            continue
        selected[directory] = files
    return selected


def _card_id(directory: str) -> str:
    digest = hashlib.sha256(directory.encode("utf-8")).hexdigest()[:12]
    return f"directory-{digest}"


def build_refinement(
    *,
    parent_collection_set: Path,
    parent_member_index: Path,
    workspace_root: Path,
    output_root: Path,
    minimum_files: int = 5,
    maximum_files: int = 30,
) -> dict[str, object]:
    if output_root.exists():
        raise CollectionMapError(f"output root already exists: {output_root}")
    workspace_root = workspace_root.resolve(strict=True)
    collection = load_json_model(parent_collection_set, WorkspaceCollectionSetV3)
    assert isinstance(collection, WorkspaceCollectionSetV3)
    snapshot_hash = workspace_snapshot_hash(str(workspace_root))
    if snapshot_hash != collection.workspace_snapshot_hash:
        raise CollectionMapError("workspace snapshot disagrees with the parent collection map")
    memberships = _read_memberships(parent_member_index)
    cards = {card.card_id: card for card in collection.cards}
    if set(cards) != set(memberships):
        raise CollectionMapError("parent map and member index card IDs disagree")
    parent_paths = {path for paths in memberships.values() for path in paths}

    discovered = discover_directory_collections(
        workspace_root,
        minimum_files=minimum_files,
        maximum_files=maximum_files,
    )
    missing = sorted({path for paths in discovered.values() for path in paths} - parent_paths)
    if missing:
        raise CollectionMapError(f"directory members are absent from the parent map: {missing[:3]}")

    added_cards: list[dict[str, object]] = []
    for directory, members in discovered.items():
        card_id = _card_id(directory)
        if card_id in cards:
            raise CollectionMapError(f"derived card ID collides with parent map: {card_id}")
        extension = Path(members[0]).suffix.lower() or "无扩展名"
        representatives = members[: min(6, len(members))]
        card = WorkspaceCollectionSummaryCard(
            card_id=card_id,
            title=f"目录文件集合：{directory}",
            description=(
                f"{directory} 目录及其子目录中的 {extension} 文件路径集合。"
                "本卡仅用于按目录定位文件；具体内容需读取原文件核验。"
            ),
            representative_content=representatives,
            file_count=len(members),
        )
        cards[card_id] = card
        memberships[card_id] = members
        added_cards.append({
            "card_id": card_id,
            "directory": directory,
            "extension": extension,
            "members": members,
        })

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
        "construction_kind": "task_independent_directory_collection_refinement",
        "reporting_boundary": (
            "Cards are derived from workspace directory boundaries, file counts, and extensions only; "
            "no task prompts, rubrics, reference answers, judge outputs, or business-value annotations are used."
        ),
        "workspace_snapshot_hash": snapshot_hash,
        "parent_collection_set": str(parent_collection_set),
        "parent_collection_set_sha256": sha256_file(parent_collection_set),
        "parent_member_index": str(parent_member_index),
        "parent_member_index_sha256": sha256_file(parent_member_index),
        "minimum_files": minimum_files,
        "maximum_files": maximum_files,
        "excluded_parts": sorted(DEFAULT_EXCLUDED_PARTS),
        "added_cards": added_cards,
        "parent_card_count": len(collection.cards),
        "final_card_count": len(refined.cards),
        "parent_membership_count": collection.membership_count,
        "final_membership_count": refined.membership_count,
        "agent_visible_collection_set_sha256": public_hash,
        "member_index_sha256": index_hash,
    }
    audit_path = final / "directory-refinement-audit.private.json"
    write_json(audit_path, audit, private=True)
    result = {
        "format": RESULT_FORMAT,
        "workspace_collection_set_path": str(public_path),
        "workspace_collection_set_sha256": public_hash,
        "workspace_collection_member_index_path": str(index_path),
        "workspace_collection_member_index_sha256": index_hash,
        "added_card_count": len(added_cards),
        "card_count": len(refined.cards),
        "membership_count": refined.membership_count,
    }
    write_json(final / "result.private.json", result, private=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-collection-set", type=Path, required=True)
    parser.add_argument("--parent-member-index", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--minimum-files", type=int, default=5)
    parser.add_argument("--maximum-files", type=int, default=30)
    args = parser.parse_args()
    try:
        result = build_refinement(
            parent_collection_set=args.parent_collection_set.resolve(strict=True),
            parent_member_index=args.parent_member_index.resolve(strict=True),
            workspace_root=args.workspace_root.resolve(strict=True),
            output_root=args.output_root.resolve(),
            minimum_files=args.minimum_files,
            maximum_files=args.maximum_files,
        )
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
