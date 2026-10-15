"""The public exception surface and the machine-readable error metadata."""

from __future__ import annotations

import importlib
import pickle
import pkgutil
import re

import pytest

import awa
import awa.errors as public_exceptions
from awa.errors import (
    AwaError,
    ConfigurationError,
    MissingConfigurationError,
    ModelProviderError,
    OptionalDependencyError,
    RateLimitExceededError,
    RunCancelledError,
    RunTimeoutError,
    SystemBusyError,
    ToolTimeoutError,
    UsageLimitExceededError,
)


def _library_errors() -> set[type[AwaError]]:
    for module in pkgutil.walk_packages(awa.__path__, "awa."):
        if module.name == "awa.__main__":
            continue
        try:
            importlib.import_module(module.name)
        except (ModuleNotFoundError, OptionalDependencyError):
            continue  # a module of an optional extra that is not installed
    found: set[type[AwaError]] = set()
    pending: list[type[AwaError]] = [AwaError]
    while pending:
        cls = pending.pop()
        if cls.__module__.startswith("awa."):
            found.add(cls)
        pending.extend(cls.__subclasses__())
    return found


def test_awa_exceptions_exports_every_library_error() -> None:
    exported = {getattr(public_exceptions, name) for name in public_exceptions.__all__}

    assert _library_errors() <= exported
    assert all(issubclass(cls, AwaError) for cls in exported)


def test_core_exceptions_all_lists_every_class_it_defines() -> None:
    defined = {
        name
        for name, value in vars(public_exceptions).items()
        if isinstance(value, type) and value.__module__ == public_exceptions.__name__
    }

    assert set(public_exceptions._DEFINED) == defined


def test_common_errors_are_importable_from_awa() -> None:
    for name in ("AwaError", "AgentRunError", "UsageLimitExceededError", "BlueprintError"):
        assert name in awa.__all__
        assert getattr(awa, name) is getattr(public_exceptions, name)
    for name in ("MaxStepsExceededError", "StructuredOutputError", "TopologyError"):
        assert name not in awa.__all__
        assert hasattr(public_exceptions, name)


def _exported() -> list[type[AwaError]]:
    return [getattr(public_exceptions, name) for name in public_exceptions.__all__]


def test_every_exported_error_has_a_unique_well_formed_code() -> None:
    codes = [cls.code for cls in _exported()]

    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"awa(\.[a-z0-9_]+)+", code) for code in codes)


def test_codes_are_derived_from_the_class_name() -> None:
    assert AwaError.code == "awa.error"
    assert UsageLimitExceededError.code == "awa.usage_limit_exceeded"
    assert public_exceptions.MCPError.code == "awa.mcp"
    assert ConfigurationError.code == "awa.configuration"


def test_a_subclass_may_set_its_own_code() -> None:
    class QuotaError(AwaError):
        code = "billing.quota"

    class DerivedError(QuotaError):
        pass

    assert QuotaError.code == "billing.quota"
    assert DerivedError.code == "awa.derived"


def test_timeouts_and_busy_systems_are_retryable() -> None:
    assert ToolTimeoutError.retryable
    assert SystemBusyError.retryable
    assert not ConfigurationError.retryable
    assert not AwaError.retryable


def test_details_are_optional_structured_and_picklable() -> None:
    assert dict(AwaError("plain").details) == {}

    error = ConfigurationError("bad backend", details={"setting": "store_backend"})

    assert str(error) == "bad backend"
    assert error.details == {"setting": "store_backend"}
    restored = pickle.loads(pickle.dumps(error))
    assert restored.details == {"setting": "store_backend"}
    assert str(restored) == "bad backend"


@pytest.mark.parametrize(
    "error",
    [
        RunTimeoutError("too slow", run_id="run_1", elapsed=2.5, limit=2.0),
        RunCancelledError("stopped", run_id="run_1", reason="user", cancelled_by="alice"),
        ModelProviderError("down", kind="server_error", status=503, retryable=True),
        RateLimitExceededError("slow down", retry_after=3.0),
    ],
)
def test_errors_with_keyword_arguments_survive_pickling(error: AwaError) -> None:
    restored = pickle.loads(pickle.dumps(error))

    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert restored.details == error.details
    assert vars(restored) == vars(error)
    assert restored.retryable == error.retryable


def test_configuration_errors_are_value_errors() -> None:
    assert issubclass(ConfigurationError, ValueError)
    assert issubclass(MissingConfigurationError, ConfigurationError)
    with pytest.raises(ValueError, match="needs"):
        raise MissingConfigurationError("AWA_POSTGRES_DSN needs a value")


def test_mixed_in_builtin_bases_still_work() -> None:
    error = ToolTimeoutError("slow")

    assert isinstance(error, TimeoutError)
    assert str(error) == "slow"
