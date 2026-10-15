"""The short forms of a blueprint: agents as a name-to-task mapping, `team: <lead>` and friends."""

from __future__ import annotations

import pytest

from awa import Agent, AgentNetwork, Blueprint, BlueprintError, Sequential, Team
from awa.blueprints import SequentialStep
from awa.testing import FakeChatModel


def test_the_shortest_blueprint_is_a_name_and_one_task() -> None:
    blueprint = Blueprint.from_yaml_text("name: helper\nagents:\n  helper: Answers questions.\n")

    agent = blueprint.build()

    assert blueprint.check() == []
    assert isinstance(agent, Agent) and agent.description == "Answers questions."
    with agent.override(model=FakeChatModel(["hi"])):
        assert agent.run_sync("hello").output == "hi"


def test_a_task_is_also_the_instructions_when_none_are_given() -> None:
    blueprint = Blueprint.from_yaml_text(
        "name: desk\nagents:\n  helper: Answers questions.\n"
        "  writer: {description: Writes replies., instructions: Be brief.}\nworkflow: [helper, writer]\n"
    )
    model = FakeChatModel(["a", "b"])

    pipeline = blueprint.build()
    assert isinstance(pipeline, Sequential)
    helper, writer = pipeline.steps
    assert isinstance(helper, Agent) and isinstance(writer, Agent)
    with helper.override(model=model), writer.override(model=model):
        pipeline.run_sync("go")

    prompts = [str(call.messages[0].content) for call in model.calls]
    # A task without instructions is the role line; instructions get their own section.
    assert "Answers questions." in prompts[0].split("\n")[1]
    assert "## Instructions\nBe brief." in prompts[1]


def test_team_names_the_orchestrator_and_everyone_else_joins() -> None:
    blueprint = Blueprint.from_yaml_text(
        "name: desk\nagents:\n  lead: Coordinates.\n  a: Does a.\n  b: Does b.\nteam: lead\n"
    )

    assert blueprint.team is not None
    assert blueprint.team.orchestrator == "lead" and blueprint.team.members == ["a", "b"]
    assert isinstance(blueprint.build(), Team)
    partial = Blueprint.from_dict(
        {
            "name": "desk",
            "agents": {"lead": "Coordinates.", "a": "Does a.", "b": "Does b."},
            "team": {"orchestrator": "lead", "task_board": True},
        }
    )
    assert partial.team is not None and partial.team.members == ["a", "b"]
    assert partial.team.task_board


def test_network_names_the_lead_and_workflow_lists_steps() -> None:
    network = Blueprint.from_dict(
        {
            "name": "scan",
            "agents": {
                "lead": "Plans.",
                "pricing": {"description": "Compares prices.", "capabilities": ["pricing"]},
            },
            "network": "lead",
        }
    )
    workflow = Blueprint.from_dict(
        {"name": "flow", "agents": {"a": "Does a.", "b": "Does b."}, "workflow": ["a", "b"]}
    )

    assert network.network is not None and network.network.teammates == ["pricing"]
    assert isinstance(network.build(), AgentNetwork)
    assert isinstance(workflow.workflow, SequentialStep)
    assert workflow.workflow.sequential == ["a", "b"]


def test_network_short_form_gives_teammates_their_name_as_capability() -> None:
    blueprint = Blueprint.from_yaml_text(
        "name: scan\n"
        "agents:\n"
        "  lead: Plans the price scan.\n"
        "  pricing: Compares prices.\n"
        "  sourcing: {description: Finds suppliers., capabilities: [suppliers, sourcing]}\n"
        "  reviewer: {description: Reviews., capabilities: []}\n"
        "network: lead\n"
    )

    capabilities = {agent.name: agent.capabilities for agent in blueprint.agents}
    assert capabilities == {
        "lead": [],
        "pricing": ["pricing"],
        "sourcing": ["suppliers", "sourcing"],
        "reviewer": [],
    }
    # Only the teammate that explicitly has none is reported.
    warnings = [issue.path for issue in blueprint.check() if issue.severity == "warning"]
    assert warnings == ["network.teammates[2]"]
    assert Blueprint.from_yaml_text(blueprint.to_yaml()) == blueprint

    clean = Blueprint.from_yaml_text(
        "name: scan\nagents:\n  lead: Plans.\n  pricing: Compares prices.\nnetwork: lead\n"
    )
    assert clean.check() == []


def test_short_forms_round_trip_through_the_long_form() -> None:
    short = Blueprint.from_yaml_text(
        "name: desk\nauto: true\nagents:\n  lead: Coordinates.\n  a: Does a.\nteam: lead\n"
    )

    again = Blueprint.from_yaml_text(short.to_yaml())

    assert again == short and again.auto


@pytest.mark.parametrize(
    ("agents", "message"),
    [
        ({"a": {"name": "b", "description": "t"}}, "sets a different name"),
        ({"a": {"task": "t"}}, "task"),
        ({"a": 3}, "agents"),
    ],
)
def test_malformed_short_forms_are_blueprint_errors(agents: object, message: str) -> None:
    with pytest.raises(BlueprintError, match=message):
        Blueprint.from_dict({"name": "x", "agents": agents})


def test_an_agent_without_a_task_is_allowed() -> None:
    blueprint = Blueprint.from_yaml_text("name: x\nagents:\n  x:\n")

    assert blueprint.agents[0].name == "x" and blueprint.agents[0].description == ""
