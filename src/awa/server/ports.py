"""Backend protocols of the data endpoints: where uploads and builds are kept, how a build runs, time and ids.

`UploadStore` (backend `FileUploadStore`), `BuildStore` (`FileBuildStore`), `BuildEngine`
(`awa.blueprints.build_project` by default), `ClockFn` and `IdFn`. One contract suite
(`awa.testing.contracts`) runs against every backend, your own too. Not-found errors do not tell
unknown, expired and another tenant's apart, on purpose. Protocols grow by addition only.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, Protocol

if TYPE_CHECKING:
    from awa.data import Limits, UploadScanner
    from awa.data._model import FileKind
    from awa.server._config import ServedSystem

FORMAT = 1
"""Version of the stored `upload.json` and `build.json`; a file of another version is not read."""

ClockFn = Callable[[], datetime]
"""Returns the current time as an aware UTC `datetime`."""

IdFn = Callable[[str], str]
"""Returns a new unguessable id for a kind of object: `ids("up")` gives `up_3f9a...`. Ids must
only hold letters, digits, `_` and `-`, because stores use them as folder names."""

Emit = Callable[[str, Mapping[str, Any]], None]
"""What a build engine calls to report progress: an event type such as `design.acceptance` and
its JSON data. Data with `stage` (`profile`, `design`, `evaluate`, `repair`, `write`), `round`,
`pass_rate` or `usage` (the model calls and tokens of the build so far) updates the build's status."""

RUNNING = frozenset({"queued", "profiling", "designing", "evaluating", "repairing", "writing"})
"""Build statuses of a build that is being worked on."""

TERMINAL = frozenset({"completed", "failed", "cancelled"})
"""Build statuses that never change again."""


@dataclass(frozen=True)
class UploadedFile:
    """One accepted file. `id` is `f1`, `f2`, ... in sorted name order, the id a profile gives it."""

    id: str
    name: str
    size: int
    kind: str
    sha256: str


