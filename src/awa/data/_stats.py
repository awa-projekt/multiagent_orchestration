"""From rows to a `TableProfile`: header row, totals rows, columns, keys (CSV and Excel)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, NamedTuple

from awa.data._infer import cell_kind, id_named, infer_column, number_reader
from awa.data._injection import injection_flags
from awa.data._model import ColumnProfile, Flag, TableProfile, column_key, make_flag
from awa.data._pii import classify_pii
from awa.data._sampling import Row, RowSample, spread_indices

_HEAD_ROWS = 20  # rows looked at to find the header
_BELOW = 8  # rows below a header candidate that are compared with it
_HEADER_CANDIDATES = 12  # the header is one of the first non-blank rows
_MAX_KEYS = 5
_KEY_CHARS = 40  # a key is short: longer unique text is a description, not an identifier
_TOTAL_LABEL = re.compile(
    r"\s*(?:grand\s+)?(?:gesamt\w*|summe|sum|total|totals|zwischensumme|subtotal|endsumme|"
    r"übertrag|netto|brutto|saldo|mwst\.?|ust\.?|vat)(?![^\W\d_])",
    re.IGNORECASE,
)
_TOTAL_STARTS = frozenset("GgSsTtZzEeÜüNnBbMmUuVv 	")  # first characters of a total label
_POSITION = re.compile(r"(?:^|_)(?:line|zeile|pos|position|item|seq|idx|index)(?:_|$)")
_LABEL = ("text", "bool")  # a header may be a word that is also a yes/no word (`No`, `Y`)
_TYPED = ("number", "date", "bool")
_NO_PERSONAL_DATA = ("empty", "bool", "decimal", "time")  # no value rule of the checks applies
_MAX_PII_VALUES = 1000  # distinct values of a column shown to the personal-data check
_MIN_INJECTION_CHARS = 24  # shorter text cannot hold an instruction worth a flag
_MAX_INJECTION_STRINGS = 300  # distinct long strings of one column that are scanned


class Header(NamedTuple):
    """Where the header is among the rows (`None`: there is none) and how sure that is."""

    row: int | None
    confidence: float
    banner: int | None = None


def find_header(rows: Sequence[Sequence[Any]]) -> Header:
    """The header row of a table among its first rows.

    A header has only text cells and typed cells below it; title lines above are skipped and a
    banner line is combined with the header below it.
    """

    head = _Head(rows)
    for r in range(min(len(head.rows), _HEADER_CANDIDATES)):
        if head.banner(r):
            return Header(r + 1, 0.8, banner=r)
        if not head.labels(r):
            continue
        found = head.contrast(r)
        if found:
            sure = found >= 2 or 2 * found >= len(head.filled(r))
            return Header(r, 0.95 if sure else 0.85)
        untyped = head.untyped_header(r)
        if untyped is not None:
            return untyped
    typed = any(k in ("number", "date") for i in range(len(head.rows)) for k in head.kinds(i))
    return Header(None, 0.8 if typed else 0.5)


def letters(index: int) -> str:
    """Spreadsheet letters of a 0-based column index: 0 `A`, 25 `Z`, 26 `AA`."""

    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def cell_value(value: Any, limit: int) -> str | int | float | bool | None:
    """A cell as it goes into `sample_rows`: text cut to `limit`, dates as ISO text, no objects."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float | Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time() else value.isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    return str(value)[:limit]


