"""The size of each package group stays under its ceiling (`size_ceilings.toml`).

A ceiling only comes down: a change that shrinks a group by more than the slack below lowers the
ceiling in the same commit, so the code cannot grow back unnoticed.
"""

from __future__ import annotations

import ast
import tomllib
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from architecture.scan import HERE, SRC

SLACK = 500
"""Lines a group may sit under its ceiling before the ceiling must be lowered."""


@cache
def groups() -> dict[str, dict[str, Any]]:
    with (HERE / "size_ceilings.toml").open("rb") as handle:
        found: dict[str, dict[str, Any]] = tomllib.load(handle)["groups"]
    return found


@cache
def lines_per_package() -> Counter[str]:
    """Code lines of the `.py` files of each top-level package or module of src/awa.

    A code line is any line that is neither blank nor a comment; docstrings count.
    """

    counts: Counter[str] = Counter()
    for path in (SRC / "awa").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(SRC / "awa").parts
        top = parts[0].removesuffix(".py") if len(parts) == 1 else parts[0]
        lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
        counts[top] += sum(1 for line in lines if line and not line.startswith("#"))
    return counts


def test_every_package_is_in_exactly_one_group() -> None:
    listed = Counter(package for group in groups().values() for package in group["packages"])

    assert not [p for p, n in listed.items() if n > 1], "a package is in two groups"
    assert not set(lines_per_package()) - set(listed), (
        f"add to a group in size_ceilings.toml: {sorted(set(lines_per_package()) - set(listed))}"
    )
    assert not set(listed) - set(lines_per_package()), (
        f"no such package in src/awa: {sorted(set(listed) - set(lines_per_package()))}"
    )


@pytest.mark.parametrize("name", sorted(groups()))
def test_a_group_stays_under_its_ceiling(name: str) -> None:
    group = groups()[name]
    size = sum(lines_per_package()[package] for package in group["packages"])

    assert size <= group["ceiling"], (
        f"{name}: {size} lines, ceiling {group['ceiling']} (target {group['target']}). Make the "
        "change smaller, or raise the ceiling with a reason in the commit message."
    )
    assert size >= group["ceiling"] - SLACK or group["ceiling"] <= group["target"], (
        f"{name}: {size} lines, {group['ceiling'] - size} under the ceiling {group['ceiling']}. "
        f"Lock the progress in: set ceiling = {size} in size_ceilings.toml."
    )


REGISTRY_CODE_LINES = 913
"""Code lines (no docstrings, comments or blanks) of every file of `awa.plugins`; it only goes down."""


def _code_lines(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    skipped: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                skipped.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return sum(
        1
        for number, line in enumerate(text.splitlines(), 1)
        if number not in skipped and line.strip() and not line.strip().startswith("#")
    )


def test_the_plugin_registry_stays_small() -> None:
    total = sum(_code_lines(path) for path in (SRC / "awa" / "plugins").glob("*.py"))

    assert total <= REGISTRY_CODE_LINES, (
        f"the plugin registry holds {total} code lines, the ceiling is {REGISTRY_CODE_LINES}"
    )
