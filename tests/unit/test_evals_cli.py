"""`awa eval` and the replay cache."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from awa import Agent
from awa.blueprints import BlueprintError
from awa.cli import build_parser
from awa.errors import ConfigurationError, MissingPriceError
from awa.evals import Dataset, EvalCase, EvalError, EvalReport, evaluate
from awa.models import ModelRegistry
from awa.models.replay import active_replay, refuse_replay, replaying
from awa.persistence.cache import InMemoryCacheStore, SqliteCacheStore
from awa.runtime.cache import Cache, CacheSettings
from awa.testing import FunctionChatModel, ModelCall, tool_call

TOOLS = '''
from langchain_core.tools import tool


@tool
def lookup(order_id: str) -> str:
    """Look up an order."""
    return {"7": "shipped", "8": "delivered"}.get(order_id, "unknown")


@tool
def pay(order_id: str) -> str:
    """Pay an order."""
    return f"paid {order_id}"
'''

SOLO = """
name: solo
agents:
  - name: clerk
    description: Looks up orders.
    tools: [lookup]
"""

GATED = """
name: payments
agents:
  - name: cashier
    tools: [pay]
approvals:
  - tool: pay
"""

CASES = """
name: orders
cases:
  - id: seven
    input: Where is order 7?
    expected: shipped
    tags: [orders]
    scorers: ["tool:lookup"]
  - id: eight
    input: Where is order 8?
    expected: delivered
