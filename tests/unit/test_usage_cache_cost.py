"""Prompt-cache tokens are counted, priced at their own rates and exported per model call.

Providers count cached tokens inside the input tokens; awa subtracts them once and prices reads
and writes at their own rates, so the cost of the model-call spans adds up to the run's cost.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from langchain_core.messages import AIMessage  # noqa: E402
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from awa import Agent, Team  # noqa: E402
from awa.contracts.usage import ModelPrice, PriceTable, Usage, cache_tokens  # noqa: E402
from awa.observability import OpenTelemetrySubscriber  # noqa: E402
from awa.testing import FakeChatModel, tool_call  # noqa: E402

PRICE = ModelPrice(
    input_per_mtok=2.0, output_per_mtok=8.0, cache_read_per_mtok=0.5, cache_write_per_mtok=2.5
)


def usage(input_tokens: int, read: int, write: int, output: int = 100) -> Usage:
    result = Usage()
    result.record_model_call(
        "a",
        model="m",
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read_tokens=read,
        cache_write_tokens=write,
    )
    return result


@pytest.mark.parametrize(
    ("input_tokens", "read", "write", "expected"),
    [
        # 200 plain input at 2.0, 600 reads at 0.5, 200 writes at 2.5, 100 output at 8.0
        (1000, 600, 200, (200 * 2.0 + 600 * 0.5 + 200 * 2.5 + 100 * 8.0) / 1_000_000),
        # no cache: every input token at the input price
        (1000, 0, 0, (1000 * 2.0 + 100 * 8.0) / 1_000_000),
        # cache larger than the input: no negative plain input, the cache is still billed
        (500, 600, 200, (0 * 2.0 + 600 * 0.5 + 200 * 2.5 + 100 * 8.0) / 1_000_000),
    ],
)
def test_cache_reads_and_writes_are_priced_at_their_own_rates(
    input_tokens: int, read: int, write: int, expected: float
) -> None:
    assert PRICE.cost(usage(input_tokens, read, write)) == pytest.approx(expected, rel=1e-12)


def test_cache_prices_default_to_the_input_price() -> None:
    plain = ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0)

    assert plain.cost(usage(1000, 600, 200)) == pytest.approx(
        (1000 * 2.0 + 100 * 8.0) / 1_000_000, rel=1e-12
    )


def test_merging_keeps_the_cache_tokens() -> None:
    total = usage(1000, 600, 200)
    total.merge(usage(1000, 300, 100))

    assert (total.cache_read_tokens, total.cache_write_tokens) == (900, 300)
    assert (total.by_model["m"].cache_read_tokens, total.by_model["m"].cache_write_tokens) == (
        900,
        300,
    )


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"cache_read": 600, "cache_creation": 200}, (600, 200)),
        ({"priority_cache_read": 600, "priority_cache_creation": 200}, (600, 200)),
        ({"flex_cache_read": 600, "flex_cache_creation": 200}, (600, 200)),
        ({"cache_read": 100, "priority_cache_read": 200, "flex_cache_read": 300}, (600, 0)),
        ({}, (0, 0)),
    ],
)
def test_every_service_tier_counts_its_cache_tokens(
    details: dict[str, int], expected: tuple[int, int]
) -> None:
    metadata = {"input_tokens": 1000, "output_tokens": 10, "input_token_details": details}
    recorded = Usage()

    recorded.record_usage_metadata("a", metadata, model="m")

    assert cache_tokens(metadata) == expected
    assert (recorded.cache_read_tokens, recorded.cache_write_tokens) == expected


# ------------------------------------------------------------------ a recorded run

RECORDED_PRICES = PriceTable(
    prices={
        "cached-model": ModelPrice(
            input_per_mtok=2.0,
            output_per_mtok=8.0,
            cache_read_per_mtok=0.5,
            cache_write_per_mtok=2.5,
        )
    }
)


def reply(content: Any, input_tokens: int, read: int, write: int, output: int) -> AIMessage:
    """A model reply with the usage a provider reported for it (cache tokens inside input)."""

    if isinstance(content, str):
        message = AIMessage(content=content)
    else:
        message = AIMessage(content="", tool_calls=[content])
    message.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "total_tokens": input_tokens + output,
        "input_token_details": {"cache_read": read, "cache_creation": write},
    }
    message.response_metadata = {"model_name": "cached-model"}
    return message


def recorded_team(hooks: list[Any]) -> Team:
    """Two agents whose model calls report the usage of a cached run (about 90 % reads)."""

    helper = Agent(
        "helper",
        description="Answers.",
        model=FakeChatModel([reply("found", 10_000, 9_070, 0, 50)], model_name="cached-model"),
    )
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [
                reply(
                    tool_call("delegate_task", {"agent": "helper", "task": "find"}),
                    12_000,
                    0,
                    11_000,
                    30,
                ),
                reply("done", 12_400, 11_000, 300, 40),
            ],
            model_name="cached-model",
        ),
    )
    return Team("crew", orchestrator=lead, members=[helper], hooks=hooks)


def recorded_spans() -> tuple[list[ReadableSpan], Usage]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = OpenTelemetrySubscriber(
        provider.get_tracer("test"), prices=RECORDED_PRICES, langfuse=True
    )
    result = asyncio.run(recorded_team([tracing]).run("go"))
    return list(exporter.get_finished_spans()), result.usage


def attr(span: ReadableSpan, name: str) -> Any:
    return (span.attributes or {}).get(name)


def test_model_call_spans_carry_the_cache_buckets() -> None:
    spans, _ = recorded_spans()

    chats = sorted((s for s in spans if s.name.startswith("chat")), key=lambda s: s.start_time or 0)
    buckets = [
        (
            attr(s, "gen_ai.usage.input_tokens"),
            attr(s, "gen_ai.usage.cache_read.input_tokens"),
            attr(s, "gen_ai.usage.cache_creation.input_tokens"),
        )
        for s in chats
    ]
    assert sorted(buckets, key=str) == sorted(
        [(12_000, None, 11_000), (10_000, 9_070, None), (12_400, 11_000, 300)], key=str
    )


def test_the_cost_of_the_model_calls_adds_up_to_the_cost_of_the_run() -> None:
    spans, run_usage = recorded_spans()

    calls = [attr(s, "gen_ai.usage.cost") for s in spans if s.name.startswith("chat")]
    run_cost = run_usage.cost(RECORDED_PRICES)
    (root,) = [s for s in spans if s.name == "invoke_workflow crew"]

    assert len(calls) == 3 and None not in calls
    # Equal to 12 decimal places: two float sums in a different order differ only below that.
    assert round(sum(calls), 12) == round(run_cost, 12)
    assert round(attr(root, "awa.usage.cost"), 12) == round(run_cost, 12)
    expected = (
        (1_000 * 2.0 + 11_000 * 2.5 + 30 * 8.0)
        + (930 * 2.0 + 9_070 * 0.5 + 50 * 8.0)
        + (1_100 * 2.0 + 11_000 * 0.5 + 300 * 2.5 + 40 * 8.0)
    ) / 1_000_000
    assert round(run_cost, 12) == round(expected, 12)


def test_langfuse_usage_details_split_the_input_into_exclusive_buckets() -> None:
    import json

    from awa.observability.otel import langfuse_usage_types

    spans, _ = recorded_spans()

    chats = [s for s in spans if s.name.startswith("chat")]
    assert len(chats) == 3
    for span in chats:
        read_key, write_key = langfuse_usage_types(attr(span, "gen_ai.provider.name"))
        details = json.loads(attr(span, "langfuse.observation.usage_details"))
        assert set(details) <= {"input", "output", read_key, write_key}
        assert sum(v for k, v in details.items() if k != "output") == attr(
            span, "gen_ai.usage.input_tokens"
        )
        assert details["output"] == attr(span, "gen_ai.usage.output_tokens")
        assert details.get(read_key) == attr(span, "gen_ai.usage.cache_read.input_tokens")
        costs = json.loads(attr(span, "langfuse.observation.cost_details"))
        assert set(costs) == set(details) | {"total"}
        assert costs["total"] == pytest.approx(attr(span, "gen_ai.usage.cost"))
        ratio = attr(span, "langfuse.observation.metadata.cache_hit_ratio")
        assert ratio == pytest.approx(
            details.get(read_key, 0) / attr(span, "gen_ai.usage.input_tokens"), abs=1e-4
        )


def test_openai_and_azure_cache_reads_use_langfuses_openai_usage_type() -> None:
    from awa.observability.otel import langfuse_usage_types

    assert langfuse_usage_types("openai") == ("input_cached_tokens", "input_cache_creation")
    assert langfuse_usage_types("azure.ai.openai")[0] == "input_cached_tokens"
    assert langfuse_usage_types("anthropic") == ("input_cache_read", "input_cache_creation")
    assert langfuse_usage_types(None)[0] == "input_cache_read"


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        # A cache hit: OpenAI and Azure OpenAI report the reused prefix as `cached_tokens`.
        ({"cached_tokens": 1071}, {"input": 3, "output": 61, "input_cached_tokens": 1071}),
        # A cache write, reported as `cache_write_tokens` by models that report it.
        ({"cache_write_tokens": 1071}, {"input": 3, "output": 61, "input_cache_creation": 1071}),
        ({}, {"input": 1074, "output": 61}),
    ],
)
def test_azure_openai_usage_reaches_langfuse_in_openais_buckets(
    details: dict[str, int], expected: dict[str, int]
) -> None:
    import json

    openai_base = pytest.importorskip("langchain_openai.chat_models.base")

    # The usage block of an Azure OpenAI chat completion, converted as langchain-openai does.
    raw = {
        "prompt_tokens": 1074,
        "completion_tokens": 61,
        "total_tokens": 1135,
        "prompt_tokens_details": details,
    }
    message = AIMessage(content="done")
    message.usage_metadata = openai_base._create_usage_metadata(raw)
    message.response_metadata = {"model_name": "gpt-5.6-terra", "model_provider": "azure_openai"}
    price = ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0, cache_read_per_mtok=0.5)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = OpenTelemetrySubscriber(
        provider.get_tracer("test"),
        prices=PriceTable(prices={"gpt-5.6-terra": price}),
        langfuse=True,
    )
    agent = Agent("a", model=FakeChatModel([message], model_name="gpt-5.6-terra"), hooks=[tracing])
    asyncio.run(agent.run("go"))
    tracing.close()

    (chat,) = [s for s in exporter.get_finished_spans() if s.name.startswith("chat")]
    assert attr(chat, "gen_ai.provider.name") == "azure.ai.openai"
    usage_details = json.loads(attr(chat, "langfuse.observation.usage_details"))
    assert usage_details == expected
    # Known prices travel as cost per bucket (cache writes at the input price by default).
    costs = json.loads(attr(chat, "langfuse.observation.cost_details"))
    rates = {"input": 2.0, "output": 8.0, "input_cached_tokens": 0.5, "input_cache_creation": 2.0}
    assert costs == pytest.approx(
        {
            **{key: tokens * rates[key] / 1_000_000 for key, tokens in expected.items()},
            "total": sum(tokens * rates[key] for key, tokens in expected.items()) / 1_000_000,
        }
    )
