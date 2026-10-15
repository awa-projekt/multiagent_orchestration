"""`EvalReport`: aggregation, thresholds, the gate, reliability, latency and cost, Markdown, JSON and baseline comparison."""

from __future__ import annotations

from datetime import UTC, datetime
from math import fsum
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from awa import Agent
from awa.contracts import ModelPrice, PriceTable, Usage
from awa.errors import MissingPriceError
from awa.evals import CaseResult, Comparison, Contains, EvalError, EvalReport, Score, evaluate
from awa.evals.dataset import Dataset, EvalCase
from awa.evals.report import Gate
from awa.testing import FunctionChatModel, ModelCall


def score(name: str, value: float, passed: bool | None = None, reason: str = "") -> Score:
    return Score(
        name=name, value=value, passed=value >= 0.5 if passed is None else passed, reason=reason
    )


def result(case_id: str, *scores: Score, repetition: int = 0, **kwargs: Any) -> CaseResult:
    status = kwargs.pop("status", None) or ("passed" if all(s.passed for s in scores) else "failed")
    return CaseResult(
        case_id=case_id, repetition=repetition, status=status, scores=list(scores), **kwargs
    )


def report(*cases: CaseResult, name: str = "run", **kwargs: Any) -> EvalReport:
    return EvalReport(
        name=name,
        system="desk",
        dataset="support",
        started_at=datetime(2026, 9, 27, tzinfo=UTC),
        cases=list(cases),
        **kwargs,
    )


def sample() -> EvalReport:
    return report(
        result(
            "a",
            score("contains", 1.0),
            score("tool:x", 1.0),
            duration_ms=100,
            usage=Usage(requests=2, input_tokens=100, output_tokens=20),
        ),
        result(
            "b",
            score("contains", 0.0, reason="missing 'refund'"),
            score("tool:x", 1.0),
            duration_ms=300,
        ),
        result(
            "c",
            score("contains", 0.0, passed=False),
            status="error",
            error="RuntimeError: boom",
            error_type="RuntimeError",
        ),
        usage=Usage(requests=2, input_tokens=100, output_tokens=20, tool_calls=1),
        duration_ms=1234,
    )


def test_aggregates() -> None:
    current = sample()
    assert current.pass_rate == pytest.approx(1 / 3)
    assert current.errors == 1
    assert [r.case_id for r in current.failures()] == ["b", "c"]
    contains = current.scorer("contains")
    assert contains is not None
    assert (contains.count, contains.passed, contains.failed) == (3, 1, 2)
    assert contains.mean == pytest.approx(1 / 3)
    tool = current.scorer("tool:x")
    assert tool is not None and tool.pass_rate == 1.0 and tool.count == 2
    assert current.scorer("missing") is None
    assert [s.name for s in current.scorers] == ["contains", "tool:x"]


def test_passed_thresholds() -> None:
    current = sample()
    assert not current.passed()
    assert current.passed(0.3)
    assert current.passed(0.3, thresholds={"tool:x": 1.0})
    assert not current.passed(0.3, thresholds={"contains": 0.5})
    assert not current.passed(0.3, thresholds={"never_ran": 0.0})
    assert not current.passed(0.3, max_errors=0)
    assert not report().passed(0.0)
    skipped = report(result("a", score("s", 1.0)), result("b", status="skipped"))
    assert skipped.pass_rate == 1.0 and skipped.passed()


def test_markdown() -> None:
    text = sample().to_markdown()
    assert text.startswith("# Evaluation: run\n")
    assert "System `desk` on dataset `support`: 3 case(s) x 1 repetition(s)." in text
    assert "- Pass rate: **33.3%** (1/3 runs)" in text
    assert "- Usage: 2 model call(s), 120 tokens (100 in, 20 out), 1 tool call(s)" in text
    assert "- Duration: 1.2 s" in text
    assert "| contains | 0.33 | 33.3% | 1 | 2 |" in text
    assert "| a | 1/1 | contains 1.00, tool:x 1.00 | 100 | 120 |" in text
    assert "| b | 0/1 | **contains 0.00**, tool:x 1.00 | 300 | 0 |" in text
    assert "| c | 0/1 (error) |" in text
    assert "- **b (repetition 1)**: contains: missing 'refund'" in text
    assert "- **c (repetition 1)**: RuntimeError: boom" in text
    assert "Judge usage" not in text


