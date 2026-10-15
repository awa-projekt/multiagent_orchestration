"""The process facts of an agent and its rendered system message, from the blueprint alone."""

from __future__ import annotations

from awa.blueprints._build_roles import role_rules
from awa.blueprints._resolve import ToolCatalog
from awa.blueprints._risk import agent_tool_risks
from awa.blueprints._schema_agent import AgentBlueprint
from awa.blueprints._schema_process import ProcessStepBlueprint
from awa.blueprints._system_message import (
    TEMPLATE_VERSION,
    MessageFacts,
    RenderedMessage,
    StepPlace,
    render_free_text_message,
    render_system_message,
)
from awa.blueprints.schema import Blueprint


def agent_steps(blueprint: Blueprint, name: str) -> list[ProcessStepBlueprint]:
    """The process steps agent `name` does, in order; a judge or reviewer works in its worker's steps."""

    if blueprint.process is None:
        return []
    names = {name}
    for spec in blueprint.agents:
        if spec.name == name and (spec.judges or spec.reviews):
            names.add(spec.judges or spec.reviews or name)
    return [step for step in blueprint.process.steps if step.agent in names]


def step_place(blueprint: Blueprint, step: ProcessStepBlueprint) -> StepPlace:
    """The place of `step` in the process: position, total and the next step."""

    assert blueprint.process is not None
    steps = blueprint.process.steps
    position = next(i for i, other in enumerate(steps) if other is step)
    following = steps[position + 1] if position + 1 < len(steps) else None
    return StepPlace(position + 1, len(steps), step, following)


def message_facts(
    blueprint: Blueprint, spec: AgentBlueprint, catalog: ToolCatalog | None = None
) -> MessageFacts:
    """What the code knows about `spec`: its first step, documents, gated tools, special cases,
    retries and the worker it reads (judge or reviewer)."""

    steps = agent_steps(blueprint, spec.name)
    step = steps[0] if steps else None
    roles = {entry.name: entry.role for entry in blueprint.data}
    documents = list(dict.fromkeys([*(step.documents if step else []), *spec.documents]))
    gated = tuple(
        reference
        for reference, _risk, is_gated in agent_tool_risks(blueprint, spec, catalog)
        if is_gated
    )
    cases: tuple[str, ...] = ()
    tries: list[str] = []
    if step is not None:
        table = blueprint.special_cases.get(step.name)
        if table is not None:
            cases = tuple(table.describe())
        if step.retry is not None:
            tries.append(f"The step is tried up to {step.retry.max_attempts} times when it fails")
        if step.after_last_try == "ask":
            tries.append("After the last failed try, a person decides")
        elif step.after_last_try.startswith("go:"):
            target = step.after_last_try.partition(":")[2].strip()
            tries.append(f"After the last failed try, the process continues at step {target}")
    return MessageFacts(
        process=blueprint.name,
        place=step_place(blueprint, step) if step is not None else None,
        inputs=tuple((name, roles.get(name, "input")) for name in documents),
        gated_tools=gated,
        special_cases=cases,
        tries=tuple(tries),
        worker=spec.judges or spec.reviews,
    )


def system_message(
    blueprint: Blueprint, spec: AgentBlueprint, catalog: ToolCatalog | None = None
) -> RenderedMessage:
    """The rendered system message: from the sections of a structured agent, else around the verbatim free text."""

    render = render_system_message if renders_sections(spec) else render_free_text_message
    return render(spec, message_facts(blueprint, spec, catalog), role_rules=role_rules(spec))


def renders_sections(spec: AgentBlueprint) -> bool:
    """Whether the system message is rendered from the spec's sections alone: `goal` set, or
    `system` without free-text `instructions`."""

    return bool(spec.goal.strip()) or (spec.system is not None and not spec.instructions.strip())


def effective_instructions(
    blueprint: Blueprint, spec: AgentBlueprint, catalog: ToolCatalog | None = None
) -> str:
    """The system prompt the agent gets: its rendered system message (one skeleton for
    structured and free-text agents)."""

    return system_message(blueprint, spec, catalog).text


def effective_skills(
    blueprint: Blueprint, spec: AgentBlueprint, catalog: ToolCatalog | None = None
) -> list[str]:
    """The agent's skills without those its system message already covers.

    `prompt-injection-awareness` is a house rule of every message; `approval-requests` is left
    out when the agent has gated tools, since the approval is the tool call itself.
    """

    gated = bool(message_facts(blueprint, spec, catalog).gated_tools)
    return [
        name
        for name in spec.skills
        if not (name == "prompt-injection-awareness" or (name == "approval-requests" and gated))
    ]


def trace_attributes(spec: AgentBlueprint) -> dict[str, str]:
    """The agent spec as span attributes of every run of the agent (`awa.agent.*`): role,
    template version (`free_text` for free-text instructions), tools and skills. The built
    agent adds `awa.agent.prompt_hash`."""

    return {
        "awa.agent.role": spec.role or "agent",
        "awa.agent.template": TEMPLATE_VERSION if renders_sections(spec) else "free_text",
        "awa.agent.tools": ",".join(spec.tools),
        "awa.agent.skills": ",".join(spec.skills),
    }
