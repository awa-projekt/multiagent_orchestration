"""`Settings` and `.env.example`: secrets never leak, precedence, empty variables, `AWA_ENV`, and the Microsoft 365 block."""

from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from awa.checks._env import CHECK_SETTINGS
from awa.checks.microsoft import NAMES
from awa.connectors._catalog import connector_context
from awa.core.settings import Settings, get_settings
from awa.persistence.checkpointer import open_checkpointer

SECRETS = {
    "OPENAI_API_KEY": "sk-oa-TOPSECRET-1",
    "ANTHROPIC_API_KEY": "sk-ant-TOPSECRET-2",
    "AZURE_OPENAI_API_KEY": "azure-TOPSECRET-3",
    "LANGSMITH_API_KEY": "ls-TOPSECRET-4",
    "AWA_POSTGRES_DSN": "postgresql://app:TOPSECRET-5@db.example/app",
}
SECRET_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "azure_openai_api_key",
    "langsmith_api_key",
    "postgres_dsn",
)


def _renderings(settings: Settings) -> list[str]:
    return [repr(settings), str(settings), repr(settings.model_dump()), settings.model_dump_json()]


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("secret_env")
def test_secrets_from_the_environment_never_appear_in_repr_or_dumps() -> None:
    settings = Settings()

    for text in _renderings(settings):
        assert "TOPSECRET" not in text
    for field in SECRET_FIELDS:
        value = getattr(settings, field)
        assert isinstance(value, SecretStr)
        assert "TOPSECRET" in value.get_secret_value()


def test_secrets_given_as_keywords_are_masked_too() -> None:
    settings = Settings(
        openai_api_key="sk-oa-TOPSECRET",  # type: ignore[arg-type]
        ANTHROPIC_API_KEY="sk-ant-TOPSECRET",
        postgres_dsn="postgresql://u:TOPSECRET@h/db",  # type: ignore[arg-type]
    )

    assert all("TOPSECRET" not in text for text in _renderings(settings))
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-TOPSECRET"


@pytest.mark.usefixtures("secret_env")
def test_validation_errors_do_not_contain_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_CHECKPOINTER_BACKEND", "redis")

    with pytest.raises(ValidationError) as caught:
        Settings()

    rendered = "".join(traceback.format_exception(caught.value))
    assert "checkpointer_backend" in rendered
    assert "TOPSECRET" not in rendered


def test_empty_variables_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "AWA_POSTGRES_DSN", "AWA_CHECKPOINTER_BACKEND"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("LANGSMITH_TRACING", "")

    settings = Settings()

    assert settings.anthropic_api_key is None
    assert settings.postgres_dsn is None
    assert settings.checkpointer_backend == "memory"
    assert settings.langsmith_tracing is False


def test_precedence_is_keywords_then_environment_then_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWA_DEFAULT_MODEL=openai:from-file\nAWA_FAST_MODEL=openai:fast-file\n"
        "AWA_STRONG_MODEL=openai:strong-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWA_FAST_MODEL", "openai:fast-env")
    monkeypatch.setenv("AWA_STRONG_MODEL", "openai:strong-env")

    settings = Settings(_env_file=env_file, strong_model="openai:strong-code")

    assert settings.default_model == "openai:from-file"
    assert settings.fast_model == "openai:fast-env"
    assert settings.strong_model == "openai:strong-code"


def test_aliased_fields_prefer_the_unprefixed_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_OPENAI_API_KEY", "sk-prefixed")
    monkeypatch.setenv("AWA_LANGSMITH_TRACING", "true")

    fallback = Settings()
    assert fallback.openai_api_key is not None
    assert fallback.openai_api_key.get_secret_value() == "sk-prefixed"
    assert fallback.langsmith_tracing is True

    monkeypatch.setenv("OPENAI_API_KEY", "sk-plain")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    preferred = Settings()
    assert preferred.openai_api_key is not None
    assert preferred.openai_api_key.get_secret_value() == "sk-plain"
    assert preferred.langsmith_tracing is False


def test_awa_env_is_a_validated_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_ENV", "production")
    assert Settings().env == "production"

    monkeypatch.setenv("AWA_ENV", "prod")
    with pytest.raises(ValidationError, match="env"):
        Settings()


def test_azure_auth_is_key_unless_set_and_forgives_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().azure_auth == "key"

    monkeypatch.setenv("AZURE_OPENAI_AUTH", " Entra ")
    assert Settings().azure_auth == "entra"

    monkeypatch.setenv("AZURE_OPENAI_AUTH", "password")
    with pytest.raises(ValidationError, match="AZURE_OPENAI_AUTH"):
        Settings()


