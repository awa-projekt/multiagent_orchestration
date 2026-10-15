"""Row numbering and sampling rules shared by the profile and every re-reader."""

from __future__ import annotations

import csv
import heapq
import zlib
from collections import deque
from collections.abc import Iterable, Iterator
from typing import Any

Row = tuple[int, list[Any]]
"""A non-blank row: its 1-based row (or line) number in the file and its cells."""

NO_DELIMITER = "\x1f"  # a one-column file: a character that cannot split anything
_KEPT_HEAD = 100  # first rows that stay in a sample
_TAIL = 10  # last rows that stay in a sample (for totals rows)


def numbered_records(
    lines: Iterable[str], delimiter: str | None
) -> Iterator[tuple[int, int, list[str]]]:
    """The records of delimited text as `(first line, last line, cells)`, lines counted from 1.

    The one place that numbers CSV rows. `lines` comes from a stream opened with `newline=""`;
    `delimiter=None` is a one-column file.
    """

    reader = csv.reader(lines, delimiter=delimiter or NO_DELIMITER)
    previous = 0
    for cells in reader:
        start, previous = previous + 1, reader.line_num
        yield start, previous, cells


def spread_indices(count: int, wanted: int) -> list[int]:
    """`wanted` indices spread evenly over `count` items, always the first and the last."""

    wanted = min(wanted, count)
    if wanted <= 0:
        return []
    if wanted == 1:
        return [0]
    return sorted({round(i * (count - 1) / (wanted - 1)) for i in range(wanted)})


class RowSample:
    """The rows a profile needs from a stream, in bounded memory.

    Up to `exact` rows are kept; beyond, the first rows and a reproducible hash sample of `size`
    rows. `total` stays exact and the last rows are kept in `tail`.
    """

    def __init__(self, exact: int, size: int) -> None:
        self.exact = exact
        self.size = min(size, exact)
        self.total = 0
        self.sampled = False
        self._head = min(_KEPT_HEAD, self.size // 2)
        self._rows: list[Row] = []
        self._pool: list[tuple[int, int, list[Any]]] = []  # a heap of (-hash, number, cells)
        self._tail: deque[Row] = deque(maxlen=_TAIL)

    def add(self, number: int, cells: list[Any]) -> None:
        """Take the next non-blank row (`number` is its row or line number in the file)."""

        row = (number, cells)
        self.total += 1
        self._tail.append(row)
        if self.sampled:
            self._offer(row)
            return
        self._rows.append(row)
        if self.total > self.exact:
            self.sampled = True
            rest, self._rows = self._rows[self._head :], self._rows[: self._head]
            for kept in rest:
                self._offer(kept)

    def _offer(self, row: Row) -> None:
        """Keep the row when its number hashes lower than the highest hash kept so far."""

        number, cells = row
        item = (-zlib.crc32(b"%d" % number), number, cells)
        if len(self._pool) < self.size - self._head:
            heapq.heappush(self._pool, item)
        elif item > self._pool[0]:
            heapq.heapreplace(self._pool, item)

    def drop_leading(self, count: int) -> None:
        """Remove the first `count` cells of every row kept (columns that are blank throughout)."""

        self._rows = [(n, cells[count:]) for n, cells in self._rows]
        self._pool = [(h, n, cells[count:]) for h, n, cells in self._pool]
        self._tail = deque(((n, cells[count:]) for n, cells in self._tail), maxlen=_TAIL)

    @property
    def rows(self) -> list[Row]:
        """The rows kept, in file order."""

        return self._rows + sorted(((n, cells) for _, n, cells in self._pool), key=lambda r: r[0])

    @property
    def tail(self) -> list[Row]:
        """The last rows of the stream, in file order."""

        return list(self._tail)
