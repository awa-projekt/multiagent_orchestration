"""`Agent` runs: results, variants, hooks and the middleware order."""

from __future__ import annotations

import asyncio
import inspect
import sys
import typing
from dataclasses import dataclass
from typing import Any, get_args
from unittest.mock import patch

import pytest
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel
from typing_extensions import TypedDict

from awa import AgentHooks
from awa.agents import Agent, TextDeltaCallback, text_delta_callbacks
from awa.agents._stack import build_stack
from awa.agents.graph_agent import GraphAgent
from awa.agents.hooks import _HANDLERS
from awa.contracts import (
    AgentLike,
    AgentStartedEvent,
    AgentStatus,
    ErrorEvent,
    ModelFallbackEvent,
    ModelRetryEvent,
    RunCompletedEvent,
    RunEvent,
    UsageLimits,
)
from awa.directory import AgentDirectory
from awa.errors import (
    AgentRunError,
    MaxStepsExceededError,
    ModelResolutionError,
    StructuredOutputError,
    UsageLimitExceededError,
)
from awa.guardrails import BlocklistGuardrail, GuardrailTripwireError, LengthGuardrail
from awa.memory import Compaction
from awa.models import ModelRegistry, ModelSettings, allow_model_requests
from awa.persistence.cache import InMemoryCacheStore
from awa.runtime import RunContext, current_run_context, new_run_id, use_run_context
from awa.runtime.cache import Cache, CacheSettings
from awa.team import AgentNetwork, Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call
from awa.tools import deny_tools
from awa.workflows import Sequential


class Answer(BaseModel):
    value: int
    reason: str = ""


@dataclass
class Point:
    x: int
    y: int


class Movie(TypedDict):
    title: str
    year: int


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def _system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


# --------------------------------------------------------------------------- basic runs


async def test_plain_text_run() -> None:
    agent = Agent("assistant", model=FakeChatModel(["Hello there."]))

    result = await agent.run("Hi")

    assert result.output == "Hello there."
    assert result.text == "Hello there."
    assert result.agent == "assistant"
    assert result.run_id.startswith("run_")
    assert [type(m) for m in result.messages] == [HumanMessage, AIMessage]
    assert result.new_messages() == result.messages
    assert result.handoff_path == ("assistant",)
    assert result.thread_id is None and not result.is_interrupted
    assert result.duration_ms >= 0


async def test_tool_loop_with_plain_function_tool() -> None:
    model = FakeChatModel([tool_call("add", {"a": 2, "b": 3}), "The sum is 5."])
    agent = Agent("calculator", model=model, tools=[add])

    result = await agent.run("2+3?")

    assert result.output == "The sum is 5."
    tool_message = result.messages[2]
    assert isinstance(tool_message, ToolMessage) and tool_message.content == "5"
    assert model.calls[1].tool_names == ("add",)
    assert result.usage.requests == 2
    assert result.usage.tool_calls == 1


async def test_instructions_become_the_system_prompt() -> None:
    model = FakeChatModel(["ok"])
    agent = Agent("assistant", model=model, instructions="Be terse.")

    await agent.run("Hi")

    assert _system_prompt(model.calls[0]) == "Be terse."


async def test_ai_messages_carry_the_agent_name() -> None:
    result = await Agent("named", model=FakeChatModel(["ok"])).run("Hi")

    assert result.messages[-1].name == "named"


async def test_message_list_input_and_history() -> None:
    model = FakeChatModel(["answer"])
    agent = Agent("assistant", model=model)
    history = [HumanMessage("earlier question"), AIMessage("earlier answer")]

    result = await agent.run([{"role": "user", "content": "new question"}], history=history)

    assert [m.content for m in model.calls[0].messages] == [
        "earlier question",
        "earlier answer",
        "new question",
    ]
    assert result.new_messages_start == 2
    assert [m.content for m in result.new_messages()] == ["new question", "answer"]


def test_run_sync_outside_an_event_loop() -> None:
    result = Agent("sync", model=FakeChatModel(["done"])).run_sync("Hi")

    assert result.output == "done"


async def test_run_sync_inside_an_event_loop_raises() -> None:
    with pytest.raises(RuntimeError, match="running event loop"):
        Agent("sync", model=FakeChatModel(["done"])).run_sync("Hi")


