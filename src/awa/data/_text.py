"""Plain text, Markdown and log files (`text`): encoding, line endings, line count, ends."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from awa._internal.sandbox import file_encoding
from awa.data._injection import injection_flags
from awa.data._model import FileProfile, TextInfo
from awa.data._parse import ParseContext

KINDS = ("text",)

HEAD_LINES = 5
_CHUNK = 1 << 20
_WINDOW = 500_000  # characters scanned at each end (what `injection_flags` looks at)


def newline_style(crlf: int, lf: int, cr: int) -> Literal["lf", "crlf", "cr", "mixed"] | None:
    """The line ending a text uses, from the counts of each kind; `mixed` for more than one."""

    if sum(bool(count) for count in (crlf, lf, cr)) > 1:
        return "mixed"
    if crlf or lf:
        return "crlf" if crlf else "lf"
    return "cr" if cr else None


def profile_text(path: Path, ctx: ParseContext) -> FileProfile:
    """Profile a text file: encoding, newline style, number of lines, first and last lines."""

    if ctx.profile.size == 0:
        return ctx.fail("DF-E-EMPTY", "the file is empty")
    encoding, bom = file_encoding(path)
    head = tail = pending = ""  # a trailing CR waits for the next chunk (half of a CRLF?)
    crlf = lf = cr = 0
    last = "\n"
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        while chunk := handle.read(_CHUNK):
            ctx.check_deadline()
            text = pending + chunk
            pending = "\r" if text.endswith("\r") else ""
            text = text[: len(text) - len(pending)]
            crlf += text.count("\r\n")
            lf += text.count("\n") - text.count("\r\n")
            cr += text.count("\r") - text.count("\r\n")
            head += text[: max(0, _WINDOW - len(head))]
            tail = (tail + text)[-_WINDOW:]
            last = chunk[-1]
    cr += bool(pending)
    tail += pending
    lines = crlf + lf + cr + (last not in "\r\n")
    limit = ctx.limits.max_chars_per_cell
    first = head.splitlines()[:HEAD_LINES]
    final = tail.splitlines()[-HEAD_LINES:] if lines > HEAD_LINES else []
    ctx.profile.text = TextInfo(
        encoding=encoding,
        newline=newline_style(crlf, lf, cr),
        bom=bom,
        lines=lines,
        head=[line[:limit] for line in first],
        tail=[line[:limit] for line in final],
    )
    scanned = head if len(head) < _WINDOW else f"{head}\n{tail}"
    ctx.profile.flags.extend(injection_flags(scanned, ctx.file_id))
    return ctx.profile
