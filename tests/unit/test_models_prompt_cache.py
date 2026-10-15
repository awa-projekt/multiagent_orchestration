"""Provider prompt caching (awa.models.prompt_cache), proven against the real provider clients.

The Claude and OpenAI chat models run unchanged; only their HTTP transport is replaced by one that
records each request body and answers with a response in the provider's documented format
(Anthropic Messages API usage `cache_creation_input_tokens` / `cache_read_input_tokens`, OpenAI
`prompt_tokens_details.cached_tokens`). No request leaves the machine.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from awa import Agent
from awa.contracts.events import ModelResponseEvent, RunEvent
from awa.contracts.usage import PriceTable
from awa.core.settings import Settings
from awa.models import ModelRegistry, ModelSettings
from awa.models.prompt_cache import (
    OPENAI_MIN_PREFIX_TOKENS,
    PAD_TARGET_TOKENS,
    PromptCacheMiddleware,
    estimated_prefix_tokens,
    padded_request,
    prefix_hash,
    prompt_cache_key,
    stable_request,
)
from awa.runtime.cache import Cache, CacheSettings
from awa.testing import FakeChatModel

pytestmark = pytest.mark.model_requests_allowed

SYSTEM = "You check supplier invoices. " * 200  # well above every provider's minimum prefix
NEAR = " ".join(["You check supplier invoices against the vendor master."] * 60)  # ~3,300 chars


def lookup_vendor(vendor_id: str) -> str:
    """Look up a vendor in the vendor master."""
    return f"{vendor_id}: Mueller GmbH"


def check_iban(iban: str) -> str:
    """Check an IBAN."""
    return "valid"


def _claude_reply(
    text: str, *, write: int, read: int, tool: dict[str, Any] | None = None
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if tool is not None:
        content.append(
            {"type": "tool_use", "id": "toolu_1", "name": tool["name"], "input": tool["input"]}
        )
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": "tool_use" if tool else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 40,
            "output_tokens": 12,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
        },
    }


def _openai_reply(text: str, *, cached: int) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5.4",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 2100,
            "completion_tokens": 7,
            "total_tokens": 2107,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


class Recorder:
    """An HTTP transport that keeps every request body and answers from a script."""

    def __init__(self, replies: Any, http: Any = httpx) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._replies = replies
        self.http = http

    def __call__(self, request: Any) -> Any:
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        if callable(self._replies):
            status, reply = self._replies(body)
        else:
            status, reply = 200, self._replies[min(len(self.bodies), len(self._replies)) - 1]
        return self.http.Response(status, json=reply)


def claude(recorder: Recorder) -> Any:
    import anthropic
    import httpx2
    from langchain_anthropic import ChatAnthropic

    recorder.http = httpx2

    # langchain-anthropic 1.7.5 defaults claude-sonnet-5-5 to max_tokens=128000, which the anthropic
    # SDK (1.11) only sends as a streamed request; these tests make plain requests.
    model = ChatAnthropic(
        model="claude-sonnet-5-5", api_key="sk-ant-test", max_retries=0, max_tokens=4096
    )
    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
        max_retries=0,
    )
    model.__dict__["_async_client"] = client
    return model


def openai(recorder: Recorder) -> Any:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model="gpt-5.4",
        api_key="sk-test",
        max_retries=0,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )


def _has_cache_control(value: Any) -> bool:
    return "cache_control" in json.dumps(value)


# ---------------------------------------------------------------- Claude


async def test_claude_writes_the_cache_on_the_first_call_and_reads_it_on_the_second() -> None:
    recorder = Recorder(
        [
            _claude_reply(
                "",
                write=2400,
                read=0,
                tool={"name": "lookup_vendor", "input": {"vendor_id": "V-100"}},
            ),
            _claude_reply("V-100 is Mueller GmbH.", write=0, read=2400),
        ]
    )
    agent = Agent("clerk", instructions=SYSTEM, model=claude(recorder), tools=[lookup_vendor])

    result = await agent.run("Who is V-100?", metadata={"awa.tenant": "acme"})

    assert len(recorder.bodies) == 2
    for body in recorder.bodies:
        assert body["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
        assert body["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
        assert body["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    calls = result.usage.by_model["claude-sonnet-5-5"]
    assert (calls.requests, calls.cache_write_tokens, calls.cache_read_tokens) == (2, 2400, 2400)


async def test_claude_costs_add_up_exactly_with_cache_write_and_read_prices() -> None:
    recorder = Recorder(
        [
            _claude_reply(
                "",
                write=2400,
                read=0,
                tool={"name": "lookup_vendor", "input": {"vendor_id": "V-1"}},
            ),
            _claude_reply("done", write=0, read=2400),
        ]
    )
    agent = Agent("clerk", instructions=SYSTEM, model=claude(recorder), tools=[lookup_vendor])
    prices = PriceTable.from_mapping(
        {
            "claude-sonnet-5-5": {
                "input_per_mtok": 3.0,
                "output_per_mtok": 15.0,
                "cache_read_per_mtok": 0.3,
                "cache_write_per_mtok": 3.75,
            }
        }
    )

    result = await agent.run("Who is V-1?")

    usage = result.usage
    # Per call 40 plain input tokens, 12 output tokens; 2400 written once and read once.
    assert (usage.input_tokens, usage.output_tokens) == (80 + 4800, 24)
    assert usage.cost(prices) == (80 * 3.0 + 2400 * 0.3 + 2400 * 3.75 + 24 * 15.0) / 1_000_000


async def test_auto_leaves_a_one_off_claude_call_without_tools_unmarked() -> None:
    recorder = Recorder([_claude_reply("Hello.", write=0, read=0)])
    agent = Agent("greeter", instructions=SYSTEM, model=claude(recorder))

    await agent.run("Hi")

    assert not _has_cache_control(recorder.bodies[0])


async def test_auto_marks_the_same_prefix_when_it_repeats_within_five_minutes() -> None:
    recorder = Recorder([_claude_reply("Hello.", write=0, read=0)])
    agent = Agent("greeter", instructions=SYSTEM, model=claude(recorder))

    await agent.run("Hi")
    await agent.run("Hi again")

    assert not _has_cache_control(recorder.bodies[0])
    assert recorder.bodies[1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


async def test_one_hour_marks_every_claude_call_with_the_one_hour_lifetime() -> None:
    recorder = Recorder([_claude_reply("Hello.", write=0, read=0)])
    agent = Agent("greeter", instructions=SYSTEM, model=claude(recorder), prompt_cache="1h")

    await agent.run("Hi")

    assert recorder.bodies[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert recorder.bodies[0]["system"][-1]["cache_control"]["ttl"] == "1h"


@pytest.mark.parametrize(
    "off", [{"prompt_cache": "off"}, {"cache": Cache(CacheSettings(enabled=False))}]
)
async def test_off_sends_no_marker(off: dict[str, Any]) -> None:
    recorder = Recorder(
        [
            _claude_reply(
                "", write=0, read=0, tool={"name": "lookup_vendor", "input": {"vendor_id": "V"}}
            ),
            _claude_reply("ok", write=0, read=0),
        ]
    )
    agent = Agent(
        "clerk", instructions=SYSTEM, model=claude(recorder), tools=[lookup_vendor], **off
    )

    await agent.run("Who is V?")

    assert not any(_has_cache_control(body) for body in recorder.bodies)


async def test_a_fallback_from_claude_to_another_provider_never_carries_cache_control() -> None:
    def failing(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}

    claude_calls = Recorder(failing)
    azure_calls = Recorder([_openai_reply("Fallback answer.", cached=0)])
    registry = ModelRegistry(Settings(_env_file=None))
    registry.register("backup", openai(azure_calls))
    agent = Agent(
        "clerk",
        instructions=SYSTEM,
        model=claude(claude_calls),
        tools=[lookup_vendor],
        model_registry=registry,
        model_settings=ModelSettings(fallbacks=("backup",)),
    )

    result = await agent.run("Who is V-100?")

    assert result.output == "Fallback answer."
    assert claude_calls.bodies and _has_cache_control(claude_calls.bodies[0])
    assert len(azure_calls.bodies) == 1
    assert not _has_cache_control(azure_calls.bodies[0])
    assert azure_calls.bodies[0]["prompt_cache_key"].startswith("awa-")


# ---------------------------------------------------------------- OpenAI and Azure


async def test_openai_gets_a_stable_opaque_prompt_cache_key_per_tenant() -> None:
    recorder = Recorder([_openai_reply("ok", cached=1920)])
    cache = Cache(project="invoices", salt="pepper")
    agent = Agent("clerk", instructions=SYSTEM, model=openai(recorder), cache=cache)

    await agent.run("a", metadata={"awa.tenant": "acme"})
    await agent.run("b", metadata={"awa.tenant": "acme"})
    await agent.run("c", metadata={"awa.tenant": "globex"})

    keys = [body["prompt_cache_key"] for body in recorder.bodies]
    assert keys[0] == keys[1] != keys[2]
    assert all(len(k) == 36 and k.startswith("awa-") for k in keys)
    assert not any(part in keys[0] for part in ("acme", "invoices", "clerk", "pepper"))
    assert not any(_has_cache_control(body) for body in recorder.bodies)
    for body in recorder.bodies:
        assert "prompt_cache_retention" not in body and "prompt_cache_options" not in body


def test_the_key_changes_with_salt_tenant_project_agent_and_prompt() -> None:
    base = {"tenant": "acme", "project": "p", "agent": "a", "prompt_hash": "h"}
    key = prompt_cache_key(b"s", **base)

    assert key == prompt_cache_key(b"s", **base)
    assert prompt_cache_key(b"t", **base) != key
    for part in base:
        assert prompt_cache_key(b"s", **{**base, part: "other"}) != key


# ---------------------------------------------------------------- prefix stability


async def test_tools_are_sent_in_name_order_and_the_prefix_hash_is_recorded() -> None:
    recorder = Recorder([_openai_reply("ok", cached=0)])
    agent = Agent(
        "clerk",
        instructions=SYSTEM + "\r\n",
        model=openai(recorder),
        tools=[lookup_vendor, check_iban],
    )
    seen: list[ModelResponseEvent] = []

    async def collect(event: RunEvent) -> None:
        if isinstance(event, ModelResponseEvent):
            seen.append(event)

    agent2 = agent.clone(hooks=[collect])
    await agent2.run("x")
    await agent2.run("y")

    names = [tool["function"]["name"] for tool in recorder.bodies[0]["tools"]]
    assert names == ["check_iban", "lookup_vendor"]
    assert recorder.bodies[0]["messages"][0]["content"] == SYSTEM.rstrip()
    assert [e.prefix_hash for e in seen] == [seen[0].prefix_hash] * 2
    assert seen[0].prefix_hash is not None and len(seen[0].prefix_hash) == 16
    assert all(e.duration_ms is not None and e.duration_ms >= 0 for e in seen)


def test_the_prefix_hash_ignores_claude_markers_but_not_the_tool_order() -> None:
    from awa._internal.upstream.agents import ModelRequest, anthropic_prompt_caching
    from awa._internal.upstream.messages import SystemMessage
    from awa._internal.upstream.tools import tool as as_tool

    tools = [as_tool(lookup_vendor), as_tool(check_iban)]
    request = ModelRequest(
        model=FakeChatModel(["x"]),
        messages=[],
        system_message=SystemMessage(content="rules"),
        tools=tools,
    )
    mark = anthropic_prompt_caching("5m")
    assert mark is not None
    marked = mark(request)

    assert prefix_hash(marked) == prefix_hash(request)
    assert prefix_hash(request.override(tools=tools[::-1])) != prefix_hash(request)
    assert prefix_hash(stable_request(request.override(tools=tools[::-1]))) == prefix_hash(
        stable_request(request)
    )


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="prompt_cache"):
        PromptCacheMiddleware("a", mode="2h")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="prompt_cache"):
        Agent("a", prompt_cache="always")  # type: ignore[arg-type]


# ---------------------------------------------------------------- padding to the minimum


def _request(system: str | None, *, tools: list[Any] | None = None) -> Any:
    from awa._internal.upstream.agents import ModelRequest
    from awa._internal.upstream.messages import SystemMessage

    return ModelRequest(
        model=FakeChatModel(["x"]),
        messages=[],
        system_message=SystemMessage(content=system) if system is not None else None,
        tools=tools or [],
    )


def test_a_prefix_just_short_of_the_minimum_is_padded_to_it_and_nothing_else() -> None:
    near = _request(NEAR)
    padded = padded_request(near)

    text = padded.system_message.content
    assert text.startswith(NEAR) and "<cache-padding>" in text
    assert estimated_prefix_tokens(near) < OPENAI_MIN_PREFIX_TOKENS
    assert text.count(" .") == PAD_TARGET_TOKENS - estimated_prefix_tokens(near)  # one token each
    assert padded_request(near).system_message.content == text  # the same bytes every time
    for unchanged in (_request(SYSTEM), _request("Be brief."), _request(None)):
        assert padded_request(unchanged) is unchanged


def test_padding_counts_the_tools_and_appends_to_a_list_system_message() -> None:
    from awa._internal.upstream.tools import tool as as_tool

    plain = _request(NEAR)
    with_tools = _request(NEAR, tools=[as_tool(lookup_vendor), as_tool(check_iban)])
    blocks = _request(None).override(
        system_message=plain.system_message.model_copy(
            update={"content": [{"type": "text", "text": NEAR}]}
        )
    )

    assert estimated_prefix_tokens(with_tools) > estimated_prefix_tokens(plain)
    assert len(padded_request(with_tools).system_message.content) < len(
        padded_request(plain).system_message.content
    )
    content = padded_request(blocks).system_message.content
    assert content[0] == {"type": "text", "text": NEAR}
    assert content[1]["text"].startswith("<cache-padding>")


async def test_openai_requests_are_padded_and_claude_requests_never() -> None:
    azure = Recorder([_openai_reply("ok", cached=0)])
    anthropic = Recorder([_claude_reply("ok", write=0, read=0)])

    await Agent("clerk", instructions=NEAR, model=openai(azure)).run("a")
    await Agent("clerk", instructions=NEAR, model=claude(anthropic)).run("a")

    assert "<cache-padding>" in azure.bodies[0]["messages"][0]["content"]
    assert "<cache-padding>" not in json.dumps(anthropic.bodies[0])


@pytest.mark.parametrize(
    "off", [{"cache": Cache(CacheSettings(prompt_pad=False))}, {"prompt_cache": "off"}]
)
async def test_padding_can_be_switched_off(off: dict[str, Any]) -> None:
    recorder = Recorder([_openai_reply("ok", cached=0)])

    await Agent("clerk", instructions=NEAR, model=openai(recorder), **off).run("a")

    assert recorder.bodies[0]["messages"][0]["content"] == NEAR


def test_the_pad_setting_comes_from_the_environment() -> None:
    env = Settings(_env_file=None, cache_prompt_pad=False)

    assert CacheSettings().prompt_pad
    assert not CacheSettings().with_environment(env).prompt_pad


async def test_without_a_salt_every_system_sends_the_same_key() -> None:
    recorder = Recorder([_openai_reply("ok", cached=0)])

    for _ in range(2):  # a system built per run, as `awa eval` and many servers do
        agent = Agent("clerk", instructions=SYSTEM, model=openai(recorder), cache=Cache())
        await agent.run("a")

    assert recorder.bodies[0]["prompt_cache_key"] == recorder.bodies[1]["prompt_cache_key"]


# ---------------------------------------------------------------- recorded usage


def _azure_gpt56_reply(*, prompt: int, cached: int, written: int) -> dict[str, Any]:
    """Usage as Azure OpenAI gpt-5.6 answered it (recorded 2026-10-09, Data Zone Standard)."""

    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5.6-terra-2026-07-09",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK."},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": 4,
            "total_tokens": prompt + 4,
            "prompt_tokens_details": {
                "audio_tokens": 0,
                "cache_write_tokens": written,
                "cached_tokens": cached,
                "image_tokens": None,
                "text_tokens": None,
            },
            "completion_tokens_details": {"audio_tokens": 0, "reasoning_tokens": 0},
        },
    }


async def test_azure_gpt56_cache_writes_and_reads_are_counted_and_priced_apart() -> None:
    # GPT-5.6 bills a cache write (1.25 times the input price); earlier models report none.
    recorder = Recorder(
        [
            _azure_gpt56_reply(prompt=2354, cached=0, written=2351),
            _azure_gpt56_reply(prompt=2370, cached=2351, written=16),
        ]
    )
    agent = Agent("clerk", instructions=SYSTEM, model=openai(recorder))
    prices = PriceTable.from_mapping(  # Azure Data Zone Standard, USD per million tokens
        {
            "gpt-5.4": {
                "input_per_mtok": 2.20,
                "output_per_mtok": 13.20,
                "cache_read_per_mtok": 0.22,
                "cache_write_per_mtok": 2.75,
            }
        }
    )

    first = await agent.run("first")
    result = await agent.run("second")

    assert (first.usage.cache_read_tokens, first.usage.cache_write_tokens) == (0, 2351)
    usage = result.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        2370,
        2351,
        16,
    )
    assert usage.cost(prices) == pytest.approx(
        (3 * 2.20 + 2351 * 0.22 + 16 * 2.75 + 4 * 13.20) / 1_000_000
    )
