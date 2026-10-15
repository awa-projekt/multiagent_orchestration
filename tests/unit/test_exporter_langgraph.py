"""The export: refusals with one finding each, the all-or-nothing writer, the modules copied into every export, and `awa export`."""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sys
import tomllib
import types
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import awa.exporter.runtime_src as runtime_src
from _helpers.scripted_model import ScriptedChatModel
from awa.blueprints import Blueprint
from awa.cli import main
from awa.cli._common import EXIT_UNSUPPORTED
from awa.errors import AwaError
from awa.exporter import MARKER, ExportError, ExportUnsupportedError, Finding, export, write_tree
from awa.exporter._refuse import refusals
from awa.exporter.runtime_src.discover import project_tools
from awa.exporter.runtime_src.limits import RunLimitExceededError, RunLimits
from awa.exporter.runtime_src.skill_loader import skill_tools
from awa.kernel import conditions

REPO = Path(__file__).resolve().parents[2]
ONE = "name: t\nagents:\n  - name: a\n    instructions: hi\n"
TWO = "name: t\nagents:\n  - name: a\n    instructions: hi\n  - name: b\n    instructions: hi\n"


def _features(text: str, folder: Path) -> list[str]:
    return [finding.feature for finding in refusals(Blueprint.from_yaml_text(text), folder)]


def test_a_plain_single_agent_is_not_refused(tmp_path: Path) -> None:
    assert _features(ONE, tmp_path) == []


@pytest.mark.parametrize(
    ("text", "feature"),
    [
        (TWO + "team: a\n", "team"),
        (TWO + "workflow: [a, b]\n", "workflow"),
        (TWO + "network: a\n", "network"),
        (TWO, "agents"),
        (ONE + "    role: worker\n", "agents.a.role"),
        (ONE + "    memory: {scope: agent}\n", "agents.a.memory"),
        (ONE + "    tools: ['builtin:read_file']\n", "agents.a.tools.builtin:read_file"),
        (
            ONE + "    tools: ['connector:mail.send_message']\n",
            "agents.a.tools.connector:mail.send_message",
        ),
        (ONE + "    tools: ['mcp:crm']\n", "agents.a.tools.mcp:crm"),
        (ONE + "    tools: ['pkg.mod:fn']\n", "agents.a.tools.pkg.mod:fn"),
        (ONE + "    model_settings: {retry: {max_attempts: 2}}\n", "agents.a.model_settings.retry"),
        (
            ONE + "    model_settings: {fallbacks: ['openai:x']}\n",
            "agents.a.model_settings.fallbacks",
        ),
        (ONE + "    model_settings: {effort: high}\n", "agents.a.model_settings.effort"),
        (ONE + "connectors: {mail: {use: smtp}}\n", "connectors"),
        (ONE + "special_cases: {step: {}}\n", "special_cases"),
        (
            ONE + "limits: {max_cost: 1}\nprices: {m: {input_per_mtok: 1, output_per_mtok: 1}}\n",
            "limits.max_cost",
        ),
        (ONE + "approvals:\n  - tool: t\n    when: 'pkg.mod:pred'\n", "approvals[0].when"),
        (ONE + "policies:\n  - ask: [external_action]\n", "policies[0].ask"),
        (ONE + "policies:\n  - use: 'pkg.mod:policy'\n", "policies[0].use"),
        (ONE + "auto: true\n", "agents.a.auto"),
    ],
)
def test_each_unsupported_feature_is_one_finding_that_names_it(
    text: str, feature: str, tmp_path: Path
) -> None:
    assert feature in _features(text, tmp_path)


def test_everything_to_change_is_listed_at_once(tmp_path: Path) -> None:
    text = TWO + "team: a\nconnectors: {mail: {use: smtp}}\n"

    assert _features(text, tmp_path) == ["team", "connectors"]


def test_a_process_is_refused() -> None:
    folder = REPO / "examples" / "processes" / "invoice-check"

    features = _features((folder / "blueprint.yaml").read_text("utf-8"), folder)

    assert "process" in features


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import awa\n", "awa"),
        ("from awa.tools import tool\n", "awa.tools"),
        ("import requests\n", "requests"),
        ("from . import helper\n", "."),
        ("from helper import x\n", "helper"),
    ],
)
def test_a_tools_file_may_import_only_what_the_export_carries(
    source: str, module: str, tmp_path: Path
) -> None:
    (tmp_path / "tools.py").write_text(f'"""x."""\n\n{source}', encoding="utf-8")

    found = refusals(Blueprint.from_yaml_text(ONE), tmp_path)

    assert [f.feature for f in found] == ["tools.py:3"]
    assert repr(module) in found[0].advice


