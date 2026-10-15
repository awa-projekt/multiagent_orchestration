"""File kind detection: content first, file name second."""

from __future__ import annotations

import csv
import json
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from awa.data._model import FileKind

_EXTENSIONS: dict[str, FileKind] = {
    **dict.fromkeys((".csv", ".tsv"), "csv"),
    **dict.fromkeys((".txt", ".md", ".markdown", ".log", ".text", ".rst", ".yaml", ".yml"), "text"),
    **dict.fromkeys((".json", ".jsonl", ".ndjson"), "json"),
    **dict.fromkeys((".eml", ".mbox"), "mail"),
    **dict.fromkeys((".xlsx", ".xlsm"), "xlsx"),
    ".docx": "docx",
    ".pdf": "pdf",
    **dict.fromkeys((".sqlite", ".sqlite3", ".db"), "sqlite"),
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp"), "image"),
    ".zip": "zip",
}
_IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*", b"GIF8", b"BM")
_MAIL_HEADERS = frozenset(
    {"from", "to", "subject", "date", "message-id", "mime-version", "received", "return-path"}
)
_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]")
_MBOX_FROM = re.compile(r"^From \S+ .*\d{4}")
_HEAD_BYTES = 8192
_CONTROLS = bytes(set(range(32)) - {9, 10, 12, 13})  # control bytes besides tab, newline, form feed
_MAX_JSON_BYTES = 1_000_000
_MAX_OFFICE_ENTRIES = 20_000
_TEXT_VARIANTS = {("text", "csv"), ("text", "json"), ("text", "mail")}


@dataclass(frozen=True)
class Detection:
    """The result of `detect_kind`.

    Attributes:
        detected_by: `content` when the bytes decided, `extension` when the name did (a text
            format with no signature).
        declared: The kind the file name claims, or `None` when the name says nothing.
        note: Empty, or why the content could not be checked to the end; the kind is then a safe
            guess.
    """

    kind: FileKind
    detected_by: Literal["content", "extension"]
    declared: FileKind | None = None
    note: str = ""

    @property
    def mismatch(self) -> bool:
        """The name claims a kind the content does not have (a `.pdf` that is not a PDF)."""

        return (
            self.declared is not None
            and self.declared != self.kind
            and (self.declared, self.kind) not in _TEXT_VARIANTS
        )


def detect_kind(path: str | Path) -> Detection:
    """The kind of the file at `path`; the bytes decide wherever a format has a signature."""

    file = Path(path)
    declared = _EXTENSIONS.get(file.suffix.lower())
    with file.open("rb") as handle:
        head = handle.read(_HEAD_BYTES)
    signed = _by_signature(file, head)
    if signed is not None:
        return Detection(signed[0], "content", declared, signed[1])
    if not _is_text(head):
        return Detection("unknown", "content", declared)
    shaped, note = _by_shape(file, head)
    if shaped is not None:
        return Detection(shaped, "content", declared, note)
    if declared in ("csv", "text", "json", "mail"):
        return Detection(declared, "extension", declared, note)
    if declared is None:
        return Detection("unknown", "extension", None, note)
    if not head:  # an empty file claiming to be a binary format
        return Detection("unknown", "content", declared, note)
    return Detection("text", "content", declared, note)  # text where a binary format was claimed


_PDF_HEADER = re.compile(rb"(?:^|[\r\n])%PDF-\d\.\d")
"""A PDF header at the start of a line, as a version (tools may put bytes in front of it)."""


def _by_signature(path: Path, head: bytes) -> tuple[FileKind, str] | None:
    if _PDF_HEADER.search(head[:1024]):
        return "pdf", ""
    if head.startswith(b"SQLite format 3\x00"):
        return "sqlite", ""
    if head.startswith(_IMAGE_SIGNATURES) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image", ""
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _zip_kind(path)
    return None


def _zip_kind(path: Path) -> tuple[FileKind, str]:
    """`xlsx`, `docx` or plain `zip` by part names; an unreadable archive is a plain zip."""

    try:
        if _zip_entries(path) > _MAX_OFFICE_ENTRIES:
            return "zip", ""
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return "zip", ""
    except Exception:
        return "zip", "the zip directory cannot be read"
    if "[Content_Types].xml" in names:
        if "xl/workbook.xml" in names:
            return "xlsx", ""
        if "word/document.xml" in names:
            return "docx", ""
    return "zip", ""


def _zip_entries(path: Path) -> int:
    """Entry count from the end-of-central-directory record, before the whole directory is read."""

    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 65557))
        tail = handle.read()
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) < at + 22:
        return 0
    return int(struct.unpack_from("<H", tail, at + 10)[0])


def _is_text(head: bytes) -> bool:
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 byte-order mark
        return True
    if b"\x00" in head:
        return False
    controls = len(head) - len(head.translate(None, _CONTROLS))
    return controls <= len(head) * 0.02


def _decode(head: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return head.decode(encoding)
        except UnicodeDecodeError:
            continue
    return head.decode("latin-1")


def _by_shape(path: Path, head: bytes) -> tuple[FileKind | None, str]:
    """`json`, `mail` or a delimited `.txt` table by text shape, and a note when a check stopped."""

    text = _decode(head)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None, ""
    if _MBOX_FROM.match(lines[0]):
        return "mail", ""
    names = {m[1].lower() for line in lines[:30] if (m := _HEADER_LINE.match(line))}
    if len(names & _MAIL_HEADERS) >= 2 and _HEADER_LINE.match(lines[0]):
        return "mail", ""
    first = text.lstrip()[:1]
    if first in "{[":
        verdict = _json_verdict(path, lines)
        if verdict is True:
            return "json", ""
        if verdict is None:
            return None, "too deeply nested to check"
    if path.suffix.lower() == ".txt" and _looks_tabular(lines):
        return "csv", ""
    return None, ""


def _json_verdict(path: Path, lines: list[str]) -> bool | None:
    """True: parses as JSON (or JSON Lines). False: does not. None: the parser gave up on the
    nesting depth or the memory it needs, so the file stays unclassified."""

    try:
        if path.stat().st_size <= _MAX_JSON_BYTES:
            try:
                json.loads(path.read_bytes().decode("utf-8-sig"))
                return True
            except ValueError:  # not JSON as a whole (UnicodeDecodeError is a ValueError)
                pass
        records = lines[:3]  # JSON Lines: several lines that are each an object
        try:
            return len(records) > 1 and all(isinstance(json.loads(r), dict) for r in records)
        except ValueError:
            return False
    except (RecursionError, MemoryError):
        return None


def _looks_tabular(lines: list[str]) -> bool:
    """At least three rows that split into the same number (two or more) of cells."""

    rows = lines[:20]
    for delimiter in ",;\t|":
        widths = [len(row) for row in csv.reader(rows, delimiter=delimiter)]
        if len(widths) >= 3 and widths[0] >= 2 and widths.count(widths[0]) >= 0.8 * len(widths):
            return True
    return False
