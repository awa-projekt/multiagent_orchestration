"""Delegation and peer consultation: running another team member on a brief, as a nested run.

The target sees only the brief; its result or failure returns as a labeled `<result>` block, and
only usage-limit breaches, cancellation and interrupts propagate.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

import pydantic_core
from pydantic import BaseModel, Field, ValidationError, create_model

from awa._internal.upstream.messages import BaseMessage
from awa._internal.upstream.tools import BaseTool, StructuredTool
from awa.agents import Agent
from awa.agents.boundary import AgentCall, CallOutcome, decide_call, decide_result
from awa.agents.middleware import BUDGET_STAGES_FROM, ROOT_RESERVE, remaining_requests
from awa.contracts.communication import AgentMessage
from awa.contracts.events import AgentMessageEvent, DelegationEvent, DelegationResultEvent
from awa.contracts.protocols import AgentLike
from awa.contracts.results import RunResult
from awa.contracts.usage import UsageLimits
from awa.errors import AgentRunError, UsageLimitExceededError
from awa.messaging.blackboard import BaseBlackboard, BlackboardEntry
from awa.runtime.context import RunContext, agent_call
from awa.runtime.engine import current_engine
from awa.runtime.ids import AgentKey, brief_hash
from awa.runtime.loops import cycle_refusal
from awa.runtime.termination import release_nested_stop
from awa.team._run import (
    PREVIEW_CHARS,
    Admission,
    BackgroundTask,
    TeamRun,
    error_text,
    frame_result,
    lookup_run,
    neutralize,
    pending_calls,
)
from awa.tools.policy import asks_for_approval

Mode = Literal["delegate", "consult"]
Status = Literal["ok", "error"]


_scope: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "awa_delegation_scope", default=None
)
"""The delegation (see `Delegator._run_target`) the current code runs inside."""

_grant: contextvars.ContextVar[tuple[TeamRun, Admission] | None] = contextvars.ContextVar(
    "awa_delegation_grant", default=None
)
"""The admission decided for the tool call the current code executes."""

DELEGATION_TOOLS: Mapping[str, str] = {"delegate_task": "agent", "consult_peer": "peer"}
"""Tools that take a delegation slot, and the argument naming their target."""

SHARED_ENTRY_CHARS = 2_000
"""How much of each shared-workspace value a member wrote is inlined in its delegation result."""


def render_brief(
    sender: str, task: str, context: str = "", expected_output: str = "", *, mode: Mode
) -> str:
    """Render the message a delegated member (or consulted peer) receives."""

    heading = "Question" if mode == "consult" else "Task"
    sections = [f"{heading} from {sender}:\n{task.strip()}"]
    if context.strip():
        sections.append(f"Context:\n{context.strip()}")
    if expected_output.strip():
        sections.append(f"Expected output:\n{expected_output.strip()}")
    return "\n\n".join(sections)


def output_text(result: RunResult[Any]) -> str:
    """A run's output as text: the text itself, or JSON of structured output."""

    output = result.output
    if isinstance(output, str):
        return output
    if output is None:
        return result.text
    return pydantic_core.to_json(output).decode()


