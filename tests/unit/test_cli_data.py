"""`awa data` commands, and every command that takes a blueprint also taking its project folder."""

from __future__ import annotations

import builtins
import getpass
import importlib.machinery
import io
import json
import os
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import awa.blueprints
import awa.server._app as server_app
from _helpers.data_fixtures import ORDERS_CSV, write_text
from awa.cli import COMMANDS, build_parser, main
from awa.core.dotenv import read_env_file
from awa.data import DataProfile
from awa.models import ModelRegistry
from awa.testing import FakeChatModel


def _run(*args: str) -> int:
    return main(["data", "inspect", *args])


def test_inspect_shows_what_awa_found_in_a_real_german_csv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(str(ORDERS_CSV))

    out = capsys.readouterr().out
    assert code == 0
    # cp1252 assumed, decimal comma
    assert out.startswith("1 file, 1 table, 8 rows, 2 warnings, 0 errors")
    assert "orders.csv  csv   8 rows" in out
    assert "encoding cp1252, delimiter ';'" in out
    assert "Betrag_EUR             decimal    decimal ','" in out
    assert "880.75 to 23400.0" in out
    assert str(ORDERS_CSV.parent) not in out  # paths are shown relative


def test_json_is_the_stable_profile_and_out_writes_the_same_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "orders.profile.json"

    code = _run(str(ORDERS_CSV), "--json", "--out", str(target))

    printed = capsys.readouterr().out
    assert code == 0
    jsonschema.validate(json.loads(printed), DataProfile.json_schema())
    assert target.read_text(encoding="utf-8") == printed.rstrip("\n")
    assert DataProfile.from_json(printed).find_column("orders.csv", "Betrag_EUR") is not None


def test_no_samples_and_mask_drop_leave_every_value_out_of_the_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(str(ORDERS_CSV), "--json") == 0
    with_values = capsys.readouterr().out
    assert "B-1001" in with_values and "Kowalski" in with_values

    for option in (["--no-samples"], ["--mask", "drop"]):
        assert _run(str(ORDERS_CSV), "--json", *option) == 0
        without = capsys.readouterr().out
        assert "B-1001" not in without and "Kowalski" not in without
        assert (
            json.loads(without)["files"][0]["tables"][0]["columns"][3]["format"]["decimal"] == ","
        )

    assert _run(str(ORDERS_CSV), "--no-samples") == 0
    assert "e.g." not in capsys.readouterr().out

    saved = tmp_path / "saved.json"
    assert _run(str(ORDERS_CSV), "--no-samples", "--out", str(saved)) == 0
    assert "B-1001" not in saved.read_text(encoding="utf-8")


def test_out_never_overwrites_a_file_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "kept.json"
    target.write_text("mine", encoding="utf-8")

    assert _run(str(ORDERS_CSV), "--out", str(target)) == 1
    assert "--force" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "mine"
    assert _run(str(ORDERS_CSV), "--out", str(target), "--force") == 0
    assert target.read_text(encoding="utf-8") != "mine"


def test_exit_codes_follow_errors_and_strict_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_text(tmp_path, "good.csv", "a;b\n1;2\n")
    note = write_text(tmp_path, "data.bin", "x")
    empty = write_text(tmp_path, "empty.csv", "")

    assert _run(str(tmp_path / "good.csv")) == 0
    assert _run(str(note)) == 0  # unsupported is a warning, not an error
    capsys.readouterr()
    assert _run(str(note), "--strict") == 1
    plain = capsys.readouterr().out
    assert "[warning] " in plain and "DF-W-" not in plain  # codes only with --verbose
    assert _run(str(note), "--strict", "--verbose") == 1
    assert "[warning] DF-W-UNSUPPORTED-KIND" in capsys.readouterr().out
    assert _run(str(empty), "--verbose") == 1
    assert "[error] DF-E-EMPTY" in capsys.readouterr().out
    assert _run(str(tmp_path / "good.csv"), "--strict") == 0


def test_folders_patterns_and_several_paths_and_bad_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_text(tmp_path, "in/a.csv", "x\n1\n")
    write_text(tmp_path, "in/sub/b.csv", "x\n1\n")
    write_text(tmp_path, "c.csv", "x\n1\n")

    assert _run(str(tmp_path / "in"), str(tmp_path / "c.csv")) == 0
    out = capsys.readouterr().out
    assert out.startswith("3 files") and "in/sub/b.csv" in out
    assert _run(str(tmp_path / "in" / "*.csv")) == 0
    assert capsys.readouterr().out.startswith("1 file,")

    assert _run(str(tmp_path / "missing.csv")) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "does not exist" in err and "Traceback" not in err


