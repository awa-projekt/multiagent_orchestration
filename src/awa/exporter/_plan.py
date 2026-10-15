"""The plan of an export: what awa decides at build time, frozen as plain data.

`build_plan` reads the validated blueprint (never the awa runtime) and keeps what the exported
code needs: the final system prompt, the model, the tool and skill names, the approval gates,
the limits and the output schema. Everything the export refuses is found first
(`awa.exporter._refuse`), so a plan always describes something the renderer can write.
"""

from __future__ import annotations

import keyword
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awa.blueprints import (
    Blueprint,
    BlueprintError,
    ToolCatalog,
    effective_instructions,
    effective_skills,
    load_tools,
    model_from_json_schema,
    tools_files,
)
from awa.exporter import _schema_code
from awa.exporter._refuse import refusals
from awa.exporter._report import ExportError, ExportUnsupportedError, Finding
from awa.models import ModelRegistry, ModelSettings
from awa.skills import SkillRegistry, compose_prompt

PROVIDER_PACKAGES = {
    "anthropic": "langchain-anthropic",
    "azure_openai": "langchain-openai",
    "bedrock": "langchain-aws",
    "bedrock_converse": "langchain-aws",
    "cohere": "langchain-cohere",
    "google_genai": "langchain-google-genai",
    "google_vertexai": "langchain-google-vertexai",
    "groq": "langchain-groq",
    "mistralai": "langchain-mistralai",
    "ollama": "langchain-ollama",
    "openai": "langchain-openai",
}
"""The LangChain integration package of each provider prefix of a `provider:model` string."""

_RESERVED = frozenset(
    {"langchain", "langchain_core", "langgraph", "pydantic", "pytest", "tests", "src", "run"}
    | {"dotenv", "typing_extensions", "google", "boto3"}
    | set(PROVIDER_PACKAGES)
    | {package.replace("-", "_") for package in PROVIDER_PACKAGES.values()}
)
"""Import names an export depends on: a package of that name would shadow them."""


@dataclass(frozen=True)
class Limits:
    """The run limits of the blueprint, `None` when not set."""

    max_requests: int | None = None
    max_tool_calls: int | None = None
    max_total_tokens: int | None = None
    max_duration: float | None = None


@dataclass(frozen=True)
class Plan:
    """Everything the renderer writes for one single-agent export.

    Attributes:
        source: The blueprint file.
        package: Import name of the exported package.
        name: Blueprint name.
        version: Blueprint version.
        description: Blueprint description.
        agent: The agent's name.
        prompt: The final system prompt, skills included.
        model: The `provider:model` id, or `None` when the profile cannot be resolved.
        model_kwargs: Construction settings of the model (`temperature`, `max_tokens`, ...).
        max_steps: Model calls one run may make.
        tools: Names of the agent's project tools, in order.
        tool_file: The project's `tools.py`, or `None`.
        skills: Names of the agent's skills (their folders are copied).
        skill_loader: Whether the agent gets `load_skill` (every mode but `inline`).
        skill_folders: The folder of each skill in `skills`.
        gates: Tool name to the approval condition (`None`: every call needs approval).
        compaction: `(max_tokens, keep_last_messages)` or `None`.
        output_schema: The JSON schema of the structured output, or `None`.
        limits: The run limits.
        sample_calls: Valid arguments of each unconditionally gated tool, for the generated tests.
        sample_output: A valid structured output for the generated tests, or `None`.
        warnings: Settings the export leaves out, listed in the README.
    """

    source: Path
    package: str
    name: str
    version: str
    description: str
    agent: str
    prompt: str
    model: str | None
    model_kwargs: dict[str, Any]
    max_steps: int
    tools: tuple[str, ...]
    tool_file: Path | None
    skills: tuple[str, ...]
    skill_loader: bool
    skill_folders: dict[str, Path]
    gates: dict[str, str | None]
    compaction: tuple[int, int] | None
    output_schema: dict[str, Any] | None
    limits: Limits
    sample_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    sample_output: Any = None
    warnings: tuple[str, ...] = ()

    @property
    def provider(self) -> str | None:
        """The LangChain package of the model's provider, or `None`."""

        return PROVIDER_PACKAGES.get((self.model or "").partition(":")[0])


def blueprint_file(source: Path) -> Path:
    """The blueprint file of `source`, a project folder or the file itself.

    Raises:
        ExportError: If there is no blueprint.
    """

    path = source / "blueprint.yaml" if source.is_dir() else source
    if not path.is_file():
        raise ExportError(f"no blueprint at {path}")
    return path


def package_name(name: str) -> str:
    """The import name for a blueprint name: lowercase, `_` for anything else.

    Raises:
        ExportError: If the result is empty, a keyword, starts with a digit or would shadow a
            standard library or LangChain package.
    """

    package = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if (
        not package
        or package[0].isdigit()
        or keyword.iskeyword(package)
        or package in sys.stdlib_module_names
        or package in _RESERVED
    ):
        raise ExportError(
            f"the blueprint name {name!r} cannot name a package ({package!r}); rename the blueprint"
        )
    return package


