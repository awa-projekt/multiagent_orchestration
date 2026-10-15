"""State of one `AgentNetwork` run, and the progress record that lets a paused run resume."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from awa._internal.upstream.messages import BaseMessage
from awa.contracts.results import RunResult
from awa.contracts.usage import Usage, UsageLimits
from awa.errors import ConfigurationError
from awa.runtime.context import RunContext
from awa.runtime.termination import caller_limits
from awa.workflows._base import record_result, restore_result

StopReason = Literal["completed", "run_timeout", "idle_timeout", "max_lead_turns", "interrupted"]
"""Why a network run ended; `"interrupted"` when it paused for a human decision."""

ParkedKind = Literal["lead", "task", "message"]
"""What a paused agent run was doing: a lead turn, a task, or a message turn."""

_RECORD_VERSION = 1
# Counters and queues of a run that a resumed run continues with.
_SAVED = (
    "tag",
    "goal",
    "created",
    "attempts",
    "handbacks",
    "handback_count",
    "message_turns",
    "lead_turns",
    "lead_mail",
    "final_turn",
    "idle_expired",
)


@dataclass(eq=False)
class Parked:
    """An agent run of the network that paused on human-in-the-loop interrupts."""

    key: str
    kind: ParkedKind
    agent: str
    thread: str
    interrupts: list[dict[str, Any]]
    task_id: str | None = None


@dataclass(eq=False)
class NetworkRun:
    """State of one network run, shared by the coordinator and the workers.

    `prefix` names the threads of the run's agent runs (`<prefix>/lead`,
    `<prefix>/<task id>/<attempt>`); `tag` identifies the run's tasks (`data["awa.run"]`) across
    resumes.
    """

    ctx: RunContext[Any]
    thread: str | None = None
    prefix: str = ""
    tag: str = ""
    resources: Any = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    wake: dict[str, asyncio.Event] = field(default_factory=dict)
    busy: set[str] = field(default_factory=set)
    goal: list[BaseMessage] = field(default_factory=list)
    tasks: dict[str, None] = field(default_factory=dict)
    created: int = 0
    creating: int = 0
    lead_mail: bool = False
    attempts: dict[str, int] = field(default_factory=dict)
    retrying: set[str] = field(default_factory=set)
    handbacks: list[str] = field(default_factory=list)
    handback_count: int = 0
    message_turns: dict[str, int] = field(default_factory=dict)
    lead_turns: int = 0
    lead: RunResult[Any] | None = None
    fatal: BaseException | None = None
    stop_reason: StopReason = "completed"
    slots: asyncio.Semaphore | None = None
    final_turn: bool = False
    idle_expired: bool = False
    parked: dict[str, Parked] = field(default_factory=dict)
    resumes: dict[str, list[tuple[Parked, Any]]] = field(default_factory=dict)
    lead_resume: tuple[Parked, Any] | None = None
    resumed: bool = False

    @property
    def lead_thread(self) -> str | None:
        return f"{self.prefix}/lead" if self.thread is not None else None

    @property
    def lead_parked(self) -> bool:
        return "lead" in self.parked or self.lead_resume is not None

    def notify(self) -> None:
        """Wake the coordinator and every idle worker."""

        self.changed.set()
        for event in self.wake.values():
            event.set()

    def raise_fatal(self) -> None:
        if self.fatal is not None:
            raise self.fatal

    def take_handbacks(self) -> list[str]:
        blocks, self.handbacks = self.handbacks, []
        return blocks

    def park(
        self,
        kind: ParkedKind,
        agent: str,
        thread: str,
        result: RunResult[Any],
        task_id: str | None = None,
    ) -> None:
        key = "lead" if kind == "lead" else task_id or thread
        interrupts = [{"id": i.get("id"), "value": i.get("value")} for i in result.interrupts]
        self.parked[key] = Parked(key, kind, agent, thread, interrupts, task_id)

    def to_record(self, interrupts: list[dict[str, Any]]) -> dict[str, Any]:
        """The run's progress record, with its parked runs when `interrupts` are pending."""

        return {
            "version": _RECORD_VERSION,
            "prefix": self.prefix,
            "tasks": list(self.tasks),
            **{name: copy(getattr(self, name)) for name in _SAVED},
            "lead": record_result(self.lead) if self.lead is not None else None,
            "parked": [asdict(item) for item in self.parked.values()] if interrupts else [],
            "interrupts": [{"id": i["id"], "value": i["value"]} for i in interrupts],
            "usage": self.ctx.usage.model_dump(mode="json"),
            "limits": (
                self.ctx.limits.model_dump(mode="json") if self.ctx.limits is not None else None
            ),
        }

    @classmethod
    def from_record(
        cls,
        ctx: RunContext[Any],
        thread: str,
        record: Mapping[str, Any],
        decisions: Mapping[str, Any],
    ) -> NetworkRun:
        """Restore a run from `record`; parked runs with a decision are queued for resuming."""

        run = cls(
            ctx=ctx,
            thread=thread,
            prefix=str(record["prefix"]),
            resumed=True,
            **{name: copy(record[name]) for name in _SAVED if name in record},
        )
        # Only a top-level run restores usage; an enclosing run (an eval's) already holds it.
        if ctx.parent_run_id is None and record.get("usage") is not None:
            ctx.usage.merge(Usage.model_validate(record["usage"]))
            # The ceilings filled in at the start are not limits the caller gave.
            if caller_limits(ctx) is None and record.get("limits") is not None:
                ctx.limits = UsageLimits.model_validate(record["limits"])
        run.tasks.update(dict.fromkeys(record["tasks"]))
        if record["lead"] is not None:
            run.lead = restore_result(record["lead"])
        for item in parked_runs(record):
            if item.key not in decisions:
                run.parked[item.key] = item
            elif item.kind == "lead":
                run.lead_resume = (item, decisions[item.key])
            else:
                run.resumes.setdefault(item.agent, []).append((item, decisions[item.key]))
        return run


def parked_runs(record: Mapping[str, Any] | None) -> list[Parked]:
    """The parked runs of a saved record."""

    return [Parked(**data) for data in (record or {}).get("parked", ())]


def route_decisions(network: str, parked: Sequence[Parked], decision: Any) -> dict[str, Any]:
    """Split a resume decision into one decision per parked run (keyed by `Parked.key`).

    Raises:
        ConfigurationError: If several interrupts are pending and `decision` is not a mapping of
            their ids.
    """

    ids = {str(i["id"]): item for item in parked for i in item.interrupts}
    if isinstance(decision, Mapping) and decision and set(map(str, decision)) <= set(ids):
        routed: dict[str, dict[str, Any]] = {}
        for interrupt_id, value in decision.items():
            routed.setdefault(ids[str(interrupt_id)].key, {})[str(interrupt_id)] = value
        return dict(routed)
    if len(ids) == 1:
        [(interrupt_id, item)] = ids.items()
        return {item.key: {interrupt_id: decision}}
    raise ConfigurationError(
        f"Network {network!r} waits for {len(ids)} decisions; pass a mapping of interrupt id "
        f"(see RunResult.interrupts) to decision, for example {{{next(iter(ids))!r}: ...}}"
    )
