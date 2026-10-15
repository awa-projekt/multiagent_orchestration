"""Settings for chat models: construction arguments and resilience policies."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from awa.models.capabilities import Effort

ModelErrorKind = Literal[
    "rate_limit",
    "server_error",
    "timeout",
    "connection",
    "authentication",
    "permission_denied",
    "invalid_request",
    "not_found",
    "context_overflow",
    "circuit_open",
    "error",
]
"""The category of a failed model request; see `ModelProviderError.kind`."""

TRANSIENT_ERROR_KINDS: tuple[ModelErrorKind, ...] = (
    "rate_limit",
    "server_error",
    "timeout",
    "connection",
)
"""Error kinds that may succeed when the same request is repeated later."""


class ModelRetryPolicy(BaseModel):
    """How often, and how patiently, a failed model request is repeated.

    By default only transient failures are retried: rate limits, 5xx, timeouts and connection
    failures. Every attempt counts towards `UsageLimits.max_requests`, not `max_steps`. The delay
    before retry `n` is `initial_delay * backoff_factor ** (n - 1)`, capped at `max_delay`,
    randomized by ±25% with `jitter`, and at least a `Retry-After` hint; a hint above `max_delay`
    gives the model up (the next fallback is tried).

    Attributes:
        max_attempts: Attempts per model, including the first one.
        backoff_factor: Growth factor of the delay per retry (`1.0` keeps it constant).
        retry_on: The error kinds to retry.

    Example:
        ```python
        settings = ModelSettings(retry=ModelRetryPolicy(max_attempts=4, initial_delay=0.5))
        agent = Agent("analyst", model="openai:gpt-6.1-sol", model_settings=settings)
        ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=3, ge=1)
    initial_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=30.0, ge=0)
    backoff_factor: float = Field(default=2.0, ge=1)
    jitter: bool = True
    retry_on: tuple[ModelErrorKind, ...] = TRANSIENT_ERROR_KINDS
    respect_retry_after: bool = True


class CircuitBreaker(BaseModel):
    """Stop calling a model that keeps failing, and probe it again later.

    After `failure_threshold` consecutive transient failures the model's circuit opens: requests go
    straight to the next fallback for `reset_after` seconds, then one trial request decides. The
    state is per model and process-wide.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_threshold: int = Field(default=5, ge=1)
    reset_after: float = Field(default=30.0, gt=0)


_CONSTRUCTION_FIELDS = ("temperature", "max_tokens", "timeout", "max_retries")


class ModelSettings(BaseModel):
    """How chat models are built and how their requests are made resilient.

    Construction fields apply when a model is built from a string; unset fields are not passed. The
    resilience fields (`retry`, `fallbacks`, `circuit_breaker`) apply to every model request of an
    agent.

    Attributes:
        temperature: Sampling temperature; not sent to models that reject sampling.
        effort: Reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`), sent as
            `reasoning_effort` where the model takes one (OpenAI reasoning models on the Responses
            API, where `xhigh` and `max` become `high`); else it pins `temperature=0` unless one is
            set. See `awa.models.capabilities`.
        timeout: Request timeout in seconds; unset, OpenAI and Azure models get
            `DEFAULT_REQUEST_TIMEOUT` (600 seconds), other providers their own default.
        max_retries: Retries of the provider client itself; with a `retry` policy and no explicit
            value, OpenAI, Azure and Anthropic clients get 0, so every attempt is visible to awa.
        extra: Additional provider-specific keyword arguments; they win over the named fields.
        retry: Retry policy for failed model requests; `None` disables retries by awa.
        fallbacks: Models to try in order after the model failed, resolved lazily through the
            agent's `ModelRegistry`; `None` means unset, `()` no fallbacks.

    Example:
        ```python
        agent = Agent("summarizer", model="openai:gpt-6.1-sol", model_settings=ModelSettings(
            temperature=0,
            retry=ModelRetryPolicy(max_attempts=3),
            fallbacks=("anthropic:claude-haiku-5-5",),
        ))
        ```
    """

    model_config = ConfigDict(frozen=True)

    temperature: float | None = None
    effort: Effort | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    timeout: float | None = Field(default=None, gt=0)
    max_retries: int | None = Field(default=None, ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)
    retry: ModelRetryPolicy | None = None
    fallbacks: tuple[str, ...] | None = None
    circuit_breaker: CircuitBreaker | None = None

    @model_validator(mode="after")
    def _check_fallbacks(self) -> ModelSettings:
        for reference in self.fallbacks or ():
            if not reference or reference != reference.strip():
                raise ValueError(f"Invalid fallback model reference {reference!r}")
        return self

    @property
    def has_construction_settings(self) -> bool:
        """Whether any construction field (or `extra`) is set."""

        return bool(self.extra) or any(
            getattr(self, name) is not None for name in _CONSTRUCTION_FIELDS
        )

    @property
    def has_resilience(self) -> bool:
        """Whether a retry policy, fallbacks or a circuit breaker is configured."""

        return self.retry is not None or bool(self.fallbacks) or self.circuit_breaker is not None

    def to_kwargs(self) -> dict[str, Any]:
        """The model constructor's keyword arguments (resilience fields never included)."""

        kwargs: dict[str, Any] = {
            name: value
            for name in _CONSTRUCTION_FIELDS
            if (value := getattr(self, name)) is not None
        }
        # Copy nested containers: provider constructors may mutate dicts such as
        # `model_kwargs`, which would silently change these frozen settings.
        # Other values (HTTP clients, credentials objects) are passed as they are.
        kwargs.update({key: _copy_containers(value) for key, value in self.extra.items()})
        return kwargs

    def merge(self, other: ModelSettings | None) -> ModelSettings:
        """New settings where every non-`None` field of `other` wins; `extra` is merged key by key."""

        if other is None:
            return self
        updates: dict[str, Any] = {
            name: value
            for name in type(other).model_fields
            if name != "extra" and (value := getattr(other, name)) is not None
        }
        updates["extra"] = {**self.extra, **other.extra}
        return self.model_copy(update=updates)


def _copy_containers(value: Any) -> Any:
    """Copy dicts and lists recursively (sets shallowly); return any other value itself."""

    if isinstance(value, dict):
        return {key: _copy_containers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_containers(item) for item in value]
    if isinstance(value, set):
        return set(value)
    return value


__all__ = [
    "TRANSIENT_ERROR_KINDS",
    "CircuitBreaker",
    "ModelErrorKind",
    "ModelRetryPolicy",
    "ModelSettings",
]
