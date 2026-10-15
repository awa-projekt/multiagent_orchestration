"""Team member middleware: delivers handbacks and mail, ends after a transfer, caps tool calls.

The last allowed model call (`max_steps`) runs without tools so a long run ends with an answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from awa._internal.upstream.agents import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
    hook_config,
)
from awa._internal.upstream.graph import Command
from awa._internal.upstream.messages import BaseMessage, HumanMessage, ToolMessage
from awa._internal.upstream.tools import BaseTool
from awa.agents import Agent
from awa.agents.middleware import current_step
from awa.contracts.communication import MessageKind
from awa.contracts.events import ToolResultEvent
from awa.errors import MessagingError, TopologyError
from awa.messaging.bus import MessageBus
from awa.runtime.context import RunContext, current_run_context
from awa.team._delegation import DELEGATION_TOOLS, Delegator
from awa.team._handoffs import team_note
from awa.team._run import TRANSFER_IGNORED, TeamRun, pending_calls

_REQUESTS: tuple[MessageKind, ...] = ("request",)

AUTO_DELIVERED_KINDS: tuple[MessageKind, ...] = (
    "notification",
    "response",
    "broadcast",
    "publication",
)
"""Message kinds delivered automatically; requests wait for `read_inbox`."""


def delivery_note(blocks: Sequence[str], pending_requests: int = 0) -> HumanMessage:
    """The context message carrying delivered results and messages."""

    parts = ["New information from your team:", *blocks]
    if pending_requests:
        parts.append(
            f"{pending_requests} request(s) from other agents are waiting for your answer: "
            "read them with read_inbox and answer with reply_to_message."
        )
    return team_note("delivery", "\n\n".join(parts))


class TeamMiddleware(AgentMiddleware):
    """Delivers handbacks and mailbox messages, and ends the loop after a transfer.

    Where the order of concurrent tool calls matters it follows tool-call order: delegation calls are
    admitted in order, and only the first transfer of a turn executes.

    Args:
        key: `RunContext.internal` key of the team's `TeamRun`.
        bus: The bus holding the agent's mailbox, or `None` without messaging.
        transfer_tools: Names of the agent's transfer tools.
        requests: Whether the agent answers requests itself (`read_inbox`, `reply_to_message`);
            otherwise every message is delivered.
        max_steps: The agent's `max_steps`; its last model call runs without tools (`None`: no
            final turn).
    """

    def __init__(
        self,
        *,
        key: str,
        agent: str,
        bus: MessageBus | None,
        delegator: Delegator | None = None,
        transfer_tools: Sequence[str] = (),
        requests: bool = True,
        max_steps: int | None = None,
    ) -> None:
        super().__init__()
        self._key = key
        self._agent = agent
        self._bus = bus
        self._delegator = delegator
        self._transfer_tools = frozenset(transfer_tools)
        self._auto: tuple[MessageKind, ...] | None = AUTO_DELIVERED_KINDS if requests else None
        self._max_steps = max_steps

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """End the loop after a transfer; otherwise deliver what arrived as one message."""

        ctx, run = self._context()
        if ctx is None:
            return None
        if run is not None and self._delegator is not None:
            self._delegator.release_unused(run, ctx.run_id)
        if run is not None and ctx.run_id in run.transfers:
            return {"jump_to": "end"}
        blocks = [task.render() for task in run.take_handbacks(ctx.run_id)] if run else []
        pending = 0
        if self._bus is not None:
            try:
                messages = await self._bus.receive(self._agent, kinds=self._auto)
                if self._auto is not None:
                    pending = len(await self._bus.peek(self._agent, kinds=_REQUESTS))
            except MessagingError:
                messages = []
            blocks.extend(message.render_for_model() for message in messages)
        if not blocks and not pending:
            return None
        return {"messages": [delivery_note(blocks, pending)]}

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> Any:
        """Make the last allowed model call of a member a final answer without tools."""

        ctx, run = self._context()
        if self._max_steps is None or ctx is None or not request.tools:
            return await handler(request)
        # The agent's own step counter (structured-output repairs included) is the one
        # source of truth; it survives a pause, so a resumed turn keeps its final step.
        step = current_step()
        if step is None or not step.is_last:
            return await handler(request)
        note = team_note(
            "limit",
            f"You reached your step limit (max_steps={self._max_steps}); tools are no longer "
            "available. Write your final answer now from what you have, and say briefly what "
            "is still open.",
        )
        if run is not None:
            run.step_stops.add(ctx.run_id)
        ctx.report_limit_exceeded(
            limit="max_steps",
            value=self._max_steps,
            maximum=self._max_steps,
            message=f"{self._agent} reached max_steps={self._max_steps}; final answer without tools",
        )
        return await handler(
            request.override(tools=[], tool_choice=None, messages=[*request.messages, note])
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Run delegation and transfer calls with decisions made in tool-call order."""

        call = request.tool_call
        call_id = call["id"]
        if not call_id:
            return await handler(request)
        if call["name"] in self._transfer_tools:
            transfers = [
                c["id"]
                for c in pending_calls(call_id, _messages(request.state))
                if c["name"] in self._transfer_tools
            ]
            token = TRANSFER_IGNORED.set(bool(transfers) and transfers[0] != call_id)
            try:
                return await handler(request)
            finally:
                TRANSFER_IGNORED.reset(token)
        if self._delegator is None or call["name"] not in DELEGATION_TOOLS:
            return await handler(request)
        ctx, run = self._context()
        if ctx is None or run is None:
            return await handler(request)
        admission = self._delegator.admit_turn(
            ctx, run, self._agent, call_id, _messages(request.state)
        )
        if admission is None:
            return await handler(request)
        with self._delegator.granted(run, admission):
            return await handler(request)

    def _context(self) -> tuple[RunContext[Any] | None, TeamRun | None]:
        ctx = current_run_context()
        run = ctx.internal.get(self._key) if ctx is not None else None
        return ctx, run if isinstance(run, TeamRun) else None


