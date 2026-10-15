"""The plugin registry, its consumers, parity with the recorded catalogue and removed routes."""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import json
import sys
import threading
import types
import warnings
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from awa import Agent
from awa.connectors import get_connector
from awa.data import profile
from awa.errors import AwaError, ComponentError
from awa.models import resolve_model
from awa.plugins import KINDS, PluginError, Registry, find, list_plugins, register
from awa.server import ApiKeyAuthenticator, Principal, create_app
from awa.testing import FakeChatModel
from conftest import FakeDist, FakeEntryPoint
from conftest import override_registry as override


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    """A module `fake_providers` whose functions are built-in providers."""

    module = types.ModuleType("fake_providers")
    module.calls = 0  # type: ignore[attr-defined]

    def rows() -> list[tuple[str, object, str]]:
        module.calls += 1  # type: ignore[attr-defined]
        return [("shared", lambda: "builtin", "the built-in")]

    module.rows = rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_providers", module)
    yield module


def _entry_points(monkeypatch: pytest.MonkeyPatch, **groups: list[FakeEntryPoint]) -> None:
    monkeypatch.setattr(
        metadata, "entry_points", lambda *, group: groups.get(group.replace(".", "_"), [])
    )


def test_there_are_exactly_six_kinds_each_with_group_awa_kind() -> None:
    assert len(KINDS) == 6
    assert all(spec.group == f"awa.{kind}" for kind, spec in KINDS.items())


def test_project_beats_entry_points_beats_builtins_with_replace(
    providers: types.ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "plugins.py").write_text(
        "from awa.plugins import register\n"
        "register('models', 'shared', lambda: 'project', replace=True)\n",
        encoding="utf-8",
    )
    registry = Registry(providers={"models": ("fake_providers:rows",)})
    assert registry.load("models", "shared")() == "builtin"
    assert registry.load("models", "shared", project=tmp_path)() == "project"
    sources = [r.source for r in registry.infos("models", project=tmp_path) if r.name == "shared"]
    assert sources == ["project", "builtin"]


def test_a_higher_entry_without_replace_leaves_the_lower_in_use_and_is_listed_shadowed(
    providers: types.ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "plugins.py").write_text(
        "from awa.plugins import register\nregister('models', 'shared', lambda: 'project')\n",
        encoding="utf-8",
    )
    registry = Registry(providers={"models": ("fake_providers:rows",)})
    assert registry.load("models", "shared", project=tmp_path)() == "builtin"
    statuses = {r.source: r.status for r in registry.infos("models", project=tmp_path)}
    assert statuses["project"].startswith("shadowed")


