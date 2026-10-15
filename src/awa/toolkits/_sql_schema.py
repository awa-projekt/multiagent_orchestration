"""`sql_schema`: what a database holds (tables, columns, keys, sizes), written for a model to read.

Every statement is a fixed catalog query; table names are passed as parameters or, for sample
rows, quoted after they came out of the catalog.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import Field

from awa._internal.upstream.tools import ToolException
from awa.toolkits._common import ToolArgs

MAX_SAMPLE_ROWS = 5
"""Hard cap of `sample_rows`."""

_MAX_TABLES = 60  # described in detail; the rest are listed by name
_MAX_NAMES = 300
_MAX_COLUMNS = 80
_MAX_VALUE = 60
PROFILE_MAX_VALUES = 50
"""A text column with at most this many distinct values is listed with each value's count."""
PROFILE_MAX_CHARS = 1_500
"""Cap on the value listings of one table (characters of the rendered listing)."""
_PROFILE_MAX_ROWS = 1_000_000  # larger tables are not profiled (one GROUP BY per column)
_TEXT_TYPE = re.compile(r"char|text|clob|string|enum|citext", re.IGNORECASE)
_DATE_TYPE = re.compile(r"date|time", re.IGNORECASE)
_DATE_NAME = re.compile(
    r"(^|_)(date|time|datetime|timestamp|at|on|day|week|month|year|period)(_|$)|_(start|end)$",
    re.IGNORECASE,
)
_CAMEL_DATE_NAME = re.compile(r"[a-z](Date|Time|At|On)$")
"""`createdAt`, `dueDate`: a date named in camel case (`update_type`, `candidate` are none)."""
_SOURCE = re.compile(
    r"\b(?:from|join)\s+((?:[\w$]+|\"[^\"]+\"|`[^`]+`|\[[^\]]+\])"
    r"(?:\s*\.\s*(?:[\w$]+|\"[^\"]+\"|`[^`]+`|\[[^\]]+\]))*)",
    re.IGNORECASE,
)
_MAX_RANGE_COLUMNS = 4
_NUMBER_TYPE = re.compile(r"int|real|num|float|double|dec|bool|money|serial", re.IGNORECASE)
_IDENTIFIER = re.compile(r"(^|_)(id|uuid|guid|key|number|no)$", re.IGNORECASE)

_SECRET_COLUMN = re.compile(
    r"pass(word|wd)?|secret|token|api_?key|hash|salt|credential|private_?key|ssn|iban|cvv"
    r"|card_?(number|no)|(^|_)(pin|otp)(_|$)",
    re.IGNORECASE,
)

_DRIVERS = {  # first part of the connection's module name -> dialect (all use `%s` placeholders)
    "psycopg": "postgresql",
    "psycopg2": "postgresql",
    "pymysql": "mysql",
    "pymssql": "mssql",
}


class SqlSchemaArgs(ToolArgs):
    table: str | None = Field(
        default=None, description="Only tables and views whose name contains this text (any case)."
    )
    sample_rows: int = Field(
        default=0,
        ge=0,
        le=MAX_SAMPLE_ROWS,
        description=f"Example rows per table (0 to {MAX_SAMPLE_ROWS}); columns that look like "
        "secrets are left out.",
    )
    schema_name: str | None = Field(
        default=None,
        description="Schema to describe (not SQLite); the connection's own by default.",
    )