async def test_agent_satisfies_agent_like() -> None:
    assert isinstance(Agent("a", model=FakeChatModel(["x"])), AgentLike)


# --------------------------------------------------------------------------- structured output


async def test_structured_output_with_pydantic_model() -> None:
    model = FakeChatModel([structured_output(Answer, {"value": 42, "reason": "because"})])
    agent = Agent("typed", model=model, output_type=Answer)

    result = await agent.run("Answer?")

    assert result.output == Answer(value=42, reason="because")
    assert agent.output_type is Answer


async def test_structured_output_after_a_tool_call() -> None:
    model = FakeChatModel(
        [tool_call("add", {"a": 1, "b": 1}), structured_output(Answer, {"value": 2})]
    )
    agent = Agent("typed", model=model, tools=[add], output_type=Answer)

    result = await agent.run("1+1?")

    assert result.output.value == 2
    assert result.usage.tool_calls == 2


@pytest.mark.parametrize(
    ("output_type", "data", "expected"),
    [
        (Point, {"x": 1, "y": 2}, Point(1, 2)),
        (Movie, {"title": "Heat", "year": 1995}, {"title": "Heat", "year": 1995}),
    ],
)
async def test_structured_output_with_dataclass_and_typed_dict(
    output_type: Any, data: dict[str, Any], expected: Any
) -> None:
    agent = Agent(
        "typed",
        model=FakeChatModel([structured_output(output_type.__name__, data)]),
        output_type=output_type,
    )

    result = await agent.run("go")

    assert result.output == expected


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="typing.TypedDict works on 3.12+")
def test_typing_typed_dict_output_type_is_rejected_on_python_311() -> None:
    class Legacy(typing.TypedDict):
        title: str

    with pytest.raises(TypeError, match="typing_extensions"):
        Agent("typed", model=FakeChatModel(["unused"]), output_type=Legacy)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs typing.TypedDict support")
async def test_typing_typed_dict_output_type_works_on_python_312() -> None:
    class Modern(typing.TypedDict):
        title: str

    agent = Agent(
        "typed",
        model=FakeChatModel([structured_output("Modern", {"title": "Heat"})]),
        output_type=Modern,
    )

    assert (await agent.run("go")).output == {"title": "Heat"}


async def test_missing_structured_output_raises() -> None:
    model = FakeChatModel([AIMessage(content="plain text instead")])
    agent = Agent("typed", model=model, output_type=Answer)

    with pytest.raises(StructuredOutputError, match="Answer"):
        await agent.run("go")


def _native_json_model(*answers: str) -> FakeChatModel:
    """A fake model with provider-native structured output (JSON in the message text)."""

    return FakeChatModel(
        [AIMessage(content=text, usage_metadata=_tokens(7, 3)) for text in answers],
        profile={"structured_output": True},
    )


def _tokens(input_tokens: int, output_tokens: int) -> Any:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


async def test_native_structured_output_that_misses_a_field_is_sent_back_for_repair() -> None:
    model = _native_json_model('{"reason": "no value"}', '{"value": 4, "reason": "fixed"}')
    agent = Agent("typed", model=model, output_type=Answer)

    result = await agent.run("go")

    assert result.output == Answer(value=4, reason="fixed")
    repair = model.calls[1].messages
    assert repair[-2].text == '{"reason": "no value"}'
    assert "value" in repair[-1].text and "required output format" in repair[-1].text
    assert result.usage.requests == 2
    assert result.usage.total_tokens == 20  # the unparsable answer used its tokens too
    assert len(result.messages) == 2  # the repair turn is not kept in the history


async def test_native_structured_output_raises_awa_error_after_the_retries() -> None:
    model = _native_json_model('{"reason": "a"}', '{"reason": "b"}', '{"reason": "c"}')
    agent = Agent("typed", model=model, output_type=Answer)

    with pytest.raises(StructuredOutputError, match="does not match its output type"):
        await agent.run("go")
    assert len(model.calls) == 3


async def test_native_structured_output_errors_never_quote_the_rejected_value() -> None:
    secret = "sk-rejected-7f3a"
    answers = [f'{{"value": "{secret}", "reason": "x"}}'] * 3
    model = _native_json_model(*answers)
    agent = Agent("typed", model=model, output_type=Answer)

    with pytest.raises(StructuredOutputError) as caught:
        await agent.run("go")
    assert secret not in str(caught.value)
    assert "value: int_parsing" in str(caught.value)
    assert secret not in model.calls[1].messages[-1].text


