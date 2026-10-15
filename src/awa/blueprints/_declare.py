"""Declare the systems of a project folder: what the web app's Data step and `awa data add-system` share.

`declare_system` tests a system with the entered settings and only then saves them in `.env`
(owner-only), reads its structure read-only and records it in `data/SYSTEMS.json`.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awa._internal.filelock import locked_file
from awa.blueprints._systems import (
    SYSTEMS_FILE,
    DeclaredSystem,
    SystemOperation,
    SystemTable,
    check_key,
    system_key,
    systems_from_json,
    systems_json,
)
from awa.core.dotenv import read_env_file, write_env_file
from awa.errors import ConfigurationError
from awa.kernel.redaction import redact_values

_NOT_SYSTEMS = frozenset({"workspace", "web"})
"""Catalog connectors that hold none of the user's data: the local folder (that is "Files")
and the public web."""

DATABASE_KINDS: Mapping[str, tuple[str, str]] = {
    "sqlite": ("SQLite file", "C:/data/ledger.db or sqlite:///C:/data/ledger.db"),
    "postgresql": ("PostgreSQL", "postgresql://reader:password@db.example.com:5432/ledger"),
    "mysql": ("MySQL or MariaDB", "mysql://reader:password@db.example.com:3306/ledger"),
    "mssql": ("SQL Server", "mssql://reader:password@db.example.com:1433/ledger"),
}
"""The databases the `sql` connector reaches, with an example address each (a form placeholder)."""

_ADVICE = (
    (("must start with", "not a connection address", "is not an openapi", "not json or yaml",
      "valid https", "invalid url", "unknown url"),
     "The address is not in the form this kind of system expects. Check the example under the "
     "field and copy the address again from the person who manages the system."),
    (("needs psycopg", "needs pymysql", "needs pymssql", "extra", "pip install", "not installed"),
     "A part of awa that talks to this kind of database is not installed. Run the awa installer "
     "again (it installs every part), or install the extra named in the message."),
    (("password authentication", "access denied", "login failed", "401", "403", "unauthorized",
      "forbidden", "authentication", "permission denied", "invalid api key"),
     "The system refused the login. Check the user name and password (or the key) and that this "
     "login may read the data."),
    (("does not exist", "no such file", "unknown database", "404", "not found"),
     "The database, file or address was not found. Check its name and spelling."),
    (("timed out", "timeout"),
     "The system did not answer in time. Check the address and your network (a system in the "
     "company network may need the VPN)."),
    (("ssl", "certificate", "tls"),
     "The secure connection failed. Check that the address is right and that the system's "
     "certificate is valid; ask the person who manages it."),
    (("name or service", "getaddrinfo", "could not translate host", "resolve", "refused",
      "unreachable", "no route", "could not connect", "connection failed", "cannot connect"),
     "awa could not reach the system. Check the host name and port, your network and, for a "
     "system in the company network, the VPN."),
)  # fmt: skip

_NOT_NAMED = frozenset({"generic", "data"})
"""Catalog categories never taken from a description by name: their names (`rest`, `sql`,
`mcp`) are ordinary words and they hold nothing until the user points them at a system."""


@dataclass(frozen=True)
class Setting:
    """One setting a system reads (a `.env` variable); a `secret` is hidden and never shown again."""

    name: str
    description: str
    secret: bool
    required: bool
    example: str = ""


@dataclass(frozen=True)
class SystemType:
    """A kind of system the user can declare: a connector of the catalog."""

    connector: str
    title: str
    category: str
    summary: str
    docs: str = ""
    setup: str = ""


@dataclass
class Declared:
    """The outcome of `try_system` and `declare_system`.

    Attributes:
        message: What the system answered (secrets removed).
        warnings: What is worth knowing although it works (a login with write rights).
        saved: The setting names written to `.env` (only when the test passed).
        settings: The settings this system reads, as far as known now.
    """

    ok: bool
    message: str
    advice: str
    warnings: tuple[str, ...] = ()
    saved: list[str] = field(default_factory=list)
    system: DeclaredSystem | None = None
    settings: list[Setting] = field(default_factory=list)


def systems_path(directory: str | Path) -> Path:
    return Path(directory) / SYSTEMS_FILE


def read_declared_systems(directory: str | Path) -> list[DeclaredSystem]:
    """The systems declared in the project folder; raises `ConfigurationError` if `data/SYSTEMS.json` is damaged or newer."""

    path = systems_path(directory)
    if not path.is_file():
        return []
    return systems_from_json(path.read_text(encoding="utf-8"), where=str(path))


def _change_declared_systems(
    directory: str | Path, change: Callable[[list[DeclaredSystem]], list[DeclaredSystem]]
) -> list[DeclaredSystem]:
    """Apply `change` to the declared systems under a lock and save the result in one step.

    The file is removed when no system is left; returns the saved systems.
    """

    path = systems_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(path):
        systems = change(read_declared_systems(directory))
        if not systems:
            path.unlink(missing_ok=True)
            return systems
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(systems_json(systems))
        os.replace(temporary.name, path)
    return systems


def remove_declared_system(directory: str | Path, key: str) -> list[DeclaredSystem]:
    """Forget the declared system `key` (its settings stay in `.env`); return the others.

    Raises:
        ValueError: If no system has that key.
    """

    def remove(systems: list[DeclaredSystem]) -> list[DeclaredSystem]:
        kept = [system for system in systems if system.key != key]
        if len(kept) == len(systems):
            raise ValueError(f"There is no declared system {key!r}")
        return kept

    return _change_declared_systems(directory, remove)


def system_types() -> list[SystemType]:
    """The catalog connectors that can be read: one with an operation that changes nothing, or that lists its operations."""

    from awa.connectors import list_connectors

    found = []
    for spec in list_connectors():
        if spec.name in _NOT_SYSTEMS:
            continue
        if not spec.dynamic and not any(not op.write for op in spec.operations):
            continue
        found.append(
            SystemType(spec.name, spec.title, spec.category, spec.summary, spec.docs, spec.setup)
        )
    return found


def system_config(connector: str, key: str) -> dict[str, Any]:
    """The `connectors:` options of a new system `key` reached through `connector` (no secret).

    A database reads `<KEY>_DATABASE_URL`, so two databases never share a setting.

    Raises:
        ConfigurationError: If `connector` is not a kind of system.
    """

    if connector not in {t.connector for t in system_types()}:
        raise ConfigurationError(
            f"{connector!r} is not a kind of system awa can read; choose one of the list"
        )
    config: dict[str, Any] = {"use": connector}
    if connector == "sql":
        config["env"] = {"DATABASE_URL": f"{key.upper()}_DATABASE_URL"}
    return config


def system_settings(
    key: str,
    config: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    transport: Any = None,
) -> list[Setting]:
    """The settings the system reads, required ones first. For an OpenAPI interface the login
    settings come from its document, so they appear once `env` holds where the document is."""

    from awa.connectors._catalog import connector_context

    ctx = connector_context(key, env=dict(env or {}), config=config)
    first = ctx.spec.auth[0] if ctx.spec.auth else None
    needed = {v.name for v in ctx.spec.env if v.required}
    if first is not None and not ctx.spec.auth_optional:
        needed |= {v.name for v in first.variables() if v.required}
    renamed = dict(config.get("env") or {})
    settings = [
        Setting(
            renamed.get(v.name, v.name),
            v.description,
            v.secret,
            v.name in needed,
            v.example or v.default or "",
        )
        for v in ctx.spec.variables()
    ]
    if config.get("use") == "openapi" and env:
        from awa.connectors._structure import openapi_settings

        known = {s.name for s in settings}
        settings += [
            Setting(name, description, secret, required)
            for name, description, secret, required in openapi_settings(key, config, env, transport)
            if name not in known
        ]
    return sorted(settings, key=lambda s: not s.required)


def system_advice(message: str, ok: bool) -> str:
    """What to do about the outcome of a system test, in plain words."""

    if ok:
        return "The connection works."
    lowered = message.lower()
    if "not configured" in lowered:
        return "Enter the missing settings first, then test again."
    for words, text in _ADVICE:
        # Whole words only: `401` is no part of port 4011, `extra` none of `extra_costs`.
        if any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered) for word in words):
            return text
    return "Check the settings, or ask the person who manages this system for the right values."


def merged_env(
    environ: Mapping[str, str], env_file: Path, entered: Mapping[str, str]
) -> dict[str, str]:
    """What a test reads: the process environment, the project's `.env` for names it does not
    set, then the values just entered."""

    merged = {k: v for k, v in environ.items() if v}
    for name, value in read_env_file(env_file).items():
        if value and not merged.get(name):
            merged[name] = value
    merged.update({k: v.strip() for k, v in entered.items() if v.strip()})
    return merged


def try_system(
    directory: str | Path,
    *,
    connector: str,
    name: str,
    values: Mapping[str, str],
    environ: Mapping[str, str],
    key: str | None = None,
    transport: Any = None,
) -> Declared:
    """Test a system with the entered `values` over the saved settings; nothing is saved."""

    root = Path(directory)
    key, config = _new(root, connector, name, key)
    clean = _checked(key, config, values, environ, root, transport)
    env = merged_env(environ, root / ".env", clean)
    return _tested(key, config, env, clean, transport)


def declare_system(
    directory: str | Path,
    *,
    connector: str,
    name: str,
    values: Mapping[str, str],
    environ: Mapping[str, str],
    samples: bool = False,
    key: str | None = None,
    transport: Any = None,
) -> Declared:
    """Test the system; only when the test passes, save the settings, read its structure and record it.

    Args:
        values: The entered settings by variable name (blank ones are left as they are).
        environ: The process environment (settings already exported).
        samples: Read up to 3 example rows per table (never columns that look like secrets).
        key: Its instance name; derived from `name` by default.
        transport: Sends the HTTP requests (a `MockTransport` answers without a network).

    Raises:
        ValueError: If a name of `values` is not a setting of this system, or the name is empty.
        ConfigurationError: If `connector` is not a kind of system.
    """

    root = Path(directory)
    key, config = _new(root, connector, name, key)
    clean = _checked(key, config, values, environ, root, transport)
    env = merged_env(environ, root / ".env", clean)
    outcome = _tested(key, config, env, clean, transport)
    if not outcome.ok:
        return outcome
    from awa.connectors._structure import read_structure

    if connector == "sql":
        config = _database_options(config, env)
    try:
        structure = read_structure(key, config, env, samples=samples, transport=transport)
    except Exception as exc:  # noqa: BLE001 - shown with advice, secrets removed
        secrets = _secrets(outcome.settings, env, clean)
        text = redact_values(f"the structure could not be read: {exc}", secrets)
        return Declared(False, text, system_advice(text, False), outcome.warnings)
    root.mkdir(parents=True, exist_ok=True)
    outcome.saved = write_env_file(root / ".env", clean) if clean else []
    names = [s.name for s in outcome.settings if s.name in env]
    system = DeclaredSystem(
        key=key,
        name=" ".join(name.split()),
        connector=connector,
        kind=_kind(connector, env, config),
        options={k: v for k, v in config.items() if k != "use"},
        variables=tuple(names),
        tables=tuple(SystemTable.model_validate(t) for t in structure.tables),
        operations=tuple(SystemOperation.model_validate(o) for o in structure.operations),
        samples=samples,
        warnings=(*outcome.warnings, *structure.notes),
        tested=outcome.message,
    )
    _change_declared_systems(root, lambda current: [*(s for s in current if s.key != key), system])
    outcome.system = system
    return outcome


def refresh_system(
    directory: str | Path, key: str, *, environ: Mapping[str, str], transport: Any = None
) -> DeclaredSystem:
    """Read the structure of the declared system `key` again.

    Raises:
        ValueError: If no system has that key.
        AwaError: If it cannot be reached.
    """

    from awa.connectors._structure import read_structure

    root = Path(directory)
    systems = read_declared_systems(root)
    system = next((s for s in systems if s.key == key), None)
    if system is None:
        raise ValueError(f"There is no declared system {key!r}")
    config = {"use": system.connector, **system.options}
    config.pop("read_only", None)
    env = merged_env(environ, root / ".env", {})
    structure = read_structure(key, config, env, samples=system.samples, transport=transport)
    updated = system.model_copy(
        update={
            "tables": tuple(SystemTable.model_validate(t) for t in structure.tables),
            "operations": tuple(SystemOperation.model_validate(o) for o in structure.operations),
        }
    )
    _change_declared_systems(
        root, lambda current: [updated if s.key == key else s for s in current]
    )
    return updated


def named_systems(
    description: str, env: Mapping[str, str], *, taken: Iterable[str] = ()
) -> list[DeclaredSystem]:
    """The catalog connectors a process description names whose required settings are set in `env`.

    They are declared read-only for the design. A connector counts as named by its name, title
    or one of its setting names as a word; operations the description names become `include`.
    Connectors in `taken`, generic and database connectors are left out.
    """

    from awa.connectors import list_connectors

    lowered = description.casefold()
    skip = set(taken)
    found: list[DeclaredSystem] = []
    for spec in list_connectors():
        if spec.name in _NOT_SYSTEMS or spec.name in skip or spec.category in _NOT_NAMED:
            continue
        reads = [op for op in spec.operations if not op.write]
        if spec.dynamic or not reads:
            continue
        words = {spec.name, spec.name.replace("_", " "), spec.title}
        named = any(re.search(rf"\b{re.escape(w.casefold())}\b", lowered) for w in words if w)
        own = [v.name for v in spec.env]
        if not named and not any(re.search(rf"\b{re.escape(v)}\b", description) for v in own):
            continue
        if not _settings_set(spec, env):
            continue
        listed = [op.name for op in reads if re.search(rf"\b{op.name}\b", description)]
        chosen = [op for op in reads if not listed or op.name in listed]
        found.append(
            DeclaredSystem(
                key=spec.name,
                name=spec.title,
                connector=spec.name,
                kind=spec.title,
                options={"include": listed} if listed else {},
                variables=tuple(v.name for v in spec.variables() if env.get(v.name)),
                operations=tuple(
                    SystemOperation(name=op.name, summary=op.description.split(". ")[0])
                    for op in chosen
                ),
                tested="named in the description; its settings are set (not called yet)",
            )
        )
    return found


def _settings_set(spec: Any, env: Mapping[str, str]) -> bool:
    """Whether `env` sets the required settings of `spec` and, when it needs a login, every
    required setting of one of its ways to log in."""

    if any(v.required and not env.get(v.name) for v in spec.env):
        return False
    if not spec.auth or spec.auth_optional:
        return True
    return any(all(env.get(v.name) for v in auth.variables() if v.required) for auth in spec.auth)


def _new(root: Path, connector: str, name: str, key: str | None) -> tuple[str, dict[str, Any]]:
    if not name.strip():
        raise ValueError("Give the system a name, for example Invoice ledger")
    declared = read_declared_systems(root)
    if key:
        chosen = check_key(key)
    else:
        same = next((s for s in declared if s.name == " ".join(name.split())), None)
        chosen = same.key if same else system_key(name, {s.key for s in declared})
    return chosen, system_config(connector, chosen)


def _checked(
    key: str,
    config: Mapping[str, Any],
    values: Mapping[str, str],
    environ: Mapping[str, str],
    root: Path,
    transport: Any,
) -> dict[str, str]:
    """The non-blank `values`, after checking every name is a setting of this system."""

    clean = {name: str(value).strip() for name, value in values.items() if str(value).strip()}
    env = merged_env(environ, root / ".env", clean)
    allowed = {s.name for s in system_settings(key, config, env, transport=transport)}
    for name in values:
        if name not in allowed:
            raise ValueError(f"{name!r} is not a setting of this kind of system")
    if config.get("use") == "sql":  # a SQLite file is kept as an absolute address
        variable = dict(config.get("env") or {}).get("DATABASE_URL", "DATABASE_URL")
        typed = clean.get(variable, "")
        if typed and "://" not in typed and Path(typed).expanduser().is_file():
            clean[variable] = "sqlite:///" + Path(typed).expanduser().resolve().as_posix()
    return clean


def _tested(
    key: str,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    entered: Mapping[str, str],
    transport: Any,
) -> Declared:
    from awa.connectors._structure import test_system

    options = _database_options(config, env) if config.get("use") == "sql" else dict(config)
    result = test_system(key, options, env, transport=transport)
    settings = system_settings(key, options, env, transport=transport)
    secrets = _secrets(settings, env, entered)
    message = redact_values(result.message, secrets)
    return Declared(
        result.ok,
        message,
        system_advice(message, result.ok),
        tuple(redact_values(w, secrets) for w in result.warnings),
        settings=settings,
    )


def _database_options(config: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """SQL Server checks a query by its text only: the instance needs `accept_text_check`."""

    options = dict(config)
    url = _database_address(config, env)
    if url.lower().startswith(("mssql", "sqlserver")):
        options["accept_text_check"] = True
    return options


def _database_address(config: Mapping[str, Any], env: Mapping[str, str]) -> str:
    variable = dict(config.get("env") or {}).get("DATABASE_URL", "DATABASE_URL")
    return env.get(variable, "")


def _kind(connector: str, env: Mapping[str, str], config: Mapping[str, Any]) -> str:
    from awa.connectors import get_connector

    if connector == "sql":
        from awa.connectors._local import describe_database

        kind, _ = describe_database(_database_address(config, env))
        return f"{kind} database" if kind != "unknown" else "SQL database"
    if connector == "openapi":
        return "REST API (OpenAPI)"
    return get_connector(connector).title


def _secrets(
    settings: Sequence[Setting], env: Mapping[str, str], entered: Mapping[str, str]
) -> list[str]:
    """Values to scrub from a message: every value just entered, and the saved values of
    the system's secret settings."""

    saved = [env[s.name] for s in settings if s.secret and env.get(s.name)]
    return [*entered.values(), *saved]
