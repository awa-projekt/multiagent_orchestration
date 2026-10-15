"""One `.env` loader for every command, `awa env`, and `write_env_file(mode="merge")`."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
from pathlib import Path

import pytest

from awa.automation import Mailbox, Webhook
from awa.automation._support import resolve_traces
from awa.automation.store import InMemoryRunStore
from awa.cli import main
from awa.cli._automation import _dsn
from awa.cli._common import prepare_environment
from awa.core import config, dotenv
from awa.core.config import (
    current,
    env_value,
    find_project,
    load_environment,
    mask,
    source_of,
    unload_environment,
)
from awa.core.dotenv import OverwriteRefused, read_env_file, remove_env_names, write_env_file
from awa.core.settings import Settings
from awa.core.settings_registry import REGISTRY, is_denied
from awa.errors import ConfigurationError
from awa.models import ModelRegistry
from awa.server._builds import export_files

SENTINEL = "sk-sentinel-4c1f9e"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "blueprint.yaml").write_text("name: demo\nagents:\n  - name: a\n", encoding="utf-8")
    monkeypatch.chdir(folder)
    for name in (
        "HOOK_SECRET",
        "MY_IMAP_USER",
        "MY_IMAP_PASSWORD",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "OLLAMA_HOST",
        "PYTHONPATH_X",
        "MYKEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AWA_PROJECT_DIR", raising=False)

    monkeypatch.setitem(Settings.model_config, "env_file", ".env")  # the suite turns it off
    return folder


def write_env(folder: Path, text: str) -> Path:
    path = folder / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------- values read from .env


def test_postgres_dsn_only_in_dotenv_reaches_the_run_store_options(project: Path) -> None:
    write_env(project, "AWA_POSTGRES_DSN=postgresql://u:pw@db/x\n")

    assert _dsn(argparse.Namespace(dsn=None)) == "postgresql://u:pw@db/x"


def test_trigger_secret_env_is_read_from_dotenv(project: Path) -> None:
    write_env(project, f"HOOK_SECRET={SENTINEL}\n")
    assert Webhook("hook", secret_env="HOOK_SECRET").secrets() == (SENTINEL,)


def test_mailbox_login_is_read_from_dotenv(project: Path) -> None:
    write_env(project, "MY_IMAP_USER=me\nMY_IMAP_PASSWORD=pw\n")
    logins: list[tuple[str, str]] = []

    class ImapStandIn:
        def login(self, user: str, password: str) -> None:
            logins.append((user, password))

        def select(self, folder: str) -> tuple[str, list[bytes]]:
            return "OK", [b"0"]

        def logout(self) -> None:
            pass

    box = Mailbox(
        "imap.example.com",
        username_env="MY_IMAP_USER",
        password_env="MY_IMAP_PASSWORD",
        connect=ImapStandIn,  # type: ignore[arg-type]  # a stand-in IMAP connection
    )
    box._connection()
    assert logins == [("me", "pw")]


def test_trace_level_is_read_from_dotenv(project: Path) -> None:
    write_env(project, "AWA_TRACE_LEVEL=loud\n")
    with pytest.raises(ConfigurationError, match="AWA_TRACE_LEVEL"):
        resolve_traces(None, InMemoryRunStore(), None)


@pytest.mark.parametrize(
    ("model", "variable", "argument"),
    [
        ("google_genai:gemini-3.8-flash", "GOOGLE_API_KEY", "api_key"),
        ("mistralai:mistral-small-4-0-26-03", "MISTRAL_API_KEY", "api_key"),
        ("groq:llama-3.1-8b-instant", "GROQ_API_KEY", "api_key"),
        ("ollama:gpt-oss:20b", "OLLAMA_HOST", "base_url"),
    ],
)
def test_other_provider_keys_in_dotenv_reach_the_model(
    project: Path, monkeypatch: pytest.MonkeyPatch, model: str, variable: str, argument: str
) -> None:
    write_env(project, f"{variable}={SENTINEL}\n")
    seen: dict[str, object] = {}

    def fake_init(name: str, **kwargs: object) -> object:
        seen.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr("awa.models.registry.init_chat_model", fake_init)
    with pytest.raises(Exception, match="stop|Cannot create"):
        ModelRegistry(Settings(_env_file=None)).create(model)
    assert seen.get(argument) == SENTINEL


def test_api_key_env_of_awa_mcp_serve_is_read_from_dotenv(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    write_env(project, "MYKEY=from-dotenv-key-1234\n")
    captured: dict[str, object] = {}

    class Served:
        system = type("S", (), {"name": "demo"})()

    class Stop(Exception):
        pass

    def fake_serve(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise Stop

    monkeypatch.setattr(
        "awa.server.mcp.build_blueprint_system",
        lambda *a, **k: (Served(), [], type("B", (), {"name": "demo"})()),
    )
    monkeypatch.setattr("awa.server.mcp.serve_as_mcp", fake_serve)
    code = main(["mcp", "serve", "blueprint.yaml", "--transport", "http", "--api-key-env", "MYKEY"])
    assert code == 1  # the stand-in server stopped
    assert captured["api_key"] == "from-dotenv-key-1234"


# ---------------------------------------------------------------------- precedence


def test_real_environment_wins_over_dotenv(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_DEFAULT_MODEL", "openai:gpt-6-luna")
    write_env(project, "AWA_DEFAULT_MODEL=anthropic:claude-opus-5-5\n")
    loaded = load_environment(export=True)
    assert env_value("AWA_DEFAULT_MODEL") == "openai:gpt-6-luna"
    assert source_of("AWA_DEFAULT_MODEL").origin == "env"
    assert "AWA_DEFAULT_MODEL" in loaded.shadowed


def test_empty_real_value_counts_as_set(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    write_env(project, f"ANTHROPIC_API_KEY={SENTINEL}\n")
    load_environment(export=True)
    assert os.environ["ANTHROPIC_API_KEY"] == ""
    assert source_of("ANTHROPIC_API_KEY").origin == "env_empty"


def test_dotenv_source_has_the_line(project: Path) -> None:
    write_env(project, "# models\n\nAWA_FAST_MODEL=openai:gpt-6-luna\n")
    source = source_of("AWA_FAST_MODEL")
    assert (source.origin, source.label) == ("dotenv", ".env:3")
    assert source_of("AWA_STRONG_MODEL").label == "default"


def test_reload_after_a_change_reexports_only_its_own_names(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWA_FAST_MODEL", raising=False)
    path = write_env(project, "AWA_FAST_MODEL=one\n")
    load_environment(export=True)
    assert os.environ["AWA_FAST_MODEL"] == "one"
    path.write_text("AWA_FAST_MODEL=two\n", encoding="utf-8")
    os.utime(path, (1, 1))
    load_environment(export=True)
    assert os.environ["AWA_FAST_MODEL"] == "two"
    unload_environment()
    assert "AWA_FAST_MODEL" not in os.environ


def test_unload_keeps_names_someone_else_changed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWA_FAST_MODEL", raising=False)
    write_env(project, "AWA_FAST_MODEL=one\n")
    load_environment(export=True)
    monkeypatch.setenv("AWA_FAST_MODEL", "mine")
    unload_environment()
    assert os.environ["AWA_FAST_MODEL"] == "mine"


# ---------------------------------------------------------------------- discovery


def test_a_subfolder_finds_the_parents_blueprint(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = project / "evals" / "deep"
    inner.mkdir(parents=True)
    monkeypatch.chdir(inner)
    assert find_project() == project.resolve()
    write_env(project, "AWA_FAST_MODEL=openai:gpt-6-luna\n")
    assert env_value("AWA_FAST_MODEL") == "openai:gpt-6-luna"


def test_home_and_other_repositories_are_not_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    work = home / "node-app" / "src"
    work.mkdir(parents=True)
    (home / ".env").write_text("AWA_FAST_MODEL=from-home\n", encoding="utf-8")
    (home / ".awa").mkdir()
    (home / "node-app" / ".git").mkdir()
    (home / "node-app" / ".env").write_text("AWA_FAST_MODEL=from-node\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(work)
    assert find_project() == work.resolve()
    assert current().dotenv_path is None
    monkeypatch.chdir(home)
    assert current().dotenv_path is None  # the home folder is never a project


def test_project_option_and_variable_win(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert find_project(project=other) == other.resolve()
    monkeypatch.setenv("AWA_PROJECT_DIR", str(other))
    assert find_project() == other.resolve()


def test_pyproject_tool_awa_is_a_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n[tool.awa]\n", encoding="utf-8")
    monkeypatch.chdir(root / "sub")
    assert find_project() == root.resolve()


# ---------------------------------------------------------------------- export rules


def test_only_allowed_names_are_exported(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGSMITH_TRACING", "SOMETHING_ELSE", "PYTHONPATH_X"):
        monkeypatch.delenv(name, raising=False)
    old_path = os.environ.get("PYTHONSTARTUP")
    write_env(
        project,
        "LANGSMITH_TRACING=true\nSOMETHING_ELSE=1\nPYTHONSTARTUP=evil.py\nHTTPS_PROXY=http://x\n",
    )
    loaded = load_environment(export=True, allow=["PYTHONSTARTUP", "HTTPS_PROXY"])
    assert os.environ.get("LANGSMITH_TRACING") == "true"  # an SDK name awa knows
    assert "SOMETHING_ELSE" not in os.environ
    assert os.environ.get("PYTHONSTARTUP") == old_path  # denied even when allowed
    assert {"SOMETHING_ELSE", "PYTHONSTARTUP"} <= loaded.not_exported


def test_allowed_names_may_come_from_a_generator(project: Path) -> None:
    write_env(project, "MYKEY=value\n")
    load_environment(export=True, allow=(name for name in ["MYKEY"]))
    assert os.environ["MYKEY"] == "value"
    unload_environment()


def test_declared_names_of_the_blueprint_are_exported(project: Path) -> None:
    (project / "blueprint.yaml").write_text(
        "name: demo\nagents:\n  - name: a\ntriggers:\n  - name: hook\n"
        "    webhook: {secret_env: HOOK_SECRET}\n",
        encoding="utf-8",
    )
    write_env(project, f"HOOK_SECRET={SENTINEL}\n")
    loaded = load_environment(export=True)
    assert os.environ.get("HOOK_SECRET") == SENTINEL
    assert "HOOK_SECRET" in loaded.declared
    assert mask("HOOK_SECRET", SENTINEL) == "set"


def test_a_plain_env_key_declares_no_variable(project: Path) -> None:
    (project / "blueprint.yaml").write_text(
        "name: demo\ndefaults:\n  env: production\nagents:\n  - name: a\n", encoding="utf-8"
    )
    assert "production" not in load_environment().declared


def test_library_mode_leaves_os_environ_unchanged(project: Path) -> None:
    write_env(project, "AWA_FAST_MODEL=openai:gpt-6-luna\nOPENAI_API_KEY=k-1234\n")
    before = dict(os.environ)
    settings = Settings()  # the project .env, read, not exported
    assert settings.fast_model == "openai:gpt-6-luna"
    assert dict(os.environ) == before


def test_settings_env_file_argument_is_seen_by_triggers(tmp_path: Path, project: Path) -> None:
    other = tmp_path / "prod.env"
    other.write_text(f"HOOK_SECRET={SENTINEL}\n", encoding="utf-8")
    Settings(_env_file=other)
    assert Webhook("hook", secret_env="HOOK_SECRET").secrets() == (SENTINEL,)


def test_settings_env_file_argument_keeps_the_names_the_cli_exported(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.core.settings import Settings

    monkeypatch.delenv("AWA_FAST_MODEL", raising=False)
    write_env(project, "AWA_FAST_MODEL=openai:fast\n")
    load_environment(export=True)
    other = tmp_path / "prod.env"
    other.write_text(f"HOOK_SECRET={SENTINEL}\n", encoding="utf-8")
    Settings(_env_file=other)
    assert os.environ["AWA_FAST_MODEL"] == "openai:fast"
    unload_environment()
    assert "AWA_FAST_MODEL" not in os.environ


# ---------------------------------------------------------------------- typos


def test_a_typo_warns_with_a_suggestion(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AWA_DEFUALT_MODEL", "x")
    write_env(project, "OPENAI_API_KEY=k\nANTHROPIC_API_KEY=k\nLANGSMITH_API_KEY=k\n")
    loaded = load_environment(export=True)
    lines = config.warn_unknown(loaded)
    assert lines == [
        "warning: AWA_DEFUALT_MODEL is not a known setting (did you mean AWA_DEFAULT_MODEL?)"
    ]
    assert config.warn_unknown(loaded) == []  # once per process
    assert "x" not in capsys.readouterr().err.split()


def test_strict_settings_turn_a_typo_into_exit_78(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AWA_DEFUALT_MODEL", "x")
    monkeypatch.setenv("AWA_STRICT_SETTINGS", "1")
    assert main(["cache", "stats"]) == 78
    assert "AWA_DEFUALT_MODEL" in capsys.readouterr().err


# ---------------------------------------------------------------------- providers


def test_azure_only_resolves_to_azure_and_never_asks_for_anthropic(project: Path) -> None:
    write_env(
        project,
        "AZURE_OPENAI_API_KEY=k-1234\nAZURE_OPENAI_ENDPOINT=https://r.openai.azure.com/\n"
        "AZURE_OPENAI_DEPLOYMENT=my-dep\nAZURE_OPENAI_DEPLOYMENT_FAST=my-fast\n",
    )
    settings = Settings()
    assert settings.default_model == "azure_openai:my-dep"
    assert settings.fast_model == "azure_openai:my-fast"
    assert settings.strong_model == "azure_openai:my-dep"
    assert settings.detected_provider == "azure_openai"


def test_explicit_profiles_are_kept(project: Path) -> None:
    write_env(project, "OPENAI_API_KEY=k-1234\nAWA_DEFAULT_MODEL=openai:gpt-6.1-sol\n")
    settings = Settings()
    assert settings.default_model == "openai:gpt-6.1-sol"
    assert settings.fast_model == "openai:gpt-6-luna"


def test_anthropic_key_keeps_the_anthropic_defaults(project: Path) -> None:
    write_env(project, "ANTHROPIC_API_KEY=k\nOPENAI_API_KEY=k\n")
    assert Settings().default_model.startswith("anthropic:")


def test_the_cli_exports_for_the_blueprint_folder_not_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    write_env(project, f"HOOK_SECRET={SENTINEL}\n")

    args = argparse.Namespace(blueprint=str(project / "blueprint.yaml"), skip_preflight=False)
    (project / "blueprint.yaml").write_text(
        "name: d\nagents: [{name: a}]\ntriggers:\n  - name: h\n    webhook: {secret_env: HOOK_SECRET}\n",
        encoding="utf-8",
    )
    assert prepare_environment(args) is None
    assert os.environ.get("HOOK_SECRET") == SENTINEL
    assert current().project_dir == project.resolve()


def test_a_dotenv_that_is_not_utf8_is_reported_not_a_traceback(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(project)
    (project / ".env").write_bytes(b"NAME=M\xfcller\n")

    assert main(["env", "list"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("warning: cannot read the project .env:")
    assert "error:" in err and "save it as UTF-8" in err


def test_worker_once_sees_a_dotenv_secret(project: Path) -> None:
    load_environment(project, export=False)
    write_env(project, f"HOOK_SECRET={SENTINEL}\n")
    assert asyncio.run(_secrets(Webhook("h", secret_env="HOOK_SECRET"))) == (SENTINEL,)


async def _secrets(hook: object) -> tuple[str, ...]:
    return hook.secrets()  # type: ignore[attr-defined, no-any-return]  # a Webhook


def test_shown_urls_lose_their_password_and_query_string() -> None:
    assert mask("TEAMS_WEBHOOK_URL", "https://x.example/hook?sig=abc") == "set"
    shown = mask("OLLAMA_HOST", "https://user:pw@q.example:6333/c?api_key=abc#frag")
    assert shown == "https://***@q.example:6333/c?***"
    assert mask("LANGFUSE_BASE_URL", "https://cloud.langfuse.com") == "https://cloud.langfuse.com"


@pytest.mark.parametrize(
    "name",
    ["BASH_ENV", "ENV", "OPENSSL_CONF", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "PERL5OPT"]
    + ["PIP_INDEX_URL", "UV_INDEX_URL", "TEMP", "TMP", "TMPDIR"]
    + ["PERL5LIB", "RUBYOPT", "RUBYLIB", "NODE_PATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS"]
    + ["JDK_JAVA_OPTIONS", "CLASSPATH", "SHELL", "LOCALAPPDATA", "SSLKEYLOGFILE"],
)
def test_names_that_change_how_programs_start_are_never_exported(name: str) -> None:

    assert is_denied(name)
    assert not [known for known in REGISTRY if is_denied(known)]


SENTINEL_ENV_CLI = "sk-env-sentinel-77aa"


@pytest.fixture
def project_env_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "desk"
    folder.mkdir()
    (folder / "blueprint.yaml").write_text("name: d\nagents: [{name: a}]\n", encoding="utf-8")
    monkeypatch.chdir(folder)
    return folder


class Stdin(io.TextIOWrapper):
    def isatty(self) -> bool:
        return False


def stdin(data: bytes) -> Stdin:
    return Stdin(io.BytesIO(data), encoding="utf-8")


def test_set_refuses_a_secret_on_the_command_line(
    project_env_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["env", "set", f"OPENAI_API_KEY={SENTINEL_ENV_CLI}"]) == 1
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY is secret" in err and SENTINEL_ENV_CLI not in err
    assert not (project_env_cli / ".env").exists()


def test_set_reads_stdin_without_bom_and_line_break(
    project_env_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", stdin(b"\xef\xbb\xbf" + SENTINEL_ENV_CLI.encode() + b"\r\n"))
    assert main(["env", "set", "OPENAI_API_KEY", "--stdin"]) == 0
    assert read_env_file(project_env_cli / ".env")["OPENAI_API_KEY"] == SENTINEL_ENV_CLI
    assert SENTINEL_ENV_CLI not in capsys.readouterr().out


def test_without_a_terminal_the_prompt_is_refused(
    project_env_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", stdin(b""))
    assert main(["env", "set", "OPENAI_API_KEY"]) == 1
    assert "--stdin" in capsys.readouterr().err


def test_set_checks_the_value_and_a_target_needs_confirm(
    project_env_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["env", "set", "AWA_TRACE_LEVEL=loud"]) == 1
    assert "one of off, summary, full" in capsys.readouterr().err
    assert main(["env", "set", "AWA_DEFAULT_MODEL=gpt5"]) == 1
    assert main(["env", "set", "OPENAI_BASE_URL=https://gw.example.com/v1"]) == 3
    assert main(["env", "set", "OPENAI_BASE_URL=https://gw.example.com/v1", "--confirm"]) == 0
    text = (project_env_cli / ".env").read_text(encoding="utf-8")
    assert "# awa:confirmed " in text and "OPENAI_BASE_URL=https://gw.example.com/v1" in text
    assert main(["env", "set", "AWA_DEFUALT_MODEL=x"]) == 1
    assert "did you mean AWA_DEFAULT_MODEL" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWA_RUN_MAX_REQUESTS", "0"),
        ("AWA_MAX_IDENTICAL_CALLS", "1"),
        ("AWA_CACHE_TOOLS_TTL", "-5"),
    ],
)
def test_set_refuses_a_value_outside_the_bounds_of_settings(
    project_env_cli: Path, capsys: pytest.CaptureFixture[str], name: str, value: str
) -> None:
    assert main(["env", "set", f"{name}={value}"]) == 1
    assert f"{name} does not accept that value" in capsys.readouterr().err
    assert not (project_env_cli / ".env").exists()


def test_list_get_and_unset_never_show_a_secret(
    project_env_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_env_cli / ".env").write_text(
        f"OPENAI_API_KEY={SENTINEL_ENV_CLI}\nAWA_FAST_MODEL=openai:gpt-6-luna\n", encoding="utf-8"
    )
    assert main(["env", "list", "--json"]) == 0
    rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["OPENAI_API_KEY"]["value"] == "set"
    assert rows["AWA_FAST_MODEL"]["source"] == ".env:2"
    assert main(["env", "get", "OPENAI_API_KEY"]) == 0
    out = capsys.readouterr().out
    assert "set" in out and SENTINEL_ENV_CLI not in out and "source:  .env:1" in out
    assert main(["env", "unset", "AWA_FAST_MODEL"]) == 0
    assert "AWA_FAST_MODEL" not in read_env_file(project_env_cli / ".env")


def test_doctor_shows_sources_and_exits_78_without_a_key(
    project_env_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_env_cli / ".env").write_text(
        f"AWA_POSTGRES_DSN=postgresql://awa:{SENTINEL_ENV_CLI}@127.0.0.1:5432/awa\n"
        "AWA_FAST_MODEL=anthropic:claude-haiku-5-5\n",
        encoding="utf-8",
    )
    assert main(["doctor"]) == 78
    out = capsys.readouterr().out
    assert SENTINEL_ENV_CLI not in out
    assert "AWA_FAST_MODEL" in out and ".env:2" in out
    assert str(project_env_cli) not in out.split("INFO  sources", 1)[1].split("provider")[0]
    assert main(["doctor", "--json"]) == 78
    data = json.loads(capsys.readouterr().out)
    assert "credentials:anthropic" in data["missing_required"]
    assert SENTINEL_ENV_CLI not in json.dumps(data)


def test_merge_keeps_lines_and_needs_confirmation_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# my notes\nA=1\nB=2\n", encoding="utf-8")
    write_env_file(path, {"C": "3"}, mode="merge")
    assert path.read_text(encoding="utf-8") == "# my notes\nA=1\nB=2\nC=3\n"
    with pytest.raises(OverwriteRefused) as refused:
        write_env_file(path, {"A": "new"}, mode="merge")
    assert refused.value.names == ["A"]
    write_env_file(path, {"A": "new"}, mode="merge", confirm_overwrite=True)
    assert read_env_file(path)["A"] == "new"
    backups = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".env.bak"))
    assert backups == [".env.bak"]
    assert read_env_file(tmp_path / ".env.bak")["A"] == "1"
    if os.name == "posix":
        assert (tmp_path / ".env.bak").stat().st_mode & 0o077 == 0


def test_the_backup_is_ignored_by_git_and_never_exported(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    patterns = (root / ".gitignore").read_text(encoding="utf-8")
    assert ".env.*" in patterns
    for name in (".env.bak", "blueprint.yaml"):
        (tmp_path / name).write_text("A=1", encoding="utf-8")
    assert export_files(tmp_path) == [Path("blueprint.yaml")]


def test_a_dollar_value_is_single_quoted_and_both_quotes_are_refused(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_env_file(path, {"PW": "ab$cd"}, mode="merge")
    assert "PW='ab$cd'" in path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="both"):
        write_env_file(path, {"PW2": "a$b'c"}, mode="merge")


def test_a_locked_file_is_retried_then_fails_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    attempts: list[object] = []

    def locked(source: object, target: object) -> None:
        attempts.append(source)
        raise PermissionError("in use")

    monkeypatch.setattr(dotenv.os, "replace", locked)
    monkeypatch.setattr(dotenv.time, "sleep", lambda seconds: None)
    with pytest.raises(PermissionError, match="locked by another program"):
        write_env_file(tmp_path / ".env", {"A": "1"})
    assert len(attempts) == 5
    assert not (tmp_path / "..env.tmp").exists() and not (tmp_path / ".env.tmp").exists()


def test_remove_drops_the_name_and_its_note(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# keep\n# awa:confirmed 2026-10-07T00:00:00Z X\nX=1\nY=2\n", encoding="utf-8")
    assert remove_env_names(path, ["X"]) == ["X"]
    assert path.read_text(encoding="utf-8") == "# keep\nY=2\n"
