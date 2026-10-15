"""The contract of a thread index (`BaseThreadIndex` with `DecisionClaims`): who owns a thread.

```python
class TestMyIndex(ThreadIndexContract):
    capabilities = ThreadIndexCapabilities(survives_restart=True)

    def opened(self, place):
        return my_index_context(place.path)   # an async context manager
```
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from awa.persistence.threads import BaseThreadIndex, DecisionClaims, OwnedThreads, ThreadOwner
from awa.testing.contracts._base import AdapterContract, Place

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ThreadIndexCapabilities:
    """What an index promises beyond the base contract (`survives_restart`: a reopened index knows owners and claims)."""

    survives_restart: bool = False


class ThreadIndexContract(AdapterContract):
    """Run this against a thread index: implement `opened` and set `capabilities`."""

    capabilities = ThreadIndexCapabilities()

    def opened(self, place: Place) -> AbstractAsyncContextManager[BaseThreadIndex]:
        """An async context manager that yields an index on `place` and closes it on exit."""

        raise NotImplementedError

    async def test_the_first_claim_wins(self, place: Place) -> None:
        async with self.opened(place) as index:
            ana = ThreadOwner("acme", "ana", T0)
            first = await index.claim("desk", "t1", ana)
            second = await index.claim("desk", "t1", ThreadOwner("globex", "gus", T0))

            assert first == ana
            assert second == ana
            assert await index.owner("desk", "t1") == ana
            assert await index.owner("desk", "t2") is None
            assert await index.owner("other", "t1") is None

    async def test_racing_claims_all_see_one_winner(self, place: Place) -> None:
        async with self.opened(place) as index:
            owners = [ThreadOwner("acme", f"p{n}", T0) for n in range(8)]

            seen = await asyncio.gather(*(index.claim("desk", "t", o) for o in owners))

            assert len(set(seen)) == 1
            assert seen[0] in owners

    async def test_listings_keep_to_the_tenant_and_principal_newest_first(
        self, place: Place
    ) -> None:
        async with self.opened(place) as index:
            for minute, (thread, tenant, principal) in enumerate(
                [("a", "acme", "ana"), ("b", "acme", "bob"), ("c", "globex", "gus"),
                 ("d", "acme", "ana"), ("e", None, None)]
            ):  # fmt: skip
                await index.claim(
                    "desk", thread, ThreadOwner(tenant, principal, T0 + timedelta(minutes=minute))
                )

            acme = await index.threads("desk", tenant="acme")
            ana = await index.threads("desk", tenant="acme", principal="ana")
            nobody = await index.threads("desk", tenant=None)

            assert [r.thread_id for r in acme] == ["d", "b", "a"]
            assert [r.thread_id for r in ana] == ["d", "a"]
            assert [r.thread_id for r in nobody] == ["e"]
            assert [r.thread_id for r in await index.threads("desk", tenant="acme", limit=1)] == [
                "d"
            ]
            assert acme[0].owner == ThreadOwner("acme", "ana", T0 + timedelta(minutes=3))

    async def test_owned_by_lists_only_the_threads_of_that_principal(self, place: Place) -> None:
        async with self.opened(place) as index:
            assert isinstance(index, OwnedThreads)
            await index.claim("desk", "b", ThreadOwner("acme", "ana", T0))
            await index.claim("desk", "a", ThreadOwner("acme", "ana", T0))
            await index.claim("other", "c", ThreadOwner("globex", "ana", T0))
            await index.claim("desk", "d", ThreadOwner("acme", "bob", T0))
            await index.claim("desk", "e", ThreadOwner(None, None, T0))

            assert sorted(await index.owned_by("ana")) == [
                ("desk", "a"),
                ("desk", "b"),
                ("other", "c"),
            ]
            assert await index.owned_by("bob") == [("desk", "d")]
            assert await index.owned_by("nobody") == []
            await index.forget("desk", "a")
            assert sorted(await index.owned_by("ana")) == [("desk", "b"), ("other", "c")]

    async def test_a_decision_is_claimed_once_until_released(self, place: Place) -> None:
        async with self.opened(place) as index:
            assert isinstance(index, DecisionClaims)
            results = await asyncio.gather(
                *(index.claim_decision("desk", "t", "v1") for _ in range(4))
            )
            assert sorted(results) == [False, False, False, True]
            assert await index.claim_decision("desk", "t", "v2")
            await index.release_decision("desk", "t", "v1")
            assert await index.claim_decision("desk", "t", "v1")

    async def test_forget_removes_the_owner_and_the_answered_decisions(self, place: Place) -> None:
        async with self.opened(place) as index:
            assert isinstance(index, DecisionClaims)
            await index.claim("desk", "t", ThreadOwner("acme", "ana", T0))
            await index.claim("desk", "u", ThreadOwner("acme", "ana", T0))
            await index.claim_decision("desk", "t", "v1")

            await index.forget("desk", "t")

            assert await index.owner("desk", "t") is None
            assert await index.owner("desk", "u") is not None
            assert await index.claim_decision("desk", "t", "v1")

    async def test_owners_and_claims_survive_a_restart(self, place: Place) -> None:
        if not self.capabilities.survives_restart:
            pytest.skip("this index lives in one process")
        async with self.opened(place) as first:
            assert isinstance(first, DecisionClaims)
            await first.claim("desk", "t", ThreadOwner("acme", "ana", T0))
            await first.claim_decision("desk", "t", "v1")
        async with self.opened(place) as second:
            assert isinstance(second, DecisionClaims)
            assert await second.owner("desk", "t") == ThreadOwner("acme", "ana", T0)
            assert not await second.claim_decision("desk", "t", "v1")


__all__ = ["ThreadIndexCapabilities", "ThreadIndexContract"]
