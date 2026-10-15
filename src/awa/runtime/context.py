"""The per-run context shared by an agent, its tools, and every agent it delegates to.

`current_run_context()` reads it inside a run. Nested runs get a child context sharing `Usage`,
`UsageLimits`, `EventEmitter` and deadline, so limits cover the whole run tree.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Generic, TypeVar
from uuid import uuid4

from awa._internal.logging import get_logger
from awa._internal.util import bound
from awa.contracts.usage import Usage, UsageLimits
from awa.errors import CallCycleError, ConfigurationError, UsageLimitExceededError
from awa.runtime.ambient import global_subscribers
from awa.runtime.ids import (
    DEFAULT_TENANT,
    AgentKey,
    RunIds,
    actor_id,
    brief_hash,
    check_session_id,
    new_session_id,
)
from awa.runtime.loops import DEPTH_WARNING, DelegationBudget, LoopState, cycle_refusal

if TYPE_CHECKING:
    from awa.contracts.events import RunEvent
    from awa.runtime.cancellation import RunScope

DepsT = TypeVar("DepsT")

EventSubscriber = Callable[["RunEvent"], Awaitable[None] | None]
"""A callback receiving every event of a run. May be sync or async."""

_logger = get_logger("runtime")


def new_run_id() -> str:
    """Return a fresh, globally unique run id."""

    return f"run_{uuid4().hex}"


class EventEmitter:
    """Fan-out of run events to any number of subscribers; a raising subscriber is logged and skipped.

    Every event also reaches the global subscribers afterwards. Synchronous code queues events with
    `emit_later`; they are delivered right before the next emitted event.
    """

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []
        self._pending: list[RunEvent] = []

    def subscribe(self, subscriber: EventSubscriber) -> Callable[[], None]:
        """Register `subscriber` and return a function that unregisters it."""

        self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

        return unsubscribe

    def emit_later(self, event: RunEvent) -> None:
        """Queue `event` for delivery right before the next `emit` (for sync code)."""

        self._pending.append(event)

    async def flush(self) -> None:
        """Deliver the events queued with `emit_later`."""

        while self._pending:
            pending, self._pending = self._pending, []
            for index, event in enumerate(pending):
                try:
                    await self._deliver(event)
                except BaseException:
                    # Cancelled mid-delivery: the events behind this one stay queued, in order.
                    self._pending[:0] = pending[index + 1 :]
                    raise

    async def emit(self, event: RunEvent) -> None:
        """Deliver `event` (after any queued ones) to every subscriber, in subscription order."""

        if self._pending:
            await self.flush()
        await self._deliver(event)

    async def _deliver(self, event: RunEvent) -> None:
        for subscriber in (*self._subscribers, *global_subscribers()):
            try:
                outcome = subscriber(event)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:  # noqa: BLE001 - observers must not fail the run
                _logger.exception("Run event subscriber failed on %s", event.type)


TENANT_METADATA_KEY = "awa.tenant"
"""`run(metadata=...)` key naming the tenant of a run tree (read once, at the top-level run)."""

ACTOR_METADATA_KEY = "awa.actor"
"""`run(metadata=...)` key naming who started a run tree (read once, at the top-level run)."""

REQUEST_ID_METADATA_KEY = "awa.request_id"
"""`run(metadata=...)` key holding the id of the request that started a run tree."""

SESSION_METADATA_KEY = "awa.session_id"
"""`run(metadata=...)` key giving a run tree its session id; see `RunIds.session_id`."""

TEAM_NAME_KEY = "awa.team.name"
"""`RunContext.internal` key under which a running team names itself; nested runs copy it, so it
holds the innermost team."""


@dataclass
class RunContext(Generic[DepsT]):
    """State shared by everything executing inside one run.

    Attributes:
        deps: User-supplied dependencies for tools and dynamic instructions; inherited by nested
            runs.
        usage: Usage accumulator shared by the whole run tree.
        depth: Nesting depth; 0 for the outermost run.
        metadata: The request-scoped values of `run(metadata=...)` (by convention
            `awa.request_id`, `awa.tenant`, `awa.actor`, `awa.session_id`); JSON-serializable,
            copied into nested runs.
        scope: The deadline and stop state of the run tree (see `awa.runtime.cancel`).
        ids: The run's frozen identifiers (see `RunIds`).
        internal: Live state of extensions and the team and network code; nested runs share its
            values. Never part of events, approval requests or traces.
    """

    run_id: str
    agent: str
    deps: DepsT
    usage: Usage = field(default_factory=Usage)
    limits: UsageLimits | None = None
    events: EventEmitter = field(default_factory=EventEmitter)
    root_run_id: str = ""
    parent_run_id: str | None = None
    depth: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    scope: RunScope | None = field(default=None, repr=False, compare=False)
    ids: RunIds = field(default_factory=RunIds)
    internal: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # Whether the caller gave the session id (a thread does not replace it then).
    session_given: bool = field(default=False, repr=False, compare=False)
    loop: LoopState = field(default_factory=LoopState, repr=False, compare=False)
    budgets: tuple[DelegationBudget, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.root_run_id:
            self.root_run_id = self.run_id
        self.metadata = dict(self.metadata)
        if self.parent_run_id is None and self.ids == RunIds():
            self.ids = _root_ids(self.metadata)

    def child(
        self,
        agent: str,
        *,
        run_id: str | None = None,
        deps: Any = None,
        link: AgentKey | None = None,
        agent_call_id: str | None = None,
        tool_call_id: str | None = None,
        budget: UsageLimits | None = None,
    ) -> RunContext[Any]:
        """Create the context for a nested run of `agent`.

        The child shares `usage`, `limits`, `events` and the deadline, inherits `deps` unless new ones
        are given, and gets derived `RunIds` with one more chain link. With a `budget`, the subtree also
        gets usage of its own checked against it.

        Raises:
            UsageLimitExceededError: If the run would nest deeper than `UsageLimits.max_depth`.
        """

        self._check_depth(agent)
        ids = self.ids.for_child(
            link or AgentKey(agent), agent_call_id=agent_call_id, tool_call_id=tool_call_id
        )
        usage = self.usage
        budgets = self.budgets
        if budget is not None:
            usage = Usage.under(self.usage)
            budgets = (*budgets, DelegationBudget(ids.agent_call_id or "", usage, budget))
        internal = dict(self.internal)
        metadata = dict(self.metadata)
        # The request values naming the tenant and actor follow the frozen ids.
        if self.ids.tenant != DEFAULT_TENANT or TENANT_METADATA_KEY in metadata:
            metadata[TENANT_METADATA_KEY] = self.ids.tenant
        return RunContext(
            run_id=run_id or new_run_id(),
            agent=agent,
            deps=self.deps if deps is None else deps,
            usage=usage,
            limits=self.limits,
            events=self.events,
            root_run_id=self.root_run_id,
            parent_run_id=self.run_id,
            depth=self.depth + 1,
            metadata=metadata,
            scope=self.scope,
            ids=ids,
            internal=internal,
            session_given=self.session_given,
            loop=self.loop,
            budgets=budgets,
        )

    def _check_depth(self, agent: str) -> None:
        depth = self.depth + 1
        limit = self.limits.max_depth if self.limits is not None else None
        if limit is not None and depth > limit:
            error = UsageLimitExceededError(
                f"Usage limit exceeded: depth={depth} > max_depth={limit} (nested run of "
                f"{agent!r} below {self.ids.branch or self.agent!r})",
                details={"limit": "max_depth", "value": depth, "maximum": limit},
            )
            self.report_limit_exceeded(error)
            raise error
        if limit is None and depth > DEPTH_WARNING and not self.loop.depth_warned:
            self.loop.depth_warned = True
            _logger.warning(
                "Run tree %s nests deeper than %d (run of %r at depth %d); set "
                "UsageLimits.max_depth to bound it",
                self.root_run_id,
                DEPTH_WARNING,
                agent,
                depth,
            )

    def admit_request(self) -> None:
        """Note a model call about to start (end it with `usage.request_ended()`), or refuse it.

        Raises:
            UsageLimitExceededError: If the call would exceed `max_requests` of the run tree or of a
                delegation budget it is inside.
        """

        limit = self.limits.max_requests if self.limits is not None else None
        started = self.usage.requests_started
        if limit is not None and started >= limit:
            error = UsageLimitExceededError(
                f"Usage limit exceeded: requests={started + 1} > max_requests={limit}",
                details={"limit": "max_requests", "value": started + 1, "maximum": limit},
            )
            self.report_limit_exceeded(error)
            raise error
        for budget in self.budgets:
            cap = budget.limits.max_requests
            used = budget.usage.own_requests_started
            if cap is not None and used >= cap:
                error = UsageLimitExceededError(
                    f"Usage limit exceeded: delegation requests={used + 1} > max_requests={cap}",
                    details={
                        "limit": "delegation_max_requests",
                        "value": used + 1,
                        "maximum": cap,
                        "delegation": budget.call_id,
                    },
                )
                self.report_limit_exceeded(error, limit="delegation_max_requests")
                raise error
        self.usage.request_started()

    @property
    def tenant(self) -> str:
        """The tenant of the run tree (`ids.tenant`)."""

        return self.ids.tenant

    @property
    def session_id(self) -> str:
        """The session of the run tree (`ids.session_id`)."""

        return self.ids.session_id

    def settle_session(self, thread_id: str | None) -> None:
        """Fix the session id: the caller's, else the thread, else a new one (top-level runs).

        A nested run keeps its parent's; under a hand-built root without one (an eval case) it
        takes its thread, so the paused and resumed parts share one session.
        """

        if self.session_given or (self.parent_run_id is not None and self.ids.session_id):
            return
        if thread_id:
            self.ids = replace(self.ids, session_id=thread_id)
        elif self.parent_run_id is None and not self.ids.session_id:
            self.ids = replace(self.ids, session_id=new_session_id())

    def remaining_time(self) -> float | None:
        """Seconds left before the run tree's `timeout` or `max_duration` (never negative), or `None`."""

        return self.scope.remaining() if self.scope is not None else None

    def event_fields(self) -> dict[str, Any]:
        """Return the identifying fields every `RunEvent` requires."""

        return {
            "run_id": self.run_id,
            "root_run_id": self.root_run_id,
            "agent": self.agent,
            "depth": self.depth,
        }

    async def emit(self, event: RunEvent) -> None:
        """Emit `event` to every subscriber of this run tree."""

        await self.events.emit(event)

    def check_limits(self) -> None:
        """Raise `UsageLimitExceededError` if the run tree exceeded its limits, reporting the breach."""

        if self.limits is not None:
            try:
                self.limits.check(self.usage)
            except UsageLimitExceededError as exc:
                self.report_limit_exceeded(exc)
                raise
        for budget in self.budgets:
            try:
                budget.limits.check(budget.usage, pooled=False)
            except UsageLimitExceededError as exc:
                error = UsageLimitExceededError(
                    f"{exc} (in delegation {budget.call_id})",
                    details={
                        **exc.details,
                        "limit": "delegation_limits",
                        "delegation": budget.call_id,
                    },
                )
                self.report_limit_exceeded(error, limit="delegation_limits")
                raise error from exc

    def report_limit_exceeded(
        self,
        error: UsageLimitExceededError | None = None,
        *,
        limit: str | None = None,
        value: float | None = None,
        maximum: float | None = None,
        message: str | None = None,
    ) -> None:
        """Queue a `LimitExceededEvent` for this run (delivered with the next event).

        Pass the error about to be raised, or describe a refused operation with `limit`, `value`,
        `maximum` and `message`; explicit arguments win over the error's `details`.
        """

        from awa.contracts.events import LimitExceededEvent

        parsed = _limit_fields(error) if error is not None else {}
        name = limit or parsed.get("limit") or "usage_limit"
        self.events.emit_later(
            LimitExceededEvent(
                limit=str(name),
                value=value if value is not None else parsed.get("value"),
                maximum=maximum if maximum is not None else parsed.get("maximum"),
                message=message if message is not None else (str(error) if error else ""),
                **self.event_fields(),
            )
        )


