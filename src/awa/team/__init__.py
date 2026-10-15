"""Runtime layer: multi-agent systems.

`Team` wires an orchestrator and members for delegation, consultation, messaging and handoffs;
`AgentNetwork` runs a lead and autonomous teammates concurrently on a shared task board.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute
from awa.team._handoffs import TEAM_NOTE_KEY, HandoffHistory, HandoffInput, is_team_note
from awa.team.limits import NetworkLimits, TeamLimits
from awa.team.team import ResumeWith, Team

if TYPE_CHECKING:
    from awa.team.network import AgentNetwork, StopReason

_HOMES = homes({"awa.team.network": "AgentNetwork StopReason"})

__all__ = [
    "TEAM_NOTE_KEY",
    "AgentNetwork",
    "HandoffHistory",
    "HandoffInput",
    "NetworkLimits",
    "ResumeWith",
    "StopReason",
    "Team",
    "TeamLimits",
    "is_team_note",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
