"""Web tools: `fetch_url`, `web_search`, `verify_quote`.

Requests go only to allowed public hosts (private addresses need `allow_private_networks`),
responses are capped and labeled as external data. `web_search` works without configuration
(the keyless `DuckDuckGoSearchProvider`) or with your own provider. Calls to an HTTP API go
through the `rest` or `openapi` connector.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from awa._internal.html import HtmlEvents
from awa._internal.http import HttpPolicy, HttpResponse, fetch, normalize_hosts
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.errors import OptionalDependencyError
from awa.toolkits._common import DEFAULT_MAX_OUTPUT_CHARS, ToolArgs, build_tool, positive, selected
from awa.toolkits._search import (
    DuckDuckGoSearchProvider,
    JSONSearchProvider,
    SearchBlockedError,
    SearchError,
    SearchFormatError,
    SearchProvider,
    SearchResult,
    fixture_search,
    search_provider,
    unique_results,
)
from awa.toolkits.documents import pdf_text
from awa.tools.risk import ToolRisk

WEB_TOOLS = ("fetch_url", "web_search", "verify_quote")
"""Names of the tools `web_toolkit` can build, in order."""

_OPT_IN = ("verify_quote",)  # built only when `include` names them


_SKIPPED_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas", "head"}
)
_VOID_TAGS = frozenset(
    """
    area base br col embed hr img input link meta
    param source track wbr
    """.split()  # noqa: SIM905 - a word list is easier to read and extend
)
_BLOCK_TAGS = frozenset(
    """
    p div section article header footer main nav aside li
    ul ol table tr h1 h2 h3 h4 h5 h6
    blockquote pre form dl dt dd figure figcaption address details
    summary
    """.split()  # noqa: SIM905 - a word list is easier to read and extend
)


_MAX_HTML_DEPTH = 256


class _TextExtractor(HtmlEvents):
    """Visible text of an HTML page; skips scripts, styles and hidden elements."""

    def __init__(self, *, keep_links: bool) -> None:
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._stack: list[tuple[str, bool]] = []
        self._open: Counter[str] = Counter()  # names on the stack: end tags cost O(1) to match
        self._skipped = 0  # entries on the stack that hide their content
        self._in_title = False
        self._keep_links = keep_links
        self._link: str | None = None
        self._link_text: list[str] = []

    @property
    def _skipping(self) -> bool:
        return self._skipped > 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: (value or "") for name, value in attrs}  # names arrive in lower case
        if tag == "title":
            self._in_title = True
        hidden = False
        if values:  # most tags carry no attributes: skip the checks
            style = values.get("style", "").replace(" ", "").lower()
            hidden = (
                "hidden" in values
                or values.get("aria-hidden", "").lower() == "true"
                or "display:none" in style
                or "visibility:hidden" in style
            )
        if tag in _VOID_TAGS:
            if tag in ("br", "hr") and not self._skipping:
                self.parts.append("\n")
            return
        if len(self._stack) < _MAX_HTML_DEPTH:  # deeper nesting is read flat
            skip = tag in _SKIPPED_TAGS or hidden
            self._stack.append((tag, skip))
            self._open[tag] += 1
            self._skipped += skip
        if self._skipping:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in ("td", "th"):
            self.parts.append(" | ")
        if tag == "a" and self._keep_links and values.get("href"):
            self._link, self._link_text = values["href"], []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr") and not self._skipping:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link is not None:
            self.links.append((" ".join("".join(self._link_text).split()), self._link))
            self._link = None
        if self._open[tag] > 0:
            while self._stack:
                name, skip = self._stack.pop()
                self._open[name] -= 1
                self._skipped -= skip
                if name == tag:
                    break
        if tag in _BLOCK_TAGS and not self._skipping:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skipping:
            return
        self.parts.append(data)
        if self._link is not None:
            self._link_text.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        compact: list[str] = []
        for line in lines:
            if line or (compact and compact[-1]):
                compact.append(line)
        return "\n".join(compact).strip()


def html_to_text(html: str, *, keep_links: bool = False) -> tuple[str, str, list[tuple[str, str]]]:
    """Return the title, the visible text and (optionally) the links of an HTML page."""

    parser = _TextExtractor(keep_links=keep_links)
    parser.parse(html)
    return " ".join(parser.title.split()), parser.text(), parser.links


class FetchUrlArgs(ToolArgs):
    url: str = Field(description="The http(s) URL of the page or PDF to read.")
    include_links: bool = Field(default=False, description="List the page's links at the end.")
    start: int = Field(
        default=0,
        ge=0,
        description="Character of the text to start at; to read on, pass the start a previous "
        "result names.",
    )
    max_chars: int = Field(
        default=12_000, ge=500, le=100_000, description="Most characters of text returned."
    )
    focus: str | None = Field(
        default=None,
        max_length=500,
        description="What you look for on the page: returns only the most relevant passages "
        "(about 3,000 characters, with their headings and positions) instead of the whole text.",
    )


_FOCUS_CHARS = 3_000
_FOCUS_PASSAGE = 1_200
_HEADING_CHARS = 100


def _passages(text: str) -> list[tuple[int, str, str]]:
    """`(start, heading, paragraph)` for each paragraph of `text`; a short line without a
    closing punctuation mark on its own is taken as the heading of the paragraphs after it."""

    found: list[tuple[int, str, str]] = []
    heading = ""
    for match in re.finditer(r"\S[^\n]*(?:\n[ \t]*\S[^\n]*)*", text):
        block = match.group().strip()
        if "\n" not in block and len(block) <= _HEADING_CHARS and block[-1] not in ".:;,!?":
            heading = block
            continue
        found.append((match.start(), heading, block))
    return found


def focus_passages(
    text: str, focus: str, *, offset: int = 0, budget: int = _FOCUS_CHARS
) -> str | None:
    """The passages of `text` most relevant to `focus` (BM25), in page order, or `None`."""

    from awa.toolkits.knowledge import KnowledgeBase

    passages = _passages(text)
    if not passages:
        return None
    base = KnowledgeBase(chunk_size=_FOCUS_PASSAGE, overlap=0)
    for index, (_, heading, block) in enumerate(passages):
        base.add_text(block, source=heading or " ", metadata={"index": index})
    hits = base.search(focus, k=len(passages))
    chosen: dict[int, str] = {}
    used = 0
    for hit in hits:
        if hit.score <= 0 or used >= budget:
            break
        index = int(hit.metadata["index"])
        piece = hit.text[: budget - used]
        chosen[index] = chosen.get(index, "") + piece
        used += len(piece)
    if not chosen:
        return None
    parts = []
    for index in sorted(chosen):
        start, heading, _ = passages[index]
        label = f"[at {offset + start}" + (f", under '{heading}'" if heading else "") + "]"
        parts.append(f"{label}\n{chosen[index]}")
    return "\n\n".join(parts)


_HTML_TYPES = ("text/html", "application/xhtml+xml")
_MAX_PDF_PAGES = 200
_SPARSE_TEXT = 200


def _window_end(text: str, start: int, size: int) -> int:
    """End of the window of `size` characters from `start`, moved back to a word boundary."""

    end = start + size
    if end >= len(text):
        return len(text)
    cut = max(text.rfind(" ", start + size // 2, end), text.rfind("\n", start + size // 2, end))
    return cut + 1 if cut >= 0 else end


class _FetchUrl:
    def __init__(self, policy: HttpPolicy, max_output_chars: int) -> None:
        self.policy = policy
        self.text_limit = max(500, max_output_chars - 1_000)  # room for the header and notes

    def _text_limit(self) -> int:
        """The window size: within the tool's own limit and the tool profile's limit, so a
        long page is read in parts (`start`) rather than offloaded."""

        from awa.tools.offload import current_profile

        return max(500, min(self.text_limit, current_profile().max_output_chars - 1_000))

    def _get(self, url: str) -> HttpResponse:
        response = fetch(
            self.policy,
            url,
            headers={
                "Accept": "text/html, text/plain;q=0.9, application/json;q=0.8, "
                "application/pdf;q=0.7"
            },
        )
        if response.status >= 400:
            raise ToolException(f"the server answered HTTP {response.status}")
        return response

    def __call__(self, args: FetchUrlArgs) -> str:
        response = self._get(args.url)
        title, text, links = self._content(response, args.include_links)
        total = len(text)
        if args.start and args.start >= total:
            raise ToolException(f"start {args.start} is past the end of the text ({total} chars)")
        header = [f"Title: {title}"] if title else []
        header.append(f"URL: {response.url}")
        if args.focus:
            focused = focus_passages(text[args.start :], args.focus, offset=args.start)
            if focused is not None:
                header.append(
                    f"Passages most relevant to {args.focus!r} ({total} characters in all; "
                    "call fetch_url with start=<at> and no focus to read on from one)"
                )
                return "\n".join([*header, "", focused])
            header.append(f"Nothing on the page matches {args.focus!r}; the text follows.")
        end = _window_end(text, args.start, min(args.max_chars, self._text_limit()))
        if args.start or end < total:
            header.append(f"Characters {args.start}-{end} of {total}")
        body = "\n".join([*header, "", text[args.start : end]])
        if end < total:
            body += f"\n… [more text: call fetch_url again with start={end}]"
        if response.truncated:
            body += f"\n… [page larger than {self.policy.max_response_bytes} bytes; rest not read]"
        sparse = response.content_type in _HTML_TYPES and len(text.strip()) < _SPARSE_TEXT
        if sparse and b"<script" in response.body.lower():
            body += (
                "\n[The page has almost no text: it probably needs JavaScript to show its "
                "content. Try another source.]"
            )
        if links:
            base = response.url
            listed = [
                f"- {label or '(no text)'}: {urllib.parse.urljoin(base, href)}"
                for label, href in links[:100]
            ]
            body += "\n\nLinks:\n" + "\n".join(listed)
        return body

    def _content(
        self, response: HttpResponse, keep_links: bool, *, page_marks: bool = True
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Title, text and links of a response: HTML, text, JSON, XML or PDF.

        `page_marks` labels the pages of a PDF (`--- page 1 ---`); without them they are
        joined by a line break only.
        """

        kind = response.content_type
        if kind in ("application/pdf", "application/x-pdf") or response.body.startswith(b"%PDF-"):
            if response.truncated:
                raise ToolException(
                    f"the PDF is larger than {self.policy.max_response_bytes} bytes "
                    "(max_response_bytes)"
                )
            try:
                pages = pdf_text(response.body, max_pages=_MAX_PDF_PAGES)
            except (ToolException, OptionalDependencyError):
                raise
            except Exception as exc:
                raise ToolException(f"the PDF could not be read: {exc}") from None
            marked = (f"--- page {n} ---\n{page}" for n, page in enumerate(pages, 1))
            text = "\n\n".join(marked) if page_marks else "\n".join(pages)
            return "", text or "The PDF has no readable text.", []
        if kind in _HTML_TYPES:
            return html_to_text(response.text(), keep_links=keep_links)
        if kind.startswith("text/") or kind.endswith(("json", "xml")):
            return "", response.text(), []
        raise ToolException(f"cannot read content of type {kind!r} as text")


