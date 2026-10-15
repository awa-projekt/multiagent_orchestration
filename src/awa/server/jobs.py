"""Extension: background jobs, run agent systems on the queue a company already has.

A `JobSpec` is a run as a JSON message; `execute` or `execute_sync` runs it in any worker (Celery,
RQ, Arq, a cloud function, a thread pool) and returns a JSON `JobResult`. Jobs follow the HTTP
server's rules (`ServedSystem` limits, idempotency caches); a `WebhookCallback` posts the signed
result. Queue recipes, webhook headers and retries are in the integration guide.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from awa._internal import aio as asyncio_compat
from awa._internal.logging import get_logger
from awa._internal.util import call_factory, elapsed_ms
from awa.contracts.protocols import AgentLike
from awa.contracts.results import RunResult
from awa.contracts.usage import UsageLimits
from awa.core import webhooks
from awa.core.config import env_value
from awa.core.webhooks import (
    DEFAULT_TOLERANCE,
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from awa.errors import (
    AdapterError,
    InvalidRequestError,
    JobFailedError,
    MissingConfigurationError,
    RunCancelledError,
    RunTimeoutError,
    StepRejectedError,
    SystemNotFoundError,
)
from awa.observability.audit import ACTOR_METADATA_KEY, TENANT_METADATA_KEY
from awa.persistence.threads import (
    BaseIdempotencyCache,
    BaseThreadIndex,
    DecisionClaims,
    IdempotencyRecord,
)
from awa.server._auth import Principal
from awa.server._config import (
    THREAD_ID_PATTERN,
    ServedSystem,
    check_system_name,
    system_checkpointer,
)
from awa.server._requests import call_options
from awa.server._serialize import pending_state, result_json
from awa.server._stores import (
    IDEMPOTENCY_KEY_PATTERN,
    IdempotencyClaim,
    InMemoryThreadIndex,
    claim_paused_state,
    decision_claims,
    fingerprint,
    scope,
)
from awa.server.errors import describe_error

_log = get_logger("jobs")


def sign_webhook(secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    """The `X-Awa-Timestamp` and `X-Awa-Signature` headers for the exact `body`."""

    return webhooks.sign(secret, body, timestamp=timestamp)


def verify_webhook(
    body: bytes,
    headers: Mapping[str, str],
    secret: str | Sequence[str],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> None:
    """Check the signature and timestamp of a received awa webhook.

    Pass the raw body (before any JSON parsing); several secrets are accepted during a key
    rotation. Remember the `X-Awa-Delivery` ids you processed for at least `tolerance` seconds,
    to process a retried delivery once.

    Raises:
        WebhookVerificationError: If a header is missing, the timestamp is
            outside the window, or no secret produces the signature.

    Example:
        ```python
        @app.post("/hooks/awa")
        async def awa_callback(request: Request) -> dict[str, str]:
            body = await request.body()
            verify_webhook(body, request.headers, os.environ["AWA_HOOK_SECRET"])
            result = JobResult.model_validate_json(body)
            ...
        ```
    """

    webhooks.verify(body, headers, secret, tolerance=tolerance, now=now)


class WebhookCallback(BaseModel):
    """Post a job's `JobResult` as signed JSON to a URL when the job ends.

    Name the secret with `secret_env` (read in the worker) whenever the job crosses a queue; a
    literal `secret` is for jobs executed in the same process and is refused when the job is
    serialized. `headers` carry no credentials (they travel with the job); `retries` are extra
    attempts, `backoff` the seconds before the first one, doubled after each; `allow_http`
    permits a plain `http://` URL for local tests.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=8, max_length=2000)
    secret: SecretStr | None = None
    secret_env: str | None = Field(default=None, min_length=1, max_length=200)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=10.0, gt=0, le=300)
    retries: int = Field(default=3, ge=0, le=10)
    backoff: float = Field(default=1.0, ge=0, le=60)
    allow_http: bool = False

    @model_validator(mode="after")
    def _check(self) -> WebhookCallback:
        if not webhooks.url_allowed(self.url, allow_http=self.allow_http):
            raise ValueError(
                "A webhook callback needs an https:// URL (allow_http for local tests)"
            )
        if self.secret is not None and not self.secret.get_secret_value():
            raise ValueError("A webhook secret must not be empty")
        return self

    @field_serializer("secret")
    def _never_serialise(self, value: SecretStr | None) -> None:
        if value is not None:
            raise ValueError(
                "A WebhookCallback with a literal secret cannot be serialized; "
                "use secret_env so the worker reads the secret from its environment"
            )
        return None

    def signing_secret(self) -> str | None:
        """The secret requests are signed with (`secret_env` first), or `None` for unsigned.

        Raises:
            MissingConfigurationError: If `secret_env` names an unset variable.
        """

        if self.secret_env is not None:
            value = (env_value(self.secret_env) or "").strip()
            if value:
                return value
            if self.secret is None:
                raise MissingConfigurationError(
                    f"Webhook callback: environment variable {self.secret_env} is not set"
                )
        return self.secret.get_secret_value() if self.secret is not None else None

    def request(
        self, result: JobResult, *, timestamp: int | None = None
    ) -> tuple[bytes, dict[str, str]]:
        """The body and headers of the delivery of `result` (for tests and custom transports)."""

        body, headers = self._unsigned(result)
        secret = self.signing_secret()
        if secret is not None:
            headers.update(sign_webhook(secret, body, timestamp=timestamp))
        return body, headers

    async def deliver(self, result: JobResult) -> None:
        """Post `result`, retrying transient failures (each attempt signed anew).

        Raises:
            WebhookDeliveryError: If the receiver refused it or every attempt failed.
            MissingConfigurationError: If `secret_env` names an unset variable.
        """

        secret = self.signing_secret()
        body, headers = self._unsigned(result)
        status = await webhooks.post_json(
            self.url,
            body,
            headers=headers,
            secret=secret,
            retries=self.retries,
            timeout=self.timeout,
            backoff=self.backoff,
            what="Webhook callback",
        )
        _log.info("Delivered job %s (%s)", result.job_id, status)

    def _unsigned(self, result: JobResult) -> tuple[bytes, dict[str, str]]:
        body = result.model_dump_json().encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "awa-jobs",
            **self.headers,
            DELIVERY_HEADER: f"{result.job_id}-{hashlib.sha256(body).hexdigest()[:16]}",
            EVENT_HEADER: f"job.{result.status}",
        }
        return body, headers


