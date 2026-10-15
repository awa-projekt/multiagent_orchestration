"""Signed webhooks: one HMAC scheme and a retrying JSON POST.

A signed request carries `X-Awa-Timestamp` (Unix seconds) and `X-Awa-Signature`
(`sha256=<hex HMAC-SHA256 of "<timestamp>.<body>">`); `post_json` signs every attempt anew.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from awa._internal.logging import get_logger
from awa._internal.retry import backoff_delay, parse_retry_after
from awa.errors import WebhookDeliveryError, WebhookVerificationError

_log = get_logger("core.webhooks")

SIGNATURE_HEADER = "X-Awa-Signature"
TIMESTAMP_HEADER = "X-Awa-Timestamp"
DELIVERY_HEADER = "X-Awa-Delivery"
EVENT_HEADER = "X-Awa-Event"
DEFAULT_TOLERANCE = 300.0
"""Seconds a signed request stays acceptable (the replay window)."""

_TIMESTAMP = re.compile(r"-?[0-9]+")

RETRIED_STATUSES = frozenset({408, 425, 429})
"""HTTP statuses below 500 that `post_json` retries (every 5xx is retried too)."""

MAX_RETRY_AFTER = 60.0
"""Longest `Retry-After` wait `post_json` honors, in seconds."""


def compute_signature(secret: str, message: bytes) -> str:
    """`sha256=<hex HMAC-SHA256 of message>`, the value of a signature header."""

    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signature_matches(header: str, message: bytes, secrets: Iterable[str]) -> bool:
    """Whether any `sha256=` value in `header` signs `message` with a non-empty secret."""

    offered = [value.encode("utf-8", "replace") for value in _offered(header)]
    for secret in secrets:
        if not secret:
            continue
        expected = compute_signature(secret, message).removeprefix("sha256=").encode("ascii")
        if any(hmac.compare_digest(expected, value) for value in offered):
            return True
    return False


def _offered(header: str) -> list[str]:
    return [
        part.strip().removeprefix("sha256=")
        for part in header.split(",")
        if part.strip().startswith("sha256=")
    ]


def sign(secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    """The `X-Awa-Timestamp` and `X-Awa-Signature` headers for the exact `body`.

    Example:
        ```python
        headers = sign(os.environ["AWA_HOOK_SECRET"], body)
        ```
    """

    if not secret:
        raise ValueError("A webhook secret must not be empty")
    stamp = str(int(time.time() if timestamp is None else timestamp))
    return {
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: compute_signature(secret, stamp.encode() + b"." + body),
    }


def verify(
    body: bytes,
    headers: Mapping[str, str],
    secret: str | Sequence[str],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> None:
    """Check the signature and timestamp of a received awa webhook.

    Pass the raw body; several secrets are accepted during a key rotation. Remember processed
    `X-Awa-Delivery` ids for at least `tolerance` seconds to process a retry once.

    Raises:
        WebhookVerificationError: If a header is missing, the timestamp is outside the window, or
            no secret produces the signature.
    """

    lowered = {key.lower(): value for key, value in headers.items()}
    stamp = lowered.get(TIMESTAMP_HEADER.lower(), "").strip()
    if _TIMESTAMP.fullmatch(stamp) is None:
        raise WebhookVerificationError(f"Webhook request lacks a valid {TIMESTAMP_HEADER} header")
    current = time.time() if now is None else now
    if abs(current - int(stamp)) > tolerance:
        raise WebhookVerificationError(
            "Webhook request timestamp is outside the replay window (replayed or clock skew)"
        )
    signature = lowered.get(SIGNATURE_HEADER.lower(), "")
    if not _offered(signature):
        raise WebhookVerificationError(f"Webhook request lacks a {SIGNATURE_HEADER} header")
    secrets = (secret,) if isinstance(secret, str) else tuple(secret)
    if not signature_matches(signature, stamp.encode() + b"." + body, secrets):
        raise WebhookVerificationError("Webhook signature does not match")


def url_allowed(url: str, *, allow_http: bool = False) -> bool:
    """Whether `url` may receive webhooks: a well-formed `https://` URL with a host, or
    `http://` when `allow_http`; spaces and control characters are refused."""

    if any(char.isspace() or not char.isprintable() for char in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # a port that is not a number raises
    except ValueError:
        return False
    schemes = ("https", "http") if allow_http else ("https",)
    return parts.scheme in schemes and bool(parts.hostname)


def url_for_logs(url: str) -> str:
    """`url` reduced to scheme and host: credentials, path, query and fragment may carry tokens."""

    scheme, separator, rest = url.partition("://")
    if not separator:
        return "<url>"
    authority, slash, _ = rest.partition("/")
    host = authority.split("?", 1)[0].split("#", 1)[0].rpartition("@")[2]
    return f"{scheme}://{host}/..." if slash else f"{scheme}://{host}"


async def post_json(
    url: str,
    body: bytes | Any,
    *,
    headers: Mapping[str, str] | None = None,
    secret: str | None = None,
    retries: int = 3,
    timeout: float = 10.0,
    backoff: float = 1.0,
    what: str = "Webhook",
) -> int:
    """POST `body` as JSON to `url`, retrying transient failures; return the HTTP status.

    Connection errors, timeouts, malformed responses, 408, 425, 429 and 5xx are retried after
    `backoff * 2**attempt` seconds, or a longer `Retry-After` (at most `MAX_RETRY_AFTER`); other
    errors fail at once.

    Args:
        url: The target URL; check it with `url_allowed` first.
        body: The exact bytes to send, or a value serialized with `json.dumps` (`default=str`).
        secret: Signing secret; `None` sends the request unsigned.
        retries: Extra attempts after a failed one.
        what: Subject of log lines and error messages (`"Webhook callback"`).

    Raises:
        WebhookDeliveryError: If the receiver refused the request or every attempt failed; the
            URL in its message names only scheme and host.
    """

    if retries < 0:
        raise ValueError("retries must be >= 0")
    data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
    base = {"Content-Type": "application/json", **(headers or {})}
    target = url_for_logs(url)
    last = "no attempt was made"
    for attempt in range(retries + 1):
        request_headers = {**base, **sign(secret, data)} if secret else dict(base)
        wait = backoff_delay(attempt, initial=backoff)
        try:
            status = await asyncio.to_thread(_post, url, data, request_headers, timeout)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code not in RETRIED_STATUSES and exc.code < 500:
                raise WebhookDeliveryError(f"{what} to {target} was refused: {last}") from None
            hint = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            if hint is not None:
                wait = max(wait, min(hint, MAX_RETRY_AFTER))
        except (OSError, http.client.HTTPException) as exc:
            last = type(exc).__name__
        else:
            _log.info("%s delivered to %s (%s)", what, target, status)
            return status
        if attempt < retries:
            _log.warning("%s to %s failed (%s); retrying in %.1fs", what, target, last, wait)
            await asyncio.sleep(wait)
    raise WebhookDeliveryError(f"{what} to {target} failed after {retries + 1} attempt(s): {last}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A receiver cannot send the signed request on to another address."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with _OPENER.open(request, timeout=timeout) as response:
        response.read(65_536)  # the answer is not used; never read an endless one
        return int(response.status)
