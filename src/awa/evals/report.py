"""Evaluation results: per-case results, aggregates, Markdown/JSON output and comparisons.

`EvalReport.passed()` and `EvalReport.compare(baseline)` are what a CI job gates on. With several
repetitions the report also measures reliability (`pass_hat_k`), latency, cost and cache hits.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import fsum
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, computed_field

from awa.contracts.events import ModelResponseEvent, RunEvent, ToolResultEvent
from awa.contracts.usage import Usage
from awa.errors import EvalError
from awa.evals._render import (
    comparison_markdown,
    grouped,
    percentile,
    report_markdown,
    scores_by_name,
)
from awa.evals.scorers import Score
from awa.evals.trace import RunTrace

CaseStatus = Literal["passed", "failed", "error", "skipped"]
"""Outcome of one run of a case: every score passed, some score failed, the
run raised, or the run never started (`fail_fast`)."""

_EPSILON = 1e-9


class CaseCaching(BaseModel):
    """What the caches did in one run.

    Attributes:
        model_calls: Model calls the provider answered.
        replayed_calls: Model calls answered from the replay cache (`awa eval --replay`).
        tool_hits: Tool calls answered from the read-only tool result cache.
        tool_misses: Cacheable tool calls that ran (and were stored).
        prefixes: Per agent, the distinct prefix hashes of its model calls (one when stable).
        model_ms: Duration of each provider-answered model call, in order.
    """

    model_calls: int = 0
    replayed_calls: int = 0
    tool_hits: int = 0
    tool_misses: int = 0
    prefixes: dict[str, list[str]] = Field(default_factory=dict)
    model_ms: list[int] = Field(default_factory=list)

    @classmethod
    def from_events(cls, events: Iterable[RunEvent]) -> CaseCaching:
        """Count the caches' work in a run's events."""

        found = cls()
        for event in events:
            if isinstance(event, ModelResponseEvent):
                if event.replayed:
                    found.replayed_calls += 1
                else:
                    found.model_calls += 1
                    if event.duration_ms is not None:
                        found.model_ms.append(event.duration_ms)
                if event.prefix_hash:
                    seen = found.prefixes.setdefault(event.agent, [])
                    if event.prefix_hash not in seen:
                        seen.append(event.prefix_hash)
            elif isinstance(event, ToolResultEvent):
                found.tool_hits += event.cache == "hit"
                found.tool_misses += event.cache == "miss"
        return found


class CaseResult(BaseModel):
    """One run of one case.

    Attributes:
        repetition: Zero-based repetition index.
        status: `"passed"`, `"failed"`, `"error"` or `"skipped"`.
        output: The run's output as JSON data (`None` on error).
        interrupted: Whether the run ended paused after the case's decisions were applied.
        error: `"<ErrorType>: <message>"` when the run failed.
        cost: The run's cost under the `prices` given to `evaluate` (`None` without; judges
            excluded).
        usage: Usage of the evaluated system's run tree.
        judge_usage: Usage of the model judges that scored this run.
        tool_calls: Names of the tools called in the run tree, across all segments.
        approvals: Tools the run paused on for approval, in order (`""` without a tool).
        decisions: The applied decisions as `"<approve|reject|edit>:<tool>"`, in order.
        segments: Runs the case took: 1, plus one per resume with a decision.
        unused_decisions: Decisions of the case the run never asked for.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    case_id: str
    repetition: int = 0
    status: CaseStatus
    tags: tuple[str, ...] = ()
    output: Any = None
    agent: str | None = None
    handoff_path: tuple[str, ...] = ()
    interrupted: bool = False
    scores: list[Score] = Field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    duration_ms: int = 0
    cost: float | None = None
    usage: Usage = Field(default_factory=Usage)
    judge_usage: Usage = Field(default_factory=Usage)
    tool_calls: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    segments: int = 1
    unused_decisions: int = 0
    caching: CaseCaching = Field(default_factory=CaseCaching)
    _trace: RunTrace | None = PrivateAttr(default=None)
    _events: tuple[RunEvent, ...] = PrivateAttr(default=())

    @property
    def trace(self) -> RunTrace | None:
        """The recorded run (result and events); `None` for failed or loaded results."""

        return self._trace

    @property
    def events(self) -> tuple[RunEvent, ...]:
        """Every event recorded for the run, also for failed runs (empty when loaded)."""

        return self._events

    def score(self, name: str) -> Score | None:
        """Return the score of scorer `name`, or `None`."""

        return next((score for score in self.scores if score.name == name), None)

    def attach(self, *, trace: RunTrace | None, events: Sequence[RunEvent]) -> CaseResult:
        """Attach the in-memory run record (not serialized) and return `self`."""

        self._trace = trace
        self._events = tuple(events)
        return self


