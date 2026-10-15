"""`awa.kernel.redaction`: the patterns, `redact_values`, and a corpus of realistic leaks and harmless text."""

from __future__ import annotations

import logging
import re
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import awa.observability as observability
from _helpers.fake_secrets import fake_secret
from awa.contracts.events import ErrorEvent
from awa.kernel.redaction import (
    SECRETS_REDACTOR,
    RegexRedactor,
    iban_valid,
    luhn_valid,
    redact_mapping,
    redact_secrets,
    redact_values,
)
from awa.observability import PII_PATTERNS, SECRET_PATTERNS, EventLogger, RedactionPattern, Redactor


def test_redact_values_replaces_every_occurrence() -> None:
    text = "token s3cr3t-value used twice: s3cr3t-value"

    assert redact_values(text, ["s3cr3t-value"]) == "token *** used twice: ***"


def test_redact_values_replaces_longer_secrets_first() -> None:
    # "abcd" is part of "abcdefgh": replacing it first would leave "***efgh".
    assert redact_values("key=abcdefgh", ["abcd", "abcdefgh"]) == "key=***"


def test_redact_values_also_replaces_the_url_encoded_form() -> None:
    dsn = "postgresql://app:p%40ss%2Fword@db/prod"

    assert redact_values(dsn, ["p@ss/word"]) == "postgresql://app:***@db/prod"


@pytest.mark.parametrize("secrets", [[None, ""], ["abc"], []])
def test_redact_values_skips_missing_and_short_values(secrets: list[str | None]) -> None:
    assert redact_values("abc def", secrets) == "abc def"


def test_redact_values_min_len_and_replacement_are_configurable() -> None:
    assert redact_values("pin 42 ok", ["42"], min_len=2, replacement="<pin>") == "pin <pin> ok"


def test_redact_values_accepts_any_iterable_and_empty_text() -> None:
    assert redact_values("", ["secret"]) == ""
    assert redact_values("a secret b", (s for s in ["secret"])) == "a *** b"


def test_awa_observability_re_exports_the_redactor() -> None:
    assert observability.RegexRedactor is RegexRedactor


@pytest.mark.parametrize("key", ["pin", "card_pin", "PIN", "otp", "passcode", "cvv", "cvc"])
def test_pins_and_card_codes_are_secrets(key: str) -> None:
    assert redact_mapping({key: "hunter2-4711"})[key] == "[REDACTED:secret]"
    assert redact_mapping({key: 4711})[key] == "[REDACTED:secret]"
    assert redact_mapping({"shipping": "standard"})["shipping"] == "standard"


SECRET = "zQ7vK2mXp9Lw4Tn8"  # a made-up value that matches no token regex by itself

SENSITIVE_KEYS = [
    "x-goog-api-key",
    "Proxy-Authorization",
    "openai_api_key",
    "ANTHROPIC_API_KEY",
    "db_password",
    "DB_PASSWORD",
    "x-amz-security-token",
    "session_token",
    "authToken",
    "accessToken",
    "secret_key",
    "SECRET_ACCESS_KEY",
    "clientSecret",
    "client_secret",
    "passwd",
    "mysql_pwd",
    "passphrase",
    "private_key_pem",
    "connection_string",
    "ConnectionString",
    "bearer",
    "cookie",
    "Set-Cookie",
    "csrf_token",
    "api-key",
    "apikey",
    "X-Api-Key",
    "id_token",
    "refreshToken",
    "AZURE_OPENAI_API_KEY",
    "credentials",
    "aws_secret_access_key",
    "webhook_secret",
    "DATABASE_PASSWORD",
]

HARMLESS_KEYS = [
    "max_tokens",
    "maxTokens",
    "token_count",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_input_tokens",
    "token_type",
    "token_url",
    "token_endpoint",
    "token_expires_at",
    "secret_name",
    "password_length",
    "password_required",
    "api_key_env",
    "credentials_file",
    "tokenizer",
    "author",
    "keyword",
    "monkey",
    "user",
    "name",
    "path",
]


