"""`local_values`: reread column values the way the profile described the files."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from awa.data._errors import RowLimitError
from awa.data._model import DataProfile, FileProfile, TableProfile
from awa.data.ports import ColumnValues, DataReader, ValueSource, local_reader


def local_values(
    profile: DataProfile,
    *,
    max_rows: int | None = None,
    max_distinct: int = 50_000,
    reader: DataReader | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> ValueSource:
    """A `ValueSource` that rereads the profiled files, bounded per table and column.

    `FileProfile.source` is trusted as a path: use it only for profiles made here or by someone you
    trust. A missing or changed file is left out of the answer; an unreadable one too, and it is
    passed to `on_error(table_id, reason)`. It never raises for a bad file.

    Args:
        max_rows: Rows read per table; default `profile.limits.max_rows_exact`.
        reader: Where the rows come from (default: `local_reader()`).
    """

    rows = max_rows or profile.limits.max_rows_exact
    source = reader or local_reader()
    tables = {t.id: (f, t) for f, t in profile.tables()}

    def read(ids: Sequence[str]) -> Mapping[str, ColumnValues]:
        by_table: dict[str, list[str]] = {}
        for column_id in ids:
            by_table.setdefault(column_id.rpartition(".")[0], []).append(column_id)
        found: dict[str, ColumnValues] = {}
        for table_id in sorted(by_table):
            if table_id not in tables:
                continue
            file, table = tables[table_id]
            try:
                found.update(
                    _read_table(source, file, table, by_table[table_id], rows, max_distinct)
                )
            except Exception as error:  # a damaged or hostile file only loses its own columns
                if on_error is not None:
                    on_error(table_id, str(error) or type(error).__name__)
        return found

    return read


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.strip() or None


def _read_table(
    reader: DataReader,
    file: FileProfile,
    table: TableProfile,
    ids: Sequence[str],
    max_rows: int,
    max_distinct: int,
) -> dict[str, ColumnValues]:
    positions = {f"{table.id}.{c.key}": at for at, c in enumerate(table.columns)}
    wanted = [i for i in ids if i in positions]
    path = Path(file.source) if file.source else None
    if not wanted or path is None or not path.is_file() or path.stat().st_size != file.size:
        return {}  # gone, or changed since it was profiled: its values would not match the profile
    slots = [positions[i] for i in wanted]
    values: list[set[str]] = [set() for _ in wanted]
    counts = [0] * len(wanted)
    complete = True
    rows = reader.rows(file, table, max_rows=max_rows)
    try:
        with contextlib.closing(rows):  # type: ignore[type-var]
            for row in rows:
                for at, slot in enumerate(slots):
                    text = _text(row[slot]) if slot < len(row) else None
                    if text is None:
                        continue
                    counts[at] += 1
                    if len(values[at]) < max_distinct or text in values[at]:
                        values[at].add(text)
                    else:
                        complete = False
    except RowLimitError:
        complete = False  # a long table is read up to the limit, and the answer says so
    return {
        column_id: ColumnValues(frozenset(values[at]), counts[at], complete)
        for at, column_id in enumerate(wanted)
    }
