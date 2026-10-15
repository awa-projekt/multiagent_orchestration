"""An export runs in a fresh environment that has never seen awa.

Installing the export's requirements reaches the package index: run it with `pytest -m live_network`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from awa.exporter import export

pytestmark = [pytest.mark.slow, pytest.mark.live_network]

DRIVER = """\
import sys
sys.path[:0] = ["src", "tests"]
from fakes import ScriptedChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from desk.graph import build_graph

call = AIMessage(content="", tool_calls=[{"name": "ship", "args": {"order_id": "o"}, "id": "1"}])
graph = build_graph(ScriptedChatModel(script=[call, AIMessage(content="sent")]), InMemorySaver())
config = {"configurable": {"thread_id": "t"}}
paused = graph.invoke({"messages": [("user", "ship")]}, config)
print("PAUSED", paused["__interrupt__"][0].value["action_requests"][0]["name"])
done = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
print("DONE", done["messages"][-1].content)
"""


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def test_the_export_runs_in_a_fresh_environment_without_awa(desk: Path, tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("the isolation proof needs uv on the path")
    out = tmp_path / "desk-export"
    export(desk, out)
    venv = tmp_path / "venv"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(venv)], check=True, capture_output=True
    )
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    installed = _run([uv, "pip", "install", "--python", str(python), "-r", "requirements.txt"], out)
    assert installed.returncode == 0, installed.stderr[-2000:]

    assert _run([str(python), "-c", "import awa"], out).returncode != 0
    tests = _run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"], out)
    assert tests.returncode == 0, tests.stdout[-2000:]
    assert _run([str(python), "-c", "import run"], out).returncode == 0
    (out / "drive.py").write_text(DRIVER, encoding="utf-8")
    driven = _run([str(python), "drive.py"], out)
    assert driven.returncode == 0, driven.stderr[-2000:]
    assert "PAUSED ship" in driven.stdout
    assert "DONE sent" in driven.stdout
