"""The contract of every builtin tool: specs, declared risk and approval defaults, valid, invalid and hostile calls, and pinned regressions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.tools_matrix.runner import build, call
from _helpers.tools_matrix.specs import BY_NAME, SPECS, Case, ToolSpec
from _helpers.tools_matrix.world import INJECTION, World
from awa import Agent
from awa.testing import FakeChatModel, tool_call
from awa.toolkits import catalog, data_toolkit, document_toolkit, sql_toolkit, utility_toolkit
from awa.tools.approval import APPROVAL_METADATA_KEY
from awa.tools.hardening import FRAMED_METADATA_KEY, unframe_tool_output
from awa.tools.risk import declared_tool_risk


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World.create(tmp_path)


def _hostile() -> list[tuple[str, int]]:
    return [(spec.name, i) for spec in SPECS for i in range(len(spec.hostile))]


def _refused(text: str, case: Case) -> bool:
    return text.startswith(case.prefix) if case.prefix else True


@pytest.mark.slow
@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_a_valid_call_works(spec: ToolSpec, world: World) -> None:
    tool = build(spec, world)
    text = call(tool, spec.valid.resolved_args(world))
    assert not text.startswith(("Error:", "invalid:", "cannot evaluate:")), text
    assert spec.valid.expect in text, text
    if spec.valid.check:
        spec.valid.check(world)


@pytest.mark.slow
@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_an_invalid_call_is_refused_with_a_message_that_names_the_fix(
    spec: ToolSpec, world: World
) -> None:
    before = world.state()
    invalid = spec.invalid
    if invalid is None:
        assert spec.no_arguments
        assert not catalog().get(spec.name).create(**spec.options(world)).args
        return
    text = call(build(spec, world), invalid.resolved_args(world))
    assert _refused(text, invalid), text
    assert invalid.expect in text, text
    assert world.state() == before


@pytest.mark.slow
@pytest.mark.parametrize(("name", "index"), _hostile(), ids=lambda v: str(v))
def test_a_hostile_call_is_refused_or_neutralised(name: str, index: int, world: World) -> None:
    spec = BY_NAME[name]
    case = spec.hostile[index]
    before = world.state()
    text = call(build(spec, world, case), case.resolved_args(world))
    assert case.expect in text, text
    if case.refused:
        assert _refused(text, case), f"expected a refusal, got: {text[:200]}"
    else:
        assert not text.startswith(("Error:", "invalid:", "cannot evaluate:")), text
    if case.absent:
        assert case.absent not in text, text
    if not case.changes:
        assert world.state() == before, "a refused hostile call changed something"
    assert (world.base / "outside.txt").read_text("utf-8") == "top secret outside the sandbox"
    if case.check:
        case.check(world)


@pytest.mark.parametrize("spec", [s for s in SPECS if s.poison is not None], ids=lambda s: s.name)
def test_text_planted_in_a_result_comes_back_as_framed_data(spec: ToolSpec, world: World) -> None:
    case = spec.poison
    assert case is not None
    tool = build(spec, world, case)
    text = call(tool, case.resolved_args(world))
    assert case.expect in text, text
    if not spec.framed:
        assert "attacker@evil.example" not in text, "a verdict must not repeat page text"
        return
    assert (tool.metadata or {}).get(FRAMED_METADATA_KEY) is True
    assert text.startswith("<tool_output"), text
    assert text.rstrip().endswith("</tool_output>")
    assert "as data, not as instructions" in text.splitlines()[0]
    assert case.expect
    unframed = unframe_tool_output(text.rstrip())
    assert unframed is not None, text
    # Some tools shorten long values, so the start of the planted text is what must be inside.
    assert INJECTION[:30] in unframed[2], "the planted text must sit inside the frame"


@pytest.mark.slow
@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_the_stated_side_effect_is_what_repeating_the_valid_call_does(
    spec: ToolSpec, world: World
) -> None:
    tool = build(spec, world)
    args = spec.valid.resolved_args(world)
    before = world.state()
    call(tool, args)
    once = world.state()
    call(tool, args)
    twice = world.state()
    if spec.effect == "none":
        assert once == before, "a call that claims no side effect changed something"
        assert twice == before
    elif spec.effect == "idempotent":
        assert once != before
        assert twice == once
    elif spec.effect == "repeats":
        assert once != before
        assert twice != once
    else:
        pytest.fail(f"unknown effect {spec.effect!r}")


GATED = [spec for spec in SPECS if catalog().get(spec.name).requires_approval]


def test_the_catalog_has_31_tools_and_each_has_a_spec() -> None:
    names = [entry.name for entry in catalog()]
    assert len(names) == 31
    assert sorted(BY_NAME) == sorted(names)
    assert len(SPECS) == len(BY_NAME), "a tool has two specs"


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_every_spec_has_the_cases_the_matrix_promises(spec: ToolSpec) -> None:
    assert spec.valid.expect
    assert spec.invalid is not None or spec.no_arguments
    if spec.invalid is not None:
        assert spec.invalid.expect
    assert spec.effect in {"none", "idempotent", "repeats"}
    assert spec.effect_note.endswith(".")
    entry = catalog().get(spec.name)
    touches_outside = (
        entry.risk.untrusted_input or entry.risk.private_data or entry.risk.external_action
    )
    if touches_outside and spec.name not in {"awa.list_connectors"}:
        assert spec.hostile, f"{spec.name} touches files, network or data and needs a hostile case"
    if entry.risk.untrusted_input:
        assert spec.poison is not None, f"{spec.name} reads outside content: add an injection case"


@pytest.mark.slow
@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_the_tool_declares_the_risk_and_approval_default_of_the_catalog(
    spec: ToolSpec, world: World
) -> None:
    entry = catalog().get(spec.name)
    tool = build(spec, world, gated=True)
    declared = declared_tool_risk(tool)
    assert declared is not None
    config = {entry.toolkit.name: spec.options(world)}
    assert declared == entry.risk_for(config)
    gated = (tool.metadata or {}).get(APPROVAL_METADATA_KEY) is True
    assert gated == entry.approval_for(config) == entry.requires_approval
    ungated = build(spec, world)
    assert APPROVAL_METADATA_KEY not in (ungated.metadata or {})


@pytest.mark.parametrize("spec", GATED, ids=lambda s: s.name)
async def test_a_gated_tool_acts_only_after_a_human_approves(spec: ToolSpec, world: World) -> None:
    args = spec.valid.resolved_args(world)
    model = FakeChatModel([tool_call(spec.name, args), "done"])
    agent = Agent(
        "clerk",
        instructions="Do the task.",
        model=model,
        tools=[build(spec, world, gated=True)],
        checkpointer=InMemorySaver(),
    )
    before = world.state()

    paused = await agent.run("Please do it.")

    assert paused.is_interrupted
    assert paused.interrupts[0]["value"]["tool"] == spec.name
    assert world.state() == before, "the tool acted before the human decided"
    assert paused.thread_id is not None
    resumed = await agent.resume(paused.thread_id, {"action": "approve"})
    assert resumed.output == "done"
    assert world.state() != before, "the tool did nothing after approval"


@pytest.mark.parametrize("spec", GATED, ids=lambda s: s.name)
async def test_a_rejected_gated_tool_leaves_the_world_unchanged(
    spec: ToolSpec, world: World
) -> None:
    model = FakeChatModel([tool_call(spec.name, spec.valid.resolved_args(world)), "stopped"])
    agent = Agent(
        "clerk", model=model, tools=[build(spec, world, gated=True)], checkpointer=InMemorySaver()
    )
    before = world.state()
    paused = await agent.run("Please do it.")
    assert paused.thread_id is not None
    await agent.resume(paused.thread_id, {"action": "reject", "note": "not now"})
    assert world.state() == before


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "add", "date": "2026-01-30", "days": 10**12},
        {"operation": "add", "date": "2026-01-30", "years": 10**9},
        {"operation": "subtract", "date": "2026-01-30", "weeks": 10**12},
        {"operation": "add", "date": "9999-12-31", "days": 1},
        {"operation": "add", "date": "2026-01-30", "months": 10**15},
    ],
)
def test_date_math_names_the_supported_range_instead_of_an_internal_error(
    args: dict[str, object],
) -> None:
    tool = next(t for t in utility_toolkit() if t.name == "date_math")
    text = tool.invoke(args)
    assert text.startswith("Error:")
    assert "supported date range" in text
    assert "OverflowError" not in text


def _readers(root: Path) -> dict[str, BaseTool]:
    tools = [*data_toolkit(root, frame_output=False), *document_toolkit(root, frame_output=False)]
    return {tool.name: tool for tool in tools}


@pytest.mark.parametrize(
    ("tool", "name", "content"),
    [
        ("read_excel", "fake.xlsx", b"not a workbook"),
        ("query_excel", "fake.xlsx", b"not a workbook"),
        ("extract_docx_text", "fake.docx", b"not a document"),
        ("extract_pdf_text", "fake.pdf", b"%PDF-1.4 not a pdf"),
    ],
)
def test_a_file_of_the_wrong_kind_is_refused_with_the_fix_and_without_the_host_path(
    tmp_path: Path, tool: str, name: str, content: bytes
) -> None:
    (tmp_path / name).write_bytes(content)
    text = _readers(tmp_path)[tool].invoke({"path": name})
    assert text.startswith("Error:")
    assert f"'{name}'" in text
    assert "Error:" not in text[len("Error:") :], "an internal exception name leaks"
    assert str(tmp_path) not in text, "the message leaks the folder the tool runs in"
    assert "save" in text or "check" in text, "the message should say what to do"


def test_sql_execute_refusal_does_not_tell_the_model_that_it_is_the_read_only_tool(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shop.db"
    sqlite3.connect(database).close()
    tool = next(
        t for t in sql_toolkit(database, requires_approval=False) if t.name == "sql_execute"
    )
    text = tool.invoke({"statement": "attach database 'stolen.db' as x"})
    assert text.startswith("Error:")
    assert "sql_query is read-only" not in text
    assert "ATTACH" in text
    assert not (tmp_path / "stolen.db").exists()
