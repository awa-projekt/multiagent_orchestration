"""Deterministic blueprint validation (`Blueprint.check`): rule tools for workers, cases dry-run before any agent runs, and the `special_cases:` section."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from awa.blueprints import AcceptanceDesign, Blueprint, BlueprintError, BlueprintIssue
from awa.blueprints._build_roles import share_check_tools
from awa.blueprints._project import acceptance_problems
from awa.blueprints.architect import ARCHITECT_INSTRUCTIONS
from awa.skills import SkillRegistry


@tool
def lookup(query: str) -> str:
    """Look something up."""

    return query


@tool
def read_csv_tool(path: str) -> str:
    """Read a file of my own."""

    return path


read_csv_tool.name = "read_csv"


@tool
def refund(order_id: str) -> str:
    """Refund an order."""

    return order_id


def blueprint(**data: Any) -> Blueprint:
    base: dict[str, Any] = {
        "name": "desk",
        "agents": [
            {"name": "lead", "description": "Leads."},
            {"name": "clerk", "description": "Looks things up.", "tools": ["lookup"]},
            {"name": "cashier", "description": "Refunds.", "tools": ["refund"]},
        ],
        "team": {"orchestrator": "lead", "members": ["clerk", "cashier"]},
    }
    base.update(data)
    return Blueprint.from_dict(base)


def errors(issues: list[BlueprintIssue]) -> dict[str, str]:
    return {issue.path: issue.message for issue in issues if issue.severity == "error"}


def warnings(issues: list[BlueprintIssue]) -> dict[str, str]:
    return {issue.path: issue.message for issue in issues if issue.severity == "warning"}


def test_valid_team_blueprint_has_no_issues() -> None:
    assert blueprint().check(tools=[lookup, refund]) == []


def test_issue_renders_severity_path_and_message() -> None:
    issue = BlueprintIssue("error", "agents[0].name", "bad")
    assert str(issue) == "error: agents[0].name: bad"


def test_invalid_and_duplicate_names() -> None:
    bp = blueprint(
        name="bad name",
        agents=[
            {"name": "lead", "description": "x"},
            {"name": "lead", "description": "x"},
            {"name": "9lives"},
            {"name": "team"},
        ],
        team=None,
        entrypoint="lead",
    )

    found = errors(bp.check())

    assert "invalid blueprint name" in found["name"]
    assert "duplicate agent name" in found["agents[1].name"]
    assert "invalid agent name" in found["agents[2].name"]
    assert "reserved" in found["agents[3].name"]


def test_tools_are_checked_only_when_a_catalog_is_given() -> None:
    bp = blueprint()
    assert errors(bp.check()) == {}

    found = errors(bp.check(tools=[lookup]))

    assert found == {"agents[2].tools[0]": "unknown tool 'refund' (known: lookup)"}


def test_a_builtin_tool_name_without_its_prefix_is_flagged() -> None:
    agents = [{"name": "reader", "description": "Reads.", "tools": ["read_csv"]}]
    bp = Blueprint.from_dict({"name": "desk", "agents": agents})
    hint = "'read_csv' is a builtin tool; write 'builtin:read_csv' to use it"

    assert warnings(bp.check()) == {"agents[0].tools[0]": hint}
    unknown = errors(bp.check(tools=[lookup]))["agents[0].tools[0]"]
    assert unknown.startswith("unknown tool 'read_csv'") and unknown.endswith(hint)
    assert errors(bp.check(tools=[read_csv_tool])) == {}


def test_tool_catalog_forms() -> None:
    assert errors(blueprint().check(tools={"lookup": lookup, "refund": refund})) == {}


def test_tool_references_duplicates_and_import_syntax() -> None:
    bp = blueprint(
        agents=[
            {
                "name": "lead",
                "description": "x",
                "tools": ["pkg.mod:tool", "bad:ref:x", "lookup", "lookup"],
            },
            {"name": "clerk", "description": "x"},
            {"name": "cashier", "description": "x"},
        ]
    )

    found = errors(bp.check(tools=[lookup]))

    assert "agents[0].tools[0]" not in found
    assert "invalid import reference" in found["agents[0].tools[1]"]
    assert "listed twice" in found["agents[0].tools[3]"]


def test_skills_are_checked_against_the_builtin_or_given_skills() -> None:
    bp = blueprint(
        agents=[
            {"name": "lead", "description": "x", "skills": ["delegation-briefs", "nope", "nope"]},
            {"name": "clerk", "description": "x"},
            {"name": "cashier", "description": "x"},
        ]
    )

    found = errors(bp.check())
    assert "unknown skill 'nope'" in found["agents[0].skills[1]"]
    assert "listed twice" in found["agents[0].skills[2]"]

    assert "agents[0].skills[0]" in errors(bp.check(skills=SkillRegistry()))
    assert "agents[0].skills[0]" not in errors(bp.check(skills={"delegation-briefs"}))


def test_output_schema_errors_are_reported() -> None:
    bp = blueprint(
        agents=[
            {"name": "lead", "description": "x"},
            {"name": "clerk", "description": "x", "output_schema": {"type": "string"}},
            {"name": "cashier", "description": "x"},
        ]
    )

    assert "type: object" in errors(bp.check())["agents[1].output_schema"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"team": None}, "cannot infer the entrypoint"),
        ({"team": None, "entrypoint": "team"}, "no team is declared"),
        ({"entrypoint": "workflow"}, "no workflow is declared"),
        ({"entrypoint": "ghost"}, "unknown entrypoint 'ghost'"),
    ],
)
def test_entrypoint_errors(data: dict[str, Any], message: str) -> None:
    assert message in errors(blueprint(**data).check())["entrypoint"]


def test_team_reference_and_topology_errors() -> None:
    bp = blueprint(
        team={
            "name": "clerk",
            "orchestrator": "boss",
            "members": ["clerk", "clerk", "lead", "ghost"],
            "delegates": {"clerk": ["clerk"], "nobody": ["clerk"]},
            "peers": {"clerk": ["lead", "cashier"]},
            "handoffs": {"lead": ["ghost"]},
        }
    )

    found = errors(bp.check())

    assert "also an agent name" in found["team.name"]
    assert "unknown agent 'boss'" in found["team.orchestrator"]
    assert "listed twice" in found["team.members[1]"]
    assert "unknown agent 'ghost'" in found["team.members[3]"]
    assert "cannot target itself" in found["team.delegates.clerk[0]"]
    assert "unknown team agent 'nobody'" in found["team.delegates.nobody"]
    assert "unknown target 'ghost'" in found["team.handoffs.lead[0]"]


def test_orchestrator_cannot_be_a_member_or_a_delegation_target() -> None:
    bp = blueprint(
        team={
            "orchestrator": "lead",
            "members": ["clerk", "lead"],
            "delegates": {"clerk": ["lead"]},
            "handoffs": {"clerk": ["lead"]},
        }
    )

    found = errors(bp.check())

    assert "cannot also be a member" in found["team.members[1]"]
    assert "unknown target 'lead'" in found["team.delegates.clerk[0]"]
    assert "team.handoffs.clerk[0]" not in found  # handing back to the orchestrator is fine


def test_handoff_source_cannot_declare_structured_output() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    bp = blueprint(
        agents=[
            {"name": "lead", "description": "x", "output_schema": schema},
            {"name": "clerk", "description": "x"},
            {"name": "cashier", "description": "x"},
        ],
        team={
            "orchestrator": "lead",
            "members": ["clerk", "cashier"],
            "handoffs": {"lead": ["clerk"]},
        },
    )

    assert "cannot transfer control" in errors(bp.check())["team.handoffs.lead"]


def test_members_without_description_and_duplicate_targets_warn() -> None:
    bp = blueprint(
        agents=[{"name": "lead"}, {"name": "clerk"}, {"name": "cashier", "description": "x"}],
        team={
            "orchestrator": "lead",
            "members": ["clerk", "cashier"],
            "peers": {"clerk": ["cashier", "cashier"]},
        },
    )

    found = warnings(bp.check())

    assert "no description" in found["team.members[0]"]
    assert "listed twice" in found["team.peers.clerk[1]"]


def test_workflow_references_and_structure() -> None:
    bp = blueprint(
        team=None,
        workflow={
            "sequential": [
                "lead",
                "ghost",
                {"parallel": ["clerk", "clerk"], "name": "bad name"},
                {"loop": "cashier", "until": "not a ref", "until_contains": "OK"},
                {"loop": "cashier"},
            ]
        },
    )

    found = errors(bp.check())

    assert "unknown workflow step 'ghost'" in found["workflow.sequential[1]"]
    assert "invalid workflow name" in found["workflow.sequential[2].name"]
    assert "appears twice" in found["workflow.sequential[2].parallel[1]"]
    assert "either 'until' or 'until_contains'" in found["workflow.sequential[3]"]
    assert "invalid import reference" in found["workflow.sequential[3].until"]
    assert "always runs 3 times" in warnings(bp.check())["workflow.sequential[4]"]


def test_workflow_may_reference_the_team_by_name() -> None:
    bp = blueprint(
        agents=[
            {"name": "lead", "description": "x"},
            {"name": "clerk", "description": "x"},
            {"name": "cashier", "description": "x"},
            {"name": "intake", "description": "x"},
        ],
        team={"name": "desk-team", "orchestrator": "lead", "members": ["clerk", "cashier"]},
        workflow={"sequential": ["intake", "desk-team"]},
        entrypoint="workflow",
    )

    assert bp.check() == []


def test_unused_parts_and_orphan_agents_warn() -> None:
    bp = blueprint(
        agents=[
            {"name": "lead", "description": "x"},
            {"name": "clerk", "description": "x"},
            {"name": "cashier", "description": "x"},
            {"name": "orphan", "description": "x"},
        ],
        workflow="clerk",
    )

    found = warnings(bp.check())

    assert "not used by the entrypoint" in found["agents[3]"]
    assert "workflow is declared" in found["workflow"]

    solo = blueprint(entrypoint="clerk")
    assert "team is declared" in warnings(solo.check())["team"]


def test_approval_errors() -> None:
    bp = blueprint(
        approvals=[
            {"tool": "refund"},
            {"tool": "refund"},
            {"tool": "wire_money"},
            {"tool": "lookup", "when": "no-such-module:fn"},
        ]
    )

    found = errors(bp.check())

    assert "two approvals" in found["approvals[1].tool"]
    assert "no agent uses tool 'wire_money'" in found["approvals[2].tool"]
    assert "invalid import reference" in found["approvals[3].when"]
    assert "approvals[0].tool" not in found


def test_approvals_inside_a_workflow_are_allowed() -> None:
    bp = blueprint(
        team=None, workflow={"sequential": ["clerk", "cashier"]}, approvals=[{"tool": "refund"}]
    )

    assert errors(bp.check(tools=[lookup, refund])) == {}
    assert bp.approval_agents() == ["cashier"]


def network_blueprint(**data: Any) -> Blueprint:
    base: dict[str, Any] = {
        "name": "desk",
        "agents": [
            {"name": "lead", "description": "Leads."},
            {"name": "clerk", "description": "Looks up.", "capabilities": ["lookup"]},
            {"name": "cashier", "description": "Refunds.", "tools": ["refund"]},
        ],
        "network": {"lead": "lead", "teammates": ["clerk", "cashier"]},
    }
    base.update(data)
    return Blueprint.from_dict(base)


def test_a_network_entrypoint_is_inferred_and_checked() -> None:
    bp = network_blueprint()

    assert bp.resolved_entrypoint() == "network"
    assert bp.active_agents() == ["lead", "clerk", "cashier"]
    assert warnings(bp.check(tools=[refund])) == {
        "network.teammates[1]": "teammate 'cashier' has no capabilities; it can only take "
        "tasks assigned to it by name"
    }


def test_network_reference_errors() -> None:
    bp = network_blueprint(
        network={
            "lead": "ghost",
            "teammates": ["clerk", "clerk", "lead"],
            "peers": {"clerk": ["clerk", "nobody"]},
        }
    )

    found = errors(bp.check())
    assert "unknown agent 'ghost'" in found["network.lead"]
    assert "listed twice" in found["network.teammates[1]"]
    assert "cannot target itself" in found["network.peers.clerk[0]"]
    assert "unknown teammate 'nobody'" in found["network.peers.clerk[1]"]


def test_a_team_can_delegate_to_the_workflow_and_the_network() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "desk",
            "agents": [
                {"name": "lead", "description": "Leads."},
                {"name": "writer", "description": "Writes."},
                {"name": "editor", "description": "Edits."},
                {"name": "planner", "description": "Plans."},
                {"name": "worker", "description": "Works.", "capabilities": ["work"]},
            ],
            "team": {"orchestrator": "lead", "members": ["drafting", "crew"]},
            "workflow": {"name": "drafting", "sequential": ["writer", "editor"]},
            "network": {"name": "crew", "lead": "planner", "teammates": ["worker"]},
        }
    )

    assert bp.check() == []
    assert set(bp.used_structures()) == {"team", "workflow", "network"}
    assert bp.active_agents() == ["lead", "writer", "editor", "planner", "worker"]


def test_a_structure_containing_itself_is_an_error() -> None:
    bp = blueprint(
        team={"orchestrator": "lead", "members": ["clerk", "loop"]},
        workflow={"name": "loop", "sequential": ["desk", "cashier"]},
    )

    found = errors(bp.check())
    assert "contains itself" in found["team"]


def test_approvals_in_a_nested_network_are_errors() -> None:
    bp = network_blueprint(
        team={"orchestrator": "boss", "members": ["crew"]},
        network={"name": "crew", "lead": "lead", "teammates": ["clerk", "cashier"]},
        agents=[
            {"name": "boss", "description": "Boss."},
            {"name": "lead", "description": "Leads."},
            {"name": "clerk", "description": "Looks up.", "capabilities": ["lookup"]},
            {"name": "cashier", "description": "Refunds.", "tools": ["refund"]},
        ],
        approvals=[{"tool": "refund"}],
    )

    assert "only as the entrypoint" in errors(bp.check())["approvals[0].tool"]


def test_file_references_are_valid_import_references() -> None:
    bp = blueprint(approvals=[{"tool": "refund", "when": "./tools.py:needs_approval"}])

    assert "approvals[0].when" not in errors(bp.check())
    bad = blueprint(approvals=[{"tool": "refund", "when": "./tools.txt:x"}])
    assert "path/to/file.py:attribute" in errors(bad.check())["approvals[0].when"]


def design() -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "triage",
            "description": "Sorts the inbox.",
            "agents": [
                {"name": "lead", "description": "Leads.", "sub_agents": ["classifier"]},
                {
                    "name": "classifier",
                    "role": "worker",
                    "description": "Classifies every mail.",
                    "tools": ["mail_query"],
                },
                {
                    "name": "count_reviewer",
                    "role": "reviewer",
                    "reviews": "classifier",
                    "description": "Checks the counts.",
                    "tools": ["counts_reconcile", "mail_query"],
                },
            ],
            "entrypoint": "lead",
        }
    )


def tools(blueprint: Blueprint, name: str) -> list[str]:
    return blueprint.agent(name).tools


def test_the_worker_gets_the_rule_tool_of_its_reviewer_and_nothing_else() -> None:
    shared = share_check_tools(design(), {"counts_reconcile"})

    assert tools(shared, "classifier") == ["mail_query", "counts_reconcile"]
    assert tools(shared, "count_reviewer") == ["counts_reconcile", "mail_query"]


def test_without_a_rule_tool_on_a_checker_nothing_changes() -> None:
    original = design()

    assert share_check_tools(original, {"other_rule"}) is original


def test_the_architect_is_told_to_self_check_batch_and_keep_shared_keys() -> None:
    assert "checks its own" in ARCHITECT_INSTRUCTIONS and "in batches" in ARCHITECT_INSTRUCTIONS
    assert "same shared key" in ARCHITECT_INSTRUCTIONS


RULE = {
    "name": "needs_review",
    "description": "Amounts from 1000 on need a review.",
    "arguments": [{"name": "amount", "type": "number"}],
    "expression": "amount >= 1000",
    "examples": [
        {"arguments": '{"amount": 1500}', "result": "true"},
        {"arguments": '{"amount": 10}', "result": "false"},
    ],
}


def draft(*expectations: dict[str, Any], **extra: Any) -> AcceptanceDesign:
    case = {"id": "main", "input": "Check order 4711.", "expectations": list(expectations)}
    case.update(extra.pop("case", {}))
    return AcceptanceDesign.model_validate(
        {"criteria": ["Orders are checked."], "cases": [case], "rules": [RULE], **extra}
    )


def messages(design: AcceptanceDesign) -> list[str]:
    return [f"{p.path}: {p.message}" for p in acceptance_problems(design, set())]


def test_a_case_with_a_deterministic_check_and_one_short_judge_passes() -> None:
    design = draft(
        {"kind": "tool_called", "value": "needs_review"},
        {"kind": "judge", "value": "The answer says whether order 4711 needs a review."},
    )

    assert messages(design) == []


def test_a_case_of_judges_only_is_sent_back() -> None:
    [problem] = messages(draft({"kind": "judge", "value": "Polite."}))

    assert problem.startswith("cases[0].expectations: ") and "deterministic" in problem


def test_a_second_judge_and_a_long_rubric_are_sent_back() -> None:
    found = messages(
        draft(
            {"kind": "tool_called", "value": "needs_review"},
            {"kind": "judge", "value": "Polite."},
            {"kind": "judge", "value": " ".join(["word"] * 41)},
        )
    )

    assert any("cases[0].expectations[2]: at most one judge" in m for m in found)
    assert any("41 words" in m for m in found)


def test_a_tool_both_expected_and_refused_is_sent_back() -> None:
    found = messages(
        draft(
            {"kind": "tool_called", "value": "needs_review"},
            {"kind": "tool_not_called", "value": "needs_review"},
        )
    )

    assert found == [
        "cases[0].expectations[0]: 'needs_review' is expected (tool_called) and also expected "
        "not to be called; keep one"
    ]


def test_a_wrong_rule_example_is_reported_even_when_the_case_has_problems() -> None:
    wrong = dict(
        RULE, examples=[*RULE["examples"], {"arguments": '{"amount": 999}', "result": "true"}]
    )
    design = draft({"kind": "judge", "value": "Polite."}, rules=[wrong])

    found = messages(design)

    assert any(m.startswith("rules[0].examples[2]: ") for m in found)
    assert any("deterministic" in m for m in found)


YAML = """\
name: invoice_desk
agents:
  clerk: Check incoming invoices.
