"""The contract of a `CacheStore`: what every backend of awa's cache store keeps.

```python
class TestMyCache(CacheStoreContract):
    def opened(self, place, clock):
        return MyCacheStore(place.path / "cache", clock=clock)
```
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from awa.runtime.ports import CacheKindStats, CacheScope, CacheStore
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.doubles import FakeClock

ACME = CacheScope("acme", "invoices", "tools")
GLOBEX = CacheScope("globex", "invoices", "tools")


class CacheStoreContract(AdapterContract):
    """Run this against a `CacheStore`: implement `opened`."""

    def opened(self, place: Place, clock: FakeClock) -> CacheStore:
        """The store on `place`, reading time from `clock`."""

        raise NotImplementedError

    @pytest.fixture
    def store(self, place: Place, clock: FakeClock) -> Iterator[CacheStore]:
        store = self.opened(place, clock)
        yield store
        store.close()

    def test_a_stored_value_comes_back_and_put_replaces_it(self, store: CacheStore) -> None:
        assert store.get(ACME, "k") is None
        store.put(ACME, "k", b"first")
        assert store.get(ACME, "k") == b"first"
        store.put(ACME, "k", b"second")

        assert store.get(ACME, "k") == b"second"

    def test_an_entry_expires_after_its_ttl(self, store: CacheStore, clock: FakeClock) -> None:
        store.put(ACME, "short", b"v", ttl=60)
        store.put(ACME, "forever", b"v")
        clock.advance(seconds=59)
        assert store.get(ACME, "short") == b"v"
        clock.advance(seconds=2)

        assert store.get(ACME, "short") is None
        assert store.get(ACME, "forever") == b"v"
        assert [key for key, _ in store.entries(ACME)] == ["forever"]

    @pytest.mark.parametrize(
        "other",
        [GLOBEX, CacheScope("acme", "orders", "tools"), CacheScope("acme", "invoices", "replay")],
    )
    def test_scopes_never_meet(self, store: CacheStore, other: CacheScope) -> None:
        store.put(ACME, "k", b"acme")

        assert store.get(other, "k") is None
        assert store.entries(other) == []
        store.put(other, "k", b"other")
        assert store.get(ACME, "k") == b"acme"

    def test_entries_lists_the_newest_first_up_to_the_limit(
        self, store: CacheStore, clock: FakeClock
    ) -> None:
        for name in ("a", "b", "c"):
            store.put(ACME, name, name.encode())
            clock.advance(seconds=1)

        assert store.entries(ACME) == [("c", b"c"), ("b", b"b"), ("a", b"a")]
        assert store.entries(ACME, limit=2) == [("c", b"c"), ("b", b"b")]

    def test_clear_deletes_exactly_what_it_matches(self, store: CacheStore) -> None:
        store.put(ACME, "k", b"1")
        store.put(CacheScope("acme", "invoices", "replay"), "k", b"2")
        store.put(GLOBEX, "k", b"3")

        assert store.clear(tenant="acme", kind="replay") == 1
        assert store.get(ACME, "k") == b"1"
        assert store.clear(tenant="acme") == 1
        assert store.get(GLOBEX, "k") == b"3"
        assert store.clear() == 1
        assert store.stats() == []

    def test_stats_count_entries_bytes_and_hits(self, store: CacheStore) -> None:
        store.put(ACME, "a", b"12345")
        store.put(ACME, "b", b"123")
        store.put(GLOBEX, "a", b"1")
        store.get(ACME, "a")
        store.get(ACME, "a")
        store.get(ACME, "missing")

        assert store.stats() == [
            CacheKindStats("acme", "invoices", "tools", entries=2, bytes=8, hits=2),
            CacheKindStats("globex", "invoices", "tools", entries=1, bytes=1, hits=0),
        ]

    def test_values_are_bytes_kept_exactly(self, store: CacheStore) -> None:
        value = bytes(range(256)) * 4
        store.put(ACME, "binary", value)

        assert store.get(ACME, "binary") == value
