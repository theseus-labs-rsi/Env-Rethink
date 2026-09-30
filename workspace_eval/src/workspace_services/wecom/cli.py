from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .core import ERR_INVALID_ARGUMENT, ERR_UNAUTHORIZED, ERR_UNSUPPORTED


Json = Any


def _emit(value: dict[str, Json]) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _error(errcode: int, errmsg: str, *, exit_code: int) -> int:
    _emit({"errcode": errcode, "errmsg": errmsg})
    return exit_code


def _load_config() -> dict[str, Json]:
    config_path = os.environ.get("WECOM_MOCK_CONFIG")
    if not config_path:
        raise ValueError("WECOM_MOCK_CONFIG is not set")
    value = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("mock config must be an object")
    base_url = value.get("base_url")
    token = value.get("token")
    if not isinstance(base_url, str) or not base_url.startswith("http://127.0.0.1:"):
        raise ValueError("mock config contains an invalid base_url")
    if not isinstance(token, str) or not token:
        raise ValueError("mock config contains an invalid token")
    return value


def _request(config: dict[str, Json], operation: str, params: dict[str, Json]) -> dict[str, Json]:
    payload = json.dumps(
        {"operation": operation, "params": params, "cwd": os.getcwd()},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        str(config["base_url"]).rstrip("/") + "/v1/execute",
        data=payload,
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read().decode("utf-8"))
        except Exception:
            value = {"errcode": ERR_UNAUTHORIZED, "errmsg": f"service returned HTTP {exc.code}"}
    if not isinstance(value, dict):
        raise ValueError("service returned a non-object response")
    return value


def _operation_from_args(args: list[str]) -> tuple[str, str | None]:
    if len(args) < 2:
        return "", None
    domain, command = args[0], args[1]
    mapping = {
        ("contact", "get_userlist"): "contact.get_userlist",
        ("msg", "get_msg_chat_list"): "msg.get_msg_chat_list",
        ("msg", "get_message"): "msg.get_message",
        ("msg", "get_msg_media"): "msg.get_msg_media",
        ("doc", "get_doc_content"): "doc.get_doc_content",
    }
    return mapping.get((domain, command), f"{domain}.{command}"), args[2] if len(args) >= 3 else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"wecom-cli mock {__version__}")
        return 0
    if args == ["auth", "show", "--auth-status"]:
        try:
            config = _load_config()
            response = _request(config, "auth.status", {})
            print("authorized" if response.get("errcode") == 0 else "unauthorized")
            return 0 if response.get("errcode") == 0 else 1
        except Exception:
            print("unauthorized")
            return 1

    operation, raw_params = _operation_from_args(args)
    if not operation:
        return _error(ERR_INVALID_ARGUMENT, "usage: wecom-cli <domain> <command> '<json object>'", exit_code=2)
    if len(args) != 3:
        if operation not in {
            "contact.get_userlist",
            "msg.get_msg_chat_list",
            "msg.get_message",
            "msg.get_msg_media",
            "doc.get_doc_content",
        }:
            return _error(ERR_UNSUPPORTED, f"unsupported operation: {operation}", exit_code=1)
        return _error(ERR_INVALID_ARGUMENT, "exactly one JSON argument is required", exit_code=2)
    try:
        params = json.loads(raw_params or "")
    except json.JSONDecodeError as exc:
        return _error(ERR_INVALID_ARGUMENT, f"invalid JSON argument: {exc.msg}", exit_code=2)
    if not isinstance(params, dict):
        return _error(ERR_INVALID_ARGUMENT, "JSON argument must be an object", exit_code=2)
    try:
        config = _load_config()
        response = _request(config, operation, params)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"wecom-cli: {exc}", file=sys.stderr)
        return _error(ERR_UNAUTHORIZED, "mock service unavailable", exit_code=1)
    _emit(response)
    return 0 if response.get("errcode") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