def test_a_tools_file_with_standard_and_langchain_imports_is_fine(tmp_path: Path) -> None:
    (tmp_path / "tools.py").write_text(
        "import json\nfrom langchain_core.tools import tool\nfrom pydantic import BaseModel\n",
        encoding="utf-8",
    )

    assert refusals(Blueprint.from_yaml_text(ONE), tmp_path) == []


def test_a_refused_blueprint_writes_nothing(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "blueprint.yaml").write_text(TWO + "team: a\n", encoding="utf-8")
    out = tmp_path / "out"

    with pytest.raises(ExportUnsupportedError) as raised:
        export(project, out)

    assert not out.exists()
    assert raised.value.lines()[0].startswith("export.unsupported ")
    assert "team" in str(raised.value)


def test_an_output_property_that_is_a_keyword_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "blueprint.yaml").write_text(
        ONE + "    output_schema:\n      type: object\n      properties:\n"
        "        class: {type: string}\n      required: [class]\n",
        encoding="utf-8",
    )

    with pytest.raises(ExportUnsupportedError, match="Python keyword"):
        export(project, tmp_path / "out")


def test_writes_every_file_and_leaves_no_staging_folder(tmp_path: Path) -> None:
    out = tmp_path / "project"

    write_tree({"a.txt": "one", "sub/b.bin": b"\x00\x01"}, out)

    assert (out / "a.txt").read_text("utf-8") == "one"
    assert (out / "sub" / "b.bin").read_bytes() == b"\x00\x01"
    assert [p.name for p in tmp_path.iterdir()] == ["project"]


def test_an_empty_destination_is_fine(tmp_path: Path) -> None:
    out = tmp_path / "project"
    out.mkdir()

    write_tree({"a.txt": "x"}, out)

    assert (out / "a.txt").is_file()


