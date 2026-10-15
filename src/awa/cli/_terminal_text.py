"""Markdown reports as terminal text: aligned tables, plain headings, no `**` or backticks."""

from __future__ import annotations

import re

_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_FAILED = re.compile(r"\*\*(.+?)\*\*")
_NUMBER = re.compile(r"^[-+]?\d[\d.,]*%?$")
"""A cell a right-aligned column aligns right: a number or a percentage (text stays left)."""
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def terminal_safe(text: str) -> str:
    """`text` without control characters other than tab and newline.

    Model and tool output then cannot move the cursor, recolor or retitle the terminal
    (escape sequences lose their ESC and print as plain text).
    """

    return _CONTROL.sub("", text)


def _inline(text: str) -> str:
    """Text without Markdown emphasis and code marks; `\\|` becomes `|`."""

    return text.replace("**", "").replace("`", "").replace("\\|", "|").strip()


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [cell.strip() for cell in _CELL_SPLIT.split(inner)]


def _table(lines: list[str]) -> list[str]:
    rows = [_cells(line) for line in lines]
    right: list[bool] = []
    if len(rows) > 1 and all(_SEPARATOR.match(cell) for cell in rows[1]):
        right = [cell.endswith(":") for cell in rows[1]]
        rows = [rows[0], *rows[2:]]
    # In a report table, bold marks a value that failed: it keeps that meaning as a word.
    rows = [[_inline(_FAILED.sub(r"\1 (failed)", cell)) for cell in row] for row in rows]
    columns = max(len(row) for row in rows)
    rows = [row + [""] * (columns - len(row)) for row in rows]
    right = right[:columns] + [False] * (columns - len(right))
    # A right-aligned column stays right-aligned only when every value in it is a number.
    right = [
        align and all(_NUMBER.match(row[i]) for row in rows[1:] if row[i])
        for i, align in enumerate(right)
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(columns)]
    out = []
    for row in rows:
        cells = [
            cell.rjust(width) if align else cell.ljust(width)
            for cell, width, align in zip(row, widths, right, strict=True)
        ]
        out.append("  " + "  ".join(cells).rstrip())
    return out


def markdown_as_text(markdown: str) -> str:
    """The terminal form of a Markdown report: headings as underlined titles, tables as
    aligned columns (numbers right-aligned where the table says so), emphasis removed."""

    out: list[str] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("|"):
            block = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                block.append(lines[index])
                index += 1
            out += _table(block)
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            title = _inline(heading[2])
            out += [title, ("=" if len(heading[1]) == 1 else "-") * len(title)]
        else:
            out.append(_inline(line) if line.strip() else "")
        index += 1
    return "\n".join(out).rstrip() + "\n"
