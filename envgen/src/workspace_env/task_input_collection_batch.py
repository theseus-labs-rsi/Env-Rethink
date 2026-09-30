"""Batch construction and persona-union compilation for v1 collection maps.

The v1 task-input map remains an explicitly targeted intervention.  This
module never invents cards: it invokes the existing real Codex A/B generator
for every selected task, then compiles only reviewed public cards into a
persona-wide read-only search index.  Task/card provenance stays in a private
sidecar and is deliberately absent from the SQLite documents table.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from .collection_map import (
    COLLECTION_MAP_MANIFEST_FORMAT,
    COLLECTION_SET_FORMAT,
    PERSONA_COLLECTION_INDEX_PUBLIC_FORMAT,
    PERSONA_COLLECTION_SEARCH_INDEX_FORMAT,
    PRIVATE_AUDIT_FORMAT,
    CollectionMapError,
    CollectionMapManifest,
    CollectionSet,
    PersonaCollectionIndexPublic,
    PersonaIndexDocument,
    PrivateCollectionAudit,
    _fts_searchable_text,
    build_workspace_catalog,
    load_json_model,
    sha256_file,
    visible_collection_text,
    write_json,
)
from .collection_synthesis import (
    COLLECTION_SYNTHESIS_VERSION,
    CollectionSynthesisConfig,
    CollectionSynthesisError,
    CollectionSynthesisOrchestrator,
)
from .integration import workspace_snapshot_hash
from .manifest import StrictModel, canonical_json


TASK_INPUT_COLLECTION_BATCH_FORMAT = "workspace-bench.task-input-collection-map-batch.v1"
PERSONA_COLLECTION_INDEX_PRIVATE_FORMAT = "workspace-bench.persona-collection-index.private.v1"
TASK_INPUT_COLLECTION_BATCH_VERSION = "codex-task-input-collection-batch-v1"

CodexRunner = Callable[..., dict[str, Any]]


class TaskInputCollectionBatchError(RuntimeError):
    """Raised when a batch cannot produce a complete, auditable map package."""


class TaskInputCollectionBatchConfig(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    persona: str = Field(min_length=1, max_length=240)
    task_root: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    catalog_root: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=240)
    auth_mode: Literal["chatgpt", "api"] = "chatgpt"
    base_url: str | None = None
    expected_codex_version: Literal["0.144.5"] = "0.144.5"
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    timeout_seconds: float = Field(default=900.0, gt=0)
    max_review_attempts: int = Field(default=5, ge=1, le=10)
    max_parallel_tasks: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def validate_provider(self) -> "TaskInputCollectionBatchConfig":
        if self.auth_mode == "api" and not self.base_url:
            raise ValueError("api auth_mode requires base_url; the key comes from the environment")
        if self.auth_mode == "chatgpt" and self.base_url is not None:
            raise ValueError("chatgpt auth_mode must not set base_url")
        return self


class BatchTaskResult(StrictModel):
    task_id: str = Field(min_length=1, max_length=200)
    status: Literal["PASS", "REVIEW_ATTEMPTS_EXHAUSTED", "ERROR"]
    collection_set_path: str | None = None
    collection_set_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    detail: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_pass_shape(self) -> "BatchTaskResult":
        if self.status == "PASS":
            if self.collection_set_path is None or self.collection_set_sha256 is None:
                raise ValueError("PASS result requires a collection-set path and hash")
        elif self.collection_set_path is not None or self.collection_set_sha256 is not None:
            raise ValueError("non-PASS result must not route a collection set")
        return self


class BatchPrivateManifest(StrictModel):
    format: Literal[TASK_INPUT_COLLECTION_BATCH_FORMAT] = TASK_INPUT_COLLECTION_BATCH_FORMAT
    created_at: str = Field(min_length=1, max_length=64)
    completed_at: str | None = Field(default=None, min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=128)
    persona: str = Field(min_length=1, max_length=240)
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_input_conditioned: Literal[True] = True
    orchestrator_version: Literal[TASK_INPUT_COLLECTION_BATCH_VERSION] = TASK_INPUT_COLLECTION_BATCH_VERSION
    collection_synthesis_version: Literal[COLLECTION_SYNTHESIS_VERSION] = COLLECTION_SYNTHESIS_VERSION
    task_results: list[BatchTaskResult]
    coverage: float = Field(ge=0.0, le=1.0)
    routing_manifest_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    persona_index_public_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    persona_index_sqlite_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_results(self) -> "BatchPrivateManifest":
        task_ids = [entry.task_id for entry in self.task_results]
        if task_ids != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
            raise ValueError("batch task results must be uniquely sorted by task_id")
        return self


class PersonaIndexProvenance(StrictModel):
    document_id: str = Field(pattern=r"^doc-[0-9a-f]{64}$")
    source_task_id: str = Field(min_length=1, max_length=200)
    source_card_id: str = Field(min_length=1, max_length=64)
    source_map_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PersonaCollectionIndexPrivateAudit(StrictModel):
    format: Literal[PERSONA_COLLECTION_INDEX_PRIVATE_FORMAT] = PERSONA_COLLECTION_INDEX_PRIVATE_FORMAT
    created_at: str = Field(min_length=1, max_length=64)
    persona: str = Field(min_length=1, max_length=240)
    task_input_conditioned: Literal[True] = True
    workspace_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    routing_manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    public_projection_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sqlite_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provenance: list[PersonaIndexProvenance] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_provenance(self) -> "PersonaCollectionIndexPrivateAudit":
        sort_key = lambda item: (item.document_id, item.source_task_id, item.source_card_id, item.source_map_sha256)
        if self.provenance != sorted(self.provenance, key=sort_key):
            raise ValueError("persona index provenance must be deterministically sorted")
        return self


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_regular_file(path: Path) -> None:
    info = path.lstat()
    if not path.is_file() or path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise TaskInputCollectionBatchError("collection batch artefact must be a regular file")


def _atomic_replace(path: Path, write: Callable[[Path], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        write(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _task_metadata_paths(task_root: Path, *, persona: str) -> list[tuple[str, Path]]:
    selected: list[tuple[str, Path]] = []
    for metadata in task_root.glob("*/metadata.json"):
        try:
            raw = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskInputCollectionBatchError(f"task metadata is invalid: {metadata}") from exc
        if isinstance(raw, dict) and raw.get("persona") == persona:
            selected.append((metadata.parent.name, metadata.resolve()))
    selected.sort(key=lambda item: item[0])
    if not selected:
        raise TaskInputCollectionBatchError("persona selection has no tasks")
    if len({task_id for task_id, _ in selected}) != len(selected):
        raise TaskInputCollectionBatchError("persona selection has duplicate task IDs")
    return selected


def _load_reviewed_source(
    *,
    task_id: str,
    collection_set_path: Path,
    expected_snapshot_hash: str,
) -> tuple[CollectionSet, str]:
    """Verify a final v1 map and return it with its exact on-disk hash."""

    _safe_regular_file(collection_set_path)
    final = collection_set_path.parent
    audit_path = final / "collection-map.private.json"
    result_path = final / "result.private.json"
    for candidate in (audit_path, result_path):
        _safe_regular_file(candidate)
    try:
        collection = load_json_model(collection_set_path, CollectionSet)
        audit = load_json_model(audit_path, PrivateCollectionAudit)
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (CollectionMapError, OSError, json.JSONDecodeError) as exc:
        raise TaskInputCollectionBatchError(f"task {task_id} has an invalid reviewed map artefact") from exc
    assert isinstance(collection, CollectionSet)
    assert isinstance(audit, PrivateCollectionAudit)
    actual_hash = sha256_file(collection_set_path)
    if collection.format != COLLECTION_SET_FORMAT:
        raise TaskInputCollectionBatchError(f"task {task_id} map format is not v1")
    if audit.format != PRIVATE_AUDIT_FORMAT or audit.review_verdict != "PASS":
        raise TaskInputCollectionBatchError(f"task {task_id} map has not passed Codex B review")
    if audit.workspace_snapshot_hash != expected_snapshot_hash:
        raise TaskInputCollectionBatchError(f"task {task_id} map snapshot differs from its persona batch")
    if audit.agent_visible_collection_set_sha256 != actual_hash:
        raise TaskInputCollectionBatchError(f"task {task_id} public map hash differs from its private audit")
    if not isinstance(result, dict) or result.get("status") != "PASS":
        raise TaskInputCollectionBatchError(f"task {task_id} final result is not PASS")
    if result.get("collection_set_sha256") != actual_hash:
        raise TaskInputCollectionBatchError(f"task {task_id} final result hash differs from its public map")
    return collection, actual_hash


def compile_persona_collection_index(
    *,
    persona: str,
    workspace_snapshot_hash: str,
    routing_manifest_path: str | Path,
    batch_root: str | Path,
    output_root: str | Path,
) -> tuple[str, str]:
    """Compile reviewed v1 maps into a provenance-free persona FTS index.

    The return values are the public projection and SQLite hashes.  Source
    task/card identities are retained solely in the private audit JSON.
    """

    routing_path = Path(routing_manifest_path).resolve(strict=True)
    _safe_regular_file(routing_path)
    try:
        routing = load_json_model(routing_path, CollectionMapManifest)
    except CollectionMapError as exc:
        raise TaskInputCollectionBatchError("persona routing manifest is invalid") from exc
    assert isinstance(routing, CollectionMapManifest)
    batch = Path(batch_root).resolve(strict=True)
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)

    documents: dict[str, PersonaIndexDocument] = {}
    provenance: list[PersonaIndexProvenance] = []
    for entry in routing.entries:
        raw_source = Path(entry.collection_set_path)
        _safe_regular_file(raw_source)
        source_path = raw_source.resolve(strict=True)
        expected_source = (batch / "tasks" / entry.task_id / "final" / "collection-set.public.json").resolve(strict=False)
        if source_path != expected_source:
            raise TaskInputCollectionBatchError("persona routing source is outside its batch task final directory")
        collection, source_hash = _load_reviewed_source(
            task_id=entry.task_id,
            collection_set_path=source_path,
            expected_snapshot_hash=workspace_snapshot_hash,
        )
        card_by_id = {card.card_id: card for card in collection.cards}
        for card_id in collection.exploration_order:
            card = card_by_id[card_id]
            text = visible_collection_text(collection, card_ids=[card.card_id])
            public_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            document_id = "doc-" + public_hash.removeprefix("sha256:")
            documents.setdefault(
                document_id,
                PersonaIndexDocument(
                    document_id=document_id,
                    public_content_sha256=public_hash,
                    public_content=text,
                ),
            )
            provenance.append(
                PersonaIndexProvenance(
                    document_id=document_id,
                    source_task_id=entry.task_id,
                    source_card_id=card.card_id,
                    source_map_sha256=source_hash,
                )
            )
    if not documents:
        raise TaskInputCollectionBatchError("persona routing manifest contains no collection cards")

    public = PersonaCollectionIndexPublic(
        workspace_snapshot_hash=workspace_snapshot_hash,
        documents=[documents[key] for key in sorted(documents)],
    )
    public_path = output / "persona-collection-index.public.json"
    public_hash = write_json(public_path, public.model_dump(mode="json"), private=False)
    index_path = output / "persona-collection-index.sqlite"

    def write_index(temporary: Path) -> None:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE documents (document_id TEXT PRIMARY KEY, public_content_sha256 TEXT NOT NULL, public_content TEXT NOT NULL, sort_key TEXT NOT NULL)"
            )
            connection.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(document_id UNINDEXED, content)")
            for document in public.documents:
                connection.execute(
                    "INSERT INTO documents (document_id, public_content_sha256, public_content, sort_key) VALUES (?, ?, ?, ?)",
                    (document.document_id, document.public_content_sha256, document.public_content, document.document_id),
                )
                connection.execute(
                    "INSERT INTO documents_fts (document_id, content) VALUES (?, ?)",
                    (document.document_id, _fts_searchable_text(document.public_content)),
                )
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                [
                    ("format", PERSONA_COLLECTION_SEARCH_INDEX_FORMAT),
                    ("public_projection_sha256", public_hash),
                    ("workspace_snapshot_hash", workspace_snapshot_hash),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    _atomic_replace(index_path, write_index)
    sqlite_hash = sha256_file(index_path)
    provenance.sort(key=lambda item: (item.document_id, item.source_task_id, item.source_card_id, item.source_map_sha256))
    private = PersonaCollectionIndexPrivateAudit(
        created_at=_utc_now(),
        persona=persona,
        workspace_snapshot_hash=workspace_snapshot_hash,
        routing_manifest_sha256=sha256_file(routing_path),
        public_projection_sha256=public_hash,
        sqlite_sha256=sqlite_hash,
        provenance=provenance,
    )
    write_json(output / "persona-collection-index.private.json", private.model_dump(mode="json"), private=True)
    return public_hash, sqlite_hash


class TaskInputCollectionBatchOrchestrator:
    """Run every selected task, then compile only a complete reviewed batch."""

    def __init__(self, *, config: TaskInputCollectionBatchConfig, codex_runner: CodexRunner) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self.task_root = Path(config.task_root).resolve(strict=True)
        self.workspace = Path(config.workspace_root).resolve(strict=True)
        self.output = Path(config.output_root).resolve()
        if not self.task_root.is_dir() or not self.workspace.is_dir():
            raise TaskInputCollectionBatchError("task_root and workspace_root must be directories")
        self.selected = _task_metadata_paths(self.task_root, persona=config.persona)
        self.snapshot_hash = workspace_snapshot_hash(str(self.workspace))

    def _initialise_output(self) -> None:
        if self.output.exists():
            info = self.output.lstat()
            if self.output.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise TaskInputCollectionBatchError("existing output_root must be a non-symlink directory")
            # A complete batch manifest makes the directory immutable:
            # resuming it could silently rewrite provenance after the persona
            # index was handed off.  An INCOMPLETE manifest records a failed
            # construction attempt, not a deliverable index; preserve it in a
            # private superseded archive before retrying only its missing work.
            manifest_path = self.output / "batch-manifest.private.json"
            if manifest_path.exists():
                _safe_regular_file(manifest_path)
                try:
                    prior = load_json_model(manifest_path, BatchPrivateManifest)
                except CollectionMapError as exc:
                    raise TaskInputCollectionBatchError("existing batch manifest is invalid") from exc
                assert isinstance(prior, BatchPrivateManifest)
                if prior.coverage == 1.0:
                    raise TaskInputCollectionBatchError("output_root already contains a finalized batch manifest")
                archive_root = self.output / "superseded-batch-manifests.private"
                archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(archive_root, 0o700)
                stamp = _utc_now().replace(":", "").replace("+", "_").replace("-", "")
                archived_manifest = archive_root / f"{stamp}-batch-manifest.private.json"
                os.replace(manifest_path, archived_manifest)
                os.chmod(archived_manifest, 0o600)
        try:
            self.output.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise TaskInputCollectionBatchError("output_root must be outside workspace_root")
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.output, 0o700)
        build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=self.snapshot_hash,
            catalog_root=self.config.catalog_root,
        )

    def _recover_completed_task(self, task_id: str) -> BatchTaskResult | None:
        """Return a verified checkpoint, without trusting an unfinished run.

        The only reusable state is the same reviewed final triplet consumed by
        the persona compiler.  This lets an operator raise worker concurrency
        after an interruption without rerunning already audited tasks.
        """

        task_output = self.output / "tasks" / task_id
        if not task_output.exists():
            return None
        info = task_output.lstat()
        if task_output.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise TaskInputCollectionBatchError(f"task {task_id} checkpoint is not a safe directory")
        final = task_output / "final"
        if not final.exists():
            return None
        final_info = final.lstat()
        if final.is_symlink() or not stat.S_ISDIR(final_info.st_mode):
            raise TaskInputCollectionBatchError(f"task {task_id} final checkpoint is not a safe directory")
        required = (
            final / "collection-set.public.json",
            final / "collection-map.private.json",
            final / "result.private.json",
        )
        if not all(path.exists() for path in required):
            return None
        collection_path = required[0]
        _collection, digest = _load_reviewed_source(
            task_id=task_id,
            collection_set_path=collection_path,
            expected_snapshot_hash=self.snapshot_hash,
        )
        return BatchTaskResult(
            task_id=task_id,
            status="PASS",
            collection_set_path=str(collection_path),
            collection_set_sha256=digest,
        )

    def _archive_incomplete_task(self, task_id: str) -> None:
        """Move partial private runtime state aside before a clean retry."""

        task_output = self.output / "tasks" / task_id
        if not task_output.exists():
            return
        info = task_output.lstat()
        if task_output.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise TaskInputCollectionBatchError(f"task {task_id} checkpoint is not a safe directory")
        archive_root = self.output / "interrupted-runs.private"
        archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(archive_root, 0o700)
        stem = _utc_now().replace(":", "").replace("+", "_").replace("-", "") + f"-{task_id}"
        destination = archive_root / stem
        suffix = 1
        while destination.exists():
            destination = archive_root / f"{stem}-{suffix}"
            suffix += 1
        os.replace(task_output, destination)
        os.chmod(destination, 0o700)

    def _task_config(self, task_id: str, metadata_path: Path) -> CollectionSynthesisConfig:
        return CollectionSynthesisConfig(
            run_id=f"{self.config.run_id}-{task_id}",
            task_metadata_path=str(metadata_path),
            workspace_root=str(self.workspace),
            catalog_root=self.config.catalog_root,
            output_root=str(self.output / "tasks" / task_id),
            model=self.config.model,
            auth_mode=self.config.auth_mode,
            base_url=self.config.base_url,
            expected_codex_version=self.config.expected_codex_version,
            reasoning_effort=self.config.reasoning_effort,
            timeout_seconds=self.config.timeout_seconds,
            max_review_attempts=self.config.max_review_attempts,
        )

    def _run_task(self, task_id: str, metadata_path: Path) -> BatchTaskResult:
        try:
            result = CollectionSynthesisOrchestrator(
                config=self._task_config(task_id, metadata_path),
                codex_runner=self.codex_runner,
            ).run()
        except (CollectionMapError, CollectionSynthesisError, OSError, ValueError) as exc:
            return BatchTaskResult(task_id=task_id, status="ERROR", detail=str(exc))
        if result.get("status") == "PASS":
            path = result.get("collection_set_path")
            digest = result.get("collection_set_sha256")
            if isinstance(path, str) and isinstance(digest, str):
                return BatchTaskResult(
                    task_id=task_id,
                    status="PASS",
                    collection_set_path=path,
                    collection_set_sha256=digest,
                )
        if result.get("status") == "REVIEW_ATTEMPTS_EXHAUSTED":
            return BatchTaskResult(task_id=task_id, status="REVIEW_ATTEMPTS_EXHAUSTED")
        return BatchTaskResult(task_id=task_id, status="ERROR", detail="unexpected single-task synthesis result")

    def run(self) -> dict[str, Any]:
        if os.environ.get("CODEX_SANDBOX_MODE") != "danger-full-access":
            raise TaskInputCollectionBatchError("CODEX_SANDBOX_MODE must be danger-full-access for native-shell A/B construction")
        self._initialise_output()
        results: list[BatchTaskResult] = []
        pending: list[tuple[str, Path]] = []
        for task_id, metadata_path in self.selected:
            checkpoint = self._recover_completed_task(task_id)
            if checkpoint is not None:
                results.append(checkpoint)
                continue
            self._archive_incomplete_task(task_id)
            pending.append((task_id, metadata_path))
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_parallel_tasks) as executor:
            futures = {
                executor.submit(self._run_task, task_id, metadata_path): task_id
                for task_id, metadata_path in pending
            }
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item.task_id)
        passed = [item for item in results if item.status == "PASS"]
        coverage = len(passed) / len(results)
        routing_hash: str | None = None
        public_hash: str | None = None
        sqlite_hash: str | None = None
        if len(passed) == len(results):
            routing = CollectionMapManifest(
                entries=[
                    {"task_id": item.task_id, "collection_set_path": item.collection_set_path}
                    for item in passed
                ]
            )
            routing_path = self.output / "routing.private.json"
            routing_hash = write_json(routing_path, routing.model_dump(mode="json"), private=True)
            public_hash, sqlite_hash = compile_persona_collection_index(
                persona=self.config.persona,
                workspace_snapshot_hash=self.snapshot_hash,
                routing_manifest_path=routing_path,
                batch_root=self.output,
                output_root=self.output / "persona-index",
            )
        manifest = BatchPrivateManifest(
            created_at=_utc_now(),
            completed_at=_utc_now(),
            run_id=self.config.run_id,
            persona=self.config.persona,
            workspace_snapshot_hash=self.snapshot_hash,
            task_results=results,
            coverage=coverage,
            routing_manifest_sha256=routing_hash,
            persona_index_public_sha256=public_hash,
            persona_index_sqlite_sha256=sqlite_hash,
        )
        write_json(self.output / "batch-manifest.private.json", manifest.model_dump(mode="json"), private=True)
        return {
            "status": "PASS" if coverage == 1.0 else "INCOMPLETE",
            "task_count": len(results),
            "passed_task_count": len(passed),
            "coverage": coverage,
            "routing_manifest_path": str(self.output / "routing.private.json") if routing_hash else None,
            "persona_index_root": str(self.output / "persona-index") if public_hash else None,
            "persona_index_public_sha256": public_hash,
            "persona_index_sqlite_sha256": sqlite_hash,
        }
