"""SQLite persistence: the checkpointer, the thread index, and `open_store`."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.store.memory import InMemoryStore

from _helpers.polling import SUBPROCESS_TIMEOUT
from awa.core.settings import Settings
from awa.errors import (
    ConfigurationError,
    DataStoreError,
    MissingConfigurationError,
    OptionalDependencyError,
)
from awa.persistence import SqliteCheckpointer
from awa.persistence.sql import SqliteDatabase
from awa.persistence.store import open_store
from awa.persistence.threads import SqliteThreadIndex, ThreadOwner


def config(thread: str, checkpoint_id: str | None = None) -> Any:
    configurable: dict[str, str] = {"thread_id": thread, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def step(
    saver: SqliteCheckpointer,
    thread: str,
    number: int,
    *,
    ts: datetime | None = None,
    source: str = "loop",
    writer: str = "",
) -> str:
    """Save checkpoint `number` of `thread` on top of the thread's latest one."""

    checkpoint = empty_checkpoint()
    checkpoint["id"] = f"{number:06}{writer}"
    if ts is not None:
        checkpoint["ts"] = ts.isoformat()
    checkpoint["channel_values"] = {"a": number}
    checkpoint["channel_versions"] = {"a": f"{number:06}"}
    latest = saver.get_tuple(config(thread))
    parent = latest.config["configurable"]["checkpoint_id"] if latest else None
    saved = saver.put(
        config(thread, parent),
        checkpoint,
        {"source": source, "step": number},
        {"a": f"{number:06}"},
    )
    saver.put_writes(saved, [("a", number)], task_id=f"task-{number}")
    return checkpoint["id"]


def counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("checkpoints", "blobs", "writes")
        }