def test_the_entra_client_secret_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "entra-TOPSECRET-6")
    settings = Settings()

    assert all("TOPSECRET" not in text for text in _renderings(settings))
    assert settings.azure_client_secret is not None
    assert settings.azure_client_secret.get_secret_value() == "entra-TOPSECRET-6"


def test_get_settings_is_cached_until_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("AWA_LOG_LEVEL", "DEBUG")
        first = get_settings()
        monkeypatch.setenv("AWA_LOG_LEVEL", "WARNING")
        assert get_settings() is first
        get_settings.cache_clear()
        assert get_settings().log_level == "WARNING"
    finally:
        get_settings.cache_clear()


def test_the_run_ceilings_default_to_the_documented_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("REQUESTS", "TOOL_CALLS", "TOKENS", "SECONDS", "DEPTH"):
        monkeypatch.delenv(f"AWA_RUN_MAX_{name}", raising=False)
    settings = Settings(_env_file=None)
    ceilings = (
        settings.run_max_requests,
        settings.run_max_tool_calls,
        settings.run_max_tokens,
        settings.run_max_seconds,
        settings.run_max_depth,
    )
    assert ceilings == (400, 200, 1_000_000, 3600, 16)


async def test_open_checkpointer_passes_the_raw_dsn_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[object] = []

    class _Saver:
        @classmethod
        def from_conn_string(cls, dsn: object, **options: object) -> Any:
            received.append(dsn)
            raise RuntimeError("stop")

    aio = pytest.importorskip("langgraph.checkpoint.postgres.aio")
    monkeypatch.setattr(aio, "AsyncPostgresSaver", _Saver)
    settings = Settings(checkpointer_backend="postgres", postgres_dsn="postgresql://u:pw@h/db")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="stop"):
        async with open_checkpointer(settings):
            pass

    assert received == ["postgresql://u:pw@h/db"]


@pytest.mark.postgres
async def test_open_checkpointer_connects_with_a_secret_dsn(postgres_dsn: str) -> None:
    settings = Settings(checkpointer_backend="postgres", postgres_dsn=postgres_dsn)  # type: ignore[arg-type]

    async with open_checkpointer(settings) as checkpointer:
        assert checkpointer is not None
    assert postgres_dsn not in repr(settings)


ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / ".env.example"
ADVANCED = {"MS_GRAPH_TOKEN"}  # a pasted delegated token: documented in the connector, not here


def block() -> list[str]:
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("# --- Microsoft Entra ID and Microsoft 365")
    )
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("# ---"))
    return lines[start:end]


def variables() -> list[str]:
    return [line.split("=", 1)[0] for line in block() if line and not line.startswith("#")]


@pytest.mark.parametrize("connector", ["outlook", "sharepoint", "teams", "microsoft_graph"])
def test_every_variable_of_the_microsoft_connectors_is_in_the_block(connector: str) -> None:
    spec = connector_context(connector, env={}, dotenv=None).spec
    wanted = {v.name for v in spec.variables()} - ADVANCED
    assert wanted <= set(variables()), wanted - set(variables())


def test_the_check_variables_are_in_the_block() -> None:
    listed = set(variables())
    assert {"AWA_CHECK_NEGATIVE_MAILBOX", "AWA_CHECK_ALLOW_POST"} <= listed
    assert set(NAMES) - {"MS_CHECK_WORKBOOK"} <= listed  # MS_CHECK_WORKBOOK: optional, rare
    assert "MS_CHECK_WORKBOOK" in CHECK_SETTINGS


def test_each_variable_line_has_a_permission_comment() -> None:
    lines = block()
    for number, line in enumerate(lines):
        if not line or line.startswith("#"):
            continue
        comments: list[str] = []
        above = number - 1
        while above >= 0 and lines[above].startswith("#"):
            comments.append(lines[above])
            above -= 1
        assert comments, f"{line} has no comment"
        assert "ermission" in " ".join(comments), f"{line}: the comment names no permission"


def test_no_variable_appears_twice_in_the_file() -> None:
    names = [
        line.split("=", 1)[0]
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    ]
    assert len(names) == len(set(names))


def test_offline_tests_see_no_variable_settings_read() -> None:
    settings = Settings()
    assert settings.langfuse_public_key is None and settings.azure_deployment is None
    assert not {"LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "AZURE_OPENAI_DEPLOYMENT"} & set(os.environ)