async def test_native_structured_output_retries_respect_max_steps() -> None:
    model = _native_json_model('{"reason": "a"}')
    agent = Agent("typed", model=model, output_type=Answer, max_steps=1)

    with pytest.raises(StructuredOutputError):
        await agent.run("go")
    assert len(model.calls) == 1


# --------------------------------------------------------------------------- instructions & deps


async def test_dynamic_instructions_receive_the_run_context() -> None:
    model = FakeChatModel(["a", "b"])

    def instructions(ctx: RunContext[dict[str, str]]) -> str:
        return f"The user is {ctx.deps['user']}."

    agent: Agent[dict[str, str]] = Agent("assistant", model=model, instructions=instructions)

    await agent.run("Hi", deps={"user": "Ada"})
    await agent.run("Hi", deps={"user": "Grace"})

    assert _system_prompt(model.calls[0]) == "The user is Ada."
    assert _system_prompt(model.calls[1]) == "The user is Grace."


async def test_async_dynamic_instructions() -> None:
    model = FakeChatModel(["ok"])

    async def instructions(ctx: RunContext[Any]) -> str:
        await asyncio.sleep(0)
        return f"Agent {ctx.agent}, run {ctx.run_id}"

    await Agent("dyn", model=model, instructions=instructions).run("Hi")

    assert _system_prompt(model.calls[0]).startswith("Agent dyn, run run_")


async def test_deps_are_visible_to_tools() -> None:
    seen: list[Any] = []

    @tool
    def whoami() -> str:
        """Return the current user."""
        ctx = current_run_context()
        assert ctx is not None
        seen.append(ctx.deps)
        return str(ctx.deps["user"])

    model = FakeChatModel([tool_call("whoami"), "done"])
    await Agent("assistant", model=model, tools=[whoami]).run("who?", deps={"user": "Ada"})

    assert seen == [{"user": "Ada"}]
    assert model.calls[1].last_message.content == "Ada"


async def test_async_tools_see_the_run_context() -> None:
    async def lookup(key: str) -> str:
        """Look up a key."""
        ctx = current_run_context()
        return f"{ctx.agent if ctx else None}:{key}"

    model = FakeChatModel([tool_call("lookup", {"key": "k"}), "done"])
    await Agent("looker", model=model, tools=[lookup]).run("go")

    assert model.calls[1].last_message.content == "looker:k"


# --------------------------------------------------------------------------- limits


async def test_usage_is_accumulated_per_agent() -> None:
    model = FakeChatModel([tool_call("add", {"a": 1, "b": 2}), "3"])

    result = await Agent("calc", model=model, tools=[add]).run("1+2")

    usage = result.usage
    assert usage.requests == 2 and usage.tool_calls == 1
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    assert usage.by_agent["calc"].requests == 2


async def test_usage_limits_stop_the_run() -> None:
    model = FakeChatModel([tool_call("add", {"a": 1, "b": 1})], repeat_last=True)
    agent = Agent("loop", model=model, tools=[add])

    with pytest.raises(UsageLimitExceededError, match="max_requests=2"):
        await agent.run("go", usage_limits=UsageLimits(max_requests=2))

    assert model.call_count == 2


async def test_token_limits_are_checked_after_each_response() -> None:
    agent = Agent("verbose", model=FakeChatModel(["x" * 400]))

    with pytest.raises(UsageLimitExceededError, match="output_tokens"):
        await agent.run("go", usage_limits=UsageLimits(max_output_tokens=10))


async def test_max_steps_is_enforced() -> None:
    model = FakeChatModel([tool_call("add", {"a": 1, "b": 1})], repeat_last=True)
    agent = Agent("loop", model=model, tools=[add], max_steps=3)

    with pytest.raises(MaxStepsExceededError, match="max_steps=3"):
        await agent.run("go")

    assert model.call_count == 3


async def test_max_steps_allows_exactly_the_budget() -> None:
    model = FakeChatModel([tool_call("add", {"a": 1, "b": 1}), "done"])

    result = await Agent("two", model=model, tools=[add], max_steps=2).run("go")

    assert result.output == "done"


