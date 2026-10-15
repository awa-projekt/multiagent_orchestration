"""Editing Excel workbooks (.xlsx) downloaded from SharePoint or OneDrive, with `openpyxl`.

Workbooks with parts openpyxl would lose are refused; values are never written as formulas.
"""

from __future__ import annotations

import math
import re
import zipfile
from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awa._internal.install import require_module
from awa._internal.sandbox import check_zip_bounds
from awa._internal.upstream.tools import ToolException

MAX_UNPACKED_BYTES = 100_000_000
"""Largest workbook (unpacked) awa edits: the whole file is held in memory while it is edited."""
MAX_CELLS = 500
"""Most cells `update_cells` changes in one call."""
MAX_ROWS = 1000
"""Most rows `append_rows` adds in one call."""

_MAX_TEXT = 32_767  # the longest text an Excel cell holds
_MAX_COLUMNS = 16_384
_MAX_SHEET_ROWS = 1_048_576
_CELL = re.compile(r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}$")

# Parts of a workbook (zip entry names) that openpyxl does not keep, and what to call them.
_LOST_PARTS = (
    (re.compile(r"^xl/vbaProject\.bin$"), "macros"),
    (re.compile(r"^xl/(pivotTables|pivotCache)/"), "pivot tables"),
    (re.compile(r"^xl/charts/"), "charts"),
    (re.compile(r"^xl/drawings/drawing\d*\.xml$"), "pictures, shapes or charts"),
    (re.compile(r"^xl/richData/"), "pictures in cells or linked data types"),
    (re.compile(r"^xl/(slicers|slicerCaches|timelines|timelineCaches)/"), "slicers or timelines"),
    (re.compile(r"^xl/(embeddings|activeX|ctrlProps)/"), "embedded objects or form controls"),
    (re.compile(r"^xl/(threadedComments|persons)/"), "threaded comments"),
    (re.compile(r"^xl/(queryTables/|connections\.xml$)"), "data connections or queries"),
)
# Newer-Excel features stored inside a sheet that openpyxl drops.
_LOST_IN_SHEETS = (
    (b"x14:sparklineGroup", "sparklines"),
    (b"x14:conditionalFormatting", "conditional formatting of newer Excel"),
    (b"x14:dataValidation", "data validation of newer Excel"),
)


@dataclass(frozen=True)
class Inspection:
    """What `inspect_for_edit` found in a workbook that is safe to edit.

    Attributes:
        formulas: Whether any sheet holds formulas (their saved results are lost on an edit;
            Excel recalculates them when the file is opened).
    """

    formulas: bool


def inspect_for_edit(path: Path) -> Inspection:
    """Check that editing `path` with openpyxl would not damage it, and that it is not a bomb."""

    check_zip_bounds(path, max_bytes=MAX_UNPACKED_BYTES)
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ToolException(
            "the file is not a normal .xlsx workbook (it may be password protected, or an "
            "old .xls file): open it in Excel and save it as .xlsx without a password"
        ) from None
    lost: set[str] = set()
    formulas = False
    with archive:
        names = archive.namelist()
        if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
            raise ToolException("the file is a zip archive but not an Excel workbook")
        if b"macroEnabled" in archive.read("[Content_Types].xml"):
            lost.add("macros")
        for name in names:
            lost.update(reason for pattern, reason in _LOST_PARTS if pattern.search(name))
            if not name.endswith((".xml", ".rels")):
                continue
            data = archive.read(name)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:  # the parts are bounded above
                raise ToolException(f"{name} declares an XML document type; the file is refused")
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                lost.update(reason for marker, reason in _LOST_IN_SHEETS if marker in data)
                formulas = formulas or b"<f>" in data or b"<f " in data
    if lost:
        raise ToolException(
            "this workbook contains " + ", ".join(sorted(lost)) + ". Editing it with awa would "
            "silently remove them, so awa does not change it. Edit it in Excel yourself, or "
            "work on a copy that does not have them. (Reading it is fine.)"
        )
    return Inspection(formulas)


