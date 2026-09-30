"""Main/worker/validator orchestration for local-noise generation."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

try:
    from ._fs import read_json, sha256_file, write_json
except ImportError:
    from _fs import read_json, sha256_file, write_json

try:
    from .prompts import main_agent_prompt, validation_prompt, worker_prompt
except ImportError:
    from prompts import main_agent_prompt, validation_prompt, worker_prompt


Json = Any
WORKER_TOOL_NAMES = (
    "corrupt_file_gen.py",
    "version_gen.py",
)
VISIBLE_BAD_NAME_RE = re.compile(
    r"(错误|旧版|过期|损坏|下载不完整|不要使用|正确答案)",
    re.IGNORECASE,
)


class AgentBackend(Protocol):
    def run(
        self,
        *,
        role: str,
        prompt: str,
        work_dir: Path,
        sandbox_dir: Path,
        resume_session_id: str | None = None,
    ) -> dict[str, Json]: ...


def install_worker_tools(worker_dir: Path) -> list[Path]:
    """Copy worker-facing helper scripts into the worker's sandbox root."""

    scripts_dir = Path(__file__).resolve().parent
    installed: list[Path] = []
    for name in WORKER_TOOL_NAMES:
        source = scripts_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"worker tool not found: {source}")
        target = worker_dir / name
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o111)
        installed.append(target)
    return installed


