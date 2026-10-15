"""Read a trace as a tree: which agent did what, in plain text (`render_trace`) or as JSON.

A resumed run's replayed agents merge with the first part, so every agent appears once. Cost
comes from the header's prices (or those passed) and ignores provider cache discounts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awa._internal.util import preview
from awa.contracts.usage import UNKNOWN_MODEL, ModelPrice, PriceTable, Usage
from awa.errors import MissingPriceError
from awa.observability.trace_store import TraceData, read_trace

TREE_FORMAT = "awa.trace.tree/v1"
"""Format version of `TraceTree.to_dict()`."""

MAX_NESTING = 100
"""Deepest run nesting the view follows; a trace that claims more is shown flat from there."""

# Terminal control characters (C0 and C1, also ESC and OSC): a trace holds what models and tools
# said, so the text view never passes them on. Line feed and tab stay.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass
class Item:
    """One thing a node did, in order: a tool call, a delegation, a handoff, an approval ..."""

    kind: str
    info: dict[str, Any] = field(default_factory=dict)
    child: Node | None = None
    hidden: bool = False
    segment: int = 0
    started: datetime | None = None


@dataclass
class Node:
    """One agent (or team, network, workflow) of the run tree."""

    agent: str
    kind: str
    depth: int
    parent: Node | None = None
    run_ids: list[str] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    pending: list[Item] = field(default_factory=list)
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    unpriced_calls: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)
    paused: bool = False

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def children(self) -> list[Node]:
        return [item.child for item in self.items if item.child is not None]

    def walk(self) -> list[Node]:
        nodes = [self]
        for child in self.children():
            nodes.extend(child.walk())
        return nodes


@dataclass
class TraceTree:
    """The run tree of one trace file.

    Attributes:
        roots: The top-level runs (one, unless the trace holds several runs).
        problems: What went wrong: errors, blocked calls, limits, cancellations.
    """

    data: TraceData
    roots: list[Node]
    totals: dict[str, Any]
    problems: list[str]
    priced: bool

    def to_dict(self) -> dict[str, Any]:
        """The tree as JSON-compatible data."""

        return {
            "format": TREE_FORMAT,
            "file": str(self.data.path),
            "level": self.data.level,
            "meta": self.data.meta,
            "root_run_id": self.data.header.get("root_run_id"),
            "outcome": self.data.outcome,
            "totals": self.totals,
            "problems": self.problems,
            "truncated": self.data.truncated,
            "roots": [_node_dict(root, self.priced) for root in self.roots],
        }

    def render(self) -> str:
        """The tree as plain text for a person."""

        return _render(self)


def build_tree(data: TraceData, *, prices: PriceTable | None = None) -> TraceTree:
    """Build the run tree of a trace (from `read_trace`); `prices` default to the header's."""

    table = prices
    stored = data.header.get("prices")
    if table is None and isinstance(stored, dict):
        try:
            table = PriceTable.model_validate(stored)
        except ValueError:
            table = None
    builder = _Builder(table)
    for record in data.events:
        builder.add(record)
    builder.finish()
    return TraceTree(
        data=data,
        roots=builder.roots,
        totals=builder.totals(),
        problems=builder.problems,
        priced=table is not None,
    )


def render_trace(
    source: TraceData | TraceTree | str | Path, *, prices: PriceTable | None = None
) -> str:
    """The text view of a trace (a file, its `TraceData` or its tree)."""

    if isinstance(source, TraceTree):
        return source.render()
    data = source if isinstance(source, TraceData) else read_trace(source)
    return build_tree(data, prices=prices).render()


_NOTES: dict[str, tuple[str, str]] = {
    "message_undelivered": ("", "message to {recipient} never delivered ({reason})"),
    "error": ("ERROR ", "{error_type}: {message}"),
    "model_retry": ("", "model {model} failed ({kind}), retry {attempt} in {delay} s"),
    "model_fallback": ("", "model {from_model} gave up ({reason}), using {to_model}"),
    "guardrail_blocked": ("BLOCKED ", "guardrail {guardrail} blocked the {stage}{where}: {reason}"),
    "limit_exceeded": ("LIMIT ", "limit {limit} reached ({value} > {maximum})"),
    "run_cancelled": ("STOPPED ", "run stopped: {reason} after {elapsed}"),
}
"""Events shown as one note line: its prefix (a note with a prefix is a problem) and its text."""