class VerifyQuoteArgs(ToolArgs):
    url: str = Field(description="The http(s) URL of the page or PDF the quote is from.")
    quote: str = Field(
        min_length=1,
        description="The quote exactly as the page words it, from one sentence or line (at most "
        "25 words). Pieces from different sentences, lines or table rows are joined with an "
        "ellipsis (...) in page order, each piece of at least two words.",
    )


_QUOTE_MIN_WORDS = 3
_QUOTE_MAX_WORDS = 25
_PIECE_MIN_WORDS = 2
_ELLIPSIS = re.compile(r"\.{3,}|\N{HORIZONTAL ELLIPSIS}")
# Markers a page puts inside a sentence ([12], [a], [note 3], [citation needed]); other
# bracketed words, such as an editor's "[not]", are part of the text.
_FOOTNOTE = re.compile(r"\[(?:\d{1,3}|[a-z]|(?:note|nb) \d{1,3}|citation needed)\]")
_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_SYMBOLS = "%$\u20ac\u00a3\u00a5<>=\u2264\u2265\u00b1+&#"
_BREAK = "\u00b6"  # between two sentences, lines or paragraphs
_TOKEN = re.compile(
    r"(?P<brk>\n[ \t]*\n|[.!?;](?=\s|$)|[\u3002\uff01\uff1f\uff1b])"
    rf"|(?P<cjk>[{_CJK}])"
    rf"|(?P<word>(?:(?<![^\W_])[-+\u2212](?=\d))?(?:(?![{_CJK}])[^\W_])+"
    rf"(?:(?<=\d)[.,](?=\d)(?:(?![{_CJK}])[^\W_])+)*)"
    rf"|(?P<symbol>[{re.escape(_SYMBOLS)}])"
)
_FUNCTION_WORDS = frozenset(
    "a an and as at by for he in is it of on or she that the they this to was".split()  # noqa: SIM905
)


