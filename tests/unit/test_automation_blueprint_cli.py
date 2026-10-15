"""Automation blueprints (`triggers:` / `automation:`) and the `worker`, `trigger` and `runs` commands."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from _helpers.automation import ScriptedSystem
from _helpers.polling import SUBPROCESS_TIMEOUT
from awa._internal import aio as asyncio_compat
from awa.automation import (
    FileDrop,
    InMemoryEventQueue,
    Mailbox,
    ProcessRun,
    QueueTrigger,
    Schedule,
    SqliteRunStore,
    Webhook,
    build_automation,
    load_automation_blueprint,
)
from awa.automation.blueprint import _retry_policy, split_automation
from awa.automation.models import WaitState
from awa.automation.versioning import blueprint_digest
from awa.blueprints import Blueprint
from awa.blueprints._automation import AutomationBlueprint
from awa.cli import build_parser
from awa.errors import BlueprintError, ConfigurationError, ProcessRunStateError, RunNotFoundError
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, tool_call

BLUEPRINT = """
format_version: awa/v1
name: invoices
version: 1.10
agents:
  - name: clerk
    description: Books supplier invoices.

triggers:
  - name: nightly
    schedule: {cron: "0 6 * * MON-FRI", timezone: UTC, catch_up: skip}
    input: "Run the dunning check for {payload.date}."
  - name: supplier-invoice
    webhook: {secret_env: INVOICE_HOOK_SECRET}
    input: "Book invoice {payload.invoice_no}"
    thread_id: "inv-{payload.invoice_no|slug}"
    dedup: "{payload.invoice_no}"
  - name: drops
    file_drop: {directory: inbox, pattern: "*.xml", stable_for: 0}
  - name: mail
    mailbox: {host: imap.example.com, folder: Invoices}
  - name: tickets
    queue: {name: tickets, backend: memory}
  - name: paused
    schedule: {every: 1h}
    enabled: false

automation:
  input: "{payload.text}"
  concurrency: 3
  retry: {max_attempts: 4, initial: 1m, max_delay: 2h}
  sla: {complete_within: 2d, approval_within: 4h, remind_every: 1h}
  migration: at_wait
  notify: [{webhook: "https://hooks.example.com/ap", secret_env: AP_HOOK_SECRET}]
