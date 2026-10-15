"""A core-only install (the base dependencies, no extra) imports and runs awa.

Slow and needs a package index or cache: skipped by default. Run it with
`python -m pytest -m core_install tests/unit/test_core_install.py`. With `AWA_TEST_WHEEL` set,
the environment gets that wheel (without its dependencies); otherwise awa comes from `src`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT
from _helpers.wheel import prebuilt_wheel
from awa._internal.install import safe_find_spec
from awa._internal.util import importable
from awa.toolkits._common import extra_installed

ROOT = Path(__file__).resolve().parents[2]

BUILD_A_TEAM = """
import importlib.util, sys
from langchain_core.messages import ToolMessage

from awa import quick
from awa.testing import FunctionChatModel, tool_call


def respond(call):
    if any(isinstance(m, ToolMessage) for m in call.messages):
        return "INV-1 is fine."
    if "delegate_task" in call.tool_names:
        return tool_call("delegate_task", {"agent": "checker", "task": "Check INV-1."})
    return "Totals match."


desk = quick.team(
    "Invoice processing",
    lead="coordinator",
    members={"checker": "Validates the totals against the purchase order."},
    model=FunctionChatModel(respond),
)
assert desk.run_sync("Process INV-1.").output == "INV-1 is fine."
extras = ("fastapi", "uvicorn", "mcp", "openai", "langchain_openai", "langchain_anthropic",
          "psycopg", "openpyxl", "pypdf", "docx", "opentelemetry")
assert [e for e in extras if importlib.util.find_spec(e)] == [], "an extra is installed"
print("core-only ok")
"""


def _run(python: Path, *args: str, cwd: Path, source: bool) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for name in [n for n in env if n.startswith("AWA_") or n == "PYTHONPATH"]:
        del env[name]
    if source:
        env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [str(python), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=SLOW_SUBPROCESS_TIMEOUT,
    )


@pytest.mark.core_install
def test_core_only_install_imports_and_builds_a_team(tmp_path: Path) -> None:
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"][
        "dependencies"
    ]
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    uv = shutil.which("uv")
    install = (
        [uv, "pip", "install", "--python", str(python)]
        if uv
        else [str(python), "-m", "pip", "install", "--quiet"]
    )
    subprocess.run([*install, *dependencies], check=True, timeout=900)
    wheel = prebuilt_wheel()
    if wheel is not None:
        subprocess.run([*install, "--no-deps", str(wheel)], check=True, timeout=900)
    source = wheel is None

    found = _run(python, "-c", "import awa; print(awa.__file__)", cwd=tmp_path, source=source)
    assert found.returncode == 0, found.stderr
    assert (venv in Path(found.stdout.strip()).parents) is not source
    helped = _run(python, "-m", "awa", "--help", cwd=tmp_path, source=source)
    assert helped.returncode == 0 and "doctor" in helped.stdout, helped.stderr
    built = _run(python, "-c", BUILD_A_TEAM, cwd=tmp_path, source=source)
    assert built.returncode == 0 and "core-only ok" in built.stdout, built.stderr[-2000:]


def test_safe_find_spec_never_raises_and_is_the_one_module_probe() -> None:
    assert safe_find_spec("json") is not None
    for name in ("awa_no_such_module", "awa_no_such_package.sub", ""):
        assert safe_find_spec(name) is None
        assert not importable(name)
        assert not extra_installed(name)
    assert importable("json") and extra_installed("json")
