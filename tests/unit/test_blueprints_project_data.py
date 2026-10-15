"""design_project and run_project on the user's real files: briefs, refused inventions, checked
drafts, and results only in data/output/."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from _helpers.build_scripts import (
    ACCEPTANCE,
    BLUEPRINT,
    INVENTED,
    ORDERS_CSV,
    PAYMENTS_CSV,
    PLAIN_BLUEPRINT,
    PROCESS,
    Script,
    case,
    mailer,
    with_changes,
    with_outbox,
)
from awa.blueprints import (
    AcceptanceDesign,
    Blueprint,
    BlueprintError,
    LocalRunFiles,
    RunOutcome,
    _run_data,
    build_project,
    design_project,
    evaluate_project,
    prepare_data,
    run_project,
)
from awa.blueprints._project import acceptance_problems
from awa.blueprints._project_files import in_data_folder
from awa.blueprints._run_data import default_requests
from awa.contracts.usage import UsageLimits
from awa.data import DataError
from awa.testing import ModelCall, tool_call


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def design(script: Script, **options: Any) -> Any:
    return await design_project(
        PROCESS, model=script.model(), environment=None, data=[ORDERS_CSV], **options
    )


def draft(acceptance: dict[str, Any]) -> AcceptanceDesign:
    return AcceptanceDesign.model_validate(acceptance)


async def test_both_designers_get_the_profile_and_the_rules_for_real_files() -> None:
    script = Script()

    project = await design(script, notes={"orders.csv": "Check every open order."})

    for role in ("designer", "architect"):
        brief = script.prompts[role][0]
        assert "## Your data" in brief
        assert "<<<DATA f1>>>" in brief and "Betrag_EUR:decimal" in brief  # the digest
        assert "connector:files.query_csv (tool files_query_csv)" in brief
        assert "files_query_csv path='orders.csv' delimiter=';' decimal=','" in brief
        assert "- orders.csv: Check every open order." in brief  # the user's note, labelled
    assert "REAL files" in script.instructions["designer"]
    assert "numeric result" in script.instructions["designer"]
    assert "REAL files" in script.instructions["architect"]
    assert "What each file is for" in script.prompts["architect"][0]
    assert [n.file for n in project.data_notes] == ["orders.csv"]
    assert project.data is not None and project.data.profile.summary.files == 1


async def test_without_data_the_designers_get_the_plain_instructions() -> None:
    script = Script(acceptances=[INVENTED], blueprints=[PLAIN_BLUEPRINT])

    await design_project(PROCESS, model=script.model(), environment=None)

    assert "REAL files" not in script.instructions["designer"]
    assert "REAL files" not in script.instructions["architect"]
    assert "## Your data" not in script.prompts["designer"][0]


async def test_the_files_are_the_data_of_the_design_not_invented_records() -> None:
    project = await design(Script())

    assert project.systems == []  # no invented system
    connectors = project.blueprint.connectors
    assert connectors["files"]["root"] == "data/files"
    assert connectors["files"]["include"] == ["list_files", "read_csv", "query_csv"]
    assert "output" not in connectors  # the agent writes nothing
    assert [entry.name for entry in project.blueprint.data] == ["orders"]
    entry = project.blueprint.data[0]
    assert entry.path == "data/files/orders.csv" and entry.role == "input"
    assert entry.expect.header_row == 1 and entry.expect.decimal == ","
    # named in the instructions
    assert entry.expect.columns == ["Bestellnr", "Betrag_EUR", "Status"]
    assert entry.handling == "Flag every open order over 20,000 EUR."
    assert project.connections == []  # real files are nothing to connect


async def test_a_file_share_invented_for_real_files_is_sent_back() -> None:
    share = with_changes(
        ACCEPTANCE,
        systems=[
            {
                "name": "Order share",
                "key": "orders",
                "description": "Spreadsheets.",
                "kind": "files",
                "tables": [{"name": "t", "description": "d", "columns": [{"name": "a"}]}],
            }
        ],
    )
    script = Script(acceptances=[share, ACCEPTANCE])

    project = await design(script)

    assert script.calls("designer") == 2
    assert "the files are real: do not describe them as a system" in script.prompts["designer"][1]
    assert project.systems == []


def test_a_system_may_not_take_the_name_of_a_data_connector() -> None:
    clash = with_changes(
        ACCEPTANCE,
        systems=[
            {
                "name": "Mail",
                "key": "files",
                "description": "Sends mail.",
                "kind": "email",
                "tables": [{"name": "mail", "description": "d", "columns": [{"name": "a"}]}],
            }
        ],
    )

    problems = acceptance_problems(draft(clash), set(), prepare_data(ORDERS_CSV))

    assert any("key files, output" in p.message for p in problems)


async def test_an_unknown_input_file_of_a_case_is_sent_back_with_the_closest_name() -> None:
    wrong = with_changes(ACCEPTANCE)
    case(wrong, "big-order")["files"] = ["order.csv"]
    script = Script(acceptances=[wrong, ACCEPTANCE])

    await design(script)

    retry = script.prompts["designer"][1]
    assert "cases[0].files[0]" in retry and "[DF-V-009]" in retry and "orders.csv" in retry


async def test_a_note_on_an_unknown_file_is_sent_back() -> None:
    wrong = with_changes(ACCEPTANCE)
    wrong["data_notes"][0]["file"] = "bestellungen.csv"
    script = Script(acceptances=[wrong, ACCEPTANCE])

    await design(script)

    assert "data_notes[0].file" in script.prompts["designer"][1]


async def test_a_value_the_data_does_not_hold_is_sent_back_before_any_evaluation() -> None:
    wrong = with_changes(ACCEPTANCE)
    case(wrong, "big-order")["expectations"][0]["value"] = "B-9999"
    script = Script(acceptances=[wrong, ACCEPTANCE])

    await design(script)

    retry = script.prompts["designer"][1]
    assert "'B-9999'" in retry and "does not occur in the data" in retry and "[DF-V-008]" in retry


async def test_a_value_taken_from_the_request_is_not_an_invented_value() -> None:
    quoted = with_changes(ACCEPTANCE)
    big = case(quoted, "big-order")
    big["input"] = "Check order B-1004 and the invoice INV-777 for it."
    big["expectations"][0]["value"] = "INV-777"
    script = Script(acceptances=[quoted])

    await design(script)

    assert script.calls("designer") == 1


async def test_a_computed_result_the_rows_contradict_is_sent_back_with_the_real_value() -> None:
    wrong = with_changes(ACCEPTANCE)
    case(wrong, "open-total")["expectations"][0]["value"] = "50.000,00"
    script = Script(acceptances=[wrong, ACCEPTANCE])

    await design(script)

    retry = script.prompts["designer"][1]
    assert "[DF-V-010]" in retry and "46161.25" in retry and "50.000,00" in retry
    assert script.calls("designer") == 2


async def test_the_correct_computed_result_passes_in_one_round() -> None:
    script = Script()

    await design(script)

    assert script.calls("designer") == 1 and script.calls("architect") == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"check": "sum(Betrag_EUR where Status == 'offen')", "file": ""}, "name the file"),
        (
            {"check": "sum(Betrag_EUR)", "file": "orders.csv", "kind": "regex"},
            "belongs to a contains",
        ),
    ],
)
def test_a_check_needs_its_file_and_a_contains_expectation(
    change: dict[str, Any], message: str
) -> None:
    wrong = with_changes(ACCEPTANCE)
    big = case(wrong, "big-order")
    big["files"] = []
    big["expectations"][0] |= change

    problems = acceptance_problems(draft(wrong), set(), prepare_data(ORDERS_CSV))

    assert any(message in p.message for p in problems)


def test_check_and_file_are_refused_without_real_files() -> None:
    problems = acceptance_problems(draft(with_changes(ACCEPTANCE, rules=[])), set(), None)

    assert any("check and file are for real data files" in p.message for p in problems)


async def test_the_architect_naming_a_column_the_file_lacks_is_sent_back_with_the_real_one() -> (
    None
):
    wrong = with_changes(BLUEPRINT)
    wrong["agents"][0]["instructions"] = "Read orders.csv. Sum the column `Betrag` of open orders."
    script = Script(blueprints=[wrong, BLUEPRINT])

    project = await design(script)

    retry = script.prompts["architect"][1]
    assert "`Betrag` is not a column of any declared data file" in retry
    assert "did you mean 'Betrag_EUR'" in retry
    assert script.calls("architect") == 2
    assert "`Betrag_EUR`" in project.blueprint.agents[0].instructions


async def test_a_wrong_header_row_in_the_instructions_is_sent_back() -> None:
    wrong = with_changes(BLUEPRINT)
    wrong["agents"][0]["instructions"] = "Read orders.csv with header_row=3, then screen it."
    script = Script(blueprints=[wrong, BLUEPRINT])

    await design(script)

    assert "[DF-V-011]" in script.prompts["architect"][1]


async def test_the_design_gives_up_after_the_repairs_when_the_columns_stay_wrong() -> None:
    wrong = with_changes(BLUEPRINT)
    wrong["agents"][0]["instructions"] = "Sum the column `Betrag` of open orders."
    script = Script(blueprints=[wrong])

    with pytest.raises(BlueprintError, match="did you mean .Betrag_EUR."):
        await design(script, max_repairs=1)


async def test_events_report_the_design_and_what_was_sent_back() -> None:
    wrong = with_changes(ACCEPTANCE)
    case(wrong, "big-order")["expectations"][0]["value"] = "B-9999"
    script = Script(acceptances=[wrong, ACCEPTANCE])
    events: list[tuple[str, dict[str, Any]]] = []

    await design(script, on_event=lambda name, facts: events.append((name, dict(facts))))

    assert [name for name, _ in events] == [
        "design.validation",
        "design.acceptance",
        "design.architecture",
    ]
    first = events[0][1]
    assert first["source"] == "acceptance" and first["round"] == 1
    assert first["issues"][0]["code"] == "DF-V-008"
    assert first["issues"][0]["path"] == "cases[0].expectations[0]"
    assert events[1][1]["cases"] == ["big-order", "open-total"] and events[1][1]["repairs"] == 1
    assert events[2][1]["agents"] == ["screener"]


async def test_the_originals_are_never_touched_by_a_design() -> None:
    before = (sha(ORDERS_CSV), ORDERS_CSV.stat().st_mtime_ns)

    await design(Script())

    assert (sha(ORDERS_CSV), ORDERS_CSV.stat().st_mtime_ns) == before


async def test_a_note_for_a_file_that_is_not_there_stops_before_any_model_call() -> None:
    script = Script()

    with pytest.raises(DataError, match="matches no file"):
        await design(script, notes={"bank.csv": "match it"})

    assert script.calls("designer") == 0


async def test_every_file_gets_its_own_read_hints() -> None:
    script = Script()

    await design_project(
        PROCESS, model=script.model(), environment=None, data=[ORDERS_CSV, PAYMENTS_CSV]
    )

    brief = script.prompts["designer"][0]
    assert "decimal='.'" in brief and "decimal=','" in brief  # each file has its own number format


async def test_builtin_tools_may_use_the_data_folder_as_their_root_but_not_a_folder_that_is_not_there() -> (
    None
):
    builtin = with_changes(BLUEPRINT)
    agent = builtin["agents"][0]
    agent["tools"] = ["builtin:read_csv", "needs_manager_approval"]
    builtin["toolkits"] = [{"toolkit": "files", "option": "root", "value": "data/files"}]
    elsewhere = with_changes(
        builtin, toolkits=[{"toolkit": "files", "option": "root", "value": "./invoices"}]
    )
    script = Script(blueprints=[elsewhere, builtin])

    project = await design(script)

    assert (
        "./invoices" in script.prompts["architect"][1]
        and "does not exist" in script.prompts["architect"][1]
    )
    assert project.blueprint.toolkits["files"]["root"] == "data/files"


async def test_a_builtin_tool_on_the_data_folder_reads_the_run_s_own_copy() -> None:
    builtin = with_changes(BLUEPRINT)
    builtin["agents"][0]["tools"] = ["builtin:read_csv"]
    builtin["toolkits"] = [{"toolkit": "data", "option": "root", "value": "data/files"}]
    acceptance = with_changes(ACCEPTANCE, rules=[])
    acceptance["cases"] = [
        {
            "id": "read",
            "input": "Read orders.csv.",
            "files": ["orders.csv"],
            "expectations": [
                {"kind": "contains", "value": "B-1004"},
                {"kind": "tool_called", "value": "read_csv"},
            ],
        }
    ]

    def agent(call: Any) -> Any:
        results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        if results:
            return f"Found {results[-1][:3000]}"
        return tool_call("read_csv", {"path": "orders.csv", "delimiter": ";", "limit": 20})

    script = Script(acceptances=[acceptance], blueprints=[builtin], agent=agent)
    project = await design(script)

    report = await evaluate_project(project, model=script.model())

    assert report.runs[0].status == "passed", (report.runs[0].scores, report.runs[0].output)
    assert report.runs[0].tool_calls == ["read_csv"]


def test_gated_builtin_tools_may_be_named_in_acceptance_expectations_when_files_are_given() -> None:
    acceptance = with_changes(ACCEPTANCE, rules=[])
    acceptance["cases"] = [
        {
            "id": "save",
            "input": "Save a summary of orders.csv.",
            "files": ["orders.csv"],
            "expectations": [
                {"kind": "approval", "value": "write_file"},
                {"kind": "approval", "value": "sql_execute"},
            ],
            "decisions": [{"action": "approve"}],
        }
    ]
    data = prepare_data(ORDERS_CSV)

    with_data = acceptance_problems(draft(acceptance), set(), data)
    without = acceptance_problems(draft(with_changes(INVENTED, systems=[])), set(), None)

    assert not [p for p in with_data if "expects a writing tool" in p.message]
    assert without == []
    plain = acceptance_problems(draft(with_changes(acceptance, data_notes=[])), set(), None)
    assert any("expects a writing tool of a system" in p.message for p in plain)


async def test_events_tell_what_the_architect_got_wrong_and_when() -> None:
    wrong = with_changes(BLUEPRINT)
    wrong["agents"][0]["instructions"] = "Sum the column `Betrag` of open orders."
    script = Script(blueprints=[wrong, BLUEPRINT])
    events: list[tuple[str, dict[str, Any]]] = []

    await design(script, on_event=lambda name, facts: events.append((name, dict(facts))))

    name, facts = events[1]
    assert name == "design.validation" and facts["source"] == "architecture" and facts["round"] == 1
    issue = facts["issues"][0]
    assert issue["path"] == "agents[0].instructions" and issue["severity"] == "error"
    assert "Betrag_EUR" in issue["message"]


def test_a_phrase_of_the_data_is_not_the_system_s_wording_but_an_invented_phrase_is() -> None:
    quoted = with_changes(ACCEPTANCE, rules=[])
    big = case(quoted, "big-order")
    big["expectations"][0] = {"kind": "contains", "value": "Kowalski Stahl Sp. z o.o."}
    invented_phrase = with_changes(quoted)
    case(invented_phrase, "big-order")["expectations"][0] = {
        "kind": "contains",
        "value": "needs the purchasing manager today",
    }
    data = prepare_data(ORDERS_CSV)

    assert not acceptance_problems(draft(quoted), set(), data)
    found = acceptance_problems(draft(invented_phrase), set(), data)
    assert any("is wording the answer may phrase differently" in p.message for p in found)


@pytest.mark.parametrize(
    ("value", "inside"),
    [
        ("data", True),
        ("./data", True),
        ("data/files", True),
        ("data/files/inbox", True),
        ("data/../secrets", False),
        ("data/files/../../x", False),
        ("database", False),
        ("/data", False),
        ("C:/data", False),
        ("data\files", False),
        ("invoices", False),
    ],
)
def test_only_the_data_folder_and_what_is_below_it_counts_as_the_data_folder(
    value: str, inside: bool
) -> None:
    assert in_data_folder(value) is inside


def writer(call: ModelCall) -> Any:
    """Reads the request, then writes `<file name>.txt` into the output folder."""

    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    if any(isinstance(m, ToolMessage) for m in call.messages):
        reply = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)][-1]
        return f"Done: {reply[:60]}"
    if "boom.csv" in request:
        raise RuntimeError("the model is down")
    name = "payments" if "payments" in request else "orders"
    return tool_call("output_write_file", {"path": f"{name}.txt", "content": f"handled: {request}"})


SAVER = with_changes(BLUEPRINT)
SAVER["agents"][0]["tools"] = ["connector:files.query_csv", "connector:output.write_file"]
SAVER_CASES = with_changes(ACCEPTANCE, rules=[])
SAVER_CASES["cases"] = [
    {
        "id": "save",
        "input": "Process orders.csv.",
        "files": ["orders.csv"],
        "expectations": [{"kind": "approval", "value": "output_write_file"}],
        "decisions": [{"action": "approve", "tool": "output_write_file"}],
    }
]


async def built(tmp_path: Path, **options: Any) -> Path:
    script = Script(acceptances=[SAVER_CASES], blueprints=[SAVER], agent=writer)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for source in (ORDERS_CSV, PAYMENTS_CSV):
        (inputs / source.name).write_bytes(source.read_bytes())
    out = tmp_path / "project"
    report = await build_project(
        PROCESS,
        out_dir=out,
        model=script.model(),
        environment=None,
        data=[inputs],
        fit="rules",
        **options,
    )
    assert report.passed, report.summary()
    return out


def approve(_: dict[str, Any]) -> dict[str, str]:
    return {"action": "approve"}


async def run(project: Path, **options: Any) -> list[RunOutcome]:
    blueprint = Blueprint.from_yaml_file(project / "blueprint.yaml")
    model = Script(agent=writer).model()
    return await run_project(
        blueprint, LocalRunFiles(project), model=model, allow_imports=True, **options
    )


async def test_one_run_per_input_file_with_the_result_only_in_the_output_folder(
    tmp_path: Path,
) -> None:
    project = await built(tmp_path, notes={"orders.csv": "Flag the big ones."})
    inputs = {name: sha(project / "data/files" / name) for name in ("orders.csv", "payments.csv")}
    done: list[RunOutcome] = []

    outcomes = await run(project, decide=approve, on_outcome=done.append)

    assert [o.file for o in outcomes] == ["orders.csv", "payments.csv"] and done == outcomes
    assert [o.status for o in outcomes] == ["completed", "completed"]
    assert outcomes[0].outputs == ("data/output/orders.csv/orders.txt",)
    assert outcomes[1].outputs == ("data/output/payments.csv/payments.txt",)
    written = (project / outcomes[0].outputs[0]).read_text(encoding="utf-8")
    assert "Process orders.csv" in written and "Flag the big ones." in written  # request + note
    assert outcomes[0].approvals == ("output_write_file",)
    assert outcomes[0].tool_calls == ("output_write_file",) and outcomes[0].usage.requests == 2
    outputs = sorted(
        p.relative_to(project).as_posix() for p in (project / "data/output").rglob("*")
    )
    assert [p for p in outputs if (project / p).is_file()] == [
        "data/output/orders.csv/orders.txt",
        "data/output/payments.csv/payments.txt",
    ]
    for name, digest in inputs.items():  # the copies of the inputs are never changed
        assert sha(project / "data/files" / name) == digest


async def test_the_report_of_a_run_names_the_input_its_digest_the_calls_and_the_decision(
    tmp_path: Path,
) -> None:
    project = await built(tmp_path)
    ids = iter(["run-a", "run-b"])
    ticks = iter(datetime(2026, 10, 5, 12, 0, s, tzinfo=UTC) for s in range(1, 9))

    outcomes = await run(
        project,
        select=["orders.csv"],
        decide=approve,
        new_id=lambda prefix: next(ids),
        clock=lambda: next(ticks),
    )

    (outcome,) = outcomes
    assert (outcome.run_id, outcome.report) == ("run-a", "runs/run-a.json")
    report = json.loads((project / "runs/run-a.json").read_text(encoding="utf-8"))
    assert report["sha256"] == sha(project / "data/files/orders.csv")
    assert (report["file"], report["status"]) == ("orders.csv", "completed")
    assert report["started"] == "2026-10-05T12:00:01+00:00"
    assert report["finished"] == "2026-10-05T12:00:02+00:00"
    assert report["tool_calls"] == ["output_write_file"] and report["decisions"] == ["approve"]
    assert report["approvals"] == ["output_write_file"]
    assert report["outputs"] == ["data/output/orders.csv/orders.txt"]
    assert report["usage"]["requests"] == 2 and report["answer"].startswith("Done: Wrote")


async def test_without_a_decision_the_run_ends_paused_and_writes_nothing(tmp_path: Path) -> None:
    project = await built(tmp_path)

    outcomes = await run(project, select=["orders.csv"])

    (outcome,) = outcomes
    assert outcome.status == "paused" and outcome.approvals == ("output_write_file",)
    assert outcome.outputs == () and outcome.answer == ""
    assert not list((project / "data/output").glob("*/*.txt"))
    report = json.loads((project / outcome.report).read_text(encoding="utf-8"))
    assert report["status"] == "paused"


async def test_a_rejection_lets_the_run_finish_without_the_write(tmp_path: Path) -> None:
    project = await built(tmp_path)

    def reject(_: dict[str, Any]) -> dict[str, str]:
        return {"action": "reject", "note": "not today"}

    (outcome,) = await run(project, select=["orders.csv"], decide=reject)

    assert outcome.status == "completed" and outcome.outputs == ()
    assert not list((project / "data/output").glob("*/*.txt"))


async def test_a_failed_run_does_not_stop_the_others(tmp_path: Path) -> None:
    project = await built(tmp_path)
    orders = project / "data/files/orders.csv"
    (project / "data/files/boom.csv").write_bytes(orders.read_bytes())
    blueprint = Blueprint.from_yaml_file(project / "blueprint.yaml")
    entries = [
        *blueprint.data,
        blueprint.data[0].model_copy(update={"name": "boom", "path": "data/files/boom.csv"}),
    ]
    blueprint = blueprint.model_copy(update={"data": entries})

    outcomes = await run_project(
        blueprint,
        LocalRunFiles(project),
        model=Script(agent=writer).model(),
        allow_imports=True,
        decide=approve,
    )

    by_name = {o.file: o for o in outcomes}
    assert by_name["boom.csv"].status == "failed"
    assert "RuntimeError: the model is down" in by_name["boom.csv"].error
    assert (
        by_name["orders.csv"].status == "completed"
        and by_name["payments.csv"].status == "completed"
    )
    assert (
        json.loads((project / by_name["boom.csv"].report).read_text("utf-8"))["status"] == "failed"
    )


async def test_a_file_operation_that_fails_ends_that_run_only(tmp_path: Path) -> None:
    project = await built(tmp_path)

    class BrokenCopy(LocalRunFiles):
        def collect(self, workspace: Path, name: str) -> list[str]:
            if name == "orders.csv":
                raise PermissionError("data/output is locked")
            return super().collect(workspace, name)

    outcomes = await run_project(
        Blueprint.from_yaml_file(project / "blueprint.yaml"),
        BrokenCopy(project),
        model=Script(agent=writer).model(),
        allow_imports=True,
        decide=approve,
    )

    assert [(o.file, o.status) for o in outcomes] == [
        ("orders.csv", "failed"),
        ("payments.csv", "completed"),
    ]
    assert outcomes[0].error == "PermissionError: data/output is locked"
    assert json.loads((project / outcomes[0].report).read_text("utf-8"))["status"] == "failed"


async def test_a_usage_limit_ends_that_run_as_failed_with_the_reason(tmp_path: Path) -> None:
    project = await built(tmp_path)

    (outcome,) = await run(
        project, select=["orders.csv"], decide=approve, usage_limits=UsageLimits(max_requests=1)
    )

    assert outcome.status == "failed" and "usage limit" in outcome.error


async def test_a_request_template_and_the_glob_pick_the_files(tmp_path: Path) -> None:
    project = await built(tmp_path)
    blueprint = Blueprint.from_yaml_file(project / "blueprint.yaml")
    entries = [e.model_copy(update={"request": "Handle {file} now"}) for e in blueprint.data]
    blueprint = blueprint.model_copy(update={"data": entries})

    outcomes = await run_project(
        blueprint,
        LocalRunFiles(project),
        model=Script(agent=writer).model(),
        allow_imports=True,
        select=["PAY*"],
        decide=approve,
    )

    assert [o.file for o in outcomes] == ["payments.csv"]
    assert "Handle payments.csv now" in (project / outcomes[0].outputs[0]).read_text("utf-8")


async def test_a_selection_that_matches_nothing_is_an_error(tmp_path: Path) -> None:
    project = await built(tmp_path)

    with pytest.raises(DataError, match="no input file matches 'bank.csv'"):
        await run(project, select=["bank.csv"])


async def test_a_blueprint_without_input_files_cannot_be_run(tmp_path: Path) -> None:
    project = await built(tmp_path)
    blueprint = Blueprint.from_yaml_file(project / "blueprint.yaml").model_copy(update={"data": []})

    with pytest.raises(BlueprintError, match="names no input file"):
        await run_project(blueprint, LocalRunFiles(project))


def test_a_folder_without_copies_of_files_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DataError, match="holds no data/files/ folder"):
        LocalRunFiles(tmp_path)


async def test_the_run_files_stay_inside_the_data_files_folder(tmp_path: Path) -> None:
    project = await built(tmp_path)
    files = LocalRunFiles(project)
    (project / "secret.txt").write_text("outside", encoding="utf-8")

    assert files.inputs("data/files/orders.csv") == ["orders.csv"]
    assert files.inputs("data/files/*.csv") == ["orders.csv", "payments.csv"]
    # a folder: everything in it
    assert files.inputs("data/files") == ["orders.csv", "payments.csv"]
    assert files.inputs("data/files/../../secret.txt") == []  # no way out
    assert files.inputs("secret.txt") == [] and files.inputs("data/output") == []
    with pytest.raises(DataError, match="is not a file of data/files/"):
        files.digest("../../secret.txt")


async def test_a_run_never_sees_the_results_of_an_earlier_run(tmp_path: Path) -> None:
    project = await built(tmp_path)
    stale = project / "data/output/orders.csv/old.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")

    (outcome,) = await run(project, select=["orders.csv"], decide=approve)

    assert not stale.exists()  # the result of this input replaces its earlier one
    assert outcome.outputs == ("data/output/orders.csv/orders.txt",)


@pytest.mark.slow
async def test_a_file_in_a_subfolder_is_picked_by_its_name_or_its_path(tmp_path: Path) -> None:
    project = await built(tmp_path)
    nested = project / "data/files/inbox"
    nested.mkdir()
    (nested / "late.csv").write_bytes((project / "data/files/orders.csv").read_bytes())
    blueprint = Blueprint.from_yaml_file(project / "blueprint.yaml")
    entry = blueprint.data[0].model_copy(
        update={"name": "late", "path": "data/files/inbox/late.csv"}
    )
    blueprint = blueprint.model_copy(update={"data": [*blueprint.data, entry]})

    async def only(pattern: str) -> list[str]:
        outcomes = await run_project(
            blueprint,
            LocalRunFiles(project),
            model=Script(agent=writer).model(),
            allow_imports=True,
            select=[pattern],
            decide=approve,
        )
        return [o.file for o in outcomes]

    assert await only("late.csv") == ["inbox/late.csv"]  # the last part of the name
    assert await only("inbox/*") == ["inbox/late.csv"]  # the name below data/files
    assert await only("*.csv") == ["orders.csv", "payments.csv", "inbox/late.csv"]  # in data: order


async def test_an_approval_nobody_decides_leaves_the_run_paused_and_asks_no_one_else(
    tmp_path: Path,
) -> None:
    project = await built(tmp_path)
    asked: list[dict[str, Any]] = []

    def nobody(pending: dict[str, Any]) -> None:
        asked.append(pending)

    (outcome,) = await run(project, select=["orders.csv"], decide=nobody)

    assert outcome.status == "paused" and outcome.outputs == () and len(asked) == 1
    assert asked[0]["value"]["tool"] == "output_write_file"  # what the person is asked about


async def test_what_a_run_sends_through_an_outbox_lands_in_the_output_folder_of_its_input(
    tmp_path: Path,
) -> None:
    """The reply a person approved is a result: it is kept with the others, not lost with the
    temporary copy of the run."""

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "orders.csv").write_bytes(ORDERS_CSV.read_bytes())
    out = tmp_path / "project"
    script = with_outbox()
    report = await build_project(
        PROCESS, out_dir=out, model=script.model(), environment=None, data=[inputs], fit="rules"
    )
    assert report.passed, report.summary()
    blueprint = Blueprint.from_yaml_file(out / "blueprint.yaml")

    (outcome,) = await run_project(
        blueprint,
        LocalRunFiles(out),
        model=Script(agent=mailer).model(),
        decide=approve,
        allow_imports=True,
    )

    assert outcome.status == "completed", outcome.error
    assert outcome.approvals == ("outbound_email_execute",)
    assert outcome.outputs == ("data/output/orders.csv/outbound_email.db",)
    with closing(sqlite3.connect(out / outcome.outputs[0])) as connection:
        rows = connection.execute("SELECT recipient FROM outgoing_messages").fetchall()
    assert rows == [("kunde@example.org",)]
    assert not (out / "data/outbound_email.db").exists()
    # the template is none
    assert [p.name for p in (out / "data/output").iterdir()] == ["orders.csv"]


def two_agents(first: int | None = None) -> Blueprint:
    agents: dict[str, Any] = {
        "reader": {"description": "Reads.", "max_steps": first},
        "writer": {"description": "Writes."},
    }
    return Blueprint.from_dict(
        {"name": "p", "agents": agents, "entrypoint": "workflow", "workflow": ["reader", "writer"]}
    )


def test_a_run_may_make_as_many_model_calls_as_its_agents_may_take_steps() -> None:
    """Without a limit the bound is the sum of what each agent may do, 25 steps unless its
    blueprint says otherwise."""

    assert default_requests(two_agents()) == 50
    assert default_requests(two_agents(first=4)) == 29


async def test_a_run_without_a_request_limit_gets_the_derived_one_and_a_given_one_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[UsageLimits | None] = []

    async def spy(*args: Any) -> RunOutcome:
        seen.append(args[7])  # the limits of that run
        return RunOutcome(file=args[2], status="completed")

    monkeypatch.setattr(_run_data, "_run_one", spy)
    project = await built(tmp_path)
    blueprint = Blueprint.from_yaml_file(project / "blueprint.yaml")
    derived = _run_data.default_requests(blueprint)

    async def limits(given: UsageLimits | None) -> int | None:
        seen.clear()
        await run_project(
            blueprint, LocalRunFiles(project), select=["orders.csv"], usage_limits=given
        )
        assert seen[0] is not None
        return seen[0].max_requests

    assert await limits(None) == derived
    # another limit: still bounded
    assert await limits(UsageLimits(max_total_tokens=1000)) == derived
    assert await limits(UsageLimits(max_requests=7)) == 7


def test_inputs_with_the_same_stem_keep_apart_output_folders(tmp_path: Path) -> None:
    (tmp_path / "data" / "files").mkdir(parents=True)
    files = LocalRunFiles(tmp_path)
    copies = []
    for name in ("orders.csv", "orders.xlsx"):
        workspace = tmp_path / f"ws_{name}"
        (workspace / "data" / "output").mkdir(parents=True)
        (workspace / "data" / "output" / "result.txt").write_text(name, encoding="utf-8")
        copies += files.collect(workspace, name)

    assert copies == ["data/output/orders.csv/result.txt", "data/output/orders.xlsx/result.txt"]
    assert (tmp_path / copies[0]).read_text(encoding="utf-8") == "orders.csv"


async def test_a_run_that_keeps_asking_for_approval_is_left_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_run_data, "_MAX_PAUSES", 0)
    project = await built(tmp_path)

    outcomes = await run(project, decide=approve)

    assert {o.status for o in outcomes} == {"paused"}
    assert "approvals in one run" in outcomes[0].error
