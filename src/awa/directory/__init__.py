"""Runtime layer: the agent directory - who does what, and whether it is available.

Preview: it works and is tested, but its API may still change in a minor release.

`AgentDirectory` stores agent cards, ranks them for a task and tracks presence;
`create_directory_tools` lets agents query it.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa.directory.directory import AgentDirectory, AgentPresence, DirectoryMatch
    from awa.directory.tools import create_directory_tools

_HOMES = homes(
    {
        "awa.directory.directory": "AgentDirectory AgentPresence DirectoryMatch",
        "awa.directory.tools": "create_directory_tools",
    }
)

__stability__ = "preview"

__all__ = ["AgentDirectory", "AgentPresence", "DirectoryMatch", "create_directory_tools"]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
