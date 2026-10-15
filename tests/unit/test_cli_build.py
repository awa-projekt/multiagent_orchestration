"""`awa build` and `awa connect`, with `--data/--note/--mask/--answer`, and `awa data run`."""

from __future__ import annotations

import asyncio
import builtins
import getpass
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_blueprints_build_loop import model
from test_blueprints_project_data import built, writer

import awa.blueprints
import awa.connectors
from _helpers.build_scripts import ORDERS_CSV, PAYMENTS_CSV, Script
from awa.blueprints import Blueprint
from awa.cli import COMMANDS, main


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run `build` and `design_project` with the scripted model and no environment."""

    seen: dict[str, Any] = {}
    build, design_project = awa.blueprints.build_project, awa.blueprints.design_project

    async def fake_build(process: str, **options: Any) -> Any:
        seen.update(options)
        return await build(process, **{**options, "model": model(), "environment": None})

    async def fake_design(process: str, **options: Any) -> Any:
        seen.update(options)
        return await design_project(process, **{**options, "model": model(), "environment": None})

    monkeypatch.setattr(awa.blueprints, "build_project", fake_build)
    monkeypatch.setattr(awa.blueprints, "design_project", fake_design)
    return seen


def test_build_writes_the_project_and_prints_the_summary(
    offline: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "desk"

    code = main(["build", "Orders.", "--out", str(out), "--rounds", "2", "--max-requests", "80"])

    captured = capsys.readouterr()
    assert code == 0
    assert "2 of 2 eval cases pass" in captured.out
    assert "[repair] Repairing the design from the failing cases" in captured.err
    assert offline["max_rounds"] == 2 and offline["usage_limits"].max_requests == 80
    assert (out / "CONNECT.md").is_file()
    assert Blueprint.from_yaml(out / "blueprint.yaml").connectors["orders"]["use"] == "workspace"


def test_build_exits_1_below_the_target(
    offline: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["build", "Orders.", "--out", str(tmp_path / "desk"), "--rounds", "1"])

    assert code == 1
    assert "target 100% not reached" in capsys.readouterr().out


def test_connect_asks_with_hidden_input_and_tests_the_connector(
    offline: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "desk"
    main(["build", "Orders.", "--out", str(out), "--rounds", "2"])
    for name in ("ORDERS_DATABASE_URL", "TICKETS_SPEC", "ISSUES_SPEC"):
        monkeypatch.setenv(name, "")  # removed again after the test
    monkeypatch.setattr(
        getpass, "getpass", lambda prompt: f"sqlite:///{out / 'data' / 'orders.db'}"
    )
    checked: list[str] = []

    def check(name: str, **options: Any) -> Any:
        checked.append(name)
        return awa.connectors.ConnectorCheck(name, True, "SQLite database orders.db is readable")

    monkeypatch.setattr(awa.connectors, "check_connector", check)

    assert main(["connect", str(out)]) == 0
    assert checked == ["orders", "tickets", "issues"]
    assert "test passed: SQLite database orders.db is readable" in capsys.readouterr().out
    assert Blueprint.from_yaml(out / "blueprint.yaml").connectors["orders"]["use"] == "sql"


def test_the_build_commands_are_registered() -> None:
    assert {"build", "connect"} <= set(COMMANDS)


@pytest.mark.parametrize("command", ["ui", "connectors"])
def test_the_ui_and_connectors_commands_are_registered(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        main([command, "--help"])

    assert exited.value.code == 0
    assert f"usage: awa {command}" in capsys.readouterr().out


PROCESS = "Purchase orders over 20,000 EUR need the purchasing manager's approval."
CV_PROCESS = "Screen the incoming CVs against our role profile and schedule interviews."


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`build_project` and `run_project` with the scripted model and no environment."""

    seen: dict[str, Any] = {"script": Script()}
    build, run = awa.blueprints.build_project, awa.blueprints.run_project

    async def fake_build(process: str, **options: Any) -> Any:
        seen.update(options)
        return await build(
            process, **{**options, "model": seen["script"].model(), "environment": None}
        )

    async def fake_run(blueprint: Any, files: Any, **options: Any) -> Any:
        seen["run"] = options
        return await run(blueprint, files, **{**options, "model": Script(agent=writer).model()})

    monkeypatch.setattr(awa.blueprints, "build_project", fake_build)
    monkeypatch.setattr(awa.blueprints, "run_project", fake_run)
    return seen


def terminal(monkeypatch: pytest.MonkeyPatch, *typed: str) -> list[str]:
    """Make the command believe it runs in a terminal where `typed` is entered, line by line."""

    prompts: list[str] = []
    lines = iter(typed)

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", answer)
    return prompts