def build_plan(source: Path) -> Plan:
    """Read the blueprint at `source` and plan its export.

    Raises:
        ExportUnsupportedError: If the blueprint uses anything the export does not carry over.
        BlueprintError: If the blueprint is invalid or its tools cannot be loaded.
        ExportError: If the project name cannot name a package.
    """

    path = blueprint_file(source)
    folder = path.parent
    blueprint = Blueprint.from_yaml(path)
    findings = refusals(blueprint, folder)
    findings += _schema_findings(blueprint)
    if findings:
        raise ExportUnsupportedError(path, findings)
    spec = blueprint.agents[0]
    files = tools_files([], path)
    tools = load_tools(files) if files else []
    skills_folder = folder / "skills"
    registry = (
        SkillRegistry.from_directory(skills_folder, include_builtin=True)
        if skills_folder.is_dir()
        else SkillRegistry.builtin()
    )
    errors = [
        issue
        for issue in blueprint.check(tools=tools, skills=registry)
        if issue.severity == "error"
    ]
    if errors:
        raise BlueprintError(f"Blueprint {blueprint.name!r} cannot be built", errors)
    catalog = ToolCatalog.from_source(tools)
    denied = {
        name
        for policy in blueprint.policies
        if policy.applies_to(spec.name)
        for name in policy.deny
    }
    names = tuple(dict.fromkeys(t for t in spec.tools if t not in denied))
    skill_names = tuple(effective_skills(blueprint, spec, catalog))
    mode = spec.skill_mode or blueprint.defaults.skill_mode
    base = blueprint.defaults.model_settings
    settings: ModelSettings | None = (
        base.merge(spec.model_settings) if base is not None else spec.model_settings
    )
    schema = spec.output_schema
    return Plan(
        source=path,
        package=package_name(blueprint.name),
        name=blueprint.name,
        version=blueprint.version,
        description=blueprint.description,
        agent=spec.name,
        prompt=_prompt(
            effective_instructions(blueprint, spec, catalog), skill_names, mode, registry
        ),
        model=_model(spec.model or blueprint.defaults.model),
        model_kwargs=settings.to_kwargs() if settings is not None else {},
        max_steps=spec.max_steps or blueprint.defaults.max_steps,
        tools=names,
        tool_file=folder / "tools.py" if files else None,
        skills=skill_names,
        skill_loader=mode != "inline",
        skill_folders={n: _folder(registry, n) for n in skill_names},
        gates=_gates(blueprint, names),
        compaction=(
            (spec.compaction.max_tokens, spec.compaction.keep_last_messages)
            if spec.compaction
            else None
        ),
        output_schema=schema,
        limits=_limits(blueprint),
        sample_calls=_sample_calls(blueprint, names, catalog),
        sample_output=_sample_output(schema),
        warnings=_warnings(blueprint),
    )


def _schema_findings(blueprint: Blueprint) -> list[Finding]:
    return [
        Finding(
            f"agents.{spec.name}.output_schema",
            f"the property {name!r} is a Python keyword and cannot become a field; rename it",
        )
        for spec in blueprint.agents
        if spec.output_schema is not None
        for name in _schema_code.keywords(spec.output_schema)
    ]


def _prompt(text: str, skills: tuple[str, ...], mode: str, registry: SkillRegistry) -> str:
    if not skills:
        return text
    if mode == "inline":
        return compose_prompt(text, list(skills), registry)
    index = registry.render_index(list(skills))
    return f"{text}\n\n{index}" if text else index


def _folder(registry: SkillRegistry, name: str) -> Path:
    source = registry.get(name).source_path
    if source is None:
        raise ExportError(f"the skill {name!r} has no folder to copy")
    return source.parent


def _model(reference: str | None) -> str | None:
    name = reference or "default"
    if ":" in name:
        return name
    settings = ModelRegistry.default().settings
    resolved = {
        "default": settings.default_model,
        "fast": settings.fast_model,
        "strong": settings.strong_model,
    }.get(name)
    return resolved if resolved and ":" in resolved else None


def _gates(blueprint: Blueprint, tools: tuple[str, ...]) -> dict[str, str | None]:
    gates: dict[str, str | None] = {}
    for approval in blueprint.approvals:
        if approval.tool not in tools:
            continue
        condition = approval.condition
        if approval.tool not in gates:
            gates[approval.tool] = condition
        elif gates[approval.tool] is not None:
            old = gates[approval.tool]
            gates[approval.tool] = None if condition is None else f"({old}) or ({condition})"
    return gates


def _limits(blueprint: Blueprint) -> Limits:
    limits = blueprint.limits
    if limits is None:
        return Limits()
    return Limits(
        max_requests=limits.max_requests,
        max_tool_calls=limits.max_tool_calls,
        max_total_tokens=limits.max_total_tokens,
        max_duration=limits.usage_limits().max_duration,
    )


def _sample_calls(
    blueprint: Blueprint, tools: tuple[str, ...], catalog: ToolCatalog
) -> dict[str, dict[str, Any]]:
    """Valid arguments for each tool every call of which needs approval (for the tests)."""

    calls: dict[str, dict[str, Any]] = {}
    for name, condition in _gates(blueprint, tools).items():
        schema = catalog.tools[name].get_input_jsonschema()
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        arguments = {key: _schema_code.sample(properties[key]) for key in required}
        if condition is None and all(value is not None for value in arguments.values()):
            calls[name] = arguments
    return calls


def _sample_output(schema: dict[str, Any] | None) -> Any:
    if schema is None:
        return None
    value = _schema_code.sample(schema)
    try:
        model_from_json_schema(schema).model_validate(value)
    except ValueError:
        return None
    return value


def _warnings(blueprint: Blueprint) -> tuple[str, ...]:
    return tuple(
        f"{name} is awa-only and is not part of the export"
        for name in ("triggers", "automation", "tool_risks")
        if getattr(blueprint, name)
    )
