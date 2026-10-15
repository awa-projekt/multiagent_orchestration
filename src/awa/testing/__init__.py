"""Interfaces layer: offline test doubles for systems built on awa.

Scripted chat models (`FakeChatModel`, `FunctionChatModel`, `tool_call`, `structured_output`,
`expect_scripts_consumed`),
`ToolContract`, `assert_terminates_via_own_budget`, the port doubles `FakeClock`, `FixedIds`,
`FakeTransport`, and `replay_models`.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute
from awa.testing.doubles import FakeClock, FixedIds
from awa.testing.models import (
    FakeChatModel,
    FunctionChatModel,
    ModelCall,
    ScriptItem,
    expect_scripts_consumed,
    structured_output,
    tool_call,
)

if TYPE_CHECKING:
    from awa.connectors import MockTransport as FakeTransport
    from awa.testing.mas_scenarios import assert_terminates_via_own_budget
    from awa.testing.replay import replay_models
    from awa.testing.tool_contract import ToolApprovalRequested, ToolContract

_HOMES = homes(
    {
        "awa.testing.mas_scenarios": "assert_terminates_via_own_budget",
        "awa.testing.replay": "replay_models",
        "awa.testing.tool_contract": "ToolApprovalRequested ToolContract",
    }
)


def __getattr__(name: str) -> Any:
    # `FakeTransport` is `awa.connectors.MockTransport` (pass it as `transport=` to
    # `connector_tools`). It and the helpers that build graphs or open stores load on first use.
    if name == "FakeTransport":
        from awa.connectors import MockTransport

        return MockTransport
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


__all__ = [
    "FakeChatModel",
    "FakeClock",
    "FakeTransport",
    "FixedIds",
    "FunctionChatModel",
    "ModelCall",
    "ScriptItem",
    "ToolApprovalRequested",
    "ToolContract",
    "assert_terminates_via_own_budget",
    "expect_scripts_consumed",
    "replay_models",
    "structured_output",
    "tool_call",
]
