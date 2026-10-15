"""Pre-flight: `run`, `chat`, `eval`, `build`, `worker` and `serve` stop with one line per problem
and exit code 78 before they bind a port, read files or open a prompt.

Before: serve logged a traceback per mailbox poll, the worker exited 0, chat printed the error
three times with a class name, build read the files first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _helpers.fake_secrets import fake_secret
from awa.cli import main
from awa.cli._preflight import check_models
from awa.core.preflight import PreflightReport, Problem, check_api_key_env, check_env, check_store

SENTINEL = fake_secret("sk-", "preflight-sentinel-91f3")

# The checks look at model keys only when real model requests are allowed (no call is made).
pytestmark = pytest.mark.model_requests_allowed


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "desk"
    (folder / "inbox").mkdir(parents=True)
    (folder / "blueprint.yaml").write_text(
        "name: desk\nagents:\n  - name: clerk\n    instructions: Answer.\n"
        "triggers:\n  - name: hook\n    webhook: {secret_env: HOOK_SECRET}\n"
        "  - name: files\n    file_drop: {directory: inbox}\n",
        encoding="utf-8",
    )
    (folder / "inbox" / "a.txt").write_text("untouched", encoding="utf-8")
    monkeypatch.chdir(folder)
    monkeypatch.delenv("HOOK_SECRET", raising=False)
    monkeypatch.delenv("MY_API_KEY", raising=False)
    return folder


def lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.strip()]


def test_worker_without_a_key_exits_78_and_leaves_the_drop_folder(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["worker", "blueprint.yaml", "--once"]) == 78
    err = capsys.readouterr().err
    assert all(line.startswith("problem: ") and " -- fix: " in line for line in lines(err))
    assert any("ANTHROPIC_API_KEY" in line for line in lines(err))
    assert any("HOOK_SECRET" in line for line in lines(err))
    assert (project / "inbox" / "a.txt").read_text(encoding="utf-8") == "untouched"
    assert not (project / "inbox" / "processed").exists()


def test_serve_with_an_unset_api_key_env_exits_78_before_binding(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("fastapi")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    monkeypatch.setenv("HOOK_SECRET", "s")
    started: list[object] = []
    monkeypatch.setattr("awa.server._app.run_uvicorn", lambda *a, **k: started.append(a))
    assert main(["serve", "blueprint.yaml", "--api-key-env", "MY_API_KEY"]) == 78
    assert started == []
    assert "MY_API_KEY is not set (needed by --api-key-env)" in capsys.readouterr().err


def test_an_unreachable_database_is_one_problem(project: Path) -> None:
    calls: list[tuple[str, float]] = []

    def refuse(dsn: str, timeout: float) -> None:
        calls.append((dsn, timeout))
        raise ConnectionRefusedError("refused")

    dsn = f"postgresql://awa:{SENTINEL}@127.0.0.1:1/awa"
    [problem] = check_store(dsn, timeout=2, connect=refuse)
    assert calls == [(dsn, 2)]
    assert problem.code == "store_unreachable" and SENTINEL not in problem.line()


def test_azure_only_never_asks_for_an_anthropic_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.core.settings import Settings
    from awa.models import ModelRegistry

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k-1234")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "dep")
    registry = ModelRegistry(Settings(_env_file=None))
    assert check_models([None, "fast", "strong"], registry=registry) == []


def test_a_missing_key_names_the_provider_that_is_used() -> None:
    from awa.core.settings import Settings
    from awa.models import ModelRegistry

    registry = ModelRegistry(Settings(_env_file=None, default_model="openai:gpt-6-luna"))
    [problem] = check_models([None], registry=registry)
    assert "OpenAI" in problem.what and "OPENAI_API_KEY" in problem.fix
    assert "ANTHROPIC" not in problem.fix


def test_a_profile_registered_as_a_model_needs_no_key() -> None:
    from awa.core.settings import Settings
    from awa.models import ModelRegistry
    from awa.testing import FakeChatModel

    registry = ModelRegistry(Settings(_env_file=None))
    registry.register("default", FakeChatModel(["hi"]))
    assert check_models([None], registry=registry) == []


def test_the_required_env_of_a_user_trigger_is_checked() -> None:
    from awa.automation import BaseTrigger

    class Ledger(BaseTrigger):
        def required_env(self) -> tuple[str, ...]:
            return ("LEDGER_TOKEN",)

        async def events(self, context):  # type: ignore[no-untyped-def, override]
            if False:
                yield

    trigger = Ledger("ledger")
    [problem] = check_env(dict.fromkeys(trigger.required_env(), f"trigger {trigger.name}"))
    assert problem.what == "LEDGER_TOKEN is not set (needed by trigger ledger)"


def test_skip_preflight_warns_once_and_doctor_shows_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import _doctor

    assert main(["--skip-preflight", "run", "blueprint.yaml", "hi"]) == 1  # the run itself fails
    err = capsys.readouterr().err
    assert err.count("--skip-preflight") == 1
    assert "problem:" not in err
    report = _doctor.run_doctor()
    check = report.check("preflight")
    assert check is not None and check.status == "warn"


def test_chat_with_a_configuration_error_prints_one_line(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "blueprint.yaml").write_text(
        "name: desk\nagents:\n  - name: clerk\n    instructions: Answer.\n", encoding="utf-8"
    )
    assert main(["chat", "blueprint.yaml"]) == 78
    err = lines(capsys.readouterr().err)
    assert len(err) == 1 and "Error" not in err[0].split(" -- ")[0]


def test_build_stops_before_it_reads_the_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[object] = []
    monkeypatch.setattr("awa.blueprints.build_project", lambda *a, **k: read.append(a))
    assert main(["build", "Book invoices.", "--data", "inbox", "--no-input"]) == 78
    assert read == []


def test_no_check_prints_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "")
    problems = [
        *check_api_key_env("MY_API_KEY"),
        *check_env({"HOOK_SECRET": f"trigger with {SENTINEL[:0]}hook"}),
        *check_store(f"postgresql://u:{SENTINEL}@h/x", connect=_refuse),
    ]
    report = PreflightReport(problems)
    assert not report.ok
    assert SENTINEL not in report.render()
    assert SENTINEL not in json.dumps(report.to_json())


def test_a_warning_does_not_stop(capsys: pytest.CaptureFixture[str]) -> None:
    from awa.cli._preflight import enforce

    enforce(PreflightReport([Problem("note", "something is off", "look", fatal=False)]))
    assert capsys.readouterr().err.startswith("warning: something is off")


def _refuse(dsn: str, timeout: float) -> None:
    raise OSError(f"could not connect to {dsn}")


def test_health_says_when_the_preflight_was_skipped() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from awa import Agent
    from awa.core.config import set_skip_preflight
    from awa.server import create_app
    from awa.testing import FakeChatModel

    app = create_app({"desk": Agent("desk", model=FakeChatModel(["hi"]))})
    with TestClient(app) as client:
        assert client.get("/v1/health").json() == {"status": "ok"}
        set_skip_preflight(True)
        assert client.get("/v1/health").json() == {"status": "ok", "preflight_skipped": True}
