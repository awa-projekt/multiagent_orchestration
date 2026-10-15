"""`evaluate` and its datasets: concurrency, ordering, repetitions, isolation, error capture, traces, validation and YAML loading."""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import ValidationError

from awa import Agent, Team
from awa.contracts import (
    AgentCard,
    AgentStartedEvent,
    DelegationEvent,
    HandoffEvent,
    RunResult,
    UsageLimits,
)
from awa.errors import UsageLimitExceededError
from awa.evals import (
    Contains,
    Custom,
    Dataset,
    DelegatedTo,
    EvalCase,
    EvalError,
    ExactMatch,
    HandedOffTo,
    MaxTokens,
    RunTrace,
    Score,
    ToolWasCalled,
    evaluate,
    evaluate_sync,
    isolated_run,
)
from awa.runtime import RunContext, current_run_context, new_run_id, use_run_context
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


class Stub:
    """A minimal agent-like system whose behavior is a function of the input."""

    def __init__(
        self,
        respond: Callable[[str], Any] = lambda text: f"echo {text}",
        *,
        delay: Callable[[str], float | Awaitable[None]] = lambda text: 0.0,
        name: str = "stub",
    ) -> None:
        self._respond = respond
        self._delay = delay
        self._name = name
        self.inputs: list[Any] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def card(self) -> AgentCard:
        return AgentCard(name=self._name)

    async def run(self, input: Any, **kwargs: Any) -> RunResult[Any]:
        self.inputs.append(input)
        text = input if isinstance(input, str) else str(input)
        pause = self._delay(text)
        await (pause if inspect.isawaitable(pause) else asyncio.sleep(pause))
        return RunResult(run_id=new_run_id(), agent=self._name, output=self._respond(text))


def cases(*inputs: str, expected: Any = None) -> Dataset:
    return Dataset(
        name="smoke",
        cases=tuple(
            EvalCase(id=f"c{i}", input=text, expected=expected) for i, text in enumerate(inputs)
        ),
    )


def waiting_agent(tracker: dict[str, int], overlap: int = 1) -> Agent:
    """An agent whose tool waits until `overlap` calls run at once, then echoes the question."""

    all_in = asyncio.Event()

    @tool
    async def wait_a_moment() -> str:
        """Wait for the other runs."""
        tracker["now"] += 1
        tracker["peak"] = max(tracker["peak"], tracker["now"])
        if tracker["now"] >= overlap:
            all_in.set()
        await asyncio.wait_for(all_in.wait(), timeout=10)
        tracker["now"] -= 1
        return "waited"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            question = next(m for m in call.messages if isinstance(m, HumanMessage))
            return f"answer to {question.content}"
        return tool_call("wait_a_moment")

    return Agent("waiter", model=FunctionChatModel(respond), tools=[wait_a_moment])


# ---------------------------------------------------------------------- concurrency and order


async def test_runs_overlap_up_to_the_concurrency_limit() -> None:
    tracker = {"now": 0, "peak": 0}
    agent = waiting_agent(tracker, overlap=3)
    dataset = cases(*(f"q{i}" for i in range(6)))

    report = await evaluate(agent, dataset, [Contains("answer")], concurrency=3)

    assert tracker["peak"] == 3
    assert [r.case_id for r in report.cases] == [f"c{i}" for i in range(6)]
    assert [r.output for r in report.cases] == [f"answer to q{i}" for i in range(6)]
    assert report.pass_rate == 1.0
    assert report.system == "waiter"
    assert all(r.tool_calls == ["wait_a_moment"] for r in report.cases)


async def test_sequential_with_concurrency_one() -> None:
    tracker = {"now": 0, "peak": 0}
    await evaluate(waiting_agent(tracker), cases("a", "b", "c"), concurrency=1)
    assert tracker["peak"] == 1


async def test_results_keep_dataset_order_when_runs_finish_out_of_order() -> None:
    finished: list[str] = []
    done = {text: asyncio.Event() for text in ("slow", "medium", "fast")}
    after = {"slow": "medium", "medium": "fast"}

    async def wait_for_turn(text: str) -> None:
        if text in after:
            await done[after[text]].wait()

    def respond(text: str) -> str:
        finished.append(text)
        done[text].set()
        return text

    system = Stub(respond, delay=wait_for_turn)
    report = await evaluate(system, cases("slow", "medium", "fast"), concurrency=3)
    assert finished == ["fast", "medium", "slow"]
    assert [r.output for r in report.cases] == ["slow", "medium", "fast"]


