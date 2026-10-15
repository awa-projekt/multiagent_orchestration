"""Risk annotations of tools, for the excessive-agency check of `Blueprint.check`."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from awa._internal.upstream.tools import BaseTool

RISK_METADATA_KEY = "awa_risk"
"""Key of a tool's risk annotation in `BaseTool.metadata`."""

RiskName = Literal["untrusted_input", "private_data", "external_action"]
"""The three risk flags of a tool."""

RISK_NAMES: tuple[RiskName, ...] = ("untrusted_input", "private_data", "external_action")
"""Every risk flag, in the order they are reported."""

_DESCRIPTION_WORD = re.compile(r"[a-z0-9]+")
_PHRASES_UNTRUSTED = ("search engine", "search the web", "current events")

_TAG_PREFIX = "awa:"

_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Description words that mark a tool whose results an outsider may have written.
_UNTRUSTED_DESCRIPTION_WORDS = _words(
    """
    web internet online url urls webpage webpages website websites browse browser scrape crawl
    google bing duckduckgo wikipedia news rss email emails inbox tweets reddit
    """
)
# Description words that mark a tool acting outside the agent. Deliberately
# narrow: descriptions often say "write" or "create" about their own answer.
_ACTION_DESCRIPTION_WORDS = _words(
    """
    send sends delete deletes remove removes purchase pay payment transfer publish publishes
    deploy execute executes shell terminal overwrite overwrites webhook smtp sms
    """
)
_WEB_WORDS = _words(
    """
    web browse browser scrape crawl url urls http https rss webpage website internet online
    """
)
_READ_VERBS = _words(
    """
    read get fetch list open load parse extract search find lookup download view show summarize
    ocr check query
    """
)
_CONTENT_WORDS = _words(
    """
    email emails mail mails inbox document documents doc docs pdf pdfs attachment attachments
    file files upload uploads feed feeds comment comments review reviews message messages page
    pages post posts
    """
)
_ACTION_VERBS = _words(
    """
    send post publish email mail sms tweet notify forward reply share upload submit pay charge
    refund transfer purchase buy book wire delete remove drop destroy purge wipe cancel
    terminate revoke deploy execute exec write update modify edit create insert approve grant
    invite merge push commit save set put patch upsert archive close mark assign move rename
    run call trigger
    """
)
_ALWAYS_ACTION_WORDS = frozenset({"webhook", "shell", "bash", "powershell", "smtp", "sms"})
_PRIVATE_WORDS = _words(
    """
    customer customers client clients crm account accounts user users profile profiles employee
    employees hr payroll salary salaries patient patients medical health invoice invoices
    billing payment payments bank ledger finance financial contract contracts database db sql
    record records secret secrets credential credentials password passwords ssn personal pii
    internal private confidential order orders
    """
)


