"""Role contract in blueprints: schema, check rules (errors and warnings), negative cases."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from awa.blueprints import Blueprint, BlueprintError

LONG = "Every factual claim in the brief is followed by a URL that supports it"


def base() -> dict[str, Any]:
    """A valid role tree: lead over a researcher and a checked writer with two reviewers."""

    return {
        "name": "brief",
        "limits": {"max_requests": 50},
        "toolkits": {"web": {"allowed_hosts": ["docs.example.com"]}},
        "agents": [
            {
                "name": "lead",
                "role": "orchestrator",
                "description": "Coordinates the brief.",
                "sub_agents": ["researcher", "writer"],
            },
            {
                "name": "researcher",
                "role": "worker",
                "description": "Finds facts.",
                "tools": ["builtin:web_search"],
                "model": "fast",
            },
            {
                "name": "writer",
                "role": "worker",
                "description": "Writes the brief.",
                "model": "fast",
            },
            {
                "name": "fact_judge",
                "role": "judge",
                "judges": "writer",
                "description": "Gates the brief.",
                "rubric": [LONG],
                "tools": ["builtin:fetch_url"],
                "model": "strong",
            },
            {
                "name": "tone_reviewer",
                "role": "reviewer",
                "reviews": "writer",
                "description": "Reads the tone.",
                "rubric": ["The tone is neutral and professional without marketing wording"],
            },
        ],
    }


def agent(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in data["agents"] if entry["name"] == name)


def issues(data: dict[str, Any], severity: str | None = None) -> list[tuple[str, str, str]]:
    found = Blueprint.from_dict(data).check()
    return [
        (i.severity, i.path, i.message) for i in found if severity is None or i.severity == severity
    ]


def messages(data: dict[str, Any], severity: str) -> str:
    return "\n".join(f"{path}: {message}" for _, path, message in issues(data, severity))


def test_the_base_tree_is_valid_and_has_a_root_entrypoint() -> None:
    blueprint = Blueprint.from_dict(base())

    assert [i for i in blueprint.check() if i.severity == "error"] == []
    assert blueprint.resolved_entrypoint() == "lead"
    assert blueprint.active_agents() == [
        "lead",
        "researcher",
        "writer",
        "fact_judge",
        "tone_reviewer",
    ]


def test_the_role_fields_round_trip_through_yaml() -> None:
    blueprint = Blueprint.from_dict(base())

    again = Blueprint.from_yaml_text(blueprint.to_yaml())

    assert again == blueprint
    assert again.agent("fact_judge").role == "judge"
    assert again.agent("lead").sub_agents == ["researcher", "writer"]


def test_a_blueprint_without_roles_is_unchanged() -> None:
    blueprint = Blueprint.from_dict({"name": "x", "agents": [{"name": "a"}, {"name": "b"}]})

    assert blueprint.resolved_entrypoint() is None
    assert "role" not in blueprint.agent("a").model_dump(exclude_defaults=True)


def test_an_unknown_role_is_rejected_by_the_schema() -> None:
    data = base()
    agent(data, "lead")["role"] = "boss"

    with pytest.raises(BlueprintError, match="role"):
        Blueprint.from_dict(data)


def test_a_single_rubric_string_is_one_criterion() -> None:
    data = base()
    agent(data, "fact_judge")["rubric"] = LONG

    assert Blueprint.from_dict(data).agent("fact_judge").rubric == [LONG]


def test_an_unknown_agent_field_is_rejected() -> None:
    data = base()
    agent(data, "lead")["roles"] = "x"

    with pytest.raises(BlueprintError):
        Blueprint.from_dict(data)


def test_a_judge_without_a_rubric_is_an_error() -> None:
    data = base()
    agent(data, "fact_judge")["rubric"] = []

    assert "needs a rubric" in messages(data, "error")


def test_a_judge_without_a_worker_is_an_error() -> None:
    data = base()
    del agent(data, "fact_judge")["judges"]

    assert "needs `judges: <worker>`" in messages(data, "error")


def test_a_judge_of_an_unknown_worker_is_an_error() -> None:
    data = base()
    agent(data, "fact_judge")["judges"] = "ghost"

    assert "unknown worker 'ghost'" in messages(data, "error")


def test_a_judge_of_a_non_worker_is_an_error() -> None:
    data = base()
    agent(data, "fact_judge")["judges"] = "lead"

    assert "not a worker" in messages(data, "error")


def test_a_worker_has_one_judge() -> None:
    data = base()
    second = copy.deepcopy(agent(data, "fact_judge"))
    second["name"] = "second_judge"
    data["agents"].append(second)

    assert "already has the judge 'fact_judge'" in messages(data, "error")


@pytest.mark.parametrize("role", ["judge", "reviewer"])
@pytest.mark.parametrize("tool", ["send_email", "delete_customer"])
def test_judge_and_reviewer_with_a_write_tool_are_errors(role: str, tool: str) -> None:
    data = base()
    target = agent(data, "fact_judge" if role == "judge" else "tone_reviewer")
    target["tools"] = ["builtin:fetch_url", tool]

    found = messages(data, "error")

    assert "must be read-only" in found
    assert tool in found


def test_open_web_reading_is_not_read_only_until_hosts_are_allowlisted() -> None:
    data = base()
    del data["toolkits"]

    found = messages(data, "error")

    assert "must be read-only" in found
    assert "builtin:fetch_url" in found
    assert "toolkits.web.allowed_hosts" in found


def test_a_write_tool_flagged_through_tool_risks_is_an_error_for_a_reviewer() -> None:
    data = base()
    agent(data, "tone_reviewer")["tools"] = ["archive_ticket"]
    data["tool_risks"] = {"archive_ticket": ["external_action"]}

    assert "must be read-only" in messages(data, "error")


@pytest.mark.parametrize("role", ["judge", "reviewer"])
def test_a_plugin_tool_of_a_judge_or_reviewer_must_be_declared_read_only(role: str) -> None:
    """A plugin's tools are only known when it loads; the check must not guess from the name."""

    data = base()
    target = agent(data, "fact_judge" if role == "judge" else "tone_reviewer")
    target["tools"] = ["plugin:acme-tools/lookup"]

    assert "plugin:acme-tools/lookup" in messages(data, "error")

    data["tool_risks"] = {"plugin:acme-tools/lookup": {"external_action": False}}
    assert "plugin:acme-tools/lookup" not in messages(data, "error")


