"""`AgentNetwork`: a lead and autonomous teammates working concurrently on a task board.

Preview: it works and is tested, but its API may still change in a minor release.

Example:
    ```python
    from awa import Agent
    from awa.team import AgentNetwork

    network = AgentNetwork(
        "studio",
        lead=Agent("lead", description="Plans and synthesizes."),
        teammates=[
            Agent("researcher", description="Finds facts.", capabilities=["research"]),
            Agent("writer", description="Writes prose.", capabilities=["writing"]),
        ],
    )
    result = await network.run("Write a short, sourced explainer on hybrid search.")
    ```
"""

from __future__ import annotations

from collections.abc import Coroutine, Mapping, Sequence
from typing import Any, Literal, NoReturn
from uuid import uuid4

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.messages import BaseMessage, HumanMessage, convert_to_messages
from awa._internal.upstream.tools import BaseTool, StructuredTool
from awa.agents import Agent, AgentHooks
from awa.contracts.cards import AgentCard, validate_agent_name
from awa.contracts.events import ErrorEvent
from awa.contracts.protocols import AgentInput
from awa.contracts.results import RunResult
from awa.contracts.usage import UsageLimits
from awa.directory import AgentDirectory, create_directory_tools
from awa.errors import (
    AgentNotFoundError,
    ConfigurationError,
    NoPendingInterruptError,
    SystemBusyError,
    TopologyError,
)
from awa.messaging import (
    BaseBlackboard,
    InMemoryMessageBus,
    MessageBus,
    TopologyPolicy,
    create_blackboard_tools,
    create_messaging_tools,
)
from awa.persistence.records import RecordStore
from awa.runtime._run import (
    RunApi,
    RunRequest,
    StreamStart,
    SubtreeSubscriber,
    resolve_thread,
    run_observed,
)
from awa.runtime.context import EventSubscriber, RunContext, current_run_context, new_run_context
from awa.tasks import BaseTaskBoard, create_task_board_tools
from awa.team._delegation import output_text
from awa.team._delivery import TeamMiddleware, wire_agent
from awa.team._network_coordinator import Coordinator
from awa.team._network_state import NetworkRun, StopReason, parked_runs, route_decisions
from awa.team._network_workers import RUN_TAG
from awa.team._prompts import lead_instructions, teammate_instructions
from awa.team._run import LEAD_TASK_TOOLS
from awa.team._scoped import RunScope, ScopedBlackboard, ScopedBus, ScopedTaskBoard, facade
from awa.team._topology import MemberInfo, Topology, build_topology
from awa.team.limits import NetworkLimits
from awa.workflows._base import input_text, union

# A teammate's final answer completes its task; `fail_task` is all it needs to finish.
# Follow-up tasks only with `teammates_create_tasks`.
_TEAMMATE_TASK_TOOLS = frozenset({"fail_task"})
_TEAMMATE_PLANNING_TOOLS = frozenset({"create_task", "create_tasks"})
TASKS_KEY = "awa.tasks"
"""Run metadata key listing open tasks of a shared board a run works on as well."""


