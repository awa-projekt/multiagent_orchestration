"""A missing or invalid model key fails fast with one clear line, and provider SDK exceptions become `ModelProviderError`s."""

from __future__ import annotations

import asyncio
import io
import sys
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core import exceptions as lc_exceptions

from _helpers.fake_secrets import fake_secret
from awa.agents import Agent
from awa.cli import main
from awa.core.settings import Settings
from awa.errors import (
    AwaError,
    ModelProviderError,
    ModelRateLimitError,
    ToolTimeoutError,
    UsageLimitExceededError,
)
from awa.models import (
    ModelRegistry,
    ModelResilienceMiddleware,
    ModelRetryPolicy,
    allow_model_requests,
    model_identity,
)
from awa.models.errors import classify_model_error
from awa.models.registry import _has_no_credentials
from awa.server.errors import describe_error
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall

ANTHROPIC_TEXT = (
    "Anthropic authentication failed: no API key or authorization credentials were provided. "
    "Set the ANTHROPIC_API_KEY environment variable, pass api_key=... to ChatAnthropic"
)
SDK_TEXT = (
    '"Could not resolve authentication method. Expected either api_key or admin_api_key to '
    'be set. Or for one of the `Authorization` headers to be explicitly omitted"'
)
OPENAI_TEXT = (
    "The api_key client option must be set either by passing api_key to the client or by "
    "setting the OPENAI_API_KEY environment variable"
)
HINT = "(set {} in .env or run awa setup; see awa doctor)"


@pytest.fixture(autouse=True)
def _no_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awa.models.registry._has_no_credentials", _has_no_credentials)
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("error", "provider", "variable"),
    [
        (TypeError(ANTHROPIC_TEXT), "anthropic", "ANTHROPIC_API_KEY"),
        (TypeError(ANTHROPIC_TEXT), None, "ANTHROPIC_API_KEY"),
        (TypeError(SDK_TEXT), "openai", "OPENAI_API_KEY"),
        (TypeError(SDK_TEXT), "anthropic", "ANTHROPIC_API_KEY"),
        (ValueError(OPENAI_TEXT), "openai", "OPENAI_API_KEY"),
    ],
)
def test_missing_credentials_are_a_non_retryable_authentication_error(
    error: Exception, provider: str | None, variable: str
) -> None:
    classified = classify_model_error(error, provider=provider)

    assert classified is not None
    assert classified.kind == "authentication" and classified.retryable is False
    name = provider or "anthropic"
    assert str(classified) == f"no API key for {name} {HINT.format(variable)}"


def test_the_error_inside_a_wrapper_is_found() -> None:
    try:
        try:
            raise TypeError(SDK_TEXT)
        except TypeError as inner:
            raise RuntimeError("model node failed") from inner
    except RuntimeError as outer:
        classified = classify_model_error(outer, provider="openai")
    assert classified is not None and classified.kind == "authentication"


def test_an_unknown_provider_still_gets_a_clear_line() -> None:
    classified = classify_model_error(TypeError(SDK_TEXT))
    assert classified is not None
    assert str(classified) == "no API key for the model provider (run awa setup; see awa doctor)"


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_keys_are_not_retried(status: int) -> None:
    class Rejected(Exception):
        status_code = status

    classified = classify_model_error(Rejected("invalid x-api-key"), provider="anthropic")
    assert classified is not None and classified.retryable is False
    assert classified.kind == ("authentication" if status == 401 else "permission_denied")


async def test_a_missing_key_is_not_retried_or_waited_for() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def respond(call: ModelCall) -> Any:
        raise TypeError(ANTHROPIC_TEXT)

    model = FunctionChatModel(respond, model_name="primary")
    agent = Agent(
        "assistant",
        model=model,
        middleware=[ModelResilienceMiddleware(retry=ModelRetryPolicy(max_attempts=5), sleep=sleep)],
    )

    with pytest.raises(ModelProviderError) as caught:
        await agent.run("hi")

    assert caught.value.kind == "authentication"
    assert delays == []
    # The server maps it to a clean 502 (not a 500 `awa.internal`), the message stays hidden.
    info = describe_error(caught.value)
    assert (info.status, info.internal, info.code) == (502, True, "awa.model_provider")
    assert info.retryable is False


def registry(**settings: Any) -> ModelRegistry:
    return ModelRegistry(Settings(_env_file=None, **settings))


@pytest.mark.parametrize(
    ("model", "variable"),
    [("anthropic:claude-x", "ANTHROPIC_API_KEY"), ("openai:gpt-x", "OPENAI_API_KEY")],
)
def test_creating_a_model_without_a_key_fails_before_the_provider_is_loaded(
    monkeypatch: pytest.MonkeyPatch, model: str, variable: str
) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the provider package must not be loaded")

    monkeypatch.setattr("awa.models.registry.init_chat_model", never)

    with pytest.raises(ModelProviderError) as caught:
        registry().create(model)

    assert caught.value.kind == "authentication"
    provider = model.partition(":")[0]
    assert str(caught.value) == f"no API key for {provider} {HINT.format(variable)}"


