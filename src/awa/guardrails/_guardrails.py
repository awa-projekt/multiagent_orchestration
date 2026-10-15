"""Deterministic guardrails: synchronous Python checks that accept, warn about or reject a value.

A check reports a failure as a `GuardrailResult`; a check that raises blocks (fail closed).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence, Sized
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from awa._internal.util import preview
from awa.contracts.events import GuardrailBlockedEvent
from awa.errors import AwaError, ConfigurationError
from awa.kernel.output import schema_problems
from awa.kernel.redaction import (
    PII_PATTERNS,
    Redactor,
    RegexRedactor,
    fold_text,
    redact_keys,
    redact_mapping,
    redact_secrets,
    strip_invisible,
)
from awa.runtime.context import current_run_context

Severity = Literal["block", "warn"]
"""How serious a failed check is."""

GuardrailStage = Literal["input", "output"]
"""Whether a result was produced by input or output guardrails."""

PIIMode = Literal["block", "redact", "warn"]
"""What `PIIGuardrail` does when it finds personal data."""

OnFail = Literal["block", "retry"]
"""What a blocking output check does first: block at once, or, as an agent's output guardrail,
send the reason back to the model for another answer (up to the agent's `output_retries`)."""

_EVENT_REASON_CHARS = 300


class GuardrailResult(BaseModel):
    """Outcome of one guardrail check or of a chain of checks.

    Attributes:
        severity: `"block"` or `"warn"`; only meaningful when `passed` is false.
        stage: `"input"` or `"output"` when produced by `run_input_guardrails` /
            `run_output_guardrails`.
        replacement: A sanitized candidate to use instead of the original; `None` keeps it.
    """

    passed: bool
    reason: str | None = None
    severity: Severity = "block"
    guardrail_name: str = ""
    stage: GuardrailStage | None = None
    replacement: Any | None = None

    @property
    def blocked(self) -> bool:
        """Whether the candidate failed with severity `"block"`."""

        return not self.passed and self.severity == "block"


@runtime_checkable
class Guardrail(Protocol):
    """A named, deterministic check configured in its constructor.

    A failed check returns a `GuardrailResult`; a guardrail that raises blocks the candidate (fail
    closed), with a reason naming the exception type, never its message.
    """

    name: str

    def check(self, candidate: Any) -> GuardrailResult:
        """Validate `candidate`."""
        ...


class GuardrailTripwireError(AwaError):
    """Raised by the `run_*guardrails` functions with `raise_on_block=True` when a guardrail blocks.

    Attributes:
        result: The blocking `GuardrailResult`.
    """

    def __init__(self, result: GuardrailResult) -> None:
        self.result = result
        super().__init__(
            f"guardrail {result.guardrail_name!r} blocked the candidate: {result.reason}"
        )


def retries_on_fail(guardrail: Guardrail) -> bool:
    """Whether a failure of `guardrail` is first sent back to the model (`on_fail="retry"`)."""

    return getattr(guardrail, "on_fail", "block") == "retry"


def _on_fail(value: str) -> OnFail:
    if value not in ("block", "retry"):
        raise ConfigurationError(f"on_fail must be 'block' or 'retry', got {value!r}")
    return value  # type: ignore[return-value]


def _blocked(name: str, reason: str) -> GuardrailResult:
    return GuardrailResult(passed=False, reason=reason, severity="block", guardrail_name=name)


def _passed(name: str) -> GuardrailResult:
    return GuardrailResult(passed=True, guardrail_name=name)


def run_guardrails(
    candidate: Any, guardrails: Sequence[Guardrail], *, raise_on_block: bool = False
) -> GuardrailResult:
    """Run `guardrails` against `candidate` in order and return the combined result.

    The first blocking failure wins, else the first warning, else a pass (also for none). A returned
    `replacement` is what later guardrails check, and a non-blocking result carries the final one.

    Raises:
        GuardrailTripwireError: If `raise_on_block` is set and a guardrail blocks.

    Example:
        ```python
        result = run_guardrails(answer, [LengthGuardrail(max_length=2000), BlocklistGuardrail(["password"])])
        ```
    """

    current = candidate
    replaced = False
    first_warning: GuardrailResult | None = None
    for guardrail in guardrails:
        try:
            result = guardrail.check(current)
        except Exception as exc:  # noqa: BLE001 - a check that cannot decide blocks
            name = getattr(guardrail, "name", type(guardrail).__name__)
            result = _blocked(name, f"the check raised {type(exc).__name__}")
        if result.blocked:
            if raise_on_block:
                raise GuardrailTripwireError(result)
            return result
        if result.replacement is not None:
            current, replaced = result.replacement, True
        if not result.passed and first_warning is None:
            first_warning = result
    outcome = first_warning if first_warning is not None else _passed("run_guardrails")
    return outcome.model_copy(update={"replacement": current}) if replaced else outcome


def run_input_guardrails(
    candidate: Any,
    guardrails: Sequence[Guardrail],
    *,
    raise_on_block: bool = False,
    tool: str | None = None,
) -> GuardrailResult:
    """`run_guardrails` on an input, with stage `"input"`; a block inside a run emits a `GuardrailBlockedEvent`.

    Args:
        tool: The tool whose arguments are checked, if any; the event then has stage `"tool"`.

    Raises:
        GuardrailTripwireError: If `raise_on_block` is set and a guardrail blocks.
    """

    return _run_staged(candidate, guardrails, "input", raise_on_block, tool)


def run_output_guardrails(
    candidate: Any,
    guardrails: Sequence[Guardrail],
    *,
    raise_on_block: bool = False,
    tool: str | None = None,
) -> GuardrailResult:
    """`run_guardrails` on an output, with stage `"output"`; a block inside a run emits a `GuardrailBlockedEvent`.

    Args:
        tool: The tool whose result is checked, if any; the event then has stage `"tool"`.

    Raises:
        GuardrailTripwireError: If `raise_on_block` is set and a guardrail blocks.
    """

    return _run_staged(candidate, guardrails, "output", raise_on_block, tool)


def _run_staged(
    candidate: Any,
    guardrails: Sequence[Guardrail],
    stage: GuardrailStage,
    raise_on_block: bool,
    tool: str | None,
) -> GuardrailResult:
    result = run_guardrails(candidate, guardrails)
    result = result.model_copy(update={"stage": stage})
    if result.blocked:
        _report_block(result, stage, tool)
        if raise_on_block:
            raise GuardrailTripwireError(result)
    return result


def _report_block(result: GuardrailResult, stage: GuardrailStage, tool: str | None) -> None:
    """Queue a `GuardrailBlockedEvent` when running inside a run."""

    ctx = current_run_context()
    if ctx is None:
        return
    ctx.events.emit_later(
        GuardrailBlockedEvent(
            guardrail=result.guardrail_name,
            stage="tool" if tool is not None else stage,
            reason=preview(redact_secrets(result.reason or ""), _EVENT_REASON_CHARS),
            tool=tool,
            **ctx.event_fields(),
        )
    )


class LengthGuardrail:
    """Blocks a candidate whose `str()` length is outside `[min_length, max_length]`.

    Args:
        max_length: Maximum length, or `None` for no upper bound.
        on_fail: `"block"` (default) or `"retry"` (see `OnFail`).
    """

    def __init__(
        self,
        *,
        min_length: int = 0,
        max_length: int | None = None,
        name: str = "length",
        on_fail: OnFail = "block",
    ) -> None:
        if max_length is not None and max_length < min_length:
            raise ConfigurationError("max_length must be greater than or equal to min_length")
        self.name = name
        self.on_fail = _on_fail(on_fail)
        self._min_length = min_length
        self._max_length = max_length

    def check(self, candidate: Any) -> GuardrailResult:
        """Check the length of `str(candidate)`."""

        length = len(str(candidate))
        if length < self._min_length:
            return _blocked(
                self.name, f"length {length} is below the minimum of {self._min_length}"
            )
        if self._max_length is not None and length > self._max_length:
            return _blocked(self.name, f"length {length} exceeds the maximum of {self._max_length}")
        return _passed(self.name)


class BlocklistGuardrail:
    """Blocks a candidate whose `str()` contains any of `terms`, ignoring case and form.

    Compatibility forms fold to plain letters, invisible format characters are dropped and case is
    folded; look-alike letters of another script are not, so a blocklist is a floor, not a guarantee.

    Raises:
        ConfigurationError: If `terms` is a single string or a term is empty.
    """

    def __init__(self, terms: Sequence[str], *, name: str = "blocklist") -> None:
        if isinstance(terms, str):
            raise ConfigurationError("terms must be a sequence of strings, not one string")
        self.name = name
        self._terms = [fold_text(term) for term in terms]
        if not all(self._terms):
            raise ConfigurationError("a blocklist term must not be empty")

    def check(self, candidate: Any) -> GuardrailResult:
        """Check `str(candidate)` for forbidden terms."""

        text = fold_text(str(candidate))
        hit = next((term for term in self._terms if term in text), None)
        if hit is not None:
            return _blocked(self.name, f"contains blocked term: {hit!r}")
        return _passed(self.name)


class SchemaGuardrail:
    """Blocks a candidate that does not validate against a Pydantic model.

    A `BaseModel` candidate is dumped first, so a compatible model validates. The reason lists
    failing fields and error types, never values or undeclared keys (shown as `<field>`).

    Args:
        on_fail: `"retry"` (default: the model answers again) or `"block"`.
    """

    def __init__(
        self, schema: type[BaseModel], *, name: str = "schema", on_fail: OnFail = "retry"
    ) -> None:
        self.name = name
        self.on_fail = _on_fail(on_fail)
        self._schema = schema

    def check(self, candidate: Any) -> GuardrailResult:
        """Validate `candidate` with `schema.model_validate`."""

        payload = candidate.model_dump() if isinstance(candidate, BaseModel) else candidate
        try:
            self._schema.model_validate(payload)
        except ValidationError as exc:
            problems = schema_problems(self._schema, exc)
            return _blocked(self.name, f"failed schema validation: {problems}")
        return _passed(self.name)


class NonEmptyFieldsGuardrail:
    """Blocks a Pydantic model whose named fields are `None` or empty containers or text.

    `0` and `False` pass; naming a field the model does not have fails.

    Args:
        on_fail: `"retry"` (default: the model answers again) or `"block"`.
    """

    def __init__(
        self,
        field_names: Sequence[str],
        *,
        name: str = "non_empty_fields",
        on_fail: OnFail = "retry",
    ) -> None:
        self.name = name
        self.on_fail = _on_fail(on_fail)
        self._field_names = tuple(field_names)

    def check(self, candidate: Any) -> GuardrailResult:
        """Check the named fields of `candidate`."""

        if not isinstance(candidate, BaseModel):
            return _blocked(
                self.name, f"candidate is not a BaseModel (got {type(candidate).__name__})"
            )
        model_fields = type(candidate).model_fields
        problems: list[str] = []
        for field_name in self._field_names:
            if field_name not in model_fields:
                problems.append(f"{field_name!r} is not a field on {type(candidate).__name__}")
                continue
            value = getattr(candidate, field_name)
            if value is None or (isinstance(value, Sized) and len(value) == 0):
                problems.append(f"{field_name!r} is missing or empty")
        if problems:
            return _blocked(self.name, "; ".join(problems))
        return _passed(self.name)


class MustIncludeGuardrail:
    """Blocks a candidate whose text (structured ones as JSON) lacks any of `terms`.

    The reason names the missing terms, so a retry knows what to add.

    Args:
        case_sensitive: Match the case exactly (default: ignore case).
        on_fail: `"retry"` (default: the model answers again) or `"block"`.

    Raises:
        ConfigurationError: If `terms` is a single string.
    """

    def __init__(
        self,
        terms: Sequence[str],
        *,
        case_sensitive: bool = False,
        name: str = "must_include",
        on_fail: OnFail = "retry",
    ) -> None:
        if isinstance(terms, str):
            raise ConfigurationError("terms must be a sequence of strings, not one string")
        self.name = name
        self.on_fail = _on_fail(on_fail)
        self._terms = tuple(term for term in terms if term)
        self._case_sensitive = case_sensitive

    def check(self, candidate: Any) -> GuardrailResult:
        """Check that `candidate` contains every term."""

        text = candidate.model_dump_json() if isinstance(candidate, BaseModel) else str(candidate)
        haystack = text if self._case_sensitive else text.lower()
        missing = [
            term
            for term in self._terms
            if (term if self._case_sensitive else term.lower()) not in haystack
        ]
        if missing:
            listed = ", ".join(repr(term) for term in missing)
            return _blocked(self.name, f"the answer must include {listed}, exactly as written")
        return _passed(self.name)


class PIIGuardrail:
    """Detects personal data (and credentials) and blocks, redacts or warns.

    Strings, mappings, lists and models (through `model_dump()`) are checked; the reason names the
    kinds found and how often, never the values. `mode="redact"` passes with `replacement` set to
    the redacted candidate (a model that no longer validates is blocked instead); `mode="warn"`
    passes unchanged.

    Args:
        mode: `"block"` (default), `"redact"` or `"warn"`.
        redactor: What counts as personal data; defaults to `RegexRedactor.pii(kinds=kinds)`. A
            redactor that is not a `RegexRedactor` reports the kind as `"pii"`.
        kinds: Built-in PII kinds to look for (`{"email", "iban"}`); only used without `redactor`.

    Raises:
        ConfigurationError: If `mode` or a kind is unknown.

    Example:
        ```python
        agent = Agent("support", input_guardrails=[PIIGuardrail(kinds={"credit_card", "iban"})])
        ```
    """

    def __init__(
        self,
        *,
        mode: PIIMode = "block",
        redactor: Redactor | None = None,
        kinds: Collection[str] | None = None,
        name: str = "pii",
    ) -> None:
        if mode not in ("block", "redact", "warn"):
            raise ConfigurationError(f"mode must be 'block', 'redact' or 'warn', got {mode!r}")
        if kinds is not None:
            unknown = set(kinds) - {pattern.kind for pattern in PII_PATTERNS}
            if unknown:
                known = ", ".join(sorted(pattern.kind for pattern in PII_PATTERNS))
                raise ConfigurationError(
                    f"unknown PII kinds: {', '.join(sorted(unknown))}; the kinds are {known}"
                )
        self.name = name
        self.mode: PIIMode = mode
        self._redactor = redactor if redactor is not None else RegexRedactor.pii(kinds=kinds)

    def check(self, candidate: Any) -> GuardrailResult:
        """Look for personal data in `candidate`."""

        model = isinstance(candidate, BaseModel)
        data = strip_invisible(candidate.model_dump() if model else candidate)
        if not isinstance(data, str | Mapping | list | tuple):
            data = strip_invisible(str(data))
        counts = self._findings(data, keys=not model)
        if not counts:
            return _passed(self.name)
        found = ", ".join(f"{kind} ({count})" for kind, count in sorted(counts.items()))
        if self.mode == "warn":
            return GuardrailResult(
                passed=False,
                reason=f"contains personal data: {found}",
                severity="warn",
                guardrail_name=self.name,
            )
        if self.mode == "block":
            return _blocked(self.name, f"contains personal data: {found}")
        redacted = redact_mapping(
            data if model else redact_keys(data, self._redactor), self._redactor, sensitive_keys=()
        )
        if self._findings(redacted, keys=not model):
            return _blocked(self.name, f"contains personal data that cannot be redacted: {found}")
        if model:
            try:
                redacted = type(candidate).model_validate(redacted)
            except ValidationError:
                return _blocked(
                    self.name, f"contains personal data that cannot be redacted: {found}"
                )
        return GuardrailResult(
            passed=True,
            reason=f"redacted personal data: {found}",
            guardrail_name=self.name,
            replacement=redacted,
        )

    def _findings(self, data: Any, *, keys: bool) -> Counter[str]:
        counts: Counter[str] = Counter()
        for text in _strings(data, keys=keys):
            if isinstance(self._redactor, RegexRedactor):
                counts.update(match.kind for match in self._redactor.find(text))
            elif self._redactor.redact(text) != text:
                counts["pii"] += 1
        return counts


def _strings(value: Any, *, keys: bool) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)
    elif isinstance(value, bytes | bytearray):
        yield bytes(value).decode("utf-8", "replace")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if keys and isinstance(key, str):
                yield key
            yield from _strings(item, keys=keys)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _strings(item, keys=keys)