_COUNTED = (
    "model_calls",
    "tool_calls",
    "delegations",
    "consults",
    "handoffs",
    "approvals_requested",
    "approvals_decided",
    "errors",
    "model_retries",
)
"""The counts of `TraceTree.totals`, in output order."""
_STEP_FIELDS = ("step", "index", "total", "title", "status", "checks", "message", "attempt", "case")


class _NoteFields(dict[str, Any]):
    """A record's fields for a note text, formatted for reading; a missing field reads `None`."""

    def __init__(self, rec: dict[str, Any]) -> None:
        super().__init__(
            rec,
            recipient=rec.get("recipient") or "?",
            where=f" on tool {rec['tool']}" if rec.get("tool") else "",
            value=_num(rec.get("value")),
            maximum=_num(rec.get("maximum")),
            elapsed=_ms(_int(rec.get("elapsed_ms"))),
        )

    def __missing__(self, key: str) -> None:
        return None


class _Builder:
    def __init__(self, prices: PriceTable | None) -> None:
        self._prices = prices
        self.roots: list[Node] = []
        self.problems: list[str] = []
        self._canon: dict[str, Node] = {}
        self._calls: dict[str, Item] = {}
        self._delegations: dict[str, Item] = {}
        self._approvals: dict[str, Item] = {}
        self._steps: dict[tuple[int, str], Item] = {}
        self._by_agent: dict[str, Node] = {}
        self._segment = -1
        self._spans: dict[tuple[str, int], list[datetime]] = {}
        self._finished: set[tuple[str, int]] = set()
        self._problem_texts: set[str] = set()
        self._counts = dict.fromkeys(_COUNTED, 0)
        self._models: set[str] = set()

    def add(self, rec: dict[str, Any]) -> None:
        kind = rec.get("type")
        if kind == "agent_started":
            self._on_agent_started(rec)
        elif isinstance(kind, str) and kind in _NOTES:
            self._note(rec, self._node(rec))
        elif (handler := getattr(self, f"_on_{kind}", None)) is not None:
            handler(rec, self._node(rec))
        else:
            return
        when = _when(rec)
        if when is not None:
            span = self._spans.setdefault((str(rec.get("run_id")), self._segment), [when, when])
            span[1] = max(span[1], when)

    def _node(self, rec: dict[str, Any]) -> Node:
        run_id = str(rec.get("run_id") or "")
        node = self._canon.get(run_id)
        if node is None:  # an event of a run whose start was not recorded
            node = Node(str(rec.get("agent") or "?"), "agent", _int(rec.get("depth")))
            node.run_ids.append(run_id)
            self._canon[run_id] = node
            self.roots.append(node)
            self._by_agent[node.agent] = node
        return node

    def _on_agent_started(self, rec: dict[str, Any]) -> None:
        agent = str(rec.get("agent") or "?")
        kind = str(rec.get("kind") or "agent")
        parent = self._canon.get(str(rec.get("parent_run_id") or ""))
        if parent is not None and _nesting(parent) >= MAX_NESTING:
            parent = None
        depth = _int(rec.get("depth"))
        node: Node | None = None
        if parent is None and depth == 0:
            last = self.roots[-1] if self.roots else None
            if last is not None and last.paused and last.agent == agent:
                node = last  # the run was resumed after a pause
            else:
                node = Node(agent, kind, depth)
                self.roots.append(node)
            self._segment += 1
        elif parent is None:
            node = Node(agent, kind, depth)
            self.roots.append(node)
        else:
            pending = next((i for i in parent.pending if i.info.get("target") == agent), None)
            if pending is not None:
                parent.pending.remove(pending)
                node = Node(agent, kind, depth, parent)
                pending.child = node
            else:
                node = next((c for c in parent.children() if c.agent == agent and c.paused), None)
                if node is None:
                    node = Node(agent, kind, depth, parent)
                    parent.items.append(Item("run", {"agent": agent}, child=node))
        node.paused = False
        run_id = str(rec.get("run_id") or "")
        if run_id not in node.run_ids:
            node.run_ids.append(run_id)
        self._canon[run_id] = node
        self._by_agent[agent] = node
        if rec.get("input") and not any(i.kind == "input" for i in node.items):
            node.items.insert(0, Item("input", {"text": rec["input"]}))

    def _on_model_response(self, rec: dict[str, Any], node: Node) -> None:
        node.model_calls += 1
        node.input_tokens += _int(rec.get("input_tokens"))
        node.output_tokens += _int(rec.get("output_tokens"))
        self._counts["model_calls"] += 1
        if rec.get("model"):
            self._models.add(str(rec["model"]))
        if rec.get("content"):
            node.items.append(Item("said", {"text": rec["content"]}, segment=self._segment))
        price = self._price(rec.get("model"))
        if price is not None:
            usage = Usage(
                input_tokens=_int(rec.get("input_tokens")),
                output_tokens=_int(rec.get("output_tokens")),
            )
            node.cost += price.cost(usage)
        elif self._prices is not None:
            node.unpriced_calls += 1

    def _price(self, model: Any) -> ModelPrice | None:
        if self._prices is None:
            return None
        try:
            return self._prices.price_for(str(model or UNKNOWN_MODEL))
        except MissingPriceError:
            return None

    def _on_tool_call(self, rec: dict[str, Any], node: Node) -> None:
        self._counts["tool_calls"] += 1
        item = Item(
            "tool",
            {
                "tool": rec.get("tool"),
                "call_id": rec.get("call_id"),
                "arguments": rec.get("arguments"),
                "arguments_preview": rec.get("arguments_preview"),
                "status": "no result recorded",
            },
            segment=self._segment,
            started=_when(rec),
        )
        node.items.append(item)
        self._calls[str(rec.get("call_id"))] = item

    def _on_tool_result(self, rec: dict[str, Any], node: Node) -> None:
        call_id = str(rec.get("call_id"))
        item = self._calls.get(call_id)
        if item is None:
            item = Item(
                "tool", {"tool": rec.get("tool"), "call_id": call_id}, segment=self._segment
            )
            node.items.append(item)
        failed = bool(rec.get("is_error"))
        item.info["status"] = "error" if failed else "ok"
        if failed:
            item.info["error"] = rec.get("error") or ""
            self._problem(node, f"tool {rec.get('tool')} failed: {rec.get('error') or 'error'}")
        if rec.get("content"):
            item.info["result"] = rec["content"]
        item.info["result_chars"] = rec.get("result_chars")
        ended = _when(rec)
        if item.segment == self._segment and item.started is not None and ended is not None:
            item.info["latency_ms"] = int((ended - item.started).total_seconds() * 1000)
        else:
            item.info["waited"] = True

    def _on_delegation(self, rec: dict[str, Any], node: Node) -> None:
        mode = str(rec.get("mode") or "delegate")
        self._counts["consults" if mode == "consult" else "delegations"] += 1
        item = Item(
            "delegation",
            {
                "mode": mode,
                "target": rec.get("target"),
                "task": rec.get("task"),
                "status": "no result recorded",
            },
            segment=self._segment,
        )
        _hide_request_call(node)
        node.items.append(item)
        node.pending.append(item)
        self._delegations[str(rec.get("correlation_id"))] = item

    def _on_delegation_result(self, rec: dict[str, Any], node: Node) -> None:
        item = self._delegations.get(str(rec.get("correlation_id")))
        if item is None:
            return
        item.info["status"] = rec.get("status") or "ok"
        if rec.get("content"):
            item.info["result"] = rec["content"]
        if item.info["status"] == "error":
            self._problem(node, f"{item.info['mode']} of {item.info['target']} failed")

    def _on_handoff(self, rec: dict[str, Any], node: Node) -> None:
        self._counts["handoffs"] += 1
        target = rec.get("target")
        item = Item(
            "handoff",
            {"source": rec.get("source"), "target": target, "reason": rec.get("reason")},
            segment=self._segment,
        )
        node.items.append(item)
        node.pending.append(item)
        giver = self._by_agent.get(str(rec.get("source")))
        if giver is not None:
            for earlier in reversed(giver.items):
                if (
                    earlier.kind == "tool"
                    and str(earlier.info.get("tool")) == f"transfer_to_{target}"
                ):
                    earlier.hidden = True
                    break

    def _on_approval_requested(self, rec: dict[str, Any], node: Node) -> None:
        self._counts["approvals_requested"] += 1
        item = Item(
            "approval",
            {
                "tool": rec.get("tool"),
                "interrupt_id": rec.get("interrupt_id"),
                "args_preview": rec.get("args_preview"),
                "decision": None,
            },
            segment=self._segment,
        )
        node.items.append(item)
        self._approvals[str(rec.get("interrupt_id"))] = item
        walker: Node | None = node
        while walker is not None:
            walker.paused = True
            walker = walker.parent

    def _on_approval_decided(self, rec: dict[str, Any], node: Node) -> None:
        self._counts["approvals_decided"] += 1
        item = self._approvals.get(str(rec.get("interrupt_id")))
        if item is None:
            item = Item("approval", {"tool": rec.get("tool")}, segment=self._segment)
            node.items.append(item)
        for key in ("decision", "reviewer", "note", "latency_ms", "remembered", "expired"):
            if rec.get(key) not in (None, "", False):
                item.info[key] = rec[key]

    def _on_step(self, rec: dict[str, Any], node: Node) -> None:
        # A resumed run replays its start (new run ids): one line per step of one workflow node.
        key = (id(node), str(rec.get("step")))
        item = self._steps.get(key)
        if item is None:
            item = Item("step", {}, segment=self._segment)
            node.items.append(item)
            self._steps[key] = item
        for field_name in _STEP_FIELDS:
            if field_name in rec:
                item.info[field_name] = rec[field_name]
        item.info["step_agent"] = rec.get("agent")
        if rec.get("status") == "failed":
            self._problem(
                node, f"step {rec.get('step')} failed: {rec.get('message') or 'a check failed'}"
            )

    def _on_agent_message(self, rec: dict[str, Any], node: Node) -> None:
        node.items.append(Item("message", dict(rec), segment=self._segment))

    def _on_agent_finished(self, rec: dict[str, Any], node: Node) -> None:
        node.duration_ms += _int(rec.get("duration_ms"))
        self._finished.add((str(rec.get("run_id")), self._segment))
        said = next((i for i in reversed(node.items) if i.kind == "said"), None)
        if rec.get("output") and not (said is not None and said.info["text"] == rec["output"]):
            node.items.append(Item("output", {"text": rec["output"]}, segment=self._segment))

    def _note(self, rec: dict[str, Any], node: Node) -> None:
        kind = rec["type"]
        prefix, template = _NOTES[kind]
        text = template.format_map(_NoteFields(rec))
        if kind == "error":
            self._counts["errors"] += 1
            node.errors.append(text)
        elif kind == "model_retry":
            self._counts["model_retries"] += 1
        node.items.append(Item("note", {"text": prefix + text}, segment=self._segment))
        if prefix:
            self._problem(node, text)

    def _problem(self, node: Node, text: str) -> None:
        # The same error passes through every run above the one that raised it: say it once.
        if text not in self._problem_texts:
            self._problem_texts.add(text)
            self.problems.append(f"{node.agent}: {text}")

    def finish(self) -> None:
        # A run that ended without `agent_finished` (it failed) is timed by its own events.
        for (run_id, segment), (first, last) in self._spans.items():
            node = self._canon.get(run_id)
            if node is not None and (run_id, segment) not in self._finished:
                node.duration_ms += int((last - first).total_seconds() * 1000)
        for root in self.roots:
            if not root.paused:
                continue
            for node in root.walk():
                for item in node.items:
                    if item.kind == "approval" and not item.info.get("decision"):
                        item.info["status"] = "waiting"

    def totals(self) -> dict[str, Any]:
        nodes = [n for root in self.roots for n in root.walk()]
        input_tokens = sum(n.input_tokens for n in nodes)
        output_tokens = sum(n.output_tokens for n in nodes)
        work = sum(1 for n in nodes for i in n.items if i.kind == "tool" and not i.hidden)
        cost: float | None = None
        complete = True
        if self._prices is not None:
            cost = sum(n.cost for n in nodes)
            complete = not any(n.unpriced_calls for n in nodes)
        return {
            **self._counts,
            "agents": len(nodes),
            "work_tool_calls": work,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tokens": input_tokens + output_tokens,
            "cost": cost,
            "cost_complete": complete,
            "duration_ms": sum(root.duration_ms for root in self.roots),
            "models": sorted(self._models),
        }


