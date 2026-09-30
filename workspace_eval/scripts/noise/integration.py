"""Deterministic integration and validation for multi-agent noise artifacts.

The module intentionally uses only the Python standard library.  It accepts a
main plan, one ``worker_result.json`` per planned job, and a read-only subset
root.  Worker artifacts are copied into a task directory, then
``metadata.json`` is updated so canonical replacements are moves rather than
additional copies.

Main plan schema (schema_version 1)::

    {
      "schema_version": 1,
      "task_id": "373",
      "jobs": [
        {
          "job_id": "file_001",
          "source_stored_relpath": "data/source.csv",
          "subset_path": "桌面/物流/source.csv"
        }
      ]
    }

Worker result schema (schema_version 1)::

    {
      "schema_version": 1,
      "job_id": "file_001",
      "source_stored_relpath": "data/source.csv",
      "artifacts": [
        {
          "artifact_id": "candidate_v1",
          "file": "artifacts/source_v1.csv",
          "version_role": "distractor",
          "noise_kind": "stale_version",
          "target_path": "桌面/物流/source_v1.csv",
          "sha256": "<hex digest>"
        }
      ]
    }

Workers only produce noise.  The canonical standard input is never routed
through a worker directory: ``integrate`` reads it straight from ``task_dir``
using the plan's ``source_stored_relpath``, so the bytes an agent can write to
cannot become the published answer.  A ``canonical`` artifact from an older run
is ignored when ``allow_legacy_canonical`` is set.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from ._fs import (
        IntegrationError,
        json_sha256,
        require_sha256,
        resolve_inside,
        safe_id,
        safe_rel_path,
        sha256_file,
        write_json_atomic,
    )
except ImportError:
    from _fs import (
        IntegrationError,
        json_sha256,
        require_sha256,
        resolve_inside,
        safe_id,
        safe_rel_path,
        sha256_file,
        write_json_atomic,
    )


SCHEMA_VERSION = 1
GENERATOR = "multi_agent_noise_v1"
CHECKS_REL_PATH = Path("generation") / "deterministic_checks.json"
STORAGE_ROOT = "data/_generated_multi_agent"
ROLES = {"canonical", "distractor", "evidence"}


@dataclass(frozen=True)
class PreparedArtifact:
    job_id: str
    artifact_id: str
    version_role: str
    noise_kind: str | None
    source: Path
    source_relpath: str
    stored_relpath: str
    target_path: str
    filename: str
    sha256: str


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IntegrationError(f"{path} must contain a JSON object")
    return value


def _validate_header(value: dict[str, Any], *, label: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise IntegrationError(
            f"{label}.schema_version must be {SCHEMA_VERSION}"
        )


def discover_worker_results(workers_root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    results: dict[str, tuple[Path, dict[str, Any]]] = {}
    if not workers_root.is_dir():
        raise IntegrationError(f"workers root does not exist: {workers_root}")
    for result_path in sorted(workers_root.glob("*/worker_result.json")):
        result = read_json_object(result_path)
        _validate_header(result, label=str(result_path))
        job_id = safe_id(result.get("job_id"), field=f"{result_path}.job_id")
        if job_id in results:
            raise IntegrationError(f"duplicate worker result for job {job_id}")
        results[job_id] = (result_path.parent, result)
    return results


def _base_metadata(task_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Restore the pre-integration manifest when safely re-running integration."""

    checks_path = task_dir / CHECKS_REL_PATH
    if not checks_path.is_file():
        return metadata
    previous = read_json_object(checks_path)
    if previous.get("generator") != GENERATOR:
        return metadata
    base_state = previous.get("base_state")
    if not isinstance(base_state, dict):
        raise IntegrationError(f"{checks_path} has no valid base_state")
    manifest = base_state.get("data_manifest")
    remove_paths = base_state.get("input_remove_paths")
    if not isinstance(manifest, list) or not isinstance(remove_paths, list):
        raise IntegrationError(f"{checks_path} has an invalid base_state")
    restored = dict(metadata)
    restored["data_manifest"] = manifest
    if remove_paths:
        restored["input_remove_paths"] = remove_paths
    else:
        restored.pop("input_remove_paths", None)
    return restored


