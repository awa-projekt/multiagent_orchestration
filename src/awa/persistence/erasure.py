"""Erase the data awa stores about one person in the backends you pass.

A building block for GDPR Art. 17 requests, not a compliance guarantee. `erase_subject` is
idempotent and validates its arguments before deleting anything. A `SubjectIndex` link is
forgotten only once its target is gone, so a second call with a missing backend completes the
erasure. Cache entries, audit logs and trace files outside `traces=` are not touched.

| Data | Found by |
| --- | --- |
| `AgentMemory` memories | the subject's per-user namespaces, and `MemoryItem.subject` |
| Remembered approval decisions | the subject id as a segment of the decision key, split on `:`, `/` or `\\|` |
| Run log rows (`run_log=`, `scopes=`) | the HMAC link of the person to the run (`RunLog.subject_runs`) |
| Process runs (`run_store=`) | their run id (found in the run log), a thread of the subject, or `actor == subject_id` |
| Checkpointer threads | a `SubjectIndex` link, `thread_ids=`, checkpoint metadata `awa.subject`, the thread index (`threads_index=`), or a thread of a deleted process run |
| Served thread records (`threads_index=`) | the principal that created the thread |
| Artifacts | a `SubjectIndex` link and `artifact_prefixes=` |
| Run traces | `traces=`: traces whose header names the subject (`subject` or `actor`), or a thread of the subject |
| Your own store data | `namespaces=` templates containing `{subject}` or `{subject_label}` |
| Anything else | a participant: an `erasure` plugin (see `awa.persistence.retention`) |
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa._internal.upstream.checkpoint import BaseCheckpointSaver, BaseStore
from awa.errors import ConfigurationError
from awa.persistence.artifacts import ArtifactRepository
from awa.persistence.namespaces import (
    delete_items,
    iter_namespace_items,
    iter_prefix_items,
    namespace_label,
)
from awa.persistence.retention import (
    SUBJECT_METADATA_KEY,
    DeletionReport,
    ErasureParticipant,
    ErasureRequest,
    SubjectIndex,
    SubjectLinks,
    _key,
    _resolve_participants,
    _Sweep,
)
from awa.persistence.runlog import RunLog, Scope
from awa.persistence.threads import OwnedThreads

if TYPE_CHECKING:
    from awa.automation.store import RunStore
    from awa.memory.long_term import AgentMemory
    from awa.observability.trace_store import TraceStore
    from awa.persistence.threads import BaseThreadIndex

ERASURE_CATEGORIES: tuple[str, ...] = (
    "memories",
    "approvals",
    "store_items",
    "runs",
    "threads",
    "served_threads",
    "artifacts",
    "traces",
    "process_runs",
    "links",
)
"""Keys of `ErasureReport.deleted` and `ErasureReport.ids`, in erasure order."""

DEFAULT_DECISION_NAMESPACES: tuple[tuple[str, ...], ...] = (("awa", "tool_approval"),)
"""Store namespaces of remembered approval decisions."""

_KEY_SEGMENTS = re.compile(r"[:/|]")


class ErasureReport(DeletionReport):
    """What `erase_subject` deleted for the data subject `subject_id`."""

    subject_id: str


def _validate(
    subject_id: str,
    namespaces: Sequence[Sequence[str]],
    artifact_prefixes: Sequence[str],
    run_log: RunLog | None,
    scopes: Sequence[Scope],
) -> None:
    if not subject_id:
        raise ConfigurationError("subject_id must not be empty")
    for template in namespaces:
        if not template or not any(
            "{subject}" in label or "{subject_label}" in label for label in template
        ):
            raise ConfigurationError(
                f"The namespace template {tuple(template)!r} holds neither {{subject}} nor "
                "{subject_label}, so it would erase data of every subject; nothing was deleted."
            )
    for prefix in artifact_prefixes:
        if "{subject}" not in prefix:
            raise ConfigurationError(
                f"The artifact prefix {prefix!r} holds no {{subject}}, so it would erase "
                "artifacts of every subject; nothing was deleted."
            )
    if run_log is not None and not scopes:
        raise ConfigurationError(
            "Erasing from a run log needs the scopes to search (scopes=[Scope(tenant, project)])."
        )


async def erase_subject(
    subject_id: str,
    *,
    store: BaseStore | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    artifacts: ArtifactRepository | None = None,
    run_log: RunLog | None = None,
    scopes: Sequence[Scope] = (),
    threads_index: BaseThreadIndex | None = None,
    run_store: RunStore | None = None,
    participants: Sequence[ErasureParticipant] | None = None,
    memory: AgentMemory | Sequence[AgentMemory] | None = None,
    traces: TraceStore | None = None,
    namespaces: Sequence[Sequence[str]] = (),
    thread_ids: Iterable[str] = (),
    artifact_prefixes: Sequence[str] = (),
    project: Path | None = None,
    dry_run: bool = False,
) -> ErasureReport:
    """Erase a data subject's data from every backend passed.

    Each backend is optional. A failing backend, and linked data whose backend was not passed, are
    reported in `errors`, so a partial erasure is never silent.

    Args:
        subject_id: The person's id: the user id memory is scoped by.
        store: The `BaseStore` holding remembered decisions, `SubjectIndex` links and (unless
            `memory` names others) `AgentMemory` memories under the default prefix.
        run_log: The `RunLog` whose runs of the person to delete (needs `scopes` and the pseudonym
            key).
        threads_index: A server's thread index: the person's threads are erased and their records
            forgotten.
        run_store: The process run store: runs of the person are deleted with their threads.
        participants: Replace the participants found through the `erasure` plugins.
        memory: `AgentMemory` instances in other stores or under other namespace prefixes.
        traces: The `TraceStore` of run traces.
        namespaces: Your own store namespaces, with `{subject}` or `{subject_label}`
            (`namespace_label(subject_id)`, for ids with `"."`); everything in and below each is
            deleted.
        thread_ids: Further threads known to hold the subject's data.
        artifact_prefixes: Ref prefixes with `{subject}`; end one with a delimiter
            (`"export:{subject}:"`) so `u-4` does not match `u-42`.
        project: The project folder to look for `erasure` plugins in.
        dry_run: Only report what would be deleted.

    Raises:
        ConfigurationError: If `subject_id` is empty, a template lacks its placeholder, or
            `run_log` comes without `scopes`; nothing is deleted then.

    Example:
        ```python
        report = await erase_subject("u-42", store=store, checkpointer=checkpointer)
        assert report.ok, report.errors
        ```
    """

    _validate(subject_id, namespaces, artifact_prefixes, run_log, scopes)
    sweep = _Sweep(ERASURE_CATEGORIES, dry_run=dry_run)
    active = _resolve_participants(
        participants,
        store=store,
        run_log=run_log,
        run_store=run_store,
        memory=memory,
        project=project,
    )
    index = SubjectIndex(store) if store is not None else None
    links = SubjectLinks()
    if index is not None:
        with sweep.guard("subject index"):
            links = await index.links(subject_id)
    done: set[str] = set()  # link keys whose target is erased or gone: the link may go

    if store is not None:
        await _erase_store_items(store, subject_id, namespaces, sweep)
    runs = await _erase_runs(run_log, scopes, subject_id, sweep)
    served = await _served_threads(threads_index, subject_id, sweep)
    request = ErasureRequest(subject_id, dry_run, runs=tuple(runs), scopes=tuple(scopes))

    known = [*links.threads, *thread_ids, *(thread_id for _, thread_id in served)]
    threads = await _subject_threads(checkpointer, subject_id, known, sweep)
    for participant in active:
        asked = replace(request, threads=tuple(threads))
        extra = await sweep.call(participant, "threads", asked) or ()
        threads += [thread_id for thread_id in extra if thread_id not in threads]
    request = replace(request, threads=tuple(threads))
    if checkpointer is not None:
        await _erase_threads(checkpointer, threads, threads_index, served, sweep, done)

    refs = list(links.artifacts)
    if artifacts is not None:
        for template in artifact_prefixes:
            with sweep.guard("artifacts"):
                refs.extend(await artifacts.list_refs(template.replace("{subject}", subject_id)))
    await _erase_artifacts(artifacts, refs, sweep, done)

    for participant in active:
        with sweep.guard(participant.category):
            sweep.add(participant.category, await participant.erase(request))
    if traces is not None:
        sweep.traces(lambda: traces.erase(subject_id, threads=threads, dry_run=dry_run))
    if index is not None and links:
        await _unlink_erased(index, subject_id, links, done, sweep)
    return sweep.report(ErasureReport, subject_id=subject_id)


async def _served_threads(
    threads_index: BaseThreadIndex | None, subject_id: str, sweep: _Sweep
) -> list[tuple[str, str]]:
    """`(system, thread_id)` of the served threads the subject created."""

    if threads_index is None:
        return []
    if not isinstance(threads_index, OwnedThreads):
        sweep.error("served_threads", "the thread index cannot list threads by owner")
        return []
    with sweep.guard("served_threads"):
        return await threads_index.owned_by(subject_id)
    return []


async def _unlink_erased(
    index: SubjectIndex, subject_id: str, links: SubjectLinks, done: set[str], sweep: _Sweep
) -> None:
    """Forget the subject's links whose target is erased or gone."""

    finished = SubjectLinks(
        threads=tuple(t for t in links.threads if _key("thread", t) in done),
        artifacts=tuple(a for a in links.artifacts if _key("artifact", a) in done),
    )
    with sweep.guard("subject index"):
        if finished and not sweep.dry_run:
            await index.unlink(subject_id, threads=finished.threads, artifacts=finished.artifacts)
        sweep.add("links", finished.keys())


