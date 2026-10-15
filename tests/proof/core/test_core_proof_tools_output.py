"""Core proof, scenarios 6 and 7: tool errors (handled, retried, reported) and structured output."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from _helpers.proof_helpers import delegate, model_of, of_type, scripted, tool_results
from awa import tool
from awa.agents import Agent
from awa.contracts import ErrorEvent, RunEvent, ToolCallEvent, ToolResultEvent
from awa.errors import StructuredOutputError
from awa.team import Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call

# ------------------------------------------------------------------ scenario 6: tool errors


def run_with(agent_tools: list[Any], *script: Any) -> tuple[Agent, list[RunEvent]]:
    events: list[RunEvent] = []
    agent = Agent(
        "worker", model=FakeChatModel(list(script)), tools=agent_tools, hooks=[events.append]
    )
    return agent, events


async def test_s6_a_failing_tool_is_reported_to_the_model_which_retries_and_succeeds() -> None:
    """Claim: an exception inside a tool does not end the run; the model receives the error as a
    flagged tool result, calls the tool again with other arguments and the run succeeds.
    Measured: 2 executions, 3 model calls, 1 error result and 1 good result."""

    attempts: list[str] = []

    @tool
    def lookup(order: str) -> str:
        """Look an order up."""

        attempts.append(order)
        if not order.startswith("A-"):
            raise ValueError(f"unknown order format {order!r}; use A-<number>")
        return f"{order}: shipped"

    agent, events = run_with(
        [lookup],
        tool_call("lookup", {"order": "7"}),
        tool_call("lookup", {"order": "A-7"}),
        "Order A-7 is shipped.",
    )

    result = await agent.run("Where is order 7?")

    assert result.output == "Order A-7 is shipped."
    assert attempts == ["7", "A-7"]
    first_reply = model_of(agent).calls[1].messages[-1]
    assert isinstance(first_reply, ToolMessage) and "unknown order format" in str(
        first_reply.content
    )
    outcomes = [e.is_error for e in of_type(events, ToolResultEvent)]
    assert outcomes == [True, False]
    assert result.usage.requests == 3


async def test_s6_arguments_that_do_not_fit_the_schema_are_reported_and_the_tool_never_runs() -> (
    None
):
    """Claim: a call with arguments of the wrong type is refused before the tool body runs; the
    model sees the validation error and can correct itself."""

    ran: list[float] = []

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers."""

        ran.append(a + b)
        return a + b

    agent, events = run_with(
        [add], tool_call("add", {"a": "many", "b": 1}), tool_call("add", {"a": 2, "b": 1}), "3"
    )

    result = await agent.run("add")

    assert ran == [3]  # only the corrected call ran the body
    assert [e.is_error for e in of_type(events, ToolResultEvent)] == [True, False]
    assert result.output == "3"


async def test_s6_transient_errors_are_retried_by_the_tool_and_the_model_sees_one_result() -> None:
    """Claim: tool(retries=2) repeats a ConnectionError; 3 attempts (2 failures, 1 success) happen
    inside one tool call, the model sees only the success; a ValueError is not retried."""

    attempts: list[int] = []

    @tool(retries=2, retry_backoff=0)
    def flaky() -> str:
        """Fetch something over an unreliable link."""

        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("link reset")
        return "payload"

    agent, events = run_with([flaky], tool_call("flaky"), "got payload")

    result = await agent.run("go")

    assert len(attempts) == 3
    assert len(of_type(events, ToolCallEvent)) == 1
    assert [e.is_error for e in of_type(events, ToolResultEvent)] == [False]
    assert result.output == "got payload" and result.usage.requests == 2

    permanent: list[int] = []

    @tool(retries=3, retry_backoff=0)
    def wrong() -> str:
        """Always rejects its input."""

        permanent.append(1)
        raise ValueError("bad input")

    agent2, _ = run_with([wrong], tool_call("wrong"), "could not do it")
    await agent2.run("go")
    assert len(permanent) == 1


async def test_s6_a_tool_that_always_fails_is_reported_and_the_agent_can_say_so() -> None:
    """Claim: when the tool keeps failing, the run ends normally with the model's explanation; the
    failure is visible in the events and the final answer, not hidden."""

    @tool
    def broken() -> str:
        """Always fails."""

        raise RuntimeError("backend is down")

    agent, events = run_with(
        [broken], tool_call("broken"), "I could not finish: the backend is down."
    )

    result = await agent.run("go")

    [outcome] = of_type(events, ToolResultEvent)
    assert outcome.is_error and "backend is down" in outcome.content
    assert "backend is down" in str(result.output)