def test_build_on_files_copies_them_and_passes_the_options_on(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "project"

    code = main(
        ["build", PROCESS, "--out", str(out), "--data", str(ORDERS_CSV)]
        + ["--note", "orders.csv=Flag the big ones.", "--mask", "placeholders"]
    )

    captured = capsys.readouterr()
    assert code == 0 and "2 of 2 eval cases pass" in captured.out
    assert scripted["data"] == [str(ORDERS_CSV)] and scripted["mask"] == "placeholders"
    assert scripted["notes"] == {"orders.csv": "Flag the big ones."}
    assert "[profile] orders.csv: csv (ok)" in captured.err
    assert (out / "data/files/orders.csv").read_bytes() == ORDERS_CSV.read_bytes()


def test_a_build_without_data_designs_without_files_or_notes(
    scripted: dict[str, Any], tmp_path: Path
) -> None:
    code = main(["build", "Orders.", "--out", str(tmp_path / "x"), "--rounds", "1", "--no-input"])

    assert scripted["data"] is None and scripted["notes"] == {} and scripted["answers"] == {}
    assert code == 1  # the scripted designer of the data tests knows no order status


def test_a_description_that_does_not_fit_prints_json_questions_and_exits_2(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "project"

    code = main(["build", CV_PROCESS, "--out", str(out), "--data", str(PAYMENTS_CSV), "--no-input"])

    captured = capsys.readouterr()
    assert code == 2 and not out.exists()
    shown = json.loads(captured.out)
    assert shown["status"] == "needs_input" and shown["questions"][0]["id"] == "q1"
    assert shown["questions"][0]["evidence"]  # cites the profile
    assert captured.err.count("Before anything is designed") == 1  # said once, not twice
    assert captured.err.count("q1: ") == 1


def test_the_answer_resumes_the_build(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "project"
    args = ["build", CV_PROCESS, "--out", str(out), "--data", str(ORDERS_CSV), "--no-input"]

    assert main(args) == 2
    capsys.readouterr()
    code = main([*args, "--answer", "q1=Check the purchase orders against the approval limit."])

    assert code == 0 and (out / "blueprint.yaml").is_file()
    assert scripted["answers"] == {"q1": "Check the purchase orders against the approval limit."}
    brief = scripted["script"].prompts["designer"][0]
    assert "- q1: Check the purchase orders against the approval limit." in brief


def test_a_bare_answer_answers_the_questions_as_a_whole(
    scripted: dict[str, Any], tmp_path: Path
) -> None:
    code = main(
        ["build", CV_PROCESS, "--out", str(tmp_path / "p"), "--data", str(ORDERS_CSV)]
        + ["--no-input", "--answer", "Use them for purchase orders, not for CVs"]
    )

    assert code == 0
    assert scripted["answers"] == {"answer_1": "Use them for purchase orders, not for CVs"}


def test_on_a_terminal_the_questions_are_asked_and_the_build_goes_on(
    scripted: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts = terminal(monkeypatch, "Check purchase orders against the approval limit.", "")
    out = tmp_path / "project"

    code = main(["build", CV_PROCESS, "--out", str(out), "--data", str(ORDERS_CSV)])

    captured = capsys.readouterr()
    assert code == 0 and (out / "blueprint.yaml").is_file()
    assert "q1:" in captured.err and "Your answer: " in prompts
    assert scripted["answers"]["q1"].startswith("Check purchase orders")


def test_without_an_answer_on_a_terminal_the_build_stays_waiting(
    scripted: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal(monkeypatch)  # nothing typed: end of input

    code = main(["build", CV_PROCESS, "--out", str(tmp_path / "p"), "--data", str(PAYMENTS_CSV)])

    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "needs_input"


def test_files_nothing_can_be_built_from_are_refused_with_exit_1(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    picture = tmp_path / "scan.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)

    code = main(["build", PROCESS, "--out", str(tmp_path / "p"), "--data", str(picture)])

    assert code == 1 and json.loads(capsys.readouterr().out)["status"] == "blocked"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--note", "orders.csv=text"], "belong to --data"),
        (["--mask", "drop"], "belong to --data"),
        (["--answer", "yes"], "belong to --data"),
    ],
)
def test_data_options_without_data_are_an_error(
    scripted: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    message: str,
) -> None:
    code = main(["build", PROCESS, "--out", str(tmp_path / "p"), *extra])

    assert code == 1 and message in capsys.readouterr().err


@pytest.mark.parametrize("note", ["orders.csv", "=text", "orders.csv="])
def test_a_malformed_note_is_an_error(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], note: str
) -> None:
    code = main(
        ["build", PROCESS, "--out", str(tmp_path / "p"), "--data", str(ORDERS_CSV), "--note", note]
    )

    assert code == 1 and "--note needs FILE=TEXT" in capsys.readouterr().err


def test_a_note_for_a_file_that_is_not_there_names_the_files(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["build", PROCESS, "--out", str(tmp_path / "p"), "--data", str(ORDERS_CSV)]
        + ["--note", "bank.csv=match them"]
    )

    err = capsys.readouterr().err
    assert code == 1 and "matches no file" in err and "orders.csv" in err


def test_a_data_path_that_does_not_exist_is_an_error_before_any_model_call(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["build", PROCESS, "--out", str(tmp_path / "p"), "--data", str(tmp_path / "no.csv")]
    )

    assert code == 1 and "error:" in capsys.readouterr().err
    assert scripted["script"].calls("designer") == 0


def test_two_notes_for_one_file_are_joined(scripted: dict[str, Any], tmp_path: Path) -> None:
    main(
        ["build", PROCESS, "--out", str(tmp_path / "p"), "--data", str(ORDERS_CSV)]
        + ["--note", "orders.csv=First.", "--note", "orders.csv=Second."]
    )

    assert scripted["notes"] == {"orders.csv": "First. Second."}


# ---------------------------------------------------------------------- awa data run


def test_data_run_runs_every_input_and_prints_where_the_results_went(
    scripted: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = asyncio.run(built(tmp_path))
    terminal(monkeypatch, "y", "y")

    code = main(["data", "run", str(project), "--file", "*"])

    out = capsys.readouterr().out
    assert code == 0
    assert "orders.csv: completed; wrote data/output/orders.csv/orders.txt (runs/" in out
    assert "payments.csv: completed; wrote data/output/payments.csv/payments.txt" in out
    assert (project / "data/output/orders.csv/orders.txt").is_file()


def test_data_run_without_a_terminal_ends_paused_with_exit_2(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = asyncio.run(built(tmp_path))

    code = main(["data", "run", str(project), "--file", "orders.csv"])

    captured = capsys.readouterr()
    assert code == 2 and "paused for an approval" in captured.err
    assert not list((project / "data/output").glob("*/*.txt"))


def test_data_run_no_input_never_asks_even_on_a_terminal(
    scripted: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = asyncio.run(built(tmp_path))
    prompts = terminal(monkeypatch, "y")

    code = main(["data", "run", str(project), "--file", "*", "--no-input"])

    assert code == 2 and prompts == []


def test_data_run_json_lists_the_outcomes(
    scripted: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = asyncio.run(built(tmp_path))
    terminal(monkeypatch, "y")

    code = main(["data", "run", str(project / "blueprint.yaml"), "--file", "pay*", "--json"])

    (outcome,) = json.loads(capsys.readouterr().out)
    assert code == 0 and outcome["file"] == "payments.csv" and outcome["status"] == "completed"
    assert outcome["outputs"] == ["data/output/payments.csv/payments.txt"]


def test_data_run_limits_tighten_each_run(scripted: dict[str, Any], tmp_path: Path) -> None:
    project = asyncio.run(built(tmp_path))

    code = main(["data", "run", str(project), "--file", "*", "--max-requests", "1"])

    assert scripted["run"]["usage_limits"].max_requests == 1
    assert code in (1, 2)


def test_data_run_needs_a_selection_and_a_built_project(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["data", "run", str(tmp_path)])
    capsys.readouterr()

    (tmp_path / "blueprint.yaml").write_text(
        "format_version: awa/v1\nname: x\nagents:\n  a: Do it.\n", encoding="utf-8"
    )
    code = main(["data", "run", str(tmp_path), "--all"])

    assert code == 1 and "holds no data/files/ folder" in capsys.readouterr().err


def declare_payments_as_reference(project: Path) -> None:
    import yaml

    path = project / "blueprint.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in document["data"]:
        if entry["path"].endswith("payments.csv"):
            entry["role"] = "reference"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_data_run_all_refuses_when_no_file_is_declared_a_lookup_file(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blueprint that names no `role: reference` file would run its lookup tables as inputs
    too: `--all` says which files and the fix."""

    project = asyncio.run(built(tmp_path))

    code = main(["data", "run", str(project), "--all", "--no-input"])

    err = capsys.readouterr().err
    assert code != 0 and "run" not in scripted  # nothing ran
    assert "orders.csv, payments.csv" in err and "role: reference" in err and "--file" in err
    assert not (project / "runs").exists()


def test_data_run_all_runs_the_inputs_once_the_lookup_files_are_declared(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = asyncio.run(built(tmp_path))
    declare_payments_as_reference(project)

    code = main(["data", "run", str(project), "--all", "--no-input", "--json"])

    outcomes = json.loads(capsys.readouterr().out)
    assert [o["file"] for o in outcomes] == ["orders.csv"] and code == 2  # paused at the approval


def test_data_run_all_on_a_single_input_needs_no_declaration(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = asyncio.run(built(tmp_path))
    (project / "data/files/payments.csv").unlink()
    import yaml

    path = project / "blueprint.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["data"] = [e for e in document["data"] if not e["path"].endswith("payments.csv")]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    code = main(["data", "run", str(project), "--all", "--no-input", "--json"])

    assert [o["file"] for o in json.loads(capsys.readouterr().out)] == ["orders.csv"] and code == 2


def test_data_run_all_runs_several_inputs_when_a_lookup_file_is_declared(
    scripted: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import yaml

    project = asyncio.run(built(tmp_path))
    (project / "data/files/lookup.csv").write_text("code;name\n1;a\n", encoding="utf-8")
    path = project / "blueprint.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["data"].append(
        {"name": "lookup", "path": "data/files/lookup.csv", "role": "reference"}
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    code = main(["data", "run", str(project), "--all", "--no-input", "--json"])

    files = [o["file"] for o in json.loads(capsys.readouterr().out)]
    assert files == ["orders.csv", "payments.csv"] and code == 2  # both paused at the approval
