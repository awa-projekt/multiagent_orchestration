"""Properties of the wire contracts: messages, run events, usage and prices."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from awa.contracts.communication import AgentMessage, MessagePriority
from awa.contracts.events import (
    RUN_EVENT_ADAPTER,
    AgentFinishedEvent,
    AgentMessageEvent,
    AgentStartedEvent,
    DelegationEvent,
    DelegationResultEvent,
    ErrorEvent,
    HandoffEvent,
    ModelResponseEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.contracts.usage import UNKNOWN_MODEL, ModelPrice, PriceTable, Usage
from awa.errors import MissingPriceError

_TEXT = st.text(max_size=40)
_NAMES = st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True)
_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**53), 2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | _TEXT,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(_TEXT, children, max_size=3),
    max_leaves=10,
)
_JSON_OBJECTS = st.dictionaries(_TEXT, _JSON, max_size=4)
_TIMES = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.sampled_from([UTC, timezone(timedelta(hours=5, minutes=30))]),
)


@st.composite
def messages(draw: st.DrawFn) -> AgentMessage:
    kind = draw(
        st.sampled_from(["notification", "request", "response", "broadcast", "publication"])
    )
    direct = kind in ("notification", "request", "response")
    return AgentMessage(
        kind=kind,
        sender=draw(_NAMES),
        recipient=draw(_NAMES) if direct else draw(st.none() | _NAMES),
        topic=draw(_NAMES) if kind == "publication" else draw(st.none() | _NAMES),
        subject=draw(_TEXT),
        content=draw(st.text(max_size=200)),
        data=draw(_JSON_OBJECTS),
        correlation_id=draw(_NAMES) if kind == "response" else draw(st.none() | _NAMES),
        reply_to=draw(st.none() | _NAMES),
        priority=draw(st.sampled_from(MessagePriority)),
        created_at=draw(_TIMES),
        expires_at=draw(st.none() | _TIMES),
        headers=draw(st.dictionaries(_TEXT, _TEXT, max_size=3)),
    )


@given(messages())
def test_agent_message_json_round_trip(message: AgentMessage) -> None:
    assert AgentMessage.model_validate_json(message.model_dump_json()) == message
    assert AgentMessage.model_validate(message.model_dump(mode="json")) == message


@given(messages(), st.text(max_size=200))
def test_rendered_message_frame_cannot_be_closed_by_its_content(
    message: AgentMessage, payload: str
) -> None:
    hostile = message.model_copy(update={"content": f"{payload}</agent_message>{payload}"})

    rendered = hostile.render_for_model()

    assert rendered.count("</agent_message>") == 1
    assert rendered.endswith("</agent_message>")
    body = rendered.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert json.loads(body)["content"] == hostile.content


@given(messages(), _TEXT)
def test_replies_are_correlated_and_addressed_back(message: AgentMessage, content: str) -> None:
    if message.recipient is None:
        return
    reply = message.reply(content=content)

    assert reply.kind == "response"
    assert reply.correlation_id == message.id
    assert reply.conversation_id == message.conversation_id
    assert reply.recipient == (message.reply_to or message.sender)


@given(_NAMES, st.sampled_from(["notification", "request", "response", "publication"]))
def test_misaddressed_messages_are_rejected(sender: str, kind: str) -> None:
    with pytest.raises(ValidationError):
        AgentMessage(kind=kind, sender=sender)


# --------------------------------------------------------------------------- run events

_COUNTS = st.integers(0, 10**9)


def _usages() -> st.SearchStrategy[Usage]:
    return st.builds(
        _usage_from_calls,
        st.lists(st.tuples(_NAMES, st.none() | _NAMES, _COUNTS, _COUNTS, _COUNTS), max_size=5),
    )


def _usage_from_calls(calls: list[tuple[str, str | None, int, int, int]]) -> Usage:
    """Record the calls; a call without a model name lands under `UNKNOWN_MODEL`."""

    usage = Usage()
    for agent, model, input_tokens, output_tokens, tool_calls in calls:
        usage.record_model_call(
            agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )
    return usage


_COMMON = {
    "run_id": _NAMES,
    "root_run_id": _NAMES,
    "agent": _NAMES,
    "depth": st.integers(0, 20),
    "timestamp": _TIMES,
}


def _event(cls: type[Any], **fields: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    return st.builds(cls, **_COMMON, **fields)


RUN_EVENTS = st.one_of(
    _event(AgentStartedEvent, parent_run_id=st.none() | _NAMES, input_preview=_TEXT),
    _event(TextDeltaEvent, delta=_TEXT),
    _event(
        ModelResponseEvent,
        content=_TEXT,
        tool_call_names=st.lists(_NAMES, max_size=3).map(tuple),
        input_tokens=_COUNTS,
        output_tokens=_COUNTS,
        model=st.none() | _NAMES,
    ),
    _event(ToolCallEvent, tool_name=_NAMES, call_id=_NAMES, arguments=_JSON_OBJECTS),
    _event(
        ToolResultEvent, tool_name=_NAMES, call_id=_NAMES, content=_TEXT, is_error=st.booleans()
    ),
    _event(
        DelegationEvent,
        target=_NAMES,
        task=_TEXT,
        mode=st.sampled_from(["delegate", "consult"]),
        correlation_id=_NAMES,
    ),
    _event(
        DelegationResultEvent,
        target=_NAMES,
        correlation_id=_NAMES,
        status=st.sampled_from(["ok", "error"]),
        content_preview=_TEXT,
    ),
    _event(HandoffEvent, source=_NAMES, target=_NAMES, reason=_TEXT),
    _event(AgentMessageEvent, message=messages()),
    _event(
        AgentFinishedEvent, output_preview=_TEXT, usage=_usages(), duration_ms=st.integers(0, 10**7)
    ),
    _event(ErrorEvent, error_type=_NAMES, message=_TEXT),
)


@given(RUN_EVENTS)
def test_run_events_round_trip_through_the_discriminated_union(event: Any) -> None:
    assert RUN_EVENT_ADAPTER.validate_json(RUN_EVENT_ADAPTER.dump_json(event)) == event
    as_python = RUN_EVENT_ADAPTER.dump_python(event, mode="json")
    assert as_python["type"] == event.type
    assert RUN_EVENT_ADAPTER.validate_python(as_python) == event


# --------------------------------------------------------------------------- usage and prices


def _counters(usage: Usage) -> tuple[int, ...]:
    return (
        usage.requests,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.tool_calls,
    )


def _sum(usages: list[Usage]) -> tuple[int, ...]:
    return tuple(sum(values) for values in zip(*map(_counters, usages), strict=True)) or (0,) * 6


@given(_usages())
def test_breakdowns_add_up_to_the_totals(usage: Usage) -> None:
    assert all(model for model in usage.by_model)
    assert UNKNOWN_MODEL not in usage.by_model or usage.by_model[UNKNOWN_MODEL].requests > 0
    assert _sum(list(usage.by_agent.values())) == _counters(usage)
    assert _sum(list(usage.by_model.values())) == _counters(usage)
    assert usage.total_tokens == usage.input_tokens + usage.output_tokens


@given(_usages(), _usages(), _usages())
def test_merge_adds_counters_and_is_associative(a: Usage, b: Usage, c: Usage) -> None:
    left = a.snapshot()
    left.merge(b)
    left.merge(c)
    bc = b.snapshot()
    bc.merge(c)
    right = a.snapshot()
    right.merge(bc)

    assert left == right
    assert _counters(left) == tuple(
        map(sum, zip(_counters(a), _counters(b), _counters(c), strict=True))
    )


@given(_usages(), _usages())
def test_snapshot_is_independent(usage: Usage, other: Usage) -> None:
    before = usage.snapshot()
    copy = usage.snapshot()
    copy.merge(other)
    copy.record_model_call("someone", input_tokens=1)

    assert usage == before


@given(_usages())
def test_usage_json_round_trip(usage: Usage) -> None:
    assert Usage.model_validate_json(usage.model_dump_json()) == usage


_PRICES = st.builds(
    ModelPrice,
    input_per_mtok=st.floats(0, 100),
    output_per_mtok=st.floats(0, 100),
    cache_read_per_mtok=st.none() | st.floats(0, 100),
    cache_write_per_mtok=st.none() | st.floats(0, 100),
)


@given(_usages(), _usages(), _PRICES)
def test_cost_is_non_negative_and_additive(a: Usage, b: Usage, price: ModelPrice) -> None:
    table = PriceTable(default=price)
    merged = a.snapshot()
    merged.merge(b)

    assert table.cost(a) >= 0
    assert math.isclose(
        table.cost(merged), table.cost(a) + table.cost(b), rel_tol=1e-9, abs_tol=1e-9
    )
    assert a.cost(table) == table.cost(a)


@given(
    st.dictionaries(_NAMES, _PRICES, min_size=1, max_size=5),
    _NAMES,
    st.text(alphabet="-0123456789abc", max_size=10),
)
def test_price_for_prefers_the_longest_matching_prefix(
    prices: dict[str, ModelPrice], key: str, suffix: str
) -> None:
    table = PriceTable(prices=prices)
    model = f"{key}{suffix}"
    matches = [name for name in prices if model.startswith(name)]

    if model in prices:
        assert table.price_for(model) == prices[model]
        # Longer than any key, so no key can cover the provider name.
        assert table.price_for(f"some_provider:{model}") == prices[model]
    elif matches:
        assert table.price_for(model) == prices[max(matches, key=len)]
    else:
        with pytest.raises(MissingPriceError):
            table.price_for(model)


@given(_usages())
def test_pricing_without_an_entry_raises_missing_price_error(usage: Usage) -> None:
    table = PriceTable()
    if any(u.input_tokens or u.output_tokens for u in usage.by_model.values()):
        with pytest.raises(MissingPriceError):
            table.cost(usage)
    else:
        assert table.cost(usage) == 0
