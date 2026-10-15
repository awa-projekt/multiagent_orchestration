"""Operation made simple: `tools.py` found by itself, `awa add trigger`, the trigger question of
`awa new process` and `awa build`, grouped help, the serve banner and the Approvals page."""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from _helpers.polling import poll
from awa import Blueprint
from awa.blueprints.components import set_component
from awa.blueprints.triggers import add_trigger, ask_trigger, trigger_entry, trigger_steps
from awa.blueprints.wizard import run_wizard
from awa.cli import COMMANDS, GROUPS, PREVIEW_COMMANDS, main
from awa.cli._serve import _print_banner
from awa.errors import ConfigurationError
from awa.models import ModelRegistry
from awa.server import build_blueprint_app
from awa.server._blueprint_app import served_name
from awa.testing import FakeChatModel, tool_call
from awa.ui import create_ui_app

TOOLS = '''
from langchain_core.tools import tool

CALLS = []


@tool
def pay(order_id: str) -> str:
    """Pay an order."""
    CALLS.append(order_id)
    return f"paid {order_id}"


@tool
def lookup(key: str) -> str:
    """Look a key up."""
    CALLS.append(key)
    return f"found {key}"
'''

ORDERS = """\
# the orders desk
name: orders
agents:
  - name: cashier
    description: Pays orders.
    tools: [pay]
approvals:
  - tool: pay
triggers:
  - name: shop
    webhook: {allow_unsigned: true}
    input: "Handle order {payload.order}"
"""

PLAIN = """\
name: plain
agents:
  - name: reader
    description: Looks keys up.
    tools: [lookup]
"""

TOKEN = "simple-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project folder `proj/` with `tools.py` beside its blueprints; the cwd is elsewhere."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWA_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "tools.py").write_text(TOOLS, encoding="utf-8")
    (folder / "orders.yaml").write_text(ORDERS, encoding="utf-8")
    (folder / "plain.yaml").write_text(PLAIN, encoding="utf-8")
    yield folder
    sys.modules.pop("tools", None)


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


def calls() -> list[str]:
    return list(sys.modules["tools"].CALLS)


# ---------------------------------------------------------------------- tools.py by itself


def test_commands_find_the_tools_file_beside_the_blueprint(
    project: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel([tool_call("lookup", {"key": "a"}), "Found a."]))

    assert main(["validate", "proj/plain.yaml"]) == 0
    assert "not checked" not in capsys.readouterr().out
    assert main(["run", "proj/plain.yaml", "find a"]) == 0
    assert "Found a." in capsys.readouterr().out
    assert calls() == ["a"]


def test_an_explicit_tools_option_still_works_and_adds_more(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "more.py").write_text(
        TOOLS.replace("CALLS = []", "")
        .replace("def pay", "def extra")
        .replace("def lookup", "def l"),
        encoding="utf-8",
    )
    (project / "plain.yaml").write_text(PLAIN.replace("[lookup]", "[lookup, extra]"))

    assert main(["validate", "proj/plain.yaml"]) == 1  # tools.py alone does not know `extra`
    assert "unknown tool 'extra'" in capsys.readouterr().out
    assert main(["validate", "proj/plain.yaml", "--tools", "proj/more.py"]) == 0
    assert main(["validate", "proj/plain.yaml", "--tools", "proj/tools.py,proj/more.py"]) == 0


def test_the_server_loads_the_tools_file_beside_the_blueprint(
    project: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel(["Order 7 handled."]))

    with TestClient(build_blueprint_app(project / "orders.yaml")) as client:
        queued = client.post("/v1/automations/orders/webhooks/shop", json={"order": 7})
        run = poll(
            lambda: client.get(f"/v1/process-runs/{queued.json()['id']}").json(),
            lambda r: r["status"] in ("succeeded", "waiting", "failed"),
            timeout=15,
            what="the run",
        )

    assert run["status"] == "succeeded"


# ---------------------------------------------------------------------- awa add trigger