class AgentNetwork(Coordinator, RunApi[Any, Any]):
    """A lead and autonomous teammates that work concurrently on a shared task board.

    A run has four phases: the lead plans tasks (with dependencies, capabilities or owners); each
    teammate claims ready tasks it can do and works on a brief with the task and its dependencies'
    results, failed tasks retried up to `max_task_attempts`; the lead runs again whenever handbacks
    or messages arrive; when nothing can progress, the lead writes the final answer.

    Teammates get their own tools plus `fail_task`, `create_task` with `teammates_create_tasks`, and
    messaging to their `peers` and the lead. The lead gets task, directory and messaging tools. All
    runs are nested in the network run, so usage, limits and events cover every agent.

    Each run works on its own in-memory task board, bus and workspace; the `task_board`, `bus` and
    `blackboard` properties act on the run in progress, or on the one started last. A board or bus
    passed as an object is shared by every run; the network then runs one goal at a time per
    process, on the tasks it created (`data["awa.run"]`) plus those listed in
    `run(metadata={"awa.tasks": [...]})`.

    **Human in the loop.** With a `checkpointer`, an agent that pauses on an interrupt parks its
    work (a teammate's task stays `in_progress` with `data["awa.awaiting_approval"]`) while others
    continue. When nothing else can progress, the run pauses with `RunResult.interrupts` (lead first,
    then by task) and `stop_reason` `"interrupted"`; `resume(thread_id, decision)` restores tasks,
    counters, usage and the lead's conversation. Decisions left out keep their runs parked. An
    expired `run_timeout` with pending approvals pauses the run; a withdrawn approval is reported as
    a `DelegationResultEvent` with status `"error"`. Resuming in another process needs a task board
    and bus that outlive the process.

    Args:
        lead: The coordinating agent.
        teammates: The agents doing the work.
        description: What the network does; generated when empty.
        task_board: A task board shared by every run; by default each run gets a new in-memory one.
        bus: A message bus shared by every run; by default each run gets a private
            `InMemoryMessageBus` that allows the lead-teammate and `peers` edges.
        directory: Directory to register the agents in; a private one when omitted.
        blackboard: `True` for a workspace per run, or a `BaseBlackboard` shared by every run.
        peers: Which teammates may message each other (symmetric edges), or `"all"`; with edges
            given, a teammate without peers gets no messaging tools.
        teammates_create_tasks: Give teammates `create_task`/`create_tasks` for follow-up work.
        hooks: `AgentHooks` or event callbacks receiving every event of a run.
        checkpointer: Enables threads, pauses on interrupts and `resume`; every agent persists its
            runs in it.

    Raises:
        TopologyError: If names are not unique, the lead is also a teammate, there are no
            teammates, a peer edge is invalid, a network tool clashes with an agent's own tool, or
            an agent already is the runtime agent of another team or network.
    """

    def __init__(
        self,
        name: str,
        *,
        lead: Agent[Any, Any],
        teammates: Sequence[Agent[Any, Any]],
        description: str = "",
        task_board: BaseTaskBoard | None = None,
        bus: MessageBus | None = None,
        directory: AgentDirectory | None = None,
        blackboard: BaseBlackboard | bool = False,
        peers: Mapping[str, Sequence[str]] | Literal["all"] = "all",
        teammates_create_tasks: bool = False,
        limits: NetworkLimits | None = None,
        hooks: Sequence[AgentHooks | EventSubscriber] = (),
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        capabilities: Sequence[str] = (),
        tags: Sequence[str] = (),
        version: str = "1.0.0",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        _check_members(name, lead, teammates, peers)
        names = [teammate.name for teammate in teammates]
        edges = {n: [o for o in names if o != n] for n in names} if peers == "all" else peers
        self._topology: Topology = build_topology(
            name,
            MemberInfo(lead.name, "agent"),
            [MemberInfo(n, "agent") for n in names],
            delegates={lead.name: names} if names else None,
            peers=edges,
            symmetric_peers=True,
            handoffs=None,
            label="Network",
        )
        self._name = name
        self._lead = lead
        self._teammates = tuple(teammates)
        self._limits = limits or NetworkLimits()
        self._teammates_create_tasks = teammates_create_tasks
        # Teammates message only when peers are asked for: `"all"`, or edges of their own.
        self._messaging_all = peers == "all"
        self._hooks: tuple[EventSubscriber, ...] = tuple(hooks)
        self._checkpointer = checkpointer
        self._records = RecordStore(name, checkpointer)
        self._key = f"awa.network:{uuid4().hex}"
        if bus is not None:
            self._register_mailboxes(bus)
            bus.add_listener(self._on_message)
        self._scope = RunScope(
            self._key,
            bus=bus or self._fresh_bus,
            blackboard=blackboard,
            task_board=task_board if task_board is not None else True,
            listener=self._on_message,
        )
        self._board: BaseTaskBoard = (
            task_board if task_board is not None else ScopedTaskBoard(self._scope)
        )
        self._bus: MessageBus = bus if bus is not None else ScopedBus(self._scope)
        self._blackboard: BaseBlackboard | None = facade(blackboard, ScopedBlackboard(self._scope))
        self._directory = directory if directory is not None else AgentDirectory()
        self._card = self._build_card(description, capabilities, tags, version, metadata)
        self._agents: dict[str, Agent[Any, Any]] = {}
        self._wire()
        self._presence = self._directory.as_event_subscriber()
        self._running = 0

    @property
    def name(self) -> str:
        """The network's name."""

        return self._name

    @property
    def description(self) -> str:
        """What the network does."""

        return self._card.description

    @property
    def card(self) -> AgentCard:
        """The network's card: role `"network"`, capabilities and skills of all its agents."""

        return self._card

    @property
    def lead(self) -> Agent[Any, Any]:
        """The lead as given (without network tools)."""

        return self._lead

    @property
    def teammates(self) -> tuple[Agent[Any, Any], ...]:
        """The teammates as given (without network tools)."""

        return self._teammates

    @property
    def limits(self) -> NetworkLimits:
        """The network's per-run limits."""

        return self._limits

    @property
    def task_board(self) -> BaseTaskBoard:
        """The task board (of the run in progress, else of the run started last)."""

        return self._board

    @property
    def bus(self) -> MessageBus:
        """The message bus (of the run in progress, else of the run started last)."""

        return self._bus

    @property
    def directory(self) -> AgentDirectory:
        """The directory the network's agents are registered in."""

        return self._directory

    @property
    def blackboard(self) -> BaseBlackboard | None:
        """The workspace (of the run in progress, else of the run started last), if any."""

        return self._blackboard

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any] | None:
        """The checkpointer runs persist in, if any."""

        return self._checkpointer

    def agent(self, name: str) -> Agent[Any, Any]:
        """Return the runtime agent of `name`, with its network tools.

        Raises:
            AgentNotFoundError: If `name` is not an agent of this network.
        """

        try:
            return self._agents[name]
        except KeyError:
            raise AgentNotFoundError(
                f"Network {self._name!r} has no agent {name!r}. Agents: {', '.join(self._agents)}"
            ) from None

    def topology(self) -> dict[str, Any]:
        """The network's structure: `{"lead": ..., "teammates": [...], "peers": {...}}`."""

        return {
            "lead": self._lead.name,
            "teammates": [teammate.name for teammate in self._teammates],
            "peers": {source: list(t) for source, t in self._topology.peers.items()},
        }

    def to_mermaid(self) -> str:
        """Render the network as a Mermaid flowchart (lead to teammates; `<-.->` for messaging edges)."""

        return self._topology.to_mermaid()

    def __repr__(self) -> str:
        names = ", ".join(teammate.name for teammate in self._teammates)
        return f"AgentNetwork({self._name!r}, lead={self._lead.name!r}, teammates=[{names}])"

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
        """Work on a goal until it is done (or paused) and return the lead's final answer.

        Args:
            input: The goal: a prompt or a list of messages.
            usage_limits: Limits for the whole run tree; only on a top-level run.
            thread_id: Thread to persist the run under (requires a checkpointer); created when omitted
                with a checkpointer. A run on an existing thread works on its new goal afresh.
            history: Not supported; pass earlier context as part of `input`.
            timeout: Wall-clock seconds for the whole run; unlike `NetworkLimits.run_timeout` it stops
                every agent at once and raises `RunTimeoutError`.
            metadata: Request-scoped values merged into `RunContext.metadata`; `"awa.tasks"` lists open
                tasks of a shared board this run may work on.

        Returns:
            `output` is the lead's final answer, `usage` that of every agent; `metadata` holds `tasks`,
            `handbacks`, `lead_turns` and `stop_reason`. A paused run has `output` `None` and lists
            `interrupts`.

        Raises:
            ConfigurationError: If `thread_id` is given without a checkpointer, `history` is given,
                `"awa.tasks"` names a task not on the board, or `usage_limits` is given for a nested run.
            RunTimeoutError: If `timeout` or `UsageLimits.max_duration` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
            AgentRunError: If a network with a shared task board or bus is already running in this
                process, or the lead pauses without a checkpointer.
            UsageLimitExceededError: If the run tree exceeds `usage_limits`.
        """

        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        return await self._start(ctx, input, thread_id, history, timeout=timeout)

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
        """Resume a network run that paused on human-in-the-loop interrupts.

        Every parked agent run with a decision continues; parked runs without one stay parked.

        Args:
            decision: The value for the pending interrupt, or a mapping of interrupt id to value; ids
                left out stay pending.

        Raises:
            ConfigurationError: If the network has no checkpointer, or several interrupts are pending
                and `decision` is not a mapping of their ids.
            NoPendingInterruptError: If the thread has no paused run.
            AgentRunError: If a network with a shared task board or bus is already running in this
                process.
            RunTimeoutError: If `timeout` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
        """

        thread = self._resolve_thread(thread_id)
        assert thread is not None
        record = await self._records.load(thread)
        if record is None or not (parked := parked_runs(record)):
            raise NoPendingInterruptError(
                f"Thread {thread_id!r} of network {self._name!r} has no pending interrupt"
            )
        decisions = route_decisions(self._name, parked, decision)
        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        return await self._start(
            ctx, None, None, None, timeout=timeout, restore=(thread, record, decisions)
        )

    def _stream_task(self, request: RunRequest) -> tuple[RunContext[Any], StreamStart]:
        ctx = new_run_context(self._name, request.deps, request.usage_limits, request.metadata)

        def begin(subscriber: EventSubscriber) -> Coroutine[Any, Any, RunResult[Any]]:
            return self._start(
                ctx,
                request.input,
                request.thread_id,
                request.history,
                timeout=request.timeout,
                stream_text=True,
                subscribers=(subscriber,),
            )

        return ctx, begin

    def _policy(self) -> TopologyPolicy:
        lead = self._lead.name
        names = [teammate.name for teammate in self._teammates]
        edges: dict[str, list[str]] = {lead: list(names)}
        for name in names:
            edges[name] = [lead, *self._topology.peers_of(name)]
        return TopologyPolicy(edges, allow_broadcast=[lead])

    def _fresh_bus(self) -> MessageBus:
        """The private bus of one run."""

        return self._register_mailboxes(InMemoryMessageBus(policy=self._policy()))

    def _register_mailboxes(self, bus: MessageBus) -> MessageBus:
        for agent in (self._lead, *self._teammates):
            if not bus.is_registered(agent.name):
                bus.register(agent.name)
        return bus

    def _task_tag(self) -> dict[str, Any]:
        run = self._current()
        return {RUN_TAG: run.tag} if run is not None else {}

    def _wire(self) -> None:
        catalog = AgentDirectory(self._teammates)
        blackboard = self._blackboard is not None
        lead = self._lead.name
        names = [teammate.name for teammate in self._teammates]
        self._agents[lead] = self._extend(
            self._lead,
            self._lead_tools(),
            lead_instructions(self._name, catalog.render(names), blackboard=blackboard),
            max_steps=self._lead._max_steps,
        )
        for teammate in self._teammates:
            peers = self._topology.peers_of(teammate.name)
            self._agents[teammate.name] = self._extend(
                teammate,
                self._teammate_tools(teammate),
                teammate_instructions(
                    self._name,
                    teammate.name,
                    lead,
                    peers,
                    blackboard=blackboard,
                    can_create=self._teammates_create_tasks,
                    messaging=bool(peers) or self._messaging_all,
                ),
            )
        directory = self._directory
        for original in (self._lead, *self._teammates):
            name = original.name
            replace = name in directory and directory.resolve(name) in (None, original)
            directory.register(self._agents[name], replace=replace)

    def _extend(
        self,
        agent: Agent[Any, Any],
        tools: list[BaseTool],
        prompt: str,
        *,
        max_steps: int | None = None,
    ) -> Agent[Any, Any]:
        wired = wire_agent(
            agent,
            tools,
            prompt,
            TeamMiddleware(key=self._key, agent=agent.name, bus=self._bus, max_steps=max_steps),
            owner=f"Network {self._name!r}",
            kind="network",
        )
        # With a checkpointer every agent run persists on a thread of its own.
        saver = self._checkpointer
        return wired if saver is None else wired._variant(checkpointer=saver)

    def _lead_tools(self) -> list[BaseTool]:
        lead = self._lead.name
        names = [teammate.name for teammate in self._teammates]
        board_tools = create_task_board_tools(
            self._board,
            lead,
            can_create=True,
            can_assign=True,
            directory=self._directory,
            task_data=self._task_tag,
        )
        tools = [self._limit_creation(t) for t in board_tools if t.name in LEAD_TASK_TOOLS]
        tools += create_messaging_tools(self._bus, lead, recipients=names)
        tools += create_directory_tools(self._directory, viewer=lead, visible=names)
        if self._blackboard is not None:
            tools += create_blackboard_tools(self._blackboard, lead)
        return tools

    def _teammate_tools(self, teammate: Agent[Any, Any]) -> list[BaseTool]:
        name = teammate.name
        board_tools = create_task_board_tools(
            self._board,
            name,
            can_create=True,
            capabilities=teammate.card.capabilities,
            task_data=self._task_tag,
        )
        wanted = _TEAMMATE_TASK_TOOLS | (
            _TEAMMATE_PLANNING_TOOLS if self._teammates_create_tasks else frozenset()
        )
        tools = [self._limit_creation(t) for t in board_tools if t.name in wanted]
        peers = self._topology.peers_of(name)
        if peers or self._messaging_all:  # `peers: {}` opts out; the lead's messages arrive anyway
            tools += create_messaging_tools(
                self._bus, name, recipients=[self._lead.name, *peers], allow_broadcast=False
            )
        if self._blackboard is not None:
            tools += create_blackboard_tools(self._blackboard, name)
        return tools

    def _limit_creation(self, tool: BaseTool) -> BaseTool:
        """Refuse task creation that would take the run past `max_tasks`."""

        if tool.name not in ("create_task", "create_tasks"):
            return tool

        async def create(**kwargs: Any) -> str:
            run = self._current()
            if run is None:
                return str(await tool.ainvoke(kwargs))
            count = len(kwargs["tasks"]) if tool.name == "create_tasks" else 1
            limit = self._limits.max_tasks
            left = max(0, limit - run.created - run.creating)
            if count > left:
                message = (
                    f"Not created: this run is limited to max_tasks={limit} and only {left} "
                    "more can be created."
                )
                ctx = current_run_context()
                if ctx is not None:
                    ctx.report_limit_exceeded(
                        limit="max_tasks",
                        value=run.created + run.creating + count,
                        maximum=limit,
                        message=message,
                    )
                return message
            run.creating += count
            try:
                return str(await tool.ainvoke(kwargs))
            finally:
                run.creating -= count

        return StructuredTool.from_function(
            coroutine=create,
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
        )

    def _build_card(
        self,
        description: str,
        capabilities: Sequence[str],
        tags: Sequence[str],
        version: str,
        metadata: Mapping[str, Any] | None,
    ) -> AgentCard:
        cards = [agent.card for agent in (self._lead, *self._teammates)]
        names = ", ".join(teammate.name for teammate in self._teammates)
        return AgentCard(
            name=self._name,
            description=description or f"Network led by {self._lead.name} with teammates {names}.",
            capabilities=union([tuple(capabilities), *(card.capabilities for card in cards)]),
            skills=union(card.skills for card in cards),
            role="network",
            tags=tuple(tags),
            version=version,
            metadata=dict(metadata or {}),
        )

    async def _start(
        self,
        ctx: RunContext[Any],
        input: AgentInput | None,
        thread_id: str | None,
        history: Sequence[BaseMessage | dict[str, Any]] | None,
        *,
        timeout: float | None,
        stream_text: bool = False,
        subscribers: Sequence[EventSubscriber] = (),
        restore: tuple[str, Mapping[str, Any], dict[str, Any]] | None = None,
    ) -> RunResult[Any]:
        """Refuse a run that cannot start, else run a new (or restored) run as `ctx`."""

        try:
            thread = restore[0] if restore is not None else self._resolve_thread(thread_id)
        except ConfigurationError as exc:
            await self._refuse(ctx, subscribers, exc)
        tasks = [str(t) for t in ctx.metadata.get(TASKS_KEY) or ()] if restore is None else []
        if (error := self._start_error(history, tasks)) is not None:
            await self._refuse(ctx, subscribers, error)
        if restore is not None:
            run = NetworkRun.from_record(ctx, *restore)
        else:
            assert input is not None
            run = self._new_run(ctx, input, thread, tasks)
        run.resources = self._scope.start(thread if restore is not None else None)
        ctx.internal[self._key] = run
        self._running += 1
        try:
            return await run_observed(
                ctx,
                lambda started: self._work(run, started, stream_text),
                kind="network",
                input_text=input_text(input) if input is not None else "",
                output_text=output_text,
                timeout=timeout,
                subscribers=(self._presence, *self._hooks, *subscribers),
                thread_id=thread,
            )
        finally:
            self._running -= 1

    def _start_error(
        self, history: Sequence[BaseMessage | dict[str, Any]] | None, tasks: Sequence[str]
    ) -> Exception | None:
        """Why a run cannot start now, if it cannot."""

        if history is not None:
            return ConfigurationError(
                f"AgentNetwork {self._name!r} works on a single goal; history is not "
                "supported. Pass earlier context as part of the input."
            )
        if self._running and self._scope.shared:
            return SystemBusyError(
                f"AgentNetwork {self._name!r} is already running; with a task board or bus "
                "passed to it, it works on one goal at a time. Leave them out to give "
                "every run its own, so runs can work concurrently."
            )
        unknown = [task_id for task_id in tasks if task_id not in self._board]
        if unknown:
            return ConfigurationError(
                f"AgentNetwork {self._name!r}: tasks {', '.join(unknown)} (metadata "
                f"{TASKS_KEY!r}) are not on its task board (only a task board passed to the "
                "network holds tasks before a run starts)."
            )
        return None

    @staticmethod
    def _new_run(
        ctx: RunContext[Any], input: AgentInput, thread: str | None, tasks: Sequence[str]
    ) -> NetworkRun:
        goal: list[BaseMessage] = (
            [HumanMessage(content=input)] if isinstance(input, str) else convert_to_messages(input)
        )
        run = NetworkRun(
            ctx=ctx,
            goal=goal,
            thread=thread,
            prefix=f"{thread}/{uuid4().hex[:8]}" if thread is not None else "",
            tag=ctx.run_id,
        )
        run.tasks.update(dict.fromkeys(tasks))
        return run

    def _resolve_thread(self, thread_id: str | None) -> str | None:
        # Agents of a network run outside any enclosing graph, so the network
        # always persists on a thread of its own.
        return resolve_thread(
            f"AgentNetwork {self._name!r}", self._checkpointer, thread_id, join_enclosing=False
        )

    async def _refuse(
        self, ctx: RunContext[Any], subscribers: Sequence[EventSubscriber], error: Exception
    ) -> NoReturn:
        """Report a run that cannot start as an `ErrorEvent`, then raise `error`."""

        unsubscribers = [
            ctx.events.subscribe(SubtreeSubscriber(ctx.run_id, subscriber))
            for subscriber in (*self._hooks, *subscribers)
        ]
        try:
            await ctx.emit(
                ErrorEvent(
                    error_type=type(error).__name__, message=str(error), **ctx.event_fields()
                )
            )
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()
        raise error


def _check_members(
    name: str,
    lead: Agent[Any, Any],
    teammates: Sequence[Agent[Any, Any]],
    peers: Mapping[str, Sequence[str]] | Literal["all"],
) -> None:
    validate_agent_name(name)
    for agent in (lead, *teammates):
        if not isinstance(agent, Agent):
            raise TypeError(
                f"The lead and teammates of a network must be Agents, got {type(agent).__name__}"
            )
    if any(teammate.name == lead.name for teammate in teammates):
        raise TopologyError(
            f"Network {name!r}: the lead {lead.name!r} must not also be a teammate."
        )
    if isinstance(peers, str) and peers != "all":
        raise ValueError(f"peers must be a mapping or 'all', got {peers!r}")


__stability__ = "preview"

__all__ = ["RUN_TAG", "TASKS_KEY", "AgentNetwork", "NetworkLimits", "StopReason"]
