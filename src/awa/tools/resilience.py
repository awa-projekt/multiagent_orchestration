"""Tool resilience: retries of network failures and a per-tool circuit breaker.

Only tools that do not act outside the agent are retried (`AWA_TOOL_RETRIES`); acting tools hand
their error to the model. After `AWA_TOOL_CIRCUIT_BREAKER` failures in a row a tool is switched
off for the run and the model is told to continue without it.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from awa._internal.upstream.agents import AgentMiddleware, ToolCallRequest, ToolRetryMiddleware
from awa._internal.upstream.graph import Command
from awa._internal.upstream.messages import ToolMessage
from awa._internal.upstream.tools import BaseTool
from awa.errors import AwaError, UsageLimitExceededError
from awa.runtime.context import current_run_context
from awa.runtime.engine import current_engine
from awa.tools.risk import infer_tool_risk

_TRANSIENT_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "TransportError",
        "URLError",
        "RemoteDisconnected",
        "IncompleteRead",
        "ServiceUnavailable",
    }
)


def is_transient(exc: BaseException) -> bool:
    """Whether `exc` is a network failure to retry; awa's own errors and timeouts never are."""

    if isinstance(exc, AwaError):
        return False
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return any(cls.__name__ in _TRANSIENT_NAMES for cls in type(exc).__mro__)


class CircuitBreakerMiddleware(AgentMiddleware):
    """Stops calling a tool for the rest of a run after `threshold` failures in a row.

    A failure is an error result or a raise; a success resets the count. State is kept per run.
    """

    def __init__(self, threshold: int = 3) -> None:
        super().__init__()
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        self.threshold = threshold
        self._failures: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _run_id() -> str:
        ctx = current_run_context()
        return ctx.run_id if ctx is not None else ""

    def _record(self, key: tuple[str, str], failed: bool) -> None:
        with self._lock:
            if failed:
                self._failures[key] = self._failures.get(key, 0) + 1
            else:
                self._failures.pop(key, None)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Short-circuit a switched-off tool, else call it and count the outcome."""

        call = request.tool_call
        key = (self._run_id(), call["name"])
        with self._lock:
            failures = self._failures.get(key, 0)
        if failures >= self.threshold:
            return ToolMessage(
                content=(
                    f"Error: {call['name']} is unavailable in this run (it failed {failures} times "
                    "in a row). Do not call it again; continue with the other tools and state in "
                    "your answer what could not be checked."
                ),
                tool_call_id=call["id"] or "",
                name=call["name"],
                status="error",
            )
        try:
            result = await handler(request)
        except (UsageLimitExceededError,) + current_engine().control_flow:
            raise
        except Exception:
            self._record(key, True)
            raise
        self._record(key, isinstance(result, ToolMessage) and result.status == "error")
        return result

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        """Forget the finished run's counts."""

        run_id = self._run_id()
        with self._lock:
            for key in [k for k in self._failures if k[0] == run_id]:
                del self._failures[key]


def tool_layer_middleware(
    tools: Sequence[BaseTool],
) -> tuple[list[AgentMiddleware], list[AgentMiddleware]]:
    """The tool layer middleware of the process settings: `(outer, inner)`.

    `outer` goes outside run tracking and prompt caching (it changes the offered tools); `inner`
    goes inside run tracking.
    """

    from awa.core.settings import get_settings
    from awa.tools.offload import OffloadMiddleware, current_profile

    settings = get_settings()
    outer: list[AgentMiddleware] = [OffloadMiddleware()] if current_profile().offload else []
    inner: list[AgentMiddleware] = []
    if settings.tool_circuit_breaker > 0:
        inner.append(CircuitBreakerMiddleware(settings.tool_circuit_breaker))
    retryable: list[BaseTool | str] = [
        t.name for t in tools if infer_tool_risk(t).external_action is False
    ]
    if settings.tool_retries > 0 and retryable:
        inner.append(
            ToolRetryMiddleware(
                tools=retryable,
                max_retries=settings.tool_retries,
                retry_on=is_transient,
                on_failure="continue",
                initial_delay=0.5,
                max_delay=8.0,
                jitter=True,
            )
        )
    return outer, inner


__all__ = ["CircuitBreakerMiddleware", "is_transient", "tool_layer_middleware"]
