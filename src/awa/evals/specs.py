"""Scorer specifications: short strings for the built-in scorers, used in dataset
files (`scorers:` of a case) and by `awa eval --scorer`.

| spec | scorer |
| --- | --- |
| `exact` | `ExactMatch()` against `expected` |
| `contains` / `contains:<text>` | `Contains()` against `expected`, or the given text |
| `regex` / `regex:<pattern>` | `RegexMatch` with `expected` or the given pattern |
| `json_fields` | `JsonFieldsMatch()` against `expected` (a mapping) |
| `tool:<name>` / `no_tool:<name>` | `ToolWasCalled` / `ToolNotCalled` |
| `tool_args_match:<name>:<mapping>` | `ToolArgsMatch`: a call had these arguments (YAML or JSON mapping) |
| `max_calls:<n>` / `max_calls:<name>:<n>` | `MaxToolCalls`: at most `n` tool calls (of that tool) |
| `delegated:<agent>` | `DelegatedTo(agent)` |
| `handoff:<agent>` | `HandedOffTo(agent)` |
| `verdict:<decision>` | `VerdictIs(decision)`: a judge's verdict is `accept`, `revise` or `escalate` |
| `approval` / `approval:<tool>` | `RequiresApproval`: the run ended paused |
| `approval_requested` / `approval_requested:<tool>` | `ApprovalRequested`: asked at any point |
| `no_approval` / `no_approval:<tool>` | `NoApprovalRequested` |
| `approval_granted` / `approval_granted:<tool>` | `ApprovalDecided(granted=True)` |
| `approval_rejected` / `approval_rejected:<tool>` | `ApprovalDecided(granted=False)` |
| `approval_present:<tool>` | `ApprovalPresent`: the tool never took effect without approval |
| `max_latency:<ms>` | `MaxLatency(ms)` |
| `max_tokens:<n>` | `MaxTokens(n)` |
| `judge:<criteria>` | `ModelJudge(criteria)` (not deterministic) |

A parsed scorer is named after its spec (`"tool:lookup_order"`); a judge is
named `"judge"`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping

import yaml

from awa._internal.upstream.models import BaseChatModel
from awa.errors import ConfigurationError, EvalError
from awa.evals.judge import ModelJudge
from awa.evals.scorers import (
    ApprovalDecided,
    ApprovalPresent,
    ApprovalRequested,
    Contains,
    DelegatedTo,
    ExactMatch,
    HandedOffTo,
    JsonFieldsMatch,
    MaxLatency,
    MaxTokens,
    MaxToolCalls,
    NoApprovalRequested,
    RegexMatch,
    RequiresApproval,
    Scorer,
    ToolArgsMatch,
    ToolNotCalled,
    ToolWasCalled,
    VerdictIs,
)

_Factory = Callable[[str | None, str], Scorer]
"""Builds a scorer from the spec's argument (`None` for a spec without one) and its name."""


def _contains(arg: str | None, name: str) -> Scorer:
    return Contains(*([arg] if arg else []), name=name)


def _approval_decided(granted: bool) -> _Factory:
    return lambda arg, name: ApprovalDecided(arg, granted=granted, name=name)