"""


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


@pytest.fixture
def blueprint_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    path = tmp_path / "invoices.yaml"
    path.write_text(BLUEPRINT, encoding="utf-8")
    return path


def cli(*argv: str) -> int:
    args = build_parser(list(argv)).parse_args(argv)
    code: int = args.handler(args)
    return code


# ---------------------------------------------------------------------- blueprint


def test_load_and_build_an_automation_blueprint(
    blueprint_file: Path, models: ModelRegistry
) -> None:
    blueprint, spec = load_automation_blueprint(blueprint_file)
    assert blueprint.name == "invoices" and blueprint.version == "1.10"
    assert spec is not None and [t.name for t in spec.triggers][:2] == [
        "nightly",
        "supplier-invoice",
    ]
    assert spec.triggers[4].kind == "queue"

    models.register("default", FakeChatModel(["ok"]))
    automation = build_automation(
        blueprint, blueprint.build(), spec=spec, base_dir=blueprint_file.parent
    )
    kinds = {t.name: type(t) for t in automation.triggers}
    assert kinds == {
        "nightly": Schedule,
        "supplier-invoice": Webhook,
        "drops": FileDrop,
        "mail": Mailbox,
        "tickets": QueueTrigger,
    }
    drops = automation.trigger("drops")
    assert drops.directory == blueprint_file.parent / "inbox"
    assert automation.name == "invoices" and automation.concurrency == 3
    assert automation.retry_policy.max_attempts == 4
    assert automation.sla is not None and automation.sla.approval_within is not None
    assert automation.migration == "at_wait"
    assert len(automation.notifiers) == 1
    assert automation.version == blueprint_digest(blueprint, extra=spec.model_dump(mode="json"))
    with pytest.raises(ConfigurationError, match="no trigger"):
        automation.trigger("nope")


def test_digest_ignores_formatting(blueprint_file: Path) -> None:
    blueprint, _ = load_automation_blueprint(blueprint_file)
    reformatted = blueprint_file.with_name("other.yaml")
    reformatted.write_text(
        "# a comment\n" + BLUEPRINT.replace("description: Books", "description:   Books"),
        encoding="utf-8",
    )
    again, _ = load_automation_blueprint(reformatted)
    assert blueprint_digest(blueprint) == blueprint_digest(again)


def test_invalid_sections_are_reported() -> None:
    with pytest.raises(ConfigurationError, match="exactly one"):
        split_automation({"triggers": [{"name": "x"}]})
    with pytest.raises(ConfigurationError, match="duplicate"):
        split_automation({"triggers": [{"name": "a", "schedule": {"every": 60}}] * 2})
    with pytest.raises(ConfigurationError, match="secret_env"):
        split_automation({"triggers": [{"name": "a", "webhook": {}}]})
    with pytest.raises(ConfigurationError, match="duration"):
        split_automation({"automation": {"dedup_window": "soon"}})
    with pytest.raises(ConfigurationError, match="not both"):
        split_automation(
            {"triggers": [], "automation": {"triggers": [{"name": "a", "schedule": {"every": 1}}]}}
        )
    assert split_automation({"name": "x"}) == ({"name": "x"}, None)


def test_check_reports_templates_and_missing_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOOK", raising=False)
    spec = AutomationBlueprint.model_validate(
        {
            "input": "{payload.",
            "triggers": [{"name": "w", "webhook": {"secret_env": "HOOK"}, "dedup": "{x|shout}"}],
        }
    )
    problems = spec.check()
    assert any("automation.input" in p for p in problems)
    assert any("triggers.w.dedup" in p for p in problems)
    assert any("HOOK is not set" in p for p in problems)


def test_build_automation_from_plain_sections(models: ModelRegistry) -> None:
    queue = InMemoryEventQueue()
    automation = build_automation(
        {"triggers": [{"name": "q", "queue": {"name": "jobs"}}], "automation": {"name": "jobs"}},
        ScriptedSystem(),
        queues={"jobs": queue},
    )
    assert automation.trigger("q").queue is queue
    # backend auto: in memory without a DSN; an explicit postgres backend needs one.
    local = build_automation(
        {"triggers": [{"name": "q", "queue": {"name": "jobs"}}]}, ScriptedSystem()
    )
    assert isinstance(local.trigger("q").queue, InMemoryEventQueue)
    with pytest.raises(ConfigurationError, match="DSN"):
        build_automation(
            {"triggers": [{"name": "q", "queue": {"name": "jobs", "backend": "postgres"}}]},
            ScriptedSystem(),
        )
    plain = Blueprint.from_yaml_text("name: plain\nagents:\n  - name: a\n")
    with pytest.raises(ConfigurationError, match="no automation"):
        build_automation(plain, ScriptedSystem())


# ---------------------------------------------------------------------- CLI


def test_trigger_list(blueprint_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli("trigger", "list", str(blueprint_file)) == 0
    out = capsys.readouterr().out
    assert "nightly" in out and "schedule" in out and "cron=0 6 * * MON-FRI" in out
    assert "paused" in out


def test_trigger_run_dry_run_and_real_run(
    blueprint_file: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli(
        "trigger",
        "run",
        str(blueprint_file),
        "supplier-invoice",
        "--payload",
        '{"invoice_no": "RE 17"}',
        "--dry-run",
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "input:     Book invoice RE 17" in out
    assert "thread_id: inv-re-17" in out and "dedup_key: RE 17" in out

    models.register("default", FakeChatModel(["Booked."]))
    payload = blueprint_file.with_name("payload.json")
    payload.write_text('{"invoice_no": "RE 18"}', encoding="utf-8")
    code = cli(
        "trigger", "run", str(blueprint_file), "supplier-invoice", "--payload", f"@{payload}"
    )
    out = capsys.readouterr().out
    assert code == 0 and "succeeded" in out and "Booked." in out

    models.register("default", FakeChatModel(["Dunning done."]))
    assert cli("trigger", "run", str(blueprint_file), "nightly", "--json") == 0
    run = json.loads(capsys.readouterr().out.split("\n", 3)[3])
    assert run["status"] == "succeeded" and run["input"].startswith("Run the dunning check for 20")
    with pytest.raises(ConfigurationError, match="JSON"):
        cli("trigger", "run", str(blueprint_file), "nightly", "--payload", "{bad")


TICKETS = """
name: tickets
agents:
  - name: clerk
    description: Files tickets.
    tools: [file_ticket, "connector:crm"]
