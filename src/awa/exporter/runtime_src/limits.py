"""Run limits that LangChain has no middleware for: total tokens and wall-clock time.

Model calls and tool calls are limited by `ModelCallLimitMiddleware` and
`ToolCallLimitMiddleware`; `RunLimits` adds the other two. Tokens are counted in the graph
state, so they survive a pause for approval. Time counts only the model and tool calls of a run
(a person's decision does not use it) and is kept in memory under a key the run keeps in its
state, so a restart of the process starts the count of a paused run again.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage


class RunLimitExceededError(RuntimeError):
    """A run used more tokens or time than its limit allows."""


class _State(AgentState[Any]):
    run_tokens: NotRequired[int]
    run_key: NotRequired[str]


class RunLimits(AgentMiddleware[Any, Any, Any]):
    """Stop a run that exceeds `max_total_tokens` or `max_duration` (seconds)."""

    state_schema = _State

    def __init__(
        self, *, max_total_tokens: int | None = None, max_duration: float | None = None
    ) -> None:
        super().__init__()
        self.max_total_tokens = max_total_tokens
        self.max_duration = max_duration
        self._spent: dict[str | None, float] = {}

    @staticmethod
    def _key(request: Any) -> str | None:
        state = getattr(request, "state", None)
        return state.get("run_key") if isinstance(state, dict) else None

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any]:
        """A new run starts: its token and time counts start at zero."""

        return {"run_tokens": 0, "run_key": uuid.uuid4().hex}

    def after_agent(self, state: Any, runtime: Any) -> None:
        """The run is over: forget its time."""

        self._spent.pop(state.get("run_key"), None)

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Count the tokens of the model call and stop when the budget is used up."""

        if self.max_total_tokens is None:
            return None
        last = state["messages"][-1]
        used = state.get("run_tokens", 0)
        if isinstance(last, AIMessage) and last.usage_metadata:
            used += int(last.usage_metadata.get("total_tokens", 0))
        if used > self.max_total_tokens:
            raise RunLimitExceededError(
                f"the run used {used} tokens; the limit is {self.max_total_tokens}"
            )
        return {"run_tokens": used}

    def _check(self, key: str | None, *, after: bool) -> None:
        spent = self._spent.get(key, 0.0)
        limit = self.max_duration
        if limit is not None and (spent > limit if after else spent >= limit):
            raise RunLimitExceededError(f"the run took {spent:.1f} seconds; the limit is {limit:g}")

    def _timed(self, handler: Callable[[Any], Any], request: Any) -> Any:
        key = self._key(request)
        self._check(key, after=False)
        started = time.monotonic()
        try:
            result = handler(request)
        finally:
            self._spent[key] = self._spent.get(key, 0.0) + time.monotonic() - started
        self._check(key, after=True)
        return result

    async def _atimed(self, handler: Callable[[Any], Awaitable[Any]], request: Any) -> Any:
        key = self._key(request)
        self._check(key, after=False)
        started = time.monotonic()
        try:
            result = await handler(request)
        finally:
            self._spent[key] = self._spent.get(key, 0.0) + time.monotonic() - started
        self._check(key, after=True)
        return result

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """Count the model call's time against `max_duration`."""

        return self._timed(handler, request)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """Count the model call's time against `max_duration`."""

        return await self._atimed(handler, request)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """Count the tool call's time against `max_duration`."""

        return self._timed(handler, request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """Count the tool call's time against `max_duration`."""

        return await self._atimed(handler, request)
