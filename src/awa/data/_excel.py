"""Excel workbooks (`xlsx`): one table per block of rows; merged cells, hidden content and
formulas come from a pattern pass over the sheet XML (no XML parser, so no entity expansion)."""

from __future__ import annotations

import codecs
import html
import importlib
import posixpath
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from awa._internal.sandbox import check_zip_bounds
from awa.data._model import (
    FileProfile,
    Flag,
    ListValidation,
    NumberFormat,
    SheetInfo,
    TableProfile,
    XlsxInfo,
    make_flag,
)
from awa.data._parse import ParseContext, ParseTimeout
from awa.data._sampling import RowSample
from awa.data._stats import blank, build_table, find_header, letters, span

KINDS = ("xlsx",)

_OLE2 = bytes.fromhex("d0cf11e0a1b11ae1")  # an old .xls or an encrypted workbook
_CHUNK = 1 << 20
_MAX_PART = 50_000_000  # the largest workbook part read whole (workbook.xml, comments)
_MAX_LISTED = 100  # merged ranges and defined names kept per sheet / workbook
_MAX_EXAMPLES = 5
_ROOT_ELEMENT = re.compile(rb"<[A-Za-z_]")  # a DOCTYPE can only come before the root element
_MAX_COLUMN = 16384
_NOTE_ROWS = 3  # a block of up to this many one-cell rows is a title or a note
_FORMAT_ROWS = 30  # rows of a table whose number formats are looked at
_FORMAT_ROW_LIMIT = 20_000  # tables that start lower are not looked at (a re-read of the sheet)
_ATTR = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')
_SHEET = re.compile(r"<sheet\b([^<>]*?)/?>")
_RELATIONSHIP = re.compile(r"<Relationship\b([^<>]*?)/?>")
_DEFINED_NAME = re.compile(r"<definedName\b([^<>]*)>")
_ROW_HIDDEN = re.compile(r'<row\b[^<>]*\bhidden="(?:1|true)"')
_FORMULA = re.compile(
    r"<c\b([^<>]*)>\s*<f\b([^<>]*?)(?:/>|>([^<]*)</f>)(\s*<v>[^<]*</v>|\s*<v\s*/>)?"
)
_MERGE = re.compile(r'<mergeCell\b[^<>]*\bref="([^"<>]+)"')
_COL = re.compile(r"<col\b([^<>]*)>")
_VALIDATION = re.compile(r"<dataValidation\b([^<>]*)>")
_VALIDATION_END = "</dataValidation>"
_FORMULA1 = re.compile(r"<formula1>([^<]*)</formula1>")
_SUM = re.compile(r"(?:SUM|SUBTOTAL|AGGREGATE)\(", re.IGNORECASE)
_CACHED_VALUE = re.compile(r"<v>[^<]+</v>")
_COLUMN_LETTERS = re.compile(r"[A-Za-z]+")
_DATE1904 = re.compile(r'<workbookPr\b[^>]*\bdate1904="(?:1|true)"')
_PREFIXED = re.compile(r"<\w+:sheetData\b")
_CURRENCY = re.compile(r"\[\$([^\]-]*)[^\]]*\]|[€$£¥]|\"?(EUR|USD|GBP|CHF)\"?")


class _Sheet(NamedTuple):
    name: str
    visible: bool
    part: str


@dataclass
class _Book:
    sheets: list[_Sheet] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    date1904: bool = False
    macros: bool = False


@dataclass
class _Facts:
    """What the worksheet XML says besides the cell values."""

    merged: list[str] = field(default_factory=list)
    merged_count: int = 0
    hidden_rows: int = 0
    hidden_columns: int = 0
    formulas: int = 0
    uncached: int = 0
    examples: list[str] = field(default_factory=list)
    sum_rows: set[int] = field(default_factory=set)
    validations: list[ListValidation] = field(default_factory=list)
    comments: int = 0
    prefixed: bool = False


