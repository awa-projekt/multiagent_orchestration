"""Every "install the `x` extra" message names the right target for the install kind.

An editable folder install is told `pip install -e "<folder>[x]"`; a wheel install is told
the wheel path (never `-e ".[x]"`, which fails without an awa folder).
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from awa._internal.install import Install, install_hint
from awa.errors import OptionalDependencyError

WHEEL = Path("/w/awa_agents-1.0.0-py3-none-any.whl")
FOLDER = Path("/src/awa")

KINDS = {
    "editable": Install("1.0.0", None, FOLDER, None, has_pip=True),
    "wheel": Install("1.0.0", None, None, None, has_pip=True, source=WHEEL),
    "wheel-without-pip": Install("1.0.0", None, None, None, has_pip=False, source=WHEEL),
    "folder-fallback": Install(None, None, None, None, has_pip=True),
}


@pytest.fixture(params=sorted(KINDS))
def kind(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Pretend awa is installed in one of the install kinds."""

    fake = KINDS[request.param]
    monkeypatch.setattr(Install, "detect", classmethod(lambda cls: fake))
    return str(request.param)


def expected(extra: str, kind: str) -> str:
    return KINDS[kind].awa_command(extra)


def test_the_hint_names_the_target_of_the_install_kind(kind: str) -> None:
    hint = install_hint("mcp")
    assert hint == expected("mcp", kind)
    if kind == "editable":
        assert hint.endswith(f'install -e "{FOLDER}[mcp]"') and "(in the awa folder)" not in hint
    elif kind.startswith("wheel"):
        assert f'"{WHEEL}[mcp]"' in hint and "-e" not in hint
        assert ("uv pip install --python" in hint) == (kind == "wheel-without-pip")
    else:
        assert hint.endswith('install -e ".[mcp]" (in the awa folder)')


def blocked(*modules: str) -> Any:
    return patch.dict(sys.modules, dict.fromkeys(modules))


def reimport(name: str, *blocked_modules: str) -> None:
    """Import `name` afresh while `blocked_modules` cannot be imported."""

    importlib.import_module("awa.server._web")  # the guard of the other modules
    saved = sys.modules.pop(name, None)
    try:
        with blocked(*blocked_modules):
            importlib.import_module(name)
    finally:
        if saved is not None:
            sys.modules[name] = saved


def sql_driver() -> str:
    from awa.connectors._local import _driver_hint

    return _driver_hint("mysql")


def psycopg() -> str:
    from awa.persistence.sql import import_psycopg

    with blocked("psycopg"), pytest.raises(OptionalDependencyError) as caught:
        import_psycopg()
    return str(caught.value)


def server_router() -> str:
    with pytest.raises(OptionalDependencyError) as caught:
        reimport("awa.server._router", "fastapi")
    return str(caught.value)


def server_automation() -> str:
    with pytest.raises(OptionalDependencyError) as caught:
        reimport("awa.server._automation", "fastapi")
    return str(caught.value)


def server_web() -> str:
    with pytest.raises(OptionalDependencyError) as caught:
        reimport("awa.server._web", "fastapi")
    return str(caught.value)


def toolkit_module() -> str:
    from awa.toolkits._common import require_module

    with blocked("pypdf"), pytest.raises(OptionalDependencyError) as caught:
        require_module("pypdf", "pypdf", feature="PDF text")
    return str(caught.value)


def mcp_client() -> str:
    from awa._compat import mcp

    with blocked("langchain_mcp_adapters.tools"), pytest.raises(OptionalDependencyError) as caught:
        mcp.require_adapters()
    return str(caught.value)


def mcp_server_sdk() -> str:
    from awa._compat import mcp_server

    mcp_server.load_server_sdk.cache_clear()
    try:
        with blocked("mcp.server.fastmcp"), pytest.raises(OptionalDependencyError) as caught:
            mcp_server.load_server_sdk()
    finally:
        mcp_server.load_server_sdk.cache_clear()
    return str(caught.value)


def otel_bridge() -> str:
    from awa.observability.otel import OpenTelemetrySubscriber

    with blocked("opentelemetry.trace"), pytest.raises(OptionalDependencyError) as caught:
        OpenTelemetrySubscriber()
    return str(caught.value)


def langfuse_exporter() -> str:
    from awa.observability.langfuse import connect_langfuse

    with (
        blocked("opentelemetry.exporter.otlp.proto.http.trace_exporter"),
        pytest.raises(OptionalDependencyError) as caught,
    ):
        connect_langfuse(public_key="pk", secret_key="sk", base_url="http://localhost:1")
    return str(caught.value)


SITES: dict[str, tuple[Callable[[], str], str]] = {
    "sql-driver": (sql_driver, "sql"),
    "psycopg": (psycopg, "postgres"),
    "server-router": (server_router, "server"),
    "server-automation": (server_automation, "server"),
    "server-web": (server_web, "server"),
    "toolkit": (toolkit_module, "files"),
    "mcp-client": (mcp_client, "mcp"),
    "mcp-server": (mcp_server_sdk, "mcp"),
    "otel": (otel_bridge, "otel"),
    "langfuse-exporter": (langfuse_exporter, "langfuse"),
}


@pytest.mark.parametrize("site", sorted(SITES))
def test_every_message_uses_the_shared_helper(site: str, kind: str) -> None:
    trigger, extra = SITES[site]
    message = trigger()
    assert expected(extra, kind) in message
    if kind != "folder-fallback":
        assert 'install -e ".[' not in message


async def test_postgres_backends_use_the_helper(kind: str) -> None:
    from awa.core.settings import Settings
    from awa.persistence.artifacts import open_artifact_repository
    from awa.persistence.checkpointer import open_checkpointer

    dsn = "postgresql://localhost/db"
    with blocked("psycopg"), pytest.raises(OptionalDependencyError) as artifacts:
        async with open_artifact_repository(
            Settings(artifact_backend="postgres", postgres_dsn=dsn)  # type: ignore[arg-type]
        ):
            pass
    assert expected("postgres", kind) in str(artifacts.value)
    with (
        blocked("langgraph.checkpoint.postgres.aio"),
        pytest.raises(OptionalDependencyError) as saver,
    ):
        async with open_checkpointer(
            Settings(checkpointer_backend="postgres", postgres_dsn=dsn)  # type: ignore[arg-type]
        ):
            pass
    assert expected("postgres", kind) in str(saver.value)


def test_toolkit_catalog_problems_use_the_helper(kind: str) -> None:
    from awa.toolkits._common import toolkits_install_hint

    assert toolkits_install_hint() == expected("files", kind)


@pytest.fixture(autouse=True)
def _restore_server_modules() -> Iterator[None]:
    names = [n for n in sys.modules if n.startswith("awa.server")]
    before = {n: sys.modules[n] for n in names}
    yield
    for name in [n for n in sys.modules if n.startswith("awa.server") and n not in before]:
        del sys.modules[name]
    sys.modules.update(before)
