"""`awa setup` and `awa.setup()`: sections, the Compose file, Docker through a fake runner.

Docker itself is never started here: a fake runner records the commands. Before: no setup.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from _helpers.polling import SUBPROCESS_TIMEOUT
from awa.cli import main
from awa.core.dotenv import OverwriteRefused, read_env_file
from awa.project_setup import SetupError, SetupPlan, setup
from awa.project_setup.services import (
    CommandResult,
    Services,
    ServicesError,
    compose_text,
    docker_binary,
    password,
    project_name,
)

SENTINEL = "sk-setup-sentinel-5e1d"
PORTS = {"db": 55432, "tracing": 53000}


class Runner:
    """Records docker commands and answers like a healthy Docker."""

    def __init__(self, *, running: bool = True, compose: str = "2.31.0") -> None:
        self.commands: list[list[str]] = []
        self.running = running
        self.compose = compose

    def __call__(self, command: Sequence[str], timeout: float) -> CommandResult:
        self.commands.append(list(command))
        if command[1:3] == ["info", "--format"]:
            if not self.running:
                return CommandResult(1, "", "Cannot connect to the Docker daemon")
            return CommandResult(0, "8000000000" if "MemTotal" in command[3] else "27.0.0")
        if command[1:3] == ["compose", "version"]:
            return CommandResult(0, self.compose)
        if "ps" in command:
            row = {"Service": "postgres", "State": "running", "Health": "healthy", "Publishers": []}
            return CommandResult(0, json.dumps(row))
        return CommandResult(0)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "my desk & co"
    folder.mkdir()
    (folder / "blueprint.yaml").write_text("name: d\nagents: [{name: a}]\n", encoding="utf-8")
    monkeypatch.chdir(folder)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWA_POSTGRES_DSN"):
        monkeypatch.delenv(name, raising=False)
    return folder


def plan_for(project: Path, runner: Runner | None = None) -> SetupPlan:
    services = Services(project, project / ".env", runner=runner or Runner(), docker="docker.exe")
    services.port_free = lambda port: True  # type: ignore[attr-defined]  # no bind test here
    return SetupPlan(project, services=services, call_model=lambda model: None)


# ---------------------------------------------------------------------- sections


def test_state_moves_from_missing_to_set_to_tested(project: Path) -> None:
    plan = plan_for(project)
    assert plan.section_status("provider").state == "missing"
    plan.apply("provider", {"provider": "openai", "key": SENTINEL})
    assert plan.section_status("provider").state == "set"
    assert plan.section_status("test").state == "set"
    plan.test()
    assert plan.section_status("test").state == "tested"
    assert read_env_file(project / ".env")["OPENAI_API_KEY"] == SENTINEL
    # The only provider set up: its models follow from the key, no profile is written.
    assert "AWA_DEFAULT_MODEL" not in read_env_file(project / ".env")


def test_a_provider_after_another_in_the_order_writes_its_models(project: Path) -> None:
    (project / ".env").write_text("ANTHROPIC_API_KEY=k-1\n", encoding="utf-8")
    plan_for(project).apply("provider", {"provider": "openai", "key": SENTINEL})
    env = read_env_file(project / ".env")
    assert env["AWA_DEFAULT_MODEL"] == "openai:gpt-6.1-sol"
    assert env["AWA_FAST_MODEL"] == "openai:gpt-6-luna"


def test_choosing_the_first_provider_again_drops_the_other_providers_models(project: Path) -> None:
    (project / ".env").write_text("ANTHROPIC_API_KEY=k-1\n", encoding="utf-8")
    plan = plan_for(project)
    plan.apply("provider", {"provider": "openai", "key": SENTINEL})

    plan.apply("provider", {"provider": "anthropic"}, confirm_overwrite=True)

    env = read_env_file(project / ".env")
    assert not {"AWA_DEFAULT_MODEL", "AWA_FAST_MODEL", "AWA_STRONG_MODEL"} & set(env)
    assert plan.provider() is not None and plan.provider().name == "anthropic"  # type: ignore[union-attr]


def test_a_failed_test_call_is_recorded_in_one_sentence(project: Path) -> None:
    def fail(model: str) -> None:
        raise RuntimeError(f"401 invalid key {SENTINEL}")

    plan = SetupPlan(project, services=plan_for(project).services, call_model=fail)
    plan.apply("provider", {"provider": "anthropic", "key": SENTINEL})
    with pytest.raises(SetupError, match="test call"):
        plan.test()
    assert plan.section_status("test").state == "failed"
    assert SENTINEL not in (project / ".awa" / "setup_state.json").read_text(encoding="utf-8")


def test_azure_only_is_preselected(project: Path) -> None:
    (project / ".env").write_text(
        "AZURE_OPENAI_API_KEY=k-1\nAZURE_OPENAI_ENDPOINT=https://r.openai.azure.com/\n"
        "AZURE_OPENAI_DEPLOYMENT_NAME=dep\n",
        encoding="utf-8",
    )
    plan = plan_for(project)
    assert plan.provider().name == "azure_openai"
    assert "Azure OpenAI" in plan.section_status("provider").detail


def test_an_overwrite_needs_confirmation_and_keeps_one_backup(project: Path) -> None:
    plan = plan_for(project)
    plan.apply("provider", {"provider": "anthropic", "key": "first-key-1"})
    with pytest.raises(OverwriteRefused):
        plan.apply("provider", {"provider": "anthropic", "key": "second-key-2"})
    plan.apply("provider", {"provider": "anthropic", "key": "second-key-2"}, confirm_overwrite=True)
    assert read_env_file(project / ".env")["ANTHROPIC_API_KEY"] == "second-key-2"
    assert read_env_file(project / ".env.bak")["ANTHROPIC_API_KEY"] == "first-key-1"


def test_postgres_in_docker_gets_a_generated_password_and_a_local_dsn(project: Path) -> None:
    plan = plan_for(project)
    plan.apply("database", {"database": "postgres-docker"})
    env = read_env_file(project / ".env")
    assert re.fullmatch(r"[A-Za-z0-9]{32}", env["AWA_SETUP_DB_PASSWORD"])
    assert env["AWA_POSTGRES_DSN"] == (
        f"postgresql://awa:{env['AWA_SETUP_DB_PASSWORD']}@127.0.0.1:{env['AWA_SETUP_DB_PORT']}/awa"
    )
    plan.apply("database", {"database": "postgres-docker"})  # again: the password stays
    assert read_env_file(project / ".env")["AWA_SETUP_DB_PASSWORD"] == env["AWA_SETUP_DB_PASSWORD"]
    assert plan.section_status("database").detail == "local PostgreSQL in Docker"


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_a_port_that_is_no_number_is_a_setup_error(project: Path, port: str) -> None:
    (project / ".env").write_text(f"AWA_SETUP_DB_PORT={port}\n", encoding="utf-8")
    plan = plan_for(project)

    with pytest.raises(SetupError, match="port number"):
        plan.apply("database", {"database": "postgres-docker"})


def test_an_own_database_is_saved_only_when_it_answers(project: Path) -> None:
    plan = plan_for(project)

    def refuse(dsn: str, timeout: float) -> None:
        raise OSError("refused")

    url = f"postgresql://u:{SENTINEL}@db.example.com/x"
    with pytest.raises(SetupError, match="nothing was saved"):
        plan.apply("database", {"database": "url", "url": url, "connect": refuse})
    assert not (project / ".env").exists()
    plan.apply("database", {"database": "url", "url": url, "connect": lambda d, t: None})
    assert read_env_file(project / ".env")["AWA_POSTGRES_DSN"] == url


def test_enter_in_the_dialog_keeps_an_own_database_and_langfuse(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.cli._setup import _ask

    text = (
        "ANTHROPIC_API_KEY=k\n"
        "AWA_POSTGRES_DSN=postgresql://u:p@db.example.com/x\n"
        "AWA_LANGFUSE_TRACING=true\nLANGFUSE_PUBLIC_KEY=pk\nLANGFUSE_SECRET_KEY=sk\n"
    )
    (project / ".env").write_text(text, encoding="utf-8")
    plan = plan_for(project)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert (plan.current_choice("database"), plan.current_choice("tracing")) == ("url", "url")
    assert _ask(plan, yes=False) == 0
    assert (project / ".env").read_text(encoding="utf-8") == text


def test_tracing_in_docker_writes_keys_and_the_switch(project: Path) -> None:
    plan_for(project).apply("tracing", {"tracing": "langfuse-docker"})
    env = read_env_file(project / ".env")
    assert env["AWA_LANGFUSE_TRACING"] == "true"
    assert env["LANGFUSE_PUBLIC_KEY"].startswith("pk-lf-")
    assert env["LANGFUSE_BASE_URL"] == f"http://localhost:{env['AWA_SETUP_LANGFUSE_PORT']}"
    assert re.fullmatch(r"[0-9a-f]{64}", env["AWA_SETUP_LANGFUSE_ENCRYPTION_KEY"])


def test_the_notebook_entry_point_writes_only_the_project_env(project: Path) -> None:
    plan = setup(provider="anthropic", api_key=SENTINEL, project=project)
    assert [s.section for s in plan.status()][:3] == ["provider", "test", "database"]
    assert read_env_file(project / ".env") == {"ANTHROPIC_API_KEY": SENTINEL}
    assert SENTINEL not in plan._repr_html_()
    state = (project / ".awa" / "setup_state.json").read_text(encoding="utf-8")
    assert SENTINEL not in state and json.loads(state)["compose_project"] == project_name(project)


# ---------------------------------------------------------------------- the compose file


def test_every_port_is_local_and_every_service_healthchecked_and_pinned() -> None:
    from awa.core.settings_registry import SETUP_ENV

    text = compose_text("awa-x-12345678", PORTS)
    spec = yaml.safe_load(text)
    assert spec["name"] == "awa-x-12345678" and "__" not in text
    assert "127.0.0.1:55432:5432" in spec["services"]["postgres"]["ports"]
    referenced = set(re.findall(r"\$\{(AWA_SETUP_\w+)\}", text))
    assert referenced == {v for v in SETUP_ENV.values() if v.startswith("AWA_SETUP_")} - {
        "AWA_SETUP_DB_PORT",
        "AWA_SETUP_LANGFUSE_PORT",
    }
    for name, service in spec["services"].items():
        assert all(port.startswith("127.0.0.1:") for port in service.get("ports", [])), name
        assert "healthcheck" in service, name
        image = service["image"]
        assert "@sha256:" in image or (":" in image and not image.endswith(":latest")), name
        assert "env_file" not in service, name
        assert service["labels"] == {"io.awa.managed": "true", "io.awa.project": "12345678"}


def test_the_compose_file_holds_no_secret(project: Path) -> None:
    plan = plan_for(project)
    plan.apply("database", {"database": "postgres-docker"})
    plan.apply("tracing", {"tracing": "langfuse-docker"})
    plan.start(["db"])
    text = (project / ".awa" / "compose.services.yaml").read_text(encoding="utf-8")
    for name, value in read_env_file(project / ".env").items():
        if name.endswith(("PASSWORD", "SECRET", "KEY", "SALT", "DSN")):
            assert value not in text, name
    assert "${AWA_SETUP_DB_PASSWORD}" in text
    assert yaml.safe_load(text)["name"] == project_name(project)


def test_the_project_name_differs_per_folder(tmp_path: Path) -> None:
    first, second = tmp_path / "a" / "desk", tmp_path / "b" / "desk"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assert project_name(first) != project_name(second)
    assert re.fullmatch(r"awa-desk-[0-9a-f]{8}", project_name(first))


def test_passwords_are_letters_and_digits() -> None:
    assert all(re.fullmatch(r"[A-Za-z0-9]{32}", password()) for _ in range(50))


def test_a_script_shim_is_refused_as_docker() -> None:
    with pytest.raises(ServicesError, match="shim"):
        docker_binary(lambda name: r"C:\tools\docker.cmd", platform="win32")
    with pytest.raises(ServicesError, match="not installed"):
        docker_binary(lambda name: None)
    assert docker_binary(lambda name: "/usr/bin/docker", platform="linux") == "/usr/bin/docker"


def test_commands_are_lists_scoped_to_this_project(project: Path) -> None:
    runner = Runner()
    plan = plan_for(project, runner)
    plan.apply("database", {"database": "postgres-docker"})
    plan.start(["db"])
    up = runner.commands[-1]
    assert up[:3] == ["docker.exe", "compose", "-p"] and up[3] == project_name(project)
    assert up[up.index("-f") + 1].endswith("compose.services.yaml")
    assert up[up.index("--env-file") + 1] == str(project / ".env")
    assert "up" in up and "--wait" in up and "my desk & co" in up[up.index("-f") + 1]


def test_docker_not_running_is_one_sentence(project: Path) -> None:
    plan = plan_for(project, Runner(running=False))
    plan.apply("database", {"database": "postgres-docker"})
    with pytest.raises(ServicesError, match="not running"):
        plan.start(["db"])
    with pytest.raises(ServicesError, match="Compose 2.20"):
        plan_for(project, Runner(compose="2.12.2")).start(["db"])


def test_langfuse_memory_warning(project: Path) -> None:
    class Small(Runner):
        def __call__(self, command: Sequence[str], timeout: float) -> CommandResult:
            if "{{.MemTotal}}" in command:
                return CommandResult(0, str(2 * 1024**3))
            return super().__call__(command, timeout)

    services = Services(project, project / ".env", runner=Small(), docker="docker")
    assert "4 GB" in (services.langfuse_memory_warning() or "")


# ---------------------------------------------------------------------- the command


class NoTerminal(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_without_a_terminal_and_options_setup_prints_and_exits(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", NoTerminal(""))
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "provider  missing" in out and "awa setup --provider" in out
    assert not (project / ".env").exists()


def test_setup_rejects_vector_option(project: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["setup", "--vector", "off"])

    assert raised.value.code == 2
    assert [state.section for state in plan_for(project).status()] == [
        "provider",
        "test",
        "database",
        "tracing",
    ]


def test_key_stdin_with_bom_and_crlf(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbf" + SENTINEL.encode() + b"\r\n"), "utf-8")
    monkeypatch.setattr("sys.stdin", data)
    assert main(["setup", "--provider", "anthropic", "--key-stdin"]) == 0
    assert read_env_file(project / ".env")["ANTHROPIC_API_KEY"] == SENTINEL
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err


def test_down_with_volumes_needs_the_typed_name(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", NoTerminal(""))
    assert main(["setup", "services", "down", "--volumes"]) == 3
    assert main(["setup", "services", "down", "--volumes", "--confirm", "wrong"]) == 3
    assert project_name(project) in capsys.readouterr().err


def test_importing_awa_asks_nothing_and_writes_nothing(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    code = "import awa, awa.project_setup, awa.config; print(awa.setup.__name__)"
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "setup"
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("argv", "selector"),
    [
        (["--key-stdin"], "--provider"),
        (["--endpoint", "https://x.openai.azure.com"], "--provider"),
        (["--database-url-stdin"], "--database"),
        (["--langfuse-url", "https://lf.example.com"], "--tracing"),
    ],
)
def test_an_option_without_its_selector_is_refused(
    project: Path, capsys: pytest.CaptureFixture[str], argv: list[str], selector: str
) -> None:
    assert main(["setup", *argv]) == 1
    assert f"needs {selector}" in capsys.readouterr().err
