"""Agent cards: the public, machine-readable description of what an agent does.

An `AgentCard` is what the `AgentDirectory` stores, what other agents read to decide whom to
delegate to, and what a UI or an audit log shows (in the spirit of the A2A agent card).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
"""Agent names end tool names (`transfer_to_<name>`), so they keep providers' tool name rules."""


def validate_agent_name(name: str) -> str:
    """Return `name` if it is a valid agent name, else raise `ValueError`.

    A valid name starts with a letter, holds only letters, digits, `_` and `-`, and has at most
    48 characters.
    """

    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid agent name {name!r}: must start with a letter, contain only "
            "letters, digits, '_' or '-', and be at most 48 characters long."
        )
    return name


class AgentStatus(StrEnum):
    """Runtime availability of an agent, as tracked by the `AgentDirectory`."""

    AVAILABLE = "available"
    BUSY = "busy"
    OFFLINE = "offline"


class AgentCard(BaseModel):
    """Static, declarative description of one agent.

    Attributes:
        description: One or two sentences on what the agent is responsible for. Other agents
            read it to decide whether to delegate, so it says *when* to use the agent.
        capabilities: Free-form tags such as `"research"` or `"billing.refunds"`, for
            capability-based lookup.
        model: Human-readable identifier of the model the agent runs on.
        role: Optional role label within a team, e.g. `"orchestrator"`.
        examples: Example requests the agent handles well, mapped to A2A skill examples.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    capabilities: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    model: str | None = None
    role: str | None = None
    team: str | None = None
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    version: str = "1.0.0"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return validate_agent_name(value)

    def summary(self) -> str:
        """Return a one-line summary suitable for a system prompt or tool description."""

        parts = [f"{self.name}: {self.description or 'no description'}"]
        if self.capabilities:
            parts.append(f"capabilities: {', '.join(self.capabilities)}")
        if self.skills:
            parts.append(f"skills: {', '.join(self.skills)}")
        return " | ".join(parts)


__all__ = ["AGENT_NAME_PATTERN", "AgentCard", "AgentStatus", "validate_agent_name"]
