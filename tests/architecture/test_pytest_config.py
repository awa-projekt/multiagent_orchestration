"""The test configuration turns deprecations raised from awa modules, and only those, into errors."""

from __future__ import annotations

import re
import tomllib

import pytest

from architecture.scan import REPO


def _deprecation_module_pattern() -> str:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    [entry] = [
        line
        for line in config["tool"]["pytest"]["ini_options"]["filterwarnings"]
        if "DeprecationWarning" in line
    ]
    action, _, category, module = entry.split(":", 3)
    assert (action, category) == ("error", "DeprecationWarning")
    return module


@pytest.mark.parametrize(
    ("module", "error"),
    [
        ("awa", True),
        ("awa.agents.agent", True),
        ("awareness", False),
        ("awa_extra.tools", False),
        ("langchain_core.messages", False),
    ],
)
def test_a_deprecation_is_an_error_only_from_an_awa_module(module: str, error: bool) -> None:
    # `warnings` matches the module regex at the start of the module name.
    assert bool(re.match(_deprecation_module_pattern(), module)) is error
