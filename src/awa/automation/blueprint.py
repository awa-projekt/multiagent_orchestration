"""Automations in blueprints: the `triggers:` and `automation:` sections and `build_automation`.

Secrets are only named (`*_env`), never written into the file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from awa._internal.util import to_timedelta
from awa.automation.automation import Automation
from awa.automation.models import SLA, RetryPolicy
from awa.automation.notify import Notifier, WebhookNotifier
from awa.automation.queues import EventQueue, InMemoryEventQueue, QueueTrigger
from awa.automation.store import RunStore
from awa.automation.triggers import FileDrop, Mailbox, Schedule, Webhook
from awa.automation.versioning import blueprint_digest
from awa.errors import ConfigurationError
from awa.runtime.clock import Clock

if TYPE_CHECKING:
    from awa.blueprints import Blueprint
    from awa.blueprints._automation import (
        AutomationBlueprint,
        QueueBlueprint,
        RetryBlueprint,
        TriggerBlueprint,
    )
    from awa.contracts.protocols import AgentLike
    from awa.contracts.usage import UsageLimits
    from awa.observability.trace_store import TraceLevel, TraceStore
    from awa.persistence.runlog import RunLog


def split_automation(data: Mapping[str, Any]) -> tuple[dict[str, Any], AutomationBlueprint | None]:
    """Separate the `triggers:` and `automation:` sections from the rest of a blueprint.

    Returns:
        The blueprint data without them, and the parsed section (`None` if absent).

    Raises:
        ConfigurationError: If the sections are invalid.
    """

    from awa.blueprints._automation import AutomationBlueprint

    rest = dict(data)
    triggers = rest.pop("triggers", None)
    section = rest.pop("automation", None)
    if triggers is None and section is None:
        return rest, None
    merged: dict[str, Any] = dict(section or {})
    if triggers is not None:
        if merged.get("triggers"):
            raise ConfigurationError(
                "Declare triggers at the top level or under automation, not both"
            )
        merged["triggers"] = triggers
    try:
        return rest, AutomationBlueprint.model_validate(merged)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'automation'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigurationError(f"Invalid automation section: {problems}") from None


def load_automation_blueprint(
    path: str | os.PathLike[str],
) -> tuple[Blueprint, AutomationBlueprint | None]:
    """Read a blueprint file and its `triggers:` / `automation:` sections.

    Raises:
        BlueprintError: If the file cannot be read or the blueprint is invalid.

    Example:
        ```yaml
        triggers:
          - name: nightly-dunning
            schedule: {cron: "0 6 * * MON-FRI", timezone: Europe/Berlin}
            input: "Run the dunning check for {payload.date}."
          - name: supplier-invoice
            webhook: {secret_env: INVOICE_HOOK_SECRET}
            dedup: "{payload.invoice_no}"
        automation:
          retry: {max_attempts: 4, initial: 1m}
          sla: {complete_within: 2d, approval_within: 4h}
        ```
    """

    from awa.blueprints import Blueprint

    blueprint = Blueprint.from_yaml_file(path)
    return blueprint, blueprint.automation_section()


def build_triggers(
    spec: AutomationBlueprint,
    *,
    base_dir: str | os.PathLike[str] | None = None,
    dsn: str | None = None,
    queues: Mapping[str, EventQueue] | None = None,
) -> list[Any]:
    """The trigger objects of an automation section.

    Args:
        base_dir: Directory relative `file_drop` directories are resolved against.
        dsn: PostgreSQL DSN for `queue` triggers (backend `postgres`, or `auto`).
        queues: Queues by name, overriding the backend.

    Raises:
        ConfigurationError: If a Postgres queue has no DSN.
    """

    base = Path(base_dir) if base_dir is not None else Path.cwd()
    return [_trigger(item, base, dsn, queues) for item in spec.triggers if item.enabled]


def build_automation(
    blueprint: Blueprint | AutomationBlueprint | Mapping[str, Any],
    system: AgentLike,
    *,
    spec: AutomationBlueprint | None = None,
    store: RunStore | None = None,
    dsn: str | None = None,
    base_dir: str | os.PathLike[str] | None = None,
    queues: Mapping[str, EventQueue] | None = None,
    notify: Sequence[Notifier] = (),
    audit_log: Any = None,
    previous_versions: Mapping[str, AgentLike] | None = None,
    version: str | None = None,
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
    trace: TraceStore | TraceLevel | None = None,
    run_log: RunLog | None = None,
    capture: bool = False,
    clock: Clock | None = None,
) -> Automation:
    """Build the `Automation` a blueprint describes, for the system built from it.

    Args:
        blueprint: A `Blueprint` (with an `automation` field, or pass `spec`), an
            `AutomationBlueprint`, or raw section data.
        system: The built system (`blueprint.build(...)`).
        base_dir: Base of relative `file_drop` directories (the blueprint's directory).
        notify: Extra notifiers besides the section's webhooks.
        version: Version of the system (default: the blueprint's digest).
        usage_limits: The limits of one process run (default: the blueprint's `limits:`); its
            `prices:` record the cost either way.

    Other arguments are those of `Automation`.

    Raises:
        ConfigurationError: If the blueprint has no automation section or it is invalid.

    Example:
        ```python
        blueprint, spec = load_automation_blueprint("invoices.yaml")
        automation = build_automation(blueprint, blueprint.build(checkpointer=saver), spec=spec,
                                      store=PostgresRunStore(dsn))
        ```
    """

    from awa.blueprints import Blueprint
    from awa.blueprints._automation import AutomationBlueprint

    section = spec or _section_of(blueprint)
    name_default: str | None = None
    digest = version
    if hasattr(blueprint, "to_dict") and not isinstance(blueprint, AutomationBlueprint):
        name_default = getattr(blueprint, "name", None)
        if digest is None:
            digest = blueprint_digest(blueprint, extra=section.model_dump(mode="json"))
    notifiers: list[Notifier] = [
        WebhookNotifier(item.webhook, secret_env=item.secret_env, headers=item.headers)
        for item in section.notify
    ]
    notifiers.extend(notify)
    prices = None
    if isinstance(blueprint, Blueprint):
        prices = blueprint.price_table()
        if usage_limits is None:
            usage_limits = blueprint.usage_limits()
    return Automation(
        system,
        name=section.name or name_default,
        triggers=build_triggers(section, base_dir=base_dir, dsn=dsn, queues=queues),
        input=section.input,
        thread_id=section.thread_id,
        dedup=section.dedup,
        dedup_window=section.dedup_window,
        deps=deps,
        concurrency=section.concurrency,
        retry=_retry_policy(section.retry),
        sla=SLA(**section.sla.model_dump()) if section.sla is not None else None,
        store=store,
        version=digest,
        previous_versions=previous_versions,
        migration=section.migration,
        notify=notifiers,
        audit_log=audit_log,
        run_timeout=_seconds(section.run_timeout),
        usage_limits=usage_limits,
        prices=prices,
        trace=trace,
        tenant=section.tenant,
        run_log=run_log,
        capture=capture,
        clock=clock,
    )


def _section_of(
    blueprint: Blueprint | AutomationBlueprint | Mapping[str, Any],
) -> AutomationBlueprint:
    from awa.blueprints._automation import AutomationBlueprint

    section: AutomationBlueprint | None
    if isinstance(blueprint, AutomationBlueprint):
        section = blueprint
    elif isinstance(blueprint, Mapping):
        wrapped = "automation" in blueprint or "triggers" in blueprint
        _, section = split_automation(blueprint if wrapped else {"automation": blueprint})
    else:
        section = blueprint.automation_section()
    if section is None:
        raise ConfigurationError(
            "The blueprint has no automation: add a `triggers:` list and/or an `automation:` "
            "section"
        )
    return section


def _trigger(
    item: TriggerBlueprint, base: Path, dsn: str | None, queues: Mapping[str, EventQueue] | None
) -> Any:
    templates: dict[str, Any] = {
        "input": item.input,
        "thread_id": item.thread_id,
        "dedup": item.dedup,
    }
    if item.schedule is not None:
        options = item.schedule.model_dump()
        return Schedule(options.pop("cron"), name=item.name, **options, **templates)
    if item.webhook is not None:
        return Webhook(item.name, **item.webhook.model_dump(), **templates)
    if item.file_drop is not None:
        options = item.file_drop.model_dump()
        directory = Path(options.pop("directory"))
        if not directory.is_absolute():
            directory = base / directory
        return FileDrop(directory, name=item.name, **options, **templates)
    if item.mailbox is not None:
        options = item.mailbox.model_dump()
        if options["attachments_dir"] is not None:
            options["attachments_dir"] = base / options["attachments_dir"]
        return Mailbox(name=item.name, **options, **templates)
    spec = item.queue
    assert spec is not None  # a trigger has exactly one source
    return QueueTrigger(
        _queue(item.name, spec, dsn, queues),
        name=item.name,
        batch=spec.batch,
        visibility=spec.visibility or timedelta(minutes=5),
        **templates,
    )


def _queue(
    trigger: str, spec: QueueBlueprint, dsn: str | None, queues: Mapping[str, EventQueue] | None
) -> EventQueue:
    if queues is not None and spec.name in queues:
        return queues[spec.name]
    if spec.backend == "memory" or (spec.backend == "auto" and not dsn):
        return InMemoryEventQueue(max_attempts=spec.max_attempts)
    if not dsn:
        raise ConfigurationError(
            f"Trigger {trigger!r} reads the PostgreSQL queue {spec.name!r}; "
            "pass a DSN (--dsn or AWA_POSTGRES_DSN)"
        )
    from awa.automation._postgres import PostgresEventQueue

    return PostgresEventQueue(dsn, name=spec.name, max_attempts=spec.max_attempts)


def _retry_policy(section: RetryBlueprint) -> RetryPolicy:
    options = section.model_dump(exclude={"retry_on"})
    if section.retry_on is not None:
        options["retry_on"] = tuple(section.retry_on)
    return RetryPolicy(**options)


def _seconds(value: Any) -> float | None:
    duration = to_timedelta(value, what="run_timeout")
    return duration.total_seconds() if duration is not None else None


__all__ = ["build_automation", "build_triggers", "load_automation_blueprint", "split_automation"]
