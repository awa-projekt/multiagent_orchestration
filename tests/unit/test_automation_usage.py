"""Usage and cost of process runs: recorded on every outcome, one budget per run, additive in every store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.automation import ScriptedSystem
from _helpers.trace_helpers import desk_team
from awa import Agent
from awa._internal import aio as asyncio_compat
from awa.automation import (
    Automation,
    InMemoryRunStore,
    ProcessEvent,
    ProcessRun,
    RetryPolicy,
    SqliteRunStore,
)
from awa.cli import build_parser
from awa.contracts import ModelPrice, PriceTable, RunResult, Usage, UsageLimits
from awa.contracts.events import ModelResponseEvent
from awa.errors import (
    ConfigurationError,
    ModelRateLimitError,
    RunTimeoutError,
    UsageLimitExceededError,
)
from awa.observability import EventRecorder
from awa.runtime import use_global_subscriber
from awa.runtime._run import usage_of_failure
from awa.team import Team
from awa.testing import FakeChatModel, FakeClock, FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

PRICES = PriceTable(prices={"gpt-test": ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0)})
REPLY_COST = (1000 * 2.0 + 200 * 8.0) / 1_000_000  # 0.0036
LIMIT = UsageLimits(max_cost=0.0001, prices=PRICES)


def reply(text: str = "ok", **fields: Any) -> AIMessage:
    """A reply that used 1000 prompt and 200 completion tokens."""

    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        **fields,
    )


def agent(*script: AIMessage, repeat: bool = True, **options: Any) -> Agent:
    model = FakeChatModel(list(script) or [reply()], repeat_last=repeat, model_name="gpt-test")
    return Agent("desk", model=model, **options)


def automation(system: Any, **options: Any) -> Automation:
    options.setdefault("clock", FakeClock())
    return Automation(system, **options)


def used(requests: int, tokens: int = 1200, *, tool_calls: int = 0) -> Usage:
    usage = Usage()
    for _ in range(requests):
        usage.record_model_call(
            "desk",
            model="gpt-test",
            input_tokens=tokens * 5 // 6,
            output_tokens=tokens // 6,
            tool_calls=tool_calls,
        )
    return usage


class MeteredSystem:
    """An `AgentLike` that records the limits it was given and fails, then succeeds, with usage."""

    name = "metered"

    def __init__(self, failures: list[tuple[BaseException, Usage]], success: Usage) -> None:
        self.failures = failures
        self.success = success
        self.limits: list[UsageLimits | None] = []

    async def run(self, input: Any, *, usage_limits: UsageLimits | None = None, **_: Any) -> Any:
        self.limits.append(usage_limits)
        if self.failures:
            error, spent = self.failures.pop(0)
            error.awa_usage = spent  # type: ignore[attr-defined]
            raise error
        return RunResult(run_id="r", agent="metered", output="done", usage=self.success)


# ---------------------------------------------------------------------- a breach


async def test_a_breach_fails_the_run_at_once_and_records_usage_and_cost() -> None:
    events: list[ProcessEvent] = []
    model = agent()
    auto = automation(
        model,
        usage_limits=LIMIT,
        retry=RetryPolicy(max_attempts=3, initial="1s"),
        subscribers=[events.append],
    )
    await auto.submit({"x": 1})

    [run] = await auto.run_pending()

    assert (run.status, run.attempts) == ("failed", 1)  # not retried
    assert run.error_code == UsageLimitExceededError.code == "awa.usage_limit_exceeded"
    assert run.error is not None and "cost=0.0036 > max_cost=0.0001" in run.error
    assert (run.usage.requests, run.usage.total_tokens) == (1, 1200)
    assert run.usage.by_model["gpt-test"].output_tokens == 200
    assert run.cost == REPLY_COST == PRICES.cost(run.usage)
    assert await auto.run_pending() == []  # nothing is queued again
    failed = [e for e in events if e.type == "process.failed"]
    assert len(failed) == 1 and failed[0].data["cost"] == REPLY_COST
    assert failed[0].data["code"] == "awa.usage_limit_exceeded"
    assert [e.type for e in events].count("process.retry_scheduled") == 0


async def test_a_token_limit_ends_a_run_the_same_way() -> None:
    auto = automation(
        agent(), usage_limits=UsageLimits(max_total_tokens=100), retry=RetryPolicy(max_attempts=3)
    )
    await auto.submit({})

    [run] = await auto.run_pending()

    assert (run.status, run.error_code, run.attempts) == ("failed", UsageLimitExceededError.code, 1)
    assert run.usage.total_tokens == 1200 and run.cost is None  # no prices, no cost


async def test_a_run_that_stays_within_the_limits_succeeds_with_its_usage_and_cost() -> None:
    auto = automation(
        agent(), usage_limits=UsageLimits(max_cost=1.0, prices=PRICES, max_requests=5)
    )
    await auto.submit({})

    [run] = await auto.run_pending()

    assert run.status == "succeeded" and run.error_code is None
    assert (run.usage.requests, run.usage.total_tokens) == (1, 1200)
    assert run.cost == REPLY_COST


async def test_cost_needs_prices_and_prices_need_no_limits() -> None:
    plain = automation(agent())
    priced = automation(agent(), prices=PRICES)
    await plain.submit({})
    await priced.submit({})

    [without] = await plain.run_pending()
    [with_prices] = await priced.run_pending()

    assert without.usage.total_tokens == 1200 and without.cost is None
    assert with_prices.cost == REPLY_COST


async def test_a_model_without_a_price_leaves_the_cost_empty_instead_of_failing_the_run() -> None:
    other = PriceTable(prices={"another-model": ModelPrice(input_per_mtok=1, output_per_mtok=1)})
    auto = automation(agent(), prices=other)
    await auto.submit({})

    [run] = await auto.run_pending()

    assert run.status == "succeeded" and run.cost is None and run.usage.total_tokens == 1200


async def test_a_failed_attempt_that_is_not_a_breach_still_counts_its_usage() -> None:
    failing = MeteredSystem([(ModelRateLimitError("slow down"), used(2))], success=used(1))
    auto = automation(failing, prices=PRICES, retry=RetryPolicy(max_attempts=2, initial="1s"))
    await auto.submit({})

    [first] = await auto.run_pending()
    assert (first.status, first.attempts, first.usage.requests) == ("queued", 1, 2)
    assert first.cost == PRICES.cost(used(2))

    auto.clock.advance(seconds=5)  # type: ignore[attr-defined]
    [second] = await auto.run_pending()
    assert second.status == "succeeded" and second.usage.requests == 3
    assert second.cost == PRICES.cost(used(3))  # exactly the table's cost of the total usage


def test_usage_of_failure_is_none_for_errors_of_other_origin() -> None:
    assert usage_of_failure(ValueError("x")) is None


async def test_the_usage_of_a_failed_awa_run_comes_with_its_error() -> None:
    with pytest.raises(UsageLimitExceededError) as caught:
        await agent().run("hi", usage_limits=LIMIT)
    spent = usage_of_failure(caught.value)

    assert spent is not None and spent.requests == 1 and spent.total_tokens == 1200


# ---------------------------------------------------------------------- max_duration


async def test_max_duration_is_a_limit_not_a_retryable_timeout() -> None:
    expired = RunTimeoutError(
        "too slow", run_id="r", elapsed=2.0, limit=1.0, limit_name="max_duration"
    )
    auto = automation(
        ScriptedSystem(errors=[expired]), retry=RetryPolicy(max_attempts=3, initial="1s")
    )
    await auto.submit({})

    [run] = await auto.run_pending()

    assert (run.status, run.attempts) == ("failed", 1)
    assert run.error_code == UsageLimitExceededError.code


async def test_a_plain_timeout_is_still_retried() -> None:
    expired = RunTimeoutError("too slow", run_id="r", elapsed=2.0, limit=1.0)
    auto = automation(
        ScriptedSystem(errors=[expired]), retry=RetryPolicy(max_attempts=3, initial="1s")
    )
    await auto.submit({})

    [run] = await auto.run_pending()

    assert (run.status, run.error_code) == ("queued", "awa.run_timeout")


async def test_a_retry_policy_can_name_the_breach_code_to_retry_it() -> None:
    policy = RetryPolicy(max_attempts=2, initial="1s", retry_on=("awa.usage_limit_exceeded",))
    auto = automation(ScriptedSystem(errors=[UsageLimitExceededError("over")]), retry=policy)
    await auto.submit({})

    [run] = await auto.run_pending()

    assert (run.status, run.attempts) == ("queued", 1)


# ---------------------------------------------------------------------- one budget per run


async def test_the_limits_are_one_budget_for_all_attempts_of_a_run() -> None:
    limits = UsageLimits(
        max_requests=10,
        max_total_tokens=5000,
        max_tool_calls=4,
        max_cost=0.05,
        prices=PRICES,
        max_duration=60.0,
    )
    system = MeteredSystem(
        [(ModelRateLimitError("slow down"), used(2, tool_calls=1))], success=used(1)
    )
    auto = automation(system, usage_limits=limits, retry=RetryPolicy(max_attempts=2, initial="1s"))
    await auto.submit({})

    [first] = await auto.run_pending()
    auto.clock.advance(seconds=5)  # type: ignore[attr-defined]
    await auto.run_pending()

    assert system.limits[0] == limits  # the first attempt gets everything
    second = system.limits[1]
    assert second is not None
    assert second.max_requests == 8  # 10 less the 2 requests already made
    assert second.max_total_tokens == 5000 - 2 * 1200
    assert second.max_tool_calls == 2
    assert second.max_cost == pytest.approx(0.05 - first.cost)  # type: ignore[operator]
    assert second.max_duration == 60.0  # a duration holds for each attempt


async def test_a_run_resumed_after_an_approval_adds_to_the_usage_it_had() -> None:
    @tool
    def pay(amount: int) -> str:
        """Pay an amount."""

        return f"paid {amount}"

    call = {"name": "pay", "args": {"amount": 900}, "id": "c1"}
    system = agent(
        reply("", tool_calls=[call]),
        reply("Paid."),
        repeat=False,
        tools=[create_tool_approval_wrapper(pay)],
        checkpointer=InMemorySaver(),
    )
    auto = automation(system, usage_limits=UsageLimits(max_cost=0.02, prices=PRICES))
    await auto.submit({})

    [paused] = await auto.run_pending()
    assert paused.status == "waiting"
    assert (paused.usage.requests, paused.cost) == (1, REPLY_COST)

    await auto.decide(paused.id, {"action": "approve"})
    [done] = await auto.run_pending()

    assert done.status == "succeeded"
    assert (done.usage.requests, done.usage.total_tokens) == (2, 2400)
    assert done.cost == 2 * REPLY_COST == PRICES.cost(done.usage)


class Plain:
    name = "plain"

    async def run(self, input: Any, *, deps: Any = None) -> Any:
        return RunResult(run_id="r", agent="plain", output="done")


async def test_limits_for_a_system_that_cannot_enforce_them_are_refused_not_ignored() -> None:
    with pytest.raises(ConfigurationError, match=r"Plain\.run does not take `usage_limits`"):
        automation(Plain(), usage_limits=UsageLimits(max_requests=3))


async def test_a_system_that_takes_no_usage_limits_runs_when_no_limits_are_set() -> None:
    auto = automation(Plain())
    await auto.submit({})

    [run] = await auto.run_pending()

    assert run.status == "succeeded" and run.usage.requests == 0 and run.cost is None


# ---------------------------------------------------------------------- stores: additive fields


def finished(**fields: Any) -> ProcessRun:
    usage = used(2)
    return ProcessRun(automation="invoices", usage=usage, cost=PRICES.cost(usage), **fields)


async def test_the_in_memory_store_keeps_usage_and_cost() -> None:
    store = InMemoryRunStore()
    created, _ = await store.create(finished())

    fetched = await store.get(created.id)

    assert fetched is not None and fetched.usage == created.usage and fetched.cost == created.cost
    assert fetched.usage.total_tokens == 2400


async def test_the_sqlite_store_keeps_usage_and_cost_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "awa.db"
    first = SqliteRunStore(path)
    created, _ = await first.create(finished())
    await first.close()

    second = SqliteRunStore(path)
    reloaded = await second.get(created.id)
    await second.close()

    assert reloaded is not None
    assert reloaded.usage == created.usage and reloaded.cost == created.cost


def test_the_sqlite_store_decodes_older_and_newer_records() -> None:
    from awa.automation._sqlite import _run

    row = json.loads(finished().model_dump_json())
    del row["usage"], row["cost"]  # a record written before usage and cost existed
    old = _run(json.dumps(row))
    row["a_field_of_a_later_version"] = {"x": 1}

    assert old.usage == Usage() and old.cost is None and old.automation == "invoices"
    assert _run(json.dumps(row)).automation == "invoices"


def test_the_postgres_store_decodes_rows_written_before_usage_existed() -> None:
    from awa.automation._postgres import _run

    stored = finished().model_dump_json()
    row = json.loads(stored)
    del row["usage"], row["cost"]

    assert _run(json.dumps(row)).usage == Usage()  # the data column is one JSON document
    assert _run(stored).cost == finished().cost
    assert _run(row).cost is None  # psycopg hands a jsonb column over as a dict


def test_a_run_summary_lists_tokens_and_cost() -> None:
    summary = finished(status="failed", error_code="awa.usage_limit_exceeded").summary()

    assert (summary["requests"], summary["tokens"], summary["cost"]) == (2, 2400, 2 * REPLY_COST)
    assert summary["error_code"] == "awa.usage_limit_exceeded"
    assert ProcessRun(automation="x").summary()["cost"] is None


# ---------------------------------------------------------------------- awa runs list / show


def seeded_store(tmp_path: Path) -> Path:
    path = tmp_path / "awa.db"
    store = SqliteRunStore(path)
    asyncio_compat.run(store.create(finished(status="failed", error="over")))
    asyncio_compat.run(store.create(ProcessRun(automation="invoices", status="succeeded")))
    asyncio_compat.run(store.close())
    return path


def runs_cli(*argv: str) -> int:
    args = build_parser(["runs", *argv]).parse_args(["runs", *argv])
    code: int = args.handler(args)
    return code


def test_runs_list_and_show_display_tokens_and_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = seeded_store(tmp_path)

    assert runs_cli("list", "--store", str(path)) == 0
    table = capsys.readouterr().out.splitlines()
    assert table[0].split()[:7] == [
        "RUN",
        "AUTOMATION",
        "STATUS",
        "ATTEMPTS",
        "TOKENS",
        "COST",
        "CREATED",
    ]
    rows = {line.split()[2]: line.split() for line in table[1:]}
    assert rows["failed"][4:6] == ["2400", f"{2 * REPLY_COST:.4f}"]
    assert rows["succeeded"][4:6] == ["0", "-"]

    assert runs_cli("list", "--store", str(path), "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert {r["tokens"] for r in listed} == {0, 2400}

    run_id = next(r["id"] for r in listed if r["tokens"])
    assert runs_cli("show", run_id, "--store", str(path)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["usage"]["total_tokens"] == 2400 and shown["cost"] == 2 * REPLY_COST
    assert shown["usage"]["by_model"]["gpt-test"]["requests"] == 2


# ---------------------------------------------------------------------- teams that pause


async def team_run(limits: UsageLimits | None = None) -> tuple[ProcessRun, EventRecorder]:
    """The desk team (12 model calls, two approvals) through an automation, with its events."""

    auto = automation(
        desk_team(),
        usage_limits=limits,
        prices=PriceTable(default=ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0)),
        input="{payload}",
    )
    events = EventRecorder()
    with use_global_subscriber(events):
        await auto.submit({"topic": "Brief on Debian"})
        run = (await auto.run_pending())[-1]
        while run.status == "waiting":
            await auto.decide(run.id, {"action": "approve"}, actor="alice")
            run = (await auto.run_pending())[-1]
    return run, events


async def test_a_team_that_pauses_is_counted_once_not_once_per_resume() -> None:
    run, events = await team_run()

    replies = events.of_type(ModelResponseEvent)
    assert run.status == "succeeded" and len(replies) == 12
    assert run.usage.requests == len(replies)  # a team reports the whole run after a resume
    assert run.usage.input_tokens == sum(e.input_tokens for e in replies)
    assert run.usage.output_tokens == sum(e.output_tokens for e in replies)
    assert run.cost is not None
    assert run.cost == pytest.approx(
        (run.usage.input_tokens * 2.0 + run.usage.output_tokens * 8.0) / 1_000_000
    )


async def test_a_team_is_not_stopped_by_the_usage_it_had_before_the_pause() -> None:
    # The run uses 12 calls in three parts; a budget of 14 holds for the whole run, not per part.
    run, _ = await team_run(UsageLimits(max_requests=14))

    assert run.status == "succeeded" and run.error_code is None


async def test_a_team_still_stops_at_the_budget_of_the_whole_run() -> None:
    # 10 of the 12 calls: low on budget, the agents answer without tools instead of failing.
    run, _ = await team_run(UsageLimits(max_requests=10))

    assert run.attempts == 1 and run.usage.requests <= 10
    assert run.status == "succeeded" and run.error_code is None


# ---------------------------------------------------------------------- a retry after a resume


class Provider:
    """Counts the replies a fake provider really gave; the call numbered `fail_on` raises instead."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.calls = 0
        self.answered = 0
        self.fail_on = fail_on

    def answer(self, text: str = "", **fields: Any) -> AIMessage:
        self.calls += 1
        if self.calls == self.fail_on:
            raise ModelRateLimitError("provider hiccup")
        self.answered += 1
        return reply(text, **fields)


