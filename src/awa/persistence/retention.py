"""Retention, and the parts erasure and retention share: subject links, reports, participants.

`apply_retention` applies a `RetentionPolicy` to every backend passed (run it on a schedule).
`SubjectIndex` links threads and artifacts to a person for `erase_subject`. Data outside this
package is handled by participants: `erasure` plugins (`awa.plugins`), classes with a `category`
and a `create(...)` class method returning the participant for the given backends, or `None`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from awa._internal.upstream.checkpoint import BaseCheckpointSaver, BaseStore
from awa._internal.util import DurationLike, to_timedelta
from awa.errors import ConfigurationError
from awa.persistence.checkpointer import checkpoint_time, postgres_thread_activity
from awa.persistence.namespaces import (
    PRIVACY_NAMESPACE,
    delete_items,
    iter_namespace_items,
    iter_prefix_items,
    namespace_label,
)
from awa.persistence.runlog import RunLog, Scope, SystemScope

if TYPE_CHECKING:
    from awa.automation.store import RunStore
    from awa.memory.long_term import AgentMemory
    from awa.observability.trace_store import TraceStore

SUBJECT_METADATA_KEY = "awa.subject"
"""Checkpoint metadata key naming the data subject of a thread.

LangGraph copies string values of `config["metadata"]` into every checkpoint
of a run, so a graph invoked with
`config={"configurable": {"thread_id": t}, "metadata": {"awa.subject": "u-42"}}`
is found by `erase_subject("u-42", checkpointer=...)`.
"""

SUBJECT_INDEX_NAMESPACE: tuple[str, ...] = (*PRIVACY_NAMESPACE, "subjects")
"""Namespace prefix of `SubjectIndex`; one namespace per subject below it."""

LinkKind = Literal["thread", "artifact"]
"""What a `SubjectIndex` entry points to."""

_KINDS: tuple[LinkKind, ...] = ("thread", "artifact")


def _key(kind: LinkKind, value: str) -> str:
    return f"{kind}:{value}"


@dataclass(frozen=True, slots=True)
class SubjectLinks:
    """Everything linked to one subject in a `SubjectIndex`: thread ids and artifact refs."""

    threads: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.threads or self.artifacts)

    def keys(self) -> list[str]:
        """The links as `kind:value` keys (`thread:t-1`, `artifact:doc-1`)."""

        return [
            *(_key("thread", value) for value in self.threads),
            *(_key("artifact", value) for value in self.artifacts),
        ]


class SubjectIndex:
    """Links data subjects to the threads and artifacts holding their data, idempotently.

    Stored under `("awa", "privacy", "subjects", <subject>)`; pass the same store to
    `erase_subject(store=...)`.

    Example:
        ```python
        index = SubjectIndex(store)
        await index.link("u-42", threads=["thread-7"], artifacts=["contract:C-17"])
        ```
    """

    def __init__(self, store: BaseStore) -> None:
        self._store = store

    @property
    def store(self) -> BaseStore:
        """The underlying store."""

        return self._store

    @staticmethod
    def _entries(threads: Iterable[str], artifacts: Iterable[str]) -> list[tuple[LinkKind, str]]:
        return [
            *(("thread", value) for value in threads),
            *(("artifact", value) for value in artifacts),
        ]

    async def link(
        self, subject_id: str, *, threads: Iterable[str] = (), artifacts: Iterable[str] = ()
    ) -> None:
        """Record that the given threads and artifacts hold `subject_id`'s data.

        Raises:
            ConfigurationError: If `subject_id` or one of the ids is empty.
        """

        namespace = self._namespace(subject_id)
        now = datetime.now(UTC).isoformat()
        entries = self._entries(threads, artifacts)
        for kind, value in entries:
            if not value:
                raise ConfigurationError(f"{kind} ids linked to a subject must not be empty")
        for kind, value in entries:
            await self._store.aput(
                namespace, _key(kind, value), {"kind": kind, "id": value, "linked_at": now}
            )

    async def links(self, subject_id: str) -> SubjectLinks:
        """Return everything linked to `subject_id`, each kind sorted."""

        found: dict[str, list[str]] = {kind: [] for kind in _KINDS}
        async for item in iter_namespace_items(self._store, self._namespace(subject_id)):
            kind, value = item.value.get("kind"), item.value.get("id")
            if kind in found and isinstance(value, str):
                found[kind].append(value)
        return SubjectLinks(
            threads=tuple(sorted(found["thread"])), artifacts=tuple(sorted(found["artifact"]))
        )

    async def unlink(
        self, subject_id: str, *, threads: Iterable[str] = (), artifacts: Iterable[str] = ()
    ) -> None:
        """Remove the given links; unknown links are ignored."""

        namespace = self._namespace(subject_id)
        for kind, value in self._entries(threads, artifacts):
            await self._store.adelete(namespace, _key(kind, value))

    async def forget(self, subject_id: str) -> int:
        """Remove every link of `subject_id` and return how many there were."""

        items = [
            item async for item in iter_namespace_items(self._store, self._namespace(subject_id))
        ]
        return len(await delete_items(self._store, items))

    async def forget_thread(self, thread_id: str, *, dry_run: bool = False) -> int:
        """Remove every link to `thread_id` and return how many there were; `dry_run` only counts."""

        key = _key("thread", thread_id)
        stale = [
            item
            async for item in iter_prefix_items(
                self._store,
                SUBJECT_INDEX_NAMESPACE,
                filter={"kind": "thread", "id": thread_id},
                predicate=lambda item: item.key == key,
            )
        ]
        return len(await delete_items(self._store, stale, dry_run=dry_run))

    def _namespace(self, subject_id: str) -> tuple[str, ...]:
        return (*SUBJECT_INDEX_NAMESPACE, namespace_label(subject_id))


class DeletionReport(BaseModel):
    """What a data-protection operation deleted.

    Attributes:
        deleted: Number of deleted entries per category.
        ids: Ids of the deleted entries per category (memory ids, decision keys, store items,
            run ids, thread ids, served thread records, artifact refs, link keys).
        errors: One line per failed backend operation; the others still ran.
    """

    model_config = ConfigDict(frozen=True)

    dry_run: bool = False
    deleted: dict[str, int] = Field(default_factory=dict)
    ids: dict[str, list[str]] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        """Entries deleted across all categories."""

        return sum(self.deleted.values())

    @property
    def ok(self) -> bool:
        """Whether every operation succeeded."""

        return not self.errors


_R = TypeVar("_R", bound=DeletionReport)


class _Sweep:
    """One purge: the ids found per category, and the errors of the steps that failed."""

    def __init__(self, categories: Sequence[str], *, dry_run: bool) -> None:
        self.ids: dict[str, list[str]] = {category: [] for category in categories}
        self.errors: list[str] = []
        self.dry_run = dry_run

    def add(self, category: str, values: Iterable[str]) -> None:
        bucket = self.ids.setdefault(category, [])
        bucket.extend(value for value in values if value not in bucket)

    def error(self, step: str, message: str) -> None:
        self.errors.append(f"{step}: {message}")

    @contextmanager
    def guard(self, step: str) -> Iterator[None]:
        """Run the block; a failure is reported under `step` and the sweep goes on."""

        try:
            yield
        except Exception as exc:  # noqa: BLE001 - report and sweep the rest
            self.error(step, f"{type(exc).__name__}: {exc}")

    async def call(self, participant: ErasureParticipant, method: str, request: Any) -> Any:
        """What the participant's optional `method` returns, or `None` when it has none or fails."""

        call = getattr(participant, method, None)
        with self.guard(f"{participant.category} {method}"):
            return None if call is None else await call(request)
        return None

    def traces(self, delete: Callable[[], list[str]]) -> None:
        """Run a `TraceStore` deletion; files deleted before a failure still count."""

        from awa.observability.trace_store import TraceDeleteError

        with self.guard("traces"):
            try:
                self.add("traces", delete())
            except TraceDeleteError as exc:
                self.add("traces", exc.deleted)
                raise

    def report(self, kind: type[_R], **fields: Any) -> _R:
        deleted = {category: len(values) for category, values in self.ids.items()}
        return kind(
            dry_run=self.dry_run, deleted=deleted, ids=self.ids, errors=self.errors, **fields
        )


