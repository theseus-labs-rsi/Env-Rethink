"""Real Codex A/B construction of task-input anchored collection maps.

Deterministic code only stages actual inputs, records provenance, and rejects
schema/path violations.  The semantic grouping and its independent review are
written by real Codex executions; this module never fabricates collection
cards or a PASS review.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import ConfigDict, Field, model_validator

from .collection_map import (
    COLLECTION_SET_FORMAT,
    PRIVATE_AUDIT_FORMAT,
    CollectionMapError,
    CollectionSet,
    InputBundle,
    PrivateCollectionAudit,
    WorkspaceCatalog,
    build_workspace_catalog,
    load_json_model,
    sha256_file,
    task_input_bundle_from_metadata,
    validate_collection_set,
    write_json,
)
from .integration import workspace_snapshot_hash
from .manifest import StrictModel, canonical_json


COLLECTION_SYNTHESIS_VERSION = "codex-collection-synthesis-v1"
CodexRunner = Callable[..., dict[str, Any]]


class CollectionSynthesisError(RuntimeError):
    pass


class CollectionSynthesisConfig(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    task_metadata_path: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    catalog_root: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=240)
    auth_mode: Literal["chatgpt", "api"] = "chatgpt"
    base_url: str | None = None
    expected_codex_version: Literal["0.144.5"] = "0.144.5"
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    timeout_seconds: float = Field(default=600.0, gt=0)
    max_review_attempts: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def validate_provider(self) -> "CollectionSynthesisConfig":
        if self.auth_mode == "api" and not self.base_url:
            raise ValueError("api auth_mode requires base_url; the key comes from the environment")
        if self.auth_mode == "chatgpt" and self.base_url is not None:
            raise ValueError("chatgpt auth_mode must not set base_url")
        return self


class PrivateReview(StrictModel):
    verdict: Literal["PASS", "REVISE"]
    issues: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_review(self) -> "PrivateReview":
        if self.verdict == "PASS" and self.issues:
            raise ValueError("PASS review must not include issues")
        if self.verdict == "REVISE" and not self.issues:
            raise ValueError("REVISE review must include at least one issue")
        return self


class ReviewEnvelope(StrictModel):
    agent_visible: CollectionSet
    private_review: PrivateReview


def _json_schema(model: type[StrictModel]) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2)


def render_codex_a_prompt(*, task_requirement: str, repair: bool) -> str:
    repair_block = (
        "这是修订轮。先读取 `input/reviewer-feedback.json`，只解决其中指出的问题，然后重写完整输出。"
        if repair
        else "这是首轮构建。"
    )
    return f"""你是 Codex A，负责构建一个任务输入锚定的工作区集合地图。

{repair_block}

你可以读取：
- `TASK_INPUT_ROOT/__provided_task_inputs__/`：本任务实际收到的完整输入文件；这是集合判断的主要锚点。
- `TASK_INPUT_ROOT/` 下其余按工作区相对路径摆放的文件：已验证的输入→工作区候选绑定。
- `WORKSPACE_ROOT/`：完整只读工作区；用它查找能支持、解释或区分输入文件的材料。
- `CATALOG_PATH`：当前不可变工作区快照的确定性目录/文件事实目录。
- `TASK_REQUIREMENT.txt`：任务要求，只作为次要语境。

必须实际使用 Codex 和 shell 检查这些文件。先从 `TASK_INPUT_ROOT/__provided_task_inputs__/` 的实际文件开始，再在 `WORKSPACE_ROOT/` 中验证其
位置与相关材料。不要把相似文件、归档、样例或模板混入主要集合；它们应成为独立的并行集合，并明确边界。

输出要求：写入 `output/collection-set.json`，内容必须严格符合下面的 JSON Schema。不要在最终回复中粘贴 JSON。

```json
{_json_schema(CollectionSet)}
```

构建规则：
1. 输出一个集合组（`cards`），不是单一集合。每个集合只能是 `primary`、`supporting` 或 `parallel`。
2. `primary` 必须同时给出 `input_match_evidence` 和 `workspace_path_evidence`。前者只填写工作区中的目标路径与匹配方式；
   后者说明该路径在完整工作区中的位置。两者都必须对应 members 中已实际核验的文件。
   `TASK_INPUT_ROOT/__provided_task_inputs__/` 保存任务实际收到的输入；`TASK_INPUT_ROOT/` 的对应路径是已验证候选。
   若与工作区文件字节一致可用 `content_hash`，若仅能由唯一文件名对应则用 `filename`。若同名但有多个、内容不同的候选，
   只能用 `filename_ambiguous`，并根据实际目录和内容把候选区分为主要或并行集合；不要把它说成内容哈希对应。
   少数实际输入在工作区没有可验证的同名候选时，它仍会保留在 `__provided_task_inputs__/` 中，但不得编造对应工作区路径。
