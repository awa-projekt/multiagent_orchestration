"""The `triggers:` and `automation:` sections of a blueprint (validated data only).

`awa.automation.build_automation` turns them into triggers and an `Automation`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from awa.blueprints._schema_spec import _Spec
from awa.core.config import env_value

_Duration = str | float | int | None


_SOURCES = ("schedule", "webhook", "file_drop", "mailbox", "queue")


def _duration(value: Any) -> Any:
    from awa._internal.util import to_timedelta

    to_timedelta(value)  # raises ValueError for anything that is not a duration
    return value


class ScheduleBlueprint(_Spec):
    """`schedule:` of a trigger: a cron expression or an interval (see `Schedule`).

    Attributes:
        cron: Cron expression; give either `cron` or `every`.
        every: Fixed interval, e.g. `"15m"`.
        catch_up: What happens to ticks missed while no worker ran: `"latest"`, `"all"` or `"skip"`.
        grace: How late a tick may still fire under `"skip"`.
        max_catch_up: Most missed ticks fired under `"all"`.
    """

    cron: str | None = None
    every: _Duration = None
    timezone: str | None = None
    catch_up: Literal["skip", "latest", "all"] = "latest"
    grace: _Duration = "1m"
    max_catch_up: int = Field(default=100, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    _check = field_validator("every", "grace")(_duration)

    @model_validator(mode="after")
    def _one(self) -> ScheduleBlueprint:
        if (self.cron is None) == (self.every is None):
            raise ValueError("a schedule needs exactly one of cron or every")
        return self


class WebhookBlueprint(_Spec):
    """`webhook:` of a trigger (see `Webhook`); the secret is named by `secret_env`.

    Attributes:
        scheme: `"timestamped"` (signature of `"<timestamp>.<body>"`), `"body"` (signature of
            the body) or `"token"` (the secret itself in a header or query parameter).
        replay_window: Largest accepted clock difference of a request.
        delivery_header: Header carrying a delivery id, the dedup key.
        deliver_key: Template of the correlation key a request is delivered to (answers a
            waiting run instead of starting one).
        allow_unsigned: Accept unsigned requests; for tests only.
    """

    secret_env: str | None = None
    scheme: Literal["timestamped", "body", "token"] = "timestamped"
    token_query: str | None = None
    replay_window: _Duration = "5m"
    signature_header: str | None = None
    timestamp_header: str | None = None
    delivery_header: str | None = None
    deliver_key: str | None = None
    allow_unsigned: bool = False

    _check = field_validator("replay_window")(_duration)

    @model_validator(mode="after")
    def _signed(self) -> WebhookBlueprint:
        if self.secret_env is None and not self.allow_unsigned:
            raise ValueError("a webhook needs secret_env (or allow_unsigned: true for tests)")
        if self.token_query is not None and self.scheme != "token":
            raise ValueError("token_query goes with scheme: token")
        return self


class FileDropBlueprint(_Spec):
    """`file_drop:` of a trigger (see `FileDrop`); a relative directory resolves against the blueprint's.

    Attributes:
        processed_dir: Where handled files go, relative to `directory`.
        failed_dir: Where refused files go, relative to `directory`.
        stable_for: How long a file must stay unchanged before it is read.
        read: `"auto"` (the text of PDF, Word, Excel, CSV and text files), `"text"`, `"bytes"`
            or `"none"`.
        max_bytes: Larger files go to `failed_dir` without a run.
    """

    directory: str
    pattern: str = "*"
    processed_dir: str = "processed"
    failed_dir: str = "failed"
    stable_for: _Duration = "2s"
    poll_interval: _Duration = "5s"
    read: Literal["auto", "text", "bytes", "none"] = "auto"
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_text_chars: int = Field(default=100_000, ge=1)

    _check = field_validator("stable_for", "poll_interval")(_duration)


class MailboxBlueprint(_Spec):
    """`mailbox:` of a trigger (see `Mailbox`); credentials are named by `*_env`.

    Attributes:
        port: Default 993 with SSL, 143 without.
        search: IMAP search criteria.
        on_success: What happens to a handled message: `"seen"`, `"move"`, `"delete"` or `"keep"`.
        attachments_dir: Where attachments are saved, relative to the blueprint's folder;
            `null` saves none.
        max_attachment_bytes: Larger attachments are listed but neither saved nor read.
    """

    host: str
    username_env: str = "AWA_IMAP_USER"
    password_env: str = "AWA_IMAP_PASSWORD"
    port: int | None = None
    ssl: bool = True
    folder: str = "INBOX"
    search: str = "UNSEEN"
    on_success: Literal["seen", "move", "delete", "keep"] = "seen"
    move_to: str | None = None
    on_failure: Literal["seen", "move", "delete", "keep"] = "keep"
    failed_folder: str | None = None
    poll_interval: _Duration = "60s"
    max_messages: int = Field(default=20, ge=1)
    attachments_dir: str | None = "attachments"
    max_attachment_bytes: int = Field(default=25 * 1024 * 1024, ge=1)

    _check = field_validator("poll_interval")(_duration)

    @model_validator(mode="after")
    def _folders(self) -> MailboxBlueprint:
        if self.on_success == "move" and self.move_to is None:
            raise ValueError("on_success: move needs move_to")
        if self.on_failure == "move" and self.failed_folder is None:
            raise ValueError("on_failure: move needs failed_folder")
        return self


class QueueBlueprint(_Spec):
    """`queue:` of a trigger: a named queue in PostgreSQL (with a DSN) or in memory.

    Attributes:
        backend: `"auto"` (PostgreSQL when a DSN is configured, else memory), `"postgres"`
            or `"memory"`.
        visibility: How long a received message stays invisible to other workers.
        max_attempts: Deliveries before a message is dead-lettered.
    """

    name: str
    backend: Literal["auto", "postgres", "memory"] = "auto"
    batch: int = Field(default=10, ge=1)
    visibility: _Duration = "5m"
    max_attempts: int = Field(default=5, ge=1)

    _check = field_validator("visibility")(_duration)


class TriggerBlueprint(_Spec):
    """One entry of `triggers:`: a unique name, exactly one source, and optional templates.

    The `input`, `thread_id` and `dedup` templates override those of `automation`.
    """

    name: str
    schedule: ScheduleBlueprint | None = None
    webhook: WebhookBlueprint | None = None
    file_drop: FileDropBlueprint | None = None
    mailbox: MailboxBlueprint | None = None
    queue: QueueBlueprint | None = None
    input: str | None = None
    thread_id: str | None = None
    dedup: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _one_source(self) -> TriggerBlueprint:
        if sum(getattr(self, key) is not None for key in _SOURCES) != 1:
            raise ValueError(
                "a trigger needs exactly one of schedule, webhook, file_drop, mailbox or queue"
            )
        return self

    @property
    def kind(self) -> str:
        """Which source the trigger uses."""

        return next(key for key in _SOURCES if getattr(self, key) is not None)


class RetryBlueprint(_Spec):
    """`automation.retry:` (see `RetryPolicy`).

    Attributes:
        retry_on: Error codes retried besides retryable errors; `None` keeps the default list.
    """

    max_attempts: int = Field(default=3, ge=1)
    initial: _Duration = "30s"
    backoff: float = Field(default=2.0, ge=1.0)
    max_delay: _Duration = "1h"
    jitter: float = Field(default=0.0, ge=0.0, le=1.0)
    retry_on: list[str] | None = None

    _check = field_validator("initial", "max_delay")(_duration)


class SlaBlueprint(_Spec):
    """`automation.sla:` (see `SLA`).

    Attributes:
        complete_within: A run must finish within this time of being queued.
        warn_at: Fraction of `complete_within` after which a warning is sent.
        on_breach: `"notify"` or `"cancel"` at the deadline.
        approval_within: An unanswered approval is escalated after this long.
    """

    complete_within: _Duration = None
    warn_at: float | None = Field(default=0.8, gt=0.0, lt=1.0)
    on_breach: Literal["notify", "cancel"] = "notify"
    approval_within: _Duration = None
    remind_every: _Duration = None

    _check = field_validator("complete_within", "approval_within", "remind_every")(_duration)


class NotifyBlueprint(_Spec):
    """One entry of `automation.notify:`: a webhook URL and the variable holding its signing secret."""

    webhook: str
    secret_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class AutomationBlueprint(_Spec):
    """The `automation:` section (plus the blueprint's `triggers:`).

    Attributes:
        input: Default input template (default: the payload as JSON).
        dedup_window: Only runs younger than this count as duplicates.
        concurrency: Most runs at once across workers.
        migration: `pin` or `at_wait`.
        run_timeout: Longest attempt, e.g. `30m`; `null` for no limit.
    """

    name: str | None = None
    triggers: list[TriggerBlueprint] = Field(default_factory=list)
    input: str = "{payload}"
    thread_id: str | None = None
    dedup: str | None = None
    dedup_window: _Duration = None
    concurrency: int = Field(default=1, ge=1)
    retry: RetryBlueprint = Field(default_factory=RetryBlueprint)
    sla: SlaBlueprint | None = None
    migration: Literal["pin", "at_wait"] = "pin"
    run_timeout: _Duration = "30m"
    tenant: str | None = None
    notify: list[NotifyBlueprint] = Field(default_factory=list)

    _check = field_validator("dedup_window", "run_timeout")(_duration)

    @model_validator(mode="after")
    def _unique(self) -> AutomationBlueprint:
        names = [trigger.name for trigger in self.triggers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate trigger name {duplicates[0]!r}")
        return self

    def check(self) -> list[str]:
        """Problems that only show at run time: templates that do not parse, unset variables."""

        from awa.automation.templates import Template

        problems: list[str] = []
        fields = ("input", "thread_id", "dedup")
        templates = [(f"automation.{field}", getattr(self, field)) for field in fields]
        templates += [
            (f"triggers.{trigger.name}.{field}", getattr(trigger, field))
            for trigger in self.triggers
            for field in fields
        ]
        for location, text in templates:
            if text is None:
                continue
            try:
                Template(text)
            except ValueError as exc:
                problems.append(f"{location}: {exc}")
        for trigger in self.triggers:
            names = []
            if trigger.webhook is not None and trigger.webhook.secret_env:
                names.append(trigger.webhook.secret_env)
            if trigger.mailbox is not None:
                names += [trigger.mailbox.username_env, trigger.mailbox.password_env]
            problems.extend(
                f"triggers.{trigger.name}: environment variable {name} is not set"
                for name in names
                if not env_value(name)
            )
        return problems
