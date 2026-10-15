"""Named model profiles and model resolution; nothing is constructed until a model is first needed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from pydantic import ValidationError

from awa._internal.upstream.models import BaseChatModel, init_chat_model
from awa.core.config import env_value
from awa.core.providers import PROVIDERS, ProviderSpec
from awa.core.settings import Settings, get_settings
from awa.errors import ModelProviderError, ModelResolutionError
from awa.models._azure import entra_provider, init_azure_v1, v1_base_url
from awa.models._http import add_loop_local_clients
from awa.models.capabilities import apply_capabilities
from awa.models.errors import missing_credentials_message
from awa.models.guard import ensure_model_request_allowed, model_requests_allowed
from awa.models.settings import ModelSettings

ModelFactory = Callable[[], BaseChatModel]
"""A zero-argument callable that builds a chat model."""

_AZURE_PREFIX = "azure_openai:"
# Providers whose clients retry on their own unless built with `max_retries=0`.
_SDK_RETRY_PREFIXES = ("openai:", "azure_openai:", "anthropic:")
# Providers whose LangChain integration has no request timeout unless one is given (a
# stalled connection would wait forever) and whose SDK clients pool connections bound to
# the first event loop (a model used by a later `asyncio.run` fails with "Event loop is
# closed"): awa sets a timeout and gives them loop-local HTTP clients.
_OPENAI_PREFIXES = ("openai:", "azure_openai:")
DEFAULT_REQUEST_TIMEOUT = 600.0
"""Request timeout (seconds) of OpenAI and Azure OpenAI models when none is set."""


@dataclass(frozen=True)
class _Profile:
    source: BaseChatModel | str | ModelFactory
    settings: ModelSettings | None


class ModelRegistry:
    """Maps profile names to chat models, constructed lazily and cached.

    The built-in profiles `default`, `fast` and `strong` come from `AWA_DEFAULT_MODEL`,
    `AWA_FAST_MODEL` and `AWA_STRONG_MODEL`. `azure_openai:` models get deployment, API version,
    endpoint and key from `Settings`; no temperature is forced (see `awa.models.capabilities`).

    Example:
        ```python
        registry = ModelRegistry.default()
        registry.register("summarizer", "openai:gpt-6-luna", settings=ModelSettings(temperature=0))
        agent = Agent("summarizer", model="summarizer")
        ```
    """

    _default: ClassVar[ModelRegistry | None] = None

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._profiles: dict[str, _Profile] = {}
        self._cache: dict[str, BaseChatModel] = {}
        self.register("default", self._settings.default_model)
        self.register("fast", self._settings.fast_model)
        self.register("strong", self._settings.strong_model)

    @classmethod
    def default(cls) -> ModelRegistry:
        """Return the process-wide registry used when no registry is given."""

        if cls._default is None:
            cls._default = cls()
        return cls._default

    @property
    def settings(self) -> Settings:
        """The configuration this registry builds models with."""

        return self._settings

    def register(
        self,
        profile: str,
        model: BaseChatModel | str | ModelFactory,
        *,
        settings: ModelSettings | None = None,
    ) -> None:
        """Register (or replace) a profile.

        Args:
            profile: Profile name; must not contain `:`.
            model: A chat model, a `provider:model` string or a zero-argument factory, evaluated on
                first `get`.
            settings: Construction fields are only valid for a string `model`; resilience fields apply
                to every agent using the profile.

        Raises:
            ValueError: If `profile` is empty or contains `:`.
        """

        if not profile or ":" in profile:
            raise ValueError(
                f"Invalid model profile name {profile!r}: must be non-empty without ':'"
            )
        if not isinstance(model, BaseChatModel | str) and not callable(model):
            raise TypeError(
                f"Profile {profile!r} needs a BaseChatModel, a model string or a factory, "
                f"got {type(model).__name__}"
            )
        if (
            settings is not None
            and settings.has_construction_settings
            and not isinstance(model, str)
        ):
            raise TypeError("Model construction settings can only be registered for a model string")
        self._profiles[profile] = _Profile(model, settings)
        self._cache.pop(profile, None)

    def get(self, profile: str, *, settings: ModelSettings | None = None) -> BaseChatModel:
        """Return the model of `profile`, constructing and caching it on first use.

        `settings` that change how the model is built give a new, uncached model.

        Raises:
            ModelResolutionError: If the profile is unknown, `settings` cannot be applied, or the model
                cannot be constructed.
        """

        entry = self._profiles.get(profile)
        if entry is None:
            known = ", ".join(sorted(self._profiles))
            raise ModelResolutionError(f"Unknown model profile {profile!r} (known: {known})")
        # A retry policy changes how a model is built too: it disables SDK-internal retries.
        if settings is not None and (
            settings.has_construction_settings
            or settings.retry is not None
            or (settings.effort is not None and isinstance(entry.source, str))
        ):
            if isinstance(entry.source, str):
                base = entry.settings or ModelSettings()
                return self.create(entry.source, base.merge(settings))
            if settings.has_construction_settings:
                raise ModelResolutionError(
                    f"Model settings cannot be applied to profile {profile!r}: it was "
                    "registered as a model instance or factory, not a model string"
                )
        cached = self._cache.get(profile)
        if cached is None:
            cached = self._build(profile, entry)
            self._cache[profile] = cached
        return cached

    def create(self, model: str, settings: ModelSettings | None = None) -> BaseChatModel:
        """Construct a new chat model from a `provider:model` string.

        Credentials held in `Settings` (environment or `.env`) are passed explicitly. OpenAI and Azure
        models get loop-local HTTP clients unless `settings.extra` sets its own.

        Raises:
            ModelResolutionError: If the provider is unknown, its package is missing, or the model
                rejects its configuration.
        """

        kwargs = settings.to_kwargs() if settings is not None else {}
        if (
            settings is not None
            and settings.retry is not None
            and "max_retries" not in kwargs
            and model.startswith(_SDK_RETRY_PREFIXES)
        ):
            # awa retries itself: provider-internal retries would hide attempts.
            kwargs["max_retries"] = 0
        apply_capabilities(
            model,
            kwargs,
            settings.effort if settings is not None else None,
            responses_api=self._settings.openai_responses_api,
        )
        azure_v1 = model.startswith(_AZURE_PREFIX) and self._uses_azure_v1()
        if model.startswith(_OPENAI_PREFIXES):
            kwargs.setdefault("timeout", DEFAULT_REQUEST_TIMEOUT)
            add_loop_local_clients(model, kwargs, chat_openai=azure_v1)
        self._credentials(model, kwargs, azure_v1=azure_v1)
        try:
            built = init_azure_v1(**kwargs) if azure_v1 else init_chat_model(model, **kwargs)
            return cast(BaseChatModel, built)
        except Exception as exc:
            # Provider validation errors can echo every constructor argument,
            # including the API key passed above: describe the error without
            # its input, redact known secrets, and drop the original exception
            # (as cause *and* context) whenever it could carry a secret.
            secrets = [str(value) for key, value in kwargs.items() if _is_secret(key, value)]
            detail = _describe_error(exc)
            for secret in secrets:
                detail = detail.replace(secret, "***")
            error = ModelResolutionError(f"Cannot create chat model {model!r}: {detail}")
            unsafe = isinstance(exc, ValidationError) or any(s in str(exc) for s in secrets)
            if not unsafe:
                raise error from exc
        raise error

    def _credentials(self, model: str, kwargs: dict[str, Any], *, azure_v1: bool) -> None:
        """Add the credentials `Settings` and the project `.env` hold for `model`'s provider.

        Raises:
            ModelProviderError: If an `anthropic:` or `openai:` model has no credentials at all.
        """

        settings = self._settings
        if azure_v1:
            self._azure_v1_kwargs(model, kwargs)
        elif model.startswith(_AZURE_PREFIX):
            self._azure_dated_kwargs(kwargs)
        elif model.startswith("anthropic:") and settings.anthropic_api_key:
            kwargs.setdefault("api_key", settings.anthropic_api_key.get_secret_value())
        elif model.startswith("openai:") and settings.openai_api_key:
            kwargs.setdefault("api_key", settings.openai_api_key.get_secret_value())
        elif (other := _other_provider(model)) is not None:
            # A value only in the project `.env` reaches these integrations too.
            argument = "base_url" if other.local else "api_key"
            value = next((v for name in other.variables() if (v := env_value(name))), None)
            if value and not any(kwargs.get(name) for name in (argument, *_OWN_AUTHENTICATION)):
                kwargs[argument] = value
        elif model.startswith(("anthropic:", "openai:")) and _has_no_credentials(kwargs):
            # Fail before loading the provider package: no key would only surface as a
            # TypeError of the SDK on the first request.
            provider = model.partition(":")[0]
            raise ModelProviderError(
                missing_credentials_message(provider),
                kind="authentication",
                provider=provider,
                model=model,
            )

    def _uses_azure_v1(self) -> bool:
        """Whether `azure_openai:` models use the v1 API: an endpoint, and no dated version."""

        return bool(self._settings.azure_openai_endpoint) and not self._settings.azure_api_version

    def _azure_v1_kwargs(self, model: str, kwargs: dict[str, Any]) -> None:
        """Arguments of `ChatOpenAI` on the v1 API of the Azure endpoint."""

        settings = self._settings
        kwargs.setdefault("model", settings.azure_deployment or model.partition(":")[2])
        kwargs.setdefault("base_url", v1_base_url(str(settings.azure_openai_endpoint)))
        if "api_key" in kwargs or "openai_api_key" in kwargs:
            return
        if settings.azure_auth == "entra":
            kwargs["api_key"] = entra_provider(settings)
        elif settings.azure_openai_api_key:
            kwargs["api_key"] = settings.azure_openai_api_key.get_secret_value()
        else:
            # Without this, `ChatOpenAI` would send OPENAI_API_KEY to Azure.
            raise ModelProviderError(
                missing_credentials_message("azure_openai"),
                kind="authentication",
                provider="azure_openai",
                model=model,
            )

    def _azure_dated_kwargs(self, kwargs: dict[str, Any]) -> None:
        """Arguments of `AzureChatOpenAI` on the dated route (`OPENAI_API_VERSION` is set)."""

        settings = self._settings
        for key, value in (
            ("azure_deployment", settings.azure_deployment),
            ("api_version", settings.azure_api_version),
            ("azure_endpoint", settings.azure_openai_endpoint),
        ):
            if value:
                kwargs.setdefault(key, value)
        if settings.azure_auth == "entra" and "api_key" not in kwargs:
            provider = entra_provider(settings)
            kwargs.setdefault("azure_ad_token_provider", provider)
            kwargs.setdefault("azure_ad_async_token_provider", provider.aget)
        elif settings.azure_openai_api_key:
            kwargs.setdefault("api_key", settings.azure_openai_api_key.get_secret_value())

    def settings_for(self, model: str | BaseChatModel | None) -> ModelSettings | None:
        """The settings registered for a model reference's profile, or `None`."""

        if isinstance(model, BaseChatModel):
            return None
        entry = self._profiles.get("default" if model is None else model)
        return entry.settings if entry is not None else None

    def profiles(self) -> list[str]:
        """Return the registered profile names, sorted."""

        return sorted(self._profiles)

    def accepts_construction_settings(self, reference: str) -> bool:
        """Whether `reference` is built from a string, so construction settings apply to it."""

        entry = self._profiles.get(reference)
        return entry is None or isinstance(entry.source, str)

    def __contains__(self, profile: object) -> bool:
        return profile in self._profiles

    def _build(self, profile: str, entry: _Profile) -> BaseChatModel:
        source = entry.source
        if isinstance(source, BaseChatModel):
            return source
        if isinstance(source, str):
            return self.create(source, entry.settings)
        try:
            built = source()
        except Exception as exc:
            raise ModelResolutionError(
                f"Model factory of profile {profile!r} failed: {exc}"
            ) from exc
        if not isinstance(built, BaseChatModel):
            raise ModelResolutionError(
                f"Model factory of profile {profile!r} returned {type(built).__name__}, "
                "not a BaseChatModel"
            )
        return built


