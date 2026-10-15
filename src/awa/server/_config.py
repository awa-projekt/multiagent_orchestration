"""What the server serves: `ServedSystem`, request-side deps and limits (no web import)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypedDict

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    convert_to_messages,
)
from awa.agents import Agent
from awa.contracts.events import RunEvent
from awa.contracts.protocols import AgentLike
from awa.contracts.usage import UsageLimits, merge_limits
from awa.persistence.threads import BaseIdempotencyCache, BaseThreadIndex
from awa.runtime.context import EventSubscriber
from awa.server._auth import Authenticator, Principal
from awa.server._limits import BaseRateLimiter, RateLimit
from awa.server.errors import InvalidRequestError, SystemNotFoundError
from awa.team import AgentNetwork, Team
from awa.workflows import Loop, Map, Parallel, Sequential, Workflow

if TYPE_CHECKING:
    from awa.persistence.retention import SubjectIndex

SystemKind: TypeAlias = Literal["agent", "team", "network", "workflow", "custom"]
ThreadScope: TypeAlias = Literal["tenant", "principal", "shared"]
SseOverflow: TypeAlias = Literal["drop", "disconnect"]
ReadinessCheck: TypeAlias = Callable[[], Awaitable[bool | None]]
AuditSubscriber: TypeAlias = Callable[[RunEvent], Any]

API_PREFIX = "/v1"
"""Path prefix of every endpoint."""

DEFAULT_MAX_BODY_BYTES = 1024 * 1024
"""Default limit on the size of a request body (1 MiB)."""

SYSTEM_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
"""Names systems are served under (the `{name}` path segment)."""

THREAD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
"""Thread ids a client may send."""

DepsFactory: TypeAlias = Callable[[dict[str, Any]], Any | Awaitable[Any]]
PrincipalDeps: TypeAlias = Callable[[Principal], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class ServingKw(TypedDict, total=False):
    """The serving keywords of `create_app`, `create_router` and `serve`, for type checkers.

    Not exported: callers pass the keywords, documented under `create_app`'s Other Parameters.
    """

    checkpointer: BaseCheckpointSaver[Any] | None
    api_key: str | None
    authenticator: Authenticator | None
    sse_ping_interval: float | None
    thread_scope: ThreadScope
    thread_index: BaseThreadIndex | None
    rate_limit: RateLimit | BaseRateLimiter | None
    idempotency_cache: BaseIdempotencyCache | None
    idempotency_ttl: float
    drain_timeout: float | None
    run_retention: float
    sse_queue_size: int
    sse_overflow: SseOverflow
    readiness_checks: Sequence[ReadinessCheck]
    readiness_timeout: float
    audit_log: AuditSubscriber | None
    subject_index: SubjectIndex | None
    subscribers: Sequence[EventSubscriber]


@dataclass(frozen=True)
class ServingOptions:
    """The serving options of `ServingKw` with their defaults, checked once."""

    checkpointer: BaseCheckpointSaver[Any] | None = None
    api_key: str | None = None
    authenticator: Authenticator | None = None
    sse_ping_interval: float | None = 15.0
    thread_scope: ThreadScope = "tenant"
    thread_index: BaseThreadIndex | None = None
    rate_limit: RateLimit | BaseRateLimiter | None = None
    idempotency_cache: BaseIdempotencyCache | None = None
    idempotency_ttl: float = 24 * 3600.0
    drain_timeout: float | None = 30.0
    run_retention: float = 3600.0
    sse_queue_size: int = 1000
    sse_overflow: SseOverflow = "drop"
    readiness_checks: Sequence[ReadinessCheck] = ()
    readiness_timeout: float = 5.0
    audit_log: AuditSubscriber | None = None
    subject_index: SubjectIndex | None = None
    subscribers: Sequence[EventSubscriber] = ()

    def __post_init__(self) -> None:
        for subscriber in self.subscribers:
            if not callable(subscriber):
                raise TypeError(f"subscribers must be callables, got {type(subscriber).__name__}")
        if self.api_key is not None and not self.api_key.strip():
            raise ValueError("api_key must not be empty; pass None to disable authentication")
        if self.api_key is not None and self.authenticator is not None:
            raise ValueError("Pass either api_key or authenticator, not both")
        if self.authenticator is not None and not isinstance(self.authenticator, Authenticator):
            raise TypeError("authenticator must have an async authenticate(request) method")
        if self.sse_ping_interval is not None and self.sse_ping_interval <= 0:
            raise ValueError("sse_ping_interval must be positive or None")
        if self.thread_scope not in ("tenant", "principal", "shared"):
            raise ValueError("thread_scope must be 'tenant', 'principal' or 'shared'")
        if self.sse_overflow not in ("drop", "disconnect"):
            raise ValueError("sse_overflow must be 'drop' or 'disconnect'")
        for option in ("idempotency_ttl", "run_retention", "sse_queue_size", "readiness_timeout"):
            if getattr(self, option) <= 0:
                raise ValueError(f"{option} must be positive")
        if self.drain_timeout is not None and self.drain_timeout < 0:
            raise ValueError("drain_timeout must not be negative")


@dataclass(frozen=True)
class ServedSystem:
    """A system plus the rules for serving it.

    Args:
        allowed_deps: Keys a request may send in `deps`; empty means none (400 otherwise).
        deps_factory: Builds the run's `deps` (sync or async) from the accepted keys and the
            `principal_deps`; a `ValueError` rejects the request with 400. Without it the dict
            itself is passed (or `None` when empty).
        usage_limits: Ceiling for every run: a request's limits can only tighten it. A cost
            limit is taken from the ceiling when it sets one.
        exclusive: One request at a time, 409 while busy; `None` means exclusive for an
            `AgentNetwork`.
        diagram: A Mermaid diagram for a system without its own `to_mermaid()`.
        principal_deps: Derives deps (sync or async) from the authenticated caller; clients
            cannot send these keys. A resumed run gets the deps of the thread's owner, known
            only by id and tenant when someone else resumes it: derive deps from those two.
        allow_system_messages: Accept `system` and `tool` messages (and assistant tool calls) in
            `input` and `history`; off so clients cannot replace the prompt or forge tool results.
        max_concurrency: Runs in flight at once in this process; others wait up to
            `queue_timeout` seconds, then get 503.
        rate_limit: Runs and resumes per principal for this system, on top of the app's limit.
        approver_roles: Roles allowed to resume; `None` lets every caller who sees the thread.
        run_timeout: Seconds a run may take, then it is stopped with its nested runs (504).
        reviewer_key: Key under which the reviewer's identity is added to a mapping decision;
            `None` passes decisions unchanged.

    Example:
        ```python
        served = ServedSystem(
            support_team,
            principal_deps=lambda caller: {"user_id": caller.id},
            deps_factory=lambda sent: SupportDeps(db=db, **sent),
            usage_limits=UsageLimits(max_requests=40),
            max_concurrency=20,
            approver_roles={"approver"},
        )
        app = create_app({"support": served}, authenticator=auth)
        ```
    """

    system: AgentLike
    allowed_deps: Collection[str] = field(default_factory=frozenset)
    deps_factory: DepsFactory | None = None
    usage_limits: UsageLimits | None = None
    exclusive: bool | None = None
    diagram: str | None = None
    principal_deps: PrincipalDeps | None = None
    allow_system_messages: bool = False
    max_concurrency: int | None = None
    queue_timeout: float = 0.0
    rate_limit: RateLimit | BaseRateLimiter | None = None
    approver_roles: Collection[str] | None = None
    reviewer_key: str | None = "reviewer"
    run_timeout: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.system, AgentLike):
            raise TypeError(
                f"ServedSystem needs an AgentLike system, got {type(self.system).__name__}"
            )
        object.__setattr__(self, "allowed_deps", frozenset(self.allowed_deps))
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1 or None")
        if self.run_timeout is not None and not self.run_timeout > 0:
            raise ValueError("run_timeout must be positive or None")
        if self.queue_timeout < 0:
            raise ValueError("queue_timeout must not be negative")
        if self.approver_roles is not None:
            if isinstance(self.approver_roles, str):
                raise TypeError("approver_roles must be a collection of role names")
            object.__setattr__(self, "approver_roles", frozenset(self.approver_roles))
        if self.rate_limit is not None and not isinstance(
            self.rate_limit, RateLimit | BaseRateLimiter
        ):
            raise TypeError("rate_limit must be a RateLimit or BaseRateLimiter")

    @property
    def is_exclusive(self) -> bool:
        """Whether the system runs one request at a time."""

        if self.exclusive is not None:
            return self.exclusive
        return isinstance(self.system, AgentNetwork)

    def may_approve(self, principal: Principal) -> bool:
        """Whether `principal` may resume paused runs of this system."""

        if self.approver_roles is None:
            return True
        return principal.has_role(*self.approver_roles)

    async def resolve_deps(
        self, sent: Mapping[str, Any] | None, principal: Principal | None = None
    ) -> Any:
        """Validate a request's `deps`, merge the `principal_deps` and build the run's deps.

        Raises:
            InvalidRequestError: If `sent` has keys outside `allowed_deps` or that
                `principal_deps` sets, or `deps_factory` rejects them.
        """

        if sent:
            if not self.allowed_deps:
                raise InvalidRequestError(
                    f"System {self.system.name!r} does not accept deps from requests"
                )
            unknown = sorted(set(sent) - set(self.allowed_deps))
            if unknown:
                raise InvalidRequestError(
                    f"System {self.system.name!r} does not accept the deps key(s) "
                    f"{', '.join(unknown)}; allowed: {', '.join(sorted(self.allowed_deps))}"
                )
        accepted = dict(sent or {})
        if self.principal_deps is not None and principal is not None:
            derived = self.principal_deps(principal)
            if inspect.isawaitable(derived):
                derived = await derived
            reserved = sorted(set(accepted) & set(derived))
            if reserved:
                raise InvalidRequestError(
                    f"The deps key(s) {', '.join(reserved)} of system {self.system.name!r} "
                    "are set by the server from the caller's identity"
                )
            accepted.update(derived)
        if self.deps_factory is None:
            return accepted or None
        try:
            deps = self.deps_factory(accepted)
            if inspect.isawaitable(deps):
                deps = await deps
        except InvalidRequestError:
            raise
        except ValueError as exc:
            raise InvalidRequestError(f"Invalid deps: {exc}") from exc
        return deps

    def check_messages(self, *groups: str | Sequence[Any] | None) -> None:
        """Refuse message roles in `input` and `history` clients may not send.

        Raises:
            InvalidRequestError: If such a message is sent and `allow_system_messages` is off.
        """

        if self.allow_system_messages:
            return
        for group in groups:
            if group is None or isinstance(group, str):
                continue
            for message in convert_to_messages(list(group)):
                if _client_message(message):
                    continue
                kind = (
                    "assistant with tool calls" if isinstance(message, AIMessage) else message.type
                )
                raise InvalidRequestError(
                    f"System {self.system.name!r} does not accept "
                    f"{kind} messages from requests; send user and "
                    "assistant messages only"
                )

    def limits_for(self, requested: UsageLimits | None) -> UsageLimits | None:
        """Combine a request's limits with the `usage_limits` ceiling."""

        return merge_limits(requested, self.usage_limits)


