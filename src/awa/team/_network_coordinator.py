"""The lead of an `AgentNetwork`: coordination turns, pauses and the result of a run."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from awa._internal.upstream.messages import BaseMessage
from awa._internal.util import elapsed_ms
from awa.contracts.results import RunResult
from awa.errors import AgentRunError
from awa.persistence.records import RecordStore
from awa.runtime.engine import current_engine
from awa.tasks import Task, TaskNotFoundError, TaskStatus
from awa.team._handoffs import team_note
from awa.team._network_state import NetworkRun, Parked
from awa.team._network_workers import Workers
from awa.team._run import render_handback
from awa.team._scoped import RunScope

_HANDBACK_ID = re.compile(r'<handback task_id="([^"]+)"')
_TASK_REF = re.compile(r"\{\{\s*task:\s*([A-Za-z0-9_.:-]+?)\s*\}\}")


class Coordinator(Workers):
    """Coordinating a run: lead turns until the goal is done or paused."""

    _records: RecordStore
    _scope: RunScope

    async def _work(self, run: NetworkRun, started: float, stream_text: bool) -> RunResult[Any]:
        """Run the coordinator and one worker per teammate, then build the result."""

        board = self._board
        limit = self._limits.max_concurrent_teammates
        run.slots = asyncio.Semaphore(limit) if limit is not None else None
        # Workers outlive tool calls, so the network runs outside any enclosing run.
        with current_engine().outside_run(stream_text=stream_text):
            paused: str | None = None
            try:
                unwatch = board.watch(lambda change: self._on_change(run, change))
                workers = [
                    asyncio.create_task(
                        self._worker(run, teammate.name), name=f"{self._name}:{teammate.name}"
                    )
                    for teammate in self._teammates
                ]
                try:
                    await self._coordinate_in_time(run, workers)
                finally:
                    await _stop(workers)
                    unwatch()
                run.raise_fatal()
                result = await self._result(run, started)
                paused = run.thread if result.is_interrupted else None
            finally:
                # Also on cancellation and timeouts: unread mail is reported, not lost.
                await self._scope.finish(run.ctx, run.resources, paused_on=paused)
        return result

    async def _coordinate_in_time(
        self, run: NetworkRun, workers: Sequence[asyncio.Task[None]]
    ) -> None:
        """Coordinate until `run_timeout`; then the lead answers, or the run pauses with its approvals."""

        timeout = self._limits.run_timeout
        scope = asyncio.timeout(timeout)
        try:
            async with scope:
                await self._coordinate(run)
        except TimeoutError:
            if not scope.expired():
                raise
            run.stop_reason = "run_timeout"
            await _stop(workers)
            if not run.parked:
                if not self._out_of_lead_turns(run):
                    await self._lead_turn(run, self._closing_note(run, timed_out=True))
                return
            # Pending approvals are never dropped: the run pauses with them.
            run.ctx.report_limit_exceeded(
                limit="run_timeout",
                value=timeout,
                maximum=timeout,
                message=(
                    f"Network {self._name!r} reached run_timeout={timeout:g}s and pauses with "
                    "its pending approvals; resume it to continue."
                ),
            )

    async def _result(self, run: NetworkRun, started: float) -> RunResult[Any]:
        """The run's result; with a thread, its progress record is saved first."""

        summary = {status.value: 0 for status in TaskStatus}
        for task_id in run.tasks:
            summary[self._board.get(task_id).status.value] += 1
        common: dict[str, Any] = {
            "run_id": run.ctx.run_id,
            "agent": self._lead.name,
            "usage": run.ctx.usage.snapshot(),
            "duration_ms": elapsed_ms(started),
            "handoff_path": (self._lead.name,),
            "thread_id": run.thread,
            "metadata": {
                "tasks": summary,
                "handbacks": run.handback_count,
                "lead_turns": run.lead_turns,
                "stop_reason": run.stop_reason,
            },
        }
        interrupts = await self._pending_interrupts(run)
        if run.thread is not None:
            await self._records.save(run.thread, run.to_record(interrupts))
        if interrupts:
            return RunResult(output=None, interrupts=interrupts, **common)
        final = run.lead
        if final is None:
            raise AgentRunError(f"Network {self._name!r} finished without a final answer")
        return RunResult(
            output=self._resolve(final.output),
            text=self._resolve(final.text),
            messages=final.messages,
            **common,
        )

    def _resolve(self, value: Any) -> Any:
        """Replace every `{{task:<id>}}` in the lead's answer with that task's full result."""

        if isinstance(value, str):
            return resolve_task_refs(value, self._task_result)
        if isinstance(value, dict):
            return {key: self._resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        return value

    def _task_result(self, task_id: str) -> str | None:
        try:
            task = self._board.get(task_id)
        except TaskNotFoundError:  # an unknown id stays as written
            return None
        return task.result if task.status.is_terminal and task.result else None

    async def _pending_interrupts(self, run: NetworkRun) -> list[dict[str, Any]]:
        """Every interrupt of the parked runs: the lead's, then by task, then message turns."""

        for key, item in list(run.parked.items()):
            if item.task_id is not None and self._owned_task(item.task_id, item.agent) is None:
                del run.parked[key]  # the lead cancelled or released the task meanwhile
                await self._withdraw(run, item)
        order = {task_id: index for index, task_id in enumerate(run.tasks)}
        rank = {"lead": 0, "task": 1, "message": 2}

        def position(item: Parked) -> tuple[int, int]:
            return rank[item.kind], order.get(item.task_id or "", len(order))

        return [
            {**interrupt, "agent": item.agent, "task_id": item.task_id}
            for item in sorted(run.parked.values(), key=position)
            for interrupt in item.interrupts
        ]

    async def _coordinate(self, run: NetworkRun) -> None:
        """Run lead turns until the goal is done or paused; see the class docstring."""

        limits = self._limits
        if not run.resumed:
            await self._lead_turn(run, run.goal)
        elif run.lead_resume is not None:
            item, decision = run.lead_resume
            run.lead_resume = None
            lead = self._agents[self._lead.name]
            await self._lead_result(run, await lead.resume(item.thread, decision))
        while True:
            run.changed.clear()
            run.raise_fatal()
            news = bool(run.handbacks) or run.lead_mail
            quiescent = self._quiescent(run)
            if (
                news
                and not run.lead_parked
                and run.lead_turns < limits.max_lead_turns - 1
                and (run.parked or not quiescent)
            ):
                await self._lead_turn(run, self._update_note(run))
                run.final_turn = False
            elif not quiescent:
                await run.changed.wait()
            elif run.parked:
                run.stop_reason = "interrupted"
                return
            elif not await self._closing_turn(run, news):
                return

    async def _closing_turn(self, run: NetworkRun, news: bool) -> bool:
        """Wait for outside progress or ask for the answer; `False` when the run is finished."""

        limits = self._limits
        open_tasks = self._open_tasks(run)
        waiting = any(t.status is TaskStatus.IN_PROGRESS for t in open_tasks)
        if waiting and not news and not run.idle_expired:
            try:
                async with asyncio.timeout(limits.idle_timeout):
                    await run.changed.wait()
            except TimeoutError:
                run.idle_expired = True
                run.stop_reason = "idle_timeout"
            return True
        if not news and (run.final_turn or not run.tasks):
            return False
        if self._out_of_lead_turns(run):
            return False
        await self._lead_turn(run, self._closing_note(run, open_tasks=open_tasks))
        run.final_turn = True
        return True

    def _out_of_lead_turns(self, run: NetworkRun) -> bool:
        """Whether `max_lead_turns` leaves no closing turn (then reported as the stop reason)."""

        maximum = self._limits.max_lead_turns
        if run.lead_turns < maximum:
            return False
        run.stop_reason = "max_lead_turns"
        run.ctx.report_limit_exceeded(
            limit="max_lead_turns",
            value=run.lead_turns + 1,
            maximum=maximum,
            message=(
                f"Network {self._name!r} reached max_lead_turns={maximum} and finished "
                "without a closing turn."
            ),
        )
        return True

    async def _lead_turn(self, run: NetworkRun, input: list[BaseMessage]) -> None:
        run.lead_turns += 1
        run.lead_mail = False
        lead = self._agents[self._lead.name]
        if run.lead_thread is not None:
            # The lead's conversation lives on its thread.
            result = await lead.run(input, thread_id=run.lead_thread)
        else:
            if run.lead is not None:
                history: list[BaseMessage] = run.lead.messages
            else:
                history = [] if input is run.goal else run.goal
            result = await lead.run(input, history=history)
        await self._lead_result(run, result)

    async def _lead_result(self, run: NetworkRun, result: RunResult[Any]) -> None:
        if not result.is_interrupted:
            run.lead = result
            return
        if run.lead_thread is None:
            raise AgentRunError(
                f"Lead {self._lead.name!r} paused on an interrupt on its own thread "
                f"{result.thread_id!r}, which network {self._name!r} cannot resume. Give the "
                "network a checkpointer (instead of the lead) to pause the whole network."
            )
        run.park("lead", self._lead.name, run.lead_thread, result)
        run.notify()

    def _quiescent(self, run: NetworkRun) -> bool:
        """Whether no teammate works and none can start without the lead."""

        if run.busy or any(run.resumes.values()):
            return False
        return not any(
            self._next_task(run, teammate) is not None
            or self._wants_message_turn(run, teammate.name)
            for teammate in self._teammates
        )

    def _open_tasks(self, run: NetworkRun) -> list[Task]:
        tasks = (self._board.get(task_id) for task_id in run.tasks)
        return [task for task in tasks if not task.is_settled]

    def _update_note(self, run: NetworkRun) -> list[BaseMessage]:
        text = "\n\n".join(
            [
                "Updates from your network:",
                *run.take_handbacks(),
                "Teammates are still working; you will be called again with further results. "
                "Adjust the plan if needed, otherwise acknowledge briefly.",
            ]
        )
        return [team_note("delivery", text)]

    def _closing_note(
        self, run: NetworkRun, *, open_tasks: Sequence[Task] = (), timed_out: bool = False
    ) -> list[BaseMessage]:
        # Handbacks the lead has not seen yet; every other settled result reached it already.
        pending = {match for block in run.take_handbacks() for match in _HANDBACK_ID.findall(block)}
        tasks = [self._board.get(task_id) for task_id in run.tasks]
        if timed_out:
            header = (
                f"Time is up (run_timeout={self._limits.run_timeout:g}s): the network stopped "
                "before all work was finished. Write the best final answer from the results "
                "so far; do not create new tasks."
            )
        else:
            header = "All work that can progress is finished. Write the final answer for the user."
        parts = [header, _board_summary(tasks)]
        if open_tasks:
            listing = ", ".join(f"{t.id} ({t.status.value})" for t in open_tasks)
            parts.append(
                f"These tasks are still open and no teammate can work on them: {listing}. "
                "Cancel them, create tasks a teammate can do, or answer with what you have."
            )
        settled = [task for task in tasks if task.status.is_terminal]
        delivered = [task for task in settled if task.id not in pending]
        results = [
            render_handback(
                task.id,
                task.owner or "unassigned",
                task.status.value,
                self._handed_back(task, task.result or ""),
            )
            for task in settled
            if task.id in pending
        ]
        if delivered:
            listing = ", ".join(f"{t.id} ({t.status.value})" for t in delivered)
            parts.append(
                f"Results you already received: {listing}. Use them as handed back; get_task "
                "shows a full result, and {{task:<id>}} in your answer inserts one verbatim."
            )
        if results:
            parts += ["New results:", *results]
        return [team_note("delivery", "\n\n".join(parts))]


async def _stop(workers: Iterable[asyncio.Task[None]]) -> None:
    pending = [worker for worker in workers if not worker.done()]
    for worker in pending:
        worker.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def resolve_task_refs(text: str, lookup: Callable[[str], str | None]) -> str:
    """Replace each `{{task:<id>}}` in `text` with `lookup(id)`; unknown ids stay as written."""

    def replace(match: re.Match[str]) -> str:
        found = lookup(match.group(1))
        return match.group(0) if found is None else found

    return _TASK_REF.sub(replace, text)


def _board_summary(tasks: Sequence[Task]) -> str:
    counts = Counter(task.status.value for task in tasks)
    listing = ", ".join(f"{count} {status}" for status, count in counts.items())
    return f"Tasks of this run: {listing or 'none'}."
