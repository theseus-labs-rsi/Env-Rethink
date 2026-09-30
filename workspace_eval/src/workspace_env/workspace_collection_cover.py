"""Exhaustive, task-independent workspace collection-map synthesis.

The model supplies semantic summaries and optional re-grouping proposals.
Deterministic code owns catalog coverage, bucket boundaries, operation
validation, and the final public/SQLite split.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
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
    build_workspace_catalog,
    build_workspace_collection_v3_index,
    load_json_model,
    sha256_file,
    write_json,
)
from .manifest import StrictModel
from .workspace_digest import workspace_snapshot_hash


logger = logging.getLogger(__name__)

COVER_SYNTHESIS_VERSION = "workspace-collection-cover-v2"
RoleRunner = Callable[..., dict[str, Any]]


class WorkspaceCollectionCoverError(RuntimeError):
    pass


class WorkspaceCollectionCoverConfig(StrictModel):
    schema_version: Literal[2] = 2
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    workspace_root: str = Field(min_length=1)
    catalog_root: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=240)
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


def _json_schema(model: type[StrictModel]) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2)


def render_bucket_prompt(bucket: DeterministicBucket) -> str:
    return f"""你是角色 A，负责概括一个工作区文件桶。

只可读取 `BUCKET_ROOT/` 和 `BUCKET_CATALOG.json`。请按需选择并读取最有代表性的文件，不要求逐个打开。
根据真实目录、文件名和抽样内容生成一个简短、可检索的集合标题、简述和最多 6 条代表性内容。
读取约束：PDF、扫描件与图片请先用本地文本工具提取文本再阅读（例如 `pdftotext -layout <文件> -`、`python -c "import fitz; ..."`），不要以图像方式打开它们——当前模型的网关不接受内联图像内容，直接把图像读进来会导致调用失败。

不要写任务、rubric、judge 或参考答案。不要推断无法从本桶观察到的业务事实。

必须把结果写入工作目录下的 `output/bucket-summary.json`（先确认目录存在），严格符合下面 schema；
`bucket_id` 必须原样使用 `{bucket.bucket_id}`。不要在回复正文里贴 JSON，也不要用 markdown 代码块包裹文件内容。

```json
{_json_schema(BucketSummary)}
```
"""


def fallback_bucket_summary(bucket: DeterministicBucket) -> BucketSummary:
    """角色没能交出可用摘要时的兜底卡片。

    标题与简述直接来自桶自己的路径，**不编造**目录名观察不到的业务事实。
    它比 LLM 写的粗糙，但真实、可检索，而且让一颗坏掉的桶不至于作废整题 ——
    实测「description 超长 8 个字符」就杀掉了一整题一小时的工作，这个代价值不得。
    """

    def clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rstrip() + "…"

    scopes = bucket.path_scopes or bucket.paths
    title = clip("、".join(scope.rsplit("/", 1)[-1] for scope in scopes[:4]), 160)
    description = clip("该集合包含以下路径下的文件：" + "、".join(bucket.paths[:24]), 600)
    return BucketSummary(
        bucket_id=bucket.bucket_id,
        title=title or bucket.bucket_id,
        description=description or bucket.bucket_id,
        representative_content=bucket.paths[:6],
    )


def render_coordinator_prompt(
    *,
    round_index: int,
) -> str:
    return f"""你是全局集合协调员，正在执行第 {round_index} 轮协调。读取 `input/cards.json` 和 `input/members.json`。

**本轮的目标：让每个集合各自表达一个内聚的语义主题，把语义混杂的集合拆开。**
判断标准是**文件的语义归属**（同一件事/同一个项目/同一类材料），**不是文件数量、不是目录层级**。
前序分桶是按文件数量机械切分的，因此经常出现「一个集合里塞了几件互不相干的事」。
请逐张检查 `input/cards.json`，对每张卡回答：这张卡里的文件是否都在讲同一件事？
- 若**否** → 必须 `split`：按语义边界把该集合完整拆分（无遗漏、无重复地列出源集合全部成员）；
  拆出的每个集合要有自己的主题，不要只是把大桶平均切成两个数量相近的桶。
