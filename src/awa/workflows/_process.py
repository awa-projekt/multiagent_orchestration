"""`ProcessWorkflow`: steps run one after the other, each checked before the next one starts.

One node per step, plus `<step>.documents` before a step with documents and `<step>.approval`
for a step that can ask a person, so answering an approval never runs the agent again. The try
number lives in the graph state, so a run paused in try 2 resumes in try 2.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, NoReturn

from pydantic import BaseModel

from awa._internal.logging import get_logger
from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import END, START, Command, StateGraph
from awa._internal.util import preview
from awa.contracts.events import ApprovalDecidedEvent, ApprovalRequestedEvent, StepEvent, StepStatus
from awa.contracts.protocols import AgentInput, AgentLike
from awa.contracts.results import RunResult
from awa.errors import StepCheckFailedError, StepRejectedError
from awa.kernel.redaction import SECRETS_REDACTOR, redact_mapping
from awa.runtime.cache import Cache, tenant_of
from awa.runtime.context import RunContext, current_run_context
from awa.runtime.engine import Pause, current_engine
from awa.workflows._base import (
    WorkflowRun,
    _Outcome,
    input_text,
    labeled,
    merge_records,
    record_result,
    restore_result,
    step_text,
)
from awa.workflows._special_cases import CaseAction, CaseOutcome, SpecialCases
from awa.workflows._step_checks import (
    ApprovalCheck,
    ApprovalRequest,
    CheckResult,
    StepCheck,
    answer_check,
    approval_result,
    documents_check,
    json_in_text,
    output_data,
    output_values,
    run_checks,
)
from awa.workflows._step_flow import (
    AfterLastTry,
    RunCondition,
    StepRetry,
    case_id,
    case_record,
    may_retry_error,
)
from awa.workflows.ports import StepDocuments
from awa.workflows.sequential import Carry, Sequential, _SequentialState
from awa.workflows.step_output import StepOutput, current_step_output_sink

_log = get_logger("workflows.process")

APPROVAL_REASON = "step_check_requires_approval"
"""`ApprovalRequestedEvent.reason` of a step approval; its `tool` is `step:<step name>`."""

_NO_REASON = "no reason given"
_DOCUMENTS_TTL = 7 * 24 * 3600
"""Seconds a cached documents check is kept (it is also replaced as soon as a file changes)."""
_MAX_OUTPUT_CHARS = 4000
_PREVIEW_CHARS = 500  # as long as a tool approval's argument preview
_STOP = AfterLastTry()


@dataclass(frozen=True)
class ProcessStep:
    """One step of a `ProcessWorkflow`.

    Attributes:
        name: Step name: letters, digits, `_` and `-`; unique in the process.
        checks: The checks that run after the agent answered; `approval` checks run last.
        retry: How often the step is tried when it fails; `None` for once.
        after_last_try: What happens when the last try failed (default: the run stops).
        when: The step runs only when this condition holds; `None` for always.
        cases: Special cases decided on the step's output after its checks passed.
    """

    name: str
    agent: AgentLike
    title: str = ""
    documents: StepDocuments | None = None
    checks: tuple[StepCheck, ...] = field(default_factory=tuple)
    retry: StepRetry | None = None
    after_last_try: AfterLastTry = _STOP
    when: RunCondition | None = None
    cases: SpecialCases | None = None

    @property
    def attempts(self) -> int:
        """Tries the step may take, the first included."""

        return self.retry.max_attempts if self.retry is not None else 1

    def can_ask(self) -> bool:
        """Whether the step may pause for a person (and so needs an approval node)."""

        return (
            any(isinstance(check, ApprovalCheck) for check in self.checks)
            or self.cases is not None
            or self.after_last_try.action == "ask"
        )

    def go_targets(self) -> list[str]:
        """Steps a `go` of this step may continue at (after the last try, or a special case)."""

        targets = [self.after_last_try.to] if self.after_last_try.to else []
        if self.cases is not None:
            actions = [*self.cases.cases, self.cases.else_, self.cases.when_unclear]
            targets += [action.to for action in actions if action.then == "go" and action.to]
        return list(dict.fromkeys(targets))


class _ProcessState(_SequentialState, total=False):
    _attempts: Annotated[dict[str, int], merge_records]
    _feedback: Annotated[dict[str, str], merge_records]
    _tries: Annotated[dict[str, list[dict[str, Any]]], merge_records]
    _status: Annotated[dict[str, dict[str, Any]], merge_records]
    # Per step with documents: the documents check (`problems`) and the block for its input.
    _documents: Annotated[dict[str, dict[str, Any]], merge_records]


@dataclass(frozen=True)
class _Failure:
    """A failed try: why, what the checks found, the result (none after an error) and the error."""

    kind: str  # "error" or "check"
    message: str
    results: tuple[CheckResult, ...]
    result: RunResult[Any] | None = None
    error: Exception | None = None


class ProcessWorkflow(Sequential):
    """Runs steps in order; each step is checked before the next one starts.

    Per step: `when` decides whether it runs (a missing or unclear value stops the run); the
    documents are checked; the agent runs on the carried text, the documents block and what failed
    in the previous try; the answer must not be empty; `schema` and `rule` checks run; the special
    cases decide; and an applying `approval` check or special case pauses the run until a person
    decides (`{"action": "approve"}` or `{"action": "reject", "note": "..."}`; anything else
    rejects). A special case that approves skips only its own question.

    A failed try is retried when the step's `retry` covers it, up to `max_attempts`. After the last
    failed try, `after_last_try` stops the run (`StepCheckFailedError` or the agent's error), asks a
    person, or continues at a later step. A rejection ends the run with `StepRejectedError`. Limits,
    cancellations, timeouts, guardrail blocks and permission or configuration errors are never
    retried. A step approval needs a checkpointer (this workflow's or the enclosing run's).

    `metadata["steps"]` lists every step with its status (`checked`, `skipped` or `failed`), checks,
    agent run id, duration, `attempts` and deciding `case`; `StepEvent`s report each status change.

    Example:
        ```python
        process = ProcessWorkflow(
            "invoice_check",
            [
                ProcessStep(
                    "extract", extractor,
                    checks=(RuleCheck("extract.rule.1", "total <= 0", "No total."),),
                    retry=StepRetry(max_attempts=3, initial=2.0),
                ),
                ProcessStep("approve", booker, checks=(ApprovalCheck("approve.approval.1"),)),
            ],
            checkpointer=StateInMemorySaver(),
        )
        ```

    Args:
        steps: The steps, in order; at least one; names unique.
        description: What the process does; generated from the steps if empty.
        carry: What later steps receive, as for `Sequential`; only steps that passed are carried.
        sleep: Waits the given seconds between tries (default `asyncio.sleep`).
        rand: Source of uniform values in `[0, 1)` for the retry jitter.
        cache: With its `steps` switch on, a step's documents check is reused while the documents'
            fingerprint (`StepDocuments.fingerprint`) is unchanged.
    """

    _kind = "process"

    def __init__(
        self,
        name: str,
        steps: Sequence[ProcessStep],
        *,
        description: str = "",
        carry: Carry = "previous",
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] | None = None,
        cache: Cache | None = None,
    ) -> None:
        super().__init__(
            name,
            [step.agent for step in steps],
            description=description,
            carry=carry,
            checkpointer=checkpointer,
        )
        _check_steps(steps)
        self._process_steps = tuple(steps)
        self._keys = [step.name for step in steps]
        self._sleep = sleep
        self._rand = rand
        self._cache = cache

    def _default_description(self) -> str:
        return (
            "Runs "
            + ", then ".join(step.name for step in self._process_steps)
            + ", checking each step before the next."
        )

    def _build_graph(self) -> StateGraph[Any, Any, Any, Any]:
        builder: StateGraph[Any, Any, Any, Any] = StateGraph(_ProcessState)
        later = [*self._keys[1:], END]
        for index, key in enumerate(self._keys):
            step = self._process_steps[index]
            forward = [*later[index:], *(t for t in step.go_targets() if t in self._keys)]
            approval = (f"{key}.approval",) if step.can_ask() else ()
            entries = [self._entry(target) for target in forward]
            if step.documents is not None:
                # The documents check runs before every try (a retry goes through it again): with
                # the step cache it is reused while the documents are unchanged.
                builder.add_node(
                    f"{key}.documents",
                    self._documents_node(index),
                    cache_policy=self._documents_policy(index),
                )
                builder.add_edge(f"{key}.documents", key)
            builder.add_node(
                key,
                self._step_node(index),
                destinations=tuple(dict.fromkeys([self._entry(key), *approval, *entries])),
            )
            if approval:
                builder.add_node(
                    approval[0],
                    self._approval_node(index),
                    destinations=tuple(dict.fromkeys(entries)),
                )
        builder.add_edge(START, self._entry(self._keys[0]))
        return builder

    def _entry(self, target: str) -> str:
        """The node a move to step `target` enters: its documents check, if it has documents."""

        if target in self._keys and self._process_steps[self._keys.index(target)].documents:
            return f"{target}.documents"
        return target

    def _recursion_limit(self) -> int:
        return (
            sum(
                step.attempts * (2 if step.documents is not None else 1) + 1
                for step in self._process_steps
            )
            + 2
        )

    def _node_cache(self) -> Any:
        if self._cache is None or not self._cache.uses("steps"):
            return None
        from awa._internal.upstream.caches import NodeCache

        cache = self._cache

        def read(key: str) -> bytes | None:
            return cache.get("steps", _tenant(), key)

        def write(key: str, value: bytes, ttl: float | None) -> None:
            cache.put("steps", _tenant(), key, value, ttl=ttl)

        return NodeCache(read, write)

    def _documents_policy(self, index: int) -> Any:
        """The `CachePolicy` of a step's documents node, or `None` when it cannot be cached."""

        documents = self._process_steps[index].documents
        fingerprint = getattr(documents, "fingerprint", None)
        if self._cache is None or not self._cache.uses("steps") or not callable(fingerprint):
            return None
        from awa._internal.upstream.caches import CachePolicy
        from awa.runtime.cache import cache_key

        step, process = self._keys[index], self._name

        def key(state: Any) -> str:
            # Tenant first; never the default key, which pickles the node input.
            return cache_key(_tenant(), process, step, fingerprint())

        return CachePolicy(key_func=key, ttl=_DOCUMENTS_TTL)

    def _documents_node(self, index: int) -> Any:
        """Check a step's documents and render their block (no model): its own node, so a
        LangGraph `CachePolicy` can reuse it while the files are unchanged."""

        step = self._process_steps[index]
        key = self._keys[index]

        async def check_documents(state: _ProcessState) -> dict[str, Any]:
            assert step.documents is not None
            problems = await asyncio.to_thread(step.documents.problems)
            block = "" if problems else await asyncio.to_thread(step.documents.block)
            return {"_documents": {key: {"problems": problems, "block": block}}}

        return check_documents

    def _step_node(self, index: int) -> Any:
        step = self._process_steps[index]
        key = self._keys[index]

        async def run_step(state: _ProcessState) -> Command[Any]:
            ctx = self._current_run().ctx
            attempt = state.get("_attempts", {}).get(key, 1)
            if attempt == 1 and step.when is not None:
                skipped = await self._decide_when(ctx, state, index)
                if skipped is not None:
                    return skipped
            await self._emit(ctx, index, "started", attempt=self._attempt(step, attempt))
            results: list[CheckResult] = []
            step_input = self._step_input(state, index)
            if step.documents is not None:
                checked = state.get("_documents", {}).get(key, {})
                results.append(documents_check(step.name, list(checked.get("problems", []))))
                if not results[-1].passed:
                    await self._stop(ctx, state, index, results)
                step_input = _with_block(step_input, str(checked.get("block", "")))
            feedback = state.get("_feedback", {}).get(key, "") if attempt > 1 else ""
            step_input = _with_block(step_input, feedback)
            try:
                result = await self.run_member(step.agent, step_input, index)
            except current_engine().control_flow:
                raise
            except Exception as exc:
                failure = _Failure("error", f"{type(exc).__name__}: {exc}", (), error=exc)
                return await self._failed_try(ctx, state, index, attempt, failure)
            await self._emit(
                ctx, index, "checking", checks=results, attempt=self._attempt(step, attempt)
            )
            results.append(answer_check(step.name, step.agent.name, result))
            verdict = run_checks(step.checks, result) if results[-1].passed else None
            if verdict is not None:
                results.extend(verdict.results)
            if verdict is None or not verdict.passed:
                reasons = " ".join(r.message for r in results if not r.passed and r.message)
                failure = _Failure("check", reasons, tuple(results), result=result)
                return await self._failed_try(ctx, state, index, attempt, failure)
            return await self._passed(ctx, state, index, attempt, result, results, verdict.approval)

        return run_step

    async def _passed(
        self,
        ctx: RunContext[Any],
        state: Mapping[str, Any],
        index: int,
        attempt: int,
        result: RunResult[Any],
        results: list[CheckResult],
        approval: ApprovalRequest | None,
    ) -> Command[Any]:
        """The checks passed: decide the special cases, then ask a person or move on."""

        step = self._process_steps[index]
        key = self._keys[index]
        await _report_output(step, index, result)
        outcome = step.cases.decide(output_values(result)) if step.cases is not None else None
        case = case_id(step.name, outcome) if outcome is not None else None
        extra: dict[str, Any] = {"attempts": attempt} if step.retry is not None else {}
        if outcome is not None:
            extra["case"] = case_record(step.name, outcome)
        if outcome is not None and outcome.then == "reject":
            await self._stop(ctx, state, index, results, rejected=outcome.describe(), case=case)
        target = self._target(
            index, outcome.action.to if outcome and outcome.then == "go" else None
        )
        request = _merged(approval, outcome, case)
        shown = self._attempt(step, attempt)
        if request is not None:
            info = _info(step, "needs_approval", results, next=target, **extra)
            return await self._ask(ctx, index, result, results, request, info, shown, case)
        info = _info(step, "checked", results, **extra)
        await self._emit(ctx, index, "checked", checks=results, attempt=shown, case=case)
        update: dict[str, Any] = {"_steps": {key: _record(result, info)}}
        return await self._move(ctx, index, target, update, why=outcome)

    async def _failed_try(
        self,
        ctx: RunContext[Any],
        state: Mapping[str, Any],
        index: int,
        attempt: int,
        failure: _Failure,
    ) -> Command[Any]:
        """A try failed: try again, or do what `after_last_try` says."""

        step = self._process_steps[index]
        covered = step.retry.on if step.retry is not None else frozenset({"error", "check"})
        # Limits, cancellation and configuration errors end the run; any other failure gets
        # `after_last_try`, also one the step does not retry.
        recoverable = failure.error is None or may_retry_error(failure.error)
        eligible = recoverable and failure.kind in covered
        tries = [
            *state.get("_tries", {}).get(self._keys[index], []),
            {"attempt": attempt, "failed": failure.kind, "message": failure.message},
        ]
        if failure.result is not None:
            await _report_output(step, index, failure.result)
        if eligible and step.retry is not None and attempt < step.retry.max_attempts:
            return await self._retry(ctx, index, attempt, failure, tries)
        action = step.after_last_try.action if recoverable else "stop"
        shown = self._attempt(step, attempt)
        if action == "stop":
            if failure.error is not None:
                await self._emit(ctx, index, "failed", message=failure.message, attempt=shown)
                raise failure.error
            await self._stop(ctx, state, index, failure.results, attempt=attempt)
        tried = f"All {attempt} tries failed" if attempt > 1 else "The step failed"
        extra: dict[str, Any] = {"tries": tries}
        if step.retry is not None:
            extra["attempts"] = attempt
        if action == "ask":
            reason = (
                f"{tried}: {failure.message} Approve to continue with the last result, "
                "or reject to end the run."
            )
            request = ApprovalRequest((reason,), (), (f"{step.name}.after_last_try",))
            info = _info(step, "needs_approval", failure.results, next=self._target(index, None))
            info |= {**extra, "approval": {"after_failure": True}}
            result = failure.result or _empty_result(ctx, step)
            return await self._ask(ctx, index, result, failure.results, request, info, shown)
        target = step.after_last_try.to
        assert target is not None
        message = f"{tried}: {failure.message} Continuing at step {target}."
        info = _info(step, "failed", failure.results, message=message, continued_at=target, **extra)
        await self._emit(
            ctx, index, "failed", checks=failure.results, message=message, attempt=shown
        )
        update = {"_status": {self._keys[index]: info}}
        return await self._move(ctx, index, self._target(index, target), update, why=None)

    async def _retry(
        self,
        ctx: RunContext[Any],
        index: int,
        attempt: int,
        failure: _Failure,
        tries: list[dict[str, Any]],
    ) -> Command[Any]:
        """Run the step again after the retry delay, telling the agent why a check failed."""

        step = self._process_steps[index]
        key = self._keys[index]
        assert step.retry is not None
        await self._emit(
            ctx,
            index,
            "retrying",
            checks=failure.results,
            message=f"Try {attempt} of {step.retry.max_attempts} failed: {failure.message}",
            attempt=attempt,
        )
        delay = step.retry.delay(attempt - 1, self._rand)
        if delay > 0:
            await self._sleep(delay)
        feedback = (
            "Your previous answer did not pass the checks of this step: "
            f"{failure.message} Answer again and fix this."
            if failure.kind == "check"
            else ""
        )
        return Command(
            goto=self._entry(key),
            update={
                "_attempts": {key: attempt + 1},
                "_feedback": {key: feedback},
                "_tries": {key: tries},
            },
        )

    async def _ask(
        self,
        ctx: RunContext[Any],
        index: int,
        result: RunResult[Any],
        results: Sequence[CheckResult],
        request: ApprovalRequest,
        info: dict[str, Any],
        attempt: int | None,
        case: str | None = None,
    ) -> Command[Any]:
        """Record the step (`info`) as waiting for `request` and go to its approval node."""

        key = self._keys[index]
        info["approval"] = {
            "reasons": list(request.reasons),
            "by": list(request.by),
            "check_ids": list(request.check_ids),
            **info.get("approval", {}),
        }
        message = "; ".join(request.reasons)
        await self._emit(
            ctx,
            index,
            "needs_approval",
            checks=results,
            message=message,
            attempt=attempt,
            case=case,
        )
        return Command(goto=f"{key}.approval", update={"_steps": {key: _record(result, info)}})

    async def _decide_when(
        self, ctx: RunContext[Any], state: Mapping[str, Any], index: int
    ) -> Command[Any] | None:
        """Skip the step when its `when` is false; `None` to run it. An unclear condition stops the run."""

        step = self._process_steps[index]
        assert step.when is not None
        holds, why = step.when.holds(self._values(state, index))
        if holds is None:
            check = CheckResult(
                f"{step.name}.when",
                "rule",
                False,
                f"Whether this step runs cannot be decided ({step.when.expression}): {why}. "
                "Make sure the earlier steps return the values it reads.",
            )
            await self._stop(ctx, state, index, [check])
        if holds:
            return None
        message = f"Skipped: {step.when.expression} is false."
        info = _info(step, "skipped", [], message=message)
        await self._emit(ctx, index, "skipped", message=message)
        return Command(
            goto=self._entry(self._target(index, None)),
            update={"_status": {self._keys[index]: info}},
        )

    async def _move(
        self,
        ctx: RunContext[Any],
        index: int,
        target: str,
        update: dict[str, Any],
        *,
        why: CaseOutcome | None,
    ) -> Command[Any]:
        """Go from step `index` to `target`; steps jumped over are reported `skipped`."""

        end = self._keys.index(target) if target in self._keys else len(self._keys)
        step = self._process_steps[index]
        status = dict(update.get("_status", {}))
        for position in range(index + 1, end):
            other = self._process_steps[position]
            because = (
                f"special case {case_id(step.name, why)} of step {step.name}"
                if why is not None
                else f"step {step.name} continued at a later step"
            )
            message = f"Skipped: {because}."
            status[self._keys[position]] = _info(other, "skipped", [], message=message)
            await self._emit(ctx, position, "skipped", message=message)
        if status:
            update = {**update, "_status": status}
        return Command(goto=self._entry(target), update=update)

    def _target(self, index: int, go_to: str | None) -> str:
        """The node after step `index`: `go_to`, else the next step, else the end."""

        if go_to is not None:
            return go_to
        return self._keys[index + 1] if index + 1 < len(self._keys) else END

    def _attempt(self, step: ProcessStep, attempt: int) -> int | None:
        return attempt if step.retry is not None else None

    def _approval_node(self, index: int) -> Any:
        step = self._process_steps[index]
        key = self._keys[index]

        async def approve(state: _ProcessState) -> Command[Any]:
            ctx = self._current_run().ctx
            record = state["_steps"][key]
            info = dict(record["metadata"]["step"])
            approval = info.get("approval") or {}
            target = info.get("next") or self._target(index, None)
            case = (info.get("case") or {}).get("id")
            payload = _payload(step, approval, restore_result(record))
            engine = current_engine()
            try:
                decision = engine.pause(payload)
            except engine.control_flow as exc:
                await ctx.emit(_requested_event(ctx, step, payload, engine.pauses(exc)))
                raise
            approved, note, reviewer = _decide(decision)
            await ctx.emit(
                ApprovalDecidedEvent(
                    tool=f"step:{step.name}",
                    decision="approve" if approved else "reject",
                    reviewer=reviewer,
                    note=note,
                    **{**ctx.event_fields(), "agent": step.agent.name},
                )
            )
            outcome = approval_result(_request_of(approval), approved=approved, note=note)
            checks = [*(CheckResult(**c) for c in info["checks"]), outcome]
            if not approved:
                await self._stop(ctx, state, index, checks, rejected=outcome.message, case=case)
            decided = {**approval, "decided": "approve", "reviewer": reviewer, "note": note}
            info = {
                **info,
                "status": "checked",
                "checks": [c.to_dict() for c in checks],
                "approval": decided,
            }
            info.pop("next", None)
            attempt = info.get("attempts") if step.retry is not None else None
            await self._emit(ctx, index, "checked", checks=checks, attempt=attempt, case=case)
            update = {"_steps": {key: {**record, "metadata": {**record["metadata"], "step": info}}}}
            go = (info.get("case") or {}).get("to") if target != self._target(index, None) else None
            why = _outcome_of(info) if go else None
            return await self._move(ctx, index, target, update, why=why)

        return approve

    def _passed_keys(self, state: Mapping[str, Any], index: int) -> list[str]:
        records = state.get("_steps", {})
        return [
            key
            for key in self._keys[:index]
            if key in records and records[key]["metadata"]["step"]["status"] == "checked"
        ]

    def _step_input(self, state: _SequentialState, index: int) -> AgentInput:
        """The step's input: the request, then the passed steps' text as `carry` says. A step
        that was skipped or failed is never carried."""

        original = state.get("_input", "")
        records = state.get("_steps", {})
        passed = self._passed_keys(state, index)
        if not passed:
            return original
        if self._carry == "previous":
            return step_text(restore_result(records[passed[-1]]))
        transcript = [
            labeled(
                self._process_steps[self._keys.index(key)].agent.name,
                step_text(restore_result(records[key])),
            )
            for key in passed
        ]
        return "\n\n".join([input_text(original), *transcript])

    def _values(self, state: Mapping[str, Any], index: int) -> dict[str, Any]:
        """What a `when` reads: the request's JSON fields, then every passed step's output
        fields (a later step wins), and `text` (the last passed step's answer)."""

        original = state.get("_input", "")
        text = input_text(original) if original else ""
        data = json_in_text(text)
        values: dict[str, Any] = {"text": text}
        if isinstance(data, dict):
            values.update({str(name): value for name, value in data.items()})
        records = state.get("_steps", {})
        for key in self._passed_keys(state, index):
            values.update(output_values(restore_result(records[key])))
        return values

    async def _stop(
        self,
        ctx: RunContext[Any],
        state: Mapping[str, Any],
        index: int,
        results: Sequence[CheckResult],
        *,
        rejected: str | None = None,
        attempt: int | None = None,
        case: str | None = None,
    ) -> NoReturn:
        """End the run at step `index`: as failed, or as `rejected` for that reason (a special
        case or a person turned the item down)."""

        step = self._process_steps[index]
        records = state.get("_steps", {})
        others = state.get("_status", {})
        status = "failed" if rejected is None else "rejected"

        def status_of(position: int) -> str:
            key = self._keys[position]
            if position == index:
                return status
            if key in records:
                return str(records[key]["metadata"]["step"]["status"])
            return str(others[key]["status"]) if key in others else "waiting"

        steps = [
            {"name": other.name, "agent": other.agent.name, "status": status_of(position)}
            for position, other in enumerate(self._process_steps)
        ]
        checks = [r.to_dict() for r in results]
        if rejected is not None:
            await self._emit(ctx, index, "rejected", checks=results, message=rejected, case=case)
            raise StepRejectedError(
                f"Step {_label(step)} rejected the item: {rejected} Later steps were not run.",
                step=step.name,
                checks=checks,
                steps=steps,
                details={"rejected": True, **({"case": case} if case else {})},
            )
        reasons = " ".join(r.message for r in results if not r.passed and r.message)
        tried = f" (after {attempt} tries)" if attempt and attempt > 1 else ""
        await self._emit(
            ctx,
            index,
            "failed",
            checks=results,
            message=reasons,
            attempt=self._attempt(step, attempt or 1),
        )
        raise StepCheckFailedError(
            f"Step {_label(step)} failed{tried}: {reasons or 'a check did not pass'} "
            "Later steps were not run. Fix the cause, then run the step again.",
            step=step.name,
            checks=checks,
            steps=steps,
        )

    async def _emit(
        self,
        ctx: RunContext[Any],
        index: int,
        status: StepStatus,
        *,
        checks: Sequence[CheckResult] = (),
        message: str = "",
        attempt: int | None = None,
        case: str | None = None,
    ) -> None:
        step = self._process_steps[index]
        await ctx.emit(
            StepEvent(
                step=step.name,
                index=index,
                total=len(self._process_steps),
                status=status,
                title=step.title,
                checks=tuple(c.to_dict() for c in checks),
                message=message,
                attempt=attempt,
                case=case,
                **{**ctx.event_fields(), "agent": step.agent.name},
            )
        )

    def _outcome(self, state: Mapping[str, Any], run: WorkflowRun) -> _Outcome:
        records = state.get("_steps", {})
        others = state.get("_status", {})
        steps = []
        for key, step in zip(self._keys, self._process_steps, strict=True):
            record = records.get(key)
            info = record["metadata"]["step"] if record is not None else others.get(key, {})
            entry: dict[str, Any] = {
                "name": step.name,
                "title": step.title,
                "agent": step.agent.name,
                "status": info.get("status", "waiting"),
                "checks": info.get("checks", []),
                "run_id": record["run_id"] if record is not None else None,
                "duration_ms": record["duration_ms"] if record is not None else 0,
            }
            for name in ("attempts", "case", "message", "continued_at"):
                if name in info:
                    entry[name] = info[name]
            steps.append(entry)
        passed = self._passed_keys(state, len(self._keys))
        metadata: dict[str, Any] = {"steps": steps}
        if not passed:
            return _Outcome(output=None, text="", metadata=metadata)
        return _Outcome.of(restore_result(records[passed[-1]]), **metadata)


def _check_steps(steps: Sequence[ProcessStep]) -> None:
    """Refuse repeated or malformed step names and a `go` that does not move forward."""

    names = [step.name for step in steps]
    if len(set(names)) != len(names):
        repeated = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"Step names must be unique; {', '.join(repeated)} appear more than once")
    for step_name in names:
        if not step_name or "." in step_name:
            raise ValueError(f"Invalid step name {step_name!r}: use letters, digits, _ and -")
    for index, step in enumerate(steps):
        later = names[index + 1 :]
        for target in step.go_targets():
            if target not in later:
                raise ValueError(
                    f"Step {step.name!r} continues at {target!r}, which is not a later step "
                    f"of the process; a 'go' moves forward only (later steps: "
                    f"{', '.join(later) or 'none'})"
                )


