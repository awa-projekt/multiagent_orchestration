"""Governance events and their run-path wiring: guardrail blocks and replacements, limit breaches, hook dispatch, deferred emission, run kinds."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from _helpers.fake_secrets import fake_secret
from awa import AgentNetwork, Sequential
from awa.agents import Agent, AgentHooks
from awa.contracts import RunEvent
from awa.contracts.events import (
    RUN_EVENT_ADAPTER,
    AgentStartedEvent,
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    ErrorEvent,
    GuardrailBlockedEvent,
    LimitExceededEvent,
    ModelResponseEvent,
    RunCancelledEvent,
    TextDeltaEvent,
)
from awa.contracts.usage import UsageLimits
from awa.errors import ConfigurationError, RunTimeoutError, UsageLimitExceededError
from awa.guardrails import (
    BlocklistGuardrail,
    GuardrailTripwireError,
    LengthGuardrail,
    PIIGuardrail,
    SchemaGuardrail,
    run_guardrails,
    run_input_guardrails,
    run_output_guardrails,
)
from awa.observability import EventRecorder, describe_event
from awa.runtime.context import EventEmitter, RunContext, use_run_context
from awa.team import NetworkLimits, Team, TeamLimits
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call

IDS: dict[str, Any] = {"run_id": "run_1", "root_run_id": "run_1", "agent": "a"}


# --------------------------------------------------------------------------- emitter


async def test_emit_later_delivers_before_the_next_event_in_order() -> None:
    emitter = EventEmitter()
    seen: list[str] = []
    emitter.subscribe(lambda event: seen.append(event.type))

    emitter.emit_later(LimitExceededEvent(limit="max_requests", **IDS))
    emitter.emit_later(GuardrailBlockedEvent(guardrail="g", stage="input", **IDS))
    assert seen == []
    await emitter.emit(ErrorEvent(error_type="E", message="m", **IDS))
    assert seen == ["limit_exceeded", "guardrail_blocked", "error"]

    emitter.emit_later(TextDeltaEvent(delta="x", **IDS))
    await emitter.flush()
    assert seen[-1] == "text_delta"


def test_new_events_round_trip_through_the_adapter() -> None:
    for event in (
        ApprovalRequestedEvent(tool="t", interrupt_id="i", **IDS),
        ApprovalDecidedEvent(tool="t", decision="edit", reviewer="bob", **IDS),
        GuardrailBlockedEvent(guardrail="g", stage="tool", tool="t", **IDS),
        LimitExceededEvent(limit="max_tool_calls", value=3, maximum=2, **IDS),
    ):
        again = RUN_EVENT_ADAPTER.validate_json(RUN_EVENT_ADAPTER.dump_json(event))
        assert again == event
        assert event.type in describe_event(event)


async def test_agent_hooks_dispatch_governance_events() -> None:
    calls: list[str] = []

    class Hooks(AgentHooks):
        async def on_approval_requested(self, event: ApprovalRequestedEvent) -> None:
            calls.append("requested")

        async def on_approval_decided(self, event: ApprovalDecidedEvent) -> None:
            calls.append("decided")

        async def on_guardrail_blocked(self, event: GuardrailBlockedEvent) -> None:
            calls.append("guardrail")

        async def on_limit_exceeded(self, event: LimitExceededEvent) -> None:
            calls.append("limit")

    hooks = Hooks()
    await hooks(ApprovalRequestedEvent(tool="t", interrupt_id="i", **IDS))
    await hooks(ApprovalDecidedEvent(tool="t", decision="approve", **IDS))
    await hooks(GuardrailBlockedEvent(guardrail="g", stage="input", **IDS))
    await hooks(LimitExceededEvent(limit="x", **IDS))
    assert calls == ["requested", "decided", "guardrail", "limit"]


# --------------------------------------------------------------------------- guardrails


async def test_agent_input_guardrail_block_is_reported_before_the_error() -> None:
    recorder = EventRecorder()
    agent = Agent(
        "helper",
        model=FakeChatModel(["unused"]),
        input_guardrails=[BlocklistGuardrail(["password"])],
        hooks=[recorder],
    )

    with pytest.raises(GuardrailTripwireError):
        await agent.run("my password is hunter2")

    types = recorder.types()
    assert types.index("guardrail_blocked") < types.index("error")
    [blocked] = recorder.of_type(GuardrailBlockedEvent)
    assert (blocked.guardrail, blocked.stage, blocked.agent) == ("blocklist", "input", "helper")
    assert "hunter2" not in blocked.model_dump_json()


async def test_agent_output_guardrail_block_is_reported() -> None:
    recorder = EventRecorder()
    agent = Agent(
        "helper",
        model=FakeChatModel(["x" * 50]),
        output_guardrails=[LengthGuardrail(max_length=10)],
        hooks=[recorder],
    )
    with pytest.raises(GuardrailTripwireError):
        await agent.run("Go")

    [blocked] = recorder.of_type(GuardrailBlockedEvent)
    assert blocked.stage == "output"
    assert "exceeds the maximum" in blocked.reason


async def test_team_input_guardrail_block_is_reported() -> None:
    recorder = EventRecorder()
    lead = Agent("lead", description="L.", model=FakeChatModel(["Done."]))
    member = Agent("a", description="A.", model=FakeChatModel(["unused"]))
    team = Team(
        "desk",
        orchestrator=lead,
        members=[member],
        input_guardrails=[PIIGuardrail(kinds={"iban"})],
        hooks=[recorder],
    )
    with pytest.raises(GuardrailTripwireError):
        await team.run("Pay to DE89370400440532013000")

    [blocked] = recorder.of_type(GuardrailBlockedEvent)
    assert blocked.agent == "desk"
    assert blocked.reason == "contains personal data: iban (1)"
    assert "DE89" not in blocked.model_dump_json()


async def test_tool_stage_and_warnings() -> None:
    emitter = EventEmitter()
    recorder = EventRecorder()
    emitter.subscribe(recorder)
    ctx: RunContext[None] = RunContext(run_id="run_1", agent="a", deps=None, events=emitter)
    with use_run_context(ctx):
        run_input_guardrails("drop table", [BlocklistGuardrail(["drop"])], tool="sql")
        run_output_guardrails("a@b.example", [PIIGuardrail(mode="warn")])  # a warning: no event
    await emitter.flush()

    [blocked] = recorder.of_type(GuardrailBlockedEvent)
    assert (blocked.stage, blocked.tool) == ("tool", "sql")


def test_guardrails_outside_a_run_emit_nothing() -> None:
    result = run_input_guardrails("password", [BlocklistGuardrail(["password"])])
    assert result.blocked


def test_blocked_reasons_in_events_are_redacted_and_truncated() -> None:
    class Leaky:
        name = "leaky"

        def check(self, candidate: Any) -> Any:
            from awa.guardrails import GuardrailResult

            return GuardrailResult(
                passed=False,
                reason=f"key {fake_secret('sk-', 'abcdefghijklmnopqrstuv')} " + "x" * 500,
            )

    emitter = EventEmitter()
    ctx: RunContext[None] = RunContext(run_id="run_1", agent="a", deps=None, events=emitter)
    with use_run_context(ctx):
        run_input_guardrails("c", [Leaky()])
    [event] = emitter._pending
    assert isinstance(event, GuardrailBlockedEvent)
    assert fake_secret("sk-", "abcdefghijklmnopqrstuv") not in event.reason
    assert len(event.reason) <= 300


# --------------------------------------------------------------------------- PII guardrail


def test_pii_guardrail_modes() -> None:
    text = "Contact jane@example.com or +49 30 1234567"

    blocked = PIIGuardrail().check(text)
    assert blocked.blocked
    assert blocked.reason == "contains personal data: email (1), phone (1)"

    warned = PIIGuardrail(mode="warn").check(text)
    assert not warned.passed and warned.severity == "warn"

    redacted = PIIGuardrail(mode="redact").check(text)
    assert redacted.passed
    assert redacted.replacement == "Contact [REDACTED:email] or [REDACTED:phone]"

    assert PIIGuardrail().check("nothing personal here").passed
    assert PIIGuardrail(kinds={"iban"}).check(text).passed


def test_pii_guardrail_on_structured_values() -> None:
    class Reply(BaseModel):
        text: str
        count: int

    reply = Reply(text="mail jane@example.com", count=2)
    result = PIIGuardrail(mode="redact").check(reply)
    assert isinstance(result.replacement, Reply)
    assert result.replacement.text == "mail [REDACTED:email]"
    assert PIIGuardrail().check({"nested": ["a@b.example"]}).blocked
    assert PIIGuardrail().check(12345).passed

    class Email(BaseModel):
        address: str

        def model_post_init(self, _: Any) -> None:
            if "@" not in self.address:
                raise ValueError("not an address")

    blocked = PIIGuardrail(mode="redact").check(Email(address="a@b.example"))
    assert blocked.blocked and "cannot be redacted" in (blocked.reason or "")


def test_pii_guardrail_with_a_custom_redactor() -> None:
    class Names:
        def redact(self, text: str) -> str:
            return text.replace("Jane", "[NAME]")

    result = PIIGuardrail(redactor=Names()).check("Jane called")
    assert result.blocked and result.reason == "contains personal data: pii (1)"


def test_pii_guardrail_rejects_bad_configuration() -> None:
    with pytest.raises(ConfigurationError):
        PIIGuardrail(mode="shred")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        PIIGuardrail(kinds={"passport"})


def test_replacements_chain_through_the_runner() -> None:
    result = run_guardrails(
        "mail jane@example.com", [PIIGuardrail(mode="redact"), BlocklistGuardrail(["jane@"])]
    )
    assert result.passed
    assert result.replacement == "mail [REDACTED:email]"


def test_schema_guardrail_reason_has_no_candidate_values() -> None:
    class Order(BaseModel):
        amount: int

    result = SchemaGuardrail(Order).check({"amount": "jane@example.com"})
    assert result.blocked
    assert "jane@example.com" not in (result.reason or "")
    assert "amount" in (result.reason or "")


# --------------------------------------------------------------------------- limits


async def test_usage_limit_breach_is_reported_with_its_numbers() -> None:
    recorder = EventRecorder()

    @tool
    def ping() -> str:
        """Ping."""
        return "pong"

    agent = Agent(
        "helper", model=FakeChatModel([tool_call("ping", {})]), tools=[ping], hooks=[recorder]
    )
    with pytest.raises(UsageLimitExceededError):
        await agent.run("Go", usage_limits=UsageLimits(max_requests=1))

    [event] = recorder.of_type(LimitExceededEvent)
    assert (event.limit, event.value, event.maximum) == ("max_requests", 2.0, 1.0)
    types = recorder.types()
    assert types.index("limit_exceeded") < types.index("error")


async def test_token_limit_breach_is_reported() -> None:
    recorder = EventRecorder()
    agent = Agent("helper", model=FakeChatModel(["a long answer " * 20]), hooks=[recorder])
    with pytest.raises(UsageLimitExceededError):
        await agent.run("Go", usage_limits=UsageLimits(max_output_tokens=1))

    [event] = recorder.of_type(LimitExceededEvent)
    assert event.limit == "max_output_tokens"
    assert event.maximum == 1.0


async def test_report_limit_exceeded_for_refused_work() -> None:
    emitter = EventEmitter()
    recorder = EventRecorder()
    emitter.subscribe(recorder)
    ctx: RunContext[None] = RunContext(run_id="run_1", agent="lead", deps=None, events=emitter)
    ctx.report_limit_exceeded(limit="max_calls_per_member", value=3, maximum=2, message="refused")
    ctx.report_limit_exceeded(
        UsageLimitExceededError("x", details={"limit": "max_cost", "value": 2.5, "maximum": 1})
    )
    await emitter.flush()

    first, second = recorder.of_type(LimitExceededEvent)
    assert (first.limit, first.value, first.maximum, first.message) == (
        "max_calls_per_member",
        3,
        2,
        "refused",
    )
    assert (second.limit, second.value, second.maximum) == ("max_cost", 2.5, 1.0)


# --------------------------------------------------------------------------- delegation


async def test_delegated_failures_have_secrets_redacted() -> None:
    def explode(call: ModelCall) -> Any:
        raise RuntimeError(
            f"provider rejected key {fake_secret('sk-', 'abcdefghijklmnopqrstuvwx')} for postgresql://u:pw123@db/x"
        )

    seen: list[str] = []

    def lead_model(call: ModelCall) -> Any:
        if len(call.messages) <= 2:
            return AIMessage(
                content="", tool_calls=[tool_call("delegate_task", {"agent": "a", "task": "x"})]
            )
        seen.append(str(call.last_message.content))
        return "Done."

    recorder = EventRecorder()
    lead = Agent("lead", description="L.", model=FunctionChatModel(lead_model))
    member = Agent("a", description="A.", model=FunctionChatModel(explode))
    await Team("desk", orchestrator=lead, members=[member], hooks=[recorder]).run("Go")

    assert seen and fake_secret("sk-", "abcdefghijklmnopqrstuvwx") not in seen[0]
    assert "pw123" not in seen[0]
    assert "[REDACTED:api_key]" in seen[0]
    results = [e for e in recorder.events if e.type == "delegation_result"]
    assert results and all("sk-abcdefghij" not in e.model_dump_json() for e in results)


def test_model_response_event_carries_provider_and_finish_reason() -> None:
    event = ModelResponseEvent(provider="openai", finish_reason="length", **IDS)
    assert (event.provider, event.finish_reason) == ("openai", "length")
    assert AgentStartedEvent(**IDS).type == "agent_started"


EMAIL = "jane.doe@example.com"


def echo_model() -> FunctionChatModel:
    """Answers with the text of the last user message it received."""

    return FunctionChatModel(lambda call: f"got: {call.last_message.content}")


# ----------------------------------------------------------------------------- guardrails


async def test_agent_input_redaction_changes_what_the_model_sees() -> None:
    model = echo_model()
    agent = Agent("clerk", model=model, input_guardrails=[PIIGuardrail(mode="redact")])

    result = await agent.run(f"Mail {EMAIL} the invoice")

    assert EMAIL not in result.output
    assert EMAIL not in str(model.calls[0].last_message.content)
    assert result.output.startswith("got: Mail ")


async def test_agent_output_redaction_changes_the_returned_output() -> None:
    agent = Agent(
        "clerk",
        model=FakeChatModel([f"Contact {EMAIL}."]),
        output_guardrails=[PIIGuardrail(mode="redact")],
    )

    result = await agent.run("Who handles this?")

    assert EMAIL not in result.output and EMAIL not in result.text
    assert result.output == result.text
    assert EMAIL not in str(result.messages[-1].content)


async def test_team_input_and_output_redaction() -> None:
    lead_model = echo_model()
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=lead_model),
        members=[Agent("helper", description="Helps.", model=FakeChatModel(["x"]))],
        input_guardrails=[PIIGuardrail(mode="redact")],
        output_guardrails=[PIIGuardrail(mode="redact")],
    )

    result = await team.run(f"Write to {EMAIL}")

    assert EMAIL not in str(lead_model.calls[0].last_message.content)
    assert EMAIL not in result.output and EMAIL not in result.text


async def test_team_output_redaction_of_an_answer_with_personal_data() -> None:
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FakeChatModel([f"Reach {EMAIL}"])),
        members=[Agent("helper", description="Helps.", model=FakeChatModel(["x"]))],
        output_guardrails=[PIIGuardrail(mode="redact")],
    )

    result = await team.run("Who?")

    assert result.output.startswith("Reach ") and EMAIL not in result.output


# ----------------------------------------------------------------------------- limits


def delegate(agent: str, task: str = "work") -> Any:
    return tool_call("delegate_task", {"agent": agent, "task": task})


async def test_team_limit_refusals_emit_limit_exceeded_events() -> None:
    events: list[RunEvent] = []
    lead = Agent(
        "lead", model=FakeChatModel([delegate("helper"), delegate("helper", "more"), "done"])
    )
    team = Team(
        "desk",
        orchestrator=lead,
        members=[Agent("helper", description="Helps.", model=FakeChatModel(["ok"]))],
        limits=TeamLimits(max_calls_per_member=1),
        hooks=[events.append],
    )

    await team.run("go")

    limits = [e for e in events if isinstance(e, LimitExceededEvent)]
    assert [(e.limit, e.value, e.maximum) for e in limits] == [("max_calls_per_member", 2, 1)]
    assert "max_calls_per_member=1" in limits[0].message


async def test_parallel_delegation_limit_is_reported() -> None:
    events: list[RunEvent] = []

    def lead_model(call: ModelCall) -> Any:
        return [delegate("a"), delegate("b")] if call.index == 0 else "done"

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(lead_model)),
        members=[
            Agent("a", description="A.", model=FakeChatModel(["a"])),
            Agent("b", description="B.", model=FakeChatModel(["b"])),
        ],
        limits=TeamLimits(max_parallel_delegations=1),
        hooks=[events.append],
    )

    await team.run("go")

    assert [e.limit for e in events if isinstance(e, LimitExceededEvent)] == [
        "max_parallel_delegations"
    ]


async def test_network_task_limit_is_reported() -> None:
    events: list[RunEvent] = []

    def lead_model(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("create_task", {"title": "one", "owner": "mate"})
        if call.index == 1:
            return tool_call("create_task", {"title": "two", "owner": "mate"})
        return "Final."

    network = AgentNetwork(
        "net",
        lead=Agent("lead", description="Plans.", model=FunctionChatModel(lead_model)),
        teammates=[Agent("mate", description="Works.", model=FakeChatModel(["done"]))],
        limits=NetworkLimits(max_tasks=1),
        hooks=[events.append],
    )

    await network.run("go")

    reported = [e for e in events if isinstance(e, LimitExceededEvent)]
    assert [(e.limit, e.maximum) for e in reported] == [("max_tasks", 1)]


# ----------------------------------------------------------------------------- run kinds


async def test_agent_started_events_name_the_run_kind() -> None:
    events: list[RunEvent] = []
    helper = Agent("helper", description="Helps.", model=FakeChatModel(["ok"]))
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FakeChatModel([delegate("helper"), "done"])),
        members=[helper],
    )
    flow = Sequential("flow", [team], description="Runs the desk.")

    async for event in flow.stream("go"):
        events.append(event)

    kinds = {e.agent: e.kind for e in events if isinstance(e, AgentStartedEvent)}
    assert kinds == {"flow": "workflow", "desk": "team", "lead": "agent", "helper": "agent"}


async def test_network_runs_are_of_kind_network() -> None:
    network = AgentNetwork(
        "net",
        lead=Agent("boss", description="Leads.", model=FakeChatModel(["Direct answer."])),
        teammates=[Agent("mate", description="Works.", model=FakeChatModel(["x"]))],
    )
    events = [event async for event in network.stream("go")]

    assert next(e for e in events if isinstance(e, AgentStartedEvent)).kind == "network"


# ----------------------------------------------------------------------------- hooks


async def test_on_run_cancelled_hook_runs_before_on_error() -> None:
    calls: list[str] = []

    class Recorder(AgentHooks):
        async def on_run_cancelled(self, event: RunCancelledEvent) -> None:
            calls.append(f"cancelled:{event.reason}")

        async def on_error(self, event: Any) -> None:
            calls.append(f"error:{event.error_type}")

    @tool
    async def hang() -> str:
        """Hang."""
        await asyncio.sleep(30)
        return "never"

    agent = Agent(
        "worker", model=FakeChatModel([tool_call("hang")]), tools=[hang], hooks=[Recorder()]
    )
    with pytest.raises(RunTimeoutError):
        await agent.run("go", timeout=0.5)

    assert calls == ["cancelled:timeout", "error:RunTimeoutError"]