- 若**是**，但与其他集合高度同质（同一主题被拆成了多张卡）→ `merge`。
- 个别文件放错了集合 → `transfer`；一个文件确实同时属于两个主题 → `copy`。

**粒度要求（本模块的目的）：**
集合地图是用来「缩小检索范围」的索引 —— 使用者会先看卡片主题，挑出相关的几张，再用
`workspace_search(card_id)` 展开成员路径。因此**一张卡应当是一个可以被单独检索的主题**：
大致相当于「某一次活动的材料」「某一类制度文件」「某一个项目的预算与审批件」「某一批
外部参考」这种粒度。如果一张卡的主题本身还能自然分出两个以上会被分别检索的子主题
（例如把「全部历史版本与归档」和「现行材料」混在一张卡、把多个不同项目的同类文件塞在
一张卡），就继续拆开 —— **拆到「再拆下去就不像独立主题了」为止**。
文件少的任务同样适用：**不要因为文件总数少就把整个工作区压成一张卡**；只要内容上能分出
不同主题，就应该有多张卡。反过来，也不要为了凑数量把同一主题硬切成多张。

**不要做的事：** 本阶段**只按语义归类**。**不要判断哪一份是正本、现行版本或权威版本**，
也不要在标题/简述里写「现行」「有效」「正式」「以…为准」这类裁决性表述 —— 判断版本沿革是
工作历史（event log）的职责，集合地图只负责把材料按主题摆清楚。

不要为了凑数量而拆分，也不要把语义混杂的集合原样留下 —— **默认应该做调整，除非每张卡都已经语义内聚**。

其余约束：
- 集合已经覆盖整个工作区；同一文件可能属于多个集合。
- 操作类型：
  - transfer：把一个文件从一个集合移动到另一个集合；若源集合只有该文件，迁移后源卡片会被删除；
  - copy：让一个文件同时属于两个集合，不复制物理文件；
  - merge：合并多个高度同质集合；`source_card_ids` 必须至少包含两个唯一现有卡片，且必须提供完整的 `target_card`；
  - split：把一个语义混杂集合完整拆分。
- 不要按任务、rubric、judge 或参考答案组织集合。操作按数组顺序应用；每项操作只能引用当时仍存在的卡片，
不得再引用已被前一项 transfer 清空、merge 或 split 删除的卡片。操作引用必须精确，split 必须无遗漏、无重复地列出源集合全部成员。
必须把结果写入工作目录下的 `output/coordination-plan.json`（先确认目录存在），严格符合下面 schema；
没有必要调整时输出空 operations。不要在回复正文里贴 JSON。

```json
{_json_schema(CoordinationPlan)}
```
"""


def render_refinement_prompt(
    *,
    card_id: str,
    round_index: int,
) -> str:
    return f"""你是角色 A，负责在第 {round_index} 轮协调后重新整理一个受到影响的集合。

读取：
- `COLLECTION_ROOT/`：该集合调整后的全部当前成员；
- `input/previous-card.json`：调整前或协调器暂定的摘要；
- `input/membership-change.json`：本轮新增和移除的成员路径。

请根据当前真实成员重新选读代表文件，并完整重写标题、简述和最多 6 条代表性内容。
读取约束：PDF、扫描件与图片请先用本地文本工具提取文本再阅读（例如 `pdftotext -layout <文件> -`、`python -c "import fitz; ..."`），不要以图像方式打开它们——当前模型的网关不接受内联图像内容，直接把图像读进来会导致调用失败。
摘要必须描述当前集合，
不能因为上一版存在就机械保留。不要写任务、rubric、judge 或参考答案，也不要推断文件中无法核验的事实。

