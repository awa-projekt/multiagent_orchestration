"""Audit trail: tamper-evident, hash-chained records of what a run decided and did.

Records hold identifiers, names, outcomes and redacted arguments, but no free-text content
unless `include_content=True`; actor, tenant and request id come from `RunContext.metadata`.
`audit_node` records the nodes of your own graph into the same chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from awa._internal.ids import new_id
from awa._internal.logging import get_logger
from awa._internal.util import elapsed_ms
from awa.contracts.events import AuditEvent, RunEvent
from awa.core.dotenv import restrict_to_owner
from awa.errors import AwaError, ConfigurationError
from awa.kernel.redaction import SECRETS_REDACTOR, Redactor, redact_mapping
from awa.observability._trace_encode import EVENT_FIELDS
from awa.runtime.context import (
    ACTOR_METADATA_KEY,
    REQUEST_ID_METADATA_KEY,
    TENANT_METADATA_KEY,
    current_run_context,
)

_logger = get_logger("audit")

AuditSink = Callable[[AuditEvent], None]
"""Receives each `AuditEvent` produced by `audit_node` (an `AuditLog` is one)."""

GENESIS_HASH = "0" * 64
"""`prev_hash` of the first record of a chain."""

DEFAULT_AUDIT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent_started",
        "agent_finished",
        "error",
        "run_cancelled",
        "delegation",
        "delegation_result",
        "handoff",
        "approval_requested",
        "approval_decided",
        "guardrail_blocked",
        "limit_exceeded",
        "tool_call",
        "node_completed",
        "node_failed",
    }
)
"""Event types an `AuditLog` records by default."""

_DEDUP_WINDOW = 256

_FREE_TEXT: dict[str, tuple[str, ...]] = {
    "error": ("message",),
    "approval_requested": ("reason",),
    "approval_decided": ("note",),
    "guardrail_blocked": ("reason",),
}
"""Free-text fields the audit log keeps only by length, besides the content of `EVENT_FIELDS`."""


class AuditRecord(BaseModel):
    """One entry of a hash-chained audit trail.

    Attributes:
        seq: Position in the chain, starting at 0.
        event_type: The recorded event's `type` (or `AuditEvent.event_type`, or the type given to
            `AuditLog.record`).
        actor: The reviewer for approval decisions, otherwise the run's actor.
        data: The event itself, redacted and without free-text content by default.
        prev_hash: `hash` of the previous record (`GENESIS_HASH` for the first).
        hash: SHA-256 over the canonical JSON of every other field.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    recorded_at: datetime
    event_type: str
    run_id: str | None = None
    root_run_id: str | None = None
    parent_run_id: str | None = None
    agent: str | None = None
    actor: str | None = None
    tenant: str | None = None
    request_id: str | None = None
    data: dict[str, Any]
    prev_hash: str
    hash: str

    def compute_hash(self) -> str:
        """Recompute the hash from the record's content (see `AuditRecord.hash`)."""

        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"hash"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditChainError(AwaError, ValueError):
    """An audit file has a damaged line; repair or move it, or read with `tolerant=True`."""

    code = "awa.audit_chain"


_UNREADABLE_KEY = "unreadable_line"
"""Key of the marker mapping a tolerant reader yields for a damaged line."""


class ChainVerification(BaseModel):
    """Result of `verify_chain`; truthy when valid.

    Attributes:
        seq: `seq` of the first bad record, for an invalid chain.
        head_hash: `hash` of the last valid record; store it outside the log to detect removal of
            records at the end.
    """

    model_config = ConfigDict(frozen=True)

    valid: bool
    checked: int
    error: str | None = None
    seq: int | None = None
    head_hash: str | None = None

    def __bool__(self) -> bool:
        return self.valid