def _limit_fields(error: UsageLimitExceededError) -> dict[str, Any]:
    """The limit, observed value and maximum a limit error carries in `details`."""

    details = error.details
    fields: dict[str, Any] = {"limit": details.get("limit")}
    for key in ("value", "maximum"):
        raw = details.get(key)
        fields[key] = float(raw) if isinstance(raw, int | float) else None
    return fields


_current_run_context: ContextVar[RunContext[Any] | None] = ContextVar(
    "awa_run_context", default=None
)
_assigned_run_id: ContextVar[str | None] = ContextVar("awa_assigned_run_id", default=None)


def current_run_context() -> RunContext[Any] | None:
    """Return the `RunContext` of the run currently executing, or `None`."""

    return _current_run_context.get()


def require_run_context() -> RunContext[Any]:
    """Return the current `RunContext`, raising `RuntimeError` outside a run."""

    context = _current_run_context.get()
    if context is None:
        raise RuntimeError("No awa run is active in the current context")
    return context


@contextmanager
def use_run_id(run_id: str) -> Iterator[str]:
    """Give the top-level runs started inside the block the run id `run_id`.

    Lets a caller know the id before the run starts; a second overlapping top-level run with the
    same id raises `ConfigurationError`.

    Example:
        ```python
        with use_run_id(request_id):
            result = await agent.run("...")
        ```
    """

    if not run_id:
        raise ValueError("run_id must not be empty")
    with bound(_assigned_run_id, run_id):
        yield run_id


