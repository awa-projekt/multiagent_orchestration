"""Folder-level checks of the automation use cases (`examples/usecases/`).

Every folder: the blueprint validates (the only warnings are the settings of the real systems,
which the README names), the README shows the diagram of `awa show --mermaid`, has the
sections for running it automatically and for connecting real systems, and names every setting
the blueprint reads; the data generator is deterministic and the committed `data/` is what it
writes; the cases parse and cover an approval that is granted, one that is rejected and a
prompt injection; every trigger renders its input from a sample payload. The end-to-end runs
through the real worker are in the `test_usecase_*.py` modules.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from _helpers.usecase_support import (
    AUTOMATION_USECASES,
    TEMPLATE_USECASES,
    USECASES,
    cli,
    data_differences,
)
from awa import Blueprint
from awa.automation.blueprint import load_automation_blueprint
from awa.evals import Dataset

IGNORED = ["__pycache__", "out", ".awa", "processed", "failed", "attachments"]


def tools_of(module: ModuleType) -> list[object]:
    return [getattr(module, name) for name in module.__all__]


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def folder(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the use case named by the parameter, as the working directory."""

    target = tmp_path / request.param
    shutil.copytree(
        USECASES / request.param, target, ignore=shutil.ignore_patterns("__pycache__", "out")
    )
    monkeypatch.chdir(target)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)
    return target


use_cases = pytest.mark.parametrize("folder", AUTOMATION_USECASES, indirect=True)


@use_cases
def test_blueprint_is_valid_and_warns_only_about_unset_systems(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = tools_of(load(folder / "tools.py", f"uc_tools_{folder.name}"))
    blueprint = Blueprint.from_yaml(folder / "blueprint.yaml")
    assert blueprint.name == folder.name
    issues = blueprint.check(tools=tools)
    assert not [i for i in issues if i.severity == "error"], issues
    assert all("not configured" in i.message or "not set" in i.message for i in issues), issues
    assert cli("validate", "blueprint.yaml", "--tools", "tools.py") == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"{folder.name}: valid")
    _, spec = load_automation_blueprint("blueprint.yaml")
    assert spec is not None and spec.triggers, "an automation use case has triggers"
    for tool in tools:
        assert (getattr(tool, "__doc__", "") or "").strip(), f"{tool} needs a docstring"


