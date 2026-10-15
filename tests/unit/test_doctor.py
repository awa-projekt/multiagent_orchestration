"""`awa doctor`: every check, offline (fake module lookup, monkeypatched environment)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib.machinery import ModuleSpec
from importlib.util import find_spec as real_find_spec
from pathlib import Path
from typing import Any

import pytest

from _helpers.fake_secrets import fake_secret
from awa._internal import install as install_module
from awa._internal.install import editable_root_of, local_source_of
from awa.cli import _doctor as doctor
from awa.cli import build_parser
from awa.cli._doctor import LIVE_POINTER, DoctorReport, read_pyvenv_cfg, run_doctor, venv_problem
from awa.core.settings import Settings
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, FunctionChatModel
from awa.testing.models import ModelCall

SECRET = fake_secret("sk-", "test-do-not-print-0123456789")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """An empty working directory: no .env, no .venv."""

    return tmp_path


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


_OPTIONAL_PACKAGES_PRESENT = (
    "langchain_anthropic",
    "langchain_openai",
    "fastapi",
    "uvicorn",
    "mcp",
    "langchain_mcp_adapters",
    "opentelemetry",
    "psycopg",
    "psycopg_pool",
    "langgraph.checkpoint.postgres",
    "mkdocs",
    "mkdocstrings",
    "material",
    "openpyxl",
    "pypdf",
    "docx",
)


@pytest.fixture(autouse=True)
def _optional_packages_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every optional package as installed unless a test says otherwise,
    so results do not depend on the extras of the machine running the tests."""

    def find_spec(name: str, package: str | None = None) -> Any:
        if any(
            name == module or name.startswith(module + ".") for module in _OPTIONAL_PACKAGES_PRESENT
        ):
            return ModuleSpec(name, None)
        return real_find_spec(name, package)

    monkeypatch.setattr(install_module, "find_spec", find_spec)


def fake_missing(monkeypatch: pytest.MonkeyPatch, *missing: str) -> None:
    """Make `find_spec` report `missing` (and their submodules) as not installed."""

    previous = install_module.find_spec

    def find_spec(name: str, package: str | None = None) -> Any:
        if any(name == module or name.startswith(module + ".") for module in missing):
            return None
        return previous(name, package)

    monkeypatch.setattr(install_module, "find_spec", find_spec)


