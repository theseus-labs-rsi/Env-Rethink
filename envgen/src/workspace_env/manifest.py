from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# Single source of truth for the two condition vocabularies.  The collection-map
# depth and the viewer observation level are separate interventions; keeping the
# literals here (instead of repeating them per configuration model) makes every
# derived choice list, branch, and test read from the same tuple.
IndexLevel = Literal[
    "no_collection_map",
    "task_input_anchored_map",
    "persona_union_map",
    "workspace_snapshot_map",
    "task_suite_oracle_map",
]
INDEX_LEVELS: tuple[str, ...] = get_args(IndexLevel)

ViewLevel = Literal[
    "shell_only",
    "text_view",
    "text_structure_view",
    "text_structure_image_view",
    "specialized_viewers",
    "redundant_text_view",
]
VIEW_LEVELS: tuple[str, ...] = get_args(ViewLevel)


class WorkspaceConfig(StrictModel):
    input_root: str
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_root: str


class DataConfig(StrictModel):
    """Private, materialized workspace-search condition state.

    ``task_input_anchored_map`` is the explicit task-input-anchored development
    intervention; ``persona_union_map`` adds a separately staged
    task-provenance-free persona union; ``workspace_snapshot_map`` is the
    task-independent workspace-snapshot alternative; ``task_suite_oracle_map`` is
    its separately reported task-suite-conditioned upper bound. These conditions
    stage their artifacts privately and must be reported separately.
    """

    index_level: IndexLevel = "no_collection_map"
    relation_graph: Literal[False, True] = False
    semantic_retrieval: Literal[False] = False
    workspace_search_backend: Literal["empty", "collection_map", "persona_collection_map", "workspace_collection_map"] = "empty"
    collection_set_path: str | None = None
    collection_set_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    collection_search_index_path: str | None = None
    collection_search_index_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    persona_collection_index_public_path: str | None = None
    persona_collection_index_public_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    persona_collection_search_index_path: str | None = None
    persona_collection_search_index_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    workspace_collection_set_path: str | None = None
    workspace_collection_set_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    workspace_collection_search_index_path: str | None = None
    workspace_collection_search_index_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    task_start_briefing: Literal["disabled", "collection_map"] = "disabled"

    @model_validator(mode="after")
    def validate_index_backend(self) -> "DataConfig":
        task_scoped = (
            self.collection_set_path,
            self.collection_set_sha256,
            self.collection_search_index_path,
            self.collection_search_index_sha256,
        )
        workspace_scoped = (
            self.workspace_collection_set_path,
            self.workspace_collection_set_sha256,
            self.workspace_collection_search_index_path,
            self.workspace_collection_search_index_sha256,
        )
        persona_scoped = (
            self.persona_collection_index_public_path,
            self.persona_collection_index_public_sha256,
            self.persona_collection_search_index_path,
            self.persona_collection_search_index_sha256,
        )
        if self.index_level == "no_collection_map":
            if (
                self.workspace_search_backend != "empty"
                or self.relation_graph is not False
                or self.task_start_briefing != "disabled"
                or any(value is not None for value in task_scoped + persona_scoped + workspace_scoped)
            ):
                raise ValueError("no_collection_map must use the stable empty workspace-search backend")
            return self
        if self.index_level == "task_input_anchored_map":
            if (
                self.workspace_search_backend != "collection_map"
                or self.relation_graph is not True
                or self.task_start_briefing != "collection_map"
                or any(value is None for value in task_scoped)
                or any(value is not None for value in persona_scoped + workspace_scoped)
            ):
                raise ValueError("task_input_anchored_map requires one staged task-input collection map and its search index")
            return self
        if self.index_level == "persona_union_map":
            if (
                self.workspace_search_backend != "persona_collection_map"
                or self.relation_graph is not True
                or self.task_start_briefing != "collection_map"
                or any(value is None for value in task_scoped + persona_scoped)
                or any(value is not None for value in workspace_scoped)
            ):
                raise ValueError("persona_union_map requires a task map plus a staged persona collection index")
            return self
        if (
            self.workspace_search_backend != "workspace_collection_map"
            or self.relation_graph is not True
            or self.task_start_briefing != "disabled"
            or any(value is not None for value in task_scoped + persona_scoped)
            or any(value is None for value in workspace_scoped)
        ):
            raise ValueError(
                "workspace_snapshot_map/task_suite_oracle_map requires one staged workspace collection map and its search index"
            )
        return self


class ContextConfig(StrictModel):
    event_delete_rate: Literal[1.0, 0.7, 0.5, 0.3, 0.1, 0.0] = 1.0
    deletion_seed: int = 0
    deletion_unit: Literal["event"] = "event"
    visible_event_log_path: str | None = None
    visible_event_log_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    event_hook_mode: Literal["disabled", "post_tool_use"] = "disabled"


class ToolConfig(StrictModel):
    view_level: ViewLevel
    text_token_cap: int = Field(ge=128, le=65536)
    structure_token_cap: int = Field(ge=128, le=65536)
    cursor_token_cap: int = Field(ge=128, le=65536)
    image_max_units: int = Field(default=1, ge=1, le=16)
    image_max_pixels: int = Field(default=1_600_000, ge=65_536, le=16_000_000)
    image_max_side: int = Field(default=2_000, ge=256, le=8_192)
    image_max_tiles: int = Field(default=1, ge=1, le=16)
    thumbnail_max_units: int = Field(default=12, ge=1, le=12)
    large_cap_tokens: int = Field(default=65_536, ge=1_024, le=65_536)
    max_input_bytes: int = Field(default=536_870_912, ge=1_024)
    parser_timeout_seconds: int = Field(default=120, ge=1, le=3_600)
    ocr_timeout_seconds: int = Field(default=120, ge=1, le=3_600)
    renderer_timeout_seconds: int = Field(default=120, ge=1, le=3_600)
    workspace_search_token_cap: Literal[2048] = 2048

    @model_validator(mode="after")
    def validate_caps(self) -> "ToolConfig":
        for value in (self.text_token_cap, self.structure_token_cap, self.cursor_token_cap):
            if value > self.large_cap_tokens:
                raise ValueError("token caps must not exceed large_cap_tokens")
        if not (self.text_token_cap == self.structure_token_cap == self.cursor_token_cap):
            raise ValueError("text, structure, and cursor token caps must match in milestone 1")
        return self


