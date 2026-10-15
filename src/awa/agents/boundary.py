"""Guards at the agent boundary: decisions around one agent calling another, inherited below it.

A hook (`AgentCallGuard.on_agent_call`) may `Deny`, `Rewrite` the brief, `AddContext` or
`Allow` a call; raising or timing out counts as `Deny`. The tool policies and hooks of every
agent on the call chain apply to every run below it (deny wins).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from awa._internal.logging import get_logger
from awa._internal.upstream.agents import AgentMiddleware, ToolCallRequest
from awa._internal.upstream.graph import Command
from awa._internal.upstream.messages import ToolMessage
from awa._internal.upstream.tools import BaseTool, StructuredTool
from awa.errors import AgentRunError, CallCycleError
from awa.runtime.context import RunContext, current_run_context, nested_call

if TYPE_CHECKING:
    from awa.contracts.protocols import AgentLike
    from awa.contracts.results import RunResult

_logger = get_logger("agents.boundary")

GUARDS_KEY = "awa.guards"
"""`RunContext.internal` key of the guard layers of the run and every run above it."""

INHERITED_KEY = "awa.guards.inherited"
"""`RunContext.internal` key of the guard layers a run inherited from the runs above it."""

CallKind = Literal["delegate", "consult", "tool", "handoff", "message"]
"""How one agent calls another: delegation, consultation, agent as a tool, handoff, message."""


@dataclass(frozen=True, slots=True)
class AgentCall:
    """One agent about to call another.

    Attributes:
        brief: What the target will receive.
        scope: The team or network the call happens in, `""` outside one.
        chain: The call chain above the caller, for display (`RunIds.branch`).
    """

    kind: CallKind
    caller: str
    target: str
    brief: str
    scope: str = ""
    chain: str = ""


@dataclass(frozen=True, slots=True)
class Allow:
    """Let the call run unchanged."""


@dataclass(frozen=True, slots=True)
class Deny:
    """Refuse the call; `reason` is what the calling model reads."""

    reason: str


@dataclass(frozen=True, slots=True)
class Rewrite:
    """Run the call with `brief` instead of the original brief."""

    brief: str


@dataclass(frozen=True, slots=True)
class AddContext:
    """Run the call with `text` appended to the brief as a context section."""

    text: str


@dataclass(frozen=True, slots=True)
class Keep:
    """Hand the result to the caller unchanged."""


@dataclass(frozen=True, slots=True)
class Replace:
    """Hand `result` to the caller instead of the target's result."""

    result: str


CallDecision: TypeAlias = Allow | Deny | Rewrite | AddContext | None
ResultDecision: TypeAlias = Keep | Replace | None


class AgentCallGuard(AgentMiddleware):
    """Base class of boundary hooks; override `on_agent_call` and/or `after_agent_call`.

    Added to an agent's or team's `middleware`, it applies to every call that agent, and every agent
    below it, makes to another agent. `Deny` beats everything; `Rewrite`s apply in stack order and
    the brief stays framed; `after_agent_call` may `Replace` the result.

    Args:
        timeout: Seconds a hook may take; longer counts as `Deny` (fail closed).
    """

    def __init__(self, *, timeout: float = 5.0) -> None:
        super().__init__()
        self.timeout = timeout

    async def on_agent_call(self, call: AgentCall) -> CallDecision:
        """Decide on a call before it starts (default: allow)."""

        return Allow()

    async def after_agent_call(self, call: AgentCall, result: str) -> ResultDecision:
        """Decide on the result before the caller receives it (default: keep)."""

        return Keep()


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """The combined decision of every boundary hook on one call."""

    denied: str | None
    brief: str


def guards(ctx: RunContext[Any] | None) -> tuple[Any, ...]:
    """The guard layers of `ctx` and of every run above it, outermost first."""

    if ctx is None:
        return ()
    value = ctx.internal.get(GUARDS_KEY, ())
    return tuple(value) if isinstance(value, tuple) else ()


def enter_guards(ctx: RunContext[Any], own: Sequence[Any]) -> None:
    """Record, at the start of a run, the guards it inherits and adds (see the module)."""

    inherited = guards(ctx) if ctx.parent_run_id is not None else ()
    ctx.internal[INHERITED_KEY] = inherited
    ctx.internal[GUARDS_KEY] = (*inherited, *(g for g in own if g not in inherited))


async def decide_call(ctx: RunContext[Any] | None, call: AgentCall) -> CallOutcome:
    """Ask every boundary hook on the chain about `call`; deny wins, rewrites chain."""

    brief = call.brief
    extra: list[str] = []
    for guard in guards(ctx):
        hook = getattr(guard, "on_agent_call", None)
        if not callable(hook):
            continue
        current = AgentCall(call.kind, call.caller, call.target, brief, call.scope, call.chain)
        decision = await _ask(guard, hook, current)
        if isinstance(decision, Deny):
            _report(ctx, call, decision.reason)
            return CallOutcome(decision.reason, brief)
        if isinstance(decision, Rewrite):
            brief = decision.brief
        elif isinstance(decision, AddContext) and decision.text.strip():
            extra.append(decision.text.strip())
    if extra:
        brief = "\n\n".join([brief, "Context:", *extra])
    return CallOutcome(None, brief)