class ToolRisk(BaseModel):
    """What a tool exposes an agent to; `None` leaves a flag to the next source.

    An agent that combines private data, untrusted input and ungated external actions is what
    `Blueprint.check` warns about. In a blueprint's `tool_risks:` map a risk is a mapping of flags
    or a list of the true ones (`fetch_page: [untrusted_input, external_action]`).

    Attributes:
        untrusted_input: Its results can contain text an outsider wrote.
        private_data: It reads data that must not leak.
        external_action: It changes something outside the agent or sends data out; gated when a
            blueprint approval covers it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    untrusted_input: bool | None = None
    private_data: bool | None = None
    external_action: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_flag_list(cls, data: Any) -> Any:
        if isinstance(data, str):
            data = [data]
        if isinstance(data, list | tuple):
            unknown = [str(flag) for flag in data if flag not in RISK_NAMES]
            if unknown:
                raise ValueError(
                    f"unknown risk flag(s) {', '.join(unknown)}; use {', '.join(RISK_NAMES)}"
                )
            return dict.fromkeys(data, True)
        return data

    def merged_with(self, fallback: ToolRisk | None) -> ToolRisk:
        """Return these flags, taking every unset one from `fallback`."""

        if fallback is None:
            return self
        own = {name: getattr(self, name) for name in RISK_NAMES}
        return ToolRisk(**{n: getattr(fallback, n) if v is None else v for n, v in own.items()})

    def flags(self) -> tuple[RiskName, ...]:
        """Return the names of the flags that are `True`."""

        return tuple(name for name in RISK_NAMES if getattr(self, name) is True)


def mark_tool_risk(
    tool: BaseTool,
    *,
    untrusted_input: bool | None = None,
    private_data: bool | None = None,
    external_action: bool | None = None,
) -> BaseTool:
    """A copy of `tool` annotated with its risk (`metadata["awa_risk"]`); `None` leaves a flag open.

    Example:
        ```python
        fetch_page = mark_tool_risk(fetch_page, untrusted_input=True, external_action=True)
        ```
    """

    risk = ToolRisk(
        untrusted_input=untrusted_input, private_data=private_data, external_action=external_action
    ).merged_with(declared_tool_risk(tool))
    metadata = {**(tool.metadata or {}), RISK_METADATA_KEY: risk.model_dump(exclude_none=True)}
    return tool.model_copy(update={"metadata": metadata})


def declared_tool_risk(tool: BaseTool) -> ToolRisk | None:
    """The risk `tool` declares itself (its `awa_risk` metadata and `awa:` tags).

    Raises:
        ValueError: If the `awa_risk` metadata is malformed.
    """

    return _declared(tool.metadata, tool.tags)


def graph_durability(system: object, name: str, checkpointer: Any) -> bool | None:
    """`RunOptions.durable_writes` for one run of `system`, recorded as a span.

    `True` when `system` holds an `external_action` tool (also in a member), so a crash right
    after a write resumes after it; `False` otherwise; `None` without a checkpointer of its own.
    """

    from awa.observability.spans import record_span

    if not checkpointer:
        return None
    writes = _writes(system, set())
    record_span(
        "awa.graph.durability",
        {
            "awa.graph.name": name,
            "awa.graph.writes": writes,
            "awa.graph.durability": "sync" if writes else "async",
        },
    )
    return writes


def infer_tool_risk(
    tool: BaseTool | str,
    *,
    declared: ToolRisk | None = None,
    tags: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> ToolRisk:
    """The resolved risk of a tool: every flag `True` or `False`.

    Per flag the first source decides: `declared`, the tool's annotation (`awa_risk` metadata or
    `awa:` tags), then heuristics (MCP tools are untrusted input, MCP hints mark actions, and name
    words such as `fetch_url`, `send_email` or `customer`). The heuristics err towards warning; a
    read-only tool with an action word in its name needs `mark_tool_risk(external_action=False)`.

    Args:
        tool: The tool, or only its name (then pass `tags` and `metadata` if known).
        declared: Flags that take precedence, such as a blueprint's `tool_risks` entry.

    Example:
        ```python
        assert infer_tool_risk("send_email").external_action
        ```
    """

    if isinstance(tool, BaseTool):
        name, tags, metadata = tool.name, tool.tags or (), tool.metadata
    else:
        name = tool
    explicit = (declared or ToolRisk()).merged_with(_declared(metadata, tags))
    heuristic = _heuristic(str((metadata or {}).get("mcp_tool", name)), metadata or {})
    resolved = explicit.merged_with(heuristic)
    return ToolRisk(**{flag: bool(getattr(resolved, flag)) for flag in RISK_NAMES})


def infer_description_risk(description: str) -> ToolRisk:
    """The risk flags a tool description alone suggests: untrusted input for words such as
    web, URL or email, external action for narrow action words such as send, delete or pay.
    Private data is never inferred from a description."""

    text = description.lower()
    words = set(_DESCRIPTION_WORD.findall(text))
    untrusted = bool(words & _UNTRUSTED_DESCRIPTION_WORDS) or any(
        phrase in text for phrase in _PHRASES_UNTRUSTED
    )
    return ToolRisk(
        untrusted_input=untrusted,
        private_data=False,
        external_action=bool(words & _ACTION_DESCRIPTION_WORDS),
    )


def _declared(metadata: Mapping[str, Any] | None, tags: Iterable[str] | None) -> ToolRisk | None:
    risk: ToolRisk | None = None
    value = (metadata or {}).get(RISK_METADATA_KEY)
    if value is not None:
        risk = value if isinstance(value, ToolRisk) else ToolRisk.model_validate(value)
    tagged = [tag[len(_TAG_PREFIX) :] for tag in tags or () if tag.startswith(_TAG_PREFIX)]
    flags = [flag for flag in tagged if flag in RISK_NAMES]
    if flags:
        from_tags = ToolRisk.model_validate(flags)
        risk = risk.merged_with(from_tags) if risk is not None else from_tags
    return risk


def _heuristic(name: str, metadata: Mapping[str, Any]) -> ToolRisk:
    words = [word.lower() for word in _WORDS.findall(name)]
    first = words[0] if words else ""
    reads = first in _READ_VERBS
    word_set = set(words)
    untrusted = bool(word_set & _WEB_WORDS) or (reads and bool(word_set & _CONTENT_WORDS))
    # The whole name is judged: `search_and_send_email` acts although it starts with a read
    # verb. After a read verb, words that also name content (`read_email`) are its object.
    verbs = word_set & _ACTION_VERBS
    if reads:
        verbs -= _CONTENT_WORDS
    action = bool(word_set & _ALWAYS_ACTION_WORDS) or bool(verbs)
    if metadata.get("destructiveHint") is True or metadata.get("readOnlyHint") is False:
        action = True
    return ToolRisk(
        untrusted_input=untrusted or "mcp_server" in metadata,
        private_data=bool(word_set & _PRIVATE_WORDS),
        external_action=action,
    )


def _writes(system: object, seen: set[int]) -> bool:
    if id(system) in seen:
        return False
    seen.add(id(system))
    for tool in getattr(system, "tools", None) or ():
        if isinstance(tool, BaseTool) and infer_tool_risk(tool).external_action:
            return True
    members = getattr(system, "members", None)
    if members is None and callable(getattr(system, "_members", None)):
        members = system._members()  # type: ignore[attr-defined]
    if callable(members):
        members = members()
    return any(_writes(member, seen) for member in members or ())


__all__ = [
    "RISK_METADATA_KEY",
    "RISK_NAMES",
    "RiskName",
    "ToolRisk",
    "declared_tool_risk",
    "infer_description_risk",
    "infer_tool_risk",
    "mark_tool_risk",
]
