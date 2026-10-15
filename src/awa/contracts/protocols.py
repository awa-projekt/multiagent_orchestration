"""Structural interfaces shared across packages."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, TypeAlias, runtime_checkable

from awa._internal.upstream.messages import BaseMessage
from awa.contracts.cards import AgentCard
from awa.contracts.results import RunResult
from awa.contracts.usage import UsageLimits
from awa.kernel.redaction import Redactor as Redactor

AgentInput: TypeAlias = str | Sequence[BaseMessage | dict[str, Any]]
"""What a run accepts: a plain user prompt, or a list of messages (LangChain
messages or `{"role": ..., "content": ...}` dicts)."""


@runtime_checkable
class AgentLike(Protocol):
    """Anything that can be run like an agent: an `Agent`, or a `Team` acting as one agent."""

    @property
    def name(self) -> str:
        """Unique name of the agent."""
        ...

    @property
    def card(self) -> AgentCard:
        """Public description of the agent."""
        ...

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        thread_id: str | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
    ) -> RunResult[Any]:
        """Run to completion and return the result.

        Implementations without conversation state raise `ValueError` for `thread_id` or `history`.
        awa's runnables also accept `timeout` and `metadata`, which this protocol does not require.
        """
        ...


__all__ = ["AgentInput", "AgentLike"]
