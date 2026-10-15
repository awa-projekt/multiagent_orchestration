"""What each run event type records in every sink, and the trace line encoder.

`EVENT_FIELDS` is the one table of event facts: the detail of a log line, the fields of a trace
line, the attributes of a span event and which fields are free text. A trace line is a redacted,
size-bounded JSON object; every text is redacted and cleared of known credentials before it is
cut, so a secret is never half stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

from awa._internal.util import preview
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentMessageEvent,
    AgentStartedEvent,
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    DelegationEvent,
    DelegationResultEvent,
    ErrorEvent,
    GuardrailBlockedEvent,
    HandoffEvent,
    LimitExceededEvent,
    MessageUndeliveredEvent,
    ModelFallbackEvent,
    ModelResponseEvent,
    ModelRetryEvent,
    OutputRetryEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunEvent,
    StepEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.kernel.redaction import Redactor, redact_mapping

# The tools by which agents hand each other work; their arguments are message bodies (the task,
# the question) and only the name of the agent they address is kept below level `full`.
_HAND_OVER_TOOLS = {"delegate_task": "agent", "consult_peer": "peer"}
_HAND_OVER_PREFIX = "transfer_to_"
ARGUMENT_PREVIEW = 200
BODY_CHARS = 4000
SHORT_CHARS = 300
ERROR_CHARS = 600
_VALUE_CHARS = 8000
_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|DSN", re.IGNORECASE)
MIN_SECRET = 8
IDENTITY_KEYS = ("subject", "actor", "thread_id")
"""Header facts that say whose trace it is; each is also stored as a hash (`<key>_ref`)."""


@dataclass(frozen=True)
class EventFields:
    """What one event type records in each sink.

    A spec lists fields, separated by spaces. A field names an attribute (`a.b` follows a nested
    one) and stores it under that name, or under `key` as `key=attribute`. Marks before it: `~`
    cuts the text short, `?` leaves out `None` and empty text, `#` stores the length of the text,
    `+` puts it on a span only with `capture_content`, `@` masks personal data on a span. A field
    named in `content` is free text: the audit log keeps only its length, a trace keeps it only at
    level `full` and a span only with `capture_content`.

    Attributes:
        log: The detail of `describe_event`'s line.
        trace: The fields of a trace line, in order (`None`: the encoder writes the line itself).
        span: The attributes of the span event `awa.<type>`, in order (`None`: no span event).
        content: The free-text fields.
    """

    log: Callable[[Any], str] | None = None
    trace: str | None = None
    span: str | None = None
    content: str = ""


def _p(text: str) -> str:
    return preview(text, 120, flatten=True)


def _error_detail(e: ErrorEvent) -> str:
    detail = f"error={e.error_type}: {_p(e.message)}"
    if e.stop is not None:
        detail += f" stop={e.stop.get('stop_reason')} bound={e.stop.get('bound')}"
    return detail


def _decided_detail(e: ApprovalDecidedEvent) -> str:
    detail = f"tool={e.tool} decision={e.decision} reviewer={e.reviewer or '-'}"
    expired = " expired=True" if e.expired else ""
    return detail + expired + (" remembered=True" if e.remembered else "")


def _message_detail(e: AgentMessageEvent) -> str:
    recipient = e.message.recipient or e.message.topic or "*"
    return f"kind={e.message.kind} sender={e.message.sender} recipient={recipient}"


EVENT_FIELDS: dict[type, EventFields] = {
    AgentStartedEvent: EventFields(
        log=lambda e: f"input={_p(e.input_preview)!r}",
        trace="parent_run_id kind input=input_preview",
        content="input_preview",
    ),
    TextDeltaEvent: EventFields(log=lambda e: f"delta={e.delta!r}", content="delta"),
    ModelResponseEvent: EventFields(
        log=lambda e: (
            f"tool_calls={','.join(e.tool_call_names) or '-'} input_tokens={e.input_tokens} "
            f"output_tokens={e.output_tokens}"
        ),
        trace="model provider finish_reason input_tokens output_tokens "
        "tool_calls=tool_call_names #content_chars=content content",
        content="content",
    ),
    ToolCallEvent: EventFields(log=lambda e: f"tool={e.tool_name} call_id={e.call_id}"),
    ToolResultEvent: EventFields(
        log=lambda e: f"tool={e.tool_name} call_id={e.call_id} is_error={e.is_error}",
        trace="tool=tool_name call_id is_error #result_chars=content ~error=error_text content",
        content="content",
    ),
    DelegationEvent: EventFields(
        log=lambda e: f"mode={e.mode} target={e.target} task={_p(e.task)!r}",
        trace="target mode correlation_id #task_chars=task task",
        span="target mode correlation_id task",
        content="task",
    ),
    DelegationResultEvent: EventFields(
        log=lambda e: f"target={e.target} status={e.status}",
        trace="target correlation_id status content=content_preview",
        span="target status correlation_id content=content_preview",
        content="content_preview",
    ),
    HandoffEvent: EventFields(
        log=lambda e: f"source={e.source} target={e.target}",
        trace="source target ~reason",
        span="source target ?reason",
        content="reason",
    ),
    AgentMessageEvent: EventFields(
        log=_message_detail,
        trace="kind=message.kind sender=message.sender recipient=message.recipient "
        "topic=message.topic message_id=message.id ~subject=message.subject "
        "content=message.content",
        span="message.id message.kind message.sender ?message.recipient ?message.topic "
        "?messaging.message.conversation_id=message.conversation_id message.subject "
        "message.content",
        content="message.subject message.content",
    ),
    MessageUndeliveredEvent: EventFields(trace="recipient ~reason"),
    AgentFinishedEvent: EventFields(
        log=lambda e: f"duration_ms={e.duration_ms} total_tokens={e.usage.total_tokens}",
        trace="duration_ms output=output_preview",
        content="output_preview",
    ),
    ErrorEvent: EventFields(log=_error_detail),
    ApprovalRequestedEvent: EventFields(
        log=lambda e: f"tool={e.tool} interrupt_id={e.interrupt_id}",
        span="tool interrupt_id ?gen_ai.conversation.id=thread_id ?expires_at +?args=args_preview",
    ),
    ApprovalDecidedEvent: EventFields(
        log=_decided_detail,
        trace="tool decision interrupt_id ?~reviewer ~note latency_ms remembered expired",
        span="tool decision remembered expired @?reviewer ?latency_ms +?note",
    ),
    GuardrailBlockedEvent: EventFields(
        log=lambda e: (
            f"guardrail={e.guardrail} stage={e.stage}" + (f" tool={e.tool}" if e.tool else "")
        ),
        trace="guardrail stage tool ~reason",
        span="guardrail stage ?tool +?reason",
    ),
    LimitExceededEvent: EventFields(
        log=lambda e: f"limit={e.limit} value={e.value} maximum={e.maximum}",
        trace="limit value maximum ~message",
        span="limit ?value ?maximum",
    ),
    StepEvent: EventFields(
        log=lambda e: f"step={e.step} {e.index + 1}/{e.total} status={e.status}",
        trace="step index total status ?~title ?~message attempt case",
        span="step step.index=index step.total=total step.status=status ?step.attempt=attempt "
        "?step.case=case ?step.checks_failed=failed_checks +?message",
    ),
    RunCompletedEvent: EventFields(log=lambda e: f"duration_ms={e.result.duration_ms}"),
    RunCancelledEvent: EventFields(
        log=lambda e: f"reason={e.reason} elapsed_ms={e.elapsed_ms}",
        trace="~reason cancelled_by elapsed_ms",
        span="reason elapsed_ms @?cancelled_by",
    ),
    ModelRetryEvent: EventFields(
        log=lambda e: f"model={e.model} attempt={e.attempt} kind={e.kind} delay={e.delay:g}s",
        trace="model attempt delay error_type kind status",
        span="model attempt delay error_type kind ?status",
    ),
    OutputRetryEvent: EventFields(
        log=lambda e: f"check={e.check} attempt={e.attempt}/{e.max_retries}",
        trace="check attempt max_retries kind",
        span="check attempt max_retries kind",
    ),
    ModelFallbackEvent: EventFields(
        log=lambda e: f"from={e.from_model} to={e.to_model} reason={e.reason}",
        trace="from_model to_model ~reason error_type",
        span="from_model to_model reason ?error_type",
    ),
}
"""The facts of every event type (see `EventFields`)."""

_DERIVED: dict[str, Callable[[Any], Any]] = {
    "error_text": lambda e: e.content if e.is_error else None,
    "failed_checks": lambda e: (
        sum(1 for check in e.checks if not check.get("passed", True)) if e.checks else None
    ),
}


@cache
def parse_fields(spec: str) -> tuple[tuple[str, str, str], ...]:
    """The marks, key and attribute of every field of a spec (see `EventFields`)."""

    fields = []
    for field in spec.split():
        name = field.lstrip("~?#+@")
        key, _, attribute = name.partition("=")
        fields.append((field[: len(field) - len(name)], key, attribute or key))
    return tuple(fields)


def field_value(event: RunEvent, attribute: str) -> Any:
    """The value of `attribute` (dotted for a nested one) of `event`."""

    derive = _DERIVED.get(attribute)
    if derive is not None:
        return derive(event)
    value: Any = event
    for part in attribute.split("."):
        value = getattr(value, part)
    return value


def environment_secrets() -> list[str]:
    """Values of the credentials in the environment (long enough not to garble ordinary text)."""

    return [
        value
        for name, value in os.environ.items()
        if _SECRET_NAME.search(name) and len(value) >= MIN_SECRET
    ]


def identity_ref(value: Any) -> str:
    """A short hash of an identity (`subject`, `actor`, `thread_id`) that finds its traces again.

    `TraceStore.erase` finds a person's traces by it even when the redactor masked the id in the
    header; a hash is pseudonymous, not anonymous.
    """

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def iso(moment: datetime) -> str:
    """`moment` as UTC text with millisecond precision."""

    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def _bounded(value: Any, depth: int = 0) -> Any:
    """`value` with long strings and big containers cut, so redaction stays cheap."""

    if isinstance(value, str):
        return value if len(value) <= _VALUE_CHARS else value[:_VALUE_CHARS] + "..."
    if depth >= 6:
        return str(value)[:SHORT_CHARS]
    if isinstance(value, Mapping):
        return {str(k): _bounded(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, list | tuple | set | frozenset):
        return [_bounded(item, depth + 1) for item in list(value)[:100]]
    if isinstance(value, bool | int | float | type(None)):
        return value
    return str(value)[:SHORT_CHARS]


class TraceEncoder:
    """Encodes run events for one trace.

    Args:
        level: `summary` (structure, no message bodies) or `full` (bodies too, cut).
        secrets: Credential values removed wherever they appear (besides the redactor's patterns).
        with_root: Store `root_run_id` on every line (a trace that follows several run trees).
    """

    def __init__(
        self, level: str, redactor: Redactor, secrets: Iterable[str], *, with_root: bool
    ) -> None:
        self.full = level == "full"
        self._redactor = redactor
        # Each secret as written and URL-encoded (as it appears in a DSN), longest first.
        needles = {
            form
            for secret in secrets
            if len(secret) >= MIN_SECRET
            for form in (secret, urllib.parse.quote(secret, safe=""))
        }
        self._needles = sorted(needles, key=len, reverse=True)
        self._with_root = with_root
        self._handlers: dict[type, Callable[[Any, dict[str, Any]], None]] = {
            **{kind: self._copy for kind, facts in EVENT_FIELDS.items() if facts.trace},
            ToolCallEvent: self._tool_call,
            ErrorEvent: self._error,
            ApprovalRequestedEvent: self._approval_requested,
            StepEvent: self._step,
        }

    def clean(self, value: Any, limit: int, *, flatten: bool = False) -> str:
        """`value` as text with secrets removed, cut to `limit` characters."""

        text = str(value)
        if len(text) > limit * 4:
            text = text[: limit * 4]
        return preview(self._scrub(self._redactor.redact(text)), limit, flatten=flatten)

    def _scrub(self, text: str) -> str:
        """`text` without the credentials of the environment."""

        for needle in self._needles:
            if needle in text:
                text = text.replace(needle, "***")
        return text

    def redact(self, value: Any) -> Any:
        """A copy of `value` (text or JSON-like data) without secrets or personal data; nothing is cut."""

        return self._scrubbed(redact_mapping(value, self._redactor))

    def _scrubbed(self, value: Any) -> Any:
        # Leaf by leaf: a secret with a quote or a backslash does not match once it is JSON text.
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, Mapping):
            return {k: self._scrubbed(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self._scrubbed(item) for item in value]
        return value

    def meta(self, meta: Mapping[str, Any]) -> dict[str, Any]:
        """Header facts, redacted (`None` values are left out), and the hashes of the identities."""

        shown = {
            str(key): value
            if isinstance(value, bool | int | float)
            else self.clean(value, SHORT_CHARS, flatten=True)
            for key, value in meta.items()
            if value is not None
        }
        refs = {f"{key}_ref": identity_ref(meta[key]) for key in IDENTITY_KEYS if meta.get(key)}
        return {**shown, **refs}

    def _short(self, value: Any) -> str:
        return self.clean(value, SHORT_CHARS, flatten=True)

    def encode(self, event: RunEvent) -> dict[str, Any] | None:
        """The line for `event`, or `None` when it is not recorded (`run_completed`)."""

        handler = self._handlers.get(type(event))
        if handler is None:  # the run's end is already recorded by `agent_finished`
            return None
        record: dict[str, Any] = {
            "type": event.type,
            "ts": iso(event.timestamp),
            "run_id": event.run_id,
            "agent": event.agent,
            "depth": event.depth,
        }
        if self._with_root:
            record["root_run_id"] = event.root_run_id
        handler(event, record)
        return {key: value for key, value in record.items() if value is not None}

    def _copy(self, e: RunEvent, r: dict[str, Any]) -> None:
        """Copy the trace fields `EVENT_FIELDS` names for the event's type."""

        facts = EVENT_FIELDS[type(e)]
        for marks, key, attribute in parse_fields(facts.trace or ""):
            content = attribute in facts.content.split() and "#" not in marks
            if content and not self.full:
                continue
            value = field_value(e, attribute)
            if value is None or ("?" in marks and value == ""):
                continue
            if "#" in marks:
                value = len(value)
            elif "~" in marks:
                value = self._short(value)
            elif content:
                value = self.clean(value, BODY_CHARS)
            elif isinstance(value, tuple):
                value = list(value)
            r[key] = value

    def _tool_call(self, e: ToolCallEvent, r: dict[str, Any]) -> None:
        r.update(tool=e.tool_name, call_id=e.call_id)
        arguments: Mapping[str, Any] = e.arguments
        hands_over = e.tool_name in _HAND_OVER_TOOLS or e.tool_name.startswith(_HAND_OVER_PREFIX)
        if not self.full and hands_over:
            key = _HAND_OVER_TOOLS.get(e.tool_name)
            arguments = {key: arguments[key]} if key is not None and key in arguments else {}
        text = json.dumps(self.redact(_bounded(arguments)), ensure_ascii=False, default=str)
        if self.full and len(text) <= BODY_CHARS:
            r["arguments"] = json.loads(text)
        else:
            r["arguments_preview"] = preview(text, BODY_CHARS if self.full else ARGUMENT_PREVIEW)

    def _error(self, e: ErrorEvent, r: dict[str, Any]) -> None:
        r.update(error_type=e.error_type, message=self.clean(e.message, ERROR_CHARS, flatten=True))
        if e.stop is not None:
            r["stop"] = e.stop

    def _approval_requested(self, e: ApprovalRequestedEvent, r: dict[str, Any]) -> None:
        r.update(
            tool=e.tool,
            interrupt_id=e.interrupt_id,
            args_preview=self.clean(e.args_preview, ARGUMENT_PREVIEW, flatten=True),
            reason=self._short(e.reason) if e.reason else None,
            expires_at=iso(e.expires_at) if e.expires_at else None,
        )

    def _step(self, e: StepEvent, r: dict[str, Any]) -> None:
        self._copy(e, r)
        if e.checks:
            r["checks"] = [
                {
                    "id": str(check.get("id", "")),
                    "kind": str(check.get("kind", "")),
                    "passed": bool(check.get("passed")),
                    **({"message": self._short(check["message"])} if check.get("message") else {}),
                }
                for check in e.checks
            ]