async def _erase_store_items(
    store: BaseStore, subject_id: str, templates: Sequence[Sequence[str]], sweep: _Sweep
) -> None:
    """Remembered decisions naming the subject, and everything under the namespace templates."""

    for namespace in DEFAULT_DECISION_NAMESPACES:
        with sweep.guard(f"approvals {'/'.join(namespace)}"):
            matches = [
                item
                async for item in iter_namespace_items(store, namespace)
                if subject_id in _KEY_SEGMENTS.split(item.key)
            ]
            deleted = await delete_items(store, matches, dry_run=sweep.dry_run)
            sweep.add("approvals", (item.key for item in deleted))
    encoded = namespace_label(subject_id)
    for template in templates:
        namespace = tuple(
            label.replace("{subject_label}", encoded).replace("{subject}", subject_id)
            for label in template
        )
        with sweep.guard(f"store namespace {'/'.join(namespace)}"):
            items = [item async for item in iter_prefix_items(store, namespace)]
            deleted = await delete_items(store, items, dry_run=sweep.dry_run)
            sweep.add("store_items", (f"{'/'.join(i.namespace)}/{i.key}" for i in deleted))


async def _erase_runs(
    run_log: RunLog | None, scopes: Sequence[Scope], subject_id: str, sweep: _Sweep
) -> list[str]:
    runs: list[str] = []
    if run_log is None:
        return runs
    for scope in scopes:
        with sweep.guard("runs"):
            try:
                run_ids = await run_log.subject_runs(scope, subject_id)
            except ConfigurationError:
                sweep.error("runs", "no pseudonym key: runs cannot be attributed")
                return runs
            if run_ids and not sweep.dry_run:
                await run_log.delete_runs(scope, run_ids)
            runs += run_ids
            sweep.add("runs", run_ids)
    return runs


