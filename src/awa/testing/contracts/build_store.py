"""The contract of a `BuildStore`: what every backend of the data endpoints' build store keeps.

```python
class TestMyBuilds(BuildStoreContract):
    def opened(self, place, clock):
        return MyBuildStore(place.path, ttl=self.ttl, clock=clock)
```
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from awa.server.errors import BuildNotFoundError
from awa.server.ports import BuildRecord, BuildStore
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.doubles import FakeClock


def _record(clock: FakeClock, build_id: str, **changes: Any) -> BuildRecord:
    base: dict[str, Any] = {
        "id": build_id,
        "tenant": "acme",
        "principal": "alice",
        "created_at": clock(),
    }
    return BuildRecord(**{**base, **changes})


class BuildStoreContract(AdapterContract):
    """Run this against a `BuildStore`: implement `opened`, building the store with `ttl` seconds."""

    ttl: ClassVar[float] = 3600.0

    def opened(self, place: Place, clock: FakeClock) -> BuildStore:
        """The store on `place`, reading time from `clock`."""

        raise NotImplementedError

    @pytest.fixture
    def store(self, place: Place, clock: FakeClock) -> BuildStore:
        return self.opened(place, clock)

    def test_a_saved_record_comes_back_and_save_replaces(
        self, store: BuildStore, clock: FakeClock
    ) -> None:
        first = _record(clock, "bld_1", request={"description": "x"}, answers={"q": "a"})

        store.save(first)
        assert store.get("acme", "bld_1") == first
        second = first.changed(status="completed", pass_rate=0.9, report={"passed": True})
        store.save(second)

        assert store.get("acme", "bld_1") == second

    def test_tenants_never_see_each_others_builds(
        self, store: BuildStore, clock: FakeClock
    ) -> None:
        store.save(_record(clock, "bld_1"))

        for stranger in ("globex", None):
            with pytest.raises(BuildNotFoundError):
                store.get(stranger, "bld_1")
            assert store.list_builds(stranger) == []

    @pytest.mark.parametrize("bad", ["", "..", "../x", "a/b", "nope"])
    def test_unknown_and_malformed_ids_are_not_found(self, store: BuildStore, bad: str) -> None:
        with pytest.raises(BuildNotFoundError):
            store.get("acme", bad)

    def test_list_is_newest_first_and_can_be_limited_to_an_owner(
        self, store: BuildStore, clock: FakeClock
    ) -> None:
        store.save(_record(clock, "bld_1"))
        clock.advance(seconds=10)
        store.save(_record(clock, "bld_2", principal="bob"))
        clock.advance(seconds=10)
        store.save(_record(clock, "bld_3"))

        assert [r.id for r in store.list_builds("acme")] == ["bld_3", "bld_2", "bld_1"]
        assert [r.id for r in store.list_builds("acme", owner="alice")] == ["bld_3", "bld_1"]
        assert [r.id for r in store.list_builds("acme", limit=2)] == ["bld_3", "bld_2"]

    def test_the_project_folder_exists_only_for_a_saved_build(
        self, store: BuildStore, clock: FakeClock
    ) -> None:
        with pytest.raises(BuildNotFoundError):
            store.project_dir("acme", "bld_1")
        store.save(_record(clock, "bld_1"))

        folder = store.project_dir("acme", "bld_1")
        (folder / "blueprint.yaml").write_text("name: x\n", encoding="utf-8")

        assert store.project_dir("acme", "bld_1") == folder
        assert (folder / "blueprint.yaml").read_text(encoding="utf-8") == "name: x\n"
        with pytest.raises(BuildNotFoundError):
            store.project_dir("globex", "bld_1")

    def test_delete_removes_the_record_and_the_project(
        self, store: BuildStore, clock: FakeClock
    ) -> None:
        store.save(_record(clock, "bld_1"))
        folder = store.project_dir("acme", "bld_1")
        (folder / "a.txt").write_text("x", encoding="utf-8")

        store.delete("acme", "bld_1")
        store.delete("acme", "bld_1")

        assert not folder.exists()
        with pytest.raises(BuildNotFoundError):
            store.get("acme", "bld_1")

    def test_purge_removes_old_finished_builds_only(
        self, store: BuildStore, clock: FakeClock
    ) -> None:
        done = clock()
        store.save(_record(clock, "bld_old", status="completed", finished_at=done))
        store.save(_record(clock, "bld_kept", status="failed", finished_at=done))
        store.save(_record(clock, "bld_running", status="designing"))
        store.save(_record(clock, "bld_waiting", status="needs_input"))
        clock.advance(seconds=self.ttl / 2)
        store.save(_record(clock, "bld_new", status="completed", finished_at=clock()))
        clock.advance(seconds=self.ttl / 2 + 1)

        removed = store.purge(keep={"bld_kept"})

        assert removed == 1
        left = {r.id for r in store.list_builds("acme")}
        assert left == {"bld_kept", "bld_running", "bld_waiting", "bld_new"}

    def test_used_bytes_and_unfinished(self, store: BuildStore, clock: FakeClock) -> None:
        store.save(_record(clock, "bld_1", status="completed", bytes=100))
        store.save(_record(clock, "bld_2", status="designing", bytes=20))
        store.save(_record(clock, "bld_3", status="needs_input", tenant="globex"))

        assert store.used_bytes("acme") == 120
        assert store.used_bytes("globex") == 0
        assert {r.id for r in store.unfinished()} == {"bld_2", "bld_3"}


__all__ = ["BuildStoreContract"]
