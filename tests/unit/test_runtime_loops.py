"""Loop protection: the pure helpers, and every loop kind ending within its bound and a small wall time, with a diagnosis."""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypedDict

import pytest
from langchain_core.tools import ToolException, tool
from langgraph.graph import END, START, StateGraph

from awa.agents import Agent
from awa.automation._support import lease_losses
from awa.automation.models import ProcessRun
from awa.connectors import MockTransport, _engine, connector_tools, get_connector, mock_response
from awa.contracts import ErrorEvent, RunEvent, UsageLimits
from awa.core.settings import get_settings
from awa.errors import (
    ConfigurationError,
    HandoffError,
    MaxStepsExceededError,
    NoProgressError,
    StepCheckFailedError,
    UsageLimitExceededError,
)
from awa.runtime import use_global_subscriber
from awa.runtime.ids import AgentKey
from awa.runtime.loops import call_signature, cycle_refusal
from awa.runtime.termination import StopDiagnosis, ceilings, with_ceilings
from awa.team import Team, TeamLimits
from awa.testing import (
    FunctionChatModel,
    ModelCall,
    assert_terminates_via_own_budget,
    structured_output,
    tool_call,
)
from awa.workflows import Checked, Loop, ProcessStep, ProcessWorkflow, RuleCheck, StepRetry
from awa.workflows.checked import Verdict


@pytest.mark.parametrize(
    ("chain", "link", "strict", "refused"),
    [
        ((), AgentKey("a", brief_hash="h"), False, False),
        ((AgentKey("a", brief_hash="h"),), AgentKey("a", brief_hash="h"), False, True),
        ((AgentKey("a", brief_hash="h"),), AgentKey("a", brief_hash="other"), False, False),
        ((AgentKey("a", brief_hash="h"),), AgentKey("a", brief_hash="other"), True, True),
        ((AgentKey("a", "team", "h"),), AgentKey("a", "other", "h"), False, False),
        ((AgentKey("a"),), AgentKey("a"), False, False),
        ((AgentKey("a"),), AgentKey("a"), True, True),
        ((AgentKey("a"), AgentKey("b")), AgentKey("c", brief_hash="h"), True, False),
    ],
)
def test_cycle_refusal(
    chain: tuple[AgentKey, ...], link: AgentKey, strict: bool, refused: bool
) -> None:
    reason = cycle_refusal(chain, link, strict=strict)

    assert (reason is not None) is refused
    if reason is not None:
        assert repr(link.agent) in reason and "loop" in reason


def test_cycle_refusal_names_the_chain() -> None:
    chain = (AgentKey("lead", "desk"), AgentKey("writer", "desk", "h"))

    reason = cycle_refusal(chain, AgentKey("writer", "desk", "h"))

    assert reason is not None and "desk.lead -> desk.writer -> desk.writer" in reason


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),
        ({"q": "Hello  World"}, {"q": "hello world"}, True),
        ({"q": "x"}, {"q": "y"}, False),
        ({"n": {1, 2}}, {"n": {2, 1}}, True),
    ],
)
def test_call_signature_is_canonical(
    first: dict[str, object], second: dict[str, object], same: bool
) -> None:
    assert (call_signature("t", first) == call_signature("t", second)) is same


def test_call_signature_separates_tools_and_survives_unserialisable_arguments() -> None:
    assert call_signature("a", {}) != call_signature("b", {})
    assert call_signature("t", {"o": object}).startswith("t:")


WALL = 20.0
"""Seconds any single scenario may take; far above the real time, far below a hang."""


async def bounded(work: Any) -> Any:
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(work, WALL)
    finally:
        assert time.perf_counter() - started < WALL


def forever(*answers: Any) -> FunctionChatModel:
    """A model that answers `answers` in turn, then repeats the last one forever."""

    return FunctionChatModel(lambda call: answers[min(call.index, len(answers) - 1)])


@tool
def lookup(n: int) -> str:
    """Look something up."""

    return "nothing found"


@tool
def broken(n: int) -> str:
    """A tool that always fails."""

    raise ToolException(f"backend down for {n}")


broken.handle_tool_error = True


def rejecting_judge() -> Agent:
    verdict = {"decision": "revise", "feedback": "no", "criteria": [{"id": "c1", "passed": False}]}
    return Agent("judge", model=forever(structured_output(Verdict, verdict)), output_type=Verdict)


