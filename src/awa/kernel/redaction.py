"""Redaction of secrets and personal data in logs, traces, audit records and payloads.

`RegexRedactor` applies `SECRET_PATTERNS` by default and `PII_PATTERNS` on request;
`redact_mapping` cleans nested structures, `redact_values` removes known secret values. Regular
expressions are a floor, not a guarantee: implement `Redactor` to plug in a detector of your own.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
import urllib.parse
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

DEFAULT_REPLACEMENT = "[REDACTED:{kind}]"
"""Replacement template of `RegexRedactor`; `{kind}` is the pattern's kind."""


@runtime_checkable
class Redactor(Protocol):
    """Replaces sensitive parts of a text; must be pure and fast. Stable API as `awa.contracts.Redactor`."""

    def redact(self, text: str) -> str:
        """Return `text` with sensitive parts replaced."""
        ...


@dataclass(frozen=True)
class RedactionPattern:
    """One kind of sensitive text, found by a regular expression.

    Attributes:
        kind: Short name used in the replacement (`"email"`, `"iban"`).
        group: The group to replace; 0 replaces the whole match.
        validate: Check on the matched group; a rejected match is left alone.
    """

    kind: str
    pattern: str | re.Pattern[str]
    group: int = 0
    validate: Callable[[str], bool] | None = None
    compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        compiled = (
            self.pattern if isinstance(self.pattern, re.Pattern) else re.compile(self.pattern)
        )
        if self.group > compiled.groups:
            raise ValueError(
                f"Pattern {self.kind!r} has {compiled.groups} groups; group={self.group} is invalid"
            )
        object.__setattr__(self, "compiled", compiled)


@dataclass(frozen=True)
class RedactionMatch:
    """Where a `RegexRedactor` found sensitive text (`kind`, `start`, `end`; the text is not kept)."""

    kind: str
    start: int
    end: int


