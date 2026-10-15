"""Blueprint models of an agent's `describe:`, `refine:` and `refinements:` sections.

`refine:` maps one to one onto LangChain: chat model constructor fields and agent middleware
by class name, checked against those classes when the blueprint loads.
"""

from __future__ import annotations

import inspect
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from awa.blueprints._schema_data import relative_inside
from awa.blueprints._schema_spec import _Spec

PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "google")
"""The provider sections of `refine:`."""

_SECTION_OF = {"azure_openai": "openai", "google_genai": "google"}  # by `model_provider`


class DescribeBlueprint(_Spec):
    """The agent's texts kept apart from the blueprint.

    Attributes:
        instructions_file: A text file relative to the blueprint with the agent's instructions
            (`instructions:` must then be empty).
        tool_docs: Per tool name, the description the model reads instead of the tool's own.
    """

    instructions_file: str | None = None
    tool_docs: dict[str, str] = Field(default_factory=dict)

    @field_validator("instructions_file")
    @classmethod
    def _inside_the_folder(cls, value: str | None) -> str | None:
        return relative_inside(value, "instructions_file") if value else value


class RefineBlueprint(_Spec):
    """Provider and middleware settings of one agent (`refine:`), applied per model attempt.

    A provider section applies only when the attempt's model (a fallback included) is of that
    provider.

    Attributes:
        model_kwargs: Chat model fields for every attempt; a key the class has no field for is
            sent with the call.
        anthropic: Fields of `ChatAnthropic`, plus `cache_ttl` (`5m` or `1h`).
        openai: Fields of `ChatOpenAI`; Azure OpenAI too.
        google: Fields of `ChatGoogleGenerativeAI`.
        middleware: Agent middleware by class name with its keyword arguments, e.g.
            `[{LLMToolSelectorMiddleware: {max_tools: 8}}]`.
    """

    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    anthropic: dict[str, Any] = Field(default_factory=dict)
    openai: dict[str, Any] = Field(default_factory=dict)
    google: dict[str, Any] = Field(default_factory=dict)
    middleware: list[dict[str, dict[str, Any]]] = Field(default_factory=list)

    @field_validator("anthropic", "openai", "google")
    @classmethod
    def _provider_fields(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        provider = info.field_name
        keys = set(value) - ({"cache_ttl"} if provider == "anthropic" else set())
        if provider == "anthropic" and value.get("cache_ttl") not in (None, "5m", "1h"):
            raise ValueError(f"anthropic.cache_ttl must be 5m or 1h, got {value['cache_ttl']!r}")
        from awa.models.errors import chat_model_class

        target = chat_model_class(provider)
        if target is not None and keys:
            fields = _fields(target)
            unknown = sorted(key for key in keys if key not in fields)
            if unknown:
                raise ValueError(
                    f"{provider}: {', '.join(unknown)} is not a field of {target.__name__}"
                )
        return value

    @field_validator("middleware")
    @classmethod
    def _middleware_classes(cls, value: list[dict[str, dict[str, Any]]]) -> Any:
        from awa._internal.upstream.agents import builtin_middleware

        for entry in value:
            if len(entry) != 1:
                raise ValueError("each middleware entry is one class name with its arguments")
            ((name, kwargs),) = entry.items()
            target = builtin_middleware(name)
            if target is None:
                raise ValueError(f"{name!r} is not a LangChain agent middleware")
            try:
                inspect.signature(target).bind(**(kwargs or {}))
            except TypeError as exc:
                raise ValueError(f"{name}: {exc}") from None
        return value

    def section(self, provider: str | None) -> dict[str, Any]:
        """The fields applied to a model attempt of `provider` (`model_kwargs` first)."""

        provider = _SECTION_OF.get(provider or "", provider)
        own = getattr(self, provider, {}) if provider in PROVIDERS else {}
        return {**self.model_kwargs, **{k: v for k, v in own.items() if k != "cache_ttl"}}


class Refinement(_Spec):
    """One line the post-build refinement added to an agent's instructions, with its provenance.

    Attributes:
        text: The line (tools, fields and procedures only, never a case value).
        pass_hat_k: `pass^2` of the cases with the line.
        raw_pass_rate: Pass rate of the raw design it was measured against.
    """

    text: str = Field(min_length=1, max_length=300)
    evidence: str = Field(default="", max_length=500)
    source: Literal["trace"] = "trace"
    pass_hat_k: float | None = None
    raw_pass_rate: float | None = None
    input_tokens: int | None = None
    raw_input_tokens: int | None = None

    @model_validator(mode="after")
    def _one_line(self) -> Refinement:
        if "\n" in self.text.strip():
            raise ValueError("a refinement is one line")
        return self


def _fields(target: type) -> set[str]:
    """Field names and aliases of a pydantic chat model class."""

    names: set[str] = set()
    for name, info in getattr(target, "model_fields", {}).items():
        names.add(name)
        if info.alias:
            names.add(info.alias)
    return names