def test_a_reviewer_cannot_hide_a_write_tool_behind_an_approval() -> None:
    data = base()
    agent(data, "tone_reviewer")["tools"] = ["send_email"]
    data["approvals"] = [{"tool": "send_email"}]

    assert "must be read-only" in messages(data, "error")


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("memory", {"scope": "agent"}, "cannot have memory"),
        ("output_schema", {"type": "object"}, "fixed output"),
        ("sub_agents", ["researcher"], "cannot have sub-agents"),
    ],
)
def test_judge_and_reviewer_cannot_have_memory_output_schema_or_sub_agents(
    field: str, value: Any, fragment: str
) -> None:
    for name in ("fact_judge", "tone_reviewer"):
        data = base()
        agent(data, name)[field] = value

        assert fragment in messages(data, "error")


def test_role_fields_on_the_wrong_role_are_errors() -> None:
    data = base()
    agent(data, "writer")["judges"] = "researcher"
    agent(data, "writer")["reviews"] = "researcher"
    agent(data, "writer")["rubric"] = ["Some criterion that has many words in it"]

    found = messages(data, "error")

    assert "`judges` is for agents with role: judge" in found
    assert "`reviews` is for agents with role: reviewer" in found
    assert "`rubric` is for a judge or a reviewer" in found


def test_unknown_sub_agent_self_duplicate_and_two_parents_are_errors() -> None:
    data = base()
    agent(data, "lead")["sub_agents"] = ["researcher", "researcher", "lead", "ghost", "writer"]
    agent(data, "writer")["sub_agents"] = ["researcher"]

    found = messages(data, "error")

    assert "unknown sub-agent 'ghost'" in found
    assert "listed twice" in found
    assert "its own sub-agent" in found
    assert "'researcher' is a sub-agent of lead and writer" in found


def test_a_cycle_is_an_error() -> None:
    data = base()
    agent(data, "researcher")["sub_agents"] = ["lead"]

    assert "sub_agents form a cycle" in messages(data, "error")


def test_a_judge_listed_as_a_sub_agent_is_an_error() -> None:
    data = base()
    agent(data, "lead")["sub_agents"].append("fact_judge")

    assert "is a judge" in messages(data, "error")


def test_a_bound_reviewer_listed_as_a_sub_agent_is_an_error_but_a_free_one_is_fine() -> None:
    data = base()
    agent(data, "lead")["sub_agents"].append("tone_reviewer")
    assert "so it is not also a sub-agent" in messages(data, "error")

    free = base()
    del agent(free, "tone_reviewer")["reviews"]
    agent(free, "lead")["sub_agents"].append("tone_reviewer")
    assert messages(free, "error") == ""


def test_the_tree_depth_is_limited_to_three_by_default() -> None:
    data = base()
    names = ["d1", "d2", "d3"]
    chain = [{"name": n, "role": "worker", "description": "x"} for n in names]
    agent(data, "researcher")["sub_agents"] = ["d1"]
    chain[0]["sub_agents"] = ["d2"]
    chain[1]["sub_agents"] = ["d3"]
    data["agents"].extend(chain)

    assert "levels deep" in messages(data, "error")

    agent(data, "lead")["delegation"] = {"max_depth": 4}
    assert messages(data, "error") == ""


def test_a_checked_worker_counts_one_level_for_its_judge() -> None:
    data = base()
    agent(data, "lead")["delegation"] = {"max_depth": 1}
    data["agents"][2]["sub_agents"] = []

    assert "levels deep" in messages(data, "error")


def test_the_root_must_be_an_orchestrator() -> None:
    data = base()
    agent(data, "lead")["role"] = "worker"

    assert "must have role: orchestrator" in messages(data, "error")


