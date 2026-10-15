"""Output validation with one retry budget: a rejected answer goes back to the model.

Schema mismatches and validator rejections (`ModelRetry`) share `max_retries`; past it
`OutputRejected` is raised. The count lives in the agent state (`RETRY_KEY`), so each run and
each new input has its own budget. The message to the model quotes the reasons, never the output.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Annotated, Any, Literal, NotRequired, get_args

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain.agents.structured_output import (
    AutoStrategy,
    ProviderStrategy,
    StructuredOutputValidationError,
    ToolStrategy,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

RETRY_KEY = "awa_output_retries"
"""Agent-state key of the retries the current turn has used."""

RetryKind = Literal["schema", "validator"]
"""What was rejected: an answer that does not match its schema, or one a validator refused."""

Validator = Callable[..., Any]
"""`fn(output)` or `fn(output, runtime)`; sync or async; returns `None` or the output."""

_PROBLEM_CHARS = 500
_SOURCE_CHARS = 1500


class ModelRetry(Exception):
    """Raise in an output validator (or in a tool) to send `message` back to the model."""


class OutputRejected(Exception):
    """The output was rejected and the retry budget is used up.

    Attributes:
        problems: The reasons of the last rejection.
        kind: `"schema"` or `"validator"`.
        causes: The `ModelRetry` errors (or the schema error) behind the problems.
    """

    def __init__(
        self,
        problems: Sequence[str],
        attempts: int,
        kind: RetryKind,
        causes: Sequence[BaseException] = (),
    ) -> None:
        self.problems = list(problems)
        self.attempts = attempts
        self.kind: RetryKind = kind
        self.causes = tuple(causes)
        super().__init__("; ".join(self.problems))


def structured_output_tool_names(response_format: Any) -> frozenset[str]:
    """Names of the tools `create_agent` binds to return `response_format`; empty for provider-native."""

    if response_format is None or isinstance(response_format, ProviderStrategy):
        return frozenset()
    strategy: ToolStrategy[Any]
    if isinstance(response_format, ToolStrategy):
        strategy = response_format
    elif isinstance(response_format, AutoStrategy):
        strategy = ToolStrategy(schema=response_format.schema)
    else:
        strategy = ToolStrategy(schema=response_format)
    return frozenset(spec.name for spec in strategy.schema_specs)


def schema_problems(schema: type[BaseModel], error: ValidationError) -> str:
    """The failures of `error` as text: field paths and error types, never the candidate's values."""

    known: set[str] = set()
    pending, seen = [schema], set()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        known.update(model.model_fields)
        pending.extend(
            n for info in model.model_fields.values() for n in _models_in(info.annotation)
        )
    return "; ".join(
        ".".join(str(p) if isinstance(p, int) or p in known else "<field>" for p in e["loc"])
        + f": {e['type']}"
        for e in error.errors(include_input=False, include_url=False)
    )


def _schema_model(response_format: Any) -> type[BaseModel] | None:
    schema = response_format
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        schema = getattr(response_format, "schema", None)
    return schema if isinstance(schema, type) and issubclass(schema, BaseModel) else None


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for part in get_args(annotation):
        yield from _models_in(part)


class OutputRetryState(AgentState[Any]):
    """The agent state with the turn's retry count, kept in every checkpoint."""

    awa_output_retries: NotRequired[Annotated[int, PrivateStateAttr]]