def jira_search(transport: Any) -> Any:
    spec = get_connector("jira")
    env = {
        v.name: (f"secret-{v.name.lower()}-value-0123" if v.secret else "https://jira.example.test")
        for v in spec.variables()
    }
    [search] = [
        t
        for t in connector_tools(
            "jira", env=env, config={"requires_approval": False}, transport=transport
        )
        if t.name == "jira_search_issues"
    ]
    return search


# ---------------------------------------------------------------- agent tool loops


async def test_the_same_call_with_the_same_result_stops_with_a_diagnosis() -> None:
    model = forever(tool_call("lookup", {"n": 1}))
    agent = Agent("solo", model=model, tools=[lookup], max_steps=500)
    events: list[RunEvent] = []
    with use_global_subscriber(events.append), pytest.raises(NoProgressError) as caught:
        await bounded(agent.run("go"))

    assert model.call_count == ceilings().max_identical_calls
    diagnosis = caught.value.diagnosis
    assert isinstance(diagnosis, StopDiagnosis)
    assert (diagnosis.stop_reason, diagnosis.bound, diagnosis.tool) == (
        "stopped:no_progress",
        "identical_tool_calls",
        "lookup",
    )
    assert diagnosis.last_steps and diagnosis.suggested_fix
    assert caught.value.details["stop"]["bound"] == "identical_tool_calls"
    [error] = [e for e in events if isinstance(e, ErrorEvent)]
    assert error.stop is not None and error.stop["bound"] == "identical_tool_calls"
    assert "nothing found" not in str(error.stop)  # names and digests, never content


async def test_a_polling_tool_whose_result_changes_is_not_stopped() -> None:
    state = {"n": 0}

    @tool
    def status() -> str:
        """Job status."""

        state["n"] += 1
        return f"pending {state['n']}"

    model = forever(*[tool_call("status", {})] * 8, "done")
    agent = Agent("poller", model=model, tools=[status], max_steps=20)
    result = await bounded(agent.run("go"))
    assert result.output == "done"


async def test_a_tool_that_always_errors_stops_after_errors_in_a_row() -> None:
    model = FunctionChatModel(lambda call: tool_call("broken", {"n": call.index}))
    agent = Agent("solo", model=model, tools=[broken], max_steps=500)
    with pytest.raises(NoProgressError) as caught:
        await bounded(agent.run("go"))
    assert caught.value.diagnosis.bound == "repeated_tool_errors"
    assert model.call_count <= ceilings().max_tool_errors + 1


async def test_without_limits_a_run_tree_has_a_request_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed(monkeypatch).setenv("AWA_RUN_MAX_REQUESTS", "7")
    model = FunctionChatModel(lambda call: tool_call("lookup", {"n": call.index}))
    agent = Agent("solo", model=model, tools=[lookup], max_steps=10_000)
    with pytest.raises(UsageLimitExceededError) as caught:
        await bounded(agent.run("go"))
    assert model.call_count == 7
    assert caught.value.diagnosis.bound == "max_requests"
    assert caught.value.diagnosis.stop_reason == "stopped:budget"


async def test_an_explicit_limit_wins_over_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    refreshed(monkeypatch).setenv("AWA_RUN_MAX_REQUESTS", "3")
    model = FunctionChatModel(lambda call: tool_call("lookup", {"n": call.index}))
    agent = Agent("solo", model=model, tools=[lookup], max_steps=10_000)
    with pytest.raises(UsageLimitExceededError):
        await bounded(agent.run("go", usage_limits=UsageLimits(max_requests=5)))
    assert model.call_count == 5


async def test_the_time_ceiling_stops_a_run_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed(monkeypatch).setenv("AWA_RUN_MAX_SECONDS", "1")

    @tool
    async def slow() -> str:
        """Takes forever."""

        await asyncio.sleep(60)
        return "late"

    agent = Agent("solo", model=forever(tool_call("slow", {})), tools=[slow])
    with pytest.raises(TimeoutError) as caught:
        await bounded(agent.run("go"))
    assert caught.value.diagnosis.stop_reason == "stopped:deadline"  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", ["0", "-1", "unlimited", "none"])
def test_a_ceiling_cannot_be_switched_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    refreshed(monkeypatch).setenv("AWA_RUN_MAX_TOOL_CALLS", value)
    with pytest.raises(ConfigurationError, match="cannot be switched off"):
        ceilings()


