"""Read-only collection-map schemas, validation, and search indexes.

``workspace-collection-set.v2`` is the task-independent, snapshot-scoped
navigation artifact.  ``collection-set.v1`` is retained as the explicitly
separate ``task_input_anchored_map`` efficacy condition.  They have
different schemas, staging paths, runtime backends, and reporting boundaries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal

import tiktoken
from pydantic import Field, field_validator, model_validator

from .manifest import StrictModel, canonical_json


logger = logging.getLogger(__name__)


COLLECTION_SET_FORMAT = "workspace-bench.collection-set.v1"
WORKSPACE_COLLECTION_SET_FORMAT = "workspace-bench.workspace-collection-set.v2"
WORKSPACE_COLLECTION_SET_V3_FORMAT = "workspace-bench.workspace-collection-set.v3"
WORKSPACE_CATALOG_FORMAT = "workspace-bench.workspace-catalog.v1"
PRIVATE_AUDIT_FORMAT = "workspace-bench.collection-map-audit.v1"
WORKSPACE_COLLECTION_PRIVATE_AUDIT_FORMAT = "workspace-bench.workspace-collection-map-audit.v2"
COLLECTION_MAP_MANIFEST_FORMAT = "workspace-bench.collection-map-manifest.v1"
SEARCH_INDEX_FORMAT = "workspace-bench.collection-search-index.v1"
WORKSPACE_COLLECTION_SEARCH_INDEX_FORMAT = "workspace-bench.workspace-collection-search-index.v2"
WORKSPACE_COLLECTION_SEARCH_INDEX_V3_FORMAT = "workspace-bench.workspace-collection-search-index.v3"
PERSONA_COLLECTION_INDEX_PUBLIC_FORMAT = "workspace-bench.persona-collection-index.public.v1"
PERSONA_COLLECTION_SEARCH_INDEX_FORMAT = "workspace-bench.persona-collection-search-index.v1"
SEARCH_SCHEMA_VERSION = 1
MAX_QUERY_CHARS = 512
MAX_CARD_COUNT = 24
MAX_MEMBERS_PER_CARD = 48
MAX_WORKSPACE_V3_CARD_COUNT = 4096
PAGE_SIZE = 4
HOOK_COLLECTION_TOKEN_CAP = 1024
# Legacy task-input prototype only.  Formal workspace_snapshot_map never
# calls its start briefing path; retaining the constant keeps old development
# artefacts inspectable without making them eligible for runtime staging.
TASK_INPUT_START_BRIEFING_TOKEN_CAP = 1536
WORKSPACE_SEARCH_QUERY_REQUIRED_TEXT = "请提供已观察到的文件名、目录名或业务词以搜索工作区集合。"
WORKSPACE_MAP_UNAVAILABLE_TEXT = "当前工作区未提供可展示的完整集合地图。"
_QUERY_TOKEN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CARD_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def normalize_search_query(query: str | None) -> tuple[str | None, str | None]:
    """Normalize one free-text workspace search query and build its FTS expression."""

    if query is None:
        return None, None
    if not isinstance(query, str):
        raise CollectionMapError("query must be text")
    display = " ".join(query.split())
    if not display:
        return None, None
    if len(display) > MAX_QUERY_CHARS:
        raise CollectionMapError("query must be at most 512 characters")
    expression = _fts_query_expression(display)
    if not expression:
        raise CollectionMapError("query must include at least one letter or number")
    return display, expression


class CollectionMapError(ValueError):
    """Raised for invalid build inputs or agent-visible search arguments."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _safe_relative_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("path must be a non-empty POSIX relative path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("path must stay inside the workspace")
    return candidate.as_posix()


