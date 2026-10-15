"""Auto-matching: give each agent the builtin skills and tools its name and task call for.

Matching is deterministic word scoring, never a model call: at most `MAX_AUTO_SKILLS` skills
and `MAX_AUTO_TOOLS` builtin tools per agent; it only adds. A tool that acts outside the
agent, waits for approval, or lacks its toolkit configuration or extra is only suggested.
`AutoReport` says per agent what was matched and why.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakKeyDictionary

from awa._internal.text import STOPWORDS, stem, words
from awa.errors import ConfigurationError
from awa.skills import Skill, SkillRegistry

if TYPE_CHECKING:
    from awa.blueprints.schema import Blueprint, SkillSource
    from awa.toolkits import CatalogEntry

MAX_AUTO_SKILLS = 3
"""Most skills auto-matching adds to one agent (a role skill included)."""

MAX_AUTO_TOOLS = 3
"""Most builtin tools auto-matching grants to one agent (suggestions are listed besides)."""

ROLE_SKILL = "orchestration-planning"
"""The skill a team orchestrator or network lead gets for its role."""

_SKILL_THRESHOLD = 5.0
_NAME_WEIGHT, _LABEL_WEIGHT, _DESCRIPTION_WEIGHT = 3.0, 2.0, 1.0
_MAX_SUGGESTIONS = 3

_STOPWORDS = STOPWORDS | frozenset(
    (  # noqa: SIM905
        "after against all also based before each every final get given good handle handles help "
        "helps like make must need needs new not one out own part parts per right should some "
        "such thing things up use used uses using via well while without work works"
    ).split()
)
Kind = Literal["skill", "tool"]
"""What an `AutoMatch` adds: a skill, or a builtin tool."""

_reports: WeakKeyDictionary[Any, AutoReport] = WeakKeyDictionary()


@dataclass(frozen=True)
class AutoMatch:
    """One skill or builtin tool that auto-matching picked for an agent.

    Attributes:
        name: The skill name, or the tool reference `builtin:<name>`.
        reason: Why it matched, e.g. `"matched: pdf, invoices"`.
        granted: Whether the agent got it; `False` for a suggestion.
        note: Why a suggestion was not granted, and how to add it.
    """

    kind: Kind
    name: str
    reason: str
    score: float = 0.0
    granted: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The match as JSON-compatible data."""

        data = asdict(self)
        if not self.note:
            del data["note"]
        return data


@dataclass(frozen=True)
class AgentAutoReport:
    """What auto-matching did for one agent: granted matches first, then suggestions."""

    agent: str
    auto: bool
    explicit_tools: tuple[str, ...] = ()
    explicit_skills: tuple[str, ...] = ()
    matches: tuple[AutoMatch, ...] = ()
    note: str = ""

    @property
    def granted(self) -> tuple[AutoMatch, ...]:
        """The matches the agent got."""

        return tuple(match for match in self.matches if match.granted)

    @property
    def suggested(self) -> tuple[AutoMatch, ...]:
        """The matches that were only suggested (not granted)."""

        return tuple(match for match in self.matches if not match.granted)

    @property
    def skills(self) -> tuple[str, ...]:
        """Names of the granted skills."""

        return tuple(m.name for m in self.granted if m.kind == "skill")

    @property
    def tools(self) -> tuple[str, ...]:
        """References (`builtin:<name>`) of the granted tools."""

        return tuple(m.name for m in self.granted if m.kind == "tool")

    def to_dict(self) -> dict[str, Any]:
        """The report as JSON-compatible data."""

        data: dict[str, Any] = {
            "agent": self.agent,
            "auto": self.auto,
            "explicit": {"tools": list(self.explicit_tools), "skills": list(self.explicit_skills)},
            "granted": [match.to_dict() for match in self.granted],
            "suggested": [match.to_dict() for match in self.suggested],
        }
        if self.note:
            data["note"] = self.note
        return data


