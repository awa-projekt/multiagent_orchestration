"""Graphs that write save each step before the next starts, so a crash does not repeat a write."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from awa.agents import Agent
from awa.connectors import (
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    MockTransport,
    Operation,
    Param,
    connector_tools,
    mock_response,
)
from awa.team import Team
from awa.testing import FakeChatModel, tool_call
from awa.tools import tool
from awa.tools.risk import graph_durability
from awa.workflows import Sequential

ENV = {"DEMO_URL": "https://api.example.test", "DEMO_TOKEN": "token-value"}
CREATE = Operation(
    "create_item",
    "Create an item.",
    method="POST",
    path="/items",
    params=(Param("title", "Title.", location="body", required=True),),
    write=True,
)
SPEC = ConnectorSpec(
    name="demo",
    title="Demo",
    category="data",
    summary="A demo system.",
    base_url="{DEMO_URL}",
    env=(EnvVar("DEMO_URL", "Address.", secret=False),),
    auth=(BearerAuth(EnvVar("DEMO_TOKEN", "Token.")),),
    operations=(CREATE,),
)


@tool(risk=[])
def look_up(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}"


def _writer(model: Any, transport: MockTransport, saver: Any = None) -> Agent:
    tools = connector_tools(SPEC, env=ENV, config={"requires_approval": False}, transport=transport)
    return Agent("clerk", model=model, tools=tools, checkpointer=saver or InMemorySaver())


def test_each_graph_gets_the_durability_of_its_tools() -> None:
    saver = InMemorySaver()
    transport = MockTransport()
    writer = _writer(FakeChatModel(["x"]), transport)
    reader = Agent("reader", model=FakeChatModel(["x"]), tools=[look_up])
    plain = Agent("plain", model=FakeChatModel(["x"]))
    lead = Agent("lead", model=FakeChatModel(["x"]))
    cases = [
        (writer, True),
        (reader, False),
        (plain, False),
        (Sequential("f", [reader, writer]), True),
        (Sequential("f", [reader, plain]), False),
        (Team("desk", orchestrator=lead, members=[reader, writer]), True),
        (Team("desk", orchestrator=lead, members=[reader, plain]), False),
    ]
    for system, durable in cases:
        assert graph_durability(system, "s", saver) is durable, system
    # Without a checkpointer of its own the graph takes the durability it runs in.
    assert graph_durability(writer, "s", None) is None


async def test_the_durability_is_passed_to_the_graph_and_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("awa.observability.spans._tracer", lambda: provider.get_tracer("t"))
    transport = MockTransport({"POST /items": mock_response(201, json={"id": "1"})})
    writer = _writer(
        FakeChatModel([tool_call("demo_create_item", {"title": "x"}), "ok"]), transport
    )
    await writer.run("go")
    [span] = [s for s in exporter.get_finished_spans() if s.name == "awa.graph.durability"]
    assert dict(span.attributes or {}) == {
        "awa.graph.name": "clerk",
        "awa.graph.writes": True,
        "awa.graph.durability": "sync",
    }


class Crash(BaseException):
    """The process is killed (nothing it still had to save reaches the database)."""


class CrashingModel(FakeChatModel):
    """Answers its script, then the process is killed on the next model call."""

    saver: Any = None

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if self.call_count >= len(self.responses):
            self.saver.dead = True
            raise Crash
        return await super().ainvoke(input, config, **kwargs)


class KillableSaver(InMemorySaver):
    """A checkpointer with database latency whose saves are lost once the process was killed."""

    def __init__(self) -> None:
        super().__init__()
        self.dead = False

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        await asyncio.sleep(0.002)  # database latency (about 15 ms on Windows)
        if self.dead:
            return config
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self, config: Any, writes: Any, task_id: str, task_path: str = ""
    ) -> None:
        await asyncio.sleep(0.002)  # database latency (about 15 ms on Windows)
        if not self.dead:
            self.put_writes(config, writes, task_id, task_path)


async def _crash_after_the_write(thread: str) -> int:
    """Write once, crash before the next step, resume in a new process; the writes made."""

    saver = KillableSaver()
    transport = MockTransport({"POST /items": mock_response(201, json={"id": "1"})})

    model = CrashingModel([tool_call("demo_create_item", {"title": "x"}, id="call_1")])
    model.saver = saver
    first = _writer(model, transport, saver)
    with pytest.raises(Crash):
        await first.run("create it", thread_id=thread)
    saver.dead = False  # the next process starts with what reached the database
    second = _writer(FakeChatModel(["created"]), transport, saver)
    result = await second.resume(thread, None)
    assert result.output == "created"
    return len(transport.requests)


@pytest.mark.slow
async def test_crash_injection_never_repeats_a_write() -> None:
    """200 crashes right after a write; about 15 s (the saves wait like a database does).

    Under LangGraph's default `"async"` the save of the step that wrote is still in flight
    when the process dies, so the resumed run writes again; with `"sync"` it is not.
    """

    writes = [await _crash_after_the_write(f"t{n}") for n in range(200)]
    assert writes == [1] * 200
