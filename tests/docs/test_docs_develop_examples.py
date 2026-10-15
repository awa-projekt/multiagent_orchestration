"""The runnable examples of the developer pages are executed, and what the pages print is real.

A page marks its example files with a title on the code fence: ```python title="statement_parser.py"
```. This test writes every titled block of a page into a fresh folder and then

* runs each `<name>.py` that has a block titled `<name>.out` and compares what it prints with that
  block, line by line;
* runs every `test_*.py` with pytest (async tests included) and expects them to pass;
* checks that every other titled `.py` file is imported or started by one of them, so no example
  is left unexecuted.

So a page cannot show output that the code does not produce, and an example cannot rot. The docs
folder is part of the repository only, so the tests skip without it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
DEVELOP = ROOT / "docs" / "develop"

pytestmark = pytest.mark.skipif(not DEVELOP.is_dir(), reason="docs/ is in the repository only")

FENCE = re.compile(
    r'^```(?P<lang>[\w+-]*)[ \t]+title="(?P<title>[^"]+)"[^\n]*\n(?P<body>.*?)^```[ \t]*$',
    re.MULTILINE | re.DOTALL,
)
REQUIRED = ["quickstart.md", "agents.md", "extend.md"]
"""Pages that must keep runnable examples; every other page with titled blocks is run as well."""
PYTEST_INI = "[pytest]\nasyncio_mode = auto\n"


@dataclass(frozen=True)
class Block:
    lang: str
    title: str
    body: str


def blocks(page: str) -> list[Block]:
    text = (DEVELOP / page).read_text("utf-8").replace("\r\n", "\n")
    return [Block(m["lang"], m["title"], m["body"]) for m in FENCE.finditer(text)]


def write_example(page: str, folder: Path) -> list[Block]:
    """Write the titled blocks of `page` into `folder`; return them."""

    found = blocks(page)
    for block in found:
        target = folder / block.title
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(block.body, encoding="utf-8", newline="\n")
    if not (folder / "pytest.ini").exists():
        (folder / "pytest.ini").write_text(PYTEST_INI, encoding="utf-8")
    return found


def run(folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(folder), str(ROOT / "src"), *filter(None, [env.get("PYTHONPATH")])]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("AWA_MODEL", None)
    return subprocess.run(
        [sys.executable, *args],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SLOW_SUBPROCESS_TIMEOUT,
        check=False,
    )


def pages_with_blocks() -> list[str]:
    if not DEVELOP.is_dir():
        return []
    return sorted(page.name for page in DEVELOP.glob("*.md") if blocks(page.name))


def test_every_example_page_exists_and_has_runnable_blocks() -> None:
    missing = [page for page in REQUIRED if page not in pages_with_blocks()]
    assert not missing, f"pages without titled example blocks: {missing}"


@pytest.mark.parametrize("page", pages_with_blocks())
def test_every_titled_python_file_is_run_tested_or_imported(page: str) -> None:
    found = blocks(page)
    titles = {block.title for block in found}
    imported = {
        name
        for block in found
        if block.title.endswith(".py")
        for name in re.findall(r"^(?:from|import) (\w+)", block.body, re.MULTILINE)
    }
    idle = [
        block.title
        for block in found
        if block.title.endswith(".py")
        and not Path(block.title).name.startswith("test_")
        and Path(block.title).name != "conftest.py"
        and block.title.removesuffix(".py") + ".out" not in titles
        and Path(block.title).stem not in imported
        and not any(
            block.title in other.body or f'"{Path(block.title).parent.as_posix()}"' in other.body
            for other in found
            if other is not block
        )
    ]
    assert not idle, f"{page}: titled files that nothing runs: {idle}"


@pytest.mark.parametrize("page", pages_with_blocks())
def test_the_printed_output_of_each_example_is_what_the_code_prints(
    page: str, tmp_path: Path
) -> None:
    found = write_example(page, tmp_path)
    outputs = [b for b in found if b.title.endswith(".out")]
    for expected in outputs:
        script = expected.title.removesuffix(".out") + ".py"
        assert any(b.title == script for b in found), f"{page}: {expected.title} has no {script}"
        done = run(tmp_path, script)
        assert done.returncode == 0, f"{page}: {script} failed:\n{done.stderr[-2000:]}"
        shown = [line.rstrip() for line in expected.body.strip().splitlines()]
        printed = [line.rstrip() for line in done.stdout.strip().splitlines()]
        assert printed == shown, f"{page}: {script} prints something else than the page shows"


@pytest.mark.parametrize("page", pages_with_blocks())
def test_the_tests_shown_on_each_page_pass(page: str, tmp_path: Path) -> None:
    found = write_example(page, tmp_path)
    tests = [b.title for b in found if Path(b.title).name.startswith("test_")]
    if not tests:
        pytest.skip(f"{page} shows no test file")
    done = run(
        tmp_path, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", *tests
    )
    assert done.returncode == 0, f"{page}:\n{done.stdout[-3000:]}\n{done.stderr[-1000:]}"


def test_the_store_suite_catches_an_adapter_that_stores_what_json_cannot(tmp_path: Path) -> None:
    """The page says which test fails when the backend forgets to refuse NaN: keep it true."""

    page = "extend.md"
    write_example(page, tmp_path)
    module = tmp_path / "sqlite_artifacts.py"
    source = module.read_text("utf-8")
    assert "allow_nan=False" in source, "the example must refuse NaN with allow_nan=False"
    module.write_text(source.replace(", allow_nan=False", ""), encoding="utf-8")
    test = "test_sqlite_artifacts.py"
    done = run(tmp_path, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", test)
    assert done.returncode != 0
    shown = re.findall(r"`(test_\w+)` fails", (DEVELOP / page).read_text("utf-8"))
    assert shown, "the page names the failing test as: `test_name` fails"
    assert all(name in done.stdout for name in shown), done.stdout[-3000:]


def test_the_store_example_closes_its_connections(tmp_path: Path) -> None:
    write_example("extend.md", tmp_path)
    done = run(
        tmp_path,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:randomly",
        "-p",
        "no:cacheprovider",
        "-W",
        "error::ResourceWarning",
        "-W",
        "error::pytest.PytestUnraisableExceptionWarning",
        "test_sqlite_artifacts.py",
    )
    assert done.returncode == 0, done.stdout[-3000:]
