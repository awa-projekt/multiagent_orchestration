"""A scripted system for the automation tests: raises the scripted errors, then answers with its input."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from awa.contracts import AgentCard, RunResult


class ScriptedSystem:
    """An `AgentLike` that raises the scripted errors, then answers with its input."""

    def __init__(self, name: str = "desk", errors: Sequence[BaseException] = ()) -> None:
        self._name = name
        self.errors = list(errors)
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def card(self) -> AgentCard:
        return AgentCard(name=self._name, description="scripted")

    async def run(
        self,
        input: Any,
        *,
        deps: Any = None,
        usage_limits: Any = None,
        thread_id: str | None = None,
        history: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult[Any]:
        self.calls.append({"input": input, "deps": deps, "metadata": dict(metadata or {})})
        if self.errors:
            raise self.errors.pop(0)
        return RunResult(run_id="r", agent=self._name, output=f"done: {input}")
