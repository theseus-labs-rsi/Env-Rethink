#!/usr/bin/env python3
"""Create a deliberately unreadable or malformed file at a requested path.

The one-argument form creates a small corrupted payload appropriate for the
target suffix:

    python3 corrupt_file_gen.py ./a/b.txt

To derive a damaged copy from an existing file without modifying the source:

    python3 corrupt_file_gen.py ./a/b.xlsx --source ./input/report.xlsx
"""

from __future__ import annotations

import argparse
import hashlib
import random
from pathlib import Path


TEXT_SUFFIXES = {
    ".csv",
    ".json",
    ".log",
    ".md",
    ".rtf",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

ZIP_CONTAINER_SUFFIXES = {
    ".docx",
    ".epub",
    ".jar",
    ".odp",
    ".ods",
    ".odt",
    ".pptx",
    ".xlsx",
    ".zip",
}

OLE_SUFFIXES = {".doc", ".msg", ".ppt", ".xls"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="Path at which to create the corrupted file.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Optional existing file from which to derive a damaged copy.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Optional deterministic seed. The default is derived from the path.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target if it already exists.",
    )
    return parser.parse_args()


def _seed_for(path: Path, seed: int | None) -> int:
    if seed is not None:
        return seed
    digest = hashlib.sha256(path.as_posix().encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _damage_source(raw: bytes, *, suffix: str, rng: random.Random) -> bytes:
    """Return bytes that retain part of the source but cannot be read normally."""

    if not raw:
        return _fresh_payload(suffix=suffix, rng=rng)

    if suffix in TEXT_SUFFIXES:
        # Keep a plausible beginning, then end with an invalid UTF-8 sequence.
        # Text readers using strict UTF-8 fail, while permissive readers see a
        # visibly incomplete final record without an explicit warning marker.
        keep = max(1, min(len(raw) // 3, 4096))
        return raw[:keep].rstrip(b"\r\n") + b"\n" + bytes((0xC3, 0x28, 0xFF))

    if suffix in ZIP_CONTAINER_SUFFIXES:
        # OOXML and ZIP readers require the central directory at the end.
        # Keeping the beginning but removing the tail preserves a realistic
        # signature while making the package invalid.
        keep = max(8, min(len(raw) // 3, 8192))
        damaged = bytearray(raw[:keep])
        if damaged[:2] != b"PK":
            damaged[:2] = b"PK"
        return bytes(damaged)

    if suffix in OLE_SUFFIXES:
        # Retain the Compound File signature but truncate before its directory.
        keep = max(8, min(len(raw) // 4, 4096))
        damaged = bytearray(raw[:keep])
        signature = bytes.fromhex("D0CF11E0A1B11AE1")
        damaged[: min(8, len(damaged))] = signature[: min(8, len(damaged))]
        return bytes(damaged)

    # Generic binary corruption: preserve a prefix, flip a short region, and
    # omit the remainder of the file.
    keep = max(1, min(len(raw) // 3, 4096))
    damaged = bytearray(raw[:keep])
    for _ in range(min(8, len(damaged))):
        index = rng.randrange(len(damaged))
        damaged[index] ^= rng.randrange(1, 256)
    return bytes(damaged)


def _fresh_payload(*, suffix: str, rng: random.Random) -> bytes:
    """Create malformed bytes when no source file was supplied."""

    if suffix in TEXT_SUFFIXES:
        # Starts plausibly, but contains invalid UTF-8 and an unfinished quote.
        return b'name,status,value\n"record,pending,12\n' + bytes(
            (0xE2, 0x28, 0xA1, 0xFF)
        )

    if suffix in ZIP_CONTAINER_SUFFIXES:
        # Local ZIP header with an impossible/truncated body and no directory.
        return (
            b"PK\x03\x04"
            + bytes(rng.randrange(256) for _ in range(24))
            + b"[Content_Types].xml"
        )

    if suffix in OLE_SUFFIXES:
        return bytes.fromhex("D0CF11E0A1B11AE1") + bytes(
            rng.randrange(256) for _ in range(56)
        )

    if suffix == ".pdf":
        return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\n"

    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        signatures = {
            ".png": b"\x89PNG\r\n\x1a\n",
            ".jpg": b"\xff\xd8\xff\xe0",
            ".jpeg": b"\xff\xd8\xff\xe0",
            ".gif": b"GIF89a",
            ".webp": b"RIFF\x20\x00\x00\x00WEBP",
        }
        return signatures[suffix] + bytes(rng.randrange(256) for _ in range(19))

    return bytes(rng.randrange(256) for _ in range(64))


def generate_corrupt_file(
    path: Path,
    *,
    source: Path | None = None,
    seed: int | None = None,
    force: bool = False,
) -> Path:
    target = path.expanduser()
    source_path = source.expanduser() if source is not None else None

    if target.exists() and not force:
        raise FileExistsError(
            f"target already exists: {target}; pass --force to overwrite"
        )
    if source_path is not None:
        if not source_path.is_file():
            raise FileNotFoundError(f"source file not found: {source_path}")
        if source_path.resolve() == target.resolve():
            raise ValueError("source and target must be different paths")

    rng = random.Random(_seed_for(target, seed))
    suffix = target.suffix.lower()
    payload = (
        _damage_source(source_path.read_bytes(), suffix=suffix, rng=rng)
        if source_path is not None
        else _fresh_payload(suffix=suffix, rng=rng)
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def main() -> int:
    args = parse_args()
    output = generate_corrupt_file(
        args.path,
        source=args.source,
        seed=args.seed,
        force=args.force,
    )
    print(output.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