class ScorerSummary(BaseModel):
    """Aggregate of one scorer over every run it scored (`errors`: the scorer itself failed)."""

    name: str
    count: int
    mean: float
    pass_rate: float
    passed: int
    failed: int
    errors: int = 0


class ScoreChange(BaseModel):
    """How one case did on one scorer (`None`: the overall outcome) in two reports."""

    case_id: str
    scorer: str | None
    baseline_mean: float
    current_mean: float
    baseline_pass_rate: float
    current_pass_rate: float

    @property
    def delta(self) -> float:
        """`current_mean - baseline_mean`."""

        return self.current_mean - self.baseline_mean

    @property
    def label(self) -> str:
        """`"<case>"` or `"<case> / <scorer>"`."""

        return self.case_id if self.scorer is None else f"{self.case_id} / {self.scorer}"


class ScorerDelta(BaseModel):
    """One scorer's aggregate in two reports (`None` where it did not run)."""

    name: str
    baseline_mean: float | None
    current_mean: float | None
    baseline_pass_rate: float | None
    current_pass_rate: float | None


@dataclass(frozen=True)
class Gate:
    """The verdict of `EvalReport.gate`: `passed`, and one reason per criterion not met."""

    passed: bool
    reasons: tuple[str, ...] = ()


class Comparison(BaseModel):
    """Differences between a report and a baseline report, per case and scorer.

    A case regresses on a scorer when its pass rate dropped, or its mean dropped by more than
    `tolerance`; improvements are the reverse. Scorer `None` is the case's overall outcome.

    Attributes:
        repetitions: Runs per case when both reports used the same number of at least 2 (`None`
            otherwise: `pass^k` is only compared like for like).
        baseline_pass_hat_k: `pass^k` of the baseline over the cases both reports have.
        new_cases: Cases only in the current report.
        missing_cases: Cases only in the baseline.
    """

    baseline: str
    current: str
    baseline_pass_rate: float
    current_pass_rate: float
    repetitions: int | None = None
    baseline_pass_hat_k: float | None = None
    current_pass_hat_k: float | None = None
    regressions: list[ScoreChange] = Field(default_factory=list)
    improvements: list[ScoreChange] = Field(default_factory=list)
    scorers: list[ScorerDelta] = Field(default_factory=list)
    new_cases: list[str] = Field(default_factory=list)
    missing_cases: list[str] = Field(default_factory=list)
    tolerance: float = 0.0

    @property
    def pass_hat_k_dropped(self) -> bool:
        """Whether `pass^k` got lower: fewer cases now pass every repetition."""

        before, after = self.baseline_pass_hat_k, self.current_pass_hat_k
        return before is not None and after is not None and after + _EPSILON < before

    @property
    def has_regressions(self) -> bool:
        """Whether anything got worse: a case or scorer, or `pass^k`."""

        return bool(self.regressions) or self.pass_hat_k_dropped

    def to_markdown(self) -> str:
        """Render the comparison as Markdown."""

        return comparison_markdown(self)


