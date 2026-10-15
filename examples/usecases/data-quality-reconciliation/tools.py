"""Tools for the data-quality-reconciliation use case.

`reconcile` compares an exported Excel or CSV file with a database table row by row, which no
model could do reliably for thousands of rows; `save_report` keeps the written report in `out/`.
The agent reads single rows and changes data through the `warehouse` connector declared in
`blueprint.yaml` (SQL writes there always wait for approval). Replace `_database_rows` to read
another database; needs openpyxl for Excel (`pip install -e ".[files]"` in the awa folder).
"""

from __future__ import annotations

import csv
import importlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from typing_extensions import TypedDict

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _out_dir() -> Path:
    """Where the process writes: the eval case's copy inside `awa eval`, else out/ here."""

    return case_dir(HERE) / "out"


def _database_file() -> Path:
    return _data_dir() / "warehouse.db"


MAX_LISTED = 40
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ColumnPair(TypedDict):
    """A column of the export file and the database column it is compared with."""

    file_column: str
    database_column: str


def _inside_data(name: str) -> Path:
    path = (_data_dir() / name).resolve() if not Path(name).is_absolute() else Path(name).resolve()
    if not path.is_relative_to(_data_dir().resolve()) or not path.is_file():
        raise ValueError(f"{name!r} is not a file inside the data folder")
    return path


def _export_rows(path: Path, key_header: str) -> list[dict[str, Any]]:
    """The rows of an Excel sheet or CSV file; the header is the first row with `key_header`."""

    if path.suffix.lower() in (".xlsx", ".xlsm"):
        openpyxl = importlib.import_module("openpyxl")

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            table = [list(row) for row in book.worksheets[0].iter_rows(values_only=True)]
        finally:
            book.close()  # read-only workbooks keep the file open until closed
    else:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        delimiter = ";" if lines and ";" in lines[0] else ","
        table = list(csv.reader(lines, delimiter=delimiter))
    start = next(
        (i for i, row in enumerate(table) if key_header in [str(c).strip() for c in row if c]), None
    )
    if start is None:
        raise ValueError(f"no row with the column {key_header!r} in {path.name}")
    names = [str(c).strip() if c is not None else "" for c in table[start]]
    return [dict(zip(names, row, strict=False)) for row in table[start + 1 :] if any(row)]


def _database_rows(table: str, key: str, columns: list[str]) -> dict[str, dict[str, Any]]:
    for name in [table, key, *columns]:
        if not _IDENTIFIER.match(name):
            raise ValueError(f"{name!r} is not a plain table or column name")
    names = ", ".join([key, *columns])
    with closing(
        sqlite3.connect(f"file:{_database_file().as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        found = connection.execute(f"SELECT {names} FROM {table}").fetchall()  # noqa: S608
    return {str(row[key]).strip(): dict(row) for row in found}


def _same(left: Any, right: Any) -> bool:
    """Equal numbers within half a cent, else equal text ignoring case and padding."""

    try:
        return abs(float(left) - float(right)) < 0.005
    except (TypeError, ValueError):
        return str(left).strip().casefold() == str(right).strip().casefold()


def reconcile(
    export_file: str, key_header: str, table: str, key_column: str, columns: list[ColumnPair]
) -> str:
    """Compare an export file with a database table and list every discrepancy.

    `export_file` is a path inside the data folder (a dropped file's `path` works). `key_header`
    is the key's column name in the file, `table` and `key_column` name the database side, and
    `columns` pairs file column names with the database columns to compare (for example
    `{"file_column": "Amount", "database_column": "amount"}`). Keys are compared without
    padding; text without case; numbers to half a cent. Returns counts and the discrepancies:
    `missing_in_database`, `missing_in_export`, `duplicate_keys` (in the export), and
    `mismatches` (key, column, export value, database value, and the row's `note` column when
    the export has one).
    The export is the source of truth for amounts; status conflicts need a business decision.
    """

    try:
        exported = _export_rows(_inside_data(export_file), key_header)
        database = _database_rows(table, key_column, [pair["database_column"] for pair in columns])
    except (ValueError, sqlite3.Error, KeyError) as exc:
        return f"Not reconciled: {exc}"
    seen: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in exported:
        key = str(row.get(key_header, "")).strip()
        if key in seen:
            duplicates.append(key)
        seen.setdefault(key, row)
    mismatches = [
        {
            "key": key,
            "column": database_column,
            "export_value": row.get(header),
            "database_value": database[key][database_column],
            **({"export_note": str(row["Note"])} if row.get("Note") else {}),
        }
        for key, row in seen.items()
        if key in database
        for header, database_column in ((p["file_column"], p["database_column"]) for p in columns)
        if not _same(row.get(header), database[key][database_column])
    ]
    missing_in_database = sorted(set(seen) - set(database))
    missing_in_export = sorted(set(database) - set(seen))
    return json.dumps(
        {
            "export_rows": len(exported),
            "database_rows": len(database),
            "matching_keys": len(set(seen) & set(database)),
            "missing_in_database": missing_in_database[:MAX_LISTED],
            "missing_in_export": missing_in_export[:MAX_LISTED],
            "duplicate_keys": sorted(set(duplicates)),
            "mismatches": mismatches[:MAX_LISTED],
            "mismatch_count": len(mismatches),
        }
    )


def save_report(name: str, markdown: str) -> str:
    """Save the reconciliation report as a Markdown file in `out/` and return its path.

    `name` is a short file name without folders (for example the export's name). In
    production: publish the report where the data owners read it (wiki, SharePoint, email).
    """

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-") or "report"
    _out_dir().mkdir(exist_ok=True)
    path = _out_dir() / f"reconciliation-{stem}-{date.today():%Y%m%d}.md"
    path.write_text(markdown, encoding="utf-8", newline="\n")
    return f"Saved the report as {path.relative_to(case_dir(HERE)).as_posix()}."


__all__ = ["reconcile", "save_report"]