@tool
def publish(title: str) -> str:
    """Publish a title."""

    return f"published {title}"


def _delegating(provider: Provider, target: str) -> FunctionChatModel:
    def script(call: ModelCall) -> AIMessage:
        if not any(isinstance(m, ToolMessage) for m in call.messages):
            delegate = tool_call("delegate_task", {"agent": target, "task": "t"})
            return provider.answer(tool_calls=[delegate])
        return provider.answer("done")

    return FunctionChatModel(script, model_name="gpt-test")


def _publisher(provider: Provider) -> Agent:
    def script(call: ModelCall) -> AIMessage:
        if not any(isinstance(m, ToolMessage) for m in call.messages):
            return provider.answer(tool_calls=[tool_call("publish", {"title": "x"})])
        return provider.answer("published")

    return Agent(
        "researcher",
        description="Researches.",
        model=FunctionChatModel(script, model_name="gpt-test"),
        tools=[create_tool_approval_wrapper(publish)],
    )


def desk(provider: Provider) -> Team:
    """lead -> researcher (approval): 2 model calls, then it pauses."""

    lead = Agent("lead", description="Leads.", model=_delegating(provider, "researcher"))
    return Team(
        "desk", orchestrator=lead, members=[_publisher(provider)], checkpointer=InMemorySaver()
    )


