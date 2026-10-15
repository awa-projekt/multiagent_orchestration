"""Retries, fallbacks and circuit breakers for model requests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import patch

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from awa.agents import Agent
from awa.blueprints import BlueprintModelSettings
from awa.contracts import RunEvent, UsageLimits
from awa.contracts.events import (
    RUN_EVENT_ADAPTER,
    ModelFallbackEvent,
    ModelResponseEvent,
    ModelRetryEvent,
)
from awa.errors import (
    MaxStepsExceededError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResolutionError,
    UsageLimitExceededError,
)
from awa.models import (
    DEFAULT_REQUEST_TIMEOUT,
    CircuitBreaker,
    CircuitBreakerRegistry,
    ModelRegistry,
    ModelResilienceMiddleware,
    ModelRetryPolicy,
    ModelSettings,
    build_resilience_middleware,
    model_identity,
)
from awa.team import Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call

# --------------------------------------------------------------------------- helpers


class _Response:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status_code = status
        self.headers = headers


class ProviderStatusError(Exception):
    """Shaped like a provider SDK's status error."""

    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status} from provider")
        self.status_code = status
        self.response = _Response(status, {"retry-after": retry_after} if retry_after else {})


def rate_limited(retry_after: str | None = None) -> ProviderStatusError:
    return ProviderStatusError(429, retry_after)


def server_error(status: int = 500) -> ProviderStatusError:
    return ProviderStatusError(status)


