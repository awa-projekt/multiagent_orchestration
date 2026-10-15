"""Limits of team and network runs, applied by the team code."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from awa.contracts.usage import UsageLimits
from awa.runtime.termination import ceilings


class TeamLimits(BaseModel):
    """Limits the team code applies to a run (the defaults are listed below); models only choose within them.

    Every counter is scoped to one team run. Delegations and peer consultations count alike. A
    rejected delegation or transfer is reported to the calling model as tool text.

    Attributes:
        max_delegation_depth: Maximum length of a delegate/consult chain, counted from the agent
            holding control (its delegations have depth 1).
        max_parallel_delegations: Maximum delegations running at once, foreground and background;
            a consultation inside a delegation shares its caller's slot.
        max_calls_per_member: Maximum delegations and consultations each member receives; `None`
            for no limit.
        delegation_timeout: Seconds a single delegation may run; `None` for no limit.
        max_handoffs: Transfers of control in one team run.
        max_result_chars: Longer delegated results are truncated; with a blackboard the full result
            is stored there and the tool result names its key.
        max_background_tasks: Background delegations open at once.
        max_handback_rounds: Extra turns the agent holding control gets after it finished, to
            process results of still outstanding background delegations.
        allow_cycles: `True` refuses only a call that repeats a member with the same brief further
            up the chain; `False` refuses every member already on the chain.
        delegation_limits: Usage limits of every single delegation or consultation, everything it
            starts included. An exceeding delegation stops alone with a `stopped:budget` result.
            `None` gives `UsageLimits(max_requests=AWA_DELEGATION_MAX_REQUESTS)` (50); there is no
            unlimited delegation.
        tool_limits: Maximum calls per tool for single members, by member name, for example
            `{"researcher": {"web_search": 2}}`, counted per run of the member.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_delegation_depth: int = Field(default=4, ge=1)
    max_parallel_delegations: int = Field(default=8, ge=1)
    max_calls_per_member: int | None = Field(default=20, ge=1)
    delegation_timeout: float | None = Field(default=300.0, gt=0)
    max_handoffs: int = Field(default=5, ge=0)
    max_result_chars: int = Field(default=12_000, ge=1)
    max_background_tasks: int = Field(default=8, ge=0)
    max_handback_rounds: int = Field(default=3, ge=0)
    allow_cycles: bool = True
    delegation_limits: UsageLimits | None = None
    tool_limits: dict[str, dict[str, Annotated[int, Field(ge=0)]]] = Field(default_factory=dict)

    def delegation_budget(self) -> UsageLimits:
        """The usage limits of one delegation: `delegation_limits`, or the default budget."""

        if self.delegation_limits is not None:
            return self.delegation_limits
        return UsageLimits(max_requests=ceilings().delegation_max_requests)


class NetworkLimits(BaseModel):
    """Limits of one network run, applied by the network code (the defaults are listed below).

    Attributes:
        max_tasks: Maximum tasks created during a run; a `create_tasks` batch beyond it is refused
            as a whole.
        max_concurrent_teammates: Maximum teammates working at once; `None` for all.
        max_task_attempts: Attempts before a task's failure is final and reported to the lead.
        max_lead_turns: Maximum lead runs, including the planning and the final-answer turn.
        max_message_turns: Maximum runs per teammate started only to handle messages.
        idle_timeout: Seconds to wait for progress on open tasks nobody in the network can work on
            before asking the lead for the final answer.
        run_timeout: Seconds the work phase may take; then the lead answers from the results so
            far. `None` for no limit.
        max_result_chars: Results are shortened to this in handbacks and briefs; the board keeps
            the full text.
        handback_chars: A longer completed result reaches the lead as its first
            `HANDBACK_SUMMARY_CHARS` characters and its task id; `{{task:<id>}}` in the final answer
            is replaced with the full result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tasks: int = Field(default=50, ge=1)
    max_concurrent_teammates: int | None = Field(default=None, ge=1)
    max_task_attempts: int = Field(default=2, ge=1)
    max_lead_turns: int = Field(default=12, ge=1)
    max_message_turns: int = Field(default=5, ge=0)
    idle_timeout: float = Field(default=30.0, gt=0)
    run_timeout: float | None = Field(default=1800.0, gt=0)
    max_result_chars: int = Field(default=12_000, ge=1)
    handback_chars: int = Field(default=1_500, ge=1)


HANDBACK_SUMMARY_CHARS = 400
"""How much of a result passed by reference the lead sees (see `NetworkLimits.handback_chars`)."""


__all__ = ["HANDBACK_SUMMARY_CHARS", "NetworkLimits", "TeamLimits"]
