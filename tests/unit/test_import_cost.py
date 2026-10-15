"""Importing awa (or its core modules) loads no heavy optional stack, and `awa --help` loads only `argparse` and the command table."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from _helpers.polling import SUBPROCESS_TIMEOUT
from awa.cli import COMMANDS, build_parser

#: Module name prefixes that `import awa` must not load.
HEAVY_PREFIXES = ("langchain", "langgraph", "langsmith")
#: Packages (and their submodules) that `import awa` must not load.
HEAVY_PACKAGES = ("fastapi", "awa.blueprints", "awa.team", "awa.quick")

_PROBE = """
import json, sys
{statement}
{extra}
print(json.dumps(sorted(sys.modules)))
"""


def _loaded_after(statement: str, extra: str = "") -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(statement=statement, extra=extra)],
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    modules: list[str] = json.loads(completed.stdout.strip().splitlines()[-1])
    return modules


def _heavy(
    modules: list[str], *, prefixes: tuple[str, ...], packages: tuple[str, ...]
) -> list[str]:
    return [
        name
        for name in modules
        if name.startswith(prefixes)
        or any(name == package or name.startswith(package + ".") for package in packages)
    ]


def test_import_awa_is_light() -> None:
    modules = _loaded_after(
        "import awa", "assert isinstance(awa.__version__, str) and awa.__version__"
    )
    assert _heavy(modules, prefixes=HEAVY_PREFIXES, packages=HEAVY_PACKAGES) == []


@pytest.mark.parametrize("module", ["awa.errors", "awa.contracts", "awa.runtime"])
def test_import_core_modules_is_light(module: str) -> None:
    modules = _loaded_after(f"import {module}")
    assert _heavy(modules, prefixes=HEAVY_PREFIXES, packages=HEAVY_PACKAGES) == []


def test_the_people_file_loads_without_the_microsoft_connector() -> None:
    # `awa doctor` and `awa people list` read the pinned people; only Entra search needs connectors.
    modules = _loaded_after("from awa.people import PEOPLE_FILE, LocalPeople, PeopleFile")
    assert _heavy(modules, prefixes=HEAVY_PREFIXES, packages=("awa.connectors",)) == []


@pytest.mark.parametrize(
    "statement", ["import awa.automation._decision", "from awa.automation import SqliteRunStore"]
)
def test_reading_process_runs_loads_no_agent_stack(statement: str) -> None:
    # `awa runs decide` and the web app's run views only read and answer stored runs.
    modules = _loaded_after(statement)
    assert _heavy(modules, prefixes=HEAVY_PREFIXES, packages=HEAVY_PACKAGES) == []


def test_importing_agent_defers_the_agent_stack() -> None:
    # LangChain and LangGraph load when an agent gets tools or first runs.
    modules = _loaded_after(
        "from awa import Agent, ModelSettings, AwaError",
        "Agent('probe', instructions='Answer briefly.')",
    )
    assert _heavy(modules, prefixes=HEAVY_PREFIXES, packages=HEAVY_PACKAGES) == []


def test_importing_the_fake_models_defers_the_graph_runtime() -> None:
    modules = _loaded_after("from awa.testing import FakeChatModel, FakeClock, tool_call")
    assert [
        name for name in modules if name.startswith(("langgraph.graph", "langgraph.pregel"))
    ] == []


HELP_MUST_NOT_LOAD = ("langchain", "langgraph", "langsmith", "fastapi", "pydantic", "yaml")
HELP_TEXT_MODULES = frozenset({"awa.cli._descriptions"})


def _imported(command: list[str]) -> set[str]:
    env = {**os.environ, "PYTHONPROFILEIMPORTTIME": "1"}
    completed = subprocess.run(
        command, capture_output=True, text=True, env=env, timeout=SUBPROCESS_TIMEOUT, check=False
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "usage: awa" in completed.stdout
    return {
        line.rsplit("|", 1)[1].strip()
        for line in completed.stderr.splitlines()
        if line.startswith("import time:") and "|" in line
    }


def _awa_script() -> str | None:
    folder = Path(sysconfig.get_path("scripts"))
    for name in ("awa.exe", "awa"):
        if (folder / name).is_file():
            return str(folder / name)
    return shutil.which("awa")


def _assert_light(modules: set[str]) -> None:
    heavy = sorted(name for name in modules if name.startswith(HELP_MUST_NOT_LOAD))
    features = sorted(
        name for name in modules if name.startswith("awa.") and not name.startswith("awa.cli")
    )
    assert heavy == [] and features == []
    assert {"awa", "awa.cli"} <= modules
    # Only the help texts (plain strings, no imports of their own) load with `--help`.
    command_modules = {name for name in modules if name.startswith("awa.cli._")}
    assert command_modules <= HELP_TEXT_MODULES, command_modules - HELP_TEXT_MODULES


def test_python_m_awa_help_imports_no_feature_package() -> None:
    _assert_light(_imported([sys.executable, "-m", "awa", "--help"]))


def test_the_awa_script_help_imports_no_feature_package() -> None:
    script = _awa_script()
    if script is None:
        pytest.skip("the awa console script is not installed in this environment")
    _assert_light(_imported([script, "--help"]))


DOCTOR_MUST_NOT_LOAD = ("mcp", "langchain_mcp_adapters", "awa.blueprints.schema")


def test_awa_doctor_loads_neither_the_mcp_sdk_nor_the_blueprint_schema(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPROFILEIMPORTTIME": "1"}
    completed = subprocess.run(
        [sys.executable, "-m", "awa", "doctor", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert json.loads(completed.stdout)["checks"], completed.stderr[-2000:]
    modules = {
        line.rsplit("|", 1)[1].strip()
        for line in completed.stderr.splitlines()
        if line.startswith("import time:") and "|" in line
    }
    loaded = sorted(
        name
        for name in modules
        if any(name == root or name.startswith(root + ".") for root in DOCTOR_MUST_NOT_LOAD)
    )
    assert loaded == []


def test_every_command_module_defines_its_commands_with_the_listed_help() -> None:
    commands = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert list(commands.choices) == list(COMMANDS)
    listed = {action.dest: action.help for action in commands._choices_actions}
    assert listed == {name: help_text for name, (_, help_text) in COMMANDS.items()}


def _lazy_imports() -> list[tuple[str, str]]:
    """Every `awa` import made inside a function of `awa.cli`, as (module, name)."""

    import ast
    import importlib.util

    spec = importlib.util.find_spec("awa.cli")
    assert spec is not None and spec.submodule_search_locations
    found: list[tuple[str, str]] = []
    for path in Path(next(iter(spec.submodule_search_locations))).glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("awa"):
                    found += [(node.module or "", alias.name) for alias in node.names]
                elif isinstance(node, ast.Import):
                    found += [(a.name, "") for a in node.names if a.name.startswith("awa")]
    return found


def test_every_lazy_import_of_the_cli_resolves() -> None:
    import importlib

    lazy = _lazy_imports()
    assert len(lazy) > 50
    for module, name in lazy:
        loaded = importlib.import_module(module)
        if name and not hasattr(loaded, name):
            importlib.import_module(f"{module}.{name}")
