"""Runners and file builders shared by the document-reader tests (repository only, never shipped).

Builders return bytes or write below a folder the test passes in (`tmp_path`); nothing here reads
a clock or the environment. Readers are run in two ways: `run` goes through `profile()` (content
detection, worker process for risky kinds, per-file error isolation) and `parse` calls one reader
directly (no detection, no worker), with no deadline, one that has already passed, or one that
passes after a given number of `check_deadline` calls (`CountdownContext`), so a time-limit test
needs no real waiting and no clock patching.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from awa.data import Limits, ParseContext, Privacy, profile
from awa.data._model import FileProfile
from awa.data._parse import Parser, ParseTimeout

CREATED = "2026-10-05T00:00:00Z"
"""The `created` value every test profile uses, so profiles are byte-comparable."""

# ------------------------------------------------------------------------------------ running


def run(path: Path, **limits: float) -> FileProfile:
    """The profile of one file, through `profile()`."""

    (file,) = profile(path, limits=Limits(**limits) if limits else None, created=CREATED).files
    return file


def codes(file: FileProfile) -> list[str]:
    """The flag codes of a file profile, in order."""

    return [flag.code for flag in file.flags]


def _embedded(name: str, data: bytes) -> FileProfile:
    return FileProfile(
        id="f1.a1",
        path=name,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        kind="unknown",
        status="unsupported",
    )


@dataclass
class CountdownContext(ParseContext):
    """A `ParseContext` whose deadline passes after `checks_allowed` calls of `check_deadline`."""

    checks_allowed: int = 0

    def check_deadline(self) -> None:
        self.checks_allowed -= 1
        if self.checks_allowed < 0:
            raise ParseTimeout("the test deadline passed")


def context(
    kind: str,
    path: Path,
    *,
    expired: bool = False,
    checks_allowed: int | None = None,
    **limits: float,
) -> ParseContext:
    """A parse context for `path` with a do-nothing child hook: no deadline, one that has passed
    (`expired`), or one that passes after `checks_allowed` checks."""

    file = FileProfile(
        id="f1",
        path=path.name,
        sha256="0" * 64,
        size=path.stat().st_size,
        kind=kind,  # type: ignore[arg-type]
    )
    deadline = 0.0 if expired else None  # monotonic time is positive: 0.0 has always passed
    args = (Limits(**limits), Privacy(), "f1", file, deadline, lambda event: None, _embedded)
    if checks_allowed is None:
        return ParseContext(*args)
    return CountdownContext(*args, checks_allowed=checks_allowed)


def parse(
    parser: Parser,
    kind: str,
    path: Path,
    *,
    expired: bool = False,
    checks_allowed: int | None = None,
    **limits: float,
) -> FileProfile:
    """Run one reader directly on `path`."""

    return parser(
        path, context(kind, path, expired=expired, checks_allowed=checks_allowed, **limits)
    )


# ---------------------------------------------------------------------------------- Word files


def make_docx(path: Path, build: Callable[..., None] | None = None) -> Path:
    """A Word file made by python-docx; `build(document)` fills it."""

    from docx import Document

    document = Document()
    if build is not None:
        build(document)
    document.save(str(path))
    return path


def patch_zip_part(path: Path, part: str, edit: Callable[[str], str]) -> Path:
    """Rewrite one XML part of a Word file (to add what python-docx cannot write)."""

    with zipfile.ZipFile(path) as source:
        items = [(info, source.read(info)) for info in source.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in items:
            if info.filename == part:
                data = edit(data.decode("utf-8")).encode("utf-8")
            target.writestr(info.filename, data)
    return path


def add_zip_entry(path: Path, name: str, data: bytes) -> Path:
    """Add one entry to a zip-based file."""

    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    return path


def tiny_png() -> bytes:
    """A valid one-pixel PNG."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------------------- mail


def build_mail(
    subject: str = "Hello", body: str = "Please send the invoice.", **headers: str
) -> EmailMessage:
    """A plain-text mail; extra keyword arguments become headers (`In_Reply_To` -> `In-Reply-To`)."""

    message = EmailMessage()
    message["From"] = "Anna Keller <anna@example.com>"
    message["To"] = "ar@company.example"
    message["Subject"] = subject
    message["Date"] = "Mon, 05 Oct 2026 09:30:00 +0200"
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    message.set_content(body)
    return message