def test_dunder_replace_on_an_entry_point_object_is_honoured_at_find(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory() -> str:
        return "installed"

    factory.__awa_replace__ = True  # type: ignore[attr-defined]
    module = types.ModuleType("fake_plugin_pkg")
    module.factory = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_plugin_pkg", module)
    dist = FakeDist("acme", "1")
    _entry_points(
        monkeypatch,
        awa_models=[FakeEntryPoint("shared", "fake_plugin_pkg:factory", "awa.models", dist)],
    )
    registry = Registry(providers={"models": ()})
    registry.register("models", "shared", lambda: "builtin")
    assert registry.load("models", "shared")() == "installed"


def test_two_distributions_with_one_name_stop_with_an_error_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(
        monkeypatch,
        awa_models=[
            FakeEntryPoint("dup", "os:getcwd", "awa.models", FakeDist("one", "1")),
            FakeEntryPoint("dup", "os:getcwd", "awa.models", FakeDist("two", "1")),
        ],
    )
    registry = Registry(providers={})
    with pytest.raises(PluginError, match="one, two"):
        registry.load("models", "dup")


def test_register_twice_needs_replace() -> None:
    with override() as registry:
        registry.register("models", "m", lambda: 1)
        with pytest.raises(PluginError, match="replace=True"):
            registry.register("models", "m", lambda: 2)
        registry.register("models", "m", lambda: 2, replace=True)
        assert registry.load("models", "m")() == 2


def test_listing_imports_no_plugin_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(
        monkeypatch, awa_models=[FakeEntryPoint("lazy", "never_imported_pkg:f", "awa.models", None)]
    )
    registry = Registry(providers={})
    assert [r.name for r in registry.infos("models")] == ["lazy"]
    assert "never_imported_pkg" not in sys.modules


def test_a_broken_entry_point_is_skipped_with_a_warning_and_the_others_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(
        monkeypatch,
        awa_models=[
            FakeEntryPoint("bad", "missing_pkg_xyz:f", "awa.models", None),
            FakeEntryPoint("good", "os:getcwd", "awa.models", None),
        ],
    )
    registry = Registry(providers={})
    with pytest.warns(RuntimeWarning, match="bad"):
        loaded = registry.loadable("models")
    assert list(loaded) == ["good"]


def test_builtins_do_not_depend_on_entry_point_metadata(
    providers: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entry_points(monkeypatch)
    registry = Registry(providers={"models": ("fake_providers:rows",)})
    assert registry.names("models") == ["shared"]


def test_builtins_keep_the_provider_rows_after_a_registration_replaces_one(
    providers: types.ModuleType,
) -> None:
    registry = Registry(providers={"models": ("fake_providers:rows",)})

    def mine() -> str:
        return "mine"

    registry.register("models", "shared", mine, replace=True)

    [shipped] = registry.builtins("models")
    assert (shipped.name, shipped.target) == ("shared", "fake_providers:rows")
    assert registry.load("models", "shared") is mine
    registry.unregister("models", "shared")
    assert [entry.name for entry in registry.builtins("models")] == ["shared"]


def test_a_failing_builtin_provider_raises_and_the_kind_is_retried(
    providers: types.ModuleType,
) -> None:
    good = providers.rows  # type: ignore[attr-defined]
    state = {"fail": True}

    def flaky() -> list[tuple[str, object, str]]:
        if state["fail"]:
            raise RuntimeError("boom")
        return good()  # type: ignore[no-any-return]

    providers.flaky = flaky  # type: ignore[attr-defined]
    registry = Registry(providers={"models": ("fake_providers:flaky",)})
    with pytest.raises(PluginError, match="boom"):
        registry.names("models")
    state["fail"] = False
    assert registry.names("models") == ["shared"]


def test_bootstrap_runs_once_under_concurrent_first_access(providers: types.ModuleType) -> None:
    registry = Registry(providers={"models": ("fake_providers:rows",)})
    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        registry.names("models")

    threads = [threading.Thread(target=go) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert providers.calls == 1  # type: ignore[attr-defined]


def test_a_provider_that_asks_the_registry_while_booting_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = types.ModuleType("reentrant_providers")
    registry = Registry(providers={"models": ("reentrant_providers:rows",)})

    def rows() -> list[tuple[str, object, str]]:
        registry.names("models")  # the same thread asks again while booting
        return [("one", lambda: 1, "")]

    module.rows = rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "reentrant_providers", module)
    done: list[list[str]] = []
    thread = threading.Thread(target=lambda: done.append(registry.names("models")))
    thread.start()
    thread.join(timeout=20)
    assert done == [["one"]]


def test_override_registry_restores_everything() -> None:
    before = [r.name for r in list_plugins("models")]
    with override():
        register("models", "temporary", lambda: 1)
        assert "temporary" in [r.name for r in list_plugins("models")]
    assert [r.name for r in list_plugins("models")] == before


def test_each_kind_rejects_a_wrong_object_with_a_fix() -> None:
    with override() as registry:
        for kind in KINDS:
            registry.register(kind, "wrong", 42)
            with pytest.raises(PluginError):
                registry.load(kind, "wrong")


PARSER = """
def profile_zzz(path, ctx):
    ctx.profile.role_hint = "from a plugin"
    return ctx.profile
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("AWA_PROJECT_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in [m for m in sys.modules if m.startswith("zzz_")]:
        del sys.modules[name]


def test_parser_from_plugins_py_profiles_a_file(project: Path) -> None:
    (project / "zzz_parser.py").write_text(PARSER, encoding="utf-8")
    (project / "plugins.py").write_text(
        "from awa.plugins import register\n"
        "register('parsers', 'text', 'zzz_parser:profile_zzz', replace=True)\n",
        encoding="utf-8",
    )
    (project / "note.txt").write_text("hello\n", encoding="utf-8")
    [file] = profile(project / "note.txt").files
    assert file.role_hint == "from a plugin"


def test_model_from_plugins_py_resolves_by_its_name(project: Path) -> None:
    (project / "plugins.py").write_text(
        "from awa.plugins import register\n"
        "from awa.testing import FakeChatModel\n"
        "register('models', 'invoice-fast', lambda: FakeChatModel(['ok']))\n",
        encoding="utf-8",
    )
    assert isinstance(resolve_model("invoice-fast"), FakeChatModel)


def test_connector_from_project_folder_replaces_with_replace_true(project: Path) -> None:
    (project / "connectors").mkdir()
    (project / "connectors" / "mine.py").write_text(
        "from awa.connectors import ConnectorSpec\n"
        "MINE = ConnectorSpec(name='mine_crm', title='Mine', category='data', summary='Mine.',\n"
        "                     base_url='https://crm.example')\n",
        encoding="utf-8",
    )
    assert get_connector("mine_crm").summary == "Mine."


def test_a_broken_plugins_py_shows_its_error_when_a_model_is_resolved(project: Path) -> None:
    from awa.plugins import PluginError

    (project / "plugins.py").write_text('raise RuntimeError("boom")\n', encoding="utf-8")
    with pytest.raises(PluginError, match="boom"):
        resolve_model("invoice-fast")


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "plugins_parity.json"


def test_listing_matches_the_recorded_plugin_list() -> None:
    rows = {row.key: row for row in list_plugins() if row.key}
    missing = []
    for kind, name, _source, security in json.loads(FIXTURE.read_text(encoding="utf-8")):
        key = f"{'guard' if kind == 'check' else kind}.{name}"
        if key not in rows:
            missing.append(key)
        else:
            assert rows[key].security is security, key
    assert not missing
    assert {key for key in rows if key.startswith("vector_store.")} == set()


async def test_the_components_routes_answer_404_and_health_keeps_security_off() -> None:
    agent = Agent("echo", description="Echoes.", model=FakeChatModel(["hi"]))
    auth = ApiKeyAuthenticator({"admin-key": Principal("ops", roles={"admin"})})
    app = create_app({"echo": agent}, authenticator=auth)
    admin = {"Authorization": "Bearer admin-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.get("/v1/components", headers=admin)).status_code == 404
        body = {"action": "disable"}
        assert (
            await client.post("/v1/components/parser.pdf", json=body, headers=admin)
        ).status_code in (404, 405)
        assert (await client.get("/v1/health", headers=admin)).json() == {"status": "ok"}


def test_the_web_app_has_no_components_route(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from awa.ui import create_ui_app

    app = create_ui_app(tmp_path, token="t", port=8766, environ={})
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        client.headers["Authorization"] = "Bearer t"
        assert client.get("/api/components").status_code == 404


def test_component_error_resolves_without_a_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolved = getattr(importlib.import_module("awa.errors"), "ComponentError")  # noqa: B009

    assert resolved is ComponentError and issubclass(resolved, AwaError)


INSTALLED_PACKAGE = '''
from langchain_core.tools import tool

from awa.connectors import ConnectorSpec


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}"


crm = ConnectorSpec(
    name="acme_crm", title="Acme CRM", category="data", summary="Acme.",
    base_url="https://crm.example",
)


def fast_model():
    return "a chat model"


def profile_acme(path, ctx):
    return ctx.profile


class Tickets:
    def erase(self, request):
        return []
'''
ENTRY_POINTS_TXT = """\
[awa.tools]
lookup_order = acme_ext:lookup_order
[awa.connectors]
acme_crm = acme_ext:crm
[awa.skills]
acme = acme_ext.skills
[awa.models]
acme-fast = acme_ext:fast_model
[awa.parsers]
acme = acme_ext:profile_acme
[awa.erasure]
tickets = acme_ext:Tickets
"""


def test_a_package_installed_with_entry_points_provides_every_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "acme_ext"
    skill = package / "skills" / "order-lookup"
    skill.mkdir(parents=True)
    (package / "__init__.py").write_text(INSTALLED_PACKAGE, encoding="utf-8")
    (package / "skills" / "__init__.py").write_text("", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: order-lookup\ndescription: Looks up orders before answering.\n---\nCall it.\n",
        encoding="utf-8",
    )
    dist_info = tmp_path / "acme_ext-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: acme-ext\nVersion: 1.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(ENTRY_POINTS_TXT, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        with override():
            rows = {row.kind: row for row in list_plugins() if row.source == "plugin"}
            assert "acme_ext" not in sys.modules
            loaded = {kind: find(kind, rows[kind].name) for kind in KINDS}
    finally:
        for name in [m for m in sys.modules if m.startswith("acme_ext")]:
            del sys.modules[name]

    assert {row.dist for row in rows.values()} == {"acme-ext"}
    assert loaded["tools"][0].invoke({"order_id": "7"}) == "order 7"
    assert loaded["connectors"].name == "acme_crm"
    assert loaded["models"]() == "a chat model"
    assert callable(loaded["parsers"]) and hasattr(loaded["erasure"], "erase")
    assert loaded["skills"].__name__ == "acme_ext.skills"
