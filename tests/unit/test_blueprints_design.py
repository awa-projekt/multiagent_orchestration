"""The blueprint architect: its brief, drafting, checking, repair rounds, and what a test designer may expect of the answer."""

from __future__ import annotations

import re
from typing import Any

import pytest
from langchain_core.tools import tool

from _helpers.build_scripts import REPO
from awa.blueprints import (
    AcceptanceDesign,
    BlueprintDesign,
    BlueprintError,
    LoopStep,
    ParallelStep,
    SequentialStep,
    design,
    prepare_data,
)
from awa.blueprints._project import TEST_DESIGNER_INSTRUCTIONS, Expectation, acceptance_problems
from awa.blueprints.architect import ARCHITECT_INSTRUCTIONS
from awa.skills import SkillRegistry
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output


@tool
def read_invoice(invoice_id: str) -> str:
    """Read an invoice document."""

    return invoice_id


@tool
def book_payment(invoice_id: str) -> str:
    """Book the payment of an invoice."""

    return invoice_id


TEAM_DESIGN: dict[str, Any] = {
    "name": "invoices",
    "description": "Checks and pays invoices.",
    "agents": [
        {"name": "lead", "description": "Coordinates.", "instructions": "Plan and delegate."},
        {
            "name": "intake",
            "description": "Reads invoices.",
            "instructions": "Extract fields.",
            "tools": ["read_invoice"],
            "capabilities": ["extraction"],
        },
        {
            "name": "payer",
            "description": "Books payments.",
            "instructions": "Pay approved invoices.",
            "tools": ["book_payment"],
        },
    ],
    "entrypoint": "team",
    "team": {
        "orchestrator": "lead",
        "members": ["intake", "payer"],
        "peers": [{"source": "payer", "targets": ["intake"]}],
        "handoffs": [{"source": "lead", "targets": ["payer"]}],
        "task_board": True,
    },
    "approvals": ["book_payment"],
}


def draft(data: dict[str, Any]) -> Any:
    return structured_output(BlueprintDesign, data)


async def test_design_returns_a_checked_team_blueprint() -> None:
    model = FakeChatModel([draft(TEAM_DESIGN)])

    blueprint = await design(
        "Invoices are read, then paid after approval.",
        model=model,
        tools=[read_invoice, book_payment],
        guidance="Use short names.",
    )

    assert blueprint.check(tools=[read_invoice, book_payment]) == []
    assert blueprint.resolved_entrypoint() == "team"
    assert blueprint.team is not None
    assert blueprint.team.delegates is None
    assert blueprint.team.peers == {"payer": ["intake"]}
    assert blueprint.team.handoffs == {"lead": ["payer"]}
    assert blueprint.team.task_board is True
    assert [a.tool for a in blueprint.approvals] == ["book_payment"]
    prompt = str(model.calls[0].last_message.content)
    assert "## Process\nInvoices are read" in prompt
    assert "- book_payment: Book the payment of an invoice." in prompt
    assert "- delegation-briefs:" in prompt
    assert "## Additional requirements\nUse short names." in prompt
    system = str(model.calls[0].messages[0].content)
    assert "You design multi-agent systems" in system


async def test_design_repairs_check_errors() -> None:
    broken = {
        **TEAM_DESIGN,
        "agents": [*TEAM_DESIGN["agents"][:2], {**TEAM_DESIGN["agents"][2], "tools": ["pay_now"]}],
    }
    model = FakeChatModel([draft(broken), draft(TEAM_DESIGN)])

    blueprint = await design("Pay invoices.", model=model, tools=[read_invoice, book_payment])

    assert blueprint.agent("payer").tools == ["book_payment"]
    repair = str(model.calls[1].last_message.content)
    assert "## Your previous draft" in repair
    assert '"pay_now"' in repair
    assert "agents[2].tools[0]: unknown tool 'pay_now'" in repair


async def test_schema_errors_are_fed_back_too() -> None:
    empty = {**TEAM_DESIGN, "agents": [], "team": None, "entrypoint": "agent"}
    model = FakeChatModel([draft(empty), draft(TEAM_DESIGN)])

    await design("Pay invoices.", model=model, tools=[read_invoice, book_payment])

    assert "## Problems to fix\n- agents:" in str(model.calls[1].last_message.content)


