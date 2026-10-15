"""Add columns to an Excel sheet and fill them per row, matched by a key; save as a new workbook.

Formatting, other sheets and formulas stay; source cells are never written. Keys match ignoring
case, spaces and punctuation (`INV-2026/0042` finds `inv 2026 0042`; `4711.0` matches `4711`).
Keys found in no row go to the result and an extra sheet, so nothing read is lost.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from awa._internal.install import require_module
from awa._internal.sandbox import check_zip_bounds
from awa._internal.upstream.tools import ToolException

UNMATCHED_SHEET = "Unmatched keys"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NOT_KEY = re.compile(r"[\W_]+", re.UNICODE)
_MAX_LISTED = 50
_FORMULA_STARTS = ("=", "+", "-", "@")


@dataclass(frozen=True)
class FillRow:
    """The values for the row with key `key`, in the order of the columns; `review` is a note
    for the review column (low confidence, missing value) or `None`."""

    key: str
    values: Sequence[str | int | float | bool | None]
    review: str | None = None


def match_key(value: Any) -> str:
    """The form keys are compared in: upper case, without spaces and punctuation; an integral
    number without its `.0`."""

    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _NOT_KEY.sub("", str(value if value is not None else "")).upper()


def _name(value: Any) -> str:
    """A column name compared without case, spaces and punctuation, in any script."""

    return _NOT_KEY.sub("", str(value or "").casefold())


def _header(sheet: Any, row: int) -> dict[str, int]:
    found: dict[str, int] = {}
    for cells in sheet.iter_rows(min_row=row, max_row=row):
        for cell in cells:
            if cell.value not in (None, ""):
                found.setdefault(_name(cell.value), cell.column)
    return found


def _pick_sheet(workbook: Any, sheet: str | None) -> Any:
    if sheet is None:
        return workbook.worksheets[0]
    if sheet not in workbook.sheetnames:
        raise ToolException(f"no sheet {sheet!r}; the sheets are {', '.join(workbook.sheetnames)}")
    return workbook[sheet]


def _write(cell: Any, value: str | int | float | bool | None) -> None:
    if isinstance(value, str) and _ISO_DATE.match(value.strip()):
        try:
            cell.value = date.fromisoformat(value.strip())
            cell.number_format = "yyyy-mm-dd"
            return
        except ValueError:
            pass
    _set(cell, value)


def _set(cell: Any, value: Any) -> None:
    """Store `value`; text that starts like a formula stays text."""

    cell.value = value
    if isinstance(value, str) and value.startswith(_FORMULA_STARTS):
        cell.data_type = "s"


def fill_workbook(
    source: Path,
    output: Path,
    *,
    key_column: str,
    columns: Sequence[str],
    rows: Sequence[FillRow],
    sheet: str | None = None,
    header_row: int = 1,
    review_column: str | None = "Review",
    append: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write `rows` into new `columns` of `source` and save the workbook as `output`.

    With `append`, an `output` written before is continued (more rows into the same new
    columns); with `overwrite` it starts again from `source`; with neither an existing
    `output` is refused.

    Returns:
        A summary with the counts and the keys found in no row or in several rows.
    """

    openpyxl = require_module("openpyxl", "openpyxl", feature="Excel editing")
    if output.resolve() == source.resolve():
        raise ToolException("output must be a new file; the source workbook is never changed")
    if output.suffix.lower() != ".xlsx":
        raise ToolException("output must be an .xlsx file")
    if output.exists() and not (append or overwrite):
        raise ToolException(
            f"{output.name} exists; set append to continue it or overwrite to replace it"
        )
    wanted = [str(c).strip() for c in columns]
    if not wanted or any(not c for c in wanted) or len({_name(c) for c in wanted}) != len(wanted):
        raise ToolException("columns must be distinct, non-empty names")
    for number, row in enumerate(rows, start=1):
        if len(row.values) != len(wanted):
            raise ToolException(
                f"row {number} ({row.key!r}) has {len(row.values)} values, expected {len(wanted)}"
            )
    continuing = append and output.is_file()
    check_zip_bounds(source)
    if continuing:
        check_zip_bounds(output)
    try:
        workbook = openpyxl.load_workbook(output if continuing else source)
    except Exception as exc:
        raise ToolException(f"cannot open the workbook: {type(exc).__name__}: {exc}") from None
    target = _pick_sheet(workbook, sheet)
    present = _header(target, header_row)
    original = present
    if continuing:
        source_book = openpyxl.load_workbook(source, read_only=True)
        try:
            original = _header(_pick_sheet(source_book, sheet), header_row)
        finally:
            source_book.close()
    key_index = present.get(_name(key_column))
    if key_index is None:
        raise ToolException(f"no column {key_column!r} in row {header_row} of {target.title!r}")
    if review_column and _name(review_column) in {_name(c) for c in wanted}:
        review_column = None  # the caller fills the review column as one of its columns
    names = [*wanted, *([review_column] if review_column else [])]
    clash = [c for c in names if _name(c) in original]
    if clash:
        raise ToolException(
            f"column(s) {', '.join(clash)} exist in the source; source cells are never "
            "overwritten, choose new names"
        )
    added, positions = _add_columns(target, header_row, present, names)
    keys: dict[str, list[int]] = {}
    for (cell,) in target.iter_rows(min_row=header_row + 1, min_col=key_index, max_col=key_index):
        if cell.value not in (None, ""):
            keys.setdefault(match_key(cell.value), []).append(cell.row)

    unmatched: list[FillRow] = []
    duplicates: list[str] = []
    matched = written = 0
    for row in rows:
        found = keys.get(match_key(row.key), []) if match_key(row.key) else []
        if not found:
            unmatched.append(row)
            continue
        matched += 1
        notes = [row.review] if row.review else []
        if len(found) > 1:
            duplicates.append(row.key)
            notes.append(f"key {row.key} is in {len(found)} rows")
        for number in found:
            for name, value in zip(wanted, row.values, strict=True):
                if value is not None and value != "":
                    _write(target.cell(row=number, column=positions[name]), value)
                    written += 1
            if review_column and notes:
                _set(target.cell(row=number, column=positions[review_column]), "; ".join(notes))
    if unmatched:
        _list_unmatched(workbook, unmatched, [key_column, *wanted], continuing=continuing)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _save_reproducible(workbook, output)
    except PermissionError:
        raise ToolException(f"cannot write {output.name}; is it open in Excel?") from None
    return {
        "sheet": target.title,
        "columns_added": added,
        "rows_given": len(rows),
        "rows_matched": matched,
        "cells_written": written,
        "unmatched_keys": [r.key for r in unmatched][:_MAX_LISTED],
        "unmatched_sheet": UNMATCHED_SHEET if unmatched else None,
        "keys_in_several_rows": duplicates[:_MAX_LISTED],
    }


