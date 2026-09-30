"""Strict private artifact schemas for the Codex A/B/C event-log loop.

These artifacts orchestrate synthesis and review.  They are never exposed by
``event_search`` and must not be mixed into the agent-visible event stream.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


WORKFLOW_SCHEMA_VERSION = 1
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
ID_BODY = r"[a-z][a-z0-9]{15,63}"
CandidateId = Annotated[str, Field(pattern=rf"^cand_{ID_BODY}$")]
EventId = Annotated[str, Field(pattern=rf"^evt_{ID_BODY}$")]


class StrictArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _relative_path(value: str) -> str:
    if not value or "\\" in value or value.startswith("/") or "\x00" in value:
        raise ValueError("artifact paths must be non-empty relative POSIX paths")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("artifact path escapes its declared root")
    return value


def _sorted_unique_paths(values: list[str]) -> list[str]:
    validated = [_relative_path(value) for value in values]
    if validated != sorted(set(validated)):
        raise ValueError("path lists must be sorted and unique")
    return validated


class CandidateTask(StrictArtifact):
    schema_version: Literal[WORKFLOW_SCHEMA_VERSION] = WORKFLOW_SCHEMA_VERSION
    artifact_type: Literal["candidate_task"] = "candidate_task"
    candidate_id: CandidateId
    candidate_index: int = Field(ge=1)
    attempt: int = Field(ge=1)
    mode: Literal["new", "repair"]
    previous_candidate_id: CandidateId | None = None
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=1_600)
    files_used: list[str] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
        description="POSIX paths relative to the Workspace root, sorted lexicographically and unique.",
    )
    events_path: str
    visible_workspace_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    author_run_id: str = Field(min_length=1, max_length=240)

    @field_validator("files_used")
    @classmethod
    def validate_files_used(cls, value: list[str]) -> list[str]:
        return _sorted_unique_paths(value)

    @field_validator("events_path")
    @classmethod
    def validate_events_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def validate_repair_link(self) -> "CandidateTask":
        if self.mode == "repair" and self.previous_candidate_id is None:
            raise ValueError("repair candidates must identify the previous candidate")
        if self.mode == "new" and self.previous_candidate_id is not None:
            raise ValueError("new candidates must not identify a previous candidate")
        return self


class SemanticCheck(StrictArtifact):
    name: Literal[
        "schema_conformance",
        "file_consistency",
        "excerpt_fidelity",
        "action_semantics",
        "timeline_plausibility",
        "task_coherence",
    ]
    status: Literal["passed", "failed"]
    rationale: str = Field(min_length=1, max_length=1_600)


class ReviewIssue(StrictArtifact):
    category: Literal[
        "schema",
        "file_consistency",
        "excerpt_fidelity",
        "action_semantics",
        "timeline",
        "task_coherence",
    ]
    message: str = Field(min_length=1, max_length=1_600)
    suggestion: str = Field(min_length=1, max_length=1_600)
    event_ids: list[EventId] = Field(default_factory=list)
    paths: list[str] = Field(
        default_factory=list,
        json_schema_extra={"uniqueItems": True},
        description="Affected Workspace-relative POSIX paths, sorted lexicographically and unique.",
    )

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: list[str]) -> list[str]:
        return _sorted_unique_paths(value)


class ReviewResult(StrictArtifact):
    schema_version: Literal[WORKFLOW_SCHEMA_VERSION] = WORKFLOW_SCHEMA_VERSION
    artifact_type: Literal["review_result"] = "review_result"
    candidate_id: CandidateId
    attempt: int = Field(ge=1)
    verdict: Literal["PASS", "REVISE"]
    checks: list[SemanticCheck] = Field(min_length=6, max_length=6)
    issues: list[ReviewIssue] = Field(default_factory=list)
    reviewer_run_id: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_verdict(self) -> "ReviewResult":
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("review checks must contain each semantic check exactly once")
        failed = any(check.status == "failed" for check in self.checks)
        if self.verdict == "PASS" and (failed or self.issues):
            raise ValueError("PASS requires all checks to pass and no issues")
        if self.verdict == "REVISE" and (not failed or not self.issues):
            raise ValueError("REVISE requires a failed check and at least one actionable issue")
        return self


class MaskManifest(StrictArtifact):
    schema_version: Literal[WORKFLOW_SCHEMA_VERSION] = WORKFLOW_SCHEMA_VERSION
    artifact_type: Literal["mask_manifest"] = "mask_manifest"
    accepted_candidate_index: int = Field(ge=1)
    random_seed: int
    mask_rate: float = Field(gt=0, le=1)
    population_paths: list[str] = Field(json_schema_extra={"uniqueItems": True})
    newly_masked_paths: list[str] = Field(json_schema_extra={"uniqueItems": True})
    masked_paths: list[str] = Field(json_schema_extra={"uniqueItems": True})
    content_hash: str = Field(pattern=SHA256_PATTERN)

    @field_validator("population_paths", "newly_masked_paths", "masked_paths")
    @classmethod
    def validate_path_set(cls, value: list[str]) -> list[str]:
        return _sorted_unique_paths(value)

    @model_validator(mode="after")
    def validate_subsets(self) -> "MaskManifest":
        if not set(self.newly_masked_paths).issubset(self.population_paths):
            raise ValueError("newly masked paths must come from the sampling population")
        if not set(self.newly_masked_paths).issubset(self.masked_paths):
            raise ValueError("newly masked paths must be present in the cumulative mask")
        if self.population_paths and not self.newly_masked_paths:
            raise ValueError("a non-empty population must mask at least one path")
        return self


class TimelineEntry(StrictArtifact):
    input_event_id: EventId
    output_event_id: EventId
    canonical_sequence: int = Field(ge=1)
    original_occurred_at: datetime
    adjusted_occurred_at: datetime

    @field_validator("original_occurred_at", "adjusted_occurred_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timeline timestamps must be ISO 8601 values with timezones")
        return value.astimezone(timezone.utc)


class TimelineResult(StrictArtifact):
    schema_version: Literal[WORKFLOW_SCHEMA_VERSION] = WORKFLOW_SCHEMA_VERSION
    artifact_type: Literal["timeline_result"] = "timeline_result"
    status: Literal["TIMELINE_COMPLETE", "TIMELINE_BLOCKED"]
    accepted_candidate_ids: list[CandidateId] = Field(min_length=1)
    entries: list[TimelineEntry] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    editor_run_id: str = Field(min_length=1, max_length=240)

    @field_validator("accepted_candidate_ids")
    @classmethod
    def validate_candidate_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("accepted candidate IDs must be unique opaque candidate IDs")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> "TimelineResult":
        sequences = [entry.canonical_sequence for entry in self.entries]
        if sequences and sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("timeline canonical_sequence must be contiguous")
        adjusted_times = [entry.adjusted_occurred_at for entry in self.entries]
        if adjusted_times != sorted(adjusted_times):
            raise ValueError("adjusted timeline timestamps must not go backwards")
        if len({entry.input_event_id for entry in self.entries}) != len(self.entries):
            raise ValueError("timeline input event IDs must be unique")
        if len({entry.output_event_id for entry in self.entries}) != len(self.entries):
            raise ValueError("timeline output event IDs must be unique")
        if self.status == "TIMELINE_COMPLETE" and (not self.entries or self.blocking_issues):
            raise ValueError("a complete timeline requires entries and no blocking issues")
        if self.status == "TIMELINE_BLOCKED" and not self.blocking_issues:
            raise ValueError("a blocked timeline requires blocking issues")
        return self


WorkflowArtifact = Annotated[
    CandidateTask | ReviewResult | MaskManifest | TimelineResult,
    Field(discriminator="artifact_type"),
]
WORKFLOW_ARTIFACT_ADAPTER = TypeAdapter(WorkflowArtifact)


def workflow_artifact_json_schema() -> dict[str, Any]:
    schema = WORKFLOW_ARTIFACT_ADAPTER.json_schema(ref_template="#/$defs/{model}")
    schema["$id"] = "https://workspace-bench.local/schema/context-event-log-workflow-v1.json"
    schema["title"] = "Workspace-Bench Context Event Log Workflow Artifacts v1"
    schema["description"] = (
        "Private Codex A/B/C orchestration artifacts. These records are not part of the agent-visible event log."
    )
    return schema
