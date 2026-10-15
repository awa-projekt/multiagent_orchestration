"""Exported projects behave like the awa system, and every shipped example exports."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from _helpers.export_projects import (
    DESK_BLUEPRINT,
    STRUCTURED_BLUEPRINT,
    run_exported_tests,
    write_project,
)
from _helpers.scripted_model import ScriptedChatModel
from awa.blueprints import Blueprint, load_tools, tools_files
from awa.cli import main
from awa.exporter import ExportUnsupportedError, export
from awa.models import ModelRegistry
from awa.skills import SkillRegistry
from awa.testing import FakeChatModel


def _call(name: str, args: dict[str, Any], ident: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": ident}])


def _awa_agent(project: Path, script: list[AIMessage]) -> Any:
    path = project / "blueprint.yaml"
    registry = ModelRegistry()
    registry.register("scripted", FakeChatModel(script))
    skills = project / "skills"
    return Blueprint.from_yaml(path).build(
        tools=load_tools(tools_files([], path)),
        model_registry=registry,
        skill_registry=(
            SkillRegistry.from_directory(skills, include_builtin=True) if skills.is_dir() else None
        ),
        checkpointer=InMemorySaver(),
    )


@pytest.fixture
def exported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Export a project and import its graph module; returns `(import, project)` helper."""

    def load(project: Path, package: str) -> Any:
        export(project, tmp_path / "out")
        monkeypatch.syspath_prepend(str(tmp_path / "out" / "src"))
        monkeypatch.syspath_prepend(str(tmp_path / "out" / "tests"))
        return importlib.import_module(f"{package}.graph")

    yield load
    for name in [n for n in sys.modules if n.split(".")[0] in {"desk", "classifier"}]:
        del sys.modules[name]


def _scripted_blueprint(text: str) -> str:
    return text.replace("anthropic:claude-sonnet-5-5", "scripted").replace(
        "openai:gpt-4o-mini", "scripted"
    )


def _desk(tmp_path: Path) -> Path:
    from _helpers.export_projects import DESK_TOOLS, SKILL

    project = write_project(tmp_path / "desk", _scripted_blueprint(DESK_BLUEPRINT), DESK_TOOLS)
    skill = project / "skills" / "refund-policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (skill / "reference.md").write_text("Limit: 500 EUR.", encoding="utf-8")
    return project


SCRIPT = [
    _call("lookup_order", {"order_id": "o-1"}, "a"),
    _call("refund", {"order_id": "o-1", "amount": 50.0}, "b"),
    _call("refund", {"order_id": "o-1", "amount": 500.0}, "c"),
    AIMessage(content="refunds done"),
]


def _script() -> list[AIMessage]:
    return [message.model_copy(deep=True) for message in SCRIPT]


def test_a_conditional_approval_pauses_the_same_call_in_both(tmp_path: Path, exported: Any) -> None:
    project = _desk(tmp_path)
    module = exported(project, "desk")
    awa_agent = _awa_agent(project, _script())
    awa_tools = _awa_tools(project)

    awa_paused = asyncio.run(awa_agent.run("refund please", thread_id="t"))
    awa_request = awa_paused.interrupts[0]["value"]
    awa_done = asyncio.run(awa_agent.resume("t", {"action": "approve"}))
    awa_calls = list(awa_tools.CALLS)

    graph = module.build_graph(ScriptedChatModel(script=_script()), InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}
    paused = graph.invoke({"messages": [("user", "refund please")]}, config)
    request = paused["__interrupt__"][0].value["action_requests"][0]
    exported_tools = importlib.import_module("desk.tools")
    before_resume = list(exported_tools.CALLS)
    done = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

    assert (request["name"], request["args"]) == (awa_request["tool"], awa_request["args"])
    assert request["args"]["amount"] == 500.0
    assert before_resume == ["lookup:o-1", "refund:o-1:50.0"]
    assert awa_calls == exported_tools.CALLS
    assert done["messages"][-1].content == awa_done.text == "refunds done"


def test_an_unconditional_approval_pauses_the_first_call_in_both(
    tmp_path: Path, exported: Any
) -> None:
    project = _desk(tmp_path)
    module = exported(project, "desk")
    script = [_call("ship", {"order_id": "o-9"}, "s")]
    awa_agent = _awa_agent(project, [m.model_copy(deep=True) for m in script])

    awa_request = asyncio.run(awa_agent.run("ship it", thread_id="t")).interrupts[0]["value"]
    graph = module.build_graph(
        ScriptedChatModel(script=[m.model_copy(deep=True) for m in script]), InMemorySaver()
    )
    paused = graph.invoke({"messages": [("user", "ship it")]}, {"configurable": {"thread_id": "t"}})
    request = paused["__interrupt__"][0].value["action_requests"][0]

    assert (request["name"], request["args"]) == (awa_request["tool"], awa_request["args"])
    assert request["name"] == "ship"