def luhn_valid(text: str) -> bool:
    """Whether the digits in `text` (13 to 19 of them) pass the Luhn checksum."""

    digits = [int(c) for c in text if c.isdecimal()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def iban_valid(text: str) -> bool:
    """Whether `text` (spaces allowed) is an IBAN with a valid mod-97 checksum."""

    compact = text.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34 or not (compact.isascii() and compact.isalnum()):
        return False
    rearranged = compact[4:] + compact[:4]
    number = "".join(str(int(c, 36)) for c in rearranged)
    return int(number) % 97 == 1


def _ipv6_valid(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _phone_valid(text: str) -> bool:
    digits = sum(c.isdigit() for c in text)
    return 8 <= digits <= 15


_HARMLESS_WORDS = (
    "count|counts|limit|limits|max|min|num|total|input|output|prompt|completion|reasoning|budget"
    "|expires|expiry|expiration|ttl|url|uri|endpoint|length|len|name|env|ref|path|file|required"
    "|enabled|policy|type|usage|used|remaining|scope|scopes|kind"
)
_NAME_TAIL = r"(?:[_-](?!(?:" + _HARMLESS_WORDS + r")(?![A-Za-z0-9]))[A-Za-z0-9]+){0,3}"
_DESCRIBING_WORDS = (
    "length|len|min|max|count|required|enabled|policy|name|env|file|path|type|kind|expires"
    "|expiry|expiration|ttl"
)
"""The only words that make a password, secret or private key name harmless (they describe it
and never hold it): `password_length`, `secret_name`. `password_input` stays a secret."""
_STRONG_TAIL = r"(?:[_-](?!(?:" + _DESCRIBING_WORDS + r")(?![A-Za-z0-9]))[A-Za-z0-9]+){0,3}"
_VALUE = (
    r"(?![<*]|\$\{|\[REDACTED)"
    r"((?<=\")[^\"\r\n]{4,512}(?=\")|(?<=')[^'\r\n]{4,512}(?=')|[^\s\"',;&]{4,})"
)
"""A credential value: up to the closing quote when it is quoted, else up to a space."""

SECRET_PATTERNS: tuple[RedactionPattern, ...] = (
    RedactionPattern(
        "private_key",
        # A block ends at its END line, before the next BEGIN line or at the end of a cut text.
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:(?!-----BEGIN )[\s\S])*?"
        r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|(?=-----BEGIN )|\Z)",
    ),
    RedactionPattern("jwt", r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
    RedactionPattern("bearer_token", r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{8,}=*)", group=1),
    RedactionPattern(
        "authorization",
        r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?"
        r"(?:(?:basic|digest|negotiate|ntlm|bearer|token|api-?key|aws4-hmac-sha256|hmac)\s+)?"
        r"(?!\[REDACTED)([^\s\"',;]{8,})(?![^\s\"',;]|\s+\[REDACTED)",
        group=1,
    ),
    RedactionPattern("cookie", r"(?i)\b(?:set-)?cookie\s*:\s*([^\s=;]+=[^\r\n]*)", group=1),
    RedactionPattern(
        "signed_url",
        r"(?i)(?<![A-Za-z0-9])(?:x-amz-signature|x-goog-signature|signature|sig)="
        r"([A-Za-z0-9%+/=_-]{8,})",
        group=1,
    ),
    RedactionPattern("webhook_url", r"\bhttps://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    RedactionPattern(
        "aws_secret_key",
        r"(?i)\baws[_-]?secret[_-]?access[_-]?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})",
        group=1,
    ),
    RedactionPattern(
        "api_key",
        # Provider key formats; `sk-`/`pk-`/`rk-` keys also when short or partly masked
        # (`sk-proj-****ab12`).
        r"\b(?:(?:sk|pk|rk)-[A-Za-z0-9_*-]{6,}"
        r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"
        r"|whsec_[A-Za-z0-9]{24,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|AIza[0-9A-Za-z_-]{35,}"
        r"|ya29\.[A-Za-z0-9_-]{20,}"
        r"|GOCSPX-[A-Za-z0-9_-]{20,}"
        r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
        r"|npm_[A-Za-z0-9]{30,}"
        r"|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}"
        r"|SK[0-9a-f]{32}"
        r"|gh[pousr]_[A-Za-z0-9]{30,}"
        r"|github_pat_[A-Za-z0-9_]{30,}"
        r"|glpat-[A-Za-z0-9_-]{20,}(?:\.[0-9a-z]{9})?"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|xapp-[A-Za-z0-9-]{20,}"
        r"|hf_[A-Za-z0-9]{30,}"
        r"|gsk_[A-Za-z0-9]{20,}"
        r"|xai-[A-Za-z0-9]{20,}"
        r"|tvly-(?:dev-|prod-)?[A-Za-z0-9]{16,}"
        r"|pplx-[A-Za-z0-9]{32,}"
        r"|r8_[A-Za-z0-9]{35,}"
        r"|dapi[0-9a-f]{32}(?:-\d)?"
        r"|dop_v1_[a-f0-9]{64}"
        r"|shpat_[a-f0-9]{32}"
        r"|ntn_[A-Za-z0-9]{40,})",
    ),
    RedactionPattern(
        "url_credentials",
        r"(?<![A-Za-z0-9+.-])[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:([^\s/]+)@",
        group=1,
    ),
    RedactionPattern(
        "credential",
        r"(?i)\bcurl\b[^\r\n|;&]{0,300}?\s(?:-u|--user)[\s=]+[\"']?[^\s:\"']+:([^\s\"']{3,})",
        group=1,
    ),
    RedactionPattern(
        "credential",
        # `name = value` where the name contains a secret word: `db_password=`, `X-Api-Key:`,
        # `authToken=`, `"client_secret": "..."`. A name ending in a harmless word
        # (`password_length=12`, `secret_name=prod`) is left alone.
        r"(?i)(?:api[_-]?key|access[_-]?key|account[_-]?key|shared[_-]?access[_-]?key"
        r"|credentials?)" + _NAME_TAIL + r"[\"']?\s*[:=]\s*[\"']?" + _VALUE,
        group=1,
    ),
    RedactionPattern(
        "credential",
        # Passwords, secrets and private keys: only a describing word (`_DESCRIBING_WORDS`)
        # after the name leaves the value alone.
        r"(?i)(?:secret(?:[_-]?key)?|client[_-]?secret|private[_-]?key|passw(?:or)?d|passphrase)"
        + _STRONG_TAIL
        + r"[\"']?\s*[:=]\s*[\"']?"
        + _VALUE,
        group=1,
    ),
    RedactionPattern(
        "credential",
        r"(?i)(?<![A-Za-z0-9])pwd\b[\"']?\s*[:=]\s*[\"']?(?![/\\]|[A-Za-z]:[/\\])" + _VALUE,
        group=1,
    ),
    RedactionPattern(
        "credential",
        r"(?i)token(?![A-Za-z0-9])" + _NAME_TAIL + r"[\"']?\s*[:=]\s*[\"']?" + _VALUE,
        group=1,
        validate=lambda value: (
            not value.isdigit()
            and value.lower() not in {"none", "null", "true", "false", "undefined"}
        ),
    ),
)
"""Built-in patterns for credentials (on by default wherever awa redacts)."""

PII_PATTERNS: tuple[RedactionPattern, ...] = (
    RedactionPattern(
        "email",
        r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
    ),
    RedactionPattern("iban", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b", validate=iban_valid),
    RedactionPattern(
        "credit_card", r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])", validate=luhn_valid
    ),
    RedactionPattern(
        "ipv6",
        r"(?<![\w:.])(?:[0-9A-Fa-f]{1,4}:){1,7}(?::?[0-9A-Fa-f]{1,4}){1,7}(?:%\w+)?(?![\w:])"
        r"|(?<![\w:.])(?:[0-9A-Fa-f]{1,4}:){1,7}:(?![\w:])"
        r"|(?<![\w:.])::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}(?![\w:])",
        validate=_ipv6_valid,
    ),
    RedactionPattern(
        "ipv4",
        r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\.?\d)",
    ),
    RedactionPattern(
        "phone",
        r"(?<![\w+])(?:\+|00)[1-9]\d{0,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]?\d{2,4}){1,4}(?!\w)"
        r"|(?<!\w)\(?0\d{1,4}\)?[ ./-]?\d{3,4}(?:[ .-]?\d{2,4}){0,3}(?!\w)"
        r"|(?<!\w)\(\d{3}\) ?\d{3}-\d{4}(?!\w)"
        r"|(?<![\w-])\d{3}-\d{3}-\d{4}(?![\w-])",
        validate=_phone_valid,
    ),
)
"""Built-in patterns for personal data (opt-in)."""