async def test_repetitions_measure_stability() -> None:
    answers = iter(["yes", "no", "yes", "yes", "no", "no"])
    report = await evaluate(
        Stub(lambda text: next(answers)),
        cases("a", "b", expected="yes"),
        [ExactMatch()],
        repetitions=3,
        concurrency=1,
    )
    assert [(r.case_id, r.repetition) for r in report.cases] == [
        ("c0", 0),
        ("c0", 1),
        ("c0", 2),
        ("c1", 0),
        ("c1", 1),
        ("c1", 2),
    ]
    assert report.repetitions == 3
    assert report.pass_rate == 0.5
    assert [r.status for r in report.results_for("c0")] == ["passed", "failed", "passed"]
    summary = report.scorer("exact_match")
    assert summary is not None and summary.count == 6 and summary.passed == 3


# ---------------------------------------------------------------------- errors


async def test_run_errors_are_recorded_per_case_with_usage() -> None:
    def respond(call: ModelCall) -> Any:
        if "boom" in str(call.last_message.content):
            raise RuntimeError("provider exploded")
        return "fine"

    agent = Agent("worker", model=FunctionChatModel(respond))
    report = await evaluate(agent, cases("ok", "boom", "ok too"), [Contains("fine")])

    assert [r.status for r in report.cases] == ["passed", "error", "passed"]
    failed = report.cases[1]
    assert failed.error == "RuntimeError: provider exploded"
    assert failed.error_type == "RuntimeError"
    assert failed.output is None
    assert failed.trace is None
    assert failed.events and failed.events[-1].type == "error"
    assert [(s.name, s.passed, s.reason) for s in failed.scores] == [
        ("contains", False, "not scored: the run failed")
    ]
    assert report.errors == 1
    assert report.pass_rate == pytest.approx(2 / 3)
    assert not report.passed()
    assert report.passed(0.6)
    assert not report.passed(0.6, max_errors=0)


async def test_usage_limits_apply_to_each_run() -> None:
    model = FakeChatModel([tool_call("ping")], repeat_last=True)

    @tool
    def ping() -> str:
        """Ping."""
        return "pong"

    agent = Agent("pinger", model=model, tools=[ping])
    report = await evaluate(
        agent, cases("a"), usage_limits=UsageLimits(max_requests=1), concurrency=1
    )
    assert report.cases[0].status == "error"
    assert report.cases[0].error_type == "UsageLimitExceededError"
    assert report.cases[0].usage.requests == 1


async def test_timeout_cancels_slow_runs() -> None:
    never = asyncio.Event()  # the slow run waits for something that never happens
    system = Stub(delay=lambda text: never.wait() if text == "slow" else 0.0)
    # The timeout is generous for the instant run (a loaded machine cannot starve it) and
    # only the slow run waits for it.
    report = await evaluate(system, cases("slow", "fast"), timeout=1.0)
    assert report.cases[0].status == "error"
    assert report.cases[0].error == "TimeoutError: The run exceeded the timeout of 1.0 s"
    assert report.cases[1].status == "passed"


async def test_fail_fast_skips_the_remaining_runs() -> None:
    report = await evaluate(
        Stub(lambda text: "wrong"),
        cases("a", "b", "c", expected="right"),
        [ExactMatch()],
        concurrency=1,
        fail_fast=True,
    )
    assert [r.status for r in report.cases] == ["failed", "skipped", "skipped"]
    assert report.stopped_early
    assert len(report.runs) == 1
    assert not report.passed(0.0)
    assert "Stopped early" in report.to_markdown()


async def test_fail_fast_without_failures_runs_everything() -> None:
    report = await evaluate(Stub(), cases("a", "b"), fail_fast=True)
    assert not report.stopped_early and report.passed()


async def test_scorer_failures_fail_the_score_not_the_evaluation() -> None:
    def broken(case: EvalCase, trace: RunTrace) -> bool:
        raise KeyError("missing metadata")

    class WrongType:
        name = "wrong_type"

        async def __call__(self, case: EvalCase, trace: RunTrace) -> Any:
            return True

    report = await evaluate(Stub(), cases("a"), [Custom(broken), WrongType(), Contains("echo")])
    result = report.cases[0]
    assert result.status == "failed"
    broken_score = result.score("broken")
    assert broken_score is not None and broken_score.error == "KeyError: 'missing metadata'"
    wrong = result.score("wrong_type")
    assert wrong is not None and wrong.error is not None and "expected a Score" in wrong.error
    assert result.score("contains") == Score(
        name="contains", value=1.0, passed=True, reason="found 1 of 1 phrase(s)"
    )
    summary = report.scorer("broken")
    assert summary is not None and summary.errors == 1