3. `supporting` 放直接支撑任务的材料；`parallel` 放相似、归档、样例或模板，必须写清为何不可与主集合混用。
4. 对来源、复制、归档或模板关系只能使用 `possible_relations` 中的 `possible_*` 关系，并说明可观察的依据；不能把推测说成确定事实。
5. 所有路径都是相对 `WORKSPACE_ROOT/` 的 POSIX 路径；cards、members 与 exploration_order 必须满足 schema 内的排序和唯一性约束。
6. `exploration_order` 应让后续执行者先看主要集合，再按需要查看支持与并行集合。文字保持短、具体、可行动。
7. 每个 `card_id` 必须以小写英文字母开头，只能使用小写字母、数字、连字符和下划线（例如 `primary-input-plans`，不能写 `01-primary-plans`）。
   `cards` 数组本身必须按 `card_id` 的字典序升序排列；`exploration_order` 可以是不同的阅读顺序，但必须恰好包含每张卡一次。
   写完后务必运行：`python3 -c 'import json; d=json.load(open("output/collection-set.json")); ids=[c["card_id"] for c in d["cards"]]; assert ids == sorted(ids) and len(ids) == len(set(ids)); assert set(d["exploration_order"]) == set(ids); assert all([m["path"] for m in c["members"]] == sorted(m["path"] for m in c["members"]) for c in d["cards"]); assert all(all(r["from_path"] in {{m["path"] for m in c["members"]}} and r["to_path"] in {{m["path"] for m in c["members"]}} for r in c["possible_relations"]) for c in d["cards"])'`。

`TASK_INPUT_ROOT`、`WORKSPACE_ROOT` 和 `CATALOG_PATH` 是当前工作目录下的实际路径名称。完成前用 Python 校验 JSON。

任务要求（次要语境）：
<TASK_REQUIREMENT>
{task_requirement}
</TASK_REQUIREMENT>
"""


def render_codex_b_prompt(*, task_requirement: str) -> str:
    return f"""你是 Codex B，独立审核 Codex A 生成的任务输入锚定工作区集合地图。

你可以读取：
- `TASK_INPUT_ROOT/__provided_task_inputs__/`：本任务实际收到的完整输入文件；这是审核的主要锚点。
- `TASK_INPUT_ROOT/` 下按工作区相对路径摆放的文件：已验证的输入→工作区候选绑定。
- `WORKSPACE_ROOT/`：完整只读工作区。
- `CATALOG_PATH`：确定性工作区文件目录。
- `TASK_REQUIREMENT.txt`：任务要求，只作为次要语境。
- `input/candidate.json`：Codex A 的候选输出。

必须亲自用 shell 检查输入文件、候选涉及的工作区文件和候选 JSON。审核 primary 是否确实同时有输入匹配与工作区位置证据；
审核 supporting/parallel 是否被恰当地分开；审核任何 `possible_*` 关系是否保持谨慎；审核路径、排序、成员和边界是否可执行。
若候选使用 `filename_ambiguous`，必须确认它没有被表述为内容相同，且目录/内容差异已被正确保留为边界。
审核关系端点时，可用 Python 检查每个 `r["from_path"]` 与 `r["to_path"]` 都属于同一张卡的 members。

写入 `output/review.json`，严格符合下面的 JSON Schema。`agent_visible` 必须逐字保留候选的集合内容，不能替 A 改写或补造内容。

```json
{_json_schema(ReviewEnvelope)}
```

当且仅当候选完整符合 schema 且上述审核均通过时使用 `private_review.verdict="PASS"`、空 issues；否则使用 `REVISE`，
并给出完整、具体、可执行的 issues。不要在最终回复中粘贴 JSON。