def test_markdown_escapes_cells_and_limits_failures() -> None:
    many = report(*(result(f"case|{i}", score("s", 0.0)) for i in range(5)))
    text = many.to_markdown(max_failures=2)
    assert "| case\\|0 |" in text
    assert "- ... and 3 more" in text


def test_json_round_trip(tmp_path: Path) -> None:
    current = sample()
    path = current.save(tmp_path / "report.json")
    text = path.read_text(encoding="utf-8")
    assert '"pass_rate"' in text and '"scorers"' in text
    loaded = EvalReport.from_json(path)
    assert loaded.cases == current.cases
    assert loaded.pass_rate == current.pass_rate
    assert EvalReport.from_json(current.to_json(indent=None)).name == "run"
    assert EvalReport.from_json(str(path)).dataset == "support"
    with pytest.raises(EvalError, match="Not an evaluation report"):
        EvalReport.from_json('{"name": 1}')
    with pytest.raises(EvalError, match="Cannot read report file"):
        EvalReport.from_json(tmp_path / "missing.json")


async def test_json_of_a_real_evaluation_excludes_live_records() -> None:
    class Echo:
        name = "echo"
        card: Any = None

        async def run(self, input: Any, **kwargs: Any) -> Any:
            from awa.contracts import RunResult

            return RunResult(run_id="r", agent="echo", output={"text": input})

    dataset = Dataset(cases=(EvalCase(id="a", input="hi"),))
    live = await evaluate(Echo(), dataset, ["json_fields"])
    assert live.cases[0].trace is not None
    loaded = EvalReport.from_json(live.to_json())
    assert loaded.cases[0].trace is None and loaded.cases[0].events == ()
    assert loaded.cases[0].output == {"text": "hi"}
    assert loaded.cases[0].scores[0].error is not None  # no expected mapping


def test_compare_finds_regressions_improvements_and_case_changes() -> None:
    baseline = report(
        result("a", score("contains", 1.0), score("judge", 0.9)),
        result("b", score("contains", 0.0)),
        result("gone", score("contains", 1.0)),
        name="baseline",
    )
    current = report(
        result("a", score("contains", 0.0), score("judge", 0.85)),
        result("b", score("contains", 1.0)),
        result("new", score("contains", 1.0)),
        name="current",
    )
    comparison = current.compare(baseline)
    assert comparison.has_regressions
    assert [(c.case_id, c.scorer) for c in comparison.regressions] == [
        ("a", None),
        ("a", "contains"),
    ]
    assert [(c.case_id, c.scorer) for c in comparison.improvements] == [
        ("b", None),
        ("b", "contains"),
    ]
    assert comparison.regressions[1].delta == -1.0
    assert comparison.regressions[1].label == "a / contains"
    assert comparison.new_cases == ["new"] and comparison.missing_cases == ["gone"]
    judge = next(s for s in comparison.scorers if s.name == "judge")
    assert judge.baseline_mean == 0.9 and judge.current_mean == 0.85

    strict = current.compare(baseline, tolerance=0.0)
    assert ("a", "judge") in [(c.case_id, c.scorer) for c in strict.regressions]

    text = comparison.to_markdown()
    assert "## Comparison with baseline" in text
    assert "Pass rate 66.7% -> 66.7% (2 regression(s), 2 improvement(s))" in text
    assert "- a / contains: pass rate 100.0% -> 0.0%, mean 1.00 -> 0.00" in text
    assert "### Improvements" in text
    assert "New cases: new" in text and "Missing cases: gone" in text


def test_compare_with_itself_is_clean() -> None:
    current = sample()
    comparison = current.compare(EvalReport.from_json(current.to_json()))
    assert not comparison.has_regressions and not comparison.improvements


