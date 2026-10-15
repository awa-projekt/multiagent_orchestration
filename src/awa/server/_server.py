"""The serving state behind an app or a router: who may do what, running systems for requests,
approvals and threads, and the Server-Sent Events buffer between a run and a slow client."""

from __future__ import annotations

import asyncio
import copy
import time
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
)
from contextlib import ExitStack, aclosing, contextmanager
from functools import partial
from typing import Any, Literal, Self

from awa._internal.aio import LoopLocal
from awa._internal.logging import get_logger
from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa.contracts.events import ErrorEvent, RunCompletedEvent, RunEvent, TextDeltaEvent
from awa.contracts.protocols import AgentLike
from awa.contracts.results import RunResult
from awa.persistence.threads import BaseThreadIndex, ThreadOwner
from awa.runtime.ambient import subscribe_globally
from awa.runtime.context import RunContext, new_run_id, use_run_context, use_run_id
from awa.server._auth import ANONYMOUS, Authenticator, Principal, principal_context
from awa.server._config import (
    API_PREFIX,
    ServedSystem,
    ServingOptions,
    SseOverflow,
    lookup,
    stamp_reviewer,
    system_checkpointer,
    system_kind,
    system_mermaid,
    system_topology,
)
from awa.server._limits import as_rate_limiter
from awa.server._logging import current_request_id
from awa.server._requests import ResumeRequest, RunCall, RunRequest, call_options, run_metadata
from awa.server._runs import RunHandle, RunMode, RunRegistry, RunStatus
from awa.server._serialize import (
    SSE_PING,
    event_json,
    http_interrupts,
    jsonable,
    pending_interrupts,
    pending_state,
    result_json,
    sse_frame,
    terminal_error_json,
)
from awa.server._stores import (
    IdempotencyClaim,
    InMemoryIdempotencyCache,
    claim_paused_state,
    decision_claims,
    fingerprint,
    scope,
)
from awa.server._web import (
    JSONResponse,
    Receive,
    Request,
    Response,
    Scope,
    Send,
    StreamingResponse,
    anyio,
    describe_and_log,
    error_parts,
    error_response,
)
from awa.server.errors import (
    InvalidRequestError,
    NoPendingInterruptError,
    PermissionDeniedError,
    RateLimitExceededError,
    RunCancelledError,
    RunNotFoundError,
    ServiceUnavailableError,
    SystemBusyError,
    ThreadNotFoundError,
    describe_error,
)

_log = get_logger("server")
_audit = get_logger("server.audit")

_BUSY_RETRY_AFTER = 1.0
_READINESS_PROBE_THREAD = "__awa_readiness_probe__"
_READINESS_MAX_AGE = 1.0
"""Seconds a readiness result is reused, so frequent callers do not reach the backends."""
_CANCEL_WAIT = 5.0
_REPLAYED_HEADERS = ("Location",)


class _Overflow(Exception):
    """The client stopped reading its event stream."""


class _EventBuffer:
    """Events waiting to be sent to one SSE client, at most `limit` of them.

    With `drop` a full buffer sheds text deltas; otherwise (or with `disconnect`) `offer` reports
    an overflow.
    """

    def __init__(self, limit: int, policy: SseOverflow) -> None:
        self._items: deque[tuple[str, Any]] = deque()
        self._ready = asyncio.Event()
        self._limit = limit
        self._policy = policy
        self.dropped = 0

    def offer(self, event: RunEvent) -> bool:
        if len(self._items) >= self._limit:
            if self._policy != "drop":
                return False
            if isinstance(event, TextDeltaEvent):
                self.dropped += 1
                return True
            for index, (kind, value) in enumerate(self._items):
                if kind == "event" and isinstance(value, TextDeltaEvent):
                    del self._items[index]
                    self.dropped += 1
                    break
            else:
                return False
        self._put(("event", event))
        return True

    def close(self, kind: str, value: Any = None, *, discard: bool = False) -> None:
        if discard:
            self._items.clear()
        self._put((kind, value))

    def _put(self, item: tuple[str, Any]) -> None:
        self._items.append(item)
        self._ready.set()

    async def get(self) -> tuple[str, Any]:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()


class _EventStreamResponse(StreamingResponse):
    """A streaming response that always closes its body iterator (cancelling the
    run behind it) and releases its reservation, however the connection ends."""

    def __init__(
        self, content: AsyncIterator[bytes], *, on_close: Callable[[], None], **kwargs: Any
    ) -> None:
        super().__init__(content, **kwargs)
        self._content = content
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self._content, "aclose", None)
            with anyio.CancelScope(shield=True):
                if aclose is not None:
                    await aclose()
            self._on_close()


async def _events(system: AgentLike, call: RunCall) -> AsyncGenerator[RunEvent, None]:
    stream = getattr(system, "stream", None)
    if not callable(stream):
        result = await system.run(call.input, **call.options(system.run))
        yield RunCompletedEvent(
            result=result, run_id=result.run_id, root_run_id=result.run_id, agent=result.agent
        )
        return
    events = stream(call.input, **call.options(stream))
    async with aclosing(events):
        async for event in events:
            yield event


