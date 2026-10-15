"""Triggers of `awa.automation`: schedules, webhooks, file drops, mailboxes, queues and document intake, and their failure modes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import imaplib
import io
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from awa.automation import (
    FileDrop,
    InMemoryEventQueue,
    InMemoryRunStore,
    Mailbox,
    QueueTrigger,
    Schedule,
    TriggerContext,
    TriggerEvent,
    Webhook,
)
from awa.automation._trigger_files import document_text
from awa.automation.triggers import PollingTrigger
from awa.errors import (
    ConfigurationError,
    InvalidCronError,
    MissingConfigurationError,
    WebhookVerificationError,
)
from awa.testing import FakeClock


async def take(iterator: AsyncIterator[TriggerEvent], count: int) -> list[TriggerEvent]:
    events: list[TriggerEvent] = []
    async for event in iterator:
        events.append(event)
        if len(events) == count:
            break
    return events


def context(clock: FakeClock, store: InMemoryRunStore | None = None) -> TriggerContext:
    return TriggerContext(
        automation="auto",
        state=store if store is not None else InMemoryRunStore(),
        clock=clock,
        sleep=clock.sleep,
    )


# ---------------------------------------------------------------------- schedule


async def test_schedule_fires_each_tick_once_starting_from_now() -> None:
    clock = FakeClock(datetime(2026, 3, 2, 8, 59, 30, tzinfo=UTC))
    schedule = Schedule("0 9 * * *", name="daily")
    events = await take(schedule.events(context(clock)), 2)

    assert [e.payload["scheduled_for"] for e in events] == [
        "2026-03-02T09:00:00+00:00",
        "2026-03-03T09:00:00+00:00",
    ]
    assert events[0].dedup_key == "daily@2026-03-02T09:00:00+00:00"
    assert events[0].payload["date"] == "2026-03-02"
    assert events[0].payload["missed"] == 0


@pytest.mark.parametrize(
    ("catch_up", "fired", "missed"), [("latest", 1, 4), ("all", 5, 0), ("skip", 0, 5)]
)
async def test_missed_ticks_follow_the_catch_up_policy(
    catch_up: str, fired: int, missed: int
) -> None:
    clock = FakeClock(datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    store = InMemoryRunStore()
    # The worker last fired at 07:00 and was down for five hourly ticks (08-12).
    await store.compare_and_set_state("schedule:auto:hourly", None, "2026-03-02T07:00:00+00:00")
    clock.advance(minutes=5)
    schedule = Schedule("0 * * * *", name="hourly", catch_up=catch_up)  # type: ignore[arg-type]
    ctx = context(clock, store)
    events: list[TriggerEvent] = []

    async def collect() -> None:
        async for event in schedule.events(ctx):
            events.append(event)

    task = asyncio.create_task(collect())
    for _ in range(20):
        await asyncio.sleep(0)
    ctx.stopping.set()
    await asyncio.wait_for(task, 5)

    caught_up = [e for e in events if e.payload["scheduled_for"] <= "2026-03-02T12:00:00+00:00"]
    assert len(caught_up) == fired
    if caught_up:
        assert caught_up[-1].payload["scheduled_for"] == "2026-03-02T12:00:00+00:00"
        assert caught_up[-1].payload["missed"] == missed
    assert await store.get_state("schedule:auto:hourly") >= "2026-03-02T12:00:00+00:00"  # type: ignore[operator]


async def test_two_workers_fire_a_tick_once() -> None:
    clock = FakeClock(datetime(2026, 3, 2, 8, 59, tzinfo=UTC))
    store = InMemoryRunStore()
    await store.compare_and_set_state("schedule:auto:s", None, clock().isoformat())
    clock.advance(minutes=2)
    first, second = Schedule("0 9 * * *", name="s"), Schedule("0 9 * * *", name="s")
    ctx_a, ctx_b = context(clock, store), context(clock, store)
    got: list[TriggerEvent] = []

    async def run(schedule: Schedule, ctx: TriggerContext) -> None:
        async for event in schedule.events(ctx):
            got.append(event)
            ctx.stopping.set()

    tasks = [asyncio.create_task(run(first, ctx_a)), asyncio.create_task(run(second, ctx_b))]
    for _ in range(10):
        await asyncio.sleep(0)
    ctx_a.stopping.set()
    ctx_b.stopping.set()
    await asyncio.gather(*tasks)
    assert [e.payload["scheduled_for"] for e in got] == ["2026-03-02T09:00:00+00:00"]


def test_interval_schedules_are_aligned_and_validated() -> None:
    schedule = Schedule(every="15m", name="poll")
    at = datetime(2026, 3, 2, 9, 7, tzinfo=UTC)
    assert schedule.next_after(at) == datetime(2026, 3, 2, 9, 15, tzinfo=UTC)
    ticks = schedule.ticks_between(at, at + timedelta(hours=1), limit=2)
    assert ticks == [
        datetime(2026, 3, 2, 9, 45, tzinfo=UTC),
        datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
    ]
    assert schedule.describe()["every"] == 900
    with pytest.raises(InvalidCronError):
        Schedule("* * * * *", every=60)
    with pytest.raises(InvalidCronError):
        Schedule(every="0.5s")
    with pytest.raises(ConfigurationError):
        Schedule(every=60, name="bad name!")
    with pytest.raises(ConfigurationError):  # `$` alone would accept a final line break
        Schedule(every=60, name="nightly\n")


# ---------------------------------------------------------------------- webhook


NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def test_webhook_verifies_timestamped_signatures() -> None:
    hook = Webhook("invoices", secret="s3cret")
    body = b'{"invoice_no": "RE-17", "amount": 10}'
    headers = hook.sign(body, timestamp=int(NOW.timestamp())) | {"X-Awa-Delivery": "d-1"}

    event = hook.verify(body, headers, now=NOW)
    assert event.payload == {"invoice_no": "RE-17", "amount": 10}
    assert event.dedup_key == "invoices:d-1"
    assert event.source == "invoices"

    lowered = {k.lower(): v for k, v in headers.items()}
    assert hook.verify(body, lowered, now=NOW + timedelta(minutes=4)).payload["amount"] == 10


@pytest.mark.parametrize(
    "tamper",
    [
        "body",
        "signature",
        "missing-signature",
        "missing-timestamp",
        "old",
        "future",
        "unreadable-timestamp",
        "huge-timestamp",
    ],
)
def test_webhook_rejects_bad_requests(tamper: str) -> None:
    hook = Webhook("invoices", secret="s3cret", replay_window="5m")
    body = b'{"a": 1}'
    headers = hook.sign(body, timestamp=int(NOW.timestamp()))
    now = NOW
    if tamper == "body":
        body = b'{"a": 2}'
    elif tamper == "signature":
        headers["X-Awa-Signature"] = "sha256=" + "0" * 64
    elif tamper == "missing-signature":
        del headers["X-Awa-Signature"]
    elif tamper == "missing-timestamp":
        del headers["X-Awa-Timestamp"]
    elif tamper == "unreadable-timestamp":  # a digit `int` cannot read
        headers["X-Awa-Timestamp"] = "\u00b2"
    elif tamper == "huge-timestamp":  # beyond the dates `datetime` holds
        headers["X-Awa-Timestamp"] = "9" * 30
    elif tamper == "old":
        now = NOW + timedelta(minutes=6)
    else:
        now = NOW - timedelta(minutes=6)
    with pytest.raises(WebhookVerificationError):
        hook.verify(body, headers, now=now)


def test_webhook_body_scheme_rotation_and_env_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOK_SECRET", "new")
    hook = Webhook("gh", secret_env="HOOK_SECRET", secret="old", scheme="body")
    body = b"plain text body"
    old_signature = hmac.new(b"old", body, hashlib.sha256).hexdigest()
    event = hook.verify(body, {"X-Awa-Signature": f"sha256=bad,sha256={old_signature}"}, now=NOW)
    assert event.payload == {"body": "plain text body"}
    assert event.dedup_key is not None and event.dedup_key.startswith("gh:")
    assert hook.sign(body)["X-Awa-Signature"] == (
        "sha256=" + hmac.new(b"new", body, hashlib.sha256).hexdigest()
    )

    monkeypatch.delenv("HOOK_SECRET")
    with pytest.raises(MissingConfigurationError):
        Webhook("x", secret_env="HOOK_SECRET").verify(body, {}, now=NOW)


def test_webhook_configuration_errors() -> None:
    with pytest.raises(ConfigurationError, match="secret"):
        Webhook("x")
    with pytest.raises(ConfigurationError):
        Webhook("x", secret="")
    unsigned = Webhook("x", allow_unsigned=True)
    assert unsigned.verify(b"", {}, now=NOW).payload == {}
    assert unsigned.verify(b"{}", {}, now=NOW).dedup_key is None
    with pytest.raises(WebhookVerificationError, match="larger"):
        Webhook("x", secret="s", max_bytes=2).verify(b"12345", {}, now=NOW)


# ---------------------------------------------------------------------- file drop


async def test_file_drop_waits_for_stable_files_and_moves_them(tmp_path: Path) -> None:
    clock = FakeClock()
    drop = FileDrop(tmp_path, "*.txt", stable_for="2s")
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "ignored.csv").write_text("x", encoding="utf-8")

    assert await drop.poll(clock()) == []  # first seen: not yet stable
    clock.advance(seconds=1)
    assert await drop.poll(clock()) == []
    clock.advance(seconds=2)
    [event] = await drop.poll(clock())
    assert event.payload["text"] == "hello"
    assert event.payload["name"] == "a.txt"
    assert event.dedup_key == f"files:a.txt:{hashlib.sha256(b'hello').hexdigest()[:32]}"
    assert await drop.poll(clock()) == []  # pending until acknowledged

    await drop.ack(event, None)
    assert (tmp_path / "processed" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "a.txt").exists()

    (tmp_path / "a.txt").write_text("again", encoding="utf-8")
    await drop.poll(clock())
    clock.advance(seconds=3)
    [second] = await drop.poll(clock())
    await drop.ack(second, ValueError("refused"))
    assert (tmp_path / "failed" / "a.txt").exists()


async def test_file_drop_restarts_the_stability_timer_when_a_file_grows(tmp_path: Path) -> None:
    clock = FakeClock()
    drop = FileDrop(tmp_path, read="bytes", stable_for="2s")
    target = tmp_path / "big.bin"
    target.write_bytes(b"12")
    await drop.poll(clock())
    clock.advance(seconds=3)
    target.write_bytes(b"1234")
    assert await drop.poll(clock()) == []
    clock.advance(seconds=3)
    [event] = await drop.poll(clock())
    assert event.payload["base64"] == "MTIzNA=="


async def test_file_drop_moves_oversized_files_aside(tmp_path: Path) -> None:
    drop = FileDrop(tmp_path, stable_for=0, max_bytes=3, processed_dir="done")
    (tmp_path / "large.txt").write_text("123456", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("1", encoding="utf-8")
    events = await drop.poll(datetime.now(UTC))
    assert [e.payload["name"] for e in events] == ["ok.txt"]
    assert (tmp_path / "failed" / "large.txt").exists()
    await drop.ack(events[0], None)
    (tmp_path / "ok.txt").write_text("2", encoding="utf-8")
    [again] = await drop.poll(datetime.now(UTC))
    await drop.ack(again, None)
    # A taken name gets the start of the content digest; the payload names the place.
    renamed = Path(again.payload["processed_path"])
    assert renamed.name == f"ok.{again.payload['sha256'][:12]}.txt" and renamed.is_file()
    assert sorted(p.name for p in (tmp_path / "done").iterdir()) == sorted(["ok.txt", renamed.name])


async def test_file_drop_keeps_same_size_files_with_different_content(tmp_path: Path) -> None:
    from awa.automation._trigger_files import _move_to

    done = tmp_path / "done"
    done.mkdir()
    (done / "a.txt").write_text("1111", encoding="utf-8")
    (tmp_path / "a.txt").write_text("2222", encoding="utf-8")
    moved = _move_to(tmp_path / "a.txt", done / "a.txt")
    assert moved != done / "a.txt" and moved.read_text(encoding="utf-8") == "2222"
    assert (done / "a.txt").read_text(encoding="utf-8") == "1111"  # never overwritten
    (tmp_path / "a.txt").write_text("1111", encoding="utf-8")
    assert _move_to(tmp_path / "a.txt", done / "a.txt") == done / "a.txt"  # a true duplicate


async def test_file_drop_ignores_symlinks(tmp_path: Path) -> None:
    watched = tmp_path / "inbox"
    watched.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("outside", encoding="utf-8")
    try:
        (watched / "link.txt").symlink_to(secret)
    except OSError:
        pytest.skip("symbolic links need a privilege here")
    (watched / "real.txt").write_text("real", encoding="utf-8")
    drop = FileDrop(watched, stable_for=0)
    assert [e.payload["name"] for e in await drop.poll(NOW)] == ["real.txt"]


async def test_file_drop_ignores_windows_device_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.automation._trigger_files as triggers

    (tmp_path / "NUL.txt").write_text("device", encoding="utf-8")
    (tmp_path / "real.txt").write_text("real", encoding="utf-8")
    monkeypatch.setattr(triggers, "_WINDOWS", True)
    drop = FileDrop(tmp_path, stable_for=0)
    assert [e.payload["name"] for e in await drop.poll(NOW)] == ["real.txt"]
    assert triggers.is_device_name("com1.log") and not triggers.is_device_name("common.txt")


async def test_polling_triggers_iterate_until_stopped(tmp_path: Path) -> None:
    clock = FakeClock()
    drop = FileDrop(tmp_path, stable_for=0, poll_interval="5s")
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    ctx = context(clock)
    [event] = await take(drop.events(ctx), 1)
    assert event.payload["name"] == "x.txt"


# ---------------------------------------------------------------------- mailbox


class FakeImap:
    """Enough of `imaplib.IMAP4` for the mailbox trigger."""

    def __init__(
        self,
        messages: dict[str, bytes],
        *,
        move: bool = False,
        uidplus: bool = False,
        refuse_login: bool = False,
        drop_fetch: str | None = None,
    ) -> None:
        self.messages = messages
        self.flags: dict[str, set[str]] = {uid: set() for uid in messages}
        self.capabilities = (
            "IMAP4REV1",
            *(["MOVE"] if move else []),
            *(["UIDPLUS"] if uidplus else []),
        )
        self.commands: list[tuple[str, ...]] = []
        self.logged_in: tuple[str, str] | None = None
        self.logouts = 0
        self.refuse_login = refuse_login
        self.drop_fetch = drop_fetch  # the connection breaks when this uid is fetched

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        if self.refuse_login:
            raise imaplib.IMAP4.error("LOGIN failed")
        self.logged_in = (user, password)
        return "OK", [b""]

    def select(self, folder: str) -> tuple[str, list[bytes]]:
        self.commands.append(("SELECT", folder))
        return "OK", [b"1"]

    def noop(self) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.commands.append((command, *[str(a) for a in args if a is not None]))
        if command == "SEARCH":
            unseen = [u for u in self.messages if "\\Seen" not in self.flags[u]]
            return "OK", [" ".join(unseen).encode()]
        if command == "FETCH":
            uid = str(args[0])
            if uid == self.drop_fetch:
                raise OSError("connection reset")
            return "OK", [(f"{uid} (BODY[] {{1}}".encode(), self.messages[uid]), b")"]
        if command == "STORE":
            self.flags[str(args[0])].add(str(args[2]).strip("()"))
            return "OK", [b""]
        return "OK", [b""]

    def expunge(self) -> tuple[str, list[bytes]]:
        self.commands.append(("EXPUNGE",))
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logouts += 1
        return "BYE", [b""]


def mail(subject: str, body: str, *, html: bool = False, message_id: str = "<m1@x>") -> bytes:
    message = EmailMessage()
    message["From"] = "supplier@example.com"
    message["To"] = "ap@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if html:
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)
        message.add_attachment(b"%PDF-1", maintype="application", subtype="pdf", filename="i.pdf")
    return message.as_bytes()


async def test_mailbox_reads_unseen_messages_and_marks_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    server = FakeImap(
        {
            "1": mail("Invoice RE-17", "Please pay 10 EUR."),
            "2": mail(
                "Hi", "<p>Hello <b>there</b></p><script>x</script>", html=True, message_id="<m2@x>"
            ),
        }
    )
    box = Mailbox("imap.example.com", connect=lambda: server)  # type: ignore[arg-type,return-value]
    events = await box.poll(NOW)

    assert [e.payload["subject"] for e in events] == ["Invoice RE-17", "Hi"]
    assert events[0].payload["text"].strip() == "Please pay 10 EUR."
    assert events[0].payload["attachments"] == [
        {"filename": "i.pdf", "content_type": "application/pdf", "size": 6}
    ]
    assert events[1].payload["text"] == "Hello there"
    assert events[0].dedup_key == "mailbox:<m1@x>"
    assert server.logged_in == ("ap", "pw")
    assert await box.poll(NOW) == []  # pending ones are not offered twice

    await box.ack(events[0], None)
    await box.ack(events[1], RuntimeError("refused"))  # on_failure="keep"
    assert server.flags["1"] == {"\\Seen"}
    assert server.flags["2"] == set()
    assert [e.payload["subject"] for e in await box.poll(NOW)] == ["Hi"]
    await box.close()


async def test_mailbox_moves_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    plain = FakeImap({"7": mail("a", "b")})
    box = Mailbox("h", on_success="move", move_to="Done", connect=lambda: plain)  # type: ignore[arg-type,return-value]
    [event] = await box.poll(NOW)
    await box.ack(event, None)
    assert ("COPY", "7", '"Done"') in plain.commands
    assert ("EXPUNGE",) in plain.commands

    modern = FakeImap({"8": mail("a", "b")}, move=True)
    box = Mailbox("h", on_success="move", move_to="Done", connect=lambda: modern)  # type: ignore[arg-type,return-value]
    [event] = await box.poll(NOW)
    await box.ack(event, None)
    assert ("MOVE", "8", '"Done"') in modern.commands


async def test_mailbox_verifies_the_server_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    contexts: list[Any] = []

    def fake_ssl(host: str, port: int, *, ssl_context: Any, timeout: float) -> FakeImap:
        contexts.append(ssl_context)
        return FakeImap({})

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    assert await Mailbox("imap.example.com").poll(NOW) == []
    [context] = contexts
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_mailbox_needs_credentials_and_valid_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWA_IMAP_USER", raising=False)
    box = Mailbox("h", connect=lambda: FakeImap({}))  # type: ignore[arg-type,return-value]
    with pytest.raises(MissingConfigurationError):
        await box.poll(NOW)
    with pytest.raises(ConfigurationError):
        Mailbox("h", on_success="move")
    with pytest.raises(ConfigurationError):
        Mailbox("h", on_failure="archive")  # type: ignore[arg-type]


async def test_mailbox_checks_the_environment_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWA_IMAP_USER", raising=False)
    monkeypatch.delenv("AWA_IMAP_PASSWORD", raising=False)
    opened: list[FakeImap] = []

    def connect() -> FakeImap:
        opened.append(FakeImap({}))
        return opened[-1]

    box = Mailbox("h", connect=connect)  # type: ignore[arg-type]
    with pytest.raises(MissingConfigurationError):
        await box.poll(NOW)
    assert opened == []


async def test_mailbox_closes_a_connection_that_fails_to_log_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    server = FakeImap({}, refuse_login=True)
    box = Mailbox("h", connect=lambda: server)  # type: ignore[arg-type,return-value]
    with pytest.raises(imaplib.IMAP4.error):
        await box.poll(NOW)
    assert server.logouts == 1


async def test_a_bad_message_does_not_hide_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    blocked = tmp_path / "attachments"
    blocked.write_text("a file, so no folder can be made in it", encoding="utf-8")
    server = FakeImap(
        {
            "1": mail("with attachment", "x", message_id="<a@x>"),
            "2": mail("plain", "<p>y</p>", html=True, message_id="<b@x>"),
        }
    )
    box = Mailbox("h", attachments_dir=blocked, connect=lambda: server)  # type: ignore[arg-type,return-value]
    events = await box.poll(NOW)
    assert [e.payload["subject"] for e in events] == ["plain"]
    assert await box.poll(NOW) == []  # the bad one is not retried every poll
    await box.ack(events[0], None)
    assert server.flags["2"] == {"\\Seen"} and server.flags["1"] == set()


async def test_a_broken_connection_returns_the_messages_fetched_so_far(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    messages = {
        "1": mail("one", "x", html=True, message_id="<a@x>"),
        "2": mail("two", "x", html=True, message_id="<b@x>"),
    }
    servers = [FakeImap(messages, drop_fetch="2"), FakeImap(messages)]
    box = Mailbox("h", connect=lambda: servers.pop(0))  # type: ignore[arg-type,return-value]
    events = await box.poll(NOW)
    assert [e.payload["subject"] for e in events] == ["one"]
    assert box._conn is None  # abandoned; the next poll reconnects
    await box.ack(events[0], None)
    assert [e.payload["subject"] for e in await box.poll(NOW)] == ["two"]


async def test_a_large_attachment_is_listed_but_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    server = FakeImap({"1": mail("big", "x")})  # carries a 6-byte PDF
    box = Mailbox(
        "h",
        attachments_dir=tmp_path / "files",
        max_attachment_bytes=5,
        connect=lambda: server,  # type: ignore[arg-type,return-value]
    )
    [event] = await box.poll(NOW)
    [attachment] = event.payload["attachments"]
    assert attachment["size"] == 6 and "max_attachment_bytes" in attachment["skipped"]
    assert "path" not in attachment and not (tmp_path / "files").exists()


async def test_delete_expunges_only_the_handled_message_with_uidplus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    modern = FakeImap({"4": mail("a", "b")}, uidplus=True)
    box = Mailbox("h", on_success="delete", connect=lambda: modern)  # type: ignore[arg-type,return-value]
    [event] = await box.poll(NOW)
    await box.ack(event, None)
    assert ("EXPUNGE", "4") in modern.commands and ("EXPUNGE",) not in modern.commands

    plain = FakeImap({"5": mail("a", "b")})
    box = Mailbox("h", on_success="delete", connect=lambda: plain)  # type: ignore[arg-type,return-value]
    [event] = await box.poll(NOW)
    await box.ack(event, None)
    assert ("EXPUNGE",) in plain.commands


# ---------------------------------------------------------------------- queues


async def test_in_memory_queue_visibility_nack_and_dead_letters() -> None:
    clock = FakeClock()
    queue = InMemoryEventQueue(max_attempts=2, clock=clock)
    first = await queue.send({"n": 1}, dedup_key="one")
    await queue.send({"n": 2}, delay="1m")

    [message] = await queue.receive(visibility="30s")
    assert message.id == first and message.attempts == 1
    assert await queue.receive() == []  # invisible, and the second is delayed
    clock.advance(seconds=31)
    [again] = await queue.receive()
    assert again.id == first and again.attempts == 2
    await queue.nack(first)
    assert [m.payload for m in queue.dead_letters] == [{"n": 1}]
    clock.advance(minutes=1)
    [second] = await queue.receive()
    await queue.ack(second.id)
    assert len(queue) == 0


async def test_a_message_whose_receivers_keep_crashing_is_dead_lettered() -> None:
    clock = FakeClock()
    queue = InMemoryEventQueue(max_attempts=2, clock=clock)
    await queue.send({"n": 1})

    for _ in range(2):  # received, never acknowledged
        assert len(await queue.receive(visibility="30s")) == 1
        clock.advance(seconds=31)

    assert await queue.receive() == []
    assert [m.payload for m in queue.dead_letters] == [{"n": 1}] and len(queue) == 0


async def test_queue_trigger_acknowledges_after_submission() -> None:
    clock = FakeClock()
    queue = InMemoryEventQueue(clock=clock)
    await queue.send({"id": "T-1"}, dedup_key="T-1")
    await queue.send({"id": "T-2"})
    trigger = QueueTrigger(queue, name="tickets", retry_delay="10s")
    events = await take(trigger.events(context(clock)), 2)
    assert events[0].dedup_key == "tickets:T-1"
    assert events[1].dedup_key == f"tickets:{events[1].id}"

    await trigger.ack(events[0], None)
    await trigger.ack(events[1], ValueError("no"))
    assert len(queue) == 1
    clock.advance(seconds=11)
    [retried] = await trigger.poll()
    assert retried.payload == {"id": "T-2"}
    assert retried.metadata["attempts"] == 2


@pytest.mark.parametrize("name", ["CON", "nul.txt", "AUX.pdf", "com1.log", "LPT9. "])
def test_mail_attachment_names_never_are_windows_device_names(name: str) -> None:
    from awa.automation._trigger_mail import _safe_filename

    saved = _safe_filename(name, 2)

    assert saved != name and saved.split(".")[0].strip().upper() not in {"CON", "NUL", "AUX"}
    assert saved.endswith(name.strip(" .")) and _safe_filename("invoice.pdf", 1) == "invoice.pdf"


async def test_file_drop_waits_for_a_file_another_program_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.automation import _trigger_files

    monkeypatch.setattr(_trigger_files, "_LOCK_RETRIES", (0.0, 0.0, 0.0))
    real_move = _trigger_files._move_to
    tries: list[int] = []

    def locked_twice(path: Path, target: Path) -> Path:
        tries.append(1)
        if len(tries) <= 2:
            raise PermissionError(32, "used by another process")
        return real_move(path, target)

    monkeypatch.setattr(_trigger_files, "_move_to", locked_twice)
    drop = FileDrop(tmp_path, stable_for=0)
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    monkeypatch.setattr(_trigger_files, "_move_to", real_move)  # claiming the file works
    [event] = await drop.poll(datetime.now(UTC))
    assert Path(event.payload["path"]).parent.name == "in-progress"
    monkeypatch.setattr(_trigger_files, "_move_to", locked_twice)
    await drop.ack(event, None)
    assert len(tries) == 3 and (tmp_path / "processed" / "a.txt").is_file()


def test_bad_trigger_configuration_is_refused() -> None:
    with pytest.raises(InvalidCronError, match="catch_up must be"):
        Schedule("* * * * *", catch_up="never")  # type: ignore[arg-type]
    with pytest.raises(InvalidCronError, match="max_catch_up"):
        Schedule("* * * * *", max_catch_up=0)
    with pytest.raises(InvalidCronError, match="exactly one"):
        Schedule()
    with pytest.raises(ConfigurationError, match="Trigger name"):
        Webhook("bad name!", allow_unsigned=True)
    with pytest.raises(ConfigurationError, match="read must be"):
        FileDrop(Path("."), read="everything", poll_interval="1s")  # type: ignore[arg-type]
    assert repr(Schedule("* * * * *", name="tick")) == "Schedule('tick')"


def test_timestamped_signing_needs_a_timestamp_and_form_bodies_survive_bad_json() -> None:
    hook = Webhook("hook", secret="s")
    with pytest.raises(ValueError, match="needs a timestamp"):
        hook.sign(b"{}")
    event = Webhook("form", allow_unsigned=True).verify(
        b"payload=%7Bnot+json", {"content-type": "application/x-www-form-urlencoded"}, now=NOW
    )
    assert event.payload == {"payload": "{not json"}


class Flaky(PollingTrigger):
    """Fails its first poll, then reports one event."""

    def __init__(self) -> None:
        super().__init__("flaky", poll_interval="5s")
        self.polls = 0

    async def poll(self, now: Any) -> list[TriggerEvent]:
        self.polls += 1
        if self.polls == 1:
            raise RuntimeError("source unavailable")
        return [TriggerEvent(source=self.name, payload={"poll": self.polls})]


async def test_a_failing_poll_is_logged_and_the_next_one_still_runs() -> None:
    clock = FakeClock()
    flaky = Flaky()

    [event] = await take(flaky.events(context(clock)), 1)

    assert flaky.polls == 2 and event.payload == {"poll": 2}
    assert clock.now() - NOW == timedelta(seconds=5)


class BrokenImap(FakeImap):
    """A server whose SEARCH fails the way `mode` says."""

    def __init__(self, mode: str) -> None:
        super().__init__({})
        self.mode = mode

    def select(self, folder: str) -> tuple[str, list[bytes]]:
        return ("NO", [b""]) if self.mode == "select" else super().select(folder)

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH" and self.mode == "search-status":
            return "NO", [b""]
        if command == "SEARCH" and self.mode == "search-drop":
            raise OSError("connection reset")
        return super().uid(command, *args)


@pytest.mark.parametrize(
    ("mode", "error", "message", "kept"),
    [
        ("select", ConfigurationError, "cannot open folder", False),
        ("search-status", ConfigurationError, "search .* failed", True),
        ("search-drop", OSError, "connection reset", False),
    ],
)
async def test_mailbox_server_failures_are_raised_and_a_broken_connection_dropped(
    monkeypatch: pytest.MonkeyPatch, mode: str, error: type[Exception], message: str, kept: bool
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    server = BrokenImap(mode)
    box = Mailbox("h", connect=lambda: server)  # type: ignore[arg-type,return-value]

    with pytest.raises(error, match=message):
        await box.poll(NOW)

    assert (box._conn is server) is kept and server.logouts == (0 if kept else 1)


def docx_bytes(*paragraphs: str) -> bytes:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes() -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Invoices"
    sheet.append(["invoice", "amount"])
    sheet.append(["RE-17", 1250.5])
    workbook.create_sheet("Notes").append(["checked"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def test_file_drop_reads_documents_by_default(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_bytes("id;name\n1;Müller\n".encode("cp1252"))
    (tmp_path / "letter.docx").write_bytes(docx_bytes("Dear supplier,", "Invoice RE-17 is due."))
    (tmp_path / "book.xlsx").write_bytes(xlsx_bytes())
    (tmp_path / "scan.bin").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    drop = FileDrop(tmp_path, stable_for=0, max_text_chars=10_000)
    found = {event.payload["name"]: event for event in await drop.poll(NOW)}
    events = {name: event.payload for name, event in found.items()}

    assert events["orders.csv"]["text"] == "id;name\n1;Müller\n"  # cp1252 fallback
    assert "Invoice RE-17 is due." in events["letter.docx"]["text"]
    workbook = events["book.xlsx"]["text"]
    assert "# Invoices" in workbook and "RE-17" in workbook and "# Notes" in workbook
    assert "text" not in events["scan.bin"]  # binary without a reader: path only
    assert "text" not in events["broken.pdf"] and events["broken.pdf"]["text_error"]
    processed = Path(events["orders.csv"]["processed_path"])
    assert processed == tmp_path / "processed" / "orders.csv"

    await drop.ack(found["orders.csv"], None)
    assert processed.read_bytes() == "id;name\n1;Müller\n".encode("cp1252")


def test_document_text_is_capped(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    assert document_text(path, b"a" * 50, max_chars=10) == {
        "text": "a" * 10,
        "text_truncated": True,
    }


def test_a_pdf_is_read_page_by_page(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    # A page without text extracts as empty text, not as an error.
    assert document_text(tmp_path / "blank.pdf", buffer.getvalue()) == {"text": ""}


async def test_mailbox_saves_attachments_and_lists_their_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWA_IMAP_USER", "ap")
    monkeypatch.setenv("AWA_IMAP_PASSWORD", "pw")
    message = EmailMessage()
    message["From"], message["Subject"], message["Message-ID"] = "s@x", "Invoice", "<m9@x>"
    message.set_content("See attached.")
    message.add_attachment(
        docx_bytes("Invoice RE-18"),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="RE-18.docx",
    )
    message.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="../../evil.csv")
    message.add_attachment(b"1,2\n", maintype="text", subtype="csv", filename="evil.csv")
    server = FakeImap({"5": message.as_bytes()})
    box = Mailbox(
        "imap.example.com",
        attachments_dir=tmp_path / "attachments",
        connect=lambda: server,  # type: ignore[arg-type,return-value]
    )
    [event] = await box.poll(NOW)
    docx, evil, second = event.payload["attachments"]

    assert Path(docx["path"]).read_bytes()[:2] == b"PK" and "Invoice RE-18" in docx["text"]
    # The sender's name cannot leave the message's folder; a repeated name gets a number.
    assert Path(evil["path"]).parent == Path(docx["path"]).parent
    assert Path(evil["path"]).name == "evil.csv" and evil["text"] == "a,b\n1,2\n"
    assert Path(second["path"]).name == "3-evil.csv"
    assert Path(docx["path"]).parent.parent == tmp_path / "attachments"
    plain = Mailbox("imap.example.com", connect=lambda: FakeImap({"5": message.as_bytes()}))  # type: ignore[arg-type,return-value]
    [listed] = await plain.poll(NOW)
    assert "path" not in listed.payload["attachments"][0]


def test_documents_are_read_within_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awa.toolkits import documents

    read: list[int] = []

    class Page:
        def __init__(self, number: int) -> None:
            self.number = number

        def extract_text(self, **_: object) -> str:
            read.append(self.number)
            return "x" * 100

    class Reader:
        is_encrypted = False
        pages = [Page(n) for n in range(1, 1_000)]

        def __init__(self, _: object) -> None:
            pass

    monkeypatch.setattr(
        documents, "require_module", lambda *a, **k: type("M", (), {"PdfReader": Reader})
    )
    found = document_text(tmp_path / "big.pdf", b"%PDF", max_chars=250)
    assert found["text_truncated"] is True and len(read) == 3  # stopped once 250 chars were in
    read.clear()
    document_text(tmp_path / "big.pdf", b"%PDF", max_chars=10_000_000)
    assert len(read) == 200  # the page cap

    import zipfile

    with zipfile.ZipFile(tmp_path / "bomb.docx", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\0" * 5_000_000)
    pytest.importorskip("docx")
    assert "unreasonable size" in document_text(tmp_path / "bomb.docx", b"")["text_error"]
