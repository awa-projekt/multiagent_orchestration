"""Web search for `web_search`: the provider interface, a JSON endpoint and the keyless default.

Inside `with search_fixture(path):` (or `awa eval --search-fixture FILE`) every search is answered
from a recorded JSON file, so evaluations see the same hits each run.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from awa._internal.html import HtmlEvents
from awa._internal.http import HttpPolicy, HttpResponse, fetch
from awa._internal.upstream.tools import ToolException
from awa._internal.version import __version__
from awa.errors import ConfigurationError
from awa.toolkits._common import dig, header_values, positive


@dataclass(frozen=True)
class SearchResult:
    """One web search hit: title, URL and a short excerpt of the page."""

    title: str
    url: str
    snippet: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    """A web search backend for `web_search`.

    Implement `search` to plug in any search API; `JSONSearchProvider` covers
    endpoints that answer a GET request with JSON. Raise `SearchError` (or a
    subclass) with a message the model can act on when the search fails.

    Example:
        ```python
        class IntranetSearch:
            def search(self, query: str, *, max_results: int) -> list[SearchResult]:
                hits = intranet_client.find(query, limit=max_results)
                return [SearchResult(hit.title, hit.link, hit.summary) for hit in hits]
        ```
    """

    def search(self, query: str, *, max_results: int) -> Sequence[SearchResult]:
        """Return up to `max_results` hits for `query`."""
        ...


class SearchError(ToolException):
    """A web search failed; the message says why and what to do.

    `web_search` returns it to the model as an error result, so the run goes on.
    """


class SearchBlockedError(SearchError):
    """The search service refused the request (a rate limit or a check for automated use)."""


class SearchFormatError(SearchError):
    """The search service answered with something that cannot be read (its format changed)."""


_CONFIGURE = "configure a search API (toolkits.web.search, or WEB_SEARCH_URL for the web connector)"


def unique_results(results: Iterable[SearchResult]) -> list[SearchResult]:
    """`results` without repeats of a page (same host without `www.`, path, query), in order."""

    seen: set[str] = set()
    kept: list[SearchResult] = []
    for result in results:
        parts = urllib.parse.urlsplit(result.url.strip())
        host = (parts.hostname or "").removeprefix("www.")
        key = (
            f"{host}{parts.path.rstrip('/')}?{parts.query}" if host else result.url or result.title
        )
        if key not in seen:
            seen.add(key)
            kept.append(result)
    return kept


class JSONSearchProvider:
    """A `SearchProvider` for a search endpoint that returns JSON.

    Args:
        url_template: The request URL with `{query}` (URL-encoded) and optionally
            `{max_results}`.
        results_path: Dotted path to the list of hits (`""` when the answer is the list).
        title_field: Field of a hit holding its title; dotted paths work for all three fields.
        headers: Fixed, non-secret request headers.
        headers_env: Secret headers: header name -> environment variable name.

    Raises:
        ConfigurationError: If the template is not an http(s) URL with `{query}`.

    Example:
        ```python
        provider = JSONSearchProvider(
            "https://search.example.com/v1?q={query}", results_path="items",
            url_field="link", headers_env={"X-Api-Key": "SEARCH_KEY"},
        )
        ```
    """

    def __init__(
        self,
        url_template: str,
        *,
        results_path: str = "results",
        title_field: str = "title",
        url_field: str = "url",
        snippet_field: str = "snippet",
        headers: Mapping[str, str] | None = None,
        headers_env: Mapping[str, str] | None = None,
        timeout: float = 15.0,
        allow_private_networks: bool = False,
    ) -> None:
        if "{query}" not in url_template:
            raise ConfigurationError("url_template must contain '{query}'")
        parts = urllib.parse.urlsplit(
            url_template.replace("{query}", "q").replace("{max_results}", "1")
        )
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigurationError(f"url_template {url_template!r} is not an http(s) URL")
        positive(timeout, "timeout")
        self.url_template = url_template
        self.results_path = results_path
        self.fields = (title_field, url_field, snippet_field)
        self.headers = dict(headers or {})
        self.headers_env = dict(headers_env or {})
        self.policy = HttpPolicy(
            allowed_hosts=(parts.hostname.lower(),),
            allow_private_networks=allow_private_networks,
            timeout=timeout,
        )

    def __repr__(self) -> str:
        return f"JSONSearchProvider({self.url_template!r})"

    def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        """Query the endpoint and map its hits to `SearchResult`s.

        Raises:
            SearchBlockedError: If the endpoint answers HTTP 429 (rate limit).
            SearchFormatError: If the answer is not JSON or has no list of hits.
        """

        url = self.url_template.replace("{query}", urllib.parse.quote(query, safe="")).replace(
            "{max_results}", str(max_results)
        )
        headers, secret = header_values(self.headers, self.headers_env)
        headers.setdefault("Accept", "application/json")
        response = fetch(self.policy, url, headers=headers, secret_headers=secret)
        if response.status == 429:
            raise SearchBlockedError("the search service is rate limiting (HTTP 429); try later")
        if response.status >= 400:
            raise SearchError(f"the search service answered HTTP {response.status}")
        try:
            data = response.json()
        except ValueError:
            raise SearchFormatError("the search service did not answer with JSON") from None
        hits = dig(data, self.results_path)
        if not isinstance(hits, list):
            raise SearchFormatError(f"the search answer has no list at {self.results_path!r}")
        title, link, snippet = self.fields
        return [
            SearchResult(
                title=str(dig(hit, title) or ""),
                url=str(dig(hit, link) or ""),
                snippet=str(dig(hit, snippet) or ""),
            )
            for hit in hits[:max_results]
            if isinstance(hit, dict)
        ]


_ENDPOINTS = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")
_SAFE_SEARCH: dict[str, str] = {"strict": "1", "moderate": "-1", "off": "-2"}
_REGION = re.compile(r"[a-z]{2}-[a-z]{2}")
_BLOCK_STATUSES = frozenset({202, 403, 418, 429})
_RETRY_STATUSES = frozenset({500, 502, 503, 504})
_HEADERS = {"Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"}
_MAX_PAGES = 3
_MAX_PAGE_BYTES = 1_500_000
_PAUSE = 60.0
_BACKOFF = 0.5
_TITLE_CLASSES = frozenset({"result__a", "result-link"})
_SNIPPET_CLASSES = frozenset({"result__snippet", "result-snippet"})

_clock = time.monotonic
_sleep = time.sleep


class _Pacer:
    """Spaces this process's requests to the keyless service; pauses it after a refusal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0
        self.paused_until = 0.0

    def wait(self, interval: float) -> None:
        with self._lock:
            now = _clock()
            start = max(now, self._next)
            self._next = start + interval
        if start > now:
            _sleep(start - now)


