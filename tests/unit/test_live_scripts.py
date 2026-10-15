"""The live acceptance scripts `scripts/live_data_first.py` and `scripts/live_microsoft.py`, offline."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from _helpers.data_scenarios import checks, generate
from _helpers.data_scenarios.scenarios import SCENARIOS
from _helpers.fake_secrets import fake_secret
from _helpers.microsoft_world import microsoft_world
from awa.contracts import ModelResponseEvent
from awa.contracts.events import ToolCallEvent, ToolResultEvent, rejection_message
from awa.models import ModelRegistry

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_data_first.py"
SECRET = fake_secret("sk-", "test-0123456789-not-a-real-key")
ENDPOINT = "https://example-resource.openai.azure.com/"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_data_first_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return load_script()


@pytest.fixture(scope="module")
def dry_run(script: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("dry")
    code = script.main(["--dry-run", "--out", str(out)])
    return {
        "code": code,
        "out": out,
        "report": json.loads((out / "report.json").read_text(encoding="utf-8")),
        "markdown": (out / "report.md").read_text(encoding="utf-8"),
    }


def event(**more: object) -> ModelResponseEvent:
    return ModelResponseEvent(run_id="r", root_run_id="r", agent="a", **more)  # type: ignore[arg-type]


def clean_environment(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """No variable the script reads or sets exists (and none stays after the test)."""

    roles = [f"AWA_{role}_MODEL" for role in ("DEFAULT", "FAST", "STRONG", "JUDGE")]
    for name in (
        *script.LOADED,
        *script.FOREIGN,
        *roles,
        "AWA_CHECKPOINTER_BACKEND",
        "PYTHONIOENCODING",
    ):
        monkeypatch.setenv(name, "")  # registers the undo: the name is absent again afterwards
        monkeypatch.delenv(name)


def checks_of(report: dict[str, Any], scenario: str) -> dict[str, bool]:
    found = next(s for s in report["scenarios"] if s["id"] == scenario)
    return {c["id"]: c["ok"] for c in found["checks"]}


# ------------------------------------------------------------------------ the dry run


@pytest.mark.slow
def test_the_dry_run_passes_all_eight_scenarios(dry_run: dict[str, object]) -> None:
    report = dry_run["report"]
    assert dry_run["code"] == 0
    assert [s["id"] for s in report["scenarios"]] == [f"s{n}" for n in range(1, 9)]
    assert report["summary"] == {"pass": 8, "fail": 0, "error": 0, "skip": 0}
    assert report["mode"].startswith("dry run")
    for scenario in report["scenarios"]:
        # a soft check only warns
        failed = [c["id"] for c in scenario["checks"] if not c["ok"] and not c.get("soft")]
        assert scenario["status"] == "pass" and not failed, (scenario["id"], failed)


def test_every_scenario_runs_the_common_checks_and_its_own(dry_run: dict[str, object]) -> None:
    report = dry_run["report"]
    for number in range(1, 9):
        found = checks_of(report, f"s{number}")
        assert {
            "profile.facts",
            "C1.originals_after_profile",
            "C2.profile",
            "C3.model_view",
        } <= set(found)
    for number in (1, 2, 4, 5):  # the full builds
        found = checks_of(report, f"s{number}")
        assert {"C3.no_call_before_profile", "C4.events", "C5.validate", "C5.data_check"} <= set(
            found
        )
        assert {"C6.nothing_invented", "C7.grounded", "build.target"} <= set(found)
    own = {
        "s1": {"s1.approve_and_reject", "run.two_files", "run.inputs_unchanged"},
        "s2": {"s2.big_order_in_cases"},
        "s3": {"s3.scan_route_stated"},
        "s5": {"s5.attacker_never_addressed", "s5.sending_waits_for_approval"},
        "s7": {"s7.write_never_executed"},
        "s8": {"ask.exit_code_2", "ask.evidence", "ask.no_design", "ask.lone_image_blocked"},
    }
    for scenario, names in own.items():
        assert names <= set(checks_of(report, scenario)), scenario


def test_the_ledger_file_and_the_reports_agree_on_the_calls(dry_run: dict[str, object]) -> None:
    out, report = dry_run["out"], dry_run["report"]
    lines = [json.loads(x) for x in (out / "ledger.jsonl").read_text("utf-8").splitlines()]
    assert lines and lines[-1]["total"] == report["calls"]
    assert sum(s["calls"] for s in report["scenarios"]) == report["calls"]
    assert 0 < report["calls"] <= report["cap"] == 350
    ask = next(s for s in report["scenarios"] if s["id"] == "s8")
    assert ask["calls"] <= 1  # a question costs at most the fit check, never a design
    assert "| s1 invoice-intake | build | PASS |" in dry_run["markdown"]


def test_the_projects_of_the_dry_run_are_there_to_read(dry_run: dict[str, object]) -> None:
    out = dry_run["out"]
    assert (out / "s1" / "project" / "blueprint.yaml").is_file()
    assert (out / "s3" / "project" / "data" / "PROFILE.json").is_file()
    assert not (out / "s8" / "project" / "blueprint.yaml").exists()


# ------------------------------------------------------------------------ arguments


def test_only_runs_the_named_scenarios_and_an_unknown_name_is_a_usage_error(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["--dry-run", "--only", "s8", "--out", str(tmp_path / "a")]) == 0
    ran = json.loads((tmp_path / "a" / "report.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in ran["scenarios"]] == ["s8"]
    assert script.main(["--dry-run", "--only", "s9", "--out", str(tmp_path / "b")]) == 2
    assert "unknown scenario" in capsys.readouterr().err
    assert script.main(["--dry-run", "--cap", "0", "--out", str(tmp_path / "c")]) == 2


def test_a_cap_below_what_a_scenario_needs_skips_it_without_a_call(
    script: ModuleType, tmp_path: Path
) -> None:
    assert script.main(["--dry-run", "--only", "s2", "--cap", "5", "--out", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    [only] = report["scenarios"]
    assert only["status"] == "skip" and only["calls"] == 0 and "needs" in only["reason"]
    assert report["calls"] == 0


def test_the_plan_fits_the_default_cap_and_names_every_scenario(script: ModuleType) -> None:
    assert script.MOST_ASK_CALLS == 1  # spec 5, scenario 8: at most ONE model call
    assert sorted(script.PLANS) == list(range(1, 9))
    assert sum(p.nominal for p in script.PLANS.values()) <= 350


# ------------------------------------------------------------------------ limits


def test_the_profile_time_limit_binds_a_live_run_and_only_warns_in_a_dry_run(
    script: ModuleType,
) -> None:
    """The dry run is a unit test on a machine that other work shares: wall time there says
    nothing about the product. The live run (a machine of its own) keeps the limit."""

    assert script.PROFILE_SECONDS == 2.0
    assert script.profile_fast_enough(1.9, live=True) and script.profile_fast_enough(
        1.9, live=False
    )
    assert not script.profile_fast_enough(2.1, live=True)
    assert script.profile_fast_enough(60.0, live=False)


def test_the_ledger_counts_responses_and_cancels_at_one_and_a_half_times_the_nominal(
    script: ModuleType, tmp_path: Path
) -> None:
    async def scenario() -> tuple[int, str, bool]:
        ledger = script.Ledger(100, tmp_path / "ledger.jsonl")
        task = asyncio.ensure_future(asyncio.sleep(30))
        ledger.begin("s2", 4, task)  # nominal 4: cancelled when the 7th call comes
        for _ in range(6):
            ledger(event(input_tokens=10, output_tokens=5))
        await asyncio.sleep(0)
        assert not ledger.tripped and not task.cancelled() and ledger.scene_tokens == 90
        ledger(event())
        await asyncio.sleep(0.01)
        return ledger.scene, ledger.tripped, task.cancelled()

    calls, why, cancelled = asyncio.run(scenario())
    assert (calls, cancelled) == (7, True) and "s2" in why and "6 calls" in why


def test_the_ledger_cancels_when_the_total_passes_the_cap_and_ignores_muted_calls(
    script: ModuleType, tmp_path: Path
) -> None:
    async def scenario() -> tuple[int, bool, int]:
        ledger = script.Ledger(3, tmp_path / "ledger.jsonl")
        task = asyncio.ensure_future(asyncio.sleep(30))
        ledger.begin("s1", 100, task)
        with ledger.mute():
            ledger(event())  # a scripted offline check costs nothing
        for _ in range(3):
            ledger(event())
        await asyncio.sleep(0)
        assert ledger.remaining == 0 and not task.cancelled()
        ledger(event())
        await asyncio.sleep(0.01)
        return ledger.total, task.cancelled(), ledger.grant(10)

    total, cancelled, grant = asyncio.run(scenario())
    assert (total, cancelled, grant) == (4, True, 0)


def test_the_ledger_ignores_events_that_are_no_model_responses(
    script: ModuleType, tmp_path: Path
) -> None:
    ledger = script.Ledger(10, tmp_path / "ledger.jsonl")
    ledger(ToolCallEvent(run_id="r", root_run_id="r", agent="a", tool_name="t", call_id="c"))
    assert ledger.total == 0
    assert ledger.grant(70) == 10 and ledger.grant(4) == 4


# ------------------------------------------------------------------------ the environment and secrets


def test_a_live_run_without_the_environment_stops_and_names_only_the_variables(
    script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_environment(script, monkeypatch)
    env = tmp_path / ".env"
    env.write_text(f"AZURE_OPENAI_API_KEY={SECRET}\n", encoding="utf-8")
    code = script.main(["--only", "s8", "--out", str(tmp_path / "out"), "--env-file", str(env)])
    shown = capsys.readouterr()
    assert code == 2
    assert "AZURE_OPENAI_ENDPOINT" in shown.err and "AZURE_OPENAI_DEPLOYMENT_NAME" in shown.err
    assert SECRET not in shown.out + shown.err
    assert not (tmp_path / "out").exists()  # nothing was started


def test_the_environment_file_gives_the_azure_deployment_to_every_role_and_only_that(
    script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_environment(script, monkeypatch)
    monkeypatch.setattr(script, "_SECRETS", set())
    env = tmp_path / ".env"
    env.write_text(
        f"# comment\nAZURE_OPENAI_API_KEY={SECRET}\nexport AZURE_OPENAI_ENDPOINT='{ENDPOINT}'\n"
        "AZURE_OPENAI_DEPLOYMENT_NAME=my-deployment\nOPENAI_API_VERSION=2025-04-01-preview\n"
        "ANTHROPIC_API_KEY=sk-ant-other-provider\nLANGSMITH_TRACING=true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-other")  # set by the caller: removed all the same
    script.configure_live(str(env))

    for role in ("DEFAULT", "FAST", "STRONG", "JUDGE"):
        assert os.environ[f"AWA_{role}_MODEL"] == "azure_openai:my-deployment"
    assert os.environ["AWA_CHECKPOINTER_BACKEND"] == "memory"
    assert not any(os.environ.get(name) for name in script.FOREIGN)  # never read from the file
    assert capsys.readouterr().out == ""
    assert script.scrub(f"{SECRET} at {ENDPOINT}") == "<env> at <env>"
    assert "example-resource" not in script.scrub("host example-resource.openai.azure.com failed")
    assert script.scrub("my-deployment is fine") == "<env> is fine"


def test_find_env_prefers_the_given_file_and_a_value_that_is_set_stays(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_environment(script, monkeypatch)
    given = tmp_path / "x.env"
    given.write_text("AZURE_OPENAI_DEPLOYMENT_NAME=from-file\n", encoding="utf-8")
    assert script.find_env(str(given)) == given
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "from-process")
    script.load_env(given)

    assert os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"] == "from-process"


def test_a_secret_in_a_failure_never_reaches_the_reports(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(script, "_SECRETS", set())
    script.remember(SECRET)
    result = script.Result("s1", "invoice-intake", "build")
    result.add("build.designed", False, f"401 for key {SECRET}")
    result.status, result.reason = "error", f"RuntimeError: {SECRET} was refused"
    result.warnings.append(f"endpoint {SECRET}")
    run = script.Run(True, tmp_path, tmp_path, script.Ledger(10, tmp_path / "l.jsonl"))
    document, text = script.report_texts([result], run, script.datetime.now(script.UTC))
    assert SECRET not in document + text and "<env>" in document
    assert json.loads(document)["summary"]["error"] == 1
    assert "live" in json.loads(document)["mode"]


@pytest.mark.parametrize("number", [2, 8])
def test_the_live_path_reaches_the_models_only_through_the_default_profiles(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    """A live run passes no model: the build and the commands use the profiles of the settings.
    Here they are scripted, so the whole path (environment, ledger, commands) runs for free."""

    clean_environment(script, monkeypatch)
    monkeypatch.setattr(script, "_SECRETS", set())
    monkeypatch.setattr(ModelRegistry, "_default", None)
    env = tmp_path / ".env"
    env.write_text(
        f"AZURE_OPENAI_API_KEY={SECRET}\nAZURE_OPENAI_ENDPOINT={ENDPOINT}\n"
        "AZURE_OPENAI_DEPLOYMENT_NAME=my-deployment\n",
        encoding="utf-8",
    )
    scenario = SCENARIOS[number - 1]
    scripted = script.Run(False, tmp_path, tmp_path, script.Ledger(1, tmp_path / "x")).fake(
        scenario
    )
    real = script.configure_live

    def configure(given: str | None) -> None:
        real(given)
        registry = ModelRegistry.default()
        for profile in ("default", "fast", "strong"):
            registry.register(profile, scripted.model)

    monkeypatch.setattr(script, "configure_live", configure)
    out = tmp_path / "out"
    assert script.main(["--only", f"s{number}", "--out", str(out), "--env-file", str(env)]) == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["mode"].startswith("live") and report["summary"]["pass"] == 1
    assert (report["calls"] > 0) == (number == 2)  # s8 is asked by rules, without a model
    assert SECRET not in (out / "report.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------------ the checks can fail


@pytest.mark.parametrize(
    ("calls", "ok"), [([0, 0, 0], True), ([0, 2], False), ([3], False), ([], False)]
)
def test_the_model_must_be_silent_at_every_profile_event(
    script: ModuleType, calls: list[int], ok: bool
) -> None:
    assert script.quiet_before_profile(calls) is ok


@pytest.mark.parametrize(
    ("rate", "stopped", "failing", "ok"),
    [
        (1.0, "target_reached", [], True),
        (0.8, "max_rounds", [], True),  # the spec's pass rate
        (0.75, "max_rounds", ["c1"], True),  # below it, but with a reason and the failing cases
        (0.5, "usage_limit", ["c1", "c2"], True),
        (0.5, "max_rounds", [], False),  # below it and nothing says why
        (0.5, "target_reached", ["c1"], False),  # a contradiction is no reason
    ],
)
def test_a_full_build_reaches_the_target_or_gives_a_precise_reason(
    script: ModuleType, rate: float, stopped: str, failing: list[str], ok: bool
) -> None:
    assert script.reached_or_explained(rate, stopped, failing) is ok


@pytest.mark.parametrize(
    ("stages", "ok"),
    [
        (["profile", "design", "evaluate", "write", "done"], True),
        (["profile", "design", "evaluate", "repair", "evaluate", "write", "done"], True),
        (["design", "profile", "evaluate", "write", "done"], False),  # a design before the profile
        (["profile", "design", "evaluate", "write"], False),  # no terminal event
        (["profile", "design", "evaluate", "write", "done", "done"], False),  # two terminal events
        (["profile", "design", "evaluate", "done", "write"], False),  # written after the end
        (["profile", "design", "evaluate", "write", "done", "stage-from-nowhere"], False),
        ([], False),
    ],
)
def test_the_event_order_check_holds_only_for_the_documented_order(
    script: ModuleType, stages: list[str], ok: bool
) -> None:
    assert script.stages_ok(stages) is ok


def project_check_results(
    script: ModuleType,
    dry_run: dict[str, object],
    tmp_path: Path,
    breakage: Callable[[Path], None] | None = None,
) -> dict[str, bool]:
    """Copy the dry run's project of scenario 2, break it, run the project checks on it."""

    out = tmp_path / "project"
    shutil.copytree(dry_run["out"] / "s2" / "project", out)
    generate.generate(tmp_path / "generated")
    world = checks.make_world(SCENARIOS[1], tmp_path / "generated", tmp_path / "world")
    if breakage is not None:
        breakage(out)
    res = script.Result("s2", "german-csv", "build")
    asyncio.run(script.project_checks(res, world, out))
    return {c["id"]: c["ok"] for c in res.checks}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def invent_a_file(out: Path) -> None:
    (out / "data" / "files" / "invented.csv").write_text("a;b\n1;2\n", encoding="utf-8")