SystemsArg: TypeAlias = (
    AgentLike
    | ServedSystem
    | Mapping[str, AgentLike | ServedSystem]
    | Sequence[AgentLike | ServedSystem]
)


def normalise_systems(systems: SystemsArg, *, allow_empty: bool = False) -> dict[str, ServedSystem]:
    """Turn `create_app`'s `systems` argument into `{served name: ServedSystem}`."""

    entries: list[tuple[str, AgentLike | ServedSystem]]
    if isinstance(systems, ServedSystem):
        entries = [(systems.system.name, systems)]
    elif isinstance(systems, AgentLike):
        entries = [(systems.name, systems)]
    elif isinstance(systems, Mapping):
        entries = list(systems.items())
    else:
        entries = [(_name_of(item), item) for item in systems]
    if not entries and not allow_empty:
        raise ValueError("create_app needs at least one system to serve")
    served: dict[str, ServedSystem] = {}
    for name, item in entries:
        check_system_name(name)
        if name in served:
            raise ValueError(f"Two systems are served under the name {name!r}")
        served[name] = item if isinstance(item, ServedSystem) else ServedSystem(_system(item))
    return served


def check_system_name(name: object) -> None:
    """Raise `ValueError` unless systems may be served under `name`."""

    if not isinstance(name, str) or not SYSTEM_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid system name {name!r}: must start with a letter and contain only "
            "letters, digits, '_' or '-' (at most 64 characters)"
        )


