"""SQLite files (`sqlite`) opened read only (`mode=ro`, `query_only`, a read-only authorizer and
a time limit): tables, columns, keys, row counts and samples; views are listed, not read."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from awa.data._injection import injection_flags
from awa.data._json import ISO_DATE, ISO_DATETIME, MAX_ENUM_VALUES
from awa.data._model import (
    ColumnProfile,
    ColumnType,
    FileProfile,
    ForeignKey,
    SqliteInfo,
    TableProfile,
    TopValue,
    column_key,
)
from awa.data._parse import ParseContext, ParseTimeout
from awa.data._pii import classify_pii
from awa.data._sampling import spread_indices

KINDS = ("sqlite",)

_SCANNED_STRINGS = 20_000
_MIN_SCANNED_CHARS = 12
_PROGRESS_STEPS = 1000
_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)
_ALLOWED_PRAGMAS = frozenset(
    {"table_xinfo", "foreign_key_list", "index_list", "index_info", "table_info"}
)


def profile_sqlite(path: Path, ctx: ParseContext) -> FileProfile:
    """Profile a SQLite file: tables, columns, keys, indexes, views, row counts, samples."""

    connection = None
    try:
        connection = _open(path, ctx)
        return _profile(connection, ctx)
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error):
            limit = ctx.limits.parse_timeout_s
            raise ParseTimeout(f"parsing took longer than {limit:g} s") from None
        return ctx.fail("DF-E-PARSE-FAILED", "the SQLite file cannot be opened or read")
    except (sqlite3.DatabaseError, UnicodeDecodeError):
        return ctx.fail("DF-E-PARSE-FAILED", "the SQLite file is damaged or encrypted")
    finally:
        if connection is not None:
            connection.close()


def _profile(connection: sqlite3.Connection, ctx: ParseContext) -> FileProfile:
    catalog = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE substr(name, 1, 7) != 'sqlite_' "
        "ORDER BY type, name"
    ).fetchall()
    info = ctx.profile.sqlite = SqliteInfo(views=[n for kind, n, _ in catalog if kind == "view"])
    triggers = [name for kind, name, _ in catalog if kind == "trigger"]
    if triggers:
        ctx.flag("DF-I-SQLITE-TRIGGERS", f"{len(triggers)} triggers exist; none can fire here")
    tables = [(name, sql or "") for kind, name, sql in catalog if kind == "table"]
    if len(tables) > ctx.limits.max_sheets:
        ctx.profile.status = "partial"
        ctx.flag(
            "DF-W-SAMPLED",
            f"only the first {ctx.limits.max_sheets} of {len(tables)} tables are read",
        )
    for number, (name, sql) in enumerate(tables[: ctx.limits.max_sheets], 1):
        ctx.check_deadline()
        ctx.profile.tables.append(_table(connection, ctx, number, name, sql))
    keys = {t.name or "": {c.name: c.key for c in t.columns} for t in ctx.profile.tables}
    primary = {t.name or "": t.primary_key for t in ctx.profile.tables}
    for table in ctx.profile.tables:
        _keys_and_indexes(connection, table, info, keys, primary)
    names = " ".join([*(t.name or "" for t in ctx.profile.tables), *info.views])
    ctx.profile.flags.extend(injection_flags(names, ctx.file_id))
    return ctx.profile


def _table(
    connection: sqlite3.Connection, ctx: ParseContext, number: int, name: str, sql: str
) -> TableProfile:
    table = TableProfile(id=ctx.table_id(number), name=name)
    if sql.upper().startswith("CREATE VIRTUAL TABLE"):
        ctx.flag("DF-W-SQLITE-VIRTUAL", f"{name} is a virtual table; it is not read", table.id)
        table.rows_exact = False
        return table
    infos = [i for i in connection.execute(f"PRAGMA table_xinfo({_quote(name)})") if i[6] != 1]
    table.columns = _columns(infos)
    keys = {c.name: c.key for c in table.columns}
    ordered = sorted((i for i in infos if i[5] > 0), key=lambda i: i[5])
    table.primary_key = [keys[i[1]] for i in ordered]
    table.rows = connection.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
    rows = connection.execute(
        f"SELECT * FROM {_quote(name)} LIMIT ?", (ctx.limits.sample_rows,)
    ).fetchall()
    table.rows_seen = len(rows)
    table.sampled = table.rows > len(rows)
    if table.sampled:
        ctx.profile.status = "partial"
        ctx.flag(
            "DF-W-SAMPLED",
            f"column facts come from the first {len(rows)} of {table.rows} rows",
            table.id,
        )
    limit = ctx.limits.max_chars_per_cell
    scanned: set[str] = set()
    for column in table.columns:
        values = [row[column.index] for row in rows]
        _describe(column, values, table.rows, limit, sampled=table.sampled)
        column.pii = classify_pii(values, column.name)
        _scan_texts(ctx, values, f"{table.id}.{column.key}", scanned)
    for index in spread_indices(len(rows), ctx.privacy.max_sample_rows):
        table.sample_rows.append([_cell(value, limit) for value in rows[index]])
    return table


def _columns(infos: list[Any]) -> list[ColumnProfile]:
    columns: list[ColumnProfile] = []
    used: set[str] = set()
    for index, (_, name, declared, notnull, *_rest) in enumerate(infos):
        key = column_key(name) or f"column_{index + 1}"
        while key in used:
            key += "_2"
        used.add(key)
        columns.append(
            ColumnProfile(
                name=name,
                key=key,
                index=index,
                declared_type=declared or None,
                nullable=not notnull,
            )
        )
    return columns


def _scan_texts(ctx: ParseContext, values: list[Any], where: str, scanned: set[str]) -> None:
    for text in sorted({v for v in values if isinstance(v, str)} - scanned):
        if len(text) >= _MIN_SCANNED_CHARS and len(scanned) < _SCANNED_STRINGS:
            scanned.add(text)
            new = [f for f in injection_flags(text, where) if f not in ctx.profile.flags]
            ctx.profile.flags.extend(new)


def _cell(value: Any, limit: int) -> str | int | float | None:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value[:limit] if isinstance(value, str) else value


def _describe(
    column: ColumnProfile, values: list[Any], rows: int, limit: int, *, sampled: bool
) -> None:
    """Type, nulls, distinct count, range and examples of one column from its sampled values."""

    filled = [v for v in values if v is not None and v != ""]
    column.null_share = round(1 - len(filled) / len(values), 4) if values else 0.0
    column.nullable = column.nullable or len(filled) < len(values)
    distinct = len(set(filled))
    column.distinct = None if sampled else distinct  # a count over a sample is no count
    column.unique = not sampled and bool(filled) and distinct == len(filled) == rows
    texts = [v for v in filled if isinstance(v, str)]
    numbers = [v for v in filled if isinstance(v, int | float) and not isinstance(v, bool)]
    if not filled:
        column.type = "empty"
    elif len(numbers) == len(filled):
        column.type = "integer" if all(isinstance(v, int) for v in numbers) else "decimal"
        column.min, column.max = min(numbers), max(numbers)
    elif len(texts) == len(filled):
        column.type = "text"
        dated: tuple[tuple[ColumnType, re.Pattern[str]], ...] = (
            ("date", ISO_DATE),
            ("datetime", ISO_DATETIME),
        )
        for kind, rule in dated:
            if all(rule.fullmatch(v) for v in texts):
                column.type = kind
                column.min, column.max = min(texts), max(texts)
                break
    else:
        column.type = "mixed"
    if column.type == "integer" and re.search("BOOL", column.declared_type or "", re.IGNORECASE):
        column.type = "bool"
    enum_like = distinct <= MAX_ENUM_VALUES and len(filled) >= 3 * distinct
    if enum_like and column.type in ("text", "integer"):
        ranked = sorted(Counter(filled).items(), key=lambda item: (-item[1], str(item[0])))
        column.top = [TopValue(value=str(v)[:limit], count=c) for v, c in ranked[:5]]
        column.type = "enum" if column.type == "text" else column.type
    column.examples = [str(v)[:limit] for v in dict.fromkeys(filled)][:3]


def _keys_and_indexes(
    connection: sqlite3.Connection,
    table: TableProfile,
    info: SqliteInfo,
    keys: dict[str, dict[str, str]],
    primary: dict[str, list[str]],
) -> None:
    """Foreign keys, unique indexes (key candidates) and named indexes of one table."""

    name = table.name or ""
    mine = keys[name]
    rows = sorted(
        connection.execute(f"PRAGMA foreign_key_list({_quote(name)})"), key=lambda r: (r[0], r[1])
    )
    for fk_id in dict.fromkeys(row[0] for row in rows):
        pairs = [(row[3], row[4]) for row in rows if row[0] == fk_id]
        target = next(row[2] for row in rows if row[0] == fk_id)
        theirs = keys.get(target, {})
        referenced = [theirs.get(b, column_key(b)) for _, b in pairs if b]
        if not referenced:  # `REFERENCES parent` without columns means the parent's primary key
            referenced = primary.get(target, [])
        info.foreign_keys.append(
            ForeignKey(
                table=name,
                columns=[mine.get(a, column_key(a)) for a, _ in pairs],
                ref_table=target,
                ref_columns=referenced,
            )
        )
    for _, index, unique, origin, _partial in connection.execute(
        f"PRAGMA index_list({_quote(name)})"
    ):
        columns = [
            mine[row[2]]
            for row in connection.execute(f"PRAGMA index_info({_quote(index)})")
            if row[2] in mine
        ]
        if unique and columns and columns not in table.key_candidates:
            table.key_candidates.append(columns)
        if origin == "c":
            info.indexes.append(f"{name}.{index}")
    # An INTEGER PRIMARY KEY is the rowid and has no index of its own.
    if table.primary_key and table.primary_key not in table.key_candidates:
        table.key_candidates.insert(0, list(table.primary_key))


def _authorize(action: int, first: str | None, second: str | None, *_: Any) -> int:
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if first in _ALLOWED_PRAGMAS else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and second == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _open(path: Path, ctx: ParseContext) -> sqlite3.Connection:
    """The file as a read-only connection that cannot write, attach or load code."""

    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
    connection.execute("PRAGMA query_only = ON")
    if hasattr(connection, "enable_load_extension"):
        connection.enable_load_extension(False)
    connection.set_authorizer(_authorize)
    connection.set_progress_handler(lambda: _expired(ctx), _PROGRESS_STEPS)
    return connection


def _expired(ctx: ParseContext) -> int:
    """1 (stop the statement) once the run's time limit is used up, else 0."""

    try:
        ctx.check_deadline()
    except ParseTimeout:
        return 1
    return 0


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
