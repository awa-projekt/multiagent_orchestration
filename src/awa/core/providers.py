"""The model providers awa knows, in one table, and which one the credentials point at.

`detect(env)` returns the first provider in table order whose credentials are complete. The model
ids are the ones of the Current models table in docs/develop/models.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

Lookup = Callable[[str], str | None]
"""Returns the non-empty value of a variable, or `None`."""


@dataclass(frozen=True)
class ProviderSpec:
    """One model provider.

    Attributes:
        name: The model prefix (`anthropic`, `openai`, `azure_openai`, ...).
        required: The variables that must all be set (for Azure: with key sign-in).
        any_of: Variables of which one is enough (Google: GOOGLE_API_KEY or GEMINI_API_KEY).
        profiles: The `default`, `fast` and `strong` models when chosen automatically (Azure: built
            from the deployment names).
        key_variable: The variable `awa setup` asks for as the key.
        own_sign_in: Variables that let the SDK sign in without the key (a gateway).
        local: A server reached by its address: no key, and `http://` is fine.
    """

    name: str
    title: str
    required: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    profiles: tuple[str, str, str] = ("", "", "")
    package: str = ""
    chat_model: str = ""
    extra: str | None = None
    key_variable: str | None = None
    own_sign_in: tuple[str, ...] = ()
    local: bool = False

    @property
    def module(self) -> str:
        """The import name of `package`."""

        return self.package.replace("-", "_")

    def variables(self) -> tuple[str, ...]:
        """Every variable the provider reads for its credentials."""

        return (*self.required, *self.any_of)

    def fields(self) -> tuple[str, ...]:
        """The variables a setup form offers: the required ones, one of `any_of`, the rest."""

        return (*self.required, *self.any_of[:1], *self.optional)

    def needed(self, get: Lookup) -> tuple[str, ...]:
        """The variables needed now: Azure's by its sign-in, of `any_of` the one set (else the
        first)."""

        if self.name == "azure_openai":
            entra = (get("AZURE_OPENAI_AUTH") or "key").strip().lower() == "entra"
            sign_in = _ENTRA if entra else ("AZURE_OPENAI_API_KEY",)
            return ("AZURE_OPENAI_ENDPOINT", *sign_in, "AZURE_OPENAI_DEPLOYMENT_NAME")
        if not self.any_of:
            return self.required
        return (*self.required, next((n for n in self.any_of if get(n)), self.any_of[0]))

    def missing(self, get: Lookup) -> tuple[str, ...]:
        """The needed variables `get` does not hold."""

        def present(name: str) -> bool:
            alternative = name == "AZURE_OPENAI_DEPLOYMENT_NAME" and get("AZURE_OPENAI_DEPLOYMENT")
            return bool(get(name) or alternative)

        return tuple(name for name in self.needed(get) if not present(name))

    def complete(self, get: Lookup) -> bool:
        """Whether the credentials in `get` are complete for this provider."""

        return not self.missing(get)

    def models(self, get: Lookup) -> tuple[str, str, str]:
        """The `default`, `fast` and `strong` models when this provider is chosen."""

        if self.name != "azure_openai":
            return self.profiles
        deployment = get("AZURE_OPENAI_DEPLOYMENT_NAME") or get("AZURE_OPENAI_DEPLOYMENT") or ""
        fast = get("AZURE_OPENAI_DEPLOYMENT_FAST") or deployment
        strong = get("AZURE_OPENAI_DEPLOYMENT_STRONG") or deployment
        return (f"azure_openai:{deployment}", f"azure_openai:{fast}", f"azure_openai:{strong}")


_ENTRA = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")

PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "anthropic",
        "Anthropic",
        required=("ANTHROPIC_API_KEY",),
        profiles=(
            "anthropic:claude-sonnet-5-5",
            "anthropic:claude-haiku-5-5",
            "anthropic:claude-opus-5-5",
        ),
        package="langchain-anthropic",
        chat_model="ChatAnthropic",
        extra="anthropic",
        key_variable="ANTHROPIC_API_KEY",
        own_sign_in=("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    ),
    ProviderSpec(
        "openai",
        "OpenAI",
        required=("OPENAI_API_KEY",),
        profiles=("openai:gpt-6.1-sol", "openai:gpt-6-luna", "openai:gpt-6-astra"),
        package="langchain-openai",
        chat_model="ChatOpenAI",
        extra="openai",
        key_variable="OPENAI_API_KEY",
        own_sign_in=("OPENAI_BASE_URL", "OPENAI_API_BASE"),
    ),
    ProviderSpec(
        "azure_openai",
        "Azure OpenAI",
        required=("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT_NAME"),
        optional=("OPENAI_API_VERSION", "AZURE_OPENAI_AUTH", *_ENTRA, "AZURE_AUTHORITY_HOST"),
        package="langchain-openai",
        chat_model="AzureChatOpenAI",
        extra="openai",
        key_variable="AZURE_OPENAI_API_KEY",
    ),
    ProviderSpec(
        "google_genai",
        "Google Gemini",
        any_of=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        profiles=(
            "google_genai:gemini-3.8-flash",
            "google_genai:gemini-3.5-flash-lite",
            "google_genai:gemini-3.1-pro-preview",
        ),
        package="langchain-google-genai",
        chat_model="ChatGoogleGenerativeAI",
        key_variable="GOOGLE_API_KEY",
    ),
    ProviderSpec(
        "mistralai",
        "Mistral AI",
        required=("MISTRAL_API_KEY",),
        profiles=(
            "mistralai:mistral-medium-3-5-26-04",
            "mistralai:mistral-small-4-0-26-03",
            "mistralai:mistral-large-4",
        ),
        package="langchain-mistralai",
        chat_model="ChatMistralAI",
        key_variable="MISTRAL_API_KEY",
    ),
    ProviderSpec(
        "groq",
        "Groq",
        required=("GROQ_API_KEY",),
        profiles=(
            "groq:llama-3.3-70b-versatile",
            "groq:llama-3.1-8b-instant",
            "groq:llama-3.3-70b-versatile",
        ),
        package="langchain-groq",
        chat_model="ChatGroq",
        key_variable="GROQ_API_KEY",
    ),
    ProviderSpec(
        "ollama",
        "Ollama (this computer or your network)",
        required=("OLLAMA_HOST",),
        profiles=("ollama:gpt-oss:20b", "ollama:gpt-oss:20b", "ollama:gpt-oss:20b"),
        package="langchain-ollama",
        chat_model="ChatOllama",
        local=True,
    ),
)
"""Every provider, in the order `detect` tries them."""

ALIASES: Mapping[str, str] = {
    "google": "google_genai",
    "gemini": "google_genai",
    "mistral": "mistralai",
    "azure": "azure_openai",
    "local": "ollama",
}
"""Other names people use for a provider, mapped to its model prefix."""


def provider(name: str) -> ProviderSpec | None:
    """The provider called `name` (its model prefix or a common other name)."""

    wanted = ALIASES.get(name.strip().lower(), name.strip().lower())
    return next((item for item in PROVIDERS if item.name == wanted), None)


def provider_of(model: str) -> str | None:
    """The provider prefix of a `provider:model` string (also `claude-...` and `gpt-...`)."""

    if ":" in model:
        prefix = model.split(":", 1)[0].strip().lower()
        return ALIASES.get(prefix, prefix)
    lowered = model.lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if lowered.startswith("gemini"):
        return "google_genai"
    return None


def detect(get: Lookup) -> ProviderSpec | None:
    """The first provider in `PROVIDERS` whose credentials `get` holds completely, or `None`."""

    return next((item for item in PROVIDERS if item.complete(get)), None)


def complete_providers(get: Lookup) -> tuple[ProviderSpec, ...]:
    """Every provider whose credentials are complete, in table order."""

    return tuple(item for item in PROVIDERS if item.complete(get))


__all__ = [
    "ALIASES",
    "PROVIDERS",
    "Lookup",
    "ProviderSpec",
    "complete_providers",
    "detect",
    "provider",
    "provider_of",
]
