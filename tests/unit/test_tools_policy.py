"""`ToolPolicyMiddleware` decisions, `Agent(tool_policy=...)`, toolboxes, `Toolbox` connectors and `env_field`."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr, ValidationError

from awa.agents import Agent
from awa.contracts.events import (
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    GuardrailBlockedEvent,
    rejection_message,
)
from awa.errors import ConfigurationError, MissingConfigurationError
from awa.observability import EventRecorder
from awa.runtime.context import RunContext
from awa.testing import FakeChatModel, ToolContract, tool_call
from awa.tools import (
    Allow,
    Ask,
    Deny,
    PolicyDecision,
    Rewrite,
    Toolbox,
    ToolCallInfo,
    ToolPolicyMiddleware,
    ToolRisk,
    ask_for_risk,
    default_approval_payload,
    deny_tools,
    env_field,
    tool,
    tool_method,
)

SENT: list[tuple[float, str]] = []


@tool(risk=["external_action"])
def pay(amount: float, to: str) -> str:
    """Send a payment."""
    SENT.append((amount, to))
    return f"paid {amount} to {to}"


@tool(approval=True, risk=["external_action"])
def wire(amount: float) -> str:
    """Wire money."""
    return f"wired {amount}"


@pytest.fixture(autouse=True)
def _reset() -> None:
    SENT.clear()
    CALLS.clear()


def _agent(
    args: dict[str, Any],
    *policies: Any,
    recorder: EventRecorder | None = None,
    name: str = "pay",
    then: str | None = "Done.",
) -> Agent[Any, Any]:
    """An agent that calls `name` and then answers `then` (None: a run that stops at the call)."""

    return Agent(
        "payer",
        model=FakeChatModel(
            [tool_call(name, args)] if then is None else [tool_call(name, args), then]
        ),
        tools=[pay, wire],
        middleware=[ToolPolicyMiddleware(*policies)],
        checkpointer=InMemorySaver(),
        hooks=[recorder] if recorder is not None else [],
    )


def _tool_texts(result: Any) -> list[str]:
    return [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]


async def test_allow_and_none_let_the_call_run() -> None:
    seen: list[ToolCallInfo] = []

    def observe(call: ToolCallInfo, ctx: RunContext[Any] | None) -> None:
        seen.append(call)

    result = await _agent({"amount": 5, "to": "acme"}, observe, lambda c, x: Allow()).run("pay")

    assert result.output == "Done." and SENT == [(5.0, "acme")]
    [info] = seen
    assert info.name == "pay" and info.args == {"amount": 5, "to": "acme"}
    assert info.risk.external_action and info.spec is not None and info.call_id


async def test_deny_returns_an_error_result_and_an_event() -> None:
    recorder = EventRecorder()
    policy = deny_tools(["pay"], reason="payments are frozen")

    result = await _agent({"amount": 5, "to": "acme"}, policy, recorder=recorder).run("pay")

    assert SENT == []
    [text] = _tool_texts(result)
    assert text == "Error: the call to 'pay' was denied by policy: payments are frozen"
    [event] = recorder.of_type(GuardrailBlockedEvent)
    assert event.stage == "tool" and event.tool == "pay" and event.reason == "payments are frozen"
    assert event.guardrail.startswith("tool_policy:")


async def test_rewrite_changes_validated_arguments() -> None:
    def normalize(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        return Rewrite({**call.args, "to": call.args["to"].lower()})

    seen: list[dict[str, Any]] = []

    def later(call: ToolCallInfo, ctx: RunContext[Any] | None) -> None:
        seen.append(call.args)

    await _agent({"amount": 5, "to": "ACME"}, normalize, later).run("pay")

    assert SENT == [(5.0, "acme")] and seen == [{"amount": 5, "to": "acme"}]


async def test_an_invalid_rewrite_is_denied() -> None:
    def broken(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        return Rewrite({"amount": "lots"})

    result = await _agent({"amount": 5, "to": "acme"}, broken).run("pay")

    assert SENT == []
    assert "rewrote the arguments into invalid ones" in _tool_texts(result)[0]


async def test_ask_pauses_for_approval_and_edits_are_validated() -> None:
    recorder = EventRecorder()

    def large(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        return Ask("large payment", {"limit": 100}) if call.args["amount"] > 100 else Allow()

    agent = _agent({"amount": 500, "to": "acme"}, large, recorder=recorder)
    paused = await agent.run("pay", thread_id="t-ask")

    assert paused.is_interrupted and SENT == []
    payload = paused.interrupts[0]["value"]
    assert payload["reason"] == "tool_policy: large payment" and payload["limit"] == 100
    assert recorder.of_type(ApprovalRequestedEvent)[0].reason == "tool_policy: large payment"

    done = await agent.resume("t-ask", {"action": "edit", "args": {"amount": 50}})
    assert done.output == "Done." and SENT == [(50.0, "acme")]


async def test_rejecting_an_ask_does_not_run_the_tool() -> None:
    agent = _agent({"amount": 5, "to": "acme"}, lambda c, x: Ask("always ask"))
    await agent.run("pay", thread_id="t-no")

    done = await agent.resume("t-no", {"action": "reject", "note": "no"})

    assert SENT == [] and "Reason: no." in _tool_texts(done)[-1]


async def test_deny_wins_over_an_earlier_ask_and_async_policies_work() -> None:
    async def deny_all(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        return Deny("closed")

    result = await _agent({"amount": 5, "to": "a"}, lambda c, x: Ask("?"), deny_all).run("pay")

    assert not result.is_interrupted and "denied by policy: closed" in _tool_texts(result)[0]


async def test_ask_for_risk_skips_tools_with_their_own_approval() -> None:
    paused = await _agent({"amount": 5, "to": "a"}, ask_for_risk(), then=None).run(
        "pay", thread_id="r1"
    )
    assert (
        paused.interrupts[0]["value"]["reason"] == "tool_policy: the tool is marked external_action"
    )

    own = await _agent({"amount": 5}, ask_for_risk(), name="wire", then=None).run(
        "wire", thread_id="r2"
    )
    assert own.interrupts[0]["value"]["reason"] == "tool_call_requires_approval"

    calm = await _agent({"amount": 5, "to": "a"}, ask_for_risk("private_data")).run("pay")
    assert not calm.is_interrupted and SENT == [(5.0, "a")]


async def test_invalid_arguments_get_an_error_result_instead_of_a_question() -> None:
    result = await _agent({"amount": "lots", "to": "a"}, lambda c, x: Ask("?")).run(
        "pay", thread_id="bad-args"
    )

    assert not result.is_interrupted and SENT == []
    [text] = _tool_texts(result)
    assert "amount" in text and "error" in text.lower()


@pytest.mark.parametrize("key", ["awa_approval_gate", "awa_requires_approval"])
def test_ask_for_risk_skips_plain_tools_marked_for_approval(key: str) -> None:
    from langchain_core.tools import StructuredTool

    def send(text: str) -> str:
        """Send a text."""
        return text

    gated = StructuredTool.from_function(send, metadata={key: True})
    call = ToolCallInfo(
        name="send", args={}, tool=gated, risk=ToolRisk(external_action=True), spec=None
    )

    assert isinstance(ask_for_risk()(call, None), Allow)


async def test_a_wrong_return_type_is_a_clear_error() -> None:
    agent = _agent({"amount": 5, "to": "a"}, lambda c, x: "yes", then=None)

    with pytest.raises(TypeError, match="return Allow"):
        await agent.run("pay")


def test_the_middleware_needs_a_policy() -> None:
    with pytest.raises(ValueError):
        ToolPolicyMiddleware()


async def test_two_asks_are_joined() -> None:
    result = await _agent(
        {"amount": 5, "to": "a"},
        lambda c, x: Ask("first", {"x": 1}),
        lambda c, x: Ask("second", {"y": 2}),
        then=None,
    ).run("pay", thread_id="t-two")
    payload = result.interrupts[0]["value"]
    assert payload["reason"] == "tool_policy: first; second"
    assert payload["x"] == 1 and payload["y"] == 2


async def test_a_later_policy_sees_rewritten_args() -> None:
    seen: list[str] = []

    def lower(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        return Rewrite({**call.args, "to": call.args["to"].lower()})

    def check(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        seen.append(call.args["to"])
        return Allow()

    await _agent({"amount": 5, "to": "ACME"}, lower, check).run("pay")
    assert seen == ["acme"] and SENT == [(5.0, "acme")]


async def test_an_unknown_tool_runs_when_allowed_and_is_refused_when_asked() -> None:
    from langchain.agents.middleware import ToolCallRequest

    call = {"name": "ghost", "args": {}, "id": "c", "type": "tool_call"}
    request = ToolCallRequest(tool_call=call, tool=None, state={}, runtime=None)  # type: ignore[arg-type]
    ran: list[str] = []

    async def handler(r: ToolCallRequest) -> ToolMessage:
        ran.append(r.tool_call["name"])
        return ToolMessage(content="ran", tool_call_id="c")

    allowed = await ToolPolicyMiddleware(lambda c, x: Allow()).awrap_tool_call(request, handler)
    assert isinstance(allowed, ToolMessage) and allowed.content == "ran"
    asked = await ToolPolicyMiddleware(lambda c, x: Ask("would ask")).awrap_tool_call(
        request, handler
    )
    assert isinstance(asked, ToolMessage) and asked.status == "error"
    assert "needs approval" in str(asked.content) and ran == ["ghost"]


async def test_the_ask_payload_events_and_rejection_text_are_stable() -> None:
    recorder = EventRecorder()
    agent = _agent(
        {"amount": 500, "to": "acme"}, lambda c, x: Ask("check", {"limit": 1}), recorder=recorder
    )
    paused = await agent.run("pay", thread_id="t-pin")

    payload = dict(paused.interrupts[0]["value"])
    assert payload.pop("run_id") == paused.run_id and payload.pop("requested_at")
    assert payload == {
        **default_approval_payload("pay", {"amount": 500.0, "to": "acme"}),
        "limit": 1,
        "reason": "tool_policy: check",
        "agent": "payer",
    }
    [requested] = recorder.of_type(ApprovalRequestedEvent)
    assert requested.args_preview == '{"amount": 500.0, "to": "acme"}'
    assert requested.interrupt_id == paused.interrupts[0]["id"]

    done = await agent.resume("t-pin", {"action": "reject", "note": "too much"})

    assert SENT == [] and _tool_texts(done)[-1] == rejection_message("pay", "too much")
    [decided] = recorder.of_type(ApprovalDecidedEvent)
    assert (decided.decision, decided.interrupt_id) == ("reject", requested.interrupt_id)


async def test_an_approved_ask_runs_the_tool_once_with_the_model_arguments() -> None:
    agent = _agent({"amount": 7, "to": "acme"}, lambda c, x: Ask("check"))
    await agent.run("pay", thread_id="t-pin-ok")

    done = await agent.resume("t-pin-ok", {"action": "approve"})

    assert SENT == [(7.0, "acme")] and _tool_texts(done) == ["paid 7.0 to acme"]


async def test_an_approved_ask_keeps_the_tool_error_and_the_artifact() -> None:
    @tool
    def pay(amount: int) -> str:
        """Pay an amount."""
        raise ValueError("bank down")

    @tool(response_format="content_and_artifact")
    def fetch(n: int) -> tuple[str, dict[str, int]]:
        """Fetch rows."""
        return "text", {"rows": n}

    results = []
    for name, args in (("pay", {"amount": 1}), ("fetch", {"n": 2})):
        model = FakeChatModel([tool_call(name, args), "done"])
        policy = ToolPolicyMiddleware(lambda c, x: Ask("check"))
        agent = Agent(
            "w", model=model, tools=[pay, fetch], middleware=[policy], checkpointer=InMemorySaver()
        )
        paused = await agent.run("go")
        assert paused.thread_id is not None
        done = await agent.resume(paused.thread_id, {"action": "approve"})
        message = next(m for m in done.messages if isinstance(m, ToolMessage))
        results.append((message.status, message.artifact))

    assert results == [("error", None), ("success", {"rows": 2})]


class Crm(Toolbox, prefix="crm"):
    base_url: str = "https://crm.example.invalid"
    token: SecretStr = env_field("TEST_CRM_TOKEN")
    limit: int = 2

    @tool_method(risk=["private_data"])
    def find_customer(self, ctx: RunContext[Any], email: str) -> str:
        """Find a customer by e-mail.

        Args:
            email: The customer's e-mail address.
        """
        return f"{self.base_url}:{email}:{ctx.deps}:{self.token.get_secret_value()}"

    @tool_method("close", approval=True)
    async def close_account(self, account_id: str) -> str:
        """Close an account."""
        return f"closed {account_id}"

    def helper(self) -> str:
        return "not a tool"


def test_toolbox_tools_are_bound_prefixed_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CRM_TOKEN", "s3cret")
    crm = Crm(limit=5)

    find, close = crm.tools()
    assert crm.tools() == [find, close] and crm.tools() is not crm.tools()
    assert [find.name, close.name] == ["crm_find_customer", "crm_close"]
    assert "s3cret" not in repr(crm)
    contract = ToolContract(find, deps="acme")
    contract.expect_args("email").expect_hidden("ctx", "self").expect_risk(private_data=True)
    assert contract.call({"email": "a@b"}) == "https://crm.example.invalid:a@b:acme:s3cret"
    ToolContract(close).expect_approval("always")
    assert Crm.tool_method_names() == ["find_customer", "close_account"]


def test_toolbox_configuration_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_CRM_TOKEN", raising=False)
    with pytest.raises(MissingConfigurationError, match="TEST_CRM_TOKEN"):
        Crm()
    crm = Crm(token=SecretStr("explicit"))
    with pytest.raises(ValidationError):
        Crm(token=SecretStr("x"), unknown=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        crm.limit = 3  # type: ignore[misc]


def test_env_field_default_and_a_toolbox_without_methods() -> None:
    class Plain(Toolbox):
        region: str = env_field("TEST_UNSET_REGION", default="eu")

    assert Plain().region == "eu"
    with pytest.raises(ConfigurationError, match="no @tool_method"):
        Plain().tools()


def test_subclasses_inherit_and_override_tool_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CRM_TOKEN", "t")

    class ReadOnlyCrm(Crm, prefix="ro"):
        def close_account(self, account_id: str) -> str:  # type: ignore[override]
            return "disabled"

    assert [item.name for item in ReadOnlyCrm().tools()] == ["ro_find_customer"]


async def test_an_agent_accepts_a_toolbox_and_its_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CRM_TOKEN", "t")
    model = FakeChatModel([tool_call("crm_close", {"account_id": "A-1"})])
    agent = Agent("desk", model=model, tools=[Crm()], checkpointer=InMemorySaver())

    result = await agent.run("close A-1")

    assert result.is_interrupted
    assert [item.name for item in agent.tools] == ["crm_find_customer", "crm_close"]
    assert [item.name for item in Agent("copy", tools=Crm().tools()).tools] == [
        "crm_find_customer",
        "crm_close",
    ]


CALLS: list[str] = []


@tool
def lookup(order_id: str) -> str:
    """Look an order up."""
    CALLS.append(f"lookup {order_id}")
    return f"order {order_id}"


@tool(risk=["external_action"])
def cancel(order_id: str) -> str:
    """Cancel an order."""
    CALLS.append(f"cancel {order_id}")
    return f"cancelled {order_id}"


class Shop(Toolbox, prefix="shop"):
    region: str = "eu"

    @tool_method()
    def stock(self, sku: str) -> str:
        """Stock of an item."""
        CALLS.append(f"stock {sku}")
        return f"{sku}: 3 in {self.region}"


def _texts(result: Any) -> list[str]:
    return [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]


def _policy_agent(call: str, args: dict[str, Any], **options: Any) -> Agent[Any, Any]:
    return Agent(
        "desk",
        model=FakeChatModel([tool_call(call, args), "Done."]),
        tools=[lookup, cancel],
        **options,
    )


async def test_a_policy_function_refuses_calls() -> None:
    agent = _policy_agent("cancel", {"order_id": "A-1"}, tool_policy=deny_tools(["cancel"]))

    result = await agent.run("cancel A-1")

    assert CALLS == [] and "denied by policy" in _texts(result)[0]
    assert isinstance(agent.tool_policy, ToolPolicyMiddleware)


async def test_several_policies_and_a_middleware_are_accepted() -> None:
    seen: list[str] = []

    def observe(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        seen.append(call.name)
        return Allow()

    listed = _policy_agent("lookup", {"order_id": "A-1"}, tool_policy=[observe, deny_tools(["x"])])
    await listed.run("look")
    middleware = ToolPolicyMiddleware(deny_tools(["lookup"]))
    given = _policy_agent("lookup", {"order_id": "A-2"}, tool_policy=middleware)
    result = await given.run("look")

    assert seen == ["lookup"] and CALLS == ["lookup A-1"]
    assert given.tool_policy is middleware and "denied" in _texts(result)[0]
    assert _policy_agent("lookup", {}, tool_policy=[]).tool_policy is None


async def test_the_policy_sees_calls_before_other_middleware_and_survives_clone() -> None:
    agent = _policy_agent(
        "cancel",
        {"order_id": "A-1"},
        tool_policy=lambda call, ctx: Ask("check it"),
        checkpointer=InMemorySaver(),
    )

    paused = await agent.clone(model=FakeChatModel([tool_call("cancel", {"order_id": "A-1"})])).run(
        "cancel"
    )

    assert paused.is_interrupted and CALLS == []
    assert paused.interrupts[0]["value"]["reason"] == "tool_policy: check it"


@pytest.mark.parametrize("value", ["deny", 3, ["deny"]])
def test_an_invalid_tool_policy_is_a_type_error(value: Any) -> None:
    with pytest.raises(TypeError, match="tool_policy"):
        Agent("desk", tool_policy=value)


async def test_toolboxes_are_flattened() -> None:
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("shop_stock", {"sku": "S-1"}), "Three."]),
        tools=[lookup, Shop(), cancel],
    )

    result = await agent.run("stock?")

    assert [t.name for t in agent.tools] == ["lookup", "shop_stock", "cancel"]
    assert agent.card.tools == ("lookup", "shop_stock", "cancel")
    assert result.output == "Three." and CALLS == ["stock S-1"]
    extra = tool(lambda: "x", name="extra", description="An extra tool.")
    assert agent.with_tools(extra).tools[-1].name == "extra"
