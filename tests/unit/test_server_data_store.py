"""The data endpoints: the local data file, the backend choice, the pseudonym key, the PostgreSQL open, and the default adapters to `awa.blueprints`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import awa.server._data_filesystem as filesystem
from _helpers.build_report import FakeReport
from _helpers.data_http import GOOD, auth, client_for, make_app, start_build, upload, wait_for
from awa.automation import SqliteRunStore
from awa.core.settings import Settings, get_settings
from awa.errors import DataStoreError, OptionalDependencyError
from awa.models import ModelRegistry
from awa.server._data_blueprints import BlueprintsEngine, blueprint_system
from awa.server._data_kit import tenant_folder
from awa.server._data_store import (
    PostgresData,
    ensure_pseudonym_key,
    open_local_data,
    pseudonym_key,
    uses_postgres,
)
from awa.server.errors import BuildDesignInvalidError
from awa.server.ports import FORMAT, BuildRecord, BuildSpec, UploadRecord
from awa.testing import FakeClock, FixedIds, FunctionChatModel, ModelCall, tool_call
from awa.testing.contracts.upload_store import CSV, receive

DSN = "postgresql://awa_user:s3cret-pw@db.example:6543/awa_prod"


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def test_the_data_lives_in_postgres_when_a_dsn_is_given_and_sqlite_is_not_forced() -> None:
    assert uses_postgres(DSN, settings())
    assert not uses_postgres(None, settings())
    assert not uses_postgres("", settings())
    assert not uses_postgres(DSN, settings(data_backend="sqlite"))


def test_the_pseudonym_key_is_made_once_read_again_and_never_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("AWA_PSEUDONYM_KEY", raising=False)
    get_settings.cache_clear()
    caplog.set_level("DEBUG")
    assert pseudonym_key(settings()) is None

    first = ensure_pseudonym_key(tmp_path, settings())
    written = (tmp_path / ".env").read_text(encoding="utf-8")
    second = ensure_pseudonym_key(tmp_path, settings())

    assert first.key == second.key and len(first.key) >= 16
    assert written.count("AWA_PSEUDONYM_KEY=") == 1
    assert (tmp_path / ".env").read_text(encoding="utf-8") == written
    assert first.key.decode() not in caplog.text
    get_settings.cache_clear()


def test_a_key_in_the_settings_is_used_without_touching_the_folder(tmp_path: Path) -> None:
    given = settings(pseudonym_key="k" * 32, pseudonym_key_id="7")

    key = ensure_pseudonym_key(tmp_path, given)

    assert key.key == b"k" * 32 and key.key_id == "7"
    assert not (tmp_path / ".env").exists()


async def test_the_local_data_keeps_the_runs_in_the_data_file(tmp_path: Path) -> None:
    data = open_local_data(tmp_path / "runs.db")
    try:
        assert isinstance(data.runs, SqliteRunStore)
        assert data.database.path == tmp_path / "runs.db"
    finally:
        await data.close()


def test_a_missing_postgres_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise OptionalDependencyError("psycopg is missing")

    monkeypatch.setattr("awa.persistence.sql.shared_pool", missing)

    with pytest.raises(OptionalDependencyError, match="postgres"):
        PostgresData(DSN, "awa", settings=settings())


class RefusingPool:
    def __init__(self) -> None:
        self.closed = False

    async def open(self, **kwargs: Any) -> None:
        raise OSError("connection refused; password s3cret-pw")

    async def close(self) -> None:
        self.closed = True


async def test_a_pool_that_cannot_open_is_closed_and_reported_without_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RefusingPool()
    data = object.__new__(PostgresData)
    data.dsn = DSN
    data.pool = pool
    data._opened = False
    data.checkpointer = SimpleNamespace()
    data._new_pool = lambda: pool  # type: ignore[method-assign]
    monkeypatch.setattr("awa.persistence.sql.check_event_loop", lambda: None)

    with pytest.raises(DataStoreError) as caught:
        await asyncio.wait_for(PostgresData.open(data), timeout=5)

    message = str(caught.value)
    assert pool.closed
    assert "db.example:6543/awa_prod" in message
    assert "s3cret-pw" not in message and "awa_user" not in message
    assert isinstance(caught.value.__cause__, OSError)


def test_store_upgrades_run_on_the_admin_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.project_setup.data_store import upgrade_store

    opened: list[str] = []

    class Pool:
        async def open(self, wait: bool) -> None: ...

        async def close(self) -> None: ...

    class Ledger:
        def __init__(self, pool: Any, **_: Any) -> None: ...

        async def upgrade(self) -> list[Any]:
            return []

    def shared_pool(dsn: str, **_: Any) -> Pool:
        opened.append(dsn)
        return Pool()

    monkeypatch.setenv("AWA_POSTGRES_DSN", "postgresql://pooler/awa")
    monkeypatch.setenv("AWA_POSTGRES_ADMIN_DSN", "postgresql://direct/awa")
    monkeypatch.setattr("awa.persistence.sql.shared_pool", shared_pool)
    monkeypatch.setattr("awa.persistence.ledger.PostgresLedger", Ledger)

    assert asyncio.run(upgrade_store(tmp_path)) == []
    assert opened == ["postgresql://direct/awa"]


class TestFileAdapters:
    """What only the adapters on disk promise: layout, restart, damaged files, leftovers."""

    def stores(
        self, root: Path
    ) -> tuple[filesystem.FileUploadStore, filesystem.FileBuildStore, FakeClock]:
        clock = FakeClock()
        uploads = filesystem.FileUploadStore(root, ttl=3600, clock=clock, ids=FixedIds().new_id)
        return uploads, filesystem.FileBuildStore(root, ttl=3600, clock=clock), clock

    def test_what_is_kept_survives_a_new_store_on_the_same_folder(self, tmp_path: Path) -> None:
        uploads, builds, clock = self.stores(tmp_path)
        record = receive(uploads, {"a.csv": CSV})
        builds.save(BuildRecord("bld_1", "acme", "alice", clock()))
        builds.project_dir("acme", "bld_1")

        again_uploads, again_builds, _ = self.stores(tmp_path)

        assert again_uploads.get("acme", record.id) == record
        assert again_builds.get("acme", "bld_1").principal == "alice"
        with again_uploads.open_files(record) as folder:
            assert (folder / "a.csv").read_bytes() == CSV

    def test_the_tenant_folder_is_a_hash_and_ids_are_the_only_other_names(
        self, tmp_path: Path
    ) -> None:
        uploads, _, _ = self.stores(tmp_path)
        record = receive(uploads, {"a.csv": CSV}, tenant="../../etc/evil")

        folder = tmp_path / tenant_folder("../../etc/evil")
        assert sorted(p.name for p in tmp_path.iterdir()) == [folder.name]
        assert (folder / "uploads" / record.id / "upload.json").is_file()
        assert (folder / "uploads" / record.id / "files" / "a.csv").read_bytes() == CSV

    def test_a_record_of_another_format_is_not_read_and_not_purged(self, tmp_path: Path) -> None:
        uploads, builds, clock = self.stores(tmp_path)
        record = receive(uploads, {"a.csv": CSV})
        meta = tmp_path / tenant_folder("acme") / "uploads" / record.id / "upload.json"
        meta.write_text(meta.read_text("utf-8").replace(f'"format": {FORMAT}', '"format": 99'))
        clock.advance(seconds=7200)

        with pytest.raises(Exception, match="upload"):  # UploadNotFoundError
            uploads.get("acme", record.id)
        assert uploads.purge() == 0  # a newer awa wrote it: this one leaves it alone
        assert meta.exists()
        with pytest.raises(ValueError, match="format"):
            UploadRecord.from_json(meta.read_text("utf-8"))
        builds.save(BuildRecord("bld_1", "acme", "alice", clock()))
        build_meta = tmp_path / tenant_folder("acme") / "builds" / "bld_1" / "build.json"
        build_meta.write_text(
            build_meta.read_text("utf-8").replace(f'"format": {FORMAT}', '"format": 99')
        )
        with pytest.raises(ValueError, match="format"):
            BuildRecord.from_json(build_meta.read_text("utf-8"))
        assert builds.list_builds("acme") == []

    def test_a_crashed_receive_leaves_staging_that_a_purge_removes_after_an_hour(
        self, tmp_path: Path
    ) -> None:
        uploads, _, clock = self.stores(tmp_path)
        stale = (
            tmp_path / tenant_folder("acme") / ".staging" / f"{int(clock().timestamp())}_st_crash"
        )
        (stale / "in").mkdir(parents=True)
        (stale / "in" / "half.csv").write_bytes(b"id;")

        uploads.purge()
        assert stale.exists()
        clock.advance(seconds=3601)
        uploads.purge()

        assert not stale.exists()

    def test_a_receive_slower_than_an_hour_keeps_its_staging_while_allowed(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        uploads = filesystem.FileUploadStore(
            tmp_path, ttl=3600, clock=clock, ids=FixedIds().new_id, stale_after=7260
        )
        slow = tmp_path / tenant_folder("acme") / ".staging" / f"{int(clock().timestamp())}_st_slow"
        slow.mkdir(parents=True)

        clock.advance(seconds=3601)
        uploads.purge()
        assert slow.exists()
        clock.advance(seconds=3700)
        uploads.purge()
        assert not slow.exists()

    def test_a_kept_upload_appears_complete_or_not_at_all(self, tmp_path: Path) -> None:
        uploads, _, _ = self.stores(tmp_path)
        record = receive(uploads, {"a.csv": CSV})

        names = {
            p.name for p in (tmp_path / tenant_folder("acme") / "uploads" / record.id).iterdir()
        }

        assert names == {"upload.json", "files"}
        assert not list((tmp_path / tenant_folder("acme") / ".staging").iterdir())

    def test_a_clock_in_the_future_expires_uploads_for_every_adapter(self, tmp_path: Path) -> None:
        uploads, _, clock = self.stores(tmp_path)
        record = receive(uploads, {"a.csv": CSV})

        clock.advance(days=2)

        assert uploads.purge() == 1
        assert not (tmp_path / tenant_folder("acme") / "uploads" / record.id).exists()


def test_an_upload_kept_while_another_is_received_counts_against_that_one(tmp_path: Path) -> None:
    from awa.server.errors import QuotaExceededError
    from awa.testing.contracts.upload_store import KINDS, LIMITS, write

    clock = FakeClock()
    store = filesystem.FileUploadStore(tmp_path, ttl=3600, clock=clock, ids=FixedIds().new_id)
    quota = len(CSV) * 3 // 2  # room for one of the two uploads, not for both
    with store.begin("acme", "alice", quota=quota) as late:  # began while nothing was stored
        receive(store, {"a.csv": CSV}, quota=quota)  # another upload is kept in the meantime
        with pytest.raises(QuotaExceededError):
            write(late, {"b.csv": CSV})
            late.commit(LIMITS, kinds=KINDS)  # type: ignore[arg-type]
    assert store.used_bytes("acme") == len(CSV)


BLUEPRINT = """\
name: shop
connectors:
  orders: {use: workspace, root: data, database: orders.db}
