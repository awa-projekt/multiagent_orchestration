"""Azure OpenAI: the v1 route, the dated route, Entra ID, and the hints for typical failures.

The real model classes (`ChatOpenAI`, `AzureChatOpenAI`) talk to `FakeAoai`, a localhost
HTTP server with the shapes and failures of an Azure OpenAI resource; nothing is patched
below the model.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from _helpers.fake_aoai import ACCEPTED_SCOPES, OPENAI_USER_ROLE, FakeAoai
from awa.core.settings import Settings
from awa.errors import ModelProviderError, ModelRateLimitError
from awa.models import (
    ModelRegistry,
    ModelSettings,
    classify_model_error,
    model_identity,
    resolve_model,
)
from awa.models._azure import EntraTokenProvider, v1_base_url
from awa.models.errors import model_provider

pytest.importorskip("langchain_openai")

pytestmark = pytest.mark.model_requests_allowed

DEPLOYMENT = "my-deployment"
NO_RETRIES = ModelSettings(max_retries=0)


@pytest.fixture
def aoai() -> Iterator[FakeAoai]:
    with FakeAoai(
        deployments=[DEPLOYMENT, "reasoning-deployment"],
        reasoning_deployments=["reasoning-deployment"],
    ) as fake:
        yield fake


def _settings(fake: FakeAoai, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "azure_openai_endpoint": fake.url,
        "azure_openai_api_key": "fake-key",
        "azure_deployment": DEPLOYMENT,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _entra(fake: FakeAoai, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "azure_openai_api_key": None,
        "azure_auth": "entra",
        "azure_tenant_id": fake.tenant,
        "azure_client_id": fake.client_id,
        "azure_client_secret": fake.client_secret,
        "azure_authority_host": fake.url,
    }
    return _settings(fake, **{**values, **overrides})


def _ask(settings: Settings, model_settings: ModelSettings | None = NO_RETRIES) -> str:
    registry = ModelRegistry(settings)
    return (
        resolve_model("azure_openai:model-label", settings=model_settings, registry=registry)
        .invoke("hi")
        .text
    )


def _failure(
    settings: Settings, model_settings: ModelSettings | None = NO_RETRIES
) -> ModelProviderError:
    registry = ModelRegistry(settings)
    model = resolve_model("azure_openai:model-label", settings=model_settings, registry=registry)
    with pytest.raises(Exception) as caught:
        model.invoke("hi")
    error = classify_model_error(
        caught.value, model="azure_openai:model-label", provider=model_provider(model)
    )
    assert error is not None
    return error


# ------------------------------------------------------------------ the v1 route


def test_without_an_api_version_the_v1_route_is_used(aoai: FakeAoai) -> None:
    assert _ask(_settings(aoai)) == f"hello from {DEPLOYMENT}"

    request = aoai.model_requests()[0]
    assert request.path == "/openai/v1/chat/completions"
    assert "api-version" not in request.query
    assert request.headers["authorization"] == "Bearer fake-key"
    assert request.body["model"] == DEPLOYMENT


def test_the_v1_model_is_identified_as_an_azure_model(aoai: FakeAoai) -> None:
    model = resolve_model("azure_openai:model-label", registry=ModelRegistry(_settings(aoai)))

    assert model_identity(model) == f"azure_openai:{DEPLOYMENT}"
    assert model_provider(model) == "azure_openai"


def test_the_v1_model_gets_loop_local_http_clients(aoai: FakeAoai) -> None:
    from awa.models import loop_local_openai_client

    model = ModelRegistry(_settings(aoai)).create("azure_openai:model-label")

    assert type(model.http_async_client) is type(loop_local_openai_client())  # type: ignore[attr-defined]
    assert model.http_client is not None  # type: ignore[attr-defined]


def test_the_v1_route_works_asynchronously_and_across_event_loops(aoai: FakeAoai) -> None:
    model = resolve_model("azure_openai:model-label", registry=ModelRegistry(_settings(aoai)))

    async def ask() -> str:
        return (await model.ainvoke("hi")).text

    assert [asyncio.run(ask()) for _ in range(3)] == [f"hello from {DEPLOYMENT}"] * 3


def test_the_deployment_defaults_to_the_name_in_the_model_string(aoai: FakeAoai) -> None:
    settings = _settings(aoai, azure_deployment=None)
    aoai.deployments.add("model-label")

    assert _ask(settings) == "hello from model-label"
    assert aoai.model_requests()[0].body["model"] == "model-label"


def test_the_responses_api_of_the_v1_route(aoai: FakeAoai) -> None:
    answer = _ask(_settings(aoai), ModelSettings(max_retries=0, extra={"use_responses_api": True}))

    assert answer == f"hello from {DEPLOYMENT}"
    assert aoai.model_requests()[0].path == "/openai/v1/responses"


def test_a_reasoning_deployment_gets_max_completion_tokens(aoai: FakeAoai) -> None:
    settings = _settings(aoai, azure_deployment="reasoning-deployment")

    assert (
        _ask(settings, ModelSettings(max_retries=0, max_tokens=50))
        == "hello from reasoning-deployment"
    )
    body = aoai.model_requests()[0].body
    assert body["max_completion_tokens"] == 50 and "max_tokens" not in body


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://res.openai.azure.com", "https://res.openai.azure.com/openai/v1/"),
        ("https://res.openai.azure.com/", "https://res.openai.azure.com/openai/v1/"),
        ("https://res.openai.azure.com/openai", "https://res.openai.azure.com/openai/v1/"),
        ("https://res.openai.azure.com/openai/v1/", "https://res.openai.azure.com/openai/v1/"),
        ("  https://res.services.ai.azure.com  ", "https://res.services.ai.azure.com/openai/v1/"),
    ],
)
def test_the_v1_base_url_from_any_spelling_of_the_endpoint(endpoint: str, expected: str) -> None:
    assert v1_base_url(endpoint) == expected


def test_the_v1_route_without_a_key_fails_before_any_request(
    aoai: FakeAoai, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An OpenAI key in the environment must never be sent to Azure.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-be-sent")
    registry = ModelRegistry(_settings(aoai, azure_openai_api_key=None))

    with pytest.raises(ModelProviderError, match="AZURE_OPENAI_API_KEY") as caught:
        registry.create("azure_openai:model-label")

    assert caught.value.kind == "authentication"
    assert aoai.requests == []


# --------------------------------------------------------- the dated route (unchanged)


def test_with_an_api_version_the_dated_route_is_used(aoai: FakeAoai) -> None:
    assert _ask(_settings(aoai, azure_api_version="test-api-version")) == f"hello from {DEPLOYMENT}"

    request = aoai.model_requests()[0]
    assert request.path == f"/openai/deployments/{DEPLOYMENT}/chat/completions"
    assert request.query == {"api-version": "test-api-version"}
    assert request.headers["api-key"] == "fake-key"
    assert "authorization" not in request.headers


def test_the_dated_model_is_an_azure_chat_model(aoai: FakeAoai) -> None:
    model = resolve_model(
        "azure_openai:model-label",
        registry=ModelRegistry(_settings(aoai, azure_api_version="test-api-version")),
    )

    assert model_identity(model) == "azure_openai:model-label"


def test_a_reasoning_deployment_works_on_the_dated_route_too(aoai: FakeAoai) -> None:
    settings = _settings(
        aoai, azure_deployment="reasoning-deployment", azure_api_version="test-api-version"
    )

    assert (
        _ask(settings, ModelSettings(max_retries=0, max_tokens=50))
        == "hello from reasoning-deployment"
    )


def test_the_classic_route_refuses_a_request_without_api_version(aoai: FakeAoai) -> None:
    url = f"{aoai.url}/openai/deployments/{DEPLOYMENT}/chat/completions"
    request = urllib.request.Request(
        url, data=b"{}", headers={"api-key": "fake-key", "content-type": "application/json"}
    )

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)  # noqa: S310 - a localhost URL

    assert caught.value.code == 404


def test_a_streamed_responses_request_is_a_clear_400(aoai: FakeAoai) -> None:
    body = json.dumps({"model": DEPLOYMENT, "input": "hi", "stream": True}).encode()
    request = urllib.request.Request(
        f"{aoai.url}/openai/v1/responses",
        data=body,
        headers={"api-key": "fake-key", "content-type": "application/json"},
    )

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)  # noqa: S310 - a localhost URL

    assert caught.value.code == 400
    assert "chat completions only" in caught.value.read().decode()


# ----------------------------------------------------------------------- Entra ID


def test_entra_signs_in_with_a_service_principal_on_the_v1_route(aoai: FakeAoai) -> None:
    settings = _entra(aoai)

    assert _ask(settings) == f"hello from {DEPLOYMENT}"
    assert _ask(settings) == f"hello from {DEPLOYMENT}"

    assert [r.headers["authorization"] for r in aoai.model_requests()] == [
        "Bearer entra-token-1"
    ] * 2
    (sign_in,) = aoai.token_requests()  # the second call reuses the cached token
    assert sign_in.body["grant_type"] == "client_credentials"
    assert sign_in.body["scope"] == ACCEPTED_SCOPES[0]
    assert "api-key" not in aoai.model_requests()[0].headers


def test_entra_signs_in_on_the_dated_route(aoai: FakeAoai) -> None:
    assert _ask(_entra(aoai, azure_api_version="test-api-version")) == f"hello from {DEPLOYMENT}"

    request = aoai.model_requests()[0]
    assert request.path == f"/openai/deployments/{DEPLOYMENT}/chat/completions"
    assert request.headers["authorization"] == "Bearer entra-token-1"
    assert "api-key" not in request.headers


def test_entra_works_asynchronously(aoai: FakeAoai) -> None:
    model = resolve_model("azure_openai:model-label", registry=ModelRegistry(_entra(aoai)))

    async def ask() -> str:
        return (await model.ainvoke("hi")).text

    assert asyncio.run(ask()) == f"hello from {DEPLOYMENT}"
    assert len(aoai.token_requests()) == 1


def test_entra_makes_a_resource_that_refuses_keys_usable(aoai: FakeAoai) -> None:
    aoai.key_auth_enabled = False

    assert _ask(_entra(aoai)) == f"hello from {DEPLOYMENT}"


def test_entra_falls_back_to_the_older_scope_and_remembers_it() -> None:
    with FakeAoai(accepted_scopes=[ACCEPTED_SCOPES[1]], deployments=[DEPLOYMENT]) as fake:
        provider = EntraTokenProvider(
            fake.tenant, fake.client_id, fake.client_secret, authority=fake.url
        )

        provider()

        assert [r.body["scope"] for r in fake.token_requests()] == list(ACCEPTED_SCOPES)
        provider._expires = 0.0
        provider()
        assert [r.body["scope"] for r in fake.token_requests()][2:] == [ACCEPTED_SCOPES[1]]


def test_entra_with_a_wrong_secret_says_what_to_fix_and_hides_the_secret(aoai: FakeAoai) -> None:
    settings = _entra(aoai, azure_client_secret="not-the-secret-value")

    with pytest.raises(ModelProviderError) as caught:
        _ask(settings)

    message = str(caught.value)
    assert "AZURE_CLIENT_SECRET" in message and "not-the-secret-value" not in message
    assert caught.value.kind == "authentication" and caught.value.retryable is False
    assert aoai.model_requests() == []


def test_entra_with_a_wrong_client_id_names_the_variable(aoai: FakeAoai) -> None:
    with pytest.raises(ModelProviderError, match="AZURE_CLIENT_ID"):
        _ask(_entra(aoai, azure_client_id="someone-else"))


def test_entra_needs_tenant_client_and_secret(aoai: FakeAoai) -> None:
    settings = _entra(aoai, azure_tenant_id=None, azure_client_secret=None)

    with pytest.raises(ModelProviderError) as caught:
        ModelRegistry(settings).create("azure_openai:model-label")

    assert "AZURE_TENANT_ID" in str(caught.value) and "AZURE_CLIENT_SECRET" in str(caught.value)
    assert aoai.requests == []


def test_an_unreachable_authority_is_a_retryable_connection_error() -> None:
    provider = EntraTokenProvider("t", "c", "s", authority="http://127.0.0.1:9")

    with pytest.raises(ModelProviderError) as caught:
        provider()

    assert caught.value.kind == "connection" and caught.value.retryable is True


# ------------------------------------------------------ the token cache and its clock


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_token_is_cached_until_shortly_before_it_expires(aoai: FakeAoai) -> None:
    clock = _Clock()
    provider = EntraTokenProvider(
        aoai.tenant, aoai.client_id, aoai.client_secret, authority=aoai.url, clock=clock
    )

    first = provider()
    clock.now += 3299  # one hour lifetime, renewed five minutes before the end
    assert provider() == first
    assert len(aoai.token_requests()) == 1

    clock.now += 2
    second = provider()
    assert second != first and len(aoai.token_requests()) == 2
    assert provider() == second


def test_a_short_lived_token_is_renewed_at_half_time() -> None:
    clock = _Clock()
    with FakeAoai(token_lifetime=200) as fake:
        provider = EntraTokenProvider(
            fake.tenant, fake.client_id, fake.client_secret, authority=fake.url, clock=clock
        )

        provider()
        clock.now += 99
        provider()
        assert len(fake.token_requests()) == 1
        clock.now += 2
        provider()
        assert len(fake.token_requests()) == 2


def test_concurrent_callers_share_one_sign_in(aoai: FakeAoai) -> None:
    provider = EntraTokenProvider(
        aoai.tenant, aoai.client_id, aoai.client_secret, authority=aoai.url
    )
    tokens: list[str] = []
    threads = [threading.Thread(target=lambda: tokens.append(provider())) for _ in range(8)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(tokens)) == 1 and len(aoai.token_requests()) == 1


async def test_the_async_form_returns_the_same_cached_token(aoai: FakeAoai) -> None:
    provider = EntraTokenProvider(
        aoai.tenant, aoai.client_id, aoai.client_secret, authority=aoai.url
    )

    assert await provider.aget() == provider()
    assert len(aoai.token_requests()) == 1


# ------------------------------------------------------------ hints for the failures


def test_a_wrong_key_is_a_401_with_a_hint(aoai: FakeAoai) -> None:
    error = _failure(_settings(aoai, azure_openai_api_key="wrong-key"))

    assert (error.kind, error.status, error.retryable) == ("authentication", 401, False)
    assert "AZURE_OPENAI_API_KEY" in str(error) and "wrong-key" not in str(error)


def test_key_authentication_disabled_points_to_entra(aoai: FakeAoai) -> None:
    aoai.key_auth_enabled = False

    error = _failure(_settings(aoai))

    assert (error.kind, error.status) == ("permission_denied", 403)
    assert "AZURE_OPENAI_AUTH=entra" in str(error)


def test_key_authentication_disabled_is_explained_on_the_dated_route_too(aoai: FakeAoai) -> None:
    aoai.key_auth_enabled = False

    error = _failure(_settings(aoai, azure_api_version="test-api-version"))

    assert "AZURE_OPENAI_AUTH=entra" in str(error)


def test_a_missing_role_is_a_403_that_names_the_role(aoai: FakeAoai) -> None:
    aoai.client_roles = set()

    error = _failure(_entra(aoai))

    assert error.status == 403 and OPENAI_USER_ROLE in str(error)
    assert "AZURE_OPENAI_AUTH=entra" not in str(error)


def test_an_unknown_deployment_says_deployment_name_not_model_name(aoai: FakeAoai) -> None:
    error = _failure(_settings(aoai, azure_deployment="model-label"))

    assert (error.kind, error.status) == ("not_found", 404)
    assert "not the model name" in str(error)


def test_the_unknown_deployment_hint_is_the_same_on_the_dated_route(aoai: FakeAoai) -> None:
    error = _failure(
        _settings(aoai, azure_deployment="model-label", azure_api_version="test-api-version")
    )

    assert "not the model name" in str(error)


def test_throttling_is_a_retryable_rate_limit_with_the_wait_time_and_a_hint(aoai: FakeAoai) -> None:
    aoai.throttle(1, retry_after=7)

    error = _failure(_settings(aoai))

    assert isinstance(error, ModelRateLimitError)
    assert (error.status, error.retry_after, error.retryable) == (429, 7.0, True)
    assert "quota" in str(error)


def test_an_expired_token_is_a_401(aoai: FakeAoai) -> None:
    model = resolve_model(
        "azure_openai:model-label", settings=NO_RETRIES, registry=ModelRegistry(_entra(aoai))
    )
    model.invoke("hi")
    aoai.expire_tokens()

    with pytest.raises(Exception) as caught:
        model.invoke("hi")

    error = classify_model_error(caught.value, provider=model_provider(model))
    assert error is not None and error.status == 401 and "Entra" in str(error)


def test_errors_of_other_providers_get_no_azure_hint() -> None:
    class OpenAiStyleError(Exception):
        status_code = 404
        body = {"code": "model_not_found", "message": "The model model-z does not exist"}

    error = classify_model_error(OpenAiStyleError("not found"), provider="openai")

    assert error is not None and "Hint" not in str(error)


# ------------------------------------- behavior follows the answers, not the names
#
# Deployment names are free text. These deployments are named like well-known models but
# behave differently from what such a name suggests, or have names that mean nothing.


@pytest.fixture
def odd() -> Iterator[FakeAoai]:
    with FakeAoai(
        deployments=["gpt-lookalike", "o3-lookalike", "x1", "plain-gpt", "both-refused"],
        rejected_params={
            "gpt-lookalike": ["temperature"],
            "o3-lookalike": ["max_completion_tokens"],
            "both-refused": ["max_tokens", "max_completion_tokens", "temperature"],
        },
        reasoning_deployments=["x1"],
        reported_models={"x1": "model-reported-by-x1"},
    ) as fake:
        yield fake


def _body_keys(fake: FakeAoai) -> list[set[str]]:
    return [set(r.body) for r in fake.model_requests()]


def _invoke(fake: FakeAoai, deployment: str, **model_settings: Any) -> Any:
    settings = _settings(fake, azure_deployment=deployment)
    registry = ModelRegistry(settings)
    return resolve_model(
        "azure_openai:label",
        settings=ModelSettings(max_retries=0, **model_settings),
        registry=registry,
    )


def test_a_refused_temperature_is_dropped_and_remembered(odd: FakeAoai) -> None:
    model = _invoke(odd, "gpt-lookalike", temperature=0.2)

    assert model.invoke("hi").text == "hello from gpt-lookalike"
    assert model.invoke("hi").text == "hello from gpt-lookalike"

    first, retry, later = _body_keys(odd)
    assert "temperature" in first and "temperature" not in retry and "temperature" not in later


def test_a_refused_token_limit_is_sent_under_its_other_name(odd: FakeAoai) -> None:
    model = _invoke(odd, "o3-lookalike", max_tokens=50)

    assert model.invoke("hi").text == "hello from o3-lookalike"
    assert model.invoke("hi").text == "hello from o3-lookalike"

    bodies = [r.body for r in odd.model_requests()]
    assert "max_completion_tokens" in bodies[0] and bodies[1].get("max_tokens") == 50
    assert "max_completion_tokens" not in bodies[1] and bodies[2].get("max_tokens") == 50
    assert len(bodies) == 3


def test_a_parameter_refused_in_every_spelling_is_dropped(odd: FakeAoai) -> None:
    model = _invoke(odd, "both-refused", temperature=0.2, max_tokens=50)

    assert model.invoke("hi").text == "hello from both-refused"

    last = _body_keys(odd)[-1]
    assert not last & {"temperature", "max_tokens", "max_completion_tokens"}


def test_a_model_named_like_a_reasoning_model_is_not_treated_as_one(odd: FakeAoai) -> None:
    model = _invoke(odd, "plain-gpt", temperature=0.2, max_tokens=50)

    assert model.invoke("hi").text == "hello from plain-gpt"
    assert len(odd.model_requests()) == 1  # nothing was refused, so nothing was changed
    assert odd.model_requests()[0].body["temperature"] == 0.2


def test_a_meaningless_name_works_whatever_the_deployment_accepts(odd: FakeAoai) -> None:
    model = _invoke(odd, "x1", max_tokens=50)

    assert model.invoke("hi").text == "hello from x1"
    assert len(odd.model_requests()) == 1
    assert odd.model_requests()[0].body["max_completion_tokens"] == 50


def test_the_adjustment_works_asynchronously_and_when_streaming(odd: FakeAoai) -> None:
    model = _invoke(odd, "gpt-lookalike", temperature=0.2)

    async def run() -> tuple[str, str]:
        answer = (await model.ainvoke("hi")).text
        streamed = "".join([c.text async for c in model.astream("hi")])
        return answer, streamed

    assert asyncio.run(run()) == ("hello from gpt-lookalike", "hello from gpt-lookalike")
    assert "".join(c.text for c in model.stream("hi")) == "hello from gpt-lookalike"

    # one refusal teaches the model; the streams after it are not refused again
    assert [("temperature" in keys) for keys in _body_keys(odd)] == [True, False, False, False]


def test_streaming_learns_from_a_refusal_before_the_first_chunk(odd: FakeAoai) -> None:
    model = _invoke(odd, "gpt-lookalike", temperature=0.2)

    assert "".join(c.text for c in model.stream("hi")) == "hello from gpt-lookalike"

    assert [("temperature" in keys) for keys in _body_keys(odd)] == [True, False]


def test_a_refusal_of_something_else_is_not_retried(odd: FakeAoai) -> None:
    odd.deployments.discard("plain-gpt")
    model = _invoke(odd, "plain-gpt")

    with pytest.raises(Exception, match="DeploymentNotFound|does not exist"):
        model.invoke("hi")

    assert len(odd.model_requests()) == 1


def test_the_answer_reports_the_model_the_service_ran(odd: FakeAoai) -> None:
    reply = _invoke(odd, "x1").invoke("hi")

    assert reply.response_metadata["model_name"] == "model-reported-by-x1"


def test_the_unsupported_parameter_is_read_from_the_error_body() -> None:
    from awa.models._azure import unsupported_parameter

    class Refused(Exception):
        status_code = 400

        def __init__(self, body: dict[str, Any]) -> None:
            super().__init__("Error code: 400")
            self.body = body

    named = Refused({"code": "unsupported_parameter", "param": "temperature", "message": "no"})
    quoted = Refused(
        {"code": "unsupported_value", "message": "Unsupported value: 'top_p' does not."}
    )
    other = Refused({"code": "content_filter", "param": "messages", "message": "blocked"})
    wrapped = Refused({"error": {"code": "unsupported_parameter", "param": "seed"}})

    assert unsupported_parameter(named) == "temperature"
    assert unsupported_parameter(quoted) == "top_p"
    assert unsupported_parameter(other) is None
    assert unsupported_parameter(wrapped) == "seed"
    assert unsupported_parameter(ValueError("x")) is None


def test_an_azure_error_body_is_recognised_even_without_a_provider_label() -> None:
    class SdkError(Exception):
        status_code = 404
        code = "DeploymentNotFound"
        body = {"error": {"code": "DeploymentNotFound", "message": "no such deployment"}}

    error = classify_model_error(SdkError("Error code: 404"))

    assert error is not None and "not the model name" in str(error)