def nested_desk(provider: Provider) -> Team:
    """outer lead -> inner team (lead -> researcher with approval)."""

    inner_lead = Agent(
        "inner_lead", description="Leads.", model=_delegating(provider, "researcher")
    )
    inner = Team("inner", orchestrator=inner_lead, members=[_publisher(provider)])
    lead = Agent("lead", description="Leads.", model=_delegating(provider, "inner"))
    return Team("desk", orchestrator=lead, members=[inner], checkpointer=InMemorySaver())


def single_agent(provider: Provider) -> Agent:
    """One agent: asks for an approval, then answers."""

    def script(call: ModelCall) -> AIMessage:
        if not any(isinstance(m, ToolMessage) for m in call.messages):
            return provider.answer(tool_calls=[tool_call("publish", {"title": "x"})])
        return provider.answer("published")

    return Agent(
        "desk",
        description="Desk.",
        model=FunctionChatModel(script, model_name="gpt-test"),
        tools=[create_tool_approval_wrapper(publish)],
        checkpointer=InMemorySaver(),
    )


async def paused_approved_failed_retried(system: Any, max_requests: int) -> ProcessRun:
    """Run to the approval, approve, let the resumed attempt fail, let the retry run."""

    auto = automation(
        system,
        name="desk",
        input="{payload}",
        usage_limits=UsageLimits(max_requests=max_requests),
        retry=RetryPolicy(max_attempts=3, initial="1s", retry_on_types=(ModelRateLimitError,)),
    )
    run = (await auto.submit({"topic": "go"})).run
    for _ in range(12):
        await auto.run_pending()
        run = await auto.store.get(run.id)  # type: ignore[assignment]
        if run.status == "waiting":
            await auto.decide(run.id, {"action": "approve"}, actor="alice")
        elif run.status == "queued":
            auto.clock.advance(seconds=10)  # type: ignore[attr-defined]
        else:
            return run
    raise AssertionError(f"the run did not end: {run.status}")