RetentionKind = Literal["threads", "memories", "runs", "traces"]
"""What a `RetentionPolicy` limits."""

RETENTION_CATEGORIES: tuple[RetentionKind, ...] = ("threads", "memories", "runs", "traces")
"""Keys of `RetentionReport.deleted`, besides `"links"` of purged threads and the category of each
participant (`"process_runs"`)."""


def _positive(value: DurationLike | None, name: str) -> timedelta | None:
    age = to_timedelta(value, what=name)
    if age is not None and age <= timedelta(0):
        raise ConfigurationError(f"{name} must be positive, got {value!r}")
    return age


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long each kind of data is kept.

    Ages are `timedelta`s, seconds or text such as `"90d"`. A kind without an age (its own or
    `max_age`) is kept indefinitely.

    Attributes:
        threads: Maximum time since a thread's latest checkpoint.
        memories: Maximum age of an `AgentMemory` memory (expired ones always go).
        runs: Maximum time since a process run started; a run that is not finished is kept.
        max_trace_files: Most run trace files to keep (the newest).

    Raises:
        ConfigurationError: If an age is not positive, or `max_trace_files` is below 1.

    Example:
        ```python
        policy = RetentionPolicy(max_age=timedelta(days=30), memories=timedelta(days=365))
        ```
    """

    max_age: DurationLike | None = None
    threads: DurationLike | None = None
    memories: DurationLike | None = None
    traces: DurationLike | None = None
    runs: DurationLike | None = None
    max_trace_files: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_age", *RETENTION_CATEGORIES):
            object.__setattr__(self, name, _positive(getattr(self, name), name))
        if self.max_trace_files is not None and self.max_trace_files < 1:
            raise ConfigurationError(
                f"max_trace_files must be at least 1, got {self.max_trace_files!r}"
            )

    def age_for(self, kind: RetentionKind) -> timedelta | None:
        """The maximum age of `kind`, or `None` to keep it indefinitely."""

        own = getattr(self, kind)
        age = own if own is not None else self.max_age
        return age if isinstance(age, timedelta) else None


class RetentionReport(DeletionReport):
    """What `apply_retention` deleted; `applied_at` is the UTC time ages were measured from."""

    applied_at: datetime


@dataclass(frozen=True)
class ErasureRequest:
    """What a participant is asked to erase for one person.

    Attributes:
        threads: Checkpointer threads found for the person.
        runs: Process runs found for the person in the run log.
        scopes: The run log scopes that were searched.
    """

    subject_id: str
    dry_run: bool = False
    threads: tuple[str, ...] = ()
    runs: tuple[str, ...] = ()
    scopes: tuple[Scope, ...] = ()


@dataclass(frozen=True)
class RetentionRequest:
    """What a participant is asked to prune under `policy`, measured from `now` (UTC)."""

    policy: RetentionPolicy
    now: datetime
    dry_run: bool = False


class ErasureParticipant(Protocol):
    """Data outside this package that erasure and retention reach through a plugin.

    Besides `category` (its key in `DeletionReport.deleted`) and `erase`, a participant may define
    `prune(request)`, `threads(request)` (checkpointer threads going with the deleted data) and
    `held_runs(request)` (process runs whose run log must be kept for now).
    """

    category: str

    async def erase(self, request: ErasureRequest) -> list[str]:
        """Delete the data about `request.subject_id`; return the ids (all, with `dry_run`)."""
        ...


def _resolve_participants(
    explicit: Sequence[ErasureParticipant] | None,
    *,
    store: BaseStore | None,
    run_log: RunLog | None,
    run_store: RunStore | None,
    memory: AgentMemory | Sequence[AgentMemory] | None,
    project: Path | None,
) -> list[ErasureParticipant]:
    """The participants to run: `explicit` as given, else every `erasure` plugin that has
    something to do for the backends passed (built-ins, the project folder, installed packages)."""

    if explicit is not None:
        return list(explicit)
    from awa.core.settings import get_settings
    from awa.plugins import REGISTRY

    settings = get_settings()
    found: list[ErasureParticipant] = []
    for plugin in REGISTRY.loadable("erasure", project=project).values():
        create = getattr(plugin, "create", None)
        participant = (
            create(
                store=store, run_log=run_log, run_store=run_store, settings=settings, memory=memory
            )
            if callable(create)
            else plugin
        )
        if participant is not None:
            found.append(participant)
    return found


async def _purge_threads(
    checkpointer: BaseCheckpointSaver[Any],
    *,
    older_than: timedelta,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Delete the threads whose latest checkpoint is older than `older_than`; return ids, sorted."""

    cutoff = _utc(now) - older_than
    activity = await thread_activity(checkpointer)
    purged = sorted(thread_id for thread_id, last in activity.items() if last < cutoff)
    if not dry_run:
        for thread_id in purged:
            await checkpointer.adelete_thread(thread_id)
    return purged