connectors:
  crm: {use: rest}
triggers:
  - name: hook
    webhook: {allow_unsigned: true}
    input: "File {payload.title}"
    thread_id: "ticket-{payload.id}"
  - name: tickets
    queue: {name: tickets}
"""


def test_trigger_dry_run_builds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("CRM_URL", "CRM_TOKEN", "CRM_USERNAME", "CRM_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "tickets.yaml"
    path.write_text(TICKETS, encoding="utf-8")
    monkeypatch.delenv("AWA_POSTGRES_DSN", raising=False)

    payload = '{"title": "Printer jam", "id": 7}'
    assert cli("trigger", "run", str(path), "hook", "--payload", payload, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "input:     File Printer jam" in out and "thread_id: ticket-7" in out

    # Without --dry-run the system is built, and its tool module is missing.
    with pytest.raises(BlueprintError, match="file_ticket"):
        cli("trigger", "run", str(path), "hook", "--payload", payload)


def seed(path: Path, *runs: ProcessRun) -> None:
    store = SqliteRunStore(path)

    async def write() -> None:
        for run in runs:
            await store.create(run)

    asyncio_compat.run(write())


def test_runs_commands_on_the_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWA_POSTGRES_DSN", raising=False)
    store_path = tmp_path / ".awa" / "awa.db"
    dead = ProcessRun(automation="invoices", status="dead_lettered", error="TimeoutError: down")
    waiting = ProcessRun(automation="invoices", status="waiting")
    seed(store_path, dead, waiting)

    assert cli("runs", "list") == 0
    out = capsys.readouterr().out
    assert dead.id in out and waiting.id in out
    assert "CREATED (UTC)" in out  # the times are UTC, whatever the machine's time zone
    assert dead.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") in out
    assert cli("runs", "list", "--status", "waiting", "--json") == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [waiting.id]
    assert cli("runs", "dlq") == 0
    assert "TimeoutError: down" in capsys.readouterr().out
    assert cli("runs", "show", dead.id) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dead_lettered"
    assert cli("runs", "retry", dead.id, "--actor", "ops") == 0
    assert "queued" in capsys.readouterr().out
    assert cli("runs", "cancel", waiting.id, "--store", str(store_path)) == 0
    assert "cancelled" in capsys.readouterr().out
    with pytest.raises(RunNotFoundError):
        cli("runs", "show", "proc_missing")


def waiting_for_approval(*interrupts: str) -> ProcessRun:
    now = datetime.now(UTC)
    waits = [
        WaitState(interrupt_id=i, kind="approval", key=f"approval:{i}", requested_at=now)
        for i in interrupts
    ]
    return ProcessRun(automation="invoices", status="waiting", waits=waits)


def test_runs_decide_answers_an_approval_in_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWA_POSTGRES_DSN", raising=False)
    store_path = tmp_path / ".awa" / "awa.db"
    approve, edit, reject = (waiting_for_approval("i1") for _ in range(3))
    several = waiting_for_approval("i1", "i2")
    seed(store_path, approve, edit, reject, several)

    def stored(run: ProcessRun) -> ProcessRun:
        found = asyncio_compat.run(SqliteRunStore(store_path).get(run.id))
        assert found is not None
        return found

    assert cli("runs", "decide", approve.id, "--approve", "--actor", "ops") == 0
    captured = capsys.readouterr()
    assert f"{approve.id} invoices queued" in captured.out and not captured.err
    assert stored(approve).resume_value == {"action": "approve", "reviewer": "ops"}

    assert cli("runs", "decide", edit.id, "--approve", "--edit-args", '{"amount": 90}') == 0
    assert stored(edit).resume_value == {"action": "edit", "args": {"amount": 90}}

    assert cli("runs", "decide", reject.id, "--reject", "--note", "Wrong supplier.") == 0
    assert stored(reject).resume_value == {"action": "reject", "note": "Wrong supplier."}
    assert stored(reject).status == "queued" and stored(reject).resuming

    # Several approvals are decided one at a time: the oldest first, or the one named.
    assert cli("runs", "decide", several.id, "--approve") == 0
    assert "1 more approval(s) open" in capsys.readouterr().out
    assert stored(several).status == "waiting"  # i2 is still open
    assert cli("runs", "decide", several.id, "--reject", "--interrupt-id", "i2") == 0
    assert stored(several).status == "queued"
    assert stored(several).resume_value == {
        "i1": {"action": "approve"},
        "i2": {"action": "reject", "note": "Rejected by the operator."},
    }
    with pytest.raises(ProcessRunStateError, match="no approval"):
        cli("runs", "decide", approve.id, "--approve")
    with pytest.raises(ConfigurationError, match="--edit-args goes with --approve"):
        cli("runs", "decide", reject.id, "--reject", "--edit-args", "{}")
    with pytest.raises(ConfigurationError, match="JSON object"):
        cli("runs", "decide", reject.id, "--approve", "--edit-args", "[1]")
    with pytest.raises(RunNotFoundError, match="blueprint's folder"):
        cli("runs", "decide", "proc_missing", "--approve")
    with pytest.raises(SystemExit):
        cli("runs", "decide", approve.id)  # --approve or --reject is required


def test_runs_migrate_dry_run(
    blueprint_file: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = blueprint_file.parent / ".awa" / "awa.db"
    seed(store_path, ProcessRun(automation="invoices", status="waiting", version="sha256:old"))
    assert cli("runs", "migrate", str(blueprint_file), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "migrate" in out and "dry run" in out
    assert cli("runs", "migrate", str(blueprint_file)) == 0
    capsys.readouterr()
    assert cli("runs", "migrate", str(blueprint_file), "--dry-run") == 0
    assert "every unfinished run is on the current version" in capsys.readouterr().out


@pytest.mark.slow
def test_the_section_models_live_with_the_blueprint_schema() -> None:
    """The blueprint schema does not import awa.automation, and awa.automation does not load the
    blueprint package."""

    def run(code: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=True,
        )
        return completed.stdout.strip()

    assert run("import sys, awa.blueprints.schema; print('awa.automation' in sys.modules)") == (
        "False"
    )
    assert run("import sys, awa.automation; print('awa.blueprints' in sys.modules)") == "False"


def test_the_retry_section_becomes_a_retry_policy() -> None:
    section = AutomationBlueprint.model_validate({"retry": {"initial": "2m", "retry_on": ["x"]}})

    policy = _retry_policy(section.retry)

    assert policy.initial == timedelta(minutes=2) and policy.retry_on == ("x",)


def test_worker_takes_only_a_blueprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jobs.py").write_text("automations = []\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not a blueprint file or project folder"):
        cli("worker", "jobs.py:automations", "--once")


def test_trigger_run_pauses_a_workflow_step_for_approval(
    tmp_path: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gated.yaml").write_text(
        "name: payments\nagents:\n  - name: first\n  - name: cashier\n"
        "    tools: [builtin:send_email]\nworkflow: [first, cashier]\n"
        "toolkits: {email: {sender: a@example.com, host: smtp.example.com}}\n"
        "approvals: [{tool: builtin:send_email}]\ntriggers:\n  - name: go\n"
        "    webhook: {allow_unsigned: true}\n",
        encoding="utf-8",
    )
    mail = {"to": ["b@example.com"], "subject": "Paid", "body": "Paid."}
    models.register("default", FakeChatModel(["Checked.", tool_call("send_email", mail)]))

    assert cli("trigger", "run", str(tmp_path / "gated.yaml"), "go") == 2  # paused, not refused
    with pytest.raises(ConfigurationError, match="Cannot read --payload file"):
        cli("trigger", "run", str(tmp_path / "gated.yaml"), "go", "--payload", "@missing.json")


def test_worker_rejects_a_blueprint_without_automation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    plain = tmp_path / "plain.yaml"
    plain.write_text("name: plain\nagents:\n  - name: a\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="no automation"):
        cli("worker", str(plain), "--once")


def test_the_automation_commands_are_registered() -> None:
    parser = build_parser()
    for argv in (["worker", "x.yaml"], ["trigger", "list", "x.yaml"], ["runs", "list"]):
        assert callable(parser.parse_args(argv).handler)


def test_worker_and_trigger_accept_a_project_folder_like_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "desk"
    project.mkdir()
    (project / "blueprint.yaml").write_text("name: plain\nagents:\n  - name: a\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="no automation"):
        cli("worker", str(project), "--once")
    assert cli("trigger", "list", str(project)) == 0