def test_a_rejection_reaches_the_model_as_a_tool_result_in_both(
    tmp_path: Path, exported: Any
) -> None:
    project = _desk(tmp_path)
    module = exported(project, "desk")
    script = [_call("ship", {"order_id": "o-9"}, "s"), AIMessage(content="not shipped")]
    awa_agent = _awa_agent(project, [m.model_copy(deep=True) for m in script])
    awa_tools = _awa_tools(project)
    asyncio.run(awa_agent.run("ship it", thread_id="t"))
    awa_done = asyncio.run(awa_agent.resume("t", {"action": "reject", "note": "no"}))

    graph = module.build_graph(
        ScriptedChatModel(script=[m.model_copy(deep=True) for m in script]), InMemorySaver()
    )
    config = {"configurable": {"thread_id": "t"}}
    graph.invoke({"messages": [("user", "ship it")]}, config)
    done = graph.invoke(
        Command(resume={"decisions": [{"type": "reject", "message": "no"}]}), config
    )

    rejections = [
        str(m.content)
        for m in [*awa_done.messages, *done["messages"]]
        if isinstance(m, ToolMessage)
    ]
    assert len(rejections) == 2
    assert all("rejected" in text and "no" in text for text in rejections)
    assert not [c for c in awa_tools.CALLS if c.startswith("ship")]
    assert exported_calls() == []
    assert done["messages"][-1].content == awa_done.text == "not shipped"


def _awa_tools(project: Path) -> Any:
    tools = load_tools(tools_files([], project / "blueprint.yaml"))
    module = sys.modules[next(t for t in tools if t.__name__ == "refund").__module__]
    module.CALLS.clear()
    return module


def exported_calls() -> list[str]:
    return [c for c in importlib.import_module("desk.tools").CALLS if c.startswith("ship")]


def test_a_denied_tool_is_unavailable_in_the_export(tmp_path: Path, exported: Any) -> None:
    module = exported(_desk(tmp_path), "desk")

    assert "delete_order" not in module.TOOL_NAMES


def test_the_structured_output_is_the_same(tmp_path: Path, exported: Any) -> None:
    project = write_project(tmp_path / "classifier", _scripted_blueprint(STRUCTURED_BLUEPRINT))
    module = exported(project, "classifier")
    answer = {"label": "spam", "score": 0.5, "tags": ["a"]}
    script = [_call("SorterOutput", answer, "x")]
    awa_agent = _awa_agent(project, script)

    awa_output = asyncio.run(awa_agent.run("buy now")).output
    graph = module.build_graph(ScriptedChatModel(script=[_call("Output", answer, "x")]))
    output = graph.invoke({"messages": [("user", "buy now")]})["structured_response"]

    assert output.model_dump() == awa_output.model_dump() == answer


def test_the_structured_output_rejects_what_awa_rejects(tmp_path: Path, exported: Any) -> None:
    from pydantic import ValidationError

    project = write_project(tmp_path / "classifier", _scripted_blueprint(STRUCTURED_BLUEPRINT))
    exported(project, "classifier")
    schemas = importlib.import_module("classifier.schemas")
    from awa.blueprints import model_from_json_schema

    spec = Blueprint.from_yaml(project / "blueprint.yaml").agents[0]
    assert spec.output_schema is not None
    awa_model = model_from_json_schema(spec.output_schema)

    for bad in (
        {"label": "other", "score": 0.5},
        {"label": "spam", "score": 2},
        {"label": "spam", "score": "0.5"},
        {"label": "spam"},
        {"label": "spam", "score": 0.1, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            awa_model.model_validate(bad)
        with pytest.raises(ValidationError):
            schemas.Output.model_validate(bad)


REPO = Path(__file__).resolve().parents[2]
EXPECTED: dict[str, list[str]] = json.loads(
    (Path(__file__).parent / "expected_refusals.json").read_text("utf-8")
)
TEMPLATES = ("minimal", "team", "workflow", "support", "network")


def _project(key: str, tmp_path: Path) -> Path:
    kind, _, name = key.partition("/")
    if kind != "init":
        return REPO / "examples" / kind / name
    folder = tmp_path / f"{name}-demo"
    assert main(["init", str(folder), "--template", name]) == 0
    return folder


def test_the_record_covers_every_shipped_example_and_template() -> None:
    found = {
        f"{kind}/{folder.name}"
        for kind in ("recipes", "usecases", "processes")
        for folder in (REPO / "examples" / kind).iterdir()
        if (folder / "blueprint.yaml").is_file()
    } | {f"init/{name}" for name in TEMPLATES}

    assert found == set(EXPECTED)


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_an_example_exports_or_is_refused_as_recorded(key: str, tmp_path: Path) -> None:
    project = _project(key, tmp_path)
    out = tmp_path / "out"

    if EXPECTED[key]:
        with pytest.raises(ExportUnsupportedError) as raised:
            export(project, out)
        assert sorted(f.feature for f in raised.value.findings) == EXPECTED[key]
        assert not out.exists()
        return
    report = export(project, out)
    assert report.written
    finished = run_exported_tests(out)
    assert finished.returncode == 0, finished.stdout[-2000:] + finished.stderr[-2000:]