@pytest.mark.parametrize(
    "setting", [{"api_key": "k"}, {"base_url": "http://gateway.test"}, {"auth_token": "t"}]
)
def test_models_with_their_own_authentication_are_not_blocked(
    monkeypatch: pytest.MonkeyPatch, setting: dict[str, Any]
) -> None:
    from awa.models import ModelSettings
    from awa.testing import FakeChatModel

    built: list[str] = []
    monkeypatch.setattr(
        "awa.models.registry.init_chat_model",
        lambda model, **kwargs: built.append(model) or FakeChatModel(["x"]),
    )

    registry().create("anthropic:claude-x", ModelSettings(extra=setting))

    assert built == ["anthropic:claude-x"]


def test_a_key_in_the_environment_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    from awa.testing import FakeChatModel

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-0123456789")
    monkeypatch.setattr(
        "awa.models.registry.init_chat_model", lambda model, **kwargs: FakeChatModel(["x"])
    )
    registry().create("anthropic:claude-x")


BLUEPRINT = """
name: solo
agents:
  - name: clerk
    description: Answers.
    model: anthropic:claude-x
"""


@pytest.mark.parametrize("command", ["run", "chat", "eval"])
def test_cli_prints_one_line_without_a_key(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(ModelRegistry, "_default", registry())
    (tmp_path / "solo.yaml").write_text(BLUEPRINT, encoding="utf-8")
    (tmp_path / "cases.yaml").write_text(
        "name: cases\ncases:\n  - id: a\n    input: hi\n  - id: b\n    input: hello\n",
        encoding="utf-8",
    )
    arguments = {
        "run": ["run", "solo.yaml", "hi"],
        "chat": ["chat", "solo.yaml"],
        "eval": ["eval", "solo.yaml", "cases.yaml"],
    }[command]
    monkeypatch.setattr(sys, "stdin", io.StringIO("hi\n/exit\n"))

    with allow_model_requests(True):
        code = main(arguments)

    err = capsys.readouterr().err
    # The pre-flight stops the command before it builds anything: one line, exit code 78.
    assert code == 78
    assert err.strip().splitlines() == [
        "problem: no credentials for Anthropic (model anthropic:claude-x) -- fix: set "
        "ANTHROPIC_API_KEY in .env, or run awa setup"
    ]


class _Response:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class ProviderStatusError(Exception):
    """Shaped like a provider SDK's status error: `status_code` plus a response."""

    def __init__(self, status: int, message: str = "boom", **headers: str) -> None:
        super().__init__(message)
        self.status_code = status
        self.response = _Response(status, {k.replace("_", "-"): v for k, v in headers.items()})


def _httpx_status_error(
    status: int, headers: dict[str, str] | None = None
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.test/v1/chat")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("provider said no", request=request, response=response)


def test_rate_limit_becomes_a_retryable_rate_limit_error_with_retry_after() -> None:
    error = classify_model_error(ProviderStatusError(429, retry_after="2"), model="primary")

    assert isinstance(error, ModelRateLimitError)
    assert error.kind == "rate_limit"
    assert error.retryable is True
    assert error.status == 429
    assert error.retry_after == 2.0
    assert error.model == "primary"
    assert error.code == "awa.model_rate_limit"
    assert error.details == {
        "kind": "rate_limit",
        "model": "primary",
        "status": 429,
        "retry_after": 2.0,
    }


def test_retry_after_as_http_date_and_in_milliseconds() -> None:
    moment = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp()
    dated = ProviderStatusError(429, retry_after=format_datetime(moment, usegmt=True))
    in_ms = ProviderStatusError(429, retry_after_ms="1500", retry_after="9")

    assert classify_model_error(dated, now=lambda: now).retry_after == 30.0  # type: ignore[union-attr]
    assert classify_model_error(in_ms).retry_after == 1.5  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (500, "server_error", True),
        (503, "server_error", True),
        (529, "server_error", True),
        (408, "timeout", True),
        (400, "invalid_request", False),
        (401, "authentication", False),
        (403, "permission_denied", False),
        (404, "not_found", False),
        (418, "error", False),
    ],
)
def test_http_status_codes_map_to_kinds(status: int, kind: str, retryable: bool) -> None:
    error = classify_model_error(ProviderStatusError(status))

    assert type(error) is ModelProviderError
    assert (error.kind, error.retryable, error.status) == (kind, retryable, status)


def test_httpx_errors_are_recognised_without_provider_sdks() -> None:
    request = httpx.Request("POST", "https://api.example.test")
    status_error = classify_model_error(_httpx_status_error(429, {"Retry-After": "3"}))
    timeout = classify_model_error(httpx.ReadTimeout("slow", request=request))
    connect = classify_model_error(httpx.ConnectError("refused", request=request))

    assert isinstance(status_error, ModelRateLimitError)
    assert status_error.retry_after == 3.0
    assert timeout is not None and timeout.kind == "timeout" and timeout.retryable
    assert connect is not None and connect.kind == "connection" and connect.retryable


