"""Which request parameters a model accepts: sampling (`temperature`, `top_p`, `top_k`) and effort.

Name-based and conservative: current Claude models reject sampling and take an effort; OpenAI
and Azure reasoning models take no temperature and get an effort only on the Responses API
(then with `store=False` and encrypted reasoning); other known providers take a temperature;
an unknown provider gets nothing forced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""Reasoning effort, from the cheapest to the deepest."""

SAMPLING_PARAMETERS = ("temperature", "top_p", "top_k")
"""Request parameters that set sampling."""

_CLAUDE_VERSION = re.compile(r"claude-(?:[a-z]+-)?(\d+)(?:[-.](\d{1,2}))?(?![\d])")
_OPENAI_REASONING = re.compile(r"^(o\d|gpt-(?:[5-9]|\d{2}))", re.IGNORECASE)
_ENCRYPTED_REASONING = "reasoning.encrypted_content"
_OPENAI_EFFORTS: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model accepts.

    Attributes:
        sampling: Whether `temperature`, `top_p` and `top_k` may be sent.
        effort: Whether a reasoning effort may be sent.
    """

    sampling: bool
    effort: bool


def model_capabilities(model: str) -> ModelCapabilities:
    """The capabilities of `provider:model` (see the module docstring for the rules)."""

    provider, _, name = model.partition(":")
    if not name:
        return ModelCapabilities(sampling=False, effort=False)
    name = name.lower()
    if provider == "anthropic":
        match = _CLAUDE_VERSION.search(name)
        if match is None:  # a family without a number in its name: a current model
            return ModelCapabilities(sampling=False, effort=True)
        version = (int(match.group(1)), int(match.group(2) or 0))
        current = version >= (4, 7)
        return ModelCapabilities(sampling=not current, effort=current)
    if provider in ("openai", "azure_openai"):
        reasoning = bool(_OPENAI_REASONING.match(name))
        return ModelCapabilities(sampling=not reasoning, effort=reasoning)
    return ModelCapabilities(sampling=True, effort=False)


def apply_capabilities(
    model: str, kwargs: dict[str, object], effort: Effort | None, *, responses_api: bool = False
) -> None:
    """Fit the constructor arguments of `model` to what it accepts, in place.

    Sampling is removed for a model that rejects it. An `effort` becomes `reasoning_effort` where
    the model takes one, else pins `temperature=0` unless one is set; explicit `kwargs` win. With
    `responses_api`, an OpenAI or Azure reasoning model with an effort is switched to the Responses
    API unless `kwargs` choose the API themselves.
    """

    caps = model_capabilities(model)
    if not caps.sampling:
        for name in SAMPLING_PARAMETERS:
            kwargs.pop(name, None)
    if effort is None:
        return
    if caps.effort:
        if not model.startswith(("openai:", "azure_openai:")):
            kwargs.setdefault("reasoning_effort", effort)
            return
        if responses_api and "use_responses_api" not in kwargs:
            kwargs["use_responses_api"] = True
            _stateless_responses(kwargs)
        if kwargs.get("use_responses_api"):
            # Chat Completions rejects function tools together with a reasoning effort
            # (HTTP 400 on current reasoning models); the Responses API takes both.
            kwargs.setdefault("reasoning_effort", _OPENAI_EFFORTS[effort])
    elif caps.sampling:
        kwargs.setdefault("temperature", 0)


def _stateless_responses(kwargs: dict[str, object]) -> None:
    """Stop Responses API storage and pass reasoning back encrypted, unless `store` is set."""

    if "store" in kwargs or kwargs.get("use_previous_response_id"):
        return
    kwargs["store"] = False
    include = kwargs.get("include")
    items = list(include) if isinstance(include, (list, tuple)) else []
    if _ENCRYPTED_REASONING not in items:
        items.append(_ENCRYPTED_REASONING)
    kwargs["include"] = items


__all__ = [
    "SAMPLING_PARAMETERS",
    "Effort",
    "ModelCapabilities",
    "apply_capabilities",
    "model_capabilities",
]