def append_rows(
    source: Path, target: Path, sheet: str, rows: Sequence[Sequence[Any]]
) -> dict[str, Any]:
    """Write `rows` below the last used row of `sheet` (from column A) and save to `target`.

    A table that ends at the last used row grows to include the new rows; the new cells take the
    number format and style of the row above. Returns `sheet`, `first_row`, `last_row` and
    `rows_added`.
    """

    if not 1 <= len(rows) <= MAX_ROWS:
        raise ToolException(f"give between 1 and {MAX_ROWS} rows")
    values = [[_value(cell) for cell in row] for row in rows]
    if any(len(row) > _MAX_COLUMNS for row in values):
        raise ToolException(f"a row has more than {_MAX_COLUMNS} values")
    workbook = _open(source)
    worksheet = _writable(workbook, sheet)
    last = _last_used_row(worksheet)
    first = last + 1
    if first + len(values) - 1 > _MAX_SHEET_ROWS:
        raise ToolException("the sheet has no room for that many more rows")
    for number, row in enumerate(values, start=first):
        for column, value in enumerate(row, start=1):
            cell = worksheet.cell(row=number, column=column)
            _store(cell, value)
            if last > 1:
                cell._style = copy(worksheet.cell(row=last, column=column)._style)
    _grow_tables(worksheet, last, first + len(values) - 1)
    workbook.save(target)
    return {
        "sheet": sheet,
        "first_row": first,
        "last_row": first + len(values) - 1,
        "rows_added": len(values),
    }


def update_cells(
    source: Path, target: Path, sheet: str, cells: Mapping[str, Any]
) -> dict[str, Any]:
    """Set single cells (`{"B2": 5, "C3": "text"}`) and save to `target`.

    Returns `sheet`, `cells_updated` and `changed` (cell, previous value, new value). Merged cells
    and formula cells are refused.
    """

    if not 1 <= len(cells) <= MAX_CELLS:
        raise ToolException(f"give between 1 and {MAX_CELLS} cells")
    openpyxl = require_module("openpyxl", "openpyxl", feature="Excel editing")
    workbook = _open(source)
    worksheet = _writable(workbook, sheet)
    changed = []
    for name, wanted in cells.items():
        if not _CELL.fullmatch(name):
            raise ToolException(
                f"{name!r} is not a single cell such as B2 (ranges are not allowed)"
            )
        column, row = openpyxl.utils.cell.coordinate_to_tuple(name.upper())[::-1]
        if column > _MAX_COLUMNS or row > _MAX_SHEET_ROWS:
            raise ToolException(f"{name!r} is outside the sheet")
        cell = worksheet[name.upper()]
        if isinstance(cell, openpyxl.cell.cell.MergedCell):
            raise ToolException(f"{name} is part of a merged range; write to its top left cell")
        if cell.data_type == "f":
            raise ToolException(
                f"{name} holds a formula; awa does not overwrite formulas (change it in Excel)"
            )
        before = cell.value
        _store(cell, _value(wanted))
        changed.append({"cell": name.upper(), "was": _short(before), "now": _short(cell.value)})
    workbook.save(target)
    return {"sheet": sheet, "cells_updated": len(changed), "changed": changed}


def _open(path: Path) -> Any:
    openpyxl = require_module("openpyxl", "openpyxl", feature="Excel editing")
    try:
        return openpyxl.load_workbook(path)
    except Exception as exc:  # openpyxl raises many kinds for a broken file
        raise ToolException(f"the workbook could not be read: {type(exc).__name__}") from None


def _writable(workbook: Any, sheet: str) -> Any:
    if sheet not in workbook.sheetnames:
        raise ToolException(f"no sheet {sheet!r} (sheets: {', '.join(workbook.sheetnames)})")
    worksheet = workbook[sheet]
    if worksheet.protection.sheet:
        raise ToolException(f"the sheet {sheet!r} is protected; awa does not write into it")
    return worksheet


def _last_used_row(worksheet: Any) -> int:
    # The stored cells only: indexing rows would create empty cells in the saved sheet.
    return max(
        (row for (row, _), cell in worksheet._cells.items() if cell.value is not None), default=0
    )


def _grow_tables(worksheet: Any, last: int, new_last: int) -> None:
    utils = require_module("openpyxl.utils", "openpyxl", feature="Excel editing")
    get_column_letter, range_boundaries = utils.get_column_letter, utils.range_boundaries
    for table in worksheet.tables.values():
        first_column, first_row, last_column, last_row = range_boundaries(table.ref)
        if last_row != last:
            continue
        if table.totalsRowCount:
            raise ToolException(
                f"the table {table.name!r} ends with a totals row; add rows in Excel instead"
            )
        table.ref = (
            f"{get_column_letter(first_column)}{first_row}:"
            f"{get_column_letter(last_column)}{new_last}"
        )
        if table.autoFilter is not None:
            table.autoFilter.ref = table.ref


def _value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolException("a number must be finite (not NaN or infinity)")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            raise ToolException(f"a text is longer than the {_MAX_TEXT} characters a cell holds")
        return value
    raise ToolException(
        f"a value must be text, a number, true/false or null, not {type(value).__name__}"
    )


def _store(cell: Any, value: str | int | float | bool | None) -> None:
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"  # text stays text: "=1+1" is never turned into a formula


def _short(value: Any) -> Any:
    text = value if isinstance(value, str | int | float | bool) or value is None else str(value)
    return text[:100] if isinstance(text, str) else text
