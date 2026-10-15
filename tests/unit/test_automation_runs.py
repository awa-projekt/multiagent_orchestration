"""Durable process runs: dedup, retries, dead letters, leases, waits, SLAs, versions and the failure modes of bad configuration, input and observers."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.automation import ScriptedSystem
from awa import Agent
from awa.automation import (
    SLA,
    Automation,
    InMemoryRunStore,
    ProcessEvent,
    ProcessRun,
    RetryPolicy,
    SqliteRunStore,
    TriggerEvent,
    Webhook,
    wait_for_event,
)
from awa.automation.automation import PROCESS_RUN_METADATA_KEY
from awa.automation.versioning import plan_migration, system_digest
from awa.automation.waits import event_delivery, parse_wait_request, wait_timeout
from awa.errors import (
    AgentRunError,
    ConfigurationError,
    ModelProviderError,
    ModelRateLimitError,
    NoWaitingRunError,
    ProcessRunStateError,
    RunNotFoundError,
    TemplateError,
    ToolTimeoutError,
)
from awa.observability import AuditLog
from awa.observability.audit import InMemoryAuditSink, verify_chain
from awa.testing import FakeChatModel, FakeClock, tool_call
from awa.tools import create_tool_approval_wrapper


def automation(system: Any = None, **options: Any) -> Automation:
    options.setdefault("clock", FakeClock())
    return Automation(system or ScriptedSystem(), **options)


def recorder() -> tuple[list[ProcessEvent], Any]:
    events: list[ProcessEvent] = []
    return events, events.append


# ---------------------------------------------------------------------- submit


async def test_submit_renders_templates_and_deduplicates() -> None:
    events, subscriber = recorder()
    auto = automation(
        input="Book invoice {payload.no} for {payload.supplier|upper}",
        thread_id="inv-{payload.no|slug}",
        dedup="{payload.no}",
        subscribers=[subscriber],
    )
    first = await auto.submit({"no": "RE 17", "supplier": "acme"})
    second = await auto.submit({"no": "RE 17", "supplier": "other"})

    assert first.created and not second.created
    assert second.run.id == first.run.id
    assert first.run.input == "Book invoice RE 17 for ACME"
    assert first.run.thread_id == "inv-re-17"
    assert first.run.dedup_key == "RE 17"
    assert first.run.status == "queued" and first.run.version == auto.version
    assert [e.type for e in events] == ["process.queued", "trigger.duplicate"]

    [done] = await auto.run_pending()
    assert done.status == "succeeded" and done.output == "done: Book invoice RE 17 for ACME"
    assert done.attempts == 1 and done.lease_owner is None


async def test_tenants_with_the_same_thread_template_get_separate_threads() -> None:
    auto = automation(thread_id="inv-{payload.no}", dedup="{payload.no}")
    acme = await auto.submit({"no": "17"}, tenant="acme")
    beta = await auto.submit({"no": "17"}, tenant="beta")

    assert beta.created
    assert acme.run.thread_id != beta.run.thread_id


async def test_dedup_window_and_trigger_overrides() -> None:
    clock = FakeClock()
    hook = Webhook("hook", allow_unsigned=True, input="From hook: {payload.text}")
    auto = automation(triggers=[hook], dedup_window="1h", clock=clock)
    event = TriggerEvent(source="hook", payload={"text": "hi"}, dedup_key="k1")
    first = await auto.submit(event)
    assert first.run.input == "From hook: hi"
    assert not (await auto.submit(event.model_copy(update={"id": "e2"}))).created
    clock.advance(hours=2)
    assert (await auto.submit(event.model_copy(update={"id": "e3"}))).created
    assert auto.render(event)["dedup_key"] == "k1"


async def test_template_errors_are_reported_and_raised() -> None:
    events, subscriber = recorder()
    auto = automation(input="{payload.missing}", subscribers=[subscriber])
    with pytest.raises(TemplateError):
        await auto.submit({"other": 1})
    assert events[0].type == "trigger.failed"
    with pytest.raises(TemplateError, match="empty"):
        await automation(thread_id="{payload.x}").submit({"x": " "})


async def test_callable_templates_and_deps_reach_the_system() -> None:
    system = ScriptedSystem()
    auto = automation(
        system,
        input=lambda event: [{"role": "user", "content": event.payload["q"]}],
        deps=lambda event: {"user": event.payload["user"]},
        metadata={"awa.tenant": "acme"},
    )
    await auto.submit({"q": "hello", "user": "u1"})
    [run] = await auto.run_pending()
    call = system.calls[0]
    assert call["input"] == [{"role": "user", "content": "hello"}]
    assert call["deps"] == {"user": "u1"}
    assert call["metadata"][PROCESS_RUN_METADATA_KEY] == run.id
    assert call["metadata"]["awa.attempt"] == 1


# ---------------------------------------------------------------------- retries


async def test_retryable_errors_are_retried_with_backoff() -> None:
    clock = FakeClock()
    events, subscriber = recorder()
    system = ScriptedSystem(
        errors=[
            ModelRateLimitError("429", kind="rate_limit", retryable=True),
            ToolTimeoutError("slow tool"),
        ]
    )
    auto = automation(
        system,
        clock=clock,
        retry=RetryPolicy(max_attempts=3, initial="30s", backoff=2.0),
        subscribers=[subscriber],
    )
    await auto.submit({"n": 1})

    [first] = await auto.run_pending()
    assert first.status == "queued" and first.attempts == 1
    assert first.next_attempt_at == clock() + timedelta(seconds=30)
    assert first.error is not None and first.error_code == "awa.model_rate_limit"
    assert await auto.run_pending() == []  # backoff not over
    clock.advance(seconds=30)
    [second] = await auto.run_pending()
    assert second.next_attempt_at == clock() + timedelta(seconds=60)
    clock.advance(seconds=60)
    [third] = await auto.run_pending()
    assert third.status == "succeeded" and third.attempts == 3
    assert [e.type for e in events].count("process.retry_scheduled") == 2


async def test_non_retryable_errors_fail_at_once() -> None:
    auto = automation(
        ScriptedSystem(errors=[ModelProviderError("bad key", kind="authentication")]),
        retry=RetryPolicy(max_attempts=5),
    )
    await auto.submit({})
    [run] = await auto.run_pending()
    assert run.status == "failed" and run.attempts == 1
    assert run.error_code == "awa.model_provider"


async def test_exhausted_retries_dead_letter_and_can_be_retried() -> None:
    clock = FakeClock()
    notified, notifier = recorder()
    audit = AuditLog(InMemoryAuditSink())
    system = ScriptedSystem(errors=[TimeoutError("down"), TimeoutError("down")])
    auto = automation(
        system,
        clock=clock,
        retry=RetryPolicy(max_attempts=2, initial=1),
        notify=[notifier],
        audit_log=audit,
    )
    submitted = await auto.submit({"id": 7})
    await auto.run_pending()
    clock.advance(seconds=1)
    [run] = await auto.run_pending()
    assert run.status == "dead_lettered"
    assert [r.id for r in await auto.dead_letters()] == [run.id]
    assert [e.type for e in notified] == ["process.dead_lettered"]

    retried = await auto.retry(run.id, actor="ops")
    assert retried.status == "queued" and retried.attempts == 0 and retried.error is None
    [done] = await auto.run_pending()
    assert done.status == "succeeded"
    records = audit.sink.read()  # type: ignore[attr-defined]
    assert verify_chain(records).valid
    kinds = [r.event_type for r in records if r.run_id == submitted.run.id]
    assert kinds[0] == "process.queued" and kinds[-1] == "process.succeeded"
    assert "process.dead_lettered" in kinds
    with pytest.raises(ProcessRunStateError):
        await auto.retry(run.id)


RUN_RECORD_FIELDS = [
    "actor",
    "attempts",
    "automation",
    "cost",
    "created_at",
    "dedup_key",
    "error",
    "error_code",
    "event_id",
    "history",
    "id",
    "input",
    "lease_epoch",
    "lease_expires_at",
    "lease_owner",
    "max_attempts",
    "next_attempt_at",
    "output",
    "payload",
    "resume_value",
    "resuming",
    "revision",
    "sla_breached",
    "sla_deadline",
    "sla_warn_at",
    "sla_warned",
    "status",
    "tenant",
    "thread_checkpoint",
    "thread_id",
    "trace",
    "trigger",
    "updated_at",
    "usage",
    "usage_paused",
    "version",
    "waits",
]


async def stored_records(store: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The records `store` holds after dedup, a retried failure and two successes."""

    counter = iter(range(1, 100))
    monkeypatch.setattr("awa.automation.models.new_id", lambda prefix: f"{prefix}_{next(counter)}")
    clock = FakeClock()
    system = ScriptedSystem(errors=[ToolTimeoutError("slow tool")])
    auto = automation(
        system,
        store=store,
        clock=clock,
        trace="off",
        dedup="{payload.no}",
        dedup_window="1h",
        retry=RetryPolicy(max_attempts=2, initial="30s"),
    )
    assert (await auto.submit({"no": 1})).created
    assert not (await auto.submit({"no": 1})).created
    clock.advance(seconds=1)
    await auto.submit({"no": 2})
    assert [run.status for run in await auto.run_pending()] == ["queued", "succeeded"]
    assert await auto.run_pending() == []
    clock.advance(seconds=30)
    assert [run.status for run in await auto.run_pending()] == ["succeeded"]
    records = [run.model_dump(mode="json") for run in await store.list()]
    await store.close()
    return records


