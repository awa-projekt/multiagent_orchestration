"""Output validators with one retry budget: schema errors, `ModelRetry` and retry guardrails."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from awa.agents import Agent, ModelRetry
from awa.contracts import OutputRetryEvent, RunEvent
from awa.errors import MaxStepsExceededError, OutputRejectedError, StructuredOutputError
from awa.guardrails import MustIncludeGuardrail
from awa.kernel.output import RETRY_KEY, OutputRejected, OutputValidatorMiddleware
from awa.runtime import RunContext
from awa.testing import FakeChatModel, structured_output, tool_call
from awa.tools import create_tool_approval_wrapper


class Answer(BaseModel):
    value: int
    reason: str = ""


def positive(answer: Answer) -> None:
    if answer.value <= 0:
        raise ModelRetry("value must be positive")


def _answers(*values: int) -> FakeChatModel:
    return FakeChatModel([structured_output(Answer, {"value": v}) for v in values])


def _native_json_model(*answers: str) -> FakeChatModel:
    return FakeChatModel(
        [AIMessage(content=text) for text in answers], profile={"structured_output": True}
    )


def _retries(events: list[RunEvent]) -> list[OutputRetryEvent]:
    return [e for e in events if isinstance(e, OutputRetryEvent)]


async def _state(agent: Agent[Any, Any], thread_id: str) -> dict[str, Any]:
    snapshot = await agent._compiled_graph(None).aget_state(
        {"configurable": {"thread_id": thread_id}}
    )
    return dict(snapshot.values)


async def test_bad_then_good_output_retries_once() -> None:
    events: list[RunEvent] = []
    model = _answers(0, 5)
    agent = Agent(
        "a",
        model=model,
        output_type=Answer,
        output_validators=[positive],
        checkpointer=InMemorySaver(),
        hooks=[events.append],
    )

    result = await agent.run("go", thread_id="t")

    assert result.output == Answer(value=5)
    assert len(model.calls) == 2
    assert (await _state(agent, "t"))[RETRY_KEY] == 1
    [retry] = _retries(events)
    assert (retry.kind, retry.attempt, retry.max_retries, retry.check) == (
        "validator",
        1,
        2,
        "positive",
    )
    feedback = model.calls[1].messages[-1]
    assert isinstance(feedback, HumanMessage) and "value must be positive" in str(feedback.content)
    assert "value=0" not in str(feedback.content)  # the feedback quotes reasons, not the output


async def test_validator_exhaustion_raises_output_rejected() -> None:
    model = _answers(0, 0, 0)
    agent = Agent("a", model=model, output_type=Answer, output_validators=[positive])

    with pytest.raises(OutputRejectedError) as caught:
        await agent.run("go")

    assert len(model.calls) == 3  # one answer and output_retries=2 retries
    assert caught.value.problems == ["value must be positive"]
    assert caught.value.attempts == 2
    assert caught.value.code == "awa.output_rejected"


async def test_validator_reads_deps_for_a_data_check() -> None:
    def known(answer: Answer, ctx: RunContext[set[int]]) -> None:
        if answer.value not in ctx.deps:
            raise ModelRetry(f"{answer.value} is not one of the known values")

    model = _answers(9, 3)
    agent = Agent("a", model=model, output_type=Answer, output_validators=[known])

    result = await agent.run("go", deps={1, 2, 3})

    assert result.output.value == 3 and len(model.calls) == 2


async def test_async_validator_and_normalising_return() -> None:
    async def clean(answer: Answer) -> Answer:
        return answer.model_copy(update={"reason": answer.reason.strip().upper()})

    model = FakeChatModel([structured_output(Answer, {"value": 1, "reason": " fine "})])
    agent = Agent("a", model=model, output_type=Answer, output_validators=[clean])

    result = await agent.run("go")

    assert result.output == Answer(value=1, reason="FINE")
    assert len(model.calls) == 1


async def test_a_validator_checks_the_text_of_an_agent_without_output_type() -> None:
    def short(text: str) -> str:
        if len(text) > 5:
            raise ModelRetry("answer in at most five characters")
        return text.upper()

    agent = Agent("a", model=FakeChatModel(["far too long", "ok"]), output_validators=[short])

    result = await agent.run("go")

    assert result.output == "OK" and result.text == "OK"


async def test_retry_budget_resets_on_new_input_and_survives_resume() -> None:
    model = _answers(0, 5, 0, 7)
    agent = Agent(
        "a",
        model=model,
        output_type=Answer,
        output_validators=[positive],
        output_retries=1,
        checkpointer=InMemorySaver(),
    )

    first = await agent.run("one", thread_id="t")
    second = await agent.run("two", thread_id="t")  # needs its own retry again

    assert (first.output.value, second.output.value) == (5, 7)
    assert len(model.calls) == 4

    @tool
    def refund(amount: int) -> str:
        """Refund an amount."""
        return f"refunded {amount}"

    saver = InMemorySaver()

    def build(model: FakeChatModel) -> Agent[Any, Any]:
        return Agent(
            "b",
            model=model,
            output_type=Answer,
            output_validators=[positive],
            output_retries=1,
            tools=[create_tool_approval_wrapper(refund)],
            checkpointer=saver,
        )

    paused = await build(
        FakeChatModel([structured_output(Answer, {"value": 0}), tool_call("refund", {"amount": 5})])
    ).run("go")
    assert paused.is_interrupted and paused.thread_id is not None
    assert (await _state(build(_answers(1)), paused.thread_id))[RETRY_KEY] == 1

    after = _answers(0)
    with pytest.raises(OutputRejectedError):  # the one retry was used before the pause
        await build(after).resume(paused.thread_id, {"action": "approve"})
    assert len(after.calls) == 1


async def test_provider_mode_schema_repair_counts_in_shared_budget() -> None:
    events: list[RunEvent] = []
    model = _native_json_model('{"reason": "no value"}', '{"value": 0}')
    agent = Agent(
        "a",
        model=model,
        output_type=Answer,
        output_validators=[positive],
        output_retries=1,
        hooks=[events.append],
    )

    with pytest.raises(OutputRejectedError):  # schema repair used the one retry
        await agent.run("go")

    assert len(model.calls) == 2
    [retry] = _retries(events)
    assert (retry.kind, retry.check) == ("schema", "schema")


async def test_provider_mode_schema_repair_succeeds_inside_the_budget() -> None:
    model = _native_json_model('{"reason": "no value"}', '{"value": 4}')
    agent = Agent("a", model=model, output_type=Answer, checkpointer=InMemorySaver())

    result = await agent.run("go", thread_id="t")

    assert result.output == Answer(value=4)
    assert (await _state(agent, "t"))[RETRY_KEY] == 1
    assert len(result.messages) == 2  # the repair turn is not kept in the history


async def test_tool_mode_schema_repair_is_bounded() -> None:
    bad = structured_output(Answer, {"reason": "no value"})
    model = FakeChatModel([bad, bad, bad])
    agent = Agent("a", model=model, output_type=Answer, output_retries=2)

    with pytest.raises(StructuredOutputError, match="does not match its output type"):
        await agent.run("go")

    assert len(model.calls) == 3


async def test_output_retries_zero_turns_schema_repair_off() -> None:
    bad = structured_output(Answer, {"reason": "no value"})
    model = FakeChatModel([bad])
    agent = Agent("a", model=model, output_type=Answer, output_retries=0)

    with pytest.raises(StructuredOutputError):
        await agent.run("go")

    assert len(model.calls) == 1


async def test_retries_count_in_max_steps() -> None:
    model = _answers(0, 0)
    agent = Agent(
        "a",
        model=model,
        output_type=Answer,
        output_validators=[positive],
        output_retries=5,
        max_steps=2,
    )

    with pytest.raises(MaxStepsExceededError):
        await agent.run("go")

    assert len(model.calls) == 2


async def test_retry_cost_is_in_usage() -> None:
    model = _answers(0, 5)
    agent = Agent("a", model=model, output_type=Answer, output_validators=[positive])

    result = await agent.run("go")

    assert result.usage.requests == len(model.calls) == 2


async def test_model_retry_in_tool_becomes_error_message() -> None:
    @tool
    def lookup(key: str) -> str:
        """Look a value up."""
        if key != "good":
            raise ModelRetry("unknown key; use the key 'good'")
        return "found"

    model = FakeChatModel([tool_call("lookup", {"key": "bad"}), "done"])
    agent = Agent("a", model=model, tools=[lookup])

    result = await agent.run("go")

    [message] = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert message.status == "error"
    assert message.content == "unknown key; use the key 'good'"
    assert result.output == "done"


async def test_retry_clears_stale_structured_response() -> None:
    model = FakeChatModel([structured_output(Answer, {"value": 0}), "plain text, no answer"])
    agent = Agent("a", model=model, output_type=Answer, output_validators=[positive])

    with pytest.raises(StructuredOutputError, match="without returning"):
        await agent.run("go")

    assert len(model.calls) == 2


async def test_tool_mode_parallel_structured_errors_count_once() -> None:
    both = [tool_call("Answer", {"value": 1}, id="c1"), tool_call("Answer", {"value": 2}, id="c2")]
    model = FakeChatModel([both, structured_output(Answer, {"value": 3})])
    agent = Agent("a", model=model, output_type=Answer, output_retries=1)

    result = await agent.run("go")

    assert result.output.value == 3 and len(model.calls) == 2


async def test_retry_guardrail_and_validator_share_one_budget() -> None:
    model = FakeChatModel(["no marker", "STATUS: too long for the validator"])

    def short(text: str) -> None:
        if len(text) > 12:
            raise ModelRetry("keep it short")

    agent = Agent(
        "a",
        model=model,
        output_guardrails=[MustIncludeGuardrail(["STATUS:"])],
        output_validators=[short],
        output_retries=1,
    )

    with pytest.raises(OutputRejectedError, match="keep it short"):
        await agent.run("go")

    assert len(model.calls) == 2


async def test_all_validators_run_so_the_model_hears_every_problem() -> None:
    def first(text: str) -> None:
        raise ModelRetry("first problem")

    def second(text: str) -> None:
        raise ModelRetry("second problem")

    model = FakeChatModel(["a", "b"])
    agent = Agent("a", model=model, output_validators=[first, second], output_retries=1)

    with pytest.raises(OutputRejectedError) as caught:
        await agent.run("go")

    feedback = str(model.calls[1].messages[-1].content)
    assert "first problem" in feedback and "second problem" in feedback
    assert caught.value.problems == ["first problem", "second problem"]


def test_output_retries_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="output_retries"):
        OutputValidatorMiddleware([], max_retries=-1)


def test_clone_keeps_the_validators() -> None:
    agent = Agent("a", model=FakeChatModel(["x"]), output_validators=[positive], output_retries=1)

    clone = agent.clone(description="copy")

    assert clone._output_validators == (positive,)
    assert clone._output_retries == 1


async def test_the_middleware_raises_output_rejected_without_awa() -> None:
    def never(output: Any) -> None:
        raise ModelRetry("no")

    middleware = OutputValidatorMiddleware([never], max_retries=0)
    state = {"messages": [AIMessage("x", id="m1")]}

    with pytest.raises(OutputRejected) as caught:
        await middleware.aafter_agent(state, None)

    assert caught.value.problems == ["no"] and caught.value.kind == "validator"
