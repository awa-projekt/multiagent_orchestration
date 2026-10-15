"""Run existing LangGraph graphs as awa agents.

`from_langgraph` wraps any compiled graph with a messages channel (including `create_agent`
graphs) as an `AgentLike`, usable as a team member, workflow step or tool.
"""

from __future__ import annotations

from collections.abc import Coroutine, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import Command, GraphInterrupt, GraphRecursionError, Pregel
from awa._internal.upstream.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    convert_to_messages,
)
from awa._internal.upstream.runnables import RunnableConfig, ensure_config
from awa._internal.upstream.tools import BaseTool
from awa._internal.util import elapsed_ms, import_path
from awa.agents._graph_tracking import GraphRunTracking
from awa.agents.boundary import as_tool_for
from awa.contracts.cards import AgentCard, validate_agent_name
from awa.contracts.protocols import AgentInput
from awa.contracts.results import RunResult
from awa.contracts.usage import UsageLimits
from awa.errors import AdapterError, AgentRunError, NoPendingInterruptError
from awa.runtime._run import RunApi, RunRequest, StreamStart, resolve_thread, run_observed
from awa.runtime._streaming import text_delta_callbacks
from awa.runtime.context import EventSubscriber, RunContext, new_run_context
from awa.runtime.engine import current_engine

_STRUCTURED_RESPONSE = "structured_response"