@pytest.mark.parametrize("key", SENSITIVE_KEYS)
def test_everyday_sensitive_key_names_are_masked_at_any_depth(key: str) -> None:
    result = redact_mapping({key: SECRET, "nested": [{key: SECRET}], "headers": {key: SECRET}})

    assert SECRET not in repr(result)


@pytest.mark.parametrize("key", HARMLESS_KEYS)
def test_harmless_key_names_keep_their_values(key: str) -> None:
    assert redact_mapping({key: "plain-value-1234"}) == {key: "plain-value-1234"}


def test_token_counts_stay_numbers_but_a_numeric_password_is_masked() -> None:
    assert redact_mapping({"tokens": 512, "usage": {"tokens": 7}}) == {
        "tokens": 512,
        "usage": {"tokens": 7},
    }
    assert redact_mapping({"password": 123456})["password"] == "[REDACTED:secret]"


def test_explicit_sensitive_keys_match_by_name_suffix() -> None:
    result = redact_mapping({"my_pin": "1234", "other": "keep"}, sensitive_keys=["pin"])

    assert result == {"my_pin": "[REDACTED:secret]", "other": "keep"}


# (text, secret that must be gone)
LEAKS = [
    ("x-api-key: " + SECRET, SECRET),
    ("X-Goog-Api-Key: " + SECRET, SECRET),
    ("Authorization: Bearer " + SECRET, SECRET),
    ("Authorization: Token " + SECRET, SECRET),
    ("Authorization: ApiKey " + SECRET, SECRET),
    ("Authorization: " + SECRET, SECRET),
    ("Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    (
        f"Authorization: AWS4-HMAC-SHA256 Credential={fake_secret('AKIA', 'IOSFODNN7EXAMPLE')}/20261006/eu-west-1/s3/"
        "aws4_request, SignedHeaders=host, Signature=" + "ab12" * 16,
        "ab12" * 16,
    ),
    ("{'Authorization': 'Bearer " + SECRET + "'}", SECRET),
    ('{"password": "' + SECRET + '"}', SECRET),
    ("{'db_password': '" + SECRET + "'}", SECRET),
    ('{"authToken":"' + SECRET + '"}', SECRET),
    ("password: " + SECRET, SECRET),
    ('{"passphrase": "correct horse ' + SECRET + '"}', SECRET),
    ("db_password='open sesame " + SECRET + "'", SECRET),
    ("https://api.example.com/v1/items?api_key=" + SECRET + "&page=2", SECRET),
    ("https://x.example/cb?access_token=" + SECRET, SECRET),
    ("https://x.example/cb?client_secret=" + SECRET + "&code=1", SECRET),
    ("https://s3.example/b?X-Amz-Signature=" + "0f" * 32 + "&X-Amz-Expires=60", "0f" * 32),
    ("postgresql://app:" + SECRET + "@db.internal:5432/prod", SECRET),
    ("amqp://guest:" + SECRET + "@mq/", SECRET),
    ("Server=db;Database=x;User Id=sa;Password=" + SECRET + ";Encrypt=true", SECRET),
    ("DefaultEndpointsProtocol=https;AccountName=a;AccountKey=" + SECRET + "==;", SECRET),
    ("OPENAI_API_KEY=" + SECRET + "\nHOME=/root", SECRET),
    ("export DB_PASSWORD='" + SECRET + "'", SECRET),
    ("AZURE_OPENAI_API_KEY = " + SECRET, SECRET),
    ("SESSION_TOKEN=" + SECRET, SECRET),
    ("authToken=" + SECRET, SECRET),
    ("Cookie: session=" + SECRET + "; theme=dark", SECRET),
    (
        "requests.exceptions.HTTPError: 401 Client Error for url: "
        "https://api.example.com/v2/me?key=AIza" + "A" * 35,
        "A" * 35,
    ),
    ("RuntimeError: login failed with password=" + SECRET + " for user admin", SECRET),
    ('  File "app.py", line 3, in <module>\n    client = Client(token="' + SECRET + '")', SECRET),
    ("curl -H 'x-api-key: " + SECRET + "' https://api.example.com", SECRET),
    ("curl -s -X POST --user admin:" + SECRET + " https://api.example.com", SECRET),
    # current provider key formats
    ("sk-ant-api03-" + "A1b2C3d4" * 6, "A1b2C3d4" * 6),
    ("sk-proj-" + "A1b2C3d4" * 6, "A1b2C3d4" * 6),
    ("gsk_" + "A1b2C3d4" * 6, "A1b2C3d4" * 6),
    ("xai-" + "A1b2C3d4" * 10, "A1b2C3d4" * 10),
    ("glpat-" + "A1b2C3d4e5" * 2 + "_-x", "A1b2C3d4e5" * 2),
    ("glpat-" + "A1b2C3d4e5" * 3 + ".0abc12def", "0abc12def"),
    ("pypi-AgEIcHlwaS5vcmc" + "A1b2C3d4" * 8, "A1b2C3d4" * 8),
    ("tvly-" + "A1b2C3d4" * 3, "A1b2C3d4" * 3),
    ("tvly-dev-" + "A1b2C3d4" * 3, "A1b2C3d4" * 3),
    ("pplx-" + "A1b2C3d4" * 5, "A1b2C3d4" * 5),
    ("r8_" + "A1b2C3d4e5" * 4, "A1b2C3d4e5" * 4),
    ("dapi" + "0123456789abcdef" * 2, "0123456789abcdef" * 2),
    ("dop_v1_" + "0123456789abcdef" * 4, "0123456789abcdef" * 4),
    ("ntn_" + "A1b2C3d4e5" * 5, "A1b2C3d4e5" * 5),
    ("ya29." + "A1b2C3d4e5" * 4, "A1b2C3d4e5" * 4),
    ("GOCSPX-" + "A1b2C3d4e5" * 3, "A1b2C3d4e5" * 3),
    ("xapp-1-A0123456789-1234567890123-" + "ab" * 20, "ab" * 20),
    ("shpat_" + "0123456789abcdef" * 2, "0123456789abcdef" * 2),
    ("whsec_" + "A1b2C3d4e5" * 3, "A1b2C3d4e5" * 3),
]


