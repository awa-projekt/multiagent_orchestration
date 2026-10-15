"""`awa new process`: turn a process into a blueprint by answering a few questions.

It asks for the name, the agents and their tasks, the structure, the orchestrator or lead,
and the model (every answer can be a flag instead), writes the shortest blueprint with
`auto: true`, asks for triggers when interactive, and prints what each agent gets.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO, cast

from awa._internal.serialize import dump_yaml
from awa.blueprints.auto import AutoReport
from awa.blueprints.schema import BLUEPRINT_FORMAT_VERSION, Blueprint, Structure
from awa.blueprints.triggers import Step, add_trigger, ask_trigger, trigger_steps
from awa.errors import ConfigurationError, ScaffoldError

STRUCTURES: tuple[Structure, ...] = ("team", "workflow", "network")
"""The structures the wizard offers, the default first."""

Ask = Callable[[str], str]
"""Reads one answer: `input` in a terminal."""


def process_blueprint(
    name: str,
    agents: Mapping[str, str],
    *,
    structure: Structure = "team",
    lead: str | None = None,
    model: str | None = None,
    auto: bool = True,
) -> str:
    """Return the shortest blueprint YAML for a process: its agents, structure and model.

    Args:
        agents: Agent name to task, in order (the workflow's step order).
        lead: The orchestrator of a team or the lead of a network (`"lead"` by default); one
            that is not one of `agents` coordinates with a generated task.
        model: Model profile or `provider:model`; `None` or `"default"` uses the default profile.

    Raises:
        ConfigurationError: If a name or task is invalid.
    """

    from awa.quick import blueprint

    if structure not in STRUCTURES:
        raise ConfigurationError(
            f"Unknown structure {structure!r}; use one of {', '.join(STRUCTURES)}"
        )
    for agent, task in agents.items():
        if not task.strip():
            raise ConfigurationError(f"Agent {agent!r} needs a task")
    full = blueprint(
        structure,
        name,
        dict(agents),
        lead=lead if structure != "workflow" else None,
        model=None if model in (None, "", "default") else model,
        auto=auto,
    )
    document: dict[str, Any] = {"format_version": BLUEPRINT_FORMAT_VERSION, "name": full.name}
    if full.description:
        document["description"] = full.description
    if auto:
        document["auto"] = True
    if full.defaults.model is not None:
        document["defaults"] = {"model": full.defaults.model}
    document["agents"] = {spec.name: spec.description for spec in full.agents}
    if full.team is not None:
        document["team"] = full.team.orchestrator
    elif full.network is not None:
        document["network"] = full.network.lead
    else:
        document["workflow"] = [spec.name for spec in full.agents]
    text = dump_yaml(document)
    Blueprint.from_yaml_text(text)  # the short form must read back as the same process
    return text


def create_process(
    path: str | Path | None,
    name: str,
    agents: Mapping[str, str],
    *,
    structure: Structure = "team",
    lead: str | None = None,
    model: str | None = None,
    auto: bool = True,
    force: bool = False,
) -> tuple[Path, AutoReport]:
    """Write `process_blueprint(...)` to `path` (default `<system name>.yaml`); return it and the auto report.

    Raises:
        ConfigurationError: If a name or task is invalid.
        ScaffoldError: If the file exists and `force` is false.
    """

    text = process_blueprint(name, agents, structure=structure, lead=lead, model=model, auto=auto)
    target = Path(path) if path is not None else Path(f"{Blueprint.from_yaml_text(text).name}.yaml")
    if target.exists() and not force:
        raise ScaffoldError(f"{target} already exists; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, Blueprint.from_yaml_file(target).explain()


def run_wizard(args: argparse.Namespace, *, ask: Ask | None, out: TextIO) -> int:
    """Complete the answers of `awa new process` (asking with `ask`, if given) and write it.

    Raises:
        ConfigurationError: If an answer is missing and cannot be asked for, or invalid.
        ScaffoldError: If the file exists and `--force` is not given.
    """

    name = args.name or _required(ask, "Process name: ", "a process name")
    agents = _parse_agents(args.agent)
    if not agents:
        if ask is None:
            raise ConfigurationError('Give the agents with --agent "name=task" (repeatable)')
        agents = _ask_agents(ask, out)
    structure: Structure = args.structure or _choice(ask, "Structure", STRUCTURES)
    lead = args.lead
    if lead is None and structure != "workflow" and ask is not None:
        known = ", ".join(agents)
        role = "Orchestrator" if structure == "team" else "Lead"
        lead = ask(f"{role} (one of {known}, or a new name) [lead]: ").strip() or None
    model = args.model
    if model is None and ask is not None:
        model = ask("Model (profile or provider:model) [default]: ").strip() or None
    written, report = create_process(
        args.output,
        name,
        agents,
        structure=structure,
        lead=lead,
        model=model,
        auto=args.auto,
        force=args.force,
    )
    out.write(f"wrote {written}\n\n{report}\n\n")
    started = _ask_triggers(ask, out, written)
    steps = [f"awa show {written}", f'awa run {written} "<a task for the {structure}>"']
    out.write("next:\n" + "".join(f"  {s}\n" for s in [*steps, *(i.command for i in started)]))
    if not started:
        out.write(
            f"  awa add trigger {written}   # start it by itself: schedule, web request, ...\n"
        )
    return 0


def _ask_triggers(ask: Ask | None, out: TextIO, written: Path) -> list[Step]:
    """Ask what should start the process (never without `ask`), write the answers, give the steps."""

    if ask is None:
        return []
    taken: list[str] = []
    while (entry := ask_trigger(ask, out, taken=taken)) is not None:
        add_trigger(written, entry)
        taken.append(entry["name"])
        out.write(f"added trigger {entry['name']!r}\n")
    return trigger_steps(Blueprint.from_yaml_file(written), str(written))


def _parse_agents(entries: Sequence[str]) -> dict[str, str]:
    agents: dict[str, str] = {}
    for entry in entries:
        name, separator, task = entry.partition("=")
        if not separator:
            name, separator, task = entry.partition(":")
        if not separator or not name.strip() or not task.strip():
            raise ConfigurationError(f"Give an agent as NAME=TASK, got {entry!r}")
        if name.strip() in agents:
            raise ConfigurationError(f"Agent {name.strip()!r} is given twice")
        agents[name.strip()] = task.strip()
    return agents


def _ask_agents(ask: Ask, out: TextIO) -> dict[str, str]:
    out.write("Agents: one per line as `name: task`; an empty line ends the list.\n")
    lines: list[str] = []
    while True:
        line = ask("  agent> ").strip()
        if not line:
            if lines:
                break
            out.write("  add at least one agent\n")
            continue
        try:
            _parse_agents([*lines, line])
        except ConfigurationError as exc:
            out.write(f"  {exc}\n")
            continue
        lines.append(line)
    return _parse_agents(lines)


def _required(ask: Ask | None, prompt: str, what: str) -> str:
    if ask is None:
        raise ConfigurationError(f"Give {what}, or run the command in a terminal to be asked")
    while True:
        answer = ask(prompt).strip()
        if answer:
            return answer


def _choice(ask: Ask | None, label: str, options: Sequence[Structure]) -> Structure:
    if ask is None:
        return options[0]
    listing = "/".join(options)
    while True:
        answer = ask(f"{label} [{listing}] ({options[0]}): ").strip().lower() or options[0]
        if answer in options:
            return cast("Structure", answer)


__all__ = ["STRUCTURES", "Ask", "create_process", "process_blueprint", "run_wizard"]