def test_none_fields_take_the_ceilings_and_a_timeout_keeps_its_time() -> None:
    bounds = ceilings()
    limits = with_ceilings(UsageLimits(max_requests=9), timeout=None)
    assert limits.max_requests == 9
    assert limits.max_tool_calls == bounds.run_max_tool_calls
    assert limits.max_total_tokens == bounds.run_max_tokens
    assert limits.max_depth == bounds.run_max_depth
    assert limits.max_duration == bounds.run_max_seconds
    assert with_ceilings(None, timeout=30).max_duration is None


# ---------------------------------------------------------------- teams


async def test_a_handoff_ping_pong_between_two_agents_ends() -> None:
    ping = Agent("ping", description="Ping.", model=forever(tool_call("transfer_to_pong", {})))
    pong = Agent("pong", description="Pong.", model=forever(tool_call("transfer_to_ping", {})))
    team = Team(
        "desk",
        orchestrator=ping,
        members=[pong],
        handoffs={"ping": ["pong"], "pong": ["ping"]},
        limits=TeamLimits(max_handoffs=3),
    )
    with pytest.raises((HandoffError, MaxStepsExceededError, NoProgressError)):
        await bounded(team.run("go"))


# ---------------------------------------------------------------- workflows


async def test_a_reviewer_that_always_rejects_ends_after_max_iterations() -> None:
    worker = Agent("writer", model=FunctionChatModel(lambda call: f"draft {call.index}"))
    judge = rejecting_judge()
    checked = Checked("gate", worker, judge=judge, rubric=["Perfect."], max_iterations=3)
    result = await bounded(checked.run("write"))
    assert result.metadata["status"] == "exhausted"
    assert result.metadata["rounds"] == 3
    assert result.metadata["stop"]["bound"] == "max_iterations"


async def test_a_worker_that_returns_the_same_draft_stops_without_progress() -> None:
    worker = Agent("writer", model=forever("the same draft"))
    judge = rejecting_judge()
    checked = Checked("gate", worker, judge=judge, rubric=["Perfect."], max_iterations=10)
    result = await bounded(checked.run("write"))
    assert result.metadata["stop_reason"] == "no_progress"
    assert result.metadata["rounds"] == 2


async def test_a_loop_whose_body_repeats_itself_stops_without_progress() -> None:
    body = Agent("echo", model=forever("same"))
    loop = Loop("again", body, until=lambda result: False, max_iterations=1000)
    result = await bounded(loop.run("go"))
    assert result.metadata["stopped_reason"] == "no_progress"
    assert result.metadata["iterations"] == 2
    assert result.metadata["stop"]["bound"] == "identical_outputs"


async def test_a_loop_that_changes_runs_to_its_cap() -> None:
    body = Agent("count", model=FunctionChatModel(lambda call: f"n{call.index}"))
    loop = Loop("again", body, until=lambda result: False, max_iterations=5)
    result = await bounded(loop.run("go"))
    assert (result.metadata["stopped_reason"], result.metadata["iterations"]) == (
        "max_iterations",
        5,
    )


async def test_a_process_step_that_always_fails_ends_after_its_attempts() -> None:
    agent = Agent("reader", model=forever("0"))
    calls: list[float] = []

    async def no_wait(seconds: float) -> None:
        calls.append(seconds)

    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep(
                "extract",
                agent,
                checks=(RuleCheck("extract.rule.1", "True", "Always wrong."),),
                retry=StepRetry(5, initial=0.0),
            )
        ],
        sleep=no_wait,
    )
    with pytest.raises(StepCheckFailedError):
        await bounded(wf.run("go"))
    assert agent._model.call_count == 5  # type: ignore[attr-defined]


def test_a_step_retry_has_a_hard_maximum() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        StepRetry(1000)


# ---------------------------------------------------------------- automation, paging


def test_a_run_that_keeps_losing_its_worker_is_counted() -> None:
    def history(*notes: tuple[str, str]) -> list[dict[str, str]]:
        return [{"at": "t", "status": status, "note": note} for status, note in notes]

    run = ProcessRun(automation="a")
    run.history = history(
        ("queued", "from api"),
        ("running", "claimed by w1"),
        ("queued", "lease of w1 expired; requeued"),
        ("running", "claimed by w2"),
        ("queued", "lease of w2 expired; requeued"),
        ("running", "claimed by w3"),
    )
    assert lease_losses(run) == 2
    run.history.append({"at": "t", "status": "waiting", "note": "approval"})
    assert lease_losses(run) == 0