def test_add_trigger_writes_every_kind_and_prints_how_to_run_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = str(project / "plain.yaml")
    kinds = [
        ["--kind", "webhook", "--name", "hook"],
        ["--kind", "schedule", "--name", "morning", "--cron", "0 7 * * MON-FRI"],
        ["--kind", "file-drop", "--name", "scans", "--directory", "scans", "--pattern", "*.pdf"],
        ["--kind", "mail", "--name", "inbox", "--host", "imap.example.com"],
        ["--kind", "queue", "--name", "jobs"],
    ]
    for options in kinds:
        assert main(["add", "trigger", file, *options]) == 0
    out = capsys.readouterr().out

    blueprint = Blueprint.from_yaml_file(project / "plain.yaml")
    section = blueprint.automation_section()
    assert section is not None
    assert [(t.name, t.kind) for t in section.triggers] == [
        ("hook", "webhook"),
        ("morning", "schedule"),
        ("scans", "file_drop"),
        ("inbox", "mailbox"),
        ("jobs", "queue"),
    ]
    assert section.triggers[0].webhook is not None
    assert section.triggers[0].webhook.secret_env == "HOOK_SECRET"
    assert "/v1/automations/plain/webhooks/hook" in out
    assert "awa serve" in out
    assert "awa trigger run" in out and "--dry-run" in out
    assert not [i for i in blueprint.check() if i.severity == "error"]


