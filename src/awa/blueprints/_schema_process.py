"""Blueprint models for the optional `process:` section: steps with an agent, documents and checks.

A step may be tried again (`retry`), continue at a later step or ask a person after its last
try (`after_last_try`), and run only when a condition holds (`when`).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator

from awa.blueprints._automation import _Duration
from awa.blueprints._schema_limits import duration_seconds
from awa.blueprints._schema_spec import _Spec
from awa.workflows._step_flow import MAX_ATTEMPTS, AfterLastTry, RetryOn, RunCondition, StepRetry


class _CheckBase(_Spec):
    """Fields every check has; the default `id` is `"<step>.<kind>.<n>"`."""

    id: str | None = Field(default=None, max_length=80)
    title: str = Field(default="", max_length=120)


class SchemaCheckBlueprint(_CheckBase):
    """The step's output must match `schema` (default: the output schema of the step's agent)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["schema"]
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class RuleCheckBlueprint(_CheckBase):
    """A rule that says when the step has failed.

    Attributes:
        fail_when: Expression over the output fields and `text`; when it is true, or cannot be
            evaluated, the step fails. Same language as the `when` of an approval.
        message: What is wrong when the rule fires; shown to the person reading the result.
    """

    kind: Literal["rule"]
    fail_when: str = Field(min_length=1, max_length=1000)
    message: str = Field(default="", max_length=500)


class ApprovalCheckBlueprint(_CheckBase):
    """A person approves the step's result before the next step starts.

    Attributes:
        when: Expression over the output fields and `text` that says when to ask; default always.
        by: Role allowed to decide. Recorded and shown; the library does not check who answers.
    """

    kind: Literal["approval"]
    when: str | None = Field(default=None, max_length=1000)
    reason: str = Field(default="", max_length=500)
    by: str | None = Field(default=None, max_length=80)


StepCheckBlueprint: TypeAlias = Annotated[
    SchemaCheckBlueprint | RuleCheckBlueprint | ApprovalCheckBlueprint, Field(discriminator="kind")
]
"""A check of a step, discriminated on `kind`: `schema`, `rule` or `approval`."""