def _manifest_index(
    task_dir: Path,
    metadata: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    """Normalize the manifest and report which entries declared a target_path.

    An entry without ``target_path`` gets one synthesized from its filename.
    That is not a real workspace location, so callers deciding whether the
    canonical input moved must fall back to the subset builder's matched path.
    """

    manifest = metadata.get("data_manifest")
    if not isinstance(manifest, list):
        raise IntegrationError("metadata.data_manifest must be a list")
    normalized: list[dict[str, Any]] = []
    by_stored: dict[str, dict[str, Any]] = {}
    declared_targets: set[str] = set()
    targets: set[str] = set()
    for index, raw_entry in enumerate(manifest):
        if not isinstance(raw_entry, dict):
            raise IntegrationError(f"metadata.data_manifest[{index}] must be an object")
        entry = dict(raw_entry)
        stored = safe_rel_path(
            entry.get("stored_relpath"),
            field=f"metadata.data_manifest[{index}].stored_relpath",
        )
        declared_target = entry.get("target_path")
        target_value = declared_target or entry.get("filename")
        target = safe_rel_path(
            target_value,
            field=f"metadata.data_manifest[{index}].target_path",
        )
        source = resolve_inside(task_dir, stored, field="stored_relpath")
        if not source.is_file():
            raise IntegrationError(f"manifest source does not exist: {stored}")
        if stored in by_stored:
            raise IntegrationError(f"duplicate manifest stored_relpath: {stored}")
        if target in targets:
            raise IntegrationError(f"duplicate manifest target_path: {target}")
        entry["stored_relpath"] = stored
        entry["target_path"] = target
        entry.setdefault("filename", PurePosixPath(target).name)
        normalized.append(entry)
        by_stored[stored] = entry
        targets.add(target)
        if declared_target:
            declared_targets.add(stored)
    return normalized, by_stored, declared_targets


def _load_plan_jobs(plan: dict[str, Any]) -> list[dict[str, str]]:
    _validate_header(plan, label="main plan")
    jobs = plan.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise IntegrationError("main plan.jobs must be a non-empty list")
    normalized: list[dict[str, str]] = []
    job_ids: set[str] = set()
    sources: set[str] = set()
    for index, raw_job in enumerate(jobs):
        if not isinstance(raw_job, dict):
            raise IntegrationError(f"main plan.jobs[{index}] must be an object")
        job_id = safe_id(raw_job.get("job_id"), field=f"jobs[{index}].job_id")
        source = safe_rel_path(
            raw_job.get("source_stored_relpath"),
            field=f"jobs[{index}].source_stored_relpath",
        )
        subset = safe_rel_path(
            raw_job.get("subset_path"),
            field=f"jobs[{index}].subset_path",
        )
        if job_id in job_ids:
            raise IntegrationError(f"duplicate planned job_id: {job_id}")
        if source in sources:
            raise IntegrationError(f"canonical source assigned twice: {source}")
        job_ids.add(job_id)
        sources.add(source)
        normalized.append(
            {
                "job_id": job_id,
                "source_stored_relpath": source,
                "subset_path": subset,
            }
        )
    return sorted(normalized, key=lambda item: item["job_id"])


def _prepare_artifacts(
    *,
    job: dict[str, str],
    worker_dir: Path,
    result: dict[str, Any],
    allow_legacy_canonical: bool = False,
) -> tuple[list[PreparedArtifact], list[str]]:
    if result.get("source_stored_relpath") != job["source_stored_relpath"]:
        raise IntegrationError(
            f"{job['job_id']}: worker source_stored_relpath does not match plan"
        )
    raw_artifacts = result.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise IntegrationError(f"{job['job_id']}: artifacts must be a list")
    prepared: list[PreparedArtifact] = []
    ignored_canonical: list[str] = []
    artifact_ids: set[str] = set()
    for index, raw in enumerate(raw_artifacts):
        if not isinstance(raw, dict):
            raise IntegrationError(
                f"{job['job_id']}.artifacts[{index}] must be an object"
            )
        prefix = f"{job['job_id']}.artifacts[{index}]"
        role = raw.get("version_role")
        if role not in ROLES:
            raise IntegrationError(f"{prefix}.version_role must be one of {sorted(ROLES)}")
        if role == "canonical":
            # The canonical input is placed from task_dir, never from a worker
            # directory. Results predating that rule still carry one; drop it
            # rather than letting agent-written bytes reach the workspace.
            if not allow_legacy_canonical:
                raise IntegrationError(
                    f"{prefix}: workers must not produce canonical artifacts"
                )
            ignored_canonical.append(str(raw.get("file") or raw.get("path") or ""))
            continue
        artifact_id = safe_id(raw.get("artifact_id"), field=f"{prefix}.artifact_id")
        if artifact_id in artifact_ids:
            raise IntegrationError(
                f"{job['job_id']}: duplicate artifact_id {artifact_id}"
            )
        artifact_ids.add(artifact_id)
        source_relpath = safe_rel_path(raw.get("file"), field=f"{prefix}.file")
        source = resolve_inside(worker_dir, source_relpath, field=f"{prefix}.file")
        if not source.is_file():
            raise IntegrationError(f"{prefix}.file does not exist: {source_relpath}")
        expected_hash = require_sha256(raw.get("sha256"), field=f"{prefix}.sha256")
        actual_hash = sha256_file(source)
        if actual_hash != expected_hash:
            raise IntegrationError(
                f"{prefix}.sha256 mismatch: expected {expected_hash}, got {actual_hash}"
            )
        target = safe_rel_path(raw.get("target_path"), field=f"{prefix}.target_path")
        filename = raw.get("filename", PurePosixPath(target).name)
        filename = safe_rel_path(filename, field=f"{prefix}.filename")
        if "/" in filename:
            raise IntegrationError(f"{prefix}.filename must be a basename")
        noise_kind = raw.get("noise_kind")
        if noise_kind is not None:
            safe_id(noise_kind, field=f"{prefix}.noise_kind")
        stored_name = f"{artifact_id}_{PurePosixPath(source_relpath).name}"
        stored = PurePosixPath(STORAGE_ROOT, job["job_id"], stored_name).as_posix()
        prepared.append(
            PreparedArtifact(
                job_id=job["job_id"],
                artifact_id=artifact_id,
                version_role=role,
                noise_kind=noise_kind,
                source=source,
                source_relpath=source_relpath,
                stored_relpath=stored,
                target_path=target,
                filename=filename,
                sha256=actual_hash,
            )
        )
    return sorted(prepared, key=lambda item: item.artifact_id), ignored_canonical


def _canonical_artifact(
    *,
    job: dict[str, str],
    task_dir: Path,
) -> PreparedArtifact:
    """Build the canonical artifact from the original input in ``task_dir``.

    Reading the source here is what makes "标准答案不变" structural rather than
    something to verify: no agent-writable copy takes part, and the destination
    is the plan's ``subset_path`` rather than a model-chosen target.
    """

    source_relpath = job["source_stored_relpath"]
    source = resolve_inside(task_dir, source_relpath, field="source_stored_relpath")
    if not source.is_file():
        raise IntegrationError(
            f"{job['job_id']}: canonical source does not exist: {source_relpath}"
        )
    filename = PurePosixPath(job["subset_path"]).name
    stored_name = f"current_{PurePosixPath(source_relpath).name}"
    return PreparedArtifact(
        job_id=job["job_id"],
        artifact_id="current",
        version_role="canonical",
        noise_kind=None,
        source=source,
        source_relpath=source_relpath,
        stored_relpath=PurePosixPath(
            STORAGE_ROOT, job["job_id"], stored_name
        ).as_posix(),
        target_path=job["subset_path"],
        filename=filename,
        sha256=sha256_file(source),
    )


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def _integration_projection(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "data_manifest": metadata.get("data_manifest", []),
        "input_remove_paths": metadata.get("input_remove_paths", []),
    }


def integrate(
    *,
    task_dir: Path,
    plan_path: Path,
    workers_root: Path,
    subset_root: Path,
    allow_legacy_canonical: bool = False,
    min_distractors_per_job: int = 0,
) -> dict[str, Any]:
    """Integrate worker artifacts and return the deterministic checks object."""

    task_dir = task_dir.resolve()
    subset_root = subset_root.resolve()
    metadata_path = task_dir / "metadata.json"
    metadata = _base_metadata(task_dir, read_json_object(metadata_path))
    base_manifest, manifest_by_stored, declared_targets = _manifest_index(
        task_dir, metadata
    )
    plan = read_json_object(plan_path)
    jobs = _load_plan_jobs(plan)
    task_id = str(plan.get("task_id", ""))
    metadata_task_id = str(metadata.get("absolute_id", metadata.get("id", "")))
    if task_id and metadata_task_id and task_id != metadata_task_id:
        raise IntegrationError(
            f"plan task_id {task_id!r} does not match metadata task id "
            f"{metadata_task_id!r}"
        )
    workers = discover_worker_results(workers_root)
    planned_ids = {job["job_id"] for job in jobs}
    if set(workers) != planned_ids:
        missing = sorted(planned_ids - set(workers))
        extra = sorted(set(workers) - planned_ids)
        raise IntegrationError(
            f"worker results do not match plan; missing={missing}, extra={extra}"
        )

    prepared_by_job: dict[str, list[PreparedArtifact]] = {}
    canonical_by_job: dict[str, PreparedArtifact] = {}
    ignored_canonical_by_job: dict[str, list[str]] = {}
    replaced_sources: set[str] = set()
    old_targets: dict[str, str] = {}
    for job in jobs:
        source_stored = job["source_stored_relpath"]
        source_entry = manifest_by_stored.get(source_stored)
        if source_entry is None:
            raise IntegrationError(
                f"{job['job_id']}: source is not in metadata.data_manifest: "
                f"{source_stored}"
            )
        subset_file = resolve_inside(
            subset_root,
            job["subset_path"],
            field=f"{job['job_id']}.subset_path",
        )
        if not subset_file.is_file():
            raise IntegrationError(
                f"{job['job_id']}: subset file does not exist: {job['subset_path']}"
            )
        task_source = resolve_inside(task_dir, source_stored, field="source_stored_relpath")
        if sha256_file(subset_file) != sha256_file(task_source):
            raise IntegrationError(
                f"{job['job_id']}: subset file does not match canonical task source"
            )
        worker_dir, result = workers[job["job_id"]]
        noise, ignored = _prepare_artifacts(
            job=job,
            worker_dir=worker_dir,
            result=result,
            allow_legacy_canonical=allow_legacy_canonical,
        )
        canonical_by_job[job["job_id"]] = _canonical_artifact(
            job=job,
            task_dir=task_dir,
        )
        prepared_by_job[job["job_id"]] = sorted(
            [canonical_by_job[job["job_id"]], *noise],
            key=lambda item: item.artifact_id,
        )
        if ignored:
            ignored_canonical_by_job[job["job_id"]] = ignored
        replaced_sources.add(source_stored)
        # Only a target the task actually recorded counts as the input's prior
        # location. Otherwise _manifest_index synthesized one from the filename
        # and the subset builder's matched business path is the real place.
        old_targets[source_stored] = (
            str(source_entry["target_path"])
            if source_stored in declared_targets
            else job["subset_path"]
        )

    retained = [
        dict(entry)
        for entry in base_manifest
        if entry["stored_relpath"] not in replaced_sources
    ]
    occupied_targets = {entry["target_path"] for entry in retained}
    generated_entries: list[dict[str, Any]] = []
    artifact_records: list[dict[str, Any]] = []
    remove_paths = {
        safe_rel_path(path, field="metadata.input_remove_paths")
        for path in metadata.get("input_remove_paths", [])
    }
    canonical_mappings: list[dict[str, Any]] = []
    # Canonical destinations are fully determined by the plan, so they can be
    # collected before any artifact is placed. Distractor/evidence collisions
    # are then checked against every job's canonical target, including jobs
    # processed later in the loop below.
    canonical_targets: dict[str, str] = {}
    for job in jobs:
        canonical = canonical_by_job[job["job_id"]]
        existing_job = canonical_targets.get(canonical.target_path)
        if existing_job is not None:
            raise IntegrationError(
                "duplicate target_path after integration (canonical): "
                f"{canonical.target_path}; jobs={existing_job},{job['job_id']}"
            )
        canonical_targets[canonical.target_path] = job["job_id"]

    for job in jobs:
        artifacts = prepared_by_job[job["job_id"]]
        source_stored = job["source_stored_relpath"]
        canonical = canonical_by_job[job["job_id"]]
        old_target = old_targets[source_stored]
        # The canonical input is restored to the exact location it came from,
        # so it never moves and never contributes a removal. The comparison is
        # kept so validate_integrated_task still checks the flag for coherence.
        if canonical.target_path != old_target:
            remove_paths.add(old_target)
        canonical_mappings.append(
            {
                "job_id": job["job_id"],
                "source_stored_relpath": source_stored,
                "source_target_path": old_target,
                "canonical_stored_relpath": canonical.stored_relpath,
                "canonical_target_path": canonical.target_path,
                "moved": canonical.target_path != old_target,
            }
        )
        for artifact in artifacts:
            target_path = artifact.target_path
            if artifact.version_role == "canonical":
                if target_path in occupied_targets:
                    raise IntegrationError(
                        f"duplicate target_path after integration "
                        f"(canonical): "
                        f"{target_path}"
                    )
            elif (
                target_path in occupied_targets
                or target_path in canonical_targets
            ):
                original = PurePosixPath(target_path)
                parent = original.parent
                stem = original.stem
                suffix = original.suffix
                candidate_name = (
                    f"{stem}_{artifact.job_id}{suffix}"
                )
                candidate = (
                    PurePosixPath(parent, candidate_name).as_posix()
                    if parent != PurePosixPath(".")
                    else candidate_name
                )
                counter = 2
                while candidate in occupied_targets:
                    candidate_name = (
                        f"{stem}_{artifact.job_id}_{counter}{suffix}"
                    )
                    candidate = (
                        PurePosixPath(parent, candidate_name).as_posix()
                        if parent != PurePosixPath(".")
                        else candidate_name
                    )
                    counter += 1
                target_path = candidate
            occupied_targets.add(target_path)
            entry: dict[str, Any] = {
                "filename": artifact.filename,
                "stored_relpath": artifact.stored_relpath,
                "target_path": target_path,
                "generated_by": GENERATOR,
                "job_id": artifact.job_id,
                "artifact_id": artifact.artifact_id,
                "version_role": artifact.version_role,
            }
            if artifact.noise_kind:
                entry["noise_kind"] = artifact.noise_kind
            generated_entries.append(entry)
            artifact_records.append(
                {
                    "job_id": artifact.job_id,
                    "artifact_id": artifact.artifact_id,
                    "version_role": artifact.version_role,
                    "noise_kind": artifact.noise_kind,
                    "worker_file": artifact.source_relpath,
                    "stored_relpath": artifact.stored_relpath,
                    "target_path": target_path,
                    "sha256": artifact.sha256,
                    "size": artifact.source.stat().st_size,
                }
            )

    new_manifest = retained + generated_entries
    new_manifest.sort(
        key=lambda entry: (
            entry["target_path"],
            entry["stored_relpath"],
        )
    )
    # A path we are placing a file at cannot also be scheduled for removal. The
    # task may already carry input_remove_paths from an earlier generation whose
    # removed location is exactly where this run puts the canonical input, so the
    # occupied targets have to win. agent_runner skips such paths at runtime too,
    # but leaving them in would make the published metadata self-contradictory.
    final_targets = {entry["target_path"] for entry in new_manifest}
    remove_paths -= final_targets
    new_metadata = dict(metadata)
    new_metadata["data_manifest"] = new_manifest
    new_metadata["input_remove_paths"] = sorted(remove_paths)
    new_metadata["noise_integration"] = {
        "generator": GENERATOR,
        "schema_version": SCHEMA_VERSION,
        "checks": CHECKS_REL_PATH.as_posix(),
        "plan_sha256": sha256_file(plan_path),
    }

    storage_dir = task_dir / STORAGE_ROOT
    if storage_dir.exists():
        shutil.rmtree(storage_dir)
    for artifacts in prepared_by_job.values():
        for artifact in artifacts:
            destination = resolve_inside(
                task_dir,
                artifact.stored_relpath,
                field="artifact.stored_relpath",
            )
            _copy_atomic(artifact.source, destination)
    write_json_atomic(metadata_path, new_metadata)

    projection = _integration_projection(new_metadata)
    distractors_by_job = {
        job_id: sum(item.version_role == "distractor" for item in artifacts)
        for job_id, artifacts in prepared_by_job.items()
    }
    # A job whose worker produced no noise leaves the standard input alone in
    # the workspace. That is a generation failure, not a pass: without it the
    # task ships with nothing to disambiguate.
    noiseless_jobs = sorted(
        job_id
        for job_id, count in distractors_by_job.items()
        if count < min_distractors_per_job
    )
    errors = [
        f"{job_id}: produced {distractors_by_job[job_id]} distractor(s), "
        f"at least {min_distractors_per_job} required"
        for job_id in noiseless_jobs
    ]
    checks: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "task_id": task_id or metadata_task_id,
        "plan_sha256": sha256_file(plan_path),
        "integration_metadata_sha256": json_sha256(projection),
        "base_state": {
            "data_manifest": base_manifest,
            "input_remove_paths": sorted(
                {
                    safe_rel_path(path, field="metadata.input_remove_paths")
                    for path in metadata.get("input_remove_paths", [])
                }
            ),
        },
        "counts": {
            "jobs": len(jobs),
            "canonical": len(jobs),
            "distractors": sum(distractors_by_job.values()),
            "artifacts": len(artifact_records),
            "manifest_entries": len(new_manifest),
        },
        "distractors_by_job": distractors_by_job,
        "noiseless_jobs": noiseless_jobs,
        "ignored_legacy_canonical": {
            job_id: sorted(files)
            for job_id, files in sorted(ignored_canonical_by_job.items())
        },
        "canonical_mappings": canonical_mappings,
        "artifacts": sorted(
            artifact_records,
            key=lambda item: (item["job_id"], item["artifact_id"]),
        ),
        "input_remove_paths": sorted(remove_paths),
    }
    checks["checks_fingerprint"] = json_sha256(
        {key: value for key, value in checks.items() if key != "checks_fingerprint"}
    )
    write_json_atomic(task_dir / CHECKS_REL_PATH, checks)
    return checks


