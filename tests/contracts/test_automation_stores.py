"""The automation backends run the shared suites: run stores, event queues and notifiers."""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from awa.automation import (
    InMemoryEventQueue,
    InMemoryRunStore,
    PostgresEventQueue,
    PostgresRunStore,
    SqliteRunStore,
    WebhookNotifier,
)
from awa.automation.models import ProcessRun
from awa.automation.queues import EventQueue, QueueMessage
from awa.automation.store import RunStore
from awa.testing.contracts import (
    EventQueueCapabilities,
    EventQueueContract,
    NotifierContract,
    NotifierUnderTest,
    Place,
    RunStoreCapabilities,
    RunStoreContract,
)
from awa.testing.doubles import FakeClock, FixedIds


@asynccontextmanager
async def closing(store: RunStore) -> AsyncIterator[RunStore]:
    try:
        yield store
    finally:
        await store.close()


class TestInMemoryRunStore(RunStoreContract):
    adapter = InMemoryRunStore
    capabilities = RunStoreCapabilities(
        survives_restart=False, claims_run_created_in_the_future=True
    )

    def opened(self, place: Place) -> AbstractAsyncContextManager[RunStore]:
        return closing(InMemoryRunStore())

    async def test_pin_the_payload_is_returned_as_given(
        self, place: Place, clock: FakeClock, ids: FixedIds
    ) -> None:
        async with self.opened(place) as store:
            run = ProcessRun(
                id=ids.new_id("proc"),
                automation="invoices",
                payload={"pair": (1, 2), "at": clock.now()},
            )
            await store.create(run)
            found = await store.get(run.id)

            assert found is not None
            assert found.payload == {"pair": (1, 2), "at": clock.now()}


@pytest.mark.postgres
class TestPostgresRunStore(RunStoreContract):
    adapter = PostgresRunStore
    capabilities = RunStoreCapabilities(survives_restart=True, normalizes_payload=True)

    @pytest.fixture
    def place(self, pg_place: Place) -> Place:
        return pg_place

    def opened(self, place: Place) -> AbstractAsyncContextManager[RunStore]:
        return closing(PostgresRunStore(place.dsn, schema=place.name))


class TestSqliteRunStore(RunStoreContract):
    adapter = SqliteRunStore
    capabilities = RunStoreCapabilities(survives_restart=True, normalizes_payload=True)

    def opened(self, place: Place) -> AbstractAsyncContextManager[RunStore]:
        return closing(SqliteRunStore(place.path / "awa.db"))


class TestInMemoryEventQueue(EventQueueContract):
    adapter = InMemoryEventQueue
    capabilities = EventQueueCapabilities(controllable_clock=True, lists_dead_letters=True)

    @asynccontextmanager
    async def opened(  # type: ignore[override]
        self,
        place: Place,
        *,
        name: str = "q",
        max_attempts: int = 5,
        clock: FakeClock | None = None,
    ) -> AsyncIterator[EventQueue]:
        kwargs = {} if clock is None else {"clock": clock}
        yield InMemoryEventQueue(max_attempts=max_attempts, **kwargs)  # type: ignore[arg-type]

    async def dead_letters(self, queue: EventQueue) -> list[QueueMessage]:
        assert isinstance(queue, InMemoryEventQueue)
        return list(queue.dead_letters)

    async def test_pin_the_payload_is_returned_as_given(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send({"items": [1], "pair": (1, 2)})
            [first] = await queue.receive(visibility=0)
            first.payload["items"].append(2)
            [again] = await queue.receive(visibility=0)

            assert first.payload == {"items": [1, 2], "pair": (1, 2)}
            assert again.payload == {"items": [1], "pair": (1, 2)}

    async def test_two_queues_of_one_name_are_separate_queues(self, place: Place) -> None:
        # Unlike a database queue, a name means nothing to an in-memory queue: instances never share.
        async with self.opened(place) as first, self.opened(place) as second:
            await first.send("only here")

            assert await second.receive() == []


@pytest.mark.postgres
class TestPostgresEventQueue(EventQueueContract):
    adapter = PostgresEventQueue
    capabilities = EventQueueCapabilities(
        survives_restart=True, lists_dead_letters=True, normalizes_payload=True
    )

    @pytest.fixture
    def place(self, pg_place: Place) -> Place:
        return pg_place

    @asynccontextmanager
    async def opened(  # type: ignore[override]
        self,
        place: Place,
        *,
        name: str = "q",
        max_attempts: int = 5,
        clock: FakeClock | None = None,
    ) -> AsyncIterator[EventQueue]:
        queue = PostgresEventQueue(
            place.dsn, name=name, schema=place.name, max_attempts=max_attempts
        )
        try:
            yield queue
        finally:
            await queue.close()

    async def dead_letters(self, queue: EventQueue) -> list[QueueMessage]:
        assert isinstance(queue, PostgresEventQueue)
        return await queue.dead_letters()

    async def test_two_queues_of_one_name_share_their_messages(self, place: Place) -> None:
        async with self.opened(place) as first, self.opened(place) as second:
            sent = await first.send("shared")

            assert [m.id for m in await second.receive()] == [sent]


class TestWebhookNotifier(NotifierContract):
    adapter = WebhookNotifier

    @asynccontextmanager
    async def opened(
        self, place: Place, *, accept: bool = True
    ) -> AsyncIterator[NotifierUnderTest]:  # type: ignore[override]
        bodies: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                if accept:
                    bodies.append(body["event"])
                self.send_response(200 if accept else 403)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/hook"
            notify = WebhookNotifier(
                url, secret="contract-secret", retries=0, timeout=5, allow_http=True
            )
            yield NotifierUnderTest(notify, lambda: list(bodies))
        finally:
            server.shutdown()
            server.server_close()
