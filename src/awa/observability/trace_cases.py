"""Re-exports the test cases from process runs of `awa.automation.trace_cases`, loaded on first use.

Importing `awa.observability` therefore never loads the automation package.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa.automation.trace_cases import JUDGE_CRITERIA, ExportedCases, add_cases, cases_from_runs

_HOMES = homes(
    {"awa.automation.trace_cases": "JUDGE_CRITERIA ExportedCases add_cases cases_from_runs"}
)

__all__ = ["JUDGE_CRITERIA", "ExportedCases", "add_cases", "cases_from_runs"]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
