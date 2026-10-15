"""`awa.testing`: fake models and `ToolContract`."""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from pydantic import BaseModel

from awa.contracts.events import ApprovalDecidedEvent
from awa.runtime.context import RunContext
from awa.testing import (
    FakeChatModel,
    FunctionChatModel,
    ModelCall,
    ToolApprovalRequested,
    ToolContract,
    expect_scripts_consumed,
    structured_output,
    tool_call,
)
from awa.tools import tool


@lc_tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


class Answer(BaseModel):
    value: int


def test_fake_model_requires_a_script() -> None:
    with pytest.raises(ValueError):
        FakeChatModel([])


async def test_fake_model_drives_a_real_tool_loop() -> None:
    model = FakeChatModel([tool_call("add", {"a": 2, "b": 3}), "The sum is 5."])
    agent = create_agent(model, tools=[add])
    result = await agent.ainvoke({"messages": [HumanMessage("2+3?")]})
    kinds = [type(m) for m in result["messages"]]
    assert kinds == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert result["messages"][2].content == "5"
    assert result["messages"][-1].content == "The sum is 5."
    assert model.call_count == 2
    assert model.remaining == 0
    assert model.calls[0].tool_names == ("add",)
    assert isinstance(model.calls[1].last_message, ToolMessage)


async def test_fake_model_supports_parallel_tool_calls() -> None:
    model = FakeChatModel(
        [[tool_call("add", {"a": 1, "b": 1}), tool_call("add", {"a": 2, "b": 2})], "done"]
    )
    agent = create_agent(model, tools=[add])
    result = await agent.ainvoke({"messages": [HumanMessage("go")]})
    tool_results = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
    assert sorted(tool_results) == ["2", "4"]


async def test_fake_model_structured_output() -> None:
    model = FakeChatModel([structured_output(Answer, {"value": 42})])
    agent = create_agent(model, tools=[], response_format=Answer)
    result = await agent.ainvoke({"messages": [HumanMessage("answer?")]})
    assert result["structured_response"] == Answer(value=42)


async def test_fake_model_attaches_usage_metadata_but_keeps_explicit_values() -> None:
    explicit = AIMessage(
        content="x", usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    )
    model = FakeChatModel(["a longer answer text", explicit])
    first = await model.ainvoke("prompt text")
    second = await model.ainvoke("prompt")
    assert first.usage_metadata is not None
    assert first.usage_metadata["input_tokens"] > 0
    assert first.usage_metadata["output_tokens"] > 0
    assert second.usage_metadata == explicit.usage_metadata


async def test_exhausted_script_fails_loudly_or_repeats() -> None:
    strict = FakeChatModel(["only"])
    await strict.ainvoke("one")
    with pytest.raises(AssertionError, match="script exhausted"):
        await strict.ainvoke("two")

    lenient = FakeChatModel(["only"], repeat_last=True)
    await lenient.ainvoke("one")
    assert (await lenient.ainvoke("two")).content == "only"


async def test_callable_script_items_see_the_call() -> None:
    model = FakeChatModel([lambda call: f"call #{call.index}: {call.last_message.content}"])
    assert (await model.ainvoke("hello")).content == "call #0: hello"


async def test_function_model_computes_each_response() -> None:
    def respond(call: ModelCall) -> object:
        if not any(isinstance(m, ToolMessage) for m in call.messages):
            return tool_call("add", {"a": 20, "b": 22})
        return f"result {call.messages[-1].content}"

    model = FunctionChatModel(respond)
    agent = create_agent(model, tools=[add])
    result = await agent.ainvoke({"messages": [HumanMessage("go")]})
    assert result["messages"][-1].content == "result 42"
    assert model.call_count == 2


async def test_unsupported_script_item_raises_type_error() -> None:
    model = FakeChatModel([123])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="Unsupported fake model response"):
        await model.ainvoke("x")


def test_sync_invoke_is_supported() -> None:
    assert FakeChatModel(["sync"]).invoke("x").content == "sync"


