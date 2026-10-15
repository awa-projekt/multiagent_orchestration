"""Building blueprints into agents, workflows and teams, also with a given task board, blackboard or bus."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from awa.agents import Agent
from awa.blueprints import Blueprint, BlueprintError, load
from awa.contracts.results import RunResult
from awa.messaging import Blackboard, InMemoryMessageBus
from awa.models import ModelRegistry
from awa.tasks import TaskBoard
from awa.team import Team
from awa.testing import FakeChatModel, ModelCall, structured_output, tool_call
from awa.workflows import Loop, Parallel, Sequential

CALLS: list[str] = []


@tool
def lookup(order_id: str) -> str:
    """Look up an order."""

    CALLS.append(f"lookup:{order_id}")
    return f"order {order_id}: shipped"


@tool
def refund(order_id: str, amount: float) -> str:
    """Refund an order."""

    CALLS.append(f"refund:{order_id}:{amount}")
    return f"refunded {amount} for {order_id}"


def imported_tool(text: str) -> str:
    """Echo text (imported by reference)."""

    return text.upper()


def large_refunds_only(args: dict[str, Any]) -> bool:
    return float(args.get("amount", 0)) > 100


def says_done(result: RunResult[Any]) -> bool:
    return "done" in result.text


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    CALLS.clear()


def registry(**profiles: Any) -> ModelRegistry:
    models = ModelRegistry()
    for name, model in profiles.items():
        models.register(name, model)
    return models


def single(**agent: Any) -> Blueprint:
    return Blueprint.from_dict({"name": "solo", "agents": [{"name": "clerk", **agent}]})


async def test_single_agent_with_tools_runs() -> None:
    bp = single(description="Looks up orders.", instructions="Be brief.", tools=["lookup"])
    model = FakeChatModel([tool_call("lookup", {"order_id": "7"}), "Order 7 shipped."])

    agent = bp.build(tools=[lookup], model_registry=registry(default=model))
    result = await agent.run("Where is order 7?")

    assert isinstance(agent, Agent)
    assert agent.card.description == "Looks up orders."
    assert result.output == "Order 7 shipped."
    assert CALLS == ["lookup:7"]
    assert model.calls[0].tool_names == ("lookup",)


async def test_output_schema_becomes_structured_output() -> None:
    bp = single(
        output_schema={
            "type": "object",
            "properties": {"status": {"enum": ["open", "closed"]}},
            "required": ["status"],
        }
    )
    agent = bp.build()
    assert isinstance(agent, Agent)
    assert agent.output_type is not None
    assert agent.output_type.__name__ == "ClerkOutput"

    with agent.override(
        model=FakeChatModel([structured_output(agent.output_type, {"status": "open"})])
    ):
        result = await agent.run("Status?")

    assert result.output.model_dump() == {"status": "open"}


def test_agent_settings_come_from_defaults_and_overrides() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "x",
            "version": "2.0.0",
            "defaults": {"model": "fast", "max_steps": 7, "skill_mode": "on_demand"},
            "agents": [
                {"name": "a", "skills": ["writing"], "capabilities": ["c"], "tags": ["t"]},
                {"name": "b", "model": "strong", "max_steps": 3},
            ],
            "workflow": {"sequential": ["a", "b"]},
        }
    )

    workflow = bp.build()

    assert isinstance(workflow, Sequential)
    first, second = workflow.steps
    assert isinstance(first, Agent)
    assert isinstance(second, Agent)
    assert first.card.model == "fast"
    assert first.card.skills == ("writing",)
    assert first.card.capabilities == ("c",)
    assert first.card.version == "2.0.0"
    assert "load_skill" in [t.name for t in first.tools]
    assert second.card.model == "strong"
    assert second._max_steps == 3


def test_models_registry_resolves_profiles_and_leaves_other_references_lazy() -> None:
    fake = FakeChatModel(["hi"])
    bp = Blueprint.from_dict(
        {
            "name": "x",
            "defaults": {"model_settings": {"temperature": 0}},
            "agents": [
                {"name": "a", "model": "mine"},
                {"name": "b", "model": "provider:some-model"},
            ],
            "workflow": {"sequential": ["a", "b"]},
        }
    )

    workflow = bp.build(model_registry=registry(mine=fake))

    assert isinstance(workflow, Sequential)
    first, second = workflow.steps
    assert isinstance(first, Agent)
    assert isinstance(second, Agent)
    assert first._model is fake
    assert second._model == "provider:some-model"


def test_unresolvable_model_profile_raises_blueprint_error() -> None:
    def broken() -> Any:
        raise RuntimeError("no credentials")

    with pytest.raises(BlueprintError, match="Cannot resolve the model of agent 'clerk'"):
        single(model="broken").build(model_registry=registry(broken=broken))


def test_build_raises_with_every_check_error() -> None:
    bp = single(tools=["lookup", "missing"], skills=["nope"])

    with pytest.raises(BlueprintError) as caught:
        bp.build(tools=[lookup])

    paths = [issue.path for issue in caught.value.issues]
    assert paths == ["agents[0].tools[1]", "agents[0].skills[0]"]


def test_tool_names_without_a_catalog_are_unknown_at_build() -> None:
    with pytest.raises(BlueprintError, match="unknown tool 'lookup'"):
        single(tools=["lookup"]).build()


def test_tool_sources_accept_mappings_and_functions() -> None:
    def shout(text: str) -> str:
        """Shout the text."""

        return text.upper()

    agent = single(tools=["shout", "find"]).build(tools={"shout": shout, "find": lookup})

    assert isinstance(agent, Agent)
    assert [t.name for t in agent.tools] == ["shout", "lookup"]


def test_undocumented_function_is_not_a_tool() -> None:
    def bare(text: str) -> str:
        return text

    with pytest.raises(BlueprintError, match="docstring"):
        single(tools=["bare"]).build(tools={"bare": bare})


def test_import_references_need_allow_imports() -> None:
    bp = single(tools=[f"{__name__}:imported_tool"])

    with pytest.raises(BlueprintError, match="allow_imports=True"):
        bp.build()

    agent = bp.build(allow_imports=True)
    assert isinstance(agent, Agent)
    assert [t.name for t in agent.tools] == ["imported_tool"]


def test_unimportable_reference_raises_blueprint_error() -> None:
    with pytest.raises(BlueprintError, match="Cannot import"):
        single(tools=[f"{__name__}:missing"]).build(allow_imports=True)
    with pytest.raises(BlueprintError, match="not a tool or a function"):
        single(tools=[f"{__name__}:CALLS"]).build(allow_imports=True)


def test_load_reads_and_builds(tmp_path: Path) -> None:
    path = tmp_path / "solo.yaml"
    path.write_text(single(tools=["lookup"]).to_yaml(), encoding="utf-8")

    agent = load(path, tools=[lookup])

    assert isinstance(agent, Agent)
    assert agent.name == "clerk"


async def test_approval_pauses_until_a_human_approves() -> None:
    model = FakeChatModel([tool_call("refund", {"order_id": "7", "amount": 5}), "Refunded."])
    agent = approval_blueprint().build(
        tools=[refund], model_registry=registry(default=model), checkpointer=InMemorySaver()
    )
    assert isinstance(agent, Agent)

    paused = await agent.run("Refund order 7.")

    assert paused.is_interrupted
    assert paused.interrupts[0]["value"]["tool"] == "refund"
    assert CALLS == []
    assert paused.thread_id is not None

    resumed = await agent.resume(paused.thread_id, {"action": "approve"})

    assert resumed.output == "Refunded."
    assert CALLS == ["refund:7:5.0"]


def approval_blueprint(when: str | None = None) -> Blueprint:
    approval: dict[str, Any] = {"tool": "refund"}
    if when is not None:
        approval["when"] = when
    return Blueprint.from_dict(
        {"name": "x", "agents": [{"name": "clerk", "tools": ["refund"]}], "approvals": [approval]}
    )


def test_approvals_require_a_checkpointer() -> None:
    with pytest.raises(BlueprintError, match="pass a checkpointer"):
        approval_blueprint().build(tools=[refund])


async def test_approval_predicate_limits_which_calls_pause() -> None:
    bp = approval_blueprint(f"{__name__}:large_refunds_only")
    model = FakeChatModel([tool_call("refund", {"order_id": "1", "amount": 5}), "Done."])

    with pytest.raises(BlueprintError, match="allow_imports=True"):
        bp.build(tools=[refund], checkpointer=InMemorySaver())

    agent = bp.build(
        tools=[refund],
        model_registry=registry(default=model),
        checkpointer=InMemorySaver(),
        allow_imports=True,
    )
    result = await agent.run("Refund 5.")

    assert not result.is_interrupted
    assert CALLS == ["refund:1:5.0"]


async def test_rejected_approval_does_not_run_the_tool() -> None:
    model = FakeChatModel([tool_call("refund", {"order_id": "7", "amount": 500}), "Not refunded."])
    agent = approval_blueprint().build(
        tools=[refund], model_registry=registry(default=model), checkpointer=InMemorySaver()
    )
    assert isinstance(agent, Agent)

    paused = await agent.run("Refund 500.")
    assert paused.thread_id is not None
    resumed = await agent.resume(paused.thread_id, {"action": "reject", "note": "too much"})

    assert resumed.output == "Not refunded."
    assert CALLS == []


def workflow_blueprint(workflow: Any, **extra: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "pipeline",
            "description": "Reviews drafts.",
            "agents": [
                {"name": "writer", "model": "writer"},
                {"name": "style", "model": "style"},
                {"name": "facts", "model": "facts"},
                {"name": "editor", "model": "editor"},
            ],
            "workflow": workflow,
            **extra,
        }
    )


async def test_sequential_and_parallel_workflow_runs_every_step() -> None:
    bp = workflow_blueprint(
        {
            "sequential": [
                "writer",
                {"parallel": ["style", "facts"], "fail_fast": False},
                "editor",
            ],
            "carry": "all",
        }
    )
    models = registry(
        writer=FakeChatModel(["draft"]),
        style=FakeChatModel(["style ok"]),
        facts=FakeChatModel(["facts ok"]),
        editor=FakeChatModel(["final"]),
    )

    workflow = bp.build(model_registry=models)
    result = await workflow.run("Write about rivers.")

    assert isinstance(workflow, Sequential)
    assert workflow.name == "pipeline"
    assert workflow.description == "Reviews drafts."
    parallel = workflow.steps[1]
    assert isinstance(parallel, Parallel)
    assert parallel.name == "parallel-1"
    assert result.output == "final"
    editor_prompt = models.get("editor").calls[0].last_message.content  # type: ignore[attr-defined]
    assert "[writer]: draft" in editor_prompt
    assert "style ok" in editor_prompt


async def test_loop_stops_on_marker() -> None:
    bp = workflow_blueprint(
        {"loop": "editor", "max_iterations": 5, "until_contains": "APPROVED", "name": "polish"}
    )
    editor = FakeChatModel(["try again", "APPROVED"])

    workflow = bp.build(model_registry=registry(editor=editor))
    result = await workflow.run("Polish.")

    assert isinstance(workflow, Loop)
    assert workflow.name == "polish"
    assert result.metadata == {"iterations": 2, "stopped_reason": "condition"}
    assert repr(workflow._until) == "until_contains('APPROVED')"


async def test_loop_imported_condition_and_default_condition() -> None:
    imported = workflow_blueprint(
        {"loop": "editor", "max_iterations": 4, "until": f"{__name__}:says_done"}
    )
    with pytest.raises(BlueprintError, match="allow_imports=True"):
        imported.build()
    loop = imported.build(
        model_registry=registry(editor=FakeChatModel(["x", "done"])), allow_imports=True
    )
    assert (await loop.run("go")).metadata["iterations"] == 2

    endless = workflow_blueprint({"loop": "editor", "max_iterations": 2})
    loop = endless.build(model_registry=registry(editor=FakeChatModel(["x"], repeat_last=True)))
    metadata = (await loop.run("go")).metadata
    assert (metadata["iterations"], metadata["stopped_reason"]) == (2, "max_iterations")
    assert metadata["stop"]["bound"] == "max_iterations"  # the stop diagnosis


def test_a_workflow_entrypoint_takes_the_checkpointer() -> None:
    saver = InMemorySaver()
    workflow = workflow_blueprint({"sequential": ["writer"]}).build(checkpointer=saver)
    assert isinstance(workflow, Sequential) and workflow.checkpointer is saver


def test_single_agent_workflow_is_the_agent() -> None:
    agent = workflow_blueprint("writer").build()
    assert isinstance(agent, Agent)
    assert agent.name == "writer"


def team_blueprint(**team: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "support",
            "description": "Answers customers.",
            "agents": [
                {"name": "lead", "description": "Coordinates.", "model": "lead"},
                {
                    "name": "clerk",
                    "description": "Looks up orders.",
                    "tools": ["lookup"],
                    "model": "clerk",
                },
                {
                    "name": "cashier",
                    "description": "Issues refunds.",
                    "tools": ["refund"],
                    "model": "cashier",
                },
            ],
            "team": {"orchestrator": "lead", "members": ["clerk", "cashier"], **team},
            "approvals": [{"tool": "refund"}],
        }
    )


def test_team_is_built_with_topology_and_options() -> None:
    bp = team_blueprint(
        peers={"clerk": ["cashier"]},
        handoffs={"lead": ["cashier"]},
        task_board=True,
        blackboard=True,
        limits={"max_handoffs": 2, "max_calls_per_member": 3},
    )

    team = bp.build(tools=[lookup, refund], checkpointer=InMemorySaver())

    assert isinstance(team, Team)
    assert team.name == "support"
    assert team.description == "Answers customers."
    topology = team.topology()
    assert topology["delegates"] == {"lead": ["clerk", "cashier"]}
    assert topology["handoffs"] == {"lead": ["cashier"]}
    assert "cashier" in topology["peers"]["clerk"]
    assert team.limits.max_handoffs == 2
    assert team.limits.max_calls_per_member == 3
    assert team.limits.max_delegation_depth == 4
    lead_tools = {t.name for t in team.agent("lead").tools}  # type: ignore[attr-defined]
    assert {"delegate_task", "transfer_to_cashier"} <= lead_tools
    assert any("task" in name for name in lead_tools)


def test_invalid_team_raises_blueprint_error() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "x",
            "agents": [
                {"name": "lead", "tools": ["delegate_task"]},
                {"name": "clerk", "description": "x"},
            ],
            "team": {"orchestrator": "lead", "members": ["clerk"]},
        }
    )

    @tool
    def delegate_task(task: str) -> str:
        """Clashes with the team tool."""

        return task

    with pytest.raises(BlueprintError, match="Cannot build team 'x'"):
        bp.build(tools=[delegate_task])


async def test_team_delegates_and_pauses_for_member_approval() -> None:
    def lead(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("delegate_task", {"agent": "cashier", "task": "Refund order 7 by 20."})
        return "Refund done."

    models = registry(
        lead=FakeChatModel([lead, lead]),
        clerk=FakeChatModel(["unused"]),
        cashier=FakeChatModel(
            [tool_call("refund", {"order_id": "7", "amount": 20}), "Refunded 20."]
        ),
    )
    team = team_blueprint().build(
        tools=[lookup, refund], model_registry=models, checkpointer=InMemorySaver()
    )
    assert isinstance(team, Team)

    paused = await team.run("Please refund order 7.", thread_id="t1")

    assert paused.is_interrupted
    assert CALLS == []

    resumed = await team.resume("t1", {"action": "approve"})

    assert resumed.output == "Refund done."
    assert CALLS == ["refund:7:20.0"]


async def test_workflow_can_run_the_team_as_a_step() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "process",
            "agents": [
                {"name": "intake", "model": "intake"},
                {"name": "lead", "description": "Coordinates.", "model": "lead"},
                {"name": "clerk", "description": "Looks up orders.", "model": "clerk"},
            ],
            "team": {"name": "desk", "orchestrator": "lead", "members": ["clerk"]},
            "workflow": {"sequential": ["intake", "desk"]},
            "entrypoint": "workflow",
        }
    )
    models = registry(
        intake=FakeChatModel(["ticket: order 7"]),
        lead=FakeChatModel(["answered"]),
        clerk=FakeChatModel(["unused"]),
    )

    workflow = bp.build(model_registry=models)
    result = await workflow.run("Customer mail")

    assert isinstance(workflow, Sequential)
    assert isinstance(workflow.steps[1], Team)
    assert result.output == "answered"


def test_blueprint_agents_attach_skills_inline_by_default() -> None:
    blueprint = Blueprint.model_validate({"name": "inline-default", "agents": [{"name": "a"}]})
    assert blueprint.defaults.skill_mode == "inline"


def blueprint(**team: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "desk",
            "agents": [
                {"name": "lead", "description": "Coordinates."},
                {"name": "clerk", "description": "Does the work."},
                {"name": "checker", "description": "Checks the work."},
            ],
            "team": {"orchestrator": "lead", "members": ["clerk", "checker"], **team},
        }
    )


def test_the_team_uses_the_given_shared_state() -> None:
    board, blackboard, bus = TaskBoard(), Blackboard(), InMemoryMessageBus()

    team = blueprint(task_board=True, blackboard=True, peers={"clerk": ["checker"]}).build(
        task_board=board, blackboard=blackboard, bus=bus
    )

    assert isinstance(team, Team)
    assert team.task_board is board and team.blackboard is blackboard and team.bus is bus
    assert bus.mailboxes() == ["checker", "clerk"]


async def test_abuild_passes_the_shared_state_on() -> None:
    board = TaskBoard()
    team = await blueprint(task_board=True).abuild(task_board=board)
    assert isinstance(team, Team) and team.task_board is board


@pytest.mark.parametrize(
    ("team", "option", "message"),
    [
        ({}, "task_board", "task_board: false"),
        ({}, "blackboard", "blackboard: false"),
        ({"messaging": False}, "bus", "messaging: false"),
    ],
)
def test_shared_state_the_team_does_not_use_is_refused(
    team: dict[str, Any], option: str, message: str
) -> None:
    value = {"task_board": TaskBoard(), "blackboard": Blackboard(), "bus": InMemoryMessageBus()}
    with pytest.raises(BlueprintError, match=message):
        blueprint(**team).build(**{option: value[option]})


def test_shared_state_needs_a_team() -> None:
    solo = Blueprint.from_dict({"name": "solo", "agents": [{"name": "clerk"}]})
    with pytest.raises(BlueprintError, match="has no team"):
        solo.build(task_board=TaskBoard())
