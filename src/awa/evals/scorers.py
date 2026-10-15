"""Scorers: turn one run of a case into a `Score` between 0 and 1.

A scorer is any object with a `name` and an async `__call__(case, trace)`. Every built-in scorer
except `ModelJudge` is deterministic. Scorers comparing with an expected value use
`case.expected` unless given one.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from awa._internal.util import preview
from awa.contracts.events import ApprovalDecidedEvent, ToolResultEvent
from awa.contracts.usage import Usage
from awa.errors import ConfigurationError
from awa.evals.dataset import EvalCase
from awa.evals.trace import RunTrace, output_data, output_text


class Score(BaseModel):
    """The verdict of one scorer on one run.

    Attributes:
        value: Score between 0 (worst) and 1 (best).
        error: Set when the scorer itself failed (the score then counts as failed).
        usage: Model usage of the scorer (model judges only), never the system's.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: float = Field(ge=0.0, le=1.0)
    passed: bool
    reason: str = ""
    error: str | None = None
    usage: Usage | None = None


@runtime_checkable
class Scorer(Protocol):
    """Anything that scores a run: a `name` and an async `__call__(case, trace)`."""

    @property
    def name(self) -> str:
        """Name of the scorer; unique among the scorers of one evaluation."""
        ...

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        """Score one run of `case`."""
        ...


class _Base:
    """Shared plumbing: a name and `Score` construction."""

    default_name = "scorer"

    def __init__(self, name: str | None = None) -> None:
        self._name = name or self.default_name

    @property
    def name(self) -> str:
        """Name of the scorer."""

        return self._name

    def _score(self, value: float, passed: bool, reason: str) -> Score:
        return Score(name=self._name, value=min(1.0, max(0.0, value)), passed=passed, reason=reason)

    def _verdict(self, passed: bool, reason: str) -> Score:
        return self._score(1.0 if passed else 0.0, passed, reason)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self._name!r})"


_MISSING: Any = object()


def _expected(case: EvalCase, given: Any, scorer: str) -> Any:
    value = case.expected if given is _MISSING else given
    if value is None:
        raise ConfigurationError(
            f"{scorer} needs an expected value: set `expected` on case {case.id!r}"
        )
    return value


def _check_bounds(min_times: int, max_times: int | None) -> None:
    if min_times < 0 or (max_times is not None and max_times < min_times):
        raise ConfigurationError("need 0 <= min_times <= max_times")


def _within(count: int, low: int, high: int | None) -> tuple[bool, str]:
    """Whether `count` lies in `low..high` (`None`: no maximum), and the range as text."""

    passed = count >= low and (high is None or count <= high)
    return passed, f"{low}..{'n' if high is None else high}"


def failed_score(name: str, error: str, usage: Usage | None = None) -> Score:
    """The score of a scorer that raised or returned no `Score` (`error` names the problem)."""

    return Score(
        name=name, value=0.0, passed=False, reason="the scorer failed", error=error, usage=usage
    )


def _clip(text: str, limit: int = 80) -> str:
    return preview(text, limit, flatten=True)


class ExactMatch(_Base):
    """The output equals the expected value (default `case.expected`).

    Text is compared stripped (case-insensitively with `case_sensitive=False`); structured outputs
    as JSON data, so a pydantic output matches an expected mapping.
    """

    default_name = "exact_match"

    def __init__(
        self, expected: Any = _MISSING, *, case_sensitive: bool = True, name: str | None = None
    ) -> None:
        super().__init__(name)
        self._expected = expected
        self._case_sensitive = case_sensitive

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        expected = output_data(_expected(case, self._expected, "ExactMatch"))
        if isinstance(expected, str):
            actual_text = output_text(trace.output).strip()
            wanted = expected.strip()
            if not self._case_sensitive:
                actual_text, wanted = actual_text.casefold(), wanted.casefold()
            if actual_text == wanted:
                return self._verdict(True, "output matches")
            return self._verdict(False, f"expected {_clip(expected)!r}, got {_clip(actual_text)!r}")
        actual = output_data(trace.output)
        if actual == expected:
            return self._verdict(True, "output matches")
        return self._verdict(
            False, f"expected {_clip(json.dumps(expected, default=str))}, got {_clip(str(actual))}"
        )