class EvalReport(BaseModel):
    """Everything one `evaluate` call measured.

    The reliability numbers (`pass_at_k`, `pass_hat_k`, latency, cost, `cache_hit_rate`) count only
    runs that started; `pass_at_k` and `pass_hat_k` need at least two `repetitions`.

    Attributes:
        cases: One result per case and repetition, in dataset order.
        usage: Total usage of the evaluated system over all runs.
        judge_usage: Total usage of model judges, reported separately.
        stopped_early: Whether `fail_fast` stopped the evaluation.

    Example:
        ```python
        report = await evaluate(team, dataset, [Contains(), DelegatedTo("billing")])
        print(report.to_markdown())
        assert report.passed(min_pass_rate=0.9)
        assert not report.compare(EvalReport.from_json("baseline.json")).has_regressions
        ```
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    system: str
    dataset: str
    started_at: datetime
    duration_ms: int = 0
    repetitions: int = 1
    cases: list[CaseResult] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    judge_usage: Usage = Field(default_factory=Usage)
    stopped_early: bool = False

    @property
    def runs(self) -> list[CaseResult]:
        """The results of runs that started (everything but `"skipped"`)."""

        return [result for result in self.cases if result.status != "skipped"]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> float:
        """Fraction of started runs whose every score passed (0 when nothing ran)."""

        runs = self.runs
        return sum(result.status == "passed" for result in runs) / len(runs) if runs else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_at_k(self) -> float | None:
        """Fraction of cases where at least one repetition passed; `None` without k or complete cases."""

        cases = self._full_cases()
        if self.repetitions < 2 or not cases:
            return None
        return sum(any(r.status == "passed" for r in runs) for runs in cases.values()) / len(cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_hat_k(self) -> float | None:
        """Fraction of cases where every repetition passed (`pass^k`); `None` without k or complete cases."""

        return self._pass_hat()

    def consistency(self) -> dict[str, float]:
        """Per case, the fraction of its started runs that passed (1.0: every time)."""

        return {
            case_id: sum(r.status == "passed" for r in runs) / len(runs)
            for case_id, runs in grouped(self.runs).items()
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def latency_p50_ms(self) -> int | None:
        """Median run duration in milliseconds (nearest-rank; `None` when nothing ran)."""

        return percentile([r.duration_ms for r in self.runs], 50)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def latency_p95_ms(self) -> int | None:
        """95th-percentile run duration in milliseconds (nearest-rank; `None` when nothing ran)."""

        return percentile([r.duration_ms for r in self.runs], 95)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost(self) -> float | None:
        """Total cost of all runs (judges excluded) in the prices' currency; `None` without prices."""

        costs = [r.cost for r in self.runs if r.cost is not None]
        return fsum(costs) if costs else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost_per_run(self) -> float | None:
        """`cost` divided by the number of runs (`None` without prices)."""

        total = self.cost
        return None if total is None else total / len(self.runs)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_hit_rate(self) -> float | None:
        """Share of prompt tokens served from the provider's cache (`None` without prompt tokens)."""

        used = self.usage
        return used.cache_read_tokens / used.input_tokens if used.input_tokens else None

    def caching(self) -> CaseCaching:
        """What the caches did over every run (prefixes merged per agent)."""

        total = CaseCaching()
        for run in self.runs:
            done = run.caching
            total.model_calls += done.model_calls
            total.replayed_calls += done.replayed_calls
            total.tool_hits += done.tool_hits
            total.tool_misses += done.tool_misses
            total.model_ms += done.model_ms
            for agent, hashes in done.prefixes.items():
                seen = total.prefixes.setdefault(agent, [])
                seen += [h for h in hashes if h not in seen]
        return total

    def _pass_hat(self, only: Collection[str] | None = None) -> float | None:
        """Fraction of the cases (of `only`, when given) that passed every repetition."""

        if self.repetitions < 2:
            return None
        cases = {
            case_id: runs
            for case_id, runs in self._full_cases().items()
            if only is None or case_id in only
        }
        if not cases:
            return None
        return sum(all(r.status == "passed" for r in runs) for runs in cases.values()) / len(cases)

    def _full_cases(self) -> dict[str, list[CaseResult]]:
        """Started runs of the cases that ran in every repetition."""

        return {
            case_id: runs
            for case_id, runs in grouped(self.runs).items()
            if len(runs) == self.repetitions
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def errors(self) -> int:
        """Number of runs that raised."""

        return sum(result.status == "error" for result in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scorers(self) -> list[ScorerSummary]:
        """Aggregate per scorer, in order of first appearance."""

        return [
            ScorerSummary(
                name=name,
                count=len(scores),
                mean=fmean(score.value for score in scores),
                pass_rate=sum(score.passed for score in scores) / len(scores),
                passed=sum(score.passed for score in scores),
                failed=sum(not score.passed for score in scores),
                errors=sum(score.error is not None for score in scores),
            )
            for name, scores in scores_by_name(self.runs).items()
        ]

    def scorer(self, name: str) -> ScorerSummary | None:
        """Return the aggregate of scorer `name`, or `None`."""

        return next((summary for summary in self.scorers if summary.name == name), None)

    def results_for(self, case_id: str) -> list[CaseResult]:
        """Return every repetition of case `case_id`."""

        return [result for result in self.cases if result.case_id == case_id]

    def failures(self) -> list[CaseResult]:
        """Return the runs that failed or raised."""

        return [result for result in self.cases if result.status in ("failed", "error")]

    def gate(
        self,
        min_pass_rate: float = 1.0,
        *,
        thresholds: Mapping[str, float] | None = None,
        max_errors: int | None = None,
        min_pass_hat_k: float | None = None,
        max_cost: float | None = None,
        max_cost_per_run: float | None = None,
    ) -> Gate:
        """Check the evaluation against the given thresholds and say why it failed.

        The gate also fails when nothing ran, `fail_fast` stopped the evaluation, or `min_pass_hat_k`
        is given without a `pass_hat_k`.

        Args:
            thresholds: Minimum pass rate per scorer name; a listed scorer that never ran fails.
            max_errors: Maximum number of runs that raised (`None`: only the pass rate counts them).
            min_pass_hat_k: Minimum fraction of cases that pass every repetition (two or more).
            max_cost: Maximum total `cost`; without prices the gate fails.
            max_cost_per_run: Maximum `cost_per_run`; needs prices as well.
        """

        if not self.runs:
            return Gate(False, ("no run finished",))
        stopped = ", stopped early" if self.stopped_early else ""
        reasons: list[str] = []
        if self.stopped_early or self.pass_rate + _EPSILON < min_pass_rate:
            reasons.append(f"pass rate {self.pass_rate:.1%} (minimum {min_pass_rate:.1%}){stopped}")
        if min_pass_hat_k is not None:
            reliable = self.pass_hat_k
            if self.stopped_early or reliable is None or reliable + _EPSILON < min_pass_hat_k:
                measured = "not measured" if reliable is None else f"{reliable:.1%}"
                reasons.append(
                    f"pass^{self.repetitions} {measured} (minimum {min_pass_hat_k:.1%}){stopped}"
                )
        for name, minimum in (thresholds or {}).items():
            summary = self.scorer(name)
            if summary is None:
                reasons.append(f"scorer {name} did not run (minimum {minimum:.1%})")
            elif summary.pass_rate + _EPSILON < minimum:
                reasons.append(
                    f"scorer {name} pass rate {summary.pass_rate:.1%} (minimum {minimum:.1%})"
                )
        if max_errors is not None and self.errors > max_errors:
            reasons.append(f"{self.errors} error(s) (maximum {max_errors})")
        reasons += self._cost_reasons(max_cost, max_cost_per_run)
        return Gate(not reasons, tuple(reasons))

    def _cost_reasons(self, max_cost: float | None, max_cost_per_run: float | None) -> list[str]:
        if max_cost is None and max_cost_per_run is None:
            return []
        cost, per_run = self.cost, self.cost_per_run
        if cost is None or per_run is None:
            return [
                "the cost gate needs prices: none were given "
                "(prices: in the blueprint or --prices FILE); awa ships no prices"
            ]
        reasons = []
        if max_cost is not None and cost > max_cost + _EPSILON:
            reasons.append(f"cost {cost:.4f} above the budget {max_cost:.4f}")
        if max_cost_per_run is not None and per_run > max_cost_per_run + _EPSILON:
            reasons.append(f"cost per run {per_run:.4f} above {max_cost_per_run:.4f}")
        return reasons

    def passed(
        self,
        min_pass_rate: float = 1.0,
        *,
        thresholds: Mapping[str, float] | None = None,
        max_errors: int | None = None,
        min_pass_hat_k: float | None = None,
        max_cost: float | None = None,
        max_cost_per_run: float | None = None,
    ) -> bool:
        """Whether the evaluation meets the given thresholds (`gate(...).passed`; see `gate`)."""

        return self.gate(
            min_pass_rate,
            thresholds=thresholds,
            max_errors=max_errors,
            min_pass_hat_k=min_pass_hat_k,
            max_cost=max_cost,
            max_cost_per_run=max_cost_per_run,
        ).passed

    def compare(self, baseline: EvalReport, *, tolerance: float = 0.1) -> Comparison:
        """Compare with `baseline`, per case and scorer.

        Args:
            tolerance: How far a mean may drop with an unchanged pass rate before it regresses; keep
                some for model judges.
        """

        before, after = _case_stats(baseline), _case_stats(self)
        shared = [case_id for case_id in after if case_id in before]
        like_for_like = self.repetitions == baseline.repetitions and self.repetitions > 1
        regressions: list[ScoreChange] = []
        improvements: list[ScoreChange] = []
        for case_id in shared:
            for scorer in [s for s in after[case_id] if s in before[case_id]]:
                (old_mean, old_rate), (new_mean, new_rate) = (
                    before[case_id][scorer],
                    after[case_id][scorer],
                )
                change = ScoreChange(
                    case_id=case_id,
                    scorer=scorer,
                    baseline_mean=old_mean,
                    current_mean=new_mean,
                    baseline_pass_rate=old_rate,
                    current_pass_rate=new_rate,
                )
                if new_rate + _EPSILON < old_rate or new_mean + tolerance + _EPSILON < old_mean:
                    regressions.append(change)
                elif new_rate > old_rate + _EPSILON or new_mean > old_mean + tolerance + _EPSILON:
                    improvements.append(change)
        old_scorers = {summary.name: summary for summary in baseline.scorers}
        new_scorers = {summary.name: summary for summary in self.scorers}
        return Comparison(
            baseline=baseline.name,
            current=self.name,
            baseline_pass_rate=baseline.pass_rate,
            current_pass_rate=self.pass_rate,
            repetitions=self.repetitions if like_for_like else None,
            baseline_pass_hat_k=baseline._pass_hat(shared) if like_for_like else None,
            current_pass_hat_k=self._pass_hat(shared) if like_for_like else None,
            regressions=regressions,
            improvements=improvements,
            scorers=[
                ScorerDelta(
                    name=name,
                    baseline_mean=old.mean if (old := old_scorers.get(name)) else None,
                    current_mean=new.mean if (new := new_scorers.get(name)) else None,
                    baseline_pass_rate=old.pass_rate if old else None,
                    current_pass_rate=new.pass_rate if new else None,
                )
                for name in dict.fromkeys([*new_scorers, *old_scorers])
            ],
            new_cases=[case_id for case_id in after if case_id not in before],
            missing_cases=[case_id for case_id in before if case_id not in after],
            tolerance=tolerance,
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return the report as JSON (without the in-memory run records)."""

        return self.model_dump_json(indent=indent)

    def save(self, path: str | Path) -> Path:
        """Write the report as JSON to `path` and return the path."""

        target = Path(path)
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    @classmethod
    def from_json(cls, source: str | Path) -> EvalReport:
        """Load a report written by `to_json` or `save` from a `Path`, a `.json` path string, or JSON text.

        Raises:
            EvalError: If the file cannot be read or is not a report.
        """

        text = str(source)
        if isinstance(source, Path) or ("\n" not in text and not text.lstrip().startswith("{")):
            try:
                text = Path(source).read_text(encoding="utf-8")
            except OSError as exc:
                raise EvalError(f"Cannot read report file {str(source)!r}: {exc}") from None
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            raise EvalError(f"Not an evaluation report: {exc}") from None

    def to_markdown(self, *, max_failures: int = 20) -> str:
        """Render the report as Markdown, detailing at most `max_failures` failing runs."""

        return report_markdown(self, max_failures=max_failures)


def _case_stats(report: EvalReport) -> dict[str, dict[str | None, tuple[float, float]]]:
    stats: dict[str, dict[str | None, tuple[float, float]]] = {}
    for case_id, results in grouped(report.runs).items():
        entry: dict[str | None, tuple[float, float]] = {}
        outcome = [1.0 if result.status == "passed" else 0.0 for result in results]
        entry[None] = (fmean(outcome), fmean(outcome))
        for name, values in scores_by_name(results).items():
            entry[name] = (
                fmean(score.value for score in values),
                sum(score.passed for score in values) / len(values),
            )
        stats[case_id] = entry
    return stats


__all__ = [
    "CaseResult",
    "CaseStatus",
    "Comparison",
    "EvalReport",
    "Gate",
    "ScoreChange",
    "ScorerDelta",
    "ScorerSummary",
]