@dataclass
class _SheetResult:
    info: SheetInfo
    tables: list[TableProfile] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    stopped: bool = False


def profile_xlsx(path: Path, ctx: ParseContext) -> FileProfile:
    """Profile an Excel workbook: one table per block of rows, or an error flag."""

    if ctx.library_missing("openpyxl"):
        return ctx.profile
    openpyxl = importlib.import_module("openpyxl")  # a driver: loaded only when a workbook is read
    invalid = importlib.import_module("openpyxl.utils.exceptions").InvalidFileException
    from awa._internal.upstream.tools import ToolException

    with path.open("rb") as handle:
        if handle.read(8) == _OLE2:
            return ctx.fail(
                "DF-E-ENCRYPTED",
                "the file is password-protected or an old binary .xls workbook; awa reads .xlsx only (remove the password or save as .xlsx)",
            )
    try:
        check_zip_bounds(path, max_ratio=ctx.limits.max_zip_ratio)
    except ToolException:
        return ctx.fail(
            "DF-E-ZIP-UNSAFE", "the workbook unpacks to an unreasonable size and is refused"
        )
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return _corrupt(ctx)
    with archive:
        if _declares_entities(archive):
            return ctx.fail(
                "DF-E-ZIP-UNSAFE",
                "the workbook contains XML entity declarations, which Office files never do; refused",
            )
        try:
            book = _workbook(archive)
            workbook = openpyxl.load_workbook(
                path, read_only=True, data_only=True, keep_links=False
            )
        except (zipfile.BadZipFile, KeyError, invalid):
            return _corrupt(ctx)
        except Exception as error:  # the driver raises many kinds for a damaged workbook
            return _corrupt(ctx, type(error).__name__)
        try:
            return _profile_book(ctx, archive, book, workbook)
        finally:
            workbook.close()


def _attrs(tag: str) -> dict[str, str]:
    return {name: html.unescape(value) for name, value in _ATTR.findall(tag)}


def _read_part(archive: zipfile.ZipFile, name: str) -> str:
    info = archive.getinfo(name)
    if info.file_size > _MAX_PART:
        raise zipfile.BadZipFile(f"part {name} is too large")
    return archive.read(name).decode("utf-8", errors="replace")


def _target(base: str, target: str) -> str:
    """The part a relationship points to (`/xl/a.xml` is absolute, others are relative)."""

    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """`{id: (type, target part)}` of the relationships of `part`."""

    folder, name = posixpath.split(part)
    rels = posixpath.join(folder, "_rels", name + ".rels")
    if rels not in archive.namelist():
        return {}
    found = {}
    for tag in _RELATIONSHIP.findall(_read_part(archive, rels)):
        attrs = _attrs(tag)
        if "Id" in attrs and "Target" in attrs:
            found[attrs["Id"]] = (attrs.get("Type", ""), _target(part, attrs["Target"]))
    return found


def _workbook(archive: zipfile.ZipFile) -> _Book:
    text = _read_part(archive, "xl/workbook.xml")
    rels = _relationships(archive, "xl/workbook.xml")
    book = _Book(macros="xl/vbaProject.bin" in archive.namelist())
    book.date1904 = _DATE1904.search(text) is not None
    for tag in _SHEET.findall(text):
        attrs = _attrs(tag)
        kind, part = rels.get(attrs.get("r:id", ""), ("", ""))
        if kind.endswith("/worksheet") and "name" in attrs:
            book.sheets.append(
                _Sheet(attrs["name"], attrs.get("state", "visible") == "visible", part)
            )
    names = {_attrs(tag).get("name", "") for tag in _DEFINED_NAME.findall(text)}
    book.names = sorted(n for n in names if n and not n.startswith("_xlnm."))[:_MAX_LISTED]
    return book


