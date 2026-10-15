"""The upstream seams: helpers and their lint rule, the MCP client seam, asyncio compatibility, and what `awa doctor` reports about upstream releases."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import awa._internal.aio as asyncio_compat
from awa._compat import mcp as compat_mcp
from awa._internal.upstream import upstream_versions
from awa._internal.util import importable
from awa.cli._doctor import run_doctor
from awa.core.settings import Settings
from awa.errors import UpstreamCompatibilityError

ROOT = Path(__file__).resolve().parents[2]


def test_importable_finds_without_importing() -> None:
    assert importable("json")
    assert not importable("awa_no_such_module")
    assert not importable("awa_no_such_package.sub")


def test_versions_report_the_installed_upstream() -> None:
    versions = upstream_versions()
    assert versions["langchain"] and versions["langgraph"]


def test_the_seam_modules_resolve_the_public_objects() -> None:
    from langchain.agents.middleware import ToolCallRequest
    from langgraph.graph.state import CompiledStateGraph

    from awa._internal.upstream import agents, graph, messages, runnables

    assert agents.ToolCallRequest is ToolCallRequest
    assert graph.CompiledStateGraph is CompiledStateGraph
    assert messages.create_tool_call(name="t", args={}, id="1")["name"] == "t"
    assert runnables.var_child_runnable_config.get() is None


def test_the_seam_gives_teams_and_workflows_the_same_upstream_objects() -> None:
    from langchain.agents.middleware import AgentMiddleware, hook_config
    from langchain_core.messages.utils import convert_to_messages
    from langchain_core.runnables.config import ensure_config
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.errors import GraphBubbleUp, GraphInterrupt
    from langgraph.types import Command, Interrupt, StateSnapshot

    from awa._internal.upstream import agents, checkpoint, graph, messages, runnables

    assert agents.AgentMiddleware is AgentMiddleware
    assert agents.hook_config is hook_config
    assert messages.convert_to_messages is convert_to_messages
    assert runnables.ensure_config is ensure_config
    assert checkpoint.BaseCheckpointSaver is BaseCheckpointSaver
    assert (graph.GraphBubbleUp, graph.GraphInterrupt) == (GraphBubbleUp, GraphInterrupt)
    assert (graph.Command, graph.Interrupt, graph.StateSnapshot) == (
        Command,
        Interrupt,
        StateSnapshot,
    )


def test_the_tools_seam_builds_argument_schemas_from_functions() -> None:
    from langchain_core.tools.base import create_schema_from_function

    from awa._internal.upstream import tools

    def lookup(order_id: str, limit: int = 3) -> str:
        """Look up an order."""
        return order_id

    assert tools.create_schema_from_function is create_schema_from_function
    schema = tools.create_schema_from_function("lookup", lookup).model_json_schema()
    assert set(schema["properties"]) == {"order_id", "limit"}
    assert schema["required"] == ["order_id"]


@pytest.mark.skipif(shutil.which("ruff") is None and not importable("ruff"), reason="needs ruff")
@pytest.mark.parametrize(
    ("path", "source", "banned"),
    [
        ("src/awa/tools/_probe.py", "from langchain_core.tools import BaseTool\n", True),
        ("src/awa/_compat/_probe.py", "from langchain_core.tools import BaseTool\n", False),
        (
            "tests/unit/_probe.py",
            "from langgraph.prebuilt.tool_node import ToolCallRequest\n",
            True,
        ),
        ("tests/unit/_probe.py", "from langchain_core.tools import BaseTool\n", False),
        ("tests/upstream/_probe.py", "from langgraph.prebuilt import ToolNode\n", False),
    ],
)
def test_the_lint_rule_bans_direct_upstream_imports(path: str, source: str, banned: bool) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "TID251",
            "--stdin-filename",
            path,
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert ("TID251" in result.stdout) is banned, result.stdout + result.stderr


def test_fallback_sanitizer_gets_the_fallback_model_when_upstream_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """langchain 1.4.3 decides per fallback model which cache settings to strip."""

    from awa._internal.upstream import agents as upstream_agents
    from awa._internal.upstream.agents import sanitize_fallback_request

    seen: list[object] = []

    class Request:
        def override(self, **changes: object) -> dict[str, object]:
            return changes

    def sanitize(request: object, fallback_model: object = None) -> object:
        seen.append(fallback_model)
        return request

    monkeypatch.setattr(upstream_agents, "_sanitize_request_for_fallback", sanitize)
    fallback = object()
    assert sanitize_fallback_request(Request(), fallback) == {"model": fallback}  # type: ignore[arg-type]
    assert seen == [fallback]


def test_the_floors_of_the_upstream_releases_are_installed() -> None:
    """The floors (langchain 1.4.3, langchain-core 1.6.7, langgraph 1.2.14) are installed."""

    from importlib.metadata import version

    def at_least(name: str, floor: tuple[int, ...]) -> bool:
        parts = tuple(int(p) for p in version(name).split(".")[: len(floor)])
        return parts >= floor

    assert at_least("langchain", (1, 4, 3))
    assert at_least("langchain-core", (1, 6, 7))
    assert at_least("langgraph", (1, 2, 14))
    from awa._internal.upstream.agents import AgentState, PrivateStateAttr

    assert AgentState is not None and PrivateStateAttr is not None


def test_doctor_reports_upstream_versions_and_whether_mcp_is_installed(tmp_path: Path) -> None:
    check = run_doctor(settings=Settings(_env_file=None), cwd=tmp_path).check("upstream")
    assert check is not None and check.status == "ok"
    assert "langgraph " in check.summary
    assert check.data["versions"]["langgraph"]
    assert any(line.startswith("MCP: ") for line in check.details)


def test_doctor_fails_when_an_upstream_api_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> dict[str, str | None]:
        raise UpstreamCompatibilityError("awa.tools.hardening needs 'ToolCallRequest'")

    monkeypatch.setattr("awa._internal.upstream.upstream_versions", missing)
    check = run_doctor(settings=Settings(_env_file=None), cwd=tmp_path).check("upstream")
    assert check is not None and check.status == "fail"
    assert "ToolCallRequest" in check.details[0]
    assert check.fix is not None and "langgraph>=1.2.14,<2" in check.fix


def test_the_tool_input_schema_is_read() -> None:
    schema = {"type": "object"}
    assert compat_mcp.tool_input_schema(SimpleNamespace(inputSchema=schema)) == schema
    assert compat_mcp.tool_input_schema(SimpleNamespace()) == {}


async def _return_value() -> int:
    return 42


async def _loop_type() -> type[asyncio.AbstractEventLoop]:
    return type(asyncio.get_running_loop())


def test_run_returns_the_coroutine_result() -> None:
    assert asyncio_compat.run(_return_value()) == 42


async def _subprocess_output() -> bytes:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('hi')", stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    return stdout.strip()


def _policy_type() -> type[Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # deprecated on 3.14+
        return type(asyncio.get_event_loop_policy())


def test_run_uses_the_default_loop_so_subprocesses_work() -> None:
    assert asyncio_compat.run(_subprocess_output()) == b"hi"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific loop selection")
def test_selector_loop_is_used_on_windows_only_when_requested() -> None:
    assert not issubclass(asyncio_compat.run(_loop_type()), asyncio.SelectorEventLoop)
    assert issubclass(
        asyncio_compat.run(_loop_type(), selector_loop=True), asyncio.SelectorEventLoop
    )


@pytest.mark.parametrize("selector_loop", [False, True])
def test_run_does_not_change_the_global_event_loop_policy(selector_loop: bool) -> None:
    before = _policy_type()

    asyncio_compat.run(_return_value(), selector_loop=selector_loop)

    assert _policy_type() is before


async def test_run_inside_a_running_loop_raises_without_leaking_the_coroutine() -> None:
    coro = _return_value()

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio_compat.run(coro)

    # The coroutine was closed, so no "never awaited" warning is emitted later.
    assert coro.cr_frame is None


# --------------------------------------------------------------------------- LoopLocal


def test_loop_local_builds_one_value_per_event_loop() -> None:
    values = asyncio_compat.LoopLocal(object)

    async def twice() -> tuple[object, object]:
        return values.get(), values.get()

    first = asyncio.run(twice())
    second = asyncio_compat.run(twice())

    assert first[0] is first[1]
    assert second[0] is second[1]
    assert first[0] is not second[0]


def test_loop_local_closes_values_when_their_loop_shuts_down() -> None:
    closed: list[object] = []

    async def close(value: object) -> None:
        await asyncio.sleep(0)  # the loop still runs
        closed.append(value)

    values = asyncio_compat.LoopLocal(object, aclose=close)

    async def use() -> object:
        return values.get()

    built = [asyncio.run(use()), asyncio_compat.run(use())]

    assert closed == built
    assert len(values) == 0


def test_loop_local_drops_values_of_loops_closed_without_shutdown() -> None:
    closed: list[object] = []

    async def close(value: object) -> None:
        closed.append(value)

    values = asyncio_compat.LoopLocal(object, aclose=close)

    async def use() -> object:
        return values.get()

    loop = asyncio.new_event_loop()
    stale = loop.run_until_complete(use())
    loop.close()  # without shutdown_asyncgens(): the value cannot be closed any more
    fresh = asyncio.run(use())

    assert fresh is not stale
    assert closed == [fresh]
    assert len(values) == 0


def test_loop_local_ignores_close_errors() -> None:
    async def close(value: object) -> None:
        raise OSError("cannot close")

    values = asyncio_compat.LoopLocal(object, aclose=close)

    async def use() -> None:
        values.get()

    asyncio.run(use())
    asyncio.run(use())


def test_loop_local_values_are_separate_per_thread() -> None:
    import threading

    values = asyncio_compat.LoopLocal(object)
    seen: list[object] = []

    async def use() -> None:
        seen.append(values.get())

    threads = [threading.Thread(target=asyncio.run, args=(use(),)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(value) for value in seen}) == 3


async def test_loop_local_peek_does_not_build() -> None:
    values = asyncio_compat.LoopLocal(object)

    assert values.peek() is None
    built = values.get()
    assert values.peek() is built


def test_loop_local_get_needs_a_running_loop() -> None:
    values = asyncio_compat.LoopLocal(object)

    assert values.peek() is None
    with pytest.raises(RuntimeError):
        values.get()


def test_loop_local_locks_work_across_consecutive_runs() -> None:
    locks = asyncio_compat.LoopLocal(asyncio.Lock)

    async def contend() -> None:
        async def hold() -> None:
            async with locks.get():
                await asyncio.sleep(0)

        await asyncio.gather(hold(), hold(), hold())  # waiters bind the lock to the loop

    asyncio.run(contend())
    asyncio.run(contend())


async def test_refuse_running_loop_names_the_call_and_its_alternative() -> None:
    with pytest.raises(RuntimeError, match=r"^X.run_sync\(\) cannot .* use `await x.run\(\)`$"):
        asyncio_compat.refuse_running_loop("X.run_sync()", "await x.run()")


def test_refuse_running_loop_passes_without_a_loop() -> None:
    asyncio_compat.refuse_running_loop("X.run_sync()", "await x.run()")