def test_compare_uses_rates_over_repetitions() -> None:
    baseline = report(
        result("a", score("s", 1.0)), result("a", score("s", 1.0), repetition=1), name="b"
    )
    flaky = report(result("a", score("s", 1.0)), result("a", score("s", 0.0), repetition=1))
    change = flaky.compare(baseline).regressions[-1]
    assert (change.baseline_pass_rate, change.current_pass_rate) == (1.0, 0.5)
    assert "| a | 1/2 | **s 0.50** |" in flaky.to_markdown()


def _golden_report(improved: bool) -> EvalReport:
    from awa.evals.report import CaseCaching

    def run(case_id: str, repetition: int, *scores: Score, **kwargs: Any) -> CaseResult:
        return result(case_id, *scores, repetition=repetition, **kwargs)

    cases = [
        run(
            "a",
            0,
            score("contains", 1.0),
            score("tool:x", 1.0),
            duration_ms=100,
            cost=0.01,
            usage=Usage(requests=2, input_tokens=100, output_tokens=20),
            caching=CaseCaching(model_calls=2, tool_hits=1, tool_misses=1, model_ms=[40, 50]),
        ),
        run(
            "a",
            1,
            score("contains", 1.0),
            score("tool:x", 1.0),
            duration_ms=120,
            cost=0.02,
            usage=Usage(requests=2, input_tokens=100, output_tokens=20),
        ),
        run(
            "b",
            0,
            score("contains", 1.0 if improved else 0.0, reason="missing 'refund'"),
            score("tool:x", 1.0),
            duration_ms=300,
            cost=0.03,
        ),
        run("b", 1, score("contains", 1.0), score("tool:x", 1.0), duration_ms=310, cost=0.01),
        run(
            "c",
            0,
            score("contains", 0.0, passed=False),
            status="error",
            error="RuntimeError: boom",
            error_type="RuntimeError",
        ),
        run("c", 1, score("contains", 1.0), duration_ms=50, cost=0.0),
    ]
    return report(
        *cases,
        name="gold",
        usage=Usage(requests=4, input_tokens=200, output_tokens=40, tool_calls=2),
        duration_ms=1234,
        repetitions=2,
    )


def test_markdown_is_unchanged_by_the_render_split() -> None:
    current = _golden_report(True)
    assert current.to_markdown() == (
        "# Evaluation: gold\n"
        "\n"
        "System `desk` on dataset `support`: 3 case(s) x 2 repetition(s).\n"
        "\n"
        "- Pass rate: **83.3%** (5/6 runs)\n"
        "- Errors: 1\n"
        "- Usage: 4 model call(s), 240 tokens (200 in, 40 out), 2 tool call(s)\n"
        "- Duration: 1.2 s\n"
        "\n"
        "## Reliability\n"
        "\n"
        "| measure | value |\n"
        "| --- | ---: |\n"
        "| pass@1 (one run passes) | 83.3% |\n"
        "| pass@2 (at least one of 2 runs passes) | 100.0% |\n"
        "| pass^2 (all 2 runs of a case pass) | 66.7% |\n"
        "| latency p50 / p95 | 100 ms / 310 ms |\n"
        "| cost | 0.0700 (0.0117 per run) |\n"
        "| cache hit rate | 0.0% |\n"
        "\n"
        "Passed only some of the time:\n"
        "- c: 1 of 2 runs\n"
        "\n"
        "## Caching\n"
        "\n"
        "| measure | value |\n"
        "| --- | ---: |\n"
        "| model calls answered by the provider | 2 |\n"
        "| model calls replayed (no cost) | 0 |\n"
        "| model call p50 / p95 | 40 ms / 50 ms |\n"
        "| tool results reused / stored | 1 / 1 |\n"
        "\n"
        "## Scorers\n"
        "\n"
        "| scorer | mean | pass rate | passed | failed |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        "| contains | 0.83 | 83.3% | 5 | 1 |\n"
        "| tool:x | 1.00 | 100.0% | 4 | 0 |\n"
        "\n"
        "## Cases\n"
        "\n"
        "| case | passed | scores | ms | tokens |\n"
        "| --- | --- | --- | ---: | ---: |\n"
        "| a | 2/2 | contains 1.00, tool:x 1.00 | 110 | 120 |\n"
        "| b | 2/2 | contains 1.00, tool:x 1.00 | 305 | 0 |\n"
        "| c | 1/2 (error) | **contains 0.50** | 25 | 0 |\n"
        "\n"
        "## Failures\n"
        "\n"
        "- **c (repetition 1)**: RuntimeError: boom\n"
    )
    assert current.compare(_golden_report(False)).to_markdown() == (
        "## Comparison with gold\n"
        "\n"
        "Pass rate 66.7% -> 83.3% (0 regression(s), 2 improvement(s))\n"
        "\n"
        "pass^2 33.3% -> 66.7%\n"
        "\n"
        "| scorer | baseline mean | mean | baseline pass rate | pass rate |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        "| contains | 0.67 | 0.83 | 66.7% | 83.3% |\n"
        "| tool:x | 1.00 | 1.00 | 100.0% | 100.0% |\n"
        "\n"
        "### Improvements\n"
        "- b: pass rate 50.0% -> 100.0%, mean 0.50 -> 1.00\n"
        "- b / contains: pass rate 50.0% -> 100.0%, mean 0.50 -> 1.00\n"
    )


