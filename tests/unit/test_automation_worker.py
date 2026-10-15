"""`Worker`: triggers feeding runs, concurrency, housekeeping and graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from _helpers.automation import ScriptedSystem
from awa import Agent
from awa.automation import (
    Automation,
    BaseTrigger,
    FileDrop,
    InMemoryEventQueue,
    InMemoryRunStore,
    QueueTrigger,
    RetryPolicy,
    Schedule,
    TriggerContext,
    TriggerEvent,
    Webhook,
    Worker,
)
from awa.automation.automation import PROCESS_RUN_METADATA_KEY
from awa.contracts import RunResult
from awa.errors import ConfigurationError
from awa.testing import FakeChatModel, FakeClock, tool_call


async def wait_for(condition: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.01)


async def test_worker_runs_queue_and_file_triggers(tmp_path: Path) -> None:
    queue = InMemoryEventQueue()
    await queue.send({"text": "ticket 1"}, dedup_key="t1")
    await queue.send({"text": "ticket 1 again"}, dedup_key="t1")
    (tmp_path / "a.txt").write_text("file body", encoding="utf-8")
    system = ScriptedSystem()
    auto = Automation(
        system,
        triggers=[
            QueueTrigger(queue, name="tickets", poll_interval=0.01, input="Q: {payload.text}"),
            FileDrop(tmp_path, stable_for=0, poll_interval=0.01, input="F: {payload.text}"),
        ],
        concurrency=2,
    )
    worker = Worker([auto], poll_interval=0.01, tick_interval=0.05)
    task = asyncio.create_task(worker.run())

    async def finished() -> bool:
        runs = await auto.store.list(status="succeeded")
        return len(runs) == 2

    await wait_for(finished)
    worker.stop()
    await asyncio.wait_for(task, 5)
    inputs = sorted(call["input"] for call in system.calls)
    assert inputs == ["F: file body", "Q: ticket 1"]
    assert len(queue) == 0  # both messages acknowledged; the duplicate started no run
    assert (tmp_path / "processed" / "a.txt").exists()


class SlowAckTrigger(BaseTrigger):
    """Yields one event; its acknowledgement takes a while (like FileDrop's file move)."""

    def __init__(self) -> None:
        super().__init__("slow-ack")
        self.ack_started = asyncio.Event()
        self.acked: list[BaseException | None] = []

    async def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        yield TriggerEvent(source=self.name, payload={"text": "one"})
        await context.stopping.wait()

    async def ack(self, event: TriggerEvent, error: BaseException | None) -> None:
        self.ack_started.set()
        await asyncio.sleep(0.1)
        self.acked.append(error)


async def test_shutdown_lets_the_event_in_hand_finish_its_acknowledgement() -> None:
    trigger = SlowAckTrigger()
    auto = Automation(ScriptedSystem(), triggers=[trigger])
    worker = Worker([auto], poll_interval=0.01)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(trigger.ack_started.wait(), 5)
    worker.stop()  # while the acknowledgement is still running
    await asyncio.wait_for(task, 5)
    assert trigger.acked == [None]  # not cancelled half-way: the event was acknowledged


async def test_worker_fires_schedules_with_injected_time() -> None:
    clock = FakeClock(datetime(2026, 3, 2, 8, 59, 59, tzinfo=UTC))
    system = ScriptedSystem()
    auto = Automation(
        system,
        triggers=[Schedule("0 9 * * *", name="daily", input="Report for {payload.date}")],
        clock=clock,
    )
    worker = Worker([auto], poll_interval=0.01, sleep=clock.sleep)
    task = asyncio.create_task(worker.run())

    async def fired() -> bool:
        return bool(await auto.store.list(status="succeeded"))

    await wait_for(fired)
    worker.stop()
    await asyncio.wait_for(task, 5)
    assert system.calls[0]["input"] == "Report for 2026-03-02"


class SlowSystem(ScriptedSystem):
    def __init__(self) -> None:
        super().__init__("slow")
        self.started = asyncio.Event()

    async def run(self, input: Any, **options: Any) -> RunResult[Any]:
        self.started.set()
        await asyncio.sleep(30)
        return await super().run(input, **options)


async def test_graceful_shutdown_hands_running_attempts_back() -> None:
    system = SlowSystem()
    auto = Automation(system)
    submitted = await auto.submit({"n": 1})
    worker = Worker([auto], poll_interval=0.01, drain_timeout=0.05)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(system.started.wait(), 5)
    assert worker.active == 1
    worker.stop()
    await asyncio.wait_for(task, 5)
    run = await auto.store.get(submitted.run.id)
    assert run is not None
    assert run.status == "queued" and run.attempts == 0 and run.lease_owner is None
    assert run.history[-1]["note"] == "worker stopped"


async def test_a_stop_before_the_run_starts_is_not_lost() -> None:
    auto = Automation(ScriptedSystem())
    await auto.submit({"n": 1})
    worker = Worker([auto], poll_interval=0.01)
    task = asyncio.create_task(worker.run())
    worker.stop()  # the task has not started yet

    await asyncio.wait_for(task, 5)
    # The next run of the worker starts afresh.
    again = asyncio.create_task(worker.run())
    await wait_for(lambda: _succeeded(auto))
    worker.stop()
    await asyncio.wait_for(again, 5)


async def _succeeded(auto: Automation) -> bool:
    return bool(await auto.store.list(automation=auto.name, status="succeeded"))


async def test_without_a_drain_timeout_shutdown_waits_for_the_attempt() -> None:
    class Brief(ScriptedSystem):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def run(self, input: Any, **options: Any) -> RunResult[Any]:
            self.started.set()
            await asyncio.sleep(0.2)
            return await super().run(input, **options)

    system = Brief()
    auto = Automation(system)
    submitted = await auto.submit({"n": 1})
    worker = Worker([auto], poll_interval=0.01, drain_timeout=None)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(system.started.wait(), 5)
    worker.stop()
    await asyncio.wait_for(task, 5)
    run = await auto.store.get(submitted.run.id)
    assert run is not None and run.status == "succeeded"


async def test_run_once_and_validation() -> None:
    auto = Automation(ScriptedSystem(), name="a")
    await auto.submit({"x": 1})
    worker = Worker([auto], worker_id="w1")
    [run] = await worker.run_once()
    assert run.status == "succeeded" and "w1" in repr(worker)
    with pytest.raises(ConfigurationError):
        Worker([])
    with pytest.raises(ConfigurationError):
        Worker([auto, Automation(ScriptedSystem(), name="a")])
    with pytest.raises(ConfigurationError):
        Worker([auto], concurrency=0)


async def test_run_once_handles_what_the_triggers_have_ready(tmp_path: Path) -> None:
    clock = FakeClock()
    (tmp_path / "a.txt").write_text("file body", encoding="utf-8")
    queue = InMemoryEventQueue()
    await queue.send({"text": "ticket 1"})
    system = ScriptedSystem()
    auto = Automation(
        system,
        triggers=[
            FileDrop(tmp_path, "*.txt", input="F: {payload.text}"),  # stable after 2 s
            QueueTrigger(queue, name="tickets", input="Q: {payload.text}"),
            Schedule(every="1h", name="hourly", input="S: {payload.date}"),
            Webhook("hook", secret="s"),  # pushed to the server: nothing to poll
        ],
    )
    worker = Worker([auto], clock=clock, sleep=clock.sleep)

    first = await worker.run_once()  # `awa worker --once` as a cron job

    assert sorted(call["input"] for call in system.calls) == ["F: file body", "Q: ticket 1"]
    assert {run.status for run in first} == {"succeeded"}
    assert (tmp_path / "processed" / "a.txt").exists() and len(queue) == 0
    # The schedule starts counting on the first pass and fires once its tick has come.
    clock.advance(hours=1)
    [tick] = await worker.run_once()
    assert tick.trigger == "hourly" and system.calls[-1]["input"] == "S: 2026-03-02"
    assert await Worker([auto], triggers=False, clock=clock).run_once() == []


async def test_a_cancel_from_another_process_stops_the_running_attempt() -> None:
    system = SlowSystem()
    auto = Automation(system)
    submitted = await auto.submit({})
    run = await auto.claim("w", lease=timedelta(seconds=0.15))
    assert run is not None
    attempt = asyncio.create_task(auto.execute(run, worker="w", lease=timedelta(seconds=0.15)))
    await asyncio.wait_for(system.started.wait(), 5)
    # Another process marks the run cancelled in the shared store; the lease renewal notices.
    from awa.automation.automation import cancel_run

    await cancel_run(auto.store, submitted.run.id, now=auto.clock.now(), reason="ops")
    stored = await asyncio.wait_for(attempt, 5)
    assert stored.status == "cancelled"


def test_an_automation_and_its_worker_run_again_in_a_new_event_loop() -> None:
    queue = InMemoryEventQueue()
    system = ScriptedSystem()
    auto = Automation(
        system,
        triggers=[QueueTrigger(queue, name="tickets", poll_interval=0.01, input="{payload}")],
    )
    worker = Worker([auto], poll_interval=0.05, tick_interval=0.05)
    claims: list[int] = []
    claim = auto.claim

    async def counting_claim(*args: Any, **kwargs: Any) -> Any:
        claims[-1] += 1
        return await claim(*args, **kwargs)

    auto.claim = counting_claim  # type: ignore[method-assign]

    async def process(text: str, expected: int) -> None:
        claims.append(0)
        task = asyncio.create_task(worker.run())
        await queue.send(text, dedup_key=text)

        async def finished() -> bool:
            return len(await auto.store.list(status="succeeded")) == expected

        await wait_for(finished)
        await asyncio.sleep(0.3)  # idle: the worker waits on its events
        worker.stop()
        await asyncio.wait_for(task, 5)
        auto._wake.clear()
        with pytest.raises(TimeoutError):  # usable in this loop
            await asyncio.wait_for(auto._wake.wait(), 0.01)

    asyncio.run(process("first", 1))
    asyncio.run(process("second", 2))

    assert sorted(call["input"] for call in system.calls) == ["first", "second"]
    # Events bound to the first loop fail every wait, so the worker would spin.
    assert claims[1] < 100, claims


class ClosingTrigger(BaseTrigger):
    """Idle trigger whose `close` fails (or records that it ran)."""

    def __init__(self, name: str, *, fail: bool) -> None:
        super().__init__(name)
        self.fail, self.closed, self.started = fail, False, asyncio.Event()

    async def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        self.started.set()
        await context.stopping.wait()
        return
        yield  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        self.closed = True
        if self.fail:
            raise RuntimeError("close failed")


async def test_a_failing_trigger_close_is_logged_and_the_others_still_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken, healthy = ClosingTrigger("broken", fail=True), ClosingTrigger("healthy", fail=False)
    worker = Worker([Automation(ScriptedSystem(), triggers=[broken, healthy])], poll_interval=0.01)
    with caplog.at_level(logging.DEBUG, logger="awa.automation.worker"):
        task = asyncio.create_task(worker.run())
        await asyncio.gather(broken.started.wait(), healthy.started.wait())
        worker.stop()
        await asyncio.wait_for(task, 5)
    assert broken.closed and healthy.closed
    [record] = [r for r in caplog.records if "Closing trigger" in r.getMessage()]
    assert "broken" in record.getMessage() and record.exc_info is not None


# ---------------------------------------------------------------- lease self-fencing


class UnreachableStore(InMemoryRunStore):
    """A store the workers named in `cut_off` cannot renew leases with (a network partition)."""

    def __init__(self, cut_off: frozenset[str] = frozenset({"a"})) -> None:
        super().__init__()
        self.cut_off = cut_off

    async def renew(
        self, run_id: str, *, worker: str, lease: timedelta, now: datetime, epoch: int | None = None
    ) -> bool:
        if worker in self.cut_off:
            raise ConnectionError("store unreachable")
        return await super().renew(run_id, worker=worker, lease=lease, now=now, epoch=epoch)


class OverlapProbe(ScriptedSystem):
    """Records whether two attempts of one process run ever executed at the same time."""

    def __init__(self, seconds: float) -> None:
        super().__init__("probe")
        self.seconds = seconds
        self.active: dict[str, int] = {}
        self.overlaps = 0
        self.cancelled = 0
        self.ran_for: list[float] = []

    async def run(self, input: Any, **options: Any) -> RunResult[Any]:
        run_id = str((options.get("metadata") or {})[PROCESS_RUN_METADATA_KEY])
        if self.active.get(run_id):
            self.overlaps += 1
        self.active[run_id] = self.active.get(run_id, 0) + 1
        started = time.monotonic()
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active[run_id] -= 1
            self.ran_for.append(time.monotonic() - started)
        return RunResult(run_id="r", agent="probe", output="done")


async def test_renew_errors_past_expiry_cancel_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("awa.observability.spans._tracer", lambda: provider.get_tracer("t"))
    system = OverlapProbe(seconds=30)
    auto = Automation(system, store=UnreachableStore(), concurrency=5, trace="off")
    await auto.submit({})
    run = await auto.claim("a", lease=timedelta(seconds=0.3))
    assert run is not None
    # Renewing fails every time: the worker stops its own attempt before the lease runs out.
    stored = await asyncio.wait_for(auto.execute(run, worker="a", lease=timedelta(seconds=0.3)), 20)
    assert system.cancelled == 1
    assert system.ran_for[0] < 0.3  # stopped within the lease, not after 30 s
    assert stored.status == "running"  # left for `tick` to requeue once the lease expired
    [span] = [s for s in exporter.get_finished_spans() if s.name == "awa.lease.stop"]
    assert span.attributes is not None
    assert span.attributes["awa.lease.reason"] == "lease expired"
    assert span.attributes["awa.lease.epoch"] == 1
    assert span.attributes["awa.process_run.id"] == run.id


async def test_an_agent_stopped_before_its_lease_expires_is_left_for_recovery() -> None:
    @tool
    async def wait() -> str:
        """Wait."""
        await asyncio.sleep(30)
        return "done"

    # The stop ends the run inside the tool, so the final answer is never requested.
    model = FakeChatModel([tool_call("wait", {}), "done"], allow_unconsumed=True)
    agent = Agent("slow", model=model, tools=[wait])
    auto = Automation(agent, store=UnreachableStore(), trace="off")
    await auto.submit({})
    run = await auto.claim("a", lease=timedelta(seconds=0.3))
    assert run is not None
    stored = await asyncio.wait_for(auto.execute(run, worker="a", lease=timedelta(seconds=0.3)), 20)
    assert stored.status == "running" and stored.attempts == 1


async def test_stale_epoch_write_rejected() -> None:
    clock = FakeClock()
    system = ScriptedSystem()
    auto = Automation(system, retry=RetryPolicy(max_attempts=3), clock=clock)
    submitted = await auto.submit({})
    first = await auto.claim("w", lease=timedelta(seconds=30))
    assert first is not None and first.lease_epoch == 1
    clock.advance(minutes=1)  # the lease expired; the same worker id claims the run again
    assert await auto.tick() == 1
    second = await auto.claim("w", lease=timedelta(seconds=30))
    assert second is not None and second.lease_epoch == 2
    # The store refuses the old claim's renewal and its outcome.
    assert not await auto.store.renew(
        first.id, worker="w", lease=timedelta(seconds=30), now=clock(), epoch=first.lease_epoch
    )
    stale = await auto.execute(first, worker="w")
    assert stale.status == "running" and stale.lease_epoch == 2
    done = await auto.execute(second, worker="w")
    assert done.status == "succeeded" and done.attempts == 2
    assert submitted.run.id == done.id


async def _take_over(auto: Automation, worker: str, lease: timedelta, count: int) -> None:
    """Requeue expired runs and execute them as `worker` until it took `count` runs."""

    taken: list[Any] = []
    while len(taken) < count:
        await auto.tick()
        while (run := await auto.claim(worker, lease=lease)) is not None:
            taken.append(asyncio.ensure_future(auto.execute(run, worker=worker, lease=lease)))
        await asyncio.sleep(0.02)
    await asyncio.gather(*taken)


@pytest.mark.slow
async def test_lease_fault_injection_never_runs_one_run_twice_at_once() -> None:
    """1,000 runs; worker `a` loses the store for every renewal; worker `b` takes over.

    About 35 s: the leases are real time (2.4 s, so a worker stops 0.8 s before its lease
    runs out), done in five batches of 200. The margin holds while the event loop lags less
    than a third of the lease, as it does with real leases of minutes, and also on a machine
    busy with a parallel test run; a warm-up attempt first loads what the first attempt of a
    process imports (that stalls the loop once).
    """

    warm_up = Automation(ScriptedSystem(), trace="off")
    await warm_up.submit({})
    await warm_up.run_pending()
    system = OverlapProbe(seconds=4.0)
    lease = timedelta(seconds=2.4)
    auto = Automation(
        system,
        store=UnreachableStore(),
        concurrency=10_000,
        retry=RetryPolicy(max_attempts=3),
        trace="off",
    )
    for batch in range(5):
        for n in range(200):
            await auto.submit({"n": batch * 200 + n})
        claimed = [await auto.claim("a", lease=lease) for _ in range(200)]
        first = [auto.execute(run, worker="a", lease=lease) for run in claimed if run]
        await asyncio.wait_for(asyncio.gather(*first, _take_over(auto, "b", lease, 200)), 30)
    succeeded = await auto.store.list(automation=auto.name, status="succeeded", limit=2000)
    assert system.overlaps == 0
    assert len(succeeded) == 1000
    assert system.cancelled == 1000  # every attempt of `a` stopped itself


class EndAckTrigger(SlowAckTrigger):
    """Acknowledged when the run ends (`ack_on_end`), without the slow acknowledgement."""

    async def ack(self, event: TriggerEvent, error: BaseException | None) -> None:
        self.acked.append(error)


async def test_a_run_another_worker_executed_is_settled_from_the_store() -> None:
    trigger = EndAckTrigger()
    auto = Automation(ScriptedSystem(), triggers=[trigger])
    claim = auto.claim

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    auto.claim = nothing  # type: ignore[method-assign]  # this worker never executes the run
    worker = Worker([auto], poll_interval=0.01, tick_interval=0.02)
    task = asyncio.create_task(worker.run())

    async def queued() -> bool:
        return bool(await auto.store.list(status="queued"))

    await wait_for(queued)
    run = await claim("elsewhere", lease=timedelta(minutes=1))
    assert run is not None
    await auto.execute(run, worker="elsewhere", lease=timedelta(minutes=1))

    async def acked() -> bool:
        return trigger.acked == [None]

    await wait_for(acked)
    worker.stop()
    await asyncio.wait_for(task, 5)
