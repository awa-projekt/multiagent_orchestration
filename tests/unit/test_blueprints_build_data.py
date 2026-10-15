"""build_project on the user's real files: copies, evaluation, questions, events, the written project."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from _helpers.build_scripts import (
    ACCEPTANCE,
    BLUEPRINT,
    INVENTED,
    ORDERS_CSV,
    PAYMENTS_CSV,
    PLAIN_BLUEPRINT,
    PROCESS,
    Script,
    with_changes,
    with_outbox,
)
from awa.blueprints import (
    Blueprint,
    BuildProgress,
    ProjectDesign,
    build_project,
    design_project,
    evaluate_project,
    prepare_data,
)
from awa.blueprints._data_project import check_project
from awa.contracts.usage import UsageLimits
from awa.data import DataError, DataProfile
from awa.errors import ScaffoldError

CV_PROCESS = "Screen the incoming CVs against our role profile and schedule interviews."


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def build(script: Script, out: Path, **options: Any) -> Any:
    return await build_project(
        options.pop("process", PROCESS),
        out_dir=out,
        model=script.model(),
        environment=None,
        data=options.pop("data", [ORDERS_CSV]),
        fit=options.pop("fit", "rules"),
        **options,
    )


async def test_a_build_on_real_files_writes_the_copies_the_profile_and_a_checked_data_section(
    tmp_path: Path,
) -> None:
    before = (sha(ORDERS_CSV), ORDERS_CSV.stat().st_mtime_ns)
    out = tmp_path / "project"

    report = await build(Script(), out)

    assert (report.passed, report.rounds, report.stopped) == (True, 1, "target_reached")
    assert (sha(ORDERS_CSV), ORDERS_CSV.stat().st_mtime_ns) == before  # originals untouched
    assert sha(out / "data/files/orders.csv") == before[0]  # the copy is byte-identical
    assert (out / "data/output").is_dir() and not list((out / "data/output").iterdir())
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    # no invented system, no sample record
    assert [p for p in written if p.startswith("data/")] == [
        "data/PROFILE.json",
        "data/PROFILE.md",
        "data/files/orders.csv",
    ]
    profile = DataProfile.from_json((out / "data/PROFILE.json").read_text(encoding="utf-8"))
    assert profile.files[0].path == "orders.csv"
    assert profile.files[0].source == str(ORDERS_CSV)  # where it came from, to detect drift
    blueprint = Blueprint.from_yaml_file(out / "blueprint.yaml")
    assert [d.path for d in blueprint.data] == ["data/files/orders.csv"]
    assert not [i for i in blueprint.check() if i.severity == "error"]
    assert not [i for i in check_project(blueprint) if i.severity == "error"]  # `awa validate`
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "## Your data" in readme and "`orders.csv` (csv, 8 rows; ok)" in readme
    assert "decimal comma" in readme and "awa data run . --all" in readme
    assert "invented" not in readme


async def test_a_change_to_the_copy_after_the_build_is_found_by_the_data_check(
    tmp_path: Path,
) -> None:
    out = tmp_path / "project"
    await build(Script(), out)
    copy = out / "data/files/orders.csv"
    text = copy.read_bytes().decode("cp1252").replace("Betrag_EUR", "Preis_EUR")
    copy.write_bytes(text.encode("cp1252"))

    issues = check_project(Blueprint.from_yaml_file(out / "blueprint.yaml"))

    assert any("Betrag_EUR" in i.message and i.severity == "error" for i in issues)


async def test_a_rebuild_replaces_the_copies_of_the_files(tmp_path: Path) -> None:
    out = tmp_path / "project"
    await build(Script(), out)
    stale = out / "data/files/old.csv"
    stale.write_text("a;b\n1;2\n", encoding="utf-8")
    result = out / "data/output/earlier"
    result.mkdir()

    await build(Script(), out, force=True)

    assert not stale.exists() and result.is_dir()  # results are never deleted by a rebuild


async def test_every_evaluation_run_gets_its_own_copy_and_the_staged_files_stay_read_only(
    tmp_path: Path,
) -> None:
    script = Script()
    project = await design_project(
        PROCESS, model=script.model(), environment=None, data=[ORDERS_CSV]
    )
    assert project.data is not None
    first, second = tmp_path / "one", tmp_path / "two"

    project.data.materialize(first)
    project.data.materialize(second)
    (first / "output" / "result.txt").write_text("run one", encoding="utf-8")

    assert not list((second / "output").iterdir())  # one run never sees another's results
    assert (first / "files/orders.csv").read_bytes() == ORDERS_CSV.read_bytes()
    with pytest.raises(PermissionError):  # a tool cannot change a file through a shared link
        (first / "files/orders.csv").open("ab").close()
    assert sha(second / "files/orders.csv") == sha(ORDERS_CSV)


async def test_a_write_to_the_output_folder_waits_for_a_person_and_never_touches_the_inputs() -> (
    None
):
    writer = with_changes(BLUEPRINT)
    writer["agents"][0]["tools"] = ["connector:files.query_csv", "connector:output.write_file"]
    acceptance = with_changes(ACCEPTANCE, rules=[])
    acceptance["cases"] = [
        {
            "id": "save-report",
            "input": "Write the report of orders.csv to report.txt.",
            "files": ["orders.csv"],
            "expectations": [
                {"kind": "approval", "value": "output_write_file"},
                {"kind": "tool_called", "value": "output_write_file"},
            ],
            "decisions": [{"action": "approve", "tool": "output_write_file"}],
        }
    ]

    def agent(call: Any) -> Any:
        from langchain_core.messages import ToolMessage

        from awa.testing import tool_call

        if any(isinstance(m, ToolMessage) for m in call.messages):
            return "Saved."
        return tool_call("output_write_file", {"path": "report.txt", "content": "ok"})

    script = Script(acceptances=[acceptance], blueprints=[writer], agent=agent)
    project = await design_project(
        PROCESS, model=script.model(), environment=None, data=[ORDERS_CSV]
    )

    report = await evaluate_project(project, model=script.model())

    assert [a.tool for a in project.blueprint.approvals] == ["connector:output.write_file"]
    assert report.runs[0].status == "passed" and report.runs[0].approvals == ["output_write_file"]
    assert [e.role for e in project.blueprint.data] == ["input", "output"]
    assert project.blueprint.connectors["output"]["root"] == "data/output"
    staged = project.data.files.root / "files" / "orders.csv"  # type: ignore[attr-defined,union-attr]
    assert sha(staged) == sha(ORDERS_CSV)


async def test_progress_events_come_in_order_and_end_with_one_terminal_event(
    tmp_path: Path,
) -> None:
    steps: list[BuildProgress] = []
    threads: set[int] = set()

    def record(step: BuildProgress) -> None:
        threads.add(threading.get_ident())
        steps.append(step)

    report = await build(Script(), tmp_path / "p", on_progress=record, refine=False)

    events = [s.event for s in steps if s.event]
    assert events == [
        "build.started",
        "data.profile.file",
        "data.profile.done",
        "design.acceptance",
        "design.architecture",
        "eval.round.started",
        "eval.case",
        "eval.case",
        "eval.round.finished",
        "write.done",
        "build.completed",
    ]
    assert threads == {threading.get_ident()}  # the profile runs in a thread, events do not
    first, done = steps[1], steps[2]
    assert first.stage == "profile" and first.data["name"] == "orders.csv"
    assert (first.data["kind"], first.data["status"]) == ("csv", "ok")
    assert first.data["flags"] >= 1  # the decisions the profile took (cp1252, decimal comma)
    assert done.data["files"] == 1 and done.data["rows"] == 8
    assert "usage" not in done.data  # no model call yet
    assert steps[-1].data["report"] == report.to_dict()
    assert steps[-1].data["usage"]["requests"] == report.usage.requests
    json.dumps([s.data for s in steps], default=str)  # plain data a UI can send


async def test_the_result_object_is_stable_json_for_a_ui(tmp_path: Path) -> None:
    report = await build(Script(), tmp_path / "p")

    data = report.to_dict()

    assert json.loads(json.dumps(data)) == data
    assert (
        data["status"] == "completed"
        and data["passed"] is True
        and data["name"] == "order_screening"
    )
    assert data["files"] == [{"path": "orders.csv", "kind": "csv", "status": "ok"}]
    assert [c["id"] for c in data["cases"]] == ["big-order", "open-total"]
    assert all(c["status"] == "passed" and c["tool_calls"] >= 1 for c in data["cases"])
    assert data["usage"]["requests"] == report.usage.requests
    assert (
        data["question"] is None and data["failing"] == [] and data["stopped"] == "target_reached"
    )
    assert set(data) >= {
        "summary", "pass_rate", "rounds", "directory", "flaky", "stage_requests", "connections"
    }  # fmt: skip


async def test_a_description_that_does_not_fit_the_files_asks_before_any_design(
    tmp_path: Path,
) -> None:
    script = Script()
    steps: list[BuildProgress] = []
    out = tmp_path / "never"

    report = await build(
        script, out, process=CV_PROCESS, data=[PAYMENTS_CSV], on_progress=steps.append
    )

    assert (report.stopped, report.passed) == ("question", False)
    assert report.design is None and report.eval_report is None
    assert report.usage.requests == 0 and script.calls("designer") == 0  # no model call at all
    assert not out.exists()  # nothing written
    question = report.question
    assert question is not None and question.status == "question"
    assert question.questions[0].id == "q1"
    assert "CVs or candidates" in question.questions[0].text
    assert any(e.startswith("f1") for e in question.questions[0].evidence)  # cites the profile
    assert steps[-1].stage == "question" and steps[-1].event == "build.question"
    assert [s.event for s in steps if s.event.startswith("build.")] == [
        "build.started",
        "build.question",
    ]
    data = report.to_dict()
    assert data["status"] == "needs_input" and data["question"]["questions"][0]["id"] == "q1"
    assert "Before anything is designed" in report.summary() and "q1:" in report.summary()


async def test_the_answers_resume_the_build_as_the_users_words_never_as_file_text(
    tmp_path: Path,
) -> None:
    script = Script()
    answer = "Use these payments to chase customers who paid too little."

    report = await build(
        script,
        tmp_path / "p",
        process=CV_PROCESS,
        data=[ORDERS_CSV, PAYMENTS_CSV],
        answers={"q1": answer, "q2": "   "},
        fit="rules",
    )

    assert report.question is None and report.design is not None
    for role in ("designer", "architect"):
        brief = script.prompts[role][0]
        assert "## The user's answers to the open questions" in brief
        assert f"- q1: {answer}" in brief and "q2" not in brief  # blank answers are dropped
        assert "a file never answers" in brief
    assert report.design.guidance.count("q1:") == 1  # kept for the repairs too


async def test_an_answer_never_lifts_a_refusal_for_unreadable_files(tmp_path: Path) -> None:
    picture = tmp_path / "scan.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    script = Script()

    report = await build(
        script, tmp_path / "p", data=[picture], answers={"q1": "just do it"}, fit="rules"
    )

    assert report.question is not None and report.question.status == "blocked"
    assert report.stopped == "question" and script.calls("designer") == 0
    assert "Nothing can be built from these files" in report.summary()
    assert "Answer them" not in report.summary()  # there is nothing to answer


async def test_the_fit_model_is_asked_only_where_the_rules_found_something_doubtful(
    tmp_path: Path,
) -> None:
    script = Script()  # `fit=None`: the model must not be asked for a description that fits

    report = await build(script, tmp_path / "p", fit="model")

    assert report.question is None and script.calls("fit") == 0


async def test_a_doubtful_description_asks_the_model_once_and_keeps_only_questions_with_evidence(
    tmp_path: Path,
) -> None:
    verdict = {
        "verdict": "question",
        "reason": "the description shares no word with the data",
        "questions": [
            {"id": "x", "text": "Are these the ledger lines?", "evidence": ["f1.t1.betrag_eur"]},
            {"id": "y", "text": "Is there another file?", "evidence": ["f9.t9"]},
        ],
    }
    script = Script(fit=verdict)

    report = await build(script, tmp_path / "p", process="Post the nightly journal.", fit="model")

    assert script.calls("fit") == 1 and script.calls("designer") == 0
    assert report.question is not None
    assert [q.text for q in report.question.questions] == ["Are these the ledger lines?"]
    assert report.question.questions[0].id == "q1"  # numbered after the gate
    assert report.usage.requests == 1  # exactly the one fit call


async def test_a_ready_made_fit_judge_replaces_the_model(tmp_path: Path) -> None:
    from awa.blueprints._fit import FitVerdict

    asked: list[str] = []

    async def judge(description: str, digest: str, findings: Any) -> FitVerdict:
        asked.append(description)
        return FitVerdict(verdict="ok")

    script = Script()

    await build(script, tmp_path / "p", process="Post the nightly journal.", fit=judge)

    assert asked == ["Post the nightly journal."] and script.calls("fit") == 0


async def test_the_mask_decides_what_the_designers_see_of_the_data(tmp_path: Path) -> None:
    shown, hidden = Script(), Script()

    await build(shown, tmp_path / "a", mask="none")
    await build(hidden, tmp_path / "b", mask="drop")

    assert "Kowalski Stahl" in shown.prompts["designer"][0]
    assert "Kowalski Stahl" not in hidden.prompts["designer"][0]
    assert "Betrag_EUR" in hidden.prompts["designer"][0]  # structure stays


async def test_a_file_that_cannot_be_taken_stops_the_build_before_any_model_call(
    tmp_path: Path,
) -> None:
    script = Script()

    with pytest.raises(DataError):
        await build(script, tmp_path / "p", data=[tmp_path / "missing.csv"])

    assert script.calls("designer") == 0 and not (tmp_path / "p").exists()


async def test_data_and_a_finished_design_cannot_be_combined(tmp_path: Path) -> None:
    script = Script(acceptances=[INVENTED], blueprints=[PLAIN_BLUEPRINT])
    design = await design_project(PROCESS, model=script.model(), environment=None)

    with pytest.raises(ValueError, match="belong to a description"):
        await build_project(design, out_dir=tmp_path / "p", data=[ORDERS_CSV])
    assert isinstance(design, ProjectDesign)


async def test_an_existing_project_is_refused_before_the_files_are_read(tmp_path: Path) -> None:
    (tmp_path / "blueprint.yaml").write_text("x", encoding="utf-8")
    script = Script()

    with pytest.raises(ScaffoldError):
        await build(script, tmp_path)

    assert script.calls("designer") == 0


async def test_the_usage_limit_covers_the_fit_check_and_the_design(tmp_path: Path) -> None:
    report = await build(
        Script(), tmp_path / "p", usage_limits=UsageLimits(max_requests=40), fit="rules"
    )

    assert report.usage.requests == sum(report.stage_requests.values())


async def test_the_staging_folder_is_removed_when_the_design_is_gone() -> None:
    import gc

    data = prepare_data(ORDERS_CSV)
    staging = data.files.root  # type: ignore[attr-defined]
    assert staging.is_dir()

    del data
    gc.collect()
    await asyncio.sleep(0)

    assert not staging.exists()


async def test_a_stager_of_your_own_replaces_the_local_file_system(tmp_path: Path) -> None:
    from _helpers.build_scripts import BytesStaged
    from awa.blueprints import ProjectData
    from awa.data import profile

    staged = BytesStaged({"orders.csv": ORDERS_CSV.read_bytes()})
    asked: list[tuple[Any, ...]] = []

    def stager(paths: Any, *, notes: Any, mask: Any, limits: Any, on_progress: Any) -> ProjectData:
        asked.append((list(paths), dict(notes), mask))
        keep = tmp_path / "profiled"
        keep.mkdir()
        (keep / "orders.csv").write_bytes(ORDERS_CSV.read_bytes())
        return ProjectData(
            profile=profile(keep, root=keep),
            files=staged,
            notes=dict(notes),
            originals={"orders.csv": "upload://42"},
            mask=mask,
        )

    out = tmp_path / "project"
    script = Script()

    report = await build(
        script, out, data=["upload://42"], stager=stager, notes={"orders.csv": "Flag the big ones."}
    )

    assert report.passed and asked == [
        (["upload://42"], {"orders.csv": "Flag the big ones."}, "none")
    ]
    assert ("data", False, True) in staged.calls  # written into the project: copies, replacing
    assert any(share for _, share, _ in staged.calls)  # evaluated on shared copies
    assert (out / "data/files/orders.csv").read_bytes() == ORDERS_CSV.read_bytes()
    recorded = DataProfile.from_json((out / "data/PROFILE.json").read_text(encoding="utf-8"))
    assert recorded.files[0].source == "upload://42"


async def test_an_outbox_the_design_adds_is_created_in_the_output_folder_not_next_to_the_data(
    tmp_path: Path,
) -> None:
    """The outbox database the design adds is created in data/output, never next to the user's
    copies."""

    out = tmp_path / "project"

    report = await build(with_outbox(), out)

    assert report.passed, report.summary()
    written = sorted(p.relative_to(out / "data").as_posix() for p in (out / "data").rglob("*"))
    assert written == ["PROFILE.json", "PROFILE.md", "files", "files/orders.csv", "output"]
    blueprint = Blueprint.from_yaml_file(out / "blueprint.yaml")
    outbox = blueprint.connectors["outbound_email"]
    assert (outbox["root"], outbox["database"]) == ("data/output", "outbound_email.db")
    assert outbox["schema"] == ['CREATE TABLE "outgoing_messages" ("recipient" TEXT, "body" TEXT)']
    assert [a.tool for a in blueprint.approvals] == ["connector:outbound_email.execute"]
    assert not [i for i in blueprint.check() if i.severity == "error"]
