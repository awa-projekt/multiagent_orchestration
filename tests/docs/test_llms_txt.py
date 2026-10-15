"""docs/llms.txt and docs/llms-full.txt are generated from the docs; this test keeps them current."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

pytestmark = pytest.mark.skipif(
    not (DOCS / "llms.txt").is_file(), reason="docs/ is in the repository only"
)


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_llms", ROOT / "scripts" / "gen_llms.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _generator()


def test_the_checked_in_files_match_the_docs() -> None:
    for name, text in gen.render().items():
        current = (DOCS / name).read_bytes().decode("utf-8")
        assert current == text, f"docs/{name} is out of date; run python scripts/gen_llms.py"


def test_the_index_links_every_page_of_the_navigation_and_every_link_exists() -> None:
    index = (DOCS / "llms.txt").read_text("utf-8")
    links = re.findall(r"^- \[[^\]]+\]\(([^)]+)\)", index, re.MULTILINE)
    nav_pages = [path for _trail, path in gen.pages(gen._site()["nav"])]
    assert links == nav_pages
    assert not [link for link in links if not (DOCS / link).is_file()]


def test_the_full_file_holds_each_page_once_in_navigation_order() -> None:
    full = (DOCS / "llms-full.txt").read_text("utf-8")
    sources = re.findall(r"^<!-- source: (.+?) -->$", full, re.MULTILINE)
    pages = [p for _t, p in gen.pages(gen._site()["nav"])]
    expected = [p for p in pages if not gen._includes_only(gen._body(DOCS / p))]
    assert sources == expected


def test_a_description_does_not_end_at_the_initial_of_a_name(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Title\n\nThe library is written by A. Author. It does more.\n", "utf-8")
    assert gen.description_of(page) == "The library is written by A. Author."