JobStatus: TypeAlias = Literal["completed", "interrupted", "failed", "cancelled", "rejected"]
"""How a job ended: its run finished, paused for a human decision, failed, was cancelled, or
was rejected (a special case or a person turned the item down)."""

JOB_ID_METADATA_KEY = "awa.job_id"
"""`RunContext.metadata` key holding the id of the job that started the run."""


def new_job_id() -> str:
    """A fresh job id (`job_<32 hex>`)."""

    return f"job_{uuid4().hex}"


class JobSpec(BaseModel):
    """A run of an agent system, as a JSON message any queue can carry.

    Put `spec.model_dump(mode="json")` on your queue and call `execute` with it in the worker.

    Attributes:
        system: A name registered in the worker's `JobRegistry`, or an import path
            (`"myco.agents:support_team"`) when the worker allows imports.
        input: A prompt or a list of `{"role": ..., "content": ...}` messages; required unless
            the job resumes a thread.
        deps: JSON run dependencies. A `ServedSystem` accepts only its `allowed_deps` and builds
            the run's deps with its `deps_factory`; a bare system receives the dict itself.
        metadata: Values for `RunContext.metadata`; `awa.job_id`, `awa.tenant` and `awa.actor`
            are added.
        decision: Resume the paused run of `thread_id` with this human decision
            (`{"action": "approve"}`) instead of starting a run.
        timeout: Seconds the run may take (capped by `ServedSystem.run_timeout`); then the job
            fails with `RunTimeoutError` (retryable).
        idempotency_key: Runs the job at most once per key while the worker's
            `idempotency_cache` remembers it; a redelivery gets the stored result.
        actor: Who asked for the job. Required for a system with `principal_deps`; without it
            the job fails instead of running without them.
        owner: On a decision job, the thread owner's principal id: `principal_deps` derive from
            it and `tenant`, while `actor` (the reviewer) stays the run's actor. Defaults to
            `actor`.

    Example:
        ```python
        spec = JobSpec(
            system="support",
            input="Customer 1042 asks where order A-7 is.",
            idempotency_key="ticket-5512",
            callback=WebhookCallback(url="https://crm.example.com/hooks/awa", secret_env="AWA_HOOK_SECRET"),
        )
        send_task("awa.run_job", args=[spec.model_dump(mode="json")])
        ```
    """

    model_config = ConfigDict(extra="forbid")

    system: str = Field(min_length=1, max_length=500)
    input: str | list[dict[str, Any]] | None = None
    history: list[dict[str, Any]] | None = None
    deps: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    thread_id: str | None = Field(default=None, pattern=THREAD_ID_PATTERN)
    decision: Any = None
    timeout: float | None = Field(default=None, gt=0)
    usage_limits: UsageLimits | None = None
    idempotency_key: str | None = Field(default=None, pattern=IDEMPOTENCY_KEY_PATTERN)
    job_id: str = Field(default_factory=new_job_id, pattern=THREAD_ID_PATTERN)
    tenant: str | None = Field(default=None, min_length=1, max_length=200)
    actor: str | None = Field(default=None, min_length=1, max_length=200)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    callback: WebhookCallback | None = None

    @field_validator("input")
    @classmethod
    def _check_input(cls, value: str | list[dict[str, Any]] | None) -> Any:
        if value is not None and not value:
            raise ValueError("input must not be empty")
        return value

    @model_validator(mode="after")
    def _check_mode(self) -> JobSpec:
        if self.decision is None and self.input is None:
            raise ValueError("A job needs an input (or a decision to resume a thread)")
        if self.decision is not None:
            if self.thread_id is None:
                raise ValueError("A job with a decision resumes a thread: set thread_id")
            if self.input is not None or self.history:
                raise ValueError("A job resumes a thread or starts a run, not both")
        return self

    @property
    def resumes(self) -> bool:
        """Whether the job resumes a paused thread instead of starting a run."""

        return self.decision is not None

    def fingerprint(self) -> str:
        """Digest of what the job asks for (job id, callback and metadata left out)."""

        return fingerprint(self.model_dump(mode="json", exclude={"job_id", "callback", "metadata"}))