@dataclass(frozen=True)
class AutoReport:
    """What auto-matching did for a whole system, agent by agent; `str()` reads, `to_dict()` is data."""

    system: str
    agents: tuple[AgentAutoReport, ...]
    blueprint: Blueprint | None = field(default=None, compare=False, repr=False)

    def for_agent(self, name: str) -> AgentAutoReport:
        """Return the report of the agent `name`; raises `KeyError` if there is none."""

        for report in self.agents:
            if report.agent == name:
                return report
        raise KeyError(name)

    @property
    def suggested(self) -> tuple[tuple[str, AutoMatch], ...]:
        """Every suggestion that was not granted, as `(agent, match)` pairs."""

        return tuple((report.agent, match) for report in self.agents for match in report.suggested)

    def to_dict(self) -> dict[str, Any]:
        """The report as JSON-compatible data."""

        return {"system": self.system, "agents": [report.to_dict() for report in self.agents]}

    def to_text(self) -> str:
        """The report as readable text: `+` granted, `?` suggested, `=` explicit."""

        lines = [f"auto-matching for {self.system}"]
        for report in self.agents:
            lines.append(f"  {report.agent}{'' if report.auto else ' (auto off)'}")
            if report.note:
                lines.append(f"    {report.note}")
            for skill in report.explicit_skills:
                lines.append(f"    = skill {skill} (explicit)")
            for tool in report.explicit_tools:
                lines.append(f"    = tool  {tool} (explicit)")
            for match in report.matches:
                sign = "+" if match.granted else "?"
                text = f"    {sign} {match.kind:<5} {match.name} ({match.reason})"
                if match.note:
                    text += f"; not granted: {match.note}"
                lines.append(text)
            if report.auto and not report.matches:
                lines.append("    nothing matched")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.to_text()


def match_agent(
    name: str,
    description: str = "",
    *,
    instructions: str = "",
    role: Literal["orchestrator", "lead"] | None = None,
    skills: SkillRegistry | None = None,
    toolkits: Mapping[str, Mapping[str, Any]] | None = None,
    explicit_tools: Sequence[str] = (),
    explicit_skills: Sequence[str] = (),
) -> AgentAutoReport:
    """Match the skills and builtin tools for one agent.

    Skills score by the agent's words in their name (3), tags and category (2) and description
    (1) and need 5 points, one from name or tags; an orchestrator or lead (`role`) also gets
    `orchestration-planning`. Builtin tools match by keywords.

    Args:
        toolkits: The toolkit configuration (a blueprint's `toolkits:`); tools of
            unconfigured toolkits are only suggested.
    """

    registry = skills if skills is not None else SkillRegistry.builtin()
    words = _words(f"{name} {description} {instructions}")
    matches = [
        *_skill_matches(words, registry, role, explicit_skills),
        *_tool_matches(words, toolkits or {}, explicit_tools),
    ]
    return AgentAutoReport(
        agent=name,
        auto=True,
        explicit_tools=tuple(explicit_tools),
        explicit_skills=tuple(explicit_skills),
        matches=tuple(sorted(matches, key=lambda m: not m.granted)),
    )


def _skill_matches(
    words: list[tuple[str, str]], registry: SkillRegistry, role: str | None, explicit: Sequence[str]
) -> list[AutoMatch]:
    taken = set(explicit)
    found: list[AutoMatch] = []
    if role is not None and ROLE_SKILL in registry and ROLE_SKILL not in taken:
        found.append(AutoMatch("skill", ROLE_SKILL, f"role: {role}", score=_SKILL_THRESHOLD))
        taken.add(ROLE_SKILL)
    spellings: dict[str, str] = {}  # key -> first spelling
    for word, key in words:
        spellings.setdefault(key, word)
    scored: list[tuple[float, str, list[str]]] = []
    for skill in registry.all():
        if skill.name in taken:
            continue
        score, hits, labelled = _skill_score(skill, spellings)
        if score >= _SKILL_THRESHOLD and labelled:
            scored.append((score, skill.name, hits))
    scored.sort(key=lambda item: (-item[0], item[1]))
    for score, skill_name, hits in scored[: max(0, MAX_AUTO_SKILLS - len(found))]:
        found.append(AutoMatch("skill", skill_name, f"matched: {', '.join(hits)}", score=score))
    return found