def _tokens(text: str, *, lines: bool) -> list[str]:
    """The lower-case words of `text`, with a break token between sentences (lines with `lines`).

    Digits keep sign and decimal separator, symbols such as `%` count as words, and each Chinese or
    Japanese character is a word of its own.
    """

    text = _FOOTNOTE.sub(" ", text.lower())
    if lines:
        text = text.replace("\n", "\n\n")
    tokens: list[str] = []
    for match in _TOKEN.finditer(text):
        token = _BREAK if match["brk"] else match[0].replace("−", "-")
        if token != _BREAK or (tokens and tokens[-1] != _BREAK):
            tokens.append(token)
    return tokens


def _weight(tokens: Sequence[str]) -> float:
    """How many words `tokens` count as: a character of Chinese or Japanese counts half."""

    return sum(
        0.5
        if re.fullmatch(f"[{_CJK}]", token)
        else float(token not in _SYMBOLS and token != _BREAK)
        for token in tokens
    )


def _trim(tokens: list[str]) -> list[str]:
    """`tokens` without a break at either end."""

    while tokens and tokens[0] == _BREAK:
        tokens = tokens[1:]
    while tokens and tokens[-1] == _BREAK:
        tokens = tokens[:-1]
    return tokens


def _quote_problem(pieces: list[list[str]]) -> str | None:
    """Why the pieces of a quote cannot be checked, or `None`."""

    total = sum(_weight(piece) for piece in pieces)
    if total < _QUOTE_MIN_WORDS:
        return (
            f"the quote has fewer than {_QUOTE_MIN_WORDS} words. Quote the sentence of the "
            "page that states the claim."
        )
    if total > _QUOTE_MAX_WORDS:
        return (
            f"the quote has more than {_QUOTE_MAX_WORDS} words. Quote the part of the sentence "
            "that states the claim."
        )
    for index, piece in enumerate(pieces):
        single_name = index == 0 and len(pieces) > 1 and piece[0] not in _FUNCTION_WORDS
        if _weight(piece) < _PIECE_MIN_WORDS and not (single_name and _weight(piece) >= 1):
            return (
                f"a piece of the quote between ellipses has fewer than {_PIECE_MIN_WORDS} words "
                "(only the first may be a single name). Quote a piece of the page's own sentence "
                "instead of single words."
            )
    return None


