"""Build tools and control their calls: approval, risk, timeout, retries, idempotency, policy.

Entry points: `tool` / `tool_method`, `Toolbox` and `ToolPolicyMiddleware`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa.tools.approval import (
        DEFAULT_APPROVAL_NAMESPACE,
        ApprovalCondition,
        create_tool_approval_wrapper,
        default_approval_payload,
    )
    from awa.tools.decorator import ToolSpec, current_idempotency_key, tool, tool_method, tool_spec
    from awa.tools.hardening import OnError, frame_tool_output, harden_tool
    from awa.tools.idempotency import (
        IdempotencyKeyAlreadyClaimedError,
        IdempotencyStore,
        InMemoryIdempotencyStore,
        run_idempotent,
    )
    from awa.tools.policy import (
        Allow,
        Ask,
        Deny,
        PolicyDecision,
        Rewrite,
        ToolCallInfo,
        ToolPolicy,
        ToolPolicyMiddleware,
        ask_for_risk,
        deny_tools,
    )
    from awa.tools.risk import ToolRisk, declared_tool_risk, infer_tool_risk, mark_tool_risk
    from awa.tools.toolset import Toolbox, env_field

_HOMES = homes(
    {
        "awa.tools.approval": (
            "DEFAULT_APPROVAL_NAMESPACE ApprovalCondition create_tool_approval_wrapper"
            " default_approval_payload"
        ),
        "awa.tools.decorator": "ToolSpec current_idempotency_key tool tool_method tool_spec",
        "awa.tools.hardening": "OnError frame_tool_output harden_tool",
        "awa.tools.idempotency": (
            "IdempotencyKeyAlreadyClaimedError IdempotencyStore InMemoryIdempotencyStore"
            " run_idempotent"
        ),
        "awa.tools.policy": (
            "Allow Ask Deny PolicyDecision Rewrite ToolCallInfo ToolPolicy ToolPolicyMiddleware"
            " ask_for_risk deny_tools"
        ),
        "awa.tools.risk": "ToolRisk declared_tool_risk infer_tool_risk mark_tool_risk",
        "awa.tools.toolset": "Toolbox env_field",
    }
)

__all__ = [
    "Allow",
    "ApprovalCondition",
    "Ask",
    "DEFAULT_APPROVAL_NAMESPACE",
    "Deny",
    "IdempotencyKeyAlreadyClaimedError",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "OnError",
    "PolicyDecision",
    "Rewrite",
    "ToolCallInfo",
    "ToolPolicy",
    "ToolPolicyMiddleware",
    "ToolRisk",
    "ToolSpec",
    "Toolbox",
    "ask_for_risk",
    "create_tool_approval_wrapper",
    "current_idempotency_key",
    "declared_tool_risk",
    "default_approval_payload",
    "deny_tools",
    "env_field",
    "frame_tool_output",
    "harden_tool",
    "infer_tool_risk",
    "mark_tool_risk",
    "run_idempotent",
    "tool",
    "tool_method",
    "tool_spec",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