DEFAULT_SCHEMA = {
    "postgresql": "SELECT current_schema()",
    "mysql": "SELECT DATABASE()",
    "mssql": "SELECT SCHEMA_NAME()",
}
# (statement, number of schema parameters): name, kind, approximate rows.
_TABLES = {
    "postgresql": (
        "SELECT c.relname, CASE WHEN c.relkind IN ('v', 'm') THEN 'view' ELSE 'table' END, "
        "c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') ORDER BY c.relname",
        1,
    ),
    "mysql": (
        "SELECT table_name, CASE WHEN table_type = 'VIEW' THEN 'view' ELSE 'table' END, "
        "table_rows FROM information_schema.tables WHERE table_schema = %s "
        "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY table_name",
        1,
    ),
    "mssql": (
        "SELECT t.TABLE_NAME, CASE WHEN t.TABLE_TYPE = 'VIEW' THEN 'view' ELSE 'table' END, "
        "c.n FROM INFORMATION_SCHEMA.TABLES t LEFT JOIN (SELECT o.name AS name, "
        "SUM(p.rows) AS n FROM sys.objects o JOIN sys.partitions p ON p.object_id = o.object_id "
        "AND p.index_id IN (0, 1) WHERE SCHEMA_NAME(o.schema_id) = %s GROUP BY o.name) c "
        "ON c.name = t.TABLE_NAME WHERE t.TABLE_SCHEMA = %s ORDER BY t.TABLE_NAME",
        2,
    ),
}
_COLUMNS = (  # {marks} is one %s per table
    "SELECT table_name, column_name, data_type, is_nullable, character_maximum_length "
    "FROM information_schema.columns WHERE table_schema = %s AND table_name IN ({marks}) "
    "ORDER BY table_name, ordinal_position"
)
# Keys: rows of (table, constraint, column, referenced table, referenced column); PostgreSQL
# gives the finished definition instead (table, text).
_KEYS_POSTGRESQL = (
    "SELECT cl.relname, pg_get_constraintdef(con.oid) FROM pg_constraint con "
    "JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace "
    "WHERE con.contype IN ('p', 'f') AND n.nspname = %s AND cl.relname IN ({marks}) "
    "ORDER BY cl.relname, con.conname"
)
_KEYS_MYSQL = (
    "SELECT table_name, constraint_name, column_name, referenced_table_name, "
    "referenced_column_name FROM information_schema.key_column_usage "
    "WHERE table_schema = %s AND table_name IN ({marks}) "
    "AND (constraint_name = 'PRIMARY' OR referenced_table_name IS NOT NULL) "
    "ORDER BY table_name, constraint_name, ordinal_position"
)
_KEYS_MSSQL = (
    "SELECT kcu.table_name, kcu.constraint_name, kcu.column_name, NULL, NULL "
    "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
    "ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema "
    "AND kcu.table_name = tc.table_name WHERE tc.constraint_type = 'PRIMARY KEY' "
    "AND tc.table_schema = %s AND tc.table_name IN ({marks}) "
    "ORDER BY kcu.table_name, kcu.constraint_name, kcu.ordinal_position",
    "SELECT OBJECT_NAME(f.parent_object_id), OBJECT_NAME(f.constraint_object_id), "
    "COL_NAME(f.parent_object_id, f.parent_column_id), OBJECT_NAME(f.referenced_object_id), "
    "COL_NAME(f.referenced_object_id, f.referenced_column_id) "
    "FROM sys.foreign_key_columns f JOIN sys.objects o ON o.object_id = f.parent_object_id "
    "WHERE SCHEMA_NAME(o.schema_id) = %s AND OBJECT_NAME(f.parent_object_id) IN ({marks}) "
    "ORDER BY OBJECT_NAME(f.parent_object_id), OBJECT_NAME(f.constraint_object_id), "
    "f.constraint_column_id",
)

Run = Callable[[str, Sequence[Any]], list[tuple[Any, ...]]]


def detect_dialect(connection: Any) -> str | None:
    """The dialect of a DB-API connection from its driver, or `None`."""

    return _DRIVERS.get(type(connection).__module__.partition(".")[0])


def _kind(value: Any) -> str:
    return "view" if str(value).lower() == "view" else "table"


def _approx(value: Any) -> int | None:
    try:
        return int(value) if value is not None and int(value) >= 0 else None
    except (TypeError, ValueError):
        return None


def _constraints(rows: Sequence[Sequence[Any]]) -> dict[str, list[str]]:
    """Group (table, constraint, column, ref table, ref column) rows into definitions."""

    grouped: dict[tuple[str, str], list[Sequence[Any]]] = {}
    for row in rows:
        grouped.setdefault((row[0], row[1]), []).append(row)
    keys: dict[str, list[str]] = {}
    for (table, _), parts in grouped.items():
        columns = ", ".join(str(p[2]) for p in parts)
        if parts[0][3] is None:
            text = f"PRIMARY KEY ({columns})"
        else:
            targets = ", ".join(str(p[4]) for p in parts)
            text = f"FOREIGN KEY ({columns}) REFERENCES {parts[0][3]}({targets})"
        keys.setdefault(table, []).append(text)
    return keys


