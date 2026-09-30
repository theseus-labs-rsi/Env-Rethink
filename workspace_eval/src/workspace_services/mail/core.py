from __future__ import annotations

import email
import email.header
import email.utils
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
from datetime import datetime, timezone
from email.header import Header
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from pathlib import Path
from typing import Any, Iterable


Json = Any
SCHEMA_VERSION = 1
DEFAULT_MAX_FETCH = 200

ERR_UNAUTHORIZED = 40001
ERR_NOT_FOUND = 40400

SYSTEM_FLAGS = ("\\Seen", "\\Answered", "\\Flagged", "\\Deleted", "\\Draft")
SPECIAL_USE_FLAGS = ("\\All", "\\Archive", "\\Drafts", "\\Flagged", "\\Junk", "\\Sent", "\\Trash")

# Audit operation names. The wire protocol is IMAP/SMTP, so operations are named
# after the semantic action rather than after a CLI subcommand.
OP_LOGIN = "mail.login"
OP_LIST = "mail.list"
OP_SELECT = "mail.select"
OP_SEARCH = "mail.search"
OP_FETCH = "mail.fetch"
OP_STORE = "mail.store"
OP_SEND = "mail.send"


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


def _require_header_string(value: Json, label: str, *, allow_empty: bool = False) -> str:
    """Validate a value that will be rendered into a mail header.

    A newline here would either abort message generation or, worse, inject a
    forged line into an IMAP response, so control characters are rejected during
    fixture validation rather than at render time.
    """
    text = _require_string(value, label, allow_empty=allow_empty)
    if any(ch == "\x7f" or ch < " " for ch in text):
        raise FixtureError(f"{label} must not contain control characters")
    return text


