"""Blueprint schema parsing, YAML/dict round trips, the `triggers:` / `automation:` sections and Mermaid rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from awa.blueprints import (
    Blueprint,
    BlueprintError,
    LoopStep,
    ParallelStep,
    SequentialStep,
    workflow_references,
)
from awa.blueprints._diagram import blueprint_diagram, diagram_to_mermaid
from awa.models import ModelRegistry
from awa.testing import FakeChatModel

YAML = """\
name: invoice-processing
description: Validate and book invoices.
version: 1.2.0
defaults:
  model: fast
  model_settings: {temperature: 0}
  max_steps: 10
agents:
  - name: intake
    description: Extracts invoice data.
    instructions: |
      Read the invoice.
      Return the fields.
    tools: [read_document]
    capabilities: [extraction]
  - name: validator
    description: Checks invoices.
  - name: fraud_check
    description: Flags fraud.
  - name: approver
    description: Approves payment.
    tools: [book_payment]
workflow:
  sequential:
    - intake
    - parallel: [validator, fraud_check]
      fail_fast: false
    - loop: approver
      max_iterations: 2
      until_contains: APPROVED
approvals:
  - tool: book_payment
"""


def test_from_yaml_text_parses_nested_models() -> None:
    blueprint = Blueprint.from_yaml(YAML)

    assert blueprint.name == "invoice-processing"
    assert blueprint.defaults.model == "fast"
    assert blueprint.defaults.model_settings is not None
    assert blueprint.defaults.model_settings.temperature == 0
    assert blueprint.agent("intake").instructions == "Read the invoice.\nReturn the fields.\n"
    workflow = blueprint.workflow
    assert isinstance(workflow, SequentialStep)
    assert workflow.sequential[0] == "intake"
    parallel = workflow.sequential[1]
    assert isinstance(parallel, ParallelStep)
    assert parallel.parallel == ["validator", "fraud_check"]
    assert parallel.fail_fast is False
    loop = workflow.sequential[2]
    assert isinstance(loop, LoopStep)
    assert loop.loop == "approver"
    assert loop.until_contains == "APPROVED"
    assert blueprint.approvals[0].tool == "book_payment"


def test_yaml_round_trip_is_lossless_and_uses_block_text() -> None:
    blueprint = Blueprint.from_yaml(YAML)

    text = blueprint.to_yaml()

    assert "instructions: |" in text
    assert "max_steps: 25" not in text  # defaults are omitted
    assert Blueprint.from_yaml(text) == blueprint
    assert Blueprint.from_dict(blueprint.to_dict()) == blueprint


def test_from_yaml_reads_yaml_and_json_files(tmp_path: Path) -> None:
    blueprint = Blueprint.from_yaml(YAML)
    yaml_file = tmp_path / "process.yaml"
    yaml_file.write_text(YAML, encoding="utf-8")
    json_file = tmp_path / "process.json"
    json_file.write_text(json.dumps(blueprint.to_dict()), encoding="utf-8")

    assert Blueprint.from_yaml(yaml_file) == blueprint
    assert Blueprint.from_yaml(str(yaml_file)) == blueprint
    assert Blueprint.from_yaml(str(json_file)) == blueprint


def test_missing_file_raises_blueprint_error(tmp_path: Path) -> None:
    with pytest.raises(BlueprintError, match="Blueprint file not found"):
        Blueprint.from_yaml(tmp_path / "missing.yaml")


def test_malformed_yaml_raises_blueprint_error() -> None:
    with pytest.raises(BlueprintError, match="Malformed blueprint YAML"):
        Blueprint.from_yaml("name: [unclosed\nagents: x")


def test_top_level_must_be_a_mapping() -> None:
    with pytest.raises(BlueprintError, match="mapping at the top level"):
        Blueprint.from_yaml("- a\n- b\n")


def test_schema_errors_list_every_problem_with_its_path() -> None:
    with pytest.raises(BlueprintError) as caught:
        Blueprint.from_dict(
            {
                "name": "x",
                "agents": [{"name": "a", "toolz": []}, {"description": "no name"}],
                "defaults": {"model_settings": {"temprature": 1}},
            }
        )

    paths = {issue.path for issue in caught.value.issues}
    assert "agents[0].toolz" in paths
    assert "agents[1].name" in paths
    assert "defaults.model_settings.temprature" in paths
    assert "agents[0].toolz" in str(caught.value)


def test_agents_are_required() -> None:
    with pytest.raises(BlueprintError, match="agents"):
        Blueprint.from_dict({"name": "x", "agents": []})


def test_workflow_nodes_accept_every_form() -> None:
    blueprint = Blueprint.from_dict(
        {
            "name": "x",
            "agents": [{"name": "a"}, {"name": "b"}],
            "workflow": {
                "sequential": [
                    {"loop": {"parallel": ["a", "b"], "max_concurrency": 1}, "until": "m:f"},
                    {"sequential": ["a"], "carry": "all", "name": "inner"},
                ]
            },
        }
    )
    workflow = blueprint.workflow
    assert isinstance(workflow, SequentialStep)
    loop = workflow.sequential[0]
    assert isinstance(loop, LoopStep)
    assert isinstance(loop.loop, ParallelStep)
    assert loop.loop.max_concurrency == 1
    inner = workflow.sequential[1]
    assert isinstance(inner, SequentialStep)
    assert inner.carry == "all"


def test_unknown_workflow_node_keys_are_rejected() -> None:
    with pytest.raises(BlueprintError):
        Blueprint.from_dict({"name": "x", "agents": [{"name": "a"}], "workflow": {"serial": ["a"]}})


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"agents": [{"name": "solo"}]}, "solo"),
        ({"agents": [{"name": "a"}, {"name": "b"}]}, None),
        ({"agents": [{"name": "a"}], "workflow": "a"}, "workflow"),
        (
            {
                "agents": [{"name": "a"}, {"name": "b"}],
                "team": {"orchestrator": "a", "members": ["b"]},
                "workflow": "a",
            },
            "team",
        ),
        ({"agents": [{"name": "a"}, {"name": "b"}], "entrypoint": "b"}, "b"),
    ],
)
def test_entrypoint_inference(data: dict[str, object], expected: str | None) -> None:
    assert Blueprint.from_dict({"name": "x", **data}).resolved_entrypoint() == expected


def test_team_name_defaults_to_blueprint_name() -> None:
    team = {"orchestrator": "a", "members": ["b"]}
    agents = [{"name": "a"}, {"name": "b"}]
    assert Blueprint.from_dict({"name": "x", "agents": agents, "team": team}).team_name() == "x"
    named = Blueprint.from_dict({"name": "x", "agents": agents, "team": {**team, "name": "desk"}})
    assert named.team_name() == "desk"


def test_agent_lookup_raises_key_error_for_unknown_names() -> None:
    with pytest.raises(KeyError):
        Blueprint.from_dict({"name": "x", "agents": [{"name": "a"}]}).agent("b")


def test_workflow_references_and_active_agents() -> None:
    blueprint = Blueprint.from_dict(
        {
            "name": "x",
            "agents": [{"name": n} for n in ("lead", "clerk", "intake", "spare")],
            "team": {"name": "desk", "orchestrator": "lead", "members": ["clerk"]},
            "workflow": {"sequential": ["intake", {"loop": {"parallel": ["desk", "intake"]}}]},
            "entrypoint": "workflow",
        }
    )

    assert blueprint.workflow is not None
    assert list(workflow_references(blueprint.workflow)) == [
        ("intake", "workflow.sequential[0]"),
        ("desk", "workflow.sequential[1].loop.parallel[0]"),
        ("intake", "workflow.sequential[1].loop.parallel[1]"),
    ]
    assert blueprint.uses_team()
    assert blueprint.active_agents() == ["lead", "clerk", "intake"]


@pytest.mark.parametrize(
    ("entrypoint", "uses_team", "active"),
    [
        ("team", True, ["lead", "clerk"]),
        ("spare", False, ["spare"]),
        ("workflow", False, ["intake"]),
    ],
)
def test_active_agents_follow_the_entrypoint(
    entrypoint: str, uses_team: bool, active: list[str]
) -> None:
    blueprint = Blueprint.from_dict(
        {
            "name": "x",
            "agents": [{"name": n} for n in ("lead", "clerk", "intake", "spare")],
            "team": {"orchestrator": "lead", "members": ["clerk"]},
            "workflow": "intake",
            "entrypoint": entrypoint,
        }
    )

    assert blueprint.uses_team() is uses_team
    assert blueprint.active_agents() == active


def test_approval_agents_are_the_active_agents_with_gated_tools() -> None:
    blueprint = Blueprint.from_dict(
        {
            "name": "x",
            "entrypoint": "a",
            "agents": [{"name": "a", "tools": ["pay"]}, {"name": "b", "tools": ["pay"]}],
            "approvals": [{"tool": "pay"}],
        }
    )

    assert blueprint.approval_agents() == ["a"]


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_output_schema_rejects_values_json_cannot_hold(value: float) -> None:
    with pytest.raises(BlueprintError, match="output_schema must contain only JSON values"):
        Blueprint.from_dict({"name": "a", "agents": [{"name": "a", "output_schema": {"x": value}}]})


BASE = {"name": "digest", "agents": [{"name": "writer", "description": "Writes."}]}


def test_top_level_triggers_join_the_automation_section() -> None:
    blueprint = Blueprint.from_dict(
        {
            **BASE,
            "triggers": [{"name": "nightly", "schedule": {"cron": "0 2 * * *"}}],
            "automation": {"concurrency": 2},
        }
    )

    section = blueprint.automation_section()
    assert section is not None and section.concurrency == 2
    assert [trigger.name for trigger in section.triggers] == ["nightly"]
    assert Blueprint.from_dict(BASE).automation_section() is None
    assert Blueprint.from_yaml_text(blueprint.to_yaml()) == blueprint


def test_check_reports_automation_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DIGEST_SECRET", raising=False)
    blueprint = Blueprint.from_dict(
        {
            **BASE,
            "triggers": [{"name": "hook", "webhook": {"secret_env": "DIGEST_SECRET"}}],
            "automation": {"triggers": [{"name": "other", "schedule": {"cron": "0 * * * *"}}]},
        }
    )

    issues = {issue.path: issue for issue in blueprint.check()}
    assert issues["triggers"].severity == "error"

    hooked = Blueprint.from_dict(
        {**BASE, "triggers": [{"name": "hook", "webhook": {"secret_env": "DIGEST_SECRET"}}]}
    )
    [unset] = [issue for issue in hooked.check() if "DIGEST_SECRET" in issue.message]
    assert (unset.severity, unset.path) == ("warning", "triggers.hook")


def test_awa_serve_mounts_the_automation_of_a_blueprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.server import build_blueprint_app

    monkeypatch.setattr(sys, "path", list(sys.path))
    registry = ModelRegistry()
    registry.register("default", FakeChatModel(["done"], repeat_last=True))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    (tmp_path / "tools_for_digest.py").write_text(
        'def noop() -> str:\n    """Do nothing."""\n    return ""\n\n__all__ = ["noop"]\n',
        encoding="utf-8",
    )
    path = tmp_path / "digest.yaml"
    path.write_text(
        "name: digest\n"
        "agents:\n  - name: writer\n    description: Writes.\n"
        "triggers:\n  - name: hook\n    webhook: {allow_unsigned: true}\n",
        encoding="utf-8",
    )

    app = build_blueprint_app(path, tools=[str(tmp_path / "tools_for_digest.py")])

    with TestClient(app) as client:
        listing = client.get("/v1/automations")
    assert listing.status_code == 200, listing.text
    assert "hook" in listing.text


AGENTS: list[dict[str, Any]] = [
    {"name": "lead"},
    {"name": "clerk", "tools": ["refund"]},
    {"name": "cashier"},
]


def test_team_diagram_shows_delegation_peers_handoffs_and_approvals() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "desk",
            "agents": AGENTS,
            "team": {
                "orchestrator": "lead",
                "members": ["clerk", "cashier"],
                "peers": {"clerk": ["cashier"], "cashier": ["clerk"]},
                "handoffs": {"lead": ["cashier"]},
            },
            "approvals": [{"tool": "refund"}],
        }
    )

    assert bp.to_mermaid() == "\n".join(
        [
            "flowchart TD",
            '    a0(["lead"])',
            '    a1["clerk<br/>approval: refund"]',
            '    a2["cashier"]',
            "    a0 --> a1",
            "    a0 --> a2",
            "    a1 <-.-> a2",
            "    a0 ==>|handoff| a2",
        ]
    )


def test_directed_peers_and_explicit_delegates() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "desk",
            "agents": AGENTS,
            "team": {
                "orchestrator": "lead",
                "members": ["clerk", "cashier"],
                "delegates": {"lead": ["clerk"], "clerk": ["cashier"]},
                "peers": {"clerk": ["cashier"]},
                "symmetric_peers": False,
            },
        }
    )

    lines = bp.to_mermaid().splitlines()

    assert "    a0 --> a1" in lines
    assert "    a1 --> a2" in lines
    assert "    a0 --> a2" not in lines
    assert "    a1 -.-> a2" in lines


def test_workflow_diagram_with_parallel_loop_and_team_step() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "flow",
            "agents": [*AGENTS, {"name": "intake"}],
            "team": {"name": "desk", "orchestrator": "lead", "members": ["clerk", "cashier"]},
            "workflow": {
                "sequential": [
                    {"parallel": ["intake", "clerk"]},
                    {"loop": "cashier", "until_contains": "OK", "max_iterations": 2},
                    "desk",
                ]
            },
            "entrypoint": "workflow",
        }
    )

    assert bp.to_mermaid() == "\n".join(
        [
            "flowchart LR",
            "    input([input])",
            "    output([output])",
            '    n1["intake"]',
            '    n2["clerk"]',
            '    n3["cashier"]',
            "    n3 -.->|until 'OK', max 2| n3",
            "    n1 --> n3",
            "    n2 --> n3",
            '    n4[["team desk"]]',
            "    n3 --> n4",
            "    input --> n1",
            "    input --> n2",
            "    n4 --> output",
        ]
    )


def test_single_agent_diagram() -> None:
    bp = Blueprint.from_dict({"name": "solo", "agents": [{"name": "helper"}]})
    assert bp.to_mermaid() == 'flowchart TD\n    a0["helper"]'


def test_the_diagram_model_is_what_mermaid_and_the_ui_draw() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "drafting",
            "agents": [{"name": "writer"}, {"name": "critic", "tools": ["refund"]}],
            "workflow": {
                "loop": {"sequential": ["writer", "critic"]},
                "until_contains": 'OK|"done"',
                "max_iterations": 2,
            },
            "approvals": [{"tool": "refund"}],
        }
    )

    diagram = blueprint_diagram(bp)

    assert diagram.direction == "LR"
    assert [(n.id, n.shape) for n in diagram.nodes] == [
        ("input", "terminal"),
        ("output", "terminal"),
        ("n1", "agent"),
        ("n2", "agent"),
    ]
    assert diagram.nodes[3].lines == ("critic", "approval: refund")
    [back] = [edge for edge in diagram.edges if edge.style == "dotted"]
    assert (back.source, back.target) == ("n2", "n1")
    assert back.label == "until 'OK|\"done\"', max 2"  # raw in the model
    # Mermaid cannot hold `|` or `"` in an edge label.
    assert "    n2 -.->|until 'OK/'done'', max 2| n1" in diagram_to_mermaid(diagram)
    assert diagram_to_mermaid(diagram) == bp.to_mermaid()
