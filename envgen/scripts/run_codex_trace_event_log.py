"""Run a real read-only Codex session and export its exact shell-event history."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.runner import RunnerContractError, load_runner
from workspace_env.codex_trace_events import (
    CODEX_TRACE_EVENT_GENERATOR_VERSION,
    CodexTraceConversionError,
    convert_codex_execution_trace,
    write_codex_trace_event_log,
)
from workspace_env.integration import workspace_snapshot_hash


READ_ONLY_AUDIT_PROMPT = """Perform a read-only workspace audit using the native shell.
Do not create, modify, move, rename, or delete anything. Do not access the network.
First run one bounded discovery command to obtain actual regular-file paths. Then
select at least three paths from that command's output, from different directories
or file types, and inspect each with a separate bounded shell command. Use file for
binary formats and head or sed for text formats. Do not finish after discovery alone.
Do not read task prompts, rubrics, reference answers, or files outside the current workspace.
In the final response, list only the paths you actually inspected and a short factual
description of each. Do not claim to have inspected any file you did not open.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Codex and produce a trace-derived event log.")
    parser.add_argument("--workspace-root", required=True, help="source workspace snapshot; Codex receives only a copy")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--runner", help="Codex runner spec 'module:attr' (default: $ENVGEN_RUNNER); see docs/RUNNER_CONTRACT.md")
    return parser.parse_args()


def _write_private_result(path: Path, result: dict[str, object]) -> None:
    path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    args = parse_args()
    try:
        runner = load_runner(args.runner)
    except RunnerContractError as exc:
        print(f"codex runner is not configured: {exc}", file=sys.stderr)
        return 2
    if os.environ.get("CODEX_SANDBOX_MODE") != "danger-full-access":
        print("CODEX_SANDBOX_MODE must be danger-full-access to preserve the native Docker shell", file=sys.stderr)
        return 2
    workspace = Path(args.workspace_root).resolve(strict=True)
    output = Path(args.output_root).resolve()
    if output.exists():
        print("output root must not already exist", file=sys.stderr)
        return 2
    try:
        output.relative_to(workspace)
    except ValueError:
        pass
    else:
        print("output root must be outside the workspace", file=sys.stderr)
        return 2
    output.mkdir(parents=True, mode=0o700)
    source_snapshot_hash = workspace_snapshot_hash(str(workspace))
    run_workspace = output / "isolated_workspace.private"
    shutil.copytree(workspace, run_workspace, symlinks=True)
    runtime_root = output / "codex_runtime.private"
    before_snapshot_hash = workspace_snapshot_hash(str(run_workspace))
    if before_snapshot_hash != source_snapshot_hash:
        print("isolated workspace copy does not match the source snapshot", file=sys.stderr)
        return 2
    result = runner(
        prompt=READ_ONLY_AUDIT_PROMPT,
        work_dir=str(run_workspace),
        sandbox_dir=str(runtime_root),
        timeout_s=args.timeout_seconds,
        api_provider={
            "authMode": "chatgpt",
            "model": args.model,
            "__codex_runtime__": {
                "expected_cli_version": "0.144.5",
                "protocol": "responses",
                "mcp_servers": {},
                "tool_schemas": {},
            },
        },
        agent_id="codex-trace-event-log",
    )
    _write_private_result(output / "codex_result.private.json", result)
    after_snapshot_hash = workspace_snapshot_hash(str(run_workspace))
    if after_snapshot_hash != before_snapshot_hash:
        print("isolated workspace changed during the Codex trace run", file=sys.stderr)
        return 3
    if workspace_snapshot_hash(str(workspace)) != source_snapshot_hash:
        print("source workspace changed during the Codex trace run", file=sys.stderr)
        return 3
    if result.get("status") != "ok":
        print(f"Codex run failed: {result.get('errorMessage')}", file=sys.stderr)
        return 3
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    collection = trace.get("collection") if isinstance(trace.get("collection"), dict) else {}
    if collection.get("complete") is not True:
        print("Codex trace collector did not report a complete JSONL trace", file=sys.stderr)
        return 4
    execution_trace = trace.get("executionTrace")
    if not isinstance(execution_trace, list) or not all(isinstance(item, dict) for item in execution_trace):
        print("Codex result has no normalized execution trace", file=sys.stderr)
        return 4
    successful_shell_commands = [
        item
        for item in execution_trace
        if item.get("type") == "tool" and item.get("tool") == "exec_command" and item.get("status") == "completed"
    ]
    if len(successful_shell_commands) < 4:
        print("Codex did not complete the required bounded discovery and file-inspection shell commands", file=sys.stderr)
        return 4
    try:
        event_log = convert_codex_execution_trace(
            execution_trace,
            workspace_snapshot_hash=before_snapshot_hash,
            session_material=str(collection.get("threadId") or ""),
            duration_ms=int(result.get("durationMs") or 0),
        )
        event_root = write_codex_trace_event_log(output / "event_log", event_log)
    except (CodexTraceConversionError, ValueError, OSError) as exc:
        print(f"Codex trace conversion failed: {exc}", file=sys.stderr)
        return 5
    (output / "run.private.json").write_text(
        json.dumps(
            {
                "event_generator_version": CODEX_TRACE_EVENT_GENERATOR_VERSION,
                "model": args.model,
                "workspace_root": str(workspace),
                "source_workspace_snapshot_hash": source_snapshot_hash,
                "isolated_workspace_snapshot_hash": before_snapshot_hash,
                "isolated_workspace_unchanged_after_run": after_snapshot_hash == before_snapshot_hash,
                "codex_status": result.get("status"),
                "trace_collection": collection,
                "event_log_root": str(event_root),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(output / "run.private.json", 0o600)
    shutil.rmtree(run_workspace)
    print(event_root)
    print(f"shell_events={event_log.source_tool_event_count} public_events={len(event_log.visible_events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
