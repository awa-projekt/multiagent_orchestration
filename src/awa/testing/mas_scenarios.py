"""Assertions for loop guards in multi-agent graphs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from awa._internal.upstream.graph import CompiledGraph, GraphRecursionError
from awa._internal.upstream.runnables import RunnableConfig


async def assert_terminates_via_own_budget(
    graph: CompiledGraph,
    initial_state: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    recursion_limit: int = 50,
) -> dict[str, Any]:
    """Run `graph` and assert it stopped through its own budget logic, not the recursion limit.

    Use it with fakes that force perpetual disagreement; a low `recursion_limit` makes a broken
    guard fail fast. Returns the final state.
    """

    run_config = cast(RunnableConfig, {**(config or {}), "recursion_limit": recursion_limit})
    try:
        result = await graph.ainvoke(dict(initial_state), config=run_config)
    except GraphRecursionError as error:
        raise AssertionError(
            f"The graph hit recursion_limit={recursion_limit} instead of stopping through its own "
            "budget; make its router reach a terminal node once the budget is exhausted."
        ) from error
    return dict(result)