def verify_chain(
    records: Iterable[AuditRecord | Mapping[str, Any]], *, expected_prev_hash: str | None = None
) -> ChainVerification:
    """Check that audit records are unmodified, complete and in order.

    Hashes must match, `seq` must increase by one and `prev_hash` must chain. A segment that does
    not start at `seq` 0 is only valid with a matching `expected_prev_hash`. Records missing at the
    very end show only against a `head_hash` kept elsewhere.

    Args:
        records: The records in chain order (models or JSON dicts; damaged-line markers are
            reported).
        expected_prev_hash: `prev_hash` of the first record, from the previous file's last record.

    Example:
        ```python
        result = verify_chain(JsonlAuditSink("audit/awa.jsonl").read())
        if not result:
            alert(f"audit trail broken at seq {result.seq}: {result.error}")
        ```
    """

    previous: AuditRecord | None = None
    checked = 0
    for raw in records:
        seq: int | None = None
        try:
            record = raw if isinstance(raw, AuditRecord) else AuditRecord.model_validate(raw)
        except ValidationError as exc:
            if isinstance(raw, Mapping) and _UNREADABLE_KEY in raw:
                error: str | None = f"record {checked} is unreadable: {raw[_UNREADABLE_KEY]}"
            else:
                error = f"record {checked} is malformed: {exc}"
        else:
            seq = record.seq
            error = _chain_error(record, previous, expected_prev_hash)
        if error is not None:
            return ChainVerification(
                valid=False,
                checked=checked,
                error=error,
                seq=seq,
                head_hash=previous.hash if previous else None,
            )
        previous = record
        checked += 1
    return ChainVerification(
        valid=True, checked=checked, head_hash=previous.hash if previous else None
    )


def _chain_error(
    record: AuditRecord, previous: AuditRecord | None, expected_prev_hash: str | None
) -> str | None:
    """What is wrong with `record` following `previous`, or `None`."""

    if record.compute_hash() != record.hash:
        return f"record {record.seq} was modified (hash mismatch)"
    if previous is not None:
        if record.seq != previous.seq + 1:
            return (
                f"expected record {previous.seq + 1}, found {record.seq} "
                "(records missing or reordered)"
            )
        if record.prev_hash != previous.hash:
            return f"record {record.seq} does not link to record {previous.seq}"
    elif expected_prev_hash is not None:
        if record.prev_hash != expected_prev_hash:
            return f"record {record.seq} does not continue the expected chain"
    elif record.seq != 0:
        return (
            f"chain starts at record {record.seq}, not 0 (earlier records are missing); "
            "pass expected_prev_hash to check a later segment"
        )
    elif record.prev_hash != GENESIS_HASH:
        return "record 0 does not start from the genesis hash"
    return None


@runtime_checkable
class AuditLogSink(Protocol):
    """Durable storage for `AuditRecord`s.

    `write` must append and raise when the record could not be stored; `last_record` lets a new
    `AuditLog` continue an existing chain.
    """

    def write(self, record: AuditRecord) -> None:
        """Append `record`."""
        ...

    def last_record(self) -> AuditRecord | None:
        """Return the most recent record, or `None` for an empty sink."""
        ...


