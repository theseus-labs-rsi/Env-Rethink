"""Codex runner injection point.

This kit deliberately does **not** ship an agent runner.  Every orchestrator here
takes ``codex_runner=<callable>`` and expects a *real* Codex CLI execution.  Write
one adapter for your environment and select it with ``--runner module:attr`` or the
``ENVGEN_RUNNER`` environment variable (the variable is inherited by the
subprocesses spawned by ``run_all_rubric_context_loop.py``).

Contract
--------
Call keyword arguments (all keyword-only)::

    prompt: str          # full prompt text
    work_dir: str        # directory Codex may work in (a read-only copy for reviews)
    sandbox_dir: str     # private directory for Codex runtime state
    timeout_s: float
    api_provider: dict   # {"authMode": ..., "model": ..., "__codex_runtime__": {...}}
    agent_id: str        # label used in audit/report files

Return value: a ``dict`` with at least::

    status: str          # "ok" on success; any other value means failure
    errorMessage: str    # human-readable failure text (used when status != "ok")
    trace: dict          # {"collection": {"complete": True, "threadId": ...},
                         #  "executionTrace": [normalised tool events, ...]}
    durationMs: int      # optional, recorded in the private run audit

Fail-closed rules the orchestrators apply after every call:

* ``status != "ok"`` aborts the role.
* ``trace.collection.complete is not True`` aborts the role — a partial JSONL
  trace must never be turned into a successful candidate.
* the runner must not write to ``work_dir``: the kit hashes the workspace before
  and after each call and invalidates the run when the hash changes.

For the trace-conversion entry point
(``scripts/run_codex_trace_event_log.py``) the result additionally has to carry a
normalised ``executionTrace`` whose ``tool``/``status`` fields are populated; that
script checks the shape itself and fails closed with exit code 4 when it is
missing.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Protocol, runtime_checkable

ENV_VAR = "ENVGEN_RUNNER"

__all__ = [
    "ENV_VAR",
    "CodexRunner",
    "RunnerContractError",
    "load_runner",
    "runner_spec",
]


class RunnerContractError(RuntimeError):
    """Raised when the configured runner cannot be imported or is not callable."""


@runtime_checkable
class CodexRunner(Protocol):
    def __call__(
        self,
        *,
        prompt: str,
        work_dir: str,
        sandbox_dir: str,
        timeout_s: float,
        api_provider: dict[str, Any] | None = None,
        agent_id: str,
        **extra: Any,
    ) -> dict[str, Any]: ...


def runner_spec(explicit: str | None = None) -> str:
    """Return the configured ``module:attr`` spec, or raise with usage guidance."""

    spec = (explicit or os.environ.get(ENV_VAR) or "").strip()
    if not spec:
        raise RunnerContractError(
            "no Codex runner configured: pass --runner module:attr or set "
            f"{ENV_VAR}. The kit intentionally ships no runner; see "
            "docs/RUNNER_CONTRACT.md."
        )
    return spec


def load_runner(explicit: str | None = None) -> CodexRunner:
    """Import ``module:attr`` and return it as a :class:`CodexRunner`."""

    spec = runner_spec(explicit)
    if spec.count(":") != 1:
        raise RunnerContractError(f"runner spec must look like 'module:attr', got {spec!r}")
    module_name, attribute = spec.split(":", 1)
    module_name, attribute = module_name.strip(), attribute.strip()
    if not module_name or not attribute:
        raise RunnerContractError(f"runner spec must look like 'module:attr', got {spec!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RunnerContractError(
            f"cannot import runner module {module_name!r}: {exc}. "
            "Make sure the module is importable from the kit's Python environment."
        ) from exc
    candidate = getattr(module, attribute, None)
    if candidate is None:
        raise RunnerContractError(f"module {module_name!r} has no attribute {attribute!r}")
    if not callable(candidate):
        raise RunnerContractError(f"{spec!r} is not callable")
    return candidate  # type: ignore[return-value]