def _add_columns(
    target: Any, header_row: int, present: dict[str, int], names: Sequence[str]
) -> tuple[list[str], dict[str, int]]:
    """Add the `names` missing from the header after its last column, styled like it.

    Returns the names added and the column number of every name.
    """

    added: list[str] = []
    positions: dict[str, int] = {}
    last = max(present.values(), default=0)
    model = target.cell(row=header_row, column=last) if last else None
    for name in names:
        position = present.get(_name(name))
        if position is None:
            last += 1
            position = last
            head = target.cell(row=header_row, column=position)
            _set(head, name)
            if model is not None and model.has_style:
                head.font, head.fill = copy(model.font), copy(model.fill)
                head.border, head.alignment = copy(model.border), copy(model.alignment)
            target.column_dimensions[head.column_letter].width = max(12, min(40, len(name) + 4))
            added.append(name)
        positions[name] = position
    return added, positions


def _list_unmatched(
    workbook: Any, unmatched: Sequence[FillRow], header: list[str], *, continuing: bool
) -> None:
    """Put the rows whose key is in no row on the unmatched sheet (continued when appending)."""

    title = UNMATCHED_SHEET
    if title in workbook.sheetnames and not continuing:
        title = f"{UNMATCHED_SHEET} 2"
    fresh = title not in workbook.sheetnames
    extra = workbook.create_sheet(title) if fresh else workbook[title]
    lines = [[row.key, *row.values, row.review or "no row with this key"] for row in unmatched]
    if fresh:
        lines.insert(0, [*header, "Note"])
    start = 1 if fresh else extra.max_row + 1
    for offset, line in enumerate(lines):
        for column, value in enumerate(line, start=1):
            _set(extra.cell(row=start + offset, column=column), value)


_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_MODIFIED = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")


def _save_reproducible(workbook: Any, output: Path) -> None:
    """Save `workbook` so the same input gives the same bytes: the archive entries get a fixed
    date and the document's "modified" stamp keeps the source's value instead of the clock."""

    props = workbook.properties
    stamp = props.modified or props.created
    stamp_text = stamp.strftime("%Y-%m-%dT%H:%M:%SZ").encode() if stamp is not None else None
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    result = io.BytesIO()
    with zipfile.ZipFile(buffer) as source, zipfile.ZipFile(result, "w") as target:
        for entry in source.infolist():
            data = source.read(entry.filename)
            if entry.filename == "docProps/core.xml" and stamp_text is not None:
                data = _MODIFIED.sub(rb"\g<1>" + stamp_text + rb"\g<2>", data)
            info = zipfile.ZipInfo(entry.filename, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = entry.external_attr
            target.writestr(info, data)
    output.write_bytes(result.getvalue())
