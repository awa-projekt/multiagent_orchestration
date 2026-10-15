"""Data tools: read, query and write CSV files, read, query and fill Excel workbooks, query JSON.

Files resolve inside the toolkit's `root`; `write_csv` waits for approval by default. CSV
encoding and delimiter are detected. `query_csv` and `query_excel` filter with the safe
expression language of `awa.kernel.conditions` (comparisons, `and`/`or`/`not`, `in [...]`,
arithmetic, text and number functions); numbers written as reports write them (`1.234,56 €`,
`12,5 %`, `(1,234.56)`) count as numbers, with the decimal separator detected per file. Column
names are case- and punctuation-insensitive (`unit_price` finds `Unit Price`).
"""

from __future__ import annotations

import codecs
import csv
import io
import itertools
import json
import os
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal, NotRequired

from pydantic import Field, model_validator
from typing_extensions import TypedDict

from awa._internal.install import require_module
from awa._internal.sandbox import Sandbox, check_zip_bounds, file_encoding
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.errors import ConfigurationError
from awa.kernel.conditions import as_number, detect_decimal, predicate
from awa.toolkits._common import (
    DEFAULT_MAX_OUTPUT_CHARS,
    ToolArgs,
    ToolSpecs,
    positive,
    selected,
    tool_json,
    toolkit_tools,
)
from awa.toolkits._jsonquery import json_query as run_json_query
from awa.tools.result_cache import DATA_ROOT_METADATA_KEY

DATA_TOOLS = (
    "read_csv",
    "query_csv",
    "write_csv",
    "read_excel",
    "query_excel",
    "json_query",
    "fill_excel",
)
"""Names of the tools `data_toolkit` builds, in order."""

_FILE_TOOLS = ("read_csv", "query_csv", "write_csv", "read_excel", "query_excel", "fill_excel")
_READERS = ("read_csv", "query_csv", "read_excel", "query_excel")
_AGGREGATE = re.compile(r"^\s*(count|count_distinct|sum|avg|min|max)\s*\(\s*([^()]*?)\s*\)\s*$")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_DELIMITERS = ",;\t|"
_MAX_FORMULA_NOTES = 10
_Cell = str | int | float | bool | None


def _key(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_")


def _blank(cell: Any) -> bool:
    return cell is None or (isinstance(cell, str) and cell.strip() == "")


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) in (b"\n", b"\r")


def _filled(row: Sequence[Any]) -> bool:
    return not all(_blank(cell) for cell in row)


def detect_encoding(path: str | os.PathLike[str]) -> str:
    """The encoding of a text file: the one its byte-order mark names, `utf-8` when the whole file
    is valid UTF-8, else `cp1252` (Windows-1252, the usual encoding of Excel and ERP exports)."""

    return file_encoding(path)[0]


def sniff_delimiter(sample: str) -> str:
    """The delimiter (`,`, `;`, tab or `|`) that splits the header into the most columns and
    keeps the column count of the sample rows; `,` when none splits the header.

    Decimal commas (`1.234,56`) do not mislead it: they split data rows, not the header.
    """

    lines = sample.splitlines(keepends=True)
    if len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines = lines[:-1]  # the sample may end within a row
    best, best_score = ",", (0.0, 0)
    for delimiter in _DELIMITERS:
        rows = [row for row in csv.reader(lines, delimiter=delimiter) if row]
        if not rows or len(rows[0]) < 2:
            continue
        width = len(rows[0])
        consistent = sum(len(row) == width for row in rows[1:]) / max(len(rows) - 1, 1)
        if (consistent, width) > best_score:
            best, best_score = delimiter, (consistent, width)
    return best