class OutputValidatorMiddleware(AgentMiddleware):
    """Sends a rejected answer back to the model, up to `max_retries` times per turn.

    Example:
        ```python
        def positive(report: Report) -> Report:
            if report.total <= 0:
                raise ModelRetry("total must be positive")
            return report

        middleware = OutputValidatorMiddleware([positive], max_retries=2, response_format=Report)
        ```

    Args:
        validators: `fn(output)` or `fn(output, runtime)`, sync or async, run in order (all of
            them, so the model hears every problem); raise `ModelRetry(reason)` to reject, return
            a value to replace the output.
        max_retries: Most retries per turn, schema errors and rejections together; 0 raises on the
            first rejection.
        response_format: With one, validators check the structured object; without, the text.
        spend_step: Called before each in-call retry of a provider-native structured answer;
            `False` when the step limit leaves no model call for it.
        on_retry: Called as `on_retry(check, kind, attempt, max_retries)` for every retry.
    """

    state_schema = OutputRetryState

    def __init__(
        self,
        validators: Sequence[Validator],
        *,
        max_retries: int,
        response_format: Any = None,
        spend_step: Callable[[], bool] | None = None,
        on_retry: Callable[[str, RetryKind, int, int], None] | None = None,
    ) -> None:
        super().__init__()
        if max_retries < 0:
            raise ValueError(f"output_retries must be at least 0, got {max_retries}")
        self._validators = list(validators)
        self._max_retries = max_retries
        self._structured = response_format is not None
        self._schema = _schema_model(response_format)
        self._tool_names = structured_output_tool_names(response_format)
        self._spend_step = spend_step
        self._on_retry = on_retry

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """New input starts a turn: its retry count starts at zero."""

        return {RETRY_KEY: 0}

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        """Retry a structured answer that does not match its schema.

        Provider-native output is retried inside the call (the repair turn stays out of the history); a
        failed tool-call answer is retried in the next turn and counted here once.
        """

        saved = used = _used(request.state)
        while True:
            try:
                response = await handler(request)
                break
            except StructuredOutputValidationError as exc:
                problem = self._schema_problem(exc.source)
                if used >= self._max_retries or not self._may_spend_step():
                    raise OutputRejected([problem], used, "schema", [exc]) from exc
                used += 1
                self._retrying("schema", "schema", used)
                request = request.override(
                    messages=[*request.messages, exc.ai_message, _schema_feedback(problem)]
                )
        errors = self._tool_mode_errors(response)
        if errors:
            if used >= self._max_retries:
                raise OutputRejected(errors, used, "schema")
            used += 1
            self._retrying("schema", "schema", used)
        if used == saved:
            return response
        if isinstance(response, AIMessage):
            response = ModelResponse(result=[response])
        return ExtendedModelResponse(
            model_response=response, command=Command(update={RETRY_KEY: used})
        )

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Run the validators on the final answer; on a rejection, ask for another answer."""

        if not self._validators:
            return None
        original = self._candidate(state)
        if original is None:
            return None
        output = original
        problems: list[str] = []
        causes: list[BaseException] = []
        failed: list[str] = []
        for validator in self._validators:
            try:
                result = _call(validator, output, runtime)
                if inspect.isawaitable(result):
                    result = await result
            except ModelRetry as exc:
                problems.append(_clip(str(exc), _PROBLEM_CHARS))
                causes.append(exc)
                failed.append(_name(validator))
                continue
            if result is not None:
                output = result
        if not problems:
            return self._replacement(state, original, output)
        used = _used(state)
        if used >= self._max_retries:
            raise OutputRejected(problems, used, "validator", causes)
        used += 1
        self._retrying(",".join(failed), "validator", used)
        update: dict[str, Any] = {
            "messages": [_validator_feedback(problems, used, self._max_retries, state)],
            RETRY_KEY: used,
            "jump_to": "model",
        }
        if self._structured:
            update["structured_response"] = None  # no stale value if the retry fails
        return update

    def _may_spend_step(self) -> bool:
        return self._spend_step is None or self._spend_step()

    def _retrying(self, check: str, kind: RetryKind, attempt: int) -> None:
        if self._on_retry is not None:
            self._on_retry(check, kind, attempt, self._max_retries)

    def _schema_problem(self, error: Exception) -> str:
        """Why a structured answer was rejected, naming fields but never the answer's values."""

        invalid: BaseException | None = error
        while invalid is not None and not isinstance(invalid, ValidationError):
            invalid = invalid.__cause__ or invalid.__context__
        if invalid is None:
            return _clip(str(error), _PROBLEM_CHARS)
        if self._schema is not None:
            return schema_problems(self._schema, invalid)
        return "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['type']}"
            for item in invalid.errors(include_input=False, include_url=False)
        )

    def _tool_mode_errors(self, response: Any) -> list[str]:
        """The error texts of a tool-call structured answer that did not parse."""

        if not self._tool_names or isinstance(response, AIMessage):
            return []
        if response.structured_response is not None:
            return []
        return [
            str(message.text)
            for message in response.result
            if isinstance(message, ToolMessage) and message.name in self._tool_names
        ]

    def _candidate(self, state: Any) -> Any:
        if self._structured:
            return state.get("structured_response")
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        return str(last.text)

    def _replacement(self, state: Any, original: Any, output: Any) -> dict[str, Any] | None:
        """The state update for a validator that changed the output, else `None`."""

        if output is original:
            return None
        if self._structured:
            return {"structured_response": output}
        return {"messages": [state["messages"][-1].model_copy(update={"content": str(output)})]}


def _used(state: Any) -> int:
    value = state.get(RETRY_KEY) if isinstance(state, dict) else None
    return value if isinstance(value, int) and value > 0 else 0


def _call(validator: Validator, output: Any, runtime: Any) -> Any:
    return validator(output, runtime) if takes_runtime(validator) else validator(output)


def takes_runtime(validator: Validator) -> bool:
    """Whether `validator` is called as `fn(output, runtime)` (two positional parameters)."""

    try:
        parameters = inspect.signature(validator).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [
        p
        for p in parameters
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2 or any(
        p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters
    )


def _name(validator: Validator) -> str:
    return str(getattr(validator, "__name__", None) or type(validator).__name__)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _schema_feedback(problem: str) -> HumanMessage:
    return HumanMessage(
        content=(
            "Your response does not match the required output format. Problems:\n"
            f"{_clip(problem, _SOURCE_CHARS)}\n"
            "Return the complete response again in the required format, with every "
            "required field."
        )
    )


def _validator_feedback(
    problems: Sequence[str], attempt: int, max_retries: int, state: Any
) -> HumanMessage:
    messages = state.get("messages") or []
    anchor = getattr(messages[-1], "id", None) if messages else None
    return HumanMessage(
        content=(
            f"Your answer failed {len(problems)} check(s) (retry {attempt} of {max_retries}):\n"
            + "\n".join(f"- {problem}" for problem in problems)
            + "\nFix exactly these points and answer again in the same format; keep "
            "everything else."
        ),
        id=f"awa-output-check-{attempt}-{anchor}" if anchor else f"awa-output-check-{attempt}",
    )


__all__ = [
    "RETRY_KEY",
    "ModelRetry",
    "OutputRejected",
    "OutputRetryState",
    "OutputValidatorMiddleware",
    "RetryKind",
    "Validator",
    "schema_problems",
    "structured_output_tool_names",
    "takes_runtime",
]
