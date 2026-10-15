"""`evaluate`: run a system on every case of a dataset and score each run.

Each run is isolated in its own run tree (own events, limits, usage and duration). A case with
`decisions` is resumed on its thread with each in turn, within the same run tree. With a
`sandbox` or a `setup`, every run gets its own temporary folder as `awa.runtime.case_dir()`.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import importlib
import importlib.util
import inspect
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias

from awa._internal.upstream.models import BaseChatModel
from awa._internal.util import elapsed_ms
from awa.contracts.events import AgentStartedEvent, ApprovalDecidedEvent, RunEvent, ToolCallEvent
from awa.contracts.protocols import AgentInput, AgentLike
from awa.contracts.results import RunResult
from awa.contracts.usage import PriceTable, Usage, UsageLimits
from awa.errors import ConfigurationError, EvalError, MissingPriceError, UsageLimitExceededError
from awa.evals.dataset import Dataset, EvalCase, split_setup
from awa.evals.judge import ModelJudge
from awa.evals.report import CaseCaching, CaseResult, EvalReport
from awa.evals.scorers import Score, Scorer, failed_score
from awa.evals.specs import parse_scorer
from awa.evals.trace import RunTrace, output_data, requested_approvals
from awa.runtime._run import run_blocking
from awa.runtime.case import use_case_dir
from awa.runtime.context import RunContext, new_run_id, use_run_context
from awa.runtime.loops import DelegationBudget

MAX_RESUMES = 50
"""Resumes one evaluated run may get from its decisions (a `repeat` decision is never used up)."""

_NOT_COPIED = ("__pycache__", "node_modules")

SystemFactory: TypeAlias = Callable[[], AgentLike | Awaitable[AgentLike]]
"""Builds a fresh system (sync or async) for every run."""

_pool: contextvars.ContextVar[tuple[Usage, UsageLimits | None] | None] = contextvars.ContextVar(
    "awa_eval_usage_pool", default=None
)


@dataclass
class IsolatedRun:
    """The outcome of `isolated_run`.

    Attributes:
        result: The final result, or `None` if the run (or a resume) raised.
        usage: Usage of the run tree (also what was used before a failure).
        segments: The result of the first run and of every resume (also before a failure).
        decisions: The applied decisions, in order (a repeating one once per request).
        unused_decisions: Decisions the run never asked for.
    """

    result: RunResult[Any] | None
    events: list[RunEvent] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    error: Exception | None = None
    segments: list[RunResult[Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    unused_decisions: int = 0


async def isolated_run(
    system: AgentLike,
    input: AgentInput,
    *,
    usage_limits: UsageLimits | None = None,
    deps: Any = None,
    timeout: float | None = None,
    decisions: Sequence[Mapping[str, Any]] = (),
) -> IsolatedRun:
    """Run `system` once in a run tree of its own and record everything it emits.

    Exceptions are returned, not raised; the events look like a direct run's. A pause is resumed on
    its thread with the next fitting decision (by interrupt id when several are pending; a `tool`
    decision answers that tool's request) until it finishes or no decision fits.

    Args:
        usage_limits: Limits for this run tree (all segments together).
        timeout: Seconds before the run (all segments) is cancelled as a `TimeoutError`.
        decisions: Human decisions in the format `Agent.resume` takes.
    """

    pooled = _pool.get()
    run_id = new_run_id()
    budgets: tuple[DelegationBudget, ...] = ()
    if pooled is not None:  # inside `evaluate(usage_pool=...)`: judges and runs share its limits
        usage = Usage.under(pooled[0])
        limits = pooled[1]
        if usage_limits is not None and usage_limits is not limits:
            # Limits of the call itself (a judge's) bound only this run, not the whole pool.
            budgets = (DelegationBudget(run_id, usage, usage_limits),)
    else:  # one case is one run tree: it gets the ceilings every run tree has
        from awa.runtime.termination import with_ceilings

        usage = Usage()
        limits = with_ceilings(usage_limits, timeout=timeout)
    # The case's run sits at depth 0, as when it runs directly.
    root: RunContext[Any] = RunContext(
        run_id=run_id,
        agent="eval",
        deps=deps,
        usage=usage,
        limits=limits,
        budgets=budgets,
        depth=-1,
    )
    events: list[RunEvent] = []
    root.events.subscribe(_Recorder(root.run_id, events))
    started = time.perf_counter()
    result: RunResult[Any] | None = None
    error: Exception | None = None
    segments: list[RunResult[Any]] = []
    applied: list[dict[str, Any]] = []
    queue = [dict(decision) for decision in decisions]
    try:
        with use_run_context(root):
            async with asyncio.timeout(timeout):
                result = await _run_segments(system, input, deps, queue, segments, applied)
    except TimeoutError as exc:
        error = exc if str(exc) else TimeoutError(f"The run exceeded the timeout of {timeout} s")
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the case
        error = exc
    return IsolatedRun(
        result=result,
        events=events,
        usage=root.usage.snapshot(),
        duration_ms=elapsed_ms(started),
        error=error,
        segments=segments,
        decisions=applied,
        unused_decisions=_unused(queue, applied) if result is not None else 0,
    )


async def _run_segments(
    system: AgentLike,
    input: AgentInput,
    deps: Any,
    queue: list[dict[str, Any]],
    segments: list[RunResult[Any]],
    applied: list[dict[str, Any]],
) -> RunResult[Any]:
    """Run and resume `system` with fitting decisions until it ends, pauses unanswered or hits `MAX_RESUMES`."""

    current = await system.run(input, deps=deps)
    segments.append(current)
    resumes = 0
    while current.is_interrupted and queue:
        resumes += 1
        if resumes > MAX_RESUMES:  # a `repeat` decision is never used up
            raise UsageLimitExceededError(
                f"Usage limit exceeded: resumes={resumes} > max_resumes={MAX_RESUMES}"
                " (the run kept asking for approval)",
                details={"limit": "max_resumes", "value": resumes},
            )
        answer, used = _answer(current.interrupts, queue)
        if not used:
            break
        current = await _resume(system, current, answer, deps)
        applied.extend(used)
        segments.append(current)
    return current


def _unused(queue: list[dict[str, Any]], applied: list[dict[str, Any]]) -> int:
    """Decisions left on `queue` that never answered a request (a repeating one stays
    on it after answering)."""

    return sum(
        1
        for decision in queue
        if not (decision.get("repeat") and any(used is decision for used in applied))
    )


def _answer(
    interrupts: Sequence[Mapping[str, Any]], queue: list[dict[str, Any]]
) -> tuple[Any, list[dict[str, Any]]]:
    """The resume value for the pending `interrupts` and the decisions it took off `queue`."""

    ids = [str(item["id"]) for item in interrupts if item.get("id")]
    if len(interrupts) <= 1 or len(ids) < len(interrupts):
        decision = _take(queue, _requested_tool(interrupts[0]) if interrupts else None)
        return (None, []) if decision is None else (_resume_value(decision), [decision])
    answers: dict[str, Any] = {}
    used: list[dict[str, Any]] = []
    for item in interrupts:
        decision = _take(queue, _requested_tool(item))
        if decision is not None:
            answers[str(item["id"])] = _resume_value(decision)
            used.append(decision)
    return answers, used


def _take(queue: list[dict[str, Any]], tool: str | None) -> dict[str, Any] | None:
    """The decision for a request of `tool`: the first one naming that tool, then its
    repeating one (which stays on `queue`), else the first one naming no tool."""

    if tool is not None:
        for index, decision in enumerate(queue):
            if decision.get("tool") == tool and not decision.get("repeat"):
                return queue.pop(index)
        for decision in queue:
            if decision.get("tool") == tool:
                return decision
    for index, decision in enumerate(queue):
        if "tool" not in decision:
            return queue.pop(index)
    return None


def _requested_tool(interrupt: Mapping[str, Any]) -> str | None:
    value = interrupt.get("value")
    tool = value.get("tool") if isinstance(value, Mapping) else None
    return tool if isinstance(tool, str) else None


def _resume_value(decision: Mapping[str, Any]) -> dict[str, Any]:
    """The decision as `resume` takes it: without the eval-only `tool` and `repeat` keys."""

    return {key: value for key, value in decision.items() if key not in ("tool", "repeat")}


async def _resume(
    system: AgentLike, paused: RunResult[Any], answer: Any, deps: Any
) -> RunResult[Any]:
    resume = getattr(system, "resume", None)
    if not callable(resume):
        raise EvalError(
            f"System {system.name!r} paused for a decision but has no resume(); "
            "a case with decisions needs a resumable system (an awa agent, team, "
            "workflow or network)"
        )
    if paused.thread_id is None:
        raise EvalError(
            f"System {system.name!r} paused without a thread to resume; give it a checkpointer"
        )
    options: dict[str, Any] = {} if deps is None else {"deps": deps}
    resumed = await resume(paused.thread_id, answer, **options)
    if not isinstance(resumed, RunResult):
        raise EvalError(f"resume() of {system.name!r} returned {type(resumed).__name__}")
    return resumed


class _Recorder:
    """Records events relative to the system's run: the evaluation's own root
    context is removed from depth, root run id and parent run id."""

    def __init__(self, root_id: str, events: list[RunEvent]) -> None:
        self._root_id = root_id
        self._system_run_id: str | None = None
        self._events = events

    def __call__(self, event: RunEvent) -> None:
        update: dict[str, Any] = {"depth": max(0, event.depth - 1)}
        if isinstance(event, AgentStartedEvent) and event.parent_run_id == self._root_id:
            self._system_run_id = self._system_run_id or event.run_id
            update["parent_run_id"] = None
        if self._system_run_id is not None:
            update["root_run_id"] = self._system_run_id
        self._events.append(event.model_copy(update=update))


async def evaluate(
    system: AgentLike | SystemFactory,
    dataset: Dataset | Iterable[EvalCase],
    scorers: Sequence[Scorer | str] = (),
    *,
    concurrency: int = 4,
    repetitions: int = 1,
    usage_limits: UsageLimits | None = None,
    fail_fast: bool = False,
    timeout: float | None = None,
    deps: Any = None,
    name: str | None = None,
    judge_model: str | BaseChatModel | None = None,
    usage_pool: Usage | None = None,
    prices: PriceTable | None = None,
    sandbox: str | Path | None = None,
    sandbox_folders: Sequence[str] | None = None,
) -> EvalReport:
    """Run `system` on every case, score every run, and return the report.

    Runs execute concurrently; results keep dataset order. A raising run is an `"error"` result
    (with its usage) failing every scorer, and a raising scorer a failed score with `error`.

    Args:
        system: The system, or a factory building a fresh one per run (recommended when it keeps
            state across runs); a shared system must support concurrent runs.
        scorers: Scorer objects or specs (see `parse_scorer`); each case's own specs are added.
        concurrency: Maximum number of runs in flight.
        repetitions: Runs per case, to measure how stable the system is.
        usage_limits: Limits for each run tree (not for the whole evaluation).
        fail_fast: Stop starting runs after the first failed run; the rest are `"skipped"`.
        timeout: Seconds per run before it is cancelled and recorded as an error.
        judge_model: Model for `judge:` specs.
        usage_pool: A `Usage` every run and judge also counts into; `usage_limits` then bound the
            whole pool.
        prices: What the models cost (awa ships no prices); judges are not priced.
        sandbox: A folder whose subfolders every run gets a fresh copy of as
            `awa.runtime.case_dir()`.
        sandbox_folders: Copy only these subfolders of `sandbox`; `None` copies all.

    Raises:
        ConfigurationError: If `concurrency`, `repetitions` or `timeout` is not positive.
        EvalError: If scorer names clash, a spec is invalid or the `sandbox` folder is missing.
        MissingPriceError: If a run used a model without a price; no new run starts after it.

    Example:
        ```python
        report = await evaluate(
            build_support_desk,              # a fresh team for every case
            Dataset.from_yaml("cases.yaml"),
            [Contains(), MaxTokens(20_000)],
            concurrency=8,
        )
        ```
    """

    if concurrency < 1:
        raise ConfigurationError(f"concurrency must be at least 1, got {concurrency!r}")
    if repetitions < 1:
        raise ConfigurationError(f"repetitions must be at least 1, got {repetitions!r}")
    if timeout is not None and timeout <= 0:
        raise ConfigurationError(f"timeout must be positive, got {timeout!r}")
    if sandbox is not None and not Path(sandbox).is_dir():
        raise EvalError(f"The sandbox folder {str(sandbox)!r} does not exist")
    cases = dataset.cases if isinstance(dataset, Dataset) else tuple(dataset)
    dataset_name = dataset.name if isinstance(dataset, Dataset) else "dataset"
    shared = [_to_scorer(s, judge_model) for s in scorers]
    plan = _scorer_plan(cases, shared, scorers, judge_model)

    runner = _Runner(
        system,
        usage_limits=usage_limits,
        timeout=timeout,
        deps=deps,
        sandbox=Path(sandbox) if sandbox is not None else None,
        sandbox_folders=tuple(sandbox_folders) if sandbox_folders is not None else None,
        setup=dataset.setup if isinstance(dataset, Dataset) else None,
    )
    jobs = [(case, repetition) for case in cases for repetition in range(repetitions)]
    results: list[CaseResult | None] = [None] * len(jobs)
    gate = asyncio.Semaphore(concurrency)
    stop = asyncio.Event()
    unpriced: list[MissingPriceError] = []
    started_at = datetime.now(UTC)
    started = time.perf_counter()

    async def work(index: int, case: EvalCase, repetition: int) -> None:
        async with gate:
            if stop.is_set():
                results[index] = CaseResult(
                    case_id=case.id, repetition=repetition, status="skipped", tags=case.tags
                )
                return
            outcome = await runner.run_case(case, repetition, plan[case.id])
            results[index] = outcome
            if prices is not None:
                try:
                    outcome.cost = prices.cost(outcome.usage)
                except MissingPriceError as exc:  # a config error: stop spending, report it
                    unpriced.append(exc)
                    stop.set()
            if fail_fast and outcome.status in ("failed", "error"):
                stop.set()

    token = _pool.set(None if usage_pool is None else (usage_pool, usage_limits))
    try:  # the tasks made by gather inherit the pool
        await asyncio.gather(*(work(i, case, rep) for i, (case, rep) in enumerate(jobs)))
    finally:
        _pool.reset(token)
    if unpriced:
        raise unpriced[0]
    finished = [result for result in results if result is not None]
    total, judges = Usage(), Usage()
    for result in finished:
        total.merge(result.usage)
        judges.merge(result.judge_usage)
    return EvalReport(
        name=name or dataset_name,
        system=runner.system_name,
        dataset=dataset_name,
        started_at=started_at,
        duration_ms=elapsed_ms(started),
        repetitions=repetitions,
        cases=finished,
        usage=total,
        judge_usage=judges,
        stopped_early=stop.is_set() and any(r.status == "skipped" for r in finished),
    )


def evaluate_sync(
    system: AgentLike | SystemFactory,
    dataset: Dataset | Iterable[EvalCase],
    scorers: Sequence[Scorer | str] = (),
    **options: Any,
) -> EvalReport:
    """Run `evaluate` from synchronous code; refused inside an event loop."""

    return run_blocking(
        lambda: evaluate(system, dataset, scorers, **options),
        "evaluate_sync()",
        "await evaluate(...)",
    )


def _to_scorer(scorer: Scorer | str, judge_model: str | BaseChatModel | None) -> Scorer:
    if isinstance(scorer, str):
        return parse_scorer(scorer, judge_model=judge_model)
    if not isinstance(scorer, Scorer):
        raise TypeError(f"Not a scorer: {scorer!r}; wrap plain functions in Custom(...)")
    return scorer


def _scorer_plan(
    cases: Sequence[EvalCase],
    shared: list[Scorer],
    given: Sequence[Scorer | str],
    judge_model: str | BaseChatModel | None,
) -> dict[str, list[Scorer]]:
    ids = [case.id for case in cases]
    repeated = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if repeated:
        raise EvalError(f"Case ids must be unique; repeated: {', '.join(repeated)}")
    names = [scorer.name for scorer in shared]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise EvalError(f"Scorer names must be unique; repeated: {', '.join(duplicates)}")
    specs = {
        s.strip(): scorer.name
        for s, scorer in zip(given, shared, strict=True)
        if isinstance(s, str)
    }
    by_spec: dict[str, Scorer] = {}
    plan: dict[str, list[Scorer]] = {}
    for case in cases:
        extra: list[Scorer] = []
        for spec in case.scorers:
            key = spec.strip()
            if key in specs:
                continue  # the same spec is already applied to every case
            try:
                scorer = by_spec.get(key) or parse_scorer(key, judge_model=judge_model)
            except EvalError as exc:
                raise EvalError(f"Case {case.id!r}: {exc}") from None
            by_spec[key] = scorer
            if scorer.name in names or any(s.name == scorer.name for s in extra):
                raise EvalError(
                    f"Case {case.id!r}: scorer {scorer.name!r} clashes with another scorer "
                    "of the same name"
                )
            extra.append(scorer)
        plan[case.id] = [*shared, *extra]
    return plan


class _Runner:
    def __init__(
        self,
        system: AgentLike | SystemFactory,
        *,
        usage_limits: UsageLimits | None,
        timeout: float | None,
        deps: Any,
        sandbox: Path | None = None,
        sandbox_folders: tuple[str, ...] | None = None,
        setup: str | None = None,
    ) -> None:
        if isinstance(system, AgentLike):
            self._shared: AgentLike | None = system
            self._factory: SystemFactory | None = None
            self.system_name = system.name
        elif callable(system):
            self._shared = None
            self._factory = system
            self.system_name = getattr(system, "__name__", "system")
        else:
            raise TypeError(f"Not an agent-like system or a factory: {system!r}")
        self._limits = usage_limits
        self._timeout = timeout
        self._deps = deps
        self._named = self._factory is None
        self._sandbox = sandbox
        self._folders = sandbox_folders
        self._setup = setup
        self._setups: dict[str, Callable[..., Any]] = {}

    async def _system(self) -> AgentLike:
        if self._shared is not None:
            return self._shared
        assert self._factory is not None
        built = self._factory()
        if inspect.isawaitable(built):
            built = await built
        if not isinstance(built, AgentLike):
            raise TypeError(f"The system factory returned {type(built).__name__}, not a system")
        if not self._named:
            self.system_name, self._named = built.name, True
        return built

    async def _run_setup(self, spec: str, folder: Path, case: EvalCase) -> None:
        """Call the setup function (loaded once per runner) with the case's folder and the case."""

        function = self._setups.get(spec)
        if function is None:
            function = self._setups[spec] = _setup_function(spec)
        outcome = function(folder, case)
        if inspect.isawaitable(outcome):
            await outcome

    async def run_case(self, case: EvalCase, repetition: int, scorers: list[Scorer]) -> CaseResult:
        """Run and score one case, in a folder of its own when there is a sandbox or a setup."""

        if self._sandbox is None and self._setup is None and case.setup is None:
            return await self._run_case(case, repetition, scorers, None)
        folder = await asyncio.to_thread(_case_folder, self._sandbox, self._folders)
        try:
            with use_case_dir(folder):
                return await self._run_case(case, repetition, scorers, folder)
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)

    async def _run_case(
        self, case: EvalCase, repetition: int, scorers: list[Scorer], folder: Path | None
    ) -> CaseResult:
        started = time.perf_counter()
        try:
            for spec in (self._setup, case.setup):
                if spec is not None and folder is not None:
                    await self._run_setup(spec, folder, case)
            system = await self._system()
        except Exception as exc:  # noqa: BLE001 - a failing setup or build fails this case only
            run = IsolatedRun(result=None, duration_ms=elapsed_ms(started), error=exc)
        else:
            run = await isolated_run(
                system,
                case.input,
                usage_limits=self._limits,
                deps=self._deps,
                timeout=self._timeout,
                decisions=case.decisions,
            )
        base = _case_fields(case, repetition, run)
        if run.error is not None or run.result is None:
            error = run.error or RuntimeError("The run returned no result")
            return CaseResult(
                status="error",
                scores=_unscored(scorers, "not scored: the run failed"),
                error=f"{type(error).__name__}: {error}",
                error_type=type(error).__name__,
                **base,
            ).attach(trace=None, events=run.events)
        result = run.result
        trace = RunTrace(
            result=result,
            events=tuple(run.events),
            duration_ms=run.duration_ms,
            usage=run.usage,
            segments=tuple(run.segments),
            decisions=tuple(run.decisions),
        )
        scores = await _score(scorers, case, trace)
        return CaseResult(
            status="passed" if all(score.passed for score in scores) else "failed",
            output=output_data(result.output),
            agent=result.agent,
            handoff_path=result.handoff_path,
            interrupted=result.is_interrupted,
            unused_decisions=run.unused_decisions,
            scores=scores,
            judge_usage=_judge_usage(scores),
            **base,
        ).attach(trace=trace, events=run.events)


