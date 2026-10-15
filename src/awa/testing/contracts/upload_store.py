"""The contract of an `UploadStore`: what every backend of the data endpoints' upload store keeps.

```python
class TestMyUploads(UploadStoreContract):
    def opened(self, place, clock, ids):
        return MyUploadStore(place.path, ttl=self.ttl, clock=clock, ids=ids.new_id)
```
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, ClassVar

import pytest

from awa.data import DataError, Limits, TooLargeError, TypeMismatchError, UnsafeArchiveError
from awa.server.errors import QuotaExceededError, UploadNotFoundError
from awa.server.ports import UploadSession, UploadStore
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.doubles import FakeClock, FixedIds

CSV = b"id;amount\n1;2,50\n2;3,00\n"
LIMITS = Limits(max_files=20, max_file_bytes=1_000_000, max_total_bytes=5_000_000)
KINDS = {"csv", "text", "json", "pdf"}


def receive(
    store: UploadStore,
    files: Mapping[str, bytes],
    *,
    tenant: str | None = "acme",
    principal: str = "alice",
    quota: int = 10**9,
    limits: Limits = LIMITS,
    kinds: set[str] | None = None,
) -> Any:
    """Receive `files` into a new upload the way an endpoint does and commit it."""

    with store.begin(tenant, principal, quota=quota) as session:
        write(session, files)
        return session.commit(limits, kinds=kinds if kinds is not None else KINDS)  # type: ignore[arg-type]


def write(session: UploadSession, files: Mapping[str, bytes]) -> None:
    """Write `files` into the open session."""

    for name, content in files.items():
        handle = session.open(name)
        session.count(len(content))
        handle.write(content)
        handle.close()


class UploadStoreContract(AdapterContract):
    """Run this against an `UploadStore`: implement `opened`, building the store with `ttl` seconds."""

    ttl: ClassVar[float] = 3600.0

    def opened(self, place: Place, clock: FakeClock, ids: FixedIds) -> UploadStore:
        """The store on `place`, reading time from `clock` and making ids with `ids.new_id`."""

        raise NotImplementedError

    @pytest.fixture
    def store(self, place: Place, clock: FakeClock, ids: FixedIds) -> UploadStore:
        return self.opened(place, clock, ids)

    def test_commit_keeps_the_files_with_ids_in_name_order(self, store: UploadStore) -> None:
        record = receive(store, {"b.csv": CSV, "A.txt": b"hello"})

        assert [(f.id, f.name, f.kind, f.size) for f in record.files] == [
            ("f1", "A.txt", "text", 5),
            ("f2", "b.csv", "csv", len(CSV)),
        ]
        assert (
            record.id == FixedIds().new_id("up")
            and record.tenant == "acme"
            and record.principal == "alice"
        )
        assert record.expires_at - record.created_at == timedelta(seconds=self.ttl)
        assert record.bytes == 5 + len(CSV)
        assert store.get("acme", record.id) == record

    def test_open_files_shows_the_files_under_their_names(self, store: UploadStore) -> None:
        record = receive(store, {"orders.csv": CSV})

        with store.open_files(record) as folder:
            assert (folder / "orders.csv").read_bytes() == CSV

    def test_the_content_decides_the_kind_and_a_lie_is_refused(self, store: UploadStore) -> None:
        with pytest.raises(TypeMismatchError):
            receive(store, {"scan.pdf": b"this is not a pdf"})

        assert store.used_bytes("acme") == 0
        with pytest.raises(UploadNotFoundError):
            store.get("acme", FixedIds().new_id("up"))

    def test_a_kind_outside_the_allow_list_is_refused(self, store: UploadStore) -> None:
        with pytest.raises(DataError):
            receive(store, {"a.csv": CSV}, kinds={"pdf"})

    def test_a_file_over_the_limit_is_refused_and_nothing_is_kept(self, store: UploadStore) -> None:
        small = Limits(max_files=5, max_file_bytes=10, max_total_bytes=1000)

        with pytest.raises(TooLargeError):
            receive(store, {"a.csv": CSV}, limits=small)

        assert store.used_bytes("acme") == 0

    def test_a_zip_is_unpacked_and_an_unsafe_one_is_refused(self, store: UploadStore) -> None:
        good, evil = io.BytesIO(), io.BytesIO()
        with zipfile.ZipFile(good, "w") as archive:
            archive.writestr("inner/orders.csv", CSV)
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("../escape.csv", CSV)

        record = receive(store, {"data.zip": good.getvalue()})
        assert [f.name for f in record.files] == ["data/inner/orders.csv"]
        with pytest.raises(UnsafeArchiveError):
            receive(store, {"evil.zip": evil.getvalue()})

    def test_tenants_never_see_each_others_uploads(self, store: UploadStore) -> None:
        mine = receive(store, {"a.csv": CSV}, tenant="acme")
        theirs = receive(store, {"a.csv": CSV}, tenant="globex")
        anonymous = receive(store, {"a.csv": CSV}, tenant=None)

        for owner, stranger in (("acme", "globex"), ("globex", None), (None, "acme")):
            record = {"acme": mine, "globex": theirs, None: anonymous}[owner]
            with pytest.raises(UploadNotFoundError):
                store.get(stranger, record.id)
        assert store.used_bytes("acme") == mine.bytes
        assert store.used_bytes("globex") == theirs.bytes

    @pytest.mark.parametrize("bad", ["", "..", "../x", "a/b", "a\\b", "up_999999", "x" * 200])
    def test_unknown_and_malformed_ids_are_simply_not_found(
        self, store: UploadStore, bad: str
    ) -> None:
        with pytest.raises(UploadNotFoundError):
            store.get("acme", bad)

    def test_an_upload_expires_with_the_clock(self, store: UploadStore, clock: FakeClock) -> None:
        record = receive(store, {"a.csv": CSV})

        clock.advance(seconds=self.ttl - 1)
        assert store.get("acme", record.id).id == record.id
        clock.advance(seconds=2)
        with pytest.raises(UploadNotFoundError):
            store.get("acme", record.id)
        assert store.get("acme", record.id, expired_ok=True).id == record.id

    def test_purge_removes_the_expired_but_not_the_kept_ones(
        self, store: UploadStore, clock: FakeClock
    ) -> None:
        old = receive(store, {"a.csv": CSV})
        needed = receive(store, {"b.csv": CSV})
        clock.advance(seconds=self.ttl / 2)
        fresh = receive(store, {"c.csv": CSV})
        clock.advance(seconds=self.ttl / 2 + 1)

        removed = store.purge(keep={needed.id})

        assert removed == 1
        with pytest.raises(UploadNotFoundError):
            store.get("acme", old.id, expired_ok=True)
        assert store.get("acme", needed.id, expired_ok=True).id == needed.id
        assert store.get("acme", fresh.id).id == fresh.id

    def test_delete_removes_an_upload_and_deleting_twice_is_fine(self, store: UploadStore) -> None:
        record = receive(store, {"a.csv": CSV})

        store.delete(record)
        store.delete(record)

        with pytest.raises(UploadNotFoundError):
            store.get("acme", record.id)
        with pytest.raises(UploadNotFoundError), store.open_files(record):
            pass
        assert store.used_bytes("acme") == 0

    def test_the_quota_stops_a_receive_at_the_chunk_that_crosses_it(
        self, store: UploadStore
    ) -> None:
        with store.begin("acme", "alice", quota=10) as session:
            session.count(6)
            with pytest.raises(QuotaExceededError):
                session.count(5)

        assert store.used_bytes("acme") == 0  # what was staged is given back

    def test_a_tenant_without_room_cannot_begin(self, store: UploadStore) -> None:
        receive(store, {"a.csv": CSV})

        with pytest.raises(QuotaExceededError), store.begin("acme", "alice", quota=len(CSV)):
            pass
        with store.begin("globex", "gus", quota=len(CSV)):  # other tenants are not affected
            pass

    def test_uploads_in_flight_count_against_the_quota_and_the_slots(
        self, store: UploadStore
    ) -> None:
        sessions = [store.begin("acme", "alice", quota=1000) for _ in range(5)]
        entered = []
        try:
            for index, manager in enumerate(sessions):
                if index < 4:
                    entered.append(manager.__enter__())
                else:
                    with pytest.raises(QuotaExceededError):
                        manager.__enter__()
            entered[0].count(600)
            assert store.used_bytes("acme") == 600
            with pytest.raises(QuotaExceededError):
                entered[1].count(600)
        finally:
            for manager in sessions[: len(entered)]:
                manager.__exit__(None, None, None)

        assert store.used_bytes("acme") == 0
        with store.begin("acme", "alice", quota=1000):  # the slots are free again
            pass

    def test_names_must_be_safe_and_unique_within_a_receive(self, store: UploadStore) -> None:
        with store.begin("acme", "alice", quota=1000) as session:
            session.open("a.csv").close()
            for bad in ("../a.csv", "sub/a.csv", "CON.txt", ".hidden"):
                with pytest.raises(DataError):
                    session.open(bad)
            with pytest.raises(DataError):
                session.open("A.CSV")  # the same name in another case

    def test_leaving_without_commit_keeps_nothing(self, store: UploadStore) -> None:
        with store.begin("acme", "alice", quota=1000) as session:
            write(session, {"a.csv": CSV})

        assert store.used_bytes("acme") == 0
        with pytest.raises(UploadNotFoundError):
            store.get("acme", FixedIds().new_id("up"))

    def test_a_commit_without_files_is_an_error(self, store: UploadStore) -> None:
        with store.begin("acme", "alice", quota=1000) as session, pytest.raises(DataError):
            session.commit(LIMITS)


__all__ = ["UploadStoreContract"]
