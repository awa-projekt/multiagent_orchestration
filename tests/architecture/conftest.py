"""Prints the report-only guards after the run (they never fail), next to their baseline."""

from __future__ import annotations

import pytest

from architecture import ratchet


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not ratchet.REPORT:
        return
    terminalreporter.section(
        "architecture report (report-only guards, see docs/develop/architecture.md)"
    )
    for key, found, allowed in sorted(ratchet.REPORT):
        trend = (
            "rising"
            if found > allowed
            else "lower, run scripts/arch_baseline.py"
            if found < allowed
            else "ok"
        )
        terminalreporter.write_line(f"{key}: {found} (baseline {allowed}) {trend}")
