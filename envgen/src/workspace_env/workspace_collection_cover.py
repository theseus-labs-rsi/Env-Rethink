"""Exhaustive, task-independent workspace collection-map synthesis.

The model supplies semantic summaries and optional re-grouping proposals.
Deterministic code owns catalog coverage, bucket boundaries, operation
validation, and the final public/SQLite split.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from .collection_map import (
    CollectionMapError,
    WorkspaceCatalog,
    WorkspaceCollectionSetV3,
    WorkspaceCollectionSummaryCard,
    WorkspaceCollectionMapSearch,
    build_workspace_catalog,
    build_workspace_collection_v3_index,
    load_json_model,
    sha256_file,
    write_json,
)
from .integration import workspace_snapshot_hash
from .manifest import StrictModel


COVER_SYNTHESIS_VERSION = "codex-workspace-collection-cover-v1"
ORACLE_TASK_SUITE_CONTEXT_FORMAT = "workspace-bench.oracle-task-suite-context.private.v1"
PATH_DISAMBIGUATION_PROFILE = "path_disambiguation_v1"
CodexRunner = Callable[..., dict[str, Any]]


class WorkspaceCollectionCoverError(RuntimeError):
    pass


class WorkspaceCollectionCoverConfig(StrictModel):
    schema_version: Literal[2] = 2
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    workspace_root: str = Field(min_length=1)
    catalog_root: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=240)
    auth_mode: Literal["chatgpt", "api"] = "chatgpt"
    base_url: str | None = None
    expected_codex_version: Literal["0.144.5"] = "0.144.5"
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    timeout_seconds: float = Field(default=600.0, gt=0)
    max_parallel_buckets: int = Field(default=5, ge=1, le=32)
    max_coordination_rounds: int = Field(default=3, ge=1, le=10)
    bucket_min_files: int = Field(default=32, ge=2, le=256)
    bucket_target_files: int = Field(default=48, ge=2, le=256)
    bucket_max_files: int = Field(default=64, ge=2, le=256)
    resume_existing_output: bool = False

    @model_validator(mode="after")
    def validate_config(self) -> "WorkspaceCollectionCoverConfig":
        if not self.bucket_min_files <= self.bucket_target_files <= self.bucket_max_files:
            raise ValueError("bucket sizes must satisfy min <= target <= max")
        if self.auth_mode == "api" and not self.base_url:
            raise ValueError("api auth_mode requires base_url")
        if self.auth_mode == "chatgpt" and self.base_url is not None:
            raise ValueError("chatgpt auth_mode must not set base_url")
        return self


class WorkspaceCollectionContinuationConfig(StrictModel):
    """Append coordination rounds to one immutable completed v3 map."""

    schema_version: Literal[3] = 3
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    workspace_root: str = Field(min_length=1)
    parent_collection_set_path: str = Field(min_length=1)
    parent_member_index_path: str = Field(min_length=1)
    parent_audit_path: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=240)
    auth_mode: Literal["chatgpt", "api"] = "chatgpt"
    base_url: str | None = None
    expected_codex_version: Literal["0.144.5"] = "0.144.5"
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    timeout_seconds: float = Field(default=600.0, gt=0)
    max_parallel_refinements: int = Field(default=5, ge=1, le=32)
    max_additional_coordination_rounds: int = Field(default=3, ge=1, le=10)
    stop_on_convergence: bool = True
    resume_existing_output: bool = False
    task_input_conditioned: bool = False
    quality_profile: Literal["path_disambiguation_v1"] | None = None
    coordinator_guidance: str | None = Field(default=None, min_length=1, max_length=4000)
    coordinator_focus_paths: list[str] = Field(default_factory=list, max_length=64)
    oracle_task_suite_context_path: str | None = None

    @model_validator(mode="after")
    def validate_config(self) -> "WorkspaceCollectionContinuationConfig":
        if self.auth_mode == "api" and not self.base_url:
            raise ValueError("api auth_mode requires base_url")
        if self.auth_mode == "chatgpt" and self.base_url is not None:
            raise ValueError("chatgpt auth_mode must not set base_url")
        oracle_mode = self.oracle_task_suite_context_path is not None
        has_focus = (
            self.coordinator_guidance is not None
            or bool(self.coordinator_focus_paths)
            or oracle_mode
        )
        if has_focus != self.task_input_conditioned:
            raise ValueError(
                "targeted coordinator guidance requires task_input_conditioned=true, "
                "and task_input_conditioned=true requires guidance or focus paths"
            )
        if self.coordinator_focus_paths != sorted(set(self.coordinator_focus_paths)):
            raise ValueError("coordinator_focus_paths must be unique and sorted")
        if oracle_mode and (
            self.coordinator_guidance is not None or self.coordinator_focus_paths
        ):
            raise ValueError(
                "oracle task-suite context cannot be combined with manual guidance or focus paths"
            )
        if self.quality_profile is not None and self.task_input_conditioned:
            raise ValueError(
                "task-independent quality profiles cannot be combined with targeted context"
            )
        return self


class OracleTaskRequirement(StrictModel):
    task_id: str = Field(min_length=1, max_length=160)
    task_description: str = Field(min_length=1, max_length=32_000)
    metadata_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    required_workspace_paths: list[str] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_requirement(self) -> "OracleTaskRequirement":
        if self.required_workspace_paths != sorted(set(self.required_workspace_paths)):
            raise ValueError("required_workspace_paths must be unique and sorted")
        for value in self.required_workspace_paths:
            pure = PurePosixPath(value)
            if (
                pure.is_absolute()
                or not pure.parts
                or any(part in {"", ".", ".."} for part in pure.parts)
                or pure.as_posix() != value
            ):
                raise ValueError("required workspace path must be canonical")
        return self


class OracleExcludedTask(StrictModel):
    task_id: str = Field(min_length=1, max_length=160)
    metadata_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: Literal["unresolved_input", "ambiguous_workspace_binding"]
    private_input_labels: list[str] = Field(default_factory=list, max_length=256)


class OracleTaskSuiteContext(StrictModel):
    format: Literal[ORACLE_TASK_SUITE_CONTEXT_FORMAT] = ORACLE_TASK_SUITE_CONTEXT_FORMAT
    persona: str = Field(min_length=1, max_length=240)
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tasks: list[OracleTaskRequirement] = Field(min_length=1, max_length=256)
    excluded_tasks: list[OracleExcludedTask] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_context(self) -> "OracleTaskSuiteContext":
        task_ids = [item.task_id for item in self.tasks]
        excluded_ids = [item.task_id for item in self.excluded_tasks]
        if task_ids != sorted(set(task_ids)):
            raise ValueError("oracle task requirements must be unique and sorted")
        if excluded_ids != sorted(set(excluded_ids)):
            raise ValueError("excluded oracle tasks must be unique and sorted")
        if set(task_ids) & set(excluded_ids):
            raise ValueError("one task cannot be both included and excluded")
        return self


class DeterministicBucket(StrictModel):
    bucket_id: str = Field(pattern=r"^bucket-[0-9a-f]{12}$")
    path_scopes: list[str] = Field(min_length=1)
    paths: list[str] = Field(min_length=1)
    small_leaf: bool

    @model_validator(mode="after")
    def validate_bucket(self) -> "DeterministicBucket":
        if self.path_scopes != sorted(set(self.path_scopes)):
            raise ValueError("path_scopes must be unique and sorted")
        if self.paths != sorted(set(self.paths)):
            raise ValueError("paths must be unique and sorted")
        return self


class BucketSummary(StrictModel):
    bucket_id: str = Field(pattern=r"^bucket-[0-9a-f]{12}$")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=600)
    representative_content: list[str] = Field(default_factory=list, max_length=6)


class ProposedCard(StrictModel):
    card_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=600)
    representative_content: list[str] = Field(default_factory=list, max_length=6)


class CoordinationOperation(StrictModel):
    kind: Literal["transfer", "copy", "merge", "split"]
    path: str | None = None
    source_card_id: str | None = None
    target_card_id: str | None = None
    source_card_ids: list[str] = Field(default_factory=list)
    target_card: ProposedCard | None = None
    split_cards: list[ProposedCard] = Field(default_factory=list)
    split_members: dict[str, list[str]] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=600)


class CoordinationPlan(StrictModel):
    operations: list[CoordinationOperation] = Field(default_factory=list, max_length=4096)


@dataclass(frozen=True, slots=True)
class CoordinationReduction:
    cards: list[WorkspaceCollectionSummaryCard]
    memberships: dict[str, list[str]]
    affected_card_ids: tuple[str, ...]
    changed: bool


def _bucket_id(paths: list[str]) -> str:
    digest = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()[:12]
    return "bucket-" + digest


def _balanced_chunks(
    paths: list[str],
    *,
    minimum: int,
    target: int,
    maximum: int,
) -> list[list[str]]:
    """Split a flat oversized group without leaving a tiny tail."""

    minimum_count = max(1, math.ceil(len(paths) / maximum))
    maximum_count = max(1, len(paths) // minimum)
    target_count = max(1, math.floor(len(paths) / target + 0.5))
    count = min(max(target_count, minimum_count), maximum_count)
    base, remainder = divmod(len(paths), count)
    chunks: list[list[str]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        chunks.append(paths[start : start + size])
        start += size
    return chunks


def partition_catalog(
    catalog: WorkspaceCatalog,
    *,
    minimum: int = 32,
    target: int = 48,
    maximum: int = 64,
) -> list[DeterministicBucket]:
    """Recursively partition by directory prefix, then merge small siblings."""

    if not 2 <= minimum <= target <= maximum:
        raise WorkspaceCollectionCoverError("bucket sizes must satisfy 2 <= min <= target <= max")
    all_paths = [entry.path for entry in catalog.files]
    if not all_paths:
        raise WorkspaceCollectionCoverError("cannot build a collection cover for an empty workspace")

    def recurse(prefix: tuple[str, ...], paths: list[str]) -> list[tuple[list[str], list[str]]]:
        scope = "/".join(prefix) or "."
        if len(paths) <= maximum:
            return [([scope], paths)]
        direct: list[str] = []
        children: dict[str, list[str]] = defaultdict(list)
        depth = len(prefix)
        for path in paths:
            parts = PurePosixPath(path).parts
            if len(parts) <= depth + 1:
                direct.append(path)
            else:
                children[parts[depth]].append(path)
        if not children:
            return [
                ([scope], chunk)
                for chunk in _balanced_chunks(
                    paths,
                    minimum=minimum,
                    target=target,
                    maximum=maximum,
                )
            ]
        groups: list[tuple[list[str], list[str]]] = []
        if direct:
            groups.extend(
                ([scope], chunk)
                for chunk in _balanced_chunks(
                    sorted(direct),
                    minimum=minimum,
                    target=target,
                    maximum=maximum,
                )
            )
        for child in sorted(children):
            groups.extend(recurse(prefix + (child,), sorted(children[child])))

        # Merge adjacent small descendants of this same parent.  The scopes
        # remain explicit because a merged bucket may span sibling prefixes.
        merged: list[tuple[list[str], list[str]]] = []
        pending_scopes: list[str] = []
        pending_paths: list[str] = []
        for scopes, group_paths in groups:
            if len(group_paths) < minimum and len(pending_paths) + len(group_paths) <= maximum:
                pending_scopes.extend(scopes)
                pending_paths.extend(group_paths)
                continue
            if pending_paths and len(pending_paths) + len(group_paths) <= maximum:
                merged.append((sorted(set(pending_scopes + scopes)), sorted(pending_paths + group_paths)))
                pending_scopes, pending_paths = [], []
            else:
                if pending_paths:
                    merged.append((sorted(set(pending_scopes)), sorted(pending_paths)))
                    pending_scopes, pending_paths = [], []
                merged.append((scopes, group_paths))
        if pending_paths:
            if merged and len(merged[-1][1]) + len(pending_paths) <= maximum:
                scopes, previous = merged.pop()
                merged.append((sorted(set(scopes + pending_scopes)), sorted(previous + pending_paths)))
            else:
                merged.append((sorted(set(pending_scopes)), sorted(pending_paths)))
        return merged

    raw = recurse((), all_paths)
    buckets = [
        DeterministicBucket(
            bucket_id=_bucket_id(paths),
            path_scopes=sorted(scopes),
            paths=sorted(paths),
            small_leaf=len(paths) < minimum,
        )
        for scopes, paths in raw
    ]
    buckets.sort(key=lambda item: (item.paths[0], item.bucket_id))
    flattened = [path for bucket in buckets for path in bucket.paths]
    if sorted(flattened) != all_paths or len(flattened) != len(set(flattened)):
        raise WorkspaceCollectionCoverError("deterministic partition lost or duplicated catalog files")
    return buckets


def apply_coordination_plan_to_state(
    *,
    current_cards: list[WorkspaceCollectionSummaryCard],
    current_memberships: dict[str, list[str]],
    plan: CoordinationPlan,
) -> CoordinationReduction:
    """Apply semantic proposals while preserving deterministic invariants."""

    cards: dict[str, ProposedCard] = {
        card.card_id: ProposedCard(
            card_id=card.card_id,
            title=card.title,
            description=card.description,
            representative_content=card.representative_content,
        )
        for card in current_cards
    }
    if set(cards) != set(current_memberships):
        raise WorkspaceCollectionCoverError("current cards and memberships disagree")
    members: dict[str, set[str]] = {
        card_id: set(paths)
        for card_id, paths in current_memberships.items()
    }
    if any(not paths for paths in members.values()):
        raise WorkspaceCollectionCoverError("current collections cannot be empty")
    before_members = {card_id: frozenset(paths) for card_id, paths in members.items()}
    catalog_paths = set().union(*members.values())
    explicitly_affected: set[str] = set()
    transfer_targets: dict[str, str] = {}

    def require_card(card_id: str | None) -> str:
        if card_id is None or card_id not in cards:
            raise WorkspaceCollectionCoverError("coordination operation references an unknown card")
        return card_id

    for operation in plan.operations:
        if operation.kind in {"transfer", "copy"}:
            source = require_card(operation.source_card_id)
            if operation.target_card is not None:
                target = operation.target_card.card_id
                if operation.target_card_id is not None and operation.target_card_id != target:
                    raise WorkspaceCollectionCoverError(
                        "coordination operation has inconsistent target card identifiers"
                    )
                if target in cards:
                    raise WorkspaceCollectionCoverError("new transfer target card_id already exists")
                cards[target] = operation.target_card
                members[target] = set()
            else:
                target = require_card(operation.target_card_id)
            if source == target:
                raise WorkspaceCollectionCoverError("transfer or copy source and target must differ")
            if operation.path is None or operation.path not in catalog_paths or operation.path not in members[source]:
                raise WorkspaceCollectionCoverError("coordination operation references an invalid source member")
            if operation.kind == "transfer":
                previous_target = transfer_targets.get(operation.path)
                if previous_target is not None and previous_target != target:
                    raise WorkspaceCollectionCoverError(
                        "one coordination round cannot transfer the same file to multiple targets"
                    )
                transfer_targets[operation.path] = target
            members[target].add(operation.path)
            if operation.kind == "transfer":
                members[source].remove(operation.path)
                if not members[source]:
                    members.pop(source)
                    cards.pop(source)
                else:
                    explicitly_affected.add(source)
            explicitly_affected.add(target)
        elif operation.kind == "merge":
            sources = operation.source_card_ids
            if len(sources) < 2 or len(set(sources)) != len(sources) or operation.target_card is None:
                raise WorkspaceCollectionCoverError("merge requires unique sources and one target card")
            for source in sources:
                require_card(source)
            target = operation.target_card
            if target.card_id in cards and target.card_id not in sources:
                raise WorkspaceCollectionCoverError("merge target card_id already exists")
            union = set().union(*(members[source] for source in sources))
            for source in sources:
                cards.pop(source)
                members.pop(source)
            cards[target.card_id] = target
            members[target.card_id] = union
            explicitly_affected.add(target.card_id)
        else:
            source = require_card(operation.source_card_id)
            if len(operation.split_cards) < 2:
                raise WorkspaceCollectionCoverError("split requires at least two target cards")
            target_ids = [card.card_id for card in operation.split_cards]
            if len(target_ids) != len(set(target_ids)) or set(operation.split_members) != set(target_ids):
                raise WorkspaceCollectionCoverError("split target definitions are inconsistent")
            split_sets = [set(operation.split_members[target]) for target in target_ids]
            if any(not paths for paths in split_sets):
                raise WorkspaceCollectionCoverError("split target cards cannot be empty")
            if set().union(*split_sets) != members[source] or sum(map(len, split_sets)) != len(members[source]):
                raise WorkspaceCollectionCoverError("split must partition every source member exactly once")
            for target_id in target_ids:
                if target_id in cards and target_id != source:
                    raise WorkspaceCollectionCoverError("split target card_id already exists")
            cards.pop(source)
            members.pop(source)
            for card in operation.split_cards:
                cards[card.card_id] = card
                members[card.card_id] = set(operation.split_members[card.card_id])
                explicitly_affected.add(card.card_id)

    covered = set().union(*members.values())
    if covered != catalog_paths:
        raise WorkspaceCollectionCoverError("coordination plan changed full workspace coverage")
    public_cards = [
        WorkspaceCollectionSummaryCard(
            **cards[card_id].model_dump(mode="json"),
            file_count=len(members[card_id]),
        )
        for card_id in sorted(cards)
    ]
    normalized_members = {card_id: sorted(members[card_id]) for card_id in sorted(members)}
    membership_changed = {
        card_id
        for card_id, paths in members.items()
        if before_members.get(card_id) != frozenset(paths)
    }
    deleted = set(before_members) - set(members)
    changed = bool(membership_changed or deleted)
    affected = sorted((explicitly_affected | membership_changed) & set(members))
    return CoordinationReduction(
        cards=public_cards,
        memberships=normalized_members,
        affected_card_ids=tuple(affected),
        changed=changed,
    )


def apply_coordination_plan(
    *,
    summaries: list[BucketSummary],
    buckets: list[DeterministicBucket],
    plan: CoordinationPlan,
) -> tuple[list[WorkspaceCollectionSummaryCard], dict[str, list[str]]]:
    """Compatibility wrapper for applying a plan to the initial buckets."""

    summary_by_id = {item.bucket_id: item for item in summaries}
    initial_cards = [
        WorkspaceCollectionSummaryCard(
            card_id=bucket.bucket_id,
            title=summary_by_id[bucket.bucket_id].title,
            description=summary_by_id[bucket.bucket_id].description,
            representative_content=summary_by_id[bucket.bucket_id].representative_content,
            file_count=len(bucket.paths),
        )
        for bucket in buckets
    ]
    reduction = apply_coordination_plan_to_state(
        current_cards=initial_cards,
        current_memberships={bucket.bucket_id: bucket.paths for bucket in buckets},
        plan=plan,
    )
    return reduction.cards, reduction.memberships


def _json_schema(model: type[StrictModel]) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2)


def render_bucket_prompt(bucket: DeterministicBucket) -> str:
    return f"""你是 Codex A，负责概括一个工作区文件桶。