def new_run_context(
    agent: str,
    deps: Any,
    usage_limits: UsageLimits | None,
    metadata: Mapping[str, Any] | None = None,
) -> RunContext[Any]:
    """The context for a run of `agent` starting now: a child inside a run, else a new root.

    Raises:
        ConfigurationError: If `usage_limits` is given for a nested run.
    """

    parent = current_run_context()
    if parent is None:
        ctx: RunContext[Any] = RunContext(
            run_id=_assigned_run_id.get() or new_run_id(),
            agent=agent,
            deps=deps,
            limits=usage_limits,
            metadata=dict(metadata or {}),
        )
        given = _given_session(metadata)
        if given is not None:
            ctx.ids = replace(ctx.ids, session_id=given)
            ctx.session_given = True
        return ctx
    if usage_limits is not None:
        raise ConfigurationError(
            "usage_limits can only be set on a top-level run; nested runs share the "
            "limits of the enclosing run"
        )
    return _nested_context(parent, agent, deps, metadata)


def _nested_context(
    parent: RunContext[Any], agent: str, deps: Any, metadata: Mapping[str, Any] | None
) -> RunContext[Any]:
    """The child of `parent` for a nested run, linked to the agent and tool call it serves."""

    _check_same_principal(parent, metadata)
    call = _pending_call.get()
    if call is not None and call.parent_run_id != parent.run_id:
        call = None
    tool_call = _tool_call.get()
    tool_call_id = tool_call[1] if tool_call is not None and tool_call[0] == parent.run_id else None
    ctx = parent.child(
        agent,
        deps=deps,
        link=call.link if call is not None else None,
        agent_call_id=call.agent_call_id if call is not None else None,
        tool_call_id=tool_call_id,
        budget=call.budget if call is not None else None,
    )
    if metadata:
        ctx.metadata.update(metadata)
        given = _given_session(metadata)
        if given is not None:
            ctx.ids = replace(ctx.ids, session_id=given)
    return ctx


