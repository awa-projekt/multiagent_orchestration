"""The README, the home page, the start pages and the user pages are checked like the guide.

What is checked: every internal link and anchor resolves, every image exists, has alt text and is
used, wording (US spelling, no repository or model id), every `awa` command and option shown is
real, YAML and Python blocks parse, and the README and the home page lead to the start pages.
Blocks marked as in the guide (`<!-- guide:run -->`) are executed by `test_the_marked_examples_run`.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import test_guide as guide

ROOT = guide.ROOT
DOCS = guide.DOCS
SHOTS = DOCS / "assets" / "screenshots"

pytestmark = pytest.mark.skipif(
    not guide.GUIDE.is_dir() or not guide.CAPTURE.is_file(),
    reason="docs/ and scripts/ are in the repository only",
)


PROJECT_REPOSITORY = "https://github.com/awa-projekt/multiagent_orchestration"


def pages() -> list[Path]:
    found = [ROOT / "README.md", ROOT / "TOUR.md", ROOT / "DEVELOPERS.md", DOCS / "index.md"]
    found += sorted((DOCS / "start").glob("*.md"))
    return found


@pytest.mark.skipif(importlib.util.find_spec("markdown") is None, reason="needs the docs extra")
def test_every_link_and_anchor_resolves() -> None:
    problems = []
    for page in pages():
        for number, line in enumerate(guide.outside_fences(guide.text_of(page)).splitlines(), 1):
            for target in re.findall(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", line):
                if re.match(r"(https?:|mailto:)", target):
                    continue
                path, _, anchor = target.partition("#")
                resolved = page if not path else (page.parent / path).resolve()
                where = f"{guide._short_path(page)}:{number}: {target}"
                if not resolved.exists():
                    problems.append(f"{where} does not exist")
                elif (
                    anchor and resolved.suffix == ".md" and anchor not in guide.anchors_of(resolved)
                ):
                    problems.append(f"{where}: no heading or id {anchor!r}")
    assert not problems, "\n".join(problems)


def test_every_image_exists_has_alt_text_and_is_used() -> None:
    used, problems = set(), []
    for page in pages():
        for number, line in enumerate(guide.text_of(page).splitlines(), 1):
            for alt, path in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", line):
                target = (page.parent / path).resolve()
                if not target.is_file():
                    problems.append(f"{guide._short_path(page)}:{number}: {path} is missing")
                    continue
                used.add(target)
                if len(alt.split()) < 5 and "badges" not in path:
                    problems.append(f"{guide._short_path(page)}:{number}: alt text too short")
    # pictures that README and the docs pages (also outside this list) show
    for page in DOCS.rglob("*.md"):
        used |= {
            (page.parent / p).resolve()
            for p in re.findall(r"!\[[^\]]*\]\(([^)]+\.png)\)", page.read_text(encoding="utf-8"))
        }
    top = {p.resolve() for p in SHOTS.glob("*.png")}
    assert not problems, "\n".join(problems)
    assert not (top - used), f"pictures no page shows: {sorted(p.name for p in top - used)}"


def test_wording_us_spelling_no_repository_no_model_id_no_internal_id() -> None:
    problems = []
    for page in pages():
        # The project's own repository is allowed: the start pages show the install from GitHub.
        text = guide.text_of(page).replace(PROJECT_REPOSITORY, "")
        for word in guide.US_ONLY:
            if re.search(
                rf"\b{word}\b", re.sub(r"[*][*][^*]+[*][*]", "", guide.prose(text)), re.IGNORECASE
            ):
                problems.append(f"{guide._short_path(page)}: British spelling {word!r}")
        for pattern, what in guide.NOT_ALLOWED:
            if "model id" in what or "package-index" in what:
                continue  # the docs name the ids of the Current models table; tested elsewhere
            for match in re.finditer(pattern, text, re.IGNORECASE):
                problems.append(f"{guide._short_path(page)}: {what}: {match.group(0)!r}")
    assert not problems, "\n".join(problems)


def test_us_spelling_in_every_docs_page_and_the_top_level_files() -> None:
    """US spelling everywhere a reader reads: all of docs/ (German pages in docs/de excepted),
    the Markdown files of the repository root. Code, quoted wording (in double quotes,
    for example a law's own term) and block quotes are not our prose and are skipped."""

    files = [
        *(p for p in DOCS.rglob("*.md") if "de" not in p.relative_to(DOCS).parts[:1]),
        *ROOT.glob("*.md"),
    ]
    problems = []
    for page in files:
        text = guide.prose(guide.text_of(page))
        text = re.sub(r"(?m)^\s*>.*$", "", text)  # block quotes
        text = re.sub(r'"[^"\n]*"', "", text)  # quoted wording
        for word in guide.US_ONLY:
            for match in re.finditer(rf"\b{word}\b", text, re.IGNORECASE):
                problems.append(f"{guide._short_path(page)}: British spelling {match.group(0)!r}")
    assert not problems, "\n".join(problems)


def test_yaml_and_python_blocks_parse() -> None:
    import ast

    import yaml

    problems = []
    for page in pages():
        for block in guide.blocks_of(page):
            try:
                if block.lang == "python":
                    ast.parse(block.text)
                elif block.lang in ("yaml", "yml") and "..." not in block.text:
                    yaml.safe_load(block.text)
            except (SyntaxError, yaml.YAMLError) as error:
                problems.append(f"{block.where}: {block.lang} does not parse: {error}")
    assert not problems, "\n".join(problems)


def test_every_awa_command_and_option_shown_is_real() -> None:
    import shlex

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
                depth = 1  # `awa setup services up`: follow the actions down
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


def test_the_readme_and_the_home_page_lead_to_the_start_pages() -> None:
    for page in (ROOT / "README.md", DOCS / "index.md"):
        text = guide.text_of(page)
        for name in ("start/install.md", "start/first-automation.md"):
            assert name in text, f"{guide._short_path(page)} does not link {name}"


def test_the_marked_examples_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marked = [p for p in pages() if "<!-- guide:" in guide.text_of(p)]
    if not marked:
        pytest.skip("no page carries an executable example")
    work = guide.Work(tmp_path / "awa-guide-work", monkeypatch)
    work.root.mkdir()
    problems = guide.run_guide(work, marked)
    assert not problems, "\n\n".join(problems)


def test_every_python_path_of_the_project_folder_page_exists() -> None:
    import pkgutil

    from awa.cli import COMMANDS

    text = guide.text_of(DOCS / "develop" / "project-folder.md")
    rows = re.findall(r"(?m)^\| ([^|`]+?) \| (.+?) \| (.+?) \|$", text.split("## Every step")[1])
    assert len(rows) >= 14
    for step, commands, python in rows[1:]:
        for command in re.findall(r"`awa ([a-z-]+)", commands):
            assert command in COMMANDS, f"{step}: no command {command!r}"
        for dotted in re.findall(r"`(awa(?:\.\w+)+)`", python):
            assert callable(pkgutil.resolve_name(dotted)), f"{step}: {dotted} is not callable"


def test_the_install_from_github_comes_first_and_names_the_project_repository() -> None:
    install = f'pip install "awa-agents[all] @ git+{PROJECT_REPOSITORY}"'
    for page in (ROOT / "README.md", DOCS / "start" / "install.md"):
        text = page.read_text(encoding="utf-8")
        assert install in text, page.name
        assert text.index(install) < text.index('pip install ".[all]"') < text.index("install-"), (
            page
        )


def test_the_package_metadata_links_the_repository_the_readme_names() -> None:
    import tomllib

    urls = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["urls"]

    assert urls["Homepage"] == urls["Source"] == PROJECT_REPOSITORY
    assert urls["Issues"] == f"{PROJECT_REPOSITORY}/issues"
    assert urls["Changelog"] == f"{PROJECT_REPOSITORY}/blob/main/CHANGELOG.md"
    assert (ROOT / "CHANGELOG.md").is_file()
