"""The docs and texts say what the mask covers: the design digest, not the run-time file tools."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATEMENT = "The mask applies to the digest that the design model sees."
PAGES = (
    "docs/develop/data.md",
    "docs/develop/production.md",
    "docs/guide/files.md",
    "docs/guide/web-app.md",
)


CANONICAL = "docs/guide/files.md"
LINK = "what-the-mask-covers"


def _text(page: str) -> str:
    return " ".join((ROOT / page).read_text(encoding="utf-8").split())


def test_one_page_explains_what_the_mask_covers() -> None:
    text = _text(CANONICAL)
    assert "### What the mask covers" in (ROOT / CANONICAL).read_text(encoding="utf-8")
    for fact in ("what the *design* model reads", "at run time", "unmasked", "RegexRedactor.pii()"):
        assert fact in text, fact


@pytest.mark.parametrize("page", PAGES)
def test_pages_say_the_mask_covers_the_design_digest_only(page: str) -> None:
    """Every page that mentions the mask states its limit or links the one explanation."""

    text = _text(page)
    states_it = STATEMENT in text and "see the file content unmasked at run time" in text
    assert states_it or LINK in text, f"{page}: state the limit or link {CANONICAL}#{LINK}"


def test_build_help_and_web_app_say_the_same() -> None:
    build_help = _text("src/awa/cli/_blueprint.py")
    assert "see the file content unmasked at run time" in build_help
    page = _text("src/awa/ui/page.html")
    assert "at run time see it unmasked" in page
    script = (ROOT / "src/awa/ui/js/25_data.js").read_text(encoding="utf-8")
    assert "at run time see it unmasked" in script


def test_the_cli_help_lists_what_placeholders_cover() -> None:
    help_text = _text("docs/reference/cli.md")
    assert "placeholders such as <IBAN_1> for names, tax and bank numbers" in help_text