def _name_of(item: AgentLike | ServedSystem) -> str:
    return (item.system if isinstance(item, ServedSystem) else _system(item)).name


def _system(item: Any) -> AgentLike:
    if not isinstance(item, AgentLike):
        raise TypeError(f"Expected an AgentLike system or ServedSystem, got {type(item).__name__}")
    return item


def _client_message(message: BaseMessage) -> bool:
    if isinstance(message, HumanMessage):
        return True
    return (
        isinstance(message, AIMessage) and not message.tool_calls and not message.invalid_tool_calls
    )


def lookup(systems: Mapping[str, ServedSystem], name: str) -> ServedSystem:
    """Return the system served as `name`.

    Raises:
        SystemNotFoundError: If no system is served under `name`.
    """

    try:
        return systems[name]
    except KeyError:
        raise SystemNotFoundError(f"No system named {name!r} is served here") from None


def system_kind(system: AgentLike) -> SystemKind:
    """Classify a system for the API's `kind` field."""

    if isinstance(system, Agent):
        return "agent"
    if isinstance(system, Team):
        return "team"
    if isinstance(system, AgentNetwork):
        return "network"
    if isinstance(system, Workflow):
        return "workflow"
    return "custom"


def system_topology(system: AgentLike) -> dict[str, Any] | None:
    """The structure of a team, network or workflow; `None` for other systems."""

    if isinstance(system, Team | AgentNetwork):
        topology: dict[str, Any] = system.topology()
        return topology
    if isinstance(system, Workflow):
        described = _describe_workflow(system)
        return described if isinstance(described, dict) else None
    return None