class JobResult(BaseModel):
    """How a job ended, as JSON: the queue task's return value and the callback's body.

    Attributes:
        result: The run's result in the shape of the HTTP API; `None` when the job failed.
        error: `type`, `code`, `message` and `retryable` of a failure; messages of unexpected
            errors are hidden (the worker logs them).
        replayed: The stored result of an earlier execution of the same idempotency key.
        callback_delivered: Whether the webhook callback was delivered (`None` without one).
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    system: str
    status: JobStatus
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    thread_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = 0
    replayed: bool = False
    callback_delivered: bool | None = None
    callback_error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the run completed or paused as designed (not failed, cancelled or rejected)."""

        return self.status in ("completed", "interrupted")

    @property
    def text(self) -> str:
        """The text of the final answer (empty when the job failed)."""

        return str((self.result or {}).get("text") or "")

    @property
    def output(self) -> Any:
        """The run's output (structured output as JSON), or `None`."""

        return (self.result or {}).get("output")

    @property
    def interrupts(self) -> list[dict[str, Any]]:
        """The human decisions a paused run waits for."""

        return list((self.result or {}).get("interrupts") or [])

    @property
    def retryable(self) -> bool:
        """Whether running the same job again later may succeed (a timeout, an overloaded model)."""

        return bool((self.error or {}).get("retryable"))

    def raise_for_status(self) -> JobResult:
        """Return the result, or raise so the queue's own retry policy acts on the failure.

        Raises:
            JobFailedError: If `status` is `failed`, `cancelled` or `rejected`.
        """

        if self.ok:
            return self
        error = self.error or {}
        raise JobFailedError(
            f"Job {self.job_id!r} of system {self.system!r} {self.status}: "
            f"{error.get('code', 'awa.error')}: {error.get('message', '')}",
            result=self,
        )


