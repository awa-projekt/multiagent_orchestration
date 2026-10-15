"""Builders and scripted adapters for tests of the data checks (repository only, not shipped).

Profiles are built by hand here so that a validator, the digest or the fit check can be tested
against exactly the facts a test needs; real files are profiled with `awa.data.profile` in the
tests themselves. The scripted classes are adapters of the ports the production code defines
(`DataReader` in `awa.data`, `FitJudge` in `awa.blueprints.ports`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path

from awa.blueprints._fit import FitVerdict
from awa.data import DataProfile, FileProfile
from awa.data._model import Cell, ColumnProfile, PdfInfo, TableProfile


def column(name: str, **values: object) -> ColumnProfile:
    """A column with the key `name.lower()`; other fields as keyword arguments."""

    return ColumnProfile(name=name, key=name.lower(), index=0, **values)  # type: ignore[arg-type]


def wrap(*files: FileProfile) -> DataProfile:
    """A profile holding exactly these files."""

    return DataProfile(awa_version="1.2.0", created="2026-10-05T00:00:00Z", files=list(files))


def csv_file(
    number: int, path: str, columns: list[ColumnProfile], rows: list[list[object]]
) -> FileProfile:
    """A CSV file `f<number>` with one table whose sample rows are `rows` and whose `source`
    points at a path that does not exist (a test of the digest must never print it)."""

    table = TableProfile(
        id=f"f{number}.t1",
        rows=len(rows),
        columns=columns,
        sample_rows=rows,  # type: ignore[arg-type]
    )
    return FileProfile(
        id=f"f{number}",
        path=path,
        source=f"C:/secret/place/{path}",
        sha256="0" * 64,
        size=1000 * number,
        kind="csv",
        tables=[table],
    )


def table_file(number: int, path: str, columns: list[ColumnProfile], rows: int = 3) -> FileProfile:
    """A CSV file `f<number>` with one table of `rows` rows and no sample rows."""

    table = TableProfile(id=f"f{number}.t1", columns=columns, rows=rows)
    return FileProfile(
        id=f"f{number}", path=path, sha256="0" * 64, size=1, kind="csv", tables=[table]
    )


def pdf_file(number: int, path: str, document_class: str) -> FileProfile:
    """A one-page PDF `f<number>` of the given class (`born_digital`, `scanned`, ...)."""

    return FileProfile(
        id=f"f{number}",
        path=path,
        sha256="0" * 64,
        size=1,
        kind="pdf",
        pdf=PdfInfo(pages=1, document_class=document_class),  # type: ignore[arg-type]
    )


def many_files(count: int, columns: int = 12) -> DataProfile:
    """`count` CSV files in seven folders, each with `columns` text columns and three rows."""

    files = []
    for number in range(1, count + 1):
        cols = [
            column(f"col_{number}_{c}", type="text", distinct=3, examples=["x", "y"])
            for c in range(columns)
        ]
        rows: list[list[object]] = [
            [f"v{number}-{r}-{c}" for c in range(columns)] for r in range(3)
        ]
        files.append(csv_file(number, f"folder{number % 7}/file_{number}.csv", cols, rows))
    return wrap(*files)


def digest_blocks(text: str) -> list[tuple[str, list[str]]]:
    """(label, content lines) of every data block of a digest; fails when the markers are not
    well formed (a block inside a block, an end without a start, a block never closed)."""

    found: list[tuple[str, list[str]]] = []
    inside: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        start = re.fullmatch(r"<<<DATA (\S+)>>>", line)
        if start:
            assert inside is None, "a block starts inside another block"
            inside = (start[1], [])
        elif line == "<<<END DATA>>>":
            assert inside is not None, "an end marker without a start"
            found.append(inside)
            inside = None
        elif inside is not None:
            inside[1].append(line)
    assert inside is None, "a block was never closed"
    return found


def digest_outside(text: str) -> str:
    """The text of a digest that is not inside a data block."""

    return re.sub(r"<<<DATA \S+>>>\n.*?\n<<<END DATA>>>\n?", "", text, flags=re.DOTALL)


class ScriptedReader:
    """A `DataReader` that returns fixed rows and remembers which files were asked for."""

    def __init__(self, rows: Sequence[list[Cell]] = (["A", "x"], ["A", "x"])) -> None:
        self._rows = [list(row) for row in rows]
        self.asked: list[str] = []

    def rows(
        self, file: FileProfile, table: TableProfile, *, max_rows: int
    ) -> Iterator[list[Cell]]:
        self.asked.append(file.path)
        return iter(self._rows)

    def text(self, file: FileProfile) -> tuple[str, bool]:
        raise AssertionError("this scripted reader has no text")


class ScriptedJudge:
    """A `FitJudge` that answers with a fixed `FitVerdict` and records what it was asked."""

    def __init__(self, answer: FitVerdict) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, list[str]]] = []

    async def __call__(self, description: str, digest: str, findings: Sequence[str]) -> FitVerdict:
        self.calls.append((description, digest, list(findings)))
        return self.answer


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


def write_png(folder: Path, name: str = "photo.png") -> Path:
    """Write a real 1x1 PNG image (a kind awa detects but does not read) and return its path."""

    path = folder / name
    path.write_bytes(_PNG)
    return path
