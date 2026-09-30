from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .core import ERR_INTERNAL, ServiceError, WeComStore


Json = Any


class WeComHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: WeComStore) -> None:
        self.store = store
        super().__init__(address, WeComRequestHandler)


class WeComRequestHandler(BaseHTTPRequestHandler):
    server: WeComHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, value: dict[str, Json]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"errcode": 40400, "errmsg": "not found"})
            return
        try:
            self.server.store.check_token(self.headers.get("Authorization"))
        except ServiceError as exc:
            self._send(exc.status, exc.response())
            return
        self._send(
            200,
            {
                "errcode": 0,
                "errmsg": "ok",
                "instance_id": self.server.store.instance_id,
                "status": "ready",
            },
        )

    def do_POST(self) -> None:
        if self.path != "/v1/execute":
            self._send(404, {"errcode": 40400, "errmsg": "not found"})
            return
        started = time.monotonic()
        operation = "unknown"
        params: Json = {}
        status = 200
        # 每次请求一个 id：既回写进响应信封（真实 API 也常有），也写进审计日志，
        # 使「agent 看到的输出」与「服务端记录」可以精确 join，而不必依赖调用序或时间戳。
        request_id = uuid.uuid4().hex
        try:
            self.server.store.check_token(self.headers.get("Authorization"))
            content_length = int(self.headers.get("Content-Length") or "0")
            if content_length < 0 or content_length > 1024 * 1024:
                raise ServiceError(40058, "request body is too large")
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ServiceError(40058, "request body must be an object")
            operation = payload.get("operation")
            if not isinstance(operation, str) or not operation:
                raise ServiceError(40058, "operation is required")
            params = payload.get("params", {})
            response = self.server.store.execute(operation, params, cwd=payload.get("cwd"))
        except ServiceError as exc:
            status = exc.status
            response = exc.response()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            response = {"errcode": 40058, "errmsg": f"invalid request: {exc}"}
        except Exception:
            status = 500
            response = {"errcode": ERR_INTERNAL, "errmsg": "internal error"}
        duration_ms = int((time.monotonic() - started) * 1000)
        if isinstance(response, dict):
            response["request_id"] = request_id
        self.server.store.audit(
            operation=operation,
            params=params,
            response=response,
            duration_ms=duration_ms,
            request_id=request_id,
        )
        self._send(status, response)


def create_server(store: WeComStore, *, port: int = 0) -> WeComHTTPServer:
    return WeComHTTPServer(("127.0.0.1", port), store)


def _write_ready_file(path: Path, *, store: WeComStore, server: WeComHTTPServer) -> None:
    value = {
        "schema_version": 1,
        "instance_id": store.instance_id,
        "base_url": f"http://127.0.0.1:{server.server_address[1]}",
        "token": store.token,
        "workspace_root": str(store.workspace_root),
    }
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the single-container Workspace-Bench WeCom mock.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--fixture", required=True)
    serve.add_argument("--blobs", required=True)
    serve.add_argument("--workspace-root", required=True)
    serve.add_argument("--state-dir", required=True)
    serve.add_argument("--ready-file", required=True)
    serve.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    ready_path = Path(args.ready_file).resolve()
    state_path = Path(args.state_dir).resolve()
    try:
        ready_path.relative_to(state_path)
    except ValueError:
        raise SystemExit("--ready-file must be inside --state-dir")
    store = WeComStore(
        fixture_path=Path(args.fixture),
        blobs_dir=Path(args.blobs),
        workspace_root=Path(args.workspace_root),
        state_dir=state_path,
    )
    server = create_server(store, port=max(0, int(args.port)))
    _write_ready_file(ready_path, store=store, server=server)
    store.mark_ready()

    stopping = threading.Event()

    def stop_server(signum: int, frame: object) -> None:
        del signum, frame
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)
    abnormal = False
    try:
        server.serve_forever(poll_interval=0.2)
    except BaseException:
        abnormal = True
        raise
    finally:
        server.server_close()
        store.close(abnormal=abnormal)


if __name__ == "__main__":
    main()
