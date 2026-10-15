"""Delimited text files (`csv`): encoding, dialect, header row, columns, types, totals rows."""

from __future__ import annotations

import codecs
import csv
import io
import itertools
import re
from collections import Counter
from pathlib import Path
from typing import BinaryIO, Literal, NamedTuple

from awa.data._model import FileProfile, TableProfile, TextInfo
from awa.data._parse import ParseContext, ParseTimeout
from awa.data._sampling import RowSample, numbered_records
from awa.data._stats import build_table
from awa.data._text import newline_style

KINDS = ("csv",)

_PROBE = 1 << 16
_CHUNK = 1 << 20
_DELIMITERS = ",;\t|"
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_QUOTED = re.compile(r'(?:^|[,;\t|])"', re.MULTILINE)
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_ROWS_PER_DEADLINE_CHECK = 2048
_DIALECT_ROWS = 100  # rows that decide the delimiter


class _Scan(NamedTuple):
    """What one pass over the decoded text counted."""

    replaced: int  # characters that could not be decoded
    wide: int  # decoded characters beyond ASCII
    controls: int
    chars: int
    newline: Literal["lf", "crlf", "cr", "mixed"] | None


class _Text(NamedTuple):
    """The decided encoding of a file and the notes about how sure that is."""

    encoding: str | None  # None: not text
    bom: bool
    scan: _Scan
    notes: list[tuple[str, str]]
    head: bytes  # the first bytes of the file, for the dialect


class _Read(NamedTuple):
    lines: int
    stopped: Literal["time", "malformed"] | None
    bytes_read: int


def profile_csv(path: Path, ctx: ParseContext) -> FileProfile:
    """Profile a delimited text file: one table, or an error flag."""

    with path.open("rb") as handle:
        return profile_csv_stream(handle, ctx)


def profile_csv_stream(handle: BinaryIO, ctx: ParseContext) -> FileProfile:
    """`profile_csv` for bytes already opened (a seekable binary stream): the parser itself
    opens nothing, so a test or an upload can hand it a `BytesIO`."""

    limits = ctx.limits
    sample = RowSample(limits.max_rows_exact, limits.sample_rows)
    size = handle.seek(0, 2)
    text = _inspect(handle)
    if text.encoding is None:
        return ctx.fail(
            "DF-E-NOT-TEXT",
            "the file has binary content (control characters or NUL bytes), not delimited text",
        )
    delimiter, quote, uncertain = _dialect(text.head, text.encoding)
    read = _read_rows(handle, text.encoding, delimiter, sample, ctx)
    if not sample.total:
        return ctx.fail("DF-E-EMPTY", "the file has no content")
    table, flags = build_table(
        sample,
        table_id=ctx.table_id(1),
        max_sample_rows=ctx.privacy.max_sample_rows,
        cell_chars=limits.max_chars_per_cell,
        unnamed=lambda index: f"column_{index + 1}",
        check_ragged=True,
    )
    profile = ctx.profile
    profile.text = TextInfo(
        encoding=text.encoding,
        newline=text.scan.newline,
        delimiter=delimiter,
        quote=quote,
        bom=text.bom,
        lines=read.lines,
    )
    for code, message in text.notes:
        ctx.flag(code, message)
    if uncertain:
        ctx.flag(
            "DF-W-DELIMITER-UNCERTAIN",
            f"the delimiter {delimiter!r} splits the rows only slightly better than another candidate; check the columns",
        )
    profile.flags.extend(flags)
    profile.tables.append(table)
    if read.stopped:
        _partial(ctx, table, read, size, sample.total)
    elif sample.sampled:
        profile.status = "partial"
        ctx.flag(
            "DF-W-SAMPLED",
            f"{table.rows} rows counted; types, distinct values, minimum and maximum come from a sample of {table.rows_seen} rows",
            table.id,
        )
    return profile


def _partial(ctx: ParseContext, table: TableProfile, read: _Read, size: int, rows: int) -> None:
    """The reading stopped early: the statistics cover the rows read, `rows` is an estimate."""

    ctx.profile.status = "partial"
    table.sampled, table.rows_seen = True, table.rows
    for column in table.columns:
        column.distinct, column.unique = None, False
    table.key_candidates = []
    if read.stopped == "time":
        table.rows = round(table.rows * size / max(read.bytes_read, 1))
        table.rows_exact = False
        ctx.flag(
            "DF-W-TIME-LIMIT",
            f"reading stopped after {rows} rows at the time limit; rows is an estimate from the bytes read",
            table.id,
        )
    else:
        ctx.flag(
            "DF-W-MALFORMED-CSV",
            f"reading stopped after line {read.lines}: the file is not valid delimited text (an unclosed quote or a cell above the size limit); rows are the ones read",
            table.id,
        )


_REPLACEMENT_UTF8 = "\N{REPLACEMENT CHARACTER}".encode()


