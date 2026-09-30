#!/usr/bin/env python3
"""Run one prepared config in a fresh task container with allowlist mounts."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import yaml

import run_isolated_benchmark as isolated


EXPECTED_IMAGE = "workspace-bench:local"
WYK_TASK_IDS = {"72", "75", "78", "79", "85", "87"}
FORWARDED_PROVIDER_ENV = {
    "APP_ID",
    "APP_KEY",
    "WS_MODEL_BASE_URL",
    "GPT56SOL_MODEL",
    "GPT56LUNA_MODEL",
    "GPT56TERRA_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
}
ENV_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def _referenced_environment_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, str):
        names.update(ENV_REFERENCE_RE.findall(value))
    elif isinstance(value, list):
        for item in value:
            names.update(_referenced_environment_names(item))
    elif isinstance(value, dict):
        for item in value.values():
            names.update(_referenced_environment_names(item))
    return names


def _read_metadata(task_root: Path, task_id: str) -> dict[str, object]:
    metadata_path = task_root / task_id / "metadata.json"
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid task metadata: {metadata_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"task metadata must be an object: {metadata_path}")
    return value


def _prepare_task_view(
    *,
    eval_root: Path,
    task_root: Path,
    task_id: str,
    view_token: str,
) -> tuple[Path, dict[str, object]]:
    metadata = _read_metadata(task_root, task_id)
    view_root = eval_root / ".generated" / "agent_task_views" / view_token
    if view_root.exists():
        shutil.rmtree(view_root)
    staged_task = view_root / task_id
    staged_task.mkdir(parents=True)
    source_root = (task_root / task_id).resolve()

    def copy_declared_path(raw_path: object, *, label: str) -> None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        source = (source_root / raw_path).resolve()
        try:
            relative = source.relative_to(source_root)
        except ValueError as exc:
            raise SystemExit(
                f"task {task_id} {label} path escapes source: {raw_path}"
            ) from exc
        if not source.exists():
            raise SystemExit(f"task {task_id} {label} source not found: {source}")
        destination = staged_task / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, destination)
        else:
            raise SystemExit(
                f"task {task_id} {label} source is not a regular file or directory"
            )

    manifest = metadata.get("data_manifest")
    if not isinstance(manifest, list):
        raise SystemExit(f"task {task_id} data_manifest must be a list")
    for index, item in enumerate(manifest):
        if not isinstance(item, dict):
            raise SystemExit(f"task {task_id} data_manifest[{index}] must be an object")
        stored_relpath = item.get("stored_relpath")
        if not isinstance(stored_relpath, str) or not stored_relpath.strip():
            raise SystemExit(
                f"task {task_id} data_manifest[{index}] is missing stored_relpath"
            )
        source = (source_root / stored_relpath).resolve()
        try:
            source.relative_to(source_root)
        except ValueError as exc:
            raise SystemExit(
                f"task {task_id} data_manifest path escapes source: {stored_relpath}"
            ) from exc
        if not source.is_file():
            raise SystemExit(
                f"task {task_id} data_manifest source not found: {source}"
            )

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
    (staged_task / "metadata.json").write_text(
        json.dumps(
            isolated._agent_visible_metadata(metadata),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return view_root, metadata


def _assert_wyk_sources_are_pdf_only(metadata: dict[str, object]) -> None:
    manifest = metadata.get("data_manifest")
    non_pdf = []
    for item in manifest if isinstance(manifest, list) else []:
        if isinstance(item, dict):
            # WYK variants embedded in a broader hard/noisy dataset may carry
            # non-PDF distractors.  The PDF-only invariant applies to the
            # authoritative standard inputs, not to intentionally
            # heterogeneous noise.  Legacy WYK metadata has no input_role, so
            # those entries continue to be checked.
            input_role = str(item.get("input_role") or "")
            if input_role and input_role != "standard":
                continue
            relpath = str(item.get("stored_relpath") or "")
            if Path(relpath).suffix.lower() != ".pdf":
                non_pdf.append(relpath)
    if non_pdf:
        raise SystemExit(
            "WYK data_manifest contains non-PDF inputs: " + ", ".join(non_pdf)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-config", required=True)
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-image-id")
    parser.add_argument("--dataset", default="tasks-hard-wyk")
    args = parser.parse_args()

    eval_root = Path(__file__).resolve().parents[1]
    repo_root = eval_root.parent
    compose_file = eval_root / "docker" / "docker-compose.yaml"
    config_path = Path(args.run_config).resolve()
    task_root = Path(args.task_root).resolve()
    task_id = str(args.task_id)
    view_token = f"{isolated._safe_name(task_id)}-{uuid.uuid4().hex[:12]}"

    image = subprocess.run(
        ["docker", "image", "inspect", "--format={{.Id}}", EXPECTED_IMAGE],
        text=True,
        capture_output=True,
        check=False,
    )
    if image.returncode != 0:
        raise SystemExit(image.stderr or image.stdout or "image inspection failed")
    image_digest = image.stdout.strip()
    if args.expected_image_id and image_digest != args.expected_image_id:
        raise SystemExit(
            f"expected Office image {args.expected_image_id}, got {image_digest}"
        )

    task_view, full_metadata = _prepare_task_view(
        eval_root=eval_root,
        task_root=task_root,
        task_id=task_id,
        view_token=view_token,
    )
    is_wyk_task = (
        args.dataset == "tasks-hard-wyk"
        or (args.dataset == "tasks-hard-all" and task_id in WYK_TASK_IDS)
    )
    if is_wyk_task:
        _assert_wyk_sources_are_pdf_only(full_metadata)

    bundle_root = None
    hidden_root = (
        eval_root / ".generated" / "agent_task_views" / "_hidden" / view_token
    )
    try:
        bundle_root, workspace_source, runs_root, config_root = (
            isolated._prepare_strict_run_view(
                eval_root=eval_root,
                config_path=config_path,
                metadata=full_metadata,
                source_task_dir=task_root / task_id,
                view_token=view_token,
            )
        )
        if is_wyk_task:
            isolated._assert_no_task_source_text_mirrors(
                source_task_dir=task_root / task_id,
                workspace_source=workspace_source,
            )
        _, hidden_results = isolated._prepare_container_output_mounts(
            hidden_root=hidden_root,
            runs_root=runs_root,
        )
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        resources = (
            config.get("task_resources")
            if isinstance(config, dict)
            and isinstance(config.get("task_resources"), dict)
            else isolated.DEFAULT_RESOURCES
        )
        env = dict(os.environ)
        env.update(
            {
                "WORKSPACE_BENCH_TASK_CPUS": str(resources.get("cpus") or "2"),
                "WORKSPACE_BENCH_TASK_MEMORY": (
                    f"{int(resources.get('memory_mb') or 8192)}m"
                ),
                "WORKSPACE_BENCH_TASK_MEMORY_MB": str(
                    int(resources.get("memory_mb") or 8192)
                ),
                "WORKSPACE_BENCH_TASK_PIDS": str(
                    int(resources.get("pids") or 512)
                ),
                "WORKSPACE_BENCH_TASK_STORAGE": (
                    f"{int(resources.get('storage_mb') or 20480)}m"
                ),
                "WORKSPACE_BENCH_TASK_STORAGE_MB": str(
                    int(resources.get("storage_mb") or 20480)
                ),
                "WORKSPACE_BENCH_TASK_TMPFS": (
                    f"{int(resources.get('storage_mb') or 20480)}m"
                ),
                "WORKSPACE_BENCH_TASK_STORAGE_QUOTA_MODE": "docker-layer",
            }
        )
        quota = isolated._storage_quota_available(
            compose_file=compose_file,
            cwd=eval_root,
            env=env,
        )
        if not quota:
            env["WORKSPACE_BENCH_TASK_STORAGE_QUOTA_MODE"] = (
                "case-directory-watchdog"
            )
        name = (
            f"workspace-bench-task-{isolated._safe_name(task_id)}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        service_env = {
            "WORKSPACE_BENCH_TASK_CONTAINER_NAME": name,
            "WORKSPACE_BENCH_TASK_IMAGE_DIGEST": image_digest,
        }
        provider_environment = FORWARDED_PROVIDER_ENV | _referenced_environment_names(
            config.get("api_provider")
            if isinstance(config, dict)
            else {}
        )
        service_env.update(
            {
                key: os.environ[key]
                for key in sorted(provider_environment)
                if key in os.environ
            }
        )
        command = isolated._compose_command(
            compose_file,
            "workspace-bench-task",
            [
                "python3",
                "-u",
                str(isolated.CONTAINER_EVAL_ROOT / "src/task_container_entry.py"),
                "--run-config",
                "/workspace/strict/config/run.yaml",
                "--task-id",
                task_id,
            ],
            compose_overrides=(
                [isolated._storage_quota_override(compose_file)] if quota else []
            ),
            container_name=name,
            service_env=service_env,
            volumes=isolated._runtime_allowlist_volumes(
                eval_root=eval_root,
                repo_root=repo_root,
            )
            + [
                (task_view, Path("/workspace/strict/tasks"), "ro"),
                (config_root, Path("/workspace/strict/config"), "ro"),
                (workspace_source, Path("/workspace/strict/workspace"), "ro"),
                (hidden_results, Path("/workspace/strict/results"), "ro"),
                (
                    runs_root,
                    Path("/workspace/strict/results") / runs_root.name,
                    "rw",
                ),
            ],
        )
        result = isolated._run(command, cwd=eval_root, env=env)
        return result.returncode
    finally:
        if bundle_root is not None:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            output_dir = isolated._host_path_from_container(
                eval_root,
                str(config.get("output_dir") or ""),
            )
            runs_root = output_dir / (
                f"{config.get('agent_name')}--"
                f"{config.get('model_name')}--"
                f"{config.get('run_name')}"
            )
            isolated._restore_evaluation_metadata(
                runs_root=runs_root,
                task_id=task_id,
                metadata=full_metadata,
                source_task_dir=task_root / task_id,
            )
        shutil.rmtree(task_view, ignore_errors=True)
        shutil.rmtree(hidden_root, ignore_errors=True)
        if bundle_root is not None:
            shutil.rmtree(bundle_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
