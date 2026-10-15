"""Which FakeGraph rules a self-test proves (`test_every_rule_has_a_test` checks the set)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

COVERED_RULES: set[str] = set()
"""Ids of rules that some self-test declared with `proves`."""


def proves(*rule_ids: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a test as the proof of rules."""

    def mark(test: Callable[..., Any]) -> Callable[..., Any]:
        COVERED_RULES.update(rule_ids)
        return test

    return mark
