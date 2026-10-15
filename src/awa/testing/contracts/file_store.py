"""The contract of a `FileStore`: private copies of the files a person uploads in the web app.

```python
class TestMyFiles(FileStoreContract):
    def opened(self, place, limits):
        return MyFileStore(place.path, limits)
```
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import AsyncIterator

import pytest

from awa.data import DataError, Limits, TooLargeError, TooManyFilesError, UnsafeArchiveError
from awa.testing.contracts._base import AdapterContract, Place
from awa.ui.ports import FileStore

CSV = b"id;amount\n1;2,50\n2;3,00\n"
ROOMY = Limits(max_files=20, max_file_bytes=1_000_000, max_total_bytes=5_000_000)


async def body(content: bytes, size: int = 7) -> AsyncIterator[bytes]:
    """`content` as the chunks of an upload body."""

    for start in range(0, len(content), size):
        yield content[start : start + size]


def zipped(entries: dict[str, bytes]) -> bytes:
    """A zip archive with the given entries."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class FileStoreContract(AdapterContract):
    """Run this against a `FileStore`: implement `opened`."""

    def opened(self, place: Place, limits: Limits) -> FileStore:
        """A store on `place` that holds at most what `limits` allows."""

        raise NotImplementedError

    @pytest.fixture
    def store(self, place: Place) -> FileStore:
        return self.opened(place, ROOMY)

    async def test_a_received_file_is_listed_with_its_kind_size_and_hash(
        self, store: FileStore
    ) -> None:
        stored = await store.receive("orders.csv", body(CSV))

        assert [(f.name, f.kind, f.size) for f in stored] == [("orders.csv", "csv", len(CSV))]
        assert stored[0].sha256 == hashlib.sha256(CSV).hexdigest()
        assert store.listing() == stored
        assert store.used() == len(CSV)

    async def test_folders_in_the_name_are_kept_and_the_listing_is_sorted(
        self, store: FileStore
    ) -> None:
        await store.receive("b/orders.csv", body(CSV))
        await store.receive("a.csv", body(CSV))

        assert [f.name for f in store.listing()] == ["a.csv", "b/orders.csv"]

    async def test_a_taken_name_gets_a_number_and_nothing_is_overwritten(
        self, store: FileStore
    ) -> None:
        await store.receive("a.csv", body(CSV))
        second = await store.receive("a.csv", body(CSV + b"3;4,00\n"))

        assert [f.name for f in second] == ["a-2.csv"]
        assert [f.name for f in store.listing()] == ["a-2.csv", "a.csv"]

    async def test_the_content_decides_the_kind_and_a_lie_is_refused(
        self, store: FileStore
    ) -> None:
        with pytest.raises(DataError):
            await store.receive("scan.pdf", body(b"this is not a pdf"))

        assert store.listing() == []

    async def test_a_zip_is_unpacked_and_an_unsafe_one_is_refused(self, store: FileStore) -> None:
        stored = await store.receive("data.zip", body(zipped({"in/orders.csv": CSV})))
        assert [f.kind for f in stored] == ["csv"]

        with pytest.raises(UnsafeArchiveError):
            await store.receive("evil.zip", body(zipped({"../escape.csv": CSV})))
        assert [f.name for f in store.listing()] == [f.name for f in stored]

    @pytest.mark.parametrize("bad", ["../a.csv", "/etc/a.csv", "C:/a.csv", "a/../../b.csv", ""])
    async def test_an_unusable_name_is_refused(self, store: FileStore, bad: str) -> None:
        with pytest.raises(DataError):
            await store.receive(bad, body(CSV))

        assert store.listing() == []

    async def test_an_empty_file_is_refused(self, store: FileStore) -> None:
        with pytest.raises(DataError):
            await store.receive("a.csv", body(b""))

        assert store.listing() == []

    async def test_a_file_over_the_limit_is_refused_and_nothing_stays(self, place: Place) -> None:
        store = self.opened(place, Limits(max_files=5, max_file_bytes=10, max_total_bytes=1000))

        with pytest.raises(TooLargeError):
            await store.receive("a.csv", body(CSV))

        assert store.listing() == [] and store.used() == 0

    async def test_the_total_limit_is_kept(self, place: Place) -> None:
        store = self.opened(
            place, Limits(max_files=5, max_file_bytes=1000, max_total_bytes=len(CSV) + 5)
        )
        await store.receive("a.csv", body(CSV))

        with pytest.raises(TooLargeError):
            await store.receive("b.csv", body(CSV))

        assert [f.name for f in store.listing()] == ["a.csv"]

    async def test_the_file_count_limit_is_kept(self, place: Place) -> None:
        store = self.opened(place, Limits(max_files=1, max_file_bytes=1000, max_total_bytes=10000))
        await store.receive("a.csv", body(CSV))

        with pytest.raises(TooManyFilesError):
            await store.receive("b.csv", body(CSV))

        assert [f.name for f in store.listing()] == ["a.csv"]

    async def test_remove_deletes_the_file_and_an_unknown_name_is_an_error(
        self, store: FileStore
    ) -> None:
        await store.receive("sub/a.csv", body(CSV))
        await store.receive("b.csv", body(CSV))

        store.remove("sub/a.csv")

        assert [f.name for f in store.listing()] == ["b.csv"]
        assert store.used() == len(CSV)
        for unknown in ("sub/a.csv", "../b.csv", "nope.csv"):
            with pytest.raises(DataError):
                store.remove(unknown)

    def test_an_empty_store_lists_nothing_and_uses_no_bytes(self, store: FileStore) -> None:
        assert store.listing() == [] and store.used() == 0


__all__ = ["FileStoreContract"]