只可读取 `BUCKET_ROOT/` 和 `BUCKET_CATALOG.json`。请按需选择并读取最有代表性的文件，不要求逐个打开。
根据真实目录、文件名和抽样内容生成一个简短、可检索的集合标题、简述和最多 6 条代表性内容。
不要写任务、rubric、judge 或参考答案。不要推断无法从本桶观察到的业务事实。

写入 `output/bucket-summary.json`，严格符合下面 schema；`bucket_id` 必须原样使用 `{bucket.bucket_id}`。

```json
{_json_schema(BucketSummary)}
```
"""


def render_coordinator_prompt(
    *,
    round_index: int,
    guidance: str | None = None,
    focus_paths: list[str] | None = None,
    oracle_task_suite: bool = False,
    quality_profile: Literal["path_disambiguation_v1"] | None = None,
) -> str:
    focus = ""
    organization_rule = "不要按任务、rubric、judge 或参考答案组织集合。"
    if oracle_task_suite:
        focus = """
本轮另提供私有 `input/task-suite-map-context.private.json`，其中包含该 workspace 的任务描述和已解析输入文件路径；
可在只读 `FOCUS_ROOT/` 中按需查看这些文件的真实内容。必须满足以下硬约束：
- 对每个具有两个或更多输入文件的任务，最终至少有一个卡片同时包含该任务的全部输入文件；
- 单文件任务不得为该文件新建专用卡片；
- 优先复用现有卡片或让多个任务共享同一语义卡片，减少新卡片、无关成员和重复 membership；
- 根据任务描述中的业务概念和文件真实内容重写受影响 card，使文件定位查询能够稳定命中，但不得逐字复制任务指令；
- 若确需新卡片，可在首个 copy/transfer 操作中提供 `target_card` 创建它，再把其余文件 copy 进去；
- 公开卡片不得出现 task ID、任务原文、required-input、rubric、judge 或参考答案，只能描述文件本身可核验的业务语义。

