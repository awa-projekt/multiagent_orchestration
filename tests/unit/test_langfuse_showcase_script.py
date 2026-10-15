"""`scripts/langfuse_showcase.py`: the installed awa, and the Langfuse keys from the environment."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "langfuse_showcase.py"


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("langfuse_showcase", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_does_not_put_the_source_tree_on_the_path() -> None:
    assert '"src"' not in SCRIPT.read_text(encoding="utf-8")


def test_missing_keys_are_named_without_values() -> None:
    module = script()
    assert module.missing_langfuse_keys({}) == [
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
    ]
    assert module.missing_langfuse_keys(
        {"LANGFUSE_PUBLIC_KEY": "pk-lf-x", "LANGFUSE_SECRET_KEY": " ", "LANGFUSE_HOST": "http://h"}
    ) == ["LANGFUSE_SECRET_KEY"]


def test_a_run_without_keys_stops_with_a_setup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    assert script().main([]) == 2
    assert (
        "set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL in"
        in capsys.readouterr().err
    )
