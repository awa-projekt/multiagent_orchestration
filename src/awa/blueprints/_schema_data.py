"""Blueprint model for the `data:` section: which files a process works on and what it relies on.

`Blueprint.check` and `awa data check` verify it against the files on disk.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from awa.blueprints._schema_spec import _Spec
from awa.blueprints.ports import Mask

DATA_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
"""A data entry name: lower-case letters, digits and `_`, at most 48 characters (a file stem such
as `2024_0412_mueller` is a good name)."""

_DRIVE = re.compile(r"^[A-Za-z]:")


def relative_inside(value: str, what: str) -> str:
    """Return `value` if it is a relative path that stays inside its folder.

    Raises:
        ValueError: If it is absolute or contains `..`.
    """

    text = value.replace("\\", "/")
    if text.startswith("/") or _DRIVE.match(text) or ".." in text.split("/"):
        raise ValueError(
            f"{what} {value!r} must be relative and stay inside the project folder "
            "(no absolute path, no '..')"
        )
    return value


class DataExpect(_Spec):
    """What the process relies on in a file; every item is optional and checked offline.

    Attributes:
        columns: Columns that must exist (spelling, case and spaces do not matter).
        key: Columns that together identify a row; they must be unique in the file.
        header_row: Row of the column names, counted from 1; `0` means no header.
        date_format: Date pattern the process assumes, for example `DD.MM.YYYY`.
    """

    columns: list[str] = Field(default_factory=list)
    key: list[str] = Field(default_factory=list)
    sheet: str | None = None
    header_row: int | None = Field(default=None, ge=0)
    decimal: Literal[",", "."] | None = None
    date_format: str | None = Field(default=None, min_length=1, max_length=40)
    min_rows: int | None = Field(default=None, ge=0)


class DataBlueprint(_Spec):
    """One file (or a glob of files) of a process and the role it plays.

    Attributes:
        path: File or glob inside the project folder (no `..`, no absolute path); for role
            `output` it may not exist yet.
        role: `input` (read and worked on), `reference` (master data), `template` (a layout
            the agent copies) or `output` (where results go).
        handling: What should happen with each piece of data, shown to the agents.
        request: Request text for one run on this file (`awa data run`); `{file}` stands for
            its name.
        mask: How personal data is hidden in the file summary the model sees (not in what a
            tool reads); `None` follows the project setting.
        digest_tokens: Size of the file summary given to the model, 200 to 12000 tokens.
    """

    name: str
    path: str = Field(min_length=1, max_length=500)
    role: Literal["input", "reference", "template", "output"] = "input"
    description: str = Field(default="", max_length=300)
    expect: DataExpect = Field(default_factory=DataExpect)
    handling: str = Field(default="", max_length=2000)
    request: str | None = Field(default=None, max_length=1000)
    mask: Mask | None = None
    digest_tokens: int | None = Field(default=None, ge=200, le=12000)

    @field_validator("name")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        if not DATA_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                f"invalid data name {value!r}: use snake_case: lower-case letters, digits "
                "and '_', at most 48 characters"
            )
        return value

    @field_validator("path")
    @classmethod
    def _inside_the_project(cls, value: str) -> str:
        return relative_inside(value, "data path")
