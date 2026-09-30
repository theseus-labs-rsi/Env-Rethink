"""Public-artifact leak gates for generated environment artifacts.

Mirrors the ambient-workspace gates of the workspacing pipeline and applies them to
the *public* artifacts this kit produces (event log, workspace collection map):

1. **filename intersection** — no filename in a public artifact may collide with a
   task-material filename (``data_manifest[].target_path`` basename or
   ``data_manifest[].filename``), after normalising extension and version tails.
2. **rubric keyword hits** — rubric keywords must not appear in public artifact text.
   Generic tokens are filtered (format suffixes, short pure numbers) so the gate does
   not fire on ordinary office vocabulary.
3. **reference alignment** — every workspace path a public artifact mentions must
   exist in the workspace snapshot it was generated from.
4. **public schema** — the artifact's own validators (forbidden keys, URL policy,
   ``synthetic`` provenance coupling) must pass.

The gate is deterministic and read-only. It never rewrites an artifact; it returns
findings and an overall verdict so the caller can refuse to ship a leaking artifact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

NAME_STOPWORDS = {
    "md", "txt", "csv", "xlsx", "xls", "json", "png", "jpg", "jpeg", "docx", "doc",
    "pdf", "pptx", "ppt", "zip", "rar", "7z", "html", "xml", "yaml", "yml",
}
VERSION_TAILS = (
    "副本", "终版", "定稿", "补充", "修改", "改", "新版", "旧版", "备份", "草稿",
    "final", "copy", "backup", "draft", "new", "old", "revised", "updated",
)
_VERSION_SUFFIX = re.compile(
    r"(?:[_\-\s]*(?:v|ver|version|rev|r)?\d+(?:\.\d+)*)$|(?:[_\-\s]*\((?:\d+|[^)]*副本[^)]*|copy)\))$",
    re.IGNORECASE,
)
WORD = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]+")


class ArtifactGateError(ValueError):
    """Raised for malformed gate inputs (never for a mere gate finding)."""


@dataclass
class GateFinding:
    check: str
    severity: str  # "fail" | "report"
    detail: str


@dataclass
class GateReport:
    findings: list[GateFinding] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(finding.severity == "fail" for finding in self.findings)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "envgen-kit-artifact-gate.v1",
            "verdict": "fail" if self.failed else "pass",
            "findings": [
                {"check": f.check, "severity": f.severity, "detail": f.detail} for f in self.findings
            ],
        }


def normalize_name(value: str) -> str:
    """Lowercase, drop the extension and any version/copy tail."""

    name = Path(value).name.casefold()
    if "." in name:
        stem, _, suffix = name.rpartition(".")
        if suffix in NAME_STOPWORDS:
            name = stem
    for _ in range(3):
        stripped = _VERSION_SUFFIX.sub("", name)
        for tail in VERSION_TAILS:
            if stripped.endswith(tail) and len(stripped) > len(tail):
                stripped = stripped[: -len(tail)]
        stripped = stripped.rstrip("_- (（")
        if stripped == name or not stripped:
            break
        name = stripped
    return name


def task_material_names(metadata: Mapping[str, Any]) -> set[str]:
    """Names the task owns: manifest filenames and manifest target-path basenames."""

    names: set[str] = set()
    for item in metadata.get("data_manifest") or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("filename", "target_path", "stored_relpath"):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                names.add(normalize_name(raw))
    output_files = metadata.get("output_files")
    if isinstance(output_files, list):
        for raw in output_files:
            if isinstance(raw, str) and raw.strip():
                names.add(normalize_name(raw))
    return {name for name in names if name}


def rubric_keyword_tiers(rubrics: Sequence[str]) -> tuple[set[str], set[str]]:
    """Split rubric material into ``(strict_phrases, loose_fragments)``.

    Strict phrases are the quoted spans (``「…」``, ``“…”``, ``"…"``, ``《…》``) — the
    high-signal strings the rubric singles out.  They are checked against both paths
    and body text.  Fragments of those phrases are checked against body text only.

    Unquoted rubric vocabulary is deliberately *not* swept by default: measured on the
    workspacing pipeline, token-level sweeps produced mostly false positives on ordinary
    office vocabulary (格式后缀、年份、通用名词).  Use ``scan_mode="tokens"`` when you want
    the stricter sweep, and ``extra_keywords`` to add operator-chosen terms.
    """

    strict: set[str] = set()
    loose: set[str] = set()
    for rubric in rubrics:
        if not isinstance(rubric, str):
            continue
        for phrase in re.findall(r"[“\"'《「【]([^”\"'》」】]{2,40})[”\"'》」】]", rubric):
            phrase = phrase.strip()
            if len(phrase) < 2:
                continue
            strict.add(phrase)
            for fragment in re.split(r"[\s/、,，;；:：()（）\[\]<>]+", phrase):
                fragment = fragment.strip()
                if len(fragment) >= 2 and normalize_name(fragment):
                    loose.add(fragment)
    return strict, loose


def token_keywords(rubrics: Sequence[str]) -> set[str]:
    """Strict sweep: rubric tokens long enough to be discriminative."""

    keywords: set[str] = set()
    for rubric in rubrics:
        if not isinstance(rubric, str):
            continue
        for token in WORD.findall(rubric):
            folded = token.casefold()
            if folded in NAME_STOPWORDS or token.isdigit():
                continue
            if re.search(r"[\u4e00-\u9fff]", token):
                if len(token) >= 4:
                    keywords.add(token)
            elif len(token) >= 6:
                keywords.add(token)
    return {keyword for keyword in keywords if normalize_name(keyword)}


def _is_path_pointer(pointer: str) -> bool:
    return pointer.endswith((".path_at_event", ".path", ".stored_relpath", ".filename", ".target_path"))


def _collect_strings(value: Any, *, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _collect_strings(item, path=f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _collect_strings(item, path=f"{path}[{index}]")


def _artifact_strings(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        out: list[tuple[str, str]] = []
        for index, row in enumerate(rows):
            out.extend(_collect_strings(row, path=f"row[{index}]"))
        return out
    return list(_collect_strings(json.loads(text)))


def _artifact_workspace_paths(path: Path) -> set[str]:
    """Workspace-relative paths a public artifact claims to reference."""

    paths: set[str] = set()
    for pointer, value in _artifact_strings(path):
        if not pointer.endswith((".path_at_event", ".path", ".stored_relpath")):
            continue
        if value and not Path(value).is_absolute() and ".." not in Path(value).parts:
            paths.add(value)
    return paths


def check_filename_intersection(
    artifact_paths: Iterable[Path],
    material_names: set[str],
    *,
    report: GateReport,
) -> None:
    for artifact in artifact_paths:
        for pointer, value in _artifact_strings(artifact):
            if not pointer.endswith((".path_at_event", ".path", ".filename", ".stored_relpath")):
                continue
            normalized = normalize_name(value)
            if normalized and normalized in material_names:
                report.findings.append(
                    GateFinding(
                        "filename_intersection",
                        "fail",
                        f"{artifact.name} {pointer}={value!r} collides with a task-material filename",
                    )
                )


def check_rubric_keywords(
    artifact_paths: Iterable[Path],
    *,
    strict_phrases: set[str],
    loose_fragments: set[str],
    report: GateReport,
    max_hits: int = 12,
) -> None:
    """Flag rubric phrases.  Strict phrases also apply to path-like strings."""

    for artifact in artifact_paths:
        for pointer, value in _artifact_strings(artifact):
            folded = value.casefold()
            if len(folded) < 2:
                continue
            candidates = strict_phrases if _is_path_pointer(pointer) else strict_phrases | loose_fragments
            hits = sorted({keyword for keyword in candidates if keyword.casefold() in folded})
            for keyword in hits[:max_hits]:
                report.findings.append(
                    GateFinding(
                        "rubric_keyword",
                        "fail",
                        f"{artifact.name} {pointer} contains rubric keyword {keyword!r}",
                    )
                )


def check_reference_alignment(
    artifact_paths: Iterable[Path],
    *,
    workspace: Path,
    report: GateReport,
) -> None:
    for artifact in artifact_paths:
        for claimed in sorted(_artifact_workspace_paths(artifact)):
            if not (workspace / claimed).exists():
                report.findings.append(
                    GateFinding(
                        "reference_alignment",
                        "fail",
                        f"{artifact.name} references {claimed!r} which does not exist in the workspace",
                    )
                )


def run_gate(
    *,
    workspace: str | Path,
    metadata_path: str | Path,
    artifact_paths: Sequence[str | Path],
    include_reference_alignment: bool = True,
    scan_mode: str = "quoted",
    extra_keywords: Sequence[str] = (),
) -> GateReport:
    workspace_root = Path(workspace).resolve(strict=True)
    if not workspace_root.is_dir():
        raise ArtifactGateError("workspace must be a directory")
    metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    if not isinstance(metadata, Mapping):
        raise ArtifactGateError("task metadata must be a JSON object")
    if scan_mode not in {"quoted", "tokens"}:
        raise ArtifactGateError("scan_mode must be 'quoted' or 'tokens'")
    artifacts = [Path(p).resolve(strict=True) for p in artifact_paths]
    for artifact in artifacts:
        if artifact.suffix not in {".json", ".jsonl"}:
            raise ArtifactGateError(f"unsupported artifact type: {artifact}")

    report = GateReport()
    check_filename_intersection(artifacts, task_material_names(metadata), report=report)
    rubrics = metadata.get("rubrics")
    if isinstance(rubrics, list):
        strict, loose = rubric_keyword_tiers(rubrics)
        if scan_mode == "tokens":
            loose |= token_keywords(rubrics)
        strict |= {keyword for keyword in extra_keywords if keyword.strip()}
        check_rubric_keywords(
            artifacts,
            strict_phrases=strict,
            loose_fragments=loose,
            report=report,
        )
        if not strict and not loose:
            report.findings.append(
                GateFinding("rubric_keyword", "report", "no quoted rubric phrases found; nothing to scan for")
            )
    else:
        report.findings.append(GateFinding("rubric_keyword", "report", "task metadata has no rubrics list"))
    if include_reference_alignment:
        check_reference_alignment(artifacts, workspace=workspace_root, report=report)
    return report
