"""`ProcessWorkflow` retries, `after_last_try`, `when`, and the fault-injection call budget."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from awa.agents import Agent
from awa.contracts import StepEvent, UsageLimits
from awa.contracts.events import RunEvent
from awa.errors import (
    ModelProviderError,
    StepCheckFailedError,
    StepRejectedError,
    UsageLimitExceededError,
)
from awa.runtime import use_global_subscriber
from awa.testing import FakeChatModel, structured_output, tool_call
from awa.tools import tool
from awa.workflows import (
    AfterLastTry,
    ProcessStep,
    ProcessWorkflow,
    RuleCheck,
    RunCondition,
    StepRetry,
)


class Invoice(BaseModel):
    supplier: str
    total: float


def invoice(total: float, supplier: str = "ACME") -> Any:
    return structured_output(Invoice, {"supplier": supplier, "total": total})


def scripted(name: str, *responses: Any, repeat: bool = False, **kwargs: Any) -> Agent:
    model = FakeChatModel(list(responses), repeat_last=repeat)
    return Agent(name, description=f"The {name} agent.", model=model, **kwargs)


def reader(*totals: float) -> Agent:
    """Answers the totals in order, then keeps answering the last one."""

    return scripted("reader", *(invoice(t) for t in totals), output_type=Invoice, repeat=True)


def model_of(agent: Agent) -> FakeChatModel:
    model = agent._model
    assert isinstance(model, FakeChatModel)
    return model


def no_total() -> RuleCheck:
    return RuleCheck("extract.rule.1", "total <= 0", "No total found.")


class Waits:
    """Records the waits between tries instead of sleeping."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def steps_of(events: list[RunEvent]) -> list[tuple[str, str, int | None]]:
    return [(e.step, e.status, e.attempt) for e in events if isinstance(e, StepEvent)]


async def run_collecting(wf: ProcessWorkflow, request: str = "go") -> tuple[Any, list[RunEvent]]:
    seen: list[RunEvent] = []
    with use_global_subscriber(seen.append):
        try:
            result: Any = await wf.run(request)
        except Exception as exc:  # noqa: BLE001 - the tests look at the error and the events
            result = exc
    return result, seen


# --------------------------------------------------------------------------- retry on check


async def test_a_failed_check_is_tried_again_with_the_message_fed_back() -> None:
    agent = reader(0, 120)
    waits = Waits()
    wf = ProcessWorkflow(
        "p",
        [ProcessStep("extract", agent, checks=(no_total(),), retry=StepRetry(3, initial=2.0))],
        sleep=waits,
    )

    result, events = await run_collecting(wf)

    assert result.output == Invoice(supplier="ACME", total=120)
    assert steps_of(events) == [
        ("extract", "started", 1),
        ("extract", "checking", 1),
        ("extract", "retrying", 1),
        ("extract", "started", 2),
        ("extract", "checking", 2),
        ("extract", "checked", 2),
    ]
    assert waits.seconds == [2.0]
    second_input = model_of(agent).calls[1].messages[-1].content
    assert "did not pass the checks of this step: No total found." in str(second_input)
    assert result.metadata["steps"][0]["attempts"] == 2


async def test_the_waits_grow_by_the_backoff_and_stop_at_the_cap() -> None:
    waits = Waits()
    retry = StepRetry(5, initial=1.0, backoff=3.0, max_delay=5.0)
    wf = ProcessWorkflow(
        "p",
        [ProcessStep("extract", reader(0, 0, 0, 0, 5), checks=(no_total(),), retry=retry)],
        sleep=waits,
    )

    await wf.run("go")

    assert waits.seconds == [1.0, 3.0, 5.0, 5.0]


async def test_after_the_last_try_the_run_stops_and_names_the_tries() -> None:
    agent = reader(0, 0, 0)
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep("extract", agent, checks=(no_total(),), retry=StepRetry(3, initial=0)),
            ProcessStep("book", scripted("booker", "booked")),
        ],
    )

    result, events = await run_collecting(wf)

    assert isinstance(result, StepCheckFailedError)
    assert "after 3 tries" in str(result)
    assert model_of(agent).call_count == 3
    assert ("extract", "failed", 3) in steps_of(events)
    assert not any(e.step == "book" for e in events if isinstance(e, StepEvent))


async def test_a_step_retrying_only_errors_does_not_retry_a_failed_check() -> None:
    agent = reader(0)
    retry = StepRetry(3, initial=0, on=frozenset({"error"}))
    wf = ProcessWorkflow("p", [ProcessStep("extract", agent, checks=(no_total(),), retry=retry)])

    with pytest.raises(StepCheckFailedError):
        await wf.run("go")

    assert model_of(agent).call_count == 1


# --------------------------------------------------------------------------- retry on error