@pytest.mark.parametrize(("text", "secret"), LEAKS, ids=[t[:40] for t, _ in LEAKS])
def test_realistic_leaks_are_removed(text: str, secret: str) -> None:
    assert secret not in redact_secrets(text)


HARMLESS_TEXT = [
    "The token count is 5 and max_tokens=1000 for this call.",
    "Tokenization splits text; the secretary keeps a secret santa list.",
    "Password policy: at least 12 characters, no reuse.",
    "keyword=search author=jane monkey=banana",
    "commit 3f786850e387550fdab836ed7e6dc881de23001b fixed it",
    "request id 123e4567-e89b-12d3-a456-426614174000 took 35 ms",
    "See https://example.com/docs/api-keys/rotate for details.",
    "ssh://git@github.com/org/repo.git and https://user@example.com/x",
    "Set the API key in the settings page, then restart.",
    "token: <your token here> and password: ***",
    "Authorization header is required; send a bearer token.",
    "path C:\\work\\jane\\project\\src\\app.py line 12",
    "Der Schluessel liegt unter dem Fussabtreter; Passwortregeln gelten.",
    "usage: input_tokens=1200 output_tokens=345 total_tokens=1545",
    "sk-learn is a library; ask-me-anything; risk-based approach",
    "gsk and xai are short; glpat is a prefix; pplx- alone; r8_x",
]


