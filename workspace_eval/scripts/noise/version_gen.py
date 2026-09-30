#!/usr/bin/env python3
"""Generate a naturally versioned filename from a base filename.

Examples:

    python3 version_gen.py 报告.xlsx
    python3 version_gen.py ./资料/薪酬汇总.xlsx
    python3 version_gen.py 报告.xlsx --count 5

The input name is used as-is. Existing suffixes such as ``v2`` or ``最终版``
are not detected or removed; a new version marker is simply appended.
"""

from __future__ import annotations

import argparse
import random
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VersionStyle:
    """A filename suffix generator and the regex describing its output."""

    name: str
    pattern: re.Pattern[str]
    templates: tuple[str, ...]
    numbered: bool = True

    def render(self, rng: random.Random) -> str:
        template = rng.choice(self.templates)
        if not self.numbered:
            return template
        number = rng.randint(1, 12)
        return template.format(n=number)


VERSION_STYLES = (
    VersionStyle(
        "parentheses",
        re.compile(r" \(\d+\)$"),
        (" ({n})",),
    ),
    VersionStyle(
        "fullwidth_parentheses",
        re.compile(r"（\d+）$"),
        ("（{n}）",),
    ),
    VersionStyle(
        "v_number",
        re.compile(r"[_ -]?[vV]\d+$"),
        ("_v{n}", "-v{n}", " v{n}", "_V{n}"),
    ),
    VersionStyle(
        "version_word",
        re.compile(r"[_ -]?[Vv]er(?:sion)?[._ -]?\d+$", re.IGNORECASE),
        ("_Ver{n}", "_Version{n}", " Version {n}"),
    ),
    VersionStyle(
        "underscore_number",
        re.compile(r"_\d+$"),
        ("_{n}",),
    ),
    VersionStyle(
        "hyphen_number",
        re.compile(r"[-—–]\d+$"),
        ("-{n}", "—{n}", "–{n}"),
    ),
    VersionStyle(
        "space_number",
        re.compile(r" \d+$"),
        (" {n}",),
    ),
    VersionStyle(
        "revision_number",
        re.compile(r"[_ -]?[rR](?:ev)?[._ -]?\d+$"),
        ("_r{n}", "_R{n}", "_rev{n}", " Rev {n}"),
    ),
    VersionStyle(
        "copy_number",
        re.compile(r"[_ -]?(?:copy|副本)[_ -]?\d+$", re.IGNORECASE),
        ("_copy{n}", " copy {n}", "_副本{n}", " 副本 {n}"),
    ),
    VersionStyle(
        "final",
        re.compile(r"[_ -]?(?:final|FINAL|Final)$"),
        ("_final", "-final", " final", "_Final", "_FINAL"),
        numbered=False,
    ),
    VersionStyle(
        "final_number",
        re.compile(r"[_ -]?(?:final|FINAL|Final)[_ -]?\d+$"),
        (
            "_final_{n}",
            "-final-{n}",
            " final {n}",
            "_Final{n}",
            "_FINAL_{n}",
        ),
    ),
    VersionStyle(
        "final_cn",
        re.compile(r"[_ -]?(?:最终版|终稿|定稿|正式版)$"),
        ("_最终版", "-最终版", " 最终版", "_终稿", "_定稿", "_正式版"),
        numbered=False,
    ),
    VersionStyle(
        "workflow_cn",
        re.compile(
            r"[_ -]?(?:修订版|更新版|提交版|归档版|确认版|完整版|"
            r"复核版|发布版|送审版|汇总版|调整版)$"
        ),
        (
            "_修订版",
            "_更新版",
            "_提交版",
            "_归档版",
            "_确认版",
            "_完整版",
            "_复核版",
            "_发布版",
            "_送审版",
            "_汇总版",
            "_调整版",
        ),
        numbered=False,
    ),
    VersionStyle(
        "workflow_number_cn",
        re.compile(
            r"[_ -]?(?:修订|更新|提交|归档|确认|复核|发布|送审)[_ -]?\d+$"
        ),
        (
            "_修订{n}",
            "_更新{n}",
            "_提交{n}",
            "_归档{n}",
            "_确认{n}",
            "_复核{n}",
            "_发布{n}",
            "_送审{n}",
        ),
    ),
    VersionStyle(
        "draft_cn",
        re.compile(r"[_ -]?(?:草稿|初稿|二稿|三稿|讨论稿|评审稿|预览版)$"),
        (
            "_草稿",
            "_初稿",
            "_二稿",
            "_三稿",
            "_讨论稿",
            "_评审稿",
            "_预览版",
        ),
        numbered=False,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_name",
        help="Base filename or path. It is not modified or version-normalized.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of distinct names to generate. Default: 1.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Optional deterministic seed; default behavior is random.",
    )
    parser.add_argument(
        "--style",
        choices=[style.name for style in VERSION_STYLES],
        help="Force one style instead of choosing randomly.",
    )
    return parser.parse_args()


def _split_name(path: Path) -> tuple[str, str]:
    """Split only the final extension while retaining names such as data.tar."""

    if path.name in {"", ".", ".."}:
        raise ValueError("base_name must include a filename")
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    return stem, suffix


def generate_versioned_name(
    base_name: str,
    *,
    rng: random.Random | None = None,
    style_name: str | None = None,
) -> str:
    """Append one random version marker while preserving path and extension."""

    randomizer = rng or random.SystemRandom()
    source = Path(base_name)
    stem, suffix = _split_name(source)
    styles = (
        [style for style in VERSION_STYLES if style.name == style_name]
        if style_name
        else list(VERSION_STYLES)
    )
    if not styles:
        raise ValueError(f"unknown style: {style_name}")
    marker = randomizer.choice(styles).render(randomizer)
    return (source.parent / f"{stem}{marker}{suffix}").as_posix()


def generate_versioned_names(
    base_name: str,
    *,
    count: int,
    seed: int | None = None,
    style_name: str | None = None,
) -> list[str]:
    if count < 1:
        raise ValueError("count must be at least 1")
    rng: random.Random = (
        random.Random(seed) if seed is not None else random.SystemRandom()
    )
    results: list[str] = []
    seen: set[str] = set()
    # Natural styles have a finite set of labels, so bound retries while still
    # allowing useful multi-name generation.
    max_attempts = max(100, count * 30)
    for _ in range(max_attempts):
        candidate = generate_versioned_name(
            base_name,
            rng=rng,
            style_name=style_name,
        )
        if candidate in seen:
            continue
        seen.add(candidate)
        results.append(candidate)
        if len(results) == count:
            return results
    raise ValueError(
        f"could not generate {count} distinct names with the selected style"
    )


def main() -> int:
    args = parse_args()
    for name in generate_versioned_names(
        args.base_name,
        count=args.count,
        seed=args.seed,
        style_name=args.style,
    ):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
