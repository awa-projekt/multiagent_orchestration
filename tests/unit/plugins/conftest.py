"""Shared fixtures of the plugin tests: a fake installed distribution and a clean registry."""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from awa.plugins import REGISTRY, Registry
from awa.plugins._kinds import Kind

PACKAGE = "awa_test_plugin_pkg"


@dataclass(frozen=True)
class FakeDist:
    name: str
    version: str


@dataclass(frozen=True)
class FakeEntryPoint:
    name: str
    value: str
    group: str
    dist: FakeDist | None

    def load(self) -> object:
        module, _, attribute = self.value.partition(":")
        import importlib

        obj: object = importlib.import_module(module)
        for part in filter(None, attribute.split(".")):
            obj = getattr(obj, part)
        return obj


ENTRY_POINTS = {
    "awa.tools": [
        FakeEntryPoint("lookup_order", f"{PACKAGE}.tools:lookup_order", "awa.tools", None),
        FakeEntryPoint("broken", f"{PACKAGE}.tools:missing", "awa.tools", None),
        FakeEntryPoint("crm", f"{PACKAGE}.tools:crm_tools", "awa.tools", None),
    ],
    "awa.skills": [FakeEntryPoint("acme", f"{PACKAGE}.skills", "awa.skills", None)],
}


@contextmanager
def override_registry(providers: Mapping[Kind, Sequence[str]] | None = None) -> Iterator[Registry]:
    """Use an empty registry (with these providers) inside the block and restore the real one."""

    saved = (REGISTRY._entries, REGISTRY._builtins, REGISTRY._ready, REGISTRY._providers)
    REGISTRY._entries, REGISTRY._builtins, REGISTRY._ready = {}, {}, set()
    REGISTRY._providers = providers if providers is not None else {}
    try:
        yield REGISTRY
    finally:
        REGISTRY._entries, REGISTRY._builtins, REGISTRY._ready, REGISTRY._providers = saved


@pytest.fixture
def registry() -> Iterator[None]:
    """An empty plugin registry (no built-in providers) for the length of the test."""

    with override_registry():
        yield


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: None) -> Iterator[Path]:
    """A plugin package on `sys.path` and entry points declared by distribution `Acme_AWA`."""

    package = tmp_path / PACKAGE
    skill = package / "skills" / "order-lookup"
    skill.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "skills" / "__init__.py").write_text("", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: order-lookup\ndescription: Looks up orders before answering.\n---\n"
        "Always call lookup_order first.\n",
        encoding="utf-8",
    )
    (package / "tools.py").write_text(
        textwrap.dedent(
            '''
            from langchain_core.tools import tool

            @tool
            def lookup_order(order_id: str) -> str:
                """Look up an order."""
                return f"order {order_id}"

            @tool
            def delete_customer(customer_id: str) -> str:
                """Delete a customer."""
                return customer_id

            crm_tools = [delete_customer]
            '''
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    dist = FakeDist("Acme_AWA", "1.4.0")
    declared = {
        group: [FakeEntryPoint(e.name, e.value, e.group, dist) for e in entries]
        for group, entries in ENTRY_POINTS.items()
    }

    def entry_points(*, group: str) -> list[FakeEntryPoint]:
        return declared.get(group, [])

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    yield package
    for name in [m for m in sys.modules if m.startswith(PACKAGE)]:
        del sys.modules[name]
