"""Printing check results for people: one line per check, the fixes, the administrator message."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO

from awa.checks._engine import CheckReport, CheckRun

_MARK = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
_COLOR = {"ok": "32", "warn": "33", "fail": "31", "skip": "90"}


def _painter(stream: TextIO) -> Callable[[str, str], str]:
    from awa.config import env_value

    colored = bool(getattr(stream, "isatty", lambda: False)()) and not env_value("NO_COLOR")

    def paint(status: str, text: str) -> str:
        return f"\x1b[{_COLOR[status]}m{text}\x1b[0m" if colored else text

    return paint


def print_line(run: CheckRun, out: TextIO | None = None) -> None:
    """One check as a line, and for a problem the fix (and that an admin text follows)."""

    out = out if out is not None else sys.stdout
    paint = _painter(out)
    status = run.result.status
    out.write(f"{paint(status, _MARK[status])} {run.check.id:<28} {run.result.said}\n")
    if status in ("fail", "warn") and run.result.fix:
        out.write(f"     fix: {run.result.fix}\n")
    if status in ("fail", "warn") and run.result.admin:
        out.write("     an administrator must act: see the message at the end\n")
    out.flush()


def print_summary(report: CheckReport, out: TextIO | None = None) -> None:
    """The counts, and the message for an administrator when one must act."""

    out = out if out is not None else sys.stdout
    counts = report.to_dict()["counts"]
    out.write(
        f"\n{counts['ok']} ok, {counts['warn']} warning(s), {counts['fail']} failed, "
        f"{counts['skip']} skipped\n"
    )
    message = report.admin_message()
    if message:
        out.write("\nSend this to your administrator:\n\n" + message + "\n")
