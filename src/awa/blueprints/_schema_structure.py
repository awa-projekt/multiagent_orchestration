"""Blueprint models for teams, networks and workflows (data only)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal, TypeAlias

from pydantic import Field

from awa.blueprints._schema_agent import HandoffHistoryMode
from awa.blueprints._schema_spec import _Spec

Structure = Literal["team", "workflow", "network"]
"""The collaboration structures a blueprint can declare, one of each at most."""


class TeamLimitsBlueprint(_Spec):
    """Deterministic limits of a team run; unset fields keep the team defaults.

    Attributes:
        max_delegation_depth: Maximum length of a delegate/consult chain.
        max_calls_per_member: Maximum delegations each member receives.
        max_handoffs: Maximum transfers of control per run.
        max_handback_rounds: Extra turns the agent holding control gets for background
            results still outstanding.
        tool_limits: Maximum calls per tool and member run, e.g. `{researcher: {web_search: 2}}`.
    """

    max_delegation_depth: int | None = Field(default=None, ge=1)
    max_parallel_delegations: int | None = Field(default=None, ge=1)
    max_calls_per_member: int | None = Field(default=None, ge=1)
    delegation_timeout: float | None = Field(default=None, gt=0)
    max_handoffs: int | None = Field(default=None, ge=0)
    max_result_chars: int | None = Field(default=None, ge=1)
    max_background_tasks: int | None = Field(default=None, ge=0)
    max_handback_rounds: int | None = Field(default=None, ge=0)
    tool_limits: dict[str, dict[str, int]] | None = None


class TeamBlueprint(_Spec):
    """A team: an orchestrator delegating to members that collaborate directly.

    Attributes:
        members: Agents, or the blueprint's workflow or network by name (each acts as one
            member that can only be delegated to).
        delegates: Who may delegate to whom; `None` lets the orchestrator delegate to every member.
        peers: Consult/message edges between members.
        handoffs: Transfer-of-control edges (`transfer_to_<agent>` tools).
        blackboard: Whether the team shares a versioned key-value workspace.
        task_board: Whether the team shares a `TaskBoard` to plan and track the work.
    """

    name: str | None = None
    description: str = ""
    orchestrator: str
    members: list[str]
    delegates: dict[str, list[str]] | None = None
    peers: dict[str, list[str]] = Field(default_factory=dict)
    symmetric_peers: bool = True
    handoffs: dict[str, list[str]] = Field(default_factory=dict)
    handoff_history: HandoffHistoryMode = "filtered"
    messaging: bool = True
    blackboard: bool = False
    task_board: bool = False
    limits: TeamLimitsBlueprint | None = None


class NetworkLimitsBlueprint(_Spec):
    """Limits of a network run; unset fields keep the defaults of `awa.team.NetworkLimits`.

    Attributes:
        max_message_turns: Runs per teammate started only to handle messages.
        idle_timeout: Seconds to wait for progress nobody in the network can make.
        run_timeout: Seconds the work phase may take.
        max_result_chars: Results longer than this are shortened in handbacks.
    """

    max_tasks: int | None = Field(default=None, ge=1)
    max_concurrent_teammates: int | None = Field(default=None, ge=1)
    max_task_attempts: int | None = Field(default=None, ge=1)
    max_lead_turns: int | None = Field(default=None, ge=1)
    max_message_turns: int | None = Field(default=None, ge=0)
    idle_timeout: float | None = Field(default=None, gt=0)
    run_timeout: float | None = Field(default=None, gt=0)
    max_result_chars: int | None = Field(default=None, ge=1)


class NetworkBlueprint(_Spec):
    """A network: a lead plans tasks on a shared board, teammates work on them concurrently.

    Teammates claim the ready tasks that match their `capabilities`. The task board and
    message bus are in memory unless `Blueprint.build(task_board=..., bus=...)` passes others.

    Attributes:
        peers: Which teammates may message each other (symmetric), or `"all"`. Every
            teammate can always message the lead.
        teammates_create_tasks: Whether teammates may add follow-up tasks.
    """

    name: str | None = None
    description: str = ""
    lead: str
    teammates: list[str]
    peers: dict[str, list[str]] | Literal["all"] = "all"
    teammates_create_tasks: bool = False
    blackboard: bool = False
    limits: NetworkLimitsBlueprint | None = None


class SequentialStep(_Spec):
    """Runs its steps in order; `carry="all"` passes the request plus every earlier step's text."""

    sequential: list[WorkflowNode] = Field(min_length=1)
    carry: Literal["previous", "all"] = "previous"
    name: str | None = None
    description: str = ""


class ParallelStep(_Spec):
    """Runs its branches (unique names) concurrently on the same input."""

    parallel: list[WorkflowNode] = Field(min_length=1)
    fail_fast: bool = True
    max_concurrency: int | None = Field(default=None, ge=1)
    name: str | None = None
    description: str = ""


class LoopStep(_Spec):
    """Repeats a step until a condition holds or `max_iterations` is reached.

    Attributes:
        until_contains: Stop once an iteration's final text contains this marker.
        until: `"package.module:predicate"` taking the iteration's `RunResult` and returning
            `True` to stop (imported only with `allow_imports=True`).
    """

    loop: WorkflowNode
    max_iterations: int = Field(default=3, ge=1)
    until_contains: str | None = None
    until: str | None = None
    name: str | None = None
    description: str = ""


class MapStep(_Spec):
    """Runs a step once per part of a list, concurrently, and checks every item has a result.

    The input is a JSON array, a JSON object with one list field, or one item per line.
    Items without a result are tried once more, then reported in the `missing` metadata.
    A part of more than one item (`part_size`) must be answered with a list of the same length.
    """

    map: WorkflowNode
    part_size: int = Field(default=1, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1)
    name: str | None = None
    description: str = ""


WorkflowNode: TypeAlias = str | SequentialStep | ParallelStep | LoopStep | MapStep
"""A workflow step: an agent, team or network name, or a nested sequential/parallel/loop/map
step."""

SequentialStep.model_rebuild()
ParallelStep.model_rebuild()
LoopStep.model_rebuild()
MapStep.model_rebuild()


def workflow_references(node: WorkflowNode, path: str = "workflow") -> Iterator[tuple[str, str]]:
    """Yield `(name, path)` for every agent or team name a workflow node references."""

    if isinstance(node, str):
        yield node, path
    elif isinstance(node, SequentialStep):
        for index, child in enumerate(node.sequential):
            yield from workflow_references(child, f"{path}.sequential[{index}]")
    elif isinstance(node, ParallelStep):
        for index, child in enumerate(node.parallel):
            yield from workflow_references(child, f"{path}.parallel[{index}]")
    elif isinstance(node, MapStep):
        yield from workflow_references(node.map, f"{path}.map")
    else:
        yield from workflow_references(node.loop, f"{path}.loop")
