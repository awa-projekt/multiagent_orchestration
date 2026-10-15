"""Guided setup: the model provider and key, the database and tracing.

`awa setup` (the command) and `awa.setup()` (in a notebook) work through sections. Each section
reports its state (`missing`, `set`, `tested` or `failed`) and, when applied, writes its values
to the project `.env` and nowhere else. Importing this package has no side effects.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, SecretStr

from awa.core.settings_registry import SETUP_ENV
from awa.errors import ConfigurationError

if TYPE_CHECKING:
    from awa.core.providers import ProviderSpec

SectionName = Literal["provider", "test", "database", "tracing"]
"""The sections of the setup, in order."""

SECTIONS: tuple[SectionName, ...] = ("provider", "test", "database", "tracing")

State = Literal["missing", "set", "tested", "failed"]


class SectionState(BaseModel):
    """Where one section stands (`missing`, `set`, `tested` or `failed`). Never holds a value.

    `source` says where its main value comes from: `env`, `dotenv` or `default`.
    """

    section: SectionName
    state: State
    source: Literal["env", "dotenv", "default"] | None = None
    detail: str = ""
    tested_at: datetime | None = None


class EnvChanges(BaseModel):
    """What a section writes to `.env` (`set`) and removes from it (`unset`)."""

    set: dict[str, SecretStr] = {}
    unset: list[str] = []


@dataclass(frozen=True)
class Choice:
    """One option of a section: its value and a sentence describing it."""

    value: str
    text: str


SetupError = ConfigurationError
"""A choice cannot be applied (a `ConfigurationError`); the message says what to do."""


ModelCall = Callable[[str], None]
"""Makes one tiny call to a model (`provider:model`); raises when it fails."""

_MODEL_VARIABLES = tuple(
    SETUP_ENV[name] for name in ("default_model", "fast_model", "strong_model")
)


@dataclass
class SetupPlan:
    """The setup of one project: the state of each section, and applying choices.

    Args:
        services: Runs Docker for the local services (tests pass one with a fake runner).
        call_model: Makes the one test call of the `test` section (default: a real call with
            `max_tokens=1` and a 15-second timeout).
    """

    project: Path
    services: Any = None
    call_model: ModelCall | None = None
    _state: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.project = Path(self.project).resolve()
        if self.services is None:
            from awa.project_setup.services import Services

            self.services = Services(self.project, self.env_file)
        self._state = self._load_state()

    @property
    def env_file(self) -> Path:
        """The project `.env`, where every value goes."""

        return self.project / ".env"

    @property
    def state_file(self) -> Path:
        from awa.project_setup.services import STATE_FILE

        return self.project / STATE_FILE

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"sections": {}, "ports": {}}
        return data if isinstance(data, dict) else {"sections": {}, "ports": {}}

    def _save_state(self) -> None:
        from awa.project_setup.services import project_name

        self._state["compose_project"] = project_name(self.project)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _mark(self, section: SectionName, state: State, detail: str = "") -> None:
        sections = self._state.setdefault("sections", {})
        entry: dict[str, Any] = {"state": state, "at": _now()}
        if detail:
            entry["detail"] = detail
        if state == "tested":
            entry["tested_at"] = entry["at"]
        sections[section] = entry
        self._save_state()

    def _environment(self) -> Any:
        from awa.core.config import LoadedEnvironment

        file = self.env_file if self.env_file.is_file() else None
        return LoadedEnvironment(project_dir=self.project, dotenv_path=file)

    def value(self, name: str) -> str | None:
        """The value of `name`: the environment, else this project's `.env`."""

        return self._environment().get(name) or None

    def _source(self, name: str) -> Literal["env", "dotenv", "default"]:
        origin = self._environment().source_of(name).origin
        return "env" if origin in ("env", "env_empty") else origin

    def status(self) -> list[SectionState]:
        """The state of every section."""

        return [self.section_status(name) for name in SECTIONS]

    def section_status(self, section: SectionName) -> SectionState:
        """The state of one section."""

        recorded = self._state.get("sections", {}).get(section, {})
        if section == "provider":
            chosen = self.provider()
            if chosen is None:
                return SectionState(section=section, state="missing", detail="no model key is set")
            key = chosen.key_variable or chosen.required[0]
            return SectionState(
                section=section,
                state="set",
                source=self._source(key),
                detail=f"{chosen.title} (credentials found)",
            )
        if section == "test":
            chosen = self.provider()
            if chosen is None:
                return SectionState(section=section, state="missing", detail="set a key first")
            if (
                recorded.get("state") in ("tested", "failed")
                and recorded.get("provider") == chosen.name
            ):
                return SectionState(
                    section=section,
                    state=recorded["state"],
                    detail=recorded.get("detail", ""),
                    tested_at=recorded.get("tested_at"),
                )
            return SectionState(section=section, state="set", detail="not tested yet")
        if section == "database":
            dsn = self.value(SETUP_ENV["dsn"])
            if not dsn:
                return SectionState(
                    section=section,
                    state="set",
                    source="default",
                    detail="local files next to the blueprint (SQLite and JSON)",
                )
            kind = (
                "local PostgreSQL in Docker" if self.value(SETUP_ENV["db_port"]) else "PostgreSQL"
            )
            state: State = "tested" if recorded.get("state") == "tested" else "set"
            return SectionState(
                section=section, state=state, source=self._source(SETUP_ENV["dsn"]), detail=kind
            )
        on = (self.value(SETUP_ENV["langfuse_tracing"]) or "").lower() in ("1", "true", "yes")
        if not on:
            return SectionState(section=section, state="set", source="default", detail="off")
        keys = self.value("LANGFUSE_PUBLIC_KEY") and self.value("LANGFUSE_SECRET_KEY")
        return SectionState(
            section=section,
            state="set" if keys else "missing",
            source=self._source(SETUP_ENV["langfuse_tracing"]),
            detail="Langfuse" if keys else "Langfuse is on but its keys are missing",
        )

    def provider(self) -> ProviderSpec | None:
        """The provider whose credentials are complete (the first in the fixed order)."""

        from awa.core.providers import detect

        return detect(self.value)

    def current_choice(self, section: Literal["database", "tracing"]) -> str:
        """The choice the project's settings already make for the database or tracing section."""

        if section == "database":
            if not self.value(SETUP_ENV["dsn"]):
                return "sqlite"
            return "postgres-docker" if self.value(SETUP_ENV["db_port"]) else "url"
        if (self.value(SETUP_ENV["langfuse_tracing"]) or "").lower() not in ("1", "true", "yes"):
            return "off"
        return "langfuse-docker" if self.value(SETUP_ENV["langfuse_port"]) else "url"

    def options(self, section: SectionName) -> list[Choice]:
        """The choices of a section."""

        if section == "provider":
            from awa.core.providers import PROVIDERS

            return [Choice(item.name, item.title) for item in PROVIDERS]
        if section == "database":
            return [
                Choice("sqlite", "local files next to the blueprint (no server)"),
                Choice("postgres-docker", "PostgreSQL in Docker on this computer"),
                Choice("url", "your own PostgreSQL (DATABASE_URL)"),
            ]
        if section == "tracing":
            return [
                Choice("off", "no tracing"),
                Choice("langfuse-docker", "Langfuse in Docker on this computer (4 GB memory)"),
                Choice("url", "your own Langfuse (address and keys)"),
            ]
        return []

    def apply(
        self, section: SectionName, choices: Mapping[str, Any], *, confirm_overwrite: bool = False
    ) -> list[SectionState]:
        """Apply `choices` to a section, write them to `.env` and return every section's state.

        Args:
            section: `provider`, `database` or `tracing` (`test` runs the test).
            choices: The section's choices (see `options`), for example
                `{"provider": "openai", "key": "..."}` or `{"database": "postgres-docker"}`.

        Raises:
            SetupError: When a choice is invalid or a test fails.
            OverwriteRefused: When a value would change without `confirm_overwrite`.
        """

        if section == "test":
            self.test()
            return self.status()
        changes = self._changes(section, dict(choices))
        self._write(changes, confirm_overwrite=confirm_overwrite)
        if section == "provider":
            self._mark("provider", "set")
        else:
            self._mark(section, "set", str(choices.get(section, "")))
        return self.status()

    def _write(self, changes: EnvChanges, *, confirm_overwrite: bool) -> None:
        from awa.core.dotenv import remove_env_names, write_env_file

        values = {name: secret.get_secret_value() for name, secret in changes.set.items()}
        if values:
            write_env_file(self.env_file, values, mode="merge", confirm_overwrite=confirm_overwrite)
        if changes.unset:
            present = [n for n in changes.unset if self.value(n)]
            if present and not confirm_overwrite:
                from awa.core.dotenv import OverwriteRefused

                raise OverwriteRefused(present)
            remove_env_names(self.env_file, changes.unset)

    def _changes(self, section: SectionName, choices: dict[str, Any]) -> EnvChanges:
        if section == "provider":
            return self._provider_changes(choices)
        if section == "database":
            return self._database_changes(choices)
        if section == "tracing":
            return self._tracing_changes(choices)
        raise SetupError(f"the {section} section has nothing to apply here")

    def _provider_changes(self, choices: dict[str, Any]) -> EnvChanges:
        from awa.core.providers import detect, provider

        chosen = provider(str(choices.get("provider") or ""))
        if chosen is None:
            raise SetupError(f"unknown provider {choices.get('provider')!r}")
        values: dict[str, str] = {}
        key = _secret_text(choices.get("key"))
        if chosen.name == "ollama":
            host = str(choices.get("host") or "http://localhost:11434").strip()
            values["OLLAMA_HOST"] = host
        elif key:
            assert chosen.key_variable is not None
            values[chosen.key_variable] = key
        elif not chosen.complete(self.value):
            raise SetupError(f"{chosen.title} needs its key ({chosen.key_variable})")
        if chosen.name == "azure_openai":
            for option, name in (
                ("endpoint", "AZURE_OPENAI_ENDPOINT"),
                ("deployment", "AZURE_OPENAI_DEPLOYMENT_NAME"),
            ):
                if choices.get(option):
                    values[name] = str(choices[option]).strip()
                elif not self.value(name):
                    raise SetupError(f"Azure OpenAI needs --{option} ({name})")

        def after(name: str) -> str | None:
            return values.get(name) or self.value(name)

        models = dict(zip(_MODEL_VARIABLES, chosen.models(after), strict=True))
        first = detect(after)
        unset: list[str] = []
        if first is None or first.name != chosen.name:
            # Another provider comes first in the fixed order: name this one's models.
            values.update(models)
        else:
            # This provider comes first: a model of another provider would still win.
            prefix = models[SETUP_ENV["default_model"]].partition(":")[0]
            unset = [
                name
                for name in models
                if (current := self.value(name)) and current.partition(":")[0] != prefix
            ]
        return EnvChanges(set={n: SecretStr(v) for n, v in values.items()}, unset=unset)

    def _database_changes(self, choices: dict[str, Any]) -> EnvChanges:
        from awa.project_setup.services import DEFAULT_PORTS, free_port, password

        kind = str(choices.get("database") or "sqlite")
        dsn_name = SETUP_ENV["dsn"]
        if kind == "sqlite":
            return EnvChanges(unset=[dsn_name] if self.value(dsn_name) else [])
        if kind == "url":
            url = _secret_text(choices.get("url"))
            if not url or not url.startswith(("postgresql://", "postgres://")):
                raise SetupError("give a postgresql:// address (--database-url-stdin)")
            from awa.core.preflight import check_store
            from awa.persistence.sql import connect_once

            problems = check_store(url, connect=choices.get("connect") or connect_once)
            if problems:
                self._mark("database", "failed", problems[0].what)
                raise SetupError(f"{problems[0].what}; nothing was saved")
            return EnvChanges(set={dsn_name: SecretStr(url)})
        if kind != "postgres-docker":
            raise SetupError(f"unknown database choice {kind!r} (sqlite, postgres-docker or url)")
        # An existing password stays: the database volume was created with it.
        secret = self.value(SETUP_ENV["db_password"]) or password()
        port = self._port("db_port") or free_port(
            DEFAULT_PORTS["db"], taken=self.configured_ports().values(), is_free=self._is_free
        )
        dsn = f"postgresql://awa:{secret}@127.0.0.1:{port}/awa"
        return EnvChanges(
            set={
                SETUP_ENV["db_password"]: SecretStr(secret),
                SETUP_ENV["db_port"]: SecretStr(str(port)),
                dsn_name: SecretStr(dsn),
            }
        )

    def _tracing_changes(self, choices: dict[str, Any]) -> EnvChanges:
        from awa.project_setup.services import DEFAULT_PORTS, free_port, password

        kind = str(choices.get("tracing") or "off")
        switch = SETUP_ENV["langfuse_tracing"]
        if kind == "off":
            return (
                EnvChanges(set={switch: SecretStr("false")}) if self.value(switch) else EnvChanges()
            )
        if kind == "url":
            url = str(choices.get("url") or "").strip()
            public, secret = (
                _secret_text(choices.get("public_key")),
                _secret_text(choices.get("secret_key")),
            )
            if not url.startswith(("https://", "http://")) or not public or not secret:
                raise SetupError("Langfuse needs its address and both keys (public and secret)")
            return EnvChanges(
                set={
                    switch: SecretStr("true"),
                    "LANGFUSE_BASE_URL": SecretStr(url),
                    "LANGFUSE_PUBLIC_KEY": SecretStr(public),
                    "LANGFUSE_SECRET_KEY": SecretStr(secret),
                }
            )
        if kind != "langfuse-docker":
            raise SetupError(f"unknown tracing choice {kind!r} (off, langfuse-docker or url)")
        port = self._port("langfuse_port") or free_port(
            DEFAULT_PORTS["tracing"], taken=self.configured_ports().values(), is_free=self._is_free
        )

        def keep(role: str, make: Callable[[], str]) -> str:
            return self.value(SETUP_ENV[role]) or make()

        values = {
            switch: "true",
            SETUP_ENV["langfuse_port"]: str(port),
            "LANGFUSE_BASE_URL": f"http://localhost:{port}",
            "LANGFUSE_PUBLIC_KEY": self.value("LANGFUSE_PUBLIC_KEY") or "pk-lf-" + password(24),
            "LANGFUSE_SECRET_KEY": self.value("LANGFUSE_SECRET_KEY") or "sk-lf-" + password(32),
            SETUP_ENV["langfuse_email"]: keep("langfuse_email", lambda: "admin@example.com"),
            SETUP_ENV["langfuse_password"]: keep("langfuse_password", password),
            SETUP_ENV["langfuse_nextauth"]: keep("langfuse_nextauth", password),
            SETUP_ENV["langfuse_salt"]: keep("langfuse_salt", password),
            SETUP_ENV["langfuse_encryption"]: keep("langfuse_encryption", _hex64),
            SETUP_ENV["langfuse_db_password"]: keep("langfuse_db_password", password),
            SETUP_ENV["langfuse_clickhouse_password"]: keep(
                "langfuse_clickhouse_password", password
            ),
            SETUP_ENV["langfuse_redis_password"]: keep("langfuse_redis_password", password),
            SETUP_ENV["langfuse_minio_password"]: keep("langfuse_minio_password", password),
        }
        return EnvChanges(set={n: SecretStr(v) for n, v in values.items()})

    def _port(self, role: str) -> int:
        """The port already set for `role` (`0` when none); `SetupError` for a non-port value."""

        name = SETUP_ENV[role]
        value = (self.value(name) or "").strip()
        if not value:
            return 0
        if not value.isdigit() or not 0 < int(value) < 65536:
            raise SetupError(f"{name} must be a port number from 1 to 65535; fix it in .env")
        return int(value)

    def configured_ports(self) -> dict[str, int]:
        """The host port of each profile set up in `.env` (`db`, `tracing`)."""

        found = {}
        for profile, role in (("db", "db_port"), ("tracing", "langfuse_port")):
            value = self.value(SETUP_ENV[role])
            if value and value.isdigit():
                found[profile] = int(value)
        return found

    def _is_free(self, port: int) -> bool:
        from awa.project_setup.services import port_free

        checker = getattr(self.services, "port_free", None)
        return bool(checker(port)) if callable(checker) else port_free(port)

    @staticmethod
    def wanted_profiles(*, database: str | None = None, tracing: str | None = None) -> list[str]:
        """The Docker profiles the choices need (`db`, `tracing`)."""

        return [
            profile
            for profile, chosen in (
                ("db", database == "postgres-docker"),
                ("tracing", tracing == "langfuse-docker"),
            )
            if chosen
        ]

    def services_ports(self) -> dict[str, int]:
        """The host port of each profile (set up or default)."""

        from awa.project_setup.services import DEFAULT_PORTS

        return {**{str(k): v for k, v in DEFAULT_PORTS.items()}, **self.configured_ports()}

    def start(self, profiles: list[str], *, wait_timeout: float = 300) -> str:
        """Write the Compose file and start `profiles`; return one line about the result."""

        services = self.services
        services.write_compose(self.services_ports())
        result = services.up(profiles, wait_timeout=wait_timeout)
        if result.returncode != 0:
            raise SetupError(
                f"the services did not become healthy: {(result.stderr or result.stdout).strip()[-300:]}"
            )
        return f"started {', '.join(profiles)} ({services.name}); ports on 127.0.0.1 only"

    def test(self) -> SectionState:
        """Make one tiny call to the `default` model (`max_tokens=1`, 15 s) and record it."""

        chosen = self.provider()
        if chosen is None:
            raise SetupError("no model key is set: apply the provider section first")
        from awa.core.config import load_environment
        from awa.core.settings import Settings

        load_environment(self.project)  # the values just written count
        model = Settings(_env_file=self.env_file if self.env_file.is_file() else None).default_model
        call = self.call_model or _one_tiny_call
        sections = self._state.setdefault("sections", {})
        try:
            call(model)
        except Exception as exc:  # noqa: BLE001 - every failure is reported as one sentence
            from awa.kernel.redaction import redact_secrets
            from awa.models.errors import classify_model_error

            known = classify_model_error(exc, model=model)
            detail = redact_secrets(str(known or exc).splitlines()[0])[:240]
            sections["test"] = {
                "state": "failed",
                "detail": detail,
                "provider": chosen.name,
                "at": _now(),
            }
            self._save_state()
            raise SetupError(f"the test call to {model} failed: {detail}") from None
        sections["test"] = {
            "state": "tested",
            "detail": f"{model} answered",
            "provider": chosen.name,
            "at": _now(),
            "tested_at": _now(),
        }
        self._save_state()
        return self.section_status("test")

    def _repr_html_(self) -> str:
        from html import escape

        rows = "".join(
            f"<tr><td>{escape(s.section)}</td><td>{escape(s.state)}</td>"
            f"<td>{escape(s.detail)}</td></tr>"
            for s in self.status()
        )
        return (
            "<table><thead><tr><th>section</th><th>state</th><th>detail</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )


def _one_tiny_call(model: str) -> None:
    from awa.models import ModelRegistry, ModelSettings

    registry = ModelRegistry()
    built = registry.create(model, ModelSettings(max_tokens=1, timeout=15, max_retries=0))
    built.invoke("Reply with OK.")


def _secret_text(value: Any) -> str:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return str(value or "").strip()


def _hex64() -> str:
    import secrets

    return secrets.token_hex(32)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup(
    provider: str | None = None,
    api_key: str | SecretStr | None = None,
    database: str | None = None,
    tracing: str | None = None,
    *,
    project: str | Path | None = None,
    confirm_overwrite: bool = False,
    start_services: bool = False,
    **options: Any,
) -> SetupPlan:
    """Set up awa for a project from a notebook or a script; the values go to its `.env`.

    Nothing is asked: what is given is applied, the rest is only reported. Call it without
    arguments to see the state (`plan.status()`, or the plan itself in a notebook).

    Args:
        provider: `anthropic`, `openai`, `azure_openai`, `google_genai`, `mistralai`, `groq`
            or `ollama`.
        api_key: The provider's key (never printed; written to `.env`).
        database: `sqlite`, `postgres-docker` or `url` (with `database_url=`).
        tracing: `off`, `langfuse-docker` or `url` (with `langfuse_url=`, `public_key=`,
            `secret_key=`).
        project: The project folder (default: found from the working folder).
        confirm_overwrite: Allow changing values already in `.env`.
        start_services: Start the Docker services the choices need.
        **options: `endpoint` and `deployment` (Azure), `host` (Ollama), `database_url`,
            `langfuse_url`, `public_key`, `secret_key`.

    Raises:
        SetupError: When a choice is invalid.
        OverwriteRefused: When a value would change without `confirm_overwrite`.

    Example:
        ```python
        plan = awa.setup(provider="openai", api_key=key)   # writes OPENAI_API_KEY to .env
        plan.status()                                       # [SectionState(...), ...]
        ```
    """

    from awa.core.config import find_project

    plan = SetupPlan(Path(project) if project is not None else find_project())
    if provider is not None:
        plan.apply(
            "provider",
            {
                "provider": provider,
                "key": api_key,
                **_pick(options, "endpoint", "deployment", "host"),
            },
            confirm_overwrite=confirm_overwrite,
        )
    if database is not None:
        plan.apply(
            "database",
            {"database": database, "url": options.get("database_url")},
            confirm_overwrite=confirm_overwrite,
        )
    if tracing is not None:
        plan.apply(
            "tracing",
            {
                "tracing": tracing,
                "url": options.get("langfuse_url"),
                "public_key": options.get("public_key"),
                "secret_key": options.get("secret_key"),
            },
            confirm_overwrite=confirm_overwrite,
        )
    if start_services:
        wanted = plan.wanted_profiles(database=database, tracing=tracing)
        if wanted:
            plan.start(wanted)
    return plan


def _pick(options: Mapping[str, Any], *names: str) -> dict[str, Any]:
    return {name: options[name] for name in names if name in options}


__all__ = [
    "SECTIONS",
    "Choice",
    "EnvChanges",
    "SectionName",
    "SectionState",
    "SetupError",
    "SetupPlan",
    "setup",
]
