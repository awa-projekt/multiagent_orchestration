"""Recording one attempt of a process run into its run log: steps, attempts, results, approvals."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from awa._internal.logging import get_logger
from awa.contracts.events import (
    AgentStartedEvent,
    ApprovalDecidedEvent,
    ModelResponseEvent,
    StepEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.contracts.usage import PriceTable, model_call_cost
from awa.persistence.runlog import (
    Approval,
    AttemptRecord,
    CheckRecord,
    RunLog,
    Scope,
    StepRecord,
    StepResult,
    ToolCallRecord,
)
from awa.runtime.clock import Clock
from awa.workflows.step_output import StepOutput

_logger = get_logger("automation.run_log")

_ATTEMPT_ENDS = {
    "checked": "succeeded",
    "needs_approval": "succeeded",
    "retrying": "failed",
    "failed": "failed",
    "rejected": "succeeded",
}
_FINAL = {"checked", "needs_approval"}


@dataclass
class _Open:
    """The attempt of a step that is running."""

    record: AttemptRecord
    agent_run_id: str | None = None
    usage: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), 0
        )
    )
    cost: float | None = 0.0
    model: str | None = None
    calls: dict[str, ToolCallRecord] = field(default_factory=dict)
    output: StepOutput | None = None


class RunRecorder:
    """Writes one attempt of a process run into its run log.

    `checked` and `needs_approval` end the attempt with a `final` result, `retrying` and `failed`
    with an `intermediate` one. Content goes through `RunLog.capture`; a failing write is logged and
    never fails the run.

    Args:
        scope: The run's scope (tenant, project, initiator, capture switch).
        prices: Model prices for each attempt's cost; without them the cost stays unknown.
    """

    def __init__(
        self,
        log: RunLog,
        scope: Scope,
        run_id: str,
        *,
        clock: Clock,
        prices: PriceTable | None = None,
    ) -> None:
        self.log = log
        self.scope = scope
        self.run_id = run_id
        self._clock = clock
        self._prices = prices
        self._steps: dict[str, StepRecord] = {}
        self._open: dict[str, _Open] = {}
        self._outputs: dict[str, StepOutput] = {}
        self._current: str | None = None
        self.failures = 0

    def _now(self) -> datetime:
        return self._clock.now()

    async def start(self) -> bool:
        """Open the run's log and read what earlier attempts stored; `False` when it failed."""

        try:
            await self.log.open_run(
                self.scope, self.run_id, now=self._now(), initiator=self.scope.principal
            )
            self._steps = {s.step_key: s for s in await self.log.steps(self.scope, self.run_id)}
        except Exception:  # noqa: BLE001 - the run goes on without its log
            self.failures += 1
            _logger.warning("Could not open the run log of %s", self.run_id, exc_info=True)
            return False
        return True

    async def step_output(self, output: StepOutput) -> None:
        """`StepOutputSink`: keep the output of the step's current try for its result."""

        self._outputs[output.step] = output

    async def __call__(self, event: Any) -> None:
        """Event subscriber: record the events of this run."""

        if getattr(event, "root_run_id", None) != self.run_id:
            return
        try:
            await self._record(event)
        except Exception:  # noqa: BLE001 - a store failure never fails the run
            self.failures += 1
            _logger.warning(
                "Could not record %s of run %s in the run log",
                type(event).__name__,
                self.run_id,
                exc_info=True,
            )

    async def _record(self, event: Any) -> None:
        if isinstance(event, StepEvent):
            await self._on_step(event)
        elif isinstance(event, ApprovalDecidedEvent) and event.tool.startswith("step:"):
            await self._on_decision(event)
        elif self._current is None or self._current not in self._open:
            return
        elif isinstance(event, AgentStartedEvent):
            opened = self._open[self._current]
            if opened.agent_run_id is None and event.depth > 0:
                opened.agent_run_id = event.run_id
                await self.log.capture(
                    self.scope, self.run_id, "input_messages", event.input_preview, now=self._now()
                )
        elif isinstance(event, ModelResponseEvent):
            await self._on_model(self._open[self._current], event)
        elif isinstance(event, ToolCallEvent):
            ref = await self.log.capture(
                self.scope, self.run_id, "tool_arguments", event.arguments, now=self._now()
            )
            self._open[self._current].calls[event.call_id] = ToolCallRecord(
                call_id=event.call_id,
                name=event.tool_name,
                arguments_length=len(json.dumps(event.arguments, default=str)),
                arguments_ref=ref,
            )
        elif isinstance(event, ToolResultEvent):
            calls = self._open[self._current].calls
            ref = await self.log.capture(
                self.scope, self.run_id, "tool_result", event.content, now=self._now()
            )
            known = calls.get(event.call_id) or ToolCallRecord(
                call_id=event.call_id, name=event.tool_name
            )
            calls[event.call_id] = known.model_copy(
                update={
                    "status": "error" if event.is_error else "ok",
                    "result_length": len(event.content),
                    "result_ref": ref,
                }
            )

    async def _on_model(self, opened: _Open, event: ModelResponseEvent) -> None:
        for name in opened.usage:
            opened.usage[name] += int(getattr(event, name))
        opened.model = event.model or opened.model
        if opened.cost is not None:
            call = model_call_cost(event, self._prices)
            opened.cost = None if call is None else opened.cost + call
        if event.content:
            await self.log.capture(
                self.scope, self.run_id, "output_messages", event.content, now=self._now()
            )

    async def _save(self, step: StepRecord) -> StepRecord:
        known = self._steps.get(step.step_key)
        stored = await self.log.save_step(
            self.scope, step, expected_revision=None if known is None else known.revision
        )
        self._steps[step.step_key] = stored
        return stored

    async def _on_step(self, event: StepEvent) -> None:
        now = self._now()
        known = self._steps.get(event.step)
        step = known or StepRecord(run_id=self.run_id, step_key=event.step, ordinal=event.index)
        changes: dict[str, Any] = {
            "status": event.status,
            "ordinal": event.index,
            "title": event.title,
            "agent": event.agent,
            "message": event.message,
        }
        failing = next((c.get("id") for c in event.checks if not c.get("passed", True)), None)
        if event.case or failing:
            changes["rule_id"] = event.case or failing
        if event.status in ("checked", "skipped", "failed", "rejected"):
            changes["ended_at"] = now
        elif event.status == "started":
            changes["ended_at"] = None
        step = await self._save(step.model_copy(update=changes))
        if event.status == "started":
            self._current = event.step
            self._outputs.pop(event.step, None)
            attempt = await self.log.start_attempt(self.scope, self.run_id, event.step, now=now)
            self._open[event.step] = _Open(attempt)
            self._steps[event.step] = step.model_copy(
                update={"attempts": attempt.attempt, "started_at": step.started_at or now}
            )
            return
        ending = _ATTEMPT_ENDS.get(event.status)
        opened = self._open.pop(event.step, None) if ending else None
        if ending is None or opened is None:
            return
        if self._current == event.step:
            self._current = None
        await self._end_attempt(event, opened, ending, now)

    async def _end_attempt(
        self, event: StepEvent, opened: _Open, ending: str, now: datetime
    ) -> None:
        output = self._outputs.pop(event.step, None)
        attempt = opened.record.model_copy(
            update={
                "status": ending,
                "ended_at": now,
                "model": opened.model,
                "agent_run_id": opened.agent_run_id or (output.agent_run_id if output else None),
                "retry_reason": (event.message or None) if event.status == "retrying" else None,
                "error_type": _error_type(event),
                "cost_micros": None if opened.cost is None else round(opened.cost * 1_000_000),
                **opened.usage,
            }
        )
        await self.log.finish_attempt(self.scope, attempt)
        answer_ref = None
        if output is not None and output.text:
            answer_ref = await self.log.capture(
                self.scope, self.run_id, "output_messages", output.text, now=now
            )
        result = await self.log.put_result(
            self.scope,
            StepResult(
                run_id=self.run_id,
                step_key=event.step,
                attempt=attempt.attempt,
                kind="final" if event.status in _FINAL else "intermediate",
                output=None if output is None else output.output,
                answer_length=0 if output is None else len(output.text),
                answer_ref=answer_ref,
                tool_calls=tuple(opened.calls.values()),
                checks=tuple(CheckRecord.model_validate(c) for c in event.checks),
                created_at=now,
            ),
        )
        if event.status == "needs_approval":
            await self.log.request_approval(
                self.scope,
                Approval(
                    run_id=self.run_id,
                    step_key=event.step,
                    result_id=result.result_id,
                    requested_at=now,
                    reasons=tuple(r for r in event.message.split("; ") if r),
                ),
            )

    async def _on_decision(self, event: ApprovalDecidedEvent) -> None:
        step = event.tool.removeprefix("step:")
        pending = [
            a
            for a in await self.log.approvals(self.scope, self.run_id)
            if a.step_key == step and a.decision is None
        ]
        if not pending:
            return
        await self.log.decide_approval(
            self.scope,
            pending[-1].approval_id,
            decision="approved" if event.decision in ("approve", "edit") else "rejected",
            reviewer=event.reviewer or "unattributed",
            reason=event.note or None,
            now=self._now(),
        )


def _error_type(event: StepEvent) -> str | None:
    """The error class of a try that raised (`"<ErrorType>: <message>"`), else `None`."""

    if event.status != "failed" or event.checks:
        return None
    head = event.message.partition(":")[0]
    return head if head.isidentifier() else None