_PACER = _Pacer()
_MIN_INTERVAL = 1.0
"""Fewest seconds between two requests of this process."""


def _blocked(seconds: float) -> SearchBlockedError:
    return SearchBlockedError(
        "DuckDuckGo is refusing automated searches from this network (rate limit or bot "
        f"check), so web search is paused for {seconds:.0f} seconds. Continue with other "
        "sources, and say in your answer that the web search was unavailable, so that missing "
        f"results are not taken for no news. For dependable search, {_CONFIGURE}."
    )


def _changed(detail: str) -> SearchFormatError:
    return SearchFormatError(
        f"{detail}; DuckDuckGo may have changed its result page. Update awa-agents, or {_CONFIGURE}."
    )


def _is_duckduckgo(host: str | None) -> bool:
    host = (host or "").lower()
    return host == "duckduckgo.com" or host.endswith(".duckduckgo.com")


def _target(href: str) -> str | None:
    """Where a result link leads: redirect links unwrapped; ads and DuckDuckGo's own pages dropped."""

    parts = urllib.parse.urlsplit(urllib.parse.urljoin(_ENDPOINTS[0], href.strip()))
    if _is_duckduckgo(parts.hostname):
        if parts.path != "/l/":
            return None  # ads (/y.js) and DuckDuckGo's own pages
        target = urllib.parse.parse_qs(parts.query).get("uddg", [""])[0]
        parts = urllib.parse.urlsplit(target)
        if _is_duckduckgo(parts.hostname):
            return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return urllib.parse.urlunsplit(parts)


