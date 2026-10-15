"""Chat models: the base class, `init_chat_model`, generation outputs and typed model errors."""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core import exceptions as model_exceptions
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult, LLMResult

__all__ = [
    "BaseChatModel",
    "ChatGeneration",
    "ChatGenerationChunk",
    "ChatResult",
    "LLMResult",
    "LanguageModelInput",
    "init_chat_model",
    "model_exceptions",
]
