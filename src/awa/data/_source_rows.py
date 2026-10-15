"""`LocalFileReader`: the `DataReader` that rereads profiled files from their source paths."""

from __future__ import annotations

import csv
import datetime
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from awa._internal.install import require_module
from awa._internal.sandbox import check_zip_bounds, file_encoding
from awa.data._errors import DataError, RowLimitError
from awa.data._model import Cell, FileProfile, TableProfile
from awa.data._sampling import numbered_records
from awa.data.ports import DataReader

MAX_TEXT_CHARS = 20_000_000
"""Most characters of one document read back as text."""

MAX_PDF_PAGES = 200
"""Most PDF pages read back as text."""

MAX_JSON_BYTES = 100_000_000
"""Largest JSON file whose records are read back."""

MAX_MESSAGES = 2_000
"""Most messages of a mailbox read back as text."""


def table_rows(file: FileProfile, table: TableProfile, *, max_rows: int) -> Iterator[list[Cell]]:
    """The data rows of `table`, one cell per column, without header, blank and totals rows.

    Raises:
        DataError: If the source file is missing, unreadable, or of a kind without rows.
        RowLimitError: While iterating, when the table has more than `max_rows` rows.
    """

    path = _source(file)
    if file.kind == "csv":
        rows = _csv_rows(path, file, table)
    elif file.kind == "xlsx":
        rows = _xlsx_rows(path, file, table)
    elif file.kind == "sqlite":
        rows = _sqlite_rows(path, table, max_rows)
    elif file.kind == "json":
        rows = _json_rows(path, file, table)
    else:
        raise DataError(f"{file.path} is a {file.kind} file; it has no rows")
    return _limited(rows, max_rows, file.path)


def document_text(file: FileProfile) -> tuple[str, bool]:
    """The text of a PDF, Word, JSON, text or mail file and whether it is the whole text.

    Raises:
        DataError: If the file is missing, unreadable or of a kind without text.
    """

    path = _source(file)
    try:
        if file.kind == "pdf":
            from awa.toolkits.documents import pdf_text

            text = "\n".join(pdf_text(path, max_pages=MAX_PDF_PAGES, max_chars=MAX_TEXT_CHARS))
            if file.pdf and file.pdf.pages > MAX_PDF_PAGES:
                return text[:MAX_TEXT_CHARS], False
        elif file.kind == "docx":
            from awa.toolkits.documents import docx_text

            text = docx_text(path)
        elif file.kind == "mail":
            text, whole = _mail_text(path)
            return text[:MAX_TEXT_CHARS], whole and len(text) <= MAX_TEXT_CHARS
        elif file.kind in ("json", "text"):
            with path.open(encoding=_encoding(path, file), errors="replace") as handle:
                text = handle.read(MAX_TEXT_CHARS + 1)
        else:
            raise DataError(f"{file.path} is a {file.kind} file; it has no text")
    except DataError:
        raise
    except Exception as error:  # a damaged or hostile document: the caller treats it as unread
        raise DataError(f"{file.path} could not be read again ({type(error).__name__})") from error
    return text[:MAX_TEXT_CHARS], len(text) <= MAX_TEXT_CHARS


class LocalFileReader(DataReader):
    """The `DataReader` of local files (CSV, Excel, SQLite rows; PDF, Word, JSON, text, mail text)."""

    def rows(
        self, file: FileProfile, table: TableProfile, *, max_rows: int
    ) -> Iterator[list[Cell]]:
        """See `DataReader.rows`."""

        return table_rows(file, table, max_rows=max_rows)

    def text(self, file: FileProfile) -> tuple[str, bool]:
        """See `DataReader.text`."""

        return document_text(file)


def _encoding(path: Path, file: FileProfile) -> str:
    """The encoding the profile found, else the one the file shows."""

    return (file.text.encoding if file.text else None) or file_encoding(path)[0]


def _source(file: FileProfile) -> Path:
    path = Path(file.source) if file.source else None
    if path is None or not path.is_file():
        raise DataError(f"{file.path} cannot be read again: its source file is not available")
    return path


def _limited(rows: Iterator[list[Cell]], max_rows: int, name: str) -> Iterator[list[Cell]]:
    for count, row in enumerate(rows, start=1):
        if count > max_rows:
            raise RowLimitError(f"{name} has more than {max_rows} rows")
        yield row


def _blank(row: list[Cell]) -> bool:
    return all(cell is None or (isinstance(cell, str) and not cell.strip()) for cell in row)


def _fit(row: list[Cell], width: int) -> list[Cell]:
    return [*row[:width], *([None] * (width - len(row)))] if len(row) != width else row


def _csv_rows(path: Path, file: FileProfile, table: TableProfile) -> Iterator[list[Cell]]:
    """The data rows of a CSV table, skipped by the line numbers the profile used."""

    info = file.text
    encoding = _encoding(path, file)
    skipped = set(table.totals_rows)
    width = len(table.columns)
    try:
        with path.open(encoding=encoding, newline="", errors="replace") as handle:
            for start, _, record in numbered_records(handle, info.delimiter if info else None):
                if start < table.data_start_row or start in skipped:
                    continue
                row: list[Cell] = list(record)
                if not _blank(row):
                    yield _fit(row, width)
    except (csv.Error, OSError) as error:
        raise DataError(f"{file.path} could not be read again ({type(error).__name__})") from error