@dataclass(frozen=True)
class UploadRecord:
    """An accepted upload: who owns it, what it holds and until when it is kept."""

    id: str
    tenant: str | None
    principal: str
    created_at: datetime
    expires_at: datetime
    files: tuple[UploadedFile, ...]

    @property
    def bytes(self) -> int:
        """The size of all files together."""

        return sum(file.size for file in self.files)

    def to_json(self) -> str:
        """The record as the text a store keeps (`upload.json`)."""

        return json.dumps(
            {
                "format": FORMAT,
                "id": self.id,
                "tenant": self.tenant,
                "principal": self.principal,
                "created_at": self.created_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "bytes": self.bytes,
                "files": [vars(file) for file in self.files],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> UploadRecord:
        """Read `to_json` text.

        Raises:
            ValueError: A damaged record, or one written in another format version.
        """

        try:
            data = json.loads(text)
            if data["format"] != FORMAT:
                raise ValueError(f"upload record format {data['format']!r} is not {FORMAT}")
            return cls(
                id=data["id"],
                tenant=data["tenant"],
                principal=data["principal"],
                created_at=datetime.fromisoformat(data["created_at"]),
                expires_at=datetime.fromisoformat(data["expires_at"]),
                files=tuple(UploadedFile(**file) for file in data["files"]),
            )
        except (KeyError, TypeError) as error:
            raise ValueError("damaged upload record") from error


class UploadSession(Protocol):
    """One upload being received. Everything written is discarded unless `commit` succeeds."""

    def open(self, name: str) -> BinaryIO:
        """A writer for one received file under a safe, unique name (`awa.data.safe_name`).

        Raises:
            DataError: The name is not safe, or is used already in this session.
        """
        ...

    def count(self, amount: int) -> None:
        """Count `amount` more received bytes against the tenant's quota.

        Raises:
            QuotaExceededError: The tenant would store more than its quota.
        """
        ...

    def commit(
        self,
        limits: Limits,
        *,
        kinds: Collection[FileKind] | None = None,
        scanner: UploadScanner | None = None,
    ) -> UploadRecord:
        """Check the received files (`awa.data.ingest`) and keep them as a new upload.

        Raises:
            DataError: A file is refused (`TypeMismatchError`, `UnsupportedTypeError`,
                `TooLargeError`, `TooManyFilesError` or `UnsafeArchiveError`); nothing is kept.
        """
        ...


class UploadStore(Protocol):
    """Uploads of every tenant. A tenant never sees another tenant's ids."""

    def begin(
        self, tenant: str | None, principal: str, *, quota: int
    ) -> AbstractContextManager[UploadSession]:
        """Start receiving an upload; leaving the block discards what was not committed.

        Args:
            quota: The most bytes the tenant may keep stored in this store, counting uploads
                being received at the same time.

        Raises:
            QuotaExceededError: No room left, or too many uploads of the tenant at once.
        """
        ...

    def get(self, tenant: str | None, upload_id: str, *, expired_ok: bool = False) -> UploadRecord:
        """The upload `upload_id` of `tenant`.

        Raises:
            UploadNotFoundError: Unknown, expired (unless `expired_ok`), or of another tenant.
        """
        ...

    def open_files(self, record: UploadRecord) -> AbstractContextManager[Path]:
        """A folder holding the files of `record` under their names, to read, never to write.

        Raises:
            UploadNotFoundError: The upload is gone.
        """
        ...

    def delete(self, record: UploadRecord) -> None:
        """Remove the upload; deleting one that is gone is not an error."""
        ...

    def purge(self, keep: Collection[str] = ()) -> int:
        """Remove expired uploads (not those in `keep`) and failed receives; return how many."""
        ...

    def used_bytes(self, tenant: str | None) -> int:
        """The bytes `tenant` has stored here (uploads being received count too)."""
        ...


@dataclass(frozen=True)
class BuildRecord:
    """The stored state of a build (what `GET /v1/builds/{id}` shows, plus who owns it)."""

    id: str
    tenant: str | None
    principal: str
    created_at: datetime
    status: str = "queued"
    stage: str | None = None
    round: int = 0
    pass_rate: float | None = None
    finished_at: datetime | None = None
    request: Mapping[str, Any] = field(default_factory=dict)
    questions: tuple[Mapping[str, Any], ...] = ()
    answers: Mapping[str, str] = field(default_factory=dict)
    report: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    bytes: int = 0

    def changed(self, **changes: Any) -> BuildRecord:
        """A copy with `changes` applied."""

        return replace(self, **changes)

    def to_json(self) -> str:
        """The record as the text a store keeps (`build.json`)."""

        return json.dumps(
            {
                "format": FORMAT,
                "id": self.id,
                "tenant": self.tenant,
                "principal": self.principal,
                "created_at": self.created_at.isoformat(),
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "status": self.status,
                "stage": self.stage,
                "round": self.round,
                "pass_rate": self.pass_rate,
                "request": dict(self.request),
                "questions": [dict(q) for q in self.questions],
                "answers": dict(self.answers),
                "report": None if self.report is None else dict(self.report),
                "error": None if self.error is None else dict(self.error),
                "usage": dict(self.usage),
                "bytes": self.bytes,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> BuildRecord:
        """Read `to_json` text.

        Raises:
            ValueError: A damaged record, or one written in another format version.
        """

        try:
            data = json.loads(text)
            if data["format"] != FORMAT:
                raise ValueError(f"build record format {data['format']!r} is not {FORMAT}")
            finished = data["finished_at"]
            return cls(
                id=data["id"],
                tenant=data["tenant"],
                principal=data["principal"],
                created_at=datetime.fromisoformat(data["created_at"]),
                finished_at=datetime.fromisoformat(finished) if finished else None,
                status=data["status"],
                stage=data["stage"],
                round=data["round"],
                pass_rate=data["pass_rate"],
                request=data["request"],
                questions=tuple(data["questions"]),
                answers=data["answers"],
                report=data["report"],
                error=data["error"],
                usage=data["usage"],
                bytes=data["bytes"],
            )
        except (KeyError, TypeError) as error:
            raise ValueError("damaged build record") from error


class BuildStore(Protocol):
    """Builds of every tenant: their records and their project folders."""

    def save(self, record: BuildRecord) -> None:
        """Create or replace the record of `record.id` (for `record.tenant`)."""
        ...

    def get(self, tenant: str | None, build_id: str) -> BuildRecord:
        """The build of `tenant`.

        Raises:
            BuildNotFoundError: Unknown, expired, or of another tenant.
        """
        ...

    def list_builds(
        self, tenant: str | None, *, owner: str | None = None, limit: int = 50
    ) -> list[BuildRecord]:
        """The builds of `tenant` (only those of principal `owner` when given), newest first."""
        ...

    def project_dir(self, tenant: str | None, build_id: str) -> Path:
        """The folder a build writes its project to and the API exports from (created).

        Raises:
            BuildNotFoundError: The build is not stored (`save` it first).
        """
        ...

    def delete(self, tenant: str | None, build_id: str) -> None:
        """Remove the record and the project folder; removing a missing build is not an error."""
        ...

    def purge(self, keep: Collection[str] = ()) -> int:
        """Remove finished builds past the retention (not those in `keep`); return how many."""
        ...

    def used_bytes(self, tenant: str | None) -> int:
        """The bytes `tenant`'s builds occupy (the `bytes` of their records)."""
        ...

    def unfinished(self) -> list[BuildRecord]:
        """Every build of every tenant that has not finished (running, or waiting for answers)."""
        ...


@dataclass(frozen=True)
class BuildSpec:
    """What a `BuildEngine` is asked to build.

    Attributes:
        data: A folder with the uploaded files; read it, never write to it.
        out_dir: The existing project folder (may hold files of an earlier attempt).
        notes: What must happen with a file (`file name -> text`).
        model: A model name allowed by `DataOptions.models`, or `None` for the default.
        mask: What the model may see of the data: `none`, `placeholders` or `drop`.
        answers: Answers to earlier questions (`question id -> answer`).
    """

    description: str
    data: Path
    out_dir: Path
    notes: Mapping[str, str]
    model: str | None
    rounds: int
    target: float
    max_requests: int
    mask: Literal["none", "placeholders", "drop"]
    guidance: str
    answers: Mapping[str, str]


@dataclass(frozen=True)
class Question:
    """A question the build needs the user to answer; `evidence` names profile ids it is based on."""

    id: str
    text: str
    options: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildOutcome:
    """What a `BuildEngine` returns: questions to answer, or the report of a written project.

    With `questions` the build waits for answers and nothing else is set.
    """

    questions: tuple[Question, ...] = ()
    passed: bool = False
    pass_rate: float = 0.0
    rounds: int = 0
    stopped: str = ""
    target: float = 1.0
    summary: str = ""
    flaky: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)


SystemFactory = Callable[[Path], "ServedSystem"]
"""Turns the project folder of a finished build into the system the server runs (and the rules
for serving it). The default reads `blueprint.yaml` and `tools.py` the way `awa serve` does."""


class BuildEngine(Protocol):
    """Designs, evaluates and writes a project. It is awaited as one coroutine and cancelled to stop.

    Raises (besides any `AwaError`): `BlueprintError` for a design that cannot be made valid and
    `UsageLimitExceededError` for used-up limits; the server maps both to `awa.build.*` errors.
    """

    def __call__(self, spec: BuildSpec, emit: Emit) -> Awaitable[BuildOutcome]: ...


__all__ = [
    "FORMAT",
    "RUNNING",
    "TERMINAL",
    "BuildEngine",
    "BuildOutcome",
    "BuildRecord",
    "BuildSpec",
    "BuildStore",
    "ClockFn",
    "Emit",
    "IdFn",
    "Question",
    "SystemFactory",
    "UploadRecord",
    "UploadSession",
    "UploadStore",
    "UploadedFile",
]
