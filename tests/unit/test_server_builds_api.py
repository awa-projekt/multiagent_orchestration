"""The build endpoints: start, follow, answer, cancel, export, run what was built, and keep tenants apart."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.data_http import (
    CSV,
    GOOD,
    KEYS,
    ScriptedBuildEngine,
    auth,
    client_for,
    make_app,
    sse_events,
    start_build,
    upload,
    wait_for,
)
from awa import Agent
from awa.blueprints.errors import BlueprintError, BlueprintIssue
from awa.errors import UsageLimitExceededError
from awa.server import RateLimit, ServedSystem
from awa.server.ports import BuildOutcome, Question
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

H = auth("k-alice")
ASK = [
    Question(
        "q1", "Which files are invoices?", options=("orders.csv", "none"), evidence=("f1.t1.id",)
    )
]


def error_of(response: httpx.Response) -> dict[str, Any]:
    return dict(response.json()["error"])


def refunds_system(project: Path) -> ServedSystem:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        return f"refunded {amount} for {order_id}"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"result: {call.last_message.content}"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    agent = Agent(
        "refunds",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(issue_refund)],
        checkpointer=InMemorySaver(),
    )
    return ServedSystem(agent)


# ---------------------------------------------------------------------- the whole path


async def test_a_build_goes_from_an_upload_and_a_description_to_a_report_and_a_project(
    tmp_path: Path,
) -> None:
    engine = ScriptedBuildEngine()
    async with client_for(make_app(tmp_path, engine=engine)) as client:
        upload_id = await upload(client)
        started = await start_build(
            client,
            upload_id,
            notes=[{"file": "orders.csv", "text": "Large orders need approval"}],
            options={"rounds": 2, "target": 0.8, "mask": "placeholders", "guidance": "Be brief"},
        )

        assert started.status_code == 202
        body = started.json()
        build_id = body["build_id"]
        assert build_id.startswith("bld_") and body["status"] == "queued"
        assert body["events_url"] == f"/v1/builds/{build_id}/events"
        assert body["url"] == started.headers["location"] == f"/v1/builds/{build_id}"
        done = await wait_for(client, build_id, "completed")

        spec = engine.calls[0]
        assert (spec.description, dict(spec.notes), spec.rounds, spec.target) == (
            "Check each order against the price list.",
            {"orders.csv": "Large orders need approval"},
            2,
            0.8,
        )
        assert (spec.model, spec.mask, spec.guidance, spec.max_requests) == (
            None,
            "placeholders",
            "Be brief",
            70,
        )
        assert dict(spec.answers) == {} and engine.data_seen == [{"orders.csv": CSV}]
        assert done["stage"] == "evaluate"
        assert done["upload_id"] == upload_id and done["pass_rate"] == 1.0 and done["round"] == 1
        assert done["usage"] == {"requests": 12, "total_tokens": 3456}
        assert done["finished_at"] and done["created_at"]
        report = done["report"]
        assert (report["passed"], report["pass_rate"], report["rounds"], report["stopped"]) == (
            True,
            1.0,
            1,
            "target_reached",
        )
        assert report["flaky"] == ["case_2"] and report["failing"] == []
        assert report["assumptions"] == ["Amounts are in euro"]
        assert report["summary"].startswith("order_desk: 3 of 3")
        assert report["files"] == [
            ".env.example",
            "README.md",
            "blueprint.yaml",
            "data/PROFILE.json",
            "data/files/orders.csv",
            "evals/cases.yaml",
        ]
        assert report["links"]["project"] == f"/v1/builds/{build_id}/project.zip"
        assert "sk-live-secret" not in json.dumps(done) and str(tmp_path) not in json.dumps(done)


async def test_the_project_can_be_downloaded_and_its_text_files_read_but_never_a_secret() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        archive = await client.get(f"/v1/builds/{build_id}/project.zip", headers=H)
        readme = await client.get(f"/v1/builds/{build_id}/files/README.md", headers=H)
        nested = await client.get(f"/v1/builds/{build_id}/files/data/files/orders.csv", headers=H)
        profile = await client.get(f"/v1/builds/{build_id}/files/data/PROFILE.json", headers=H)

        assert archive.headers["content-type"] == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
        assert "blueprint.yaml" in names and "data/PROFILE.json" in names
        assert ".env" not in names and "signing.key" not in names
        assert b"sk-live-secret" not in archive.content and b"PRIVATE KEY" not in archive.content
        assert readme.text == "# Order desk\n" and readme.headers["content-type"].startswith(
            "text/plain"
        )
        assert readme.headers["content-disposition"] == "inline"
        assert readme.headers["x-content-type-options"] == "nosniff"
        assert nested.content == CSV
        assert json.loads(profile.text)["schema"] == "awa.data/profile/v1"


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "signing.key",
        "%2e%2e/%2e%2e/etc/passwd",
        "..%2f..%2fsecret",
        "data/..%2f.env",
        "%2e%2e%5cwindows%5cwin.ini",
        "data%5c..%5c.env",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "evals/../.env",
        "nope.yaml",
        "data",
    ],
)
async def test_reading_a_project_file_cannot_leave_the_project_or_reach_a_secret(path: str) -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        response = await client.get(f"/v1/builds/{build_id}/files/{path}", headers=H)

        assert response.status_code in (400, 404, 422)
        assert "sk-live-secret" not in response.text and "root:" not in response.text


async def test_binary_and_large_files_are_not_shown_as_text() -> None:
    def extra_files(spec: Any) -> BuildOutcome:
        (spec.out_dir / "logo.png").write_bytes(
            bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        )
        (spec.out_dir / "big.txt").write_text("x" * 1_500_000, encoding="utf-8")
        return GOOD

    async with client_for(make_app(engine=ScriptedBuildEngine(extra_files))) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        binary = await client.get(f"/v1/builds/{build_id}/files/logo.png", headers=H)
        large = await client.get(f"/v1/builds/{build_id}/files/big.txt", headers=H)
        archive = await client.get(f"/v1/builds/{build_id}/project.zip", headers=H)

        assert binary.status_code == 415 and error_of(binary)["code"] == "awa.data.unsupported_type"
        assert large.status_code == 413 and error_of(large)["code"] == "awa.data.too_large"
        assert "logo.png" in zipfile.ZipFile(io.BytesIO(archive.content)).namelist()


async def test_the_events_are_numbered_replayed_after_last_event_id_and_end_with_204() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        everything = await client.get(f"/v1/builds/{build_id}/events", headers=H)
        later = await client.get(
            f"/v1/builds/{build_id}/events", headers={**H, "Last-Event-ID": "3"}
        )
        over = await client.get(
            f"/v1/builds/{build_id}/events", headers={**H, "Last-Event-ID": "5"}
        )
        junk = await client.get(
            f"/v1/builds/{build_id}/events", headers={**H, "Last-Event-ID": "abc"}
        )

        assert everything.headers["content-type"].startswith("text/event-stream")
        events = sse_events(everything.text)
        assert [e["event"] for e in events] == [
            "build.started",
            "data.profile.file",
            "design.acceptance",
            "eval.round.finished",
            "build.completed",
        ]
        assert [e["id"] for e in events] == ["1", "2", "3", "4", "5"]
        first = events[0]["data"]
        assert set(first) == {"seq", "ts", "type", "build_id", "data"}
        assert (first["seq"], first["type"], first["build_id"]) == (1, "build.started", build_id)
        assert events[1]["data"]["data"] == {
            "stage": "profile",
            "name": "orders.csv",
            "status": "ok",
        }
        assert events[4]["data"]["data"]["report"]["passed"] is True
        assert [e["id"] for e in sse_events(later.text)] == ["4", "5"]
        assert over.status_code == 204 and over.content == b""
        assert [e["id"] for e in sse_events(junk.text)] == ["1", "2", "3", "4", "5"]


async def test_a_client_that_follows_a_running_build_gets_its_events_as_they_happen() -> None:
    gate = asyncio.Event()
    engine = ScriptedBuildEngine(gate=gate)
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        following = asyncio.create_task(client.get(f"/v1/builds/{build_id}/events", headers=H))
        await engine.started.wait()
        await asyncio.sleep(0.05)
        running = (await client.get(f"/v1/builds/{build_id}", headers=H)).json()

        assert not following.done()  # the stream stays open while the build runs
        assert (
            running["status"] == "evaluating" and running["round"] == 1 and "report" not in running
        )
        gate.set()
        events = sse_events((await following).text)
        assert events[-1]["event"] == "build.completed" and len(events) == 5


async def test_a_build_can_be_started_as_an_event_stream_in_one_call() -> None:
    async with client_for(make_app()) as client:
        response = await client.post(
            "/v1/builds?stream=true",
            headers=H,
            json={"description": "Check orders.", "upload_id": await upload(client)},
        )

        assert response.status_code == 200
        assert response.headers["x-build-id"].startswith("bld_")
        assert [e["event"] for e in sse_events(response.text)][-1] == "build.completed"


# ---------------------------------------------------------------------- questions and answers


async def test_a_build_that_needs_input_waits_and_resumes_with_the_answers() -> None:
    engine = ScriptedBuildEngine(ASK, GOOD)
    async with client_for(make_app(engine=engine)) as client:
        upload_id = await upload(client)
        build_id = (await start_build(client, upload_id)).json()["build_id"]

        waiting = await wait_for(client, build_id, "needs_input")

        assert waiting["questions"] == [
            {
                "id": "q1",
                "text": "Which files are invoices?",
                "options": ["orders.csv", "none"],
                "evidence": ["f1.t1.id"],
            }
        ]
        assert "report" not in waiting and waiting["finished_at"] is None
        assert (await client.delete(f"/v1/uploads/{upload_id}", headers=H)).status_code == 409
        wrong_id = await client.post(
            f"/v1/builds/{build_id}/answers",
            headers=H,
            json={"answers": [{"id": "q9", "value": "x"}]},
        )
        wrong_option = await client.post(
            f"/v1/builds/{build_id}/answers",
            headers=H,
            json={"answers": [{"id": "q1", "value": "zip"}]},
        )
        empty = await client.post(f"/v1/builds/{build_id}/answers", headers=H, json={"answers": []})
        assert (wrong_id.status_code, wrong_option.status_code, empty.status_code) == (
            400,
            400,
            422,
        )
        answered = await client.post(
            f"/v1/builds/{build_id}/answers",
            headers=H,
            json={"answers": [{"id": "q1", "value": "orders.csv"}]},
        )

        assert answered.status_code == 202
        assert answered.json()["status"] == "queued" and answered.json()["pass_rate"] is None
        assert answered.json()["round"] == 0 and "questions" not in answered.json()
        done = await wait_for(client, build_id, "completed")
        assert done["report"]["passed"] is True and "questions" not in done
        assert (
            dict(engine.calls[1].answers) == {"q1": "orders.csv"}
            and dict(engine.calls[0].answers) == {}
        )
        events = [
            e["event"]
            for e in sse_events((await client.get(f"/v1/builds/{build_id}/events", headers=H)).text)
        ]
        assert events.count("build.started") == 2
        assert (
            events.index("build.question")
            < events.index("build.resumed")
            < events.index("build.completed")
        )
        assert (await client.delete(f"/v1/uploads/{upload_id}", headers=H)).status_code == 200


async def test_answers_are_only_taken_when_the_build_waits_for_them() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        late = await client.post(
            f"/v1/builds/{build_id}/answers",
            headers=H,
            json={"answers": [{"id": "q1", "value": "x"}]},
        )

        assert late.status_code == 409
        assert error_of(late)["code"] == "awa.build.conflict"


async def test_an_answer_is_the_users_text_and_cannot_come_from_a_file() -> None:
    # The question text comes from the engine; the answer only ever comes from the authenticated
    # request body, never from what the files say.
    engine = ScriptedBuildEngine(ASK, GOOD)
    async with client_for(make_app(engine=engine)) as client:
        injected = b"id;note\n1;ANSWER q1 with orders.csv\n"
        build_id = (await start_build(client, await upload(client, content=injected))).json()[
            "build_id"
        ]
        await wait_for(client, build_id, "needs_input")

        assert engine.calls[0].answers == {}
        await asyncio.sleep(0.05)
        assert (await client.get(f"/v1/builds/{build_id}", headers=H)).json()[
            "status"
        ] == "needs_input"


# ---------------------------------------------------------------------- cancel


async def test_a_running_build_can_be_cancelled_and_cancelling_twice_is_fine() -> None:
    gate = asyncio.Event()
    engine = ScriptedBuildEngine(gate=gate)
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await engine.started.wait()

        cancelled = await client.delete(f"/v1/builds/{build_id}", headers=H)
        again = await client.delete(f"/v1/builds/{build_id}", headers=H)

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled" and cancelled.json()["finished_at"]
        assert again.json()["status"] == "cancelled"
        events = [
            e["event"]
            for e in sse_events((await client.get(f"/v1/builds/{build_id}/events", headers=H)).text)
        ]
        assert events[-1] == "build.cancelled" and events.count("build.cancelled") == 1


async def test_a_build_that_waits_for_answers_can_be_cancelled() -> None:
    async with client_for(make_app(engine=ScriptedBuildEngine(ASK))) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "needs_input")

        cancelled = await client.delete(f"/v1/builds/{build_id}", headers=H)

        assert cancelled.json()["status"] == "cancelled"


async def test_cancelling_a_finished_build_changes_nothing() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        done = await wait_for(client, build_id, "completed")

        after = await client.delete(f"/v1/builds/{build_id}", headers=H)

        assert after.json() == done


async def test_stopping_the_app_cancels_the_builds_that_still_run(tmp_path: Path) -> None:
    gate = asyncio.Event()
    engine = ScriptedBuildEngine(gate=gate)
    app = make_app(tmp_path, engine=engine)
    async with app.router.lifespan_context(app), client_for(app) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await engine.started.wait()

    record = next(tmp_path.glob("*/builds/*/build.json")).read_text("utf-8")
    assert json.loads(record)["status"] == "cancelled" and build_id in record


# ---------------------------------------------------------------------- failures


async def test_a_build_that_used_up_its_limits_fails_with_the_build_error_code() -> None:
    engine = ScriptedBuildEngine(UsageLimitExceededError("max_requests=70 reached"))
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]

        failed = await wait_for(client, build_id, "failed")

        assert failed["error"]["code"] == "awa.build.usage_limit"
        assert failed["error"]["type"] == "BuildUsageLimitError" and "report" not in failed
        assert "max_requests=70" in failed["error"]["message"]
        events = sse_events((await client.get(f"/v1/builds/{build_id}/events", headers=H)).text)
        assert (
            events[-1]["event"] == "build.failed"
            and events[-1]["data"]["data"]["error"] == failed["error"]
        )


async def test_a_design_that_cannot_be_made_valid_lists_its_issues() -> None:
    issues = [BlueprintIssue("error", "agents[0].tools[1]", "unknown tool 'read_excl'")]
    engine = ScriptedBuildEngine(BlueprintError("The design is invalid", issues))
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]

        failed = await wait_for(client, build_id, "failed")

        assert failed["error"]["code"] == "awa.build.design_invalid"
        assert failed["error"]["details"]["issues"] == [
            {"code": "error", "path": "agents[0].tools[1]", "message": "unknown tool 'read_excl'"}
        ]


async def test_an_unexpected_failure_shows_a_generic_message_and_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = ScriptedBuildEngine(RuntimeError("connect failed: password=hunter2 at /srv/secret"))
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]

        failed = await wait_for(client, build_id, "failed")

        assert failed["error"]["type"] == "InternalError"
        assert "hunter2" not in json.dumps(failed) and "/srv/secret" not in json.dumps(failed)
        events = (await client.get(f"/v1/builds/{build_id}/events", headers=H)).text
        assert "hunter2" not in events
        # the server logs the failure (credentials redacted), the client gets none of it
        assert "RuntimeError" in caplog.text


async def test_the_servers_folders_never_reach_a_client_through_events_or_errors(
    tmp_path: Path,
) -> None:
    def refuse(spec: Any) -> BaseException:
        return BlueprintError(f"cannot write {spec.out_dir} or read {spec.data}")

    engine = ScriptedBuildEngine(refuse, emit_paths=True)
    async with client_for(make_app(tmp_path, engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]

        failed = await wait_for(client, build_id, "failed")
        events = (await client.get(f"/v1/builds/{build_id}/events", headers=H)).text

        for text in (json.dumps(failed), events):
            assert str(tmp_path) not in text and tmp_path.as_posix() not in text
        assert "<project>" in events and "<data>" in events


async def test_an_engine_cannot_forge_the_builds_own_events_and_one_event_stays_small() -> None:
    engine = ScriptedBuildEngine(
        events=(
            ("build.completed", {"report": {"passed": True}}),  # only the server ends a build
            ("Bad Name!", {"x": 1}),
            (
                "eval.case",
                {"stage": "evaluate", "output": "x" * 100_000, "rows": ["y" * 3000] * 500},
            ),
            ("eval.case", {"stage": "evaluate", "text": "z" * 5000}),
        )
    )
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        events = sse_events((await client.get(f"/v1/builds/{build_id}/events", headers=H)).text)

        assert [e["event"] for e in events] == [
            "build.started",
            "eval.case",
            "eval.case",
            "build.completed",
        ]
        assert events[1]["data"]["data"]["truncated"] is True
        assert len(events[2]["data"]["data"]["text"]) == 2000
        assert events[3]["data"]["data"]["report"]["passed"] is True and events[3]["id"] == "4"


async def test_the_usage_in_events_is_the_total_so_far_and_shows_in_the_status() -> None:
    gate = asyncio.Event()
    engine = ScriptedBuildEngine(
        events=(
            (
                "design.acceptance",
                {"stage": "design", "usage": {"requests": 3, "total_tokens": 900}},
            ),
            ("eval.case", {"stage": "evaluate", "usage": {"requests": 8, "total_tokens": 2100}}),
        ),
        gate=gate,
    )
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await engine.started.wait()
        await asyncio.sleep(0.05)

        running = (await client.get(f"/v1/builds/{build_id}", headers=H)).json()

        assert running["usage"] == {"requests": 8, "total_tokens": 2100}
        gate.set()


async def test_the_replay_buffer_keeps_only_the_newest_events() -> None:
    many = tuple(("eval.case", {"stage": "evaluate", "n": n}) for n in range(30))
    async with client_for(
        make_app(engine=ScriptedBuildEngine(events=many), options={"event_buffer": 10})
    ) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        events = sse_events((await client.get(f"/v1/builds/{build_id}/events", headers=H)).text)

        assert [e["id"] for e in events] == [str(n) for n in range(23, 33)]
        assert events[-1]["event"] == "build.completed"


# ---------------------------------------------------------------------- requests


@pytest.mark.parametrize(
    ("extra", "status", "code"),
    [
        ({"options": {"model": "azure_openai:gpt-expensive"}}, 400, "awa.invalid_request"),
        ({"options": {"max_requests": 5000}}, 400, "awa.invalid_request"),
        ({"options": {"rounds": 0}}, 422, "awa.validation"),
        ({"options": {"rounds": 9}}, 422, "awa.validation"),
        ({"options": {"target": 1.5}}, 422, "awa.validation"),
        ({"options": {"mask": "everything"}}, 422, "awa.validation"),
        ({"options": {"unknown": 1}}, 422, "awa.validation"),
        ({"description": ""}, 422, "awa.validation"),
        ({"description": "x" * 4001}, 422, "awa.validation"),
        ({"notes": [{"file": "a.csv", "text": ""}]}, 422, "awa.validation"),
        ({"unknown": 1}, 422, "awa.validation"),
        ({"upload_id": "../x"}, 422, "awa.validation"),
    ],
)
async def test_an_invalid_build_request_is_refused_before_anything_starts(
    extra: dict[str, Any], status: int, code: str
) -> None:
    engine = ScriptedBuildEngine()
    async with client_for(make_app(engine=engine)) as client:
        upload_id = await upload(client)
        body = {"description": "Check orders.", "upload_id": upload_id, **extra}

        response = await client.post("/v1/builds", headers=H, json=body)

        assert (response.status_code, error_of(response)["code"]) == (status, code)
        assert engine.calls == [] and (await client.get("/v1/builds", headers=H)).json() == {
            "builds": []
        }


async def test_a_listed_model_and_the_request_ceiling_are_taken() -> None:
    engine = ScriptedBuildEngine()
    async with client_for(
        make_app(engine=engine, options={"models": {"fast", "gpt-fast"}, "max_requests": 40})
    ) as client:
        upload_id = await upload(client)

        ok = await start_build(client, upload_id, options={"model": "fast", "max_requests": 40})
        default = await start_build(client, upload_id)
        await wait_for(client, ok.json()["build_id"], "completed")
        await wait_for(client, default.json()["build_id"], "completed")

        assert [(c.model, c.max_requests) for c in engine.calls] == [("fast", 40), (None, 40)]
        none_allowed = await start_build(client, upload_id, options={"model": "default"})
        assert none_allowed.status_code == 400


async def test_a_build_needs_an_upload_of_the_caller() -> None:
    async with client_for(make_app()) as client:
        missing = await start_build(client, "up_000000000000000000000000")
        theirs = await start_build(client, await upload(client, "k-gus"), key="k-alice")
        unauthenticated = await client.post(
            "/v1/builds", json={"description": "x", "upload_id": "up_1"}
        )

        assert (missing.status_code, theirs.status_code, unauthenticated.status_code) == (
            404,
            404,
            401,
        )
        assert error_of(missing)["code"] == error_of(theirs)["code"] == "awa.data.upload_not_found"


async def test_a_caller_without_the_builder_role_cannot_start_or_cancel_builds() -> None:
    async with client_for(make_app(options={"roles": {"builder"}})) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        refused = await start_build(client, "up_1", key="k-vera")
        cancel = await client.delete(f"/v1/builds/{build_id}", headers=auth("k-vera"))
        look = await client.get(f"/v1/builds/{build_id}", headers=auth("k-vera"))

        assert refused.status_code == 403 and cancel.status_code == 403
        assert look.status_code == 200  # same tenant: reading is not writing


async def test_one_tenant_can_run_only_so_many_builds_at_once_and_others_are_not_affected() -> None:
    gate = asyncio.Event()
    engine = ScriptedBuildEngine(gate=gate)
    async with client_for(make_app(engine=engine, options={"max_builds": 1})) as client:
        mine, theirs = await upload(client), await upload(client, "k-gus")
        first = await start_build(client, mine)
        await engine.started.wait()

        second = await start_build(client, mine)
        other = await start_build(client, theirs, key="k-gus")

        assert second.status_code == 429 and error_of(second)["code"] == "awa.data.quota_exceeded"
        assert int(second.headers["retry-after"]) >= 1
        assert other.status_code == 202
        gate.set()
        await wait_for(client, first.json()["build_id"], "completed")
        assert (await start_build(client, mine)).status_code == 202


async def test_new_builds_and_answers_are_rate_limited_per_principal() -> None:
    async with client_for(make_app(options={"rate_limit": RateLimit(3, per=60)})) as client:
        upload_id = await upload(client)  # 1
        first = await start_build(client, upload_id)  # 2
        second = await start_build(client, upload_id)  # 3
        third = await start_build(client, upload_id)  # over

        assert (first.status_code, second.status_code, third.status_code) == (202, 202, 429)
        assert error_of(third)["type"] == "RateLimitExceededError"


# ---------------------------------------------------------------------- who sees what


async def test_another_tenant_gets_the_same_404_for_every_build_endpoint() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")
        gus = auth("k-gus")
        unknown = "bld_000000000000000000000000"
        calls = (
            ("GET", "", None),
            ("GET", "/events", None),
            ("DELETE", "", None),
            ("POST", "/answers", {"answers": [{"id": "q1", "value": "x"}]}),
            ("GET", "/project.zip", None),
            ("GET", "/files/README.md", None),
            ("POST", "/runs", {"input": "hi"}),
            ("GET", "/threads/t1/interrupts", None),
            ("POST", "/threads/t1/resume", {"decision": {"action": "approve"}}),
        )

        for method, suffix, body in calls:
            stranger = await client.request(
                method, f"/v1/builds/{build_id}{suffix}", headers=gus, json=body
            )
            missing = await client.request(
                method, f"/v1/builds/{unknown}{suffix}", headers=gus, json=body
            )
            assert (stranger.status_code, missing.status_code) == (404, 404), (method, suffix)
            assert error_of(stranger)["code"] == error_of(missing)["code"] == "awa.build.not_found"
            assert error_of(stranger)["message"].replace(build_id, "X") == error_of(missing)[
                "message"
            ].replace(unknown, "X")
        assert (await client.get("/v1/builds", headers=gus)).json() == {"builds": []}
        assert [
            b["build_id"]
            for b in (await client.get("/v1/builds", headers=auth("k-bob"))).json()["builds"]
        ] == [build_id]


async def test_with_principal_scope_a_colleague_does_not_see_the_build() -> None:
    async with client_for(make_app(thread_scope="principal")) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        assert (
            await client.get(f"/v1/builds/{build_id}", headers=auth("k-bob"))
        ).status_code == 404
        assert (await client.get("/v1/builds", headers=auth("k-bob"))).json() == {"builds": []}
        assert (await client.get("/v1/builds", headers=H)).json()["builds"][0][
            "build_id"
        ] == build_id


async def test_the_list_is_newest_first_and_limited() -> None:
    async with client_for(make_app()) as client:
        upload_id = await upload(client)
        ids = []
        for _ in range(3):
            ids.append((await start_build(client, upload_id)).json()["build_id"])
            await wait_for(client, ids[-1], "completed")

        listed = (await client.get("/v1/builds?limit=2", headers=H)).json()["builds"]

        assert [b["build_id"] for b in listed] == [ids[2], ids[1]]
        assert (await client.get("/v1/builds?limit=0", headers=H)).status_code == 422


# ---------------------------------------------------------------------- running what was built


async def test_the_built_process_runs_like_a_system_and_is_not_listed_as_one() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        ran = await client.post(f"/v1/builds/{build_id}/runs", headers=H, json={"input": "hello"})
        listing = await client.get("/v1/systems", headers=H)
        stranger = await client.post(
            "/v1/systems/build-" + build_id + "/runs", headers=auth("k-gus"), json={"input": "hi"}
        )

        assert ran.status_code == 200
        body = ran.json()
        assert (
            body["status"] == "completed"
            and body["text"] == "done: hello"
            and body["agent"] == "order_desk"
        )
        assert [s["name"] for s in listing.json()["systems"]] == ["echo"]
        assert stranger.status_code == 404


async def test_a_run_can_name_a_file_of_the_builds_profile() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        by_id = await client.post(f"/v1/builds/{build_id}/runs", headers=H, json={"file": "f1"})
        by_name = await client.post(
            f"/v1/builds/{build_id}/runs",
            headers=H,
            json={"file": "orders.csv", "input": "Check it"},
        )
        unknown = await client.post(
            f"/v1/builds/{build_id}/runs", headers=H, json={"file": "../../x"}
        )
        nothing = await client.post(f"/v1/builds/{build_id}/runs", headers=H, json={})

        assert by_id.json()["text"] == "done: Process orders.csv"
        assert by_name.json()["text"] == "done: Check it\n\nFile: orders.csv"
        assert unknown.status_code == 400 and nothing.status_code == 400


async def test_a_build_that_has_not_completed_cannot_be_run() -> None:
    gate = asyncio.Event()
    engine = ScriptedBuildEngine(gate=gate)
    async with client_for(make_app(engine=engine)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await engine.started.wait()

        early = await client.post(f"/v1/builds/{build_id}/runs", headers=H, json={"input": "hi"})
        zip_early = await client.get(f"/v1/builds/{build_id}/project.zip", headers=H)

        assert early.status_code == 409 and zip_early.status_code == 409
        assert error_of(early)["code"] == "awa.build.conflict"
        gate.set()


async def test_runs_can_be_async_and_belong_to_their_caller() -> None:
    async with client_for(make_app()) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        accepted = await client.post(
            f"/v1/builds/{build_id}/runs?mode=async", headers=H, json={"input": "hi"}
        )
        run_id = accepted.json()["run_id"]

        assert accepted.status_code == 202
        assert accepted.headers["location"] == f"/v1/runs/{run_id}"
        for _ in range(200):  # the run finishes in the background; poll instead of a fixed sleep
            status = (await client.get(f"/v1/runs/{run_id}", headers=H)).json()["status"]
            if status != "running":
                break
            await asyncio.sleep(0.02)
        assert status == "completed"
        assert (await client.get(f"/v1/runs/{run_id}", headers=auth("k-gus"))).status_code == 404


async def test_approvals_of_a_run_are_answered_through_the_build_by_approvers_of_the_tenant() -> (
    None
):
    app = make_app(system_factory=refunds_system, options={"approver_roles": {"approver"}})
    async with client_for(app) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        paused = await client.post(
            f"/v1/builds/{build_id}/runs", headers=H, json={"input": "refund order A-1"}
        )
        thread = paused.json()["thread_id"]
        base = f"/v1/builds/{build_id}/threads/{thread}"
        pending = await client.get(f"{base}/interrupts", headers=H)
        decision = {"decision": {"action": "approve"}}
        by_owner = await client.post(f"{base}/resume", headers=H, json=decision)
        by_stranger = await client.post(f"{base}/resume", headers=auth("k-gus"), json=decision)
        read_by_stranger = await client.get(f"{base}/interrupts", headers=auth("k-gus"))
        by_approver = await client.post(f"{base}/resume", headers=auth("k-rita"), json=decision)

        assert paused.json()["status"] == "interrupted" and pending.json()["interrupts"]
        assert by_owner.status_code == 403  # alice is no approver here
        assert by_stranger.status_code == 404 and read_by_stranger.status_code == 404
        assert by_approver.status_code == 200
        assert (
            by_approver.json()["status"] == "completed"
            and "refunded 480" in by_approver.json()["text"]
        )


# ---------------------------------------------------------------------- stored builds survive a restart


async def test_after_a_restart_a_finished_build_is_still_there_and_a_running_one_has_failed(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    first = make_app(tmp_path, engine=ScriptedBuildEngine(GOOD))
    async with client_for(first) as client:
        upload_id = await upload(client)
        finished = (await start_build(client, upload_id)).json()["build_id"]
        await wait_for(client, finished, "completed")
    blocked = ScriptedBuildEngine(gate=gate)
    second = make_app(tmp_path, engine=blocked)
    async with client_for(second) as client:
        running = (await start_build(client, upload_id)).json()["build_id"]
        await blocked.started.wait()

        third = make_app(tmp_path)  # the server starts again while that build was running
        async with client_for(third) as again:
            kept = (await again.get(f"/v1/builds/{finished}", headers=H)).json()
            lost = (await again.get(f"/v1/builds/{running}", headers=H)).json()
            replay = await again.get(f"/v1/builds/{finished}/events", headers=H)
            nothing_more = await again.get(
                f"/v1/builds/{finished}/events", headers={**H, "Last-Event-ID": "1"}
            )
            archive = await again.get(f"/v1/builds/{finished}/project.zip", headers=H)

        assert kept["status"] == "completed" and kept["report"]["passed"] is True
        assert lost["status"] == "failed" and lost["error"]["code"] == "awa.service_unavailable"
        assert [e["event"] for e in sse_events(replay.text)] == ["build.completed"]
        assert nothing_more.status_code == 204
        assert "blueprint.yaml" in zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
        gate.set()


async def test_after_a_restart_a_build_that_waits_for_answers_can_still_be_answered(
    tmp_path: Path,
) -> None:
    first = make_app(tmp_path, engine=ScriptedBuildEngine(ASK))
    async with client_for(first) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "needs_input")

    engine = ScriptedBuildEngine(GOOD)
    async with client_for(make_app(tmp_path, engine=engine)) as client:
        waiting = (await client.get(f"/v1/builds/{build_id}", headers=H)).json()
        answered = await client.post(
            f"/v1/builds/{build_id}/answers",
            headers=H,
            json={"answers": [{"id": "q1", "value": "none"}]},
        )
        done = await wait_for(client, build_id, "completed")

        assert waiting["status"] == "needs_input" and answered.status_code == 202
        assert dict(engine.calls[0].answers) == {"q1": "none"}
        assert done["report"]["passed"] is True


async def test_builds_count_against_the_storage_quota_of_the_tenant(tmp_path: Path) -> None:
    async with client_for(make_app(tmp_path, options={"max_tenant_bytes": 700})) as client:
        upload_id = await upload(client)
        build_id = (await start_build(client, upload_id)).json()["build_id"]
        await wait_for(client, build_id, "completed")

        refused = await client.post(
            "/v1/uploads", headers=H, files=[("files", ("b.csv", CSV * 20, "text/csv"))]
        )

        assert refused.status_code == 429 and error_of(refused)["code"] == "awa.data.quota_exceeded"


async def test_audit_records_what_happened_to_a_build() -> None:
    records: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    class Log:
        def __call__(self, event: Any) -> None: ...

        def record(self, event_type: str, data: dict[str, Any], **identity: Any) -> None:
            records.append((event_type, data, identity))

    async with client_for(make_app(audit_log=Log())) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

    kinds = [name for name, _, _ in records]
    assert kinds == ["upload_created", "build_started", "build_finished"]
    assert records[-1][1] == {"build_id": build_id, "status": "completed"}
    assert records[-1][2]["actor"] == "alice" and records[-1][2]["tenant"] == "acme"
    assert KEYS and "Check each order" not in json.dumps(records, default=str)


def slow_system(loads: list[Path], *, max_concurrency: int | None = None) -> Any:
    def factory(project: Path) -> ServedSystem:
        import time

        loads.append(project)
        time.sleep(0.2)  # a slow first load: concurrent first runs overlap it

        @tool
        async def look_up(order_id: str) -> str:
            """Look an order up."""
            await asyncio.sleep(0.3)
            return f"order {order_id}"

        def respond(call: ModelCall) -> Any:
            if isinstance(call.last_message, ToolMessage):
                return f"done: {call.last_message.content}"
            return tool_call("look_up", {"order_id": "A-1"})

        agent = Agent(
            "order_desk",
            model=FunctionChatModel(respond),
            tools=[look_up],
            checkpointer=InMemorySaver(),
        )
        return ServedSystem(agent, max_concurrency=max_concurrency, queue_timeout=0)

    return factory


async def test_concurrent_first_runs_of_a_build_load_its_system_once() -> None:
    loads: list[Path] = []
    async with client_for(make_app(system_factory=slow_system(loads))) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")
        runs = await asyncio.gather(
            *(
                client.post(f"/v1/builds/{build_id}/runs", headers=H, json={"input": str(n)})
                for n in range(3)
            )
        )
    assert [run.status_code for run in runs] == [200, 200, 200]
    assert len(loads) == 1


async def test_a_built_system_keeps_its_concurrency_limit() -> None:
    loads: list[Path] = []
    factory = slow_system(loads, max_concurrency=1)
    async with client_for(make_app(system_factory=factory)) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")
        await client.post(f"/v1/builds/{build_id}/runs", headers=H, json={"input": "warm"})
        runs = await asyncio.gather(
            *(
                client.post(f"/v1/builds/{build_id}/runs", headers=H, json={"input": str(n)})
                for n in range(2)
            )
        )
    assert sorted(run.status_code for run in runs) == [200, 503]


async def test_the_system_of_a_purged_build_is_no_longer_kept(tmp_path: Path) -> None:
    import gc
    import weakref

    from awa.server._data_filesystem import FileBuildStore
    from awa.server._data_kit import utcnow

    store = FileBuildStore(tmp_path, ttl=3600.0, clock=utcnow)
    made: list[weakref.ref[Any]] = []

    def factory(project: Path) -> ServedSystem:
        served = echo_like()
        made.append(weakref.ref(served.system))
        return served

    async with client_for(make_app(system_factory=factory, options={"builds": store})) as client:
        first = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, first, "completed")
        assert (
            await client.post(f"/v1/builds/{first}/runs", headers=H, json={"input": "a"})
        ).status_code == 200
        store.delete("acme", first)
        second = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, second, "completed")
        await client.post(f"/v1/builds/{second}/runs", headers=H, json={"input": "b"})
        gc.collect()
        assert made[0]() is None


def echo_like() -> ServedSystem:
    model = FunctionChatModel(lambda call: f"done: {call.last_message.content}")
    return ServedSystem(Agent("order_desk", model=model))


async def test_followers_of_a_build_that_waits_for_answers_leave_no_waiters_behind() -> None:
    from types import SimpleNamespace

    from awa.server._builds import _Live
    from awa.server.ports import BuildRecord

    service = SimpleNamespace(_event_buffer=10)
    live = _Live(BuildRecord("bld_1", "acme", "alice", _now(), status="needs_input"), service)  # type: ignore[arg-type]
    for _ in range(50):  # pings of a stream that follows the build
        await live.wait(0.001)
    follower = asyncio.ensure_future(live.wait(None))
    await asyncio.sleep(0)
    follower.cancel()  # the client went away
    with pytest.raises(asyncio.CancelledError):
        await follower
    assert live.waiters == []


async def test_a_late_engine_event_leaves_an_ended_build_ended() -> None:
    from types import SimpleNamespace

    from awa.server._builds import _Live
    from awa.server.ports import BuildRecord

    service = SimpleNamespace(_event_buffer=10, _clock=_now)
    live = _Live(BuildRecord("bld_1", "acme", "alice", _now(), status="cancelled"), service)  # type: ignore[arg-type]

    live.engine_emit("design.round", {"stage": "evaluate", "round": 2})

    assert live.record.status == "cancelled" and live.seq == 1


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def test_without_a_root_the_data_is_kept_in_a_folder_removed_with_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import gc

    import awa.server._data_api as data_api

    folder = tmp_path / "data"
    folder.mkdir()
    monkeypatch.setattr(data_api.tempfile, "mkdtemp", lambda prefix: str(folder))
    app = make_app()
    assert "temporary folder" in caplog.text and folder.is_dir()

    del app
    gc.collect()

    assert not folder.exists()


def test_an_exported_project_keeps_its_data_but_no_secret(tmp_path: Path) -> None:
    from awa.server._builds import project_files

    keep = [
        "blueprint.yaml",
        ".env.example",
        "config/other.yaml",
        "data/orders.db",
        "data/orders.db-wal",
        "data/orders.db-shm",
        "data/uploads/ledger.sqlite",
        "data/files/orders.csv",
    ]
    drop = [
        ".env",
        ".env.local",
        "signing.key",
        "cert.pem",
        "config/people.yaml",
        "cache.db",
        "tools/state.sqlite3",
        "cache.db-wal",
        ".awa/awa.db",
        ".awa/checkpoints.db",
        "__pycache__/x.pyc",
        ".venv/lib/x.py",
    ]
    for name in keep + drop:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")

    assert [p.as_posix() for p in project_files(tmp_path)] == sorted(keep)


async def test_a_follower_sees_events_that_arrive_while_it_handles_one() -> None:
    from types import SimpleNamespace

    from awa.server._builds import BuildService

    live = SimpleNamespace(
        events=[{"seq": 1, "type": "build.started"}],
        seq=1,
        record=SimpleNamespace(status="running"),
    )

    async def never(ping: float | None) -> None:
        raise AssertionError("waited although an event was there")

    live.wait = never
    service = SimpleNamespace(
        get=lambda tenant, build_id: live.record,
        _live={("t", "b"): live},
        _key=lambda tenant, build_id: (tenant, build_id),
    )
    seen = []
    async for event in BuildService.events(service, "t", "b", 0, 15.0):  # type: ignore[arg-type]
        seen.append(event["type"])
        if event["seq"] == 1:  # the build ends while the follower handles its first event
            live.events.append({"seq": 2, "type": "build.completed"})
            live.seq, live.record.status = 2, "completed"

    assert seen == ["build.started", "build.completed"]


@pytest.mark.parametrize(
    "name", ["Config/People.yaml", ".VENV/lib/x.py", "__PYCACHE__/x.pyc", ".ENV", "Cert.PEM"]
)
def test_hidden_files_are_found_whatever_their_case(name: str) -> None:
    from awa.server._builds import hidden

    assert hidden(Path(name))


def test_an_export_with_too_many_files_fails_instead_of_being_cut_short(tmp_path: Path) -> None:
    from awa.data import TooManyFilesError
    from awa.server._builds import export_files

    for number in range(2001):
        (tmp_path / f"{number}.txt").write_bytes(b"")

    with pytest.raises(TooManyFilesError, match="more than 2000 files"):
        export_files(tmp_path)
    (tmp_path / "0.txt").unlink()
    assert len(export_files(tmp_path)) == 2000


async def test_a_build_with_many_files_completes_but_its_export_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    def many_files(spec: Any) -> BuildOutcome:
        spec.out_dir.mkdir(parents=True, exist_ok=True)
        for number in range(2001):
            (spec.out_dir / f"{number}.txt").write_bytes(b"")
        return GOOD

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    async with client_for(make_app(engine=ScriptedBuildEngine(many_files))) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        status = await wait_for(client, build_id, "completed")
        archive = await client.get(f"/v1/builds/{build_id}/project.zip", headers=H)

    assert len(status["report"]["files"]) > 2000
    assert archive.status_code == 413
    assert list(tmp_path.glob("awa-project-*.zip")) == []