class InMemoryAuditSink:
    """Keeps records in a list (tests, inspection)."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        """Append `record`."""

        self.records.append(record)

    def last_record(self) -> AuditRecord | None:
        """Return the last record, if any."""

        return self.records[-1] if self.records else None

    def read(self) -> list[AuditRecord]:
        """Return a copy of all records."""

        return list(self.records)


class JsonlAuditSink:
    """Appends records to a JSON-lines file, one record per line.

    The folder is created with mode 0700 and each new file is readable by its owner only (mode
    0600; on Windows an ACL of the current user). Each write is flushed.
    With `max_bytes`, a full file is renamed to `<stem>.<n><suffix>` and a new one started; the
    chain continues and rotated files are never deleted.

    Args:
        fsync: Force every record to disk before `write` returns.
        max_bytes: Rotate before a write would pass this size; `None` never rotates.
    """

    def __init__(
        self, path: str | os.PathLike[str], *, fsync: bool = True, max_bytes: int | None = None
    ) -> None:
        if max_bytes is not None and max_bytes < 1:
            raise ConfigurationError("max_bytes must be at least 1")
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fsync = fsync
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, record: AuditRecord) -> None:
        """Append `record`, rotating first when the file is full."""

        line = (record.model_dump_json() + "\n").encode("utf-8")
        with self._lock:
            if self._max_bytes is not None and self.path.exists():
                size = self.path.stat().st_size
                if size > 0 and size + len(line) > self._max_bytes:
                    self._rotate()
            # Records name people and tool arguments: the owner alone may read them. Rotated
            # files keep their permissions.
            created = not self.path.exists()
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(self.path, flags, 0o600), "ab") as handle:
                if created:
                    restrict_to_owner(self.path)
                handle.write(line)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())

    def files(self) -> list[Path]:
        """The rotated files in chain order, then the current file (if it exists)."""

        files = [path for _, path in sorted(self._rotated(), key=lambda item: item[0])]
        if self.path.exists():
            files.append(self.path)
        return files

    def read(self, *, tolerant: bool = False) -> list[Any]:
        """Read every record, across rotated files, in chain order.

        Raises:
            AuditChainError: A line is damaged (unless `tolerant`, which yields a marker instead).
        """

        return [
            record for path in self.files() for record in read_audit_file(path, tolerant=tolerant)
        ]

    def last_record(self) -> AuditRecord | None:
        """Return the newest record (from the current file, else the newest rotated one)."""

        for path in reversed(self.files()):
            line = _last_line(path)
            if line:
                try:
                    return AuditRecord.model_validate_json(line)
                except ValidationError as exc:
                    raise _damaged(path, _line_count(path), exc) from exc
        return None

    def _rotated(self) -> list[tuple[int, Path]]:
        pattern = re.compile(rf"^{re.escape(self.path.stem)}\.(\d+){re.escape(self.path.suffix)}$")
        return [
            (int(match.group(1)), candidate)
            for candidate in self.path.parent.iterdir()
            if (match := pattern.match(candidate.name)) is not None and candidate.is_file()
        ]

    def _rotate(self) -> None:
        number = max((n for n, _ in self._rotated()), default=0) + 1
        target = self.path.with_name(f"{self.path.stem}.{number:06d}{self.path.suffix}")
        os.replace(self.path, target)


def read_audit_file(path: str | os.PathLike[str], *, tolerant: bool = False) -> Iterator[Any]:
    """Iterate over the records of one JSON-lines audit file.

    Raises:
        AuditChainError: A line is damaged (unless `tolerant`, which yields
            `{"unreadable_line": "<file>, line <n>"}`).
    """

    with Path(path).open("rb") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                yield AuditRecord.model_validate_json(line)
            except ValidationError as exc:
                if not tolerant:
                    raise _damaged(Path(path), number, exc) from exc
                yield {_UNREADABLE_KEY: f"{path}, line {number}"}


def _damaged(path: Path, number: int, exc: ValidationError) -> AuditChainError:
    """The error for a damaged line of an audit file, with the fix."""

    reason = exc.errors()[0]["msg"] if exc.errors() else "invalid record"
    return AuditChainError(
        f"audit file {path}, line {number} is damaged ({reason}). A torn last line "
        "is usually a crash while writing: repair the file or move the damaged line "
        "out (keep a copy as evidence), or read with tolerant=True and run "
        "verify_chain to see where the chain breaks."
    )


def _line_count(path: Path) -> int:
    """Number of lines of `path` (the number of its last line)."""

    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _last_line(path: Path) -> bytes:
    """The last non-empty line of `path`, read from the end."""

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0:
            step = min(8192, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            lines = buffer.rstrip().rsplit(b"\n", 1)
            if len(lines) == 2 and lines[1].strip():
                return lines[1].strip()
        return buffer.strip()


class AuditLog:
    """Run-event subscriber writing a hash-chained audit trail.

    Pass it as a hook (it sees the whole run tree) or as `sink=` to `audit_node`. Writing is
    synchronous and thread-safe; a failed write is logged and does not advance the chain, and an
    event seen twice is recorded once.

    Args:
        event_types: Event types to record; defaults to `DEFAULT_AUDIT_EVENT_TYPES`.
        redactor: Applied to every string; `RegexRedactor.pii()` also masks personal data, `None`
            stores values as they are.
        include_content: Also keep free-text content, redacted, instead of only its length.

    Example:
        ```python
        audit = AuditLog(JsonlAuditSink("audit/awa.jsonl"), redactor=RegexRedactor.pii())
        agent = Agent("payments", tools=[guarded_transfer], hooks=[audit])
        assert verify_chain(audit.sink.read()).valid
        ```
    """

    def __init__(
        self,
        sink: AuditLogSink,
        *,
        event_types: Collection[str] | None = None,
        redactor: Redactor | None = SECRETS_REDACTOR,
        include_content: bool = False,
    ) -> None:
        self.sink = sink
        self.event_types = frozenset(
            DEFAULT_AUDIT_EVENT_TYPES if event_types is None else event_types
        )
        self._redactor = redactor
        self._include_content = include_content
        self._lock = threading.RLock()
        self._recent: deque[object] = deque(maxlen=_DEDUP_WINDOW)
        last = sink.last_record()
        self._next_seq = last.seq + 1 if last is not None else 0
        self._head = last.hash if last is not None else GENESIS_HASH

    @property
    def head(self) -> tuple[int, str]:
        """`(seq, hash)` of the newest record (`(-1, GENESIS_HASH)` when empty)."""

        with self._lock:
            return self._next_seq - 1, self._head

    def __call__(self, event: RunEvent | AuditEvent) -> None:
        """Record `event` if its type is configured. Never raises."""

        try:
            with self._lock:
                if any(seen is event for seen in self._recent):
                    return
                self._recent.append(event)
            if isinstance(event, AuditEvent):
                self._record_node(event)
            elif event.type in self.event_types:
                self._record_event(event)
        except Exception:  # noqa: BLE001 - observers must never fail the run
            _logger.exception("Audit log failed on %s", getattr(event, "type", "event"))

    def record(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        run_id: str | None = None,
        root_run_id: str | None = None,
        agent: str | None = None,
        actor: str | None = None,
        tenant: str | None = None,
        request_id: str | None = None,
    ) -> AuditRecord:
        """Append a record of your own (an application decision, an export, ...) and return it.

        Missing identity fields come from the current run; a failed write raises.
        """

        ctx = current_run_context()
        return self._append(
            event_type,
            self._redact(dict(data)),
            run_id=run_id or (ctx.run_id if ctx else None),
            root_run_id=root_run_id or (ctx.root_run_id if ctx else None),
            parent_run_id=ctx.parent_run_id if ctx else None,
            agent=agent or (ctx.agent if ctx else None),
            actor=actor,
            tenant=tenant,
            request_id=request_id,
        )

    def _record_event(self, event: RunEvent) -> None:
        reviewer = getattr(event, "reviewer", None)
        ctx = current_run_context()
        parent = getattr(event, "parent_run_id", None)
        if parent is None and ctx is not None and ctx.run_id == event.run_id:
            parent = ctx.parent_run_id
        self._append(
            event.type,
            self._minimize(event),
            run_id=event.run_id,
            root_run_id=event.root_run_id,
            parent_run_id=parent,
            agent=event.agent,
            actor=reviewer if isinstance(reviewer, str) else None,
        )

    def _record_node(self, event: AuditEvent) -> None:
        if event.event_type not in self.event_types:
            return
        self._append(
            event.event_type,
            self._redact(event.model_dump(mode="json")),
            run_id=event.run_id,
            root_run_id=event.root_run_id,
            parent_run_id=None,
            agent=event.agent_name,
        )

    def _minimize(self, event: RunEvent) -> dict[str, Any]:
        data = event.model_dump(mode="json")
        if event.type == "run_completed":
            data.pop("result", None)
        if not self._include_content:
            facts = EVENT_FIELDS.get(type(event))
            content = facts.content.split() if facts is not None else []
            for name in (*content, *_FREE_TEXT.get(event.type, ())):
                value = data.get(name)
                if isinstance(value, str) and value:
                    data[name] = f"[omitted: {len(value)} chars]"
            message = data.get("message")
            if isinstance(message, dict):
                data["message"] = {
                    key: message.get(key)
                    for key in ("id", "kind", "sender", "recipient", "topic", "conversation_id")
                }
        return self._redact(data)

    def _redact(self, data: dict[str, Any]) -> dict[str, Any]:
        if self._redactor is None:
            return data
        redacted: dict[str, Any] = redact_mapping(data, self._redactor)
        return redacted

    def _append(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        run_id: str | None,
        root_run_id: str | None,
        parent_run_id: str | None,
        agent: str | None,
        actor: str | None = None,
        tenant: str | None = None,
        request_id: str | None = None,
    ) -> AuditRecord:
        """Write the next record; identity fields not given come from the current run."""

        context_actor, context_tenant, context_request_id = _context_identity()
        with self._lock:
            draft = AuditRecord(
                seq=self._next_seq,
                recorded_at=datetime.now(UTC),
                event_type=event_type,
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent=agent,
                actor=actor or context_actor,
                tenant=tenant or context_tenant,
                request_id=request_id or context_request_id,
                data=data,
                prev_hash=self._head,
                hash="",
            )
            # Hash the JSON form, exactly as it is stored and read back.
            record = draft.model_copy(update={"hash": draft.compute_hash()})
            self.sink.write(record)
            self._next_seq += 1
            self._head = record.hash
            return record


def _identity_value(value: Any, attribute: str = "id") -> str | None:
    """`value` itself when it is a string, else its `attribute` (key or attribute)."""

    if isinstance(value, str):
        return value or None
    found = value.get(attribute) if isinstance(value, Mapping) else getattr(value, attribute, None)
    return None if found is None else str(found)


def _context_identity() -> tuple[str | None, str | None, str | None]:
    """Actor, tenant and request id of the current run, from its metadata."""

    ctx = current_run_context()
    if ctx is None:
        return None, None, None
    metadata = ctx.metadata
    actor_value = metadata.get(ACTOR_METADATA_KEY)
    actor = _identity_value(actor_value)
    tenant = _identity_value(metadata.get(TENANT_METADATA_KEY))
    if tenant is None and actor_value is not None and not isinstance(actor_value, str):
        tenant = _identity_value(actor_value, "tenant")
    request_id = _identity_value(metadata.get(REQUEST_ID_METADATA_KEY))
    return actor, tenant, request_id


@contextmanager
def audit_node(
    node_name: str,
    *,
    agent_name: str | None = None,
    model_profile: str | None = None,
    input_artifact_refs: Sequence[str] = (),
    output_artifact_refs: Sequence[str] = (),
    sink: AuditSink | None = None,
    run_id: str | None = None,
) -> Iterator[None]:
    """Record one `AuditEvent` for the enclosed block: `"node_completed"` or `"node_failed"`.

    It is logged as JSON on the `awa.audit` logger and passed to `sink` (an `AuditLog` chains it).
    Pass identifiers and references only, never secrets or raw content.

    Example:
        ```python
        with audit_node("summarize", agent_name="writer", sink=audit_log):
            result = await writer.run(task)
        ```
    """

    ctx = current_run_context()
    started = time.perf_counter()
    event_type = "node_completed"
    try:
        yield
    except BaseException:
        event_type = "node_failed"
        raise
    finally:
        event = AuditEvent(
            event_id=new_id("evt"),
            event_type=event_type,
            node_name=node_name,
            run_id=run_id or (ctx.run_id if ctx else None),
            root_run_id=ctx.root_run_id if ctx else None,
            agent_name=agent_name,
            model_profile=model_profile,
            input_artifact_refs=list(input_artifact_refs),
            output_artifact_refs=list(output_artifact_refs),
            latency_ms=elapsed_ms(started),
            created_at=datetime.now(UTC),
        )
        _logger.info(event.model_dump_json())
        if sink is not None:
            try:
                sink(event)
            except Exception:  # noqa: BLE001 - an audit sink must not fail the node
                _logger.exception("Audit sink failed for node %s", node_name)


def sort_audit_trail(
    events: Iterable[AuditEvent], *, declared_order: Sequence[str]
) -> list[AuditEvent]:
    """Order audit events by `declared_order` of nodes (stable; unknown nodes last), as a new list."""

    rank = {name: index for index, name in enumerate(declared_order)}
    fallback_rank = len(rank)
    return sorted(events, key=lambda event: rank.get(event.node_name, fallback_rank))


__all__ = [
    "ACTOR_METADATA_KEY",
    "DEFAULT_AUDIT_EVENT_TYPES",
    "GENESIS_HASH",
    "REQUEST_ID_METADATA_KEY",
    "TENANT_METADATA_KEY",
    "AuditChainError",
    "AuditLog",
    "AuditLogSink",
    "AuditRecord",
    "AuditSink",
    "ChainVerification",
    "InMemoryAuditSink",
    "JsonlAuditSink",
    "audit_node",
    "read_audit_file",
    "sort_audit_trail",
    "verify_chain",
]