async def test_stream_chunk_size_streams_text_in_chunks() -> None:
    model = FakeChatModel(["abcdefgh"], stream_chunk_size=3)
    chunks = [chunk.content async for chunk in model.astream("x")]
    assert chunks[:3] == ["abc", "def", "gh"]
    assert "".join(str(c) for c in chunks) == "abcdefgh"


async def test_a_streamed_response_reports_the_model_name_like_an_invoked_one() -> None:
    model = FakeChatModel(["abcdefgh"], model_name="m-1", stream_chunk_size=3)
    chunks = [chunk async for chunk in model.astream("x")]
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged += chunk
    assert merged.response_metadata["model_name"] == "m-1"
    assert merged.usage_metadata is not None


async def test_streaming_is_disabled_without_chunk_size() -> None:
    model = FakeChatModel(["abcdefgh"])
    chunks = [chunk.content async for chunk in model.astream("x")]
    assert chunks == ["abcdefgh"]


def test_stream_chunk_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="stream_chunk_size"):
        FakeChatModel(["x"], stream_chunk_size=0)


@tool(approval=lambda args: args["amount"] > 100, risk=["external_action"], timeout=5)
async def refund(
    ctx: RunContext[dict[str, Any]], order_id: str, amount: float, note: str = ""
) -> str:
    """Refund an order.

    Args:
        order_id: The order to refund.
        amount: Amount in EUR.
        note: Optional note.
    """
    if order_id == "missing":
        raise KeyError("unknown order")
    return f"refunded {amount} ({ctx.deps['currency']}, {ctx.metadata.get('awa.tenant')})"


CONTRACT = ToolContract(refund, deps={"currency": "EUR"}, metadata={"awa.tenant": "acme"})


def test_schema_and_flag_assertions_chain() -> None:
    (
        CONTRACT.expect_args("order_id", "amount", optional=["note"])
        .expect_hidden("ctx")
        .expect_description("Refund")
        .expect_risk(external_action=True, untrusted_input=False)
        .expect_approval()
        .expect_approval("conditional")
        .expect_spec(timeout=5.0, on_error="return")
    )
    assert CONTRACT.name == "refund"


@pytest.mark.parametrize(
    "check",
    [
        lambda c: c.expect_args("order_id"),
        lambda c: c.expect_args("order_id", "amount", "note"),
        lambda c: c.expect_args("order_id", optional=["amount", "note"]),
        lambda c: c.expect_args("missing", exact=False),
        lambda c: c.expect_hidden("amount"),
        lambda c: c.expect_description("nothing like this"),
        lambda c: c.expect_risk(private_data=True),
        lambda c: c.expect_risk(unknown=True),
        lambda c: c.expect_approval(False),
        lambda c: c.expect_spec(retries=3),
        lambda c: c.expect_spec(color="red"),
        lambda c: c.expect_snapshot({"name": "other"}),
    ],
)
def test_failed_expectations_raise_assertion_errors(check: Any) -> None:
    with pytest.raises(AssertionError):
        check(CONTRACT)


def test_snapshot_is_stable() -> None:
    snapshot = CONTRACT.snapshot()

    assert snapshot == {
        "name": "refund",
        "description": "Refund an order.",
        "args": {
            "order_id": {"type": "string", "required": True, "description": "The order to refund."},
            "amount": {"type": "number", "required": True, "description": "Amount in EUR."},
            "note": {"type": "string", "required": False, "description": "Optional note."},
        },
        "risk": ["external_action"],
        "approval": "conditional",
    }
    CONTRACT.expect_snapshot({"name": "refund", "approval": "conditional"})


def test_sync_calls_run_in_a_fake_context() -> None:
    assert CONTRACT.call({"order_id": "A", "amount": 5}) == "refunded 5.0 (EUR, acme)"
    assert CONTRACT.call_message({"order_id": "A", "amount": 5}).status == "success"
    assert "unknown order" in CONTRACT.expect_error({"order_id": "missing", "amount": 1})