def _scan_sheet(archive: zipfile.ZipFile, part: str, ctx: ParseContext) -> _Facts:
    """Merged ranges, hidden rows and columns, formulas, dropdown lists, comments of one sheet."""

    facts = _Facts()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    first = True
    with archive.open(part) as handle:
        for raw in iter(lambda: handle.read(_CHUNK), b""):
            ctx.check_deadline()
            buffer += decoder.decode(raw)
            if first:
                facts.prefixed = bool(_PREFIXED.search(buffer))
                first = False
            cut = buffer.rfind("</row>")
            if cut != -1:
                _scan_text(buffer[: cut + 6], facts)
                buffer = buffer[cut + 6 :]
    _scan_text(buffer + decoder.decode(b"", final=True), facts)
    for rel_type, target in _relationships(archive, part).values():
        small = target in archive.namelist() and archive.getinfo(target).file_size <= _MAX_PART
        if rel_type.endswith("/comments") and small:
            facts.comments += archive.read(target).count(b"<comment ")
    return facts


def _scan_text(text: str, facts: _Facts) -> None:
    facts.hidden_rows += len(_ROW_HIDDEN.findall(text))
    for tag in _COL.findall(text):
        attrs = _attrs(tag)
        if attrs.get("hidden") in ("1", "true"):
            low, high = _column_number(attrs.get("min")), _column_number(attrs.get("max"))
            if low is not None and high is not None and high >= low:
                facts.hidden_columns += high - low + 1
    for match in _FORMULA.finditer(text):
        facts.formulas += 1
        cell, formula = _attrs(match[1]).get("r", ""), html.unescape(match[3] or "")
        cached = _CACHED_VALUE.search(match[4] or "") is not None
        if not cached:
            facts.uncached += 1
            if len(facts.examples) < _MAX_EXAMPLES:
                facts.examples.append(f"{cell}: ={formula[:40]}")
        if _SUM.match(formula) and (row := _COLUMN_LETTERS.sub("", cell)).isdigit():
            facts.sum_rows.add(int(row))
    for ref in _MERGE.findall(text):
        facts.merged_count += 1
        if len(facts.merged) < _MAX_LISTED:
            facts.merged.append(ref)
    for attributes, body in _validations(text):
        attrs = _attrs(attributes)
        found = _FORMULA1.search(body)
        if attrs.get("type") != "list" or found is None:
            continue
        literal = html.unescape(found[1]).strip()
        if len(literal) > 1 and literal[0] == literal[-1] == '"':  # a typed-in list, not a range
            values = [v.strip() for v in literal[1:-1].split(",") if v.strip()]
            facts.validations.append(
                ListValidation(range=attrs.get("sqref", ""), values=values[:50])
            )


def _column_number(text: str | None) -> int | None:
    """A `<col>` bound (1 to 16384), or `None` when it is missing or not a number."""

    if text is None or not text.strip().isdigit():
        return None
    return min(max(int(text), 1), _MAX_COLUMN)


def _validations(text: str) -> Iterator[tuple[str, str]]:
    """Attributes and body of each `<dataValidation>`, found in one pass (no backtracking)."""

    position = 0
    while (start := _VALIDATION.search(text, position)) is not None:
        end = text.find(_VALIDATION_END, start.end())
        if end < 0:
            return
        yield start[1], text[start.end() : end]
        position = end + len(_VALIDATION_END)


def _read_blocks(ws: Any, ctx: ParseContext) -> tuple[list[RowSample], bool]:
    """The non-blank rows of a sheet as blocks of consecutive rows, and whether time ran out."""

    limits = ctx.limits
    blocks: list[RowSample] = []
    current: RowSample | None = None
    previous = 0
    ws.reset_dimensions()  # the declared used range is not trusted
    try:
        for number, row in enumerate(ws.iter_rows(values_only=True), 1):
            if number % 1000 == 0:
                ctx.check_deadline()
            if all(blank(cell) for cell in row):
                continue
            if current is None or number != previous + 1:
                current = RowSample(limits.max_rows_exact, limits.sample_rows)
                blocks.append(current)
            current.add(number, list(row))
            previous = number
    except ParseTimeout:
        if not blocks:
            raise
        return blocks, True
    return blocks, False


