"""What an export reports: the files it wrote or would write, warnings, and why it refused."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from awa.errors import AwaError


class ExportError(AwaError):
    """Raised when an export cannot be written: a destination that is not empty and not an
    earlier export, an unusable project name, or generated code that does not import."""


@dataclass(frozen=True)
class Finding:
    """One thing of a blueprint the export does not carry over.

    Attributes:
        feature: The blueprint feature, as written in the blueprint (`team`, `connectors`).
        advice: What to do about it.
    """

    feature: str
    advice: str


class ExportUnsupportedError(ExportError):
    """Raised, with nothing written, when a blueprint uses features the export does not carry
    over; one finding per feature, so everything to change is listed at once.

    Attributes:
        findings: The refused features with advice.
        source: The blueprint file the findings are about.
    """

    def __init__(self, source: Path | str, findings: list[Finding]) -> None:
        self.source = str(source)
        self.findings = tuple(findings)
        super().__init__(
            "\n".join(self.lines()), details={"features": [f.feature for f in findings]}
        )

    def lines(self) -> list[str]:
        """The findings as `export.unsupported <blueprint>: <feature> - <what to do>` lines."""

        return [
            f"export.unsupported {self.source}: {f.feature} - {f.advice}" for f in self.findings
        ]


@dataclass(frozen=True)
class ExportReport:
    """The result of `export`.

    Attributes:
        out: The destination folder.
        package: The import name of the exported package (`src/<package>`).
        files: The files of the export, relative to `out`, sorted.
        warnings: Settings of the blueprint the export leaves out and the README explains.
        written: Whether the folder was written (`False` for a `check`).
    """

    out: Path
    package: str
    files: tuple[str, ...]
    warnings: tuple[str, ...]
    written: bool