def make_venv(root: Path, home: Path) -> Path:
    scripts = root / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if sys.platform == "win32" else "python")).write_text("")
    (root / "pyvenv.cfg").write_text(
        f"home = {home}\nimplementation = CPython\nversion_info = 3.13.5\n", encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------- python and venv


def test_python_below_the_floor_fails() -> None:
    old = doctor._check_python((3, 10, 12), "CPython", "/usr/bin/python3.10")
    assert old.status == "fail"
    assert "older than the required 3.11" in old.summary
    assert old.fix is not None and "uv python install" in old.fix
    root = Path(__file__).resolve().parents[2]
    for script in re.findall(r"scripts/[\w.-]+", old.fix):
        assert (root / script).is_file(), script
    assert doctor._check_python((3, 11, 0), "CPython", "python").status == "ok"


def test_pyvenv_cfg_is_parsed(tmp_path: Path) -> None:
    make_venv(tmp_path / "env", Path(sys.base_prefix))
    config = read_pyvenv_cfg(tmp_path / "env")
    assert config is not None
    assert config["home"] == str(Path(sys.base_prefix))
    assert config["version_info"] == "3.13.5"
    assert read_pyvenv_cfg(tmp_path / "missing") is None


def test_a_venv_copied_from_another_machine_is_detected(tmp_path: Path) -> None:
    copied = make_venv(tmp_path / "copied", tmp_path / "Users" / "someone-else" / "Python313")
    problem = venv_problem(copied)
    assert problem is not None
    assert "does not exist" in problem and "another machine" in problem


def test_healthy_and_incomplete_venvs(tmp_path: Path) -> None:
    assert venv_problem(make_venv(tmp_path / "ok", Path(sys.base_prefix))) is None
    (tmp_path / "empty").mkdir()
    assert "no pyvenv.cfg" in (venv_problem(tmp_path / "empty") or "")


def test_broken_local_venv_is_reported(workdir: Path) -> None:
    make_venv(workdir / ".venv", workdir / "gone")
    report = run_doctor(settings=settings(), cwd=workdir)
    local = report.check("local-venv")
    assert local is not None and local.status == "warn"
    assert "broken" in local.summary
    assert local.fix is not None and "uv venv" in local.fix


def test_healthy_inactive_local_venv_is_a_note(workdir: Path) -> None:
    make_venv(workdir / ".venv", Path(sys.base_prefix))
    local = run_doctor(settings=settings(), cwd=workdir).check("local-venv")
    assert local is not None and local.status == "info"
    assert local.fix is not None and "activate" in local.fix


def test_unhealthy_active_venv_warns(tmp_path: Path) -> None:
    active = make_venv(tmp_path / "active", tmp_path / "other-machine")
    checks = doctor._check_environment(active, Path(sys.base_prefix), tmp_path)
    assert checks[0].name == "environment"
    assert checks[0].status == "warn"
    assert "another machine" in checks[0].summary


def test_global_interpreter_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    prefix = tmp_path / "python"
    checks = doctor._check_environment(prefix, prefix, tmp_path)
    assert checks[0].status == "warn"
    assert "not running in a virtual environment" in checks[0].summary


def test_the_running_environment_is_healthy(workdir: Path) -> None:
    report = run_doctor(settings=settings(), cwd=workdir)
    python = report.check("python")
    assert python is not None and python.status == "ok"
    assert python.data["executable"] == sys.executable
    assert report.check("dependencies") is not None


# ---------------------------------------------------------------------- install and extras


def test_awa_install_is_described(workdir: Path) -> None:
    awa = run_doctor(settings=settings(), cwd=workdir).check("awa")
    assert awa is not None
    assert awa.data["kind"] in {"editable", "wheel"}
    assert awa.data["version"]


def test_editable_root_is_read_from_direct_url() -> None:
    windows = '{"url": "file:///C:/src/awa%20lib", "dir_info": {"editable": true}}'
    posix = '{"url": "file:///srv/me/awa", "dir_info": {"editable": true}}'
    root = editable_root_of(windows if sys.platform == "win32" else posix)
    assert root == (Path("C:/src/awa lib") if sys.platform == "win32" else Path("/srv/me/awa"))
    wheel = '{"url": "file:///tmp/awa_agents-1.0.0-py3-none-any.whl", "archive_info": {}}'
    assert editable_root_of(wheel) is None
    assert editable_root_of(None) is None
    assert editable_root_of("not json") is None
    assert local_source_of(wheel) == Path("/tmp/awa_agents-1.0.0-py3-none-any.whl")
    assert local_source_of(windows if sys.platform == "win32" else posix) is None
    assert local_source_of('{"url": "https://example.org/x.whl"}') is None


def test_missing_optional_extra_is_a_note_with_the_install_command(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_missing(monkeypatch, "fastapi", "uvicorn")
    report = run_doctor(settings=settings(), cwd=workdir)
    server = report.check("extra:server")
    assert server is not None and server.status == "info"
    assert server.fix is not None and "install" in server.fix and "[server]" in server.fix
    assert report.usable


def test_missing_extra_of_the_default_model_is_a_warning_with_the_install_command(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_missing(monkeypatch, "langchain_anthropic")
    report = run_doctor(settings=settings(default_model="anthropic:claude-x"), cwd=workdir)
    extra = report.check("extra:anthropic")
    assert extra is not None and extra.status == "warn"
    assert "model profile default" in extra.summary
    assert extra.fix is not None and "[anthropic]" in extra.fix
    assert report.usable and extra not in report.missing_required


def test_missing_postgres_extra_blocks_a_postgres_backend(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_missing(monkeypatch, "psycopg")
    configured = settings(checkpointer_backend="postgres", postgres_dsn="postgresql://h/db")
    extra = run_doctor(settings=configured, cwd=workdir).check("extra:postgres")
    assert extra is not None and extra.status == "fail"
    assert "checkpointer backend" in extra.summary


def test_missing_core_dependency_fails(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_missing(monkeypatch, "langgraph")
    core = run_doctor(settings=settings(), cwd=workdir).check("dependencies")
    assert core is not None and core.status == "fail"
    assert "langgraph" in core.summary


def test_install_command_uses_pip_or_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    with_pip = doctor._Install("1.0.0", None, None, None, has_pip=True)
    assert with_pip.command('-e ".[mcp]"').endswith('-m pip install -e ".[mcp]"')
    # awa is installed from its local folder or wheel, never from a package index
    assert with_pip.awa_command("mcp").endswith('install -e ".[mcp]" (in the awa folder)')
    wheel = Path("/w/awa_agents-1.0.0-py3-none-any.whl")
    from_wheel = doctor._Install("1.0.0", None, None, None, has_pip=True, source=wheel)
    assert from_wheel.awa_command("mcp").endswith(f'install "{wheel}[mcp]"')
    without_pip = doctor._Install("1.0.0", None, None, None, has_pip=False)
    assert without_pip.command("x").startswith("uv pip install --python ")
    monkeypatch.setattr(
        "awa._internal.install.sys.executable", "C:/Program Files/Python/python.exe"
    )
    monkeypatch.setattr("awa._internal.install.os.name", "nt")
    assert (
        without_pip.command("x") == 'uv pip install --python "C:/Program Files/Python/python.exe" x'
    )
    assert with_pip.command("x") == '& "C:/Program Files/Python/python.exe" -m pip install x'
    editable = doctor._Install("1.0.0", None, Path("/src/awa"), None, has_pip=True)
    assert editable.extra_target("docs").startswith('-e "') and "[docs]" in editable.extra_target(
        "docs"
    )


# ---------------------------------------------------------------------- credentials


def test_present_credentials_are_never_printed(workdir: Path) -> None:
    report = run_doctor(settings=settings(anthropic_api_key=SECRET), cwd=workdir)
    anthropic = report.check("credentials:anthropic")
    assert anthropic is not None and anthropic.status == "ok"
    assert "ANTHROPIC_API_KEY: PRESENT" in anthropic.details
    assert SECRET not in report.render()
    assert SECRET not in json.dumps(report.to_dict())


def test_absent_credentials_warn_but_do_not_block(workdir: Path) -> None:
    report = run_doctor(settings=settings(), cwd=workdir)
    anthropic = report.check("credentials:anthropic")
    assert anthropic is not None and anthropic.status == "warn"
    assert "ABSENT" in anthropic.summary
    assert (
        anthropic.fix
        == "set ANTHROPIC_API_KEY in .env (awa env set, or awa setup) or the environment"
    )
    assert report.usable


def test_azure_needs_every_setting(workdir: Path) -> None:
    configured = settings(
        default_model="azure_openai:gpt-x",
        fast_model="azure_openai:gpt-x",
        strong_model="azure_openai:gpt-x",
        azure_openai_api_key=SECRET,
    )
    azure = run_doctor(settings=configured, cwd=workdir).check("credentials:azure_openai")
    assert azure is not None and azure.status == "warn"
    assert "AZURE_OPENAI_API_KEY: PRESENT" in azure.details
    assert "AZURE_OPENAI_ENDPOINT: ABSENT" in azure.details
    assert azure.fix is not None and "AZURE_OPENAI_DEPLOYMENT_NAME" in azure.fix


@pytest.mark.parametrize("variable", ["GOOGLE_API_KEY", "GEMINI_API_KEY"])
def test_other_providers_read_the_environment(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    fake_missing(monkeypatch, "langchain_google_genai")
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, SECRET)
    report = run_doctor(settings=settings(default_model="google_genai:gemini-x"), cwd=workdir)
    package = report.check("provider:google_genai")
    assert package is not None and package.status == "fail"
    assert package.fix is not None and "langchain-google-genai" in package.fix
    credentials = report.check("credentials:google_genai")
    assert credentials is not None and credentials.status == "ok"
    assert SECRET not in report.render()


def test_unknown_provider_is_not_checked(workdir: Path) -> None:
    report = run_doctor(settings=settings(default_model="acme:model-1"), cwd=workdir)
    unknown = report.check("credentials:acme")
    assert unknown is not None and unknown.status == "info"


# ---------------------------------------------------------------------- .env and settings


def test_env_file_is_found_and_read(workdir: Path) -> None:
    (workdir / ".env").write_text("AWA_DEFAULT_MODEL=openai:gpt-x\n", encoding="utf-8")
    report = run_doctor(cwd=workdir)
    env = report.check(".env")
    assert env is not None and env.status == "ok"
    configured = report.check("settings")
    assert configured is not None and configured.data["default_model"] == "openai:gpt-x"
    assert report.check("credentials:openai") is not None


def test_the_env_file_of_the_project_folder_is_checked_not_the_working_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.core.config
    from awa.core.config import LoadedEnvironment

    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    (project / ".env").write_text("AWA_ENV=local\n", encoding="utf-8")
    loaded = LoadedEnvironment(project_dir=project, dotenv_path=project / ".env")
    monkeypatch.setattr(awa.core.config, "current", lambda: loaded)
    monkeypatch.chdir(elsewhere)

    env = run_doctor(settings=settings()).check(".env")

    assert env is not None and env.status == "ok" and str(project) in env.summary


def test_missing_env_file_suggests_the_example(workdir: Path) -> None:
    (workdir / ".env.example").write_text("AWA_ENV=local\n", encoding="utf-8")
    env = run_doctor(settings=settings(), cwd=workdir).check(".env")
    assert env is not None and env.status == "info"
    assert env.fix is not None and ".env.example" in env.fix


def test_invalid_settings_are_summarised_without_values(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWA_CHECKPOINTER_BACKEND", "mongodb-secret-ish")
    monkeypatch.setenv("AWA_ENV", "prod")
    report = run_doctor(cwd=workdir)
    problems = report.check("settings")
    assert problems is not None and problems.status == "fail"
    assert any(line.startswith("AWA_CHECKPOINTER_BACKEND:") for line in problems.details)
    assert any(line.startswith("AWA_ENV:") for line in problems.details)
    assert "mongodb-secret-ish" not in report.render()
    assert not report.usable
    # Every other check still ran, with the defaults in place of the invalid values.
    assert report.check("credentials:anthropic") is not None


# ---------------------------------------------------------------------- postgres


def test_postgres_backend_without_dsn_fails(workdir: Path) -> None:
    check = run_doctor(settings=settings(store_backend="postgres"), cwd=workdir).check("postgres")
    assert check is not None and check.status == "fail"
    assert "AWA_POSTGRES_DSN is not set" in check.summary


@pytest.mark.slow
def test_postgres_is_only_contacted_on_request(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def connect(dsn: str) -> str | None:
        calls.append(dsn)
        return None

    monkeypatch.setattr(doctor, "_connect_postgres", connect)
    configured = settings(checkpointer_backend="postgres", postgres_dsn="postgresql://h/db")
    passive = run_doctor(settings=configured, cwd=workdir).check("postgres")
    assert passive is not None and passive.status == "ok" and not calls
    active = run_doctor(settings=configured, cwd=workdir, check_postgres=True).check("postgres")
    assert active is not None and active.status == "ok" and active.data["connected"] is True
    assert calls == ["postgresql://h/db"]
    assert run_doctor(settings=settings(), cwd=workdir).check("postgres") is None


def test_failed_postgres_connection_blocks_use(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "_connect_postgres", lambda dsn: "OperationalError: refused")
    configured = settings(postgres_dsn="postgresql://h/db")
    report = run_doctor(settings=configured, cwd=workdir, check_postgres=True)
    check = report.check("postgres")
    assert check is not None and check.status == "fail" and "refused" in check.summary
    assert not report.usable


def test_postgres_errors_are_redacted() -> None:
    dsn = "postgresql://app:hunter2secret@db.internal:5432/app"
    message = doctor._redact(f"failed for {dsn} (password hunter2secret)", doctor._dsn_secrets(dsn))
    assert "hunter2secret" not in message


# ---------------------------------------------------------------------- models


def fake_registry(model: Any) -> ModelRegistry:
    registry = ModelRegistry(settings())
    for profile in ("default", "fast", "strong"):
        registry.register(profile, model)
    return registry


@pytest.mark.slow
def test_models_are_only_called_on_request(workdir: Path) -> None:
    model = FakeChatModel(["OK"], repeat_last=True)
    registry = fake_registry(model)
    run_doctor(settings=settings(), registry=registry, cwd=workdir)
    assert model.call_count == 0
    report = run_doctor(settings=settings(), registry=registry, cwd=workdir, check_models=True)
    assert model.call_count == 3
    for profile in ("default", "fast", "strong"):
        check = report.check(f"model:{profile}")
        assert check is not None and check.status == "ok"


def test_failed_model_call_blocks_use_and_is_redacted(workdir: Path) -> None:
    def fail(call: ModelCall) -> str:
        raise RuntimeError(f"401 invalid x-api-key {SECRET}")

    report = run_doctor(
        settings=settings(anthropic_api_key=SECRET),
        registry=fake_registry(FunctionChatModel(fail)),
        cwd=workdir,
        check_models=True,
    )
    check = report.check("model:default")
    assert check is not None and check.status == "fail"
    assert "401" in check.summary and SECRET not in check.summary
    assert not report.usable


# ---------------------------------------------------------------------- report and cli


def test_report_render_and_json() -> None:
    report = DoctorReport(
        (
            doctor.DoctorCheck("python", "ok", "CPython 3.13"),
            doctor.DoctorCheck("extra:mcp", "info", "not installed", fix="pip install x"),
            doctor.DoctorCheck("credentials:openai", "warn", "ABSENT"),
        )
    )
    text = report.render()
    assert "fix: pip install x" in text
    assert text.endswith("Result: usable with 1 warning(s).\n" + LIVE_POINTER)
    text.encode("ascii")  # safe on every console code page
    data = report.to_dict()
    assert data["usable"] is True and data["warnings"] == 1
    assert [check["name"] for check in data["checks"]] == [
        "python",
        "extra:mcp",
        "credentials:openai",
    ]
    failed = DoctorReport((doctor.DoctorCheck("settings", "fail", "bad"),))
    assert "NOT usable" in failed.render()


def parse(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


@pytest.mark.parametrize(("status", "code"), [("ok", 0), ("warn", 0), ("fail", 1)])
def test_cli_exit_code_and_json(
    status: doctor.DoctorStatus,
    code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(**options: Any) -> DoctorReport:
        seen.update(options)
        return DoctorReport((doctor.DoctorCheck("python", status, "checked"),))

    monkeypatch.setattr(doctor, "run_doctor", fake_run)
    args = parse(["doctor", "--json", "--check-postgres"])
    assert args.handler(args) == code
    assert seen == {"check_models": False, "check_postgres": True, "check_langfuse": False}
    output = json.loads(capsys.readouterr().out)
    assert output["usable"] is (code == 0)


def test_cli_prints_text_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        doctor, "run_doctor", lambda **_: DoctorReport((doctor.DoctorCheck("awa", "ok", "fine"),))
    )
    args = parse(["doctor"])
    assert args.handler(args) == 0
    assert capsys.readouterr().out.startswith("awa doctor")


# ------------------------------------------------------------------ Azure OpenAI


def azure_settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "default_model": "azure_openai:model-label",
        "fast_model": "azure_openai:model-label",
        "strong_model": "azure_openai:model-label",
        "azure_openai_endpoint": "https://res.openai.azure.com/",
        "azure_openai_api_key": SECRET,
        "azure_deployment": "my-deployment",
    }
    return settings(**{**base, **values})


def test_azure_does_not_need_an_api_version(workdir: Path) -> None:
    report = run_doctor(settings=azure_settings(), cwd=workdir)

    credentials = report.check("credentials:azure_openai")
    assert credentials is not None and credentials.status == "ok"
    assert not any("OPENAI_API_VERSION" in detail for detail in credentials.details)
    azure = report.check("azure_openai")
    assert azure is not None and azure.status == "ok"
    assert azure.data["route"] == "v1" and azure.data["auth"] == "key"
    assert "route: v1 API (no API version needed)" in azure.details
    assert SECRET not in report.render()


def test_a_dated_api_version_is_reported_with_the_way_to_the_v1_api(workdir: Path) -> None:
    report = run_doctor(settings=azure_settings(azure_api_version="test-api-version"), cwd=workdir)

    azure = report.check("azure_openai")
    assert azure is not None and azure.status == "ok" and azure.data["route"] == "dated"
    assert "route: dated API version test-api-version" in azure.details
    assert any("remove OPENAI_API_VERSION" in detail for detail in azure.details)


def test_a_preview_api_version_is_flagged(workdir: Path) -> None:
    report = run_doctor(
        settings=azure_settings(azure_api_version="test-api-version-preview"), cwd=workdir
    )

    azure = report.check("azure_openai")
    assert azure is not None and azure.status == "warn" and "preview" in azure.summary


def test_entra_replaces_the_key_in_the_needed_variables(workdir: Path) -> None:
    entra = {
        "azure_auth": "entra",
        "azure_openai_api_key": None,
        "azure_tenant_id": "tenant",
        "azure_client_id": "client",
        "azure_client_secret": SECRET,
    }
    report = run_doctor(settings=azure_settings(**entra), cwd=workdir)

    credentials = report.check("credentials:azure_openai")
    assert credentials is not None and credentials.status == "ok"
    assert "AZURE_CLIENT_SECRET: PRESENT" in credentials.details
    assert not any("AZURE_OPENAI_API_KEY" in detail for detail in credentials.details)
    azure = report.check("azure_openai")
    assert azure is not None and azure.data["auth"] == "entra"
    assert SECRET not in report.render() and SECRET not in json.dumps(report.to_dict())

    without_secret = azure_settings(**{**entra, "azure_client_secret": None})
    credentials = run_doctor(settings=without_secret, cwd=workdir).check("credentials:azure_openai")
    assert credentials is not None and credentials.status == "warn"
    assert credentials.fix is not None and "AZURE_CLIENT_SECRET" in credentials.fix


def test_entra_variables_without_the_entra_switch_are_flagged(workdir: Path) -> None:
    report = run_doctor(
        settings=azure_settings(
            azure_openai_api_key=None,
            azure_tenant_id="tenant",
            azure_client_id="client",
            azure_client_secret=SECRET,
        ),
        cwd=workdir,
    )

    azure = report.check("azure_openai")
    assert azure is not None and azure.status == "warn"
    assert "AZURE_OPENAI_AUTH" in azure.summary


def test_an_endpoint_with_a_path_is_flagged(workdir: Path) -> None:
    endpoint = "https://res.services.ai.azure.com/api/projects/demo"
    report = run_doctor(settings=azure_settings(azure_openai_endpoint=endpoint), cwd=workdir)

    azure = report.check("azure_openai")
    assert azure is not None and azure.status == "warn" and "has a path" in azure.summary


def test_no_azure_lines_when_no_profile_uses_azure(workdir: Path) -> None:
    report = run_doctor(settings=settings(), cwd=workdir)

    assert report.check("azure_openai") is None


@pytest.mark.model_requests_allowed
def test_a_failed_azure_call_comes_with_a_fix(workdir: Path) -> None:
    from _helpers.fake_aoai import FakeAoai

    with FakeAoai(deployments=["my-deployment"], key_auth_enabled=False) as aoai:
        report = run_doctor(
            settings=azure_settings(
                azure_openai_endpoint=aoai.url, azure_openai_api_key="fake-key"
            ),
            cwd=workdir,
            check_models=True,
        )

    probe = report.check("model:default")
    assert probe is not None and probe.status == "fail"
    assert probe.fix is not None and "AZURE_OPENAI_AUTH=entra" in probe.fix
    assert "fake-key" not in report.render()


@pytest.mark.model_requests_allowed
def test_azure_with_entra_answers_the_probe(workdir: Path) -> None:
    from _helpers.fake_aoai import FakeAoai

    with FakeAoai(deployments=["my-deployment"], key_auth_enabled=False) as aoai:
        entra = azure_settings(
            azure_openai_endpoint=aoai.url,
            azure_openai_api_key=None,
            azure_auth="entra",
            azure_tenant_id=aoai.tenant,
            azure_client_id=aoai.client_id,
            azure_client_secret=aoai.client_secret,
            azure_authority_host=aoai.url,
        )
        report = run_doctor(settings=entra, cwd=workdir, check_models=True)

    probe = report.check("model:default")
    assert probe is not None and probe.status == "ok"
    assert "azure_openai:my-deployment replied" in probe.summary
    assert aoai.client_secret not in report.render()


@pytest.mark.model_requests_allowed
def test_the_probe_prints_the_model_the_service_reports(workdir: Path) -> None:
    from _helpers.fake_aoai import FakeAoai

    # A deployment name that means nothing: what the service answers is what is shown.
    with FakeAoai(deployments=["x1"], reported_models={"x1": "model-reported-by-x1"}) as aoai:
        report = run_doctor(
            settings=azure_settings(
                azure_openai_endpoint=aoai.url,
                azure_openai_api_key="fake-key",
                azure_deployment="x1",
            ),
            cwd=workdir,
            check_models=True,
        )

    probe = report.check("model:default")
    assert probe is not None and probe.status == "ok"
    assert "the service reports model model-reported-by-x1" in probe.summary
    assert probe.data["service_model"] == "model-reported-by-x1"


# ---------------------------------------------------------------------- Windows long paths


def _wheel_install(location: Path) -> doctor._Install:
    return doctor._Install("1.2.0", location, None, location, True)


def test_deep_windows_install_folder_warns_with_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    deep = Path("C:/" + "/".join(["a_long_folder_name"] * 8) + "/Lib/site-packages/awa")
    check = _wheel_install(deep).check()
    assert check.status == "warn"
    assert "260" in check.summary
    assert check.fix is not None and "short folder" in check.fix and "long paths" in check.fix
    assert "C:\\awa" in check.fix and check.fix.isprintable()


def test_short_windows_install_folder_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    short = Path("C:/awa/env/Lib/site-packages/awa")
    assert _wheel_install(short).check().status == "ok"


def test_deep_install_folder_is_no_problem_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    deep = Path("/" + "/".join(["a_long_folder_name"] * 8) + "/lib/site-packages/awa")
    assert _wheel_install(deep).check().status == "ok"


def test_credentials_come_from_the_checked_project_not_the_working_folder(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("AZURE_OPENAI_ENDPOINT=https://elsewhere.invalid\n")
    monkeypatch.chdir(elsewhere)
    configured = settings(default_model="azure_openai:gpt-x", azure_openai_api_key=SECRET)
    azure = run_doctor(settings=configured, cwd=workdir).check("credentials:azure_openai")
    assert azure is not None and "AZURE_OPENAI_ENDPOINT: ABSENT" in azure.details
    (workdir / ".env").write_text("AZURE_OPENAI_ENDPOINT=https://checked.invalid\n")
    azure = run_doctor(settings=configured, cwd=workdir).check("credentials:azure_openai")
    assert azure is not None and "AZURE_OPENAI_ENDPOINT: PRESENT" in azure.details


def test_the_langfuse_keys_never_follow_a_redirect() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    stolen: list[str] = []

    class Thief(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            stolen.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    thief = ThreadingHTTPServer(("127.0.0.1", 0), Thief)
    target = f"http://127.0.0.1:{thief.server_address[1]}/steal"

    class Langfuse(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            moved = self.path.startswith("/api/public/projects")
            self.send_response(302 if moved else 200)
            if moved:
                self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    langfuse = ThreadingHTTPServer(("127.0.0.1", 0), Langfuse)
    for server in (thief, langfuse):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{langfuse.server_address[1]}"
        problem = doctor._contact_langfuse(base, "pk-lf-1", "sk-lf-1")
        refused = doctor._contact_langfuse("file:///etc", "pk", "sk")
    finally:
        thief.shutdown()
        langfuse.shutdown()

    assert problem is not None and "302" in problem and stolen == []
    assert refused is not None and "must start with http" in refused
