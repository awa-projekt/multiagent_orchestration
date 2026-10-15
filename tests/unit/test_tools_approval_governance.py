"""The approval wrapper: context-aware predicates, edits, expiry, decision TTL, events and a real checkpointed graph."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict

import pytest
from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import BaseModel

import awa.tools.approval as approval_module
from awa.agents import Agent
from awa.contracts.events import ApprovalDecidedEvent, ApprovalRequestedEvent
from awa.errors import ConfigurationError
from awa.observability import EventRecorder, RegexRedactor
from awa.runtime.context import RunContext
from awa.testing import FakeChatModel, tool_call
from awa.tools.approval import create_tool_approval_wrapper

SENT: list[dict[str, Any]] = []


class SendArgs(BaseModel):
    amount: float
    recipient: str


async def _send(amount: float, recipient: str) -> str:
    SENT.append({"amount": amount, "recipient": recipient})
    return f"sent {amount} to {recipient}"


def _sync_send(amount: float, recipient: str) -> str:
    SENT.append({"amount": amount, "recipient": recipient})
    return f"sent {amount} to {recipient}"


def payment_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=_sync_send,
        coroutine=_send,
        name="send_payment",
        description="Send a payment.",
        args_schema=SendArgs,
    )


@pytest.fixture(autouse=True)
def _reset() -> None:
    SENT.clear()


def agent_with(
    tool: Any, recorder: EventRecorder, *, amount: float = 500, then: str | None = "Done."
) -> Agent[Any, Any]:
    """An agent that calls `send_payment` and then answers `then` (None: a run that stays paused)."""

    call = tool_call("send_payment", {"amount": amount, "recipient": "acme"})
    return Agent(
        "payments",
        model=FakeChatModel([call] if then is None else [call, then]),
        tools=[tool],
        checkpointer=InMemorySaver(),
        hooks=[recorder],
    )


async def test_request_and_decision_events_carry_ids_reviewer_and_latency() -> None:
    recorder = EventRecorder()
    agent = agent_with(create_tool_approval_wrapper(payment_tool()), recorder)

    paused = await agent.run("Pay acme", thread_id="t-events")

    assert paused.is_interrupted
    payload = paused.interrupts[0]["value"]
    assert payload["agent"] == "payments"
    assert payload["run_id"] == paused.run_id
    assert datetime.fromisoformat(payload["requested_at"]) <= datetime.now(UTC)
    assert "'edit'" in payload["instructions"]
    [requested] = recorder.of_type(ApprovalRequestedEvent)
    assert requested.interrupt_id == paused.interrupts[0]["id"]
    assert requested.tool == "send_payment"
    assert requested.thread_id == "t-events"
    assert '"amount": 500' in requested.args_preview
    assert requested.expires_at is None

    done = await agent.resume(
        "t-events", {"action": "approve", "note": "ok", "reviewer": {"id": "bob", "roles": []}}
    )

    assert done.output == "Done."
    assert SENT == [{"amount": 500.0, "recipient": "acme"}]
    [decided] = recorder.of_type(ApprovalDecidedEvent)
    assert decided.decision == "approve"
    assert decided.reviewer == "bob"
    assert decided.interrupt_id == requested.interrupt_id
    assert decided.latency_ms is not None and decided.latency_ms >= 0
    assert not decided.remembered and not decided.expired
    assert recorder.types().count("approval_requested") == 1


async def test_a_context_aware_predicate_sees_deps() -> None:
    seen: list[RunContext[Any] | None] = []

    def needs_approval(args: dict[str, Any], ctx: RunContext[Any] | None) -> bool:
        seen.append(ctx)
        assert ctx is not None
        return bool(args["amount"] > ctx.deps["limit"])

    recorder = EventRecorder()
    agent = agent_with(
        create_tool_approval_wrapper(payment_tool(), requires_approval=needs_approval), recorder
    )

    result = await agent.run("Pay", deps={"limit": 1000}, thread_id="t-ctx")

    assert not result.is_interrupted
    assert SENT and seen[0] is not None and seen[0].agent == "payments"


async def test_an_async_one_argument_predicate_still_works() -> None:
    async def needs_approval(args: dict[str, Any]) -> bool:
        return bool(args["amount"] > 100)

    recorder = EventRecorder()
    agent = agent_with(
        create_tool_approval_wrapper(payment_tool(), requires_approval=needs_approval),
        recorder,
        amount=50,
    )
    result = await agent.run("Pay", thread_id="t-async")
    assert not result.is_interrupted and SENT


async def test_edit_runs_the_call_with_the_merged_arguments() -> None:
    recorder = EventRecorder()
    agent = agent_with(create_tool_approval_wrapper(payment_tool()), recorder)
    await agent.run("Pay", thread_id="t-edit")

    await agent.resume("t-edit", {"action": "edit", "args": {"amount": 42}, "actor": "carol"})

    assert SENT == [{"amount": 42.0, "recipient": "acme"}]
    [decided] = recorder.of_type(ApprovalDecidedEvent)
    assert (decided.decision, decided.reviewer) == ("edit", "carol")


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ({"action": "edit", "args": {"amount": "lots"}}, "edited arguments are invalid"),
        ({"action": "edit"}, "needs an 'args' mapping"),
        ("approve", "malformed resume payload"),
    ],
)
async def test_invalid_edits_and_payloads_reject(decision: Any, expected: str) -> None:
    recorder = EventRecorder()
    agent = agent_with(create_tool_approval_wrapper(payment_tool()), recorder)
    await agent.run("Pay", thread_id="t-invalid")

    await agent.resume("t-invalid", decision)

    assert SENT == []
    [decided] = recorder.of_type(ApprovalDecidedEvent)
    assert decided.decision == "reject"
    assert expected in decided.note


async def test_edits_can_be_disabled() -> None:
    recorder = EventRecorder()
    agent = agent_with(create_tool_approval_wrapper(payment_tool(), allow_edits=False), recorder)
    await agent.run("Pay", thread_id="t-no-edit")
    await agent.resume("t-no-edit", {"action": "edit", "args": {"amount": 1}})

    assert SENT == []
    assert "not allowed" in recorder.of_type(ApprovalDecidedEvent)[0].note


async def test_an_expired_request_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = EventRecorder()
    wrapped = create_tool_approval_wrapper(payment_tool(), expires_after=timedelta(hours=1))
    agent = agent_with(wrapped, recorder)
    paused = await agent.run("Pay", thread_id="t-expire")
    payload = paused.interrupts[0]["value"]
    expires_at = datetime.fromisoformat(payload["expires_at"])
    assert expires_at - datetime.fromisoformat(payload["requested_at"]) == timedelta(hours=1)
    assert recorder.of_type(ApprovalRequestedEvent)[0].expires_at == expires_at

    later = datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr(approval_module, "_now", lambda: later)
    await agent.resume("t-expire", {"action": "approve"})

    assert SENT == []
    [decided] = recorder.of_type(ApprovalDecidedEvent)
    assert decided.decision == "reject" and decided.expired
    assert "expired" in decided.note


async def test_a_request_within_its_lifetime_is_approved() -> None:
    recorder = EventRecorder()
    wrapped = create_tool_approval_wrapper(payment_tool(), expires_after=3600)
    agent = agent_with(wrapped, recorder)
    await agent.run("Pay", thread_id="t-in-time")
    await agent.resume("t-in-time", {"action": "approve"})
    assert SENT


def test_lifetimes_must_be_positive() -> None:
    with pytest.raises(ConfigurationError):
        create_tool_approval_wrapper(payment_tool(), expires_after=0)
    with pytest.raises(ConfigurationError):
        create_tool_approval_wrapper(payment_tool(), decision_ttl=timedelta(seconds=-1))


class _State(TypedDict, total=False):
    result: str


def _graph(tool: Any, *, sync: bool = False, args: dict[str, Any] | None = None) -> Any:
    call_args = args or {"amount": 500, "recipient": "acme"}

    async def call_tool(state: _State) -> dict[str, Any]:
        return {"result": await tool.ainvoke(call_args)}

    def call_tool_sync(state: _State) -> dict[str, Any]:
        return {"result": tool.invoke(call_args)}

    builder: StateGraph[_State] = StateGraph(_State)
    builder.add_node("call_tool", call_tool_sync if sync else call_tool)
    builder.add_edge(START, "call_tool")
    builder.add_edge("call_tool", END)
    return builder.compile(checkpointer=InMemorySaver())


async def test_remembered_decisions_expire_after_their_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(
        payment_tool(),
        store=store,
        memory_key=lambda args, ctx: str(args["recipient"]),
        decision_ttl=timedelta(days=1),
    )
    graph = _graph(wrapped)
    first = {"configurable": {"thread_id": "ttl-1"}}
    await graph.ainvoke({}, config=first)
    await graph.ainvoke(Command(resume={"action": "approve", "reviewer": "bob"}), config=first)
    stored = await store.aget(("awa", "tool_approval"), "local|acme")
    assert stored is not None and stored.value["reviewer"] == "bob"

    reused = await graph.ainvoke({}, config={"configurable": {"thread_id": "ttl-2"}})
    assert "__interrupt__" not in reused

    later = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(approval_module, "_now", lambda: later)
    asked = await graph.ainvoke({}, config={"configurable": {"thread_id": "ttl-3"}})
    assert "__interrupt__" in asked
    assert await store.aget(("awa", "tool_approval"), "local|acme") is None


async def test_an_expired_decision_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(
        payment_tool(),
        store=store,
        memory_key=lambda args, ctx: str(args["recipient"]),
        expires_after=3600,
    )
    graph = _graph(wrapped)
    first = {"configurable": {"thread_id": "late-1"}}
    await graph.ainvoke({}, config=first)
    late = datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr(approval_module, "_now", lambda: late)
    await graph.ainvoke(Command(resume={"action": "approve"}), config=first)
    assert SENT == []  # the late approval was rejected as expired ...
    assert await store.aget(("awa", "tool_approval"), "local|acme") is None  # ... and not kept

    monkeypatch.undo()
    asked = await graph.ainvoke({}, config={"configurable": {"thread_id": "late-2"}})
    assert "__interrupt__" in asked  # the next identical call is asked again, not auto-rejected


async def test_the_lifetime_and_the_decision_ttl_include_their_last_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(approval_module, "_now", lambda: moment)
    monkeypatch.setattr(approval_module, "_request_time", lambda: moment)  # else: the checkpoint's
    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(
        payment_tool(),
        store=store,
        memory_key=lambda args, ctx: str(args["recipient"]),
        expires_after=3600,
        decision_ttl=timedelta(days=1),
    )
    graph = _graph(wrapped)
    first = {"configurable": {"thread_id": "edge-1"}}
    await graph.ainvoke({}, config=first)
    monkeypatch.setattr(approval_module, "_now", lambda: moment + timedelta(hours=1))
    await graph.ainvoke(Command(resume={"action": "approve"}), config=first)
    assert len(SENT) == 1  # exactly one hour after the request: still in time

    monkeypatch.setattr(approval_module, "_now", lambda: moment + timedelta(hours=1, days=1))
    reused = await graph.ainvoke({}, config={"configurable": {"thread_id": "edge-2"}})
    assert "__interrupt__" not in reused  # exactly one day after the decision: still remembered

    monkeypatch.setattr(
        approval_module, "_now", lambda: moment + timedelta(days=1, hours=1, seconds=1)
    )
    asked = await graph.ainvoke({}, config={"configurable": {"thread_id": "edge-3"}})
    assert "__interrupt__" in asked


@pytest.mark.parametrize(
    "answer",
    [
        {"action": "edit", "args": {"amount": 1}},
        {"action": "edit", "args": {"amount": "five"}},
        {"action": "aprove"},
        "yes",
    ],
)
async def test_only_explicit_approvals_and_rejections_are_remembered(answer: Any) -> None:
    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(payment_tool(), store=store, memory_key=lambda args: "k")
    graph = _graph(wrapped)
    config = {"configurable": {"thread_id": "edit-memory"}}
    await graph.ainvoke({}, config=config)
    await graph.ainvoke(Command(resume=answer), config=config)
    assert await store.aget(("awa", "tool_approval"), "local|k") is None


async def test_an_edit_cannot_set_an_injected_argument() -> None:
    seen: list[str] = []

    def act(target: str, account: Annotated[str, InjectedToolArg] = "own") -> str:
        """Act on a target."""
        seen.append(account)
        return "done"

    wrapped = create_tool_approval_wrapper(StructuredTool.from_function(act), allow_edits=True)
    graph = _graph(wrapped, args={"target": "x", "account": "own"})
    config = {"configurable": {"thread_id": "edit-injected"}}
    await graph.ainvoke({}, config=config)
    edit = {"action": "edit", "args": {"target": "y", "account": "admin"}}
    result = await graph.ainvoke(Command(resume=edit), config=config)

    assert seen == [] and "only the model's arguments, not account" in result["result"]


async def test_the_reviewer_payload_can_be_redacted() -> None:
    wrapped = create_tool_approval_wrapper(payment_tool(), redactor=RegexRedactor.pii())
    graph = _graph(_EmailTool.wrap(wrapped))
    config = {"configurable": {"thread_id": "redacted"}}

    paused = await graph.ainvoke({}, config=config)
    assert paused["__interrupt__"][0].value["args"]["recipient"] == "[REDACTED:email]"
    await graph.ainvoke(Command(resume={"action": "approve"}), config=config)
    assert SENT == [{"amount": 500.0, "recipient": "jane@example.com"}]


class _EmailTool:
    """Calls the wrapped tool with an e-mail recipient."""

    @staticmethod
    def wrap(tool: Any) -> Any:
        class Caller:
            async def ainvoke(self, args: dict[str, Any]) -> Any:
                return await tool.ainvoke({**args, "recipient": "jane@example.com"})

        return Caller()


def test_the_sync_path_pauses_and_resumes() -> None:
    wrapped = create_tool_approval_wrapper(payment_tool())
    graph = _graph(wrapped, sync=True)
    config = {"configurable": {"thread_id": "sync"}}

    paused = graph.invoke({}, config=config)
    assert "__interrupt__" in paused
    result = graph.invoke(Command(resume={"action": "reject", "note": "no"}), config=config)
    assert "rejected" in result["result"] and SENT == []

    graph2 = _graph(create_tool_approval_wrapper(payment_tool()), sync=True)
    config2 = {"configurable": {"thread_id": "sync-2"}}
    graph2.invoke({}, config=config2)
    graph2.invoke(Command(resume={"action": "approve"}), config=config2)
    assert SENT == [{"amount": 500.0, "recipient": "acme"}]


def test_an_async_predicate_on_the_sync_path_is_an_error() -> None:
    async def needs(args: dict[str, Any]) -> bool:
        return True

    wrapped = create_tool_approval_wrapper(payment_tool(), requires_approval=needs)
    with pytest.raises(TypeError, match="awaitable"):
        wrapped.invoke({"amount": 1, "recipient": "x"})


def _call(name: str, args: dict[str, Any]) -> Any:
    from awa._internal.upstream.messages import ToolCall

    return ToolCall(name=name, args=args, id="c1", type="tool_call")


def _failing_tool(handler: Any) -> StructuredTool:
    from awa._internal.upstream.tools import ToolException

    def explode(amount: float, recipient: str) -> str:
        raise ToolException("provider rejected the payment")

    return StructuredTool.from_function(
        func=explode,
        name="send_payment",
        description="Send a payment.",
        args_schema=SendArgs,
        handle_tool_error=handler,
    )


@pytest.mark.parametrize(
    ("handler", "text"),
    [
        (True, "provider rejected the payment"),
        (lambda exc: f"Error: {exc}", "Error: provider rejected the payment"),
        ("payment failed", "payment failed"),
    ],
)
async def test_a_handled_tool_error_stays_an_error_through_the_gate(
    handler: Any, text: str
) -> None:
    wrapped = create_tool_approval_wrapper(
        _failing_tool(handler), requires_approval=lambda a: False
    )
    call = _call("send_payment", {"amount": 5, "recipient": "acme"})
    for message in (wrapped.invoke(call), await wrapped.ainvoke(call)):
        assert message.status == "error" and message.content == text


async def test_validation_errors_use_the_tools_own_handler() -> None:
    tool = payment_tool()
    tool.handle_validation_error = "check the arguments"
    wrapped = create_tool_approval_wrapper(tool, requires_approval=lambda a: False)
    message = await wrapped.ainvoke(_call("send_payment", {"amount": "many", "recipient": "x"}))
    assert message.status == "error" and message.content == "check the arguments"


def test_edit_and_rewrite_validate_a_tool_with_an_injected_runtime_parameter() -> None:
    from awa.tools.approval import validate_args

    def send(amount: float, recipient: str, runtime: ToolRuntime) -> str:
        """Send a payment."""

        return "sent"

    tool = StructuredTool.from_function(send, name="send_payment")
    assert validate_args(tool, {"amount": 5, "recipient": "acme", "runtime": object()}) is None
    assert validate_args(tool, {"amount": 5, "recipient": "acme"}) is None
    problem = validate_args(tool, {"amount": "many", "recipient": "acme"})
    assert problem is not None and "amount" in problem


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize(
    ("resume", "sent", "text"),
    [
        ({"action": "approve"}, [{"amount": 500.0, "recipient": "acme"}], "sent 500.0 to acme"),
        ({"action": "reject", "note": "no"}, [], "rejected"),
        (
            {"action": "edit", "args": {"amount": 7}},
            [{"amount": 7.0, "recipient": "acme"}],
            "sent 7.0 to acme",
        ),
        ({"action": "edit", "args": {"amount": "many"}}, [], "invalid"),
    ],
)
def test_sync_and_async_gate_paths_agree(
    sync: bool, resume: dict[str, Any], sent: list[dict[str, Any]], text: str
) -> None:
    import asyncio

    graph = _graph(create_tool_approval_wrapper(payment_tool()), sync=sync)
    config = {"configurable": {"thread_id": f"agree-{sync}"}}
    command = Command(resume=resume)
    if sync:
        assert "__interrupt__" in graph.invoke({}, config=config)
        result = graph.invoke(command, config=config)
    else:

        async def run() -> Any:
            assert "__interrupt__" in await graph.ainvoke({}, config=config)
            return await graph.ainvoke(command, config=config)

        result = asyncio.run(run())
    assert text in result["result"] and sent == SENT


async def _run_for(tenant: str, wrapped: Any, thread: str, then: str | None = "Done.") -> Any:
    agent = agent_with(wrapped, EventRecorder(), then=then)
    return await agent.run("Pay acme", thread_id=thread, metadata={"awa.tenant": tenant}), agent


async def test_two_tenants_with_one_memory_key_do_not_share_a_decision() -> None:
    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(
        payment_tool(), store=store, memory_key=lambda args: str(args["recipient"])
    )
    paused, agent = await _run_for("acme", wrapped, "t-a1")
    assert paused.is_interrupted
    await agent.resume(
        "t-a1", {"action": "approve", "reviewer": "bob"}, metadata={"awa.tenant": "acme"}
    )

    again, _ = await _run_for("acme", wrapped, "t-a2")
    assert not again.is_interrupted  # the tenant's own decision is reused
    other, _ = await _run_for("globex", wrapped, "t-g1", then=None)
    assert other.is_interrupted  # another tenant is asked


async def test_the_stored_key_splits_into_tenant_and_memory_key() -> None:
    import re

    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(
        payment_tool(), store=store, memory_key=lambda args: f"pay:{args['recipient']}"
    )
    _, agent = await _run_for("acme", wrapped, "t-split")
    await agent.resume("t-split", {"action": "approve"}, metadata={"awa.tenant": "acme"})
    keys = [item.key for item in await store.asearch(("awa", "tool_approval"))]
    assert [re.split(r"[:/|]", key) for key in keys] == [["acme", "pay", "acme"]]


def _drive(graph: Any, thread: str, resume: Any, *, sync: bool) -> Any:
    """Start the graph (it pauses), resume it, and return the final state."""

    import asyncio

    config = {"configurable": {"thread_id": thread}}
    if sync:
        assert "__interrupt__" in graph.invoke({}, config=config)
        return graph.invoke(Command(resume=resume), config=config)

    async def run() -> Any:
        assert "__interrupt__" in await graph.ainvoke({}, config=config)
        return await graph.ainvoke(Command(resume=resume), config=config)

    return asyncio.run(run())


@pytest.mark.parametrize("sync", [False, True])
def test_valid_actor_false_raises_invalid_gate_actor(sync: bool) -> None:
    from awa.tools.approval import InvalidGateActorError

    wrapped = create_tool_approval_wrapper(payment_tool(), valid_actor=lambda decision: False)
    with pytest.raises(InvalidGateActorError):
        _drive(_graph(wrapped, sync=sync), f"actor-{sync}", {"action": "approve"}, sync=sync)
    assert SENT == []


@pytest.mark.parametrize("sync", [False, True])
def test_a_malformed_resume_rejects_with_the_note(sync: bool) -> None:
    wrapped = create_tool_approval_wrapper(payment_tool())
    result = _drive(_graph(wrapped, sync=sync), f"bad-{sync}", "approve please", sync=sync)
    assert "malformed resume payload" in result["result"] and SENT == []


def test_reviewer_forms() -> None:
    from types import SimpleNamespace

    from awa.tools.approval import _reviewer

    assert _reviewer({"reviewer": "bob"}) == "bob"
    assert _reviewer({"reviewer": {"id": "bob", "roles": []}}) == "bob"
    assert _reviewer({"actor": SimpleNamespace(id="amy")}) == "amy"
    assert _reviewer({"reviewer": "", "actor": "carl"}) == "carl"
    assert _reviewer({}) is None and _reviewer({"reviewer": ""}) is None


@pytest.mark.parametrize("sync", [False, True])
def test_remembered_approval_and_ttl_on_both_paths(
    sync: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(
        payment_tool(),
        store=store,
        memory_key=lambda args: str(args["recipient"]),
        decision_ttl=timedelta(days=1),
    )
    graph = _graph(wrapped, sync=sync)
    _drive(graph, f"mem-1-{sync}", {"action": "approve", "reviewer": "bob"}, sync=sync)
    assert len(SENT) == 1

    def start(thread: str) -> Any:
        config = {"configurable": {"thread_id": thread}}
        return graph.invoke({}, config=config) if sync else asyncio.run(graph.ainvoke({}, config))

    assert "__interrupt__" not in start(f"mem-2-{sync}") and len(SENT) == 2
    later = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(approval_module, "_now", lambda: later)
    assert "__interrupt__" in start(f"mem-3-{sync}") and len(SENT) == 2


async def test_a_resume_answer_wins_over_a_decision_a_parallel_call_just_remembered() -> None:
    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(payment_tool(), store=store, memory_key=lambda args: "k")
    model = FakeChatModel(
        [
            [
                tool_call("send_payment", {"amount": 5, "recipient": "a"}, id="c1"),
                tool_call("send_payment", {"amount": 7, "recipient": "a"}, id="c2"),
            ],
            "done",
        ]
    )
    agent = Agent("payer", model=model, tools=[wrapped], checkpointer=InMemorySaver())
    paused = await agent.run("pay both")
    answers = {
        item["id"]: {"action": "reject"}
        if item["value"]["args"]["amount"] == 5
        else {"action": "approve"}
        for item in paused.interrupts
    }

    await agent.resume(paused.thread_id, answers)

    assert [sent["amount"] for sent in SENT] == [7]


def test_approvals_off_returns_the_tool_unwrapped_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("awa.plugins.switches.is_off", lambda name: name == "guard.approvals")
    tool = payment_tool()
    with caplog.at_level("WARNING", logger="awa.tools"):
        assert create_tool_approval_wrapper(tool) is tool
    assert "Approvals are switched off" in caplog.text


def test_approvals_on_wraps_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awa.plugins.switches.is_off", lambda name: False)
    wrapped = create_tool_approval_wrapper(payment_tool())
    assert wrapped.metadata is not None and wrapped.metadata["awa_approval_gate"] is True


async def _send_payment(amount: float, recipient: str) -> str:
    return f"sent {amount} to {recipient}"


def _make_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_send_payment,
        name="send_payment",
        description="Send a payment.",
        args_schema=SendArgs,
    )


class ToolCallState(TypedDict, total=False):
    result: str


def _build_graph(tool: Any) -> Any:
    async def call_tool(state: ToolCallState) -> dict[str, Any]:
        result = await tool.ainvoke({"amount": 500, "recipient": "acme"})
        return {"result": result}

    builder: StateGraph[ToolCallState] = StateGraph(ToolCallState)
    builder.add_node("call_tool", call_tool)
    builder.add_edge(START, "call_tool")
    builder.add_edge("call_tool", END)
    return builder.compile(checkpointer=InMemorySaver())


async def test_approved_call_actually_runs_the_wrapped_tool() -> None:
    wrapped = create_tool_approval_wrapper(_make_tool())
    graph = _build_graph(wrapped)
    config = {"configurable": {"thread_id": "t-approve"}}

    paused = await graph.ainvoke({}, config=config)
    assert "__interrupt__" in paused
    payload = paused["__interrupt__"][0].value
    assert payload["tool"] == "send_payment"
    assert payload["args"] == {"amount": 500, "recipient": "acme"}

    result = await graph.ainvoke(Command(resume={"action": "approve"}), config=config)
    assert result["result"] == "sent 500.0 to acme"


async def test_rejected_call_never_runs_the_wrapped_tool() -> None:
    wrapped = create_tool_approval_wrapper(_make_tool())
    graph = _build_graph(wrapped)
    config = {"configurable": {"thread_id": "t-reject"}}

    await graph.ainvoke({}, config=config)
    result = await graph.ainvoke(
        Command(resume={"action": "reject", "note": "over limit"}), config=config
    )

    assert "sent" not in result["result"]
    assert "rejected" in result["result"]
    assert "over limit" in result["result"]


async def test_requires_approval_false_skips_the_interrupt_entirely() -> None:
    wrapped = create_tool_approval_wrapper(_make_tool(), requires_approval=lambda args: False)
    graph = _build_graph(wrapped)
    config = {"configurable": {"thread_id": "t-no-approval-needed"}}

    result = await graph.ainvoke({}, config=config)

    assert "__interrupt__" not in result
    assert result["result"] == "sent 500.0 to acme"


async def test_a_remembered_approval_skips_the_interrupt_on_a_later_matching_call() -> None:
    store = InMemoryStore()
    wrapped = create_tool_approval_wrapper(
        _make_tool(), store=store, memory_key=lambda args: f"{args['recipient']}"
    )
    graph = _build_graph(wrapped)

    first_call = {"configurable": {"thread_id": "t-remember-1"}}
    await graph.ainvoke({}, config=first_call)
    await graph.ainvoke(Command(resume={"action": "approve"}), config=first_call)

    second_call = {"configurable": {"thread_id": "t-remember-2"}}
    result = await graph.ainvoke({}, config=second_call)

    assert "__interrupt__" not in result
    assert result["result"] == "sent 500.0 to acme"
