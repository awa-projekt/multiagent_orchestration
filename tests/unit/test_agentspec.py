"""Agent specs: the optional fields, the system message renderer, and lint of specs and tool cards."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from _helpers.fake_secrets import fake_secret
from awa import tool
from awa.blueprints import Blueprint
from awa.blueprints._agent_facts import effective_instructions, renders_sections, system_message
from awa.blueprints._lint import lint_blueprint
from awa.blueprints._resolve import ToolCatalog, load_tools, tools_files
from awa.blueprints._system_message import HOUSE_RULES
from awa.blueprints._tool_cards import ToolCard, card_of, lint_card, sentences
from awa.blueprints.errors import BlueprintError
from awa.skills import SkillRegistry
from awa.tools.risk import ToolRisk

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "agentspec" / "examples"
SKILLS = [skill.name for skill in SkillRegistry.builtin().all()]


@pytest.mark.parametrize("name", ["worker", "judge", "reviewer", "orchestrator"])
def test_the_four_design_examples_are_lint_clean_in_the_strict_profile(name: str) -> None:
    bp = Blueprint.from_yaml_file(EXAMPLES / f"{name}.yaml")

    assert lint_blueprint(bp, profile="generated", skills=SKILLS) == []


def _repository_blueprints() -> list[Path]:
    paths = sorted((ROOT / "examples").rglob("*.yaml"))
    return [path for path in paths if "agents" in (_yaml_mapping(path) or {})]


def _yaml_mapping(path: Path) -> dict[str, object] | None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


@pytest.mark.parametrize("path", _repository_blueprints(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_repository_example_has_no_lint_errors(path: Path) -> None:
    bp = Blueprint.from_yaml_file(path)
    specs = tools_files([], path)
    catalog = ToolCatalog.from_source(load_tools(specs)) if specs else None

    errors = [f for f in lint_blueprint(bp, tools=catalog, skills=SKILLS) if f.severity == "error"]
    assert errors == []


def _lint(text: str, **options: object) -> dict[str, list[str]]:
    bp = Blueprint.from_yaml_text(text)
    found: dict[str, list[str]] = {}
    for finding in lint_blueprint(bp, skills=SKILLS, **options):  # type: ignore[arg-type]
        found.setdefault(finding.code, []).append(finding.severity)
    return found


_BASE = """
name: p
toolkits: {data: {root: .}}
data: [{name: prices, path: prices.csv, role: reference}]
agents:
  - name: a
    description: Checks prices. Use for every invoice.
    goal: Check the prices.
    tools: ["builtin:read_csv"]
    system:
      output: "Per line: status."
"""


def test_a_clean_structured_agent_has_no_findings() -> None:
    assert _lint(_BASE, profile="generated") == {}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("      tools_in_step: Use `send_email` for the reply.\n", "E02"),
        ("      context: Follow `contract-clause-review` here.\n", "E03"),
        ("      context: The amount is {amount}.\n", "E04"),
        ("      context: Read the document `ledger` first.\n", "E05"),
        ("      context: Use the document `prices` first.\n", "A02"),
        ("      context: <<<DATA x>>>\n", "E06"),
        (f"      context: The key is {fake_secret('sk-', 'abcdefghijklmnopqrstuvwx')}.\n", "E11"),
    ],
)
def test_each_structural_rule_fires(change: str, code: str) -> None:
    text = _BASE.replace("    system:\n", "    system:\n" + change)

    assert code in _lint(text, profile="generated")


def test_a_missing_output_is_an_error_when_generated_and_a_warning_when_authored() -> None:
    text = _BASE.replace('      output: "Per line: status."\n', "      context: Prices matter.\n")

    assert _lint(text, profile="generated")["E01"] == ["error"]
    assert _lint(text, profile="authored")["E01"] == ["warning"]


def test_a_long_message_is_e10() -> None:
    long = "      context: " + "word " * 790 + "\n      how_to_work: " + "word " * 1190 + "\n"
    text = _BASE.replace("    system:\n", "    system:\n" + long)

    assert "E10" in _lint(text, profile="generated")


def test_free_text_instructions_get_only_the_info_and_safety_rules() -> None:
    text = "name: p\nagents:\n  - {name: a, instructions: You MUST think step by step.}\n"

    assert _lint(text) == {"unstructured": ["info"]}


def test_more_than_three_skills_is_a04() -> None:
    text = _BASE.replace(
        "    goal:",
        "    skills: [writing, data-analysis, data-validation, report-generation]\n    goal:",
    )

    assert "A04" in _lint(text)


_ACTING = """
name: p
toolkits: {data: {root: .}}
agents:
  - name: a
    description: Exports the list. Use when asked.
    tools: ["builtin:write_csv"]
