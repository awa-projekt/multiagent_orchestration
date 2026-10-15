"""Run lifecycle shared by agents, teams, workflows and networks.

`run_observed` wraps one run with its deadline, subscribers and events; `resolve_thread` picks
its thread; `RunApi` gives every runnable `run_sync`, `start` and `stream`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeAlias, TypeVar
from uuid import uuid4

from awa._internal import aio as asyncio_compat
from awa._internal.util import elapsed_ms, preview
from awa.contracts.usage import Usage
from awa.errors import ConfigurationError, MaxStepsExceededError
from awa.runtime.cancellation import RunHandle, enter_run, start_run
from awa.runtime.context import EventSubscriber, RunContext, use_run_context
from awa.runtime.engine import current_engine

if TYPE_CHECKING:
    from awa._internal.upstream.checkpoint import BaseCheckpointSaver
    from awa._internal.upstream.messages import BaseMessage
    from awa.contracts.events import RunEvent, RunKind
    from awa.contracts.protocols import AgentInput
    from awa.contracts.results import RunResult
    from awa.contracts.usage import UsageLimits

_T = TypeVar("_T")
_DepsT = TypeVar("_DepsT")
_OutputT = TypeVar("_OutputT")

_FAILED_USAGE = "awa_usage"

THREAD_METADATA_KEY = "awa.thread_id"
"""`RunContext.metadata` key of the thread a top-level run persists on (tracing groups by it)."""

SECURITY_OFF_METADATA_KEY = "awa.security_off"
"""`RunContext.metadata` key of the security parts a top-level run ran without (tracing shows it)."""

DISABLED_METADATA_KEY = "awa.components.disabled"
"""`RunContext.metadata` key of the components switched off for a top-level run."""

REPLACED_METADATA_KEY = "awa.components.replaced"
"""`RunContext.metadata` key of the components replaced by own code for a top-level run."""

AGENT_ROLE_METADATA_KEY = "awa.agent_role"
"""`RunContext.metadata` key of the declared role of the agent a run executes (tracing shows it)."""

AGENT_SPEC_METADATA_PREFIX = "awa.agent."
"""Prefix of the `RunContext.metadata` keys that describe the agent spec a run executes
(prompt template version, prompt hash, tools, skills, selection ids); tracing puts them on the
agent's span. Set per agent run and never inherited by a nested agent."""


def usage_of_failure(error: BaseException) -> Usage | None:
    """What a top-level run had used when `error` ended it, or `None` (not an awa run's error)."""

    found = getattr(error, _FAILED_USAGE, None)
    return found if isinstance(found, Usage) else None


class SubtreeSubscriber:
    """Forwards only the events of one run and the runs nested inside it."""

    def __init__(self, run_id: str, subscriber: EventSubscriber) -> None:
        self._runs = {run_id}
        self._subscriber = subscriber

    def __call__(self, event: RunEvent) -> Awaitable[None] | None:
        from awa.contracts.events import AgentStartedEvent

        if isinstance(event, AgentStartedEvent) and event.parent_run_id in self._runs:
            self._runs.add(event.run_id)
        if event.run_id not in self._runs:
            return None
        return self._subscriber(event)


