"""PostgreSQL persistence, erasure, pause and resume, and the SQL schema against a real database."""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from _helpers.framing import unframe
from awa.agents import Agent
from awa.blueprints._connect import run_connect_database
from awa.connectors import _local, check_connector, connector_tools
from awa.core.dotenv import read_env_file
from awa.core.settings import Settings
from awa.memory import AgentMemory, MemoryScope
from awa.persistence import open_artifact_repository, open_checkpointer, open_store
from awa.persistence.artifacts import PostgresArtifactRepository
from awa.persistence.erasure import erase_subject
from awa.persistence.retention import (
    RetentionPolicy,
    SubjectIndex,
    _purge_threads,
    apply_retention,
    thread_activity,
)
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper
from awa.workflows import Parallel, Sequential

pytestmark = pytest.mark.postgres


def settings_for(dsn: str) -> Settings:
    return Settings(
        checkpointer_backend="postgres",
        store_backend="postgres",
        artifact_backend="postgres",
        postgres_dsn=dsn,  # type: ignore[arg-type]
    )


async def test_checkpointer_persists_threads_across_connections(schema_dsn: str) -> None:
    settings = settings_for(schema_dsn)
    async with open_checkpointer(settings) as checkpointer:
        agent = Agent("assistant", model=FakeChatModel(["Hello, Ada."]), checkpointer=checkpointer)
        first = await agent.run("I am Ada.", thread_id="t-1")
        assert first.text == "Hello, Ada."

    # A new connection (for example another worker) continues the same thread.
    async with open_checkpointer(settings) as checkpointer:
        agent = Agent(
            "assistant",
            model=FakeChatModel(["You are Ada.", "I do not know yet."]),
            checkpointer=checkpointer,
        )
        second = await agent.run("Who am I?", thread_id="t-1")
        assert [m.content for m in second.messages] == [
            "I am Ada.",
            "Hello, Ada.",
            "Who am I?",
            "You are Ada.",
        ]
        other = await agent.run("Who am I?", thread_id="t-2")
        assert len(other.messages) == 2


async def test_store_round_trips_items_and_searches_namespaces(schema_dsn: str) -> None:
    settings = settings_for(schema_dsn)
    async with open_store(settings) as store:
        await store.aput(("users", "ada"), "profile", {"tone": "formal", "langs": ["en", "de"]})
        await store.aput(("users", "bob"), "profile", {"tone": "casual"})
    async with open_store(settings) as store:
        item = await store.aget(("users", "ada"), "profile")
        assert item is not None and item.value == {"tone": "formal", "langs": ["en", "de"]}
        found = await store.asearch(("users",))
        assert sorted(i.namespace[-1] for i in found) == ["ada", "bob"]
        await store.adelete(("users", "bob"), "profile")
        assert await store.aget(("users", "bob"), "profile") is None


async def test_store_deletes_expired_items(schema_dsn: str) -> None:
    settings = settings_for(schema_dsn)
    async with open_store(settings) as store:
        await store.aput(("users", "ada"), "note", {"v": 1}, ttl=0.001)  # 60 milliseconds
        await store.aput(("users", "ada"), "kept", {"v": 2})
    await asyncio.sleep(0.5)
    async with open_store(settings) as store:
        # The open swept the expired row; the one without a ttl is kept.
        assert await store.sweep_ttl() == 0
        keys = sorted(i.key for i in await store.asearch(("users",)))
        assert keys == ["kept"]


@pytest.mark.parametrize(
    "value",
    [
        "plain text",
        {"nested": [1, 2.5, None, True, {"k": "v"}]},
        [],
        None,
        0,
        1e20,  # jsonb would return the integer 100000000000000000000
        -0.0,
        "NUL \x00 inside",  # jsonb rejects \u0000 in strings
        {"b": 1, "a": 2},
    ],
)
async def test_artifacts_read_back_exactly_what_was_stored(schema_dsn: str, value: Any) -> None:
    async with open_artifact_repository(settings_for(schema_dsn)) as artifacts:
        assert isinstance(artifacts, PostgresArtifactRepository)
        assert await artifacts.exists("ref") is False
        await artifacts.put("ref", value)
        stored = await artifacts.get("ref")
        assert stored == value and type(stored) is type(value)
        assert repr(stored) == repr(value)
        await artifacts.put("ref", {"replaced": True})
        assert await artifacts.get("ref") == {"replaced": True}
        with pytest.raises(KeyError):
            await artifacts.get("missing")
        with pytest.raises(ValueError):
            await artifacts.put("nan", float("nan"))


