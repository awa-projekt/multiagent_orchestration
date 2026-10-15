"""Runtime layer: chat model resolution, settings and resilience."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa.models._http import loop_local_openai_client
    from awa.models.errors import classify_model_error, model_identity
    from awa.models.guard import (
        allow_model_requests,
        ensure_model_request_allowed,
        is_offline_model,
        model_requests_allowed,
        set_allow_model_requests,
    )
    from awa.models.registry import (
        DEFAULT_REQUEST_TIMEOUT,
        ModelFactory,
        ModelRegistry,
        resolve_model,
    )
    from awa.models.resilience import (
        CircuitBreakerRegistry,
        CircuitState,
        ModelResilienceMiddleware,
        build_resilience_middleware,
    )
    from awa.models.settings import (
        TRANSIENT_ERROR_KINDS,
        CircuitBreaker,
        ModelErrorKind,
        ModelRetryPolicy,
        ModelSettings,
    )

_HOMES = homes(
    {
        "awa.models._http": "loop_local_openai_client",
        "awa.models.errors": "classify_model_error model_identity",
        "awa.models.guard": (
            "allow_model_requests ensure_model_request_allowed is_offline_model"
            " model_requests_allowed set_allow_model_requests"
        ),
        "awa.models.registry": "DEFAULT_REQUEST_TIMEOUT ModelFactory ModelRegistry resolve_model",
        "awa.models.resilience": (
            "CircuitBreakerRegistry CircuitState ModelResilienceMiddleware"
            " build_resilience_middleware"
        ),
        "awa.models.settings": (
            "TRANSIENT_ERROR_KINDS CircuitBreaker ModelErrorKind ModelRetryPolicy ModelSettings"
        ),
    }
)

__all__ = [
    "DEFAULT_REQUEST_TIMEOUT",
    "TRANSIENT_ERROR_KINDS",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitState",
    "ModelErrorKind",
    "ModelFactory",
    "ModelRegistry",
    "ModelResilienceMiddleware",
    "ModelRetryPolicy",
    "ModelSettings",
    "allow_model_requests",
    "build_resilience_middleware",
    "classify_model_error",
    "ensure_model_request_allowed",
    "is_offline_model",
    "loop_local_openai_client",
    "model_identity",
    "model_requests_allowed",
    "resolve_model",
    "set_allow_model_requests",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