SystemSource: TypeAlias = (
    AgentLike
    | ServedSystem
    | Callable[[], AgentLike | ServedSystem | Awaitable[AgentLike | ServedSystem]]
)
"""A system, a `ServedSystem`, or a function (sync or async) building one on first use."""


@dataclass(frozen=True)
class RegisteredSystem:
    """A system resolved for a job: the system and the rules it is served with."""

    name: str
    served: ServedSystem
    bare: bool
    """Registered without a `ServedSystem`: job deps are passed through unchanged."""


class JobRegistry:
    """The systems a worker runs jobs for.

    Each system is built once, on first use, and reused. A `ServedSystem` applies the HTTP
    server's rules (accepted deps, `deps_factory`, usage ceiling, `run_timeout`).

    Args:
        systems: Name to system, `ServedSystem`, or function building one.
        allow_imports: Whether jobs may name systems by import path: `True` for any module, or
            the allowed module prefixes. Importing runs module code, so allow only your own.
        thread_index: Lets two decision jobs for the same pause apply it once (the second fails
            with `SystemBusyError`); pass a `PostgresThreadIndex` for several workers.

    Example:
        ```python
        registry = JobRegistry(
            {
                "support": lambda: awa.load("support.yaml", tools=load_tools("tools.py")),
                "invoices": ServedSystem(invoice_team, deps_factory=InvoiceDeps.from_json),
            }
        )
        result = await execute(payload, registry)
        ```
    """

    def __init__(
        self,
        systems: Mapping[str, SystemSource] | None = None,
        *,
        allow_imports: bool | Sequence[str] = False,
        thread_index: BaseThreadIndex | None = None,
    ) -> None:
        if isinstance(allow_imports, str):
            raise TypeError("allow_imports takes a list of module prefixes, not a string")
        self.decisions = decision_claims(
            thread_index if thread_index is not None else InMemoryThreadIndex()
        )
        self._sources: dict[str, SystemSource] = {}
        self._built: dict[str, RegisteredSystem] = {}
        self._lock = threading.Lock()
        self._allow_imports: bool | tuple[str, ...] = (
            allow_imports if isinstance(allow_imports, bool) else tuple(allow_imports)
        )
        for name, source in (systems or {}).items():
            self.register(name, source)

    def register(self, name: str, system: SystemSource) -> None:
        """Make `system` available to jobs as `name` (a new, valid name)."""

        check_system_name(name)
        if name in self._sources:
            raise ValueError(f"A system named {name!r} is already registered")
        if not (isinstance(system, AgentLike | ServedSystem) or callable(system)):
            raise TypeError(
                f"Expected a system, ServedSystem or factory, got {type(system).__name__}"
            )
        self._sources[name] = system

    @property
    def names(self) -> list[str]:
        """The registered names, in registration order."""

        return list(self._sources)

    def allows_import(self, path: str) -> bool:
        """Whether jobs may name `path` (an import path) as their system."""

        if isinstance(self._allow_imports, bool):
            return self._allow_imports
        module = path.partition(":")[0]
        return any(
            module == prefix or module.startswith(prefix + ".") for prefix in self._allow_imports
        )

    async def resolve(self, ref: str) -> RegisteredSystem:
        """The system a job names, built on first use.

        Raises:
            SystemNotFoundError: If `ref` is neither registered nor an importable allowed path.
        """

        built = self._built.get(ref)
        if built is not None:
            return built
        source = self._sources.get(ref)
        if source is None:
            source = self._import(ref)
        # Workers may resolve from several threads, each with its own event
        # loop; a system built twice at once is kept once.
        built = await _build(ref, source)
        with self._lock:
            return self._built.setdefault(ref, built)

    def _import(self, ref: str) -> SystemSource:
        from awa._internal.util import import_path

        if (":" not in ref and "." not in ref) or not self.allows_import(ref):
            known = ", ".join(self._sources) or "none"
            hint = "" if self._allow_imports else " (import paths are not allowed here)"
            raise SystemNotFoundError(
                f"No system {ref!r} is registered for jobs{hint}; registered: {known}"
            )
        try:
            return import_path(ref)  # type: ignore[no-any-return]  # a system of any kind
        except AdapterError as exc:
            raise SystemNotFoundError(f"Cannot import the job system {ref!r}: {exc}") from None


