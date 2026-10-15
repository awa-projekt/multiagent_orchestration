"""Shared run machinery for the workflow combinators, also used by teams and networks.

Every workflow compiles into a small LangGraph graph; its state records what each finished
member produced, so `resume` continues at the member that was running.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import pydantic_core

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import CompiledStateGraph, StateGraph
from awa._internal.upstream.messages import BaseMessage, convert_to_messages
from awa._internal.util import elapsed_ms
from awa.contracts.cards import AgentCard, validate_agent_name
from awa.contracts.protocols import AgentInput, AgentLike
from awa.contracts.results import RunResult
from awa.contracts.usage import Usage, UsageLimits
from awa.errors import AgentRunError, ConfigurationError, NoPendingInterruptError
from awa.runtime._run import RunApi, RunRequest, StreamStart, resolve_thread, run_observed
from awa.runtime._streaming import text_streaming
from awa.runtime.context import EventSubscriber, RunContext, new_run_context
from awa.runtime.engine import Pause, Resume, RunOptions, current_engine
from awa.runtime.termination import output_digest
from awa.tools.risk import graph_durability

StepRecord = dict[str, Any]
"""What a finished member produced, as stored in the checkpointed graph state
(plain data: see `record_result` and `restore_result`)."""


@dataclass
class _Outcome:
    """What a workflow produced from its final graph state."""

    output: Any
    text: str
    messages: list[BaseMessage] = field(default_factory=list)
    new_messages_start: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, result: RunResult[Any], **metadata: Any) -> _Outcome:
        """The outcome passing on one member's result."""

        return cls(
            output=result.output,
            text=result.text,
            messages=list(result.messages),
            new_messages_start=result.new_messages_start,
            metadata=metadata,
        )


@dataclass(frozen=True)
class _Origin:
    """Where in a (possibly nested) workflow an interrupt was raised."""

    path: tuple[str, ...]
    order: tuple[int, ...]
    position: int


@dataclass(eq=False)
class WorkflowRun:
    """In-memory state of one workflow run segment, read by the graph nodes.

    Attributes:
        slots: Bounds the members running at once (`Parallel.max_concurrency`).
        errors: Exceptions of failed branches (`Parallel(fail_fast=False)`).
    """

    workflow: Workflow
    ctx: RunContext[Any]
    deps: Any
    slots: asyncio.Semaphore | None = None
    errors: dict[str, Exception] = field(default_factory=dict)


_active: ContextVar[WorkflowRun | None] = ContextVar("awa_workflow_run", default=None)
"""The workflow run whose graph the current code executes in."""

_origins: ContextVar[dict[str, _Origin] | None] = ContextVar(
    "awa_workflow_interrupt_origins", default=None
)
"""Interrupt id -> origin, shared by every workflow level of one top-level run."""