async def test_async_calls_and_approvals() -> None:
    contract = ToolContract(refund, deps={"currency": "USD"})

    assert await contract.acall({"order_id": "A", "amount": 5}) == "refunded 5.0 (USD, None)"
    payload = await contract.aexpect_approval_request({"order_id": "A", "amount": 500})
    assert payload["tool"] == "refund" and payload["agent"] == "tool-contract"
    with pytest.raises(ToolApprovalRequested) as raised:
        await contract.acall({"order_id": "A", "amount": 500})
    assert raised.value.payload["args"]["amount"] == 500
    approved = await contract.acall({"order_id": "A", "amount": 500}, approve="approve")
    assert approved.startswith("refunded 500.0")
    edited = await contract.acall(
        {"order_id": "A", "amount": 500}, approve={"action": "edit", "args": {"amount": 90}}
    )
    assert edited.startswith("refunded 90.0")
    rejected = await contract.acall({"order_id": "A", "amount": 500}, approve=False)
    assert "rejected" in rejected
    decisions = [e.decision for e in contract.events if isinstance(e, ApprovalDecidedEvent)]
    assert decisions == ["approve", "edit", "reject"]
    with pytest.raises(AssertionError, match="expected an approval pause"):
        await contract.aexpect_approval_request({"order_id": "A", "amount": 1})
    with pytest.raises(AssertionError, match="expected an error"):
        await contract.aexpect_error({"order_id": "A", "amount": 1})
    with pytest.raises(ValueError):
        await contract.acall({"order_id": "A", "amount": 500}, approve="maybe")  # type: ignore[arg-type]


def test_plain_functions_and_foreign_tools() -> None:
    def greet(name: str) -> str:
        """Greet someone."""
        return f"hi {name}"

    contract = ToolContract(greet)
    contract.expect_args("name").expect_approval(False)
    assert contract.call({"name": "Ann"}) == "hi Ann"

    @lc_tool
    def shout(text: str) -> str:
        """Shout."""
        return text.upper()

    foreign = ToolContract(shout)
    assert foreign.spec is None and foreign.snapshot()["approval"] is None
    foreign.expect_approval(False)
    with pytest.raises(AssertionError, match="no ToolSpec"):
        foreign.expect_spec(timeout=None)


def test_threads_calling_one_fake_model_each_get_their_own_response() -> None:
    from concurrent.futures import ThreadPoolExecutor

    model = FakeChatModel([str(index) for index in range(64)])

    with ThreadPoolExecutor(max_workers=16) as pool:
        answers = list(pool.map(lambda _: model.invoke("hi").content, range(64)))

    assert sorted(answers, key=int) == [str(index) for index in range(64)]
    assert [call.index for call in model.calls] == list(range(64))


@pytest.mark.unconsumed_script  # the suite-wide check would also see the leftover
def test_an_unconsumed_script_fails_at_the_end_of_the_block() -> None:
    with (
        pytest.raises(AssertionError, match=r"1 of 2 left, next 'second'"),
        expect_scripts_consumed(),
    ):
        model = FakeChatModel(["first", "second"])
        model.invoke("hi")


def test_a_consumed_script_passes_and_the_block_lists_its_models() -> None:
    with expect_scripts_consumed() as created:
        model = FakeChatModel(["only"])
        model.invoke("hi")
    assert created == [model]


def test_allow_unconsumed_exempts_one_model() -> None:
    with expect_scripts_consumed():
        FakeChatModel(["used", "left over"], allow_unconsumed=True).invoke("hi")


def test_a_model_that_was_never_called_is_a_placeholder() -> None:
    with expect_scripts_consumed():
        FakeChatModel(["never used"])


@pytest.mark.unconsumed_script
def test_a_failing_block_raises_its_own_error_not_the_script_check() -> None:
    with pytest.raises(ValueError, match="boom"), expect_scripts_consumed():
        FakeChatModel(["used", "left over"]).invoke("hi")
        raise ValueError("boom")


@pytest.mark.unconsumed_script
def test_the_marker_lets_a_test_end_before_its_script() -> None:
    FakeChatModel(["used", "left over"]).invoke("hi")
