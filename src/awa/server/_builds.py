"""`BuildService`: start, follow, answer and cancel builds, and the events they report."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from awa._internal.logging import get_logger
from awa.blueprints.errors import BlueprintError
from awa.errors import UsageLimitExceededError
from awa.server._data_kit import directory_size
from awa.server._serialize import jsonable
from awa.server.errors import (
    BuildConflictError,
    BuildDesignInvalidError,
    BuildUsageLimitError,
    InvalidRequestError,
    QuotaExceededError,
    ServiceUnavailableError,
    describe_error,
)
from awa.server.ports import (
    RUNNING,
    TERMINAL,
    BuildEngine,
    BuildOutcome,
    BuildRecord,
    BuildSpec,
    BuildStore,
    ClockFn,
    IdFn,
    Question,
    UploadStore,
)

_log = get_logger("server.data")

EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_TERMINAL_EVENTS = {
    "build.completed": "completed",
    "build.failed": "failed",
    "build.cancelled": "cancelled",
}
_OWN_EVENTS = {*_TERMINAL_EVENTS, "build.started", "build.question", "build.resumed"}
_PREFIX_STATUS = (
    ("data.profile", "profiling"),
    ("design.", "designing"),
    ("eval.", "evaluating"),
    ("repair.", "repairing"),
    ("write.", "writing"),
)
_STAGE_STATUS = {
    "profile": "profiling",
    "design": "designing",
    "evaluate": "evaluating",
    "repair": "repairing",
    "write": "writing",
}
_MAX_TEXT = 2000
_MAX_EVENT_BYTES = 64 * 1024
_MAX_ITEMS = 200
_LIVE_KEEP = timedelta(hours=1)
_MAINTAIN_EVERY = timedelta(seconds=60)
_CANCEL_WAIT = 5.0
_MAX_EXPORT_FILES = 2000

Auditor = Callable[..., None]
"""`audit(event, actor=..., tenant=..., **data)`: records what happened to the audit log."""


_NOT_EXPORTED = frozenset({".venv", "__pycache__", ".git", ".awa"})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
# A SQLite database in WAL mode keeps unwritten changes in its -wal file.
_DATABASE_SUFFIXES = tuple(
    base + journal for base in (".db", ".sqlite", ".sqlite3") for journal in ("", "-wal", "-shm")
)
_PEOPLE_FILE = "config/people.yaml"


def hidden(relative: Path) -> bool:
    """Whether a project-relative file is never shown or exported.

    Environment files, keys, databases outside `data/`, run state, caches and
    `config/people.yaml`; databases under `data/` are exported, so the project runs on its data.
    Names are compared without case, as Windows and macOS find the files.
    """

    parts = tuple(part.casefold() for part in relative.parts)
    lower = parts[-1]
    return (
        (lower.startswith(".env") and lower != ".env.example")
        or lower.endswith(_SECRET_SUFFIXES)
        or (lower.endswith(_DATABASE_SUFFIXES) and parts[0] != "data")
        or bool(_NOT_EXPORTED.intersection(parts))
        or "/".join(parts) == _PEOPLE_FILE
    )


def project_files(folder: Path, *, limit: int | None = None) -> list[Path]:
    """The regular, not `hidden` files below `folder`, sorted and relative to it.

    Raises:
        TooManyFilesError: If there are more than `limit` such files.
    """

    base = folder.resolve()
    found: list[Path] = []
    for path in sorted(folder.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file() or hidden(path.relative_to(folder)):
            continue
        if path.resolve().is_relative_to(base):
            found.append(path.relative_to(folder))
        if limit is not None and len(found) > limit:
            from awa.data import TooManyFilesError

            raise TooManyFilesError(
                f"The project has more than {limit} files to export; remove files "
                "it no longer needs or put them in a zip archive"
            )
    return found


def export_files(folder: Path) -> list[Path]:
    """`project_files` for a download or export: an export is never cut short.

    Raises:
        TooManyFilesError: If the project has more than 2000 such files.
    """

    return project_files(folder, limit=_MAX_EXPORT_FILES)


@dataclass(frozen=True)
class BuildRequest:
    """What a caller asks for (already validated by the endpoint)."""

    description: str
    upload_id: str
    notes: Mapping[str, str]
    model: str | None
    rounds: int
    target: float
    max_requests: int
    mask: Literal["none", "placeholders", "drop"]
    guidance: str

    def as_json(self) -> dict[str, Any]:
        """The request as stored in the build record."""

        return {**asdict(self), "notes": dict(self.notes)}


def snapshot(record: BuildRecord) -> dict[str, Any]:
    """A build as `GET /v1/builds/{id}` shows it (no links; the endpoint adds them)."""

    body: dict[str, Any] = {
        "build_id": record.id,
        "status": record.status,
        "stage": record.stage,
        "round": record.round,
        "pass_rate": record.pass_rate,
        "upload_id": record.request.get("upload_id"),
        "description": record.request.get("description"),
        "created_at": record.created_at.isoformat(),
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "usage": dict(record.usage),
    }
    if record.status == "needs_input":
        body["questions"] = [dict(question) for question in record.questions]
    if record.report is not None:
        body["report"] = dict(record.report)
    if record.error is not None:
        body["error"] = dict(record.error)
    return body


def _question_json(question: Question) -> dict[str, Any]:
    return {
        "id": question.id,
        "text": question.text,
        "options": list(question.options),
        "evidence": list(question.evidence),
    }


def _failure(exc: BaseException) -> BaseException:
    """The `awa.build.*` error that stands for `exc`, if there is one."""

    if isinstance(exc, UsageLimitExceededError) and not isinstance(exc, BuildUsageLimitError):
        return BuildUsageLimitError(str(exc))
    if isinstance(exc, BlueprintError):
        issues = [
            {"code": issue.severity, "path": issue.path, "message": issue.message}
            for issue in exc.issues
        ]
        summary = str(exc).splitlines()[0] if str(exc) else "the design is not valid"
        return BuildDesignInvalidError(summary, details={"issues": issues})
    return exc


class _Live:
    """The runtime side of one build: events for replay, who waits for them, the task."""

    def __init__(self, record: BuildRecord, service: BuildService) -> None:
        self.record = record
        self.service = service
        self.events: deque[dict[str, Any]] = deque(maxlen=service._event_buffer)
        self.seq = 0
        self.waiters: list[asyncio.Future[None]] = []
        self.task: asyncio.Task[None] | None = None
        self.loop = asyncio.get_running_loop()
        self.scrub: Callable[[str], str] = lambda text: text

    def emit(self, event: str, data: Mapping[str, Any]) -> None:
        """Report an event (safe to call from another thread)."""

        try:
            here: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is self.loop:
            self._append(event, data)
        else:
            self.loop.call_soon_threadsafe(self._append, event, data)

    def engine_emit(self, event: str, data: Mapping[str, Any]) -> None:
        """What the engine calls: events of the engine only, never the build's own."""

        if EVENT_TYPE.fullmatch(event) and event not in _OWN_EVENTS:
            self.emit(event, data)
        else:
            _log.debug("Build %s: an engine event %r was ignored", self.record.id, event)

    def _append(self, event: str, data: Mapping[str, Any]) -> None:
        service = self.service
        self.seq += 1
        body = _clean(jsonable(dict(data)), self.scrub)
        size = len(json.dumps(body))
        if size > _MAX_EVENT_BYTES:  # one event never fills the replay buffer on its own
            body = {"truncated": True, "bytes": size}
        self.events.append(
            {
                "seq": self.seq,
                "ts": service._clock().isoformat(),
                "type": event,
                "build_id": self.record.id,
                "data": body,
            }
        )
        update: dict[str, Any] = {}
        status = _TERMINAL_EVENTS.get(event) or _status_of(event, body)
        if status is not None and status != self.record.status:
            update["status"] = status
        if isinstance(body.get("stage"), str):
            update["stage"] = body["stage"]
        if isinstance(body.get("round"), int) and not isinstance(body.get("round"), bool):
            update["round"] = body["round"]
        if isinstance(body.get("pass_rate"), int | float):
            update["pass_rate"] = float(body["pass_rate"])
        if isinstance(body.get("usage"), dict):  # the model calls and tokens of the build so far
            update["usage"] = body["usage"]
        if update and self.record.status not in TERMINAL:  # a late event changes no ended build
            self.record = self.record.changed(**update)
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)
        self.waiters.clear()

    async def wait(self, timeout: float | None) -> None:
        """Wait for the next event (or `timeout` seconds)."""

        waiter: asyncio.Future[None] = self.loop.create_future()
        self.waiters.append(waiter)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter, timeout)
        finally:
            # A follower that timed out or went away leaves nothing behind: a build that waits
            # for answers for days keeps one waiter per open stream, not one per ping.
            with contextlib.suppress(ValueError):
                self.waiters.remove(waiter)