async def test_artifact_table_of_earlier_versions_is_migrated(schema_dsn: str) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(schema_dsn, autocommit=True) as conn:
        # The table as earlier versions created it, with a stored artifact.
        await conn.execute(
            "CREATE TABLE awa_artifacts (ref TEXT PRIMARY KEY, payload JSONB NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await conn.execute("""INSERT INTO awa_artifacts VALUES ('old', '{"kept": 1}')""")
    async with open_artifact_repository(settings_for(schema_dsn)) as artifacts:
        assert await artifacts.get("old") == {"kept": 1}
        await artifacts.put("new", 1e20)
        assert await artifacts.get("new") == 1e20


async def test_many_processes_can_set_up_at_the_same_time(schema_dsn: str) -> None:
    """Several workers starting together must not fail on concurrent migrations."""

    settings = settings_for(schema_dsn)

    async def start(opener: Any) -> str:
        async with opener(settings) as backend:
            return type(backend).__name__

    for opener in (open_checkpointer, open_store, open_artifact_repository):
        names = await asyncio.wait_for(
            asyncio.gather(*(start(opener) for _ in range(5))), timeout=60
        )
        assert len(set(names)) == 1


async def test_setup_waits_for_the_lock_but_not_forever(
    schema_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    import awa.persistence.sql as postgres_setup
    from awa.errors import PostgresSetupTimeoutError

    settings = settings_for(schema_dsn)
    monkeypatch.setattr(postgres_setup, "POSTGRES_SETUP_LOCK_TIMEOUT", 0.3)
    async with await psycopg.AsyncConnection.connect(schema_dsn, autocommit=True) as holder:
        await postgres_setup.acquire_setup_lock(holder)
        with pytest.raises(PostgresSetupTimeoutError):
            async with open_store(settings):
                pass
        with pytest.raises(PostgresSetupTimeoutError):
            async with open_artifact_repository(settings):
                pass
        # Released while another setup waits: the waiter proceeds.
        waiter = asyncio.create_task(_open_and_close(settings, monkeypatch))
        await asyncio.sleep(0.1)
        await postgres_setup.release_setup_lock(holder)
        await asyncio.wait_for(waiter, timeout=30)


async def _open_and_close(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import awa.persistence.sql as postgres_setup

    monkeypatch.setattr(postgres_setup, "POSTGRES_SETUP_LOCK_TIMEOUT", 20.0)
    async with open_checkpointer(settings):
        pass


async def test_artifacts_delete_and_list_by_prefix(schema_dsn: str) -> None:
    async with open_artifact_repository(settings_for(schema_dsn)) as artifacts:
        for ref in ("b:1", "a_%:1", "a:2", "a:1", "ä:1"):
            await artifacts.put(ref, {"ref": ref})

        assert await artifacts.list_refs() == ["a:1", "a:2", "a_%:1", "b:1", "ä:1"]
        # `%` and `_` are plain characters, not patterns.
        assert await artifacts.list_refs("a_%") == ["a_%:1"]
        assert await artifacts.list_refs("a:") == ["a:1", "a:2"]
        assert await artifacts.delete("a:1") is True
        assert await artifacts.delete("a:1") is False
        assert await artifacts.list_refs("a") == ["a:2", "a_%:1"]


async def test_erase_subject_across_postgres_backends(schema_dsn: str) -> None:
    settings = settings_for(schema_dsn)
    async with (
        open_checkpointer(settings) as checkpointer,
        open_store(settings) as store,
        open_artifact_repository(settings) as artifacts,
    ):
        memory = AgentMemory(store, scope="user")
        alice = MemoryScope(agent="assistant", user_id="u-42")
        await memory.remember("Alice prefers EUR", owner=alice)
        await memory.remember(
            "Bob prefers USD", owner=MemoryScope(agent="assistant", user_id="u-7")
        )
        await AgentMemory(store, scope="global").remember("Alice is a VIP", owner=alice)
        agent = Agent(
            "assistant", model=FakeChatModel(["ok"], repeat_last=True), checkpointer=checkpointer
        )
        for thread_id in ("t-alice", "t-bob"):
            await agent.run("hello", thread_id=thread_id)
        await artifacts.put("export:u-42:1", {"rows": 1})
        await SubjectIndex(store).link("u-42", threads=["t-alice"], artifacts=["export:u-42:1"])

        report = await erase_subject(
            "u-42", store=store, checkpointer=checkpointer, artifacts=artifacts
        )

        assert report.ok, report.errors
        assert report.deleted["memories"] == 2
        assert report.ids["threads"] == ["t-alice"]
        assert report.ids["artifacts"] == ["export:u-42:1"]
        assert await checkpointer.aget_tuple({"configurable": {"thread_id": "t-alice"}}) is None
        assert await checkpointer.aget_tuple({"configurable": {"thread_id": "t-bob"}}) is not None
        assert await artifacts.list_refs() == []
        assert len(await memory.list_items(owner=MemoryScope(agent="x", user_id="u-7"))) == 1

        again = await erase_subject(
            "u-42", store=store, checkpointer=checkpointer, artifacts=artifacts
        )
        assert again.ok and again.total == 0


async def test_retention_on_postgres_uses_checkpoint_times(schema_dsn: str) -> None:
    settings = settings_for(schema_dsn)
    async with open_checkpointer(settings) as checkpointer, open_store(settings) as store:
        agent = Agent(
            "assistant", model=FakeChatModel(["ok"], repeat_last=True), checkpointer=checkpointer
        )
        await agent.run("hello", thread_id="t-1")
        await agent.run("again", thread_id="t-1")
        await agent.run("hello", thread_id="t-2")

        activity = await thread_activity(checkpointer)
        assert set(activity) == {"t-1", "t-2"}
        assert all(
            abs(datetime.now(UTC) - when) < timedelta(minutes=5) for when in activity.values()
        )

        assert await _purge_threads(checkpointer, older_than=timedelta(days=1)) == []
        later = datetime.now(UTC) + timedelta(days=3)
        report = await apply_retention(
            RetentionPolicy(max_age=timedelta(days=1)),
            checkpointer=checkpointer,
            store=store,
            now=later,
        )
        assert report.ok, report.errors
        assert report.ids["threads"] == ["t-1", "t-2"]
        assert await thread_activity(checkpointer) == {}


SENT: list[str] = []


@tool
def send(text: str) -> str:
    """Send a text to the customer."""

    SENT.append(text)
    return f"sent {text!r}"


@pytest.fixture(autouse=True)
def _clear() -> None:
    SENT.clear()


@asynccontextmanager
async def checkpointer(dsn: str) -> AsyncIterator[Any]:
    settings = Settings(checkpointer_backend="postgres", postgres_dsn=dsn)  # type: ignore[arg-type]
    async with open_checkpointer(settings) as saver:
        yield saver


def sender(name: str, text: str) -> Agent:
    # Decides from the conversation, so a model rebuilt in a new process continues.
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"{name} done"
        return tool_call("send", {"text": text})

    return Agent(name, model=FunctionChatModel(respond), tools=[create_tool_approval_wrapper(send)])


async def test_a_sequential_resumes_in_a_new_process_without_rerunning_step_one(
    schema_dsn: str,
) -> None:
    first_calls: list[int] = []

    def build(saver: Any) -> Sequential:
        drafter = Agent(
            "drafter", model=FunctionChatModel(lambda call: first_calls.append(1) or "draft")
        )
        return Sequential("pipeline", [drafter, sender("mailer", "hello")], checkpointer=saver)

    async with checkpointer(schema_dsn) as saver:
        paused = await build(saver).run("Go.", thread_id="seq-1")
    assert paused.interrupts[0]["agent"] == "mailer" and SENT == []

    async with checkpointer(schema_dsn) as saver:
        done = await build(saver).resume("seq-1", {"action": "approve"})
    assert done.output == "mailer done" and SENT == ["hello"]
    assert len(first_calls) == 1


async def test_parallel_branches_resume_one_by_one_across_processes(schema_dsn: str) -> None:
    def build(saver: Any) -> Parallel:
        return Parallel(
            "fanout",
            [sender("a", "to a"), sender("b", "to b"), Agent("c", model=FakeChatModel(["c"]))],
            checkpointer=saver,
        )

    async with checkpointer(schema_dsn) as saver:
        paused = await build(saver).run("Go.", thread_id="par-1")
    assert [p["agent"] for p in paused.interrupts] == ["a", "b"]
    first = paused.interrupts[0]["id"]

    async with checkpointer(schema_dsn) as saver:
        again = await build(saver).resume("par-1", {first: {"action": "approve"}})
    assert [p["agent"] for p in again.interrupts] == ["b"] and SENT == ["to a"]

    async with checkpointer(schema_dsn) as saver:
        done = await build(saver).resume("par-1", {"action": "approve"})
    assert done.output == {"a": "a done", "b": "b done", "c": "c"}
    assert sorted(SENT) == ["to a", "to b"]


@pytest.mark.parametrize("strict", [False, True], ids=["permissive", "strict"])
async def test_a_structured_output_is_revived_as_its_model_after_a_restart(
    schema_dsn: str, strict: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langgraph.checkpoint.serde import _msgpack

    from awa.blueprints import model_from_json_schema
    from awa.testing import structured_output

    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", strict)
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}

    def build(saver: Any) -> tuple[Sequential, type]:
        output = model_from_json_schema(schema, name="drafter_output")
        drafter = Agent(
            "drafter",
            model=FunctionChatModel(lambda call: structured_output(output, {"n": 7})),
            output_type=output,
        )
        return Sequential("pipeline", [drafter, sender("mailer", "hi")], checkpointer=saver), output

    async with checkpointer(schema_dsn) as saver:
        await build(saver)[0].run("Go.", thread_id="out-1")

    async with checkpointer(schema_dsn) as saver:
        flow, output = build(saver)
        state = await saver.aget_tuple({"configurable": {"thread_id": "out-1"}})
        revived = state.checkpoint["channel_values"]["_steps"]["drafter"]["output"]  # type: ignore[union-attr]
        done = await flow.resume("out-1", {"action": "approve"})
    assert type(revived) is output and revived.model_dump() == {"n": 7}
    assert done.output == "mailer done"


async def test_the_postgres_saver_does_not_call_a_function_named_by_a_crafted_blob(
    schema_dsn: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ormsgpack
    from langgraph.checkpoint.serde import _msgpack

    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", False)  # LangGraph's own default
    call = ormsgpack.packb(("tempfile", "mkdtemp", ("", "planted_", str(tmp_path))))
    blob = ("msgpack", ormsgpack.packb(ormsgpack.Ext(1, call)))

    async with checkpointer(schema_dsn) as saver:
        revived = saver.serde.loads_typed(blob)

    assert list(tmp_path.iterdir()) == [] and list(revived) == ["", "planted_", str(tmp_path)]


@pytest.fixture
def shop(postgres_dsn: str) -> Iterator[tuple[str, str]]:
    """A small shop in its own schema; yields `(address, schema)` and drops the schema after."""

    address = re.sub(r"^postgres(ql)?://", "postgresql://", postgres_dsn)
    schema = f"awa_shop_{uuid4().hex[:8]}"
    connection = _local._connector("postgresql", address, read_only=False)()
    cursor = connection.cursor()
    for statement in (
        f"CREATE SCHEMA {schema}",
        f"CREATE TABLE {schema}.customers (id int PRIMARY KEY, name varchar(60) NOT NULL, "
        "api_token text)",
        f"CREATE TABLE {schema}.orders (id int PRIMARY KEY, customer_id int NOT NULL "
        f"REFERENCES {schema}.customers(id), total numeric(10, 2))",
        f"CREATE VIEW {schema}.big_orders AS SELECT * FROM {schema}.orders WHERE total > 100",
        f"INSERT INTO {schema}.customers VALUES (1, 'ACME GmbH', 'tok-1'), (2, 'Globex', 'tok-2')",
        f"INSERT INTO {schema}.orders VALUES (1, 1, 49.50), (2, 2, 250.00)",
        f"ANALYZE {schema}.customers",
    ):
        cursor.execute(statement)
    connection.commit()
    connection.close()
    try:
        yield address, schema
    finally:
        connection = _local._connector("postgresql", address, read_only=False)()
        connection.cursor().execute(f"DROP SCHEMA {schema} CASCADE")
        connection.commit()
        connection.close()


def test_the_connector_describes_a_real_postgres_schema_read_only(shop: tuple[str, str]) -> None:
    address, schema = shop
    tools = {tool.name: tool for tool in connector_tools("sql", env={"DATABASE_URL": address})}

    data = json.loads(
        unframe(tools["sql_schema"].invoke({"schema_name": schema, "sample_rows": 2}))
    )

    tables = {table["name"]: table for table in data["tables"]}
    assert set(tables) == {"big_orders", "customers", "orders"}
    assert tables["big_orders"]["type"] == "view"
    assert tables["customers"]["approx_rows"] == 2
    assert "name character varying(60) NOT NULL" in tables["customers"]["columns"]
    assert tables["orders"]["keys"][0] == "PRIMARY KEY (id)"
    assert f"REFERENCES {schema}.customers(id)" in tables["orders"]["keys"][1]
    assert tables["customers"]["sample"]["columns"] == ["id", "name"]  # api_token left out
    assert "tok-1" not in json.dumps(data)
    answer = json.loads(
        unframe(
            tools["sql_query"].invoke({"query": f"SELECT total FROM {schema}.orders ORDER BY id"})
        )
    )
    assert answer["types"] == ["number"] and answer["has_more"] is False
    assert "only SELECT or WITH" in tools["sql_query"].invoke({"query": "DROP TABLE x"})


def test_the_health_check_names_the_tables_and_connect_saves_the_address(
    shop: tuple[str, str], tmp_path: Path
) -> None:
    address, schema = shop

    check = check_connector("sql", env={"DATABASE_URL": address})

    assert check.ok and "PostgreSQL answered SELECT 1" in check.message
    out = io.StringIO()

    code = run_connect_database(tmp_path, "Shop", secret=lambda _: address, out=out)

    assert code == 0
    assert read_env_file(tmp_path / ".env")["SHOP_DATABASE_URL"] == address
    assert "use: sql" in out.getvalue() and address not in out.getvalue()