@pytest.mark.parametrize(
    ("build", "fail_on"),
    [(desk, 4), (nested_desk, 6), (single_agent, 2)],
    ids=["team", "nested team", "agent"],
)
async def test_a_run_retried_after_a_failed_resume_counts_what_was_really_used(
    build: Any, fail_on: int
) -> None:
    # The run pauses at the approval, the resumed attempt makes one more call and fails on the
    # next one (`fail_on`), the retry makes the last call: `fail_on + 1` calls in all. A budget of
    # exactly that holds, and the record counts every call once (a team reports the whole run
    # after a resume, an agent only the resumed part).
    provider = Provider(fail_on=fail_on)

    run = await paused_approved_failed_retried(build(provider), max_requests=fail_on + 1)

    assert run.status == "succeeded" and run.error_code is None, run.error
    assert run.usage.requests == provider.calls == fail_on + 1
    assert run.usage.total_tokens == provider.answered * 1200
    assert run.cost is None


async def test_a_team_retried_after_a_failed_resume_is_still_held_to_the_whole_budget() -> None:
    provider = Provider(fail_on=4)

    run = await paused_approved_failed_retried(desk(provider), max_requests=3)

    assert run.status == "failed" and run.error_code == "awa.usage_limit_exceeded"
    assert provider.calls == run.usage.requests == 3  # the fourth call was never made


