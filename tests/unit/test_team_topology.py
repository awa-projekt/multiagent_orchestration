"""Team topology, limits and messages between members."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel, ValidationError

from awa.agents import Agent
from awa.contracts import (
    AgentLike,
    AgentMessage,
    AgentMessageEvent,
    MessageUndeliveredEvent,
    RunEvent,
)
from awa.directory import AgentDirectory
from awa.errors import (
    AgentNotFoundError,
    AgentRunError,
    CommunicationNotAllowedError,
    TopologyError,
)
from awa.messaging import Blackboard, InMemoryMessageBus
from awa.tasks import BaseTaskBoard, TaskBoard
from awa.team import Team, TeamLimits, is_team_note
from awa.team._scoped import RunScope, ScopedBus, ScopedTaskBoard
from awa.team.limits import NetworkLimits
from awa.testing import FakeChatModel, tool_call
from awa.workflows import Sequential


def agent(name: str, **kwargs: Any) -> Agent:
    kwargs.setdefault("model", FakeChatModel(["ok"], repeat_last=True))
    kwargs.setdefault("description", f"The {name} agent.")
    return Agent(name, **kwargs)


def tool_names(team: Team, name: str) -> list[str]:
    runtime = team.agent(name)
    assert isinstance(runtime, Agent)
    return [tool.name for tool in runtime.tools]


def literal_values(team: Team, name: str, tool: str, field: str) -> list[str]:
    runtime = team.agent(name)
    assert isinstance(runtime, Agent)
    [found] = [t for t in runtime.tools if t.name == tool]
    schema = found.tool_call_schema.model_json_schema()["properties"][field]
    return sorted(schema["enum"]) if "enum" in schema else [schema["const"]]


def desk(**kwargs: Any) -> Team:
    return Team(
        "desk",
        orchestrator=agent("lead"),
        members=[agent("researcher", capabilities=["research"]), agent("writer")],
        **kwargs,
    )


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"delegates": {"ghost": ["writer"]}}, "delegates lists unknown agent 'ghost'"),
        ({"delegates": {"lead": ["ghost"]}}, "delegates lists unknown agent 'ghost'"),
        ({"delegates": {"lead": ["lead"]}}, "'lead' -> 'lead' points to itself"),
        ({"delegates": {"researcher": ["lead"]}}, "targets the orchestrator"),
        ({"delegates": {"lead": "writer"}}, "must be a sequence of agent names"),
        ({"peers": {"researcher": ["researcher"]}}, "points to itself"),
        ({"peers": {"researcher": ["lead"]}}, "'lead' is the orchestrator"),
        ({"peers": {"researcher": ["ghost"]}}, "peers lists unknown agent 'ghost'"),
        ({"handoffs": {"lead": ["ghost"]}}, "handoffs lists unknown agent 'ghost'"),
        ({"handoffs": {"writer": ["writer"]}}, "points to itself"),
    ],
)
def test_invalid_edges_raise_precise_topology_errors(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(TopologyError, match=message):
        desk(**kwargs)


def test_names_must_be_unique_including_nested_teams() -> None:
    with pytest.raises(TopologyError, match="'writer' is used more than once"):
        Team("desk", orchestrator=agent("lead"), members=[agent("writer"), agent("writer")])

    inner = Team("research", orchestrator=agent("scout"), members=[agent("writer")])
    with pytest.raises(TopologyError, match="'writer' is used more than once"):
        Team("desk", orchestrator=agent("lead"), members=[inner, agent("writer")])

    with pytest.raises(TopologyError, match="'desk' is used more than once"):
        Team("desk", orchestrator=agent("lead"), members=[agent("desk")])


def test_orchestrator_must_not_be_a_member_and_members_are_required() -> None:
    lead = agent("lead")
    with pytest.raises(TopologyError, match="must not also be listed as a member"):
        Team("desk", orchestrator=lead, members=[lead, agent("writer")])
    with pytest.raises(TopologyError, match="needs at least one member"):
        Team("desk", orchestrator=lead, members=[])


def test_non_agent_members_are_delegate_only() -> None:
    pipeline = Sequential("pipeline", [agent("step_a")])
    members = [pipeline, agent("writer")]

    with pytest.raises(TopologyError, match="'pipeline' is a workflow and cannot have outgoing"):
        Team(
            "desk", orchestrator=agent("lead"), members=members, delegates={"pipeline": ["writer"]}
        )
    with pytest.raises(TopologyError, match="peers must be Agents"):
        Team("desk", orchestrator=agent("lead"), members=members, peers={"writer": ["pipeline"]})
    with pytest.raises(TopologyError, match="handoff targets must be Agents"):
        Team("desk", orchestrator=agent("lead"), members=members, handoffs={"lead": ["pipeline"]})


def test_handoff_sources_cannot_declare_structured_output() -> None:
    class Answer(BaseModel):
        text: str

    with pytest.raises(TopologyError, match="declares an output_type and cannot hand off"):
        Team(
            "desk",
            orchestrator=agent("lead", output_type=Answer),
            members=[agent("writer")],
            handoffs={"lead": ["writer"]},
        )


def test_constructor_argument_errors() -> None:
    with pytest.raises(TypeError, match="orchestrator must be an Agent"):
        Team("desk", orchestrator=object(), members=[agent("writer")])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be AgentLike"):
        Team("desk", orchestrator=agent("lead"), members=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="handoff_history"):
        desk(handoff_history="everything")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="resume_with"):
        desk(resume_with="first")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="messaging=True"):
        desk(messaging=False, bus=InMemoryMessageBus())
    with pytest.raises(ValueError, match="Invalid agent name"):
        Team("bad name", orchestrator=agent("lead"), members=[agent("writer")])


def test_team_tool_clashing_with_an_agent_tool_is_a_topology_error() -> None:
    def delegate_task(task: str) -> str:
        """A user tool with a reserved name."""
        return task

    with pytest.raises(TopologyError, match="cannot add team tools to 'lead'"):
        Team("desk", orchestrator=agent("lead", tools=[delegate_task]), members=[agent("writer")])


# --------------------------------------------------------------------------- wiring


def test_tools_are_injected_exactly_per_role() -> None:
    team = desk(
        peers={"researcher": ["writer"]},
        handoffs={"lead": ["writer"], "writer": ["lead"]},
        blackboard=True,
    )

    shared = ["read_shared", "write_shared", "list_shared"]
    background = ["check_delegations", "wait_for_delegations", "cancel_delegation"]
    assert tool_names(team, "lead") == ["delegate_task", *background, "transfer_to_writer", *shared]
    # Peers ask with consult_peer; send_message only leaves one-way notes.
    assert tool_names(team, "researcher") == ["consult_peer", "send_message", *shared]
    assert tool_names(team, "writer") == [
        "consult_peer",
        "send_message",
        "transfer_to_lead",
        *shared,
    ]
    researcher = team.agent("researcher")
    assert isinstance(researcher, Agent)
    [send] = [t for t in researcher.tools if t.name == "send_message"]
    assert "expect_reply" not in send.args and "consult_peer" in send.description
    assert literal_values(team, "lead", "delegate_task", "agent") == ["researcher", "writer"]
    assert literal_values(team, "writer", "consult_peer", "peer") == ["researcher"]
    assert literal_values(team, "researcher", "send_message", "to") == ["writer"]


def test_user_agents_are_not_modified() -> None:
    lead = agent("lead", instructions="Delegate.")
    Team("desk", orchestrator=lead, members=[agent("writer")])

    assert lead.tools == ()
    assert lead.card.tools == ()


def test_directory_tools_only_for_agents_reaching_at_least_three_agents() -> None:
    small = desk()
    assert "find_agents" not in tool_names(small, "lead")

    team = Team("desk", orchestrator=agent("lead"), members=[agent("a"), agent("b"), agent("c")])
    assert tool_names(team, "lead")[-3:] == ["list_agents", "find_agents", "describe_agent"]


async def test_directory_tools_are_scoped_to_reachable_agents() -> None:
    team = Team(
        "desk",
        orchestrator=agent("lead"),
        members=[agent("a"), agent("b"), agent("c"), agent("d")],
        delegates={"lead": ["a", "b", "c"]},
    )
    lead = team.agent("lead")
    assert isinstance(lead, Agent)
    [list_agents] = [t for t in lead.tools if t.name == "list_agents"]

    listing = await list_agents.ainvoke({})

    assert '"a"' in listing and '"c"' in listing
    assert '"d"' not in listing and '"lead"' not in listing


def test_messaging_off_removes_messaging_tools_and_bus() -> None:
    team = desk(peers={"researcher": ["writer"]}, messaging=False)

    assert team.bus is None
    assert tool_names(team, "researcher") == ["consult_peer"]


def test_asymmetric_peers() -> None:
    team = desk(peers={"researcher": ["writer"]}, symmetric_peers=False)

    assert team.topology()["peers"] == {"researcher": ["writer"]}
    assert "consult_peer" not in tool_names(team, "writer")


def test_task_board_tools_by_role() -> None:
    board = TaskBoard()
    team = desk(task_board=board)

    assert team.task_board is board
    assert isinstance(desk(task_board=True).task_board, BaseTaskBoard)
    assert desk().task_board is None
    lead_tools = set(tool_names(team, "lead"))
    assert {"create_task", "create_tasks", "cancel_task", "list_tasks"} <= lead_tools
    # Members do not claim on their own: nothing to assign, release or wait for.
    assert not {"wait_for_tasks", "release_task", "claim_task"} & lead_tools
    lead = team.agent("lead")
    assert isinstance(lead, Agent)
    [create] = [t for t in lead.tools if t.name == "create_task"]
    assert "owner" not in create.args
    assert "complete_task" in tool_names(team, "writer")
    assert "wait_for_tasks" not in tool_names(team, "writer")


def test_team_instructions_are_appended_and_data_driven() -> None:
    team = desk(peers={"researcher": ["writer"]}, handoffs={"lead": ["writer"]})

    lead = team.agent("lead")
    researcher = team.agent("researcher")
    assert isinstance(lead, Agent) and isinstance(researcher, Agent)
    lead_prompt = lead._static_prompt or ""
    assert 'You coordinate the team "desk".' in lead_prompt
    assert "- **researcher**: The researcher agent. (capabilities: research)" in lead_prompt
    assert "`transfer_to_<name>`" in lead_prompt
    member_prompt = researcher._static_prompt or ""
    assert 'You are "researcher", a member of the team "desk"' in member_prompt
    assert "Peers you can ask with `consult_peer`" in member_prompt
    assert "`send_message` only leaves a peer a one-way note" in member_prompt
    assert "delegate_task" not in member_prompt


def test_properties_card_and_agent_like() -> None:
    board = Blackboard()
    directory = AgentDirectory()
    team = desk(blackboard=board, directory=directory, capabilities=["writing"], tags=["demo"])

    assert isinstance(team, AgentLike)
    assert team.blackboard is board and team.directory is directory
    assert team.limits == TeamLimits()
    assert team.card.role == "team"
    assert team.card.capabilities == ("writing", "research")
    assert team.card.tags == ("demo",)
    assert "Team coordinated by lead" in team.description
    assert [m.name for m in team.members] == ["researcher", "writer"]
    assert team.orchestrator.name == "lead"
    assert directory.names() == ["lead", "researcher", "writer"]
    assert directory.resolve("lead") is team.agent("lead")
    assert repr(team) == "Team('desk', orchestrator='lead', members=[researcher, writer])"
    with pytest.raises(AgentNotFoundError, match="has no agent 'ghost'"):
        team.agent("ghost")


def test_registering_into_a_shared_directory() -> None:
    writer = agent("writer")
    directory = AgentDirectory([writer])
    Team("desk", orchestrator=agent("lead"), members=[writer], directory=directory)

    assert directory.resolve("writer") is not writer
    assert directory.names() == ["lead", "writer"]


async def test_team_tools_outside_a_team_run_explain_themselves() -> None:
    team = desk()
    lead = team.agent("lead")
    assert isinstance(lead, Agent)
    [delegate] = [t for t in lead.tools if t.name == "delegate_task"]

    with pytest.raises(AgentRunError, match="only available while team 'desk' is running"):
        await delegate.ainvoke({"agent": "writer", "task": "x"})


# --------------------------------------------------------------------------- topology views


def test_topology_defaults_and_symmetric_peers() -> None:
    team = desk(peers={"researcher": ["writer"]}, handoffs={"lead": ["writer"]})

    assert team.topology() == {
        "delegates": {"lead": ["researcher", "writer"]},
        "peers": {"researcher": ["writer"], "writer": ["researcher"]},
        "handoffs": {"lead": ["writer"]},
    }


def test_mermaid_snapshot_with_nested_team_and_workflow() -> None:
    inner = Team(
        "research",
        orchestrator=agent("scout"),
        members=[agent("reader"), agent("analyst")],
        peers={"reader": ["analyst"]},
    )
    pipeline = Sequential("pipeline", [agent("formatter")])
    team = Team(
        "desk",
        orchestrator=agent("lead"),
        members=[inner, agent("writer"), agent("editor"), pipeline],
        peers={"writer": ["editor"]},
        symmetric_peers=False,
        handoffs={"lead": ["writer"]},
    )

    assert team.to_mermaid() == "\n".join(
        [
            "flowchart TD",
            '    n0(["lead"])',
            '    subgraph n1["research (team)"]',
            '        n2(["scout"])',
            '        n3["reader"]',
            '        n4["analyst"]',
            "        n2 --> n3",
            "        n2 --> n4",
            "        n3 <-.-> n4",
            "    end",
            '    n5["writer"]',
            '    n6["editor"]',
            '    n7[["pipeline"]]',
            "    n0 --> n1",
            "    n0 --> n5",
            "    n0 --> n6",
            "    n0 --> n7",
            "    n5 -.-> n6",
            "    n0 ==>|handoff| n5",
        ]
    )


def test_eight_specialists_in_a_ring_with_a_four_level_chain() -> None:
    specialists = [f"specialist_{i}" for i in range(8)]
    chain = ["tier1", "tier2", "tier3"]
    peers = {name: [specialists[(i + 1) % 8]] for i, name in enumerate(specialists)}
    delegates = {
        "lead": specialists,
        "specialist_0": ["tier1"],
        "tier1": ["tier2"],
        "tier2": ["tier3"],
    }

    team = Team(
        "desk",
        orchestrator=agent("lead"),
        members=[agent(name) for name in (*specialists, *chain)],
        delegates=delegates,
        peers=peers,
    )

    topology = team.topology()
    assert topology["delegates"]["lead"] == specialists
    assert sorted(topology["peers"]["specialist_3"]) == ["specialist_2", "specialist_4"]
    assert literal_values(team, "tier2", "delegate_task", "agent") == ["tier3"]
    assert "delegate_task" not in tool_names(team, "tier3")


@pytest.mark.parametrize("limits", [TeamLimits, NetworkLimits])
def test_limits_reject_unknown_options(limits: type[TeamLimits | NetworkLimits]) -> None:
    with pytest.raises(ValidationError, match="parallel_overflow"):
        limits(parallel_overflow="queue")  # type: ignore[call-arg]


def delegate(agent: str, task: str = "do it") -> Any:
    return tool_call("delegate_task", {"agent": agent, "task": task})


def member(name: str, *responses: Any) -> Agent:
    return Agent(
        name, description=f"The {name} agent.", model=FakeChatModel(list(responses) or ["ok"])
    )


def model_of(agent: Agent) -> FakeChatModel:
    model = agent._model
    assert isinstance(model, FakeChatModel)
    return model


def team_with(members: list[Agent], *lead_script: Any, **kwargs: Any) -> Team:
    return Team(
        "desk",
        orchestrator=Agent("lead", model=FakeChatModel(list(lead_script))),
        members=members,
        **kwargs,
    )


def send(to: str, content: str, **extra: Any) -> Any:
    return tool_call("send_message", {"to": to, "subject": "note", "content": content, **extra})


async def test_messages_along_peer_edges_are_delivered_automatically() -> None:
    researcher = member("researcher", send("writer", "Source: RFC 42."), "researched")
    writer = member("writer", "written")
    events: list[RunEvent] = []
    team = team_with(
        [researcher, writer],
        delegate("researcher"),
        delegate("writer"),
        "done",
        peers={"researcher": ["writer"]},
        hooks=[events.append],
    )

    await team.run("go")

    first_call = model_of(writer).calls[0].messages
    [delivery] = [m for m in first_call if isinstance(m, HumanMessage) and is_team_note(m)]
    assert first_call[-1] is delivery and first_call[-2].content == "Task from lead:\ndo it"
    assert "<agent_message" in str(delivery.content)
    assert '"from": "researcher"' in str(delivery.content)
    assert "Source: RFC 42." in str(delivery.content)
    assert team.bus is not None and team.bus.pending_count("writer") == 0
    [message_event] = [e for e in events if isinstance(e, AgentMessageEvent)]
    assert message_event.agent == "researcher"
    assert message_event.message.recipient == "writer"


async def test_messages_are_delivered_once() -> None:
    researcher = member("researcher", send("writer", "hello"), "researched")
    writer = Agent("writer", model=FakeChatModel([send("researcher", "thanks"), "written"]))
    team = team_with(
        [researcher, writer],
        delegate("researcher"),
        delegate("writer"),
        "done",
        peers={"researcher": ["writer"]},
    )

    await team.run("go")

    calls = model_of(writer).calls
    notes = [m for m in calls[1].messages if isinstance(m, HumanMessage) and is_team_note(m)]
    assert len(notes) == 1


async def test_peers_ask_with_consult_peer_and_send_only_one_way_notes() -> None:
    # Seen in the audit: a request sent with send_message(expect_reply=True) stalled,
    # because a delegated member never ran again to read and answer it.
    researcher = member("researcher", send("writer", "FYI: RFC 42."), "researched")
    team = team_with(
        [researcher, member("writer")],
        delegate("researcher"),
        "done",
        peers={"researcher": ["writer"]},
    )
    runtime = team.agent("researcher")
    assert isinstance(runtime, Agent)
    tools = {t.name: t for t in runtime.tools}
    assert "read_inbox" not in tools and "reply_to_message" not in tools
    assert "expect_reply" not in tools["send_message"].args

    events: list[RunEvent] = []
    team = team_with(
        [member("researcher", send("writer", "FYI: RFC 42."), "researched"), member("writer")],
        delegate("researcher"),
        "done",
        peers={"researcher": ["writer"]},
        hooks=[events.append],
    )
    result = await team.run("go")

    # The writer never ran again: its unread note is reported, not silently lost.
    [undelivered] = [e for e in events if isinstance(e, MessageUndeliveredEvent)]
    assert undelivered.recipient == "writer" and undelivered.reason == "run_ended"
    assert undelivered.message.content == "FYI: RFC 42."
    assert undelivered.message.run_id == result.run_id
    assert team.bus is not None and team.bus.pending_count("writer") == 0
    [letter] = team.bus.dead_letters
    assert letter.reason == "run_ended"


async def test_policy_denies_non_edges() -> None:
    researcher = member("researcher")
    team = team_with(
        [researcher, member("writer"), member("editor")],
        "done",
        peers={"researcher": ["writer"], "editor": ["writer"]},
    )
    assert team.bus is not None

    with pytest.raises(CommunicationNotAllowedError):
        await team.bus.send(AgentMessage(sender="researcher", recipient="editor", content="hi"))
    runtime = team.agent("researcher")
    assert isinstance(runtime, Agent)
    [send_tool] = [t for t in runtime.tools if t.name == "send_message"]
    with pytest.raises(ValidationError):
        await send_tool.ainvoke({"to": "editor", "subject": "s", "content": "c"})


async def test_a_shared_bus_gets_mailboxes_registered(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("writer")
    team = team_with(
        [member("researcher"), member("writer")], "done", peers={"researcher": ["writer"]}, bus=bus
    )

    assert team.bus is bus
    assert bus.mailboxes() == ["researcher", "writer"]


async def test_blackboard_is_shared_across_agents(durable: Any) -> None:
    researcher = member(
        "researcher",
        tool_call("write_shared", {"key": "facts", "value": '{"claim": "BM25 + vectors"}'}),
        "stored",
    )
    writer = member("writer", tool_call("read_shared", {"key": "facts"}), "written")
    team = team_with(
        [researcher, writer],
        delegate("researcher"),
        delegate("writer"),
        "done",
        blackboard=await durable.blackboard(),
    )

    await team.run("go")

    read = model_of(writer).calls[1].messages[-1]
    assert isinstance(read, ToolMessage)
    assert '"author":"researcher"' in str(read.content)
    assert "BM25 + vectors" in str(read.content)
    assert team.blackboard is not None
    assert await team.blackboard.read("facts") == {"claim": "BM25 + vectors"}


async def test_task_board_flow_through_delegation(durable: Any) -> None:
    writer = member(
        "writer",
        tool_call("claim_task", {}),
        tool_call("complete_task", {"task_id": "task_1", "result": "Draft done."}),
        "claimed and completed",
    )
    team = team_with(
        [writer, member("editor")],
        tool_call("create_task", {"title": "Write the draft", "owner": "writer"}),
        delegate("writer", "Work on your task."),
        "all tasks done",
        task_board=await durable.task_board(),
    )

    result = await team.run("go")

    assert result.output == "all tasks done"
    assert team.task_board is not None
    task = team.task_board.get("task_1")
    assert task.status == "completed" and task.owner == "writer"
    assert task.result == "Draft done."


async def test_scoped_resources_act_on_the_run_started_last() -> None:
    scope = RunScope("k", bus=InMemoryMessageBus, blackboard=None, task_board=True)
    bus, board = ScopedBus(scope), ScopedTaskBoard(scope)
    first = scope.start()
    await board.create("first task", created_by="a")
    bus.register("a")
    second = scope.start()

    assert first.task_board is not None and len(first.task_board) == 1
    assert second.task_board is not None and len(second.task_board) == 0
    assert len(board) == 0
    assert bus.mailboxes() == []
    assert first.bus is not None and first.bus.mailboxes() == ["a"]
