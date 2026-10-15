"""Process automation (extension): start systems from events and see every run through.

Preview: it works and is tested, but its API may still change in a minor release.

Triggers (`Schedule`, `Webhook`, `FileDrop`, `Mailbox`, `QueueTrigger`) feed an `Automation`, which
keeps each event as a durable `ProcessRun`; a `Worker` runs them with leases, retries and waits.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import lazy_attribute
from awa.automation._postgres import PostgresEventQueue, PostgresRunStore
from awa.automation._sqlite import SqliteRunStore
from awa.automation.models import (
    SLA,
    ProcessEvent,
    ProcessRun,
    RetryPolicy,
    RunStatus,
    TriggerEvent,
)
from awa.automation.notify import Notifier, WebhookNotifier
from awa.automation.queues import EventQueue, InMemoryEventQueue, QueueMessage, QueueTrigger
from awa.automation.store import InMemoryRunStore, RunStore
from awa.automation.triggers import (
    BaseTrigger,
    FileDrop,
    Mailbox,
    PollingTrigger,
    Schedule,
    Trigger,
    TriggerContext,
    Webhook,
)
from awa.automation.waits import wait_for_event

if TYPE_CHECKING:  # loaded on use: they bring the agent and workflow stack
    from awa.automation.automation import Automation
    from awa.automation.blueprint import build_automation, load_automation_blueprint
    from awa.automation.worker import Worker

_HOMES = {
    "Automation": "awa.automation.automation",
    "build_automation": "awa.automation.blueprint",
    "load_automation_blueprint": "awa.automation.blueprint",
    "Worker": "awa.automation.worker",
}

__stability__ = "preview"

__all__ = [
    "SLA",
    "Automation",
    "BaseTrigger",
    "EventQueue",
    "FileDrop",
    "InMemoryEventQueue",
    "InMemoryRunStore",
    "Mailbox",
    "Notifier",
    "PollingTrigger",
    "PostgresEventQueue",
    "PostgresRunStore",
    "ProcessEvent",
    "ProcessRun",
    "QueueMessage",
    "QueueTrigger",
    "RetryPolicy",
    "RunStatus",
    "RunStore",
    "Schedule",
    "SqliteRunStore",
    "Trigger",
    "TriggerContext",
    "TriggerEvent",
    "Webhook",
    "WebhookNotifier",
    "Worker",
    "build_automation",
    "load_automation_blueprint",
    "wait_for_event",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)
