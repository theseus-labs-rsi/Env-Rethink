"""Role runner injection point for the environment-generation chains.

The orchestrators in this package never talk to a model CLI directly.  They take
``role_runner=<callable>`` and expect a *real* agent execution — every generated
artifact must come from an actual model run, never from a rule or placeholder.

Select the adapter with ``--runner module:attr`` or the ``ENVGEN_RUNNER``
environment variable.  The shipped adapter is
``workspace_env.agentkit_runner:run`` (runs Claude Code / Codex in a docker container).

Contract
--------
Call keyword arguments (all keyword-only)::

    prompt: str          # full prompt text
    work_dir: str        # directory the role may work in (a read-only copy for reviews)
    sandbox_dir: str     # private directory for runner state and audits
    timeout_s: float
    api_provider: dict   # {"authMode": ..., "model": ..., "reasoning_effort": ...}
    agent_id: str        # label used in audit/report files

Return value: a ``dict`` with at least::

    status: str          # "ok" on success; any other value means failure
    errorMessage: str    # human-readable failure text (used when status != "ok")
    trace: dict          # {"collection": {"complete": True, "threadId": ...},
                         #  "executionTrace": [normalised tool events, ...]}
    durationMs: int      # optional, recorded in the private run audit

Fail-closed rules the orchestrators apply after every call:

* ``status != "ok"`` aborts the role.
* ``trace.collection.complete is not True`` aborts the role — an incomplete
  execution trace must never be turned into a successful candidate.
* the runner must not write to ``work_dir``: a role may only create files under
  ``<work_dir>/output``.  The adapter is responsible for hashing the workspace
  *inside the runtime* before and after the call, because a host-side copy is
  never touched by a sandboxed agent.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Protocol, runtime_checkable

ENV_VAR = "ENVGEN_RUNNER"

__all__ = [
    "ENV_VAR",
    "RoleRunner",
    "RunnerContractError",
    "load_runner",
    "runner_spec",
]


class RunnerContractError(RuntimeError):
    """Raised when the configured runner cannot be imported or is not callable."""


@runtime_checkable
class RoleRunner(Protocol):
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
            "no role runner configured: pass --runner module:attr or set "
            f"{ENV_VAR} (e.g. {ENV_VAR}=workspace_env.agentkit_runner:run)."
        )
    return spec


def load_runner(explicit: str | None = None) -> RoleRunner:
    """Import ``module:attr`` and return it as a :class:`RoleRunner`."""

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
            "Make sure the module is importable from the current Python environment."
        ) from exc
    candidate = getattr(module, attribute, None)
    if candidate is None:
        raise RunnerContractError(f"module {module_name!r} has no attribute {attribute!r}")
    if not callable(candidate):
        raise RunnerContractError(f"{spec!r} is not callable")
    return candidate  # type: ignore[return-value]