async def test_memory_and_sqlite_stores_record_a_run_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_memory = await stored_records(InMemoryRunStore(), monkeypatch)
    in_sqlite = await stored_records(SqliteRunStore(tmp_path / "awa.db"), monkeypatch)

    assert in_memory == in_sqlite
    assert [sorted(record) for record in in_memory] == [RUN_RECORD_FIELDS] * 2
    second, first = in_memory
    assert (second["id"], second["revision"], second["attempts"]) == ("proc_6", 2, 1)
    assert (first["id"], first["revision"], first["attempts"]) == ("proc_2", 4, 2)
    assert first["updated_at"] == "2026-03-02T09:00:31Z"
    assert [(entry["status"], entry["note"]) for entry in first["history"]] == [
        ("queued", "from api"),
        ("running", "claimed by local"),
        ("queued", "attempt 1 failed; retry in 30s"),
        ("running", "claimed by local"),
        ("succeeded", "succeeded"),
    ]


def test_retry_policy_delays() -> None:
    policy = RetryPolicy(initial="10s", backoff=3, max_delay="1m", jitter=0)
    assert [policy.delay(n).total_seconds() for n in (1, 2, 3)] == [10, 30, 60]
    hinted = ModelRateLimitError("429", retry_after=45, retryable=True)
    assert policy.delay(1, hinted).total_seconds() == 45
    assert policy.is_retryable(hinted)
    assert not policy.is_retryable(ValueError("x"))
    assert RetryPolicy(retry_on_types=(KeyError,)).is_retryable(KeyError("k"))
    assert RetryPolicy(jitter=0.5).delay(1).total_seconds() >= 30