def test_a_non_empty_destination_needs_force_and_an_earlier_export(tmp_path: Path) -> None:
    out = tmp_path / "project"
    out.mkdir()
    (out / "mine.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ExportError, match="not empty"):
        write_tree({"a.txt": "x"}, out)
    with pytest.raises(ExportError, match="not an earlier export"):
        write_tree({"a.txt": "x"}, out, force=True)

    assert (out / "mine.txt").read_text("utf-8") == "keep"
    assert not (out / "a.txt").exists()


def test_force_replaces_an_earlier_export(tmp_path: Path) -> None:
    out = tmp_path / "project"
    write_tree({MARKER: "old", "old.txt": "x"}, out)

    write_tree({MARKER: "new", "new.txt": "y"}, out, force=True)

    assert sorted(p.name for p in out.iterdir()) == [MARKER, "new.txt"]
    assert [p.name for p in tmp_path.iterdir()] == ["project"]


def test_a_file_as_destination_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(ExportError, match="is a file"):
        write_tree({"a.txt": "x"}, target)


@pytest.mark.parametrize("name", ["../escape.txt", "/abs.txt", "a/../../b.txt", ""])
def test_unsafe_paths_are_refused_before_anything_is_written(tmp_path: Path, name: str) -> None:
    out = tmp_path / "project"

    with pytest.raises(ExportError, match="unsafe path"):
        write_tree({"ok.txt": "x", name: "y"}, out)

    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_leaves_the_earlier_export_whole(tmp_path: Path) -> None:
    out = tmp_path / "project"
    write_tree({MARKER: "old", "old.txt": "x"}, out)

    with pytest.raises(TypeError):
        write_tree({"new.txt": 5}, out, force=True)  # type: ignore[dict-item]

    assert sorted(p.name for p in out.iterdir()) == [MARKER, "old.txt"]
    assert [p.name for p in tmp_path.iterdir()] == ["project"]


def test_the_refusal_lists_every_finding_and_is_an_awa_error() -> None:
    error = ExportUnsupportedError(
        "blueprint.yaml",
        [Finding("team", "export a single agent"), Finding("connectors", "write a tool")],
    )

    assert isinstance(error, AwaError)
    assert error.lines() == [
        "export.unsupported blueprint.yaml: team - export a single agent",
        "export.unsupported blueprint.yaml: connectors - write a tool",
    ]
    assert str(error) == "\n".join(error.lines())
    assert error.details["features"] == ["team", "connectors"]


SOURCE = Path(runtime_src.__file__).parent
COPIED = [*SOURCE.glob("*.py"), Path(conditions.__file__)]


def _imported(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    return names


def test_no_copied_module_imports_awa() -> None:
    for path in COPIED:
        found = _imported(ast.parse(path.read_text("utf-8")))
        assert not [n for n in found if n == "awa" or n.startswith("awa.")], path


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """`approvals.py` with `conditions` beside it, as an export ships them."""

    monkeypatch.setitem(sys.modules, "awa.exporter.runtime_src.conditions", conditions)
    monkeypatch.delitem(sys.modules, "awa.exporter.runtime_src.approvals", raising=False)
    return importlib.import_module("awa.exporter.runtime_src.approvals")


def _call(name: str, args: dict[str, Any]) -> Any:
    return types.SimpleNamespace(tool_call={"name": name, "args": args, "id": "1"})


def test_a_condition_asks_only_for_calls_it_matches(approvals: types.ModuleType) -> None:
    when = approvals.needs_approval("amount >= 100")

    assert when(_call("refund", {"amount": 150})) is True
    assert when(_call("refund", {"amount": 99.5})) is False
    assert when(_call("refund", {"amount": "150"})) is True  # numeric text reads as a number


def test_a_condition_that_cannot_be_decided_asks(approvals: types.ModuleType) -> None:
    when = approvals.needs_approval("amount >= 100")

    assert when(_call("refund", {"amount": "lots"})) is True
    assert when(_call("refund", {})) is True  # a missing value is never guessed


def test_a_missing_argument_takes_the_tools_default(approvals: types.ModuleType) -> None:
    @tool
    def refund(order: str, amount: float = 500.0) -> str:
        """Refund."""
        return order

    config = approvals.interrupt_on({"refund": "amount >= 100"}, [refund])

    assert config["refund"]["when"](_call("refund", {"order": "o"})) is True


def test_an_unconditional_gate_has_no_predicate(approvals: types.ModuleType) -> None:
    @tool
    def ship(order: str) -> str:
        """Ship."""
        return order

    assert approvals.interrupt_on({"ship": None}, [ship]) == {
        "ship": {"allowed_decisions": ["approve", "edit", "reject"]}
    }


def _agent(tools: list[Any], script: list[AIMessage], middleware: list[Any]) -> Any:
    return create_agent(
        ScriptedChatModel(script=script), tools, middleware=middleware, checkpointer=InMemorySaver()
    )


def _call_message(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "c1"}])


def test_the_gate_pauses_matching_calls_and_lets_the_others_run(
    approvals: types.ModuleType,
) -> None:
    ran: list[float] = []

    @tool
    def refund(amount: float) -> str:
        """Refund."""
        ran.append(amount)
        return "ok"

    def gate() -> HumanInTheLoopMiddleware:
        return HumanInTheLoopMiddleware(
            approvals.interrupt_on({"refund": "amount >= 100"}, [refund])
        )

    done = AIMessage(content="done")
    config = {"configurable": {"thread_id": "t"}}
    small = _agent([refund], [_call_message("refund", {"amount": 5}), done], [gate()])
    small.invoke({"messages": [("user", "go")]}, config)
    other = {"configurable": {"thread_id": "u"}}
    large = _agent([refund], [_call_message("refund", {"amount": 500}), done], [gate()])

    paused = large.invoke({"messages": [("user", "go")]}, other)

    assert ran == [5]
    assert paused["__interrupt__"][0].value["action_requests"][0]["args"] == {"amount": 500}
    large.invoke(Command(resume={"decisions": [{"type": "approve"}]}), other)
    assert ran == [5, 500]