def json_first_object(
    text: str,
    *,
    require: Callable[[dict[str, Json]], bool] | None = None,
) -> dict[str, Json] | None:
    """Return a JSON object from free-form model output.

    Agents routinely echo the schema examples from their own prompt before the
    real answer, and the worker template embeds a ``noise_files`` example ahead
    of the ``file_jobs`` one. Taking the first decodable object would then hand
    back a placeholder. ``require`` lets the caller name the shape it wants so
    echoed templates are skipped; with no match the caller falls back rather
    than acting on the wrong object.
    """

    decoder = json.JSONDecoder()
    source = str(text or "")
    first: dict[str, Json] | None = None
    for index, char in enumerate(source):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(source[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if require is None or require(value):
            return value
        if first is None:
            first = value
    return first if require is None else None


def looks_like_task_plan(value: dict[str, Json]) -> bool:
    return isinstance(value.get("file_jobs"), list) and bool(value["file_jobs"])


def looks_like_validation(value: dict[str, Json]) -> bool:
    return isinstance(value.get("status"), str)


def profile_inputs(task_dir: Path, metadata: dict[str, Json]) -> list[dict[str, Json]]:
    profiles: list[dict[str, Json]] = []
    manifest = metadata.get("data_manifest")
    if not isinstance(manifest, list):
        return profiles
    for index, item in enumerate(manifest):
        if not isinstance(item, dict) or item.get("generated_by"):
            continue
        stored = str(item.get("stored_relpath") or "").strip()
        filename = str(item.get("filename") or Path(stored).name).strip()
        source = task_dir / stored
        if not source.is_file():
            raise FileNotFoundError(source)
        head = source.read_bytes()[:1024]
        profiles.append(
            {
                "input_index": index,
                "filename": filename,
                "stored_relpath": stored,
                "target_path": item.get("target_path"),
                "size_bytes": source.stat().st_size,
                "sha256": sha256_file(source),
                "suffix": source.suffix.lower(),
                "text_head": head.decode("utf-8", errors="ignore")[:300],
            }
        )
    return profiles


def validate_task_plan(
    plan: dict[str, Json],
    *,
    profiles: list[dict[str, Json]],
) -> None:
    jobs = plan.get("file_jobs")
    if not isinstance(jobs, list):
        raise ValueError("task plan file_jobs must be a list")
    if len(jobs) != len(profiles):
        raise ValueError(
            f"expected {len(profiles)} file jobs, received {len(jobs)}"
        )
    expected = {int(item["input_index"]) for item in profiles}
    actual: set[int] = set()
    job_ids: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("file job must be an object")
        input_index = int(job.get("input_index"))
        job_id = str(job.get("job_id") or "").strip()
        if not job_id or job_id in job_ids:
            raise ValueError(f"invalid or duplicate job_id: {job_id!r}")
        worker_task = str(job.get("worker_prompt") or "").strip()
        if not worker_task:
            raise ValueError(
                f"file job {job_id!r} must include a natural-language "
                "worker_prompt"
            )
        job_ids.add(job_id)
        actual.add(input_index)
    if actual != expected:
        raise ValueError(
            f"file job input indices mismatch: expected={sorted(expected)}, "
            f"actual={sorted(actual)}"
        )


def complete_task_plan(
    plan: dict[str, Json],
    *,
    profiles: list[dict[str, Json]],
    seed: int,
    task_id: str | None = None,
) -> None:
    """Fill program-owned routing fields omitted by the planning Agent.

    The planner is asked for the minimal ``{input_index, worker_prompt}`` shape,
    so plan-level bookkeeping that ``integration`` requires (``schema_version``,
    ``task_id``) has to be supplied here rather than expected from the model.
    """

    plan.setdefault("schema_version", 1)
    if task_id is not None:
        plan.setdefault("task_id", str(task_id))
    jobs = plan.get("file_jobs")
    if not isinstance(jobs, list):
        return
    profile_by_index = {
        int(profile["input_index"]): profile for profile in profiles
    }
    for position, job in enumerate(jobs):
        if not isinstance(job, dict):
            continue
        input_index = int(job.get("input_index"))
        profile = profile_by_index.get(input_index, {})
        job.setdefault("job_id", f"file_{position + 1:03d}")
        job.setdefault("source_id", f"source_{position + 1:03d}")
        job.setdefault("seed", seed + position + 1)
        job.setdefault("related_rubric_indices", [])
        job.setdefault("input_file", profile.get("filename"))
        job.setdefault("count_as_independent_source", True)


def validate_worker_result(
    result: dict[str, Json],
    *,
    job: dict[str, Json],
    work_dir: Path,
    min_distractors: int = 0,
) -> None:
    """Validate a worker's noise output.

    Workers only produce noise. The canonical standard input is placed by
    ``integration`` straight from the task directory, so there is no canonical
    artifact to count here and no answer-preservation flag to trust: the
    property holds structurally because no agent-writable byte reaches it.
    """

    if result.get("job_id") != job.get("job_id"):
        raise ValueError("worker result job_id mismatch")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("worker result must include artifacts")
    distractors = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("worker artifact must be an object")
        rel = str(artifact.get("path") or "").strip()
        path = (work_dir / rel).resolve()
        if not path.is_relative_to(work_dir.resolve()) or not path.is_file():
            raise ValueError(f"worker artifact not found or unsafe: {rel}")
        if VISIBLE_BAD_NAME_RE.search(Path(rel).name):
            raise ValueError(f"worker artifact has an obvious noise name: {rel}")
        role = str(artifact.get("version_role") or "")
        if role == "distractor":
            distractors += 1
            if not artifact.get("exclusion_evidence"):
                raise ValueError(f"distractor lacks exclusion evidence: {rel}")
    if distractors < min_distractors:
        raise ValueError(
            f"worker produced {distractors} distractor(s), "
            f"at least {min_distractors} required"
        )


def adapt_noise_files_result(
    result: dict[str, Json],
    *,
    job: dict[str, Json],
    work_dir: Path,
    input_path: Path | None = None,
) -> dict[str, Json]:
    """Convert the worker-facing minimal JSON into the internal artifact model.

    Workers only need to describe generated noise with four fields.  Target
    paths, hashes, roles, and stable artifact ids are program-owned
    implementation details and are added here.  ``input_path`` is the worker's
    read-only copy of the standard input; listing it as noise is rejected.
    """

    noise_files = result.get("noise_files")
    if not isinstance(noise_files, list):
        return result

    # Preserve exactly what the worker authored for inspection and future
    # prompt/debug work. The normalized result remains private to the pipeline.
    write_json(work_dir / "worker_noise_result.json", result)

    artifacts: list[dict[str, Json]] = []
    type_to_kind = {
        "version": "version_variant",
        "corrupt": "format_variant",
        "template": "partial_data",
        "other": "version_variant",
    }
    target_parent = PurePosixPath(str(job["subset_path"])).parent
    protected = {input_path.resolve()} if input_path is not None else set()
    input_sha = sha256_file(input_path) if input_path is not None else None
    seen_paths: set[Path] = set()
    seen_digests: dict[str, str] = {}
    rejected: list[dict[str, Json]] = []
    for index, raw in enumerate(noise_files, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"noise_files[{index - 1}] must be an object")
        rel = str(raw.get("path") or "").strip()
        source = (work_dir / rel).resolve()
        # A path escaping the worker directory is a safety violation and still
        # fails the job. A merely missing file is a bookkeeping slip -- the model
        # listed something it never wrote -- so drop that entry and keep the rest.
        if not source.is_relative_to(work_dir.resolve()):
            raise ValueError(f"noise file path is unsafe: {rel}")
        if not source.is_file():
            rejected.append({"path": rel, "reason": "declared but not written"})
            continue
        if source in protected:
            raise ValueError(
                f"noise file must not be the standard input itself: {rel}"
            )
        if source in seen_paths:
            raise ValueError(f"noise file listed more than once: {rel}")
        seen_paths.add(source)
        # Reject the offending file rather than the whole job. A single bad
        # artifact should not discard its siblings; each drop is recorded so the
        # loss is visible instead of silently reducing the noise count.
        if VISIBLE_BAD_NAME_RE.search(source.name):
            rejected.append({"path": rel, "reason": "obvious noise name"})
            continue
        digest = sha256_file(source)
        # A byte-identical copy is legitimate noise: duplicate files under
        # different names are among the most common real-world distractors. Only
        # record the fact so the Validator can judge whether the worker's stated
        # changes actually match the bytes -- the program does not decide that.
        duplicate_of = seen_digests.setdefault(digest, rel)
        noise_type = str(raw.get("type") or "other").strip().lower()
        changes = str(raw.get("changes") or "").strip()
        exclusion = str(raw.get("exclusion_reason") or "").strip()
        if not changes:
            raise ValueError(f"noise file lacks changes: {rel}")
        if not exclusion:
            raise ValueError(f"noise file lacks exclusion_reason: {rel}")
        target = (
            PurePosixPath(source.name)
            if str(target_parent) == "."
            else target_parent / source.name
        )
        artifact: dict[str, Json] = {
            "path": rel,
            "file": rel,
            "artifact_id": f"noise_{index:03d}",
            "version_role": "distractor",
            "noise_kind": type_to_kind.get(
                noise_type, "version_variant"
            ),
            "logical_version": noise_type or "other",
            "target_path": target.as_posix(),
            "filename": source.name,
            "changes": [changes],
            "affected_fact_ids": [],
            "exclusion_evidence": [exclusion],
            "sha256": digest,
        }
        # Surface content identity so the Validator can check the worker's
        # stated changes against the bytes. A duplicate whose changes claim a
        # different period or figure is describing something not in the file.
        if input_sha is not None and digest == input_sha:
            artifact["identical_to_standard_input"] = True
        if duplicate_of != rel:
            artifact["identical_to_noise_file"] = duplicate_of
        artifacts.append(artifact)
    adapted: dict[str, Json] = {
        "schema_version": 1,
        "job_id": job["job_id"],
        "source_id": job.get("source_id", job["job_id"]),
        "source_stored_relpath": job["source_stored_relpath"],
        "artifacts": artifacts,
        "reworked_issue_ids": [],
        "generation_status": "noise_files_adapted",
    }
    if rejected:
        adapted["rejected_noise_files"] = rejected
    return adapted


def normalize_worker_result_roles(
    result: dict[str, Json],
    *,
    work_dir: Path,
) -> bool:
    """Drop a legacy canonical artifact from an older worker result.

    Results produced before the canonical input left the worker contract still
    declare one. ``integration`` ignores it, but removing it here keeps the
    stored result and the integrated manifest describing the same set of files.
    """

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    remaining = [
        artifact
        for artifact in artifacts
        if not (
            isinstance(artifact, dict)
            and artifact.get("version_role") == "canonical"
        )
    ]
    if len(remaining) == len(artifacts):
        return False
    result["artifacts"] = remaining
    result.pop("canonical_answer_preserved", None)
    return True


def normalize_duplicate_target_paths(result: dict[str, Json]) -> bool:
    """Give artifacts unique natural targets within a job."""

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    seen: set[str] = set()
    changed = False
    for item in (item for item in artifacts if isinstance(item, dict)):
        target = str(item.get("target_path") or "").strip()
        if not target:
            continue
        if target not in seen:
            seen.add(target)
            continue
        source_name = Path(str(item.get("path") or item.get("file") or "")).name
        parent = Path(target).parent.as_posix()
        candidate = (
            f"{parent}/{source_name}" if parent not in {"", "."} else source_name
        )
        stem = Path(source_name).stem
        suffix = Path(source_name).suffix
        counter = 2
        while candidate in seen:
            renamed = f"{stem} ({counter}){suffix}"
            candidate = (
                f"{parent}/{renamed}" if parent not in {"", "."} else renamed
            )
            counter += 1
        item["target_path"] = candidate
        seen.add(candidate)
        changed = True
    return changed


def repair_worker_artifact_paths(
    result: dict[str, Json],
    *,
    work_dir: Path,
) -> bool:
    """Repair a missing artifact path when one unique normalized file matches."""

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    artifacts_dir = work_dir / "artifacts"
    available = [
        path for path in artifacts_dir.rglob("*")
        if path.is_file()
    ] if artifacts_dir.is_dir() else []

    def key(value: str) -> str:
        return re.sub(r"[\s_\-（）()]+", "", value).casefold()

    changed = False
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        rel = str(artifact.get("path") or artifact.get("file") or "")
        if not rel:
            continue
        if (work_dir / rel).is_file():
            continue
        wanted = key(Path(rel).name)
        matches = [
            path for path in available
            if key(path.name) == wanted
        ]
        if len(matches) != 1:
            continue
        repaired = matches[0].relative_to(work_dir).as_posix()
        artifact["path"] = repaired
        artifact["file"] = repaired
        artifact["filename"] = matches[0].name
        artifact["sha256"] = sha256_file(matches[0])
        changed = True
    return changed


def expand_worker_artifact_directories(
    result: dict[str, Json],
    *,
    work_dir: Path,
) -> bool:
    """Expand a declared artifact directory into deterministic file artifacts."""

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    expanded: list[dict[str, Json]] = []
    changed = False
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            expanded.append(artifact)
            continue
        rel = str(artifact.get("path") or artifact.get("file") or "")
        source = work_dir / rel
        if not source.is_dir():
            expanded.append(artifact)
            continue
        changed = True
        base_id = str(artifact.get("artifact_id") or "evidence_dir")
        base_target = str(
            artifact.get("target_path")
            or Path(rel).name
        ).rstrip("/")
        files = sorted(
            path for path in source.rglob("*") if path.is_file()
        )
        for index, path in enumerate(files):
            relative_inside = path.relative_to(source).as_posix()
            item = copy.deepcopy(artifact)
            item["path"] = path.relative_to(work_dir).as_posix()
            item["file"] = item["path"]
            item["filename"] = path.name
            item["artifact_id"] = (
                f"{base_id}_{index:04d}_"
                f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', path.stem)}"
            )
            item["target_path"] = (
                f"{base_target}/{relative_inside}"
            )
            item["version_role"] = "evidence"
            item["sha256"] = sha256_file(path)
            expanded.append(item)
    if changed:
        result["artifacts"] = expanded
    return changed


@dataclass
class CodexBackend:
    provider: dict[str, Json]
    timeout_seconds: float = 1800.0
    agent_module_path: Path | None = None

    def _module(self):
        path = self.agent_module_path
        if path is None:
            path = Path(__file__).resolve().parents[2] / "src" / "agents" / "codex.py"
        src_dir = str(path.parents[1])
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        spec = importlib.util.spec_from_file_location("noise_agent_codex", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load Codex backend: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run(
        self,
        *,
        role: str,
        prompt: str,
        work_dir: Path,
        sandbox_dir: Path,
        resume_session_id: str | None = None,
    ) -> dict[str, Json]:
        module = self._module()
        result = module.run(
            prompt=prompt,
            work_dir=str(work_dir),
            sandbox_dir=str(sandbox_dir),
            timeout_s=self.timeout_seconds,
            api_provider=copy.deepcopy(self.provider),
            agent_id=f"noise-{role}",
            resume_session_id=resume_session_id,
            persist_session=role.startswith("worker"),
        )
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise RuntimeError(
                f"{role} agent failed: "
                f"{result.get('errorMessage') if isinstance(result, dict) else result}"
            )
        return result


@dataclass
class NoisePipeline:
    task_dir: Path
    subset_root: Path
    run_dir: Path
    backend: AgentBackend
    planner_backend: AgentBackend | None = None
    worker_backend: AgentBackend | None = None
    validator_backend: AgentBackend | None = None
    worker_fallback_backend: AgentBackend | None = None
    seed: int = 1
    max_versions: int = 3
    max_rework_rounds: int = 3
    worker_parallelism: int = 1
    min_distractors_per_job: int = 1
    integrate_callback: Callable[..., dict[str, Json]] | None = None

    def _metadata(self) -> dict[str, Json]:
        return read_json(self.task_dir / "metadata.json")

    def _backend_for(self, role: str) -> AgentBackend:
        if role == "planner" and self.planner_backend is not None:
            return self.planner_backend
        if role == "worker" and self.worker_backend is not None:
            return self.worker_backend
        if role == "validator" and self.validator_backend is not None:
            return self.validator_backend
        return self.backend

    @staticmethod
    def _requests_for_validation(
        validation: dict[str, Json],
        jobs_by_id: dict[str, dict[str, Json]],
    ) -> dict[str, dict[str, Json]]:
        requests = {
            str(item["job_id"]): copy.deepcopy(item)
            for item in validation.get("rework_requests", [])
            if isinstance(item, dict) and item.get("job_id")
        }
        issues_by_job: dict[str, list[dict[str, Json]]] = {}
        for issue in validation.get("blocking_issues", []):
            if isinstance(issue, dict) and issue.get("job_id"):
                issues_by_job.setdefault(
                    str(issue["job_id"]), []
                ).append(issue)
        for raw_job_id in validation.get("affected_jobs", []):
            job_id = str(raw_job_id)
            if job_id in requests or job_id not in jobs_by_id:
                continue
            issues = issues_by_job.get(job_id, [])
            requests[job_id] = {
                "job_id": job_id,
                "issue_ids": [
                    str(issue["issue_id"])
                    for issue in issues
                    if issue.get("issue_id")
                ],
                "problem": "; ".join(
                    str(issue.get("reason") or "")
                    for issue in issues
                    if issue.get("reason")
                ),
                "must_preserve": jobs_by_id[job_id].get(
                    "must_preserve", []
                ),
                "requested_changes": [
                    str(issue.get("required_fix"))
                    for issue in issues
                    if issue.get("required_fix")
                ],
                "acceptance": [
                    "修复 blocking issue 且保持 canonical 标准答案不变。",
                    "只能使用当前任务包中的输入和规则完成返工。",
                ],
            }
        return requests

    def _run_rework_batch(
        self,
        *,
        plan: dict[str, Json],
        jobs_by_id: dict[str, dict[str, Json]],
        results_by_id: dict[str, dict[str, Json]],
        job_ids: list[str],
        requests: dict[str, dict[str, Json]],
        rework_round: int,
    ) -> dict[str, str]:
        def rework_one(job_id: str) -> tuple[str, dict[str, Json]]:
            request = copy.deepcopy(requests.get(job_id) or {})
            request["rework_round"] = rework_round
            request.setdefault(
                "seed",
                self.seed + rework_round * 100000 + len(job_id),
            )
            return (
                job_id,
                self.run_worker(
                    plan=plan,
                    job=jobs_by_id[job_id],
                    rework_request=request,
                ),
            )

        failures: dict[str, str] = {}
        if self.worker_parallelism > 1 and len(job_ids) > 1:
            with ThreadPoolExecutor(
                max_workers=min(self.worker_parallelism, len(job_ids))
            ) as executor:
                future_to_job = {
                    executor.submit(rework_one, job_id): job_id
                    for job_id in job_ids
                }
                for future in as_completed(future_to_job):
                    job_id = future_to_job[future]
                    try:
                        completed_id, worker_result = future.result()
                        results_by_id[completed_id] = worker_result
                    except Exception as exc:
                        failures[job_id] = f"{type(exc).__name__}: {exc}"
        else:
            for job_id in job_ids:
                try:
                    completed_id, worker_result = rework_one(job_id)
                    results_by_id[completed_id] = worker_result
                except Exception as exc:
                    failures[job_id] = f"{type(exc).__name__}: {exc}"
        return failures

    def _subset_manifest(self) -> dict[str, Json]:
        for path in (
            self.subset_root / "subset_manifest.json",
            self.subset_root.parent / "subset_manifest.json",
        ):
            if path.is_file():
                return read_json(path)
        return {"business_roots": [], "root": str(self.subset_root)}

    def _source_path_map(self) -> dict[str, Json]:
        for path in (
            self.subset_root / "source_path_map.json",
            self.subset_root.parent / "source_path_map.json",
        ):
            if path.is_file():
                return read_json(path)
        return {"sources": []}

    def _main_context(self, metadata: dict[str, Json]) -> Path:
        context = self.run_dir / "main"
        if context.exists():
            shutil.rmtree(context)
        (context / "standard_inputs").mkdir(parents=True)
        for item in metadata.get("data_manifest", []):
            if not isinstance(item, dict) or item.get("generated_by"):
                continue
            source = self.task_dir / str(item["stored_relpath"])
            shutil.copy2(source, context / "standard_inputs" / source.name)
        shutil.copy2(self.task_dir / "metadata.json", context / "metadata.json")
        return context

    def plan(self) -> dict[str, Json]:
        metadata = self._metadata()
        profiles = profile_inputs(self.task_dir, metadata)
        compact_metadata = {
            "id": metadata.get("id", metadata.get("absolute_id")),
            "task": metadata.get("task"),
            "rubrics": metadata.get("rubrics", []),
            "file_dep_graph": metadata.get("file_dep_graph", []),
            "output_files": metadata.get("output_files", []),
        }
        context = self._main_context(metadata)
        plan_task_id = str(
            metadata.get("absolute_id", metadata.get("id", "")) or ""
        )
        planner_error = None
        try:
            result = self._backend_for("planner").run(
                role="main",
                prompt=main_agent_prompt(
                    task_metadata=compact_metadata,
                    input_profiles=profiles,
                    subset_manifest=self._subset_manifest(),
                    seed=self.seed,
                    max_versions=self.max_versions,
                ),
                work_dir=context,
                sandbox_dir=self.run_dir / "sandboxes" / "main",
            )
            plan = json_first_object(
                str(result.get("trace", {}).get("lastText") or ""),
                require=looks_like_task_plan,
            )
            if plan is not None:
                # Validate before accepting. An unusable plan must fall through
                # to the deterministic fallback instead of aborting the run.
                probe = copy.deepcopy(plan)
                complete_task_plan(
                    probe,
                    profiles=profiles,
                    seed=self.seed,
                    task_id=plan_task_id,
                )
                validate_task_plan(probe, profiles=profiles)
        except Exception as exc:
            planner_error = f"{type(exc).__name__}: {exc}"
            plan = None
        if plan is None:
            candidate = context / "task_plan.json"
            if candidate.is_file():
                try:
                    plan = read_json(candidate)
                    probe = copy.deepcopy(plan)
                    complete_task_plan(
                        probe,
                        profiles=profiles,
                        seed=self.seed,
                        task_id=plan_task_id,
                    )
                    validate_task_plan(probe, profiles=profiles)
                except Exception as exc:
                    planner_error = (
                        f"{planner_error}; " if planner_error else ""
                    ) + f"task_plan.json unusable: {type(exc).__name__}: {exc}"
                    plan = None
        if plan is None:
            rubric_indices = list(
                range(len(metadata.get("rubrics", [])))
            )
            plan = {
                "schema_version": 1,
                "task_id": str(
                    metadata.get("id", metadata.get("absolute_id", ""))
                ),
                "scope_rules": {
                    "business_roots": self._subset_manifest().get(
                        "local_roots", []
                    ),
                    "include_rules": [
                        "使用工作区子集中的标准输入和自然候选版本"
                    ],
                    "exclude_rules": [
                        "排除无法由内容支持的候选版本"
                    ],
                },
                "calculation_rules": {
                    "deduplication": "沿用标准任务输入的文件级口径",
                    "time_scope": "沿用标准任务描述",
                    "rounding": "沿用原始文件规则",
                    "missing_values": "不补造缺失标准事实",
                },
                "facts": [],
                "file_jobs": [
                    {
                        "job_id": f"file_{index + 1:03d}",
                        "input_index": int(profile["input_index"]),
                        "input_file": profile["filename"],
                        "source_id": f"source_{index + 1:03d}",
                        "file_role": "标准任务输入",
                        "related_rubric_indices": rubric_indices,
                        "must_preserve": [
                            "标准输入内容及其对原始答案的支持"
                        ],
                        "allowed_noise": [
                            "生成一个内容可核验的自然版本候选"
                        ],
                        "required_evidence": [
                            "标准输入可由内容完整性识别"
                        ],
                        "count_as_independent_source": True,
                        "seed": self.seed + index + 1,
                        "worker_prompt": (
                            f"你负责处理“{profile['filename']}”。input/ 中是它的"
                            "只读副本。请以该副本为基底制作两个自然可信的内容型"
                            "噪声版本；噪声应修改与任务结论有关的数据范围、状态、"
                            "公式或完整性细节，不能只改名称。每个噪声必须能通过"
                            "文件内部的范围、合计、公式、日期或业务规则被排除。"
                            "不要修改或复制 input/ 中的文件。将噪声写入 "
                            "artifacts/，并在 worker_result.json 的 noise_files "
                            "中记录 path、type、changes 和 exclusion_reason。"
                        ),
                    }
                    for index, profile in enumerate(profiles)
                ],
                "integration_plan": {
                    "common_version_evidence_files": [],
                    "path_policy": "保持标准输入业务路径",
                    "max_rework_rounds": self.max_rework_rounds,
                },
                "generation_status": "deterministic_fallback_plan",
                "planner_error": planner_error,
            }
        complete_task_plan(
            plan,
            profiles=profiles,
            seed=self.seed,
            task_id=plan_task_id,
        )
        validate_task_plan(plan, profiles=profiles)
        profile_by_index = {
            int(profile["input_index"]): profile for profile in profiles
        }
        integration_jobs = []
        source_map = self._source_path_map()
        for job in plan["file_jobs"]:
            profile = profile_by_index[int(job["input_index"])]
            job.setdefault("input_file", profile["filename"])
            job["source_stored_relpath"] = profile["stored_relpath"]
            job["subset_path"] = (
                next(
                    (
                        source.get("selected_path")
                        for source in source_map.get("sources", [])
                        if source.get("stored_relpath")
                        == profile["stored_relpath"]
                    ),
                    None,
                )
                or profile.get("target_path")
                or profile["filename"]
            )
            integration_jobs.append(
                {
                    "job_id": job["job_id"],
                    "source_stored_relpath": job["source_stored_relpath"],
                    "subset_path": job["subset_path"],
                }
            )
        plan["jobs"] = integration_jobs
        write_json(self.run_dir / "task_plan.json", plan)
        write_json(
            self.run_dir / "fact_ledger.json",
            {"schema_version": 1, "facts": plan.get("facts", [])},
        )
        return plan


    def run_worker(
        self,
        *,
        plan: dict[str, Json],
        job: dict[str, Json],
        rework_request: dict[str, Json] | None = None,
    ) -> dict[str, Json]:
        metadata = self._metadata()
        job_id = str(job["job_id"])
        worker_dir = self.run_dir / "workers" / job_id
        if worker_dir.exists() and rework_request is None:
            shutil.rmtree(worker_dir)
        worker_dir.mkdir(parents=True, exist_ok=True)
        state_path = worker_dir / "worker_state.json"
        previous_state = (
            read_json(state_path)
            if rework_request is not None and state_path.is_file()
            else {}
        )
        profile = next(
            item
            for item in profile_inputs(self.task_dir, metadata)
            if int(item["input_index"]) == int(job["input_index"])
        )
        source = self.task_dir / str(profile["stored_relpath"])
        input_dir = worker_dir / "input"
        input_dir.mkdir(exist_ok=True)
        input_copy = input_dir / str(profile["filename"])
        shutil.copy2(source, input_copy)
        install_worker_tools(worker_dir)
        write_json(worker_dir / "file_job.json", job)
        if rework_request is not None:
            write_json(worker_dir / "rework_request.json", rework_request)
        # Create the crash-safe scaffold only for the initial run. During
        # rework, retain all artifacts and worker_result.json so the same
        # agent can inspect and amend its previous work in place.
        #
        # The scaffold holds no artifacts: the standard input is placed by
        # integration straight from the task directory, so a failed worker
        # simply means this job produced no noise.
        artifacts_dir = worker_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        result_path = worker_dir / "worker_result.json"
        if rework_request is None or not result_path.is_file():
            write_json(
                result_path,
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "source_id": job.get("source_id", job_id),
                    "source_stored_relpath": job["source_stored_relpath"],
                    "artifacts": [],
                    "reworked_issue_ids": [],
                    "generation_status": "empty_scaffold",
                },
            )
        prompt = worker_prompt(
            job=job,
            rework_request=rework_request,
        )
        primary_error: Exception | None = None
        rework_round = int(
            (rework_request or {}).get("rework_round")
            or previous_state.get("round")
            or 0
        )
        previous_session_id = str(
            previous_state.get("session_id") or ""
        ).strip() or None
        previous_backend = str(
            previous_state.get("backend") or "primary"
        )
        active_backend = (
            self.worker_fallback_backend
            if previous_backend == "fallback"
            and self.worker_fallback_backend is not None
            else self._backend_for("worker")
        )
        active_backend_name = (
            "fallback"
            if active_backend is self.worker_fallback_backend
            else "primary"
        )
        sandbox_dir = (
            self.run_dir
            / "sandboxes"
            / f"worker-{job_id}"
            / f"round_{rework_round}"
        )
        agent_result: dict[str, Json] | None = None
        try:
            agent_result = active_backend.run(
                role=f"worker-{job_id}",
                prompt=prompt,
                work_dir=worker_dir,
                sandbox_dir=sandbox_dir,
                resume_session_id=previous_session_id,
            )
        except Exception as exc:
            primary_error = exc
        # Only fall back when nothing usable landed. A primary that wrote its
        # noise files and then timed out needs no second model, and re-running
        # one would just pay for the same work twice.
        def _has_noise_on_disk() -> bool:
            if not result_path.is_file():
                return False
            try:
                saved = read_json(result_path)
            except (OSError, ValueError):
                return False
            for key in ("noise_files", "artifacts"):
                value = saved.get(key)
                if isinstance(value, list) and value:
                    return True
            return False

        if (
            (primary_error is not None or not result_path.is_file())
            and not _has_noise_on_disk()
            and self.worker_fallback_backend is not None
        ):
            fallback_note = (
                "\n\n上一模型未完成落盘。请检查当前 artifacts/ 和 input/，"
                "保留可用产物并完成 worker_result.json；不要重复无关分析。"
            )
            try:
                agent_result = self.worker_fallback_backend.run(
                    role=f"worker-fallback-{job_id}",
                    prompt=prompt + fallback_note,
                    work_dir=worker_dir,
                    sandbox_dir=self.run_dir
                    / "sandboxes"
                    / f"worker-fallback-{job_id}",
                    resume_session_id=(
                        previous_session_id
                        if previous_backend == "fallback"
                        else None
                    ),
                )
                active_backend_name = "fallback"
                primary_error = None
            except Exception as fallback_error:
                # Record both failures instead of turning one unavailable model
                # into an exception. The job is left with no noise, which the
                # deterministic zero-noise check reports as a failure.
                result = read_json(result_path)
                result["generation_status"] = (
                    "empty_scaffold_after_primary_and_fallback_failure"
                )
                result["generation_error"] = {
                    "primary": (
                        f"{type(primary_error).__name__}: {primary_error}"
                        if primary_error is not None
                        else "primary did not create worker_result.json"
                    ),
                    "fallback": (
                        f"{type(fallback_error).__name__}: {fallback_error}"
                    ),
                }
                write_json(result_path, result)
                primary_error = None
        trace = (
            agent_result.get("trace", {})
            if isinstance(agent_result, dict)
            and isinstance(agent_result.get("trace"), dict)
            else {}
        )
        session_id = str(
            trace.get("threadId") or previous_session_id or ""
        ).strip() or None
        state_history = list(previous_state.get("history") or [])
        state_history.append(
            {
                "round": rework_round,
                "backend": active_backend_name,
                "session_id": session_id,
                "resumed": bool(previous_session_id),
                "rework": rework_request is not None,
            }
        )
        write_json(
            state_path,
            {
                "schema_version": 1,
                "job_id": job_id,
                "backend": active_backend_name,
                "session_id": session_id,
                "round": rework_round,
                "history": state_history,
            },
        )
        if primary_error is not None and not result_path.is_file():
            raise primary_error
        if not result_path.is_file():
            raise ValueError(f"worker {job_id} did not create worker_result.json")
        result = read_json(result_path)
        if isinstance(result.get("noise_files"), list):
            result = adapt_noise_files_result(
                result,
                job=job,
                work_dir=worker_dir,
                input_path=input_copy,
            )
            write_json(result_path, result)
        if primary_error is not None:
            # A worker that wrote real noise before dying (commonly: it finished
            # the files, then burned the rest of its budget and hit the timeout)
            # has produced usable work. Keep it and record the error rather than
            # discarding the artifacts, which would fail the job for no reason.
            salvaged = bool(result.get("artifacts"))
            result["generation_status"] = (
                "salvaged_after_model_failure"
                if salvaged
                else "empty_scaffold_after_model_failure"
            )
            result["generation_error"] = (
                f"{type(primary_error).__name__}: {primary_error}"
            )
        expand_worker_artifact_directories(
            result,
            work_dir=worker_dir,
        )
        result["source_stored_relpath"] = job["source_stored_relpath"]
        used_ids: set[str] = set()
        for index, artifact in enumerate(result.get("artifacts", [])):
            rel = str(artifact.get("path") or "")
            artifact.setdefault("file", rel)
            raw_id = str(artifact.get("artifact_id") or f"candidate_{index}")
            artifact_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_id).strip("._-") or f"artifact_{index}"
            while artifact_id in used_ids:
                artifact_id += f"_{index}"
            used_ids.add(artifact_id)
            artifact["artifact_id"] = artifact_id
            source = worker_dir / rel
            if source.is_file():
                artifact["sha256"] = sha256_file(source)
            artifact.setdefault("filename", Path(rel).name)
            if artifact.get("version_role") == "distractor":
                artifact.setdefault("noise_kind", "version_variant")
        repair_worker_artifact_paths(result, work_dir=worker_dir)
        normalize_worker_result_roles(result, work_dir=worker_dir)
        normalize_duplicate_target_paths(result)
        if rework_request is not None:
            resolved = {
                str(item)
                for item in result.get("reworked_issue_ids", [])
            }
            resolved.update(
                str(item)
                for item in rework_request.get("issue_ids", [])
            )
            result["reworked_issue_ids"] = sorted(resolved)
        write_json(result_path, result)
        validate_worker_result(result, job=job, work_dir=worker_dir)
        return result

    def run_workers(
        self,
        plan: dict[str, Json],
    ) -> list[dict[str, Json]]:
        jobs = list(plan["file_jobs"])
        failures: dict[str, str] = {}
        if self.worker_parallelism <= 1 or len(jobs) <= 1:
            results = []
            for job in jobs:
                job_id = str(job["job_id"])
                try:
                    results.append(self.run_worker(plan=plan, job=job))
                except Exception as exc:
                    failures[job_id] = f"{type(exc).__name__}: {exc}"
        else:
            by_job_id: dict[str, dict[str, Json]] = {}
            with ThreadPoolExecutor(
                max_workers=min(self.worker_parallelism, len(jobs))
            ) as executor:
                futures = {
                    executor.submit(
                        self.run_worker,
                        plan=plan,
                        job=job,
                    ): str(job["job_id"])
                    for job in jobs
                }
                for future in as_completed(futures):
                    job_id = futures[future]
                    try:
                        by_job_id[job_id] = future.result()
                    except Exception as exc:
                        failures[job_id] = f"{type(exc).__name__}: {exc}"
            results = [
                by_job_id[str(job["job_id"])]
                for job in jobs
                if str(job["job_id"]) in by_job_id
            ]
        write_json(
            self.run_dir / "worker_results.json",
            {"schema_version": 1, "results": results},
        )
        write_json(
            self.run_dir / "worker_failures.json",
            {"schema_version": 1, "failed_jobs": failures},
        )
        return results

    def integrate(
        self,
        *,
        plan: dict[str, Json],
        worker_results: list[dict[str, Json]],
    ) -> dict[str, Json]:
        if self.integrate_callback is not None:
            checks = self.integrate_callback(
                task_dir=self.task_dir,
                subset_root=self.subset_root,
                run_dir=self.run_dir,
                task_plan=plan,
                worker_results=worker_results,
            )
        else:
            try:
                from .integration import integrate as deterministic_integrate
            except ImportError:
                from integration import integrate as deterministic_integrate
            integrated_root = self.run_dir / "integrated"
            integrated_task = integrated_root / "task"
            if integrated_task.exists():
                shutil.rmtree(integrated_task)
            shutil.copytree(self.task_dir, integrated_task)
            checks = deterministic_integrate(
                task_dir=integrated_task,
                plan_path=self.run_dir / "task_plan.json",
                workers_root=self.run_dir / "workers",
                subset_root=self.subset_root,
                # Results predating the canonical-free worker contract still
                # declare one; it is ignored rather than treated as an error.
                allow_legacy_canonical=True,
                min_distractors_per_job=self.min_distractors_per_job,
            )
            workspace = integrated_root / "workspace"
            if workspace.exists():
                shutil.rmtree(workspace)
            shutil.copytree(self.subset_root, workspace)
            for report_name in (
                "subset_manifest.json",
                "source_path_map.json",
                "build_report.json",
            ):
                try:
                    (workspace / report_name).unlink()
                except FileNotFoundError:
                    pass
            metadata = read_json(integrated_task / "metadata.json")
            for raw_path in metadata.get("input_remove_paths", []):
                target = (workspace / str(raw_path)).resolve()
                if target.is_relative_to(workspace.resolve()):
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        try:
                            target.unlink()
                        except FileNotFoundError:
                            pass
            for item in metadata.get("data_manifest", []):
                if not isinstance(item, dict):
                    continue
                source = integrated_task / str(item["stored_relpath"])
                target = workspace / str(
                    item.get("target_path") or item.get("filename")
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            write_json(
                integrated_root / "deterministic_checks.json",
                checks,
            )
        return checks

    def validate(
        self,
        *,
        plan: dict[str, Json],
        worker_results: list[dict[str, Json]],
        deterministic_checks: dict[str, Json],
        round_index: int,
    ) -> dict[str, Json]:
        workspace = self.run_dir / "integrated" / "workspace"
        validation_dir = self.run_dir / "validation" / f"round_{round_index}"
        validation_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = self._backend_for("validator").run(
                role=f"validator-{round_index}",
                prompt=validation_prompt(
                    task_metadata=self._metadata(),
                    task_plan=plan,
                    worker_results=worker_results,
                    deterministic_checks=deterministic_checks,
                    max_rework_rounds=self.max_rework_rounds,
                ),
                work_dir=workspace,
                sandbox_dir=self.run_dir / "sandboxes" / f"validator-{round_index}",
            )
            validation = json_first_object(
                str(result.get("trace", {}).get("lastText") or ""),
                require=looks_like_validation,
            )
        except Exception as exc:
            validation = {
                "schema_version": 1,
                "status": "failed",
                "summary": (
                    "验证 Agent 未完成；确定性检查结果已保留。"
                ),
                "affected_jobs": [],
                "blocking_issues": [
                    {
                        "issue_id": "VALIDATOR-UNAVAILABLE",
                        "type": "validator_failure",
                        "job_id": None,
                        "files": [],
                        "reason": f"{type(exc).__name__}: {exc}",
                        "required_fix": "重新运行验证 Agent。",
                    }
                ],
                "rework_requests": [],
            }
        if validation is None:
            validation = {
                "schema_version": 1,
                "status": "failed",
                "summary": "验证 Agent 未返回可解析 JSON。",
                "affected_jobs": [],
                "blocking_issues": [
                    {
                        "issue_id": "VALIDATOR-INVALID-OUTPUT",
                        "type": "validator_failure",
                        "job_id": None,
                        "files": [],
                        "reason": "lastText 中没有 JSON 对象",
                        "required_fix": "重新运行验证 Agent。",
                    }
                ],
                "rework_requests": [],
            }
        write_json(validation_dir / "validation_result.json", validation)
        for request in validation.get("rework_requests", []):
            if isinstance(request, dict) and request.get("job_id"):
                write_json(
                    validation_dir
                    / "rework_requests"
                    / f"{request['job_id']}.json",
                    request,
                )
        return validation

    def run(self) -> dict[str, Json]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        plan = self.plan()
        jobs_by_id = {str(job["job_id"]): job for job in plan["file_jobs"]}
        worker_results = self.run_workers(plan)
        results_by_id = {
            str(result["job_id"]): result
            for result in worker_results
        }
        missing_jobs = [
            str(job["job_id"])
            for job in plan["file_jobs"]
            if str(job["job_id"]) not in results_by_id
        ]
        if missing_jobs:
            failures_path = self.run_dir / "worker_failures.json"
            failures = (
                read_json(failures_path).get("failed_jobs", {})
                if failures_path.is_file()
                else {}
            )
            result = {
                "schema_version": 1,
                "status": "worker_incomplete",
                "rounds": 0,
                "missing_jobs": missing_jobs,
                "failed_jobs": failures,
                "validation_history": [],
            }
            write_json(self.run_dir / "pipeline_result.json", result)
            return result
        history = []
        for round_index in range(self.max_rework_rounds + 1):
            worker_results = list(results_by_id.values())
            checks = self.integrate(
                plan=plan,
                worker_results=worker_results,
            )
            validation = self.validate(
                plan=plan,
                worker_results=worker_results,
                deterministic_checks=checks,
                round_index=round_index,
            )
            history.append(validation)
            status = str(validation.get("status") or "")
            if status == "passed" and checks.get("status") == "passed":
                result = {
                    "schema_version": 1,
                    "status": "passed",
                    "rounds": round_index,
                    "validation_history": history,
                }
                write_json(self.run_dir / "pipeline_result.json", result)
                return result
            # A job with no noise cannot pass on semantic review alone: the
            # workspace would ship the standard input with nothing to
            # disambiguate. Let rework run, but never report success.
            if status == "passed":
                result = {
                    "schema_version": 1,
                    "status": "failed",
                    "rounds": round_index,
                    "deterministic_errors": checks.get("errors", []),
                    "noiseless_jobs": checks.get("noiseless_jobs", []),
                    "validation_history": history,
                }
                write_json(self.run_dir / "pipeline_result.json", result)
                return result
            if status != "rework" or round_index >= self.max_rework_rounds:
                result = {
                    "schema_version": 1,
                    "status": "failed",
                    "rounds": round_index,
                    "deterministic_errors": checks.get("errors", []),
                    "noiseless_jobs": checks.get("noiseless_jobs", []),
                    "validation_history": history,
                }
                write_json(self.run_dir / "pipeline_result.json", result)
                return result
            requests = {
                str(item["job_id"]): item
                for item in validation.get("rework_requests", [])
                if isinstance(item, dict) and item.get("job_id")
            }
            affected = [
                str(job_id)
                for job_id in validation.get("affected_jobs", [])
            ]
            if not affected or any(job_id not in jobs_by_id for job_id in affected):
                raise ValueError("validator returned invalid affected_jobs")
            for job_id in affected:
                request = copy.deepcopy(requests.get(job_id) or {})
                request["rework_round"] = round_index + 1
                request.setdefault(
                    "seed",
                    self.seed + (round_index + 1) * 100000 + len(job_id),
                )
                results_by_id[job_id] = self.run_worker(
                    plan=plan,
                    job=jobs_by_id[job_id],
                    rework_request=request,
                )
        raise AssertionError("unreachable")

    def resume(self) -> dict[str, Json]:
        """Resume from an existing plan, workers, and latest validation round."""

        plan = read_json(self.run_dir / "task_plan.json")
        jobs_by_id = {str(job["job_id"]): job for job in plan["file_jobs"]}
        validation_root = self.run_dir / "validation"
        rounds = []
        if validation_root.is_dir():
            for path in sorted(validation_root.glob("round_*/validation_result.json")):
                match = re.search(r"round_(\d+)", path.as_posix())
                if match:
                    rounds.append((int(match.group(1)), read_json(path)))
        if not rounds:
            results_by_id: dict[str, dict[str, Json]] = {}
            missing_jobs: list[str] = []
            for job_id, job in jobs_by_id.items():
                result_path = (
                    self.run_dir / "workers" / job_id / "worker_result.json"
                )
                if not result_path.is_file():
                    missing_jobs.append(job_id)
                    continue
                try:
                    result = read_json(result_path)
                    changed = expand_worker_artifact_directories(
                        result, work_dir=result_path.parent
                    )
                    if repair_worker_artifact_paths(
                        result, work_dir=result_path.parent
                    ):
                        changed = True
                    if normalize_worker_result_roles(
                        result, work_dir=result_path.parent
                    ):
                        changed = True
                    if normalize_duplicate_target_paths(result):
                        changed = True
                    if changed:
                        write_json(result_path, result)
                    validate_worker_result(
                        result,
                        job=job,
                        work_dir=result_path.parent,
                    )
                    results_by_id[job_id] = result
                except Exception:
                    missing_jobs.append(job_id)

            failures = self._run_rework_batch(
                plan=plan,
                jobs_by_id=jobs_by_id,
                results_by_id=results_by_id,
                job_ids=missing_jobs,
                requests={job_id: {} for job_id in missing_jobs},
                rework_round=0,
            )
            if failures:
                result = {
                    "schema_version": 1,
                    "status": "worker_incomplete",
                    "rounds": 0,
                    "missing_jobs": sorted(failures),
                    "failed_jobs": failures,
                    "validation_history": [],
                }
                write_json(self.run_dir / "pipeline_result.json", result)
                return result
            worker_results = [
                results_by_id[str(job["job_id"])]
                for job in plan["file_jobs"]
            ]
            write_json(
                self.run_dir / "worker_results.json",
                {"schema_version": 1, "results": worker_results},
            )
            checks = self.integrate(
                plan=plan,
                worker_results=worker_results,
            )
            validation = self.validate(
                plan=plan,
                worker_results=worker_results,
                deterministic_checks=checks,
                round_index=0,
            )
            write_json(
                self.run_dir / "pipeline_result.json",
                {
                    "schema_version": 1,
                    "status": (
                        "passed"
                        if validation.get("status") == "passed"
                        and checks.get("status") == "passed"
                        else "rework_incomplete"
                        if validation.get("status") == "rework"
                        else "failed"
                    ),
                    "rounds": 0,
                    "deterministic_errors": checks.get("errors", []),
                    "noiseless_jobs": checks.get("noiseless_jobs", []),
                    "validation_history": [validation],
                },
            )
            # Any non-passed status continues through the normal validated
            # resume path on the next invocation, so rework stays individually
            # restartable.
            return read_json(self.run_dir / "pipeline_result.json")
        round_index, latest = max(rounds, key=lambda item: item[0])
        history = [value for _, value in sorted(rounds)]
        if latest.get("status") == "passed":
            result = {
                "schema_version": 1,
                "status": "passed",
                "rounds": round_index,
                "validation_history": history,
            }
            write_json(self.run_dir / "pipeline_result.json", result)
            return result
        latest_issue_types = {
            str(item.get("type") or "")
            for item in latest.get("blocking_issues", [])
            if isinstance(item, dict)
        }
        if (
            latest.get("status") == "failed"
            and latest_issue_types
            and latest_issue_types <= {"validator_failure"}
        ):
            # A transient validator/tooling failure does not invalidate the
            # preceding successful worker rework. Remove only the failed
            # validation attempt and resume from the previous semantic result.
            failed_round_dir = (
                self.run_dir / "validation" / f"round_{round_index}"
            )
            shutil.rmtree(failed_round_dir, ignore_errors=True)
            rounds = [
                (index, value)
                for index, value in rounds
                if index != round_index
            ]
            if not rounds:
                # Re-enter the no-validation-round recovery path. Existing
                # worker results are retained and validated before integration,
                # so this retries only the unavailable validator rather than
                # regenerating a potentially very large task.
                return self.resume()
            round_index, latest = max(rounds, key=lambda item: item[0])
            history = [value for _, value in sorted(rounds)]
        if latest.get("status") != "rework":
            raise ValueError("latest validation round is not resumable")
        if round_index >= self.max_rework_rounds:
            result = {
                "schema_version": 1,
                "status": "failed",
                "rounds": round_index,
                "validation_history": history,
                "summary": "已达到最大返工轮次，保留最新验证报告。",
            }
            write_json(self.run_dir / "pipeline_result.json", result)
            return result

        requests = self._requests_for_validation(latest, jobs_by_id)
        affected = [str(item) for item in latest.get("affected_jobs", [])]
        affected_set = set(affected)
        results_by_id: dict[str, dict[str, Json]] = {}
        for job_id, job in jobs_by_id.items():
            result_path = self.run_dir / "workers" / job_id / "worker_result.json"
            if not result_path.is_file():
                if job_id in affected_set:
                    continue
                raise ValueError(f"missing worker result for resume: {job_id}")
            result = read_json(result_path)
            changed = expand_worker_artifact_directories(
                result,
                work_dir=result_path.parent,
            )
            if repair_worker_artifact_paths(
                result,
                work_dir=result_path.parent,
            ):
                changed = True
            if normalize_worker_result_roles(
                result,
                work_dir=result_path.parent,
            ):
                changed = True
            if normalize_duplicate_target_paths(result):
                changed = True
            request_path = result_path.parent / "rework_request.json"
            if request_path.is_file():
                request = read_json(request_path)
                resolved = {
                    str(item)
                    for item in result.get("reworked_issue_ids", [])
                }
                before = set(resolved)
                resolved.update(
                    str(item)
                    for item in request.get("issue_ids", [])
                )
                if resolved != before:
                    result["reworked_issue_ids"] = sorted(resolved)
                    changed = True
            if changed:
                write_json(result_path, result)
            validate_worker_result(
                result,
                job=job,
                work_dir=result_path.parent,
            )
            results_by_id[job_id] = result

        pending_affected = []
        for job_id in affected:
            issue_ids = {
                str(item)
                for item in (requests.get(job_id) or {}).get("issue_ids", [])
            }
            resolved = {
                str(item)
                for item in results_by_id.get(job_id, {}).get(
                    "reworked_issue_ids", []
                )
            }
            if issue_ids and issue_ids.issubset(resolved):
                continue
            pending_affected.append(job_id)

        failures = self._run_rework_batch(
            plan=plan,
            jobs_by_id=jobs_by_id,
            results_by_id=results_by_id,
            job_ids=pending_affected,
            requests=requests,
            rework_round=round_index + 1,
        )
        if failures:
            write_json(
                self.run_dir / "validation" / f"round_{round_index}"
                / "rework_failures.json",
                {"schema_version": 1, "failed_jobs": failures},
            )
            result = {
                "schema_version": 1,
                "status": "rework_incomplete",
                "rounds": round_index,
                "failed_jobs": failures,
                "validation_history": history,
            }
            write_json(self.run_dir / "pipeline_result.json", result)
            return result

        for next_round in range(
            round_index + 1,
            self.max_rework_rounds + 1,
        ):
            worker_results = [
                results_by_id[str(job["job_id"])]
                for job in plan["file_jobs"]
            ]
            checks = self.integrate(
                plan=plan,
                worker_results=worker_results,
            )
            validation = self.validate(
                plan=plan,
                worker_results=worker_results,
                deterministic_checks=checks,
                round_index=next_round,
            )
            history.append(validation)
            status = str(validation.get("status") or "")
            if status == "passed":
                result = {
                    "schema_version": 1,
                    "status": (
                        "passed"
                        if checks.get("status") == "passed"
                        else "failed"
                    ),
                    "rounds": next_round,
                    "deterministic_errors": checks.get("errors", []),
                    "noiseless_jobs": checks.get("noiseless_jobs", []),
                    "validation_history": history,
                }
                write_json(self.run_dir / "pipeline_result.json", result)
                return result
            if status != "rework" or next_round >= self.max_rework_rounds:
                break
            next_affected = [
                str(job_id)
                for job_id in validation.get("affected_jobs", [])
            ]
            requests = self._requests_for_validation(
                validation, jobs_by_id
            )
            failures = self._run_rework_batch(
                plan=plan,
                jobs_by_id=jobs_by_id,
                results_by_id=results_by_id,
                job_ids=next_affected,
                requests=requests,
                rework_round=next_round + 1,
            )
            if failures:
                write_json(
                    self.run_dir / "validation" / f"round_{next_round}"
                    / "rework_failures.json",
                    {"schema_version": 1, "failed_jobs": failures},
                )
                result = {
                    "schema_version": 1,
                    "status": "rework_incomplete",
                    "rounds": next_round,
                    "failed_jobs": failures,
                    "validation_history": history,
                }
                write_json(self.run_dir / "pipeline_result.json", result)
                return result
        result = {
            "schema_version": 1,
            "status": "failed",
            "rounds": len(history) - 1,
            "validation_history": history,
        }
        write_json(self.run_dir / "pipeline_result.json", result)
        return result
