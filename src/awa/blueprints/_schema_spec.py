"""Optional agent-spec fields of an agent entry: goal, system sections and escalation.

With `goal` or `system`, the system message is rendered from the sections in a fixed order
instead of being the free text of `instructions`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_EXAMPLES = 3
"""Most examples one system message carries."""

SECTION_NAMES = (
    "context",
    "how_to_work",
    "tools_in_step",
    "done_when",
    "rules",
    "special_cases",
    "output",
    "examples",
    "delegation_brief",
)
"""The sections of `system:`, in the order the system message renders them."""


class _Spec(BaseModel):
    """Base of every blueprint section: an unknown key is an error, not ignored."""

    model_config = ConfigDict(extra="forbid")


class SpecExample(_Spec):
    """One example of the system message: an input, the expected output and why."""

    input: str = Field(min_length=1, max_length=2000)
    output: str = Field(min_length=1, max_length=2000)
    why: str = Field(default="", max_length=300)


class SystemSections(_Spec):
    """The sections of an agent's system message (`system:`); each is optional.

    Attributes:
        tools_in_step: When to use which of the agent's tools in this step (not a tool manual).
        done_when: What must hold before the agent answers.
        rules: Rules, each with its reason (`"..., because ..."`).
        special_cases: Extra special-case lines beyond those generated from the process.
        output: What the answer must contain; `auto` renders the field notes of `output_schema`.
        delegation_brief: Orchestrator only: what every brief to a member states.
    """

    context: str = Field(default="", max_length=4000)
    how_to_work: str = Field(default="", max_length=6000)
    tools_in_step: str = Field(default="", max_length=3000)
    done_when: str = Field(default="", max_length=2000)
    rules: list[str] = Field(default_factory=list, max_length=30)
    special_cases: list[str] = Field(default_factory=list, max_length=20)
    output: str = Field(default="", max_length=4000)
    examples: list[SpecExample] = Field(default_factory=list, max_length=MAX_EXAMPLES)
    delegation_brief: str = Field(default="", max_length=4000)

    @field_validator("rules", "special_cases", mode="before")
    @classmethod
    def _one_line_as_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    def section(self, name: str) -> Any:
        """The value of section `name` (one of `SECTION_NAMES`)."""

        if name not in SECTION_NAMES:
            raise KeyError(name)
        return getattr(self, name)


class EscalationBlueprint(_Spec):
    """Where (`person` or `orchestrator`) and when the agent hands a case it must not decide itself."""

    to: Literal["person", "orchestrator"] = "person"
    when: list[str] = Field(default_factory=list, max_length=20)
    how: str = Field(default='status "escalate" with a reason', max_length=300)

    @field_validator("when", mode="before")
    @classmethod
    def _one_line_as_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value
