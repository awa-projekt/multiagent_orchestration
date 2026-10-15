"""The registry: which backends implement which backend protocol, and which suite holds them to it.

`test_registry.py` fails when a backend in awa implements one of these backend protocols and is not listed, and
when a listed backend has no suite class (or a suite class runs a backend that is not listed).
"""

from __future__ import annotations

from typing import Any

from awa._internal.upstream.checkpoint import BaseCheckpointSaver, postgres_saver_class
from awa.automation import (
    InMemoryEventQueue,
    InMemoryRunStore,
    PostgresEventQueue,
    PostgresRunStore,
    SqliteRunStore,
)
from awa.automation.notify import Notifier, WebhookNotifier
from awa.automation.queues import EventQueue
from awa.automation.store import RunStore
from awa.observability.audit import AuditLogSink, InMemoryAuditSink, JsonlAuditSink
from awa.people import EntraPeople, LocalPeople, PeopleDirectory
from awa.persistence import ArtifactRepository
from awa.persistence.artifacts import (
    FileArtifactRepository,
    InMemoryArtifactRepository,
    PostgresArtifactRepository,
)
from awa.persistence.cache import InMemoryCacheStore, SqliteCacheStore
from awa.persistence.checkpointer import StateInMemorySaver
from awa.persistence.runlog import PostgresRunLog, RunLog, SqliteRunLog
from awa.persistence.sqlite import SqliteCheckpointer
from awa.persistence.threads import BaseThreadIndex, PostgresThreadIndex, SqliteThreadIndex
from awa.runtime.ports import CacheStore
from awa.server._data_filesystem import FileBuildStore, FileUploadStore
from awa.server._stores import InMemoryThreadIndex
from awa.server.ports import BuildStore, UploadStore
from awa.testing.contracts import (
    ArtifactRepositoryContract,
    AuditSinkContract,
    BuildStoreContract,
    CacheStoreContract,
    CheckpointerContract,
    EventQueueContract,
    NotifierContract,
    RunLogContract,
    RunStoreContract,
    ThreadIndexContract,
    UploadStoreContract,
)
from awa.testing.people_contract import PeopleDirectoryContract


def postgres_savers() -> tuple[type, ...]:
    """LangGraph's PostgreSQL checkpointer, when its package is installed (`postgres` extra)."""

    try:
        return (postgres_saver_class(),)
    except ImportError:
        return ()


BACKENDS: dict[Any, tuple[type, ...]] = {
    RunStore: (InMemoryRunStore, PostgresRunStore, SqliteRunStore),
    EventQueue: (InMemoryEventQueue, PostgresEventQueue),
    ArtifactRepository: (
        InMemoryArtifactRepository,
        FileArtifactRepository,
        PostgresArtifactRepository,
    ),
    AuditLogSink: (InMemoryAuditSink, JsonlAuditSink),
    UploadStore: (FileUploadStore,),
    BuildStore: (FileBuildStore,),
    CacheStore: (InMemoryCacheStore, SqliteCacheStore),
    PeopleDirectory: (EntraPeople, LocalPeople),
    RunLog: (SqliteRunLog, PostgresRunLog),
    BaseThreadIndex: (InMemoryThreadIndex, SqliteThreadIndex, PostgresThreadIndex),
    Notifier: (WebhookNotifier,),
    BaseCheckpointSaver: (StateInMemorySaver, SqliteCheckpointer, *postgres_savers()),
}
"""Backend protocol -> every backend that awa ships for it, in-memory and file backends included.

A key is a class, or a `Callable` alias such as `Notifier` (a class implements it when its
`__call__` takes the alias's argument types).
"""

SUITES: dict[Any, type] = {
    RunStore: RunStoreContract,
    EventQueue: EventQueueContract,
    ArtifactRepository: ArtifactRepositoryContract,
    AuditLogSink: AuditSinkContract,
    UploadStore: UploadStoreContract,
    BuildStore: BuildStoreContract,
    CacheStore: CacheStoreContract,
    PeopleDirectory: PeopleDirectoryContract,
    RunLog: RunLogContract,
    BaseThreadIndex: ThreadIndexContract,
    Notifier: NotifierContract,
    BaseCheckpointSaver: CheckpointerContract,
}
"""Backend protocol -> the contract suite of that backend protocol."""