def test_add_trigger_keeps_comments_on_the_first_append_and_refuses_bad_input(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = project / "plain.yaml"
    file.write_text("# keep me\n" + PLAIN, encoding="utf-8")

    assert main(["add", "trigger", str(file), "--kind", "schedule", "--every", "15m"]) == 0
    assert file.read_text(encoding="utf-8").startswith("# keep me\n")
    before = file.read_text(encoding="utf-8")
    assert main(["add", "trigger", str(file), "--kind", "schedule", "--every", "1h"]) == 1
    assert "already has a trigger named 'schedule'" in capsys.readouterr().err
    assert main(["add", "trigger", str(file), "--kind", "mail", "--name", "m"]) == 1
    assert (
        main(["add", "trigger", str(file), "--kind", "schedule", "--name", "x", "--cron", "nope"])
        == 1
    )
    assert main(["add", "trigger", str(file), "--kind", "queue", "--dry-run"]) == 0
    assert file.read_text(encoding="utf-8") == before


def test_add_trigger_asks_in_a_terminal_and_never_without_one(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = str(project / "plain.yaml")
    assert main(["add", "trigger", file]) == 1  # no terminal, no --kind
    assert "--kind" in capsys.readouterr().err

    answers = iter(["file-drop", "", "scans", "*.pdf", "Book {payload.name}"])
    entry = ask_trigger(lambda prompt: next(answers), io.StringIO())

    assert entry == {
        "name": "file-drop",
        "file_drop": {"directory": "scans", "pattern": "*.pdf"},
        "input": "Book {payload.name}",
    }
    assert ask_trigger(lambda prompt: "", io.StringIO()) is None


def test_the_try_command_reads_the_sample_event_from_a_file_and_runs_as_printed(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows PowerShell 5.1 drops the inner quotes of `'{"x": 1}'`: the printed command takes
    the sample event from a file (`"@NAME.json"`), which every shell passes unchanged."""

    import shlex

    add_trigger(
        project / "plain.yaml", trigger_entry("file-drop", "scan", input="Book {payload.name}")
    )
    steps = trigger_steps(Blueprint.from_yaml_file(project / "plain.yaml"), "plain.yaml")
    try_it = next(s for s in steps if s.title.startswith("Try scan"))

    assert try_it.command == 'awa trigger run plain.yaml scan --payload "@scan.json" --dry-run'
    assert "'{" not in try_it.command
    assert 'save a sample event as scan.json in this folder, for example {"name": "..."}' in (
        try_it.note
    )
    # the command works as printed once the sample is saved
    monkeypatch.chdir(project)
    (project / "scan.json").write_text('{"name": "INV-1.pdf"}', encoding="utf-8")
    capsys.readouterr()
    assert main(shlex.split(try_it.command)[1:]) == 0
    assert "Book INV-1.pdf" in capsys.readouterr().out


def test_the_wizard_gives_a_second_trigger_of_one_kind_a_free_name(project: Path) -> None:
    from awa.blueprints.wizard import _ask_triggers

    kinds = iter(["schedule", "schedule", ""])

    def ask(prompt: str) -> str:
        return next(kinds) if prompt.startswith("What should start it?") else ""

    _ask_triggers(ask, io.StringIO(), project / "plain.yaml")

    names = [t.name for t in Blueprint.from_yaml_file(project / "plain.yaml").triggers]
    assert len(names) == 2 and len(set(names)) == 2


def test_add_trigger_takes_the_blueprint_keys_as_kinds_too(project: Path) -> None:
    assert trigger_entry("file_drop", "scan")["file_drop"]["directory"] == "inbox"
    assert "mailbox" in trigger_entry("mailbox", "mail", host="imap.example.com")
    answers = iter(["mailbox", "inbox-mail", "imap.example.com", "INBOX", ""])
    asked = ask_trigger(lambda prompt: next(answers), io.StringIO())
    assert asked is not None and asked["mailbox"]["host"] == "imap.example.com"
    assert main(["add", "trigger", str(project / "plain.yaml"), "--kind", "file_drop"]) == 0


def test_a_webhook_trigger_shows_the_url_header_and_signature(project: Path) -> None:
    entry = trigger_entry("webhook", "invoices", secret_env="INVOICE_SECRET", scheme="token")
    add_trigger(project / "plain.yaml", entry)

    steps = trigger_steps(Blueprint.from_yaml_file(project / "plain.yaml"), "plain.yaml")
    hook = next(s for s in steps if "send a POST" in s.title)

    assert hook.command == "http://127.0.0.1:8000/v1/automations/plain/webhooks/invoices"
    assert "X-Awa-Token" in hook.note and "INVOICE_SECRET" in hook.note
    assert steps[0].command == "awa serve plain.yaml"
    with pytest.raises(ConfigurationError, match="Unknown trigger kind"):
        trigger_entry("pigeon")


def test_the_process_wizard_asks_for_a_trigger_only_when_asked(project: Path) -> None:
    def args(output: Path) -> argparse.Namespace:
        return argparse.Namespace(
            name="Desk",
            agent=["clerk=Answers."],
            structure="team",
            lead=None,
            model=None,
            output=str(output),
            auto=True,
            force=False,
        )

    answers = iter(["", "", "schedule", "", "0 7 * * MON-FRI", "", ""])  # lead, model, trigger
    out = io.StringIO()
    assert run_wizard(args(project / "a.yaml"), ask=lambda p: next(answers), out=out) == 0
    section = Blueprint.from_yaml_file(project / "a.yaml").automation_section()
    assert section is not None and section.triggers[0].schedule is not None
    assert "awa worker" in out.getvalue()

    out = io.StringIO()
    assert run_wizard(args(project / "b.yaml"), ask=None, out=out) == 0  # --no-input
    assert Blueprint.from_yaml_file(project / "b.yaml").automation_section() is None
    assert "awa add trigger" in out.getvalue()


# ---------------------------------------------------------------------- help and banner


def test_help_groups_the_commands_and_names_the_path(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out

    assert [line for line in text.splitlines() if line.endswith(":")][: len(GROUPS)] == [
        f"{title}:" for title, names in GROUPS.items() if set(names) & set(COMMANDS)
    ]
    assert all(f"\n  {name} " in text for name in COMMANDS)
    assert "awa add trigger" in text and "awa build" in text and "awa ui" in text
    # the path names the files and commands a project really has (evals/, a worker)
    assert "awa eval blueprint.yaml evals/cases.yaml" in text
    assert "awa worker blueprint.yaml" in text and "awa serve blueprint.yaml" in text
    # a group title never calls a stable command a preview; `init` is a way to start
    for title, names in GROUPS.items():
        assert "preview" not in title.lower() or set(names) <= PREVIEW_COMMANDS, title
    assert "init" in GROUPS["Start"]


def test_serve_prints_the_names_it_serves(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_trigger(project / "plain.yaml", trigger_entry("webhook", "hook"))
    blueprint = Blueprint.from_yaml_file(project / "plain.yaml")

    _print_banner(str(project / "plain.yaml"), "127.0.0.1", 8123)
    err = capsys.readouterr().err

    assert served_name(blueprint) == "reader"
    # the address it really listens on: `localhost` may resolve to another address (::1)
    assert "http://127.0.0.1:8123/v1/systems/reader/runs" in err and "localhost" not in err
    _print_banner(str(project / "plain.yaml"), "::1", 8123)
    assert "http://[::1]:8123/v1/systems/reader/runs" in capsys.readouterr().err
    assert "/v1/automations/plain/webhooks/hook" in err
    assert "'reader' in /v1/systems, the automation 'plain'" in err


# ---------------------------------------------------------------------- the web app


def ui_client(folder: Path) -> TestClient:
    app = create_ui_app(folder, token=TOKEN, port=8765, environ={})
    client = TestClient(app, base_url=BASE)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


def test_the_use_it_page_adds_a_trigger_and_shows_what_to_run(project: Path) -> None:
    client = ui_client(project)
    (project / "blueprint.yaml").write_text(PLAIN, encoding="utf-8")

    refused = client.post("/api/trigger", json={"kind": "webhook"})
    added = client.post(
        "/api/trigger",
        json={"kind": "schedule", "name": "morning", "settings": {"cron": "0 7 * * *"}},
        headers=SAFE,
    )
    bad = client.post("/api/trigger", json={"kind": "mail", "settings": {}}, headers=SAFE)
    unknown = client.post(
        "/api/trigger", json={"kind": "queue", "settings": {"x": "1"}}, headers=SAFE
    )
    deploy = client.get("/api/deploy").json()

    assert refused.status_code == 403
    assert added.status_code == 200 and added.json()["name"] == "morning"
    assert bad.status_code == 400 and "IMAP" in bad.json()["error"]["message"]
    assert unknown.status_code == 400
    assert deploy["trigger_kinds"] == ["webhook", "schedule", "file-drop", "mail", "queue"]
    assert any(t["command"].startswith("awa worker") for t in deploy["triggers"])
    assert all("--tools" not in c["command"] for c in deploy["commands"])
    assert "Approvals" in client.get("/").text


def test_the_approvals_page_decides_a_paused_automated_run(
    project: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"}), "Paid 9."]))
    store = project / ".awa" / "awa.db"
    ui = ui_client(project)
    assert ui.get("/api/approvals").json() == {"approvals": []}  # nothing yet, no store file
    assert not store.exists()

    with TestClient(build_blueprint_app(project / "orders.yaml", store_path=store)) as served:
        run_id = served.post("/v1/automations/orders/webhooks/shop", json={"order": 9}).json()["id"]
        poll(
            lambda: ui.get("/api/approvals").json()["approvals"],
            bool,
            timeout=15,
            what="the paused run in the inbox",
        )
        [waiting] = ui.get("/api/approvals").json()["approvals"]
        ask = waiting["approvals"][0]
        assert waiting["id"] == run_id and ask["tool"] == "pay" and '"order_id"' in ask["args"]
        assert calls() == []

        forbidden = ui.post(
            "/api/approvals", json={"run": run_id}, headers={"Origin": "http://attacker.example"}
        )
        edited = ui.post(
            "/api/approvals",
            json={
                "run": run_id,
                "interrupt_id": ask["interrupt_id"],
                "approve": True,
                "note": "checked",
                "args": '{"order_id": "10"}',
            },
            headers=SAFE,
        )
        done = poll(
            lambda: served.get(f"/v1/process-runs/{run_id}").json(),
            lambda r: r["status"] == "succeeded",
            timeout=15,
            what="the resumed run",
        )

    assert forbidden.status_code == 403
    assert edited.json() == {"run": run_id, "status": "queued", "open": 0}
    assert done["output"] == "Paid 9." and calls() == ["10"]  # the edited arguments ran
    assert ui.get("/api/approvals").json() == {"approvals": []}


def test_the_approvals_page_rejects_with_a_note_and_refuses_bad_answers(
    project: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"}), "Not paid."]))
    ui = ui_client(project)

    with TestClient(
        build_blueprint_app(project / "orders.yaml", store_path=project / ".awa" / "awa.db")
    ) as served:
        run_id = served.post("/v1/automations/orders/webhooks/shop", json={"order": 9}).json()["id"]
        [waiting] = poll(
            lambda: ui.get("/api/approvals").json()["approvals"], bool, timeout=15, what="inbox"
        )
        interrupt = waiting["approvals"][0]["interrupt_id"]
        body: dict[str, Any] = {"run": run_id, "interrupt_id": interrupt, "approve": True}

        garbled = ui.post("/api/approvals", json={**body, "args": "{nope"}, headers=SAFE)
        scalar = ui.post("/api/approvals", json={**body, "args": "[1]"}, headers=SAFE)
        no_such = ui.post("/api/approvals", json={**body, "run": "proc_missing"}, headers=SAFE)
        rejected = ui.post(
            "/api/approvals",
            json={**body, "approve": False, "note": "wrong supplier"},
            headers=SAFE,
        )
        done = poll(
            lambda: served.get(f"/v1/process-runs/{run_id}").json(),
            lambda r: r["status"] == "succeeded",
            timeout=15,
            what="the resumed run",
        )

    assert garbled.status_code == scalar.status_code == no_such.status_code == 400
    assert rejected.json()["status"] == "queued"
    assert done["output"] == "Not paid." and calls() == []


def test_decisions_use_the_format_of_awa_runs_decide() -> None:
    from awa.automation._decision import approval_decision

    assert approval_decision(True) == {"action": "approve"}
    assert approval_decision(True, "ok", {"a": 1}) == {
        "action": "edit",
        "args": {"a": 1},
        "note": "ok",
    }
    assert approval_decision(False) == {"action": "reject", "note": "Rejected by the operator."}
    with pytest.raises(ConfigurationError):
        approval_decision(False, None, {"a": 1})


def test_build_offers_the_trigger_question(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli._blueprint import _offer_triggers

    answers = iter(["schedule", "", "0 7 * * MON-FRI", "", ""])  # then Enter: nothing more
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    _offer_triggers(project / "plain.yaml")
    out = capsys.readouterr().out

    section = Blueprint.from_yaml_file(project / "plain.yaml").automation_section()
    assert section is not None and section.triggers[0].schedule is not None
    assert "awa worker" in out and "plain.yaml" in out


def test_blueprint_edits_keep_comments_and_the_list_indentation(tmp_path: Path) -> None:
    file = tmp_path / "blueprint.yaml"
    file.write_text(
        "# the desk\nname: desk\nagents:\n  - name: clerk  # answers mail\n"
        "    description: Answers.\n",
        encoding="utf-8",
    )

    add_trigger(file, trigger_entry("queue", "orders"))
    add_trigger(file, trigger_entry("queue", "returns"))
    set_component(file, "parser.pdf", "off")
    text = file.read_text(encoding="utf-8")

    assert "# the desk" in text and "# answers mail" in text
    assert "  - name: orders" in text and "  - name: returns" in text
    blueprint = Blueprint.from_yaml_file(file)
    assert [t["name"] for t in blueprint.model_dump()["triggers"]] == ["orders", "returns"]
    assert blueprint.components == {"parser": {"pdf": False}}
