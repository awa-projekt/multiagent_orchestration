"""Human approval for individual tool calls, through LangGraph `interrupt()`.

A paused call resumes with `agent.resume(thread_id, {"action": "approve" | "reject" | "edit"})`;
the run needs a checkpointer.
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

from awa._internal.logging import get_logger
from awa._internal.upstream.tools import BaseTool, ToolException
from awa._internal.util import preview
from awa.contracts.events import ApprovalDecidedEvent, ApprovalRequestedEvent, rejection_message
from awa.errors import AwaError, ConfigurationError
from awa.kernel.redaction import SECRETS_REDACTOR, Redactor, redact_mapping
from awa.runtime.cache import tenant_of
from awa.runtime.context import RunContext, current_run_context
from awa.runtime.engine import Pause, current_engine
from awa.tools.hardening import ainvoke_tool, invoke_tool, model_arguments, positive_seconds, rewrap

APPROVAL_METADATA_KEY = "awa_requires_approval"
"""Tool metadata key recording that the tool waits for human approval."""

# LangGraph loads when an approval gate first runs, so `@tool` stays cheap to import.
if TYPE_CHECKING:
    from awa._internal.upstream.checkpoint import BaseStore

APPROVAL_GATE_METADATA_KEY = "awa_approval_gate"
"""Tool metadata key marking a tool whose calls can wait for a human decision."""

DEFAULT_APPROVAL_NAMESPACE: tuple[str, ...] = ("awa", "tool_approval")
"""Store namespace used for remembered tool-approval decisions."""

_log = get_logger("tools")
_NO_REASON = "no reason given"
_PREVIEW_CHARS = 500
_GREGORIAN_EPOCH = datetime(1582, 10, 15, tzinfo=UTC)

ApprovalPredicate = Callable[..., bool | Awaitable[bool]]
"""`requires_approval(args)` or `requires_approval(args, context)`, sync or async; `context` is the
current `RunContext` (agent, `deps`, `metadata`), or `None` outside an awa run."""

DecisionAction = Literal["approve", "reject", "edit"]


class InvalidGateActorError(AwaError):
    """Raised when `valid_actor` rejects a resume payload (a payload check, not authentication)."""


def _always(_args: dict[str, Any]) -> bool:
    return True


def default_approval_payload(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
    """The JSON interrupt payload a reviewer sees: the tool, its arguments, how to resume."""

    return {
        "tool": tool_name,
        "args": tool_args,
        "reason": "tool_call_requires_approval",
        "instructions": (
            "Approve with agent.resume(thread_id, {'action': 'approve'}), reject with "
            "agent.resume(thread_id, {'action': 'reject', 'note': '...'}) or approve changed "
            "arguments with agent.resume(thread_id, {'action': 'edit', 'args': {...}}); "
            "use team.resume(...) for a team run"
        ),
    }


class ApprovalCondition:
    """An approval predicate written as an expression over the call's arguments.

    `"amount >= 5000"`, `"country not in ['DE', 'AT'] or amount > 1000"` and
    `"contains(recipient, '@partner.example')"` work: comparisons, `and`/`or`/`not`, `in`,
    arithmetic, `true`/`false`/`null` and the functions `contains`, `startswith`, `endswith`,
    `lower`, `upper`, `len`, `number`, `empty`, `abs`, `round`, `min`, `max`. It is evaluated by
    walking an `ast` allowlist, never `eval`. A left-out argument has the tool's default (`null`
    without one). The condition fails closed: an expression that cannot be evaluated, an ordered
    comparison with `null`, or a number written as `5.000` or `5,000` needs approval.

    Args:
        arguments: The tool's argument names; when given, the expression may read only these.
        defaults: Values of arguments a call leaves out.

    Raises:
        ConfigurationError: If the expression is malformed, uses anything the language does not
            allow, or reads a name that is not an argument.

    Example:
        ```python
        needs_approval = ApprovalCondition("amount >= 5000", arguments=["invoice_id", "amount"])
        needs_approval({"invoice_id": "INV-7", "amount": 6200})   # True
        ```
    """

    def __init__(
        self,
        expression: str,
        *,
        arguments: Iterable[str] | None = None,
        defaults: Mapping[str, Any] | None = None,
    ) -> None:
        from awa.kernel.conditions import predicate

        text = " ".join(str(expression).split())
        if not text:
            raise ConfigurationError("An approval condition needs an expression")
        try:
            self._parsed = predicate(text, strict=True)
            self._lenient = predicate(text)
        except ToolException as exc:
            raise ConfigurationError(f"Invalid approval condition {text!r}: {exc}") from None
        self._expression = text
        self._defaults = dict(defaults or {})
        self._tool_arguments: frozenset[str] | None = None
        self._bound = False
        if arguments is not None:
            unknown = self.unknown_names(arguments)
            if unknown:
                known = ", ".join(sorted(arguments)) or "none"
                raise ConfigurationError(
                    f"Approval condition {text!r} reads {', '.join(unknown)}, which "
                    f"{'is not an argument' if len(unknown) == 1 else 'are not arguments'} "
                    f"of the tool (arguments: {known})"
                )

    @property
    def expression(self) -> str:
        """The condition, with whitespace normalized."""

        return self._expression

    @property
    def names(self) -> frozenset[str]:
        """The argument names the condition reads."""

        return self._parsed.names

    def unknown_names(self, arguments: Iterable[str]) -> list[str]:
        """The names the condition reads that are not in `arguments`, sorted."""

        return sorted(self.names - set(arguments))

    def for_tool(self, tool: BaseTool, *, strict: bool = True) -> ApprovalCondition:
        """The condition bound to `tool`: its argument names checked, its defaults applied.

        The tool's own argument defaults win over the `defaults` given to this condition, which
        still fill the names the tool has no default for. `strict=False` is for a condition
        shared by several tools, where a missing name is `null`.

        Raises:
            ConfigurationError: If `strict` and the condition reads a name `tool` does not have.
        """

        properties = _argument_schemas(tool)
        defaults = {
            **self._defaults,
            **{
                name: schema["default"]
                for name, schema in properties.items()
                if isinstance(schema, Mapping) and "default" in schema
            },
        }
        bound = ApprovalCondition(
            self._expression, arguments=list(properties) if strict else None, defaults=defaults
        )
        bound._bound = True
        bound._tool_arguments = frozenset(properties)
        return bound

    def __call__(self, args: Mapping[str, Any]) -> bool:
        """Whether the call with arguments `args` needs approval."""

        def value(name: str) -> Any:
            return args[name] if name in args else self._defaults.get(name)

        # A condition shared by several tools reads a name some of them lack: for those a missing
        # name means "does not apply" (null). For a tool that has the argument, missing is a guess.
        shared = self._tool_arguments is not None and bool(self.names - self._tool_arguments)
        try:
            return bool((self._lenient if shared else self._parsed).evaluate(value))
        except ToolException:
            return True

    def __repr__(self) -> str:
        return f"ApprovalCondition({self._expression!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ApprovalCondition):
            return NotImplemented
        return self._expression == other._expression and self._defaults == other._defaults

    def __hash__(self) -> int:
        return hash(self._expression)


def create_tool_approval_wrapper(
    tool: BaseTool,
    *,
    requires_approval: ApprovalPredicate | ApprovalCondition | str = _always,
    build_interrupt_payload: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    store: BaseStore | None = None,
    memory_namespace: tuple[str, ...] = DEFAULT_APPROVAL_NAMESPACE,
    memory_key: Callable[..., str | None] | None = None,
    valid_actor: Callable[[Mapping[str, Any]], bool] | None = None,
    expires_after: timedelta | float | None = None,
    decision_ttl: timedelta | float | None = None,
    allow_edits: bool = True,
    redactor: Redactor | None = None,
) -> BaseTool:
    """Wrap `tool` so selected calls wait for a human decision before running.

    The wrapper keeps the tool's name, description, schema, `return_direct`, `response_format`,
    metadata and tags. Decisions: `{"action": "approve"}`, `{"action": "reject", "note": ...}`, or
    `{"action": "edit", "args": {...}}` (merged over the call and validated). Anything else, an edit
    without `allow_edits`, or a decision after `expires_after` is a rejection with a note. A
    rejection returns a short message instead of raising. With approvals switched off in the
    blueprint the tool is returned unwrapped and a warning is logged.

    Args:
        requires_approval: Per call, from the arguments (and optionally the `RunContext`), sync or
            async; or an expression such as `"amount >= 5000"` or an `ApprovalCondition`. Defaults
            to always.
        build_interrupt_payload: What the reviewer sees; defaults to `default_approval_payload`.
            Mapping payloads also get `agent`, `run_id`, `requested_at` and `expires_at`.
        store: Store for remembering decisions across threads, used with `memory_key`.
        memory_namespace: Store namespace of the remembered decisions.
        memory_key: Maps the arguments (and optionally the `RunContext`) to the key a decision is
            remembered under, per tenant; `None` remembers nothing. Edits are never remembered.
        valid_actor: Validates the resume payload; see `InvalidGateActorError`.
        expires_after: How long a request stays answerable, measured from the checkpoint the run
            paused at, so it survives restarts.
        decision_ttl: How long a remembered decision stays valid; without it, forever.
        redactor: Redacts the arguments shown to the reviewer; the tool runs with the real ones.

    Raises:
        ConfigurationError: If `expires_after`/`decision_ttl` is not positive or a condition is
            invalid.

    Example:
        ```python
        booking = create_tool_approval_wrapper(book_invoice, requires_approval="amount >= 5000")
        guarded = create_tool_approval_wrapper(
            transfer_funds,
            requires_approval=lambda args, ctx: args["amount"] > ctx.deps.approval_threshold,
            expires_after=timedelta(hours=24),
        )
        ```
    """

    from awa.plugins.switches import is_off

    if is_off("guard.approvals"):
        _log.warning(
            "Approvals are switched off for this project (guard.approvals): %s runs without "
            "waiting for a person",
            tool.name,
        )
        return tool
    if isinstance(requires_approval, str):
        requires_approval = ApprovalCondition(requires_approval)
    if isinstance(requires_approval, ApprovalCondition) and not requires_approval._bound:
        requires_approval = requires_approval.for_tool(tool)
    gate = _ApprovalGate(
        tool=tool,
        requires_approval=requires_approval,
        payload_fn=build_interrupt_payload or default_approval_payload,
        store=store,
        namespace=memory_namespace,
        memory_key=memory_key,
        valid_actor=valid_actor,
        expires_after=positive_seconds(expires_after, "expires_after"),
        decision_ttl=positive_seconds(decision_ttl, "decision_ttl"),
        allow_edits=allow_edits,
        redactor=redactor,
    )

    async def _arun(**kwargs: Any) -> Any:
        return await gate.arun(kwargs)

    def _run(**kwargs: Any) -> Any:
        return gate.run(kwargs)

    return rewrap(
        tool,
        func=_run,
        coroutine=_arun,
        metadata={**(tool.metadata or {}), APPROVAL_GATE_METADATA_KEY: True},
    )


@dataclass(frozen=True)
class _Outcome:
    decision: DecisionAction
    note: str
    reviewer: str | None
    args: dict[str, Any]
    expired: bool = False
    explicit: bool = True


@dataclass(frozen=True)
class _Request:
    requested_at: datetime
    expires_at: datetime | None
    payload: Any
    interrupt_id: str
    thread_id: str | None


@dataclass
class _ApprovalGate:
    """The decision flow shared by the async and sync paths of a wrapped tool."""

    tool: BaseTool
    requires_approval: ApprovalPredicate
    payload_fn: Callable[[str, dict[str, Any]], dict[str, Any]]
    store: BaseStore | None
    namespace: tuple[str, ...]
    memory_key: Callable[..., str | None] | None
    valid_actor: Callable[[Mapping[str, Any]], bool] | None
    expires_after: float | None
    decision_ttl: float | None
    allow_edits: bool
    redactor: Redactor | None
    requires_context: bool = field(init=False)
    key_context: bool = field(init=False)

    def __post_init__(self) -> None:
        self.requires_context = takes_context(self.requires_approval)
        self.key_context = self.memory_key is not None and takes_context(self.memory_key)

    async def arun(self, args: dict[str, Any]) -> Any:
        ctx = current_run_context()
        needed = self._needed(args, ctx)
        if inspect.isawaitable(needed):
            needed = await needed
        if not needed:
            return await ainvoke_tool(self.tool, args)
        engine = current_engine()
        slot = self._slot(args, ctx)
        if slot is not None and self.store is not None and not engine.has_decision():
            item = await self.store.aget(self.namespace, slot)
            remembered = self._recall(item.value if item is not None else None, args)
            if remembered is not None:
                if ctx is not None:
                    await ctx.emit(self._decided_event(ctx, remembered, None))
                return self._rejected(remembered) or await ainvoke_tool(self.tool, args)
            if item is not None:
                await self.store.adelete(self.namespace, slot)
        request = self._request(args, ctx)
        try:
            decision = engine.pause(request.payload)
        except engine.control_flow as exc:
            if ctx is not None:
                await ctx.emit(self._requested_event(ctx, args, request, engine.pauses(exc)))
            raise
        outcome, record = self._settle(decision, args, request)
        if slot is not None and self.store is not None and record is not None:
            await self.store.aput(self.namespace, slot, record)
        if ctx is not None:
            await ctx.emit(self._decided_event(ctx, outcome, request))
        return self._rejected(outcome) or await ainvoke_tool(self.tool, outcome.args)

    def run(self, args: dict[str, Any]) -> Any:
        ctx = current_run_context()
        needed = _sync_value(self._needed(args, ctx), "requires_approval")
        if not needed:
            return invoke_tool(self.tool, args)
        engine = current_engine()
        slot = self._slot(args, ctx)
        if slot is not None and self.store is not None and not engine.has_decision():
            item = self.store.get(self.namespace, slot)
            remembered = self._recall(item.value if item is not None else None, args)
            if remembered is not None:
                if ctx is not None:
                    ctx.events.emit_later(self._decided_event(ctx, remembered, None))
                return self._rejected(remembered) or invoke_tool(self.tool, args)
            if item is not None:
                self.store.delete(self.namespace, slot)
        request = self._request(args, ctx)
        try:
            decision = engine.pause(request.payload)
        except engine.control_flow as exc:
            if ctx is not None:
                ctx.events.emit_later(self._requested_event(ctx, args, request, engine.pauses(exc)))
            raise
        outcome, record = self._settle(decision, args, request)
        if slot is not None and self.store is not None and record is not None:
            self.store.put(self.namespace, slot, record)
        if ctx is not None:
            ctx.events.emit_later(self._decided_event(ctx, outcome, request))
        return self._rejected(outcome) or invoke_tool(self.tool, outcome.args)

    def _rejected(self, outcome: _Outcome) -> Any:
        """The tool result of a rejection, or `None` when the call may run."""

        if outcome.decision != "reject":
            return None
        text = rejection_message(self.tool.name, outcome.note)
        return (text, None) if self.tool.response_format == "content_and_artifact" else text

    def _needed(self, args: dict[str, Any], ctx: RunContext[Any] | None) -> Any:
        fn = self.requires_approval
        return fn(args, ctx) if self.requires_context else fn(args)

    def _slot(self, args: dict[str, Any], ctx: RunContext[Any] | None) -> str | None:
        fn = self.memory_key
        if self.store is None or fn is None:
            return None
        key = _sync_value(fn(args, ctx) if self.key_context else fn(args), "memory_key")
        return None if key is None else f"{tenant_of(ctx)}|{key}"

    def _settle(
        self, decision: Any, args: dict[str, Any], request: _Request
    ) -> tuple[_Outcome, dict[str, Any] | None]:
        """The outcome of a resume, and the record to remember (`None`: nothing to remember)."""

        outcome = self._resolve(decision, args, request)
        if outcome.decision == "edit" or not outcome.explicit:
            return outcome, None
        record = {"action": outcome.decision, "note": outcome.note, "reviewer": outcome.reviewer}
        return outcome, {**record, "decided_at": _now().isoformat()}

    def _request(self, args: dict[str, Any], ctx: RunContext[Any] | None) -> _Request:
        requested_at = _request_time()
        expires_at = None
        if self.expires_after is not None:
            expires_at = requested_at + timedelta(seconds=self.expires_after)
        shown = redact_mapping(args, self.redactor) if self.redactor is not None else args
        payload: Any = self.payload_fn(self.tool.name, dict(shown))
        if isinstance(payload, dict):
            payload = dict(payload)
            if ctx is not None:
                payload.setdefault("agent", ctx.agent)
                payload.setdefault("run_id", ctx.run_id)
            payload.setdefault("requested_at", requested_at.isoformat())
            if isinstance(self.requires_approval, ApprovalCondition):
                payload.setdefault("condition", self.requires_approval.expression)
            if expires_at is not None:
                payload.setdefault("expires_at", expires_at.isoformat())
        interrupt_id, thread_id = _interrupt_identity()
        return _Request(requested_at, expires_at, payload, interrupt_id, thread_id)

    def _resolve(self, decision: Any, args: dict[str, Any], request: _Request) -> _Outcome:
        if not isinstance(decision, Mapping):
            return _Outcome(
                "reject", f"{_NO_REASON} (malformed resume payload)", None, args, explicit=False
            )
        if self.valid_actor is not None and not self.valid_actor(decision):
            raise InvalidGateActorError(f"Resume payload rejected by valid_actor: {decision!r}")
        reviewer = _reviewer(decision)
        if self.expires_after is not None:
            age = (_now() - request.requested_at).total_seconds()
            if age > self.expires_after:
                return _Outcome(
                    "reject",
                    f"the approval request expired after {self.expires_after:g} seconds",
                    reviewer,
                    args,
                    expired=True,
                    explicit=False,
                )
        action = decision.get("action")
        note = str(decision.get("note", _NO_REASON))
        if action == "approve":
            return _Outcome("approve", note, reviewer, args)
        if action != "edit":
            return _Outcome("reject", note, reviewer, args, explicit=action == "reject")
        problem = self._edit_problem(decision.get("args"), args)
        if problem is not None:
            return _Outcome("reject", problem, reviewer, args, explicit=False)
        merged = {**args, **decision["args"]}
        return _Outcome("edit", note, reviewer, merged)

    def _edit_problem(self, edited: Any, args: dict[str, Any]) -> str | None:
        """Why the edit `edited` of `args` cannot run, or `None`."""

        if not self.allow_edits:
            return "edits are not allowed for this tool"
        if not isinstance(edited, Mapping):
            return "an edit decision needs an 'args' mapping"
        if injected := sorted(set(edited) - set(model_arguments(self.tool, edited))):
            return f"an edit may change only the model's arguments, not {', '.join(injected)}"
        problem = validate_args(self.tool, {**args, **edited})
        return None if problem is None else f"the edited arguments are invalid: {problem}"

    def _recall(self, value: Mapping[str, Any] | None, args: dict[str, Any]) -> _Outcome | None:
        if value is None:
            return None
        if self.decision_ttl is not None:
            decided_at = _parse_time(value.get("decided_at"))
            if decided_at is None or (_now() - decided_at).total_seconds() > self.decision_ttl:
                return None
        action: DecisionAction = "approve" if value.get("action") == "approve" else "reject"
        reviewer = value.get("reviewer")
        return _Outcome(
            action,
            str(value.get("note", _NO_REASON)),
            str(reviewer) if reviewer is not None else None,
            args,
        )

    def _requested_event(
        self, ctx: RunContext[Any], args: dict[str, Any], request: _Request, raised: Sequence[Pause]
    ) -> ApprovalRequestedEvent:
        interrupt_id = raised[0].id if raised else ""
        payload = request.payload
        reason = payload.get("reason") if isinstance(payload, dict) else None
        return ApprovalRequestedEvent(
            tool=self.tool.name,
            interrupt_id=interrupt_id or request.interrupt_id,
            args_preview=_args_preview(args, self.redactor),
            reason=str(reason) if reason else "tool_call_requires_approval",
            thread_id=request.thread_id,
            expires_at=request.expires_at,
            **ctx.event_fields(),
        )

    def _decided_event(
        self, ctx: RunContext[Any], outcome: _Outcome, request: _Request | None
    ) -> ApprovalDecidedEvent:
        """The decision event; `request` is `None` for a remembered decision."""

        latency = None
        if request is not None:
            latency = max(0, int((_now() - request.requested_at).total_seconds() * 1000))
        return ApprovalDecidedEvent(
            tool=self.tool.name,
            decision=outcome.decision,
            interrupt_id=request.interrupt_id if request is not None else None,
            reviewer=outcome.reviewer,
            note=SECRETS_REDACTOR.redact(outcome.note),
            latency_ms=latency,
            remembered=request is None,
            expired=outcome.expired,
            thread_id=request.thread_id if request is not None else _interrupt_identity()[1],
            **ctx.event_fields(),
        )


def takes_context(fn: Callable[..., Any]) -> bool:
    """Whether `fn` takes the `RunContext` as its second positional argument: one without a
    default, or `*args` (a second parameter with a default is an option, not the context)."""

    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters):
        return True
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    required = [p for p in parameters if p.kind in positional and p.default is p.empty]
    return len(required) >= 2


def _sync_value(value: Any, name: str) -> Any:
    if inspect.isawaitable(value):
        close = getattr(value, "close", None)
        if callable(close):
            close()
        raise TypeError(f"{name} returned an awaitable; call the tool asynchronously")
    return value


def _reviewer(decision: Mapping[str, Any]) -> str | None:
    """The reviewer named in a resume payload (`reviewer`, else `actor`)."""

    for key in ("reviewer", "actor"):
        value = decision.get(key)
        if isinstance(value, Mapping):
            value = value.get("id")
        elif value is not None and not isinstance(value, str):
            value = getattr(value, "id", value)
        if value is not None and str(value):
            return str(value)
    return None


def validate_args(tool: BaseTool, args: dict[str, Any]) -> str | None:
    """Why `args` do not fit the tool's argument schema, or `None` when they do."""

    schema = tool.tool_call_schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            schema.model_validate(model_arguments(tool, args))
        except ValidationError as exc:
            return "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
        return None
    if isinstance(schema, Mapping):
        properties = schema.get("properties") or {}
        missing = [name for name in schema.get("required", ()) if name not in args]
        if missing:
            return f"missing required arguments: {', '.join(missing)}"
        if schema.get("additionalProperties") is False:
            unknown = [name for name in args if name not in properties]
            if unknown:
                return f"unknown arguments: {', '.join(unknown)}"
    return None