async def run_observed(
    ctx: RunContext[Any],
    work: Callable[[float], Awaitable[RunResult[Any]]],
    *,
    kind: RunKind,
    input_text: str,
    output_text: Callable[[RunResult[Any]], str],
    timeout: float | None,
    subscribers: Iterable[EventSubscriber] = (),
    thread_id: str | None = None,
    role: str | None = None,
    spec: Mapping[str, str | int | float | bool] | None = None,
) -> RunResult[Any]:
    """Execute `work(started)` as the run of `ctx`, with its deadline, subscribers and events.

    Emits `AgentStartedEvent`, then `AgentFinishedEvent` (also on a pause), `ErrorEvent` or the
    cancellation events. The thread id, the agent's own `role` and `spec` go into `ctx.metadata`
    before the start event, where tracing reads them; a top-level run's session is fixed here.
    """

    from awa.contracts.events import AgentFinishedEvent, ErrorEvent
    from awa.runtime.termination import diagnose_stop, enter_tree

    # A top-level run gets the tree's hard ceilings; a nested run gets nothing here unless its
    # root is a hand-built context.
    leave_tree = enter_tree(ctx, timeout=timeout)
    try:
        guard = enter_run(ctx, timeout)
    except BaseException:
        if leave_tree is not None:
            leave_tree()
        raise
    unsubscribers: list[Callable[[], None]] = []
    try:
        for subscriber in subscribers:
            unsubscribers.append(ctx.events.subscribe(SubtreeSubscriber(ctx.run_id, subscriber)))
        ctx.settle_session(thread_id)
        off = _annotate(ctx, kind, thread_id, role, spec)
        started = time.perf_counter()
        fields = ctx.event_fields()
        with use_run_context(ctx):
            try:
                await ctx.emit(started_event(ctx, input_text, kind))
                result = await work(started)
                await ctx.emit(
                    AgentFinishedEvent(
                        output_preview=preview(output_text(result)),
                        usage=result.usage,
                        duration_ms=result.duration_ms,
                        **fields,
                    )
                )
            except current_engine().control_flow:
                # Control passes to an enclosing graph: it records the interrupt, or runs
                # the command (for example a handoff).
                await ctx.emit(
                    AgentFinishedEvent(
                        usage=ctx.usage.snapshot(), duration_ms=elapsed_ms(started), **fields
                    )
                )
                raise
            except asyncio.CancelledError:
                if (stopped := await guard.cancelled(fields)) is None:
                    raise
                raise stopped from None
            except Exception as exc:
                error = _typed_stop(ctx, kind, exc)
                stop = await diagnose_stop(ctx, error)
                await ctx.emit(
                    ErrorEvent(
                        error_type=type(error).__name__, message=str(error), stop=stop, **fields
                    )
                )
                if error is not exc:
                    raise error from exc
                raise
            if off:
                result.metadata["security_off"] = off
            return result
    except BaseException as exc:
        if ctx.parent_run_id is None:
            # Not a `__slots__` exception: the attribute is how the caller learns the cost.
            with contextlib.suppress(AttributeError):
                setattr(exc, _FAILED_USAGE, ctx.usage.snapshot())
        raise
    finally:
        guard.exit()
        for unsubscribe in unsubscribers:
            unsubscribe()
        if leave_tree is not None:
            leave_tree()


def _annotate(
    ctx: RunContext[Any],
    kind: RunKind,
    thread_id: str | None,
    role: str | None,
    spec: Mapping[str, str | int | float | bool] | None,
) -> list[str]:
    """Put the thread, the agent's role and spec and the switched parts into `ctx.metadata`;
    return the security parts a top-level run runs without (empty for a nested run)."""

    if thread_id is not None and ctx.parent_run_id is None:
        ctx.metadata[THREAD_METADATA_KEY] = thread_id
    if kind == "agent":
        if role:
            ctx.metadata[AGENT_ROLE_METADATA_KEY] = role
        else:
            ctx.metadata.pop(AGENT_ROLE_METADATA_KEY, None)
        for key in [k for k in ctx.metadata if str(k).startswith(AGENT_SPEC_METADATA_PREFIX)]:
            del ctx.metadata[key]
        for key, value in (spec or {}).items():
            if key.startswith(AGENT_SPEC_METADATA_PREFIX):
                ctx.metadata[key] = value
    if ctx.parent_run_id is not None:
        return []
    from awa.plugins.switches import in_effect

    switched = in_effect()
    # Traces carry the scalar `awa.*` metadata of the top-level run on every span.
    for key, names in (
        (SECURITY_OFF_METADATA_KEY, switched["security_off"]),
        (DISABLED_METADATA_KEY, switched["disabled"]),
        (REPLACED_METADATA_KEY, switched["replaced"]),
    ):
        if names:
            ctx.metadata[key] = ", ".join(names)
    return switched["security_off"]


def _typed_stop(ctx: RunContext[Any], kind: RunKind, error: Exception) -> Exception:
    """`error`, or a `MaxStepsExceededError` when it means the engine step limit ran out."""

    if not current_engine().is_step_limit(error):
        return error
    return MaxStepsExceededError(
        f"The {kind} {ctx.agent!r} did not finish within its graph step limit "
        "(recursion_limit): a route kept going back without ending",
        details={"limit": "recursion_limit"},
    )


def started_event(ctx: RunContext[Any], input_text: str, kind: RunKind = "agent") -> Any:
    """The `AgentStartedEvent` of the run of `ctx`, with its ids (session, call, branch)."""

    from awa.contracts.events import AgentStartedEvent

    return AgentStartedEvent(
        parent_run_id=ctx.parent_run_id,
        input_preview=preview(input_text),
        kind=kind,
        session_id=ctx.ids.session_id or None,
        agent_call_id=ctx.ids.agent_call_id,
        tool_call_id=ctx.ids.tool_call_id,
        branch=ctx.ids.branch or None,
        **ctx.event_fields(),
    )


