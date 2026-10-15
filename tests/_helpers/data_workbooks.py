"""Build and damage Excel workbooks for tests (repository-only test support).

The workbooks are written by `openpyxl` into a test's `tmp_path`: no workbook file is stored in
the repository. `patch_member` and `add_member` rewrite one part of a finished file (a wrong
dimension, a namespace prefix, a macro project) the way a foreign producer would have written it.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable
from pathlib import Path

from openpyxl import Workbook


def save(book: Workbook, folder: Path, name: str = "book.xlsx") -> Path:
    """Write the workbook and return its path."""

    path = folder / name
    book.save(path)
    return path


def rows_to(sheet: object, rows: list[list[object]], start: int = 1, column: int = 1) -> None:
    """Write `rows` into `sheet` starting at (`start`, `column`), one list per row."""

    for offset, values in enumerate(rows):
        for shift, value in enumerate(values):
            if value is not None:
                sheet.cell(row=start + offset, column=column + shift, value=value)  # type: ignore[attr-defined]


def patch_member(path: Path, member: str, change: Callable[[str], str]) -> Path:
    """Rewrite the text of one part of the workbook in place."""

    with zipfile.ZipFile(path) as source:
        parts = {info.filename: source.read(info.filename) for info in source.infolist()}
    parts[member] = change(parts[member].decode("utf-8")).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)
    return path


def add_member(path: Path, member: str, data: bytes) -> Path:
    """Add a part (for example `xl/vbaProject.bin`) to the workbook."""

    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as target:
        target.writestr(member, data)
    return path


def with_namespace_prefix(xml: str, prefix: str = "x") -> str:
    """The same worksheet XML with every element written as `<x:name>` (a valid variant)."""

    xml = re.sub(r"<(/?)([A-Za-z]\w*)", rf"<\1{prefix}:\2", xml)
    return xml.replace(f"<{prefix}:worksheet xmlns=", f"<{prefix}:worksheet xmlns:{prefix}=", 1)