def test_the_jitter_of_a_retry_delay_comes_from_the_given_source() -> None:
    policy = RetryPolicy(initial="40s", jitter=0.5, max_delay="1h")
    assert policy.delay(1, rand=lambda: 0.0).total_seconds() == 40  # never shorter
    assert policy.delay(1, rand=lambda: 0.5).total_seconds() == 50
    assert policy.delay(2, rand=lambda: 1.0).total_seconds() == 120  # 80 s plus half of it
    assert RetryPolicy(initial="40s", jitter=0).delay(1, rand=lambda: 1.0).total_seconds() == 40


# ---------------------------------------------------------------------- leases


async def test_expired_leases_are_recovered_by_another_worker() -> None:
    clock = FakeClock()
    events, subscriber = recorder()
    auto = automation(clock=clock, retry=RetryPolicy(max_attempts=2), subscribers=[subscriber])
    await auto.submit({"x": 1})
    crashed = await auto.claim("worker-a", lease="1m")
    assert crashed is not None and crashed.status == "running"
    assert await auto.claim("worker-b") is None  # concurrency 1: the run is taken

    clock.advance(seconds=30)
    assert await auto.tick() == 0  # lease still valid
    clock.advance(seconds=31)
    assert await auto.tick() == 1
    recovered = await auto.store.get(crashed.id)
    assert recovered is not None and recovered.status == "queued" and recovered.lease_owner is None
    assert "process.recovered" in [e.type for e in events]

    run = await auto.claim("worker-b")
    assert run is not None and run.attempts == 2
    done = await auto.execute(run, worker="worker-b")
    assert done.status == "succeeded"
    # The crashed worker comes back and tries to renew: it lost the run.
    assert not await auto.store.renew(
        run.id, worker="worker-a", lease=timedelta(minutes=1), now=clock()
    )