class RegexRedactor:
    """Redacts every match of a list of `RedactionPattern`s, applied in order (most specific first).

    Args:
        patterns: The patterns; defaults to `SECRET_PATTERNS`.
        replacement: Replacement template; `{kind}` is the pattern's kind.

    Example:
        ```python
        RegexRedactor.pii().redact("Mail jane@example.com, card 4111 1111 1111 1111")
        # 'Mail [REDACTED:email], card [REDACTED:credit_card]'
        ```
    """

    def __init__(
        self,
        patterns: Iterable[RedactionPattern] | None = None,
        *,
        replacement: str = DEFAULT_REPLACEMENT,
    ) -> None:
        self.patterns: tuple[RedactionPattern, ...] = (
            SECRET_PATTERNS if patterns is None else tuple(patterns)
        )
        self.replacement = replacement

    @classmethod
    def secrets(cls, *, replacement: str = DEFAULT_REPLACEMENT) -> RegexRedactor:
        """A redactor for credentials only (the default everywhere)."""

        return cls(SECRET_PATTERNS, replacement=replacement)

    @classmethod
    def pii(
        cls,
        *,
        kinds: Collection[str] | None = None,
        extra: Sequence[RedactionPattern] = (),
        replacement: str = DEFAULT_REPLACEMENT,
    ) -> RegexRedactor:
        """A redactor for credentials and personal data.

        Args:
            kinds: Restrict the built-in PII patterns to these kinds; secrets are always included.
            extra: Additional patterns, applied last.
        """

        pii = [p for p in PII_PATTERNS if kinds is None or p.kind in kinds]
        return cls([*SECRET_PATTERNS, *pii, *extra], replacement=replacement)

    @property
    def kinds(self) -> tuple[str, ...]:
        """The kinds of the configured patterns, in order."""

        return tuple(pattern.kind for pattern in self.patterns)

    def redact(self, text: str) -> str:
        """Return `text` with every match replaced."""

        if not text:
            return text
        for pattern in self.patterns:
            text = self._apply(pattern, text)
        return text

    def find(self, text: str) -> list[RedactionMatch]:
        """Return where each pattern matches `text` (checked separately, not chained)."""

        found: list[RedactionMatch] = []
        for pattern in self.patterns:
            for match in pattern.compiled.finditer(text):
                if self._accepted(pattern, match):
                    start, end = match.span(pattern.group)
                    found.append(RedactionMatch(pattern.kind, start, end))
        return sorted(found, key=lambda m: (m.start, m.end))

    def _apply(self, pattern: RedactionPattern, text: str) -> str:
        replacement = self.replacement.replace("{kind}", pattern.kind)

        def substitute(match: re.Match[str]) -> str:
            if not self._accepted(pattern, match):
                return match.group(0)
            if pattern.group == 0:
                return replacement
            whole_start = match.start(0)
            start, end = match.span(pattern.group)
            full = match.group(0)
            return full[: start - whole_start] + replacement + full[end - whole_start :]

        return pattern.compiled.sub(substitute, text)

    @staticmethod
    def _accepted(pattern: RedactionPattern, match: re.Match[str]) -> bool:
        if match.group(pattern.group) is None:
            return False
        return pattern.validate is None or pattern.validate(match.group(pattern.group))


