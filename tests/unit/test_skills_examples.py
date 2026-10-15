"""Executed example for every builtin skill: an agent with a scripted model loads it on demand.

The scripted model proves the technical flow only (discovery, progressive disclosure, the
loaded text), not the quality of any real model's answer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import SystemMessage, ToolMessage

from awa.agents import Agent
from awa.skills import SkillRegistry
from awa.testing import FunctionChatModel, ModelCall, tool_call

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "gen_skill_matrix", ROOT / "scripts/gen_skill_matrix.py"
)
assert _spec and _spec.loader
matrix = importlib.util.module_from_spec(_spec)
sys.modules["gen_skill_matrix"] = matrix
_spec.loader.exec_module(matrix)

BUILTIN = SkillRegistry.builtin()
NAMES = sorted(BUILTIN.names())


def _system(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def _everything_sent(call: ModelCall) -> str:
    return "\n".join(str(message.content) for message in call.messages)


def _example_agent(name: str) -> tuple[Agent, FunctionChatModel]:
    """An agent that loads `name` on demand and answers with the output format it was given."""

    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("load_skill", {"name": name})
        loaded = next(m for m in reversed(call.messages) if isinstance(m, ToolMessage))
        text = str(loaded.content)
        block = text.split("```", 2)[1].split("\n", 1)[1].rstrip("\n")
        return f"Result for the task:\n{block}"

    model = FunctionChatModel(respond)
    agent = Agent(
        "worker",
        instructions="Do the task and follow the loaded skill.",
        model=model,
        skills=[name],
        skill_mode="on_demand",
    )
    return agent, model


@pytest.mark.slow
@pytest.mark.parametrize("name", NAMES)
async def test_skill_example(name: str) -> None:
    skill = BUILTIN.get(name)
    agent, model = _example_agent(name)

    result = await agent.run("Handle the case in the attached request.")

    # Discovered: the agent card and the catalog in the prompt name the skill.
    assert agent.card.skills == (name,)
    first, second = model.calls
    assert f"`{name}`: {' '.join(skill.description.split())}" in " ".join(_system(first).split())
    # The tool the model needs is bound, and its name argument is limited to this skill.
    assert "load_skill" in first.tool_names
    load = next(tool for tool in agent.tools if tool.name == "load_skill")
    name_schema = load.args_schema.model_json_schema()["properties"]["name"]
    assert name_schema.get("enum", [name_schema.get("const")]) == [name]
    # Progressive disclosure: no body before the call, the body (and file list) after it.
    body_marker = skill.instructions.splitlines()[0]
    assert body_marker not in _everything_sent(first)
    assert body_marker in _everything_sent(second)
    for resource in skill.resources:
        assert resource in _everything_sent(second)
        assert skill.read_resource(resource)[:60] not in _everything_sent(second)
    # The example output is the skill's own output format, and the matrix page shows its start.
    assert len(model.calls) == 2
    output = str(result.output)
    assert matrix.first_example_block(skill) in output
    page = (ROOT / "docs/reference/skills.md").read_text(encoding="utf-8")
    assert f"`{matrix.table_cell(output.splitlines()[1].strip())}`" in page


async def test_a_skill_that_is_not_loaded_never_reaches_the_prompt() -> None:
    model = FunctionChatModel(lambda call: "done")
    agent = Agent(
        "worker",
        model=model,
        skills=["invoice-reconciliation", "email-triage"],
        skill_mode="on_demand",
    )

    await agent.run("hello")

    sent = _everything_sent(model.calls[0])
    assert "`invoice-reconciliation`:" in sent and "`email-triage`:" in sent
    assert "## Matching, per line" not in sent and "## Decision rules" not in sent


async def test_loading_one_skill_leaves_the_other_out_and_resources_stay_on_request() -> None:
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("load_skill", {"name": "invoice-reconciliation"})
        if call.index == 1:
            return tool_call(
                "read_skill_resource",
                {"name": "invoice-reconciliation", "path": "references/match-tolerances.md"},
            )
        return "ok"

    model = FunctionChatModel(respond)
    agent = Agent(
        "worker",
        model=model,
        skills=["invoice-reconciliation", "email-triage"],
        skill_mode="on_demand",
    )

    await agent.run("check invoice")

    resource = BUILTIN.get("invoice-reconciliation").read_resource("references/match-tolerances.md")
    assert "## Matching, per line" in _everything_sent(model.calls[1])
    assert resource[:80] not in _everything_sent(model.calls[1])
    assert resource[:80] in _everything_sent(model.calls[2])
    assert "# Email Triage" not in _everything_sent(model.calls[2])


async def test_the_example_on_the_skills_page_is_the_real_output() -> None:
    page = (ROOT / "docs/develop/skills.md").read_text(encoding="utf-8")
    shown = page.split("model's answer. For `invoice-reconciliation` the answer is:")[1]
    shown = shown.split("```text\n", 1)[1].split("```", 1)[0].rstrip("\n")
    agent, _ = _example_agent("invoice-reconciliation")

    result = await agent.run("Reconcile invoice INV-2291.")

    assert str(result.output) == f"Result for the task:\n{shown}"
