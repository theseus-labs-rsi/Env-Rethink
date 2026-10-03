#!/usr/bin/env python3
"""Host-side launcher for the reproducible per-task-container protocol.

The regular runner remains useful for local development.  This launcher is the
recommended evaluation path: it first materializes one pristine standard
workspace, then evaluates each selected task in a newly created and removed
``workspace-bench-task`` container.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml


Json = Any
DEFAULT_RESOURCES = {"cpus": "2", "memory_mb": 8192, "pids": 512, "storage_mb": 20480}
CONTAINER_REPO_ROOT = Path("/workspace/Workspace-Bench")
CONTAINER_EVAL_ROOT = CONTAINER_REPO_ROOT / "evaluation"
STORAGE_QUOTA_UNSUPPORTED_MARKERS = (
    "--storage-opt is supported only for overlay over xfs with 'pquota' mount option",
    "storage-opt is supported only for overlay over xfs with 'pquota' mount option",
    "storage-opt is not supported",
    "storage_opt is not supported",
)
EVALUATION_ONLY_METADATA_KEYS = {
    "file_dep_graph",
    "job",
    "noise_integration",
    "rubrics",
    "rubric_types",
    "judge_metadata",
    "ground_truth",
    "input_file_summary",
    "reference_output",
    "service_expectations",
    "task_diff",
    "tested_capabilities",
    "user_profit",
}
EVALUATION_ONLY_METADATA_PREFIXES = (
    "ground_truth_",
    "judge_",
    "noise_",
    "rubric_",
)
DATASET_TASK_ROOTS = {
    "smoke": "tasks_lite",
    "lite": "tasks_lite",
    "full": "tasks",
    "tasks-new": "tasks_new",
    "tasks-hard": "tasks_hard",
    "tasks-hard-v2": "tasks_hard_v2",
    "tasks-hard-wyk": "tasks_hard_wyk",
    "tasks-hard-all": "tasks_hard_all",
}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")[:48] or "task"


def _value_after(args: list[str], flag: str, default: str | None = None) -> str | None:
    for index, value in enumerate(args):
        if value == flag and index + 1 < len(args):
            return args[index + 1]
    return default


def _split_benchmark_args(args: list[str]) -> tuple[list[str], list[str], str | None, str]:
    """Remove selection and run-name flags, retaining all other runner flags."""
    base: list[str] = []
    requested_ids: list[str] = []
    persona: str | None = None
    run_name: str | None = None
    selection_flags = 0
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--task-ids":
            selection_flags += 1
            index += 1
            count_before = len(requested_ids)
            while index < len(args) and not args[index].startswith("--"):
                requested_ids.extend(part for part in args[index].split(",") if part)
                index += 1
            if len(requested_ids) == count_before:
                raise SystemExit("--task-ids requires at least one task id")
            continue
        if value in {"--task-limit", "--persona", "--run-name", "--task-parallel-workers"}:
            if index + 1 >= len(args):
                raise SystemExit(f"{value} requires a value")
            option_value = args[index + 1]
            if value == "--task-limit":
                selection_flags += 1
                requested_ids = [f"__limit__:{option_value}"]
            elif value == "--persona":
                selection_flags += 1
                persona = option_value
            elif value == "--run-name":
                run_name = option_value
            index += 2
            continue
        if value == "--no-task-parallel":
            index += 1
            continue
        base.append(value)
        index += 1

    dataset = str(_value_after(base, "--dataset", "lite") or "lite").strip().lower()
    if dataset not in DATASET_TASK_ROOTS:
        raise SystemExit(f"unsupported dataset: {dataset}")
    if selection_flags > 1:
        raise SystemExit("--task-limit, --task-ids, and --persona are mutually exclusive")
    return base, requested_ids, persona, run_name or {
        "smoke": "Smoke",
        "lite": "Lite",
        "full": "Full",
        "tasks-new": "Tasks-New",
        "tasks-hard": "Tasks-Hard",
        "tasks-hard-v2": "Tasks-Hard-V2",
        "tasks-hard-wyk": "Tasks-Hard-WYK",
        "tasks-hard-all": "Tasks-Hard-All",
    }[dataset]


def _selected_task_ids(eval_root: Path, *, dataset: str, requested: list[str], persona: str | None) -> list[str]:
    task_root_name = DATASET_TASK_ROOTS.get(dataset)
    if task_root_name is None:
        raise SystemExit(f"unsupported dataset: {dataset}")
    task_root = eval_root / task_root_name
    if not task_root.is_dir():
        raise SystemExit(f"task directory not found: {task_root}; download the selected dataset first")
    metadata: list[dict[str, Json]] = []
    for path in sorted(task_root.glob("*/metadata.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"invalid metadata: {path}: {exc}") from exc
        if isinstance(value, dict) and str(value.get("id") or "").strip():
            metadata.append(value)

    by_id = {str(item["id"]): item for item in metadata}
    if requested:
        if len(requested) == 1 and requested[0].startswith("__limit__:"):
            try:
                limit = max(0, int(requested[0].split(":", 1)[1]))
            except ValueError as exc:
                raise SystemExit("--task-limit must be an integer") from exc
            return [str(item["id"]) for item in metadata[:limit]]
        duplicates = sorted({item for item in requested if requested.count(item) > 1})
        missing = [item for item in requested if item not in by_id]
        if duplicates or missing:
            problem = []
            if duplicates:
                problem.append("duplicate task id(s): " + ", ".join(duplicates))
            if missing:
                problem.append("unknown task id(s): " + ", ".join(missing))
            raise SystemExit("; ".join(problem))
        return requested
    if persona is not None:
        selected = [str(item["id"]) for item in metadata if str(item.get("persona") or "") == persona]
        if not selected:
            raise SystemExit(f"no tasks for persona: {persona}")
        return selected
    return [str(item["id"]) for item in (metadata[:1] if dataset == "smoke" else metadata)]


def _compose_command(
    compose_file: Path,
    service: str,
    command: list[str],
    *,
    compose_overrides: list[Path] | None = None,
    container_name: str | None = None,
    service_env: dict[str, str] | None = None,
    volumes: list[tuple[Path, Path, str]] | None = None,
) -> list[str]:
    out = ["docker", "compose", "-f", str(compose_file)]
    for override in compose_overrides or []:
        out.extend(["-f", str(override)])
    out.extend(["run", "--rm", "--no-deps"])
    if container_name:
        out.extend(["--name", container_name])
    for key, value in sorted((service_env or {}).items()):
        out.extend(["-e", f"{key}={value}"])
    for source, destination, mode in volumes or []:
        out.extend(["-v", f"{source.resolve()}:{destination}:{mode}"])
    out.append(service)
    out.extend(command)
    return out


def _run(command: list[str], *, cwd: Path, env: dict[str, str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), env=env, text=True, check=False, capture_output=capture)


def _storage_quota_unsupported(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(marker.lower() in output for marker in STORAGE_QUOTA_UNSUPPORTED_MARKERS)


def _storage_quota_override(compose_file: Path) -> Path:
    return compose_file.with_name("docker-compose.storage-quota.yaml")


def _storage_quota_available(
    *,
    compose_file: Path,
    cwd: Path,
    env: dict[str, str],
) -> bool:
    """Probe Docker's task-layer quota support once before task execution.

    The regular task command keeps streaming its output after this preflight.
    If the host rejects ``storage_opt.size``, the launcher uses the same
    disposable task service without the quota override and relies on the
    independent case-directory watchdog.
    """
    quota_override = _storage_quota_override(compose_file)
    if not quota_override.is_file():
        return False
    command = _compose_command(
        compose_file,
        "workspace-bench-task",
        ["bash", "-lc", "true"],
        compose_overrides=[quota_override],
    )
    result = _run(command, cwd=cwd, env=env, capture=True)
    if result.returncode == 0:
        return True
    if _storage_quota_unsupported(result):
        print(
            "[warn] Docker storage_opt.size is unsupported on this host; "
            "using the task case-directory storage watchdog fallback.",
            file=sys.stderr,
        )
        return False
    raise SystemExit(
        result.stderr
        or result.stdout
        or "failed to probe Docker task storage quota support"
    )


def _agent_visible_metadata(metadata: dict[str, Json]) -> dict[str, Json]:
    """Remove fields reserved for post-run evaluation from an agent task view."""
    visible = {
        key: value
        for key, value in metadata.items()
        if key not in EVALUATION_ONLY_METADATA_KEYS
        and not key.startswith(EVALUATION_ONLY_METADATA_PREFIXES)
    }
    visible.pop("data_manifest", None)
    return visible


def _prepare_agent_task_view(
    eval_root: Path,
    *,
    dataset: str,
    task_id: str,
    view_token: str,
) -> tuple[Path, dict[str, Json]]:
    task_root_name = DATASET_TASK_ROOTS.get(dataset)
    if task_root_name is None:
        raise SystemExit(f"unsupported dataset: {dataset}")
    source_task_dir = eval_root / task_root_name / task_id
    metadata_path = source_task_dir / "metadata.json"
    if not source_task_dir.is_dir() or not metadata_path.is_file():
        raise SystemExit(f"task source not found: {source_task_dir}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid metadata: {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SystemExit(f"metadata must be an object: {metadata_path}")

    view_root = eval_root / ".generated" / "agent_task_views" / view_token
    if view_root.exists():
        shutil.rmtree(view_root)
    staged_task_dir = view_root / task_id
    staged_task_dir.mkdir(parents=True)
    source_task_resolved = source_task_dir.resolve()

    def copy_declared_path(raw_path: Json, *, label: str) -> None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        source = (source_task_dir / raw_path).resolve()
        try:
            relative = source.relative_to(source_task_resolved)
        except ValueError as exc:
            raise SystemExit(
                f"{label} path escapes task source: {raw_path}"
            ) from exc
        if not source.exists():
            raise SystemExit(f"{label} source not found: {source}")
        destination = staged_task_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, destination)
        else:
            raise SystemExit(f"{label} source is not a regular file or directory: {source}")

    workspace_services = metadata.get("workspace_services")
    if isinstance(workspace_services, dict):
        for service_name, service_config in workspace_services.items():
            if not isinstance(service_config, dict):
                continue
            copy_declared_path(
                service_config.get("fixture"),
                label=f"workspace_services.{service_name}.fixture",
            )
            copy_declared_path(
                service_config.get("blobs"),
                label=f"workspace_services.{service_name}.blobs",
            )
    (staged_task_dir / "metadata.json").write_text(
        json.dumps(_agent_visible_metadata(metadata), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return view_root, metadata


def _host_path_from_container(eval_root: Path, value: str) -> Path:
    """Translate repository paths emitted inside Docker back to host paths."""
    path = Path(str(value))
    try:
        relative = path.relative_to(CONTAINER_EVAL_ROOT)
    except ValueError:
        return path
    return eval_root / relative


def _selected_workspace_source(
    *,
    eval_root: Path,
    config: dict[str, Json],
    metadata: dict[str, Json],
) -> Path:
    fs_map_value = str(config.get("fs_map_file") or "").strip()
    if not fs_map_value:
        raise SystemExit("run config is missing fs_map_file")
    fs_map_path = _host_path_from_container(eval_root, fs_map_value)
    try:
        fs_map = json.loads(fs_map_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid fs map: {fs_map_path}: {exc}") from exc
    standard = fs_map.get("standard_work_dir") if isinstance(fs_map, dict) else None
    if not isinstance(standard, dict):
        raise SystemExit(f"fs map is missing standard_work_dir: {fs_map_path}")
    role = str(metadata.get("file_system") or "")
    raw_source = standard.get(role) if role in standard else standard.get("*")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise SystemExit(f"cannot resolve standard workspace for task role {role!r}")
    source = _host_path_from_container(eval_root, raw_source).resolve()
    if not source.is_dir():
        raise SystemExit(f"standard workspace directory not found: {source}")
    return source


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_task_source_text_mirrors(
    *,
    source_task_dir: Path,
    workspace_source: Path,
) -> None:
    """Reject exact task-source TXT mirrors in an exposed noisy workspace."""
    source_texts = [
        path
        for path in (source_task_dir / "data").rglob("*.txt")
        if path.is_file()
    ]
    if not source_texts:
        return
    source_names = {path.name for path in source_texts}
    source_sizes = {path.stat().st_size for path in source_texts}
    source_digests = {_file_digest(path) for path in source_texts}
    leaks: list[str] = []
    for root, dirs, files in os.walk(workspace_source, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not (Path(root) / name).is_symlink()
        ]
        for name in files:
            candidate = Path(root) / name
            if candidate.suffix.lower() != ".txt" or candidate.is_symlink():
                continue
            if candidate.name in source_names:
                leaks.append(str(candidate))
                continue
            try:
                if (
                    candidate.stat().st_size in source_sizes
                    and _file_digest(candidate) in source_digests
                ):
                    leaks.append(str(candidate))
            except OSError:
                continue
    if leaks:
        raise SystemExit(
            "workspace exposes task-source TXT mirror(s): "
            + ", ".join(sorted(set(leaks)))
        )


def _copy_workspace_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    copied = subprocess.run(
        [
            "cp",
            "-a",
            "--reflink=auto",
            "--no-preserve=ownership",
            f"{source}/.",
            str(destination),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if copied.returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(source, destination)
    readable = subprocess.run(
        ["chmod", "-R", "u+rwX,a+rX", str(destination)],
        text=True,
        capture_output=True,
        check=False,
    )
    if readable.returncode != 0:
        raise SystemExit(
            readable.stderr
            or readable.stdout
            or (
                "failed to make prepared task workspace writable/readable: "
                f"{destination}"
            )
        )


def _prepare_task_workspace_source(
    *,
    base_workspace: Path,
    source_task_dir: Path,
    metadata: dict[str, Json],
    destination: Path,
) -> Path:
    """Create a task-private merged baseline before the Agent container starts."""
    _copy_workspace_tree(base_workspace, destination)
    destination_resolved = destination.resolve()

    manifest_targets: set[Path] = set()
    manifest = metadata.get("data_manifest")
    if not isinstance(manifest, list):
        raise SystemExit("task data_manifest must be a list")
    for index, item in enumerate(manifest):
        if not isinstance(item, dict):
            raise SystemExit(f"data_manifest[{index}] must be an object")
        target_path = item.get("target_path")
        stored_relpath = item.get("stored_relpath")
        if not isinstance(target_path, str) or not target_path.strip():
            raise SystemExit(f"data_manifest[{index}] is missing target_path")
        if not isinstance(stored_relpath, str) or not stored_relpath.strip():
            raise SystemExit(f"data_manifest[{index}] is missing stored_relpath")
        target = (destination / target_path).resolve()
        source = (source_task_dir / stored_relpath).resolve()
        try:
            target.relative_to(destination_resolved)
            source.relative_to(source_task_dir.resolve())
        except ValueError as exc:
            raise SystemExit(
                f"data_manifest[{index}] path escapes task/workspace"
            ) from exc
        if not source.is_file():
            raise SystemExit(f"manifest source not found: {source}")
        manifest_targets.add(target)

    raw_remove_paths = metadata.get("input_remove_paths")
    for raw_path in raw_remove_paths if isinstance(raw_remove_paths, list) else []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        candidate = (destination / raw_path).resolve()
        try:
            candidate.relative_to(destination_resolved)
        except ValueError as exc:
            raise SystemExit(f"input_remove_paths escapes workspace: {raw_path}") from exc
        if candidate in manifest_targets:
            continue
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)

    for item in manifest:
        assert isinstance(item, dict)
        source = (source_task_dir / str(item["stored_relpath"])).resolve()
        target = (destination / str(item["target_path"])).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o444)
    return destination


def _prepare_strict_run_view(
    *,
    eval_root: Path,
    config_path: Path,
    metadata: dict[str, Json],
    source_task_dir: Path,
    view_token: str,
) -> tuple[Path, Path, Path, Path]:
    """Build the allowlisted config/workspace/output view for one task."""
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid run config: {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit(f"run config must be a mapping: {config_path}")

    base_workspace = _selected_workspace_source(
        eval_root=eval_root,
        config=config,
        metadata=metadata,
    )
    output_dir = _host_path_from_container(
        eval_root,
        str(config.get("output_dir") or ""),
    )
    runs_root = output_dir / (
        f"{config.get('agent_name')}--"
        f"{config.get('model_name')}--"
        f"{config.get('run_name')}"
    )

    bundle_root = eval_root / ".generated" / "strict_task_runs" / view_token
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    config_root = bundle_root / "config"
    config_root.mkdir(parents=True)
    workspace_source = _prepare_task_workspace_source(
        base_workspace=base_workspace,
        source_task_dir=source_task_dir,
        metadata=metadata,
        destination=bundle_root / "workspace",
    )
    strict_fs_map = {
        "raw_work_dir": {"*": "/workspace/strict/workspace"},
        "standard_work_dir": {"*": "/workspace/strict/workspace"},
        "work_dir": {"*": "/workspace/strict/workspace"},
    }
    (config_root / "fs_map.json").write_text(
        json.dumps(strict_fs_map, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    strict_config = dict(config)
    api_provider = (
        dict(strict_config.get("api_provider"))
        if isinstance(strict_config.get("api_provider"), dict)
        else {}
    )
    # The task container is already the security boundary: it is read-only,
    # drops all capabilities, and receives only explicit allowlist mounts.
    # Codex's workspace-write mode adds a nested bubblewrap namespace, which
    # is unavailable under the hardened container profile. Disable only that
    # redundant inner sandbox; this cannot expose paths not mounted by Docker.
    api_provider["codexSandboxMode"] = "danger-full-access"
    strict_config.update(
        {
            "task_path": "/workspace/strict/tasks",
            "task_ids": [str(metadata.get("id") or "")],
            "output_dir": "/workspace/strict/results",
            "fs_map_file": "/workspace/strict/config/fs_map.json",
            "task_parallel": False,
            "task_workdir_isolation": True,
            "task_workdir_materialization": "copy",
            "api_provider": api_provider,
        }
    )
    (config_root / "run.yaml").write_text(
        yaml.safe_dump(strict_config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return bundle_root, workspace_source, runs_root, config_root


def _runtime_allowlist_volumes(
    *,
    eval_root: Path,
    repo_root: Path,
) -> list[tuple[Path, Path, str]]:
    """Return code-only mounts required by the task runtime.

    The repository root itself is intentionally absent.  The tested agent may
    inspect runner code, but cannot traverse into datasets, archives, prior
    outputs, experiment artifacts, Git history, or developer configuration.
    """
    candidates = [
        (eval_root / "src", CONTAINER_EVAL_ROOT / "src"),
        (eval_root / "baselines", CONTAINER_EVAL_ROOT / "baselines"),
        (eval_root / "bin", CONTAINER_EVAL_ROOT / "bin"),
        (eval_root / "node_modules", CONTAINER_EVAL_ROOT / "node_modules"),
        (eval_root / "vendor", CONTAINER_EVAL_ROOT / "vendor"),
        (
            repo_root / "skills" / "email",
            CONTAINER_REPO_ROOT / "skills" / "email",
        ),
        (
            eval_root / "pyproject.toml",
            CONTAINER_EVAL_ROOT / "pyproject.toml",
        ),
        (eval_root / "uv.lock", CONTAINER_EVAL_ROOT / "uv.lock"),
        (
            eval_root / ".python-version",
            CONTAINER_EVAL_ROOT / ".python-version",
        ),
    ]
    return [(source, destination, "ro") for source, destination in candidates if source.exists()]


def _restore_evaluation_metadata(
    *,
    runs_root: Path,
    task_id: str,
    metadata: dict[str, Json],
    source_task_dir: Path | None = None,
) -> None:
    case_dir = runs_root / _safe_name(task_id)
    if not case_dir.is_dir():
        return
    if source_task_dir is not None:
        input_source = case_dir / "input_source"
        if input_source.exists():
            shutil.rmtree(input_source)
        input_source.mkdir(parents=True)
        manifest = metadata.get("data_manifest")
        for index, item in enumerate(manifest if isinstance(manifest, list) else []):
            if not isinstance(item, dict):
                continue
            stored_relpath = item.get("stored_relpath")
            if not isinstance(stored_relpath, str) or not stored_relpath.strip():
                continue
            source = (source_task_dir / stored_relpath).resolve()
            try:
                relative = source.relative_to(source_task_dir.resolve())
            except ValueError as exc:
                raise SystemExit(
                    f"data_manifest[{index}] escapes task source"
                ) from exc
            destination = input_source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        (input_source / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (case_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_container_output_mounts(
    *,
    hidden_root: Path,
    runs_root: Path,
) -> tuple[Path, Path]:
    """Create bind-mount sources before Docker applies the read-only mask."""
    empty_root = hidden_root / "empty"
    hidden_output_root = hidden_root / "output"
    empty_root.mkdir(parents=True, exist_ok=True)
    (hidden_output_root / runs_root.name).mkdir(parents=True, exist_ok=True)

    # The task container can run under a remapped UID (for example with
    # root-squash on the host filesystem). It only receives this run directory,
    # so making the bind source writable does not expose other run results.
    runs_root.mkdir(parents=True, exist_ok=True)
    runs_root.chmod(0o777)
    return empty_root, hidden_output_root


def _build_config(
    *, compose_file: Path, eval_root: Path, env: dict[str, str], base_args: list[str], task_id: str, run_name: str, resources: dict[str, Json]
) -> str:
    command = _compose_command(
        compose_file,
        "workspace-bench",
        [
            "python3",
            "/workspace/Workspace-Bench/evaluation/scripts/build_run_config.py",
            "--eval-root",
            "/workspace/Workspace-Bench/evaluation",
            *base_args,
            "--task-ids",
            task_id,
            "--run-name",
            run_name,
            "--no-task-parallel",
            "--task-isolation",
            "container",
            "--task-cpus",
            str(resources["cpus"]),
            "--task-memory-mb",
            str(resources["memory_mb"]),
            "--task-pids",
            str(resources["pids"]),
            "--task-storage-mb",
            str(resources["storage_mb"]),
        ],
    )
    result = _run(command, cwd=eval_root, env=env, capture=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or "failed to build isolated task config")
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not paths:
        raise SystemExit("build_run_config.py did not return a config path")
    return paths[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate each selected task in a fresh constrained Docker container.")
    parser.add_argument("--task-cpus", default=DEFAULT_RESOURCES["cpus"])
    parser.add_argument("--task-memory-mb", type=int, default=DEFAULT_RESOURCES["memory_mb"])
    parser.add_argument("--task-pids", type=int, default=DEFAULT_RESOURCES["pids"])
    parser.add_argument("--task-storage-mb", type=int, default=DEFAULT_RESOURCES["storage_mb"])
    # The benchmark runner has its own CLI.  Keep its flags opaque here so the
    # recommended command can remain natural (no mandatory `--` separator).
    args, raw_args = parser.parse_known_args()
    raw_args = list(raw_args)
    if raw_args[:1] == ["--"]:
        raw_args = raw_args[1:]
    if not raw_args:
        raw_args = ["--harness", "codex", "--model", "kimi-k2.5", "--dataset", "lite"]
    if not _value_after(raw_args, "--harness") or not _value_after(raw_args, "--model"):
        raise SystemExit("benchmark arguments must include --harness and --model")

    base_args, requested_ids, persona, run_name = _split_benchmark_args(raw_args)
    dataset = str(_value_after(base_args, "--dataset", "lite") or "lite").lower()
    eval_root = Path(__file__).resolve().parents[1]
    compose_file = eval_root / "docker" / "docker-compose.yaml"
    task_ids = _selected_task_ids(eval_root, dataset=dataset, requested=requested_ids, persona=persona)
    if not task_ids:
        raise SystemExit("task selection is empty")
    resources: dict[str, Json] = {
        "cpus": str(args.task_cpus),
        "memory_mb": max(1, int(args.task_memory_mb)),
        "pids": max(1, int(args.task_pids)),
        "storage_mb": max(1, int(args.task_storage_mb)),
    }
    env = dict(os.environ)
    env.update(
        {
            "WORKSPACE_BENCH_TASK_CPUS": str(resources["cpus"]),
            "WORKSPACE_BENCH_TASK_MEMORY": f"{resources['memory_mb']}m",
            "WORKSPACE_BENCH_TASK_PIDS": str(resources["pids"]),
            "WORKSPACE_BENCH_TASK_STORAGE": f"{resources['storage_mb']}m",
            "WORKSPACE_BENCH_TASK_STORAGE_MB": str(resources["storage_mb"]),
            "WORKSPACE_BENCH_TASK_TMPFS": f"{resources['storage_mb']}m",
            "WORKSPACE_BENCH_TASK_STORAGE_QUOTA_MODE": "docker-layer",
        }
    )

    configs = [
        _build_config(
            compose_file=compose_file,
            eval_root=eval_root,
            env=env,
            base_args=base_args,
            task_id=task_id,
            run_name=run_name,
            resources=resources,
        )
        for task_id in task_ids
    ]
    prepare = _compose_command(
        compose_file,
        "workspace-bench",
        ["python3", "/workspace/Workspace-Bench/evaluation/scripts/prepare_workdirs_for_run.py", "--run-config", configs[0]],
    )
    prepared = _run(prepare, cwd=eval_root, env=env)
    if prepared.returncode != 0:
        return prepared.returncode

    storage_quota_enforced = _storage_quota_available(
        compose_file=compose_file,
        cwd=eval_root,
        env=env,
    )

    image = _run(
        ["docker", "image", "inspect", "--format={{.Id}}", "workspace-bench:local"],
        cwd=eval_root,
        env=env,
        capture=True,
    )
    image_digest = image.stdout.strip() if image.returncode == 0 else ""

    failures = 0
    for task_id, config_path in zip(task_ids, configs):
        name = f"workspace-bench-task-{_safe_name(task_id)}-{uuid.uuid4().hex[:8]}"
        view_token = f"{_safe_name(task_id)}-{uuid.uuid4().hex[:12]}"
        agent_task_view, evaluation_metadata = _prepare_agent_task_view(
            eval_root,
            dataset=dataset,
            task_id=task_id,
            view_token=view_token,
        )
        hidden_root = eval_root / ".generated" / "agent_task_views" / "_hidden" / view_token
        config = _host_path_from_container(eval_root, config_path)
        config_value = yaml.safe_load(config.read_text(encoding="utf-8"))
        if not isinstance(config_value, dict):
            raise SystemExit(f"invalid run config: {config}")
        bundle_root, workspace_source, runs_root, config_root = (
            _prepare_strict_run_view(
                eval_root=eval_root,
                config_path=config,
                metadata=evaluation_metadata,
                source_task_dir=(
                    eval_root / DATASET_TASK_ROOTS[dataset] / task_id
                ),
                view_token=view_token,
            )
        )
        if dataset == "tasks-hard-wyk":
            _assert_no_task_source_text_mirrors(
                source_task_dir=eval_root / DATASET_TASK_ROOTS[dataset] / task_id,
                workspace_source=workspace_source,
            )
        empty_root, hidden_output_root = _prepare_container_output_mounts(
            hidden_root=hidden_root,
            runs_root=runs_root,
        )
        repo_root = eval_root.parent
        task_env = dict(env)
        task_env["WORKSPACE_BENCH_TASK_CONTAINER_NAME"] = name
        if not storage_quota_enforced:
            task_env["WORKSPACE_BENCH_TASK_STORAGE_QUOTA_MODE"] = "case-directory-watchdog"
        command = _compose_command(
            compose_file,
            "workspace-bench-task",
            [
                "python3",
                "-u",
                "/workspace/Workspace-Bench/evaluation/src/task_container_entry.py",
                "--run-config",
                "/workspace/strict/config/run.yaml",
                "--task-id",
                task_id,
            ],
            compose_overrides=[_storage_quota_override(compose_file)] if storage_quota_enforced else [],
            container_name=name,
            service_env={
                "WORKSPACE_BENCH_TASK_CONTAINER_NAME": name,
                "WORKSPACE_BENCH_TASK_IMAGE_DIGEST": image_digest,
            },
            volumes=_runtime_allowlist_volumes(
                eval_root=eval_root,
                repo_root=repo_root,
            )
            + [
                (
                    agent_task_view,
                    Path("/workspace/strict/tasks"),
                    "ro",
                ),
                (
                    config_root,
                    Path("/workspace/strict/config"),
                    "ro",
                ),
                (
                    workspace_source,
                    Path("/workspace/strict/workspace"),
                    "ro",
                ),
                (
                    hidden_output_root,
                    Path("/workspace/strict/results"),
                    "ro",
                ),
                (
                    runs_root,
                    Path("/workspace/strict/results") / runs_root.name,
                    "rw",
                ),
            ],
        )
        try:
            result = _run(command, cwd=eval_root, env=task_env)
            if result.returncode != 0:
                failures += 1
        finally:
            _restore_evaluation_metadata(
                runs_root=runs_root,
                task_id=task_id,
                metadata=evaluation_metadata,
                source_task_dir=(
                    eval_root / DATASET_TASK_ROOTS[dataset] / task_id
                ),
            )
            shutil.rmtree(agent_task_view, ignore_errors=True)
            shutil.rmtree(hidden_root, ignore_errors=True)
            shutil.rmtree(bundle_root, ignore_errors=True)

    aggregate = _compose_command(
        compose_file,
        "workspace-bench",
        [
            "python3",
            "/workspace/Workspace-Bench/evaluation/scripts/aggregate_isolated_run.py",
            "--run-config",
            configs[0],
            "--task-ids",
            *task_ids,
        ],
    )
    aggregate_result = _run(aggregate, cwd=eval_root, env=env)
    return aggregate_result.returncode if aggregate_result.returncode != 0 else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())
