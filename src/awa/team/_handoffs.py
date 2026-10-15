"""Handoffs: transferring control of the conversation to another agent.

Assistant messages carry the producing agent in `AIMessage.name`; the team's own bookkeeping
messages are `HumanMessage`s marked with `TEAM_NOTE_KEY` in `additional_kwargs`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from awa._internal.upstream.messages import AIMessage, BaseMessage, HumanMessage
from awa._internal.upstream.tools import BaseTool, StructuredTool
from awa.errors import HandoffError
from awa.runtime.context import RunContext
from awa.team._run import TRANSFER_IGNORED, TeamRun, Transfer, lookup_run, neutralize

TEAM_NOTE_KEY = "awa_team_note"
"""`additional_kwargs` key marking a message the team added for its own bookkeeping."""


@dataclass(frozen=True)
class HandoffInput:
    """What a custom `handoff_history` function receives.

    Attributes:
        messages: The team transcript so far, attributed via `AIMessage.name`.
        source: The agent handing over control, or `None` when `target` starts a new turn.
    """

    messages: tuple[BaseMessage, ...]
    source: str | None
    target: str
    reason: str = ""


HandoffHistory: TypeAlias = (
    Literal["filtered", "full"] | Callable[[HandoffInput], Sequence[BaseMessage]]
)
"""What an agent sees of the conversation when it takes over.

- `"filtered"`: user messages, the agent's own earlier replies, and other
  agents' replies as attributed context (`[researcher]: ...`); tool calls,
  tool results and team notes are dropped.
- `"full"`: the raw transcript, including other agents' tool calls.
- a function of `HandoffInput` returning the messages.
"""

HISTORY_MODES = ("filtered", "full")


def team_note(kind: str, content: str) -> HumanMessage:
    """Create a bookkeeping message the team adds to an agent's input."""

    return HumanMessage(content=content, additional_kwargs={TEAM_NOTE_KEY: kind})


def is_team_note(message: BaseMessage) -> bool:
    """Whether `message` was added by the team for its own bookkeeping."""

    return bool((message.additional_kwargs or {}).get(TEAM_NOTE_KEY))


def handoff_note(source: str, target: str, reason: str) -> HumanMessage:
    """The message that tells `target` it received control from `source`."""

    text = f"{source} transferred the conversation to you ({target})."
    if reason.strip():
        text += f" Their reason: {neutralize(reason.strip())}"
    return team_note("handoff", f"{text}\nContinue from here and respond to the user directly.")


def attribute(messages: Sequence[BaseMessage], agent: str) -> list[BaseMessage]:
    """Return `messages` with every unattributed assistant message named after `agent`."""

    return [
        message.model_copy(update={"name": agent})
        if isinstance(message, AIMessage) and not message.name
        else message
        for message in messages
    ]


def build_history(mode: HandoffHistory, handoff: HandoffInput) -> list[BaseMessage]:
    """Select what `handoff.target` sees of the conversation; see `HandoffHistory`."""

    if callable(mode):
        return list(mode(handoff))
    if mode == "full":
        return list(handoff.messages)
    return _filtered(handoff.messages, handoff.target)


def _filtered(messages: Sequence[BaseMessage], viewer: str) -> list[BaseMessage]:
    selected: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            if not is_team_note(message):
                selected.append(message)
        elif isinstance(message, AIMessage) and (text := str(message.text).strip()):
            if message.name == viewer:
                selected.append(AIMessage(content=text, name=viewer))
            else:
                author = message.name or "assistant"
                selected.append(team_note("context", f"[{author}]: {text}"))
    return selected


class _TransferArgs(BaseModel):
    reason: str = Field(
        default="", description="Why the other agent should take over, in one sentence."
    )


def transfer_tool(*, key: str, team: str, source: str, target: str, description: str) -> BaseTool:
    """Create `transfer_to_<target>` for `source`; a rejected transfer is explained in the tool result."""

    name = f"transfer_to_{target}"

    async def transfer(reason: str = "") -> str:
        ctx, run = lookup_run(key, team, name)
        return _request_transfer(ctx, run, source, target, reason)

    about = f" {description}" if description else ""
    return StructuredTool.from_function(
        coroutine=transfer,
        name=name,
        description=(
            f"Transfer the conversation to {target}, who then answers the user; you will not "
            f"regain control.{about}"
        ),
        args_schema=_TransferArgs,
    )


def _request_transfer(
    ctx: RunContext[Any], run: TeamRun, source: str, target: str, reason: str
) -> str:
    """Record a transfer requested by the run `ctx`, or explain why it is rejected; a transfer
    past the handoff limit raises `HandoffError`."""

    if ctx.parent_run_id != run.run_id:
        return "Transfer rejected: you are working on a delegated task. Return your result instead."
    if ctx.run_id in run.transfers or TRANSFER_IGNORED.get():
        return "Ignored: only one transfer per turn."
    if run.last_handoff == (target, source):
        return (
            f"Transfer rejected: {target} just transferred this conversation to you. Handle "
            "it yourself or transfer to another agent."
        )
    limit = run.limits.max_handoffs
    if run.handoffs >= limit:
        if ctx.run_id in run.limit_warned:
            raise HandoffError(
                f"Team {run.team!r}: {source!r} tried to transfer to {target!r} again after "
                f"the handoff limit (max_handoffs={limit}) was reached."
            )
        run.limit_warned.add(ctx.run_id)
        message = (
            f"Transfer rejected: this run reached its handoff limit (max_handoffs={limit}). "
            "Answer the user yourself."
        )
        ctx.report_limit_exceeded(
            limit="max_handoffs", value=run.handoffs + 1, maximum=limit, message=message
        )
        return message
    run.transfers[ctx.run_id] = Transfer(target=target, reason=reason)
    return f"Transferred to {target}."
