"""The pages of `awa ui` end to end with offline models, also on the real build loop."""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from awa import blueprints
from awa.blueprints import AcceptanceDesign, BlueprintDesign
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call
from awa.ui import _app as ui_app
from awa.ui import create_ui_app

TOKEN = "flow-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}

BLUEPRINT = """\
name: bookings
description: Books appointments after a human approves.
agents:
  - name: clerk
    description: Books appointments.
    instructions: Book what is asked.
    tools: [book]
approvals:
  - tool: book
"""

TOOLS = '''\
from langchain_core.tools import tool


@tool
def book(slot: str) -> str:
    """Book an appointment slot."""
    return f"booked {slot}"
'''

CASES = """\
name: bookings
cases:
  - id: greets
    input: Hello
    expected: Hello back
    scorers: [contains]
  - id: fails
    input: Something else
    expected: never said
    scorers: [contains]
"""

DESIGN: dict[str, Any] = {
    "name": "greeter",
    "description": "Greets visitors.",
    "agents": [{"name": "host", "description": "Greets.", "instructions": "Say hello."}],
    "entrypoint": "agent",
}


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a Server-Sent Events body into (event, data) pairs."""

    parsed = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


def registry(model: Any) -> ModelRegistry:
    models = ModelRegistry()
    models.register("default", model)
    return models


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(**options: Any) -> TestClient:
        app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ={}, **options)
        client = TestClient(app, base_url=BASE).__enter__()
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def project(folder: Path, *, cases: bool = True, connect: str = "") -> None:
    (folder / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
    (folder / "tools.py").write_text(TOOLS, encoding="utf-8")
    if cases:
        (folder / "evals").mkdir()
        (folder / "evals" / "cases.yaml").write_text(CASES, encoding="utf-8")
    if connect:
        (folder / "CONNECT.md").write_text(connect, encoding="utf-8")


# ---------------------------------------------------------------------- describe


def test_describe_builds_the_project_with_live_progress(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, Any] = {}

    class Report:
        passed = True
        pass_rate = 1.0

        def summary(self) -> str:
            return "Ready: 2 of 2 eval cases pass."

        def to_dict(self) -> dict[str, Any]:
            return {"name": "booking", "cases": [], "data_map": None}

    async def build(process: str, *, out_dir: Path, on_progress: Any, **options: Any) -> Report:
        calls.update(process=process, out_dir=out_dir, force=options["force"])
        on_progress(type("Progress", (), {"message": "Round 1: 2 of 2 pass", "round": 1})())
        (out_dir / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
        return Report()

    monkeypatch.setattr(blueprints, "build_project", build)
    client = make_client()

    response = client.post("/api/build", json={"process": "Book appointments."}, headers=SAFE)

    assert calls == {"process": "Book appointments.", "out_dir": tmp_path.resolve(), "force": True}
    got = events(response.text)
    assert ("progress", {"message": "Round 1: 2 of 2 pass", "round": 1}) in got
    assert got[-1] == (
        "done",
        {
            "summary": "Ready: 2 of 2 eval cases pass.",
            "passed": True,
            "pass_rate": 1.0,
            "name": "booking",
            "cases": [],
            "data_map": None,
        },
    )
    replay = client.get("/api/jobs/build")
    assert events(replay.text) == got
    assert client.get("/api/state").json() == {
        "folder": str(tmp_path.resolve()),
        "has_system": True,
        "has_cases": False,
        "jobs": {"build": False},
        "security_off": [],
        "process": "",
    }


def test_describe_uses_design_project_when_testing_is_off(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    written: list[Path] = []

    class Design:
        blueprint = type("B", (), {"name": "greeter"})()

        def write(self, directory: Path) -> Path:
            written.append(directory)
            return directory

    async def design_project(process: str, **options: Any) -> Design:
        return Design()

    monkeypatch.setattr(blueprints, "design_project", design_project, raising=False)
    client = make_client()

    response = client.post(
        "/api/build", json={"process": "Greet.", "thorough": False}, headers=SAFE
    )

    assert events(response.text)[-1][0] == "done"
    assert written == [tmp_path.resolve()]


def test_an_empty_description_is_explained(make_client: Callable[..., TestClient]) -> None:
    response = make_client().post("/api/build", json={"process": "  "}, headers=SAFE)

    kind, data = events(response.text)[-1]
    assert kind == "error"
    assert "Describe the process" in data["message"]
    assert data["fix"]


# ---------------------------------------------------------------------- review


def test_review_shows_agents_diagram_cases_and_connections(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path, connect="# Connect\n1. Get an API key\n")
    text = BLUEPRINT.replace("tools: [book]", "tools: [book, 'connector:jira.search']")
    (tmp_path / "blueprint.yaml").write_text(text, encoding="utf-8")

    data = make_client().get("/api/project").json()

    assert data["name"] == "bookings"
    assert data["agents"] == [
        {
            "name": "clerk",
            "description": "Books appointments.",
            "tools": ["book", "connector:jira.search"],
            "approvals": ["book"],
        }
    ]
    assert data["diagram"].startswith("<svg")
    assert "approval: book" in data["diagram"]
    assert [case["id"] for case in data["cases"]] == ["greets", "fails"]
    assert [(c["name"], c["configured"]) for c in data["connectors"]] == [("jira", False)]
    assert data["connect"].startswith("# Connect")
    assert data["yaml"] == text


def test_review_without_a_system_says_so(make_client: Callable[..., TestClient]) -> None:
    assert make_client().get("/api/project").json()["exists"] is False


def test_edited_yaml_is_checked_and_saved_only_without_errors(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path)
    client = make_client()
    broken = BLUEPRINT.replace("tools: [book]", "tools: [missing_tool]")

    checked = client.post("/api/project", json={"yaml": broken, "save": True}, headers=SAFE)
    unparsable = client.post("/api/project", json={"yaml": "name: [", "save": False}, headers=SAFE)
    renamed = BLUEPRINT.replace("name: bookings", "name: calendar")
    saved = client.post("/api/project", json={"yaml": renamed, "save": True}, headers=SAFE)

    assert checked.json()["saved"] is False
    assert any(issue["severity"] == "error" for issue in checked.json()["issues"])
    assert unparsable.json()["issues"][0]["severity"] == "error"
    assert saved.json() == {"issues": [], "saved": True}
    assert "name: calendar" in (tmp_path / "blueprint.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- test


def test_tests_report_pass_and_fail_per_case(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path)
    model = FunctionChatModel(lambda call: "Hello back to you")
    client = make_client(model_registry=registry(model))

    got = events(client.post("/api/test", json={}, headers=SAFE).text)

    assert got[0] == ("started", {"total": 2})
    cases = {data["id"]: data for kind, data in got if kind == "case"}
    assert cases["greets"]["status"] == "passed"
    assert cases["fails"]["status"] == "failed"
    assert cases["fails"]["failed"][0]["check"] == "contains"
    assert got[-1] == ("done", {"passed": 1, "total": 2})


def test_test_results_never_show_a_saved_credential(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path)
    secret = "test-key-not-real"
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    model = FunctionChatModel(lambda call: f"my key is {secret}")
    client = make_client(model_registry=registry(model))

    text = client.post("/api/test", json={}, headers=SAFE).text

    assert secret not in text and "my key is" in text


def test_tests_without_cases_explain_how_to_get_them(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path, cases=False)

    kind, data = events(make_client().post("/api/test", json={}, headers=SAFE).text)[-1]

    assert kind == "error"
    assert "no eval cases" in data["message"]
    assert "Describe" in data["fix"]


# ---------------------------------------------------------------------- run


def test_a_conversation_pauses_for_approval_and_continues(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path)
    model = FakeChatModel([tool_call("book", {"slot": "Mon 9:00"}), "Booked Monday 9:00."])
    client = make_client(model_registry=registry(model))

    paused = events(client.post("/api/run", json={"message": "Book Monday."}, headers=SAFE).text)

    step = next(data for kind, data in paused if kind == "activity")
    assert step["text"] == "Book an appointment slot (Slot: Mon 9:00)"
    kind, approval = paused[-1]
    assert kind == "approval"
    card = approval["pending"][0]
    assert card["tool"] == "book"
    assert card["why"] == "because this step always needs a person's OK"
    assert card["fields"][0]["title"] == "Slot" and card["fields"][0]["value"] == "Mon 9:00"
    assert "Mon 9:00" in card["args"]

    decision = {"id": approval["pending"][0]["id"], "approve": True}
    resumed = client.post(
        "/api/run/resume",
        json={"thread_id": approval["thread_id"], "decisions": [decision]},
        headers=SAFE,
    )

    assert events(resumed.text)[-1] == (
        "done",
        {"thread_id": approval["thread_id"], "agent": "clerk", "text": "Booked Monday 9:00."},
    )


def test_a_run_error_comes_with_the_fix(make_client: Callable[..., TestClient]) -> None:
    kind, data = events(make_client().post("/api/run", json={"message": "Hi"}, headers=SAFE).text)[
        -1
    ]

    assert kind == "error"
    assert "no system" in data["message"]
    assert "Describe" in data["fix"]


# ---------------------------------------------------------------------- deploy


def test_deploy_lists_commands_and_exports_the_folder(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path)
    (tmp_path / ".env").write_text("KEY=value-never-exported\n")
    client = make_client()

    data = client.get("/api/deploy").json()
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export").content))

    commands = [item["command"] for item in data["commands"]]
    assert any(" serve " in command and "blueprint.yaml" in command for command in commands)
    assert any(" mcp serve " in command for command in commands)
    assert any(" eval " in command and "cases.yaml" in command for command in commands)
    assert not any("--tools" in command for command in commands)  # tools.py is found by itself
    assert sorted(archive.namelist()) == ["blueprint.yaml", "evals/cases.yaml", "tools.py"]


def test_a_folder_name_beyond_latin_1_is_exported_under_its_own_name(tmp_path: Path) -> None:
    folder = tmp_path / "Señor 案件"
    folder.mkdir()
    project(folder)
    app = create_ui_app(folder, token=TOKEN, port=8765, environ={})
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        response = client.get("/api/export")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert 'filename="Se_or __.zip"' in disposition
    assert "filename*=utf-8''Se%C3%B1or%20%E6%A1%88%E4%BB%B6.zip" in disposition


# ---------------------------------------------------------------------- connections


GUIDE = """\
# Connecting bookings to its external systems