def test_error_runs_lower_pass_rate_and_pass_hat_k() -> None:
    ok = [result("a", score("s", 1.0), repetition=i) for i in range(2)]
    broken = [
        result("b", score("s", 1.0), repetition=0),
        result("b", score("s", 0.0, passed=False), repetition=1, status="error", error="X: y"),
    ]
    current = report(*ok, *broken, repetitions=2)
    assert current.pass_rate == pytest.approx(0.75)
    assert current.pass_hat_k == pytest.approx(0.5)


def run(
    case_id: str, passed: bool, *, repetition: int = 0, cost: float | None = None
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        repetition=repetition,
        status="passed" if passed else "failed",
        scores=[Score(name="contains", value=float(passed), passed=passed)],
        cost=cost,
    )


def report_gate(*cases: CaseResult, **kwargs: Any) -> EvalReport:
    return EvalReport(
        name="r",
        system="desk",
        dataset="d",
        started_at=datetime(2026, 9, 27, tzinfo=UTC),
        cases=list(cases),
        **kwargs,
    )


def test_cost_gate_without_prices_fails_with_the_clear_message() -> None:
    gate = report_gate(run("a", True)).gate(max_cost=1.0)
    assert not gate.passed
    assert len(gate.reasons) == 1 and "needs prices" in gate.reasons[0]
    assert not report_gate(run("a", True)).gate(max_cost_per_run=1.0).passed


def test_cost_budget_and_per_run() -> None:
    current = report_gate(run("a", True, cost=0.2), run("b", True, cost=0.2))
    assert current.cost == pytest.approx(0.4)
    assert current.gate(max_cost=0.4, max_cost_per_run=0.2).passed  # equality passes
    over = current.gate(max_cost=0.39, max_cost_per_run=0.19)
    assert over.reasons == (
        "cost 0.4000 above the budget 0.3900",
        "cost per run 0.2000 above 0.1900",
    )
    assert not current.gate(max_cost=0.39).passed
    assert current.passed(max_cost=0.5) and not current.passed(max_cost_per_run=0.19)


def test_passed_equals_gate_passed() -> None:
    cases = [run("a", True, repetition=0), run("a", False, repetition=1), run("b", True)]
    current = report_gate(*cases, repetitions=2, stopped_early=False)
    combos: list[dict[str, Any]] = [
        {},
        {"min_pass_rate": 0.5},
        {"min_pass_rate": 0.0, "min_pass_hat_k": 0.9},
        {"min_pass_rate": 0.0, "thresholds": {"contains": 0.9}},
        {"min_pass_rate": 0.0, "thresholds": {"missing": 0.1}},
        {"min_pass_rate": 0.0, "max_errors": 0},
    ]
    for options in combos:
        assert current.passed(**options) == current.gate(**options).passed
    assert not report_gate().passed() and report_gate().gate().reasons == ("no run finished",)
    assert isinstance(current.gate(), Gate)