SECRETS_REDACTOR: RegexRedactor = RegexRedactor.secrets()
"""The shared secrets-only redactor used by default."""


def strip_invisible(value: Any) -> Any:
    """`value` without invisible format characters (Unicode category Cf, such as the zero-width
    space) in every string and mapping key, so that one inside an address does not hide it."""

    if isinstance(value, str):
        return (
            value
            if value.isascii()
            else "".join(c for c in value if unicodedata.category(c) != "Cf")
        )
    if isinstance(value, Mapping):
        return {strip_invisible(k): strip_invisible(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [strip_invisible(item) for item in value]
    return value


def fold_text(text: str) -> str:
    """`text` folded for matching: NFKC (fullwidth and compatibility forms), no invisible
    format characters, case-folded. Look-alike letters of another script are not folded."""

    if text.isascii():
        return text.casefold()
    return str(strip_invisible(unicodedata.normalize("NFKC", text))).casefold()


def redact_secrets(text: str) -> str:
    """Redact credentials in `text` with `SECRETS_REDACTOR`."""

    return SECRETS_REDACTOR.redact(text)


def redact_values(
    text: str, secrets: Iterable[str | None], *, min_len: int = 4, replacement: str = "***"
) -> str:
    """Return `text` with every occurrence of the given secret values replaced.

    Longer secrets go first, URL-encoded forms are removed too, and values shorter than `min_len`
    are ignored so ordinary text is not garbled.
    """

    if not text:
        return text
    values = {secret for secret in secrets if secret and len(secret) >= min_len}
    for secret in sorted(values, key=len, reverse=True):
        text = text.replace(secret, replacement)
        quoted = urllib.parse.quote(secret, safe="")
        if quoted != secret:
            text = text.replace(quoted, replacement)
    return text


def _key_words(key: str) -> tuple[str, ...]:
    """Lower-case words of a key name: `authToken` and `AUTH_TOKEN` give `("auth", "token")`."""

    return tuple(w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", key))


_SECRET_PHRASES = (
    "apikey",
    "privatekey",
    "secretkey",
    "accesskey",
    "accountkey",
    "signingkey",
    "encryptionkey",
    "clientsecret",
    "connectionstring",
    "connstr",
    "authorization",
    "password",
    "passwd",
    "passphrase",
    "credential",
)
_SECRET_WORDS = frozenset(
    {"token", "tokens", "secret", "secrets", "bearer", "cookie", "cookies", "dsn", "pwd"}
    # PINs, one-time passwords and card security codes.
    | {"pin", "otp", "passcode", "cvv", "cvc"}
)
_HARMLESS_KEY_WORDS = frozenset(_HARMLESS_WORDS.split("|"))
_DESCRIBING_KEY_WORDS = frozenset(_DESCRIBING_WORDS.split("|"))
_STRONG_PHRASES = ("password", "passwd", "passphrase", "secret", "privatekey")
_COUNTER_WORDS = frozenset({"token", "tokens"})


def _is_sensitive_key(key: str, value: Any) -> bool:
    """Whether a key names a secret: a secret word as a whole word (`db_password`, not `max_tokens`)."""

    words = _key_words(key)
    compact = "".join(words)
    if any(phrase in compact for phrase in _STRONG_PHRASES):
        # Never exempted by a harmless word: only a last word that describes it does.
        return not (len(words) > 1 and words[-1] in _DESCRIBING_KEY_WORDS)
    if _HARMLESS_KEY_WORDS.intersection(words):
        return False
    secret_words = _SECRET_WORDS.intersection(words)
    if not secret_words and not any(phrase in compact for phrase in _SECRET_PHRASES):
        return False
    # A bare `tokens: 512` is a counter; a number under `password` is still a password.
    only_counter = secret_words <= _COUNTER_WORDS and not any(p in compact for p in _SECRET_PHRASES)
    return not (only_counter and isinstance(value, bool | int | float))


def _matches_custom_key(key: str, custom: tuple[tuple[str, ...], ...]) -> bool:
    words = _key_words(key)
    return any(words[-len(name) :] == name or "".join(words) == "".join(name) for name in custom)


def redact_mapping(
    value: Any,
    redactor: Redactor | None = None,
    *,
    sensitive_keys: Collection[str] | None = None,
    mask: str = "[REDACTED:secret]",
) -> Any:
    """Return a copy of `value` with every string redacted and sensitive keys masked.

    Values under a sensitive key become `mask` whatever their type (except plain numbers under
    `token`/`tokens`, which are counters).

    Args:
        redactor: Redactor for strings; defaults to `SECRETS_REDACTOR`.
        sensitive_keys: Key names whose values are masked; a key matches when it ends in one as
            whole words (`pin` matches `my_pin`). `()` masks none.

    Example:
        ```python
        redact_mapping({"user": "jane@example.com", "password": "hunter2"}, RegexRedactor.pii())
        # {'user': '[REDACTED:email]', 'password': '[REDACTED:secret]'}
        ```
    """

    active = SECRETS_REDACTOR if redactor is None else redactor
    custom = None if sensitive_keys is None else tuple(_key_words(k) for k in sensitive_keys)
    return _redact(value, active, custom, mask)


def redact_keys(value: Any, redactor: Redactor) -> Any:
    """A copy of `value` with every string key redacted; keys that become equal get a `#` appended."""

    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new = redactor.redact(key) if isinstance(key, str) else key
            while new in out:
                new = f"{new}#"
            out[new] = redact_keys(item, redactor)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_keys(item, redactor) for item in value]
    return value


def _masks(key: str, value: Any, custom: tuple[tuple[str, ...], ...] | None) -> bool:
    return _is_sensitive_key(key, value) if custom is None else _matches_custom_key(key, custom)


def _redact(
    value: Any, redactor: Redactor, custom: tuple[tuple[str, ...], ...] | None, mask: str
) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, Mapping):
        return {
            k: (
                mask
                if isinstance(k, str) and v not in (None, "") and _masks(k, v, custom)
                else _redact(v, redactor, custom, mask)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        items = [_redact(item, redactor, custom, mask) for item in value]
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, set | frozenset):
        return type(value)(_redact(item, redactor, custom, mask) for item in value)
    return value


__all__ = [
    "DEFAULT_REPLACEMENT",
    "PII_PATTERNS",
    "SECRETS_REDACTOR",
    "SECRET_PATTERNS",
    "RedactionMatch",
    "RedactionPattern",
    "Redactor",
    "RegexRedactor",
    "fold_text",
    "iban_valid",
    "luhn_valid",
    "redact_keys",
    "redact_mapping",
    "redact_secrets",
    "redact_values",
    "strip_invisible",
]