# ---------------------------------------------------------------------- an attempt that is stopped


@tool
async def slow_lookup() -> str:
    """Look something up, slowly."""

    await asyncio.sleep(30)
    return "found"


def slow_agent(provider: Provider) -> Agent:
    def script(call: ModelCall) -> AIMessage:
        if not any(isinstance(m, ToolMessage) for m in call.messages):
            return provider.answer(tool_calls=[tool_call("slow_lookup", {})])
        return provider.answer("done")

    return Agent(
        "desk",
        description="Desk.",
        model=FunctionChatModel(script, model_name="gpt-test"),
        tools=[slow_lookup],
    )


async def started(auto: Automation, provider: Provider) -> tuple[ProcessRun, asyncio.Task[Any]]:
    """Claim the run and let its attempt make its first model call (the tool then sleeps)."""

    run = await auto.claim("w1")
    assert run is not None
    calls = provider.calls
    task = asyncio.ensure_future(auto.execute(run, worker="w1"))
    for _ in range(200):
        if provider.calls > calls:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    return run, task


async def test_the_usage_of_an_attempt_stopped_by_a_shutdown_still_counts() -> None:
    provider = Provider()
    auto = automation(slow_agent(provider), usage_limits=UsageLimits(max_requests=3))
    run = (await auto.submit({"x": 1})).run

    for round_ in range(1, 4):
        _, task = await started(auto, provider)
        task.cancel()  # the worker shuts down
        with pytest.raises(asyncio.CancelledError):
            await task
        stored = await auto.store.get(run.id)
        assert stored is not None
        assert (stored.status, stored.attempts) == ("queued", 0)  # handed back, not counted
        assert stored.usage.requests == provider.calls == round_

    # One budget for the run, however often it was restarted: no fourth call.
    [last] = await auto.run_pending()
    assert last.status == "failed" and last.error_code == "awa.usage_limit_exceeded"
    assert provider.calls == last.usage.requests == 3


async def test_the_usage_of_an_attempt_cancelled_by_a_person_still_counts() -> None:
    provider = Provider()
    auto = automation(slow_agent(provider))
    run = (await auto.submit({"x": 1})).run
    _, task = await started(auto, provider)

    await auto.cancel(run.id, reason="not needed")
    stopped = await asyncio.wait_for(task, 5)

    assert stopped.status == "cancelled"
    stored = await auto.store.get(run.id)
    assert stored is not None and stored.usage.requests == provider.calls == 1