def _tables_of(blocks: list[RowSample]) -> tuple[list[RowSample], list[int]]:
    """Drop one-line titles, join blocks that continue the table above; the dropped rows."""

    tables: list[RowSample] = []
    notes: list[int] = []
    widest = max((max((span(c) for _, c in b.rows), default=0) for b in blocks), default=0)
    for block in blocks:
        rows = block.rows
        if widest > 1 and block.total <= _NOTE_ROWS and all(span(c) <= 1 for _, c in rows):
            notes.extend(n for n, _ in rows)  # a title or a note above or beside a wider table
        elif tables and not block.sampled and find_header([c for _, c in rows]).row is None:
            for number, cells in rows:  # no header of its own: blank rows inside one table
                tables[-1].add(number, cells)
        else:
            tables.append(block)
    return tables, notes


def _offset(block: RowSample) -> int:
    """The first column that holds anything (columns before it are blank in every row)."""

    return min(
        (
            next((j for j, cell in enumerate(cells) if not blank(cell)), len(cells))
            for _, cells in block.rows
        ),
        default=0,
    )


def _format_facts(fmt: str) -> tuple[bool, str | None]:
    """Whether a number format shows a percent, and its currency sign or code."""

    found = _CURRENCY.search(fmt)
    if found is None:
        currency = None
    elif found[0].startswith("[$"):  # `[$€-407]`; `[$-409]` names a locale only
        currency = found[1]
    else:
        currency = found[2] or found[0]
    return "%" in fmt, (currency or None)


def _number_formats(ws: Any, table: TableProfile, offset: int) -> None:
    """Percent and currency of typed numbers come from the number format of their cells."""

    numeric = [c for c in table.columns if c.type in ("integer", "decimal") and c.format is None]
    if not numeric or table.data_start_row > _FORMAT_ROW_LIMIT or table.rows == 0:
        return
    first, last = table.data_start_row, table.data_start_row + _FORMAT_ROWS
    seen: dict[int, dict[str, int]] = {}
    for row in ws.iter_rows(min_row=first, max_row=last):
        for cell in row:
            fmt = getattr(cell, "number_format", None)
            if fmt and fmt != "General" and isinstance(cell.value, int | float):
                formats = seen.setdefault(cell.column - 1 - offset, {})
                formats[fmt] = formats.get(fmt, 0) + 1
    for column in numeric:
        formats = seen.get(column.index, {})
        if not formats:
            continue
        percent, currency = _format_facts(max(formats, key=lambda f: (formats[f], f)))
        if percent or currency:
            column.format = NumberFormat(percent=percent, currency=currency)


def _stored_as_text(table: TableProfile, rows: list[tuple[int, list[Any]]]) -> list[Flag]:
    """Flags for columns whose numbers or dates are text cells (looked at in the first rows)."""

    flags = []
    for column in table.columns:
        if column.type not in ("integer", "decimal", "date", "datetime"):
            continue
        cells = [
            cells[column.index]
            for number, cells in rows[:200]
            if number >= table.data_start_row and column.index < len(cells)
        ]
        cells = [cell for cell in cells if not blank(cell)]
        text = sum(cell.__class__ is str for cell in cells)
        if not cells or text * 2 <= len(cells):
            continue
        what = "dates" if column.type in ("date", "datetime") else "numbers"
        code = "DF-W-DATES-AS-TEXT" if what == "dates" else "DF-W-NUMBERS-AS-TEXT"
        column.flags.append(code)
        flags.append(
            make_flag(
                code,
                f"{text} of the first {len(cells)} {what} are stored as text, not as {what}: the data tools return text for them (convert them in the workbook or parse them in the process)",
                f"{table.id}.{column.key}",
            )
        )
    return flags


