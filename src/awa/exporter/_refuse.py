"""What the export does not carry over: every refusal, found before anything is rendered.

A blueprint is exported whole or not at all. `refusals` reads the blueprint (and the Python
files beside it) and returns one `Finding` per feature the export cannot express with plain
LangChain and LangGraph, each saying what to do. Nothing here builds, imports or runs the
project.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

from awa.blueprints import Blueprint
from awa.blueprints.schema import AgentBlueprint
from awa.exporter._report import Finding
from awa.models import ModelSettings

ALLOWED_TOOL_IMPORTS = frozenset(
    {"langchain", "langchain_core", "langgraph", "pydantic", "typing_extensions"}
)
"""Third-party packages a project's `tools.py` may import: the export depends on them anyway."""

_STRUCTURES = {
    "team": "the coordinator, its members and their delegation are not exported in this release",
    "workflow": "the sequential, parallel, loop and map steps are not exported in this release",
    "process": "the process steps and their checks are not exported in this release",
    "network": "agent networks need awa's message bus and are not exported",
}
_AGENT_FEATURES = {
    "role": "role trees (judges, reviewers, sub-agents) are not exported",
    "sub_agents": "role trees (judges, reviewers, sub-agents) are not exported",
    "judges": "role trees (judges, reviewers, sub-agents) are not exported",
    "reviews": "role trees (judges, reviewers, sub-agents) are not exported",
    "memory": "long-term memory is awa-only; keep the project as an awa project, or use a "
    "LangGraph store in the exported code",
    "refine": "provider fields and middleware per model attempt are awa-only",
    "refinements": "post-build refinements are awa-only; write the lines into the instructions",
    "describe": "tool descriptions and instruction files kept apart are awa-only; write the "
    "text into the blueprint",
    "delegation": "delegation limits belong to teams, which are not exported",
    "documents": "documents in the instructions need awa's file readers; leave them out",
}
_SETTINGS = {
    "effort": "set the provider's reasoning option under extra",
    "retry": "retries of model calls are awa-only; configure the provider client",
    "fallbacks": "model fallbacks are awa-only; add them with plain LangChain after the export",
    "circuit_breaker": "the circuit breaker is awa-only",
}
_ADVICE = {
    "builtin:": "builtin tools need awa; write a project tool in tools.py or attach an MCP server",
    "connector:": "connector tools need the awa connectors; write a project tool in tools.py",
    "mcp:": "MCP servers are not exported in this release; write a project tool in tools.py",
    "plugin:": "plugin tools need awa; write a project tool in tools.py",
}


def refusals(blueprint: Blueprint, folder: Path) -> list[Finding]:
    """Every feature of `blueprint` the export refuses, in blueprint order.

    Args:
        blueprint: The loaded blueprint.
        folder: The project folder (the Python files beside the blueprint are scanned).
    """

    found: list[Finding] = []
    structure = [name for name in _STRUCTURES if getattr(blueprint, name) is not None]
    found += [Finding(name, _STRUCTURES[name]) for name in structure]
    if len(blueprint.agents) != 1 and not structure:
        found.append(Finding("agents", "export a blueprint with exactly one agent"))
    for spec in blueprint.agents:
        found += _agent(blueprint, spec)
    found += _project(blueprint)
    found += _tool_files(folder)
    return found


def _agent(blueprint: Blueprint, spec: AgentBlueprint) -> Iterator[Finding]:
    where = f"agents.{spec.name}"
    if blueprint.uses_auto() and (spec.auto if spec.auto is not None else blueprint.auto):
        yield Finding(
            f"{where}.auto",
            "automatic tool and skill picks are decided at build time; list them in the "
            "blueprint (awa show names them) and set auto: false",
        )
    for name, advice in _AGENT_FEATURES.items():
        value = getattr(spec, name)
        if value is not None and value != [] and value != "":
            yield Finding(f"{where}.{name}", advice)
    base = blueprint.defaults.model_settings
    settings: ModelSettings | None = (
        base.merge(spec.model_settings) if base is not None else spec.model_settings
    )
    if settings is not None:
        for name, advice in _SETTINGS.items():
            if getattr(settings, name):
                yield Finding(f"{where}.model_settings.{name}", advice)
    for reference in spec.tools:
        for prefix, advice in _ADVICE.items():
            if reference.startswith(prefix):
                yield Finding(f"{where}.tools.{reference}", advice)
                break
        else:
            if ":" in reference:
                yield Finding(
                    f"{where}.tools.{reference}",
                    "import references are not exported; list the function in tools.py",
                )


def _project(blueprint: Blueprint) -> Iterator[Finding]:
    if blueprint.connectors:
        yield Finding("connectors", "connectors need awa; write the calls as project tools")
    if blueprint.mcp_servers:
        yield Finding("mcp_servers", "MCP servers are not exported in this release")
    if blueprint.data:
        yield Finding("data", "data files need awa's readers; read them inside a project tool")
    if blueprint.special_cases:
        yield Finding(
            "special_cases", "special-case tables are awa-only; write them into the prompt"
        )
    if blueprint.components:
        yield Finding("components", "component switches are awa-only; remove the section")
    if blueprint.limits is not None and blueprint.limits.max_cost is not None:
        yield Finding("limits.max_cost", "cost limits need awa's price table; use a token limit")
    for index, approval in enumerate(blueprint.approvals):
        if approval.when is not None and approval.condition is None:
            yield Finding(
                f"approvals[{index}].when",
                "an import reference is not exported; write the condition as an expression",
            )
    for index, policy in enumerate(blueprint.policies):
        if policy.ask:
            yield Finding(
                f"policies[{index}].ask", "risk-based approvals are awa-only; use approvals"
            )
        if policy.use is not None:
            yield Finding(f"policies[{index}].use", "custom tool policies are awa-only")


def _tool_files(folder: Path) -> Iterator[Finding]:
    """The imports of the project's `tools.py` that the export cannot satisfy."""

    path = folder / "tools.py"
    if not path.is_file():
        return
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        yield Finding("tools.py", f"cannot be read: {exc}")
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = ["." * node.level + (node.module or "")]
        else:
            continue
        for module in modules:
            root = module.split(".")[0]
            if module.startswith(".") or not (
                root in sys.stdlib_module_names or root in ALLOWED_TOOL_IMPORTS
            ):
                yield Finding(
                    f"tools.py:{node.lineno}",
                    f"imports {module!r}, which the export does not carry; "
                    "keep tools.py to the standard library and LangChain",
                )