class Contains(_Base):
    """The output text contains every phrase (`mode="any"`: at least one); the value is the share found.

    `*phrases` default to `case.expected` (a string or a list of strings).
    """

    default_name = "contains"

    def __init__(
        self,
        *phrases: str,
        case_sensitive: bool = False,
        mode: Literal["all", "any"] = "all",
        name: str | None = None,
    ) -> None:
        if mode not in ("all", "any"):
            raise ConfigurationError(f"mode must be 'all' or 'any', got {mode!r}")
        super().__init__(name)
        self._phrases = phrases
        self._case_sensitive = case_sensitive
        self._mode = mode

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        phrases = list(self._phrases) or _phrases(_expected(case, _MISSING, "Contains"))
        text = output_text(trace.output)
        haystack = text if self._case_sensitive else text.casefold()
        found = [p for p in phrases if (p if self._case_sensitive else p.casefold()) in haystack]
        missing = [p for p in phrases if p not in found]
        passed = not missing if self._mode == "all" else bool(found)
        value = len(found) / len(phrases)
        if passed:
            return self._score(value, True, f"found {len(found)} of {len(phrases)} phrase(s)")
        return self._score(value, False, f"missing {', '.join(repr(p) for p in missing)}")


def _phrases(expected: Any) -> list[str]:
    if isinstance(expected, str):
        return [expected]
    if isinstance(expected, Sequence) and expected and all(isinstance(p, str) for p in expected):
        return list(expected)
    raise ConfigurationError(
        "Contains needs a string or a non-empty list of strings as expected value"
    )


class RegexMatch(_Base):
    """The output text matches `pattern` (default `case.expected`) with `re.search`."""

    default_name = "regex"

    def __init__(
        self, pattern: str | None = None, *, flags: int = 0, name: str | None = None
    ) -> None:
        super().__init__(name)
        self._pattern = re.compile(pattern, flags) if pattern is not None else None
        self._flags = flags

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        pattern = self._pattern
        if pattern is None:
            expected = _expected(case, _MISSING, "RegexMatch")
            if not isinstance(expected, str):
                raise ConfigurationError("RegexMatch needs a pattern string as expected value")
            pattern = re.compile(expected, self._flags)
        text = output_text(trace.output)
        if pattern.search(text):
            return self._verdict(True, f"output matches /{pattern.pattern}/")
        return self._verdict(False, f"no match for /{pattern.pattern}/ in {_clip(text)!r}")


class JsonFieldsMatch(_Base):
    """Fields of a structured output have the expected values; the value is the share that match.

    The output may be a model, a dataclass, a mapping or JSON text; keys may be dotted paths
    (`"customer.id"`, `"items.0"`). `fields` default to `case.expected`.

    Example:
        ```python
        JsonFieldsMatch({"status": "delivered", "needs_human": False})
        ```
    """

    default_name = "json_fields"

    def __init__(self, fields: Mapping[str, Any] | None = None, *, name: str | None = None) -> None:
        super().__init__(name)
        if fields is not None and not fields:
            raise ConfigurationError("JsonFieldsMatch needs at least one field")
        self._fields = dict(fields) if fields is not None else None

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        fields = self._fields
        if fields is None:
            expected = _expected(case, _MISSING, "JsonFieldsMatch")
            if not isinstance(expected, Mapping) or not expected:
                raise ConfigurationError(
                    "JsonFieldsMatch needs a non-empty mapping as expected value"
                )
            fields = dict(expected)
        data = output_data(trace.output)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return self._verdict(False, "the output is not a JSON object")
        wrong: list[str] = []
        for path, wanted in fields.items():
            actual = _lookup(data, path)
            if actual is _MISSING:
                wrong.append(f"{path} missing")
            elif actual != output_data(wanted):
                wrong.append(f"{path}={_clip(json.dumps(actual, default=str), 40)}")
        value = (len(fields) - len(wrong)) / len(fields)
        if not wrong:
            return self._score(1.0, True, f"{len(fields)} field(s) match")
        return self._score(value, False, f"mismatched: {', '.join(wrong)}")


