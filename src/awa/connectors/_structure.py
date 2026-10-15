"""Test a system and read its structure, read-only; no message contains a credential."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from awa._internal.upstream.tools import ToolException
from awa.connectors._catalog import check_connector, connector_context, health_arguments
from awa.connectors._engine import ConnectorContext, Transport, call_operation
from awa.connectors._local import database_connector, database_url, schema_tool
from awa.connectors._openapi import instance_spec
from awa.connectors._spec import Operation
from awa.errors import AwaError

_SAMPLE_ROWS = 3
_WRITE_RIGHTS = (
    "The login can change data in this database. awa only reads it (every query runs in a "
    "read-only session), but a read-only login is safer: ask the person who manages the database "
    "for one, for example GRANT SELECT ON ALL TABLES IN SCHEMA public TO <login> (PostgreSQL)."
)
_WRITE_RIGHTS_MYSQL = (
    "The login can change data in this database. awa only reads it (every query runs in a "
    "read-only transaction), but a read-only login is safer: ask the person who manages the "
    "database for one, for example GRANT SELECT ON <database>.* TO <login> (MySQL)."
)
_WRITE_RIGHTS_MSSQL = (
    "The login can change data in this database. SQL Server has no read-only session, so awa "
    "checks each query's text only: give this system a login with read rights only "
    "(db_datareader)."
)


@dataclass(frozen=True)
class SystemCheck:
    """The outcome of `test_system` (never with a credential); `warnings` such as write rights."""

    ok: bool
    message: str
    warnings: tuple[str, ...] = ()
    seconds: float = 0.0


@dataclass(frozen=True)
class Structure:
    """What a system holds, read from its catalog or its API description (no secrets).

    `tables` hold per table or view `name`, `kind`, `rows`, `columns`, `keys` (and samples when
    asked); `operations` hold `name`, `summary`, `method`, `path`, `write`.
    """

    tables: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def test_system(
    key: str,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    transport: Transport | None = None,
) -> SystemCheck:
    """Test the system `key` with `config` and the settings in `env` (nothing is saved).

    A database is asked `SELECT 1` and for its table names; an OpenAPI interface without a
    `health` option gets one operation that changes nothing and needs no argument as its test call
    (when the document has one; otherwise reading the document is the test).
    """

    started = time.monotonic()
    options = dict(config)
    if options.get("use") == "openapi":
        return _test_api(key, options, env, transport, started)
    result = check_connector(key, env=env, config=options, transport=transport)
    warnings: tuple[str, ...] = ()
    if result.ok and options.get("use") == "sql":
        with contextlib.suppress(AwaError, ToolException, OSError, ValueError):
            warnings = _write_rights(connector_context(key, env=env, config=options))
    return SystemCheck(result.ok, result.message, warnings, time.monotonic() - started)


def _test_api(
    key: str,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    transport: Transport | None,
    started: float,
) -> SystemCheck:
    """Read the OpenAPI document, then call one argument-free read (or the `health` operation)."""

    ctx = connector_context(key, env=env, config=config, transport=transport)
    try:
        ctx = _converted(ctx)
        missing = ctx.missing()
        if missing:
            return SystemCheck(False, f"not configured: set {', '.join(missing)}")
        reads = [op for op in ctx.operations() if not op.write]
        wanted = config.get("health")
        probe = next(
            (
                op
                for op in reads
                if (op.name == wanted) or (not wanted and not any(p.required for p in op.params))
            ),
            None,
        )
        if probe is None:
            message = (
                f"the API description was read: {len(reads)} operation(s) that read data; none "
                "can be called without arguments, so the login is checked at the first real call"
            )
        else:
            call_operation(ctx, probe, health_arguments(probe))
            message = f"{ctx.spec.title} answered ({probe.name})"
    except (AwaError, ToolException, OSError, ValueError) as exc:
        return SystemCheck(False, ctx.redact(str(exc)), (), time.monotonic() - started)
    return SystemCheck(True, ctx.redact(message), (), time.monotonic() - started)


def read_structure(
    key: str,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    samples: bool = False,
    transport: Transport | None = None,
) -> Structure:
    """What the system `key` holds: a database's tables, an API's operations.

    Raises:
        AwaError: If the system cannot be reached or described (the message names no secret).
    """

    ctx = connector_context(key, env=env, config=config, transport=transport)
    if ctx.spec.name == "sql":
        return _database_structure(ctx, _SAMPLE_ROWS if samples else 0)
    if ctx.spec.name == "openapi":
        ctx = _converted(ctx)
    if ctx.spec.dynamic:
        return Structure(notes=[f"{ctx.spec.title} lists its operations only when it runs"])
    return Structure(operations=[_operation(op) for op in ctx.operations()])


def _database_structure(ctx: ConnectorContext, sample_rows: int) -> Structure:
    answer = str(schema_tool(*database_url(ctx)).invoke({"sample_rows": sample_rows}))
    try:
        found = json.loads(answer)
    except ValueError:
        raise ToolException(ctx.redact(answer.removeprefix("Error:").strip())) from None
    tables = []
    for table in found.get("tables", []):
        entry: dict[str, Any] = {
            "name": str(table["name"]),
            "kind": "view" if table.get("type") == "view" else "table",
            "rows": table.get("approx_rows"),
            "columns": [str(c) for c in table.get("columns", [])],
            "keys": [str(k) for k in table.get("keys", [])],
        }
        sample = table.get("sample")
        if sample_rows and sample:
            entry["sample_columns"] = [str(c) for c in sample["columns"]]
            entry["sample_rows"] = [list(row) for row in sample["rows"]]
        tables.append(entry)
    notes = []
    if found.get("more_tables"):
        notes.append(
            f"{len(found['more_tables'])} more table(s) are listed by name only: "
            + ", ".join(found["more_tables"][:20])
        )
    return Structure(tables=tables, notes=notes)


_RIGHTS = {
    "postgresql": (
        "SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) OR EXISTS ("
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE'))"
    ),
    "mssql": (
        "SELECT CASE WHEN IS_SRVROLEMEMBER('sysadmin') = 1 OR IS_ROLEMEMBER('db_owner') = 1 "
        "OR IS_ROLEMEMBER('db_datawriter') = 1 THEN 1 ELSE 0 END"
    ),
}


def _write_rights(ctx: ConnectorContext) -> tuple[str, ...]:
    """A warning when the login of a database server may change data, else `()`."""

    dialect, url, path = database_url(ctx)
    if path is not None:
        return ()
    connection = database_connector(dialect, url, read_only=False)()
    try:
        cursor = connection.cursor()
        if dialect == "mysql":
            cursor.execute("SHOW GRANTS")
            writes = any(_mysql_writes(str(row[0])) for row in cursor.fetchall())
        else:
            cursor.execute(_RIGHTS[dialect])
            row = cursor.fetchone()
            writes = bool(row and row[0])
        cursor.close()
    finally:
        with contextlib.suppress(Exception):
            connection.rollback()
        connection.close()
    if not writes:
        return ()
    advice = {"mssql": _WRITE_RIGHTS_MSSQL, "mysql": _WRITE_RIGHTS_MYSQL}
    return (advice.get(dialect, _WRITE_RIGHTS),)


def _mysql_writes(grant: str) -> bool:
    """Whether a `SHOW GRANTS` line gives a privilege that changes data."""

    found = re.match(r"\s*GRANT\s+(.+?)\s+ON\s", grant, re.IGNORECASE | re.DOTALL)
    privileges = {p.strip().upper() for p in found[1].split(",")} if found else set()
    return bool(privileges & {"ALL", "ALL PRIVILEGES", "INSERT", "UPDATE", "DELETE"})


def _converted(ctx: ConnectorContext) -> ConnectorContext:
    """`ctx` with the operations its OpenAPI tools would have (the document is read now)."""

    return ctx.derive(instance_spec(ctx))


def openapi_settings(
    key: str, config: Mapping[str, Any], env: Mapping[str, str], transport: Transport | None
) -> list[tuple[str, str, bool, bool]]:
    """The settings an OpenAPI interface reads, from its document's security schemes:
    `(name, description, secret, required)` each (empty when the document cannot be read)."""

    try:
        ctx = _converted(connector_context(key, env=env, config=config, transport=transport))
    except (AwaError, ToolException, OSError, ValueError):
        return []
    found: list[tuple[str, str, bool, bool]] = []
    first = ctx.spec.auth[0] if ctx.spec.auth else None
    for variable in ctx.spec.variables():
        required = variable.required or (
            first is not None and variable in first.variables() and not ctx.spec.auth_optional
        )
        found.append((variable.name, variable.description, variable.secret, required))
    return found


def _operation(operation: Operation) -> dict[str, Any]:
    return {
        "name": operation.name,
        "summary": " ".join(str(operation.description).split())[:240],
        "method": operation.method if operation.path else "",
        "path": operation.path,
        "write": bool(operation.write),
    }