class Flaky:
    """An agent-like step that raises `failures` times, then answers."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.inner = scripted("flaky", "fine")
        self.failures = failures
        self.error = error or ModelProviderError("provider down", retryable=True)
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def card(self) -> Any:
        return self.inner.card

    async def run(self, input: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return await self.inner.run(input, **kwargs)


async def test_an_error_is_tried_again_and_the_step_passes() -> None:
    flaky = Flaky(2)
    wf = ProcessWorkflow("p", [ProcessStep("call", flaky, retry=StepRetry(3, initial=0))])

    result, events = await run_collecting(wf)

    assert result.output == "fine" and flaky.calls == 3
    assert [s for s in steps_of(events) if s[1] == "retrying"] == [
        ("call", "retrying", 1),
        ("call", "retrying", 2),
    ]


async def test_the_error_of_the_last_try_propagates_unchanged() -> None:
    flaky = Flaky(5)
    wf = ProcessWorkflow("p", [ProcessStep("call", flaky, retry=StepRetry(2, initial=0))])

    with pytest.raises(ModelProviderError, match="provider down"):
        await wf.run("go")

    assert flaky.calls == 2


async def test_a_reached_usage_limit_is_never_tried_again() -> None:
    flaky = Flaky(5, UsageLimitExceededError("limit"))
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep(
                "call",
                flaky,
                retry=StepRetry(5, initial=0),
                after_last_try=AfterLastTry("go", "next"),
            ),
            ProcessStep("next", scripted("next", "n")),
        ],
    )

    with pytest.raises(UsageLimitExceededError):
        await wf.run("go")

    assert flaky.calls == 1


async def test_usage_limits_bound_the_tries_of_the_whole_run() -> None:
    agent = reader(0)
    wf = ProcessWorkflow(
        "p", [ProcessStep("extract", agent, checks=(no_total(),), retry=StepRetry(5, initial=0))]
    )

    with pytest.raises(UsageLimitExceededError):
        await wf.run("go", usage_limits=UsageLimits(max_requests=2))

    assert model_of(agent).call_count <= 3


# --------------------------------------------------------------------------- after_last_try


async def test_after_last_try_go_skips_to_the_later_step_without_the_failed_result() -> None:
    target = scripted("archiver", "archived")
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep(
                "extract",
                reader(0),
                checks=(no_total(),),
                retry=StepRetry(2, initial=0),
                after_last_try=AfterLastTry("go", "archive"),
            ),
            ProcessStep("book", scripted("booker", "booked")),
            ProcessStep("archive", target),
        ],
    )

    result, events = await run_collecting(wf, "the request")

    assert result.output == "archived"
    assert [s["status"] for s in result.metadata["steps"]] == ["failed", "skipped", "checked"]
    assert result.metadata["steps"][0]["continued_at"] == "archive"
    assert ("book", "skipped", None) in steps_of(events)
    assert model_of(target).calls[0].messages[-1].content == "the request"


async def test_after_last_try_applies_to_a_failure_the_step_does_not_retry() -> None:
    agent = reader(0)
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep(
                "extract",
                agent,
                checks=(no_total(),),
                retry=StepRetry(3, initial=0, on=frozenset({"error"})),
                after_last_try=AfterLastTry("go", "archive"),
            ),
            ProcessStep("archive", scripted("archiver", "archived")),
        ],
    )

    result = await wf.run("go")

    assert result.output == "archived" and model_of(agent).call_count == 1
    assert result.metadata["steps"][0]["continued_at"] == "archive"


async def test_after_last_try_ask_lets_a_person_continue_with_the_last_result() -> None:
    booker = scripted("booker", "booked")
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep(
                "extract",
                reader(0),
                checks=(no_total(),),
                retry=StepRetry(2, initial=0),
                after_last_try=AfterLastTry("ask"),
            ),
            ProcessStep("book", booker),
        ],
        checkpointer=InMemorySaver(),
    )

    paused = await wf.run("go")
    value = paused.interrupts[0]["value"]
    assert value["after_failure"] is True and "All 2 tries failed" in value["reason"]

    done = await wf.resume(paused.thread_id, {"action": "approve", "reviewer": "ana"})

    assert done.output == "booked" and model_of(booker).call_count == 1
    assert done.metadata["steps"][0]["status"] == "checked"


async def test_after_last_try_ask_rejected_ends_the_run_as_rejected() -> None:
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep(
                "extract", reader(0), checks=(no_total(),), after_last_try=AfterLastTry("ask")
            ),
            ProcessStep("book", scripted("booker", "booked")),
        ],
        checkpointer=InMemorySaver(),
    )
    paused = await wf.run("go")

    with pytest.raises(StepRejectedError) as raised:
        await wf.resume(paused.thread_id, {"action": "reject", "note": "no"})

    assert raised.value.code == "awa.step_rejected"
    assert raised.value.steps[0]["status"] == "rejected"


def test_a_go_backwards_or_to_an_unknown_step_is_refused() -> None:
    first = ProcessStep("first", scripted("a", "x"))
    back = ProcessStep("second", scripted("b", "x"), after_last_try=AfterLastTry("go", "first"))
    with pytest.raises(ValueError, match="later step"):
        ProcessWorkflow("p", [first, back])
    with pytest.raises(ValueError):
        StepRetry(6)


# --------------------------------------------------------------------------- when


async def test_a_step_whose_condition_is_false_is_skipped_and_the_previous_result_carried() -> None:
    checker = scripted("checker", "checked")
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep("extract", reader(100)),
            ProcessStep("board", scripted("board", "x"), when=RunCondition("total >= 5000")),
            ProcessStep("check", checker),
        ],
    )

    result, events = await run_collecting(wf)

    assert ("board", "skipped", None) in steps_of(events)
    assert [s["status"] for s in result.metadata["steps"]] == ["checked", "skipped", "checked"]
    assert '"total":100' in str(model_of(checker).calls[0].messages[-1].content).replace(" ", "")


async def test_a_step_whose_condition_holds_runs() -> None:
    board = scripted("board", "decided")
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep("extract", reader(9000)),
            ProcessStep("board", board, when=RunCondition("total >= 5000")),
        ],
    )

    assert (await wf.run("go")).output == "decided"


async def test_an_unclear_condition_stops_the_run_instead_of_guessing() -> None:
    board = scripted("board", "decided")
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep("extract", reader(9000)),
            ProcessStep("board", board, when=RunCondition("amount >= 5000")),
        ],
    )

    with pytest.raises(StepCheckFailedError, match="cannot be decided"):
        await wf.run("go")

    assert model_of(board).call_count == 0


# --------------------------------------------------------------------------- resume inside a try


def make_gated_reader() -> tuple[Agent, list[str]]:
    sent: list[str] = []

    @tool(approval=True)
    def look_up(number: str) -> str:
        """Look the invoice up in the ledger."""
        sent.append(number)
        return "found"

    agent = scripted(
        "reader",
        invoice(0),  # try 1: fails the rule
        tool_call("look_up", {"number": "R-1"}),  # try 2: pauses on the tool approval
        invoice(250),  # try 2, after the approval
        output_type=Invoice,
        tools=[look_up],
    )
    return agent, sent


async def test_a_pause_on_a_tool_approval_in_try_two_resumes_in_try_two() -> None:
    agent, sent = make_gated_reader()
    wf = ProcessWorkflow(
        "p",
        [ProcessStep("extract", agent, checks=(no_total(),), retry=StepRetry(3, initial=0))],
        checkpointer=InMemorySaver(),
    )

    paused = await wf.run("go")
    assert paused.is_interrupted and sent == []
    assert model_of(agent).call_count == 2

    seen: list[RunEvent] = []
    with use_global_subscriber(seen.append):
        done = await wf.resume(paused.thread_id, {"action": "approve"})

    assert done.output == Invoice(supplier="ACME", total=250)
    assert sent == ["R-1"]
    assert model_of(agent).call_count == 3  # try 1 is not run again
    assert {e.attempt for e in seen if isinstance(e, StepEvent)} == {2}
    assert done.metadata["steps"][0]["attempts"] == 2


# --------------------------------------------------------------------------- fault injection

MAX_MODEL_CALLS_PER_FAILING_STEP = 6
"""Written maximum: 3 tries x 2 model calls (one tool call, one answer)."""

MAX_HTTP_CALLS_PER_FAILING_STEP = 9
"""Written maximum: 3 tries x 3 HTTP requests (the tool's own 2 retries stack on the step's)."""

MAX_SECONDS_PER_FAILING_STEP = 5.0
"""Wall-clock limit for the whole failing run on this machine, waits included."""


async def test_fault_injection_one_failing_step_stays_within_the_call_and_time_budget() -> None:
    requests: list[httpx.Request] = []

    def unreachable(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    @tool(retries=2, retry_backoff=0.01)
    async def fetch_rate(currency: str) -> str:
        """Fetch today's exchange rate."""
        async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
            response = await client.get(f"https://rates.invalid/{currency}")
        return response.text

    agent = scripted(
        "reader",
        *[tool_call("fetch_rate", {"currency": "USD"}), invoice(0)] * 3,
        output_type=Invoice,
        tools=[fetch_rate],
    )
    wf = ProcessWorkflow(
        "p",
        [
            ProcessStep(
                "extract",
                agent,
                checks=(no_total(),),
                retry=StepRetry(3, initial=0.01, max_delay=0.05),
            )
        ],
    )

    started = time.monotonic()
    with pytest.raises(StepCheckFailedError):
        await wf.run("go")
    elapsed = time.monotonic() - started

    assert model_of(agent).call_count <= MAX_MODEL_CALLS_PER_FAILING_STEP
    assert len(requests) <= MAX_HTTP_CALLS_PER_FAILING_STEP
    assert model_of(agent).call_count == 6 and len(requests) == 9  # the budget is exact
    assert elapsed < MAX_SECONDS_PER_FAILING_STEP
