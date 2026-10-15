"""A profiled file as a card in plain words: what it is, how big, which columns, what to watch
(the words follow what the profile decided)."""

from __future__ import annotations

from typing import Any

from awa.data import ColumnProfile, FileProfile, Flag, TableProfile
from awa.ui._data_folder import megabytes

_KINDS = {
    "csv": "Table (CSV)",
    "xlsx": "Excel workbook",
    "pdf": "PDF document",
    "docx": "Word document",
    "mail": "Email",
    "json": "JSON data",
    "sqlite": "Database file",
    "text": "Text file",
    "image": "Image",
    "zip": "Archive",
    "unknown": "Unknown file type",
}
_TYPES = {
    "text": "text",
    "integer": "whole numbers",
    "decimal": "numbers with decimals",
    "date": "dates",
    "datetime": "dates and times",
    "time": "times",
    "bool": "yes or no",
    "id": "identifiers",
    "enum": "a few fixed values",
    "empty": "empty",
    "mixed": "mixed (text and numbers)",
}
_PERSONAL = {
    "email": "email addresses",
    "iban": "IBANs",
    "card": "card numbers",
    "ip": "IP addresses",
    "phone": "phone numbers",
    "name": "names",
    "address": "addresses",
    "tax_id": "tax numbers",
    "birthdate": "birth dates",
}
_SCAN = "Looks like a scan (the pages are pictures): the text cannot be read without text recognition (OCR)."
_PLAIN = {
    "DF-W-SCANNED": _SCAN,
    "DF-W-NO-TEXT-LAYER": _SCAN,
    "DF-W-FORMULA-NO-CACHE": "Some formulas have no saved result, so awa cannot see their values: "
    "open the file in Excel and save it again.",
    "DF-W-HIDDEN-SHEET": "The workbook has a hidden sheet.",
    "DF-W-HIDDEN-CONTENT": "Contains hidden rows, columns or text that a reader does not see.",
    "DF-W-NUMBERS-AS-TEXT": "Some numbers are stored as text; awa reads them as numbers.",
    "DF-W-ENCODING-ASSUMED": "The character encoding was guessed: check that umlauts and "
    "special characters look right.",
    "DF-W-PDF-ACTIVE-CONTENT": "The PDF contains scripts or active content: it is never run.",
    "DF-E-CORRUPT": "The file looks damaged and cannot be read.",
    "DF-W-DECIMAL-COMMA": "German number format found (decimal comma, for example 4.250,00): "
    "awa reads it as a German number; check a few values.",
    "DF-W-NUMBER-AMBIGUOUS": "A number format cannot be told apart safely: say how the "
    "numbers are written in your note.",
    "DF-W-DATE-AMBIGUOUS": "Day and month cannot be told apart safely: say how dates are "
    "written in your note.",
    "DF-W-TOTALS-ROW": "A totals row was found: it is not counted as data.",
    "DF-W-INJECTION-SUSPECTED": "Contains text that looks like instructions aimed at an AI: "
    "it is ignored.",
    "DF-W-HIDDEN-TEXT": "Contains hidden text that a reader does not see: it is treated as "
    "data only.",
    "DF-W-MACROS": "Contains macros: they are never run.",
    "DF-W-SAMPLED": "The file is large: only a part of it was analyzed.",
    "DF-W-HEADER-UNCERTAIN": "Not sure which row holds the column names.",
    "DF-W-MULTIROW-HEADER": "The column names span several rows.",
    "DF-W-LEADING-ZEROS": "Numbers with leading zeros (like 00123) are kept as text.",
    "DF-W-DUPLICATE-ROWS": "Some rows appear more than once.",
    "DF-E-ENCRYPTED": "The file is password protected, so awa cannot read it.",
    "DF-E-EMPTY": "The file is empty.",
    "DF-E-PARSE-FAILED": "awa could not read this file; it may be damaged.",
    "DF-E-TIMEOUT": "Reading the file took too long and was stopped.",
    "DF-E-TOO-LARGE": "The file is too large to analyze.",
}
_MAX_COLUMNS = 40
_MAX_SAMPLE_ROWS = 3


def kind_label(kind: str) -> str:
    """What a kind of file is called for a person (`xlsx` as `Excel workbook`)."""

    return _KINDS.get(kind, kind)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


def _rows(table: TableProfile) -> str:
    approx = "" if table.rows_exact else "about "
    return f"{approx}{_plural(table.rows, 'row')}, {_plural(len(table.columns), 'column')}"


