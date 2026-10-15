"""Cron expressions, templates and durations, webhook signatures and the signed `WebhookNotifier`."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _helpers.automation import ScriptedSystem
from awa._internal.util import to_timedelta
from awa.automation import Automation, Webhook, WebhookNotifier
from awa.automation._time import zone
from awa.automation.blueprint import build_triggers
from awa.automation.cron import CronExpression, _to_utc
from awa.automation.models import ProcessEvent
from awa.automation.templates import Template
from awa.blueprints import Blueprint
from awa.blueprints._automation import AutomationBlueprint, MailboxBlueprint, WebhookBlueprint
from awa.core.webhooks import verify
from awa.errors import (
    ConfigurationError,
    InvalidCronError,
    TemplateError,
    WebhookDeliveryError,
    WebhookVerificationError,
)
from awa.server import create_automation_router


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def berlin_available() -> bool:
    try:
        zone("Europe/Berlin")
    except InvalidCronError:
        return False
    return True


needs_zones = pytest.mark.skipif(not berlin_available(), reason="no IANA time zone data")


# ---------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("expression", "after", "expected"),
    [
        ("* * * * *", utc(2026, 1, 1, 0, 0), utc(2026, 1, 1, 0, 1)),
        ("*/15 * * * *", utc(2026, 1, 1, 0, 7), utc(2026, 1, 1, 0, 15)),
        ("0 6 * * *", utc(2026, 1, 1, 6, 0), utc(2026, 1, 2, 6, 0)),
        ("30 8-10/2 * * *", utc(2026, 1, 1, 8, 30), utc(2026, 1, 1, 10, 30)),
        ("0 0 1 * *", utc(2026, 1, 15), utc(2026, 2, 1)),
        ("0 0 * * MON-FRI", utc(2026, 1, 2, 12), utc(2026, 1, 5)),  # Fri noon -> Mon
        ("0 0 * * 7", utc(2026, 1, 1), utc(2026, 1, 4)),  # 7 is Sunday
        ("0 9 1 * MON", utc(2026, 6, 1, 10), utc(2026, 6, 8, 9)),  # DOM or DOW
        ("0 0 29 2 *", utc(2026, 1, 1), utc(2028, 2, 29)),
        ("0 12 * JAN,jul *", utc(2026, 2, 1), utc(2026, 7, 1, 12)),
        ("@hourly", utc(2026, 1, 1, 5, 30), utc(2026, 1, 1, 6)),
        ("@daily", utc(2026, 1, 1, 5), utc(2026, 1, 2)),
        ("@weekly", utc(2026, 1, 1), utc(2026, 1, 4)),
        ("@monthly", utc(2026, 1, 1), utc(2026, 2, 1)),
        ("@yearly", utc(2026, 1, 1), utc(2027, 1, 1)),
        ("5,10  *  * * *", utc(2026, 1, 1, 0, 5), utc(2026, 1, 1, 0, 10)),
    ],
)
def test_next_after(expression: str, after: datetime, expected: datetime) -> None:
    assert CronExpression(expression).next_after(after) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "5-1 * * * *",
        "*/0 * * * *",
        "a * * * *",
        "\u00b2 * * * *",  # a digit `int` cannot read
        "*/\u00b2 * * * *",
        "1,,2 * * * *",
        "L * * * *",
    ],
)
def test_invalid_expressions_are_rejected(expression: str) -> None:
    with pytest.raises(InvalidCronError):
        CronExpression(expression)


def test_an_expression_that_never_fires_is_reported() -> None:
    with pytest.raises(InvalidCronError, match="never fires"):
        CronExpression("0 0 31 2 *").next_after(utc(2026, 1, 1))


def test_star_step_does_not_restrict_the_day() -> None:
    # `*/2` in the day-of-month field counts as unrestricted, so only Mondays match.
    cron = CronExpression("0 0 */2 * MON")
    assert cron.matches_day(date(2026, 1, 5))  # a Monday on an odd day
    assert not cron.matches_day(date(2026, 1, 7))


def test_equality_and_repr() -> None:
    cron = CronExpression("0 */6 * * *")
    assert cron == CronExpression("0  */6 * * *")
    assert hash(cron) == hash(CronExpression("0 */6 * * *"))
    assert "0 */6" in repr(cron)


def test_unknown_zone() -> None:
    with pytest.raises(InvalidCronError, match="time zone"):
        CronExpression("* * * * *", timezone="Mars/Olympus")


@needs_zones
def test_time_zone_and_daylight_saving() -> None:
    cron = CronExpression("0 6 * * *", timezone="Europe/Berlin")
    # Winter: 06:00 CET is 05:00 UTC; summer: 06:00 CEST is 04:00 UTC.
    assert cron.next_after(utc(2026, 1, 10)) == utc(2026, 1, 10, 5)
    assert cron.next_after(utc(2026, 7, 10)) == utc(2026, 7, 10, 4)


@needs_zones
def test_a_time_in_the_spring_gap_fires_once_after_the_jump() -> None:
    cron = CronExpression("30 2 * * *", timezone="Europe/Berlin")
    # 29 March 2026: clocks jump from 02:00 to 03:00; 02:30 becomes 03:30 CEST = 01:30 UTC.
    assert cron.next_after(utc(2026, 3, 28, 12)) == utc(2026, 3, 29, 1, 30)
    assert cron.next_after(utc(2026, 3, 29, 1, 30)) == utc(2026, 3, 30, 0, 30)


@needs_zones
def test_an_ambiguous_autumn_time_fires_once() -> None:
    cron = CronExpression("30 2 * * *", timezone="Europe/Berlin")
    # 25 October 2026: 02:30 happens twice; only the first (CEST, 00:30 UTC) fires.
    first = cron.next_after(utc(2026, 10, 24, 12))
    assert first == utc(2026, 10, 25, 0, 30)
    assert cron.next_after(first) == utc(2026, 10, 26, 1, 30)


@needs_zones
def test_hourly_ticks_across_the_spring_gap_are_strictly_increasing() -> None:
    cron = CronExpression("*/30 * * * *", timezone="Europe/Berlin")
    ticks = []
    current = utc(2026, 3, 28, 23)
    while (current := cron.next_after(current)) <= utc(2026, 3, 29, 3):
        ticks.append(current)
    assert ticks == sorted(set(ticks))
    assert all(b - a >= timedelta(minutes=30) for a, b in zip(ticks, ticks[1:], strict=False))


def _brute_force_next(cron: CronExpression, after: datetime) -> datetime:
    """The earliest matching instant after `after`, from every wall time of nearby days."""

    local = after.astimezone(cron.timezone).date()
    instants = [
        _to_utc(datetime(day.year, day.month, day.day, hour, minute), cron.timezone)
        for day in (local + timedelta(days=offset) for offset in range(-1, 3))
        if cron.matches_day(day)
        for hour in cron._hours
        for minute in cron._minutes
    ]
    return min(instant for instant in instants if instant > after)


@needs_zones
@pytest.mark.parametrize("expression", ["*/30 * * * *", "0,45 1-3 * * *", "15 2 * * *"])
@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25), date(2026, 6, 1)])
def test_next_after_takes_the_earliest_instant_around_daylight_saving(
    expression: str, day: date
) -> None:
    cron = CronExpression(expression, timezone="Europe/Berlin")
    start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=2)
    for minutes in range(0, 8 * 60, 7):
        after = start + timedelta(minutes=minutes, seconds=30)
        assert cron.next_after(after) == _brute_force_next(cron, after)


def test_catching_up_a_day_of_minute_ticks_is_fast() -> None:
    cron = CronExpression("* * * * *")
    current = utc(2026, 3, 2)
    started = time.perf_counter()
    for _ in range(1440):
        current = cron.next_after(current)
    assert current == utc(2026, 3, 3)
    assert time.perf_counter() - started < 1.0


# ---------------------------------------------------------------------- templates


def test_template_paths_filters_and_fallbacks() -> None:
    context = {
        "payload": {
            "subject": "  Invoice RE 17 ",
            "lines": [{"sku": "A-1"}],
            "invoice-no": "RE/17",
            "empty": "",
            "amount": 12.5,
            "ok": True,
        },
        "trigger": "mail",
    }
    assert Template("{payload.subject|trim|lower}").render(context) == "invoice re 17"
    assert Template("{payload.lines[0].sku}").render(context) == "A-1"
    assert Template('{payload["invoice-no"]|slug}').render(context) == "re-17"
    assert Template('{payload.missing|"n/a"}-{payload.empty|"none"}').render(context) == "n/a-none"
    assert Template("{payload.amount} {payload.ok} {trigger|upper}").render(context) == (
        "12.5 true MAIL"
    )
    assert Template("{{literal}} {payload.lines|json}").render(context) == (
        '{literal} [{"sku": "A-1"}]'
    )
    assert Template("{payload.lines[0]}").render(context) == '{"sku": "A-1"}'
    assert Template("x {payload.subject} {trigger}").fields == ("payload.subject", "trigger")


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "}",
        "{}",
        "{payload.}",
        "{payload[x]}",
        "{payload[\u00b2]}",
        "{payload|shout}",
        "{1abc}",
        "{a{b}",
        "{payload[--1]}",
    ],
)
def test_malformed_templates(text: str) -> None:
    with pytest.raises(TemplateError):
        Template(text)


def test_a_quoted_key_may_hold_brackets_and_pipes() -> None:
    context = {"payload": {"a]b": "x", "c|d": "y"}}

    assert Template('{payload["a]b"]|upper}').render(context) == "X"
    assert Template("{payload['c|d']|\"-\"}").render(context) == "y"


def test_a_long_placeholder_is_split_in_linear_time() -> None:
    import time

    text = "{payload" + "|trim" * 4_000 + "["

    started = time.perf_counter()
    with pytest.raises(TemplateError):
        Template(text + "}")
    assert time.perf_counter() - started < 1.0


def test_missing_fields_raise_without_fallback() -> None:
    with pytest.raises(TemplateError, match="missing"):
        Template("{payload.nope}").render({"payload": {}})
    with pytest.raises(TemplateError):
        Template("{payload.items[3]}").render({"payload": {"items": [1]}})
    assert Template("{payload.none}").render({"payload": {"none": None}}) == ""


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("90s", 90),
        ("15m", 900),
        ("2h", 7200),
        ("3d", 259200),
        ("1w", 604800),
        ("1d12h", 129600),
        ("250ms", 0.25),
        (30, 30),
        ("45", 45),
        (timedelta(minutes=2), 120),
    ],
)
def test_durations(value: object, seconds: float) -> None:
    assert to_timedelta(value) == timedelta(seconds=seconds)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "soon", "5 parsecs", "-5", True, [1], 1e20, "1e20s"])
def test_invalid_durations(value: object) -> None:
    with pytest.raises(ValueError):
        to_timedelta(value)  # type: ignore[arg-type]


def at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def test_body_scheme_signs_the_body_alone() -> None:
    hook = Webhook("hook", scheme="body", secret="s3cret")
    body = b'{"n": 1}'
    signed = hook.sign(body)
    assert signed == {
        "X-Awa-Signature": "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    }
    assert hook.verify(body, signed).payload == {"n": 1}
    with pytest.raises(WebhookVerificationError, match="does not match"):
        hook.verify(b'{"n": 2}', signed)


def test_webhook_vendor_scheme_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="body"):
        Webhook("gh", scheme="github", secret="s")  # type: ignore[arg-type]


def test_token_scheme_in_a_header_a_bearer_or_the_query() -> None:
    hook = Webhook("forms", scheme="token", secret=["new-token", "old-token"])
    assert hook.verify(b'{"a": 1}', {"X-Awa-Token": "old-token"}).payload == {"a": 1}
    bearer = Webhook("forms", scheme="token", secret="t0k", signature_header="Authorization")
    assert bearer.verify(b"{}", {"Authorization": "Bearer t0k"}).dedup_key is None
    query = Webhook("forms", scheme="token", secret="t0k", token_query="token")
    assert query.verify(b"{}", {}, query={"token": "t0k"}).payload == {}
    with pytest.raises(WebhookVerificationError, match="lacks its token"):
        query.verify(b"{}", {})
    with pytest.raises(WebhookVerificationError, match="does not match"):
        query.verify(b"{}", {}, query={"token": "guess"})
    # Every request carries the same token, so it never serves as the dedup key.
    first = hook.verify(b"{}", {"X-Awa-Token": "new-token"})
    second = hook.verify(b"{}", {"X-Awa-Token": "new-token", "X-Awa-Delivery": "d-2"})
    assert first.dedup_key is None and second.dedup_key == "forms:d-2"
    with pytest.raises(ConfigurationError, match="token_query"):
        Webhook("x", secret="s", token_query="token")
    with pytest.raises(ConfigurationError, match="scheme must be one of"):
        Webhook("x", secret="s", scheme="gitlab")  # type: ignore[arg-type]


def test_blueprint_sections_take_the_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOK_SECRET", "s3cret")
    spec = AutomationBlueprint.model_validate(
        {
            "triggers": [
                {"name": "signed", "webhook": {"scheme": "body", "secret_env": "HOOK_SECRET"}},
                {
                    "name": "form",
                    "webhook": {"scheme": "token", "secret_env": "HOOK_SECRET", "token_query": "t"},
                },
            ]
        }
    )
    signed, form = build_triggers(spec)
    assert signed.scheme == "body" and signed.timestamp_header == "X-Awa-Timestamp"
    assert signed.verify(b'{"n": 1}', signed.sign(b'{"n": 1}')).payload == {"n": 1}
    assert form.token_query == "t"
    with pytest.raises(ValueError, match="token_query goes with scheme: token"):
        WebhookBlueprint(secret_env="X", token_query="t")


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ({"cron": "0 25 * * *"}, "outside 0-23"),
        ({"cron": "0 6 * * *", "timezone": "Mars/Olympus"}, "Unknown time zone"),
        ({"every": "0s"}, "at least one second"),
    ],
)
def test_the_check_builds_every_schedule(schedule: dict[str, Any], message: str) -> None:
    blueprint = Blueprint.from_dict(
        {
            "name": "timed",
            "agents": [{"name": "a"}],
            "triggers": [{"name": "tick", "schedule": schedule}],
        }
    )

    [issue] = [i for i in blueprint.check() if i.severity == "error"]

    assert issue.path == "automation.triggers[0].schedule"
    assert message in issue.message


@pytest.mark.parametrize(
    ("mailbox", "message"),
    [
        ({"on_success": "move"}, "on_success: move needs move_to"),
        ({"on_failure": "move"}, "on_failure: move needs failed_folder"),
    ],
)
def test_a_mailbox_that_moves_messages_names_the_folder(
    mailbox: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MailboxBlueprint.model_validate({"host": "imap.example.com", **mailbox})


def app_for(*triggers: Webhook) -> tuple[FastAPI, Automation]:
    automation = Automation(ScriptedSystem(), name="hooks", triggers=list(triggers))
    app = FastAPI()
    app.include_router(create_automation_router([automation]))
    return app, automation


def test_router_passes_the_query_token() -> None:
    form = Webhook("form", scheme="token", secret="t0k", token_query="token")
    app, automation = app_for(form)
    client = TestClient(app)

    accepted = client.post("/v1/automations/hooks/webhooks/form?token=t0k", json={"n": 1})
    refused = client.post("/v1/automations/hooks/webhooks/form?token=nope", json={"n": 1})
    assert accepted.status_code == 202 and refused.status_code == 401

    assert len(automation.store) == 1  # only the accepted request started a run


def test_every_refusal_reads_the_same_and_a_non_ascii_header_is_a_401(
    caplog: pytest.LogCaptureFixture,
) -> None:
    signed = Webhook("signed", secret="s3cret")
    token = Webhook("token", scheme="token", secret="t0k", token_query="token")
    app, _ = app_for(signed, token)
    client = TestClient(app)
    body = b'{"n": 1}'
    now = int(datetime.now(UTC).timestamp())
    good = signed.sign(body, timestamp=now)

    attempts: list[tuple[str, bytes, dict[str, str | bytes]]] = [
        ("signed", body, {}),  # nothing sent
        ("signed", body, {**good, "X-Awa-Signature": "sha256=" + "0" * 64}),  # wrong signature
        ("signed", body, signed.sign(body, timestamp=now - 3600)),  # replayed
        ("signed", body, {**good, "X-Awa-Signature": "sha256=ä".encode()}),
        ("token", body, {"X-Awa-Token": "tökén".encode()}),
    ]
    with caplog.at_level("WARNING"):
        texts = set()
        for trigger, content, headers in attempts:
            response = client.post(
                f"/v1/automations/hooks/webhooks/{trigger}", content=content, headers=headers
            )
            assert response.status_code == 401, headers
            texts.add(response.json()["error"]["message"])
    assert texts == {"Webhook verification failed"}
    assert "outside the replay window" in caplog.text  # the reason stays in the log


def test_a_chunked_body_over_the_limit_is_refused_with_413() -> None:
    hook = Webhook("big", allow_unsigned=True, max_bytes=100)
    app, automation = app_for(hook)
    client = TestClient(app)

    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * 50

    streamed = client.post("/v1/automations/hooks/webhooks/big", content=chunks())
    declared = client.post("/v1/automations/hooks/webhooks/big", content=b"x" * 500)
    assert streamed.status_code == declared.status_code == 413
    assert streamed.json()["error"]["type"] == "PayloadTooLarge"
    assert client.post("/v1/automations/hooks/webhooks/big", json={"n": 1}).status_code == 202
    assert len(automation.store) == 1


def test_a_token_in_the_url_is_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        Webhook("form", scheme="token", secret="t", token_query="token")
    assert "token_query puts the secret in the URL" in caplog.text


@pytest.mark.parametrize("scheme", ["timestamped", "body"])
def test_a_replayed_signature_is_one_request_whatever_the_unsigned_delivery_header_says(
    scheme: str,
) -> None:
    hook = Webhook("hook", scheme=scheme, secret="s3cret")  # type: ignore[arg-type]
    body = b'{"no": 1}'
    signed = hook.sign(body, timestamp=1_700_000_000 if scheme == "timestamped" else None)
    seen = at(1_700_000_030)

    keys = [
        hook.verify(
            body, {**signed, hook.delivery_header or "X-Awa-Delivery": delivery}, now=seen
        ).dedup_key
        for delivery in ("d-1", "d-2", "d-3")
    ]
    assert keys == ["hook:d-1"] * 3
    respelled = {**signed, hook.signature_header: f"sha256=00, {signed[hook.signature_header]}"}
    respelled[hook.delivery_header or "X-Awa-Delivery"] = "d-4"
    assert hook.verify(body, respelled, now=seen).dedup_key == "hook:d-1"
    if scheme != "timestamped":  # these senders give no timestamp: only the window protects
        late = {**signed, hook.delivery_header or "X-Awa-Delivery": "d-9"}
        assert hook.verify(body, late, now=at(1_700_000_700)).dedup_key == "hook:d-9"


EVENT = ProcessEvent(type="process.dead_lettered", automation="ap-desk", run_id="run-1")


class _Receiver:
    """A local HTTP server answering each POST with the next scripted status."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.requests: list[tuple[dict[str, str], bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                receiver.requests.append((dict(self.headers), self.rfile.read(length)))
                self.send_response(receiver.statuses.pop(0) if receiver.statuses else 200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def receiver() -> Iterator[Any]:
    created: list[_Receiver] = []

    def make(statuses: list[int]) -> _Receiver:
        created.append(_Receiver(statuses))
        return created[-1]

    yield make
    for item in created:
        item.server.shutdown()
        item.server.server_close()


def test_the_url_must_be_https_unless_http_is_allowed() -> None:
    with pytest.raises(ConfigurationError, match="https://"):
        WebhookNotifier("http://hooks.example.com/ap")
    assert WebhookNotifier("http://localhost/ap", allow_http=True).url == "http://localhost/ap"


async def test_events_are_posted_as_signed_json(
    receiver: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = receiver([200])
    monkeypatch.setenv("AP_HOOK_SECRET", "whsec-notify")
    notify = WebhookNotifier(
        server.url, secret_env="AP_HOOK_SECRET", headers={"X-Team": "ap"}, allow_http=True
    )

    await notify(EVENT)

    [(headers, body)] = server.requests
    verify(body, headers, "whsec-notify")
    assert headers["X-Team"] == "ap"
    assert json.loads(body) == {"text": EVENT.describe(), "event": EVENT.model_dump(mode="json")}


async def test_transient_failures_are_retried_and_refusals_are_not(
    receiver: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("awa.core.webhooks.backoff_delay", lambda *args, **kwargs: 0.0)
    flaky = receiver([503, 200])
    await WebhookNotifier(flaky.url, retries=2, allow_http=True)(EVENT)
    assert len(flaky.requests) == 2

    refusing = receiver([400, 200])
    with pytest.raises(WebhookDeliveryError, match="was refused: HTTP 400"):
        await WebhookNotifier(refusing.url, retries=2, allow_http=True)(EVENT)
    assert len(refusing.requests) == 1
