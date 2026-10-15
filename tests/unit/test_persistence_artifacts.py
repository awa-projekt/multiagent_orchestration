"""Artifacts: copies not stored objects, deletion, listing and the store layer helpers."""

from __future__ import annotations

import json
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langgraph.store.memory import InMemoryStore

import awa.persistence.artifacts as artifacts_module
import awa.persistence.sql as postgres_setup
from awa.core.settings import Settings
from awa.errors import MissingConfigurationError, OptionalDependencyError, PostgresSetupTimeoutError
from awa.persistence import ArtifactRepository
from awa.persistence.artifacts import (
    FileArtifactRepository,
    InMemoryArtifactRepository,
    PostgresArtifactRepository,
    open_artifact_repository,
)
from awa.persistence.namespaces import iter_namespace_items, iter_prefix_items, namespace_label
from awa.persistence.sql import POSTGRES_SETUP_LOCK_KEY, acquire_setup_lock


async def test_in_memory_artifacts_return_copies_not_the_stored_object() -> None:
    repo = InMemoryArtifactRepository()
    value = {"items": [1, 2]}

    await repo.put("a", value)
    value["items"].append(3)
    loaded = await repo.get("a")
    loaded["items"].append(4)

    assert await repo.get("a") == {"items": [1, 2]}


async def test_in_memory_artifacts_reject_non_json_values_like_the_other_backends() -> None:
    with pytest.raises(TypeError):
        await InMemoryArtifactRepository().put("a", object())