async def test_design_gives_up_after_max_repairs() -> None:
    broken = {**TEAM_DESIGN, "entrypoint": "workflow"}
    model = FakeChatModel([draft(broken)], repeat_last=True)

    with pytest.raises(BlueprintError, match="after 1 repair round") as caught:
        await design("Pay invoices.", model=model, max_repairs=1)

    assert model.call_count == 2
    assert [issue.path for issue in caught.value.issues] == ["entrypoint"]


async def test_workflow_designs_become_sequential_and_parallel_steps() -> None:
    data = {
        "name": "review",
        "description": "Reviews documents.",
        "agents": [
            {"name": name, "description": name, "instructions": name}
            for name in ("reader", "legal", "finance", "editor")
        ],
        "entrypoint": "workflow",
        "workflow": [
            {"agents": ["reader"]},
            {"agents": ["legal", "finance"]},
            {"agents": []},
            {"agents": ["editor"]},
        ],
    }

    blueprint = await design("Review a contract.", model=FakeChatModel([draft(data)]))

    workflow = blueprint.workflow
    assert isinstance(workflow, SequentialStep)
    assert workflow.sequential[0] == "reader"
    assert isinstance(workflow.sequential[1], ParallelStep)
    assert workflow.sequential[1].parallel == ["legal", "finance"]
    assert workflow.sequential[2] == "editor"
    assert not isinstance(workflow.sequential[2], LoopStep)


async def test_single_agent_designs() -> None:
    data = {
        "name": "helper",
        "description": "Answers questions.",
        "agents": [{"name": "helper", "description": "Answers.", "instructions": "Answer."}],
        "entrypoint": "agent",
    }

    def respond(call: ModelCall) -> Any:
        assert "No tool catalog is given" in str(call.last_message.content)
        assert "No skills are available." in str(call.last_message.content)
        return draft(data)

    blueprint = await design(
        "Answer questions.", model=FunctionChatModel(respond), skills=SkillRegistry()
    )

    assert blueprint.resolved_entrypoint() == "helper"


async def test_empty_tool_catalog_is_announced() -> None:
    data = {
        **TEAM_DESIGN,
        "agents": [{**a, "tools": []} for a in TEAM_DESIGN["agents"]],
        "approvals": [],
    }
    model = FakeChatModel([draft(data)])

    await design("Pay invoices.", model=model, tools=[])

    assert "No tools are available" in str(model.calls[0].last_message.content)


@pytest.mark.parametrize(("process", "repairs"), [("  ", 2), ("Do it.", -1)])
async def test_invalid_arguments(process: str, repairs: int) -> None:
    with pytest.raises(ValueError):
        await design(process, model=FakeChatModel(["x"]), max_repairs=repairs)


async def test_an_acting_tool_the_design_forgot_to_gate_gets_an_approval() -> None:
    model = FakeChatModel([draft({**TEAM_DESIGN, "approvals": []})])

    blueprint = await design(
        "Invoices are read, then paid.", model=model, tools=[read_invoice, book_payment]
    )

    assert [a.tool for a in blueprint.approvals] == ["book_payment"]
    assert all(a.when is None for a in blueprint.approvals)


async def test_act_without_approval_is_the_explicit_opt_out() -> None:
    data = {**TEAM_DESIGN, "approvals": [], "act_without_approval": ["book_payment"]}
    model = FakeChatModel([draft(data)])

    blueprint = await design(
        "Invoices are read and paid without a person.",
        model=model,
        tools=[read_invoice, book_payment],
    )

    assert blueprint.approvals == []
    assert blueprint.tool_risks["book_payment"].external_action is False


DRAFT: dict[str, Any] = {
    "name": "sales_report",
    "description": "Reports on the sales data.",
    "agents": [
        {
            "name": "analyst",
            "description": "Analyses the sales CSV.",
            "instructions": "Answer from the CSV files.",
            "tools": ["builtin:read_csv", "builtin:calculator"],
        }
    ],
    "entrypoint": "agent",
    "toolkits": [{"toolkit": "files", "option": "root", "value": "./data"}],
}


