"""`Mailbox`: unread emails of an IMAP folder."""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import hashlib
import imaplib
import os
import re
import ssl
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from awa._internal.logging import get_logger
from awa._internal.util import DurationLike
from awa.automation._time import as_utc
from awa.automation._trigger_base import PollingTrigger
from awa.automation._trigger_files import document_text, is_device_name
from awa.automation.models import TriggerEvent
from awa.automation.templates import TemplateLike
from awa.core.config import env_value
from awa.errors import ConfigurationError, MissingConfigurationError

_logger = get_logger("automation.triggers")

MailAction = Literal["seen", "move", "delete", "keep"]
"""What `Mailbox` does with a message once its run is queued or refused."""


class Mailbox(PollingTrigger):
    """Start a run for every unread email of an IMAP folder.

    Messages are fetched without marking them read; once the run is queued, `on_success` applies
    (`"seen"`, `"move"` to `move_to`, delete, or keep); a refused message gets `on_failure`.
    Credentials come only from environment variables. Events carry `message_id`, `from`, `to`, `cc`,
    `subject`, `date`, `text` and `attachments` (with `attachments_dir` also their saved `path` and
    `text`; larger than `max_attachment_bytes` only listed as `skipped`). The dedup key is the
    `Message-ID`; a message that cannot be processed is logged and offered again after a restart.

    Args:
        port: Default 993 with SSL, 143 without.
        search: IMAP search criteria.
        max_text_chars: The body text and each attachment's text are cut after this many characters.
        attachments_dir: Folder attachments are saved to, one subfolder per message.
        connect: Opens a logged-out `imaplib.IMAP4` connection (tests).

    Example:
        ```python
        Mailbox("imap.example.com", username_env="AP_IMAP_USER",
                password_env="AP_IMAP_PASSWORD", folder="Invoices",
                on_success="move", move_to="Invoices/Done",
                thread_id="mail-{payload.subject|slug}")
        ```
    """

    def __init__(
        self,
        host: str,
        *,
        username_env: str = "AWA_IMAP_USER",
        password_env: str = "AWA_IMAP_PASSWORD",
        port: int | None = None,
        ssl: bool = True,
        folder: str = "INBOX",
        search: str = "UNSEEN",
        on_success: MailAction = "seen",
        move_to: str | None = None,
        on_failure: MailAction = "keep",
        failed_folder: str | None = None,
        name: str = "mailbox",
        poll_interval: DurationLike = timedelta(seconds=60),
        max_messages: int = 20,
        max_text_chars: int = 50_000,
        attachments_dir: str | os.PathLike[str] | None = None,
        max_attachment_bytes: int = 25 * 1024 * 1024,
        connect: Callable[[], imaplib.IMAP4] | None = None,
        input: TemplateLike | None = None,
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
    ) -> None:
        super().__init__(
            name, poll_interval=poll_interval, input=input, thread_id=thread_id, dedup=dedup
        )
        for action, target, label in (
            (on_success, move_to, "move_to"),
            (on_failure, failed_folder, "failed_folder"),
        ):
            if action not in ("seen", "move", "delete", "keep"):
                raise ConfigurationError(f"Unknown mailbox action {action!r}")
            if action == "move" and not target:
                raise ConfigurationError(f"Mailbox action 'move' needs {label}=")
        self.host = host
        self.username_env = username_env
        self.password_env = password_env
        self.port = port
        self.ssl = ssl
        self.folder = folder
        self.search = search
        self.on_success: MailAction = on_success
        self.move_to = move_to
        self.on_failure: MailAction = on_failure
        self.failed_folder = failed_folder
        self.max_messages = max_messages
        self.max_text_chars = max_text_chars
        self.attachments_dir = Path(attachments_dir) if attachments_dir is not None else None
        self.max_attachment_bytes = max_attachment_bytes
        self._connect = connect
        self._conn: imaplib.IMAP4 | None = None
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._broken: set[str] = set()  # messages that failed to process; logged once

    def describe(self) -> dict[str, Any]:
        """See `BaseTrigger.describe`."""

        return {**super().describe(), "host": self.host, "folder": self.folder}

    def required_env(self) -> tuple[str, ...]:
        """The user name and password variables. With the defaults `AWA_IMAP_USER` and
        `AWA_IMAP_PASSWORD` unset, the IMAP connector's `IMAP_USERNAME` and `IMAP_PASSWORD`
        are used when set, so one login serves the trigger and the connector."""

        names = []
        for name, default, shared in (
            (self.username_env, "AWA_IMAP_USER", "IMAP_USERNAME"),
            (self.password_env, "AWA_IMAP_PASSWORD", "IMAP_PASSWORD"),
        ):
            if name == default and not env_value(name) and env_value(shared):
                name = shared
            names.append(name)
        return (names[0], names[1])

    async def poll(self, now: datetime) -> list[TriggerEvent]:
        """Fetch unread messages as events."""

        return await asyncio.to_thread(self._fetch, as_utc(now))

    async def ack(self, event: TriggerEvent, error: BaseException | None) -> None:
        """Apply `on_success` or `on_failure` to the message."""

        uid = str(event.metadata.get("uid", ""))
        if not uid:
            return
        try:
            await asyncio.to_thread(self._finish, uid, error is None)
        finally:
            self._pending.discard(uid)

    async def close(self) -> None:
        """Log out of the server."""

        await asyncio.to_thread(self._logout)

    def _connection(self) -> imaplib.IMAP4:
        if self._conn is not None:
            try:
                self._conn.noop()
                return self._conn
            except (imaplib.IMAP4.error, OSError):
                self._abandon()
        user_env, password_env = self.required_env()
        user = env_value(user_env) or ""
        password = env_value(password_env) or ""
        if not user or not password:
            raise MissingConfigurationError(
                f"Mailbox {self.name!r} needs the environment variables {user_env} "
                f"and {password_env} (set them in .env)"
            )
        if self._connect is not None:
            conn = self._connect()
        elif self.ssl:
            conn = imaplib.IMAP4_SSL(
                self.host, self.port or 993, ssl_context=ssl.create_default_context(), timeout=30
            )
        else:
            conn = imaplib.IMAP4(self.host, self.port or 143, timeout=30)
        try:
            conn.login(user, password)
            status, _ = conn.select(_quote(self.folder))
            if status != "OK":
                raise ConfigurationError(
                    f"Mailbox {self.name!r}: cannot open folder {self.folder!r}"
                )
        except BaseException:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                conn.logout()
            raise
        self._conn = conn
        return conn

    def _abandon(self) -> None:
        """Drop a connection that failed, closing it if it still can be."""

        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                conn.logout()

    def _fetch(self, now: datetime) -> list[TriggerEvent]:
        with self._lock:
            conn = self._connection()
            try:
                status, data = conn.uid("SEARCH", None, self.search)  # type: ignore[arg-type]  # typeshed lacks None (default charset)
            except (imaplib.IMAP4.error, OSError):
                self._abandon()
                raise
            if status != "OK":
                raise ConfigurationError(f"Mailbox {self.name!r}: search {self.search!r} failed")
            uids = [uid.decode() for uid in (data[0] or b"").split()]
            events: list[TriggerEvent] = []
            for uid in uids:
                if uid in self._pending or uid in self._broken:
                    continue
                if len(events) >= self.max_messages:
                    break
                try:
                    status, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                except (imaplib.IMAP4.error, OSError) as exc:
                    _logger.warning(
                        "Mailbox %s: the connection failed (%s); keeping %d message(s)",
                        self.name,
                        exc,
                        len(events),
                    )
                    self._abandon()
                    break
                raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
                if status != "OK" or not isinstance(raw, bytes):
                    continue
                try:
                    event = self._event(uid, raw, now)
                except Exception:  # noqa: BLE001 - one bad message must not hide the others
                    _logger.warning("Mailbox %s: message %s skipped", self.name, uid, exc_info=True)
                    self._broken.add(uid)
                    continue
                events.append(event)
                self._pending.add(uid)
            return events

    def _event(self, uid: str, raw: bytes, now: datetime) -> TriggerEvent:
        message = email.message_from_bytes(raw, policy=email.policy.default)
        message_id = str(message.get("Message-ID", "")).strip()
        folder = None
        if self.attachments_dir is not None:
            key = hashlib.sha256((message_id or f"{self.folder}:{uid}").encode()).hexdigest()
            folder = self.attachments_dir / f"{now:%Y%m%d}-{key[:16]}"
        used: set[str] = set()
        attachments = [
            self._attachment(part, number, folder, used)
            for number, part in enumerate(
                (p for p in message.walk() if p.get_content_disposition() == "attachment"), start=1
            )
        ]
        text = _body_text(message)[: self.max_text_chars]
        payload = {
            "uid": uid,
            "message_id": message_id,
            "from": str(message.get("From", "")),
            "to": str(message.get("To", "")),
            "cc": str(message.get("Cc", "")),
            "subject": str(message.get("Subject", "")),
            "date": str(message.get("Date", "")),
            "text": text,
            "attachments": attachments,
        }
        return TriggerEvent(
            source=self.name,
            payload=payload,
            received_at=now,
            dedup_key=f"{self.name}:{message_id or self.folder + ':' + uid}",
            metadata={"uid": uid, "folder": self.folder},
        )

    def _attachment(
        self, part: Any, number: int, folder: Path | None, used: set[str]
    ) -> dict[str, Any]:
        data = part.get_payload(decode=True) or b""
        entry: dict[str, Any] = {
            "filename": part.get_filename(),
            "content_type": part.get_content_type(),
            "size": len(data),
        }
        if len(data) > self.max_attachment_bytes:
            entry["skipped"] = f"larger than max_attachment_bytes ({self.max_attachment_bytes})"
        elif folder is not None:
            name = _safe_filename(part.get_filename(), number)
            name = f"{number}-{name}" if name in used else name
            used.add(name)
            path = folder / name
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            entry["path"] = str(path)
            entry.update(document_text(path, data, max_chars=self.max_text_chars))
        return entry

    def _finish(self, uid: str, success: bool) -> None:
        action = self.on_success if success else self.on_failure
        target = self.move_to if success else self.failed_folder
        if action == "keep":
            return
        with self._lock:
            conn = self._connection()
            try:
                if action == "seen":
                    _require(conn.uid("STORE", uid, "+FLAGS", "(\\Seen)"))
                    return
                capabilities = getattr(conn, "capabilities", ())
                if action == "move" and target:
                    if "MOVE" in capabilities:
                        _require(conn.uid("MOVE", uid, _quote(target)))
                        return
                    _require(conn.uid("COPY", uid, _quote(target)))
                _require(conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)"))
                # UID EXPUNGE removes only this message, not others a person marked deleted.
                if "UIDPLUS" in capabilities:
                    _require(conn.uid("EXPUNGE", uid))
                else:
                    conn.expunge()
            except (imaplib.IMAP4.error, OSError):
                self._abandon()
                raise

    def _logout(self) -> None:
        with self._lock:
            self._abandon()


def _require(response: tuple[str, Any]) -> None:
    if response[0] != "OK":
        raise imaplib.IMAP4.error(f"the server answered {response[0]}")


def _safe_filename(name: str | None, number: int) -> str:
    """A sender's file name made safe to save: no folders, no special characters."""

    cleaned = re.sub(r"[^\w.\- ]", "_", Path(str(name or "").replace("\\", "/")).name).strip(" .")
    if is_device_name(cleaned):  # `NUL.txt` is no file on Windows
        cleaned = f"_{cleaned}"
    return cleaned[:150] or f"attachment-{number}"


def _quote(folder: str) -> str:
    if folder.startswith('"'):
        return folder
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _body_text(message: Any) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, ValueError):
        content = (body.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    text = str(content)
    if body.get_content_type() == "text/html":
        from awa.toolkits.web import html_to_text

        text = html_to_text(text)[1]
    return text
