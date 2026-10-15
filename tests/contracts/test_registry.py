"""The completeness test: every backend of a contracted protocol runs that protocol's suite."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
import warnings
from typing import Any

import pytest

import awa
from awa.testing.contracts import AdapterContract
from contracts import (
    test_automation_stores,
    test_people_directory,
    test_persistence_stores,
    test_server_stores,
)
from contracts.registry import BACKENDS, SUITES

SUITE_MODULES = (
    test_automation_stores,
    test_people_directory,
    test_persistence_stores,
    test_server_stores,
)


def awa_classes() -> list[type]:
    """Every concrete class that a module of awa defines (modules of missing extras are skipped)."""

    found: dict[str, type] = {}
    names = ["awa", *(m.name for m in pkgutil.walk_packages(awa.__path__, "awa."))]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for name in names:
            try:
                module = importlib.import_module(name)
            except Exception:  # noqa: BLE001 - an optional extra that is not installed
                continue
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if cls.__module__.startswith("awa.") and not inspect.isabstract(cls):
                    found[f"{cls.__module__}.{cls.__qualname__}"] = cls
    return list(found.values())


def suite_classes(suite: type) -> list[type]:
    """The test classes of `suite`, one per backend."""

    found: list[type] = []
    for module in SUITE_MODULES:
        found += [
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, suite) and cls is not suite
        ]
    return found


def is_callable_alias(protocol: Any) -> bool:
    """A backend protocol written as `Callable[[...], ...]` (such as `Notifier`), not as a class."""

    return typing.get_origin(protocol) is not None and not isinstance(protocol, type)


def name_of(protocol: Any) -> str:
    """The dotted name of a backend protocol; an alias is named by the registry entry that holds it."""

    if is_callable_alias(protocol):
        from awa.automation import notify

        found = [n for n, v in vars(notify).items() if v is protocol]
        return f"{notify.__name__}.{found[0]}" if found else repr(protocol)
    return f"{protocol.__module__}.{protocol.__name__}"


def is_subtype(cls: type, protocol: Any) -> bool:
    """`issubclass`, also for a protocol that is not runtime-checkable (explicit bases count).

    For a `Callable` alias, a class qualifies when its `__call__` takes the alias's argument types.
    """

    if is_callable_alias(protocol):
        wanted = typing.get_args(protocol)[0]
        call = cls.__dict__.get("__call__")
        if call is None:
            return False
        try:
            hints = typing.get_type_hints(call)
        except Exception:  # noqa: BLE001 - unresolvable annotations: not this protocol
            return False
        params = [p for p in inspect.signature(call).parameters if p != "self"]
        return len(params) >= len(wanted) and all(
            hints.get(p) is t for p, t in zip(params, wanted, strict=False)
        )
    try:
        return issubclass(cls, protocol)
    except TypeError:
        return protocol in cls.__mro__


def implements(cls: type, protocol: Any) -> bool:
    return (
        cls is not protocol
        and not getattr(cls, "_is_protocol", False)
        and is_subtype(cls, protocol)
    )


@pytest.mark.parametrize("protocol", list(BACKENDS), ids=lambda p: name_of(p).rsplit(".", 1)[1])
def test_every_backend_that_implements_the_protocol_is_registered(protocol: Any) -> None:
    missing = [
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in awa_classes()
        if implements(cls, protocol) and cls not in BACKENDS[protocol]
    ]

    assert not missing, (
        f"{missing} implement {name_of(protocol)} but are not in tests/contracts/registry.py: "
        "register them and run the suite (rule R7)"
    )


@pytest.mark.parametrize("protocol", list(BACKENDS), ids=lambda p: name_of(p).rsplit(".", 1)[1])
def test_the_suite_runs_exactly_the_registered_backends(protocol: Any) -> None:
    run = [cls.adapter for cls in suite_classes(SUITES[protocol])]

    assert all(a is not None and is_subtype(a, protocol) for a in run)
    assert sorted(a.__name__ for a in run if a) == sorted(a.__name__ for a in BACKENDS[protocol])


@pytest.mark.parametrize("protocol", list(BACKENDS), ids=lambda p: name_of(p).rsplit(".", 1)[1])
def test_a_known_deviation_names_a_test_of_the_suite_and_has_a_pin(protocol: Any) -> None:
    for cls in suite_classes(SUITES[protocol]):
        tests = {n for n, _ in inspect.getmembers(cls, inspect.isfunction) if n.startswith("test_")}
        unknown = set(cls.known_deviations) - tests  # type: ignore[attr-defined]
        assert not unknown, f"{cls.__name__}.known_deviations names no test: {sorted(unknown)}"
        if cls.known_deviations:  # type: ignore[attr-defined]
            assert any(n.startswith("test_pin_") for n in tests), (
                f"{cls.__name__} has known deviations but no test_pin_* test that records "
                "what the backend does instead"
            )


def test_every_suite_is_an_adapter_contract() -> None:
    assert all(issubclass(suite, AdapterContract) for suite in SUITES.values())