def scripted(name: str, *items: Any) -> FunctionChatModel:
    """A model answering from `items` in order; exceptions in the script are raised."""

    def respond(call: ModelCall) -> Any:
        item = items[min(call.index, len(items) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    return FunctionChatModel(respond, model_name=name)


class Sleeps:
    """Records requested delays instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _fresh_default_breakers() -> Iterator[None]:
    CircuitBreakerRegistry.default().reset()
    yield
    CircuitBreakerRegistry.default().reset()


def resilient_agent(
    model: Any,
    *,
    retry: ModelRetryPolicy | None = None,
    fallbacks: tuple[Any, ...] = (),
    circuit_breaker: CircuitBreaker | None = None,
    breakers: CircuitBreakerRegistry | None = None,
    sleep: Sleeps | None = None,
    random: Any = None,
    registry: ModelRegistry | None = None,
    **agent_kwargs: Any,
) -> Agent:
    middleware = ModelResilienceMiddleware(
        retry=retry,
        fallbacks=fallbacks,
        circuit_breaker=circuit_breaker,
        model_registry=registry,
        breakers=breakers,
        sleep=sleep or Sleeps(),
        **({"random": random} if random is not None else {}),
    )
    return Agent("assistant", model=model, middleware=[middleware], **agent_kwargs)


async def run_collecting(agent: Agent, prompt: str = "hi", **kwargs: Any) -> tuple[Any, list[Any]]:
    events: list[RunEvent] = []
    result = None
    async for event in agent.stream(prompt, **kwargs):
        events.append(event)
        if event.type == "run_completed":
            result = event.result
    return result, events


def of_type(events: list[Any], kind: type) -> list[Any]:
    return [e for e in events if isinstance(e, kind)]


NO_JITTER = {"jitter": False}

# --------------------------------------------------------------------------- retries


async def test_rate_limits_are_retried_honouring_retry_after() -> None:
    model = scripted("primary", rate_limited(retry_after="2"), rate_limited(), "done")
    sleeps = Sleeps()
    agent = resilient_agent(
        model,
        retry=ModelRetryPolicy(max_attempts=3, initial_delay=0.5, **NO_JITTER),
        sleep=sleeps,
        max_steps=1,  # retries are not steps
    )

    result, events = await run_collecting(agent)

    assert result.output == "done"
    assert sleeps.delays == [2.0, 1.0]  # Retry-After beats the 0.5s backoff; then 0.5 * 2
    retries = of_type(events, ModelRetryEvent)
    assert [(e.attempt, e.delay, e.kind, e.status) for e in retries] == [
        (1, 2.0, "rate_limit", 429),
        (2, 1.0, "rate_limit", 429),
    ]
    assert all(e.model == "awa-function:primary" for e in retries)
    assert all(e.error_type == "ProviderStatusError" for e in retries)
    assert result.usage.requests == 3  # every attempt is a request
    assert result.usage.by_model["primary"].requests == 3


async def test_backoff_grows_exponentially_and_is_capped() -> None:
    model = scripted("primary", server_error(), server_error(502), server_error(503), "ok")
    sleeps = Sleeps()
    agent = resilient_agent(
        model,
        retry=ModelRetryPolicy(
            max_attempts=4, initial_delay=1.0, backoff_factor=3.0, max_delay=5.0, **NO_JITTER
        ),
        sleep=sleeps,
    )

    result = await agent.run("hi")

    assert result.output == "ok"
    assert sleeps.delays == [1.0, 3.0, 5.0]


@pytest.mark.parametrize(("draw", "expected"), [(0.0, 0.75), (0.5, 1.0), (0.999999, 1.25)])
async def test_jitter_spreads_delays_by_a_quarter(draw: float, expected: float) -> None:
    sleeps = Sleeps()
    agent = resilient_agent(
        scripted("primary", server_error(), "ok"),
        retry=ModelRetryPolicy(max_attempts=2, initial_delay=1.0),
        sleep=sleeps,
        random=lambda: draw,
    )

    await agent.run("hi")

    assert sleeps.delays == [pytest.approx(expected)]


@pytest.mark.parametrize(("draw", "expected"), [(0.0, 3.0), (0.999999, 4.0)])
async def test_jitter_spreads_a_capped_delay_below_the_cap(draw: float, expected: float) -> None:
    sleeps = Sleeps()
    agent = resilient_agent(
        scripted("primary", server_error(), "ok"),
        retry=ModelRetryPolicy(max_attempts=2, initial_delay=10.0, max_delay=4.0),
        sleep=sleeps,
        random=lambda: draw,
    )

    await agent.run("hi")

    assert sleeps.delays == [pytest.approx(expected)]


async def test_timeouts_and_connection_errors_are_retried() -> None:
    model = scripted("primary", TimeoutError("slow"), ConnectionResetError("reset"), "ok")
    agent = resilient_agent(model, retry=ModelRetryPolicy(max_attempts=3, initial_delay=0))

    result = await agent.run("hi")

    assert result.output == "ok"
    assert model.call_count == 3


async def test_usage_keeps_one_key_per_configured_model_whatever_the_provider_reports() -> None:
    # An Azure deployment configured as "my-deployment" answers as a dated model version.
    answer = AIMessage(content="ok", response_metadata={"model_name": "gpt-4.1-2025-04-14"})
    model = scripted("my-deployment", server_error(503), answer)
    agent = resilient_agent(model, retry=ModelRetryPolicy(max_attempts=2, initial_delay=0))

    result, events = await run_collecting(agent)

    assert list(result.usage.by_model) == ["my-deployment"]
    assert result.usage.by_model["my-deployment"].requests == 2  # the failure and the answer
    assert result.usage.by_model["my-deployment"].total_tokens == result.usage.total_tokens > 0
    [response] = of_type(events, ModelResponseEvent)
    assert response.model == "gpt-4.1-2025-04-14"


async def test_retry_after_beyond_max_delay_gives_up_instead_of_retrying_early() -> None:
    model = scripted("primary", rate_limited(retry_after="120"), "never")
    sleeps = Sleeps()
    agent = resilient_agent(model, retry=ModelRetryPolicy(max_delay=30), sleep=sleeps)

    with pytest.raises(ModelRateLimitError) as caught:
        await agent.run("hi")

    assert caught.value.retry_after == 120.0
    assert sleeps.delays == []
    assert model.call_count == 1


async def test_non_retryable_errors_are_not_retried() -> None:
    model = scripted("primary", server_error(400), "never")
    agent = resilient_agent(model, retry=ModelRetryPolicy(initial_delay=0))

    with pytest.raises(ModelProviderError) as caught:
        await agent.run("hi")

    assert caught.value.kind == "invalid_request"
    assert not caught.value.retryable
    assert model.call_count == 1


async def test_exhausted_retries_raise_a_typed_error_caused_by_the_provider_error() -> None:
    model = scripted("primary", server_error(503))
    agent = resilient_agent(model, retry=ModelRetryPolicy(max_attempts=3, initial_delay=0))

    with pytest.raises(ModelProviderError) as caught:
        result = await agent.run("hi")
        del result

    assert caught.value.kind == "server_error" and caught.value.retryable
    assert isinstance(caught.value.__cause__, ProviderStatusError)
    assert model.call_count == 3


async def test_retry_on_can_narrow_the_retried_kinds() -> None:
    model = scripted("primary", server_error(), "ok")
    agent = resilient_agent(model, retry=ModelRetryPolicy(retry_on=("rate_limit",)))

    with pytest.raises(ModelProviderError):
        await agent.run("hi")
    assert model.call_count == 1


async def test_unrecognised_errors_are_not_retried_and_re_raised_unchanged() -> None:
    model = scripted("primary", RuntimeError("bug in a custom model"))
    agent = resilient_agent(model, retry=ModelRetryPolicy(initial_delay=0))

    with pytest.raises(RuntimeError, match="bug in a custom model"):
        await agent.run("hi")
    assert model.call_count == 1


# --------------------------------------------------------------------------- limits, cancellation


async def test_max_requests_counts_every_attempt() -> None:
    model = scripted("primary", server_error())
    agent = resilient_agent(model, retry=ModelRetryPolicy(max_attempts=5, initial_delay=0))

    with pytest.raises(UsageLimitExceededError):
        await agent.run("hi", usage_limits=UsageLimits(max_requests=2))

    assert model.call_count == 2


async def test_usage_limit_errors_are_never_retried_or_handed_to_a_fallback() -> None:
    model = scripted("primary", UsageLimitExceededError("budget"))
    backup = FakeChatModel(["backup"], model_name="backup")
    agent = resilient_agent(model, retry=ModelRetryPolicy(initial_delay=0), fallbacks=(backup,))

    with pytest.raises(UsageLimitExceededError, match="budget"):
        await agent.run("hi")

    assert model.call_count == 1
    assert backup.call_count == 0


async def test_retries_do_not_count_as_steps() -> None:
    model = scripted("primary", server_error(), server_error(), tool_call("noop"), "ok")

    def noop() -> str:
        """Do nothing."""
        return "done"

    agent = resilient_agent(
        model, retry=ModelRetryPolicy(max_attempts=3, initial_delay=0), tools=[noop], max_steps=2
    )

    result = await agent.run("hi")

    assert result.output == "ok"
    too_few = resilient_agent(scripted("again", tool_call("noop"), "ok"), tools=[noop], max_steps=1)
    with pytest.raises(MaxStepsExceededError):
        await too_few.run("hi")


def _request(model: Any) -> ModelRequest[Any]:
    return ModelRequest(model=model, messages=[HumanMessage("hi")])


async def test_cancellation_is_never_retried_and_releases_a_half_open_trial() -> None:
    clock = Clock()
    breakers = CircuitBreakerRegistry(clock=clock)
    breaker = CircuitBreaker(failure_threshold=1, reset_after=5)
    model = FakeChatModel(["x"], model_name="primary")
    key = model_identity(model)
    breakers.record_failure(key, breaker)
    clock.now += 5
    middleware = ModelResilienceMiddleware(
        retry=ModelRetryPolicy(initial_delay=0), circuit_breaker=breaker, breakers=breakers
    )
    calls = 0

    async def handler(request: ModelRequest[Any]) -> ModelResponse[Any]:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await middleware.awrap_model_call(_request(model), handler)

    assert calls == 1
    assert breakers.acquire(key, breaker)  # the trial slot was released


async def test_without_a_run_context_the_middleware_still_retries() -> None:
    middleware = ModelResilienceMiddleware(retry=ModelRetryPolicy(initial_delay=0))
    outcomes: list[Any] = [server_error(), ModelResponse(result=[AIMessage("fine")])]

    async def handler(request: ModelRequest[Any]) -> ModelResponse[Any]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return cast(ModelResponse[Any], outcome)

    response = await middleware.awrap_model_call(
        _request(FakeChatModel(["x"], model_name="m")), handler
    )

    assert isinstance(response, ModelResponse)
    assert response.result[0].text == "fine"


# --------------------------------------------------------------------------- fallbacks


def _registry(**profiles: Any) -> ModelRegistry:
    registry = ModelRegistry()
    for name, model in profiles.items():
        registry.register(name, model)
    return registry


async def test_fallback_chain_is_tried_in_order_and_usage_is_per_actual_model() -> None:
    primary = scripted("primary", server_error())
    backup1 = scripted("backup1", server_error(503))
    backup2 = FakeChatModel(["from backup2"], model_name="backup2")
    registry = _registry(backup1=backup1, backup2=backup2)
    agent = resilient_agent(
        primary,
        retry=ModelRetryPolicy(max_attempts=2, initial_delay=0),
        fallbacks=("backup1", "backup2"),
        registry=registry,
    )

    result, events = await run_collecting(agent)

    assert result.output == "from backup2"
    sequence = [
        (e.type, getattr(e, "model", None), getattr(e, "to_model", None))
        for e in events
        if isinstance(e, ModelRetryEvent | ModelFallbackEvent)
    ]
    assert sequence == [
        ("model_retry", "awa-function:primary", None),
        ("model_fallback", None, "backup1"),
        ("model_retry", "backup1", None),
        ("model_fallback", None, "backup2"),
    ]
    fallbacks = of_type(events, ModelFallbackEvent)
    assert [(e.from_model, e.reason, e.error_type) for e in fallbacks] == [
        ("awa-function:primary", "server_error", "ProviderStatusError"),
        ("backup1", "server_error", "ProviderStatusError"),
    ]
    assert (primary.call_count, backup1.call_count, backup2.call_count) == (2, 2, 1)
    by_model = result.usage.by_model
    assert by_model["primary"].requests == 2 and by_model["primary"].total_tokens == 0
    assert by_model["backup1"].requests == 2 and by_model["backup1"].total_tokens == 0
    assert by_model["backup2"].requests == 1 and by_model["backup2"].total_tokens > 0
    assert of_type(events, ModelResponseEvent)[-1].model == "backup2"


async def test_non_retryable_errors_go_straight_to_the_fallback() -> None:
    primary = scripted("primary", server_error(401))
    backup = FakeChatModel(["backup answer"], model_name="backup")
    agent = resilient_agent(primary, retry=ModelRetryPolicy(), fallbacks=(backup,))

    result, events = await run_collecting(agent)

    assert result.output == "backup answer"
    assert primary.call_count == 1
    assert of_type(events, ModelFallbackEvent)[0].reason == "authentication"
    assert of_type(events, ModelFallbackEvent)[0].to_model == "awa-fake:backup"


async def test_last_failure_is_raised_when_every_fallback_fails() -> None:
    agent = resilient_agent(
        scripted("primary", server_error()), fallbacks=(scripted("backup", rate_limited("1")),)
    )

    with pytest.raises(ModelRateLimitError) as caught:
        await agent.run("hi")

    assert caught.value.model == "awa-function:backup"


async def test_model_settings_fallbacks_apply_to_a_model_instance() -> None:
    primary = scripted("primary", server_error())
    registry = _registry(backup=FakeChatModel(["saved"], model_name="backup"))
    agent = Agent(
        "assistant",
        model=primary,
        model_registry=registry,
        model_settings=ModelSettings(retry=ModelRetryPolicy(max_attempts=1), fallbacks=("backup",)),
    )

    result = await agent.run("hi")

    assert result.output == "saved"


async def test_profile_settings_make_every_agent_on_the_profile_resilient() -> None:
    registry = ModelRegistry()
    registry.register("backup", FakeChatModel(["backup"], model_name="backup", repeat_last=True))
    registry.register(
        "main", scripted("main", server_error()), settings=ModelSettings(fallbacks=("backup",))
    )
    first = Agent("first", model="main", model_registry=registry)
    second = Agent("second", model="main", model_registry=registry)

    assert (await first.run("hi")).output == "backup"
    assert (await second.run("hi")).output == "backup"


async def test_the_model_request_guard_applies_to_fallbacks() -> None:
    string_fallback = resilient_agent(
        scripted("primary", server_error()), fallbacks=("openai:gpt-5-mini",)
    )
    with pytest.raises(ModelResolutionError, match="blocked"):
        await string_fallback.run("hi")

    class RealModel(FakeChatModel):
        @property
        def _llm_type(self) -> str:
            return "provider"

    real = RealModel(responses=["would call the network"], model_name="real")
    instance_fallback = resilient_agent(scripted("primary", server_error()), fallbacks=(real,))
    with pytest.raises(ModelResolutionError, match="blocked"):
        await instance_fallback.run("hi")
    assert real.call_count == 0


class Answer(BaseModel):
    city: str
    population: int


async def test_structured_output_is_validated_after_a_fallback() -> None:
    backup = FakeChatModel(
        [structured_output(Answer, {"city": "Graz", "population": 300_000})], model_name="backup"
    )
    agent = resilient_agent(
        scripted("primary", server_error()), fallbacks=(backup,), output_type=Answer
    )

    result = await agent.run("Largest city in Styria?")

    output: Any = result.output
    assert output == Answer(city="Graz", population=300_000)
    assert "Answer" in backup.calls[0].tool_names


async def test_a_team_member_falls_back_when_its_model_fails() -> None:
    registry = _registry(backup=FakeChatModel(["BM25 plus vectors."], model_name="backup"))
    researcher = Agent(
        "researcher",
        description="Finds evidence.",
        model=scripted("primary", server_error(503)),
        model_registry=registry,
        model_settings=ModelSettings(
            retry=ModelRetryPolicy(max_attempts=2, initial_delay=0), fallbacks=("backup",)
        ),
    )
    lead = Agent(
        "lead",
        description="Coordinates.",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "researcher", "task": "hybrid search?"}), "done"]
        ),
    )
    team = Team("desk", orchestrator=lead, members=[researcher])

    events: list[Any] = []
    result = None
    async for event in team.stream("What is hybrid search?"):
        events.append(event)
        if event.type == "run_completed":
            result = event.result

    assert result is not None and result.output == "done"
    fallback = of_type(events, ModelFallbackEvent)
    assert [(e.agent, e.to_model, e.reason) for e in fallback] == [
        ("researcher", "backup", "server_error")
    ]
    assert [e.agent for e in of_type(events, ModelRetryEvent)] == ["researcher"]
    assert any("BM25 plus vectors." in str(m.content) for m in result.messages)
    assert result.usage.by_model["backup"].requests == 1
    assert result.usage.by_model["primary"].requests == 2


# --------------------------------------------------------------------------- circuit breaker


async def test_circuit_opens_after_the_threshold_skips_the_model_then_half_opens() -> None:
    clock = Clock()
    breakers = CircuitBreakerRegistry(clock=clock)
    breaker = CircuitBreaker(failure_threshold=2, reset_after=10)
    outcomes: list[Any] = [server_error(), server_error(), "recovered"]

    def respond(call: ModelCall) -> Any:
        item = outcomes[min(call.index, len(outcomes) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    primary = FunctionChatModel(respond, model_name="primary")
    backup = FakeChatModel(["backup"], model_name="backup", repeat_last=True)
    agent = resilient_agent(
        primary, fallbacks=(backup,), circuit_breaker=breaker, breakers=breakers
    )
    key = model_identity(primary)

    await agent.run("1")
    assert breakers.state(key, breaker) == "closed"
    await agent.run("2")
    assert breakers.state(key, breaker) == "open"

    result, events = await run_collecting(agent, "3")
    assert result.output == "backup"
    assert primary.call_count == 2  # skipped while open
    assert of_type(events, ModelFallbackEvent)[0].reason == "circuit_open"
    assert of_type(events, ModelFallbackEvent)[0].error_type is None

    clock.now += 10
    assert breakers.state(key, breaker) == "half_open"
    result = await agent.run("4")
    assert result.output == "recovered"  # the trial request succeeded
    assert breakers.state(key, breaker) == "closed"


async def test_a_failed_half_open_trial_opens_the_circuit_again() -> None:
    clock = Clock()
    breakers = CircuitBreakerRegistry(clock=clock)
    breaker = CircuitBreaker(failure_threshold=1, reset_after=5)
    primary = scripted("primary", server_error())
    agent = resilient_agent(
        primary,
        fallbacks=(FakeChatModel(["b"], repeat_last=True),),
        circuit_breaker=breaker,
        breakers=breakers,
    )
    key = model_identity(primary)

    await agent.run("1")
    clock.now += 5
    await agent.run("2")  # the trial fails

    assert primary.call_count == 2
    assert breakers.state(key, breaker) == "open"
    assert breakers.retry_in(key, breaker) == pytest.approx(5)


async def test_all_circuits_open_raises_a_retryable_circuit_open_error() -> None:
    clock = Clock()
    breakers = CircuitBreakerRegistry(clock=clock)
    breaker = CircuitBreaker(failure_threshold=1, reset_after=30)
    primary = scripted("primary", server_error())
    agent = resilient_agent(primary, circuit_breaker=breaker, breakers=breakers)

    with pytest.raises(ModelProviderError):
        await agent.run("1")
    clock.now += 12
    with pytest.raises(ModelProviderError) as caught:
        await agent.run("2")

    assert caught.value.kind == "circuit_open"
    assert caught.value.retryable
    assert caught.value.retry_after == pytest.approx(18)
    assert primary.call_count == 1


async def test_non_transient_errors_do_not_trip_the_breaker() -> None:
    breakers = CircuitBreakerRegistry()
    breaker = CircuitBreaker(failure_threshold=1)
    primary = scripted("primary", server_error(400))
    agent = resilient_agent(primary, circuit_breaker=breaker, breakers=breakers)

    for _ in range(3):
        with pytest.raises(ModelProviderError):
            await agent.run("hi")

    assert breakers.state(model_identity(primary), breaker) == "closed"
    assert primary.call_count == 3


async def test_circuit_state_is_shared_per_model_across_agents_and_runs() -> None:
    settings = ModelSettings(circuit_breaker=CircuitBreaker(failure_threshold=2))
    first = Agent("first", model=scripted("shared", server_error()), model_settings=settings)
    second = Agent("second", model=scripted("shared", server_error()), model_settings=settings)

    for agent in (first, second):
        with pytest.raises(ModelProviderError):
            await agent.run("hi")

    state = CircuitBreakerRegistry.default().state("awa-function:shared", CircuitBreaker())
    assert state == "open"
    with pytest.raises(ModelProviderError) as caught:
        await first.run("hi")
    assert caught.value.kind == "circuit_open"


async def test_half_open_admits_exactly_one_concurrent_trial() -> None:
    clock = Clock()
    breakers = CircuitBreakerRegistry(clock=clock)
    breaker = CircuitBreaker(failure_threshold=1, reset_after=1)
    breakers.record_failure("m", breaker)
    clock.now += 1

    async def try_acquire() -> str | None:
        await asyncio.sleep(0)
        return breakers.acquire("m", breaker)

    admitted = await asyncio.gather(*(try_acquire() for _ in range(20)))

    assert admitted.count("trial") == 1
    assert admitted.count(None) == 19
    breakers.record_success("m")
    assert all(breakers.acquire("m", breaker) == "closed" for _ in range(3))


def test_acquire_reports_closed_trial_or_refusal_under_a_fake_clock() -> None:
    clock = Clock()
    breakers = CircuitBreakerRegistry(clock=clock)
    breaker = CircuitBreaker(failure_threshold=1, reset_after=5)
    assert breakers.acquire("m", breaker) == "closed"
    breakers.record_failure("m", breaker)
    assert breakers.acquire("m", breaker) is None
    clock.now += 5
    assert breakers.acquire("m", breaker) == "trial"
    assert breakers.acquire("m", breaker) is None
    breakers.release("m")
    assert breakers.acquire("m", breaker) == "trial"


def test_retry_in_counts_down_and_ends_after_reset_after() -> None:
    clock = Clock()
    breakers = CircuitBreakerRegistry(clock=clock)
    breaker = CircuitBreaker(failure_threshold=1, reset_after=5)
    assert breakers.retry_in("m", breaker) is None
    breakers.record_failure("m", breaker)
    assert breakers.retry_in("m", breaker) == 5
    clock.now += 5
    assert breakers.retry_in("m", breaker) is None


def test_reset_closes_one_model_or_all_and_release_of_an_unknown_model_is_a_noop() -> None:
    breakers = CircuitBreakerRegistry(clock=Clock())
    breaker = CircuitBreaker(failure_threshold=1, reset_after=5)
    breakers.record_failure("a", breaker)
    breakers.record_failure("b", breaker)
    breakers.release("unknown")
    breakers.reset("a")
    assert breakers.state("a", breaker) == "closed"
    assert breakers.state("b", breaker) == "open"
    breakers.reset()
    assert breakers.state("b", breaker) == "closed"


def test_a_string_fallback_is_resolved_once_and_cached() -> None:
    middleware = ModelResilienceMiddleware(fallbacks=["fake:backup"])
    with patch(
        "awa.models.resilience.resolve_model", side_effect=lambda *a, **k: FakeChatModel(["x"])
    ):
        first = middleware._fallback_model(0, "fake:backup")
        second = middleware._fallback_model(0, "fake:backup")
    assert first is second


# --------------------------------------------------------------------------- settings and wiring


def test_merge_keeps_nested_policies_and_to_kwargs_excludes_them() -> None:
    base = ModelSettings(temperature=0, retry=ModelRetryPolicy(max_attempts=2))
    merged = base.merge(
        ModelSettings(fallbacks=("fast",), circuit_breaker=CircuitBreaker(failure_threshold=3))
    )

    assert isinstance(merged.retry, ModelRetryPolicy) and merged.retry.max_attempts == 2
    assert merged.fallbacks == ("fast",)
    assert isinstance(merged.circuit_breaker, CircuitBreaker)
    assert merged.to_kwargs() == {"temperature": 0}
    assert merged.has_resilience and merged.has_construction_settings
    assert not ModelSettings(retry=ModelRetryPolicy()).has_construction_settings


def test_invalid_policies_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelRetryPolicy(max_attempts=0)
    with pytest.raises(ValidationError):
        ModelRetryPolicy(retry_on=("sometimes",))
    with pytest.raises(ValidationError):
        CircuitBreaker(reset_after=0)
    with pytest.raises(ValidationError):
        ModelSettings(fallbacks=("",))


def test_construction_settings_with_a_model_instance_are_still_rejected() -> None:
    model = FakeChatModel(["x"])

    Agent("ok", model=model, model_settings=ModelSettings(fallbacks=("fast",)))
    with pytest.raises(ValueError, match="model_settings"):
        Agent("bad", model=model, model_settings=ModelSettings(temperature=0))


def test_build_resilience_middleware_only_when_configured() -> None:
    registry = ModelRegistry()

    assert build_resilience_middleware("fast", None, registry) is None
    assert build_resilience_middleware("fast", ModelSettings(temperature=0), registry) is None
    middleware = build_resilience_middleware(
        "fast", ModelSettings(fallbacks=("strong",), retry=ModelRetryPolicy()), registry
    )
    assert middleware is not None and middleware.fallbacks == ("strong",)


@pytest.mark.model_requests_allowed  # the model is built by a patched constructor, never called
def test_a_fallback_leaves_retrying_to_awa_when_the_profile_sets_the_retry_policy() -> None:
    registry = ModelRegistry()
    registry.register(
        "primary",
        "openai:gpt-5-mini",
        settings=ModelSettings(retry=ModelRetryPolicy(), fallbacks=("openai:gpt-5-nano",)),
    )
    middleware = build_resilience_middleware("primary", None, registry)
    assert middleware is not None

    with patch("awa.models.registry.init_chat_model") as init:
        middleware._fallback_model(0, "openai:gpt-5-nano")

    assert init.call_args.kwargs["max_retries"] == 0


def test_a_retry_policy_turns_off_provider_sdk_retries() -> None:
    registry = ModelRegistry()
    with patch("awa.models.registry.init_chat_model") as init:
        registry.create("openai:gpt-5-mini", ModelSettings(retry=ModelRetryPolicy()))
        registry.create(
            "anthropic:claude-x", ModelSettings(retry=ModelRetryPolicy(), max_retries=4)
        )
        registry.create("google_genai:gemini-x", ModelSettings(retry=ModelRetryPolicy()))
        registry.create("openai:gpt-5-mini", ModelSettings(temperature=0))

    kwargs = [call.kwargs for call in init.call_args_list]
    assert kwargs[0]["max_retries"] == 0
    assert kwargs[1]["max_retries"] == 4
    assert "max_retries" not in kwargs[2]
    assert "max_retries" not in kwargs[3]


def test_openai_models_get_a_request_timeout_unless_one_is_set() -> None:
    registry = ModelRegistry()
    with patch("awa.models.registry.init_chat_model") as init:
        registry.create("azure_openai:my-deployment")
        registry.create("openai:gpt-5-mini", ModelSettings(timeout=30))
        registry.create("anthropic:claude-x")

    kwargs = [call.kwargs for call in init.call_args_list]
    assert kwargs[0]["timeout"] == DEFAULT_REQUEST_TIMEOUT == 600.0
    assert kwargs[1]["timeout"] == 30
    assert "timeout" not in kwargs[2]


def test_the_request_timeout_reaches_the_openai_client() -> None:
    model = ModelRegistry().create("openai:gpt-5-mini", ModelSettings(extra={"api_key": "sk-test"}))

    assert model.request_timeout == DEFAULT_REQUEST_TIMEOUT  # type: ignore[attr-defined]


def test_blueprint_model_settings_accept_resilience_and_reject_typos() -> None:
    settings = BlueprintModelSettings.model_validate(
        {
            "temperature": 0,
            "retry": {"max_attempts": 4, "initial_delay": 0.5},
            "fallbacks": ["fast", "openai:gpt-5-mini"],
            "circuit_breaker": {"failure_threshold": 3},
        }
    )

    assert settings.retry == ModelRetryPolicy(max_attempts=4, initial_delay=0.5)
    assert settings.fallbacks == ("fast", "openai:gpt-5-mini")
    with pytest.raises(ValidationError):
        BlueprintModelSettings.model_validate({"retry": {"max_attempt": 4}})


def test_resilience_events_round_trip_through_the_event_adapter() -> None:
    fields = {"run_id": "r", "root_run_id": "r", "agent": "a"}
    retry = ModelRetryEvent(
        model="m", attempt=1, delay=0.5, error_type="E", kind="timeout", **fields
    )
    fallback = ModelFallbackEvent(from_model="m", to_model="n", reason="circuit_open", **fields)

    for event in (retry, fallback):
        assert RUN_EVENT_ADAPTER.validate_json(RUN_EVENT_ADAPTER.dump_json(event)) == event
