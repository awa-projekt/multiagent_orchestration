"""Auto-matching of builtin skills and tools, and the pattern catalog with its suggestion and test."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from awa import Agent, Blueprint, Team
from awa.blueprints import (
    PATTERNS,
    BlueprintDesign,
    Pattern,
    PatternError,
    PatternProposal,
    explain,
    suggest_patterns,
    try_patterns,
)
from awa.blueprints._patterns import _Ranking, pattern_by_id
from awa.blueprints._project import AcceptanceDesign
from awa.blueprints.auto import MAX_AUTO_SKILLS, MAX_AUTO_TOOLS, ROLE_SKILL, AutoReport, match_agent
from awa.contracts.usage import UsageLimits
from awa.errors import ConfigurationError, UsageLimitExceededError
from awa.models import ModelRegistry
from awa.skills import Skill, SkillRegistry
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output
from awa.toolkits import catalog


@tool
def calculator(expression: str) -> str:
    """My own calculator."""
    return expression


def _blueprint(text: str) -> Blueprint:
    return Blueprint.from_yaml_text(text)


INVOICES = """
name: invoice_processing
auto: true
agents:
  coordinator: Coordinates the processing of incoming invoices.
  extractor: Reads incoming invoices and extracts the fields.
  checker: Validates the totals against the purchase order.
  notifier: E-mails the supplier about mismatches.
team: coordinator
"""


def test_skills_and_tools_match_the_task_and_reasons_name_the_words() -> None:
    report = match_agent("checker", "Validates the totals against the purchase order.")

    assert report.skills == ("data-validation", "invoice-reconciliation")
    assert report.tools == ("builtin:calculator",)
    calculator_match = report.granted[-1]
    assert calculator_match.reason == "matched: totals" and calculator_match.kind == "tool"


def test_vague_tasks_match_nothing() -> None:
    report = match_agent("helper", "Answers questions.")

    assert report.matches == () and report.auto


@pytest.mark.parametrize(
    ("task", "tool"),
    [
        ("E-mails the supplier about mismatches.", "builtin:send_email"),
        ("Posts a summary to the Slack channel.", "builtin:post_webhook"),
        ("Updates the database with the new totals.", "builtin:sql_execute"),
        ("Saves the report to a file with write file.", "builtin:write_file"),
    ],
)
def test_tools_that_act_outside_are_only_suggested(task: str, tool: str) -> None:
    report = match_agent(
        "worker", task, toolkits={"files": {"root": "."}, "sql": {"database": "x"}}
    )

    assert tool not in report.tools
    suggestion = next(m for m in report.suggested if m.name == tool)
    assert not suggestion.granted and "outside the agent" in suggestion.note


def test_unconfigured_toolkits_are_suggested_and_configured_ones_granted() -> None:
    task = "Analyses the sales CSV file."
    bare = match_agent("analyst", task)
    configured = match_agent("analyst", task, toolkits={"files": {"root": "./data"}})

    assert "builtin:read_csv" not in bare.tools
    note = next(m.note for m in bare.suggested if m.name == "builtin:read_csv")
    assert "toolkits.data.root" in note
    assert "builtin:read_csv" in configured.tools


def test_caps_limit_skills_and_tools() -> None:
    task = (
        "Calculates totals, due dates and business days, converts units, generates uuid "
        "identifiers, renders templates, reads json payloads, splits long text into chunks, "
        "reconciles invoices with purchase orders and goods receipts, validates data quality "
        "rules and writes reports with kpis for stakeholders."
    )
    report = match_agent("clerk", task, role="lead")

    assert len(report.skills) == MAX_AUTO_SKILLS and report.skills[0] == ROLE_SKILL
    assert len(report.tools) == MAX_AUTO_TOOLS


def test_explicit_tools_and_skills_win() -> None:
    report = match_agent(
        "checker",
        "Validates the totals against the purchase order.",
        explicit_tools=["calculator"],
        explicit_skills=["data-validation"],
    )

    assert "builtin:calculator" not in report.tools
    assert "data-validation" not in report.skills
    assert report.explicit_tools == ("calculator",)


def test_every_builtin_tool_has_auto_matching_keywords() -> None:
    assert all(entry.keywords for entry in catalog())


def test_custom_skill_registries_are_matched() -> None:
    registry = SkillRegistry(
        [
            Skill(
                name="refund-policy",
                description="How refunds are decided.",
                instructions="Refund within 30 days.",
                tags=("refunds", "returns"),
            )
        ]
    )

    report = match_agent("refunds", "Decides refunds for returns.", skills=registry)

    assert report.skills == ("refund-policy",)


def test_blueprint_auto_adds_matches_when_building() -> None:
    blueprint = _blueprint(INVOICES)

    assert all("auto-matching suggests" in issue.message for issue in blueprint.check())
    team = blueprint.build()

    assert isinstance(team, Team)
    agents = {agent.name: agent for agent in _agents(team)}
    assert agents["checker"].card.skills == ("data-validation", "invoice-reconciliation")
    assert "calculator" in agents["checker"].card.tools
    assert agents["coordinator"].card.skills[0] == ROLE_SKILL
    assert "send_email" not in agents["notifier"].card.tools
    report = explain(team)
    assert report.blueprint == blueprint
    assert report.for_agent("notifier").suggested[0].name == "builtin:send_email"


def _agents(team: Team) -> list[Agent[Any, Any]]:
    found = [team.orchestrator]
    found.extend(m for m in team.members if isinstance(m, Agent))
    return found


def test_check_warns_once_per_agent_about_suggestions() -> None:
    [warning] = _blueprint(INVOICES).check()

    assert warning.severity == "warning" and warning.path == "agents[3]"
    assert "builtin:send_email" in warning.message and "auto: false" in warning.message


def test_auto_can_be_turned_off_per_agent_and_on_per_agent() -> None:
    blueprint = _blueprint(
        """
