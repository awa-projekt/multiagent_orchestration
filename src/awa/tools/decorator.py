"""`@tool`: one decorator for the whole tool contract.

Options compile onto the existing wrappers: `approval` (`create_tool_approval_wrapper`),
`timeout`, `on_error`, `max_output_chars`, `frame_output` (`harden_tool`), `risk`
(`mark_tool_risk`), `idempotent` (`run_idempotent`) and `retries`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import inspect
import itertools
import json
import re
import time
import types
import typing
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, TypedDict, Unpack, overload

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from awa._internal.retry import backoff_delay, retry_on_transient
from awa._internal.upstream.tools import BaseTool, StructuredTool, create_schema_from_function
from awa.errors import ConfigurationError, UsageLimitExceededError
from awa.runtime.cache import tenant_of
from awa.runtime.context import RunContext, current_run_context
from awa.tools.approval import (
    ApprovalCondition,
    ApprovalPredicate,
    create_tool_approval_wrapper,
    takes_context,
)
from awa.tools.hardening import (
    OnError,
    ainvoke_tool,
    check_output_options,
    harden_tool,
    invoke_tool,
    positive_seconds,
    rewrap,
)
from awa.tools.idempotency import IdempotencyStore, InMemoryIdempotencyStore, run_idempotent
from awa.tools.risk import ToolRisk, declared_tool_risk, mark_tool_risk

TOOL_SPEC_METADATA_KEY = "awa_tool"
"""Key of a decorated tool's `ToolSpec` (as a JSON-friendly dict) in `BaseTool.metadata`."""

RetryOn = type[Exception] | tuple[type[Exception], ...] | Callable[[Exception], bool]
"""Which errors `retries` repeats: exception types, or a predicate on the error."""

IdempotencyKey = Callable[..., str]
"""`key(args)` or `key(args, ctx)`: the de-duplication key of a call."""

RiskSpec = ToolRisk | Mapping[str, bool | None] | Iterable[str] | str
"""A `ToolRisk`, a mapping of flags, or the names of the flags that are true."""

ApprovalMode = Literal["never", "always", "conditional"]

_MAX_BACKOFF = 30.0
_REMEMBERED_RESULTS = 1024
_SECTION = re.compile(
    r"^(Args|Arguments|Parameters|Params|Returns|Return|Raises|Yields|Examples?|Notes?|"
    r"Attributes|Warnings?|See Also|Todo):\s*$"
)
_DUPLICATE = (
    "An identical call to {tool!r} was already made (idempotency key {key}); it was not repeated."
)
"""Also returned while the first call runs or after it failed: a result is known only on success."""

_CONTENT_BLOCK_TYPES = frozenset(
    {"text", "image", "image_url", "file", "audio", "video", "tool_use", "reasoning"}
)


_current_idempotency_key: ContextVar[str | None] = ContextVar(
    "awa_tool_idempotency_key", default=None
)


def current_idempotency_key() -> str | None:
    """The idempotency key of the `idempotent` tool call currently running, or `None`.

    Pass it to the called system, so a repeat after a crash is de-duplicated there too.
    """

    return _current_idempotency_key.get()


@dataclass(frozen=True)
class ToolSpec:
    """The safety contract a tool was built with, kept in `metadata["awa_tool"]` (see `tool_spec`).

    Attributes:
        approval: `"always"`, `"conditional"` (a predicate or condition decides) or `"never"`.
        approval_when: The condition of a conditional approval given as an expression, else `None`.
        frame_output: Whether output is labeled as external data.
        injects_context: Whether the function receives the `RunContext` or `deps`.
    """

    approval: ApprovalMode = "never"
    approval_when: str | None = None
    timeout: float | None = None
    retries: int = 0
    idempotent: bool = False
    on_error: OnError = "return"
    max_output_chars: int | None = None
    frame_output: bool = False
    injects_context: bool = False

    def to_metadata(self) -> dict[str, Any]:
        """The spec as a JSON-friendly dict."""

        return dataclasses.asdict(self)

    @classmethod
    def from_metadata(cls, value: Mapping[str, Any]) -> ToolSpec:
        """Read a spec written by `to_metadata`; unknown keys are ignored."""

        names = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in names})


def tool_spec(tool: BaseTool) -> ToolSpec | None:
    """Return the `ToolSpec` recorded on `tool`, or `None` for a tool built elsewhere."""

    value = (tool.metadata or {}).get(TOOL_SPEC_METADATA_KEY)
    if isinstance(value, ToolSpec):
        return value
    return ToolSpec.from_metadata(value) if isinstance(value, Mapping) else None


