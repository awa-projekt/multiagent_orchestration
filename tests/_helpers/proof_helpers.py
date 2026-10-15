"""Shared helpers of the core proof suite: scripted agents, an event recorder, a run census.

Everything here uses only the core agent system (`Agent`, `Team`, tools, limits, checkpointer) and
scripted models from `awa.testing`; there is no data profile, no connector and no web app.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from langchain_core.messages import ToolMessage

from awa.agents import Agent
from awa.contracts import RunEvent
from awa.testing import FakeChatModel, FunctionChatModel, tool_call

T = TypeVar("T")


def scripted(name: str, *responses: Any, **kwargs: Any) -> Agent:
    """An agent whose model plays back `responses` in order."""

    return Agent(
        name,
        description=f"The {name} agent.",
        model=FakeChatModel(list(responses) or ["ok"]),
        **kwargs,
    )


def model_of(agent: Agent) -> FakeChatModel | FunctionChatModel:
    """The scripted model of an agent built by `scripted` or with a fake model."""

    model = agent._model
    assert isinstance(model, FakeChatModel | FunctionChatModel)
    return model


def delegate(agent: str, task: str = "do it", **extra: Any) -> Any:
    return tool_call("delegate_task", {"agent": agent, "task": task, **extra})


def transfer(target: str, reason: str = "") -> Any:
    return tool_call(f"transfer_to_{target}", {"reason": reason})


def tool_results(result: Any) -> list[str]:
    """The text of every tool message of a result, in order."""

    return [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]


def of_type(events: list[RunEvent], kind: type[T]) -> list[T]:
    return [event for event in events if isinstance(event, kind)]


def only(events: list[RunEvent], *kinds: type[Any]) -> list[RunEvent]:
    """The events of the given classes, in the order they were emitted."""

    return [event for event in events if isinstance(event, kinds)]


async def settle(ticks: int = 20, pause: float = 0.01) -> None:
    """Let any task that should have been stopped show that it is still running."""

    for _ in range(ticks):
        await asyncio.sleep(pause)


def other_tasks() -> list[asyncio.Task[Any]]:
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not current and not t.done()]


class Census:
    """Counts what really happened: model calls per model and executions per tool.

    A run that "ended" must also have stopped doing things: `frozen()` is true when nothing
    was called since the last `mark()`.
    """

    def __init__(self, *agents: Agent, effects: list[Any] | None = None) -> None:
        self.agents = agents
        self.effects = effects if effects is not None else []
        self._mark = self.snapshot()

    def snapshot(self) -> tuple[Any, ...]:
        return (*(model_of(a).call_count for a in self.agents), len(self.effects))

    def mark(self) -> None:
        self._mark = self.snapshot()

    def frozen(self) -> bool:
        return self.snapshot() == self._mark