@pytest.mark.parametrize("text", HARMLESS_TEXT)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert SECRETS_REDACTOR.redact(text) == text


def test_redaction_is_idempotent_on_the_corpus() -> None:
    for text, _ in LEAKS:
        once = redact_secrets(text)
        assert redact_secrets(once) == once


@pytest.mark.parametrize(
    "unit",
    [
        "secret-",
        "password_",
        "a:",
        "token_a",
        "x-",
        "curl ",
        fake_secret("-----BEGIN ", "PRIVATE KEY-----"),
    ],
)
def test_long_hostile_input_is_redacted_in_linear_time(unit: str) -> None:
    started = time.monotonic()
    redact_secrets(unit * 40_000)

    assert time.monotonic() - started < 20


@pytest.mark.parametrize(
    "key", ["password_input", "input_password", "secret_ref", "db_password_url", "secret_value"]
)
def test_a_harmless_word_never_exempts_a_password_or_secret(key: str) -> None:
    assert SECRET not in repr(redact_mapping({key: SECRET}))
    assert SECRET not in redact_secrets(f"{key}={SECRET}")


def test_a_private_key_cut_before_its_end_line_is_masked() -> None:
    cut = fake_secret("-----BEGIN ", "PRIVATE KEY-----\n") + "MIIEbody" * 8

    assert "MIIEbody" not in redact_secrets(cut)


IDS = {"run_id": "run_1", "root_run_id": "run_1", "agent": "a"}
PII = RegexRedactor.pii()


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Authorization: Bearer abcdefgh.ijklmnop-123", "bearer_token"),
        (f"key {fake_secret('sk-', 'proj-abcdefghijklmnopqrstuvwx')}", "api_key"),
        (f"key {fake_secret('sk-', 'ant-api03-abcdefghijklmnopqrst')}", "api_key"),
        ("Incorrect API key provided: sk-proj-****************ab12", "api_key"),
        (f"aws {fake_secret('AKIA', 'IOSFODNN7EXAMPLE')}", "api_key"),
        (f"gh {fake_secret('ghp_', 'abcdefghijklmnopqrstuvwxyz0123456789')}", "api_key"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N", "jwt"),
        ("dsn postgresql://awa:s3cr3t@db.internal:5432/awa", "url_credentials"),
        ("password=hunter22 rest", "credential"),
        ('{"api_key": "abcd1234"}', "credential"),
        (
            fake_secret("-----BEGIN RSA ", "PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"),
            "private_key",
        ),
    ],
)
def test_secret_patterns_are_redacted_by_default(text: str, kind: str) -> None:
    redacted = RegexRedactor().redact(text)

    assert f"[REDACTED:{kind}]" in redacted
    assert redact_secrets(text) == redacted


def test_group_patterns_keep_their_context() -> None:
    assert redact_secrets("Bearer abcdefgh12345") == "Bearer [REDACTED:bearer_token]"
    assert (
        redact_secrets("postgresql://awa:pw1234@host/db")
        == "postgresql://awa:[REDACTED:url_credentials]@host/db"
    )
    assert redact_secrets("password: hunter22") == "password: [REDACTED:credential]"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("mail jane.doe@example.co.uk now", "email"),
        ("IBAN DE89 3704 0044 0532 0130 00", "iban"),
        ("GB82WEST12345698765432", "iban"),
        ("card 4111 1111 1111 1111", "credit_card"),
        ("card 5500-0000-0000-0004", "credit_card"),
        ("from 192.168.1.20", "ipv4"),
        ("from 2001:db8::8a2e:370:7334", "ipv6"),
        ("call +49 30 1234567", "phone"),
        ("call (030) 1234567", "phone"),
        ("call 555-123-4567", "phone"),
    ],
)
def test_pii_patterns_are_opt_in(text: str, kind: str) -> None:
    assert f"[REDACTED:{kind}]" in PII.redact(text)
    assert f"[REDACTED:{kind}]" not in RegexRedactor().redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "card 4111 1111 1111 1112",  # fails the Luhn check
        "order 12345678901234",
        "IBAN DE89370400440532013001",  # fails mod-97
        "at 2026-09-27T08:49:20Z",
        "version 1.2.3.4.5",
        "time 12:30:45",
    ],
)
def test_checksums_and_shapes_avoid_false_positives(text: str) -> None:
    assert PII.redact(text) == text


