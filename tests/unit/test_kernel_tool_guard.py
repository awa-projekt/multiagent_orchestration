"""`RepairToolCallsMiddleware` and context editing of older tool results."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from _helpers.polling import TIMEOUT_FACTOR
from awa.agents import Agent
from awa.contracts.events import ModelResponseEvent
from awa.core.settings import get_settings
from awa.errors import RunTimeoutError
from awa.kernel.tool_guard import RepairToolCallsMiddleware, unanswered_tool_calls
from awa.memory.context_editing import (
    PLACEHOLDER,
    PRE_CLEAR_NOTE,
    ContextEditing,
    _SteppedClear,
    cleared_tool_results,
)
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools.approval import create_tool_approval_wrapper


@tool
async def hang() -> str:
    """Wait for a very long time."""
    await asyncio.sleep(30)
    return "never"


def _ai(*calls: tuple[str, str], id: str = "ai") -> AIMessage:
    return AIMessage(
        content="", id=id, tool_calls=[{"name": n, "args": {}, "id": c} for n, c in calls]
    )


def _pairing_model(seen: list[list[BaseMessage]]) -> FunctionChatModel:
    """A model that refuses a history with an unanswered tool call, as a provider does."""

    def respond(call: ModelCall) -> str:
        seen.append(list(call.messages))
        answered = {m.tool_call_id for m in call.messages if isinstance(m, ToolMessage)}
        for message in call.messages:
            if isinstance(message, AIMessage):
                for pending in message.tool_calls:
                    assert pending["id"] in answered, "HTTP 400: tool call without a result"
        return "ok"

    return FunctionChatModel(respond)


def test_unanswered_tool_calls_counts_calls_without_a_tool_message() -> None:
    messages = [
        HumanMessage("go"),
        _ai(("a", "c1"), ("b", "c2")),
        ToolMessage("r", tool_call_id="c1"),
    ]

    assert unanswered_tool_calls(messages) == 1
    assert unanswered_tool_calls(messages[:1]) == 0


async def test_the_repair_keeps_every_original_message_and_adds_error_messages() -> None:
    middleware = RepairToolCallsMiddleware()
    first, second = _ai(("a", "c1"), ("b", "c2"), id="one"), HumanMessage("again", id="h2")
    state = {"messages": [HumanMessage("go", id="h1"), first, second]}

    update = await middleware.abefore_agent(state, None)

    assert update is not None
    remove, *patched = update["messages"]
    assert isinstance(remove, RemoveMessage) and remove.id == REMOVE_ALL_MESSAGES
    assert [m.id for m in patched if not isinstance(m, ToolMessage)] == ["h1", "one", "h2"]
    assert patched[1] is first and patched[-1] is second
    synthetic = [m for m in patched if isinstance(m, ToolMessage)]
    assert [(m.tool_call_id, m.name, m.status) for m in synthetic] == [
        ("c1", "a", "error"),
        ("c2", "b", "error"),
    ]
    assert "did not complete" in str(synthetic[0].content) and "c1" in str(synthetic[0].content)
    assert patched[2:4] == synthetic


async def test_an_answered_thread_is_untouched() -> None:
    middleware = RepairToolCallsMiddleware()
    state = {
        "messages": [HumanMessage("go"), _ai(("a", "c1")), ToolMessage("r", tool_call_id="c1")]
    }

    assert await middleware.abefore_agent(state, None) is None
    assert await middleware.abefore_agent({"messages": []}, None) is None


async def test_dangling_call_after_cancel_is_answered() -> None:
    saver = InMemorySaver()
    crashed = Agent(
        "worker",
        model=FakeChatModel([tool_call("hang", {}, id="call_1")]),
        tools=[hang],
        checkpointer=saver,
    )
    with pytest.raises(RunTimeoutError):
        await crashed.run("go", thread_id="t1", timeout=3.0 * TIMEOUT_FACTOR)
    before = (
        await crashed._compiled_graph(None).aget_state({"configurable": {"thread_id": "t1"}})
    ).values["messages"]
    assert unanswered_tool_calls(before) == 1

    seen: list[list[BaseMessage]] = []
    again = Agent("worker", model=_pairing_model(seen), tools=[hang], checkpointer=saver)
    result = await again.run("try again", thread_id="t1")

    assert result.output == "ok"
    [history] = seen
    assert [m.id for m in history if m.id in {b.id for b in before}] == [m.id for m in before]
    added = [m for m in history if m.id not in {b.id for b in before}]
    assert [type(m).__name__ for m in added] == ["ToolMessage", "HumanMessage"]
    assert isinstance(added[0], ToolMessage) and added[0].status == "error"
    assert added[0].tool_call_id == "call_1"


async def test_new_messages_start_skips_repaired_messages() -> None:
    saver = InMemorySaver()
    crashed = Agent(
        "worker", model=FakeChatModel([tool_call("hang")]), tools=[hang], checkpointer=saver
    )
    with pytest.raises(RunTimeoutError):
        await crashed.run("go", thread_id="t2", timeout=3.0 * TIMEOUT_FACTOR)

    again = Agent("worker", model=_pairing_model([]), tools=[hang], checkpointer=saver)
    result = await again.run("try again", thread_id="t2")

    first_new = result.messages[result.new_messages_start]
    assert isinstance(first_new, HumanMessage) and first_new.content == "try again"
    assert len(result.new_messages()) == 2


@tool
def transfer(amount: int) -> str:
    """Send money."""
    return f"sent {amount}"


async def test_resume_is_not_repaired() -> None:
    seen: list[list[BaseMessage]] = []

    def respond(call: ModelCall) -> Any:
        seen.append(list(call.messages))
        if len(seen) == 1:
            return tool_call("transfer", {"amount": 5})
        return "Done."

    agent = Agent(
        "payer",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(transfer)],
        checkpointer=InMemorySaver(),
    )
    paused = await agent.run("pay")
    assert paused.is_interrupted and paused.thread_id is not None

    final = await agent.resume(paused.thread_id, {"action": "approve"})

    assert final.output == "Done."
    tool_results = [m for m in final.messages if isinstance(m, ToolMessage)]
    assert [m.status for m in tool_results] == ["success"]
    assert "did not complete" not in str(tool_results[0].content)


def _conversation(results: int) -> list[Any]:
    messages: list[Any] = [HumanMessage("go")]
    for n in range(results):
        call = {"name": "big", "args": {"n": n}, "id": f"c{n}", "type": "tool_call"}
        messages += [AIMessage("", tool_calls=[call]), ToolMessage("x" * 400, tool_call_id=f"c{n}")]
    return messages


def _cleared(results: int, **config: Any) -> int:
    messages = _conversation(results)
    _SteppedClear(ContextEditing(trigger=10, **config)).apply(
        messages, count_tokens=lambda ms: sum(len(str(m.content)) for m in ms) // 4
    )
    return cleared_tool_results(messages)


def test_results_are_cleared_in_steps_so_the_prefix_stays_stable() -> None:
    assert [_cleared(n, keep=2, step=4) for n in range(1, 12)] == [0, 0, 0, 0, 0, 4, 4, 4, 4, 8, 8]


def test_nothing_is_cleared_below_the_trigger() -> None:
    messages = _conversation(10)
    _SteppedClear(ContextEditing(trigger=10**6)).apply(messages, count_tokens=lambda ms: 1000)
    assert cleared_tool_results(messages) == 0


def test_bad_values_are_refused() -> None:
    with pytest.raises(ValueError):
        ContextEditing(step=0)


@pytest.fixture
def small_trigger() -> Any:
    os.environ.update(AWA_CONTEXT_EDITING_TRIGGER="50", AWA_CONTEXT_EDITING_KEEP="1")
    os.environ["AWA_CONTEXT_EDITING_STEP"] = "1"
    os.environ["AWA_CONTEXT_EDITING_CLEAR_AT_LEAST"] = "0"
    get_settings.cache_clear()
    yield
    for name in ("TRIGGER", "KEEP", "STEP", "CLEAR_AT_LEAST"):
        os.environ.pop(f"AWA_CONTEXT_EDITING_{name}", None)
    get_settings.cache_clear()


def test_an_agent_sends_placeholders_but_keeps_every_result(small_trigger: None) -> None:
    @tool
    def big(n: int) -> str:
        """A large result."""

        return f"result {n} " + "y" * 400

    def script() -> FakeChatModel:
        return FakeChatModel([tool_call("big", {"n": n}) for n in range(3)] + ["done"])

    async def collect() -> list[Any]:
        return [e async for e in Agent("a", model=script(), tools=[big]).stream("go")]

    events = asyncio.run(collect())
    model = script()
    result = asyncio.run(Agent("a", model=model, tools=[big]).run("go"))
    last = model.calls[-1].messages
    assert [m.content.startswith(PLACEHOLDER) for m in last if isinstance(m, ToolMessage)] == [
        True,
        True,
        False,
    ]
    assert last[2].content == f'{PLACEHOLDER} Call: big {{"n": 0}}'  # names the call
    kept = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert all(m.content.startswith("result") for m in kept)  # state is untouched
    responses = [e for e in events if isinstance(e, ModelResponseEvent)]
    assert responses[-1].cleared_tool_results == 2


def test_context_editing_can_be_switched_off(small_trigger: None) -> None:
    os.environ["AWA_CONTEXT_EDITING"] = "false"
    get_settings.cache_clear()
    try:

        @tool
        def big(n: int) -> str:
            """A large result."""

            return "y" * 400

        model = FakeChatModel([tool_call("big", {"n": n}) for n in range(3)] + ["done"])
        asyncio.run(Agent("a", model=model, tools=[big]).run("go"))
        assert cleared_tool_results(model.calls[-1].messages) == 0
    finally:
        os.environ.pop("AWA_CONTEXT_EDITING", None)


def _apply(messages: list[Any], **config: Any) -> list[Any]:
    _SteppedClear(ContextEditing(**config)).apply(
        messages, count_tokens=lambda ms: sum(len(str(m.content)) for m in ms) // 4
    )
    return messages


def test_cleared_results_name_the_tool_and_arguments() -> None:
    messages = _apply(_conversation(6), trigger=10, keep=2, step=4)
    stubs = [m.content for m in messages if isinstance(m, ToolMessage)][:4]
    assert stubs == [f'{PLACEHOLDER} Call: big {{"n": {n}}}' for n in range(4)]


def test_a_step_freeing_too_little_clears_nothing() -> None:
    # Four cleared results free about 230 tokens: with 1,000 required nothing is cleared.
    assert (
        cleared_tool_results(_apply(_conversation(6), trigger=10, keep=2, clear_at_least=1_000))
        == 0
    )
    assert (
        cleared_tool_results(_apply(_conversation(6), trigger=10, keep=2, clear_at_least=150)) == 4
    )


def test_a_note_asks_to_record_facts_before_the_first_clearing() -> None:
    # 3 results, keep 4: nothing clearable yet; about 300 tokens, trigger 350 (80 %: 280).
    messages = _apply(_conversation(5), trigger=550, keep=4)
    assert messages[-1].content.endswith(PRE_CLEAR_NOTE)
    assert cleared_tool_results(messages) == 0
    far = _apply(_conversation(5), trigger=10_000, keep=4)
    assert not far[-1].content.endswith(PRE_CLEAR_NOTE)


def test_the_profile_sets_the_defaults() -> None:
    from awa.memory.context_editing import from_settings

    cost = from_settings()
    assert cost is not None and (cost.trigger, cost.keep, cost.clear_at_least) == (10_000, 3, 3_000)
    os.environ["AWA_TOOL_PROFILE"] = "quality"
    get_settings.cache_clear()
    try:
        quality = from_settings()
        assert quality is not None and (quality.trigger, quality.keep) == (40_000, 4)
        assert quality.clear_at_least == 0
    finally:
        os.environ.pop("AWA_TOOL_PROFILE", None)
        get_settings.cache_clear()
