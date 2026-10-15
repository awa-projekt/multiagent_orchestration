"""Errors of the HTTP server and how every error maps to an HTTP response.

Only deliberate library errors (`AwaError`) reach clients with their message; anything else is a
500 with a generic message (model provider failures keep their type, not their message), and the
full exception is logged. The status of every error is listed in the serving operations guide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from awa._internal.util import preview
from awa.errors import (
    AuthenticationError,
    AwaError,
    ConfigurationError,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidRequestError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResolutionError,
    NoPendingInterruptError,
    OptionalDependencyError,
    PermissionDeniedError,
    ProcessRunStateError,
    RateLimitExceededError,
    RunCancelledError,
    RunNotFoundError,
    RunTimeoutError,
    ServiceUnavailableError,
    SystemBusyError,
    SystemNotFoundError,
    ThreadNotFoundError,
    UsageLimitExceededError,
)

_MAX_MESSAGE_CHARS = 2000
_HIDDEN_ERRORS: tuple[type[AwaError], ...] = (
    OptionalDependencyError,
    ConfigurationError,
    ModelResolutionError,
)
# Raised by the server itself, never inside a run. Run events only carry an
# error's class name, and these names are also used by provider SDKs (an
# `AuthenticationError` of a model client), so run events never count them as safe.
_SERVER_ONLY_ERRORS: tuple[type[AwaError], ...] = (
    AuthenticationError,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidRequestError,
    NoPendingInterruptError,
    PermissionDeniedError,
    RateLimitExceededError,
    RunNotFoundError,
    ServiceUnavailableError,
    SystemBusyError,
    SystemNotFoundError,
    ThreadNotFoundError,
)


class UploadNotFoundError(AwaError):
    """No upload with this id for the caller (unknown, expired, or of another tenant)."""

    code = "awa.data.upload_not_found"


class UploadTimeoutError(AwaError):
    """An upload stalled (no data for too long) or took longer than allowed in total."""

    code = "awa.data.upload_timeout"
    retryable = True


class QuotaExceededError(RateLimitExceededError):
    """The tenant's stored data or its concurrent builds would exceed the quota."""

    code = "awa.data.quota_exceeded"


class BuildNotFoundError(AwaError):
    """No build with this id for the caller (unknown, expired, or of another tenant)."""

    code = "awa.build.not_found"


class BuildConflictError(AwaError):
    """The build is not in a state that allows this (answers need `needs_input`, runs `completed`)."""

    code = "awa.build.conflict"


class BuildDesignInvalidError(AwaError):
    """The build could not produce a valid design; `details["issues"]` lists code, path, message."""

    code = "awa.build.design_invalid"


class BuildUsageLimitError(UsageLimitExceededError):
    """The build used up its model call or token limits before it finished."""

    code = "awa.build.usage_limit"


# `awa.data` errors by code, so this module need not import the heavier `awa.data` package.
_DATA_STATUS = {
    "awa.data.too_large": 413,
    "awa.data.too_many_files": 413,
    "awa.data.unsupported_type": 415,
    "awa.data.type_mismatch": 415,
    "awa.data.zip_unsafe": 422,
}
_STATUS: tuple[tuple[tuple[type[AwaError], ...], int], ...] = (
    ((InvalidRequestError,), 400),
    ((AuthenticationError,), 401),
    ((PermissionDeniedError,), 403),
    ((UploadTimeoutError,), 408),
    ((SystemNotFoundError, ThreadNotFoundError, RunNotFoundError), 404),
    ((UploadNotFoundError, BuildNotFoundError), 404),
    (
        (
            SystemBusyError,
            NoPendingInterruptError,
            RunCancelledError,
            IdempotencyConflictError,
            ProcessRunStateError,
            BuildConflictError,
        ),
        409,
    ),
    ((IdempotencyMismatchError,), 422),
    ((UsageLimitExceededError, RateLimitExceededError), 429),
    ((ServiceUnavailableError,), 503),
    ((RunTimeoutError,), 504),
)
INTERNAL_ERROR_MESSAGE = "The server failed to process the request."
"""Message sent to clients in place of the details of an unexpected error."""

