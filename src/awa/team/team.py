"""`Team`: a hybrid multi-agent system in one object.

Example:
    ```python
    from awa import Agent, Team

    researcher = Agent("researcher", description="Finds and cites evidence.", tools=[search])
    writer = Agent("writer", description="Writes the final answer.")
    lead = Agent("lead", description="Coordinates the team.",
                 instructions="Delegate, then synthesize.")

    team = Team("desk", orchestrator=lead, members=[researcher, writer],
                peers={"researcher": ["writer"]})
    result = await team.run("Explain hybrid search.")
    ```
"""

from __future__ import annotations

from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import uuid4

from awa._internal.upstream.agents import AgentMiddleware
from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import END, START, CompiledStateGraph, StateGraph
from awa._internal.upstream.messages import BaseMessage, HumanMessage, convert_to_messages
from awa._internal.upstream.tools import BaseTool
from awa._internal.util import elapsed_ms
from awa.agents import Agent, AgentHooks
from awa.agents.agent import replace_last_human_text
from awa.agents.boundary import AgentCallGuard, as_tool_for, enter_guards
from awa.contracts.cards import AgentCard, validate_agent_name
from awa.contracts.communication import AgentMessage
from awa.contracts.events import AgentMessageEvent, HandoffEvent
from awa.contracts.protocols import AgentInput, AgentLike
from awa.contracts.results import RunResult
from awa.contracts.usage import Usage, UsageLimits
from awa.directory import AgentDirectory, create_directory_tools
from awa.errors import AgentNotFoundError, AgentRunError, NoPendingInterruptError, TopologyError
from awa.guardrails import Guardrail, run_input_guardrails, run_output_guardrails
from awa.messaging import (
    BaseBlackboard,
    InMemoryMessageBus,
    MessageBus,
    TopologyPolicy,
    create_blackboard_tools,
    create_messaging_tools,
)
from awa.persistence.records import RecordStore
from awa.runtime._run import RunApi, RunRequest, StreamStart, resolve_thread, run_observed
from awa.runtime.context import (
    TEAM_NAME_KEY,
    EventSubscriber,
    RunContext,
    current_run_context,
    new_run_context,
)
from awa.runtime.engine import EngineOutcome, Resume, RunOptions, current_engine
from awa.runtime.termination import caller_limits
from awa.tasks import BaseTaskBoard, create_task_board_tools
from awa.team._delegation import Delegator, output_text
from awa.team._delivery import TeamMiddleware, ToolLimitMiddleware, delivery_note, wire_agent
from awa.team._handoffs import (
    HISTORY_MODES,
    HandoffHistory,
    HandoffInput,
    attribute,
    build_history,
    handoff_note,
    transfer_tool,
)
from awa.team._prompts import PromptContext, team_instructions
from awa.team._run import (
    DIRECTORY_TOOLS_MIN_REACHABLE,
    MEMBER_TASK_TOOLS,
    TEAM_LEAD_TASK_TOOLS,
    TeamRun,
    lookup_run,
)
from awa.team._scoped import RunScope, ScopedBlackboard, ScopedBus, ScopedTaskBoard, facade
from awa.team._topology import MemberInfo, build_topology
from awa.team.limits import TeamLimits
from awa.tools.risk import graph_durability
from awa.workflows import Workflow
from awa.workflows._base import union

ResumeWith = Literal["last_active", "orchestrator"]
"""Who receives the next user message of a thread: the agent that held control
last, or always the orchestrator."""

_STEP = "step"


class _TeamState(TypedDict, total=False):
    """The team graph's state: the conversation and who holds control."""

    transcript: list[BaseMessage]
    turn_start: int
    active: str
    next_input: list[BaseMessage]
    next_history: list[BaseMessage] | None
    handoff_source: str | None
    handoff_reason: str
    path: list[str]
    handoffs: int
    rounds: int
    done: bool


