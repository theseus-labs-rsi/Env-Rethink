from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

try:
    from ._fs import sha256_file
except ImportError:
    from _fs import sha256_file


DEFAULT_COMMON_DIRECTORIES = (
    "桌面",
    "下载",
    "文档",
    "归档",
    "共享",
    "Desktop",
    "Downloads",
    "Documents",
    "Archive",
    "Shared",
)
REPORT_FILENAMES = {
    "subset_manifest.json",
    "source_path_map.json",
    "build_report.json",
}


class WorkspaceSubsetError(RuntimeError):
    """Raised when a deterministic workspace subset cannot be built."""


@dataclass(frozen=True)
class SubsetBudget:
    max_files: int = 500
    max_bytes: int = 512 * 1024 * 1024
    common_dir_files: int = 20

    def __post_init__(self) -> None:
        if self.max_files <= 0:
            raise ValueError("max_files must be positive")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.common_dir_files < 0:
            raise ValueError("common_dir_files must be non-negative")


@dataclass(frozen=True)
class SourceMatch:
    manifest_index: int
    filename: str
    stored_relpath: str
    target_path: str | None
    sha256: str
    matching_paths: tuple[str, ...]
    selected_path: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_index": self.manifest_index,
            "filename": self.filename,
            "stored_relpath": self.stored_relpath,
            "target_path": self.target_path,
            "sha256": self.sha256,
            "matching_paths": list(self.matching_paths),
            "selected_path": self.selected_path,
            "status": "matched" if self.selected_path else "unmatched",
        }


