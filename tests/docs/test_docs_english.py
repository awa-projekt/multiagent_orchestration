"""The documentation is English, with no German part.

There is no German section and no German page in the navigation. No page of `docs/`, no top-level Markdown file (README,
CHANGELOG, CONTRIBUTING) and no Markdown file under `examples/` holds German prose, and
no file or folder name under `docs/`, `src/`, `examples/` or the repository root is German.
Code, scripts, example data and tests are guarded by `test_english_only.py`, which allows German
only in named German test data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

pytestmark = pytest.mark.skipif(not DOCS.is_dir(), reason="docs/ is in the repository only")

# Function words that are German and not English; a line with three of them is German prose.
WORDS = frozenset(
    {
        *("der", "die", "das", "und", "nicht", "ist", "mit", "für", "eine", "wird", "oder"),
        *("bei", "von", "dem", "den", "des", "sich", "wenn", "nur", "auch", "sind", "werden"),
        *("kein", "keine", "jede", "jeder", "dass", "zum", "zur", "nach", "aus", "über"),
    }
)
# Parts of German file and folder names.
NAME_PARTS = (
    "deutsch", "unternehmen", "anbindung", "vertrieb", "verwaltung", "ausbau", "abnahme",
    "automatisierung", "aufgaben", "anleitung", "rechnung", "lieferant", "einstellung",
)  # fmt: skip
FENCE = re.compile(r"^\s*```")
CODE = re.compile(r"`[^`]*`")
LINK = re.compile(r"\]\([^)]*\)")
SKIPPED_PARTS = {"__pycache__", ".venv", ".venv-dev", "node_modules", "site", ".git"}


class _MkdocsLoader(yaml.SafeLoader):
    """Reads `mkdocs.yml`, which names Python objects for extensions: they are not needed here."""


_MkdocsLoader.add_multi_constructor("tag:yaml.org,2002:python/", lambda loader, suffix, node: None)


def prose(text: str) -> list[str]:
    """The lines of a Markdown page outside code blocks, without inline code and link targets."""

    lines, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            lines.append(LINK.sub("]", CODE.sub("", line)))
    return lines


def german_lines(text: str) -> list[str]:
    return [
        line
        for line in prose(text)
        if len(WORDS & set(re.findall(r"[a-zäöüß]+", line.lower()))) >= 3
    ]


def markdown_files() -> list[Path]:
    found = sorted(DOCS.rglob("*.md")) + sorted(ROOT.glob("*.md"))
    found += sorted((ROOT / "examples").rglob("*.md"))
    return [p for p in found if not SKIPPED_PARTS.intersection(p.parts)]


def nav_files(items: list) -> list[str]:
    found: list[str] = []
    for item in items:
        if isinstance(item, str):
            found.append(item)
        else:
            [(_, value)] = item.items()
            found += nav_files(value) if isinstance(value, list) else [value]
    return found


def test_the_guard_finds_german_prose() -> None:
    assert german_lines("Die Vorlage ist nicht für den Versand und wird nur gelesen.")
    assert not german_lines("The template is not for sending and is only read.")


def test_there_is_no_german_section() -> None:
    assert not (DOCS / "de").exists()
    nav = yaml.load((ROOT / "mkdocs.yml").read_text("utf-8"), Loader=_MkdocsLoader)["nav"]
    assert not [entry for entry in nav if isinstance(entry, dict) and "Deutsch" in entry]
    assert not [name for name in nav_files(nav) if name.startswith("de/")]


def test_no_german_prose_in_the_docs_the_top_level_files_or_the_example_guides() -> None:
    offenders = {}
    for page in markdown_files():
        found = german_lines(page.read_text("utf-8"))
        if found:
            offenders[page.relative_to(ROOT).as_posix()] = found[:2]
    assert not offenders, f"German prose (the docs are English; translate it): {offenders}"


def test_no_german_file_or_folder_names() -> None:
    paths = list(ROOT.iterdir())
    for folder in ("docs", "src", "examples"):
        paths += list((ROOT / folder).rglob("*"))
    offenders = sorted(
        p.relative_to(ROOT).as_posix()
        for p in paths
        if not SKIPPED_PARTS.intersection(p.relative_to(ROOT).parts)
        and any(part in p.name.lower() for part in NAME_PARTS)
    )
    assert not offenders, f"German file or folder names (name them in English): {offenders}"


def test_no_page_links_to_a_german_section() -> None:
    linked = [
        page.relative_to(ROOT).as_posix()
        for page in markdown_files()
        if re.search(r"\]\((\.\./)*(docs/)?de/", page.read_text("utf-8"))
    ]
    assert not linked, linked