def _normalize_path_lookup(value: str | None) -> str | None:
    """Validate a model-supplied file name or workspace-relative path.

    Unlike a filesystem resolver, this function never guesses outside the
    indexed workspace.  A basename is legal, but absolute paths, traversal,
    backslashes, NULs, and directory-only spellings are rejected.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise CollectionMapError("path must be text")
    display = value.strip()
    if not display:
        return None
    if len(display) > 4096:
        raise CollectionMapError("path must be at most 4096 characters")
    try:
        return _safe_relative_path(display)
    except ValueError as exc:
        raise CollectionMapError(str(exc)) from exc


def _path_match_rank(candidate: str, needle: str) -> tuple[int, int, str] | None:
    """Rank exact path/basename matches before bounded substring fallbacks."""

    folded_candidate = candidate.casefold()
    folded_needle = needle.casefold()
    basename = PurePosixPath(candidate).name.casefold()
    needle_basename = PurePosixPath(needle).name.casefold()
    if folded_candidate == folded_needle:
        return (0, len(candidate), candidate)
    if basename == folded_needle:
        return (1, len(candidate), candidate)
    if folded_candidate.endswith("/" + folded_needle):
        return (2, len(candidate), candidate)
    if needle_basename and needle_basename in basename:
        return (3, len(candidate), candidate)
    if folded_needle in folded_candidate:
        return (4, len(candidate), candidate)
    return None


def _keep_best_path_match_class(
    matches: list[Any],
) -> list[Any]:
    """Avoid mixing fuzzy candidates into an exact path/name result."""

    if not matches:
        return []
    best_class = min(item[0][0] for item in matches)
    return [item for item in matches if item[0][0] == best_class]


class CatalogFile(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    extension: str = Field(min_length=1, max_length=32)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class WorkspaceCatalog(StrictModel):
    format: Literal[WORKSPACE_CATALOG_FORMAT] = WORKSPACE_CATALOG_FORMAT
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    files: list[CatalogFile] = Field(max_length=200_000)

    @model_validator(mode="after")
    def validate_order(self) -> "WorkspaceCatalog":
        paths = [entry.path for entry in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("catalog files must be unique and sorted by path")
        return self


class WorkspacePathEvidence(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    reason: str = Field(min_length=1, max_length=360)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class PossibleRelation(StrictModel):
    relation: Literal["possible_source", "possible_copy", "possible_archive", "possible_template"]
    from_path: str = Field(min_length=1, max_length=4096)
    to_path: str = Field(min_length=1, max_length=4096)
    basis: str = Field(min_length=1, max_length=360)

    @field_validator("from_path", "to_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class WorkspaceCollectionMember(StrictModel):
    """A workspace-observable member of a generic collection card.

    The role vocabulary deliberately has no task-derived concept such as
    ``task_input``.  These labels describe only observable workspace context.
    """

    path: str = Field(min_length=1, max_length=4096)
    role: Literal[
        "current_candidate",
        "archive_candidate",
        "template_candidate",
        "copy_candidate",
        "source_candidate",
        "related",
    ]
    note: str = Field(min_length=1, max_length=360)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class WorkspaceCollectionCard(StrictModel):
    """One task-independent, agent-visible workspace navigation card."""

    card_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    members: list[WorkspaceCollectionMember] = Field(min_length=1, max_length=MAX_MEMBERS_PER_CARD)
    boundaries: list[str] = Field(default_factory=list, max_length=12)
    possible_relations: list[PossibleRelation] = Field(default_factory=list, max_length=32)

    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, value: str) -> str:
        if not _CARD_ID.fullmatch(value):
            raise ValueError("card_id must be a stable lowercase identifier")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> "WorkspaceCollectionCard":
        paths = [member.path for member in self.members]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("card members must be unique and sorted by path")
        return self


class WorkspaceCollectionSet(StrictModel):
    """The task-independent collection-map payload visible at runtime."""

    format: Literal[WORKSPACE_COLLECTION_SET_FORMAT] = WORKSPACE_COLLECTION_SET_FORMAT
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cards: list[WorkspaceCollectionCard] = Field(min_length=1, max_length=MAX_CARD_COUNT)

    @model_validator(mode="after")
    def validate_set(self) -> "WorkspaceCollectionSet":
        ids = [card.card_id for card in self.cards]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("cards must be unique and sorted by card_id")
        _validate_workspace_visible_prose(self)
        return self


class WorkspaceCollectionSummaryCard(StrictModel):
    """One compact public card; member paths live only in the companion index."""

    card_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=600)
    representative_content: list[str] = Field(default_factory=list, max_length=6)
    file_count: int = Field(ge=1, le=200_000)

    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, value: str) -> str:
        if not _CARD_ID.fullmatch(value):
            raise ValueError("card_id must be a stable lowercase identifier")
        return value


class WorkspaceCollectionSetV3(StrictModel):
    """Exhaustive public collection summaries without member-path disclosure."""

    format: Literal[WORKSPACE_COLLECTION_SET_V3_FORMAT] = WORKSPACE_COLLECTION_SET_V3_FORMAT
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    distinct_file_count: int = Field(ge=1, le=200_000)
    membership_count: int = Field(ge=1)
    cards: list[WorkspaceCollectionSummaryCard] = Field(
        min_length=1,
        max_length=MAX_WORKSPACE_V3_CARD_COUNT,
    )

    @model_validator(mode="after")
    def validate_set(self) -> "WorkspaceCollectionSetV3":
        ids = [card.card_id for card in self.cards]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("cards must be unique and sorted by card_id")
        if sum(card.file_count for card in self.cards) != self.membership_count:
            raise ValueError("membership_count must equal the sum of card file counts")
        _validate_workspace_v3_visible_prose(self)
        return self


def _validate_workspace_v3_visible_prose(collection: WorkspaceCollectionSetV3) -> None:
    # 保留真正表示"任务派生/评测元信息"的中英标记；移除 ML/评测领域常见词
    # （rubric / judge / reference answer）——当工作区本身就是评测类语料时，模型
    # 在地图描述里用这些词是对文件的忠实描述，不是任务标签（task 124 实测被误拒）。
    # 泄题防护由 artifact_gate 承担（它会把短语与工作区文本/任务描述做归属比对）。
    prohibited = (
        "任务实际输入",
        "本任务输入",
        "task input",
        "data manifest",
        "input_match",
        "参考答案",
    )
    # 只检查作者写的 title/description：representative_content 是**文件原文的逐字引用**，
    # 工作区里本来就可能有 "rubric"/"judge"/"reference answer" 这类词（例如论文自己在讲
    # benchmark 评测），把它们当泄题信号会造成误报（task 124 实测被此挡住）。
    prose: list[str] = []
    for card in collection.cards:
        prose.extend((card.title, card.description))
    folded = "\n".join(prose).casefold()
    for token in prohibited:
        if token.casefold() in folded:
            raise ValueError("workspace collection prose contains a task-derived or evaluation label")


def _validate_workspace_visible_prose(collection: WorkspaceCollectionSet) -> None:
    """Reject prose that signals a task-derived map rather than workspace facts.

    Paths are intentionally excluded: a legitimate workspace may contain a
    directory called ``task``.  The check applies only to model-written prose
    and labels, which must never describe task inputs or evaluation metadata.
    """

    prohibited = (
        "任务实际输入",
        "本任务输入",
        "task input",
        "data manifest",
        "input_match",
        "rubric",
        "reference answer",
        "judge",
        "参考答案",
        "主要集合",
        "支持集合",
        "并行集合",
    )
    prose: list[str] = []
    for card in collection.cards:
        prose.append(card.title)
        prose.extend(member.note for member in card.members)
        prose.extend(card.boundaries)
        prose.extend(relation.basis for relation in card.possible_relations)
    folded = "\n".join(prose).casefold()
    for token in prohibited:
        if token.casefold() in folded:
            raise ValueError("workspace collection prose contains a task-derived or evaluation label")


class PrivateCollectionAudit(StrictModel):
    """Private provenance.  Never stage or expose this to task Codex."""

    format: Literal[PRIVATE_AUDIT_FORMAT] = PRIVATE_AUDIT_FORMAT
    created_at: str = Field(min_length=1, max_length=64)
    construction_kind: Literal["task_input_anchored_collection_map"] = "task_input_anchored_collection_map"
    task_input_conditioned: Literal[True] = True
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_bundle_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    agent_visible_collection_set_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    codex_a_artifact_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    codex_b_artifact_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_verdict: Literal["PASS"] = "PASS"


class WorkspaceCollectionPrivateAudit(StrictModel):
    """Private provenance for a task-independent workspace collection map."""

    format: Literal[WORKSPACE_COLLECTION_PRIVATE_AUDIT_FORMAT] = WORKSPACE_COLLECTION_PRIVATE_AUDIT_FORMAT
    created_at: str = Field(min_length=1, max_length=64)
    construction_kind: Literal["workspace_snapshot_collection_map"] = "workspace_snapshot_collection_map"
    task_input_conditioned: Literal[False] = False
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workspace_catalog_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    agent_visible_collection_set_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    codex_a_artifact_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    codex_b_artifact_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_verdict: Literal["PASS"] = "PASS"
@dataclass(frozen=True, slots=True)
class StagedCollectionMap:
    collection_set_path: str
    collection_set_sha256: str
    search_index_path: str
    search_index_sha256: str


def _atomic_write(path: Path, raw: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_json(path: str | Path, value: Any, *, private: bool) -> str:
    target = Path(path)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_write(target, raw, mode=0o600 if private else 0o644)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _truncate_overlong_strings(raw: Any, exc: ValueError) -> list[str]:
    """把「只是超长」的字符串字段截到 schema 上限，返回被截断的字段路径。

    为什么值得这样做：字符上限（``description`` ≤ 600 之类）是**可读性**约束，
    不是语义约束。而构造角色产出这些 JSON 之后，上游为此已经烧掉了一小时的
    桶划分与协调工作；实测出现过 608/600 —— 超 8 个字符就把整题判死，
    代价与收益完全不成比例。

    只处理 ``string_too_long`` 一种错误，其余校验失败照旧抛出，不做任何猜测性修补。
    """

    items = getattr(exc, "errors", None)
    if not callable(items):
        return []
    repaired: list[str] = []
    for item in items():
        if item.get("type") != "string_too_long":
            continue
        limit = (item.get("ctx") or {}).get("max_length")
        location = item.get("loc") or ()
        if not isinstance(limit, int) or not location:
            continue
        target = raw
        for part in location[:-1]:
            try:
                target = target[part]
            except (KeyError, IndexError, TypeError):
                target = None
                break
        field = location[-1]
        if not isinstance(target, dict) or not isinstance(target.get(field), str):
            continue
        cut = target[field][:limit]
        # 尽量切在词边界上，但不要为了一个空格砍掉太多内容。
        space = cut.rfind(" ")
        if space > limit * 0.9:
            cut = cut[:space]
        target[field] = cut
        repaired.append(".".join(str(part) for part in location))
    return repaired


def load_json_model(path: str | Path, model: type[StrictModel]) -> StrictModel:
    try:
        info = Path(path).lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CollectionMapError("collection artefact must be a regular file")
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionMapError(f"invalid collection artefact: {path}") from exc
    try:
        return model.model_validate(raw)
    except ValueError as exc:
        repaired = _truncate_overlong_strings(raw, exc)
        if repaired:
            logger.warning(
                "collection artefact %s had over-long strings; truncated to the schema cap: %s",
                path,
                ", ".join(repaired),
            )
            try:
                return model.model_validate(raw)
            except ValueError:
                pass
        # The detailed location/message is private construction feedback.  It
        # makes a failed repair actionable (for example, a card-order
        # violation) without making the validator fabricate a replacement.
        details: list[str] = []
        error_items = getattr(exc, "errors", None)
        if callable(error_items):
            for item in error_items():
                location = ".".join(str(part) for part in item.get("loc", ())) or "collection"
                message = str(item.get("msg", "invalid value"))
                details.append(f"{location}: {message}")
        suffix = "; ".join(details[:8]) if details else "collection artefact does not follow its schema"
        raise CollectionMapError(f"collection schema validation failed: {suffix}") from exc


def catalog_path(catalog_root: str | Path, snapshot_hash: str) -> Path:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot_hash):
        raise CollectionMapError("workspace snapshot hash is invalid")
    return Path(catalog_root).resolve() / snapshot_hash.removeprefix("sha256:") / "workspace-catalog.json"


def build_workspace_catalog(
    workspace_root: str | Path,
    *,
    workspace_snapshot_hash: str,
    catalog_root: str | Path,
) -> tuple[WorkspaceCatalog, Path]:
    """Build once per immutable snapshot; it deliberately contains no prose."""

    root = Path(workspace_root).resolve(strict=True)
    if not root.is_dir():
        raise CollectionMapError("workspace root must be a directory")
    target = catalog_path(catalog_root, workspace_snapshot_hash)
    if target.exists():
        existing = load_json_model(target, WorkspaceCatalog)
        assert isinstance(existing, WorkspaceCatalog)
        if existing.workspace_snapshot_hash != workspace_snapshot_hash:
            raise CollectionMapError("existing workspace catalog disagrees with immutable snapshot")
        return existing, target
    files: list[CatalogFile] = []
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            info = (current_path / name).lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise CollectionMapError("workspace catalog rejects symlink or special directories")
        for name in sorted(filenames):
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise CollectionMapError("workspace catalog rejects symlink or special files")
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.lower().lstrip(".") or "none"
            files.append(
                CatalogFile(
                    path=relative,
                    extension=suffix[:32],
                    size_bytes=info.st_size,
                    sha256=sha256_file(path),
                )
            )
    catalog = WorkspaceCatalog(
        workspace_snapshot_hash=workspace_snapshot_hash,
        files=sorted(files, key=lambda item: item.path),
    )
    write_json(target, catalog.model_dump(mode="json"), private=True)
    return catalog, target


def visible_workspace_collection_text(
    collection: WorkspaceCollectionSet,
    *,
    card_ids: Iterable[str] | None = None,
) -> str:
    """Render only task-independent card content as bounded plain text."""

    by_id = {card.card_id: card for card in collection.cards}
    order = list(card_ids) if card_ids is not None else [card.card_id for card in collection.cards]
    rendered: list[str] = []
    for card_id in order:
        card = by_id.get(card_id)
        if card is None:
            continue
        rendered.append(f"[工作区集合] {card.title}")
        rendered.append("文件：" + "、".join(f"`{member.path}`（{member.note}）" for member in card.members))
        if card.boundaries:
            rendered.append("边界：" + "；".join(card.boundaries))
        if card.possible_relations:
            rendered.append(
                "可能关系："
                + "；".join(
                    f"{relation.relation}: `{relation.from_path}` → `{relation.to_path}`（{relation.basis}）"
                    for relation in card.possible_relations
                )
            )
    return "\n".join(rendered) if rendered else "未找到匹配的工作区集合。"


def visible_workspace_collection_v2_summary_text(
    collection: WorkspaceCollectionSet,
    *,
    card_ids: Iterable[str] | None = None,
) -> str:
    """Render v2 task-independent cards without member-path disclosure."""

    by_id = {card.card_id: card for card in collection.cards}
    order = list(card_ids) if card_ids is not None else [card.card_id for card in collection.cards]
    rendered: list[str] = []
    for card_id in order:
        card = by_id.get(card_id)
        if card is None:
            continue
        rendered.append(
            f"[工作区集合] {card.title}（{len(card.members)} 个文件，card_id={card.card_id}）"
        )
        if card.boundaries:
            rendered.append("集合概览：" + "；".join(card.boundaries))
        else:
            rendered.append("集合概览：一组由离线工作区地图归纳的语义相关文件。")
    return "\n".join(rendered) if rendered else "未找到匹配的工作区集合。"


def visible_workspace_collection_summary_text(
    collection: WorkspaceCollectionSetV3,
    *,
    card_ids: Iterable[str] | None = None,
) -> str:
    """Render compact cards and explicitly advertise on-demand expansion."""

    by_id = {card.card_id: card for card in collection.cards}
    order = list(card_ids) if card_ids is not None else [card.card_id for card in collection.cards]
    rendered: list[str] = []
    for card_id in order:
        card = by_id.get(card_id)
        if card is None:
            continue
        rendered.append(f"[工作区集合] {card.title}（{card.file_count} 个文件，card_id={card.card_id}）")
        rendered.append("简述：" + card.description)
        if card.representative_content:
            rendered.append("代表性内容：" + "；".join(card.representative_content))
        rendered.append(f"查看文件路径：调用 workspace_search(card_id=\"{card.card_id}\")。")
    return "\n".join(rendered) if rendered else "未找到匹配的工作区集合。"


def visible_workspace_collection_directory_text(
    collection: WorkspaceCollectionSetV3,
) -> str:
    """Render every v3 card once without disclosing member paths."""

    rendered = [
        f"[完整工作区地图] 共 {len(collection.cards)} 个集合，覆盖 "
        f"{collection.distinct_file_count} 个文件。"
    ]
    for card in collection.cards:
        rendered.append(
            f"[集合卡] {card.title}（{card.file_count} 个文件，card_id={card.card_id}）"
        )
        rendered.append("概览：" + card.description)
    return "\n".join(rendered)


def visible_workspace_collection_members_text(
    *,
    card: WorkspaceCollectionSummaryCard,
    paths: Iterable[str],
) -> str:
    listed = list(paths)
    rendered = [
        f"[集合文件] {card.title}（card_id={card.card_id}，共 {card.file_count} 个文件）",
        *[f"- `{path}`" for path in listed],
    ]
    return "\n".join(rendered)


def _path_metadata_overview(
    path: str,
    *,
    representative_content: Iterable[str] = (),
) -> str:
    """Return grounded metadata plus an explicitly card-level content clue."""

    value = PurePosixPath(path)
    extension = value.suffix.lstrip(".").upper()
    kind = f"{extension} 文件" if extension else "无扩展名文件"
    parent = value.parent.as_posix()
    location = f"；位于 `{parent}`" if parent != "." else ""
    overview = f"{kind}；文件名为 `{value.name}`{location}。"
    lookup_tokens = set(_fts_query_tokens(value.stem))
    ranked: list[tuple[int, int, str]] = []
    for index, clue in enumerate(representative_content):
        folded = clue.casefold()
        score = sum(token.casefold() in folded for token in lookup_tokens)
        ranked.append((score, -index, clue))
    if ranked:
        score, _, clue = max(ranked)
        minimum = max(2, min(4, len(lookup_tokens) // 3))
        if score >= minimum:
            overview += f" 内容线索（来自集合摘要）：{clue}；仍需读取原文件核实。"
    return overview


def visible_workspace_collection_path_matches_text(
    *,
    card: WorkspaceCollectionCard,
    matches: Iterable[WorkspaceCollectionMember],
) -> str:
    rendered = [
        f"[工作区集合] {card.title}（card_id={card.card_id}）",
        "集合概览：该集合包含语义相关的工作区文件；以下只列出本次 path 检索命中的文件。",
        "[匹配文件]",
    ]
    for member in matches:
        rendered.append(f"- 具体路径：`{member.path}`")
        rendered.append(f"  文件概览：{member.note}")
    return "\n".join(rendered)


def visible_workspace_collection_v3_path_matches_text(
    *,
    card: WorkspaceCollectionSummaryCard,
    paths: Iterable[str],
) -> str:
    rendered = [
        f"[工作区集合] {card.title}（{card.file_count} 个文件，card_id={card.card_id}）",
        "集合概览：" + card.description,
    ]
    if card.representative_content:
        rendered.append("代表性内容：" + "；".join(card.representative_content))
    rendered.append("[匹配文件]")
    for path in paths:
        rendered.append(f"- 具体路径：`{path}`")
        rendered.append(
            "  文件概览："
            + _path_metadata_overview(
                path,
                representative_content=card.representative_content,
            )
        )
    return "\n".join(rendered)


def bounded_collection_text(text: str, *, token_cap: int, continuation_hint: str) -> str:
    encoding = tiktoken.get_encoding("cl100k_base")
    token_ids = encoding.encode(text, disallowed_special=())
    if len(token_ids) <= token_cap:
        return text
    marker = "\n[…集合导航已截断；" + continuation_hint + "…]"
    marker_tokens = encoding.encode(marker, disallowed_special=())
    prefix_cap = max(1, token_cap - len(marker_tokens))
    prefix = encoding.decode(token_ids[:prefix_cap]).rstrip()
    return prefix + marker


def _workspace_card_search_text(card: WorkspaceCollectionCard) -> str:
    parts = [card.title]
    for member in card.members:
        parts.extend((member.path, member.role, member.note))
    parts.extend(card.boundaries)
    for relation in card.possible_relations:
        parts.extend((relation.relation, relation.from_path, relation.to_path, relation.basis))
    return "\n".join(parts)


def _fts_searchable_text(value: str) -> str:
    """Expose CJK substrings to SQLite FTS without changing visible prose."""

    return _CJK_CHARACTER.sub(lambda match: " " + match.group(0) + " ", value)


def _fts_query_tokens(display: str) -> list[str]:
    tokens: list[str] = []
    for token in _QUERY_TOKEN.findall(display):
        if _CJK_CHARACTER.search(token):
            tokens.extend(character for character in token if _CJK_CHARACTER.fullmatch(character))
        else:
            tokens.append(token)
    return tokens


def _fts_query_expression(display: str) -> str:
    """Build a broad keyword-union query while retaining phrase cohesion.

    Model-generated topic searches commonly contain several whitespace-
    separated concepts.  Requiring every character from every concept to
    occur in one card makes those ordinary queries nearly impossible to
    satisfy.  Treat each whitespace-separated concept as an OR alternative;
    within a CJK concept, retain the existing character-level AND so a card
    must still contain the concept's complete character vocabulary.
    """

    groups: list[str] = []
    for keyword in display.split():
        # Repeated CJK characters add no retrieval value and make the emitted
        # FTS expression harder to inspect.  Preserve first-seen order.
        tokens = list(dict.fromkeys(_fts_query_tokens(keyword)))
        if not tokens:
            continue
        quoted = ['"' + token.replace('"', '""') + '"' for token in tokens]
        groups.append("(" + " AND ".join(quoted) + ")")
    return " OR ".join(groups)
@dataclass(frozen=True, slots=True)
class CollectionSearchPage:
    content: str
    original_tokens: int
    returned_tokens: int
    next_state: dict[str, int | str] | None


def _collection_search_page(
    *,
    card_ids: list[str],
    card_text: dict[str, str],
    offset: int,
    token_cap: int,
    encoding: tiktoken.Encoding,
    fragment_card_id: str | None,
    fragment_token_offset: int | None,
) -> CollectionSearchPage:
    """Pack ranked cards greedily and make an oversized card resumable.

    A page always considers up to ``PAGE_SIZE`` ranked results.  Earlier code
    collapsed every oversized four-card result into one card and then cut that
    card without a usable continuation.  This helper preserves ranking,
    records the full candidate cost, and carries an opaque fragment cursor
    state when a single card cannot fit in the caller's observation budget.
    """

    if offset < 0 or token_cap < 1:
        raise RuntimeError("collection-search cursor is invalid")
    visible_ids = card_ids[:PAGE_SIZE]
    if not visible_ids:
        content = "未找到匹配的工作区集合。"
        tokens = len(encoding.encode(content, disallowed_special=()))
        return CollectionSearchPage(content, tokens, tokens, None)
    if fragment_card_id is not None:
        if fragment_card_id != visible_ids[0] or fragment_token_offset is None or fragment_token_offset < 0:
            raise RuntimeError("collection-search fragment cursor is invalid")
        token_ids = encoding.encode(card_text[fragment_card_id], disallowed_special=())
        if fragment_token_offset >= len(token_ids):
            raise RuntimeError("collection-search fragment cursor is exhausted")
        end = min(len(token_ids), fragment_token_offset + token_cap)
        content = encoding.decode(token_ids[fragment_token_offset:end]).rstrip()
        next_state: dict[str, int | str] | None
        if end < len(token_ids):
            next_state = {
                "offset": offset,
                "fragment_card_id": fragment_card_id,
                "fragment_token_offset": end,
            }
        elif len(card_ids) > 1:
            next_state = {"offset": offset + 1}
        else:
            next_state = None
        return CollectionSearchPage(
            content=content,
            original_tokens=sum(
                len(encoding.encode(card_text[card_id], disallowed_special=())) for card_id in visible_ids
            ),
            returned_tokens=len(encoding.encode(content, disallowed_special=())),
            next_state=next_state,
        )

    original_tokens = sum(
        len(encoding.encode(card_text[card_id], disallowed_special=())) for card_id in visible_ids
    )
    packed: list[str] = []
    used_tokens = 0
    for card_id in visible_ids:
        card_tokens = len(encoding.encode(card_text[card_id], disallowed_special=()))
        if used_tokens + card_tokens <= token_cap:
            packed.append(card_id)
            used_tokens += card_tokens
            continue
        if not packed:
            token_ids = encoding.encode(card_text[card_id], disallowed_special=())
            content = encoding.decode(token_ids[:token_cap]).rstrip()
            return CollectionSearchPage(
                content=content,
                original_tokens=original_tokens,
                returned_tokens=len(encoding.encode(content, disallowed_special=())),
                next_state={
                    "offset": offset,
                    "fragment_card_id": card_id,
                    "fragment_token_offset": min(token_cap, len(token_ids)),
                },
            )
        break
    content = "\n".join(card_text[card_id] for card_id in packed)
    next_state = {"offset": offset + len(packed)} if len(card_ids) > len(packed) else None
    return CollectionSearchPage(
        content=content,
        original_tokens=original_tokens,
        returned_tokens=len(encoding.encode(content, disallowed_special=())),
        next_state=next_state,
    )


def build_workspace_collection_search_index(
    collection: WorkspaceCollectionSet,
    *,
    index_path: str | Path,
) -> str:
    """Build a deterministic FTS5 index for a task-independent map."""

    target = Path(index_path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE cards (card_id TEXT PRIMARY KEY, sort_index INTEGER NOT NULL, content TEXT NOT NULL)"
            )
            connection.execute("CREATE VIRTUAL TABLE cards_fts USING fts5(card_id UNINDEXED, content)")
            for index, card in enumerate(collection.cards):
                content = _workspace_card_search_text(card)
                connection.execute(
                    "INSERT INTO cards (card_id, sort_index, content) VALUES (?, ?, ?)",
                    (card.card_id, index, content),
                )
                connection.execute(
                    "INSERT INTO cards_fts (card_id, content) VALUES (?, ?)",
                    (card.card_id, _fts_searchable_text(content)),
                )
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("format", WORKSPACE_COLLECTION_SEARCH_INDEX_FORMAT),
            )
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                (
                    "workspace_collection_set_sha256",
                    "sha256:" + hashlib.sha256(canonical_json(collection.model_dump(mode="json")).encode("utf-8")).hexdigest(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return sha256_file(target)


def build_workspace_collection_v3_index(
    collection: WorkspaceCollectionSetV3,
    *,
    memberships: dict[str, list[str]],
    index_path: str | Path,
) -> str:
    """Build the v3 FTS and exhaustive card-membership index."""

    expected_ids = {card.card_id for card in collection.cards}
    if set(memberships) != expected_ids:
        raise CollectionMapError("v3 memberships must contain every card exactly once")
    distinct_paths: set[str] = set()
    for card in collection.cards:
        paths = memberships[card.card_id]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise CollectionMapError("v3 card memberships must be unique and sorted")
        if len(paths) != card.file_count:
            raise CollectionMapError("v3 card file_count disagrees with memberships")
        for path in paths:
            distinct_paths.add(_safe_relative_path(path))
    if len(distinct_paths) != collection.distinct_file_count:
        raise CollectionMapError("v3 distinct_file_count disagrees with memberships")
    if sum(len(paths) for paths in memberships.values()) != collection.membership_count:
        raise CollectionMapError("v3 membership_count disagrees with memberships")

    target = Path(index_path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE cards ("
                "card_id TEXT PRIMARY KEY, sort_index INTEGER NOT NULL, title TEXT NOT NULL, "
                "description TEXT NOT NULL, representative_content TEXT NOT NULL, file_count INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE members ("
                "card_id TEXT NOT NULL, path TEXT NOT NULL, member_index INTEGER NOT NULL, "
                "PRIMARY KEY (card_id, path), FOREIGN KEY(card_id) REFERENCES cards(card_id))"
            )
            connection.execute("CREATE INDEX members_path ON members(path, card_id)")
            connection.execute("CREATE VIRTUAL TABLE cards_fts USING fts5(card_id UNINDEXED, content)")
            for index, card in enumerate(collection.cards):
                representative = "\n".join(card.representative_content)
                content = "\n".join((card.title, card.description, representative))
                connection.execute(
                    "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?)",
                    (card.card_id, index, card.title, card.description, representative, card.file_count),
                )
                connection.execute(
                    "INSERT INTO cards_fts (card_id, content) VALUES (?, ?)",
                    (card.card_id, _fts_searchable_text(content)),
                )
                connection.executemany(
                    "INSERT INTO members (card_id, path, member_index) VALUES (?, ?, ?)",
                    ((card.card_id, path, member_index) for member_index, path in enumerate(memberships[card.card_id])),
                )
            metadata = {
                "format": WORKSPACE_COLLECTION_SEARCH_INDEX_V3_FORMAT,
                "workspace_snapshot_hash": collection.workspace_snapshot_hash,
                "workspace_collection_set_sha256": "sha256:"
                + hashlib.sha256(canonical_json(collection.model_dump(mode="json")).encode("utf-8")).hexdigest(),
                "distinct_file_count": str(collection.distinct_file_count),
                "membership_count": str(collection.membership_count),
            }
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                sorted(metadata.items()),
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return sha256_file(target)


class WorkspaceCollectionMapSearch:
    """Read-only lookup over a staged, task-independent workspace map."""

    def __init__(self, *, collection_set_path: str, collection_set_sha256: str, index_path: str, index_sha256: str) -> None:
        self.collection_path = Path(collection_set_path)
        self.index_path = Path(index_path)
        for path, expected in ((self.collection_path, collection_set_sha256), (self.index_path, index_sha256)):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise RuntimeError("staged workspace collection artefact is unsafe")
            if sha256_file(path) != expected:
                raise RuntimeError("staged workspace collection artefact integrity check failed")
        try:
            raw_format = json.loads(self.collection_path.read_text(encoding="utf-8")).get("format")
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError("staged workspace collection map is invalid") from exc
        if raw_format == WORKSPACE_COLLECTION_SET_V3_FORMAT:
            loaded = load_json_model(self.collection_path, WorkspaceCollectionSetV3)
            assert isinstance(loaded, WorkspaceCollectionSetV3)
            self.collection: WorkspaceCollectionSet | WorkspaceCollectionSetV3 = loaded
            self.version = 3
        else:
            loaded = load_json_model(self.collection_path, WorkspaceCollectionSet)
            assert isinstance(loaded, WorkspaceCollectionSet)
            self.collection = loaded
            self.version = 2
        self.encoding = tiktoken.get_encoding("cl100k_base")
        self._validate_index()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.index_path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    def _validate_index(self) -> None:
        try:
            with self._connect() as connection:
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        except sqlite3.Error as exc:
            raise RuntimeError("staged workspace collection search index is invalid") from exc
        expected = (
            WORKSPACE_COLLECTION_SEARCH_INDEX_V3_FORMAT
            if self.version == 3
            else WORKSPACE_COLLECTION_SEARCH_INDEX_FORMAT
        )
        if metadata.get("format") != expected:
            raise RuntimeError("staged workspace collection search index format is invalid")
        if self.version == 3:
            assert isinstance(self.collection, WorkspaceCollectionSetV3)
            canonical_hash = "sha256:" + hashlib.sha256(
                canonical_json(self.collection.model_dump(mode="json")).encode("utf-8")
            ).hexdigest()
            if (
                metadata.get("workspace_collection_set_sha256") != canonical_hash
                or metadata.get("workspace_snapshot_hash") != self.collection.workspace_snapshot_hash
                or metadata.get("distinct_file_count") != str(self.collection.distinct_file_count)
                or metadata.get("membership_count") != str(self.collection.membership_count)
            ):
                raise RuntimeError("staged workspace collection search index metadata is invalid")

    @staticmethod
    def normalize_query(query: str | None) -> tuple[str | None, str | None]:
        return normalize_search_query(query)

    def _token_count(self, content: str) -> int:
        return len(self.encoding.encode(content, disallowed_special=()))

    def full_map(self) -> str:
        if self.version == 3:
            assert isinstance(self.collection, WorkspaceCollectionSetV3)
            return visible_workspace_collection_directory_text(self.collection)
        assert isinstance(self.collection, WorkspaceCollectionSet)
        return visible_workspace_collection_v2_summary_text(self.collection)

    def search(
        self,
        *,
        query: str | None,
        path: str | None = None,
        card_id: str | None = None,
        offset: int,
        token_cap: int,
        fragment_card_id: str | None = None,
        fragment_token_offset: int | None = None,
    ) -> CollectionSearchPage:
        if offset < 0:
            raise RuntimeError("workspace-collection-search cursor is invalid")
        normalized_path = _normalize_path_lookup(path)
        selected_modes = sum(value is not None for value in (query, normalized_path, card_id))
        if selected_modes > 1:
            raise CollectionMapError("query, path, and card_id are mutually exclusive")
        if card_id is not None:
            if self.version != 3:
                raise CollectionMapError("card_id detail lookup requires a v3 workspace collection map")
            if not _CARD_ID.fullmatch(card_id):
                raise CollectionMapError("card_id is invalid")
            assert isinstance(self.collection, WorkspaceCollectionSetV3)
            card = next((item for item in self.collection.cards if item.card_id == card_id), None)
            if card is None:
                content = "未找到该工作区集合。"
                tokens = self._token_count(content)
                return CollectionSearchPage(content, tokens, tokens, None)
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT path FROM members WHERE card_id = ? ORDER BY member_index LIMIT ? OFFSET ?",
                    (card_id, PAGE_SIZE * 4 + 1, offset),
                ).fetchall()
            paths = [str(row[0]) for row in rows]
            visible = paths[: PAGE_SIZE * 4]
            packed: list[str] = []
            for path in visible:
                candidate = visible_workspace_collection_members_text(card=card, paths=[*packed, path])
                if self._token_count(candidate) <= token_cap:
                    packed.append(path)
                else:
                    break
            if not packed and visible:
                # A single legal path may be unusually long. Preserve progress
                # and keep the observation bounded; ordinary paths are emitted
                # losslessly by the branch above.
                raw = visible_workspace_collection_members_text(card=card, paths=[visible[0]])
                token_ids = self.encoding.encode(raw, disallowed_special=())
                content = self.encoding.decode(token_ids[:token_cap]).rstrip()
                consumed = 1
            else:
                content = visible_workspace_collection_members_text(card=card, paths=packed)
                consumed = len(packed)
            tokens = self._token_count(content)
            original_tokens = self._token_count(
                visible_workspace_collection_members_text(card=card, paths=visible)
            )
            has_more = len(paths) > consumed
            next_state = {"offset": offset + consumed, "card_id": card_id} if has_more else None
            return CollectionSearchPage(content, original_tokens, tokens, next_state)
        if normalized_path is not None:
            if self.version == 3:
                assert isinstance(self.collection, WorkspaceCollectionSetV3)
                with self._connect() as connection:
                    member_rows = [
                        (str(row[0]), str(row[1]))
                        for row in connection.execute(
                            "SELECT card_id, path FROM members ORDER BY path, card_id"
                        )
                    ]
                ranked_rows: list[tuple[tuple[int, int, str], str, str]] = []
                for found_card_id, found_path in member_rows:
                    rank = _path_match_rank(found_path, normalized_path)
                    if rank is not None:
                        ranked_rows.append((rank, found_card_id, found_path))
                ranked_rows = _keep_best_path_match_class(ranked_rows)
                ranked_rows.sort(key=lambda item: (item[0], item[1], item[2]))
                grouped_paths: dict[str, list[str]] = {}
                for _, found_card_id, found_path in ranked_rows:
                    grouped_paths.setdefault(found_card_id, []).append(found_path)
                card_ids = list(grouped_paths)
                visible_ids = card_ids[offset : offset + PAGE_SIZE]
                by_id = {card.card_id: card for card in self.collection.cards}
                return _collection_search_page(
                    card_ids=card_ids[offset : offset + PAGE_SIZE + 1],
                    card_text={
                        found_card_id: visible_workspace_collection_v3_path_matches_text(
                            card=by_id[found_card_id],
                            paths=grouped_paths[found_card_id],
                        )
                        for found_card_id in visible_ids
                    },
                    offset=offset,
                    token_cap=token_cap,
                    encoding=self.encoding,
                    fragment_card_id=fragment_card_id,
                    fragment_token_offset=fragment_token_offset,
                )
            assert isinstance(self.collection, WorkspaceCollectionSet)
            ranked_members: list[
                tuple[tuple[int, int, str], str, WorkspaceCollectionMember]
            ] = []
            for card in self.collection.cards:
                for member in card.members:
                    rank = _path_match_rank(member.path, normalized_path)
                    if rank is not None:
                        ranked_members.append((rank, card.card_id, member))
            ranked_members = _keep_best_path_match_class(ranked_members)
            ranked_members.sort(key=lambda item: (item[0], item[1]))
            grouped_members: dict[str, list[WorkspaceCollectionMember]] = {}
            for _, found_card_id, member in ranked_members:
                grouped_members.setdefault(found_card_id, []).append(member)
            card_ids = list(grouped_members)
            visible_ids = card_ids[offset : offset + PAGE_SIZE]
            by_id = {card.card_id: card for card in self.collection.cards}
            return _collection_search_page(
                card_ids=card_ids[offset : offset + PAGE_SIZE + 1],
                card_text={
                    found_card_id: visible_workspace_collection_path_matches_text(
                        card=by_id[found_card_id],
                        matches=grouped_members[found_card_id],
                    )
                    for found_card_id in visible_ids
                },
                offset=offset,
                token_cap=token_cap,
                encoding=self.encoding,
                fragment_card_id=fragment_card_id,
                fragment_token_offset=fragment_token_offset,
            )
        display, expression = self.normalize_query(query)
        if expression is None:
            content = WORKSPACE_SEARCH_QUERY_REQUIRED_TEXT
            tokens = self._token_count(content)
            return CollectionSearchPage(content, tokens, tokens, None)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT c.card_id FROM cards_fts
                    JOIN cards AS c ON c.card_id = cards_fts.card_id
                    WHERE cards_fts MATCH ?
                    ORDER BY bm25(cards_fts), c.sort_index, c.card_id
                    LIMIT ? OFFSET ?
                    """,
                    (expression, PAGE_SIZE + 1, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("workspace collection search query failed") from exc
        card_ids = [str(row[0]) for row in rows]
        if not card_ids:
            content = "未找到匹配的工作区集合。"
            tokens = self._token_count(content)
            return CollectionSearchPage(content, tokens, tokens, None)
        visible = card_ids[:PAGE_SIZE]
        assert isinstance(self.collection, (WorkspaceCollectionSet, WorkspaceCollectionSetV3))
        renderer = (
            visible_workspace_collection_summary_text
            if self.version == 3
            else visible_workspace_collection_v2_summary_text
        )
        return _collection_search_page(
            card_ids=card_ids,
            card_text={
                card_id: renderer(self.collection, card_ids=[card_id])  # type: ignore[arg-type]
                for card_id in visible
            },
            offset=offset,
            token_cap=token_cap,
            encoding=self.encoding,
            fragment_card_id=fragment_card_id,
            fragment_token_offset=fragment_token_offset,
        )

    def path_context(self, paths: Iterable[str]) -> str:
        normalized: set[str] = set()
        for path in paths:
            try:
                normalized.add(_safe_relative_path(path))
            except ValueError:
                continue
        if not normalized:
            return ""
        matching: list[str] = []
        if self.version == 3:
            assert isinstance(self.collection, WorkspaceCollectionSetV3)
            with self._connect() as connection:
                for path in sorted(normalized):
                    rows = connection.execute(
                        "SELECT DISTINCT card_id FROM members "
                        "WHERE path = ? OR path LIKE ? ORDER BY card_id",
                        (path, path.rstrip("/") + "/%"),
                    ).fetchall()
                    matching.extend(str(row[0]) for row in rows)
            matching = sorted(set(matching))
            return (
                visible_workspace_collection_summary_text(self.collection, card_ids=matching)
                if matching
                else ""
            )
        assert isinstance(self.collection, WorkspaceCollectionSet)
        for card in self.collection.cards:
            members = {member.path for member in card.members}
            if any(path in members or any(member.startswith(path.rstrip("/") + "/") for member in members) for path in normalized):
                matching.append(card.card_id)
        return visible_workspace_collection_text(self.collection, card_ids=matching) if matching else ""


class EmptyCollectionMapSearch:
    """Empty ``no_collection_map`` backend with the same public workspace-search
    call shape."""

    def __init__(self) -> None:
        self.encoding = tiktoken.get_encoding("cl100k_base")

    def full_map(self) -> str:
        return WORKSPACE_MAP_UNAVAILABLE_TEXT

    def search(
        self,
        *,
        query: str | None,
        path: str | None = None,
        card_id: str | None = None,
        offset: int,
        token_cap: int,
        fragment_card_id: str | None = None,
        fragment_token_offset: int | None = None,
    ) -> CollectionSearchPage:
        _normalize_path_lookup(path)
        if sum(value is not None for value in (query, path, card_id)) > 1:
            raise CollectionMapError("query, path, and card_id are mutually exclusive")
        del query, path, card_id, offset, token_cap, fragment_card_id, fragment_token_offset
        content = "未找到匹配的工作区集合。"
        tokens = len(self.encoding.encode(content, disallowed_special=()))
        return CollectionSearchPage(content, tokens, tokens, None)

    def path_context(self, paths: Iterable[str]) -> str:
        del paths
        return ""


def stage_workspace_collection_map(
    *,
    collection_set_path: str | Path,
    artifact_root: str | Path,
    workspace_root: str | Path,
    expected_workspace_snapshot_hash: str,
    exclude_controlled_agents_md: bool = False,
) -> StagedCollectionMap:
    """Stage a snapshot-scoped public map and its deterministic FTS index."""

    source = Path(collection_set_path).resolve(strict=True)
    try:
        raw_format = json.loads(source.read_text(encoding="utf-8")).get("format")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise CollectionMapError("invalid workspace collection artefact") from exc
    if raw_format == WORKSPACE_COLLECTION_SET_V3_FORMAT:
        loaded = load_json_model(source, WorkspaceCollectionSetV3)
        assert isinstance(loaded, WorkspaceCollectionSetV3)
        collection: WorkspaceCollectionSet | WorkspaceCollectionSetV3 = loaded
    else:
        loaded = load_json_model(source, WorkspaceCollectionSet)
        assert isinstance(loaded, WorkspaceCollectionSet)
        collection = loaded
    if collection.workspace_snapshot_hash != expected_workspace_snapshot_hash:
        raise CollectionMapError("workspace collection map does not match the task workspace snapshot")
    workspace = Path(workspace_root).resolve(strict=True)
    if not workspace.is_dir():
        raise CollectionMapError("workspace root is not a directory")
    root = Path(artifact_root).resolve()
    target = root / "workspace-collection-map.public.json"
    _atomic_write(target, source.read_bytes(), mode=0o600)
    index = root / "workspace-collection-map.search.sqlite"
    if isinstance(collection, WorkspaceCollectionSetV3):
        source_index = source.with_name("workspace-collection-map.members.sqlite")
        if not source_index.is_file() or source_index.is_symlink():
            raise CollectionMapError("v3 workspace collection map lacks its companion member index")
        try:
            connection = sqlite3.connect(source_index.resolve(strict=True).as_uri() + "?mode=ro&immutable=1", uri=True)
            connection.execute("PRAGMA query_only = ON")
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            rows = connection.execute("SELECT card_id, path FROM members ORDER BY card_id, member_index").fetchall()
            card_rows = connection.execute(
                "SELECT card_id, title, description, representative_content, file_count "
                "FROM cards ORDER BY card_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CollectionMapError("v3 workspace collection member index is invalid") from exc
        finally:
            if "connection" in locals():
                connection.close()
        if metadata.get("format") != WORKSPACE_COLLECTION_SEARCH_INDEX_V3_FORMAT:
            raise CollectionMapError("v3 workspace collection member index format is invalid")
        if metadata.get("workspace_snapshot_hash") != expected_workspace_snapshot_hash:
            raise CollectionMapError("v3 workspace collection member index snapshot is invalid")
        canonical_hash = "sha256:" + hashlib.sha256(
            canonical_json(collection.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        if metadata.get("workspace_collection_set_sha256") != canonical_hash:
            raise CollectionMapError("v3 workspace collection member index public binding is invalid")
        if metadata.get("distinct_file_count") != str(collection.distinct_file_count):
            raise CollectionMapError("v3 workspace collection member index distinct count is invalid")
        if metadata.get("membership_count") != str(collection.membership_count):
            raise CollectionMapError("v3 workspace collection member index membership count is invalid")
        expected_card_rows = [
            (
                card.card_id,
                card.title,
                card.description,
                "\n".join(card.representative_content),
                card.file_count,
            )
            for card in collection.cards
        ]
        if card_rows != expected_card_rows:
            raise CollectionMapError("v3 workspace collection member index card set is invalid")
        distinct: set[str] = set()
        for card_id, member_path in rows:
            del card_id
            safe_path = _safe_relative_path(str(member_path))
            distinct.add(safe_path)
            member_target = (workspace / safe_path).resolve()
            try:
                member_target.relative_to(workspace)
            except ValueError as exc:
                raise CollectionMapError("workspace collection member escapes workspace root") from exc
            info = member_target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise CollectionMapError("workspace collection member is missing or unsafe")
        if len(distinct) != collection.distinct_file_count:
            raise CollectionMapError("v3 workspace collection member coverage is invalid")
        if len(rows) != collection.membership_count:
            raise CollectionMapError("v3 workspace collection member count is invalid")
        workspace_paths = {
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not (
                exclude_controlled_agents_md
                and path.relative_to(workspace).as_posix() == "AGENTS.md"
            )
        }
        if distinct != workspace_paths:
            raise CollectionMapError("v3 workspace collection map does not cover the full workspace")
        _atomic_write(index, source_index.read_bytes(), mode=0o600)
        index_hash = sha256_file(index)
    else:
        for card in collection.cards:
            for member in card.members:
                member_target = (workspace / member.path).resolve()
                try:
                    member_target.relative_to(workspace)
                except ValueError as exc:
                    raise CollectionMapError("workspace collection member escapes workspace root") from exc
                info = member_target.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise CollectionMapError("workspace collection member is missing or unsafe")
        index_hash = build_workspace_collection_search_index(collection, index_path=index)
    return StagedCollectionMap(
        collection_set_path=str(target),
        collection_set_sha256=sha256_file(target),
        search_index_path=str(index),
        search_index_sha256=index_hash,
    )