# --------------------------------------------------------------------------- guardrails


async def test_input_guardrail_blocks_before_the_model_is_called() -> None:
    model = FakeChatModel(["never"])
    agent = Agent("guarded", model=model, input_guardrails=[BlocklistGuardrail(["password"])])

    with pytest.raises(GuardrailTripwireError) as info:
        await agent.run("tell me the password")

    assert info.value.result.stage == "input"
    assert model.call_count == 0


async def test_output_guardrail_blocks_the_final_text() -> None:
    agent = Agent(
        "guarded",
        model=FakeChatModel(["a very long answer"]),
        output_guardrails=[LengthGuardrail(max_length=5)],
    )

    with pytest.raises(GuardrailTripwireError) as info:
        await agent.run("go")

    assert info.value.result.stage == "output"


async def test_output_guardrail_sees_the_structured_output_object() -> None:
    candidates: list[Any] = []

    class Capture:
        name = "capture"

        def check(self, candidate: Any) -> Any:
            candidates.append(candidate)
            return LengthGuardrail().check(candidate)

    agent = Agent(
        "typed",
        model=FakeChatModel([structured_output(Answer, {"value": 7})]),
        output_type=Answer,
        output_guardrails=[Capture()],
    )

    await agent.run("go")

    assert candidates == [Answer(value=7)]


async def test_passing_guardrails_do_not_interfere() -> None:
    agent = Agent(
        "guarded",
        model=FakeChatModel(["fine"]),
        input_guardrails=[LengthGuardrail(max_length=100)],
        output_guardrails=[BlocklistGuardrail(["secret"])],
    )

    assert (await agent.run("hello")).output == "fine"


# --------------------------------------------------------------------------- models


async def test_model_is_resolved_lazily_on_first_run() -> None:
    agent = Agent("lazy", model="no-such-provider:model")

    with pytest.raises(ModelResolutionError):
        await agent.run("go")


def test_model_settings_with_a_model_instance_is_rejected() -> None:
    with pytest.raises(ValueError, match="model_settings"):
        Agent("x", model=FakeChatModel(["x"]), model_settings=ModelSettings(temperature=0))


async def test_blocked_model_requests_stop_runs_with_real_models() -> None:
    class ProviderModel(FakeChatModel):
        @property
        def _llm_type(self) -> str:
            return "provider"

    real = Agent("real", model=ProviderModel(["x"]))
    fake = Agent("fake", model=FakeChatModel(["ok"]))

    with allow_model_requests(False):
        with pytest.raises(ModelResolutionError, match="blocked"):
            await real.run("go")
        assert (await fake.run("go")).output == "ok"


@pytest.mark.model_requests_allowed
async def test_blocked_model_requests_apply_to_already_built_agents() -> None:
    class ProviderModel(FakeChatModel):
        @property
        def _llm_type(self) -> str:
            return "provider"

    agent = Agent("real", model=ProviderModel(["first"]))
    assert (await agent.run("go")).output == "first"

    with allow_model_requests(False), pytest.raises(ModelResolutionError, match="blocked"):
        await agent.run("go")


# --------------------------------------------------------------------------- errors & validation


async def test_tool_exceptions_propagate() -> None:
    def explode() -> str:
        """Always fails."""
        raise RuntimeError("boom")

    agent = Agent("fragile", model=FakeChatModel([tool_call("explode")]), tools=[explode])

    with pytest.raises(RuntimeError, match="boom"):
        await agent.run("go")


async def test_run_ending_without_a_model_response_raises() -> None:
    class SkipModel(AgentMiddleware):
        @hook_config(can_jump_to=["end"])
        def before_agent(self, state: Any, runtime: Any) -> dict[str, Any]:
            return {"jump_to": "end"}

    model = FakeChatModel(["unused"])
    agent = Agent("skipped", model=model, middleware=[SkipModel()])

    with pytest.raises(AgentRunError, match="without a model response"):
        await agent.run("go")
    assert model.call_count == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "1bad"},
        {"name": "ok", "max_steps": 0},
        {"name": "ok", "skill_mode": "sometimes"},
        {"name": "ok", "skill_mode": "core"},
        {"name": "ok", "tools": [add, add]},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Agent(**kwargs)