def _messages(state: Any) -> list[BaseMessage]:
    messages = state.get("messages") if isinstance(state, dict) else None
    return list(messages) if isinstance(messages, list) else []


def wire_agent(
    agent: Agent[Any, Any],
    tools: Sequence[BaseTool],
    instructions: str,
    middleware: TeamMiddleware | None,
    *,
    owner: str,
    kind: str,
    extra: Sequence[AgentMiddleware] = (),
) -> Agent[Any, Any]:
    """Derive the runtime variant of `agent` with the tools, instructions and middleware of `owner`.

    Raises:
        TopologyError: If `agent` already is the runtime agent of a team or network, or a tool
            clashes with one of its own.
    """

    if any(isinstance(m, TeamMiddleware) for m in agent._middleware):
        raise TopologyError(
            f"{owner}: {agent.name!r} is the runtime agent of another team or network "
            "(it carries that team's tools and middleware). Pass the original agent instead."
        )
    try:
        return agent.with_tools(
            *tools,
            extra_instructions=instructions,
            middleware=[*extra, *([middleware] if middleware is not None else [])],
        )
    except ValueError as exc:
        raise TopologyError(f"{owner}: cannot add {kind} tools to {agent.name!r}: {exc}") from exc


_COUNTS_KEY = "awa.team.tool_limit_counts"
"""`RunContext.internal` key of the call counts, by `(run id, tool name)`."""


class ToolLimitMiddleware(AgentMiddleware):
    """Refuses a member's tool calls beyond its per-run limits with an error result.

    LangChain's `ToolCallLimitMiddleware` keeps its counts in the agent's graph state and emits
    nothing, whereas this one counts per awa run (so per delegation), reports the breach to the
    run context and emits the refusal as a `ToolResultEvent`.
    """

    def __init__(self, *, agent: str, limits: Mapping[str, int]) -> None:
        super().__init__()
        self._agent = agent
        self._limits = dict(limits)

    @property
    def name(self) -> str:
        """Unique per member, so several members' middleware never clash."""

        return f"awa_tool_limits_{self._agent}"

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Run the call, or refuse it when it is over the member's limit for the tool."""

        call = request.tool_call
        name = call["name"]
        cap = self._limits.get(name)
        ctx = current_run_context()
        if cap is None or ctx is None:
            return await handler(request)
        counts: dict[tuple[str, str], int] = ctx.internal.setdefault(_COUNTS_KEY, {})
        key = (ctx.run_id, name)
        count = counts[key] = counts.get(key, 0) + 1
        if count <= cap:
            return await handler(request)
        text = (
            f"Not run: {name!r} reached its limit of {cap} calls for {self._agent!r} in this "
            "task (tool_limits). Continue with what you have."
        )
        ctx.report_limit_exceeded(
            limit="tool_limits", value=count, maximum=cap, message=f"{self._agent}.{name}: {text}"
        )
        call_id = call.get("id") or ""
        fields = {"tool_name": name, "call_id": call_id, **ctx.event_fields()}
        await ctx.emit(ToolResultEvent(content=text, is_error=True, **fields))
        return ToolMessage(content=text, tool_call_id=call_id, name=name, status="error")
