"""Real Codex A/B/C orchestration for inferred workspace event logs.

The orchestrator does not invent tasks, events, reviews, or timeline decisions.
Those semantic artifacts must be written by real Codex runs.  Deterministic code
is limited to isolation, file masking, schema checks, state transitions, and
private audit material.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .event_log import (
    CanonicalEvent,
    EventAction,
    PublicEvent,
    opaque_id,
    validate_canonical_events,
    validate_visible_events,
)
from .event_log_workflow import CandidateTask, MaskManifest, ReviewResult, TimelineResult
from .integration import workspace_snapshot_hash


SYNTHESIS_SCHEMA_VERSION = 1
SYNTHESIS_ORCHESTRATOR_VERSION = "codex-event-synthesis-v24"
RESUMABLE_ORCHESTRATOR_VERSIONS = {
    "codex-event-synthesis-v15",
    "codex-event-synthesis-v16",
    "codex-event-synthesis-v17",
    "codex-event-synthesis-v18",
    "codex-event-synthesis-v19",
    "codex-event-synthesis-v20",
    "codex-event-synthesis-v21",
    "codex-event-synthesis-v22",
    SYNTHESIS_ORCHESTRATOR_VERSION,
}
DEFAULT_SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "docs"


class EventSynthesisError(RuntimeError):
    """Raised when a run cannot continue without compromising its auditability."""


class CodexRoleError(EventSynthesisError):
    """Raised when a Codex role returns a non-success execution status."""

    def __init__(self, *, agent_id: str, status: str, message: str | None) -> None:
        self.agent_id = agent_id
        self.status = status
        self.message = message
        super().__init__(f"{agent_id} failed with status={status}: {message}")


class InterferenceBridge(BaseModel):
    """Private file-selection anchors for one synthetic historical investigation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    bridge_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    distractor_files: list[str] = Field(min_length=1)
    correct_files: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_paths(self) -> "InterferenceBridge":
        for field_name, paths in (
            ("distractor_files", self.distractor_files),
            ("correct_files", self.correct_files),
        ):
            if any(not path.strip() for path in paths):
                raise ValueError(f"{field_name} must not contain blank paths")
            if len(paths) != len(set(paths)):
                raise ValueError(f"{field_name} must not contain duplicates")
        overlap = sorted(set(self.distractor_files) & set(self.correct_files))
        if overlap:
            raise ValueError(
                "distractor_files and correct_files must be disjoint: "
                + ", ".join(overlap)
            )
        return self


class SynthesisRunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[SYNTHESIS_SCHEMA_VERSION] = SYNTHESIS_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    repetition_id: int = Field(ge=1)
    workspace_root: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=240)
    auth_mode: Literal["chatgpt", "api"] = "chatgpt"
    base_url: str | None = None
    expected_codex_version: Literal["0.144.5"] = "0.144.5"
    timeout_seconds: float = Field(default=600.0, gt=0)
    workspace_view_mode: Literal["isolated_masked_copy", "shared_readonly_unmasked"] = (
        "isolated_masked_copy"
    )
    shared_workspace_snapshot_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    mask_rate: float = Field(default=0.5, gt=0, le=1)
    target_file_coverage: float = Field(default=0.1, gt=0, le=1)
    max_b_failures_per_candidate: int = Field(default=2, ge=1)
    max_candidate_slots: int = Field(default=20, ge=1)
    random_seed: int = 0
    construction_mode: Literal[
        "workspace_inference", "rubric_context", "interference_bridge"
    ] = "workspace_inference"
    selection_policy: Literal["untargeted", "targeted_diagnostic"] = "untargeted"
    preferred_files: list[str] = Field(default_factory=list)
    rubric_contexts: list[str] = Field(default_factory=list)
    interference_bridges: list[InterferenceBridge] = Field(default_factory=list)
    targeted_task_ids: list[str] = Field(default_factory=list)
    base_natural_event_log: str | None = None
    stop_after_accepted_candidates: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_provider(self) -> "SynthesisRunConfig":
        if self.auth_mode == "api" and not self.base_url:
            raise ValueError("api auth_mode requires base_url; the API key must come from the environment")
        if self.auth_mode == "chatgpt" and self.base_url is not None:
            raise ValueError("chatgpt auth_mode must not set base_url")
        if self.preferred_files and self.selection_policy != "targeted_diagnostic":
            raise ValueError("preferred_files require selection_policy=targeted_diagnostic")
        if self.selection_policy == "targeted_diagnostic" and not self.preferred_files:
            raise ValueError("targeted_diagnostic selection requires preferred_files")
        if len(self.preferred_files) != len(set(self.preferred_files)):
            raise ValueError("preferred_files must not contain duplicates")
        if any(not context.strip() for context in self.rubric_contexts):
            raise ValueError("rubric_contexts must not contain blank values")
        if len({bridge.bridge_id for bridge in self.interference_bridges}) != len(
            self.interference_bridges
        ):
            raise ValueError("interference bridge IDs must not repeat")
        if any(not task_id.strip() for task_id in self.targeted_task_ids):
            raise ValueError("targeted_task_ids must not contain blank values")
        if len(self.targeted_task_ids) != len(set(self.targeted_task_ids)):
            raise ValueError("targeted_task_ids must not contain duplicates")
        if self.rubric_contexts and self.interference_bridges:
            raise ValueError(
                "rubric_contexts and interference_bridges are separate interventions"
            )
        if self.construction_mode == "workspace_inference":
            # Preserve old rubric configs while making the selected mode
            # explicit in the stored private run configuration.
            if self.rubric_contexts:
                self.construction_mode = "rubric_context"
            elif self.interference_bridges:
                self.construction_mode = "interference_bridge"
        if self.construction_mode == "rubric_context" and not self.rubric_contexts:
            raise ValueError("rubric_context mode requires private rubric_contexts")
        if self.construction_mode == "interference_bridge" and not self.interference_bridges:
            raise ValueError(
                "interference_bridge mode requires private interference_bridges"
            )
        if self.construction_mode == "interference_bridge" and not self.targeted_task_ids:
            raise ValueError(
                "interference_bridge mode requires private targeted_task_ids for audit"
            )
        if self.construction_mode == "workspace_inference" and self.targeted_task_ids:
            raise ValueError(
                "targeted_task_ids are only valid for targeted-context modes"
            )
        if (
            self.construction_mode == "workspace_inference"
            and self.base_natural_event_log is not None
        ):
            raise ValueError(
                "base_natural_event_log is only valid for targeted-context modes"
            )
        if self.construction_mode == "workspace_inference" and (
            self.rubric_contexts or self.interference_bridges
        ):
            raise ValueError("workspace_inference mode cannot contain targeted context inputs")
        if self.construction_mode != "rubric_context" and self.rubric_contexts:
            raise ValueError("rubric_contexts require construction_mode=rubric_context")
        if self.construction_mode != "interference_bridge" and self.interference_bridges:
            raise ValueError(
                "interference_bridges require construction_mode=interference_bridge"
            )
        if self.workspace_view_mode == "shared_readonly_unmasked":
            if self.construction_mode == "workspace_inference":
                raise ValueError(
                    "shared_readonly_unmasked requires a private targeted-context mode"
                )
            if self.shared_workspace_snapshot_hash is None:
                raise ValueError(
                    "shared_readonly_unmasked requires a batch-precomputed shared_workspace_snapshot_hash"
                )
        elif self.shared_workspace_snapshot_hash is not None:
            raise ValueError(
                "shared_workspace_snapshot_hash is only supported by shared_readonly_unmasked"
            )
        # Reviewed rubric bundles can legitimately retain repeated wording for
        # distinct rubric entries. Preserve that private input cardinality for
        # Codex A rather than rejecting an otherwise valid latest bundle.
        if self.construction_mode != "workspace_inference" and self.stop_after_accepted_candidates is None:
            self.stop_after_accepted_candidates = 1
        if (
            self.stop_after_accepted_candidates is not None
            and self.stop_after_accepted_candidates > self.max_candidate_slots
        ):
            raise ValueError("stop_after_accepted_candidates must not exceed max_candidate_slots")
        return self


CodexRunner = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate_index: int
    attempt: int
    candidate_id: str
    task: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    artifact_root: Path


