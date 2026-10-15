"""Extension: send awa runs to Langfuse over OpenTelemetry with one call.

`connect_langfuse()` builds an OTLP/HTTP exporter for Langfuse with a private tracer provider
(no Langfuse SDK needed; the extra `langfuse`). A failed batch or score is retried with backoff;
`shutdown()` waits for every buffered span. Captured text and metadata are redacted first.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from awa._internal.install import install_hint
from awa._internal.logging import get_logger
from awa._internal.retry import backoff_delay
from awa.contracts.usage import PriceTable
from awa.core.config import env_value
from awa.errors import ConfigurationError, OptionalDependencyError
from awa.kernel.redaction import Redactor
from awa.observability.otel import PII_REDACTOR, OpenTelemetrySubscriber, resolve_redactor

DEFAULT_LANGFUSE_URL = "https://cloud.langfuse.com"
"""Langfuse Cloud, EU region: used when neither `LANGFUSE_BASE_URL` nor `LANGFUSE_HOST` is set."""

TRACES_PATH = "/api/public/otel/v1/traces"
"""Where Langfuse receives OTLP/HTTP traces (self-hosted needs Langfuse 3.22 or newer)."""

SCORES_PATH = "/api/public/scores"
"""Where Langfuse receives scores (public API)."""

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_MAX_METADATA_CHARS = 256
_MAX_COMMENT_CHARS = 1000
_logger = get_logger("langfuse")
_export_logger = get_logger("tracing")
_EXPORT_ATTEMPTS = 3
"""Tries per span batch or score, the first included."""
_RETRY_INITIAL_DELAY = 1.0
"""Seconds before the second try; each further try waits twice as long, at most 8 seconds."""
_RETRY_MAX_DELAY = 8.0


def _retry_wait(attempt: int) -> float:
    """Seconds to wait after the failed try number `attempt` (0 for the first)."""

    return backoff_delay(attempt, initial=_RETRY_INITIAL_DELAY, cap=_RETRY_MAX_DELAY, jitter=0.1)


def _with_retries(
    send: Callable[[], bool], *, stop: threading.Event | None = None
) -> tuple[bool, int]:
    """Call `send` until it returns `True`, at most `_EXPORT_ATTEMPTS` times; return success and
    tries. A set `stop` ends the waiting."""

    stop = stop or threading.Event()
    for attempt in range(_EXPORT_ATTEMPTS):
        if send():
            return True, attempt + 1
        if attempt + 1 < _EXPORT_ATTEMPTS and stop.wait(_retry_wait(attempt)):
            return False, attempt + 1
    return False, _EXPORT_ATTEMPTS


class _RetryingSpanExporter:
    """A span exporter that sends a failed batch again with backoff and counts what it drops.

    It runs in the batch processor's thread, so waits never hold up a run; a batch that still fails
    is dropped with one warning counting the lost spans.

    Args:
        inner: The OTLP exporter that sends one batch.
        success: The result `inner` returns for a sent batch (`SpanExportResult.SUCCESS`).
        failure: The result to return for a dropped batch (`SpanExportResult.FAILURE`).
        timeout: Seconds per request of `inner`, for the warning.
    """

    def __init__(self, inner: Any, *, success: Any, failure: Any, timeout: float) -> None:
        self._inner = inner
        self._success = success
        self._failure = failure
        self._timeout = timeout
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.dropped = 0

    def export(self, spans: Sequence[Any]) -> Any:
        """Send `spans`; on a failure try again, then drop them with one warning."""

        ok, tries = _with_retries(
            lambda: self._inner.export(spans) == self._success, stop=self._stop
        )
        if ok:
            return self._success
        with self._lock:
            self.dropped += len(spans)
            total = self.dropped
        _export_logger.warning(
            "Langfuse did not take %d span(s) after %d tries (%.0f s each, "
            "AWA_TRACE_EXPORT_TIMEOUT); they are dropped (%d dropped so far).",
            len(spans),
            tries,
            self._timeout,
            total,
        )
        return self._failure

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush the inner exporter."""

        return bool(self._inner.force_flush(timeout_millis))

    def shutdown(self) -> None:
        """Stop waiting for retries and shut the inner exporter down."""

        self._stop.set()
        self._inner.shutdown()


class _EvaluatedCases(Protocol):
    """What `score_eval` reads of an evaluation report (an `awa.evals.EvalReport`)."""

    @property
    def cases(self) -> Sequence[Any]: ...