async def test_a_lost_lease_after_the_last_attempt_dead_letters() -> None:
    clock = FakeClock()
    auto = automation(clock=clock)
    await auto.submit({})
    await auto.claim("w", lease=10)
    clock.advance(seconds=11)
    await auto.tick()
    [run] = await auto.store.list()
    assert run.status == "dead_lettered" and run.error_code == "awa.worker_lost"


async def test_a_worker_that_lost_its_lease_does_not_overwrite_the_run() -> None:
    clock = FakeClock()
    auto = automation(clock=clock, retry=RetryPolicy(max_attempts=3))
    await auto.submit({})
    stale = await auto.claim("a", lease=10)
    assert stale is not None
    clock.advance(seconds=11)
    await auto.tick()
    fresh = await auto.claim("b", lease=60)
    assert fresh is not None
    events, subscriber = recorder()
    auto.subscribers.append(subscriber)
    result = await auto.execute(stale, worker="a")
    assert result.status == "running" and result.lease_owner == "b"
    assert [e.type for e in events][-1] == "process.lease_lost"


async def test_concurrency_limits_running_runs() -> None:
    auto = automation(concurrency=2)
    for n in range(3):
        await auto.submit({"n": n})
    assert await auto.claim("a") is not None
    assert await auto.claim("b") is not None
    assert await auto.claim("c") is None


async def test_cancel_and_errors_for_unknown_runs() -> None:
    auto = automation()
    submitted = await auto.submit({})
    cancelled = await auto.cancel(submitted.run.id, reason="duplicate", actor="ops")
    assert cancelled.status == "cancelled" and "duplicate" in (cancelled.error or "")
    assert await auto.run_pending() == []
    with pytest.raises(ProcessRunStateError):
        await auto.cancel(submitted.run.id)
    with pytest.raises(RunNotFoundError):
        await auto.retry("proc_missing")
    retried = await auto.retry(submitted.run.id)
    assert retried.status == "queued"


# ---------------------------------------------------------------------- waits


@tool
def ask_supplier(invoice: str) -> str:
    """Ask the supplier to confirm the invoice and wait for the reply."""

    reply = wait_for_event(f"reply:{invoice}", timeout="5d", remind_every="1d")
    return f"supplier said {reply['text']}"


@tool
def ask_or_continue(invoice: str) -> str:
    """Wait at most a day for a reply."""

    reply = wait_for_event(f"reply:{invoice}", timeout="1d", on_timeout="continue", default=None)
    return "no reply" if reply is None else "reply"


def clerk(
    saver: InMemorySaver, script: list[Any], tools: list[Any] | None = None
) -> Agent[Any, Any]:
    return Agent(
        "clerk", model=FakeChatModel(script), tools=tools or [ask_supplier], checkpointer=saver
    )


async def test_wait_pause_deliver_resume_across_a_restart(tmp_path: Path) -> None:
    saver = InMemorySaver()
    store = SqliteRunStore(tmp_path / "awa.db")
    clock = FakeClock()
    events, subscriber = recorder()

    def process(script: list[Any]) -> Automation:
        return Automation(
            clerk(saver, script),
            input="Check invoice {payload.no}",
            store=store,
            clock=clock,
            version="v1",
            subscribers=[subscriber],
        )

    before = process([tool_call("ask_supplier", {"invoice": "I-1"})])
    await before.submit({"no": "I-1"})
    [paused] = await before.run_pending()
    assert paused.status == "waiting"
    assert paused.wait_keys == ["reply:I-1"]
    assert paused.waits[0].deadline == clock() + timedelta(days=5)
    assert paused.thread_id == paused.id

    # "Restart": new automation, system and store objects on the same file and checkpointer.
    after = process(["Confirmed by the supplier."])
    after.store = SqliteRunStore(tmp_path / "awa.db")
    [delivered] = await after.deliver("reply:I-1", {"text": "all good"}, actor="supplier")
    assert delivered.status == "queued" and delivered.resuming
    [done] = await after.run_pending()
    assert done.status == "succeeded" and done.output == "Confirmed by the supplier."
    assert done.attempts == 1  # resuming is not a new attempt
    kinds = [e.type for e in events]
    assert kinds == [
        "process.queued",
        "process.started",
        "process.waiting",
        "process.delivered",
        "process.resumed",
        "process.succeeded",
    ]
    with pytest.raises(NoWaitingRunError):
        await after.deliver("reply:I-1", {}, require_waiting=True)