def _require_int(value: Json, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FixtureError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise FixtureError(f"{label} must be between {minimum} and {maximum}")
    return value


def _parse_fixture_time(value: Json, label: str) -> tuple[str, datetime]:
    raw = _require_string(value, label)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise FixtureError(f"{label} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return raw, parsed


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
    if _path_is_within(path.resolve(), workspace_root):
        raise FixtureError(f"{label} must not be inside workspace root")


def _safe_filename(value: str, fallback: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    if name in {"", ".", ".."}:
        name = fallback
    name = re.sub(r"[:*?\"<>|]", "_", name)
    return name[:240] or fallback


def _validate_address(value: Json, label: str) -> dict[str, str]:
    item = _require_mapping(value, label)
    _reject_unknown(item, {"name", "address"}, label)
    address = _require_string(item.get("address"), f"{label}.address")
    if address.count("@") != 1 or address.startswith("@") or address.endswith("@"):
        raise FixtureError(f"{label}.address must look like local@domain")
    if any(ch == "\x7f" or ch < " " for ch in address):
        raise FixtureError(f"{label}.address must not contain control characters")
    name = _require_header_string(item.get("name", ""), f"{label}.name", allow_empty=True)
    return {"name": name, "address": address}


def _validate_address_list(value: Json, label: str) -> list[dict[str, str]]:
    if value is None:
        return []
    return [_validate_address(item, f"{label}[{index}]") for index, item in enumerate(_require_list(value, label))]


def _validate_flags(value: Json, label: str) -> list[str]:
    if value is None:
        return []
    out: list[str] = []
    for index, item in enumerate(_require_list(value, label)):
        flag = _require_string(item, f"{label}[{index}]")
        match = next((known for known in SYSTEM_FLAGS if known.lower() == flag.lower()), None)
        if match is None:
            raise FixtureError(f"{label}[{index}] must be one of {', '.join(SYSTEM_FLAGS)}")
        if match not in out:
            out.append(match)
    return out


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
        {"schema_version", "account", "settings", "mailboxes", "messages", "attachments", "faults"},
        "fixture",
    )
    if fixture.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError(f"unsupported schema_version: {fixture.get('schema_version')!r}")

    account_raw = _require_mapping(fixture.get("account"), "account")
    _reject_unknown(account_raw, {"address", "login", "password", "display_name"}, "account")
    account_address = _validate_address(
        {"name": account_raw.get("display_name", ""), "address": account_raw.get("address")},
        "account",
    )
    account = {
        "address": account_address["address"],
        "display_name": account_address["name"],
        "login": _require_string(account_raw.get("login", account_address["address"]), "account.login"),
        "password": _require_string(account_raw.get("password"), "account.password"),
    }

    settings = _require_mapping(fixture.get("settings", {}), "settings")
    _reject_unknown(settings, {"max_fetch"}, "settings")
    max_fetch = _require_int(
        settings.get("max_fetch", DEFAULT_MAX_FETCH), "settings.max_fetch", minimum=1, maximum=5000
    )

    mailboxes: list[dict[str, Json]] = []
    mailbox_names: set[str] = set()
    for index, item in enumerate(_require_list(fixture.get("mailboxes", []), "mailboxes")):
        label = f"mailboxes[{index}]"
        entry = _require_mapping(item, label)
        _reject_unknown(entry, {"name", "special_use", "uid_validity"}, label)
        name = _require_string(entry.get("name"), f"{label}.name")
        if "\r" in name or "\n" in name or '"' in name or "\\" in name:
            raise FixtureError(f"{label}.name must not contain quotes, backslashes, or newlines")
        if name in mailbox_names:
            raise FixtureError(f"{label}.name duplicates an earlier mailbox")
        mailbox_names.add(name)
        special_use = entry.get("special_use")
        if special_use is not None:
            special_use = _require_string(special_use, f"{label}.special_use")
            match = next((known for known in SPECIAL_USE_FLAGS if known.lower() == special_use.lower()), None)
            if match is None:
                raise FixtureError(
                    f"{label}.special_use must be one of {', '.join(SPECIAL_USE_FLAGS)}"
                )
            special_use = match
        mailboxes.append(
            {
                "name": name,
                "special_use": special_use,
                "uid_validity": _require_int(
                    entry.get("uid_validity", 1), f"{label}.uid_validity", minimum=1, maximum=2**32 - 1
                ),
            }
        )
    if "INBOX" not in mailbox_names:
        raise FixtureError("mailboxes must include INBOX")

    attachments: dict[str, dict[str, Json]] = {}
    required_digests: dict[str, str] = {}
    for index, item in enumerate(_require_list(fixture.get("attachments", []), "attachments")):
        label = f"attachments[{index}]"
        entry = _require_mapping(item, label)
        _reject_unknown(entry, {"id", "filename", "content_type", "blob", "size"}, label)
        attachment_id = _require_string(entry.get("id"), f"{label}.id")
        if attachment_id in attachments:
            raise FixtureError(f"{label}.id duplicates an earlier attachment")
        filename = _require_string(entry.get("filename"), f"{label}.filename")
        content_type = entry.get("content_type")
        if content_type is None:
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        content_type = _require_string(content_type, f"{label}.content_type")
        if content_type.count("/") != 1:
            raise FixtureError(f"{label}.content_type must be a maintype/subtype pair")
        digest = _blob_digest(entry.get("blob"), f"{label}.blob")
        required_digests[digest] = f"{label}.blob"
        attachments[attachment_id] = {
            "id": attachment_id,
            "filename": filename,
            "content_type": content_type,
            "blob": digest,
            "declared_size": entry.get("size"),
        }

    messages: list[dict[str, Json]] = []
    message_ids: set[str] = set()
    seen_uids: set[tuple[str, int]] = set()
    for index, item in enumerate(_require_list(fixture.get("messages", []), "messages")):
        label = f"messages[{index}]"
        entry = _require_mapping(item, label)
        _reject_unknown(
            entry,
            {
                "id",
                "mailbox",
                "uid",
                "flags",
                "from",
                "to",
                "cc",
                "reply_to",
                "subject",
                "date",
                "message_id",
                "in_reply_to",
                "body_text",
                "body_html",
                "attachments",
            },
            label,
        )
        message_key = _require_string(entry.get("id"), f"{label}.id")
        if message_key in message_ids:
            raise FixtureError(f"{label}.id duplicates an earlier message")
        message_ids.add(message_key)
        mailbox = _require_string(entry.get("mailbox"), f"{label}.mailbox")
        if mailbox not in mailbox_names:
            raise FixtureError(f"{label}.mailbox references unknown mailbox: {mailbox}")
        uid = _require_int(entry.get("uid"), f"{label}.uid", minimum=1, maximum=2**32 - 1)
        if (mailbox, uid) in seen_uids:
            raise FixtureError(f"{label}.uid duplicates another message in {mailbox}")
        seen_uids.add((mailbox, uid))
        raw_date, parsed_date = _parse_fixture_time(entry.get("date"), f"{label}.date")
        body_text = entry.get("body_text", "")
        if not isinstance(body_text, str):
            raise FixtureError(f"{label}.body_text must be a string")
        body_html = entry.get("body_html")
        if body_html is not None and not isinstance(body_html, str):
            raise FixtureError(f"{label}.body_html must be a string")
        message_attachments: list[str] = []
        for position, value in enumerate(_require_list(entry.get("attachments", []), f"{label}.attachments")):
            attachment_id = _require_string(value, f"{label}.attachments[{position}]")
            if attachment_id not in attachments:
                raise FixtureError(
                    f"{label}.attachments[{position}] references unknown attachment: {attachment_id}"
                )
            message_attachments.append(attachment_id)
        messages.append(
            {
                "id": message_key,
                "mailbox": mailbox,
                "uid": uid,
                "flags": _validate_flags(entry.get("flags"), f"{label}.flags"),
                "from": _validate_address(entry.get("from"), f"{label}.from"),
                "to": _validate_address_list(entry.get("to"), f"{label}.to"),
                "cc": _validate_address_list(entry.get("cc"), f"{label}.cc"),
                "reply_to": _validate_address_list(entry.get("reply_to"), f"{label}.reply_to"),
                "subject": _require_header_string(
                    entry.get("subject", ""), f"{label}.subject", allow_empty=True
                ),
                "date": raw_date,
                "internal_date": parsed_date,
                "message_id": _require_header_string(
                    entry.get("message_id") or f"<{message_key}@mail.mock>", f"{label}.message_id"
                ),
                "in_reply_to": (
                    None
                    if entry.get("in_reply_to") is None
                    else _require_header_string(entry.get("in_reply_to"), f"{label}.in_reply_to")
                ),
                "body_text": body_text,
                "body_html": body_html,
                "attachments": message_attachments,
            }
        )
    faults = _require_list(fixture.get("faults", []), "faults")
    if faults:
        raise FixtureError("faults are not supported yet")

    copied_blobs: dict[str, dict[str, Json]] = {}
    for digest, label in sorted(required_digests.items()):
        blob_path = (blobs_dir / digest).resolve()
        if not _path_is_within(blob_path, blobs_dir):
            raise FixtureError(f"{label} escapes the blob directory")
        if not blob_path.is_file():
            raise FixtureError(f"{label} refers to a missing blob: {digest}")
        actual_digest, actual_size = _sha256_file(blob_path)
        if actual_digest != digest:
            raise FixtureError(f"{label} content does not match its sha256 digest")
        copied_blobs[digest] = {"source": str(blob_path), "size": actual_size}

    for attachment in attachments.values():
        actual_size = copied_blobs[attachment["blob"]]["size"]
        declared = attachment.pop("declared_size")
        if declared is not None:
            declared_size = _require_int(declared, "attachments[].size", minimum=0, maximum=2**40)
            if declared_size != actual_size:
                raise FixtureError(
                    f"attachment {attachment['id']} declares size {declared_size} but the blob is {actual_size}"
                )
        attachment["size"] = actual_size

    messages.sort(key=lambda item: (item["mailbox"], item["uid"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "account": account,
        "settings": {"max_fetch": max_fetch},
        "mailboxes": mailboxes,
        "messages": messages,
        "attachments": attachments,
        "blobs": copied_blobs,
        "fixture_path": str(fixture_path),
        "blobs_dir": str(blobs_dir),
        "workspace_root": str(workspace_root),
    }


def _format_address_header(entries: Iterable[dict[str, str]]) -> str:
    return ", ".join(
        email.utils.formataddr((item.get("name") or "", item["address"])) for item in entries
    )


def _imap_datetime(value: datetime) -> str:
    # RFC 3501 date-time: "01-Apr-2026 09:00:00 +0800" with a space-padded day.
    offset = value.utcoffset() or timezone.utc.utcoffset(value)
    total_minutes = int((offset.total_seconds() if offset else 0) // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return (
        f"{value.day:2d}-{months[value.month - 1]}-{value.year:04d} "
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d} "
        f"{sign}{total_minutes // 60:02d}{total_minutes % 60:02d}"
    )


def _constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking length via early exit.

    ``hmac.compare_digest`` rejects non-ASCII ``str`` inputs outright, and both
    logins and passwords in these fixtures may contain CJK characters, so the
    comparison is done on the UTF-8 encodings.
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _encode_header_value(value: str) -> str:
    """Encode a header value as RFC 2047 only when it is not pure ASCII.

    Folding is suppressed: this value goes into an IMAP ENVELOPE, which is a
    single response line, so an embedded newline would desynchronise the client.
    Folding in the RFC822 message itself is handled separately by the generator.
    """
    if value.isascii():
        return value
    return Header(value, "utf-8").encode(maxlinelen=10**6)


def _build_rfc822(message: dict[str, Json], blob_reader) -> bytes:
    """Render a fixture message into deterministic RFC822 bytes."""
    msg = EmailMessage()
    msg["From"] = _format_address_header([message["from"]])
    if message["to"]:
        msg["To"] = _format_address_header(message["to"])
    if message["cc"]:
        msg["Cc"] = _format_address_header(message["cc"])
    if message["reply_to"]:
        msg["Reply-To"] = _format_address_header(message["reply_to"])
    msg["Subject"] = message["subject"]
    msg["Date"] = email.utils.format_datetime(message["internal_date"])
    msg["Message-ID"] = message["message_id"]
    if message["in_reply_to"]:
        msg["In-Reply-To"] = message["in_reply_to"]
        msg["References"] = message["in_reply_to"]

    msg.set_content(message["body_text"], subtype="plain", charset="utf-8")
    if message["body_html"]:
        msg.add_alternative(message["body_html"], subtype="html", charset="utf-8")
    for attachment_id in message["attachments"]:
        attachment = blob_reader(attachment_id)
        maintype, _, subtype = attachment["content_type"].partition("/")
        msg.add_attachment(
            attachment["data"],
            maintype=maintype,
            subtype=subtype,
            filename=attachment["filename"],
        )

    # email.generator picks a random MIME boundary, which would make the rendered
    # bytes differ between runs. Assign boundaries derived from the message id so
    # the same fixture always produces byte-identical messages.
    _assign_boundaries(msg, hashlib.sha256(message["id"].encode("utf-8")).hexdigest()[:16])
    return msg.as_bytes(policy=SMTP_POLICY)


def _assign_boundaries(part: EmailMessage, seed: str, path: str = "0") -> None:
    if not part.is_multipart():
        return
    part.set_boundary(f"==={seed}.{path}==")
    payload = part.get_payload()
    if isinstance(payload, list):
        for index, sub in enumerate(payload):
            _assign_boundaries(sub, seed, f"{path}.{index}")


def _envelope_address(entries: Iterable[dict[str, str]]) -> str | None:
    items = list(entries)
    if not items:
        return None
    parts = []
    for item in items:
        local, _, domain = item["address"].partition("@")
        name = item.get("name") or ""
        parts.append(
            "("
            + " ".join(
                [
                    _imap_string(_encode_header_value(name)) if name else "NIL",
                    "NIL",
                    _imap_string(local),
                    _imap_string(domain) if domain else "NIL",
                ]
            )
            + ")"
        )
    return "(" + "".join(parts) + ")"


def _imap_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_envelope(message: dict[str, Json]) -> str:
    """Render an RFC 3501 ENVELOPE for a stored message."""
    sender = _envelope_address([message["from"]])
    fields = [
        _imap_string(email.utils.format_datetime(message["internal_date"])),
        _imap_string(_encode_header_value(message["subject"])) if message["subject"] else "NIL",
        sender or "NIL",
        sender or "NIL",
        _envelope_address(message["reply_to"]) or sender or "NIL",
        _envelope_address(message["to"]) or "NIL",
        _envelope_address(message["cc"]) or "NIL",
        "NIL",
        _imap_string(message["in_reply_to"]) if message["in_reply_to"] else "NIL",
        _imap_string(message["message_id"]),
    ]
    return "(" + " ".join(fields) + ")"


def _decode_subject(payload: bytes) -> str:
    """Read a Subject header back as plain text, undoing any RFC 2047 encoding.

    Submitted mail is stored so rubrics can assert on it, so the decoded form is
    what gets recorded rather than the encoded-word wire form.
    """
    try:
        raw = email.message_from_bytes(payload).get("Subject")
        if raw is None:
            return ""
        return "".join(
            fragment.decode(charset or "utf-8", errors="replace")
            if isinstance(fragment, bytes)
            else fragment
            for fragment, charset in email.header.decode_header(str(raw))
        )
    except Exception:  # pragma: no cover - defensive
        return ""


class MailStore:
    """Task-scoped mail state plus the audit log for every client action."""

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
        self.instance_id = f"mail-{uuid.uuid4().hex}"
        self.token = secrets.token_urlsafe(32)
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
        self.outbox_dir = self.state_dir / "outbox"
        self.outbox_dir.mkdir(mode=0o700)

        self.database_path = self.state_dir / "mail.sqlite3"
        self.events_path = self.state_dir / "mail-service-events.jsonl"
        self.manifest_path = self.state_dir / "mail-service-manifest.json"
        self.health_path = self.state_dir / "mail-service-health.json"
        self._rendered: dict[str, bytes] = {}
        self._initialize_database()
        self._write_manifest()
        self._write_health("starting")

    # ---- accessors -------------------------------------------------------

    @property
    def account(self) -> dict[str, str]:
        return dict(self._fixture["account"])

    @property
    def max_fetch(self) -> int:
        return int(self._fixture["settings"]["max_fetch"])

    @property
    def mailboxes(self) -> list[dict[str, Json]]:
        return [dict(item) for item in self._fixture["mailboxes"]]

    def mailbox(self, name: str) -> dict[str, Json] | None:
        # INBOX is case-insensitive per RFC 3501; other names are exact.
        for item in self._fixture["mailboxes"]:
            if item["name"] == name:
                return dict(item)
            if item["name"].upper() == "INBOX" and name.upper() == "INBOX":
                return dict(item)
        return None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE mailboxes (
                    name TEXT PRIMARY KEY,
                    special_use TEXT,
                    uid_validity INTEGER NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    mailbox TEXT NOT NULL REFERENCES mailboxes(name),
                    uid INTEGER NOT NULL,
                    flags TEXT NOT NULL,
                    internal_date TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    from_address TEXT NOT NULL,
                    to_addresses TEXT NOT NULL,
                    cc_addresses TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    UNIQUE (mailbox, uid)
                );
                CREATE TABLE sent_messages (
                    sequence INTEGER PRIMARY KEY,
                    envelope_from TEXT NOT NULL,
                    recipients TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    path TEXT NOT NULL
                );
                CREATE TABLE audit_events (
                    sequence INTEGER PRIMARY KEY,
                    event_json TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO mailboxes VALUES (?, ?, ?)",
                [(item["name"], item["special_use"], item["uid_validity"]) for item in self._fixture["mailboxes"]],
            )
            rows = []
            for message in self._fixture["messages"]:
                rendered = self._render(message)
                rows.append(
                    (
                        message["id"],
                        message["mailbox"],
                        message["uid"],
                        json.dumps(message["flags"]),
                        message["internal_date"].isoformat(),
                        message["subject"],
                        message["from"]["address"],
                        json.dumps([item["address"] for item in message["to"]]),
                        json.dumps([item["address"] for item in message["cc"]]),
                        len(rendered),
                    )
                )
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )

    def _read_attachment(self, attachment_id: str) -> dict[str, Json]:
        attachment = self._fixture["attachments"][attachment_id]
        source = self.private_blobs_dir / attachment["blob"]
        if not source.is_file():
            source = Path(self._fixture["blobs"][attachment["blob"]]["source"])
        return {
            "filename": _safe_filename(attachment["filename"], attachment["id"]),
            "content_type": attachment["content_type"],
            "data": source.read_bytes(),
        }

    def _render(self, message: dict[str, Json]) -> bytes:
        cached = self._rendered.get(message["id"])
        if cached is None:
            cached = _build_rfc822(message, self._read_attachment)
            self._rendered[message["id"]] = cached
        return cached

    def message_source(self, message_id: str) -> bytes:
        for message in self._fixture["messages"]:
            if message["id"] == message_id:
                return self._render(message)
        raise ServiceError(ERR_NOT_FOUND, "message not found")

    def messages_in(self, mailbox: str) -> list[dict[str, Json]]:
        resolved = self.mailbox(mailbox)
        if resolved is None:
            return []
        return [item for item in self._fixture["messages"] if item["mailbox"] == resolved["name"]]

    # ---- mutable flag state ---------------------------------------------

    def flags_for(self, message_id: str) -> list[str]:
        with self._connect() as connection:
            row = connection.execute("SELECT flags FROM messages WHERE id = ?", (message_id,)).fetchone()
        return list(json.loads(row["flags"])) if row is not None else []

    def update_flags(self, message_id: str, flags: Iterable[str]) -> list[str]:
        ordered = [flag for flag in SYSTEM_FLAGS if flag in set(flags)]
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE messages SET flags = ? WHERE id = ?", (json.dumps(ordered), message_id)
            )
        return ordered

    # ---- SMTP ingest -----------------------------------------------------

    def record_sent_message(
        self, *, envelope_from: str, recipients: list[str], payload: bytes
    ) -> dict[str, Json]:
        """Persist a message received over SMTP as a scoreable artifact."""
        with self._lock:
            parsed_subject = _decode_subject(payload)
            received_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS highest FROM sent_messages"
                ).fetchone()
                sequence = int(row["highest"]) + 1
                filename = f"sent-{sequence:04d}.eml"
                # The row is inserted first so a crash cannot leave an orphaned
                # .eml whose sequence would be handed out again and overwritten.
                connection.execute(
                    "INSERT INTO sent_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        envelope_from,
                        json.dumps(recipients),
                        parsed_subject,
                        len(payload),
                        received_at,
                        filename,
                    ),
                )
            target = self.outbox_dir / filename
            temp_target = self.outbox_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
            temp_target.write_bytes(payload)
            os.chmod(temp_target, 0o600)
            os.replace(temp_target, target)
            return {
                "sequence": sequence,
                "path": str(target),
                "subject": parsed_subject,
                "recipients": list(recipients),
                "size": len(payload),
            }

    # ---- bookkeeping -----------------------------------------------------

    def _write_json_atomic(self, path: Path, value: dict[str, Json]) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)

    def _write_manifest(self) -> None:
        fixture_digest, _ = _sha256_file(self.fixture_path)
        self._write_json_atomic(
            self.manifest_path,
            {
                "schemaVersion": 1,
                "fixtureSchemaVersion": SCHEMA_VERSION,
                "apiVersion": "v1",
                "serviceVersion": "0.1.0",
                "instanceId": self.instance_id,
                "fixtureSha256": fixture_digest,
                "workspaceRoot": str(self.workspace_root),
                "account": self._fixture["account"]["address"],
                "counts": {
                    "mailboxes": len(self._fixture["mailboxes"]),
                    "messages": len(self._fixture["messages"]),
                    "attachments": len(self._fixture["attachments"]),
                    "blobs": len(self._fixture["blobs"]),
                },
                "maxFetch": self.max_fetch,
            },
        )

    def _write_health(self, status: str) -> None:
        self._write_json_atomic(
            self.health_path,
            {
                "schemaVersion": 1,
                "instanceId": self.instance_id,
                "status": status,
                "pid": os.getpid(),
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "requestCount": self._request_count,
                "errorCount": self._error_count,
                "closed": self._closed,
            },
        )

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
        supplied = (
            authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
        )
        if not supplied or not _constant_time_equals(supplied, self.token):
            raise ServiceError(ERR_UNAUTHORIZED, "invalid credential", status=401)

    def check_login(self, login: str, password: str) -> bool:
        account = self._fixture["account"]
        login_ok = _constant_time_equals(login, account["login"]) or _constant_time_equals(
            login, account["address"]
        )
        return bool(login_ok and _constant_time_equals(password, account["password"]))

    def audit(
        self,
        *,
        operation: str,
        request: dict[str, Json] | None = None,
        resource_ids: Iterable[str] = (),
        status: str = "ok",
        errcode: int = 0,
        result_count: int = 0,
        duration_ms: int = 0,
    ) -> None:
        """Append one structured audit event.

        Only metadata is recorded. Message bodies and attachment content never
        enter the audit log, matching the WeCom mock's isolation rules.
        """
        with self._lock:
            self._request_count += 1
            if status != "ok":
                self._error_count += 1
            self._event_sequence += 1
            event = {
                "schemaVersion": 1,
                "sequence": self._event_sequence,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "instanceId": self.instance_id,
                "operation": operation,
                "request": dict(request or {}),
                "resourceIds": list(dict.fromkeys(str(item) for item in resource_ids)),
                "status": status,
                "errcode": errcode,
                "resultCount": result_count,
                "durationMs": duration_ms,
            }
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO audit_events VALUES (?, ?)", (self._event_sequence, line)
                )
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            self._write_health("ready")