def _marks(names: Sequence[str]) -> str:
    return ", ".join(["%s"] * len(names))


def _value(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, int | float | bool) or value is None:
        return value
    text = str(value)
    return text if len(text) <= _MAX_VALUE else text[:_MAX_VALUE] + "…"


def _quote(dialect: str, name: str) -> str:
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    if dialect == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def _sample_statement(
    dialect: str, schema: str | None, table: str, columns: Sequence[str], count: int
) -> str:
    name = _quote(dialect, table)
    if schema:
        name = f"{_quote(dialect, schema)}.{name}"
    selected = ", ".join(_quote(dialect, column) for column in columns)
    if dialect == "mssql":
        return f"SELECT TOP {count} {selected} FROM {name}"
    return f"SELECT {selected} FROM {name} LIMIT {count}"


def _describe(
    dialect: str,
    schema: str | None,
    tables: list[tuple[str, str, int | None]],
    columns: dict[str, list[tuple[str, str, bool]]],
    keys: dict[str, list[str]],
    samples: Callable[[str, Sequence[str]], list[tuple[Any, ...]]] | None,
    note: str,
    run: Run | None = None,
) -> dict[str, Any]:
    described = []
    for name, kind, rows in tables[:_MAX_TABLES]:
        cols = columns.get(name, [])
        entry: dict[str, Any] = {
            "name": name,
            "type": kind,
            "approx_rows": rows,
            "columns": [
                f"{column} {ctype}" + ("" if nullable else " NOT NULL")
                for column, ctype, nullable in cols[:_MAX_COLUMNS]
            ],
            "keys": keys.get(name, []),
        }
        if len(cols) > _MAX_COLUMNS:
            entry["more_columns"] = len(cols) - _MAX_COLUMNS
        if samples is not None:
            shown = [
                column for column, _, _ in cols[:_MAX_COLUMNS] if not _SECRET_COLUMN.search(column)
            ]
            if shown:
                entry["sample"] = {
                    "columns": shown,
                    "rows": [[_value(v) for v in row] for row in samples(name, shown)],
                }
        if run is not None and (rows is None or rows <= _PROFILE_MAX_ROWS):
            profiled = _profile(dialect, schema, name, cols[:_MAX_COLUMNS], run)
            if profiled:
                entry["values"] = profiled
            ranges = _ranges(dialect, schema, name, [(c, t) for c, t, _ in cols], run)
            if ranges and ranges[1]:
                entry["date_ranges"] = ", ".join(ranges[1])
        described.append(entry)
    result: dict[str, Any] = {"dialect": dialect, "tables": described}
    if schema:
        result["schema"] = schema
    rest = [name for name, _, _ in tables[_MAX_TABLES : _MAX_TABLES + _MAX_NAMES]]
    if rest:
        result["more_tables"] = rest
        note = f"{note} Pass table= to see the other tables in detail.".strip()
    if len(tables) > _MAX_TABLES + _MAX_NAMES:
        result["omitted"] = len(tables) - _MAX_TABLES - _MAX_NAMES
    if note:
        result["note"] = note
    return result


def _qualified(dialect: str, schema: str | None, table: str) -> str:
    name = _quote(dialect, table)
    return f"{_quote(dialect, schema)}.{name}" if schema else name


def _profile(
    dialect: str, schema: str | None, table: str, columns: Sequence[tuple[str, str, bool]], run: Run
) -> dict[str, str]:
    """Distinct values with counts of the low-cardinality text columns of one table.

    Qualify: text columns that are no date, identifier or secret, with at most `PROFILE_MAX_VALUES`
    distinct values not all unique; the listing stops at `PROFILE_MAX_CHARS`.
    """

    listed: dict[str, str] = {}
    used = 0
    for column, ctype, _ in columns:
        if (
            not _TEXT_TYPE.search(ctype)
            or _SECRET_COLUMN.search(column)
            or _IDENTIFIER.search(column)
            or date_columns([(column, ctype)])
        ):
            continue
        try:
            counts = [
                (v, int(n)) for v, n in run(_profile_statement(dialect, schema, table, column), ())
            ]
        except Exception:  # too slow (the time limit) or unreadable: no listing
            continue
        if not counts or len(counts) > PROFILE_MAX_VALUES:
            continue
        if all(n == 1 for _, n in counts) and len(counts) > 1:
            continue
        text = ", ".join(
            f"{'NULL' if v is None else json.dumps(str(_value(v)), ensure_ascii=False)} {n}"
            for v, n in counts
        )
        if used + len(column) + len(text) > PROFILE_MAX_CHARS:
            break
        listed[column] = text
        used += len(column) + len(text)
    return listed