async def test_an_early_delivery_answers_the_wait_when_it_starts() -> None:
    saver = InMemorySaver()
    auto = automation(clerk(saver, [tool_call("ask_supplier", {"invoice": "I-2"}), "ok"]))
    assert await auto.deliver("reply:I-2", {"text": "early"}) == []
    await auto.submit({})
    runs = await auto.run_pending()
    assert [r.status for r in runs] == ["queued", "succeeded"]
    assert runs[-1].output == "ok"


async def test_wait_timeouts_reminders_and_escalation() -> None:
    clock = FakeClock()
    saver = InMemorySaver()
    events, subscriber = recorder()
    auto = automation(
        clerk(saver, [tool_call("ask_supplier", {"invoice": "I-3"})]),
        clock=clock,
        subscribers=[subscriber],
    )
    await auto.submit({})
    [paused] = await auto.run_pending()
    clock.advance(days=1, seconds=1)
    assert await auto.tick() == 1
    clock.advance(days=1)
    await auto.tick()
    run = await auto.store.get(paused.id)
    assert run is not None and run.waits[0].reminders_sent == 2
    clock.advance(days=3)
    await auto.tick()
    failed = await auto.store.get(paused.id)
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "awa.wait_timeout"
    kinds = [e.type for e in events]
    assert kinds.count("process.reminder") >= 2
    assert "process.wait_timeout" in kinds and kinds[-1] == "process.failed"


async def test_a_timed_out_wait_can_continue_the_run() -> None:
    clock = FakeClock()
    saver = InMemorySaver()
    model_script = [tool_call("ask_or_continue", {"invoice": "I-4"}), "Proceeding without reply."]
    auto = automation(clerk(saver, model_script, tools=[ask_or_continue]), clock=clock)
    await auto.submit({})
    await auto.run_pending()
    clock.advance(days=1, seconds=1)
    await auto.tick()
    [done] = await auto.run_pending()
    assert done.status == "succeeded"
    assert done.output == "Proceeding without reply."


def test_wait_request_helpers() -> None:
    assert parse_wait_request({"awa_wait": {"key": "k"}}).key == "k"  # type: ignore[union-attr]
    assert parse_wait_request({"tool": "x"}) is None
    assert event_delivery(1) == {"awa_wait_result": "delivered", "payload": 1}
    assert wait_timeout() == {"awa_wait_result": "timeout"}
    with pytest.raises(ValueError):
        wait_for_event(" ")
    with pytest.raises(ValueError, match="timeout must be positive"):
        wait_for_event("k", timeout=0)
    with pytest.raises(ValueError, match="remind_every must be positive"):
        wait_for_event("k", remind_every="0s")