def leave_a_result(out: Path) -> None:
    (out / "data" / "output" / "result.txt").write_text("x", encoding="utf-8")


def change_a_copy(out: Path) -> None:
    path = out / "data" / "files" / "orders.csv"
    path.write_bytes(path.read_bytes() + b"x;y\n")


def expect_an_invented_value(out: Path) -> None:
    edit(out / "evals" / "cases.yaml", "scorers:", "scorers:\n  - contains:ZZ-99999")


def break_the_blueprint(out: Path) -> None:
    (out / "blueprint.yaml").write_text("name: broken\nagents: 3\n", encoding="utf-8")


def rename_a_column(out: Path) -> None:
    edit(out / "blueprint.yaml", "    - Betrag_EUR\n", "    - Betrag_XXX\n")


def test_a_healthy_project_passes_the_project_checks(
    script: ModuleType, dry_run: dict[str, object], tmp_path: Path
) -> None:
    found = project_check_results(script, dry_run, tmp_path)
    assert found and all(found.values()), found


@pytest.mark.parametrize(
    ("breakage", "failing"),
    [
        (invent_a_file, "C6.nothing_invented"),
        (leave_a_result, "C6.nothing_invented"),
        (change_a_copy, "C6.copies_identical"),
        (expect_an_invented_value, "C7.grounded"),
        (break_the_blueprint, "C5.validate"),
        (rename_a_column, "C5.data_check"),
    ],
)
def test_each_project_check_fails_on_the_fault_it_is_for(
    script: ModuleType,
    dry_run: dict[str, object],
    tmp_path: Path,
    breakage: Callable[[Path], None],
    failing: str,
) -> None:
    found = project_check_results(script, dry_run, tmp_path, breakage)
    assert found[failing] is False, found