def build_table(
    sample: RowSample,
    *,
    table_id: str,
    max_sample_rows: int,
    cell_chars: int,
    sheet: str | None = None,
    unnamed: Callable[[int], str] | None = None,
    check_ragged: bool = False,
    sum_rows: frozenset[int] = frozenset(),
) -> tuple[TableProfile, list[Flag]]:
    """The `TableProfile` of the rows in `sample`, with the flags that explain its decisions.

    Args:
        table_id: Id of the table (`f1.t1`); column flags point at `<table_id>.<key>`.
        unnamed: Name of a header cell without text, by 0-based index (default: column letters).
        check_ragged: Count rows with fewer cells than the table (delimited text only).
        sum_rows: Row numbers whose cells hold a `SUM(` formula (a totals hint).

    Returns:
        The table and its flags, table flags first.
    """

    flags: list[Flag] = []

    def flag(code: str, message: str) -> None:
        flags.append(make_flag(code, message, table_id))

    rows = sample.rows
    header = find_header([cells for _, cells in rows])
    start = 0 if header.row is None else header.row + 1
    width = _width(header, rows, rows[start:])
    names = _names(header, rows, width, unnamed or letters)
    cut = _cut_totals(sample, rows[start:], width, names, sum_rows, cell_chars, table_id, flag)
    data, totals = cut.data, cut.totals
    header_row = 0 if header.row is None else rows[header.row][0]
    table = TableProfile(
        id=table_id,
        sheet=sheet,
        header_row=header_row,
        header_confidence=header.confidence,
        data_start_row=data[0][0] if data else max(header_row + 1, 1),
        rows=len(data) if not sample.sampled else max(sample.total - start - len(totals), 0),
        sampled=sample.sampled,
        rows_seen=len(data) if sample.sampled else None,
        totals_rows=totals,
        columns=cut.columns,
    )
    _describe_header(table, header, rows, flag)
    if not data and header.row is not None:
        flag("DF-W-NO-DATA-ROWS", "the table has a header and no data rows")
    if check_ragged:
        _ragged(data, width, flag)
    if not sample.sampled and data:
        table.key_candidates = _key_candidates(cut.cells, cut.columns)
        repeated = len(cut.cells) - len({tuple(row) for row in cut.cells})
        if repeated:
            flag("DF-W-DUPLICATE-ROWS", f"{repeated} row(s) repeat an earlier row exactly")
    table.sample_rows = _pick(data, sample, set(totals), max_sample_rows, width, cell_chars)
    return table, flags + cut.column_flags


def blank(cell: Any) -> bool:
    """Whether a cell holds nothing: `None` or only white space."""

    return cell is None or (isinstance(cell, str) and not cell.strip())


def span(cells: Sequence[Any]) -> int:
    """Number of cells up to the last one that holds something."""

    for j in range(len(cells) - 1, -1, -1):
        if not blank(cells[j]):
            return j + 1
    return 0


class _Head:
    """The first rows of a table, with the cell kinds each row has (worked out once per row)."""

    def __init__(self, rows: Sequence[Sequence[Any]]) -> None:
        self.rows = [list(row) for row in rows[:_HEAD_ROWS]]
        self._kinds: dict[int, list[str]] = {}
        spans = [n for n in (span(row) for row in self.rows) if n]
        self.width = max(spans, key=lambda w: (spans.count(w), w), default=0)

    def kinds(self, r: int) -> list[str]:
        if r not in self._kinds:
            self._kinds[r] = [cell_kind(cell) for cell in self.rows[r]]
        return self._kinds[r]

    def filled(self, r: int) -> list[int]:
        return [j for j, cell in enumerate(self.rows[r]) if not blank(cell)]

    def labels(self, r: int) -> bool:
        cols = self.filled(r)
        kinds = self.kinds(r)
        return (
            bool(cols) and all(kinds[j] in _LABEL for j in cols) and len(cols) >= 0.6 * self.width
        )

    def contrast(self, r: int) -> int:
        """How many cells of row `r` stand over typed values of another kind."""

        count = 0
        kinds = self.kinds(r)
        for j in self.filled(r):
            below = [
                self.kinds(i)[j]
                for i in range(r + 1, min(r + 1 + _BELOW, len(self.rows)))
                if j < len(self.rows[i]) and self.kinds(i)[j] != "empty"
            ]
            # A header cell that is a yes/no word contrasts with numbers and dates below it, but a
            # yes/no word over yes/no values is the same kind as its column: that row is data.
            typed = sum(kind in _TYPED and kind != kinds[j] for kind in below)
            count += bool(below) and typed >= 0.8 * len(below)
        return count

    def banner(self, r: int) -> bool:
        """Group names over some of the columns of the header line below (`Q1  Q2`)."""

        cols, nxt = self.filled(r), r + 1
        return (
            len(cols) >= 2
            and all(self.kinds(r)[j] in _LABEL for j in cols)
            and all(b - a >= 2 for a, b in zip(cols, cols[1:], strict=False))
            and nxt < len(self.rows)
            and self.labels(nxt)
            and set(cols) <= set(self.filled(nxt))
            and self.contrast(nxt) > 0
        )

    def untyped_header(self, r: int) -> Header | None:
        """Row `r` as the header of a table without typed values below it, if it reads as one."""

        nxt = r + 1
        typed_below = any(
            kind in _TYPED
            for i in range(nxt, min(nxt + _BELOW, len(self.rows)))
            for kind in self.kinds(i)
        )
        if typed_below or (nxt >= len(self.rows) and r != 0):  # a header needs rows below it
            return None
        text = [str(self.rows[r][j]).strip() for j in self.filled(r)]
        if any(_digit_heavy(cell) for cell in text):
            return None
        below = {
            str(row[j]).strip() for row in self.rows[nxt:] for j in self.filled(r) if j < len(row)
        }
        return Header(r, 0.55 if below & set(text) else 0.75)