def run_blocking(start: Callable[[], Coroutine[Any, Any, _T]], call: str, alternative: str) -> _T:
    """Run `start()` from synchronous code (backs `RunApi.run_sync`); refused inside an event loop."""

    asyncio_compat.refuse_running_loop(call, alternative)
    return asyncio_compat.run(start())


StreamStart: TypeAlias = "Callable[[EventSubscriber], Coroutine[Any, Any, RunResult[Any]]]"
"""Starts a streamed run with the subscriber that receives its events."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    """The arguments of one `run_sync`, `start` or `stream` call."""

    input: AgentInput
    deps: Any = None
    history: Sequence[BaseMessage | dict[str, Any]] | None = None
    thread_id: str | None = None
    usage_limits: UsageLimits | None = None
    timeout: float | None = None
    metadata: Mapping[str, Any] | None = None


class RunApi(ABC, Generic[_DepsT, _OutputT]):
    """`run_sync`, `start` and `stream` over a runnable's `run` and `_stream_task`."""

    @abstractmethod
    async def run(
        self,
        input: AgentInput,
        *,
        deps: _DepsT | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
        thread_id: str | None = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[_OutputT]:
        """Run to completion."""

    @abstractmethod
    def _stream_task(self, request: RunRequest) -> tuple[RunContext[Any], StreamStart]:
        """The context of a streamed run and the function that starts it."""

    def _call_run(self, request: RunRequest) -> Coroutine[Any, Any, RunResult[_OutputT]]:
        return self.run(
            request.input,
            deps=request.deps,
            history=request.history,
            thread_id=request.thread_id,
            usage_limits=request.usage_limits,
            timeout=request.timeout,
            metadata=request.metadata,
        )

    def run_sync(
        self,
        input: AgentInput,
        *,
        deps: _DepsT | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
        thread_id: str | None = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[_OutputT]:
        """Run from synchronous code; see `run`. Refused while an event loop is running."""

        request = RunRequest(input, deps, history, thread_id, usage_limits, timeout, metadata)
        name = type(self).__name__
        return run_blocking(
            lambda: self._call_run(request), f"{name}.run_sync()", f"await {name}.run(...)"
        )

    def start(
        self,
        input: AgentInput,
        *,
        deps: _DepsT | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
        thread_id: str | None = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunHandle[RunResult[_OutputT]]:
        """Start a top-level run in the background and return its `RunHandle`; arguments as for `run`.

        Raises:
            ConfigurationError: If called inside an active run; await `run` there instead.
        """

        request = RunRequest(input, deps, history, thread_id, usage_limits, timeout, metadata)
        return start_run(lambda: self._call_run(request), what=f"{type(self).__name__}.start()")

    async def stream(
        self,
        input: AgentInput,
        *,
        deps: _DepsT | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
        thread_id: str | None = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[RunEvent]:
        """Run and yield every event of the run tree as it happens; arguments as for `run`.

        Ends with a `RunCompletedEvent` carrying the `RunResult`, includes `TextDeltaEvent`s for model
        text, and on failure yields the `ErrorEvent` before raising. Closing the iterator early cancels
        the run.
        """

        from awa.runtime._streaming import stream_run

        ctx, begin = self._stream_task(
            RunRequest(input, deps, history, thread_id, usage_limits, timeout, metadata)
        )
        async with aclosing(stream_run(begin, ctx.event_fields)) as events:
            async for event in events:
                yield event


def resolve_thread(
    owner: str,
    checkpointer: BaseCheckpointSaver[Any] | None,
    thread_id: str | None,
    *,
    join_enclosing: bool = True,
) -> str | None:
    """The thread a run persists on: `thread_id`, a new one, or `None`.

    `None` without a checkpointer, or with `join_enclosing` when nested in a checkpointed graph.

    Raises:
        ConfigurationError: If `thread_id` is given without a checkpointer.
    """

    if checkpointer is None:
        if thread_id is not None:
            raise ConfigurationError(f"{owner} has no checkpointer; thread_id requires one")
        return None
    if thread_id is not None:
        return thread_id
    if join_enclosing and current_engine().in_thread():
        return None
    return f"thread_{uuid4().hex}"
