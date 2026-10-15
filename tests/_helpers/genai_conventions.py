"""A validator of received spans against the OpenTelemetry GenAI semantic conventions.

The rules are the ones the conventions state for the spans awa emits: `invoke_agent`,
`invoke_workflow`, `chat` (the inference span) and `execute_tool`, taken from the GenAI spans and
agent-spans pages of semantic-conventions v1.44.0 (the documented target; see
`docs/develop/observability.md#opentelemetry`). Attribute names and the
well-known values are cross-checked against the installed `opentelemetry-semantic-conventions`
package in `known_values()`.

`validate(spans)` checks every rule on every span it applies to and returns a `Report`: the
violations, and how many times each rule was checked, so a test can see that no rule passed
only because nothing was there to check. Each rule is listed in `RULES` with its source.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from _helpers.otlp_collector import WireSpan

OPERATIONS = ("invoke_agent", "invoke_workflow", "chat", "execute_tool")
FINISH_REASONS = ("stop", "length", "content_filter", "tool_call", "error")
TOOL_TYPES = ("function", "extension", "datastore")
TOKEN_TYPES = ("input", "output")
MESSAGE_ROLES = ("system", "user", "assistant", "tool")
MESSAGE_PART_TYPES = ("text", "tool_call", "tool_call_response", "blob", "file", "uri", "reasoning")
USAGE_ATTRIBUTES = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")


@dataclass(frozen=True)
class Rule:
    id: str
    text: str
    source: str


RULES: tuple[Rule, ...] = (
    Rule(
        "G01",
        "gen_ai.operation.name is a well-known value of the span kind awa emits",
        "gen-ai-spans: operation names",
    ),
    Rule(
        "G02",
        "span name is `{operation} {name}`: `invoke_agent {agent}`, `invoke_workflow {workflow}`, `chat {model}` (`chat` without a model), `execute_tool {tool}`",
        "agent-spans, gen-ai-spans: Span name",
    ),
    Rule(
        "G03",
        "span kind: invoke_agent, invoke_workflow and execute_tool INTERNAL; chat CLIENT (INTERNAL allowed in process)",
        "agent-spans, gen-ai-spans: Span kind",
    ),
    Rule(
        "G04",
        "Required attributes: gen_ai.operation.name on every span; gen_ai.provider.name on chat; gen_ai.tool.name on execute_tool",
        "Requirement level: Required",
    ),
    Rule(
        "G05",
        "Conditionally required attributes: gen_ai.agent.name on invoke_agent and execute_tool, gen_ai.workflow.name on invoke_workflow, gen_ai.request.model on chat when the model is known",
        "Requirement level: Conditionally Required",
    ),
    Rule(
        "G06",
        "attribute types: token counts non-negative int, finish reasons a string array of known reasons, tool call id string, tool type function|extension|datastore, provider name non-empty string",
        "Attributes tables",
    ),
    Rule(
        "G07",
        "token usage only on chat, invoke_agent and invoke_workflow spans, never on execute_tool",
        "gen-ai-spans: usage attributes",
    ),
    Rule(
        "G08",
        "an ERROR span carries error.type (Conditionally Required) and a span that did not fail carries none",
        "Recording Errors",
    ),
    Rule(
        "G09",
        "gen_ai.conversation.id is never a fallback (not the trace id, not awa.run_id)",
        "gen_ai.conversation.id note",
    ),
    Rule(
        "G10",
        "gen_ai.input.messages / gen_ai.output.messages (Opt-In) are JSON of the messages schema; output messages carry finish_reason; tool arguments are JSON",
        "Capturing instructions, inputs, and outputs",
    ),
    Rule(
        "T01",
        "trace ids are 32 and span ids 16 hex digits, non-zero, span ids unique",
        "OpenTelemetry trace API",
    ),
    Rule(
        "T02",
        "every parent is a span of the same trace and every trace has exactly one root",
        "Span parent/child",
    ),
    Rule(
        "T03",
        "parent kinds: chat and execute_tool under invoke_agent and without children; invoke_agent under invoke_agent or invoke_workflow or a root; invoke_workflow under invoke_workflow, invoke_agent or a root",
        "agent -> chat -> execute_tool, nested agents for delegation",
    ),
    Rule("T04", "a span ends after it starts and lies inside its parent's interval", "Span times"),
    Rule(
        "T05",
        "invoke_workflow coordinates nested runs: it has at least one invoke_agent or invoke_workflow child",
        "agent-spans: Invoke workflow span",
    ),
    Rule(
        "T06",
        "a delegation shows as a nested agent span: every awa.delegation event of a span has a child invoke_agent of its target",
        "delegation as nested agent spans",
    ),
    Rule(
        "T07",
        "token usage rolls up: an agent or workflow span counts its own chat spans and its nested runs",
        "usage of a run and everything nested in it",
    ),
    Rule(
        "T08",
        "every span is ended cleanly: none is marked awa.incomplete, except the call of a tool that paused its run for an approval",
        "spans are always ended",
    ),
    Rule("T09", "span events are `exception` (with exception.type) or `awa.*`", "span events"),
    Rule(
        "T10",
        "instrumentation scope `awa` with a version, and a resource with service.name",
        "instrumentation scope",
    ),
)


@dataclass(frozen=True)
class Violation:
    rule: str
    span: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule} [{self.span}] {self.message}"


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    checks: Counter[str] = field(default_factory=Counter)

    @property
    def ok(self) -> bool:
        return not self.violations

    def failed(self, rule: str) -> list[Violation]:
        return [v for v in self.violations if v.rule == rule]

    def __str__(self) -> str:
        return "\n".join(str(v) for v in self.violations) or "no violations"


def known_values() -> dict[str, tuple[str, ...]]:
    """Attribute names and well-known values of the installed `opentelemetry-semantic-conventions`."""

    from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai

    return {
        "attributes": tuple(
            value
            for name, value in vars(gen_ai).items()
            if name.startswith("GEN_AI_") and isinstance(value, str)
        ),
        "operations": tuple(v.value for v in gen_ai.GenAiOperationNameValues),
        "providers": tuple(v.value for v in gen_ai.GenAiProviderNameValues),
        "token_types": tuple(v.value for v in gen_ai.GenAiTokenTypeValues),
    }


def _label(span: WireSpan) -> str:
    return f"{span.name} {span.span_id[:8]}"


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("not a string")
    return json.loads(value)


def _check_messages(value: Any, *, output: bool) -> str | None:
    """Why `value` is not a valid messages attribute (`None` when it is)."""

    try:
        messages = _json(value)
    except ValueError:
        return "not JSON"
    if not isinstance(messages, list) or not messages:
        return "not a non-empty array of messages"
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in MESSAGE_ROLES:
            return f"message without a known role: {message!r:.80}"
        parts = message.get("parts")
        if not isinstance(parts, list):
            return "message without parts"
        for part in parts:
            if not isinstance(part, dict) or part.get("type") not in MESSAGE_PART_TYPES:
                return f"part of unknown type: {part!r:.80}"
            if part["type"] == "text" and not isinstance(part.get("content"), str):
                return "text part without content"
            if part["type"] == "tool_call" and not isinstance(part.get("name"), str):
                return "tool_call part without name"
        if output and message.get("finish_reason") not in FINISH_REASONS:
            return "output message without a known finish_reason"
    return None


class _Checker:
    def __init__(self, spans: Sequence[WireSpan]) -> None:
        self.spans = list(spans)
        self.by_id = {s.span_id: s for s in self.spans}
        self.children: dict[str, list[WireSpan]] = defaultdict(list)
        for span in self.spans:
            if span.parent_id is not None:
                self.children[span.parent_id].append(span)
        self.report = Report()

    def check(self, rule: str, ok: bool, span: WireSpan | str, message: str) -> None:
        self.report.checks[rule] += 1
        if not ok:
            label = span if isinstance(span, str) else _label(span)
            self.report.violations.append(Violation(rule, label, message))

    def parent(self, span: WireSpan) -> WireSpan | None:
        return self.by_id.get(span.parent_id) if span.parent_id else None

    # -------------------------------------------------------------- per span

    def span_rules(self, span: WireSpan) -> None:
        attrs = span.attributes
        operation = span.operation
        self.check("G01", operation in OPERATIONS, span, f"operation {operation!r}")
        if operation not in OPERATIONS:
            return
        self.check(
            "G03",
            span.kind == ("CLIENT" if operation == "chat" else "INTERNAL")
            or (operation == "chat" and span.kind == "INTERNAL"),
            span,
            f"kind {span.kind}",
        )
        self._names(span, operation)
        self._required(span, operation)
        self._types(span, operation)
        self.check(
            "G07",
            operation != "execute_tool" or not any(key in attrs for key in USAGE_ATTRIBUTES),
            span,
            "token usage on a tool span",
        )
        failed = span.status_code == "ERROR"
        self.check(
            "G08",
            failed == ("error.type" in attrs),
            span,
            f"status {span.status_code}, error.type {attrs.get('error.type')!r}",
        )
        conversation = attrs.get("gen_ai.conversation.id")
        if conversation is not None:
            self.check(
                "G09",
                conversation not in (span.trace_id, attrs.get("awa.run_id")),
                span,
                "conversation id is a fallback value",
            )
        for key, output in (("gen_ai.input.messages", False), ("gen_ai.output.messages", True)):
            if key in attrs:
                problem = _check_messages(attrs[key], output=output)
                self.check("G10", problem is None, span, f"{key}: {problem}")
        for key in ("gen_ai.tool.call.arguments",):
            if key in attrs:
                try:
                    _json(attrs[key])
                    valid = True
                except ValueError:
                    valid = False
                self.check("G10", valid, span, f"{key} is not JSON")
        for event in span.events:
            named = event.name.startswith("awa.") or (
                event.name == "exception"
                and isinstance(event.attributes.get("exception.type"), str)
            )
            self.check("T09", named, span, f"event {event.name!r}")
        self.check(
            "T08",
            "awa.incomplete" not in attrs or self._paused_for_approval(span),
            span,
            "span was ended as incomplete",
        )
        self.check(
            "T10",
            span.scope_name == "awa"
            and bool(span.scope_version)
            and bool(span.resource.get("service.name")),
            span,
            f"scope {span.scope_name!r} {span.scope_version!r}, resource {dict(span.resource)}",
        )

    def _paused_for_approval(self, span: WireSpan) -> bool:
        """Is `span` the call of a tool that paused its run for an approval?"""

        parent = self.parent(span)
        return (
            span.operation == "execute_tool"
            and parent is not None
            and any(
                event.name == "awa.approval_requested"
                and event.attributes.get("awa.tool") == span.attributes.get("gen_ai.tool.name")
                for event in parent.events
            )
        )

    def _names(self, span: WireSpan, operation: str) -> None:
        attrs = span.attributes
        if operation == "invoke_agent":
            expected = f"invoke_agent {attrs.get('gen_ai.agent.name')}"
        elif operation == "invoke_workflow":
            expected = f"invoke_workflow {attrs.get('gen_ai.workflow.name')}"
        elif operation == "execute_tool":
            expected = f"execute_tool {attrs.get('gen_ai.tool.name')}"
        else:
            model = attrs.get("gen_ai.request.model")
            expected = f"chat {model}" if model else "chat"
        self.check("G02", span.name == expected, span, f"expected name {expected!r}")

    def _required(self, span: WireSpan, operation: str) -> None:
        attrs = span.attributes

        def present(rule: str, key: str) -> None:
            value = attrs.get(key)
            self.check(rule, isinstance(value, str) and bool(value), span, f"{key} missing")

        present("G04", "gen_ai.operation.name")
        if operation == "chat":
            present("G04", "gen_ai.provider.name")
        if operation == "execute_tool":
            present("G04", "gen_ai.tool.name")
            present("G05", "gen_ai.agent.name")
        if operation == "invoke_agent":
            present("G05", "gen_ai.agent.name")
        if operation == "invoke_workflow":
            present("G05", "gen_ai.workflow.name")

    def _types(self, span: WireSpan, operation: str) -> None:
        attrs = span.attributes
        for key in USAGE_ATTRIBUTES:
            if key in attrs:
                self.check("G06", _is_count(attrs[key]), span, f"{key} = {attrs[key]!r}")
        if operation == "chat":
            reasons = attrs.get("gen_ai.response.finish_reasons")
            self.check(
                "G06",
                isinstance(reasons, list)
                and bool(reasons)
                and all(r in FINISH_REASONS for r in reasons),
                span,
                f"finish reasons {reasons!r}",
            )
            self.check(
                "G06",
                all(key in attrs for key in USAGE_ATTRIBUTES),
                span,
                "chat span without token usage",
            )
        if operation == "execute_tool":
            self.check(
                "G06",
                attrs.get("gen_ai.tool.type") in TOOL_TYPES,
                span,
                f"tool type {attrs.get('gen_ai.tool.type')!r}",
            )
            if "gen_ai.tool.call.id" in attrs:
                self.check(
                    "G06",
                    isinstance(attrs["gen_ai.tool.call.id"], str)
                    and bool(attrs["gen_ai.tool.call.id"]),
                    span,
                    "tool call id",
                )

    # -------------------------------------------------------------- structure

    def structure_rules(self, span: WireSpan) -> None:
        operation = span.operation
        parent = self.parent(span)
        if span.parent_id is not None:
            self.check(
                "T02",
                parent is not None and parent.trace_id == span.trace_id,
                span,
                "parent is not a span of the same trace",
            )
        kids = self.children.get(span.span_id, [])
        self.check("T04", span.end_ns >= span.start_ns, span, "ends before it starts")
        if parent is not None:
            self.check(
                "T04",
                parent.start_ns <= span.start_ns and span.end_ns <= parent.end_ns,
                span,
                f"outside the interval of {parent.name}",
            )
        if operation in ("chat", "execute_tool"):
            self.check(
                "T03",
                parent is not None and parent.operation == "invoke_agent",
                span,
                f"parent is {parent.name if parent else 'none'}",
            )
            self.check("T03", not kids, span, "has children")
        elif operation == "invoke_agent":
            self.check(
                "T03",
                parent is None or parent.operation in ("invoke_agent", "invoke_workflow"),
                span,
                f"parent is {parent.name if parent else 'none'}",
            )
        elif operation == "invoke_workflow":
            self.check(
                "T03",
                parent is None or parent.operation in ("invoke_agent", "invoke_workflow"),
                span,
                f"parent is {parent.name if parent else 'none'}",
            )
            nested = [k for k in kids if k.operation in ("invoke_agent", "invoke_workflow")]
            self.check("T05", bool(nested), span, "no nested run")
        if operation in ("invoke_agent", "invoke_workflow"):
            self._delegations(span, kids)
            self._usage(span, kids)

    def _delegations(self, span: WireSpan, kids: list[WireSpan]) -> None:
        targets = Counter(
            kid.attributes.get("gen_ai.agent.name")
            for kid in kids
            if kid.operation == "invoke_agent"
        )
        for event in span.events:
            if event.name == "awa.delegation":
                target = event.attributes.get("awa.target")
                self.check(
                    "T06",
                    targets.get(target, 0) > 0,
                    span,
                    f"no nested agent span for the delegation to {target!r}",
                )

    def _usage(self, span: WireSpan, kids: list[WireSpan]) -> None:
        if "awa.incomplete" in span.attributes:
            return
        for key in USAGE_ATTRIBUTES:
            own = sum(
                kid.attributes[key]
                for kid in kids
                if kid.operation in ("chat", "invoke_agent", "invoke_workflow")
                and _is_count(kid.attributes.get(key))
            )
            self.check(
                "T07",
                span.attributes.get(key) == own,
                span,
                f"{key} is {span.attributes.get(key)!r}, its chat and nested spans sum to {own}",
            )

    def trace_rules(self) -> None:
        ids = Counter(s.span_id for s in self.spans)
        for span in self.spans:
            valid = (
                re.fullmatch(r"[0-9a-f]{32}", span.trace_id) is not None
                and re.fullmatch(r"[0-9a-f]{16}", span.span_id) is not None
                and int(span.trace_id, 16) != 0
                and int(span.span_id, 16) != 0
                and ids[span.span_id] == 1
            )
            self.check("T01", valid, span, "malformed or duplicate ids")
        roots: Counter[str] = Counter(s.trace_id for s in self.spans if s.parent_id is None)
        for trace_id in {s.trace_id for s in self.spans}:
            self.check(
                "T02",
                roots[trace_id] == 1,
                f"trace {trace_id[:8]}",
                f"{roots[trace_id]} root spans",
            )


def validate(spans: Sequence[WireSpan]) -> Report:
    """Check every rule of `RULES` on `spans`; the report lists violations and checks per rule."""

    checker = _Checker(spans)
    checker.trace_rules()
    for span in checker.spans:
        checker.span_rules(span)
        if span.operation in OPERATIONS:
            checker.structure_rules(span)
    return checker.report


def validate_metrics(metrics_data: Any) -> Report:
    """Check the GenAI client metrics of an SDK `MetricsData` (in-memory reader) against the rules."""

    report = Report()

    def check(rule: str, ok: bool, what: str, message: str) -> None:
        report.checks[rule] += 1
        if not ok:
            report.violations.append(Violation(rule, what, message))

    seen: dict[str, Any] = {}
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                seen[metric.name] = metric
    expected = {"gen_ai.client.token.usage": "{token}", "gen_ai.client.operation.duration": "s"}
    for name, unit in expected.items():
        metric = seen.get(name)
        check("M01", metric is not None, name, "metric not emitted")
        if metric is None:
            continue
        check("M01", metric.unit == unit, name, f"unit {metric.unit!r}, expected {unit!r}")
        points = metric.data.data_points
        check("M01", bool(points), name, "no data points")
        for point in points:
            attrs = dict(point.attributes)
            check(
                "M02",
                attrs.get("gen_ai.operation.name") == "chat",
                name,
                f"operation {attrs.get('gen_ai.operation.name')!r}",
            )
            provider = attrs.get("gen_ai.provider.name")
            check(
                "M02",
                isinstance(provider, str) and bool(provider),
                name,
                "gen_ai.provider.name missing",
            )
            if name == "gen_ai.client.token.usage":
                check(
                    "M02",
                    attrs.get("gen_ai.token.type") in TOKEN_TYPES,
                    name,
                    f"token type {attrs.get('gen_ai.token.type')!r}",
                )
            check(
                "M03", point.count > 0 and point.sum >= 0, name, "empty or negative histogram point"
            )
    return report


METRIC_RULES: tuple[Rule, ...] = (
    Rule(
        "M01",
        "gen_ai.client.token.usage (unit {token}) and gen_ai.client.operation.duration (unit s) are emitted as histograms",
        "gen-ai-metrics",
    ),
    Rule(
        "M02",
        "metric attributes: gen_ai.operation.name, gen_ai.provider.name (Required), gen_ai.token.type input|output on token usage",
        "gen-ai-metrics",
    ),
    Rule("M03", "histogram points are non-empty with a non-negative sum", "gen-ai-metrics"),
)


def summary(report: Report, rules: Sequence[Rule] = RULES) -> Mapping[str, int]:
    """How often each rule of `rules` was checked."""

    return {rule.id: report.checks[rule.id] for rule in rules}
