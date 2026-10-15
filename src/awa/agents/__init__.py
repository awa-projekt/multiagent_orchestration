"""Runtime layer: the `Agent` class, lifecycle hooks, run tracking and text streaming.

Names load on first use, so importing `Agent` does not load LangGraph until an agent first runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa.agents.agent import Agent, AgentDepsT, AgentOutputT, Instructions
    from awa.agents.hooks import AgentHooks
    from awa.agents.middleware import RunTrackingMiddleware
    from awa.kernel.output import ModelRetry
    from awa.runtime._streaming import TextDeltaCallback, text_delta_callbacks

_HOMES = homes(
    {
        "awa.agents.agent": "Agent AgentDepsT AgentOutputT Instructions",
        "awa.agents.hooks": "AgentHooks",
        "awa.kernel.output": "ModelRetry",
        "awa.agents.middleware": "RunTrackingMiddleware",
        "awa.runtime._streaming": "TextDeltaCallback text_delta_callbacks",
    }
)

__all__ = [
    "Agent",
    "AgentDepsT",
    "AgentHooks",
    "AgentOutputT",
    "Instructions",
    "ModelRetry",
    "RunTrackingMiddleware",
    "TextDeltaCallback",
    "text_delta_callbacks",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
