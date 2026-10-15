"""Blueprint models for the defaults and the agents (data only)."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, Literal, TypeAlias

from pydantic import ConfigDict, Field, field_validator, model_validator

from awa._internal.upstream.tools import BaseTool
from awa.blueprints._schema_refine import DescribeBlueprint, RefineBlueprint, Refinement
from awa.blueprints._schema_spec import EscalationBlueprint, SystemSections, _Spec
from awa.models import ModelSettings
from awa.runtime.cache import CacheSettings
from awa.skills import SkillRegistry

ToolSource: TypeAlias = (
    Mapping[str, BaseTool | Callable[..., Any]] | Sequence[BaseTool | Callable[..., Any]]
)
"""Tools a blueprint's tool names resolve against: a name mapping or a list."""

SkillSource: TypeAlias = SkillRegistry | Collection[str]
"""Skills a blueprint's skill names are checked against: a registry or a set of names."""

Role = Literal["orchestrator", "worker", "judge", "reviewer"]
"""What an agent is in a role tree: `orchestrator` plans and delegates, `worker` does the work,
`judge` gates one worker's output with a typed verdict, `reviewer` reads finished work once."""

HandoffHistoryMode = Literal["filtered", "full"]
"""What a handoff target sees of the conversation; see `awa.team.HandoffHistory`."""


class BlueprintModelSettings(ModelSettings):
    """`ModelSettings` that reject unknown keys, so a typo in a blueprint is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class BlueprintDefaults(_Spec):
    """Settings every agent inherits unless it overrides them.

    Attributes:
        model: Model profile name or `provider:model` string; `None` uses the `default` profile.
        model_settings: Construction settings for models given as a string.
        max_steps: Maximum model calls per agent run.
        skill_mode: `inline` puts the full skill text in the prompt; `on_demand` keeps name
            and description and loads the body with `load_skill`.
        tool_timeout: Seconds a call of the blueprint's own tools may take unless the tool
            sets its own timeout; `None` disables the limit.
        cache: Cache settings (`awa.runtime.CacheSettings`); `AWA_CACHE_*` variables win.
    """

    model: str | None = None
    model_settings: BlueprintModelSettings | None = None
    max_steps: int = Field(default=25, ge=1)

    skill_mode: Literal["inline", "on_demand"] = "inline"
    tool_timeout: float | None = Field(default=120.0, gt=0)
    cache: CacheSettings = Field(default_factory=CacheSettings)


class DelegationBlueprint(_Spec):
    """Limits of the nested team an agent with `sub_agents` compiles to (defaults: depth 3, 20 at once)."""

    max_depth: int | None = Field(default=None, ge=1, le=10)
    max_parallel: int | None = Field(default=None, ge=1)


class AgentMemoryBlueprint(_Spec):
    """Long-term memory of an agent; see `awa.memory.AgentMemory`.

    Attributes:
        auto_recall: Memories recalled per run into a context message; 0 disables it.
        require_user: Refuse runs without a user id (every scope but `"global"`).
        user_id: Where a served system gets the run's user id (`deps["user_id"]`):
            `"principal"` (the authenticated caller) or `"deps"` (sent by the client).
    """

    scope: Literal["agent", "user", "team", "global"] = "agent"
    auto_recall: int = Field(default=0, ge=0)
    require_user: bool = False
    user_id: Literal["principal", "deps"] | None = None


class CompactionBlueprint(_Spec):
    """Context compaction of an agent: summarize older turns beyond `max_tokens`; see `awa.memory.Compaction`."""

    max_tokens: int = Field(default=8000, ge=1)
    keep_last_messages: int = Field(default=20, ge=1)


class AgentBlueprint(_Spec):
    """One role of the process.

    Attributes:
        description: What the agent is responsible for; collaborators read it when deciding
            whom to involve.
        instructions: The system prompt; when empty, the description instructs the agent.
        tools: Tool names, `"package.module:attribute"` imports (only with
            `allow_imports=True`), `"mcp:<server>[/<tool>]"`, `"builtin:<name>"` (configured
            in `toolkits:`) or `"connector:<name>[.<operation>]"` (configured in `connectors:`;
            without an operation its read tools).
        capabilities: Capability tags published on the agent's card.
        output_schema: JSON schema of a structured output; the output is then validated data.
        memory: Long-term memory tools; agents with the same scope share one memory.
        auto: Match builtin skills and tools to the task; `None` follows the blueprint.
        role: The agent's place in a role tree; `None` is a plain agent.
        sub_agents: The agents directly below this one; the agent compiles to a team it leads.
            A sub-agent inherits nothing from it.
        rubric: The criteria a judge checks (required) or a reviewer reports on; a judge
            answers per criterion `c1`, `c2`, ... in order.
        judges: Judge only: the worker whose output it gates.
        reviews: Reviewer only: the worker whose finished output it reads once. Without it,
            the orchestrator calls the reviewer itself.
        max_iterations: How many times a judge may send a worker's output back (default 3).
        delegation: Depth and parallelism limits of the agent's sub-agents.
        documents: Names of `data:` entries the agent always has, listed in its instructions.
        goal: One sentence: what the agent achieves. With `goal` or `system`, the system
            message is rendered from sections (see `render_system_message`).
        system: The sections of the system message.
        escalation: Where and when the agent hands a case to a person or the orchestrator.
        acknowledge_ungated: Acting tools that run without approval on purpose (lint warns
            instead of failing).
        describe: Texts kept in files and the agent's own tool descriptions.
        refine: Provider fields and middleware applied per model attempt.
        refinements: Lines the post-build refinement added, each with its evidence.
    """

    name: str
    description: str = ""
    instructions: str = ""
    model: str | None = None
    model_settings: BlueprintModelSettings | None = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    skill_mode: Literal["inline", "on_demand"] | None = None
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    max_steps: int | None = Field(default=None, ge=1)
    memory: AgentMemoryBlueprint | None = None
    compaction: CompactionBlueprint | None = None
    auto: bool | None = None
    role: Role | None = None
    sub_agents: list[str] = Field(default_factory=list)
    rubric: list[str] = Field(default_factory=list)
    judges: str | None = None
    reviews: str | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=10)
    delegation: DelegationBlueprint | None = None
    documents: list[str] = Field(default_factory=list)
    goal: str = Field(default="", max_length=500)
    system: SystemSections | None = None
    escalation: EscalationBlueprint | None = None
    acknowledge_ungated: list[str] = Field(default_factory=list)
    describe: DescribeBlueprint | None = None
    refine: RefineBlueprint | None = None
    refinements: list[Refinement] = Field(default_factory=list, max_length=3)

    @field_validator("rubric", mode="before")
    @classmethod
    def _one_criterion_as_text(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _one_instructions_source(self) -> AgentBlueprint:
        if self.describe is not None and self.describe.instructions_file and self.instructions:
            raise ValueError("give either 'instructions' or 'describe.instructions_file', not both")
        return self

    @field_validator("output_schema")
    @classmethod
    def _json_schema_values(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        # A JSON schema is JSON: infinity and NaN cannot be written to a blueprint file.
        if value is not None:
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"output_schema must contain only JSON values: {exc}") from None
        return value
