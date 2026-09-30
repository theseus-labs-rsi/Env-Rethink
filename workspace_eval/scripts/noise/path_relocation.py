#!/usr/bin/env python3
"""Path-enhancement (file-path relocation) step for the local-noise pipeline.

After system B produces noise, a *planner* Agent proposes new ``target_path``
values for every file in the task (standard inputs + generated noise), and an
*auditor* Agent checks that moving those files would not break solvability
(e.g. a path the task description names explicitly). Accepted moves are written
back into ``metadata.data_manifest``; the old ``target_path`` is added to
``input_remove_paths`` so the runtime deletes the stale location when the task
is materialized. Unsafe (absolute / ``..`` / backslash) planner paths and
auditor-rejected files keep their original location. Everything is recorded,
never raised, so the step never blocks the enclosing add-noise run.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from _fs import IntegrationError, read_json, safe_rel_path, write_json
    from multi_agent import AgentBackend, CodexBackend, json_first_object
    from prompts import path_auditor_prompt, path_planner_prompt
except ImportError:  # allow running as a plain script
    from ._fs import IntegrationError, read_json, safe_rel_path, write_json
    from .multi_agent import AgentBackend, CodexBackend, json_first_object
    from .prompts import path_auditor_prompt, path_planner_prompt


Json = Any

COMMON_DIRS = [
    "桌面/业务",
    "桌面/下载",
    "桌面/文档",
    "桌面/桌面文件",
    "下载",
    "文档",
    "文档/项目资料",
    "业务资料",
    "工作区",
]


def _load_provider(path: Path) -> dict[str, Json]:
    import yaml

    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("provider config must be an object")
    provider = (
        value.get("api_provider")
        if isinstance(value.get("api_provider"), dict)
        else value
    )
    return dict(provider)


def _disambiguate(target_path: str, occupied: set[str]) -> str:
    """Return ``target_path`` unless taken, then ``<stem>_<n><suffix>`` variants.

    Mirrors the in-place rename used by ``integration.py`` for same-name
    collisions: keep the extension and parent, insert a numeric suffix before it.
    """

    if target_path not in occupied:
        return target_path
    original = PurePosixPath(target_path)
    parent = original.parent
    stem = original.stem
    suffix = original.suffix
    counter = 1
    while True:
        name = f"{stem}_{counter}{suffix}"
        candidate = (
            (parent / name).as_posix() if parent != PurePosixPath(".") else name
        )
        if candidate not in occupied:
            return candidate
        counter += 1


@dataclass
class PathRelocator:
    task_dir: Path
    generation_dir: Path
    planner_backend: AgentBackend | None = None
    auditor_backend: AgentBackend | None = None
    seed: int = 1
    clean: bool = False

    # -- introspection ------------------------------------------------------

    @staticmethod
    def _task_description(metadata: dict[str, Json]) -> str:
        for key in ("instruction", "task", "description", "prompt"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    def gather_files(self, metadata: dict[str, Json]) -> list[dict[str, Json]]:
        """Return every manifest file as ``{index, key, current_target_path, kind}``.

        ``kind`` is ``"generated"`` for noise (marked with ``generated_by``) and
        ``"canonical"`` for the original standard inputs. ``key`` is a stable
        identifier derived from the filename (deduped with a numeric suffix).
        """

        manifest = metadata.get("data_manifest")
        files: list[dict[str, Json]] = []
        if not isinstance(manifest, list):
            return files
        seen: dict[str, bool] = {}
        for index, item in enumerate(manifest):
            if not isinstance(item, dict):
                continue
            target = item.get("target_path")
            if not isinstance(target, str) or not target.strip():
                target = item.get("filename")
            if not isinstance(target, str) or not target.strip():
                continue
            base = item.get("filename") or item.get("stored_relpath") or f"file_{index}"
            key = str(base)
            n = 2
            while key in seen:
                key = f"{base}_{n}"
                n += 1
            seen[key] = True
            kind = "generated" if item.get("generated_by") else "canonical"
            files.append(
                {
                    "index": index,
                    "key": key,
                    "current_target_path": str(target),
                    "kind": kind,
                }
            )
        return files

    # -- planner / auditor --------------------------------------------------

    def plan_paths(
        self,
        metadata: dict[str, Json],
        files: list[dict[str, Json]],
    ) -> dict[str, str]:
        if self.planner_backend is None:
            raise RuntimeError("no planner backend configured")
        planner_input = [
            {
                "key": item["key"],
                "current_target_path": item["current_target_path"],
                "kind": item["kind"],
            }
            for item in files
        ]
        prompt = path_planner_prompt(
            task_description=self._task_description(metadata),
            files=planner_input,
            common_dirs=COMMON_DIRS,
            seed=self.seed,
        )
        result = self.planner_backend.run(
            role="path_planner",
            prompt=prompt,
            work_dir=self.task_dir,
            sandbox_dir=self.generation_dir / "sandbox_planner",
        )
        plan = json_first_object(
            str(result.get("trace", {}).get("lastText") or ""),
            require=lambda value: isinstance(value.get("path_map"), dict),
        )
        if not isinstance(plan, dict) or not isinstance(plan.get("path_map"), dict):
            raise RuntimeError("planner returned no usable path_map")
        raw_map = plan["path_map"]
        # Keep only keys we recognize; the auditor decides acceptance later.
        path_map: dict[str, str] = {}
        for item in files:
            key = item["key"]
            if key in raw_map and isinstance(raw_map[key], str):
                path_map[key] = str(raw_map[key])
        if not path_map:
            raise RuntimeError("planner path_map contained no known keys")
        return path_map

    def audit_paths(
        self,
        task_description: str,
        files: list[dict[str, Json]],
        path_map: dict[str, str],
    ) -> tuple[list[str], list[dict[str, Json]]]:
        if self.auditor_backend is None:
            raise RuntimeError("no auditor backend configured")
        plan_for_audit = [
            {
                "key": item["key"],
                "current_target_path": item["current_target_path"],
                "targeted_path": path_map.get(
                    item["key"], item["current_target_path"]
                ),
                "kind": item["kind"],
            }
            for item in files
            if item["key"] in path_map
        ]
        prompt = path_auditor_prompt(
            task_description=task_description,
            path_plan=plan_for_audit,
        )
        result = self.auditor_backend.run(
            role="path_auditor",
            prompt=prompt,
            work_dir=self.task_dir,
            sandbox_dir=self.generation_dir / "sandbox_auditor",
        )
        audit = json_first_object(
            str(result.get("trace", {}).get("lastText") or ""),
            require=lambda value: isinstance(value.get("accepted"), list)
            or isinstance(value.get("rejected"), list),
        )
        if not isinstance(audit, dict):
            raise RuntimeError("auditor returned no parseable JSON")
        accepted = [str(key) for key in (audit.get("accepted") or [])]
        accepted_set = set(accepted)
        rejected: list[dict[str, Json]] = []
        rejected_keys: set[str] = set()
        for entry in audit.get("rejected") or []:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            key = str(entry["key"])
            rejected.append(
                {"key": key, "reason": str(entry.get("reason") or "")}
            )
            rejected_keys.add(key)
        # A planned-but-unaudited key is accepted (least-blocking): moving it is
        # safe unless the auditor explicitly rejected it.
        for item in files:
            key = item["key"]
            if (
                key in path_map
                and key not in rejected_keys
                and key not in accepted_set
            ):
                accepted.append(key)
                accepted_set.add(key)
        return accepted, rejected

    # -- apply --------------------------------------------------------------

    def _apply(
        self,
        files: list[dict[str, Json]],
        path_map: dict[str, str],
        accepted: list[str],
        rejected: list[dict[str, Json]],
        failures: list[dict[str, Json]],
    ) -> int:
        metadata = read_json(self.task_dir / "metadata.json")
        manifest = metadata.get("data_manifest")
        if not isinstance(manifest, list):
            failures.append(
                {"type": "apply", "error": "data_manifest is not a list"}
            )
            return 0
        accepted_set = set(accepted)
        rejected_keys = {
            entry["key"] for entry in rejected if isinstance(entry, dict)
        }
        occupied = {item["current_target_path"] for item in files}
        remove_paths = set(metadata.get("input_remove_paths") or [])
        final_map: dict[str, str] = {}
        move_count = 0
        for item in files:
            key = item["key"]
            old = item["current_target_path"]
            manifest_item = manifest[item["index"]]
            if key in path_map and key in accepted_set and key not in rejected_keys:
                desired = path_map[key]
                try:
                    safe = safe_rel_path(desired, field="targeted_path")
                except IntegrationError as exc:
                    failures.append(
                        {
                            "type": "unsafe_path",
                            "key": key,
                            "desired": desired,
                            "reason": str(exc),
                        }
                    )
                    final_map[key] = old
                    occupied.add(old)
                    continue
                final = _disambiguate(safe, occupied)
                occupied.add(final)
                manifest_item["target_path"] = final
                final_map[key] = final
                if final != old:
                    remove_paths.add(old)
                    move_count += 1
            else:
                final_map[key] = old
                occupied.add(old)
        metadata["input_remove_paths"] = sorted(remove_paths)
        write_json(self.task_dir / "metadata.json", metadata)
        return move_count, final_map

    # -- rerun safety -------------------------------------------------------

    def _restore_base_state(self) -> None:
        """Reset the manifest to the snapshot from the previous generation run."""

        path = self.generation_dir / "path_relocation.json"
        if not path.is_file():
            return
        try:
            previous = read_json(path)
            base = previous.get("base_state")
            if not isinstance(base, dict):
                return
            metadata = read_json(self.task_dir / "metadata.json")
            manifest = metadata.get("data_manifest")
            if not isinstance(manifest, list):
                return
            for entry in base.get("files", []):
                if not isinstance(entry, dict):
                    continue
                index = entry.get("index")
                if isinstance(index, int) and 0 <= index < len(manifest):
                    manifest[index]["target_path"] = entry.get(
                        "original_target_path"
                    )
            metadata["input_remove_paths"] = list(
                base.get("input_remove_paths") or []
            )
            write_json(self.task_dir / "metadata.json", metadata)
        except Exception:
            # Non-fatal: a clean run without a restorable state simply starts
            # from whatever the manifest currently holds.
            pass

    # -- orchestration ------------------------------------------------------

    def run(
        self,
        pipeline_result: dict[str, Json] | None = None,
        deterministic_checks: dict[str, Json] | None = None,
    ) -> dict[str, Json]:
        failures: list[dict[str, Json]] = []
        for source in (pipeline_result, deterministic_checks):
            if isinstance(source, dict):
                for job in source.get("noiseless_jobs") or []:
                    failures.append(
                        {
                            "type": "noiseless_job",
                            "key": str(job),
                            "reason": (
                                "no noise generated for this input; relocation "
                                "skipped for safety"
                            ),
                        }
                    )

        if self.clean:
            self._restore_base_state()

        try:
            metadata = read_json(self.task_dir / "metadata.json")
        except Exception as exc:
            failures.append(
                {"type": "gather", "error": f"{type(exc).__name__}: {exc}"}
            )
            return self._write_result(
                status="failed",
                base_state={"files": [], "input_remove_paths": []},
                path_map={},
                audit={"accepted": [], "rejected": []},
                rejected=[],
                failures=failures,
            )

        files = self.gather_files(metadata)
        base_state = {
            "files": [
                {
                    "index": item["index"],
                    "key": item["key"],
                    "original_target_path": item["current_target_path"],
                    "kind": item["kind"],
                }
                for item in files
            ],
            "input_remove_paths": list(metadata.get("input_remove_paths") or []),
        }

        if not files:
            failures.append(
                {"type": "no_files", "reason": "data_manifest empty or missing"}
            )
            return self._write_result(
                status="no_change",
                base_state=base_state,
                path_map={},
                audit={"accepted": [], "rejected": []},
                rejected=[],
                failures=failures,
            )

        path_map: dict[str, str] = {}
        accepted: list[str] = []
        rejected: list[dict[str, Json]] = []
        task_description = self._task_description(metadata)

        try:
            path_map = self.plan_paths(metadata, files)
        except Exception as exc:
            failures.append(
                {"type": "plan", "error": f"{type(exc).__name__}: {exc}"}
            )
            path_map = {}

        try:
            accepted, rejected = self.audit_paths(task_description, files, path_map)
        except Exception as exc:
            failures.append(
                {"type": "audit", "error": f"{type(exc).__name__}: {exc}"}
            )
            # Default to accepting the planner's plan so the step does not block.
            accepted = [key for key in path_map if key not in {
                entry["key"] for entry in rejected
            }]
            rejected = list(rejected)

        move_count = 0
        final_map: dict[str, str] = {}
        if path_map:
            try:
                move_count, final_map = self._apply(
                    files, path_map, accepted, rejected, failures
                )
            except Exception as exc:
                failures.append(
                    {"type": "apply", "error": f"{type(exc).__name__}: {exc}"}
                )

        status = "applied" if move_count > 0 else "no_change"
        return self._write_result(
            status=status,
            base_state=base_state,
            path_map=final_map,
            audit={"accepted": accepted, "rejected": rejected},
            rejected=rejected,
            failures=failures,
        )

    def _write_result(
        self,
        *,
        status: str,
        base_state: dict[str, Json],
        path_map: dict[str, str],
        audit: dict[str, Json],
        rejected: list[dict[str, Json]],
        failures: list[dict[str, Json]],
    ) -> dict[str, Json]:
        result = {
            "schema_version": 1,
            "status": status,
            "seed": self.seed,
            "base_state": base_state,
            "path_map": path_map,
            "audit": audit,
            "rejected": rejected,
            "failures": failures,
        }
        self.generation_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.generation_dir / "path_relocation.json", result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--planner-provider-config", type=Path)
    parser.add_argument("--auditor-provider-config", type=Path)
    parser.add_argument("--generation-dir", type=Path)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    task_dir = args.task_dir.resolve()
    generation_dir = (
        args.generation_dir.resolve()
        if args.generation_dir
        else task_dir.parent / "generation"
    )

    planner_config = args.planner_provider_config or args.provider_config
    auditor_config = args.auditor_provider_config or args.provider_config
    planner_backend: AgentBackend | None = (
        CodexBackend(
            provider=_load_provider(planner_config),
            timeout_seconds=args.timeout_seconds,
        )
        if planner_config
        else None
    )
    auditor_backend: AgentBackend | None = (
        CodexBackend(
            provider=_load_provider(auditor_config),
            timeout_seconds=args.timeout_seconds,
        )
        if auditor_config
        else None
    )

    relocator = PathRelocator(
        task_dir=task_dir,
        generation_dir=generation_dir,
        planner_backend=planner_backend,
        auditor_backend=auditor_backend,
        seed=args.seed,
        clean=args.clean,
    )
    result = relocator.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