def test_gate_reasons_keep_the_cli_texts() -> None:
    current = report_gate(run("a", True), run("b", False))
    assert current.gate(0.9).reasons == ("pass rate 50.0% (minimum 90.0%)",)
    assert current.gate(0.0).passed

    reps = report_gate(run("a", True), run("a", False, repetition=1), repetitions=2)
    assert reps.gate(0.0, min_pass_hat_k=0.9).reasons == ("pass^2 0.0% (minimum 90.0%)",)
    single = report_gate(run("a", True))
    assert single.gate(0.0, min_pass_hat_k=0.9).reasons == ("pass^1 not measured (minimum 90.0%)",)

    early = report_gate(run("a", True), stopped_early=True)
    assert early.gate().reasons == ("pass rate 100.0% (minimum 100.0%), stopped early",)
    assert early.gate(0.0, thresholds={"contains": 1.0}, max_errors=0).reasons == (
        "pass rate 100.0% (minimum 0.0%), stopped early",
    )


PRICES = PriceTable(
    prices={
        "gpt-test": ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0, cache_read_per_mtok=0.5)
    }
)


def usage_message(text: str, *, cache_read: int = 400) -> AIMessage:
    """A reply of 1000 prompt tokens (400 of them cached) and 200 completion tokens."""

    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_token_details": {"cache_read": cache_read},
        },
    )


def flaky_agent(fail: set[tuple[str, int]], *, model_name: str = "gpt-test") -> Agent:
    """An agent that answers `ok`, except on the n-th call (1-based) for the listed questions."""

    seen: dict[str, int] = {}

    def respond(call: ModelCall) -> AIMessage:
        question = str(call.last_message.content)
        seen[question] = seen.get(question, 0) + 1
        return usage_message("wrong" if (question, seen[question]) in fail else "ok")

    return Agent("desk", model=FunctionChatModel(respond, model_name=model_name))


def dataset(*ids: str) -> Dataset:
    return Dataset(name="cases", cases=tuple(EvalCase(id=i, input=i, expected="ok") for i in ids))


def run_result(case_id: str, passed: bool, *, repetition: int = 0, ms: int = 0) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        repetition=repetition,
        status="passed" if passed else "failed",
        duration_ms=ms,
    )


def report_of(*runs: CaseResult, repetitions: int, **kwargs: Any) -> EvalReport:
    return EvalReport(
        name="r",
        system="desk",
        dataset="cases",
        started_at=datetime(2026, 10, 5, tzinfo=UTC),
        repetitions=repetitions,
        cases=list(runs),
        **kwargs,
    )


async def test_pass_at_1_and_pass_hat_k_are_exact() -> None:
    report = await evaluate(
        flaky_agent({("B", 2)}), dataset("A", "B", "C"), [Contains()], repetitions=3, concurrency=1
    )
    assert report.pass_rate == 8 / 9  # pass@1: eight of nine runs
    assert report.pass_hat_k == 2 / 3  # pass^3: case B did not pass every time
    assert report.pass_at_k == 1.0  # every case passed at least once
    assert report.consistency() == {"A": 1.0, "B": 2 / 3, "C": 1.0}


async def test_two_repetitions_give_five_sixths_and_two_thirds() -> None:
    report = await evaluate(
        flaky_agent({("B", 2)}), dataset("A", "B", "C"), [Contains()], repetitions=2, concurrency=1
    )
    assert (report.pass_rate, report.pass_hat_k, report.pass_at_k) == (5 / 6, 2 / 3, 1.0)


async def test_a_case_that_never_passes_lowers_pass_at_k() -> None:
    report = await evaluate(
        flaky_agent({("B", 1), ("B", 2)}), dataset("A", "B"), [Contains()], repetitions=2
    )
    assert (report.pass_hat_k, report.pass_at_k) == (0.5, 0.5)


async def test_one_repetition_has_no_pass_hat_k() -> None:
    report = await evaluate(flaky_agent(set()), dataset("A", "B"), [Contains()])
    assert report.pass_hat_k is None and report.pass_at_k is None
    assert report.passed(1.0) and not report.passed(1.0, min_pass_hat_k=0.5)
    assert "| pass^k (all runs of a case pass) | n/a (needs 2 or more repetitions) |" in (
        report.to_markdown()
    )