class ToolOptions(TypedDict, total=False):
    """The keyword options of `tool` and `tool_method` (see `tool` for each one)."""

    name: str | None
    description: str | None
    approval: bool | str | ApprovalPredicate
    approval_options: Mapping[str, Any] | None
    risk: RiskSpec | None
    timeout: float | timedelta | None
    retries: int
    retry_on: RetryOn
    retry_backoff: float
    idempotent: bool | IdempotencyKey
    idempotency_store: IdempotencyStore | None
    on_error: OnError
    frame_output: bool | None
    max_output_chars: int | None
    return_direct: bool
    response_format: Literal["content", "content_and_artifact"]
    args_schema: type[BaseModel] | None
    tags: Iterable[str]
    metadata: Mapping[str, Any] | None


ToolTarget = Callable[..., Any] | BaseTool


@overload
def tool(func: ToolTarget, /, **options: Unpack[ToolOptions]) -> BaseTool: ...


@overload
def tool(
    name: str | None = None, /, **options: Unpack[ToolOptions]
) -> Callable[[ToolTarget], BaseTool]: ...


def tool(
    target: ToolTarget | str | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    approval: bool | str | ApprovalPredicate = False,
    approval_options: Mapping[str, Any] | None = None,
    risk: RiskSpec | None = None,
    timeout: float | timedelta | None = None,
    retries: int = 0,
    retry_on: RetryOn = retry_on_transient,
    retry_backoff: float = 0.5,
    idempotent: bool | IdempotencyKey = False,
    idempotency_store: IdempotencyStore | None = None,
    on_error: OnError = "return",
    frame_output: bool | None = None,
    max_output_chars: int | None = None,
    return_direct: bool = False,
    response_format: Literal["content", "content_and_artifact"] = "content",
    args_schema: type[BaseModel] | None = None,
    tags: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> BaseTool | Callable[[ToolTarget], BaseTool]:
    """Turn a function (sync or async) into a tool with awa's safety policies.

    Use it bare (`@tool`), with a name (`@tool("lookup")`) or with options (`@tool(timeout=10)`).
    Given a `BaseTool`, it returns a copy with the options applied. The schema comes from the type
    hints, the description from the docstring (`Args:` entries become field descriptions). A
    parameter annotated `RunContext[...]` (or an unannotated `ctx`) is hidden from the model and
    filled from the current run; a parameter named `deps` receives `ctx.deps`.

    Args:
        target: The function or tool, or the tool's name when used as `@tool("name")`.
        description: What the model reads; defaults to the docstring up to its first section.
        approval: `True` pauses every call; an expression such as `"amount >= 5000"` or a predicate
            `(args)` / `(args, ctx)` decides per call (see `create_tool_approval_wrapper`).
        approval_options: More keyword arguments for `create_tool_approval_wrapper`.
        risk: Risk flags: a `ToolRisk`, a mapping, or a list such as `["external_action"]`.
        timeout: Maximum seconds for a call, retries included.
        retries: Extra attempts after a transient error, with exponential backoff.
        retry_on: Exception types or a predicate; defaults to timeouts and connection errors, never
            awa's own decisions. Interrupts and usage limits are never retried.
        retry_backoff: Seconds before the first retry; doubled per retry, at most 30 seconds.
        idempotent: `True` runs identical calls (same run tree, tool and arguments) once; a function
            `key(args)` or `key(args, ctx)` sets the key. Idempotent tools run on the async path.
        idempotency_store: Where keys are claimed; defaults to a per-tool in-memory store.
        on_error: `"return"` (default) turns failures into tool text; `"raise"` propagates them.
        frame_output: Label the output as external data; `None` frames it for untrusted-input tools.
        max_output_chars: Longer successful text output is cut with a truncation note.
        return_direct: End the agent's turn with the tool's output as the answer.
        response_format: `"content_and_artifact"` for functions returning `(content, artifact)`.
        args_schema: An explicit pydantic model for the arguments.

    Raises:
        ConfigurationError: If the function has no description, uses `*args`, `**kwargs` or
            positional-only parameters, or an option is invalid.

    Example:
        ```python
        @tool(approval=lambda args: args["amount"] > 500, risk=["external_action"], timeout=20)
        async def issue_refund(ctx: RunContext[Shop], order_id: str, amount: float) -> str:
            \"\"\"Refund an amount for an order.

            Args:
                order_id: The order to refund, for example "A-1001".
                amount: Amount in EUR.
            \"\"\"
            return await ctx.deps.payments.refund(order_id, amount)
        ```
    """

    options = _Options(
        name=name,
        description=description,
        approval=approval,
        approval_options=dict(approval_options or {}),
        risk=risk,
        timeout=positive_seconds(timeout, "timeout"),
        retries=retries,
        retry_on=retry_on,
        retry_backoff=retry_backoff,
        idempotent=idempotent,
        idempotency_store=idempotency_store,
        on_error=on_error,
        frame_output=frame_output,
        max_output_chars=max_output_chars,
        return_direct=return_direct,
        response_format=response_format,
        args_schema=args_schema,
        tags=tuple(tags),
        metadata=dict(metadata or {}),
    )
    options.check()
    if isinstance(target, str):
        options = dataclasses.replace(options, name=options.name or target)
        target = None
    if target is None:
        return lambda function: _build_tool(function, options)
    return _build_tool(target, options)


def tool_method(
    name: str | None = None, /, **options: Unpack[ToolOptions]
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a method of a `Toolbox` as a tool, with the options of `tool`.

    The method stays an ordinary method; `Toolbox.tools()` binds it to the instance.
    """

    if callable(name):
        raise ConfigurationError("use @tool_method() with parentheses")
    if name is not None:
        options = {**options, "name": name}

    def mark(method: Callable[..., Any]) -> Callable[..., Any]:
        if not callable(method):
            raise ConfigurationError("@tool_method decorates methods")
        method.__awa_tool__ = dict(options)  # type: ignore[attr-defined]  # a marker attribute
        return method

    return mark


def serialize_output(result: Any) -> Any:
    """Turn a tool function's return value into tool content.

    Models, dataclasses, mappings and lists of them become JSON; strings and content-block lists
    pass unchanged, other values are stringified by LangChain.
    """

    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return json.dumps(dataclasses.asdict(result), default=str, ensure_ascii=False)
    if isinstance(result, list) and result and all(_is_block(item) for item in result):
        return result
    if isinstance(result, Mapping | list | tuple):
        return json.dumps(to_jsonable_python(result, fallback=str), ensure_ascii=False)
    return result


@dataclass(frozen=True)
class _Options:
    name: str | None = None
    description: str | None = None
    approval: bool | str | ApprovalPredicate = False
    approval_options: dict[str, Any] = dataclasses.field(default_factory=dict)
    risk: RiskSpec | None = None
    timeout: float | None = None
    retries: int = 0
    retry_on: RetryOn = retry_on_transient
    retry_backoff: float = 0.5
    idempotent: bool | IdempotencyKey = False
    idempotency_store: IdempotencyStore | None = None
    on_error: OnError = "return"
    frame_output: bool | None = None
    max_output_chars: int | None = None
    return_direct: bool = False
    response_format: Literal["content", "content_and_artifact"] = "content"
    args_schema: type[BaseModel] | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def check(self) -> None:
        if self.retries < 0:
            raise ConfigurationError(f"retries must be 0 or more, got {self.retries!r}")
        if self.retry_backoff < 0:
            raise ConfigurationError("retry_backoff must not be negative")
        check_output_options(self.on_error, self.max_output_chars)
        if isinstance(self.approval, str):
            ApprovalCondition(self.approval)
        elif not (isinstance(self.approval, bool) or callable(self.approval)):
            raise ConfigurationError("approval must be a bool, a condition or a predicate")
        self.resolved_risk()

    def resolved_risk(self) -> ToolRisk | None:
        risk = self.risk
        if risk is None:
            return None
        if isinstance(risk, Mapping):
            risk = dict(risk)
        elif not isinstance(risk, ToolRisk | str):
            risk = list(risk)
        try:
            return ToolRisk.model_validate(risk)
        except ValueError as exc:
            raise ConfigurationError(f"invalid risk {self.risk!r}: {exc}") from exc

    @property
    def approval_mode(self) -> ApprovalMode:
        if self.approval is True:
            return "always"
        if isinstance(self.approval, str) or callable(self.approval):
            return "conditional"
        return "never"


def _build_tool(target: ToolTarget, options: _Options) -> BaseTool:
    if isinstance(target, BaseTool):
        base, injects = _from_tool(target, options), False
    elif callable(target):
        base, injects = _from_function(target, options)
    else:
        raise ConfigurationError(f"@tool needs a function or a tool, got {type(target).__name__}")
    return _compile(base, options, injects_context=injects)


def _compile(base: BaseTool, options: _Options, *, injects_context: bool) -> BaseTool:
    risk = options.resolved_risk()
    declared = risk.merged_with(declared_tool_risk(base)) if risk else declared_tool_risk(base)
    frame = (
        options.frame_output
        if options.frame_output is not None
        else bool(declared is not None and declared.untrusted_input)
    )
    built = base
    condition: ApprovalCondition | None = None
    if isinstance(options.approval, str):
        condition = ApprovalCondition(options.approval).for_tool(base)
    if options.approval:
        predicate = condition or (options.approval if callable(options.approval) else None)
        extra = dict(options.approval_options)
        if predicate is not None:
            extra["requires_approval"] = predicate
        built = create_tool_approval_wrapper(built, **extra)
    if options.on_error == "return" or options.timeout or options.max_output_chars or frame:
        built = harden_tool(
            built,
            timeout=options.timeout,
            on_error=options.on_error,
            max_output_chars=options.max_output_chars,
            frame_output=frame,
        )
    spec = ToolSpec(
        approval=options.approval_mode,
        approval_when=condition.expression if condition is not None else None,
        timeout=options.timeout,
        retries=options.retries,
        idempotent=bool(options.idempotent),
        on_error=options.on_error,
        max_output_chars=options.max_output_chars,
        frame_output=frame,
        injects_context=injects_context,
    )
    metadata = {
        **(base.metadata or {}),
        **(built.metadata or {}),
        **options.metadata,
        TOOL_SPEC_METADATA_KEY: spec.to_metadata(),
    }
    tags = list(dict.fromkeys([*(base.tags or ()), *options.tags]))
    built = built.model_copy(
        update={"metadata": metadata, "tags": tags or None, "return_direct": base.return_direct}
    )
    return built if risk is None else mark_tool_risk(built, **risk.model_dump())


@dataclass(frozen=True)
class _Injection:
    context: tuple[str, ...]
    deps: tuple[str, ...]
    defaults: Mapping[str, Any]

    @property
    def names(self) -> tuple[str, ...]:
        return (*self.context, *self.deps)

    def arguments(
        self, tool_name: str, kwargs: dict[str, Any], ctx: RunContext[Any] | None
    ) -> dict[str, Any]:
        arguments = dict(kwargs)
        for name in self.names:
            if ctx is None:
                if name not in self.defaults:
                    raise ConfigurationError(
                        f"Tool {tool_name!r} needs a RunContext for {name!r}: call it inside "
                        "an awa run, or in tests with awa.testing.ToolContract(tool, deps=...)"
                    )
                arguments[name] = self.defaults[name]
            else:
                arguments[name] = ctx if name in self.context else ctx.deps
        return arguments


def _from_function(func: Callable[..., Any], options: _Options) -> tuple[BaseTool, bool]:
    name = options.name or getattr(func, "__name__", None)
    if not name or name == "<lambda>":
        raise ConfigurationError("a tool made from a lambda needs a name")
    description = options.description or _description(func)
    if not description:
        raise ConfigurationError(
            f"Tool {name!r} needs a description: give the function a docstring or pass "
            "description=..."
        )
    signature, hints = _signature(func, name)
    injection = _injection(signature, hints)
    schema = options.args_schema or _schema(func, name, signature, hints, injection)
    is_async = inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(
        getattr(func, "__call__", None)  # noqa: B004 - async callable objects
    )
    core = _Core(name, func, is_async, injection, options)
    sync_ok = not is_async and not options.idempotent
    base = StructuredTool.from_function(
        func=core.run if sync_ok else None,
        coroutine=core.arun if (is_async or options.idempotent) else None,
        name=name,
        description=description,
        args_schema=schema,
        return_direct=options.return_direct,
        response_format=options.response_format,
    )
    return base, bool(injection.names)


def _from_tool(target: BaseTool, options: _Options) -> BaseTool:
    update: dict[str, Any] = {}
    if options.name:
        update["name"] = options.name
    if options.description:
        update["description"] = options.description
    if options.return_direct:
        update["return_direct"] = True
    base = target.model_copy(update=update) if update else target
    if not options.retries and not options.idempotent:
        return base

    async def ainvoke(**kwargs: Any) -> Any:
        return await ainvoke_tool(base, kwargs)

    def invoke(**kwargs: Any) -> Any:
        return invoke_tool(base, kwargs)

    none = _Injection((), (), {})
    core_options = dataclasses.replace(options, response_format=base.response_format)
    supports_sync = not options.idempotent and not (
        isinstance(base, StructuredTool) and base.func is None
    )
    return rewrap(
        base,
        func=_Core(base.name, invoke, False, none, core_options).run if supports_sync else None,
        coroutine=_Core(base.name, ainvoke, True, none, core_options).arun,
    )


def _signature(func: Callable[..., Any], name: str) -> tuple[inspect.Signature, dict[str, Any]]:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Tool {name!r}: cannot read the signature: {exc}") from exc
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise ConfigurationError(
                f"Tool {name!r}: *args and **kwargs are not supported; declare named parameters"
            )
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise ConfigurationError(f"Tool {name!r}: positional-only parameters are not supported")
    return signature, _hints(func, signature)


def _hints(func: Callable[..., Any], signature: inspect.Signature) -> dict[str, Any]:
    target = inspect.unwrap(getattr(func, "__func__", func))
    try:
        return typing.get_type_hints(target, include_extras=True)
    except Exception:  # noqa: BLE001 - fall back to resolving parameter by parameter
        pass
    namespace = dict(getattr(target, "__globals__", {}))
    hints: dict[str, Any] = {}
    for parameter in signature.parameters.values():
        annotation = parameter.annotation
        if isinstance(annotation, str):
            # Unresolvable text is kept; `RunContext` is then matched by name.
            with contextlib.suppress(Exception):
                annotation = eval(annotation, namespace)
        if annotation is not inspect.Parameter.empty:
            hints[parameter.name] = annotation
    return hints


def _is_run_context(annotation: Any) -> bool:
    if isinstance(annotation, str):
        return bool(re.match(r"^(\w+\.)*RunContext\b", annotation.strip()))
    if typing.get_origin(annotation) is typing.Annotated:
        return _is_run_context(typing.get_args(annotation)[0])
    if annotation is RunContext or typing.get_origin(annotation) is RunContext:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return any(_is_run_context(arg) for arg in typing.get_args(annotation))
    return False


def _injection(signature: inspect.Signature, hints: Mapping[str, Any]) -> _Injection:
    context: list[str] = []
    deps: list[str] = []
    defaults: dict[str, Any] = {}
    for parameter in signature.parameters.values():
        annotation = hints.get(parameter.name, parameter.annotation)
        if _is_run_context(annotation) or (
            parameter.name == "ctx" and annotation is inspect.Parameter.empty
        ):
            context.append(parameter.name)
        elif parameter.name == "deps":
            deps.append(parameter.name)
        else:
            continue
        if parameter.default is not inspect.Parameter.empty:
            defaults[parameter.name] = parameter.default
    return _Injection(tuple(context), tuple(deps), defaults)


def _schema(
    func: Callable[..., Any],
    name: str,
    signature: inspect.Signature,
    hints: Mapping[str, Any],
    injection: _Injection,
) -> type[BaseModel]:
    parameters = [
        parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
        for parameter in signature.parameters.values()
        if parameter.name not in injection.names
    ]

    def proxy(**kwargs: Any) -> None:  # pragma: no cover - never called
        return None

    proxy.__signature__ = signature.replace(  # type: ignore[attr-defined]  # what inspect reads
        parameters=parameters, return_annotation=inspect.Signature.empty
    )
    proxy.__annotations__ = {
        parameter.name: parameter.annotation
        for parameter in parameters
        if parameter.annotation is not inspect.Parameter.empty
    }
    proxy.__doc__ = inspect.getdoc(func)
    proxy.__name__ = proxy.__qualname__ = name
    proxy.__module__ = getattr(func, "__module__", __name__)
    try:
        schema = create_schema_from_function(
            name, proxy, parse_docstring=True, error_on_invalid_docstring=False
        )
    except Exception as exc:
        raise ConfigurationError(f"Tool {name!r}: cannot build the argument schema: {exc}") from exc
    return typing.cast(type[BaseModel], schema)


def _description(func: Callable[..., Any]) -> str:
    lines = (inspect.getdoc(func) or "").splitlines()
    kept = itertools.takewhile(lambda line: not _SECTION.match(line.strip()), lines)
    return "\n".join(kept).strip()


class _Core:
    """Runs the function: injects the context, retries, de-duplicates, serializes."""

    def __init__(
        self,
        name: str,
        func: Callable[..., Any],
        is_async: bool,
        injection: _Injection,
        options: _Options,
    ) -> None:
        self.name = name
        self.func = func
        self.is_async = is_async
        self.injection = injection
        self.options = options
        self.idempotency: _Idempotency | None = None
        if options.idempotent:
            key = options.idempotent if callable(options.idempotent) else None
            store = options.idempotency_store or InMemoryIdempotencyStore()
            self.idempotency = _Idempotency(name, key, store)

    async def arun(self, **kwargs: Any) -> Any:
        ctx = current_run_context()
        if self.idempotency is None:
            return await self._attempts(kwargs, ctx)
        idem = self.idempotency
        key = idem.key(kwargs, ctx)
        token = _current_idempotency_key.set(key)
        try:

            async def once() -> Any:
                result = await self._attempts(kwargs, ctx)
                idem.remember(key, result)
                return result

            return await run_idempotent(
                once, key=key, store=idem.store, on_duplicate=idem.duplicate
            )
        finally:
            _current_idempotency_key.reset(token)

    def run(self, **kwargs: Any) -> Any:
        ctx = current_run_context()
        for attempt in range(self.options.retries + 1):
            try:
                return self._output(self.func(**self.injection.arguments(self.name, kwargs, ctx)))
            except Exception as exc:
                if not self._retries(exc, attempt):
                    raise
                time.sleep(self._delay(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _attempts(self, kwargs: dict[str, Any], ctx: RunContext[Any] | None) -> Any:
        for attempt in range(self.options.retries + 1):
            try:
                arguments = self.injection.arguments(self.name, kwargs, ctx)
                if self.is_async:
                    result = await self.func(**arguments)
                else:
                    result = await asyncio.to_thread(self.func, **arguments)
                return self._output(result)
            except Exception as exc:
                if not self._retries(exc, attempt):
                    raise
                await asyncio.sleep(self._delay(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    def _retries(self, exc: Exception, attempt: int) -> bool:
        from awa.runtime.engine import current_engine

        retry_on = self.options.retry_on
        if isinstance(exc, (UsageLimitExceededError, *current_engine().control_flow)):
            return False
        if isinstance(retry_on, type | tuple):
            return attempt < self.options.retries and isinstance(exc, retry_on)
        return attempt < self.options.retries and bool(retry_on(exc))

    def _delay(self, attempt: int) -> float:
        return backoff_delay(attempt, initial=self.options.retry_backoff, cap=_MAX_BACKOFF)

    def _output(self, result: Any) -> Any:
        if self.options.response_format == "content_and_artifact":
            return result
        return serialize_output(result)


class _Idempotency:
    def __init__(self, tool: str, key_fn: IdempotencyKey | None, store: IdempotencyStore) -> None:
        self.tool = tool
        self.key_fn = key_fn
        self.with_context = key_fn is not None and takes_context(key_fn)
        self.store = store
        self.results: OrderedDict[str, Any] = OrderedDict()

    def key(self, kwargs: dict[str, Any], ctx: RunContext[Any] | None) -> str:
        args = to_jsonable_python(kwargs, fallback=str)
        if self.key_fn is not None:
            custom = self.key_fn(args, ctx) if self.with_context else self.key_fn(args)
        else:
            data = {"run": ctx.root_run_id if ctx else "", "tool": self.tool, "args": args}
            text = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
            custom = hashlib.sha256(text.encode()).hexdigest()
        return f"awa:tool:{tenant_of(ctx)}:{self.tool}:{custom}"

    def remember(self, key: str, result: Any) -> None:
        self.results[key] = result
        self.results.move_to_end(key)
        while len(self.results) > _REMEMBERED_RESULTS:
            self.results.popitem(last=False)

    def duplicate(self, key: str) -> Any:
        if key in self.results:
            return self.results[key]
        return _DUPLICATE.format(tool=self.tool, key=key)


def _is_block(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") in _CONTENT_BLOCK_TYPES


__all__ = [
    "TOOL_SPEC_METADATA_KEY",
    "IdempotencyKey",
    "RetryOn",
    "RiskSpec",
    "ToolOptions",
    "ToolSpec",
    "current_idempotency_key",
    "serialize_output",
    "tool",
    "tool_method",
    "tool_spec",
]