def _status_of(event: str, data: Mapping[str, Any]) -> str | None:
    for prefix, status in _PREFIX_STATUS:
        if event.startswith(prefix):
            return status
    stage = data.get("stage")
    return _STAGE_STATUS.get(stage) if isinstance(stage, str) else None


def _clean(value: Any, scrub: Callable[[str], str], depth: int = 0) -> Any:
    """JSON data with text cut to 2000 characters, at most 200 items per list, paths scrubbed."""

    if isinstance(value, str):
        return scrub(value)[:_MAX_TEXT]
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth > 8:
        return scrub(str(value))[:_MAX_TEXT]
    if isinstance(value, Mapping):
        return {
            str(key): _clean(item, scrub, depth + 1)
            for key, item in list(value.items())[:_MAX_ITEMS]
        }
    if isinstance(value, list | tuple):
        return [_clean(item, scrub, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    return scrub(str(value))[:_MAX_TEXT]


def _scrubber(*folders: Path) -> Callable[[str], str]:
    """Replace the server's own folders in a text (so storage layout never reaches a client)."""

    names = {
        text: label
        for folder, label in zip(folders, ("<data>", "<project>"), strict=False)
        for text in (str(folder), folder.as_posix())
    }

    def scrub(text: str) -> str:
        for path, label in names.items():
            text = text.replace(path, label)
        return text

    return scrub


class BuildService:
    """Builds of all tenants: `max_builds` per tenant at once, `event_buffer` events for replay.

    It holds what a store cannot: the running task, the events kept for replay (`Last-Event-ID`,
    an hour after the end) and who waits for the next one. After a restart a build that was
    running is `failed` (`recover`).
    """

    def __init__(
        self,
        *,
        store: BuildStore,
        uploads: UploadStore,
        engine: BuildEngine,
        clock: ClockFn,
        ids: IdFn,
        max_builds: int,
        event_buffer: int,
        audit: Auditor,
    ) -> None:
        self._store = store
        self._uploads = uploads
        self._engine = engine
        self._clock = clock
        self._ids = ids
        self._max_builds = max_builds
        self._event_buffer = event_buffer
        self._audit = audit
        self._live: dict[tuple[str, str], _Live] = {}
        self._last_maintained: datetime | None = None

    def start(self, tenant: str | None, principal: str, request: BuildRequest) -> BuildRecord:
        """Create a build and start it.

        Raises:
            UploadNotFoundError: The upload is unknown or of another tenant.
            QuotaExceededError: The tenant already runs its limit of builds.
        """

        self.maintain()
        self._uploads.get(tenant, request.upload_id)
        self._check_capacity(tenant)
        record = BuildRecord(
            id=self._ids("bld"),
            tenant=tenant,
            principal=principal,
            created_at=self._clock(),
            request=request.as_json(),
        )
        self._store.save(record)
        live = self._track(record)
        live.task = asyncio.create_task(self._run(live), name=f"awa-build-{record.id}")
        self._audit(
            "build_started",
            actor=principal,
            tenant=tenant,
            build_id=record.id,
            upload_id=request.upload_id,
        )
        return record

    def answer(self, tenant: str | None, build_id: str, answers: Mapping[str, str]) -> BuildRecord:
        """Answer the questions of a build that waits for input; it resumes at once.

        Raises:
            BuildConflictError: The build does not wait for answers.
            InvalidRequestError: An answer names no question, or is not one of the options.
            QuotaExceededError: The tenant already runs its limit of builds.
        """

        record = self.get(tenant, build_id)
        if record.status != "needs_input":
            raise BuildConflictError(
                f"Build {build_id!r} is {record.status}, not waiting for answers"
            )
        questions = {question["id"]: question for question in record.questions}
        for question_id, value in answers.items():
            question = questions.get(question_id)
            if question is None:
                raise InvalidRequestError(f"There is no question {question_id!r} to answer")
            options = question.get("options") or []
            if options and value not in options:
                raise InvalidRequestError(
                    f"The answer to {question_id!r} must be one of: {', '.join(options)}"
                )
        self._check_capacity(tenant)
        record = record.changed(
            status="queued",
            stage=None,
            round=0,
            pass_rate=None,
            questions=(),
            answers={**record.answers, **answers},
        )
        self._store.save(record)
        live = self._track(record)
        live.record = record
        live.emit("build.resumed", {"answered": sorted(answers)})
        live.task = asyncio.create_task(self._run(live), name=f"awa-build-{record.id}")
        self._audit(
            "build_answered",
            actor=record.principal,
            tenant=tenant,
            build_id=build_id,
            answered=len(answers),
        )
        return live.record

    async def cancel(self, tenant: str | None, build_id: str, *, actor: str) -> BuildRecord:
        """Stop a build. A finished build is returned unchanged.

        Raises:
            BuildNotFoundError: Unknown, expired, or of another tenant.
        """

        record = self.get(tenant, build_id)
        if record.status in TERMINAL:
            return record
        live = self._track(record)
        if live.task is not None and not live.task.done():
            live.task.cancel()
            await asyncio.wait({live.task}, timeout=_CANCEL_WAIT)
        elif live.record.status not in TERMINAL:  # waiting for answers: nothing is running
            self._finish(live, "build.cancelled", {})
        self._audit("build_cancelled", actor=actor, tenant=tenant, build_id=build_id)
        return live.record

    async def close(self) -> None:
        """Cancel every running build (the server shuts down)."""

        tasks = [live.task for live in self._live.values() if live.task and not live.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def get(self, tenant: str | None, build_id: str) -> BuildRecord:
        """The build, with what is happening now.

        Raises:
            BuildNotFoundError: Unknown, expired, or of another tenant.
        """

        live = self._live.get(self._key(tenant, build_id))
        return live.record if live is not None else self._store.get(tenant, build_id)

    def list_builds(
        self, tenant: str | None, *, owner: str | None, limit: int
    ) -> list[BuildRecord]:
        """The builds of the tenant (of `owner` only when given), newest first."""

        stored = {
            record.id: record
            for record in self._store.list_builds(tenant, owner=owner, limit=limit)
        }
        for (_, build_id), live in self._live.items():
            if build_id in stored:
                stored[build_id] = live.record
        return sorted(stored.values(), key=lambda record: record.created_at, reverse=True)

    async def events(
        self, tenant: str | None, build_id: str, after: int, ping: float | None
    ) -> AsyncIterator[dict[str, Any] | None]:
        """The events after `after`, then new ones until the terminal event.

        Yields `None` after `ping` quiet seconds (a keep-alive); a build whose events are gone
        yields one event that states its state.
        """

        record = self.get(tenant, build_id)
        live = self._live.get(self._key(tenant, build_id))
        if live is None:
            if after < 1:
                yield _state_event(record)
            return
        cursor = after
        while True:
            for event in [e for e in live.events if e["seq"] > cursor]:
                cursor = event["seq"]
                yield event
            if live.record.status in TERMINAL and cursor >= live.seq:
                return
            if any(e["seq"] > cursor for e in live.events):
                continue  # events arrived while the last ones were handed out
            before = live.seq
            await live.wait(ping)
            if live.seq == before:
                yield None

    def recover(self) -> int:
        """Mark builds running when the server stopped as failed (at startup); return how many."""

        marked = 0
        for record in self._store.unfinished():
            if record.status in RUNNING:
                error = describe_error(
                    ServiceUnavailableError(
                        "The server restarted while the build was running; start it again"
                    )
                ).body(None)
                error.pop("request_id")
                self._store.save(
                    record.changed(status="failed", finished_at=self._clock(), error=error)
                )
                marked += 1
        return marked

    def needed_uploads(self) -> set[str]:
        """The uploads a build that has not finished still needs."""

        return {str(record.request.get("upload_id")) for record in self._store.unfinished()}

    def maintain(self) -> None:
        """Remove expired uploads and builds, at most once a minute (what no build still needs)."""

        now = self._clock()
        if self._last_maintained is not None and now - self._last_maintained < _MAINTAIN_EVERY:
            return
        self._last_maintained = now
        unfinished = self._store.unfinished()
        self._uploads.purge(keep={str(r.request.get("upload_id")) for r in unfinished})
        self._store.purge(keep={record.id for record in unfinished})
        stale = [
            key
            for key, live in self._live.items()
            if live.record.finished_at and now - live.record.finished_at > _LIVE_KEEP
        ]
        for key in stale:
            del self._live[key]

    def _key(self, tenant: str | None, build_id: str) -> tuple[str, str]:
        return (tenant or "", build_id)

    def _track(self, record: BuildRecord) -> _Live:
        key = self._key(record.tenant, record.id)
        live = self._live.get(key)
        if live is None:
            live = self._live[key] = _Live(record, self)
        return live

    def _check_capacity(self, tenant: str | None) -> None:
        running = sum(
            1
            for live in self._live.values()
            if live.record.tenant == tenant and live.record.status in RUNNING
        )
        if running >= self._max_builds:
            raise QuotaExceededError(
                f"{running} builds are already running; at most {self._max_builds} may run at "
                "once. Retry when one has finished.",
                retry_after=10.0,
            )

    async def _run(self, live: _Live) -> None:
        record = live.record
        tenant = record.tenant
        request = record.request
        live.emit(
            "build.started",
            {"stage": "profile", "upload_id": request["upload_id"], "answers": len(record.answers)},
        )
        try:
            upload = self._uploads.get(tenant, request["upload_id"], expired_ok=True)
            out = self._store.project_dir(tenant, record.id)
            with self._uploads.open_files(upload) as data:
                live.scrub = _scrubber(data, out)
                spec = BuildSpec(
                    description=request["description"],
                    data=data,
                    out_dir=out,
                    notes=request["notes"],
                    model=request["model"],
                    rounds=request["rounds"],
                    target=request["target"],
                    max_requests=request["max_requests"],
                    mask=request["mask"],
                    guidance=request["guidance"],
                    answers=dict(live.record.answers),
                )
                outcome = await self._engine(spec, live.engine_emit)
            if outcome.questions:
                self._ask(live, outcome.questions)
            else:
                self._complete(live, outcome, out)
        except asyncio.CancelledError:
            self._finish(live, "build.cancelled", {})
            raise
        except Exception as exc:  # noqa: BLE001 - every failure ends the build, none escapes the task
            self._fail(live, exc)

    def _ask(self, live: _Live, questions: Sequence[Question]) -> None:
        listed = tuple(_question_json(question) for question in questions)
        live.record = live.record.changed(questions=listed)
        live.emit("build.question", {"questions": [dict(q) for q in listed]})
        live.record = live.record.changed(status="needs_input")
        self._store.save(live.record)

    def _complete(self, live: _Live, outcome: BuildOutcome, out: Path) -> None:
        report = {
            "summary": live.scrub(outcome.summary),
            "passed": outcome.passed,
            "pass_rate": outcome.pass_rate,
            "rounds": outcome.rounds,
            "stopped": outcome.stopped,
            "target_pass_rate": outcome.target,
            "flaky": list(outcome.flaky),
            "failing": list(outcome.failing),
            "assumptions": [live.scrub(item) for item in outcome.assumptions],
            "files": [path.as_posix() for path in project_files(out)],
        }
        live.record = live.record.changed(
            pass_rate=outcome.pass_rate,
            round=outcome.rounds,
            usage=dict(outcome.usage),
            report=report,
            bytes=directory_size(out),
        )
        self._finish(live, "build.completed", {"report": report})

    def _fail(self, live: _Live, exc: BaseException) -> None:
        mapped = _failure(exc)
        info = describe_error(mapped)
        if info.internal:
            _log.error("Build %s failed: %s", live.record.id, type(exc).__name__, exc_info=exc)
        error = {**info.body(None), "message": live.scrub(info.message)}
        error.pop("request_id")
        if details := getattr(mapped, "details", None):
            error["details"] = _clean(jsonable(dict(details)), live.scrub)
        live.record = live.record.changed(error=error)
        self._finish(live, "build.failed", {"error": error})

    def _finish(self, live: _Live, event: str, data: Mapping[str, Any]) -> None:
        live.emit(event, data)
        live.record = live.record.changed(finished_at=self._clock())
        self._store.save(live.record)
        self._audit(
            "build_finished",
            actor=live.record.principal,
            tenant=live.record.tenant,
            build_id=live.record.id,
            status=live.record.status,
        )


def _state_event(record: BuildRecord) -> dict[str, Any]:
    """One event that states what a build is (its events are gone, the record is not)."""

    terminal = {status: event for event, status in _TERMINAL_EVENTS.items()}
    if record.status in terminal:
        event = terminal[record.status]
    elif record.status == "needs_input":
        event = "build.question"
    else:
        event = "build.state"
    return {
        "seq": 1,
        "ts": (record.finished_at or record.created_at).isoformat(),
        "type": event,
        "build_id": record.id,
        "data": snapshot(record),
    }