def _lookup(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


class ToolWasCalled(_Base):
    """A tool was called (anywhere in the run tree) between `min_times` and `max_times` times.

    Args:
        agent: Only count calls made by this agent.
        args: Only count calls whose arguments include these values.
        name: Scorer name; defaults to `"tool:<tool>"`.
    """

    def __init__(
        self,
        tool: str,
        *,
        agent: str | None = None,
        args: Mapping[str, Any] | None = None,
        min_times: int = 1,
        max_times: int | None = None,
        name: str | None = None,
    ) -> None:
        _check_bounds(min_times, max_times)
        super().__init__(name or f"tool:{tool}")
        self._tool = tool
        self._agent = agent
        self._args = dict(args or {})
        self._min = min_times
        self._max = max_times

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        calls = [
            event
            for event in trace.tool_calls(self._tool)
            if (self._agent is None or event.agent == self._agent)
            and all(
                key in event.arguments and event.arguments[key] == value
                for key, value in self._args.items()
            )
        ]
        passed, bounds = _within(len(calls), self._min, self._max)
        by = f" by {self._agent}" if self._agent else ""
        return self._verdict(
            passed, f"{self._tool} called {len(calls)} time(s){by}, wanted {bounds}"
        )


class ToolNotCalled(ToolWasCalled):
    """A tool was never called (by `agent`, when given) in the run tree; named `"no_tool:<tool>"`."""

    def __init__(self, tool: str, *, agent: str | None = None, name: str | None = None) -> None:
        super().__init__(
            tool, agent=agent, min_times=0, max_times=0, name=name or f"no_tool:{tool}"
        )


def _fits(actual: Any, wanted: Any) -> bool:
    """Whether an argument fits the wanted value: mappings by wanted keys, lists by items, text by containment."""

    if isinstance(wanted, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _fits(actual[key], value) for key, value in wanted.items()
        )
    if isinstance(wanted, list):
        items = actual if isinstance(actual, list) else [actual]
        return all(any(_fits(item, value) for item in items) for value in wanted)
    if isinstance(wanted, str):
        text = actual if isinstance(actual, str) else json.dumps(actual, default=str)
        return " ".join(wanted.split()).casefold() in " ".join(text.split()).casefold()
    if isinstance(wanted, bool) or isinstance(actual, bool):
        return actual is wanted
    return bool(actual == wanted)


class ToolArgsMatch(_Base):
    """Some call of `tool` had arguments that match `args`; named `"tool_args_match:<tool>"`.

    Mappings match by their wanted keys, lists when every wanted item fits some item, text when the
    argument contains it (ignoring case and spacing), numbers and booleans by equality.

    Example:
        ```python
        ToolArgsMatch("create_purchase_order", {"supplier_id": "S-104", "quantity": 25})
        ```
    """

    def __init__(self, tool: str, args: Mapping[str, Any], *, name: str | None = None) -> None:
        if not isinstance(args, Mapping) or not args:
            raise ConfigurationError("tool_args_match needs a mapping of the wanted arguments")
        super().__init__(name or f"tool_args_match:{tool}")
        self._tool = tool
        self._args = dict(args)

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        calls = trace.tool_calls(self._tool)
        if any(_fits(call.arguments, self._args) for call in calls):
            return self._verdict(True, f"{self._tool} called with {_clip(json.dumps(self._args))}")
        if not calls:
            return self._verdict(False, f"{self._tool} was not called")
        seen = "; ".join(_clip(json.dumps(c.arguments, default=str), 120) for c in calls[-3:])
        return self._verdict(
            False, f"no {self._tool} call matched {_clip(json.dumps(self._args))} (got: {seen})"
        )


class MaxToolCalls(_Base):
    """The run tree made at most `limit` tool calls (of `tool`, when given)."""

    def __init__(self, limit: int, *, tool: str | None = None, name: str | None = None) -> None:
        if limit < 0:
            raise ConfigurationError(f"limit must not be negative, got {limit!r}")
        super().__init__(name or (f"max_calls:{tool}:{limit}" if tool else f"max_calls:{limit}"))
        self._limit = limit
        self._tool = tool

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        count = len(trace.tool_calls(self._tool))
        what = f"{self._tool} calls" if self._tool else "tool calls"
        return self._verdict(count <= self._limit, f"{count} {what}, at most {self._limit}")


class DelegatedTo(_Base):
    """Some agent (`by`, when given) delegated to or consulted `agent` at least `min_times` times.

    Args:
        mode: `"delegate"`, `"consult"`, or `None` for both.
        name: Scorer name; defaults to `"delegated:<agent>"`.
    """

    def __init__(
        self,
        agent: str,
        *,
        by: str | None = None,
        mode: Literal["delegate", "consult"] | None = None,
        min_times: int = 1,
        name: str | None = None,
    ) -> None:
        if min_times < 1:
            raise ConfigurationError("min_times must be at least 1")
        super().__init__(name or f"delegated:{agent}")
        self._agent = agent
        self._by = by
        self._mode = mode
        self._min = min_times

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        count = sum(
            event.target == self._agent
            and (self._by is None or event.agent == self._by)
            and (self._mode is None or event.mode == self._mode)
            for event in trace.delegations()
        )
        seen = sorted({event.target for event in trace.delegations()})
        passed = count >= self._min
        reason = f"{count} delegation(s) to {self._agent}"
        if not passed:
            reason += f"; delegated to: {', '.join(seen) or 'nobody'}"
        return self._verdict(passed, reason)


class VerdictIs(_Base):
    """The run's typed `Verdict` has this decision (`accept`, `revise` or `escalate`).

    Reads a judge agent's output or the last verdict of a directly run `Checked`; a run without a
    verdict fails.

    Raises:
        ConfigurationError: If `decision` is not `accept`, `revise` or `escalate`.
    """

    def __init__(self, decision: str, *, name: str | None = None) -> None:
        if decision not in ("accept", "revise", "escalate"):
            raise ConfigurationError(
                f"decision must be accept, revise or escalate, got {decision!r}"
            )
        super().__init__(name or f"verdict:{decision}")
        self._decision = decision

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        found = _verdict_decision(trace)
        if found is None:
            return self._verdict(
                False,
                "the run has no verdict; evaluate a judge agent (awa eval --agent <judge>) "
                "or a Checked unit",
            )
        return self._verdict(found == self._decision, f"the verdict is {found}")


def _verdict_decision(trace: RunTrace) -> str | None:
    data = output_data(trace.output)
    if isinstance(data, Mapping) and isinstance(data.get("decision"), str):
        return str(data["decision"])
    verdicts = trace.result.metadata.get("verdicts")
    if isinstance(verdicts, list) and verdicts and isinstance(verdicts[-1], Mapping):
        decision = verdicts[-1].get("decision")
        return decision if isinstance(decision, str) else None
    return None


class HandedOffTo(_Base):
    """Control was handed off to `agent` (from `source`, when given; with `final`, it also answered)."""

    def __init__(
        self, agent: str, *, source: str | None = None, final: bool = False, name: str | None = None
    ) -> None:
        super().__init__(name or f"handoff:{agent}")
        self._agent = agent
        self._source = source
        self._final = final

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        handed = any(
            event.target == self._agent and (self._source is None or event.source == self._source)
            for event in trace.handoffs()
        )
        path = " -> ".join(trace.result.handoff_path) or "-"
        if not handed:
            return self._verdict(False, f"no handoff to {self._agent} (path {path})")
        if self._final and trace.result.agent != self._agent:
            return self._verdict(False, f"{trace.result.agent} answered last (path {path})")
        return self._verdict(True, f"handed off to {self._agent} (path {path})")


class RequiresApproval(_Base):
    """The run ended paused for a human decision, optionally on a call of `tool`.

    It checks the final state after the case's `decisions`; for a request that was then answered,
    use `ApprovalRequested`.
    """

    def __init__(self, tool: str | None = None, *, name: str | None = None) -> None:
        super().__init__(name or (f"approval:{tool}" if tool else "approval"))
        self._tool = tool

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        pending = trace.result.interrupts
        tools = [
            str(item["value"].get("tool"))
            for item in pending
            if isinstance(item.get("value"), Mapping) and item["value"].get("tool")
        ]
        passed = bool(pending) if self._tool is None else self._tool in tools
        shown = ", ".join(tools) or "interrupt"
        if passed:
            return self._verdict(True, f"paused for approval ({shown})")
        if pending:
            return self._verdict(False, f"paused for {shown}, not {self._tool}")
        return self._verdict(False, "the run did not pause for approval")


class ApprovalRequested(_Base):
    """The run paused for approval between `min_times` and `max_times` times, every pause counted.

    Example:
        ```python
        ApprovalRequested("book_invoice")          # 6,200 EUR: a human was asked
        NoApprovalRequested("book_invoice")        # 800 EUR: booked straight away
        ```
    """

    def __init__(
        self,
        tool: str | None = None,
        *,
        min_times: int = 1,
        max_times: int | None = None,
        name: str | None = None,
    ) -> None:
        _check_bounds(min_times, max_times)
        super().__init__(name or (f"approval_requested:{tool}" if tool else "approval_requested"))
        self._tool = tool
        self._min = min_times
        self._max = max_times

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        requested = trace.approval_requests()
        count = sum(self._tool is None or tool == self._tool for tool in requested)
        passed, bounds = _within(count, self._min, self._max)
        what = f"approval for {self._tool}" if self._tool else "approval"
        shown = ", ".join(tool or "interrupt" for tool in requested) or "none"
        return self._verdict(
            passed, f"{what} requested {count} time(s), wanted {bounds} (requests: {shown})"
        )


class NoApprovalRequested(ApprovalRequested):
    """The run never paused for approval (of `tool`, when given)."""

    def __init__(self, tool: str | None = None, *, name: str | None = None) -> None:
        super().__init__(
            tool,
            min_times=0,
            max_times=0,
            name=name or (f"no_approval:{tool}" if tool else "no_approval"),
        )


class ApprovalDecided(_Base):
    """A decision on an approval-gated call was applied: granted (`granted=True`) or rejected.

    An approval, also with edited arguments, grants; a rejection, also an expired or malformed one,
    rejects.
    """

    def __init__(
        self,
        tool: str | None = None,
        *,
        granted: bool = True,
        min_times: int = 1,
        name: str | None = None,
    ) -> None:
        if min_times < 1:
            raise ConfigurationError("min_times must be at least 1")
        kind = "approval_granted" if granted else "approval_rejected"
        super().__init__(name or (f"{kind}:{tool}" if tool else kind))
        self._tool = tool
        self._granted = granted
        self._min = min_times

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        decisions = trace.approval_decisions(self._tool)
        wanted = ("approve", "edit") if self._granted else ("reject",)
        count = sum(event.decision in wanted for event in decisions)
        what = "granted" if self._granted else "rejected"
        target = f" for {self._tool}" if self._tool else ""
        shown = ", ".join(f"{e.decision}:{e.tool}" for e in decisions) or "none"
        return self._verdict(
            count >= self._min, f"{count} call(s){target} {what} (decisions: {shown})"
        )


class ApprovalPresent(_Base):
    """`tool` never took effect without a person's approval.

    Every successful result must follow its own granting decision. A run that never called `tool`
    passes; combine with `tool:<name>` when the call is required.
    """

    def __init__(self, tool: str, *, name: str | None = None) -> None:
        super().__init__(name or f"approval_present:{tool}")
        self._tool = tool

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        pending: list[str] = []  # decisions not yet answered by a result, oldest first
        executed = ungated = 0
        for event in trace.events:
            if isinstance(event, ApprovalDecidedEvent) and event.tool == self._tool:
                pending.append(event.decision)
            elif isinstance(event, ToolResultEvent) and event.tool_name == self._tool:
                decision = pending.pop(0) if pending else None
                if event.is_error or decision == "reject":
                    continue  # nothing took effect (a rejection returns its message)
                executed += 1
                ungated += decision is None
        if ungated:
            return self._verdict(
                False, f"{self._tool} took effect {ungated} time(s) without an approval"
            )
        if not executed:
            return self._verdict(True, f"{self._tool} never took effect")
        return self._verdict(True, f"{self._tool} took effect {executed} time(s), each approved")


class MaxLatency(_Base):
    """The run finished within `ms` milliseconds; slower runs score `ms / duration`."""

    default_name = "max_latency"

    def __init__(self, ms: float, *, name: str | None = None) -> None:
        if ms <= 0:
            raise ConfigurationError(f"ms must be positive, got {ms!r}")
        super().__init__(name)
        self._ms = ms

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        duration = trace.duration_ms
        passed = duration <= self._ms
        value = 1.0 if passed else self._ms / duration
        return self._score(value, passed, f"{duration} ms (budget {self._ms:g} ms)")


class MaxTokens(_Base):
    """The run tree used at most `limit` tokens (`kind`: total, input or output); over budget scores `limit / used`."""

    default_name = "max_tokens"

    def __init__(
        self,
        limit: int,
        *,
        kind: Literal["total", "input", "output"] = "total",
        name: str | None = None,
    ) -> None:
        if limit < 0:
            raise ConfigurationError(f"limit must not be negative, got {limit!r}")
        if kind not in ("total", "input", "output"):
            raise ConfigurationError(f"kind must be 'total', 'input' or 'output', got {kind!r}")
        super().__init__(name)
        self._limit = limit
        self._kind = kind

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        usage = trace.total_usage
        used = {
            "total": usage.total_tokens,
            "input": usage.input_tokens,
            "output": usage.output_tokens,
        }[self._kind]
        passed = used <= self._limit
        value = 1.0 if passed else self._limit / used
        return self._score(value, passed, f"{used} {self._kind} tokens (budget {self._limit})")


CustomOutcome: TypeAlias = Score | bool | float | tuple[float | bool, str]
"""What a `Custom` function returns: a `Score`, a bool, a value in [0, 1], or
`(value_or_bool, reason)`."""

CustomFunction: TypeAlias = Callable[[EvalCase, RunTrace], CustomOutcome | Awaitable[CustomOutcome]]
"""The function a `Custom` scorer wraps: `(case, trace)` to a `CustomOutcome`, sync or async."""


class Custom(_Base):
    """A scorer from a plain (sync or async) function of `(case, trace)`.

    Args:
        function: Returns a `Score`, a bool, a value in [0, 1], or a `(value_or_bool, reason)`
            tuple.
        threshold: Minimum value that passes, for numeric results.

    Example:
        ```python
        def cites_order(case: EvalCase, trace: RunTrace) -> bool:
            return case.metadata["order_id"] in str(trace.output)

        scorer = Custom(cites_order)
        ```
    """

    def __init__(
        self, function: CustomFunction, *, name: str | None = None, threshold: float = 0.5
    ) -> None:
        super().__init__(name or getattr(function, "__name__", "custom"))
        self._function = function
        self._threshold = threshold

    async def __call__(self, case: EvalCase, trace: RunTrace) -> Score:
        outcome = self._function(case, trace)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        reason = ""
        if isinstance(outcome, tuple):
            outcome, reason = outcome
        if isinstance(outcome, Score):
            return (
                outcome
                if outcome.name == self.name
                else outcome.model_copy(update={"name": self.name})
            )
        if isinstance(outcome, bool):
            return self._verdict(outcome, reason or ("passed" if outcome else "failed"))
        if isinstance(outcome, int | float):
            value = float(outcome)
            if not 0.0 <= value <= 1.0:
                raise ConfigurationError(f"{self.name} returned {value}, outside [0, 1]")
            return self._score(value, value >= self._threshold, reason or f"score {value:.2f}")
        raise TypeError(
            f"{self.name} returned {type(outcome).__name__}; expected a Score, bool or float"
        )


__all__ = [
    "ApprovalDecided",
    "ApprovalPresent",
    "ApprovalRequested",
    "Contains",
    "Custom",
    "CustomFunction",
    "CustomOutcome",
    "DelegatedTo",
    "ExactMatch",
    "HandedOffTo",
    "JsonFieldsMatch",
    "MaxLatency",
    "MaxTokens",
    "MaxToolCalls",
    "NoApprovalRequested",
    "RegexMatch",
    "RequiresApproval",
    "Score",
    "Scorer",
    "ToolArgsMatch",
    "ToolNotCalled",
    "ToolWasCalled",
    "VerdictIs",
]
