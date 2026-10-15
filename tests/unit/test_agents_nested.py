"""Agents inside agents: nested runs and composition."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt
from pydantic import BaseModel

from awa.agents import Agent, AgentHooks
from awa.contracts import (
    AgentFinishedEvent,
    AgentStartedEvent,
    ErrorEvent,
    RunEvent,
    ToolCallEvent,
    UsageLimits,
)
from awa.errors import (
    AgentRunError,
    ModelResolutionError,
    SkillNotFoundError,
    UsageLimitExceededError,
)
from awa.runtime import RunContext, current_run_context
from awa.skills import Skill, SkillRegistry
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def _then(first: Any, answer: str | None) -> FakeChatModel:
    """A script of `first` and then `answer`, or only `first` for a run that stops before the answer."""

    return FakeChatModel([first] if answer is None else [first, answer])


def _adder(answer: str | None = "three", **kwargs: Any) -> Agent:
    return Agent(
        "adder",
        description="Adds numbers.",
        model=_then(tool_call("add", {"a": 1, "b": 2}), answer),
        tools=[add],
        **kwargs,
    )


def _delegator(
    inner: Agent[Any, Any], final: str | None = "The answer is three.", **kwargs: Any
) -> Agent:
    return Agent(
        "lead",
        model=_then(tool_call(inner.name, {"task": "What is 1+2?"}), final),
        tools=[inner.as_tool()],
        **kwargs,
    )


# --------------------------------------------------------------------------- as_tool


def test_as_tool_schema_and_description() -> None:
    agent_tool = _adder().as_tool()

    assert agent_tool.name == "adder"
    assert agent_tool.description == "Adds numbers."
    assert list(agent_tool.args) == ["task"]

    renamed = _adder().as_tool(name="ask_adder", description="Ask the adder.")
    assert (renamed.name, renamed.description) == ("ask_adder", "Ask the adder.")


async def test_as_tool_returns_the_final_text_to_the_caller() -> None:
    inner = _adder()
    outer = _delegator(inner)

    result = await outer.run("go")

    assert result.output == "The answer is three."
    tool_message = result.messages[2]
    assert tool_message.content == "three"


async def test_as_tool_returns_structured_output_as_json() -> None:
    class Sum(BaseModel):
        total: int

    inner = Agent(
        "summer", model=FakeChatModel([structured_output(Sum, {"total": 3})]), output_type=Sum
    )
    result = await _delegator(inner).run("go")

    assert result.messages[2].content == '{"total":3}'


async def test_nested_run_shares_usage_and_emits_child_events() -> None:
    events: list[RunEvent] = []
    outer = _delegator(_adder(), hooks=[events.append])

    result = await outer.run("go")

    assert result.usage.requests == 4
    assert result.usage.by_agent["adder"].requests == 2
    assert result.usage.by_agent["lead"].requests == 2
    child_start = next(e for e in events if isinstance(e, AgentStartedEvent) and e.agent == "adder")
    assert child_start.depth == 1
    assert child_start.parent_run_id == result.run_id
    assert child_start.root_run_id == result.run_id
    assert child_start.input_preview == "What is 1+2?"
    assert {e.depth for e in events if e.agent == "adder"} == {1}


async def test_nested_runs_share_usage_limits() -> None:
    outer = _delegator(_adder(answer=None), final=None)

    with pytest.raises(UsageLimitExceededError):
        await outer.run("go", usage_limits=UsageLimits(max_requests=2))


async def test_nested_runs_inherit_deps_unless_overridden() -> None:
    seen: list[Any] = []

    @tool
    def capture() -> str:
        """Record the deps."""
        ctx = current_run_context()
        assert ctx is not None
        seen.append((ctx.agent, ctx.deps))
        return "ok"

    inner = Agent(
        "inner",
        model=FakeChatModel([tool_call("capture"), "done", tool_call("capture"), "done"]),
        tools=[capture],
    )

    @tool
    async def call_inner(task: str) -> str:
        """Call the inner agent twice."""
        await inner.run(task)
        await inner.run(task, deps="explicit")
        return "done"

    outer = Agent(
        "outer",
        model=FakeChatModel([tool_call("call_inner", {"task": "t"}), "end"]),
        tools=[call_inner],
    )

    await outer.run("go", deps="inherited")

    assert seen == [("inner", "inherited"), ("inner", "explicit")]


async def test_usage_limits_are_rejected_for_nested_runs() -> None:
    inner = Agent("inner", model=FakeChatModel(["x"]))

    @tool
    async def call_inner(task: str) -> str:
        """Call the inner agent with its own limits."""
        await inner.run(task, usage_limits=UsageLimits(max_requests=1))
        return "unreachable"

    outer = Agent(
        "outer", model=FakeChatModel([tool_call("call_inner", {"task": "t"})]), tools=[call_inner]
    )

    with pytest.raises(ValueError, match="top-level run"):
        await outer.run("go")


async def test_nested_failures_emit_error_events_for_every_run() -> None:
    def explode() -> str:
        """Fails."""
        raise RuntimeError("boom")

    inner = Agent("inner", model=FakeChatModel([tool_call("explode")]), tools=[explode])
    events: list[RunEvent] = []
    outer = _delegator(inner, final=None, hooks=[events.append])

    with pytest.raises(RuntimeError, match="boom"):
        await outer.run("go")

    assert [(e.agent, e.depth) for e in events if isinstance(e, ErrorEvent)] == [
        ("inner", 1),
        ("lead", 0),
    ]


# --------------------------------------------------------------------------- hooks scoping


async def test_hooks_of_a_nested_agent_only_see_its_own_subtree() -> None:
    inner_events: list[RunEvent] = []
    outer_events: list[RunEvent] = []
    inner = _adder(hooks=[inner_events.append])
    outer = _delegator(inner, hooks=[outer_events.append])

    await outer.run("go")

    assert {e.agent for e in inner_events} == {"adder"}
    assert {e.agent for e in outer_events} == {"lead", "adder"}
    assert isinstance(inner_events[0], AgentStartedEvent)
    assert isinstance(inner_events[-1], AgentFinishedEvent)


async def test_agent_hooks_dispatch_by_event_type() -> None:
    calls: list[str] = []

    class All(AgentHooks):
        async def on_agent_start(self, event: AgentStartedEvent) -> None:
            calls.append("start")

        async def on_tool_call(self, event: ToolCallEvent) -> None:
            calls.append(event.tool_name)

    hooks = All()
    fields = {"run_id": "r", "root_run_id": "r", "agent": "a"}
    await hooks(AgentStartedEvent(**fields))
    await hooks(ToolCallEvent(tool_name="t", call_id="c", **fields))
    await hooks(ErrorEvent(error_type="E", message="m", **fields))  # default no-op

    assert calls == ["start", "t"]


# --------------------------------------------------------------------------- nested interrupts


def ask(question: str) -> str:
    """Ask a human."""
    return f"human said {interrupt(question)}"


def _asker(answer: str | None = "approved", **kwargs: Any) -> Agent:
    return Agent(
        "asker",
        description="Asks a human.",
        model=_then(tool_call("ask", {"question": "ok?"}), answer),
        tools=[ask],
        **kwargs,
    )


async def test_nested_interrupt_pauses_a_checkpointed_top_level_run() -> None:
    outer = _delegator(_asker(), final="done", checkpointer=InMemorySaver())

    paused = await outer.run("go")

    assert paused.is_interrupted and paused.output is None
    assert paused.interrupts[0]["value"] == "ok?"
    assert paused.thread_id is not None

    resumed = await outer.resume(paused.thread_id, "yes")

    assert resumed.output == "done"
    assert [m.content for m in resumed.messages][-2:] == ["approved", "done"]


async def test_nested_interrupt_without_any_checkpointer_raises() -> None:
    events: list[RunEvent] = []
    outer = _delegator(_asker(answer=None), final=None, hooks=[events.append])

    with pytest.raises(AgentRunError, match="checkpointer"):
        await outer.run("go")

    error = next(e for e in events if isinstance(e, ErrorEvent) and e.agent == "asker")
    assert error.error_type == "AgentRunError" and error.depth == 1


async def test_interrupt_of_an_as_tool_agent_on_its_own_thread_raises() -> None:
    outer = _delegator(_asker(answer=None, checkpointer=InMemorySaver()), final=None)

    with pytest.raises(AgentRunError, match="own thread"):
        await outer.run("go")


async def test_run_context_is_restored_after_a_run() -> None:
    assert current_run_context() is None
    await _adder().run("go")
    assert current_run_context() is None


async def test_run_context_type_is_exposed() -> None:
    contexts: list[RunContext[Any]] = []

    def capture(ctx: RunContext[Any]) -> str:
        contexts.append(ctx)
        return ""

    await Agent("ctx", model=FakeChatModel(["x"]), instructions=capture).run("go", deps=5)

    assert contexts[0].deps == 5 and contexts[0].depth == 0


class Report(BaseModel):
    title: str


def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


def _system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


# --------------------------------------------------------------------------- card


def test_card_describes_the_agent() -> None:
    agent = Agent(
        "analyst",
        description="Analyzes data.",
        model="openai:gpt-5-mini",
        tools=[add],
        output_type=Report,
        capabilities=["analysis"],
        tags=["internal"],
        role="specialist",
        metadata={"owner": "data"},
        version="2.0.0",
    )

    card = agent.card
    assert card.name == "analyst"
    assert card.description == "Analyzes data."
    assert card.tools == ("add",)
    assert card.capabilities == ("analysis",)
    assert card.tags == ("internal",)
    assert card.role == "specialist"
    assert card.model == "openai:gpt-5-mini"
    assert card.version == "2.0.0"
    assert card.metadata == {"owner": "data"}
    assert card.output_schema is not None
    assert card.output_schema["properties"]["title"]["type"] == "string"
    assert card.input_schema is None


def test_card_model_names() -> None:
    assert Agent("a").card.model == "default"
    assert Agent("b", model=FakeChatModel(["x"])).card.model == "FakeChatModel"


def test_properties_and_repr() -> None:
    agent = Agent("helper", description="Helps.", tools=[add])

    assert agent.name == "helper"
    assert agent.description == "Helps."
    assert [t.name for t in agent.tools] == ["add"]
    assert agent.output_type is None
    assert repr(agent) == "Agent(name='helper', model='default', tools=1)"


def test_building_an_agent_needs_no_model_or_credentials() -> None:
    agent = Agent("offline", model="anthropic:claude-sonnet-5")

    assert agent.card.model == "anthropic:claude-sonnet-5"


# --------------------------------------------------------------------------- clone / with_tools


async def test_clone_applies_overrides_without_mutating_the_original() -> None:
    original = Agent("base", description="Original.", model=FakeChatModel(["a"]), tools=[add])

    copy = original.clone(name="copy", description="Copy.", model=FakeChatModel(["b"]))

    assert (copy.name, copy.description) == ("copy", "Copy.")
    assert [t.name for t in copy.tools] == ["add"]
    assert (original.name, original.description) == ("base", "Original.")
    assert (await copy.run("go")).output == "b"
    assert (await original.run("go")).output == "a"


def test_clone_rejects_unknown_arguments() -> None:
    with pytest.raises(TypeError):
        Agent("base").clone(colour="blue")


async def test_with_tools_adds_tools_and_instructions_without_mutating() -> None:
    model = FakeChatModel([tool_call("multiply", {"a": 2, "b": 3}), "6"])
    original = Agent("calc", instructions="Compute.", model=model, tools=[add])

    extended = original.with_tools(multiply, extra_instructions="You may multiply.")

    assert [t.name for t in extended.tools] == ["add", "multiply"]
    assert [t.name for t in original.tools] == ["add"]
    assert extended.card.tools == ("add", "multiply")
    result = await extended.run("2*3")
    assert result.output == "6"
    assert _system_prompt(model.calls[0]) == "Compute.\n\nYou may multiply."


async def test_with_tools_appends_to_dynamic_instructions() -> None:
    model = FakeChatModel(["ok"])

    def instructions(ctx: RunContext[str]) -> str:
        return f"Serve {ctx.deps}."

    agent: Agent[str] = Agent("dyn", instructions=instructions, model=model)

    await agent.with_tools(extra_instructions="Be brief.").run("go", deps="Ada")

    assert _system_prompt(model.calls[0]) == "Serve Ada.\n\nBe brief."


async def test_with_tools_on_empty_instructions() -> None:
    model = FakeChatModel(["ok"])
    agent = Agent("plain", model=model).with_tools(add, extra_instructions="Use add.")

    await agent.run("go")

    assert _system_prompt(model.calls[0]) == "Use add."


def test_with_tools_rejects_duplicate_tool_names() -> None:
    with pytest.raises(ValueError, match="duplicate tool names: add"):
        Agent("calc", tools=[add]).with_tools(add)


# --------------------------------------------------------------------------- override


async def test_override_replaces_the_model_inside_the_block_only() -> None:
    agent = Agent("assistant", model=FakeChatModel(["real"], repeat_last=True))

    with agent.override(model=FakeChatModel(["stub"])):
        assert (await agent.run("go")).output == "stub"

    assert (await agent.run("go")).output == "real"


async def test_override_replaces_deps() -> None:
    seen: list[Any] = []

    def instructions(ctx: RunContext[str]) -> str:
        seen.append(ctx.deps)
        return ""

    agent: Agent[str] = Agent(
        "assistant", instructions=instructions, model=FakeChatModel(["x"], repeat_last=True)
    )

    with agent.override(deps="test-deps"):
        await agent.run("go", deps="real-deps")
    await agent.run("go", deps="real-deps")

    assert seen == ["test-deps", "real-deps"]


async def test_override_applies_to_nested_runs() -> None:
    member = Agent("member", description="Helps.", model=FakeChatModel(["real member"]))
    lead = Agent(
        "lead",
        model=FakeChatModel([tool_call("member", {"task": "help"}), "done"]),
        tools=[member.as_tool()],
    )

    with member.override(model=FakeChatModel(["stub member"])):
        result = await lead.run("go")

    assert result.messages[2].content == "stub member"


async def test_overrides_stack_and_follow_with_tools_but_not_clones() -> None:
    agent = Agent("assistant", model=FakeChatModel(["real"], repeat_last=True))
    variant = agent.with_tools(add)
    clone = agent.clone()

    with agent.override(model=FakeChatModel(["outer"], repeat_last=True)):
        with agent.override(deps="d"):
            assert (await agent.run("go")).output == "outer"
        assert (await variant.run("go")).output == "outer"
        assert (await clone.run("go")).output == "real"
    with variant.override(model=FakeChatModel(["from variant"])):
        assert (await agent.run("go")).output == "from variant"


async def test_override_resolves_a_model_string() -> None:
    agent = Agent("assistant", model=FakeChatModel(["real"]))

    with (
        agent.override(model="no-such-provider:model"),
        pytest.raises(ModelResolutionError, match="no-such-provider"),
    ):
        await agent.run("go")


# --------------------------------------------------------------------------- skills


def _skill(name: str, **kwargs: Any) -> Skill:
    return Skill(
        name=name, description=f"The {name} skill.", instructions=f"Apply {name}.", **kwargs
    )


async def test_inline_skills_are_appended_to_the_prompt() -> None:
    model = FakeChatModel(["ok"])
    agent = Agent("writer", instructions="Write.", model=model, skills=[_skill("tone")])

    await agent.run("go")

    prompt = _system_prompt(model.calls[0])
    assert prompt.startswith("Write.")
    assert "## Skill: tone\nApply tone." in prompt
    assert agent.card.skills == ("tone",)
    assert agent.tools == ()


async def test_inline_skills_with_dynamic_instructions() -> None:
    model = FakeChatModel(["ok"])

    def instructions(ctx: RunContext[Any]) -> str:
        return f"Hello {ctx.deps}."

    agent = Agent("writer", instructions=instructions, model=model, skills=[_skill("tone")])
    await agent.run("go", deps="Ada")

    assert _system_prompt(model.calls[0]).startswith("Hello Ada.\n\n## Skill: tone")


async def test_on_demand_skills_add_an_index_and_skill_tools() -> None:
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("load_skill", {"name": "tone"})
        return f"loaded: {call.last_message.content}"

    model = FunctionChatModel(respond)
    agent = Agent(
        "writer",
        instructions="Write.",
        model=model,
        skills=[_skill("tone"), _skill("length")],
        skill_mode="on_demand",
    )

    result = await agent.run("go")

    prompt = _system_prompt(model.calls[0])
    assert "## Skill: tone" not in prompt
    assert "`tone`: The tone skill." in prompt and "load_skill" in prompt
    assert [t.name for t in agent.tools] == ["load_skill"]  # no resources to read
    assert "Apply tone." in str(result.output)
    assert agent.card.skills == ("tone", "length")


def test_skill_names_resolve_in_the_given_registry() -> None:
    registry = SkillRegistry([_skill("custom")])

    agent = Agent("writer", skills=["custom"], skill_registry=registry)

    assert agent.card.skills == ("custom",)
    with pytest.raises(SkillNotFoundError):
        Agent("writer", skills=["missing"], skill_registry=registry)


async def test_builtin_skills_are_the_default_registry() -> None:
    model = FakeChatModel(["ok"])
    agent = Agent("lead", model=model, skills=["delegation-briefs", "orchestration-planning"])

    await agent.run("go")

    assert agent.card.skills == ("delegation-briefs", "orchestration-planning")
    assert "## Skill: delegation-briefs" in _system_prompt(model.calls[0])


async def test_duplicate_skills_are_rendered_once() -> None:
    model = FakeChatModel(["ok"])
    tone = _skill("tone")
    agent = Agent(
        "writer", model=model, skills=[tone, "tone"], skill_registry=SkillRegistry([tone])
    )

    await agent.run("go")

    assert agent.card.skills == ("tone",)
    assert _system_prompt(model.calls[0]).count("## Skill: tone") == 1


def test_inline_skills_with_resources_get_a_resource_tool() -> None:
    agent = Agent("reviewer", skills=["code-review", "writing"])

    assert [t.name for t in agent.tools] == ["read_skill_resource"]


# --------------------------------------------------------------------------- tools


async def test_langchain_tools_are_used_as_is() -> None:
    @tool
    def ping() -> str:
        """Reply with pong."""
        ctx = current_run_context()
        return f"pong from {ctx.agent if ctx else '?'}"

    model = FakeChatModel([tool_call("ping"), "done"])
    agent = Agent("pinger", model=model, tools=[ping])

    await agent.run("go")

    assert agent.tools == (ping,)
    assert model.calls[1].last_message.content == "pong from pinger"
