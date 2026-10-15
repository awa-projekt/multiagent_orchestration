"""Loop protection of a run tree: call-chain cycles, repeated calls, per-tool limits, budgets.

A repeated coordination call (`REPEAT_GUARDED_TOOLS`) gets a steering text, later repeats an
error. `LoopState` is shared by every run of a tree and saved on a pause, so a resume never
resets a counter.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from awa.contracts.usage import Usage, UsageLimits
from awa.runtime.ids import AgentKey

REPEAT_GUARDED_TOOLS = frozenset({"delegate_task", "consult_peer", "send_message"})
"""awa's coordination tools whose identical repeats are refused (user tools may poll)."""

REPEAT_LIMIT = 3
"""The call count at which an identical coordination call becomes an error (the 2nd is steered)."""

DEPTH_WARNING = 8
"""Nesting depth beyond which a run tree without `UsageLimits.max_depth` logs a warning."""


@dataclass(eq=False)
class LoopState:
    """Counters of one run tree that bound its loops; shared by every run of the tree.

    Attributes:
        signatures: How often each run made each guarded call, by `(run id, signature)`.
        counted_calls: Ids of the counted tool calls: a call resumed after a pause counts once.
    """

    tool_calls: dict[str, int] = field(default_factory=dict)
    counted_calls: set[str] = field(default_factory=set)
    signatures: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    depth_warned: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def count_tool(self, name: str, call_id: str = "") -> int:
        """Count the call `call_id` of tool `name` unless it is counted already; return the count."""

        with self._lock:
            if call_id not in self.counted_calls:
                self.counted_calls.update([call_id] if call_id else [])
                self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
            return self.tool_calls[name]

    def count_signature(self, run_id: str, signature: str, turn: int) -> int:
        """Note the call `signature` by run `run_id` in model turn `turn`; return in how many
        turns the run made it. Identical calls of one turn (parallel work) count once."""

        with self._lock:
            turns = self.signatures.setdefault((run_id, signature), set())
            turns.add(turn)
            return len(turns)

    def snapshot(self) -> dict[str, Any]:
        """The counters to save with a paused run (JSON-serializable)."""

        with self._lock:
            return {
                "tool_calls": dict(self.tool_calls),
                "counted_calls": sorted(self.counted_calls),
                "signatures": [
                    [run_id, signature, sorted(turns)]
                    for (run_id, signature), turns in self.signatures.items()
                ],
            }

    def restore(self, saved: Mapping[str, Any] | None) -> None:
        """Continue the counters `snapshot` saved (counts only grow)."""

        calls = (saved or {}).get("tool_calls")
        signatures = (saved or {}).get("signatures")
        counted = (saved or {}).get("counted_calls")
        with self._lock:
            self.counted_calls.update(map(str, counted if isinstance(counted, list) else []))
            if isinstance(calls, Mapping):
                for name, count in calls.items():
                    if isinstance(count, int):
                        self.tool_calls[str(name)] = max(self.tool_calls.get(str(name), 0), count)
            for item in signatures if isinstance(signatures, list) else []:
                if isinstance(item, list) and len(item) == 3 and isinstance(item[2], list):
                    turns = self.signatures.setdefault((str(item[0]), str(item[1])), set())
                    turns.update(t for t in item[2] if isinstance(t, int))


def call_signature(name: str, arguments: Mapping[str, Any]) -> str:
    """A canonical text of one tool call: the name and the arguments, keys sorted."""

    try:
        args = json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args = repr(sorted(arguments.items()))
    return f"{name}:{' '.join(args.split()).casefold()}"


def cycle_refusal(
    chain: tuple[AgentKey, ...], link: AgentKey, *, strict: bool = False
) -> str | None:
    """Why calling `link` from `chain` would be a cycle, or `None`.

    By default only the same agent with the same brief on the chain is a cycle, so consult-back
    stays allowed; with `strict`, any agent already on the chain is refused.
    """

    for key in chain:
        same_agent = key.agent == link.agent and key.scope == link.scope
        if same_agent and (strict or (link.brief_hash and key.brief_hash == link.brief_hash)):
            what = "is already working on this request" if not strict else "is already on the chain"
            return (
                f"Not started: {link.agent!r} {what} further up the call chain "
                f"({' -> '.join(k.label() for k in (*chain, link))}); calling it again would "
                "loop. Use the result you have, or do this part yourself."
            )
    return None


@dataclass(eq=False)
class DelegationBudget:
    """The usage limits of one delegated subtree; its usage also counts into the tree."""

    call_id: str
    usage: Usage
    limits: UsageLimits


__all__ = [
    "DEPTH_WARNING",
    "REPEAT_GUARDED_TOOLS",
    "REPEAT_LIMIT",
    "DelegationBudget",
    "LoopState",
    "call_signature",
    "cycle_refusal",
]
