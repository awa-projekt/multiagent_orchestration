"""The model registry and the provider table: sampling parameters and reasoning effort per model and role, key variables and the provider of a model."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from awa.blueprints import Blueprint
from awa.blueprints._build_roles import role_model_settings
from awa.core.providers import PROVIDERS, provider_of
from awa.core.settings import Settings
from awa.errors import ModelResolutionError
from awa.models import (
    DEFAULT_REQUEST_TIMEOUT,
    ModelRegistry,
    ModelSettings,
    allow_model_requests,
    ensure_model_request_allowed,
    is_offline_model,
    model_requests_allowed,
    resolve_model,
    set_allow_model_requests,
)
from awa.models.capabilities import apply_capabilities, model_capabilities
from awa.models.errors import KEY_VARIABLES, missing_credentials_message
from awa.testing import FakeChatModel, FunctionChatModel


def _settings(**overrides: Any) -> Settings:
    return Settings(**overrides)


class _RealModel(FakeChatModel):
    """Stands in for a provider model: same behavior, not an offline llm type."""

    @property
    def _llm_type(self) -> str:
        return "provider"


@pytest.fixture
def fake_init() -> Iterator[Any]:
    # The loop-local HTTP clients are covered in test_models_event_loops.py.
    with (
        patch("awa.models.registry.init_chat_model") as init,
        patch("awa.models.registry.add_loop_local_clients"),
    ):
        init.side_effect = lambda model, **kwargs: FakeChatModel([f"{model}:{kwargs}"])
        yield init


# --------------------------------------------------------------------------- ModelSettings


def test_model_settings_kwargs_skip_unset_fields_and_include_extra() -> None:
    settings = ModelSettings(temperature=0.0, max_tokens=100, extra={"top_p": 0.5})

    assert settings.to_kwargs() == {"temperature": 0.0, "max_tokens": 100, "top_p": 0.5}
    assert ModelSettings().to_kwargs() == {}


def test_model_settings_merge_prefers_the_other_settings() -> None:
    base = ModelSettings(temperature=0.2, timeout=10, extra={"a": 1, "b": 1})
    merged = base.merge(ModelSettings(temperature=0.9, extra={"b": 2}))

    assert merged.to_kwargs() == {"temperature": 0.9, "timeout": 10, "a": 1, "b": 2}
    assert base.merge(None) is base


def test_model_settings_validate_ranges() -> None:
    with pytest.raises(ValueError):
        ModelSettings(max_tokens=0)
    with pytest.raises(ValueError):
        ModelSettings(max_retries=-1)


# --------------------------------------------------------------------------- ModelRegistry


def test_builtin_profiles_come_from_settings(fake_init: Any) -> None:
    registry = ModelRegistry(
        _settings(default_model="openai:gpt-a", fast_model="openai:gpt-b", strong_model="x:y")
    )

    assert registry.profiles() == ["default", "fast", "strong"]
    registry.get("fast")

    fake_init.assert_called_once_with("openai:gpt-b", timeout=DEFAULT_REQUEST_TIMEOUT)


def test_get_is_lazy_and_cached(fake_init: Any) -> None:
    registry = ModelRegistry(_settings())
    assert fake_init.call_count == 0

    first = registry.get("default")

    assert registry.get("default") is first
    assert fake_init.call_count == 1


def test_register_accepts_instances_strings_and_factories(fake_init: Any) -> None:
    registry = ModelRegistry(_settings())
    instance = FakeChatModel(["x"])
    calls: list[int] = []

    def factory() -> BaseChatModel:
        calls.append(1)
        return instance

    registry.register("instance", instance)
    registry.register("string", "openai:gpt-z", settings=ModelSettings(temperature=0))
    registry.register("factory", factory)

    assert registry.get("instance") is instance
    registry.get("string")
    fake_init.assert_called_with("openai:gpt-z", temperature=0, timeout=DEFAULT_REQUEST_TIMEOUT)
    assert calls == []
    registry.get("factory")
    registry.get("factory")
    assert calls == [1]
    assert "factory" in registry and "missing" not in registry


def test_register_replaces_and_clears_the_cache(fake_init: Any) -> None:
    registry = ModelRegistry(_settings())
    old = registry.get("default")
    replacement = FakeChatModel(["new"])

    registry.register("default", replacement)

    assert registry.get("default") is replacement is not old


def test_register_rejects_invalid_input() -> None:
    registry = ModelRegistry(_settings())
    with pytest.raises(ValueError):
        registry.register("openai:gpt", "openai:gpt")
    with pytest.raises(ValueError):
        registry.register("", "openai:gpt")
    with pytest.raises(TypeError):
        registry.register("bad", 42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.register("bad", FakeChatModel(["x"]), settings=ModelSettings(temperature=0))


def test_get_unknown_profile_lists_known_ones() -> None:
    with pytest.raises(ModelResolutionError, match="known: default, fast, strong"):
        ModelRegistry(_settings()).get("nope")


def test_get_with_settings_builds_a_fresh_model_from_the_profile_string(fake_init: Any) -> None:
    registry = ModelRegistry(_settings())
    registry.register("tuned", "openai:gpt-t", settings=ModelSettings(temperature=0.1, timeout=5))

    first = registry.get("tuned", settings=ModelSettings(temperature=0.7))

    fake_init.assert_called_with("openai:gpt-t", temperature=0.7, timeout=5)
    assert registry.get("tuned", settings=ModelSettings(temperature=0.7)) is not first
    registry.register("instance", FakeChatModel(["x"]))
    with pytest.raises(ModelResolutionError, match="cannot be applied"):
        registry.get("instance", settings=ModelSettings(temperature=0))


def test_factory_failures_become_model_resolution_errors() -> None:
    registry = ModelRegistry(_settings())

    def broken() -> BaseChatModel:
        raise RuntimeError("no credentials")

    registry.register("broken", broken)
    registry.register("wrong", lambda: "not a model")  # type: ignore[arg-type,return-value]

    with pytest.raises(ModelResolutionError, match="no credentials"):
        registry.get("broken")
    with pytest.raises(ModelResolutionError, match="not a BaseChatModel"):
        registry.get("wrong")


def test_create_wraps_init_errors() -> None:
    with pytest.raises(ModelResolutionError, match="Cannot create chat model"):
        ModelRegistry(_settings()).create("no-such-provider:model")


def test_azure_models_receive_the_configured_connection(fake_init: Any) -> None:
    registry = ModelRegistry(
        _settings(
            azure_deployment="dep",
            azure_api_version="2025-01-01",
            azure_openai_endpoint="https://example.invalid",
            azure_openai_api_key="key",
        )
    )

    registry.create("azure_openai:gpt-5")

    fake_init.assert_called_once_with(
        "azure_openai:gpt-5",
        azure_deployment="dep",
        api_version="2025-01-01",
        azure_endpoint="https://example.invalid",
        api_key="key",
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )


def test_azure_without_configuration_passes_nothing_extra(fake_init: Any) -> None:
    ModelRegistry(_settings()).create("azure_openai:gpt-4.1", ModelSettings(temperature=0.3))

    fake_init.assert_called_once_with(
        "azure_openai:gpt-4.1", temperature=0.3, timeout=DEFAULT_REQUEST_TIMEOUT
    )


def test_non_azure_models_ignore_azure_configuration(fake_init: Any) -> None:
    ModelRegistry(_settings(azure_deployment="dep")).create("anthropic:claude")

    fake_init.assert_called_once_with("anthropic:claude")


def test_default_registry_is_a_process_wide_singleton() -> None:
    assert ModelRegistry.default() is ModelRegistry.default()


# --------------------------------------------------------------------------- resolve_model


def test_resolve_model_returns_instances_as_is() -> None:
    model = FakeChatModel(["x"])

    assert resolve_model(model) is model
    with pytest.raises(ModelResolutionError, match="model instance"):
        resolve_model(model, settings=ModelSettings(temperature=0))


def test_resolve_model_uses_profiles_and_the_default(fake_init: Any) -> None:
    registry = ModelRegistry(_settings())
    fast = FakeChatModel(["fast"])
    default = FakeChatModel(["default"])
    registry.register("fast", fast)
    registry.register("default", default)

    assert resolve_model("fast", registry=registry) is fast
    assert resolve_model(None, registry=registry) is default
    fake_init.assert_not_called()


@pytest.mark.model_requests_allowed
def test_resolve_model_builds_other_strings_with_settings(fake_init: Any) -> None:
    registry = ModelRegistry(_settings())

    resolve_model("openai:gpt-x", settings=ModelSettings(max_tokens=10), registry=registry)

    fake_init.assert_called_once_with(
        "openai:gpt-x", max_tokens=10, timeout=DEFAULT_REQUEST_TIMEOUT
    )


# --------------------------------------------------------------------------- request guard


@pytest.fixture
def blocked() -> Iterator[None]:
    with allow_model_requests(False):
        yield


@pytest.mark.model_requests_allowed
def test_requests_are_allowed_by_default() -> None:
    assert model_requests_allowed()


def test_offline_models_are_recognised() -> None:
    assert is_offline_model(FakeChatModel(["x"]))
    assert is_offline_model(FunctionChatModel(lambda call: "x"))
    assert is_offline_model(GenericFakeChatModel(messages=iter([])))
    assert not is_offline_model(_RealModel(["x"]))


@pytest.mark.usefixtures("blocked")
def test_blocked_requests_reject_real_models_only() -> None:
    assert not model_requests_allowed()
    ensure_model_request_allowed(FakeChatModel(["x"]))
    with pytest.raises(ModelResolutionError, match="blocked"):
        ensure_model_request_allowed(_RealModel(["x"]))
    with pytest.raises(ModelResolutionError, match="blocked"):
        resolve_model(_RealModel(["x"]))


@pytest.mark.usefixtures("blocked")
def test_blocked_requests_refuse_model_strings_before_constructing(fake_init: Any) -> None:
    registry = ModelRegistry(_settings())
    fake = FakeChatModel(["x"])
    registry.register("fake", fake)

    with pytest.raises(ModelResolutionError, match="refusing to create model 'openai:gpt'"):
        resolve_model("openai:gpt", registry=registry)
    with pytest.raises(ModelResolutionError, match="'default'"):
        resolve_model(None, registry=registry)
    assert resolve_model("fake", registry=registry) is fake
    fake_init.assert_not_called()


@pytest.mark.model_requests_allowed
def test_allow_model_requests_restores_the_previous_setting() -> None:
    with allow_model_requests(False):
        with allow_model_requests(True):
            assert model_requests_allowed()
        assert not model_requests_allowed()
    assert model_requests_allowed()


def test_set_allow_model_requests_is_global() -> None:
    try:
        set_allow_model_requests(False)
        assert not model_requests_allowed()
    finally:
        set_allow_model_requests(True)
    assert model_requests_allowed()


def test_default_model_setting_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_DEFAULT_MODEL", "openai:gpt-env")

    assert Settings().default_model == "openai:gpt-env"
    assert _settings().fast_model and _settings().strong_model


@pytest.mark.model_requests_allowed
def test_provider_keys_from_settings_are_passed_explicitly() -> None:
    pytest.importorskip("langchain_anthropic")
    pytest.importorskip("langchain_openai")
    from awa.core.settings import Settings
    from awa.models import ModelRegistry

    registry = ModelRegistry(Settings(anthropic_api_key="sk-ant-test", openai_api_key="sk-oa-test"))
    anthropic_model = registry.create("anthropic:claude-sonnet-5")
    openai_model = registry.create("openai:gpt-5")
    assert anthropic_model.anthropic_api_key.get_secret_value() == "sk-ant-test"  # type: ignore[attr-defined]
    assert openai_model.openai_api_key.get_secret_value() == "sk-oa-test"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("model", "sampling", "effort"),
    [
        ("anthropic:claude-opus-5-5", False, True),
        ("anthropic:claude-sonnet-5-5", False, True),
        ("anthropic:claude-haiku-5-5", False, True),
        ("anthropic:claude-opus-4-7", False, True),
        ("anthropic:claude-fable", False, True),
        ("anthropic:claude-haiku-4-5-20251001", True, False),
        ("anthropic:claude-sonnet-4-20250514", True, False),
        ("anthropic:claude-3-5-sonnet-20241022", True, False),
        ("openai:gpt-5.6-terra", False, True),
        ("azure_openai:o4-mini", False, True),
        ("openai:gpt-4.1", True, False),
        ("ollama:llama3", True, False),
        ("unknown-model", False, False),
    ],
)
def test_model_capabilities(model: str, sampling: bool, effort: bool) -> None:
    caps = model_capabilities(model)
    assert (caps.sampling, caps.effort) == (sampling, effort)


@pytest.mark.parametrize("model", ["anthropic:claude-opus-5-5", "anthropic:claude-haiku-5-5"])
def test_no_sampling_parameter_reaches_a_current_claude_model(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: dict[str, Any] = {}

    def fake_init(name: str, **kwargs: Any) -> object:
        built.update(kwargs)
        return object()

    monkeypatch.setattr("awa.models.registry.init_chat_model", fake_init)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings = ModelSettings(temperature=0.3, effort="low", extra={"top_p": 0.9, "top_k": 5})
    ModelRegistry().create(model, settings)

    assert not {"temperature", "top_p", "top_k"} & set(built)
    assert built["reasoning_effort"] == "low"


def test_effort_maps_to_the_provider_parameter() -> None:
    reasoning: dict[str, object] = {"use_responses_api": True}
    apply_capabilities("azure_openai:gpt-5.6-terra", reasoning, "max")
    assert reasoning["reasoning_effort"] == "high"

    # Chat Completions rejects function tools with a reasoning effort: without the
    # Responses API (opt-in) nothing is sent.
    chat: dict[str, object] = {}
    apply_capabilities("azure_openai:gpt-5.6-terra", chat, "low")
    assert chat == {}

    sampling: dict[str, object] = {}
    apply_capabilities("openai:gpt-4.1", sampling, "low")
    assert sampling == {"temperature": 0}

    explicit: dict[str, object] = {"temperature": 0.7}
    apply_capabilities("openai:gpt-4.1", explicit, "low")
    assert explicit == {"temperature": 0.7}

    unknown: dict[str, object] = {}
    apply_capabilities("mystery", unknown, "low")
    assert unknown == {}  # nothing is forced on an unknown provider


def test_roles_get_an_effort_unless_the_agent_sets_one() -> None:
    bp = Blueprint.from_yaml_text(
        "name: p\nagents:\n"
        "  - {name: w, instructions: Work.}\n"
        "  - {name: j, instructions: Judge., role: judge, judges: w, rubric: [ok]}\n"
        "  - {name: o, instructions: Plan., role: orchestrator}\n"
        "  - {name: t, instructions: Hot., model_settings: {temperature: 0.8}}\n"
    )
    efforts = {
        spec.name: (settings.effort if (settings := role_model_settings(spec, None)) else None)
        for spec in bp.agents
    }
    assert efforts["w"] == "medium" and efforts["j"] == "low" and efforts["o"] is None
    hot = bp.agent("t")
    assert role_model_settings(hot, hot.model_settings) == hot.model_settings


def test_the_responses_api_switch_carries_the_effort_to_reasoning_models() -> None:
    switched: dict[str, object] = {}
    apply_capabilities("azure_openai:gpt-5.6-terra", switched, "low", responses_api=True)
    assert switched == {
        "use_responses_api": True,
        "reasoning_effort": "low",
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }

    # The model's own choice wins, and without an effort the API is not switched.
    own: dict[str, object] = {"use_responses_api": False}
    apply_capabilities("azure_openai:gpt-5.6-terra", own, "low", responses_api=True)
    assert own == {"use_responses_api": False}
    plain: dict[str, object] = {}
    apply_capabilities("openai:gpt-5.6", plain, None, responses_api=True)
    assert plain == {}
    # A model that takes a temperature is not touched by the switch.
    older: dict[str, object] = {}
    apply_capabilities("openai:gpt-4.1", older, "low", responses_api=True)
    assert older == {"temperature": 0}


def test_the_responses_api_switch_keeps_no_response_data_on_the_provider() -> None:
    # The Responses API stores every response unless `store=False`; the reasoning travels
    # encrypted in the conversation instead, so multi-turn reasoning works statelessly.
    switched: dict[str, object] = {"include": ["file_search_call.results"]}
    apply_capabilities("openai:gpt-5.6", switched, "high", responses_api=True)
    assert switched["store"] is False
    assert switched["include"] == ["file_search_call.results", "reasoning.encrypted_content"]

    # An explicit `store`, or server-side conversation state, is left alone.
    stored: dict[str, object] = {"store": True}
    apply_capabilities("openai:gpt-5.6", stored, "high", responses_api=True)
    assert stored["store"] is True and "include" not in stored
    chained: dict[str, object] = {"use_previous_response_id": True}
    apply_capabilities("openai:gpt-5.6", chained, "high", responses_api=True)
    assert "store" not in chained and "include" not in chained


def test_the_built_responses_model_does_not_store_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awa.core.settings import Settings

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    registry = ModelRegistry(Settings(openai_responses_api=True))
    model: Any = registry.create("openai:gpt-5.6", ModelSettings(effort="low"))

    assert model.use_responses_api is True
    assert model.store is False
    assert model.include == ["reasoning.encrypted_content"]
    assert model.reasoning_effort == "low"


def test_key_variables_come_from_the_provider_table() -> None:
    assert {p.name: p.key_variable for p in PROVIDERS if p.key_variable} == KEY_VARIABLES
    assert KEY_VARIABLES["google_genai"] == "GOOGLE_API_KEY"


@pytest.mark.parametrize("provider", sorted(KEY_VARIABLES))
def test_every_keyed_provider_names_its_variable(provider: str) -> None:
    assert KEY_VARIABLES[provider] in missing_credentials_message(provider)


def test_an_unknown_provider_gets_the_generic_message() -> None:
    assert "run awa setup" in missing_credentials_message("cohere")
    assert "run awa setup" in missing_credentials_message(None)


def test_the_provider_is_read_from_the_prefix_or_a_bare_model_name() -> None:
    assert provider_of("claude-sonnet-5-5") == "anthropic"
    assert provider_of("gpt-6-luna") == "openai"
    assert provider_of("openai:gpt-6.1-sol") == "openai"
    assert provider_of("azure:my-deployment") == "azure_openai"
    assert provider_of("gemini-3.8-flash") == "google_genai"
    assert provider_of("mystery") is None
