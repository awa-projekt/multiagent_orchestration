"""The contract of a `RunStore`: what every store of process runs does the same way."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from awa.automation.models import ProcessRun, WaitState
from awa.automation.store import RunStore
from awa.errors import ProcessRunStateError, RunNotFoundError
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.doubles import FakeClock, FixedIds

LEASE = timedelta(minutes=5)


@dataclass(frozen=True)
class RunStoreCapabilities:
    """What a store promises beyond the base contract.

    Attributes:
        survives_restart: A second store opened on the same `Place` sees the runs and state.
        normalizes_payload: A payload comes back as JSON (a tuple as a list, a datetime as text).
        claims_run_created_in_the_future: A queued run created ahead of the worker's clock can be
            claimed at once; a store that waits for that time (PostgreSQL) leaves this off.
    """

    survives_restart: bool = False
    normalizes_payload: bool = False
    claims_run_created_in_the_future: bool = False


class RunStoreContract(AdapterContract):
    """Run this against a `RunStore`: implement `opened` and set `capabilities`."""

    capabilities = RunStoreCapabilities()

    def opened(self, place: Place) -> AbstractAsyncContextManager[RunStore]:
        """An async context manager yielding a store on `place`; opening twice is a restart."""

        raise NotImplementedError

    @staticmethod
    def run(
        clock: FakeClock, ids: FixedIds, automation: str = "invoices", **fields: Any
    ) -> ProcessRun:
        """A queued run created now by the fake clock, with a predictable id."""

        fields.setdefault("created_at", clock.now())
        fields.setdefault("updated_at", fields["created_at"])
        return ProcessRun(id=ids.new_id("proc"), automation=automation, **fields)

    async def test_create_stores_the_run_and_get_returns_an_equal_copy(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            run = self.run(clock, ids, input="pay invoice 7", payload={"id": 7})
            stored, created = await store.create(run)
            found = await store.get(run.id)

            assert created and stored == run
            assert found == run and found is not stored
            assert await store.get("proc_missing") is None

    async def test_a_second_run_with_the_same_id_is_refused(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            run = self.run(clock, ids)
            await store.create(run)

            with pytest.raises(ProcessRunStateError):
                await store.create(run)

    async def test_the_same_dedup_key_returns_the_existing_run(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            first, _ = await store.create(self.run(clock, ids, dedup_key="k"))
            again, created = await store.create(self.run(clock, ids, dedup_key="k"))
            other, other_created = await store.create(self.run(clock, ids, dedup_key="other"))
            elsewhere, elsewhere_created = await store.create(
                self.run(clock, ids, "payments", dedup_key="k")
            )

            assert not created and again.id == first.id
            assert other_created and other.id != first.id
            assert elsewhere_created and elsewhere.automation == "payments"

    async def test_dedup_since_ignores_older_runs(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            first, _ = await store.create(self.run(clock, ids, dedup_key="k"))
            clock.advance(hours=2)
            same, created_same = await store.create(
                self.run(clock, ids, dedup_key="k"), dedup_since=clock.now() - timedelta(hours=3)
            )
            fresh, created_fresh = await store.create(
                self.run(clock, ids, dedup_key="k"), dedup_since=clock.now() - timedelta(hours=1)
            )

            assert not created_same and same.id == first.id
            assert created_fresh and fresh.id != first.id

    async def test_list_returns_newest_first_and_filters(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            made = []
            for automation, tenant in (("a", "t1"), ("a", "t2"), ("b", "t1")):
                clock.advance(minutes=1)
                run, _ = await store.create(self.run(clock, ids, automation, tenant=tenant))
                made.append(run)
            await store.modify(
                made[0].id, lambda r: r.model_copy(update={"status": "failed"}), now=clock.now()
            )

            assert [r.id for r in await store.list()] == [r.id for r in reversed(made)]
            assert [r.id for r in await store.list(automation="a")] == [made[1].id, made[0].id]
            assert [r.id for r in await store.list(tenant="t1")] == [made[2].id, made[0].id]
            assert [r.id for r in await store.list(status="failed")] == [made[0].id]
            both = await store.list(status=["failed", "queued"], automation="a")
            assert [r.id for r in both] == [made[1].id, made[0].id]
            assert [r.id for r in await store.list(limit=1)] == [made[2].id]
            assert await store.list(limit=0) == []

    async def test_list_returns_at_most_a_hundred_runs_by_default(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            for _ in range(105):
                await store.create(self.run(clock, ids))

            assert len(await store.list()) == 100
            assert len(await store.list(limit=101)) == 101

    async def test_list_refuses_an_unknown_status(self, place: Place) -> None:
        async with self.opened(place) as store:
            with pytest.raises(ValueError):
                await store.list(status="nonsense")

    async def test_the_payload_round_trips_as_json(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        payload = {"n": 1, "x": 1.5, "s": "Müller", "l": [1, [2]], "none": None, "d": {"a": {}}}
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids, payload=payload))
            found = await store.get(run.id)

            assert found is not None and found.payload == payload

    async def test_a_large_payload_round_trips(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        payload = {"rows": [{"n": n, "text": "x" * 40} for n in range(2000)]}
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids, payload=payload))
            found = await store.get(run.id)

            assert found is not None and found.payload == payload

    async def test_a_tuple_in_the_payload_comes_back_as_a_list(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        if not self.capabilities.normalizes_payload:
            pytest.skip("keeps the payload object as given: a tuple stays a tuple")
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids, payload={"pair": (1, 2)}))
            found = await store.get(run.id)

            assert found is not None and found.payload == {"pair": [1, 2]}

    async def test_a_datetime_in_the_payload_comes_back_as_text(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        if not self.capabilities.normalizes_payload:
            pytest.skip("keeps the payload object as given: a datetime stays a datetime")
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids, payload={"at": clock.now()}))
            found = await store.get(run.id)

            assert found is not None and isinstance(found.payload["at"], str)

    async def test_runs_created_at_the_same_time_are_listed_by_id_newest_first(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            made = [(await store.create(self.run(clock, ids)))[0] for _ in range(3)]

            assert [r.id for r in await store.list()] == [r.id for r in reversed(made)]

    async def test_concurrent_creates_with_one_dedup_key_make_one_run(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            results = await asyncio.gather(
                *[store.create(self.run(clock, ids, dedup_key="k")) for _ in range(5)]
            )

            assert [created for _, created in results].count(True) == 1
            assert len({run.id for run, _ in results}) == 1
            assert len(await store.list()) == 1

    async def test_returned_runs_are_copies(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids, payload={"k": [1]}))
            run.payload["k"].append(2)
            first = await store.get(run.id)
            assert first is not None
            first.payload["k"].append(3)
            again = await store.get(run.id)

            assert again is not None and again.payload == {"k": [1]}

    async def test_modify_applies_the_change_bumps_the_revision_and_stamps_the_time(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids))
            clock.advance(hours=1)
            changed = await store.modify(
                run.id, lambda r: r.model_copy(update={"output": "done"}), now=clock.now()
            )

            assert changed.output == "done" and changed.revision == run.revision + 1
            assert changed.updated_at == clock.now()
            assert await store.get(run.id) == changed

    async def test_modify_that_returns_nothing_changes_nothing(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids))
            clock.advance(hours=1)
            same = await store.modify(run.id, lambda r: None, now=clock.now())

            assert same == run and await store.get(run.id) == run

    async def test_modify_hands_the_change_a_copy(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        def scribble(run: ProcessRun) -> None:
            run.output = "scribbled"
            run.payload = {"touched": True}

        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids))
            await store.modify(run.id, scribble, now=clock.now())

            assert await store.get(run.id) == run

    async def test_a_change_that_raises_leaves_the_run_as_it_was(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        def refuse(run: ProcessRun) -> ProcessRun:
            raise RuntimeError("no")

        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids))
            with pytest.raises(RuntimeError):
                await store.modify(run.id, refuse, now=clock.now())

            assert await store.get(run.id) == run

    async def test_modify_of_an_unknown_run_raises(self, place: Place, clock: FakeClock) -> None:
        async with self.opened(place) as store:
            with pytest.raises(RunNotFoundError):
                await store.modify("proc_missing", lambda r: r, now=clock.now())

    async def test_claim_leases_the_oldest_ready_run_to_one_worker(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            old, _ = await store.create(self.run(clock, ids))
            clock.advance(minutes=1)
            new, _ = await store.create(self.run(clock, ids))
            clock.advance(minutes=1)
            first = await store.claim("invoices", worker="w1", lease=LEASE, now=clock.now())
            second = await store.claim("invoices", worker="w2", lease=LEASE, now=clock.now())
            third = await store.claim("invoices", worker="w3", lease=LEASE, now=clock.now())

            assert first is not None and second is not None and third is None
            assert (first.id, second.id) == (old.id, new.id)
            assert first.status == "running" and first.lease_owner == "w1"
            assert first.attempts == 1 and first.lease_expires_at == clock.now() + LEASE
            assert first.revision == old.revision + 1
            assert await store.get(old.id) == first

    async def test_claim_only_takes_queued_runs_of_its_automation(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            await store.create(self.run(clock, ids, "payments"))
            await store.create(self.run(clock, ids, status="succeeded"))

            assert await store.claim("invoices", worker="w", lease=LEASE, now=clock.now()) is None

    async def test_claim_skips_a_run_that_waits_for_its_retry_time(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            retry_at = clock.now() + timedelta(minutes=10)
            run, _ = await store.create(self.run(clock, ids, next_attempt_at=retry_at))

            assert await store.claim("invoices", worker="w", lease=LEASE, now=clock.now()) is None
            clock.advance(minutes=10)
            claimed = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())
            assert claimed is not None and claimed.id == run.id

    async def test_claim_breaks_ties_by_id(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            made = [(await store.create(self.run(clock, ids)))[0] for _ in range(3)]
            claims = [
                await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())
                for _ in range(3)
            ]

            assert [c.id for c in claims if c is not None] == [r.id for r in made]

    async def test_claim_takes_a_run_created_after_its_clock_reading(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        # "A run is ready when `next_attempt_at` is not in the future" (RunStore.claim): the
        # creation time does not matter, so a worker whose clock lags still sees the run. A store
        # that measures from the creation time says so (a capability) and releases the run then.
        async with self.opened(place) as store:
            ahead = clock.now() + timedelta(minutes=5)
            run, _ = await store.create(self.run(clock, ids, created_at=ahead))
            claimed = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())

            if not self.capabilities.claims_run_created_in_the_future:
                assert claimed is None
                clock.advance(minutes=5)
                claimed = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())
            assert claimed is not None and claimed.id == run.id

    async def test_claim_filters_by_version(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            v1, _ = await store.create(self.run(clock, ids, version="v1"))
            unpinned, _ = await store.create(self.run(clock, ids))
            now = clock.now()

            only = await store.claim("invoices", worker="w", lease=LEASE, now=now, versions=["v2"])
            none_left = await store.claim(
                "invoices", worker="w", lease=LEASE, now=now, versions=["v2"]
            )
            pinned = await store.claim(
                "invoices", worker="w", lease=LEASE, now=now, versions=["v1", "v2"]
            )

            assert only is not None and only.id == unpinned.id  # a run without a version fits any
            assert none_left is None
            assert pinned is not None and pinned.id == v1.id

    async def test_claim_stops_at_max_running(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            for _ in range(3):
                await store.create(self.run(clock, ids))
            kwargs: dict[str, Any] = {"lease": LEASE, "now": clock.now(), "max_running": 2}

            assert await store.claim("invoices", worker="a", **kwargs) is not None
            assert await store.claim("invoices", worker="b", **kwargs) is not None
            assert await store.claim("invoices", worker="c", **kwargs) is None

    async def test_running_runs_of_another_automation_do_not_count_toward_max_running(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            await store.create(self.run(clock, ids, "payments"))
            await store.create(self.run(clock, ids))
            await store.claim("payments", worker="a", lease=LEASE, now=clock.now())

            claimed = await store.claim(
                "invoices", worker="b", lease=LEASE, now=clock.now(), max_running=1
            )
            assert claimed is not None

    async def test_concurrent_claims_never_share_a_run(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            for _ in range(6):
                await store.create(self.run(clock, ids))
            claims = await asyncio.gather(
                *[
                    store.claim("invoices", worker=f"w{n}", lease=LEASE, now=clock.now())
                    for n in range(8)
                ]
            )
            taken = [run.id for run in claims if run is not None]

            assert len(taken) == 6 and len(set(taken)) == 6

    async def test_a_resumed_run_keeps_its_attempt_count(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            await store.create(self.run(clock, ids, attempts=1, resuming=True))
            claimed = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())

            assert claimed is not None and claimed.attempts == 1

    async def test_renew_extends_the_lease_of_the_worker_that_holds_it(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            await store.create(self.run(clock, ids))
            run = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())
            assert run is not None
            clock.advance(minutes=4)

            assert await store.renew(run.id, worker="w", lease=LEASE, now=clock.now())
            held = await store.get(run.id)
            assert held is not None and held.lease_expires_at == clock.now() + LEASE
            assert not await store.renew(run.id, worker="other", lease=LEASE, now=clock.now())
            assert not await store.renew("proc_missing", worker="w", lease=LEASE, now=clock.now())

    async def test_each_claim_has_a_new_lease_epoch_and_renew_refuses_an_old_one(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            await store.create(self.run(clock, ids, max_attempts=3))
            first = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())
            assert first is not None and first.lease_epoch == 1

            def requeue(run: ProcessRun) -> ProcessRun:
                return run.model_copy(
                    update={"status": "queued", "lease_owner": None, "lease_expires_at": None}
                )

            await store.modify(first.id, requeue, now=clock.now())
            second = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())
            assert second is not None and second.lease_epoch == 2

            assert not await store.renew(
                first.id, worker="w", lease=LEASE, now=clock.now(), epoch=first.lease_epoch
            )
            assert await store.renew(
                first.id, worker="w", lease=LEASE, now=clock.now(), epoch=second.lease_epoch
            )

    async def test_renew_does_nothing_for_a_run_that_is_not_running(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            run, _ = await store.create(self.run(clock, ids))

            assert not await store.renew(run.id, worker="w", lease=LEASE, now=clock.now())

    async def test_expired_lists_running_runs_whose_lease_ran_out(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            await store.create(self.run(clock, ids))
            run = await store.claim("invoices", worker="w", lease=LEASE, now=clock.now())
            assert run is not None

            assert await store.expired("invoices", now=clock.now() + LEASE) == []
            clock.advance(minutes=6)
            [lapsed] = await store.expired("invoices", now=clock.now())
            assert lapsed.id == run.id and lapsed.lease_owner == "w"
            assert await store.expired("payments", now=clock.now()) == []

    async def test_due_lists_unfinished_runs_by_the_time_they_need_attention(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        soon = clock.now() + timedelta(hours=1)
        async with self.opened(place) as store:
            late, _ = await store.create(
                self.run(clock, ids, sla_deadline=soon + timedelta(hours=1))
            )
            early, _ = await store.create(self.run(clock, ids, sla_deadline=soon))
            await store.create(self.run(clock, ids))  # no deadline: never due
            await store.create(self.run(clock, ids, status="succeeded", sla_deadline=soon))

            assert await store.due("invoices", now=clock.now()) == []
            assert [r.id for r in await store.due("invoices", now=soon)] == [early.id]  # inclusive
            clock.advance(hours=3)
            due = await store.due("invoices", now=clock.now())
            assert [r.id for r in due] == [early.id, late.id]
            limited = await store.due("invoices", now=clock.now(), limit=1)
            assert [r.id for r in limited] == [early.id]
            assert await store.due("payments", now=clock.now()) == []

    async def test_waiting_on_finds_runs_with_an_unanswered_wait_for_the_key(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        def waits(key: str, *, answered: bool = False) -> list[WaitState]:
            return [
                WaitState(
                    interrupt_id=f"i-{key}", key=key, requested_at=clock.now(), answered=answered
                )
            ]

        async with self.opened(place) as store:
            waiting, _ = await store.create(
                self.run(clock, ids, status="waiting", waits=waits("invoice:1"))
            )
            await store.create(self.run(clock, ids, status="waiting", waits=waits("invoice:2")))
            await store.create(
                self.run(clock, ids, status="waiting", waits=waits("invoice:1", answered=True))
            )
            await store.create(
                self.run(clock, ids, "payments", status="waiting", waits=waits("invoice:1"))
            )

            found = await store.waiting_on("invoices", "invoice:1")
            assert [r.id for r in found] == [waiting.id]
            assert await store.waiting_on("invoices", "invoice:9") == []

    async def test_state_is_a_compare_and_set_value(self, place: Place) -> None:
        async with self.opened(place) as store:
            assert await store.get_state("tick") is None
            assert await store.compare_and_set_state("tick", None, "1")
            assert not await store.compare_and_set_state("tick", None, "2")
            assert not await store.compare_and_set_state("tick", "0", "2")
            assert await store.compare_and_set_state("tick", "1", "2")
            assert await store.get_state("tick") == "2"
            assert await store.get_state("other") is None
            assert await store.compare_and_set_state("tick", "2", "2")  # no change is a success

    async def test_exactly_one_of_several_workers_sets_a_state(self, place: Place) -> None:
        async with self.opened(place) as store:
            results = await asyncio.gather(
                *[store.compare_and_set_state("tick", None, f"w{n}") for n in range(6)]
            )

            assert results.count(True) == 1

    async def test_delete_removes_runs_for_good_and_counts_the_ones_it_found(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            first, _ = await store.create(self.run(clock, ids))
            second, _ = await store.create(self.run(clock, ids))
            kept, _ = await store.create(self.run(clock, ids))

            assert await store.delete([first.id, second.id, "proc_missing", first.id]) == 2
            assert await store.get(first.id) is None and await store.get(second.id) is None
            assert await store.get(kept.id) == kept
            assert await store.delete([]) == 0

    async def test_close_may_be_called_twice(self, place: Place) -> None:
        async with self.opened(place) as store:
            await store.close()
            await store.close()

    async def test_runs_and_state_survive_a_restart(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        if not self.capabilities.survives_restart:
            pytest.skip("a single-process store: a restart starts empty")
        async with self.opened(place) as first:
            run, _ = await first.create(self.run(clock, ids, dedup_key="k"))
            await first.compare_and_set_state("tick", None, "1")
        async with self.opened(place) as second:
            assert await second.get(run.id) == run
            assert await second.get_state("tick") == "1"
            again, created = await second.create(self.run(clock, ids, dedup_key="k"))
            assert not created and again.id == run.id

    async def test_a_single_process_store_starts_empty_after_a_restart(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        if self.capabilities.survives_restart:
            pytest.skip("a durable store keeps its runs")
        async with self.opened(place) as first:
            run, _ = await first.create(self.run(clock, ids))
        async with self.opened(place) as second:
            assert await second.get(run.id) is None


__all__ = ["RunStoreCapabilities", "RunStoreContract"]
