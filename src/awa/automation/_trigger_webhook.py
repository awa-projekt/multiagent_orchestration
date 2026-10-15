"""`Webhook`: signed HTTP requests as trigger events."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import urllib.parse
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from awa._internal.logging import get_logger
from awa._internal.util import DurationLike
from awa.automation._time import as_utc, require_timedelta, utcnow
from awa.automation._trigger_base import BaseTrigger, TriggerContext
from awa.automation.models import TriggerEvent
from awa.automation.templates import TemplateLike
from awa.core.config import env_value
from awa.errors import ConfigurationError, MissingConfigurationError, WebhookVerificationError

_logger = get_logger("automation.triggers")

WebhookScheme = Literal["timestamped", "body", "token"]
"""How a webhook request proves it comes from the sender (see `Webhook`)."""


@dataclass(frozen=True)
class _Sender:
    """The headers a scheme uses by default."""

    signature: str
    timestamp: str | None = None
    delivery: str | None = None


_SENDERS: dict[str, _Sender] = {
    "timestamped": _Sender("X-Awa-Signature", "X-Awa-Timestamp", "X-Awa-Delivery"),
    "body": _Sender("X-Awa-Signature", "X-Awa-Timestamp", "X-Awa-Delivery"),
    "token": _Sender("X-Awa-Token", None, "X-Awa-Delivery"),
}


class Webhook(BaseTrigger):
    """Receive HTTP requests from another system, verified with a shared secret.

    Served at `POST /v1/automations/{automation}/webhooks/{name}`. `scheme`: `"timestamped"`
    (default) signs `"<timestamp>.<body>"` with HMAC-SHA256 in `X-Awa-Signature` plus
    `X-Awa-Timestamp`, refused outside `replay_window`; `"body"` signs the body alone and does not
    expire; `"token"` sends the secret in a header or `token_query`. Several secrets and signatures
    allow rotation. The delivery id (`X-Awa-Delivery`) is the dedup key; a replayed signature within
    the window keeps its first key. A JSON body becomes the payload, a form its fields, other bodies
    `{"body": text}`. With `deliver_key` a request answers a waiting run instead.

    Args:
        secret: The shared secret, or several. Prefer `secret_env` (read on every request).
        allow_unsigned: Accept requests without verification (never in production).
        deliver_key: Template of the correlation key the payload is delivered to.

    Example:
        ```python
        Webhook("supplier-invoice", secret_env="INVOICE_HOOK_SECRET",
                dedup="{payload.invoice_no}")
        ```
    """

    def __init__(
        self,
        name: str = "webhook",
        *,
        secret: str | Sequence[str] | None = None,
        secret_env: str | None = None,
        signature_header: str | None = None,
        timestamp_header: str | None = None,
        delivery_header: str | None = None,
        scheme: WebhookScheme = "timestamped",
        token_query: str | None = None,
        replay_window: DurationLike = timedelta(minutes=5),
        allow_unsigned: bool = False,
        deliver_key: TemplateLike | None = None,
        max_bytes: int = 1024 * 1024,
        input: TemplateLike | None = None,
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
    ) -> None:
        super().__init__(name, input=input, thread_id=thread_id, dedup=dedup)
        if secret is None and secret_env is None and not allow_unsigned:
            raise ConfigurationError(
                f"Webhook {name!r} needs secret= or secret_env= (or allow_unsigned=True for "
                "local tests only)"
            )
        if scheme not in _SENDERS:
            raise ConfigurationError(f"scheme must be one of {', '.join(_SENDERS)}, got {scheme!r}")
        if token_query is not None and scheme != "token":
            raise ConfigurationError("token_query goes with scheme='token'")
        if token_query is not None:
            _logger.warning(
                "Webhook %s: token_query puts the secret in the URL, where access logs and "
                "proxies record it; prefer the header",
                name,
            )
        self._secrets = (secret,) if isinstance(secret, str) else tuple(secret or ())
        if any(not item for item in self._secrets):
            raise ConfigurationError("A webhook secret must not be empty")
        sender = _SENDERS[scheme]
        self.secret_env = secret_env
        self.scheme: WebhookScheme = scheme
        self.signature_header = signature_header or sender.signature
        self.timestamp_header = timestamp_header or sender.timestamp
        self.delivery_header = delivery_header or sender.delivery
        self.token_query = token_query
        self.replay_window = require_timedelta(replay_window, what="replay_window")
        self.allow_unsigned = allow_unsigned
        self.deliver_key = deliver_key
        self.max_bytes = max_bytes
        self._seen: dict[bytes, tuple[datetime, str]] = {}

    def describe(self) -> dict[str, Any]:
        """See `BaseTrigger.describe`."""

        return {
            **super().describe(),
            "scheme": self.scheme,
            "signed": not self.allow_unsigned,
            "delivers": self.deliver_key is not None,
        }

    def required_env(self) -> tuple[str, ...]:
        """The secret's variable, unless secrets were given directly."""

        return (self.secret_env,) if self.secret_env and not self._secrets else ()

    def secrets(self) -> tuple[str, ...]:
        """The secrets a signature may be made with (the environment variable's first)."""

        found: list[str] = []
        if self.secret_env is not None:
            value = (env_value(self.secret_env) or "").strip()
            if not value and not self._secrets:
                raise MissingConfigurationError(
                    f"Webhook {self.name!r}: environment variable {self.secret_env} is not set"
                )
            if value:
                found.append(value)
        found.extend(self._secrets)
        return tuple(found)

    def sign(self, body: bytes | str, *, timestamp: int | None = None) -> dict[str, str]:
        """The headers a sender attaches for `body` (the timestamped scheme needs `timestamp`)."""

        raw = body.encode("utf-8") if isinstance(body, str) else body
        secret = self.secrets()[0]
        if self.scheme == "token":
            return {self.signature_header: secret}
        if self.scheme == "timestamped" and timestamp is None:
            raise ValueError("The timestamped scheme needs a timestamp")
        stamp = str(int(timestamp)) if timestamp is not None else None
        headers: dict[str, str] = {}
        if stamp is not None and self.timestamp_header is not None:
            headers[self.timestamp_header] = stamp
        digest = _hmac(secret, self._message(raw, stamp))
        headers[self.signature_header] = f"sha256={digest.hex()}"
        return headers

    def verify(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        now: datetime | None = None,
        query: Mapping[str, str] | None = None,
    ) -> TriggerEvent:
        """Check a request and turn it into an event.

        Raises:
            WebhookVerificationError: If the signature or token is missing or wrong, or the
                timestamp is outside the replay window.
            MissingConfigurationError: If `secret_env` is not set.
        """

        moment = as_utc(now or utcnow())
        if len(body) > self.max_bytes:
            raise WebhookVerificationError(f"Webhook body is larger than {self.max_bytes} bytes")
        lowered = {key.lower(): value for key, value in headers.items()}

        def header(name: str | None) -> str | None:
            return lowered.get(name.lower()) if name else None

        signature = header(self.signature_header) or ""
        timestamp = header(self.timestamp_header)
        verified = None
        if not self.allow_unsigned:
            verified = self._authenticate(body, signature, timestamp, moment, query)
        payload = _decode_body(body, header("content-type"))
        delivery = header(self.delivery_header)
        if delivery:
            dedup: str | None = f"{self.name}:{delivery[:200]}"
        elif verified:
            dedup = f"{self.name}:{hashlib.sha256(verified.encode()).hexdigest()[:32]}"
        else:
            dedup = None
        if verified and dedup is not None:
            dedup = self._first_key(verified, dedup, moment)
        metadata: dict[str, Any] = {"delivery_id": delivery, "timestamp": timestamp}
        return TriggerEvent(
            source=self.name,
            payload=payload,
            received_at=moment,
            dedup_key=dedup,
            metadata=metadata,
        )

    def _first_key(self, signature: str, dedup: str, moment: datetime) -> str:
        """The dedup key of the first request with the verified `signature` in the window."""

        seen = self._seen
        while seen and (
            moment - next(iter(seen.values()))[0] > self.replay_window or len(seen) > 4096
        ):
            del seen[next(iter(seen))]
        digest = hashlib.sha256(signature.encode()).digest()
        return seen.setdefault(digest, (moment, dedup))[1]

    def _authenticate(
        self,
        body: bytes,
        signature: str,
        timestamp: str | None,
        moment: datetime,
        query: Mapping[str, str] | None,
    ) -> str | None:
        """Check the token or signature (and timestamp); the signature that matched, as hex."""

        secrets = self.secrets()  # a missing secret is a configuration error, not a bad request
        if self.scheme == "token":
            self._check_token(signature, (query or {}).get(self.token_query or ""), secrets)
            return None
        offered = self._offered(signature)
        if self.scheme == "timestamped" or timestamp is not None:
            self._check_timestamp(timestamp, moment)
        return self._check_signature(self._message(body, timestamp), offered, secrets)

    async def ready(self, context: TriggerContext) -> list[TriggerEvent]:
        """None: webhooks are pushed to the server."""

        return []

    async def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        """Webhooks are pushed by the server; this iterator only waits for shutdown."""

        await context.stopping.wait()
        return
        yield  # pragma: no cover - makes this an async generator

    def _message(self, body: bytes, timestamp: str | None) -> bytes:
        stamp = (timestamp or "").encode("ascii", "replace")
        if self.scheme == "timestamped":
            return stamp + b"." + body
        return body

    def _offered(self, header: str) -> list[str]:
        """The signatures in a header, as hex."""

        prefix = "sha256="
        return [
            part.strip().removeprefix(prefix)
            for part in header.split(",")
            if part.strip() and part.strip().startswith(prefix)
        ]

    def _check_timestamp(self, timestamp: str | None, now: datetime) -> None:
        try:
            sent = datetime.fromtimestamp(int((timestamp or "").strip()), tz=now.tzinfo)
        except (ValueError, OverflowError, OSError):  # not a number, or out of range
            raise WebhookVerificationError(
                f"Webhook request lacks a valid {self.timestamp_header or 'timestamp'}"
            ) from None
        if abs(now - sent) > self.replay_window:
            raise WebhookVerificationError(
                "Webhook request timestamp is outside the replay window (replayed or clock skew)"
            )

    def _check_signature(self, message: bytes, offered: list[str], secrets: Sequence[str]) -> str:
        if not offered:
            raise WebhookVerificationError(
                f"Webhook request lacks a signature in {self.signature_header}"
            )
        for secret in secrets:
            digest = _hmac(secret, message)
            wanted = digest.hex().encode()
            if any(
                hmac.compare_digest(wanted, value.encode("utf-8", "replace")) for value in offered
            ):
                return wanted.decode()
        raise WebhookVerificationError("Webhook signature does not match")

    def _check_token(self, header: str, query: str | None, secrets: Sequence[str]) -> None:
        offered = [value for value in (header.removeprefix("Bearer ").strip(), query) if value]
        if not offered:
            where = self.signature_header + (
                f" or ?{self.token_query}=" if self.token_query else ""
            )
            raise WebhookVerificationError(f"Webhook request lacks its token ({where})")
        for secret in secrets:
            wanted = secret.encode()
            if any(
                hmac.compare_digest(wanted, value.encode("utf-8", "replace")) for value in offered
            ):
                return
        raise WebhookVerificationError("Webhook token does not match")


def _hmac(secret: str, message: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def _decode_body(body: bytes, content_type: str | None = None) -> Any:
    text = body.decode("utf-8", errors="replace")
    if not text.strip():
        return {}
    if (content_type or "").split(";")[0].strip().lower() == "application/x-www-form-urlencoded":
        fields: dict[str, Any] = dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
        if isinstance(fields.get("payload"), str):  # a form field that carries JSON
            with contextlib.suppress(ValueError):
                fields["payload"] = json.loads(fields["payload"])
        return fields
    try:
        return json.loads(text)
    except ValueError:
        return {"body": text}
