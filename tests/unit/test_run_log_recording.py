"""A process run in the run log, and the run-status readers for a rejection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from _helpers.automation import ScriptedSystem
from awa.agents import Agent
from awa.automation import Automation, ProcessEvent, ProcessRun, RetryPolicy, SqliteRunStore
from awa.contracts import StepEvent
from awa.contracts.usage import ModelPrice, PriceTable
from awa.errors import StepCheckFailedError, StepRejectedError
from awa.persistence import Scope
from awa.persistence.runlog import PseudonymKey, SqliteDatabase, SqliteRunLog
from awa.server._runs import RunHandle
from awa.server.jobs import JobRegistry, JobSpec, execute
from awa.testing import FakeChatModel, structured_output, tool_call
from awa.testing.doubles import FakeClock
from awa.tools import tool
from awa.workflows import ApprovalCheck, ProcessStep, ProcessWorkflow, RuleCheck, StepRetry

PROMPT = "PROMPT-SENTINEL-7f3a"
ANSWER = "ANSWER-SENTINEL-91c2"
TOOL_BODY = "TOOL-SENTINEL-55d0"
KEY = PseudonymKey(b"test-pseudonym-key-of-32-bytes!!")


class Invoice(BaseModel):
    supplier: str
    total: float


@tool
def lookup(supplier: str) -> str:
    """Look up a supplier."""

    return f"{supplier} is known. {TOOL_BODY}"


def invoice_process() -> ProcessWorkflow:
    extractor = Agent(
        "extractor",
        description="Reads invoices.",
        model=FakeChatModel(
            [
                tool_call("lookup", {"supplier": "ACME"}),
                structured_output(Invoice, {"supplier": "ACME", "total": 0}),
                structured_output(Invoice, {"supplier": "ACME", "total": 6200.0}),
            ]
        ),
        tools=[lookup],
        output_type=Invoice,
    )
    booker = Agent("booker", description="Books.", model=FakeChatModel([ANSWER]))
    return ProcessWorkflow(
        "invoice_check",
        [
            ProcessStep(
                "extract",
                extractor,
                title="Read the invoice",
                checks=(
                    RuleCheck("extract.rule.1", "total <= 0", "No total found."),
                    ApprovalCheck("extract.approval.1", when="total >= 5000", reason="Large."),
                ),
                retry=StepRetry(max_attempts=2, initial=0.0),
            ),
            ProcessStep("book", booker),
        ],
        checkpointer=InMemorySaver(),
        sleep=_no_wait,
    )


async def _no_wait(seconds: float) -> None:
    return None


def automation(path: Path, *, capture: bool) -> tuple[Automation, SqliteRunLog]:
    database = SqliteDatabase(path)
    log = SqliteRunLog(database, pseudonym=KEY)
    auto = Automation(
        invoice_process(),
        name="invoices",
        input="{payload.text}",
        store=SqliteRunStore(database),
        run_log=log,
        capture=capture,
        prices=PriceTable(prices={}, default=ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0)),
        trace="off",
        clock=FakeClock(),
    )
    return auto, log


async def run_to_end(auto: Automation) -> Any:
    await auto.submit({"text": f"Check this invoice. {PROMPT}"}, actor="ana", tenant="acme")
    [paused] = await auto.run_pending()
    assert paused.status == "waiting"
    await auto.decide(
        paused.id, {"action": "approve", "note": "Checked with purchasing"}, actor="rita"
    )
    [done] = await auto.run_pending()
    assert done.status == "succeeded", done.error
    return done


async def test_a_run_with_a_retry_and_an_approval_is_stored_in_order(tmp_path: Path) -> None:
    auto, log = automation(tmp_path / "awa.db", capture=False)
    run = await run_to_end(auto)
    scope = Scope("acme", "invoices")

    steps = await log.steps(scope, run.id)
    attempts = await log.attempts(scope, run.id)
    results = await log.results(scope, run.id)
    [approval] = await log.approvals(scope, run.id)

    assert [(s.step_key, s.ordinal, s.status, s.attempts) for s in steps] == [
        ("extract", 0, "checked", 2),
        ("book", 1, "checked", 1),
    ]
    assert steps[0].title == "Read the invoice" and steps[0].agent == "extractor"
    assert [(a.step_key, a.attempt, a.status) for a in attempts] == [
        ("extract", 1, "failed"),
        ("extract", 2, "succeeded"),
        ("book", 1, "succeeded"),
    ]
    assert attempts[0].retry_reason and "No total found." in attempts[0].retry_reason
    assert all(a.agent_run_id for a in attempts)
    assert [(r.step_key, r.attempt, r.kind) for r in results] == [
        ("extract", 1, "intermediate"),
        ("extract", 2, "final"),
        ("book", 1, "final"),
    ]
    assert results[0].output == {"supplier": "ACME", "total": 0}
    assert results[1].output == {"supplier": "ACME", "total": 6200.0}
    failed = [c for c in results[0].checks if not c.passed]
    assert [c.id for c in failed] == ["extract.rule.1"]
    [call] = results[0].tool_calls
    assert (call.name, call.status, call.arguments_ref, call.result_ref) == (
        "lookup",
        "ok",
        None,
        None,
    )
    assert call.result_length > 0
    assert results[2].answer_length == len(ANSWER) and results[2].answer_ref is None
    assert (approval.step_key, approval.result_id) == ("extract", results[1].result_id)
    assert (approval.decision, approval.reviewer, approval.reason) == (
        "approved",
        "rita",
        "Checked with purchasing",
    )
    for step in steps:
        assert step.input_tokens == sum(
            a.input_tokens for a in attempts if a.step_key == step.step_key
        )
        assert step.cost_micros is not None
    assert await log.subject_runs(scope, "ana") == [run.id]
    assert await log.subject_runs(scope, "rita") == [run.id]
    assert await log.steps(Scope("globex", "invoices"), run.id) == []


async def test_with_capture_off_prompts_and_answers_are_stored_nowhere(tmp_path: Path) -> None:
    auto, log = automation(tmp_path / "awa.db", capture=False)
    run = await run_to_end(auto)
    scope = Scope("acme", "invoices")
    assert await log.results(scope, run.id)  # the steps were recorded

    # The run record itself keeps the run's input and final output (to retry it and to return
    # its result); with it deleted, nothing else in the file may hold a prompt or an answer.
    assert await auto.store.delete([run.id]) == 1  # type: ignore[attr-defined]
    auto.store.database.compact()  # type: ignore[attr-defined]

    data = b"".join(path.read_bytes() for path in tmp_path.glob("awa.db*") if path.is_file())
    for sentinel in (PROMPT, ANSWER, TOOL_BODY):
        assert sentinel.encode() not in data, sentinel
    assert await log.steps(scope, run.id)  # the run log is still there, without content
    await log.close()


async def test_with_capture_on_bodies_are_kept_for_auditors(tmp_path: Path) -> None:
    auto, log = automation(tmp_path / "awa.db", capture=True)
    run = await run_to_end(auto)
    auditor = Scope("acme", "invoices", principal="audrey", roles=frozenset({"auditor"}))

    results = await log.results(auditor, run.id)
    answer = await log.content(auditor, results[-1].answer_ref or "")
    [call] = results[0].tool_calls
    tool_result = await log.content(auditor, call.result_ref or "")

    assert answer is not None and answer.body == ANSWER
    assert tool_result is not None and TOOL_BODY in tool_result.body
    with pytest.raises(Exception, match="auditor"):
        await log.content(Scope("acme", "invoices"), results[-1].answer_ref or "")


# --------------------------------------------------------------------------- reading back


async def test_awa_runs_show_steps_prints_the_ordered_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from awa.cli import main

    path = tmp_path / ".awa" / "awa.db"
    auto, log = automation(path, capture=False)
    run = await run_to_end(auto)
    await log.close()
    capsys.readouterr()

    import asyncio

    code = await asyncio.to_thread(main, ["runs", "show", run.id, "--steps", "--store", str(path)])

    shown = json.loads(capsys.readouterr().out)
    assert code == 0 and shown["steps_recorded"] is True
    assert [s["step_key"] for s in shown["steps"]] == ["extract", "book"]
    extract = shown["steps"][0]
    assert [a["attempt"] for a in extract["attempts"]] == [1, 2]
    assert [r["kind"] for a in extract["attempts"] for r in a["results"]] == [
        "intermediate",
        "final",
    ]
    assert extract["approvals"][0]["reviewer"] == "rita"
    assert extract["approvals"][0]["reason"] == "Checked with purchasing"
    assert extract["rule_id"] is None or isinstance(extract["rule_id"], str)


async def test_the_api_returns_the_steps_of_the_callers_runs_only(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    import hashlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from awa.server import create_automation_router
    from awa.server._auth import ApiKeyAuthenticator, Principal

    auto, log = automation(tmp_path / "awa.db", capture=False)
    run = await run_to_end(auto)
    keys = {
        hashlib.sha256(b"acme-key").hexdigest(): Principal("ana", tenant="acme"),
        hashlib.sha256(b"globex-key").hexdigest(): Principal("gus", tenant="globex"),
    }
    app = FastAPI()
    app.include_router(
        create_automation_router([auto], authenticator=ApiKeyAuthenticator.from_digests(keys))
    )

    import asyncio

    def calls() -> tuple[Any, Any]:
        with TestClient(app) as client:
            mine = client.get(
                f"/v1/process-runs/{run.id}/steps", headers={"Authorization": "Bearer acme-key"}
            )
            theirs = client.get(
                f"/v1/process-runs/{run.id}/steps", headers={"Authorization": "Bearer globex-key"}
            )
            return mine, theirs

    mine, theirs = await asyncio.to_thread(calls)

    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["recorded"] is True
    assert [s["step_key"] for s in body["steps"]] == ["extract", "book"]
    assert body["steps"][0]["attempts"][1]["results"][0]["output"]["total"] == 6200.0
    assert theirs.status_code == 404
    await log.close()


async def test_deleting_more_runs_than_sqlite_takes_parameters_works(tmp_path: Path) -> None:
    auto, log = automation(tmp_path / "awa.db", capture=False)
    run = await run_to_end(auto)
    scope = Scope("acme", "invoices")
    many = [f"missing_{i}" for i in range(40_000)] + [run.id]

    assert await log.delete_runs(scope, many) == 1
    assert await auto.store.delete(many) == 1  # type: ignore[attr-defined]
    await log.close()


def rejection() -> StepRejectedError:
    return StepRejectedError(
        "Step 'decide' rejected the item: Blocked supplier.",
        step="decide",
        steps=[{"name": "decide", "agent": "clerk", "status": "rejected"}],
        details={"rejected": True, "case": "decide.case.1"},
    )


def test_a_rejection_is_a_step_check_failure_with_its_own_code() -> None:
    error = rejection()
    assert isinstance(error, StepCheckFailedError)
    assert error.code == "awa.step_rejected" and error.details["rejected"] is True


async def test_an_automation_run_that_is_rejected_ends_rejected_and_is_never_retried(
    tmp_path: Path,
) -> None:
    events: list[ProcessEvent] = []
    system = ScriptedSystem(errors=[rejection(), rejection()])
    store = SqliteRunStore(tmp_path / "awa.db")
    auto = Automation(
        system,
        store=store,
        clock=FakeClock(),
        retry=RetryPolicy(max_attempts=3, initial=0),
        subscribers=[events.append],
    )
    await auto.submit({"id": 1})

    [run] = await auto.run_pending()

    assert run.status == "rejected" and run.is_finished and len(system.calls) == 1
    assert run.error_code == "awa.step_rejected"
    rejected = [e for e in events if e.type == "process.rejected"]
    assert rejected and rejected[0].data["case"] == "decide.case.1"
    await store.close()

    reopened = SqliteRunStore(tmp_path / "awa.db")
    [loaded] = await reopened.list(status="rejected")
    assert loaded.id == run.id and loaded.status == "rejected"
    await reopened.close()


def test_a_served_run_that_raises_a_rejection_has_status_rejected() -> None:
    handle = RunHandle(run_id="r", system="s", mode="sync", tenant=None, principal="p")
    assert handle.outcome(rejection()) == "rejected"
    assert handle.outcome(StepCheckFailedError("x")) == "failed"


class Rejecting(ScriptedSystem):
    async def run(self, input: Any, **kwargs: Any) -> Any:
        raise rejection()


async def test_a_job_whose_run_is_rejected_reports_rejected_and_is_not_retryable() -> None:
    result = await execute(JobSpec(system="desk", input="x"), JobRegistry({"desk": Rejecting()}))

    assert result.status == "rejected" and not result.ok
    assert result.error is not None and result.error["code"] == "awa.step_rejected"
    assert result.retryable is False


# --------------------------------------------------------------------------- the web app


def test_the_inbox_and_the_run_words_load_runs_with_the_new_statuses(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from awa.ui._operate import status_words
    from awa.ui._project import Project

    folder = tmp_path / "proj"
    (folder / ".awa").mkdir(parents=True)
    (folder / "blueprint.yaml").write_text(
        "name: desk\nagents:\n  - {name: clerk, description: Decides.}\n", encoding="utf-8"
    )
    runs = [
        ProcessRun(automation="desk", status="rejected", error="StepRejectedError: Blocked."),
        ProcessRun(automation="desk", status="succeeded", output="done"),
    ]
    import asyncio

    async def seed() -> None:
        store = SqliteRunStore(folder / ".awa" / "awa.db")
        for run in runs:
            await store.create(run)
        await store.close()

    asyncio.run(seed())

    project = Project(folder)
    assert asyncio.run(project.approvals()) == []
    assert status_words("rejected") == ("Turned down by a special case or a person", "warning")


def test_the_try_it_narrator_words_retries_skips_and_rejections() -> None:
    pytest.importorskip("fastapi")
    from awa.ui._try import Narrator

    class Plain:
        def redacted(self, text: str) -> str:
            return text

    narrator = Narrator.__new__(Narrator)
    narrator._project = Plain()  # type: ignore[attr-defined]
    common: dict[str, Any] = {
        "step": "extract",
        "index": 0,
        "total": 2,
        "agent": "reader",
        "run_id": "r",
        "root_run_id": "r",
    }
    retrying = narrator._step(
        StepEvent(status="retrying", attempt=1, message="Try 1 of 3 failed: No total.", **common)
    )
    skipped = narrator._step(StepEvent(status="skipped", message="Skipped: x is false.", **common))
    assert b"is tried again (try 1): Try 1 of 3 failed: No total." in retrying
    assert b"was skipped: Skipped: x is false." in skipped


def test_the_trace_records_and_shows_the_try_and_the_case(tmp_path: Path) -> None:
    from awa.contracts.events import AgentStartedEvent
    from awa.observability import TraceRecorder, read_trace, render_trace

    path = tmp_path / "t.jsonl"
    ids = {"run_id": "run_1", "root_run_id": "run_1", "agent": "desk"}
    recorder = TraceRecorder(path, level="summary")
    for event in (
        AgentStartedEvent(kind="workflow", **ids),
        StepEvent(step="extract", index=0, total=2, status="retrying", attempt=1, **ids),
        StepEvent(step="extract", index=0, total=2, status="checked", attempt=2, **ids),
        StepEvent(
            step="decide",
            index=1,
            total=2,
            status="rejected",
            case="decide.case.1",
            message="Blocked supplier.",
            **ids,
        ),
    ):
        recorder(event)
    recorder.close("failed")

    steps = [e for e in read_trace(path).events if e["type"] == "step"]
    assert [(e["status"], e.get("attempt"), e.get("case")) for e in steps] == [
        ("retrying", 1, None),
        ("checked", 2, None),
        ("rejected", None, "decide.case.1"),
    ]
    text = render_trace(path)
    assert "step 1/2 extract (desk): checked (try 2)" in text
    assert "step 2/2 decide (desk): rejected [decide.case.1]" in text
    assert "Blocked supplier." in text
