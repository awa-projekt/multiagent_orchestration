"""Run-time tool policies: decide per call whether a tool call runs, asks, changes or stops.

A `ToolPolicy` is `policy(call, ctx)` returning `Allow()` (or `None`), `Deny(reason)` (an error
result and a `GuardrailBlockedEvent`), `Ask(reason)` (a pause for approval) or `Rewrite(args)`
(validated against the tool's schema). Policies may be sync or async.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_core import to_jsonable_python

from awa._internal.upstream.agents import AgentMiddleware, ToolCallRequest
from awa._internal.upstream.graph import Command
from awa._internal.upstream.messages import ToolCall, ToolMessage
from awa._internal.upstream.tools import BaseTool, StructuredTool
from awa.contracts.events import GuardrailBlockedEvent
from awa.kernel.redaction import SECRETS_REDACTOR
from awa.runtime.context import RunContext, current_run_context
from awa.tools.approval import (
    APPROVAL_GATE_METADATA_KEY,
    create_tool_approval_wrapper,
    default_approval_payload,
    validate_args,
)
from awa.tools.decorator import ToolSpec, tool_spec
from awa.tools.importing import APPROVAL_METADATA_KEY
from awa.tools.risk import RiskName, ToolRisk, infer_tool_risk

_REASON_CHARS = 500


@dataclass(frozen=True)
class ToolCallInfo:
    """What a `ToolPolicy` knows about a tool call.

    Attributes:
        args: The arguments the model supplied (after earlier rewrites).
        tool: The tool, or `None` when the agent has no tool of that name.
        risk: The tool's resolved risk flags (annotation, else name heuristics).
        spec: The tool's `ToolSpec` when it was built with `awa.tool`.
    """

    name: str
    args: dict[str, Any]
    call_id: str = ""
    tool: BaseTool | None = field(default=None, repr=False, compare=False)
    risk: ToolRisk = field(default_factory=ToolRisk)
    spec: ToolSpec | None = None

    @classmethod
    def from_call(cls, call: ToolCall, tool: BaseTool | None) -> ToolCallInfo:
        """Describe `call`, made to `tool`."""

        return cls(
            name=call["name"],
            args=dict(call.get("args") or {}),
            call_id=call.get("id") or "",
            tool=tool,
            risk=infer_tool_risk(tool if tool is not None else call["name"]),
            spec=tool_spec(tool) if tool is not None else None,
        )


@dataclass(frozen=True)
class Allow:
    """Let the call run unchanged."""


@dataclass(frozen=True)
class Deny:
    """Refuse the call; the model sees `reason` as an error result."""

    reason: str


@dataclass(frozen=True)
class Ask:
    """Pause the call for a human decision; the reviewer sees `reason` and `payload`."""

    reason: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rewrite:
    """Run the call with the complete new `args` (validated against the tool's schema)."""

    args: Mapping[str, Any]


PolicyDecision = Allow | Deny | Ask | Rewrite
"""What a `ToolPolicy` returns."""

ToolPolicy = Callable[
    [ToolCallInfo, RunContext[Any] | None], PolicyDecision | None | Awaitable[PolicyDecision | None]
]
"""`policy(call, ctx)`, sync or async; `None` counts as `Allow()`."""


def deny_tools(
    names: Iterable[str], *, reason: str = "this tool is not allowed here"
) -> ToolPolicy:
    """A policy that denies every call to the named tools."""

    blocked = frozenset(names)

    def policy(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        return Deny(reason) if call.name in blocked else Allow()

    return policy


def ask_for_risk(*flags: RiskName, reason: str | None = None) -> ToolPolicy:
    """A policy that asks a human before tools with any of the given risk flags run.

    `flags` default to `"external_action"`. A tool without declared risk is judged by name
    heuristics only, so declare the risk of acting tools; tools with their own approval are not
    asked again.
    """

    wanted: tuple[RiskName, ...] = flags or ("external_action",)

    def policy(call: ToolCallInfo, ctx: RunContext[Any] | None) -> PolicyDecision:
        if call.tool is not None and asks_for_approval(call.tool):
            return Allow()
        hits = [flag for flag in wanted if getattr(call.risk, flag) is True]
        if not hits:
            return Allow()
        return Ask(reason or f"the tool is marked {', '.join(hits)}")

    return policy


def asks_for_approval(tool: BaseTool) -> bool:
    """Whether `tool` pauses for a human approval itself (an `awa.tool` spec, a gate or a toolkit
    flag)."""

    spec = tool_spec(tool)
    if spec is not None and spec.approval != "never":
        return True
    metadata = tool.metadata or {}
    return bool(metadata.get(APPROVAL_GATE_METADATA_KEY) or metadata.get(APPROVAL_METADATA_KEY))


@dataclass(frozen=True)
class _Verdict:
    decision: PolicyDecision
    args: dict[str, Any]
    policy: str


class ToolPolicyMiddleware(AgentMiddleware):
    """Applies `ToolPolicy` functions to every tool call of an agent.

    Policies run in order: a `Rewrite` changes what later ones see, the first `Deny` wins, and any
    `Ask` (unless a later policy denies) pauses the call, which needs a checkpointer. An `Ask` for a
    call to a tool the agent does not have is refused with an error result, never run unapproved.

    Args:
        *policies: The policies, applied in order.
        approval_options: More options for the approval of `Ask` decisions, such as `expires_after`.

    Example:
        ```python
        def payments_policy(call: ToolCallInfo, ctx: RunContext[Shop] | None) -> PolicyDecision:
            if call.name == "issue_refund" and call.args["amount"] > 500:
                return Ask("refunds above 500 EUR need a second pair of eyes")
            if call.risk.external_action and ctx and ctx.tenant == "trial":
                return Deny("trial tenants cannot change external systems")
            return Allow()

        agent = Agent("desk", tools=tools, middleware=[ToolPolicyMiddleware(payments_policy)])
        ```
    """

    def __init__(
        self, *policies: ToolPolicy, approval_options: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__()
        if not policies:
            raise ValueError("ToolPolicyMiddleware needs at least one policy")
        self._policies = policies
        self._approval_options = dict(approval_options or {})

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Decide on the call, then run, pause, rewrite or refuse it."""

        ctx = current_run_context()
        info = ToolCallInfo.from_call(request.tool_call, request.tool)
        verdict = await self._adecide(info, ctx)
        if isinstance(verdict.decision, Deny):
            return await _refuse(request, verdict.decision, verdict.policy, ctx)
        if isinstance(verdict.decision, Ask) and request.tool is None:
            name = request.tool_call["name"]
            return ToolMessage(
                content=(
                    f"Error: the call to {name!r} needs approval ({verdict.decision.reason}), "
                    "but the agent has no tool of that name to approve"
                ),
                tool_call_id=request.tool_call.get("id") or "",
                name=name,
                status="error",
            )
        if isinstance(verdict.decision, Ask) and request.tool is not None:
            if validate_args(request.tool, verdict.args) is not None:
                # Invalid arguments never run: the tool node answers with its own error.
                return await handler(self._with_args(request, verdict.args))
            return await self._ask(request, request.tool, handler, verdict.decision, verdict.args)
        return await handler(self._with_args(request, verdict.args))

    async def _ask(
        self,
        request: ToolCallRequest,
        tool: BaseTool,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        ask: Ask,
        args: dict[str, Any],
    ) -> ToolMessage | Command[Any]:
        ran: list[ToolMessage | Command[Any]] = []

        async def run_approved(**kwargs: Any) -> str:
            # The tool node's own message (an error status, an artifact) is the result as it is.
            ran.append(await handler(self._with_args(request, _plain(kwargs))))
            return ""

        def payload(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {
                **default_approval_payload(tool_name, args),
                **dict(ask.payload),
                "reason": f"tool_policy: {ask.reason}",
            }

        proxy = StructuredTool.from_function(
            coroutine=run_approved,
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
        )
        gate = create_tool_approval_wrapper(
            proxy, build_interrupt_payload=payload, **self._approval_options
        )
        outcome = await gate.ainvoke(args)
        if ran:
            return ran[0]
        call_id, name = request.tool_call.get("id") or "", request.tool_call["name"]
        return ToolMessage(content=str(outcome), tool_call_id=call_id, name=name)

    async def _adecide(self, info: ToolCallInfo, ctx: RunContext[Any] | None) -> _Verdict:
        asks: list[tuple[str, Ask]] = []
        for policy in self._policies:
            outcome = policy(info, ctx)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            name = str(getattr(policy, "__qualname__", None) or type(policy).__name__)
            if isinstance(outcome, Deny):
                return _Verdict(outcome, info.args, name)
            if isinstance(outcome, Ask):
                asks.append((name, outcome))
            elif isinstance(outcome, Rewrite):
                args = dict(outcome.args)
                problem = validate_args(info.tool, args) if info.tool is not None else None
                if problem is not None:
                    denial = Deny(f"a policy rewrote the arguments into invalid ones: {problem}")
                    return _Verdict(denial, info.args, name)
                info = replace(info, args=args)
            elif outcome is not None and not isinstance(outcome, Allow):
                raise TypeError(
                    f"Tool policy {name} returned {type(outcome).__name__}; "
                    "return Allow(), Deny(reason), Ask(reason), Rewrite(args) or None"
                )
        if not asks:
            return _Verdict(Allow(), info.args, "")
        payload: dict[str, Any] = {}
        for _, ask in asks:
            payload.update(ask.payload)
        reason = "; ".join(ask.reason for _, ask in asks)
        return _Verdict(Ask(reason, payload), info.args, ", ".join(name for name, _ in asks))

    @staticmethod
    def _with_args(request: ToolCallRequest, args: dict[str, Any]) -> ToolCallRequest:
        if args == request.tool_call.get("args"):
            return request
        return request.override(tool_call={**request.tool_call, "args": args})


async def _refuse(
    request: ToolCallRequest, deny: Deny, policy: str, ctx: RunContext[Any] | None
) -> ToolMessage:
    call = request.tool_call
    name = call["name"]
    if ctx is not None:
        await ctx.emit(
            GuardrailBlockedEvent(
                guardrail=f"tool_policy:{policy}",
                stage="tool",
                reason=SECRETS_REDACTOR.redact(deny.reason)[:_REASON_CHARS],
                tool=name,
                **ctx.event_fields(),
            )
        )
    return ToolMessage(
        content=f"Error: the call to {name!r} was denied by policy: {deny.reason}",
        tool_call_id=call.get("id") or "",
        name=name,
        status="error",
    )


def _plain(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Validated arguments back as plain JSON values, for the tool node to validate again."""

    value = to_jsonable_python(dict(kwargs), fallback=str)
    return value if isinstance(value, dict) else dict(kwargs)


__all__ = [
    "Allow",
    "Ask",
    "Deny",
    "PolicyDecision",
    "Rewrite",
    "ToolCallInfo",
    "ToolPolicy",
    "ToolPolicyMiddleware",
    "ask_for_risk",
    "asks_for_approval",
    "deny_tools",
]
