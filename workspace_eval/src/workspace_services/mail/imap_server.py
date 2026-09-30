"""A deterministic IMAP4rev1 subset sufficient for real IMAP clients.

Only the commands that read-oriented clients actually issue are implemented.
The advertised capability set is deliberately minimal: STARTTLS is never
advertised, because clients such as imapflow opportunistically upgrade when they
see it and this service speaks plaintext on 127.0.0.1 only. Every optional
extension (ID, ENABLE, NAMESPACE, CONDSTORE, COMPRESS=DEFLATE, LITERAL+, ...) is
likewise left unadvertised so compliant clients skip those round trips.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .core import (
    OP_FETCH,
    OP_LIST,
    OP_LOGIN,
    OP_SEARCH,
    OP_SELECT,
    OP_STORE,
    SYSTEM_FLAGS,
    MailStore,
    _imap_datetime,
    build_envelope,
)


Json = Any

CAPABILITIES = ("IMAP4rev1",)
MAX_LITERAL = 64 * 1024

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class ProtocolError(Exception):
    """Raised for a malformed client command; answered with BAD."""


class CommandError(Exception):
    """Raised for a well-formed command that cannot be completed; answered with NO."""


class FatalProtocolError(Exception):
    """Raised when the stream can no longer be resynchronised.

    Oversized lines and literals leave unread bytes in the buffer that cannot be
    attributed to a command, so the only safe response is to say BYE and close.
    Answering BAD and continuing would re-read the same bytes forever.
    """


class Token:
    """A parsed IMAP argument, whether it arrived as an atom, string, or literal."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    @property
    def upper(self) -> str:
        return self.value.upper()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.value!r})"