def system_mermaid(served: ServedSystem) -> str | None:
    """The system's Mermaid diagram: its own, the configured one, or LangGraph's for a workflow."""

    render = getattr(served.system, "to_mermaid", None)
    if callable(render):
        diagram = render()
        if isinstance(diagram, str):
            return diagram
    if served.diagram is not None:
        return served.diagram
    if isinstance(served.system, Workflow):
        return served.system._compiled().get_graph().draw_mermaid()
    return None


def stamp_reviewer(
    decision: Any, key: str | None, pending_ids: Collection[Any], principal: Principal
) -> Any:
    """The decision with the authenticated reviewer written under `key` (each of several too).

    Whatever the client put under `key` or `actor` is replaced or dropped, so the recorded
    reviewer is always the caller. A non-mapping decision or `key=None` is returned unchanged.
    """

    if key is None or not isinstance(decision, Mapping):
        return decision
    reviewer = principal.describe()

    def stamped(value: Mapping[str, Any]) -> dict[str, Any]:
        kept = {name: item for name, item in value.items() if name not in (key, "actor")}
        return {**kept, key: reviewer}

    ids = set(pending_ids)
    if decision and set(decision) <= ids and all(isinstance(v, Mapping) for v in decision.values()):
        return {interrupt: stamped(value) for interrupt, value in decision.items()}
    return stamped(decision)


def system_checkpointer(system: AgentLike) -> BaseCheckpointSaver[Any] | None:
    """The checkpointer an `Agent` or `Team` was built with, if any."""

    for attribute in ("checkpointer", "_checkpointer"):
        candidate = getattr(system, attribute, None)
        if isinstance(candidate, BaseCheckpointSaver):
            return candidate
    return None


def _describe_workflow(node: AgentLike) -> Any:
    if isinstance(node, Sequential):
        return {"sequential": [_describe_workflow(step) for step in node.steps], "name": node.name}
    if isinstance(node, Parallel):
        return {
            "parallel": [_describe_workflow(branch) for branch in node.branches],
            "name": node.name,
        }
    if isinstance(node, Loop):
        return {
            "loop": _describe_workflow(node.body),
            "max_iterations": node.max_iterations,
            "name": node.name,
        }
    if isinstance(node, Map):
        return {
            "map": _describe_workflow(node.member),
            "part_size": node.part_size,
            "name": node.name,
        }
    return node.name
