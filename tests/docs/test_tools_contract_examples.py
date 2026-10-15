"""Every built-in tool has an executed example on docs/reference/tools.md.

The page marks its files with a title on the code fence: ```python title="tool_read_file.py"```. This
test writes every titled block into a fresh folder, runs each `tool_<name>.py` that has a block
titled `tool_<name>.out` and compares what it prints with that block, line by line. So the page
cannot show output the code does not produce. Everything is offline (temporary files, a web server
on this computer, a function instead of a mail server, a scripted model).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT
from awa.toolkits import catalog

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "reference" / "tools.md"

pytestmark = pytest.mark.skipif(not PAGE.is_file(), reason="docs/ is in the repository only")

FENCE = re.compile(
    r'^```(?P<lang>[\w+-]*)[ \t]+title="(?P<title>[^"]+)"[^\n]*\n(?P<body>.*?)^```[ \t]*$',
    re.MULTILINE | re.DOTALL,
)


def titled_blocks() -> dict[str, str]:
    text = PAGE.read_text("utf-8").replace("\r\n", "\n")
    return {m["title"]: m["body"] for m in FENCE.finditer(text)}


def script_of(name: str) -> str:
    return "tool_" + name.replace(".", "_") + ".py"


def test_every_tool_of_the_catalog_has_an_example_and_its_printed_output() -> None:
    blocks = titled_blocks()
    missing = [
        entry.name
        for entry in catalog()
        if script_of(entry.name) not in blocks or script_of(entry.name)[:-3] + ".out" not in blocks
    ]
    assert not missing, f"tools without an example and output block: {missing}"
    known = {script_of(entry.name) for entry in catalog()} | {"sandbox.py", "demo_site.py"}
    scripts = {title for title in blocks if title.endswith(".py")}
    assert scripts == known, f"examples for unknown tools or helper files: {scripts ^ known}"


@pytest.fixture(scope="module")
def example_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("tool_examples")
    for title, body in titled_blocks().items():
        (folder / title).write_text(body, encoding="utf-8", newline="\n")
    return folder


@pytest.mark.parametrize("name", [entry.name for entry in catalog()])
def test_the_printed_output_of_each_example_is_what_the_code_prints(
    name: str, example_folder: Path
) -> None:
    script = script_of(name)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(example_folder), str(ROOT / "src"), *filter(None, [env.get("PYTHONPATH")])]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("AWA_MODEL", None)
    done = subprocess.run(
        [sys.executable, script],
        cwd=example_folder,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SLOW_SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert done.returncode == 0, f"{script} failed:\n{done.stderr[-2000:]}"
    shown = [line.rstrip() for line in titled_blocks()[script[:-3] + ".out"].strip().splitlines()]
    printed = [line.rstrip() for line in done.stdout.strip().splitlines()]
    assert printed == shown, f"{script} prints something else than the page shows"