def _root_ids(metadata: Mapping[str, Any]) -> RunIds:
    tenant = metadata.get(TENANT_METADATA_KEY)
    actor = metadata.get(ACTOR_METADATA_KEY)
    return RunIds(tenant=str(tenant) if tenant else DEFAULT_TENANT, actor=actor_id(actor))


def _given_session(metadata: Mapping[str, Any] | None) -> str | None:
    """The session id the caller gave (from the metadata), checked."""

    value = (metadata or {}).get(SESSION_METADATA_KEY)
    if not value:
        return None
    try:
        return check_session_id(str(value))
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def _check_same_principal(parent: RunContext[Any], metadata: Mapping[str, Any] | None) -> None:
    """Refuse a nested run that names another tenant, actor or session than its run tree."""

    if not metadata:
        return
    tenant = metadata.get(TENANT_METADATA_KEY)
    if tenant and str(tenant) != parent.ids.tenant:
        raise ConfigurationError(
            f"A nested run cannot change the tenant (run tree tenant {parent.ids.tenant!r}, "
            f"asked for {str(tenant)!r}); start a separate top-level run for another tenant"
        )
    actor = actor_id(metadata.get(ACTOR_METADATA_KEY))
    if actor and parent.ids.actor is not None and actor != parent.ids.actor:
        raise ConfigurationError(
            "A nested run cannot change the actor of its run tree; start a separate "
            "top-level run for another actor"
        )
    session = metadata.get(SESSION_METADATA_KEY)
    if session and parent.ids.session_id and str(session) != parent.ids.session_id:
        raise ConfigurationError(
            f"A nested run cannot change the session (run tree session "
            f"{parent.ids.session_id!r}, asked for {str(session)!r}); start a separate "
            "top-level run for another session"
        )