def _digit_heavy(text: str) -> bool:
    return len(text) > 1 and sum(ch.isdigit() for ch in text) * 2 >= len(text)


def _width(header: Header, rows: list[Row], data: list[Row]) -> int:
    """The number of columns: the most common span of the data rows, at least that of the header."""

    spans = Counter(filter(None, (span(cells) for _, cells in data)))
    width = max(spans, key=lambda w: (spans[w], w), default=0)
    if header.row is not None:
        width = max(width, span(rows[header.row][1]))
    return width


class _Cut(NamedTuple):
    """The data rows once totals rows are taken out, their padded cells and the columns."""

    data: list[Row]
    cells: list[list[Any]]
    columns: list[ColumnProfile]
    column_flags: list[Flag]
    totals: list[int]


def _cut_totals(
    sample: RowSample,
    data: list[Row],
    width: int,
    names: list[str],
    sum_rows: frozenset[int],
    cell_chars: int,
    table_id: str,
    flag: Callable[[str, str], None],
) -> _Cut:
    """Leave labeled or sum-equal totals rows out of the data, flagged with `DF-W-TOTALS-ROW`."""

    keys = _keys_of(names)
    labeled = {n for n, cells in _with_tail(data, sample) if _is_total(cells, n, sum_rows)}
    data = [row for row in data if row[0] not in labeled]
    totals = sorted(labeled)
    cells = [(row[1][:width] + [None] * width)[:width] for row in data]
    columns, column_flags = _columns(cells, names, keys, sample.sampled, cell_chars, table_id)
    if labeled:
        every = {**dict(sample.rows), **dict(sample.tail)}
        # A sample holds only some rows above a total: its sums would not match.
        evidence = "" if sample.sampled else _sum_evidence(totals, every, data, cells, columns)
        flag(
            "DF-W-TOTALS-ROW",
            f"rows {', '.join(map(str, totals))} are labeled as totals{evidence}; they are not counted in rows or statistics",
        )
    elif len(data) >= 4 and not sample.sampled:
        evidence = _unlabeled_total(cells, columns, data[-1][0] in sum_rows)
        if evidence:
            totals = [data[-1][0]]
            data, cells = data[:-1], cells[:-1]
            columns, column_flags = _columns(cells, names, keys, False, cell_chars, table_id)
            flag(
                "DF-W-TOTALS-ROW",
                f"row {totals[0]} has no label but its numbers are the sums of the rows above ({evidence}); it is not counted in rows or statistics",
            )
    return _Cut(data, cells, columns, column_flags, totals)


def _with_tail(data: list[Row], sample: RowSample) -> list[Row]:
    """The data rows plus, for a sampled table, the last rows of the stream that were not kept."""

    if not sample.sampled:
        return data
    seen = {n for n, _ in data}
    return sorted([*data, *(row for row in sample.tail if row[0] not in seen)], key=lambda r: r[0])


def _names(header: Header, rows: list[Row], width: int, unnamed: Callable[[int], str]) -> list[str]:
    if header.row is None:
        return [letters(j) for j in range(width)]
    cells = _texts(rows[header.row][1], width)
    if header.banner is not None:
        upper = _texts(rows[header.banner][1], width)
        for j in range(1, width):  # a banner cell spans the columns up to the next banner cell
            upper[j] = upper[j] or upper[j - 1]
        cells = [f"{up} {low}".strip() for up, low in zip(upper, cells, strict=True)]
    return [cell or unnamed(j) for j, cell in enumerate(cells)]


