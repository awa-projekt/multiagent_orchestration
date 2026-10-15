"""Eval harness integrity: a folder per case, setup hooks, tool activity in the report,
widened limits, trajectory scorers, the judge's compact digest and recorded web search."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent, tool
from awa.blueprints import Blueprint
from awa.cli import build_parser
from awa.cli._common import limits_from_args
from awa.contracts.events import (
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    RunEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.contracts.results import RunResult
from awa.errors import ConfigurationError
from awa.evals import (
    ApprovalPresent,
    Dataset,
    EvalCase,
    EvalError,
    MaxToolCalls,
    ModelJudge,
    RunTrace,
    ToolArgsMatch,
    evaluate,
    parse_scorer,
)
from awa.models import ModelRegistry
from awa.runtime import case_dir, use_case_dir
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.toolkits import SearchResult, search_fixture, web_toolkit


@tool
def note(text: str) -> str:
    """Add a line to the notes file and say how many lines it has.

    Args:
        text: The line.
    """

    folder = case_dir() / "out"
    folder.mkdir(exist_ok=True)
    with (folder / "notes.txt").open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    seed = (case_dir() / "data" / "seed.txt").read_text(encoding="utf-8").strip()
    lines = (folder / "notes.txt").read_text(encoding="utf-8").splitlines()
    return f"{len(lines)} line(s), seed {seed}"


def noter_model(call: ModelCall) -> Any:
    last = call.last_message
    if isinstance(last, ToolMessage):
        return f"Done: {last.content}"
    return tool_call("note", {"text": str(last.content)})


def noter() -> Agent:
    return Agent("noter", model=FunctionChatModel(noter_model), tools=[note])


@pytest.fixture
def example(tmp_path: Path) -> Path:
    folder = tmp_path / "example"
    (folder / "data").mkdir(parents=True)
    (folder / "data" / "seed.txt").write_text("S-1\n", encoding="utf-8")
    (folder / "out").mkdir()
    (folder / "out" / "notes.txt").write_text("from an earlier run\n", encoding="utf-8")
    (folder / ".awa").mkdir()
    (folder / "src").mkdir()
    return folder


# ---------------------------------------------------------------------- sandbox and setup


async def test_every_case_writes_into_its_own_copy(example: Path) -> None:
    cases = [
        EvalCase(id=f"c{n}", input=f"line {n}", scorers=("contains:2 line(s)",)) for n in range(4)
    ]
    report = await evaluate(noter, cases, sandbox=example, concurrency=4)

    assert [c.status for c in report.cases] == ["passed"] * 4, report.to_markdown()
    assert all("seed S-1" in str(c.output) for c in report.cases)
    # the example's own files are untouched
    assert (example / "out" / "notes.txt").read_text(encoding="utf-8") == "from an earlier run\n"


async def test_only_the_named_folders_are_copied_and_the_copy_is_removed(example: Path) -> None:
    seen: list[Path] = []

    @tool
    def look() -> str:
        """List the case folder."""

        seen.append(case_dir())
        return ",".join(sorted(p.name for p in case_dir().iterdir()))

    def model(call: ModelCall) -> Any:
        last = call.last_message
        return f"has {last.content}" if isinstance(last, ToolMessage) else tool_call("look", {})

    def system() -> Agent:
        return Agent("looker", model=FunctionChatModel(model), tools=[look])

    report = await evaluate(
        system, [EvalCase(id="one", input="look")], sandbox=example, sandbox_folders=["data", "x"]
    )
    assert report.cases[0].output == "has data"
    assert seen and not seen[0].exists()

    everything = await evaluate(system, [EvalCase(id="all", input="look")], sandbox=example)
    assert everything.cases[0].output == "has data,out,src"  # hidden folders are left out


async def test_a_missing_sandbox_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="does not exist"):
        await evaluate(noter, [EvalCase(id="a", input="x")], sandbox=tmp_path / "missing")


SETUP = """
from pathlib import Path