## 1. Calendar: needs its settings

<!-- awa:step {"system": "Calendar", "status": "configure", "connector": "calendar", \
"env_vars": ["CAL_URL", "CAL_TOKEN"], "switch": {"book": ["connector:calendar.book"]}} -->
1. Get `CAL_URL` and `CAL_TOKEN`.

## 2. Archive: works on sample data

<!-- awa:step {"system": "Archive", "status": "map", "connector": null, "env_vars": [], \
"switch": {"archive": []}} -->
Link it later.
"""


def test_review_lists_connection_steps_with_what_is_still_missing(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path, connect=GUIDE)
    (tmp_path / ".env").write_text("CAL_URL=https://calendar.example\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "slots.csv").write_text("slot\nMon 9:00\n")

    data = make_client().get("/api/project").json()

    assert data["steps"] == [
        {
            "system": "Calendar",
            "status": "configure",
            "connector": "calendar",
            "missing": ["CAL_TOKEN"],
            "tools": ["book"],
        },
        {
            "system": "Archive",
            "status": "map",
            "connector": None,
            "missing": [],
            "tools": ["archive"],
        },
    ]
    assert "awa:step" not in data["connect"]
    assert data["data"]["exists"] is True
    assert data["data"]["files"] == ["slots.csv"]
    assert data["data"]["count"] == 1


def test_setup_asks_for_the_settings_the_project_needs(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    (tmp_path / ".env.example").write_text(
        "# The model\n# AWA_DEFAULT_MODEL=\n\n# Calendar\nCAL_URL=\nCAL_TOKEN=\n"
    )
    (tmp_path / ".env").write_text("CAL_URL=https://calendar.example\n")

    data = make_client().get("/api/setup").json()

    assert data["project"] == [
        {"name": "CAL_URL", "set": True},
        {"name": "CAL_TOKEN", "set": False},
    ]
    assert "calendar.example" not in json.dumps(data)


def test_use_it_names_the_systems_that_are_not_connected_yet(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path, connect=GUIDE)

    deploy = make_client().get("/api/deploy").json()

    assert deploy["not_connected"] == ["Calendar", "Archive"]
    assert deploy["data"]["exists"] is False


def test_the_data_map_labels_exactly_the_states_of_a_source() -> None:
    import re
    from typing import get_args

    from awa.blueprints._data_map import State

    page = ui_app.page_source()
    block = page.split("const SOURCE_STATE = {", 1)[1].split("};", 1)[0]
    labelled = re.findall(r'(?:^|,)\s*"?([\w-]+)"?: "', block)

    assert sorted(labelled) == sorted(get_args(State))
    assert "Needs code" not in page


def test_the_export_leaves_out_secrets_run_history_and_databases(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    project(tmp_path)
    (tmp_path / ".awa").mkdir()
    for name in (
        ".env",
        ".env.local",
        ".env.production",
        ".awa/awa.db",
        ".awa/checkpoints.db",
        "history.sqlite3",
        "history.db-wal",
        "server.pem",
        "deploy.key",
    ):
        (tmp_path / name).write_text("secret")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n")
    (tmp_path / "evals" / "notes.md").write_text("kept")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    with contextlib.suppress(OSError):  # no right to create links here
        (tmp_path / "link.txt").symlink_to(outside)

    archive = zipfile.ZipFile(io.BytesIO(make_client().get("/api/export").content))

    assert sorted(archive.namelist()) == [
        ".env.example",
        "blueprint.yaml",
        "evals/cases.yaml",
        "evals/notes.md",
        "tools.py",
    ]


ACCEPTANCE: dict[str, Any] = {
    "criteria": ["Order questions are answered with the status."],
    "systems": [
        {
            "name": "Order database",
            "key": "orders",
            "description": "The shop's orders.",
            "kind": "database",
            "tables": [
                {
                    "name": "orders",
                    "description": "One order.",
                    "columns": [{"name": "order_id"}, {"name": "status"}],
                    "rows": ["1002, shipped"],
                }
            ],
        }
    ],
    "cases": [
        {
            "id": "order-status",
            "input": "Where is order 1002?",
            "expectations": [{"kind": "contains", "value": "shipped"}],
        }
    ],
}

BLUEPRINT_INTEGRATION: dict[str, Any] = {
    "name": "order_desk",
    "description": "Answers order questions.",
    "entrypoint": "agent",
    "agents": [
        {
            "name": "desk",
            "description": "Answers customers.",
            "instructions": "Look orders up in the orders table.",
            "tools": ["connector:orders.query"],
        }
    ],
}


def respond(call: ModelCall) -> Any:
    """Test designer, architect and the designed desk agent in one offline model."""

    first = call.messages[0]
    system = str(first.content) if isinstance(first, SystemMessage) else ""
    if system.startswith("You write the acceptance tests"):
        return structured_output(AcceptanceDesign, ACCEPTANCE)
    if system.startswith("You design multi-agent systems"):
        return structured_output(BlueprintDesign, BLUEPRINT_INTEGRATION)
    results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
    if not results:
        sql = "SELECT status FROM orders WHERE order_id = ?"
        return tool_call("orders_query", {"query": sql, "parameters": ["1002"]})
    asked = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    return f"About '{asked}': {results[-1]}"


def events_integration(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = [dict(line.split(": ", 1) for line in f.splitlines()) for f in body.split("\n\n") if f]
    return [(frame["event"], json.loads(frame["data"])) for frame in frames if "event" in frame]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    model = FunctionChatModel(respond)
    models = ModelRegistry()
    models.register("default", model)
    app = create_ui_app(
        tmp_path, token=TOKEN, port=8765, environ={}, model=model, model_registry=models
    )
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client


@pytest.mark.slow
def test_a_described_process_becomes_a_tested_runnable_project(
    client: TestClient, tmp_path: Path
) -> None:
    built = events_integration(
        client.post(
            "/api/build", json={"process": "Customers ask about orders."}, headers=SAFE
        ).text
    )

    assert any(kind == "progress" and "Round 1" in data["message"] for kind, data in built)
    kind, outcome = built[-1]
    assert kind == "done", outcome
    assert outcome["passed"] is True
    assert "order_desk" in outcome["summary"]
    assert (tmp_path / "blueprint.yaml").is_file()

    review = client.get("/api/project").json()
    assert review["name"] == "order_desk"
    assert [step["status"] for step in review["steps"]] == ["map"]
    assert review["steps"][0]["missing"] == ["ORDERS_DATABASE_URL"]
    assert review["connectors"] == []  # the project's own instance is covered by its step
    assert review["data"]["exists"] is True
    assert review["data"]["files"] == ["orders.db"]
    assert [case["id"] for case in review["cases"]] == ["order-status"]
    assert all(issue["severity"] != "error" for issue in review["issues"])

    tested = events_integration(client.post("/api/test", json={}, headers=SAFE).text)
    assert tested[-1] == ("done", {"passed": 1, "total": 1})

    ran = events_integration(
        client.post("/api/run", json={"message": "Where is 1002?"}, headers=SAFE).text
    )
    step = next(data for kind, data in ran if kind == "activity")
    assert step["text"].startswith("Look something up in the Order database")
    assert "1002" in step["text"] and "uses" not in step["text"]
    kind, answer = ran[-1]
    assert kind == "done"
    assert "shipped" in answer["text"]

    setup = client.get("/api/setup").json()
    assert {"name": "ORDERS_DATABASE_URL", "set": False} in setup["project"]
    assert client.get("/api/deploy").json()["not_connected"] == ["Order database"]

    systems = client.get("/api/connections").json()["systems"]
    assert [(s["system"], s["status"], s["mode"]) for s in systems] == [
        ("Order database", "not_set_up", "sample")
    ]
    assert [(f["name"], f["kind"]) for f in systems[0]["fields"]] == [
        ("ORDERS_DATABASE_URL", "secret")
    ]


@pytest.mark.slow
def test_a_project_built_in_the_web_app_runs_unchanged_from_python_and_the_terminal(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import awa
    from awa.cli import main
    from awa.evals import Dataset, evaluate

    built = events_integration(
        client.post(
            "/api/build", json={"process": "Customers ask about orders."}, headers=SAFE
        ).text
    )
    assert built[-1][0] == "done"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    models = ModelRegistry()
    models.register("default", FunctionChatModel(respond))

    desk = awa.load(tmp_path, model_registry=models)
    answer = asyncio.run(desk.run("Where is 1002?"))
    report = asyncio.run(evaluate(desk, Dataset.load(tmp_path / "evals" / "cases.yaml")))

    assert "shipped" in answer.output
    assert report.pass_rate == 1.0
    assert main(["validate", str(tmp_path / "blueprint.yaml")]) == 0
