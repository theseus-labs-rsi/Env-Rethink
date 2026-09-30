"""A minimal ESMTP receiver that records submitted mail as a scoreable artifact.

Written against the standard library only. STARTTLS is not advertised: the
listener is plaintext on 127.0.0.1, and clients that see STARTTLS advertised will
try to upgrade.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Any

from .core import OP_SEND, MailStore


Json = Any

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_LINE = 64 * 1024
MAX_RECIPIENTS = 100

_ADDRESS = re.compile(r"<([^>]*)>|(\S+)")


def _extract_address(argument: str) -> str | None:
    """Pull the address out of ``FROM:<a@b>`` / ``TO:<a@b>`` style parameters."""
    _, _, remainder = argument.partition(":")
    remainder = remainder.strip()
    if not remainder:
        return None
    # Drop ESMTP parameters such as SIZE=... or BODY=8BITMIME.
    match = _ADDRESS.match(remainder)
    if match is None:
        return None
    return (match.group(1) if match.group(1) is not None else match.group(2)).strip()


class SMTPSession:
    def __init__(self, store: MailStore, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.store = store
        self.reader = reader
        self.writer = writer
        self.authenticated = False
        self.mail_from: str | None = None
        self.recipients: list[str] = []

    def _write(self, text: str) -> None:
        self.writer.write(text.encode("utf-8"))

    async def _reply(self, text: str) -> None:
        self._write(text if text.endswith("\r\n") else text + "\r\n")
        await self.writer.drain()

    async def _read_line(self) -> str | None:
        try:
            raw = await self.reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError:
            return None
        except asyncio.LimitOverrunError:
            return None
        return raw[:-2].decode("utf-8", errors="replace")

    def _reset(self) -> None:
        self.mail_from = None
        self.recipients = []

    async def run(self) -> None:
        await self._reply("220 mail.mock ESMTP mail mock ready")
        while True:
            line = await self._read_line()
            if line is None:
                return
            verb, _, argument = line.partition(" ")
            verb = verb.upper()
            argument = argument.strip()

            if verb in {"EHLO", "HELO"}:
                if verb == "HELO":
                    await self._reply(f"250 mail.mock hello {argument or 'client'}")
                else:
                    self._write("250-mail.mock hello " + (argument or "client") + "\r\n")
                    self._write(f"250-SIZE {MAX_MESSAGE_BYTES}\r\n")
                    self._write("250-8BITMIME\r\n")
                    self._write("250-SMTPUTF8\r\n")
                    self._write("250-AUTH PLAIN LOGIN\r\n")
                    await self._reply("250 HELP")
                self._reset()
                continue
            if verb == "AUTH":
                await self._cmd_auth(argument)
                continue
            if verb == "NOOP":
                await self._reply("250 OK")
                continue
            if verb == "RSET":
                self._reset()
                await self._reply("250 OK")
                continue
            if verb == "QUIT":
                await self._reply("221 mail.mock closing connection")
                return
            if verb == "STARTTLS":
                await self._reply("454 TLS not available on this server")
                continue
            if verb == "MAIL":
                if not self.authenticated:
                    await self._reply("530 Authentication required")
                    continue
                address = _extract_address(argument)
                if address is None:
                    await self._reply("501 Syntax error in MAIL parameters")
                    continue
                self._reset()
                self.mail_from = address
                await self._reply("250 OK")
                continue
            if verb == "RCPT":
                if self.mail_from is None:
                    await self._reply("503 Need MAIL before RCPT")
                    continue
                address = _extract_address(argument)
                if not address:
                    await self._reply("501 Syntax error in RCPT parameters")
                    continue
                if len(self.recipients) >= MAX_RECIPIENTS:
                    await self._reply("452 Too many recipients")
                    continue
                self.recipients.append(address)
                await self._reply("250 OK")
                continue
            if verb == "DATA":
                await self._cmd_data()
                continue
            await self._reply(f"500 Unrecognised command: {verb}")

    async def _cmd_auth(self, argument: str) -> None:
        mechanism, _, initial = argument.partition(" ")
        mechanism = mechanism.upper()
        initial = initial.strip()
        if mechanism == "PLAIN":
            if not initial:
                await self._reply("334 ")
                line = await self._read_line()
                if line is None:
                    return
                initial = line.strip()
            login, password = _decode_plain(initial)
        elif mechanism == "LOGIN":
            await self._reply("334 " + base64.b64encode(b"Username:").decode())
            login_line = await self._read_line()
            if login_line is None:
                return
            await self._reply("334 " + base64.b64encode(b"Password:").decode())
            password_line = await self._read_line()
            if password_line is None:
                return
            login = _decode_b64(login_line.strip())
            password = _decode_b64(password_line.strip())
        else:
            await self._reply("504 Unrecognised authentication mechanism")
            return
        if login is None or password is None or not self.store.check_login(login, password):
            self.store.audit(
                operation=OP_SEND,
                request={"stage": "auth", "login": login or ""},
                status="error",
                errcode=1,
            )
            await self._reply("535 Authentication credentials invalid")
            return
        self.authenticated = True
        await self._reply("235 Authentication successful")

    async def _read_long_line(self, consumed: int) -> bytes | None:
        """Read a DATA line that exceeds the stream limit, in bounded chunks."""
        parts: list[bytes] = []
        while True:
            try:
                parts.append(await self.reader.readexactly(consumed))
            except asyncio.IncompleteReadError as exc:
                parts.append(exc.partial)
                return b"".join(parts) or None
            try:
                parts.append(await self.reader.readuntil(b"\r\n"))
                return b"".join(parts)
            except asyncio.IncompleteReadError as exc:
                parts.append(exc.partial)
                return b"".join(parts) or None
            except asyncio.LimitOverrunError as exc:
                consumed = exc.consumed

    async def _cmd_data(self) -> None:
        if self.mail_from is None or not self.recipients:
            await self._reply("503 Need MAIL and RCPT before DATA")
            return
        await self._reply("354 End data with <CR><LF>.<CR><LF>")
        started = time.monotonic()
        chunks: list[bytes] = []
        total = 0
        truncated = False
        while True:
            try:
                raw = await self.reader.readuntil(b"\r\n")
            except asyncio.IncompleteReadError:
                return
            except asyncio.LimitOverrunError as exc:
                # A single line longer than the stream limit. Agent-composed HTML
                # bodies are routinely one long line, so read it in chunks rather
                # than dropping the message without a reply.
                raw = await self._read_long_line(exc.consumed)
                if raw is None:
                    return
            if raw == b".\r\n":
                break
            # Undo dot-stuffing (RFC 5321 4.5.2).
            if raw.startswith(b".."):
                raw = raw[1:]
            total += len(raw)
            if total > MAX_MESSAGE_BYTES:
                truncated = True
                continue
            chunks.append(raw)
        if truncated:
            self._reset()
            await self._reply("552 Message exceeds maximum permitted size")
            return
        payload = b"".join(chunks)
        record = self.store.record_sent_message(
            envelope_from=self.mail_from, recipients=list(self.recipients), payload=payload
        )
        self.store.audit(
            operation=OP_SEND,
            request={
                "from": self.mail_from,
                "to": list(self.recipients),
                "subject": record["subject"],
                "size": record["size"],
            },
            resource_ids=list(self.recipients),
            result_count=1,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._reset()
        await self._reply(f"250 OK: queued as {record['sequence']:04d}")


def _decode_b64(value: str) -> str | None:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except Exception:
        return None


def _decode_plain(value: str) -> tuple[str | None, str | None]:
    decoded = _decode_b64(value)
    if decoded is None:
        return None, None
    parts = decoded.split("\x00")
    if len(parts) != 3:
        return None, None
    # authzid is ignored; authcid and password are what matter.
    return parts[1], parts[2]


async def start_smtp_server(store: MailStore, *, host: str = "127.0.0.1", port: int = 0) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = SMTPSession(store, reader, writer)
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

    return await asyncio.start_server(handle, host, port, limit=MAX_LINE)
