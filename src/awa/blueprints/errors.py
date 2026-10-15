"""Blueprint validation results and the error raised for invalid blueprints."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from awa.errors import AwaError

Severity = Literal["error", "warning"]
"""How serious a `BlueprintIssue` is: errors block `build`, warnings do not."""


@dataclass(frozen=True)
class BlueprintIssue:
    """One finding of `Blueprint.check`: an `"error"` (cannot be built) or a `"warning"`, at a path like `"agents[1].tools[0]"`."""

    severity: Severity
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.path}: {self.message}"


class BlueprintError(AwaError):
    """Raised when a blueprint cannot be parsed, validated, built or designed; `issues` holds the errors."""

    def __init__(self, message: str, issues: Iterable[BlueprintIssue] = ()) -> None:
        self.issues: tuple[BlueprintIssue, ...] = tuple(issues)
        details = "".join(f"\n  - {issue.path}: {issue.message}" for issue in self.issues)
        super().__init__(f"{message}{details}")


__all__ = ["BlueprintError", "BlueprintIssue", "Severity"]
