"""Local durable automation, and where a dropped file goes when its run fails, waits or succeeds."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.automation import ScriptedSystem
from awa import Agent
from awa.automation import (
    Automation,
    FileDrop,
    InMemoryEventQueue,
    Mailbox,
    ProcessRun,
    QueueTrigger,
    RetryPolicy,
    SqliteRunStore,
    Worker,
    wait_for_event,
)
from awa.automation._trigger_base import PollingTrigger, TriggerContext
from awa.automation.automation import DEFAULT_RUN_TIMEOUT, retry_run
from awa.automation.models import TriggerEvent
from awa.cli._automation import once_exit_code
from awa.contracts import RunResult
from awa.core.settings import Settings
from awa.errors import CheckpointLostError, ConfigurationError
from awa.persistence import SqliteCheckpointer, open_checkpointer
from awa.testing import FakeChatModel, FakeClock, tool_call
from awa.tools import create_tool_approval_wrapper

PAID: list[str] = []


@tool
def pay(order: str) -> str:
    """Pay an order."""

    PAID.append(order)
    return f"paid {order}"


def payer(saver: Any, script: list[Any]) -> Agent[Any, Any]:
    return Agent(
        "payer",
        model=FakeChatModel(script),
        tools=[create_tool_approval_wrapper(pay)],
        checkpointer=saver,
    )


# ---------------------------------------------------------------------- checkpointer


async def test_an_approval_survives_a_restart_with_the_sqlite_checkpointer(tmp_path: Path) -> None:
    PAID.clear()
    store_path, db = tmp_path / "awa.db", tmp_path / "checkpoints.db"
    model = FakeChatModel([tool_call("pay", {"order": "7"}), "Order 7 paid."])

    def process() -> Automation:  # a fresh process: new system, saver and store objects
        agent = Agent(
            "payer",
            model=model,
            tools=[create_tool_approval_wrapper(pay)],
            checkpointer=SqliteCheckpointer(db),
        )
        return Automation(agent, store=SqliteRunStore(store_path), version="v1")

    before = process()
    await before.submit({"order": 7})
    [paused] = await before.run_pending()
    assert paused.status == "waiting" and PAID == []

    after = process()
    await after.decide(paused.id, {"action": "approve"}, actor="lead")
    [done] = await after.run_pending()
    assert done.status == "succeeded" and done.output == "Order 7 paid." and PAID == ["7"]


async def test_sqlite_checkpointer_lists_filters_and_deletes(tmp_path: Path) -> None:
    saver = SqliteCheckpointer(tmp_path / "c.db")
    agent = Agent("echo", model=FakeChatModel(["one", "two"]), checkpointer=saver)
    await agent.run("first", thread_id="t1")
    await agent.run("second", thread_id="t1")
    config = {"configurable": {"thread_id": "t1"}}
    history = [item async for item in saver.alist(config)]
    assert len(history) >= 4 and history == sorted(
        history, key=lambda t: t.config["configurable"]["checkpoint_id"], reverse=True
    )
    latest = await saver.aget_tuple(config)
    assert latest is not None and latest.config == history[0].config
    assert len([item async for item in saver.alist(config, limit=2)]) == 2
    before = [item async for item in saver.alist(config, before=history[0].config)]
    assert before == history[1:]
    sources = {t.metadata.get("source") for t in history}
    assert [t.metadata["source"] for t in saver.list(config, filter={"source": "input"})] == [
        "input"
    ] * sum(t.metadata.get("source") == "input" for t in history)
    assert "loop" in sources
    reopened = SqliteCheckpointer(tmp_path / "c.db")
    assert (await reopened.aget_tuple(config)).checkpoint["id"] == latest.checkpoint["id"]
    await reopened.adelete_thread("t1")
    assert await saver.aget_tuple(config) is None
    reopened.close()
    saver.close()


async def test_settings_select_the_sqlite_backend(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, checkpointer_backend="sqlite", checkpointer_path=str(tmp_path / "x.db")
    )
    async with open_checkpointer(settings) as opened:
        assert isinstance(opened, SqliteCheckpointer) and opened.path == tmp_path / "x.db"


# ---------------------------------------------------------------------- lost checkpoint


async def test_a_lost_checkpoint_fails_clearly_and_a_retry_restarts_from_the_input() -> None:
    PAID.clear()
    script = [tool_call("pay", {"order": "8"}), tool_call("pay", {"order": "8"}), "Paid 8."]
    model = FakeChatModel(script)
    first = Automation(
        Agent(
            "payer",
            model=model,
            tools=[create_tool_approval_wrapper(pay)],
            checkpointer=InMemorySaver(),
        ),
        version="v1",
    )
    await first.submit({"order": 8})
    [paused] = await first.run_pending()
    # The process that held the checkpoints in memory is gone: same store, empty saver.
    after = Automation(
        Agent(
            "payer",
            model=model,
            tools=[create_tool_approval_wrapper(pay)],
            checkpointer=InMemorySaver(),
        ),
        store=first.store,
        version="v1",
    )
    await after.decide(paused.id, {"action": "approve"})
    [lost] = await after.run_pending()
    assert lost.status == "failed" and lost.error_code == CheckpointLostError.code
    assert "checkpoint lost" in (lost.error or "") and f"awa runs retry {lost.id}" in lost.error
    assert await after.run_pending() == []  # no endless loop

    retried = await after.retry(lost.id)
    assert not retried.resuming and retried.resume_value is None and retried.waits == []
    assert "from the input" in retried.history[-1]["note"]
    [again] = await after.run_pending()
    assert again.status == "waiting"  # started over, asks for the approval again
    await after.decide(again.id, {"action": "approve"})
    [done] = await after.run_pending()
    assert done.status == "succeeded" and PAID == ["8"]


async def test_retry_run_restarts_by_error_code_or_on_request() -> None:
    auto = Automation(ScriptedSystem())
    now = FakeClock()()
    lost = ProcessRun(
        automation="desk",
        status="failed",
        error_code=CheckpointLostError.code,
        resume_value={"action": "approve"},
    )
    failed = ProcessRun(automation="desk", status="failed", resume_value={"action": "approve"})
    for run in (lost, failed):
        await auto.store.create(run)
    assert not (await retry_run(auto.store, lost.id, now=now)).resuming
    assert (await retry_run(auto.store, failed.id, now=now)).resuming
    await auto.store.modify(failed.id, lambda r: r.model_copy(update={"status": "failed"}), now=now)
    assert not (await retry_run(auto.store, failed.id, now=now, restart=True)).resuming


# ---------------------------------------------------------------------- timeouts


class Hanging(ScriptedSystem):
    """A system without a timeout of its own that never finishes."""

    async def run(self, input: Any, **options: Any) -> RunResult[Any]:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


async def test_attempts_have_a_default_timeout_and_are_retried() -> None:
    assert Automation(ScriptedSystem()).run_timeout == DEFAULT_RUN_TIMEOUT == 1800
    assert Automation(ScriptedSystem(), run_timeout=None).run_timeout is None
    with pytest.raises(ConfigurationError, match="run_timeout"):
        Automation(ScriptedSystem(), run_timeout=0)
    clock = FakeClock()
    auto = Automation(
        Hanging(), run_timeout=0.05, clock=clock, retry=RetryPolicy(max_attempts=2, initial=1)
    )
    await auto.submit({})
    [first] = await auto.run_pending()
    assert first.status == "queued" and "TimeoutError" in (first.error or "")
    clock.advance(seconds=2)
    [second] = await auto.run_pending()
    assert second.status == "dead_lettered" and second.attempts == 2


# ---------------------------------------------------------------------- approvals


async def test_several_approvals_are_decided_one_at_a_time() -> None:
    PAID.clear()
    calls = [tool_call("pay", {"order": "1"}), tool_call("pay", {"order": "2"})]
    auto = Automation(payer(InMemorySaver(), [calls, "Both handled."]))
    await auto.submit({})
    [paused] = await auto.run_pending()
    assert [w.kind for w in paused.waits] == ["approval", "approval"]
    one = await auto.decide(paused.id, {"action": "approve"})
    assert one.status == "waiting" and len(one.wait_keys) == 1
    both = await auto.decide(paused.id, {"action": "reject", "note": "not 2"})
    assert both.status == "queued" and set(both.resume_value) == {
        w.interrupt_id for w in paused.waits
    }
    [done] = await auto.run_pending()
    assert done.status == "succeeded" and PAID == ["1"]


async def test_runs_of_a_network_with_shared_state_wait_for_each_other() -> None:
    from types import SimpleNamespace

    from awa.automation._support import exclusive
    from awa.team import AgentNetwork

    network = AgentNetwork.__new__(AgentNetwork)
    network._scope = SimpleNamespace(shared=True)  # type: ignore[attr-defined]
    order: list[str] = []

    async def attempt(name: str) -> None:
        async with exclusive(network):
            order.append(f"{name} start")
            await asyncio.sleep(0.02)
            order.append(f"{name} end")

    await asyncio.gather(attempt("a"), attempt("b"))
    assert order == ["a start", "a end", "b start", "b end"]
    network._scope = SimpleNamespace(shared=False)  # type: ignore[attr-defined]
    order.clear()
    await asyncio.gather(attempt("a"), attempt("b"))
    assert order[:2] == ["a start", "b start"]  # per-run state: no waiting


def drop(folder: Path, name: str = "invoice.txt", text: str = "RE-17") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


async def test_a_failed_run_leaves_its_file_in_failed_with_a_reason(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    drop(inbox)
    system = ScriptedSystem(errors=[RuntimeError("the model is down")])
    auto = Automation(system, triggers=[FileDrop(inbox, stable_for=0)])

    runs = await Worker([auto]).run_once()

    assert {run.status for run in runs} <= {"failed", "dead_lettered"}
    assert once_exit_code(runs) == 1
    assert not (inbox / "processed" / "invoice.txt").exists()
    assert (inbox / "failed" / "invoice.txt").read_text(encoding="utf-8") == "RE-17"
    reason = (inbox / "failed" / "invoice.txt.error.txt").read_text(encoding="utf-8")
    assert f"run: {runs[0].id}" in reason
    assert "the model is down" in reason
    assert f"awa runs show {runs[0].id}" in reason


async def test_a_succeeded_run_moves_its_file_to_processed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    drop(inbox)
    auto = Automation(ScriptedSystem(), triggers=[FileDrop(inbox, stable_for=0)])

    runs = await Worker([auto]).run_once()

    assert [run.status for run in runs] == ["succeeded"]
    assert once_exit_code(runs) == 0
    assert (inbox / "processed" / "invoice.txt").exists()
    assert not (inbox / "failed").exists()


@tool
def ask_supplier(invoice: str) -> str:
    """Ask the supplier and wait for the reply."""

    reply = wait_for_event(f"reply:{invoice}")
    return f"supplier said {reply['text']}"


def waiting_automation(
    inbox: Path, store: Any, saver: InMemorySaver, clock: FakeClock, script: list[Any]
) -> Automation:
    agent = Agent("clerk", model=FakeChatModel(script), tools=[ask_supplier], checkpointer=saver)
    return Automation(
        agent,
        triggers=[FileDrop(inbox, stable_for=0)],
        input="Check {payload.name}",
        store=store,
        clock=clock,
    )


async def test_a_waiting_run_parks_its_file_and_a_restart_does_not_submit_it_again(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    drop(inbox)
    store = SqliteRunStore(tmp_path / "awa.db")
    saver, clock = InMemorySaver(), FakeClock()

    first = waiting_automation(
        inbox, store, saver, clock, [tool_call("ask_supplier", {"invoice": "I-1"})]
    )
    [paused] = await Worker([first], clock=clock).run_once()
    assert paused.status == "waiting" and once_exit_code([paused]) == 2
    assert (inbox / "in-progress" / "invoice.txt").exists()
    assert not (inbox / "invoice.txt").exists()

    # A new worker process: the parked file is offered again and finds its run.
    second = waiting_automation(inbox, store, saver, clock, ["Booked."])
    restarted = Worker([second], clock=clock)
    again = await restarted.run_once()
    assert again == [] and len(await store.list()) == 1
    assert (inbox / "in-progress" / "invoice.txt").exists()

    await second.deliver("reply:I-1", {"text": "ok"})
    [done] = await restarted.run_once()
    assert done.status == "succeeded"
    assert (inbox / "processed" / "invoice.txt").exists()
    assert not (inbox / "in-progress" / "invoice.txt").exists()


async def test_a_trigger_can_acknowledge_when_queued(tmp_path: Path) -> None:
    class QueuedAck(FileDrop):
        ack_on_end = False

    inbox = tmp_path / "inbox"
    drop(inbox)
    system = ScriptedSystem(errors=[RuntimeError("boom")])
    auto = Automation(system, triggers=[QueuedAck(inbox, stable_for=0)])

    await Worker([auto]).run_once()

    assert (inbox / "processed" / "invoice.txt").exists()  # acknowledged when queued


async def test_a_refused_event_goes_to_failed_with_a_reason(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    drop(inbox)
    auto = Automation(
        ScriptedSystem(), triggers=[FileDrop(inbox, stable_for=0, input="{payload.missing}")]
    )

    assert await Worker([auto]).run_once() == []

    reason = (inbox / "failed" / "invoice.txt.error.txt").read_text(encoding="utf-8")
    assert "status: refused" in reason


class Flaky(PollingTrigger):
    def __init__(self, failures: int, stop: asyncio.Event) -> None:
        super().__init__("flaky", poll_interval=60)
        self.left = failures
        self.stop = stop

    async def poll(self, now: datetime) -> list[TriggerEvent]:
        if self.left:
            self.left -= 1
            raise OSError("mail server unreachable")
        self.stop.set()
        return []


async def test_polling_backs_off_and_logs_one_line_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awa.automation._trigger_base as base

    waits: list[float] = []
    logged: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - the logging API
            return False

        def warning(self, message: str, *args: Any, **options: Any) -> None:
            logged.append((message % args, options))

        error = warning

    monkeypatch.setattr(base, "_logger", Recorder())

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    stop = asyncio.Event()
    trigger = Flaky(5, stop)
    context = TriggerContext(automation="mail", state=None, sleep=sleep, stopping=stop)  # type: ignore[arg-type]  # no state needed
    _ = [event async for event in trigger.events(context)]

    assert len(waits) == 5 and waits[0] == 60
    expected = [min(60 * 2**n, 900) for n in range(5)]  # doubled, at most fifteen minutes
    assert all(0.8 * e <= wait <= 1.2 * e for e, wait in zip(expected, waits, strict=True))
    failed = [(text, options) for text, options in logged if "poll failed" in text]
    assert len(failed) == 1 and not failed[0][1].get("exc_info")
    assert any("recovered after 5 failed poll(s)" in text for text, _ in logged)


def test_the_backoff_is_capped_at_fifteen_minutes() -> None:
    trigger = Flaky(0, asyncio.Event())
    assert trigger.backoff(1, jitter=0) == 60
    assert trigger.backoff(3, jitter=0) == 240
    assert trigger.backoff(30, jitter=0) == 900
    assert trigger.backoff(30, jitter=1) == pytest.approx(1080)


async def test_a_mailbox_without_credentials_stops_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("AWA_IMAP_USER", "AWA_IMAP_PASSWORD", "IMAP_USERNAME", "IMAP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    mailbox = Mailbox("imap.example.com", poll_interval=0.01)
    queue = InMemoryEventQueue()
    await queue.send({"text": "ticket"})
    system = ScriptedSystem()
    auto = Automation(
        system, triggers=[mailbox, QueueTrigger(queue, name="tickets", input="{payload.text}")]
    )
    worker = Worker([auto], poll_interval=0.01)
    running = asyncio.create_task(worker.run())
    for _ in range(500):
        if system.calls and mailbox.stopped:
            break
        await asyncio.sleep(0.01)
    worker.stop()
    await running

    assert [call["input"] for call in system.calls] == ["ticket"]
    assert mailbox.stopped and "AWA_IMAP_USER" in mailbox.stopped
    assert sum("stopped" in r.getMessage() for r in caplog.records) == 1


def test_the_mailbox_uses_the_imap_connector_login_when_its_own_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("AWA_IMAP_USER", "AWA_IMAP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IMAP_USERNAME", "me")
    monkeypatch.setenv("IMAP_PASSWORD", "pw")
    assert Mailbox("imap.example.com").required_env() == ("IMAP_USERNAME", "IMAP_PASSWORD")
    monkeypatch.setenv("AWA_IMAP_USER", "own")
    assert Mailbox("imap.example.com").required_env()[0] == "AWA_IMAP_USER"
