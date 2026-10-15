"""Agents and middleware: `create_agent`, the middleware types and the prebuilt middleware."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    ToolCallRequest,
    ToolRetryMiddleware,
    hook_config,
)
from langchain.agents.middleware.model_fallback import (
    _sanitize_request_for_fallback,  # private upstream helper, used only here
)
from langchain.agents.middleware.summarization import DEFAULT_SUMMARY_PROMPT
from langchain.agents.middleware.types import PrivateStateAttr
from langchain.tools import ToolRuntime

_MIDDLEWARE = "langchain.agents.middleware"


def sanitize_fallback_request(request: ModelRequest[Any], model: Any) -> ModelRequest[Any]:
    """Prepare `request` for a fallback `model` the way `ModelFallbackMiddleware` does.

    It strips provider-specific cache settings the fallback provider cannot take, using a private
    upstream helper in this one place.
    """

    return _sanitize_request_for_fallback(request, model).override(model=model)


def anthropic_prompt_caching(
    ttl: Literal["5m", "1h"],
) -> Callable[[ModelRequest[Any]], ModelRequest[Any]] | None:
    """What LangChain's `AnthropicPromptCachingMiddleware` with `ttl` does to a request, or `None`
    without langchain-anthropic.

    The middleware's private `_apply_caching` marks the request; it is used in this one place.
    """

    try:
        from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
    except ImportError:  # the optional `anthropic` extra
        return None
    marker = AnthropicPromptCachingMiddleware(ttl=ttl, unsupported_model_behavior="ignore")
    apply: Callable[[ModelRequest[Any]], ModelRequest[Any]] = marker._apply_caching
    return apply


def builtin_middleware(name: str) -> type | None:
    """The LangChain agent middleware class `name`, public in `langchain.agents.middleware`
    (or in `langchain_anthropic.middleware` for the Anthropic-only ones), or `None`."""

    for module_name in (_MIDDLEWARE, "langchain_anthropic.middleware"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if name not in getattr(module, "__all__", ()):
            continue
        value = getattr(module, name, None)
        if isinstance(value, type) and issubclass(value, AgentMiddleware):
            return value
    return None


__all__ = [
    "DEFAULT_SUMMARY_PROMPT",
    "AgentMiddleware",
    "AgentState",
    "ClearToolUsesEdit",
    "ContextEditingMiddleware",
    "ExtendedModelResponse",
    "ModelRequest",
    "ModelResponse",
    "PrivateStateAttr",
    "SummarizationMiddleware",
    "ToolCallRequest",
    "ToolRetryMiddleware",
    "ToolRuntime",
    "anthropic_prompt_caching",
    "builtin_middleware",
    "create_agent",
    "hook_config",
    "sanitize_fallback_request",
]