def save_mail(folder: Path, name: str, message: EmailMessage) -> Path:
    """Write a mail as bytes below `folder`."""

    path = folder / name
    path.write_bytes(message.as_bytes())
    return path


def mbox_text(count: int, *, body: str = "Hello") -> str:
    """A mailbox of `count` short messages (From_ lines, blank-line separated)."""

    parts = [
        f"From sender{number}@example.com Mon Oct  5 09:30:00 2026\n"
        f"From: sender{number}@example.com\nSubject: message {number}\n\n{body} {number}\n"
        for number in range(1, count + 1)
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------------------- PDF


def pdf_file(objects: list[bytes]) -> bytes:
    """A PDF file from object bodies (object 1 is the catalog), with a correct xref table."""

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def pdf_stream(dictionary: bytes, data: bytes) -> bytes:
    """An object body that is a stream."""

    return b"<< %s /Length %d >>\nstream\n" % (dictionary, len(data)) + data + b"\nendstream"


PAGE_TEXT = (
    b"BT /F1 11 Tf 56 780 Td (Hello world, this page holds enough plain text to count) Tj ET"
)
"""Content stream of a page with 55 characters of text (enough to count as a text layer)."""


def pdf_pages(
    *contents: bytes,
    resources: bytes = b"/Font << /F1 5 0 R >>",
    extras: tuple[bytes, ...] = (),
    catalog: bytes = b"<< /Type /Catalog /Pages 2 0 R >>",
    count: int | None = None,
) -> bytes:
    """A PDF with one page per content stream; objects 1 to 5 are catalog, tree and font.

    `extras` become objects 6 + 2n and up (after the n pages and n content streams), `count`
    overrides the page count the tree claims.
    """

    n = len(contents)
    kids = b" ".join(b"%d 0 R" % (6 + i) for i in range(n))
    objects = [
        catalog,
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n if count is None else count),
        b"<< >>",
        b"<< >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
        b"/Resources << %s >> >>" % (6 + n + i, resources)
        for i in range(n)
    ]
    objects += [pdf_stream(b"", content) for content in contents]
    return pdf_file(objects + list(extras))


SCAN_PAGE = b"q 595 0 0 842 0 0 cm /Im1 Do Q"
"""Content stream of a page that is one picture and has no text."""


def image_pdf(*contents: bytes) -> bytes:
    """A PDF whose pages all draw the same one-pixel image (default: one scan page)."""

    n = max(1, len(contents))
    kids = b" ".join(b"%d 0 R" % (4 + i) for i in range(n))
    image = pdf_stream(
        b"/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8",
        b"\x00\xff\xff\x00",
    )
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n),
        image,
    ]
    streams = contents or (SCAN_PAGE,)
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources "
        b"<< /XObject << /Im1 3 0 R >> /Font << /F1 %d 0 R >> >> >>" % (4 + n + i, 4 + 2 * n)
        for i in range(n)
    ]
    objects += [pdf_stream(b"", content) for content in streams]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return pdf_file(objects)


# ------------------------------------------------------------------------- JSON Lines, SQLite


def write_jsonl(folder: Path, name: str, records: list[object]) -> Path:
    """A JSON Lines file with one record per line."""

    path = folder / name
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def make_db(
    path: Path, statements: list[str], rows: list[tuple[str, tuple[object, ...]]] | None = None
) -> Path:
    """A SQLite file made by running `statements`, then `(statement, values)` insert rows."""

    connection = sqlite3.connect(path)
    for statement in statements:
        connection.execute(statement)
    for statement, values in rows or []:
        connection.execute(statement, values)
    connection.commit()
    connection.close()
    return path


def write_file(folder: Path, name: str, data: bytes) -> Path:
    """Write `data` to `folder/name` and return the path."""

    path = folder / name
    path.write_bytes(data)
    return path


def allocate_too_much(path: Path, ctx: ParseContext) -> FileProfile:
    """A reader (named by `module:function` for the worker) that asks for 3 GiB at once."""

    bytearray(3 * 1024**3)
    return ctx.profile


def sha256_of(path: Path) -> str:
    """The SHA-256 of a file, to prove a reader left it untouched."""

    return hashlib.sha256(path.read_bytes()).hexdigest()
