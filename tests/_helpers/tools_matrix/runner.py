"""Build a tool from its spec and call it the way an agent's tool node does."""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import BaseTool

from awa.toolkits import catalog

from .specs import Case, ToolSpec
from .world import World


def build(
    spec: ToolSpec, world: World, case: Case | None = None, *, gated: bool = False
) -> BaseTool:
    """Create the tool with the spec's options (a case may replace them).

    Args:
        spec: The tool's spec.
        world: The world the options point into.
        case: A case whose `options` replace the spec's.
        gated: Keep the default approval; otherwise approval is switched off so the tool can
            be called directly (the gate itself is tested separately).
    """

    options = case.options(world) if case is not None and case.options else spec.options(world)
    entry = catalog().get(spec.name)
    if gated or not entry.requires_approval:
        return entry.create(**options)
    return entry.create(requires_approval=False, **options)


def call(tool: BaseTool, args: dict[str, Any]) -> str:
    """Call the tool asynchronously (builder tools are async) and return its text result."""

    result = asyncio.run(tool.ainvoke(args))
    assert isinstance(result, str), type(result)
    return result