def _args_preview(args: dict[str, Any], redactor: Redactor | None) -> str:
    redacted = redact_mapping(args, redactor)
    if redactor is not None and redactor is not SECRETS_REDACTOR:
        redacted = redact_mapping(redacted, SECRETS_REDACTOR)
    try:
        text = json.dumps(redacted, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(redacted)
    return preview(text, _PREVIEW_CHARS)


def _config() -> Mapping[str, Any]:
    from awa._internal.upstream.runnables import get_config

    try:
        configurable = get_config().get("configurable", {})
    except RuntimeError:
        return {}
    return configurable if isinstance(configurable, Mapping) else {}


def _interrupt_identity() -> tuple[str, str | None]:
    """The id `interrupt()` gives the current tool call's interrupt, and the thread id."""

    from awa._internal.upstream.graph import Interrupt

    configurable = _config()
    thread_id = configurable.get("thread_id")
    namespace = configurable.get("checkpoint_ns")
    interrupt_id = Interrupt.from_ns(None, namespace).id if isinstance(namespace, str) else ""
    return interrupt_id, str(thread_id) if thread_id is not None else None


def _request_time() -> datetime:
    """When the paused run's checkpoint was written (stable across resumes), else now."""

    stamps = [
        stamp
        for value in (_config().get("checkpoint_map") or {}).values()
        if (stamp := _uuid6_time(value)) is not None
    ]
    return max(stamps) if stamps else _now()


def _uuid6_time(value: Any) -> datetime | None:
    try:
        parsed = uuid.UUID(str(value))
    except ValueError:
        return None
    if parsed.version != 6:
        return None
    number = parsed.int
    ticks = ((number >> 96) << 28) | (((number >> 80) & 0xFFFF) << 12) | ((number >> 64) & 0x0FFF)
    return _GREGORIAN_EPOCH + timedelta(microseconds=ticks // 10)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _argument_schemas(tool: BaseTool) -> dict[str, Any]:
    """The JSON schema of each model-supplied argument of `tool`, by name."""

    try:
        properties = tool.args
    except Exception:  # noqa: BLE001 - a tool without a readable schema has no named arguments
        return {}
    return dict(properties) if isinstance(properties, Mapping) else {}


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "APPROVAL_GATE_METADATA_KEY",
    "APPROVAL_METADATA_KEY",
    "DEFAULT_APPROVAL_NAMESPACE",
    "ApprovalCondition",
    "ApprovalPredicate",
    "InvalidGateActorError",
    "create_tool_approval_wrapper",
    "default_approval_payload",
]