def _ranges(
    dialect: str, schema: str | None, table: str, columns: Sequence[tuple[str, str]], run: Run
) -> tuple[Any, list[str]] | None:
    """The row count and "column from MIN to MAX" of the date columns, or `None` on an error."""

    dates = date_columns(columns)[:_MAX_RANGE_COLUMNS]
    parts = ["COUNT(*)"] + [f"MIN({_quote(dialect, c)}), MAX({_quote(dialect, c)})" for c in dates]
    try:
        [row] = run(f"SELECT {', '.join(parts)} FROM {_qualified(dialect, schema, table)}", ())
    except Exception:  # an unreadable table only loses its ranges
        return None
    return row[0], [
        f"{column} from {row[1 + 2 * i]} to {row[2 + 2 * i]}"
        for i, column in enumerate(dates)
        if row[1 + 2 * i] is not None
    ]


def _profile_statement(dialect: str, schema: str | None, table: str, column: str) -> str:
    name = _qualified(dialect, schema, table)
    col = _quote(dialect, column)
    top, limit = (
        (f"TOP {PROFILE_MAX_VALUES + 1} ", "")
        if dialect == "mssql"
        else ("", f" LIMIT {PROFILE_MAX_VALUES + 1}")
    )
    return (
        f"SELECT {top}{col}, COUNT(*) FROM {name} GROUP BY {col} ORDER BY COUNT(*) DESC, {col}"
        f"{limit}"
    )


def query_tables(statement: str, known: Sequence[str]) -> list[str]:
    """The known tables a statement reads (names after FROM and JOIN, any case)."""

    lookup = {name.lower(): name for name in known}
    found: list[str] = []
    for match in _SOURCE.finditer(statement):
        last = re.split(r"\s*\.\s*", match.group(1))[-1].strip('"`[]')
        name = lookup.get(last.lower())
        if name is not None and name not in found:
            found.append(name)
    return found


def date_columns(columns: Sequence[tuple[str, str]]) -> list[str]:
    """Columns that hold dates or times: by declared type, or by name when not numeric."""

    def named(name: str) -> bool:
        return bool(_DATE_NAME.search(name) or _CAMEL_DATE_NAME.search(name))

    return [
        name
        for name, ctype in columns
        if _DATE_TYPE.search(ctype) or (named(name) and not _NUMBER_TYPE.search(ctype))
    ]


def coverage(
    dialect: str,
    schema: str | None,
    tables: Sequence[tuple[str, Sequence[tuple[str, str]]]],
    run: Run,
) -> list[str]:
    """Rows and MIN/MAX of the date columns of `tables`, as short sentences.

    Answers "which period does the data cover?" after a query matched nothing, so an empty
    result is never mistaken for "no data". A table that cannot be read is left out.
    """

    lines: list[str] = []
    for table, columns in tables:
        found = _ranges(dialect, schema, table, columns, run)
        if found is not None:
            count, ranges = found
            lines.append(
                f"{table} holds {count} rows" + (f"; {', '.join(ranges)}" if ranges else "") + "."
            )
    return lines


def _match(
    tables: list[tuple[str, str, int | None]], text: str | None
) -> tuple[list[tuple[str, str, int | None]], str]:
    if not text:
        return tables, ""
    found = [t for t in tables if text.lower() in t[0].lower()]
    if found:
        return found, ""
    names = ", ".join(t[0] for t in tables[:100])
    return [], f"No table or view contains {text!r}. Tables: {names or 'none'}."