"""


def respond(call: ModelCall) -> Any:
    last = call.last_message
    if isinstance(last, ToolMessage):
        return f"Order status: {last.content}"
    order = re.search(r"\d+", str(last.content))
    tool = "pay" if "pay" in call.tool_names else "lookup"
    return tool_call(tool, {"order_id": order.group() if order else "0"})


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(respond))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "eval_cli_tools", raising=False)
    for name, text in {
        "eval_cli_tools.py": TOOLS,
        "solo.yaml": SOLO,
        "gated.yaml": GATED,
        "cases.yaml": CASES,
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def awa_eval(*argv: str) -> int:
    args = build_parser(["eval"]).parse_args(["eval", *argv])
    code: int = args.handler(args)
    return code


BASE = ("solo.yaml", "cases.yaml", "--tools", "eval_cli_tools.py")


def test_passing_evaluation_prints_markdown_and_writes_json(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = awa_eval(
        *BASE, "--scorer", "contains", "--json", "report.json", "--markdown", "r.md",
        "--format", "markdown",
    )  # fmt: skip
    out, err = capsys.readouterr()
    assert code == 0, out + err
    assert out.startswith("# Evaluation: solo\n")
    assert "- Pass rate: **100.0%** (2/2 runs)" in out
    assert "| tool:lookup | 1.00 | 100.0% | 1 | 0 |" in out
    assert "wrote report.json" in err
    report = EvalReport.from_json(workspace / "report.json")
    assert [r.output for r in report.cases] == ["Order status: shipped", "Order status: delivered"]
    assert report.system == "clerk"
    assert (workspace / "r.md").read_text(encoding="utf-8") == out


def test_the_terminal_gets_a_table_and_the_file_markdown(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = awa_eval(*BASE, "--scorer", "contains", "--markdown", "r.md")
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.startswith("Evaluation: solo\n================\n")
    assert "System clerk on dataset orders: 2 case(s) x 1 repetition(s)." in out
    assert "- Pass rate: 100.0% (2/2 runs)" in out
    assert "**" not in out and "| ---" not in out and "`" not in out
    scorer_rows = [line.split() for line in out.splitlines() if line.startswith("  tool:lookup")]
    assert scorer_rows == [["tool:lookup", "1.00", "100.0%", "1", "0"]]
    assert (workspace / "r.md").read_text(encoding="utf-8").startswith("# Evaluation: solo\n")


def test_below_the_minimum_pass_rate_exits_1(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = awa_eval(*BASE, "--scorer", "contains:shipped")
    _, err = capsys.readouterr()
    assert code == 1
    assert "evaluation failed: pass rate 50.0% (minimum 100.0%)" in err
    assert awa_eval(*BASE, "--scorer", "contains:shipped", "--min-pass-rate", "0.5") == 0


def test_baseline_regressions_exit_1(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert awa_eval(*BASE, "--scorer", "exact", "--json", "baseline.json") == 1
    assert awa_eval(*BASE, "--scorer", "contains", "--json", "good.json") == 0
    capsys.readouterr()

    assert awa_eval(*BASE, "--scorer", "contains", "--baseline", "good.json") == 0
    out, _ = capsys.readouterr()
    assert "Comparison with solo\n" in out and "0 regression(s)" in out

    (workspace / "cases.yaml").write_text(CASES.replace("expected: shipped", "expected: lost"))
    code = awa_eval(
        *BASE, "--scorer", "contains", "--baseline", "good.json", "--min-pass-rate", "0"
    )
    out, err = capsys.readouterr()
    assert code == 1
    assert "- seven / contains: pass rate 100.0% -> 0.0%" in out
    assert "regression(s) against the baseline" in err


def test_tags_and_options(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lines = [
        {"id": "seven", "input": "Where is order 7?", "expected": "shipped", "tags": ["orders"]},
        {"id": "other", "input": "Where is order 9?", "tags": ["misc"]},
    ]
    (workspace / "cases.json").write_text(json.dumps({"cases": lines}))
    code = awa_eval(
        "solo.yaml",
        "cases.json",
        "--tools",
        "eval_cli_tools.py",
        "--tag",
        "orders",
        "--scorer",
        "contains",
        "--concurrency",
        "2",
        "--repetitions",
        "2",
        "--max-requests",
        "5",
        "--max-total-tokens",
        "100000",
        "--timeout",
        "30",
    )
    out, _ = capsys.readouterr()
    assert code == 0
    assert "1 case(s) x 2 repetition(s)" in out
    assert any(line.split()[:2] == ["seven", "2/2"] for line in out.splitlines() if line)


def test_usage_limits_and_fail_fast(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = awa_eval(*BASE, "--max-requests", "1", "--fail-fast", "--concurrency", "1")
    out, err = capsys.readouterr()
    assert code == 1
    assert "UsageLimitExceededError" in out
    assert "stopped early" in err


def test_approval_gates_are_evaluated_with_a_checkpointer(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "pay.yaml").write_text("- {id: pay, input: Pay order 5}\n", encoding="utf-8")
    code = awa_eval(
        "gated.yaml",
        "pay.yaml",
        "--tools",
        "eval_cli_tools.py",
        "--scorer",
        "approval:pay",
        "--scorer",
        "no_tool:lookup",
    )
    out, _ = capsys.readouterr()
    assert code == 0, out


@pytest.mark.parametrize(
    ("argv", "error", "message"),
    [
        (("--scorer", "bogus"), EvalError, "Unknown scorer spec 'bogus'"),
        (("--tag", "nothing"), EvalError, "has no cases to run"),
        (("--concurrency", "0"), EvalError, "--concurrency must be at least 1"),
        (("--min-pass-rate", "2"), EvalError, "--min-pass-rate must be between 0 and 1"),
        (("--baseline", "missing.json"), EvalError, "Cannot read report file"),
        (("--json", "."), EvalError, "is a folder; name a file"),
        (("--markdown", "cases.yaml/report.md"), EvalError, "is not a writable folder"),
    ],
)
def test_invalid_options_are_clean_errors(
    workspace: Path, argv: tuple[str, ...], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=re.escape(message)):
        awa_eval(*BASE, *argv)


def test_blueprint_problems_are_reported_before_any_run(workspace: Path) -> None:
    with pytest.raises(BlueprintError):
        awa_eval("solo.yaml", "cases.yaml")  # the lookup tool is missing
    with pytest.raises(EvalError, match="Cannot read dataset file"):
        awa_eval("solo.yaml", "nope.yaml", "--tools", "eval_cli_tools.py")


# ------------------------------------------------------------------ reliability and prices


@pytest.fixture
def priced(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The workspace with a named model that gets order 7 wrong on its second run."""

    sevens = {"answers": 0}

    def flaky(call: ModelCall) -> Any:
        last = call.last_message
        if isinstance(last, ToolMessage):
            if "shipped" in str(last.content):
                sevens["answers"] += 1
                if sevens["answers"] == 2:
                    return "Order status: lost"
            return f"Order status: {last.content}"
        order = re.search(r"\d+", str(last.content))
        return tool_call("lookup", {"order_id": order.group() if order else "0"})

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(flaky, model_name="gpt-test"))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    (workspace / "prices.yaml").write_text(
        "gpt-test: {input_per_mtok: 1000000, output_per_mtok: 2000000}\n", encoding="utf-8"
    )
    return workspace


