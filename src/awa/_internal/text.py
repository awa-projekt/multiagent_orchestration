"""Word splitting, stemming and stop words of the lexical matchers."""

from __future__ import annotations

import re

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[^\W_]+")
_MIN_STEM = 3

STOPWORDS = frozenset(
    (  # noqa: SIM905
        "a about an and any are as at be by can do does for from has have how i if in into is it "
        "its me my of on or our so that the their them then there these they this to us was "
        "we what when where which who why will with you your"
    ).split()
)
"""English function words that say nothing about a topic."""


def words(text: str) -> list[str]:
    """The lowercase words of `text`, split at punctuation and camelCase."""

    return _WORD.findall(_CAMEL.sub(" ", text).lower())


def stem(word: str) -> str:
    """A crude stem of a lowercase word: one common suffix stripped."""

    if word.endswith("ies") and len(word) - 3 >= _MIN_STEM:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    for suffix in ("ing", "ers", "er", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[: -len(suffix)]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > _MIN_STEM:
        return word[:-1]
    return word


def terms(text: str, stopwords: frozenset[str] = STOPWORDS) -> list[str]:
    """The stems of the words of `text` that are not in `stopwords`, in order."""

    return [stem(word) for word in words(text) if word not in stopwords]
