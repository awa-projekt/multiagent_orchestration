"""The Reference pages: the examples print what the pages show, every public package has a
page, the console lines parse, the exit code table matches the command line, and the cheat sheet
shows every command, imports only names that exist and holds a blueprint that validates."""

from __future__ import annotations

import argparse
import ast
import importlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "reference"
API = REFERENCE / "api"

pytestmark = pytest.mark.skipif(not API.is_dir(), reason="docs/ is in the repository only")

FENCE = re.compile(
    r'^```(?P<lang>[\w+-]*)[ \t]+title="(?P<title>[^"]+)"[^\n]*\n(?P<body>.*?)^```[ \t]*$',
    re.MULTILINE | re.DOTALL,
)
DIRECTIVE = re.compile(r"^::: (awa(?:\.\w+)*)$", re.MULTILINE)
CONSOLE = re.compile(r"^```console\n(?P<body>.*?)^```", re.MULTILINE | re.DOTALL)
PYTHON = re.compile(r"^```python\n(?P<body>.*?)^```", re.MULTILINE | re.DOTALL)
BLUEPRINT = re.compile(r"^```yaml\n(?P<body>.*?)^```", re.MULTILINE | re.DOTALL)
CHEATSHEET = REFERENCE / "cheatsheet.md"
# The command line, the web app and the setup checks are used through `awa`, not imported.
NOT_IMPORTED = {"cli", "ui", "checks", "project_setup"}


def _pages() -> list[Path]:
    return sorted(API.glob("*.md"))


@pytest.mark.parametrize(
    "page", [*_pages(), REFERENCE / "exceptions.md"], ids=lambda page: page.stem
)
def test_the_example_of_each_api_page_prints_what_the_page_shows(
    page: Path, tmp_path: Path
) -> None:
    blocks = {m["title"]: m["body"] for m in FENCE.finditer(page.read_text("utf-8"))}
    scripts = [title for title in blocks if title.endswith(".py")]
    assert scripts, f"{page.name}: no titled example"
    for script in scripts:
        (tmp_path / script).write_text(blocks[script], encoding="utf-8", newline="\n")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")])
        env.pop("AWA_MODEL", None)
        done = subprocess.run(
            [sys.executable, script],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=SLOW_SUBPROCESS_TIMEOUT,
            check=False,
        )
        assert done.returncode == 0, f"{page.name}: {script} failed:\n{done.stderr[-2000:]}"
        shown = blocks[script.removesuffix(".py") + ".out"].strip().splitlines()
        assert [line.rstrip() for line in done.stdout.strip().splitlines()] == shown


def test_every_public_package_is_on_an_api_page_and_every_directive_imports() -> None:
    import importlib

    documented = {name for page in _pages() for name in DIRECTIVE.findall(page.read_text("utf-8"))}
    folders = [path.parent.name for path in (ROOT / "src" / "awa").glob("*/__init__.py")]
    packages = {
        f"awa.{name}"
        for name in folders
        if not name.startswith("_")
        and name not in NOT_IMPORTED
        and getattr(importlib.import_module(f"awa.{name}"), "__all__", ())
    }
    assert packages <= documented, f"packages without an API page: {packages - documented}"
    for name in documented:
        assert importlib.import_module(name).__all__, name


def test_every_console_line_of_the_reference_parses() -> None:
    from awa.cli import build_parser

    lines = [
        line.split("#")[0].strip()
        for page in REFERENCE.glob("*.md")
        for block in CONSOLE.finditer(page.read_text("utf-8"))
        for line in block["body"].splitlines()
    ]
    commands = [shlex.split(line)[1:] for line in lines if line.startswith("awa ")]
    assert len(commands) >= 5
    for words in commands:
        if words[-1] != "--help":
            build_parser(words).parse_args(words)
            continue
        parser = build_parser(words)
        for word in words[:-1]:  # every word before --help names a command or subcommand
            parser = next(
                action.choices[word]
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            )


def test_the_exit_code_table_lists_every_exit_code_of_the_command_line() -> None:
    from awa.cli import _common

    codes = {value for name, value in vars(_common).items() if name.startswith("EXIT_")}
    page = (REFERENCE / "cli.md").read_text("utf-8")
    table = page.split("## Exit codes", 1)[1]
    shown = {int(code) for code in re.findall(r"^\| (\d+) \|", table, re.MULTILINE)}
    assert codes <= shown, codes - shown
    assert shown - codes == {130}, "only Ctrl+C has no constant"


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _commands(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
    """`prefix` and every subcommand below it, as word tuples."""

    found = [prefix]
    for name, child in _subcommands(parser).items():
        found += _commands(child, (*prefix, name))
    return found


def test_the_cheat_sheet_shows_every_command_and_subcommand() -> None:
    from awa.cli import COMMANDS, build_parser

    shown = {
        tuple(shlex.split(line.split("#")[0])[1:])
        for block in CONSOLE.finditer(CHEATSHEET.read_text("utf-8"))
        for line in block["body"].splitlines()
        if line.startswith("awa ")
    }
    missing = []
    for name in COMMANDS:
        top = _subcommands(build_parser([name]))[name]
        for words in _commands(top, (name,)):
            if not any(line[: len(words)] == words for line in shown):
                missing.append("awa " + " ".join(words))
    assert not missing, f"commands without a line on the cheat sheet: {missing}"


def test_the_python_of_the_cheat_sheet_parses_and_imports_only_names_that_exist() -> None:
    missing = []
    for block in PYTHON.finditer(CHEATSHEET.read_text("utf-8")):
        for node in ast.walk(ast.parse(block["body"])):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "awa":
                module = importlib.import_module(str(node.module))
                missing += [
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if not hasattr(module, alias.name)
                ]
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "awa":
                        importlib.import_module(alias.name)
    assert not missing, f"names that do not exist: {missing}"


def test_the_blueprint_of_the_cheat_sheet_validates(tmp_path: Path) -> None:
    from awa import Blueprint

    block = BLUEPRINT.search(CHEATSHEET.read_text("utf-8"))
    assert block, "the cheat sheet shows no blueprint"
    (tmp_path / "data").mkdir()  # the root of its files toolkit
    path = tmp_path / "blueprint.yaml"
    path.write_text(block["body"], encoding="utf-8")
    issues = Blueprint.from_yaml(path).check()
    errors = [str(issue) for issue in issues if issue.severity == "error"]
    assert not errors, errors
