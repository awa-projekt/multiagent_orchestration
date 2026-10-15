"""SQL tools: `sql_schema`, `sql_query` (read-only) and `sql_execute` (approval by default).

SQLite is read-only three times over (read-only file, `PRAGMA query_only`, an authorizer). For
another database (`connect=`) `sql_query` runs one `SELECT`/`WITH` that a dialect-aware text
check passes, then rolls back; the check is a safety net, so give the connection a read-only
role. `sql_schema` reads only the catalog inside the same read-only session.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field

from awa._internal.upstream.tools import BaseTool, ToolException
from awa.errors import ConfigurationError
from awa.toolkits import _sql_schema
from awa.toolkits._common import (
    DEFAULT_MAX_OUTPUT_CHARS,
    ToolArgs,
    ToolSpecs,
    positive,
    selected,
    tool_json,
    toolkit_tools,
)
from awa.toolkits._sql_guard import DIALECTS, check_read_only_sql, clean_dialects

SQL_TOOLS = ("sql_schema", "sql_query", "sql_execute")
"""Names of the tools `sql_toolkit` builds, in order."""

_Param = str | int | float | bool | None
_MAX_CELL = 500

_READ_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)
_NEVER_ACTIONS = frozenset({sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA})
_BLOCKED_FUNCTIONS = frozenset(
    {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}
)


class SqlQueryArgs(ToolArgs):
    query: str = Field(description="One SELECT (or WITH ... SELECT) statement.")
    parameters: list[_Param] | None = Field(
        default=None, description="Values for the statement's placeholders, in order."
    )
    limit: int | None = Field(default=None, ge=1, description="Most rows returned.")


class SqlExecuteArgs(ToolArgs):
    statement: str = Field(description="One INSERT, UPDATE, DELETE or DDL statement.")
    parameters: list[_Param] | None = Field(
        default=None, description="Values for the statement's placeholders, in order."
    )


def _cell(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, str) and len(value) > _MAX_CELL:
        return f"{value[:_MAX_CELL]}… ({len(value)} characters)"
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float | Decimal):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, bytes | bytearray | memoryview):
        return "binary"
    return type(value).__name__.lower()


@dataclass(frozen=True)
class _Sql:
    database: Path | None
    connect: Callable[[], Any] | None
    max_rows: int
    timeout: float
    dialect: str | None = None

    def _sqlite(self, *, read_only: bool) -> sqlite3.Connection:
        assert self.database is not None
        mode = "ro" if read_only else "rw"
        uri = f"{self.database.as_uri()}?mode={mode}"
        try:
            connection = sqlite3.connect(
                uri, uri=True, timeout=self.timeout, check_same_thread=False
            )
        except sqlite3.OperationalError as exc:
            raise ToolException(f"cannot open the database: {exc}") from None
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        connection.set_authorizer(self._reader if read_only else self._writer)
        deadline = time.monotonic() + self.timeout
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 5_000)
        return connection

    @staticmethod
    def _reader(action: int, arg1: str | None, *_: Any) -> int:
        if action == sqlite3.SQLITE_FUNCTION and (arg1 or "").lower() in _BLOCKED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY

    @staticmethod
    def _writer(action: int, arg1: str | None, *_: Any) -> int:
        if action in _NEVER_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg1 or "").lower() in _BLOCKED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _run_sqlite(self, statement: str, parameters: Sequence[Any], limit: int) -> str:
        connection = self._sqlite(read_only=True)
        try:
            cursor = connection.execute(statement, tuple(parameters))
            rows = cursor.fetchmany(limit + 1)
            columns = [column[0] for column in cursor.description or ()]
            covered = [] if rows else self._sqlite_coverage(connection, statement)
        except sqlite3.DatabaseError as exc:
            raise ToolException(self._sqlite_error(exc)) from None
        except sqlite3.Warning as exc:  # several statements
            raise ToolException(f"only a single statement is allowed ({exc})") from None
        finally:
            connection.close()
        return self._result(columns, rows, limit, covered)

    @staticmethod
    def _sqlite_coverage(connection: sqlite3.Connection, statement: str) -> list[str]:
        connection.set_authorizer(None)  # the file is read-only; the statements are fixed
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        ]
        tables = [
            (
                name,
                connection.execute(
                    "SELECT name, type FROM pragma_table_info(?)", (name,)
                ).fetchall(),
            )
            for name in _sql_schema.query_tables(statement, names)
        ]

        def run(text: str, parameters: Sequence[Any]) -> list[tuple[Any, ...]]:
            return connection.execute(text, tuple(parameters)).fetchall()

        return _sql_schema.coverage("sqlite", None, tables, run)

    def _sqlite_error(self, exc: sqlite3.Error, *, writing: bool = False) -> str:
        text = str(exc)
        if "one statement at a time" in text:  # Python 3.12+; 3.11 raises sqlite3.Warning
            return "only a single statement is allowed"
        if writing and "not authorized" in text:
            return (
                f"the statement was refused: {text} "
                "(ATTACH, DETACH and PRAGMA are never allowed, and neither is loading extensions)"
            )
        if "not authorized" in text or "readonly" in text or "read-only" in text:
            return f"the query was refused: {text} (sql_query is read-only)"
        if "interrupted" in text:
            return f"the query was stopped after {self.timeout:g} seconds"
        return f"the database reported an error: {text}"

    def _run_dbapi(self, statement: str, parameters: Sequence[Any], limit: int) -> str:
        assert self.connect is not None
        known = self.dialect if self.dialect in DIALECTS else None
        if known is not None:
            check_read_only_sql(statement, known)
            clean: tuple[str, ...] = (known,)
        else:
            clean = clean_dialects(statement)  # refuses here, before any connection is opened
        connection = self.connect()
        try:
            if known is None and len(clean) < len(DIALECTS):
                self._require_clean_for_driver(connection, clean)
            _refuse_autocommit(connection)
            try:
                with contextlib.closing(connection.cursor()) as cursor:
                    _execute(cursor, statement, parameters)
                    rows = cursor.fetchmany(limit + 1)
                    columns = [column[0] for column in cursor.description or ()]
                    covered = [] if rows else self._dbapi_coverage(connection, cursor, statement)
            finally:
                connection.rollback()
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException(f"the database reported an error: {exc}") from None
        finally:
            connection.close()
        return self._result(columns, rows, limit, covered)

    def _dbapi_coverage(self, connection: Any, cursor: Any, statement: str) -> list[str]:
        """Rows and date ranges of the queried tables; empty when anything goes wrong."""

        dialect = self.dialect or _sql_schema.detect_dialect(connection)
        if dialect not in DIALECTS:
            return []

        def run(text: str, parameters: Sequence[Any]) -> list[tuple[Any, ...]]:
            check_read_only_sql(text, dialect)
            _execute(cursor, text, parameters)
            return [tuple(row) for row in cursor.fetchall()]

        try:
            connection.rollback()  # a failed statement must not poison the hint queries
            schema = str(run(_sql_schema.DEFAULT_SCHEMA[dialect], ())[0][0])
            listed = run(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
                (schema,),
            )
        except Exception:
            return []
        columns: dict[str, list[tuple[str, str]]] = {}
        for table, column, ctype in listed:
            columns.setdefault(str(table), []).append((str(column), str(ctype)))
        tables = [(t, columns[t]) for t in _sql_schema.query_tables(statement, list(columns))]
        return _sql_schema.coverage(dialect, schema, tables, run)

    @staticmethod
    def _require_clean_for_driver(connection: Any, clean: tuple[str, ...]) -> None:
        """Refuse a query that reads safely in some dialects only, unless the driver is one."""

        dialect = _sql_schema.detect_dialect(connection)
        if dialect not in clean:
            raise ToolException(
                "the query reads differently in other SQL dialects and the database type "
                f"({dialect or 'unknown'}) is not one where it is safe; use plain SQL (double a "
                "quote inside text, no backslashes, no $$ strings) or pass dialect= to sql_toolkit"
            )

    def _result(
        self,
        columns: list[str],
        rows: Sequence[Sequence[Any]],
        limit: int,
        covered: Sequence[str] = (),
    ) -> str:
        shown = rows[:limit]
        types = [
            next((_type_name(row[i]) for row in shown if row[i] is not None), "unknown")
            for i in range(len(columns))
        ]
        result: dict[str, Any] = {
            "columns": columns,
            "types": types,
            "rows": [[_cell(value) for value in row] for row in shown],
            "returned": len(shown),
            "has_more": len(rows) > limit,
        }
        if not shown:
            result["note"] = (
                "0 rows matched: the query ran without error, but no row meets its conditions. "
                + " ".join(covered)
                + (" " if covered else "")
                + "Check the filter values and the period against the data (sql_schema lists "
                "the values of text columns) before reporting that there is no data."
            )
        if result["has_more"]:
            result["note"] = (
                f"Only the first {limit} rows are shown. Add a WHERE condition, an aggregate "
                "(COUNT, SUM, GROUP BY) or a smaller LIMIT instead of reading everything."
            )
        return tool_json(result)

    def schema(self, args: _sql_schema.SqlSchemaArgs) -> str:
        if self.database is not None:
            connection = self._sqlite(read_only=True)
            connection.set_authorizer(None)  # the file is read-only; the statements are fixed
            try:
                return tool_json(_sql_schema.schema_sqlite(connection, args))
            except sqlite3.DatabaseError as exc:
                raise ToolException(self._sqlite_error(exc)) from None
            finally:
                connection.close()
        assert self.connect is not None
        connection = self.connect()
        try:
            dialect = self.dialect or _sql_schema.detect_dialect(connection)
            if dialect is None:
                raise ToolException("the database type is unknown; pass dialect= to sql_toolkit")
            try:
                with contextlib.closing(connection.cursor()) as cursor:

                    def run(statement: str, parameters: Sequence[Any]) -> list[tuple[Any, ...]]:
                        check_read_only_sql(statement, dialect)
                        _execute(cursor, statement, parameters)
                        return [tuple(row) for row in cursor.fetchall()]

                    return tool_json(_sql_schema.schema_dbapi(run, dialect, args))
            finally:
                connection.rollback()
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException(f"the database reported an error: {exc}") from None
        finally:
            connection.close()

    def query(self, args: SqlQueryArgs) -> str:
        limit = min(args.limit or self.max_rows, self.max_rows)
        parameters = args.parameters or []
        if self.database is not None:
            return self._run_sqlite(args.query, parameters, limit)
        return self._run_dbapi(args.query, parameters, limit)

    def execute(self, args: SqlExecuteArgs) -> str:
        parameters = tuple(args.parameters or ())
        if re.match(r"\s*vacuum\b", args.statement, re.IGNORECASE):
            raise ToolException("VACUUM is not allowed")
        if self.database is not None:
            connection = self._sqlite(read_only=False)
            try:
                cursor = connection.execute(args.statement, parameters)
                connection.commit()
                affected = cursor.rowcount
            except sqlite3.Warning as exc:
                raise ToolException(f"only a single statement is allowed ({exc})") from None
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise ToolException(self._sqlite_error(exc, writing=True)) from None
            finally:
                connection.close()
        else:
            assert self.connect is not None
            connection = self.connect()
            try:
                with contextlib.closing(connection.cursor()) as cursor:
                    _execute(cursor, args.statement, parameters)
                    affected = cursor.rowcount
                connection.commit()
            except Exception as exc:
                connection.rollback()
                raise ToolException(f"the database reported an error: {exc}") from None
            finally:
                connection.close()
        return tool_json(
            {"rows_affected": affected if affected is not None and affected >= 0 else None}
        )


def sql_toolkit(
    database: str | os.PathLike[str] | None = None,
    *,
    connect: Callable[[], Any] | None = None,
    dialect: str | None = None,
    include: Sequence[str] | None = None,
    requires_approval: bool = True,
    max_rows: int = 200,
    timeout: float = 10.0,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    description: str | None = None,
    frame_output: bool = True,
) -> list[BaseTool]:
    """Build `sql_schema` and `sql_query` (read-only) and `sql_execute` (approval by default).

    Args:
        database: Path of an existing SQLite database file.
        connect: Instead of `database`: a function returning a new DB-API connection per call.
        dialect: With `connect=`: `"postgresql"`, `"mysql"` or `"mssql"`, for `sql_schema`
            (found from the driver psycopg, pymysql or pymssql when omitted).
        include: Build only these tools (names from `SQL_TOOLS`).
        timeout: Seconds a SQLite statement may run (and waits for a lock). With `connect=`
            the connection's own statement time limit applies instead (the `sql` connector
            limits its queries to 30 s); give your connections a limit of their own.
        description: What the database contains, added to the tool descriptions, so the model
            writes correct queries.
        frame_output: Label the rows `sql_query` and `sql_schema` return as external data: a row
            can carry injected instructions.

    Raises:
        ConfigurationError: If neither or both of `database` and `connect` are given, the
            database file does not exist, or a limit is not positive.

    Example:
        ```python
        tools = sql_toolkit("shop.db", description="Tables: orders(id, customer, total).")
        ```
    """

    if (database is None) == (connect is None):
        raise ConfigurationError("sql_toolkit needs either a database path or connect=")
    positive(max_rows, "max_rows")
    positive(timeout, "timeout")
    path: Path | None = None
    if database is not None:
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise ConfigurationError(f"SQLite database {str(database)!r} does not exist")
    names = selected(SQL_TOOLS, include, "sql")
    impl = _Sql(database=path, connect=connect, max_rows=max_rows, timeout=timeout, dialect=dialect)
    about = f" {description.strip()}" if description else ""
    specs: ToolSpecs = {
        "sql_schema": (
            impl.schema,
            _sql_schema.SqlSchemaArgs,
            "Describe the database: tables and views with columns, types, primary and "
            "foreign keys and approximate row counts. Call this first to learn which "
            "tables and columns exist, then write queries with sql_query. Filter with "
            f"table; sample_rows (at most {_sql_schema.MAX_SAMPLE_ROWS}) adds example "
            f"rows. Text columns with few distinct values list each value with its row count; "
            f"use these exact spellings in filters.{about}",
        ),
        "sql_query": (
            impl.query,
            SqlQueryArgs,
            "Run one read-only SQL query (SELECT or WITH) against the database and "
            f"return columns and rows (at most {max_rows}). Use placeholders with "
            "parameters for values; call sql_schema first when the tables are "
            f"unknown.{about}",
        ),
        "sql_execute": (
            impl.execute,
            SqlExecuteArgs,
            "Run one SQL statement that changes the database (INSERT, UPDATE, DELETE "
            f"or DDL) and return the number of affected rows. Needs human approval.{about}",
        ),
    }
    return toolkit_tools(
        "sql",
        names,
        specs,
        requires_approval=requires_approval,
        framed=("sql_schema", "sql_query") if frame_output else (),
        max_output_chars=max_output_chars,
    )


def _execute(cursor: Any, statement: str, parameters: Sequence[Any]) -> None:
    """Run `statement` on a DB-API cursor, without a parameter argument when there is none."""

    if parameters:
        cursor.execute(statement, tuple(parameters))
    else:
        cursor.execute(statement)


def _refuse_autocommit(connection: Any) -> None:
    """Switch autocommit off, so the closing rollback undoes anything the query did.

    A driver whose `autocommit` is a method (PyMySQL, pymssql) is switched with
    `autocommit(False)` and must confirm the state through `get_autocommit()` or
    `autocommit_state`.

    Raises:
        ToolException: If the connection stays in autocommit mode or cannot confirm it is off.
    """

    switch = getattr(connection, "autocommit", False)
    if callable(switch):
        with contextlib.suppress(Exception):
            switch(False)
        on = _autocommit_state(connection) is not False
    elif switch:
        with contextlib.suppress(Exception):
            connection.autocommit = False
        on = bool(getattr(connection, "autocommit", False))
    else:
        return
    if on:
        raise ToolException(
            "the connection is in autocommit mode, so a query could not be rolled back; "
            "return a connection with autocommit off from connect="
        )


def _autocommit_state(connection: Any) -> bool | None:
    """The autocommit state a driver with an `autocommit()` method reports, or `None`."""

    getter = getattr(connection, "get_autocommit", None)
    try:
        state = getter() if callable(getter) else getattr(connection, "autocommit_state", None)
    except Exception:
        return None
    return state if isinstance(state, bool) else None


__all__ = ["SQL_TOOLS", "check_read_only_sql", "sql_toolkit"]
