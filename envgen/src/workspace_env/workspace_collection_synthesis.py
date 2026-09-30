"""Real Codex A/B construction of task-independent workspace collection maps.

Unlike the legacy task-input prototype, this module is deliberately a pure
function of an immutable workspace snapshot and its deterministic catalog.
No task metadata, prompt, data manifest, rubric, or reference answer is an
input to either Codex role or the public result.
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

from pydantic import Field, model_validator

from .collection_map import (
    WORKSPACE_COLLECTION_PRIVATE_AUDIT_FORMAT,
    CollectionMapError,
    WorkspaceCatalog,
    WorkspaceCollectionPrivateAudit,
    WorkspaceCollectionSet,
    build_workspace_catalog,
    load_json_model,
    sha256_file,
    validate_workspace_collection_set,
    write_json,
)
from .integration import workspace_snapshot_hash
from .manifest import StrictModel


WORKSPACE_COLLECTION_SYNTHESIS_VERSION = "codex-workspace-collection-synthesis-v2"
CodexRunner = Callable[..., dict[str, Any]]


class WorkspaceCollectionSynthesisError(RuntimeError):
    pass


class WorkspaceCollectionSynthesisConfig(StrictModel):
    schema_version: Literal[1] = 1
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
    max_review_attempts: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def validate_provider(self) -> "WorkspaceCollectionSynthesisConfig":
        if self.auth_mode == "api" and not self.base_url:
            raise ValueError("api auth_mode requires base_url; the key comes from the environment")
        if self.auth_mode == "chatgpt" and self.base_url is not None:
            raise ValueError("chatgpt auth_mode must not set base_url")
        return self


class WorkspacePrivateReview(StrictModel):
    verdict: Literal["PASS", "REVISE"]
    issues: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_review(self) -> "WorkspacePrivateReview":
        if self.verdict == "PASS" and self.issues:
            raise ValueError("PASS review must not include issues")
        if self.verdict == "REVISE" and not self.issues:
            raise ValueError("REVISE review must include at least one issue")
        return self


class WorkspaceReviewEnvelope(StrictModel):
    agent_visible: WorkspaceCollectionSet
    private_review: WorkspacePrivateReview


def _json_schema(model: type[StrictModel]) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2)


def render_workspace_codex_a_prompt(*, workspace_snapshot_hash: str, repair: bool) -> str:
    repair_block = (
        "这是修订轮。先读取 `input/reviewer-feedback.json`，只解决其中指出的问题，然后重写完整输出。"
        if repair
        else "这是首轮构建。"
    )
    return f"""你是 Codex A，负责为一个不可变工作区快照构建通用的文件集合导航。

{repair_block}

你可以读取：
- `WORKSPACE_ROOT/`：完整只读工作区。
- `CATALOG_PATH`：当前快照的确定性目录/文件事实目录。

必须实际使用 Codex 和 shell 检查文件。集合应表达工作区中可观察的版本、归档、模板、近似副本、目录边界或可验证引用关系；
不要把仅名称相似的材料混为一组，也不要把推测说成确定关系。

输出要求：写入 `output/workspace-collection-set.json`，内容必须严格符合下面的 JSON Schema。不要在最终回复中粘贴 JSON。

```json
{_json_schema(WorkspaceCollectionSet)}
```

构建规则：
1. `workspace_snapshot_hash` 必须为 `{workspace_snapshot_hash}`。
2. 所有路径必须相对 `WORKSPACE_ROOT/`，且 cards 按 `card_id` 字典序、members 按 `path` 字典序排列。
3. `role` 只描述可观察的工作区语境，例如 current/archive/template/copy/source candidate 或 related。
4. `possible_relations` 只能使用可观察依据，并且两个端点必须属于同一张 card；没有直接依据时应省略关系。
5. 标题、member note、boundaries 和 relation basis 只写工作区可观察事实。
6. 文字保持短、具体、可行动；集合应帮助后续探索者避免混用归档、模板或范围不同的材料。