async def _report_output(step: ProcessStep, index: int, result: RunResult[Any]) -> None:
    """Hand a try's output to the step output sink of the context, when one is set."""

    sink = current_step_output_sink()
    if sink is None:
        return
    try:
        await sink(StepOutput(step.name, index, output_data(result), result.text, result.run_id))
    except Exception:  # noqa: BLE001 - recording an output never fails the step
        _log.warning("The step output sink failed for step %s", step.name, exc_info=True)


def _label(step: ProcessStep) -> str:
    return f"'{step.title}' ({step.name})" if step.title else f"'{step.name}'"


def _merged(
    approval: ApprovalRequest | None, outcome: CaseOutcome | None, case: str | None
) -> ApprovalRequest | None:
    """One question for the approval checks that apply and a special case that asks."""

    if outcome is None or outcome.then != "ask" or case is None:
        return approval
    reason = outcome.action.message or outcome.describe()
    asked = ApprovalRequest(
        reasons=(f"Special case: {reason}",),
        by=(outcome.action.by,) if outcome.action.by else (),
        check_ids=(case,),
    )
    if approval is None:
        return asked
    return ApprovalRequest(
        reasons=(*approval.reasons, *asked.reasons),
        by=tuple(dict.fromkeys([*approval.by, *asked.by])),
        check_ids=(*approval.check_ids, *asked.check_ids),
    )