def test_two_roots_need_an_entrypoint() -> None:
    data = base()
    data["agents"].append({"name": "other", "role": "orchestrator", "sub_agents": ["extra"]})
    data["agents"].append({"name": "extra", "role": "worker", "description": "x"})

    assert "give the system one root" in messages(data, "error")


def test_roles_cannot_be_mixed_with_a_team_section() -> None:
    data = base()
    data["team"] = {"orchestrator": "lead", "members": ["researcher"]}

    assert "cannot be combined with a `team:` section" in messages(data, "error")


def test_roles_cannot_be_mixed_with_a_process_section() -> None:
    data = base()
    data["process"] = {"steps": [{"name": "write", "agent": "writer"}]}

    assert "cannot be combined with a `process:` section" in messages(data, "error")


def test_a_judge_without_any_tree_is_an_error() -> None:
    data = {
        "name": "x",
        "agents": [
            {"name": "w", "role": "worker"},
            {"name": "j", "role": "judge", "judges": "w", "rubric": [LONG]},
        ],
    }

    assert "need a tree" in messages(data, "error")


def test_a_judge_whose_worker_is_outside_the_tree_is_an_error() -> None:
    data = base()
    data["agents"].append({"name": "loose", "role": "worker", "description": "x"})
    data["agents"].append(
        {"name": "loose_judge", "role": "judge", "judges": "loose", "rubric": [LONG]}
    )

    assert "would never run" in messages(data, "error")


def test_generated_names_may_not_clash() -> None:
    data = base()
    data["agents"].append({"name": "writer_checked", "role": "worker"})

    assert "is used for the unit built around 'writer'" in messages(data, "error")


def test_the_blueprint_name_may_not_be_an_agent_name() -> None:
    data = base()
    data["name"] = "lead"

    assert "also an agent name" in messages(data, "error")


def test_max_iterations_and_delegation_belong_on_agents_with_sub_agents() -> None:
    data = base()
    agent(data, "writer")["max_iterations"] = 2
    agent(data, "writer")["delegation"] = {"max_parallel": 3}

    found = messages(data, "error")

    assert "`max_iterations` belongs on the orchestrator" in found
    assert "`delegation` needs `sub_agents`" in found


@pytest.mark.parametrize("value", [0, 11])
def test_max_iterations_is_one_to_ten(value: int) -> None:
    data = base()
    agent(data, "lead")["max_iterations"] = value

    with pytest.raises(BlueprintError):
        Blueprint.from_dict(data)


def test_the_base_tree_has_no_warning() -> None:
    assert messages(base(), "warning") == ""


def test_a_judge_with_the_workers_model_and_tools_warns() -> None:
    data = base()
    agent(data, "fact_judge")["model"] = "fast"
    agent(data, "fact_judge")["tools"] = []

    found = messages(data, "warning")

    assert "uses the same model as worker 'writer'" in found
    assert "same tools as worker 'writer' (none)" in found


def test_a_judge_without_any_model_set_shares_the_default_with_its_worker() -> None:
    data = base()
    for name in ("writer", "fact_judge"):
        del agent(data, name)["model"]

    assert "same model" in messages(data, "warning")


@pytest.mark.parametrize("criterion", ["is good", "Correct and accurate", "quality"])
def test_a_vague_criterion_warns(criterion: str) -> None:
    data = base()
    agent(data, "fact_judge")["rubric"] = [criterion]

    assert "is vague" in messages(data, "warning")


def test_an_orchestrator_with_tools_warns() -> None:
    data = base()
    agent(data, "lead")["tools"] = ["builtin:fetch_url"]

    assert "plans and delegates" in messages(data, "warning")


def test_a_role_tree_without_limits_warns() -> None:
    data = base()
    del data["limits"]

    assert "add `limits:`" in messages(data, "warning")


def test_many_judge_rounds_warn() -> None:
    data = base()
    agent(data, "lead")["max_iterations"] = 8

    assert "8 judge rounds is a lot" in messages(data, "warning")


def test_an_agent_outside_the_tree_warns() -> None:
    data = base()
    data["agents"].append({"name": "idle", "role": "worker", "description": "x"})

    assert "agents outside the tree are never run: idle" in messages(data, "warning")


def test_a_reviewer_cannot_hold_a_connector_write_operation() -> None:
    data = base()
    agent(data, "tone_reviewer")["tools"] = ["connector:billing.create_invoice"]
    data["connectors"] = {"billing": {"use": "rest", "base_url": "https://x.example.com"}}

    assert "must be read-only" in messages(data, "error")


def test_auto_matching_does_not_hand_a_reviewer_a_tool_that_sends() -> None:
    data = base()
    data["auto"] = True
    agent(data, "tone_reviewer")["description"] = (
        "Reads the tone and sends an email with the result"
    )

    blueprint = Blueprint.from_dict(data)

    assert [i for i in blueprint.check() if i.severity == "error"] == []
    report = blueprint.explain()
    granted = {
        match.name
        for entry in report.agents
        if entry.agent == "tone_reviewer"
        for match in entry.matches
        if match.granted
    }
    assert "builtin:send_email" not in granted