def _headline(file: FileProfile) -> str:
    """One sentence on what the file is."""

    if file.status == "unsupported":
        return "awa does not read this kind of file."
    if file.status == "error":
        return "awa could not read this file."
    if file.pdf is not None:
        scan = {
            "born_digital": "its text can be read",
            "scanned": "it is a scan: the text cannot be read",
            "mixed": "some pages are scans",
            "empty": "no text found",
        }[file.pdf.document_class]
        return f"PDF document with {_plural(file.pdf.pages, 'page')}; {scan}."
    if file.docx is not None:
        parts = [_plural(file.docx.paragraphs, "paragraph")]
        if file.docx.tables:
            parts.append(_plural(len(file.docx.tables), "table"))
        return "Word document with " + " and ".join(parts) + "."
    if file.mail is not None:
        return f"{_plural(file.mail.messages_total, 'email message')}."
    if file.json_doc is not None:
        records = file.json_doc.records
        what = {"object": "one object", "array": "a list", "jsonl": "one record per line"}.get(
            file.json_doc.top_level, "a value"
        )
        suffix = f" with {_plural(records, 'record')}" if records else ""
        return f"JSON data: {what}{suffix}."
    if file.sqlite is not None:
        return f"Database file with {_plural(len(file.tables), 'table')}."
    if file.xlsx is not None:
        rows = sum(t.rows for t in file.tables)
        return (
            f"Excel workbook with {_plural(len(file.xlsx.sheets), 'sheet')} and "
            f"{_plural(rows, 'row')} of data."
        )
    if file.tables:
        return f"Table with {_rows(file.tables[0])}."
    if file.text is not None and file.text.lines is not None:
        return f"Text file with {_plural(file.text.lines, 'line')}."
    return kind_label(file.kind) + "."


def _column(column: ColumnProfile) -> dict[str, Any]:
    notes = []
    fmt = column.format
    if fmt is not None and fmt.decimal == ",":
        notes.append("decimal comma")
    if column.date_format:
        notes.append(f"dates like {column.date_format}")
    if column.ambiguous:
        notes.append("format not certain")
    if column.unit:
        notes.append(f"unit {column.unit}")
    if column.pii.kinds:
        notes.append("personal data: " + ", ".join(_PERSONAL.get(k, k) for k in column.pii.kinds))
    return {
        "name": column.name,
        "type": _TYPES.get(column.type, column.type),
        "examples": [str(example) for example in column.examples[:3]],
        "notes": notes,
    }


def _table(table: TableProfile) -> dict[str, Any]:
    title = table.sheet or table.name or "Table"
    shown = table.columns[:_MAX_COLUMNS]
    rows = [
        ["" if cell is None else str(cell) for cell in row[: len(shown)]]
        for row in table.sample_rows[:_MAX_SAMPLE_ROWS]
    ]
    return {
        "title": f"Sheet {title}" if table.sheet else title,
        "summary": _rows(table),
        "header_row": table.header_row if table.header_confidence >= 0.5 else None,
        "totals_rows": list(table.totals_rows),
        "columns": [_column(column) for column in shown],
        "more_columns": max(0, len(table.columns) - len(shown)),
        "sample": {"columns": [column.name for column in shown], "rows": rows} if rows else None,
    }


def _where(file: FileProfile, flag: Flag) -> str:
    """The column a flag is about (`Column Betrag: `), or nothing."""

    if not flag.where or "." not in flag.where:
        return ""
    for table in file.tables:
        for column in table.columns:
            if flag.where == f"{table.id}.{column.key}":
                return f"Column {column.name}: "
    return ""


def _flags(file: FileProfile) -> list[dict[str, str]]:
    flags = []
    for flag in file.flags:
        text = _PLAIN.get(flag.code) or flag.message[:1].upper() + flag.message[1:]
        flags.append(
            {"severity": flag.severity, "code": flag.code, "text": _where(file, flag) + text}
        )
    return flags


def file_card(file: FileProfile, note: str = "") -> dict[str, Any]:
    """The card of one profiled file.

    `status` is `ok`, `partial`, `error` or `unsupported`; `flags` say what to watch and
    `personal` the kinds of personal data found.

    Example:
        ```python
        card = file_card(profile.files[0])
        card["headline"]  # 'Table with 412 rows, 6 columns.'
        ```
    """

    personal = sorted(
        {_PERSONAL.get(k, k) for t in file.tables for c in t.columns for k in c.pii.kinds}
    )
    excerpt = ""
    for part in (file.pdf, file.docx):
        if part is not None and part.excerpt:
            excerpt = part.excerpt
    if file.mail is not None and file.mail.messages:
        message = file.mail.messages[0]
        excerpt = message.headers.get("subject", "") or message.excerpt
    return {
        "name": file.path,
        "size_text": megabytes(file.size),
        "kind": file.kind,
        "kind_label": kind_label(file.kind),
        "status": file.status,
        "headline": _headline(file),
        "tables": [_table(table) for table in file.tables],
        "flags": _flags(file),
        "personal": personal,
        "excerpt": excerpt[:300],
        "attachments": [
            a.name for m in (file.mail.messages if file.mail else []) for a in m.attachments
        ],
        "note": note,
    }


def pending_card(name: str, size: int, kind: str, note: str = "") -> dict[str, Any]:
    """The card of a stored file that has not been analyzed yet."""

    return {
        "name": name,
        "size_text": megabytes(size),
        "kind": kind,
        "kind_label": kind_label(kind),
        "status": "new",
        "headline": "Not analyzed yet.",
        "tables": [],
        "flags": [],
        "personal": [],
        "excerpt": "",
        "attachments": [],
        "note": note,
    }
