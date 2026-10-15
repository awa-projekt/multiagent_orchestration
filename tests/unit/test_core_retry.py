"""Retries and HTTP: backoff, Retry-After, transient errors, transport failures, and webhook signing with its retrying POST."""

from __future__ import annotations

import email.message
import email.utils
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import socket
import struct
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from awa._internal.http import HttpPolicy, _ascii_url, _embedded_ipv4, fetch
from awa._internal.retry import (
    backoff_delay,
    parse_retry_after,
    retry_after_from_headers,
    retry_on_transient,
)
from awa._internal.upstream.tools import ToolException
from awa.core import webhooks
from awa.core.webhooks import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    compute_signature,
    post_json,
    sign,
    signature_matches,
    url_allowed,
    url_for_logs,
    verify,
)
from awa.errors import (
    ToolTimeoutError,
    TopologyError,
    UsageLimitExceededError,
    WebhookDeliveryError,
    WebhookVerificationError,
)


@pytest.mark.parametrize(
    ("attempt", "expected"), [(0, 0.5), (1, 1.0), (2, 2.0), (3, 4.0), (-2, 0.5)]
)
def test_backoff_doubles_from_the_initial_delay(attempt: int, expected: float) -> None:
    assert backoff_delay(attempt, initial=0.5) == expected


def test_backoff_uses_the_factor_and_the_cap() -> None:
    assert backoff_delay(2, initial=1.0, factor=3.0) == 9.0
    assert backoff_delay(10, initial=0.5, cap=8.0) == 8.0
    assert backoff_delay(10_000, initial=1.0, cap=30.0) == 30.0  # no overflow


def test_backoff_without_a_cap_can_grow_without_bound() -> None:
    assert math.isinf(backoff_delay(10_000, initial=1.0))


def test_jitter_spreads_the_delay_symmetrically() -> None:
    assert backoff_delay(1, initial=1.0, jitter=0.25, rand=lambda: 0.0) == 1.5
    assert backoff_delay(1, initial=1.0, jitter=0.25, rand=lambda: 0.5) == 2.0
    assert backoff_delay(1, initial=1.0, jitter=0.25, rand=lambda: 0.999) == pytest.approx(
        2.5, 1e-2
    )
    for _ in range(50):
        assert 0.5 <= backoff_delay(0, initial=1.0, jitter=0.5) < 1.5


def test_jitter_is_applied_before_the_cap() -> None:
    assert backoff_delay(3, initial=1.0, cap=8.0, jitter=0.5, rand=lambda: 0.99) == 8.0