class _ResultPage(HtmlEvents):
    """Hits, next-page form and status markers of a DuckDuckGo result page (both layouts)."""

    def __init__(self) -> None:
        self.hits: list[list[str]] = []  # [href, title, snippet]
        self.blocked = False
        self.empty = False
        self.next_form: dict[str, str] = {}
        self._form: dict[str, str] | None = None
        self._field: int | None = None  # 1: title, 2: snippet of the last hit
        self._tag = ""
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = set(values.get("class", "").split())
        if "no-results" in classes:
            self.empty = True
        if "anomaly" in values.get("class", "") + values.get("action", "") or (
            values.get("id") == "challenge-form"
        ):
            self.blocked = True
        if tag == "form":
            self._form = {}
        elif tag == "input" and self._form is not None and values.get("type") == "hidden":
            self._form[values.get("name", "")] = values.get("value", "")
        if self._field is not None:
            if tag == self._tag:
                self._depth += 1
        elif tag == "a" and classes & _TITLE_CLASSES:
            self.hits.append([values.get("href", ""), "", ""])
            self._field, self._tag, self._depth = 1, tag, 1
        elif classes & _SNIPPET_CLASSES and self.hits:
            self._field, self._tag, self._depth = 2, tag, 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._form is not None:
            form, self._form = self._form, None
            form.pop("", None)
            offset = form.get("s", "")
            if (
                form.get("q")
                and offset.isdecimal()
                and int(offset) > int(self.next_form.get("s", -1))
            ):
                self.next_form = form
        if self._field is not None and tag == self._tag:
            self._depth -= 1
            if self._depth == 0:
                self._field = None

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self.hits[-1][self._field] += data

    def results(self) -> list[SearchResult]:
        found = []
        for href, title, snippet in self.hits:
            url, name = _target(href), " ".join(title.split())
            if url is not None and name:
                found.append(SearchResult(name, url, " ".join(snippet.split())))
        return found