def validate_integrated_task(task_dir: Path) -> dict[str, Any]:
    """Validate an integrated task without requiring worker directories."""

    task_dir = task_dir.resolve()
    errors: list[str] = []
    checks_path = task_dir / CHECKS_REL_PATH
    try:
        checks = read_json_object(checks_path)
        metadata = read_json_object(task_dir / "metadata.json")
    except IntegrationError as exc:
        return {"status": "failed", "errors": [str(exc)]}

    if checks.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"checks schema_version must be {SCHEMA_VERSION}")
    if checks.get("generator") != GENERATOR:
        errors.append(f"checks generator must be {GENERATOR}")
    fingerprint = checks.get("checks_fingerprint")
    fingerprint_payload = {
        key: value for key, value in checks.items() if key != "checks_fingerprint"
    }
    if fingerprint != json_sha256(fingerprint_payload):
        errors.append("deterministic_checks.json fingerprint mismatch")

    projection = _integration_projection(metadata)
    if checks.get("integration_metadata_sha256") != json_sha256(projection):
        errors.append("metadata manifest/removal projection hash mismatch")

    manifest = metadata.get("data_manifest")
    if not isinstance(manifest, list):
        errors.append("metadata.data_manifest must be a list")
        manifest = []
    targets: set[str] = set()
    generated: dict[tuple[str, str], dict[str, Any]] = {}
    canonical_counts: dict[str, int] = {}
    for index, raw in enumerate(manifest):
        if not isinstance(raw, dict):
            errors.append(f"manifest item {index} is not an object")
            continue
        try:
            stored = safe_rel_path(
                raw.get("stored_relpath"),
                field=f"manifest[{index}].stored_relpath",
            )
            target = safe_rel_path(
                raw.get("target_path"),
                field=f"manifest[{index}].target_path",
            )
            source = resolve_inside(task_dir, stored, field="stored_relpath")
        except IntegrationError as exc:
            errors.append(str(exc))
            continue
        if not source.is_file():
            errors.append(f"manifest source does not exist: {stored}")
        if target in targets:
            errors.append(f"duplicate manifest target_path: {target}")
        targets.add(target)
        if raw.get("generated_by") == GENERATOR:
            job_id = raw.get("job_id")
            artifact_id = raw.get("artifact_id")
            if not isinstance(job_id, str) or not isinstance(artifact_id, str):
                errors.append(f"generated manifest item {index} lacks job/artifact id")
                continue
            key = (job_id, artifact_id)
            if key in generated:
                errors.append(f"duplicate generated job/artifact id: {key}")
            generated[key] = raw
            if raw.get("version_role") == "canonical":
                canonical_counts[job_id] = canonical_counts.get(job_id, 0) + 1

    artifact_records = checks.get("artifacts")
    if not isinstance(artifact_records, list):
        errors.append("checks.artifacts must be a list")
        artifact_records = []
    for record in artifact_records:
        if not isinstance(record, dict):
            errors.append("checks artifact record is not an object")
            continue
        key = (record.get("job_id"), record.get("artifact_id"))
        entry = generated.get(key)
        if entry is None:
            errors.append(f"artifact missing from manifest: {key}")
            continue
        stored = record.get("stored_relpath")
        if entry.get("stored_relpath") != stored:
            errors.append(f"artifact stored path mismatch: {key}")
            continue
        try:
            path = resolve_inside(
                task_dir,
                safe_rel_path(stored, field=f"artifact {key} stored_relpath"),
                field=f"artifact {key} stored_relpath",
            )
        except IntegrationError as exc:
            errors.append(str(exc))
            continue
        if path.is_file():
            if sha256_file(path) != record.get("sha256"):
                errors.append(f"artifact hash mismatch: {key}")
            if path.stat().st_size != record.get("size"):
                errors.append(f"artifact size mismatch: {key}")

    expected_artifacts = {
        (record.get("job_id"), record.get("artifact_id"))
        for record in artifact_records
        if isinstance(record, dict)
    }
    extra_generated = sorted(set(generated) - expected_artifacts)
    if extra_generated:
        errors.append(f"unrecorded generated manifest artifacts: {extra_generated}")

    mappings = checks.get("canonical_mappings")
    if not isinstance(mappings, list):
        errors.append("checks.canonical_mappings must be a list")
        mappings = []
    for mapping in mappings:
        if not isinstance(mapping, dict):
            errors.append("canonical mapping is not an object")
            continue
        job_id = mapping.get("job_id")
        if canonical_counts.get(job_id, 0) != 1:
            errors.append(
                f"{job_id}: expected exactly one canonical manifest artifact, "
                f"found {canonical_counts.get(job_id, 0)}"
            )
        source_target = mapping.get("source_target_path")
        canonical_target = mapping.get("canonical_target_path")
        moved = source_target != canonical_target
        if mapping.get("moved") is not moved:
            errors.append(f"{job_id}: canonical moved flag is inconsistent")
        remove_paths = metadata.get("input_remove_paths", [])
        if moved and source_target not in remove_paths:
            errors.append(f"{job_id}: moved source path is not removed")
        if canonical_target in remove_paths:
            errors.append(f"{job_id}: canonical target is also marked for removal")

    remove_paths = metadata.get("input_remove_paths", [])
    if not isinstance(remove_paths, list):
        errors.append("metadata.input_remove_paths must be a list")
    else:
        for index, path in enumerate(remove_paths):
            try:
                safe_rel_path(path, field=f"input_remove_paths[{index}]")
            except IntegrationError as exc:
                errors.append(str(exc))
        if remove_paths != sorted(set(remove_paths)):
            errors.append("metadata.input_remove_paths must be sorted and unique")

    expected_count = checks.get("counts", {}).get("artifacts")
    if expected_count != len(artifact_records):
        errors.append("checks artifact count is inconsistent")
    report = {
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "counts": {
            "manifest_entries": len(manifest),
            "generated_artifacts": len(generated),
            "canonical_jobs": len(canonical_counts),
        },
    }
    return report