async def test_approvals_escalate_past_their_sla_and_are_decided() -> None:
    clock = FakeClock()
    saver = InMemorySaver()
    notified, notifier = recorder()

    @tool
    def pay(amount: int) -> str:
        """Pay an amount."""

        return f"paid {amount}"

    agent = Agent(
        "payer",
        model=FakeChatModel([tool_call("pay", {"amount": 900}), "Payment done."]),
        tools=[create_tool_approval_wrapper(pay)],
        checkpointer=saver,
    )
    auto = automation(
        agent,
        clock=clock,
        sla=SLA(complete_within="1d", warn_at=0.5, approval_within="4h", remind_every="1h"),
        notify=notifier,
    )
    await auto.submit({})
    [paused] = await auto.run_pending()
    [wait] = paused.waits
    assert paused.status == "waiting" and wait.kind == "approval"
    assert wait.escalate_at == clock() + timedelta(hours=4)

    clock.advance(hours=4, minutes=1)
    await auto.tick()
    clock.advance(hours=9)
    await auto.tick()  # SLA warning at 12h
    kinds = [e.type for e in notified]
    assert "process.escalated" in kinds and "process.reminder" in kinds
    assert "process.sla_warning" in kinds

    with pytest.raises(ProcessRunStateError):
        await auto.decide(paused.id, {"action": "approve"}, interrupt_id="other")
    queued = await auto.decide(paused.id, {"action": "approve"}, actor="lead")
    assert queued.status == "queued"
    assert queued.resume_value == {"action": "approve", "reviewer": "lead"}
    [done] = await auto.run_pending()
    assert done.status == "succeeded" and done.output == "Payment done."
    clock.advance(days=1)
    await auto.tick()
    assert "process.sla_breached" not in [e.type for e in notified]  # finished in time


@pytest.mark.parametrize(
    ("sla", "deadline", "warn_at"),
    [
        (SLA(complete_within="1d", warn_at=0.5), timedelta(days=1), timedelta(hours=12)),
        (SLA(complete_within="4h", warn_at=0.25), timedelta(hours=4), timedelta(hours=1)),
        (SLA(complete_within="1h", warn_at=None), timedelta(hours=1), None),
        (SLA(approval_within="4h"), None, None),
        (None, None, None),
    ],
    ids=["half", "quarter", "no-warning", "no-deadline", "no-sla"],
)
async def test_a_run_gets_its_sla_deadline_and_warning_time_from_its_creation(
    sla: SLA | None, deadline: timedelta | None, warn_at: timedelta | None
) -> None:
    clock = FakeClock()
    auto = automation(clock=clock, sla=sla)
    created = clock()
    clock.advance(minutes=7)  # the submission time counts, not the construction time
    submitted = await auto.submit({})
    start = created + timedelta(minutes=7)
    assert submitted.run.sla_deadline == (None if deadline is None else start + deadline)
    assert submitted.run.sla_warn_at == (None if warn_at is None else start + warn_at)


async def test_sla_breach_can_cancel_the_run() -> None:
    clock = FakeClock()
    auto = automation(clock=clock, sla=SLA(complete_within="1h", warn_at=None, on_breach="cancel"))
    submitted = await auto.submit({})
    clock.advance(hours=2)
    await auto.tick()
    run = await auto.store.get(submitted.run.id)
    assert run is not None and run.sla_breached and run.status == "cancelled"


async def test_interrupts_without_a_checkpointer_fail_the_run() -> None:
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("ask_supplier", {"invoice": "x"})]),
        tools=[ask_supplier],
    )
    auto = automation(agent)
    assert not auto.threads
    await auto.submit({})
    [run] = await auto.run_pending()
    assert run.status == "failed" and AgentRunError.__name__ in (run.error or "")


async def test_a_retry_resumes_the_thread_from_its_last_step() -> None:
    saver = InMemorySaver()
    calls: list[str] = []

    @tool
    def lookup(order: str) -> str:
        """Look up an order."""

        calls.append(order)
        if len(calls) == 1:
            raise ToolTimeoutError("backend slow")
        return "shipped"

    script = [
        tool_call("lookup", {"order": "1"}),
        tool_call("lookup", {"order": "1"}),
        "Order 1 shipped.",
    ]
    agent = Agent("desk", model=FakeChatModel(script), tools=[lookup], checkpointer=saver)
    clock = FakeClock()
    auto = automation(agent, clock=clock, retry=RetryPolicy(max_attempts=2, initial=1))
    await auto.submit({"q": "where is 1"})
    runs = await auto.run_pending()
    clock.advance(seconds=2)
    runs += await auto.run_pending()
    assert [run.status for run in runs] == ["queued", "succeeded"]
    assert runs[-1].output == "Order 1 shipped."
    assert runs[-1].attempts == 2


# ---------------------------------------------------------------------- versions


