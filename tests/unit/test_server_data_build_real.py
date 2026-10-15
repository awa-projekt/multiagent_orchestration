"""The data endpoints on the real `awa.blueprints.build_project` (scripted models, real example files).

These tests need the data-first build (`build_project(data=...)` and `tests/_helpers/build_scripts.py`)
and skip until it is part of the tree they run in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from _helpers.data_http import KEYS, auth, client_for, make_app, sse_events, start_build, wait_for
from awa.models import ModelRegistry

scripts = pytest.importorskip("_helpers.build_scripts")

H = auth("k-alice")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Any:
    played = scripts.Script()
    registry = ModelRegistry()
    for profile in ("default", "fast", "strong"):
        registry.register(profile, played.model())
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return played


async def upload_real(client: Any, *paths: Path) -> str:
    response = await client.post(
        "/v1/uploads",
        headers=H,
        files=[
            ("files", (path.name, path.read_bytes(), "application/octet-stream")) for path in paths
        ],
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


async def test_a_real_build_from_an_upload_to_a_run(tmp_path: Path, script: Any) -> None:
    store = tmp_path / "store"
    async with client_for(make_app(store, real=True)) as client:
        upload_id = await upload_real(client, scripts.ORDERS_CSV)

        started = await start_build(
            client, upload_id, description=scripts.PROCESS, options={"rounds": 2, "target": 1.0}
        )
        build_id = started.json()["build_id"]
        done = await wait_for(client, build_id, "completed")

        report = done["report"]
        assert (report["passed"], report["stopped"], report["rounds"]) == (
            True,
            "target_reached",
            1,
        )
        assert "data/files/orders.csv" in report["files"] and "blueprint.yaml" in report["files"]
        assert done["usage"]["requests"] > 0 and done["pass_rate"] == 1.0
        events = sse_events((await client.get(f"/v1/builds/{build_id}/events", headers=H)).text)
        kinds = [e["event"] for e in events]
        assert kinds[0] == "build.started" and kinds[-1] == "build.completed"
        assert kinds.count("build.started") == kinds.count("build.completed") == 1
        for expected in ("data.profile.file", "design.acceptance", "eval.round.finished"):
            assert expected in kinds, kinds
        assert [e["id"] for e in events] == [str(n) for n in range(1, len(events) + 1)]
        everything = json.dumps(done) + json.dumps(events)
        assert str(tmp_path) not in everything and tmp_path.as_posix() not in everything
        assert str(scripts.ORDERS_CSV) not in everything

        readme = await client.get(f"/v1/builds/{build_id}/files/README.md", headers=H)
        profile = await client.get(f"/v1/builds/{build_id}/files/data/PROFILE.json", headers=H)
        assert readme.status_code == 200 and "orders.csv" in readme.text
        assert json.loads(profile.text)["files"][0]["path"] == "orders.csv"

        ran = await client.post(
            f"/v1/builds/{build_id}/runs",
            headers=H,
            json={"input": "Check order B-1004 in orders.csv."},
        )
        assert ran.status_code == 200, ran.text
        assert "B-1004" in ran.json()["text"]
        assert str(tmp_path) not in ran.text
        assert KEYS


async def test_a_description_that_does_not_fit_the_files_asks_and_the_answer_resumes(
    tmp_path: Path, script: Any
) -> None:
    async with client_for(make_app(tmp_path / "store", real=True)) as client:
        upload_id = await upload_real(client, scripts.ORDERS_CSV, scripts.PAYMENTS_CSV)
        started = await start_build(
            client,
            upload_id,
            description="Screen the incoming CVs against our role profile and schedule interviews.",
        )
        build_id = started.json()["build_id"]

        waiting = await wait_for(client, build_id, "needs_input")

        assert waiting["questions"] and waiting["questions"][0]["id"] == "q1"
        assert waiting["questions"][0]["evidence"]  # it cites ids of the profile
        assert script.calls("designer") == 0  # not one design call before the question
        answered = await client.post(
            f"/v1/builds/{build_id}/answers",
            headers=H,
            json={
                "answers": [
                    {
                        "id": "q1",
                        "value": "Use these payments to chase customers who paid too little.",
                    }
                ]
            },
        )
        assert answered.status_code == 202
        done = await wait_for(client, build_id, "completed")
        assert done["report"]["passed"] is True
        assert script.calls("designer") >= 1
        assert "chase customers" in script.prompts["designer"][0]  # the user's words, labelled


async def test_two_tenants_building_the_same_files_do_not_share_anything(
    tmp_path: Path, script: Any
) -> None:
    async with client_for(make_app(tmp_path / "store", real=True)) as client:
        mine = await upload_real(client, scripts.ORDERS_CSV)
        build_id = (await start_build(client, mine, description=scripts.PROCESS)).json()["build_id"]
        await wait_for(client, build_id, "completed")

        stranger = auth("k-gus")
        for path in ("", "/events", "/project.zip", "/files/blueprint.yaml"):
            response = await client.get(f"/v1/builds/{build_id}{path}", headers=stranger)
            assert response.status_code == 404
        run = await client.post(
            f"/v1/builds/{build_id}/runs", headers=stranger, json={"input": "hi"}
        )
        assert run.status_code == 404