def _case_folder(sandbox: Path | None, folders: tuple[str, ...] | None) -> Path:
    """A new temporary folder with a copy of the subfolders of `sandbox` (all, or `folders`)."""

    folder = Path(tempfile.mkdtemp(prefix="awa-case-"))
    if sandbox is not None:
        for child in sandbox.iterdir():
            wanted = child.name in folders if folders is not None else True
            hidden = child.name.startswith(".") or child.name in _NOT_COPIED
            if wanted and not hidden and child.is_dir():
                shutil.copytree(
                    child, folder / child.name, ignore=shutil.ignore_patterns(*_NOT_COPIED)
                )
    return folder


def _setup_function(spec: str) -> Callable[..., Any]:
    """The function a setup spec names (`<file.py>:<function>` or `<module>:<function>`)."""

    target, name = split_setup(spec)
    try:
        if target.lower().endswith(".py"):
            path = Path(target).resolve()
            digest = hashlib.sha1(str(path).encode(), usedforsecurity=False).hexdigest()[:12]
            module_name = f"awa_eval_setup_{digest}"
            module_spec = importlib.util.spec_from_file_location(module_name, path)
            if module_spec is None or module_spec.loader is None:
                raise EvalError(f"Cannot load the setup file {target!r}")
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_name] = module
            try:
                module_spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(module_name, None)
                raise
        else:
            module = importlib.import_module(target)
    except EvalError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as a setup error
        raise EvalError(f"Cannot load setup {spec!r}: {type(exc).__name__}: {exc}") from None
    function: Callable[..., Any] | None = getattr(module, name, None)
    if not callable(function):
        raise EvalError(f"Setup {spec!r}: {target} has no function {name!r}")
    return function