# kind -> (factory, argument): "none" takes no argument, "needs" requires one, "either" allows one.
_SCORERS: dict[str, tuple[_Factory, str]] = {
    "exact": (lambda arg, name: ExactMatch(name=name), "none"),
    "json_fields": (lambda arg, name: JsonFieldsMatch(name=name), "none"),
    "contains": (_contains, "either"),
    "regex": (lambda arg, name: RegexMatch(arg, name=name), "either"),
    "approval": (lambda arg, name: RequiresApproval(arg, name=name), "either"),
    "approval_requested": (lambda arg, name: ApprovalRequested(arg, name=name), "either"),
    "no_approval": (lambda arg, name: NoApprovalRequested(arg, name=name), "either"),
    "approval_granted": (_approval_decided(True), "either"),
    "approval_rejected": (_approval_decided(False), "either"),
    "tool": (lambda arg, name: ToolWasCalled(_text(arg), name=name), "needs"),
    "tool_args_match": (lambda arg, name: _tool_args(_text(arg), name), "needs"),
    "max_calls": (lambda arg, name: _max_calls(_text(arg), name), "needs"),
    "approval_present": (lambda arg, name: ApprovalPresent(_text(arg), name=name), "needs"),
    "no_tool": (lambda arg, name: ToolNotCalled(_text(arg), name=name), "needs"),
    "delegated": (lambda arg, name: DelegatedTo(_text(arg), name=name), "needs"),
    "handoff": (lambda arg, name: HandedOffTo(_text(arg), name=name), "needs"),
    "verdict": (lambda arg, name: VerdictIs(_text(arg), name=name), "needs"),
    "max_latency": (lambda arg, name: MaxLatency(_number(_text(arg)), name=name), "needs"),
    "max_tokens": (lambda arg, name: MaxTokens(_integer(_text(arg)), name=name), "needs"),
}


def _text(argument: str | None) -> str:
    assert argument is not None  # a "needs" kind is only built with its argument
    return argument


def parse_scorer(spec: str, *, judge_model: str | BaseChatModel | None = None) -> Scorer:
    """Build a built-in scorer from its specification (see the module table), named after it.

    Raises:
        EvalError: If the spec is unknown or its argument is invalid.

    Example:
        ```python
        scorers = [parse_scorer("delegated:billing_specialist"), parse_scorer("max_tokens:5000")]
        ```
    """

    text = spec.strip()
    kind, separator, argument = text.partition(":")
    kind, argument = kind.strip().lower(), argument.strip()
    try:
        if kind == "judge":
            if not argument:
                raise EvalError("A judge spec needs criteria: 'judge:<criteria>'")
            return ModelJudge(argument, model=judge_model)
        factory, allows = _SCORERS.get(kind, (None, "none"))
        if factory is not None and not separator and allows in ("none", "either"):
            return factory(None, text)
        if factory is not None and separator and allows in ("needs", "either"):
            if not argument:
                raise EvalError(f"Scorer spec {spec!r} needs a value after ':'")
            return factory(argument, text)
    except (ValueError, re.error) as exc:
        raise EvalError(f"Invalid scorer spec {spec!r}: {exc}") from None
    known = sorted(
        {
            *(k for k, (_, allows) in _SCORERS.items() if allows != "needs"),
            *(f"{k}:<value>" for k, (_, allows) in _SCORERS.items() if allows != "none"),
            "judge:<criteria>",
        }
    )
    raise EvalError(f"Unknown scorer spec {spec!r}; use one of: {', '.join(known)}")


def _tool_args(argument: str, name: str) -> Scorer:
    tool, separator, text = argument.partition(":")
    if not separator or not tool.strip() or not text.strip():
        raise ConfigurationError(
            "write tool_args_match:<tool>:<mapping>, e.g. tool_args_match:send:{to: a}"
        )
    try:
        wanted = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ConfigurationError(f"{text.strip()!r} is not a YAML or JSON mapping") from None
    if not isinstance(wanted, Mapping) or not wanted:
        raise ConfigurationError(f"{text.strip()!r} is not a mapping of arguments")
    return ToolArgsMatch(tool.strip(), wanted, name=name)


def _max_calls(argument: str, name: str) -> Scorer:
    tool, _, limit = argument.rpartition(":")
    return MaxToolCalls(_integer(limit), tool=tool.strip() or None, name=name)


def _number(argument: str) -> float:
    try:
        value = float(argument.replace("_", ""))
    except ValueError:
        raise ConfigurationError(f"{argument!r} is not a number") from None
    if not math.isfinite(value):
        raise ConfigurationError(f"{argument!r} is not a finite number")
    return value


def _integer(argument: str) -> int:
    _number(argument)
    digits = argument.replace("_", "")
    if not digits.isascii() or not digits.isdigit():
        raise ConfigurationError(f"{argument!r} is not a whole number")
    return int(digits)


__all__ = ["parse_scorer"]