# ---------------------------------------------------------------------- factories and systems


async def test_a_factory_builds_a_fresh_system_for_every_run() -> None:
    built: list[Stub] = []

    def factory() -> Stub:
        system = Stub(name="fresh")
        built.append(system)
        return system

    report = await evaluate(factory, cases("a", "b"), repetitions=2)
    assert len(built) == 4
    assert all(len(system.inputs) == 1 for system in built)
    assert report.system == "fresh"

    async def async_factory() -> Stub:
        return Stub(name="async_built")

    assert (await evaluate(async_factory, cases("a"))).system == "async_built"


async def test_factory_failures_are_case_errors() -> None:
    def broken() -> Stub:
        raise RuntimeError("cannot build")

    report = await evaluate(broken, cases("a"))
    assert report.cases[0].status == "error"
    assert report.cases[0].error == "RuntimeError: cannot build"

    report = await evaluate(lambda: "not a system", cases("a"))  # type: ignore[arg-type,return-value]
    assert report.cases[0].error_type == "TypeError"


async def test_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await evaluate(Stub(), cases("a"), concurrency=0)
    with pytest.raises(ValueError, match="repetitions"):
        await evaluate(Stub(), cases("a"), repetitions=0)
    with pytest.raises(ValueError, match="timeout"):
        await evaluate(Stub(), cases("a"), timeout=0)
    with pytest.raises(TypeError, match="Not an agent-like system"):
        await evaluate(42, cases("a"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="wrap plain functions in Custom"):
        await evaluate(Stub(), cases("a"), [lambda c, t: True])  # type: ignore[list-item]


async def test_cases_with_the_same_id_are_refused() -> None:
    twins = [EvalCase(id="a", input="x", scorers=("contains:x",)), EvalCase(id="a", input="y")]
    with pytest.raises(EvalError, match="Case ids must be unique; repeated: a"):
        await evaluate(Stub(), twins)


async def test_scorer_specs_and_name_clashes() -> None:
    dataset = Dataset(
        cases=(
            EvalCase(id="a", input="x", scorers=("contains:echo", "max_tokens:10")),
            EvalCase(id="b", input="y", scorers=("contains:nope",)),
        )
    )
    report = await evaluate(Stub(), dataset, ["max_tokens:10"])
    assert [s.name for s in report.cases[0].scores] == ["max_tokens:10", "contains:echo"]
    assert [s.name for s in report.cases[1].scores] == ["max_tokens:10", "contains:nope"]
    assert [r.status for r in report.cases] == ["passed", "failed"]

    with pytest.raises(EvalError, match="Scorer names must be unique; repeated: contains"):
        await evaluate(Stub(), dataset, [Contains(), Contains("x")])
    clash = Dataset(cases=(EvalCase(id="a", input="x", scorers=("contains",)),))
    with pytest.raises(EvalError, match="Case 'a': scorer 'contains' clashes"):
        await evaluate(Stub(), clash, [Contains("x")])
    bad = Dataset(cases=(EvalCase(id="z", input="x", scorers=("bogus",)),))
    with pytest.raises(EvalError, match="Case 'z': Unknown scorer spec 'bogus'"):
        await evaluate(Stub(), bad)


async def test_message_inputs_and_deps_reach_the_system() -> None:
    @tool
    def whoami() -> str:
        """Return the current user."""
        ctx = current_run_context()
        assert ctx is not None
        return str(ctx.deps["user"])

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            question = next(m for m in call.messages if isinstance(m, HumanMessage))
            return f"{question.content} -> {call.last_message.content}"
        return tool_call("whoami")

    agent = Agent("helper", model=FunctionChatModel(respond), tools=[whoami])
    dataset = Dataset(cases=(EvalCase(id="m", input=[{"role": "user", "content": "hi"}]),))
    report = await evaluate(agent, dataset, deps={"user": "ada"})
    assert report.cases[0].output == "hi -> ada"


# ---------------------------------------------------------------------- isolation and usage


async def test_runs_never_join_an_enclosing_run() -> None:
    outer: RunContext[None] = RunContext(run_id=new_run_id(), agent="outer", deps=None)
    agent = Agent("solo", model=FakeChatModel(["hello"], repeat_last=True))
    with use_run_context(outer):
        report = await evaluate(agent, cases("a", "b"))
    assert outer.usage.requests == 0
    assert report.usage.requests == 2
    assert [r.usage.requests for r in report.cases] == [1, 1]
    assert report.usage.total_tokens == sum(r.usage.total_tokens for r in report.cases)


async def test_isolated_run_records_events_like_a_direct_run() -> None:
    agent = Agent("solo", model=FakeChatModel(["hello"]))
    run = await isolated_run(agent, "hi")
    assert run.error is None and run.result is not None
    first = run.events[0]
    assert isinstance(first, AgentStartedEvent)
    assert first.depth == 0 and first.parent_run_id is None
    assert first.run_id == first.root_run_id == run.result.run_id
    assert {event.root_run_id for event in run.events} == {first.run_id}
    assert run.usage.requests == 1 and run.duration_ms >= 0


async def test_isolated_run_counts_max_depth_like_a_direct_run() -> None:
    helper = Agent("helper", model=FakeChatModel(["helped"]))
    lead = Agent(
        "lead",
        model=FakeChatModel([tool_call("helper", {"task": "x"}), "done"]),
        tools=[helper.as_tool(name="helper")],
    )

    run = await isolated_run(lead, "go", usage_limits=UsageLimits(max_depth=1))

    assert run.error is None and run.result is not None
    assert [m.content for m in run.result.messages if isinstance(m, ToolMessage)] == ["helped"]


def test_evaluate_sync() -> None:
    report = evaluate_sync(Stub(), cases("a"), ["contains:echo"])
    assert report.passed()


async def test_evaluate_sync_refuses_a_running_loop() -> None:
    with pytest.raises(RuntimeError, match="running event loop"):
        evaluate_sync(Stub(), cases("a"))


# ---------------------------------------------------------------------- trace scorers on a team


def support_team() -> Team:
    """Triage delegates billing questions and hands refunds off."""

    def triage(call: ModelCall) -> Any:
        last = call.last_message
        if isinstance(last, ToolMessage):
            return f"Billing says: {last.content}"
        text = str(last.content)
        if "refund" in text:
            return tool_call("transfer_to_refunds", {"reason": "refund request"})
        return tool_call("delegate_task", {"agent": "billing", "task": text})

    billing = Agent(
        "billing", description="Invoices.", model=FunctionChatModel(lambda c: "INV-7 is paid.")
    )
    refunds = Agent(
        "refunds", description="Refunds.", model=FunctionChatModel(lambda c: "Refund started.")
    )
    lead = Agent("triage", description="Front desk.", model=FunctionChatModel(triage))
    return Team(
        "desk", orchestrator=lead, members=[billing, refunds], handoffs={"triage": ["refunds"]}
    )


async def test_trace_scorers_on_a_real_team_run() -> None:
    dataset = Dataset.from_yaml(
        """
        cases:
          - id: invoice
            input: Is invoice INV-7 paid?
            expected: paid
            scorers: ["delegated:billing", "no_tool:transfer_to_refunds"]
          - id: refund
            input: I want a refund for order 1001.
            expected: Refund started
            scorers: ["handoff:refunds"]
        """
    )
    report = await evaluate(
        support_team,
        dataset,
        [Contains(), MaxTokens(5_000), ToolWasCalled("delegate_task", max_times=1, min_times=0)],
    )
    assert report.pass_rate == 1.0, report.to_markdown()
    invoice, refund = report.cases
    assert invoice.trace is not None and refund.trace is not None
    assert [e.target for e in invoice.trace.of_type(DelegationEvent)] == ["billing"]
    assert {e.agent for e in invoice.events} >= {"desk", "triage", "billing"}
    assert [e.target for e in refund.trace.of_type(HandoffEvent)] == ["refunds"]
    assert refund.handoff_path == ("triage", "refunds") and refund.agent == "refunds"

    # The same scorers catch a system that routes wrongly.
    wrong = await evaluate(
        support_team,
        dataset.filter(ids=["invoice"]),
        [DelegatedTo("refunds"), HandedOffTo("refunds")],
    )
    assert [s.passed for s in wrong.cases[0].scores] == [False, False, True, True]


async def test_usage_pool_bounds_all_concurrent_runs_and_judges_together() -> None:
    from awa.contracts.usage import Usage
    from awa.evals import ModelJudge

    fake = FakeChatModel(["answer"], repeat_last=True)
    judge_model = FakeChatModel(['{"score": 1.0, "reasoning": "ok"}'], repeat_last=True)
    judge = ModelJudge("fine", model=judge_model)
    pool = Usage()

    report = await evaluate(
        lambda: Agent("worker", model=fake),
        [EvalCase(id=f"c{i}", input="go") for i in range(6)],
        [judge],
        concurrency=6,
        usage_limits=UsageLimits(max_requests=7),
        usage_pool=pool,
    )

    made = fake.call_count + judge_model.call_count
    assert made == pool.requests == 7
    assert report.usage.requests + report.judge_usage.requests == 7
    assert pool.by_agent.keys() >= {"worker"}
    assert any(result.status != "passed" for result in report.cases)


def test_a_pooled_usage_counts_into_the_pool_and_snapshots_without_it() -> None:
    from awa.contracts.usage import Usage

    pool = Usage()
    mine = Usage.under(pool)
    mine.request_started()
    assert pool.requests_started == mine.requests_started == 1
    mine.request_ended()
    mine.record_model_call("a", input_tokens=3, output_tokens=2)

    assert (mine.requests, pool.requests, pool.total_tokens) == (1, 1, 5)
    copy = mine.snapshot()
    copy.record_model_call("a")
    assert pool.requests == 1 and mine.pool is pool
    mine.record_model_call("a")  # the pool holds 2 now, one more than the limit
    with pytest.raises(UsageLimitExceededError, match="requests=2 > max_requests=1"):
        UsageLimits(max_requests=1).check(mine)


# ------------------------------------------------------------------------------ setup files

_ANNOTATED_SETUP = """from __future__ import annotations

from dataclasses import dataclass

CALLS = []
LOADS = 0
LOADS += 1


@dataclass
class Note:
    text: str


def seed(folder, case) -> None:
    CALLS.append(case.id)
    (folder / "note.txt").write_text(Note(case.id).text, encoding="utf-8")
"""


async def test_setup_file_with_future_annotations_and_dataclass_loads(tmp_path: Path) -> None:
    (tmp_path / "fixtures.py").write_text(_ANNOTATED_SETUP, encoding="utf-8")
    case = EvalCase(id="a", input="x", setup=f"{tmp_path / 'fixtures.py'}:seed")
    [result] = (await evaluate(Stub(), [case])).cases
    assert result.status != "error", result.error


@pytest.fixture(autouse=True)
def _no_setup_modules() -> Any:
    def clear() -> None:
        for name in [n for n in sys.modules if n.startswith("awa_eval_setup_")]:
            del sys.modules[name]

    clear()
    yield
    clear()


async def test_setup_module_runs_once_per_evaluation(tmp_path: Path) -> None:
    (tmp_path / "fixtures.py").write_text(_ANNOTATED_SETUP, encoding="utf-8")
    spec = f"{tmp_path / 'fixtures.py'}:seed"
    cases = tuple(EvalCase(id=f"c{i}", input="x", setup=spec) for i in range(3))
    report = await evaluate(Stub(), Dataset(name="s", cases=cases))
    assert all(case.status != "error" for case in report.cases)
    [module] = [m for n, m in sys.modules.items() if n.startswith("awa_eval_setup_")]
    assert module.LOADS == 1 and sorted(module.CALLS) == ["c0", "c1", "c2"]


async def test_failed_setup_leaves_no_module_in_sys_modules(tmp_path: Path) -> None:
    (tmp_path / "fixtures.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    case = EvalCase(id="a", input="x", setup=f"{tmp_path / 'fixtures.py'}:seed")
    [result] = (await evaluate(Stub(), [case])).cases
    assert result.status == "error" and "boom" in (result.error or "")
    assert not [name for name in sys.modules if name.startswith("awa_eval_setup_")]


async def test_a_judge_limit_is_honoured_inside_a_usage_pool() -> None:
    from awa.contracts.usage import Usage
    from awa.evals import JudgeVerdict, ModelJudge
    from awa.testing import structured_output

    reply = structured_output(JudgeVerdict, {"score": 1.0, "reasoning": "ok"})
    judge_model = FunctionChatModel(lambda call: reply)
    judge = ModelJudge("fine", model=judge_model, usage_limits=UsageLimits(max_requests=0))

    report = await evaluate(
        lambda: Agent("worker", model=FakeChatModel(["a"], repeat_last=True)),
        [EvalCase(id=f"c{i}", input="go") for i in range(3)],
        [judge],
        usage_limits=UsageLimits(max_requests=100),
        usage_pool=Usage(),
    )

    scores = [case.score("judge") for case in report.cases]
    assert all(s is not None and s.error and "max_requests=0" in s.error for s in scores)
    assert judge_model.call_count == 0  # the judge's own limit stopped it, not the pool's
    assert report.usage.requests == 3  # the runs themselves are unaffected


YAML = """
name: support
cases:
  - id: order-status
    input: Where is order 1002?
    expected: shipped
    tags: [orders]
    scorers: [contains, "delegated:order_specialist"]
  - input:
      - {role: user, content: "Invoice INV-1002?"}
    expected: {paid: false}
    tags: [billing]
    metadata: {priority: high}
"""


def test_case_validation() -> None:
    case = EvalCase(id="a", input="hello", tags=("x",))
    assert case.expected is None and case.metadata == {} and case.scorers == ()
    with pytest.raises(ValidationError):
        EvalCase(id="", input="hello")
    with pytest.raises(ValidationError):
        EvalCase(id="a", input="   ")
    with pytest.raises(ValidationError):
        EvalCase(id="a", input=[])
    with pytest.raises(ValidationError):
        EvalCase(id="a", input=[{"text": "no role"}])
    with pytest.raises(ValidationError):
        EvalCase(id="a", input="hi", unknown=1)  # type: ignore[call-arg]


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Duplicate case ids: a"):
        Dataset(cases=(EvalCase(id="a", input="x"), EvalCase(id="a", input="y")))
    with pytest.raises(EvalError, match="Duplicate case ids"):
        Dataset.from_records([{"id": "a", "input": "x"}, {"id": "a", "input": "y"}])


def test_from_yaml_text_and_file(tmp_path: Path) -> None:
    dataset = Dataset.from_yaml(YAML)
    assert dataset.name == "support"
    assert [case.id for case in dataset] == ["order-status", "case-2"]
    assert dataset["case-2"].input == [{"role": "user", "content": "Invoice INV-1002?"}]
    assert dataset["case-2"].expected == {"paid": False}
    assert dataset["order-status"].scorers == ("contains", "delegated:order_specialist")
    assert dataset.tags == ["billing", "orders"]
    assert len(dataset) == 2

    file = tmp_path / "cases.yml"
    file.write_text("- {id: one, input: hi}\n", encoding="utf-8")
    loaded = Dataset.from_yaml(str(file))
    assert loaded.name == "cases" and loaded["one"].input == "hi"
    assert Dataset.load(file, name="renamed").name == "renamed"
    with pytest.raises(KeyError):
        loaded["missing"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("cases: [1]", "Case 1 must be a mapping"),
        ("{cases: [{id: a}]}", "Invalid case 'a': input"),
        ("just text", "must be a list of cases"),
        ("{name: x, extra: 1}", "Unknown dataset keys: extra"),
        ("a: [b", "Malformed dataset YAML"),
    ],
)
def test_invalid_yaml_is_a_clean_error(text: str, message: str) -> None:
    with pytest.raises(EvalError, match=message):
        Dataset.from_yaml(text)


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="Cannot read dataset file"):
        Dataset.load(tmp_path / "missing.yaml")


def test_filter_by_tags_and_ids() -> None:
    dataset = Dataset.from_yaml(YAML)
    assert [c.id for c in dataset.filter("billing")] == ["case-2"]
    assert [c.id for c in dataset.filter("billing", "orders")] == ["order-status", "case-2"]
    assert [c.id for c in dataset.filter(ids=["order-status"])] == ["order-status"]
    assert [c.id for c in dataset.filter("billing", ids=["order-status"])] == []
    assert dataset.filter().cases == dataset.cases
    assert dataset.filter("billing").name == "support"


def test_numeric_id_and_scalar_tags_load_from_yaml() -> None:
    dataset = Dataset.from_yaml("cases: [{id: 7, input: hi, tags: smoke}]")
    [case] = dataset.cases
    assert case.id == "7" and case.tags == ("smoke",)