class GraphAgent(RunApi[Any, Any]):
    """A compiled LangGraph graph with a messages channel, run as an awa agent.

    A run sends the input (and `history`) on `input_key`; `text` is the run's last AI message and
    `output` the value of `output_key` (default `structured_response`, else the text). Threads,
    interrupts and `resume` use the graph's own checkpointer; nested in a checkpointed awa run, the
    graph runs as a subgraph of that thread.

    Usage, events and `UsageLimits` are tracked through LangChain callbacks; model calls the graph
    makes without passing its callbacks on are not seen.
    """

    def __init__(
        self,
        graph: Pregel[Any, Any, Any, Any],
        *,
        name: str,
        description: str = "",
        input_key: str = "messages",
        output_key: str | None = None,
        capabilities: Sequence[str] = (),
        tags: Sequence[str] = (),
        role: str | None = None,
        recursion_limit: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        validate_agent_name(name)
        if not isinstance(graph, Pregel):
            raise AdapterError(
                f"from_langgraph needs a compiled LangGraph graph (StateGraph.compile(), "
                f"create_agent(...)), got {type(graph).__name__}"
            )
        channels = graph.channels
        if input_key not in channels:
            raise AdapterError(
                f"Graph {graph.get_name()!r} has no {input_key!r} channel to send messages on "
                f"(channels: {', '.join(sorted(k for k in channels if not k.startswith('__')))}); "
                "pass input_key="
            )
        if output_key is not None and output_key not in channels:
            raise AdapterError(f"Graph {graph.get_name()!r} has no {output_key!r} channel")
        if recursion_limit is not None and recursion_limit < 1:
            raise ValueError(f"recursion_limit must be at least 1, got {recursion_limit}")
        self._graph = graph
        self._name = name
        self._description = description
        self._input_key = input_key
        self._output_key = output_key
        self._recursion_limit = recursion_limit
        self._card = AgentCard(
            name=name,
            description=description,
            capabilities=tuple(capabilities),
            tools=_graph_tool_names(graph),
            model=None,
            role=role,
            tags=("graph", *tags),
            metadata={"adapter": "langgraph", "graph": graph.get_name(), **(metadata or {})},
        )

    @property
    def name(self) -> str:
        """The agent's unique name."""

        return self._name

    @property
    def description(self) -> str:
        """What the agent is for."""

        return self._description

    @property
    def card(self) -> AgentCard:
        """The agent's public description."""

        return self._card

    @property
    def graph(self) -> Pregel[Any, Any, Any, Any]:
        """The wrapped graph."""

        return self._graph

    @property
    def has_checkpointer(self) -> bool:
        """Whether the graph persists threads (and can therefore pause on interrupts)."""

        return isinstance(self._graph.checkpointer, BaseCheckpointSaver)

    def __repr__(self) -> str:
        return f"GraphAgent(name={self._name!r}, graph={self._graph.get_name()!r})"

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        thread_id: str | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[Any]:
        """Run the graph to completion.

        Args:
            deps: Dependencies for the run context (`awa.runtime.current_run_context()`).
            usage_limits: Limits for the whole run tree; only on a top-level run.
            thread_id: Thread to continue and persist (requires a checkpointer on the graph); created
                when omitted with a checkpointer.
            history: Earlier messages sent before `input`.

        Returns:
            The result; `output` is `None` while the run is paused on an interrupt.

        Raises:
            ConfigurationError: If `thread_id` is given but the graph has no checkpointer, or
                `usage_limits` is given for a nested run.
            RunTimeoutError: If `timeout` or `UsageLimits.max_duration` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
            UsageLimitExceededError: If the run tree exceeds `usage_limits`.
            AgentRunError: If the graph produced no AI message, exceeded its recursion limit, or was
                interrupted with nowhere to pause.
        """

        thread = self._resolve_thread(thread_id)
        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        graph_input, text, start = self._graph_input(input, history)
        return await self._execute(ctx, graph_input, text, start, thread_id=thread, timeout=timeout)

    def _stream_task(self, request: RunRequest) -> tuple[RunContext[Any], StreamStart]:
        thread = self._resolve_thread(request.thread_id)
        ctx = new_run_context(self._name, request.deps, request.usage_limits, request.metadata)
        graph_input, text, start_index = self._graph_input(request.input, request.history)

        def begin(subscriber: EventSubscriber) -> Coroutine[Any, Any, RunResult[Any]]:
            return self._execute(
                ctx,
                graph_input,
                text,
                start_index,
                thread_id=thread,
                timeout=request.timeout,
                stream_text=True,
                subscribers=(subscriber,),
            )

        return ctx, begin

    async def resume(
        self,
        thread_id: str | None,
        decision: Any,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[Any]:
        """Resume a run that paused on an interrupt; see `Agent.resume`.

        Raises:
            ConfigurationError: If the graph has no checkpointer.
            NoPendingInterruptError: If `thread_id` is `None` (the run kept no thread).
        """

        if thread_id is None:
            raise NoPendingInterruptError(f"Graph {self._name!r} has no thread to resume")
        thread = self._resolve_thread(thread_id)
        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        return await self._execute(
            ctx, Command(resume=decision), None, 0, thread_id=thread, timeout=timeout
        )

    def as_tool(self, *, name: str | None = None, description: str | None = None) -> BaseTool:
        """Expose the graph as a nested-run tool returning its final text.

        Args:
            name: Tool name; defaults to the agent name.
            description: Tool description; defaults to the agent description.
        """

        return as_tool_for(self, _output_text, noun="graph", name=name, description=description)

    def _resolve_thread(self, thread_id: str | None) -> str | None:
        checkpointer = self._graph.checkpointer
        saver = checkpointer if isinstance(checkpointer, BaseCheckpointSaver) else None
        return resolve_thread(f"Graph {self._name!r}", saver, thread_id)

    def _graph_input(
        self, input: AgentInput, history: Sequence[BaseMessage | dict[str, Any]] | None
    ) -> tuple[dict[str, Any], str, int]:
        messages = (
            [HumanMessage(content=input)] if isinstance(input, str) else convert_to_messages(input)
        )
        prior = convert_to_messages(history) if history else []
        text = next((str(m.text) for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        return {self._input_key: [*prior, *messages]}, text, len(prior)

    async def _execute(
        self,
        ctx: RunContext[Any],
        graph_input: dict[str, Any] | Command[Any],
        input_text: str | None,
        history_length: int,
        *,
        thread_id: str | None,
        timeout: float | None = None,
        stream_text: bool = False,
        subscribers: Sequence[EventSubscriber] = (),
    ) -> RunResult[Any]:
        resumable = current_engine().in_thread()

        async def work(started: float) -> RunResult[Any]:
            try:
                return await self._invoke(
                    ctx, graph_input, history_length, thread_id, stream_text, started
                )
            except GraphInterrupt as exc:
                if resumable:
                    raise
                raise AgentRunError(
                    f"Graph {self._name!r} was interrupted inside a nested run, but no "
                    "enclosing graph has a checkpointer to pause on. Give the top-level "
                    "agent a checkpointer to use human-in-the-loop in nested graphs."
                ) from exc

        return await run_observed(
            ctx,
            work,
            kind="agent",
            input_text=input_text or "",
            output_text=_output_text,
            timeout=timeout,
            subscribers=subscribers,
            thread_id=thread_id,
            role=self._card.role,
        )

    async def _invoke(
        self,
        ctx: RunContext[Any],
        graph_input: dict[str, Any] | Command[Any] | None,
        history_length: int,
        thread_id: str | None,
        stream_text: bool,
        started: float,
    ) -> RunResult[Any]:
        graph = self._graph
        config: RunnableConfig = {"run_name": self._name}
        if self._recursion_limit is not None:
            config["recursion_limit"] = self._recursion_limit
        start = history_length
        if thread_id is not None:
            config["configurable"] = {"thread_id": thread_id}
            snapshot = await graph.aget_state(config)
            values = snapshot.values if isinstance(snapshot.values, dict) else {}
            start += len(values.get(self._input_key) or [])
            if isinstance(graph_input, Command) and not snapshot.interrupts and snapshot.next:
                # Resuming a run that was stopped mid-run: continue from its last step.
                graph_input = None
        tracking = GraphRunTracking(ctx, stream_text=stream_text)
        if stream_text and (streaming := text_delta_callbacks()) is not None:
            config["callbacks"] = _with_handler(streaming, tracking)
        else:
            config["callbacks"] = _with_handler(ensure_config().get("callbacks"), tracking)
        try:
            state = await graph.ainvoke(graph_input, config=config)
        except GraphRecursionError as exc:
            raise AgentRunError(
                f"Graph {self._name!r} exceeded its recursion limit without finishing"
            ) from exc
        if not isinstance(state, dict):
            state = {"output": state}
        if state.get("__interrupt__") and thread_id is None and not current_engine().in_thread():
            raise AgentRunError(
                f"Graph {self._name!r} was interrupted, but it has no checkpointer to pause on. "
                "Compile the graph with a checkpointer to use human-in-the-loop interrupts."
            )
        return self._build_result(ctx, state, start, thread_id, started)

    def _build_result(
        self,
        ctx: RunContext[Any],
        state: dict[str, Any],
        start: int,
        thread_id: str | None,
        started: float,
    ) -> RunResult[Any]:
        raw = state.get(self._input_key)
        messages = [m for m in raw if isinstance(m, BaseMessage)] if isinstance(raw, list) else []
        interrupts = [{"id": i.id, "value": i.value} for i in state.get("__interrupt__", ())]
        reply = next((m for m in reversed(messages[start:]) if isinstance(m, AIMessage)), None)
        text = str(reply.text) if reply is not None else ""
        output: Any = None
        if not interrupts:
            if self._output_key is not None:
                output = state.get(self._output_key)
            elif state.get(_STRUCTURED_RESPONSE) is not None:
                output = state[_STRUCTURED_RESPONSE]
            elif reply is None:
                raise AgentRunError(
                    f"Graph {self._name!r} finished without an AI message on "
                    f"{self._input_key!r}; pass output_key= to read another channel"
                )
            else:
                output = text
        return RunResult(
            run_id=ctx.run_id,
            agent=self._name,
            output=output,
            text=text,
            messages=messages,
            new_messages_start=min(start, len(messages)),
            usage=ctx.usage.snapshot(),
            duration_ms=elapsed_ms(started),
            handoff_path=(self._name,),
            thread_id=thread_id,
            interrupts=interrupts,
        )


def from_langgraph(
    graph: Pregel[Any, Any, Any, Any] | str,
    *,
    name: str,
    description: str = "",
    input_key: str = "messages",
    output_key: str | None = None,
    capabilities: Sequence[str] = (),
    tags: Sequence[str] = (),
    role: str | None = None,
    recursion_limit: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> GraphAgent:
    """Wrap a compiled LangGraph graph with a messages channel as an awa agent.

    Args:
        graph: The compiled graph, or an import path to one, such as `"billing.graph:graph"`.
        description: What the agent is for; orchestrators read it when delegating.
        input_key: The channel that receives the input messages.
        output_key: A channel whose final value is the run's `output`; defaults to
            `structured_response` when the graph sets it, else the final text.
        tags: Labels for the agent card (`"graph"` is always added).
        recursion_limit: Maximum graph steps per run; LangGraph's default when unset.

    Raises:
        AdapterError: If `graph` is not a compiled graph, or has no `input_key` (or `output_key`)
            channel.

    Example:
        ```python
        agent = from_langgraph(create_agent(model, tools=[search]), name="researcher")
        result = await agent.run("What is hybrid search?")
        ```
    """

    if isinstance(graph, str):
        graph = import_path(graph)
    return GraphAgent(
        graph,  # type: ignore[arg-type]  # a path was imported above: `import_path` returns Any
        name=name,
        description=description,
        input_key=input_key,
        output_key=output_key,
        capabilities=capabilities,
        tags=tags,
        role=role,
        recursion_limit=recursion_limit,
        metadata=metadata,
    )


def _graph_tool_names(graph: Pregel[Any, Any, Any, Any]) -> tuple[str, ...]:
    """The names of the tools in the graph's tool nodes, each once."""

    names: dict[str, None] = {}
    for node in graph.nodes.values():
        by_name = getattr(getattr(node, "bound", None), "tools_by_name", None)
        if isinstance(by_name, Mapping):
            names.update(
                (tool.name, None) for tool in by_name.values() if isinstance(tool, BaseTool)
            )
    return tuple(names)


def _with_handler(callbacks: Any, handler: GraphRunTracking) -> Any:
    """`callbacks` (a list, a manager or `None`) plus `handler`."""

    if callbacks is None:
        return [handler]
    if isinstance(callbacks, list):
        return [*callbacks, handler]
    manager = callbacks.copy()
    manager.add_handler(handler, inherit=True)
    return manager


def _output_text(result: RunResult[Any]) -> str:
    if result.text or result.output is None:
        return result.text
    if isinstance(result.output, str):
        return result.output
    if isinstance(result.output, BaseModel):
        return result.output.model_dump_json()
    import pydantic_core

    return pydantic_core.to_json(result.output).decode()


__all__ = ["GraphAgent", "from_langgraph"]
