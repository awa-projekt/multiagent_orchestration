"""The contract of a `RunLog`: what every backend of the run log keeps.

`count_rows` lets the suite prove that nothing is kept where nothing may be.

```python
class TestMyRunLog(RunLogContract):
    def opened(self, place, ids, pseudonym):
        return my_run_log_context(place.path, ids, pseudonym)   # an async context manager

    async def count_rows(self, place, table, run_id=None):
        ...
```
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta

import pytest

from awa.errors import (
    ConfigurationError,
    PermissionDeniedError,
    RunNotFoundError,
    StoreConflictError,
)
from awa.persistence.runlog import (
    Approval,
    AttemptRecord,
    CheckRecord,
    PseudonymKey,
    RunLog,
    Scope,
    StepRecord,
    StepResult,
    SystemScope,
    ToolCallRecord,
)
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.doubles import FixedIds

ACME = Scope("acme", "invoices", principal="ana")
GLOBEX = Scope("globex", "invoices", principal="gus")
ACME_OTHER_PROJECT = Scope("acme", "payroll")
CAPTURING = Scope("acme", "invoices", principal="ana", capture=True)
AUDITOR = Scope("acme", "invoices", principal="audrey", roles=frozenset({"auditor"}))
KEY = PseudonymKey(b"contract-suite-pseudonym-key-32b", key_id="k1")
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
RUN_TABLES = (
    "run_log_head",
    "run_step",
    "step_attempt",
    "step_result",
    "approval",
    "captured_content",
    "data_subject_link",
)


def _step(run_id: str, key: str, ordinal: int, **fields: object) -> StepRecord:
    return StepRecord(run_id=run_id, step_key=key, ordinal=ordinal, **fields)


class RunLogContract(AdapterContract):
    """Run this against a `RunLog`: implement `opened` and `count_rows`."""

    def opened(
        self, place: Place, ids: FixedIds, pseudonym: PseudonymKey | None
    ) -> AbstractAsyncContextManager[RunLog]:
        """The run log on `place`, with ids from `ids` and people linked with `pseudonym`."""

        raise NotImplementedError

    async def count_rows(self, place: Place, table: str, run_id: str | None = None) -> int:
        """How many rows `table` holds (only those of `run_id`, when given), read directly."""

        raise NotImplementedError

    @pytest.fixture
    async def log(self, place: Place, ids: FixedIds) -> AsyncIterator[RunLog]:
        async with self.opened(place, ids, KEY) as log:
            yield log

    async def _run(self, log: RunLog, scope: Scope = ACME, run_id: str = "proc_1") -> str:
        await log.open_run(scope, run_id, now=T0, initiator=scope.principal)
        return run_id

    def test_the_tenant_and_project_must_always_be_given(self) -> None:
        for tenant, project in (("", "p"), ("t", ""), ("  ", "p"), (None, "p"), ("t\x00", "p")):
            with pytest.raises(ValueError):
                Scope(tenant, project)  # type: ignore[arg-type]

    async def test_another_scope_sees_and_changes_nothing(self, log: RunLog, place: Place) -> None:
        run = await self._run(log)
        await log.save_step(ACME, _step(run, "extract", 0), expected_revision=None)
        await log.start_attempt(ACME, run, "extract", now=T0)
        await log.put_result(
            ACME, StepResult(run_id=run, step_key="extract", attempt=1, kind="final", created_at=T0)
        )
        await log.request_approval(ACME, Approval(run_id=run, requested_at=T0))

        for other in (GLOBEX, ACME_OTHER_PROJECT):
            assert await log.head(other, run) is None
            assert await log.steps(other, run) == []
            assert await log.attempts(other, run) == []
            assert await log.results(other, run) == []
            assert await log.approvals(other, run) == []
            assert await log.project_runs(other) == []
            assert await log.delete_runs(other, [run]) == 0
            with pytest.raises(RunNotFoundError):
                await log.save_step(other, _step(run, "x", 5), expected_revision=None)
            with pytest.raises(RunNotFoundError):
                await log.start_attempt(other, run, "extract", now=T0)
            with pytest.raises(StoreConflictError):
                await log.open_run(other, run, now=T0)
        assert [s.step_key for s in await log.steps(ACME, run)] == ["extract"]
        assert await self.count_rows(place, "run_log_head") == 1

    async def test_steps_come_back_by_position(self, log: RunLog) -> None:
        run = await self._run(log)
        for key, ordinal in (("book", 2), ("extract", 0), ("check", 1)):
            await log.save_step(ACME, _step(run, key, ordinal), expected_revision=None)

        assert [s.step_key for s in await log.steps(ACME, run)] == ["extract", "check", "book"]

    async def test_a_stale_revision_is_a_conflict(self, log: RunLog) -> None:
        run = await self._run(log)
        first = await log.save_step(ACME, _step(run, "a", 0), expected_revision=None)
        second = await log.save_step(
            ACME, first.model_copy(update={"status": "checked"}), expected_revision=first.revision
        )

        assert second.revision == first.revision + 1
        assert second.status == "checked"
        with pytest.raises(StoreConflictError):
            await log.save_step(ACME, first, expected_revision=first.revision)
        with pytest.raises(StoreConflictError):
            await log.save_step(ACME, first, expected_revision=None)
        with pytest.raises(StoreConflictError):  # the position is taken
            await log.save_step(ACME, _step(run, "b", 0), expected_revision=None)

    async def test_attempts_are_numbered_by_the_log_and_totals_are_their_sums(
        self, log: RunLog
    ) -> None:
        run = await self._run(log)
        await log.save_step(ACME, _step(run, "a", 0), expected_revision=None)
        usage = [(100, 20, 10, 5, 1500), (200, 40, 0, 0, 2500), (50, 5, 0, 0, 700)]
        for number, (inp, out, read, write, cost) in enumerate(usage, start=1):
            attempt = await log.start_attempt(ACME, run, "a", now=T0 + timedelta(seconds=number))
            assert attempt.attempt == number
            await log.finish_attempt(
                ACME,
                attempt.model_copy(
                    update={
                        "status": "failed" if number < 3 else "succeeded",
                        "ended_at": T0 + timedelta(seconds=number, milliseconds=500),
                        "model": "model-a",
                        "agent_run_id": f"run_agent_{number}",
                        "retry_reason": "check" if number < 3 else None,
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_read_tokens": read,
                        "cache_write_tokens": write,
                        "cost_micros": cost,
                    }
                ),
            )

        [step] = await log.steps(ACME, run)
        attempts = await log.attempts(ACME, run, step_key="a")
        assert [a.attempt for a in attempts] == [1, 2, 3]
        assert [a.status for a in attempts] == ["failed", "failed", "succeeded"]
        assert [a.agent_run_id for a in attempts] == ["run_agent_1", "run_agent_2", "run_agent_3"]
        assert step.attempts == 3
        assert step.started_at == T0 + timedelta(seconds=1)
        assert (step.input_tokens, step.output_tokens) == (350, 65)
        assert (step.cache_read_tokens, step.cache_write_tokens) == (10, 5)
        assert step.cost_micros == 4700
        assert step.model == "model-a"

    async def test_the_cost_is_unknown_while_an_attempt_has_no_price(self, log: RunLog) -> None:
        run = await self._run(log)
        await log.save_step(ACME, _step(run, "a", 0), expected_revision=None)
        for cost in (1000, None):
            attempt = await log.start_attempt(ACME, run, "a", now=T0)
            await log.finish_attempt(
                ACME, attempt.model_copy(update={"status": "succeeded", "cost_micros": cost})
            )

        [step] = await log.steps(ACME, run)
        assert step.cost_micros is None

    async def test_concurrent_attempts_get_distinct_numbers(self, log: RunLog) -> None:
        run = await self._run(log)
        await log.save_step(ACME, _step(run, "a", 0), expected_revision=None)

        started = await asyncio.gather(
            *(log.start_attempt(ACME, run, "a", now=T0) for _ in range(8))
        )

        assert sorted(a.attempt for a in started) == list(range(1, 9))
        [step] = await log.steps(ACME, run)
        assert step.attempts == 8

    async def test_an_attempt_of_an_unknown_step_is_refused(self, log: RunLog) -> None:
        run = await self._run(log)

        with pytest.raises(RunNotFoundError):
            await log.start_attempt(ACME, run, "missing", now=T0)
        with pytest.raises(RunNotFoundError):
            await log.finish_attempt(
                ACME, AttemptRecord(run_id=run, step_key="missing", attempt=1, started_at=T0)
            )

    async def test_results_round_trip_in_order_with_one_final_per_step(
        self, log: RunLog, place: Place
    ) -> None:
        run = await self._run(log)
        await log.save_step(ACME, _step(run, "check", 1), expected_revision=None)
        await log.save_step(ACME, _step(run, "extract", 0), expected_revision=None)
        checks = (
            CheckRecord(id="extract.rule.1", kind="rule", passed=False, message="No total."),
            CheckRecord(id="extract.schema", kind="schema", passed=True),
        )
        calls = (
            ToolCallRecord(call_id="c1", name="lookup", status="ok", arguments_length=12),
            ToolCallRecord(call_id="c2", name="post", status="error", result_length=40),
        )
        output = {"total": 1234.5, "lines": [{"text": "Bürobedarf €", "n": 2}], "ok": True}
        await log.put_result(
            ACME,
            StepResult(
                run_id=run,
                step_key="check",
                attempt=1,
                kind="final",
                output={"x": 1},
                created_at=T0,
            ),
        )
        await log.put_result(
            ACME,
            StepResult(
                run_id=run,
                step_key="extract",
                attempt=1,
                kind="intermediate",
                output=output,
                tool_calls=calls,
                checks=checks,
                answer_length=77,
                created_at=T0 + timedelta(microseconds=123456),
            ),
        )
        await log.put_result(
            ACME,
            StepResult(
                run_id=run,
                step_key="extract",
                attempt=2,
                kind="final",
                output={"v": 1},
                created_at=T0,
            ),
        )
        replaced = await log.put_result(
            ACME,
            StepResult(
                run_id=run,
                step_key="extract",
                attempt=2,
                kind="final",
                output={"v": 2},
                created_at=T0,
            ),
        )

        found = await log.results(ACME, run)
        assert [(r.step_key, r.attempt, r.kind) for r in found] == [
            ("extract", 1, "intermediate"),
            ("extract", 2, "final"),
            ("check", 1, "final"),
        ]
        first = found[0]
        assert first.output == output
        assert first.checks == checks
        assert first.tool_calls == calls
        assert first.answer_length == 77
        assert first.created_at == T0 + timedelta(microseconds=123456)
        assert found[1].result_id == replaced.result_id
        assert found[1].output == {"v": 2}
        assert await log.results(ACME, run, kind="final", step_key="extract") == [found[1]]
        assert await self.count_rows(place, "step_result", run) == 3

    async def test_capture_off_stores_no_content_anywhere(self, log: RunLog, place: Place) -> None:
        run = await self._run(log)

        ref = await log.capture(ACME, run, "input_messages", "PROMPT-SENTINEL", now=T0)

        assert ref is None
        assert await self.count_rows(place, "captured_content") == 0

    async def test_the_capture_switch_is_frozen_when_the_run_opens(self, log: RunLog) -> None:
        await self._run(log, ACME, "proc_off")
        await log.open_run(CAPTURING, "proc_off", now=T0)

        head = await log.head(CAPTURING, "proc_off")
        assert head is not None
        assert head.capture is False
        assert await log.capture(CAPTURING, "proc_off", "system", "x", now=T0) is None

    async def test_capture_on_keeps_bodies_for_auditors_and_records_each_read(
        self, log: RunLog, place: Place
    ) -> None:
        run = await self._run(log, CAPTURING)
        body = [{"role": "user", "content": "Prüfe die Rechnung"}]

        ref = await log.capture(CAPTURING, run, "input_messages", body, now=T0)

        assert ref is not None
        with pytest.raises(PermissionDeniedError):
            await log.content(CAPTURING, ref)
        found = await log.content(AUDITOR, ref)
        assert found is not None
        assert (found.kind, found.body, found.run_id) == ("input_messages", body, run)
        await log.content(AUDITOR, ref)
        assert (
            await log.content(Scope("globex", "invoices", roles=frozenset({"auditor"})), ref)
            is None
        )
        assert await self.count_rows(place, "captured_content_read") == 2

    async def test_an_approval_is_decided_once_by_a_named_reviewer(self, log: RunLog) -> None:
        run = await self._run(log)
        asked = await log.request_approval(
            ACME,
            Approval(run_id=run, step_key="book", requested_at=T0, reasons=("Total above limit",)),
        )

        with pytest.raises(ValueError):
            await log.decide_approval(
                ACME, asked.approval_id, decision="approved", reviewer=" ", reason=None, now=T0
            )
        decided = await log.decide_approval(
            ACME,
            asked.approval_id,
            decision="rejected",
            reviewer="rita",
            reason="Wrong cost center",
            now=T0 + timedelta(minutes=5),
        )
        with pytest.raises(StoreConflictError):
            await log.decide_approval(
                ACME, asked.approval_id, decision="approved", reviewer="rita", reason=None, now=T0
            )
        with pytest.raises(RunNotFoundError):
            await log.decide_approval(
                GLOBEX, asked.approval_id, decision="approved", reviewer="gus", reason=None, now=T0
            )

        assert (decided.decision, decided.reviewer, decided.reason) == (
            "rejected",
            "rita",
            "Wrong cost center",
        )
        assert decided.decided_at == T0 + timedelta(minutes=5)
        assert await log.approvals(ACME, run) == [decided]
        assert decided.reasons == ("Total above limit",)

    async def test_people_are_linked_by_hmac_to_the_runs_they_started_or_decided(
        self, log: RunLog
    ) -> None:
        await self._run(log, ACME, "proc_a")
        await log.open_run(ACME, "proc_b", now=T0, initiator="bob")
        asked = await log.request_approval(ACME, Approval(run_id="proc_b", requested_at=T0))
        await log.decide_approval(
            ACME, asked.approval_id, decision="approved", reviewer="ana", reason=None, now=T0
        )

        assert await log.subject_runs(ACME, "ana") == ["proc_a", "proc_b"]
        assert await log.subject_runs(ACME, "bob") == ["proc_b"]
        assert await log.subject_runs(GLOBEX, "ana") == []
        assert await log.subject_runs(ACME, "nobody") == []

    async def test_without_a_key_nobody_is_linked_and_person_lookups_are_refused(
        self, place: Place, ids: FixedIds
    ) -> None:
        async with self.opened(place, ids, None) as log:
            await log.open_run(ACME, "proc_1", now=T0, initiator="ana")

            with pytest.raises(ConfigurationError):
                await log.subject_runs(ACME, "ana")
        assert await self.count_rows(place, "data_subject_link") == 0

    async def test_deleting_runs_removes_every_row_they_own(
        self, log: RunLog, place: Place
    ) -> None:
        for run in ("proc_gone", "proc_kept"):
            await log.open_run(CAPTURING, run, now=T0, initiator="ana")
            await log.save_step(CAPTURING, _step(run, "a", 0), expected_revision=None)
            attempt = await log.start_attempt(CAPTURING, run, "a", now=T0)
            await log.finish_attempt(CAPTURING, attempt.model_copy(update={"status": "succeeded"}))
            result = await log.put_result(
                CAPTURING,
                StepResult(run_id=run, step_key="a", attempt=1, kind="final", created_at=T0),
            )
            asked = await log.request_approval(
                CAPTURING, Approval(run_id=run, result_id=result.result_id, requested_at=T0)
            )
            await log.decide_approval(
                CAPTURING, asked.approval_id, decision="approved", reviewer="rita", reason=None,
                now=T0,
            )  # fmt: skip
            await log.capture(CAPTURING, run, "output_messages", "answer", now=T0)

        assert await log.delete_runs(ACME, ["proc_gone", "proc_gone", "proc_missing"]) == 1

        for table in RUN_TABLES:
            assert await self.count_rows(place, table, "proc_gone") == 0, table
            assert await self.count_rows(place, table, "proc_kept") >= 1, table
        assert await log.project_runs(ACME) == ["proc_kept"]

    async def test_expired_lists_old_runs_of_every_tenant_oldest_first(self, log: RunLog) -> None:
        await log.open_run(ACME, "proc_new", now=T0 + timedelta(days=40))
        await log.open_run(GLOBEX, "proc_old", now=T0)
        await log.open_run(ACME, "proc_mid", now=T0 + timedelta(days=10))

        found = await log.expired(SystemScope("retention"), before=T0 + timedelta(days=30))

        assert [(r.run_id, r.tenant) for r in found] == [
            ("proc_old", "globex"),
            ("proc_mid", "acme"),
        ]
        assert found[0].opened_at == T0
        assert (
            await log.expired(SystemScope("x"), before=T0 + timedelta(days=30), limit=1)
            == found[:1]
        )


__all__ = ["RunLogContract"]
