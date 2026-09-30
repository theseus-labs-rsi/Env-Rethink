from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .event_search import EventSearchError, VisibleEventLog
from .manifest import ConditionManifest, ContextConfig, DataConfig, IndexLevel, StrictModel, ToolConfig


class ExperimentDataConfig(StrictModel):
    """Run-config input for workspace search.

    ``collection_set_path`` / ``collection_set_manifest_path`` select the
    explicit task-input-anchored development intervention.  The separate
    ``workspace_collection_set_path`` is the task-independent alternative and
    must be identical for every task in a workspace snapshot.
    """

    index_level: IndexLevel = "no_collection_map"
    relation_graph: Literal[False, True] = False
    semantic_retrieval: Literal[False] = False
    collection_set_path: str | None = None
    collection_set_manifest_path: str | None = None
    persona_collection_index_public_path: str | None = None
    persona_collection_index_sqlite_path: str | None = None
    workspace_collection_set_path: str | None = None

    @model_validator(mode="after")
    def validate_collection_source(self) -> "ExperimentDataConfig":
        task_source_count = sum(value is not None for value in (self.collection_set_path, self.collection_set_manifest_path))
        if self.index_level == "no_collection_map":
            if (
                self.relation_graph is not False
                or task_source_count
                or self.workspace_collection_set_path is not None
                or self.persona_collection_index_public_path is not None
                or self.persona_collection_index_sqlite_path is not None
            ):
                raise ValueError("no_collection_map must not receive a collection map")
            return self
        if self.index_level == "task_input_anchored_map":
            if (
                self.relation_graph is not True
                or task_source_count != 1
                or self.workspace_collection_set_path is not None
                or self.persona_collection_index_public_path is not None
                or self.persona_collection_index_sqlite_path is not None
            ):
                raise ValueError("task_input_anchored_map requires relation_graph=true and exactly one task collection source")
            return self
        if self.index_level == "persona_union_map":
            if (
                self.relation_graph is not True
                or task_source_count != 1
                or self.workspace_collection_set_path is not None
                or self.persona_collection_index_public_path is None
                or self.persona_collection_index_sqlite_path is None
            ):
                raise ValueError("persona_union_map requires one task collection source and a persona collection index")
            return self
        if (
            self.relation_graph is not True
            or task_source_count
            or self.workspace_collection_set_path is None
            or self.persona_collection_index_public_path is not None
            or self.persona_collection_index_sqlite_path is not None
        ):
            raise ValueError("workspace_snapshot_map/task_suite_oracle_map requires relation_graph=true and one workspace_collection_set_path")
        return self


