"""Find the agent's tools in the project's `tools.py`.

A tool is a LangChain tool object the module defines, or a documented function listed in the
module's `__all__` (its signature is the argument schema and its docstring the description).
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from types import ModuleType

from langchain_core.tools import BaseTool, tool


def project_tools(module: ModuleType, names: Sequence[str]) -> list[BaseTool]:
    """The tools `names` of `module`, in that order.

    Raises:
        LookupError: If the module defines no tool of one of the names.
    """

    found: dict[str, BaseTool] = {
        value.name: value for value in vars(module).values() if isinstance(value, BaseTool)
    }
    for attribute in getattr(module, "__all__", ()):
        value = getattr(module, attribute, None)
        if inspect.isfunction(value) and inspect.getdoc(value) and value.__name__ not in found:
            found[value.__name__] = tool(value)
    missing = [name for name in names if name not in found]
    if missing:
        raise LookupError(f"{module.__name__} defines no tool named {', '.join(missing)}")
    return [found[name] for name in names]
