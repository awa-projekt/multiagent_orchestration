"""Opening an example in the web app, and the pattern panel and Assistant page."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_blueprints_auto import PROCESS, scripted

from awa.core import dotenv
from awa.errors import ConfigurationError
from awa.testing import FakeChatModel
from awa.ui import Project, create_ui_app
from awa.ui._examples import (
    example_target,
    find_examples,
    list_examples,
    open_example,
    process_text,
    project_process,
)
from awa.ui._patterns import Patterns

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
TOKEN = "examples-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}
IDS = [e["id"] for e in list_examples(EXAMPLES)]


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = [dict(line.split(": ", 1) for line in f.splitlines()) for f in body.split("\n\n") if f]
    return [(frame["event"], json.loads(frame["data"])) for frame in frames if "event" in frame]


def client_for(folder: Path, examples: Path | None = EXAMPLES) -> TestClient:
    app = create_ui_app(Project(folder, environ={}), token=TOKEN, port=8765, examples=examples)
    client = TestClient(app, base_url=BASE)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


def fake_example(root: Path, name: str = "new-desk", *, process: bool = True) -> Path:
    folder = root / "usecases" / name
    (folder / "data" / "inbox").mkdir(parents=True)
    (folder / "data" / "outbox").mkdir()
    (folder / "README.md").write_text(
        "# Use case: new desk\n\n**Process.** Customers write in.\nThe desk answers.\n\nMore.\n",
        encoding="utf-8",
    )
    (folder / "blueprint.yaml").write_text(
        "name: new-desk\ndescription: Answers customers.\nagents:\n  - name: desk\n",
        encoding="utf-8",
    )
    (folder / "cases.yaml").write_text("name: new-desk\ncases: []\n", encoding="utf-8")
    (folder / "data" / "customers.csv").write_text("id;name\n1;Ada\n", encoding="utf-8")
    (folder / "data" / "inbox" / "mail-1.txt").write_text("Where is my order?\n", "utf-8")
    (folder / "data" / "outbox" / "reply.txt").write_text("output\n", encoding="utf-8")
    if process:
        (folder / "process.md").write_text("# New desk\n\nCustomers write in.\n", "utf-8")
    return folder


# ---- the library


def test_every_shipped_example_is_listed_use_cases_first() -> None:
    found = list_examples(EXAMPLES)
    kinds = [e["kind"] for e in found]

    assert {"expense-report-audit", "deep-research"} <= set(IDS)
    assert kinds == sorted(kinds, key=lambda kind: kind != "use case")
    assert all(e["title"][:1].isupper() and e["description"] for e in found)
    assert next(e for e in found if e["id"] == "expense-report-audit")["files"] >= 20
    assert next(e for e in found if e["id"] == "lead-qualification")["files"] == 0


def test_a_new_example_folder_is_listed_without_a_catalog_entry(tmp_path: Path) -> None:
    fake_example(tmp_path)

    (entry,) = list_examples(tmp_path)

    assert entry == {
        "id": "new-desk",
        "kind": "use case",
        "title": "New desk",
        "description": "Answers customers.",
        "files": 2,
    }


def test_opening_copies_the_data_under_files_with_folders_and_names_kept(tmp_path: Path) -> None:
    target = tmp_path / "project"

    opened = open_example("expense-report-audit", target, examples=EXAMPLES)

    source = EXAMPLES / "usecases" / "expense-report-audit"
    files = sorted(
        p.relative_to(source / "data").as_posix()
        for p in (source / "data").rglob("*")
        if p.is_file()
    )
    assert opened["files"] == files and "receipts/EXP-2001-1.pdf" in files
    for name in files:
        copy = target / "data" / "files" / name
        assert copy.read_bytes() == (source / "data" / name).read_bytes()
    assert opened["process"] == process_text((source / "process.md").read_text("utf-8"))
    assert (
        project_process(target) == opened["process"] and opened["title"] == "Expense report audit"
    )
    assert (target / "evals" / "cases.yaml").read_bytes() == (source / "cases.yaml").read_bytes()
    assert (target / "reference" / "blueprint.yaml").is_file()
    assert not (target / "blueprint.yaml").exists() and not (target / "tools.py").exists()
    assert Project(target, environ={}).cases()  # the Test step lists the example's cases


def test_outputs_are_left_out_and_a_readme_paragraph_stands_in_for_process_md(
    tmp_path: Path,
) -> None:
    examples = tmp_path / "examples"
    fake_example(examples, process=False)

    opened = open_example("new-desk", tmp_path / "p", examples=examples)

    assert opened["files"] == ["customers.csv", "inbox/mail-1.txt"]
    assert opened["process"] == "Customers write in. The desk answers."
    assert opened["title"] == "New desk"


def test_settings_are_copied_as_a_file_into_the_new_project(tmp_path: Path) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / ".env").write_text("AWA_DEFAULT_MODEL=test\n", encoding="utf-8")

    open_example(
        "lead-qualification", tmp_path / "new", examples=EXAMPLES, settings_from=tmp_path / "old"
    )

    assert (tmp_path / "new" / ".env").read_bytes() == (tmp_path / "old" / ".env").read_bytes()


def test_copied_settings_are_readable_by_the_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted: list[Path] = []
    monkeypatch.setattr(dotenv, "restrict_to_owner", restricted.append)
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / ".env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")

    open_example(
        "lead-qualification", tmp_path / "new", examples=EXAMPLES, settings_from=tmp_path / "old"
    )

    copy = tmp_path / "new" / ".env"
    assert restricted == [copy]
    if os.name == "posix":
        assert copy.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad", ["no-such-example", "../usecases", "Expense", ""])
def test_an_unknown_example_is_refused_with_the_list(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigurationError, match="expense-report-audit"):
        open_example(bad, tmp_path / "p", examples=EXAMPLES)
    assert not (tmp_path / "p").exists()


def test_a_folder_that_holds_a_project_is_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / "blueprint.yaml").write_text("name: mine\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="already holds a project"):
        open_example("deep-research", tmp_path, examples=EXAMPLES)


def test_without_examples_folder_nothing_is_listed_and_opening_says_why(tmp_path: Path) -> None:
    assert find_examples(tmp_path) is None and list_examples(tmp_path) == []
    with pytest.raises(ConfigurationError, match="--examples"):
        open_example("deep-research", tmp_path / "p", examples=tmp_path)


def test_the_target_is_the_folder_while_it_is_fresh_else_a_new_one(tmp_path: Path) -> None:
    base = tmp_path / "work"
    assert example_target(base, "deep-research", beside=True) == base.resolve()
    (base / ".awa").mkdir(parents=True)
    assert example_target(base, "deep-research", beside=True) == base.resolve()
    (base / "blueprint.yaml").write_text("x", encoding="utf-8")
    assert (
        example_target(base, "deep-research", beside=True) == (tmp_path / "deep-research").resolve()
    )
    assert example_target(base, "deep-research", beside=False) == (base / "deep-research").resolve()
    (tmp_path / "deep-research").mkdir()
    (tmp_path / "deep-research" / "process.md").write_text("x", encoding="utf-8")
    assert (
        example_target(base, "deep-research", beside=True)
        == (tmp_path / "deep-research-2").resolve()
    )


def test_every_example_data_set_is_analysed_without_an_unreadable_file(tmp_path: Path) -> None:
    unread: dict[str, list[str]] = {}
    for example in IDS:
        target = tmp_path / example
        open_example(example, target, examples=EXAMPLES)
        state = Project(target, environ={}).data.analyse(lambda event, data: None)
        assert state["analysed"] or not state["files"], example
        bad = [
            f"{c['name']}: {c['status']}"
            for c in state["files"]
            if c["status"] in ("error", "unsupported")
        ]
        if bad:
            unread[example] = bad

    assert unread == {}


# ---- the web app


def test_the_page_lists_and_opens_an_example_and_switches_to_it(tmp_path: Path) -> None:
    start = tmp_path / "start"
    start.mkdir()
    (start / "blueprint.yaml").write_text("name: mine\n", encoding="utf-8")
    client = client_for(start)

    listed = client.get("/api/examples").json()["examples"]
    opened = client.post("/api/examples/open", json={"id": "expense-report-audit"}, headers=SAFE)

    assert [e["id"] for e in listed] == IDS
    assert opened.status_code == 201, opened.text
    folder = Path(opened.json()["folder"])
    assert folder == (tmp_path / "expense-report-audit").resolve()
    state = client.get("/api/state").json()
    assert state["folder"] == str(folder) and state["process"] == opened.json()["process"]
    assert state["has_cases"] and not state["has_system"]
    names = [f["name"] for f in client.get("/api/data").json()["files"]]
    assert "receipts/EXP-2001-1.pdf" in names and "policy.csv" in names
    assert client.get("/api/cases").json()["exists"]
    assert sorted(p.name for p in start.iterdir()) == ["blueprint.yaml"]  # left as it was


def test_the_page_refuses_bad_ids_and_needs_the_same_origin(tmp_path: Path) -> None:
    client = client_for(tmp_path / "p")

    assert client.post("/api/examples/open", json={"id": "../x"}, headers=SAFE).status_code == 422
    unknown = client.post("/api/examples/open", json={"id": "no-such"}, headers=SAFE)
    assert unknown.status_code == 400 and "no example" in unknown.json()["error"]["message"]
    assert client.post("/api/examples/open", json={"id": "deep-research"}).status_code == 403


def test_without_examples_the_page_offers_none(tmp_path: Path) -> None:
    client = client_for(tmp_path / "p", examples=tmp_path / "nothing")

    assert client.get("/api/examples").json() == {"examples": []}


def test_end_to_end_two_examples_are_opened_and_analysed(tmp_path: Path) -> None:
    client = client_for(tmp_path / "first")

    for example in ("order-to-cash-exceptions", "recruiting-cv-screening"):
        opened = client.post("/api/examples/open", json={"id": example}, headers=SAFE)
        assert opened.status_code == 201, opened.text
        answer = client.post("/api/data/analyse", json={}, headers=SAFE)
        (event, state), *_ = [e for e in events(answer.text) if e[0] in ("done", "error")]

        assert event == "done", state
        assert state["analysed"] and len(state["files"]) == len(opened.json()["files"])
        assert [c["name"] for c in state["files"] if c["status"] not in ("ok", "partial")] == []
        assert Path(opened.json()["folder"], "data", "PROFILE.json").is_file()
    first = (tmp_path / "first").resolve()
    assert Path(client.get("/api/state").json()["folder"]).parent == first.parent


# ---- the command line


def test_awa_ui_example_opens_the_example_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import awa.server._app as uvicorn_module
    from _helpers.uvicorn_doubles import recording_uvicorn
    from awa.cli import main

    served: list[Path] = []

    monkeypatch.setattr(uvicorn_module, "import_uvicorn", lambda: recording_uvicorn(served))
    project = tmp_path / "work"

    code = main(
        [
            "ui",
            "--project",
            str(project),
            "--example",
            "purchase-requisition",
            "--examples",
            str(EXAMPLES),
            "--no-browser",
            "--port",
            "0",
        ]
    )

    assert code == 0
    assert served == [project.resolve()]
    assert (project / "data" / "files" / "requisitions" / "REQ-1001.txt").is_file()
    assert "Opened the example Purchase requisition" in capsys.readouterr().out


def events_patterns(body: str) -> list[tuple[str, dict[str, Any]]]:
    parsed = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


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


def last(response_text: str) -> tuple[str, dict[str, Any]]:
    return events_patterns(response_text)[-1]


def test_describe_suggest_test_use_and_hand_over_end_to_end(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(model=scripted())

    suggested = last(
        client.post("/api/patterns/suggest", json={"process": PROCESS}, headers=SAFE).text
    )
    assert suggested[0] == "done"
    cards = suggested[1]["proposals"]
    assert 1 <= len(cards) <= 3 and suggested[1]["ranked_by"] == "words"
    assert all("<svg" in card["diagram"] and card["why"] for card in cards)

    ids = ["single-agent", "pipeline", "review-loop"]
    tested = events_patterns(
        client.post("/api/patterns/test", json={"process": PROCESS, "ids": ids}, headers=SAFE).text
    )
    names = [name for name, _ in tested]
    assert names.count("pattern.started") == 3 and names.count("pattern.done") == 3
    kind, result = tested[-1]
    assert kind == "done" and result["cap"] == 60 and not result["capped"]
    assert [t["status"] for t in result["trials"]] == ["scored"] * 3
    assert result["best"] == "single-agent"
    assert not (tmp_path / "blueprint.yaml").exists()  # nothing is written by a test

    used = client.post("/api/patterns/use", json={"id": "pipeline"}, headers=SAFE)
    assert used.status_code == 200 and used.json()["used"] == "pipeline"
    assert (tmp_path / "blueprint.yaml").is_file() and (tmp_path / "evals" / "cases.yaml").is_file()

    brief = client.get("/api/brief").json()
    assert brief["brief"].startswith("# Brief for a coding assistant: fixed_order")
    assert "`pipeline`" in brief["brief"] and not brief["exists"]
    assert client.post("/api/brief", json={}, headers=SAFE).json() == {"written": "BRIEF.md"}
    assert (tmp_path / "BRIEF.md").read_text(encoding="utf-8") == brief["brief"]
    again = client.post("/api/brief", json={}, headers=SAFE)
    assert again.status_code == 400 and "already exists" in again.json()["error"]["message"]
    assert client.post("/api/brief", json={"force": True}, headers=SAFE).status_code == 200


def test_a_model_the_person_did_not_allow_is_never_used(
    make_client: Callable[..., TestClient],
) -> None:
    class Live(FakeChatModel):
        """Stands for a real model: not a test double by its module."""

    Live.__module__ = "somewhere.else"
    model = Live(["never used"])
    client = make_client(model=model)

    suggested = last(
        client.post("/api/patterns/suggest", json={"process": PROCESS}, headers=SAFE).text
    )
    refused = client.post(
        "/api/patterns/test", json={"process": PROCESS, "ids": ["pipeline"]}, headers=SAFE
    )

    assert suggested[1]["ranked_by"] == "words"
    kind, error = last(refused.text)
    assert kind == "error" and "costs tokens" in error["message"]
    assert model.calls == []


def test_the_cap_is_shown_and_respected(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client(model=scripted())

    response = client.post(
        "/api/patterns/test",
        json={"process": PROCESS, "ids": ["pipeline", "single-agent"], "max_requests": 8},
        headers=SAFE,
    )

    kind, result = last(response.text)
    assert kind == "done" and result["capped"] and result["cap"] == 8
    assert result["trials"][1]["status"] == "stopped"
    assert (
        client.post("/api/patterns/use", json={"id": "single-agent"}, headers=SAFE).status_code
        == 400
    )
    assert not (tmp_path / "blueprint.yaml").exists()


def test_ranking_by_the_model_uses_the_default_model_when_the_project_names_none(
    tmp_path: Path,
) -> None:
    patterns = Patterns(Project(tmp_path))

    assert patterns.model() is None
    assert patterns.live(allow_model=True) == "default"
    assert patterns.live(allow_model=False) is None


@pytest.mark.parametrize(
    "body",
    [
        {"process": PROCESS, "ids": []},
        {"process": PROCESS, "ids": ["a", "b", "c", "d"]},
        {"process": PROCESS, "ids": ["pipeline"], "max_requests": 0},
        {"process": PROCESS, "ids": ["pipeline"], "max_requests": 500},
    ],
)
def test_the_test_is_limited_to_three_patterns_and_a_sane_cap(
    make_client: Callable[..., TestClient], body: dict[str, Any]
) -> None:
    assert (
        make_client(model=scripted())
        .post("/api/patterns/test", json=body, headers=SAFE)
        .status_code
        == 422
    )


def test_an_unknown_pattern_and_an_empty_description_are_explained(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(model=scripted())

    unknown = last(
        client.post(
            "/api/patterns/test", json={"process": PROCESS, "ids": ["nope"]}, headers=SAFE
        ).text
    )
    empty = last(client.post("/api/patterns/suggest", json={"process": " "}, headers=SAFE).text)

    assert unknown[0] == "error" and "no pattern" in unknown[1]["message"]
    assert empty[0] == "error" and "Describe the process" in empty[1]["message"]


def test_the_assistant_page_without_a_system_says_so(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()

    response = client.get("/api/brief")

    assert response.status_code == 200
    assert response.json() == {"brief": None, "file": "BRIEF.md", "exists": False}


def test_the_page_offers_the_panels_and_every_new_id_exists() -> None:
    from awa.ui._app import page_source

    page = page_source()
    for element in (
        "suggest-patterns",
        "patterns-result",
        "copy-brief",
        "download-brief",
        "save-brief",
    ):
        assert f'id="{element}"' in page
    assert re.search(r'<li><a href="#assistant">', page) and "const PAGES = [" in page