def seed(folder: Path, case) -> None:
    (folder / "data").mkdir(exist_ok=True)
    (folder / "data" / "seed.txt").write_text(f"for {case.id}", encoding="utf-8")


async def more(folder: Path, case) -> None:
    (folder / "out").mkdir(exist_ok=True)
    (folder / "out" / "notes.txt").write_text("prepared\\n", encoding="utf-8")


def broken(folder: Path, case) -> None:
    raise RuntimeError("no fixtures")
"""


async def test_setup_hooks_prepare_each_case_folder(tmp_path: Path) -> None:
    (tmp_path / "fixtures.py").write_text(SETUP, encoding="utf-8")
    (tmp_path / "cases.yaml").write_text(
        "name: noted\n"
        "setup: fixtures.py:seed\n"
        "cases:\n"
        "  - {id: plain, input: hello}\n"
        "  - {id: prepared, input: hello, setup: 'fixtures.py:more'}\n"
        "  - {id: broken, input: hello, setup: 'fixtures.py:broken'}\n",
        encoding="utf-8",
    )
    dataset = Dataset.load(tmp_path / "cases.yaml")
    assert dataset.setup == f"{(tmp_path / 'fixtures.py').resolve()}:seed"
    assert dataset.filter(ids=["plain"]).setup == dataset.setup
    assert Dataset.from_yaml(dataset.to_yaml()).setup == dataset.setup

    report = await evaluate(noter, dataset)
    plain, prepared, broken = report.cases
    assert plain.output == "Done: 1 line(s), seed for plain"
    assert prepared.output == "Done: 2 line(s), seed for prepared"
    assert broken.status == "error" and broken.error == "RuntimeError: no fixtures"


def test_setup_specs_are_checked() -> None:
    with pytest.raises(EvalError, match="must be"):
        Dataset.from_yaml("setup: no_function\ncases: [{id: a, input: x}]")
    with pytest.raises(EvalError, match="setup"):
        Dataset.from_records([{"input": "x", "setup": "fixtures.py:"}])


async def test_an_unknown_setup_function_fails_the_case(tmp_path: Path) -> None:
    (tmp_path / "fixtures.py").write_text(SETUP, encoding="utf-8")
    case = EvalCase(id="a", input="x", setup=f"{tmp_path / 'fixtures.py'}:missing")
    [result] = (await evaluate(noter, [case])).cases
    assert result.status == "error" and "has no function 'missing'" in (result.error or "")


def test_case_dir_falls_back_to_the_default_then_the_working_directory(tmp_path: Path) -> None:
    assert case_dir(tmp_path) == tmp_path
    assert case_dir() == Path.cwd()
    with use_case_dir(tmp_path / "case"):
        assert case_dir(tmp_path) == tmp_path / "case"
    with use_case_dir(None):
        assert case_dir(tmp_path) == tmp_path


# ---------------------------------------------------------------------- limits


def _limits(blueprint: str, **given: Any) -> Any:
    args = argparse.Namespace(
        max_requests=None, max_total_tokens=None, max_tool_calls=None, max_cost=None,
        max_duration=None,
    )  # fmt: skip
    for name, value in given.items():
        setattr(args, name, value)
    return limits_from_args(args, Blueprint.from_yaml(blueprint), widen=True)


BUDGETED = "name: b\nagents: [{name: a}]\nlimits: {max_requests: 80, max_tool_calls: 10}\n"


def test_eval_limits_take_the_larger_value_of_blueprint_and_options() -> None:
    limits = _limits(BUDGETED, max_requests=40, max_tool_calls=30, max_total_tokens=5000)
    assert (limits.max_requests, limits.max_tool_calls, limits.max_total_tokens) == (80, 30, 5000)
    assert _limits("name: b\nagents: [{name: a}]\n", max_requests=40).max_requests == 40
    assert _limits(BUDGETED).max_requests == 80


# ---------------------------------------------------------------------- trajectory scorers


def _trace(*events: RunEvent) -> RunTrace:
    return RunTrace(result=RunResult(output="done", agent="a", run_id="r"), events=events)


def _call(tool_name: str, call_id: str, **arguments: Any) -> ToolCallEvent:
    return ToolCallEvent(
        tool_name=tool_name,
        call_id=call_id,
        arguments=arguments,
        run_id="r",
        root_run_id="r",
        agent="a",
    )


def _result(tool_name: str, call_id: str, content: str = "ok", *, error: bool = False) -> RunEvent:
    return ToolResultEvent(
        tool_name=tool_name,
        call_id=call_id,
        content=content,
        is_error=error,
        run_id="r",
        root_run_id="r",
        agent="a",
    )


def _decided(tool_name: str, decision: str) -> RunEvent:
    return ApprovalDecidedEvent(
        tool=tool_name, decision=decision, run_id="r", root_run_id="r", agent="a"
    )


CASE = EvalCase(id="c", input="x")


async def test_tool_args_match_is_tolerant_with_text_and_strict_with_numbers() -> None:
    trace = _trace(
        _call("send", "1", to=["Anna@Example.com", "ben@example.com"], body="PO-17  approved", n=3)
    )
    passing: list[dict[str, Any]] = [
        {"to": "anna@example.com"},
        {"to": ["ben@example.com"], "body": "po-17 approved"},
        {"n": 3},
    ]
    for wanted in passing:
        assert (await ToolArgsMatch("send", wanted)(CASE, trace)).passed, wanted
    for wanted in ({"n": 4}, {"cc": "x"}, {"body": "rejected"}):
        score = await ToolArgsMatch("send", wanted)(CASE, trace)
        assert not score.passed and "got:" in score.reason
    missing = await ToolArgsMatch("book", {"id": 1})(CASE, trace)
    assert missing.reason == "book was not called"
    with pytest.raises(ValueError):
        ToolArgsMatch("send", {})


async def test_approval_present_catches_side_effects_without_approval() -> None:
    gated = _trace(
        _call("book", "1"), _decided("book", "approve"), _result("book", "1"),
        _call("book", "2"), _decided("book", "reject"), _result("book", "2", "rejected"),
    )  # fmt: skip
    assert (await ApprovalPresent("book")(CASE, gated)).passed
    ungated = _trace(_call("book", "1"), _result("book", "1"))
    score = await ApprovalPresent("book")(CASE, ungated)
    assert not score.passed and "without an approval" in score.reason
    failed = _trace(_call("book", "1"), _result("book", "1", "boom", error=True))
    assert (await ApprovalPresent("book")(CASE, failed)).passed
    assert (await ApprovalPresent("book")(CASE, _trace())).reason == "book never took effect"


async def test_max_calls_counts_all_or_one_tool() -> None:
    events = [_call("read", "1"), _call("read", "2"), _call("send", "3")]
    assert (await MaxToolCalls(3)(CASE, _trace(*events))).passed
    assert not (await MaxToolCalls(1, tool="read")(CASE, _trace(*events))).passed
    assert not (await MaxToolCalls(2)(CASE, _trace(*events))).passed
    assert (await MaxToolCalls(1, tool="send")(CASE, _trace(*events))).passed


def test_the_trajectory_specs_parse() -> None:
    assert isinstance(parse_scorer("tool_args_match:send:{to: anna@example.com}"), ToolArgsMatch)
    assert isinstance(parse_scorer('tool_args_match:send:{"n": 3}'), ToolArgsMatch)
    assert isinstance(parse_scorer("approval_present:book"), ApprovalPresent)
    assert parse_scorer("max_calls:12").name == "max_calls:12"
    assert isinstance(parse_scorer("max_calls:read:2"), MaxToolCalls)
    assert parse_scorer("tool:lookup").name == "tool:lookup"
    for bad in ("tool_args_match:send", "tool_args_match:send:[1]", "max_calls:x"):
        with pytest.raises(EvalError):
            parse_scorer(bad)


@tool(approval="amount >= 5000")
def pay(invoice: str, amount: float) -> str:
    """Pay an invoice.

    Args:
        invoice: The invoice.
        amount: EUR.
    """

    return f"paid {invoice}"


def payer_model(call: ModelCall) -> Any:
    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    if isinstance(call.last_message, ToolMessage):
        return f"Result: {call.last_message.content}"
    return tool_call("pay", {"invoice": "INV-1", "amount": float(re.findall(r"\d+", request)[0])})


async def test_trajectory_scorers_on_real_runs() -> None:
    def payer() -> Agent:
        return Agent(
            "payer", model=FunctionChatModel(payer_model), tools=[pay], checkpointer=InMemorySaver()
        )

    specs = ("approval_present:pay", "tool_args_match:pay:{invoice: inv-1}", "max_calls:1")
    cases = [
        EvalCase(id="large", input="Pay 6000", decisions=("approve",), scorers=specs),
        EvalCase(id="rejected", input="Pay 7000", decisions=("reject",), scorers=specs),
        EvalCase(id="small", input="Pay 100", scorers=specs),
    ]
    large, rejected, small = (await evaluate(payer, cases)).cases
    assert large.status == "passed", large.scores
    assert rejected.status == "passed", rejected.scores
    assert small.status == "failed"  # below the threshold nobody was asked
    ungated = small.score("approval_present:pay")
    assert ungated is not None
    assert ungated.reason == "pay took effect 1 time(s) without an approval"


# ---------------------------------------------------------------------- judge


def test_the_judge_digest_keeps_what_was_written_and_cuts_reads_first() -> None:
    letter = "Dear Anna, " + "w" * 3800
    events: list[RunEvent] = []
    for n in range(30):
        events += [
            _call("read_file", f"r{n}", path=f"f{n}.txt"),
            _result("read_file", f"r{n}", "r" * 2000),
        ]
    events += [
        _call("send_email", "s", to="anna@example.com", body=letter),
        _result("send_email", "s", "sent"),
        ApprovalRequestedEvent(
            tool="send_email", interrupt_id="i", run_id="r", root_run_id="r", agent="a"
        ),
    ]
    judge = ModelJudge("The letter greets Anna.")
    prompt = judge.prompt(CASE, _trace(*events))

    activity = prompt.split("<tool_activity>")[1].split("</tool_activity>")[0]
    assert letter in activity  # the written letter is there in full
    assert len(activity) <= 8100
    assert "read_file" in activity and "r" * 601 not in activity
    assert prompt.index("<criteria>") < prompt.index("<input>") < prompt.index("<response>")
    assert prompt.startswith("<criteria>\nThe letter greets Anna.")


def test_small_runs_keep_their_read_results() -> None:
    events = [_call("lookup", "1", order="7"), _result("lookup", "1", "shipped on Monday")]
    prompt = ModelJudge("ok").prompt(CASE, _trace(*events))
    assert '- lookup({"order": "7"}) result: shipped on Monday' in prompt


async def test_the_judge_is_not_asked_when_a_deterministic_scorer_failed(example: Path) -> None:
    calls: list[int] = []

    def judge_model(call: ModelCall) -> Any:
        calls.append(1)
        return tool_call("JudgeVerdict", {"score": 1.0, "reasoning": "fine"})

    judge = ModelJudge("anything", model=FunctionChatModel(judge_model))
    report = await evaluate(
        noter,
        [EvalCase(id="a", input="hello")],
        [parse_scorer("contains:never"), judge],
        sandbox=example,
    )
    assert report.cases[0].score("judge").reason == "not judged: contains:never failed"  # type: ignore[union-attr]
    assert not calls and report.judge_usage.requests == 0


# ---------------------------------------------------------------------- awa eval


TOOLS = '''
from langchain_core.tools import tool

from awa.runtime import case_dir


@tool
def note(text: str) -> str:
    """Add a line to the notes."""
    folder = case_dir() / "out"
    folder.mkdir(exist_ok=True)
    with (folder / "notes.txt").open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")
    return f"{len((folder / 'notes.txt').read_text(encoding='utf-8').splitlines())} line(s)"
'''

BLUEPRINT = """
name: notes
connectors:
  docs: {use: workspace, root: docs, include: [read_file]}