def test_concurrent_writers_in_separate_connections_lose_nothing(tmp_path: Path) -> None:
    path = tmp_path / "c.db"
    savers = [SqliteCheckpointer(path, keep_checkpoints=None) for _ in range(4)]
    barrier = threading.Barrier(len(savers))
    errors: list[BaseException] = []

    def write(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            for number in range(1, 16):
                step(savers[index], f"own-{index}", number)
                step(savers[index], "shared", number, writer=f"-{index}")
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(len(savers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == [] and not any(thread.is_alive() for thread in threads)

    reader = SqliteCheckpointer(path, keep_checkpoints=None)
    for index in range(len(savers)):
        assert len(list(reader.list(config(f"own-{index}")))) == 15
    assert len(list(reader.list(config("shared")))) == 60
    for saver in (*savers, reader):
        saver.close()


def test_delete_thread_removes_every_trace_of_it_and_only_it(tmp_path: Path) -> None:
    saver = SqliteCheckpointer(tmp_path / "c.db")
    for number in range(1, 4):
        step(saver, "gone", number)
        step(saver, "kept", number)
    saver.delete_thread("gone")
    assert saver.get_tuple(config("gone")) is None
    assert counts(tmp_path / "c.db") == {"checkpoints": 3, "blobs": 3, "writes": 3}
    kept = saver.get_tuple(config("kept"))
    assert kept is not None and kept.checkpoint["channel_values"] == {"a": 3}
    assert kept.pending_writes == [("task-3", "a", 3)]
    saver.delete_thread("never-existed")  # not an error
    saver.close()


def test_list_filters_pages_and_limits(tmp_path: Path) -> None:
    saver = SqliteCheckpointer(tmp_path / "c.db", keep_checkpoints=None)
    sources = ["input", "loop", "loop", "update", "loop"]
    ids = [
        step(saver, "t", number, source=source) for number, source in enumerate(sources, start=1)
    ]
    step(saver, "other", 1)
    newest_first = list(reversed(ids))

    def listed(**options: Any) -> list[str]:
        return [
            t.config["configurable"]["checkpoint_id"] for t in saver.list(config("t"), **options)
        ]

    assert listed() == newest_first
    assert listed(limit=2) == newest_first[:2]
    assert listed(limit=0) == []
    assert listed(before=config("t", ids[2])) == [ids[1], ids[0]]
    assert listed(before=config("t", ids[2]), limit=1) == [ids[1]]
    assert listed(filter={"source": "loop"}) == [ids[4], ids[2], ids[1]]
    assert listed(filter={"source": "loop"}, limit=2) == [ids[4], ids[2]]  # filter, then limit
    assert listed(filter={"source": "missing"}) == []
    everything = [t.config["configurable"]["thread_id"] for t in saver.list(None)]
    assert sorted(set(everything)) == ["other", "t"] and len(everything) == 6
    assert len(list(saver.list(None, limit=3))) == 3
    saver.close()


def test_a_damaged_file_fails_with_a_clear_message(tmp_path: Path) -> None:
    path = tmp_path / "c.db"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(ConfigurationError, match="Cannot open the checkpoint database"):
        SqliteCheckpointer(path)
    with pytest.raises(ConfigurationError, match="keep_checkpoints"):
        SqliteCheckpointer(tmp_path / "other.db", keep_checkpoints=0)


def opened_connections(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    """Every connection `SqliteCheckpointer` opens from now on."""

    opened: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def recording(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(connect(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(sqlite3, "connect", recording)
    return opened


def assert_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_a_damaged_file_does_not_leave_its_connection_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c.db"
    path.write_bytes(b"this is not a database" * 100)
    opened = opened_connections(monkeypatch)
    with pytest.raises(ConfigurationError, match="Cannot open"):
        SqliteCheckpointer(path)
    [connection] = opened
    assert_closed(connection)


@pytest.mark.parametrize("failure", [OSError("disk gone"), KeyboardInterrupt()])
def test_any_failure_while_opening_closes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    def broken(self: SqliteCheckpointer) -> None:
        raise failure

    monkeypatch.setattr(SqliteCheckpointer, "_upgrade_format", broken)
    opened = opened_connections(monkeypatch)
    with pytest.raises(type(failure)):
        SqliteCheckpointer(tmp_path / "c.db")
    [connection] = opened
    assert_closed(connection)


def test_a_failing_retention_prune_closes_the_connection_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: SqliteCheckpointer, idle_for: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteCheckpointer, "prune_idle", broken)
    opened = opened_connections(monkeypatch)
    with pytest.raises(ConfigurationError, match="database is locked"):
        SqliteCheckpointer(tmp_path / "c.db")
    [connection] = opened
    assert_closed(connection)


def test_each_thread_keeps_a_bounded_number_of_checkpoints(tmp_path: Path) -> None:
    saver = SqliteCheckpointer(tmp_path / "c.db", keep_checkpoints=4)
    for number in range(1, 31):
        step(saver, "busy", number)
        step(saver, "quiet", number if number <= 2 else 2)  # re-saves checkpoint 2
    total = counts(tmp_path / "c.db")
    assert 4 <= len(list(saver.list(config("busy")))) <= 6  # four, plus a batch not yet dropped
    assert len(list(saver.list(config("quiet")))) == 2
    # Dropped checkpoints take their writes and values with them.
    assert total["writes"] == total["checkpoints"] == total["blobs"]
    latest = saver.get_tuple(config("busy"))
    assert latest is not None and latest.checkpoint["id"] == "000030"
    assert latest.checkpoint["channel_values"] == {"a": 30}
    saver.close()


def test_threads_idle_for_the_retention_are_deleted_on_open(tmp_path: Path) -> None:
    path = tmp_path / "c.db"
    saver = SqliteCheckpointer(path, retention=None)
    old = datetime.now(UTC) - timedelta(days=100)
    step(saver, "old", 1, ts=old)
    step(saver, "recent", 1, ts=datetime.now(UTC) - timedelta(days=1))
    step(saver, "revived", 1, ts=old)
    step(saver, "revived", 2)  # active again: its newest checkpoint is recent
    saver.close()

    keeping = SqliteCheckpointer(path, retention=None)
    assert keeping.get_tuple(config("old")) is not None
    keeping.close()
    pruned = SqliteCheckpointer(path, retention=timedelta(days=90))
    assert pruned.get_tuple(config("old")) is None
    assert pruned.get_tuple(config("recent")) is not None
    assert pruned.get_tuple(config("revived")) is not None
    assert pruned.prune_idle(timedelta(days=90)) == 0
    pruned.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_database_is_private_to_its_owner(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        saver = SqliteCheckpointer(tmp_path / "c.db")
        step(saver, "t", 1)
        saver.close()
    finally:
        os.umask(previous)
    for name in ("c.db", "c.db-wal", "c.db-shm"):
        file = tmp_path / name
        if file.exists():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600, name


def modes(folder: Path) -> dict[str, int]:
    return {p.name: stat.S_IMODE(p.stat().st_mode) for p in folder.iterdir() if p.is_file()}


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_every_file_of_the_data_file_cache_and_checkpoints_is_private(tmp_path: Path) -> None:
    from awa.persistence.cache import SqliteCacheStore
    from awa.persistence.sql import SqliteDatabase
    from awa.runtime.ports import CacheScope

    previous = os.umask(0o022)
    try:
        database = SqliteDatabase(tmp_path / "data" / "awa.db")
        cache = SqliteCacheStore(tmp_path / "cache" / "cache.db")
        cache.put(CacheScope("local", "demo", "model"), "k", b"value")
        saver = SqliteCheckpointer(tmp_path / "ckpt" / "c.db")
        step(saver, "t", 1)
        database.backup(tmp_path / "copies" / "data.db")
        saver.backup(tmp_path / "copies" / "ckpt.db")
        found = {
            "data": modes(tmp_path / "data"),
            "cache": modes(tmp_path / "cache"),
            "ckpt": modes(tmp_path / "ckpt"),
            "copies": modes(tmp_path / "copies"),
        }
        saver.close()
        database.close()
    finally:
        os.umask(previous)

    assert {"data.db", "ckpt.db"} <= set(found["copies"])
    for folder, files in found.items():
        assert files, folder
        assert set(files.values()) == {0o600}, (folder, files)
    assert {"awa.db-wal", "awa.db-shm"} <= set(found["data"])


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_the_files_have_an_acl_for_the_current_user_only(tmp_path: Path) -> None:
    import subprocess

    from awa.persistence.sql import SqliteDatabase

    database = SqliteDatabase(tmp_path / "awa.db")
    database.backup(tmp_path / "copy.db")
    database.close()

    for name in ("awa.db", "copy.db"):
        shown = subprocess.run(
            ["icacls", str(tmp_path / name)], capture_output=True, text=True, check=True
        ).stdout
        assert os.environ["USERNAME"].lower() in shown.lower(), shown
        for wide in ("Everyone", r"BUILTIN\Users", "Authenticated Users"):
            assert wide not in shown, shown


def test_a_backup_is_a_consistent_copy_and_an_existing_target_is_refused(tmp_path: Path) -> None:
    saver = SqliteCheckpointer(tmp_path / "c.db")
    for number in range(1, 4):
        step(saver, "t", number)
    target = tmp_path / "out" / "copy.db"

    written = saver.backup(target)
    with pytest.raises(ConfigurationError, match="exists"):
        saver.backup(target)
    step(saver, "t", 4)

    assert written == target
    assert counts(target)["checkpoints"] == 3
    copy = SqliteCheckpointer(target, retention=None)
    latest = copy.get_tuple(config("t"))
    assert latest is not None and latest.checkpoint["id"] == "000003"
    copy.close()
    saver.close()


def test_keep_checkpoints_below_one_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="keep_checkpoints"):
        SqliteCheckpointer(tmp_path / "c.db", keep_checkpoints=0)


def test_trimming_removes_the_values_no_checkpoint_refers_to_any_more(tmp_path: Path) -> None:
    saver = SqliteCheckpointer(tmp_path / "c.db", keep_checkpoints=2)
    for number in range(1, 13):
        step(saver, "t", number)
    saver.close()

    with sqlite3.connect(tmp_path / "c.db") as db:
        versions = {row[0] for row in db.execute("SELECT version FROM blobs")}
        newest = {
            row[0]
            for row in db.execute(
                "SELECT checkpoint_id FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 2"
            )
        }

    assert {"000012", "000011"} <= versions
    assert len(versions) <= 3  # the two kept checkpoints, plus a batch not yet dropped
    assert newest == {"000012", "000011"}


def owner(name: str) -> ThreadOwner:
    return ThreadOwner(tenant="t", principal=name)


async def test_two_indexes_on_one_file_agree_on_one_owner(tmp_path: Path) -> None:
    first = SqliteThreadIndex(tmp_path / "awa.db")
    second = SqliteThreadIndex(tmp_path / "awa.db")
    try:
        claims = [
            (first if n % 2 else second).claim("sys", "thread-1", owner(f"user-{n}"))
            for n in range(8)
        ]
        winners = {won.principal for won in await asyncio.gather(*claims)}
        assert len(winners) == 1
        assert (await first.owner("sys", "thread-1")) == (await second.owner("sys", "thread-1"))
    finally:
        await first.close()
        await second.close()


async def test_two_indexes_on_one_file_give_one_decision_claim(tmp_path: Path) -> None:
    first = SqliteThreadIndex(tmp_path / "awa.db")
    second = SqliteThreadIndex(tmp_path / "awa.db")
    try:
        claims = [
            (first if n % 2 else second).claim_decision("sys", "thread-1", "v1") for n in range(8)
        ]
        assert sorted(await asyncio.gather(*claims)) == [False] * 7 + [True]
    finally:
        await first.close()
        await second.close()


_CLAIM_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    from awa.persistence.threads import SqliteThreadIndex

    async def main() -> None:
        index = SqliteThreadIndex(sys.argv[1])
        try:
            print(await index.claim_decision("sys", "thread-1", "v1"))
        finally:
            await index.close()

    asyncio.run(main())
    """
)


def test_two_processes_give_one_decision_claim(tmp_path: Path) -> None:
    path = tmp_path / "awa.db"
    SqliteDatabase(path).close()
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _CLAIM_SCRIPT, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=SUBPROCESS_TIMEOUT) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], outputs
    assert sorted(out.strip() for out, _ in outputs) == ["False", "True"]


async def test_closing_an_index_on_a_shared_database_keeps_it_usable(tmp_path: Path) -> None:
    database = SqliteDatabase(tmp_path / "awa.db")
    try:
        index = SqliteThreadIndex(database)
        await index.claim("sys", "thread-1", owner("a"))
        await index.close()
        assert await database.read(lambda conn: conn.execute("SELECT 1").fetchone()) == (1,)
    finally:
        database.close()


async def test_an_index_that_opened_its_file_releases_it(tmp_path: Path) -> None:
    path = tmp_path / "awa.db"
    index = SqliteThreadIndex(path)
    await index.claim("sys", "thread-1", owner("a"))
    await index.close()
    for suffix in ("-wal", "-shm", ""):
        (tmp_path / f"awa.db{suffix}").unlink(missing_ok=True)
    assert not path.exists()


async def test_repr_names_the_file(tmp_path: Path) -> None:
    index = SqliteThreadIndex(tmp_path / "awa.db")
    try:
        assert repr(index) == f"SqliteThreadIndex({str(tmp_path / 'awa.db')!r})"
    finally:
        await index.close()


@pytest.mark.parametrize("call", ["claim", "owner", "threads", "forget", "claim_decision", "ping"])
async def test_a_call_after_close_raises_a_data_store_error(tmp_path: Path, call: str) -> None:
    index = SqliteThreadIndex(tmp_path / "awa.db")
    await index.close()
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "claim": lambda: index.claim("sys", "t", owner("a")),
        "owner": lambda: index.owner("sys", "t"),
        "threads": lambda: index.threads("sys", tenant="t"),
        "forget": lambda: index.forget("sys", "t"),
        "claim_decision": lambda: index.claim_decision("sys", "t", "v"),
        "ping": lambda: index.ping(),
    }
    with pytest.raises(DataStoreError, match="closed"):
        await calls[call]()


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


async def test_open_store_memory_backend_is_usable() -> None:
    async with open_store(_settings(store_backend="memory")) as store:
        assert isinstance(store, InMemoryStore)
        await store.aput(("ns",), "k", {"v": 1})
        item = await store.aget(("ns",), "k")
        assert item is not None
        assert item.value == {"v": 1}


async def test_open_store_postgres_without_dsn_fails_clearly() -> None:
    with pytest.raises(MissingConfigurationError, match="AWA_POSTGRES_DSN"):
        async with open_store(_settings(store_backend="postgres", postgres_dsn=None)):
            pass


async def test_open_store_postgres_without_extra_installed_fails_clearly() -> None:
    with (
        patch.dict(sys.modules, {"langgraph.store.postgres.aio": None}),
        pytest.raises(OptionalDependencyError, match="optional extra"),
    ):
        async with open_store(
            _settings(store_backend="postgres", postgres_dsn="postgresql://localhost/db")
        ):
            pass


class _FakePostgresStore:
    """Records how `open_store` opens, sweeps and closes the Postgres store."""

    opened: list[dict[str, object]] = []
    events: list[str] = []

    def __init__(self, ttl: object) -> None:
        self.ttl_config = ttl

    @classmethod
    def from_conn_string(cls, dsn: str, **options: object) -> object:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def opened() -> AsyncIterator[_FakePostgresStore]:
            cls.opened.append(options)
            yield cls(options.get("ttl"))
            cls.events.append("closed")

        return opened()

    async def setup(self) -> None:
        self.events.append("setup")

    async def sweep_ttl(self) -> int:
        self.events.append("sweep")
        return 0

    async def start_ttl_sweeper(self) -> object:
        self.events.append("sweeper started")
        return None

    async def stop_ttl_sweeper(self, timeout: float | None = None) -> bool:
        self.events.append("sweeper stopped")
        return True


async def test_open_store_postgres_deletes_expired_items(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def no_lock(dsn: str) -> AsyncIterator[None]:
        yield None

    _FakePostgresStore.opened.clear()
    _FakePostgresStore.events.clear()
    monkeypatch.setattr("awa.persistence.store.postgres_store_class", lambda: _FakePostgresStore)
    monkeypatch.setattr("awa.persistence.checkpointer.postgres_setup_lock", no_lock)

    settings = _settings(store_backend="postgres", postgres_dsn="postgresql://localhost/db")
    async with open_store(settings) as store:
        assert isinstance(store, _FakePostgresStore)
        assert _FakePostgresStore.events == ["setup", "sweep", "sweeper started"]

    # A TTL configuration makes the store sweep; reads do not extend a memory's lifetime.
    ttl = _FakePostgresStore.opened[0]["ttl"]
    assert isinstance(ttl, dict)
    assert ttl["refresh_on_read"] is False and ttl["sweep_interval_minutes"] > 0
    assert _FakePostgresStore.events[-2:] == ["sweeper stopped", "closed"]


async def test_expired_memories_leave_the_in_memory_store() -> None:
    from datetime import UTC, datetime, timedelta

    from awa.memory import AgentMemory, MemoryScope

    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    store = InMemoryStore()
    memory = AgentMemory(store, scope="user", ttl=timedelta(minutes=5), clock=lambda: clock[0])
    owner = MemoryScope(user_id="ada", agent="a")
    await memory.remember("prefers short answers", owner=owner)
    assert len(await store.asearch(("awa",), limit=100)) == 1

    clock[0] += timedelta(minutes=6)
    assert await memory.prune() != []
    assert await store.asearch(("awa",), limit=100) == []


def test_a_failed_commit_rolls_back_and_the_connection_keeps_writing(tmp_path: Path) -> None:
    from awa.persistence.sql import SqliteConnection

    db = SqliteConnection(tmp_path / "x.db", timeout=1, pragmas=["foreign_keys=ON"])
    db.conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    db.conn.execute(
        "CREATE TABLE child (parent INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    with pytest.raises(sqlite3.IntegrityError), db.writing() as conn:
        conn.execute("INSERT INTO child VALUES (7)")  # fails only at COMMIT

    with db.writing() as conn:
        conn.execute("INSERT INTO parent VALUES (1)")
    assert db.conn.execute("SELECT count(*) FROM child").fetchone() == (0,)
    assert db.conn.execute("SELECT count(*) FROM parent").fetchone() == (1,)
    db.conn.close()
