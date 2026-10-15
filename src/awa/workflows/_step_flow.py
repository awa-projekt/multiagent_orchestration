"""Retries, run conditions, special-case routing and the documents port of one process step."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from awa._internal.retry import backoff_delay
from awa.workflows._special_cases import CASE_TABLE_NAME, CaseOutcome, SpecialCases

MAX_ATTEMPTS = 5
"""Most attempts one step may have, the first included: a retried step costs a model run each
time, and a model that keeps failing a check rarely passes it on the sixth try."""

RetryOn: TypeAlias = Literal["error", "check"]
"""What a retry reacts to: `error` (the agent run raised) or `check` (a check of the step failed,
the implicit answer check included)."""

LastTryAction: TypeAlias = Literal["stop", "ask", "go"]
"""What happens when the last try failed: `stop` the run, `ask` a person, or `go` to a later step."""

_NOT_RETRIED = frozenset(
    {
        "awa.usage_limit_exceeded",
        "awa.run_cancelled",
        "awa.run_timeout",
        "awa.guardrail_tripwire",
        "awa.permission_denied",
        "awa.authentication",
        "awa.configuration",
        "awa.missing_configuration",
        "awa.optional_dependency",
        "awa.model_resolution",
        "awa.step_check_failed",
        "awa.step_rejected",
    }
)
_PROGRAMMING_ERRORS = (
    ValueError,
    TypeError,
    ArithmeticError,
    ImportError,
    LookupError,
    NameError,
    SyntaxError,
    RuntimeError,
    AssertionError,
    AttributeError,
)


@dataclass(frozen=True)
class StepRetry:
    """How often a step is tried and how long it waits in between.

    The wait before try n+1 is `initial * backoff**(n-1)`, at most `max_delay` (default ten times
    `initial`), spread by up to `jitter` (a fraction of it) either way.

    Attributes:
        max_attempts: Tries in total, the first included; 1 to `MAX_ATTEMPTS`.
        initial: Seconds to wait before the second try.
        backoff: Growth of the wait per try (2 doubles it).
        jitter: Spread of the wait as a fraction (0 for none, at most 1).
        max_delay: Longest wait in seconds; `None` for ten times `initial`.
        on: What is tried again: `error`, `check` or both.
    """

    max_attempts: int = 3
    initial: float = 1.0
    backoff: float = 2.0
    max_delay: float | None = None
    jitter: float = 0.0
    on: frozenset[RetryOn] = field(default_factory=lambda: frozenset({"error", "check"}))

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be 1 to {MAX_ATTEMPTS}, not {self.max_attempts}")
        if self.initial < 0 or self.backoff < 1 or not 0 <= self.jitter <= 1:
            raise ValueError("initial must be >= 0, backoff >= 1 and jitter between 0 and 1")
        if self.max_delay is not None and self.max_delay < 0:
            raise ValueError("max_delay must be >= 0")
        unknown = set(self.on) - {"error", "check"}
        if not self.on or unknown:
            raise ValueError("on must name 'error', 'check' or both")

    def delay(self, retries_done: int, rand: Callable[[], float] | None = None) -> float:
        """Seconds to wait before the next try, after `retries_done` retries (0 before the first)."""

        cap = self.max_delay if self.max_delay is not None else 10 * self.initial
        return backoff_delay(
            retries_done,
            initial=self.initial,
            factor=self.backoff,
            cap=cap,
            jitter=self.jitter,
            rand=rand,
        )


@dataclass(frozen=True)
class AfterLastTry:
    """What happens when the last try of a step failed.

    Attributes:
        action: `stop` (default: the run fails), `ask` (a person decides: approve continues with
            the last result) or `go` (continue at step `to`; the failed result is not passed on).
        to: The later step for `go`; required for `go` only.
    """

    action: LastTryAction = "stop"
    to: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ("stop", "ask", "go"):
            raise ValueError(f"after_last_try must be stop, ask or go, not {self.action!r}")
        if (self.action == "go") != (self.to is not None):
            raise ValueError(
                "after_last_try 'go' needs the step to continue at, and only 'go' does"
            )
        if self.to is not None and not CASE_TABLE_NAME.fullmatch(self.to):
            raise ValueError(f"{self.to!r} is not a step name")


@dataclass(frozen=True)
class RunCondition:
    """A step's `when`: the step runs only when the expression is true.

    Evaluated strictly: a needed value that is missing, empty or unclear makes the answer unknown
    (`None`), never false.
    """

    expression: str
    _table: SpecialCases = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            table = SpecialCases.model_validate(
                {"cases": [{"if": self.expression, "then": "approve"}]}
            )
        except ValueError as exc:
            raise ValueError(f"when {self.expression!r} cannot be read: {exc}") from None
        object.__setattr__(self, "_table", table)

    def holds(self, values: Mapping[str, Any]) -> tuple[bool | None, str]:
        """`(True|False|None, why)`: whether the step runs; `None` with the reason when unclear."""

        return self._table.check_row(1, values)

    @property
    def fields(self) -> list[str]:
        """The value names the condition reads."""

        return self._table.fields()


def may_retry_error(exc: BaseException) -> bool:
    """Whether a step may be tried again after `exc`.

    Not after a limit, cancellation, run timeout, guardrail block, permission or configuration
    problem, a nested process's check or rejection, or a programming error outside awa's own errors.
    """

    from awa.errors import UsageLimitExceededError

    if not isinstance(exc, Exception) or isinstance(exc, UsageLimitExceededError):
        return False  # every reached limit, `NoProgressError` included, is final
    code = getattr(exc, "code", None)
    if isinstance(code, str):  # a subclass keeps the rule of the error it refines
        codes = {getattr(cls, "code", None) for cls in type(exc).__mro__}
        return _NOT_RETRIED.isdisjoint(codes | {code})
    return not isinstance(exc, _PROGRAMMING_ERRORS)


def case_id(step: str, outcome: CaseOutcome) -> str:
    """The id of a special-case outcome: `<step>.case.<row>`, `<step>.case.else` or
    `<step>.case.<row>.unclear`."""

    if outcome.source == "row":
        return f"{step}.case.{outcome.row}"
    if outcome.source == "else":
        return f"{step}.case.else"
    return f"{step}.case.{outcome.row}.unclear"


def case_record(step: str, outcome: CaseOutcome) -> dict[str, Any]:
    """The outcome as plain data for the run record: id, action, source, row, words."""

    return {
        "id": case_id(step, outcome),
        "then": outcome.then,
        "to": outcome.action.to,
        "by": outcome.action.by,
        "source": outcome.source,
        "row": outcome.row,
        "text": outcome.describe(),
    }