class Workflow(RunApi[Any, Any], ABC):
    """Base class of the deterministic workflow combinators; a workflow is `AgentLike`.

    A nested run gets a child of the active `RunContext`, so usage, limits and events are shared by
    the whole run tree. With a `checkpointer`, runs persist per `thread_id` and pause on interrupts
    anywhere in a member: `RunResult.interrupts` lists them with the `agent` and the `path` of member
    names, and `resume(thread_id, decision)` continues there without running finished members
    again; a cancelled, timed-out or crashed run resumes the same way. Nested in a checkpointed run,
    a workflow persists on the enclosing thread.

    Workflows keep no conversation between runs: a new `run` on a thread starts afresh, and
    `history` is not supported.
    """

    _kind: str

    def __init__(
        self, name: str, *, description: str, checkpointer: BaseCheckpointSaver[Any] | None = None
    ) -> None:
        self._name = validate_agent_name(name)
        self._description = description
        self._checkpointer = checkpointer
        self._graph: CompiledStateGraph[Any, Any, Any, Any] | None = None

    @property
    def name(self) -> str:
        """Unique name of the workflow."""

        return self._name

    @property
    def description(self) -> str:
        """The configured description, or a generated one naming the members."""

        return self._description or self._default_description()

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any] | None:
        """The checkpointer runs persist in, if any."""

        return self._checkpointer

    @property
    def card(self) -> AgentCard:
        """Public description; capabilities and skills are the union of the members'."""

        cards = [member.card for member in self._members()]
        return AgentCard(
            name=self._name,
            description=self.description,
            capabilities=union(card.capabilities for card in cards),
            skills=union(card.skills for card in cards),
            role="workflow",
            tags=("workflow", self._kind),
            input_schema=self._input_schema(cards),
            output_schema=self._output_schema(cards),
        )

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
        """Run the workflow to completion, or until it pauses on an interrupt.

        Args:
            input: A prompt or a list of messages, passed to the first member(s).
            usage_limits: Limits for the whole run tree; only on a top-level run.
            thread_id: Thread to persist the run under (requires a checkpointer); created when omitted
                with a checkpointer. A run on an existing thread starts the workflow afresh.
            history: Not supported; pass earlier messages as part of `input`.
            timeout: Wall-clock seconds for the whole workflow, every member run included.
            metadata: Request-scoped values merged into `RunContext.metadata`; see `Agent.run`.

        Returns:
            The result; `usage` covers the whole run tree. A paused run has `output` `None` and lists
            `interrupts`.

        Raises:
            ConfigurationError: If `thread_id` is given without a checkpointer, `history` is given, or
                `usage_limits` is given for a nested run.
            RunTimeoutError: If `timeout` or `UsageLimits.max_duration` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
            AgentRunError: If a member is interrupted and no run has a checkpointer to pause on.
        """

        thread = self._prepare(thread_id, history)
        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        return await self._run(ctx, input, deps, thread=thread, timeout=timeout)

    def _stream_task(self, request: RunRequest) -> tuple[RunContext[Any], StreamStart]:
        thread = self._prepare(request.thread_id, request.history)
        ctx = new_run_context(self._name, request.deps, request.usage_limits, request.metadata)

        def begin(subscriber: EventSubscriber) -> Coroutine[Any, Any, RunResult[Any]]:
            return self._run(
                ctx,
                request.input,
                request.deps,
                thread=thread,
                timeout=request.timeout,
                subscriber=subscriber,
            )

        return ctx, begin

    async def resume(
        self,
        thread_id: str,
        decision: Any,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[Any]:
        """Resume a workflow run that paused on a human-in-the-loop interrupt.

        The interrupted member continues with `decision` as the value of its pending `interrupt(...)`
        (for an approval-gated tool, `{"action": "approve"}`); finished members are not run again. A
        run that was cancelled, timed out or crashed continues from its last finished member, and
        `decision` is ignored.

        Args:
            decision: The value for the pending interrupt; with several, a mapping of interrupt id to
                value.

        Raises:
            ConfigurationError: If the workflow has no checkpointer.
            NoPendingInterruptError: If the thread neither has a pending interrupt nor was stopped
                mid-run.
            RunTimeoutError: If `timeout` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
        """

        thread = resolve_thread(f"Workflow {self._name!r}", self._checkpointer, thread_id)
        assert thread is not None
        saved = await current_engine().state(self._compiled(), thread)
        if not saved.pending and not saved.has_next:
            raise NoPendingInterruptError(
                f"Thread {thread_id!r} of workflow {self._name!r} has no pending interrupt"
            )
        # Without an interrupt the run was stopped mid-run: continue from its last step.
        resume = Resume(decision, by_pending_id=True) if saved.pending else None
        ctx = new_run_context(self._name, deps, usage_limits, metadata)
        return await self._run(ctx, resume, deps, thread=thread, timeout=timeout)

    async def run_member(
        self, member: AgentLike, step_input: AgentInput, index: int
    ) -> RunResult[Any]:
        """Run one member in the current node; refuse a member paused on a thread of its own."""

        run = self._current_run()
        engine = current_engine()
        try:
            result = await member.run(step_input, deps=run.deps)
        except engine.control_flow as exc:
            _note_origins(engine.pauses(exc), member.name, index)
            raise
        if result.is_interrupted:
            raise AgentRunError(
                f"Member {member.name!r} of workflow {self._name!r} paused on a "
                "human-in-the-loop interrupt on its own thread "
                f"{result.thread_id!r}, which the workflow cannot resume. Give the "
                "workflow a checkpointer (instead of the member) so the interrupt "
                "pauses the whole workflow."
            )
        run.ctx.check_limits()
        return result

    def _current_run(self) -> WorkflowRun:
        run = _active.get()
        if run is None or run.workflow is not self:
            raise AgentRunError(
                f"Workflow {self._name!r}: a graph node ran outside a run of this workflow"
            )
        return run

    def _prepare(
        self, thread_id: str | None, history: Sequence[BaseMessage | dict[str, Any]] | None
    ) -> str | None:
        if history is not None:
            raise ConfigurationError(
                f"Workflow {self._name!r} does not take history; pass earlier messages as "
                "part of the input"
            )
        return resolve_thread(f"Workflow {self._name!r}", self._checkpointer, thread_id)

    def _compiled(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        if self._graph is None:
            self._graph = self._build_graph().compile(
                checkpointer=self._checkpointer, name=self._name, cache=self._node_cache()
            )
        return self._graph

    def _node_cache(self) -> Any:
        """The LangGraph cache of the graph's cached nodes; `None` when no node is cached."""

        return None

    async def _run(
        self,
        ctx: RunContext[Any],
        input: AgentInput | Resume | None,
        deps: Any,
        *,
        thread: str | None,
        timeout: float | None = None,
        subscriber: EventSubscriber | None = None,
    ) -> RunResult[Any]:
        shown = "" if input is None or isinstance(input, Resume) else input_text(input)

        async def execute(started: float) -> RunResult[Any]:
            run = WorkflowRun(workflow=self, ctx=ctx, deps=deps)
            run_token = _active.set(run)
            origins_token = _origins.set({}) if _origins.get() is None else None
            try:
                self._prepare_run(run)
                graph_input: Any = input
                if input is not None and not isinstance(input, Resume):
                    stored = input if isinstance(input, str) else convert_to_messages(input)
                    graph_input = self._initial_state(stored)
                options = RunOptions(
                    run_name=self._name,
                    thread_id=thread,
                    step_limit=self._recursion_limit(),
                    durable_writes=graph_durability(self, self._name, self._checkpointer),
                    max_concurrency=self._node_concurrency(),
                )
                outcome = await current_engine().run(self._compiled(), graph_input, options)
                return self._result(ctx, run, outcome.values, outcome.pending, thread, started)
            finally:
                _active.reset(run_token)
                if origins_token is not None:
                    _origins.reset(origins_token)

        with text_streaming() if subscriber is not None else nullcontext():
            return await run_observed(
                ctx,
                execute,
                kind="workflow",
                input_text=shown,
                output_text=lambda result: result.text,
                timeout=timeout,
                subscribers=(subscriber,) if subscriber is not None else (),
                thread_id=thread,
            )

    def _result(
        self,
        ctx: RunContext[Any],
        run: WorkflowRun,
        state: Mapping[str, Any],
        pending: Sequence[Pause],
        thread: str | None,
        started: float,
    ) -> RunResult[Any]:
        if pending and thread is None:
            raise AgentRunError(
                f"Workflow {self._name!r} was interrupted, but it has no checkpointer to "
                "pause on. Give the workflow (or the run it is nested in) a checkpointer "
                "to use human-in-the-loop interrupts."
            )
        outcome = _Outcome(output=None, text="") if pending else self._outcome(state, run)
        return RunResult(
            run_id=ctx.run_id,
            agent=self._name,
            output=outcome.output,
            text=outcome.text,
            messages=outcome.messages,
            new_messages_start=outcome.new_messages_start,
            usage=ctx.usage.snapshot(),
            duration_ms=elapsed_ms(started),
            thread_id=thread,
            metadata=outcome.metadata,
            interrupts=_describe_interrupts(pending),
        )

    @abstractmethod
    def _members(self) -> Sequence[AgentLike]:
        """The workflow's direct members, in declared order."""

    @abstractmethod
    def _default_description(self) -> str:
        """Description used when none was configured."""

    @abstractmethod
    def _build_graph(self) -> StateGraph[Any, Any, Any, Any]:
        """The graph whose nodes run the members (compiled once, on first use)."""

    @abstractmethod
    def _initial_state(self, input: str | list[BaseMessage]) -> dict[str, Any]:
        """The graph input of a new run on `input`."""

    @abstractmethod
    def _outcome(self, state: Mapping[str, Any], run: WorkflowRun) -> _Outcome:
        """Combine the final graph state into the workflow's result."""

    @abstractmethod
    def _recursion_limit(self) -> int:
        """Graph steps one run needs at most."""

    def _node_concurrency(self) -> int | None:
        """The most graph nodes running at once, or `None` for no bound."""

        return None

    def _prepare_run(self, run: WorkflowRun) -> None:  # noqa: B027 - optional hook
        """Set up per-run resources on `run` (for example concurrency slots)."""

    def _input_schema(self, cards: Sequence[AgentCard]) -> dict[str, Any] | None:
        return cards[0].input_schema

    def _output_schema(self, cards: Sequence[AgentCard]) -> dict[str, Any] | None:
        return cards[-1].output_schema

    def __repr__(self) -> str:
        members = ", ".join(member.name for member in self._members())
        return f"{type(self).__name__}({self._name!r}, [{members}])"


def union(groups: Iterable[Iterable[str]]) -> tuple[str, ...]:
    """The items of `groups` in first-seen order, without duplicates."""

    return tuple(dict.fromkeys(item for group in groups for item in group))


def out_of_steps(remaining: int | None, steps: int) -> bool:
    """Whether `steps` more graph steps would exceed the recursion limit (`remaining` included).

    A workflow that would run out routes to its end, so the run ends with a stop diagnosis rather
    than a `GraphRecursionError`.
    """

    return remaining is not None and remaining <= steps


def stop_diagnosis(
    name: str, bound: str, value: int, maximum: int, fix: str, **extra: Any
) -> dict[str, Any]:
    """Why `bound` (`identical_*`: no progress) ended workflow `name` at `value`."""

    reason = "no_progress" if bound.startswith("identical_") else "steps"
    return {
        "stop_reason": f"stopped:{reason}",
        "bound": bound,
        "value": value,
        "maximum": maximum,
        "agent": name,
        **extra,
        "suggested_fix": fix,
        "source": "rules",
    }


def step_limit_stop(name: str, limit: int, value: int) -> dict[str, Any]:
    """The stop diagnosis of a workflow that ended early to stay within its recursion limit."""

    fix = (
        "the workflow's graph step budget would have run out before the next round: "
        "lower max_iterations, or start a new run on the last result to continue"
    )
    return stop_diagnosis(name, "recursion_limit", value, limit, fix)


def progress(state: Mapping[str, Any], result: RunResult[Any]) -> dict[str, Any]:
    """No-progress state: `result`'s digest (`_digest`) and the identical results in a row (`_same`)."""

    digest = output_digest(step_text(result))
    same = state.get("_same", 0) + 1 if digest == state.get("_digest") else 1
    return {"_digest": digest, "_same": same}


def record_result(result: RunResult[Any]) -> StepRecord:
    """A member's result as plain data for the checkpointed graph state."""

    return {
        "run_id": result.run_id,
        "agent": result.agent,
        "output": result.output,
        "text": result.text,
        "messages": list(result.messages),
        "new_messages_start": result.new_messages_start,
        "usage": result.usage.model_dump(mode="json"),
        "duration_ms": result.duration_ms,
        "handoff_path": list(result.handoff_path),
        "metadata": dict(result.metadata),
    }


def restore_result(record: StepRecord) -> RunResult[Any]:
    """The `RunResult` of a member, rebuilt from its `record_result` record."""

    return RunResult(
        run_id=record["run_id"],
        agent=record["agent"],
        output=record["output"],
        text=record["text"],
        messages=list(record.get("messages", [])),
        new_messages_start=record.get("new_messages_start", 0),
        usage=Usage.model_validate(record.get("usage") or {}),
        duration_ms=record.get("duration_ms", 0),
        handoff_path=tuple(record.get("handoff_path", ())),
        metadata=dict(record.get("metadata") or {}),
    )


def merge_records(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Graph-state reducer merging records written by concurrent nodes."""

    return {**(left or {}), **(right or {})}


def input_text(input: AgentInput) -> str:
    """Return the text of a prompt, or of the last user message of a message list."""

    if isinstance(input, str):
        return input
    fallback = ""
    for index, message in enumerate(reversed(input)):
        if isinstance(message, BaseMessage):
            role, text = message.type, str(message.text)
        else:
            role, text = str(message.get("role", "")), _content_text(message.get("content", ""))
        if role in ("human", "user"):
            return text
        if index == 0:
            fallback = text
    return fallback


def step_text(result: RunResult[Any]) -> str:
    """A member's final text, or the JSON of its output when the text is empty."""

    if result.text or result.output is None:
        return result.text
    if isinstance(result.output, str):
        return result.output
    return pydantic_core.to_json(result.output).decode()


def labeled(name: str, text: str) -> str:
    """Render a member's text for a combined transcript."""

    return f"[{name}]: {text}"


def _note_origins(pauses: Sequence[Pause], member: str, index: int) -> None:
    """Prefix the origin of each pause with the member that raised it."""

    origins = _origins.get()
    if origins is None:
        return
    for position, pause in enumerate(pauses):
        inner = origins.get(pause.id, _Origin((), (), position))
        origins[pause.id] = _Origin((member, *inner.path), (index, *inner.order), inner.position)


def _describe_interrupts(pending: Sequence[Pause]) -> list[dict[str, Any]]:
    """The pending interrupts in member order, then tool-call order, with their origin."""

    origins = _origins.get() or {}
    described: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for arrival, pause in enumerate(pending):
        entry: dict[str, Any] = {"id": pause.id, "value": pause.value}
        origin = origins.get(pause.id)
        if origin is None:
            key: tuple[Any, ...] = (1, (), arrival)
        else:
            entry["agent"] = origin.path[-1]
            entry["path"] = list(origin.path)
            key = (0, origin.order, origin.position)
        described.append((key, entry))
    return [entry for _, entry in sorted(described, key=lambda item: item[0])]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, str) or (isinstance(item, dict) and item.get("type") == "text")
        ]
        return "".join(parts)
    return str(content)