class DuckDuckGoSearchProvider:
    """The built-in keyless `SearchProvider`: DuckDuckGo's result pages, no API key needed.

    `web_toolkit` uses it when no other provider is configured. It reads the result pages for
    browsers without JavaScript (`html.duckduckgo.com`, then `lite.duckduckgo.com`), sends at
    most one request per second per process, retries network and server errors, skips ads and
    duplicates, and pages up to `max_results`. After a refusal (`SearchBlockedError`) it sends
    nothing for a minute. The service is free, without guarantees, and meant for moderate use;
    for high volumes use a search API (`JSONSearchProvider`).

    Args:
        region: Region code such as `"de-de"` or `"us-en"`; `"wt-wt"` means no region.
        safe_search: `"strict"`, `"moderate"` or `"off"`.
        retries: Further attempts after a network failure or server error.
        use_env_proxies: Use the proxies configured in the environment (`HTTPS_PROXY`).

    Raises:
        ConfigurationError: If an option is invalid.
    """

    def __init__(
        self,
        *,
        region: str = "wt-wt",
        safe_search: Literal["strict", "moderate", "off"] = "moderate",
        timeout: float = 15.0,
        retries: int = 1,
        use_env_proxies: bool = False,
    ) -> None:
        if not _REGION.fullmatch(region):
            raise ConfigurationError(f"region {region!r} is not a code like 'de-de' or 'wt-wt'")
        if safe_search not in _SAFE_SEARCH:
            raise ConfigurationError(f"safe_search must be one of {', '.join(_SAFE_SEARCH)}")
        positive(timeout, "timeout")
        if retries < 0:
            raise ConfigurationError("retries must not be negative")
        self.region = region
        self.safe_search = safe_search
        self.retries = retries
        self.policy = HttpPolicy(
            allowed_hosts=tuple(str(urllib.parse.urlsplit(url).hostname) for url in _ENDPOINTS),
            timeout=timeout,
            max_response_bytes=_MAX_PAGE_BYTES,
            user_agent=f"awa-agents/{__version__} (web_search)",
            use_env_proxies=use_env_proxies,
        )

    def __repr__(self) -> str:
        return f"DuckDuckGoSearchProvider(region={self.region!r})"

    def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        """Return up to `max_results` hits for `query`.

        Raises:
            SearchBlockedError: DuckDuckGo refuses automated searches right now.
            SearchFormatError: Its result pages could not be read.
            SearchError: It could not be reached or answered a server error.
        """

        paused = _PACER.paused_until - _clock()
        if paused > 0:
            raise _blocked(paused)
        failures: list[SearchError] = []
        for endpoint in _ENDPOINTS:
            try:
                return self._search(endpoint, query, max_results)
            except (SearchBlockedError, SearchFormatError) as exc:
                failures.append(exc)
        if any(isinstance(failure, SearchBlockedError) for failure in failures):
            _PACER.paused_until = _clock() + _PAUSE
            raise _blocked(_PAUSE)
        raise failures[0]

    def _search(self, endpoint: str, query: str, max_results: int) -> list[SearchResult]:
        form = {"q": query, "kl": self.region, "kp": _SAFE_SEARCH[self.safe_search]}
        results: list[SearchResult] = []
        for _ in range(_MAX_PAGES):
            try:
                page = self._page(endpoint, form)
            except SearchError:
                if results:
                    break  # keep the hits of the pages already read
                raise
            results = unique_results([*results, *page.results()])
            following = page.next_form
            if len(results) >= max_results or int(following.get("s", "0")) <= int(
                form.get("s", "0")
            ):
                break
            form = following
        return results[:max_results]

    def _page(self, endpoint: str, form: Mapping[str, str]) -> _ResultPage:
        response = self._post(endpoint, form)
        page = _ResultPage()
        page.parse(response.text())
        if response.status in _BLOCK_STATUSES or page.blocked:
            raise _blocked(_PAUSE)
        if response.status >= 500:
            raise SearchError(f"DuckDuckGo answered HTTP {response.status}; try again shortly")
        if response.status >= 400:
            raise _changed(f"DuckDuckGo answered HTTP {response.status}")
        if not page.hits and not page.empty:
            raise _changed("the DuckDuckGo result page could not be read")
        return page

    def _post(self, url: str, form: Mapping[str, str]) -> HttpResponse:
        data = urllib.parse.urlencode(form).encode("ascii")
        attempt = 0
        while True:
            _PACER.wait(_MIN_INTERVAL)
            try:
                response = fetch(self.policy, url, method="POST", headers=_HEADERS, data=data)
            except ToolException as exc:
                if attempt >= self.retries:
                    raise SearchError(
                        f"could not reach DuckDuckGo: {exc}. Check the internet connection "
                        "(behind a proxy, enable use_env_proxies)"
                    ) from None
            else:
                if response.status not in _RETRY_STATUSES or attempt >= self.retries:
                    return response
            _sleep(_BACKOFF * 2**attempt)
            attempt += 1