class Delegator:
    """Runs delegations and consultations for one team and builds the tools for them.

    Args:
        key: `RunContext.internal` key of the team's `TeamRun`.
        runtime: The runtime agent of every member, by name (resolved at call time).
        blackboard: Where oversized results are stored, if the team has one.
    """

    def __init__(
        self,
        *,
        team: str,
        key: str,
        runtime: Mapping[str, AgentLike],
        blackboard: BaseBlackboard | None,
    ) -> None:
        self._team = team
        self._key = key
        self._runtime = runtime
        self._blackboard = blackboard
        self._schemas: dict[tuple[str, str], type[BaseModel]] = {}

    def delegation_tools(self, source: str, targets: Sequence[str]) -> list[BaseTool]:
        """`delegate_task` restricted to `targets`, plus the background-task tools."""

        options = tuple(targets)
        choice: Any = Literal[options]  # built at run time from the names
        args = create_model(
            "DelegateTaskArgs",
            agent=(
                choice,
                Field(description=f"Member to delegate to. One of: {', '.join(options)}."),
            ),
            task=(
                str,
                Field(
                    description=(
                        "The objective, stated so the member can act on it without seeing "
                        "this conversation."
                    )
                ),
            ),
            context=(
                str,
                Field(default="", description="Facts, constraints and material the member needs."),
            ),
            expected_output=(
                str,
                Field(
                    default="", description="What the member should return, and in which format."
                ),
            ),
            background=(
                bool,
                Field(
                    default=False,
                    description=(
                        "Run in the background and continue working; the result is handed "
                        "back to you when it is ready."
                    ),
                ),
            ),
        )

        self._schemas[(source, "delegate_task")] = args

        async def delegate_task(
            agent: str,
            task: str,
            context: str = "",
            expected_output: str = "",
            background: bool = False,
        ) -> str:
            ctx, run = lookup_run(self._key, self._team, "delegate_task")
            brief = render_brief(source, task, context, expected_output, mode="delegate")
            if background:
                return await self._start_background(ctx, run, agent, task, brief)
            return await self._delegate(ctx, run, agent, task, brief, mode="delegate")

        return [
            StructuredTool.from_function(
                coroutine=delegate_task,
                name="delegate_task",
                description=(
                    "Delegate a task to a team member and get its result. The member sees "
                    "only what you pass here. Several calls in one turn run in parallel."
                ),
                args_schema=args,
            ),
            *self._background_tools(),
        ]

    def consult_tool(self, source: str, peers: Sequence[str]) -> BaseTool:
        """`consult_peer` restricted to `peers`."""

        options = tuple(peers)
        choice: Any = Literal[options]  # built at run time from the names
        args = create_model(
            "ConsultPeerArgs",
            peer=(choice, Field(description=f"Peer to ask. One of: {', '.join(options)}.")),
            question=(str, Field(description="One specific question.")),
            context=(str, Field(default="", description="The context the peer needs to answer.")),
        )

        self._schemas[(source, "consult_peer")] = args

        async def consult_peer(peer: str, question: str, context: str = "") -> str:
            ctx, run = lookup_run(self._key, self._team, "consult_peer")
            brief = render_brief(source, question, context, mode="consult")
            return await self._delegate(ctx, run, peer, question, brief, mode="consult")

        return StructuredTool.from_function(
            coroutine=consult_peer,
            name="consult_peer",
            description=(
                "Ask a peer a question and get its answer. The peer sees only what you pass here."
            ),
            args_schema=args,
        )

    def _background_tools(self) -> list[BaseTool]:
        async def check_delegations() -> str:
            ctx, run = lookup_run(self._key, self._team, "check_delegations")
            tasks = run.owned(ctx.run_id)
            if not tasks:
                return "You have no background tasks."
            return json.dumps({"tasks": [t.summary() for t in tasks]}, ensure_ascii=False)

        async def wait_for_delegations(
            task_ids: list[str] | None = None, timeout: float | None = None
        ) -> str:
            ctx, run = lookup_run(self._key, self._team, "wait_for_delegations")
            owned = {task.id: task for task in run.owned(ctx.run_id)}
            wanted = list(owned) if task_ids is None else list(dict.fromkeys(task_ids))
            known = [owned[i] for i in wanted if i in owned]
            pending = {t.handle for t in known if not t.done and t.handle is not None}
            limit = run.limits.delegation_timeout
            # Without a timeout, wait until the tasks end (at most the delegation timeout): a
            # short default made models call this again and again, each time a model call.
            wait = timeout if timeout is not None else limit
            bound = None if wait is None else max(0.0, wait if limit is None else min(wait, limit))
            if pending:
                await asyncio.wait(pending, timeout=bound)
            run.raise_fatal()
            parts = []
            for task in known:
                if task.done and not task.delivered:
                    task.delivered = True
                    parts.append(task.render())
            running = [t.id for t in known if not t.done]
            unknown = [i for i in wanted if i not in owned]
            if running:
                parts.append(f"Still running: {', '.join(running)}.")
            if unknown:
                parts.append(
                    f"Unknown task ids: {', '.join(unknown)}. Background tasks do not survive "
                    "a pause for a human decision; delegate again if you still need the result."
                )
            return (
                "\n\n".join(parts)
                or "No new results: every requested task was already handed back."
            )

        async def cancel_delegation(task_id: str) -> str:
            ctx, run = lookup_run(self._key, self._team, "cancel_delegation")
            task = next((t for t in run.owned(ctx.run_id) if t.id == task_id), None)
            if task is None:
                return f"Unknown task id {task_id!r}."
            if task.done:
                return f"Task {task_id} already finished with status {task.status}."
            if task.handle is not None:
                task.handle.cancel()
                await asyncio.gather(task.handle, return_exceptions=True)
            task.delivered = True
            return f"Cancelled task {task_id}."

        return [
            StructuredTool.from_function(
                coroutine=check_delegations,
                name="check_delegations",
                description="Show the status of your background tasks.",
                args_schema=_NoArgs,
            ),
            StructuredTool.from_function(
                coroutine=wait_for_delegations,
                name="wait_for_delegations",
                description=(
                    "Wait until background tasks finish and return their results, in one "
                    "call: it blocks until they end. Omit task_ids to wait for all of them."
                ),
                args_schema=_WaitArgs,
            ),
            StructuredTool.from_function(
                coroutine=cancel_delegation,
                name="cancel_delegation",
                description="Cancel a background task that is still running.",
                args_schema=_CancelArgs,
            ),
        ]

    def admit_turn(
        self,
        ctx: RunContext[Any],
        run: TeamRun,
        source: str,
        call_id: str,
        messages: Sequence[BaseMessage],
    ) -> Admission | None:
        """Admit the delegation calls of the model turn containing `call_id`, in tool-call order.

        The first call of a turn to arrive decides for every unanswered delegation call of that turn, so
        the earlier call wins a contested slot regardless of scheduling. Returns `None` for a call with
        invalid arguments.
        """

        if call_id not in run.admissions:
            for call in pending_calls(call_id, messages):
                schema = self._schemas.get((source, call["name"]))
                ident = call["id"]
                if schema is None or ident is None or ident in run.admissions:
                    continue
                try:
                    args = schema.model_validate(call["args"])
                except ValidationError:
                    continue
                target = str(getattr(args, DELEGATION_TOOLS[call["name"]]))
                background = bool(getattr(args, "background", False))
                if f"{source}:{ident}" in run.resumed:
                    # Re-entered on resume: it was counted when it first started.
                    run.calls[target] = run.calls.get(target, 1) - 1
                mode: Mode = "consult" if call["name"] == "consult_peer" else "delegate"
                decision = self._admit(ctx, run, target, background=background, mode=mode)
                run.admissions[ident] = Admission(
                    ctx.run_id,
                    target,
                    background,
                    decision,
                    call_id=ident,
                    slot=_takes_slot(ctx, run, mode),
                )
        return run.admissions.pop(call_id, None)

    def release_unused(self, run: TeamRun, owner_run_id: str) -> None:
        """Release the slots reserved for calls of `owner_run_id` that never ran."""

        for ident, admission in list(run.admissions.items()):
            if admission.owner_run_id == owner_run_id:
                del run.admissions[ident]
                _release_admission(run, admission)

    @contextmanager
    def granted(self, run: TeamRun, admission: Admission) -> Iterator[None]:
        """Make `admission` available to the tool call; an unconsumed reserved slot is released on exit."""

        token = _grant.set((run, admission))
        try:
            yield
        finally:
            _grant.reset(token)
            if not admission.used:
                _release_admission(run, admission)

    def _cycle(self, ctx: RunContext[Any], run: TeamRun, target: str, brief: str) -> str | None:
        """The refusal of a call that would loop on the call chain (see `TeamLimits.allow_cycles`)."""

        link = AgentKey(target, scope=self._team, brief_hash=brief_hash(brief))
        refusal = cycle_refusal(ctx.ids.chain, link, strict=not run.limits.allow_cycles)
        if refusal is not None:
            ctx.report_limit_exceeded(
                limit="call_cycle", value=len(ctx.ids.chain) + 1, message=refusal
            )
        return refusal

    async def _boundary(
        self, ctx: RunContext[Any], target: str, brief: str, mode: Mode
    ) -> tuple[AgentCall, CallOutcome]:
        """Ask the boundary hooks of the chain about the call (deny wins, rewrites chain)."""

        call = AgentCall(mode, ctx.agent, target, brief, self._team, ctx.ids.branch)
        outcome = await decide_call(ctx, call)
        if outcome.brief != brief:
            # A rewritten brief is text like any other: framing tags stay escaped.
            outcome = CallOutcome(outcome.denied, neutralize(outcome.brief))
        return call, outcome

    async def _delegate(
        self, ctx: RunContext[Any], run: TeamRun, target: str, task: str, brief: str, *, mode: Mode
    ) -> str:
        if (refusal := self._cycle(ctx, run, target, brief)) is not None:
            return frame_result(target, "error", refusal)
        call, outcome = await self._boundary(ctx, target, brief, mode)
        if outcome.denied is not None:
            return frame_result(target, "error", neutralize(outcome.denied))
        brief = outcome.brief
        refusal = self._reserve(ctx, run, target, background=False, mode=mode)
        if refusal is not None:
            return frame_result(target, "error", refusal)
        grant = _grant.get()
        key = f"{ctx.agent}:{grant[1].call_id}" if grant is not None and grant[1].call_id else ""
        resumed = run.resumed.pop(key, None) if key else None
        correlation_id = resumed or _new_id()
        if key:
            run.started[key] = correlation_id
        try:
            if resumed is None:
                await _emit_started(ctx, target, task, mode, correlation_id)
            status, body = await self._run_target(ctx, run, target, brief, correlation_id)
        finally:
            if _takes_slot(ctx, run, mode):
                run.release_slot()
        if status == "ok":
            body = neutralize(await decide_result(ctx, call, body))
        await _emit_result(ctx, target, correlation_id, status, body)
        return frame_result(target, status, body)

    async def _start_background(
        self, ctx: RunContext[Any], run: TeamRun, target: str, task: str, brief: str
    ) -> str:
        if (refusal := self._cycle(ctx, run, target, brief)) is not None:
            return frame_result(target, "error", refusal)
        call, outcome = await self._boundary(ctx, target, brief, "delegate")
        if outcome.denied is not None:
            return frame_result(target, "error", neutralize(outcome.denied))
        brief = outcome.brief
        decision = self._reserve(ctx, run, target, background=True, mode="delegate")
        if isinstance(decision, str):
            return frame_result(target, "error", decision)
        record = BackgroundTask(
            id=_new_id(),
            owner_run_id=ctx.run_id,
            owner=ctx.agent,
            agent=target,
            task=task,
            scope=_scope.get(),
        )
        try:
            await _emit_started(ctx, target, task, "delegate", record.id)
        except BaseException:
            run.release_slot()
            run.pending_background -= 1
            raise
        # The task outlives this tool call, so it must not run inside the caller's
        # LangGraph execution (its checkpoint namespace ends with the call).
        record.handle = asyncio.create_task(
            self._run_background(ctx, run, record, brief, call), context=current_engine().detached()
        )
        # A done callback (not the coroutine) releases the slot, so a task
        # cancelled before it started still frees it and is marked cancelled.
        record.handle.add_done_callback(lambda _: _finish_background(run, record))
        run.background[record.id] = record
        run.pending_background -= 1
        return (
            f'<task_started id="{record.id}" agent="{target}"/>\n'
            "The result will be handed back to you automatically when it is ready. Use "
            "check_delegations, wait_for_delegations or cancel_delegation to manage it."
        )

    async def _run_background(
        self,
        ctx: RunContext[Any],
        run: TeamRun,
        record: BackgroundTask,
        brief: str,
        call: AgentCall,
    ) -> None:
        try:
            status, body = await self._run_target(
                ctx, run, record.agent, brief, record.id, background=True
            )
            if status == "ok":
                body = neutralize(await decide_result(ctx, call, body))
        except asyncio.CancelledError:
            record.status, record.content = "cancelled", "Cancelled."
            raise
        except Exception as exc:  # usage limit or control flow: fails the team run
            record.status, record.content, record.fatal = "error", error_text(str(exc)), exc
            return
        record.status, record.content = status, body
        await _emit_result(ctx, record.agent, record.id, status, body)
        await ctx.emit(
            AgentMessageEvent(
                message=AgentMessage(
                    sender=record.agent,
                    recipient=record.owner,
                    subject=f"Background task {record.id}: {status}",
                    content=body,
                    data={
                        "handback": {"task_id": record.id, "agent": record.agent, "status": status}
                    },
                ),
                **ctx.event_fields(),
            )
        )

    def _reserve(
        self, ctx: RunContext[Any], run: TeamRun, target: str, *, background: bool, mode: Mode
    ) -> str | None:
        """Take the decision admitted for this tool call, or admit it now."""

        grant = _grant.get()
        if grant is not None:
            granted_run, admission = grant
            if (
                granted_run is run
                and not admission.used
                and admission.target == target
                and admission.background == background
            ):
                admission.used = True
                return admission.rejection
        return self._admit(ctx, run, target, background=background, mode=mode)

    def _admit(
        self, ctx: RunContext[Any], run: TeamRun, target: str, *, background: bool, mode: Mode
    ) -> str | None:
        """Reserve a delegation slot, or explain why it cannot start."""

        limits = run.limits
        depth = ctx.depth - run.base_depth
        if background and _needs_approval(self._runtime[target]):
            return (
                f"Not started: {target!r} has tools that need a human approval, and a "
                "background delegation cannot pause for one. Delegate to it with "
                "background=false."
            )
        if depth > limits.max_delegation_depth:
            return _refuse(
                ctx,
                "max_delegation_depth",
                depth,
                limits.max_delegation_depth,
                "Not started: the delegation depth limit "
                f"(max_delegation_depth={limits.max_delegation_depth}) is reached. Do this "
                "part yourself.",
            )
        calls = run.calls.get(target, 0)
        if limits.max_calls_per_member is not None and calls >= limits.max_calls_per_member:
            return _refuse(
                ctx,
                "max_calls_per_member",
                calls + 1,
                limits.max_calls_per_member,
                f"Not started: {target!r} already received {calls} requests in this run "
                f"(max_calls_per_member={limits.max_calls_per_member}).",
            )
        if not _takes_slot(ctx, run, mode):
            run.calls[target] = calls + 1
            return None
        if run.in_flight >= limits.max_parallel_delegations:
            return _refuse(
                ctx,
                "max_parallel_delegations",
                run.in_flight + 1,
                limits.max_parallel_delegations,
                f"Not started: {run.in_flight} delegations are already running "
                f"(max_parallel_delegations={limits.max_parallel_delegations}). Wait for "
                "their results, then delegate again.",
            )
        background_tasks = run.running_background() + run.pending_background
        if background and background_tasks >= limits.max_background_tasks:
            return _refuse(
                ctx,
                "max_background_tasks",
                background_tasks + 1,
                limits.max_background_tasks,
                f"Not started: {background_tasks} background tasks are already "
                f"running (max_background_tasks={limits.max_background_tasks}). Wait for "
                "them or delegate in the foreground.",
            )
        run.calls[target] = calls + 1
        run.in_flight += 1
        if background:
            run.pending_background += 1
        return None

    async def _run_target(
        self,
        ctx: RunContext[Any],
        run: TeamRun,
        target: str,
        brief: str,
        correlation_id: str,
        *,
        background: bool = False,
    ) -> tuple[Status, str]:
        """Run `target` on `brief`; return the status and the safe result body."""

        agent = self._runtime[target]
        timeout = run.limits.delegation_timeout
        scope = asyncio.timeout(timeout)
        marker = object()
        token = _scope.set(marker)
        written: list[BlackboardEntry] = []
        unwatch = (
            self._blackboard.watch(
                lambda entry: (
                    written.append(entry) if entry.agent_call_id == correlation_id else None
                )
            )
            if self._blackboard is not None
            else None
        )
        try:
            link = AgentKey(target, scope=self._team, brief_hash=brief_hash(brief))
            budget = _delegation_budget(ctx, run)
            async with scope:
                with agent_call(ctx, link, agent_call_id=correlation_id, budget=budget):
                    result = await agent.run(brief, deps=ctx.deps)
                handbacks = await run.settle(result.run_id, wait=True)
        except UsageLimitExceededError as exc:
            if release_nested_stop(ctx, exc):
                # A member that repeats itself ends its delegation, not the whole run.
                return "error", error_text(f"{target!r} stopped without progress: {exc}")
            if exc.details.get("delegation") != correlation_id:
                raise  # the run tree's own limits end the whole run
            return "error", error_text(
                f"stopped:budget - {target!r} used up the budget of one delegation "
                f"(delegation_limits) and was stopped; its partial work is lost: {exc}. "
                "Continue with what you have, delegate a smaller task, or answer without it."
            )
        except current_engine().control_flow:
            raise
        except TimeoutError as exc:
            if scope.expired():
                return "error", f"{target!r} did not finish within {timeout:g} seconds."
            return "error", error_text(f"{target!r} failed: TimeoutError: {exc}")
        except Exception as exc:
            if background and isinstance(exc, AgentRunError) and "interrupted" in str(exc):
                return "error", _background_pause(target)
            return "error", error_text(f"{target!r} failed: {type(exc).__name__}: {exc}")
        finally:
            if unwatch is not None:
                unwatch()
            _scope.reset(token)
            # Background work the target started must not outlive a failed,
            # timed-out or cancelled delegation (after success it is settled).
            await run.cancel_scope(marker)
        if result.is_interrupted and background:
            return "error", _background_pause(target)
        if result.is_interrupted:
            return "error", (
                f"{target!r} paused on a human-in-the-loop interrupt on its own thread, which "
                "the team cannot resume. Give the team a checkpointer instead of the member."
            )
        body = await self._result_body(run, target, correlation_id, result, written, handbacks)
        return "ok", body

    async def _result_body(
        self,
        run: TeamRun,
        target: str,
        correlation_id: str,
        result: RunResult[Any],
        written: Sequence[BlackboardEntry],
        handbacks: Sequence[BackgroundTask],
    ) -> str:
        """The safe text of a finished delegation: its result, what it shared and handed back."""

        body = await self._fit(run, target, correlation_id, neutralize(output_text(result)))
        body += _shared_digest(written, run.limits.max_result_chars - len(body))
        if result.run_id in run.step_stops:
            body = (
                f"stopped:steps - {target!r} reached its step limit and answered with what it "
                f"had; the answer may be incomplete.\n{body}"
            )
        if handbacks:
            blocks = "\n".join(task.render() for task in handbacks)
            body += f"\n\nResults of background tasks {target} started:\n{blocks}"
        return body

    async def _fit(self, run: TeamRun, target: str, correlation_id: str, text: str) -> str:
        limit = run.limits.max_result_chars
        if len(text) <= limit:
            return text
        note = f"Result truncated at {limit} of {len(text)} characters."
        if self._blackboard is not None:
            key = f"results/{correlation_id}"
            await self._blackboard.write(key, text, author=target)
            note += (
                f" The full result is in the shared workspace under key {key!r}; "
                "read it with read_shared."
            )
        return f"{text[:limit]}\n[{note}]"