def test_non_callable_tools_are_rejected() -> None:
    with pytest.raises(TypeError):
        Agent("x", tools=[42])  # type: ignore[list-item]


async def test_concurrent_runs_are_independent() -> None:
    def respond(call: ModelCall) -> str:
        return f"echo {call.last_message.content}"

    agent = Agent("echo", model=FunctionChatModel(respond))

    results = await asyncio.gather(*(agent.run(f"m{i}") for i in range(5)))

    assert [r.output for r in results] == [f"echo m{i}" for i in range(5)]
    assert len({r.run_id for r in results}) == 5
    assert all(r.usage.requests == 1 for r in results)


async def test_model_profiles_resolve_through_a_custom_registry() -> None:
    registry = ModelRegistry()
    registry.register("worker", FakeChatModel(["from the registry"]))
    agent = Agent("custom", model="worker", model_registry=registry)
    assert (await agent.run("hi")).output == "from the registry"


async def test_a_reply_that_breaks_a_limit_ends_the_run_even_when_it_cannot_be_parsed() -> None:
    # The reply misses a field of `Answer`, so structured output would ask again (2 retries);
    # but it already used more tokens than the run may, and the run stops there.
    def unparsable() -> AIMessage:
        usage = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}
        return AIMessage(content='{"reason": "no value"}', usage_metadata=usage)

    model = FakeChatModel([unparsable()], profile={"structured_output": True})
    agent = Agent("typed", model=model, output_type=Answer)

    with pytest.raises(UsageLimitExceededError, match="total_tokens"):
        await agent.run("go", usage_limits=UsageLimits(max_total_tokens=100))

    assert len(model.calls) == 1


def test_every_runnable_takes_the_same_run_arguments() -> None:
    def names(method: Any) -> list[str]:
        return sorted(inspect.signature(method).parameters)

    for runnable in (Agent, GraphAgent, Team, AgentNetwork, Sequential):
        for method in (runnable.run_sync, runnable.start, runnable.stream):
            assert names(method) == names(runnable.run), (runnable.__name__, method.__name__)