special_cases:
  decide:
    cases:
      - {if: [{field: amount_eur, op: ">=", value: 5000}], then: ask, by: finance}
      - {if: [{field: amount_eur, op: ">=", value: 20000}], then: ask, by: board}
      - {if: "status == 'paid'", then: go, to: archive}
    else: approve
    when_unclear: reject
  route:
    - {if: "country in ['DE', 'AT']", then: approve}
"""


def test_special_cases_load_by_name_with_the_short_form() -> None:
    blueprint = Blueprint.from_yaml_text(YAML)
    decide, route = blueprint.special_cases["decide"], blueprint.special_cases["route"]
    assert decide.decide({"amount_eur": 6000}).action.by == "finance"
    assert decide.decide({}).then == "reject"
    assert route.else_.then == "ask"
    assert route.decide({"country": "FR"}).source == "else"


def test_round_trip_through_yaml_keeps_the_tables() -> None:
    blueprint = Blueprint.from_yaml_text(YAML)
    again = Blueprint.from_yaml_text(blueprint.to_yaml())
    assert again.special_cases == blueprint.special_cases
    text = blueprint.to_yaml()
    assert (
        "    - if:\n" in text and "    else: approve\n" in text and "when_unclear: reject" in text
    )


PROCESS = """\
process:
  - {name: decide, agent: clerk}
  - {name: archive, agent: clerk}
  - {name: route, agent: clerk}
