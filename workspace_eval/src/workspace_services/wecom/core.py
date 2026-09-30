from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


Json = Any
SCHEMA_VERSION = 1
# 审计日志 schema：
#   1 = 仅元数据（operation/request/resourceIds/resultCount）
#   2 = 追加 requestId 与 result（响应投影），使回放可视化无需解析 agent stdout
AUDIT_SCHEMA_VERSION = 2
DEFAULT_PAGE_SIZE = 50
MAX_CONTACTS = 10

ERR_UNAUTHORIZED = 40001
ERR_FORBIDDEN = 40003
ERR_INVALID_ARGUMENT = 40058
ERR_NOT_FOUND = 40400
ERR_UNSUPPORTED = 48004
ERR_INTERNAL = 50000

SUPPORTED_OPERATIONS = {
    "auth.status": "wecom.auth_status",
    "contact.get_userlist": "wecom.get_userlist",
    "msg.get_msg_chat_list": "wecom.list_chats",
    "msg.get_message": "wecom.get_messages",
    "msg.get_msg_media": "wecom.download_media",
    "doc.get_doc_content": "wecom.get_document",
}


class FixtureError(ValueError):
    """Raised when a fixture cannot be loaded safely."""


class ServiceError(Exception):
    def __init__(self, errcode: int, errmsg: str, *, status: int = 200) -> None:
        super().__init__(errmsg)
        self.errcode = int(errcode)
        self.errmsg = str(errmsg)
        self.status = int(status)

    def response(self) -> dict[str, Json]:
        return {"errcode": self.errcode, "errmsg": self.errmsg}


def _require_mapping(value: Json, label: str) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise FixtureError(f"{label} must be an object")
    return value


def _require_list(value: Json, label: str) -> list[Json]:
    if not isinstance(value, list):
        raise FixtureError(f"{label} must be an array")
    return value


