"""Environment-derived configuration: `Settings` reads `AWA_*` variables and a `.env` file."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import AliasChoices, Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from awa.core.config import current, env_value, load_environment

Environment = Literal["local", "test", "staging", "production"]
"""Deployment environment names accepted by `Settings.env`."""

_PROJECT_DOTENV: Any = object()
"""`Settings(_env_file=...)` not given: read the project's `.env` (see `awa.core.config`)."""

DATA_BACKEND_VARIABLE = "AWA_DATA_BACKEND"
"""The variable that says where runs, steps and results are kept (`Settings.data_backend`)."""

POSTGRES_DSN_VARIABLE = "AWA_POSTGRES_DSN"
"""The variable that holds the PostgreSQL DSN (`Settings.postgres_dsn`)."""

PSEUDONYM_KEY_VARIABLE = "AWA_PSEUDONYM_KEY"
"""The variable that holds the key linking people to their runs (`Settings.pseudonym_key`)."""

CACHE_SALT_VARIABLE = "AWA_CACHE_SALT"
"""The variable that holds the project's cache salt (`Settings.cache_salt`)."""

ALLOWED_HOSTS_VARIABLE = "AWA_ALLOWED_HOSTS"
"""The variable that lists further Host names `awa serve` answers (comma-separated)."""

_PROFILES = ("default_model", "fast_model", "strong_model")


