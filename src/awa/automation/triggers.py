"""Triggers: the events that start process runs.

A worker submits every event of every trigger and acknowledges it once its run is safely queued.
Built in: `Schedule`, `Webhook`, `FileDrop`, `Mailbox` and `QueueTrigger`.
"""

from __future__ import annotations

from awa.automation._trigger_base import (
    BaseTrigger,
    PollingTrigger,
    StateStore,
    Trigger,
    TriggerContext,
)
from awa.automation._trigger_files import FileDrop
from awa.automation._trigger_mail import MailAction, Mailbox
from awa.automation._trigger_schedule import CatchUp, Schedule
from awa.automation._trigger_webhook import Webhook, WebhookScheme

__all__ = [
    "BaseTrigger",
    "CatchUp",
    "FileDrop",
    "MailAction",
    "Mailbox",
    "PollingTrigger",
    "Schedule",
    "StateStore",
    "Trigger",
    "TriggerContext",
    "Webhook",
    "WebhookScheme",
]
