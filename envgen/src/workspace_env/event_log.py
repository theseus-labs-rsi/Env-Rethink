"""Validated, condition-blind workspace history events.

The public event stream is intentionally smaller than its private audit record:
it contains no canonical ordinal, deletion metadata, task prompt, rubric label,
or content hash.  This lets a history-deletion condition vary visible evidence
without exposing the condition itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Annotated, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


EVENT_LOG_SCHEMA_VERSION = 2
MAX_PUBLIC_TEXT_CHARS = 1_600
MAX_PUBLIC_LABEL_CHARS = 240
OPAQUE_ID_BODY = r"[a-z][a-z0-9]{15,63}"
FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "canonical_sequence",
        "condition_id",
        "deletion_rate",
        "deletion_seed",
        "event_delete_rate",
        "parent_event_id",
        "task_id",
        "task_label",
        "task_description",
        "rubric",
        "reference_answer",
        "relevance",
        "selection_reason",
        "file_label",
        "content_hash",
        "source_content_hash",
        "excerpt_hash",
        "private_url",
        "access_token",
        "api_key",
        "authorization",
    }
)
SENSITIVE_KEY_PARTS = ("secret", "token", "password", "credential", "cookie")
URL_PATTERN = re.compile(r"(?:https?|file)://", re.IGNORECASE)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EventAction(StrEnum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    FOLDER_CREATE = "folder.create"
    FOLDER_MOVE = "folder.move"
    FOLDER_RENAME = "folder.rename"
    FOLDER_DELETE = "folder.delete"
    FILE_DOWNLOAD = "file.download"
    FILE_IMPORT = "file.import"
    FILE_OPEN = "file.open"
    FILE_PREVIEW = "file.preview"
    FILE_READ = "file.read"
    FILE_CREATE = "file.create"
    FILE_WRITE = "file.write"
    FILE_SAVE = "file.save"
    FILE_COPY = "file.copy"
    FILE_MOVE = "file.move"
    FILE_RENAME = "file.rename"
    FILE_EXPORT = "file.export"
    FILE_EXTRACT = "file.extract"
    FILE_LINK = "file.link"
    FILE_DELETE = "file.delete"
    FILE_RESTORE = "file.restore"
    SHELL_COMMAND = "shell.command"


class LineLocator(StrictModel):
    kind: Literal["line"]
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> "LineLocator":
        if self.end < self.start:
            raise ValueError("locator end must not precede start")
        return self


class ParagraphLocator(LineLocator):
    kind: Literal["paragraph"]


class PageLocator(LineLocator):
    kind: Literal["page"]


class SlideLocator(LineLocator):
    kind: Literal["slide"]


class SheetRangeLocator(StrictModel):
    kind: Literal["sheet_range"]
    sheet: str = Field(min_length=1, max_length=255)
    range: str = Field(pattern=r"^[A-Za-z]{1,3}[1-9][0-9]*:[A-Za-z]{1,3}[1-9][0-9]*$")


class RegionLocator(StrictModel):
    kind: Literal["region"]
    page: int | None = Field(default=None, ge=1)
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)
    coordinate_space: Literal["points", "pixels"]


class TimeRangeLocator(StrictModel):
    kind: Literal["time_range"]
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> "TimeRangeLocator":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("time range end must follow start")
        return self


class WholeFileLocator(StrictModel):
    kind: Literal["whole_file"]


Locator = Annotated[
    LineLocator
    | ParagraphLocator
    | PageLocator
    | SlideLocator
    | SheetRangeLocator
    | RegionLocator
    | TimeRangeLocator
    | WholeFileLocator,
    Field(discriminator="kind"),
]
LOCATOR_ADAPTER = TypeAdapter(Locator)


class EventObject(StrictModel):
    object_id: str = Field(pattern=rf"^obj_{OPAQUE_ID_BODY}$")
    path_at_event: str = Field(min_length=1, max_length=1_024)
    mime_type: str | None = Field(default=None, max_length=255)
    revision_id: str | None = Field(default=None, pattern=rf"^rev_{OPAQUE_ID_BODY}$")

    @field_validator("path_at_event")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if "\\" in value or value.startswith("/") or "\x00" in value:
            raise ValueError("event object path must be a relative POSIX path")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("event object path escapes its workspace")
        return value


class PublicProvenance(StrictModel):
    synthetic: bool
    generation_method: Literal["agent_inference", "snapshot_observation", "trace_conversion"]
    content_basis: Literal[
        "workspace_content",
        "workspace_metadata",
        "trace_record",
        "agent_inference",
        "not_applicable",
    ]
    transition_basis: Literal["trace_record", "agent_inference", "synthetic_transition", "not_applicable"]
    temporal_basis: Literal["observed_timestamp", "filesystem_timestamp", "synthetic_timestamp"]

    @model_validator(mode="after")
    def validate_generation_method(self) -> "PublicProvenance":
        expected_synthetic = self.generation_method in {"agent_inference", "snapshot_observation"}
        if self.synthetic != expected_synthetic:
            raise ValueError("synthetic must agree with the provenance generation_method")
        if self.generation_method == "snapshot_observation" and (
            self.content_basis != "workspace_metadata"
            or self.transition_basis != "not_applicable"
            or self.temporal_basis != "synthetic_timestamp"
        ):
            raise ValueError("snapshot observations require metadata-only synthetic provenance")
        if self.generation_method == "trace_conversion" and (
            self.content_basis != "trace_record"
            or self.transition_basis != "trace_record"
            or self.temporal_basis != "observed_timestamp"
        ):
            raise ValueError("trace conversion requires directly recorded provenance")
        if self.generation_method == "agent_inference" and self.transition_basis != "agent_inference":
            raise ValueError("agent-inferred history must label its inferred transitions")
        return self


class SessionStartPayload(StrictModel):
    application_context: list[str] = Field(min_length=1, max_length=8)
    title: str | None = Field(default=None, max_length=MAX_PUBLIC_LABEL_CHARS)
    narrative: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)

    @field_validator("application_context")
    @classmethod
    def validate_application_context(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > MAX_PUBLIC_LABEL_CHARS for item in value):
            raise ValueError("application_context entries must be bounded non-empty labels")
        return value


class SessionEndPayload(StrictModel):
    status: Literal["completed", "abandoned", "interrupted"]
    duration_seconds: int = Field(ge=0)


class FolderCreatePayload(StrictModel):
    path: str


class FolderMovePayload(StrictModel):
    source_path: str
    destination_path: str


class FolderRenamePayload(StrictModel):
    path_before: str
    path_after: str


class FolderDeletePayload(StrictModel):
    path_before: str
    deletion_kind: Literal["trash", "permanent"]


class FileDownloadPayload(StrictModel):
    source_kind: Literal["web", "email_attachment", "shared_drive", "other"]
    source_label: str | None = Field(default=None, max_length=MAX_PUBLIC_LABEL_CHARS)
    destination_path: str


class FileImportPayload(StrictModel):
    source_kind: Literal["email_attachment", "shared_drive", "local_transfer", "generated_by_application", "other"]
    source_label: str | None = Field(default=None, max_length=MAX_PUBLIC_LABEL_CHARS)


class FileOpenPayload(StrictModel):
    application: str = Field(min_length=1, max_length=MAX_PUBLIC_LABEL_CHARS)
    open_mode: Literal["read_only", "read_write"] | None = None
    observation: Literal["metadata_only", "content_available"] | None = None
    size_bytes: int | None = Field(default=None, ge=0)


class FilePreviewPayload(StrictModel):
    locator: Locator
    observed_excerpt: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)


class FileReadPayload(StrictModel):
    locator: Locator
    excerpt: str = Field(max_length=MAX_PUBLIC_TEXT_CHARS)
    purpose: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)
    observation: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)


class FileCreatePayload(StrictModel):
    creation_method: str = Field(min_length=1, max_length=MAX_PUBLIC_LABEL_CHARS)
    initial_excerpt: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)


class FileWritePayload(StrictModel):
    write_mode: Literal["insert", "append", "replace", "overwrite"]
    locator: Locator
    before_excerpt: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)
    after_excerpt: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)
    diff_excerpt: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)
    summary: str | None = Field(default=None, max_length=MAX_PUBLIC_TEXT_CHARS)

    @model_validator(mode="after")
    def validate_written_content(self) -> "FileWritePayload":
        if self.after_excerpt is None and self.diff_excerpt is None:
            raise ValueError("file.write needs after_excerpt or diff_excerpt")
        return self


class FileSavePayload(StrictModel):
    application: str = Field(min_length=1, max_length=MAX_PUBLIC_LABEL_CHARS)


class FileCopyPayload(StrictModel):
    source_object_id: str = Field(pattern=rf"^obj_{OPAQUE_ID_BODY}$")
    destination_object_id: str = Field(pattern=rf"^obj_{OPAQUE_ID_BODY}$")
    source_path: str
    destination_path: str


class FileMovePayload(StrictModel):
    source_path: str
    destination_path: str


class FileRenamePayload(StrictModel):
    path_before: str
    path_after: str


class FileExportPayload(StrictModel):
    source_object_id: str = Field(pattern=rf"^obj_{OPAQUE_ID_BODY}$")
    destination_object_id: str = Field(pattern=rf"^obj_{OPAQUE_ID_BODY}$")
    destination_path: str
    format: str = Field(min_length=1, max_length=MAX_PUBLIC_LABEL_CHARS)


class FileExtractPayload(StrictModel):
    source_object_id: str = Field(pattern=rf"^obj_{OPAQUE_ID_BODY}$")
    destination_directory: str


class FileLinkPayload(StrictModel):
    target_object_id: str = Field(pattern=rf"^obj_{OPAQUE_ID_BODY}$")
    link_path: str


class FileDeletePayload(StrictModel):
    path_before: str
    deletion_kind: Literal["trash", "permanent"]


class FileRestorePayload(StrictModel):
    restore_path: str


class ShellCommandPayload(StrictModel):
    command: str = Field(min_length=1, max_length=MAX_PUBLIC_TEXT_CHARS)
    status: Literal["completed", "failed", "declined"]
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)


PAYLOAD_MODELS: dict[EventAction, type[StrictModel]] = {
    EventAction.SESSION_START: SessionStartPayload,
    EventAction.SESSION_END: SessionEndPayload,
    EventAction.FOLDER_CREATE: FolderCreatePayload,
    EventAction.FOLDER_MOVE: FolderMovePayload,
    EventAction.FOLDER_RENAME: FolderRenamePayload,
    EventAction.FOLDER_DELETE: FolderDeletePayload,
    EventAction.FILE_DOWNLOAD: FileDownloadPayload,
    EventAction.FILE_IMPORT: FileImportPayload,
    EventAction.FILE_OPEN: FileOpenPayload,
    EventAction.FILE_PREVIEW: FilePreviewPayload,
    EventAction.FILE_READ: FileReadPayload,
    EventAction.FILE_CREATE: FileCreatePayload,
    EventAction.FILE_WRITE: FileWritePayload,
    EventAction.FILE_SAVE: FileSavePayload,
    EventAction.FILE_COPY: FileCopyPayload,
    EventAction.FILE_MOVE: FileMovePayload,
    EventAction.FILE_RENAME: FileRenamePayload,
    EventAction.FILE_EXPORT: FileExportPayload,
    EventAction.FILE_EXTRACT: FileExtractPayload,
    EventAction.FILE_LINK: FileLinkPayload,
    EventAction.FILE_DELETE: FileDeletePayload,
    EventAction.FILE_RESTORE: FileRestorePayload,
    EventAction.SHELL_COMMAND: ShellCommandPayload,
}


def _validate_json(value: Any) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("event payload must be JSON serializable") from exc


def _walk_public_payload(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key, nested
            yield from _walk_public_payload(nested)
    elif isinstance(value, list):
        for nested in value:
            yield None, nested
            yield from _walk_public_payload(nested)


def _validate_relative_path(value: str) -> None:
    if "\\" in value or value.startswith("/") or "\x00" in value:
        raise ValueError("event payload path must be a relative POSIX path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("event payload path escapes its workspace")


def _validate_payload_paths(payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        if key in {"path", "path_before", "path_after", "destination_directory"} or key.endswith("_path"):
            if not isinstance(value, str):
                raise ValueError(f"event payload field {key} must be a path string")
            _validate_relative_path(value)


def _validate_action_payload(action: EventAction, payload: dict[str, Any]) -> None:
    _validate_payload_paths(payload)
    PAYLOAD_MODELS[action].model_validate(payload)


class PublicEvent(StrictModel):
    schema_version: Literal[EVENT_LOG_SCHEMA_VERSION] = EVENT_LOG_SCHEMA_VERSION
    event_id: str = Field(pattern=rf"^evt_{OPAQUE_ID_BODY}$")
    occurred_at: datetime
    workspace_id: str = Field(pattern=rf"^wrk_{OPAQUE_ID_BODY}$")
    session_id: str = Field(pattern=rf"^ses_{OPAQUE_ID_BODY}$")
    actor: Literal["workspace_owner", "workspace_collaborator", "workspace_automation", "codex_agent"]
    action: EventAction
    object: EventObject | None = None
    payload: dict[str, Any]
    provenance: PublicProvenance

    @field_validator("action", mode="before")
    @classmethod
    def parse_action(cls, value: EventAction | str) -> EventAction:
        if isinstance(value, EventAction):
            return value
        try:
            return EventAction(value)
        except ValueError as exc:
            raise ValueError("action is not part of the closed event taxonomy") from exc

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("occurred_at must be ISO 8601") from exc
        if not isinstance(value, datetime):
            raise ValueError("occurred_at must be an ISO 8601 timestamp")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json(value)
        for key, nested in _walk_public_payload(value):
            if key is not None:
                lowered = key.lower()
                if lowered in FORBIDDEN_PUBLIC_KEYS or any(part in lowered for part in SENSITIVE_KEY_PARTS):
                    raise ValueError(f"public event payload contains a forbidden key: {key}")
            if isinstance(nested, str):
                if len(nested) > MAX_PUBLIC_TEXT_CHARS:
                    raise ValueError("public event payload contains unbounded text")
                if URL_PATTERN.search(nested):
                    raise ValueError("public event payload must not contain a URL")
        return value

    @model_validator(mode="after")
    def validate_action(self) -> "PublicEvent":
        if self.action not in {
            EventAction.SESSION_START,
            EventAction.SESSION_END,
            EventAction.FOLDER_CREATE,
            EventAction.FOLDER_MOVE,
            EventAction.FOLDER_RENAME,
            EventAction.FOLDER_DELETE,
            EventAction.SHELL_COMMAND,
        } and self.object is None:
            raise ValueError("file actions require an object")
        _validate_action_payload(self.action, self.payload)
        return self


class CausalLink(StrictModel):
    event_id: str = Field(pattern=rf"^evt_{OPAQUE_ID_BODY}$")
    relation: Literal["created_from", "derived_from", "informed_by"]


class CanonicalEvent(StrictModel):
    """Private audit record.  Never serialize this model to an agent response."""

    event: PublicEvent
    canonical_sequence: int = Field(ge=1)
    causal_links: list[CausalLink] = Field(default_factory=list)
    source_content_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_metadata_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_trace_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    excerpt_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    generator_version: str = Field(min_length=1, max_length=128)
    validator_status: Literal["passed"]


class VisibleLogAudit(StrictModel):
    """Private provenance emitted beside, but never inside, the visible JSONL."""

    schema_version: Literal[EVENT_LOG_SCHEMA_VERSION] = EVENT_LOG_SCHEMA_VERSION
    source_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_fingerprint_kind: Literal["content_inventory_v1", "metadata_inventory_v1"]
    canonical_log_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    visible_log_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    deletion_seed: int
    deletion_rate: float = Field(ge=0, le=1)
    generator_version: str = Field(min_length=1, max_length=128)
    validator_version: str = Field(min_length=1, max_length=128)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def opaque_id(kind: Literal["evt", "obj", "rev", "ses", "wrk"], stable_material: str) -> str:
    """Return a stable non-sequential public identifier from private material."""
    digest = hashlib.sha256(f"workspace-event-v2:{kind}:{stable_material}".encode("utf-8")).hexdigest()
    return f"{kind}_{kind[0]}{digest[:20]}"


def public_event_json_schema() -> dict[str, Any]:
    """Return the public v2 JSON Schema with strict action-specific payloads."""
    schema = PublicEvent.model_json_schema()
    definitions = schema.setdefault("$defs", {})
    definitions["PublicProvenance"]["allOf"] = [
        {
            "if": {"properties": {"generation_method": {"const": "agent_inference"}}},
            "then": {
                "properties": {
                    "synthetic": {"const": True},
                    "transition_basis": {"const": "agent_inference"},
                }
            },
        },
        {
            "if": {"properties": {"generation_method": {"const": "snapshot_observation"}}},
            "then": {
                "properties": {
                    "synthetic": {"const": True},
                    "content_basis": {"const": "workspace_metadata"},
                    "transition_basis": {"const": "not_applicable"},
                    "temporal_basis": {"const": "synthetic_timestamp"},
                }
            },
        },
        {
            "if": {"properties": {"generation_method": {"const": "trace_conversion"}}},
            "then": {
                "properties": {
                    "synthetic": {"const": False},
                    "content_basis": {"const": "trace_record"},
                    "transition_basis": {"const": "trace_record"},
                    "temporal_basis": {"const": "observed_timestamp"},
                }
            },
        },
    ]
    locator_schema = LOCATOR_ADAPTER.json_schema(ref_template="#/$defs/{model}")
    definitions.update(locator_schema.pop("$defs", {}))
    definitions["ContentLocator"] = locator_schema
    actions_without_object = {
        EventAction.SESSION_START,
        EventAction.SESSION_END,
        EventAction.FOLDER_CREATE,
        EventAction.FOLDER_MOVE,
        EventAction.FOLDER_RENAME,
        EventAction.FOLDER_DELETE,
        EventAction.SHELL_COMMAND,
    }
    conditions: list[dict[str, Any]] = []
    for action, payload_model in PAYLOAD_MODELS.items():
        payload_schema = payload_model.model_json_schema(ref_template="#/$defs/{model}")
        definitions.update(payload_schema.pop("$defs", {}))
        payload_schema["propertyNames"] = {"not": {"enum": sorted(FORBIDDEN_PUBLIC_KEYS)}}
        if action == EventAction.FILE_WRITE:
            payload_schema["anyOf"] = [
                {
                    "required": ["after_excerpt"],
                    "properties": {"after_excerpt": {"type": "string"}},
                },
                {
                    "required": ["diff_excerpt"],
                    "properties": {"diff_excerpt": {"type": "string"}},
                },
            ]
        then: dict[str, Any] = {"properties": {"payload": payload_schema}}
        if action not in actions_without_object:
            then["required"] = ["object"]
            then["properties"]["object"] = {"not": {"type": "null"}}
        conditions.append({"if": {"properties": {"action": {"const": action.value}}}, "then": then})
    schema["allOf"] = conditions
    return schema


def canonical_event_json_schema() -> dict[str, Any]:
    """Return the private canonical JSONL-row schema with the strict public event embedded."""
    schema = CanonicalEvent.model_json_schema(ref_template="#/$defs/{model}")
    definitions = schema.setdefault("$defs", {})
    public_schema = public_event_json_schema()
    definitions.update(public_schema.pop("$defs", {}))
    definitions["PublicEvent"] = public_schema
    schema["$id"] = "https://workspace-bench.local/schema/context-event-log-canonical-v2.json"
    schema["title"] = "Workspace-Bench Canonical Context Event v2"
    schema["description"] = "Private canonical JSONL row; never expose this record through event_search."
    return schema


def validate_visible_events(events: Iterable[PublicEvent | dict[str, Any]]) -> list[PublicEvent]:
    """Validate a whole agent-visible stream and its deterministic ordering."""
    validated = [
        PublicEvent.model_validate(event.model_dump(mode="python")) if isinstance(event, PublicEvent) else PublicEvent.model_validate(event)
        for event in events
    ]
    event_ids = [event.event_id for event in validated]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("visible event IDs must be unique")
    workspaces = {event.workspace_id for event in validated}
    if len(workspaces) > 1:
        raise ValueError("a visible event log must contain one workspace")
    expected = sorted(validated, key=lambda event: (event.occurred_at, event.event_id))
    if validated != expected:
        raise ValueError("visible events must be sorted by occurred_at and opaque event_id")
    return validated


def validate_canonical_events(events: Iterable[CanonicalEvent | dict[str, Any]]) -> list[CanonicalEvent]:
    validated = [
        CanonicalEvent.model_validate(event.model_dump(mode="python"))
        if isinstance(event, CanonicalEvent)
        else CanonicalEvent.model_validate(event)
        for event in events
    ]
    visible = validate_visible_events([event.event for event in validated])
    if len(validated) != len(visible):
        raise ValueError("canonical event validation failed")
    expected_sequences = list(range(1, len(validated) + 1))
    if [event.canonical_sequence for event in validated] != expected_sequences:
        raise ValueError("canonical sequences must be contiguous and private")
    known_sequences = {event.event.event_id: event.canonical_sequence for event in validated}
    for event in validated:
        if event.event.event_id in {link.event_id for link in event.causal_links}:
            raise ValueError("an event must not cause itself")
        if not {link.event_id for link in event.causal_links}.issubset(known_sequences):
            raise ValueError("causal links must reference canonical events")
        if any(known_sequences[link.event_id] >= event.canonical_sequence for link in event.causal_links):
            raise ValueError("causal links must point to an earlier canonical event")
    return validated


def select_visible_events(
    canonical_events: Iterable[CanonicalEvent | dict[str, Any]],
    *,
    deletion_rate: float,
    deletion_seed: int,
) -> list[PublicEvent]:
    """Deterministically select a condition's events without leaking its metadata.

    The selected public events retain no canonical sequence, causal edge, seed,
    or rate.  Their only order is stable chronology plus opaque event ID.
    """
    if not 0 <= deletion_rate <= 1:
        raise ValueError("deletion_rate must be between zero and one")
    canonical = validate_canonical_events(canonical_events)
    keep_count = round(len(canonical) * (1 - deletion_rate))
    ranked = sorted(
        canonical,
        key=lambda event: hashlib.sha256(f"{deletion_seed}:{event.event.event_id}".encode("utf-8")).digest(),
    )
    selected_ids = {event.event.event_id for event in ranked[:keep_count]}
    visible = [event.event for event in canonical if event.event.event_id in selected_ids]
    return validate_visible_events(sorted(visible, key=lambda event: (event.occurred_at, event.event_id)))


def build_visible_log_audit(
    canonical_events: Iterable[CanonicalEvent | dict[str, Any]],
    visible_events: Iterable[PublicEvent | dict[str, Any]],
    *,
    source_snapshot_hash: str,
    snapshot_fingerprint_kind: Literal["content_inventory_v1", "metadata_inventory_v1"] = "content_inventory_v1",
    deletion_seed: int,
    deletion_rate: float,
    generator_version: str,
    validator_version: str,
) -> VisibleLogAudit:
    canonical = validate_canonical_events(canonical_events)
    visible = validate_visible_events(visible_events)
    return VisibleLogAudit(
        source_snapshot_hash=source_snapshot_hash,
        snapshot_fingerprint_kind=snapshot_fingerprint_kind,
        canonical_log_hash=_hash_json([event.model_dump(mode="json") for event in canonical]),
        visible_log_hash=_hash_json([event.model_dump(mode="json") for event in visible]),
        deletion_seed=deletion_seed,
        deletion_rate=deletion_rate,
        generator_version=generator_version,
        validator_version=validator_version,
    )
