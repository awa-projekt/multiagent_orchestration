"""Adapters: foreign LangGraph graphs as awa agents, LangChain tools, toolkits, functions and runnables, and imported skill folders and Markdown prompts."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, BaseToolkit, Tool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field

from awa import Agent, Team
from awa._internal.util import import_path
from awa.agents.graph_agent import GraphAgent, from_langgraph
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    DelegationEvent,
    ModelResponseEvent,
    RunCompletedEvent,
    RunEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.contracts.protocols import AgentLike
from awa.contracts.usage import UsageLimits
from awa.errors import (
    AdapterError,
    ConfigurationError,
    ModelResolutionError,
    UsageLimitExceededError,
)
from awa.skills import Skill, SkillRegistry
from awa.skills.importing import (
    REVIEW_METADATA_KEY,
    SOURCE_METADATA_KEY,
    import_skills,
    render_skill_markdown,
)
from awa.testing import FakeChatModel, tool_call
from awa.tools import ToolRisk, declared_tool_risk, mark_tool_risk
from awa.tools.importing import (
    APPROVAL_METADATA_KEY,
    IMPORT_METADATA_KEY,
    ToolImportReport,
    from_langchain,
    from_langchain_tool,
    import_langchain_tools,
)
from awa.tools.risk import infer_description_risk, infer_tool_risk
from awa.workflows import Sequential


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool
def issue_refund(order_id: str) -> str:
    """Issue a refund for an order."""
    return f"refunded {order_id}"


def calculator(*script: Any, **options: Any) -> Any:
    return create_agent(
        FakeChatModel(list(script)), tools=[add], system_prompt="You add.", name="calc", **options
    )


def chat_graph(model: BaseChatModel, **compile_options: Any) -> Any:
    """A hand-built graph: one `chatbot` node calling the model on the messages."""

    async def chatbot(state: MessagesState) -> dict[str, Any]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("chatbot", chatbot)
    builder.add_edge(START, "chatbot")
    builder.add_edge("chatbot", END)
    return builder.compile(**compile_options)


async def test_a_create_agent_graph_runs_as_an_agent_with_usage_and_events() -> None:
    agent = from_langgraph(
        calculator(tool_call("add", {"a": 1, "b": 2}), "It is 3."),
        name="calc",
        description="Adds numbers.",
        capabilities=["math"],
    )
    assert isinstance(agent, AgentLike)
    assert agent.card.tools == ("add",)
    assert agent.card.tags == ("graph",)
    assert agent.card.capabilities == ("math",)

    events: list[RunEvent] = [event async for event in agent.stream("What is 1+2?")]
    result = next(e.result for e in events if isinstance(e, RunCompletedEvent))

    assert result.output == "It is 3."
    assert result.agent == "calc"
    assert result.usage.requests == 2
    assert result.usage.total_tokens > 0
    assert result.usage.by_agent["calc"].tool_calls == 1
    assert [m.type for m in result.new_messages()] == ["human", "ai", "tool", "ai"]
    kinds = [type(e) for e in events]
    assert kinds[0] is AgentStartedEvent
    assert kinds.count(ModelResponseEvent) == 2
    call = next(e for e in events if isinstance(e, ToolCallEvent))
    outcome = next(e for e in events if isinstance(e, ToolResultEvent))
    assert (call.tool_name, call.arguments) == ("add", {"a": 1, "b": 2})
    assert (outcome.tool_name, outcome.content, outcome.call_id) == ("add", "3", call.call_id)
    assert isinstance(events[-2], AgentFinishedEvent)


async def test_a_graph_member_takes_delegations_in_a_team() -> None:
    member = from_langgraph(
        calculator(tool_call("add", {"a": 2, "b": 2}), "Four."),
        name="calc",
        description="Adds numbers.",
    )
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "calc", "task": "Add 2 and 2"}), "It is four."]
        ),
    )
    events: list[RunEvent] = []
    team = Team("desk", orchestrator=lead, members=[member], hooks=[events.append])
    result = await team.run("What is 2+2?")

    assert result.output == "It is four."
    assert set(result.usage.by_agent) == {"lead", "calc"}
    assert result.usage.requests == 4
    assert any(isinstance(e, DelegationEvent) and e.target == "calc" for e in events)
    member_responses = [
        e for e in events if isinstance(e, ModelResponseEvent) and e.agent == "calc"
    ]
    assert len(member_responses) == 2
    assert team.agent("calc") is member


async def test_threads_interrupts_and_resume_use_the_graph_checkpointer() -> None:
    (guarded,) = from_langchain(issue_refund)
    graph = create_agent(
        FakeChatModel(
            [tool_call("issue_refund", {"order_id": "A-1"}), "Refunded.", "Anything else?"]
        ),
        tools=[guarded],
        checkpointer=InMemorySaver(),
        name="refunds",
    )
    agent = from_langgraph(graph, name="refunds")
    assert agent.has_checkpointer

    paused = await agent.run("Refund order A-1")
    assert paused.is_interrupted and paused.output is None
    assert paused.interrupts[0]["value"]["tool"] == "issue_refund"
    assert paused.thread_id is not None

    done = await agent.resume(paused.thread_id, {"action": "approve"})
    assert done.output == "Refunded."
    assert any(m.content == "refunded A-1" for m in done.messages)

    follow_up = await agent.run("Thanks", thread_id=paused.thread_id)
    assert follow_up.output == "Anything else?"
    assert [m.type for m in follow_up.new_messages()] == ["human", "ai"]
    assert follow_up.new_messages_start == len(done.messages)


async def test_a_graph_member_interrupt_pauses_a_checkpointed_team() -> None:
    (guarded,) = from_langchain(issue_refund)
    graph = create_agent(
        FakeChatModel([tool_call("issue_refund", {"order_id": "B-2"}), "Refunded B-2."]),
        tools=[guarded],
        name="refunds",
    )
    member = from_langgraph(graph, name="refunds", description="Issues refunds.")
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "refunds", "task": "Refund B-2"}), "Done."]
        ),
    )
    team = Team("desk", orchestrator=lead, members=[member], checkpointer=InMemorySaver())

    paused = await team.run("Refund B-2")
    assert paused.is_interrupted
    assert paused.interrupts[0]["value"]["tool"] == "issue_refund"

    done = await team.resume(paused.thread_id or "", {"action": "approve"})
    assert done.output == "Done."


async def test_a_custom_state_graph_streams_text_and_reads_an_output_channel() -> None:
    model = FakeChatModel(["Hello there, friend."], stream_chunk_size=5)
    agent = from_langgraph(chat_graph(model), name="greeter")
    assert agent.card.tools == ()

    events = [event async for event in agent.stream("Hi")]
    deltas = "".join(e.delta for e in events if isinstance(e, TextDeltaEvent))
    result = next(e.result for e in events if isinstance(e, RunCompletedEvent))

    assert deltas == "Hello there, friend."
    assert result.output == "Hello there, friend."
    assert result.usage.requests == 1

    class SummaryState(MessagesState):
        summary: str

    def summarise(state: SummaryState) -> dict[str, Any]:
        return {"messages": [AIMessage("done")], "summary": f"{len(state['messages'])} message(s)"}

    builder = StateGraph(SummaryState)
    builder.add_node("summarise", summarise)
    builder.add_edge(START, "summarise")
    builder.add_edge("summarise", END)
    summary = from_langgraph(builder.compile(), name="summary", output_key="summary")
    outcome = await summary.run("a", history=[{"role": "user", "content": "earlier"}])
    assert outcome.output == "2 message(s)"
    assert outcome.text == "done"
    assert outcome.new_messages_start == 1


async def test_start_runs_in_the_background_with_history() -> None:
    agent = from_langgraph(chat_graph(FakeChatModel(["noted"])), name="notes")

    handle = agent.start("and now?", history=[{"role": "user", "content": "earlier"}])
    result = await handle.result()

    assert handle.run_id == result.run_id and result.output == "noted"
    assert [m.text for m in result.messages[:2]] == ["earlier", "and now?"]
    assert result.new_messages_start == 1


async def test_structured_responses_and_workflow_steps() -> None:
    class Answer(BaseModel):
        value: int

    graph = create_agent(
        FakeChatModel([tool_call("Answer", {"value": 5})], repeat_last=True),
        response_format=Answer,
        name="typed",
    )
    typed = from_langgraph(graph, name="typed")
    result = await typed.run("Five?")
    assert result.output == Answer(value=5)

    echo = from_langgraph(chat_graph(FakeChatModel(["step two"])), name="second")
    pipeline = Sequential("pipe", [typed, echo], description="Typed then echo.")
    assert (await pipeline.run("go")).output == "step two"


async def test_limits_and_the_real_model_guard_apply_to_graph_model_calls() -> None:
    agent = from_langgraph(calculator(tool_call("add", {"a": 1, "b": 1})), name="calc")
    with pytest.raises(UsageLimitExceededError):
        await agent.run("1+1", usage_limits=UsageLimits(max_requests=1))

    class RealModel(FakeChatModel):
        @property
        def _llm_type(self) -> str:
            return "real-provider"

    guarded = from_langgraph(chat_graph(RealModel(responses=["hi"])), name="real")
    with pytest.raises(ModelResolutionError, match="Real model requests are blocked"):
        await guarded.run("Hi")


async def test_a_graph_as_a_tool_of_an_awa_agent() -> None:
    member = from_langgraph(chat_graph(FakeChatModel(["42"])), name="oracle", description="Knows.")
    asker = Agent(
        "asker",
        model=FakeChatModel([tool_call("oracle", {"task": "The answer?"}), "It says 42."]),
        tools=[member.as_tool()],
    )
    result = await asker.run("Ask the oracle")
    assert result.output == "It says 42."
    assert set(result.usage.by_agent) == {"asker", "oracle"}


def test_invalid_graphs_and_options_are_rejected() -> None:
    with pytest.raises(AdapterError, match="compiled LangGraph graph"):
        GraphAgent(object(), name="x")  # type: ignore[arg-type]
    with pytest.raises(AdapterError, match="no 'input' channel"):
        from_langgraph(chat_graph(FakeChatModel(["x"])), name="x", input_key="input")
    with pytest.raises(AdapterError, match="no 'result' channel"):
        from_langgraph(chat_graph(FakeChatModel(["x"])), name="x", output_key="result")
    with pytest.raises(ValueError, match="Invalid agent name"):
        from_langgraph(chat_graph(FakeChatModel(["x"])), name="not valid")
    stateless = from_langgraph(chat_graph(FakeChatModel(["x"])), name="x")
    with pytest.raises(ConfigurationError, match="no checkpointer"):
        stateless.run_sync("hi", thread_id="t1")


def test_from_langgraph_accepts_an_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = f"awa_test_graph_{uuid4().hex[:8]}"
    (tmp_path / f"{module}.py").write_text(
        "from langchain.agents import create_agent\n"
        "from awa.testing import FakeChatModel\n"
        "graph = create_agent(FakeChatModel(['hi']), name='remote')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    agent = from_langgraph(f"{module}:graph", name="remote")
    assert agent.run_sync("hello").output == "hi"


async def test_from_langgraph_runs_a_create_agent_graph_and_resumes() -> None:
    graph = calculator(
        tool_call("add", {"a": 3, "b": 4}), "Seven.", "Eight.", checkpointer=InMemorySaver()
    )
    agent = from_langgraph(graph, name="calc")
    assert agent.card.tools == ("add",)
    assert agent.card.model is None

    first = await agent.run("3+4?")
    assert first.output == "Seven."
    assert first.thread_id is not None
    assert any(m.content == "7" for m in first.messages)

    second = await agent.run("And one more?", thread_id=first.thread_id)
    assert second.output == "Eight."
    assert second.new_messages_start == len(first.messages)


async def test_a_hand_built_graph_agent_has_the_run_tree_request_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWA_RUN_MAX_REQUESTS", "4")
    model = FakeChatModel(["again"], repeat_last=True)

    async def chatbot(state: MessagesState) -> dict[str, Any]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("chatbot", chatbot)
    builder.add_edge(START, "chatbot")
    builder.add_edge("chatbot", "chatbot")
    agent = from_langgraph(builder.compile(), name="looper", recursion_limit=1_000)

    with pytest.raises(UsageLimitExceededError):
        await agent.run("go")
    assert len(model.calls) == 4


class PageArgs(BaseModel):
    url: str = Field(description="Address of the page.")


class FetchPage(BaseTool):
    name: str = "fetch_page"
    description: str = "Fetch a page and return its text."
    args_schema: type[BaseModel] = PageArgs

    def _run(self, url: str) -> str:
        return f"<html>{url}</html>"


@tool
def send_email(to: str, body: str) -> str:
    """Send an e-mail to a recipient."""
    return f"sent to {to}"


class MathToolkit(BaseToolkit):
    def get_tools(self) -> list[BaseTool]:
        return [add, FetchPage()]


def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b


def undocumented(a: int) -> int:
    return a


@pytest.fixture
def tool_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A module on `sys.path` defining a tool class, a toolkit class, a factory and a tool."""

    name = f"awa_test_tools_{uuid4().hex[:8]}"
    (tmp_path / f"{name}.py").write_text(
        textwrap.dedent(
            '''
            from langchain_core.tools import BaseTool, BaseToolkit, tool

            class WebSearch(BaseTool):
                name: str = "web_search"
                description: str = "Search the web."
                max_results: int = 3

                def _run(self, query: str) -> str:
                    return f"{self.max_results} results for {query}"

            @tool
            def lookup_customer(customer_id: str) -> str:
                """Look up a customer record."""
                return customer_id

            class CrmToolkit(BaseToolkit):
                region: str = "eu"

                def get_tools(self) -> list[BaseTool]:
                    return [lookup_customer]

            def make_tools() -> list[BaseTool]:
                return [lookup_customer, WebSearch()]

            def ping() -> str:
                """Answer pong."""
                return "pong"
            '''
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    sys.modules.pop(name, None)


def test_a_base_tool_keeps_its_name_description_and_schema_and_gets_its_risk() -> None:
    (fetched,) = from_langchain(FetchPage())

    assert fetched.name == "fetch_page"
    assert fetched.description == "Fetch a page and return its text."
    assert fetched.args_schema is PageArgs
    assert declared_tool_risk(fetched) == ToolRisk(
        untrusted_input=True, private_data=False, external_action=False
    )
    assert fetched.metadata is not None
    assert fetched.metadata[IMPORT_METADATA_KEY] == "BaseTool"
    assert APPROVAL_METADATA_KEY not in fetched.metadata
    assert fetched.invoke({"url": "x"}) == "<html>x</html>"


def test_external_actions_are_gated_with_approval_unless_opted_out() -> None:
    report = import_langchain_tools(send_email, add)

    email = report.entry("send_email")
    assert email.requires_approval
    assert email.approval_reason == "inferred: external action (name)"
    assert email.tool.metadata is not None and email.tool.metadata[APPROVAL_METADATA_KEY]
    assert email.tool.args == send_email.args
    assert not report.entry("add").requires_approval

    opted_out = import_langchain_tools(send_email, requires_approval=False).entry("send_email")
    assert not opted_out.requires_approval
    assert opted_out.approval_reason == "opted out"
    assert opted_out.tool.invoke({"to": "a", "body": "b"}) == "sent to a"

    forced = import_langchain_tools(add, requires_approval=True).entry("add")
    assert forced.requires_approval and forced.approval_reason == "requested"


async def test_a_gated_tool_pauses_the_agent_until_a_human_approves() -> None:
    (guarded,) = from_langchain(send_email)
    model = FakeChatModel([tool_call("send_email", {"to": "ops", "body": "hi"}), "Sent."])
    agent = Agent("mailer", model=model, tools=[guarded], checkpointer=InMemorySaver())

    paused = await agent.run("Mail ops")
    assert paused.is_interrupted
    assert paused.interrupts[0]["value"]["tool"] == "send_email"

    done = await agent.resume(paused.thread_id or "", {"action": "approve"})
    assert done.output == "Sent."
    assert any("sent to ops" in str(m.content) for m in done.messages)


async def test_a_single_input_tool_is_gated_with_a_tool_input_argument() -> None:
    sms = Tool(name="send_sms", func=lambda text: f"sms: {text}", description="Send an SMS.")
    (guarded,) = from_langchain(sms)
    assert set(guarded.args) == {"tool_input"}

    model = FakeChatModel([tool_call("send_sms", {"tool_input": "hello"}), "Done."])
    agent = Agent("texter", model=model, tools=[guarded], checkpointer=InMemorySaver())
    paused = await agent.run("Text hello")
    done = await agent.resume(paused.thread_id or "", {"action": "approve"})

    assert any(str(m.content) == "sms: hello" for m in done.messages)


def test_the_risk_comes_from_explicit_then_declared_then_heuristics() -> None:
    declared = mark_tool_risk(add, private_data=True)
    entry = import_langchain_tools(declared, risk=["untrusted_input"]).entries[0]

    assert entry.risk == ToolRisk(untrusted_input=True, private_data=True, external_action=False)
    assert entry.risk_origins == {
        "untrusted_input": "explicit",
        "private_data": "declared",
        "external_action": "default",
    }

    @tool
    def lookup(query: str) -> str:
        """Search the web for current events."""
        return query

    described = import_langchain_tools(lookup).entries[0]
    assert described.risk.untrusted_input
    assert described.risk_origins["untrusted_input"] == "description"

    overridden = import_langchain_tools(send_email, risk={"external_action": False}).entries[0]
    assert not overridden.requires_approval


def test_description_risk_matches_the_shared_heuristic() -> None:
    @tool
    def helper(query: str) -> str:
        """Deploy the release to production."""
        return query

    @tool
    def send_report(query: str) -> str:
        """Builds a report."""
        return query

    by_description = import_langchain_tools(helper).entries[0]
    assert by_description.risk.external_action
    assert by_description.risk_origins["external_action"] == "description"
    assert (
        by_description.risk.external_action
        == infer_description_risk(helper.description).external_action
    )

    by_name = import_langchain_tools(send_report).entries[0]
    assert by_name.risk == infer_tool_risk("send_report")
    assert by_name.risk_origins["external_action"] == "name"


def test_infer_description_risk_reads_words_and_phrases() -> None:
    assert infer_description_risk("Browse a website").untrusted_input
    assert infer_description_risk("Use a search engine").untrusted_input
    assert infer_description_risk("Sends an SMS").external_action
    assert infer_description_risk("Create a summary of the answer") == ToolRisk(
        untrusted_input=False, private_data=False, external_action=False
    )


def test_a_toolkit_contributes_all_its_tools_with_prefix_and_selection() -> None:
    tools = from_langchain(MathToolkit(), prefix="m_")
    assert [t.name for t in tools] == ["m_add", "m_fetch_page"]

    picked = from_langchain(MathToolkit(), select=["fetch_page", "add"])
    assert [t.name for t in picked] == ["fetch_page", "add"]
    report = import_langchain_tools(MathToolkit())
    assert report.entries[0].source == "toolkit MathToolkit"

    with pytest.raises(AdapterError, match="No tool named 'nope'"):
        from_langchain(MathToolkit(), select=["nope"])
    with pytest.raises(AdapterError, match="yields 2 tools"):
        from_langchain_tool(MathToolkit())
    assert from_langchain_tool(MathToolkit(), name="add").name == "add"


def test_functions_and_runnables_are_converted() -> None:
    (product,) = from_langchain(multiply)
    assert product.invoke({"a": 2, "b": 3}) == 6

    def shout(text: str) -> str:
        return text.upper()

    runnable = RunnableLambda(shout)
    converted = from_langchain_tool(runnable)
    assert converted.name == "shout"
    assert converted.invoke("hi") == "HI"

    with pytest.raises(AdapterError, match="docstring"):
        from_langchain(undocumented)


def test_import_paths_instantiate_classes_and_call_factories(tool_module: str) -> None:
    search = from_langchain_tool(f"{tool_module}.WebSearch", init_kwargs={"max_results": 7})
    assert search.name == "web_search"
    assert search.invoke({"query": "q"}) == "7 results for q"
    assert search.metadata is not None
    assert search.metadata[IMPORT_METADATA_KEY] == f"{tool_module}.WebSearch (BaseTool)"

    made = from_langchain(f"{tool_module}:make_tools")
    assert [t.name for t in made] == ["lookup_customer", "web_search"]
    assert declared_tool_risk(made[0]) == ToolRisk(
        untrusted_input=False, private_data=True, external_action=False
    )

    kit = from_langchain(f"{tool_module}:CrmToolkit", init_kwargs={"region": "us"})
    assert [t.name for t in kit] == ["lookup_customer"]
    assert from_langchain_tool(f"{tool_module}:lookup_customer").name == "lookup_customer"
    assert from_langchain_tool(f"{tool_module}:ping").invoke({}) == "pong"
    assert import_path(f"{tool_module}.WebSearch").__name__ == "WebSearch"


def test_bad_sources_raise_adapter_errors(tool_module: str) -> None:
    with pytest.raises(AdapterError, match="Invalid import path"):
        from_langchain("not a path")
    with pytest.raises(AdapterError, match="no module"):
        from_langchain(f"awa_test_missing_{uuid4().hex[:8]}.Tool")
    with pytest.raises(AdapterError, match="Cannot import"):
        from_langchain(f"{tool_module}:Missing")
    with pytest.raises(AdapterError, match="is a class"):
        from_langchain(FetchPage)
    with pytest.raises(AdapterError, match="init_kwargs given"):
        from_langchain(f"{tool_module}:ping", init_kwargs={"x": 1})
    with pytest.raises(AdapterError, match="Cannot import int as a tool"):
        from_langchain(42)
    with pytest.raises(AdapterError, match="share the names add"):
        from_langchain(add, MathToolkit())
    with pytest.raises(AdapterError, match="Invalid risk"):
        from_langchain(add, risk=["dangerous"])  # type: ignore[list-item]


def test_a_missing_dependency_of_the_module_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = f"awa_test_pkg_{uuid4().hex[:8]}"
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    # The missing module's name is a prefix of the module that imports it.
    (tmp_path / package / "module.py").write_text(f"import {package}.mo\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        with pytest.raises(AdapterError, match=rf"No module named '{package}\.mo'"):
            import_path(f"{package}.module.Thing")
    finally:
        for name in [m for m in sys.modules if m.startswith(package)]:
            sys.modules.pop(name)


def test_a_graph_is_pointed_to_from_langgraph() -> None:
    builder = StateGraph(MessagesState)
    builder.add_node("echo", lambda state: {"messages": []})
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)

    with pytest.raises(AdapterError, match="from_langgraph"):
        from_langchain(builder.compile())


def test_invalid_names_are_fixed_and_the_report_explains_everything() -> None:
    @tool("lookup.order")
    def lookup_order(order_id: str) -> str:
        """Look up an order."""
        return order_id

    report = import_langchain_tools(lookup_order, send_email)
    assert isinstance(report, ToolImportReport)
    assert report.tools[0].name == "lookup_order"
    text = report.render()
    assert "renamed from 'lookup.order'" in text
    assert "send_email  [BaseTool]" in text
    assert "risk: external_action (name)" in text
    assert "risk: private_data (name)" in text
    assert "approval: required (inferred: external action (name))" in text
    assert ToolImportReport().render() == "no tools imported"


SKILL_MD = """---
name: release-notes
description: Writes release notes from a list of merged changes. Use for every release.
metadata:
  version: "1.2.0"
  tags: release writing
---
# Release notes

Group changes by user impact. See references/style.md.
"""


def write_skill(root: Path, name: str = "release-notes", text: str = SKILL_MD) -> Path:
    folder = root / name
    (folder / "references").mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    (folder / "references" / "style.md").write_text("Short sentences.", encoding="utf-8")
    return folder


def test_a_skill_folder_is_validated_and_copied_with_its_resources(tmp_path: Path) -> None:
    source = write_skill(tmp_path / "src")
    marker = tmp_path / "executed.txt"
    (source / "scripts").mkdir()
    (source / "scripts" / "setup.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')", encoding="utf-8"
    )

    report = import_skills(source, into=tmp_path / "skills")

    assert report.ok
    (entry,) = report.imported
    assert entry.kind == "skill" and not entry.needs_review
    assert entry.written_to == tmp_path / "skills" / "release-notes" / "SKILL.md"
    copied = Skill.from_path(tmp_path / "skills" / "release-notes")
    assert copied.version == "1.2.0"
    assert copied.resources == ("references/style.md", "scripts/setup.py")
    assert copied.read_resource("references/style.md") == "Short sentences."
    assert not marker.exists()  # imported code is copied, never run


def test_invalid_skills_are_reported_and_the_rest_imported(tmp_path: Path) -> None:
    root = tmp_path / "library"
    write_skill(root)
    broken = root / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\nname: Broken Name\n---\nText", encoding="utf-8")
    (root / "README.md").write_text("# About this folder", encoding="utf-8")

    registry = SkillRegistry()
    report = import_skills(root, into=registry)

    assert registry.names() == ["release-notes"]
    assert not report.ok
    (issue,) = report.issues
    assert "broken" in issue.source and "description" in issue.message
    assert "skipped" in report.render()


def test_a_markdown_prompt_becomes_a_skill_flagged_for_review(tmp_path: Path) -> None:
    prompt = tmp_path / "Triage Tickets.md"
    prompt.write_text(
        "# Ticket triage\n\nSort incoming tickets into billing, bug or question.\n\n"
        "- Ask for the order number when it is missing.\n",
        encoding="utf-8",
    )

    report = import_skills(prompt, into=tmp_path / "skills")

    (entry,) = report.imported
    assert entry.needs_review and entry.kind == "markdown"
    skill = Skill.from_path(tmp_path / "skills" / "ticket-triage")
    assert skill.name == "ticket-triage"
    assert skill.description == "Sort incoming tickets into billing, bug or question."
    assert skill.metadata[REVIEW_METADATA_KEY] == "true"
    assert skill.metadata[SOURCE_METADATA_KEY] == "Triage Tickets.md"
    assert "Ask for the order number" in skill.instructions
    assert "[review]" in report.render()


def test_markdown_front_matter_supplies_name_description_and_tags(tmp_path: Path) -> None:
    prompt = tmp_path / "refunds.md"
    prompt.write_text(
        "---\nname: refund-policy\ndescription: Applies the refund policy.\ntags: [refunds]\n"
        "owner: finance\n---\nRefund within 30 days only.\n",
        encoding="utf-8",
    )

    (entry,) = import_skills(prompt).imported

    assert not entry.needs_review
    assert entry.skill.name == "refund-policy"
    assert entry.skill.tags == ("refunds",)
    assert REVIEW_METADATA_KEY not in entry.skill.metadata
    assert entry.notes == ("front matter keys ignored: owner",)


def test_skill_fields_are_shared_with_skill_md(tmp_path: Path) -> None:
    prompt = tmp_path / "audit.md"
    prompt.write_text(
        "---\nname: audit\ndescription: Audits.\nrisk: high\ncompatibility: any model\n"
        "allowed-tools: read_file\n---\nCheck everything.\n",
        encoding="utf-8",
    )

    (entry,) = import_skills(prompt).imported

    assert entry.skill.risk == "high"
    assert entry.skill.compatibility == "any model"
    assert entry.skill.allowed_tools == ("read_file",)
    assert entry.notes == ()


def test_a_derived_name_says_where_it_came_from(tmp_path: Path) -> None:
    heading = tmp_path / "a.md"
    heading.write_text("# Ticket triage\n\nSort tickets.\n", encoding="utf-8")
    plain = tmp_path / "Plain Notes.md"
    plain.write_text("Sort tickets.\n", encoding="utf-8")
    named = tmp_path / "b.md"
    named.write_text("---\nname: Big Name\ndescription: D.\n---\nText.\n", encoding="utf-8")

    notes = [import_skills(p).imported[0].notes[0] for p in (heading, plain, named)]

    assert notes == [
        "name 'ticket-triage' derived from the first heading",
        "name 'plain-notes' derived from the file name",
        "name 'big-name' derived from the front-matter name",
    ]


def test_serialised_prompt_files_and_objects_are_reported_as_unsupported(tmp_path: Path) -> None:
    (tmp_path / "summary.yaml").write_text("template: Summarise.\n", encoding="utf-8")

    from_folder = import_skills(tmp_path)
    from_file = import_skills(tmp_path / "summary.yaml")
    from_object = import_skills(object())

    assert from_folder.imported == () and from_folder.ok
    assert not from_file.ok and "unsupported file type" in from_file.issues[0].message
    assert not from_object.ok


def test_conflicts_overwrite_and_dry_runs(tmp_path: Path) -> None:
    source = write_skill(tmp_path / "src")
    target = tmp_path / "skills"

    dry = import_skills(source, into=target, dry_run=True)
    assert dry.dry_run and not target.exists()
    assert dry.render().startswith("would import 1 skill(s)")

    import_skills(source, into=target)
    again = import_skills(source, into=target)
    assert not again.imported and "already exists" in again.issues[0].message
    assert import_skills(source, into=target, overwrite=True).ok

    registry = SkillRegistry([Skill.from_path(source)])
    assert "already registered" in import_skills(source, into=registry).issues[0].message

    twice = import_skills(source, source)
    assert len(twice.imported) == 1 and "imported twice" in twice.issues[0].message

    with pytest.raises(AdapterError, match="not found"):
        import_skills(tmp_path / "missing")
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(AdapterError, match="it is a file"):
        import_skills(source, into=tmp_path / "file.txt")


def test_importing_a_skills_folder_into_itself_keeps_it(tmp_path: Path) -> None:
    folder = write_skill(tmp_path / "skills")

    report = import_skills(tmp_path / "skills", into=tmp_path / "skills", overwrite=True)

    assert report.ok and report.imported[0].written_to == folder / "SKILL.md"
    assert (folder / "references" / "style.md").read_text(encoding="utf-8") == "Short sentences."
    assert Skill.from_path(folder).name == "release-notes"


def test_symbolic_links_in_a_skill_folder_are_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_skill(tmp_path / "src")
    # Stands in for a link to a file outside the skill (creating real links needs
    # privileges on Windows).
    (source / "references" / "leak.md").write_text("private", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: self.name == "leak.md" or real_is_symlink(self)
    )

    import_skills(source, into=tmp_path / "skills")

    copied = Skill.from_path(tmp_path / "skills" / "release-notes")
    assert copied.resources == ("references/style.md",)


def test_render_skill_markdown_round_trips(tmp_path: Path) -> None:
    skill = Skill(
        name="triage",
        description="Sorts tickets.",
        instructions="Label every ticket.",
        tags=("support", "first line"),
        category="ops",
        risk="medium",
        metadata={"owner": "desk"},
    )
    folder = tmp_path / "triage"
    folder.mkdir()
    (folder / "SKILL.md").write_text(render_skill_markdown(skill), encoding="utf-8")

    loaded = Skill.from_path(folder)
    assert loaded.model_dump(exclude={"source_path"}) == skill.model_dump(exclude={"source_path"})
    assert loaded.tags == ("support", "first-line")


async def test_imported_skills_are_usable_by_an_agent(tmp_path: Path) -> None:
    registry = SkillRegistry()
    import_skills(write_skill(tmp_path), into=registry)
    model = FakeChatModel(["Notes."])

    agent = Agent("writer", model=model, skills=["release-notes"], skill_registry=registry)
    await agent.run("Write the notes")

    system = model.calls[0].messages[0]
    assert "## Skill: release-notes" in str(system.content)


def test_a_derived_description_keeps_emphasis_and_drops_list_markers(tmp_path: Path) -> None:
    prompt = tmp_path / "notes.md"
    prompt.write_text("# Notes\n\n**Important** thing to do\n", encoding="utf-8")
    listed = tmp_path / "steps.md"
    listed.write_text("# Steps\n\n- > First step\n", encoding="utf-8")

    (notes,) = import_skills(prompt).imported
    (steps,) = import_skills(listed).imported

    assert notes.skill.description == "**Important** thing to do"
    assert steps.skill.description == "First step"
