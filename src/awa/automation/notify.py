"""Notifiers: tell people when a process needs them (reminders, escalations, dead letters).

A notifier is any sync or async callable taking a `ProcessEvent`; it gets the events in
`NOTIFY_EVENT_TYPES`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TypeAlias

from awa.automation.models import ProcessEvent
from awa.core.config import env_value
from awa.core.webhooks import post_json, url_allowed, url_for_logs
from awa.errors import ConfigurationError

Notifier: TypeAlias = Callable[[ProcessEvent], Awaitable[None] | None]
"""Receives the process events people must act on. May be sync or async."""


class WebhookNotifier:
    """POST process events as JSON to a URL, signed like awa webhooks.

    The body is `{"text": <one line>, "event": <ProcessEvent JSON>}`; with a secret, every attempt
    carries `X-Awa-Timestamp` and `X-Awa-Signature` (check with `verify_webhook`). Connection
    errors, timeouts, 408, 425, 429 and 5xx are retried `retries` times with doubling delays (or
    `Retry-After`). A failed delivery is logged; a notifier never fails the run.

    Args:
        url: Target URL (`https://` unless `allow_http=True`).
        secret: Signing secret; prefer `secret_env`.

    Example:
        ```python
        notify = WebhookNotifier("https://hooks.example.com/ap-team", secret_env="AP_HOOK_SECRET")
        ```
    """

    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        secret_env: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        retries: int = 2,
        allow_http: bool = False,
    ) -> None:
        if not url_allowed(url, allow_http=allow_http):
            raise ConfigurationError("WebhookNotifier needs an https:// URL")
        self.url = url
        self._secret = secret
        self.secret_env = secret_env
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.retries = max(0, retries)

    def __repr__(self) -> str:
        return f"WebhookNotifier({url_for_logs(self.url)!r})"

    async def __call__(self, event: ProcessEvent) -> None:
        """Deliver `event`.

        Raises:
            WebhookDeliveryError: If the receiver refused the event or every attempt failed.
        """

        secret = (env_value(self.secret_env) or "") if self.secret_env else ""
        await post_json(
            self.url,
            {"text": event.describe(), "event": event.model_dump(mode="json")},
            headers=self.headers,
            secret=secret or self._secret or None,
            retries=self.retries,
            timeout=self.timeout,
            backoff=0.5,
            what="Webhook notification",
        )


__all__ = ["Notifier", "WebhookNotifier"]
