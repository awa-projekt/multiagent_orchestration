"""The check engine: `Check`, `Result`, `CheckContext`, `run_checks` and `CheckReport`."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Status = Literal["ok", "warn", "fail", "skip"]
"""`ok` works, `warn` works with a caveat, `fail` does not work, `skip` was not run."""

EXIT_OK = 0
"""Nothing failed."""
EXIT_FAILED = 1
"""At least one check failed."""
EXIT_CONFIGURATION = 2
"""The checks could not run: the settings could not be read, or the command was wrong."""


@dataclass(frozen=True)
class Result:
    """The answer of one check.

    Attributes:
        status: `ok`, `warn`, `fail` or `skip`.
        said: What was found, in one plain sentence.
        fix: The one step that fixes a problem (`None` when there is none).
        admin: Text to send to an administrator, when one must act.
        code: A stable advice code (`ms.site_not_granted`, ...) that tests and scripts match.
        data: Facts for scripts (never a secret value).
    """

    status: Status
    said: str
    fix: str | None = None
    admin: str | None = None
    docs: str | None = None
    code: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, said: str, **more: Any) -> Result:
        return cls("ok", said, **more)

    @classmethod
    def warn(cls, said: str, fix: str | None = None, **more: Any) -> Result:
        return cls("warn", said, fix, **more)

    @classmethod
    def fail(cls, said: str, fix: str, **more: Any) -> Result:
        return cls("fail", said, fix, **more)

    @classmethod
    def skip(cls, said: str, fix: str | None = None, **more: Any) -> Result:
        return cls("skip", said, fix, **more)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "said": self.said,
            "fix": self.fix,
            "admin": self.admin,
            "docs": self.docs,
            "code": self.code,
            "data": dict(self.data),
        }


@dataclass
class CheckContext:
    """What the checks of one run share.

    Attributes:
        env: Looks a variable up (`None`: not set); the process environment, then `.env`.
        allow_post: The write checks that may run (`{"teams"}`); every other write is skipped.
        options: Connector HTTP options (a local test server allows plain HTTP to 127.0.0.1).
        shared: Values one check leaves for a later one (a site id, a token's permissions).
    """

    env: Callable[[str], str | None]
    allow_post: frozenset[str] = frozenset()
    no_model: bool = False
    project: Path = field(default_factory=Path.cwd)
    options: dict[str, Any] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)

    def value(self, name: str) -> str | None:
        """The non-empty, stripped value of `name`, or `None`."""

        return (self.env(name) or "").strip() or None


@dataclass(frozen=True)
class Check:
    """One check.

    Attributes:
        id: `group.name`, stable (`microsoft.outlook`).
        run: Does the check; may raise, which is reported as a failure.
        needs: Variables the check needs; when one is not set the check is skipped and says so.
        after: Ids of checks this one depends on; when one failed or was skipped, this one is
            skipped.
        writes: The check changes something (sends a card); it runs only when `allow` is in
            `CheckContext.allow_post`.
    """

    id: str
    group: str
    title: str
    run: Callable[[CheckContext], Result]
    needs: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    writes: bool = False
    allow: str | None = None


@dataclass(frozen=True)
class CheckRun:
    """A check with its result and how long it took."""

    check: Check
    result: Result
    seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.check.id,
            "group": self.check.group,
            "title": self.check.title,
            **self.result.to_dict(),
            "seconds": round(self.seconds, 3),
        }


@dataclass(frozen=True)
class CheckReport:
    """The results of one run, in order."""

    runs: tuple[CheckRun, ...]
    notes: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[CheckRun, ...]:
        return tuple(run for run in self.runs if run.result.status == "fail")

    @property
    def exit_code(self) -> int:
        return EXIT_FAILED if self.failed else EXIT_OK

    def result(self, check_id: str) -> Result:
        """The result of `check_id` (`KeyError` if no check of that id ran)."""

        for run in self.runs:
            if run.check.id == check_id:
                return run.result
        raise KeyError(check_id)

    def admin_message(self) -> str | None:
        """The steps for an administrator collected from every problem line, or `None`."""

        steps = [
            run.result.admin
            for run in self.runs
            if run.result.admin and run.result.status in ("fail", "warn")
        ]
        steps = list(dict.fromkeys(steps))
        if not steps:
            return None
        numbered = "\n\n".join(f"{n}. {step}" for n, step in enumerate(steps, start=1))
        return (
            "Hello, awa (an automation tool) needs these settings for our Microsoft 365 app "
            "registration. They give it access to exactly one mailbox, one SharePoint site and "
            "one Teams channel, nothing more.\n\n" + numbered + "\n\nThank you."
        )

    def to_dict(self) -> dict[str, Any]:
        counts = dict.fromkeys(("ok", "warn", "fail", "skip"), 0)
        for run in self.runs:
            counts[run.result.status] += 1
        return {
            "counts": counts,
            "exit_code": self.exit_code,
            "notes": list(self.notes),
            "checks": [run.to_dict() for run in self.runs],
            "admin_message": self.admin_message(),
        }


def run_checks(
    checks: Iterable[Check],
    ctx: CheckContext,
    *,
    on_result: Callable[[CheckRun], None] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    notes: Sequence[str] = (),
) -> CheckReport:
    """Run `checks` in order and return the report.

    A check is skipped when a variable it `needs` is not set, a check it comes `after` did not
    pass (`ok` or `warn`), or it `writes` and its `allow` word is not in `ctx.allow_post`. A check
    that raises is reported as a failure with the error's text (never with a traceback).

    Args:
        clock: Seconds, for the time each check took.
        notes: Lines the report carries (the hosts used).
    """

    done: dict[str, Status] = {}
    runs: list[CheckRun] = []
    for check in checks:
        started = clock()
        result = _gate(check, ctx, done)
        if result is None:
            try:
                result = check.run(ctx)
            except Exception as error:  # a check must never end the run
                result = Result.fail(
                    f"The check stopped with an error: {type(error).__name__}: {error}",
                    "Run it again; if it repeats, report this line.",
                    code="check.error",
                )
        done[check.id] = result.status
        run = CheckRun(check, result, clock() - started)
        runs.append(run)
        if on_result is not None:
            on_result(run)
    return CheckReport(tuple(runs), notes=tuple(notes))


def _gate(check: Check, ctx: CheckContext, done: Mapping[str, Status]) -> Result | None:
    """Why `check` is not run, or `None` when it runs."""

    for earlier in check.after:
        status = done.get(earlier)
        if status not in ("ok", "warn"):
            reason = {"fail": "failed", "skip": "was skipped"}.get(status or "", "did not run")
            return Result.skip(f"Not checked: {earlier} {reason}.", code="check.depends")
    missing = [name for name in check.needs if ctx.value(name) is None]
    if missing:
        return Result.skip(
            f"Not set up: {', '.join(missing)} not set.",
            f"Set {', '.join(missing)} in .env if you use {check.title}.",
            code="check.not_set_up",
        )
    if check.writes and (check.allow is None or check.allow not in ctx.allow_post):
        return Result.skip(
            "Not run: it would send something.",
            f"Run with --allow-post {check.allow} to send one test message."
            if check.allow
            else None,
            code="check.write_not_allowed",
        )
    return None