approvals: []
"""


def test_an_ungated_acting_tool_is_t14_by_profile() -> None:
    # write_csv waits for approval by default; turn that off to make it ungated.
    text = _ACTING.replace("{data: {root: .}}", "{data: {root: ., requires_approval: false}}")

    assert _lint(text, profile="generated")["T14"] == ["error"]
    assert _lint(text)["T14"] == ["warning"]
    acknowledged = text.replace(
        'tools: ["builtin:write_csv"]',
        'tools: ["builtin:write_csv"]\n    acknowledge_ungated: ["builtin:write_csv"]',
    )
    assert _lint(acknowledged)["T14"] == ["info"]


def test_an_approval_satisfies_t14() -> None:
    assert "T14" not in _lint(_ACTING, profile="generated")


def test_a_judge_with_an_acting_tool_is_t21_even_when_hand_written() -> None:
    text = """
name: p
toolkits: {data: {root: ., requires_approval: false}}
agents:
  - {name: w, role: worker, description: Works.}
  - {name: j, role: judge, judges: w, rubric: [ok], tools: ["builtin:write_csv"]}
"""
    assert _lint(text)["T21"] == ["error"]


def test_card_rules() -> None:
    assert [
        f.code
        for f in lint_card(
            ToolCard(
                "bad name!",
                "x" * 80 + ". Two. Three.",
                {"type": "object", "additionalProperties": False},
            )
        )
    ] == ["T01"]
    assert [(f.code, f.severity) for f in lint_card(ToolCard("short", "Reads.", None))] == [
        ("T03", "error")
    ]
    two = ToolCard(
        "ok", "Reads the price list and returns the rows. Use it once per file.", {"type": "object"}
    )
    assert [(f.code, f.severity) for f in lint_card(two)] == [("T03", "info"), ("T05", "info")]
    root = ToolCard("ok", "Reads it. Returns rows. Limited to 50 rows.", {"type": "array"})
    assert [f.code for f in lint_card(root)] == ["T05"]
    contradiction = ToolCard(
        "ok",
        "Reads it. Returns rows. Limited to 50 rows.",
        None,
        {"readOnlyHint": True, "destructiveHint": True},
    )
    assert [f.code for f in lint_card(contradiction)] == ["T13"]
    lowered = ToolCard(
        "ok",
        "Reads it. Returns rows. Limited to 50 rows.",
        None,
        {"readOnlyHint": True},
        ToolRisk(external_action=True),
    )
    assert [f.code for f in lint_card(lowered)] == ["T13"]


def test_a_card_is_read_from_the_tool_object() -> None:
    @tool
    def lookup_price(article: str) -> str:
        """Look up the agreed unit price of one article. Returns the price in euro. Only for
        articles of the price list."""

        return "4.20"

    card = card_of(lookup_price)

    assert card.name == "lookup_price"
    assert sentences(card.description) == 3
    assert card.input_schema is not None and card.input_schema["type"] == "object"
    assert lint_card(card) and {f.code for f in lint_card(card)} <= {"T05"}


_PROMISED = """
name: p
agents:
  - name: a
    description: Sends the offer. Use when an offer is due.
    instructions: Write the offer and submit it for the manager's approval.
    tools: [lookup_price]
"""


def test_an_approval_in_the_text_without_a_gated_tool_is_t22_by_profile() -> None:
    assert _lint(_PROMISED, profile="generated")["T22"] == ["error"]
    assert _lint(_PROMISED)["T22"] == ["warning"]


def test_t22_reads_the_special_cases_too() -> None:
    text = """
