"""Personal data in a column from column-name hints and value rules, without a model."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from functools import cache
from itertools import chain, islice
from typing import NamedTuple, get_args

from awa.data._model import PiiInfo, PiiKind, column_key
from awa.kernel.redaction import PII_PATTERNS

MAX_VALUES = 20_000
"""Most non-empty values `classify_pii` examines per column."""

_KINDS: tuple[PiiKind, ...] = get_args(PiiKind)
REDACTION_KINDS: dict[str, PiiKind] = {
    "email": "email",
    "iban": "iban",
    "credit_card": "card",
    "ipv4": "ip",
    "ipv6": "ip",
    "phone": "phone",
}
"""The PII kind of each pattern kind of `awa.kernel.redaction`."""
_WHOLE_VALUE: frozenset[PiiKind] = frozenset({"card", "phone"})
_STRICT: frozenset[PiiKind] = frozenset({"email", "iban"})
_NEEDS_HINT: frozenset[PiiKind] = frozenset({"name", "address", "birthdate"})

# Column-name hints, matched on `column_key(name)` (lower case, `_` between words; umlauts are `_`).
_HINT_PATTERNS: dict[PiiKind, str] = {
    "email": r"e_?mail|(?:^|_)mail(?:_|$)",
    "iban": r"iban",
    "card": r"card_?(?:number|no|num)|karten_?(?:nummer|nr)|kreditkarte|credit_?card"
    r"|(?:^|_)(?:pan|cc_?(?:number|num|no))(?:_|$)",
    "ip": r"(?:^|_)ip(?:_?addr(?:ess)?|_?adresse)?(?:_|$)",
    "phone": r"phone|telefon|telephone|(?:^|_)tel(?:_|$)|mobil|handy|fax|rufnummer|(?:^|_)gsm"
    r"(?:_|$)|cell(?:phone)?(?:_|$)",
    "name": r"vorname|nachname|familienname|surname|(?:first|last|full|given|family|middle)"
    r"_?name|ansprechpartner|kontaktperson|contact_?person|bewerber|candidate|applicant"
    r"|mitarbeiter(?:_?name)?|(?:customer|kunde|employee|user|contact|owner|patient|client"
    r"|requester|approver|reviewer|author|sender|recipient|empf_nger)_?name|^names?$",
    "address": r"(?<!mail_)(?<!ip_)adress|(?<!mail_)(?<!ip_)address|anschrift|strasse|stra_e"
    r"|street|wohnort",
    "tax_id": r"steuer_?id|steuernummer|steuer_?nr|tax_?id|tax_?number|ust_?id|vat_?(?:id|no"
    r"|number)|(?:^|_)tin(?:_|$)|umsatzsteuer|identifikationsnummer",
    "birthdate": r"geburt|birth|(?:^|_)dob(?:_|$)|(?:^|_)geb(?:_|$)|geboren",
}
_HINTS = {kind: re.compile(pattern) for kind, pattern in _HINT_PATTERNS.items()}

_COMPANY_TEXT = (
    "gmbh mbh ag kg se ug ohg gbr ltd llc inc corp co bv nv sarl srl spa plc sa oy ab aps"
)
_COMPANY_WORDS = frozenset(_COMPANY_TEXT.split()) | {"ek", "e.k.", "&", "und", "and"}
_PARTICLE_TEXT = "von van de der den zu da di del la le bin"
_PARTICLES = frozenset(_PARTICLE_TEXT.split())
_NAME_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*\.?")
_DATE = re.compile(
    r"(?:\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}|(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2})(?:[ T].*)?"
)
_PHONE_SHAPE = re.compile(r"[0-9+()/.\- ]{6,25}")
_CARD_SHAPE = re.compile(r"[0-9 -]{13,23}")
_VAT_DE = re.compile(r"DE\d{9}")
_ELEVEN_DIGITS = re.compile(r"[1-9]\d{10}")


def _steuer_id_valid(text: str) -> bool:
    """German tax identification number: eleven digits, check digit by ISO 7064 MOD 11,10."""

    if not _ELEVEN_DIGITS.fullmatch(text):
        return False
    product = 10
    for digit in text[:10]:
        total = (int(digit) + product) % 10 or 10
        product = (total * 2) % 11
    return (11 - product) % 10 == int(text[10])


def _person_like(text: str) -> bool:
    words = text.replace(",", " ").split()
    if not 2 <= len(words) <= 4:
        return False
    for word in words:
        if word.lower() in _COMPANY_WORDS or not _NAME_WORD.fullmatch(word):
            return False
        if not (word[0].isupper() or word in _PARTICLES):
            return False
    return True


def _digits(text: str) -> int:
    return sum(char.isdigit() for char in text)


def _looks_like_address(text: str) -> bool:
    letters = any(char.isalpha() for char in text)
    return len(text) >= 5 and letters and (_digits(text) > 0 or "," in text)


_PLAUSIBLE: dict[PiiKind, Callable[[str], bool]] = {
    "name": _person_like,
    "address": _looks_like_address,
    "birthdate": lambda text: _DATE.fullmatch(text) is not None,
    "tax_id": lambda text: sum(char.isalnum() for char in text) >= 8,
    "phone": lambda text: _PHONE_SHAPE.fullmatch(text) is not None and 6 <= _digits(text) <= 15,
    "card": lambda text: _CARD_SHAPE.fullmatch(text) is not None and 13 <= _digits(text) <= 19,
}
"""What a value must look like for a column whose name says it holds this kind."""


def _hinted(name: str | None) -> frozenset[PiiKind]:
    key = column_key(name) if name else ""
    return frozenset(kind for kind, hint in _HINTS.items() if hint.search(key))


_CONTAINS = {"email": "@", "ipv6": ":", "ipv4": "."}
"""A character that every match of the pattern of this redaction kind contains: a value without it
is not tried against the pattern (the same answer, found sooner)."""
_SHORTEST = {"iban": 15, "credit_card": 13, "phone": 8}
"""The fewest characters a match of the pattern of this redaction kind has (a phone number has
eight digits)."""


class _Rule(NamedTuple):
    kind: PiiKind
    compiled: re.Pattern[str]
    whole: bool  # the match must be the whole value
    validate: Callable[[str], bool] | None
    contains: str | None
    shortest: int


@cache
def _rules() -> tuple[_Rule, ...]:
    return tuple(
        _Rule(
            REDACTION_KINDS[pattern.kind],
            pattern.compiled,
            REDACTION_KINDS[pattern.kind] in _WHOLE_VALUE,
            pattern.validate,
            _CONTAINS.get(pattern.kind),
            _SHORTEST.get(pattern.kind, 0),
        )
        for pattern in PII_PATTERNS
    )


def _evidence(text: str, hints: frozenset[PiiKind]) -> set[PiiKind]:
    """The kinds this one value is evidence of (the thresholds are applied per column)."""

    found: set[PiiKind] = set()
    size = len(text)
    for rule in _rules():
        if (
            rule.kind in found
            or size < rule.shortest
            or (rule.contains and rule.contains not in text)
        ):
            continue
        for match in rule.compiled.finditer(text):
            whole = not rule.whole or match.group(0) == text
            if whole and (rule.validate is None or rule.validate(match.group(0))):
                found.add(rule.kind)
                break
    if "phone" in found and "phone" not in hints and text.isdigit():
        found.discard("phone")  # a bare digit string is an id, not a phone number
    if len(text) == 11 and (_VAT_DE.fullmatch(text) or _steuer_id_valid(text)):  # both: 11 chars
        found.add("tax_id")
    for kind in hints:
        check = _PLAUSIBLE.get(kind)
        if check is not None and check(text):
            found.add(kind)
    return found


def classify_pii(values: Iterable[object], name: str | None = None) -> PiiInfo:
    """The kinds of personal data in a column and the share of values that carry them.

    `email` and `iban` count from 2 % of the values; `card`, `ip`, `phone`, `tax_id` need half, or a
    fitting column name and a fifth; `name`, `address`, `birthdate` need a fitting name and half.
    The rules err toward masking (a product column `Name` is reported as `name`). At most 20,000
    non-empty values are examined.

    Returns:
        `PiiInfo(kinds, share)`: `kinds` in the order of `PiiKind`, `share` the fraction of
        non-empty values that carry a reported kind.

    Example:
        ```python
        classify_pii(["anna@example.com", "tom@example.org", ""], "Contact")
        # PiiInfo(kinds=['email'], share=1.0)
        ```
    """

    texts = (text for text in (str(value).strip() for value in values if value is not None) if text)
    cells = list(islice(texts, MAX_VALUES))
    if not cells:
        return PiiInfo()
    hints = _hinted(name)
    evidence = [_evidence(text, hints) for text in cells]
    counts = Counter(chain.from_iterable(evidence))  # values per kind
    kept: set[PiiKind] = set()
    for kind in _KINDS:
        share = counts[kind] / len(cells)
        if kind in _STRICT:
            needed = 0.02
        elif kind in _NEEDS_HINT:
            needed = 0.5 if kind in hints else 2.0
        else:
            needed = 0.2 if kind in hints else 0.5
        if share >= needed:
            kept.add(kind)
    carrying = sum(not found.isdisjoint(kept) for found in evidence)
    return PiiInfo(
        kinds=[kind for kind in _KINDS if kind in kept], share=round(carrying / len(cells), 4)
    )
