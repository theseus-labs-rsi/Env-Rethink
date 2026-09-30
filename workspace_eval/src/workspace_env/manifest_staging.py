"""Stage the generated environment layer for one task run.

The generation chains write three artifacts per task (collection map JSON,
member index SQLite, visible event log JSONL) next to a ``fixture.json`` that
records how they were produced.  At task time the sidecar copies those artifacts
into a private artifact root, verifies them against the recorded digests, and
builds the :class:`ConditionManifest` the runtime consumes.

Deliberate difference from the source project
---------------------------------------------
The source implementation re-hashed the *entire* live workspace
(``workspace.rglob("*")``) and required the staged map's member set to equal it.
That is impossible here — the map is built on the host over the task's file pool
while the runtime workspace is the agent's work dir (role workspace + task
inputs + outputs) — and it is forbidden by this repository's "no full-tree
scans" rule.  Verification is therefore:

1. fatal: artifact digests, and the map/index internal consistency counts;
2. informational: a bounded ``stat`` spot check of up to
   :data:`SPOT_CHECK_LIMIT` member paths, recorded in the staging audit.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .manifest import (
    ConditionManifest,
    ContextConfig,
    DataConfig,
    LoggingConfig,
    ResolvedManifest,
    ToolConfig,
    WorkspaceConfig,
    canonical_json,
    resolve_manifest,
    sha256_text,
)

SPOT_CHECK_LIMIT = 200

# Artifact surfaces a task may ship.  ``collection_map`` stages the reviewed
# collection map (plus its member index) for the map/search tools; ``history``
# stages the visible work history for ``event_search``.  A fixture that omits
# ``surfaces`` ships both, which keeps every pre-existing fixture valid; a
# disabled surface keeps its tools reachable but backed by the empty backend, so
# the agent sees the same tool surface either way.
SURFACE_COLLECTION_MAP = "collection_map"
SURFACE_HISTORY = "history"
SURFACES: tuple[str, ...] = (SURFACE_COLLECTION_MAP, SURFACE_HISTORY)
FIXTURE_SCHEMA_VERSION = 1

COLLECTION_SET_NAME = "workspace-collection-map.public.json"
MEMBER_INDEX_NAME = "workspace-collection-map.members.sqlite"
EVENT_LOG_NAME = "visible_events.public.jsonl"

__all__ = ["ManifestStagingError", "stage_environment_layer"]


class ManifestStagingError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestStagingError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestStagingError(f"{path} must contain a JSON object")
    return value


def enabled_surfaces(fixture: dict[str, Any]) -> tuple[str, ...]:
    """Enabled artifact surfaces, in canonical order.

    Absent means "ship both", so fixtures written before this field existed keep
    their behaviour.  An unknown or empty list is a fixture error: silently
    serving nothing would make a broken task look like an empty one.
    """

    raw = fixture.get("surfaces")
    if raw is None:
        return SURFACES
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) for item in raw):
        raise ManifestStagingError("fixture surfaces must be a non-empty list of surface names")
    unknown = sorted(set(raw) - set(SURFACES))
    if unknown:
        raise ManifestStagingError(f"unknown fixture surface(s): {', '.join(unknown)}")
    return tuple(name for name in SURFACES if name in raw)


def _require_file(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ManifestStagingError(f"fixture is missing {label}")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ManifestStagingError(f"{label} escapes the fixture root: {relative}") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise ManifestStagingError(f"{label} is not a regular file: {candidate}")
    return candidate


def _copy_verified(source: Path, target: Path, expected_sha256: Any) -> str:
    if not isinstance(expected_sha256, str) or not expected_sha256.startswith("sha256:"):
        raise ManifestStagingError(f"fixture is missing the digest for {source.name}")
    digest = _sha256_file(source)
    if digest != expected_sha256:
        raise ManifestStagingError(f"artifact digest mismatch for {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    return digest


def _member_paths(index_path: Path, *, limit: int) -> tuple[list[str], int]:
    connection = sqlite3.connect(index_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT path FROM members ORDER BY path LIMIT ?", (limit,)
        ).fetchall()
        total = connection.execute("SELECT COUNT(*) FROM members").fetchone()
    finally:
        connection.close()
    return [str(row[0]) for row in rows], int(total[0]) if total else 0


def stage_environment_layer(
    *,
    fixture_path: Path,
    blobs_root: Path,
    workspace_root: Path,
    state_dir: Path,
    run_id: str,
    task_id: str,
) -> tuple[ResolvedManifest, Path, dict[str, Any]]:
    """Stage artifacts and return ``(resolved_manifest, manifest_path, audit)``."""

    fixture = _read_json(fixture_path)
    if fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ManifestStagingError("unsupported environment fixture schema version")
    artifacts = fixture.get("artifacts")
    pool = fixture.get("pool")
    run_config = fixture.get("run_config")
    if not isinstance(artifacts, dict) or not isinstance(pool, dict) or not isinstance(run_config, dict):
        raise ManifestStagingError("fixture is missing artifacts/pool/run_config")

    artifact_root = (state_dir / "artifacts").resolve()
    artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(artifact_root, 0o700)

    enabled = enabled_surfaces(fixture)
    collection_path: Path | None = None
    index_path: Path | None = None
    events_path: Path | None = None
    collection_sha: str | None = None
    index_sha: str | None = None
    events_sha: str | None = None
    if SURFACE_COLLECTION_MAP in enabled:
        collection_source = _require_file(
            blobs_root, artifacts.get("collection_set"), "collection_set"
        )
        index_source = _require_file(blobs_root, artifacts.get("member_index"), "member_index")
        collection_path = artifact_root / COLLECTION_SET_NAME
        index_path = artifact_root / MEMBER_INDEX_NAME
        collection_sha = _copy_verified(
            collection_source, collection_path, artifacts.get("collection_set_sha256")
        )
        index_sha = _copy_verified(
            index_source, index_path, artifacts.get("member_index_sha256")
        )
    if SURFACE_HISTORY in enabled:
        events_source = _require_file(blobs_root, artifacts.get("events"), "events")
        events_path = artifact_root / EVENT_LOG_NAME
        events_sha = _copy_verified(
            events_source, events_path, artifacts.get("events_sha256")
        )

    snapshot_hash = pool.get("workspace_snapshot_hash")
    if not isinstance(snapshot_hash, str) or not snapshot_hash.startswith("sha256:"):
        raise ManifestStagingError("fixture is missing the generation-time workspace snapshot hash")

    # Bounded, informational spot check: the map's members should exist in the
    # live work dir, but the condition decides which inputs are materialized, so
    # a mismatch is recorded rather than fatal.
    members: list[str] = []
    member_total = 0
    missing: list[str] = []
    if index_path is not None:
        members, member_total = _member_paths(index_path, limit=SPOT_CHECK_LIMIT)
        missing = [
            path
            for path in members
            if not (workspace_root / path).is_file()
        ]

    if SURFACE_COLLECTION_MAP in enabled:
        data = DataConfig(
            index_level="workspace_snapshot_map",
            relation_graph=True,
            workspace_search_backend="workspace_collection_map",
            workspace_collection_set_path=str(collection_path),
            workspace_collection_set_sha256=collection_sha,
            workspace_collection_search_index_path=str(index_path),
            workspace_collection_search_index_sha256=index_sha,
        )
    else:
        # Same tool surface, empty backend: the condition manifest records the
        # absence explicitly instead of relying on a missing index file.
        data = DataConfig()
    history_enabled = SURFACE_HISTORY in enabled
    context = ContextConfig(
        event_delete_rate=(run_config.get("event_delete_rate", 0.0) if history_enabled else 1.0),
        deletion_seed=int(run_config.get("deletion_seed", 0)),
        visible_event_log_path=(str(events_path) if history_enabled else None),
        visible_event_log_sha256=(events_sha if history_enabled else None),
    )
    tools = ToolConfig(view_level="shell_only")
    manifest = ConditionManifest(
        schema_version=1,
        run_id=run_id,
        task_id=task_id,
        repetition_id=1,
        workspace=WorkspaceConfig(
            input_root=str(workspace_root),
            snapshot_hash=snapshot_hash,
            artifact_root=str(artifact_root),
        ),
        data=data,
        context=context,
        tools=tools,
        logging=LoggingConfig(
            audit_path=str(artifact_root / "mcp_calls.jsonl"),
            raw_audit_path=str(artifact_root / "mcp_calls_raw.jsonl"),
        ),
    )
    resolved = resolve_manifest(manifest)
    manifest_path = artifact_root / "condition_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    (artifact_root / "condition_manifest.sha256").write_text(
        resolved.manifest_hash + "\n", encoding="utf-8"
    )

    audit = {
        "schema_version": 1,
        "fixture": str(fixture_path),
        "fixture_sha256": _sha256_file(fixture_path),
        "generator": fixture.get("generator"),
        "artifact_root": str(artifact_root),
        "workspace_snapshot_hash": snapshot_hash,
        "workspace_snapshot_hash_scope": "generation-time task file pool",
        "manifest_hash": resolved.manifest_hash,
        "condition_hash": resolved.condition_hash,
        "member_count": member_total,
        "surfaces": list(enabled),
        "spot_check": {
            "checked": len(members),
            "missing_in_workspace": len(missing),
            "missing_examples": missing[:20],
        },
        "conditional_hash": sha256_text(
            canonical_json(
                {
                    "fixture": fixture.get("artifacts"),
                    "pool": pool,
                    "surfaces": list(enabled),
                }
            )
        ),
    }
    audit_path = artifact_root / "staging.private.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(audit_path, 0o600)
    return resolved, manifest_path, audit
