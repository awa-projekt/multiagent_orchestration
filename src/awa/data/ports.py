"""The interfaces `awa.data` is written against: parsers, scanners, readers, value sources."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from awa.data._model import Cell, FileProfile, TableProfile
    from awa.data._parse import ParseContext

Parser = Callable[[Path, "ParseContext"], "FileProfile"]
"""`parser(path, ctx) -> FileProfile`: add what you learn to `ctx.profile` and return it. A parser
never raises for bad content (it returns `ctx.fail(...)`), never executes what it reads, and
stays inside `ctx.limits` and its deadline. Details: `awa.data._parse`."""


class UploadScanner(Protocol):
    """A virus scanner or other check of each accepted file: `scan` rejects by raising."""

    def scan(self, path: Path) -> None: ...


class DataReader(Protocol):
    """What the checks need to see of the real data.

    A missing, unreadable or unsuitable file raises `DataError`; more rows than allowed raise
    `RowLimitError` while iterating. A reader never returns partial data silently.
    """

    def rows(
        self, file: FileProfile, table: TableProfile, *, max_rows: int
    ) -> Iterator[list[Cell]]:
        """The data rows of `table` (header, blank and totals rows left out), one cell per
        profiled column; raises `RowLimitError` while iterating past `max_rows`."""
        ...

    def text(self, file: FileProfile) -> tuple[str, bool]:
        """The text of a document and whether it is the whole text (a long one is cut)."""
        ...


class ColumnValues(NamedTuple):
    """The distinct values of one column, as written (blank cells left out)."""

    values: frozenset[str]
    cells: int  # non-blank cells read; equal to len(values) when no value repeats
    complete: bool  # the whole column was read


ValueSource = Callable[[Sequence[str]], Mapping[str, ColumnValues]]
"""`source(column_ids) -> {column_id: ColumnValues}`; an id it cannot read is missing from the answer."""


def local_reader() -> DataReader:
    """The reader of the files a profile came from (CSV, Excel and SQLite rows; document text)."""

    from awa.data._source_rows import LocalFileReader

    return LocalFileReader()


__all__ = ["ColumnValues", "DataReader", "Parser", "UploadScanner", "ValueSource", "local_reader"]