@dataclass(frozen=True, slots=True)
class SchemaContracts:
    public: Draft202012Validator
    canonical: Draft202012Validator
    workflow: Draft202012Validator
    files: tuple[Path, ...]

    @classmethod
    def load(cls, schema_root: Path) -> "SchemaContracts":
        paths = (
            schema_root / "context-event-log-schema.json",
            schema_root / "context-event-log-canonical-schema.json",
            schema_root / "context-event-log-workflow-schema.json",
        )
        schemas: list[dict[str, Any]] = []
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EventSynthesisError(f"cannot load schema contract {path}: {exc}") from exc
            Draft202012Validator.check_schema(value)
            schemas.append(value)
        return cls(
            public=Draft202012Validator(schemas[0]),
            canonical=Draft202012Validator(schemas[1]),
            workflow=Draft202012Validator(schemas[2]),
            files=paths,
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _opaque_candidate_id(run_id: str, candidate_index: int, attempt: int) -> str:
    digest = hashlib.sha256(f"{run_id}:{candidate_index}:{attempt}".encode("utf-8")).hexdigest()
    return "cand_c" + digest[:20]


def _write_json(path: Path, value: Any, *, private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600 if private else 0o644)
    os.replace(temporary, path)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]], *, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(_canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    os.chmod(temporary, 0o600 if private else 0o644)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventSynthesisError(f"invalid or missing JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EventSynthesisError(f"JSON artifact must be an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EventSynthesisError(f"cannot read JSONL artifact {path}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventSynthesisError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise EventSynthesisError(f"JSONL row must be an object at {path}:{line_number}")
        rows.append(row)
    if not rows:
        raise EventSynthesisError(f"JSONL artifact contains no events: {path}")
    return rows


def _schema_errors(validator: Draft202012Validator, value: Any) -> list[str]:
    errors = sorted(
        validator.iter_errors(value), key=lambda error: tuple(str(item) for item in error.absolute_path)
    )
    rendered: list[str] = []
    for error in errors[:50]:
        location = "/".join(str(item) for item in error.absolute_path) or "<root>"
        rendered.append(f"{location}: {error.message}")
    if len(errors) > 50:
        rendered.append(f"... {len(errors) - 50} additional schema errors")
    return rendered


def _validate_schema(validator: Draft202012Validator, value: Any, label: str) -> None:
    errors = _schema_errors(validator, value)
    if errors:
        raise EventSynthesisError(f"{label} failed JSON Schema validation: " + " | ".join(errors))


def _safe_workspace_files(root: Path) -> tuple[list[str], list[str]]:
    if not root.is_dir():
        raise EventSynthesisError("workspace_root must be a directory")
    files: list[str] = []
    directories: list[str] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        retained: list[str] = []
        for name in sorted(dirnames):
            child = directory_path / name
            info = child.lstat()
            relative = child.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                raise EventSynthesisError(f"workspace contains a directory symlink: {relative}")
            if not stat.S_ISDIR(info.st_mode):
                raise EventSynthesisError(
                    f"workspace contains a non-directory entry in its directory tree: {relative}"
                )
            retained.append(name)
            directories.append(relative)
        dirnames[:] = retained
        for name in sorted(filenames):
            child = directory_path / name
            info = child.lstat()
            relative = child.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                raise EventSynthesisError(f"workspace contains a file symlink: {relative}")
            if not stat.S_ISREG(info.st_mode):
                raise EventSynthesisError(f"workspace contains a non-regular file: {relative}")
            files.append(relative)
    return sorted(files), sorted(directories)


def _workspace_metadata_fingerprint(root: Path) -> str:
    """Detect source-view changes without re-reading every file's contents.

    The shared readonly mode receives a content hash calculated once by the
    batch launcher.  Rehashing that same multi-gigabyte source for every role
    would defeat the mode, so this inexpensive metadata fingerprint is the
    per-run mutation guard in addition to the required read-only mount.
    """
    files, directories = _safe_workspace_files(root)
    digest = hashlib.sha256()
    for relative in [*directories, *files]:
        info = (root / relative).lstat()
        digest.update(
            _canonical_json(
                [
                    relative,
                    stat.S_IMODE(info.st_mode),
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_dev,
                    info.st_ino,
                ]
            ).encode("utf-8")
        )
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _decode_mountinfo_path(value: str) -> str:
    """Decode the octal escapes used in Linux ``/proc/self/mountinfo`` paths."""
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _mount_options_for_path(path: Path) -> frozenset[str] | None:
    """Return options for the most-specific Linux mount containing ``path``."""
    try:
        target = path.resolve(strict=True)
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    best: tuple[int, frozenset[str]] | None = None
    for line in lines:
        before_separator, separator, _ = line.partition(" - ")
        if not separator:
            continue
        fields = before_separator.split()
        if len(fields) < 6:
            continue
        mount_point = Path(_decode_mountinfo_path(fields[4]))
        try:
            target.relative_to(mount_point)
        except ValueError:
            continue
        options = frozenset(option for option in fields[5].split(",") if option)
        candidate = (len(mount_point.parts), options)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best is not None else None


def materialize_masked_workspace(source: Path, target: Path, masked_paths: set[str]) -> dict[str, Any]:
    """Copy one read-only view while omitting exactly the supplied regular-file paths."""
    source = source.resolve(strict=True)
    if target.exists():
        raise EventSynthesisError(f"workspace view already exists: {target}")
    files, directories = _safe_workspace_files(source)
    known = set(files)
    unknown_masks = sorted(masked_paths - known)
    if unknown_masks:
        raise EventSynthesisError(
            "mask references paths absent from the source workspace: " + ", ".join(unknown_masks)
        )
    target.mkdir(parents=True, mode=0o700)
    for relative in directories:
        (target / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    visible_files = [relative for relative in files if relative not in masked_paths]
    for relative in visible_files:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source / relative, destination, follow_symlinks=False)
        os.chmod(destination, 0o444)
    for directory in sorted(target.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if directory.is_dir():
            os.chmod(directory, 0o555)
    os.chmod(target, 0o555)
    return {
        "visible_paths": visible_files,
        "visible_snapshot_hash": workspace_snapshot_hash(str(target)),
    }


def deterministic_mask(
    *,
    accepted_files: set[str],
    previous_mask: set[str],
    mask_rate: float,
    random_seed: int,
    accepted_candidate_index: int,
) -> dict[str, Any]:
    """Select the next cumulative mask by private hash ranking; no coverage metric is computed."""
    population = sorted(accepted_files - previous_mask)
    if population:
        sample_count = max(1, min(len(population), round(len(population) * mask_rate)))
        ranked = sorted(
            population,
            key=lambda path: hashlib.sha256(
                f"{random_seed}:{accepted_candidate_index}:{path}".encode("utf-8")
            ).digest(),
        )
        newly_masked = sorted(ranked[:sample_count])
    else:
        newly_masked = []
    cumulative = sorted(previous_mask | set(newly_masked))
    body = {
        "schema_version": 1,
        "artifact_type": "mask_manifest",
        "accepted_candidate_index": accepted_candidate_index,
        "random_seed": random_seed,
        "mask_rate": mask_rate,
        "population_paths": population,
        "newly_masked_paths": newly_masked,
        "masked_paths": cumulative,
    }
    return {**body, "content_hash": _sha256_json(body)}


def _run_spec_block(spec: dict[str, Any]) -> str:
    return "<run-spec>\n" + json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2) + "\n</run-spec>"


def _trace_has_image_observation(result: dict[str, Any]) -> bool:
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    execution = trace.get("executionTrace") if isinstance(trace.get("executionTrace"), list) else []
    for item in execution:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool") or item.get("tool_name")
        if not isinstance(tool, str):
            continue
        normalized = tool.strip().lower().replace("-", "_")
        if normalized not in {"image", "view_image"} and not normalized.endswith("/view_image"):
            continue
        status = str(item.get("status") or "completed").strip().lower()
        if status not in {"failed", "declined", "error", "in_progress"} and not item.get("error"):
            return True
    return False


def _visual_preview_trace_errors(
    events: list[dict[str, Any]], author_result: dict[str, Any]
) -> list[str]:
    visual_event_ids = [
        str(event.get("event_id") or "<unknown>")
        for event in events
        if event.get("action") == "file.preview"
        and isinstance(event.get("payload"), dict)
        and isinstance(event["payload"].get("observed_excerpt"), str)
        and bool(event["payload"]["observed_excerpt"].strip())
    ]
    if not visual_event_ids or _trace_has_image_observation(author_result):
        return []
    return [
        f"event {event_id}: non-empty file.preview observed_excerpt requires successful "
        "image-capable Codex trace evidence"
        for event_id in visual_event_ids
    ]


def _interference_bridge_validation_errors(
    *,
    task: dict[str, Any],
    events: list[dict[str, Any]],
    bridges: list[InterferenceBridge],
) -> list[str]:
    """Require each private bridge to be realized as one ordered public session."""
    if not bridges:
        return []
    errors: list[str] = []
    files_used = {
        path for path in task.get("files_used", []) if isinstance(path, str)
    }
    reads_by_session: dict[str, dict[str, list[int]]] = {}
    for index, event in enumerate(events):
        if event.get("action") != "file.read":
            continue
        session_id = event.get("session_id")
        object_value = event.get("object")
        path = (
            object_value.get("path_at_event")
            if isinstance(object_value, dict)
            else None
        )
        if isinstance(session_id, str) and isinstance(path, str):
            reads_by_session.setdefault(session_id, {}).setdefault(path, []).append(index)

    public_text = _canonical_json({"task": task, "events": events})
    for bridge in bridges:
        required_paths = set(bridge.distractor_files) | set(bridge.correct_files)
        missing_files_used = sorted(required_paths - files_used)
        if missing_files_used:
            errors.append(
                f"interference bridge {bridge.bridge_id}: files_used is missing "
                + ", ".join(missing_files_used)
            )
        sessions_with_all_reads = [
            paths
            for paths in reads_by_session.values()
            if required_paths <= set(paths)
        ]
        if not sessions_with_all_reads:
            errors.append(
                f"interference bridge {bridge.bridge_id}: one session must file.read every "
                "distractor and correct path"
            )
        elif not any(
            max(
                index
                for path in bridge.distractor_files
                for index in session_reads[path]
            )
            < min(
                index
                for path in bridge.correct_files
                for index in session_reads[path]
            )
            for session_reads in sessions_with_all_reads
        ):
            errors.append(
                f"interference bridge {bridge.bridge_id}: all distractor reads must precede "
                "all correct-file reads in the shared session"
            )
        if bridge.bridge_id in public_text:
            errors.append(
                f"interference bridge {bridge.bridge_id}: private bridge_id leaked into public artifacts"
            )
    for forbidden_label in (
        "interference_bridge",
        "distractor_files",
        "correct_files",
        "干扰文件",
        "正确文件",
        "目标文件",
    ):
        if forbidden_label in public_text:
            errors.append(
                f"private interference-bridge label leaked into public artifacts: {forbidden_label}"
            )
    return errors


def _reviewer_safe_validation_result(
    validation_errors: list[str],
) -> dict[str, Any]:
    """Hide private bridge identities and file roles from Codex B."""
    private_prefixes = (
        "interference bridge ",
        "private interference-bridge label leaked",
    )
    public_errors = [
        error
        for error in validation_errors
        if not error.startswith(private_prefixes)
    ]
    private_error_count = len(validation_errors) - len(public_errors)
    if private_error_count:
        public_errors.append(
            "candidate violates one or more private authoring constraints; return "
            "REVISE and ask Codex A to follow its private run-spec and mechanical feedback"
        )
    return {"valid": not validation_errors, "errors": public_errors}


def render_codex_a_prompt(spec: dict[str, Any], *, repair: bool) -> str:
    if repair:
        repair_text = (
            "这是 repair。先读取 input/previous-candidate-task.json、input/previous-events.jsonl 和 "
            "input/reviewer-feedback.json；同时读取 input/mechanical-validation.json，修复其中列出的所有机械错误。"
            "只修复反馈指出的问题；不要换一个无关任务。"
        )
    elif spec.get("construction_mode") == "interference_bridge":
        repair_text = (
            "这是新候选。围绕 run-spec 中的 interference_bridges，构造一次从容易混淆的材料逐步核对到更合适"
            "材料的自然历史工作过程；不要另选无关任务。"
        )
    else:
        repair_text = (
            "这是新候选。自行选择当前可见 Workspace 中具有真实内容联系的文件，构造一个合理的历史工作任务。"
        )
    return f"""你是 Codex A，负责基于当前可见 Workspace 合成一个候选 Context Event Log。

{repair_text}

可读范围：
- workspace/：当前轮次的只读 Workspace；只能使用这里实际可见的文件。
- contracts/context-event-log-schema.json：每条公开事件的 JSON Schema。
- contracts/context-event-log-workflow-schema.json：candidate_task 的 JSON Schema。
- input/：repair 时的上一版候选和 Codex B 反馈。

必须真实使用 Codex 和 shell 检查文件。除 run-spec 中可选的私有 rubric_contexts 或
interference_bridges 外，不得读取 workspace/ 之外的
benchmark task、rubric、reference answer；不得访问网络，不得修改 workspace/，不得查看 runtime.private 或当前工作目录之外的任何路径。

生成原则：
1. 你可以推断下载、打开、读取、复制、移动、写入、导出等历史，不要求独立历史证据。
2. 推断事件必须使用 synthetic=true、generation_method=agent_inference、
   transition_basis=agent_inference、temporal_basis=synthetic_timestamp。
3. content_basis 根据内容来源选择 workspace_content、workspace_metadata、agent_inference 或 not_applicable。
4. 引用实际文件内容的 excerpt 必须与 locator 覆盖的原文或单元格值忠实一致，不能概括、改写、补词，
   也不能省略 locator 范围内会改变含义的内容。表格范围过大时缩小 locator 或拆成多个 evidence，
   不要把不连续单元格拼成一个连续摘录。纯推断内容必须明确使用 content_basis=agent_inference。
   每条 file.read 的 purpose、observation 和由该事件支撑的任务叙述，只能使用该 locator/excerpt 或时间线上
   更早事件已经提供的证据；不能读取相邻但未记录的页/段落/单元格后，把结论塞进较窄 locator 的事件。
   excerpt 若在句中、条款标题或跨页处截断，不得凭未记录的后续内容补全含义；需要该结论时扩大 locator、
   增加忠实的后续 read，或把 observation 缩窄到当前摘录实际支持的范围。后续 read 不能倒过来证明早先事件。
   当前 shell.command payload 只记录 command、status、exit_code 和 duration_ms，不保存 stdout/stderr；它只能
   证明命令被执行，不能证明搜索命中了哪些文本，也不能替代 file.read 的 locator/excerpt。即使用 rg/sed 等
   命令实际找到了内容，也必须先增加相应 file.read，再在该 read 或后续事件中使用这一事实。
   任何公开 payload 字符串都不能包含 http://、https:// 或 file:// URL；遇到带 URL 的单元格时必须
   缩小或更换 locator，完整避开该单元格，不能通过删改 URL 来伪造“忠实摘录”。
5. 日志应形成一个连贯任务，不要为了 action 数量而堆砌事件，也不要求覆盖所有 action。
6. 当前 Workspace 是最终状态。copy/move/rename/write/delete 等历史必须与最终可见文件状态相容。
   不要先用 file.read 把最终值描述为已经存在，又用后续 file.write 声称首次补入同一值；若确实是整理、
   格式化或保留既有内容，read、before_excerpt、after_excerpt、diff_excerpt 和 summary 必须一致地说明。
   file.create 后若最终文件含多个 sheet/section，事件链必须解释所有已填充 sheet/section 的内容来自何处或如何写入。
   file.write 中每项来自 workspace_content 的具体新内容，都应有更早的 file.read 证据；否则应缩窄写入、
   补充真实读取，或将相应内容如实标为 agent_inference。
   每条 file.write 必须至少提供一个非 null 的 after_excerpt 或 diff_excerpt，并与 locator 的写入结果一致。
   对 ZIP 等归档文件，file.open 只能支持“查看归档元数据或成员清单”，不能表示已经读取成员内容。
   如果任务声称使用归档成员，必须先用 file.extract 记录解压目录，再对历史成员对象记录必要的 open/preview/read；
   成员若不在最终 Workspace 中，还必须用 file.delete 或 folder.delete 等后续事件闭合最终状态。
   归档成员是由可见归档推断出的历史对象，不加入 files_used；files_used 仍只列当前真实可见的 regular file。
   file.preview 的 observed_excerpt=null 只表示文件被显示，不能支持“已确认图像内容或与文本相符”等结论。
   仅根据归档成员文件名选择图片时，summary、purpose 和 observation 必须明确是 filename/metadata-level 推断，
   不能冒充视觉核验。只有实际使用本地图像能力检查像素内容后，才可写视觉观察，并用非空 observed_excerpt
   记录简短观察、content_basis=workspace_content；若无法实际检查，就缩窄任务叙事，不得猜测图像内容。
   编排器会机械检查本次 Codex trace；解压、读取文件名、ImageMagick 元数据或在文本中声称“已查看”都不算
   image-capable 证据。没有成功的 image/view_image 工具事件时，非空 observed_excerpt 会被机械拒绝。
7. 所有路径都相对 workspace/ 目录内部的 Workspace 根：看到 workspace/预算/budget.csv 时，
   files_used 和 object.path_at_event 必须写预算/budget.csv，绝不能写 workspace/预算/budget.csv。
   files_used 只列你实际检查并用于构造任务的当前可见 regular file，排序且去重。
8. 所有事件使用 run-spec 指定的 workspace_id；时间带时区并严格不倒退；ID 必须符合 schema。
   event_id 和 session_id 必须是 opaque identifier，只保留 evt_、ses_ 类型前缀和不透明随机后缀，
   不得在 ID 中编码日期、时间、路径、action 或顺序。repair 若改变时间或对象，也必须消除旧 ID 中残留的语义。
9. 每个 session 的第一条事件必须是 session.start，最后一条必须是 session.end，且各恰好出现一次。
10. 不写 task.start/task.finish，不泄露 mask、condition、seed、文件重要性或任何 rubric 信息。

私有 rubric_contexts 规则：run-spec 的 rubric_contexts 仅在非空时出现，且只会提供给你（Codex A）。它是一组彼此相关的
私有定向信号，用于补充最终 Workspace 中未保留、但自然可能存在于过往会议、评审或协作中的背景。综合整组上下文，构造一段
与实际 Workspace 文件自然相关、连贯的简短历史，而不是逐条把它们当作要完成或证明的要求：
- 不得逐字复制、概括复述或暗中编码任何 rubric_contexts 条目，也不得在 candidate title、summary、公开事件、路径、ID、locator、
  excerpt、payload 中提及 rubric、评分、任务、标准答案或“隐藏要求”。
- 不要把条件清单伪装成历史记录。应以自然的过往协作过程表达必要背景，例如一次评审提出的取舍、一次会议确认的交付约定，
  或曾短暂存在、随后删除的工作便笺；内容必须与实际检查过的 Workspace 文件相容。
- 必须把这段历史显式落成一条可审计的事件链，而不能把私有信号直接写进派生交付物：先以 `file.create` 和
  `file.write` 创建一份自然命名的、最终不存在的历史评审记录/会议纪要/协作便笺；该 write 的 provenance 必须为
  `content_basis=agent_inference`。随后用 `file.read` 读取这份历史记录，且该 read 同样标为 `agent_inference`，
  使用它的 locator/excerpt 忠实记录该段自然历史。只有在这个 read 之后，才可在后续事件中据此形成派生交付物。
- 后续交付物中来自该历史记录的内容仍须标为 `agent_inference`，并明确写成“依照先前评审约定/会后共识整理”，不能声称
  是从当前 Workspace 文件直接读得。candidate summary 也必须区分当前文件的事实核对与历史约定，不能把两者合称为
  “源资料已确认的结论”。
- 历史记录及由它产生、但最终 Workspace 中不存在的临时文件或目录，必须用后续 delete 事件闭合，并且不能列入
  files_used。使用一个语义内聚的历史记录，不要为了逐条对应私有信号而堆砌多个无关 session 或事件。

rubric_contexts 是私有输入而非公开日志证据；公开事件仍须遵守下面全部 excerpt、路径、provenance 和状态转换要求。

私有 interference_bridges 规则：run-spec 的 interference_bridges 仅在
construction_mode=interference_bridge 时出现，且只会提供给你（Codex A）。每一项描述一组曾经容易被混淆的
文件路径，以及同一次历史排查最终转向的更合适文件路径。它用于生成自然的文件选择历史，不是让你公开贴标签：
- 每个 bridge 必须形成一个独立且连贯的 session。在同一 session 中，先逐一实际读取 distractor_files，再逐一
  实际读取 correct_files；所有列出的路径都必须出现在 candidate 的 files_used 中并至少有一条 file.read。
- 先根据 distractor_files 的真实内容、适用范围、来源属性、时间、对象或文档目的，记录当时为什么发现它们不适合
  当前那次历史工作的自然迹象；再读取 correct_files，并根据同样可核验的内容差异记录为何转向这些文件。
- 公开 title、summary、事件和任何 ID 中不得出现 bridge_id、interference_bridge、distractor_files、
  correct_files、“干扰文件”“正确答案”“目标文件”等实验标签。应使用自然业务措辞，例如“先核对外部参考材料，
  发现适用范围不符后，转向内部现行记录”。
- 不能仅凭路径或文件名断言哪份更合适。每个关键选择理由必须由该 session 中时间更早的忠实 file.read excerpt
  支撑；如果文件内容不能支持某种强结论，就缩窄表述为实际可观察到的差异。
- 不要创建一份直接罗列路径对应关系的便笺。路径间的联系应由同一次自然排查 session 建立，使后续按任一已读路径
  查询历史时都能召回同 session 中的其他关联文件。

interference_bridges 是私有构造输入而非公开日志证据。它的约束优先于下面“preferred_files 不是必须使用的清单”：
bridge 内列出的每个路径都必须使用；其他 preferred_files 仍只是可选建议。

定向诊断规则：若 run-spec 的 preferred_files 非空，优先从其中仍可见的路径选择一个语义内聚的子集，
并可搭配 Workspace 中自然相关的其他文件构造任务。这只是选择建议，不是 PASS 或覆盖要求；不得为了使用列表
而拼接无关文件。若这些文件无法形成自然任务，可以改选其他可见文件。candidate title、summary 和公开事件中
不得提及 preferred_files、selection_policy、task ID、数据集选择过程或“优先/重要文件”等实验信息。
检索范围规则：preferred_files 非空时，先直接读取其中可用的文件；不得以 find workspace、rg --files workspace
或同等的无边界目录枚举作为起点，也不要扫描与这些路径无关的整个 Workspace。确有必要补充材料时，只查看一个
已选文件的直接父目录或一条明确相关的候选路径，并尽快停止搜索。这个约束只限制无关的探索成本，不要求使用每个
preferred_files，也不改变上述“可改选其他文件”的规则。

视觉文件约束：run-spec 的 image_capable=false 表示本次 Codex 运行没有受审计的 image/view_image 能力。此时不得
把 PNG/JPG/GIF/WebP 等纯视觉文件写入 files_used，不得写 file.preview，也不得从截图、渲染页或终端图片中归纳
任何事实。PDF 若能用当前环境中的本地 shell/parser 提取真实文本，可以写入 files_used 并记录 file.read，但
locator 必须精确到页码或可核验文本范围，excerpt 必须与提取结果忠实一致，且不得声称检查了排版、图像、图表或
其他未被文本解析覆盖的视觉内容。只有 image_capable=true 时才允许视觉预览，并且每个非空 preview 摘录都必须有
成功 image/view_image trace 支撑。preferred_files 不是必须使用的清单。

必须写出：
- output/candidate-task.json
- output/events.jsonl

candidate-task.json 的 candidate_id、candidate_index、attempt、mode、previous_candidate_id、
visible_workspace_manifest_hash、author_run_id 必须逐字采用 run-spec；events_path 必须是 events.jsonl。
完成前用本地 JSON Schema 检查两个文件。最终回复只说明文件已写出，不要把 JSONL 粘贴到回复中。

{_run_spec_block(spec)}
"""


def render_codex_b_prompt(spec: dict[str, Any]) -> str:
    return f"""你是 Codex B，负责独立审查 Codex A 的候选 Context Event Log。

可读范围：
- workspace/：与 Codex A 完全相同的只读 Workspace 视图。
- input/candidate-task.json 与 input/events.jsonl：待审查候选；文件缺失或无效也属于审查结果。
- input/mechanical-validation.json：编排器对候选执行的确定性校验结果；其中 valid=false 或 errors 非空时必须 REVISE。
- contracts/：公开事件和 workflow JSON Schema。

不得修改 workspace/ 或 input/，不得访问网络，不得读取当前工作目录之外的任何路径。
你必须亲自用 shell 和 JSON Schema 检查候选，并打开 files_used 中的相关文件进行语义核对。

逐项检查且恰好各输出一次：schema_conformance、file_consistency、excerpt_fidelity、
action_semantics、timeline_plausibility、task_coherence。
即使 mechanical-validation 为 invalid，也必须在同一次审查中继续完成其余五项实质检查，尽量一次性列出
当前候选的全部可发现问题；不要把语义问题推迟到 repair 后的下一次审查。

PASS 条件：六项全部 passed、没有 issue，并且 mechanical-validation 的 valid=true、候选能被 schema 验证、
files_used 中的每个路径都是当前真实可见的 regular file（目录绝不是 file）、
excerpt 与 locator 对应的原文或单元格值忠实相符（不能把概括改写当作 excerpt）、操作链和最终
Workspace 状态一致、任务整体自然连贯。特别检查 read→write 是否把同一最终值同时声称为“已存在”和
“首次补入”，write 的具体 workspace_content 是否有更早的读取依据，以及新建文件的所有已填充
sheet/section 是否都被事件链解释。
excerpt 逐字正确仍不足以 PASS：逐条检查 file.read 的 purpose、observation，以及 candidate summary 中归因于
该 read 的事实，是否由该 locator/excerpt 或时间线上更早证据实际支持。不得用 B 自己看到但候选没有记录的
相邻页、下一页、其他段落或单元格替 A 补证据；后续 read 也不能倒过来证明较早事件当时已经知道的内容。
若 excerpt 在句中、条款标题或跨页处截断，而 observation 补出了未记录的后续要求、名单、数值或结论，必须
REVISE，要求 A 扩大 locator、增加后续 read，或缩窄 observation 和任务叙述。
shell.command 的 command/status/exit_code/duration_ms 只能证明命令被执行，当前公开 schema 不保存 stdout/stderr。
不得把 rg、sed、unzip 等命令本身当作其输出内容的证据，也不得用后置 shell.command 倒推早先 file.read 已知
某项事实。若 summary、purpose 或 observation 使用了 shell 搜索发现的内容，必须有更早或同位置的 file.read
locator/excerpt 明确记录该内容；否则必须 REVISE。
当前 Workspace 只能证明最终状态，不能提供历史 before state。不要要求 A 通过读取当前最终值来证明
过去的旧版本；只要转换被明确标为 agent_inference、before/after 叙事与最终状态相容，就允许推断旧状态。
同样，content_basis=agent_inference 的新增设计内容不要求在更早的 file.read 中逐字出现；应检查它是否
被如实标注、与已读背景自然相关且没有在任务摘要中伪称为直接摘录或唯一事实来源。
当候选先以 `agent_inference` 创建并写入一份最终已删除的历史评审记录/会议纪要，随后又以同一 provenance
读取该记录时，后续交付物可以把这个更早 read 作为“先前协作约定”的来源。不要因为这份历史记录不在当前
Workspace 中或其内容不能由当前文件独立复现而拒绝；应检查 create→write→read→使用→delete 链条完整、
provenance 诚实，且候选没有把历史约定伪称为当前文件直接事实。若缺少这条历史 read，或 summary 混淆了
历史约定与当前文件事实，必须 REVISE。
对于 ZIP 等归档文件，file.open 只能证明查看归档元数据或成员清单。若候选声称查看、读取或使用成员内容，
却没有先记录 file.extract，必须 REVISE。亲自核对归档内的成员路径、时间和格式；历史解压成员可以不在
files_used 中，但若这些成员在最终 Workspace 不可见，事件链必须通过 file.delete/folder.delete 等操作
解释其消失。不得仅因最终快照没有解压目录而否定 provenance 正确且带有合理清理步骤的 inferred extract。
逐项比较 candidate summary、preview 的 purpose/observation 和 observed_excerpt。observed_excerpt=null 只能证明
文件被打开或显示，不能证明图片内容已被确认、与报告相符或包含某个人物/行为。仅查看 ZIP 成员名也只是
metadata-level 依据，不能冒充视觉检查。若候选提出视觉结论，B 必须用本地图像能力独立核验；做不到时应
REVISE，要求 A 删除视觉结论或改成明确的 filename/metadata-level 推断。
input/mechanical-validation.json 也会检查 A trace：非空 observed_excerpt 若没有成功的 image/view_image
工具事件必定 invalid。此时必须 REVISE，不能用“描述看起来正确”覆盖 provenance 缺口。
file.download、file.import、file.export、file.copy、file.move、file.rename、file.delete 和 file.restore
本来就是最终快照通常无法直接证明的历史转换。只要事件明确标为 agent_inference、源与目标的名称、格式、
内容关系在语义上合理，并且操作链与最终可见状态相容，就不得仅以“没有 trace/历史证据证明该转换发生”
为由判失败。例如，同一对象的源格式与派生格式可以被合理推断为 export；只有格式或内容明显不相容、
最终状态矛盾，或 provenance 冒充直接观察/trace 时才应拒绝。候选摘要可以把这种转换叙述为推断出的历史，
不必在每句话重复“不确定”，但不能声称存在未实际读取的 trace 或外部证据。
每个 session 必须恰好包含一个 session.start 和一个 session.end，并分别是该 session 的首尾事件。
路径必须相对 workspace/ 目录内部的 Workspace 根；files_used 和 object.path_at_event 出现
workspace/ 前缀时必须 REVISE，即使从当前 role workdir 出发该路径能够打开。
event_id 和 session_id 应当是 opaque identifier；如果 ID 自身编码的日期、路径、action 或顺序与事件内容
发生矛盾，必须 REVISE，要求 A 改为只含类型前缀和不透明随机后缀的标识符。
否则返回 REVISE；至少给出一个具体 issue，包含可执行 suggestion。不要因为 action 少、没有覆盖率或
没有独立历史证据而拒绝；推断本身是允许的，只要 provenance 标注正确且语义合理。
每个 issue.paths 必须使用相对 Workspace 根的路径，并按 Unicode 字典序排序且去重；空列表可以保留。

必须写出 output/review-result.json，candidate_id 和 attempt 必须采用 run-spec 中的预期值。
完成前使用 workflow JSON Schema 验证。最终回复只给出 PASS 或 REVISE。

{_run_spec_block(spec)}
"""


def render_codex_c_prompt(spec: dict[str, Any]) -> str:
    return f"""你是 Codex C，负责把所有 Codex B 已 PASS 的候选整理为一条全局 canonical timeline。

启动前提已满足：accepted/ 至少包含一个通过候选。workspace/ 是完整只读 Workspace。
contracts/context-event-log-canonical-schema.json 约束 canonical.private.jsonl 的每一行，
contracts/context-event-log-workflow-schema.json 约束 timeline-result.json。

不得访问网络，不得修改 workspace/ 或 accepted/，不得新增或删除候选事件，不得改变 action、object、
path、locator、excerpt、payload 或 provenance。允许修改的公开字段只有 event_id、session_id、occurred_at；
workspace_id 必须保持 run-spec 指定值。你可以在 private canonical wrapper 中设置 canonical_sequence 和
causal_links。所有 causal link 必须指向更早事件。

要求：
1. 每个输入事件恰好出现一次。保持候选内部因果顺序；跨任务依赖按生产者在前、消费者在后排列。
2. 全局 occurred_at 不倒退，canonical_sequence 从 1 连续递增。
3. timeline-result.json 的 entries 必须一一映射 input_event_id 到 output_event_id，并与 canonical sequence 对齐。
4. 若冲突不能只靠重排和允许字段解决，写 TIMELINE_BLOCKED 和具体 blocking_issues，不篡改语义。
5. complete 时写 output/canonical.private.jsonl、output/timeline-result.json、output/timeline-decisions.md。
6. blocked 时至少写 output/timeline-result.json 和 output/timeline-decisions.md。
7. canonical wrapper 使用 generator_version=codex-c-timeline-v1、validator_status=passed；证据 hash 可省略。
8. causal_links 不是可省略的装饰：除 session.start 外，每条事件必须至少有一条指向同一 session
   内更早事件的 causal link。按语义选择 created_from、derived_from 或 informed_by；跨 session 依赖可以
   额外添加，但不能替代同一 session 内的基本因果链。session.end 至少链接该 session 的最后一个实质事件。
9. 输出的 event_id 和 session_id 必须保持 opaque：只使用 evt_、ses_ 类型前缀和不透明随机后缀，
   不得编码日期、时间、路径、action 或 canonical_sequence。输入 ID 若带有这类人类可读语义，使用允许的
   ID 重写能力消除它，并在 timeline-result.json 中保留准确的 input_event_id→output_event_id 映射。

完成前使用两个 JSON Schema 验证输出。最终回复只给出 TIMELINE_COMPLETE 或 TIMELINE_BLOCKED。

{_run_spec_block(spec)}
"""


class EventSynthesisOrchestrator:
    def __init__(
        self,
        *,
        config: SynthesisRunConfig,
        codex_runner: CodexRunner,
        schema_root: Path = DEFAULT_SCHEMA_ROOT,
        resume: bool = False,
    ) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self.contracts = SchemaContracts.load(schema_root.resolve(strict=True))
        self.source = Path(config.workspace_root).resolve(strict=True)
        self.output = Path(config.output_root).resolve()
        self.resume = resume
        self.all_source_files = set(_safe_workspace_files(self.source)[0])
        if not self.all_source_files:
            raise EventSynthesisError("workspace_root must contain at least one regular file")
        self.source_metadata_fingerprint: str | None = None
        self.source_mount_options: tuple[str, ...] | None = None
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            self.source_mount_options = self._assert_shared_source_is_readonly()
            assert self.config.shared_workspace_snapshot_hash is not None  # validated by SynthesisRunConfig
            self.source_snapshot_hash = self.config.shared_workspace_snapshot_hash
            self.source_metadata_fingerprint = _workspace_metadata_fingerprint(self.source)
        else:
            self.source_snapshot_hash = workspace_snapshot_hash(str(self.source))
        self.workspace_id = opaque_id("wrk", self.source_snapshot_hash)
        unknown_preferred_files = sorted(set(config.preferred_files) - self.all_source_files)
        if unknown_preferred_files:
            raise EventSynthesisError(
                "preferred_files reference paths absent from the source workspace: "
                + ", ".join(unknown_preferred_files)
            )
        bridge_paths = {
            path
            for bridge in config.interference_bridges
            for path in (*bridge.distractor_files, *bridge.correct_files)
        }
        unknown_bridge_paths = sorted(bridge_paths - self.all_source_files)
        if unknown_bridge_paths:
            raise EventSynthesisError(
                "interference bridges reference paths absent from the source workspace: "
                + ", ".join(unknown_bridge_paths)
            )
        self.base_natural_event_log = (
            Path(config.base_natural_event_log).resolve(strict=True)
            if config.base_natural_event_log is not None
            else None
        )
        if (
            self.base_natural_event_log is not None
            and not self.base_natural_event_log.is_file()
        ):
            raise EventSynthesisError("base_natural_event_log must be a regular file")
        self.target_file_count = math.ceil(len(self.all_source_files) * config.target_file_coverage)
        self.accepted: list[CandidateRecord] = []
        self.masked_paths: set[str] = set()
        self.current_candidate_b_failures = 0
        self.abandoned_candidate_count = 0
        self.candidate_index = 1

    def _assert_shared_source_is_readonly(self) -> tuple[str, ...]:
        options = _mount_options_for_path(self.source)
        if options is None or "ro" not in options:
            raise EventSynthesisError(
                "shared_readonly_unmasked requires workspace_root on a read-only mount"
            )
        return tuple(sorted(options))

    def _assert_source_unchanged(self) -> None:
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            self._assert_shared_source_is_readonly()
            assert self.source_metadata_fingerprint is not None
            if _workspace_metadata_fingerprint(self.source) != self.source_metadata_fingerprint:
                raise EventSynthesisError(
                    "shared readonly source workspace metadata changed during event-log synthesis"
                )
            return
        if workspace_snapshot_hash(str(self.source)) != self.source_snapshot_hash:
            raise EventSynthesisError("source workspace changed during event-log synthesis")

    def _remaining_preferred_files(self) -> list[str]:
        accepted_or_masked = self._accepted_files() | self.masked_paths
        return [
            path for path in self.config.preferred_files if path not in accepted_or_masked
        ]

    def _accepted_files(self) -> set[str]:
        return {
            path
            for record in self.accepted
            for path in record.task.get("files_used", [])
            if isinstance(path, str)
        }

    def _coverage(self) -> dict[str, Any]:
        accepted_file_count = len(self._accepted_files())
        accepted_preferred_file_count = len(
            self._accepted_files() & set(self.config.preferred_files)
        )
        preferred_file_count = len(self.config.preferred_files)
        return {
            "accepted_file_count": accepted_file_count,
            "total_source_file_count": len(self.all_source_files),
            "file_coverage": accepted_file_count / len(self.all_source_files),
            "target_file_coverage": self.config.target_file_coverage,
            "target_file_count": self.target_file_count,
            "coverage_target_met": accepted_file_count >= self.target_file_count,
            "preferred_file_count": preferred_file_count,
            "accepted_preferred_file_count": accepted_preferred_file_count,
            "preferred_file_coverage": (
                accepted_preferred_file_count / preferred_file_count
                if preferred_file_count
                else None
            ),
        }

    def _progress(self) -> dict[str, Any]:
        coverage = self._coverage()
        accepted_candidate_count = len(self.accepted)
        accepted_candidate_target_met = (
            self.config.stop_after_accepted_candidates is not None
            and accepted_candidate_count >= self.config.stop_after_accepted_candidates
        )
        return {
            **coverage,
            "accepted_candidate_count": accepted_candidate_count,
            "stop_after_accepted_candidates": self.config.stop_after_accepted_candidates,
            "accepted_candidate_target_met": accepted_candidate_target_met,
            "termination_target_met": (
                accepted_candidate_target_met
                if self.config.stop_after_accepted_candidates is not None
                else coverage["coverage_target_met"]
            ),
        }

    def _api_provider(self) -> dict[str, Any]:
        provider: dict[str, Any] = {
            "authMode": self.config.auth_mode,
            "model": self.config.model,
            "__codex_runtime__": {
                "expected_cli_version": self.config.expected_codex_version,
                "protocol": "responses",
                "mcp_servers": {},
                "tool_schemas": {},
            },
        }
        if self.config.base_url is not None:
            provider["baseUrl"] = self.config.base_url
        return provider

    def _initialise_output(self) -> None:
        if self.output.exists():
            raise EventSynthesisError("output_root must not already exist")
        try:
            self.output.relative_to(self.source)
        except ValueError:
            pass
        else:
            raise EventSynthesisError("output_root must be outside workspace_root")
        self.output.mkdir(parents=True, mode=0o700)
        stored_config = self.config.model_dump(mode="json")
        base_url = stored_config.pop("base_url", None)
        stored_config["base_url_sha256"] = (
            "sha256:" + hashlib.sha256(base_url.encode("utf-8")).hexdigest()
            if isinstance(base_url, str)
            else None
        )
        _write_json(
            self.output / "run-config.private.json",
            {
                **stored_config,
                "orchestrator_version": SYNTHESIS_ORCHESTRATOR_VERSION,
                "source_snapshot_hash": self.source_snapshot_hash,
                "workspace_id": self.workspace_id,
                "workspace_view": {
                    "mode": self.config.workspace_view_mode,
                    "source_mount_options": self.source_mount_options,
                    "source_metadata_fingerprint": self.source_metadata_fingerprint,
                },
                "schema_hashes": {
                    path.name: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.contracts.files
                },
            },
        )
        self._save_state("RUNNING")

    def _expected_stored_config(self) -> dict[str, Any]:
        stored = self.config.model_dump(mode="json")
        base_url = stored.pop("base_url", None)
        stored["base_url_sha256"] = (
            "sha256:" + hashlib.sha256(base_url.encode("utf-8")).hexdigest()
            if isinstance(base_url, str)
            else None
        )
        return stored

    def _load_accepted_records(self) -> list[CandidateRecord]:
        accepted_root = self.output / "accepted.private"
        records: list[CandidateRecord] = []
        global_event_ids: set[str] = set()
        if not accepted_root.exists():
            return records
        for artifact_root in sorted(path for path in accepted_root.iterdir() if path.is_dir()):
            acceptance = _read_json(artifact_root / "acceptance.private.json")
            task = _read_json(artifact_root / "candidate-task.json")
            review = _read_json(artifact_root / "review-result.json")
            events = _read_jsonl(artifact_root / "events.jsonl")
            _validate_schema(self.contracts.workflow, task, "resumed candidate task")
            _validate_schema(self.contracts.workflow, review, "resumed candidate review")
            try:
                CandidateTask.model_validate(task)
                ReviewResult.model_validate(review)
            except ValueError as exc:
                raise EventSynthesisError(
                    f"resumed accepted candidate failed semantic validation: {artifact_root}: {exc}"
                ) from exc
            candidate_index = acceptance.get("candidate_index")
            candidate_id = acceptance.get("candidate_id")
            attempt = acceptance.get("attempt")
            if not isinstance(candidate_index, int) or candidate_index < 1:
                raise EventSynthesisError(f"invalid accepted candidate index: {artifact_root}")
            if artifact_root.name != f"{candidate_index:04d}-{candidate_id}":
                raise EventSynthesisError(f"accepted artifact directory name disagrees with metadata: {artifact_root}")
            expected_identity = (candidate_id, candidate_index, attempt)
            if (task.get("candidate_id"), task.get("candidate_index"), task.get("attempt")) != expected_identity:
                raise EventSynthesisError(f"accepted candidate task identity disagrees with acceptance: {artifact_root}")
            if (review.get("candidate_id"), task.get("candidate_index"), review.get("attempt")) != expected_identity:
                raise EventSynthesisError(f"accepted review identity disagrees with acceptance: {artifact_root}")
            if review.get("verdict") != "PASS":
                raise EventSynthesisError(f"resumed accepted candidate lacks a PASS review: {artifact_root}")
            for relative in task.get("files_used", []):
                if relative not in self.all_source_files:
                    raise EventSynthesisError(
                        f"resumed accepted candidate references a missing source file: {relative}"
                    )
            validated_events: list[PublicEvent] = []
            for event_index, event in enumerate(events, start=1):
                _validate_schema(
                    self.contracts.public,
                    event,
                    f"resumed candidate {candidate_id} event {event_index}",
                )
                try:
                    parsed = PublicEvent.model_validate(event)
                except (ValueError, TypeError) as exc:
                    raise EventSynthesisError(
                        f"resumed candidate {candidate_id} event failed semantic validation: {exc}"
                    ) from exc
                if parsed.workspace_id != self.workspace_id:
                    raise EventSynthesisError(
                        f"resumed candidate {candidate_id} has a mismatched workspace_id"
                    )
                if parsed.event_id in global_event_ids:
                    raise EventSynthesisError(
                        f"resumed candidates contain duplicate event ID: {parsed.event_id}"
                    )
                global_event_ids.add(parsed.event_id)
                validated_events.append(parsed)
            try:
                validate_visible_events(validated_events)
            except (ValueError, TypeError) as exc:
                raise EventSynthesisError(
                    f"resumed candidate {candidate_id} failed cross-event validation: {exc}"
                ) from exc
            records.append(
                CandidateRecord(
                    candidate_index=candidate_index,
                    attempt=int(attempt),
                    candidate_id=str(candidate_id),
                    task=task,
                    events=tuple(events),
                    artifact_root=artifact_root,
                )
            )
        records.sort(key=lambda record: record.candidate_index)
        indices = [record.candidate_index for record in records]
        if len(indices) != len(set(indices)):
            raise EventSynthesisError("resumed accepted candidates contain duplicate candidate indexes")
        return records

    def _restore_masks(self) -> None:
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            masks_root = self.output / "masks.private"
            existing_masks = sorted(masks_root.glob("mask-*.json")) if masks_root.exists() else []
            if existing_masks:
                raise EventSynthesisError("shared readonly run must not contain mask manifests")
            self.masked_paths = set()
            return
        previous_mask: set[str] = set()
        accepted_files: set[str] = set()
        masks_root = self.output / "masks.private"
        for accepted_number, record in enumerate(self.accepted, start=1):
            accepted_files.update(
                path for path in record.task.get("files_used", []) if isinstance(path, str)
            )
            expected = deterministic_mask(
                accepted_files=accepted_files,
                previous_mask=previous_mask,
                mask_rate=self.config.mask_rate,
                random_seed=self.config.random_seed,
                accepted_candidate_index=accepted_number,
            )
            mask_path = masks_root / f"mask-{accepted_number:04d}.json"
            actual = _read_json(mask_path)
            _validate_schema(self.contracts.workflow, actual, f"resumed mask {accepted_number}")
            try:
                MaskManifest.model_validate(actual)
            except ValueError as exc:
                raise EventSynthesisError(f"resumed mask failed semantic validation: {mask_path}: {exc}") from exc
            if actual != expected:
                raise EventSynthesisError(f"resumed mask does not match deterministic reconstruction: {mask_path}")
            previous_mask = set(actual["masked_paths"])
        existing_masks = sorted(masks_root.glob("mask-*.json")) if masks_root.exists() else []
        if len(existing_masks) != len(self.accepted):
            raise EventSynthesisError("resumed mask count does not match accepted candidate count")
        self.masked_paths = previous_mask

    def _abandon_interrupted_candidates(
        self, *, accepted_indices: set[int], abandoned_indices: set[int], previous_error: str | None
    ) -> list[int]:
        runs_root = self.output / "runs.private"
        run_indices: set[int] = set()
        if runs_root.exists():
            for path in runs_root.glob("candidate-[0-9][0-9][0-9][0-9]"):
                if path.is_dir():
                    run_indices.add(int(path.name.removeprefix("candidate-")))
        interrupted = sorted(run_indices - accepted_indices - abandoned_indices)
        for candidate_index in interrupted:
            candidate_root = runs_root / f"candidate-{candidate_index:04d}"
            attempts = len([path for path in candidate_root.glob("attempt-*" ) if path.is_dir()])
            _write_json(
                self.output / "abandoned.private" / f"candidate-{candidate_index:04d}.json",
                {
                    "schema_version": 1,
                    "artifact_type": "abandoned_candidate",
                    "candidate_index": candidate_index,
                    "attempts": attempts,
                    "last_candidate_id": None,
                    "last_review": None,
                    "failed_role": None,
                    "error": previous_error,
                    "reason": "resume_after_interrupted_candidate",
                },
            )
        return interrupted

    def _resume_output(self) -> None:
        if not self.output.is_dir():
            raise EventSynthesisError("--resume requires an existing output_root directory")
        stored = _read_json(self.output / "run-config.private.json")
        state = _read_json(self.output / "state.private.json")
        stored_version = stored.get("orchestrator_version")
        if stored_version not in RESUMABLE_ORCHESTRATOR_VERSIONS:
            raise EventSynthesisError(f"output was created by an incompatible orchestrator: {stored_version}")
        if state.get("status") == "TIMELINE_COMPLETE":
            raise EventSynthesisError("a completed timeline cannot be resumed")
        expected_config = self._expected_stored_config()
        actual_config = {key: stored.get(key) for key in expected_config}
        if "workspace_view_mode" not in stored:
            actual_config["workspace_view_mode"] = "isolated_masked_copy"
        if "shared_workspace_snapshot_hash" not in stored:
            actual_config["shared_workspace_snapshot_hash"] = None
        if "rubric_contexts" not in stored:
            legacy_rubric_context = stored.get("rubric_context")
            actual_config["rubric_contexts"] = (
                [legacy_rubric_context] if isinstance(legacy_rubric_context, str) else []
            )
        if "interference_bridges" not in stored:
            actual_config["interference_bridges"] = []
        if "construction_mode" not in stored:
            actual_config["construction_mode"] = (
                "rubric_context"
                if actual_config["rubric_contexts"]
                else "workspace_inference"
            )
        if "targeted_task_ids" not in stored:
            actual_config["targeted_task_ids"] = expected_config["targeted_task_ids"]
        if "base_natural_event_log" not in stored:
            actual_config["base_natural_event_log"] = None
        if "stop_after_accepted_candidates" not in stored:
            actual_config["stop_after_accepted_candidates"] = (
                1
                if actual_config["construction_mode"] != "workspace_inference"
                else None
            )
        stored_candidate_limit = actual_config.pop("max_candidate_slots", None)
        requested_candidate_limit = expected_config.pop("max_candidate_slots")
        if actual_config != expected_config:
            raise EventSynthesisError("resume config does not exactly match the stored run config")
        if not isinstance(stored_candidate_limit, int):
            raise EventSynthesisError("stored max_candidate_slots is invalid")
        if requested_candidate_limit < stored_candidate_limit:
            raise EventSynthesisError("resume cannot decrease max_candidate_slots")
        configuration_changes: list[dict[str, Any]] = []
        if requested_candidate_limit > stored_candidate_limit:
            if (
                state.get("status")
                not in {"COVERAGE_TARGET_NOT_MET", "TARGETED_CONTEXT_NOT_GENERATED"}
                or state.get("candidate_index") != stored_candidate_limit + 1
            ):
                raise EventSynthesisError(
                    "max_candidate_slots may increase only after the stored candidate limit was exhausted"
                )
            configuration_changes.append(
                {
                    "field": "max_candidate_slots",
                    "previous_value": stored_candidate_limit,
                    "new_value": requested_candidate_limit,
                    "reason": "continue_after_candidate_limit_reached",
                }
            )
        expected_schema_hashes = {
            path.name: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.contracts.files
        }
        if stored.get("source_snapshot_hash") != self.source_snapshot_hash:
            raise EventSynthesisError("source workspace snapshot changed since the interrupted run")
        if stored.get("workspace_id") != self.workspace_id:
            raise EventSynthesisError("stored workspace_id does not match the current source workspace")
        stored_view = stored.get("workspace_view")
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            if not isinstance(stored_view, dict):
                raise EventSynthesisError("shared readonly run lacks workspace view audit")
            if stored_view.get("mode") != self.config.workspace_view_mode:
                raise EventSynthesisError("stored workspace view mode does not match the current run")
            if stored_view.get("source_metadata_fingerprint") != self.source_metadata_fingerprint:
                raise EventSynthesisError("shared readonly source metadata changed since the interrupted run")
        if stored.get("schema_hashes") != expected_schema_hashes:
            raise EventSynthesisError("schema contracts changed since the interrupted run")

        self.accepted = self._load_accepted_records()
        if state.get("accepted_candidate_ids") != [record.candidate_id for record in self.accepted]:
            raise EventSynthesisError("state accepted_candidate_ids disagree with accepted artifacts")
        self._restore_masks()
        if state.get("masked_paths") != sorted(self.masked_paths):
            raise EventSynthesisError("state masked_paths disagree with reconstructed masks")

        abandoned_root = self.output / "abandoned.private"
        abandoned_indices: set[int] = set()
        if abandoned_root.exists():
            for path in abandoned_root.glob("candidate-[0-9][0-9][0-9][0-9].json"):
                artifact = _read_json(path)
                index = artifact.get("candidate_index")
                if not isinstance(index, int) or path.name != f"candidate-{index:04d}.json":
                    raise EventSynthesisError(f"invalid abandoned candidate artifact: {path}")
                abandoned_indices.add(index)
        accepted_indices = {record.candidate_index for record in self.accepted}
        interrupted = self._abandon_interrupted_candidates(
            accepted_indices=accepted_indices,
            abandoned_indices=abandoned_indices,
            previous_error=state.get("error") if isinstance(state.get("error"), str) else None,
        )
        abandoned_indices.update(interrupted)
        self.abandoned_candidate_count = len(abandoned_indices)
        occupied = accepted_indices | abandoned_indices
        stored_candidate_index = state.get("candidate_index")
        if not isinstance(stored_candidate_index, int) or stored_candidate_index < 1:
            raise EventSynthesisError("stored candidate_index is invalid")
        self.candidate_index = max(stored_candidate_index, max(occupied, default=0) + 1)
        self.current_candidate_b_failures = 0

        resumes_root = self.output / "resumes.private"
        resume_number = len(list(resumes_root.glob("resume-*.json"))) + 1 if resumes_root.exists() else 1
        previous_run_config_sha256 = _sha256_json(stored)
        _write_json(
            resumes_root / f"resume-{resume_number:04d}.json",
            {
                "schema_version": 1,
                "artifact_type": "resume_audit",
                "resumed_at": datetime.now(timezone.utc).isoformat(),
                "previous_orchestrator_version": stored_version,
                "current_orchestrator_version": SYNTHESIS_ORCHESTRATOR_VERSION,
                "previous_state_sha256": _sha256_json(state),
                "previous_run_config_sha256": previous_run_config_sha256,
                "previous_status": state.get("status"),
                "configuration_changes": configuration_changes,
                "loaded_accepted_candidate_ids": [record.candidate_id for record in self.accepted],
                "reconstructed_mask_count": len(self.accepted),
                "interrupted_candidate_indices": interrupted,
                "resumed_candidate_index": self.candidate_index,
                "remaining_preferred_files": self._remaining_preferred_files(),
            },
        )
        if configuration_changes or stored_version != SYNTHESIS_ORCHESTRATOR_VERSION:
            updated_stored = {**stored, **self._expected_stored_config()}
            updated_stored["max_candidate_slots"] = requested_candidate_limit
            updated_stored["orchestrator_version"] = SYNTHESIS_ORCHESTRATOR_VERSION
            _write_json(self.output / "run-config.private.json", updated_stored)
        self._save_state("RUNNING")

    def _save_state(self, status: str, *, error: str | None = None) -> None:
        _write_json(
            self.output / "state.private.json",
            {
                "schema_version": 1,
                "orchestrator_version": SYNTHESIS_ORCHESTRATOR_VERSION,
                "run_id": self.config.run_id,
                "repetition_id": self.config.repetition_id,
                "status": status,
                "source_snapshot_hash": self.source_snapshot_hash,
                "candidate_index": self.candidate_index,
                "current_candidate_b_failures": self.current_candidate_b_failures,
                "max_b_failures_per_candidate": self.config.max_b_failures_per_candidate,
                "max_candidate_slots": self.config.max_candidate_slots,
                "abandoned_candidate_count": self.abandoned_candidate_count,
                "accepted_candidate_ids": [record.candidate_id for record in self.accepted],
                "workspace_view_mode": self.config.workspace_view_mode,
                "masked_paths": sorted(self.masked_paths),
                **self._progress(),
                "error": error,
            },
        )

    def _prepare_role_root(self, role_key: str, *, masked_paths: set[str]) -> tuple[Path, dict[str, Any]]:
        audit_root = self.output / "runs.private" / role_key
        role_root = audit_root / "workdir"
        # A Codex C failure can occur after its role root has been materialized
        # but before a final timeline exists.  Candidate roles are never
        # reused on resume, while Codex C intentionally is; archive the entire
        # interrupted C audit before recreating that one reusable role.  This
        # preserves the original trace and makes the retry provenance explicit
        # instead of failing with FileExistsError on workdir/workspace.
        if role_key == "codex-c" and self.resume and audit_root.exists():
            attempts_root = self.output / "runs.private" / "codex-c-attempts"
            attempts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            attempt_number = len(
                [path for path in attempts_root.glob("attempt-*") if path.is_dir()]
            ) + 1
            archived_root = attempts_root / f"attempt-{attempt_number:04d}"
            shutil.move(str(audit_root), str(archived_root))
            _write_json(
                archived_root / "resume-archive.private.json",
                {
                    "schema_version": 1,
                    "artifact_type": "interrupted_codex_c_archive",
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "resume_after_interrupted_codex_c",
                    "original_role_key": role_key,
                },
            )
        role_root.mkdir(parents=True, mode=0o700)
        workspace_view = role_root / "workspace"
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            if masked_paths:
                raise EventSynthesisError("shared readonly workspace view cannot apply file masks")
            self._assert_shared_source_is_readonly()
            os.symlink(self.source, workspace_view, target_is_directory=True)
            manifest = {
                "view_mode": "shared_readonly_unmasked",
                "visible_file_count": len(self.all_source_files),
                "visible_snapshot_hash": self.source_snapshot_hash,
                "source_metadata_fingerprint": self.source_metadata_fingerprint,
            }
        else:
            manifest = {
                "view_mode": "isolated_masked_copy",
                **materialize_masked_workspace(self.source, workspace_view, masked_paths),
            }
        contracts_root = role_root / "contracts"
        contracts_root.mkdir(mode=0o700)
        for path in self.contracts.files:
            shutil.copyfile(path, contracts_root / path.name)
            os.chmod(contracts_root / path.name, 0o444)
        (role_root / "input").mkdir(mode=0o700)
        (role_root / "output").mkdir(mode=0o700)
        return role_root, manifest

    def _run_role(self, *, role_key: str, agent_id: str, role_root: Path, prompt: str) -> dict[str, Any]:
        audit_root = self.output / "runs.private" / role_key
        prompt_path = audit_root / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        (audit_root / "prompt.sha256").write_text(
            "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest() + "\n", encoding="utf-8"
        )
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            before_hash = self.source_snapshot_hash
        else:
            before_hash = workspace_snapshot_hash(str(role_root / "workspace"))
        result = self.codex_runner(
            prompt=prompt,
            work_dir=str(role_root),
            sandbox_dir=str(audit_root / "runtime.private"),
            timeout_s=self.config.timeout_seconds,
            api_provider=self._api_provider(),
            agent_id=agent_id,
        )
        _write_json(audit_root / "codex-result.private.json", result)
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            self._assert_shared_source_is_readonly()
            after_hash = self.source_snapshot_hash
        else:
            after_hash = workspace_snapshot_hash(str(role_root / "workspace"))
        if after_hash != before_hash:
            raise EventSynthesisError(f"{agent_id} modified its read-only Workspace view")
        if result.get("status") != "ok":
            raise CodexRoleError(
                agent_id=agent_id,
                status=str(result.get("status") or "unknown"),
                message=(
                    str(result.get("errorMessage"))
                    if result.get("errorMessage") is not None
                    else None
                ),
            )
        trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
        if collection.get("complete") is not True:
            raise EventSynthesisError(f"{agent_id} did not produce a complete Codex JSONL trace")
        return result

    def _candidate_validation_errors(
        self,
        *,
        candidate_path: Path,
        events_path: Path,
        expected: dict[str, Any],
        visible_root: Path,
        author_result: dict[str, Any],
    ) -> tuple[list[str], dict[str, Any] | None, list[dict[str, Any]] | None]:
        errors: list[str] = []
        try:
            task = _read_json(candidate_path)
        except EventSynthesisError as exc:
            return [str(exc)], None, None
        errors.extend(_schema_errors(self.contracts.workflow, task))
        if not errors:
            try:
                CandidateTask.model_validate(task)
            except ValueError as exc:
                errors.append(f"candidate semantic contract failed: {exc}")
        for field in (
            "candidate_id",
            "candidate_index",
            "attempt",
            "mode",
            "previous_candidate_id",
            "visible_workspace_manifest_hash",
            "author_run_id",
        ):
            if task.get(field) != expected.get(field):
                errors.append(f"candidate field {field} does not match run-spec")
        if task.get("events_path") != "events.jsonl":
            errors.append("candidate events_path must be events.jsonl")
        files_used = task.get("files_used")
        if isinstance(files_used, list):
            for relative in files_used:
                if not isinstance(relative, str):
                    continue
                path = visible_root / relative
                if not path.is_file() or path.is_symlink():
                    errors.append(f"files_used path is not a visible regular file: {relative}")
        try:
            events = _read_jsonl(events_path)
        except EventSynthesisError as exc:
            return [*errors, str(exc)], task, None
        errors.extend(_visual_preview_trace_errors(events, author_result))
        errors.extend(
            _interference_bridge_validation_errors(
                task=task,
                events=events,
                bridges=self.config.interference_bridges,
            )
        )
        validated_events: list[PublicEvent] = []
        for index, event in enumerate(events, start=1):
            event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else "<unknown>"
            event_schema_errors = _schema_errors(self.contracts.public, event)
            errors.extend(
                f"event {index} ({event_id}): {error}" for error in event_schema_errors
            )
            if event.get("workspace_id") != self.workspace_id:
                errors.append(
                    f"event {index} ({event_id}): workspace_id does not match run-spec"
                )
            if not event_schema_errors:
                try:
                    validated_events.append(PublicEvent.model_validate(event))
                except (ValueError, TypeError) as exc:
                    errors.append(
                        f"event {index} ({event_id}): semantic contract failed: {exc}"
                    )
        if not errors:
            try:
                validate_visible_events(validated_events)
            except (ValueError, TypeError) as exc:
                errors.append(f"cross-event validation failed: {exc}")
        if not errors:
            sessions: dict[str, list[dict[str, Any]]] = {}
            for event in events:
                session_id = event.get("session_id")
                if isinstance(session_id, str):
                    sessions.setdefault(session_id, []).append(event)
            for session_id, session_events in sorted(sessions.items()):
                actions = [event.get("action") for event in session_events]
                if actions.count("session.start") != 1:
                    errors.append(f"session {session_id}: must contain exactly one session.start")
                if actions.count("session.end") != 1:
                    errors.append(f"session {session_id}: must contain exactly one session.end")
                if actions and actions[0] != "session.start":
                    errors.append(f"session {session_id}: first event must be session.start")
                if actions and actions[-1] != "session.end":
                    errors.append(f"session {session_id}: last event must be session.end")
        return errors, task, events

    def _validate_review(self, path: Path, *, candidate_id: str, attempt: int) -> dict[str, Any]:
        review = _read_json(path)
        _validate_schema(self.contracts.workflow, review, "Codex B review")
        if review.get("artifact_type") != "review_result":
            raise EventSynthesisError("Codex B output is not a review_result")
        raw_hash = _sha256_json(review)
        normalized_fields: list[dict[str, Any]] = []
        for issue_index, issue in enumerate(review.get("issues", [])):
            if not isinstance(issue, dict) or not isinstance(issue.get("paths"), list):
                continue
            original_paths = issue["paths"]
            if not all(isinstance(item, str) for item in original_paths):
                continue
            normalized_paths = sorted(original_paths)
            if original_paths != normalized_paths:
                issue["paths"] = normalized_paths
                normalized_fields.append({"issue_index": issue_index, "field": "paths"})
        if normalized_fields:
            _write_json(
                path.parents[2] / "review-normalization.private.json",
                {
                    "normalizer_version": "review-path-order-v1",
                    "raw_review_sha256": raw_hash,
                    "normalized_review_sha256": _sha256_json(review),
                    "normalized_fields": normalized_fields,
                },
            )
        try:
            ReviewResult.model_validate(review)
        except ValueError as exc:
            raise EventSynthesisError(f"Codex B review failed semantic contract validation: {exc}") from exc
        if review.get("candidate_id") != candidate_id or review.get("attempt") != attempt:
            raise EventSynthesisError("Codex B review does not identify the expected candidate attempt")
        return review

    def _accept_candidate(
        self,
        *,
        task: dict[str, Any],
        events: list[dict[str, Any]],
        review: dict[str, Any],
        attempt_root: Path,
        review_root: Path,
    ) -> CandidateRecord:
        candidate_id = str(task["candidate_id"])
        existing_event_ids = {
            event["event_id"]
            for accepted_record in self.accepted
            for event in accepted_record.events
        }
        duplicate_event_ids = sorted(existing_event_ids & {event["event_id"] for event in events})
        if duplicate_event_ids:
            raise EventSynthesisError(
                "accepted candidates must have globally unique input event IDs: " + ", ".join(duplicate_event_ids)
            )
        target = self.output / "accepted.private" / f"{self.candidate_index:04d}-{candidate_id}"
        target.mkdir(parents=True, mode=0o700)
        _write_json(target / "candidate-task.json", task)
        _write_jsonl(target / "events.jsonl", events, private=True)
        _write_json(target / "review-result.json", review)
        record = CandidateRecord(
            candidate_index=self.candidate_index,
            attempt=int(task["attempt"]),
            candidate_id=candidate_id,
            task=task,
            events=tuple(events),
            artifact_root=target,
        )
        _write_json(
            target / "acceptance.private.json",
            {
                "candidate_id": candidate_id,
                "candidate_index": self.candidate_index,
                "attempt": task["attempt"],
                "source_attempt_root": str(attempt_root),
                "source_review_root": str(review_root),
            },
        )
        return record

    def _update_mask(self) -> None:
        if self.config.workspace_view_mode == "shared_readonly_unmasked":
            return
        manifest = deterministic_mask(
            accepted_files=self._accepted_files(),
            previous_mask=self.masked_paths,
            mask_rate=self.config.mask_rate,
            random_seed=self.config.random_seed,
            accepted_candidate_index=len(self.accepted),
        )
        _validate_schema(self.contracts.workflow, manifest, "mask manifest")
        try:
            MaskManifest.model_validate(manifest)
        except ValueError as exc:  # pragma: no cover - deterministic construction should make this unreachable
            raise EventSynthesisError(f"mask manifest failed semantic contract validation: {exc}") from exc
        self.masked_paths = set(manifest["masked_paths"])
        _write_json(self.output / "masks.private" / f"mask-{len(self.accepted):04d}.json", manifest)

    def _abandon_candidate(self, *, attempt: int, candidate_id: str, review: dict[str, Any]) -> None:
        _write_json(
            self.output / "abandoned.private" / f"candidate-{self.candidate_index:04d}.json",
            {
                "schema_version": 1,
                "artifact_type": "abandoned_candidate",
                "candidate_index": self.candidate_index,
                "attempts": attempt,
                "last_candidate_id": candidate_id,
                "last_review": review,
                "reason": "max_b_failures_per_candidate_reached",
            },
        )
        self.abandoned_candidate_count += 1
        self.current_candidate_b_failures = 0
        self.candidate_index += 1
        self._save_state("RUNNING")

    def _abandon_candidate_for_role_timeout(
        self,
        *,
        attempt: int,
        candidate_id: str,
        failed_role: Literal["codex-a", "codex-b"],
        error: CodexRoleError,
    ) -> None:
        _write_json(
            self.output / "abandoned.private" / f"candidate-{self.candidate_index:04d}.json",
            {
                "schema_version": 1,
                "artifact_type": "abandoned_candidate",
                "candidate_index": self.candidate_index,
                "attempts": attempt,
                "last_candidate_id": candidate_id,
                "last_review": None,
                "failed_role": failed_role,
                "error": str(error),
                "reason": "codex_role_timeout",
            },
        )
        self.abandoned_candidate_count += 1
        self.current_candidate_b_failures = 0
        self.candidate_index += 1
        self._save_state("RUNNING")

    def _run_candidate_loop(self) -> None:
        while (
            not self._progress()["termination_target_met"]
            and self.candidate_index <= self.config.max_candidate_slots
        ):
            attempt = 1
            previous_task: dict[str, Any] | None = None
            previous_events_path: Path | None = None
            previous_review: dict[str, Any] | None = None
            previous_validation: dict[str, Any] | None = None
            previous_candidate_id: str | None = None
            while attempt <= self.config.max_b_failures_per_candidate:
                candidate_id = _opaque_candidate_id(self.config.run_id, self.candidate_index, attempt)
                role_prefix = f"candidate-{self.candidate_index:04d}/attempt-{attempt:02d}"
                a_key = role_prefix + "/codex-a"
                a_root, visible = self._prepare_role_root(a_key, masked_paths=self.masked_paths)
                a_spec = {
                    "candidate_id": candidate_id,
                    "candidate_index": self.candidate_index,
                    "attempt": attempt,
                    "mode": "new" if attempt == 1 else "repair",
                    "previous_candidate_id": previous_candidate_id,
                    "visible_workspace_manifest_hash": visible["visible_snapshot_hash"],
                    "workspace_id": self.workspace_id,
                    "author_run_id": f"{self.config.run_id}:codex-a:{self.candidate_index}:{attempt}",
                    "construction_mode": self.config.construction_mode,
                    "selection_policy": self.config.selection_policy,
                    "preferred_files": self._remaining_preferred_files(),
                    "image_capable": False,
                    "output_files": ["output/candidate-task.json", "output/events.jsonl"],
                }
                if self.config.rubric_contexts:
                    a_spec["rubric_contexts"] = self.config.rubric_contexts
                if self.config.interference_bridges:
                    a_spec["interference_bridges"] = [
                        bridge.model_dump(mode="json")
                        for bridge in self.config.interference_bridges
                    ]
                if attempt > 1:
                    assert previous_task is not None
                    assert previous_events_path is not None
                    assert previous_review is not None
                    assert previous_validation is not None
                    _write_json(a_root / "input/previous-candidate-task.json", previous_task)
                    if previous_events_path.is_file():
                        shutil.copyfile(previous_events_path, a_root / "input/previous-events.jsonl")
                    else:
                        (a_root / "input/previous-events.missing.txt").write_text(
                            "Codex A did not produce events.jsonl in the previous attempt.\n", encoding="utf-8"
                        )
                    _write_json(a_root / "input/reviewer-feedback.json", previous_review)
                    _write_json(a_root / "input/mechanical-validation.json", previous_validation)
                try:
                    author_result = self._run_role(
                        role_key=a_key,
                        agent_id=f"codex-a-{self.candidate_index}-{attempt}",
                        role_root=a_root,
                        prompt=render_codex_a_prompt(a_spec, repair=attempt > 1),
                    )
                except CodexRoleError as exc:
                    if exc.status != "timeout":
                        raise
                    self._abandon_candidate_for_role_timeout(
                        attempt=attempt,
                        candidate_id=candidate_id,
                        failed_role="codex-a",
                        error=exc,
                    )
                    break
                candidate_path = a_root / "output/candidate-task.json"
                events_path = a_root / "output/events.jsonl"
                validation_errors, task, events = self._candidate_validation_errors(
                    candidate_path=candidate_path,
                    events_path=events_path,
                    expected=a_spec,
                    visible_root=a_root / "workspace",
                    author_result=author_result,
                )
                validation_result = {"valid": not validation_errors, "errors": validation_errors}
                _write_json(
                    self.output / "runs.private" / a_key / "candidate-validation.private.json",
                    validation_result,
                )

                b_key = role_prefix + "/codex-b"
                b_root, b_visible = self._prepare_role_root(b_key, masked_paths=self.masked_paths)
                if b_visible["visible_snapshot_hash"] != visible["visible_snapshot_hash"]:
                    raise EventSynthesisError("Codex A and B did not receive identical Workspace views")
                if candidate_path.is_file():
                    shutil.copyfile(candidate_path, b_root / "input/candidate-task.json")
                if events_path.is_file():
                    shutil.copyfile(events_path, b_root / "input/events.jsonl")
                _write_json(
                    b_root / "input/mechanical-validation.json",
                    _reviewer_safe_validation_result(validation_errors),
                )
                b_spec = {
                    "candidate_id": candidate_id,
                    "candidate_index": self.candidate_index,
                    "attempt": attempt,
                    "reviewer_run_id": f"{self.config.run_id}:codex-b:{self.candidate_index}:{attempt}",
                    "output_file": "output/review-result.json",
                }
                try:
                    self._run_role(
                        role_key=b_key,
                        agent_id=f"codex-b-{self.candidate_index}-{attempt}",
                        role_root=b_root,
                        prompt=render_codex_b_prompt(b_spec),
                    )
                except CodexRoleError as exc:
                    if exc.status != "timeout":
                        raise
                    self._abandon_candidate_for_role_timeout(
                        attempt=attempt,
                        candidate_id=candidate_id,
                        failed_role="codex-b",
                        error=exc,
                    )
                    break
                review = self._validate_review(
                    b_root / "output/review-result.json", candidate_id=candidate_id, attempt=attempt
                )
                if review["verdict"] == "PASS":
                    if validation_errors or task is None or events is None:
                        raise EventSynthesisError(
                            "Codex B returned PASS for a mechanically invalid candidate: "
                            + " | ".join(validation_errors)
                        )
                    record = self._accept_candidate(
                        task=task,
                        events=events,
                        review=review,
                        attempt_root=a_root,
                        review_root=b_root,
                    )
                    self.accepted.append(record)
                    self.current_candidate_b_failures = 0
                    self._update_mask()
                    self.candidate_index += 1
                    self._save_state("RUNNING")
                    break

                self.current_candidate_b_failures += 1
                self._save_state("RUNNING")
                if self.current_candidate_b_failures >= self.config.max_b_failures_per_candidate:
                    self._abandon_candidate(
                        attempt=attempt,
                        candidate_id=candidate_id,
                        review=review,
                    )
                    break
                previous_task = task or {
                    "candidate_id": candidate_id,
                    "candidate_index": self.candidate_index,
                    "attempt": attempt,
                }
                previous_events_path = events_path
                previous_review = review
                previous_validation = validation_result
                previous_candidate_id = candidate_id
                attempt += 1

    @staticmethod
    def _event_semantics(event: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in event.items()
            if key not in {"event_id", "session_id", "occurred_at"}
        }

    def _validate_timeline(self, c_root: Path, spec: dict[str, Any]) -> str:
        result = _read_json(c_root / "output/timeline-result.json")
        _validate_schema(self.contracts.workflow, result, "Codex C timeline result")
        if result.get("artifact_type") != "timeline_result":
            raise EventSynthesisError("Codex C output is not a timeline_result")
        try:
            TimelineResult.model_validate(result)
        except ValueError as exc:
            raise EventSynthesisError(f"Codex C timeline failed semantic contract validation: {exc}") from exc
        expected_ids = [record.candidate_id for record in self.accepted]
        if result.get("accepted_candidate_ids") != expected_ids:
            raise EventSynthesisError("Codex C timeline does not identify the accepted candidate pool exactly")
        if result.get("editor_run_id") != spec["editor_run_id"]:
            raise EventSynthesisError("Codex C timeline editor_run_id does not match run-spec")
        if result["status"] == "TIMELINE_BLOCKED":
            final_root = self.output / "final"
            _write_json(final_root / "timeline-result.private.json", result)
            decisions = c_root / "output/timeline-decisions.md"
            if decisions.is_file():
                shutil.copyfile(decisions, final_root / "timeline-decisions.private.md")
                os.chmod(final_root / "timeline-decisions.private.md", 0o600)
            return "TIMELINE_BLOCKED"

        canonical_rows = _read_jsonl(c_root / "output/canonical.private.jsonl")
        for index, row in enumerate(canonical_rows, start=1):
            _validate_schema(self.contracts.canonical, row, f"canonical event {index}")
        try:
            canonical = validate_canonical_events([CanonicalEvent.model_validate(row) for row in canonical_rows])
        except (ValueError, TypeError) as exc:
            raise EventSynthesisError(f"Codex C canonical log failed cross-event validation: {exc}") from exc

        canonical_by_id = {row.event.event_id: row for row in canonical}
        for row in canonical:
            if row.event.action == EventAction.SESSION_START:
                continue
            same_session_links = [
                link
                for link in row.causal_links
                if canonical_by_id[link.event_id].event.session_id == row.event.session_id
            ]
            if not same_session_links:
                raise EventSynthesisError(
                    "Codex C must give every non-session.start event an earlier same-session causal link"
                )

        input_events = {
            event["event_id"]: event
            for record in self.accepted
            for event in record.events
        }
        if len(input_events) != sum(len(record.events) for record in self.accepted):
            raise EventSynthesisError("accepted candidates contain duplicate input event IDs")
        entries = result["entries"]
        if len(entries) != len(input_events) or len(canonical) != len(input_events):
            raise EventSynthesisError("Codex C did not preserve the exact accepted event count")
        output_by_id = {row.event.event_id: row for row in canonical}
        if {entry["input_event_id"] for entry in entries} != set(input_events):
            raise EventSynthesisError("Codex C timeline mapping does not cover every input event exactly")
        if {entry["output_event_id"] for entry in entries} != set(output_by_id):
            raise EventSynthesisError("Codex C timeline mapping does not cover every output event exactly")
        for entry in entries:
            output = output_by_id[entry["output_event_id"]]
            if output.canonical_sequence != entry["canonical_sequence"]:
                raise EventSynthesisError("timeline entry sequence disagrees with canonical event")
            input_event = PublicEvent.model_validate(input_events[entry["input_event_id"]]).model_dump(
                mode="json", exclude_none=True
            )
            output_event = output.event.model_dump(mode="json", exclude_none=True)
            original_time = datetime.fromisoformat(str(entry["original_occurred_at"]).replace("Z", "+00:00"))
            adjusted_time = datetime.fromisoformat(str(entry["adjusted_occurred_at"]).replace("Z", "+00:00"))
            input_time = datetime.fromisoformat(str(input_event["occurred_at"]).replace("Z", "+00:00"))
            output_time = datetime.fromisoformat(str(output_event["occurred_at"]).replace("Z", "+00:00"))
            if original_time.astimezone(timezone.utc) != input_time.astimezone(timezone.utc):
                raise EventSynthesisError("timeline entry original timestamp disagrees with its input event")
            if adjusted_time.astimezone(timezone.utc) != output_time.astimezone(timezone.utc):
                raise EventSynthesisError("timeline entry adjusted timestamp disagrees with its output event")
            if output.generator_version != "codex-c-timeline-v1":
                raise EventSynthesisError("Codex C used an unexpected canonical generator_version")
            if self._event_semantics(input_event) != self._event_semantics(output_event):
                raise EventSynthesisError("Codex C changed event semantics outside the allowed timeline fields")
        final_root = self.output / "final"
        normalized_canonical_rows = [
            row.model_dump(mode="json", exclude_none=True) for row in canonical
        ]
        _write_jsonl(
            final_root / "canonical.private.jsonl", normalized_canonical_rows, private=True
        )
        public_rows = [row.event.model_dump(mode="json", exclude_none=True) for row in canonical]
        _write_jsonl(final_root / "events.public.jsonl", public_rows, private=False)
        _write_json(final_root / "timeline-result.private.json", result)
        decisions = c_root / "output/timeline-decisions.md"
        if decisions.is_file():
            shutil.copyfile(decisions, final_root / "timeline-decisions.private.md")
            os.chmod(final_root / "timeline-decisions.private.md", 0o600)
        return "TIMELINE_COMPLETE"

    def _run_codex_c(self) -> str:
        c_root, _ = self._prepare_role_root("codex-c", masked_paths=set())
        accepted_root = c_root / "accepted"
        accepted_root.mkdir(mode=0o700)
        input_event_ids: list[str] = []
        for record in self.accepted:
            destination = accepted_root / f"{record.candidate_index:04d}-{record.candidate_id}"
            destination.mkdir(mode=0o700)
            shutil.copyfile(record.artifact_root / "candidate-task.json", destination / "candidate-task.json")
            shutil.copyfile(record.artifact_root / "events.jsonl", destination / "events.jsonl")
            for path in destination.iterdir():
                os.chmod(path, 0o444)
            os.chmod(destination, 0o555)
            input_event_ids.extend(event["event_id"] for event in record.events)
        os.chmod(accepted_root, 0o555)
        spec = {
            "workspace_id": self.workspace_id,
            "accepted_candidate_ids": [record.candidate_id for record in self.accepted],
            "input_event_ids": input_event_ids,
            "editor_run_id": f"{self.config.run_id}:codex-c",
            "output_files": [
                "output/canonical.private.jsonl",
                "output/timeline-result.json",
                "output/timeline-decisions.md",
            ],
        }
        self._run_role(
            role_key="codex-c",
            agent_id="codex-c",
            role_root=c_root,
            prompt=render_codex_c_prompt(spec),
        )
        return self._validate_timeline(c_root, spec)

    def _write_interference_bridge_audit(self) -> Path:
        public_log = self.output / "final" / "events.public.jsonl"
        public_rows = _read_jsonl(public_log)
        validated = validate_visible_events(public_rows)
        invalid_provenance = [
            event.event_id
            for event in validated
            if not event.provenance.synthetic
            or event.provenance.generation_method != "agent_inference"
        ]
        if invalid_provenance:
            raise EventSynthesisError(
                "interference bridge public events must all be synthetic agent inference: "
                + ", ".join(invalid_provenance)
            )
        if self.base_natural_event_log is None:
            base_natural_log: dict[str, Any] = {
                "available": False,
                "composition": (
                    "No natural-history component is included in this standalone "
                    "targeted-context overlay."
                ),
            }
        else:
            validate_visible_events(_read_jsonl(self.base_natural_event_log))
            base_natural_log = {
                "available": True,
                "path": str(self.base_natural_event_log),
                "sha256": "sha256:"
                + hashlib.sha256(self.base_natural_event_log.read_bytes()).hexdigest(),
                "composition": (
                    "The natural-history component is recorded for later audited "
                    "composition and is not copied into this standalone overlay."
                ),
            }
        audit = {
            "format": "workspace-bench-targeted-context-audit-v1",
            "visibility": "private_only",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "construction_kind": "targeted_context_construction",
            "targeted_task_ids": self.config.targeted_task_ids,
            "agent_visible_event_log": "final/events.public.jsonl",
            "agent_visible_event_log_sha256": "sha256:"
            + hashlib.sha256(public_log.read_bytes()).hexdigest(),
            "base_natural_log": base_natural_log,
            "public_content_policy": [
                (
                    "Public events are synthetic agent-inferred historical context and "
                    "do not expose task IDs, rubric IDs, judge results, pass/fail "
                    "instructions, reference answers, or private file-role mappings."
                ),
                (
                    "Public history records a natural file investigation: observed "
                    "scope or content differences motivate a later source choice."
                ),
                (
                    "No private bridge identifier or distractor/correct label may be "
                    "staged into the runtime event log."
                ),
            ],
            "construction_scope": [
                {
                    "historical_context_type": (
                        "interference-to-correct-file-selection history"
                    ),
                    "bridges": [
                        bridge.model_dump(mode="json")
                        for bridge in self.config.interference_bridges
                    ],
                    "source_workspace_snapshot_hash": self.source_snapshot_hash,
                    "public_event_ids": [event.event_id for event in validated],
                }
            ],
            "reporting_boundary": (
                "This is a synthetic interference-bridge targeted-context "
                "intervention. It must be reported separately from natural-history, "
                "no-history, and rubric-context conditions; the private audit and "
                "bridge inputs must not reach Codex task execution or the judge."
            ),
        }
        audit_path = self.output / "final" / "targeted-context.private.json"
        _write_json(audit_path, audit)
        return audit_path

    def run(self) -> dict[str, Any]:
        if os.environ.get("CODEX_SANDBOX_MODE") != "danger-full-access":
            raise EventSynthesisError(
                "CODEX_SANDBOX_MODE must be danger-full-access so every Codex role keeps the native shell"
            )
        if self.resume:
            self._resume_output()
        else:
            self._initialise_output()
        try:
            self._run_candidate_loop()
            progress = self._progress()
            targeted_audit_path: Path | None = None
            if progress["termination_target_met"]:
                status = self._run_codex_c()
                if self.config.construction_mode == "interference_bridge":
                    targeted_audit_path = self._write_interference_bridge_audit()
                stop_reason = (
                    "accepted_candidate_target_met"
                    if self.config.stop_after_accepted_candidates is not None
                    else "coverage_target_met"
                )
            else:
                status = (
                    "TARGETED_CONTEXT_NOT_GENERATED"
                    if self.config.construction_mode != "workspace_inference"
                    else "COVERAGE_TARGET_NOT_MET"
                )
                stop_reason = "candidate_limit_reached"
            self._assert_source_unchanged()
            self._save_state(status)
            result = {
                "status": status,
                "abandoned_candidate_count": self.abandoned_candidate_count,
                "candidate_slots_completed": self.candidate_index - 1,
                "max_b_failures_per_candidate": self.config.max_b_failures_per_candidate,
                "max_candidate_slots": self.config.max_candidate_slots,
                "stop_reason": stop_reason,
                **progress,
                "codex_c_ran": bool(progress["termination_target_met"]),
                "output_root": str(self.output),
            }
            if targeted_audit_path is not None:
                result["targeted_audit"] = str(targeted_audit_path)
            _write_json(self.output / "result.json", result, private=False)
            return result
        except Exception as exc:
            error = str(exc)
            try:
                self._assert_source_unchanged()
            except EventSynthesisError as source_exc:
                error += f" | {source_exc}"
            except OSError as source_exc:
                error += f" | source workspace could not be re-validated: {source_exc}"
            self._save_state("FAILED", error=error)
            raise