async def test_s6_a_slow_tool_is_stopped_by_its_timeout_and_reported() -> None:
    """Claim: tool(timeout=...) ends a hanging tool call; the model gets an error result naming
    the timeout and the run continues."""

    @tool(timeout=0.05)
    async def slow() -> str:
        """Hangs."""

        await asyncio.sleep(30)
        return "never"

    agent, events = run_with([slow], tool_call("slow"), "it timed out")

    result = await agent.run("go")

    [outcome] = of_type(events, ToolResultEvent)
    assert outcome.is_error and "Timeout" in outcome.content
    assert result.output == "it timed out"


async def test_s6_a_member_whose_model_fails_is_reported_to_the_lead_as_an_error_result() -> None:
    """Claim: when a delegated member fails, the lead receives status="error" as tool text and can
    still give its own final answer; the run does not crash."""

    def explode(call: ModelCall) -> Any:
        raise RuntimeError("provider outage")

    member = Agent("worker", description="Works.", model=FunctionChatModel(explode))
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=scripted("lead", delegate("worker"), "The worker failed; no result."),
        members=[member],
        hooks=[events.append],
    )

    result = await team.run("go")

    [report] = tool_results(result)
    assert 'agent="worker" status="error"' in report
    assert result.output == "The worker failed; no result."
    assert [e.agent for e in of_type(events, ErrorEvent)] == ["worker"]


# ------------------------------------------------------------------ scenario 7: structured output


class Triage(BaseModel):
    category: Literal["billing", "delivery", "other"]
    urgent: bool


async def test_s7_structured_output_is_a_validated_instance() -> None:
    """Claim: with output_type the result is an instance of the type with the model's values."""

    model = FakeChatModel([structured_output(Triage, {"category": "delivery", "urgent": True})])
    agent = Agent("triage", model=model, output_type=Triage)

    result = await agent.run("My parcel is late!")

    assert result.output == Triage(category="delivery", urgent=True)
    assert agent.card.output_schema is not None
    assert agent.card.output_schema["properties"]["category"]["enum"] == [
        "billing",
        "delivery",
        "other",
    ]


async def test_s7_an_invalid_value_is_sent_back_and_a_corrected_answer_is_accepted() -> None:
    """Claim: a value outside the schema is not accepted; the model gets the validation error and
    its corrected answer is the result. Measured: 2 model calls."""

    model = FakeChatModel(
        [
            structured_output(Triage, {"category": "shipping", "urgent": True}),
            structured_output(Triage, {"category": "delivery", "urgent": True}),
        ]
    )
    agent = Agent("triage", model=model, output_type=Triage)

    result = await agent.run("go")

    assert result.output.category == "delivery"
    assert model.call_count == 2
    complaint = model.calls[1].messages[-1]
    assert isinstance(complaint, ToolMessage) and "category" in str(complaint.content)


async def test_s7_no_structured_answer_raises_structured_output_error() -> None:
    """Claim: a model that answers in plain text never produces an output; the run raises
    StructuredOutputError instead of returning text as if it were typed."""

    agent = Agent(
        "triage", model=FakeChatModel([AIMessage(content="plain text")]), output_type=Triage
    )

    with pytest.raises(StructuredOutputError, match="Triage"):
        await agent.run("go")


async def test_s7_native_json_output_is_repaired_and_given_up_after_three_bad_answers() -> None:
    """Claim: with a provider-native JSON mode an answer that misses a field is sent back for
    repair (and counted in the usage); three bad answers raise StructuredOutputError."""

    def native(*answers: str) -> FakeChatModel:
        return FakeChatModel(
            [AIMessage(content=a) for a in answers], profile={"structured_output": True}
        )

    repaired = native('{"urgent": true}', '{"category": "billing", "urgent": true}')
    result = await Agent("t", model=repaired, output_type=Triage).run("go")
    assert result.output == Triage(category="billing", urgent=True)
    assert result.usage.requests == 2

    hopeless = native('{"urgent": 1}', '{"urgent": 1}', '{"urgent": 1}')
    with pytest.raises(StructuredOutputError):
        await Agent("t", model=hopeless, output_type=Triage).run("go")
    assert hopeless.call_count == 3


async def test_s7_a_member_with_typed_output_returns_it_to_the_lead_as_json() -> None:
    """Claim: a delegated member with output_type hands its validated result back as JSON text in
    the framed result, and the lead's own typed answer is validated as well."""

    classifier = Agent(
        "classifier",
        description="Classifies tickets.",
        model=FakeChatModel([structured_output(Triage, {"category": "billing", "urgent": False})]),
        output_type=Triage,
    )
    lead = Agent(
        "lead",
        description="Coordinates.",
        model=FakeChatModel(
            [
                delegate("classifier"),
                structured_output(Triage, {"category": "billing", "urgent": False}),
            ]
        ),
        output_type=Triage,
    )
    team = Team("desk", orchestrator=lead, members=[classifier])

    result = await team.run("Invoice question")

    report = tool_results(result)[0]
    assert '"category":"billing"' in report.replace(" ", "")
    assert result.output == Triage(category="billing", urgent=False)
