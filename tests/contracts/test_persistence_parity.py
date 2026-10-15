"""The same approval-gated agent pauses and resumes the same way on every checkpointer.

One agent asks for approval before it sends; it is run, paused, and resumed with an approval and
with a rejection. The output, the tool messages and the interrupt are the same on the in-memory
saver, the SQLite file and PostgreSQL, and the same again when a new saver object resumes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from awa.agents import Agent
from awa.core.settings import Settings
from awa.persistence.checkpointer import StateInMemorySaver, open_checkpointer
from awa.persistence.sqlite import SqliteCheckpointer
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.testing.contracts import Place
from awa.tools import create_tool_approval_wrapper

SENT: list[str] = []
APPROVE = {"action": "approve"}
REJECT = {"action": "reject", "note": "no"}

Outcome = tuple[Any, ...]
Opener = Callable[[], AbstractAsyncContextManager[Any]]


@tool
def send(text: str) -> str:
    """Send a text to the customer."""

    SENT.append(text)
    return f"sent {text!r}"


def mailer(saver: Any) -> Agent:
    # Decides from the conversation, so an agent built again in a new process continues.
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "mailer done"
        return tool_call("send", {"text": "hello"})

    return Agent(
        "mailer",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(send)],
        checkpointer=saver,
    )


async def outcome(opened: Opener, decision: dict[str, str], *, reopen: bool = False) -> Outcome:
    """Run until the approval, decide, and report what the run shows."""

    SENT.clear()
    thread = f"parity-{uuid4().hex[:10]}"
    async with opened() as saver:
        paused = await mailer(saver).run("Go.", thread_id=thread)
        if not reopen:
            done = await mailer(saver).resume(thread, decision)
    if reopen:
        async with opened() as saver:  # a new saver object, as another process would hold
            done = await mailer(saver).resume(thread, decision)
    asked = [
        (i["value"]["tool"], i["value"]["args"], i["value"]["reason"], i["value"]["agent"])
        for i in paused.interrupts
    ]
    tool_messages = [m.content for m in done.messages if isinstance(m, ToolMessage)]
    return (asked, paused.output, done.output, tool_messages, done.interrupts, list(SENT))


@asynccontextmanager
async def kept(saver: Any) -> AsyncIterator[Any]:
    yield saver


def memory() -> Opener:
    saver = StateInMemorySaver()
    return lambda: kept(saver)


def sqlite(folder: Path) -> Opener:
    path = folder / f"{uuid4().hex[:8]}.db"

    @asynccontextmanager
    async def opened() -> AsyncIterator[Any]:
        saver = SqliteCheckpointer(path, retention=None)
        try:
            yield saver
        finally:
            saver.close()

    return opened


def postgres(place: Place) -> Opener:
    assert place.dsn is not None
    separator = "&" if "?" in place.dsn else "?"
    dsn = f"{place.dsn}{separator}options={quote(f'-csearch_path={place.name}')}"
    settings = Settings(checkpointer_backend="postgres", postgres_dsn=dsn)  # type: ignore[arg-type]
    return lambda: open_checkpointer(settings)


ASKED = [("send", {"text": "hello"}, "tool_call_requires_approval", "mailer")]
APPROVED = (ASKED, None, "mailer done", ["sent 'hello'"], [], ["hello"])
REJECTED = (
    ASKED,
    None,
    "mailer done",
    [
        "Tool call to 'send' was rejected by a human reviewer and was not executed. "
        "Reason: no. Do not retry this call unless the user asks for it."
    ],
    [],
    [],  # rejected: the tool never ran
)
EXPECTED = {"approve": (APPROVE, APPROVED), "reject": (REJECT, REJECTED)}


@pytest.mark.parametrize("name", EXPECTED)
async def test_memory_and_sqlite_pause_and_resume_the_same_way(name: str, tmp_path: Path) -> None:
    decision, expected = EXPECTED[name]

    in_memory = await outcome(memory(), decision)
    on_file = await outcome(sqlite(tmp_path), decision)

    assert in_memory == on_file == expected


@pytest.mark.parametrize("name", EXPECTED)
async def test_a_new_sqlite_saver_resumes_what_another_one_paused(
    name: str, tmp_path: Path
) -> None:
    decision, expected = EXPECTED[name]

    assert await outcome(sqlite(tmp_path), decision, reopen=True) == expected


@pytest.mark.postgres
@pytest.mark.parametrize("name", EXPECTED)
async def test_postgres_pauses_and_resumes_like_the_others_in_one_process_and_in_two(
    name: str, pg_place: Place
) -> None:
    decision, expected = EXPECTED[name]

    same = await outcome(postgres(pg_place), decision)
    new_object = await outcome(postgres(pg_place), decision, reopen=True)
    local = await outcome(memory(), decision)

    assert same == new_object == local == expected
