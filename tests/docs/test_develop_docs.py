"""The Develop and About pages state facts about the code; this test keeps them true.

What is checked on every page under `docs/develop` and `docs/about`: the page is in the navigation,
every internal link and anchor resolves, Python, YAML and JSON blocks parse, every `awa` name a
Python block imports exists without a deprecation, every `awa` command and option shown is real,
every repository path named in backticks exists, and the wording is US English without unfinished
markers. The architecture page shows the import layers of `.importlinter`, and the web app design
page names only tokens and components that exist. The runnable examples of these pages are executed
by `test_docs_develop_examples.py`. The docs folder is part of the repository only, so the tests
skip without it.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import re
import shlex
import warnings
from pathlib import Path

import pytest
import test_guide as guide
import yaml

ROOT = guide.ROOT
DOCS = guide.DOCS
SECTIONS = ("develop", "about")

pytestmark = pytest.mark.skipif(
    not (DOCS / "develop").is_dir(), reason="docs/ is in the repository only"
)

UNFINISHED = re.compile(r"TODO|FIXME|TBD|coming soon|lorem ipsum|version plan", re.IGNORECASE)
REPO_PATH = re.compile(r"`((?:examples|scripts|tests|src|deploy)/[\w./-]+)`")


def pages() -> list[Path]:
    return sorted(p for section in SECTIONS for p in (DOCS / section).rglob("*.md"))


def python_blocks() -> list[tuple[guide.Block, ast.Module]]:
    found = []
    for page in pages():
        for block in guide.blocks_of(page):
            if block.lang == "python":
                try:
                    found.append((block, ast.parse(block.text)))
                except SyntaxError:
                    continue  # reported by test_python_yaml_and_json_blocks_parse
    return found


def test_every_page_is_in_the_navigation() -> None:
    nav = (ROOT / "mkdocs.yml").read_text("utf-8")
    missing = [
        page.relative_to(DOCS).as_posix()
        for page in pages()
        if page.relative_to(DOCS).as_posix() not in nav
    ]
    assert not missing, f"pages without a navigation entry: {missing}"


def _anchors(page: Path) -> set[str]:
    """The anchors of a page as mkdocs makes them (underscores stay), and the generated API pages."""

    from markdown.extensions.toc import slugify

    text = guide.text_of(page)
    if "\n::: " in text or text.startswith("::: "):
        return {"*"}  # mkdocstrings makes the anchors of an API page
    headings = re.findall(r"(?m)^#{1,6} +(.+?)\s*$", guide.outside_fences(text))
    found = {
        slugify(re.sub(r"[*`]", "", heading.split("{")[0].strip()), "-") for heading in headings
    }
    return found | guide.anchors_of(page)


@pytest.mark.skipif(importlib.util.find_spec("markdown") is None, reason="needs the docs extra")
def test_every_link_and_anchor_resolves() -> None:
    problems = []
    for page in pages():
        text = guide.outside_fences(guide.text_of(page))
        for number, line in enumerate(text.splitlines(), 1):
            for target in re.findall(r"\]\(([^)\s]+)\)", line):
                if re.match(r"(https?:|mailto:)", target):
                    continue
                path, _, anchor = target.partition("#")
                resolved = page if not path else (page.parent / path).resolve()
                where = f"{guide._short_path(page)}:{number}: {target}"
                if not resolved.exists():
                    problems.append(f"{where} does not exist")
                elif anchor and resolved.suffix == ".md" and not {anchor, "*"} & _anchors(resolved):
                    problems.append(f"{where}: no heading or id {anchor!r}")
    assert not problems, "\n".join(problems)


def test_python_yaml_and_json_blocks_parse() -> None:
    problems = []
    for page in pages():
        for block in guide.blocks_of(page):
            try:
                if block.lang == "python":
                    ast.parse(block.text)
                elif block.lang in ("yaml", "yml") and "..." not in block.text:
                    yaml.safe_load(block.text)
                elif block.lang == "json":
                    json.loads(block.text)
            except (SyntaxError, yaml.YAMLError, ValueError) as error:
                problems.append(f"{block.where}: {block.lang} does not parse: {error}")
    assert not problems, "\n".join(problems)


def _exists(module: str, name: str | None) -> bool:
    """Whether `module` (and `name` in it) can be imported without a deprecation warning."""

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        return _importable(module, name)


def _importable(module: str, name: str | None) -> bool:
    try:
        loaded = importlib.import_module(module)
    except (ImportError, DeprecationWarning):
        return False
    if name is None or hasattr(loaded, name):
        return True
    try:
        importlib.import_module(f"{module}.{name}")
    except ImportError:
        return False
    return True


def test_every_awa_name_a_python_block_imports_exists() -> None:
    problems = []
    for block, tree in python_blocks():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "awa":
                for alias in node.names:
                    if alias.name != "*" and not _exists(str(node.module), alias.name):
                        problems.append(f"{block.where}: from {node.module} import {alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "awa" and not _exists(alias.name, None):
                        problems.append(f"{block.where}: import {alias.name}")
    assert not problems, "names that do not exist:\n" + "\n".join(problems)


def test_every_awa_command_and_option_shown_is_real() -> None:
    from awa.cli import COMMANDS

    problems = []
    for page in pages():
        for block in guide.blocks_of(page):
            if block.lang not in ("console", "bash", "powershell", "shell", "text", ""):
                continue
            for line in block.text.splitlines():
                command = line[2:] if line.startswith("$ ") else line
                if not re.match(r"awa [a-z-]", command):
                    continue
                try:
                    words = shlex.split(command, comments=True)[1:]
                except ValueError:
                    continue
                if not words or words[0].startswith("-"):
                    continue
                if words[0] not in COMMANDS:
                    problems.append(f"{block.where}: no such command {words[0]!r}")
                    continue
                helped = guide.help_text([words[0]])
                depth = 1
                while len(words) > depth and re.search(
                    rf"(?m)^    {re.escape(words[depth])}\s{{2,}}", helped
                ):
                    depth += 1
                    helped = guide.help_text(words[:depth])
                for token in words:
                    option = token.split("=")[0]
                    if re.fullmatch(r"--[a-z][\w-]*", option) and option not in helped:
                        problems.append(f"{block.where}: {option} is not an option of `{command}`")
    assert not problems, "\n".join(problems)


def test_every_repository_path_named_exists() -> None:
    missing = []
    for page in pages():
        if page.name == "roadmap.md":
            continue  # files that are planned
        for path in REPO_PATH.findall(guide.text_of(page)):
            if any(mark in path for mark in "<*{") or path.endswith("/"):
                continue
            if not (ROOT / path).exists():
                missing.append(f"{guide._short_path(page)}: {path}")
    assert not missing, "\n".join(missing)


def test_wording_is_us_english_and_finished() -> None:
    problems = []
    for page in pages():
        text = guide.text_of(page)
        prose = guide.prose(text)
        for word in guide.US_ONLY:
            if re.search(rf"\b{word}\b", prose, re.IGNORECASE):
                problems.append(f"{guide._short_path(page)}: British spelling {word!r}")
        for match in UNFINISHED.finditer(text):
            problems.append(
                f"{guide._short_path(page)}: unfinished or internal: {match.group(0)!r}"
            )
    assert not problems, "\n".join(problems)


def _import_layers() -> str:
    """The layers of `.importlinter`, top to bottom, without the `awa.` prefix."""

    import configparser

    config = configparser.ConfigParser()
    config.read(ROOT / ".importlinter", encoding="utf-8")
    lines = []
    for raw in config["importlinter:contract:layers"]["layers"].strip().splitlines():
        names = [part.strip().removeprefix("awa.") for part in re.split(r"[|:]", raw)]
        lines.append((" | " if "|" in raw else " : ").join(names))
    return "\n".join(lines)


def test_the_architecture_page_shows_the_import_layers_of_the_code() -> None:
    text = guide.text_of(DOCS / "develop" / "architecture.md").replace("\r\n", "\n")
    shown = text.split("<!-- layers:start -->", 1)[1].split("<!-- layers:end -->", 1)[0]
    block = shown.split("```text\n", 1)[1].split("```", 1)[0].strip()
    assert block == _import_layers(), "update the layers block from .importlinter"


def test_every_package_the_architecture_page_names_exists() -> None:
    text = guide.text_of(DOCS / "develop" / "architecture.md")
    missing = [
        name
        for name in sorted(set(re.findall(r"`(awa(?:\.[a-z_]+)+)`", text)))
        if not _exists(name, None) and not _exists(*name.rsplit(".", 1))
    ]
    assert not missing, f"names the architecture page shows that do not exist: {missing}"


def test_the_tokens_and_components_the_design_page_names_exist_in_the_web_app() -> None:
    page = guide.text_of(DOCS / "develop" / "web-app-design.md")
    ui = ROOT / "src" / "awa" / "ui"
    css = (ui / "page.css").read_text("utf-8")
    scripts = "\n".join(path.read_text("utf-8") for path in sorted((ui / "js").glob("*.js")))
    tokens = set(re.findall(r"`(--[a-z0-9-]+)`", page))
    functions = set(re.findall(r"`([a-zA-Z]+)\(", page)) - {"var"}
    missing = [token for token in sorted(tokens) if f"{token}:" not in css]
    missing += [name for name in sorted(functions) if f"function {name}(" not in scripts]
    assert tokens and functions and not missing, f"not in the web app: {missing}"