async def test_the_brief_lists_builtin_tools_and_compact_skills() -> None:
    model = FakeChatModel([structured_output(BlueprintDesign, DRAFT)])

    blueprint = await design("Report on the monthly sales.", model=model)

    brief = str(model.calls[0].last_message.content)
    assert "## Builtin tools (reference as builtin:<name>" in brief
    assert "- builtin:calculator: Evaluate arithmetic exactly" in brief
    assert "- builtin:send_email [approval]: Send an email" in brief
    assert "- builtin:read_csv [needs toolkits.files.root]:" in brief
    assert "- builtin:web_search: Search the web (built-in, no API key" in brief
    assert "- builtin:fetch_url: Read a web page" in brief
    assert "[extra: files]" in brief
    skills = brief.split("## Skills\n", 1)[1].splitlines()
    assert len(skills) == len(SkillRegistry.builtin()) and all(len(line) < 200 for line in skills)
    assert skills[0].startswith("- agent-collaboration: Works effectively with peer agents")
    system = str(model.calls[0].messages[0].content)
    assert "Builtin tools (`builtin:<name>`) are always available" in system

    assert blueprint.toolkits == {"files": {"root": "./data"}}
    assert blueprint.check() == []


async def test_builtin_tools_are_offered_next_to_a_tool_catalog() -> None:
    draft = {**DRAFT, "toolkits": []}
    draft["agents"] = [{**DRAFT["agents"][0], "tools": ["builtin:calculator"]}]
    model = FakeChatModel([structured_output(BlueprintDesign, draft)])

    blueprint = await design("Report on the sales.", model=model, tools=[])

    brief = str(model.calls[0].last_message.content)
    assert "No tools are available" in brief and "- builtin:calculator:" in brief
    assert blueprint.agents[0].tools == ["builtin:calculator"] and blueprint.toolkits == {}


INBOX = REPO / "examples/benchmarks/core/04_document_intake/data/inbox"
PROCESS = "Hold every document that cannot be read reliably for manual entry."


def draft_expectations(
    *cases: dict[str, Any], rules: list[dict[str, Any]] | None = None
) -> AcceptanceDesign:
    return AcceptanceDesign.model_validate(
        {"criteria": ["Unreadable documents are held."], "cases": list(cases), "rules": rules or []}
    )


def holds(
    value: str, *, request: str = "Process invoice 4711.", name: str = "hold"
) -> dict[str, Any]:
    return {"id": name, "input": request, "expectations": [{"kind": "contains", "value": value}]}


def messages(found: list[Any]) -> list[str]:
    return [f"{issue.path}: {issue.message}" for issue in found]


def test_a_code_the_process_states_in_words_is_fine() -> None:
    process = "Book the clean ones and put everything else on hold with the reasons."

    assert acceptance_problems(draft_expectations(holds("on_hold")), set(), process=process) == []


def test_a_code_nobody_states_is_sent_back_with_the_plain_ways_out() -> None:
    found = acceptance_problems(
        draft_expectations(holds("on_hold")), set(), process="Book invoices."
    )

    [problem] = messages(found)
    assert problem.startswith("cases[0].expectations[0]: ")
    assert "'on_hold'" in problem and "plain word" in problem and "rule" in problem


def test_a_code_a_rule_of_the_design_returns_is_declared() -> None:
    rule = {
        "name": "hold_reason",
        "description": "Why a document is held.",
        "arguments": [{"name": "text_layer", "type": "boolean", "description": "Has text."}],
        "expression": "'no_usable_text_layer' if not text_layer else ''",
        "examples": [
            {"arguments": '{"text_layer": false}', "result": '"no_usable_text_layer"'},
            {"arguments": '{"text_layer": true}', "result": '""'},
        ],
    }

    declared = acceptance_problems(
        draft_expectations(holds("no_usable_text_layer"), rules=[rule]), set(), process="Hold."
    )
    assert declared == []
    assert (
        acceptance_problems(
            draft_expectations(holds("no_usable_text_layer")), set(), process="Hold."
        )
        != []
    )


def test_a_plain_word_and_a_request_value_need_no_declaration() -> None:
    plain = draft_expectations(
        holds("hold"), holds("RE-2024-17-A", request="Check RE-2024-17-A.", name="id")
    )

    assert acceptance_problems(plain, set(), process="Book invoices.") == []


def test_without_the_process_the_code_check_stays_silent() -> None:
    assert acceptance_problems(draft_expectations(holds("on_hold")), set()) == []