class Team(RunApi[Any, Any]):
    """An orchestrator and its members, wired for delegation, consultation, messaging and handoffs.

    Each `Agent` gets exactly the tools its edges allow (derived with `Agent.with_tools`; the user's
    agents are never modified): `delegate_task` and the background delegation tools along
    delegation edges (several delegations in one turn run concurrently); `consult_peer` and, with
    `messaging`, `send_message` along peer edges; `transfer_to_<agent>` per handoff edge; directory
    tools for an orchestrator reaching at least three agents; workspace tools with a `blackboard`
    and task tools with a `task_board`. Tools an agent already has by name are kept.

    The task board is a shared plan: the orchestrator creates and tracks tasks and delegates them
    by id; members update, complete and fail the tasks they are given. For teammates that claim
    tasks themselves, use `AgentNetwork`. Each agent's instructions get a generated `## Your team`
    section; the builtin skills `orchestration-planning`, `delegation-briefs`,
    `agent-collaboration` and `handoff-protocol` hold longer guidance.

    A run starts with the orchestrator (or, with `resume_with="last_active"`, the agent that held
    control last) and ends when an agent finishes without transferring; outstanding background
    delegations then get up to `limits.max_handback_rounds` extra turns. Delegated members only see
    their brief. A member reaching `max_steps` answers without tools (a delegated result then starts
    with `stopped:steps`).

    Every run is isolated: limits, counters and the in-memory bus, blackboard and task board are per
    run; one passed as an object is shared by every run. The `bus`, `blackboard` and `task_board`
    properties act on the run in progress, or on the one started last.

    With a `checkpointer`, runs persist per `thread_id`, and interrupts anywhere in the run tree
    pause the team run (`RunResult.interrupts`); `resume(thread_id, decision)` continues with the
    run's counters and usage. Background delegations cannot pause, so members with approval-gated
    tools are delegated to in the foreground.

    Args:
        orchestrator: The agent that receives the user's request.
        members: Agents, nested teams, workflows or any `AgentLike`; non-agents can only be
            delegated to.
        description: What the team does; generated when empty.
        delegates: Who may delegate to whom; defaults to the orchestrator delegating to every member.
        peers: Consult/message edges between Agent members.
        symmetric_peers: Also allow every peer edge in the reverse direction.
        handoffs: Transfer edges between Agents; none by default.
        handoff_history: What an agent sees when it takes over; see `HandoffHistory`.
        resume_with: Who receives the next message of a continued thread.
        messaging: Give agents with peers mailboxes and `send_message` for one-way notes.
        blackboard: `True` for a workspace per run, or a `BaseBlackboard` shared by every run.
        task_board: `True` for a task board per run, or a `BaseTaskBoard` shared by every run.
        directory: Directory to register the team's agents in; a private one when omitted.
        bus: Message bus for `messaging`, shared by every run; by default each run gets a private
            `InMemoryMessageBus` that only allows peer edges.
        output_guardrails: Checks on the final agent's structured output, otherwise its text.
        capabilities: Extra capabilities for the team's card (the members' are always included).
        middleware: LangChain agent middleware added to every Agent of the team, after its own;
            boundary hooks (`awa.agents.boundary.AgentCallGuard`) among them decide on every
            delegation and consultation.

    Raises:
        TopologyError: If names are not unique, an edge names an unknown agent or itself, a
            delegation targets the orchestrator, a non-Agent has outgoing edges or is a peer or
            handoff target, a handoff source declares an `output_type`, a team tool clashes with an
            agent's own tool, or an agent already is the runtime agent of another team.
        AgentAlreadyRegisteredError: If `directory` holds a different agent under a team name.
    """

    def __init__(
        self,
        name: str,
        *,
        orchestrator: Agent[Any, Any],
        members: Sequence[AgentLike],
        description: str = "",
        delegates: Mapping[str, Sequence[str]] | None = None,
        peers: Mapping[str, Sequence[str]] | None = None,
        symmetric_peers: bool = True,
        handoffs: Mapping[str, Sequence[str]] | None = None,
        handoff_history: HandoffHistory = "filtered",
        resume_with: ResumeWith = "last_active",
        messaging: bool = True,
        blackboard: BaseBlackboard | bool = False,
        task_board: BaseTaskBoard | bool = False,
        directory: AgentDirectory | None = None,
        bus: MessageBus | None = None,
        limits: TeamLimits | None = None,
        input_guardrails: Sequence[Guardrail] = (),
        output_guardrails: Sequence[Guardrail] = (),
        hooks: Sequence[AgentHooks | EventSubscriber] = (),
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        capabilities: Sequence[str] = (),
        tags: Sequence[str] = (),
        version: str = "1.0.0",
        metadata: Mapping[str, Any] | None = None,
        middleware: Sequence[AgentMiddleware] = (),
    ) -> None:
        validate_agent_name(name)
        if not isinstance(orchestrator, Agent):
            raise TypeError(f"orchestrator must be an Agent, got {type(orchestrator).__name__}")
        for member in members:
            if not isinstance(member, AgentLike):
                raise TypeError(
                    f"Team members must be AgentLike (an Agent, Team or workflow), got "
                    f"{type(member).__name__}"
                )
        if not callable(handoff_history) and handoff_history not in HISTORY_MODES:
            raise ValueError(
                f"handoff_history must be one of {', '.join(HISTORY_MODES)} or a function, "
                f"got {handoff_history!r}"
            )
        if resume_with not in ("last_active", "orchestrator"):
            raise ValueError(
                f"resume_with must be 'last_active' or 'orchestrator', got {resume_with!r}"
            )
        if bus is not None and not messaging:
            raise ValueError("bus is only used with messaging=True")

        self._name = name
        self._orchestrator = orchestrator
        self._members: tuple[AgentLike, ...] = tuple(members)
        self._topology = build_topology(
            name,
            _member_info(orchestrator),
            [_member_info(member) for member in members],
            delegates=delegates,
            peers=peers,
            symmetric_peers=symmetric_peers,
            handoffs=handoffs,
        )
        self._middleware = tuple(middleware)
        self._handoff_history: HandoffHistory = handoff_history
        self._resume_with: ResumeWith = resume_with
        self._limits = limits or TeamLimits()
        self._input_guardrails = tuple(input_guardrails)
        self._output_guardrails = tuple(output_guardrails)
        self._hooks: tuple[EventSubscriber, ...] = tuple(hooks)
        self._checkpointer = checkpointer
        self._key = f"awa.team:{uuid4().hex}"
        self._card = self._build_card(description, capabilities, tags, version, metadata)

        self._directory = directory if directory is not None else AgentDirectory()
        self._counters = RecordStore(f"{name}.counters", checkpointer)
        if bus is not None:
            self._register_mailboxes(bus)
        self._scope = RunScope(
            self._key,
            bus=(bus or self._fresh_bus) if messaging else None,
            blackboard=blackboard,
            task_board=task_board,
            listener=self._on_message,
        )
        self._blackboard: BaseBlackboard | None = facade(blackboard, ScopedBlackboard(self._scope))
        self._task_board: BaseTaskBoard | None = facade(task_board, ScopedTaskBoard(self._scope))
        self._bus: MessageBus | None = (bus or ScopedBus(self._scope)) if messaging else None

        self._runtime: dict[str, AgentLike] = {}
        self._agents: dict[str, Agent[Any, Any]] = {}
        self._delegator = Delegator(
            team=name, key=self._key, runtime=self._runtime, blackboard=self._blackboard
        )
        self._wire()
        self._presence = self._directory.as_event_subscriber()
        if bus is not None:
            bus.add_listener(self._on_message)
        self._graph: CompiledStateGraph[Any, Any, Any, Any] | None = None

    @property
    def name(self) -> str:
        """The team's name."""

        return self._name

    @property
    def description(self) -> str:
        """What the team does."""

        return self._card.description

    @property
    def card(self) -> AgentCard:
        """The team's card: role `"team"`, capabilities and skills of all its agents."""

        return self._card

    @property
    def orchestrator(self) -> Agent[Any, Any]:
        """The orchestrator as given (without team tools)."""

        return self._orchestrator

    @property
    def members(self) -> tuple[AgentLike, ...]:
        """The members as given (without team tools)."""

        return self._members

    @property
    def limits(self) -> TeamLimits:
        """The team's per-run limits."""

        return self._limits

    @property
    def directory(self) -> AgentDirectory:
        """The directory the team's agents are registered in."""

        return self._directory

    @property
    def bus(self) -> MessageBus | None:
        """The message bus (of the run in progress, else of the run started last), or `None`."""

        return self._bus

    @property
    def blackboard(self) -> BaseBlackboard | None:
        """The workspace (of the run in progress, else of the run started last), if any."""

        return self._blackboard

    @property
    def task_board(self) -> BaseTaskBoard | None:
        """The task board (of the run in progress, else of the run started last), if any."""

        return self._task_board

    def agent(self, name: str) -> AgentLike:
        """Return the runtime agent of `name`: an `Agent` with its team tools, or the member itself.

        Raises:
            AgentNotFoundError: If `name` is not an agent of this team.
        """

        try:
            return self._runtime[name]
        except KeyError:
            known = ", ".join(self._runtime)
            raise AgentNotFoundError(
                f"Team {self._name!r} has no agent {name!r}. Agents: {known}"
            ) from None

    def topology(self) -> dict[str, dict[str, list[str]]]:
        """The team's edges: `{"delegates": {...}, "peers": {...}, "handoffs": {...}}`."""

        return self._topology.as_dict()

    def to_mermaid(self) -> str:
        """Render the team as a Mermaid flowchart (nested teams as subgraphs)."""

        return self._topology.to_mermaid()

    def __repr__(self) -> str:
        members = ", ".join(member.name for member in self._members)
        return (
            f"Team({self._name!r}, orchestrator={self._orchestrator.name!r}, members=[{members}])"
        )

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
        thread_id: str | None = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[Any]:
        """Run the team to completion; inside another run it nests and shares usage, limits and events.

        Args:
            input: A user prompt or a list of messages.
            deps: Dependencies available to every agent's tools via the `RunContext`.
            history: Earlier messages sent before `input`.
            thread_id: Thread to continue and persist (requires a checkpointer); created when omitted
                with a checkpointer.
            usage_limits: Limits for the whole run tree; only on a top-level run.
            timeout: Wall-clock seconds for the whole team run, background delegations included.
            metadata: Request-scoped values merged into `RunContext.metadata`; see `Agent.run`.

        Returns:
            `agent` produced the final `output`, `handoff_path` lists the agents that held control,
            `messages` is the team transcript and `usage` the whole run tree's usage. A paused run has
            `output` `None` and lists `interrupts`.

        Raises:
            ConfigurationError: If `thread_id` is given without a checkpointer, or `usage_limits` for a
                nested run.
            RunTimeoutError: If `timeout` or `UsageLimits.max_duration` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
            GuardrailTripwireError: If a team guardrail blocks input or output.
            HandoffError: If an agent keeps transferring past `max_handoffs`.
            UsageLimitExceededError: If the run tree exceeds `usage_limits`.
            AgentRunError: If an agent is interrupted without a checkpointer, or pauses on its own
                thread.
        """

        thread = self._resolve_thread(thread_id)
        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        return await self._execute(ctx, thread, turn=_Turn.of(input, history), timeout=timeout)

    def _stream_task(self, request: RunRequest) -> tuple[RunContext[Any], StreamStart]:
        thread = self._resolve_thread(request.thread_id)
        ctx = new_run_context(self._name, request.deps, request.usage_limits, request.metadata)

        def begin(subscriber: EventSubscriber) -> Coroutine[Any, Any, RunResult[Any]]:
            return self._execute(
                ctx,
                thread,
                turn=_Turn.of(request.input, request.history),
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
        """Resume a team run that paused on a human-in-the-loop interrupt.

        The interrupted agent continues with `decision` as the value of its pending `interrupt(...)`. A
        run that was cancelled or timed out without a pending interrupt continues from its last
        completed step, and `decision` is ignored.

        Args:
            decision: The value for the pending interrupt; with several, a mapping of interrupt id to
                value.

        Raises:
            ConfigurationError: If the team has no checkpointer.
            NoPendingInterruptError: If the thread neither has a pending interrupt nor was stopped
                mid-run.
            RunTimeoutError: If `timeout` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
        """

        thread = self._resolve_thread(thread_id)
        assert thread is not None
        saved = await current_engine().state(self._compiled(), thread)
        if not saved.pending and not saved.has_next:
            raise NoPendingInterruptError(
                f"Thread {thread_id!r} of team {self._name!r} has no pending interrupt"
            )
        # Without an interrupt the run was stopped mid-run: continue from its last step.
        resume = Resume(decision) if saved.pending else None
        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        return await self._execute(ctx, thread, resume=resume, timeout=timeout)

    def as_tool(self, *, name: str | None = None, description: str | None = None) -> BaseTool:
        """Expose the team as a nested-run tool returning its final text, or the JSON of structured output.

        Args:
            name: Tool name; defaults to the team name.
            description: Tool description; defaults to the team description.
        """

        return as_tool_for(self, output_text, noun="team", name=name, description=description)

    def _wire(self) -> None:
        """Give every Agent of the team its team tools and instructions, and register it."""

        self._check_tool_limits()
        catalog = AgentDirectory(self._originals())
        for info in self._topology.agents():
            original = self._original(info.name)
            if isinstance(original, Agent):
                wired = self._wired(original, catalog)
                self._runtime[info.name] = self._agents[info.name] = wired
            else:
                self._runtime[info.name] = original
        directory = self._directory
        for info in self._topology.agents():
            name = info.name
            replace = name in directory and directory.resolve(name) in (None, self._original(name))
            directory.register(self._runtime[name], replace=replace)

    def _check_tool_limits(self) -> None:
        agents = {agent.name for agent in self._originals() if isinstance(agent, Agent)}
        for limited in self._limits.tool_limits:
            if limited not in agents:
                raise TopologyError(
                    f"Team {self._name!r}: tool_limits names {limited!r}, which is not an "
                    "Agent member of the team"
                )

    def _wired(self, original: Agent[Any, Any], catalog: AgentDirectory) -> Agent[Any, Any]:
        """`original` with the tools, instructions and middleware its edges give it."""

        topology = self._topology
        name = original.name
        has_mailbox = self._bus is not None and bool(topology.peers_of(name))
        prompt = team_instructions(
            PromptContext(
                team=self._name,
                agent=name,
                orchestrator=topology.orchestrator.name,
                delegates=topology.delegates_of(name),
                peers=topology.peers_of(name),
                handoffs=topology.handoffs_of(name),
                messaging=has_mailbox,
                blackboard=self._blackboard is not None,
                task_board=self._task_board is not None,
                render=catalog.render,
            )
        )
        uses_delegator = bool(topology.delegates_of(name) or topology.peers_of(name))
        # Every member gets the team middleware: its last allowed model call (max_steps)
        # is a final answer without tools, so a member at its step limit still answers.
        middleware = TeamMiddleware(
            key=self._key,
            agent=name,
            bus=self._bus if has_mailbox else None,
            delegator=self._delegator if uses_delegator else None,
            transfer_tools=[f"transfer_to_{t}" for t in topology.handoffs_of(name)],
            requests=False,
            max_steps=original._max_steps,
        )
        limits = self._limits.tool_limits.get(name)
        extra = [
            *self._middleware,
            *([ToolLimitMiddleware(agent=name, limits=limits)] if limits else []),
        ]
        return wire_agent(
            original,
            self._tools_for(name),
            prompt,
            middleware,
            owner=f"Team {self._name!r}",
            kind="team",
            extra=extra,
        )

    def _tools_for(self, name: str) -> list[BaseTool]:
        """Every team tool `name` gets, one group per team feature."""

        topology = self._topology
        tools: list[BaseTool] = []
        if delegates := topology.delegates_of(name):
            tools += self._delegator.delegation_tools(name, delegates)
        if peers := topology.peers_of(name):
            tools.append(self._delegator.consult_tool(name, peers))
            if self._bus is not None:
                tools += create_messaging_tools(
                    self._bus, name, recipients=peers, allow_broadcast=False, replies=False
                )
        for target in topology.handoffs_of(name):
            tools.append(
                transfer_tool(
                    key=self._key,
                    team=self._name,
                    source=name,
                    target=target,
                    description=self._original(target).card.description,
                )
            )
        resources: list[BaseTool] = []
        reachable = topology.reachable(name)
        # Directory tools are for coordinating: only the orchestrator gets them (members see
        # the agents they can reach listed in their instructions).
        lead = name == topology.orchestrator.name
        if lead and len(reachable) >= DIRECTORY_TOOLS_MIN_REACHABLE:
            resources += create_directory_tools(self._directory, viewer=name, visible=reachable)
        if self._blackboard is not None:
            resources += create_blackboard_tools(self._blackboard, name)
        if self._task_board is not None:
            resources += self._task_board_tools(name)
        # An agent may already carry tools for these shared resources (for example a
        # task manager built for the team's board); it keeps its own.
        original = self._original(name)
        own = {tool.name for tool in original.tools} if isinstance(original, Agent) else set()
        return [*tools, *(tool for tool in resources if tool.name not in own)]

    def _task_board_tools(self, name: str) -> list[BaseTool]:
        board = self._task_board
        assert board is not None
        if name == self._topology.orchestrator.name:
            # The orchestrator plans and tracks; members get tasks done by delegation.
            coordination = create_task_board_tools(
                board, name, can_create=True, directory=self._directory
            )
            return [tool for tool in coordination if tool.name in TEAM_LEAD_TASK_TOOLS]
        # A member finishes the task it was given; planning stays with the orchestrator.
        member = create_task_board_tools(
            board, name, can_create=False, capabilities=self._original(name).card.capabilities
        )
        return [tool for tool in member if tool.name in MEMBER_TASK_TOOLS]

    def _fresh_bus(self) -> MessageBus:
        """The private bus of one run: mailboxes along peer edges only."""

        return self._register_mailboxes(
            InMemoryMessageBus(policy=TopologyPolicy(self._topology.peers))
        )

    def _register_mailboxes(self, bus: MessageBus) -> MessageBus:
        for name in self._topology.peers:
            if not bus.is_registered(name):
                bus.register(name)
        return bus

    def _build_card(
        self,
        description: str,
        capabilities: Sequence[str],
        tags: Sequence[str],
        version: str,
        metadata: Mapping[str, Any] | None,
    ) -> AgentCard:
        cards = [agent.card for agent in self._originals()]
        members = ", ".join(member.name for member in self._members)
        # With handoffs the final output may come from any agent holding control.
        output_schema = None if self._topology.handoffs else self._orchestrator.card.output_schema
        return AgentCard(
            name=self._name,
            description=description
            or f"Team coordinated by {self._orchestrator.name} with members {members}.",
            capabilities=union([tuple(capabilities), *(card.capabilities for card in cards)]),
            skills=union(card.skills for card in cards),
            role="team",
            tags=tuple(tags),
            output_schema=output_schema,
            version=version,
            metadata=dict(metadata or {}),
        )

    def _originals(self) -> list[AgentLike]:
        return [self._orchestrator, *self._members]

    def _original(self, name: str) -> AgentLike:
        return next(agent for agent in self._originals() if agent.name == name)

    async def _on_message(self, message: AgentMessage) -> None:
        ctx = current_run_context()
        if ctx is None or self._key not in ctx.internal or message.sender not in self._runtime:
            return
        await ctx.emit(AgentMessageEvent(message=message, **ctx.event_fields()))

    def _compiled(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        if self._graph is None:
            builder: StateGraph[Any, Any, Any, Any] = StateGraph(_TeamState)
            builder.add_node(_STEP, self._step)
            builder.add_edge(START, _STEP)
            builder.add_conditional_edges(_STEP, _route, [_STEP, END])
            self._graph = builder.compile(checkpointer=self._checkpointer, name=self._name)
        return self._graph

    def _resolve_thread(self, thread_id: str | None) -> str | None:
        return resolve_thread(f"Team {self._name!r}", self._checkpointer, thread_id)

    def _options(self, thread: str | None, stream_text: bool) -> RunOptions:
        limits = self._limits
        return RunOptions(
            run_name=self._name,
            thread_id=thread,
            # One graph step per agent turn; the team's counters bound them exactly.
            step_limit=limits.max_handoffs + limits.max_handback_rounds + 5,
            durable_writes=graph_durability(self, self._name, self._checkpointer),
            stream_text=stream_text,
        )

    async def _execute(
        self,
        ctx: RunContext[Any],
        thread: str | None,
        *,
        turn: _Turn | None = None,
        resume: Resume | None = None,
        timeout: float | None = None,
        stream_text: bool = False,
        subscribers: Sequence[EventSubscriber] = (),
    ) -> RunResult[Any]:
        input_text = turn.text() if turn is not None else ""

        async def execute(started: float) -> RunResult[Any]:
            nonlocal turn
            run = TeamRun(
                team=self._name, run_id=ctx.run_id, base_depth=ctx.depth, limits=self._limits
            )
            run.resources = self._scope.start(thread if turn is None else None)
            ctx.internal.update({self._key: run, TEAM_NAME_KEY: self._name})
            enter_guards(ctx, [m for m in self._middleware if isinstance(m, AgentCallGuard)])
            if turn is None and thread is not None:
                await self._restore_counters(ctx, run, thread)
            engine = current_engine()
            paused: str | None = None
            try:
                if turn is not None and self._input_guardrails:
                    checked = run_input_guardrails(
                        input_text, list(self._input_guardrails), raise_on_block=True
                    )
                    if checked.replacement is not None:
                        turn = turn.with_text(str(checked.replacement))
                graph_input = resume if turn is None else await self._turn_input(thread, turn)
                outcome = await engine.run(
                    self._compiled(), graph_input, self._options(thread, stream_text)
                )
                if outcome.pending and thread is not None:
                    await self._save_counters(ctx, run, thread)
                    paused = thread
            finally:
                # Also on cancellation and timeouts: unread mail is reported, not lost.
                await run.shutdown()
                await self._scope.finish(ctx, run.resources, paused_on=paused)
            return self._result(ctx, run, outcome, thread, started)

        return await run_observed(
            ctx,
            execute,
            kind="team",
            input_text=input_text,
            output_text=output_text,
            timeout=timeout,
            subscribers=(self._presence, *self._hooks, *subscribers),
            thread_id=thread,
        )

    async def _save_counters(self, ctx: RunContext[Any], run: TeamRun, thread: str) -> None:
        """Keep what the paused run counted, so its resume continues the same run."""

        record = {
            "calls": dict(run.calls),
            "started": dict(run.started),
            "usage": ctx.usage.model_dump(mode="json"),
            "limits": ctx.limits.model_dump(mode="json") if ctx.limits is not None else None,
            "loop": ctx.loop.snapshot(),
        }
        await self._counters.save(f"{thread}/awa.team", record)

    async def _restore_counters(self, ctx: RunContext[Any], run: TeamRun, thread: str) -> None:
        """Continue the counters, usage and usage limits of the paused run on `thread`."""

        record = await self._counters.load(f"{thread}/awa.team")
        if record is None:
            return
        run.calls.update(record.get("calls") or {})
        run.resumed.update(record.get("started") or {})
        # Only a top-level run restores usage; an enclosing run (an eval's) already holds it.
        if ctx.parent_run_id is None:
            ctx.usage.merge(Usage.model_validate(record["usage"]))
            # The ceilings filled in at the start are not limits the caller gave.
            if caller_limits(ctx) is None and record.get("limits") is not None:
                ctx.limits = UsageLimits.model_validate(record["limits"])
            ctx.loop.restore(record.get("loop"))

    async def _turn_input(self, thread: str | None, turn: _Turn) -> dict[str, Any]:
        previous: dict[str, Any] = {}
        if thread is not None:
            previous = dict((await current_engine().state(self._compiled(), thread)).values)
        transcript = [*previous.get("transcript", []), *turn.history]
        entry = self._topology.orchestrator.name
        last = previous.get("active")
        if self._resume_with == "last_active" and last in self._topology.active_agents():
            entry = last
        return {
            "transcript": transcript,
            "turn_start": len(transcript),
            "active": entry,
            "next_input": list(turn.messages),
            "next_history": None,
            "handoff_source": None,
            "handoff_reason": "",
            "path": [entry],
            "handoffs": 0,
            "rounds": 0,
            "done": False,
        }

    async def _step(self, state: _TeamState) -> dict[str, Any]:
        """Run the agent holding control once, then record a transfer or the final result."""

        ctx, run = lookup_run(self._key, self._name, "team step")
        name = state["active"]
        source = state.get("handoff_source")
        run.handoffs = state.get("handoffs", 0)
        run.last_handoff = (source, name) if source else None
        history = state.get("next_history")
        if history is None:
            history = build_history(
                self._handoff_history,
                HandoffInput(
                    messages=tuple(state.get("transcript", ())),
                    source=source,
                    target=name,
                    reason=state.get("handoff_reason", ""),
                ),
            )
        result = await self._agents[name].run(state.get("next_input", []), history=history)
        if result.is_interrupted:
            raise AgentRunError(
                f"Agent {name!r} paused on an interrupt on its own thread {result.thread_id!r}, "
                f"which team {self._name!r} cannot resume. Give the team a checkpointer instead."
            )
        transcript = [*state.get("transcript", []), *attribute(result.new_messages(), name)]
        transfer = run.transfers.pop(result.run_id, None)
        rounds = state.get("rounds", 0)
        handbacks = await run.settle(
            result.run_id, wait=transfer is not None or rounds < self._limits.max_handback_rounds
        )
        blocks = [task.render() for task in handbacks]
        if transfer is not None:
            await ctx.emit(
                HandoffEvent(
                    source=name,
                    target=transfer.target,
                    reason=transfer.reason,
                    **ctx.event_fields(),
                )
            )
            note = handoff_note(name, transfer.target, transfer.reason)
            return {
                "transcript": transcript,
                "active": transfer.target,
                "next_input": [note, delivery_note(blocks)] if blocks else [note],
                "next_history": None,
                "handoff_source": name,
                "handoff_reason": transfer.reason,
                "path": [*state.get("path", []), transfer.target],
                "handoffs": run.handoffs + 1,
            }
        if blocks:
            return {
                "transcript": transcript,
                "next_input": [delivery_note(blocks)],
                "next_history": result.messages,
                "rounds": rounds + 1,
            }
        run.final = result
        return {"transcript": transcript, "done": True}

    def _result(
        self,
        ctx: RunContext[Any],
        run: TeamRun,
        outcome: EngineOutcome,
        thread: str | None,
        started: float,
    ) -> RunResult[Any]:
        state = outcome.values
        transcript: list[BaseMessage] = list(state.get("transcript", []))
        common: dict[str, Any] = {
            "run_id": ctx.run_id,
            "messages": transcript,
            "new_messages_start": state.get("turn_start", 0),
            "usage": ctx.usage.snapshot(),
            "duration_ms": elapsed_ms(started),
            "handoff_path": tuple(state.get("path", ())),
            "thread_id": thread,
        }
        interrupts = [{"id": pause.id, "value": pause.value} for pause in outcome.pending]
        if interrupts:
            return RunResult(
                agent=state.get("active", self._topology.orchestrator.name),
                output=None,
                interrupts=interrupts,
                **common,
            )
        final = run.final
        if final is None:
            raise AgentRunError(f"Team {self._name!r} finished without a final result")
        output, text = final.output, final.text
        if self._output_guardrails:
            checked = run_output_guardrails(
                output, list(self._output_guardrails), raise_on_block=True
            )
            if checked.replacement is not None:
                # A guardrail sanitized the output; the thread keeps the original.
                output = checked.replacement
                text = output if isinstance(output, str) else text
        return RunResult(agent=final.agent, output=output, text=text, **common)


@dataclass(frozen=True)
class _Turn:
    """The input of one run: new messages, plus optional earlier history."""

    messages: list[BaseMessage]
    history: list[BaseMessage]

    @classmethod
    def of(cls, input: AgentInput, history: Sequence[BaseMessage | dict[str, Any]] | None) -> _Turn:
        messages: list[BaseMessage] = (
            [HumanMessage(content=input)] if isinstance(input, str) else convert_to_messages(input)
        )
        return cls(messages, convert_to_messages(history) if history else [])

    def text(self) -> str:
        """The text of the last user message."""

        for message in reversed(self.messages):
            if isinstance(message, HumanMessage):
                return str(message.text)
        return ""

    def with_text(self, text: str) -> _Turn:
        """This turn with the text of its last user message replaced."""

        return _Turn(replace_last_human_text(self.messages, text), self.history)


def _route(state: _TeamState) -> str:
    return END if state.get("done") else _STEP


def _member_info(agent: AgentLike) -> MemberInfo:
    if isinstance(agent, Agent):
        return MemberInfo(agent.name, "agent", has_output_type=agent.output_type is not None)
    if isinstance(agent, Team):
        return MemberInfo(agent.name, "team", nested=agent._topology)
    return MemberInfo(agent.name, "workflow" if isinstance(agent, Workflow) else "other")


__all__ = ["ResumeWith", "Team"]
