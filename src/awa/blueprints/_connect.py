"""`awa connect DIR`: walk through the connection steps of a built project.

Per system still on its workspace: ask for each missing variable (hidden), save it in `.env`,
test the connector and, once it passes, switch the `connectors:` entry to the real system.
Values are never printed. `--database NAME` tests and saves one database address.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from itertools import takewhile
from pathlib import Path
from typing import Any, TextIO

from awa.blueprints import _project_files as files
from awa.blueprints._data_files import SQLITE_SUFFIXES
from awa.blueprints._project import switch_tools
from awa.blueprints.schema import Blueprint
from awa.core.dotenv import read_env_file, write_env_file
from awa.errors import ConfigurationError

Ask = Callable[[str], str]
"""Reads one answer without echoing it: `getpass.getpass` in a terminal."""

ConnectorTest = Callable[[str, Mapping[str, Any]], tuple[bool, str]]
"""Tests a connector instance with its options: `(passed, message)`."""

_SAFETY = ("read_only", "requires_approval", "allowed_hosts")
"""Options of a stand-in that limit what the real system may do: kept when it is connected."""


def run_connect(
    directory: str | Path,
    *,
    secret: Ask = getpass.getpass,
    out: TextIO | None = None,
    test: ConnectorTest | None = None,
) -> int:
    """Walk through `directory/CONNECT.md`; returns 0 when every system is connected, else 1.

    Args:
        secret: Asks for a value without echoing it.
        test: Tests an instance; by default one small call through `check_connector`.

    Raises:
        ConfigurationError: If the folder has no `CONNECT.md` written by awa.
    """

    out = out if out is not None else sys.stdout
    root = Path(directory)
    guide = root / "CONNECT.md"
    if not guide.is_file():
        raise ConfigurationError(
            f"{str(root)!r} has no CONNECT.md; create a project first with `awa build`"
        )
    steps = files.parse_steps(guide.read_text(encoding="utf-8"))
    if not steps:
        out.write("There is nothing to connect.\n")
        return 0
    env_file = root / ".env"
    blueprint_file = root / "blueprint.yaml"
    tester = test or _check
    open_steps = 0
    for number, step in enumerate(steps, start=1):
        out.write(f"\n{number}. {step['system']}: ")
        current = Blueprint.from_yaml(blueprint_file).connectors.get(step["key"], {})
        if current.get("use") != "workspace":
            out.write("connected.\n")
        elif not _connect_step(step, env_file, blueprint_file, secret, out, tester):
            open_steps += 1
    return 0 if not open_steps else 1


def database_variable(system: str) -> str:
    """The `.env` name for the database of `system` (`"Shop DB"` -> `SHOP_DB_DATABASE_URL`).

    Raises:
        ValueError: If `system` has no letters or digits to build a name from.
    """

    name = re.sub(r"[^A-Z0-9]+", "_", system.upper()).strip("_")
    if not name or name[0].isdigit():
        raise ValueError("Give the database a short name that starts with a letter, e.g. shop")
    return f"{name}_DATABASE_URL"


def database_url(text: str) -> str:
    """The connection address from what was typed: a SQLite file path becomes `sqlite:///...`.

    Raises:
        ValueError: If it is empty, or a path that is not an existing SQLite file.
    """

    value = text.strip()
    if not value:
        raise ValueError("Enter the connection address or the path of a SQLite file")
    if "://" in value:
        return value
    path = Path(value.removeprefix("file:")).expanduser()
    if not path.is_file() or path.suffix.lower() not in SQLITE_SUFFIXES:
        raise ValueError(
            "Not a connection address (postgresql://, mysql://, mssql://, sqlite:///) and "
            "not an existing SQLite file (.db, .sqlite, .sqlite3)"
        )
    return "sqlite:///" + path.resolve().as_posix()


def check_database(url: str) -> tuple[bool, str]:
    """Test a database address: `(passed, message)`, the message naming tables (never rows, never the address)."""

    from awa.connectors import check_connector

    try:
        result = check_connector("sql", env={"DATABASE_URL": database_url(url)})
    except ValueError as exc:
        return False, str(exc)
    return result.ok, result.message


def database_snippet(system: str, url: str) -> str:
    """The blueprint lines that use the database saved for `system` (no secret in it)."""

    key = re.sub(r"[^a-z0-9]+", "_", system.lower()).strip("_")
    lines = [
        "connectors:",
        f"  {key}:",
        "    use: sql",
        f"    env: {{DATABASE_URL: {database_variable(system)}}}",
        "    read_only: true   # only schema and query; no tool that changes data",
    ]
    if url.lower().startswith(("mssql", "sqlserver")):
        lines.append("    accept_text_check: true   # SQL Server: give the login read-only rights")
    lines += [
        "agents:",
        "  - name: analyst",
        f"    tools: [connector:{key}.schema, connector:{key}.query]",
    ]
    return "\n".join(lines) + "\n"


def save_database(
    env_file: str | Path,
    system: str,
    url: str,
    *,
    test: Callable[[str], tuple[bool, str]] | None = None,
) -> tuple[bool, str, str]:
    """Test `url` and only when it passes save it as `<SYSTEM>_DATABASE_URL`; return `(saved, message, variable)`.

    Raises:
        ValueError: If the name or the address cannot be used.
    """

    variable = database_variable(system)
    address = database_url(url)
    passed, message = (test or check_database)(address)
    if not passed:
        return False, message, variable
    write_env_file(env_file, {variable: address})
    return True, message, variable


def run_connect_database(
    directory: str | Path, system: str, *, secret: Ask = getpass.getpass, out: TextIO | None = None
) -> int:
    """`awa connect DIR --database NAME`: ask for the address (hidden), test it, save it; 0 when saved."""

    out = out if out is not None else sys.stdout
    root = Path(directory)
    try:
        variable = database_variable(system)
        address = secret(f"{variable} (postgresql://, mysql://, mssql:// or SQLite file), hidden: ")
        root.mkdir(parents=True, exist_ok=True)
        saved, message, variable = save_database(root / ".env", system, address)
        url = database_url(address)
    except ValueError as exc:
        out.write(f"{exc}\n")
        return 1
    if not saved:
        out.write(f"The connection test failed: {message}\n")
        return 1
    out.write(f"Test passed: {message}\nSaved {variable} in {root / '.env'}\n")
    out.write("\nAdd this to blueprint.yaml (agents get the tools they list):\n\n")
    out.write(database_snippet(system, url))
    return 0


def _connect_step(
    step: Mapping[str, Any],
    env_file: Path,
    blueprint_file: Path,
    secret: Ask,
    out: TextIO,
    test: ConnectorTest,
) -> bool:
    key, connector = step["key"], step["connector"]
    through = "generic connector" if step["status"] == "map" else "connector"
    out.write(f"{through} {connector}.\n")
    saved = read_env_file(env_file)
    entered: dict[str, str] = {}
    login = list(step.get("login") or ())
    for variable in [*step["env_vars"], *login]:
        if os.environ.get(variable) or saved.get(variable):
            out.write(f"  {variable}: already set\n")
            continue
        hint = " (only if it asks for a login)" if variable in login else ""
        value = secret(f"  {variable}{hint}, hidden, empty to skip: ").strip()
        if value:
            entered[variable] = value
        else:
            out.write(f"  {variable}: skipped\n")
    if entered:
        write_env_file(env_file, entered)
        out.write(f"  saved {', '.join(entered)} in {env_file}\n")
    for variable in [*step["env_vars"], *login]:  # the connector test reads the environment
        stored = entered.get(variable) or saved.get(variable)
        if stored and not os.environ.get(variable):
            os.environ[variable] = stored
    missing = [variable for variable in step["env_vars"] if not os.environ.get(variable)]
    if missing:
        out.write(f"  still missing: {', '.join(missing)}; run `awa connect` again later\n")
        return False
    passed, message = test(key, step["config"])
    if not passed:
        out.write(f"  the connection test failed: {message}\n")
        return False
    out.write(f"  test passed: {message}\n")
    switch = {reference: tuple(targets) for reference, targets in step["switch"].items()}
    connectors = Blueprint.from_yaml(blueprint_file).connectors
    config = kept_safety(connectors.get(key) or {}, step["config"])
    if "read_only" not in config and _reads_only(switch, connectors):
        config["read_only"] = True  # the stand-in was only read: so is the real system
    _switch_file(blueprint_file, switch, {key: config})
    out.write(f"  connected: blueprint.yaml now uses {connector} for {step['system']}\n")
    dropped = [reference for reference, targets in switch.items() if not targets]
    if dropped:
        out.write(
            f"  {', '.join(dropped)} was removed: choose the operation that changes "
            f"{step['system']} with `awa connectors show {connector}` (CONNECT.md)\n"
        )
    return not dropped


def kept_safety(current: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """The real system's options `config`, keeping the stand-in's limits (`_SAFETY`) and, where possible, its `include` list."""

    merged = dict(config)
    for option in _SAFETY:
        if option in current and option not in merged:
            merged[option] = current[option]
    include = current.get("include")
    if include and "include" not in merged:
        from awa.connectors._catalog import connector_context

        try:
            operations = connector_context("system", env={}, config=merged).spec.operations
        except Exception:  # noqa: BLE001 - an unknown connector keeps its options as they are
            operations = ()
        kept = [name for name in include if name in {op.name for op in operations}]
        if kept:
            merged["include"] = kept
    return merged


def _reads_only(switch: Mapping[str, Sequence[str]], connectors: Mapping[str, Any]) -> bool:
    """Whether every stand-in tool being switched only reads (none changes or sends data)."""

    from awa.blueprints._resolve import connector_operations

    if not switch:
        return False
    for reference in switch:
        resolved = connector_operations(reference, connectors)
        if resolved is None or any(op.write for op in resolved[1]):
            return False
    return True


def _switch_file(
    path: Path, switch: Mapping[str, Sequence[str]], connectors: Mapping[str, Mapping[str, Any]]
) -> None:
    """Set connector options in a blueprint file and rename switched tool references, keeping its leading comment."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    header = "".join(takewhile(lambda line: line.startswith("#"), lines))
    blueprint = switch_tools(Blueprint.from_yaml_text(text), switch)
    data = blueprint.to_dict()
    data["connectors"] = {**blueprint.connectors, **connectors}
    path.write_text(header + Blueprint.from_dict(data).to_yaml(), encoding="utf-8", newline="\n")


def _check(name: str, config: Mapping[str, Any]) -> tuple[bool, str]:
    from awa.connectors import check_connector

    result = check_connector(name, config=config)
    return result.ok, result.message
