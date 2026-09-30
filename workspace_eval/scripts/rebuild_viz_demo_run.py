#!/usr/bin/env python3
"""Rebuild the viz demo run by replaying a real trace against the live WeCom mock.

The committed demo run was recorded before the audit log gained response
projections and ``request_id`` (schemaVersion 2), so the replay UI cannot render
it. Rather than hand-write a synthetic fixture, this script replays the real
agent's exact ``wecom-cli`` call sequence against the current mock service and
rewrites the trace with the ``request_id`` values the service actually issued.

The result is a demo run whose terminal replay is the real agent transcript and
whose WeCom panel is driven by a real ``wecom-service-events.jsonl``.

Usage (from the repository root):

    uv run --project evaluation --frozen --no-sync \
        python evaluation/scripts/rebuild_viz_demo_run.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "evaluation"
sys.path.insert(0, str(EVAL / "src"))

from workspace_services.wecom.core import WeComStore  # noqa: E402
from workspace_services.wecom.server import create_server  # noqa: E402

TASK_ID = "382"
RUN_REL = Path("Codex--Qwen3.8-Max--Tasks-New") / TASK_ID
DEMO_ROOT = REPO / "viz" / "tests" / "fixtures" / "demo-output"
SOURCE_TRACE = DEMO_ROOT / RUN_REL / "agent.json"

Json = Any


def wecom_invocations(command: str) -> list[tuple[str, str | None]]:
    """Return (operation, raw json arg) for each wecom-cli call in a command.

    The agent writes shell loops (``for m in a b c; do wecom-cli ... "$m"; done``)
    whose arguments interpolate variables, so the raw argument is only a hint;
    callers fall back to the recorded response when it cannot be parsed.
    """
    invocations: list[tuple[str, str | None]] = []
    for match in re.finditer(r"wecom-cli\s+(\w+)\s+(\w+)", command):
        operation = f"{match.group(1)}.{match.group(2)}"
        tail = command[match.end():]
        arg = re.search(r"'(\{.*?\})'|\"(\{.*?\})\"", tail)
        raw = None
        if arg:
            raw = (arg.group(1) or arg.group(2) or "").replace('\\"', '"')
        invocations.append((operation, raw))
    return invocations


def recorded_responses(output: Json) -> list[dict[str, Json]]:
    """Parse the JSON responses the agent saw in one tool event's stdout."""
    if not isinstance(output, str):
        return []
    responses: list[dict[str, Json]] = []
    for line in output.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            responses.append(value)
    return responses


def params_for(operation: str, raw: str | None, recorded: dict[str, Json]) -> dict[str, Json] | None:
    """Reconstruct the request params for one call.

    Prefers the literal argument from the command; when that is unparseable
    (shell interpolation) it derives the params from the recorded response.
    """
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    # Fall back to the response: media downloads echo their media_id.
    media = recorded.get("media_item")
    if operation == "msg.get_msg_media" and isinstance(media, dict):
        media_id = media.get("media_id")
        if isinstance(media_id, str):
            return {"media_id": media_id}
    return None


def main() -> int:
    if not SOURCE_TRACE.is_file():
        print(f"source trace not found: {SOURCE_TRACE}", file=sys.stderr)
        return 1

    trace_document = json.loads(SOURCE_TRACE.read_text(encoding="utf-8"))
    events = trace_document["trace"]["executionTrace"]

    workspace = DEMO_ROOT / RUN_REL / "_workdir"
    raw_dir = DEMO_ROOT / RUN_REL / "raw"
    state_dir = raw_dir / "workspace-services-private" / "wecom"
    for directory in (workspace, state_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    store = WeComStore(
        fixture_path=EVAL / "tasks_new" / TASK_ID / "services" / "wecom.json",
        blobs_dir=EVAL / "tasks_new" / TASK_ID / "services" / "blobs",
        workspace_root=workspace,
        state_dir=state_dir,
    )
    server = create_server(store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store.mark_ready()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1/execute"

    def call(operation: str, params: dict[str, Json]) -> dict[str, Json]:
        request = urllib.request.Request(
            base_url,
            data=json.dumps(
                {"operation": operation, "params": params, "cwd": str(workspace)},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {store.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    # Cursors and document task ids are per-instance, so a recorded value cannot
    # be reused. Track the live ones per (operation, chat/doc) instead.
    cursors: dict[str, str] = {}
    doc_tasks: dict[str, str] = {}
    replayed = skipped = 0

    for event in events:
        if not isinstance(event, dict) or event.get("type") != "tool":
            continue
        command = (event.get("input") or {}).get("command")
        if not isinstance(command, str) or "wecom-cli" not in command:
            continue
        if event.get("status") != "completed":
            continue

        recorded = recorded_responses(event.get("output"))
        invocations = wecom_invocations(command)
        # A `for` loop produces one invocation pattern but many responses.
        if len(recorded) > len(invocations) and invocations:
            invocations = invocations[:1] * len(recorded)

        fresh: list[str] = []
        for position, (operation, raw) in enumerate(invocations):
            recorded_response = recorded[position] if position < len(recorded) else {}
            if operation == "auth.show":
                operation = "auth.status"

            params = params_for(operation, raw, recorded_response)
            if params is None:
                skipped += 1
                continue

            key = str(params.get("chatid") or params.get("url") or params.get("docid") or "")
            if params.pop("cursor", None) is not None:
                live_cursor = cursors.get(f"{operation}:{key}")
                if live_cursor is None:
                    skipped += 1
                    continue
                params["cursor"] = live_cursor
            if params.get("task_id") is not None:
                live_task = doc_tasks.get(key)
                if live_task is None:
                    skipped += 1
                    continue
                params["task_id"] = live_task

            response = call(operation, params)
            replayed += 1

            next_cursor = response.get("next_cursor")
            if isinstance(next_cursor, str) and next_cursor:
                cursors[f"{operation}:{key}"] = next_cursor
            task_id = response.get("task_id")
            if isinstance(task_id, str) and task_id:
                doc_tasks[key] = task_id

            if operation == "auth.status":
                fresh.append("authorized" if response.get("errcode") == 0 else "unauthorized")
            else:
                fresh.append(json.dumps(response, ensure_ascii=False))

        if fresh:
            # Keep the agent's original framing (the `=== id ===` separators the
            # loop echoed) but swap in responses that carry real request_ids.
            separator = "\n"
            if isinstance(event.get("output"), str) and "=== " in event["output"]:
                separator = "\n"
            event["output"] = separator.join(fresh) + "\n"

    server.shutdown()

    SOURCE_TRACE.write_text(
        json.dumps(trace_document, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    events_path = state_dir / "wecom-service-events.jsonl"
    audit_target = raw_dir / "wecom-service-events.jsonl"
    shutil.copy2(events_path, audit_target)
    # The private state dir is a runtime artifact; only the collected log ships.
    shutil.rmtree(state_dir.parent)
    shutil.rmtree(workspace)

    audit_lines = audit_target.read_text(encoding="utf-8").strip().splitlines()
    print(f"replayed={replayed} skipped={skipped} audit_events={len(audit_lines)}")
    print(f"trace:  {SOURCE_TRACE.relative_to(REPO)}")
    print(f"audit:  {audit_target.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
