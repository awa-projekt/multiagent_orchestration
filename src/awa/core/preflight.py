"""Pre-flight checks: is the configuration complete before a command starts working?

Each check returns `Problem`s that name variables and the fix, never a value; only
`check_store` opens a connection.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from awa.core.config import env_value


@dataclass(frozen=True)
class Problem:
    """One thing that keeps a command from working.

    Attributes:
        code: A short identifier (`model_key`, `env_missing`, `store_unreachable`, ...).
        fatal: Whether the command must not start (`False`: a warning).
    """

    code: str
    what: str
    fix: str
    fatal: bool = True

    def line(self) -> str:
        """`problem: <what> -- fix: <fix>` (or `warning: ...`)."""

        kind = "problem" if self.fatal else "warning"
        return f"{kind}: {self.what} -- fix: {self.fix}"


@dataclass
class PreflightReport:
    """The problems the checks found."""

    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether no problem is fatal."""

        return not any(problem.fatal for problem in self.problems)

    def render(self) -> str:
        """One line per problem."""

        return "\n".join(problem.line() for problem in self.problems)

    def to_json(self) -> dict[str, Any]:
        """The report as JSON data."""

        return {"ok": self.ok, "problems": [asdict(problem) for problem in self.problems]}


def check_env(names: Mapping[str, str]) -> list[Problem]:
    """Whether each variable is set; `names` maps each to what needs it."""

    return [
        Problem(
            "env_missing",
            f"{name} is not set (needed by {needed_by})",
            f"set {name} in .env (awa env set {name}) or in the environment",
        )
        for name, needed_by in names.items()
        if not (env_value(name) or "").strip()
    ]


def check_api_key_env(name: str | None) -> list[Problem]:
    """Whether the variable `--api-key-env` names is set."""

    if not name:
        return []
    return check_env({name: "--api-key-env"})


def check_store(
    dsn: str | None, *, timeout: float = 5.0, connect: Callable[[str, float], None]
) -> list[Problem]:
    """Whether the PostgreSQL database `dsn` answers within `timeout` seconds (`None` checks nothing).

    Args:
        connect: Opens and closes one connection; raises on failure.
    """

    if not dsn:
        return []
    from awa.errors import OptionalDependencyError

    try:
        connect(dsn, timeout)
    except OptionalDependencyError:
        return [
            Problem(
                "store_driver",
                "the PostgreSQL driver is not installed",
                'install the postgres extra: pip install "awa-agents[postgres]"',
            )
        ]
    except Exception as exc:  # noqa: BLE001 - any failure means the database cannot be used
        kind = type(exc).__name__
        return [
            Problem(
                "store_unreachable",
                f"the database in AWA_POSTGRES_DSN (or --dsn) does not answer ({kind})",
                "start the database (awa setup services up --profile db) or correct the DSN; "
                "awa doctor --check-postgres shows the reason",
            )
        ]
    return []


__all__ = ["PreflightReport", "Problem", "check_api_key_env", "check_env", "check_store"]