class StepRetryBlueprint(_Spec):
    """`retry:` of a step: the shape of `automation.retry`, plus `on`; `retry: 3` is the short form.

    Attributes:
        max_attempts: Tries in total, the first included; 1 to 5. Also `attempts` or `tries`.
        initial: Wait before the second try (`"10s"`, `"2m"` or seconds); also `wait`.
        max_delay: Longest wait; default ten times `initial`.
        on: What is tried again: `error`, `check` (the next try gets the check's message) or both.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_attempts: int = Field(
        default=3,
        ge=1,
        le=MAX_ATTEMPTS,
        validation_alias=AliasChoices("max_attempts", "attempts", "tries"),
    )
    initial: _Duration = Field(default="1s", validation_alias=AliasChoices("initial", "wait"))
    backoff: float = Field(default=2.0, ge=1.0)
    max_delay: _Duration = None
    jitter: float = Field(default=0.0, ge=0.0, le=1.0)
    on: list[RetryOn] = Field(
        default_factory=lambda: list[RetryOn](["error", "check"]), min_length=1
    )

    @model_validator(mode="before")
    @classmethod
    def _count_only(cls, data: Any) -> Any:
        if isinstance(data, int) and not isinstance(data, bool):
            return {"max_attempts": data}
        if isinstance(data, dict) and True in data and "on" not in data:
            # YAML 1.1 reads a bare `on:` key as true; it is meant as the word.
            data = {("on" if key is True else key): value for key, value in data.items()}
        return data

    @field_validator("initial", "max_delay")
    @classmethod
    def _duration(cls, value: _Duration) -> _Duration:
        duration_seconds(
            value, "retry wait"
        )  # raises ValueError for anything that is not a duration
        return value

    def step_retry(self) -> StepRetry:
        """The `StepRetry` the workflow uses."""

        return StepRetry(
            max_attempts=self.max_attempts,
            initial=duration_seconds(self.initial) or 0.0,
            backoff=self.backoff,
            max_delay=duration_seconds(self.max_delay),
            jitter=self.jitter,
            on=frozenset(self.on),
        )


class ProcessStepBlueprint(_Spec):
    """One step of the process: an agent, the documents it gets here, and the step's checks.

    Attributes:
        name: Step id; defaults to the agent name, with `_2`, `_3` ... for a repeated agent.
        documents: Names of `data:` entries given in this step's input.
        checks: Run after the agent answered: `schema` and `rule` checks, then the step's
            special cases, `approval` checks last.
        retry: How often the step is tried; not allowed for an agent that writes or sends.
        after_last_try: `stop` (the run fails), `ask` (a person decides) or `go: <step>`
            (continue at a later step; not for an agent that writes or sends).
        when: Condition over the request's JSON fields and earlier outputs; the step is
            `skipped` when it is false, and a missing or unclear value stops the run.
    """

    name: str = ""
    title: str = Field(default="", max_length=80)
    agent: str = ""
    documents: list[str] = Field(default_factory=list)
    checks: list[StepCheckBlueprint] = Field(default_factory=list)
    retry: StepRetryBlueprint | None = None
    after_last_try: str = Field(default="stop", max_length=80)
    when: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def _agent_name_only(cls, data: Any) -> Any:
        return {"agent": data} if isinstance(data, str) else data

    @field_validator("after_last_try", mode="before")
    @classmethod
    def _last_try_words(cls, value: Any) -> Any:
        if isinstance(value, dict) and set(value) == {"go"}:
            value = f"go: {value['go']}"
        if not isinstance(value, str):
            raise ValueError("after_last_try is stop, ask or go: <step>")
        word, colon, step = value.strip().partition(":")
        word = word.strip().lower()
        if colon and word == "go":
            text = f"go: {step.strip()}"
        elif word in ("stop", "ask") and not colon:
            text = word
        else:
            raise ValueError(f"after_last_try is stop, ask or go: <step>, not {value!r}")
        _last_try(text)  # checks the step name
        return text

    @field_validator("when")
    @classmethod
    def _condition_reads(cls, value: str | None) -> str | None:
        if value is not None:
            RunCondition(value)  # raises ValueError when it cannot be read
        return value

    def last_try(self) -> AfterLastTry:
        """`after_last_try` as the workflow's `AfterLastTry`."""

        return _last_try(self.after_last_try)

    def run_condition(self) -> RunCondition | None:
        """`when` as the workflow's `RunCondition`, or `None`."""

        return RunCondition(self.when) if self.when is not None else None


class ProcessBlueprint(_Spec):
    """The `process:` section: steps run one after the other, each checked; a bare list is the short form."""

    steps: list[ProcessStepBlueprint] = Field(default_factory=list)
    carry: Literal["previous", "all"] = "previous"

    @model_validator(mode="before")
    @classmethod
    def _bare_list(cls, data: Any) -> Any:
        return {"steps": data} if isinstance(data, list) else data

    @model_validator(mode="after")
    def _name_the_steps(self) -> ProcessBlueprint:
        """Give a step without a name its agent's name (`_2`, `_3` ... when it repeats)."""

        used = {step.name for step in self.steps if step.name}
        for position, step in enumerate(self.steps, 1):
            if step.name:
                continue
            base = step.agent or f"step_{position}"
            name, number = base, 1
            while name in used:
                number += 1
                name = f"{base}_{number}"
            used.add(name)
            step.name = name
        return self


def _last_try(text: str) -> AfterLastTry:
    word, _, step = text.partition(":")
    if word == "go":
        return AfterLastTry("go", step.strip())
    return AfterLastTry("ask" if word == "ask" else "stop")


def describe_check(check: StepCheckBlueprint) -> str:
    """One check in plain words, for `awa show` and the diagram notes."""

    if isinstance(check, SchemaCheckBlueprint):
        return check.title or "the output has the right fields"
    if isinstance(check, RuleCheckBlueprint):
        return check.title or f"fails when {check.fail_when}: {check.message}"
    asked = f"a person decides when {check.when}" if check.when else "a person always decides"
    role = f" ({check.by})" if check.by else ""
    why = f": {check.reason}" if check.reason else ""
    return check.title or f"{asked}{role}{why}"


def check_id(step: ProcessStepBlueprint, index: int) -> str:
    """The id of check `index` of `step`: its own `id`, else `"<step>.<kind>.<n>"`."""

    check = step.checks[index]
    if check.id:
        return check.id
    same_kind = sum(1 for other in step.checks[: index + 1] if other.kind == check.kind)
    return f"{step.name}.{check.kind}.{same_kind}"