async def test_runs_are_pinned_to_their_version() -> None:
    store = InMemoryRunStore()
    v1 = automation(version="v1", store=store)
    await v1.submit({"n": 1})
    v2 = automation(version="v2", store=store)
    assert await v2.run_pending() == []  # a v2 worker leaves v1 runs alone
    both = automation(version="v2", store=store, previous_versions={"v1": ScriptedSystem("old")})
    [run] = await both.run_pending()
    assert run.status == "succeeded" and run.version == "v1"
    plan = await v2.migration_plan()
    assert plan == []


async def test_at_wait_migration_moves_waiting_runs() -> None:
    saver = InMemorySaver()
    store = InMemoryRunStore()
    events, subscriber = recorder()
    old = Automation(
        clerk(saver, [tool_call("ask_supplier", {"invoice": "I-9"})] * 2),
        store=store,
        version="v1",
        clock=FakeClock(),
    )
    await old.submit({})
    await old.submit({"second": True})
    first, second = await old.run_pending()
    assert (first.status, second.status) == ("waiting", "waiting")
    new = Automation(
        clerk(saver, ["Done on v2.", "Done on v2."]),
        store=store,
        version="v2",
        migration="at_wait",
        clock=FakeClock(),
        subscribers=[subscriber],
    )
    plan = await new.migration_plan()
    assert {item.action for item in plan} <= {"migrate"}
    moved = await new.deliver("reply:I-9", {"text": "ok"})
    assert all(run.version == "v2" for run in moved)
    assert "process.migrated" in [e.type for e in events]
    assert len(moved) == 2
    finished = await new.run_pending()
    assert [run.status for run in finished] == ["succeeded", "succeeded"]

    waiting = ProcessRun(automation="desk", status="waiting", version="v1")
    running = ProcessRun(automation="desk", status="queued", version="v1")
    plan = plan_migration([waiting, running], current="v3", policy="pin", known=["v1"])
    assert [item.action for item in plan] == ["pinned", "pinned"]
    plan = plan_migration([waiting, running], current="v3", policy="at_wait")
    assert [item.action for item in plan] == ["migrate", "orphaned"]


def test_a_migration_plan_skips_finished_runs_and_runs_of_the_current_version() -> None:
    old = {"automation": "desk", "version": "v1"}
    finished = [ProcessRun(status=status, **old) for status in ("succeeded", "failed", "cancelled")]
    current = ProcessRun(automation="desk", status="waiting", version="v3")
    unpinned = ProcessRun(automation="desk", status="waiting")
    waiting = ProcessRun(status="waiting", **old)
    plan = plan_migration([*finished, current, unpinned, waiting], current="v3", policy="at_wait")
    assert [(item.run_id, item.action) for item in plan] == [(waiting.id, "migrate")]
    assert plan_migration(finished, current="v3", policy="pin", known=["v1"]) == []


async def test_migrate_applies_the_plan() -> None:
    store = InMemoryRunStore()
    await store.create(ProcessRun(automation="desk", status="waiting", version="v1"))
    auto = automation(store=store, version="v2", migration="at_wait")
    [item] = await auto.migrate()
    assert item.action == "migrate"
    [run] = await store.list()
    assert run.version == "v2"


async def test_migrate_leaves_a_run_that_finished_after_the_plan() -> None:
    store = InMemoryRunStore()
    run, _ = await store.create(ProcessRun(automation="desk", status="waiting", version="v1"))
    auto = automation(store=store, version="v2", migration="at_wait")
    plan = auto.migration_plan

    async def plan_then_finish() -> Any:
        found = await plan()
        await store.modify(
            run.id,
            lambda current: current.model_copy(update={"status": "completed"}),
            now=run.created_at,
        )
        return found

    auto.migration_plan = plan_then_finish  # type: ignore[method-assign]
    [item] = await auto.migrate()

    assert item.action == "migrate"
    [stored] = await store.list()
    assert (stored.status, stored.version) == ("completed", "v1")