class ExperimentConfig(StrictModel):
    repetition_id: int = Field(ge=1)
    docker_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_harness: Literal["codex", "pi"] = "codex"
    codex_version: Literal["0.144.5"] | None = "0.144.5"
    pi_version: Literal["0.84.1"] | None = None
    api_protocol: Literal["responses", "pi-json"] = "responses"
    require_user_isolation: bool = True
    agent_user: str = "workspace-agent"
    workspace_root: str = "/home/workspace-agent/workspace-bench-experiment"
    project_instructions: Literal["disabled", "controlled_agents"] = "disabled"
    expose_mcp_tools: bool = True
    tools: ToolConfig
    data: ExperimentDataConfig = Field(default_factory=ExperimentDataConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

    @model_validator(mode="after")
    def validate_harness_protocol(self) -> "ExperimentConfig":
        if self.task_harness == "codex":
            if self.codex_version != "0.144.5" or self.pi_version is not None or self.api_protocol != "responses":
                raise ValueError("Codex workspace_env runs require Codex 0.144.5 and native Responses")
            return self
        if self.pi_version != "0.84.1" or self.codex_version is not None or self.api_protocol != "pi-json":
            raise ValueError("PI workspace_env runs require PI 0.84.1 and the PI JSON protocol")
        if self.context.event_hook_mode != "disabled":
            raise ValueError("PI workspace_env runs do not support the Codex post_tool_use hook")
        if self.project_instructions != "disabled":
            raise ValueError("PI workspace_env runs require project instructions to remain disabled")
        return self


def workspace_snapshot_hash(root: str, *, exclude_controlled_agents_md: bool = False) -> str:
    """Hash the immutable workspace input view.

    ``model_output`` is the harness-reserved destination for a task result.
    It may be created empty before the MCP sidecar starts and is subsequently
    written by Codex, so it is deliberately not part of the input snapshot
    that validates an index or collection map.  When a controlled experiment
    stages its harness-owned ``AGENTS.md`` into the task cwd, that one runtime
    instruction file can likewise be excluded explicitly.  All other paths,
    including a source-workspace ``AGENTS.md`` under the default setting,
    remain part of the hash.
    """

    base = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        rel = path.relative_to(base).as_posix()
        if rel == "model_output" or rel.startswith("model_output/"):
            continue
        if exclude_controlled_agents_md and rel == "AGENTS.md":
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        elif stat.S_ISDIR(info.st_mode):
            kind = "dir"
        else:
            kind = "special"
        digest.update(json.dumps([rel, kind, stat.S_IMODE(info.st_mode)], separators=(",", ":")).encode("utf-8"))
        digest.update(b"\0")
        if kind == "symlink":
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif kind == "file":
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _stage_visible_event_log(context: ContextConfig, artifact: Path) -> ContextConfig:
    source_path = context.visible_event_log_path
    if source_path is None:
        if context.visible_event_log_sha256 is not None:
            raise ValueError("visible event-log hash requires a visible event-log path")
        if context.event_delete_rate != 1.0:
            raise ValueError("a visible event log is required unless event_delete_rate is 1.0")
        raw_bytes = b""
        digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    else:
        try:
            loaded = VisibleEventLog.load(source_path, expected_sha256=context.visible_event_log_sha256)
        except EventSearchError as exc:
            raise ValueError("visible event log could not be staged") from exc
        if context.event_delete_rate == 1.0 and loaded.events:
            raise ValueError("event_delete_rate=1.0 requires an empty visible event log")
        assert loaded.raw_bytes is not None
        raw_bytes = loaded.raw_bytes
        digest = loaded.sha256
    target = artifact / "visible_events.public.jsonl"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ValueError("visible event log could not be copied into the run artifact") from exc
    os.chmod(target, 0o600)
    return context.model_copy(
        update={
            "visible_event_log_path": str(target.resolve()),
            "visible_event_log_sha256": digest,
        }
    )


def _resolve_task_collection_set_path(data: ExperimentDataConfig, *, task_id: str) -> str:
    if data.collection_set_path is not None:
        return data.collection_set_path
    assert data.collection_set_manifest_path is not None
    from .collection_map import CollectionMapError, CollectionMapManifest, load_json_model

    try:
        manifest = load_json_model(data.collection_set_manifest_path, CollectionMapManifest)
    except CollectionMapError as exc:
        raise ValueError("private task collection-map routing manifest is invalid") from exc
    assert isinstance(manifest, CollectionMapManifest)
    matched = [entry.collection_set_path for entry in manifest.entries if entry.task_id == task_id]
    if len(matched) != 1:
        raise ValueError("private task collection-map routing manifest has no entry for this task")
    source = Path(matched[0])
    if source.is_file():
        return str(source)
    # Batch routing is private provenance and may have been produced on the
    # host before being mounted into Docker.  The public map package has a
    # fixed, task-scoped layout, so resolve only that verified relative target
    # when the recorded absolute host path is unavailable.  Do not attempt a
    # broad prefix rewrite or search arbitrary directories.
    relocated = (
        Path(data.collection_set_manifest_path).resolve().parent
        / "tasks"
        / task_id
        / "final"
        / "collection-set.public.json"
    )
    if relocated.is_file():
        return str(relocated)
    return matched[0]


def _stage_workspace_search_data(
    data: ExperimentDataConfig,
    *,
    artifact: Path,
    task_id: str,
    workspace_root: str,
    expected_workspace_snapshot_hash: str,
    exclude_controlled_agents_md: bool,
) -> DataConfig:
    if data.index_level == "no_collection_map":
        return DataConfig(index_level="no_collection_map")
    if data.index_level in {"task_input_anchored_map", "persona_union_map"}:
        collection_set_path = _resolve_task_collection_set_path(data, task_id=task_id)
        from .collection_map import CollectionMapError, stage_collection_map

        try:
            staged = stage_collection_map(collection_set_path=collection_set_path, artifact_root=artifact)
        except (CollectionMapError, OSError, ValueError) as exc:
            raise ValueError("reviewed task-input collection map could not be staged") from exc
        base = dict(
            index_level=data.index_level,
            relation_graph=True,
            collection_set_path=staged.collection_set_path,
            collection_set_sha256=staged.collection_set_sha256,
            collection_search_index_path=staged.search_index_path,
            collection_search_index_sha256=staged.search_index_sha256,
            task_start_briefing="collection_map",
        )
        if data.index_level == "task_input_anchored_map":
            return DataConfig(workspace_search_backend="collection_map", **base)
        assert data.persona_collection_index_public_path is not None
        assert data.persona_collection_index_sqlite_path is not None
        from .collection_map import stage_persona_collection_index

        try:
            persona = stage_persona_collection_index(
                public_index_path=data.persona_collection_index_public_path,
                search_index_path=data.persona_collection_index_sqlite_path,
                artifact_root=artifact,
                expected_workspace_snapshot_hash=expected_workspace_snapshot_hash,
            )
        except (CollectionMapError, OSError, ValueError) as exc:
            raise ValueError("reviewed persona collection index could not be staged") from exc
        return DataConfig(
            workspace_search_backend="persona_collection_map",
            persona_collection_index_public_path=persona.public_index_path,
            persona_collection_index_public_sha256=persona.public_index_sha256,
            persona_collection_search_index_path=persona.search_index_path,
            persona_collection_search_index_sha256=persona.search_index_sha256,
            **base,
        )
    assert data.workspace_collection_set_path is not None
    from .collection_map import CollectionMapError, stage_workspace_collection_map

    try:
        staged = stage_workspace_collection_map(
            collection_set_path=data.workspace_collection_set_path,
            artifact_root=artifact,
            workspace_root=workspace_root,
            expected_workspace_snapshot_hash=expected_workspace_snapshot_hash,
            exclude_controlled_agents_md=exclude_controlled_agents_md,
        )
    except (CollectionMapError, OSError, ValueError) as exc:
        raise ValueError("reviewed workspace collection map could not be staged") from exc
    return DataConfig(
        index_level=data.index_level,
        relation_graph=True,
        workspace_search_backend="workspace_collection_map",
        workspace_collection_set_path=staged.collection_set_path,
        workspace_collection_set_sha256=staged.collection_set_sha256,
        workspace_collection_search_index_path=staged.search_index_path,
        workspace_collection_search_index_sha256=staged.search_index_sha256,
    )


def materialize_condition_manifest(
    *,
    config: ExperimentConfig,
    run_id: str,
    task_id: str,
    work_dir: str,
    artifact_root: str,
) -> tuple[ConditionManifest, Path]:
    artifact = Path(artifact_root)
    artifact.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(artifact, 0o700)
    snapshot_hash = workspace_snapshot_hash(
        work_dir,
        exclude_controlled_agents_md=(config.project_instructions == "controlled_agents"),
    )
    staged_context = _stage_visible_event_log(config.context, artifact)
    staged_data = _stage_workspace_search_data(
        config.data,
        artifact=artifact,
        workspace_root=work_dir,
        task_id=task_id,
        expected_workspace_snapshot_hash=snapshot_hash,
        exclude_controlled_agents_md=(config.project_instructions == "controlled_agents"),
    )
    manifest = ConditionManifest(
        schema_version=1,
        run_id=run_id,
        task_id=task_id,
        repetition_id=config.repetition_id,
        workspace={
            "input_root": str(Path(work_dir).resolve()),
            "snapshot_hash": snapshot_hash,
            "artifact_root": str(artifact.resolve()),
        },
        data=staged_data,
        context=staged_context,
        tools=config.tools,
        logging={
            "audit_path": str((artifact / "mcp_calls.jsonl").resolve()),
            "raw_audit_path": str((artifact / "mcp_calls_raw.jsonl").resolve()),
        },
    )
    target = artifact / "condition_manifest.json"
    target.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    hash_path = artifact / "condition_manifest.sha256"
    hash_path.write_text("sha256:" + digest + "\n", encoding="utf-8")
    os.chmod(hash_path, 0o600)
    return manifest, target
