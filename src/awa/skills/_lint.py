"""Lint a skill against the Agent Skills authoring rules, offline."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Literal

from awa.errors import SkillReferenceNotFoundError
from awa.skills.skill import Skill

MAX_BODY_LINES = 500
"""Longest SKILL.md body the authoring guide recommends."""

TOC_AFTER_LINES = 100
"""Reference files longer than this start with a table of contents."""

_RESERVED = ("anthropic", "claude")
_USE_WHEN = re.compile(r"\bUse (when|whenever|for|at|before|to)\b", re.IGNORECASE)
_FIRST_OR_SECOND_PERSON = re.compile(r"^(I|You|We)\b|\b(I can|you can|we can|I will)\b")
_TIME_SENSITIVE = re.compile(
    r"\b(before|after|until|since|as of)\s+(January|February|March|April|May|June|July|"
    r"August|September|October|November|December\s+)?\s*20\d\d\b(?!-)",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{12,})",
    re.IGNORECASE,
)
_NETWORK = re.compile(
    r"\b(curl|wget|Invoke-WebRequest|nc|netcat|scp)\s+\S|requests\.(get|post)\(|"
    r"\b(send|upload|post|forward)\b[^.\n]{0,40}\b(credentials?|secrets?|api keys?|tokens?)\b",
    re.IGNORECASE,
)
_OVERRIDE = re.compile(
    r"(?<![\"'(])\b(ignore|disregard|bypass|override)\b[^.\n]{0,30}\b(previous|prior|system|safety|"
    r"security|guardrails?)\b[^.\n]{0,20}\b(instructions?|rules?|prompts?|polic(y|ies))\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"\]\(([^)#\s]+\.md)\)|`([\w./-]+\.md)`")

SkillSeverity = Literal["error", "warning"]


@dataclass(frozen=True)
class SkillFinding:
    """One lint finding: a stable rule `code` (`K01` ...) and `severity` `error` or `warning`."""

    skill: str
    code: str
    severity: SkillSeverity
    message: str

    def __str__(self) -> str:
        return f"skill {self.skill}: {self.severity} {self.code}: {self.message}"


def lint_skill(skill: Skill, *, tool_names: frozenset[str] = frozenset()) -> list[SkillFinding]:
    """Lint `skill`, errors first.

    Checks the third-person description, a short body one reference level deep, a table of contents
    in long references, no credentials, network or exfiltration commands, no instruction to ignore
    safety rules, and no names from `tool_names` (snake-case anywhere, one-word names only as code).
    """

    found: list[SkillFinding] = []

    def add(code: str, severity: SkillSeverity, message: str) -> None:
        found.append(SkillFinding(skill.name, code, severity, message))

    if any(word in skill.name for word in _RESERVED):
        add("K01", "error", "the name must not contain 'anthropic' or 'claude'")
    if not _USE_WHEN.search(skill.description):
        add("K02", "warning", 'the description must say when to use the skill ("Use when ...")')
    what = _USE_WHEN.split(skill.description, maxsplit=1)[0]
    if _FIRST_OR_SECOND_PERSON.search(what):
        add("K03", "warning", "write the description in the third person ('Reviews ...')")
    lines = skill.instructions.count("\n") + 1
    if lines > MAX_BODY_LINES:
        add(
            "K04",
            "warning",
            f"the body has {lines} lines; keep it under {MAX_BODY_LINES} and "
            "move details into reference files",
        )
    texts = {"SKILL.md": skill.instructions}
    for resource in skill.resources:
        try:
            text = skill.read_resource(resource)
        except SkillReferenceNotFoundError:  # not UTF-8 text: an image, a binary
            continue
        texts[resource] = text  # scripts and data files are checked for K07-K10 too
        if not resource.endswith(".md"):
            continue
        if text.count("\n") + 1 > TOC_AFTER_LINES and not re.search(
            r"^#+\s*(Contents|Table of contents)\b", text, re.IGNORECASE | re.MULTILINE
        ):
            add(
                "K05",
                "warning",
                f"{resource} is longer than {TOC_AFTER_LINES} lines; start "
                "it with a '## Contents' list",
            )
        here = posixpath.dirname(resource)
        links = [m.group(1) or m.group(2) for m in _LINK.finditer(text)]
        # A link is relative to its file (`table.md`) or written from the skill (`references/...`).
        nested = [*links, *(posixpath.normpath(posixpath.join(here, link)) for link in links)]
        if any(link in skill.resources for link in nested):
            add(
                "K06",
                "warning",
                f"{resource} points to another reference file; keep "
                "references one level deep from SKILL.md",
            )
    for where, text in texts.items():
        if _SECRET.search(text):
            add("K07", "error", f"{where} contains what looks like a credential")
        if _NETWORK.search(text):
            add("K08", "error", f"{where} contains a network or data-export command")
        if _OVERRIDE.search(text):
            add(
                "K09",
                "error",
                f"{where} tells the agent to ignore its instructions or safety rules",
            )
        if _TIME_SENSITIVE.search(text):
            add(
                "K10",
                "warning",
                f"{where} contains a time-sensitive statement; describe the current way only",
            )
    named = sorted(
        name
        for name in tool_names
        if re.search(
            rf"\b{re.escape(name)}\b" if "_" in name else rf"`{re.escape(name)}`",
            skill.instructions,
        )
    )
    if named:
        add(
            "K11",
            "warning",
            f"names the tools {', '.join(named)}; a skill holds know-how, the "
            "agent decides which tools it has",
        )
    return sorted(found, key=lambda f: (f.severity != "error", f.code))
