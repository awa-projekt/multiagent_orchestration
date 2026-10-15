"""LangChain tools that let an agent look up other agents in an `AgentDirectory`."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from awa._internal.serialize import to_json
from awa._internal.upstream.tools import BaseTool, StructuredTool
from awa.contracts.cards import AgentCard
from awa.directory.directory import AgentDirectory

MAX_FIND_LIMIT = 20
"""Upper bound applied to the `limit` argument of `find_agents` (lower bound: 1)."""


def create_directory_tools(
    directory: AgentDirectory,
    *,
    viewer: str | None = None,
    visible: Sequence[str] | Callable[[AgentCard], bool] | None = None,
) -> list[BaseTool]:
    """Create the `list_agents`, `find_agents` and `describe_agent` tools.

    Each tool returns a compact JSON string. Visibility is evaluated on every
    call, so the tools reflect later changes to the directory.

    Args:
        directory: The directory to expose.
        viewer: Name of the agent using the tools; never in `list_agents` or `find_agents` results.
        visible: Names or a card predicate the viewer may see; `None` shows every agent.
    """

    if isinstance(visible, str):
        raise TypeError("visible must be a sequence of agent names, not a single string")
    allowed = None if visible is None or callable(visible) else frozenset(visible)

    def can_see(card: AgentCard) -> bool:
        if allowed is not None:
            return card.name in allowed
        return not callable(visible) or visible(card)

    def listed(card: AgentCard) -> bool:
        return card.name != viewer and can_see(card)

    def list_agents(capability: str | None = None) -> str:
        """List the agents you can work with: name, description, capabilities, skills and
        current status. Optionally filter by capability.

        Args:
            capability: Only list agents with this capability (e.g. 'research'). Omit to list all.
        """

        agents = [
            {
                "name": card.name,
                "description": card.description,
                "capabilities": list(card.capabilities),
                "skills": list(card.skills),
                "status": directory.presence(card.name).status.value,
            }
            for card in directory.list(capability=capability)
            if listed(card)
        ]
        return to_json({"agents": agents})

    def find_agents(query: str, limit: int = 5) -> str:
        """Find the agents best suited for a task, ranked by relevance, with the reasons each
        one matched.

        Args:
            query: The task you need help with, in plain language.
            limit: Maximum number of agents to return (1-20).
        """

        ranked = directory.find(query, limit=max(1, len(directory)))
        bounded = min(max(limit, 1), MAX_FIND_LIMIT)
        matches = [match for match in ranked if listed(match.card)][:bounded]
        agents = [
            {
                "name": match.card.name,
                "description": match.card.description,
                "capabilities": list(match.card.capabilities),
                "status": directory.presence(match.card.name).status.value,
                "score": match.score,
                "reasons": list(match.reasons),
            }
            for match in matches
        ]
        return to_json({"agents": agents})

    def describe_agent(name: str) -> str:
        """Show the full card of one agent: description, capabilities, skills, tools,
        input/output schema and current presence.

        Args:
            name: Exact name of the agent to describe.
        """

        card = directory.get(name) if name in directory else None
        if card is None or not can_see(card):
            known = ", ".join(card.name for card in directory if listed(card)) or "none"
            return to_json({"error": f"Unknown agent {name!r}. Available agents: {known}"})
        description = card.model_dump(mode="json", exclude_none=True)
        description["presence"] = directory.presence(name).model_dump(mode="json")
        return to_json(description)

    return [_tool(list_agents), _tool(find_agents), _tool(describe_agent)]


def _tool(func: Callable[..., str]) -> BaseTool:
    """Turn a handler into a tool (arguments and description from its signature and docstring)
    that runs inline, without an executor, when awaited."""

    async def coroutine(**kwargs: Any) -> str:
        return func(**kwargs)

    return StructuredTool.from_function(func=func, coroutine=coroutine, parse_docstring=True)