@dataclass
class LangfuseConnection:
    """The result of `connect_langfuse`: the hook to give your agents and how to flush it.

    Attributes:
        subscriber: Pass it as a hook (`hooks=[connection.subscriber]`).
        endpoint: The full OTLP URL the spans are sent to (never contains a key).
    """

    subscriber: OpenTelemetrySubscriber
    endpoint: str
    _provider: Any
    _base_url: str = field(default="", repr=False)
    _authorization: str = field(default="", repr=False)
    _timeout: float = field(default=30.0, repr=False)
    _exporter: _RetryingSpanExporter | None = field(default=None, repr=False)
    _redactor: Redactor | None = field(default=None, repr=False)
    _capture_content: bool = field(default=False, repr=False)

    @property
    def dropped_spans(self) -> int:
        """Spans that could not be sent after every try (each loss was logged once)."""

        return self._exporter.dropped if self._exporter is not None else 0

    def flush(self, timeout_millis: int | None = None) -> bool:
        """Send everything that is still buffered, retries included; `False` when the timeout
        ran out (default: long enough for every try of a batch)."""

        if timeout_millis is None:  # every try of a batch at the timeout, the waits, and a margin
            waits = sum(_retry_wait(attempt) for attempt in range(_EXPORT_ATTEMPTS - 1))
            timeout_millis = int((_EXPORT_ATTEMPTS * self._timeout + waits) * 1000 * 1.2)
        return bool(self._provider.force_flush(timeout_millis))

    def shutdown(self) -> None:
        """End open spans, send the buffered ones (waiting for their retries), and stop
        exporting. Call it before the process ends."""

        self.subscriber.close()
        self.flush()
        self._provider.shutdown()

    def score(
        self,
        run_id: str,
        name: str,
        value: float | bool,
        *,
        comment: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Attach a score to the Langfuse trace of run `run_id`; `False` if not traced here or not sent.

        A `bool` is a boolean score, a number numeric. Transient failures are retried with backoff;
        failures are logged, never raised.
        """

        trace_id = self.subscriber.trace_id(run_id)
        if trace_id is None or not self._base_url:
            return False
        body: dict[str, Any] = {
            "traceId": trace_id,
            "name": name,
            "value": (1 if value else 0) if isinstance(value, bool) else float(value),
            "dataType": "BOOLEAN" if isinstance(value, bool) else "NUMERIC",
        }
        if comment:
            body["comment"] = comment[:_MAX_COMMENT_CHARS]
        from awa._internal.http import HttpPolicy, fetch

        host = urlsplit(self._base_url).hostname or ""
        # The address was checked by `connect_langfuse`; Langfuse may run on this computer.
        policy = HttpPolicy(
            allowed_hosts=(host,),
            allow_private_networks=True,
            allow_http=True,
            timeout=timeout if timeout is not None else self._timeout,
        )
        data = json.dumps(body).encode("utf-8")
        failure: dict[str, Any] = {}

        def send() -> bool:
            try:
                response = fetch(
                    policy,
                    self._base_url + SCORES_PATH,
                    method="POST",
                    headers={
                        "Authorization": self._authorization,
                        "Content-Type": "application/json",
                    },
                    secret_headers=frozenset({"Authorization"}),
                    data=data,
                    follow_redirects=False,
                )
            except Exception as exc:  # noqa: BLE001 - a lost score must not fail the command
                failure["reason"] = str(exc)
                return False
            if 200 <= response.status < 300:
                return True
            failure["reason"] = f"HTTP {response.status}"
            if response.status != 429 and response.status < 500:
                raise _ScoreRefusedError  # a refusal does not change on a second try
            return False

        try:
            sent, tries = _with_retries(send)
        except _ScoreRefusedError:
            _logger.warning("Langfuse refused the score %r (%s)", name, failure["reason"])
            return False
        if not sent:
            _logger.warning(
                "Could not send the score %r to Langfuse after %d tries: %s",
                name,
                tries,
                failure.get("reason"),
            )
        return sent

    def score_eval(self, report: _EvaluatedCases) -> int:
        """Send an evaluation's results as scores (`eval.passed`, `eval.<scorer>`) and return how many.

        A score's comment is the case label; with `capture_content` it is the scorer's reason or
        error, redacted.
        """

        from awa.contracts.events import AgentStartedEvent

        sent = 0
        for case in report.cases:
            run_id = next(
                (
                    event.run_id
                    for event in case.events
                    if isinstance(event, AgentStartedEvent) and event.parent_run_id is None
                ),
                None,
            )
            if run_id is None or self.subscriber.trace_id(run_id) is None:
                continue
            label = f"case {case.case_id}"
            sent += self.score(run_id, "eval.passed", case.status == "passed", comment=label)
            for result in case.scores:
                sent += self.score(
                    run_id,
                    f"eval.{result.name}",
                    result.value,
                    comment=self._comment(result.error or result.reason, label),
                )
        return sent

    def _comment(self, text: str | None, label: str) -> str:
        if not text or not self._capture_content:
            return label
        return self._redactor.redact(text) if self._redactor is not None else text


class _ScoreRefusedError(Exception):
    """Langfuse answered a score with a client error: trying again cannot help."""


def langfuse_endpoint(base_url: str) -> str:
    """The OTLP traces URL of a Langfuse server (`http://localhost:3000` gives `.../api/public/otel/v1/traces`)."""

    return base_url.strip().rstrip("/") + TRACES_PATH


def connect_langfuse(
    *,
    public_key: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: Sequence[str] = (),
    trace_name: str | None = None,
    environment: str | None = None,
    release: str | None = None,
    version: str | None = None,
    capture_content: bool = False,
    prices: PriceTable | None = None,
    redactor: Redactor | None = PII_REDACTOR,
    metadata: Mapping[str, str | bool | int | float] | None = None,
    attributes: Mapping[str, str | bool | int | float] | None = None,
    allow_http: bool = False,
    export_timeout_seconds: float | None = None,
) -> LangfuseConnection:
    """Connect awa to Langfuse: spans of your runs appear as traces with the agent graph.

    Args:
        public_key: Defaults to `LANGFUSE_PUBLIC_KEY`.
        secret_key: Defaults to `LANGFUSE_SECRET_KEY`; never logged.
        base_url: Defaults to `LANGFUSE_BASE_URL`, then `LANGFUSE_HOST`, then Langfuse Cloud (EU).
            Must be `https`, except on this computer.
        session_id: Groups traces into one Langfuse session (a chat thread, a ticket).
        user_id: User id of every trace.
        tags: Tags of every trace.
        trace_name: Name of the trace; defaults to the name of the top-level run.
        environment: Defaults to `LANGFUSE_TRACING_ENVIRONMENT`.
        release: Release of your application, for example a git tag; defaults to
            `LANGFUSE_RELEASE`.
        version: Version of the system that ran (for example the blueprint's).
        capture_content: Send prompts, replies and tool bodies, redacted (off: names, ids, tokens
            and timing only).
        prices: Model prices (`PriceTable`); without them Langfuse prices models it knows by name.
        redactor: The default masks credentials and personal data; `None` sends text unchanged.
        metadata: Trace metadata for every run, filterable in Langfuse; each run's `awa.*` metadata
            is added.
        attributes: Extra attributes of every span.
        allow_http: Accept a plain `http` server on another computer (traffic is unencrypted).
        export_timeout_seconds: Seconds per export or score request; defaults to
            `AWA_TRACE_EXPORT_TIMEOUT` (30).

    Raises:
        ConfigurationError: A key is missing, the URL is not http(s), or it is plain `http` on
            another computer without `allow_http=True`.
        OptionalDependencyError: The extra `langfuse` is not installed.

    Example:
        ```python
        connection = connect_langfuse(session_id="chat-42")  # reads the LANGFUSE_* variables
        agent = Agent("assistant", hooks=[connection.subscriber])
        await agent.run("Research hybrid search.")
        connection.shutdown()  # flushes the spans before the process ends
        ```
    """

    from awa.plugins.switches import is_off

    if is_off("tracer.langfuse"):
        raise ConfigurationError(
            "Langfuse tracing is switched off for this project (tracer.langfuse); remove it "
            "from the blueprint's `components:` to switch it on"
        )
    redactor = resolve_redactor(redactor)
    public = public_key or (env_value("LANGFUSE_PUBLIC_KEY") or "")
    secret = secret_key or (env_value("LANGFUSE_SECRET_KEY") or "")
    if not public or not secret:
        raise ConfigurationError(
            "Langfuse needs a public and a secret key: set LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY (Langfuse project settings, API keys) or pass them as arguments."
        )
    base = _checked_base_url(base_url, allow_http=allow_http)
    endpoint = langfuse_endpoint(base)
    environment = environment or env_value("LANGFUSE_TRACING_ENVIRONMENT") or None
    release = release or env_value("LANGFUSE_RELEASE") or None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
    except ImportError as exc:
        raise OptionalDependencyError(
            "Sending spans to Langfuse needs the OpenTelemetry SDK and the OTLP/HTTP exporter: "
            f"{install_hint('langfuse')}"
        ) from exc

    timeout = export_timeout_seconds
    if timeout is None:
        from awa.core.settings import get_settings

        try:
            timeout = float(get_settings().trace_export_timeout)
        except Exception:  # noqa: BLE001 - an unreadable configuration must not stop tracing
            timeout = 30.0
    if timeout <= 0:
        raise ConfigurationError(f"The trace export timeout must be positive, got {timeout!r}")
    token = base64.b64encode(f"{public}:{secret}".encode()).decode("ascii")
    exporter = _RetryingSpanExporter(
        OTLPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": f"Basic {token}", "x-langfuse-ingestion-version": "4"},
            timeout=timeout,
        ),
        success=SpanExportResult.SUCCESS,
        failure=SpanExportResult.FAILURE,
        timeout=timeout,
    )
    resource_attributes: dict[str, str] = {"service.name": "awa"}
    if environment:
        resource_attributes["langfuse.environment"] = environment
    provider = TracerProvider(resource=Resource(resource_attributes))
    # Duck-typed: `_RetryingSpanExporter` has the exporter methods, not the base class.
    span_exporter: Any = exporter
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    extra = _span_attributes(
        attributes,
        metadata,
        redactor=redactor,
        environment=environment,
        release=release,
        version=version,
    )
    subscriber = OpenTelemetrySubscriber(
        provider.get_tracer("awa", _awa_version()),
        capture_content=capture_content,
        attributes=extra,
        prices=prices,
        redactor=redactor,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        trace_name=trace_name,
        langfuse=True,
    )
    return LangfuseConnection(
        subscriber=subscriber,
        endpoint=endpoint,
        _provider=provider,
        _base_url=base.strip().rstrip("/"),
        _authorization=f"Basic {token}",
        _timeout=timeout,
        _exporter=exporter,
        _redactor=redactor,
        _capture_content=capture_content,
    )


def _checked_base_url(base_url: str | None, *, allow_http: bool) -> str:
    """The Langfuse server address, refused when it is not https outside this computer."""

    base = (
        base_url
        or env_value("LANGFUSE_BASE_URL")
        or env_value("LANGFUSE_HOST")
        or DEFAULT_LANGFUSE_URL
    )
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigurationError(
            f"The Langfuse address {base!r} is not an http(s) URL; use for example "
            "LANGFUSE_BASE_URL=http://localhost:3000."
        )
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        if not allow_http:
            raise ConfigurationError(
                f"The Langfuse address {base!r} is plain http on another computer: the keys and "
                "spans would travel unencrypted. Use https, or a server on this computer "
                "(http://localhost:3000), or pass allow_http=True inside a private network."
            )
        _logger.warning(
            "Langfuse at %s is reached without TLS: the keys and spans travel unencrypted.",
            parts.hostname,
        )
    return base


def _span_attributes(
    attributes: Mapping[str, str | bool | int | float] | None,
    metadata: Mapping[str, str | bool | int | float] | None,
    *,
    redactor: Redactor | None,
    environment: str | None,
    release: str | None,
    version: str | None,
) -> dict[str, str | bool | int | float]:
    """The attributes every span of a connection carries, with redacted trace metadata."""

    extra: dict[str, str | bool | int | float] = dict(attributes or {})
    if environment:
        extra["langfuse.environment"] = environment
    if release:
        extra["langfuse.release"] = release
    if version:
        extra["langfuse.version"] = version
    extra["langfuse.trace.metadata.awa_version"] = _awa_version()
    for key, value in (metadata or {}).items():
        if isinstance(value, str):
            value = redactor.redact(value) if redactor is not None else value
            value = value[:_MAX_METADATA_CHARS]
        extra[f"langfuse.trace.metadata.{key}"] = value
    return extra


def _awa_version() -> str:
    from awa._internal.version import __version__

    return __version__


__all__ = [
    "DEFAULT_LANGFUSE_URL",
    "SCORES_PATH",
    "TRACES_PATH",
    "LangfuseConnection",
    "connect_langfuse",
    "langfuse_endpoint",
]