def schema_sqlite(connection: sqlite3.Connection, args: SqlSchemaArgs) -> dict[str, Any]:
    """Describe a SQLite database (`connection` must be read-only)."""

    def count(table: str) -> int | None:
        try:
            return int(
                connection.execute(f"SELECT count(*) FROM {_quote('sqlite', table)}").fetchone()[0]
            )
        except sqlite3.DatabaseError:  # too slow (the time limit) or unreadable
            return None

    listed = connection.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND substr(name, 1, 7) != 'sqlite_' ORDER BY name"
    ).fetchall()
    chosen, note = _match([(n, _kind(k), None) for n, k in listed], args.table)
    chosen = [
        (n, k, count(n) if k == "table" and i < _MAX_TABLES else None)
        for i, (n, k, _) in enumerate(chosen)
    ]
    columns: dict[str, list[tuple[str, str, bool]]] = {}
    keys: dict[str, list[str]] = {}
    for name, _, _ in chosen[:_MAX_TABLES]:
        info = connection.execute(
            'SELECT name, type, "notnull", pk FROM pragma_table_info(?)', (name,)
        ).fetchall()
        columns[name] = [(c, t or "", not (nn or pk)) for c, t, nn, pk in info]
        primary = [c for c, _, _, pk in sorted(info, key=lambda r: r[3]) if pk]
        keys[name] = [f"PRIMARY KEY ({', '.join(primary)})"] if primary else []
        links: dict[int, list[tuple[str, str, str | None]]] = {}
        for ident, target, source, to in connection.execute(
            'SELECT id, "table", "from", "to" FROM pragma_foreign_key_list(?) ORDER BY id, seq',
            (name,),
        ):
            links.setdefault(ident, []).append((target, source, to))
        for parts in links.values():
            targets = ", ".join(p[2] or "" for p in parts)
            keys[name].append(
                f"FOREIGN KEY ({', '.join(p[1] for p in parts)}) REFERENCES {parts[0][0]}({targets})"
            )

    def samples(table: str, shown: Sequence[str]) -> list[tuple[Any, ...]]:
        statement = _sample_statement("sqlite", None, table, shown, args.sample_rows)
        return connection.execute(statement).fetchall()

    def run(statement: str, parameters: Sequence[Any]) -> list[tuple[Any, ...]]:
        return connection.execute(statement, tuple(parameters)).fetchall()

    sample = samples if args.sample_rows else None
    return _describe("sqlite", None, chosen, columns, keys, sample, note, run)


def schema_dbapi(run: Run, dialect: str, args: SqlSchemaArgs) -> dict[str, Any]:
    """Describe a PostgreSQL, MySQL/MariaDB or SQL Server database through `run`."""

    if dialect not in _TABLES:
        raise ToolException(f"sql_schema does not know the dialect {dialect!r}")
    schema = args.schema_name or str(run(DEFAULT_SCHEMA[dialect], ())[0][0])
    statement, count = _TABLES[dialect]
    listed = [(str(n), _kind(k), _approx(r)) for n, k, r in run(statement, (schema,) * count)]
    chosen, note = _match(listed, args.table)
    if not listed and not note:
        note = f"No tables or views in schema {schema!r}; pass schema_name= to look elsewhere."
    names = [name for name, _, _ in chosen[:_MAX_TABLES]]
    columns: dict[str, list[tuple[str, str, bool]]] = {}
    keys: dict[str, list[str]] = {}
    if names:
        marked = (schema, *names)
        for table, column, ctype, nullable, length in run(
            _COLUMNS.format(marks=_marks(names)), marked
        ):
            size = f"({length})" if length and int(length) > 0 else ""
            columns.setdefault(table, []).append(
                (column, f"{ctype}{size}", str(nullable).upper() != "NO")
            )
        if dialect == "postgresql":
            for table, text in run(_KEYS_POSTGRESQL.format(marks=_marks(names)), marked):
                keys.setdefault(table, []).append(text)
            for texts in keys.values():
                texts.sort(key=lambda t: not t.startswith("PRIMARY"))
        else:
            statements = (_KEYS_MYSQL,) if dialect == "mysql" else _KEYS_MSSQL
            rows = [r for s in statements for r in run(s.format(marks=_marks(names)), marked)]
            keys = _constraints(rows)
            for texts in keys.values():
                texts.sort(key=lambda t: not t.startswith("PRIMARY"))

    def samples(table: str, shown: Sequence[str]) -> list[tuple[Any, ...]]:
        return run(_sample_statement(dialect, schema, table, shown, args.sample_rows), ())

    return _describe(
        dialect, schema, chosen, columns, keys, samples if args.sample_rows else None, note, run
    )