# Settings of a model that bring their own way to authenticate (or a gateway that needs none).
_OWN_AUTHENTICATION = (
    "api_key",
    "anthropic_api_key",
    "openai_api_key",
    "auth_token",
    "default_headers",
    "base_url",
    "anthropic_api_url",
    "openai_api_base",
)
_OWN_AUTHENTICATION_ENV = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
)


def _other_provider(model: str) -> ProviderSpec | None:
    """`model`'s provider when `Settings` holds no key for it (Google, Mistral AI, Groq, Ollama)."""

    prefix = model.partition(":")[0]
    keyed = ("anthropic", "openai", "azure_openai")
    return next((p for p in PROVIDERS if p.name == prefix and prefix not in keyed), None)


def _has_no_credentials(kwargs: dict[str, Any]) -> bool:
    """Whether an `anthropic:` or `openai:` model has no API key from any source."""

    if any(kwargs.get(name) for name in _OWN_AUTHENTICATION):
        return False
    return not any(env_value(name) for name in _OWN_AUTHENTICATION_ENV)


_SECRET_MARKERS = ("key", "token", "secret", "password", "credential")


def _is_secret(name: str, value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) >= 4
        and any(marker in name.lower() for marker in _SECRET_MARKERS)
    )


def _describe_error(exc: Exception) -> str:
    """Describe a construction error without echoing the constructor input."""

    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'model'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        return f"{exc.error_count()} validation error(s) for {exc.title}: {problems}"
    return str(exc)