def test_prices_and_pass_hat_k_in_the_report(
    priced: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = awa_eval(
        *BASE, "--scorer", "contains", "--repetitions", "3", "--concurrency", "1",
        "--prices", "prices.yaml", "--json", "report.json", "--min-pass-hat-k", "0.5",
        "--min-pass-rate", "0.8",
    )  # fmt: skip
    out, err = capsys.readouterr()
    assert code == 0, out + err
    report = EvalReport.from_json(priced / "report.json")
    assert report.pass_rate == 5 / 6 and report.pass_hat_k == 0.5
    assert report.cost is not None and report.cost > 0
    tokens = report.usage
    assert report.cost == pytest.approx(tokens.input_tokens + 2 * tokens.output_tokens)
    assert "  pass^3 (all 3 runs of a case pass)" in out and "50.0%" in out
    assert "not computed" not in out


def test_min_pass_hat_k_fails_the_command(priced: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = awa_eval(
        *BASE, "--scorer", "contains", "--repetitions", "3", "--concurrency", "1",
        "--min-pass-rate", "0.5", "--min-pass-hat-k", "0.9",
    )  # fmt: skip
    _, err = capsys.readouterr()
    assert code == 1
    assert "evaluation failed: pass^3 50.0% (minimum 90.0%)" in err


@pytest.mark.parametrize(
    ("argv", "error", "message"),
    [
        (("--min-pass-hat-k", "0.5"), EvalError, "--min-pass-hat-k needs --repetitions 2 or more"),
        (
            ("--min-pass-hat-k", "2", "--repetitions", "2"),
            EvalError,
            "--min-pass-hat-k must be between 0 and 1",
        ),
        (("--prices", "nope.yaml"), ConfigurationError, "Cannot read prices file"),
        (("--prices", "cases.yaml"), ConfigurationError, "model 'name'"),
    ],
)
def test_invalid_reliability_options_are_clean_errors(
    workspace: Path, argv: tuple[str, ...], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=re.escape(message)):
        awa_eval(*BASE, *argv)


def test_a_model_without_a_price_is_a_clear_error(priced: Path) -> None:
    (priced / "prices.yaml").write_text(
        "other: {input_per_mtok: 1, output_per_mtok: 1}\n", encoding="utf-8"
    )
    with pytest.raises(MissingPriceError, match="gpt-test"):
        awa_eval(*BASE, "--prices", "prices.yaml")


LIMITED = (
    SOLO
    + """limits: {max_cost: 0.0001}
prices:
  gpt-test: {input_per_mtok: 1000000, output_per_mtok: 2000000}
"""
)


def test_the_prices_file_prices_the_limit_as_well_as_the_report(
    priced: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (priced / "limited.yaml").write_text(LIMITED, encoding="utf-8")
    (priced / "cheap.yaml").write_text(
        "gpt-test: {input_per_mtok: 0.001, output_per_mtok: 0.001}\n", encoding="utf-8"
    )
    base = ("limited.yaml", "cases.yaml", "--tools", "eval_cli_tools.py", "--scorer", "contains")

    # With the blueprint's prices one reply costs more than the limit ...
    assert awa_eval(*base) == 1
    assert "max_cost=0.0001" in capsys.readouterr().out
    # ... with the prices you pass, one table prices the limit and the report.
    assert awa_eval(*base, "--prices", "cheap.yaml", "--json", "cheap.json") == 0
    assert EvalReport.from_json(priced / "cheap.json").cost == pytest.approx(
        EvalReport.from_json(priced / "cheap.json").usage.total_tokens * 0.001 / 1_000_000
    )


def test_a_drop_of_pass_hat_k_alone_is_not_called_a_regression_count(
    priced: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    assert awa_eval(*BASE, "--json", "base.json") == 0
    dropped = SimpleNamespace(
        regressions=[], pass_hat_k_dropped=True, has_regressions=True, to_markdown=lambda: ""
    )
    monkeypatch.setattr(EvalReport, "compare", lambda self, baseline, **_: dropped)
    capsys.readouterr()

    assert awa_eval(*BASE, "--baseline", "base.json") == 1

    err = capsys.readouterr().err
    assert "evaluation failed: pass^k dropped against the baseline" in err
    assert "0 regression" not in err


# ------------------------------------------------------------------ cost budget and --case


def test_cost_budget_without_prices_raises_before_any_model_call(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def counted(call: ModelCall) -> Any:
        calls.append("call")
        return respond(call)

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(counted))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    with pytest.raises(EvalError, match="needs prices"):
        awa_eval(*BASE, "--cost-budget", "1")
    assert calls == []


def test_cost_budget_gates_the_total(priced: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = awa_eval(*BASE, "--prices", "prices.yaml", "--cost-budget", "1")
    _, err = capsys.readouterr()
    assert code == 1
    assert re.search(r"evaluation failed: cost [\d.]+ above the budget 1\.0000", err)
    assert awa_eval(*BASE, "--prices", "prices.yaml", "--cost-budget", "1000000000") == 0


def test_cost_budget_must_be_positive(workspace: Path) -> None:
    with pytest.raises(EvalError, match="--cost-budget must be greater than 0"):
        awa_eval(*BASE, "--cost-budget", "0")


def test_case_reruns_one_case(workspace: Path) -> None:
    assert awa_eval(*BASE, "--case", "eight", "--json", "one.json") == 0
    assert [c.case_id for c in EvalReport.from_json(workspace / "one.json").cases] == ["eight"]
    assert awa_eval(*BASE, "--case", "eight", "--case", "seven", "--json", "two.json") == 0
    assert [c.case_id for c in EvalReport.from_json(workspace / "two.json").cases] == [
        "seven",
        "eight",
    ]
    with pytest.raises(EvalError, match="has no cases to run"):
        awa_eval(*BASE, "--case", "eight", "--tag", "orders")
    assert awa_eval(*BASE, "--case", "seven", "--tag", "orders", "--json", "both.json") == 0
    assert [c.case_id for c in EvalReport.from_json(workspace / "both.json").cases] == ["seven"]


def test_unknown_case_suggests_the_close_match(workspace: Path) -> None:
    with pytest.raises(EvalError) as raised:
        awa_eval(*BASE, "--case", "sevn")
    message = str(raised.value)
    assert "'sevn'" in message and "did you mean seven?" in message
    assert "(cases: seven, eight)" in message


TOOLS_REPLAY = '''
from langchain_core.tools import tool


@tool
def lookup(order_id: str) -> str:
    """Look up an order."""
    return {"7": "shipped", "8": "delivered"}.get(order_id, "unknown")
'''


REAL = """
name: real
agents:
  - name: clerk
    tools: [lookup, "connector:jira"]
"""

CASES_REPLAY = """
name: orders
cases:
  - id: seven
    input: Where is order 7?
    expected: shipped
  - id: eight
    input: Where is order 8?
    expected: delivered
"""


class Counting:
    """A scripted model that counts the calls that really reach it."""

    def __init__(self, name: str = "gpt-test") -> None:
        self.calls = 0
        self.model = FunctionChatModel(self.respond, model_name=name)

    def respond(self, call: ModelCall) -> Any:
        self.calls += 1
        last = call.last_message
        if isinstance(last, ToolMessage):
            return f"Order status: {last.content}"
        order = re.search(r"\d+", str(last.content))
        return tool_call("lookup", {"order_id": order.group() if order else "0"})


def lookup(order_id: str) -> str:
    """Look up an order."""
    return {"7": "shipped", "8": "delivered"}.get(order_id, "unknown")


DATASET = Dataset(
    name="orders",
    cases=[
        EvalCase(id="seven", input="Where is order 7?", expected="shipped"),
        EvalCase(id="eight", input="Where is order 8?", expected="delivered"),
    ],
)


async def run_eval(model: Any, store: Any, *, tenant: str = "local") -> EvalReport:
    cache = Cache(CacheSettings(), store, project="solo")
    with replaying(cache, tenant=tenant):
        return await evaluate(
            lambda: Agent("clerk", model=model, tools=[lookup]), DATASET, ["contains"]
        )


async def test_a_second_unchanged_run_makes_no_model_call_and_costs_nothing() -> None:
    model, store = Counting(), InMemoryCacheStore()

    first = await run_eval(model.model, store)
    calls = model.calls
    second = await run_eval(model.model, store)

    assert calls == 4 and model.calls == calls  # 2 cases x 2 calls, then none
    assert first.caching().model_calls == 4 and first.caching().replayed_calls == 0
    assert second.caching().model_calls == 0 and second.caching().replayed_calls == 4
    assert second.usage.total_tokens == 0
    assert [c.output for c in second.cases] == [c.output for c in first.cases]
    assert second.pass_rate == 1.0


async def test_two_tenants_never_share_recorded_responses() -> None:
    model, store = Counting(), InMemoryCacheStore()

    await run_eval(model.model, store, tenant="acme")
    await run_eval(model.model, store, tenant="globex")

    assert model.calls == 8


async def test_two_models_never_share_recorded_responses() -> None:
    store = InMemoryCacheStore()
    first, other = Counting("model-a"), Counting("model-b")

    await run_eval(first.model, store)
    await run_eval(other.model, store)

    assert first.calls == 4 and other.calls == 4


async def test_outside_a_replay_scope_nothing_is_recorded_or_replayed() -> None:
    model = Counting()
    agent = Agent("clerk", model=model.model, tools=[lookup])

    await agent.run("Where is order 7?")
    await agent.run("Where is order 7?")

    assert model.calls == 4 and active_replay() is None
    assert model.model.cache is None  # the model instance itself is never changed


def test_a_replay_scope_needs_a_store() -> None:
    with pytest.raises(ConfigurationError, match="store"), replaying(Cache()):
        pass


def test_server_web_app_and_worker_refuse_a_replay_scope(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from awa.automation import Worker
    from awa.server import create_app
    from awa.ui import create_ui_app

    with replaying(Cache(CacheSettings(), InMemoryCacheStore())):
        with pytest.raises(ConfigurationError, match="HTTP server refuses the replay cache"):
            create_app({"clerk": Agent("clerk")})
        with pytest.raises(ConfigurationError, match="web app refuses the replay cache"):
            create_ui_app(tmp_path)
        with pytest.raises(ConfigurationError, match="worker refuses the replay cache"):
            Worker([])
    refuse_replay("anything")  # outside the scope: no error


# ---------------------------------------------------------------- the command


@pytest.fixture
def workspace_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Counting]:
    counting = Counting()
    registry = ModelRegistry()
    registry.register("default", counting.model)
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "replay_tools", raising=False)
    for name, text in {
        "replay_tools.py": TOOLS_REPLAY,
        "solo.yaml": SOLO,
        "real.yaml": REAL,
        "cases.yaml": CASES_REPLAY,
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path, counting


def test_eval_replay_records_then_replays_from_a_file(
    workspace_replay: tuple[Path, Counting], capsys: pytest.CaptureFixture[str]
) -> None:
    folder, counting = workspace_replay
    base = ("solo.yaml", "cases.yaml", "--tools", "replay_tools.py", "--replay")

    assert awa_eval(*base, "--json", "first.json") == 0
    recorded = counting.calls
    assert awa_eval(*base, "--json", "second.json") == 0
    out, err = capsys.readouterr()

    assert recorded == 4 and counting.calls == recorded
    assert "replay: 4 model call(s) replayed, 0 recorded" in err
    assert any(
        line.split()[-1:] == ["4"] and "model calls replayed (no cost)" in line
        for line in out.splitlines()
    )
    second = EvalReport.from_json(folder / "second.json")
    assert second.caching().model_calls == 0 and second.usage.total_tokens == 0
    assert (folder / ".awa" / "replay.db").is_file()
    assert SqliteCacheStore(folder / ".awa" / "replay.db").stats()[0].kind == "replay"


def test_eval_without_replay_calls_the_model_every_time(
    workspace_replay: tuple[Path, Counting],
) -> None:
    _, counting = workspace_replay
    base = ("solo.yaml", "cases.yaml", "--tools", "replay_tools.py")

    awa_eval(*base)
    awa_eval(*base)

    assert counting.calls == 8


def test_eval_replay_refuses_tools_that_reach_real_systems(
    workspace_replay: tuple[Path, Counting],
) -> None:
    with pytest.raises(EvalError, match="stand-in tools.*connector:jira"):
        awa_eval("real.yaml", "cases.yaml", "--tools", "replay_tools.py", "--replay")


async def test_the_testing_fixture_replays_from_a_file(tmp_path: Path) -> None:
    from awa.testing import replay_models

    model = Counting()
    for _ in range(2):
        with replay_models(tmp_path / "rec.db"):
            await Agent("clerk", model=model.model, tools=[lookup]).run("Where is order 7?")

    assert model.calls == 2


def test_stable_keeps_a_non_json_prompt_unchanged() -> None:
    from awa.models.replay import _stable

    assert _stable("not json at all") == "not json at all"
    assert _stable('{"id": "x", "keep": 1}') == {"keep": 1}