def _used_range(blocks: list[RowSample]) -> tuple[str, int, int] | None:
    """The bounding box of every non-blank cell of the sheet as `A1:F66`, its rows and columns."""

    if not blocks:
        return None
    top = min(b.rows[0][0] for b in blocks)
    bottom = max(b.tail[-1][0] for b in blocks)
    left = min(_offset(b) for b in blocks)
    right = max(max(span(cells) for _, cells in b.rows) for b in blocks)
    return f"{letters(left)}{top}:{letters(right - 1)}{bottom}", bottom - top + 1, right - left


def _table_of(
    ctx: ParseContext, ws: Any, sheet: str, block: RowSample, number: int, sum_rows: set[int]
) -> tuple[TableProfile, list[Flag]]:
    offset = _offset(block)
    if offset:
        block.drop_leading(offset)
    table, flags = build_table(
        block,
        table_id=ctx.table_id(number),
        max_sample_rows=ctx.privacy.max_sample_rows,
        cell_chars=ctx.limits.max_chars_per_cell,
        sheet=sheet,
        unnamed=lambda index: letters(offset + index),
        sum_rows=frozenset(sum_rows),
    )
    first = table.header_row or table.data_start_row
    last = block.tail[-1][0]
    table.range = f"{letters(offset)}{first}:{letters(offset + len(table.columns) - 1)}{last}"
    flags += _stored_as_text(table, block.rows)
    _number_formats(ws, table, offset)
    if block.sampled:
        message = f"{table.rows} rows counted; types, distinct values, minimum and maximum come from a sample of {table.rows_seen} rows"
        flags.append(make_flag("DF-W-SAMPLED", message, table.id))
    return table, flags


def _profile_sheet(
    ctx: ParseContext, archive: zipfile.ZipFile, workbook: Any, sheet: _Sheet, first_number: int
) -> _SheetResult:
    """One sheet: structure facts, then every block of rows as a table."""

    result = _SheetResult(SheetInfo(name=sheet.name, visible=sheet.visible))
    facts = _scan_sheet(archive, sheet.part, ctx)
    ws = workbook[sheet.name]
    blocks, result.stopped = _read_blocks(ws, ctx)
    _sheet_facts(result, facts, sheet, ctx.file_id)
    used = _used_range(blocks)
    if used:
        result.info.used_range, result.info.rows, result.info.columns = used
    tables, notes = _tables_of(blocks)
    if notes:
        shown = ", ".join(map(str, notes[:10]))
        message = f"sheet {sheet.name!r}: row(s) {shown} hold a single title or note, not a table"
        result.flags.append(make_flag("DF-I-NOTE-ROWS", message, ctx.file_id))
    for block in tables:
        number = first_number + len(result.tables)
        table, flags = _table_of(ctx, ws, sheet.name, block, number, facts.sum_rows)
        result.tables.append(table)
        result.flags += flags
    if len(tables) > 1:
        ranges = ", ".join(t.range or "" for t in result.tables)
        message = f"sheet {sheet.name!r} holds {len(tables)} tables ({ranges}); each is profiled on its own"
        result.flags.append(make_flag("DF-W-MULTIPLE-TABLES", message, result.tables[0].id))
    result.info.tables = [t.id for t in result.tables]
    return result