def _usage(total: int) -> dict[str, int]:
    return {"input_tokens": total // 2, "output_tokens": total - total // 2, "total_tokens": total}


def test_the_token_limit_stops_a_run() -> None:
    reply = AIMessage(content="more", usage_metadata=_usage(12))
    agent = _agent([], [reply], [RunLimits(max_total_tokens=10)])

    with pytest.raises(RunLimitExceededError, match="12 tokens"):
        agent.invoke({"messages": [("user", "hi")]}, {"configurable": {"thread_id": "t"}})


def test_tokens_are_counted_per_run() -> None:
    replies = [AIMessage(content="ok", usage_metadata=_usage(6)) for _ in range(2)]
    agent = _agent([], replies, [RunLimits(max_total_tokens=10)])
    config = {"configurable": {"thread_id": "t"}}

    agent.invoke({"messages": [("user", "one")]}, config)
    result = agent.invoke({"messages": [("user", "two")]}, config)  # a new run starts at zero

    assert result["messages"][-1].content == "ok"


def test_the_duration_limit_counts_call_time_and_refuses_the_next_call() -> None:
    limits = RunLimits(max_duration=0.05)

    def slow(request: Any) -> str:
        import time

        time.sleep(0.08)
        return "x"

    with pytest.raises(RunLimitExceededError, match="seconds"):
        limits._timed(slow, None)
    with pytest.raises(RunLimitExceededError, match="seconds"):
        limits._timed(lambda request: "never", None)  # the spent time is kept


def test_each_run_counts_its_own_time_and_a_finished_run_is_forgotten() -> None:
    limits = RunLimits(max_duration=60)
    agent = _agent([], [AIMessage(content="ok"), AIMessage(content="ok")], [limits])

    agent.invoke({"messages": [("user", "one")]}, {"configurable": {"thread_id": "t"}})
    agent.invoke({"messages": [("user", "two")]}, {"configurable": {"thread_id": "u"}})

    assert limits._spent == {}


def test_the_duration_limit_works_for_async_calls() -> None:
    limits = RunLimits(max_duration=0.01)

    async def slow(request: Any) -> str:
        await asyncio.sleep(0.03)
        return "x"

    with pytest.raises(RunLimitExceededError):
        asyncio.run(limits.awrap_tool_call(None, slow))


SKILL = "---\nname: refund-policy\ndescription: d\n---\n# Policy\n\nRefund paid orders.\n"


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    folder = tmp_path / "refund-policy"
    folder.mkdir()
    (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (folder / "reference.md").write_text("Limit 500.", encoding="utf-8")
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return tmp_path


def test_load_skill_returns_the_instructions_without_the_frontmatter(skills: Path) -> None:
    load = skill_tools(skills, ["refund-policy"])[0]

    assert load.name == "load_skill"
    assert load.invoke({"name": "refund-policy"}) == "# Policy\n\nRefund paid orders."


def test_a_resource_reader_exists_only_when_a_skill_has_resources(skills: Path) -> None:
    assert [t.name for t in skill_tools(skills, ["plain"])] == ["load_skill"]
    both = {t.name: t for t in skill_tools(skills, ["refund-policy"])}
    assert set(both) == {"load_skill", "read_skill_resource"}
    reading = both["read_skill_resource"].invoke({"name": "refund-policy", "path": "reference.md"})
    assert reading == "Limit 500."


def test_resources_stay_inside_the_skill_folder(skills: Path) -> None:
    read = skill_tools(skills, ["refund-policy"])[1]

    for path in ("../plain/SKILL.md", "missing.md", "."):
        assert "Unknown resource" in read.invoke({"name": "refund-policy", "path": path})


def test_the_loader_can_be_left_out(skills: Path) -> None:
    names = [t.name for t in skill_tools(skills, ["refund-policy"], load=False)]

    assert names == ["read_skill_resource"]


def test_project_tools_finds_objects_and_documented_functions() -> None:
    module = types.ModuleType("fake_tools")
    source = (
        "from langchain_core.tools import tool\n"
        "@tool\n"
        "def made(x: str) -> str:\n"
        '    """Made."""\n'
        "    return x\n"
        "def plain(x: str) -> str:\n"
        '    """Plain."""\n'
        "    return x\n"
        "def hidden(x: str) -> str:\n"
        '    """Hidden."""\n'
        "    return x\n"
        "__all__ = ['plain']\n"
    )
    exec(source, module.__dict__)
    sys.modules["fake_tools"] = module

    tools = project_tools(module, ["plain", "made"])

    assert [t.name for t in tools] == ["plain", "made"]
    assert tools[0].invoke({"x": "a"}) == "a"
    with pytest.raises(LookupError, match="hidden"):
        project_tools(module, ["hidden"])


BLUEPRINT = """\
name: tiny
description: One agent.
agents:
  - name: helper
    instructions: Help.
"""
TEAM = BLUEPRINT + "  - name: other\n    instructions: Help too.\nteam: helper\n"


def _project(root: Path, text: str = BLUEPRINT) -> Path:
    root.mkdir()
    (root / "blueprint.yaml").write_text(text, encoding="utf-8")
    return root


def test_export_writes_the_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"

    code = main(["export", str(_project(tmp_path / "p")), "--out", str(out)])

    assert code == 0
    assert (out / "src" / "tiny" / "graph.py").is_file()
    assert "exported to" in capsys.readouterr().out


def test_export_takes_the_blueprint_file_too(tmp_path: Path) -> None:
    project = _project(tmp_path / "p")

    assert main(["export", str(project / "blueprint.yaml"), "--out", str(tmp_path / "o")]) == 0


def test_a_refusal_has_its_own_exit_code_lists_every_finding_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    text = TEAM + "connectors: {mail: {use: smtp}}\n"

    code = main(["export", str(_project(tmp_path / "p", text)), "--out", str(out)])

    assert code == EXIT_UNSUPPORTED == 4
    error = capsys.readouterr().err
    assert "export.unsupported" in error
    assert " team - " in error
    assert " connectors - " in error
    assert not out.exists()


def test_a_refusal_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["export", str(_project(tmp_path / "p", TEAM)), "--out", str(tmp_path / "o"), "--json"]
    )

    assert code == EXIT_UNSUPPORTED
    found = json.loads(capsys.readouterr().out)["unsupported"]
    assert [item["feature"] for item in found] == ["team"]


def test_export_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["export", str(_project(tmp_path / "p")), "--out", str(tmp_path / "o"), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["package"] == "tiny"
    assert report["written"] is True
    assert "src/tiny/graph.py" in report["files"]


def test_check_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "o"

    code = main(["export", str(_project(tmp_path / "p")), "--out", str(out), "--check"])

    assert code == 0
    assert not out.exists()
    assert "check passed" in capsys.readouterr().out


def test_without_out_or_check_it_says_what_to_give(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["export", str(_project(tmp_path / "p"))])

    assert code == 1
    assert "--out" in capsys.readouterr().err


def test_a_folder_that_is_not_empty_needs_force_and_an_earlier_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path / "p")
    out = tmp_path / "out"
    assert main(["export", str(project), "--out", str(out)]) == 0

    assert main(["export", str(project), "--out", str(out)]) == 1
    assert "not empty" in capsys.readouterr().err
    assert main(["export", str(project), "--out", str(out), "--force"]) == 0


def test_the_model_id_stays_data_in_the_generated_settings(tmp_path: Path) -> None:
    model = 'openai:gpt-4o"); open(r"PWNED", "w").write("x"); X = ("'
    text = BLUEPRINT + f"    model: {json.dumps(model)}\n"
    out = tmp_path / "out"

    export(_project(tmp_path / "p", text), out)

    settings = ast.parse((out / "src" / "tiny" / "settings.py").read_text(encoding="utf-8"))
    calls = [node for node in settings.body if not isinstance(node, ast.Assign | ast.AnnAssign)]
    assert all(isinstance(node, ast.Expr | ast.Import | ast.ImportFrom) for node in calls)
    assert all(isinstance(node.value, ast.Constant) for node in calls if isinstance(node, ast.Expr))
    [default] = [
        node.value.args[1]
        for node in settings.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "MODEL"
        and isinstance(node.value, ast.Call)
    ]
    assert isinstance(default, ast.Constant) and default.value == model


def test_the_version_stays_a_string_in_the_exported_pyproject(tmp_path: Path) -> None:
    version = '1.0"\ndependencies = ["evilpkg"]\n[tool.x]\ny = "'
    text = BLUEPRINT + f"version: {json.dumps(version)}\n"
    out = tmp_path / "out"

    export(_project(tmp_path / "p", text), out)

    project = tomllib.loads((out / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == version
    assert "evilpkg" not in project["dependencies"]