async def _build(name: str, source: SystemSource) -> RegisteredSystem:
    value = await call_factory(source, (AgentLike, ServedSystem))
    if isinstance(value, ServedSystem):
        return RegisteredSystem(name, value, bare=False)
    if isinstance(value, AgentLike):
        return RegisteredSystem(name, ServedSystem(value), bare=True)
    raise TypeError(
        f"The job system {name!r} must be an AgentLike or ServedSystem (or build one), "
        f"got {type(value).__name__}"
    )


JobInput: TypeAlias = JobSpec | Mapping[str, Any] | str | bytes
"""A job: a `JobSpec`, or its JSON (a dict as a queue delivers it, or a string)."""

CompletionHook: TypeAlias = Callable[[JobResult], Awaitable[None] | None]
"""Called with every result (replays too), for example to store it; may be async."""

DEFAULT_IDEMPOTENCY_TTL = 24 * 3600.0
"""Seconds an idempotency key is remembered unless `execute` is told otherwise."""

_IDEMPOTENCY_SCOPE = "awa.jobs"


async def execute(
    job: JobInput,
    registry: JobRegistry | None = None,
    *,
    idempotency_cache: BaseIdempotencyCache | None = None,
    idempotency_ttl: float = DEFAULT_IDEMPOTENCY_TTL,
    on_complete: CompletionHook | None = None,
) -> JobResult:
    """Run one job and return how it ended, ready to serialize.

    Runs the system the job names, posts the result to the job's `callback` and returns a
    `JobResult`. A failing run does not raise: the result has `status="failed"` and an `error`
    with `retryable` (`result.raise_for_status()` hands it to the queue's retry policy). With
    `idempotency_cache`, a job with an `idempotency_key` runs at most once while the key is
    remembered; retryable failures are not stored.

    Args:
        registry: The systems jobs may name; without one, none (a job naming an import path
            needs `JobRegistry(allow_imports=...)`, only for messages you trust).
        idempotency_cache: Where results of keyed jobs are kept; without it keys are not
            enforced.
        on_complete: Called with the result (replays too), after the callback.

    Raises:
        InvalidRequestError: If `job` is not a valid job.
        IdempotencyConflictError: If a job with the same key is still
            running (retryable: try again later).
        IdempotencyMismatchError: If the key was used for a different job.

    Example:
        ```python
        registry = JobRegistry({"support": build_support_team})

        async def handle_message(payload: dict[str, Any]) -> dict[str, Any]:
            result = await execute(payload, registry)
            return result.model_dump(mode="json")
        ```
    """

    spec = _parse(job)
    if idempotency_ttl <= 0:
        raise ValueError("idempotency_ttl must be positive")
    claim: IdempotencyClaim | None = None
    stored: IdempotencyRecord | None = None
    if idempotency_cache is not None and spec.idempotency_key is not None:
        claim = IdempotencyClaim(
            idempotency_cache,
            scope(_IDEMPOTENCY_SCOPE, spec.tenant),
            spec.idempotency_key,
            spec.fingerprint(),
            f"job {spec.job_id}",
        )
        stored = await claim.begin(
            idempotency_ttl,
            mismatch=f"The idempotency key {spec.idempotency_key!r} was used for a different job",
            conflict=(
                f"A job with the idempotency key {spec.idempotency_key!r} is still running; "
                "retry later"
            ),
        )
    if claim is not None and stored is not None:
        result = await _replay(claim, spec, JobResult.model_validate(stored.body))
    else:
        try:
            result = await _run_job(spec, registry if registry is not None else JobRegistry())
            if spec.callback is not None:
                result = await _deliver(spec.callback, result)
        except BaseException:
            if claim is not None:
                await claim.release()
            raise
        if claim is not None:
            # A failure a retry may overcome is not stored: the retry runs again.
            await claim.finish(
                200, result.model_dump(mode="json"), retryable=not result.ok and result.retryable
            )
    if on_complete is not None:
        outcome = on_complete(result)
        if inspect.isawaitable(outcome):
            await outcome
    return result