def test_a_file_over_the_size_limit_is_reported_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_text(tmp_path, "big.csv", "x\n" + "1\n" * 100)

    assert _run(str(path), "--max-file-mb", "0.0001") == 1
    assert "[error] 202 bytes; the limit is 100" in capsys.readouterr().out
    assert _run(str(path), "--max-file-mb", "0.0001", "--verbose") == 1
    assert "DF-E-TOO-LARGE" in capsys.readouterr().out


def test_model_view_prints_the_digest_with_the_privacy_and_budget_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def render_for_model(profile: DataProfile, budget_tokens: int = 6000, *, privacy: Any) -> str:
        seen.update(files=len(profile.files), budget=budget_tokens, mask=privacy.mask)
        return "DIGEST OF THE FILES"

    monkeypatch.setattr("awa.data.render_for_model", render_for_model, raising=False)

    assert _run(str(ORDERS_CSV), "--model-view", "--mask", "placeholders", "--budget", "900") == 0

    assert capsys.readouterr().out == "DIGEST OF THE FILES\n"
    assert seen == {"files": 1, "budget": 900, "mask": "placeholders"}


def test_the_real_model_view_is_the_budgeted_digest_with_the_mask_applied(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(str(ORDERS_CSV), "--model-view") == 0
    plain = capsys.readouterr().out
    assert "orders.csv" in plain and "<<<DATA" in plain and "B-1001" in plain

    assert _run(str(ORDERS_CSV), "--model-view", "--mask", "drop", "--budget", "1500") == 0
    dropped = capsys.readouterr().out
    assert "orders.csv" in dropped and "B-1001" not in dropped and len(dropped) < 6000 + 500


PROJECT = """name: demo
agents:
  clerk: Reads the orders.
data:
  - name: orders
    path: data/files/orders.csv
    role: input
    description: Purchase orders.
    expect:
      columns: [Bestellnr, Betrag_EUR]
      key: [Bestellnr]
      decimal: ","
"""
GOOD_ORDERS = "Bestellnr;Lieferant;Betrag_EUR\r\nB-1;Kowalski;4.250,00\r\nB-2;Müller;5.980,50\r\n"


def _project(tmp_path: Path, orders: str = GOOD_ORDERS) -> Path:
    write_text(tmp_path, "data/files/orders.csv", orders, "cp1252")
    write_text(tmp_path, "blueprint.yaml", PROJECT)
    return tmp_path


def test_check_passes_when_the_files_still_match_the_data_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)

    code = main(["data", "check", str(project)])

    assert code == 0
    assert capsys.readouterr().out.strip() == f"{project}: data matches (0 errors, 0 warnings)"


def test_check_reports_a_renamed_column_and_a_duplicate_key_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(
        tmp_path, "Bestellnr;Lieferant;Betrag\r\nB-1;Kowalski;4.250,00\r\nB-1;Müller;5.980,50\r\n"
    )

    assert main(["data", "check", str(project / "blueprint.yaml")]) == 1

    out = capsys.readouterr().out
    assert "error: data[0].expect.columns[1]: column 'Betrag_EUR' does not exist" in out
    assert "did you mean 'Betrag'?" in out and "key column 'Bestellnr'" in out
    assert out.strip().endswith("blueprint.yaml: 2 errors, 0 warnings")


def test_check_json_lists_the_issues_for_a_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    (project / "data" / "files" / "orders.csv").unlink()

    assert main(["data", "check", str(project), "--json"]) == 1

    result = json.loads(capsys.readouterr().out)
    assert (result["ok"], result["errors"], result["warnings"]) == (False, 1, 0)
    (issue,) = result["issues"]
    assert issue["severity"] == "error" and issue["path"] == "data[0].path"
    assert "matches no file" in issue["message"]


