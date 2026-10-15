"""Communication policies decide who may message whom; replies to a request are always allowed."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class CommunicationPolicy(Protocol):
    """Decides which messages a `MessageBus` may deliver."""

    def allows(self, sender: str, recipient: str) -> bool:
        """Return whether `sender` may send a direct message to `recipient`."""
        ...

    def allows_topic(self, sender: str, topic: str) -> bool:
        """Return whether `sender` may publish on `topic`."""
        ...

    def allows_broadcast(self, sender: str, recipient: str) -> bool:
        """Return whether a broadcast from `sender` may reach `recipient`."""
        ...


class AllowAll:
    """Policy that permits every message. The default for a bus."""

    def allows(self, sender: str, recipient: str) -> bool:
        return True

    def allows_topic(self, sender: str, topic: str) -> bool:
        return True

    def allows_broadcast(self, sender: str, recipient: str) -> bool:
        return True


class TopologyPolicy:
    """Policy that permits only explicitly declared communication edges.

    Args:
        edges: Maps each sender to the recipients it may message directly.
        symmetric: Also allow every edge in the reverse direction.
        topic_publishers: Maps each topic to its allowed publishers; unlisted topics are then
            closed. `None` lets anyone publish anywhere.
        allow_broadcast: Senders whose broadcasts reach every mailbox; others may not broadcast.
            `None` lets everyone broadcast, but only along their edges.

    Raises:
        TypeError: If a single string is given where a sequence of names is expected.

    Example:
        ```python
        policy = TopologyPolicy(
            {"lead": ["researcher", "writer"], "researcher": ["writer"]},
            symmetric=True,
            topic_publishers={"findings": ["researcher"]},
            allow_broadcast=["lead"],
        )
        ```
    """

    def __init__(
        self,
        edges: Mapping[str, Iterable[str]],
        *,
        symmetric: bool = False,
        topic_publishers: Mapping[str, Iterable[str]] | None = None,
        allow_broadcast: Iterable[str] | None = None,
    ) -> None:
        for sender, recipients in edges.items():
            _names(recipients, f"edges[{sender!r}]")
        for topic, senders in (topic_publishers or {}).items():
            _names(senders, f"topic_publishers[{topic!r}]")
        if allow_broadcast is not None:
            _names(allow_broadcast, "allow_broadcast")
        graph: dict[str, set[str]] = {}
        for sender, recipients in edges.items():
            for recipient in recipients:
                graph.setdefault(sender, set()).add(recipient)
                if symmetric:
                    graph.setdefault(recipient, set()).add(sender)
        self._edges = {sender: frozenset(targets) for sender, targets in graph.items()}
        self._topic_publishers = (
            None
            if topic_publishers is None
            else {topic: frozenset(senders) for topic, senders in topic_publishers.items()}
        )
        self._broadcasters = None if allow_broadcast is None else frozenset(allow_broadcast)

    def allows(self, sender: str, recipient: str) -> bool:
        """Return whether the edge `sender -> recipient` was declared."""

        return recipient in self._edges.get(sender, frozenset())

    def allows_topic(self, sender: str, topic: str) -> bool:
        """Return whether `sender` is a declared publisher of `topic`."""

        if self._topic_publishers is None:
            return True
        return sender in self._topic_publishers.get(topic, frozenset())

    def allows_broadcast(self, sender: str, recipient: str) -> bool:
        """Return whether a broadcast from `sender` may reach `recipient`."""

        if self._broadcasters is None:
            return self.allows(sender, recipient)
        return sender in self._broadcasters


def _names(value: object, where: str) -> None:
    if isinstance(value, str):
        raise TypeError(f"{where} must be a sequence of agent names, not the string {value!r}")


__all__ = ["AllowAll", "CommunicationPolicy", "TopologyPolicy"]