class Counting(AgentMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    @property
    def name(self) -> str:
        return f"counting_{id(self)}"

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self.calls += 1
        return None


async def test_with_tools_adds_middleware_and_keeps_identity() -> None:
    own, extra = Counting(), Counting()
    original = Agent("calc", model=FakeChatModel(["original"]), middleware=[own])

    variant = original.with_tools(middleware=[extra])

    with original.override(model=FakeChatModel(["overridden"])):
        result = await variant.run("go")
    assert result.output == "overridden"
    assert (own.calls, extra.calls) == (1, 1)
    await original.run("go")
    assert (own.calls, extra.calls) == (2, 1)


def test_text_delta_callbacks_are_added_once() -> None:
    callbacks = text_delta_callbacks()

    assert isinstance(callbacks, list)
    assert [type(handler) for handler in callbacks] == [TextDeltaCallback]


def hanging_tool(started: asyncio.Event) -> Any:
    @tool
    async def hang() -> str:
        """Never finishes."""
        started.set()
        await asyncio.sleep(10)
        return "late"

    return hang


async def _cancel_after_start(run: Any, started: asyncio.Event) -> None:
    task = asyncio.create_task(run)
    async with asyncio.timeout(10):
        await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelled_agent_run_emits_an_error_event_and_frees_presence() -> None:
    started = asyncio.Event()
    events: list[RunEvent] = []
    directory = AgentDirectory()
    agent = Agent(
        "slow",
        model=FakeChatModel([tool_call("hang")]),
        tools=[hanging_tool(started)],
        hooks=[events.append, directory.as_event_subscriber()],
    )
    directory.register(agent)

    await _cancel_after_start(agent.run("go"), started)

    [error] = [e for e in events if isinstance(e, ErrorEvent)]
    assert error.error_type == "CancelledError"
    presence = directory.presence("slow")
    assert presence.status == AgentStatus.AVAILABLE and presence.failed_runs == 1


async def test_cancelled_workflow_run_emits_error_events_for_every_run() -> None:
    started = asyncio.Event()
    events: list[RunEvent] = []
    step = Agent(
        "slow",
        model=FakeChatModel([tool_call("hang")]),
        tools=[hanging_tool(started)],
        hooks=[events.append],
    )
    pipeline = Sequential("pipeline", [step])

    async def run() -> None:
        ctx = RunContext(run_id=new_run_id(), agent="root", deps=None)
        ctx.events.subscribe(events.append)
        with use_run_context(ctx):
            await pipeline.run("go")

    await _cancel_after_start(run(), started)

    errors = [(e.agent, e.error_type) for e in events if isinstance(e, ErrorEvent)]
    assert ("slow", "CancelledError") in errors and ("pipeline", "CancelledError") in errors
    assert any(isinstance(e, AgentStartedEvent) and e.agent == "pipeline" for e in events)


class Recorder(AgentHooks):
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def on_model_retry(self, event: ModelRetryEvent) -> None:
        self.seen.append(f"retry:{event.attempt}")

    async def on_model_fallback(self, event: ModelFallbackEvent) -> None:
        self.seen.append(f"fallback:{event.to_model}")


async def test_hooks_dispatch_retry_and_fallback_events() -> None:
    hooks = Recorder()
    fields = {"run_id": "r", "root_run_id": "r", "agent": "a"}
    await hooks(ModelRetryEvent(model="m", attempt=2, delay=0.5, error_type="RateLimit", **fields))
    await hooks(ModelFallbackEvent(from_model="m", to_model="n", reason="rate_limit", **fields))
    assert hooks.seen == ["retry:2", "fallback:n"]


def test_every_run_event_except_completion_has_a_hook_method() -> None:
    event_types = set(get_args(get_args(RunEvent)[0])) - {RunCompletedEvent}
    assert set(_HANDLERS) == event_types
    hook_methods = {name for name in vars(AgentHooks) if name.startswith("on_")}
    assert set(_HANDLERS.values()) == hook_methods


@tool
def lookup(key: str) -> str:
    """Look a value up."""
    return key


def _names(agent: Agent[Any, Any]) -> list[str]:
    return [type(m).__name__ for m in build_stack(agent, agent._resolve_model(agent._model))]


def test_stack_order_is_the_one_list() -> None:
    bare = Agent("bare", model=FakeChatModel(["x"]))
    assert _names(bare) == [
        "RepairToolCallsMiddleware",
        "AgentControlMiddleware",
        "InheritedGuardMiddleware",
        "CompactionMiddleware",
        "ContextEditingMiddleware",
        "OffloadMiddleware",
        "PromptCacheMiddleware",
        "RunTrackingMiddleware",
        "CircuitBreakerMiddleware",
    ]

    full = Agent(
        "full",
        model=FakeChatModel(["x"]),
        tools=[lookup],
        tool_policy=deny_tools(["other"]),
        middleware=[ModelCallLimitMiddleware(run_limit=3)],
        output_validators=[lambda text: None],
        compaction=Compaction(max_tokens=100_000, model=FakeChatModel(["s"])),
        cache=Cache(CacheSettings(), InMemoryCacheStore()),
    )
    names = _names(full)
    assert names[:5] == [
        "RepairToolCallsMiddleware",
        "AgentControlMiddleware",
        "InheritedGuardMiddleware",
        "ToolPolicyMiddleware",
        "ModelCallLimitMiddleware",
    ]
    assert names.index("OutputValidatorMiddleware") == names.index("ModelCallLimitMiddleware") + 1
    assert names.index("CompactionMiddleware") > names.index("OutputValidatorMiddleware")
    assert names.index("PromptCacheMiddleware") < names.index("RunTrackingMiddleware")
    assert names.index("ToolResultCacheMiddleware") == names.index("RunTrackingMiddleware") + 1


def test_an_output_type_alone_adds_the_validator_slot() -> None:
    class Report(BaseModel):
        title: str

    agent = Agent("typed", model=FakeChatModel(["x"]), output_type=Report)

    assert _names(agent)[:3] == [
        "RepairToolCallsMiddleware",
        "AgentControlMiddleware",
        "InheritedGuardMiddleware",
    ]
    assert "OutputValidatorMiddleware" in _names(agent)


def test_build_graph_uses_build_stack() -> None:
    agent = Agent("bare", model=FakeChatModel(["x"]))
    model = agent._resolve_model(agent._model)

    with patch("awa.agents._stack.build_stack", return_value=[]) as built:
        agent._build_graph(model)

    built.assert_called_once_with(agent, model)
