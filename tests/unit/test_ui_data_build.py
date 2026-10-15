"""Data -> Describe -> build -> Review -> Test, end to end through the web app: real example files
are dropped, analysed, described in plain words, handed to the real build with scripted models,
and the written project works on the copies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from test_blueprints_data_kinds import ACCEPTANCE, BLUEPRINT, INTAKE, INTAKE_PROCESS

from _helpers.build_scripts import ORDERS_CSV, PROCESS, Script
from awa.blueprints import Blueprint
from awa.blueprints._data_project import check_project
from awa.data import DataProfile
from awa.models import ModelRegistry
from awa.testing import tool_call
from awa.ui import create_ui_app

TOKEN = "data-build-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = [dict(line.split(": ", 1) for line in f.splitlines()) for f in body.split("\n\n") if f]
    return [(frame["event"], json.loads(frame["data"])) for frame in frames if "event" in frame]


def clerk(call: Any) -> Any:
    """The designed clerk: reads the Meyer invoice, then says what it did."""

    if any(isinstance(m, ToolMessage) for m in call.messages):
        return "Invoice LN-2024-17 checked; inbox/scan_0001.pdf is held for manual entry."
    return tool_call(
        "files_read_excel",
        {"path": "inbox/ln-2024-17_meyer.xlsx", "sheet": "Kopf", "header_row": 0},
    )


def app_for(folder: Path, script: Script) -> Any:
    models = ModelRegistry()
    models.register("default", script.model())
    return create_ui_app(
        folder, token=TOKEN, port=8765, environ={}, model=script.model(), model_registry=models
    )


def test_files_dropped_in_the_web_app_are_what_the_build_designs_on(tmp_path: Path) -> None:
    original = sha(ORDERS_CSV)
    with TestClient(app_for(tmp_path, Script()), base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        stored = client.put(
            "/api/data/files",
            params={"name": "orders.csv"},
            content=ORDERS_CSV.read_bytes(),
            headers=SAFE,
        )
        client.post(
            "/api/data/note", json={"name": "orders.csv", "text": "Check totals"}, headers=SAFE
        )

        built = client.post("/api/build", json={"process": PROCESS}, headers=SAFE)

    assert stored.status_code == 201
    assert "event: done" in built.text and "Using your 1 data file(s)" in built.text
    assert sha(tmp_path / "data" / "files" / "orders.csv") == original  # a copy, byte for byte
    profile = DataProfile.from_json((tmp_path / "data" / "PROFILE.json").read_text("utf-8"))
    assert [f.path for f in profile.files] == ["orders.csv"]
    blueprint = Blueprint.from_yaml_file(tmp_path / "blueprint.yaml")
    assert [d.path for d in blueprint.data] == ["data/files/orders.csv"]
    assert blueprint.data[0].handling.startswith("Check totals")  # the note reached the design


@pytest.mark.slow
def test_the_intake_files_go_from_the_drop_zone_to_a_tested_project(tmp_path: Path) -> None:
    script = Script(acceptances=[ACCEPTANCE], blueprints=[BLUEPRINT], agent=clerk)
    sources = sorted(p for p in INTAKE.rglob("*") if p.is_file())
    with TestClient(app_for(tmp_path, script), base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        for source in sources:  # folder structure kept, as a dropped folder arrives
            name = source.relative_to(INTAKE).as_posix()
            sent = client.put(
                "/api/data/files", params={"name": name}, content=source.read_bytes(), headers=SAFE
            )
            assert sent.status_code == 201, (name, sent.text)
        waiting = client.get("/api/data").json()
        analysed = events(client.post("/api/data/analyse", json={}, headers=SAFE).text)
        state = analysed[-1][1]
        client.post(
            "/api/data/note",
            json={"name": "inbox/scan_0001.pdf", "text": "Hold it for manual entry."},
            headers=SAFE,
        )
        client.post("/api/data/mask", json={"mask": "placeholders"}, headers=SAFE)

        described = client.get("/api/state").json()
        built = events(
            client.post("/api/build", json={"process": INTAKE_PROCESS}, headers=SAFE).text
        )
        review = client.get("/api/project").json()
        tested = events(client.post("/api/test", json={}, headers=SAFE).text)
        tried = events(
            client.post("/api/run", json={"message": "Check the Meyer invoice."}, headers=SAFE).text
        )

    names = [source.relative_to(INTAKE).as_posix() for source in sources]
    assert [f["name"] for f in waiting["files"]] == names
    assert {f["status"] for f in waiting["files"]} == {"new"} and waiting["analysed"] is False
    assert analysed[-1][0] == "done" and state["analysed"] is True
    cards = {c["name"]: c for c in state["files"]}
    assert cards["inbox/scan_0001.pdf"]["headline"].startswith("PDF document with 1 page")
    assert any("Looks like a scan" in f["text"] for f in cards["inbox/scan_0001.pdf"]["flags"])
    assert cards["inbox/2024-0412_mueller.pdf"]["headline"].endswith("its text can be read.")
    assert cards["inbox/88231_zuercher.docx"]["kind_label"] == "Word document"
    meyer = cards["inbox/ln-2024-17_meyer.xlsx"]
    assert meyer["headline"].startswith("Excel workbook with 2 sheets")
    assert any("totals row" in f["text"] for f in meyer["flags"])
    assert cards["vendors.csv"]["tables"][0]["columns"][0]["name"]
    assert cards["purchase_orders.json"]["kind_label"] == "JSON data"
    assert described["has_system"] is False

    assert built[-1][0] == "done" and built[-1][1]["passed"] is True, built[-3:]
    assert ("progress", {"message": f"Using your {len(sources)} data file(s)"}) in built
    for source in sources:  # every original and its copy in the project are identical
        assert sha(tmp_path / "data" / "files" / source.relative_to(INTAKE)) == sha(source)
    blueprint = Blueprint.from_yaml_file(tmp_path / "blueprint.yaml")
    by_name = {entry.name: entry for entry in blueprint.data}
    assert by_name["scan_0001"].handling.startswith("Hold it for manual entry.")  # the UI note
    assert not [i for i in check_project(blueprint) if i.severity == "error"]
    assert review["data"]["count"] >= len(sources)
    assert "files/inbox/scan_0001.pdf" in review["data"]["files"]
    assert tested[-1] == ("done", {"passed": 2, "total": 2})
    assert tried[-1][0] == "done" and "LN-2024-17" in tried[-1][1]["text"]
    kept = json.loads((tmp_path / ".awa" / "data.json").read_text("utf-8"))
    assert kept["mask"] == "placeholders"
    assert kept["notes"] == {"inbox/scan_0001.pdf": "Hold it for manual entry."}
