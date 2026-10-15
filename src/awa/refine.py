"""Refinement hooks: plain LangChain objects in, plain LangChain objects out.

A `refine.py` beside a `blueprint.yaml` holds hooks; when the blueprint is built with
`allow_imports=True` (as `awa run` and `awa eval` do), each hook of an agent runs once, in
definition order. An exported project ships `refine.py` unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

HookKind = Literal["model", "agent"]
"""`model`: the hook takes and returns the chat model; `agent`: the middleware list."""

MARK = "__awa_refine__"
"""Attribute a decorated hook carries: `(kind, agent names)`."""

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass(frozen=True)
class RefineContext:
    """What a hook knows about the agent it refines: blueprint name, agent name and model.

    `model` is the reference as the blueprint gives it (`None`: the default).
    """

    project: str
    agent: str
    model: str | None = None


def _hook(kind: HookKind, agents: tuple[str, ...]) -> Callable[[_F], _F]:
    for name in agents:
        if not isinstance(name, str) or not name:
            raise TypeError(f"refine_{kind}() takes agent names, got {name!r}")

    def mark(function: _F) -> _F:
        if not callable(function):
            raise TypeError(f"refine_{kind}() decorates a function, got {function!r}")
        setattr(function, MARK, (kind, agents))
        return function

    return mark


def refine_model(*agents: str) -> Callable[[_F], _F]:
    """Mark `fn(model, ctx) -> model` as a model hook of `agents` (every agent without names).

    Example:
        ```python
        @refine_model("analyst")
        def tune(model, ctx):
            return model.model_copy(update={"max_tokens": 16000})


        @refine_agent("analyst")
        def extra(middleware, ctx):
            from langchain.agents.middleware import ToolCallLimitMiddleware

            return [*middleware, ToolCallLimitMiddleware(tool_name="search", run_limit=5)]
        ```
    """

    return _hook("model", agents)


def refine_agent(*agents: str) -> Callable[[_F], _F]:
    """Mark `fn(middleware, ctx) -> middleware` as an agent hook of `agents` (every agent without names)."""

    return _hook("agent", agents)


__all__ = ["HookKind", "RefineContext", "refine_agent", "refine_model"]