def test_system_digest_is_stable_and_sensitive() -> None:
    a = Agent("clerk", model=FakeChatModel(["x"]), tools=[ask_supplier])
    b = Agent("clerk", model=FakeChatModel(["y"]), tools=[ask_supplier])
    c = Agent("clerk", model=FakeChatModel(["x"]), tools=[ask_or_continue])
    assert system_digest(a) == system_digest(b)
    assert system_digest(a) != system_digest(c)
    assert system_digest(a).startswith("sha256:")


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"concurrency": 0}, "concurrency must be >= 1"),
        ({"run_timeout": 0}, "run_timeout must be positive"),
        ({"migration": "later"}, "migration must be 'pin' or 'at_wait'"),
        (
            {
                "triggers": [
                    Webhook("hook", allow_unsigned=True),
                    Webhook("hook", allow_unsigned=True),
                ]
            },
            "duplicate trigger 'hook'",
        ),
    ],
)
def test_bad_configuration_is_refused_at_construction(
    options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        automation(**options)


def test_repr_names_the_triggers() -> None:
    assert repr(automation(name="desk", triggers=[Webhook("hook", allow_unsigned=True)])) == (
        "Automation('desk', triggers=['hook'])"
    )


async def test_a_template_that_renders_too_long_a_key_is_refused() -> None:
    auto = automation(thread_id="{payload.text}")

    with pytest.raises(TemplateError, match="more than 255 characters"):
        await auto.submit({"text": "x" * 300})
    assert await auto.store.list() == []


async def test_failing_observers_never_fail_the_process() -> None:
    class BrokenAudit:
        def record(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit down")

    seen: list[ProcessEvent] = []

    def broken(event: ProcessEvent) -> None:
        raise RuntimeError("subscriber down")

    auto = automation(subscribers=[broken, seen.append], audit_log=BrokenAudit())
    await auto.submit({})

    [run] = await auto.run_pending()

    assert run.status == "succeeded"
    assert [e.type for e in seen][-1] == "process.succeeded"


async def test_an_awaitable_deps_value_is_resolved_for_the_run() -> None:
    system = ScriptedSystem()

    async def deps(event: Any) -> dict[str, str]:
        return {"tenant": "acme"}

    auto = automation(system, deps=deps)
    await auto.submit({})

    await auto.run_pending()

    assert system.calls[0]["deps"] == {"tenant": "acme"}


async def test_answers_for_runs_that_do_not_wait_are_refused() -> None:
    auto = automation()
    submitted = await auto.submit({})
    [done] = await auto.run_pending()

    with pytest.raises(RunNotFoundError):
        await auto.decide("proc_missing", {"action": "approve"})
    with pytest.raises(ProcessRunStateError):
        await auto.decide(done.id, {"action": "approve"})
    assert submitted.run.id == done.id
    assert await auto.deliver("reply:none", {"text": "nobody waits"}) == []


async def test_a_delivery_for_another_key_leaves_the_wait_untouched() -> None:
    saver = InMemorySaver()
    auto = automation(clerk(saver, [tool_call("ask_supplier", {"invoice": "I-9"})]))
    await auto.submit({})
    [waiting] = await auto.run_pending()
    assert waiting.status == "waiting"

    assert await auto.deliver("reply:other", {"text": "x"}) == []
    with pytest.raises(ProcessRunStateError, match="waits for no approval"):
        await auto.decide(waiting.id, {"action": "approve"})
    still = await auto.store.get(waiting.id)
    assert still is not None and still.status == "waiting"


def test_a_wait_answered_by_its_timeout_is_neither_escalated_nor_reminded() -> None:
    from datetime import UTC, datetime, timedelta

    from awa.automation._support import act_on_waits
    from awa.automation.models import ProcessRun, WaitState

    now = datetime(2026, 3, 2, 12, tzinfo=UTC)
    wait = WaitState(
        interrupt_id="i-1",
        key="reply:1",
        requested_at=now - timedelta(days=2),
        deadline=now - timedelta(hours=1),
        on_timeout="continue",
        escalate_at=now - timedelta(hours=2),
        remind_every=timedelta(hours=4),
        next_reminder_at=now - timedelta(minutes=5),
    )
    events: list[tuple[str, dict[str, object]]] = []

    act_on_waits(ProcessRun(automation="a", status="waiting", waits=[wait]), now, events)

    assert [name for name, _ in events] == ["process.wait_timeout"]
    assert wait.answered and not wait.escalated and wait.reminders_sent == 0
