"""Public-artifact leak gates for generated environment artifacts.

Ported from ``envgen-kit/src/workspace_env/artifact_gate.py`` and applied to the
*public* artifacts this repository stages into a task (event log, workspace
collection map).  Checks:

1. **filename intersection** — a public artifact may not mention a task-material
   filename (``data_manifest`` / ``output_files`` basenames, extension and
   version tails normalised away).  ``report`` severity by default here: unlike
   the kit's ambient layers, this repository's generated map/history are built
   over the task's own file pool on purpose, so an intersection is expected and
   only recorded.  Pass ``filename_severity="fail"`` to enforce the kit rule.
2. **rubric keyword hits** — rubric quoted spans (「…」 “…” "…" 《…》) must not
   appear in public artifact text; fragments are scanned in body text only.
3. **reference alignment** — every workspace path a public artifact mentions
   must exist in the workspace snapshot it was generated from.

The gate is deterministic and read-only.  It never rewrites an artifact; it
returns findings and an overall verdict so the caller can refuse to ship a
leaking artifact.  ``fail`` findings must block staging.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

GATE_FORMAT = "workspace-bench-artifact-gate.v1"

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


TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".log", ".xml", ".html", ".ini", ".conf",
}
# Bounded attribution scan: how much workspace text to load when deciding
# whether a rubric phrase is a faithful quotation of workspace content.
WORKSPACE_TEXT_MAX_FILE_BYTES = 2_000_000
WORKSPACE_TEXT_MAX_TOTAL_CHARS = 8_000_000


OFFICE_SUFFIXES = {".xlsx", ".docx", ".pptx"}
LEGACY_OFFICE_SUFFIXES = {".doc", ".xls", ".ppt"}
# 默认用本机 office 镜像抽取 PDF/Office 文本（宿主通常没有 pdftotext/soffice）。
DEFAULT_EXTRACT_IMAGE = "workspace-bench:local"

_DOCKER_EXTRACT_SCRIPT = r"""
set -e
out=/tmp/workspace-text.txt
: > "$out"
cd /w
find . -type f | LC_ALL=C sort | while read -r f; do
  case "${f##*.}" in
    txt|md|csv|tsv|json|jsonl|yaml|yml|log|xml|html|ini|conf)
      head -c 400000 "$f" >> "$out" 2>/dev/null || true ;;
    pdf)
      pdftotext -layout "$f" - 2>/dev/null | head -c 400000 >> "$out" || true ;;
    docx|doc|xlsx|xls|pptx|ppt)
      d=$(mktemp -d)
      soffice --headless --convert-to txt:Text --outdir "$d" "$f" >/dev/null 2>&1 || true
      for t in "$d"/*.txt; do
        [ -f "$t" ] && head -c 400000 "$t" >> "$out" 2>/dev/null || true
      done
      rm -rf "$d" ;;
  esac
  printf '\n' >> "$out"
done
head -c 8000000 "$out"
""".strip()


def _docker_text_blob(workspace: Path, image: str) -> str:
    """Extract workspace text with the office image (pdftotext/soffice) when the host lacks them."""

    import shutil as _shutil
    import subprocess

    if _shutil.which("docker") is None:
        return ""
    try:
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{workspace}:/w:ro",
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                _DOCKER_EXTRACT_SCRIPT,
            ],
            capture_output=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0 and not completed.stdout:
        return ""
    return re.sub(r"\s+", " ", completed.stdout.decode("utf-8", "ignore"))


def _office_zip_text(path: Path) -> str:
    """Extract visible text from an OOXML file with the standard library only."""

    import zipfile

    parts: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for member in sorted(names):
                if path.suffix == ".xlsx" and (
                    member == "xl/sharedStrings.xml" or member.startswith("xl/worksheets/")
                ):
                    parts.append(archive.read(member).decode("utf-8", "ignore"))
                elif path.suffix in {".docx", ".pptx"} and member.endswith(".xml") and (
                    member.startswith("word/") or member.startswith("ppt/slides/")
                ):
                    parts.append(archive.read(member).decode("utf-8", "ignore"))
    except (OSError, zipfile.BadZipFile, KeyError):
        return ""
    text = " ".join(parts)
    compact = re.sub(r"<[^>]+>", "", text)
    spaced = re.sub(r"<[^>]+>", " ", text)
    # OOXML splits a sentence across runs, so the compact variant is needed for
    # substring attribution; the spaced variant keeps unrelated strings apart.
    return compact + " " + re.sub(r"\s+", " ", spaced)


def _soffice_text(path: Path) -> str:
    """Best-effort document text through LibreOffice (handles .doc/.xls/.ppt too)."""

    import shutil as _shutil
    import subprocess
    import tempfile

    binary = _shutil.which("soffice") or _shutil.which("libreoffice")
    if binary is None:
        return ""
    with tempfile.TemporaryDirectory() as target:
        try:
            subprocess.run(
                [binary, "--headless", "--convert-to", "txt:Text", "--outdir", target, str(path)],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        parts: list[str] = []
        for converted in sorted(Path(target).glob("*.txt")):
            try:
                parts.append(converted.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return re.sub(r"\s+", " ", " ".join(parts))


def _pdf_text(path: Path) -> str:
    """Best-effort PDF text via poppler's pdftotext when it is available."""

    import shutil as _shutil
    import subprocess

    binary = _shutil.which("pdftotext")
    if binary is None:
        return ""
    try:
        completed = subprocess.run(
            [binary, "-layout", str(path), "-"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return re.sub(r"\s+", " ", completed.stdout.decode("utf-8", "ignore"))


def workspace_text_blob(
    workspace: Path,
    *,
    extract_image: str | None = DEFAULT_EXTRACT_IMAGE,
) -> str:
    """Normalized text of the workspace's readable files (bounded, deterministic).

    Used to attribute rubric phrases: rubrics in this benchmark routinely quote
    the task's own files, and this layer quotes the same files on purpose, so a
    phrase that occurs in workspace content is a faithful quotation rather than
    a leak.  Plain text is read directly; OOXML via the standard library; PDF and
    legacy Office formats through ``pdftotext``/``soffice`` when present, and
    otherwise through a one-shot ``extract_image`` container (the office image
    ships Poppler + LibreOffice).  A phrase that cannot be found anywhere stays a
    ``fail`` and needs human triage.
    """

    import shutil as _shutil

    chunks: list[str] = []
    total = 0
    host_pdf = _shutil.which("pdftotext") is not None
    host_soffice = _shutil.which("soffice") is not None or _shutil.which("libreoffice") is not None
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        # File names and relative paths are workspace content too: an artifact
        # that mentions a real pool file name is referencing task material.
        relative = path.relative_to(workspace).as_posix()
        chunks.append(relative)
        chunks.append(path.name)
        suffix = path.suffix.casefold()
        text = ""
        try:
            if suffix in TEXT_SUFFIXES:
                if path.stat().st_size > WORKSPACE_TEXT_MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            elif suffix in OFFICE_SUFFIXES:
                if path.stat().st_size > WORKSPACE_TEXT_MAX_FILE_BYTES:
                    continue
                text = _office_zip_text(path)
                if host_soffice:
                    text += " " + _soffice_text(path)
            elif suffix in LEGACY_OFFICE_SUFFIXES:
                if host_soffice:
                    text = _soffice_text(path)
            elif suffix == ".pdf":
                if path.stat().st_size > WORKSPACE_TEXT_MAX_FILE_BYTES:
                    continue
                if host_pdf:
                    text = _pdf_text(path)
            else:
                continue
        except OSError:
            continue
        if not text:
            continue
        chunks.append(text)
        total += len(text)
        if total >= WORKSPACE_TEXT_MAX_TOTAL_CHARS:
            break
    blob = re.sub(r"\s+", " ", "\n".join(chunks))
    if extract_image and not host_pdf:
        # 宿主缺 Poppler/LibreOffice：用 office 镜像一次性抽取（含 PDF 与 .doc/.xls）。
        container_blob = _docker_text_blob(workspace, extract_image)
        if container_blob:
            blob = f"{blob} {container_blob}"
    return blob[: WORKSPACE_TEXT_MAX_TOTAL_CHARS * 2]


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
            "format": GATE_FORMAT,
            "verdict": "fail" if self.failed else "pass",
            "findings": [
                {"check": f.check, "severity": f.severity, "detail": f.detail}
                for f in self.findings
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

    Strict phrases are the quoted spans (``「…」``, ``“…”``, ``"…"``, ``《…》``) —
    the high-signal strings the rubric singles out.  They are checked against
    both paths and body text; fragments of those phrases against body text only.

    Unquoted rubric vocabulary is deliberately not swept by default (measured on
    the source pipeline, token-level sweeps produced mostly false positives on
    ordinary office vocabulary).  Use ``scan_mode="tokens"`` for the stricter
    sweep, plus ``extra_keywords`` for operator-chosen terms.
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


_PATH_POINTER_SUFFIXES = (
    ".path_at_event",
    ".path",
    ".stored_relpath",
    ".filename",
    ".target_path",
    ".source_path",
    ".destination_path",
)


_COMPACT_STRIP = re.compile(r"""[\s：:、，,。.·•|｜\-—_()（）\[\]【】\"'“”‘’]+""")


def _compact(text: str) -> str:
    """Whitespace- and punctuation-insensitive form for phrase attribution.

    Rubrics normalize punctuation when they quote a source line (``阶段合计：140天``
    is quoted as ``阶段合计 140 天``); both variants must attribute to the same
    workspace content, otherwise a faithful quotation looks like a leak.
    """

    return _COMPACT_STRIP.sub("", text)


def _is_path_pointer(pointer: str) -> bool:
    return pointer.endswith(_PATH_POINTER_SUFFIXES)


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
    severity: str = "report",
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
                        severity,
                        f"{artifact.name} {pointer}={value!r} collides with a task-material filename",
                    )
                )


def check_rubric_keywords(
    artifact_paths: Iterable[Path],
    *,
    strict_phrases: set[str],
    loose_fragments: set[str],
    report: GateReport,
    workspace: Path | None = None,
    workspace_text: str = "",
    compact_workspace_text: str = "",
    public_text: str = "",
    public_compact_text: str = "",
    max_hits: int = 12,
) -> None:
    """Flag rubric phrases.  Strict phrases also apply to path-like strings.

    Repository-specific refinement: when a strict phrase occurs inside a path
    pointer whose value resolves to a real workspace file, the artifact is
    faithfully referencing task material — that is this layer's job — so the
    finding is downgraded to ``report``.  Everything else (rubric-only values,
    answer strings, deliverable names, hallucinated paths) stays ``fail``.
    """

    for artifact in artifact_paths:
        for pointer, value in _artifact_strings(artifact):
            folded = value.casefold()
            if len(folded) < 2:
                continue
            candidates = strict_phrases if _is_path_pointer(pointer) else strict_phrases | loose_fragments
            hits = sorted({keyword for keyword in candidates if keyword.casefold() in folded})
            faithful_workspace_path = (
                workspace is not None
                and _is_path_pointer(pointer)
                and not Path(value).is_absolute()
                and ".." not in Path(value).parts
                and (workspace / value).exists()
            )
            for keyword in hits[:max_hits]:
                if faithful_workspace_path:
                    report.findings.append(
                        GateFinding(
                            "rubric_keyword_reference",
                            "report",
                            f"{artifact.name} {pointer} mentions rubric phrase {keyword!r} "
                            "inside a real workspace path",
                        )
                    )
                    continue
                if workspace_text and (
                    re.sub(r"\s+", " ", keyword) in workspace_text
                    or _compact(keyword) in compact_workspace_text
                ):
                    report.findings.append(
                        GateFinding(
                            "rubric_keyword_quotation",
                            "report",
                            f"{artifact.name} {pointer} contains {keyword!r} which occurs in "
                            "workspace content (faithful quotation)",
                        )
                    )
                    continue
                if public_text and (
                    re.sub(r"\s+", " ", keyword) in public_text
                    or _compact(keyword) in public_compact_text
                ):
                    report.findings.append(
                        GateFinding(
                            "rubric_keyword_public_task_text",
                            "report",
                            f"{artifact.name} {pointer} contains {keyword!r} which the task "
                            "statement itself states (public vocabulary)",
                        )
                    )
                    continue
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
    severity: str = "fail",
    missing_paths: Iterable[str] = (),
) -> None:
    """Every referenced workspace path must exist.

    ``severity="report"`` is the right mode for a *history* artifact: a past
    state legitimately mentions files that were later deleted or renamed (the
    schema closes those chains with delete events).  The collection map, which
    describes the current snapshot, keeps ``fail``.

    ``missing_paths`` lets the caller pre-declare paths that are legitimately
    absent (e.g. a disabled-surface fixture): they are skipped.
    """

    allowed_missing = {str(item) for item in missing_paths}
    for artifact in artifact_paths:
        for claimed in sorted(_artifact_workspace_paths(artifact)):
            if claimed in allowed_missing:
                continue
            if not (workspace / claimed).exists():
                report.findings.append(
                    GateFinding(
                        "reference_alignment",
                        severity,
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
    filename_severity: str = "report",
    reference_alignment_severity: str = "fail",
    missing_paths: Iterable[str] = (),
    extract_image: str | None = DEFAULT_EXTRACT_IMAGE,
) -> GateReport:
    workspace_root = Path(workspace).resolve(strict=True)
    if not workspace_root.is_dir():
        raise ArtifactGateError("workspace must be a directory")
    metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    if not isinstance(metadata, Mapping):
        raise ArtifactGateError("task metadata must be a JSON object")
    if scan_mode not in {"quoted", "tokens"}:
        raise ArtifactGateError("scan_mode must be 'quoted' or 'tokens'")
    if filename_severity not in {"fail", "report"}:
        raise ArtifactGateError("filename_severity must be 'fail' or 'report'")
    if reference_alignment_severity not in {"fail", "report"}:
        raise ArtifactGateError("reference_alignment_severity must be 'fail' or 'report'")
    artifacts = [Path(p).resolve(strict=True) for p in artifact_paths]
    for artifact in artifacts:
        if artifact.suffix not in {".json", ".jsonl"}:
            raise ArtifactGateError(f"unsupported artifact type: {artifact}")

    report = GateReport()
    # 任务描述本身就是公开给 agent 的文本：其中的措辞不算泄题。
    public_text = re.sub(r"\s+", " ", str(metadata.get("task") or ""))
    workspace_text = workspace_text_blob(workspace_root, extract_image=extract_image)
    check_filename_intersection(
        artifacts,
        task_material_names(metadata),
        report=report,
        severity=filename_severity,
    )
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
            workspace=workspace_root,
            workspace_text=workspace_text,
            compact_workspace_text=_compact(workspace_text),
            public_text=public_text,
            public_compact_text=_compact(public_text),
        )
        if not strict and not loose:
            report.findings.append(
                GateFinding("rubric_keyword", "report", "no quoted rubric phrases found; nothing to scan for")
            )
    else:
        report.findings.append(GateFinding("rubric_keyword", "report", "task metadata has no rubrics list"))
    if include_reference_alignment:
        check_reference_alignment(
            artifacts,
            workspace=workspace_root,
            report=report,
            severity=reference_alignment_severity,
            missing_paths=missing_paths,
        )
    return report
