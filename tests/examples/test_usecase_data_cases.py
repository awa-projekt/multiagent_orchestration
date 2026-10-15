"""Offline checks of the data-based evaluation use cases (`DATA_USECASES` in `usecase_support`).

Each folder computes its expected answers from its data with `make_cases.py`. The tests check:

- the blueprint validates against its tools with no issue, and the README shows its diagram;
- `make_data.py` is deterministic and the committed `data/` is what it writes (for a use case
  built from downloaded open data, the check runs on the committed excerpt instead);
- `cases.yaml` is exactly what `make_cases.py` computes from the committed data, has 12 to 20
  cases, an approval that is granted, one that is rejected and a prompt injection;
- every case passes on a scripted model that replays the case's reference solution against the
  real tools and data: it makes the reads of `metadata.steps` and the writes of
  `metadata.calls`, checks that the tool results show every value of `metadata.evidence`, and
  answers with `metadata.answer`. So the scorers agree with the reference answer, and the
  reference answer is reachable through the tools the blueprint gives the agent;
- `awa eval` runs the cases from the command line with the same scripted model.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.conversation import request
from _helpers.usecase_support import DATA_USECASES, USECASES, data_differences
from awa import Blueprint
from awa.evals import Dataset, evaluate
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, tool_call

IGNORED = ("outbox", "__pycache__", "ORIGIN.md")


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tools_of(folder: Path) -> list[Any]:
    module = load(folder / "tools.py", f"awa_data_usecase_{folder.name.replace('-', '_')}_tools")
    return [getattr(module, name) for name in module.__all__]


@pytest.fixture
def folder(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the use case named by the parameter, as the working directory."""

    target = tmp_path / request.param
    shutil.copytree(
        USECASES / request.param, target, ignore=shutil.ignore_patterns("__pycache__", "outbox")
    )
    monkeypatch.chdir(target)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)
    return target


use_cases = pytest.mark.parametrize("folder", DATA_USECASES, indirect=True)


class ReplayScript:
    """Plays the agent of a data use case by replaying each case's reference solution."""

    def __init__(self, dataset: Dataset) -> None:
        self.cases = {str(case.input).strip(): case for case in dataset.cases}
        self.missing: list[str] = []  # evidence a case's tool results did not show

    def registry(self) -> ModelRegistry:
        registry = ModelRegistry()
        registry.register("default", FunctionChatModel(self))
        return registry

    def __call__(self, call: ModelCall) -> Any:
        case = self.cases[request(call).strip()]
        plan = [*case.metadata.get("steps", []), *case.metadata.get("calls", [])]
        done = sum(1 for m in call.messages if isinstance(m, AIMessage) and m.tool_calls)
        if done < len(plan):
            return tool_call(plan[done]["tool"], plan[done]["args"])
        seen = "\n".join(str(m.content) for m in call.messages if isinstance(m, ToolMessage))
        self.missing += [
            f"{case.id}: {value!r}"
            for value in case.metadata.get("evidence", [])
            if value not in seen
        ]
        return str(case.metadata["answer"])


@use_cases
def test_blueprint_is_valid_and_the_readme_shows_it(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    tools = tools_of(folder)
    blueprint = Blueprint.from_yaml(folder / "blueprint.yaml")
    assert blueprint.name == folder.name
    assert blueprint.check(tools=tools) == []
    assert main(["validate", "blueprint.yaml", "--tools", "tools.py"]) == 0
    assert capsys.readouterr().out.strip() == f"{folder.name}: valid"
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert blueprint.to_mermaid() in readme, "README diagram is out of date"
    assert (folder / "data" / "ORIGIN.md").is_file(), "every data folder names its origin"
    for tool in tools:
        assert (getattr(tool, "__doc__", "") or "").strip(), f"{tool} needs a docstring"


@use_cases
def test_data_generator_is_deterministic_and_matches_the_committed_data(
    folder: Path, tmp_path: Path
) -> None:
    maker = load(folder / "make_data.py", f"awa_data_usecase_{folder.name.replace('-', '_')}_make")
    if getattr(maker, "DOWNLOADED", False):  # built from open data: rebuild from the excerpt
        first, second = tmp_path / "first", tmp_path / "second"
        maker.rebuild(folder / "data", first)
        maker.rebuild(folder / "data", second)
    else:
        first, second = tmp_path / "first", tmp_path / "second"
        maker.generate(first)
        maker.generate(second)
    assert any(first.rglob("*")), "the generator wrote nothing"
    assert not data_differences(first, second, ignore=IGNORED, same_bytes=True)
    committed = data_differences(first, folder / "data", ignore=IGNORED, same_bytes=False)
    assert not committed, committed


@use_cases
def test_cases_are_computed_from_the_data(folder: Path) -> None:
    maker = load(
        folder / "make_cases.py", f"awa_data_usecase_{folder.name.replace('-', '_')}_cases"
    )
    committed = (folder / "cases.yaml").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert maker.render(folder / "data") == committed, "run make_cases.py again"
    dataset = Dataset.from_yaml(folder / "cases.yaml")
    assert dataset.name == folder.name
    assert 12 <= len(dataset.cases) <= 20
    actions = {d["action"] for case in dataset.cases for d in case.decisions}
    assert {"approve", "reject"} <= actions
    assert any("injection" in case.tags for case in dataset.cases)
    judges = [s for case in dataset.cases for s in case.scorers if s.startswith("judge:")]
    assert len(judges) <= 1, "the answers are checked by rules, at most one judge"
    for case in dataset.cases:
        assert case.metadata.get("answer"), f"{case.id} needs a reference answer"


@use_cases
async def test_every_case_passes_on_its_reference_solution(folder: Path) -> None:
    dataset = Dataset.from_yaml(folder / "cases.yaml")
    script = ReplayScript(dataset)
    blueprint = Blueprint.from_yaml(folder / "blueprint.yaml")
    tools = tools_of(folder)

    def build() -> Any:
        return blueprint.build(
            tools=tools,
            model_registry=script.registry(),
            checkpointer=InMemorySaver() if blueprint.approval_agents() else None,
        )

    report = await evaluate(build, dataset, concurrency=1)
    assert report.pass_rate == 1.0, report.to_markdown()
    assert not script.missing, script.missing


@use_cases
def test_cli_eval_passes(
    folder: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    script = ReplayScript(Dataset.from_yaml(folder / "cases.yaml"))
    monkeypatch.setattr(ModelRegistry, "_default", script.registry())
    code = main(
        ["eval", "blueprint.yaml", "cases.yaml", "--format", "markdown", "--tools", "tools.py"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "- Pass rate: **100.0%**" in out


def test_the_data_use_cases_are_folders() -> None:
    for name in DATA_USECASES:
        assert (USECASES / name / "blueprint.yaml").is_file(), name