async def decide_result(ctx: RunContext[Any] | None, call: AgentCall, result: str) -> str:
    """Let every `after_agent_call` hook keep or replace `result`; a failing hook withholds it."""

    for guard in guards(ctx):
        hook = getattr(guard, "after_agent_call", None)
        if not callable(hook):
            continue
        name = type(guard).__name__
        try:
            decision = await _with_timeout(guard, hook(call, result))
        except TimeoutError:
            _report(ctx, call, f"result check {name} timed out")
            return f"Result withheld: the boundary check {name} timed out."
        except Exception as exc:  # noqa: BLE001 - a failing hook withholds (fail closed)
            _logger.exception("after_agent_call of %s failed", name)
            _report(ctx, call, f"result check {name} failed")
            return f"Result withheld: the boundary check {name} failed ({type(exc).__name__})."
        if isinstance(decision, Replace):
            result = decision.result
        elif decision is not None and not isinstance(decision, Keep):
            _logger.error("after_agent_call of %s returned %r, not a decision", name, decision)
            _report(ctx, call, f"result check {name} returned no decision")
            return f"Result withheld: the boundary check {name} returned no decision."
    return result


class _Task(BaseModel):
    task: str = Field(
        description="The complete, self-contained task, including all context it needs."
    )


def as_tool_for(
    runnable: AgentLike,
    output_text: Callable[[RunResult[Any]], str],
    *,
    noun: str,
    name: str | None = None,
    description: str | None = None,
) -> BaseTool:
    """`runnable` as a tool that runs it nested on a `task`, behind the boundary hooks.

    A denied or repeating call returns the reason instead of `output_text`.

    Raises:
        AgentRunError: If the run paused on an interrupt on a thread of its own.
    """

    async def run_nested(task: str) -> str:
        ctx = current_run_context()
        call = AgentCall("tool", ctx.agent if ctx is not None else "", runnable.name, task)
        decided = await decide_call(ctx, call)
        if decided.denied is not None:
            return decided.denied
        try:
            with nested_call(runnable.name, decided.brief):
                result = await runnable.run(decided.brief)
        except CallCycleError as exc:
            return str(exc)
        if result.is_interrupted:
            raise AgentRunError(
                f"{noun.capitalize()} {runnable.name!r} paused on an interrupt while running as "
                f"a tool, on its own thread {result.thread_id!r}. Give the top-level agent a "
                "checkpointer so the interrupt pauses the whole run."
            )
        return await decide_result(ctx, call, output_text(result))

    return StructuredTool.from_function(
        coroutine=run_nested,
        name=name or runnable.name,
        description=description or runnable.card.description or f"Run the {runnable.name} {noun}.",
        args_schema=_Task,
    )


async def _ask(guard: Any, hook: Callable[..., Any], call: AgentCall) -> CallDecision:
    try:
        decision = await _with_timeout(guard, hook(call))
    except TimeoutError:
        return Deny(f"Not started: the boundary check {type(guard).__name__} timed out.")
    except Exception as exc:  # noqa: BLE001 - a failing guard denies (fail closed)
        _logger.exception("on_agent_call of %s failed", type(guard).__name__)
        return Deny(
            f"Not started: the boundary check {type(guard).__name__} failed ({type(exc).__name__})."
        )
    if decision is None or isinstance(decision, Allow | Deny | Rewrite | AddContext):
        return decision
    _logger.error("on_agent_call of %s returned %r, not a decision", type(guard).__name__, decision)
    return Deny(f"Not started: the boundary check {type(guard).__name__} returned no decision.")


async def _with_timeout(guard: Any, outcome: Any) -> Any:
    if not inspect.isawaitable(outcome):
        return outcome
    timeout = getattr(guard, "timeout", None)
    async with asyncio.timeout(timeout if isinstance(timeout, int | float) else None):
        return await outcome


def _report(ctx: RunContext[Any] | None, call: AgentCall, reason: str) -> None:
    if ctx is not None:
        ctx.report_limit_exceeded(
            limit="boundary_denied", message=f"{call.kind} {call.target}: {reason}"
        )


class InheritedGuardMiddleware(AgentMiddleware):
    """Applies the tool policies a run inherited from the runs above it, before the agent's own."""

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        ctx = current_run_context()
        inherited = ctx.internal.get(INHERITED_KEY, ()) if ctx is not None else ()
        chained = handler
        for guard in reversed(tuple(inherited)):
            wrap = getattr(guard, "awrap_tool_call", None)
            if callable(wrap) and not isinstance(guard, AgentCallGuard):
                chained = _bind(wrap, chained)
        return await chained(request)


def _bind(
    wrap: Callable[..., Awaitable[Any]], inner: Callable[[ToolCallRequest], Awaitable[Any]]
) -> Callable[[ToolCallRequest], Awaitable[Any]]:
    async def call(request: ToolCallRequest) -> Any:
        return await wrap(request, inner)

    return call


__all__ = [
    "GUARDS_KEY",
    "INHERITED_KEY",
    "AddContext",
    "AgentCall",
    "AgentCallGuard",
    "Allow",
    "CallDecision",
    "CallKind",
    "CallOutcome",
    "Deny",
    "InheritedGuardMiddleware",
    "Keep",
    "Replace",
    "ResultDecision",
    "Rewrite",
    "decide_call",
    "decide_result",
    "enter_guards",
    "guards",
]
