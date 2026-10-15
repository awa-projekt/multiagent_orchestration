"""Write docs/llms.txt and docs/llms-full.txt from the documentation.

Run it from the repository root after changing a docs page or the navigation in mkdocs.yml:

    python scripts/gen_llms.py            # rewrite both files
    python scripts/gen_llms.py --check    # exit 1 when a file is out of date

`llms.txt` is an index: the title, a one-line description and a relative link of every page of the
navigation. `llms-full.txt` is the same pages concatenated in navigation order. MkDocs copies both
files to the root of the built site. tests/docs/test_llms_txt.py fails while the checked-in files
differ from the pages.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUTPUTS = {"llms.txt": "index", "llms-full.txt": "full"}
MAX_DESCRIPTION = 200


class _Loader(yaml.SafeLoader):
    """Reads mkdocs.yml, which carries a python tag for the diagram fence."""


_Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda *_: None)


def _site() -> dict[str, Any]:
    data = yaml.load((ROOT / "mkdocs.yml").read_text("utf-8"), _Loader)  # noqa: S506
    assert isinstance(data, dict)
    return data


def _text(path: Path) -> str:
    return path.read_text("utf-8").replace("\r\n", "\n")


def _title_of(page: Path) -> str:
    for line in _text(page).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return page.stem.replace("-", " ").capitalize()


def pages(nav: Any, trail: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield `(section titles, page path)` for every page of a navigation, in order."""

    if isinstance(nav, str):
        yield trail, nav
    elif isinstance(nav, list):
        for item in nav:
            yield from pages(item, trail)
    elif isinstance(nav, dict):
        for label, value in nav.items():
            if isinstance(value, str):
                yield trail, value
            else:
                yield from pages(value, (*trail, str(label)))


def _labels(nav: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    if isinstance(nav, list):
        for item in nav:
            found.update(_labels(item))
    elif isinstance(nav, dict):
        for label, value in nav.items():
            if isinstance(value, str):
                found[value] = str(label)
            else:
                found.update(_labels(value))
    return found


def _plain(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\{\s*[#.:][^}]*\}", "", text)
    text = re.sub(r"[*`]+|(?<!\w)_+|_+(?!\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def description_of(page: Path) -> str:
    """The first sentence of the first prose paragraph that follows the title."""

    paragraph: list[str] = []
    fenced = False
    for line in _text(page).splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        skip = (
            not stripped
            or stripped.startswith(("#", "<!--", "--8<--", ":::", "!!!", "???", "|", "-", ">"))
            or line.startswith((" ", "\t"))
        )
        if skip:
            if paragraph and paragraph[0].startswith("**For**"):
                paragraph = []  # the audience line, not a description
                continue
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    text = _plain(" ".join(paragraph))
    sentence = re.match(r".+?(?<!\b[A-Z])[.!?](?=\s|$)", text)
    text = sentence.group(0) if sentence else text
    if len(text) > MAX_DESCRIPTION:
        text = text[: MAX_DESCRIPTION - 3].rsplit(" ", 1)[0] + "..."
    return text


def _body(page: Path) -> str:
    """The page without API directives and their option lines."""

    lines: list[str] = []
    skipping = False
    for line in _text(page).splitlines():
        if line.startswith(":::"):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _includes_only(body: str) -> bool:
    """Whether a page only includes another file, such as a file of the repository root."""
    lines = [line for line in body.splitlines() if line.strip() and not line.startswith("<!--")]
    return bool(lines) and all(line.startswith("--8<--") for line in lines)


def render_index() -> str:
    site = _site()
    nav = site["nav"]
    labels = _labels(nav)
    lines = [f"# {site['site_name']}", "", f"> {site['site_description']}", ""]
    section: tuple[str, ...] | None = None
    for trail, path in pages(nav):
        file = DOCS / path
        if trail != section:
            lines += ["", f"## {' / '.join(trail) if trail else 'Documentation'}", ""]
            section = trail
        title = labels.get(path) or _title_of(file)
        description = description_of(file)
        lines.append(f"- [{title}]({path})" + (f": {description}" if description else ""))
    return "\n".join(lines).replace("\n\n\n", "\n\n").strip() + "\n"


def render_full() -> str:
    site = _site()
    parts = [f"# {site['site_name']}", "", f"> {site['site_description']}"]
    for _trail, path in pages(site["nav"]):
        body = _body(DOCS / path)
        if _includes_only(body):
            continue
        parts += ["", f"<!-- source: {path} -->", "", body]
    return "\n".join(parts).strip() + "\n"


def render() -> dict[str, str]:
    """Both files, by file name."""

    return {"llms.txt": render_index(), "llms-full.txt": render_full()}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, text in render().items():
        target = DOCS / name
        current = target.read_bytes().decode("utf-8") if target.is_file() else None
        if current != text:
            stale.append(name)
            if not check:
                target.write_bytes(text.encode("utf-8"))
    if check and stale:
        print(f"out of date: {', '.join(stale)}; run python scripts/gen_llms.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