def test_validators() -> None:
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert not luhn_valid("1234")
    assert iban_valid("DE89370400440532013000")
    assert not iban_valid("DE00370400440532013000")
    # Non-ASCII digits and letters are rejected instead of raising.
    assert not luhn_valid("4111 1111 1111 111²")
    assert not iban_valid("DE8937040044053201300²")
    assert not iban_valid("DÉ89370400440532013000")


def test_pii_kinds_and_extra_patterns() -> None:
    only_mail = RegexRedactor.pii(kinds={"email"})
    assert only_mail.redact("a@b.example 4111111111111111") == "[REDACTED:email] 4111111111111111"
    custom = RegexRedactor([*SECRET_PATTERNS, RedactionPattern("employee_id", r"EMP-\d{6}")])
    assert custom.redact("EMP-123456") == "[REDACTED:employee_id]"
    assert "employee_id" in custom.kinds
    assert RegexRedactor(replacement="***").redact("Bearer abcdefgh1234") == "Bearer ***"


def test_invalid_group_is_rejected() -> None:
    with pytest.raises(ValueError, match="group"):
        RedactionPattern("x", r"abc", group=1)


def test_find_reports_positions_not_values() -> None:
    text = "a@b.example and 4111111111111111"
    matches = PII.find(text)

    assert [m.kind for m in matches] == ["email", "credit_card"]
    assert text[matches[0].start : matches[0].end] == "a@b.example"


def test_redact_mapping_masks_sensitive_keys_and_nested_strings() -> None:
    value = {
        "X-API-Key": "abc",
        "Authorization": "Bearer zzz",
        "user": {"email": "jane@example.com", "age": 41},
        "items": ["jane@example.com", 3, ("x@y.example",)],
        "empty_token": None,
    }

    redacted = redact_mapping(value, PII)

    assert redacted["X-API-Key"] == "[REDACTED:secret]"
    assert redacted["Authorization"] == "[REDACTED:secret]"
    assert redacted["user"] == {"email": "[REDACTED:email]", "age": 41}
    assert redacted["items"] == ["[REDACTED:email]", 3, ("[REDACTED:email]",)]
    assert value["user"]["email"] == "jane@example.com"  # the input is not modified
    assert redact_mapping({"password": "x"}, sensitive_keys=())["password"] == "x"


def test_regex_redactor_satisfies_the_protocol() -> None:
    class Upper:
        def redact(self, text: str) -> str:
            return text.upper()

    assert isinstance(RegexRedactor(), Redactor)
    assert isinstance(Upper(), Redactor)
    assert len(PII.patterns) == len(SECRET_PATTERNS) + len(PII_PATTERNS)


_EMAILS = st.from_regex(
    re.compile(r"[a-z]{1,8}(\.[a-z]{1,8})?@[a-z]{2,10}\.(com|de|org|io)"), fullmatch=True
)


@settings(max_examples=60, deadline=None)
@given(prefix=st.text(alphabet="abc XYZ,;:!", max_size=20), email=_EMAILS)
def test_a_redacted_text_never_contains_the_email(prefix: str, email: str) -> None:
    assert email not in PII.redact(f"{prefix} {email} end")


@pytest.mark.parametrize("unit", ["a.", "a-", "ab"])
def test_long_dotted_runs_are_scanned_for_emails_in_linear_time(unit: str) -> None:
    import time

    started = time.monotonic()
    PII.redact(unit * 40_000)

    assert time.monotonic() - started < 20