def test_zero_delay_stays_zero_with_jitter() -> None:
    assert backoff_delay(3, initial=0.0, jitter=1.0) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial": -1.0},
        {"initial": 1.0, "factor": -2.0},
        {"initial": 1.0, "jitter": 1.5},
        {"initial": 1.0, "jitter": -0.1},
        {"initial": 1.0, "cap": -1.0},
    ],
)
def test_backoff_rejects_invalid_arguments(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        backoff_delay(0, **kwargs)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120.0),
        (" 7 ", 7.0),
        ("1.5", 1.5),
        (30, 30.0),
        (2.5, 2.5),
        ("-3", 0.0),
        (-3, 0.0),
        (None, None),
        ("", None),
        ("   ", None),
        ("soon", None),
        ("nan", None),
        ("inf", None),
        (float("nan"), None),
        (True, None),
    ],
)
def test_parse_retry_after_reads_seconds(value: object, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_parse_retry_after_reads_http_dates_relative_to_now() -> None:
    now = 1_700_000_000.0
    header = email.utils.formatdate(now + 90, usegmt=True)

    assert parse_retry_after(header, now=now) == pytest.approx(90.0)
    assert parse_retry_after(email.utils.formatdate(now - 90, usegmt=True), now=now) == 0.0


def test_parse_retry_after_treats_a_minus_zero_zone_as_utc() -> None:
    now = 1_700_000_000.0
    header = email.utils.formatdate(now + 30).rsplit(" ", 1)[0] + " -0000"

    assert parse_retry_after(header, now=now) == pytest.approx(30.0)


def test_retry_after_from_headers_prefers_milliseconds() -> None:
    assert retry_after_from_headers({"retry-after-ms": "1500", "retry-after": "9"}) == 1.5
    assert retry_after_from_headers({"Retry-After": "9"}) == 9.0
    assert retry_after_from_headers({"retry-after-ms": "x", "retry-after": "4"}) == 4.0
    assert retry_after_from_headers({}) is None
    assert retry_after_from_headers(None) is None
    assert retry_after_from_headers("not headers") is None


def test_retry_after_from_headers_reads_email_messages_case_insensitively() -> None:
    message = email.message.Message()
    message["RETRY-AFTER"] = "12"

    assert retry_after_from_headers(message) == 12.0


def test_library_decisions_are_never_retried_but_timeouts_are() -> None:
    assert not retry_on_transient(UsageLimitExceededError("limit"))
    assert not retry_on_transient(TopologyError("bad edge"))
    assert retry_on_transient(ToolTimeoutError("slow"))


def test_transient_rule_follows_langgraph_for_other_errors() -> None:
    assert retry_on_transient(ConnectionError("reset"))
    assert not retry_on_transient(ValueError("bug"))
    assert not retry_on_transient(TypeError("bug"))


def test_timeouts_are_retried_and_tool_refusals_are_not() -> None:
    assert retry_on_transient(TimeoutError("slow"))
    assert not retry_on_transient(ToolException("refused by the tool"))


def test_importing_the_module_does_not_load_langgraph() -> None:
    code = "import sys, awa._internal.retry; assert 'langgraph' not in sys.modules"

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


async def test_a_node_timeout_is_not_retried_by_the_transient_rule() -> None:
    import asyncio
    from typing import TypedDict

    from langgraph.errors import NodeTimeoutError
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import RetryPolicy

    class State(TypedDict):
        n: int

    attempts: list[int] = []

    async def slow(state: State) -> State:
        attempts.append(1)
        await asyncio.sleep(5)
        return state

    def graph(policy: RetryPolicy) -> object:
        builder = StateGraph(State)
        builder.add_node("slow", slow, timeout=0.05, retry_policy=policy)
        builder.add_edge(START, "slow")
        builder.add_edge("slow", END)
        return builder.compile()

    assert not retry_on_transient(NodeTimeoutError("slow", 0.05, kind="run", run_timeout=0.05))
    with pytest.raises(NodeTimeoutError):
        await graph(RetryPolicy(max_attempts=3, retry_on=retry_on_transient)).ainvoke({"n": 0})
    assert len(attempts) == 1

    # A policy that names the node timeout retries it.
    attempts.clear()
    configured = RetryPolicy(
        max_attempts=2, initial_interval=0, jitter=False, retry_on=NodeTimeoutError
    )
    with pytest.raises(NodeTimeoutError):
        await graph(configured).ainvoke({"n": 0})  # type: ignore[attr-defined]
    assert len(attempts) == 2


LOCAL = HttpPolicy(allowed_hosts=None, allow_http=True, allow_private_networks=True)


@contextmanager
def _resetting_server() -> Iterator[str]:
    """A server that reads the request, then resets the connection."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def serve() -> None:
        try:
            while True:
                conn, _ = listener.accept()
                conn.settimeout(2)
                with suppress(OSError):
                    conn.recv(65_536)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                conn.close()
        except OSError:
            return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/x"
    finally:
        listener.close()
        thread.join(timeout=3)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_reset_connection_is_a_tool_exception(method: str) -> None:
    with (
        _resetting_server() as url,
        pytest.raises(ToolException, match="the request failed|malformed"),
    ):
        fetch(LOCAL, url, method=method, data=b"{}" if method == "POST" else None)


def test_a_non_latin1_header_is_refused() -> None:
    with pytest.raises(ToolException, match="cannot carry"):
        fetch(LOCAL, "http://127.0.0.1:9/x", headers={"X-Name": "\u4e2d"})


def test_an_accented_url_is_encoded_not_crashed() -> None:
    assert _ascii_url("https://b\u00fccher.example/caf\u00e9?q=\u00fc&r=%C3%BC") == (
        "https://xn--bcher-kva.example/caf%C3%A9?q=%C3%BC&r=%C3%BC"
    )
    assert _ascii_url("https://a.example:8443/x") == "https://a.example:8443/x"


@pytest.mark.parametrize(
    ("literal", "embedded"),
    [("::127.0.0.1", "127.0.0.1"), ("::a00:1", "10.0.0.1"), ("::ffff:10.0.0.1", "10.0.0.1")],
)
def test_embedded_ipv4_forms_are_unwrapped(literal: str, embedded: str) -> None:
    assert _embedded_ipv4(ipaddress.ip_address(literal)) == ipaddress.ip_address(embedded)
    assert not _embedded_ipv4(ipaddress.ip_address(literal)).is_global


@pytest.mark.parametrize("address", ["::127.0.0.1", "::a00:1", "fec0::1"])
def test_unroutable_ipv6_forms_are_refused(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 80, 0, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer)
    with pytest.raises(ToolException, match="private or reserved"):
        HttpPolicy(allowed_hosts=None, allow_http=True).check_url("http://x.example.test/")


@pytest.mark.parametrize("address", ["::", "::1"])
def test_the_unspecified_and_loopback_addresses_stay_themselves(address: str) -> None:
    ip = ipaddress.ip_address(address)
    assert _embedded_ipv4(ip) == ip


SECRET = "whsec-test-secret"


# ---------------------------------------------------------------------- signing


def test_sign_produces_the_documented_headers() -> None:
    body = b'{"a":1}'

    headers = sign(SECRET, body, timestamp=1_700_000_000)

    digest = hmac.new(SECRET.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert headers == {TIMESTAMP_HEADER: "1700000000", SIGNATURE_HEADER: f"sha256={digest}"}


def test_sign_refuses_an_empty_secret() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        sign("", b"x")


def test_verify_accepts_a_signed_request_in_any_header_case() -> None:
    body = b"payload"
    headers = {k.lower(): v for k, v in sign(SECRET, body, timestamp=1000).items()}

    verify(body, headers, SECRET, now=1100)
    verify(body, headers, ["old-secret", SECRET], now=1100)  # key rotation


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({}, "lacks a valid X-Awa-Timestamp"),
        ({TIMESTAMP_HEADER: "soon"}, "lacks a valid X-Awa-Timestamp"),
        ({TIMESTAMP_HEADER: "--5"}, "lacks a valid X-Awa-Timestamp"),
        ({TIMESTAMP_HEADER: "\u00b2"}, "lacks a valid X-Awa-Timestamp"),
        ({TIMESTAMP_HEADER: "1000"}, "lacks a X-Awa-Signature"),
        ({TIMESTAMP_HEADER: "1000", SIGNATURE_HEADER: "xsha256=00"}, "lacks a X-Awa-Signature"),
        ({TIMESTAMP_HEADER: "1", SIGNATURE_HEADER: "sha256=00"}, "outside the replay window"),
        ({TIMESTAMP_HEADER: "1000", SIGNATURE_HEADER: "sha256=00"}, "does not match"),
        ({TIMESTAMP_HEADER: "1000", SIGNATURE_HEADER: "sha256=\u00e9"}, "does not match"),
    ],
)
def test_verify_refuses_bad_requests(headers: dict[str, str], message: str) -> None:
    with pytest.raises(WebhookVerificationError, match=message):
        verify(b"payload", headers, SECRET, now=1000)


def test_verify_refuses_a_tampered_body_and_wrong_or_empty_secrets() -> None:
    headers = sign(SECRET, b"payload", timestamp=1000)

    with pytest.raises(WebhookVerificationError, match="does not match"):
        verify(b"payload!", headers, SECRET, now=1000)
    with pytest.raises(WebhookVerificationError, match="does not match"):
        verify(b"payload", headers, ["", "other"], now=1000)


def test_signature_matches_any_offered_value() -> None:
    good = compute_signature(SECRET, b"m")

    assert signature_matches(f"sha256=00, {good}", b"m", [SECRET])
    assert not signature_matches(good, b"m", ["wrong"])
    assert not signature_matches("", b"m", [SECRET])
    assert not signature_matches("sha256=\u00e9\u00e9", b"m", [SECRET])


def test_url_helpers() -> None:
    assert url_allowed("https://x.example/hook")
    assert not url_allowed("http://x.example/hook")
    assert url_allowed("http://localhost/hook", allow_http=True)
    assert not url_allowed("ftp://x.example", allow_http=True)
    for malformed in (
        "https://",
        "https://x.example/a b",
        "https://x.example:port/",
        "https://a\r\n",
    ):
        assert not url_allowed(malformed)
    assert url_for_logs("https://x.example/hook?token=abc") == "https://x.example/..."
    assert url_for_logs("https://bot:s3cret@x.example/hook#key=1") == "https://x.example/..."
    assert url_for_logs("https://x.example/services/T0/B0/s3cret") == "https://x.example/..."
    assert url_for_logs("https://x.example?token=abc") == "https://x.example"
    assert url_for_logs("https://x.example") == "https://x.example"


# ---------------------------------------------------------------------- delivery


class _Receiver:
    """A local HTTP server answering each POST with the next scripted status."""

    def __init__(self, statuses: list[int], headers: dict[str, str] | None = None) -> None:
        self.statuses = statuses
        self.headers = headers or {}
        self.requests: list[tuple[dict[str, str], bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                receiver.requests.append((dict(self.headers), self.rfile.read(length)))
                status = receiver.statuses.pop(0) if receiver.statuses else 200
                self.send_response(status)
                for name, value in receiver.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook?token=hidden"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver() -> Iterator[Any]:
    created: list[_Receiver] = []

    def make(statuses: list[int], headers: dict[str, str] | None = None) -> _Receiver:
        created.append(_Receiver(statuses, headers))
        return created[-1]

    yield make
    for item in created:
        item.close()


async def test_post_json_sends_json_and_returns_the_status(receiver: Any) -> None:
    server = receiver([202])

    status = await post_json(server.url, {"text": "hi"}, headers={"X-Extra": "1"}, retries=0)

    assert status == 202
    [(headers, body)] = server.requests
    assert json.loads(body) == {"text": "hi"}
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Extra"] == "1"
    assert SIGNATURE_HEADER not in headers


async def test_post_json_sends_bytes_unchanged_and_signs_them(receiver: Any) -> None:
    server = receiver([200])
    body = b'{"exact":  "bytes"}'

    await post_json(server.url, body, secret=SECRET, retries=0)

    [(headers, received)] = server.requests
    assert received == body
    verify(received, headers, SECRET)


async def test_post_json_retries_transient_statuses_and_signs_each_attempt(
    receiver: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = receiver([503, 429, 200])
    stamps = iter([100, 200, 300])

    def stamped(secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
        return sign(secret, body, timestamp=next(stamps))

    monkeypatch.setattr(webhooks, "sign", stamped)

    status = await post_json(server.url, {"n": 1}, secret=SECRET, retries=3, backoff=0)

    assert status == 200
    assert [h[TIMESTAMP_HEADER] for h, _ in server.requests] == ["100", "200", "300"]


async def test_post_json_fails_at_once_on_a_refusal(receiver: Any) -> None:
    server = receiver([403])

    with pytest.raises(WebhookDeliveryError, match="was refused: HTTP 403") as caught:
        await post_json(server.url, {}, retries=3, backoff=0, what="Webhook callback")

    assert len(server.requests) == 1
    assert "token=hidden" not in str(caught.value)
    assert str(caught.value).startswith("Webhook callback to http://127.0.0.1")


async def test_post_json_gives_up_after_the_retries(receiver: Any) -> None:
    server = receiver([500, 502, 500])

    with pytest.raises(WebhookDeliveryError, match=r"failed after 3 attempt\(s\): HTTP 500"):
        await post_json(server.url, {}, retries=2, backoff=0)

    assert len(server.requests) == 3


async def test_post_json_honours_retry_after_up_to_the_limit(
    receiver: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = receiver([429, 200], headers={"Retry-After": "600"})
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(webhooks.asyncio, "sleep", sleep)

    assert await post_json(server.url, {}, retries=1, backoff=0.25) == 200
    assert waits == [webhooks.MAX_RETRY_AFTER]


async def test_post_json_backs_off_exponentially_on_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    def refuse(*args: Any) -> int:
        raise ConnectionRefusedError

    monkeypatch.setattr(webhooks.asyncio, "sleep", sleep)
    monkeypatch.setattr(webhooks, "_post", refuse)

    with pytest.raises(WebhookDeliveryError, match="ConnectionRefusedError"):
        await post_json("https://x.example/hook", {}, retries=3, backoff=0.5)
    assert waits == [0.5, 1.0, 2.0]


async def test_post_json_retries_a_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    async def sleep(seconds: float) -> None:
        pass

    def garble(*args: Any) -> int:
        attempts.append(1)
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(webhooks.asyncio, "sleep", sleep)
    monkeypatch.setattr(webhooks, "_post", garble)

    with pytest.raises(WebhookDeliveryError, match="BadStatusLine"):
        await post_json("https://x.example/hook", {}, retries=2)
    assert len(attempts) == 3


async def test_post_json_rejects_negative_retries() -> None:
    with pytest.raises(ValueError):
        await post_json("https://x.example/hook", {}, retries=-1)


async def test_post_json_does_not_follow_a_redirect(receiver: Any) -> None:
    elsewhere = receiver([200])
    server = receiver([302], {"Location": elsewhere.url})

    with pytest.raises(WebhookDeliveryError, match="was refused: HTTP 302"):
        await post_json(server.url, {"secret": "payload"}, secret=SECRET, retries=3, backoff=0)

    assert len(server.requests) == 1 and elsewhere.requests == []
