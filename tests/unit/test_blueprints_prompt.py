"""From a prompt to a system and its automation: triggers, assumptions, similar packages."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from awa.blueprints import AcceptanceDesign, BlueprintDesign, build_project, design, design_project
from awa.blueprints import _catalog as catalog
from awa.blueprints._project import (
    TEST_DESIGNER_INSTRUCTIONS,
    Expectation,
    TriggerDesign,
    acceptance_problems,
)
from awa.cli import main
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

SCHEDULE: dict[str, Any] = {
    "kind": "schedule",
    "name": "friday-summary",
    "cron": "0 16 * * FRI",
    "timezone": "Europe/Berlin",
    "input": "Summarise the open tickets on {payload.date}.",
    "sample_payload": '{"date": "2026-10-09"}',
}

ACCEPTANCE: dict[str, Any] = {
    "criteria": [
        "Open tickets are summarised by customer.",
        "Tickets older than 5 days are flagged.",
    ],
    "assumptions": ["Open means status 'open'.", "The summary goes to the requester."],
    "systems": [
        {
            "name": "Ticket system",
            "key": "tickets",
            "description": "Support tickets.",
            "kind": "database",
            "tables": [
                {
                    "name": "tickets",
                    "description": "One ticket.",
                    "columns": [
                        {"name": "ticket_id"},
                        {"name": "customer"},
                        {"name": "age_days", "type": "integer"},
                    ],
                    "rows": ["T-1, Acme, 7", "T-2, Beta, 1"],
                }
            ],
        }
    ],
    "triggers": [SCHEDULE],
    "cases": [
        {
            "id": "friday-run",
            "trigger": "friday-summary",
            "payload": '{"date": "2026-10-09"}',
            "expectations": [{"kind": "contains", "value": "Acme"}],
        },
        {
            "id": "by-hand",
            "input": "Which tickets are older than 5 days?",
            "expectations": [{"kind": "contains", "value": "T-1"}],
        },
    ],
}

BLUEPRINT: dict[str, Any] = {
    "name": "ticket_summary",
    "description": "Summarises open tickets by customer.",
    "entrypoint": "agent",
    "agents": [
        {
            "name": "summariser",
            "description": "Summarises tickets.",
            "instructions": "Query the tickets table and summarise by customer.",
            "tools": ["connector:tickets.query"],
        }
    ],
}


def system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def scripted(acceptance: dict[str, Any] | None = None) -> FunctionChatModel:
    def respond(call: ModelCall) -> Any:
        system = system_prompt(call)
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, acceptance or ACCEPTANCE)
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, BLUEPRINT)
        if not any(isinstance(m, ToolMessage) for m in call.messages):
            query = "SELECT ticket_id, customer, age_days FROM tickets"
            return tool_call("tickets_query", {"query": query})
        return "Acme: T-1 (7 days, older than 5), Beta: T-2"

    return FunctionChatModel(respond)


def with_triggers(*triggers: dict[str, Any], **cases: Any) -> dict[str, Any]:
    draft = copy.deepcopy(ACCEPTANCE)
    draft["triggers"] = list(triggers)
    draft.update(cases)
    return draft


def test_the_shipped_catalog_is_generated_from_the_examples() -> None:
    assert list(catalog.catalog()) == catalog.build(EXAMPLES)
    kinds = [entry["kind"] for entry in catalog.catalog()]
    assert (kinds.count("use case"), kinds.count("recipe")) == (13, 9)


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("weekly KPI report by mail", "weekly-management-report"),
        ("incoming supplier invoices", "invoice-processing"),
        ("check expense reports from employees", "expense-report-audit"),
        ("screen applicants' CVs", "recruiting-cv-screening"),
        (
            "Every morning check our shared inbox for supplier invoices, match them to purchase "
            "orders and ask me before paying anything above 2,000 euros",
            "invoice-processing",
        ),
        (
            "When a new employee form lands in a folder, create the accounts checklist and "
            "mail the manager",
            "employee-onboarding",
        ),
    ],
)
def test_the_closest_package_is_picked_without_a_model(prompt: str, expected: str) -> None:
    picked = catalog.closest(prompt)

    assert picked[0]["id"] == expected
    assert len(picked) <= 2


def test_nothing_is_picked_when_nothing_is_close() -> None:
    assert catalog.closest("we sell colourful shoes") == []
    assert catalog.reference_section("we sell colourful shoes") == ""


def test_the_reference_shows_structure_and_says_to_adapt() -> None:
    section = catalog.reference_section("weekly KPI report by mail")

    assert "adapt, do not copy" in section
    assert "### weekly management report (use case)" in section
    assert "workflow: kpi_analyst -> report_writer" in section
    assert "approvals: send_report" in section
    assert "triggers: weekly = schedule 0 7 * * MON" in section


async def test_the_designers_see_the_closest_packages() -> None:
    model = scripted()

    await design_project("Every Friday summarise our open support tickets.", model=model)

    for call in (model.calls[0], model.calls[1]):
        brief = str(call.last_message.content)
        assert "## Similar packages (style reference only: adapt, do not copy" in brief
        assert "customer support desk" in brief.lower()


async def test_the_plain_architect_sees_them_too() -> None:
    plain = {**BLUEPRINT, "agents": [{**BLUEPRINT["agents"][0], "tools": []}]}
    model = FunctionChatModel(lambda call: structured_output(BlueprintDesign, plain))

    await design("Weekly KPI report by mail to management.", model=model)

    assert "### weekly management report (use case)" in str(model.calls[0].last_message.content)


async def test_a_trigger_the_process_implies_lands_in_the_blueprint() -> None:
    model = scripted()

    project = await design_project(
        "Each Friday summarise open support tickets by customer.", model=model
    )

    [trigger] = project.blueprint.triggers
    assert (trigger.name, trigger.kind) == ("friday-summary", "schedule")
    assert trigger.schedule is not None
    assert (trigger.schedule.cron, trigger.schedule.timezone) == ("0 16 * * FRI", "Europe/Berlin")
    assert trigger.input == "Summarise the open tickets on {payload.date}."
    architect = str(model.calls[1].last_message.content)
    assert "## Triggers (they start runs by themselves" in architect
    assert "- friday-summary (schedule): each event sends" in architect


async def test_a_case_goes_through_the_triggers_input_template() -> None:
    project = await design_project("Each Friday summarise tickets.", model=scripted())

    assert [case.input for case in project.evals] == [
        "Summarise the open tickets on 2026-10-09.",
        "Which tickets are older than 5 days?",
    ]


async def test_the_project_runs_automatically_as_the_readme_says(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = await design_project("Each Friday summarise tickets.", model=scripted())

    project.write(tmp_path)

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "## Run it automatically" in readme
    assert "- friday-summary: runs by itself: `0 16 * * FRI (Europe/Berlin)`" in readme
    assert (
        "- Try friday-summary without waiting for a real event: `awa trigger run blueprint.yaml "
        'friday-summary --payload "@friday-summary.json" --dry-run` (First save a sample event '
        'as friday-summary.json in this folder, for example {"date": "2026-10-09"}.'
    ) in readme
    assert "Start it: `awa worker blueprint.yaml`" in readme
    assert "triggers:\n- name: friday-summary" in (tmp_path / "blueprint.yaml").read_text("utf-8")
    assert main(["validate", str(tmp_path / "blueprint.yaml")]) == 0
    capsys.readouterr()
    command = f"trigger run {tmp_path / 'blueprint.yaml'} friday-summary --dry-run --payload"
    assert main([*command.split(), '{"date": "2026-10-09"}']) == 0
    assert "input:     Summarise the open tickets on 2026-10-09." in capsys.readouterr().out


async def test_a_webhook_names_its_secret_and_a_mail_trigger_its_login(tmp_path: Path) -> None:
    webhook = {
        "kind": "webhook",
        "name": "order-hook",
        "input": "Handle the order {payload.id}.",
        "sample_payload": '{"id": "A-1"}',
    }
    mail = {
        "kind": "mail",
        "name": "invoice-mail",
        "input": "Mail from {payload.from}: {payload.subject}\n{payload.text}",
        "sample_payload": '{"from": "ap@acme.example", "subject": "Invoice 4711", "text": "Hi"}',
    }
    acceptance = with_triggers(webhook, mail)
    acceptance["cases"][0].update(trigger="", payload="", input="Which tickets are old?")
    acceptance["cases"][1].update(input="", trigger="invoice-mail", payload=mail["sample_payload"])
    acceptance["cases"][1]["expectations"] = [{"kind": "contains", "value": "Acme"}]

    project = await design_project("Orders and invoices.", model=scripted(acceptance))
    project.write(tmp_path)

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "ORDER_HOOK_SECRET" in readme and "AWA_IMAP_USER" in readme
    assert "http://127.0.0.1:8000/v1/automations/ticket_summary/webhooks/order-hook" in readme
    assert "The mailbox server is imap.example.com, a placeholder" in readme
    assert "secret_env: ORDER_HOOK_SECRET" in (tmp_path / "blueprint.yaml").read_text("utf-8")
    assert "sk-" not in readme


async def test_a_file_drop_folder_is_created(tmp_path: Path) -> None:
    drop = {
        "kind": "file-drop",
        "name": "new-form",
        "directory": "forms",
        "pattern": "*.pdf",
        "input": "Process the form {payload.name}:\n{payload.text}",
        "sample_payload": '{"name": "anna.pdf", "text": "Anna Schmidt, starts 1 Nov"}',
    }
    acceptance = with_triggers(drop)
    acceptance["cases"][0].update(trigger="new-form", payload=drop["sample_payload"])
    acceptance["cases"][0]["expectations"] = [{"kind": "contains", "value": "Acme"}]

    (await design_project("Forms land in a folder.", model=scripted(acceptance))).write(tmp_path)

    assert (tmp_path / "forms").is_dir()
    assert "drop files (*.pdf) into: `forms`" in (tmp_path / "README.md").read_text(
        encoding="utf-8"
    )


async def test_a_process_without_a_start_has_no_triggers(tmp_path: Path) -> None:
    acceptance = with_triggers()
    acceptance["cases"][0].update(trigger="", payload="", input="Summarise the open tickets.")
    project = await design_project("Summarise tickets.", model=scripted(acceptance))

    project.write(tmp_path)

    assert project.blueprint.triggers == []
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "$ awa add trigger blueprint.yaml" in readme and "already has triggers" not in readme


@pytest.mark.parametrize(
    ("trigger", "path", "problem"),
    [
        ({"cron": "every monday"}, "triggers[0]", "Invalid trigger 'friday-summary'"),
        ({"cron": "", "every": ""}, "triggers[0].cron", "a schedule needs cron or every"),
        ({"name": "Friday Summary"}, "triggers[0].name", "not a kebab-case name"),
        ({"input": "Summarise {payload.week}."}, "triggers[0].sample_payload", "payload.week"),
        ({"sample_payload": "[1]"}, "triggers[0].sample_payload", "must be a JSON object"),
        (
            {"kind": "file-drop", "directory": "../outside"},
            "triggers[0].directory",
            "inside the project",
        ),
        ({"kind": "file-drop", "directory": "/etc"}, "triggers[0].directory", "inside the project"),
    ],
)
def test_invalid_triggers_are_sent_back(trigger: dict[str, Any], path: str, problem: str) -> None:
    draft = with_triggers({**SCHEDULE, **trigger})
    draft["cases"][0].update(trigger="", payload="", input="Summarise.")
    draft["cases"][1]["trigger"] = ""
    draft["cases"][0]["trigger"] = trigger.get("name", "friday-summary")

    problems = [
        p
        for p in acceptance_problems(AcceptanceDesign.model_validate(draft), set())
        if p.path.startswith("triggers")
    ]

    assert [p.path for p in problems] == [path]
    assert problem in problems[0].message


def test_a_trigger_needs_a_case_that_uses_it() -> None:
    draft = with_triggers(SCHEDULE)
    draft["cases"][0].update(trigger="", payload="", input="Summarise.")

    problems = acceptance_problems(AcceptanceDesign.model_validate(draft), set())

    assert [(p.path, p.message) for p in problems] == [
        ("cases", "a case must go through a trigger: set its trigger and payload")
    ]


def test_a_case_names_a_known_trigger_and_a_payload_that_fits_it() -> None:
    draft = copy.deepcopy(ACCEPTANCE)
    draft["cases"][0].update(trigger="nightly", payload="{}")
    unknown = acceptance_problems(AcceptanceDesign.model_validate(draft), set())
    draft["cases"][0].update(trigger="friday-summary", payload="{}")
    lacking = acceptance_problems(AcceptanceDesign.model_validate(draft), set())

    assert [(p.path, p.message) for p in unknown] == [
        ("cases[0].trigger", "unknown trigger 'nightly'")
    ]
    assert [p.path for p in lacking] == ["cases[0].trigger"]
    assert "payload.date" in lacking[0].message


def test_the_designer_is_told_about_triggers_assumptions_and_questions() -> None:
    text = " ".join(TEST_DESIGNER_INSTRUCTIONS.split())

    assert "Ask no questions" in text and "list it in assumptions" in text
    assert "triggers: what starts the process by itself, only when the process says so" in text
    assert "at least one case goes through a trigger" in text
    assert TriggerDesign.model_fields["sample_payload"].description


async def test_assumptions_are_listed_in_the_readme(tmp_path: Path) -> None:
    acceptance = copy.deepcopy(ACCEPTANCE)
    acceptance["systems"][0]["writes"] = True
    model = scripted(acceptance)

    project = await design_project("Summarise tickets each Friday.", model=model)
    project.write(tmp_path)

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    block = readme.split("## Assumptions made\n", 1)[1].split("\n## ", 1)[0]
    assert "- Open means status 'open'." in block
    assert "- The summary goes to the requester." in block
    assert "- A person decides before these run: connector:tickets.query" not in block
    assert project.assumptions == [
        "Open means status 'open'.",
        "The summary goes to the requester.",
    ]


async def test_gated_writes_are_named_among_the_assumptions(tmp_path: Path) -> None:
    acceptance = copy.deepcopy(ACCEPTANCE)
    acceptance["systems"][0]["writes"] = True
    acceptance["assumptions"] = []
    acceptance["triggers"] = []
    acceptance["cases"] = [ACCEPTANCE["cases"][1]]
    blueprint = copy.deepcopy(BLUEPRINT)
    blueprint["agents"][0]["tools"].append("connector:tickets.execute")

    def respond(call: ModelCall) -> Any:
        if system_prompt(call).startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, acceptance)
        return structured_output(BlueprintDesign, blueprint)

    project = await design_project("Close old tickets.", model=FunctionChatModel(respond))
    project.write(tmp_path)

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert (
        "## Assumptions made\n\n- A person decides before these run: connector:tickets.execute."
        in readme
    )


async def test_the_prompt_reaches_the_designer_unchanged_for_a_one_line_process() -> None:
    model = scripted()

    await design_project("Remind me of overdue tickets.", model=model)

    brief = str(next(m for m in model.calls[0].messages if isinstance(m, HumanMessage)).content)
    assert brief.startswith("## Process\nRemind me of overdue tickets.\n")


async def test_a_build_reports_how_it_runs_by_itself_and_passes(tmp_path: Path) -> None:
    report = await build_project(
        "Each Friday summarise open support tickets by customer.",
        out_dir=tmp_path / "tickets",
        model=scripted(),
        environment=None,
    )

    summary = report.summary()
    assert report.passed and report.stopped == "target_reached"
    assert "Runs by itself: friday-summary (schedule); start it with `awa worker " in summary
    assert "Assumptions made: 2 (README.md lists them)." in summary
    assert main(["validate", str(tmp_path / "tickets" / "blueprint.yaml")]) == 0


@pytest.mark.parametrize(
    ("answer", "found"),
    [
        ("Paid 1,250.00 EUR.", True),
        ("Paid EUR 1250", True),
        ("Zahlung 1.250,00 EUR", True),
        ("Paid 11250 EUR", False),
        ("Paid 1,350.00 EUR", False),
    ],
)
def test_an_amount_matches_however_the_answer_writes_it(answer: str, found: bool) -> None:
    spec = Expectation(kind="contains", value="1250.00").spec()

    assert spec.startswith("regex:")
    assert bool(re.search(spec.removeprefix("regex:"), answer)) is found


def test_plain_words_and_short_numbers_stay_plain_contains_checks() -> None:
    assert Expectation(kind="contains", value="shipped").spec() == "contains:shipped"
    assert Expectation(kind="contains", value="49").spec() == "contains:49"
    assert Expectation(kind="contains", value="INV-24091").spec() == "contains:INV-24091"


async def test_a_toolkit_folder_is_sent_back_instead_of_crashing_the_first_evaluation() -> None:
    # a toolkit root nobody created
    bad = {
        **BLUEPRINT,
        "agents": [
            {**BLUEPRINT["agents"][0], "tools": ["connector:tickets.query", "builtin:read_csv"]}
        ],
        "toolkits": [{"toolkit": "files", "option": "root", "value": "./employee_files"}],
    }
    answers = [bad, BLUEPRINT]

    def respond(call: ModelCall) -> Any:
        if system_prompt(call).startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, ACCEPTANCE)
        return structured_output(BlueprintDesign, answers.pop(0))

    model = FunctionChatModel(respond)

    project = await design_project("Summarise tickets.", model=model)

    repair = str(model.calls[-1].last_message.content)
    assert (
        "toolkits.files.root: './employee_files' does not exist in the project's workspace"
        in repair
    )
    assert project.blueprint.toolkits == {}


async def test_a_system_tool_written_by_its_name_becomes_its_reference() -> None:
    named = {
        **BLUEPRINT,
        "agents": [
            {
                **BLUEPRINT["agents"][0],
                "tools": ["tickets_query", "connector:tickets.query", "tickets_execute"],
            }
        ],
        "approvals": ["tickets_execute"],
    }
    acceptance = copy.deepcopy(ACCEPTANCE)
    acceptance["systems"][0]["writes"] = True

    def respond(call: ModelCall) -> Any:
        if system_prompt(call).startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, acceptance)
        return structured_output(BlueprintDesign, named)

    project = await design_project("Summarise tickets.", model=FunctionChatModel(respond))

    assert project.blueprint.agents[0].tools == [
        "connector:tickets.query",
        "connector:tickets.execute",
    ]
    assert [a.tool for a in project.blueprint.approvals] == ["connector:tickets.execute"]