def _texts(cells: Sequence[Any], width: int) -> list[str]:
    texts = [" ".join(str(cell_value(cell, 200) or "").split()) for cell in cells[:width]]
    return texts + [""] * (width - len(texts))


def _keys_of(names: list[str]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for name in names:
        base = column_key(name) or "column"
        key, number = base, 2
        while key in seen:
            key, number = f"{base}_{number}", number + 1
        keys.append(key)
        seen.add(key)
    return keys


def _columns(
    cells: list[list[Any]],
    names: list[str],
    keys: list[str],
    sampled: bool,
    cell_chars: int,
    table_id: str,
) -> tuple[list[ColumnProfile], list[Flag]]:
    """Infer every column. In a sample, distinct counts and uniqueness are not provable."""

    columns: list[ColumnProfile] = []
    flags: list[Flag] = []
    for j, (name, key) in enumerate(zip(names, keys, strict=True)):
        notes: list[tuple[str, str]] = []
        values = [row[j] for row in cells]
        column = infer_column(
            values, name=name, key=key, index=j, cell_chars=cell_chars, notes=notes
        )
        if sampled:
            column.distinct, column.unique = None, False
        where = f"{table_id}.{key}"
        flags.extend(make_flag(code, text, where) for code, text in notes)
        _classify_pii(column, values)
        flags.extend(f for f in _injection(where, column, values) if f not in flags)
        columns.append(column)
    return columns, flags


def _classify_pii(column: ColumnProfile, values: Iterable[Any]) -> None:
    """Set `column.pii` from the name and the distinct values of the column (at most 1000, evenly
    spread; `share` is then the share of those distinct values)."""

    if column.type in _NO_PERSONAL_DATA:
        return
    distinct = list(dict.fromkeys(str(v) for v in values if v is not None))
    if len(distinct) > _MAX_PII_VALUES:
        distinct = [distinct[i * len(distinct) // _MAX_PII_VALUES] for i in range(_MAX_PII_VALUES)]
    column.pii = classify_pii(distinct, column.name)


def _injection(where: str, column: ColumnProfile, values: Sequence[Any]) -> list[Flag]:
    """Flags (located at `where`) for text cells that look like instructions for an assistant."""

    if column.type not in ("text", "enum", "mixed"):
        return []
    strings = [
        v for v in dict.fromkeys(values) if isinstance(v, str) and len(v) >= _MIN_INJECTION_CHARS
    ]
    found: list[Flag] = []
    for text in strings[:_MAX_INJECTION_STRINGS]:
        found += [f for f in injection_flags(text, where) if f not in found]
    return found


def _is_total(cells: Sequence[Any], number: int, sum_rows: frozenset[int]) -> bool:
    """A row labeled as a total (`Summe`, `Total`, ...) with nothing but numbers besides."""

    label = None
    for j in range(min(3, len(cells))):
        cell = cells[j]
        if cell.__class__ is str and cell[:1] in _TOTAL_STARTS and _TOTAL_LABEL.match(cell):
            label = j
            break
    if label is None:
        return False
    others = [cell_kind(cell) for j, cell in enumerate(cells) if j != label]
    return all(kind in ("empty", "number") for kind in others) and (
        "number" in others or number in sum_rows
    )


def _sum_evidence(
    totals: list[int],
    lines: dict[int, list[Any]],
    data: list[Row],
    cells: list[list[Any]],
    columns: list[ColumnProfile],
) -> str:
    """How many numbers of the totals rows equal the sum of the rows above them."""

    matched = checked = 0
    for index, number in enumerate(totals):
        previous = totals[index - 1] if index else 0
        for j, column in enumerate(columns):
            if column.type not in ("integer", "decimal") or column.ambiguous:
                continue
            read = number_reader(column)
            written = read(lines[number][j]) if j < len(lines[number]) else None
            if written is None:
                continue
            above = [
                (n, read(row[j])) for (n, _), row in zip(data, cells, strict=True) if n < number
            ]
            sums = [
                sum(v for _, v in above if v is not None),
                sum(v for n, v in above if v is not None and n > previous),
            ]
            checked += 1
            matched += any(_equal_sum(written, s) for s in sums)
    return (
        f" (sums checked: {matched} of {checked} numbers equal the rows above)" if checked else ""
    )


def _unlabeled_total(
    cells: list[list[Any]], columns: list[ColumnProfile], has_sum_formula: bool
) -> str:
    """Evidence that the last row is a total without a label, or an empty string."""

    last = cells[-1]
    kinds = [cell_kind(cell) for cell in last]
    if any(kind not in ("empty", "number") for kind in kinds) or "number" not in kinds:
        return ""
    matched = 0
    for j, column in enumerate(columns):
        if kinds[j] != "number" or column.type not in ("integer", "decimal") or column.ambiguous:
            continue
        read = number_reader(column)
        total, written = (
            sum(v for v in map(read, (row[j] for row in cells[:-1])) if v is not None),
            read(last[j]),
        )
        matched += bool(total) and written is not None and _equal_sum(written, total)
    if matched >= 2 or (matched and has_sum_formula):
        return f"{matched} number(s) equal the sum of the rows above"
    return ""


def _equal_sum(written: float, total: float) -> bool:
    return abs(written - total) <= max(0.01, abs(total) * 1e-6)


def _describe_header(
    table: TableProfile, header: Header, rows: list[Row], flag: Callable[[str, str], None]
) -> None:
    if header.row is None:
        flag(
            "DF-W-NO-HEADER",
            "no header row found: the first row holds data; columns are named A, B, ...",
        )
    elif header.banner is not None:
        flag(
            "DF-W-MULTIROW-HEADER",
            f"rows {rows[header.banner][0]} and {rows[header.row][0]} form a two-line header; column names join both lines",
        )
    elif header.row > 0:
        flag(
            "DF-W-HEADER-ROW",
            f"the header is row {table.header_row}; the {header.row} non-blank row(s) above it are not table data and are ignored",
        )
    if header.row is not None and header.confidence < 0.7:
        flag(
            "DF-W-HEADER-UNCERTAIN",
            f"row {table.header_row} is taken as the header but a value of it also occurs below in the same column, so it may be a data row",
        )


def _ragged(data: list[Row], width: int, flag: Callable[[str, str], None]) -> None:
    bad = [n for n, cells in data if len(cells) < width or span(cells) > width]
    if bad:
        shown = ", ".join(map(str, bad[:5])) + (", ..." if len(bad) > 5 else "")
        flag(
            "DF-W-RAGGED-ROWS",
            f"{len(bad)} row(s) do not have {width} cells (lines {shown}); missing cells count as empty, cells beyond the table are ignored",
        )


def _key_candidates(cells: list[list[Any]], columns: list[ColumnProfile]) -> list[list[str]]:
    """Columns (else pairs of columns) whose values are never empty and never repeat."""

    def single(types: tuple[str, ...]) -> list[int]:
        found = (
            j
            for j, c in enumerate(columns)
            if c.unique
            and not c.nullable
            and c.type in types
            and max((len(str(row[j])) for row in cells), default=0) <= _KEY_CHARS
        )
        return sorted(found, key=lambda j: (not id_named(columns[j].name), j))

    keys = single(("text", "id", "integer")) or single(("date", "datetime"))
    if keys or len(cells) > 100_000:
        return [[columns[j].key] for j in keys[:_MAX_KEYS]]
    usable = [
        j
        for j, c in enumerate(columns[:8])
        if not c.nullable
        and (
            c.type in ("text", "id", "enum", "date", "datetime")
            or (c.type == "integer" and (id_named(c.name) or _POSITION.search(c.key)))
        )
    ]
    pairs = []
    for a, first in enumerate(usable):
        for second in usable[a + 1 :]:
            if len({(row[first], row[second]) for row in cells}) == len(cells):
                pairs.append([columns[first].key, columns[second].key])
    return pairs[:_MAX_KEYS]


def _pick(
    data: list[Row], sample: RowSample, totals: set[int], count: int, width: int, cell_chars: int
) -> list[list[Any]]:
    """`count` rows spread over the data (the first and the last included)."""

    rows = list(data)
    if sample.sampled and rows:
        rows.extend(row for row in sample.tail if row[0] not in totals and row[0] > rows[-1][0])
    if not rows:
        return []
    return [
        [cell_value(c, cell_chars) for c in (rows[i][1][:width] + [None] * width)[:width]]
        for i in spread_indices(len(rows), count)
    ]