先逐项核对 task-suite context 与当前完整成员关系，再输出能在本轮最大程度满足尚未满足约束的操作。
"""
        organization_rule = (
            "任务上下文只用于优化成员共置；公开标题、摘要和代表性内容必须保持任务不可见。"
        )
    elif guidance is not None or focus_paths:
        focus = f"""
本轮另提供 `input/coordinator-focus.json`。如其中列有文件，可在只读 `FOCUS_ROOT/` 中按需查看真实内容。
这只是需要认真复核的关注点，不预设调整结论；仍须根据文件语义和完整成员关系判断是 transfer、copy、merge、
split 还是不修改。不要把关注说明或任务措辞写进公开集合标题与摘要。
关注说明：{guidance or "无额外文字说明"}
"""
    elif quality_profile == PATH_DISAMBIGUATION_PROFILE:
        focus = """
本轮执行 task-independent 的路径消歧质量检查；你没有任务、rubric 或重要文件标签。对整张地图统一使用以下标准：
- 对标题或摘要词汇近似、但实际路径与用途不同的集合，优先用最短且有区分度的目录锚点消除歧义；
- 若一张卡跨越多个各自完整、可独立检索的子目录，且其成员无共同业务对象或流程，应按路径语义 split；不要仅因文件格式不同拆分同一业务链；
- 新卡或受影响卡必须能从标题、简述或代表性内容中看出它对应的目录范围，避免与其他卡共用“模板”“报表”“运营资料”等空泛标题；
- 对六个或更少文件的紧凑目录集合，代表性内容应覆盖全部成员路径；对更大集合则选择能区分目录和用途的路径。

这些标准只依据当前公开卡片与完整成员路径，不得推测任务需求，也不得为单个未见任务设计专用卡。
"""
    return f"""你是全局集合协调 Codex，正在执行第 {round_index} 轮协调。读取 `input/cards.json` 和 `input/members.json`。

当前集合已经覆盖整个工作区；经过前序轮次后，同一文件可能属于多个集合。你可以不做修改；只有摘要或路径提供清楚依据时，才提出：
- transfer：把一个文件从一个集合移动到另一个集合；若源集合只有该文件，迁移后源卡片会被删除；
- copy：让一个文件同时属于两个集合，不复制物理文件；
- merge：合并多个高度同质集合；`source_card_ids` 必须至少包含两个唯一现有卡片，且必须提供完整的 `target_card`；
- split：把一个语义混杂集合完整拆分。
{focus}

{organization_rule}操作按数组顺序应用；每项操作只能引用当时仍存在的卡片，
不得再引用已被前一项 transfer 清空、merge 或 split 删除的卡片。操作引用必须精确，split 必须无遗漏、无重复地列出源集合全部成员。
写入 `output/coordination-plan.json`，严格符合下面 schema。没有必要调整时输出空 operations。

```json
{_json_schema(CoordinationPlan)}
```
"""


def render_refinement_prompt(
    *,
    card_id: str,
    round_index: int,
    oracle_task_suite: bool = False,
    quality_profile: Literal["path_disambiguation_v1"] | None = None,
) -> str:
    oracle_rule = ""
    if oracle_task_suite:
        oracle_rule = """
任务上下文只决定成员共置。公开摘要应概括这些文件共同、可核验的业务用途，不得暴露任务描述、task ID 或“任务输入”等来源信息。
"""
    elif quality_profile == PATH_DISAMBIGUATION_PROFILE:
        oracle_rule = """
本轮是 task-independent 路径消歧优化。标题应保留最短且有区分度的目录或业务锚点，简述要明确该目录与其他近似资料库的区别。代表性内容必须显式带相对路径；若当前成员不超过 6 个，则覆盖全部成员。
"""
    return f"""你是 Codex A，负责在第 {round_index} 轮协调后重新整理一个受到影响的集合。

读取：
- `COLLECTION_ROOT/`：该集合调整后的全部当前成员；
- `input/previous-card.json`：调整前或协调器暂定的摘要；
- `input/membership-change.json`：本轮新增和移除的成员路径。

请根据当前真实成员重新选读代表文件，并完整重写标题、简述和最多 6 条代表性内容。摘要必须描述当前集合，
不能因为上一版存在就机械保留。不要写任务、rubric、judge 或参考答案，也不要推断文件中无法核验的事实。
{oracle_rule}

写入 `output/card-summary.json`，严格符合下面 schema；`card_id` 必须原样使用 `{card_id}`。

