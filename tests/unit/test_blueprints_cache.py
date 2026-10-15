"""The blueprint's `defaults.cache` block and the `memory` and `compaction` fields of its agents."""

from __future__ import annotations

from typing import Any

import pytest

from awa.agents import Agent
from awa.blueprints import Blueprint, BlueprintError
from awa.core.settings import get_settings
from awa.memory import MEMORY_TOOL_NAMES, Compaction
from awa.models import ModelRegistry
from awa.runtime.cache import Cache, CacheSettings
from awa.team import Team
from awa.testing import FakeChatModel, tool_call
from awa.tools import tool

CALLS: list[str] = []


@tool(risk={"external_action": False})
def lookup(order_id: str) -> str:
    """Look up an order."""
    CALLS.append(order_id)
    return f"order {order_id}: shipped"


def blueprint(**cache: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "shop",
            "defaults": {"cache": cache} if cache else {},
            "agents": [
                {"name": "first", "tools": ["lookup"]},
                {"name": "second", "tools": ["lookup"]},
            ],
            "workflow": {"sequential": ["first", "second"]},
        }
    )


def models() -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(
        "default",
        FakeChatModel([tool_call("lookup", {"order_id": "7"}), "done"], repeat_last=False),
    )
    return registry


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    CALLS.clear()
    for name in ("AWA_CACHE_ENABLED", "AWA_CACHE_TOOLS", "AWA_CACHE_PROMPT", "AWA_CACHE_STORE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def test_the_block_is_validated_like_every_other_setting() -> None:
    assert blueprint().defaults.cache == CacheSettings()
    assert blueprint(prompt="1h", tools_ttl=60).defaults.cache.prompt == "1h"
    with pytest.raises(BlueprintError):
        blueprint(prompts="auto")


def test_every_agent_of_a_system_shares_one_cache() -> None:
    system = blueprint(prompt="off").build(tools=[lookup], model_registry=models())

    agents = list(system._steps)  # type: ignore[attr-defined]
    caches = {id(agent._cache) for agent in agents if isinstance(agent, Agent)}
    assert len(caches) == 1
    shared: Cache = agents[0]._cache
    assert shared.project == "shop" and shared.settings.prompt == "off"
    assert all(agent._prompt_cache == "off" for agent in agents)


async def test_a_lookup_of_the_first_agent_is_reused_by_the_second() -> None:
    def scripted() -> ModelRegistry:
        registry = ModelRegistry()
        registry.register(
            "default", FakeChatModel([tool_call("lookup", {"order_id": "7"}), "done"] * 2)
        )
        return registry

    system = blueprint().build(tools=[lookup], model_registry=scripted())
    await system.run("Where is order 7?")

    assert CALLS == ["7"]


async def test_the_environment_switch_wins_over_the_blueprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWA_CACHE_TOOLS", "false")
    get_settings.cache_clear()
    registry = ModelRegistry()
    registry.register(
        "default", FakeChatModel([tool_call("lookup", {"order_id": "7"}), "done"] * 2)
    )

    system = blueprint(tools=True).build(tools=[lookup], model_registry=registry)
    await system.run("Where is order 7?")

    assert CALLS == ["7", "7"]


def test_a_given_cache_is_used_as_it_is() -> None:
    mine = Cache(CacheSettings(prompt="1h"), None, project="mine")

    system = blueprint().build(tools=[lookup], model_registry=models(), cache=mine)

    assert all(agent._cache is mine for agent in system._steps)  # type: ignore[attr-defined]


def blueprint_memory(**agent_fields: Any) -> dict[str, Any]:
    return {
        "name": "support",
        "agents": [{"name": "assistant", "description": "Helps.", **agent_fields}],
    }


def test_agent_memory_and_compaction_are_built() -> None:
    built = Blueprint.from_dict(
        blueprint_memory(
            memory={"scope": "user", "auto_recall": 3},
            compaction={"max_tokens": 12000, "keep_last_messages": 12},
        )
    ).build()

    assert isinstance(built, Agent)
    assert built.memory is not None
    assert (built.memory.scope, built.memory.auto_recall) == ("user", 3)
    assert built.compaction == Compaction(max_tokens=12000, keep_last_messages=12)
    assert set(MEMORY_TOOL_NAMES) <= set(built.card.tools)


def test_agents_without_the_fields_have_neither() -> None:
    built = Blueprint.from_dict(blueprint_memory()).build()
    assert isinstance(built, Agent)
    assert built.memory is None and built.compaction is None


def test_agents_with_the_same_scope_share_one_memory() -> None:
    data = {
        "name": "desk",
        "agents": [
            {"name": "lead", "description": "Leads.", "memory": {"scope": "team"}},
            {"name": "writer", "description": "Writes.", "memory": {"scope": "team"}},
            {"name": "editor", "description": "Edits.", "memory": {"scope": "agent"}},
        ],
        "entrypoint": "team",
        "team": {"orchestrator": "lead", "members": ["writer", "editor"]},
    }
    team = Blueprint.from_dict(data).build()
    assert isinstance(team, Team)
    agents = {a.name: a for a in (team.orchestrator, *team.members) if isinstance(a, Agent)}

    lead, writer, editor = (agents[name].memory for name in ("lead", "writer", "editor"))
    assert lead is not None and editor is not None
    assert lead is writer
    assert editor is not lead and editor.scope == "agent"
    assert lead.store is editor.store


@pytest.mark.parametrize(
    "fields",
    [
        {"memory": {"scope": "tenant"}},
        {"memory": {"auto_recall": -1}},
        {"memory": {"unknown": 1}},
        {"compaction": {"max_tokens": 0}},
        {"compaction": {"keep_last_messages": 0}},
    ],
)
def test_invalid_fields_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(BlueprintError):
        Blueprint.from_dict(blueprint_memory(**fields))


def test_team_scope_outside_a_team_is_an_error() -> None:
    issues = Blueprint.from_dict(blueprint_memory(memory={"scope": "team"})).check()
    assert [(i.severity, i.path) for i in issues] == [("error", "agents[0].memory.scope")]


def test_fields_round_trip_through_yaml() -> None:
    original = Blueprint.from_dict(
        blueprint_memory(memory={"scope": "user", "auto_recall": 2}, compaction={"max_tokens": 900})
    )
    again = Blueprint.from_yaml(original.to_yaml())
    assert again.agents[0].memory == original.agents[0].memory
    assert again.agents[0].compaction == original.agents[0].compaction


def test_require_user_is_built_and_refuses_anonymous_runs() -> None:
    built = Blueprint.from_dict(
        blueprint_memory(memory={"scope": "agent", "require_user": True, "user_id": "principal"})
    ).build()
    assert isinstance(built, Agent) and built.memory is not None
    assert built.memory.require_user is True
    assert Blueprint.from_dict(blueprint_memory(memory={})).build().memory.require_user is False  # type: ignore[union-attr]


def test_memories_differing_in_require_user_are_separate() -> None:
    data = {
        "name": "desk",
        "agents": [
            {"name": "lead", "description": "Leads.", "memory": {"scope": "global"}},
            {
                "name": "writer",
                "description": "Writes.",
                "memory": {"scope": "global", "require_user": True, "user_id": "deps"},
            },
        ],
        "entrypoint": "team",
        "team": {"orchestrator": "lead", "members": ["writer"]},
    }
    team = Blueprint.from_dict(data).build()
    assert isinstance(team, Team)
    lead, writer = (a.memory for a in (team.orchestrator, *team.members) if isinstance(a, Agent))
    assert lead is not None and writer is not None
    assert lead is not writer and lead.store is writer.store


@pytest.mark.parametrize(
    ("memory", "warned"),
    [
        ({"scope": "user"}, True),
        ({"scope": "agent", "require_user": True}, True),
        ({"scope": "user", "user_id": "principal"}, False),
        ({"scope": "user", "user_id": "deps", "require_user": True}, False),
        ({"scope": "agent"}, False),
        ({"scope": "global", "require_user": False}, False),
    ],
)
def test_per_user_memory_without_a_user_id_source_is_a_warning(
    memory: dict[str, Any], warned: bool
) -> None:
    issues = Blueprint.from_dict(blueprint_memory(memory=memory)).check()
    expected = [("warning", "agents[0].memory.user_id")] if warned else []
    assert [(i.severity, i.path) for i in issues] == expected
    if warned:
        assert "'principal'" in issues[0].message and "awa serve" in issues[0].message


def test_unknown_user_id_source_is_rejected() -> None:
    with pytest.raises(BlueprintError):
        Blueprint.from_dict(blueprint_memory(memory={"scope": "user", "user_id": "header"}))
