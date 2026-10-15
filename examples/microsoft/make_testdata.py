"""Build the synthetic SharePoint files of the Microsoft process examples.

The price list and the list of responsibilities are Excel workbooks; their content is kept as
CSV files in `testdata/` (easy to read and to change) and made into workbooks here, with real
numbers and real dates in the cells, as a person would type them.

    python examples/microsoft/make_testdata.py OUT_FOLDER

writes `OUT_FOLDER/Approved/` with `price-list.xlsx`, `responsibilities.xlsx` and the sample
documents. Upload that folder to a TEST SharePoint site to try the examples on a real tenant;
the offline tests build the same files in memory. Needs the `files` extra (openpyxl).
"""

from __future__ import annotations

import csv
import io
import sys
from datetime import date
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"
WORKBOOKS = {
    "price-list.xlsx": ("price-list", ("Prices", "Discounts", "Approval", "Tax")),
    "responsibilities.xlsx": ("responsibilities", ("Responsibilities",)),
}


def _cell(text: str) -> Any:
    """A CSV field as a cell: blank is empty, `2026-01-31` a date, `19` or `199.90` a number."""

    if not text.strip():
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() and "." not in text else number


def workbook(folder: str, sheets: tuple[str, ...]) -> bytes:
    """The workbook with one sheet per `testdata/<folder>/<sheet>.csv`, as `.xlsx` bytes."""

    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name in sheets:
        sheet = book.create_sheet(name)
        text = (TESTDATA / folder / f"{name}.csv").read_text(encoding="utf-8")
        header, *rows = csv.reader(io.StringIO(text))
        sheet.append(header)
        for row in rows:
            sheet.append([_cell(field) for field in row])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def documents() -> dict[str, bytes]:
    """The sample documents (name to bytes) that sit next to the workbooks in `Approved`."""

    found = {path.name: path.read_bytes() for path in sorted((TESTDATA / "documents").iterdir())}
    found.update({name: workbook(folder, sheets) for name, (folder, sheets) in WORKBOOKS.items()})
    return found


def write(target: Path) -> list[Path]:
    """Write `target/Approved/*` and return the files."""

    folder = target / "Approved"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in documents().items():
        (folder / name).write_bytes(data)
        written.append(folder / name)
    return written


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python make_testdata.py OUT_FOLDER")
    for path in write(Path(sys.argv[1])):
        print(path)