def _delegation_budget(ctx: RunContext[Any], run: TeamRun) -> UsageLimits:
    """The limits of one delegation: `delegation_limits`, or a share of the caller's remaining budget.

    The remaining calls, less `ROOT_RESERVE`, are shared among concurrent delegations (from
    `BUDGET_STAGES_FROM` calls on), capped by `TeamLimits.delegation_budget` when no own
    `delegation_limits` are set.
    """

    limits = run.limits.delegation_limits
    if limits is not None and limits.max_requests is not None:
        return limits
    budget = remaining_requests(ctx)
    if budget is None or budget[1] < BUDGET_STAGES_FROM:
        return run.limits.delegation_budget()
    share = max(1, (budget[0] - ROOT_RESERVE) // max(1, run.in_flight))
    if limits is None:
        default = run.limits.delegation_budget()
        if default.max_requests is not None:
            return default.model_copy(update={"max_requests": min(share, default.max_requests)})
    return (limits or UsageLimits()).model_copy(update={"max_requests": share})


def _shared_digest(entries: Sequence[BlackboardEntry], room: int) -> str:
    """The shared-workspace values a member wrote, inlined so the caller need not read them."""

    latest: dict[str, BlackboardEntry] = {}
    for entry in entries:
        latest[entry.key] = entry
    current = [entry for entry in latest.values() if not entry.deleted]
    if not current or room <= 200:
        return ""
    each = max(200, min(SHARED_ENTRY_CHARS, room // len(current)))
    lines = []
    for entry in current:
        text = entry.value if isinstance(entry.value, str) else _json(entry.value)
        text = neutralize(text)
        if len(text) > each:
            text = f"{text[:each]} [shortened; read_shared {entry.key!r} has all of it]"
        lines.append(f"- {entry.key} (version {entry.version}): {text}")
    return (
        "\n\nShared workspace entries it wrote (current values; no need to read them again):\n"
        + "\n".join(lines)
    )


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


async def _emit_started(
    ctx: RunContext[Any], target: str, task: str, mode: Mode, correlation_id: str
) -> None:
    await ctx.emit(
        DelegationEvent(
            target=target, task=task, mode=mode, correlation_id=correlation_id, **ctx.event_fields()
        )
    )


async def _emit_result(
    ctx: RunContext[Any], target: str, correlation_id: str, status: Status, body: str
) -> None:
    await ctx.emit(
        DelegationResultEvent(
            target=target,
            correlation_id=correlation_id,
            status=status,
            content_preview=body[:PREVIEW_CHARS],
            **ctx.event_fields(),
        )
    )


def _refuse(ctx: RunContext[Any], limit: str, value: int, maximum: int, message: str) -> str:
    """Report the exceeded limit and return `message` as the refusal text."""

    ctx.report_limit_exceeded(limit=limit, value=value, maximum=maximum, message=message)
    return message


def _takes_slot(ctx: RunContext[Any], run: TeamRun, mode: Mode) -> bool:
    """Whether a call from `ctx` holds a delegation slot (a consultation inside a delegation does not)."""

    return mode == "delegate" or ctx.depth - run.base_depth <= 1


def _release_admission(run: TeamRun, admission: Admission) -> None:
    """Give back what `Delegator._admit` took for a call that never ran."""

    if admission.rejection is None:
        run.calls[admission.target] -= 1
        if admission.slot:
            run.release_slot()
        if admission.background:
            run.pending_background -= 1


def _finish_background(run: TeamRun, record: BackgroundTask) -> None:
    run.release_slot()
    if record.status == "running":
        record.status, record.content = "cancelled", "Cancelled."


def _needs_approval(agent: AgentLike) -> bool:
    """Whether `agent` has tools that pause for a human approval (`@tool(approval=...)` or
    `create_tool_approval_wrapper`)."""

    return isinstance(agent, Agent) and any(asks_for_approval(tool) for tool in agent.tools)


def _background_pause(target: str) -> str:
    return (
        f"{target!r} stopped to wait for a human decision (an approval), but it ran as a "
        "background delegation, which cannot pause. Delegate to it again with "
        "background=false."
    )


def _new_id() -> str:
    return f"dlg_{uuid4().hex[:12]}"


class _NoArgs(BaseModel):
    pass


class _WaitArgs(BaseModel):
    task_ids: list[str] | None = Field(
        default=None, description="Ids of the tasks to wait for; omit to wait for all."
    )
    timeout: float | None = Field(
        default=None, ge=0, description="Maximum seconds to wait; omit to wait until they end."
    )


class _CancelArgs(BaseModel):
    task_id: str = Field(description="Id of the task to cancel.")