class _VerifyQuote(_FetchUrl):
    """Checks that the words of a quote occur on a page, in one sentence or line each.

    That the words are there is all it proves: whether they support a claim is for the caller
    to judge. Its answers contain no text of the page or of the server.
    """

    def __call__(self, args: VerifyQuoteArgs) -> str:  # type: ignore[override]  # narrower than the base's
        pieces = [_trim(_tokens(part, lines=False)) for part in _ELLIPSIS.split(args.quote)]
        pieces = [piece for piece in pieces if piece]
        if problem := (_quote_problem(pieces) if pieces else "the quote has no words."):
            return f"NOT VERIFIED: {problem}"
        response = self._get(args.url)
        is_html = response.content_type in _HTML_TYPES
        text = self._content(response, False, page_marks=False)[1]
        page = f" {' '.join(_tokens(text, lines=is_html))} "
        position = 0
        for piece in pieces:
            found = page.find(f" {' '.join(piece)} ", position)
            if found < 0:
                rest = (
                    " The page is larger than the limit; only its first part was read."
                    if response.truncated
                    else ""
                )
                return (
                    f"NOT FOUND: the quote does not occur on {args.url}.{rest} Do not cite it: "
                    "quote the page's own words from one sentence or line (pieces from "
                    "different sentences, lines or table rows must be joined with an ellipsis, "
                    "..., in page order), or drop the claim."
                )
            position = found + len(" ".join(piece)) + 1
        return f"VERIFIED: the quote occurs on {args.url}."


class WebSearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=500, description="What to search for.")
    max_results: int = Field(default=5, ge=1, le=20, description="Most hits returned.")


class _WebSearch:
    def __init__(self, provider: SearchProvider) -> None:
        self.provider = provider

    def __call__(self, args: WebSearchArgs) -> str:
        found = fixture_search(self.provider, args.query, max_results=args.max_results)
        results = unique_results(found)
        if not results:
            return f"No results for {args.query!r}."
        return "\n\n".join(
            f"{index}. {hit.title}\n   {hit.url}\n   {hit.snippet}".rstrip()
            for index, hit in enumerate(results[: args.max_results], start=1)
        )