def _reject_unknown(value: dict[str, Json], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise FixtureError(f"{label} contains unknown field(s): {', '.join(unknown)}")


def _require_string(value: Json, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise FixtureError(f"{label} must be a non-empty string")
    return value


def _optional_string_list(value: Json, label: str) -> list[str] | None:
    if value is None:
        return None
    items = _require_list(value, label)
    out: list[str] = []
    for index, item in enumerate(items):
        out.append(_require_string(item, f"{label}[{index}]"))
    if len(out) != len(set(out)):
        raise FixtureError(f"{label} contains duplicate values")
    return out


def _parse_fixture_time(value: Json, label: str) -> tuple[str, str]:
    raw = _require_string(value, label)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise FixtureError(f"{label} must be an ISO-8601 datetime") from exc
    return raw, parsed.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _parse_query_time(value: Json, label: str) -> str:
    if not isinstance(value, str):
        raise ServiceError(ERR_INVALID_ARGUMENT, f"{label} must use YYYY-MM-DD HH:mm:ss")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ServiceError(ERR_INVALID_ARGUMENT, f"{label} must use YYYY-MM-DD HH:mm:ss") from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _blob_digest(value: Json, label: str) -> str:
    raw = _require_string(value, label)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", raw):
        raise FixtureError(f"{label} must use sha256:<64 lowercase hex characters>")
    return raw.split(":", 1)[1]


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _ensure_outside_workspace(path: Path, workspace_root: Path, label: str) -> None:
    resolved = path.resolve()
    if _path_is_within(resolved, workspace_root):
        raise FixtureError(f"{label} must not be inside workspace root")


def _safe_filename(value: str, media_id: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    if name in {"", ".", ".."}:
        name = media_id
    name = re.sub(r"[:*?\"<>|]", "_", name)
    return name[:240] or media_id


def validate_fixture(
    fixture_path: Path,
    blobs_dir: Path,
    workspace_root: Path,
) -> dict[str, Json]:
    fixture_path = fixture_path.resolve()
    blobs_dir = blobs_dir.resolve()
    workspace_root = workspace_root.resolve()
    if not fixture_path.is_file():
        raise FixtureError(f"fixture not found: {fixture_path}")
    if not blobs_dir.is_dir():
        raise FixtureError(f"blob directory not found: {blobs_dir}")
    if not workspace_root.is_dir():
        raise FixtureError(f"workspace root not found: {workspace_root}")
    _ensure_outside_workspace(fixture_path, workspace_root, "fixture")
    _ensure_outside_workspace(blobs_dir, workspace_root, "blob directory")

    try:
        root = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot read fixture: {exc}") from exc
    fixture = _require_mapping(root, "fixture")
    _reject_unknown(
        fixture,
        {
            "schema_version",
            "current_user_id",
            "settings",
            "users",
            "chats",
            "messages",
            "media",
            "documents",
            "faults",
        },
        "fixture",
    )
    if fixture.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError(f"unsupported schema_version: {fixture.get('schema_version')!r}")
    current_user_id = _require_string(fixture.get("current_user_id"), "current_user_id")

    settings = _require_mapping(fixture.get("settings", {}), "settings")
    _reject_unknown(settings, {"page_size"}, "settings")
    page_size = settings.get("page_size", DEFAULT_PAGE_SIZE)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 500:
        raise FixtureError("settings.page_size must be an integer between 1 and 500")

    faults = _require_list(fixture.get("faults", []), "faults")
    if faults:
        raise FixtureError("non-empty faults are not supported by the prototype")

    users: list[dict[str, Json]] = []
    user_ids: set[str] = set()
    for index, raw_user in enumerate(_require_list(fixture.get("users", []), "users")):
        user = _require_mapping(raw_user, f"users[{index}]")
        _reject_unknown(user, {"id", "name", "alias", "department", "visible_to"}, f"users[{index}]")
        user_id = _require_string(user.get("id"), f"users[{index}].id")
        if user_id in user_ids:
            raise FixtureError(f"duplicate user id: {user_id}")
        user_ids.add(user_id)
        users.append(
            {
                "id": user_id,
                "name": _require_string(user.get("name"), f"users[{index}].name"),
                "alias": _require_string(user.get("alias", ""), f"users[{index}].alias", allow_empty=True),
                "department": _require_string(
                    user.get("department", ""),
                    f"users[{index}].department",
                    allow_empty=True,
                ),
                "visible_to": _optional_string_list(user.get("visible_to"), f"users[{index}].visible_to"),
            }
        )
    if current_user_id not in user_ids:
        raise FixtureError("current_user_id does not reference a user")
    for user in users:
        visible_to = user["visible_to"]
        if visible_to is not None:
            unknown_users = sorted(set(visible_to) - user_ids)
            if unknown_users:
                raise FixtureError(
                    f"user {user['id']} visible_to references unknown user(s): {', '.join(unknown_users)}"
                )

    chats: list[dict[str, Json]] = []
    chat_ids: set[str] = set()
    for index, raw_chat in enumerate(_require_list(fixture.get("chats", []), "chats")):
        chat = _require_mapping(raw_chat, f"chats[{index}]")
        _reject_unknown(chat, {"id", "type", "name", "members"}, f"chats[{index}]")
        chat_id = _require_string(chat.get("id"), f"chats[{index}].id")
        if chat_id in chat_ids:
            raise FixtureError(f"duplicate chat id: {chat_id}")
        chat_ids.add(chat_id)
        chat_type = _require_string(chat.get("type"), f"chats[{index}].type")
        if chat_type not in {"direct", "group"}:
            raise FixtureError(f"chats[{index}].type must be direct or group")
        members = _optional_string_list(chat.get("members"), f"chats[{index}].members") or []
        unknown_members = sorted(set(members) - user_ids)
        if unknown_members:
            raise FixtureError(f"chat {chat_id} references unknown member(s): {', '.join(unknown_members)}")
        if chat_type == "direct":
            if len(members) != 2 or current_user_id not in members:
                raise FixtureError(f"direct chat {chat_id} must contain current user and exactly one peer")
            peer_id = next(item for item in members if item != current_user_id)
            if chat_id != peer_id:
                raise FixtureError(f"direct chat id must equal peer userid: expected {peer_id}, got {chat_id}")
        chats.append(
            {
                "id": chat_id,
                "type": chat_type,
                "name": _require_string(chat.get("name"), f"chats[{index}].name"),
                "members": members,
            }
        )

    copied_blobs: dict[str, dict[str, Json]] = {}

    def validate_blob(raw_blob: Json, label: str, expected_size: int | None = None) -> str:
        digest = _blob_digest(raw_blob, label)
        blob_path = blobs_dir / digest
        if not blob_path.is_file() or blob_path.is_symlink():
            raise FixtureError(f"{label} blob not found or is not a regular file: {digest}")
        actual_digest, actual_size = _sha256_file(blob_path)
        if actual_digest != digest:
            raise FixtureError(f"{label} blob digest mismatch: {digest}")
        if expected_size is not None and expected_size != actual_size:
            raise FixtureError(f"{label} size mismatch: expected {expected_size}, got {actual_size}")
        copied_blobs[digest] = {"source": str(blob_path), "size": actual_size}
        return digest

    media: list[dict[str, Json]] = []
    media_ids: set[str] = set()
    for index, raw_media in enumerate(_require_list(fixture.get("media", []), "media")):
        item = _require_mapping(raw_media, f"media[{index}]")
        _reject_unknown(
            item,
            {"id", "filename", "content_type", "blob", "size", "visible_to", "type"},
            f"media[{index}]",
        )
        media_id = _require_string(item.get("id"), f"media[{index}].id")
        if media_id in media_ids:
            raise FixtureError(f"duplicate media id: {media_id}")
        media_ids.add(media_id)
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise FixtureError(f"media[{index}].size must be a non-negative integer")
        media_type = _require_string(item.get("type", "file"), f"media[{index}].type")
        if media_type not in {"file", "image", "voice", "video"}:
            raise FixtureError(f"media[{index}].type is unsupported")
        media.append(
            {
                "id": media_id,
                "filename": _require_string(item.get("filename"), f"media[{index}].filename"),
                "content_type": _require_string(
                    item.get("content_type") or "application/octet-stream",
                    f"media[{index}].content_type",
                ),
                "blob": validate_blob(item.get("blob"), f"media[{index}].blob", size),
                "size": size,
                "visible_to": _optional_string_list(item.get("visible_to"), f"media[{index}].visible_to"),
                "type": media_type,
            }
        )
    for item in media:
        visible_to = item["visible_to"]
        if visible_to is not None:
            unknown_users = sorted(set(visible_to) - user_ids)
            if unknown_users:
                raise FixtureError(
                    f"media {item['id']} visible_to references unknown user(s): {', '.join(unknown_users)}"
                )

    documents: list[dict[str, Json]] = []
    document_ids: set[str] = set()
    document_urls: set[str] = set()
    for index, raw_document in enumerate(_require_list(fixture.get("documents", []), "documents")):
        item = _require_mapping(raw_document, f"documents[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "title",
                "url",
                "content_blob",
                "updated_at",
                "visible_to",
                "polls_before_ready",
            },
            f"documents[{index}]",
        )
        document_id = _require_string(item.get("id"), f"documents[{index}].id")
        url = _require_string(item.get("url"), f"documents[{index}].url")
        if document_id in document_ids:
            raise FixtureError(f"duplicate document id: {document_id}")
        if url in document_urls:
            raise FixtureError(f"duplicate document url: {url}")
        document_ids.add(document_id)
        document_urls.add(url)
        polls = item.get("polls_before_ready", 1)
        if isinstance(polls, bool) or not isinstance(polls, int) or polls < 0:
            raise FixtureError(f"documents[{index}].polls_before_ready must be a non-negative integer")
        updated_at, _ = _parse_fixture_time(item.get("updated_at"), f"documents[{index}].updated_at")
        documents.append(
            {
                "id": document_id,
                "title": _require_string(item.get("title"), f"documents[{index}].title"),
                "url": url,
                "content_blob": validate_blob(item.get("content_blob"), f"documents[{index}].content_blob"),
                "updated_at": updated_at,
                "visible_to": _optional_string_list(
                    item.get("visible_to"),
                    f"documents[{index}].visible_to",
                ),
                "polls_before_ready": polls,
            }
        )
    for item in documents:
        visible_to = item["visible_to"]
        if visible_to is not None:
            unknown_users = sorted(set(visible_to) - user_ids)
            if unknown_users:
                raise FixtureError(
                    f"document {item['id']} visible_to references unknown user(s): {', '.join(unknown_users)}"
                )

    messages: list[dict[str, Json]] = []
    message_ids: set[str] = set()
    for index, raw_message in enumerate(_require_list(fixture.get("messages", []), "messages")):
        item = _require_mapping(raw_message, f"messages[{index}]")
        _reject_unknown(
            item,
            {"id", "chat_id", "sender_id", "sent_at", "type", "text", "media_id"},
            f"messages[{index}]",
        )
        message_id = _require_string(item.get("id"), f"messages[{index}].id")
        if message_id in message_ids:
            raise FixtureError(f"duplicate message id: {message_id}")
        message_ids.add(message_id)
        chat_id = _require_string(item.get("chat_id"), f"messages[{index}].chat_id")
        sender_id = _require_string(item.get("sender_id"), f"messages[{index}].sender_id")
        if chat_id not in chat_ids:
            raise FixtureError(f"message {message_id} references unknown chat: {chat_id}")
        if sender_id not in user_ids:
            raise FixtureError(f"message {message_id} references unknown sender: {sender_id}")
        chat = next(candidate for candidate in chats if candidate["id"] == chat_id)
        if sender_id not in chat["members"]:
            raise FixtureError(f"message {message_id} sender is not a member of chat {chat_id}")
        message_type = _require_string(item.get("type"), f"messages[{index}].type")
        if message_type not in {"text", "file"}:
            raise FixtureError(f"messages[{index}].type must be text or file")
        text = None
        media_id = None
        if message_type == "text":
            text = _require_string(item.get("text"), f"messages[{index}].text", allow_empty=True)
            if item.get("media_id") is not None:
                raise FixtureError(f"text message {message_id} must not contain media_id")
        else:
            media_id = _require_string(item.get("media_id"), f"messages[{index}].media_id")
            if media_id not in media_ids:
                raise FixtureError(f"message {message_id} references unknown media: {media_id}")
            if item.get("text") is not None:
                raise FixtureError(f"file message {message_id} must not contain text")
        sent_at, send_time = _parse_fixture_time(item.get("sent_at"), f"messages[{index}].sent_at")
        messages.append(
            {
                "id": message_id,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "sent_at": sent_at,
                "send_time": send_time,
                "type": message_type,
                "text": text,
                "media_id": media_id,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "current_user_id": current_user_id,
        "settings": {"page_size": page_size},
        "users": users,
        "chats": chats,
        "messages": messages,
        "media": media,
        "documents": documents,
        "blobs": copied_blobs,
        "fixture_path": str(fixture_path),
        "blobs_dir": str(blobs_dir),
        "workspace_root": str(workspace_root),
    }


class WeComStore:
    def __init__(
        self,
        *,
        fixture_path: Path,
        blobs_dir: Path,
        workspace_root: Path,
        state_dir: Path,
    ) -> None:
        self.fixture_path = fixture_path.resolve()
        self.blobs_dir = blobs_dir.resolve()
        self.workspace_root = workspace_root.resolve()
        self.state_dir = state_dir.resolve()
        if self.state_dir.exists() and any(self.state_dir.iterdir()):
            raise FixtureError(f"state directory must be empty: {self.state_dir}")
        _ensure_outside_workspace(self.state_dir, self.workspace_root, "state directory")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        fixture = validate_fixture(self.fixture_path, self.blobs_dir, self.workspace_root)
        self.instance_id = f"wecom-{uuid.uuid4().hex}"
        self.token = secrets.token_urlsafe(32)
        self.cursor_secret = secrets.token_bytes(32)
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._request_count = 0
        self._error_count = 0
        self._event_sequence = 0
        self._closed = False
        self._fixture = fixture
        self.private_blobs_dir = self.state_dir / "blobs"
        self.private_blobs_dir.mkdir(mode=0o700)
        for digest, blob in fixture["blobs"].items():
            shutil.copyfile(blob["source"], self.private_blobs_dir / digest)
        self.database_path = self.state_dir / "wecom.sqlite3"
        self.events_path = self.state_dir / "wecom-service-events.jsonl"
        self.manifest_path = self.state_dir / "wecom-service-manifest.json"
        self.health_path = self.state_dir / "wecom-service-health.json"
        self._initialize_database()
        self._write_manifest()
        self._write_health("starting")

    @property
    def current_user_id(self) -> str:
        return str(self._fixture["current_user_id"])

    @property
    def page_size(self) -> int:
        return int(self._fixture["settings"]["page_size"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    department TEXT NOT NULL,
                    visible_to TEXT
                );
                CREATE TABLE chats (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    members TEXT NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id),
                    sender_id TEXT NOT NULL REFERENCES users(id),
                    sent_at TEXT NOT NULL,
                    send_time TEXT NOT NULL,
                    type TEXT NOT NULL,
                    text TEXT,
                    media_id TEXT
                );
                CREATE TABLE media (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    blob TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    visible_to TEXT,
                    type TEXT NOT NULL
                );
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT UNIQUE NOT NULL,
                    content_blob TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    visible_to TEXT,
                    polls_before_ready INTEGER NOT NULL
                );
                CREATE TABLE doc_poll_tasks (
                    task_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    poll_count INTEGER NOT NULL
                );
                CREATE TABLE audit_events (
                    sequence INTEGER PRIMARY KEY,
                    event_json TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        item["id"],
                        item["name"],
                        item["alias"],
                        item["department"],
                        json.dumps(item["visible_to"]) if item["visible_to"] is not None else None,
                    )
                    for item in self._fixture["users"]
                ],
            )
            connection.executemany(
                "INSERT INTO chats VALUES (?, ?, ?, ?)",
                [
                    (item["id"], item["type"], item["name"], json.dumps(item["members"]))
                    for item in self._fixture["chats"]
                ],
            )
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["id"],
                        item["chat_id"],
                        item["sender_id"],
                        item["sent_at"],
                        item["send_time"],
                        item["type"],
                        item["text"],
                        item["media_id"],
                    )
                    for item in self._fixture["messages"]
                ],
            )
            connection.executemany(
                "INSERT INTO media VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["id"],
                        item["filename"],
                        item["content_type"],
                        item["blob"],
                        item["size"],
                        json.dumps(item["visible_to"]) if item["visible_to"] is not None else None,
                        item["type"],
                    )
                    for item in self._fixture["media"]
                ],
            )
            connection.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["id"],
                        item["title"],
                        item["url"],
                        item["content_blob"],
                        item["updated_at"],
                        json.dumps(item["visible_to"]) if item["visible_to"] is not None else None,
                        item["polls_before_ready"],
                    )
                    for item in self._fixture["documents"]
                ],
            )

    def _write_json_atomic(self, path: Path, value: dict[str, Json]) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)

    def _write_manifest(self) -> None:
        fixture_digest, _ = _sha256_file(self.fixture_path)
        value = {
            "schemaVersion": 1,
            "fixtureSchemaVersion": SCHEMA_VERSION,
            "apiVersion": "v1",
            "cliVersion": "0.1.0",
            "instanceId": self.instance_id,
            "fixtureSha256": fixture_digest,
            "workspaceRoot": str(self.workspace_root),
            "counts": {
                "users": len(self._fixture["users"]),
                "chats": len(self._fixture["chats"]),
                "messages": len(self._fixture["messages"]),
                "media": len(self._fixture["media"]),
                "documents": len(self._fixture["documents"]),
                "blobs": len(self._fixture["blobs"]),
            },
            "pageSize": self.page_size,
        }
        self._write_json_atomic(self.manifest_path, value)

    def _write_health(self, status: str) -> None:
        value = {
            "schemaVersion": 1,
            "instanceId": self.instance_id,
            "status": status,
            "pid": os.getpid(),
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "requestCount": self._request_count,
            "errorCount": self._error_count,
            "closed": self._closed,
        }
        self._write_json_atomic(self.health_path, value)

    def mark_ready(self) -> None:
        with self._lock:
            self._write_health("ready")

    def close(self, *, abnormal: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._write_health("error" if abnormal else "stopped")

    def check_token(self, authorization: str | None) -> None:
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, self.token):
            raise ServiceError(ERR_UNAUTHORIZED, "invalid credential", status=401)

    def _visible(self, raw_visible_to: str | None) -> bool:
        if raw_visible_to is None:
            return True
        return self.current_user_id in json.loads(raw_visible_to)

    def _chat_visible(self, row: sqlite3.Row) -> bool:
        return self.current_user_id in json.loads(row["members"])

    def _query_fingerprint(self, operation: str, params: dict[str, Json]) -> str:
        payload = json.dumps(
            {"operation": operation, "params": {k: v for k, v in params.items() if k != "cursor"}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _encode_cursor(self, operation: str, params: dict[str, Json], offset: int) -> str:
        body = json.dumps(
            {
                "operation": operation,
                "fingerprint": self._query_fingerprint(operation, params),
                "offset": offset,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self.cursor_secret, body, hashlib.sha256).hexdigest()[:32].encode("ascii")
        return base64.urlsafe_b64encode(body + b"." + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, operation: str, params: dict[str, Json]) -> int:
        raw = params.get("cursor")
        if raw in {None, ""}:
            return 0
        if not isinstance(raw, str) or len(raw) > 256:
            raise ServiceError(ERR_INVALID_ARGUMENT, "invalid cursor")
        try:
            padded = raw + "=" * (-len(raw) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            body, signature = decoded.rsplit(b".", 1)
            expected = hmac.new(self.cursor_secret, body, hashlib.sha256).hexdigest()[:32].encode("ascii")
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            value = json.loads(body.decode("utf-8"))
            if (
                value.get("operation") != operation
                or value.get("fingerprint") != self._query_fingerprint(operation, params)
                or isinstance(value.get("offset"), bool)
                or not isinstance(value.get("offset"), int)
                or value["offset"] < 0
            ):
                raise ValueError("cursor mismatch")
            return int(value["offset"])
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ServiceError(ERR_INVALID_ARGUMENT, "invalid cursor") from exc

    def _paginate(
        self,
        operation: str,
        params: dict[str, Json],
        values: list[dict[str, Json]],
    ) -> tuple[list[dict[str, Json]], bool, str]:
        offset = self._decode_cursor(operation, params)
        if offset > len(values):
            raise ServiceError(ERR_INVALID_ARGUMENT, "invalid cursor")
        page = values[offset : offset + self.page_size]
        next_offset = offset + len(page)
        has_more = next_offset < len(values)
        cursor = self._encode_cursor(operation, params, next_offset) if has_more else ""
        return page, has_more, cursor

    def _require_params(self, params: Json) -> dict[str, Json]:
        if not isinstance(params, dict):
            raise ServiceError(ERR_INVALID_ARGUMENT, "arguments must be a JSON object")
        return params

    def execute(self, operation: str, params_value: Json, *, cwd: str | None = None) -> dict[str, Json]:
        params = self._require_params(params_value)
        if operation == "auth.status":
            return {"errcode": 0, "errmsg": "ok", "auth_status": "authorized"}
        if operation == "contact.get_userlist":
            return self._get_userlist(params)
        if operation == "msg.get_msg_chat_list":
            return self._get_chat_list(params)
        if operation == "msg.get_message":
            return self._get_messages(params)
        if operation == "msg.get_msg_media":
            return self._get_media(params, cwd=cwd)
        if operation == "doc.get_doc_content":
            return self._get_document(params)
        raise ServiceError(ERR_UNSUPPORTED, f"unsupported operation: {operation}")

    def _get_userlist(self, params: dict[str, Json]) -> dict[str, Json]:
        if params:
            raise ServiceError(ERR_INVALID_ARGUMENT, "get_userlist does not accept arguments")
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
        users = [
            {"userid": row["id"], "name": row["name"], "alias": row["alias"]}
            for row in rows
            if self._visible(row["visible_to"])
        ]
        if len(users) > MAX_CONTACTS:
            raise ServiceError(ERR_INVALID_ARGUMENT, "visible user count exceeds supported limit of 10")
        return {"errcode": 0, "errmsg": "ok", "userlist": users}

    def _time_range(self, params: dict[str, Json]) -> tuple[str, str]:
        begin = _parse_query_time(params.get("begin_time"), "begin_time")
        end = _parse_query_time(params.get("end_time"), "end_time")
        if end < begin:
            raise ServiceError(ERR_INVALID_ARGUMENT, "end_time must be greater than or equal to begin_time")
        return begin, end

    def _get_chat_list(self, params: dict[str, Json]) -> dict[str, Json]:
        unknown = sorted(set(params) - {"begin_time", "end_time", "cursor"})
        if unknown:
            raise ServiceError(ERR_INVALID_ARGUMENT, f"unknown argument(s): {', '.join(unknown)}")
        begin, end = self._time_range(params)
        with self._connect() as connection:
            chats = connection.execute("SELECT * FROM chats").fetchall()
            messages = connection.execute(
                "SELECT chat_id, send_time FROM messages WHERE send_time >= ? AND send_time <= ?",
                (begin, end),
            ).fetchall()
        by_chat: dict[str, list[str]] = {}
        for message in messages:
            by_chat.setdefault(message["chat_id"], []).append(message["send_time"])
        values: list[dict[str, Json]] = []
        for chat in chats:
            chat_messages = by_chat.get(chat["id"], [])
            if not chat_messages or not self._chat_visible(chat):
                continue
            values.append(
                {
                    "chat_id": chat["id"],
                    "chat_name": chat["name"],
                    "last_msg_time": max(chat_messages),
                    "msg_count": len(chat_messages),
                }
            )
        values.sort(key=lambda item: item["chat_id"])
        values.sort(key=lambda item: item["last_msg_time"], reverse=True)
        page, has_more, cursor = self._paginate("msg.get_msg_chat_list", params, values)
        return {
            "errcode": 0,
            "errmsg": "ok",
            "chats": page,
            "has_more": has_more,
            "next_cursor": cursor,
        }

    def _get_messages(self, params: dict[str, Json]) -> dict[str, Json]:
        unknown = sorted(set(params) - {"chat_type", "chatid", "begin_time", "end_time", "cursor"})
        if unknown:
            raise ServiceError(ERR_INVALID_ARGUMENT, f"unknown argument(s): {', '.join(unknown)}")
        chat_type = params.get("chat_type")
        if isinstance(chat_type, bool) or chat_type not in {1, 2}:
            raise ServiceError(ERR_INVALID_ARGUMENT, "chat_type must be 1 or 2")
        chat_id = params.get("chatid")
        if not isinstance(chat_id, str) or not chat_id or len(chat_id.encode("utf-8")) > 256:
            raise ServiceError(ERR_INVALID_ARGUMENT, "chatid must be a non-empty string up to 256 bytes")
        begin, end = self._time_range(params)
        with self._connect() as connection:
            chat = connection.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
            if chat is None:
                raise ServiceError(ERR_NOT_FOUND, "chat not found")
            if not self._chat_visible(chat):
                raise ServiceError(ERR_FORBIDDEN, "chat is not visible to current user")
            expected_type = 1 if chat["type"] == "direct" else 2
            if chat_type != expected_type:
                raise ServiceError(ERR_INVALID_ARGUMENT, "chat_type does not match chat")
            rows = connection.execute(
                """
                SELECT messages.*, media.filename, media.type AS media_type,
                       media.visible_to AS media_visible_to
                FROM messages
                LEFT JOIN media ON media.id = messages.media_id
                WHERE chat_id = ? AND send_time >= ? AND send_time <= ?
                ORDER BY send_time ASC, messages.id ASC
                """,
                (chat_id, begin, end),
            ).fetchall()
        values: list[dict[str, Json]] = []
        for row in rows:
            result: dict[str, Json] = {
                "userid": row["sender_id"],
                "send_time": row["send_time"],
                "msgtype": row["type"],
            }
            if row["type"] == "text":
                result["text"] = {"content": row["text"]}
            else:
                if not self._visible(row["media_visible_to"]):
                    continue
                result["file"] = {"media_id": row["media_id"], "name": row["filename"]}
            values.append(result)
        page, _, cursor = self._paginate("msg.get_message", params, values)
        return {"errcode": 0, "errmsg": "ok", "messages": page, "next_cursor": cursor}

    def _download_target(self, row: sqlite3.Row, cwd: str | None) -> Path:
        if not isinstance(cwd, str) or not cwd.strip():
            raise ServiceError(ERR_INVALID_ARGUMENT, "client working directory is required")
        cwd_path = Path(cwd).resolve()
        if not cwd_path.is_dir() or not _path_is_within(cwd_path, self.workspace_root):
            raise ServiceError(ERR_FORBIDDEN, "current directory is outside workspace root")
        hidden_dir = cwd_path / ".wecom"
        if hidden_dir.exists() and hidden_dir.is_symlink():
            raise ServiceError(ERR_FORBIDDEN, "download directory must not be a symlink")
        hidden_dir.mkdir(mode=0o700, exist_ok=True)
        if not hidden_dir.is_dir():
            raise ServiceError(ERR_FORBIDDEN, "download directory is not a directory")
        download_dir = hidden_dir / "downloads"
        if download_dir.exists() and download_dir.is_symlink():
            raise ServiceError(ERR_FORBIDDEN, "download directory must not be a symlink")
        download_dir.mkdir(mode=0o700, exist_ok=True)
        if not download_dir.is_dir():
            raise ServiceError(ERR_FORBIDDEN, "download directory is not a directory")
        resolved_download_dir = download_dir.resolve()
        if not _path_is_within(resolved_download_dir, self.workspace_root):
            raise ServiceError(ERR_FORBIDDEN, "download directory escapes workspace root")
        filename = _safe_filename(row["filename"], row["id"])
        target = resolved_download_dir / filename
        source = self.private_blobs_dir / row["blob"]
        if target.exists():
            if target.is_file() and _sha256_file(target)[0] == row["blob"]:
                return target.resolve()
            stem = Path(filename).stem or row["id"]
            suffix = Path(filename).suffix
            target = resolved_download_dir / f"{stem}_{row['id']}{suffix}"
            counter = 2
            while target.exists() and (not target.is_file() or _sha256_file(target)[0] != row["blob"]):
                target = resolved_download_dir / f"{stem}_{row['id']}_{counter}{suffix}"
                counter += 1
            if target.exists():
                return target.resolve()
        resolved_target = target.resolve(strict=False)
        if not _path_is_within(resolved_target, resolved_download_dir):
            raise ServiceError(ERR_FORBIDDEN, "download path escapes workspace root")
        temp_target = resolved_download_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(source, temp_target)
        os.replace(temp_target, target)
        return target.resolve()

    def _get_media(self, params: dict[str, Json], *, cwd: str | None) -> dict[str, Json]:
        if set(params) != {"media_id"}:
            raise ServiceError(ERR_INVALID_ARGUMENT, "get_msg_media requires only media_id")
        media_id = params.get("media_id")
        if not isinstance(media_id, str) or not 1 <= len(media_id) <= 256:
            raise ServiceError(ERR_INVALID_ARGUMENT, "media_id length must be between 1 and 256")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if row is None:
            raise ServiceError(ERR_NOT_FOUND, "media not found")
        if not self._visible(row["visible_to"]):
            raise ServiceError(ERR_FORBIDDEN, "media is not visible to current user")
        target = self._download_target(row, cwd)
        return {
            "errcode": 0,
            "errmsg": "ok",
            "media_item": {
                "media_id": row["id"],
                "name": row["filename"],
                "type": row["type"],
                "local_path": str(target),
                "size": row["size"],
                "content_type": row["content_type"]
                or mimetypes.guess_type(row["filename"])[0]
                or "application/octet-stream",
            },
        }

    def _document_task_id(self, document_id: str) -> str:
        digest = hmac.new(self.cursor_secret, f"document:{document_id}".encode(), hashlib.sha256).hexdigest()
        return f"doc_task_{digest[:24]}"

    def _get_document(self, params: dict[str, Json]) -> dict[str, Json]:
        unknown = sorted(set(params) - {"docid", "url", "type", "task_id"})
        if unknown:
            raise ServiceError(ERR_INVALID_ARGUMENT, f"unknown argument(s): {', '.join(unknown)}")
        docid = params.get("docid")
        url = params.get("url")
        if bool(docid) == bool(url):
            raise ServiceError(ERR_INVALID_ARGUMENT, "exactly one of docid or url is required")
        if params.get("type") != 2:
            raise ServiceError(ERR_INVALID_ARGUMENT, "type must be 2")
        lookup_field = "id" if docid else "url"
        lookup_value = docid or url
        if not isinstance(lookup_value, str):
            raise ServiceError(ERR_INVALID_ARGUMENT, f"{lookup_field} must be a string")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM documents WHERE {lookup_field} = ?",
                (lookup_value,),
            ).fetchone()
            if row is None:
                raise ServiceError(ERR_NOT_FOUND, "document not found")
            if not self._visible(row["visible_to"]):
                raise ServiceError(ERR_FORBIDDEN, "document is not visible to current user")
            expected_task_id = self._document_task_id(row["id"])
            supplied_task_id = params.get("task_id")
            if supplied_task_id is not None and supplied_task_id != expected_task_id:
                raise ServiceError(ERR_INVALID_ARGUMENT, "task_id does not match document")
            task = connection.execute(
                "SELECT * FROM doc_poll_tasks WHERE task_id = ?",
                (expected_task_id,),
            ).fetchone()
            poll_count = int(task["poll_count"]) if task is not None else 0
            if supplied_task_id is not None:
                poll_count += 1
            if task is None:
                connection.execute(
                    "INSERT INTO doc_poll_tasks VALUES (?, ?, ?)",
                    (expected_task_id, row["id"], poll_count),
                )
            else:
                connection.execute(
                    "UPDATE doc_poll_tasks SET poll_count = ? WHERE task_id = ?",
                    (poll_count, expected_task_id),
                )
        if poll_count < int(row["polls_before_ready"]):
            return {
                "errcode": 0,
                "errmsg": "ok",
                "content": "",
                "task_id": expected_task_id,
                "task_done": False,
            }
        try:
            content = (self.private_blobs_dir / row["content_blob"]).read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ServiceError(ERR_INTERNAL, "document content is not valid UTF-8", status=500) from exc
        return {
            "errcode": 0,
            "errmsg": "ok",
            "content": content,
            "task_id": expected_task_id,
            "task_done": True,
        }

    def audit(
        self,
        *,
        operation: str,
        params: Json,
        response: dict[str, Json],
        duration_ms: int,
        request_id: str | None = None,
    ) -> None:
        with self._lock:
            self._request_count += 1
            if response.get("errcode") != 0:
                self._error_count += 1
            self._event_sequence += 1
            resource_ids: list[str] = []
            if isinstance(params, dict):
                for key in ("chatid", "media_id", "docid", "url"):
                    value = params.get(key)
                    if isinstance(value, str):
                        resource_ids.append(value)
            if isinstance(response.get("chats"), list):
                resource_ids.extend(
                    item["chat_id"]
                    for item in response["chats"]
                    if isinstance(item, dict) and isinstance(item.get("chat_id"), str)
                )
            media_item = response.get("media_item")
            if isinstance(media_item, dict) and isinstance(media_item.get("media_id"), str):
                resource_ids.append(media_item["media_id"])
            safe_params = dict(params) if isinstance(params, dict) else {}
            safe_params.pop("task_id", None)
            event = {
                "schemaVersion": AUDIT_SCHEMA_VERSION,
                "sequence": self._event_sequence,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "instanceId": self.instance_id,
                "operation": SUPPORTED_OPERATIONS.get(operation, f"wecom.unsupported:{operation}"),
                "request": safe_params,
                "resourceIds": list(dict.fromkeys(resource_ids)),
                "status": "ok" if response.get("errcode") == 0 else "error",
                "errcode": response.get("errcode"),
                "resultCount": self._result_count(response),
                "durationMs": duration_ms,
            }
            if request_id is not None:
                event["requestId"] = request_id
            projection = self._project_response(response)
            if projection is not None:
                event["result"] = projection
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO audit_events VALUES (?, ?)",
                    (self._event_sequence, line),
                )
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            self._write_health("ready")

    @staticmethod
    def _project_response(response: dict[str, Json]) -> dict[str, Json] | None:
        """Project the response into a replay-friendly, leak-safe shape.

        This is the authoritative record of what the agent actually observed, so
        downstream tooling (the viz replay UI) does not have to re-parse agent
        stdout out of execution traces.

        Two hard rules:

        1. **Only what was returned.** Everything here was already handed to the
           agent, so recording it leaks nothing that the agent did not see. Data
           the agent never fetched must never be added — otherwise a replay would
           show ground truth the agent never had.
        2. **No host paths, no document bodies.** ``media_item.local_path`` is an
           absolute path on the host and is reduced to its basename; document
           content is reduced to a byte count.
        """
        result: dict[str, Json] = {}

        userlist = response.get("userlist")
        if isinstance(userlist, list):
            result["users"] = [
                {
                    "userid": item.get("userid"),
                    "name": item.get("name"),
                    "alias": item.get("alias"),
                }
                for item in userlist
                if isinstance(item, dict)
            ]

        chats = response.get("chats")
        if isinstance(chats, list):
            result["chats"] = [
                {
                    "chat_id": item.get("chat_id"),
                    "chat_name": item.get("chat_name"),
                    "last_msg_time": item.get("last_msg_time"),
                    "msg_count": item.get("msg_count"),
                }
                for item in chats
                if isinstance(item, dict)
            ]

        messages = response.get("messages")
        if isinstance(messages, list):
            projected_messages: list[Json] = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                message: dict[str, Json] = {
                    "userid": item.get("userid"),
                    "send_time": item.get("send_time"),
                    "msgtype": item.get("msgtype"),
                }
                text = item.get("text")
                if isinstance(text, dict) and isinstance(text.get("content"), str):
                    message["text"] = text["content"]
                file_item = item.get("file")
                if isinstance(file_item, dict):
                    message["media_id"] = file_item.get("media_id")
                    message["name"] = file_item.get("name")
                projected_messages.append(message)
            result["messages"] = projected_messages

        media_item = response.get("media_item")
        if isinstance(media_item, dict):
            local_path = media_item.get("local_path")
            result["media"] = {
                "media_id": media_item.get("media_id"),
                "name": media_item.get("name"),
                "type": media_item.get("type"),
                "size": media_item.get("size"),
                "content_type": media_item.get("content_type"),
                # basename only: local_path is an absolute host path.
                "saved_as": os.path.basename(local_path) if isinstance(local_path, str) else None,
            }

        if "task_done" in response:
            content = response.get("content")
            result["document"] = {
                "task_done": bool(response.get("task_done")),
                # Length only. The document body stays out of the audit log.
                "content_bytes": len(content.encode("utf-8")) if isinstance(content, str) else 0,
            }

        # 分页游标只暴露"是否还有下一页"，签名后的 cursor 本身是服务端状态。
        if "next_cursor" in response:
            result["has_next_cursor"] = bool(response.get("next_cursor"))
        if "has_more" in response:
            result["has_more"] = bool(response.get("has_more"))

        return result or None

    @staticmethod
    def _result_count(response: dict[str, Json]) -> int:
        for key in ("userlist", "chats", "messages"):
            value = response.get(key)
            if isinstance(value, list):
                return len(value)
        if response.get("media_item") or response.get("task_done") is True:
            return 1
        return 0
