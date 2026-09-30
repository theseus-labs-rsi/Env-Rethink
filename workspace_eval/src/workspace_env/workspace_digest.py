"""Deterministic digest of one workspace view.

The environment chains bind every generated artifact to an immutable workspace
snapshot.  This module holds the single implementation of that digest.

Scope constraint
----------------
This walks **every** entry below ``root`` (dirs, symlinks, file bytes, mode
bits).  That is exactly what a snapshot binding needs, and it is affordable
because the generation chains only ever point it at a *task file pool*
(tens to a few hundred files).  Never call it on a role workspace or on an
agent's live workdir — those are GB-scale, and the repository explicitly
forbids full-tree hashing in batch hot paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

__all__ = ["workspace_snapshot_hash"]


def workspace_snapshot_hash(root: str, *, exclude_controlled_agents_md: bool = False) -> str:
    """Hash the immutable workspace input view.

    ``model_output`` is the harness-reserved destination for a task result.  It
    may be created empty before the MCP sidecar starts and is subsequently
    written by the agent, so it is deliberately not part of the input snapshot
    that validates an index or collection map.  When a controlled experiment
    stages its harness-owned ``AGENTS.md`` into the task cwd, that one runtime
    instruction file can likewise be excluded explicitly.  All other paths,
    including a source-workspace ``AGENTS.md`` under the default setting,
    remain part of the hash.
    """

    base = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        rel = path.relative_to(base).as_posix()
        if rel == "model_output" or rel.startswith("model_output/"):
            continue
        if exclude_controlled_agents_md and rel == "AGENTS.md":
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        elif stat.S_ISDIR(info.st_mode):
            kind = "dir"
        else:
            kind = "special"
        digest.update(
            json.dumps(
                [rel, kind, stat.S_IMODE(info.st_mode)], separators=(",", ":")
            ).encode("utf-8")
        )
        digest.update(b"\0")
        if kind == "symlink":
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif kind == "file":
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()
