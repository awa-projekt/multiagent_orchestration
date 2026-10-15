from __future__ import annotations

import asyncio
import fnmatch
import os
import sys
from collections.abc import Callable, Iterator
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest
from pydantic import AliasChoices

import awa.persistence.sql as postgres_setup
from _helpers.clean_tree import untracked_files
from awa.core.settings import Settings
from awa.models import allow_model_requests
from awa.testing import expect_scripts_consumed


def _unprefixed_settings_variables() -> tuple[str, ...]:
    names: set[str] = set()
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        elif isinstance(alias, AliasChoices):
            names.update(choice for choice in alias.choices if isinstance(choice, str))
    return tuple(sorted(names))


# Settings read `.env`, `AWA_*` and the unprefixed provider variables of their aliases. Tests
# must never pick up real credentials, so all of them are removed for every test that is not
# explicitly marked `integration`.
_ENV_VARS_SETTINGS_MAY_READ = _unprefixed_settings_variables()


@pytest.fixture(autouse=True)
def _offline_settings(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("integration") is not None:
        return
    for name in _ENV_VARS_SETTINGS_MAY_READ:
        monkeypatch.delenv(name, raising=False)
    # Every `AWA_*` variable feeds `Settings`; only the opt-in `AWA_TEST_*` switches survive.
    for name in [n for n in os.environ if n.startswith("AWA_") and not n.startswith("AWA_TEST_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    # `get_settings()` is cached: a test that sets a variable reads it, the next starts clean.
    from awa.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _forget_the_loaded_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # `awa` commands export the project `.env`: each test starts and ends without it.
    from awa.core import config

    # The `AWA_TEST_*` switches of this suite are no settings: commands never warn about them.
    switches = frozenset(name for name in os.environ if name.startswith("AWA_TEST_"))
    if switches:
        known = config.known_names
        monkeypatch.setattr(config, "known_names", lambda: known() | switches)
    config.unload_environment()
    yield
    config.unload_environment()


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def _example_files() -> set[str]:
    return {
        path.relative_to(EXAMPLES_DIR).as_posix()
        for path in EXAMPLES_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.fixture(scope="session", autouse=True)
def _examples_tree_untouched() -> Iterator[None]:
    """Tests write into `tmp_path` only: no file may appear in (or vanish from) `examples/`."""

    before = _example_files()
    yield
    after = _example_files()
    assert after == before, (
        f"tests changed examples/: +{sorted(after - before)} -{sorted(before - after)}"
    )


REPOSITORY = Path(__file__).resolve().parents[1]
_UNTRACKED_AT_START = pytest.StashKey[frozenset[str] | None]()


def pytest_sessionstart(session: pytest.Session) -> None:
    if not hasattr(session.config, "workerinput"):  # the controller, or a run without xdist
        session.config.stash[_UNTRACKED_AT_START] = untracked_files(REPOSITORY)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # Tests write into `tmp_path` only: a run that leaves a new untracked file in the checkout fails.
    before = session.config.stash.get(_UNTRACKED_AT_START, None)
    after = untracked_files(REPOSITORY) if before is not None else None
    if before is None or after is None or not (left := sorted(after - before)):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"the tests left new files in the repository: {left}", red=True)
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _provider_strings_build_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # `ModelRegistry.create("openai:...")` refuses to run without any API key; the suite builds
    # such models with a patched `init_chat_model` and no key (test_models_credentials.py
    # turns the check back on).
    monkeypatch.setattr("awa.models.registry._has_no_credentials", lambda kwargs: False)


@pytest.fixture(autouse=True)
def _bounded_postgres_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    # A setup waiting for the advisory lock fails after 30 s instead of hanging the suite.
    monkeypatch.setattr(postgres_setup, "POSTGRES_SETUP_LOCK_TIMEOUT", 30.0)


@pytest.fixture(autouse=True)
def _block_real_model_requests(request: pytest.FixtureRequest) -> Iterator[None]:
    if any(
        request.node.get_closest_marker(marker) is not None
        for marker in ("integration", "model_requests_allowed")
    ):
        yield
        return
    with allow_model_requests(False):
        yield


_CALL_FAILED = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    report = yield
    if report.when == "call" and report.failed:
        item.stash[_CALL_FAILED] = True
    return report


@pytest.fixture(autouse=True)
def _scripts_consumed(request: pytest.FixtureRequest) -> Iterator[None]:
    # A fake model with unreturned responses means the test stopped before the run it scripted.
    if request.node.get_closest_marker("unconsumed_script") is not None:
        yield
        return
    with expect_scripts_consumed() as created:
        yield
        if request.node.stash.get(_CALL_FAILED, False):
            created.clear()  # the test already failed; a leftover script adds nothing


# --------------------------------------------------------------------------- #
# Shared-state backends: the in-memory ones, and the PostgreSQL ones when
# AWA_TEST_POSTGRES_DSN points at a database (tests marked `postgres` are
# skipped otherwise). Tests taking `durable` run once per backend, which
# proves that both follow the same rules.
# --------------------------------------------------------------------------- #

POSTGRES_DSN_VARIABLE = "AWA_TEST_POSTGRES_DSN"
_BACKEND_FIXTURES = frozenset({"backend", "postgres_dsn"})


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"postgres: needs a PostgreSQL database; set {POSTGRES_DSN_VARIABLE} (skipped otherwise)",
    )


DOCS_TESTS = Path(__file__).resolve().parent / "docs"
FAST_LANE_SKIPS = ("examples", "docs")
"""Folders the fast lane leaves to their own lanes (`--fast`)."""


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--fast",
        action="store_true",
        help="the fast lane: leave out tests marked slow and the example and documentation tests",
    )


@pytest.hookimpl(tryfirst=True)  # before `-m` selects by marker
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(DOCS_TESTS):
            item.add_marker(pytest.mark.docs)
    if config.getoption("--fast"):
        slow = [item for item in items if item.get_closest_marker("slow") is not None]
        if slow:
            config.hook.pytest_deselected(items=slow)
            items[:] = [item for item in items if item.get_closest_marker("slow") is None]
    _skip_postgres_without_a_database(items)


def _skip_postgres_without_a_database(items: list[pytest.Item]) -> None:
    if not os.environ.get(POSTGRES_DSN_VARIABLE):
        reason = f"set {POSTGRES_DSN_VARIABLE} to run PostgreSQL tests"
    elif not (_installed("psycopg") and _installed("langgraph.checkpoint.postgres")):
        reason = 'install the "postgres" extra to run PostgreSQL tests'
    else:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    # psycopg's async driver needs a selector event loop on Windows.
    names = set(getattr(item, "fixturenames", ()))
    if sys.platform == "win32" and (
        item.get_closest_marker("postgres") is not None or names & _BACKEND_FIXTURES
    ):
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest.fixture
def postgres_dsn() -> str:
    """The test database; tests using it must be marked `postgres`."""

    dsn = os.environ.get(POSTGRES_DSN_VARIABLE)
    if not dsn:
        pytest.skip(f"set {POSTGRES_DSN_VARIABLE} to run PostgreSQL tests")
    if not (_installed("psycopg") and _installed("langgraph.checkpoint.postgres")):
        pytest.skip('install the "postgres" extra to run PostgreSQL tests')
    return dsn


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def durable() -> DurableBackends:
    return DurableBackends()


class DurableBackends:
    """Creates independent in-memory task boards, blackboards and buses for a test."""

    async def task_board(self, **options: Any) -> Any:
        from awa.tasks import TaskBoard

        return TaskBoard()

    async def blackboard(self, **options: Any) -> Any:
        from awa.messaging import Blackboard

        return Blackboard(**options)

    async def bus(self, **options: Any) -> Any:
        from awa.messaging import InMemoryMessageBus

        return InMemoryMessageBus(**options)


Eventually = Callable[..., Any]


@pytest.fixture
def eventually() -> Eventually:
    """`await eventually(condition)` waits until `condition()` holds (a durable
    backend may take a moment to deliver), failing after `timeout` seconds."""

    async def wait(condition: Callable[[], bool], timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.005)

    return wait


# Test files that exercise an optional extra. When the extra's package is not
# installed (for example after `pip install -e .` without extras), the files are
# not collected and the terminal summary says which extras were missing. A
# pattern starting with "!" exempts files an earlier pattern matched.
_OPTIONAL_TEST_FILES: dict[str, tuple[str, ...]] = {
    "cryptography": ("unit/test_connect_microsoft.py",),
    "fastapi": (
        "unit/test_server_*.py",
        "!unit/test_server_jobs*.py",
        "!unit/test_server_mcp*.py",
        "unit/test_automation_server*.py",
        "unit/test_components.py",
        "examples/test_10_serve_api.py",
    ),
    "mcp": (
        "unit/test_connectors_mcp*.py",
        "unit/test_blueprints_mcp*.py",
        "unit/test_server_mcp*.py",
        "examples/test_09_mcp_tools.py",
    ),
    "opentelemetry.sdk": (
        "unit/test_observability_otel*.py",
        "examples/test_benchmarks_otel_collector.py",
        "examples/test_observability_trace_demo.py",
    ),
    "psycopg": ("integration/*.py", "unit/test_postgres_event_loops.py"),
}
_TESTS_ROOT = Path(__file__).parent
_skipped_for_missing_extras: dict[str, set[str]] = {}


def _installed(module: str) -> bool:
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _matches(relative: str, patterns: tuple[str, ...]) -> bool:
    included = any(fnmatch.fnmatch(relative, p) for p in patterns if not p.startswith("!"))
    return included and not any(
        fnmatch.fnmatch(relative, p[1:]) for p in patterns if p.startswith("!")
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    try:
        relative = collection_path.relative_to(_TESTS_ROOT).as_posix()
    except ValueError:
        return None
    if config.getoption("--fast") and relative.split("/")[0] in FAST_LANE_SKIPS:
        return True
    for module, patterns in _OPTIONAL_TEST_FILES.items():
        if _matches(relative, patterns) and not _installed(module):
            _skipped_for_missing_extras.setdefault(module, set()).add(relative)
            return True
    return None


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _skipped_for_missing_extras:
        return
    terminalreporter.section("optional extras not installed")
    for module, files in sorted(_skipped_for_missing_extras.items()):
        terminalreporter.write_line(f"{module}: skipped {len(files)} test file(s)")
