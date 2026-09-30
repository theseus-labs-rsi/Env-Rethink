"""Task-scoped mail mock: IMAP + SMTP listeners plus a health endpoint.

All three listeners bind 127.0.0.1 only. The runner learns the assigned ports by
reading the ready file, which also carries the environment variables an IMAP/SMTP
client needs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

from . import __version__
from .core import FixtureError, MailStore
from .imap_server import start_imap_server
from .smtp_server import start_smtp_server


Json = Any


def client_environment(ready: dict[str, Json]) -> dict[str, str]:
    """Translate a ready file into the env vars IMAP/SMTP clients expect.

    These are the conventional names used by IMAP/SMTP tooling, so a client can be
    pointed at the mock without editing its configuration.
    """
    host = str(ready["host"])
    login = str(ready["login"])
    password = str(ready["password"])
    address = str(ready["address"])
    return {
        "IMAP_HOST": host,
        "IMAP_PORT": str(ready["imap_port"]),
        "IMAP_USER": login,
        "IMAP_PASS": password,
        "IMAP_TLS": "false",
        "IMAP_REJECT_UNAUTHORIZED": "false",
        "IMAP_MAILBOX": "INBOX",
        "SMTP_HOST": host,
        "SMTP_PORT": str(ready["smtp_port"]),
        "SMTP_SECURE": "false",
        "SMTP_USER": login,
        "SMTP_PASS": password,
        "SMTP_FROM": address,
        "SMTP_REJECT_UNAUTHORIZED": "false",
    }


async def _serve_health(store: MailStore, *, host: str, port: int) -> asyncio.Server:
    """A tiny HTTP endpoint so the runner can probe liveness the same way it does for other services."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            parts = request_line.decode("latin-1").split()
            authorized = True
            try:
                store.check_token(headers.get("authorization"))
            except Exception:
                authorized = False
            if len(parts) < 2 or parts[0] != "GET" or parts[1] != "/health":
                status, body = 404, {"errcode": 40400, "errmsg": "not found"}
            elif not authorized:
                status, body = 401, {"errcode": 40001, "errmsg": "invalid credential"}
            else:
                status, body = 200, {
                    "errcode": 0,
                    "errmsg": "ok",
                    "instance_id": store.instance_id,
                    "status": "ready",
                }
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            reason = {200: "OK", 401: "Unauthorized", 404: "Not Found"}[status]
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n".encode("latin-1")
                + payload
            )
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    return await asyncio.start_server(handle, host, port)


def _port_of(server: asyncio.Server) -> int:
    sockets = server.sockets or ()
    if not sockets:  # pragma: no cover - defensive
        raise RuntimeError("server is not bound to a socket")
    return int(sockets[0].getsockname()[1])


def _write_ready_file(path: Path, value: dict[str, Json]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


async def _serve(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    ready_file = Path(args.ready_file).resolve()
    try:
        ready_file.relative_to(state_dir)
    except ValueError:
        raise SystemExit("--ready-file must live inside --state-dir")

    store = MailStore(
        fixture_path=Path(args.fixture),
        blobs_dir=Path(args.blobs),
        workspace_root=Path(args.workspace_root),
        state_dir=state_dir,
    )
    host = "127.0.0.1"
    imap_server = await start_imap_server(store, host=host, port=max(0, int(args.imap_port)))
    smtp_server = await start_smtp_server(store, host=host, port=max(0, int(args.smtp_port)))
    health_server = await _serve_health(store, host=host, port=max(0, int(args.health_port)))

    account = store.account
    ready = {
        "schema_version": 1,
        "instance_id": store.instance_id,
        "host": host,
        "imap_port": _port_of(imap_server),
        "smtp_port": _port_of(smtp_server),
        "base_url": f"http://{host}:{_port_of(health_server)}",
        "token": store.token,
        "login": account["login"],
        "password": account["password"],
        "address": account["address"],
        "workspace_root": str(store.workspace_root),
    }
    ready["environment"] = client_environment(ready)
    _write_ready_file(ready_file, ready)
    store.mark_ready()

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent
            pass

    print(
        f"mail mock ready: imap={ready['imap_port']} smtp={ready['smtp_port']} "
        f"health={ready['base_url']}",
        flush=True,
    )
    abnormal = False
    try:
        await stopping.wait()
    except asyncio.CancelledError:  # pragma: no cover - defensive
        abnormal = True
    finally:
        for server in (imap_server, smtp_server, health_server):
            server.close()
        for server in (imap_server, smtp_server, health_server):
            try:
                await server.wait_closed()
            except Exception:  # pragma: no cover - defensive
                abnormal = True
        store.close(abnormal=abnormal)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task-scoped mail mock (IMAP + SMTP).")
    parser.add_argument("--version", action="version", version=f"mail-mock {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Serve one task's mailbox.")
    serve.add_argument("--fixture", required=True)
    serve.add_argument("--blobs", required=True)
    serve.add_argument("--workspace-root", required=True)
    serve.add_argument("--state-dir", required=True)
    serve.add_argument("--ready-file", required=True)
    serve.add_argument("--imap-port", type=int, default=0)
    serve.add_argument("--smtp-port", type=int, default=0)
    serve.add_argument("--health-port", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_serve(args))
    except FixtureError as exc:
        print(f"mail-mock: invalid fixture: {exc}", flush=True)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
