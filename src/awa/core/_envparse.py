"""The one `.env` parser: the assignments of a file and the line of each."""

from __future__ import annotations

import re
from pathlib import Path

from awa.errors import ConfigurationError

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
"""Accepted `.env` variable names."""

ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env_file(path: str | Path) -> tuple[dict[str, str], dict[str, int]]:
    """The assignments of the `.env` file at `path` and the line of each; the last assignment wins.

    Raises:
        ConfigurationError: The file is not UTF-8 text.
    """

    file = Path(path)
    if not file.is_file():
        return {}, {}
    from dotenv import dotenv_values

    try:
        values = {
            name: value or ""
            for name, value in dotenv_values(file, interpolate=False, encoding="utf-8-sig").items()
        }
        text = file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            f"{file} is not UTF-8 text (byte {exc.start}); save it as UTF-8"
        ) from None
    lines: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = ASSIGNMENT.match(line)
        if match:
            lines[match.group(1)] = number
    return values, lines