def _outcome_of(info: Mapping[str, Any]) -> CaseOutcome | None:
    """The special case recorded in `info`, rebuilt for the words of a skip."""

    case = info.get("case")
    if not case:
        return None
    return CaseOutcome(CaseAction(then="go", to=case["to"]), case["source"], case["row"])


def _empty_result(ctx: RunContext[Any], step: ProcessStep) -> RunResult[Any]:
    """The result of a step whose agent raised: no output, no text."""

    return RunResult(run_id=ctx.run_id, agent=step.agent.name, output=None, text="")


def _with_block(step_input: AgentInput, block: str) -> AgentInput:
    if not block:
        return step_input
    if isinstance(step_input, str):
        return f"{step_input}\n\n{block}" if step_input else block
    return [*step_input, {"role": "user", "content": block}]


def _info(
    step: ProcessStep, status: str, results: Sequence[CheckResult], **extra: Any
) -> dict[str, Any]:
    return {
        "name": step.name,
        "agent": step.agent.name,
        "status": status,
        "checks": [r.to_dict() for r in results],
        **extra,
    }


def _record(result: RunResult[Any], info: dict[str, Any]) -> dict[str, Any]:
    record = record_result(result)
    record["metadata"] = {**record["metadata"], "step": info}
    return record


def _decide(decision: Any) -> tuple[bool, str, str | None]:
    """`(approved, note, reviewer)`: only `{"action": "approve"}` approves; anything else, a
    malformed answer included, is a rejection."""

    if not isinstance(decision, Mapping):
        return False, f"{_NO_REASON} (malformed resume payload)", None
    note = str(decision.get("note", _NO_REASON))
    reviewer: Any = decision.get("reviewer", decision.get("actor"))
    if isinstance(reviewer, Mapping):
        reviewer = reviewer.get("id")
    shown = str(reviewer) if reviewer else None
    return decision.get("action") == "approve", note, shown