def execute_sync(
    job: JobInput,
    registry: JobRegistry | None = None,
    *,
    idempotency_cache: BaseIdempotencyCache | None = None,
    idempotency_ttl: float = DEFAULT_IDEMPOTENCY_TTL,
    on_complete: CompletionHook | None = None,
) -> JobResult:
    """Run one job from synchronous code (no running event loop); see `execute`.

    Example:
        ```python
        @celery_app.task
        def run_job(payload: dict[str, Any]) -> dict[str, Any]:
            return execute_sync(payload, registry).model_dump(mode="json")
        ```
    """

    return asyncio_compat.run(
        execute(
            job,
            registry,
            idempotency_cache=idempotency_cache,
            idempotency_ttl=idempotency_ttl,
            on_complete=on_complete,
        )
    )


def _parse(job: JobInput) -> JobSpec:
    if isinstance(job, JobSpec):
        return job
    try:
        if isinstance(job, str | bytes):
            return JobSpec.model_validate_json(job)
        if isinstance(job, Mapping):
            return JobSpec.model_validate(dict(job))
    except ValidationError as exc:
        raise InvalidRequestError(f"Invalid job: {exc}") from None
    raise TypeError(f"Expected a JobSpec or its JSON, got {type(job).__name__}")


async def _run_job(spec: JobSpec, registry: JobRegistry) -> JobResult:
    started = datetime.now(UTC)
    clock = time.perf_counter()
    status: JobStatus
    body: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        entry = await registry.resolve(spec.system)
        run = await _start(entry, spec, registry.decisions)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported in the result
        info = describe_error(exc)
        if info.internal:
            _log.error("Job %s of system %s failed", spec.job_id, spec.system, exc_info=exc)
        else:
            _log.info("Job %s of system %s failed: %s", spec.job_id, spec.system, exc)
        status = _failed_status(exc)
        error = {k: v for k, v in info.body().items() if k != "request_id"}
    else:
        body = result_json(run)
        status = "interrupted" if run.is_interrupted else "completed"
    return JobResult(
        job_id=spec.job_id,
        system=spec.system,
        status=status,
        result=body,
        error=error,
        thread_id=(body or {}).get("thread_id") or spec.thread_id,
        idempotency_key=spec.idempotency_key,
        metadata=spec.metadata,
        started_at=started,
        finished_at=datetime.now(UTC),
        duration_ms=elapsed_ms(clock),
    )


def _failed_status(exc: Exception) -> JobStatus:
    if isinstance(exc, RunCancelledError):
        return "cancelled"
    if isinstance(exc, StepRejectedError):
        return "rejected"
    return "failed"


async def _start(entry: RegisteredSystem, spec: JobSpec, index: DecisionClaims) -> RunResult[Any]:
    if not spec.resumes:
        return await _call(entry, spec)
    version = await _claim_decision(entry, spec, index)
    if version is None:
        return await _call(entry, spec)
    assert spec.thread_id is not None
    try:
        return await _call(entry, spec)
    except BaseException:
        # A failed resume may be retried; a successful one keeps its claim.
        await index.release_decision(entry.name, spec.thread_id, version)
        raise


