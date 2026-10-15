"""The agent directory: which agent does what, how to reach it, and whether it is free."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from awa._internal.text import STOPWORDS, stem, words
from awa.contracts.cards import AgentCard, AgentStatus
from awa.contracts.events import AgentFinishedEvent, AgentStartedEvent, ErrorEvent, RunEvent
from awa.contracts.protocols import AgentLike
from awa.errors import AgentAlreadyRegisteredError, AgentNotFoundError
from awa.runtime.context import EventSubscriber

_STOPWORDS = STOPWORDS | {"agent", "agents", "help", "need", "please", "should", "someone"}

_RETURNS_SHOWN = 12
"""Top-level output fields a catalog line names before it shortens the list."""

_MIN_PREFIX = 3
_EXACT = 1.0
_PREFIX = 0.5

_Values = tuple[tuple[str, frozenset[str]], ...]
_CardIndex = tuple[tuple[str, float, _Values], ...]
"""Per searchable field, in reason order: `(label, weight, (display value, stems) pairs)`."""


class AgentPresence(BaseModel):
    """Snapshot of an agent's runtime availability and run statistics.

    `total_runs` counts finished runs, failed or not; `average_duration_ms` is `None` before the
    first run finishes; `last_active_at` is the last run start or end (UTC).
    """

    model_config = ConfigDict(frozen=True)

    status: AgentStatus
    active_runs: int = 0
    total_runs: int = 0
    failed_runs: int = 0
    average_duration_ms: float | None = None
    last_active_at: datetime | None = None


class DirectoryMatch(BaseModel):
    """One result of `AgentDirectory.find`: the card, its score (higher is better) and reasons."""

    model_config = ConfigDict(frozen=True)

    card: AgentCard
    score: float
    reasons: tuple[str, ...] = ()


@dataclass
class _Presence:
    pinned_status: AgentStatus | None = None
    active_runs: int = 0
    total_runs: int = 0
    failed_runs: int = 0
    total_duration_ms: int = 0
    last_active_at: datetime | None = None

    @property
    def status(self) -> AgentStatus:
        if self.pinned_status is not None:
            return self.pinned_status
        return AgentStatus.BUSY if self.active_runs > 0 else AgentStatus.AVAILABLE

    def snapshot(self) -> AgentPresence:
        average = self.total_duration_ms / self.total_runs if self.total_runs else None
        return AgentPresence(
            status=self.status,
            active_runs=self.active_runs,
            total_runs=self.total_runs,
            failed_runs=self.failed_runs,
            average_duration_ms=average,
            last_active_at=self.last_active_at,
        )


@dataclass
class _Entry:
    card: AgentCard
    agent: AgentLike | None
    index: _CardIndex
    presence: _Presence


class AgentDirectory:
    """Registry of agent cards with capability lookup, ranking and presence tracking.

    Every agent is registered under its card name. Registering a live agent
    (anything satisfying `AgentLike`, e.g. `Agent` or `Team`) stores its card
    and keeps the object for `resolve`; registering a bare `AgentCard`
    describes an agent reachable by other means.

    Presence: an agent is `BUSY` while it has active runs and `AVAILABLE`
    otherwise. `set_status` can pin `OFFLINE` or `BUSY`, which then holds
    until `set_status` is called again; pinning `AVAILABLE` returns the agent
    to automatic tracking.

    Example:
        ```python
        directory = AgentDirectory([researcher, writer])
        matches = directory.find("cite evidence for a claim")
        agent = directory.resolve(matches[0].card.name)
        ```

    Raises:
        AgentAlreadyRegisteredError: If `agents` contains a name twice.
    """

    def __init__(self, agents: Iterable[AgentCard | AgentLike] = ()) -> None:
        self._entries: dict[str, _Entry] = {}
        for agent in agents:
            self.register(agent)

    def register(self, agent: AgentCard | AgentLike, *, replace: bool = False) -> AgentCard:
        """Register an agent card or a live agent and return the stored card.

        Replacing an existing entry keeps its presence statistics. A
        replacement registered as a bare card drops the previous live object.

        Raises:
            AgentAlreadyRegisteredError: If the name is taken and `replace` is false.
        """

        live: AgentLike | None
        if isinstance(agent, AgentCard):
            card, live = agent, None
        elif isinstance(agent, AgentLike):
            card, live = agent.card, agent
            if not isinstance(card, AgentCard):
                raise TypeError(f"{agent.name!r}.card must be an AgentCard, got {type(card)!r}")
            if agent.name != card.name:
                raise ValueError(
                    f"Agent name {agent.name!r} does not match its card name {card.name!r}"
                )
        else:
            raise TypeError(f"Expected an AgentCard or AgentLike, got {type(agent)!r}")

        existing = self._entries.get(card.name)
        if existing is not None and not replace:
            raise AgentAlreadyRegisteredError(
                f"Agent {card.name!r} is already registered; pass replace=True to overwrite it"
            )
        presence = existing.presence if existing is not None else _Presence()
        self._entries[card.name] = _Entry(card, live, _index_card(card), presence)
        return card

    def unregister(self, name: str) -> None:
        """Remove the agent registered as `name`.

        Raises:
            AgentNotFoundError: If no agent is registered as `name`.
        """

        self._entry(name)
        del self._entries[name]

    def get(self, name: str) -> AgentCard:
        """Return the card of `name`.

        Raises:
            AgentNotFoundError: If no agent is registered as `name`.
        """

        return self._entry(name).card

    def resolve(self, name: str) -> AgentLike | None:
        """Return the live agent registered as `name`, or `None` for a bare card.

        Raises:
            AgentNotFoundError: If no agent is registered as `name`.
        """

        return self._entry(name).agent

    def names(self) -> builtins.list[str]:
        """Return all registered names in sorted order."""

        return sorted(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[AgentCard]:
        """Iterate over the registered cards in name order."""

        return iter([self._entries[name].card for name in self.names()])

    def list(
        self,
        *,
        capability: str | None = None,
        skill: str | None = None,
        tag: str | None = None,
        team: str | None = None,
        role: str | None = None,
        status: AgentStatus | None = None,
    ) -> builtins.list[AgentCard]:
        """Return the cards matching every given filter, in name order.

        String filters are case-insensitive. `capability` also matches
        dotted sub-capabilities: `"billing"` matches `"billing.refunds"`.
        `status` compares against the effective presence status.
        """

        wanted = None if status is None else AgentStatus(status)
        cards: builtins.list[AgentCard] = []
        for name in self.names():
            entry = self._entries[name]
            card = entry.card
            if (
                _matches(card.capabilities, capability, dotted=True)
                and _matches(card.skills, skill)
                and _matches(card.tags, tag)
                and _matches((card.team,), team)
                and _matches((card.role,), role)
                and (wanted is None or entry.presence.status is wanted)
            ):
                cards.append(card)
        return cards

    def find(
        self,
        query: str,
        *,
        limit: int = 5,
        capability: str | None = None,
        available_only: bool = False,
    ) -> builtins.list[DirectoryMatch]:
        """Rank agents by lexical relevance to a free-text task description.

        The query is split into words (punctuation and camelCase break words;
        common stopwords are dropped) and crudely stemmed. Each query term is
        compared against every searchable field; the best-matching value in a
        field adds `weight * quality`, with quality 1.0 for an equal stem and
        0.5 when one stem is a prefix of the other (at least three
        characters). Weights: name 3, capability 3, skill 2, tag 1.5, tool 1,
        description 1. Cards scoring zero are excluded; ties are broken by
        name, so results are fully deterministic. `capability` filters as in `list`.
        """

        if limit < 1:
            raise ValueError("limit must be at least 1")
        terms = _query_terms(query)
        if not terms:
            return []
        status = AgentStatus.AVAILABLE if available_only else None
        matches: builtins.list[DirectoryMatch] = []
        for card in self.list(capability=capability, status=status):
            score, reasons = _score_card(terms, self._entries[card.name].index)
            if score > 0:
                matches.append(DirectoryMatch(card=card, score=score, reasons=reasons))
        matches.sort(key=lambda match: (-match.score, match.card.name))
        return matches[:limit]

    def presence(self, name: str) -> AgentPresence:
        """Return a snapshot of the presence of `name`.

        Raises:
            AgentNotFoundError: If no agent is registered as `name`.
        """

        return self._entry(name).presence.snapshot()

    def set_status(self, name: str, status: AgentStatus) -> None:
        """Set the status of `name`.

        `OFFLINE` and `BUSY` are pinned until the next `set_status` call;
        `AVAILABLE` returns the agent to automatic tracking from its runs.

        Raises:
            AgentNotFoundError: If no agent is registered as `name`.
        """

        status = AgentStatus(status)
        presence = self._entry(name).presence
        presence.pinned_status = None if status is AgentStatus.AVAILABLE else status

    def record_run_start(self, name: str) -> None:
        """Record that a run of `name` started.

        Raises:
            AgentNotFoundError: If no agent is registered as `name`.
        """

        presence = self._entry(name).presence
        presence.active_runs += 1
        presence.last_active_at = datetime.now(UTC)

    def record_run_end(self, name: str, *, ok: bool, duration_ms: int) -> None:
        """Record that a run of `name` finished.

        Raises:
            AgentNotFoundError: If no agent is registered as `name`.
        """

        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        presence = self._entry(name).presence
        presence.active_runs = max(0, presence.active_runs - 1)
        presence.total_runs += 1
        presence.failed_runs += 0 if ok else 1
        presence.total_duration_ms += duration_ms
        presence.last_active_at = datetime.now(UTC)

    def as_event_subscriber(self) -> EventSubscriber:
        """Return an event subscriber that maintains presence from run events.

        `AgentStartedEvent` records a run start; the matching (same `run_id`)
        `AgentFinishedEvent` or `ErrorEvent` records its end. Events of agents
        not registered in this directory are ignored, as are ends of runs whose
        start was not observed.
        """

        runs: dict[str, tuple[str, datetime]] = {}

        def track(event: RunEvent) -> None:
            if isinstance(event, AgentStartedEvent):
                if event.agent in self and event.run_id not in runs:
                    runs[event.run_id] = (event.agent, event.timestamp)
                    self.record_run_start(event.agent)
                return
            if not isinstance(event, AgentFinishedEvent | ErrorEvent):
                return
            started = runs.pop(event.run_id, None)
            if started is None or started[0] not in self:
                return
            name, started_at = started
            if isinstance(event, AgentFinishedEvent):
                self.record_run_end(name, ok=True, duration_ms=event.duration_ms)
            else:
                elapsed = (event.timestamp - started_at).total_seconds() * 1000
                self.record_run_end(name, ok=False, duration_ms=max(0, int(elapsed)))

        return track

    def render(self, names: Sequence[str] | None = None, *, include_presence: bool = False) -> str:
        """Render a compact markdown catalog of agents for a system prompt.

        Each line names the agent's capabilities, skills and, for an agent with a
        structured output, the top-level fields it returns; empty when there are no agents.

        Args:
            names: Agents to include, in this order; all agents in name order when `None`.
            include_presence: Append each agent's status and active runs.

        Raises:
            AgentNotFoundError: If a name in `names` is not registered.
        """

        lines: builtins.list[str] = []
        for name in self.names() if names is None else names:
            entry = self._entry(name)
            card = entry.card
            details: builtins.list[str] = []
            if card.capabilities:
                details.append(f"capabilities: {', '.join(card.capabilities)}")
            if card.skills:
                details.append(f"skills: {', '.join(card.skills)}")
            if returns := _output_fields(card.output_schema):
                details.append(f"returns: {returns}")
            if include_presence:
                presence = entry.presence
                details.append(f"status: {presence.status.value}")
                if presence.active_runs:
                    details.append(f"active runs: {presence.active_runs}")
            line = f"- **{card.name}**: {card.description or 'No description.'}"
            if details:
                line += f" ({'; '.join(details)})"
            lines.append(line)
        return "\n".join(lines)

    def _entry(self, name: str) -> _Entry:
        entry = self._entries.get(name)
        if entry is None:
            known = ", ".join(self.names()) or "none"
            raise AgentNotFoundError(f"Unknown agent {name!r}. Registered agents: {known}")
        return entry


def _query_terms(query: str) -> list[tuple[str, str]]:
    """Return the distinct `(word, stem)` pairs of `query`, stopwords removed."""

    terms: dict[str, str] = {}
    for word in words(query):
        if word not in _STOPWORDS:
            terms.setdefault(stem(word), word)
    return [(word, stemmed) for stemmed, word in terms.items()]


def _match_quality(term: str, stems: frozenset[str]) -> float:
    if term in stems:
        return _EXACT
    for candidate in stems:
        if min(len(term), len(candidate)) >= _MIN_PREFIX and (
            term.startswith(candidate) or candidate.startswith(term)
        ):
            return _PREFIX
    return 0.0


def _index_card(card: AgentCard) -> _CardIndex:
    """Precompute the weighted searchable values of `card`."""

    def field(label: str, weight: float, values: tuple[str, ...]) -> tuple[str, float, _Values]:
        return label, weight, tuple((v, frozenset(stem(w) for w in words(v))) for v in values)

    return (
        field("name", 3.0, (card.name,)),
        field("capability", 3.0, card.capabilities),
        field("skill", 2.0, card.skills),
        field("tag", 1.5, card.tags),
        field("tool", 1.0, card.tools),
        field("description", 1.0, tuple(dict.fromkeys(words(card.description)))),
    )


def _score_card(terms: list[tuple[str, str]], index: _CardIndex) -> tuple[float, tuple[str, ...]]:
    """Return the score of one card and one reason per contributing `(field, term)` pair."""

    total = 0.0
    reasons: list[str] = []
    for label, weight, values in index:
        for word, term in terms:
            quality, value = _best_value(term, values)
            if quality:
                total += weight * quality
                verb = "matches" if quality == _EXACT else "partially matches"
                reasons.append(f"{label} {value!r} {verb} {word!r}")
    return total, tuple(reasons)


def _best_value(term: str, values: _Values) -> tuple[float, str]:
    """Return the best match quality of `term` in one field and the value that reached it."""

    best_quality, best_value = 0.0, ""
    for value, stems in values:
        quality = _match_quality(term, stems)
        if quality > best_quality:
            best_quality, best_value = quality, value
            if quality == _EXACT:
                break
    return best_quality, best_value


def _matches(values: Iterable[str | None], wanted: str | None, *, dotted: bool = False) -> bool:
    """Case-insensitive filter check; `dotted` also accepts sub-values like `wanted.child`."""

    if wanted is None:
        return True
    wanted = wanted.strip().casefold()
    normalized = (value.strip().casefold() for value in values if value is not None)
    return any(v == wanted or (dotted and v.startswith(wanted + ".")) for v in normalized)


def _output_fields(schema: Mapping[str, Any] | None) -> str:
    """Name the top-level fields of a structured output schema; `""` when there are none."""

    properties = schema.get("properties") if schema else None
    if not isinstance(properties, Mapping) or not properties:
        return ""
    names = [str(name) for name in properties]
    shown = ", ".join(names[:_RETURNS_SHOWN])
    return shown + (", ..." if len(names) > _RETURNS_SHOWN else "")