任务要求（次要语境）：
<TASK_REQUIREMENT>
{task_requirement}
</TASK_REQUIREMENT>
"""


def _readonly_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise CollectionSynthesisError("role input tree contains an unsafe file")
            os.chmod(path, 0o444)
        for name in directories:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise CollectionSynthesisError("role input tree contains an unsafe directory")
            os.chmod(path, 0o555)
    os.chmod(root, 0o555)


class CollectionSynthesisOrchestrator:
    def __init__(self, *, config: CollectionSynthesisConfig, codex_runner: CodexRunner) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self.workspace = Path(config.workspace_root).resolve(strict=True)
        self.metadata = Path(config.task_metadata_path).resolve(strict=True)
        self.output = Path(config.output_root).resolve()
        if not self.workspace.is_dir():
            raise CollectionSynthesisError("workspace_root must be a directory")
        self.snapshot_hash = workspace_snapshot_hash(str(self.workspace))
        self.catalog, self.catalog_path = build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=self.snapshot_hash,
            catalog_root=config.catalog_root,
        )
        self.input_bundle = task_input_bundle_from_metadata(
            self.metadata,
            workspace_root=self.workspace,
            workspace_snapshot_hash=self.snapshot_hash,
        )
        try:
            metadata_json = json.loads(self.metadata.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CollectionSynthesisError("task metadata is invalid JSON") from exc
        task_requirement = metadata_json.get("task") if isinstance(metadata_json, dict) else None
        if not isinstance(task_requirement, str) or not task_requirement.strip():
            raise CollectionSynthesisError("task metadata has no task requirement")
        self.task_requirement = task_requirement.strip()

    def _api_provider(self) -> dict[str, Any]:
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

    def _initialise_output(self) -> None:
        if self.output.exists():
            raise CollectionSynthesisError("output_root must not already exist")
        try:
            self.output.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise CollectionSynthesisError("output_root must be outside workspace_root")
        self.output.mkdir(parents=True, mode=0o700)
        stored = self.config.model_dump(mode="json")
        base_url = stored.pop("base_url", None)
        stored["base_url_sha256"] = (
            "sha256:" + hashlib.sha256(base_url.encode("utf-8")).hexdigest()
            if isinstance(base_url, str)
            else None
        )
        write_json(
            self.output / "run-config.private.json",
            {**stored, "orchestrator_version": COLLECTION_SYNTHESIS_VERSION, "workspace_snapshot_hash": self.snapshot_hash},
            private=True,
        )
        write_json(self.output / "input-bundle.private.json", self.input_bundle.model_dump(mode="json"), private=True)
        shutil.copyfile(self.catalog_path, self.output / "workspace-catalog.private.json")
        os.chmod(self.output / "workspace-catalog.private.json", 0o600)

    def _prepare_role_root(self, *, role_key: str) -> Path:
        root = self.output / "runs.private" / role_key / "workdir"
        root.mkdir(parents=True, mode=0o700)
        workspace = root / "WORKSPACE_ROOT"
        shutil.copytree(self.workspace, workspace, symlinks=False)
        _readonly_tree(workspace)
        inputs = root / "TASK_INPUT_ROOT"
        provided = inputs / "__provided_task_inputs__"
        source_relpaths = sorted(
            {entry.stored_relpath for entry in self.input_bundle.inputs}
            | {entry.stored_relpath for entry in self.input_bundle.unresolved_inputs}
        )
        for stored_relpath in source_relpaths:
            source = (self.metadata.parent / stored_relpath).resolve(strict=True)
            try:
                source.relative_to(self.metadata.parent)
            except ValueError as exc:
                raise CollectionSynthesisError("task input source escapes task root") from exc
            destination = provided / stored_relpath
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
        for entry in self.input_bundle.inputs:
            source = (self.metadata.parent / entry.stored_relpath).resolve(strict=True)
            try:
                source.relative_to(self.metadata.parent)
            except ValueError as exc:
                raise CollectionSynthesisError("task input source escapes task root") from exc
            destination = inputs / entry.workspace_path
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
        _readonly_tree(inputs)
        shutil.copyfile(self.catalog_path, root / "CATALOG_PATH")
        os.chmod(root / "CATALOG_PATH", 0o444)
        (root / "TASK_REQUIREMENT.txt").write_text(self.task_requirement + "\n", encoding="utf-8")
        os.chmod(root / "TASK_REQUIREMENT.txt", 0o444)
        (root / "input").mkdir(mode=0o700)
        (root / "output").mkdir(mode=0o700)
        return root

    def _run_role(self, *, role_key: str, prompt: str) -> dict[str, Any]:
        audit_root = self.output / "runs.private" / role_key
        role_root = audit_root / "workdir"
        prompt_path = audit_root / "prompt.private.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        before = workspace_snapshot_hash(str(role_root / "WORKSPACE_ROOT"))
        inputs_before = workspace_snapshot_hash(str(role_root / "TASK_INPUT_ROOT"))
        result = self.codex_runner(
            prompt=prompt,
            work_dir=str(role_root),
            sandbox_dir=str(audit_root / "runtime.private"),
            timeout_s=self.config.timeout_seconds,
            api_provider=self._api_provider(),
            agent_id=role_key.replace("/", "-"),
        )
        write_json(audit_root / "codex-result.private.json", result, private=True)
        if workspace_snapshot_hash(str(role_root / "WORKSPACE_ROOT")) != before:
            raise CollectionSynthesisError("Codex role modified its read-only workspace view")
        if workspace_snapshot_hash(str(role_root / "TASK_INPUT_ROOT")) != inputs_before:
            raise CollectionSynthesisError("Codex role modified its read-only task-input view")
        if result.get("status") != "ok":
            raise CollectionSynthesisError(f"{role_key} failed: {result.get('errorMessage') or result.get('status')}")
        trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
        if collection.get("complete") is not True:
            raise CollectionSynthesisError(f"{role_key} did not produce a complete Codex trace")
        return result

    @staticmethod
    def _load_collection(path: Path) -> CollectionSet:
        loaded = load_json_model(path, CollectionSet)
        assert isinstance(loaded, CollectionSet)
        return loaded

    def run(self) -> dict[str, Any]:
        if os.environ.get("CODEX_SANDBOX_MODE") != "danger-full-access":
            raise CollectionSynthesisError("CODEX_SANDBOX_MODE must be danger-full-access for native-shell A/B construction")
        self._initialise_output()
        previous_review: dict[str, Any] | None = None
        for attempt in range(1, self.config.max_review_attempts + 1):
            a_key = f"attempt-{attempt:02d}/codex-a"
            a_root = self._prepare_role_root(role_key=a_key)
            if previous_review is not None:
                write_json(a_root / "input" / "reviewer-feedback.json", previous_review, private=True)
            self._run_role(
                role_key=a_key,
                prompt=render_codex_a_prompt(task_requirement=self.task_requirement, repair=attempt > 1),
            )
            candidate_path = a_root / "output" / "collection-set.json"
            try:
                candidate = self._load_collection(candidate_path)
                validate_collection_set(candidate, catalog=self.catalog, input_bundle=self.input_bundle)
                mechanical: dict[str, Any] = {"valid": True, "errors": []}
            except (CollectionMapError, ValueError) as exc:
                candidate = None
                mechanical = {"valid": False, "errors": [str(exc)]}
            write_json(self.output / "runs.private" / a_key / "mechanical-validation.private.json", mechanical, private=True)

            # A malformed collection cannot be placed verbatim in ReviewEnvelope:
            # its `agent_visible` field intentionally has the same strict schema as
            # a valid collection.  Give the deterministic validator feedback to A
            # directly instead of asking B to manufacture an invalid envelope.
            if candidate is None:
                previous_review = {
                    "verdict": "REVISE",
                    "issues": [
                        "机械校验未通过：" + error
                        for error in mechanical["errors"]
                    ],
                }
                continue

            b_key = f"attempt-{attempt:02d}/codex-b"
            b_root = self._prepare_role_root(role_key=b_key)
            if candidate_path.is_file():
                shutil.copyfile(candidate_path, b_root / "input" / "candidate.json")
            write_json(b_root / "input" / "mechanical-validation.json", mechanical, private=True)
            self._run_role(role_key=b_key, prompt=render_codex_b_prompt(task_requirement=self.task_requirement))
            review_path = b_root / "output" / "review.json"
            try:
                review_raw = json.loads(review_path.read_text(encoding="utf-8"))
                review = ReviewEnvelope.model_validate(review_raw)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise CollectionSynthesisError("Codex B did not produce a valid review envelope") from exc
            if candidate is not None and review.agent_visible != candidate:
                raise CollectionSynthesisError("Codex B changed the agent-visible candidate instead of reviewing it")
            if review.private_review.verdict == "PASS":
                if candidate is None or not mechanical["valid"]:
                    raise CollectionSynthesisError("Codex B passed a mechanically invalid collection set")
                final = self.output / "final"
                final.mkdir(mode=0o700)
                visible_path = final / "collection-set.public.json"
                visible_hash = write_json(visible_path, candidate.model_dump(mode="json"), private=False)
                a_result_path = self.output / "runs.private" / a_key / "codex-result.private.json"
                b_result_path = self.output / "runs.private" / b_key / "codex-result.private.json"
                audit = PrivateCollectionAudit(
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    workspace_snapshot_hash=self.snapshot_hash,
                    input_bundle_sha256=sha256_file(self.output / "input-bundle.private.json"),
                    agent_visible_collection_set_sha256=visible_hash,
                    codex_a_artifact_sha256=sha256_file(a_result_path),
                    codex_b_artifact_sha256=sha256_file(b_result_path),
                )
                write_json(final / "collection-map.private.json", audit.model_dump(mode="json"), private=True)
                result = {
                    "status": "PASS",
                    "attempt": attempt,
                    "collection_set_path": str(visible_path),
                    "collection_set_sha256": visible_hash,
                    "private_audit_format": PRIVATE_AUDIT_FORMAT,
                }
                write_json(final / "result.private.json", result, private=True)
                return result
            previous_review = review.private_review.model_dump(mode="json")
            write_json(self.output / "runs.private" / b_key / "review.private.json", review_raw, private=True)
        result = {"status": "REVIEW_ATTEMPTS_EXHAUSTED", "attempt": self.config.max_review_attempts}
        write_json(self.output / "final" / "result.private.json", result, private=True)
        return result