class _Literal:
    """A literal value already read off the wire, carried as one token."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


LITERAL_SUFFIX = re.compile(r"\{(\d+)(\+?)\}$")


class _Lexer:
    """Tokenises a command whose literals have already been resolved.

    The command arrives as a list of segments: plain text to be lexed character
    by character, and ``_Literal`` values that each yield exactly one token.
    """

    def __init__(self, segments: list[str | _Literal]) -> None:
        self._segments = segments
        self._index = 0
        self._pos = 0

    def _current(self) -> str | _Literal | None:
        while self._index < len(self._segments):
            segment = self._segments[self._index]
            if isinstance(segment, _Literal):
                return segment
            self._skip_spaces()
            if self._pos < len(segment):
                return segment
            self._index += 1
            self._pos = 0
        return None

    def _skip_spaces(self) -> None:
        segment = self._segments[self._index]
        if isinstance(segment, _Literal):
            return
        while self._pos < len(segment) and segment[self._pos] == " ":
            self._pos += 1

    def at_end(self) -> bool:
        return self._current() is None

    def next_token(self) -> Token:
        segment = self._current()
        if segment is None:
            raise ProtocolError("unexpected end of command")
        if isinstance(segment, _Literal):
            self._index += 1
            self._pos = 0
            return Token(segment.value)
        char = segment[self._pos]
        if char == '"':
            return Token(self._read_quoted(segment))
        if char in "()[]":
            self._pos += 1
            return Token(char)
        start = self._pos
        while self._pos < len(segment) and segment[self._pos] not in ' ()[]{"':
            self._pos += 1
        return Token(segment[start : self._pos])

    def _read_quoted(self, segment: str) -> str:
        self._pos += 1  # opening quote
        out: list[str] = []
        while True:
            if self._pos >= len(segment):
                raise ProtocolError("unterminated quoted string")
            char = segment[self._pos]
            if char == "\\":
                self._pos += 1
                if self._pos >= len(segment):
                    raise ProtocolError("unterminated escape sequence")
                out.append(segment[self._pos])
            elif char == '"':
                self._pos += 1
                return "".join(out)
            else:
                out.append(char)
            self._pos += 1

    def rest(self) -> str:
        """Return the remaining text, with literals rendered as quoted strings."""
        out: list[str] = []
        segment = self._current()
        if segment is None:
            return ""
        if isinstance(segment, str):
            out.append(segment[self._pos :])
            start = self._index + 1
        else:
            start = self._index
        for item in self._segments[start:]:
            out.append(f'"{item.value}"' if isinstance(item, _Literal) else item)
        self._index = len(self._segments)
        self._pos = 0
        return "".join(out).strip()


def _parse_uid_set(value: str, available: list[int]) -> list[int]:
    """Expand a UID set such as ``1,3:5`` or ``2:*`` against known UIDs."""
    if not value:
        raise ProtocolError("empty sequence set")
    highest = max(available) if available else 0
    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ProtocolError("malformed sequence set")
        if ":" in part:
            raw_low, _, raw_high = part.partition(":")
            low = highest if raw_low == "*" else _parse_uint(raw_low)
            high = highest if raw_high == "*" else _parse_uint(raw_high)
            if low > high:
                low, high = high, low
            selected.update(uid for uid in available if low <= uid <= high)
        elif part == "*":
            if highest:
                selected.add(highest)
        else:
            selected.add(_parse_uint(part))
    return sorted(uid for uid in selected if uid in set(available))


def _parse_uint(value: str) -> int:
    if not value.isdigit():
        raise ProtocolError(f"invalid number: {value!r}")
    return int(value)


def _parse_search_date(value: str) -> datetime:
    match = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", value.strip())
    if match is None:
        raise ProtocolError(f"invalid date: {value!r}")
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        raise ProtocolError(f"invalid month: {value!r}")
    return datetime(int(match.group(3)), month, int(match.group(1)), tzinfo=timezone.utc)


class IMAPSession:
    """Per-connection state machine."""

    NOT_AUTHENTICATED = "not_authenticated"
    AUTHENTICATED = "authenticated"
    SELECTED = "selected"
    LOGOUT = "logout"

    def __init__(self, store: MailStore, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.store = store
        self.reader = reader
        self.writer = writer
        self.state = self.NOT_AUTHENTICATED
        self.mailbox: str | None = None
        self.read_only = False

    # ---- transport helpers ----------------------------------------------

    def _write(self, text: str) -> None:
        self.writer.write(text.encode("utf-8"))

    def _write_bytes(self, payload: bytes) -> None:
        self.writer.write(payload)

    async def _flush(self) -> None:
        await self.writer.drain()

    async def _read_line(self) -> str | None:
        try:
            raw = await self.reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError:
            return None
        except asyncio.LimitOverrunError as exc:
            # The line exceeds the stream limit and no CRLF is in sight. The
            # unread bytes cannot be skipped reliably, so the session ends here;
            # continuing would re-raise on every iteration.
            raise FatalProtocolError("command line too long") from exc
        return raw[:-2].decode("utf-8", errors="replace")

    async def _read_command(self) -> list[str | _Literal] | None:
        """Read one complete command, resolving any synchronising literals.

        A line ending in ``{n}`` means the client is waiting for a continuation
        response before sending ``n`` bytes of literal data, followed by the rest
        of the command. Literals are read here so the lexer stays synchronous.
        """
        line = await self._read_line()
        if line is None:
            return None
        segments: list[str | _Literal] = []
        while True:
            match = LITERAL_SUFFIX.search(line)
            if match is None:
                segments.append(line)
                return segments
            length = int(match.group(1))
            if length > MAX_LITERAL:
                # The client will send the octets regardless, so the stream can
                # no longer be trusted to resynchronise on a command boundary.
                raise FatalProtocolError("literal too large")
            segments.append(line[: match.start()])
            if not match.group(2):
                # Synchronising literal: the client waits for our go-ahead.
                self._write("+ ready for literal data\r\n")
                await self._flush()
            payload = await self.reader.readexactly(length)
            segments.append(_Literal(payload.decode("utf-8", errors="replace")))
            continuation = await self._read_line()
            if continuation is None:
                return segments
            line = continuation

    # ---- main loop -------------------------------------------------------

    async def run(self) -> None:
        self._write(f"* OK [CAPABILITY {' '.join(CAPABILITIES)}] mail mock ready\r\n")
        await self._flush()
        while self.state != self.LOGOUT:
            tag = "*"
            try:
                segments = await self._read_command()
                if segments is None:
                    return
                if not any(
                    item.strip() if isinstance(item, str) else item.value for item in segments
                ):
                    continue
                lexer = _Lexer(segments)
                tag = lexer.next_token().value
                if not tag or tag == "*":
                    raise ProtocolError("missing command tag")
                command = lexer.next_token().upper
                await self._dispatch(tag, command, lexer)
            except ProtocolError as exc:
                self._write(f"{tag} BAD {exc}\r\n")
            except CommandError as exc:
                self._write(f"{tag} NO {exc}\r\n")
            except FatalProtocolError as exc:
                self._write(f"* BYE {exc}\r\n")
                self._write(f"{tag} BAD {exc}\r\n")
                await self._flush()
                return
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                return
            except Exception as exc:
                # Never leave the client waiting on a reply that will not come:
                # report the failure, then end the session rather than risk
                # looping on a corrupted stream.
                self._write(f"* BYE internal error: {type(exc).__name__}\r\n")
                self._write(f"{tag} BAD internal error: {type(exc).__name__}\r\n")
                try:
                    await self._flush()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                raise
            await self._flush()

    async def _dispatch(self, tag: str, command: str, lexer: _Lexer) -> None:
        started = time.monotonic()
        if command == "CAPABILITY":
            self._write(f"* CAPABILITY {' '.join(CAPABILITIES)}\r\n")
            self._write(f"{tag} OK CAPABILITY completed\r\n")
            return
        if command == "NOOP":
            self._write(f"{tag} OK NOOP completed\r\n")
            return
        if command == "LOGOUT":
            self.state = self.LOGOUT
            self._write("* BYE mail mock signing off\r\n")
            self._write(f"{tag} OK LOGOUT completed\r\n")
            return
        if command == "LOGIN":
            await self._cmd_login(tag, lexer, started)
            return
        if command in {"STARTTLS", "AUTHENTICATE", "COMPRESS", "ENABLE", "NAMESPACE", "ID", "IDLE"}:
            # Not advertised, so a conforming client never sends these. Refuse
            # explicitly rather than pretending to support them.
            self._write(f"{tag} BAD {command} is not supported by this server\r\n")
            return
        if self.state == self.NOT_AUTHENTICATED:
            raise CommandError("authenticate first")
        if command in {"LIST", "LSUB"}:
            # Every fixture mailbox is treated as subscribed, so LSUB mirrors LIST.
            # Clients such as imapflow call both when enumerating folders.
            self._cmd_list(tag, command, lexer, started)
            return
        if command in {"SELECT", "EXAMINE"}:
            self._cmd_select(tag, command, lexer, started)
            return
        if command == "CLOSE":
            self.mailbox = None
            self.state = self.AUTHENTICATED
            self._write(f"{tag} OK CLOSE completed\r\n")
            return
        if command == "UID":
            sub = lexer.next_token().upper
            if sub == "SEARCH":
                self._cmd_search(tag, lexer, started)
                return
            if sub == "FETCH":
                self._cmd_fetch(tag, lexer, started)
                return
            if sub == "STORE":
                self._cmd_store(tag, lexer, started)
                return
            raise ProtocolError(f"unsupported UID subcommand: {sub}")
        if command in {"SEARCH", "FETCH", "STORE"}:
            # Sequence-number variants are intentionally unimplemented: clients in
            # scope always use the UID forms, and supporting both would create two
            # addressing schemes to keep consistent.
            raise CommandError(f"{command} requires the UID variant on this server")
        raise ProtocolError(f"unsupported command: {command}")

    # ---- commands --------------------------------------------------------

    async def _cmd_login(self, tag: str, lexer: _Lexer, started: float) -> None:
        login = lexer.next_token().value
        password = lexer.next_token().value
        if not lexer.at_end():
            raise ProtocolError("LOGIN takes exactly two arguments")
        duration = int((time.monotonic() - started) * 1000)
        if not self.store.check_login(login, password):
            self.store.audit(
                operation=OP_LOGIN,
                request={"login": login},
                status="error",
                errcode=1,
                duration_ms=duration,
            )
            raise CommandError("[AUTHENTICATIONFAILED] invalid credentials")
        self.state = self.AUTHENTICATED
        self.store.audit(
            operation=OP_LOGIN, request={"login": login}, result_count=1, duration_ms=duration
        )
        self._write(f"{tag} OK LOGIN completed\r\n")

    def _cmd_list(self, tag: str, command: str, lexer: _Lexer, started: float) -> None:
        reference = lexer.next_token().value
        pattern = lexer.next_token().value
        if not lexer.at_end():
            raise ProtocolError(f"{command} takes exactly two arguments")
        names: list[str] = []
        if pattern == "" and reference == "":
            # RFC 3501: an empty pattern asks only for the hierarchy delimiter.
            # Clients without NAMESPACE use this to discover the separator.
            self._write(f'* {command} (\\Noselect) "/" ""\r\n')
        else:
            regex = _pattern_to_regex(pattern)
            for mailbox in self.store.mailboxes:
                if not regex.fullmatch(mailbox["name"]):
                    continue
                flags = ["\\HasNoChildren"]
                if mailbox["special_use"]:
                    flags.append(mailbox["special_use"])
                names.append(mailbox["name"])
                self._write(f'* {command} ({" ".join(flags)}) "/" "{mailbox["name"]}"\r\n')
        self.store.audit(
            operation=OP_LIST,
            request={"command": command, "reference": reference, "pattern": pattern},
            resource_ids=names,
            result_count=len(names),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._write(f"{tag} OK {command} completed\r\n")

    def _cmd_select(self, tag: str, command: str, lexer: _Lexer, started: float) -> None:
        name = lexer.next_token().value
        if not lexer.at_end():
            raise ProtocolError(f"{command} takes exactly one argument")
        mailbox = self.store.mailbox(name)
        if mailbox is None:
            # RFC 3501 6.3.1: a failed SELECT/EXAMINE leaves no mailbox selected,
            # so any previous selection must be dropped before reporting failure.
            self.mailbox = None
            self.read_only = False
            self.state = self.AUTHENTICATED
            self.store.audit(
                operation=OP_SELECT,
                request={"mailbox": name, "readOnly": command == "EXAMINE"},
                status="error",
                errcode=1,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise CommandError("[NONEXISTENT] mailbox does not exist")
        messages = self.store.messages_in(mailbox["name"])
        unseen = [item for item in messages if "\\Seen" not in self.store.flags_for(item["id"])]
        next_uid = max((item["uid"] for item in messages), default=0) + 1
        self.mailbox = mailbox["name"]
        self.read_only = command == "EXAMINE"
        self.state = self.SELECTED
        self._write(f"* {len(messages)} EXISTS\r\n")
        self._write("* 0 RECENT\r\n")
        self._write(f"* FLAGS ({' '.join(SYSTEM_FLAGS)})\r\n")
        self._write(f"* OK [PERMANENTFLAGS ({' '.join(SYSTEM_FLAGS)})] limited\r\n")
        self._write(f"* OK [UIDVALIDITY {mailbox['uid_validity']}] UIDs valid\r\n")
        self._write(f"* OK [UIDNEXT {next_uid}] predicted next UID\r\n")
        if unseen:
            first_unseen = min(messages.index(item) for item in unseen) + 1
            self._write(f"* OK [UNSEEN {first_unseen}] first unseen message\r\n")
        self.store.audit(
            operation=OP_SELECT,
            request={"mailbox": mailbox["name"], "readOnly": self.read_only},
            resource_ids=[mailbox["name"]],
            result_count=len(messages),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        access = "READ-ONLY" if self.read_only else "READ-WRITE"
        self._write(f"{tag} OK [{access}] {command} completed\r\n")

    def _require_selected(self) -> str:
        if self.state != self.SELECTED or self.mailbox is None:
            raise CommandError("no mailbox selected")
        return self.mailbox

    def _cmd_search(self, tag: str, lexer: _Lexer, started: float) -> None:
        mailbox = self._require_selected()
        criteria = _collect_tokens(lexer)
        if not criteria:
            raise ProtocolError("SEARCH requires at least one criterion")
        if criteria and criteria[0].upper == "CHARSET":
            charset = criteria[1].value.upper() if len(criteria) > 1 else ""
            if charset not in {"UTF-8", "US-ASCII"}:
                raise CommandError(f"[BADCHARSET] unsupported charset: {charset}")
            criteria = criteria[2:]
        messages = self.store.messages_in(mailbox)
        matcher = _SearchMatcher(criteria, self.store, [item["uid"] for item in messages])
        matched = [item for item in messages if matcher.matches(item)]
        uids = [item["uid"] for item in matched]
        self._write("* SEARCH" + "".join(f" {uid}" for uid in uids) + "\r\n")
        self.store.audit(
            operation=OP_SEARCH,
            request={"mailbox": mailbox, "criteria": matcher.describe()},
            resource_ids=[f"{mailbox}:{uid}" for uid in uids],
            result_count=len(uids),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._write(f"{tag} OK UID SEARCH completed\r\n")

    def _cmd_fetch(self, tag: str, lexer: _Lexer, started: float) -> None:
        mailbox = self._require_selected()
        messages = self.store.messages_in(mailbox)
        available = [item["uid"] for item in messages]
        uid_set = lexer.next_token().value
        requested = _parse_uid_set(uid_set, available)
        items = _parse_fetch_items(lexer)
        if len(requested) > self.store.max_fetch:
            raise CommandError(f"refusing to fetch more than {self.store.max_fetch} messages at once")
        by_uid = {item["uid"]: item for item in messages}
        touched: list[str] = []
        for uid in requested:
            message = by_uid[uid]
            sequence = available.index(uid) + 1
            # RFC 3501 6.4.5: the non-peek BODY[]/RFC822 spellings set \Seen.
            # Flags are updated before rendering so the reported FLAGS match.
            if "BODY[]!PEEK" in items and not self.read_only:
                current = self.store.flags_for(message["id"])
                if "\\Seen" not in current:
                    self.store.update_flags(message["id"], [*current, "\\Seen"])
            parts, body_payload = self._render_fetch_items(message, items)
            self._write(f"* {sequence} FETCH ({' '.join(parts)}")
            if body_payload is not None:
                prefix, payload = body_payload
                self._write(f" {prefix}{{{len(payload)}}}\r\n")
                self._write_bytes(payload)
                self._write(")\r\n")
            else:
                self._write(")\r\n")
            touched.append(f"{mailbox}:{uid}")
        self.store.audit(
            operation=OP_FETCH,
            request={"mailbox": mailbox, "uids": uid_set, "items": sorted(items)},
            resource_ids=touched,
            result_count=len(requested),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._write(f"{tag} OK UID FETCH completed\r\n")

    def _render_fetch_items(
        self, message: dict[str, Json], items: set[str]
    ) -> tuple[list[str], tuple[str, bytes] | None]:
        parts: list[str] = []
        body_payload: tuple[str, bytes] | None = None
        source = self.store.message_source(message["id"])
        # UID is always echoed: clients rely on it to correlate responses.
        parts.append(f"UID {message['uid']}")
        if "FLAGS" in items:
            parts.append(f"FLAGS ({' '.join(self.store.flags_for(message['id']))})")
        if "ENVELOPE" in items:
            parts.append(f"ENVELOPE {build_envelope(message)}")
        if "INTERNALDATE" in items:
            parts.append(f'INTERNALDATE "{_imap_datetime(message["internal_date"])}"')
        if "RFC822.SIZE" in items:
            parts.append(f"RFC822.SIZE {len(source)}")
        if "BODY[]" in items:
            # Both spellings are answered as BODY[]; only the \Seen side effect
            # differs, and that is applied by the caller before rendering.
            body_payload = ("BODY[] ", source)
        return parts, body_payload

    def _cmd_store(self, tag: str, lexer: _Lexer, started: float) -> None:
        mailbox = self._require_selected()
        if self.read_only:
            raise CommandError("mailbox is open read-only")
        messages = self.store.messages_in(mailbox)
        available = [item["uid"] for item in messages]
        uid_set = lexer.next_token().value
        requested = _parse_uid_set(uid_set, available)
        operation = lexer.next_token().upper
        silent = operation.endswith(".SILENT")
        if silent:
            operation = operation[: -len(".SILENT")]
        if operation not in {"FLAGS", "+FLAGS", "-FLAGS"}:
            raise ProtocolError(f"unsupported STORE operation: {operation}")
        flags = _parse_flag_list(lexer)
        by_uid = {item["uid"]: item for item in messages}
        touched: list[str] = []
        for uid in requested:
            message = by_uid[uid]
            current = set(self.store.flags_for(message["id"]))
            if operation == "FLAGS":
                updated = set(flags)
            elif operation == "+FLAGS":
                updated = current | set(flags)
            else:
                updated = current - set(flags)
            stored = self.store.update_flags(message["id"], updated)
            touched.append(f"{mailbox}:{uid}")
            if not silent:
                sequence = available.index(uid) + 1
                self._write(f"* {sequence} FETCH (UID {uid} FLAGS ({' '.join(stored)}))\r\n")
        self.store.audit(
            operation=OP_STORE,
            request={"mailbox": mailbox, "uids": uid_set, "operation": operation, "flags": flags},
            resource_ids=touched,
            result_count=len(requested),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._write(f"{tag} OK UID STORE completed\r\n")


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    out = []
    for char in pattern:
        if char == "*":
            out.append(".*")
        elif char == "%":
            out.append("[^/]*")
        else:
            out.append(re.escape(char))
    return re.compile("".join(out), re.IGNORECASE)


def _collect_tokens(lexer: _Lexer) -> list[Token]:
    tokens: list[Token] = []
    while not lexer.at_end():
        tokens.append(lexer.next_token())
    return tokens


def _parse_flag_list(lexer: _Lexer) -> list[str]:
    tokens = _collect_tokens(lexer)
    values = [token.value for token in tokens if token.value not in {"(", ")"}]
    out: list[str] = []
    for value in values:
        match = next((known for known in SYSTEM_FLAGS if known.lower() == value.lower()), None)
        if match is None:
            # Keywords are accepted syntactically but ignored, mirroring servers
            # that expose a fixed PERMANENTFLAGS set.
            continue
        if match not in out:
            out.append(match)
    return out


SIMPLE_FETCH_ITEMS = frozenset(
    {"UID", "FLAGS", "ENVELOPE", "INTERNALDATE", "RFC822.SIZE"}
)
FETCH_MACROS = {
    "FAST": {"FLAGS", "INTERNALDATE", "RFC822.SIZE"},
    "ALL": {"FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE"},
    "FULL": {"FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE", "BODY[]"},
}
_BODY_ITEM = re.compile(r"^(BODY|BODY\.PEEK)\[([^\]]*)\](<[^>]*>)?$")


def _parse_fetch_items(lexer: _Lexer) -> set[str]:
    """Normalise a FETCH data-item list into canonical item names.

    Each token is matched against an exact whitelist. Anything unrecognised is
    rejected rather than ignored, so a client never receives a successful reply
    that silently omits data it asked for.

    Returns canonical names; ``BODY[]`` means the full source, and the separate
    ``BODY[]!PEEK`` marker records that the non-peek spelling was used, which
    obliges the caller to set ``\\Seen``.
    """
    raw = lexer.rest().strip()
    if not raw:
        raise ProtocolError("FETCH requires data items")
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    tokens = raw.split()
    if not tokens:
        raise ProtocolError("FETCH requires data items")
    items: set[str] = set()
    for token in tokens:
        name = token.upper()
        if name in SIMPLE_FETCH_ITEMS:
            items.add(name)
            continue
        if name in FETCH_MACROS:
            items |= FETCH_MACROS[name]
            continue
        if name == "RFC822":
            # Equivalent to BODY[], including the \Seen side effect.
            items.add("BODY[]")
            items.add("BODY[]!PEEK")
            continue
        match = _BODY_ITEM.match(name)
        if match is not None:
            if match.group(2).strip():
                raise ProtocolError("only BODY[] with an empty section is supported")
            if match.group(3):
                raise ProtocolError("partial BODY[] fetches are not supported")
            items.add("BODY[]")
            if match.group(1) == "BODY":
                items.add("BODY[]!PEEK")
            continue
        raise ProtocolError(f"unsupported FETCH data item: {token}")
    return items


class _SearchMatcher:
    """Compiles IMAP SEARCH criteria into a predicate over stored messages."""

    def __init__(self, tokens: list[Token], store: MailStore, available: list[int]) -> None:
        self.store = store
        self._predicates: list[Callable[[dict[str, Json]], bool]] = []
        self._described: list[str] = []
        self._parse(tokens, available)

    def _parse(self, tokens: list[Token], available: list[int]) -> None:
        index = 0
        while index < len(tokens):
            token = tokens[index]
            key = token.upper
            index += 1
            if key in {"(", ")"}:
                continue
            if key == "ALL":
                self._described.append("ALL")
                continue
            if key in {"SEEN", "UNSEEN", "FLAGGED", "UNFLAGGED", "ANSWERED", "UNANSWERED", "DELETED", "UNDELETED", "DRAFT", "UNDRAFT"}:
                negated = key.startswith("UN")
                flag = f"\\{(key[2:] if negated else key).capitalize()}"
                self._predicates.append(
                    lambda message, flag=flag, negated=negated: (
                        flag in self.store.flags_for(message["id"])
                    )
                    is not negated
                )
                self._described.append(key)
                continue
            if key == "NEW":
                self._predicates.append(
                    lambda message: "\\Seen" not in self.store.flags_for(message["id"])
                )
                self._described.append(key)
                continue
            if key in {"FROM", "TO", "CC", "SUBJECT", "BODY", "TEXT"}:
                if index >= len(tokens):
                    raise ProtocolError(f"{key} requires a value")
                needle = tokens[index].value
                index += 1
                self._predicates.append(self._text_predicate(key, needle))
                # Search values can contain copied message-body text. Audit only
                # the criterion type, never the caller-supplied literal.
                self._described.append(key)
                continue
            if key in {"SINCE", "BEFORE", "ON", "SENTSINCE", "SENTBEFORE", "SENTON"}:
                if index >= len(tokens):
                    raise ProtocolError(f"{key} requires a date")
                boundary = _parse_search_date(tokens[index].value)
                index += 1
                self._predicates.append(self._date_predicate(key, boundary))
                self._described.append(key)
                continue
            if key == "UID":
                if index >= len(tokens):
                    raise ProtocolError("UID requires a sequence set")
                wanted = set(_parse_uid_set(tokens[index].value, available))
                index += 1
                self._predicates.append(lambda message, wanted=wanted: message["uid"] in wanted)
                self._described.append("UID")
                continue
            if key == "HEADER":
                if index + 1 >= len(tokens):
                    raise ProtocolError("HEADER requires a field and value")
                field = tokens[index].value
                needle = tokens[index + 1].value
                index += 2
                self._predicates.append(self._header_predicate(field, needle))
                self._described.append("HEADER")
                continue
            if key in {"LARGER", "SMALLER"}:
                if index >= len(tokens):
                    raise ProtocolError(f"{key} requires a size")
                size = _parse_uint(tokens[index].value)
                index += 1
                self._predicates.append(self._size_predicate(key, size))
                self._described.append(key)
                continue
            if key == "NOT":
                raise CommandError("NOT is not supported by this server")
            if key == "OR":
                raise CommandError("OR is not supported by this server")
            raise CommandError(f"unsupported search key: {key}")

    def _text_predicate(self, key: str, needle: str) -> Callable[[dict[str, Json]], bool]:
        folded = needle.casefold()

        def predicate(message: dict[str, Json]) -> bool:
            if key == "SUBJECT":
                haystacks = [message["subject"]]
            elif key == "FROM":
                haystacks = [message["from"]["address"], message["from"].get("name", "")]
            elif key == "TO":
                haystacks = [part for item in message["to"] for part in (item["address"], item.get("name", ""))]
            elif key == "CC":
                haystacks = [part for item in message["cc"] for part in (item["address"], item.get("name", ""))]
            elif key == "BODY":
                haystacks = [message["body_text"], message["body_html"] or ""]
            else:  # TEXT searches headers and body
                haystacks = [
                    message["subject"],
                    message["from"]["address"],
                    message["body_text"],
                    message["body_html"] or "",
                ]
            return any(folded in (value or "").casefold() for value in haystacks)

        return predicate

    @staticmethod
    def _date_predicate(key: str, boundary: datetime) -> Callable[[dict[str, Json]], bool]:
        def predicate(message: dict[str, Json]) -> bool:
            # RFC 3501 compares dates in the message's own timezone and ignores
            # the time component. Converting to UTC first would shift any message
            # sent before 08:00 in a +0800 fixture into the previous day.
            actual = message["internal_date"].date()
            wanted = boundary.date()
            if key in {"SINCE", "SENTSINCE"}:
                return actual >= wanted
            if key in {"BEFORE", "SENTBEFORE"}:
                return actual < wanted
            return actual == wanted

        return predicate

    def _header_predicate(self, field: str, needle: str) -> Callable[[dict[str, Json]], bool]:
        folded_field = field.casefold()
        folded_needle = needle.casefold()

        def predicate(message: dict[str, Json]) -> bool:
            if folded_field == "subject":
                value = message["subject"]
            elif folded_field == "from":
                value = message["from"]["address"]
            elif folded_field == "message-id":
                value = message["message_id"]
            else:
                source = self.store.message_source(message["id"]).decode("utf-8", errors="replace")
                headers = source.split("\r\n\r\n", 1)[0]
                return any(
                    line.casefold().startswith(f"{folded_field}:") and folded_needle in line.casefold()
                    for line in headers.splitlines()
                )
            return folded_needle in (value or "").casefold()

        return predicate

    def _size_predicate(self, key: str, size: int) -> Callable[[dict[str, Json]], bool]:
        def predicate(message: dict[str, Json]) -> bool:
            actual = len(self.store.message_source(message["id"]))
            return actual > size if key == "LARGER" else actual < size

        return predicate

    def matches(self, message: dict[str, Json]) -> bool:
        return all(predicate(message) for predicate in self._predicates)

    def describe(self) -> list[str]:
        return list(self._described)


async def start_imap_server(store: MailStore, *, host: str = "127.0.0.1", port: int = 0) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = IMAPSession(store, reader, writer)
        try:
            await session.run()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    return await asyncio.start_server(handle, host, port)
