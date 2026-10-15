"""Messaging (extension): communication between agents.

Preview: it works and is tested, but its API may still change in a minor release.

A message bus (mailboxes, broadcast, topics, request/reply), the `Blackboard`, and their tools.
"""

from awa.messaging.blackboard import BaseBlackboard, Blackboard, BlackboardEntry
from awa.messaging.bus import DeadLetter, InMemoryMessageBus, MessageBus, OverflowPolicy
from awa.messaging.policy import AllowAll, CommunicationPolicy, TopologyPolicy
from awa.messaging.tools import create_blackboard_tools, create_messaging_tools

__stability__ = "preview"

__all__ = [
    "AllowAll",
    "BaseBlackboard",
    "Blackboard",
    "BlackboardEntry",
    "CommunicationPolicy",
    "DeadLetter",
    "InMemoryMessageBus",
    "MessageBus",
    "OverflowPolicy",
    "TopologyPolicy",
    "create_blackboard_tools",
    "create_messaging_tools",
]