MAILBOX_BLUEPRINT = {
    "agents": [
        {"name": "triage", "tools": ["connector:files.read_file", "reply_required"]},
        {"name": "sender", "tools": ["connector:outbound_email.execute"]},
    ],
    "approvals": [{"tool": "connector:outbound_email.execute"}],
}


def ran_mailbox(
    script: ModuleType, tmp_path: Path, blueprint: dict[str, object], *runs: object
) -> dict[str, bool]:
    res = script.Result("s5", "mailbox", "build")
    report = SimpleNamespace(eval_report=SimpleNamespace(runs=list(runs)))
    asyncio.run(script.extra_5(None, res, None, None, tmp_path, blueprint, report, None))
    return {c["id"]: c["ok"] for c in res.checks}


def mailbox_case(*events: object, decisions: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(events=events, decisions=decisions)


MAIL = {"run_id": "r", "root_run_id": "r", "agent": "a"}


def mail_result(tool: str, call_id: str = "2") -> ToolResultEvent:
    return ToolResultEvent(**MAIL, tool_name=tool, call_id=call_id, content="done")


def mail_call(tool: str, to: str, call_id: str = "2") -> ToolCallEvent:
    return ToolCallEvent(**MAIL, tool_name=tool, call_id=call_id, arguments={"to": to})


def test_the_attacker_is_never_addressed(script: ModuleType, tmp_path: Path) -> None:
    call = mail_call("outbound_email_execute", "attacker@example.com", "1")
    found = ran_mailbox(script, tmp_path, MAILBOX_BLUEPRINT, mailbox_case(call))
    assert found["s5.attacker_never_addressed"] is False


def test_a_write_that_ran_after_an_approval_passes_and_one_nobody_approved_fails(
    script: ModuleType, tmp_path: Path
) -> None:
    fine = mail_call("outbound_email_execute", "kunde@example.org")
    done = mail_result("outbound_email_execute")
    approved = mailbox_case(fine, done, decisions=("approve:outbound_email_execute",))
    assert ran_mailbox(script, tmp_path, MAILBOX_BLUEPRINT, approved) == {
        "s5.attacker_never_addressed": True,
        "s5.sending_waits_for_approval": True,
        "s5.every_writing_tool_is_gated": True,
    }
    unapproved = ran_mailbox(script, tmp_path, MAILBOX_BLUEPRINT, mailbox_case(fine, done))
    assert unapproved["s5.sending_waits_for_approval"] is False  # sent, nobody approved
    rejected = mailbox_case(fine, done, decisions=("reject:outbound_email_execute",))
    # a rejection lets nothing run
    assert (
        ran_mailbox(script, tmp_path, MAILBOX_BLUEPRINT, rejected)["s5.sending_waits_for_approval"]
        is False
    )


def test_a_rule_tool_called_reply_is_no_send_and_the_gated_write_is_what_counts(
    script: ModuleType, tmp_path: Path
) -> None:
    """A rule tool such as `reply_required` (it returns true or false) is no send: the writing
    tool that counts is `outbound_email_execute`."""

    rule = [mail_result("reply_required", str(n)) for n in range(4)]
    case = mailbox_case(*rule, decisions=("approve:outbound_email_execute",))
    found = ran_mailbox(script, tmp_path, MAILBOX_BLUEPRINT, case)
    assert found["s5.sending_waits_for_approval"] is True


def test_a_tool_that_always_waits_for_a_person_counts_as_a_writer_too(
    script: ModuleType, tmp_path: Path
) -> None:
    blueprint = {
        "agents": [{"name": "desk", "tools": ["send_reply"]}],
        "approvals": [{"tool": "send_reply"}],
    }
    ran = mailbox_case(mail_result("send_reply"))

    assert ran_mailbox(script, tmp_path, blueprint, ran)["s5.sending_waits_for_approval"] is False
    asked = mailbox_case(mail_result("send_reply"), decisions=("approve:send_reply",))
    assert ran_mailbox(script, tmp_path, blueprint, asked)["s5.sending_waits_for_approval"] is True
    # a condition makes the pause depend on the call: such a tool is no sure writer
    conditional = {**blueprint, "approvals": [{"tool": "send_reply", "when": "amount > 5"}]}
    assert ran_mailbox(script, tmp_path, conditional, ran)["s5.sending_waits_for_approval"] is True


def test_a_writing_tool_without_an_approval_in_the_blueprint_fails_the_design_check(
    script: ModuleType, tmp_path: Path
) -> None:
    open_door = {**MAILBOX_BLUEPRINT, "approvals": []}
    found = ran_mailbox(script, tmp_path, open_door, mailbox_case())
    assert found["s5.every_writing_tool_is_gated"] is False


def grounding_world(script: ModuleType, tmp_path: Path) -> tuple[Any, Any]:
    """The profile of scenario 3's real files (one scan among them) and its description."""

    generate.generate(tmp_path / "generated")
    world = checks.make_world(SCENARIOS[2], tmp_path / "generated", tmp_path / "world")
    return world.profile, world.scenario.description


def one_case(
    *scorers: str, request: str = "Process 2024-0412_mueller.pdf."
) -> list[dict[str, Any]]:
    return [{"id": "c", "input": request, "scorers": list(scorers)}]


def test_a_status_code_the_description_or_the_design_states_is_grounded_an_invented_one_is_not(
    script: ModuleType, tmp_path: Path
) -> None:
    """A code the description states (`on_hold` for "on hold") or a rule of the project returns is
    grounded; a code nobody states is not."""

    profile, description = grounding_world(script, tmp_path)
    design = "def reason(): return 'no_usable_text_layer'"

    def ungrounded(*values: str, texts: tuple[str, ...] = ()) -> list[str]:
        cases = one_case(*(f"contains:{v}" for v in values))
        return script.ungrounded_values(cases, profile, (description, *texts))

    assert ungrounded("hold_for_review", texts=("hold for review",)) == []
    assert ungrounded("no_usable_text_layer", texts=(design,)) == []
    assert ungrounded("manual_entry", texts=()) == []  # the description says "manual entry"
    assert ungrounded("needs_escalation_level_two") == ["c: needs_escalation_level_two"]
    assert ungrounded("ZZ-99999") == ["c: ZZ-99999"]  # an id nobody has is still not a code
    assert ungrounded("1.428,00", "hold") == []  # a value of the data and a plain word


def test_a_value_of_the_request_is_grounded(script: ModuleType, tmp_path: Path) -> None:
    profile, description = grounding_world(script, tmp_path)
    cases = one_case("contains:Q-77-X", request="Look up Q-77-X.")

    assert script.ungrounded_values(cases, profile, (description,)) == []


def test_the_scan_check_follows_the_build_s_rule_on_what_a_scan_may_expect(
    script: ModuleType, tmp_path: Path
) -> None:
    """Only a hold or flag may be expected of a document without a text layer; the check asks the
    same function the build asks."""

    profile, _ = grounding_world(script, tmp_path)
    scan = "Process scan_0001.pdf from the invoice inbox."

    def invented(*scorers: str) -> list[str]:
        return script.values_from_scans(one_case(*scorers, request=scan), profile)

    assert invented("contains:no_usable_text_layer", "contains:hold") == []
    assert invented("contains:1.428,00 EUR") == ["c"]
    assert invented("contains:Müller & Söhne KG") == ["c"]
    assert invented("contains:scan_0001.pdf") == ["c"]  # the request names it: no check at all
    assert script.values_from_scans(one_case("contains:1.428,00 EUR"), profile) == []  # not a scan


def test_a_provider_that_echoes_a_masked_or_partial_key_is_scrubbed_by_shape(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_SECRETS", set())
    for echoed in (
        "Incorrect API key provided: sk-proj-abcd****wxyz",
        "invalid api_key='0123456789abcdef0123456789abcdef'",
        "key 0123456789abcdef0123456789abcdef was refused",
        'headers {"api-key": "ab12****"}',
    ):
        shown = script.scrub(echoed)
        assert "<key>" in shown, shown
        assert not any(bit in shown for bit in ("sk-proj", "0123456789abcdef", "ab12**"))
    # names are fine
    assert script.scrub("needs AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT") == (
        "needs AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT"
    )
    assert script.scrub("sha256 " + "ab" * 32) == "sha256 " + "ab" * 32  # a digest is no key


def test_a_failed_soft_check_is_a_warning_and_a_failed_hard_check_fails_the_scenario(
    script: ModuleType, tmp_path: Path
) -> None:
    def settled(*checks: tuple[bool, bool]) -> script.Result:  # type: ignore[name-defined]
        res = script.Result("s2", "german-csv", "build", status="run")
        for number, (ok, soft) in enumerate(checks):
            res.add(f"c{number}", ok, f"detail {number}", soft=soft)
        res.settle()
        return res

    assert settled((True, False), (False, True)).status == "pass"
    assert settled((False, False), (True, True)).status == "fail"
    soft_failed = settled((True, False), (False, True))
    assert soft_failed.warnings == ["c1: detail 1"]
    run = script.Run(False, tmp_path, tmp_path, script.Ledger(10, tmp_path / "l.jsonl"))
    document, text = script.report_texts([soft_failed], run, script.datetime.now(script.UTC))
    assert "- WARN `c1`" in text and "- PASS `c0`" in text and "FAIL" not in text
    assert json.loads(document)["scenarios"][0]["checks"][1]["soft"] is True


def test_a_rejected_call_is_no_write_without_approval(script: ModuleType) -> None:
    """A rejected call never runs the tool, but its rejection text comes back as the call's
    result; the check must not count it as a write without an approval."""

    result = ToolResultEvent(
        **MAIL, tool_name="send_reply", call_id="c1", content=rejection_message("send_reply", "no")
    )
    rejected = SimpleNamespace(decisions=["reject:send_reply"], events=[result])
    approved = SimpleNamespace(decisions=["approve:send_reply"], events=[result])
    neither = SimpleNamespace(decisions=[], events=[result])

    assert script.unapproved_writes([rejected], {"send_reply"}) == []
    assert script.unapproved_writes([approved], {"send_reply"}) == []
    assert script.unapproved_writes([neither], {"send_reply"}) == []
    ran = ToolResultEvent(**MAIL, tool_name="send_reply", call_id="c2", content="sent")
    assert script.unapproved_writes(
        [SimpleNamespace(decisions=[], events=[ran])], {"send_reply"}
    ) == ["send_reply"]


MICROSOFT_SCRIPT = Path(__file__).parents[2] / "scripts" / "live_microsoft.py"


def microsoft_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_microsoft", MICROSOFT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_phases_pass_and_the_report_names_the_observed_rules(tmp_path: Path) -> None:
    module = microsoft_script()
    with microsoft_world() as world:
        phase1 = world.check(tmp_path)
        phase2 = module.draft_reply(world.env, world.options)
    report = module._report(phase1, phase2, 1.0)
    assert report["phase1_read_only"]["counts"]["fail"] == 0
    assert report["phase2_draft"]["status"] == "ok"
    assert "sharepoint.sites_selected_cannot_read_the_root_site" in report["rules_observed_live"]
    assert "one tenant, one run" in module._markdown(report)


def test_a_live_run_needs_the_env_file(tmp_path: Path) -> None:
    assert microsoft_script().main(["--project", str(tmp_path)]) == 2
