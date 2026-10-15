"""`AgentDirectory`: registration, lookup, ranking, presence, rendering, watching, serialisation and its tools."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from awa._internal.text import stem, words
from awa.contracts import (
    AgentCard,
    AgentFinishedEvent,
    AgentLike,
    AgentStartedEvent,
    AgentStatus,
    ErrorEvent,
    RunResult,
    TextDeltaEvent,
)
from awa.directory import AgentDirectory, AgentPresence, DirectoryMatch, create_directory_tools
from awa.directory.directory import _query_terms
from awa.directory.tools import MAX_FIND_LIMIT
from awa.errors import AgentAlreadyRegisteredError, AgentNotFoundError
from awa.runtime import RunContext, use_run_context


@dataclass
class StubAgent:
    """Minimal object satisfying `AgentLike`."""

    name: str
    card: AgentCard

    async def run(
        self,
        input: Any,
        *,
        deps: Any = None,
        usage_limits: Any = None,
        thread_id: str | None = None,
        history: Any = None,
    ) -> RunResult[Any]:
        raise NotImplementedError


def stub(name: str, **card_fields: Any) -> StubAgent:
    return StubAgent(name=name, card=AgentCard(name=name, **card_fields))


def sample_directory() -> AgentDirectory:
    return AgentDirectory(
        [
            AgentCard(
                name="researcher",
                description="Finds evidence.",
                capabilities=("research", "web.search"),
                skills=("citations",),
                tags=("core",),
                team="desk",
                role="specialist",
            ),
            AgentCard(
                name="billing",
                description="Handles refunds.",
                capabilities=("Billing.Refunds",),
                tags=("finance", "core"),
                team="support",
            ),
            AgentCard(name="lead", description="Coordinates.", role="orchestrator", team="desk"),
        ]
    )


def test_stub_satisfies_agent_like() -> None:
    assert isinstance(stub("a"), AgentLike)


def test_register_card_and_live_agent() -> None:
    directory = AgentDirectory()
    card = AgentCard(name="writer")
    live = stub("researcher", capabilities=("research",))

    assert directory.register(card) is card
    assert directory.register(live) is live.card
    assert directory.get("writer") is card
    assert directory.resolve("writer") is None
    assert directory.resolve("researcher") is live
    assert len(directory) == 2
    assert "writer" in directory
    assert "nobody" not in directory
    assert directory.names() == ["researcher", "writer"]
    assert [card.name for card in directory] == ["researcher", "writer"]


def test_constructor_registers_initial_agents() -> None:
    directory = AgentDirectory([AgentCard(name="b"), stub("a")])
    assert directory.names() == ["a", "b"]


def test_constructor_rejects_duplicates() -> None:
    with pytest.raises(AgentAlreadyRegisteredError):
        AgentDirectory([AgentCard(name="a"), AgentCard(name="a")])


def test_duplicate_registration_raises_unless_replace() -> None:
    directory = AgentDirectory([stub("a", description="old")])
    with pytest.raises(AgentAlreadyRegisteredError, match="'a'"):
        directory.register(AgentCard(name="a"))

    new = AgentCard(name="a", description="new")
    assert directory.register(new, replace=True) is new
    assert directory.get("a").description == "new"
    assert directory.resolve("a") is None


def test_replace_keeps_presence() -> None:
    directory = AgentDirectory([AgentCard(name="a")])
    directory.record_run_end("a", ok=True, duration_ms=10)
    directory.set_status("a", AgentStatus.OFFLINE)

    directory.register(stub("a"), replace=True)

    presence = directory.presence("a")
    assert presence.total_runs == 1
    assert presence.status is AgentStatus.OFFLINE


def test_register_rejects_invalid_objects() -> None:
    directory = AgentDirectory()
    with pytest.raises(TypeError):
        directory.register("writer")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="does not match"):
        directory.register(StubAgent(name="alias", card=AgentCard(name="real")))

    @dataclass
    class BadCard:
        name: str
        card: dict[str, Any]

        async def run(self, input: Any, **kwargs: Any) -> Any:
            return None

    with pytest.raises(TypeError, match="AgentCard"):
        directory.register(BadCard(name="x", card={}))  # type: ignore[arg-type]


def test_unregister() -> None:
    directory = AgentDirectory([AgentCard(name="a")])
    directory.unregister("a")
    assert "a" not in directory
    with pytest.raises(AgentNotFoundError):
        directory.unregister("a")


def test_unknown_names_raise_with_known_names() -> None:
    directory = AgentDirectory([AgentCard(name="a")])
    for call in (directory.get, directory.resolve, directory.presence):
        with pytest.raises(AgentNotFoundError, match="Registered agents: a"):
            call("missing")
    with pytest.raises(AgentNotFoundError, match="none"):
        AgentDirectory().get("missing")


def test_list_without_filters_is_name_ordered() -> None:
    assert [card.name for card in sample_directory().list()] == ["billing", "lead", "researcher"]


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"capability": "research"}, ["researcher"]),
        ({"capability": "WEB.search"}, ["researcher"]),
        ({"capability": "web"}, ["researcher"]),
        ({"capability": "billing"}, ["billing"]),
        ({"capability": "billing.refunds"}, ["billing"]),
        ({"capability": "bill"}, []),
        ({"skill": "Citations"}, ["researcher"]),
        ({"tag": "core"}, ["billing", "researcher"]),
        ({"team": "desk"}, ["lead", "researcher"]),
        ({"role": "orchestrator"}, ["lead"]),
        ({"team": "desk", "role": "specialist"}, ["researcher"]),
        ({"tag": "core", "team": "support"}, ["billing"]),
        ({"team": "nobody"}, []),
    ],
)
def test_list_filters(filters: dict[str, Any], expected: list[str]) -> None:
    assert [card.name for card in sample_directory().list(**filters)] == expected


def test_list_filters_by_status() -> None:
    directory = sample_directory()
    directory.record_run_start("billing")
    directory.set_status("lead", AgentStatus.OFFLINE)

    assert [c.name for c in directory.list(status=AgentStatus.BUSY)] == ["billing"]
    assert [c.name for c in directory.list(status=AgentStatus.OFFLINE)] == ["lead"]
    assert [c.name for c in directory.list(status=AgentStatus.AVAILABLE)] == ["researcher"]


def test_render_catalog() -> None:
    directory = sample_directory()
    directory.register(AgentCard(name="silent"))

    assert directory.render() == "\n".join(
        [
            "- **billing**: Handles refunds. (capabilities: Billing.Refunds)",
            "- **lead**: Coordinates.",
            "- **researcher**: Finds evidence. "
            "(capabilities: research, web.search; skills: citations)",
            "- **silent**: No description.",
        ]
    )


def test_render_names_the_fields_a_structured_member_returns() -> None:
    """An orchestrator sees what a member answers with, so it does not ask for other fields."""

    directory = AgentDirectory()
    schema = {"type": "object", "properties": {"status": {}, "vendor_id": {}, "differences": {}}}
    directory.register(AgentCard(name="validator", description="Matches.", output_schema=schema))
    wide = {"type": "object", "properties": {f"f{i}": {} for i in range(14)}}
    directory.register(AgentCard(name="wide", output_schema=wide))
    directory.register(AgentCard(name="text", output_schema={"type": "string"}))

    assert directory.render() == "\n".join(
        [
            "- **text**: No description.",
            "- **validator**: Matches. (returns: status, vendor_id, differences)",
            "- **wide**: No description. (returns: "
            + ", ".join(f"f{i}" for i in range(12))
            + ", ...)",
        ]
    )


def test_render_selected_names_with_presence() -> None:
    directory = sample_directory()
    directory.record_run_start("researcher")
    directory.set_status("lead", AgentStatus.OFFLINE)

    assert directory.render(["researcher", "lead"], include_presence=True) == "\n".join(
        [
            "- **researcher**: Finds evidence. (capabilities: research, web.search; "
            "skills: citations; status: busy; active runs: 1)",
            "- **lead**: Coordinates. (status: offline)",
        ]
    )
    assert directory.render([]) == ""
    assert AgentDirectory().render() == ""
    with pytest.raises(AgentNotFoundError):
        directory.render(["missing"])


CARDS = [
    AgentCard(
        name="researcher",
        description="Finds and cites evidence from the web.",
        capabilities=("research", "web.search"),
        skills=("citations",),
        tools=("web_search",),
    ),
    AgentCard(
        name="writer",
        description="Writes the final answer in clear prose.",
        capabilities=("writing",),
        tags=("prose",),
    ),
    AgentCard(
        name="billing",
        description="Handles refunds and invoices.",
        capabilities=("billing.refunds", "billing.invoices"),
    ),
    AgentCard(name="reviewer", description="Reviews drafts for clarity.", tags=("quality",)),
]


def directory() -> AgentDirectory:
    return AgentDirectory(CARDS)


def names(matches: list[DirectoryMatch]) -> list[str]:
    return [match.card.name for match in matches]


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("research", "research"),
        ("researcher", "research"),
        ("researching", "research"),
        ("researchers", "research"),
        ("queries", "query"),
        ("refunds", "refund"),
        ("billed", "bill"),
        ("class", "class"),
        ("status", "status"),
        ("user", "user"),
        ("bus", "bus"),
        ("searches", "search"),
        ("boxes", "box"),
        ("addresses", "address"),
    ],
)
def test_stem(word: str, expected: str) -> None:
    assert stem(word) == expected


def test_words_split_punctuation_and_camel_case() -> None:
    assert words("webSearch billing.refunds web_search-API") == [
        "web",
        "search",
        "billing",
        "refunds",
        "web",
        "search",
        "api",
    ]


def test_query_terms_drop_stopwords_and_duplicates() -> None:
    assert _query_terms("Find the agent who researches research") == [
        ("find", "find"),
        ("researches", "research"),
    ]
    assert _query_terms("the agent for me") == []


def test_capability_and_name_outrank_description() -> None:
    matches = directory().find("research")
    assert names(matches) == ["researcher"]
    assert matches[0].score == 6.0
    assert matches[0].reasons == (
        "name 'researcher' matches 'research'",
        "capability 'research' matches 'research'",
    )


def test_reasons_explain_every_contribution() -> None:
    [match] = directory().find("refund an invoice")
    assert match.card.name == "billing"
    assert match.score == 8.0
    assert match.reasons == (
        "capability 'billing.refunds' matches 'refund'",
        "capability 'billing.invoices' matches 'invoice'",
        "description 'refunds' matches 'refund'",
        "description 'invoices' matches 'invoice'",
    )


def test_prefix_matches_score_half() -> None:
    [match] = directory().find("write")
    assert match.card.name == "writer"
    # name "writer" -> "writ" (prefix, 3 * 0.5), capability "writing" -> "writ"
    # (prefix, 3 * 0.5), description "writes" -> "write" (exact, 1).
    assert match.score == 4.0
    assert "name 'writer' partially matches 'write'" in match.reasons
    assert "description 'writes' matches 'write'" in match.reasons


def test_short_prefixes_do_not_match() -> None:
    assert directory().find("wr") == []


def test_all_fields_are_searched() -> None:
    assert names(directory().find("citations")) == ["researcher"]  # skill
    assert names(directory().find("quality")) == ["reviewer"]  # tag
    assert names(directory().find("web_search")) == ["researcher"]  # tool + capability
    assert names(directory().find("clarity")) == ["reviewer"]  # description


def test_multiple_agents_ranked_by_score() -> None:
    matches = directory().find("clear prose drafts")
    assert names(matches) == ["writer", "reviewer"]
    assert matches[0].score > matches[1].score


def test_ties_broken_by_name() -> None:
    cards = [
        AgentCard(name="zeta", capabilities=("translation",)),
        AgentCard(name="alpha", capabilities=("translation",)),
        AgentCard(name="mid", capabilities=("translation",)),
    ]
    matches = AgentDirectory(cards).find("translation")
    assert names(matches) == ["alpha", "mid", "zeta"]
    assert {match.score for match in matches} == {3.0}


def test_ranking_independent_of_registration_order() -> None:
    expected = directory().find("search the web and write clear answers")
    shuffled = list(CARDS)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert AgentDirectory(shuffled).find("search the web and write clear answers") == expected


def test_zero_score_and_empty_queries_return_nothing() -> None:
    assert directory().find("quantum chromodynamics") == []
    assert directory().find("") == []
    assert directory().find("the agent who can") == []


def test_limit() -> None:
    cards = [AgentCard(name=f"agent{i}", capabilities=("translation",)) for i in range(8)]
    many = AgentDirectory(cards)
    assert len(many.find("translation")) == 5
    assert len(many.find("translation", limit=2)) == 2
    with pytest.raises(ValueError, match="limit"):
        many.find("translation", limit=0)


def test_capability_filter() -> None:
    assert names(directory().find("clear prose drafts")) == ["writer", "reviewer"]
    assert names(directory().find("clear prose drafts", capability="writing")) == ["writer"]
    assert directory().find("refunds", capability="research") == []


def test_available_only_excludes_busy_and_offline() -> None:
    d = AgentDirectory(
        [
            AgentCard(name="one", capabilities=("translation",)),
            AgentCard(name="two", capabilities=("translation",)),
            AgentCard(name="three", capabilities=("translation",)),
        ]
    )
    d.record_run_start("one")
    d.set_status("two", AgentStatus.OFFLINE)
    assert names(d.find("translation")) == ["one", "three", "two"]
    assert names(d.find("translation", available_only=True)) == ["three"]


def test_replaced_card_is_reindexed() -> None:
    d = directory()
    d.register(AgentCard(name="writer", capabilities=("poetry",)), replace=True)
    assert d.find("prose") == []
    assert names(d.find("poetry")) == ["writer"]


def directory_presence() -> AgentDirectory:
    return AgentDirectory([AgentCard(name="a"), AgentCard(name="b")])


def started(agent: str, run_id: str, at: datetime | None = None) -> AgentStartedEvent:
    extra = {"timestamp": at} if at is not None else {}
    return AgentStartedEvent(run_id=run_id, root_run_id=run_id, agent=agent, **extra)


def finished(agent: str, run_id: str, duration_ms: int) -> AgentFinishedEvent:
    return AgentFinishedEvent(
        run_id=run_id, root_run_id=run_id, agent=agent, duration_ms=duration_ms
    )


def failed(agent: str, run_id: str, at: datetime | None = None) -> ErrorEvent:
    extra = {"timestamp": at} if at is not None else {}
    return ErrorEvent(
        run_id=run_id, root_run_id=run_id, agent=agent, error_type="X", message="m", **extra
    )


def test_initial_presence() -> None:
    assert directory_presence().presence("a") == AgentPresence(status=AgentStatus.AVAILABLE)


def test_run_lifecycle_updates_status_and_statistics() -> None:
    d = directory_presence()
    before = datetime.now(UTC)

    d.record_run_start("a")
    d.record_run_start("a")
    busy = d.presence("a")
    assert busy.status is AgentStatus.BUSY
    assert busy.active_runs == 2
    assert busy.last_active_at is not None and busy.last_active_at >= before

    d.record_run_end("a", ok=True, duration_ms=100)
    assert d.presence("a").status is AgentStatus.BUSY
    d.record_run_end("a", ok=False, duration_ms=300)

    done = d.presence("a")
    assert done.status is AgentStatus.AVAILABLE
    assert done.active_runs == 0
    assert done.total_runs == 2
    assert done.failed_runs == 1
    assert done.average_duration_ms == 200.0
    assert d.presence("b").total_runs == 0


def test_run_end_without_start_does_not_go_negative() -> None:
    d = directory_presence()
    d.record_run_end("a", ok=True, duration_ms=5)
    assert d.presence("a").active_runs == 0
    assert d.presence("a").total_runs == 1


def test_negative_duration_rejected() -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        directory_presence().record_run_end("a", ok=True, duration_ms=-1)


def test_presence_calls_on_unknown_agent_raise() -> None:
    d = directory_presence()
    with pytest.raises(AgentNotFoundError):
        d.record_run_start("x")
    with pytest.raises(AgentNotFoundError):
        d.record_run_end("x", ok=True, duration_ms=1)
    with pytest.raises(AgentNotFoundError):
        d.set_status("x", AgentStatus.OFFLINE)


def test_offline_is_sticky_until_set_status() -> None:
    d = directory_presence()
    d.set_status("a", AgentStatus.OFFLINE)
    d.record_run_start("a")
    assert d.presence("a").status is AgentStatus.OFFLINE
    d.record_run_end("a", ok=True, duration_ms=1)
    assert d.presence("a").status is AgentStatus.OFFLINE

    d.record_run_start("a")
    d.set_status("a", AgentStatus.AVAILABLE)
    assert d.presence("a").status is AgentStatus.BUSY  # automatic tracking resumes
    d.record_run_end("a", ok=True, duration_ms=1)
    assert d.presence("a").status is AgentStatus.AVAILABLE


def test_busy_can_be_pinned() -> None:
    d = directory_presence()
    d.set_status("a", AgentStatus.BUSY)
    d.record_run_start("a")
    d.record_run_end("a", ok=True, duration_ms=1)
    assert d.presence("a").status is AgentStatus.BUSY
    d.set_status("a", AgentStatus.AVAILABLE)
    assert d.presence("a").status is AgentStatus.AVAILABLE


def test_event_subscriber_tracks_successful_runs() -> None:
    d = directory_presence()
    subscriber = d.as_event_subscriber()

    subscriber(started("a", "r1"))
    assert d.presence("a").status is AgentStatus.BUSY
    subscriber(TextDeltaEvent(run_id="r1", root_run_id="r1", agent="a", delta="hi"))
    subscriber(finished("a", "r1", duration_ms=42))

    presence = d.presence("a")
    assert presence.status is AgentStatus.AVAILABLE
    assert presence.total_runs == 1
    assert presence.failed_runs == 0
    assert presence.average_duration_ms == 42.0


def test_event_subscriber_tracks_failed_runs_with_elapsed_time() -> None:
    d = directory_presence()
    subscriber = d.as_event_subscriber()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    subscriber(started("a", "r1", at=t0))
    subscriber(failed("a", "r1", at=t0 + timedelta(milliseconds=250)))

    presence = d.presence("a")
    assert presence.failed_runs == 1
    assert presence.total_runs == 1
    assert presence.average_duration_ms == 250.0
    assert presence.active_runs == 0


def test_event_subscriber_ignores_unknown_agents_and_unmatched_ends() -> None:
    d = directory_presence()
    subscriber = d.as_event_subscriber()

    subscriber(started("stranger", "r1"))
    subscriber(finished("stranger", "r1", duration_ms=1))
    subscriber(finished("a", "never-started", duration_ms=1))
    subscriber(failed("b", "never-started"))
    subscriber(started("a", "r2"))
    subscriber(started("a", "r2"))  # duplicate start of the same run

    assert d.presence("a").active_runs == 1
    assert d.presence("a").total_runs == 0
    assert d.presence("b").total_runs == 0

    d.unregister("a")
    subscriber(finished("a", "r2", duration_ms=1))  # agent gone: ignored


def test_event_subscriber_tracks_concurrent_nested_runs() -> None:
    d = directory_presence()
    subscriber = d.as_event_subscriber()

    subscriber(started("a", "outer"))
    subscriber(started("b", "inner"))
    subscriber(started("a", "second"))
    assert d.presence("a").active_runs == 2
    subscriber(finished("b", "inner", duration_ms=10))
    subscriber(failed("a", "second"))
    subscriber(finished("a", "outer", duration_ms=30))

    assert d.presence("a").active_runs == 0
    assert d.presence("a").total_runs == 2
    assert d.presence("a").failed_runs == 1
    assert d.presence("b").total_runs == 1


async def test_event_subscriber_works_with_run_context_emitter() -> None:
    d = directory_presence()
    ctx: RunContext[None] = RunContext(run_id="root", agent="a", deps=None)
    ctx.events.subscribe(d.as_event_subscriber())

    with use_run_context(ctx):
        await ctx.emit(AgentStartedEvent(**ctx.event_fields()))
        child = ctx.child("b")
        await child.emit(AgentStartedEvent(**child.event_fields()))
        assert d.presence("b").status is AgentStatus.BUSY
        await child.emit(AgentFinishedEvent(**child.event_fields(), duration_ms=5))
        await ctx.emit(AgentFinishedEvent(**ctx.event_fields(), duration_ms=9))

    assert d.presence("a").total_runs == 1
    assert d.presence("b").average_duration_ms == 5.0
    assert d.list(status=AgentStatus.BUSY) == []


def sample_directory_tools() -> AgentDirectory:
    return AgentDirectory(
        [
            AgentCard(
                name="lead",
                description="Coordinates the research team.",
                capabilities=("coordination", "research"),
            ),
            AgentCard(
                name="researcher",
                description="Finds and cites evidence.",
                capabilities=("research",),
                skills=("citations",),
                tools=("web_search",),
                output_schema={"type": "object"},
            ),
            AgentCard(
                name="writer",
                description="Writes clear prose.",
                capabilities=("writing",),
                team="desk",
            ),
            AgentCard(name="auditor", description="Audits research.", capabilities=("research",)),
        ]
    )


def by_name(tools: list[BaseTool]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


async def call(tool: BaseTool, args: dict[str, Any]) -> Any:
    raw = await tool.ainvoke(args)
    assert isinstance(raw, str)
    value = json.loads(raw)
    assert raw == json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return value


def test_tool_names_and_schemas() -> None:
    tools = create_directory_tools(sample_directory_tools())
    assert [tool.name for tool in tools] == ["list_agents", "find_agents", "describe_agent"]
    assert all(tool.description for tool in tools)
    assert set(tools[0].args) == {"capability"}
    assert set(tools[1].args) == {"query", "limit"}
    assert set(tools[2].args) == {"name"}


async def test_list_agents() -> None:
    d = sample_directory_tools()
    d.record_run_start("writer")
    tools = by_name(create_directory_tools(d))

    listing = await call(tools["list_agents"], {})
    assert [agent["name"] for agent in listing["agents"]] == [
        "auditor",
        "lead",
        "researcher",
        "writer",
    ]
    assert listing["agents"][2] == {
        "name": "researcher",
        "description": "Finds and cites evidence.",
        "capabilities": ["research"],
        "skills": ["citations"],
        "status": "available",
    }
    assert listing["agents"][3]["status"] == "busy"

    filtered = await call(tools["list_agents"], {"capability": "writing"})
    assert [agent["name"] for agent in filtered["agents"]] == ["writer"]


async def test_find_agents() -> None:
    tools = by_name(create_directory_tools(sample_directory_tools()))

    result = await call(tools["find_agents"], {"query": "research evidence"})
    names = [agent["name"] for agent in result["agents"]]
    assert names[0] == "researcher"
    assert set(names) == {"researcher", "lead", "auditor"}
    top = result["agents"][0]
    assert top["score"] == 7.0
    assert "capability 'research' matches 'research'" in top["reasons"]
    assert top["status"] == "available"

    limited = await call(tools["find_agents"], {"query": "research", "limit": 1})
    assert [agent["name"] for agent in limited["agents"]] == ["researcher"]

    assert await call(tools["find_agents"], {"query": "astronomy"}) == {"agents": []}


async def test_find_agents_clamps_limit() -> None:
    cards = [AgentCard(name=f"agent{i}", capabilities=("translation",)) for i in range(30)]
    tools = by_name(create_directory_tools(AgentDirectory(cards)))

    many = await call(tools["find_agents"], {"query": "translation", "limit": 500})
    assert len(many["agents"]) == MAX_FIND_LIMIT
    one = await call(tools["find_agents"], {"query": "translation", "limit": 0})
    assert len(one["agents"]) == 1


async def test_describe_agent() -> None:
    d = sample_directory_tools()
    d.record_run_end("researcher", ok=True, duration_ms=12)
    tools = by_name(create_directory_tools(d))

    card = await call(tools["describe_agent"], {"name": "researcher"})
    assert card["name"] == "researcher"
    assert card["tools"] == ["web_search"]
    assert card["output_schema"] == {"type": "object"}
    assert "team" not in card  # None fields are omitted
    assert card["presence"]["total_runs"] == 1
    assert card["presence"]["average_duration_ms"] == 12.0

    missing = await call(tools["describe_agent"], {"name": "ghost"})
    assert missing == {
        "error": "Unknown agent 'ghost'. Available agents: auditor, lead, researcher, writer"
    }


async def test_viewer_never_sees_itself() -> None:
    tools = by_name(create_directory_tools(sample_directory_tools(), viewer="researcher"))

    listing = await call(tools["list_agents"], {})
    assert "researcher" not in [agent["name"] for agent in listing["agents"]]
    found = await call(tools["find_agents"], {"query": "research", "limit": 2})
    # Without the viewer, "researcher" would rank first.
    assert [agent["name"] for agent in found["agents"]] == ["auditor", "lead"]
    own = await call(tools["describe_agent"], {"name": "researcher"})
    assert own["name"] == "researcher"


async def test_visible_names_restrict_every_tool() -> None:
    tools = by_name(
        create_directory_tools(
            sample_directory_tools(), viewer="lead", visible=["researcher", "writer"]
        )
    )

    listing = await call(tools["list_agents"], {})
    assert [agent["name"] for agent in listing["agents"]] == ["researcher", "writer"]
    found = await call(tools["find_agents"], {"query": "research"})
    assert [agent["name"] for agent in found["agents"]] == ["researcher"]
    hidden = await call(tools["describe_agent"], {"name": "auditor"})
    assert hidden == {"error": "Unknown agent 'auditor'. Available agents: researcher, writer"}


async def test_visible_predicate_is_evaluated_per_call() -> None:
    d = sample_directory_tools()
    tools = by_name(create_directory_tools(d, visible=lambda card: card.team == "desk"))

    listing = await call(tools["list_agents"], {})
    assert [agent["name"] for agent in listing["agents"]] == ["writer"]

    d.register(AgentCard(name="editor", description="Edits.", team="desk"))
    d.set_status("editor", AgentStatus.OFFLINE)
    listing = await call(tools["list_agents"], {})
    assert [(a["name"], a["status"]) for a in listing["agents"]] == [
        ("editor", "offline"),
        ("writer", "available"),
    ]


def test_visible_rejects_single_string() -> None:
    with pytest.raises(TypeError, match="single string"):
        create_directory_tools(sample_directory_tools(), visible="writer")


def test_tools_support_sync_invocation() -> None:
    tools = by_name(create_directory_tools(sample_directory_tools()))
    result = json.loads(tools["describe_agent"].invoke({"name": "writer"}))
    assert result["team"] == "desk"
    assert json.loads(tools["find_agents"].invoke({"query": "prose"}))["agents"][0]["name"] == (
        "writer"
    )
    assert json.loads(tools["list_agents"].invoke({}))["agents"]


async def test_non_ascii_content_is_preserved() -> None:
    d = AgentDirectory([AgentCard(name="translator", description="Übersetzt Texte.")])
    raw = await by_name(create_directory_tools(d))["list_agents"].ainvoke({})
    assert "Übersetzt" in raw
