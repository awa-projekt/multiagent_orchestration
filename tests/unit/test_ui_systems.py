"""The web app's data and systems: the Systems part of Your data, the data map on Describe,
Review and Connect, the Test step's case editor, the way back to a stand-in, and the page's
keyboard and screen-reader labels."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

import yaml
from fastapi.testclient import TestClient

from awa.models import ModelRegistry
from awa.testing import FunctionChatModel
from awa.ui import create_ui_app
from awa.ui._app import page_source
from awa.ui._project import _progress

TOKEN = "systems-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}
PASSWORD = "Pw-for-the-ledger-90210"

PAGE = page_source()
SCRIPT = PAGE.split('<script nonce="{{nonce}}">', 1)[1].split("</script>", 1)[0]
MARKUP = PAGE.split("<script", 1)[0]

STEP = {
    "system": "Invoice ledger",
    "key": "ledger",
    "status": "map",
    "connector": "sql",
    "env_vars": ["LEDGER_DATABASE_URL"],
    "login": [],
    "config": {"use": "sql", "env": {"DATABASE_URL": "LEDGER_DATABASE_URL"}},
    "switch": {},
}

STAND_IN = """\
name: desk
agents:
  - name: clerk
    description: Looks invoices up.
    instructions: Look it up.
    tools: [connector:ledger.query]
connectors:
  ledger:
    use: workspace
    root: data
    database: ledger.db
"""

CASES = """\
name: desk
cases:
  - id: invoice-status
    input: What is the status of INV-1?
    expected: open
    scorers: [contains]
  - id: other
    input: Something else
    expected: never said
    scorers: [contains]
