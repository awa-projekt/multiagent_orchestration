"""Retries, fallbacks and circuit breakers for model requests.

Every attempt passes run tracking (request guard, `max_requests`, usage of the answering model)
and none counts towards `max_steps`. Usage limits, `max_steps`, cancellation and blocked requests
are never retried or handed to a fallback.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from awa._internal.retry import backoff_delay
from awa._internal.upstream.agents import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    sanitize_fallback_request,
)
from awa._internal.upstream.messages import AIMessage
from awa._internal.upstream.models import BaseChatModel
from awa.contracts.events import ModelFallbackEvent, ModelRetryEvent
from awa.errors import AwaError, ModelProviderError
from awa.models.errors import classify_model_error, model_identity, model_provider
from awa.models.registry import ModelRegistry, resolve_model
from awa.models.settings import (
    TRANSIENT_ERROR_KINDS,
    CircuitBreaker,
    ModelRetryPolicy,
    ModelSettings,
)
from awa.runtime.context import current_run_context
from awa.runtime.engine import current_engine

CircuitState = Literal["closed", "open", "half_open"]
"""State of a model's circuit: `closed` (requests flow), `open` (requests
skip the model) or `half_open` (the next request is a trial)."""


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None
    probing: bool = False


class CircuitBreakerRegistry:
    """Circuit state per `provider:model`, shared by every agent and run in the process; thread-safe.

    Args:
        clock: Monotonic clock in seconds.
    """

    _default: ClassVar[CircuitBreakerRegistry | None] = None

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> CircuitBreakerRegistry:
        """Return the process-wide registry agents use."""

        if cls._default is None:
            cls._default = cls()
        return cls._default

    def state(self, model: str, breaker: CircuitBreaker) -> CircuitState:
        """The current state of `model`'s circuit under `breaker`'s settings."""

        with self._lock:
            circuit = self._circuits.get(model)
            if circuit is None or circuit.opened_at is None:
                return "closed"
            if self._clock() - circuit.opened_at < breaker.reset_after:
                return "open"
            return "half_open"

    def acquire(self, model: str, breaker: CircuitBreaker) -> Literal["closed", "trial"] | None:
        """Admit a request to `model` (`"closed"`, or `"trial"` once when half-open), or refuse with `None`."""

        with self._lock:
            circuit = self._circuits.setdefault(model, _Circuit())
            if circuit.opened_at is None:
                return "closed"
            if self._clock() - circuit.opened_at < breaker.reset_after or circuit.probing:
                return None
            circuit.probing = True
            return "trial"

    def record_success(self, model: str) -> None:
        """Record that `model` answered: the circuit closes."""

        with self._lock:
            self._circuits[model] = _Circuit()

    def record_failure(self, model: str, breaker: CircuitBreaker) -> None:
        """Record a transient failure of `model`; the circuit opens at the threshold or a failed trial."""

        with self._lock:
            circuit = self._circuits.setdefault(model, _Circuit())
            circuit.failures += 1
            if circuit.probing or circuit.failures >= breaker.failure_threshold:
                circuit.opened_at = self._clock()
            circuit.probing = False

    def release(self, model: str) -> None:
        """End a trial request without an outcome (it was cancelled, or the
        error says nothing about the provider's health)."""

        with self._lock:
            circuit = self._circuits.get(model)
            if circuit is not None:
                circuit.probing = False

    def retry_in(self, model: str, breaker: CircuitBreaker) -> float | None:
        """Seconds until an open circuit admits a trial request, or `None` if it is not open."""

        with self._lock:
            circuit = self._circuits.get(model)
            if circuit is None or circuit.opened_at is None:
                return None
            remaining = breaker.reset_after - (self._clock() - circuit.opened_at)
            return remaining if remaining > 0 else None

    def reset(self, model: str | None = None) -> None:
        """Close the circuit of `model`, or of every model."""

        with self._lock:
            if model is None:
                self._circuits.clear()
            else:
                self._circuits.pop(model, None)


@dataclass(frozen=True)
class _Failure:
    """Why one model of the chain was given up on."""

    reason: str
    exc: BaseException | None
    error: ModelProviderError | None

    def to_raise(self) -> BaseException:
        if self.error is None:
            assert self.exc is not None
            return self.exc
        if self.exc is not None and self.exc is not self.error:
            self.error.__cause__ = self.exc
        return self.error


Outcome = Literal["success", "failure", "neutral"]


class ModelResilienceMiddleware(AgentMiddleware):
    """Retries transient model failures, falls back to other models, and skips open circuits.

    Each model in the chain (the agent's, then `fallbacks`) is tried up to `retry.max_attempts`
    times on retryable errors. When every model failed, the last failure is raised as a
    `ModelProviderError` (or `ModelRateLimitError`); a non-provider exception is re-raised
    unchanged. `ModelRetryEvent` and `ModelFallbackEvent` precede each retry and fallback.

    `Agent` builds it from `ModelSettings`; add it yourself to inject a clock, sleep or random
    source, or to use model instances as fallbacks. It must run inside run tracking.

    Args:
        retry: Retry policy per model; `None` makes a single attempt per model.
        fallbacks: Profile names, `provider:model` strings (resolved lazily) or model instances.
        circuit_breaker: Circuit breaker settings; `None` disables it.
        model_registry: Registry that resolves string fallbacks; defaults to
            `ModelRegistry.default()`.
        model_settings: Construction settings for string fallbacks; resilience fields are ignored.
        breakers: Circuit state; defaults to `CircuitBreakerRegistry.default()`.
        sleep: Awaited with the delay before a retry.
        random: Source of uniform numbers in `[0, 1)` for the jitter.

    Example:
        ```python
        resilience = ModelResilienceMiddleware(
            retry=ModelRetryPolicy(max_attempts=2, jitter=False),
            fallbacks=[FakeChatModel(["backup answer"])],
        )
        agent = Agent("a", model=flaky_model, middleware=[resilience])
        ```
    """

    def __init__(
        self,
        *,
        retry: ModelRetryPolicy | None = None,
        fallbacks: Sequence[str | BaseChatModel] = (),
        circuit_breaker: CircuitBreaker | None = None,
        model_registry: ModelRegistry | None = None,
        model_settings: ModelSettings | None = None,
        breakers: CircuitBreakerRegistry | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        random: Callable[[], float] = random.random,
    ) -> None:
        super().__init__()
        for reference in fallbacks:
            if not isinstance(reference, str | BaseChatModel):
                raise TypeError(
                    "Fallbacks must be model references or chat models, got "
                    f"{type(reference).__name__}"
                )
        self.retry = retry
        self.fallbacks: tuple[str | BaseChatModel, ...] = tuple(fallbacks)
        self.circuit_breaker = circuit_breaker
        self._registry = model_registry
        self._settings = (
            model_settings.model_copy(update={"fallbacks": None, "circuit_breaker": None})
            if model_settings is not None
            else None
        )
        self._breakers = breakers
        self._sleep = sleep
        self._random = random
        self._resolved: dict[int, BaseChatModel] = {}

    @property
    def breakers(self) -> CircuitBreakerRegistry:
        """The circuit state this middleware reads and updates."""

        return self._breakers if self._breakers is not None else CircuitBreakerRegistry.default()

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        """Call the model with retries, then each fallback model in turn."""

        label = model_identity(request.model)
        outcome = await self._attempt(request, handler, request.model, label)
        if not isinstance(outcome, _Failure):
            return outcome
        for index, reference in enumerate(self.fallbacks):
            model = self._fallback_model(index, reference)
            target = reference if isinstance(reference, str) else model_identity(model)
            await _emit_fallback(label, target, outcome)
            label = target
            fallback_request = sanitize_fallback_request(request, model)
            outcome = await self._attempt(fallback_request, handler, model, label)
            if not isinstance(outcome, _Failure):
                return outcome
        raise outcome.to_raise()

    async def _attempt(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
        model: BaseChatModel,
        label: str,
    ) -> ModelResponse[Any] | _Failure:
        """Call one model, retrying while the policy allows."""

        key = model_identity(model)
        breaker = self.circuit_breaker
        previous: _Failure | None = None
        attempt = 0
        while True:
            attempt += 1
            admitted = self.breakers.acquire(key, breaker) if breaker is not None else "closed"
            if admitted is None:
                assert breaker is not None
                return previous or self._circuit_open(key, label, breaker)
            trial = admitted == "trial"
            try:
                response = await handler(request)
            except current_engine().control_flow:
                self._settle(key, "neutral", trial)
                raise
            except Exception as exc:
                if isinstance(exc, AwaError) and not isinstance(exc, ModelProviderError):
                    # Limits, max_steps and blocked requests are decisions, not failures.
                    self._settle(key, "neutral", trial)
                    raise
                error = classify_model_error(exc, model=label, provider=model_provider(model))
                # A non-transient provider error means the provider answered: it is healthy.
                health: Outcome = "neutral"
                if error is not None:
                    health = "failure" if error.kind in TRANSIENT_ERROR_KINDS else "success"
                self._settle(key, health, trial)
                failure = _Failure(
                    reason=error.kind if error is not None else "error", exc=exc, error=error
                )
                delay = self._retry_delay(error, attempt)
                if delay is None or error is None:
                    return failure
            except BaseException:
                # Cancellation: no outcome, never retried.
                self._settle(key, "neutral", trial)
                raise
            else:
                self._settle(key, "success", trial)
                return response
            previous = failure
            await _emit_retry(label, attempt, delay, failure.exc, error)
            if delay > 0:
                await self._sleep(delay)

    def _retry_delay(self, error: ModelProviderError | None, attempt: int) -> float | None:
        """Seconds to wait before the next attempt, or `None` to give up on the model."""

        policy = self.retry
        if (
            policy is None
            or error is None
            or error.kind not in policy.retry_on
            or attempt >= policy.max_attempts
        ):
            return None
        delay = backoff_delay(
            attempt - 1,
            initial=policy.initial_delay,
            factor=policy.backoff_factor,
            cap=policy.max_delay,
        )
        if policy.jitter:
            # Jitter the capped delay, so retries held at `max_delay` still spread out.
            delay = backoff_delay(
                0, initial=delay, jitter=0.25, cap=policy.max_delay, rand=self._random
            )
        if policy.respect_retry_after and error.retry_after is not None:
            if error.retry_after > policy.max_delay:
                return None
            delay = max(delay, error.retry_after)
        return delay

    def _settle(self, key: str, outcome: Outcome, trial: bool) -> None:
        breaker = self.circuit_breaker
        if breaker is None:
            return
        if outcome == "success":
            self.breakers.record_success(key)
        elif outcome == "failure":
            self.breakers.record_failure(key, breaker)
        elif trial:
            self.breakers.release(key)

    def _circuit_open(self, key: str, label: str, breaker: CircuitBreaker) -> _Failure:
        retry_in = self.breakers.retry_in(key, breaker)
        error = ModelProviderError(
            f"Model {label!r} is not called: its circuit breaker is open after repeated failures",
            kind="circuit_open",
            model=label,
            retry_after=retry_in,
            retryable=True,
        )
        return _Failure(reason="circuit_open", exc=None, error=error)

    def _fallback_model(self, index: int, reference: str | BaseChatModel) -> BaseChatModel:
        if isinstance(reference, BaseChatModel):
            return reference
        model = self._resolved.get(index)
        if model is None:
            registry = self._registry if self._registry is not None else ModelRegistry.default()
            settings = self._settings
            if settings is not None and not registry.accepts_construction_settings(reference):
                settings = None
            model = resolve_model(reference, settings=settings, registry=registry)
            model = self._resolved.setdefault(index, model)
        return model


async def _emit_retry(
    label: str, attempt: int, delay: float, exc: BaseException | None, error: ModelProviderError
) -> None:
    ctx = current_run_context()
    if ctx is not None:
        await ctx.emit(
            ModelRetryEvent(
                model=label,
                attempt=attempt,
                delay=delay,
                error_type=type(exc).__name__,
                kind=error.kind,
                status=error.status,
                **ctx.event_fields(),
            )
        )


async def _emit_fallback(label: str, target: str, failure: _Failure) -> None:
    ctx = current_run_context()
    if ctx is not None:
        await ctx.emit(
            ModelFallbackEvent(
                from_model=label,
                to_model=target,
                reason=failure.reason,
                error_type=type(failure.exc).__name__ if failure.exc is not None else None,
                **ctx.event_fields(),
            )
        )


def build_resilience_middleware(
    model: str | BaseChatModel | None,
    settings: ModelSettings | None,
    registry: ModelRegistry | None = None,
) -> ModelResilienceMiddleware | None:
    """The resilience middleware for this model configuration, or `None` when none is configured.

    The profile's registered settings are merged with `settings` (the agent's own win).
    """

    registry = registry if registry is not None else ModelRegistry.default()
    profile = registry.settings_for(model)
    effective = profile.merge(settings) if profile is not None else settings
    if effective is None or not effective.has_resilience:
        return None
    return ModelResilienceMiddleware(
        retry=effective.retry,
        fallbacks=effective.fallbacks or (),
        circuit_breaker=effective.circuit_breaker,
        model_registry=registry,
        # With the merged retry, string fallbacks also leave retrying to awa.
        model_settings=(settings or ModelSettings()).model_copy(update={"retry": effective.retry}),
    )


__all__ = [
    "CircuitBreakerRegistry",
    "CircuitState",
    "ModelResilienceMiddleware",
    "build_resilience_middleware",
]