agents:
  - name: noter
    tools: [note, "connector:docs.read_file"]
"""


def cli_model(call: ModelCall) -> Any:
    results = [m for m in call.messages if isinstance(m, ToolMessage)]
    if not results:
        return tool_call("docs_read_file", {"path": "hello.txt"})
    if len(results) == 1:
        return tool_call("note", {"text": "hello" if "hello" in str(results[0].content) else "?"})
    return f"Done: {results[-1].content}"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(cli_model))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    monkeypatch.setattr(sys, "path", list(sys.path))
    folder = tmp_path / "project"
    (folder / "docs").mkdir(parents=True)
    (folder / "docs" / "hello.txt").write_text("hello", encoding="utf-8")
    (folder / "out").mkdir()
    (folder / "out" / "notes.txt").write_text("old\n", encoding="utf-8")
    (folder / "big").mkdir()
    (folder / "notes.yaml").write_text(BLUEPRINT, encoding="utf-8")
    (folder / "note_tools.py").write_text(TOOLS, encoding="utf-8")
    (folder / "cases.yaml").write_text(
        "cases:\n  - {id: a, input: go}\n  - {id: b, input: go}\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # relative roots resolve against the case folder, not here
    return folder


def awa_eval(*argv: str) -> int:
    args = build_parser(["eval"]).parse_args(["eval", *argv])
    code: int = args.handler(args)
    return code


def test_awa_eval_runs_every_case_in_a_copy_of_the_blueprint_folder(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_file = project / "report.json"
    code = awa_eval(
        str(project / "notes.yaml"), str(project / "cases.yaml"),
        "--tools", str(project / "note_tools.py"), "--scorer", "contains:2 line(s)",
        "--json", str(report_file),
    )  # fmt: skip
    out, err = capsys.readouterr()
    assert code == 0, out + err
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert [c["output"] for c in report["cases"]] == ["Done: 2 line(s)"] * 2
    assert (project / "out" / "notes.txt").read_text(encoding="utf-8") == "old\n"


def test_awa_eval_without_a_sandbox_shares_the_files(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    code = awa_eval(
        "notes.yaml", "cases.yaml", "--tools", "note_tools.py", "--no-sandbox",
        "--concurrency", "1", "--min-pass-rate", "0", "--json", "r.json",
    )  # fmt: skip
    capsys.readouterr()
    assert code == 0
    assert (project / "out" / "notes.txt").read_text(encoding="utf-8").splitlines()[0] == "old"
    assert len((project / "out" / "notes.txt").read_text(encoding="utf-8").splitlines()) == 3
    with pytest.raises(EvalError, match="exclude each other"):
        awa_eval("notes.yaml", "cases.yaml", "--no-sandbox", "--sandbox", ".")


LEDGER_TOOLS = '''
__all__ = ["note"]
BOOKED: list[str] = []  # an in-memory sample system, like a recipe's ledger


def note(text: str) -> str:
    """Book a note.

    Args:
        text: The note.
    """

    if text in BOOKED:
        return "refused: already booked"
    BOOKED.append(text)
    return f"{len(BOOKED)} line(s)"
'''


@pytest.mark.slow
@pytest.mark.parametrize("sandboxed", [True, False])
def test_every_case_starts_from_the_initial_state_of_the_tools_module(
    project: Path, capsys: pytest.CaptureFixture[str], sandboxed: bool
) -> None:
    # Each repetition starts from a fresh tools module, so a booking of one repetition cannot
    # refuse the next.
    (project / "note_tools.py").write_text(LEDGER_TOOLS, encoding="utf-8")
    report_file = project / "report.json"
    code = awa_eval(
        str(project / "notes.yaml"), str(project / "cases.yaml"),
        "--tools", str(project / "note_tools.py"), "--concurrency", "1",
        "--repetitions", "2", "--min-pass-rate", "0", "--json", str(report_file),
        *([] if sandboxed else ["--no-sandbox"]),
    )  # fmt: skip
    out, err = capsys.readouterr()
    assert code == 0, out + err
    outputs = [c["output"] for c in json.loads(report_file.read_text(encoding="utf-8"))["cases"]]
    if sandboxed:
        assert outputs == ["Done: 1 line(s)"] * 4
    else:  # without a sandbox the cases share the module, as they share the files
        assert outputs[0] == "Done: 1 line(s)"
        assert outputs[1:] == ["Done: refused: already booked"] * 3


def test_the_eval_help_explains_the_widened_limits() -> None:
    text = build_parser(["eval"]).parse_args(["eval", "a.yaml", "b.yaml"])
    assert text.sandbox is None
    parser = build_parser(["eval"])
    help_text = next(
        action.choices["eval"].format_help()
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "a larger value in the blueprint's limits wins" in " ".join(help_text.split())


def test_asyncio_marker_is_not_needed() -> None:  # the module's async tests run in auto mode
    assert asyncio.iscoroutinefunction(test_every_case_writes_into_its_own_copy)


# ---------------------------------------------------------------------- search fixture


class CountingSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        self.queries.append(query)
        return [SearchResult("EU AI Act news", "https://example.test/ai", f"about {query}")]


def test_a_search_fixture_records_once_and_replays_after(tmp_path: Path) -> None:
    live = CountingSearch()
    [search] = web_toolkit(search=live, include=["web_search"])
    file = tmp_path / "search.json"
    with search_fixture(file, record=True) as fixture:
        first = search.invoke({"query": "AI  Act"})
    assert live.queries == ["AI  Act"] and fixture.misses == 1
    assert json.loads(file.read_text(encoding="utf-8"))["ai act | 5"][0]["url"] == (
        "https://example.test/ai"
    )

    with search_fixture(file) as fixture:
        again = search.invoke({"query": "ai act"})
        missed = search.invoke({"query": "something else"})  # an error the model can act on
    assert "not in the recorded search results" in missed
    assert again == first and live.queries == ["AI  Act"] and fixture.hits == 1
    assert "about live again" in search.invoke({"query": "live again"})  # live outside
    with (
        pytest.raises(ConfigurationError, match="record it first"),
        search_fixture(tmp_path / "missing.json"),
    ):
        pass


def writer_model(call: ModelCall) -> Any:
    if isinstance(call.last_message, ToolMessage):
        return f"Done: {call.last_message.content}"
    return tool_call(
        "workspace_write_file", {"path": "outbox/x.txt", "content": "hi", "overwrite": True}
    )


def test_an_approved_workspace_write_lands_in_the_case_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(writer_model))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    (tmp_path / "data").mkdir()
    (tmp_path / "w.yaml").write_text(
        "name: w\nconnectors:\n  workspace: {root: data}\n"
        "agents:\n  - name: a\n    tools: [connector:workspace.write_file]\n"
        "approvals:\n  - tool: connector:workspace.write_file\n",
        encoding="utf-8",
    )
    (tmp_path / "c.yaml").write_text(
        "cases:\n  - {id: a, input: go, decisions: [approve], "
        "scorers: ['approval_present:workspace_write_file', 'tool:workspace_write_file']}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert awa_eval("w.yaml", "c.yaml") == 0, capsys.readouterr()
    assert list((tmp_path / "data").iterdir()) == []  # written only in the case's copy