"""


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    parsed = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


def make_project(folder: Path) -> None:
    (folder / "blueprint.yaml").write_text(STAND_IN, encoding="utf-8")
    (folder / "CONNECT.md").write_text(
        f"# Connecting desk\n\n### 1. Invoice ledger\n\n<!-- awa:step {json.dumps(STEP)} -->\n",
        encoding="utf-8",
    )
    (folder / "data" / "files").mkdir(parents=True)
    (folder / "data" / "files" / "INV-1.pdf").write_bytes(b"%PDF-1.4 stub")
    with sqlite3.connect(folder / "data" / "ledger.db") as db:
        db.execute("CREATE TABLE invoices (invoice_id TEXT, status TEXT)")
        db.execute("INSERT INTO invoices VALUES ('INV-1', 'open')")
    (folder / "evals").mkdir()
    (folder / "evals" / "cases.yaml").write_text(CASES, encoding="utf-8")


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "company-ledger.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE invoices (invoice_id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO invoices VALUES ('INV-1', 'open')")
    return path


@pytest.fixture
def environ() -> dict[str, str]:
    return {}


@pytest.fixture
def make_client(tmp_path: Path, environ: dict[str, str]) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(folder: Path | None = None, **options: Any) -> TestClient:
        app = create_ui_app(
            folder or tmp_path / "project", token=TOKEN, port=8765, environ=environ, **options
        )
        client = TestClient(app, base_url=BASE).__enter__()
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------- the page


def test_your_data_has_two_parts_files_and_systems() -> None:
    data = MARKUP.split('<section id="data"', 1)[1].split("</section>", 1)[0]

    assert data.index('id="files-title">Files<') < data.index(
        'id="systems-title">External systems<'
    )
    # one main action
    assert data.count('class="button primary"') + data.count('class="primary"') == 1


@pytest.mark.parametrize(
    "control", ["system-type", "system-name", "system-database", "system-samples"]
)
def test_every_control_of_the_system_form_has_a_label(control: str) -> None:
    assert re.search(rf'<label for="{control}"|<label class="check"><input id="{control}"', MARKUP)


def test_the_system_form_works_with_the_keyboard_and_announces_results() -> None:
    form = MARKUP.split('id="add-system"', 1)[1].split('id="data-next"', 1)[0]

    assert "<summary>" in form  # a details element: Enter or Space opens it
    assert 'id="test-system" type="button"' in form and 'id="save-system" type="button"' in form
    assert 'id="system-result" role="status" aria-live="polite"' in form
    assert "onclick" not in form and 'tabindex="-1"' not in form


def test_script_labels_for_screen_readers() -> None:
    assert '"aria-label": "Remove " + title + " from your data"' in SCRIPT  # the chips on Describe
    assert '"aria-labelledby": title' in SCRIPT  # every system card has its name as its label
    assert '"aria-label": label + ": " + (marks[state.status]' in SCRIPT  # every build stage
    assert 'h("caption", {class: "hint", text: "What each agent reads and writes' in SCRIPT


def test_the_header_no_longer_shows_the_folder_but_set_up_and_use_it_do() -> None:
    header = MARKUP.split("<header>", 1)[1].split("</header>", 1)[0]

    assert "{{folder}}" not in header and "Project folder" not in header
    assert 'Project folder: <code id="setup-folder"></code>' in MARKUP
    assert '$("#folder-chip").textContent = $("#setup-folder").textContent = state.folder' in SCRIPT
    assert 'h("p", {class: "hint"}, "Project: ", h("code", {text: data.folder}))' in SCRIPT


def test_one_word_for_a_stand_in_everywhere() -> None:
    assert "built-in sample data" not in PAGE
    assert "Stand-in" in SCRIPT and "stand-in" in MARKUP
    assert "Analyse" not in PAGE and "organisation" not in PAGE


def test_build_progress_carries_the_facts_of_the_progress_view() -> None:
    from awa.blueprints import BuildProgress

    step = BuildProgress(
        "evaluate", 1, "Case refund: failed", event="eval.case",
        data={"id": "refund", "status": "failed", "failed": ["contains"], "usage": {"requests": 3}},
    )  # fmt: skip

    assert _progress(step) == {
        "message": "Case refund: failed",
        "stage": "evaluate",
        "round": 1,
        "event": "eval.case",
        "id": "refund",
        "status": "failed",
    }


# ---------------------------------------------------------------------- systems


def test_a_system_is_tested_saved_shown_refreshed_and_removed(
    make_client: Callable[..., TestClient], ledger: Path
) -> None:
    client = make_client()
    body = {
        "connector": "sql",
        "name": "Invoice ledger",
        "values": {"INVOICE_LEDGER_DATABASE_URL": str(ledger)},
    }

    form = client.post(
        "/api/systems/form", json={"connector": "sql", "name": "Invoice ledger"}, headers=SAFE
    )
    tested = client.post("/api/systems/test", json=body, headers=SAFE).json()
    saved = client.post("/api/systems", json=body, headers=SAFE).json()

    assert form.json()["fields"][0]["label"] == "Database connection address"
    assert tested["ok"] and tested["system"] is None
    card = saved["system"]
    assert card["state"] == "live" and card["tools"] == [
        "invoice_ledger_schema",
        "invoice_ledger_query",
    ]
    assert card["settings"] == [
        {"name": "INVOICE_LEDGER_DATABASE_URL", "label": "Database connection address", "set": True}
    ]
    state = client.get("/api/systems").json()
    assert [s["name"] for s in state["systems"]] == ["Invoice ledger"]
    assert str(ledger) not in json.dumps(state)
    refreshed = client.post("/api/systems/invoice_ledger/refresh", json={}, headers=SAFE).json()
    assert refreshed["tables"][0]["name"] == "invoices"
    removed = client.post("/api/systems/invoice_ledger/remove", json={}, headers=SAFE).json()
    assert removed["systems"] == []


def test_a_setting_that_is_not_part_of_the_form_is_refused(
    make_client: Callable[..., TestClient], environ: dict[str, str]
) -> None:
    client = make_client()
    body = {"connector": "sql", "name": "Ledger", "values": {"PATH": "C:/evil"}}

    response = client.post("/api/systems", json=body, headers=SAFE)

    assert response.status_code == 400 and "not a setting" in response.json()["error"]["message"]
    assert "PATH" not in environ


def test_an_api_description_must_be_a_secure_link(make_client: Callable[..., TestClient]) -> None:
    body = {
        "connector": "openapi",
        "name": "Portal",
        "values": {"PORTAL_SPEC": "http://api.example.com/openapi.json"},
    }

    response = make_client().post("/api/systems/test", json=body, headers=SAFE)

    assert response.status_code == 400 and "https://" in response.json()["error"]["message"]


@pytest.mark.slow
def test_the_password_never_reaches_the_browser(
    make_client: Callable[..., TestClient], environ: dict[str, str], tmp_path: Path
) -> None:
    client = make_client()
    address = f"postgresql://reader:{PASSWORD}@127.0.0.1:1/ledger"
    body = {"connector": "sql", "name": "Ledger", "values": {"LEDGER_DATABASE_URL": address}}

    answers = [
        client.post(path, json=body, headers=SAFE).text
        for path in ("/api/systems/test", "/api/systems")
    ]
    answers += [
        client.get(path).text
        for path in ("/api/systems", "/api/setup", "/api/datamap", "/api/data")
    ]

    assert all(PASSWORD not in text for text in answers)
    assert not (tmp_path / "project" / ".env").exists()  # the failed test saved nothing


# ---------------------------------------------------------------------- the data map


def test_the_data_map_shows_a_stand_in_until_it_is_connected(
    make_client: Callable[..., TestClient], tmp_path: Path, environ: dict[str, str]
) -> None:
    folder = tmp_path / "desk"
    folder.mkdir()
    make_project(folder)
    client = make_client(folder)

    before = client.get("/api/datamap").json()
    [source] = before["sources"]
    assert (source["name"], source["state"]) == ("Invoice ledger", "stand-in")
    assert "Connect it on the Connect step" in source["sentence"]
    assert before["agents"] == [
        {
            "name": "clerk",
            "uses": [
                {
                    "agent": "clerk",
                    "source": "Invoice ledger",
                    "tool": "ledger_query",
                    "reference": "connector:ledger.query",
                    "access": "reads",
                    "approval": False,
                }
            ],
        }
    ]
    assert client.get("/api/project").json()["data_map"] == before

    saved = client.post(
        "/api/connections/ledger",
        json={"values": {"LEDGER_DATABASE_URL": str(folder / "data" / "ledger.db")}},
        headers=SAFE,
    ).json()
    assert saved["connected"] is True
    [after] = client.get("/api/datamap").json()["sources"]
    assert after["state"] == "live" and after["name"] == "Invoice ledger"


def test_without_a_system_there_is_no_map_yet(make_client: Callable[..., TestClient]) -> None:
    assert make_client().get("/api/datamap").json() == {
        "exists": False,
        "sources": [],
        "agents": [],
    }


# ---------------------------------------------------------------------- stand-in and live per case


def connected(folder: Path, client: TestClient) -> None:
    answer = client.post(
        "/api/connections/ledger",
        json={"values": {"LEDGER_DATABASE_URL": str(folder / "data" / "ledger.db")}},
        headers=SAFE,
    )
    assert answer.json()["connected"] is True


def test_a_connected_system_can_go_back_to_its_stand_in(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    folder = tmp_path / "desk"
    folder.mkdir()
    make_project(folder)
    client = make_client(folder)
    connected(folder, client)
    assert "use: sql" in (folder / "blueprint.yaml").read_text(encoding="utf-8")

    back = client.post("/api/connections/ledger/stand-in", json={}, headers=SAFE)

    assert back.json() == {"stand_in": True}
    text = (folder / "blueprint.yaml").read_text(encoding="utf-8")
    entry = yaml.safe_load(text)["connectors"]["ledger"]
    assert entry == {"use": "workspace", "root": "data", "database": "ledger.db"}
    assert client.get("/api/datamap").json()["sources"][0]["state"] == "stand-in"
    refused = client.post("/api/connections/ledger/stand-in", json={}, headers=SAFE)
    assert refused.status_code == 400  # already on its stand-in


def test_a_case_is_edited_saved_in_the_cases_file_and_may_run_on_the_stand_in(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    folder = tmp_path / "desk"
    folder.mkdir()
    make_project(folder)
    client = make_client(folder)
    connected(folder, client)

    view = client.get("/api/cases").json()
    assert view["files"] == ["INV-1.pdf"] and view["systems"] == [
        {"key": "ledger", "name": "Invoice ledger"}
    ]
    assert view["cases"][0]["systems"] == {"ledger": "live"} and view["cases"][0]["files"] == []

    saved = client.post(
        "/api/cases/invoice-status",
        json={"input": "Check INV-1.pdf: what is its status?", "systems": {"ledger": "stand-in"}},
        headers=SAFE,
    ).json()

    assert saved["case"]["files"] == ["INV-1.pdf"] and saved["case"]["systems"] == {
        "ledger": "stand-in"
    }
    stored = yaml.safe_load((folder / "evals" / "cases.yaml").read_text(encoding="utf-8"))
    case = stored["cases"][0]
    assert case["input"] == "Check INV-1.pdf: what is its status?"
    assert case["metadata"] == {"systems": {"ledger": "stand-in"}}
    assert stored["cases"][1]["input"] == "Something else"  # the other case is untouched
    from awa.ui._project import Project

    project = Project(folder, environ={})
    variant = project.case_editor.blueprint_for(project.dataset()["invoice-status"])
    assert variant is not None and variant.connectors["ledger"]["use"] == "workspace"
    assert project.case_editor.blueprint_for(project.dataset()["other"]) is None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"input": "x", "systems": {"crm": "stand-in"}}, 400),  # not a system of this project
        ({"input": "x", "systems": {"ledger": "maybe"}}, 400),
        ({"input": "", "systems": {}}, 422),
    ],
)
def test_a_wrong_case_change_is_refused(
    make_client: Callable[..., TestClient], tmp_path: Path, body: dict[str, Any], status: int
) -> None:
    folder = tmp_path / "desk"
    folder.mkdir()
    make_project(folder)
    client = make_client(folder)
    connected(folder, client)
    before = (folder / "evals" / "cases.yaml").read_text(encoding="utf-8")

    response = client.post("/api/cases/invoice-status", json=body, headers=SAFE)

    assert response.status_code == status
    assert (folder / "evals" / "cases.yaml").read_text(encoding="utf-8") == before


def test_one_case_runs_alone(make_client: Callable[..., TestClient], tmp_path: Path) -> None:
    folder = tmp_path / "desk"
    folder.mkdir()
    make_project(folder)
    model = FunctionChatModel(lambda call: "INV-1 is open")
    models = ModelRegistry()
    models.register("default", model)
    client = make_client(folder, model_registry=models)

    got = events(client.post("/api/test/case", json={"id": "invoice-status"}, headers=SAFE).text)

    assert got[0] == ("started", {"total": 1})
    assert [data["id"] for kind, data in got if kind == "case"] == ["invoice-status"]
    assert got[-1] == ("done", {"passed": 1, "total": 1})
    assert model.call_count == 1  # one case, one model call: the other case did not run
    unknown = events(client.post("/api/test/case", json={"id": "nope"}, headers=SAFE).text)
    assert unknown[-1][0] == "error"
