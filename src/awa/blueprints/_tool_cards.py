"""Tool cards: what a model learns about a tool, read from the tool object itself.

The card rules of `awa validate`: T01 (error) a name of 1-64 letters, digits, `_` and `-`;
T03 a description of at least `MIN_DESCRIPTION` characters saying what the tool does and
returns; T05 an object input schema; T13 (error) contradicting MCP hints, or a read-only hint
on a tool awa's risk data says acts outside (server hints never lower a risk).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from awa._internal.upstream.tools import BaseTool
from awa.tools.risk import ToolRisk, infer_tool_risk

TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""A tool name every provider accepts."""

MIN_DESCRIPTION = 40
"""Shortest description (characters) that can say what a tool does and returns."""

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
"""The four MCP tool annotations."""

CardSeverity = Literal["error", "warning", "info"]

_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


@dataclass(frozen=True)
class ToolCard:
    """What a model is told about one tool: name, description, input schema, MCP hints and awa's risk."""

    name: str
    description: str
    input_schema: Mapping[str, Any] | None
    hints: Mapping[str, bool] = field(default_factory=dict)
    risk: ToolRisk | None = None


@dataclass(frozen=True)
class CardFinding:
    """One finding of a card rule."""

    code: str
    severity: CardSeverity
    message: str


def card_of(tool: BaseTool, *, risk: ToolRisk | None = None) -> ToolCard:
    """The card of a LangChain tool: name, description, argument schema and MCP hints."""

    schema: Mapping[str, Any] | None
    try:
        schema = tool.tool_call_schema.model_json_schema()  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        raw = getattr(tool, "args_schema", None)
        schema = raw if isinstance(raw, Mapping) else None
    metadata = tool.metadata or {}
    hints = {name: metadata[name] for name in HINTS if isinstance(metadata.get(name), bool)}
    return ToolCard(
        tool.name,
        tool.description or "",
        schema,
        hints,
        risk if risk is not None else infer_tool_risk(tool),
    )


def sentences(text: str) -> int:
    """How many sentences `text` has (ends of `.`, `!` or `?` followed by a space or the end)."""

    stripped = text.strip()
    if not stripped:
        return 0
    count = len(_SENTENCE_END.findall(stripped))
    return count if stripped[-1] in ".!?" else count + 1


def lint_card(card: ToolCard) -> list[CardFinding]:
    """The T01, T03, T05 and T13 findings of one card."""

    found: list[CardFinding] = []
    if not TOOL_NAME.fullmatch(card.name):
        found.append(
            CardFinding(
                "T01",
                "error",
                f"tool name {card.name!r} is not 1-64 letters, digits, '_' or '-'; rename it "
                "so every provider accepts it",
            )
        )
    description = card.description.strip()
    if len(description) < MIN_DESCRIPTION:
        found.append(
            CardFinding(
                "T03",
                "error",
                f"tool {card.name!r} has {'no' if not description else 'a too short'} "
                "description; say what it does, what it returns and its limits",
            )
        )
    elif sentences(description) < 3:
        found.append(
            CardFinding(
                "T03",
                "info",
                f"the description of tool {card.name!r} has fewer than three sentences; say "
                "what it does, what it returns and its limits",
            )
        )
    schema = card.input_schema
    if schema is not None:
        if schema.get("type") != "object":
            found.append(
                CardFinding(
                    "T05",
                    "error",
                    f"the input schema of tool {card.name!r} is not an object; take named "
                    "arguments",
                )
            )
        elif schema.get("additionalProperties") is not False:
            found.append(
                CardFinding(
                    "T05",
                    "info",
                    f"the input schema of tool {card.name!r} allows unknown arguments; set "
                    "additionalProperties: false so it can be sent in strict mode",
                )
            )
    hints = card.hints
    if hints.get("readOnlyHint") is True and hints.get("destructiveHint") is True:
        found.append(
            CardFinding(
                "T13",
                "error",
                f"tool {card.name!r} is annotated both read-only and destructive; correct the "
                "server's annotations",
            )
        )
    elif (
        hints.get("readOnlyHint") is True
        and card.risk is not None
        and card.risk.external_action is True
    ):
        found.append(
            CardFinding(
                "T13",
                "error",
                f"tool {card.name!r} is annotated read-only, but awa's risk data says it acts "
                "outside; the annotation is not trusted, check the tool or declare its risk "
                "under tool_risks",
            )
        )
    return found