async def _subject_threads(
    checkpointer: BaseCheckpointSaver[Any] | None,
    subject_id: str,
    known: Sequence[str],
    sweep: _Sweep,
) -> list[str]:
    threads = dict.fromkeys(known)
    if checkpointer is None:
        if threads:
            sweep.error(
                "threads",
                f"{len(threads)} thread(s) of the subject were not erased; pass the checkpointer",
            )
        return list(threads)
    with sweep.guard("threads (metadata search)"):
        async for checkpoint in checkpointer.alist(None, filter={SUBJECT_METADATA_KEY: subject_id}):
            thread_id = checkpoint.config.get("configurable", {}).get("thread_id")
            if thread_id:
                threads[str(thread_id)] = None
    return list(threads)


async def _erase_threads(
    checkpointer: BaseCheckpointSaver[Any],
    threads: Sequence[str],
    threads_index: BaseThreadIndex | None,
    served: Sequence[tuple[str, str]],
    sweep: _Sweep,
    done: set[str],
) -> None:
    """Erase the threads, then forget the subject's thread records whose checkpoints are gone."""

    for thread_id in threads:
        with sweep.guard(f"thread {thread_id}"):
            config: Any = {"configurable": {"thread_id": thread_id}}
            if await checkpointer.aget_tuple(config) is not None:
                if not sweep.dry_run:
                    await checkpointer.adelete_thread(thread_id)
                sweep.add("threads", [thread_id])
            done.add(_key("thread", thread_id))
    if threads_index is None:
        return
    for system, thread_id in served:
        if _key("thread", thread_id) in done:
            with sweep.guard(f"served thread {system}/{thread_id}"):
                if not sweep.dry_run:
                    await threads_index.forget(system, thread_id)
                sweep.add("served_threads", [f"{system}/{thread_id}"])


async def _erase_artifacts(
    artifacts: ArtifactRepository | None, refs: Sequence[str], sweep: _Sweep, done: set[str]
) -> None:
    unique = list(dict.fromkeys(refs))
    if artifacts is None:
        if unique:
            sweep.error(
                "artifacts",
                f"{len(unique)} artifact(s) of the subject were not erased; "
                "pass the artifact repository",
            )
        return
    for ref in unique:
        with sweep.guard(f"artifact {ref}"):
            existed = await artifacts.exists(ref) if sweep.dry_run else await artifacts.delete(ref)
            done.add(_key("artifact", ref))
            if existed:
                sweep.add("artifacts", [ref])


__all__ = ["DEFAULT_DECISION_NAMESPACES", "ERASURE_CATEGORIES", "ErasureReport", "erase_subject"]
