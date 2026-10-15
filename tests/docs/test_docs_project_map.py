"""The project map page lists every package of `src/awa` and every top-level folder, and only those."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "develop" / "project-map.md"
LAYERS = {"Interfaces", "Compiler", "Integrations", "Orchestration", "Runtime", "Foundation"}

pytestmark = pytest.mark.skipif(not PAGE.is_file(), reason="docs/ is in the repository only")


def rows() -> list[list[str]]:
    text = PAGE.read_text("utf-8")
    table = text.split("## The packages of `src/awa`", 1)[1].split("\n## ", 1)[0]
    cells = [[c.strip() for c in line.strip().strip("|").split("|")] for line in table.splitlines()]
    return [r for r in cells if len(r) == 5 and r[0].startswith("`awa.")]


def test_every_package_and_top_level_module_is_on_the_page_and_nothing_else() -> None:
    src = ROOT / "src" / "awa"
    actual = {p.name for p in src.iterdir() if p.is_dir() and not p.name.startswith("__")}
    actual |= {p.stem for p in src.glob("*.py") if p.stem != "__init__"}
    shown = {row[0].strip("`").removeprefix("awa.") for row in rows()}
    assert shown == actual, f"only on the page: {shown - actual}; missing: {actual - shown}"


def test_each_row_names_a_layer_and_tests_that_exist_and_docs_that_exist() -> None:
    problems = []
    for name, _purpose, layer, tests, docs in rows():
        if layer not in LAYERS:
            problems.append(f"{name}: layer {layer!r}")
        for pattern in re.findall(r"`(tests/[^`]+)`", tests):
            if not list(ROOT.glob(pattern)):
                problems.append(f"{name}: no file matches {pattern}")
        for target in re.findall(r"\]\(([^)#]+)", docs):
            if not (PAGE.parent / target).is_file():
                problems.append(f"{name}: {target} does not exist")
    assert not problems, problems


def test_the_layers_agree_with_the_import_contracts() -> None:
    from architecture.import_contracts import layer_of_package

    expected = {name.removeprefix("awa."): layer for name, layer in layer_of_package().items()}
    shown = {row[0].strip("`").removeprefix("awa."): row[2] for row in rows()}
    wrong = {
        n: (shown[n], layer) for n, layer in expected.items() if n in shown and shown[n] != layer
    }
    assert not wrong, wrong


def test_every_top_level_folder_is_on_the_page() -> None:
    page = PAGE.read_text("utf-8").split("## The layers", 1)[0]
    folders = {
        p.name
        for p in ROOT.iterdir()
        if p.is_dir()
        and not p.name.startswith((".", "_"))
        and p.name not in {"site", "dist", "build"}
    }
    missing = {f for f in folders if f"`{f}/`" not in page}
    assert not missing, f"top-level folders not on the page: {missing}"
