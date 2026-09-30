"""Write the shell entry point an agent CLI's hook config points at.

Kept in its own module, and deliberately **stdlib-only**, because of *who*
imports it: the sandbox driver imports this before the sidecar has installed
its dependencies.  The hook itself renders history with ``tiktoken`` and the
collection map with pydantic, so importing ``agent_context_hook`` at that point
fails with ``ModuleNotFoundError: No module named 'tiktoken'`` — measured, on
the first real smoke run.  Pinning this to stdlib keeps the driver's import
order irrelevant; the hook process later gets the full ``PYTHONPATH`` from the
script written here.
"""

from __future__ import annotations

import os
import shlex

from pathlib import Path


def write_hook_launcher(
    path: str | Path,
    *,
    python: str,
    pythonpath: str,
    artifact_root: str,
    workspace_root: str,
    audit_log: str,
    deduplication_state: str,
    tiktoken_cache_dir: str | None = None,
) -> Path:
    """Write the script that the agent CLI's hook config will name.

    The hook is started by the *agent's* CLI, so it inherits the agent's
    environment, not the sidecar's.  Everything it needs beyond the repo on
    ``PYTHONPATH`` — the task-private dependency directory (``tiktoken`` is not
    in every image) and the matching tokenizer cache — is therefore pinned into
    this script.  The agent config then only ever names one path, which keeps
    sandbox-specific paths out of the host-side plan.

    ``PYTHONPATH`` and ``TIKTOKEN_CACHE_DIR`` are exported through ``env(1)``
    with every argument quoted, so a path containing spaces cannot be split.
    """

    variables = [
        ("PYTHONPATH", pythonpath),
        ("TIKTOKEN_CACHE_DIR", tiktoken_cache_dir or ""),
    ]
    command = [
        python,
        "-m",
        "workspace_env.agent_context_hook",
        "--artifact-root",
        artifact_root,
        "--audit-log",
        audit_log,
        "--deduplication-state",
        deduplication_state,
        "--workspace-root",
        workspace_root,
    ]
    body = "\n".join(
        [
            "#!/bin/sh",
            "# Written by the sandbox agent runner: the agent CLI's hook config only",
            "# ever names this path, never the paths below.",
            "exec env "
            + " ".join(
                shlex.quote(name) + "=" + shlex.quote(value)
                for name, value in variables
                if value
            )
            + " "
            + " ".join(shlex.quote(part) for part in command),
        ]
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(body + "\n", encoding="utf-8")
    os.chmod(target, 0o755)
    return target


__all__ = ["write_hook_launcher"]
