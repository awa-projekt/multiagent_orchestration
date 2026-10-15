"""Idempotent tool side effects: run exactly once, and the key released after a failure."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import StructuredTool, tool

from awa.errors import ConfigurationError
from awa.tools import create_tool_approval_wrapper, default_approval_payload
from awa.tools.idempotency import (
    IdempotencyKeyAlreadyClaimedError,
    InMemoryIdempotencyStore,
    run_idempotent,
)


async def test_runs_the_side_effect_exactly_once() -> None:
    store = InMemoryIdempotencyStore()
    calls = 0

    async def publish() -> str:
        nonlocal calls
        calls += 1
        return "published"

    result = await run_idempotent(publish, key="doc-1", store=store)

    assert result == "published"
    assert calls == 1


async def test_second_call_with_same_key_is_rejected_by_default() -> None:
    store = InMemoryIdempotencyStore()

    async def publish() -> str:
        return "published"

    await run_idempotent(publish, key="doc-1", store=store)

    with pytest.raises(IdempotencyKeyAlreadyClaimedError):
        await run_idempotent(publish, key="doc-1", store=store)


async def test_on_duplicate_returns_a_value_instead_of_raising() -> None:
    store = InMemoryIdempotencyStore()

    async def publish() -> str:
        return "published"

    await run_idempotent(publish, key="doc-1", store=store)
    result = await run_idempotent(
        publish, key="doc-1", store=store, on_duplicate=lambda key: f"already-done:{key}"
    )

    assert result == "already-done:doc-1"


async def test_concurrent_callers_race_the_same_key_only_one_wins() -> None:
    """The scenario a naive check-then-act guard gets wrong: two callers race
    the same key at the same time - only one may actually run the side effect."""

    store = InMemoryIdempotencyStore()
    ran = 0

    async def publish() -> str:
        nonlocal ran
        await asyncio.sleep(0)
        ran += 1
        return "published"

    async def attempt() -> str | Exception:
        try:
            return await run_idempotent(publish, key="doc-1", store=store)
        except IdempotencyKeyAlreadyClaimedError as error:
            return error

    results = await asyncio.gather(attempt(), attempt(), attempt(), attempt())

    assert ran == 1
    assert sum(1 for r in results if r == "published") == 1
    assert sum(1 for r in results if isinstance(r, IdempotencyKeyAlreadyClaimedError)) == 3


async def test_a_failing_side_effect_runs_once_by_default() -> None:
    store = InMemoryIdempotencyStore()
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("failed after a partial effect")
        return "ok"

    with pytest.raises(RuntimeError, match="partial effect"):
        await run_idempotent(flaky, key="charge-1", store=store)
    assert calls == 1  # no hidden second run of the effect
    assert await run_idempotent(flaky, key="charge-1", store=store) == "ok"  # the caller decides


async def test_the_in_memory_store_is_bounded() -> None:
    store = InMemoryIdempotencyStore(max_claims=2)
    for key in ("a", "b", "c"):
        assert await store.try_claim(key) is True
    assert await store.try_claim("c") is False and await store.try_claim("b") is False
    assert await store.try_claim("a") is True  # the oldest claim was forgotten
    with pytest.raises(ConfigurationError):
        InMemoryIdempotencyStore(max_claims=0)


async def test_release_of_an_unclaimed_key_is_a_noop_and_reclaim_after_release_works() -> None:
    store = InMemoryIdempotencyStore()
    await store.release("never-claimed")
    assert await store.try_claim("k") is True
    await store.release("k")
    assert await store.try_claim("k") is True


async def test_run_idempotent_releases_the_key_after_a_failure() -> None:
    store = InMemoryIdempotencyStore()

    async def always_fails() -> str:
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError):
        await run_idempotent(always_fails, key="k", store=store)

    async def works() -> str:
        return "done"

    assert await run_idempotent(works, key="k", store=store) == "done"


async def test_run_idempotent_keeps_the_claim_when_cancelled() -> None:
    store = InMemoryIdempotencyStore()
    started = asyncio.Event()

    async def slow() -> str:
        started.set()
        await asyncio.sleep(10)
        return "never"

    task = asyncio.create_task(run_idempotent(slow, key="k", store=store))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await store.try_claim("k") is False


def test_default_approval_payload_names_the_tool_and_arguments() -> None:
    payload = default_approval_payload("delete_file", {"path": "a.txt"})

    assert payload["tool"] == "delete_file"
    assert payload["args"] == {"path": "a.txt"}
    assert "agent.resume(thread_id, {'action': 'approve'})" in payload["instructions"]


def test_approval_wrapper_preserves_the_tool_contract() -> None:
    @tool(return_direct=True)
    async def publish(text: str) -> str:
        """Publish text."""
        return text

    wrapped = create_tool_approval_wrapper(publish)

    assert isinstance(wrapped, StructuredTool)
    assert wrapped.name == "publish"
    assert wrapped.description == "Publish text."
    assert wrapped.return_direct is True
    assert wrapped.tool_call_schema.model_json_schema()["required"] == ["text"]