def _payload(
    step: ProcessStep, approval: Mapping[str, Any], result: RunResult[Any]
) -> dict[str, Any]:
    """What the person sees: `{kind: step_approval, step, title, reason, output, agent}`, plus
    `by`, and `tool` and `args` so the cards of tool approvals can show it too."""

    data = output_data(result)
    output: Any = (
        _plain(data) if data is not None else {"text": preview(result.text, _MAX_OUTPUT_CHARS)}
    )
    payload: dict[str, Any] = {
        "kind": "step_approval",
        "tool": f"step:{step.name}",
        "step": step.name,
        "title": step.title or step.name,
        "agent": step.agent.name,
        "reason": "; ".join(approval.get("reasons") or ()),
        "output": output,
        "args": output if isinstance(output, dict) else {"output": output},
    }
    if approval.get("by"):
        payload["by"] = list(approval["by"])
    if approval.get("after_failure"):
        payload["after_failure"] = True
    return payload


def _plain(value: Any) -> Any:
    """`value` as JSON-compatible data with long texts cut."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        loaded = json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        loaded = str(value)
    return _shorten(loaded)


def _shorten(value: Any) -> Any:
    if isinstance(value, str):
        return preview(value, _MAX_OUTPUT_CHARS)
    if isinstance(value, list):
        return [_shorten(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _shorten(item) for key, item in value.items()}
    return value


def _output_preview(output: Any) -> str:
    """The step output for the approval event, redacted as a tool approval's arguments are:
    credentials in text and every value under a sensitive key (`api_key`, `password`, ...)."""

    redacted = redact_mapping(output, SECRETS_REDACTOR)
    try:
        text = json.dumps(redacted, default=str, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(redacted)
    return preview(text, _PREVIEW_CHARS)


def _requested_event(
    ctx: RunContext[Any], step: ProcessStep, payload: Mapping[str, Any], raised: Sequence[Pause]
) -> ApprovalRequestedEvent:
    interrupt_id = raised[0].id if raised else ""
    return ApprovalRequestedEvent(
        tool=f"step:{step.name}",
        interrupt_id=interrupt_id,
        args_preview=_output_preview(payload["output"]),
        reason=APPROVAL_REASON,
        **{**ctx.event_fields(), "agent": step.agent.name},
    )


def _request_of(approval: Mapping[str, Any]) -> ApprovalRequest:
    return ApprovalRequest(
        reasons=tuple(approval.get("reasons") or ()),
        by=tuple(approval.get("by") or ()),
        check_ids=tuple(approval.get("check_ids") or ("approval",)),
    )


def _tenant() -> str:
    return tenant_of(current_run_context())
