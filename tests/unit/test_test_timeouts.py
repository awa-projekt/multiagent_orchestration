"""The long subprocess timeouts of the suite are upper bounds that scale on a slow machine."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from _helpers import polling

TESTS = Path(__file__).resolve().parents[1]


def test_the_factor_scales_every_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        monkeypatch.setenv("AWA_TEST_TIMEOUT_FACTOR", "3")
        scaled = importlib.reload(polling)
        assert (scaled.SUBPROCESS_TIMEOUT, scaled.SLOW_SUBPROCESS_TIMEOUT) == (360.0, 900.0)
        monkeypatch.setenv("AWA_TEST_TIMEOUT_FACTOR", "not a number")
        assert importlib.reload(polling).SUBPROCESS_TIMEOUT == 120.0
        monkeypatch.setenv("AWA_TEST_TIMEOUT_FACTOR", "0")
        assert importlib.reload(polling).SUBPROCESS_TIMEOUT == 120.0  # never below the default
    finally:
        monkeypatch.delenv("AWA_TEST_TIMEOUT_FACTOR", raising=False)
        importlib.reload(polling)


def test_no_test_hard_codes_a_long_subprocess_timeout() -> None:
    literal = re.compile(r"timeout=(120|300)\b")
    found = [
        f"{path.relative_to(TESTS).as_posix()}:{number}"
        for path in TESTS.rglob("*.py")
        if path.name != Path(__file__).name and "subprocess" in path.read_text("utf-8")
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if literal.search(line)
    ]
    assert found == [], f"use polling.SUBPROCESS_TIMEOUT or SLOW_SUBPROCESS_TIMEOUT: {found}"