def _is_judge(scorer: Scorer) -> bool:
    """Whether `scorer` asks a model (not deterministic): a `ModelJudge`, or a scorer that says
    so with `deterministic = False`."""

    return isinstance(scorer, ModelJudge) or getattr(scorer, "deterministic", True) is False


async def _score(scorers: Sequence[Scorer], case: EvalCase, trace: RunTrace) -> list[Score]:
    """Every scorer's verdict, in order: deterministic scorers first; the judges only when all
    of them passed (a judge cannot save a run a deterministic check failed)."""

    plain = [s for s in scorers if not _is_judge(s)]
    judges = [s for s in scorers if _is_judge(s)]
    verdicts = list(await asyncio.gather(*(_apply(s, case, trace) for s in plain)))
    failed = next((score.name for score in verdicts if not score.passed), None)
    if failed is None:
        judged = list(await asyncio.gather(*(_apply(s, case, trace) for s in judges)))
    else:
        reason = f"not judged: {failed} failed"
        judged = _unscored(judges, reason)
    by_id = {id(s): v for s, v in zip([*plain, *judges], [*verdicts, *judged], strict=True)}
    return [by_id[id(s)] for s in scorers]


def _case_fields(case: EvalCase, repetition: int, run: IsolatedRun) -> dict[str, Any]:
    """The `CaseResult` fields every outcome of a case shares."""

    return {
        "case_id": case.id,
        "repetition": repetition,
        "tags": case.tags,
        "duration_ms": run.duration_ms,
        "usage": run.usage,
        "tool_calls": [e.tool_name for e in run.events if isinstance(e, ToolCallEvent)],
        "approvals": requested_approvals(run.segments),
        "decisions": [
            f"{e.decision}:{e.tool}" for e in run.events if isinstance(e, ApprovalDecidedEvent)
        ],
        "segments": len(run.segments),
        "caching": CaseCaching.from_events(run.events),
    }


def _judge_usage(scores: Sequence[Score]) -> Usage:
    """The model usage the judges reported on `scores`."""

    total = Usage()
    for score in scores:
        if score.usage is not None:
            total.merge(score.usage)
    return total


def _unscored(scorers: Sequence[Scorer], reason: str) -> list[Score]:
    """A failed score with `reason` for each of `scorers`."""

    return [Score(name=s.name, value=0.0, passed=False, reason=reason) for s in scorers]


async def _apply(scorer: Scorer, case: EvalCase, trace: RunTrace) -> Score:
    try:
        score = await scorer(case, trace)
    except Exception as exc:  # noqa: BLE001 - a broken scorer fails its score, not the run
        return failed_score(scorer.name, f"{type(exc).__name__}: {exc}")
    if not isinstance(score, Score):
        return failed_score(scorer.name, f"TypeError: expected a Score, got {type(score).__name__}")
    return score if score.name == scorer.name else score.model_copy(update={"name": scorer.name})


__all__ = ["IsolatedRun", "SystemFactory", "evaluate", "evaluate_sync", "isolated_run"]