name: desk
agents:
  checker: {description: Validates the totals against the purchase order., auto: true}
  notifier: {description: E-mails the supplier., auto: false}
workflow: [checker, notifier]
"""
    )

    report = blueprint.explain()

    assert report.for_agent("checker").auto and report.for_agent("checker").tools
    assert not report.for_agent("notifier").auto and not report.for_agent("notifier").matches
    assert blueprint.check() == []


def test_blueprints_without_auto_are_unchanged() -> None:
    blueprint = _blueprint(INVOICES.replace("auto: true\n", ""))

    team = blueprint.build()

    assert not blueprint.uses_auto()
    assert isinstance(team, Team)
    assert all(agent.card.skills == () for agent in _agents(team))
    assert "(auto off)" in str(explain(team))


def test_user_tools_named_like_builtins_are_not_duplicated() -> None:
    blueprint = _blueprint(
        """
name: desk
auto: true
agents:
  - name: checker
    description: Validates the totals against the purchase order.
    tools: [calculator]
"""
    )

    agent = blueprint.build(tools=[calculator])

    assert isinstance(agent, Agent)
    assert agent.card.tools.count("calculator") == 1
    # The user tool itself, wrapped only with the default call timeout.
    assert (
        agent.tools[0].name == "calculator" and agent.tools[0].description == calculator.description
    )


def test_report_text_and_data() -> None:
    report = _blueprint(INVOICES).explain()
    text = str(report)

    assert text.splitlines()[0] == "auto-matching for invoice_processing"
    assert "    + skill data-validation (matched: checker, validates)" in text
    assert "    ? tool  builtin:send_email (matched: e-mail); not granted:" in text
    data = report.to_dict()
    assert data["system"] == "invoice_processing"
    notifier = data["agents"][3]
    assert notifier["granted"] == [] and notifier["suggested"][0]["granted"] is False
    assert report.suggested[0][0] == "notifier"
    with pytest.raises(KeyError):
        report.for_agent("nobody")


def test_skills_given_as_names_limit_matching() -> None:
    report = _blueprint(INVOICES).explain(skills={"data-validation"})

    assert report.for_agent("checker").skills == ("data-validation",)
    assert report.for_agent("coordinator").skills == ()


def test_explain_needs_a_system_built_from_a_blueprint() -> None:
    with pytest.raises(ConfigurationError, match="no auto-matching report"):
        explain(Agent("plain"))
    assert isinstance(explain(_blueprint(INVOICES)), AutoReport)


PROCESS = "Every invoice is checked and then booked."


def agent(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "description": f"{name}.", "instructions": f"Act as {name}.", **extra}


DESIGNS: dict[str, dict[str, Any]] = {
    "ONE agent": {"agents": [agent("solo")], "entrypoint": "agent"},
    "fixed order": {
        "agents": [agent("first_step"), agent("second_step")],
        "entrypoint": "workflow",
        "workflow": [{"agents": ["first_step"]}, {"agents": ["second_step"]}],
    },
    "EXACTLY two agents": {
        "agents": [agent("maker"), agent("reviewer")],
        "entrypoint": "workflow",
        "workflow": [{"agents": ["maker"]}, {"agents": ["reviewer"]}],
    },
    "in parallel": {
        "agents": [agent("one"), agent("two"), agent("merge")],
        "entrypoint": "workflow",
        "workflow": [{"agents": ["one", "two"]}, {"agents": ["merge"]}],
    },
    "coordinating orchestrator": {
        "agents": [agent("lead"), agent("helper")],
        "entrypoint": "team",
        "team": {"orchestrator": "lead", "members": ["helper"]},
    },
    "front desk": {
        "agents": [agent("desk"), agent("expert")],
        "entrypoint": "team",
        "team": {
            "orchestrator": "desk",
            "members": ["expert"],
            "handoffs": [{"source": "desk", "targets": ["expert"]}],
        },
    },
    "network of concurrent": {
        "agents": [agent("boss"), agent("worker", capabilities=["work"])],
        "entrypoint": "team",
        "team": {"orchestrator": "boss", "members": ["worker"]},
    },
}

ACCEPTANCE = {
    "criteria": ["Invoices are booked."],
    "cases": [
        {
            "id": "first",
            "input": "Invoice 1",
            "expectations": [{"kind": "contains", "value": "done"}],
        },
        {
            "id": "second",
            "input": "Invoice 2",
            "expectations": [{"kind": "contains", "value": "done"}],
        },
    ],
}


def scripted(*, broken: str = "", answer: str = "done") -> FunctionChatModel:
    """Test designer, architect and every designed agent in one model."""

    def respond(call: ModelCall) -> Any:
        first = call.messages[0]
        system = str(first.content) if isinstance(first, SystemMessage) else ""
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, ACCEPTANCE)
        if system.startswith("You design multi-agent systems"):
            humans = [str(m.content) for m in call.messages if isinstance(m, HumanMessage)]
            guidance = humans[-1].split("## Structure to build", 1)[-1]
            for marker, design in DESIGNS.items():
                if marker in guidance:
                    if marker == broken:
                        design = {**design, "workflow": [{"agents": ["ghost"]}]}
                    name = marker.replace(" ", "_").lower()
                    data = {"name": name, "description": "A sample.", **design}
                    return structured_output(BlueprintDesign, data)
            data = {"name": "plain", "description": "A sample.", **DESIGNS["ONE agent"]}
            return structured_output(BlueprintDesign, data)
        if "start your answer with APPROVED" in system:
            return f"APPROVED {answer}"
        return answer

    return FunctionChatModel(respond)


def proposals(*ids: str, gate: bool = False) -> list[PatternProposal]:
    return [PatternProposal(pid, "because", "", gate) for pid in ids]


def test_the_catalog_names_the_seven_shapes_awa_builds() -> None:
    ids = [p.id for p in PATTERNS]

    assert ids == [
        "single-agent",
        "pipeline",
        "review-loop",
        "fan-out",
        "coordinator-team",
        "handoff-desk",
        "parallel-network",
    ]


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.id)
def test_every_sample_is_a_valid_blueprint_that_builds(pattern: Pattern) -> None:
    blueprint = pattern.blueprint()
    models = ModelRegistry()
    models.register("default", FakeChatModel(["ok"] * 5))

    assert blueprint.check() == []
    assert blueprint.build(model_registry=models) is not None
    expected = {"workflow": "workflow", "team": "team", "network": "network"}.get(pattern.structure)
    if expected:
        assert blueprint.resolved_entrypoint() == expected


async def test_words_pick_the_pipeline_for_steps_that_never_change() -> None:
    got = await suggest_patterns("We always do the same steps for every order.")

    assert got[0].pattern_id == "pipeline"
    assert got[0].source == "words" and "same steps" in got[0].why


async def test_words_pick_the_network_for_work_at_the_same_time() -> None:
    got = await suggest_patterns("Many independent tasks run at the same time and are combined.")

    assert got[0].pattern_id == "parallel-network"


async def test_words_flag_a_process_that_needs_an_approval() -> None:
    got = await suggest_patterns("A manager must approve the payment, then it is booked.")

    assert all(p.approval_gate for p in got)


async def test_a_description_without_cues_still_gets_proposals() -> None:
    got = await suggest_patterns("Gardening.", count=2)

    assert [p.pattern_id for p in got] == ["single-agent", "pipeline"]


@pytest.mark.parametrize(("text", "count"), [("", 3), ("  ", 3), ("x", 0), ("x", 4)])
async def test_bad_input_is_refused(text: str, count: int) -> None:
    with pytest.raises(PatternError):
        await suggest_patterns(text, count=count)


async def test_the_model_ranks_inside_the_catalog_and_python_repairs_the_answer() -> None:
    answer = {
        "choices": [
            {"pattern_id": "made-up", "why": "no such pattern"},
            {"pattern_id": "Fan-Out", "why": "  Several\nsides.  ", "risks": "x" * 500},
            {"pattern_id": "fan-out", "why": "repeated"},
            {"pattern_id": "pipeline", "why": ""},
            {"pattern_id": "review-loop", "why": "Quality bar."},
            {"pattern_id": "single-agent", "why": "A third."},
        ],
        "approval_gate": True,
    }
    model = FakeChatModel([structured_output(_Ranking, answer)])

    got = await suggest_patterns(PROCESS, model=model, count=2)

    assert [p.pattern_id for p in got] == ["fan-out", "review-loop"]
    assert got[0].why == "Several sides." and len(got[0].risks) <= 200
    assert got[0].source == "model" and got[0].approval_gate
    assert len(model.calls) == 1
    prompt = str(model.calls[0].last_message.content)
    assert "<process_description>" in prompt and PROCESS in prompt


async def test_an_unusable_model_answer_falls_back_to_the_words() -> None:
    unusable = {"choices": [{"pattern_id": "nope", "why": "x"}]}
    model = FakeChatModel([structured_output(_Ranking, unusable)])

    got = await suggest_patterns("Always the same steps.", model=model)

    assert got[0].pattern_id == "pipeline" and got[0].source == "words"


def test_an_unknown_pattern_is_named_with_the_known_ones() -> None:
    with pytest.raises(PatternError, match="pipeline"):
        pattern_by_id("nope")


async def test_three_patterns_run_on_the_same_cases_and_are_compared() -> None:
    result = await try_patterns(
        PROCESS,
        proposals("single-agent", "pipeline", "review-loop"),
        model=scripted(),
        environment=None,
    )

    assert result.cases == ("first", "second")
    assert [t.pattern_id for t in result.trials] == ["single-agent", "pipeline", "review-loop"]
    assert [t.status for t in result.trials] == ["scored"] * 3
    assert all(t.passed == t.total == 2 and t.pass_rate == 1.0 for t in result.trials)
    assert not result.capped and result.requests == sum(t.requests for t in result.trials) + 2
    loop = result.trials[2].design  # the loop is made by Python around the reviewer
    assert loop is not None and "loop" in loop.blueprint.to_yaml()
    assert result.best is not None and result.best.pattern_id == "single-agent"
    assert "One agent" in result.table()
    assert result.to_dict()["best"] == "single-agent"


async def test_a_network_pattern_is_drafted_as_a_team_and_turned_into_a_network() -> None:
    result = await try_patterns(
        PROCESS, proposals("parallel-network"), model=scripted(), environment=None
    )

    design = result.trials[0].design
    assert design is not None
    assert design.blueprint.network is not None and design.blueprint.team is None
    assert design.blueprint.resolved_entrypoint() == "network"


async def test_a_failing_pattern_is_reported_not_hidden() -> None:
    result = await try_patterns(
        PROCESS,
        proposals("single-agent", "pipeline"),
        model=scripted(broken="fixed order"),
        environment=None,
    )

    single, broken = result.trials
    assert single.status == "scored"
    assert broken.status == "failed" and broken.verdict.startswith("No valid blueprint")
    assert broken.design is None and result.best == single


async def test_wrong_answers_show_as_failed_cases() -> None:
    result = await try_patterns(
        PROCESS, proposals("single-agent"), model=scripted(answer="nope"), environment=None
    )

    trial = result.trials[0]
    assert trial.status == "scored" and trial.pass_rate == 0.0
    assert result.best is None and result.to_dict()["best"] is None
    assert trial.failed_cases == ("first", "second")


async def test_the_request_cap_stops_the_run_and_says_so() -> None:
    result = await try_patterns(
        PROCESS,
        proposals("pipeline", "review-loop", "single-agent"),
        model=scripted(),
        usage_limits=UsageLimits(max_requests=8),
        environment=None,
    )

    assert result.capped and result.requests <= 8
    assert result.trials[0].status == "scored"
    assert [t.status for t in result.trials[1:]] == ["stopped", "stopped"]
    assert "cap of 8" in result.trials[1].verdict


async def test_a_cap_that_cannot_cover_the_design_is_an_error() -> None:
    with pytest.raises(UsageLimitExceededError):
        await try_patterns(
            PROCESS,
            proposals("pipeline"),
            model=scripted(),
            usage_limits=UsageLimits(max_requests=1),
            environment=None,
        )


@pytest.mark.parametrize("count", [0, 4])
async def test_between_one_and_three_patterns_are_tested(count: int) -> None:
    with pytest.raises(PatternError, match="between 1 and 3"):
        await try_patterns(PROCESS, proposals(*["pipeline"] * count), model=scripted())


async def test_the_approval_gate_is_added_to_the_instructions_of_the_architect() -> None:
    model = scripted()

    await try_patterns(PROCESS, proposals("single-agent", gate=True), model=model, environment=None)

    prompts = [str(c.last_message.content) for c in model.calls]
    assert any("wait for a person's approval" in p for p in prompts)