"""


def test_tables_for_steps_the_process_does_not_have_are_errors() -> None:
    issues = Blueprint.from_yaml_text(YAML).check()
    found = errors(issues)
    assert "which the process does not have" in found["special_cases.decide"]
    assert "which the process does not have" in found["special_cases.route"]
    assert not any("does not apply them" in issue.message for issue in issues)


def test_check_reports_else_approve_and_shadowed_rows_of_tables_bound_to_steps() -> None:
    issues = Blueprint.from_yaml_text(YAML + PROCESS).check()
    found = {(issue.severity, issue.path, issue.message.split(":")[0]) for issue in issues}
    assert ("warning", "special_cases.decide.else", "else is 'approve'") in found
    shadowed = [i for i in issues if i.path == "special_cases.decide.cases[1]"]
    assert [i.message for i in shadowed] == [
        "Row 2 never applies: row 1 already decides every amount it covers."
    ]
    assert not [i for i in issues if i.severity == "error"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("decide:", "9decide:"), "special_cases name '9decide' is not usable"),
        (("when_unclear: reject", "when_unclear: approve"), "when_unclear cannot be 'approve'"),
        (('op: ">=", value: 5000', 'op: ">=", value: "5.000"'), "two different numbers"),
        (('op: ">=", value: 5000', 'op: ">=", value: high'), "needs a number"),
        (("then: go, to: archive", "then: go"), "'go' needs the step"),
    ],
)
def test_invalid_tables_fail_to_load_with_the_fix(change: tuple[str, str], message: str) -> None:
    with pytest.raises(BlueprintError, match=message):
        Blueprint.from_yaml_text(YAML.replace(*change, 1))


def test_a_blueprint_without_special_cases_is_unchanged() -> None:
    blueprint = Blueprint.from_yaml_text("name: plain\nagents:\n  clerk: Check invoices.\n")
    assert blueprint.special_cases == {}
    assert "special_cases" not in blueprint.to_yaml()
    assert not [i for i in blueprint.check() if i.path.startswith("special_cases")]


@pytest.mark.parametrize("where", ["trigger", "automation"])
def test_processed_path_in_any_input_template_is_warned_about(where: str) -> None:
    template = "Read {payload.processed_path}"
    trigger: dict[str, Any] = {"name": "drop", "file_drop": {"directory": "inbox"}}
    automation: dict[str, Any] = {"triggers": [trigger]}
    if where == "trigger":
        trigger["input"] = template
    else:
        automation["input"] = template
    bp = Blueprint.from_dict({"name": "d", "agents": [{"name": "a"}], "automation": automation})

    assert any("processed_path only after its run" in i.message for i in bp.check())
