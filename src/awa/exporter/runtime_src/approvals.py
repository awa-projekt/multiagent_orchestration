"""Approval gates for the human-in-the-loop middleware.

An unconditional gate asks a person before every call of a tool. A conditional gate asks only
when an expression over the call's arguments holds (`amount >= 5000`). The expression language is
the safe one of `conditions.py`: it is parsed, never evaluated with `eval`, and a call whose
condition cannot be decided (arithmetic on text, a missing value that would have to be guessed)
asks for approval: the gate fails closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import ToolCallRequest
from langchain_core.tools import BaseTool, ToolException

from .conditions import predicate

DECISIONS = ["approve", "edit", "reject"]
"""What a reviewer may decide: run the call, run it with changed arguments, or refuse it."""


def needs_approval(
    expression: str, defaults: Mapping[str, Any] | None = None
) -> Callable[[ToolCallRequest], bool]:
    """The `when` predicate of a gate: whether a call needs a person's decision.

    Args:
        expression: The condition over the call's arguments.
        defaults: Values of arguments a call leaves out (the tool's own defaults).

    Raises:
        ToolException: If the expression is malformed or uses anything the language forbids.
    """

    parsed = predicate(" ".join(expression.split()), strict=True)
    fallback = dict(defaults or {})

    def when(request: ToolCallRequest) -> bool:
        arguments = request.tool_call["args"]

        def value(name: str) -> Any:
            return arguments[name] if name in arguments else fallback.get(name)

        try:
            return bool(parsed.evaluate(value))
        except ToolException:
            return True

    return when


def interrupt_on(
    gates: Mapping[str, str | None], tools: Sequence[BaseTool]
) -> dict[str, dict[str, Any]]:
    """The `interrupt_on` argument of `HumanInTheLoopMiddleware` for `gates`.

    Args:
        gates: Tool name to the condition that asks for approval, or `None` for every call.
        tools: The agent's tools; their argument defaults complete a condition.
    """

    by_name = {tool.name: tool for tool in tools}
    config: dict[str, dict[str, Any]] = {}
    for name, expression in gates.items():
        entry: dict[str, Any] = {"allowed_decisions": list(DECISIONS)}
        if expression is not None:
            defaults = {
                key: schema["default"]
                for key, schema in by_name[name].args.items()
                if isinstance(schema, Mapping) and "default" in schema
            }
            entry["when"] = needs_approval(expression, defaults)
        config[name] = entry
    return config