class LoggingConfig(StrictModel):
    audit_path: str
    raw_audit_path: str


class ConditionManifest(StrictModel):
    schema_version: Literal[1]
    run_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    repetition_id: int = Field(ge=1)
    workspace: WorkspaceConfig
    data: DataConfig = Field(default_factory=DataConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: ToolConfig
    logging: LoggingConfig

    @model_validator(mode="after")
    def validate_roots(self) -> "ConditionManifest":
        workspace = Path(self.workspace.input_root).resolve()
        artifact = Path(self.workspace.artifact_root).resolve()
        try:
            artifact.relative_to(workspace)
        except ValueError:
            pass
        else:
            raise ValueError("artifact_root must be outside workspace input_root")
        for raw in (self.logging.audit_path, self.logging.raw_audit_path):
            target = Path(raw).resolve()
            try:
                target.relative_to(artifact)
            except ValueError as exc:
                raise ValueError("audit paths must be contained by artifact_root") from exc
        event_log = self.context.visible_event_log_path
        event_hash = self.context.visible_event_log_sha256
        if event_log is None:
            if event_hash is not None:
                raise ValueError("visible event-log hash requires a visible event-log path")
            if self.context.event_delete_rate != 1.0:
                raise ValueError("a visible event log is required unless event_delete_rate is 1.0")
        else:
            if event_hash is None:
                raise ValueError("visible event-log path requires a visible event-log hash")
            event_target = Path(event_log).resolve()
            try:
                event_target.relative_to(artifact)
            except ValueError as exc:
                raise ValueError("visible event log must be staged under artifact_root") from exc
        for raw in (
            self.data.collection_set_path,
            self.data.collection_search_index_path,
            self.data.persona_collection_index_public_path,
            self.data.persona_collection_search_index_path,
            self.data.workspace_collection_set_path,
            self.data.workspace_collection_search_index_path,
        ):
            if raw is None:
                continue
            target = Path(raw).resolve()
            try:
                target.relative_to(artifact)
            except ValueError as exc:
                raise ValueError("collection-map artefacts must be staged under artifact_root") from exc
        return self


class ResolvedManifest(StrictModel):
    manifest: ConditionManifest
    manifest_hash: str
    condition_hash: str
    tokenizer: dict[str, str]
    components: dict[str, str]
    truncation: dict[str, Any]
    image_layout: dict[str, Any]


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _command_version(command: list[str]) -> str:
    try:
        proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return (proc.stdout or proc.stderr or "unavailable").splitlines()[0].strip()


def _tesseract_languages() -> str:
    try:
        proc = subprocess.run(["tesseract", "--list-langs"], check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    lines = sorted(line.strip() for line in proc.stdout.splitlines()[1:] if line.strip())
    return ",".join(lines) or "unavailable"


def resolve_manifest(manifest: ConditionManifest) -> ResolvedManifest:
    dumped = manifest.model_dump(mode="json")
    condition = {
        "data": dumped["data"],
        "context": dumped["context"],
        "tools": dumped["tools"],
    }
    return ResolvedManifest(
        manifest=manifest,
        manifest_hash=sha256_text(canonical_json(dumped)),
        condition_hash=sha256_text(canonical_json(condition)),
        tokenizer={"name": "cl100k_base", "package": "tiktoken", "version": _package_version("tiktoken")},
        components={
            "python": platform.python_version(),
            "mcp": _package_version("mcp"),
            "pymupdf": _package_version("PyMuPDF"),
            "python-docx": _package_version("python-docx"),
            "openpyxl": _package_version("openpyxl"),
            "python-pptx": _package_version("python-pptx"),
            "pillow": _package_version("Pillow"),
            "libreoffice": _command_version(["libreoffice", "--version"]),
            "tesseract": _command_version(["tesseract", "--version"]),
            "tesseract_languages": _tesseract_languages(),
            "workspace_env": "9",
            "structure_schema": "workspace.structure.v1",
            "renderer_schema": "workspace.image.v1",
        },
        truncation={
            "head_ratio": 0.7,
            "text_marker": "\n[...TRUNCATED_MIDDLE; continue with next_cursor...]\n",
            "continuation_marker": "\n[...MORE_CONTINUATION_AVAILABLE...]\n",
            "cap_applies_to": "content",
        },
        image_layout={
            "thumbnail_columns": 4,
            "thumbnail_rows": 3,
            "thumbnail_max_units": 12,
            "thumbnail_cell_width": 320,
            "thumbnail_cell_height": 240,
            "thumbnail_label_height": 24,
            "origin": "top-left",
        },
    )


def load_manifest(path: str | os.PathLike[str]) -> ConditionManifest:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw) if str(path).lower().endswith(".json") else yaml.safe_load(raw)
    return ConditionManifest.model_validate(data)


def load_and_resolve_manifest(path: str | os.PathLike[str]) -> ResolvedManifest:
    return resolve_manifest(load_manifest(path))


def save_resolved_manifest(resolved: ResolvedManifest) -> Path:
    root = Path(resolved.manifest.workspace.artifact_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    target = root / "resolved_manifest.json"
    target.write_text(
        json.dumps(resolved.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    return target