UPSTREAM_ERROR_MESSAGE = "The model provider failed to process the request."
"""Message sent to clients in place of the details of a model provider failure."""


@dataclass(frozen=True)
class ErrorInfo:
    """How an exception is reported to a client.

    Attributes:
        internal: The error was unexpected (logged with its traceback, details hidden).
        code: `AwaError.code`, or `"awa.internal"` for unexpected errors.
        retry_after: Seconds to wait before retrying (`Retry-After`), when known.
    """

    status: int
    type: str
    message: str
    internal: bool = False
    code: str = "awa.internal"
    retry_after: float | None = None
    retryable: bool = False

    def body(self, request_id: str | None = None) -> dict[str, Any]:
        """The `error` object of a JSON error response."""

        return {
            "type": self.type,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": request_id,
        }

    def headers(self) -> dict[str, str]:
        """Response headers for this error (`Retry-After`, `WWW-Authenticate`)."""

        headers: dict[str, str] = {}
        if self.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if self.retry_after is not None:
            headers["Retry-After"] = str(max(1, math.ceil(self.retry_after)))
        return headers


def describe_error(exc: BaseException) -> ErrorInfo:
    """Map an exception to what a client receives; internals of non-library errors are hidden."""

    name = type(exc).__name__
    if not isinstance(exc, AwaError) or isinstance(exc, _HIDDEN_ERRORS):
        return ErrorInfo(500, "InternalError", INTERNAL_ERROR_MESSAGE, internal=True)
    retryable = bool(exc.retryable)
    if isinstance(exc, ModelProviderError):
        # The provider is overloaded or down (503: retry later) or refused
        # the request (502); its own message is never passed on.
        overloaded = isinstance(exc, ModelRateLimitError) or retryable
        return ErrorInfo(
            503 if overloaded else 502,
            name,
            UPSTREAM_ERROR_MESSAGE,
            internal=True,
            code=exc.code,
            retry_after=exc.retry_after if overloaded else None,
            retryable=retryable,
        )
    status = _DATA_STATUS.get(exc.code) or next(
        (code for types, code in _STATUS if isinstance(exc, types)), 422
    )
    retry_after = getattr(exc, "retry_after", None)
    return ErrorInfo(
        status,
        name,
        preview(str(exc), _MAX_MESSAGE_CHARS),
        code=exc.code,
        retry_after=retry_after if isinstance(retry_after, int | float) else None,
        retryable=retryable,
    )


def is_safe_error_type(name: str) -> bool:
    """Whether an `ErrorEvent.error_type` may be shown with its message (run library errors).

    The server's own errors (names shared with provider SDK errors) and hidden ones do not count.
    """

    return name in _safe_error_names()


def _safe_error_names() -> set[str]:
    names = {"CancelledError"}
    hidden = (*_HIDDEN_ERRORS, ModelProviderError)
    pending: list[type[BaseException]] = [AwaError]
    while pending:
        cls = pending.pop()
        if not issubclass(cls, hidden) and cls not in _SERVER_ONLY_ERRORS:
            names.add(cls.__name__)
        pending.extend(cls.__subclasses__())
    return names


__all__ = [
    "INTERNAL_ERROR_MESSAGE",
    "UPSTREAM_ERROR_MESSAGE",
    "AuthenticationError",
    "BuildConflictError",
    "BuildDesignInvalidError",
    "BuildNotFoundError",
    "BuildUsageLimitError",
    "ErrorInfo",
    "IdempotencyConflictError",
    "IdempotencyMismatchError",
    "InvalidRequestError",
    "NoPendingInterruptError",
    "PermissionDeniedError",
    "QuotaExceededError",
    "RateLimitExceededError",
    "RunCancelledError",
    "RunNotFoundError",
    "RunTimeoutError",
    "ServiceUnavailableError",
    "SystemBusyError",
    "SystemNotFoundError",
    "ThreadNotFoundError",
    "UploadNotFoundError",
    "UploadTimeoutError",
    "describe_error",
    "is_safe_error_type",
]
