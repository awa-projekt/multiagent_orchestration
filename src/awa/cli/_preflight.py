"""The pre-flight of `run`, `chat`, `eval`, `build`, `worker` and `serve`: print one line per
problem and stop with exit code 78 before anything starts (the model check here, the others in
`awa.core.preflight`)."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa.core.config import env_value
from awa.core.preflight import PreflightReport, Problem, check_api_key_env, check_env, check_store
from awa.core.providers import complete_providers, provider, provider_of

if TYPE_CHECKING:
    from awa.models import ModelRegistry


class PreflightFailed(Exception):
    """The configuration is incomplete; `awa` prints the report and exits with 78."""

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        super().__init__(report.render())


def check_models(
    references: Iterable[str | None], *, registry: ModelRegistry | None = None
) -> list[Problem]:
    """One problem per provider the model `references` name that lacks credentials.

    Profiles registered as an instance or factory and unknown providers are not checked; nothing is
    checked while real model requests are blocked.
    """

    from awa.models import ModelRegistry, model_requests_allowed

    if not model_requests_allowed():
        return []
    registry = registry if registry is not None else ModelRegistry.default()
    settings = registry.settings
    lookup = settings.credential
    users: dict[str, list[str]] = {}
    for reference in dict.fromkeys(ref or "default" for ref in references):
        entry = registry._profiles.get(reference)
        if entry is not None and not isinstance(entry.source, str):
            continue  # a model instance or a factory
        model = entry.source if entry is not None else reference
        assert isinstance(model, str)
        prefix = provider_of(model)
        if prefix is None:
            continue
        label = f"{reference} ({model})" if entry is not None and reference != model else model
        users.setdefault(prefix, []).append(label)
    problems = []
    for prefix, labels in users.items():
        spec = provider(prefix)
        if spec is None or spec.complete(lookup):
            continue
        if any(env_value(name) for name in spec.own_sign_in):
            continue
        missing = spec.missing(lookup)
        others = [item.title for item in complete_providers(lookup)]
        fix = f"set {', '.join(missing)} in .env, or run awa setup"
        if others:
            fix += f" (credentials found for {', '.join(others)}: set AWA_DEFAULT_MODEL to use it)"
        what = f"no credentials for {spec.title} (model {', '.join(labels)})"
        problems.append(Problem("model_key", what, fix))
    return problems


def enforce(report: PreflightReport) -> None:
    """Print the warnings of `report`; raise `PreflightFailed` on a fatal one (not when skipped)."""

    from awa.core.config import preflight_skipped

    if preflight_skipped():
        return
    for problem in report.problems:
        if not problem.fatal:
            print(problem.line(), file=sys.stderr)
    if not report.ok:
        raise PreflightFailed(PreflightReport([p for p in report.problems if p.fatal]))


def model_references(blueprint: str | Path | None) -> list[str | None]:
    """Every model a blueprint names (`model:` anywhere in it), plus the `default` profile when
    an agent names none (or there is no blueprint)."""

    if blueprint is None:
        return [None]
    data = _yaml(Path(blueprint))
    references: list[str | None] = []
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "model" and isinstance(value, str) and value.strip():
                    references.append(value.strip())
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    if not references or _uses_the_default(data):
        references.insert(0, None)
    return references


def _uses_the_default(data: Any) -> bool:
    """Whether an agent of the blueprint has no model of its own and no default model."""

    if not isinstance(data, dict):
        return True
    defaults = data.get("defaults")
    if isinstance(defaults, dict) and isinstance(defaults.get("model"), str):
        return False
    agents = data.get("agents")
    items = list(agents.values()) if isinstance(agents, dict) else agents
    if not isinstance(items, list) or not items:
        return True
    return any(not (isinstance(item, dict) and item.get("model")) for item in items)


def trigger_env(blueprint: str | Path) -> dict[str, str]:
    """The variables the blueprint's triggers read (`required_env` of each), with the trigger
    that needs it. A blueprint without triggers, or one that cannot be loaded, needs none here
    (loading reports its own errors)."""

    from awa.automation.blueprint import build_triggers, load_automation_blueprint
    from awa.automation.queues import InMemoryEventQueue
    from awa.errors import AwaError

    path = Path(blueprint)
    try:
        _, spec = load_automation_blueprint(path)
    except (AwaError, OSError, ValueError):
        return {}
    if spec is None or not spec.triggers:
        return {}
    queues = {item.queue.name: InMemoryEventQueue() for item in spec.triggers if item.queue}
    needed: dict[str, str] = {}
    for trigger in build_triggers(spec, base_dir=path.resolve().parent, queues=queues):
        required = getattr(trigger, "required_env", None)
        for name in required() if callable(required) else ():
            needed.setdefault(name, f"trigger {trigger.name}")
    return needed


def preflight(
    blueprint: str | Path | None,
    *,
    models: bool = True,
    triggers: bool = False,
    dsn: str | None = None,
    api_key_env: str | None = None,
    extra_models: Iterable[str | None] = (),
    registry: ModelRegistry | None = None,
) -> None:
    """Check what a command needs and stop it (exit 78) when something is missing.

    Args:
        models: Check the credentials of the blueprint's models.
        triggers: Check the variables of its triggers.
        dsn: A database to reach (worker and serve with a DSN).
        api_key_env: The variable `--api-key-env` names.
        extra_models: More model references (for example `awa build --model`).
    """

    problems: list[Problem] = []
    problems += check_api_key_env(api_key_env)
    if models:
        problems += check_models([*model_references(blueprint), *extra_models], registry=registry)
    if triggers and blueprint is not None and _is_blueprint(blueprint):
        problems += check_env(trigger_env(blueprint))
    if dsn:
        from awa.persistence.sql import connect_once

        problems += check_store(dsn, connect=connect_once)
    enforce(PreflightReport(problems))


def _is_blueprint(path: str | Path) -> bool:
    candidate = Path(path)
    return candidate.suffix.lower() in {".yaml", ".yml", ".json"} and candidate.is_file()


def _yaml(path: Path) -> Any:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