def load_metadata(metadata_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceSubsetError(
            f"cannot read task metadata {metadata_path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise WorkspaceSubsetError("task metadata must be a JSON object")
    if not isinstance(value.get("data_manifest"), list):
        raise WorkspaceSubsetError("task metadata.data_manifest must be a list")
    return value


def _safe_relative_path(value: str, *, field: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise WorkspaceSubsetError(f"unsafe {field}: {value!r}")
    return path


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_files(root: Path) -> Iterable[Path]:
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not (Path(current_root) / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(current_root) / filename
            if path.is_file() and not path.is_symlink():
                yield path


def index_workspace_by_filename(
    raw_workspace: Path,
    filenames: Iterable[str] | None = None,
) -> dict[str, list[Path]]:
    wanted = set(filenames) if filenames is not None else None
    index: dict[str, list[Path]] = defaultdict(list)
    for path in _iter_files(raw_workspace):
        if wanted is None or path.name in wanted:
            index[path.name].append(path)
    return {name: paths for name, paths in sorted(index.items())}


def _suffix_similarity(left: PurePosixPath, right: PurePosixPath) -> int:
    score = 0
    for left_part, right_part in zip(
        reversed(left.parts), reversed(right.parts)
    ):
        if left_part != right_part:
            break
        score += 1
    return score


def _select_source_path(
    candidates: Sequence[str],
    target_path: str | None,
) -> str | None:
    if not candidates:
        return None
    if not target_path:
        return min(candidates)
    target = _safe_relative_path(target_path, field="target_path")
    if target.as_posix() in candidates:
        return target.as_posix()
    return min(
        candidates,
        key=lambda candidate: (
            -_suffix_similarity(PurePosixPath(candidate), target),
            len(PurePosixPath(candidate).parts),
            candidate,
        ),
    )


def match_manifest_sources(
    task_dir: Path,
    metadata: Mapping[str, Any],
    raw_workspace: Path,
    *,
    include_generated: bool = False,
) -> list[SourceMatch]:
    manifest = metadata.get("data_manifest")
    if not isinstance(manifest, list):
        raise WorkspaceSubsetError("task metadata.data_manifest must be a list")

    eligible: list[tuple[int, Mapping[str, Any]]] = []
    filenames: set[str] = set()
    for index, raw_item in enumerate(manifest):
        if not isinstance(raw_item, Mapping):
            raise WorkspaceSubsetError(
                f"data_manifest[{index}] must be an object"
            )
        if not include_generated and (
            raw_item.get("generated_by") or raw_item.get("noise_kind")
        ):
            continue
        filename = raw_item.get("filename")
        stored_relpath = raw_item.get("stored_relpath")
        if not isinstance(filename, str) or not filename:
            raise WorkspaceSubsetError(
                f"data_manifest[{index}].filename must be a non-empty string"
            )
        if not isinstance(stored_relpath, str) or not stored_relpath:
            raise WorkspaceSubsetError(
                "data_manifest"
                f"[{index}].stored_relpath must be a non-empty string"
            )
        eligible.append((index, raw_item))
        filenames.add(filename)

    filename_index = index_workspace_by_filename(raw_workspace, filenames)
    hash_cache: dict[Path, str] = {}
    matches: list[SourceMatch] = []
    for index, item in eligible:
        filename = str(item["filename"])
        stored_relpath = str(item["stored_relpath"])
        stored_path = task_dir.joinpath(
            *_safe_relative_path(
                stored_relpath, field=f"data_manifest[{index}].stored_relpath"
            ).parts
        )
        if not stored_path.is_file():
            raise WorkspaceSubsetError(
                f"stored input does not exist: {stored_path}"
            )
        source_hash = sha256_file(stored_path)
        matching_paths: list[str] = []
        for candidate in filename_index.get(filename, []):
            candidate_hash = hash_cache.get(candidate)
            if candidate_hash is None:
                candidate_hash = sha256_file(candidate)
                hash_cache[candidate] = candidate_hash
            if candidate_hash == source_hash:
                matching_paths.append(
                    _relative_posix(candidate, raw_workspace)
                )
        matching_paths.sort()
        target_path = item.get("target_path")
        if target_path is not None and not isinstance(target_path, str):
            raise WorkspaceSubsetError(
                f"data_manifest[{index}].target_path must be a string"
            )
        matches.append(
            SourceMatch(
                manifest_index=index,
                filename=filename,
                stored_relpath=stored_relpath,
                target_path=target_path,
                sha256=source_hash,
                matching_paths=tuple(matching_paths),
                selected_path=_select_source_path(
                    matching_paths, target_path
                ),
            )
        )
    return matches


def _common_ancestor(paths: Sequence[PurePosixPath]) -> PurePosixPath:
    if not paths:
        return PurePosixPath(".")
    common_parts = list(paths[0].parts)
    for path in paths[1:]:
        matching_prefix_length = 0
        for left, right in zip(common_parts, path.parts):
            if left != right:
                break
            matching_prefix_length += 1
        common_parts = common_parts[:matching_prefix_length]
    return (
        PurePosixPath(*common_parts)
        if common_parts
        else PurePosixPath(".")
    )


def derive_local_roots(
    selected_paths: Iterable[str],
    *,
    min_root_depth: int = 2,
) -> tuple[list[str], bool]:
    if min_root_depth < 1:
        raise ValueError("min_root_depth must be at least 1")
    parents = sorted(
        {
            PurePosixPath(path).parent
            for path in selected_paths
            if path
        },
        key=lambda path: path.as_posix(),
    )
    if not parents:
        return [], False

    common = _common_ancestor(parents)
    common_depth = 0 if common == PurePosixPath(".") else len(common.parts)
    if common_depth >= min_root_depth:
        return [common.as_posix()], False

    groups: dict[tuple[str, ...], list[PurePosixPath]] = defaultdict(list)
    for parent in parents:
        depth = min(min_root_depth, len(parent.parts))
        key = tuple(parent.parts[:depth])
        groups[key].append(parent)

    roots: list[PurePosixPath] = []
    for key in sorted(groups):
        local_common = _common_ancestor(groups[key])
        if local_common == PurePosixPath("."):
            local_common = PurePosixPath(*key)
        roots.append(local_common)

    reduced: list[PurePosixPath] = []
    for root in sorted(roots, key=lambda item: (len(item.parts), item.as_posix())):
        if any(
            root == existing or existing in root.parents
            for existing in reduced
        ):
            continue
        reduced.append(root)
    return [root.as_posix() for root in reduced], True


def _path_distance(left: PurePosixPath, right: PurePosixPath) -> int:
    common = _common_ancestor([left, right])
    common_depth = 0 if common == PurePosixPath(".") else len(common.parts)
    return len(left.parts) + len(right.parts) - 2 * common_depth


def _is_under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _candidate_files_under(
    raw_workspace: Path,
    roots: Iterable[str],
) -> set[Path]:
    files: set[Path] = set()
    for raw_root in roots:
        root = raw_workspace.joinpath(*PurePosixPath(raw_root).parts)
        if root.is_file() and not root.is_symlink():
            files.add(root)
        elif root.is_dir() and not root.is_symlink():
            files.update(_iter_files(root))
    return files


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _prepare_output_root(output_root: Path, clean: bool) -> None:
    if output_root.exists() and clean:
        shutil.rmtree(output_root)
    elif output_root.exists() and any(output_root.iterdir()):
        raise WorkspaceSubsetError(
            f"output root is not empty (use clean=True): {output_root}"
        )
    output_root.mkdir(parents=True, exist_ok=True)


def build_workspace_subset(
    *,
    task_dir: Path,
    raw_workspace: Path,
    output_root: Path,
    metadata_path: Path | None = None,
    budget: SubsetBudget | None = None,
    min_root_depth: int = 2,
    common_directories: Sequence[str] = DEFAULT_COMMON_DIRECTORIES,
    include_generated: bool = False,
    strict_matches: bool = True,
    clean: bool = False,
) -> dict[str, Any]:
    """Build a deterministic, budget-limited subset of a role workspace."""

    task_dir = task_dir.resolve()
    raw_workspace = raw_workspace.resolve()
    output_root = output_root.resolve()
    metadata_path = (metadata_path or task_dir / "metadata.json").resolve()
    budget = budget or SubsetBudget()

    if not raw_workspace.is_dir():
        raise WorkspaceSubsetError(
            f"raw workspace is not a directory: {raw_workspace}"
        )
    if output_root == raw_workspace or raw_workspace in output_root.parents:
        raise WorkspaceSubsetError(
            "output root must not be inside the raw workspace"
        )
    metadata = load_metadata(metadata_path)
    matches = match_manifest_sources(
        task_dir,
        metadata,
        raw_workspace,
        include_generated=include_generated,
    )
    unmatched = [match for match in matches if not match.selected_path]
    if strict_matches and unmatched:
        names = ", ".join(match.filename for match in unmatched)
        raise WorkspaceSubsetError(
            f"standard inputs not found by filename and SHA-256: {names}"
        )

    selected_paths = sorted(
        {
            match.selected_path
            for match in matches
            if match.selected_path is not None
        }
    )
    local_roots, lca_degenerated = derive_local_roots(
        selected_paths, min_root_depth=min_root_depth
    )
    mandatory_sources = {
        raw_workspace.joinpath(*PurePosixPath(path).parts)
        for path in selected_paths
    }
    local_candidates = _candidate_files_under(raw_workspace, local_roots)

    common_roots = [
        name
        for name in common_directories
        if raw_workspace.joinpath(
            *_safe_relative_path(name, field="common directory").parts
        ).is_dir()
    ]
    common_candidates: set[Path] = set()
    for common_root in common_roots:
        root_path = raw_workspace.joinpath(
            *PurePosixPath(common_root).parts
        )
        files = list(_iter_files(root_path))
        files.sort(key=lambda path: _relative_posix(path, raw_workspace))
        common_candidates.update(files[: budget.common_dir_files])

    anchor_paths = [PurePosixPath(path) for path in selected_paths]

    def candidate_key(path: Path) -> tuple[int, int, str]:
        relative = PurePosixPath(_relative_posix(path, raw_workspace))
        source_class = 0 if path in local_candidates else 1
        distance = (
            min(_path_distance(relative, anchor) for anchor in anchor_paths)
            if anchor_paths
            else len(relative.parts)
        )
        return source_class, distance, relative.as_posix()

    optional_sources = sorted(
        (local_candidates | common_candidates) - mandatory_sources,
        key=candidate_key,
    )
    copied_sources: list[Path] = []
    skipped_budget: list[str] = []
    copied_bytes = 0

    for source in sorted(
        mandatory_sources,
        key=lambda path: _relative_posix(path, raw_workspace),
    ):
        copied_sources.append(source)
        copied_bytes += source.stat().st_size

    for source in optional_sources:
        size = source.stat().st_size
        if (
            len(copied_sources) >= budget.max_files
            or copied_bytes + size > budget.max_bytes
        ):
            skipped_budget.append(_relative_posix(source, raw_workspace))
            continue
        copied_sources.append(source)
        copied_bytes += size

    _prepare_output_root(output_root, clean)
    copied_entries: list[dict[str, Any]] = []
    for source in copied_sources:
        relative = _relative_posix(source, raw_workspace)
        destination = output_root.joinpath(*PurePosixPath(relative).parts)
        _copy_file(source, destination)
        copied_entries.append(
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
                "is_standard_input": source in mandatory_sources,
            }
        )

    fallback_entries: list[dict[str, Any]] = []
    for match in unmatched:
        stored = task_dir.joinpath(
            *_safe_relative_path(
                match.stored_relpath, field="stored_relpath"
            ).parts
        )
        relative = (
            _safe_relative_path(match.target_path, field="target_path")
            if match.target_path
            else PurePosixPath("文档", "任务输入", match.filename)
        )
        destination = output_root.joinpath(*relative.parts)
        _copy_file(stored, destination)
        entry = {
            "path": relative.as_posix(),
            "size": stored.stat().st_size,
            "sha256": sha256_file(stored),
            "is_standard_input": True,
            "source": "task_data_fallback",
            "stored_relpath": match.stored_relpath,
        }
        copied_entries.append(entry)
        fallback_entries.append(entry)

    source_path_map = {
        "schema_version": 1,
        "task_id": metadata.get("id", metadata.get("absolute_id")),
        "raw_workspace": str(raw_workspace),
        "sources": [match.as_dict() for match in matches],
    }
    subset_manifest = {
        "schema_version": 1,
        "task_id": metadata.get("id", metadata.get("absolute_id")),
        "raw_workspace": str(raw_workspace),
        "local_roots": local_roots,
        "common_roots": common_roots,
        "files": copied_entries,
        "fallback_inputs": fallback_entries,
    }
    mandatory_bytes = sum(
        source.stat().st_size for source in mandatory_sources
    )
    build_report = {
        "schema_version": 1,
        "status": "ok" if not unmatched else "partial",
        "matched_inputs": len(matches) - len(unmatched),
        "unmatched_inputs": len(unmatched),
        "selected_source_files": len(mandatory_sources),
        "copied_files": len(copied_sources) + len(fallback_entries),
        "copied_bytes": copied_bytes
        + sum(item["size"] for item in fallback_entries),
        "local_roots": local_roots,
        "lca_degenerated": lca_degenerated,
        "budget": {
            "max_files": budget.max_files,
            "max_bytes": budget.max_bytes,
            "common_dir_files": budget.common_dir_files,
        },
        "mandatory_inputs_exceed_budget": (
            len(mandatory_sources) > budget.max_files
            or mandatory_bytes > budget.max_bytes
        ),
        "skipped_due_to_budget": skipped_budget,
        "fallback_inputs": [
            item["path"] for item in fallback_entries
        ],
    }
    _write_json(output_root / "source_path_map.json", source_path_map)
    _write_json(output_root / "subset_manifest.json", subset_manifest)
    _write_json(output_root / "build_report.json", build_report)
    return build_report
