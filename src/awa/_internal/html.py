"""A small HTML tokenizer that reads hostile input in time proportional to its size.

Subclass `HtmlEvents`, override the `handle_*` methods and call `parse`. Names arrive in lower
case, attribute values and text with character references resolved. A tag keeps at most
`MAX_ATTRIBUTES` attributes; subclasses must keep their own work per event constant.
"""

from __future__ import annotations

import re
from html import unescape
from itertools import islice

MAX_ATTRIBUTES = 128

_TOKEN = re.compile(
    r"""
      <!--(?:-?>|(?P<comment>.*?)(?:-->|\Z))
    | <!(?:\[CDATA\[.*?(?:\]\]>|\Z)|[^>]*>?)
    | <\?[^>]*>?
    | </(?P<close>[A-Za-z][^\s/>]*)[^>]*>?
    | <(?P<open>[A-Za-z][^\s/>]*)(?P<attrs>(?:[^>"']+|"[^"]*"?|'[^']*'?)*)>?
    | (?P<text>(?:[^<]+|<(?![A-Za-z!?]|/[A-Za-z]))+)
    """,
    re.VERBOSE | re.DOTALL,
)
_COMMENT, _CLOSE, _OPEN, _ATTRS, _TEXT = 1, 2, 3, 4, 5  # group numbers of _TOKEN
_ATTRIBUTE = re.compile(
    r"""([^\s"'/=<>][^\s/=>]*)(?:\s*=\s*(?:"([^"]*)"?|'([^']*)'?|([^\s>]*)))?""", re.DOTALL
)
_RAW_END = {
    "script": re.compile(r"</script", re.IGNORECASE),
    "style": re.compile(r"</style", re.IGNORECASE),
}


class HtmlEvents:
    """Calls `handle_starttag`, `handle_endtag`, `handle_data` and `handle_comment` in order."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """An opening tag; `attrs` holds `(name, value)`, the value is None without `=`."""

    def handle_endtag(self, tag: str) -> None:
        """A closing tag."""

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """A tag written `<br/>`; by default an opening tag followed by a closing tag."""

        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        """Text between tags (character references resolved), or the raw body of a script."""

    def handle_comment(self, data: str) -> None:
        """The text between `<!--` and `-->` (up to the end of the input if never closed)."""

    def parse(self, html: str) -> None:
        """Read the whole page and call the handlers; never raises on malformed input."""

        match_at = _TOKEN.match
        position, end = 0, len(html)
        while position < end:
            match = match_at(html, position)
            if match is None:  # cannot happen: the last alternative takes any character
                break
            position = match.end()
            kind = match.lastindex
            if kind == _TEXT:
                text = match[_TEXT]
                self.handle_data(unescape(text) if "&" in text else text)
            elif kind == _ATTRS:
                position = self._read_start(html, match, position)
            elif kind == _CLOSE:
                self.handle_endtag(match[_CLOSE].lower())
            elif kind == _COMMENT:
                self.handle_comment(match[_COMMENT])

    def _read_start(self, html: str, match: re.Match[str], position: int) -> int:
        tag = match[_OPEN].lower()
        attrs_text = match[_ATTRS]
        attrs: list[tuple[str, str | None]] = []
        if attrs_text and not attrs_text.isspace():
            attributes_end = 0
            for found in islice(_ATTRIBUTE.finditer(attrs_text), MAX_ATTRIBUTES):
                name, double, single, bare = found.groups()
                attrs.append((name.lower(), _value(double, single, bare)))
                attributes_end = found.end()
            closing = attrs_text.rstrip()
            if closing.endswith("/") and len(closing) > attributes_end:
                self.handle_startendtag(tag, attrs)
                return position
        self.handle_starttag(tag, attrs)
        raw_end = _RAW_END.get(tag)
        if raw_end is None:
            return position
        closing = raw_end.search(html, position)
        stop = closing.start() if closing else len(html)
        if stop > position:
            self.handle_data(html[position:stop])  # raw text: no character references
        return stop


def _value(double: str | None, single: str | None, bare: str | None) -> str | None:
    value = double if double is not None else single if single is not None else bare
    return unescape(value) if value else value