@dataclass
class _Table:
    """The columns of a table, a row iterator and how the file was read."""

    columns: list[str]
    rows: Iterator[list[Any]]
    meta: dict[str, Any] = field(default_factory=dict)
    sample: list[list[Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lookup = {_key(column): index for index, column in enumerate(self.columns)}
        for index, column in enumerate(self.columns):
            self._lookup.setdefault(column, index)

    def index(self, name: str) -> int:
        position = self._lookup.get(name)
        if position is None:
            position = self._lookup.get(_key(name))
        if position is None:
            raise ToolException(f"unknown column {name!r} (columns: {', '.join(self.columns)})")
        return position

    def resolver(self, row: list[Any]) -> Callable[[str], Any]:
        def names(name: str) -> Any:
            position = self.index(name)
            return row[position] if position < len(row) else ""

        return names


class ReadCsvArgs(ToolArgs):
    path: str = Field(description="CSV file, relative to the root.")
    offset: int = Field(default=0, ge=0, description="Data rows to skip first.")
    limit: int = Field(default=50, ge=1, description="Most rows returned.")
    delimiter: str | None = Field(
        default=None, max_length=1, description="Column separator; detected when omitted."
    )


class _QueryArgs(ToolArgs):
    where: str | None = Field(
        default=None,
        description="Filter, e.g. \"status == 'open' and amount > 100\" or "
        "\"contains(name, 'acme')\".",
    )
    columns: list[str] | None = Field(
        default=None, description="Columns to return (without group_by); default all."
    )
    group_by: list[str] | None = Field(default=None, description="Columns to group rows by.")
    aggregates: list[str] | None = Field(
        default=None,
        description="With or without group_by: count(), count_distinct(col), sum(col), "
        "avg(col), min(col), max(col).",
    )
    order_by: list[str] | None = Field(
        default=None,
        description="Columns or aggregates to sort by; prefix '-' for descending, e.g. "
        "['-sum(amount)'].",
    )
    limit: int = Field(default=50, ge=1, description="Most rows returned.")
    decimal: Literal[".", ","] | None = Field(
        default=None,
        description="Decimal separator of numbers written as text (',' for 1.234,56); "
        "detected when omitted.",
    )


class QueryCsvArgs(_QueryArgs):
    path: str = Field(description="CSV file, relative to the root.")
    delimiter: str | None = Field(default=None, max_length=1, description="Column separator.")


class WriteCsvArgs(ToolArgs):
    path: str = Field(description="CSV file to write, relative to the root.")
    columns: list[str] = Field(min_length=1, description="Header row.")
    rows: list[list[_Cell]] = Field(description="Data rows, one list of values per row.")
    mode: Literal["create", "overwrite", "append"] = Field(
        default="create",
        description="'create' refuses an existing file, 'overwrite' replaces it, 'append' "
        "adds rows (the header must match).",
    )
    delimiter: Literal[",", ";", "\t", "|"] | None = Field(
        default=None,
        description="Column separator: ',' by default (an appended file keeps its own); "
        "';' for Excel in German and other European settings.",
    )
    excel: bool = Field(
        default=False,
        description="Start a new file with a UTF-8 byte-order mark so Excel shows umlauts and "
        "other accents correctly.",
    )


class ReadExcelArgs(ToolArgs):
    path: str = Field(description="Excel workbook (.xlsx), relative to the root.")
    sheet: str | None = Field(default=None, description="Sheet name; default the first sheet.")
    offset: int = Field(default=0, ge=0, description="Data rows to skip first.")
    limit: int = Field(default=50, ge=1, description="Most rows returned.")
    header_row: int = Field(
        default=1,
        ge=0,
        description="Row number holding the column names (rows above it are skipped); 0: no "
        "header, columns are named A, B, C, ...",
    )


class QueryExcelArgs(_QueryArgs):
    path: str = Field(description="Excel workbook (.xlsx), relative to the root.")
    sheet: str | None = Field(default=None, description="Sheet name; default the first sheet.")
    header_row: int = Field(
        default=1, ge=0, description="Row number holding the column names; 0: columns A, B, C."
    )


class FillExcelRow(TypedDict):
    """One row to fill: its key, the values in the order of the columns, a review note.

    A plain dictionary, not a model: the call's arguments are stored with the run (approval)
    and stay JSON.
    """

    key: str
    values: list[_Cell]
    review: NotRequired[str | None]


class FillExcelArgs(ToolArgs):
    path: str = Field(description="The source workbook (.xlsx); it is never changed.")
    output: str = Field(description="The new workbook to write (.xlsx), relative to the root.")
    key_column: str = Field(description="The column of the source sheet that holds the keys.")
    columns: list[str] = Field(
        min_length=1, description="The new columns to add, in order (not in the source yet)."
    )
    rows: list[FillExcelRow] = Field(
        description="One entry per row to fill, matched by key: {key (e.g. an invoice number), "
        "values (in the order of columns; null leaves a cell empty; dates as YYYY-MM-DD, "
        "amounts as numbers), review (optional note: low confidence, a value missing)}."
    )
    sheet: str | None = Field(default=None, description="Sheet name; default the first sheet.")
    header_row: int = Field(default=1, ge=1, description="Row number holding the column names.")
    review_column: str | None = Field(
        default="Review", description="Column for the review notes of the rows; null for none."
    )
    append: bool = Field(
        default=False,
        description="Continue an output written earlier in this run (more rows into the same "
        "new columns) instead of starting again from the source.",
    )
    overwrite: bool = Field(
        default=False,
        description="Replace an existing output, starting again from the source; without it "
        "(or append) an existing output is refused.",
    )


class JsonQueryArgs(ToolArgs):
    query: str = Field(
        description="Path such as 'orders[?total > 100].id', 'items[*].price | sum' or "
        "'customer.\"postal code\"'."
    )
    data: str | None = Field(default=None, description="The JSON document, as text.")
    path: str | None = Field(
        default=None,
        description="Instead of data: a .json (or .jsonl, one document per line) file "
        "relative to the root.",
    )

    @model_validator(mode="after")
    def _one_source(self) -> JsonQueryArgs:
        if (self.data is None) == (self.path is None):
            raise ValueError("give either data or path")
        return self


@dataclass(frozen=True)
class _DataTools:
    sandbox: Sandbox | None
    max_rows: int
    max_file_bytes: int
    encoding: str
    escape_formulas: bool
    sources: Sandbox | None = None

    def _file(self, path_text: str, *, must_exist: bool = True) -> Path:
        if self.sandbox is None:
            raise ToolException("no data directory is configured for this tool (root)")
        path = self.sandbox.resolve(path_text, must_exist=must_exist)
        if must_exist:
            if not path.is_file():
                raise ToolException(f"{path_text!r} is not a file")
            if path.stat().st_size > self.max_file_bytes:
                raise ToolException(
                    f"{path_text!r} is larger than the limit of {self.max_file_bytes} bytes"
                )
        return path

    def _table(self, path: Path, delimiter: str | None) -> tuple[_Table, Callable[[], None]]:
        encoding = detect_encoding(path) if self.encoding == "auto" else self.encoding
        handle = path.open("r", encoding=encoding, newline="", errors="replace")
        try:
            sample = handle.read(16_384)
            handle.seek(0)
            delimiter = delimiter or sniff_delimiter(sample)
            reader = csv.reader(handle, delimiter=delimiter)
            columns = next(reader, [])
        except BaseException:
            handle.close()
            raise
        if columns and columns[0].startswith("﻿"):
            columns[0] = columns[0][1:]
        sample_rows = [
            row for row in csv.reader(io.StringIO(sample), delimiter=delimiter) if _filled(row)
        ][1:200]
        meta = {"encoding": encoding, "delimiter": delimiter}
        rows = (row for row in reader if _filled(row))  # blank lines are no rows
        table = _Table([column.strip() for column in columns], rows, meta, sample_rows)
        return table, handle.close

    def _limit(self, requested: int) -> int:
        return min(requested, self.max_rows)

    def read_csv(self, args: ReadCsvArgs) -> str:
        table, close = self._table(self._file(args.path), args.delimiter)
        try:
            limit = self._limit(args.limit)
            rows: list[list[str]] = []
            total = 0
            for row in table.rows:
                if total >= args.offset and len(rows) < limit:
                    rows.append(row)
                total += 1
        finally:
            close()
        return tool_json(
            {
                "columns": table.columns,
                "rows": rows,
                "total_rows": total,
                "offset": args.offset,
                "returned": len(rows),
                **table.meta,
            }
        )

    def query_csv(self, args: QueryCsvArgs) -> str:
        table, close = self._table(self._file(args.path), args.delimiter)
        try:
            return self._query(table, args)
        finally:
            close()

    def _query(self, table: _Table, args: _QueryArgs) -> str:
        decimal = args.decimal or detect_decimal(itertools.chain.from_iterable(table.sample))
        condition = predicate(args.where, decimal=decimal) if args.where else None
        matched = [
            row
            for row in table.rows
            if condition is None or condition.evaluate(table.resolver(row))
        ]
        if args.group_by or args.aggregates:
            columns, rows = self._aggregate(table, matched, args, decimal)
            rows = self._order(columns, rows, args.order_by, decimal)
        else:
            full = [row + [""] * (len(table.columns) - len(row)) for row in matched]
            ordered = self._order(table.columns, full, args.order_by, decimal)
            indexes = [table.index(name) for name in args.columns or table.columns]
            columns = [table.columns[i] for i in indexes]
            rows = [[row[i] for i in indexes] for row in ordered]
        limit = self._limit(args.limit)
        return tool_json(
            {
                "columns": columns,
                "rows": rows[:limit],
                "matched_rows": len(matched),
                "returned": min(len(rows), limit),
                "truncated": len(rows) > limit,
                "decimal": decimal,
                **table.meta,
            }
        )

    def _aggregate(
        self, table: _Table, rows: list[list[Any]], args: _QueryArgs, decimal: str
    ) -> tuple[list[str], list[list[Any]]]:
        group_indexes = [table.index(name) for name in args.group_by or ()]
        specs: list[tuple[str, str, int | None]] = []
        for text in args.aggregates or ["count()"]:
            match = _AGGREGATE.match(text)
            if match is None:
                raise ToolException(
                    f"invalid aggregate {text!r}; use count(), count_distinct(col), sum(col), "
                    "avg(col), min(col) or max(col)"
                )
            function, column = match.group(1), match.group(2)
            if function == "count" and column in ("", "*"):
                specs.append((f"count({column})", function, None))
            elif not column:
                raise ToolException(f"{function}() needs a column")
            else:
                specs.append((f"{function}({column})", function, table.index(column)))
        groups: dict[tuple[Any, ...], list[list[Any]]] = {}
        for row in rows:
            key = tuple(row[i] if i < len(row) else "" for i in group_indexes)
            groups.setdefault(key, []).append(row)
        if not group_indexes and not groups:
            groups[()] = []
        columns = [table.columns[i] for i in group_indexes] + [label for label, _, _ in specs]
        result: list[list[Any]] = []
        for key, members in groups.items():
            values: list[Any] = list(key)
            for _, function, index in specs:
                values.append(_aggregate_value(function, index, members, decimal))
            result.append(values)
        return columns, result

    @staticmethod
    def _order(
        columns: list[str], rows: list[list[Any]], order_by: Sequence[str] | None, decimal: str
    ) -> list[list[Any]]:
        if not order_by:
            return rows
        lookup = {_key(column): index for index, column in enumerate(columns)}
        lookup.update({column.replace(" ", ""): i for i, column in enumerate(columns)})
        ordered = list(rows)
        for term in reversed(order_by):
            descending = term.startswith("-")
            name = term.lstrip("-+").strip()
            index = lookup.get(name.replace(" ", ""), lookup.get(_key(name)))
            if index is None:
                raise ToolException(f"cannot order by {name!r} (columns: {', '.join(columns)})")
            ordered.sort(key=_column_key(index, decimal), reverse=descending)
        return ordered

    def write_csv(self, args: WriteCsvArgs) -> str:
        path = self._file(args.path, must_exist=False)
        exists = path.exists()
        if exists and args.mode == "create":
            raise ToolException(f"{args.path!r} exists; use mode 'overwrite' or 'append'")
        width = len(args.columns)
        for number, row in enumerate(args.rows, start=1):
            if len(row) != width:
                raise ToolException(f"row {number} has {len(row)} values, expected {width}")
        appending = args.mode == "append" and exists
        delimiter = args.delimiter or ","
        explicit = self.encoding.lower().replace("_", "-")
        codec = "utf-8" if explicit in ("auto", "utf-8-sig") else self.encoding
        if appending:
            table, close = self._table(path, args.delimiter)
            close()
            if table.columns != args.columns:
                raise ToolException(
                    f"the columns do not match the file's header ({', '.join(table.columns)})"
                )
            delimiter = table.meta["delimiter"]
            found = str(table.meta["encoding"])
            codec = "utf-8" if found.startswith("utf-8") else found
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=delimiter)
        if not appending:
            writer.writerow([self._cell(value) for value in args.columns])
        writer.writerows([[self._cell(value) for value in row] for row in args.rows])
        # A byte-order mark only starts a new file; it would land in the middle of an appended one.
        bom = codecs.BOM_UTF8 if args.excel and not appending and codec == "utf-8" else b""
        try:
            data = bom + buffer.getvalue().encode(codec)
        except UnicodeEncodeError as exc:
            raise ToolException(f"a value cannot be written as {codec}: {exc.reason}") from None
        size = path.stat().st_size if appending else 0
        if size and not _ends_with_newline(path):
            data = writer.dialect.lineterminator.encode(codec) + data
        if size + len(data) > self.max_file_bytes:
            raise ToolException(f"the file would exceed the limit of {self.max_file_bytes} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:  # "xb" refuses a file that appeared since the check above
            with path.open({"append": "ab", "overwrite": "wb"}.get(args.mode, "xb")) as handle:
                handle.write(data)
        except FileExistsError:
            raise ToolException(f"{args.path!r} exists; use mode 'overwrite' or 'append'") from None
        assert self.sandbox is not None
        return f"Wrote {len(args.rows)} rows to {self.sandbox.relative(path)}."

    def _cell(self, value: _Cell) -> str:
        text = "" if value is None else str(value)
        if self.escape_formulas and text.startswith(_FORMULA_START) and as_number(text) is None:
            return "'" + text  # keep spreadsheets from running it as a formula
        return text

    def _workbook(
        self, path_text: str, sheet: str | None, header_row: int, stop: int | None = None
    ) -> tuple[_Table, list[str], str]:
        """The sheet as a table (rows read up front, at most `stop` of them: the file is then
        read no further and `has_more` is set), the sheet names and the chosen sheet."""

        openpyxl = require_module("openpyxl", "openpyxl", feature="Excel reading")
        path = self._file(path_text)
        check_zip_bounds(path)
        try:
            values = openpyxl.load_workbook(path, read_only=True, data_only=True)
            formulas = openpyxl.load_workbook(path, read_only=True, data_only=False)
        except Exception:
            raise ToolException(
                f"{path_text!r} is not a readable .xlsx workbook; check the file or save it as .xlsx"
            ) from None
        try:
            names = list(values.sheetnames)
            if sheet is not None and sheet not in names:
                raise ToolException(f"no sheet {sheet!r} (sheets: {', '.join(names)})")
            name = sheet or names[0]
            header: list[Any] = []
            rows: list[list[Any]] = []
            uncalculated: list[str] = []  # the first few only
            missing = 0
            more = False
            pairs = itertools.zip_longest(
                values[name].iter_rows(values_only=True),
                formulas[name].iter_rows(values_only=True),
                fillvalue=(),
            )
            for number, (cached, written) in enumerate(pairs, start=1):
                cells = []
                for index, (value, formula) in enumerate(itertools.zip_longest(cached, written)):
                    if value is None and isinstance(formula, str) and formula.startswith("="):
                        missing += 1
                        if len(uncalculated) < _MAX_FORMULA_NOTES:
                            uncalculated.append(f"{_column_letter(index)}{number}: {formula}")
                    cells.append(_excel_value(value))
                if number < header_row:
                    continue
                if number == header_row:
                    header = cells
                elif not all(_blank(cell) for cell in cells):
                    if stop is not None and len(rows) >= stop:
                        more = True
                        break
                    rows.append(cells)
        finally:
            values.close()
            formulas.close()
        meta: dict[str, Any] = {"sheet": name}
        if more:
            meta["has_more"] = True
        if missing:
            meta["formulas_without_values"] = missing
            meta["note"] = (
                f"{missing} formula cell(s) have no saved result and read as null "
                "(the workbook was written by a program that does not calculate formulas; "
                "open and save it in Excel or LibreOffice to store the results), e.g. "
                + "; ".join(uncalculated[:_MAX_FORMULA_NOTES])
            )
        return _sheet_table(header, rows, meta), names, name

    def read_excel(self, args: ReadExcelArgs) -> str:
        limit = self._limit(args.limit)
        table, names, _ = self._workbook(
            args.path, args.sheet, args.header_row, stop=args.offset + limit
        )
        rows = list(table.rows)
        window = rows[args.offset : args.offset + limit]
        counted = {} if table.meta.get("has_more") else {"total_rows": len(rows)}
        return tool_json(
            {
                "sheets": names,
                "columns": table.columns,
                "rows": window,
                **counted,
                "returned": len(window),
                **table.meta,
            }
        )

    def query_excel(self, args: QueryExcelArgs) -> str:
        table, _, _ = self._workbook(args.path, args.sheet, args.header_row)
        return self._query(table, args)

    def fill_excel(self, args: FillExcelArgs) -> str:
        from awa.toolkits._excel_fill import FillRow, fill_workbook

        if self.sources is not None:
            source = self.sources.resolve(args.path, must_exist=True)
            if source.stat().st_size > self.max_file_bytes:
                raise ToolException(f"{args.path!r} is larger than the limit")
        else:
            source = self._file(args.path)
        output = self._file(args.output, must_exist=False)
        summary = fill_workbook(
            source,
            output,
            key_column=args.key_column,
            columns=args.columns,
            rows=[FillRow(r["key"], r["values"], r.get("review")) for r in args.rows],
            sheet=args.sheet,
            header_row=args.header_row,
            review_column=args.review_column,
            append=args.append,
            overwrite=args.overwrite,
        )
        assert self.sandbox is not None
        return tool_json({"output": self.sandbox.relative(output), **summary})

    def json_query(self, args: JsonQueryArgs) -> str:
        if args.path is not None:
            path = self._file(args.path)
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            source = args.path
        else:
            text, source = args.data or "", "data"
        try:
            if args.path is not None and Path(args.path).suffix.lower() in (".jsonl", ".ndjson"):
                data: Any = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                data = json.loads(text)
        except ValueError as exc:
            raise ToolException(f"{source} is not valid JSON: {exc}") from None
        return tool_json(run_json_query(data, args.query))


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _sheet_table(header: list[Any], rows: list[list[Any]], meta: dict[str, Any]) -> _Table:
    """The sheet's rows as a table as wide as its used cells; unnamed columns get their letter."""

    used = [i for row in [header, *rows] for i, cell in enumerate(row) if not _blank(cell)]
    width = max(used, default=-1) + 1
    columns = [
        str(cell).strip() if not _blank(cell) else _column_letter(index)
        for index, cell in enumerate((header + [None] * width)[:width])
    ]
    rows = [(row + [None] * width)[:width] for row in rows]
    return _Table(columns, iter(rows), meta, rows[:200])


def _excel_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time() else value.isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)


def _aggregate_value(function: str, index: int | None, rows: list[list[Any]], decimal: str) -> Any:
    if index is None:
        return len(rows)
    cells = [row[index] if index < len(row) else "" for row in rows]
    present = [cell for cell in cells if not _blank(cell)]
    if function == "count":
        return len(present)
    if function == "count_distinct":
        return len({str(cell) for cell in present})
    numbers = [number for cell in present if (number := as_number(cell, decimal)) is not None]
    if function in ("min", "max") and not numbers:
        texts = [str(cell) for cell in present]
        if not texts:
            return None
        return min(texts) if function == "min" else max(texts)
    if not numbers:
        return None
    if function == "sum":
        return _tidy(sum(numbers))
    if function == "avg":
        return _tidy(sum(numbers) / len(numbers))
    return _tidy(min(numbers) if function == "min" else max(numbers))


def _tidy(number: float | int) -> float | int:
    if isinstance(number, float):
        return int(number) if number.is_integer() else round(number, 10)
    return number


def _column_key(index: int, decimal: str) -> Callable[[list[Any]], tuple[int, Any]]:
    def key(row: list[Any]) -> tuple[int, Any]:
        return _sort_key(row[index], decimal)

    return key


def _sort_key(value: Any, decimal: str = ".") -> tuple[int, Any]:
    number = as_number(value, decimal)
    if number is not None:
        return (0, number)
    if _blank(value):
        return (2, "")
    return (1, str(value).lower())


def data_toolkit(
    root: str | os.PathLike[str] | None = None,
    *,
    include: Sequence[str] | None = None,
    requires_approval: bool = True,
    max_rows: int = 500,
    max_file_bytes: int = 50_000_000,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    encoding: str = "auto",
    escape_formulas: bool = True,
    allow_hidden: bool = False,
    frame_output: bool = True,
    source_root: str | os.PathLike[str] | None = None,
) -> list[BaseTool]:
    """Build the CSV, Excel and JSON tools.

    | Tool | Does | Approval |
    | --- | --- | --- |
    | `read_csv` | returns columns, a window of rows, the encoding and delimiter | no |
    | `query_csv` | filters, selects, groups, aggregates and sorts rows | no |
    | `write_csv` | writes or appends rows (formula cells escaped; `;` and BOM for Excel) | yes |
    | `read_excel` | reads a sheet of an `.xlsx` workbook (`files` extra) | no |
    | `query_excel` | `query_csv` for a sheet of an `.xlsx` workbook (`files` extra) | no |
    | `json_query` | extracts values from JSON text or a `.json`/`.jsonl` file | no |
    | `fill_excel` | adds columns to a sheet and fills them per row matched by a key, saved as a new workbook (`files` extra) | yes |

    Numbers in text cells may carry thousands separators, currency signs, a percent sign
    (`12,5 %` is 12.5) or parentheses for negatives; the decimal separator of ambiguous values
    (`1.234`) is detected per file, or set with `decimal`. Excel dates read as ISO dates; formula
    cells without a saved result read as null and are listed in a `note`.

    Args:
        root: Directory the file tools work in. Without it only `json_query` (on text) is built.
        include: Build only these tools (names from `DATA_TOOLS`).
        encoding: Text encoding of CSV files; `auto` reads UTF-8 (with or without byte-order
            mark), else Windows-1252, and writes UTF-8.
        escape_formulas: Prefix cells that start with `=`, `+`, `-` or `@` with `'` in
            `write_csv` (CSV injection).
        frame_output: Label file contents as external data.
        source_root: Where `fill_excel` reads its source workbook (default: `root`); the output
            is always written in `root`.

    Raises:
        ConfigurationError: If `root` is not a directory, a limit is not positive, or `include`
            names an unknown tool or a file tool without a `root`.

    Example:
        ```python
        readers = data_toolkit("./data", include=["read_csv", "query_csv"])
        ```
    """

    positive(max_rows, "max_rows")
    positive(max_file_bytes, "max_file_bytes")
    if encoding != "auto":
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise ConfigurationError(f"unknown encoding {encoding!r}") from None
    available = DATA_TOOLS if root is not None else ("json_query",)
    if root is None and include is not None:
        missing = [name for name in include if name in _FILE_TOOLS]
        if missing:
            raise ConfigurationError(f"{', '.join(missing)} need(s) a root directory")
    names = selected(available, include, "data")
    impl = _DataTools(
        Sandbox(root, allow_hidden=allow_hidden) if root is not None else None,
        max_rows=max_rows,
        max_file_bytes=max_file_bytes,
        encoding=encoding,
        escape_formulas=escape_formulas,
        sources=Sandbox(source_root, allow_hidden=allow_hidden) if source_root else None,
    )
    query_help = (
        "e.g. where=\"region == 'EU' and amount > 100\", group_by=['product'], "
        "aggregates=['count()', 'sum(amount)'], order_by=['-sum(amount)']. Numbers such as "
        "'1.234,56 €' or '12,5 %' compare and add up as numbers."
    )
    specs: ToolSpecs = {
        "read_csv": (
            impl.read_csv,
            ReadCsvArgs,
            "Read a CSV file: returns its columns, a window of rows (offset/limit), the "
            "total row count and the detected encoding and delimiter. Use it to see what a "
            "table contains before querying it.",
        ),
        "query_csv": (
            impl.query_csv,
            QueryCsvArgs,
            f"Filter, select, group, aggregate and sort the rows of a CSV file, {query_help} "
            "Use it instead of reading whole files to answer questions about tabular data.",
        ),
        "write_csv": (
            impl.write_csv,
            WriteCsvArgs,
            "Write rows to a CSV file (create, overwrite or append); for Excel in German or "
            "other European settings use delimiter ';' and excel=true. Needs human approval.",
        ),
        "read_excel": (
            impl.read_excel,
            ReadExcelArgs,
            "Read a sheet of an Excel workbook (.xlsx): returns sheet names, columns and a "
            "window of rows; header_row picks the row with the column names.",
        ),
        "query_excel": (
            impl.query_excel,
            QueryExcelArgs,
            f"Filter, select, group, aggregate and sort the rows of an Excel sheet (.xlsx), "
            f"{query_help}",
        ),
        "fill_excel": (
            impl.fill_excel,
            FillExcelArgs,
            "Add columns to an Excel sheet and fill them row by row, each row found by its key "
            "(e.g. invoice number; case, spaces and punctuation ignored), and save the result "
            "as a new workbook: formatting and other sheets stay, source cells are never "
            "changed, keys found in no row are listed and kept with their values on the sheet "
            "'Unmatched keys' (the separate list of unmatched items). Use it to "
            "enrich a large register with facts read from documents: collect the facts first, "
            "then call it once with all rows. Needs human approval.",
        ),
        "json_query": (
            impl.json_query,
            JsonQueryArgs,
            "Extract values from a JSON document (data, or a .json/.jsonl file as path) with "
            "a path query: fields ('a.b'), indexes ('items[0]'), projections ('items[*].id'), "
            "filters ('items[?price > 10].name') and a final function ('| length', '| sum'). "
            "Use it to pick fields or totals out of an API response or a JSON file instead of "
            "reading the whole document. Returns the matching values as JSON.",
        ),
    }
    data_root = {DATA_ROOT_METADATA_KEY: str(Path(root).expanduser().resolve())} if root else None
    readers = (*_READERS, "json_query") if root is not None else _READERS
    return toolkit_tools(
        "data",
        names,
        specs,
        requires_approval=requires_approval,
        framed=readers if frame_output else (),
        metadata=data_root,
        max_output_chars=max_output_chars,
    )


__all__ = ["DATA_TOOLS", "data_toolkit", "detect_encoding", "sniff_delimiter"]