```json
{_json_schema(ProposedCard)}
```
"""


def validate_oracle_task_coverage(
    *,
    context: OracleTaskSuiteContext,
    memberships: dict[str, list[str]],
    parent_card_ids: set[str],
) -> dict[str, Any]:
    """Enforce the oracle intervention without leaking task bindings publicly."""

    member_sets = {card_id: set(paths) for card_id, paths in memberships.items()}
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for task in context.tasks:
        required = set(task.required_workspace_paths)
        covering = sorted(
            card_id for card_id, paths in member_sets.items() if required <= paths
        )
        selected = min(
            covering,
            key=lambda card_id: (len(member_sets[card_id] - required), card_id),
            default=None,
        )
        if len(required) >= 2 and selected is None:
            missing.append(task.task_id)
        records.append(
            {
                "task_id": task.task_id,
                "input_count": len(required),
                "covering_card_ids": covering,
                "selected_card_id": selected,
                "selected_card_extra_file_count": (
                    len(member_sets[selected] - required) if selected is not None else None
                ),
                "selected_card_input_precision": (
                    len(required) / len(member_sets[selected]) if selected is not None else None
                ),
            }
        )
    multi_file_requirements = [
        set(task.required_workspace_paths)
        for task in context.tasks
        if len(task.required_workspace_paths) >= 2
    ]
    unjustified_new_cards = sorted(
        card_id
        for card_id, paths in member_sets.items()
        if card_id not in parent_card_ids
        and not any(required <= paths for required in multi_file_requirements)
    )
    if missing:
        raise WorkspaceCollectionCoverError(
            "oracle map does not co-locate every multi-file task: " + ", ".join(missing)
        )
    if unjustified_new_cards:
        raise WorkspaceCollectionCoverError(
            "oracle map created new cards that do not cover a multi-file task: "
            + ", ".join(unjustified_new_cards)
        )
    multi_count = sum(len(item.required_workspace_paths) >= 2 for item in context.tasks)
    return {
        "task_count": len(context.tasks),
        "multi_file_task_count": multi_count,
        "multi_file_task_covered_count": multi_count,
        "single_file_task_count": len(context.tasks) - multi_count,
        "task_coverage": records,
        "unjustified_new_card_ids": unjustified_new_cards,
    }


def _readonly_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WorkspaceCollectionCoverError("role input tree contains an unsafe file")
            os.chmod(path, 0o444)
        for name in directories:
            os.chmod(current_path / name, 0o555)
    os.chmod(root, 0o555)


def load_completed_v3_state(
    *,
    workspace_root: str | Path,
    collection_set_path: str | Path,
    member_index_path: str | Path,
) -> tuple[WorkspaceCollectionSetV3, dict[str, list[str]], str, str]:
    """Load and fully bind one completed public map to its immutable workspace."""

    workspace = Path(workspace_root).resolve(strict=True)
    public = Path(collection_set_path).resolve(strict=True)
    index = Path(member_index_path).resolve(strict=True)
    public_hash = sha256_file(public)
    index_hash = sha256_file(index)
    backend = WorkspaceCollectionMapSearch(
        collection_set_path=str(public),
        collection_set_sha256=public_hash,
        index_path=str(index),
        index_sha256=index_hash,
    )
    if backend.version != 3 or not isinstance(backend.collection, WorkspaceCollectionSetV3):
        raise WorkspaceCollectionCoverError("continuation requires a completed v3 workspace collection map")
    collection = backend.collection
    if workspace_snapshot_hash(str(workspace)) != collection.workspace_snapshot_hash:
        raise WorkspaceCollectionCoverError("parent map does not match the current workspace snapshot")
    try:
        connection = sqlite3.connect(index.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT card_id, path FROM members ORDER BY card_id, member_index"
        ).fetchall()
    except sqlite3.Error as exc:
        raise WorkspaceCollectionCoverError("parent member index cannot be read") from exc
    finally:
        if "connection" in locals():
            connection.close()
    memberships: dict[str, list[str]] = {card.card_id: [] for card in collection.cards}
    for raw_card_id, raw_path in rows:
        card_id = str(raw_card_id)
        path = str(raw_path)
        if card_id not in memberships:
            raise WorkspaceCollectionCoverError("parent member index references an unknown card")
        memberships[card_id].append(path)
    if any(paths != sorted(set(paths)) for paths in memberships.values()):
        raise WorkspaceCollectionCoverError("parent memberships must be unique and sorted")
    if any(
        len(memberships[card.card_id]) != card.file_count
        for card in collection.cards
    ):
        raise WorkspaceCollectionCoverError("parent card counts disagree with its member index")
    indexed_paths = set().union(*(set(paths) for paths in memberships.values()))
    workspace_paths: set[str] = set()
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace).as_posix()
        if relative == "model_output" or relative.startswith("model_output/"):
            continue
        if path.is_symlink():
            raise WorkspaceCollectionCoverError("continuation workspace contains an unsafe symlink")
        if path.is_file():
            workspace_paths.add(relative)
    if indexed_paths != workspace_paths:
        raise WorkspaceCollectionCoverError("parent memberships do not cover the full workspace")
    return collection, memberships, public_hash, index_hash


class WorkspaceCollectionCoverOrchestrator:
    def __init__(self, *, config: WorkspaceCollectionCoverConfig, codex_runner: CodexRunner) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self.workspace = Path(config.workspace_root).resolve(strict=True)
        self.output = Path(config.output_root).resolve()
        self.snapshot_hash = workspace_snapshot_hash(str(self.workspace))
        self.catalog, self.catalog_path = build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=self.snapshot_hash,
            catalog_root=config.catalog_root,
        )

    def _provider(self) -> dict[str, Any]:
        provider: dict[str, Any] = {
            "authMode": self.config.auth_mode,
            "model": self.config.model,
            "__codex_runtime__": {
                "expected_cli_version": self.config.expected_codex_version,
                "protocol": "responses",
                "reasoning_effort": self.config.reasoning_effort,
                "mcp_servers": {},
                "tool_schemas": {},
            },
        }
        if self.config.base_url is not None:
            provider["baseUrl"] = self.config.base_url
        return provider

    def _run(self, *, key: str, root: Path, prompt: str) -> None:
        audit = self.output / "runs.private" / key
        audit.mkdir(parents=True, exist_ok=True, mode=0o700)
        (audit / "prompt.private.md").write_text(prompt, encoding="utf-8")
        result = self.codex_runner(
            prompt=prompt,
            work_dir=str(root),
            sandbox_dir=str(audit / "runtime.private"),
            timeout_s=self.config.timeout_seconds,
            api_provider=self._provider(),
            agent_id=key.replace("/", "-"),
        )
        write_json(audit / "codex-result.private.json", result, private=True)
        trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
        if result.get("status") != "ok" or collection.get("complete") is not True:
            raise WorkspaceCollectionCoverError(f"{key} failed or produced an incomplete trace")

    def _prepare_bucket(self, bucket: DeterministicBucket) -> Path:
        root = self.output / "runs.private" / "buckets" / bucket.bucket_id / "workdir"
        bucket_root = root / "BUCKET_ROOT"
        bucket_root.mkdir(parents=True, mode=0o700)
        for relative in bucket.paths:
            source = self.workspace / relative
            target = bucket_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _readonly_tree(bucket_root)
        write_json(root / "BUCKET_CATALOG.json", bucket.model_dump(mode="json"), private=True)
        (root / "output").mkdir(mode=0o700)
        return root

    def _load_reusable_bucket_summary(self, bucket: DeterministicBucket) -> BucketSummary | None:
        """Reuse only a completed, schema-valid role from this exact run."""

        role_root = self.output / "runs.private" / "buckets" / bucket.bucket_id
        result_path = role_root / "codex-result.private.json"
        summary_path = role_root / "workdir" / "output" / "bucket-summary.json"
        if not result_path.is_file() or not summary_path.is_file():
            return None
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
            collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
            if result.get("status") != "ok" or collection.get("complete") is not True:
                return None
            summary = load_json_model(summary_path, BucketSummary)
            assert isinstance(summary, BucketSummary)
        except (OSError, json.JSONDecodeError, CollectionMapError):
            return None
        if summary.bucket_id != bucket.bucket_id:
            return None
        return summary

    def _archive_incomplete_bucket_attempt(self, bucket: DeterministicBucket) -> str | None:
        role_root = self.output / "runs.private" / "buckets" / bucket.bucket_id
        if not role_root.exists():
            return None
        attempt = 1
        while True:
            archived = role_root.with_name(f"{bucket.bucket_id}.failed-attempt-{attempt:02d}")
            if not archived.exists():
                break
            attempt += 1
        role_root.rename(archived)
        return archived.name

    def _validate_resume_state(self, buckets: list[DeterministicBucket]) -> None:
        try:
            stored = json.loads((self.output / "run-config.private.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceCollectionCoverError("resume output has no valid run config") from exc
        expected = self.config.model_dump(
            mode="json",
            exclude={"base_url", "resume_existing_output"},
        )
        for key, value in expected.items():
            if stored.get(key) != value:
                raise WorkspaceCollectionCoverError(f"resume config changed immutable field: {key}")
        if (
            stored.get("orchestrator_version") != COVER_SYNTHESIS_VERSION
            or stored.get("workspace_snapshot_hash") != self.snapshot_hash
        ):
            raise WorkspaceCollectionCoverError("resume output belongs to a different orchestrator or snapshot")
        saved_catalog = load_json_model(
            self.output / "workspace-catalog.private.json",
            WorkspaceCatalog,
        )
        assert isinstance(saved_catalog, WorkspaceCatalog)
        if saved_catalog != self.catalog:
            raise WorkspaceCollectionCoverError("resume workspace catalog changed")
        try:
            saved_buckets = [
                DeterministicBucket.model_validate(item)
                for item in json.loads((self.output / "buckets.private.json").read_text(encoding="utf-8"))
            ]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise WorkspaceCollectionCoverError("resume bucket partition is invalid") from exc
        if saved_buckets != buckets:
            raise WorkspaceCollectionCoverError("resume bucket partition changed")

    def _prepare_refinement(
        self,
        *,
        round_index: int,
        card: WorkspaceCollectionSummaryCard,
        current_paths: list[str],
        previous_paths: list[str],
    ) -> Path:
        root = (
            self.output
            / "runs.private"
            / "coordination"
            / f"round-{round_index:02d}"
            / "refine"
            / card.card_id
            / "workdir"
        )
        collection_root = root / "COLLECTION_ROOT"
        collection_root.mkdir(parents=True, mode=0o700)
        for relative in current_paths:
            source = self.workspace / relative
            target = collection_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _readonly_tree(collection_root)
        (root / "input").mkdir(mode=0o700)
        (root / "output").mkdir(mode=0o700)
        write_json(
            root / "input" / "previous-card.json",
            card.model_dump(mode="json"),
            private=True,
        )
        current = set(current_paths)
        previous = set(previous_paths)
        write_json(
            root / "input" / "membership-change.json",
            {
                "added": sorted(current - previous),
                "removed": sorted(previous - current),
                "current_file_count": len(current_paths),
            },
            private=True,
        )
        return root

    def run(self) -> dict[str, Any]:
        if os.environ.get("CODEX_SANDBOX_MODE") != "danger-full-access":
            raise WorkspaceCollectionCoverError("CODEX_SANDBOX_MODE must be danger-full-access")
        output_exists = self.output.exists()
        if output_exists and not self.config.resume_existing_output:
            raise WorkspaceCollectionCoverError("output_root must not already exist")
        try:
            self.output.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise WorkspaceCollectionCoverError("output_root must be outside workspace_root")
        buckets = partition_catalog(
            self.catalog,
            minimum=self.config.bucket_min_files,
            target=self.config.bucket_target_files,
            maximum=self.config.bucket_max_files,
        )
        if output_exists:
            self._validate_resume_state(buckets)
        else:
            self.output.mkdir(parents=True, mode=0o700)
            write_json(
                self.output / "run-config.private.json",
                {
                    **self.config.model_dump(
                        mode="json",
                        exclude={"base_url", "resume_existing_output"},
                    ),
                    "base_url_sha256": (
                        "sha256:" + hashlib.sha256(self.config.base_url.encode()).hexdigest()
                        if self.config.base_url
                        else None
                    ),
                    "orchestrator_version": COVER_SYNTHESIS_VERSION,
                    "workspace_snapshot_hash": self.snapshot_hash,
                },
                private=True,
            )
            shutil.copyfile(self.catalog_path, self.output / "workspace-catalog.private.json")
            write_json(
                self.output / "buckets.private.json",
                [bucket.model_dump(mode="json") for bucket in buckets],
                private=True,
            )
        resume_audit: dict[str, Any] = {
            "resumed": output_exists,
            "reused_bucket_ids": [],
            "archived_bucket_attempts": [],
        }
        def summarize(bucket: DeterministicBucket) -> BucketSummary:
            if output_exists:
                existing = self._load_reusable_bucket_summary(bucket)
                if existing is not None:
                    resume_audit["reused_bucket_ids"].append(bucket.bucket_id)
                    return existing
                archived = self._archive_incomplete_bucket_attempt(bucket)
                if archived is not None:
                    resume_audit["archived_bucket_attempts"].append(archived)
            root = self._prepare_bucket(bucket)
            self._run(key=f"buckets/{bucket.bucket_id}", root=root, prompt=render_bucket_prompt(bucket))
            summary = load_json_model(root / "output" / "bucket-summary.json", BucketSummary)
            assert isinstance(summary, BucketSummary)
            if summary.bucket_id != bucket.bucket_id:
                raise WorkspaceCollectionCoverError("bucket summary changed its assigned bucket_id")
            return summary

        summaries_by_id: dict[str, BucketSummary] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.config.max_parallel_buckets, len(buckets)),
            thread_name_prefix="workspace-cover",
        ) as executor:
            futures = {executor.submit(summarize, bucket): bucket.bucket_id for bucket in buckets}
            for future in as_completed(futures):
                bucket_id = futures[future]
                try:
                    summaries_by_id[bucket_id] = future.result()
                except Exception as exc:
                    for pending in futures:
                        pending.cancel()
                    raise WorkspaceCollectionCoverError(f"bucket summary failed: {bucket_id}") from exc
        summaries = [summaries_by_id[bucket.bucket_id] for bucket in buckets]
        if output_exists:
            write_json(
                self.output / "resume-audit.private.json",
                {
                    "format": "workspace-bench.workspace-collection-cover-resume-audit.v1",
                    "workspace_snapshot_hash": self.snapshot_hash,
                    "reused_bucket_ids": sorted(resume_audit["reused_bucket_ids"]),
                    "archived_bucket_attempts": sorted(resume_audit["archived_bucket_attempts"]),
                },
                private=True,
            )

        summary_by_id = {summary.bucket_id: summary for summary in summaries}
        cards = [
            WorkspaceCollectionSummaryCard(
                card_id=bucket.bucket_id,
                title=summary_by_id[bucket.bucket_id].title,
                description=summary_by_id[bucket.bucket_id].description,
                representative_content=summary_by_id[bucket.bucket_id].representative_content,
                file_count=len(bucket.paths),
            )
            for bucket in sorted(buckets, key=lambda item: item.bucket_id)
        ]
        memberships = {
            bucket.bucket_id: bucket.paths
            for bucket in sorted(buckets, key=lambda item: item.bucket_id)
        }

        def membership_state_hash(value: dict[str, list[str]]) -> str:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()

        seen_states = {membership_state_hash(memberships)}
        rounds_executed = 0
        converged = False
        round_audit: list[dict[str, Any]] = []
        for round_index in range(1, self.config.max_coordination_rounds + 1):
            coordinator = (
                self.output
                / "runs.private"
                / "coordination"
                / f"round-{round_index:02d}"
                / "coordinator"
                / "workdir"
            )
            (coordinator / "input").mkdir(parents=True, mode=0o700)
            (coordinator / "output").mkdir(mode=0o700)
            write_json(
                coordinator / "input" / "cards.json",
                [card.model_dump(mode="json") for card in cards],
                private=True,
            )
            write_json(coordinator / "input" / "members.json", memberships, private=True)
            self._run(
                key=f"coordination/round-{round_index:02d}/coordinator",
                root=coordinator,
                prompt=render_coordinator_prompt(round_index=round_index),
            )
            plan = load_json_model(
                coordinator / "output" / "coordination-plan.json",
                CoordinationPlan,
            )
            assert isinstance(plan, CoordinationPlan)
            previous_memberships = memberships
            reduction = apply_coordination_plan_to_state(
                current_cards=cards,
                current_memberships=memberships,
                plan=plan,
            )
            rounds_executed = round_index
            round_record: dict[str, Any] = {
                "round": round_index,
                "operation_count": len(plan.operations),
                "changed": reduction.changed,
                "affected_card_ids": list(reduction.affected_card_ids),
            }
            if not reduction.changed:
                cards = reduction.cards
                memberships = reduction.memberships
                converged = True
                round_audit.append(round_record)
                break

            state_hash = membership_state_hash(reduction.memberships)
            if state_hash in seen_states:
                raise WorkspaceCollectionCoverError("coordination entered a repeated membership state")
            seen_states.add(state_hash)
            reduction_cards = {card.card_id: card for card in reduction.cards}

            def refine(card_id: str) -> ProposedCard:
                card = reduction_cards[card_id]
                root = self._prepare_refinement(
                    round_index=round_index,
                    card=card,
                    current_paths=reduction.memberships[card_id],
                    previous_paths=previous_memberships.get(card_id, []),
                )
                self._run(
                    key=f"coordination/round-{round_index:02d}/refine/{card_id}",
                    root=root,
                    prompt=render_refinement_prompt(card_id=card_id, round_index=round_index),
                )
                refined = load_json_model(root / "output" / "card-summary.json", ProposedCard)
                assert isinstance(refined, ProposedCard)
                if refined.card_id != card_id:
                    raise WorkspaceCollectionCoverError("refinement changed its assigned card_id")
                return refined

            refined_by_id: dict[str, ProposedCard] = {}
            affected = list(reduction.affected_card_ids)
            with ThreadPoolExecutor(
                max_workers=min(self.config.max_parallel_buckets, len(affected)),
                thread_name_prefix=f"workspace-refine-{round_index}",
            ) as executor:
                futures = {executor.submit(refine, card_id): card_id for card_id in affected}
                for future in as_completed(futures):
                    card_id = futures[future]
                    try:
                        refined_by_id[card_id] = future.result()
                    except Exception as exc:
                        for pending in futures:
                            pending.cancel()
                        raise WorkspaceCollectionCoverError(
                            f"collection refinement failed: {card_id}"
                        ) from exc
            cards = [
                WorkspaceCollectionSummaryCard(
                    **(
                        refined_by_id[card.card_id].model_dump(mode="json")
                        if card.card_id in refined_by_id
                        else {
                            "card_id": card.card_id,
                            "title": card.title,
                            "description": card.description,
                            "representative_content": card.representative_content,
                        }
                    ),
                    file_count=len(reduction.memberships[card.card_id]),
                )
                for card in reduction.cards
            ]
            memberships = reduction.memberships
            round_record["refined_card_ids"] = sorted(refined_by_id)
            round_audit.append(round_record)

        collection = WorkspaceCollectionSetV3(
            workspace_snapshot_hash=self.snapshot_hash,
            distinct_file_count=len(self.catalog.files),
            membership_count=sum(len(paths) for paths in memberships.values()),
            cards=cards,
        )
        final = self.output / "final"
        final.mkdir(mode=0o700)
        public_path = final / "workspace-collection-set.public.json"
        public_hash = write_json(public_path, collection.model_dump(mode="json"), private=False)
        index_path = final / "workspace-collection-map.members.sqlite"
        index_hash = build_workspace_collection_v3_index(
            collection,
            memberships=memberships,
            index_path=index_path,
        )
        audit = {
            "format": "workspace-bench.workspace-collection-cover-audit.v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "construction_kind": "workspace_snapshot_exhaustive_collection_map",
            "task_input_conditioned": False,
            "workspace_snapshot_hash": self.snapshot_hash,
            "workspace_catalog_sha256": sha256_file(self.output / "workspace-catalog.private.json"),
            "agent_visible_collection_set_sha256": public_hash,
            "member_index_sha256": index_hash,
            "bucket_count": len(buckets),
            "card_count": len(cards),
            "distinct_file_count": collection.distinct_file_count,
            "membership_count": collection.membership_count,
            "coordination_rounds_executed": rounds_executed,
            "coordination_converged": converged,
            "coordination_rounds": round_audit,
        }
        write_json(final / "workspace-collection-map.private.json", audit, private=True)
        result = {
            "status": "PASS",
            "workspace_collection_set_path": str(public_path),
            "workspace_collection_set_sha256": public_hash,
            "workspace_collection_member_index_path": str(index_path),
            "workspace_collection_member_index_sha256": index_hash,
            "bucket_count": len(buckets),
            "card_count": len(cards),
            "distinct_file_count": collection.distinct_file_count,
            "membership_count": collection.membership_count,
            "coordination_rounds_executed": rounds_executed,
            "coordination_converged": converged,
        }
        write_json(final / "result.private.json", result, private=True)
        return result


class WorkspaceCollectionContinuationOrchestrator:
    """Continue coordination from an immutable completed v3 parent version."""

    def __init__(
        self,
        *,
        config: WorkspaceCollectionContinuationConfig,
        codex_runner: CodexRunner,
    ) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self.workspace = Path(config.workspace_root).resolve(strict=True)
        self.output = Path(config.output_root).resolve()
        (
            self.parent_collection,
            self.parent_memberships,
            self.parent_public_hash,
            self.parent_index_hash,
        ) = load_completed_v3_state(
            workspace_root=self.workspace,
            collection_set_path=config.parent_collection_set_path,
            member_index_path=config.parent_member_index_path,
        )
        self.snapshot_hash = self.parent_collection.workspace_snapshot_hash
        self.parent_audit_path = Path(config.parent_audit_path).resolve(strict=True)
        if self.parent_audit_path.is_symlink() or not self.parent_audit_path.is_file():
            raise WorkspaceCollectionCoverError("parent audit must be a regular file")
        try:
            self.parent_audit = json.loads(self.parent_audit_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkspaceCollectionCoverError("parent audit is invalid JSON") from exc
        if not isinstance(self.parent_audit, dict):
            raise WorkspaceCollectionCoverError("parent audit must be a JSON object")
        if (
            self.parent_audit.get("agent_visible_collection_set_sha256") != self.parent_public_hash
            or self.parent_audit.get("member_index_sha256") != self.parent_index_hash
            or self.parent_audit.get("workspace_snapshot_hash") != self.snapshot_hash
        ):
            raise WorkspaceCollectionCoverError("parent audit does not bind the supplied v3 artefacts")
        prior_rounds = self.parent_audit.get("coordination_rounds_executed")
        if isinstance(prior_rounds, bool) or not isinstance(prior_rounds, int) or prior_rounds < 0:
            raise WorkspaceCollectionCoverError("parent audit has an invalid coordination round count")
        self.parent_rounds = prior_rounds
        self.oracle_context: OracleTaskSuiteContext | None = None
        self.oracle_context_hash: str | None = None
        if config.oracle_task_suite_context_path is not None:
            context_path = Path(config.oracle_task_suite_context_path).resolve(strict=True)
            info = context_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WorkspaceCollectionCoverError("oracle task-suite context must be a regular file")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise WorkspaceCollectionCoverError("oracle task-suite context must have mode 0600")
            loaded = load_json_model(context_path, OracleTaskSuiteContext)
            assert isinstance(loaded, OracleTaskSuiteContext)
            if loaded.workspace_snapshot_hash != self.snapshot_hash:
                raise WorkspaceCollectionCoverError(
                    "oracle task-suite context does not match the workspace snapshot"
                )
            self.oracle_context = loaded
            self.oracle_context_hash = sha256_file(context_path)
            self.coordinator_focus_paths = self._validate_workspace_paths(
                sorted(
                    {
                        path
                        for task in loaded.tasks
                        for path in task.required_workspace_paths
                    }
                )
            )
        else:
            self.coordinator_focus_paths = self._validate_workspace_paths(
                self.config.coordinator_focus_paths
            )

    def _validate_workspace_paths(self, raw_paths: list[str]) -> list[str]:
        validated: list[str] = []
        for raw_path in raw_paths:
            pure = PurePosixPath(raw_path)
            if (
                pure.is_absolute()
                or not pure.parts
                or any(part in {"", ".", ".."} for part in pure.parts)
                or pure.as_posix() != raw_path
            ):
                raise WorkspaceCollectionCoverError("coordinator focus path is not canonical")
            source = self.workspace.joinpath(*pure.parts)
            try:
                info = source.lstat()
                source.resolve(strict=True).relative_to(self.workspace)
            except (OSError, ValueError) as exc:
                raise WorkspaceCollectionCoverError("coordinator focus path is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WorkspaceCollectionCoverError("coordinator focus path must be a regular file")
            validated.append(raw_path)
        return validated

    def _stage_coordinator_focus(self, coordinator: Path) -> None:
        if not self.config.task_input_conditioned:
            return
        focus_root = coordinator / "FOCUS_ROOT"
        if focus_root.exists():
            return
        focus_root.mkdir(mode=0o700, exist_ok=True)
        for relative in self.coordinator_focus_paths:
            source = self.workspace.joinpath(*PurePosixPath(relative).parts)
            target = focus_root.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _readonly_tree(focus_root)
        if self.oracle_context is not None:
            write_json(
                coordinator / "input" / "task-suite-map-context.private.json",
                self.oracle_context.model_dump(mode="json"),
                private=True,
            )
        else:
            write_json(
                coordinator / "input" / "coordinator-focus.json",
                {
                    "guidance": self.config.coordinator_guidance,
                    "paths": self.coordinator_focus_paths,
                },
                private=True,
            )

    def _provider(self) -> dict[str, Any]:
        provider: dict[str, Any] = {
            "authMode": self.config.auth_mode,
            "model": self.config.model,
            "__codex_runtime__": {
                "expected_cli_version": self.config.expected_codex_version,
                "protocol": "responses",
                "reasoning_effort": self.config.reasoning_effort,
                "mcp_servers": {},
                "tool_schemas": {},
            },
        }
        if self.config.base_url is not None:
            provider["baseUrl"] = self.config.base_url
        return provider

    def _run(self, *, key: str, root: Path, prompt: str) -> None:
        audit = self.output / "runs.private" / key
        audit.mkdir(parents=True, exist_ok=True, mode=0o700)
        prompt_path = audit / "prompt.private.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        result = self.codex_runner(
            prompt=prompt,
            work_dir=str(root),
            sandbox_dir=str(audit / "runtime.private"),
            timeout_s=self.config.timeout_seconds,
            api_provider=self._provider(),
            agent_id=key.replace("/", "-"),
        )
        write_json(audit / "codex-result.private.json", result, private=True)
        trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
        if result.get("status") != "ok" or collection.get("complete") is not True:
            raise WorkspaceCollectionCoverError(f"{key} failed or produced an incomplete trace")

    def _prepare_refinement(
        self,
        *,
        round_number: int,
        card: WorkspaceCollectionSummaryCard,
        current_paths: list[str],
        previous_paths: list[str],
    ) -> Path:
        root = (
            self.output
            / "runs.private"
            / "coordination"
            / f"round-{round_number:02d}"
            / "refine"
            / card.card_id
            / "workdir"
        )
        collection_root = root / "COLLECTION_ROOT"
        collection_root.mkdir(parents=True, mode=0o700)
        for relative in current_paths:
            source = self.workspace / relative
            target = collection_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _readonly_tree(collection_root)
        (root / "input").mkdir(mode=0o700)
        (root / "output").mkdir(mode=0o700)
        write_json(root / "input" / "previous-card.json", card.model_dump(mode="json"), private=True)
        current = set(current_paths)
        previous = set(previous_paths)
        write_json(
            root / "input" / "membership-change.json",
            {
                "added": sorted(current - previous),
                "removed": sorted(previous - current),
                "current_file_count": len(current_paths),
            },
            private=True,
        )
        return root

    @staticmethod
    def _state_hash(memberships: dict[str, list[str]]) -> str:
        raw = json.dumps(memberships, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _stored_run_config(self) -> dict[str, Any]:
        stored = self.config.model_dump(
            mode="json",
            exclude={"base_url", "resume_existing_output"},
        )
        stored.update(
            {
                "base_url_sha256": (
                    "sha256:" + hashlib.sha256(self.config.base_url.encode()).hexdigest()
                    if self.config.base_url
                    else None
                ),
                "orchestrator_version": COVER_SYNTHESIS_VERSION,
                "workspace_snapshot_hash": self.snapshot_hash,
                "parent_collection_set_sha256": self.parent_public_hash,
                "parent_member_index_sha256": self.parent_index_hash,
                "parent_audit_sha256": sha256_file(self.parent_audit_path),
                "parent_coordination_rounds_executed": self.parent_rounds,
                "oracle_task_suite_context_sha256": self.oracle_context_hash,
            }
        )
        return stored

    def _validate_resume_state(self) -> None:
        try:
            stored = json.loads(
                (self.output / "run-config.private.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceCollectionCoverError("continuation resume output has no valid run config") from exc
        expected = self._stored_run_config()
        if stored != expected:
            changed = sorted(
                key for key in set(stored) | set(expected) if stored.get(key) != expected.get(key)
            )
            detail = changed[0] if changed else "unknown"
            raise WorkspaceCollectionCoverError(
                f"continuation resume config changed immutable field: {detail}"
            )
        if (self.output / "final").exists():
            raise WorkspaceCollectionCoverError("completed continuation output cannot be resumed")

    def _archive_role_attempt(self, key: str) -> str | None:
        role_root = self.output / "runs.private" / key
        if not role_root.exists():
            return None
        for attempt in range(1, 100):
            archived = role_root.with_name(f"{role_root.name}.failed-attempt-{attempt:02d}")
            if not archived.exists():
                role_root.rename(archived)
                return str(archived.relative_to(self.output / "runs.private"))
        raise WorkspaceCollectionCoverError(f"too many archived attempts for {key}")

    def _load_reusable_role_output(
        self,
        *,
        key: str,
        relative_output: str,
        model: type[StrictModel],
    ) -> StrictModel | None:
        role_root = self.output / "runs.private" / key
        try:
            result = json.loads(
                (role_root / "codex-result.private.json").read_text(encoding="utf-8")
            )
            trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
            collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
            if result.get("status") != "ok" or collection.get("complete") is not True:
                return None
            return load_json_model(role_root / "workdir" / relative_output, model)
        except (OSError, json.JSONDecodeError, CollectionMapError):
            return None

    def run(self) -> dict[str, Any]:
        if os.environ.get("CODEX_SANDBOX_MODE") != "danger-full-access":
            raise WorkspaceCollectionCoverError("CODEX_SANDBOX_MODE must be danger-full-access")
        output_exists = self.output.exists()
        if output_exists and not self.config.resume_existing_output:
            raise WorkspaceCollectionCoverError("output_root must not already exist")
        try:
            self.output.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise WorkspaceCollectionCoverError("output_root must be outside workspace_root")
        if output_exists:
            self._validate_resume_state()
        else:
            self.output.mkdir(parents=True, mode=0o700)
            write_json(self.output / "run-config.private.json", self._stored_run_config(), private=True)

        resume_audit: dict[str, Any] = {
            "resumed": output_exists,
            "reused_coordinator_rounds": [],
            "reused_refinement_roles": [],
            "archived_role_attempts": [],
        }

        cards = list(self.parent_collection.cards)
        memberships = {
            card_id: list(paths)
            for card_id, paths in sorted(self.parent_memberships.items())
        }
        seen_states = {self._state_hash(memberships)}
        added_rounds = 0
        converged = False
        round_audit: list[dict[str, Any]] = []
        for local_round in range(1, self.config.max_additional_coordination_rounds + 1):
            round_number = self.parent_rounds + local_round
            coordinator = (
                self.output
                / "runs.private"
                / "coordination"
                / f"round-{round_number:02d}"
                / "coordinator"
                / "workdir"
            )
            coordinator_key = f"coordination/round-{round_number:02d}/coordinator"
            coordinator_preexisted = (
                self.output / "runs.private" / coordinator_key
            ).exists()
            (coordinator / "input").mkdir(parents=True, mode=0o700, exist_ok=True)
            (coordinator / "output").mkdir(mode=0o700, exist_ok=True)
            write_json(
                coordinator / "input" / "cards.json",
                [card.model_dump(mode="json") for card in cards],
                private=True,
            )
            write_json(coordinator / "input" / "members.json", memberships, private=True)
            self._stage_coordinator_focus(coordinator)
            previous_memberships = memberships
            plan = self._load_reusable_role_output(
                key=coordinator_key,
                relative_output="output/coordination-plan.json",
                model=CoordinationPlan,
            ) if coordinator_preexisted else None
            reduction: CoordinationReduction | None = None
            if isinstance(plan, CoordinationPlan):
                try:
                    reduction = apply_coordination_plan_to_state(
                        current_cards=cards,
                        current_memberships=memberships,
                        plan=plan,
                    )
                except (WorkspaceCollectionCoverError, CollectionMapError, ValueError):
                    plan = None
                else:
                    resume_audit["reused_coordinator_rounds"].append(round_number)
            if plan is None:
                archived = self._archive_role_attempt(coordinator_key) if coordinator_preexisted else None
                if archived is not None:
                    resume_audit["archived_role_attempts"].append(archived)
                    coordinator = (
                        self.output / "runs.private" / "coordination"
                        / f"round-{round_number:02d}" / "coordinator" / "workdir"
                    )
                    (coordinator / "input").mkdir(parents=True, mode=0o700)
                    (coordinator / "output").mkdir(mode=0o700)
                    write_json(
                        coordinator / "input" / "cards.json",
                        [card.model_dump(mode="json") for card in cards],
                        private=True,
                    )
                    write_json(coordinator / "input" / "members.json", memberships, private=True)
                    self._stage_coordinator_focus(coordinator)
                self._run(
                    key=coordinator_key,
                    root=coordinator,
                    prompt=render_coordinator_prompt(
                        round_index=round_number,
                        guidance=self.config.coordinator_guidance,
                        focus_paths=self.coordinator_focus_paths,
                        oracle_task_suite=self.oracle_context is not None,
                        quality_profile=self.config.quality_profile,
                    ),
                )
                loaded = load_json_model(
                    coordinator / "output" / "coordination-plan.json",
                    CoordinationPlan,
                )
                assert isinstance(loaded, CoordinationPlan)
                plan = loaded
                reduction = apply_coordination_plan_to_state(
                    current_cards=cards,
                    current_memberships=memberships,
                    plan=plan,
                )
            assert reduction is not None
            added_rounds = local_round
            record: dict[str, Any] = {
                "round": round_number,
                "operation_count": len(plan.operations),
                "changed": reduction.changed,
                "affected_card_ids": list(reduction.affected_card_ids),
            }
            if not reduction.changed:
                cards = reduction.cards
                memberships = reduction.memberships
                converged = True
                round_audit.append(record)
                if self.config.stop_on_convergence:
                    break
                continue
            converged = False
            state_hash = self._state_hash(reduction.memberships)
            if state_hash in seen_states:
                raise WorkspaceCollectionCoverError("continuation entered a repeated membership state")
            seen_states.add(state_hash)
            reduction_cards = {card.card_id: card for card in reduction.cards}

            def refine(card_id: str) -> ProposedCard:
                card = reduction_cards[card_id]
                refinement_key = f"coordination/round-{round_number:02d}/refine/{card_id}"
                reusable = self._load_reusable_role_output(
                    key=refinement_key,
                    relative_output="output/card-summary.json",
                    model=ProposedCard,
                ) if output_exists else None
                if isinstance(reusable, ProposedCard) and reusable.card_id == card_id:
                    resume_audit["reused_refinement_roles"].append(refinement_key)
                    return reusable
                archived = self._archive_role_attempt(refinement_key) if output_exists else None
                if archived is not None:
                    resume_audit["archived_role_attempts"].append(archived)
                root = self._prepare_refinement(
                    round_number=round_number,
                    card=card,
                    current_paths=reduction.memberships[card_id],
                    previous_paths=previous_memberships.get(card_id, []),
                )
                self._run(
                    key=refinement_key,
                    root=root,
                    prompt=render_refinement_prompt(
                        card_id=card_id,
                        round_index=round_number,
                        oracle_task_suite=self.oracle_context is not None,
                        quality_profile=self.config.quality_profile,
                    ),
                )
                refined = load_json_model(root / "output" / "card-summary.json", ProposedCard)
                assert isinstance(refined, ProposedCard)
                if refined.card_id != card_id:
                    raise WorkspaceCollectionCoverError("refinement changed its assigned card_id")
                return refined

            refined_by_id: dict[str, ProposedCard] = {}
            affected = list(reduction.affected_card_ids)
            if not affected:
                raise WorkspaceCollectionCoverError("changed continuation round has no refinable cards")
            with ThreadPoolExecutor(
                max_workers=min(self.config.max_parallel_refinements, len(affected)),
                thread_name_prefix=f"workspace-continue-{round_number}",
            ) as executor:
                futures = {executor.submit(refine, card_id): card_id for card_id in affected}
                for future in as_completed(futures):
                    card_id = futures[future]
                    try:
                        refined_by_id[card_id] = future.result()
                    except Exception as exc:
                        for pending in futures:
                            pending.cancel()
                        raise WorkspaceCollectionCoverError(
                            f"collection continuation refinement failed: {card_id}"
                        ) from exc
            cards = [
                WorkspaceCollectionSummaryCard(
                    **(
                        refined_by_id[card.card_id].model_dump(mode="json")
                        if card.card_id in refined_by_id
                        else {
                            "card_id": card.card_id,
                            "title": card.title,
                            "description": card.description,
                            "representative_content": card.representative_content,
                        }
                    ),
                    file_count=len(reduction.memberships[card.card_id]),
                )
                for card in reduction.cards
            ]
            memberships = reduction.memberships
            record["refined_card_ids"] = sorted(refined_by_id)
            round_audit.append(record)

        if output_exists:
            write_json(
                self.output / "resume-audit.private.json",
                {
                    "format": "workspace-bench.workspace-collection-continuation-resume-audit.v1",
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "workspace_snapshot_hash": self.snapshot_hash,
                    "parent_collection_set_sha256": self.parent_public_hash,
                    "parent_member_index_sha256": self.parent_index_hash,
                    "reused_coordinator_rounds": sorted(resume_audit["reused_coordinator_rounds"]),
                    "reused_refinement_roles": sorted(resume_audit["reused_refinement_roles"]),
                    "archived_role_attempts": sorted(resume_audit["archived_role_attempts"]),
                },
                private=True,
            )

        oracle_coverage: dict[str, Any] | None = None
        if self.oracle_context is not None:
            oracle_coverage = validate_oracle_task_coverage(
                context=self.oracle_context,
                memberships=memberships,
                parent_card_ids=set(self.parent_memberships),
            )

        collection = WorkspaceCollectionSetV3(
            workspace_snapshot_hash=self.snapshot_hash,
            distinct_file_count=self.parent_collection.distinct_file_count,
            membership_count=sum(len(paths) for paths in memberships.values()),
            cards=cards,
        )
        final = self.output / "final"
        final.mkdir(mode=0o700)
        public_path = final / "workspace-collection-set.public.json"
        public_hash = write_json(public_path, collection.model_dump(mode="json"), private=False)
        index_path = final / "workspace-collection-map.members.sqlite"
        index_hash = build_workspace_collection_v3_index(
            collection,
            memberships=memberships,
            index_path=index_path,
        )
        total_rounds = self.parent_rounds + added_rounds
        audit = {
            "format": "workspace-bench.workspace-collection-continuation-audit.v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "construction_kind": (
                "task_oracle_upper_bound_workspace_collection_map"
                if self.oracle_context is not None
                else "targeted_workspace_collection_map_continuation"
                if self.config.task_input_conditioned
                else "workspace_collection_map_continuation"
            ),
            "task_input_conditioned": self.config.task_input_conditioned,
            "quality_profile": self.config.quality_profile,
            "coordinator_focus_paths": self.coordinator_focus_paths,
            "coordinator_guidance_sha256": (
                "sha256:" + hashlib.sha256(self.config.coordinator_guidance.encode()).hexdigest()
                if self.config.coordinator_guidance is not None
                else None
            ),
            "oracle_task_suite_context_sha256": self.oracle_context_hash,
            "workspace_snapshot_hash": self.snapshot_hash,
            "parent_collection_set_sha256": self.parent_public_hash,
            "parent_member_index_sha256": self.parent_index_hash,
            "parent_audit_sha256": sha256_file(self.parent_audit_path),
            "parent_coordination_rounds_executed": self.parent_rounds,
            "agent_visible_collection_set_sha256": public_hash,
            "member_index_sha256": index_hash,
            "card_count": len(cards),
            "distinct_file_count": collection.distinct_file_count,
            "membership_count": collection.membership_count,
            "additional_coordination_rounds_executed": added_rounds,
            "additional_coordination_rounds_requested": self.config.max_additional_coordination_rounds,
            "stop_on_convergence": self.config.stop_on_convergence,
            "coordination_rounds_executed": total_rounds,
            "coordination_converged": converged,
            "coordination_rounds": round_audit,
        }
        write_json(final / "workspace-collection-map.private.json", audit, private=True)
        if oracle_coverage is not None:
            parent_sets = {
                card_id: set(paths) for card_id, paths in self.parent_memberships.items()
            }
            final_sets = {card_id: set(paths) for card_id, paths in memberships.items()}
            membership_delta = {
                card_id: {
                    "added": sorted(final_sets.get(card_id, set()) - parent_sets.get(card_id, set())),
                    "removed": sorted(parent_sets.get(card_id, set()) - final_sets.get(card_id, set())),
                }
                for card_id in sorted(set(parent_sets) | set(final_sets))
                if parent_sets.get(card_id, set()) != final_sets.get(card_id, set())
            }
            oracle_audit = {
                "format": "workspace-bench.oracle-map-audit.private.v1",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "construction_kind": "task_oracle_upper_bound",
                "workspace_snapshot_hash": self.snapshot_hash,
                "oracle_task_suite_context_sha256": self.oracle_context_hash,
                "parent_collection_set_sha256": self.parent_public_hash,
                "agent_visible_collection_set_sha256": public_hash,
                "included_task_ids": [task.task_id for task in self.oracle_context.tasks],
                "excluded_tasks": [
                    item.model_dump(mode="json") for item in self.oracle_context.excluded_tasks
                ],
                "new_card_ids": sorted(set(final_sets) - set(parent_sets)),
                "deleted_card_ids": sorted(set(parent_sets) - set(final_sets)),
                "membership_delta": membership_delta,
                **oracle_coverage,
            }
            write_json(final / "oracle-map-audit.private.json", oracle_audit, private=True)
        result = {
            "status": "PASS",
            "workspace_collection_set_path": str(public_path),
            "workspace_collection_set_sha256": public_hash,
            "workspace_collection_member_index_path": str(index_path),
            "workspace_collection_member_index_sha256": index_hash,
            "parent_collection_set_sha256": self.parent_public_hash,
            "parent_member_index_sha256": self.parent_index_hash,
            "card_count": len(cards),
            "distinct_file_count": collection.distinct_file_count,
            "membership_count": collection.membership_count,
            "additional_coordination_rounds_executed": added_rounds,
            "additional_coordination_rounds_requested": self.config.max_additional_coordination_rounds,
            "stop_on_convergence": self.config.stop_on_convergence,
            "coordination_rounds_executed": total_rounds,
            "coordination_converged": converged,
            "quality_profile": self.config.quality_profile,
            "oracle_task_suite_context_sha256": self.oracle_context_hash,
            "oracle_multi_file_task_count": (
                oracle_coverage["multi_file_task_count"] if oracle_coverage else None
            ),
            "oracle_multi_file_task_covered_count": (
                oracle_coverage["multi_file_task_covered_count"] if oracle_coverage else None
            ),
        }
        write_json(final / "result.private.json", result, private=True)
        return result