def _skill_score(skill: Skill, spellings: Mapping[str, str]) -> tuple[float, list[str], bool]:
    """The score of `skill`, the words that hit, and whether one hit its name or tags."""

    fields = (
        (_NAME_WEIGHT, _keys(skill.name)),
        (_LABEL_WEIGHT, _keys(" ".join((*skill.tags, skill.category or "")))),
        (_DESCRIPTION_WEIGHT, _keys(skill.description)),
    )
    credited: set[tuple[float, str]] = set()
    score = 0.0
    hits: list[str] = []
    labelled = False
    for key, word in spellings.items():
        for weight, keys in fields:
            target = _match(key, keys)
            if target is None:
                continue
            if (weight, target) not in credited:
                credited.add((weight, target))
                score += weight
                hits.append(word)
                labelled = labelled or weight > _DESCRIPTION_WEIGHT
            break
    return score, hits, labelled


def _tool_matches(
    words: list[tuple[str, str]], toolkits: Mapping[str, Mapping[str, Any]], explicit: Sequence[str]
) -> list[AutoMatch]:
    from awa.toolkits import catalog

    have = {_plain_tool_name(reference) for reference in explicit}
    keys = [key for _, key in words]
    phrase = f" {' '.join(keys)} "
    originals = {key: word for word, key in reversed(words)}
    candidates: list[tuple[int, int, CatalogEntry, list[str]]] = []
    for position, entry in enumerate(catalog().entries()):
        if entry.name in have:
            continue
        hits = _keyword_hits(entry, keys, phrase, originals)
        if hits:
            candidates.append((len(hits), position, entry, hits))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    granted: list[AutoMatch] = []
    suggested: list[AutoMatch] = []
    for score, _, entry, hits in candidates:
        match = AutoMatch("tool", entry.reference, f"matched: {', '.join(hits)}", float(score))
        blocker = _not_grantable(entry, toolkits)
        if blocker is None and len(granted) < MAX_AUTO_TOOLS:
            granted.append(match)
        elif blocker is not None and len(suggested) < _MAX_SUGGESTIONS:
            suggested.append(replace(match, granted=False, note=blocker))
    return [*granted, *suggested]


def _keyword_hits(
    entry: CatalogEntry, keys: list[str], phrase: str, originals: Mapping[str, str]
) -> list[str]:
    keywords = entry.keywords or (entry.name.replace("_", " "),)
    present = set(keys)
    covered: set[str] = set()
    hits: list[str] = []
    # Phrases first, so "e-mail" is one hit rather than "e-mail" and "mail".
    for parts, keyword in sorted(
        (([key for _, key in _words(keyword)], keyword) for keyword in keywords),
        key=lambda item: -len(item[0]),
    ):
        if not parts or set(parts) <= covered:
            continue
        if len(parts) == 1:
            if parts[0] in present:
                hits.append(originals.get(parts[0], keyword))
                covered.add(parts[0])
        elif f" {' '.join(parts)} " in phrase:
            hits.append(keyword)
            covered.update(parts)
    return hits


