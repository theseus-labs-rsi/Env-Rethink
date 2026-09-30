"""MCP sidecar for the generated workspace environment layer.

Serves three read-only tools over streamable HTTP — ``workspace_map``,
``workspace_search`` and ``event_search`` — plus the ``/health`` probe and the
``ready.json`` handshake that the Workspace-Bench task runner needs in order to
supervise it as a workspace service::

    python -m workspace_env.server \
        --fixture <task>/services/workspace-env/fixture.json \
        --blobs <task>/services/workspace-env/blobs \
        --workspace-root <work_dir> \
        --state-dir <raw>/workspace-services-private/workspace_env \
        --ready-file <state_dir>/ready.json --port 0

Artifacts written under ``--state-dir`` and collected by
``WorkspaceServiceManager``: ``workspace_env-service-manifest.json``,
``workspace_env-service-events.jsonl`` (one line per MCP tool call) and
``workspace_env-service-health.json`` (``status="stopped"`` on clean exit).
Everything else — staged artifacts, audit logs, cursors — lives under
``<state-dir>/artifacts``, deliberately outside the agent's workspace.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .manifest import canonical_json, sha256_text
from .manifest_staging import ManifestStagingError, stage_environment_layer
from .runtime import WorkspaceRuntime

SERVICE_NAME = "workspace_env"
READY_SCHEMA_VERSION = 1
TOOL_NAMES = ("workspace_map", "workspace_search", "event_search")

EVENT_SEARCH_DESCRIPTION = (
    "Search visible workspace history by path, literal keywords, operation category, and inclusive time range. Keywords "
    "are OR; other filters are AND. A path also summarizes same-session related files. For a truncated result, pass its "
    "cursor alone to get the next page. Set detail=true only to read public excerpts from matching file.read or file.write "
    "events; omit it for the default summary. Returns bounded plain text."
)
WORKSPACE_SEARCH_DESCRIPTION = (
    "Navigate the reviewed workspace collection map in one of three modes. Use path when the task gives a concrete file "
    "name or workspace-relative file path; it returns the matching collection card and overview, canonical file path, "
    "and compact file overview. After workspace_map, normally pass the chosen card_id directly to browse that "
    "collection's paths. Use query only as a fallback when the complete map has no clear candidate; whitespace-separated "
    "business/topic keywords form an OR union and return ranked collection-card summaries without member paths. "
    "query, path, card_id, and cursor are mutually exclusive. Continue an incomplete result using only its cursor. "
    "Results are bounded plain-text navigation, not full file content."
)
WORKSPACE_MAP_DESCRIPTION = (
    "Return the complete workspace collection-card directory in one unpaginated plain-text result. Call this once at "
    "the start of every task before reading files. It lists every card's card_id, title, description, and file count, "
    "but never member paths or file contents. Normally choose relevant cards and call workspace_search with card_id; "
    "use path for a concrete name/path and query only when the map has no clear candidate."
)


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, expected: str) -> None:
        self.expected = expected

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self.expected):
            return None
        return AccessToken(token=token, client_id="workspace-bench-task", scopes=[])


def build_server(runtime: WorkspaceRuntime, *, host: str, port: int, bearer_token: str) -> FastMCP:
    base = f"http://{host}:{port}"
    server = FastMCP(
        SERVICE_NAME,
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        auth=AuthSettings(issuer_url=base, resource_server_url=base + "/mcp", required_scopes=[]),
        token_verifier=StaticTokenVerifier(bearer_token),
        log_level="WARNING",
    )
    readonly = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    def workspace_map_tool(
        note: Annotated[
            str,
            Field(description="Optional free-form note. The server ignores it; omit it."),
        ] = "",
    ) -> Any:
        """Workspace map wrapper.

        ``note`` is accepted and ignored: some gateways reject a tool whose
        parameter object is empty ("functionDeclaration parameters schema should
        be of type OBJECT"), so the map tool advertises one harmless optional
        argument instead of none.
        """

        del note
        return runtime.workspace_map_text()

    server.add_tool(
        workspace_map_tool,
        name="workspace_map",
        description=WORKSPACE_MAP_DESCRIPTION,
        annotations=readonly,
        structured_output=False,
    )
    server.add_tool(
        runtime.workspace_search_text,
        name="workspace_search",
        description=WORKSPACE_SEARCH_DESCRIPTION,
        annotations=readonly,
        structured_output=False,
    )
    server.add_tool(
        runtime.event_search_text,
        name="event_search",
        description=EVENT_SEARCH_DESCRIPTION,
        annotations=readonly,
        structured_output=False,
    )
    return server


def tool_schema(server: FastMCP) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for tool in sorted(server._tool_manager.list_tools(), key=lambda item: item.name):
        schemas.append(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.parameters,
                "annotations": (
                    tool.annotations.model_dump(mode="json") if tool.annotations is not None else None
                ),
            }
        )
    return schemas


def _write_json_atomic(path: Path, value: dict[str, Any], *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)


def _audit_events(artifact_root: Path) -> list[dict[str, Any]]:
    """Project the private MCP call audit into the service-visible event list."""

    events: list[dict[str, Any]] = []
    audit_path = artifact_root / "mcp_calls.jsonl"
    if not audit_path.is_file():
        return events
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        events.append(
            {
                "timestamp": record.get("timestamp"),
                "operation": str(record.get("tool") or "unknown"),
                "requestId": record.get("request_id"),
                "status": record.get("status"),
                "errorCode": record.get("error_code"),
                "path": record.get("path"),
                "returnedTokens": record.get("returned_tokens"),
                "latencyMs": record.get("latency_ms"),
            }
        )
    return events


class SidecarState:
    def __init__(self, *, state_dir: Path, artifact_root: Path, instance_id: str) -> None:
        self.state_dir = state_dir
        self.artifact_root = artifact_root
        self.instance_id = instance_id
        self.started_at = time.time()
        self.closed = False
        self.summary: dict[str, Any] = {}

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / f"{SERVICE_NAME}-service-manifest.json"

    @property
    def events_path(self) -> Path:
        return self.state_dir / f"{SERVICE_NAME}-service-events.jsonl"

    @property
    def health_path(self) -> Path:
        return self.state_dir / f"{SERVICE_NAME}-service-health.json"

    def write_manifest(self) -> None:
        _write_json_atomic(
            self.manifest_path,
            {
                "schemaVersion": 1,
                "instanceId": self.instance_id,
                "fixture": self.summary.get("fixture_path"),
                "workspaceRoot": self.summary.get("input_root"),
                "artifactRoot": str(self.artifact_root),
                "toolSchemaHash": self.summary.get("tool_schema_hash"),
                "collectionSetSha256": self.summary.get("collection_set_sha256"),
                "memberIndexSha256": self.summary.get("member_index_sha256"),
                "eventLogSha256": self.summary.get("events_sha256"),
                "memberCount": self.summary.get("member_count"),
                "workspaceSnapshotHash": self.summary.get("workspace_snapshot_hash"),
                "generator": self.summary.get("generator"),
                "tools": list(TOOL_NAMES),
            },
            mode=0o600,
        )

    def write_events(self) -> None:
        events = _audit_events(self.artifact_root)
        temporary = self.events_path.with_name(self.events_path.name + ".tmp")
        temporary.write_text(
            "".join(canonical_json(event) + "\n" for event in events), encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.events_path)

    def write_health(self, status: str) -> None:
        events = _audit_events(self.artifact_root)
        operations = Counter(str(event.get("operation")) for event in events)
        error_count = sum(1 for event in events if event.get("status") not in (None, "ok"))
        _write_json_atomic(
            self.health_path,
            {
                "schemaVersion": 1,
                "instanceId": self.instance_id,
                "status": status,
                "pid": os.getpid(),
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "requestCount": len(events),
                "errorCount": error_count,
                "closed": self.closed,
                "operations": dict(sorted(operations.items())),
            },
            mode=0o600,
        )


async def _health_response(scope: dict[str, Any], receive: Any, send: Any, *, token: str, instance_id: str) -> None:
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])
    }
    payload: dict[str, Any]
    if hmac.compare_digest(headers.get("authorization", ""), f"Bearer {token}"):
        payload = {"errcode": 0, "errmsg": "ok", "status": "ready", "instance_id": instance_id}
        status = 200
    else:
        payload = {"errcode": 401, "errmsg": "unauthorized"}
        status = 401
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _health_routing_app(mcp_app: Any, *, token: str, instance_id: str) -> Any:
    """Serve ``/health`` locally and forward every other request to the MCP app."""

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").rstrip("/") == "/health":
            await _health_response(scope, receive, send, token=token, instance_id=instance_id)
            return
        await mcp_app(scope, receive, send)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the workspace_env MCP sidecar")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--blobs", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    instance_id = uuid.uuid4().hex
    state = SidecarState(
        state_dir=state_dir,
        artifact_root=(state_dir / "artifacts").resolve(),
        instance_id=instance_id,
    )
    state.write_health("starting")

    try:
        resolved, _, staging = stage_environment_layer(
            fixture_path=Path(args.fixture).resolve(),
            blobs_root=Path(args.blobs).resolve(),
            workspace_root=Path(args.workspace_root).resolve(),
            state_dir=state_dir,
            run_id=args.run_id or f"{SERVICE_NAME}-{instance_id[:8]}",
            task_id=args.task_id or "unknown",
        )
    except ManifestStagingError as exc:
        state.closed = True
        state.write_health("error")
        print(f"environment staging failed: {exc}", file=sys.stderr)
        return 2

    runtime = WorkspaceRuntime(resolved)
    token = secrets.token_urlsafe(32)
    server = build_server(runtime, host=args.host, port=args.port, bearer_token=token)
    tool_schema_hash = sha256_text(canonical_json(tool_schema(server)))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(128)
    port = int(listener.getsockname()[1])

    import uvicorn

    app = _health_routing_app(server.streamable_http_app(), token=token, instance_id=instance_id)
    http_server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=port, log_level="warning", access_log=False)
    )
    stop = threading.Event()

    def _request_stop(signum: int, _frame: Any) -> None:
        del signum
        stop.set()
        http_server.should_exit = True
        state.closed = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    thread = threading.Thread(
        target=http_server.run, kwargs={"sockets": [listener]}, name="workspace-env-uvicorn"
    )
    thread.start()
    if not _wait_for_health(host=args.host, port=port, token=token, timeout=20.0):
        stop.set()
        http_server.should_exit = True
        state.closed = True
        state.write_health("error")
        print("workspace_env sidecar did not become healthy", file=sys.stderr)
        return 3

    state.summary = {
        "fixture_path": str(Path(args.fixture).resolve()),
        "input_root": str(Path(args.workspace_root).resolve()),
        "tool_schema_hash": tool_schema_hash,
        "member_count": staging.get("member_count"),
        "workspace_snapshot_hash": staging.get("workspace_snapshot_hash"),
        "generator": staging.get("generator"),
        "collection_set_sha256": resolved.manifest.data.workspace_collection_set_sha256,
        "member_index_sha256": resolved.manifest.data.workspace_collection_search_index_sha256,
        "events_sha256": resolved.manifest.context.visible_event_log_sha256,
    }
    state.write_manifest()
    state.write_events()
    state.write_health("ready")

    _write_json_atomic(
        Path(args.ready_file),
        {
            "schema_version": READY_SCHEMA_VERSION,
            "instance_id": instance_id,
            "base_url": f"http://{args.host}:{port}",
            "token": token,
            "mcp_path": "/mcp",
            "tool_schema_hash": tool_schema_hash,
        },
        mode=0o600,
    )

    try:
        stop.wait()
    except KeyboardInterrupt:
        stop.set()
    finally:
        http_server.should_exit = True
        thread.join(timeout=30)
        try:
            listener.close()
        except OSError:
            pass
        state.closed = True
        try:
            state.write_events()
        finally:
            state.write_health("stopped")
        runtime.close()
    return 0


def _wait_for_health(*, host: str, port: int, token: str, timeout: float) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        f"http://{host}:{port}/health", headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ready":
                return True
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
