"""Translate provider SDK exceptions into awa's typed model errors, without importing any SDK."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType

from awa._internal.retry import retry_after_from_headers
from awa._internal.upstream.models import BaseChatModel, model_exceptions
from awa._internal.util import preview
from awa.core.providers import PROVIDERS
from awa.core.providers import provider as provider_spec
from awa.errors import AwaError, ModelProviderError, ModelRateLimitError
from awa.kernel.redaction import redact_secrets
from awa.models.settings import TRANSIENT_ERROR_KINDS, ModelErrorKind

_LANGCHAIN_KINDS: tuple[tuple[type[BaseException], ModelErrorKind], ...] = (
    # Most specific first: context overflow is also an invalid request.
    (model_exceptions.ContextOverflowError, "context_overflow"),
    (model_exceptions.ModelRateLimitError, "rate_limit"),
    (model_exceptions.ModelTimeoutError, "timeout"),
    (model_exceptions.ModelConnectionError, "connection"),
    (model_exceptions.ModelAPIError, "server_error"),
    (model_exceptions.ModelAuthenticationError, "authentication"),
    (model_exceptions.ModelPermissionDeniedError, "permission_denied"),
    (model_exceptions.ModelNotFoundError, "not_found"),
    (model_exceptions.ModelInvalidRequestError, "invalid_request"),
)

_STATUS_KINDS: Mapping[int, ModelErrorKind] = {
    400: "invalid_request",
    401: "authentication",
    403: "permission_denied",
    404: "not_found",
    408: "timeout",
    409: "server_error",
    413: "invalid_request",
    422: "invalid_request",
    429: "rate_limit",
}

# Class names (anywhere in the MRO) of transport and SDK errors that carry no status.
_NAME_KINDS: tuple[tuple[str, ModelErrorKind], ...] = (
    ("RateLimit", "rate_limit"),
    ("ResourceExhausted", "rate_limit"),
    ("TooManyRequests", "rate_limit"),
    ("Timeout", "timeout"),
    ("DeadlineExceeded", "timeout"),
    ("APIConnectionError", "connection"),
    ("ConnectError", "connection"),
    ("NetworkError", "connection"),
    ("RemoteProtocolError", "connection"),
    ("ServiceUnavailable", "server_error"),
    ("InternalServerError", "server_error"),
    ("Overloaded", "server_error"),
)

_PROVIDER_MODULES: Mapping[str, str] = {
    **{
        module: spec.name
        for spec in PROVIDERS
        if spec.name != "azure_openai"
        for module in (spec.name, spec.module)
    },
    "google": "google_genai",
    "langchain_google_vertexai": "google",
    "cohere": "cohere",
    "langchain_cohere": "cohere",
    "botocore": "aws",
    "langchain_aws": "aws",
}
"""The provider of an exception or model class, by the top-level module that defines it."""

_PROVIDERS_BY_LLM_TYPE: Mapping[str, str] = {
    "openai-chat": "openai",
    "azure-openai-chat": "azure_openai",
    "anthropic-chat": "anthropic",
    "anthropic-bedrock-chat": "aws",
    "anthropic-chat-vertexai": "google",
}

_MODEL_NAME_ATTRIBUTES = ("model_name", "model", "model_id", "deployment_name", "azure_deployment")

_MAX_DETAIL_CHARS = 300

KEY_VARIABLES: Mapping[str, str] = MappingProxyType(
    {item.name: item.key_variable for item in PROVIDERS if item.key_variable}
)
"""The environment variable that holds the API key of a provider, by model prefix."""

# What provider SDKs (and langchain-anthropic / langchain-openai) say when no credentials
# were configured at all; they raise a plain `TypeError` or `OpenAIError`, not an HTTP error.
_MISSING_CREDENTIALS = (
    "could not resolve authentication method",
    "authentication failed: no api key",
    "api_key client option must be set",
)

_KIND_DESCRIPTIONS: Mapping[str, str] = {
    "rate_limit": "rate limit exceeded",
    "server_error": "server error",
    "timeout": "request timed out",
    "connection": "connection failed",
    "authentication": "authentication failed",
    "permission_denied": "permission denied",
    "invalid_request": "invalid request",
    "not_found": "model not found",
    "context_overflow": "input exceeds the context window",
    "circuit_open": "circuit open",
    "error": "request failed",
}


def missing_credentials_message(provider: str | None) -> str:
    """The one-line message for a model call without credentials."""

    spec = provider_spec(provider) if provider else None
    variable = spec.key_variable if spec is not None else None
    if not variable:
        return "no API key for the model provider (run awa setup; see awa doctor)"
    return f"no API key for {provider} (set {variable} in .env or run awa setup; see awa doctor)"


def _is_missing_credentials(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(marker in text for marker in _MISSING_CREDENTIALS):
            return True
        current = current.__cause__ or current.__context__
    return False


def classify_model_error(
    exc: BaseException,
    *,
    model: str | None = None,
    provider: str | None = None,
    now: Callable[[], float] = time.time,
) -> ModelProviderError | None:
    """Translate a model provider failure into a `ModelProviderError`.

    Args:
        provider: Provider name, when known from the model; else derived from the exception's
            module.
        now: Clock (Unix time) for turning an HTTP-date `Retry-After` into seconds.

    Returns:
        `exc` itself if it already is a `ModelProviderError`; a new `ModelRateLimitError` or
        `ModelProviderError` for recognized provider failures; `None` for anything else, including
        every other `AwaError`.

    Example:
        ```python
        try:
            response = await model.ainvoke(messages)
        except Exception as exc:
            error = classify_model_error(exc)
            if error is None or not error.retryable:
                raise
        ```
    """

    if isinstance(exc, ModelProviderError):
        return exc
    if isinstance(exc, AwaError) or not isinstance(exc, Exception):
        return None
    if _is_missing_credentials(exc):
        text = str(exc).lower()
        provider = (
            provider
            or _provider_of_class(type(exc))
            or next((name for name in ("anthropic", "openai") if name in text), None)
        )
        return ModelProviderError(
            missing_credentials_message(provider),
            kind="authentication",
            provider=provider,
            model=model,
            retryable=False,
        )
    status = _status_code(exc)
    kind = _kind(exc, status)
    if kind is None:
        return None
    provider = provider or _provider_of_class(type(exc))
    error_type = ModelRateLimitError if kind == "rate_limit" else ModelProviderError
    return error_type(
        _message(kind, provider, model, status, exc),
        kind=kind,
        provider=provider,
        model=model,
        status=status,
        retry_after=_retry_after(exc, now),
        retryable=kind in TRANSIENT_ERROR_KINDS,
    )


def model_identity(model: BaseChatModel) -> str:
    """A stable `provider:model` label for a chat model instance (class and object id without a name)."""

    llm_type = _llm_type(model)
    provider = _PROVIDERS_BY_LLM_TYPE.get(llm_type, llm_type)
    name = model_name(model)
    if name:
        return f"{provider}:{name}"
    return f"{provider}:{type(model).__name__}@{id(model):x}"


def model_name(model: BaseChatModel) -> str | None:
    """The model name a chat model instance is configured with, if it has one."""

    for attribute in _MODEL_NAME_ATTRIBUTES:
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def model_provider(model: BaseChatModel) -> str | None:
    """The provider of a chat model instance, when it is a known provider."""

    return _PROVIDERS_BY_LLM_TYPE.get(_llm_type(model)) or _provider_of_class(type(model))


def chat_model_class(provider: str) -> type | None:
    """The LangChain chat model class of `provider` (`anthropic`, `openai`, `google`), or
    `None` when its integration package is not installed."""

    spec = provider_spec(provider)
    if spec is None:
        return None
    try:
        value = getattr(importlib.import_module(spec.module), spec.chat_model, None)
    except ImportError:
        return None
    return value if isinstance(value, type) else None


def _llm_type(model: BaseChatModel) -> str:
    try:
        return str(model._llm_type)
    except Exception:  # noqa: BLE001 - an identity must never fail
        return type(model).__name__


def _status_code(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "http_status", None),
        getattr(exc, "status", None),
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 100:
            return candidate
    return None


def _kind(exc: BaseException, status: int | None) -> ModelErrorKind | None:
    for cls, kind in _LANGCHAIN_KINDS:
        if isinstance(exc, cls):
            return kind
    if status is not None and status >= 400:
        return _STATUS_KINDS.get(status, "server_error" if status >= 500 else "error")
    names = [cls.__name__ for cls in type(exc).__mro__]
    for fragment, kind in _NAME_KINDS:
        if any(fragment in name for name in names):
            return kind
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    if isinstance(exc, model_exceptions.ModelError):
        return "error"
    return None


def _provider_of_class(cls: type) -> str | None:
    """The provider whose package defines `cls` or one of its bases (nothing is imported)."""

    for base in cls.__mro__:
        provider = _PROVIDER_MODULES.get(base.__module__.split(".", 1)[0])
        if provider is not None:
            return provider
    return None


def _retry_after(exc: BaseException, now: Callable[[], float]) -> float | None:
    direct = getattr(exc, "retry_after", None)
    if isinstance(direct, int | float) and not isinstance(direct, bool) and direct >= 0:
        return float(direct)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    return retry_after_from_headers(headers, now=now())


def _message(
    kind: str, provider: str | None, model: str | None, status: int | None, exc: BaseException
) -> str:
    subject = f"Model {model!r}" if model else "Model request"
    context = [part for part in (provider, f"HTTP {status}" if status else None) if part]
    via = f" ({', '.join(context)})" if context else ""
    detail = preview(redact_secrets(str(getattr(exc, "message", None) or exc)), _MAX_DETAIL_CHARS)
    description = _KIND_DESCRIPTIONS.get(kind, "request failed")
    suffix = f": {type(exc).__name__}: {detail}" if detail else f": {type(exc).__name__}"
    hint = azure_error_hint(exc, status=status, provider=provider)
    return f"{subject} failed{via}, {description}{suffix}" + (f". Hint: {hint}" if hint else "")


# Error codes only Azure uses; they identify an Azure failure whatever the provider label says.
_AZURE_CODES = frozenset({"deploymentnotfound", "authenticationtypedisabled", "ratelimitreached"})


def azure_error_hint(
    exc: BaseException, *, status: int | None = None, provider: str | None = None
) -> str | None:
    """A one-line fix for the usual Azure OpenAI failures (401, 403, 404, 429), or `None`."""

    status = status if status is not None else _status_code(exc)
    code, text = _error_code_and_text(exc)
    if provider != "azure_openai" and code not in _AZURE_CODES and "azure" not in text:
        return None
    if code == "deploymentnotfound":
        return (
            "no deployment has this name: AZURE_OPENAI_DEPLOYMENT_NAME is the name you gave "
            "the deployment in the portal (Models + endpoints), not the model name"
        )
    if code == "authenticationtypedisabled" or (status == 403 and "key based auth" in text):
        return (
            "this resource refuses API keys: set AZURE_OPENAI_AUTH=entra with AZURE_TENANT_ID, "
            "AZURE_CLIENT_ID and AZURE_CLIENT_SECRET"
        )
    if status == 403:
        return (
            'the identity lacks the role "Cognitive Services OpenAI User" on the resource '
            "(or a firewall blocks this network)"
        )
    if status == 401:
        return (
            "the key (or Entra sign-in) is not valid for this resource: check "
            "AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT, or the Entra client secret and role"
        )
    if status == 404:
        return (
            "wrong endpoint or API version: use https://<resource>.openai.azure.com/ and leave "
            "OPENAI_API_VERSION unset for the current v1 API"
        )
    if status == 429 or code == "ratelimitreached":
        return (
            "the quota of this deployment (tokens or requests per minute) is used up: wait, or "
            "raise the quota in the portal"
        )
    if code == "content_filter":
        return "Azure's content filter blocked the request or the answer"
    return None


def _error_code_and_text(exc: BaseException) -> tuple[str, str]:
    """The lower-cased error code and message of a provider exception."""

    body = getattr(exc, "body", None)
    if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
        body = body["error"]
    fields = body if isinstance(body, Mapping) else {}
    code = getattr(exc, "code", None) or fields.get("code") or ""
    message = fields.get("message") or getattr(exc, "message", None) or exc
    return str(code).lower(), str(message).lower()


__all__ = [
    "KEY_VARIABLES",
    "classify_model_error",
    "missing_credentials_message",
    "model_identity",
    "model_name",
    "model_provider",
]