def _scan(handle: BinaryIO, encoding: str) -> _Scan:
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    replaced = wide = controls = chars = 0
    crlf = lf = cr = 0
    carry = ""
    tail = b""  # the end of the last chunk: a U+FFFD the file holds may span two chunks
    handle.seek(0)
    for raw in iter(lambda: handle.read(_CHUNK), b""):
        text = carry + decoder.decode(raw)
        carry = "\r" if text.endswith("\r") else ""
        text = text[: len(text) - len(carry)]
        found = text.count("\N{REPLACEMENT CHARACTER}")
        # A U+FFFD written in the file is valid UTF-8, not a byte that failed to decode.
        written = (tail + raw).count(_REPLACEMENT_UTF8) if encoding == "utf-8" else 0
        tail = raw[-2:]
        lost = max(found - written, 0)
        replaced += lost
        if not text.isascii():
            wide += len(_NON_ASCII.findall(text)) - lost
        controls += len(_CONTROL.findall(text))
        chars += len(text)
        pairs = text.count("\r\n")
        crlf += pairs
        lf += text.count("\n") - pairs
        cr += text.count("\r") - pairs
    cr += len(carry)
    return _Scan(replaced, wide, controls, chars, newline_style(crlf, lf, cr))


def _inspect(handle: BinaryIO) -> _Text:
    handle.seek(0)
    head = handle.read(_PROBE)
    notes: list[tuple[str, str]] = []
    encoding = next((name for mark, name in _BOMS if head.startswith(mark)), None)
    bom = encoding is not None
    if encoding is None and b"\x00" in head:
        even, odd = head[0::2].count(0), head[1::2].count(0)
        if max(even, odd) < 0.4 * (len(head) / 2):
            return _Text(None, False, _Scan(0, 0, len(head), len(head), None), notes, head)
        encoding = "utf-16-le" if odd > even else "utf-16-be"
    scan = _scan(handle, encoding or "utf-8")
    if encoding is None:
        encoding = "utf-8"
        if scan.replaced and scan.wide:
            notes.append(
                (
                    "DF-W-MIXED-ENCODING",
                    f"{scan.wide} characters are valid UTF-8 but {scan.replaced} bytes are not; the file mixes encodings and the invalid bytes read as replacement characters",
                )
            )
        elif scan.replaced:
            encoding = "cp1252"
            scan = _scan(handle, encoding)
            notes.append(
                (
                    "DF-W-ENCODING-ASSUMED",
                    "the bytes are not valid UTF-8; the file is read as Windows-1252, the usual encoding of Excel and ERP exports in Western Europe (umlauts in the samples show whether that is right)",
                )
            )
    if encoding.startswith(("utf-16", "utf-32")):
        notes.append(
            (
                "DF-W-ENCODING-NOT-READABLE",
                f"the file is {encoding.upper()} text; the data tools of awa read UTF-8 and Windows-1252 only, so convert the file before a process reads it",
            )
        )
    if encoding == "cp1252" and scan.replaced:
        notes.append(
            (
                "DF-W-MIXED-ENCODING",
                f"{scan.replaced} bytes are not valid in Windows-1252 and read as replacement characters",
            )
        )
    if scan.chars and scan.controls / scan.chars > 0.02:
        return _Text(None, bom, scan, notes, head)
    return _Text(encoding, bom, scan, notes, head)


def _dialect(raw: bytes, encoding: str) -> tuple[str | None, str | None, bool]:
    """The delimiter (`None` for one column), the quote character and whether that is uncertain."""

    text = codecs.getincrementaldecoder(encoding)(errors="replace").decode(raw)
    if len(raw) == _PROBE:
        text = text[: text.rfind("\n") + 1] or text
    scores: dict[str, float] = {}
    for delimiter in _DELIMITERS:
        if delimiter not in text:
            continue
        try:
            rows = [
                row
                for row in itertools.islice(
                    csv.reader(io.StringIO(text), delimiter=delimiter), _DIALECT_ROWS
                )
                if any(cell.strip() for cell in row)
            ]
        except csv.Error:
            continue
        widths = Counter(len(row) for row in rows)
        count, width = max(((n, w) for w, n in widths.items() if w >= 2), default=(0, 0))
        if rows and (count * 2 > len(rows) or (count * 2 == len(rows) and len(rows[0]) == width)):
            scores[delimiter] = sum(n / len(rows) * (w - 1) / w for w, n in widths.items())
    quote = '"' if _QUOTED.search(text) else None
    ranked = sorted(scores, key=lambda d: (-scores[d], _DELIMITERS.index(d)))
    if not ranked:
        return None, quote, False
    close = len(ranked) > 1 and scores[ranked[1]] >= 0.9 * scores[ranked[0]]
    return ranked[0], quote, close


def _read_rows(
    handle: BinaryIO, encoding: str, delimiter: str | None, sample: RowSample, ctx: ParseContext
) -> _Read:
    stopped: Literal["time", "malformed"] | None = None
    lines = counter = 0
    handle.seek(0)
    text = io.TextIOWrapper(handle, encoding=encoding, errors="replace", newline="")
    try:
        for start, end, cells in numbered_records(text, delimiter):
            counter, lines = counter + 1, end
            if counter % _ROWS_PER_DEADLINE_CHECK == 0:
                ctx.check_deadline()
            if any(cell.strip() for cell in cells):
                sample.add(start, cells)
    except ParseTimeout:
        if not sample.total:
            raise
        stopped = "time"
    except csv.Error:
        stopped = "malformed"
    finally:
        text.detach()  # the caller closes the file
    return _Read(lines, stopped, handle.tell())
