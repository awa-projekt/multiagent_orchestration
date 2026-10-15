"""The stores of the data endpoints run the shared suites: `BuildStore`, `FileStore` and `UploadStore`."""

from __future__ import annotations

from awa.data import Limits
from awa.server._data_filesystem import FileBuildStore, FileUploadStore
from awa.server.ports import BuildStore, UploadStore
from awa.testing.contracts import BuildStoreContract, FileStoreContract, Place, UploadStoreContract
from awa.testing.doubles import FakeClock, FixedIds
from awa.ui._data_folder import LocalFileStore
from awa.ui.ports import FileStore


class TestFileBuildStore(BuildStoreContract):
    adapter = FileBuildStore

    def opened(self, place: Place, clock: FakeClock) -> BuildStore:
        return FileBuildStore(place.path, ttl=self.ttl, clock=clock)


class TestLocalFileStore(FileStoreContract):
    adapter = LocalFileStore

    def opened(self, place: Place, limits: Limits) -> FileStore:
        return LocalFileStore(place.path / "files", place.path / "scratch", limits)


class TestFileUploadStore(UploadStoreContract):
    adapter = FileUploadStore

    def opened(self, place: Place, clock: FakeClock, ids: FixedIds) -> UploadStore:
        return FileUploadStore(place.path, ttl=self.ttl, clock=clock, ids=ids.new_id)