async def _claim_decision(
    entry: RegisteredSystem, spec: JobSpec, index: DecisionClaims
) -> str | None:
    """Claim the paused state a decision job answers; `None` when it cannot be read.

    Raises:
        SystemBusyError: If another resume answers (or answered) the same paused state.
    """

    checkpointer = system_checkpointer(entry.served.system)
    if checkpointer is None or spec.thread_id is None:
        return None
    state = await pending_state(checkpointer, spec.thread_id)
    if state is None:
        return None  # no such thread: the system's own resume says so
    await claim_paused_state(index, entry.name, spec.thread_id, state.version)
    return state.version


async def _call(entry: RegisteredSystem, spec: JobSpec) -> RunResult[Any]:
    served = entry.served
    # A resumed run gets the deps of the thread's owner, not the reviewer's.
    subject = spec.owner or spec.actor
    principal = Principal(subject, tenant=spec.tenant) if subject is not None else None
    if entry.bare:
        deps: Any = dict(spec.deps) if spec.deps else None
    else:
        if served.principal_deps is not None and principal is None:
            raise InvalidRequestError(
                f"System {spec.system!r} derives deps from the caller (principal_deps): "
                "set actor (and tenant) on the job"
            )
        deps = await served.resolve_deps(spec.deps, principal)
    method: Callable[..., Awaitable[RunResult[Any]]]
    fixed: dict[str, Any] = {}
    if spec.resumes:
        resume = getattr(served.system, "resume", None)
        if not callable(resume):
            raise InvalidRequestError(f"System {spec.system!r} does not support resuming runs")
        method = resume
        args: tuple[Any, ...] = (spec.thread_id, spec.decision)
    else:
        served.check_messages(spec.input, spec.history)
        method = served.system.run
        args = (spec.input,)
        fixed["thread_id"] = spec.thread_id
        if spec.history:
            fixed["history"] = spec.history
    timeout = min((t for t in (spec.timeout, served.run_timeout) if t is not None), default=None)
    options = call_options(
        method,
        deps=deps,
        usage_limits=served.limits_for(spec.usage_limits),
        metadata={
            **spec.metadata,
            JOB_ID_METADATA_KEY: spec.job_id,
            TENANT_METADATA_KEY: spec.tenant,
            ACTOR_METADATA_KEY: spec.actor,
        },
        timeout=timeout,
        **fixed,
    )
    if timeout is not None and "timeout" not in options:
        # Systems that take no `timeout=` are stopped by cancelling the task instead.
        try:
            async with asyncio.timeout(timeout) as scope:
                return await method(*args, **options)
        except TimeoutError:
            if not scope.expired():
                raise  # a timeout inside the system, not the job's limit
            raise RunTimeoutError(
                f"The job's run took longer than {timeout:g}s",
                run_id=spec.job_id,
                elapsed=timeout,
                limit=timeout,
            ) from None
    return await method(*args, **options)


async def _deliver(callback: WebhookCallback, result: JobResult) -> JobResult:
    try:
        await callback.deliver(result)
    except Exception as exc:  # noqa: BLE001 - a delivery problem never discards a finished job
        _log.warning("Callback of job %s was not delivered: %s", result.job_id, exc)
        return result.model_copy(update={"callback_delivered": False, "callback_error": str(exc)})
    return result.model_copy(update={"callback_delivered": True, "callback_error": None})


async def _replay(claim: IdempotencyClaim, spec: JobSpec, stored: JobResult) -> JobResult:
    """The stored result, its callback sent again if it was not delivered."""

    result = stored.model_copy(update={"replayed": True})
    if spec.callback is not None and stored.callback_delivered is False:
        result = await _deliver(spec.callback, result)
        if result.callback_delivered:
            await claim.finish(
                200, result.model_copy(update={"replayed": False}).model_dump(mode="json")
            )
    return result


__all__ = [
    "DEFAULT_IDEMPOTENCY_TTL",
    "DEFAULT_TOLERANCE",
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "JOB_ID_METADATA_KEY",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "CompletionHook",
    "JobInput",
    "JobRegistry",
    "JobResult",
    "JobSpec",
    "JobStatus",
    "RegisteredSystem",
    "SystemSource",
    "WebhookCallback",
    "execute",
    "execute_sync",
    "new_job_id",
    "sign_webhook",
    "verify_webhook",
]