def _not_grantable(entry: CatalogEntry, toolkits: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Why `entry` cannot be granted automatically, or `None` when it can."""

    if entry.name in ("fetch_url", "verify_quote") and entry.risk_for(toolkits).external_action:
        return (
            "it can send data to any web address it reads; set toolkits.web.allowed_hosts "
            "to grant it automatically"
        )
    if entry.risk_for(toolkits).external_action or entry.approval_for(toolkits):
        return "it sends or changes data outside the agent" + (
            " (once added, its calls wait for approval)" if entry.requires_approval else ""
        )
    problems = entry.problems(toolkits)
    return "; ".join(problems) if problems else None


def blueprint_auto_report(blueprint: Blueprint, *, skills: SkillSource | None = None) -> AutoReport:
    """The auto-matching report of `blueprint`; see `Blueprint.explain`."""

    registry = _registry(skills)
    roles = _roles(blueprint)
    reports: list[AgentAutoReport] = []
    for spec in blueprint.agents:
        enabled = spec.auto if spec.auto is not None else blueprint.auto
        explicit = (tuple(spec.tools), tuple(spec.skills))
        if not enabled:
            report = AgentAutoReport(spec.name, False, *explicit)
        else:
            report = match_agent(
                spec.name,
                spec.description,
                instructions=spec.instructions,
                role=roles.get(spec.name),
                skills=registry,
                toolkits=blueprint.toolkits,
                explicit_tools=spec.tools,
                explicit_skills=spec.skills,
            )
        reports.append(report)
    return AutoReport(blueprint.name, tuple(reports), blueprint)


def apply_auto(
    blueprint: Blueprint, *, skills: SkillSource | None = None
) -> tuple[Blueprint, AutoReport]:
    """Return `blueprint` with the granted matches added to its agents, and the report."""

    report = blueprint_auto_report(blueprint, skills=skills)
    if not any(agent.granted for agent in report.agents):
        return blueprint, report
    agents = []
    for spec, agent in zip(blueprint.agents, report.agents, strict=True):
        if agent.granted:
            spec = spec.model_copy(
                update={
                    "tools": [*spec.tools, *agent.tools],
                    "skills": [*spec.skills, *agent.skills],
                }
            )
        agents.append(spec)
    return blueprint.model_copy(update={"agents": agents}), report


def _roles(blueprint: Blueprint) -> dict[str, Literal["orchestrator", "lead"]]:
    roles: dict[str, Literal["orchestrator", "lead"]] = {}
    if blueprint.team is not None:
        roles[blueprint.team.orchestrator] = "orchestrator"
    if blueprint.network is not None:
        roles[blueprint.network.lead] = "lead"
    return roles


def _registry(skills: SkillSource | None) -> SkillRegistry:
    if skills is None:
        return SkillRegistry.builtin()
    if isinstance(skills, SkillRegistry):
        return skills
    names = set(skills)
    return SkillRegistry(skill for skill in SkillRegistry.builtin().all() if skill.name in names)


def remember_report(system: Any, report: AutoReport) -> None:
    """Keep `report` for `explain(system)` as long as `system` lives."""

    with contextlib.suppress(TypeError):  # a system that cannot be weakly referenced
        _reports[system] = report


def explain(system: Any) -> AutoReport:
    """Report what auto-matching gave each agent of a system, and what it only suggested.

    Args:
        system: A system built by `awa.quick` or `Blueprint.build`, or a `Blueprint`.

    Raises:
        ConfigurationError: If `system` was built some other way.

    Example:
        ```python
        print(explain(desk).for_agent("checker"))
        ```
    """

    from awa.blueprints.schema import Blueprint

    if isinstance(system, Blueprint):
        return system.explain()
    try:
        report = _reports.get(system)
    except TypeError:  # not weakly referenceable, so never remembered
        report = None
    if report is None:
        raise ConfigurationError(
            f"{type(system).__name__} {getattr(system, 'name', '')!r} was not built by "
            "awa.quick or a Blueprint, so there is no auto-matching report for it"
        )
    return report


def _words(text: str) -> list[tuple[str, str]]:
    """`(word, key)` pairs of the text's words, stopwords left out, in order."""

    return [(word, stem(word)) for word in words(text) if word not in _STOPWORDS]


def _keys(text: str) -> set[str]:
    return {key for _, key in _words(text)}


def _match(key: str, candidates: set[str]) -> str | None:
    """The candidate equal to `key` or sharing a long prefix (`validat`, `valid`), else `None`."""

    if key in candidates:
        return key
    if len(key) < 4:
        return None
    for candidate in sorted(candidates):
        shortest = min(len(key), len(candidate))
        if shortest < 4:
            continue
        shared = len(os.path.commonprefix((key, candidate)))
        if shared == shortest or shared >= max(5, 0.75 * shortest):
            return candidate
    return None


def _plain_tool_name(reference: str) -> str:
    """The tool name a reference builds (`builtin:read_file` -> `read_file`)."""

    name = reference.removeprefix("builtin:")
    if ":" in name:
        name = name.rpartition(":")[2].rpartition(".")[2]
    return name.rpartition("/")[2]


__all__ = [
    "MAX_AUTO_SKILLS",
    "MAX_AUTO_TOOLS",
    "ROLE_SKILL",
    "AgentAutoReport",
    "AutoMatch",
    "AutoReport",
    "Kind",
    "apply_auto",
    "blueprint_auto_report",
    "explain",
    "match_agent",
    "remember_report",
]