def _sheet_facts(result: _SheetResult, facts: _Facts, sheet: _Sheet, where: str) -> None:
    info, flags, name = result.info, result.flags, sheet.name
    info.merged_ranges, info.hidden_rows = facts.merged, facts.hidden_rows
    info.hidden_columns, info.formulas = facts.hidden_columns, facts.formulas
    info.formulas_without_cache, info.comments = facts.uncached, facts.comments
    info.validations = facts.validations
    if not sheet.visible:
        message = f"sheet {name!r} is hidden (the data tools still read it)"
        flags.append(make_flag("DF-W-HIDDEN-SHEET", message, where))
    if facts.hidden_rows or facts.hidden_columns:
        message = f"sheet {name!r} has {facts.hidden_rows} hidden row(s) and {facts.hidden_columns} hidden column(s); the data tools read them like any other"
        flags.append(make_flag("DF-W-HIDDEN-CONTENT", message, where))
    if facts.merged_count:
        message = f"sheet {name!r} has {facts.merged_count} merged range(s) (first: {facts.merged[0]}); only the top-left cell of a merged range holds a value"
        flags.append(make_flag("DF-W-MERGED-CELLS", message, where))
    if facts.uncached:
        examples = "; ".join(facts.examples[:3])
        message = f"sheet {name!r}: {facts.uncached} of {facts.formulas} formulas have no saved result and read as empty (e.g. {examples}); open and save the workbook in Excel or LibreOffice to store the results"
        flags.append(make_flag("DF-W-FORMULA-NO-CACHE", message, where))
    if facts.prefixed:
        message = f"sheet {name!r} uses a namespace prefix in its XML: merged cells, hidden rows, formulas and dropdown lists were not inspected"
        flags.append(make_flag("DF-W-STRUCTURE-UNKNOWN", message, where))


def _corrupt(ctx: ParseContext, reason: str = "") -> FileProfile:
    detail = f" ({reason})" if reason else ""
    return ctx.fail("DF-E-PARSE-FAILED", f"the file is not a valid .xlsx workbook{detail}")


def _declares_entities(archive: zipfile.ZipFile) -> bool:
    """Whether any XML part declares a DOCTYPE or ENTITY, read in chunks up to its root element."""

    for info in archive.infolist():
        if info.filename.endswith((".xml", ".rels", ".vml")) and _prolog_declares(archive, info):
            return True
    return False


def _prolog_declares(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    seen = b""
    with archive.open(info) as handle:
        while chunk := handle.read(_CHUNK):
            if not seen and (chunk.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in chunk[:4]):
                return True  # UTF-16 XML: Excel never writes it, and it hides a declaration here
            seen = seen[-16:] + chunk
            root = _ROOT_ELEMENT.search(seen)
            prolog = seen if root is None else seen[: root.start()]
            if b"<!DOCTYPE" in prolog or b"<!ENTITY" in prolog:
                return True
            if root is not None:
                return False
    return False


def _profile_book(
    ctx: ParseContext, archive: zipfile.ZipFile, book: _Book, workbook: Any
) -> FileProfile:
    profile = ctx.profile
    sheets: list[SheetInfo] = []
    stopped = False
    for index, sheet in enumerate(book.sheets):
        if index >= ctx.limits.max_sheets:
            profile.status = "partial"
            ctx.flag(
                "DF-W-SHEET-LIMIT",
                f"only the first {ctx.limits.max_sheets} of {len(book.sheets)} sheets are profiled",
            )
            break
        ctx.progress(f"sheet {index + 1} of {len(book.sheets)}")
        try:
            result = _profile_sheet(ctx, archive, workbook, sheet, len(profile.tables) + 1)
        except ParseTimeout:
            if not sheets:
                raise
            stopped = True
            break
        sheets.append(result.info)
        if any(table.sampled for table in result.tables):
            profile.status = "partial"
        profile.tables += result.tables
        profile.flags += result.flags
        if result.stopped:
            stopped = True
            break
    if stopped:
        profile.status = "partial"
        ctx.flag(
            "DF-W-TIME-LIMIT",
            "reading stopped at the time limit: the last sheet may be cut and later sheets are not profiled",
        )
    if not profile.tables:
        return ctx.fail("DF-E-EMPTY", "the workbook has no cell with a value")
    profile.xlsx = XlsxInfo(
        sheets=sheets,
        date_system="1904" if book.date1904 else "1900",
        defined_names=book.names,
        macros=book.macros,
    )
    if book.macros:
        ctx.flag("DF-W-MACROS", "the workbook carries a macro project; awa never runs it")
    if book.date1904:
        ctx.flag(
            "DF-W-DATE-SYSTEM-1904",
            "the workbook counts dates from 1904 (an old Mac setting): serial numbers read by another tool are 4 years off; the typed dates here are already right",
        )
    return profile