agents:
  - name: clerk
    instructions: Answer order questions.
    tools: [connector:orders.query, connector:orders.execute]
limits: {max_requests: 25}
"""


def write_shop(folder: Path) -> None:
    (folder / "data").mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(folder / "data" / "orders.db")) as db, db:
        db.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
        db.execute("INSERT INTO orders VALUES (7, 'shipped')")
    (folder / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8", newline="\n")


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("orders_query", {"query": "SELECT status FROM orders WHERE id = 7"})
        return f"Answer from: {call.last_message.content}"

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(respond))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


# ---------------------------------------------------------------------- the system of a project


async def test_a_project_runs_on_its_own_data_whatever_the_working_directory(
    tmp_path: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    write_shop(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    served = blueprint_system(project)
    result = await served.system.run("Where is order 7?")

    assert "shipped" in result.output
    assert served.usage_limits is not None and served.usage_limits.max_requests == 25
    assert served.diagram and not list(elsewhere.iterdir())


def test_a_project_blueprint_cannot_import_code_of_its_own(
    tmp_path: Path, models: ModelRegistry
) -> None:
    project = tmp_path / "project"
    write_shop(project)
    text = (
        (project / "blueprint.yaml")
        .read_text("utf-8")
        .replace(
            "tools: [connector:orders.query, connector:orders.execute]", "tools: ['os:system']"
        )
    )
    (project / "blueprint.yaml").write_text(text, encoding="utf-8", newline="\n")

    with pytest.raises(Exception, match="os:system|import|allow"):
        blueprint_system(project)


async def test_a_build_runs_end_to_end_with_the_default_system_factory(
    tmp_path: Path, models: ModelRegistry
) -> None:
    from _helpers.data_http import ScriptedBuildEngine

    def write_real_project(spec: BuildSpec) -> Any:
        write_shop(spec.out_dir)
        return GOOD

    app = make_app(
        tmp_path / "store",
        engine=ScriptedBuildEngine(write_real_project),
        system_factory=blueprint_system,
    )
    async with client_for(app) as client:
        build_id = (await start_build(client, await upload(client))).json()["build_id"]
        await wait_for(client, build_id, "completed")

        ran = await client.post(
            f"/v1/builds/{build_id}/runs",
            headers=auth("k-alice"),
            json={"input": "Where is order 7?"},
        )

        assert ran.status_code == 200, ran.text
        assert "shipped" in ran.json()["text"]
        assert ran.json()["agent"] == "clerk"
        assert str(tmp_path) not in ran.text


# ---------------------------------------------------------------------- the engine


def spec(tmp_path: Path, **changes: Any) -> BuildSpec:
    base: dict[str, Any] = {
        "description": "Check orders",
        "data": tmp_path / "data",
        "out_dir": tmp_path / "out",
        "notes": {"a.csv": "keep"},
        "model": "fast",
        "rounds": 2,
        "target": 0.8,
        "max_requests": 40,
        "mask": "drop",
        "guidance": "brief",
        "answers": {},
    }
    return BuildSpec(**{**base, **changes})


async def test_the_engine_hands_the_build_what_the_server_decided_and_reports_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_build_project(description: str, **options: Any) -> FakeReport:
        seen.update(options, description=description)
        progress = options["on_progress"]

        @dataclass
        class Step:
            stage: str = "design"
            round: int = 0
            message: str = "Designing"
            pass_rate: float | None = None
            event: str = ""
            data: dict[str, Any] = field(default_factory=dict)

        progress(Step())
        progress(Step(event="design.acceptance", data={"criteria": 3}))
        return FakeReport()

    monkeypatch.setattr("awa.blueprints.build_project", fake_build_project)
    events: list[tuple[str, dict[str, Any]]] = []

    outcome = await BlueprintsEngine()(
        spec(tmp_path), lambda name, data: events.append((name, dict(data)))
    )

    assert seen["description"] == "Check orders"
    assert seen["environment"] is None  # the server's own connectors never reach a tenant's design
    assert (
        seen["force"] is True
        and seen["data"] == [tmp_path / "data"]
        and seen["out_dir"] == tmp_path / "out"
    )
    assert (seen["model"], seen["max_rounds"], seen["target_pass_rate"]) == ("fast", 2, 0.8)
    assert (seen["mask"], seen["guidance"], seen["notes"], seen["answers"]) == (
        "drop",
        "brief",
        {"a.csv": "keep"},
        None,
    )
    assert seen["usage_limits"].max_requests == 40
    assert events == [
        (
            "build.progress",
            {"stage": "design", "round": 0, "message": "Designing", "pass_rate": None},
        ),
        ("design.acceptance", {"criteria": 3}),
    ]
    assert outcome.passed and outcome.pass_rate == 0.9 and outcome.rounds == 2
    assert outcome.summary == "shop: 3 of 3 eval cases pass."  # never the folder line
    assert outcome.failing == ("c9", "c4") and outcome.flaky == ("c2",)
    assert outcome.assumptions == ("amounts in euro",)
    assert dict(outcome.usage) == {"requests": 14, "total_tokens": 0}
    assert not outcome.questions


async def test_the_engine_passes_answers_on_and_turns_questions_into_an_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    @dataclass
    class Q:
        id: str
        text: str
        options: tuple[str, ...] = ()
        evidence: tuple[str, ...] = ()

    async def fake_build_project(description: str, **options: Any) -> FakeReport:
        seen.append(options["answers"])
        if options["answers"] is None:
            return FakeReport(
                question={"questions": [Q("q1", "Which?", ("a", "b"), ("f1.t1.id",))]}
            )
        return FakeReport()

    monkeypatch.setattr("awa.blueprints.build_project", fake_build_project)

    asked = await BlueprintsEngine()(spec(tmp_path), lambda *_: None)
    done = await BlueprintsEngine()(spec(tmp_path, answers={"q1": "a"}), lambda *_: None)

    assert [(q.id, q.text, q.options, q.evidence) for q in asked.questions] == [
        ("q1", "Which?", ("a", "b"), ("f1.t1.id",))
    ]
    assert seen == [None, {"q1": "a"}] and done.passed and not done.questions
    assert json.dumps(asked.questions[0].__dict__)


async def test_files_that_cannot_be_used_end_the_build_with_the_reason_not_with_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_build_project(description: str, **options: Any) -> FakeReport:
        return FakeReport(
            question={"status": "blocked", "questions": [], "reason": "no file awa can read"}
        )

    monkeypatch.setattr("awa.blueprints.build_project", fake_build_project)

    with pytest.raises(BuildDesignInvalidError, match="no file awa can read") as caught:
        await BlueprintsEngine()(spec(tmp_path), lambda *_: None)

    assert caught.value.details == {"issues": []}
