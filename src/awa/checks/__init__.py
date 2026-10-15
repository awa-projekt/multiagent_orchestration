"""Checks: is everything set up? One engine for `awa check`, `awa connect` and scripts.

Preview: its API may still change.

A `Check` asks one small question of a setup; its `Result` says `ok`, `warn`, `fail` or `skip`
in a plain sentence, with the fix and, when an administrator must act, the text to send them.
The one group, `microsoft`, only reads, except a Teams test card that runs when allowed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awa.checks._engine import (
    EXIT_CONFIGURATION,
    EXIT_FAILED,
    EXIT_OK,
    Check,
    CheckContext,
    CheckReport,
    CheckRun,
    Result,
    Status,
    run_checks,
)
from awa.checks._env import CHECK_SETTINGS, project_env
from awa.errors import ConfigurationError

__stability__ = "preview"

GROUPS = ("microsoft",)
"""The check groups (`all` runs every one)."""


def run_groups(
    groups: Iterable[str],
    *,
    allow_post: Iterable[str] = (),
    no_model: bool = False,
    project: Path | None = None,
    on_result: Callable[[CheckRun], None] | None = None,
    connector_options: Mapping[str, Any] | None = None,
) -> CheckReport:
    """Run the checks of `groups` (`all` for every group) and return the report.

    Args:
        allow_post: Write checks that may run (`teams`: one test card).
        no_model: Make no model call (the models are only built).
        connector_options: HTTP options of the live checks (a local test server passes
            `allow_http` and `allow_private_networks`; production passes none).

    Raises:
        ConfigurationError: If a group is not one of `GROUPS` or `all`.

    Example:
        ```python
        report = run_groups(["microsoft"], no_model=True)
        for run in report.runs:
            print(run.result.status, run.check.id, run.result.said)
        ```
    """

    from awa.checks.microsoft import CHECKS as checks

    folder = (project or Path.cwd()).resolve()
    for group in groups:
        if group not in (*GROUPS, "all"):
            raise ConfigurationError(
                f"unknown check group {group!r}; choose from {', '.join(GROUPS)} or all"
            )
    ctx = CheckContext(
        project_env(folder),
        allow_post=frozenset(allow_post),
        no_model=no_model,
        project=folder,
        options=dict(connector_options or {}),
    )
    return run_checks(checks, ctx, on_result=on_result, notes=_live_notes(ctx))


def _live_notes(ctx: CheckContext) -> tuple[str, ...]:
    authority = ctx.value("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com"
    graph = ctx.value("MS_GRAPH_URL") or "https://graph.microsoft.com"
    return (f"live checks against your tenant: sign-in host {authority}, Graph {graph}",)


def save_report(
    report: CheckReport, project: Path, *, now: Callable[[], float] = time.time
) -> Path:
    """Write `report` (never a `.env` value) to `.awa/checks/<timestamp>.json`; return the file."""

    stamp = datetime.fromtimestamp(now(), UTC).strftime("%Y%m%dT%H%M%S%fZ")
    folder = project / ".awa" / "checks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}.json"
    path.write_text(
        json.dumps(report.to_dict(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


__all__ = [
    "CHECK_SETTINGS",
    "EXIT_CONFIGURATION",
    "EXIT_FAILED",
    "EXIT_OK",
    "GROUPS",
    "Check",
    "CheckContext",
    "CheckReport",
    "CheckRun",
    "Result",
    "Status",
    "run_checks",
    "run_groups",
    "save_report",
]