def test_check_without_a_data_section_or_a_blueprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_text(tmp_path, "plain/blueprint.yaml", "name: demo\nagents:\n  clerk: Reads.\n")
    (tmp_path / "empty").mkdir()

    assert main(["data", "check", str(tmp_path / "plain")]) == 0
    assert "nothing to check" in capsys.readouterr().out
    assert main(["data", "check", str(tmp_path / "empty")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "holds no blueprint" in err and "Traceback" not in err


def test_validate_and_check_agree_on_the_data_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, "Bestellnr;Lieferant;Betrag\r\nB-1;Kowalski;4.250,00\r\n")

    assert main(["validate", str(project)]) == 1
    validated = capsys.readouterr().out
    assert main(["data", "check", str(project)]) == 1
    checked = capsys.readouterr().out

    for line in checked.splitlines():
        if line.startswith("error:"):
            assert line in validated


def test_json_and_model_view_cannot_be_combined(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _run(str(ORDERS_CSV), "--json", "--model-view")

    assert "not allowed with" in capsys.readouterr().err


def test_the_command_is_listed_grouped_and_extendable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "data" in COMMANDS
    parser = build_parser(["data"])
    assert "inspect" in parser.format_help()

    added: list[str] = []
    probe = types.ModuleType("awa.cli._data_probe")
    probe.__spec__ = importlib.machinery.ModuleSpec("awa.cli._data_probe", None)
    probe.add_data_subcommands = lambda actions: added.append("probe")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "awa.cli._data_probe", probe)
    monkeypatch.setattr("awa.cli._data._EXTENSIONS", ("_data_probe", "_not_there"))

    build_parser(["data"])

    assert added == ["probe"]


PASSWORD = "Cli-ledger-pw-31337"


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "company-ledger.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE invoices (invoice_id TEXT PRIMARY KEY, amount_eur REAL)")
        db.execute("INSERT INTO invoices VALUES ('INV-2041', 1180.0)")
    return path


def test_add_system_asks_hidden_tests_saves_and_shows_the_structure(
    tmp_path: Path,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": asked.append(prompt) or str(ledger))
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail(f"asked openly: {prompt}"))
    project = tmp_path / "invoice-desk"

    code = main(["data", "add-system", str(project), "--type", "sql", "--name", "Invoice ledger"])

    out = capsys.readouterr().out
    assert (
        code == 0 and asked and "INVOICE_LEDGER_DATABASE_URL" in asked[0] and "hidden" in asked[0]
    )
    assert "Test passed" in out and "table invoices" in out and "invoice_ledger_query" in out
    assert str(ledger) not in out
    assert read_env_file(project / ".env") == {
        "INVOICE_LEDGER_DATABASE_URL": "sqlite:///" + ledger.resolve().as_posix()
    }

    code = main(["data", "inspect", str(project)])

    out = capsys.readouterr().out
    assert code == 0 and "External systems (1):" in out and "live, read-only" in out


@pytest.mark.slow
def test_add_system_without_input_uses_the_environment_and_fails_plainly(
    tmp_path: Path,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "p"
    monkeypatch.setenv("LEDGER_DATABASE_URL", f"postgresql://reader:{PASSWORD}@127.0.0.1:1/x")

    code = main(
        ["data", "add-system", str(project), "--type", "sql", "--name", "Ledger", "--no-input"]
    )

    out = capsys.readouterr().out
    assert code == 1 and "The connection test failed" in out and "What to do:" in out
    assert "Nothing was saved." in out and PASSWORD not in out
    monkeypatch.setenv("LEDGER_DATABASE_URL", str(ledger))

    code = main(
        ["data", "add-system", str(project), "--type", "sql", "--name", "Ledger", "--no-input"]
    )

    assert code == 0 and "LEDGER_DATABASE_URL: already set" in capsys.readouterr().out
    assert not (project / ".env").exists()  # the value came from the environment: not copied


def test_an_unknown_kind_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["data", "add-system", str(tmp_path), "--type", "smtp", "--name", "Mail", "--no-input"]
    )

    assert code != 0 and "not a kind of system" in capsys.readouterr().err