async def test_file_artifacts_keep_the_old_value_when_a_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = FileArtifactRepository(tmp_path)
    await repo.put("doc", {"v": 1})

    def failing_replace(src: str, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(artifacts_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        await repo.put("doc", {"v": 2})
    monkeypatch.undo()

    assert await repo.get("doc") == {"v": 1}
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp-")] == []


async def test_file_artifacts_reject_non_json_values(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        await FileArtifactRepository(tmp_path).put("doc", {"v": object()})


async def test_file_artifacts_round_trip_refs_with_path_characters(tmp_path: Path) -> None:
    repo = FileArtifactRepository(tmp_path)

    await repo.put("run:a/b\\c", "text")

    assert await repo.get("run:a/b\\c") == "text"
    assert len(list(tmp_path.iterdir())) == 1


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self._conn.statements.append(sql)
        if sql.startswith("SELECT pg_try_advisory_lock"):
            self._row = (self._conn.lock_free,)
            return
        # Fail on SQL this fake does not model instead of answering with no row:
        # a silent `None` once made the setup-lock poll spin forever.
        assert sql.startswith(("SELECT payload::text", "SELECT 1 FROM awa_artifacts")), sql
        payload = self._conn.rows.get(params[0])
        if payload is None:
            self._row = None
        elif sql.startswith("SELECT payload::text"):
            self._row = (payload,)
        else:
            self._row = (1,)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeConnection:
    """Records SQL and stores JSON payloads as their text."""

    def __init__(self, *, lock_free: bool = True) -> None:
        self.statements: list[str] = []
        self.rows: dict[str, str] = {}
        self.lock_free = lock_free

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append(sql)
        if params is not None and sql.startswith("INSERT"):
            ref, payload = params
            self.rows[ref] = payload

    def cursor(self, **_options: Any) -> _FakeCursor:
        return _FakeCursor(self)


_NEEDS_PSYCOPG = pytest.mark.skipif(
    find_spec("psycopg") is None, reason='install the "postgres" extra'
)


@_NEEDS_PSYCOPG
@pytest.mark.parametrize("value", ["plain text", {"a": [1, "x"]}, 3, None])
async def test_postgres_artifacts_round_trip_every_json_value(value: Any) -> None:
    conn = _FakeConnection()
    repo = PostgresArtifactRepository(conn)
    await repo.setup()

    await repo.put("ref", value)

    assert await repo.exists("ref") is True
    assert await repo.get("ref") == value
    [insert] = [sql for sql in conn.statements if sql.startswith("INSERT")]
    # `json` keeps the text as written; `jsonb` would not read every value back unchanged.
    assert "%s::json)" in insert
    assert json.loads(conn.rows["ref"]) == value


@_NEEDS_PSYCOPG
async def test_postgres_setup_gives_up_when_the_lock_stays_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConnection(lock_free=False)
    repo = PostgresArtifactRepository(conn)

    with pytest.raises(PostgresSetupTimeoutError, match="setup lock") as caught:
        await acquire_setup_lock(conn, timeout=0.05)
    assert isinstance(caught.value, TimeoutError) and caught.value.retryable
    assert caught.value.details["lock_key"] == POSTGRES_SETUP_LOCK_KEY
    # The repository's setup uses the (test-bounded) default timeout.
    monkeypatch.setattr(postgres_setup, "POSTGRES_SETUP_LOCK_TIMEOUT", 0.05)
    with pytest.raises(PostgresSetupTimeoutError):
        await repo.setup()
    assert not any(sql.startswith("CREATE TABLE") for sql in conn.statements)


@_NEEDS_PSYCOPG
async def test_postgres_artifacts_missing_ref_raises_key_error() -> None:
    repo = PostgresArtifactRepository(_FakeConnection())

    assert await repo.exists("nope") is False
    with pytest.raises(KeyError):
        await repo.get("nope")


@pytest.fixture(params=["memory", "file"])
def artifacts(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactRepository:
    if request.param == "file":
        return FileArtifactRepository(tmp_path / "artifacts")
    return InMemoryArtifactRepository()


async def test_delete_and_list_refs(artifacts: ArtifactRepository) -> None:
    for ref in ("b:2", "a:1", "a:2", "a/x:y"):
        await artifacts.put(ref, {"ref": ref})

    assert await artifacts.list_refs() == ["a/x:y", "a:1", "a:2", "b:2"]
    assert await artifacts.list_refs("a:") == ["a:1", "a:2"]
    assert await artifacts.list_refs("zzz") == []
    assert await artifacts.delete("a:1") is True
    assert await artifacts.delete("a:1") is False
    assert not await artifacts.exists("a:1")
    assert await artifacts.list_refs("a") == ["a/x:y", "a:2"]


async def test_file_listing_skips_foreign_and_temporary_files(tmp_path: Path) -> None:
    artifacts = FileArtifactRepository(tmp_path)
    await artifacts.put("kept", 1)
    (tmp_path / ".tmp-123.json").write_text("{}", encoding="utf-8")
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
    (tmp_path / "other.json").write_text('{"no_ref": 1}', encoding="utf-8")

    assert await artifacts.list_refs() == ["kept"]


async def test_custom_backends_without_delete_say_so() -> None:
    class Minimal(ArtifactRepository):
        async def put(self, ref: str, value: Any) -> None: ...

        async def get(self, ref: str) -> Any:
            raise KeyError(ref)

        async def exists(self, ref: str) -> bool:
            return False

    with pytest.raises(NotImplementedError, match="Minimal does not implement delete"):
        await Minimal().delete("x")
    with pytest.raises(NotImplementedError, match="list_refs"):
        await Minimal().list_refs()


async def test_store_layer_helpers() -> None:
    store = InMemoryStore()
    await store.aput(("awa", "x"), "a", {})
    await store.aput(("awa", "x", "nested"), "b", {})
    for index in range(150):
        await store.aput(("awa", "y"), f"k{index}", {})

    assert [i.key async for i in iter_namespace_items(store, ("awa", "x"))] == ["a"]
    assert sorted([i.key async for i in iter_prefix_items(store, ("awa", "x"))]) == ["a", "b"]
    assert len([i async for i in iter_prefix_items(store, ("awa", "y"), page_size=40)]) == 150
    assert namespace_label("jane.doe@example.com") == "jane%2Edoe%40example%2Ecom"
    with pytest.raises(ValueError):
        namespace_label("")


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


async def test_in_memory_repository_round_trips() -> None:
    repo = InMemoryArtifactRepository()

    assert await repo.exists("x") is False
    await repo.put("x", {"a": 1})
    assert await repo.exists("x") is True
    assert await repo.get("x") == {"a": 1}


async def test_file_repository_round_trips_and_persists_across_instances(tmp_path: object) -> None:
    repo_a = FileArtifactRepository(tmp_path)  # type: ignore[arg-type]
    await repo_a.put("draft:1", {"text": "hello"})

    # A second repository instance pointed at the same directory sees it too -
    # the actual property that distinguishes this from InMemoryArtifactRepository.
    repo_b = FileArtifactRepository(tmp_path)  # type: ignore[arg-type]
    assert await repo_b.exists("draft:1") is True
    assert await repo_b.get("draft:1") == {"text": "hello"}


async def test_file_repository_get_missing_ref_raises_key_error(tmp_path: object) -> None:
    repo = FileArtifactRepository(tmp_path)  # type: ignore[arg-type]

    with pytest.raises(KeyError):
        await repo.get("nope")


async def test_open_artifact_repository_memory_and_file_backends_need_no_connection(
    tmp_path: object,
) -> None:
    async with open_artifact_repository(_settings(artifact_backend="memory")) as repo:
        assert isinstance(repo, InMemoryArtifactRepository)

    async with open_artifact_repository(
        _settings(artifact_backend="file", artifact_dir=str(tmp_path))
    ) as repo:
        assert isinstance(repo, FileArtifactRepository)


async def test_open_artifact_repository_postgres_without_dsn_fails_clearly() -> None:
    with pytest.raises(MissingConfigurationError, match="AWA_POSTGRES_DSN"):
        async with open_artifact_repository(
            _settings(artifact_backend="postgres", postgres_dsn=None)
        ):
            pass


async def test_open_artifact_repository_postgres_without_extra_installed_fails_clearly() -> None:
    with (
        patch.dict(sys.modules, {"psycopg": None}),
        pytest.raises(OptionalDependencyError, match="optional extra"),
    ):
        async with open_artifact_repository(
            _settings(artifact_backend="postgres", postgres_dsn="postgresql://localhost/db")
        ):
            pass