完成前用 Python 校验 JSON、排序和关系端点。
"""


def render_workspace_codex_b_prompt(*, workspace_snapshot_hash: str) -> str:
    return f"""你是 Codex B，独立审核一个不可变工作区快照的通用文件集合导航。

你可以读取：
- `WORKSPACE_ROOT/`：完整只读工作区。
- `CATALOG_PATH`：当前快照的确定性目录/文件事实目录。
- `input/candidate.json`：Codex A 的候选输出。

快照 hash 应为 `{workspace_snapshot_hash}`。必须亲自用 shell 检查候选涉及的文件、候选 JSON 和必要的目录/内容证据。
审核成员是否存在、边界是否有可观察依据、相似或归档材料是否被错误混用、以及任何 `possible_*` 关系是否过度推断。

写入 `output/review.json`，严格符合下面的 JSON Schema。`agent_visible` 必须逐字保留候选内容，不能替 A 改写或补造内容。

```json
{_json_schema(WorkspaceReviewEnvelope)}
```

当且仅当候选完整符合 schema 且上述审核均通过时使用 `private_review.verdict="PASS"`、空 issues；否则使用 `REVISE`，
并给出完整、具体、可执行的 issues。不要在最终回复中粘贴 JSON。
"""


def _readonly_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WorkspaceCollectionSynthesisError("role input tree contains an unsafe file")
            os.chmod(path, 0o444)
        for name in directories:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceCollectionSynthesisError("role input tree contains an unsafe directory")
            os.chmod(path, 0o555)
    os.chmod(root, 0o555)


class WorkspaceCollectionSynthesisOrchestrator:
    def __init__(self, *, config: WorkspaceCollectionSynthesisConfig, codex_runner: CodexRunner) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self.workspace = Path(config.workspace_root).resolve(strict=True)
        if not self.workspace.is_dir():
            raise WorkspaceCollectionSynthesisError("workspace_root must be a directory")
        self.output = Path(config.output_root).resolve()
        self.snapshot_hash = workspace_snapshot_hash(str(self.workspace))
        self.catalog, self.catalog_path = build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=self.snapshot_hash,
            catalog_root=config.catalog_root,
        )

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
            raise WorkspaceCollectionSynthesisError("output_root must not already exist")
        try:
            self.output.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise WorkspaceCollectionSynthesisError("output_root must be outside workspace_root")
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
            {**stored, "orchestrator_version": WORKSPACE_COLLECTION_SYNTHESIS_VERSION, "workspace_snapshot_hash": self.snapshot_hash},
            private=True,
        )
        shutil.copyfile(self.catalog_path, self.output / "workspace-catalog.private.json")
        os.chmod(self.output / "workspace-catalog.private.json", 0o600)

    def _prepare_role_root(self, *, role_key: str) -> Path:
        root = self.output / "runs.private" / role_key / "workdir"
        root.mkdir(parents=True, mode=0o700)
        workspace = root / "WORKSPACE_ROOT"
        shutil.copytree(self.workspace, workspace, symlinks=False)
        _readonly_tree(workspace)
        shutil.copyfile(self.catalog_path, root / "CATALOG_PATH")
        os.chmod(root / "CATALOG_PATH", 0o444)
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
            raise WorkspaceCollectionSynthesisError("Codex role modified its read-only workspace view")
        if result.get("status") != "ok":
            raise WorkspaceCollectionSynthesisError(f"{role_key} failed: {result.get('errorMessage') or result.get('status')}")
        trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
        if collection.get("complete") is not True:
            raise WorkspaceCollectionSynthesisError(f"{role_key} did not produce a complete Codex trace")
        return result

    @staticmethod
    def _load_collection(path: Path) -> WorkspaceCollectionSet:
        loaded = load_json_model(path, WorkspaceCollectionSet)
        assert isinstance(loaded, WorkspaceCollectionSet)
        return loaded

    def run(self) -> dict[str, Any]:
        if os.environ.get("CODEX_SANDBOX_MODE") != "danger-full-access":
            raise WorkspaceCollectionSynthesisError("CODEX_SANDBOX_MODE must be danger-full-access for native-shell A/B construction")
        self._initialise_output()
        previous_review: dict[str, Any] | None = None
        for attempt in range(1, self.config.max_review_attempts + 1):
            a_key = f"attempt-{attempt:02d}/codex-a"
            a_root = self._prepare_role_root(role_key=a_key)
            if previous_review is not None:
                write_json(a_root / "input" / "reviewer-feedback.json", previous_review, private=True)
            self._run_role(
                role_key=a_key,
                prompt=render_workspace_codex_a_prompt(workspace_snapshot_hash=self.snapshot_hash, repair=attempt > 1),
            )
            candidate_path = a_root / "output" / "workspace-collection-set.json"
            try:
                candidate = self._load_collection(candidate_path)
                validate_workspace_collection_set(
                    candidate,
                    catalog=self.catalog,
                    expected_workspace_snapshot_hash=self.snapshot_hash,
                )
                mechanical: dict[str, Any] = {"valid": True, "errors": []}
            except (CollectionMapError, ValueError) as exc:
                candidate = None
                mechanical = {"valid": False, "errors": [str(exc)]}
            write_json(self.output / "runs.private" / a_key / "mechanical-validation.private.json", mechanical, private=True)
            if candidate is None:
                previous_review = {"verdict": "REVISE", "issues": ["机械校验未通过：" + error for error in mechanical["errors"]]}
                continue

            b_key = f"attempt-{attempt:02d}/codex-b"
            b_root = self._prepare_role_root(role_key=b_key)
            shutil.copyfile(candidate_path, b_root / "input" / "candidate.json")
            write_json(b_root / "input" / "mechanical-validation.json", mechanical, private=True)
            self._run_role(
                role_key=b_key,
                prompt=render_workspace_codex_b_prompt(workspace_snapshot_hash=self.snapshot_hash),
            )
            review_path = b_root / "output" / "review.json"
            try:
                review = WorkspaceReviewEnvelope.model_validate(json.loads(review_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise WorkspaceCollectionSynthesisError("Codex B did not produce a valid review envelope") from exc
            if review.agent_visible != candidate:
                raise WorkspaceCollectionSynthesisError("Codex B changed the agent-visible candidate instead of reviewing it")
            if review.private_review.verdict != "PASS":
                previous_review = review.private_review.model_dump(mode="json")
                write_json(self.output / "runs.private" / b_key / "review.private.json", review.model_dump(mode="json"), private=True)
                continue

            final = self.output / "final"
            final.mkdir(mode=0o700)
            visible_path = final / "workspace-collection-set.public.json"
            visible_hash = write_json(visible_path, candidate.model_dump(mode="json"), private=False)
            a_result_path = self.output / "runs.private" / a_key / "codex-result.private.json"
            b_result_path = self.output / "runs.private" / b_key / "codex-result.private.json"
            audit = WorkspaceCollectionPrivateAudit(
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                workspace_snapshot_hash=self.snapshot_hash,
                workspace_catalog_sha256=sha256_file(self.output / "workspace-catalog.private.json"),
                agent_visible_collection_set_sha256=visible_hash,
                codex_a_artifact_sha256=sha256_file(a_result_path),
                codex_b_artifact_sha256=sha256_file(b_result_path),
            )
            write_json(final / "workspace-collection-map.private.json", audit.model_dump(mode="json"), private=True)
            result = {
                "status": "PASS",
                "attempt": attempt,
                "workspace_collection_set_path": str(visible_path),
                "workspace_collection_set_sha256": visible_hash,
                "private_audit_format": WORKSPACE_COLLECTION_PRIVATE_AUDIT_FORMAT,
            }
            write_json(final / "result.private.json", result, private=True)
            return result
        result = {"status": "REVIEW_ATTEMPTS_EXHAUSTED", "attempt": self.config.max_review_attempts}
        final = self.output / "final"
        final.mkdir(mode=0o700)
        write_json(final / "result.private.json", result, private=True)
        return result
