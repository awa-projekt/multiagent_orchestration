"""awa: AI agents for business processes, with hard limits enforced in code, approvals before gated actions, eval cases, and an exit to plain LangGraph for single agents.

The top level holds the everyday names (agents, teams, runs, blueprints, the main errors), loaded
on first use. Each concept has one subpackage for the rest: `awa.tools`, `awa.toolkits`,
`awa.connectors`, `awa.mcp`, `awa.skills`, `awa.guardrails`, `awa.server` and more.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awa import quick
    from awa.agents import Agent, AgentHooks
    from awa.blueprints import Blueprint, aload, load
    from awa.contracts import AgentLike, RunEvent, RunResult, UsageLimits
    from awa.core.settings import Settings
    from awa.directory import AgentDirectory
    from awa.errors import (
        AgentRunError,
        AwaError,
        BlueprintError,
        GuardrailTripwireError,
        RunCancelledError,
        RunTimeoutError,
        UsageLimitExceededError,
    )
    from awa.guardrails import Guardrail
    from awa.memory import AgentMemory
    from awa.messaging import Blackboard
    from awa.models import ModelSettings
    from awa.project_setup import setup
    from awa.runtime import RunContext, current_run_context
    from awa.skills import Skill, SkillRegistry
    from awa.tasks import TaskBoard
    from awa.team import AgentNetwork, NetworkLimits, Team, TeamLimits
    from awa.tools import tool
    from awa.workflows import Loop, Parallel, Sequential

    __version__: str


__all__ = [
    "Agent",
    "AgentHooks",
    "tool",
    "Skill",
    "SkillRegistry",
    "AgentMemory",
    "Guardrail",
    "ModelSettings",
    "Team",
    "TeamLimits",
    "AgentNetwork",
    "NetworkLimits",
    "Sequential",
    "Parallel",
    "Loop",
    "Blackboard",
    "TaskBoard",
    "AgentDirectory",
    "RunContext",
    "current_run_context",
    "RunResult",
    "RunEvent",
    "UsageLimits",
    "AgentLike",
    "Settings",
    "setup",
    "Blueprint",
    "load",
    "aload",
    "quick",
    "AwaError",
    "AgentRunError",
    "UsageLimitExceededError",
    "GuardrailTripwireError",
    "RunCancelledError",
    "RunTimeoutError",
    "BlueprintError",
    "__version__",
]


def _homes(module: str, *names: str) -> dict[str, str]:
    return dict.fromkeys(names, module)


# Public name -> the module that defines or re-exports it (a module named like
# the attribute, `awa.quick`, is the attribute itself).
_LAZY: dict[str, str] = {
    **_homes("awa.agents", "Agent", "AgentHooks"),
    **_homes("awa.tools", "tool"),
    **_homes("awa.skills", "Skill", "SkillRegistry"),
    **_homes("awa.memory", "AgentMemory"),
    **_homes("awa.guardrails", "Guardrail"),
    **_homes("awa.models", "ModelSettings"),
    **_homes("awa.team", "Team", "TeamLimits", "AgentNetwork", "NetworkLimits"),
    **_homes("awa.workflows", "Sequential", "Parallel", "Loop"),
    **_homes("awa.messaging", "Blackboard"),
    **_homes("awa.tasks", "TaskBoard"),
    **_homes("awa.directory", "AgentDirectory"),
    **_homes("awa.runtime", "RunContext", "current_run_context"),
    **_homes("awa.contracts", "RunResult", "RunEvent", "UsageLimits", "AgentLike"),
    **_homes("awa.core.settings", "Settings"),
    **_homes("awa.project_setup", "setup"),
    **_homes("awa.blueprints", "Blueprint", "load", "aload"),
    **_homes(
        "awa.errors",
        "AwaError",
        "AgentRunError",
        "UsageLimitExceededError",
        "GuardrailTripwireError",
        "RunCancelledError",
        "RunTimeoutError",
        "BlueprintError",
    ),
    "quick": "awa.quick",
}


def __getattr__(name: str) -> Any:
    """Import a public name on first access and cache it in the module namespace."""

    value: Any
    if name == "__version__":
        from awa._internal.version import __version__ as value
    elif name in _LAZY:
        module_name = _LAZY[name]
        module = importlib.import_module(module_name)
        value = module if module_name == f"{__name__}.{name}" else getattr(module, name)
    else:
        import difflib

        matches = difflib.get_close_matches(name, __all__, n=3)
        hint = f"; did you mean {', '.join(repr(m) for m in matches)}?" if matches else ""
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}{hint}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