async def test_cost_is_exact_per_run_and_in_total() -> None:
    report = await evaluate(
        flaky_agent(set()), dataset("A", "B", "C"), [Contains()], repetitions=3, prices=PRICES
    )
    # 600 plain prompt tokens * 2.0 + 400 cached * 0.5 + 200 completion tokens * 8.0, per million
    assert [run.cost for run in report.cases] == [3000 / 1_000_000] * 9
    assert report.cost == fsum([3000 / 1_000_000] * 9)
    assert report.cost_per_run == report.cost / 9  # type: ignore[operator]
    assert report.cache_hit_rate == 0.4
    assert report.cost == pytest.approx(0.027, abs=1e-12)


async def test_without_prices_there_is_no_cost() -> None:
    report = await evaluate(flaky_agent(set()), dataset("A"), [Contains()])
    assert report.cost is None and report.cost_per_run is None
    assert all(run.cost is None for run in report.cases)
    assert report.cache_hit_rate == 0.4
    assert "| cost | not computed (no prices given) |" in report.to_markdown()


async def test_a_model_without_a_price_stops_the_evaluation() -> None:
    calls: list[str] = []

    def respond(call: ModelCall) -> AIMessage:
        calls.append(str(call.last_message.content))
        return usage_message("ok")

    agent = Agent("desk", model=FunctionChatModel(respond, model_name="other-model"))
    with pytest.raises(MissingPriceError, match="other-model"):
        await evaluate(
            agent, dataset("A", "B", "C", "D"), [Contains()], concurrency=1, prices=PRICES
        )
    assert calls == ["A"]  # no new run started after the first unpriced one


def test_latency_percentiles_use_the_nearest_rank() -> None:
    twenty = report_of(
        *[run_result("A", True, repetition=i, ms=i + 1) for i in range(20)], repetitions=20
    )
    assert (twenty.latency_p50_ms, twenty.latency_p95_ms) == (10, 19)
    three = report_of(
        run_result("A", True, ms=100), run_result("B", True, ms=300), run_result("C", True, ms=200),
        repetitions=1,
    )  # fmt: skip
    assert (three.latency_p50_ms, three.latency_p95_ms) == (200, 300)
    assert report_of(repetitions=1).latency_p95_ms is None


def test_skipped_runs_are_not_counted() -> None:
    skipped = CaseResult(case_id="B", status="skipped")
    report = report_of(run_result("A", True, repetition=0, ms=50), skipped, repetitions=1)
    assert report.latency_p95_ms == 50 and report.cost is None


async def test_the_json_round_trip_keeps_every_number(tmp_path: Path) -> None:
    report = await evaluate(
        flaky_agent({("B", 2)}), dataset("A", "B", "C"), [Contains()], repetitions=3, prices=PRICES
    )
    path = report.save(tmp_path / "report.json")
    loaded = EvalReport.from_json(path)
    for name in (
        "pass_rate", "pass_at_k", "pass_hat_k", "latency_p50_ms", "latency_p95_ms",
        "cost", "cost_per_run", "cache_hit_rate",
    ):  # fmt: skip
        assert getattr(loaded, name) == getattr(report, name) is not None
    assert [run.cost for run in loaded.cases] == [run.cost for run in report.cases]
    assert loaded.consistency() == report.consistency()
    assert EvalReport.from_json(report.to_json()).to_markdown() == report.to_markdown()


def test_pass_hat_k_says_why_it_is_missing_when_no_case_ran_every_repetition() -> None:
    report = report_of(run_result("A", True), run_result("B", False), repetitions=2)

    assert report.pass_hat_k is None
    assert "| pass^k (all runs of a case pass) | n/a (no case ran every repetition) |" in (
        report.to_markdown()
    )