async def _finish(
    claim: IdempotencyClaim | None, status: int, payload: dict[str, Any], headers: Mapping[str, str]
) -> None:
    """Store the response of an `Idempotency-Key` request (if any) for replays."""

    if claim is None:
        return
    kept = {name: value for name, value in headers.items() if name in _REPLAYED_HEADERS}
    # Failures a retry may overcome are not stored, so the retry runs again.
    retryable = status >= 500 or status in (408, 409, 425, 429)
    await claim.finish(status, payload, kept, retryable=retryable)


async def _release(claim: IdempotencyClaim | None) -> None:
    if claim is not None:
        await claim.release()


def _run_body(result: RunResult[Any], request_id: str | None = None) -> dict[str, Any]:
    body = result_json(result)
    body["request_id"] = request_id if request_id is not None else current_request_id()
    return body


def _decision_summary(decision: Any) -> Any:
    if isinstance(decision, Mapping):
        if "action" in decision:
            return decision.get("action")
        return {k: v.get("action") if isinstance(v, Mapping) else None for k, v in decision.items()}
    return None


class Server:
    """The serving state behind an app of `create_app` or a router of `create_router`."""

    def __init__(
        self,
        systems: dict[str, ServedSystem],
        options: ServingOptions,
        *,
        authenticator: Authenticator | None,
        thread_index: BaseThreadIndex,
        single_caller: bool = False,
    ) -> None:
        self.systems = systems
        self._subscribers = tuple(options.subscribers)
        self._subscribed = 0
        self._unsubscribe: list[Callable[[], None]] = []
        self._checkpointer = options.checkpointer
        self.authenticator = authenticator
        # Every caller is the same principal (no authentication, or one api_key); a router
        # with an `auth` dependency of its host app sets it to False.
        self.single_caller = single_caller
        self.ping_interval = options.sse_ping_interval
        self._scope = options.thread_scope
        self._index = thread_index
        self._decisions = decision_claims(thread_index)
        self._rate_limiter = as_rate_limiter(options.rate_limit)
        self._system_limiters = {
            name: limiter
            for name, served in systems.items()
            if (limiter := as_rate_limiter(served.rate_limit)) is not None
        }
        self.idempotency = options.idempotency_cache or InMemoryIdempotencyCache()
        self.idempotency_ttl = options.idempotency_ttl
        self.drain_timeout = options.drain_timeout
        self._sse_queue_size = options.sse_queue_size
        self._sse_overflow: SseOverflow = options.sse_overflow
        self._readiness_checks = tuple(options.readiness_checks)
        self._readiness_timeout = options.readiness_timeout
        self._readiness_probed: tuple[float, dict[str, str]] | None = None
        self._held: set[tuple[str, str | None]] = set()
        # One semaphore per event loop: an app may be served by several loops in turn.
        self._slots: dict[str, LoopLocal[asyncio.Semaphore]] = {
            name: LoopLocal(partial(asyncio.Semaphore, served.max_concurrency))
            for name, served in systems.items()
            if served.max_concurrency is not None
        }
        self.runs = RunRegistry(retention=options.run_retention)
        self._audit_log = options.audit_log
        self._subjects = options.subject_index

    def sibling(self) -> Self:
        """A server with systems of its own (built projects) that shares everything else.

        Its systems are not reachable through this server's endpoints.
        """

        other = copy.copy(self)
        other.systems = {}
        other._held = set()
        other._slots = {}
        other._system_limiters = {}
        return other

    def add_system(self, name: str, served: ServedSystem) -> None:
        """Serve `served` as `name` from now on, with its own concurrency and rate limits."""

        self.systems[name] = served
        if (limiter := as_rate_limiter(served.rate_limit)) is not None:
            self._system_limiters[name] = limiter
        if served.max_concurrency is not None:
            self._slots[name] = LoopLocal(partial(asyncio.Semaphore, served.max_concurrency))

    def remove_system(self, name: str) -> None:
        """Stop serving `name` (runs in flight finish; new requests answer 404)."""

        self.systems.pop(name, None)
        self._system_limiters.pop(name, None)
        self._slots.pop(name, None)

    @contextmanager
    def subscribed(self) -> Iterator[None]:
        """Subscribe the server's `subscribers` globally while the block (a lifespan) runs.

        Nested and repeated lifespans of one server register them once.
        """

        if self._subscribed == 0:
            self._unsubscribe = [subscribe_globally(s) for s in self._subscribers]
        self._subscribed += 1
        try:
            yield
        finally:
            self._subscribed -= 1
            if self._subscribed == 0:
                for unsubscribe in self._unsubscribe:
                    unsubscribe()
                self._unsubscribe = []

    async def authenticate(self, request: Request) -> Principal:
        if self.authenticator is None:
            principal = ANONYMOUS
        else:
            principal = await self.authenticator.authenticate(request)
            if not isinstance(principal, Principal):
                raise TypeError(
                    f"{type(self.authenticator).__name__}.authenticate must return a Principal"
                )
        return self.bind_principal(request, principal)

    @staticmethod
    def bind_principal(request: Request, principal: Any) -> Principal:
        """Make `principal` the caller of `request` (for logs, runs and `current_principal`)."""

        if not isinstance(principal, Principal):
            raise TypeError(
                f"The auth dependency must return an awa.server.Principal, got "
                f"{type(principal).__name__}"
            )
        principal_context.set(principal)
        request.state.awa_principal = principal
        return principal

    def owns(self, tenant: str | None, owner: str | None, principal: Principal) -> bool:
        """Whether `principal` may use a thread or run created by `owner` of `tenant`."""

        if self._scope == "shared":
            return True
        if tenant != principal.tenant:
            return False
        return self._scope == "tenant" or owner == principal.id

    @staticmethod
    def _is_reviewer(served: ServedSystem, principal: Principal) -> bool:
        return served.approver_roles is not None and served.may_approve(principal)

    async def _visible_thread(
        self,
        name: str,
        served: ServedSystem,
        thread_id: str,
        principal: Principal,
        *,
        reviewing: bool,
        claim: bool = False,
    ) -> ThreadOwner:
        """The thread's owner; with `claim`, a new thread becomes the caller's.

        Raises:
            ThreadNotFoundError: If the caller may not use the thread.
        """

        owner = await self._index.owner(name, thread_id)
        if owner is None:
            owner = await self._unrecorded_thread(name, served, thread_id, principal, claim=claim)
        allowed = self.owns(owner.tenant, owner.principal, principal) or (
            # Principal scope: reviewers of the tenant may read and answer approvals.
            reviewing and owner.tenant == principal.tenant and self._is_reviewer(served, principal)
        )
        if not allowed:
            raise ThreadNotFoundError(f"System {name!r} has no thread {thread_id!r}")
        return owner

    async def _unrecorded_thread(
        self, name: str, served: ServedSystem, thread_id: str, principal: Principal, *, claim: bool
    ) -> ThreadOwner:
        """The owner of a thread the index has no record of.

        A new thread becomes the caller's with `claim`. An existing one was made elsewhere: only a
        single-caller server or `thread_scope="shared"` treats it as the default tenant's.

        Raises:
            ThreadNotFoundError: If the thread exists and the server has several callers.
        """

        checkpointer = self.checkpointer_for(served)
        state = None if checkpointer is None else await pending_interrupts(checkpointer, thread_id)
        if claim and state is None:
            return await self._claim(name, thread_id, principal)
        if not (self.single_caller or self._scope == "shared"):
            raise ThreadNotFoundError(f"System {name!r} has no thread {thread_id!r}")
        return ThreadOwner(tenant=None, principal=None)

    async def _bind_result_thread(
        self, name: str, thread_id: str | None, principal: Principal
    ) -> None:
        if thread_id is not None:
            await self._claim(name, thread_id, principal)

    async def _claim(self, name: str, thread_id: str, principal: Principal) -> ThreadOwner:
        """Record `principal` as the thread's owner (unless it has one) and link the
        thread to the caller as data subject for `awa.persistence.erase_subject`."""

        owner = await self._index.claim(
            name, thread_id, ThreadOwner(principal.tenant, principal.id)
        )
        if self._subjects is not None and owner.principal == principal.id:
            try:
                await self._subjects.link(principal.id, threads=[thread_id])
            except Exception:  # noqa: BLE001 - the run itself is fine
                _log.exception("Could not link thread %s to its data subject", thread_id)
        return owner

    def checkpointer_for(self, served: ServedSystem) -> BaseCheckpointSaver[Any] | None:
        if system_kind(served.system) in ("agent", "team"):
            # Their threads live in their own checkpointer, or nowhere.
            return system_checkpointer(served.system)
        # Workflows and networks built with a checkpointer persist there.
        return system_checkpointer(served.system) or self._checkpointer

    def reserve(self, name: str, served: ServedSystem, thread_id: str | None) -> Callable[[], None]:
        keys: list[tuple[str, str | None]] = []
        if served.is_exclusive:
            keys.append((name, None))
        if thread_id is not None:
            keys.append((name, thread_id))
        for key in keys:
            if key in self._held:
                if key[1] is None:
                    raise SystemBusyError(
                        f"System {name!r} is already running a request; it runs one at a time. "
                        "Retry when it has finished."
                    )
                raise SystemBusyError(f"Thread {thread_id!r} of system {name!r} is already running")
        self._held.update(keys)
        return lambda: self._held.difference_update(keys)

    async def admit(self, name: str, served: ServedSystem, thread_id: str | None) -> ExitStack:
        """Reserve the thread (and exclusive system) and take a concurrency slot.

        Closing the returned stack frees all of it, once.
        """

        admission = ExitStack()
        admission.callback(self.reserve(name, served, thread_id))
        per_loop = self._slots.get(name)
        if per_loop is None:
            return admission
        slots = per_loop.get()
        try:
            if slots.locked() and served.queue_timeout <= 0:
                raise TimeoutError
            await asyncio.wait_for(slots.acquire(), timeout=served.queue_timeout or None)
        except TimeoutError:
            admission.close()
            raise ServiceUnavailableError(
                f"System {name!r} is running as many requests as it allows "
                f"({served.max_concurrency}); retry shortly",
                retry_after=_BUSY_RETRY_AFTER,
            ) from None
        except BaseException:
            admission.close()
            raise
        admission.callback(slots.release)
        return admission

    def ensure_accepting(self) -> None:
        if self.runs.draining:
            raise ServiceUnavailableError("The server is shutting down; retry on another instance")

    async def check_rate(self, name: str, principal: Principal) -> None:
        key = f"{principal.tenant or ''}/{principal.id}"
        checks = ((self._rate_limiter, key), (self._system_limiters.get(name), f"{key}/{name}"))
        for limiter, bucket in checks:
            if limiter is None:
                continue
            wait = await limiter.acquire(bucket)
            if wait is not None:
                raise RateLimitExceededError(
                    "Too many requests; slow down and retry later", retry_after=wait
                )

    def list_systems(self) -> dict[str, Any]:
        return {
            "systems": [
                {
                    "name": name,
                    "kind": system_kind(served.system),
                    "card": served.system.card.model_dump(mode="json"),
                }
                for name, served in self.systems.items()
            ]
        }

    def describe_system(self, name: str) -> dict[str, Any]:
        served = lookup(self.systems, name)
        system = served.system
        card = system.card
        return {
            "name": name,
            "kind": system_kind(system),
            "card": card.model_dump(mode="json"),
            "input_schema": card.input_schema,
            "output_schema": card.output_schema,
            "topology": jsonable(system_topology(system)),
            "mermaid": system_mermaid(served),
            "accepts_deps": sorted(served.allowed_deps),
            "exclusive": served.is_exclusive,
            "threads": self.checkpointer_for(served) is not None,
            "streaming": callable(getattr(system, "stream", None)),
            "resume": callable(getattr(system, "resume", None)),
            "max_concurrency": served.max_concurrency,
            "approval_requires_role": served.approver_roles is not None,
        }

    async def readiness(self) -> tuple[bool, dict[str, str]]:
        """Whether the server is ready, and the outcome per check; probes run at most once a second."""

        checks: dict[str, str] = {}
        if self.runs.draining:
            checks["server"] = "draining"
        now = time.monotonic()
        probed = self._readiness_probed
        if probed is None or now - probed[0] >= _READINESS_MAX_AGE:
            probed = (now, await self._probe())
            self._readiness_probed = probed
        checks.update(probed[1])
        return all(value == "ok" for value in checks.values()), checks

    async def _probe(self) -> dict[str, str]:
        checks: dict[str, str] = {}
        probes: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        seen: set[int] = set()
        candidates = [("server", self._checkpointer)] + [
            (name, self.checkpointer_for(served)) for name, served in self.systems.items()
        ]
        for label, checkpointer in candidates:
            if checkpointer is not None and id(checkpointer) not in seen:
                seen.add(id(checkpointer))
                probes.append(
                    (
                        f"checkpointer:{label}",
                        partial(pending_interrupts, checkpointer, _READINESS_PROBE_THREAD),
                    )
                )
        probes.append(("thread_index", self._index.ping))
        probes.append(("idempotency", self.idempotency.ping))
        for number, check in enumerate(self._readiness_checks, 1):
            probes.append((getattr(check, "__name__", None) or f"check_{number}", check))
        for label, probe in probes:
            try:
                outcome: Any = await asyncio.wait_for(probe(), timeout=self._readiness_timeout)
            except TimeoutError:
                checks[label] = "timeout"
            except Exception as exc:  # noqa: BLE001 - a failed probe means not ready
                _log.warning("Readiness check %s failed: %s", label, type(exc).__name__)
                checks[label] = "failed"
            else:
                checks[label] = "failed" if outcome is False else "ok"
        return checks

    def _require_checkpointer(self, name: str, served: ServedSystem) -> BaseCheckpointSaver[Any]:
        checkpointer = self.checkpointer_for(served)
        if checkpointer is None:
            raise InvalidRequestError(
                f"System {name!r} keeps no threads: it has no checkpointer, and the server "
                "was given none"
            )
        return checkpointer

    def _visible_run(self, run_id: str, principal: Principal) -> RunHandle:
        handle = self.runs.get(run_id)
        if handle is None or not self.owns(handle.tenant, handle.principal, principal):
            raise RunNotFoundError(f"No run {run_id!r} is known here")
        return handle

    def run_state(self, run_id: str, principal: Principal) -> dict[str, Any]:
        return self._visible_run(run_id, principal).status_json()

    def list_runs(self, principal: Principal, limit: int) -> dict[str, Any]:
        handles = [h for h in self.runs.handles() if self.owns(h.tenant, h.principal, principal)]
        return {"runs": [handle.status_json() for handle in handles[:limit]]}

    async def prepare_run(
        self, name: str, served: ServedSystem, body: RunRequest, principal: Principal
    ) -> RunCall:
        kind = system_kind(served.system)
        # Built-in systems keep threads only in a checkpointer of their own.
        if (
            body.thread_id is not None
            and kind != "custom"
            and system_checkpointer(served.system) is None
        ):
            raise InvalidRequestError(
                f"System {name!r} keeps no threads: it has no checkpointer, so thread_id "
                "cannot be used"
            )
        if body.history and kind in ("network", "workflow"):
            raise InvalidRequestError(
                f"System {name!r} does not take history; send earlier messages as part of input"
            )
        served.check_messages(body.input, body.history)
        return RunCall(
            input=body.input,
            deps=await served.resolve_deps(body.deps, principal),
            thread_id=body.thread_id,
            usage_limits=served.limits_for(body.usage_limits),
            history=body.history,
            metadata=run_metadata(principal),
            timeout=served.run_timeout,
        )

    async def run(
        self, served: ServedSystem, call: RunCall, handle: RunHandle, *, note_ids: bool = False
    ) -> RunResult[Any]:
        """Run to completion under the server's run id, feeding the audit log if any.

        With `note_ids`, the run is also reachable under the run id its events carry.
        """

        with use_run_id(handle.run_id):
            if self._audit_log is None and not note_ids:
                return await served.system.run(call.input, **call.options(served.system.run))
            result: RunResult[Any] | None = None
            async with aclosing(_events(served.system, call)) as events:
                async for event in events:
                    if note_ids:
                        self._note_run_id(handle, event)
                    self._audit(event, call)
                    if isinstance(event, RunCompletedEvent) and event.run_id == event.root_run_id:
                        result = event.result
        if result is None:  # pragma: no cover - every stream ends with its result
            raise RuntimeError("The run ended without a result")
        return result

    def _audit(self, event: RunEvent, call: RunCall) -> None:
        """Hand `event` to the audit log with the request's identity bound."""

        if self._audit_log is None:
            return
        identity: RunContext[None] = RunContext(
            run_id=event.root_run_id,
            agent=event.agent,
            deps=None,
            metadata=dict(call.metadata or {}),
        )
        with use_run_context(identity):
            self._audit_log(event)

    async def start_run(
        self,
        name: str,
        body: RunRequest,
        principal: Principal,
        *,
        stream: bool,
        mode: Literal["sync", "async"],
        idempotency_key: str | None,
        runs_path: str = f"{API_PREFIX}/runs",
    ) -> Response:
        """Start a run; `runs_path` is where the router serves `GET .../runs/{run_id}`."""

        idempotency: IdempotencyClaim | None = None
        try:
            self.ensure_accepting()
            served = lookup(self.systems, name)
            if stream and mode == "async":
                raise InvalidRequestError("A run is either streamed or async, not both")
            if stream and idempotency_key is not None:
                raise InvalidRequestError("Idempotency-Key is not supported for streamed runs")
            await self.check_rate(name, principal)
            call = await self.prepare_run(name, served, body, principal)
            if body.thread_id is not None:
                await self._visible_thread(
                    name, served, body.thread_id, principal, reviewing=False, claim=True
                )
            if idempotency_key is not None:
                idempotency, replay = await self._begin_idempotent(
                    name, body, principal, mode, idempotency_key
                )
                if replay is not None:
                    return replay
            admission = await self.admit(name, served, body.thread_id)
            handle = self._open_run(
                name, "stream" if stream else mode, principal, body.thread_id, admission
            )
        except Exception as exc:
            await _release(idempotency)
            return error_response(exc)
        if stream:
            return self.stream(name, served, call, admission, handle, principal)
        if mode == "async":
            handle.task = asyncio.create_task(
                self._run_async(name, served, call, admission, handle, principal),
                name=f"awa-server-async-run-{name}",
            )
            status, payload, headers = self._accepted(name, handle, runs_path)
        else:
            try:
                status, payload, headers = await self._run_to_end(
                    handle,
                    admission,
                    self.run(served, call, handle),
                    f"awa-server-run-{name}",
                    principal=principal,
                )
            except BaseException:
                # The client went away: a retry with the same key must run again.
                await _release(idempotency)
                raise
        await _finish(idempotency, status, payload, headers)
        return JSONResponse(payload, status_code=status, headers=headers)

    async def _begin_idempotent(
        self, name: str, body: RunRequest, principal: Principal, mode: str, key: str
    ) -> tuple[IdempotencyClaim, Response | None]:
        """Claim `key` for this request; the stored response when it was answered before."""

        claim = self._idempotency_claim(name, body, principal, mode, key)
        stored = await claim.begin(
            self.idempotency_ttl,
            mismatch="This Idempotency-Key was used for a different request; use a new key",
            conflict="A request with this Idempotency-Key is still being processed; retry later",
        )
        if stored is None:
            return claim, None
        headers = {**stored.headers, "Idempotent-Replayed": "true"}
        return claim, JSONResponse(stored.body, status_code=stored.status or 200, headers=headers)

    def _open_run(
        self,
        name: str,
        mode: RunMode,
        principal: Principal,
        thread_id: str | None,
        admission: ExitStack,
    ) -> RunHandle:
        """Register a new run of the caller; frees `admission` if the server refuses it."""

        handle = RunHandle(
            run_id=new_run_id(),
            system=name,
            mode=mode,
            tenant=principal.tenant,
            principal=principal.id,
            request_id=current_request_id(),
            thread_id=thread_id,
        )
        try:
            self.runs.start(handle)
        except BaseException:
            admission.close()
            raise
        return handle

    async def _run_to_end(
        self,
        handle: RunHandle,
        admission: ExitStack,
        work: Coroutine[Any, Any, RunResult[Any]],
        task_name: str,
        *,
        principal: Principal | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Run `work` as the run's task; the answer is its result or error.

        With `principal`, the run is also reachable under its result's run id and
        a new thread of the result becomes the caller's.
        """

        task = asyncio.create_task(work, name=task_name)
        handle.task = task
        try:
            result = await task
            if principal is not None:
                self.runs.add_id(handle, result.run_id)
                await self._bind_result_thread(handle.system, result.thread_id, principal)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if handle.cancel_error is None or (current is not None and current.cancelling()):
                raise
            return error_parts(handle.cancel_error)
        except Exception as exc:
            return error_parts(handle.reported(exc))
        finally:
            admission.close()
            self.runs.finish(handle, "completed")
        return 200, _run_body(result), {}

    @staticmethod
    def _accepted(
        name: str, handle: RunHandle, runs_path: str
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        status_url = f"{runs_path}/{handle.run_id}"
        payload = {
            "run_id": handle.run_id,
            "status": "running",
            "system": name,
            "thread_id": handle.thread_id,
            "request_id": handle.request_id,
            "status_url": status_url,
        }
        return 202, payload, {"Location": status_url}

    def _note_run_id(self, handle: RunHandle, event: RunEvent) -> None:
        # Systems that are not awa runnables may not use the server's run id:
        # make the run reachable under the id its events carry too.
        if event.depth == 0 and event.run_id not in handle.ids:
            self.runs.add_id(handle, event.run_id)

    async def _run_async(
        self,
        name: str,
        served: ServedSystem,
        call: RunCall,
        admission: ExitStack,
        handle: RunHandle,
        principal: Principal,
    ) -> None:
        try:
            result = await self.run(served, call, handle, note_ids=True)
            await self._bind_result_thread(name, result.thread_id, principal)
        except asyncio.CancelledError:
            error = handle.cancel_error or RunCancelledError(
                "The run was cancelled", run_id=handle.run_id
            )
            self.runs.finish(
                handle, "cancelled", error=describe_error(error).body(current_request_id())
            )
            raise
        except Exception as exc:
            info = describe_and_log(handle.reported(exc))
            self.runs.finish(handle, handle.outcome(exc), error=info.body(handle.request_id))
        else:
            self.runs.add_id(handle, result.run_id)
            handle.thread_id = result.thread_id
            self.runs.finish(
                handle,
                "interrupted" if result.is_interrupted else "completed",
                result=_run_body(result, handle.request_id),
            )
        finally:
            admission.close()

    def _idempotency_claim(
        self, name: str, body: RunRequest, principal: Principal, mode: str, key: str
    ) -> IdempotencyClaim:
        request = {"system": name, "mode": mode, "body": body.model_dump(mode="json")}
        return IdempotencyClaim(
            self.idempotency,
            scope(principal.tenant, principal.id),
            key,
            fingerprint(request),
            "a request with an Idempotency-Key",
        )

    async def cancel_run(self, run_id: str, principal: Principal) -> dict[str, Any]:
        handle = self._visible_run(run_id, principal)
        error = RunCancelledError(
            f"Run {handle.run_id!r} was cancelled by {principal.id!r}",
            run_id=handle.run_id,
            reason="cancelled via the API",
            cancelled_by=principal.id,
        )
        if handle.cancel(error, reason="cancelled via the API", cancelled_by=principal.id):
            _audit.info(
                "Run %s of system %s cancelled by %s",
                handle.run_id,
                handle.system,
                principal.id,
                extra={
                    "event": "run_cancelled",
                    "run_id": handle.run_id,
                    "system": handle.system,
                    "by": principal.describe(),
                },
            )
            task = handle.task
            if task is not None:
                await asyncio.wait({task}, timeout=_CANCEL_WAIT)
        return handle.status_json()

    async def drain(self, timeout: float | None) -> int:
        cancelled = await self.runs.drain(timeout)
        if cancelled:
            _log.warning("Cancelled %d run(s) still in flight at shutdown", cancelled)
        return cancelled

    def stream(
        self,
        name: str,
        served: ServedSystem,
        call: RunCall,
        admission: ExitStack,
        handle: RunHandle,
        principal: Principal,
    ) -> Response:
        def closed() -> None:
            admission.close()
            if not handle.done:
                self.runs.finish(handle, "cancelled")

        return _EventStreamResponse(
            self._sse(name, served, call, admission, handle, principal),
            on_close=closed,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _pump(
        self,
        buffer: _EventBuffer,
        name: str,
        served: ServedSystem,
        call: RunCall,
        handle: RunHandle,
        principal: Principal,
    ) -> None:
        """Feed the events of the run into `buffer`; end it with `done` or an `error`."""

        try:
            with use_run_id(handle.run_id):
                async with aclosing(_events(served.system, call)) as events:
                    async for event in events:
                        self._note_run_id(handle, event)
                        self._audit(event, call)
                        if isinstance(event, RunCompletedEvent):
                            await self._bind_result_thread(name, event.result.thread_id, principal)
                        if not buffer.offer(event):
                            raise _Overflow
        except _Overflow:
            overflow = ServiceUnavailableError(
                "The client did not read the event stream fast enough; the run was cancelled"
            )
            buffer.close("error", overflow, discard=True)
        except asyncio.CancelledError:
            if handle.cancel_error is not None:
                buffer.close("error", handle.cancel_error, discard=True)
            raise
        except Exception as exc:
            buffer.close("error", handle.reported(exc))
        else:
            buffer.close("done")

    async def _sse(
        self,
        name: str,
        served: ServedSystem,
        call: RunCall,
        admission: ExitStack,
        handle: RunHandle,
        principal: Principal,
    ) -> AsyncIterator[bytes]:
        buffer = _EventBuffer(self._sse_queue_size, self._sse_overflow)

        producer = asyncio.create_task(
            self._pump(buffer, name, served, call, handle, principal),
            name=f"awa-server-stream-{name}",
        )
        handle.task = producer
        fields: dict[str, Any] = {}
        reported = 0
        status: RunStatus = "cancelled"
        try:
            while True:
                try:
                    kind, value = await asyncio.wait_for(buffer.get(), timeout=self.ping_interval)
                except TimeoutError:
                    yield SSE_PING
                    continue
                if buffer.dropped > reported:
                    yield f": dropped {buffer.dropped - reported} text_delta events\n\n".encode()
                    reported = buffer.dropped
                if kind == "done":
                    return
                if kind == "error":
                    info = describe_and_log(value)
                    status = handle.outcome(value)
                    if not fields:
                        run_id = new_run_id()
                        fields = {
                            "run_id": run_id,
                            "root_run_id": run_id,
                            "depth": 0,
                            "agent": served.system.name,
                        }
                    yield sse_frame("error", terminal_error_json(info, fields, handle.request_id))
                    return
                event: RunEvent = value
                if event.depth == 0 and event.run_id == event.root_run_id:
                    fields = {
                        "run_id": event.run_id,
                        "root_run_id": event.root_run_id,
                        "agent": event.agent,
                        "depth": 0,
                    }
                    if isinstance(event, ErrorEvent):
                        # Replaced by the terminal error event, which carries the status.
                        continue
                data = event_json(event)
                if isinstance(event, RunCompletedEvent):
                    data["result"]["request_id"] = handle.request_id
                    status = "interrupted" if event.result.is_interrupted else "completed"
                yield sse_frame(event.type, data)
        finally:
            admission.close()
            producer.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(producer, return_exceptions=True)
            self.runs.finish(handle, status)

    async def resume(
        self, name: str, thread_id: str, body: ResumeRequest, principal: Principal
    ) -> tuple[
        ExitStack,
        Callable[[str], Coroutine[Any, Any, RunResult[Any]]],
        Callable[[], Coroutine[Any, Any, None]],
    ]:
        """Claim the paused state of the thread for this decision.

        Returns the admission, the resumed run, and `release`, which gives the claim back unless
        the resumed run has succeeded (call it when the run never started or did not finish).
        """

        self.ensure_accepting()
        served = lookup(self.systems, name)
        resume = getattr(served.system, "resume", None)
        if not callable(resume):
            raise InvalidRequestError(f"System {name!r} does not support resuming runs")
        await self.check_rate(name, principal)
        self._require_checkpointer(name, served)
        owner = await self._visible_thread(name, served, thread_id, principal, reviewing=True)
        if not served.may_approve(principal):
            raise PermissionDeniedError(
                f"Resuming runs of system {name!r} needs one of the roles "
                f"{', '.join(sorted(served.approver_roles or ()))}"
            )
        # Reserve the thread before checking that it waits for a decision, so two
        # concurrent resumes cannot both pass the check and answer it twice.
        admission = await self.admit(name, served, thread_id)
        try:
            pending, version = await self._require_pending(name, served, thread_id)
            # The run goes on for the thread's owner; the reviewer is recorded
            # in the decision.
            subject = principal
            if owner.principal is not None and owner.principal != principal.id:
                subject = Principal(owner.principal, tenant=owner.tenant)
            deps = await served.resolve_deps(body.deps, subject)
            limits = served.limits_for(body.usage_limits)
            # Across workers too: only one resume answers this paused state.
            await claim_paused_state(self._decisions, name, thread_id, version)
        except BaseException:
            admission.close()
            raise
        pending_ids = [item.get("id") for item in pending]
        decision = stamp_reviewer(body.decision, served.reviewer_key, pending_ids, principal)
        # The reviewer is the actor of the resumed run.
        options = call_options(
            resume,
            deps=deps,
            usage_limits=limits,
            metadata=run_metadata(principal),
            timeout=served.run_timeout,
        )
        self.record(
            "approval_submitted",
            actor=principal.id,
            tenant=principal.tenant,
            system=name,
            thread_id=thread_id,
            reviewer=principal.describe(),
            decision=_decision_summary(body.decision),
            interrupts=[item.get("id") for item in pending],
        )

        settled = False  # the claim was given back, or the resume succeeded and keeps it

        async def release() -> None:
            nonlocal settled
            if not settled:
                settled = True
                await self._decisions.release_decision(name, thread_id, version)

        async def resumed(run_id: str) -> RunResult[Any]:
            nonlocal settled
            try:
                with use_run_id(run_id):
                    result: RunResult[Any] = await resume(thread_id, decision, **options)
            except BaseException:
                # A failed resume may be retried; a successful one keeps its claim.
                await release()
                raise
            settled = True
            return result

        return admission, resumed, release

    def record(self, event: str, *, actor: str | None, tenant: str | None, **data: Any) -> None:
        """Log `event` to `awa.server.audit` and record it in the audit log, if it keeps records."""

        _audit.info(
            "%s by %s",
            event,
            actor,
            extra={"event": event, "actor": actor, "tenant": tenant, **data},
        )
        log = self._audit_log
        if log is None or not hasattr(log, "record"):
            return
        try:
            log.record(event, data, actor=actor, tenant=tenant, request_id=current_request_id())
        except Exception:  # noqa: BLE001 - auditing must not stop the request
            _log.exception("Could not write %s to the audit log", event)

    async def run_resume(
        self, name: str, thread_id: str, body: ResumeRequest, principal: Principal
    ) -> Response:
        try:
            admission, resumed, release = await self.resume(name, thread_id, body, principal)
        except Exception as exc:
            return error_response(exc)
        # From here on the claim is given back whenever the resumed run does not succeed (the
        # server refuses the run, or the request is cancelled before the run starts), so the
        # paused state can still be answered.
        try:
            handle = self._open_run(name, "sync", principal, thread_id, admission)
        except BaseException as exc:
            await release()
            if isinstance(exc, Exception):
                return error_response(exc)
            raise
        work = resumed(handle.run_id)
        try:
            status, payload, headers = await self._run_to_end(
                handle, admission, work, f"awa-server-resume-{name}"
            )
        except BaseException:
            work.close()  # a run that never started: no "never awaited" warning
            await release()
            raise
        return JSONResponse(payload, status_code=status, headers=headers or None)

    async def interrupts(self, name: str, thread_id: str, principal: Principal) -> dict[str, Any]:
        served = lookup(self.systems, name)
        checkpointer = self._require_checkpointer(name, served)
        await self._visible_thread(name, served, thread_id, principal, reviewing=True)
        pending = await pending_interrupts(checkpointer, thread_id)
        if pending is None:
            raise ThreadNotFoundError(f"System {name!r} has no thread {thread_id!r}")
        return {"thread_id": thread_id, "interrupts": http_interrupts(pending, thread_id)}

    async def list_threads(self, name: str, principal: Principal, limit: int) -> dict[str, Any]:
        served = lookup(self.systems, name)
        self._require_checkpointer(name, served)
        only_own = self._scope == "principal" and not self._is_reviewer(served, principal)
        records = await self._index.threads(
            name, tenant=principal.tenant, principal=principal.id if only_own else None, limit=limit
        )
        return {
            "threads": [
                {
                    "thread_id": record.thread_id,
                    "principal": record.owner.principal,
                    "created_at": record.owner.created_at.isoformat(),
                }
                for record in records
            ]
        }

    async def _require_pending(
        self, name: str, served: ServedSystem, thread_id: str
    ) -> tuple[list[dict[str, Any]], str]:
        checkpointer = self._require_checkpointer(name, served)
        state = await pending_state(checkpointer, thread_id)
        if state is None:
            raise ThreadNotFoundError(f"System {name!r} has no thread {thread_id!r}")
        if not state.interrupts:
            raise NoPendingInterruptError(
                f"Thread {thread_id!r} of system {name!r} is not waiting for a decision"
            )
        return state.interrupts, state.version