async def test_a_resuming_run_that_crashes_its_worker_is_dead_lettered() -> None:
    from datetime import UTC, datetime, timedelta

    from awa.automation import Automation

    agent = Agent("desk", description="Desk.", model=forever("done"))
    auto = Automation(agent)
    run = (await auto.submit({"x": 1})).run
    now = datetime.now(UTC)
    for lost in range(1, 10):
        claimed = await auto.claim(f"w{lost}")
        assert claimed is not None
        await auto.store.modify(
            run.id,
            lambda current: current.model_copy(
                update={"resuming": True, "lease_expires_at": now - timedelta(seconds=1)}
            ),
            now=now,
        )
        await auto._recover(run.id, now)
        stored = await auto.store.get(run.id)
        assert stored is not None
        if stored.status == "dead_lettered":
            break
    assert stored.status == "dead_lettered"
    assert stored.error_code == "awa.resume_crash_loop"
    assert lost == ceilings().max_lease_losses


def test_a_list_call_that_always_has_a_next_page_stops_at_the_page_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
    refreshed(monkeypatch).setenv("AWA_MAX_PAGES_PER_CALL", "4")
    pages = [
        mock_response(json={"issues": [{"key": f"A-{i}"}], "nextPageToken": f"t{i}"})
        for i in range(100)
    ]
    transport = MockTransport({"GET /rest/api/3/search/jql": pages})
    out = jira_search(transport).invoke({"jql": "project = A", "limit": 200})
    assert len(transport.requests) == 4
    assert "next_page" in str(out)


def test_a_server_that_names_the_same_page_again_ends_the_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
    page = {"issues": [{"key": "A-1"}], "nextPageToken": "same"}
    transport = MockTransport({"GET /rest/api/3/search/jql": [mock_response(json=page)] * 50})
    jira_search(transport).invoke({"jql": "project = A", "limit": 200})
    assert len(transport.requests) == 2  # the second answer repeats the token: stop


# ---------------------------------------------------------------- triggers


async def test_a_trigger_event_that_fires_itself_again_starts_one_run() -> None:
    from awa.automation import Automation, TriggerEvent

    calls: list[str] = []

    def respond(call: ModelCall) -> str:
        calls.append("x")
        return "done"

    agent = Agent("desk", description="Desk.", model=FunctionChatModel(respond))
    auto = Automation(agent)
    event = {"source": "mail", "payload": {"x": 1}, "dedup_key": "mail-1"}
    first = await auto.submit(TriggerEvent(**event))
    for _ in range(20):  # the run's own output fires the same event again and again
        again = await auto.submit(TriggerEvent(**event))
        assert again.run.id == first.run.id
    await bounded(auto.run_pending())
    assert len(calls) == 1


def refreshed(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """`monkeypatch`, with the settings cache dropped so the next run reads the new value."""

    get_settings.cache_clear()
    return monkeypatch


class LoopState(TypedDict):
    rounds: int
    max_rounds: int
    status: str


def _bump(state: LoopState) -> dict[str, Any]:
    return {"rounds": state["rounds"] + 1}


def _never_agrees_but_has_a_budget(state: LoopState) -> str:
    if state["rounds"] >= state["max_rounds"]:
        return "end"
    return "orchestrator"


def _never_agrees_and_has_no_budget(state: LoopState) -> str:
    return "orchestrator"


def _build(loop_back: Any) -> Any:
    builder: StateGraph[LoopState] = StateGraph(LoopState)
    builder.add_node("orchestrator", lambda state: {})
    builder.add_node("critic", _bump)
    builder.add_edge(START, "orchestrator")
    builder.add_edge("orchestrator", "critic")
    builder.add_conditional_edges("critic", loop_back, {"end": END, "orchestrator": "orchestrator"})
    return builder.compile()


async def test_terminates_when_the_graph_has_a_real_budget_guard() -> None:
    graph = _build(_never_agrees_but_has_a_budget)

    result = await assert_terminates_via_own_budget(
        graph, {"rounds": 0, "max_rounds": 5, "status": "pending"}, recursion_limit=50
    )

    assert result["rounds"] == 5


async def test_fails_loudly_when_the_graph_has_no_budget_guard_at_all() -> None:
    graph = _build(_never_agrees_and_has_no_budget)

    with pytest.raises(AssertionError, match="instead of stopping through its own budget"):
        await assert_terminates_via_own_budget(
            graph, {"rounds": 0, "max_rounds": 999999, "status": "pending"}, recursion_limit=10
        )
