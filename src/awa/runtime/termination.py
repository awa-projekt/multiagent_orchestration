"""Termination: the ceilings every run tree has, no-progress stops and the stop diagnosis.

Every top-level run gets hard ceilings (`AWA_RUN_MAX_*`) for fields left `None`; there is no
"unlimited". A `TreeGuard` stops a run repeating an identical tool call or failing one tool
repeatedly; a nested stop is scoped to that run. Every forced stop gets a deterministic
`StopDiagnosis` attached to the error and its `ErrorEvent`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from awa._internal.logging import get_logger
from awa.errors import (
    ConfigurationError,
    MaxStepsExceededError,
    NoProgressError,
    RunTimeoutError,
    UsageLimitExceededError,
)

if TYPE_CHECKING:
    from awa.contracts.events import RunEvent
    from awa.contracts.usage import UsageLimits
    from awa.core.settings import Settings
    from awa.runtime.context import RunContext

_logger = get_logger(__name__)

_MAX_STEPS = re.compile(r"max_steps=(\d+)")

CALLER_LIMITS_KEY = "awa.caller_limits"
"""`RunContext.internal` key of the `UsageLimits` the caller gave a top-level run (or `None`)."""

_FIXES: Mapping[str, str] = {
    "max_requests": "if the work really needs more model calls, raise the limit where it was "
    "set (--max-requests, the blueprint's limits, UsageLimits(max_requests=...) or "
    "AWA_RUN_MAX_REQUESTS); otherwise the last steps show the loop to break",
    "max_tool_calls": "if the work really needs more tool calls, pass "
    "UsageLimits(max_tool_calls=...) or set AWA_RUN_MAX_TOOL_CALLS; otherwise the last steps "
    "show the loop to break",
    "max_total_tokens": "shorten what goes into each call (smaller tool results, compaction) or "
    "pass UsageLimits(max_total_tokens=...) / set AWA_RUN_MAX_TOKENS",
    "max_input_tokens": "shorten what goes into each call or raise max_input_tokens",
    "max_output_tokens": "ask for shorter answers or raise max_output_tokens",
    "max_cost": "the run cost more than max_cost: use a cheaper model or raise max_cost",
    "max_duration": "the run took longer than its time ceiling: pass timeout=... or "
    "UsageLimits(max_duration=...) / set AWA_RUN_MAX_SECONDS, or split the work",
    "timeout": "the run took longer than its timeout: raise timeout=... or split the work",
    "max_depth": "agents keep starting nested runs (delegating or calling each other): give the "
    "members clear, final tasks, or set AWA_RUN_MAX_DEPTH higher if the nesting is intended",
    "max_steps": "the agent needed more model calls than max_steps without a final answer: give "
    "it a clearer task or tools that finish the job, or raise max_steps",
    "recursion_limit": "the workflow or team did not finish within its graph steps: check the "
    "routing for a cycle, or raise the iterations or handoffs it may use",
    "identical_tool_calls": "the tool keeps returning the same result for the same arguments: "
    "make the result say what is missing or what to do next, or tell the agent when to stop",
    "repeated_tool_errors": "the tool keeps failing: fix the tool or its inputs (the last error "
    "says why), or tell the agent to give up after a failure",
}

_DELEGATION_FIX = (
    "a delegation used up its own budget: give the member a smaller task or raise "
    "TeamLimits.delegation_limits"
)
_DEFAULT_FIX = (
    "look at the last steps for the repetition, then raise the bound only if the work really "
    "needs more"
)

StopCategory = Literal["budget", "deadline", "no_progress", "steps", "depth"]
"""What kind of bound stopped a run: a usage budget, the wall clock, a repetition without
progress, a step limit (`max_steps`, a graph's recursion limit) or the nesting depth."""


def ceilings() -> Settings:
    """The settings that carry the ceilings (`Settings.run_max_*`, `Settings.max_*`).

    Raises:
        ConfigurationError: If a setting is not a whole number of at least 1 (2 for the repetition
            thresholds); a ceiling cannot be switched off.
    """

    from pydantic import ValidationError

    from awa.core.settings import get_settings

    try:
        return get_settings()
    except ValidationError as exc:
        names = ", ".join(str(error["loc"][0]).upper() for error in exc.errors() if error["loc"])
        raise ConfigurationError(
            f"Invalid setting {names}: a ceiling cannot be switched off; set a larger whole "
            "number to allow more"
        ) from None


def with_ceilings(limits: UsageLimits | None, *, timeout: float | None) -> UsageLimits:
    """`limits` with every `None` ceiling field set; `max_duration` stays unset under a `timeout`."""

    from awa.contracts.usage import UsageLimits

    settings = ceilings()
    current = limits if limits is not None else UsageLimits()
    defaults: dict[str, Any] = {
        "max_requests": settings.run_max_requests,
        "max_tool_calls": settings.run_max_tool_calls,
        "max_total_tokens": settings.run_max_tokens,
        "max_depth": settings.run_max_depth,
    }
    if timeout is None:
        defaults["max_duration"] = float(settings.run_max_seconds)
    update = {name: value for name, value in defaults.items() if getattr(current, name) is None}
    return current.model_copy(update=update) if update else current


@dataclass(frozen=True)
class StopDiagnosis:
    """Why a run was stopped, where, what it did last, and what to change.

    Built by rules without a model call; holds names, numbers and digests, never content.

    Attributes:
        stop_reason: `"stopped:<category>"`, for example `"stopped:no_progress"`.
        bound: The bound that was reached (`max_requests`, `max_steps`, `recursion_limit`,
            `identical_tool_calls`, ...).
        error_type: The class name of the error that ended the run.
        last_steps: The last steps of the run tree, oldest first (`"tool_result search error"`).
        suggested_fix: One concrete change that would avoid the stop.
        tool: The tool involved, for a no-progress stop.
    """

    stop_reason: str
    category: StopCategory
    bound: str
    run_id: str
    root_run_id: str
    agent: str
    depth: int
    error_type: str
    suggested_fix: str
    value: float | None = None
    maximum: float | None = None
    last_steps: tuple[str, ...] = ()
    tool: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe copy."""

        data = asdict(self)
        data["last_steps"] = list(self.last_steps)
        return data

    def summary(self) -> str:
        """One line: the bound, where, and the fix."""

        reached = f"the limit {self.bound}"
        if self.value is not None and self.maximum is not None:
            reached += f" ({_number(self.value)} of {_number(self.maximum)})"
        elif self.maximum is not None:
            reached += f" ({_number(self.maximum)})"
        where = f" in agent {self.agent!r}" if self.agent else ""
        return f"{reached} stopped the run{where}. Fix: {self.suggested_fix}"


@dataclass
class _Tree:
    """The guard of one executing run tree, shared by every run entered in it."""

    guard: TreeGuard
    unsubscribe: Callable[[], None]
    entered: int = 0

    lock: ClassVar[threading.Lock] = threading.Lock()
    by_root: ClassVar[dict[str, _Tree]] = {}


@dataclass
class _RunWatch:
    """Repetition counters of one agent run."""

    identical: dict[tuple[str, str, str], int] = field(default_factory=dict)
    errors_in_a_row: dict[str, int] = field(default_factory=dict)


class TreeGuard:
    """Ceilings, recent steps and no-progress counters of one executing run tree, found by root run id."""

    def __init__(self, ctx: RunContext[Any]) -> None:
        self.settings = ceilings()
        self.root_run_id = ctx.root_run_id
        self.steps: deque[str] = deque(maxlen=12)
        self._arguments: dict[tuple[str, str], str] = {}
        self._runs: dict[str, _RunWatch] = {}
        self._parents: dict[str, str | None] = {}
        self._emitter = ctx.events
        self.tripped: StopDiagnosis | None = None
        self._message = ""

    def observe(self, event: RunEvent) -> None:
        """Record `event` as a step and update the repetition counters (an event subscriber)."""

        from awa.contracts.events import (
            AgentStartedEvent,
            DelegationEvent,
            HandoffEvent,
            LimitExceededEvent,
            ModelResponseEvent,
            StepEvent,
            ToolCallEvent,
            ToolResultEvent,
        )

        if isinstance(event, ToolCallEvent):
            self._arguments[(event.run_id, event.call_id)] = output_digest(event.arguments)
            self.steps.append(f"{event.agent}: call {event.tool_name}")
        elif isinstance(event, ToolResultEvent):
            outcome = "error" if event.is_error else "ok"
            self.steps.append(f"{event.agent}: {event.tool_name} -> {outcome}")
            self._count(event)
        elif isinstance(event, ModelResponseEvent):
            tools = ",".join(event.tool_call_names)
            self.steps.append(f"{event.agent}: model -> {tools or 'answer'}")
        elif isinstance(event, HandoffEvent):
            self.steps.append(f"handoff {event.source} -> {event.target}")
        elif isinstance(event, DelegationEvent):
            self.steps.append(f"{event.agent}: {event.mode} {event.target}")
        elif isinstance(event, StepEvent):
            self.steps.append(f"step {event.step} {event.status}")
        elif isinstance(event, AgentStartedEvent):
            self._parents[event.run_id] = event.parent_run_id
            if event.depth > 0:
                self.steps.append(f"start {event.agent} (depth {event.depth})")
        elif isinstance(event, LimitExceededEvent):
            self.steps.append(f"limit {event.limit}")

    def _count(self, event: Any) -> None:
        if self.tripped is not None:
            return
        watch = self._runs.setdefault(event.run_id, _RunWatch())
        arguments = self._arguments.pop((event.run_id, event.call_id), "")
        key = (event.tool_name, arguments, output_digest(event.content))
        seen = watch.identical.get(key, 0) + 1
        watch.identical[key] = seen
        errors = watch.errors_in_a_row.get(event.tool_name, 0) + 1 if event.is_error else 0
        watch.errors_in_a_row[event.tool_name] = errors
        if seen >= self.settings.max_identical_calls:
            what = (
                f"{event.agent!r} called {event.tool_name!r} {seen} times with the same "
                "arguments and got the same result each time"
            )
            self._trip(event, "identical_tool_calls", seen, self.settings.max_identical_calls, what)
        elif errors >= self.settings.max_tool_errors:
            what = f"{event.tool_name!r} failed {errors} times in a row in {event.agent!r}"
            self._trip(event, "repeated_tool_errors", errors, self.settings.max_tool_errors, what)

    def _trip(self, event: Any, bound: str, value: int, maximum: int, what: str) -> None:
        from awa.contracts.events import LimitExceededEvent

        self.tripped = StopDiagnosis(
            stop_reason="stopped:no_progress",
            category="no_progress",
            bound=bound,
            value=float(value),
            maximum=float(maximum),
            run_id=event.run_id,
            root_run_id=event.root_run_id,
            agent=event.agent,
            depth=event.depth,
            error_type=NoProgressError.__name__,
            last_steps=tuple(self.steps),
            tool=event.tool_name,
            suggested_fix=_fix(bound, tool=event.tool_name),
        )
        self._message = f"No progress: {what}; the run was stopped"
        self._emitter.emit_later(
            LimitExceededEvent(
                limit=bound,
                value=float(value),
                maximum=float(maximum),
                message=self._message,
                run_id=event.run_id,
                root_run_id=event.root_run_id,
                agent=event.agent,
                depth=event.depth,
            )
        )

    def stops(self, run_id: str) -> bool:
        """Whether the tripped stop applies to `run_id`: the whole tree, or a nested run
        that is `run_id` or one of its ancestors."""

        diagnosis = self.tripped
        if diagnosis is None:
            return False
        if diagnosis.depth == 0:
            return True
        current: str | None = run_id
        seen: set[str] = set()
        while current is not None and current not in seen:
            if current == diagnosis.run_id:
                return True
            seen.add(current)
            current = self._parents.get(current)
        return False

    def stop_error(self) -> NoProgressError | None:
        """A fresh error for the tripped stop, with its diagnosis; `None` when not tripped."""

        diagnosis = self.tripped
        if diagnosis is None:
            return None
        error = NoProgressError(
            self._message,
            details={
                "limit": diagnosis.bound,
                "value": diagnosis.value,
                "maximum": diagnosis.maximum,
                "tool": diagnosis.tool,
            },
        )
        _attach(error, diagnosis)
        return error


def enter_tree(ctx: RunContext[Any], *, timeout: float | None) -> Callable[[], None] | None:
    """Give a top-level run (or a run under a hand-built root) its ceilings and a `TreeGuard`.

    Returns the function that leaves the guard when the run ends, or `None`.

    Raises:
        ConfigurationError: If a ceiling setting is invalid.
    """

    if ctx.parent_run_id is not None and tree_guard(ctx) is not None:
        return None
    ctx.internal[CALLER_LIMITS_KEY] = ctx.limits
    ctx.limits = with_ceilings(ctx.limits, timeout=timeout)
    return _join(ctx)


def _join(ctx: RunContext[Any]) -> Callable[[], None]:
    root = ctx.root_run_id
    with _Tree.lock:
        tree = _Tree.by_root.get(root)
        if tree is None:
            guard = TreeGuard(ctx)
            tree = _Tree.by_root[root] = _Tree(guard, ctx.events.subscribe(guard.observe))
        tree.entered += 1

    def leave() -> None:
        with _Tree.lock:
            if _Tree.by_root.get(root) is not tree:
                return
            tree.entered -= 1
            if tree.entered > 0:
                return
            del _Tree.by_root[root]
        tree.unsubscribe()

    return leave


def caller_limits(ctx: RunContext[Any]) -> UsageLimits | None:
    """The limits the caller gave the top-level run of `ctx`, before the ceilings filled them."""

    if CALLER_LIMITS_KEY in ctx.internal:
        return ctx.internal[CALLER_LIMITS_KEY]  # type: ignore[no-any-return]
    return ctx.limits


def tree_guard(ctx: RunContext[Any] | None) -> TreeGuard | None:
    """The guard of the run tree `ctx` belongs to, if its top-level run registered one."""

    if ctx is None:
        return None
    tree = _Tree.by_root.get(ctx.root_run_id)
    return tree.guard if tree is not None else None


def raise_if_stopped(ctx: RunContext[Any] | None) -> None:
    """Raise the tree's no-progress stop, if one tripped (awa calls it before every step).

    Raises:
        NoProgressError: If the run tree was stopped for repeating itself.
    """

    guard = tree_guard(ctx)
    if guard is not None and ctx is not None and guard.stops(ctx.run_id):
        error = guard.stop_error()
        assert error is not None
        raise error


def ends_the_run(ctx: RunContext[Any], error: Exception) -> bool:
    """Whether `error` of a member ends the run of `ctx` instead of failing only that member.

    A limit of the run tree does; a member that stopped without progress does not, and its stop
    is cleared (`release_nested_stop`).
    """

    return isinstance(error, UsageLimitExceededError) and not release_nested_stop(ctx, error)


def release_nested_stop(ctx: RunContext[Any], error: BaseException) -> bool:
    """Clear a no-progress stop that tripped below `ctx`; `True` when `error` is one."""

    guard = tree_guard(ctx)
    if not isinstance(error, NoProgressError) or guard is None or guard.tripped is None:
        return False
    if guard.tripped.depth == 0 or guard.stops(ctx.run_id):
        return False
    guard.tripped = None
    return True


async def diagnose_stop(ctx: RunContext[Any], error: BaseException) -> dict[str, Any] | None:
    """The diagnosis of `error` as a dict, built, attached and logged once; `None` if no bound stopped it."""

    known = getattr(error, "diagnosis", None)
    if isinstance(known, StopDiagnosis):
        if not getattr(error, "_awa_stop_logged", False):
            _log(error, known)
        return known.to_dict()
    diagnosis = _rules(ctx, error)
    if diagnosis is None:
        return None
    _attach(error, diagnosis)
    _log(error, diagnosis)
    return diagnosis.to_dict()


def _rules(ctx: RunContext[Any], error: BaseException) -> StopDiagnosis | None:
    from awa.runtime.context import _limit_fields

    category: StopCategory
    value: float | None = None
    maximum: float | None = None
    if isinstance(error, RunTimeoutError):
        category, bound = "deadline", str(error.details.get("limit_name") or "timeout")
        value = _float(error.details.get("elapsed"))
        maximum = _float(error.details.get("limit"))
    elif isinstance(error, UsageLimitExceededError):
        fields = _limit_fields(error)
        bound = str(fields.get("limit") or "usage_limit")
        value, maximum = fields.get("value"), fields.get("maximum")
        category = "depth" if bound == "max_depth" else "budget"
    elif isinstance(error, MaxStepsExceededError):
        category = "steps"
        bound = str(error.details.get("limit") or "max_steps")
        maximum = _float(error.details.get("maximum"))
        if maximum is None and (found := _MAX_STEPS.search(str(error))) is not None:
            maximum = float(found.group(1))
    else:
        return None
    guard = tree_guard(ctx)
    return StopDiagnosis(
        stop_reason=f"stopped:{category}",
        category=category,
        bound=bound,
        value=value,
        maximum=maximum,
        run_id=ctx.run_id,
        root_run_id=ctx.root_run_id,
        agent=ctx.agent,
        depth=ctx.depth,
        error_type=type(error).__name__,
        last_steps=tuple(guard.steps) if guard is not None else (),
        suggested_fix=_fix(bound),
    )


def _attach(error: BaseException, diagnosis: StopDiagnosis) -> None:
    try:
        error.diagnosis = diagnosis  # type: ignore[attr-defined]
        details = getattr(error, "details", None)
        if isinstance(details, Mapping):
            error.details = {**details, "stop": diagnosis.to_dict()}  # type: ignore[attr-defined]
    except AttributeError:  # an exception with __slots__: the event still carries it
        pass


def _log(error: BaseException, diagnosis: StopDiagnosis) -> None:
    # The error already tells the caller that the run stopped; the diagnosis rides on it, on
    # the trace and in this log line (INFO, so a command line does not print it twice).
    _logger.info("Run %s: %s", diagnosis.run_id, diagnosis.summary())
    with contextlib.suppress(AttributeError):
        error._awa_stop_logged = True  # type: ignore[attr-defined]


def _fix(bound: str, *, tool: str | None = None) -> str:
    text = _FIXES.get(bound) or (
        _DELEGATION_FIX if bound.startswith("delegation") else _DEFAULT_FIX
    )
    return f"{tool}: {text}" if tool else text


def output_digest(value: Any) -> str:
    """A short digest of a value (tool arguments, a result, a draft) for the no-progress checks."""

    try:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


__all__ = [
    "StopCategory",
    "StopDiagnosis",
    "TreeGuard",
    "caller_limits",
    "ceilings",
    "diagnose_stop",
    "enter_tree",
    "output_digest",
    "raise_if_stopped",
    "ends_the_run",
    "release_nested_stop",
    "tree_guard",
    "with_ceilings",
]