async def thread_activity(checkpointer: BaseCheckpointSaver[Any]) -> dict[str, datetime]:
    """Return the time of the latest checkpoint of every thread (UTC)."""

    postgres = await postgres_thread_activity(checkpointer)
    if postgres is not None:
        return postgres
    latest: dict[str, datetime] = {}
    async for checkpoint in checkpointer.alist(None):
        thread_id = checkpoint.config.get("configurable", {}).get("thread_id")
        stamp = checkpoint.checkpoint.get("ts")
        if not thread_id or not stamp:
            continue
        moment = checkpoint_time(str(stamp))
        if thread_id not in latest or moment > latest[str(thread_id)]:
            latest[str(thread_id)] = moment
    return latest


def _utc(now: datetime | None) -> datetime:
    """`now` in UTC; a naive time is UTC."""

    moment = now or datetime.now(UTC)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


async def apply_retention(
    policy: RetentionPolicy,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: BaseStore | None = None,
    memory: AgentMemory | Sequence[AgentMemory] | None = None,
    run_log: RunLog | None = None,
    run_store: RunStore | None = None,
    participants: Sequence[ErasureParticipant] | None = None,
    traces: TraceStore | None = None,
    project: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RetentionReport:
    """Delete what `policy` no longer allows to keep, from every backend passed.

    Old threads (with their `SubjectIndex` links), what participants prune (old or expired
    memories, old finished process runs and their threads), old run log rows of runs no participant
    holds, and old or surplus run trace files. A failing backend is reported in `errors`.

    Args:
        store: The store holding links and memories.
        memory: `AgentMemory` instances in other stores or under other prefixes.
        participants: Replace the participants found through the `erasure` plugins.
        project: The project folder to look for `erasure` plugins in.
        now: The time ages are measured from; a naive time is UTC.
        dry_run: Only report what would be deleted.

    Example:
        ```python
        report = await apply_retention(
            RetentionPolicy(max_age=timedelta(days=90)), checkpointer=saver, store=store
        )
        ```
    """

    applied_at = _utc(now)
    sweep = _Sweep((*RETENTION_CATEGORIES, "links"), dry_run=dry_run)
    request = RetentionRequest(policy, applied_at, dry_run)
    active = _resolve_participants(
        participants,
        store=store,
        run_log=run_log,
        run_store=run_store,
        memory=memory,
        project=project,
    )

    purged = await _purge_old_threads(checkpointer, request, active, sweep)
    if store is not None:
        await _forget_thread_links(store, purged, sweep)
    await _prune_runs(run_log, request, active, sweep)
    if traces is not None:
        sweep.traces(
            lambda: traces.prune(
                max_age=policy.age_for("traces"),
                max_files=policy.max_trace_files,
                now=applied_at,
                dry_run=dry_run,
            )
        )
    return sweep.report(RetentionReport, applied_at=applied_at)


async def _purge_old_threads(
    checkpointer: BaseCheckpointSaver[Any] | None,
    request: RetentionRequest,
    participants: Sequence[ErasureParticipant],
    sweep: _Sweep,
) -> list[str]:
    """Delete the threads past their age and the threads going with what participants prune."""

    purged: list[str] = []
    age = request.policy.age_for("threads")
    if checkpointer is not None and age is not None:
        with sweep.guard("threads"):
            purged = await _purge_threads(
                checkpointer, older_than=age, now=request.now, dry_run=request.dry_run
            )
    extra: dict[str, None] = {}
    for participant in participants:
        for thread_id in await sweep.call(participant, "threads", request) or ():
            if thread_id not in purged:
                extra[thread_id] = None
    if extra and checkpointer is None:
        sweep.error(
            "threads",
            f"{len(extra)} thread(s) of pruned runs were not purged; pass the checkpointer",
        )
    elif checkpointer is not None:
        for thread_id in extra:
            purged.append(thread_id)
            if not request.dry_run:
                with sweep.guard(f"thread {thread_id}"):
                    await checkpointer.adelete_thread(thread_id)
    sweep.add("threads", purged)
    return purged


async def _forget_thread_links(store: BaseStore, threads: Sequence[str], sweep: _Sweep) -> None:
    links = SubjectIndex(store)
    for thread_id in threads:
        with sweep.guard(f"thread {thread_id}"):
            if await links.forget_thread(thread_id, dry_run=sweep.dry_run):
                sweep.add("links", [_key("thread", thread_id)])


async def _prune_runs(
    run_log: RunLog | None,
    request: RetentionRequest,
    participants: Sequence[ErasureParticipant],
    sweep: _Sweep,
) -> None:
    """Let the participants prune, then delete the old run log rows no participant holds."""

    held: set[str] = set()
    for participant in participants:
        held |= await sweep.call(participant, "held_runs", request) or set()
    for participant in participants:
        sweep.add(participant.category, await sweep.call(participant, "prune", request) or [])
    age = request.policy.age_for("runs")
    if run_log is not None and age is not None:
        with sweep.guard("runs"):
            await _prune_run_log(run_log, request.now - age, held, sweep)


async def _prune_run_log(run_log: RunLog, before: datetime, held: set[str], sweep: _Sweep) -> None:
    system = SystemScope("retention")
    if sweep.dry_run:
        page = await run_log.expired(system, before=before, limit=10**6)
        sweep.add("runs", (ref.run_id for ref in page if ref.run_id not in held))
        return
    limit = len(held) + 1000
    while True:
        page = await run_log.expired(system, before=before, limit=limit)
        todo = [ref for ref in page if ref.run_id not in held]
        by_scope: dict[tuple[str, str], list[str]] = {}
        for ref in todo:
            by_scope.setdefault((ref.tenant, ref.project), []).append(ref.run_id)
        deleted = 0
        for (tenant, project), run_ids in by_scope.items():
            deleted += await run_log.delete_runs(Scope(tenant, project), run_ids)
        sweep.add("runs", (ref.run_id for ref in todo))
        if deleted == 0 or len(page) < limit:
            return


__all__ = [
    "RETENTION_CATEGORIES",
    "SUBJECT_INDEX_NAMESPACE",
    "SUBJECT_METADATA_KEY",
    "DeletionReport",
    "ErasureParticipant",
    "ErasureRequest",
    "LinkKind",
    "RetentionKind",
    "RetentionPolicy",
    "RetentionReport",
    "RetentionRequest",
    "SubjectIndex",
    "SubjectLinks",
    "apply_retention",
    "thread_activity",
]