def test_remove_system(
    tmp_path: Path,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LEDGER_DATABASE_URL", str(ledger))
    main(["data", "add-system", str(tmp_path), "--type", "sql", "--name", "Ledger", "--no-input"])

    assert main(["data", "remove-system", str(tmp_path), "ledger"]) == 0
    assert main(["data", "remove-system", str(tmp_path), "ledger"]) == 1
    assert "no declared system" in capsys.readouterr().out


def test_inspect_json_of_a_project_lists_its_systems(
    tmp_path: Path,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LEDGER_DATABASE_URL", str(ledger))
    main(["data", "add-system", str(tmp_path), "--type", "sql", "--name", "Ledger", "--no-input"])
    capsys.readouterr()

    assert main(["data", "inspect", str(tmp_path), "--json"]) == 0

    found = json.loads(capsys.readouterr().out)
    assert (
        found["systems"][0]["key"] == "ledger"
        and found["systems"][0]["tables"][0]["name"] == "invoices"
    )


def test_build_takes_the_systems_declared_in_its_folder(
    tmp_path: Path,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from awa.blueprints._declare import declare_system

    project = tmp_path / "desk"
    declare_system(
        project,
        connector="sql",
        name="Ledger",
        values={"LEDGER_DATABASE_URL": str(ledger)},
        environ={},
    )
    monkeypatch.delenv("LEDGER_DATABASE_URL", raising=False)
    seen: dict[str, Any] = {}

    async def fake_build(process: str, **options: Any) -> Any:
        seen.update(options, url=os.environ.get("LEDGER_DATABASE_URL"))
        raise SystemExit(0)

    monkeypatch.setattr(awa.blueprints, "build_project", fake_build)

    with pytest.raises(SystemExit):
        main(["build", "Answer invoice questions.", "--out", str(project), "--no-input"])

    assert [s.key for s in seen["declared_systems"]] == ["ledger"]
    # exported from DIR/.env for the test runs
    assert seen["url"] == "sqlite:///" + ledger.resolve().as_posix()
    assert "[systems] Ledger (SQLite database): live, read-only" in capsys.readouterr().err


TOOLS = '''
def lookup(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}: shipped"


__all__ = ["lookup"]
'''

BLUEPRINT = """
name: desk
agents:
  - name: clerk
    description: Looks up orders.
    tools: [lookup]
"""

CASES = """
name: orders
cases:
  - id: one
    input: Where is order 1?
    expected: shipped
"""

SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_test_server.py"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project folder: `blueprint.yaml` and a `tools.py` that only the folder's own path finds."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
    (folder / "tools.py").write_text(TOOLS, encoding="utf-8")
    (tmp_path / "cases.yaml").write_text(CASES, encoding="utf-8")
    registry = ModelRegistry()
    registry.register("default", FakeChatModel(["Order 1 shipped."], repeat_last=True))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return folder


@pytest.mark.parametrize(
    "argv",
    [
        ["validate"],
        ["show"],
        ["run"],
        ["chat"],
        ["eval"],
        ["serve"],
        ["mcp", "lock"],
        ["mcp", "list"],
        ["mcp", "serve"],
        ["add", "trigger"],
        ["trigger", "list"],
        ["trigger", "run"],
        ["runs", "migrate"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_every_blueprint_command_resolves_a_project_folder(argv: list[str], project: Path) -> None:
    extra = {"run": ["hi"], "eval": ["cases.yaml"]}.get(argv[0], [])
    if argv == ["trigger", "run"]:
        extra = ["nightly"]
    args = build_parser([argv[0]]).parse_args([*argv, str(project), *extra])
    assert Path(args.blueprint) == project / "blueprint.yaml"


def test_a_folder_without_a_blueprint_error_names_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "empty").mkdir()
    main(["run", str(tmp_path / "empty"), "hi"])
    assert "blueprint.yaml" in capsys.readouterr().err


def test_run_takes_a_project_folder(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(project), "Where is order 1?"]) == 0
    assert "Order 1 shipped." in capsys.readouterr().out


def test_chat_takes_a_project_folder(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("Where is order 1?\n/exit\n"))
    assert main(["chat", str(project)]) == 0
    assert "Order 1 shipped." in capsys.readouterr().out


def test_eval_takes_a_project_folder(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["eval", str(project), "cases.yaml", "--scorer", "contains"])
    assert code == 0, capsys.readouterr()
    assert "Pass rate: 100.0%" in capsys.readouterr().out


def test_serve_takes_a_project_folder(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[Any] = []

    class Uvicorn:
        class Config:
            def __init__(self, app: Any, **options: Any) -> None:
                self.app = app

        class Server:
            def __init__(self, config: Any) -> None:
                self.config = config

            def run(self) -> None:
                served.append(self.config.app)

            async def shutdown(self, sockets: Any = None) -> None:
                return None

    monkeypatch.setattr(server_app, "import_uvicorn", lambda: Uvicorn)
    assert main(["serve", str(project)]) == 0
    assert [app.title for app in served] == ["desk"]
    assert (project / ".awa").is_dir()  # the run state lives in the project folder
    assert not (project.parent / ".awa").exists()


def test_eval_builds_a_blueprint_with_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("langchain_mcp_adapters")
    monkeypatch.chdir(tmp_path)
    registry = ModelRegistry()
    registry.register("default", FakeChatModel(["Customer found."], repeat_last=True))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    blueprint = {
        "name": "desk",
        "mcp_servers": [
            {
                "name": "crm",
                "command": sys.executable,
                "args": [str(SERVER_SCRIPT)],
                "include": ["lookup_customer"],
            }
        ],
        "agents": [{"name": "a", "description": "Finds customers.", "tools": ["mcp:crm"]}],
    }
    (tmp_path / "bp.yaml").write_text(json.dumps(blueprint), encoding="utf-8")
    (tmp_path / "cases.yaml").write_text(
        "name: crm\ncases:\n  - id: one\n    input: Find customer 1\n    expected: found\n"
    )
    code = main(["eval", "bp.yaml", "cases.yaml", "--scorer", "contains"])
    out, err = capsys.readouterr()
    assert code == 0, out + err
    assert "Pass rate: 100.0%" in out