@use_cases
def test_readme_has_the_current_diagram_and_the_operating_sections(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = (folder / "README.md").read_text(encoding="utf-8")
    blueprint = Blueprint.from_yaml(folder / "blueprint.yaml")
    assert blueprint.to_mermaid() in readme, "README diagram is out of date"
    assert cli("show", "blueprint.yaml", "--mermaid") == 0
    assert f"```mermaid\n{capsys.readouterr().out.strip()}\n```" in readme
    for heading in ("## How to run it automatically", "## Connect your real systems"):
        assert heading in readme, heading
    for words in ("awa serve", "awa worker", "awa runs decide", "awa trigger run"):
        assert words in readme, words
    text = (folder / "blueprint.yaml").read_text(encoding="utf-8")
    for variable in sorted(set(re.findall(r"\b([A-Z][A-Z0-9]+_[A-Z0-9_]+)\b", text))):
        assert variable in readme, f"the README does not mention {variable}"
    _, spec = load_automation_blueprint("blueprint.yaml")
    assert spec is not None
    for trigger in spec.triggers:
        assert f"`{trigger.name}`" in readme, f"the README does not explain trigger {trigger.name}"


@use_cases
def test_data_generator_is_deterministic_and_matches_the_committed_data(
    folder: Path, tmp_path: Path
) -> None:
    maker = load(USECASES / folder.name / "make_data.py", f"uc_make_{folder.name}")
    first, second = tmp_path / "first", tmp_path / "second"
    maker.generate(first)
    maker.generate(second)
    assert not data_differences(first, second, ignore=tuple(IGNORED), same_bytes=True)
    committed = data_differences(
        first, USECASES / folder.name / "data", ignore=tuple(IGNORED), same_bytes=False
    )
    assert not committed, committed
    assert any(first.rglob("*")), "the generator wrote nothing"


@use_cases
def test_cases_parse_and_cover_approval_rejection_and_injection(folder: Path) -> None:
    dataset = Dataset.from_yaml(folder / "cases.yaml")
    if folder.name in TEMPLATE_USECASES:
        assert 8 <= len(dataset.cases) <= 12
        assert sum("boundary" in case.tags for case in dataset.cases) >= 2
    else:
        assert 5 <= len(dataset.cases) <= 8
    actions = {d["action"] for case in dataset.cases for d in case.decisions}
    assert {"approve", "reject"} <= actions
    assert any(re.search(r"inject|instruction", c.id) for c in dataset.cases)
    blueprint = Blueprint.from_yaml(folder / "blueprint.yaml")
    agents = {spec.name for spec in blueprint.agents}
    for case in dataset.cases:
        for scorer in case.scorers:
            if isinstance(scorer, str) and scorer.startswith("delegated:"):
                assert scorer.split(":", 1)[1] in agents, (case.id, scorer)


@use_cases
def test_every_trigger_renders_its_input_from_a_sample_payload(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples = {
        "daily": {},
        "weekly": {"date": "2026-10-05"},
        "inbox": json.loads((folder / "data" / "sample_mail.json").read_text(encoding="utf-8"))
        if (folder / "data" / "sample_mail.json").exists()
        else {},
        "export": {"name": "erp_export.xlsx", "path": "data/inbox/in-progress/erp_export.xlsx"},
        "low-stock": {"sku": "MOT-AC-750"},
        "resolved": json.loads((folder / "data" / "sample_alert.json").read_text(encoding="utf-8"))
        if (folder / "data" / "sample_alert.json").exists()
        else {},
    }
    _, spec = load_automation_blueprint("blueprint.yaml")
    assert spec is not None
    for trigger in spec.triggers:
        payload = json.dumps(samples[trigger.name])
        assert (
            cli("trigger", "run", "blueprint.yaml", trigger.name, "--payload", payload, "--dry-run")
            == 0
        )
        assert capsys.readouterr().out.startswith("input:")


def free_form_objects(schema: object, path: str = "$") -> list[str]:
    """Paths of the objects in a tool schema that OpenAI's strict mode refuses: no fixed
    `properties`, or properties that are not all required."""

    found: list[str] = []
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if (
            schema.get("type") == "object"
            and properties
            and (
                schema.get("additionalProperties") is not False
                or schema.get("required") != list(properties)
            )
        ):
            found.append(path)
        if schema.get("type") == "object" and properties is None:
            found.append(path)
        for key, value in schema.items():
            found += free_form_objects(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found += free_form_objects(value, f"{path}[{index}]")
    return found


@pytest.mark.parametrize(
    "name", sorted(p.name for p in USECASES.iterdir() if (p / "tools.py").is_file())
)
def test_tool_parameters_are_typed_so_strict_providers_accept_them(name: str) -> None:
    """An agent with an output schema sends its tools to OpenAI in strict mode, which refuses a
    bare `dict`/`list[dict]` parameter with a 400: parameters are `TypedDict`s (from
    `typing_extensions`, which pydantic needs before 3.12)."""

    tools = load(USECASES / name / "tools.py", f"strict_tools_{name.replace('-', '_')}")
    for tool in tools_of(tools):
        schema = convert_to_openai_tool(tool, strict=True)["function"]["parameters"]
        assert not free_form_objects(schema), (name, tool, free_form_objects(schema))


def test_data_comparison_ignores_how_a_database_was_written_and_empty_folders(
    tmp_path: Path,
) -> None:
    import sqlite3
    from contextlib import closing

    for name, churn in (("a", False), ("b", True)):
        folder = tmp_path / name
        folder.mkdir()
        if name == "a":
            (folder / "inbox").mkdir()  # git keeps no empty folder
        with closing(sqlite3.connect(folder / "x.db")) as database, database:
            database.execute("CREATE TABLE t (n INTEGER)")
            if churn:  # same rows, other pages: the bytes differ, the content does not
                database.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
                database.execute("DELETE FROM t")
            database.execute("INSERT INTO t VALUES (1)")
    left, right = tmp_path / "a", tmp_path / "b"
    assert data_differences(left, right, ignore=(), same_bytes=True) == ["differs: x.db"]
    assert data_differences(left, right, ignore=(), same_bytes=False) == []
    (right / "extra.txt").write_text("x", encoding="utf-8")
    assert data_differences(left, right, ignore=(), same_bytes=False) == ["only in b: extra.txt"]


@pytest.mark.parametrize("folder", ["inventory-replenishment"], indirect=True)
def test_a_low_stock_alert_is_deduplicated_per_day(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.dumps({"sku": "MOT-AC-750"})
    assert (
        cli("trigger", "run", "blueprint.yaml", "low-stock", "--payload", payload, "--dry-run") == 0
    )

    assert re.search(
        r"dedup_key: low-stock-MOT-AC-750-\d{4}-\d\d-\d\d$", capsys.readouterr().out, re.M
    )
