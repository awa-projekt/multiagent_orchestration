"""A process-wide switch that blocks requests to real model providers; offline fakes always pass."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from awa._internal.upstream.models import BaseChatModel
from awa.errors import ModelResolutionError

OFFLINE_LLM_TYPES = frozenset(
    {
        "awa-fake",
        "awa-function",
        "fake-chat-model",
        "fake-list-chat-model",
        "fake-messages-list-chat-model",
        "generic-fake-chat-model",
        "parrot-fake-chat-model",
    }
)
"""The `_llm_type` of the test models that never make a network request."""

_allowed = True


def set_allow_model_requests(allowed: bool) -> None:
    """Allow or block requests to real (non-test) chat models for the whole process."""

    global _allowed
    _allowed = allowed


def model_requests_allowed() -> bool:
    """Whether requests to real chat models are currently allowed."""

    return _allowed


@contextmanager
def allow_model_requests(allowed: bool) -> Iterator[None]:
    """Set whether real model requests are allowed for the enclosed block (process-wide).

    Example:
        ```python
        with allow_model_requests(False):
            await agent.run("hi")   # ModelResolutionError unless the model is a fake
        ```
    """

    previous = _allowed
    set_allow_model_requests(allowed)
    try:
        yield
    finally:
        set_allow_model_requests(previous)


def is_offline_model(model: BaseChatModel) -> bool:
    """Whether `model` is a test model that never makes network requests."""

    return model._llm_type in OFFLINE_LLM_TYPES


def ensure_model_request_allowed(model: BaseChatModel) -> None:
    """Raise if real model requests are blocked and `model` is not an offline test model.

    Raises:
        ModelResolutionError: If requests are blocked and `model` is a real model.
    """

    if _allowed or is_offline_model(model):
        return
    raise ModelResolutionError(
        f"Real model requests are blocked (set_allow_model_requests(False)); refusing to use "
        f"{type(model).__name__}. Use awa.testing.FakeChatModel or FunctionChatModel, or "
        "allow requests with allow_model_requests(True)."
    )


__all__ = [
    "allow_model_requests",
    "ensure_model_request_allowed",
    "is_offline_model",
    "model_requests_allowed",
    "set_allow_model_requests",
]
