"""Contracts of `create_agent` and its middleware that awa relies on.

Each assertion names the awa module that breaks when the upstream behavior
changes. These tests import upstream directly, at the locations they pin.
"""

from __future__ import annotations

import inspect
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    ToolCallRequest,
    hook_config,
)
from langchain.agents.structured_output import AutoStrategy, ProviderStrategy, ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel

from awa._internal.upstream import agents as compat_agents
from awa.kernel.output import structured_output_tool_names
from awa.testing import FakeChatModel, structured_output, tool_call


def broken(awa_module: str, what: str) -> str:
    return f"Upstream contract broken: {what}. awa depends on it in {awa_module}."


@tool
def lookup(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}: shipped"


@tool
def explode(order_id: str) -> str:
    """Always fails."""
    raise ToolException("database down")


explode.handle_tool_error = True


class _Recorder(AgentMiddleware[Any, Any]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.model_requests: list[ModelRequest[Any]] = []
        self.tool_requests: list[ToolCallRequest] = []
        self.tool_results: list[Any] = []

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        self.calls.append("before_agent")

    async def abefore_model(self, state: Any, runtime: Any) -> None:
        self.calls.append("before_model")

    async def awrap_model_call(self, request: ModelRequest[Any], handler: Any) -> Any:
        self.calls.append("wrap_model_call")
        self.model_requests.append(request)
        return await handler(request)

    async def aafter_model(self, state: Any, runtime: Any) -> None:
        self.calls.append("after_model")

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        self.calls.append("wrap_tool_call")
        self.tool_requests.append(request)
        result = await handler(request)
        self.tool_results.append(result)
        return result

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        self.calls.append("after_agent")


def test_create_agent_accepts_the_arguments_awa_passes() -> None:
    parameters = inspect.signature(create_agent).parameters
    for name in ("tools", "middleware", "response_format", "checkpointer", "store", "name"):
        assert name in parameters, broken("awa.agents.agent", f"create_agent({name}=...)")
    assert next(iter(parameters)) == "model", broken(
        "awa.agents.agent", "create_agent takes the model first"
    )


def test_tool_call_request_is_exported_publicly_and_matches_the_seam() -> None:
    assert compat_agents.ToolCallRequest is ToolCallRequest, broken(
        "awa.tools.hardening, awa.agents.middleware",
        "awa._internal.upstream.agents re-exports ToolCallRequest from the public "
        "langchain.agents.middleware",
    )
    fields = {"tool_call", "tool", "state", "runtime"}
    assert fields <= set(inspect.signature(ToolCallRequest).parameters), broken(
        "awa.tools.hardening", f"ToolCallRequest fields {sorted(fields)}"
    )
    assert callable(getattr(ToolCallRequest, "override", None)), broken(
        "awa.tools.policy", "ToolCallRequest.override()"
    )


async def test_middleware_hooks_run_in_the_order_awa_expects() -> None:
    recorder = _Recorder()
    model = FakeChatModel([tool_call("lookup", {"order_id": "A1"}, id="c1"), "It shipped."])
    agent = create_agent(model, tools=[lookup], middleware=[recorder])

    result = await agent.ainvoke({"messages": [HumanMessage("Where is A1?")]})

    assert recorder.calls == [
        "before_agent",
        "before_model",
        "wrap_model_call",
        "after_model",
        "wrap_tool_call",
        "before_model",
        "wrap_model_call",
        "after_model",
        "after_agent",
    ], broken("awa.agents.middleware (run tracking, limits)", "the middleware hook order")
    assert result["messages"][-1].content == "It shipped."


async def test_model_and_tool_requests_carry_the_fields_awa_reads() -> None:
    recorder = _Recorder()
    model = FakeChatModel([tool_call("lookup", {"order_id": "A1"}, id="c1"), "done"])
    agent = create_agent(model, tools=[lookup], middleware=[recorder], system_prompt="Be brief.")
    await agent.ainvoke({"messages": [HumanMessage("Where is A1?")]})

    request = recorder.model_requests[0]
    for field in ("model", "messages", "tools", "response_format", "system_message", "state"):
        assert hasattr(request, field), broken(
            "awa.agents.middleware, awa.models.resilience, awa.models.prompt_cache",
            f"ModelRequest.{field}",
        )
    assert request.system_message is not None and "Be brief." in str(
        request.system_message.content
    ), broken("awa.models.prompt_cache", "ModelRequest.system_message holds the system prompt")
    overridden = request.override(model=model)
    assert overridden.model is model, broken("awa.models.resilience", "ModelRequest.override()")

    tool_request = recorder.tool_requests[0]
    assert tool_request.tool_call["name"] == "lookup" and tool_request.tool_call["id"] == "c1", (
        broken("awa.tools.hardening", "ToolCallRequest.tool_call is the model's ToolCall")
    )
    assert tool_request.tool is not None and tool_request.tool.name == "lookup", broken(
        "awa.tools.hardening", "ToolCallRequest.tool is the BaseTool"
    )
    message = recorder.tool_results[0]
    assert isinstance(message, ToolMessage), broken(
        "awa.agents.middleware", "a tool call handler returns a ToolMessage"
    )
    assert (message.tool_call_id, message.name, message.status) == ("c1", "lookup", "success")


async def test_model_response_wraps_the_ai_message() -> None:
    seen: list[Any] = []

    class Capture(AgentMiddleware[Any, Any]):
        async def awrap_model_call(self, request: ModelRequest[Any], handler: Any) -> Any:
            response = await handler(request)
            seen.append(response)
            return response

    await create_agent(FakeChatModel(["hi"]), middleware=[Capture()]).ainvoke(
        {"messages": [HumanMessage("hello")]}
    )
    assert isinstance(seen[0], ModelResponse), broken(
        "awa.agents.middleware", "wrap_model_call handlers return a ModelResponse"
    )
    assert isinstance(seen[0].result[0], AIMessage), broken(
        "awa.agents.middleware", "ModelResponse.result is a list of messages, AIMessage first"
    )


async def test_a_failing_tool_becomes_an_error_tool_message() -> None:
    recorder = _Recorder()
    model = FakeChatModel([tool_call("explode", {"order_id": "A1"}, id="c9"), "sorry"])
    agent = create_agent(model, tools=[explode], middleware=[recorder])
    await agent.ainvoke({"messages": [HumanMessage("go")]})
    message = recorder.tool_results[0]
    assert isinstance(message, ToolMessage) and message.status == "error", broken(
        "awa.tools.hardening, awa.agents.middleware",
        "a ToolException of a tool with handle_tool_error becomes ToolMessage(status='error')",
    )
    assert "database down" in str(message.content)


async def test_before_model_can_jump_to_end_with_hook_config() -> None:
    class Stop(AgentMiddleware[Any, Any]):
        @hook_config(can_jump_to=["end"])
        async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
            return {"jump_to": "end"}

    model = FakeChatModel(["never"])
    await create_agent(model, middleware=[Stop()]).ainvoke({"messages": [HumanMessage("x")]})
    assert model.call_count == 0, broken(
        "awa.team._delivery (transfer ends the loop)", "before_model jump_to='end' skips the model"
    )


class Invoice(BaseModel):
    number: str
    total: float


async def test_tool_strategy_names_the_output_tool_after_the_schema() -> None:
    assert structured_output_tool_names(Invoice) == {"Invoice"}, broken(
        "awa.agents.middleware, awa.testing.structured_output",
        "structured-output tools are named after the schema class",
    )
    assert structured_output_tool_names(ToolStrategy(Invoice)) == {"Invoice"}
    assert structured_output_tool_names(AutoStrategy(Invoice)) == {"Invoice"}
    assert structured_output_tool_names(ProviderStrategy(Invoice)) == frozenset()

    model = FakeChatModel([structured_output(Invoice, {"number": "I-1", "total": 9.5})])
    result = await create_agent(model, response_format=Invoice).ainvoke(
        {"messages": [HumanMessage("Extract")]}
    )
    assert result["structured_response"] == Invoice(number="I-1", total=9.5), broken(
        "awa.agents.agent (output_type)", "the tool strategy fills state['structured_response']"
    )


def test_summarization_middleware_takes_the_arguments_awa_passes() -> None:
    parameters = inspect.signature(SummarizationMiddleware.__init__).parameters
    for name in ("model", "trigger", "keep", "summary_prompt", "trim_tokens_to_summarize"):
        assert name in parameters, broken(
            "awa.memory.compaction", f"SummarizationMiddleware({name}=...)"
        )
    middleware = SummarizationMiddleware(
        FakeChatModel(["summary"]),
        trigger=("tokens", 1000),
        keep=("messages", 4),
        summary_prompt=compat_agents.DEFAULT_SUMMARY_PROMPT,
        trim_tokens_to_summarize=None,
    )
    assert isinstance(middleware, AgentMiddleware)
    assert "{messages}" in compat_agents.DEFAULT_SUMMARY_PROMPT, broken(
        "awa.memory.compaction", "the default summary prompt has a {messages} placeholder"
    )