def _hide_request_call(node: Node) -> None:
    """Hide the tool call that carried a delegation request: the delegation line shows it."""

    open_calls = [
        i
        for i in node.items
        if i.kind == "tool" and not i.hidden and i.info.get("status") == "no result recorded"
    ]
    named = [i for i in open_calls if i.info.get("tool") in ("delegate_task", "consult_peer")]
    for item in (named or open_calls)[-1:]:
        item.hidden = True


def _num(value: Any) -> str:
    return f"{value:g}" if isinstance(value, int | float) else str(value)


def _when(rec: dict[str, Any]) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(rec["ts"]))
    except (KeyError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _int(value: Any) -> int:
    """`value` as a whole number; a damaged or hostile field counts as 0."""

    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _nesting(node: Node) -> int:
    levels = 0
    while node.parent is not None:
        levels += 1
        node = node.parent
    return levels


def _item_dict(item: Item, priced: bool) -> dict[str, Any]:
    info = {k: v for k, v in item.info.items() if v not in (None, "")}
    data: dict[str, Any] = {"type": item.kind, **info}
    if item.hidden:
        data["coordination"] = True
    if item.child is not None:
        data["run"] = _node_dict(item.child, priced)
    return data


def _node_dict(node: Node, priced: bool) -> dict[str, Any]:
    return {
        "agent": node.agent,
        "kind": node.kind,
        "run_ids": node.run_ids,
        "model_calls": node.model_calls,
        "input_tokens": node.input_tokens,
        "output_tokens": node.output_tokens,
        "tokens": node.tokens,
        "cost": node.cost if priced else None,
        "duration_ms": node.duration_ms,
        "errors": node.errors,
        "items": [_item_dict(item, priced) for item in node.items],
    }


def _flat(text: Any, limit: int = 300) -> str:
    """`text` on one line, cut for the text view (the JSON view keeps it whole)."""

    return preview(str(text), limit, flatten=True)


def _ms(value: int) -> str:
    if value < 1000:
        return f"{value} ms"
    return f"{value / 1000:.1f} s"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _stats(node: Node, priced: bool) -> str:
    parts = [_plural(node.model_calls, "model call") if node.model_calls else "no model calls"]
    if node.model_calls:
        parts.append(f"{node.tokens:,} tokens")
        if priced:
            parts.append(f"cost {node.cost:.4f}" + ("+" if node.unpriced_calls else ""))
    if node.duration_ms:
        parts.append(_ms(node.duration_ms))
    return ", ".join(parts)


def _render(tree: TraceTree) -> str:
    data, totals, meta = tree.data, tree.totals, tree.data.meta
    lines: list[str] = []
    run = data.header.get("root_run_id") or meta.get("process_run") or data.path.stem
    attempt = f", attempt {meta['attempt']}" if meta.get("attempt") else ""
    lines.append(f"Trace of run {run}{attempt}")
    about = [
        f"{key} {meta[key]}"
        for key in ("automation", "trigger", "thread_id", "tenant")
        if meta.get(key)
    ]
    if about:
        lines.append("  " + ", ".join(about))
    lines.append(f"  file {data.path} (level {data.level})")
    lines.append(f"Result: {_result(tree)}")
    cost = ""
    if totals["cost"] is not None:
        cost = f", cost {totals['cost']:.4f}"
        cost += "" if totals["cost_complete"] else " (some models have no price)"
    lines.append(
        f"Totals: {_plural(totals['model_calls'], 'model call')}, {totals['tokens']:,} tokens "
        f"({totals['input_tokens']:,} in, {totals['output_tokens']:,} out){cost}, "
        f"time {_ms(totals['duration_ms'])}"
    )
    handover = totals["tool_calls"] - totals["work_tool_calls"]
    lines.append(
        f"        agents {totals['agents']}, tool calls {totals['tool_calls']} "
        f"({handover} only hand work to other agents), delegations {totals['delegations']}, "
        f"consultations {totals['consults']}, handoffs {totals['handoffs']}, "
        f"approvals {totals['approvals_requested']} asked / {totals['approvals_decided']} answered"
    )
    if tree.problems:
        lines.append("")
        lines.append("What went wrong:")
        lines.extend(f"  - {problem}" for problem in tree.problems)
    lines.append("")
    for root in tree.roots:
        _render_node(root, 0, lines, tree.priced)
    if not tree.roots:
        lines.append("(no events were recorded)")
    notes = _footnotes(tree)
    if notes:
        lines.append("")
        lines.extend(notes)
    return _CONTROL.sub("", "\n".join(lines) + "\n")


def _result(tree: TraceTree) -> str:
    data = tree.data
    paused = any(root.paused for root in tree.roots)
    errors = [error for root in tree.roots for error in root.errors]
    if data.outcome == "failed" or errors:
        why = data.error or (errors[0] if errors else "")
        return f"failed ({why})" if why else "failed"
    if data.outcome == "cancelled":
        return "cancelled"
    if paused:
        return "paused, waiting for an approval"
    if data.outcome == "paused":
        return "paused, waiting for an answer"
    if not tree.roots:
        return "nothing recorded"
    if not data.ends:
        return "not finished (the trace has no closing line: the run is still going, or it crashed)"
    return "finished, with problems (see below)" if tree.problems else "finished"


def _render_node(node: Node, level: int, lines: list[str], priced: bool) -> None:
    lines.append(f"{'  ' * level}{node.kind} {_flat(node.agent)}  [{_stats(node, priced)}]")
    for item in node.items:
        _render_item(item, level + 1, lines, priced)


def _render_item(item: Item, level: int, lines: list[str], priced: bool) -> None:
    pad = "  " * level
    info = item.info
    if item.kind == "tool" and not item.hidden:
        args = info.get("arguments")
        shown = (
            json.dumps(args, ensure_ascii=False)
            if args is not None
            else _flat(info.get("arguments_preview") or "")
        )
        status = _flat(info.get("status"))
        extra = ""
        if info.get("latency_ms") is not None:
            extra = f" ({_ms(_int(info['latency_ms']))})"
        elif info.get("waited"):
            extra = " (after the approval)"
        suffix = f" -> {status}{extra}"
        lines.append(f"{pad}tool {_flat(info.get('tool'))} {shown}{suffix}")
        if info.get("error"):
            lines.append(f"{pad}  error: {_flat(info['error'])}")
        if info.get("result"):
            lines.append(f"{pad}  result: {_flat(info['result'])}")
    elif item.kind == "delegation":
        verb = "consults" if info.get("mode") == "consult" else "delegates to"
        task = f" - {_flat(info['task'])}" if info.get("task") else ""
        target = _flat(info.get("target"))
        lines.append(f"{pad}{verb} {target}{task} -> {_flat(info.get('status'))}")
        if item.child is not None:
            _render_node(item.child, level + 1, lines, priced)
        if info.get("result"):
            lines.append(f"{pad}  answer: {_flat(info['result'])}")
    elif item.kind == "handoff":
        reason = f" ({_flat(info['reason'])})" if info.get("reason") else ""
        lines.append(f"{pad}hands over to {_flat(info.get('target'))}{reason}")
        if item.child is not None:
            _render_node(item.child, level + 1, lines, priced)
    elif item.kind == "run" and item.child is not None:
        _render_node(item.child, level, lines, priced)
    elif item.kind == "approval":
        lines.append(f"{pad}approval for {_flat(info.get('tool'))}: {_verdict(info)}")
    elif item.kind == "step":
        _render_step(item, pad, lines)
    elif item.kind == "message":
        target = _flat(info.get("recipient") or info.get("topic") or "all")
        subject = f": {_flat(info['subject'])}" if info.get("subject") else ""
        sender = _flat(info.get("sender"))
        lines.append(f"{pad}message {_flat(info.get('kind'))} {sender} -> {target}{subject}")
    elif item.kind == "note":
        lines.append(f"{pad}! {_flat(info.get('text'))}")
    elif item.kind == "input":
        lines.append(f"{pad}input: {_flat(info.get('text'))}")
    elif item.kind == "said":
        lines.append(f"{pad}said: {_flat(info.get('text'))}")
    elif item.kind == "output":
        lines.append(f"{pad}output: {_flat(info.get('text'))}")


def _render_step(item: Item, pad: str, lines: list[str]) -> None:
    info = item.info
    number = f"{_int(info.get('index')) + 1}/{_int(info.get('total'))}"
    title = f" {_flat(info['title'])}" if info.get("title") else ""
    lines.append(
        f"{pad}step {number} {_flat(info.get('step'))}{title} ({_flat(info.get('step_agent'))}): "
        f"{str(info.get('status')).replace('_', ' ')}"
        + (f" (try {info['attempt']})" if info.get("attempt") else "")
        + (f" [{_flat(info['case'])}]" if info.get("case") else "")
    )
    for check in info.get("checks") or ():
        if isinstance(check, dict) and not check.get("passed", True):
            check_id = _flat(check.get("id"))
            lines.append(f"{pad}  check {check_id} failed: {_flat(check.get('message'))}")
    shown = ("failed", "retrying", "skipped", "rejected")
    if info.get("status") in shown and info.get("message"):
        lines.append(f"{pad}  {_flat(info['message'])}")


def _verdict(info: dict[str, Any]) -> str:
    decision = info.get("decision")
    if not decision:
        return "still waiting for a decision"
    words = {"approve": "approved", "reject": "rejected", "edit": "approved with edited arguments"}
    text = words.get(str(decision), str(decision))
    if info.get("expired"):
        text = "expired, treated as rejected"
    who = f" by {info['reviewer']}" if info.get("reviewer") else ""
    after = f" after {_ms(_int(info['latency_ms']))}" if info.get("latency_ms") else ""
    note = f" ({_flat(info['note'])})" if info.get("note") else ""
    remembered = " (remembered earlier decision)" if info.get("remembered") else ""
    return f"{text}{who}{after}{note}{remembered}"


def _footnotes(tree: TraceTree) -> list[str]:
    notes: list[str] = []
    data = tree.data
    if data.truncated:
        notes.append(
            f"Note: this trace was cut at its size limit; {data.dropped} event(s) are not in it."
        )
    if data.damaged:
        notes.append(f"Note: {data.damaged} line(s) of the file could not be read.")
    if data.level == "summary":
        notes.append(
            "Note: level summary keeps no message texts (prompts, replies, tool results). "
            "Record with level full to see them."
        )
    if not tree.priced:
        notes.append(
            "Note: no prices, so no cost. Add `prices:` to the blueprint or pass --prices FILE."
        )
    return notes


__all__ = ["TREE_FORMAT", "TraceTree", "build_tree", "render_trace"]