def _luhn_complete(digits: str) -> str:
    for check in "0123456789":
        if luhn_valid(digits + check):
            return digits + check
    raise AssertionError("unreachable")


@settings(max_examples=60, deadline=None)
@given(body=st.text(alphabet="0123456789", min_size=15, max_size=15))
def test_a_redacted_text_never_contains_a_valid_card(body: str) -> None:
    card = _luhn_complete(body)
    spaced = " ".join(card[i : i + 4] for i in range(0, 16, 4))
    for form in (card, spaced):
        assert form not in PII.redact(f"pay with {form} today")


def test_event_logger_redacts_secrets_by_default(caplog: pytest.LogCaptureFixture) -> None:
    event = ErrorEvent(
        error_type="AuthError",
        message=f"bad key {fake_secret('sk-', 'abcdefghijklmnopqrstuv')}",
        **IDS,
    )
    with caplog.at_level(logging.INFO, logger="awa.events"):
        EventLogger()(event)
        EventLogger(redactor=None)(event)

    first, second = (record.getMessage() for record in caplog.records)
    assert fake_secret("sk-", "abcdefghijklmnopqrstuv") not in first
    assert "[REDACTED:api_key]" in first
    assert fake_secret("sk-", "abcdefghijklmnopqrstuv") in second


def test_event_logger_can_redact_pii(caplog: pytest.LogCaptureFixture) -> None:
    event = ErrorEvent(error_type="E", message="user jane@example.com failed", **IDS)
    with caplog.at_level(logging.INFO, logger="awa.events"):
        EventLogger(redactor=PII)(event)

    assert "jane@example.com" not in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
        ("AZURE_OPENAI_API_KEY=0123456789abcdef0123456789abcdef", "0123456789abcdef"),
        ("GITHUB_TOKEN: ghx-opaque-value-123", "ghx-opaque-value-123"),
        ("Set-Cookie: session=abc123def456; HttpOnly", "abc123def456"),
        ("Cookie: theme=dark; sid=9f8e7d6c5b4a", "9f8e7d6c5b4a"),
        ("https://x.blob.core.windows.net/c?sv=2020&sig=abcDEF%2Bxyz123", "abcDEF%2Bxyz123"),
        ("POST https://hooks.slack.com/services/T0000/B0000/XXXXXXXXXXXXXXXX", "XXXXXXXXXXXXXXXX"),
        (
            fake_secret(
                "SG.", "abcdefghijklmnopqrstuv.abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
            ),
            "abcdefghijklmn",
        ),
        ("npm_abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmn"),
        ("twilio SKabcdefabcdefabcdefabcdefabcdefab", "SKabcdef"),
        (fake_secret("AIza", "SyA-abcdefghijklmnopqrstuvwxyz012345"), "012345"),
    ],
)
def test_more_secret_shapes_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact_secrets(text)


@pytest.mark.parametrize(
    "text",
    [
        "Basic authentication needs a basic understanding of headers.",
        "The cookie: chocolate chip is a kind of biscuit.",
        "Set the token limit to 4096 and max_tokens=100.",
        "sig=short and design=something-long-enough",
        "A hook at https://hooks.example.com/services/team is fine.",
        "SKU12345 and SG.1 and npm_install are plain words.",
        "The aws account number is 123456789012.",
        "next_page=2 key=value",
    ],
)
def test_normal_text_is_not_redacted(text: str) -> None:
    assert redact_secrets(text) == text


@pytest.mark.parametrize("text", ["pwd=/run/secrets/app", r"pwd=C:\vault\app"])
def test_a_working_directory_after_pwd_is_left_alone(text: str) -> None:
    assert redact_secrets(text) == text


def test_a_pwd_value_that_is_no_path_is_redacted() -> None:
    assert redact_secrets("pwd=hunter2x") == "pwd=[REDACTED:credential]"