class Settings(BaseSettings):
    """All environment-derived configuration; `get_settings()` returns the cached instance.

    Fields read `AWA_<FIELD_NAME>`, or first the conventional unprefixed variable (for example
    `OPENAI_API_KEY`); names are case-insensitive and empty variables count as unset. Precedence:
    keyword arguments, environment, the `.env` file (`_env_file=` for another or `None`), defaults.
    Credentials are `SecretStr`, masked everywhere but `.get_secret_value()`. Every variable is
    listed with its meaning in the settings reference (docs/develop/configuration.md, `awa env`).

    Example:
        ```python
        settings = Settings(_env_file=None, checkpointer_backend="postgres",
                            postgres_dsn="postgresql://app:pw@db/app")
        async with open_checkpointer(settings) as checkpointer:
            ...
        ```
    """

    model_config = SettingsConfigDict(
        env_prefix="AWA_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    env: Environment = "local"
    log_level: str = "INFO"

    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Passed to Azure models explicitly: pydantic-settings reads `.env` without
    # exporting it to `os.environ`, where the provider SDK would look.
    azure_openai_api_key: SecretStr | None = Field(default=None, alias="AZURE_OPENAI_API_KEY")
    azure_openai_endpoint: str | None = Field(default=None, alias="AZURE_OPENAI_ENDPOINT")
    azure_deployment: str | None = Field(
        default=None,
        alias="AZURE_OPENAI_DEPLOYMENT_NAME",
        validation_alias=AliasChoices("AZURE_OPENAI_DEPLOYMENT_NAME", "AZURE_OPENAI_DEPLOYMENT"),
    )
    azure_api_version: str | None = Field(default=None, alias="OPENAI_API_VERSION")
    azure_auth: Literal["key", "entra"] = Field(default="key", alias="AZURE_OPENAI_AUTH")
    azure_tenant_id: str | None = Field(default=None, alias="AZURE_TENANT_ID")
    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")
    azure_client_secret: SecretStr | None = Field(default=None, alias="AZURE_CLIENT_SECRET")
    azure_authority_host: str | None = Field(default=None, alias="AZURE_AUTHORITY_HOST")

    default_model: str = "anthropic:claude-sonnet-5-5"
    fast_model: str = "anthropic:claude-haiku-5-5"
    strong_model: str = "anthropic:claude-opus-5-5"
    openai_responses_api: bool = False
    vision_model: str | None = None

    checkpointer_backend: Literal["memory", "sqlite", "postgres"] = "memory"
    checkpointer_path: str | None = None
    postgres_dsn: SecretStr | None = None
    postgres_admin_dsn: SecretStr | None = None
    postgres_pool_max: int = Field(default=10, ge=1)
    postgres_pooler: Literal["none", "pgbouncer"] = "none"
    data_backend: Literal["sqlite", "postgres"] | None = None
    capture_content: bool = False
    run_retention_days: int | None = Field(default=None, gt=0)
    pseudonym_key: SecretStr | None = None
    pseudonym_key_id: str = "1"

    artifact_backend: Literal["memory", "file", "postgres"] = "memory"
    artifact_dir: str | None = None

    store_backend: Literal["memory", "postgres"] = "memory"

    # The unprefixed names LangChain/LangSmith read themselves, so one flag
    # controls both tracing and awa's `langsmith` trace tag.
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: SecretStr | None = Field(default=None, alias="LANGSMITH_API_KEY")

    # Langfuse keeps its standard variable names; the switches are awa's own.
    langfuse_tracing: bool = False
    langfuse_capture_content: bool = False
    langfuse_strict: bool = False
    trace_export_timeout: float = Field(default=30.0, gt=0)
    langfuse_public_key: SecretStr | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: SecretStr | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_base_url: str | None = Field(
        default=None,
        alias="LANGFUSE_BASE_URL",
        validation_alias=AliasChoices("LANGFUSE_BASE_URL", "LANGFUSE_HOST"),
    )
    langfuse_release: str | None = Field(default=None, alias="LANGFUSE_RELEASE")
    langfuse_environment: str | None = Field(default=None, alias="LANGFUSE_TRACING_ENVIRONMENT")

    # The cache settings block (`awa.runtime.cache.CacheSettings`); a value set here wins
    # over the blueprint's `defaults.cache`.
    cache_enabled: bool | None = None
    cache_prompt: Literal["auto", "off", "1h"] | None = None
    cache_prompt_pad: bool | None = None
    cache_tools: bool | None = None
    cache_tools_ttl: float | None = Field(default=None, gt=0)
    cache_steps: bool | None = None
    cache_store: Literal["memory", "sqlite"] | None = None
    cache_path: str | None = None
    cache_salt: SecretStr | None = None

    # A ceiling cannot be switched off, only raised (`awa.runtime.termination.ceilings`).
    run_max_requests: int = Field(default=400, ge=1)
    run_max_tool_calls: int = Field(default=200, ge=1)
    run_max_tokens: int = Field(default=1_000_000, ge=1)
    run_max_seconds: int = Field(default=3600, ge=1)
    run_max_depth: int = Field(default=16, ge=1)
    delegation_max_requests: int = Field(default=50, ge=1)
    max_identical_calls: int = Field(default=5, ge=2)
    max_tool_errors: int = Field(default=5, ge=1)
    max_identical_outputs: int = Field(default=2, ge=2)
    max_pages_per_call: int = Field(default=20, ge=1)
    max_lease_losses: int = Field(default=3, ge=1)

    # The tool layer profile: `cost` (small results, early clearing) or `quality` (larger
    # results, late clearing); the settings below left unset take the profile's value.
    tool_profile: Literal["cost", "quality"] = "cost"
    # Longest text result a tool hands to the model (`awa.tools.offload`).
    tool_max_output_chars: int | None = Field(default=None, ge=1)
    # Failures in a row after which a tool is switched off for the run (`awa.tools.resilience`).
    tool_circuit_breaker: int = Field(default=3, ge=0)
    # Retries of a tool call that failed with a network error (`awa.tools.resilience`).
    tool_retries: int = Field(default=2, ge=0)

    # Clearing of older tool results before a model call (`awa.memory.context_editing`).
    context_editing: bool = True
    context_editing_trigger: int | None = Field(default=None, ge=1)
    context_editing_keep: int | None = Field(default=None, ge=0)
    context_editing_step: int = Field(default=4, ge=1)
    context_editing_clear_at_least: int | None = Field(default=None, ge=0)

    _detected_provider: str | None = PrivateAttr(default=None)

    def __init__(self, _env_file: Any = _PROJECT_DOTENV, **values: Any) -> None:
        if _env_file is _PROJECT_DOTENV:
            configured = self.model_config.get("env_file")
            # ".env" (the default) means the project's `.env`, found by `awa.core.config`.
            _env_file = current().dotenv_file() if configured == ".env" else configured
        elif isinstance(_env_file, str | os.PathLike):
            target = os.fspath(_env_file)
            if isinstance(target, str) and not _is_current(target):
                # Triggers and the names a blueprint declares then read this file too.
                load_environment(dotenv=target)
        super().__init__(_env_file=_env_file, **values)

    @field_validator("azure_auth", mode="before")
    @classmethod
    def _azure_auth_spelling(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _profiles_from_the_credentials(self) -> Settings:
        """Profiles left unset follow the provider whose credentials are set (Anthropic first)."""

        unset = [name for name in _PROFILES if name not in self.model_fields_set]
        if not unset:
            return self
        from awa.core.providers import detect

        chosen = detect(self.credential)
        if chosen is None or chosen.name == "anthropic":
            return self
        models = dict(zip(_PROFILES, chosen.models(self.credential), strict=True))
        for name in unset:
            setattr(self, name, models[name])
        self._detected_provider = chosen.name
        return self

    def credential(self, variable: str) -> str | None:
        """The value of a provider variable: from these settings, else the environment and
        the project `.env`."""

        field_name = _field_of(variable)
        if field_name is not None:
            value = getattr(self, field_name, None)
            raw = value.get_secret_value() if isinstance(value, SecretStr) else value
            return str(raw) if raw is not None and str(raw).strip() else None
        found = env_value(variable)
        return found if found and found.strip() else None

    @property
    def detected_provider(self) -> str | None:
        """The provider the unset model profiles were chosen from, or `None` (Anthropic, or
        the profiles were set)."""

        return self._detected_provider


def _is_current(path: str) -> bool:
    """Whether `path` is the `.env` already loaded (then loading it again changes nothing)."""

    loaded = current().dotenv_file()
    if loaded is None:
        return False
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(loaded))


@lru_cache
def _field_of(variable: str) -> str | None:
    """The `Settings` field that `variable` fills, by the field's aliases."""

    for name, info in Settings.model_fields.items():
        alias = info.validation_alias
        if variable in (alias.choices if isinstance(alias, AliasChoices) else (info.alias,)):
            return name
    return None


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings`, cached until `get_settings.cache_clear()`."""

    return Settings()


__all__ = [
    "ALLOWED_HOSTS_VARIABLE",
    "CACHE_SALT_VARIABLE",
    "DATA_BACKEND_VARIABLE",
    "POSTGRES_DSN_VARIABLE",
    "PSEUDONYM_KEY_VARIABLE",
    "Environment",
    "Settings",
    "get_settings",
]