必须把结果写入工作目录下的 `output/card-summary.json`（先确认目录存在），严格符合下面 schema；
`card_id` 必须原样使用 `{card_id}`。不要在回复正文里贴 JSON。

```json
{_json_schema(ProposedCard)}
```
"""


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


class WorkspaceCollectionCoverOrchestrator:
    def __init__(self, *, config: WorkspaceCollectionCoverConfig, role_runner: RoleRunner) -> None:
        self.config = config
        self.role_runner = role_runner
        self.workspace = Path(config.workspace_root).resolve(strict=True)
        self.output = Path(config.output_root).resolve()
        self.snapshot_hash = workspace_snapshot_hash(str(self.workspace))
        self.catalog, self.catalog_path = build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=self.snapshot_hash,
            catalog_root=config.catalog_root,
        )

    def _provider(self) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "reasoning_effort": self.config.reasoning_effort,
        }

    def _record_degradations(self, entries: list[dict[str, Any]]) -> None:
        """把降级记到一份私有审计文件里（跨阶段追加）。

        降级过的产物**照常产出**，但必须留下痕迹：一张兜底卡片不该看起来和
        LLM 精心写的卡片一模一样，下游（含 judge）有权知道哪几张是退而求其次的。
        """

        if not entries:
            return
        path = self.output / "collection-cover-degradations.private.json"
        existing: list[dict[str, Any]] = []
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload.get("degradations"), list):
                    existing = payload["degradations"]
            except (OSError, json.JSONDecodeError, AttributeError):
                existing = []
        write_json(
            path,
            {
                "format": "workspace-bench.collection-cover-degradations.v1",
                "workspace_snapshot_hash": self.snapshot_hash,
                "degradations": existing + entries,
            },
            private=True,
        )

    def _run(
        self,
        *,
        key: str,
        root: Path,
        prompt: str,
        expect: Path | None = None,
    ) -> None:
        audit = self.output / "runs.private" / key
        audit.mkdir(parents=True, exist_ok=True, mode=0o700)
        (audit / "prompt.private.md").write_text(prompt, encoding="utf-8")
        result = self.role_runner(
            prompt=prompt,
            work_dir=str(root),
            sandbox_dir=str(audit / "runtime.private"),
            timeout_s=self.config.timeout_seconds,
            api_provider=self._provider(),
            agent_id=key.replace("/", "-"),
        )
        write_json(audit / "role-result.private.json", result, private=True)
        trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
        if result.get("status") != "ok" or collection.get("complete") is not True:
            # 角色以非零退出码收场，但它可能**已经把产物写完了**：deepseek 的
            # 思考回传约束（reasoning_content）会在收尾时把 session 打死，
            # 产物却是完整可用的（实测 bucket-summary.json 内容完全正确）。
            # 产物在就继续 —— 让一个做完的桶因为退出码作废，代价是一小时。
            if expect is not None and expect.is_file():
                logger.warning(
                    "%s reported status=%s but its artifact exists at %s; continuing with it",
                    key,
                    result.get("status"),
                    expect,
                )
                return
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
        result_path = role_root / "role-result.private.json"
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
            exclude={"resume_existing_output"},
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
        # Re-runnable after an interrupted run: the refinement role root is
        # rebuilt in place rather than rejecting an existing directory.
        collection_root = root / "COLLECTION_ROOT"
        collection_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        for relative in current_paths:
            source = self.workspace / relative
            target = collection_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _readonly_tree(collection_root)
        (root / "input").mkdir(mode=0o700, exist_ok=True)
        (root / "output").mkdir(mode=0o700, exist_ok=True)
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
                        exclude={"resume_existing_output"},
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
        #: 本阶段降级记录。产物照旧产出，但**把降级写下来**——一张兜底卡片
        #: 不应该看起来和 LLM 写的卡片一模一样，下游（含 judge）有权知道。
        degradations: list[dict[str, Any]] = []
        buckets_by_id = {bucket.bucket_id: bucket for bucket in buckets}

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
            summary_path = root / "output" / "bucket-summary.json"
            self._run(
                key=f"buckets/{bucket.bucket_id}",
                root=root,
                prompt=render_bucket_prompt(bucket),
                expect=summary_path,
            )
            summary = load_json_model(summary_path, BucketSummary)
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
                    # 一颗桶坏掉不该作废整题 —— 用桶自己的路径兜底，记一笔，继续。
                    logger.warning(
                        "bucket summary failed for %s (%s: %s); falling back to a path-derived card",
                        bucket_id,
                        type(exc).__name__,
                        exc,
                    )
                    degradations.append(
                        {
                            "stage": "bucket",
                            "bucket_id": bucket_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    summaries_by_id[bucket_id] = fallback_bucket_summary(buckets_by_id[bucket_id])
        if degradations:
            self._record_degradations(degradations)
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
            round_root = (
                self.output
                / "runs.private"
                / "coordination"
                / f"round-{round_index:02d}"
            )
            if output_exists and round_root.exists():
                # Keep the interrupted round for audit, then rebuild the roles.
                archived = round_root.with_name(
                    f"{round_root.name}.failed-attempt-{len(list(round_root.parent.glob(round_root.name + '.failed-attempt-*'))) + 1:02d}"
                )
                round_root.rename(archived)
            coordinator = round_root / "coordinator" / "workdir"
            (coordinator / "input").mkdir(parents=True, mode=0o700, exist_ok=True)
            (coordinator / "output").mkdir(mode=0o700, exist_ok=True)
            write_json(
                coordinator / "input" / "cards.json",
                [card.model_dump(mode="json") for card in cards],
                private=True,
            )
            write_json(coordinator / "input" / "members.json", memberships, private=True)
            plan_path = coordinator / "output" / "coordination-plan.json"
            self._run(
                key=f"coordination/round-{round_index:02d}/coordinator",
                root=coordinator,
                prompt=render_coordinator_prompt(round_index=round_index),
                expect=plan_path,
            )
            try:
                plan = load_json_model(plan_path, CoordinationPlan)
                assert isinstance(plan, CoordinationPlan)
                previous_memberships = memberships
                reduction = apply_coordination_plan_to_state(
                    current_cards=cards,
                    current_memberships=memberships,
                    plan=plan,
                )
            except (CollectionMapError, WorkspaceCollectionCoverError) as exc:
                # 协调员这一轮交出的方案用不了（引用了不存在的 card、方案自相矛盾……）。
                # **保留当前这套自洽的集合状态收工**，而不是作废整题：走到这一步时
                # 桶阶段和前几轮协调已经烧掉了一小时，而手上的状态是完整可用的。
                logger.warning(
                    "coordination round %d produced an unusable plan (%s: %s); keeping the previous state",
                    round_index,
                    type(exc).__name__,
                    exc,
                )
                self._record_degradations(
                    [
                        {
                            "stage": "coordination",
                            "round": round_index,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    ]
                )
                round_audit.append(
                    {
                        "round": round_index,
                        "skipped": True,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                break
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
                        # **单张卡的 refine 失败不该拖垮整轮。**
                        # 下面构造 cards 时，没有 refined 结果的卡会退回到协调器给的
                        # 暂定摘要（title/description/representative_content），
                        # 覆盖度不受影响，只是那张卡的摘要没那么精细。
                        # 之前这里直接 raise，代价是「一张卡挂掉 → 整轮重来」，
                        # 而 refine 的 LLM 调用又慢又偶发（deepseek 的
                        # reasoning_content 回传约束），整题因此反复失败。
                        logger.warning(
                            "collection refinement failed for card %s; "
                            "falling back to its provisional summary: %s",
                            card_id,
                            exc,
                        )
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
        final.mkdir(mode=0o700, exist_ok=True)
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