@dataclass(frozen=True, slots=True)
class _PendingCall:
    """The agent call the next nested run of `parent_run_id` belongs to (see `agent_call`)."""

    parent_run_id: str
    link: AgentKey
    agent_call_id: str | None = None
    budget: UsageLimits | None = None


_pending_call: ContextVar[_PendingCall | None] = ContextVar("awa_pending_call", default=None)
_tool_call: ContextVar[tuple[str, str] | None] = ContextVar("awa_tool_call", default=None)


@contextmanager
def agent_call(
    parent: RunContext[Any],
    link: AgentKey,
    *,
    agent_call_id: str | None = None,
    budget: UsageLimits | None = None,
) -> Iterator[None]:
    """Make the nested run `parent` starts inside the block one agent call, with `link` and an id."""

    with bound(_pending_call, _PendingCall(parent.run_id, link, agent_call_id, budget)):
        yield


@contextmanager
def nested_call(agent: str, task: str) -> Iterator[None]:
    """Make the run of `agent` started inside the block a call on `task` (no-op outside a run).

    Raises:
        CallCycleError: If `agent` already works on the same task further up the call chain.
    """

    parent = current_run_context()
    if parent is None:
        yield
        return
    link = AgentKey(agent, brief_hash=brief_hash(task))
    refusal = cycle_refusal(parent.ids.chain, link)
    if refusal is not None:
        parent.report_limit_exceeded(
            limit="call_cycle", value=len(parent.ids.chain) + 1, message=refusal
        )
        raise CallCycleError(refusal)
    with agent_call(parent, link):
        yield


@contextmanager
def tool_call_scope(run_id: str, call_id: str) -> Iterator[None]:
    """Record that run `run_id` executes the provider tool call `call_id` inside the block."""

    with bound(_tool_call, (run_id, call_id)):
        yield


@contextmanager
def use_run_context(context: RunContext[Any]) -> Iterator[RunContext[Any]]:
    """Bind `context` as the current run context for the enclosed block."""

    with bound(_current_run_context, context):
        yield context


__all__ = [
    "ACTOR_METADATA_KEY",
    "REQUEST_ID_METADATA_KEY",
    "SESSION_METADATA_KEY",
    "TEAM_NAME_KEY",
    "TENANT_METADATA_KEY",
    "DepsT",
    "EventEmitter",
    "EventSubscriber",
    "RunContext",
    "agent_call",
    "current_run_context",
    "nested_call",
    "new_run_id",
    "require_run_context",
    "tool_call_scope",
    "use_run_context",
    "use_run_id",
]