def test_builtin_timeout_and_connection_errors_are_transient() -> None:
    assert classify_model_error(TimeoutError()).kind == "timeout"  # type: ignore[union-attr]
    assert classify_model_error(ConnectionResetError()).kind == "connection"  # type: ignore[union-attr]


def test_langchain_model_error_taxonomy_is_used() -> None:
    rate = classify_model_error(lc_exceptions.ModelRateLimitError("slow down"))
    overflow = classify_model_error(lc_exceptions.ContextOverflowError("too long"))

    assert isinstance(rate, ModelRateLimitError)
    assert overflow is not None and overflow.kind == "context_overflow" and not overflow.retryable


def test_openai_sdk_errors_name_the_provider() -> None:
    openai = pytest.importorskip("openai")
    http = pytest.importorskip("httpx2")
    request = http.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = http.Response(429, headers={"retry-after-ms": "250"}, request=request)
    sdk_error = openai.RateLimitError("Rate limit reached", response=response, body=None)

    error = classify_model_error(sdk_error, model="openai:gpt-5-mini")

    assert isinstance(error, ModelRateLimitError)
    assert error.provider == "openai"
    assert error.retry_after == 0.25
    assert "openai, HTTP 429" in str(error)


def test_library_decisions_and_programming_errors_are_not_provider_errors() -> None:
    for exc in (
        UsageLimitExceededError("limit"),
        ToolTimeoutError("tool timed out"),
        AwaError("anything"),
        ValueError("bug"),
        RuntimeError("unknown"),
    ):
        assert classify_model_error(exc) is None
    assert classify_model_error(asyncio.CancelledError()) is None


def test_an_existing_provider_error_is_returned_unchanged() -> None:
    error = ModelProviderError("down", kind="server_error", retryable=True)

    assert classify_model_error(error) is error


def test_messages_never_carry_credentials() -> None:
    exc = ProviderStatusError(
        401,
        f"Incorrect API key provided: {fake_secret('sk-', 'proj-abcdef1234567890')}. Header Bearer abc.def.ghi123",
    )

    error = classify_model_error(exc, model="openai:gpt-5")

    assert error is not None
    assert fake_secret("sk-", "proj-abcdef1234567890") not in str(error)
    assert "abc.def.ghi123" not in str(error)
    assert "authentication failed" in str(error)


def test_partly_masked_and_short_provider_keys_are_redacted_too() -> None:
    exc = ProviderStatusError(401, "Incorrect API key provided: sk-proj-****************ab12")

    error = classify_model_error(exc)

    assert error is not None
    assert "ab12" not in str(error)
    assert "[REDACTED:api_key]" in str(error)


def test_retryable_is_per_occurrence_and_the_class_default_stays_false() -> None:
    transient = ModelProviderError("x", retryable=True)
    permanent = ModelProviderError("y")

    assert transient.retryable and not permanent.retryable
    assert ModelProviderError.retryable is False


def test_model_identity_uses_provider_and_model_name() -> None:
    named = FakeChatModel(["hi"], model_name="claude-test")
    unnamed = FunctionChatModel(lambda call: "hi")

    assert model_identity(named) == "awa-fake:claude-test"
    assert model_identity(unnamed).startswith("awa-function:FunctionChatModel@")
    assert model_identity(unnamed) != model_identity(FunctionChatModel(lambda call: "hi"))


def test_errors_survive_pickling_with_their_fields() -> None:
    import pickle

    error = ModelRateLimitError(
        "slow", kind="rate_limit", status=429, retry_after=1.0, retryable=True
    )
    copy: Any = pickle.loads(pickle.dumps(error))

    assert (copy.kind, copy.status, copy.retry_after, copy.retryable) == (
        "rate_limit",
        429,
        1.0,
        True,
    )


# ------------------------------------------------------------------ one provider lookup


def test_model_provider_reads_the_type_name_then_the_package_of_the_class() -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import AzureChatOpenAI, ChatOpenAI

    from awa.models.errors import model_provider
    from awa.testing import FakeChatModel

    class GeminiLike(FakeChatModel):
        pass

    GeminiLike.__module__ = "langchain_google_genai.chat_models"

    assert model_provider(ChatAnthropic(model="claude-sonnet-4-5", api_key="k")) == "anthropic"
    assert model_provider(ChatOpenAI(model="gpt-6", api_key="k")) == "openai"
    azure = AzureChatOpenAI(
        azure_deployment="d",
        azure_endpoint="https://x.openai.azure.com",
        api_key="k",
        api_version="v",
    )
    assert model_provider(azure) == "azure_openai"
    assert model_provider(GeminiLike(["x"])) == "google_genai"
    assert model_provider(FakeChatModel(["x"])) is None


def test_chat_model_class_finds_an_installed_integration_only() -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI

    from awa.models.errors import chat_model_class

    assert chat_model_class("anthropic") is ChatAnthropic
    assert chat_model_class("openai") is ChatOpenAI
    assert chat_model_class("unknown") is None