name: p
agents:
  - name: a
    description: Sends the offer. Use when an offer is due.
    goal: Write the offer.
    system:
      special_cases: [An offer above 5000 EUR needs the manager's approval.]
    tools: [lookup_price]
"""
    assert _lint(text)["T22"] == ["warning"]


def test_t22_is_quiet_with_a_gated_tool_or_without_the_word() -> None:
    gated = _PROMISED.replace("[lookup_price]", "[lookup_price, send_offer]") + (
        "approvals: [{tool: send_offer}]\n"
    )
    assert "T22" not in _lint(gated, profile="generated")
    plain = _PROMISED.replace(" and submit it for the manager's approval", "")
    assert "T22" not in _lint(plain, profile="generated")


def _worker() -> Blueprint:
    return Blueprint.from_yaml_file(EXAMPLES / "worker.yaml")


def test_sections_come_in_the_fixed_order() -> None:
    message = system_message(_worker(), _worker().agents[0])

    assert [s.name for s in message.sections] == [
        "role_and_goal",
        "context",
        "how_to_work",
        "tools_in_step",
        "rules",
        "special_cases",
        "before_you_answer",
        "house_rules",
    ]
    assert message.text.startswith(
        '## Role and goal\nYou are the worker for step 1 of 1 ("Check invoice")'
    )


def test_the_code_fills_documents_checks_retries_and_special_cases() -> None:
    text = system_message(_worker(), _worker().agents[0]).text

    assert "Inputs arrive in the step input: invoice (input), price_list (reference)" in text
    assert "- Every invoice line needs a status." in text  # the rule check's message
    assert "The step is tried up to 2 times when it fails." in text
    assert "After the last failed try, a person decides." in text
    assert 'If status is "done": approved without asking a person.' in text
    assert "Escalate to a person" in text
    # The output_schema is enforced as structured output; the message does not repeat it.
    assert "## Output" not in text


def test_empty_optional_sections_are_left_out() -> None:
    bp = Blueprint.from_yaml_text(
        "name: p\nagents:\n  - {name: a, goal: Sort the mail., system: {output: One label per mail.}}\n"
    )
    message = system_message(bp, bp.agents[0])

    assert [s.name for s in message.sections] == ["role_and_goal", "output", "house_rules"]
    assert 'You are the agent in the process "p". Sort the mail.' in message.text


def test_a_gated_tool_gets_the_approval_sentence() -> None:
    bp = Blueprint.from_yaml_text(
        """
name: p
toolkits: {email: {host: smtp.example.com, sender: a@example.com}}
agents:
  - name: a
    goal: Send the confirmation.
    tools: ["builtin:send_email"]
    system: {output: The message id.}
    escalation: {when: [the address is missing]}
"""
    )
    text = system_message(bp, bp.agents[0]).text

    assert "Calls of `builtin:send_email` need a person's approval; the system asks for it" in text


def test_a_judge_gets_its_role_rules_and_criteria() -> None:
    bp = Blueprint.from_yaml_file(EXAMPLES / "judge.yaml")
    judge = bp.agent("check_invoice_judge")
    message = system_message(bp, judge)

    rules = message.section("role_rules")
    assert rules is not None and rules.title == "Judging"
    assert "- c1: Every invoice line has exactly one status." in rules.text
    assert message.section("output") is not None  # a verdict


def test_an_orchestrator_gets_its_delegation_brief() -> None:
    bp = Blueprint.from_yaml_file(EXAMPLES / "orchestrator.yaml")
    message = system_message(bp, bp.agent("invoice_process_lead"))

    delegation = message.section("delegation")
    assert delegation is not None and "objective, output format" in delegation.text


def test_every_structured_agent_gets_the_house_rules() -> None:
    text = system_message(_worker(), _worker().agents[0]).text

    for line in HOUSE_RULES:
        assert f"- {line}" in text
    assert len(HOUSE_RULES) <= 5


def test_a_free_text_agent_gets_the_same_skeleton() -> None:
    bp = Blueprint.from_yaml_text("name: p\nagents:\n  - {name: a, instructions: Do it.}\n")

    text = effective_instructions(bp, bp.agents[0])
    assert text.startswith('## Role and goal\nYou are the agent "a" in the process "p".')
    assert "\n\n## Instructions\nDo it.\n\n## Rules\n" in text
    for line in HOUSE_RULES:
        assert f"- {line}" in text


def test_the_built_agent_runs_with_the_rendered_message() -> None:
    bp = _worker()
    agent = bp.build_agent("check_invoice")

    instructions = agent._instructions  # type: ignore[attr-defined]
    assert isinstance(instructions, str)
    assert instructions.startswith(system_message(bp, bp.agents[0]).text)


def test_an_output_schema_gives_a_before_you_answer_checklist() -> None:
    message = system_message(_worker(), _worker().agents[0])

    checklist = message.section("before_you_answer")
    assert checklist is not None
    lines = checklist.text.splitlines()
    assert 1 <= len(lines) <= 3
    assert lines[0].startswith("- Every required field is filled from the data you fetched: `")
    assert any("step's checks" in line for line in lines)


def test_a_gated_tool_adds_the_action_line_and_judges_get_no_checklist() -> None:
    bp = Blueprint.from_yaml_text(
        """
name: p
toolkits: {email: {host: smtp.example.com, sender: a@example.com}}
agents:
  - {name: a, instructions: Send it., tools: ["builtin:send_email"]}
  - {name: j, role: judge, judges: a, rubric: [It was sent.], instructions: Judge.}
"""
    )
    text = system_message(bp, bp.agent("a")).text
    assert "## Before you answer\n- Every action the task calls for was made with its tool" in text
    assert system_message(bp, bp.agent("j")).section("before_you_answer") is None


def test_a_free_text_agent_keeps_its_text_and_gets_refinement_sections() -> None:
    bp = Blueprint.from_yaml_text(
        """
name: p
agents:
  - name: a
    instructions: Do it exactly so.
    system:
      how_to_work: "1. Read. 2. Write."
      tools_in_step: Call lookup first.
      done_when: Every item has a status.
"""
    )
    message = system_message(bp, bp.agents[0])

    assert [s.name for s in message.sections] == [
        "role_and_goal",
        "instructions",
        "how_to_work",
        "tools_in_step",
        "done_when",
        "rules",
    ]
    assert message.section("instructions").text == "Do it exactly so."  # type: ignore[union-attr]
    assert "## Done when\nEvery item has a status." in message.text


def test_a_free_text_agent_keeps_its_context_special_cases_and_escalation() -> None:
    bp = Blueprint.from_yaml_text(
        """
name: p
agents:
  - name: a
    instructions: Do it exactly so.
    system:
      context: Orders come from the web shop.
      special_cases: [A refund above 500 EUR goes to finance.]
    escalation: {to: person, when: [the address is missing]}
"""
    )
    message = system_message(bp, bp.agents[0])

    assert message.section("context").text == "Orders come from the web shop."  # type: ignore[union-attr]
    cases = message.section("special_cases").text  # type: ignore[union-attr]
    assert "- A refund above 500 EUR goes to finance." in cases
    assert "Escalate to a person" in cases and "the address is missing" in cases


def _bp(text: str) -> Blueprint:
    return Blueprint.from_yaml_text(text)


@pytest.mark.parametrize(
    "entry",
    ['{name: "builtin:read_csv"}', '{ref: "builtin:read_csv", why: {rule: x}}', "{why: {rule: x}}"],
)
def test_a_tool_entry_is_a_plain_reference(entry: str) -> None:
    with pytest.raises(BlueprintError):
        _bp(f"name: t\nagents:\n  - name: a\n    tools: [{entry}]\n")


def test_a_free_text_agent_keeps_its_plain_fields() -> None:
    text = "name: t\nagents:\n  - {name: a, description: Does a., instructions: Do a.}\n"
    bp = _bp(text)

    assert not renders_sections(bp.agents[0])
    assert bp.check() == []
    assert set(bp.to_dict()["agents"][0]) == {"name", "description", "instructions"}


def test_sections_escalation_and_exclusions_load() -> None:
    bp = _bp(
        """
name: t
agents:
  - name: a
    goal: Decide.
    system:
      rules: Mark it, because finance decides.
      examples: [{input: one line, output: ok, why: simple}]
    escalation: {to: person, when: deviation above 10 percent}
"""
    )
    spec = bp.agents[0]

    assert renders_sections(spec)
    assert spec.system is not None and spec.system.rules == ["Mark it, because finance decides."]
    assert spec.escalation is not None and spec.escalation.when == ["deviation above 10 percent"]


def test_more_than_three_examples_are_refused() -> None:
    examples = ", ".join("{input: a, output: b}" for _ in range(4))
    with pytest.raises(BlueprintError, match="examples"):
        _bp(f"name: t\nagents:\n  - name: a\n    system: {{examples: [{examples}]}}\n")


def test_an_output_schema_file_is_read_beside_the_blueprint(tmp_path: Path) -> None:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "out.json").write_text(
        '{"type": "object", "properties": {"x": {"type": "string"}}}'
    )
    (tmp_path / "bp.yaml").write_text(
        "name: t\nagents:\n  - {name: a, output_schema: {$ref: schemas/out.json}}\n"
    )

    bp = Blueprint.from_yaml_file(tmp_path / "bp.yaml")

    assert bp.agents[0].output_schema == {"type": "object", "properties": {"x": {"type": "string"}}}
    assert bp.check() == []


def test_an_output_schema_file_outside_the_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "out.json").write_text('{"type": "object"}')
    (tmp_path / "inner").mkdir()
    (tmp_path / "inner" / "bp.yaml").write_text(
        "name: t\nagents:\n  - {name: a, output_schema: {$ref: ../out.json}}\n"
    )

    with pytest.raises(BlueprintError, match="outside the blueprint's folder"):
        Blueprint.from_yaml_file(tmp_path / "inner" / "bp.yaml")


def test_an_output_schema_reference_without_a_file_is_a_check_error() -> None:
    bp = _bp("name: t\nagents:\n  - {name: a, output_schema: {$ref: schemas/out.json}}\n")

    assert any(i.path == "agents[0].output_schema" for i in bp.check() if i.severity == "error")


@pytest.mark.parametrize("name", ["worker", "judge", "reviewer", "orchestrator"])
def test_the_four_design_examples_load_and_check_clean(name: str) -> None:
    bp = Blueprint.from_yaml_file(EXAMPLES / f"{name}.yaml")

    assert bp.check() == []