def test_a_case_that_never_passed_is_not_listed_as_passing_some_of_the_time() -> None:
    runs = [run_result("A", False), run_result("B", True)]
    runs += [run_result("A", False, repetition=1), run_result("B", False, repetition=1)]

    text = report_of(*runs, repetitions=2).to_markdown()

    assert "Passed only some of the time:\n- B: 1 of 2 runs" in text
    assert "- A:" not in text


async def test_the_markdown_has_a_reliability_block() -> None:
    report = await evaluate(
        flaky_agent({("B", 2)}), dataset("A", "B", "C"), [Contains()], repetitions=3, prices=PRICES
    )
    text = report.to_markdown()
    assert "## Reliability" in text
    assert "| pass@1 (one run passes) | 88.9% |" in text
    assert "| pass@3 (at least one of 3 runs passes) | 100.0% |" in text
    assert "| pass^3 (all 3 runs of a case pass) | 66.7% |" in text
    assert "| cost | 0.0270 (0.0030 per run) |" in text
    assert "| cache hit rate | 40.0% |" in text
    assert "Passed only some of the time:\n- B: 2 of 3 runs" in text
    assert text.index("## Reliability") < text.index("## Scorers")


def test_passed_can_require_pass_hat_k() -> None:
    report = report_of(
        run_result("A", True), run_result("A", True, repetition=1),
        run_result("B", True), run_result("B", False, repetition=1),
        repetitions=2,
    )  # fmt: skip
    assert report.pass_hat_k == 0.5
    assert report.passed(0.7) and report.passed(0.7, min_pass_hat_k=0.5)
    assert not report.passed(0.7, min_pass_hat_k=0.6)


def test_compare_flags_a_pass_hat_k_drop() -> None:
    good = report_of(
        *[run_result(c, True, repetition=i) for c in "AB" for i in range(3)], repetitions=3
    )
    worse = report_of(
        *[run_result(c, c != "B" or i != 1, repetition=i) for c in "AB" for i in range(3)],
        repetitions=3,
    )
    comparison = worse.compare(good)
    assert (comparison.baseline_pass_hat_k, comparison.current_pass_hat_k) == (1.0, 0.5)
    assert comparison.repetitions == 3 and comparison.pass_hat_k_dropped
    assert comparison.has_regressions
    assert "pass^3 100.0% -> 50.0% (lower)" in comparison.to_markdown()
    same = good.compare(good)
    assert not same.pass_hat_k_dropped and not same.has_regressions
    assert "pass^3 100.0% -> 100.0%\n" in same.to_markdown()


def test_compare_only_looks_at_pass_hat_k_like_for_like() -> None:
    three = report_of(*[run_result("A", True, repetition=i) for i in range(3)], repetitions=3)
    one = report_of(run_result("A", False), repetitions=1)
    assert one.compare(three).repetitions is None
    assert three.compare(one).current_pass_hat_k is None
    assert "pass^" not in one.compare(three).to_markdown()


def test_a_pass_hat_k_drop_alone_is_a_regression() -> None:
    assert Comparison(
        baseline="a", current="b", baseline_pass_rate=1.0, current_pass_rate=1.0,
        repetitions=2, baseline_pass_hat_k=1.0, current_pass_hat_k=0.5,
    ).has_regressions  # fmt: skip
    assert not Comparison(
        baseline="a", current="b", baseline_pass_rate=1.0, current_pass_rate=1.0
    ).has_regressions


def test_new_cases_do_not_count_for_the_pass_hat_k_comparison() -> None:
    before = report_of(*[run_result("A", True, repetition=i) for i in range(2)], repetitions=2)
    after = report_of(
        *[run_result("A", True, repetition=i) for i in range(2)],
        *[run_result("N", False, repetition=i) for i in range(2)],
        repetitions=2,
    )
    comparison = after.compare(before)
    assert comparison.current_pass_hat_k == 1.0 and not comparison.has_regressions


def test_usage_is_the_source_of_the_cache_hit_rate() -> None:
    report = report_of(
        run_result("A", True), repetitions=1,
        usage=Usage(requests=1, input_tokens=300, cache_read_tokens=100),
    )  # fmt: skip
    assert report.cache_hit_rate == 100 / 300
    assert report_of(run_result("A", True), repetitions=1).cache_hit_rate is None