def web_toolkit(
    *,
    allowed_hosts: Sequence[str] | None = None,
    search: SearchProvider | Mapping[str, Any] | None = None,
    include: Sequence[str] | None = None,
    timeout: float = 20.0,
    max_response_bytes: int = 3_000_000,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    allow_private_networks: bool = False,
    allow_http: bool = False,
    use_env_proxies: bool = False,
    frame_output: bool = True,
) -> list[BaseTool]:
    """Build `fetch_url` (read a web page as text) and `web_search` (search the web).

    Args:
        allowed_hosts: Hosts `fetch_url` may read (`*.example.com` for subdomains); `None`
            allows any public host.
        search: A `SearchProvider`, or the keyword arguments of a `JSONSearchProvider` (with
            `url_template`) or of the built-in `DuckDuckGoSearchProvider` (without); `None` uses
            the built-in keyless search.
        include: Build only these tools (names from `WEB_TOOLS`); `verify_quote` is built only
            when named.
        timeout: Seconds to connect and for each read; a whole request is cut off after three
            times this.
        allow_private_networks: Allow pages on private or loopback addresses.
        use_env_proxies: Route requests through the proxies of `HTTP_PROXY` and `HTTPS_PROXY`.
        frame_output: Label page text as external data, since pages can contain injected
            instructions.

    Raises:
        ConfigurationError: If an option is invalid.

    Example:
        ```python
        fetch_url, web_search = web_toolkit()
        [docs_reader] = web_toolkit(allowed_hosts=["docs.example.com"], include=["fetch_url"])
        ```
    """

    positive(timeout, "timeout")
    positive(max_response_bytes, "max_response_bytes")
    provider = search_provider(search, timeout=timeout, use_env_proxies=use_env_proxies)
    names = selected(WEB_TOOLS, include, "web")
    if include is None:
        names = [name for name in names if name not in _OPT_IN]
    hosts = normalize_hosts(allowed_hosts)
    policy = HttpPolicy(
        allowed_hosts=hosts,
        allow_private_networks=allow_private_networks,
        allow_http=allow_http,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        use_env_proxies=use_env_proxies,
    )
    tools: list[BaseTool] = []
    if "fetch_url" in names:
        where = "any public web site" if hosts is None else ", ".join(hosts)
        tools.append(
            build_tool(
                _FetchUrl(policy, max_output_chars),
                name="fetch_url",
                description=(
                    f"Read a web page or PDF ({where}) and return its title and visible text, "
                    "without scripts, styles or hidden elements. Use it to read a page whose "
                    "URL you know; set focus to get only the passages about what you look for, "
                    "include_links to see where the page links to. Long texts come in parts: "
                    "the result names the start to read on from."
                ),
                args_schema=FetchUrlArgs,
                toolkit="web",
                # Reading any URL can also send data out, in the URL itself.
                risk=ToolRisk(
                    untrusted_input=True, private_data=False, external_action=hosts is None
                ),
                frame=frame_output,
                max_output_chars=max_output_chars,
                source="web",
            )
        )
    if "web_search" in names:
        tools.append(
            build_tool(
                _WebSearch(provider),
                name="web_search",
                description=(
                    "Search the web and return the top hits (title, URL, snippet). Use it to "
                    "find pages about a topic or current information; read a hit with fetch_url."
                ),
                args_schema=WebSearchArgs,
                toolkit="web",
                # The query goes only to the search service, never to a host the model picks.
                risk=ToolRisk(untrusted_input=True, private_data=False, external_action=False),
                frame=frame_output,
                max_output_chars=max_output_chars,
                source="web_search",
            )
        )
    if "verify_quote" in names:
        tools.append(
            build_tool(
                _VerifyQuote(policy, max_output_chars),
                name="verify_quote",
                description=(
                    "Check that a quote really occurs on a web page: reads the page and "
                    "answers VERIFIED or NOT FOUND (case, punctuation and footnote markers "
                    "do not matter; the words must stand in one sentence or line, in order). "
                    "It checks the words, not whether they prove your claim. Use it before "
                    "citing a quote as evidence."
                ),
                args_schema=VerifyQuoteArgs,
                toolkit="web",
                # Like fetch_url, the URL itself can send data out and a page is read; the
                # result is fixed text that repeats only the URL the model gave.
                risk=ToolRisk(
                    untrusted_input=True, private_data=False, external_action=hosts is None
                ),
                max_output_chars=max_output_chars,
                source="web",
            )
        )
    return tools


__all__ = [
    "WEB_TOOLS",
    "DuckDuckGoSearchProvider",
    "JSONSearchProvider",
    "SearchBlockedError",
    "SearchError",
    "SearchFormatError",
    "SearchProvider",
    "SearchResult",
    "html_to_text",
    "web_toolkit",
]