def _xlsx_rows(path: Path, file: FileProfile, table: TableProfile) -> Iterator[list[Cell]]:
    openpyxl = require_module("openpyxl", "openpyxl", feature="Excel reading")
    try:
        check_zip_bounds(path)
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            sheet = workbook[table.sheet] if table.sheet else workbook.worksheets[0]
            sheet.reset_dimensions()  # like the profile: the declared used range is not trusted
            first, last, left, right = table.data_start_row, None, 1, None
            if table.range:
                left, _, right, last = openpyxl.utils.cell.range_boundaries(table.range)
            skipped = set(table.totals_rows)
            width = len(table.columns)
            rows = sheet.iter_rows(
                min_row=first, max_row=last, min_col=left, max_col=right, values_only=True
            )
            for number, record in enumerate(rows, start=first):
                row = [_value(cell) for cell in record]
                if number not in skipped and not _blank(row):
                    yield _fit(row, width)
        finally:
            workbook.close()
    except DataError:
        raise
    except Exception as error:  # a damaged or hostile workbook is unread, never half read
        raise DataError(f"{file.path} could not be read again ({type(error).__name__})") from error


def _value(cell: object) -> Cell:
    if isinstance(cell, datetime.datetime) and cell.time() == datetime.time():
        return cell.date().isoformat()  # a day: written like the profile writes it
    if isinstance(cell, datetime.datetime | datetime.date | datetime.time):
        return cell.isoformat()
    return cell if isinstance(cell, str | int | float | bool) or cell is None else str(cell)


def _json_cell(value: object) -> Cell:
    return (
        json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list) else _value(value)
    )


def _json_records(path: Path, file: FileProfile) -> Iterator[object]:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise DataError(f"{file.path} is too large to read its records again")
    with path.open(encoding=_encoding(path, file), errors="replace") as handle:
        if file.json_doc and file.json_doc.top_level == "jsonl":
            for line in handle:
                if line.strip():
                    yield json.loads(line)
            return
        document = json.load(handle)
    yield from document if isinstance(document, list) else [document]


def _json_rows(path: Path, file: FileProfile, table: TableProfile) -> Iterator[list[Cell]]:
    """The records of a JSON or JSONL file as rows: one cell per column, found by its path."""

    names = [column.name.split(".") for column in table.columns]
    try:
        for record in _json_records(path, file):
            if not isinstance(record, dict):
                continue
            row: list[Cell] = []
            for parts in names:
                value: object = record
                for part in parts:
                    value = value.get(part) if isinstance(value, dict) else None
                row.append(_json_cell(value))
            yield row
    except ValueError as error:  # also json.JSONDecodeError
        raise DataError(f"{file.path} could not be read again ({type(error).__name__})") from error


def _sqlite_rows(path: Path, table: TableProfile, max_rows: int) -> Iterator[list[Cell]]:
    quote = '"{}"'.format
    columns = ", ".join(quote(column.name.replace('"', '""')) for column in table.columns)
    name = quote((table.name or "").replace('"', '""'))
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        cursor = connection.execute(f"SELECT {columns} FROM {name} LIMIT {max_rows + 1}")  # noqa: S608 - quoted profile names
        for record in cursor:
            yield [_value(cell) for cell in record]
    except sqlite3.Error as error:
        raise DataError(f"table {table.name} could not be read ({type(error).__name__})") from error
    finally:
        connection.close()


def _messages(path: Path) -> tuple[list[Any], bool]:
    """The messages of an `.eml` or `.mbox` file, parsed like a mail reader does, and whether all
    of them were taken (a mailbox is cut at `MAX_MESSAGES`)."""

    import email
    import mailbox
    from email import policy

    if path.suffix.lower() != ".mbox":
        with path.open("rb") as handle:
            return [email.message_from_binary_file(handle, policy=policy.default)], True
    box = mailbox.mbox(path, create=False)
    keys = list(box.iterkeys())
    messages = [
        email.message_from_bytes(box.get_bytes(key), policy=policy.default)
        for key in keys[:MAX_MESSAGES]
    ]
    return messages, len(keys) <= MAX_MESSAGES


def _mail_text(path: Path) -> tuple[str, bool]:
    """Headers and text parts of the messages of a mail file, decoded as a mail reader shows
    them, and the text of PDF attachments; `False` when something could not be read (another
    kind of attachment, an undecodable part, more than `MAX_MESSAGES` messages)."""

    messages, whole = _messages(path)
    texts: list[str] = []
    for message in messages:
        texts += [f"{name}: {value}" for name, value in message.items()]
        for part in message.walk():
            if part.is_multipart():
                continue
            kind = part.get_content_type()
            try:
                if kind.startswith("text/"):
                    texts.append(part.get_content())
                elif kind == "application/pdf":
                    from awa.toolkits.documents import pdf_text

                    pages = pdf_text(part.get_payload(decode=True), max_pages=MAX_PDF_PAGES)
                    texts.append("\n".join(pages))
                else:
                    whole = False
            except Exception:  # an undecodable or damaged part stays unseen
                whole = False
    return "\n".join(texts), whole
