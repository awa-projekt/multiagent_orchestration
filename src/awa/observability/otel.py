"""Extension: OpenTelemetry bridge, every agent system as a span tree in your tracing backend.

Spans follow the GenAI semantic conventions (`invoke_agent`/`invoke_workflow`, `chat`,
`execute_tool`); docs/develop/observability.md#opentelemetry lists every span and attribute.
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from awa._internal.install import install_hint
from awa._internal.logging import get_logger
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    ApprovalRequestedEvent,
    DelegationResultEvent,
    ErrorEvent,
    HandoffEvent,
    ModelResponseEvent,
    RunCompletedEvent,
    RunEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.contracts.usage import ModelPrice, PriceTable, Usage, cost_or_none, model_call_cost
from awa.errors import ConfigurationError, OptionalDependencyError
from awa.kernel.redaction import SECRETS_REDACTOR, Redactor, RegexRedactor, redact_mapping
from awa.observability._trace_encode import EVENT_FIELDS, EventFields, field_value, parse_fields
from awa.runtime.context import ACTOR_METADATA_KEY, current_run_context

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.metrics import Histogram, Meter
    from opentelemetry.trace import Link, Span, SpanContext, Tracer

AttributeValue = (
    str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
)
"""A value OpenTelemetry accepts as a span attribute."""

DEFAULT_MAX_CONTENT_CHARS = 4000
"""Default truncation length for captured content attributes."""

PII_REDACTOR = RegexRedactor.pii()
"""Masks credentials and personal data; the default `redactor` of the trace and log sinks."""


_UNKNOWN_PROVIDER = "unknown"
"""`gen_ai.provider.name` of a model response whose provider is not known (the attribute is required)."""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_METADATA_PREFIX = "awa."
_MAX_METADATA_CHARS = 256
# Run metadata keys with a meaning for tracing (set by `run_observed` and by callers of `run`).
_THREAD_KEY = "awa.thread_id"
_ROLE_KEY = "awa.agent_role"
_SPEC_PREFIX = "awa.agent."  # the agent spec of an agent run (awa.runtime._run)
_DEDUP_WINDOW = 64
_USAGE_TOTALS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "tool_calls",
)
_logger = get_logger("otel")

# Bucket boundaries the GenAI metric conventions advise.
_TOKEN_BUCKETS = tuple(float(4**power) for power in range(14))
_DURATION_BUCKETS = tuple(0.01 * 2**power for power in range(14))

# Provider names reported by chat model integrations, mapped to the GenAI
# conventions' well-known `gen_ai.provider.name` values.
_PROVIDER_ALIASES: Mapping[str, str] = {
    "openai": "openai",
    "azure": "azure.ai.openai",
    "azure_openai": "azure.ai.openai",
    "azure_ai": "azure.ai.inference",
    "anthropic": "anthropic",
    "bedrock": "aws.bedrock",
    "bedrock_converse": "aws.bedrock",
    "amazon_bedrock": "aws.bedrock",
    "google_genai": "gcp.gemini",
    "google": "gcp.gemini",
    "gemini": "gcp.gemini",
    "google_vertexai": "gcp.vertex_ai",
    "vertexai": "gcp.vertex_ai",
    "mistralai": "mistral_ai",
    "mistral": "mistral_ai",
    "cohere": "cohere",
    "groq": "groq",
    "deepseek": "deepseek",
    "xai": "x_ai",
    "perplexity": "perplexity",
    "ibm": "ibm.watsonx.ai",
    "watsonx": "ibm.watsonx.ai",
}

# Model-name prefixes that identify the provider when the response does not say.
_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("chatgpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("claude", "anthropic"),
    ("gemini", "gcp.gemini"),
    ("mistral", "mistral_ai"),
    ("mixtral", "mistral_ai"),
    ("codestral", "mistral_ai"),
    ("command", "cohere"),
    ("deepseek", "deepseek"),
    ("grok", "x_ai"),
)


_TRACE_ID_MEMORY = 10_000
"""How many run ids `OpenTelemetrySubscriber.trace_id` remembers (the most recent ones)."""

_OPENAI_PROVIDERS = frozenset({"openai", "azure.ai.openai"})


def resolve_redactor(redactor: Redactor | None) -> Redactor | None:
    """`redactor`, except that the default one masks credentials only while the project
    switched `guard.redaction` off. Any other value (or `None`) is returned as given."""

    if redactor is PII_REDACTOR:
        from awa.plugins.switches import is_off

        if is_off("guard.redaction"):
            return SECRETS_REDACTOR
    return redactor


def langfuse_usage_types(provider: str | None) -> tuple[str, str]:
    """The Langfuse usage types of cache read and write tokens for `provider` (as its model definitions price them)."""

    read = "input_cached_tokens" if provider in _OPENAI_PROVIDERS else "input_cache_read"
    return read, "input_cache_creation"


def provider_name(provider: str | None, model: str | None) -> str | None:
    """The `gen_ai.provider.name` for a reported provider and model name, or `None`.

    Without a reported provider, a `provider:` prefix or a well-known model-name prefix decides.
    """

    if provider:
        key = provider.strip().lower()
        return _PROVIDER_ALIASES.get(key, key)
    if model:
        name = model.strip().lower().rsplit("/", 1)[-1]
        if ":" in name:
            prefix, _, rest = name.partition(":")
            if prefix in _PROVIDER_ALIASES:
                return _PROVIDER_ALIASES[prefix]
            name = rest
        for prefix, value in _MODEL_PREFIXES:
            if name.startswith(prefix):
                return value
    return None


def _ns(timestamp: datetime) -> int:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return ((timestamp - _EPOCH) // timedelta(microseconds=1)) * 1000


def _model_attributes(provider: str | None, model: str | None) -> dict[str, AttributeValue]:
    """The `chat` operation, provider and model attributes of a model response."""

    attributes: dict[str, AttributeValue] = {"gen_ai.operation.name": "chat"}
    if provider is not None:
        attributes["gen_ai.provider.name"] = provider
    if model:
        attributes["gen_ai.request.model"] = model
        attributes["gen_ai.response.model"] = model
    return attributes


@dataclass
class _RunState:
    span: Span
    agent: str
    root_run_id: str
    parent_run_id: str | None
    local_root: bool
    last_timestamp: datetime
    trace_attributes: dict[str, AttributeValue] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str | None = None
    children: list[str] = field(default_factory=list)
    tools: dict[str, Span] = field(default_factory=dict)  # open tool spans by call id
    tool_names: dict[str, str] = field(default_factory=dict)  # tool name by call id


class OpenTelemetrySubscriber:
    """Run-event subscriber that exports runs as OpenTelemetry spans.

    Pass it as a hook; it sees that run and everything nested in it. Spans are always ended
    (leftovers marked `awa.incomplete=True`), a span active when a top-level run starts becomes its
    parent, and it never raises into the run. Every span carries the scalar `awa.*` run metadata,
    redacted and cut; the thread id becomes `session.id`, the actor `user.id`.

    Args:
        tracer: Defaults to the global tracer provider's `awa` tracer.
        capture_content: Record prompts, outputs, tool bodies, tasks, messages and exception
            messages (off: names, ids, counts and timing only).
        attributes: Extra attributes set on every span.
        max_content_chars: Captured text longer than this is cut (`DEFAULT_MAX_CONTENT_CHARS`).
        prices: Prices used to add `awa.usage.cost` to the top-level span.
        redactor: Applied to captured text before it is cut; the default masks credentials and
            personal data, `None` sends it unchanged.
        meter: Records `gen_ai.client.token.usage` and `gen_ai.client.operation.duration`.
        session_id: Set on every span as `session.id` and `gen_ai.conversation.id`.
        user_id: Set on every span as `user.id`.
        tags: Set on every span as `langfuse.trace.tags`.
        trace_name: `langfuse.trace.name`; defaults to the top-level run's name.
        langfuse: Also describe spans the way Langfuse draws them (observation types, costs, event
            child spans, run metadata as trace metadata, levels).

    Raises:
        OptionalDependencyError: If `opentelemetry-api` is not installed.

    Example:
        ```python
        agent = Agent("assistant", tools=[search], hooks=[OpenTelemetrySubscriber()])
        with trace.get_tracer("app").start_as_current_span("handle_request"):
            await agent.run("Find the latest release notes.")
        ```
    """

    def __init__(
        self,
        tracer: Tracer | None = None,
        *,
        capture_content: bool = False,
        attributes: Mapping[str, AttributeValue] | None = None,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        prices: PriceTable | None = None,
        redactor: Redactor | None = PII_REDACTOR,
        meter: Meter | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        tags: Sequence[str] = (),
        trace_name: str | None = None,
        langfuse: bool = False,
    ) -> None:
        try:
            import opentelemetry.trace as trace
        except ImportError as exc:
            raise OptionalDependencyError(
                f"The OpenTelemetry bridge requires the optional extra: {install_hint('otel')}"
            ) from exc
        if max_content_chars < 1:
            raise ConfigurationError(
                f"max_content_chars must be at least 1, got {max_content_chars!r}"
            )
        from awa.plugins.switches import is_off

        component = "tracer.langfuse" if langfuse else "tracer.otel"
        if is_off(component):
            raise ConfigurationError(
                f"tracing is switched off for this project ({component}); remove it from "
                "the blueprint's `components:` to switch it on"
            )
        # `guard.redaction` off: personal data is no longer masked in span metadata.
        self._mask_personal = not is_off("guard.redaction")
        if tracer is None:
            from awa._internal.version import __version__

            tracer = trace.get_tracer("awa", __version__)
        self._tracer = tracer
        self._capture = capture_content
        self._attributes: dict[str, AttributeValue] = dict(attributes or {})
        self._has_session = bool(session_id)
        self._has_user = bool(user_id)
        if session_id:
            self._attributes["session.id"] = session_id
            self._attributes["gen_ai.conversation.id"] = session_id
        if user_id:
            self._attributes["user.id"] = user_id
        if tags:
            self._attributes["langfuse.trace.tags"] = list(tags)
        if trace_name:
            self._attributes["langfuse.trace.name"] = trace_name
        self._langfuse = langfuse
        self._max_chars = max_content_chars
        self._prices = prices
        self._redactor = resolve_redactor(redactor)
        self._trace_ids: dict[str, str] = {}
        self._token_usage: Histogram | None = None
        self._duration: Histogram | None = None
        if meter is not None:
            self._token_usage = meter.create_histogram(
                "gen_ai.client.token.usage",
                unit="{token}",
                description="Number of input and output tokens used.",
                explicit_bucket_boundaries_advisory=_TOKEN_BUCKETS,
            )
            self._duration = meter.create_histogram(
                "gen_ai.client.operation.duration",
                unit="s",
                description="GenAI operation duration.",
                explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
            )
        self._runs: dict[str, _RunState] = {}
        # Last span context of each agent run, keyed by (parent run id, agent), for handoff links.
        self._last_runs: dict[tuple[str, str], SpanContext] = {}
        self._handoff_links: dict[tuple[str, str], SpanContext] = {}
        # The same event object can reach this subscriber twice when it is a hook of
        # both a team and one of its members; recent events are skipped by identity.
        self._recent: deque[RunEvent] = deque(maxlen=_DEDUP_WINDOW)
        self._lock = threading.RLock()

    @property
    def open_spans(self) -> int:
        """Number of agent-run and tool-call spans that have not ended yet."""

        with self._lock:
            return sum(1 + len(run.tools) for run in self._runs.values())

    def trace_id(self, run_id: str) -> str | None:
        """The trace id (32 hex) of run `run_id`'s span, among the most recent runs; `None` when not traced."""

        return self._trace_ids.get(run_id)

    def __call__(self, event: RunEvent) -> None:
        """Update the span tree with `event`. Never raises."""

        try:
            with self._lock:
                if any(seen is event for seen in self._recent):
                    return
                self._recent.append(event)
                self._handle(event)
        except Exception:  # noqa: BLE001 - tracing must never fail the traced run
            _logger.exception("OpenTelemetry subscriber failed on %s", event.type)

    def close(self) -> None:
        """End every span that is still open (for example at shutdown)."""

        with self._lock:
            now = datetime.now(UTC)
            self._end_tree(None, now)
            for run_id in list(self._runs):
                self._end_run(run_id, now, incomplete=True)

    def _handle(self, event: RunEvent) -> None:
        if isinstance(event, AgentStartedEvent):
            self._on_started(event)
        elif isinstance(event, ModelResponseEvent):
            self._on_model_response(event)
        elif isinstance(event, ToolCallEvent):
            self._on_tool_call(event)
        elif isinstance(event, ToolResultEvent):
            self._on_tool_result(event)
        elif isinstance(event, AgentFinishedEvent):
            self._on_finished(event)
        elif isinstance(event, ErrorEvent):
            self._on_error(event)
        elif isinstance(event, RunCompletedEvent):
            self._end_tree(event.root_run_id, event.timestamp)
        elif (facts := EVENT_FIELDS.get(type(event))) is not None and facts.span is not None:
            self._add_event(event, f"awa.{event.type}", self._span_fields(event, facts))
            if isinstance(event, ApprovalRequestedEvent):
                self._mark_waiting(event)
            elif isinstance(event, HandoffEvent):
                source = self._last_runs.get((event.run_id, event.source))
                if source is not None:
                    self._handoff_links[(event.run_id, event.target)] = source
            elif isinstance(event, DelegationResultEvent):
                run = self._runs.get(event.run_id)
                if run is not None:
                    run.last_timestamp = max(run.last_timestamp, event.timestamp)

    def _span_fields(self, event: RunEvent, facts: EventFields) -> dict[str, AttributeValue]:
        """The attributes of the event's span event, as `EVENT_FIELDS` names them."""

        attributes: dict[str, AttributeValue] = {}
        for marks, key, attribute in parse_fields(facts.span or ""):
            captured = "+" in marks or attribute in facts.content.split()
            if captured and not self._capture:
                continue
            value = field_value(event, attribute)
            if "?" in marks and value in (None, ""):
                continue
            if isinstance(value, datetime):
                value = value.isoformat()
            elif "@" in marks:
                value = self._personal(value)
            elif captured:
                value = self._content(value)
            absolute = key.startswith(("gen_ai.", "messaging."))
            attributes[key if absolute else f"awa.{key}"] = value
        return attributes

    def _on_started(self, event: AgentStartedEvent) -> None:
        if event.run_id in self._runs:
            return
        run = self._open_run(event, event.parent_run_id)
        if self._capture and event.input_preview:
            run.span.set_attribute(
                "gen_ai.input.messages", self._messages("user", event.input_preview)
            )

    def _open_run(self, event: RunEvent, parent_run_id: str | None) -> _RunState:
        from opentelemetry import trace

        parent = self._runs.get(parent_run_id) if parent_run_id else None
        started = event if isinstance(event, AgentStartedEvent) else None
        context: Context | None = trace.set_span_in_context(parent.span) if parent else None
        links: list[Link] = []
        # A run started by a tool call (a delegation, an agent called as a tool) stays a child
        # of the calling agent's span (GenAI agent spans nest agents directly) and links to
        # the `execute_tool` span of that call, so the trace shows which call made it.
        tool_span = (
            parent.tools.get(started.tool_call_id)
            if parent is not None and started is not None and started.tool_call_id
            else None
        )
        if tool_span is not None:
            links.append(trace.Link(tool_span.get_span_context(), {"awa.link": "tool_call"}))
        if parent_run_id is not None:
            source = self._handoff_links.pop((parent_run_id, event.agent), None)
            if source is not None:
                links.append(trace.Link(source, {"awa.link": "handoff"}))
        # Teams, networks and workflows coordinate nested runs: `invoke_workflow`.
        coordinating = isinstance(event, AgentStartedEvent) and event.kind != "agent"
        operation = "invoke_workflow" if coordinating else "invoke_agent"
        metadata = self._run_metadata(event)
        trace_attributes = (
            parent.trace_attributes if parent is not None else self._trace_metadata(event, metadata)
        )
        role = None if coordinating else metadata.get(_ROLE_KEY)
        spec = (
            {}
            if coordinating
            else {k: v for k, v in metadata.items() if k.startswith(_SPEC_PREFIX)}
        )
        span = self._tracer.start_span(
            f"{operation} {event.agent}",
            context=context,
            kind=trace.SpanKind.INTERNAL,
            attributes=self._attrs(
                trace_attributes,
                {
                    **(self._role_attributes(role) if role is not None else {}),
                    **spec,
                    "gen_ai.operation.name": operation,
                    "gen_ai.agent.name": event.agent,
                    **({"gen_ai.workflow.name": event.agent} if coordinating else {}),
                    **self._observation_type("chain" if coordinating else "agent"),
                    **(
                        {"awa.run_kind": event.kind} if isinstance(event, AgentStartedEvent) else {}
                    ),
                    **self._identity(event),
                    **({"awa.parent_run_id": parent_run_id} if parent_run_id else {}),
                    **self._call_attributes(started),
                },
            ),
            links=links,
            start_time=_ns(event.timestamp),
        )
        state = _RunState(
            span=span,
            agent=event.agent,
            root_run_id=event.root_run_id,
            parent_run_id=parent_run_id if parent else None,
            local_root=parent is None,
            last_timestamp=event.timestamp,
            trace_attributes=trace_attributes,
        )
        self._runs[event.run_id] = state
        self._trace_ids[event.run_id] = format(span.get_span_context().trace_id, "032x")
        while len(self._trace_ids) > _TRACE_ID_MEMORY:
            self._trace_ids.pop(next(iter(self._trace_ids)))
        if parent is not None:
            parent.children.append(event.run_id)
        return state

    def _run_for(self, event: RunEvent) -> _RunState | None:
        """The run's state, or the open state of its root when the run was not seen."""

        return self._runs.get(event.run_id) or self._runs.get(event.root_run_id)

    def _on_finished(self, event: AgentFinishedEvent) -> None:
        run = self._ensure_run(event)
        if self._capture and event.output_preview:
            run.span.set_attribute(
                "gen_ai.output.messages",
                self._messages("assistant", event.output_preview, finish_reason="stop"),
            )
        if event.depth == 0:
            self._set_totals(run.span, event.usage)
        self._end_run(event.run_id, event.timestamp)

    def _on_error(self, event: ErrorEvent) -> None:
        from opentelemetry.trace import Status, StatusCode

        run = self._ensure_run(event)
        run.span.set_attribute("error.type", event.error_type)
        details: dict[str, AttributeValue] = {"exception.type": event.error_type}
        if self._capture:  # an error text can quote the prompt or the arguments
            details["exception.message"] = self._content(event.message)
        run.span.add_event("exception", details, timestamp=_ns(event.timestamp))
        if event.stop is not None:
            # Why a bound stopped the run: names, numbers and digests only (no content).
            run.span.set_attributes(_stop_attributes(event.stop))
        run.span.set_status(Status(StatusCode.ERROR, event.error_type))
        self._set_level(run.span, "ERROR", str(details.get("exception.message", event.error_type)))
        self._end_run(event.run_id, event.timestamp)

    def _ensure_run(self, event: RunEvent) -> _RunState:
        run = self._runs.get(event.run_id)
        if run is None:
            # End event without a start: open (and end) a span at the event time.
            parent = self._runs.get(event.root_run_id)
            run = self._open_run(event, event.root_run_id if parent else None)
        return run

    def _end_run(self, run_id: str, timestamp: datetime, *, incomplete: bool = False) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        # Nested runs still open end first, so their usage rolls up into this run.
        for child_id in run.children:
            if child_id in self._runs:
                self._end_run(child_id, timestamp, incomplete=True)
        del self._runs[run_id]
        for tool_span in run.tools.values():
            tool_span.set_attribute("awa.incomplete", True)
            tool_span.end(end_time=_ns(timestamp))
        run.tools.clear()
        run.tool_names.clear()
        # Langfuse adds up the usage of every observation of a trace, so with its attributes on
        # the roll-up of a run (its calls and everything nested) must not look like model usage.
        usage_prefix = "awa.subtree" if self._langfuse else "gen_ai.usage"
        run.span.set_attribute(f"{usage_prefix}.input_tokens", run.input_tokens)
        run.span.set_attribute(f"{usage_prefix}.output_tokens", run.output_tokens)
        if incomplete:
            run.span.set_attribute("awa.incomplete", True)
        run.span.end(end_time=_ns(timestamp))
        parent = self._runs.get(run.parent_run_id) if run.parent_run_id else None
        if parent is not None:
            parent.input_tokens += run.input_tokens
            parent.output_tokens += run.output_tokens
            parent.last_timestamp = max(parent.last_timestamp, timestamp)
            self._last_runs[(run.parent_run_id or "", run.agent)] = run.span.get_span_context()
        if run.local_root:
            self._forget_finished()

    def _end_tree(self, root_run_id: str | None, timestamp: datetime) -> None:
        """End the local root runs of `root_run_id` (of every tree for `None`) as incomplete."""

        roots = [
            run_id
            for run_id, run in self._runs.items()
            if run.local_root and root_run_id in (None, run.root_run_id)
        ]
        for run_id in roots:
            self._end_run(run_id, timestamp, incomplete=True)

    def _forget_finished(self) -> None:
        """Drop handoff bookkeeping whose parent run is no longer open."""

        open_ids = set(self._runs)
        self._last_runs = {k: v for k, v in self._last_runs.items() if k[0] in open_ids}
        self._handoff_links = {k: v for k, v in self._handoff_links.items() if k[0] in open_ids}

    def _set_totals(self, span: Span, usage: Usage) -> None:
        for name in _USAGE_TOTALS:
            span.set_attribute(f"awa.usage.{name}", getattr(usage, name))
        cost = cost_or_none(usage, self._prices)
        if cost is not None:
            span.set_attribute("awa.usage.cost", cost)
        elif self._prices is not None:
            _logger.warning("Could not price the run usage for its span")

    def _on_model_response(self, event: ModelResponseEvent) -> None:
        from opentelemetry import trace

        run = self._run_for(event)
        if run is None:
            return
        model = event.model
        provider = provider_name(event.provider, model)
        finish = event.finish_reason or ("tool_call" if event.tool_call_names else "stop")
        model_attributes = _model_attributes(provider or _UNKNOWN_PROVIDER, model)
        attributes = self._attrs(
            run.trace_attributes,
            {**model_attributes, **self._usage_attributes(event, finish), **self._identity(event)},
        )
        if event.tool_call_names:
            attributes["awa.tool_call_names"] = list(event.tool_call_names)
        if event.prefix_hash:
            attributes["awa.prompt.prefix_hash"] = event.prefix_hash
        if event.replayed:
            attributes["awa.cache.replayed"] = True
        if event.duration_ms is not None:
            attributes["awa.model.duration_ms"] = event.duration_ms
        attributes.update(self._observation_type("generation"))
        attributes.update(self._cost_attributes(event, provider))
        if self._capture:
            attributes["gen_ai.output.messages"] = self._messages(
                "assistant", event.content, finish_reason=finish, tool_calls=event.tool_call_names
            )
        start = min(run.last_timestamp, event.timestamp)
        if event.duration_ms is not None:
            # The call itself, not the preparation before it, is the generation's latency.
            start = max(start, event.timestamp - timedelta(milliseconds=event.duration_ms))
        span = self._tracer.start_span(
            f"chat {model}" if model else "chat",
            context=trace.set_span_in_context(run.span),
            kind=trace.SpanKind.CLIENT,
            attributes=attributes,
            start_time=_ns(start),
        )
        span.end(end_time=_ns(event.timestamp))
        if event.run_id in self._runs:
            run.input_tokens += event.input_tokens
            run.output_tokens += event.output_tokens
            if provider is not None and run.provider is None:
                run.provider = provider
                run.span.set_attribute("gen_ai.provider.name", provider)
                if model:
                    run.span.set_attribute("gen_ai.request.model", model)
        run.last_timestamp = max(run.last_timestamp, event.timestamp)
        if self._token_usage is not None and self._duration is not None:
            duration = (event.timestamp - start).total_seconds()
            self._duration.record(max(0.0, duration), model_attributes)
            for token_type, tokens in (
                ("input", event.input_tokens),
                ("output", event.output_tokens),
            ):
                self._token_usage.record(
                    tokens, {**model_attributes, "gen_ai.token.type": token_type}
                )

    @staticmethod
    def _usage_attributes(event: ModelResponseEvent, finish: str) -> dict[str, AttributeValue]:
        """Token counts, finish reason and cache figures of one model call."""

        attributes: dict[str, AttributeValue] = {
            "gen_ai.usage.input_tokens": event.input_tokens,
            "gen_ai.usage.output_tokens": event.output_tokens,
            "gen_ai.response.finish_reasons": [finish],
        }
        # Part of the input tokens; Langfuse prices them as their own buckets.
        if event.cache_read_tokens:
            attributes["gen_ai.usage.cache_read.input_tokens"] = event.cache_read_tokens
        if event.cache_write_tokens:
            attributes["gen_ai.usage.cache_creation.input_tokens"] = event.cache_write_tokens
        if event.input_tokens:
            attributes["awa.usage.cache_hit_ratio"] = round(
                event.cache_read_tokens / event.input_tokens, 4
            )
        if event.cleared_tool_results:
            attributes["awa.context.cleared_tool_results"] = event.cleared_tool_results
        return attributes

    def _cost_attributes(
        self, event: ModelResponseEvent, provider: str | None
    ) -> dict[str, AttributeValue]:
        """The call's cost, and the Langfuse usage details when exporting to Langfuse."""

        attributes: dict[str, AttributeValue] = {}
        cost = model_call_cost(event, self._prices)
        if cost is not None:
            attributes["awa.usage.cost"] = cost
            if self._langfuse:
                attributes["gen_ai.usage.cost"] = cost
        if self._langfuse:
            attributes.update(self._langfuse_usage(event, provider))
        return attributes

    def _on_tool_call(self, event: ToolCallEvent) -> None:
        run = self._run_for(event)
        if run is None or event.call_id in run.tools:
            return
        span = self._start_tool(run, event)
        if self._capture:
            span.set_attribute("gen_ai.tool.call.arguments", self._json(event.arguments))
        run.tools[event.call_id] = span
        run.tool_names[event.call_id] = event.tool_name

    def _on_tool_result(self, event: ToolResultEvent) -> None:
        from opentelemetry.trace import Status, StatusCode

        run = self._run_for(event)
        if run is None:
            return
        span = run.tools.pop(event.call_id, None)
        run.tool_names.pop(event.call_id, None)
        if span is None:
            span = self._start_tool(run, event)
            if self._capture and event.arguments is not None:
                span.set_attribute("gen_ai.tool.call.arguments", self._json(event.arguments))
        if event.cache is not None:
            span.set_attribute("awa.cache.hit", event.cache == "hit")
        if event.is_error:
            span.set_attribute("error.type", "tool_error")
            description = self._content(event.content) if self._capture else "tool_error"
            span.set_status(Status(StatusCode.ERROR, description))
            self._set_level(span, "ERROR", description)
        elif self._capture:
            span.set_attribute("gen_ai.tool.call.result", self._content(event.content))
        span.end(end_time=_ns(event.timestamp))
        run.last_timestamp = max(run.last_timestamp, event.timestamp)

    def _start_tool(self, run: _RunState, event: ToolCallEvent | ToolResultEvent) -> Span:
        from opentelemetry import trace

        return self._tracer.start_span(
            f"execute_tool {event.tool_name}",
            context=trace.set_span_in_context(run.span),
            kind=trace.SpanKind.INTERNAL,
            attributes=self._attrs(
                run.trace_attributes,
                {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.agent.name": event.agent,
                    "gen_ai.tool.name": event.tool_name,
                    "gen_ai.tool.type": "function",
                    **self._observation_type("tool"),
                    **({"gen_ai.tool.call.id": event.call_id} if event.call_id else {}),
                    **self._identity(event),
                },
            ),
            start_time=_ns(event.timestamp),
        )

    def _attrs(
        self, shared: Mapping[str, AttributeValue], own: Mapping[str, AttributeValue]
    ) -> dict[str, AttributeValue]:
        """A span's attributes: the subscriber's, the run tree's `shared` ones, then its `own`."""

        return {**self._attributes, **shared, **own}

    def _add_event(self, event: RunEvent, name: str, attributes: dict[str, AttributeValue]) -> None:
        run = self._run_for(event)
        if run is not None:
            details = {**attributes, "awa.agent": event.agent}
            run.span.add_event(name, details, timestamp=_ns(event.timestamp))
            if self._langfuse:
                self._event_observation(run, name, details, event.timestamp)

    def _event_observation(
        self, run: _RunState, name: str, details: dict[str, AttributeValue], timestamp: datetime
    ) -> None:
        """Langfuse drops span events, so each one also becomes an instant `event` observation."""

        from opentelemetry import trace

        span = self._tracer.start_span(
            name,
            context=trace.set_span_in_context(run.span),
            kind=trace.SpanKind.INTERNAL,
            attributes=self._attrs(
                run.trace_attributes,
                {
                    **details,
                    **self._observation_type("event"),
                    # Langfuse shows an event's input on its page: the same details, readable.
                    "langfuse.observation.input": json.dumps(
                        {key.removeprefix("awa."): value for key, value in details.items()},
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ),
            start_time=_ns(timestamp),
        )
        span.end(end_time=_ns(timestamp))

    def _run_metadata(self, event: RunEvent) -> dict[str, AttributeValue]:
        """The scalar `awa.*` metadata of the event's own run, redacted and cut (empty outside it)."""

        context = current_run_context()
        if context is None or context.run_id != event.run_id:
            return {}
        values: dict[str, AttributeValue] = {}
        for key, value in context.metadata.items():
            if not isinstance(key, str) or not key.startswith(_METADATA_PREFIX):
                continue
            if key == ACTOR_METADATA_KEY and not isinstance(value, str | bool | int | float):
                found = (
                    value.get("id") if isinstance(value, Mapping) else getattr(value, "id", None)
                )
                value = None if found is None else str(found)
            if isinstance(value, bool | int | float):
                values[key] = value
            elif isinstance(value, str) and value:
                values[key] = self._personal(value)[:_MAX_METADATA_CHARS]
        return values

    def _personal(self, text: str) -> str:
        """`text` that may name a person (a reviewer, metadata), redacted; without content
        capture personal data such as an email address is masked too."""

        if self._redactor is not None:
            text = self._redactor.redact(text)
        if not self._capture and self._mask_personal:
            text = PII_REDACTOR.redact(text)
        return text

    def _trace_metadata(
        self, event: RunEvent, metadata: Mapping[str, AttributeValue]
    ) -> dict[str, AttributeValue]:
        """The attributes every span of a run tree carries, from its top-level run's metadata."""

        shared = {
            key: value
            for key, value in metadata.items()
            if key != _ROLE_KEY and not key.startswith(_SPEC_PREFIX)
        }
        attributes = dict(shared)
        # The run tree's session (given by the caller, else its thread, else a new one);
        # the subscriber's own `session_id` wins.
        session = event.session_id if isinstance(event, AgentStartedEvent) else None
        thread = attributes.get(_THREAD_KEY)
        if not session and isinstance(thread, str):
            session = thread
        if session and not self._has_session and "session.id" not in self._attributes:
            attributes["session.id"] = session
            # A thread named after the run (an automation run) would read as a fallback value.
            fallback = session in (event.run_id, event.root_run_id)
            if not fallback and "gen_ai.conversation.id" not in self._attributes:
                attributes["gen_ai.conversation.id"] = session
        actor = attributes.get(ACTOR_METADATA_KEY)
        if isinstance(actor, str) and not self._has_user:
            attributes["user.id"] = actor
        if self._langfuse:
            for key, value in shared.items():
                name = key.removeprefix(_METADATA_PREFIX).replace(".", "_")
                attributes[f"langfuse.trace.metadata.{name}"] = value
            attributes["langfuse.trace.metadata.run_id"] = event.root_run_id
            if "langfuse.trace.name" not in self._attributes:
                attributes["langfuse.trace.name"] = event.agent
        return attributes

    @staticmethod
    def _call_attributes(event: AgentStartedEvent | None) -> dict[str, AttributeValue]:
        """The ids of the call that started a run: `awa.agent_call_id`, `awa.branch`, ..."""

        if event is None:
            return {}
        values = {
            "awa.session_id": event.session_id,
            "awa.agent_call_id": event.agent_call_id,
            "awa.tool_call_id": event.tool_call_id,
            "awa.branch": event.branch,
        }
        return {key: value for key, value in values.items() if value}

    def _role_attributes(self, role: AttributeValue) -> dict[str, AttributeValue]:
        """The declared role of an agent run, also as Langfuse observation metadata."""

        attributes: dict[str, AttributeValue] = {_ROLE_KEY: role}
        if self._langfuse:
            attributes["langfuse.observation.metadata.agent_role"] = role
        return attributes

    def _set_level(self, span: Span, level: str, message: str) -> None:
        """Langfuse's level and status message of an observation (only with Langfuse attributes)."""

        if self._langfuse:
            span.set_attribute("langfuse.observation.level", level)
            span.set_attribute("langfuse.observation.status_message", message)

    def _mark_waiting(self, event: ApprovalRequestedEvent) -> None:
        """Mark the open call of the tool that waits for an approval."""

        run = self._run_for(event)
        if run is None:
            return
        for call_id, name in run.tool_names.items():
            if name == event.tool and call_id in run.tools:
                run.tools[call_id].set_attribute("awa.waiting_for_approval", True)
                self._set_level(run.tools[call_id], "WARNING", "waiting for approval")

    def _observation_type(self, kind: str) -> dict[str, AttributeValue]:
        """`langfuse.observation.type` when Langfuse attributes are on."""

        return {"langfuse.observation.type": kind} if self._langfuse else {}

    def _langfuse_usage(
        self, event: ModelResponseEvent, provider: str | None
    ) -> dict[str, AttributeValue]:
        """Langfuse's usage buckets (uncached, cache read, cache write) and, with prices, their costs."""

        read_key, write_key = langfuse_usage_types(provider)
        cached = event.cache_read_tokens
        written = event.cache_write_tokens
        details = {
            "input": max(0, event.input_tokens - cached - written),
            "output": event.output_tokens,
            **({read_key: cached} if cached else {}),
            **({write_key: written} if written else {}),
        }
        attributes: dict[str, AttributeValue] = {
            "langfuse.observation.usage_details": json.dumps(details)
        }
        if event.input_tokens:
            attributes["langfuse.observation.metadata.cache_hit_ratio"] = round(
                cached / event.input_tokens, 4
            )
        price = self._price_for(event.model)
        if price is not None:
            read, write = price.cache_read_per_mtok, price.cache_write_per_mtok
            rates = {
                "input": price.input_per_mtok,
                "output": price.output_per_mtok,
                read_key: price.input_per_mtok if read is None else read,
                write_key: price.input_per_mtok if write is None else write,
            }
            costs = {key: tokens * rates[key] / 1_000_000 for key, tokens in details.items()}
            costs["total"] = math.fsum(costs.values())
            attributes["langfuse.observation.cost_details"] = json.dumps(costs)
        return attributes

    def _price_for(self, model: str | None) -> ModelPrice | None:
        """The price of `model` in the subscriber's prices, `None` when it has none."""

        if self._prices is None or not model:
            return None
        try:
            return self._prices.price_for(model)
        except Exception:  # noqa: BLE001 - a missing price must not lose the span
            return None

    @staticmethod
    def _identity(event: RunEvent) -> dict[str, AttributeValue]:
        return {
            "awa.run_id": event.run_id,
            "awa.root_run_id": event.root_run_id,
            "awa.depth": event.depth,
        }

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        return text[: max(0, self._max_chars - 3)] + "..."

    def _content(self, text: str) -> str:
        """Redact, then truncate, a piece of recorded text."""

        if self._redactor is not None:
            text = self._redactor.redact(text)
        return self._truncate(text)

    def _json(self, value: Any) -> str:
        if self._redactor is not None:
            value = redact_mapping(value, self._redactor)
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
        if self._redactor is not None:  # `default=str` can print objects the mapping walk skipped
            text = self._redactor.redact(text)
        return self._truncate(text)

    def _messages(
        self,
        role: str,
        text: str,
        *,
        finish_reason: str | None = None,
        tool_calls: Sequence[str] = (),
    ) -> str:
        """One GenAI message as JSON: `text` (if any) and `tool_calls` as parts."""

        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "text", "content": self._content(text)})
        parts.extend({"type": "tool_call", "name": name} for name in tool_calls)
        message: dict[str, Any] = {"role": role, "parts": parts}
        if finish_reason is not None:
            message["finish_reason"] = finish_reason
        return json.dumps([message], ensure_ascii=False)


def _stop_attributes(stop: Mapping[str, Any]) -> dict[str, AttributeValue]:
    """The span attributes of a stop diagnosis (`awa.stop.*`)."""

    attributes: dict[str, AttributeValue] = {}
    for key in ("stop_reason", "category", "bound", "agent", "tool", "suggested_fix", "source"):
        value = stop.get(key)
        if isinstance(value, str) and value:
            attributes[f"awa.stop.{key}"] = value
    for key in ("value", "maximum"):
        value = stop.get(key)
        if isinstance(value, int | float):
            attributes[f"awa.stop.{key}"] = float(value)
    steps = stop.get("last_steps")
    if isinstance(steps, list) and steps:
        attributes["awa.stop.last_steps"] = [str(step) for step in steps]
    return attributes


__all__ = ["DEFAULT_MAX_CONTENT_CHARS", "OpenTelemetrySubscriber", "provider_name"]
