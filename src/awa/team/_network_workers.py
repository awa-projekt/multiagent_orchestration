"""The teammates of an `AgentNetwork` at work: tasks, message turns, parked runs and observers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext, suppress
from typing import Any

from awa._internal.logging import get_logger
from awa.agents import Agent
from awa.contracts.communication import AgentMessage
from awa.contracts.events import AgentMessageEvent, DelegationEvent, DelegationResultEvent
from awa.contracts.results import RunResult
from awa.errors import AgentRunError, TaskError, UsageLimitExceededError
from awa.messaging import MessageBus
from awa.runtime.context import current_run_context
from awa.runtime.engine import current_engine
from awa.runtime.termination import ends_the_run
from awa.tasks import BaseTaskBoard, Task, TaskChange, TaskStatus
from awa.tasks.board import render_tasks
from awa.team._delegation import output_text
from awa.team._handoffs import team_note
from awa.team._network_state import NetworkRun, Parked
from awa.team._run import PREVIEW_CHARS, error_text, neutralize, render_handback
from awa.team.limits import HANDBACK_SUMMARY_CHARS, NetworkLimits

_logger = get_logger("team.network")


RUN_TAG = "awa.run"
"""Task data key naming the network run that created a task."""
_AWAITING_APPROVAL = "awa.awaiting_approval"


class Workers:
    """What a run does with its teammates; `AgentNetwork` inherits it through `Coordinator`."""

    _key: str
    _name: str
    _lead: Agent[Any, Any]
    _teammates: tuple[Agent[Any, Any], ...]
    _agents: dict[str, Agent[Any, Any]]
    _limits: NetworkLimits
    _board: BaseTaskBoard
    _bus: MessageBus

    def _current(self) -> NetworkRun | None:
        """The run of this network the calling code belongs to, if any."""

        ctx = current_run_context()
        run = ctx.internal.get(self._key) if ctx is not None else None
        return run if isinstance(run, NetworkRun) else None

    def _teammate(self, name: str) -> Agent[Any, Any]:
        return next(teammate for teammate in self._teammates if teammate.name == name)

    def _owned_task(self, task_id: str, name: str) -> Task | None:
        """The task if `name` still works on it (the lead may cancel or release it meanwhile)."""

        task = self._board.get(task_id) if task_id in self._board else None
        if task is None or task.owner != name or task.status is not TaskStatus.IN_PROGRESS:
            return None
        return task

    async def _withdraw(self, run: NetworkRun, item: Parked) -> None:
        """Report a parked task whose approval is no longer wanted (never dropped silently)."""

        text = (
            f"{item.task_id} was cancelled or released while {item.agent} waited for a human "
            f"decision; its pending approval ({len(item.interrupts)} interrupt(s)) was withdrawn."
        )
        _logger.warning("Network %r: %s", self._name, text)
        await run.ctx.emit(
            DelegationResultEvent(
                target=item.agent,
                correlation_id=item.task_id or item.key,
                status="error",
                content_preview=text[:PREVIEW_CHARS],
                **run.ctx.event_fields(),
            )
        )

    async def _mark_awaiting(self, name: str, task_id: str, waiting: bool) -> None:
        """Flag a parked task on the board (`data["awa.awaiting_approval"]`)."""

        note = (
            "Waiting for a human decision; the work continues once it is given."
            if waiting
            else None
        )
        with suppress(TaskError):
            await self._board.update(
                task_id, author=name, note=note, data={_AWAITING_APPROVAL: waiting}
            )

    async def _worker(self, run: NetworkRun, name: str) -> None:
        """Resume `name`'s parked runs that got a decision, then claim and do tasks."""

        teammate = self._teammate(name)
        wake = run.wake.setdefault(name, asyncio.Event())
        try:
            # Resumes stay listed until done, so the coordinator never sees a gap.
            while resumes := run.resumes.get(name):
                item, decision = resumes[0]
                try:
                    await self._occupy(run, name, self._resume_parked(run, name, item, decision))
                finally:
                    resumes.pop(0)
            while True:
                wake.clear()
                if (task := self._next_task(run, teammate)) is not None:
                    await self._occupy(run, name, self._do_task(run, name, task))
                elif self._wants_message_turn(run, name):
                    await self._occupy(run, name, self._message_turn(run, name))
                else:
                    await wake.wait()
        except Exception as exc:  # a usage limit or another fatal error ends the run
            run.fatal = exc
            run.notify()

    async def _occupy(self, run: NetworkRun, name: str, work: Awaitable[None]) -> None:
        """Do `work` for `name` in a concurrency slot, marked busy for the coordinator."""

        async with run.slots or nullcontext():
            run.busy.add(name)
            try:
                await work
            finally:
                run.busy.discard(name)
                run.notify()

    def _next_task(self, run: NetworkRun, teammate: Agent[Any, Any]) -> Task | None:
        attempts = self._limits.max_task_attempts
        for task in self._board.ready(teammate.name, teammate.card.capabilities):
            if task.id in run.tasks and run.attempts.get(task.id, 0) < attempts:
                return task
        return None

    def _wants_message_turn(self, run: NetworkRun, name: str) -> bool:
        if name in run.busy or run.message_turns.get(name, 0) >= self._limits.max_message_turns:
            return False
        return self._bus.pending_count(name) > 0

    async def _do_task(self, run: NetworkRun, name: str, candidate: Task) -> None:
        capabilities = self._teammate(name).card.capabilities
        try:
            task = await self._board.claim(candidate.id, name, capabilities=capabilities)
        except TaskError:
            return  # claimed by someone else in the meantime
        attempt = run.attempts[task.id] = run.attempts.get(task.id, 0) + 1
        thread = f"{run.prefix}/{task.id}/{attempt}" if run.thread is not None else None
        await run.ctx.emit(
            DelegationEvent(
                target=name,
                task=task.title,
                mode="delegate",
                correlation_id=task.id,
                **run.ctx.event_fields(),
            )
        )
        brief = self._brief(run, task)
        await self._run_task(
            run, name, task.id, lambda: self._agents[name].run(brief, thread_id=thread)
        )

    async def _resume_parked(self, run: NetworkRun, name: str, item: Parked, decision: Any) -> None:
        """Continue a teammate's parked run with the human decision."""

        def resume() -> Awaitable[RunResult[Any]]:
            return self._agents[name].resume(item.thread, decision)

        if item.task_id is None:
            await self._message_run(run, name, item.thread, resume)
        elif self._owned_task(item.task_id, name) is not None:
            await self._mark_awaiting(name, item.task_id, False)
            await self._run_task(run, name, item.task_id, resume)
        else:
            await self._withdraw(run, item)

    async def _run_task(
        self,
        run: NetworkRun,
        name: str,
        task_id: str,
        start: Callable[[], Awaitable[RunResult[Any]]],
    ) -> None:
        """Run (or resume) a teammate on its claimed task; park, settle or release it."""

        board = self._board
        result: RunResult[Any] | None = None
        error: str | None = None
        try:
            result = await start()
            if result.is_interrupted:
                if run.thread is None or result.thread_id is None:
                    raise AgentRunError(
                        f"Teammate {name!r} paused on an interrupt on its own thread "
                        f"{result.thread_id!r}, which network {self._name!r} cannot resume. "
                        "Give the network a checkpointer (instead of the teammate)."
                    )
                run.park("task", name, result.thread_id, result, task_id)
                await self._mark_awaiting(name, task_id, True)
                return
        except asyncio.CancelledError:
            if task_id not in run.parked:
                with suppress(TaskError):
                    if board.get(task_id).status is TaskStatus.IN_PROGRESS:
                        await board.release(task_id, author=name)
            raise
        except Exception as exc:
            if isinstance(exc, current_engine().control_flow) or ends_the_run(run.ctx, exc):
                raise
            result, error = None, error_text(f"{type(exc).__name__}: {exc}")
        await self._settle_task(run, name, task_id, error, result)

    async def _settle_task(
        self,
        run: NetworkRun,
        name: str,
        task_id: str,
        error: str | None,
        result: RunResult[Any] | None,
    ) -> None:
        """Record a teammate's outcome on the board and hand it back; lead changes take precedence."""

        board = self._board
        task = board.get(task_id)
        if task.owner == name and task.status is TaskStatus.IN_PROGRESS:
            with suppress(TaskError):
                if result is not None:
                    text = output_text(result).strip() or "(no result text)"
                    await board.complete(task_id, author=name, result=text)
                else:
                    await board.fail(task_id, author=name, reason=error or "No result.")
            task = board.get(task_id)
        attempts = run.attempts.get(task_id, 0)
        if task.status is TaskStatus.FAILED and attempts < self._limits.max_task_attempts:
            run.retrying.add(task_id)
            try:
                with suppress(TaskError):
                    await board.release(task_id, author=name)
            finally:
                run.retrying.discard(task_id)
            _logger.info("Network %r: retrying %s after a failed attempt", self._name, task_id)
            return
        if task.status in (TaskStatus.PENDING, TaskStatus.CANCELLED):
            return
        await self._hand_back(run, name, task, attempts)

    async def _hand_back(self, run: NetworkRun, name: str, task: Task, attempts: int) -> None:
        """Give the lead the settled `task`: a handback note, a result event and a message."""

        status = task.status.value
        body = task.result or ""
        if task.status is TaskStatus.FAILED:
            body = f"Failed after {attempts} attempt(s): {body}"
        elif task.status is TaskStatus.BLOCKED:
            body = "; ".join(note.text for note in task.notes[-1:]) or "Reported blocked."
        body = self._handed_back(task, body)
        run.handbacks.append(render_handback(task.id, name, status, body))
        run.handback_count += 1
        fields = run.ctx.event_fields()
        await run.ctx.emit(
            DelegationResultEvent(
                target=name,
                correlation_id=task.id,
                status="ok" if task.status is TaskStatus.COMPLETED else "error",
                content_preview=body[:PREVIEW_CHARS],
                **fields,
            )
        )
        await run.ctx.emit(
            AgentMessageEvent(
                message=AgentMessage(
                    sender=name,
                    recipient=self._lead.name,
                    subject=f"{task.id} {status}: {task.title}",
                    content=body,
                    data={"handback": {"task_id": task.id, "agent": name, "status": status}},
                ),
                **fields,
            )
        )

    async def _message_turn(self, run: NetworkRun, name: str) -> None:
        turn = run.message_turns[name] = run.message_turns.get(name, 0) + 1
        note = team_note(
            "messages",
            "You have no task right now, but new messages arrived. Act on them: answer "
            "requests with read_inbox and reply_to_message, or message the sender.",
        )
        thread = f"{run.prefix}/{name}/messages/{turn}" if run.thread is not None else None
        await self._message_run(
            run, name, thread, lambda: self._agents[name].run([note], thread_id=thread)
        )

    async def _message_run(
        self,
        run: NetworkRun,
        name: str,
        thread: str | None,
        start: Callable[[], Awaitable[RunResult[Any]]],
    ) -> None:
        """Run (or resume) a message turn; a paused one is parked, a failed one logged."""

        try:
            result = await start()
        except Exception as exc:
            if _is_fatal(exc):
                raise
            _logger.warning(
                "Network %r: message turn of %r failed: %s",
                self._name,
                name,
                error_text(f"{type(exc).__name__}: {exc}"),
            )
            return
        if result.is_interrupted and thread is not None and run.thread is not None:
            run.park("message", name, thread, result)
        elif result.is_interrupted:
            _logger.warning(
                "Network %r: message turn of %r paused on its own thread %r, which the network "
                "cannot resume; give the network a checkpointer",
                self._name,
                name,
                result.thread_id,
            )

    def _brief(self, run: NetworkRun, task: Task) -> str:
        board = self._board
        sections = [f"Task {task.id}: {task.title}"]
        if task.description.strip():
            sections.append(task.description.strip())
        dependencies = [board.get(task_id) for task_id in task.depends_on]
        if dependencies:
            lines = [
                f"- {dep.id} ({dep.title}): {self._fit(neutralize(dep.result or ''), dep.id)}"
                for dep in dependencies
            ]
            sections.append("Results of the tasks this builds on:\n" + "\n".join(lines))
        own = [board.get(task_id) for task_id in run.tasks]
        ready = {t.id for t in board.ready()}
        sections.append(
            "Open work on the task board:\n" + render_tasks(own, ready, include_done=False)
        )
        sections.append(
            "Answer with the complete result of this task. If you cannot do it, call fail_task "
            f"with task_id {task.id!r} and the reason."
        )
        return "\n\n".join(sections)

    def _fit(self, text: str, task_id: str) -> str:
        limit = self._limits.max_result_chars
        if len(text) <= limit:
            return text
        return (
            f"{text[:limit]}\n[Shortened to {limit} of {len(text)} characters; get_task "
            f"{task_id!r} shows the full result.]"
        )

    def _handed_back(self, task: Task, body: str) -> str:
        """`body` as the lead receives it: by reference for a completed task, else shortened."""

        text = neutralize(body)
        if task.status is TaskStatus.COMPLETED:
            return self._by_reference(text, task.id)
        return self._fit(text, task.id)

    def _by_reference(self, text: str, task_id: str) -> str:
        """A long completed result as a summary and its task id (`NetworkLimits.handback_chars`)."""

        if len(text) <= self._limits.handback_chars:
            return text
        head = text[: min(HANDBACK_SUMMARY_CHARS, self._limits.handback_chars)].rstrip()
        return (
            f"{head} ...\n[Summary of a {len(text)}-character result kept on the board: "
            f"get_task {task_id!r} shows it, and {{{{task:{task_id}}}}} in your final answer "
            "inserts it verbatim.]"
        )

    def _on_change(self, run: NetworkRun, change: TaskChange) -> None:
        task = change.task
        if change.kind == "created":
            if task.data.get(RUN_TAG) != run.tag:
                return  # created by another run (on a shared board) or outside the network
            run.tasks[task.id] = None
            run.created += 1
        elif change.kind == "released" and task.id not in run.retrying:
            run.attempts.pop(task.id, None)  # released by the lead: a fresh retry
        run.notify()

    async def _on_message(self, message: AgentMessage) -> None:
        current = current_run_context()
        run = self._current()
        if current is None or run is None or message.sender not in self._agents:
            return
        if message.recipient == self._lead.name:
            run.lead_mail = True
        run.notify()
        await run.ctx.emit(AgentMessageEvent(message=message, **current.event_fields()))


def _is_fatal(exc: BaseException) -> bool:
    """Errors that end the whole run instead of failing one task."""

    return isinstance(exc, (UsageLimitExceededError,) + current_engine().control_flow)