def resolve_model(
    model: str | BaseChatModel | None,
    *,
    settings: ModelSettings | None = None,
    registry: ModelRegistry | None = None,
) -> BaseChatModel:
    """Turn a model reference into a chat model.

    Args:
        model: A chat model (returned as-is), `None` (the `default` profile), a profile name, or a
            `provider:model` string for LangChain's `init_chat_model`.
        registry: Registry to resolve profiles in; defaults to `ModelRegistry.default()`.

    Raises:
        ModelResolutionError: If the reference cannot be resolved, construction settings come with
            a model instance, or real model requests are blocked for a non-test model.
    """

    if isinstance(model, BaseChatModel):
        if settings is not None and settings.has_construction_settings:
            raise ModelResolutionError(
                "Model settings cannot be applied to a model instance; configure the instance "
                "directly"
            )
        resolved = model
    else:
        registry = registry if registry is not None else ModelRegistry.default()
        resolved = _resolve_name("default" if model is None else model, settings, registry)
    ensure_model_request_allowed(resolved)
    return resolved


def _resolve_name(
    name: str, settings: ModelSettings | None, registry: ModelRegistry
) -> BaseChatModel:
    """The model a profile name, a plugin model name or a `provider:model` string names."""

    profile = registry._profiles.get(name)
    factory = _plugin_factory(name) if profile is None else None
    if factory is not None:
        if settings is not None and settings.has_construction_settings:
            raise ModelResolutionError(
                f"Model settings cannot be applied to the plugin model {name!r}; configure "
                "it in its factory"
            )
        built = factory()
        if not isinstance(built, BaseChatModel):
            raise ModelResolutionError(
                f"The plugin model {name!r} returned a {type(built).__name__}, not a chat model"
            )
        return built
    if not model_requests_allowed() and (profile is None or isinstance(profile.source, str)):
        # A model string never names an offline test model: refuse before constructing.
        raise ModelResolutionError(
            f"Real model requests are blocked (set_allow_model_requests(False)); refusing "
            f"to create model {name!r}"
        )
    if profile is not None:
        return registry.get(name, settings=settings)
    return registry.create(name, settings)


def _plugin_factory(name: str) -> Callable[[], Any] | None:
    """The factory a plugin registered as a `models` plugin (a bare name, no `provider:`)."""

    if ":" in name:
        return None
    from awa.plugins import current_project, find

    return find("models", name, project=current_project(), default=None)  # type: ignore[no-any-return]


__all__ = ["DEFAULT_REQUEST_TIMEOUT", "ModelFactory", "ModelRegistry", "resolve_model"]