@pytest.mark.parametrize("answer", ["21,420.00 EUR", "21.420,00 EUR", "21420 EUR", "21 420"])
def test_a_grouped_number_matches_every_notation_of_the_answer(answer: str) -> None:
    spec = Expectation(kind="contains", value="21.420").spec()

    assert spec.startswith("regex:")
    assert re.search(spec.removeprefix("regex:"), answer)
    assert not re.search(spec.removeprefix("regex:"), "121.4201")


def test_a_word_and_an_id_stay_what_they_are() -> None:
    assert Expectation(kind="contains", value="RE-2024-17-A").spec() == "contains:RE-2024-17-A"
    assert Expectation(kind="contains", value="booked").spec() == "contains:booked"


@pytest.fixture(scope="module")
def inbox(tmp_path_factory: pytest.TempPathFactory) -> Any:
    folder = tmp_path_factory.mktemp("inbox")
    for name in ("scan_0001.pdf", "2024-0412_mueller.pdf"):
        (folder / name).write_bytes((INBOX / name).read_bytes())
    return prepare_data([folder])


def case(request: str, files: list[str], *values: str) -> dict[str, Any]:
    return {
        "id": "case-" + files[0].split(".")[0].lower().replace("_", "-"),
        "input": request,
        "files": files,
        "expectations": [{"kind": "contains", "value": v} for v in values],
    }


def test_the_name_of_a_file_the_request_names_is_no_check(inbox: Any) -> None:
    request = "Process 2024-0412_mueller.pdf."

    found = acceptance_problems(
        draft_expectations(case(request, ["2024-0412_mueller.pdf"], "2024-0412_mueller.pdf")),
        set(),
        inbox,
        process=PROCESS,
    )

    [problem] = messages(found)
    assert "name of a file" in problem and "value the file holds" in problem


def test_a_case_on_a_document_without_a_text_layer_expects_no_value_from_it(inbox: Any) -> None:
    request = "Process scan_0001.pdf."

    found = acceptance_problems(
        draft_expectations(
            case(request, ["scan_0001.pdf"], "1.428,00 EUR", "Müller & Söhne KG", "hold")
        ),
        set(),
        inbox,
        process=PROCESS,
    )

    problems = messages(found)
    assert len(problems) == 2 and all("no text layer" in p for p in problems)
    assert "'1.428,00 EUR'" in problems[0] and "'Müller & Söhne KG'" in problems[1]


def test_a_scan_found_by_the_name_in_the_request_counts_too(inbox: Any) -> None:
    named = {
        "id": "scan",
        "input": "Process scan_0001.pdf.",
        "expectations": [{"kind": "contains", "value": "4711-A"}],
    }

    assert messages(acceptance_problems(draft_expectations(named), set(), inbox, process=PROCESS))


def test_a_born_digital_document_keeps_its_values_and_a_scan_its_hold(inbox: Any) -> None:
    fine = acceptance_problems(
        draft_expectations(
            case("Process 2024-0412_mueller.pdf.", ["2024-0412_mueller.pdf"], "1.428,00", "Müller"),
            case("Process scan_0001.pdf.", ["scan_0001.pdf"], "hold"),
        ),
        set(),
        inbox,
        process=PROCESS,
    )

    assert messages(fine) == []


def squashed(text: str) -> str:
    return " ".join(text.split())


def test_the_test_designer_is_told_what_the_answer_and_the_tools_can_be_checked_for() -> None:
    """A case checks what the system produced: not phrases that only occur in the request, and
    not reads of a text the request already carries."""
    text = squashed(TEST_DESIGNER_INSTRUCTIONS).lower()
    assert "the answer is the system's final message" in text
    assert "a phrase that only occurs in the request is no check of the system" in text
    assert "only a tool the case cannot be done without" in text
    assert "when the request carries the text to work on" in text


def test_the_architect_is_told_that_the_last_answer_is_all_the_checks_see() -> None:
    text = squashed(ARCHITECT_INSTRUCTIONS)
    assert "contains, regex and judge expectations see only the final message" in text
    assert "the full text of anything it drafted or sent" in text


def test_a_value_the_request_itself_gives_may_be_expected_in_a_scan_case(inbox: Any) -> None:
    request = "Process scan_0001.pdf; the sender says it is invoice 4711-A."

    found = acceptance_problems(
        draft_expectations(case(request, ["scan_0001.pdf"], "4711-A", "hold")),
        set(),
        inbox,
        process=PROCESS,
    )

    assert messages(found) == []