def search_provider(
    search: SearchProvider | Mapping[str, object] | None,
    *,
    timeout: float = 15.0,
    use_env_proxies: bool = False,
) -> SearchProvider:
    """The provider `web_search` uses: `search` itself, or one built from its options.

    A mapping with `url_template` configures a `JSONSearchProvider`; any other
    mapping, and `None`, the built-in `DuckDuckGoSearchProvider` (which takes
    `timeout` and `use_env_proxies` unless the mapping sets them).

    Raises:
        ConfigurationError: If the options are invalid.
    """

    if search is not None and not isinstance(search, Mapping):
        return search
    options = dict(search or {})
    template = options.pop("url_template", None)
    try:
        if template is None:
            options = {"timeout": timeout, "use_env_proxies": use_env_proxies, **options}
            return DuckDuckGoSearchProvider(**options)  # type: ignore[arg-type]  # options are user data
        if not isinstance(template, str):
            raise ConfigurationError("search.url_template must be a URL with {query}")
        return JSONSearchProvider(template, **options)  # type: ignore[arg-type]  # options are user data
    except TypeError as exc:
        raise ConfigurationError(f"invalid search option: {exc}") from None


class SearchFixture:
    """Recorded search results, keyed by the normalized query and the number of hits asked for.

    Args:
        path: The JSON file (`{"<query> | <max_results>": [{title, url, snippet}, ...]}`).
        record: Search live for queries the file lacks and keep their results.

    Raises:
        ConfigurationError: If the file exists but is not a fixture.
    """

    def __init__(self, path: str | Path, *, record: bool = False) -> None:
        self.path = Path(path)
        self.record = record
        self.hits = self.misses = 0
        self._lock = threading.Lock()
        self._changed = False
        self._entries: dict[str, list[dict[str, str]]] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigurationError(
                    f"cannot read the search fixture {self.path}: {exc}"
                ) from None
            if not isinstance(data, dict):
                raise ConfigurationError(f"the search fixture {self.path} is not a JSON object")
            self._entries = data
        elif not record:
            raise ConfigurationError(
                f"the search fixture {self.path} does not exist; record it first "
                "(awa eval --search-fixture FILE --record-search)"
            )

    @staticmethod
    def key(query: str, max_results: int) -> str:
        """The entry of a query: lower case, single spaces, and the number of hits."""

        return f"{' '.join(query.split()).casefold()} | {max_results}"

    def search(
        self, provider: SearchProvider, query: str, *, max_results: int
    ) -> Sequence[SearchResult]:
        """The recorded hits for `query`; a query not recorded is searched live when recording.

        Raises:
            SearchError: If the query is not recorded and the fixture does not record.
        """

        key = self.key(query, max_results)
        with self._lock:
            found = self._entries.get(key)
        if found is not None:
            self.hits += 1
            return [SearchResult(**hit) for hit in found]
        self.misses += 1
        if not self.record:
            raise SearchError(
                f"{query!r} is not in the recorded search results ({self.path.name}); "
                "search with a query the evaluation recorded, or record it again"
            )
        results = list(provider.search(query, max_results=max_results))
        with self._lock:
            self._entries[key] = [asdict(hit) for hit in results]
            self._changed = True
        return results

    def save(self) -> None:
        """Write the recorded results when something was added (atomically)."""

        if not self._changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        draft = self.path.with_suffix(self.path.suffix + ".tmp")
        text = json.dumps(self._entries, ensure_ascii=False, indent=1, sort_keys=True)
        draft.write_text(text + "\n", encoding="utf-8")
        os.replace(draft, self.path)
        self._changed = False


_active: contextvars.ContextVar[SearchFixture | None] = contextvars.ContextVar(
    "awa_search_fixture", default=None
)


@contextlib.contextmanager
def search_fixture(path: str | Path, *, record: bool = False) -> Iterator[SearchFixture]:
    """Answer every web search inside the block from the fixture at `path` (see the module)."""

    fixture = SearchFixture(path, record=record)
    token = _active.set(fixture)
    try:
        yield fixture
    finally:
        _active.reset(token)
        fixture.save()


def fixture_search(
    provider: SearchProvider, query: str, *, max_results: int
) -> Sequence[SearchResult]:
    """`provider.search(...)`, or the active fixture's answer inside `search_fixture()`."""

    fixture = _active.get()
    if fixture is None:
        return provider.search(query, max_results=max_results)
    return fixture.search(provider, query, max_results=max_results)
