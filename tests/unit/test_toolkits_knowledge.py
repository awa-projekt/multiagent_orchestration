"""`awa.toolkits.knowledge` and `web_search` without an API key."""

from __future__ import annotations

import socket
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from awa import Agent
from awa._internal.http import HttpPolicy, HttpResponse, host_allowed
from awa._internal.upstream.tools import ToolException
from awa.errors import ConfigurationError
from awa.testing import FakeChatModel, tool_call
from awa.toolkits import (
    DuckDuckGoSearchProvider,
    JSONSearchProvider,
    KnowledgeBase,
    SearchBlockedError,
    SearchError,
    SearchFormatError,
    SearchResult,
    _search,
    catalog,
    knowledge_toolkit,
    web_toolkit,
)
from awa.toolkits.knowledge import tokenize
from awa.tools.risk import declared_tool_risk


@pytest.fixture
def handbook(tmp_path: Path) -> Path:
    root = tmp_path / "handbook"
    (root / "hr").mkdir(parents=True)
    (root / "travel.md").write_text(
        "# Travel\n\nBusiness trips abroad need approval by the team lead.\n\n"
        "Hotel costs are refunded up to 150 EUR per night.",
        encoding="utf-8",
    )
    (root / "hr" / "vacation.txt").write_text(
        "Employees get 30 vacation days per year. Unused days expire in March.", encoding="utf-8"
    )
    (root / "prices.csv").write_text("product,price\nWidget,2.50\nGadget,10.00\n", encoding="utf-8")
    (root / "page.html").write_text(
        "<html><title>Security</title><body><p>Passwords rotate every 90 days.</p>"
        "<script>steal()</script></body></html>",
        encoding="utf-8",
    )
    (root / ".secret.md").write_text("hidden vacation policy", encoding="utf-8")
    return root


def test_tokenize_drops_stop_words_and_plurals() -> None:
    assert tokenize("The hotels of the Company") == ["hotel", "company"]
    assert tokenize("class glass") == ["class", "glass"]


def test_search_ranks_the_relevant_passage_first(handbook: Path) -> None:
    kb = KnowledgeBase.from_directory(handbook)
    assert sorted(kb.sources) == ["hr/vacation.txt", "page.html", "prices.csv", "travel.md"]
    [top, *_] = kb.search("how many vacation days do I get")
    assert top.source == "hr/vacation.txt" and "30 vacation days" in top.text
    assert kb.search("hotel refund per night")[0].source == "travel.md"
    assert kb.search("gadget price")[0].text.startswith("product: Widget")
    security = kb.search("password rotation")
    assert security[0].source == "page.html" and "steal" not in security[0].text
    assert kb.search("zebra") == []
    assert kb.search("vacation", sources=["travel.md"]) == []
    assert len(kb.search("the", k=3)) == 0


def test_windows_1252_files_and_semicolon_csv_are_read_as_written(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_bytes("Die Größe der Büros ist geregelt.".encode("cp1252"))
    (tmp_path / "staff.csv").write_bytes("Name;Raum\nMüller;Büro 4\n".encode("cp1252"))

    kb = KnowledgeBase.from_directory(tmp_path)

    assert "Größe der Büros" in kb.search("Größe Büros")[0].text
    assert kb.search("Müller")[0].text == "Name: Müller; Raum: Büro 4"


def test_add_text_and_chunking() -> None:
    kb = KnowledgeBase(chunk_size=100, overlap=10)
    added = kb.add_text("Refunds. " * 40, source="policy", metadata={"owner": "legal"})
    assert added > 1 and len(kb) == added
    [hit] = kb.search("refund", k=1)
    assert hit.metadata["owner"] == "legal" and hit.chunk >= 0
    with pytest.raises(ConfigurationError):
        KnowledgeBase(chunk_size=10)
    with pytest.raises(ConfigurationError, match="not a directory"):
        kb.load_directory("does/not/exist")


def test_an_embedding_function_adds_semantic_matches() -> None:
    def embed(texts: Sequence[str]) -> list[list[float]]:
        # A toy embedding: "holiday" and "vacation" mean the same thing.
        return [
            [float(any(w in t.lower() for w in ("holiday", "vacation"))), float("car" in t.lower())]
            for t in texts
        ]

    kb = KnowledgeBase(embed=embed)
    kb.add_text("Staff receive 30 vacation days.", source="leave.md")
    kb.add_text("Company cars are available.", source="fleet.md")
    hits = kb.search("holiday allowance", k=1)
    assert hits[0].source == "leave.md"


def test_pdf_files_are_skipped_without_the_extra(
    handbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.toolkits.knowledge as knowledge

    (handbook / "scan.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(knowledge, "extra_installed", lambda module: False)
    kb = KnowledgeBase.from_directory(handbook)
    assert "scan.pdf" not in kb.sources


def test_the_search_tool(handbook: Path) -> None:
    [tool] = knowledge_toolkit(handbook, description="Search the staff handbook.", k=2)
    assert tool.name == "search_knowledge"
    assert tool.description.startswith("Search the staff handbook.")
    assert "travel.md" in tool.description
    risk = declared_tool_risk(tool)
    assert risk is not None and risk.untrusted_input and risk.private_data
    text = tool.invoke({"query": "vacation days"})
    assert text.startswith("<tool_output")
    assert "[1] hr/vacation.txt (passage 1" in text
    assert "No passages" in tool.invoke({"query": "zebra"})
    assert "No passages" in tool.invoke({"query": "vacation", "source": "travel.md"})


async def test_an_agent_answers_from_the_knowledge_base(handbook: Path) -> None:
    kb = KnowledgeBase.from_directory(handbook)
    model = FakeChatModel(
        [tool_call("search_knowledge", {"query": "hotel limit"}), "Up to 150 EUR per night."]
    )
    agent = Agent(
        "helpdesk", instructions="Answer from the handbook.", model=model, tools=[kb.as_tool()]
    )
    result = await agent.run("How much may a hotel cost?")
    assert result.output == "Up to 150 EUR per night."
    tool_message = model.calls[1].messages[-1]
    assert "150 EUR per night" in str(tool_message.content)


def english() -> KnowledgeBase:
    kb = KnowledgeBase()
    kb.add_text(
        "# Invoicing FAQ\n\nWe send a copy of the invoice to the billing contact on request.",
        source="invoicing.md",
    )
    kb.add_text("# Returns\n\nHardware can be returned within 30 days.", source="returns.md")
    return kb


def test_the_corpus_language_is_detected_and_named_in_the_description() -> None:
    kb = english()
    german = KnowledgeBase()
    german.add_text(
        "Die Rechnung wird nicht vor dem Monatsende an den Kunden gesendet.", source="a"
    )

    assert kb.language == "English"
    assert german.language == "German"
    assert KnowledgeBase().language is None
    assert "The documents are in English; search with English terms." in kb.as_tool().description
    assert "documents are in" not in KnowledgeBase().as_tool().description


def test_a_mixed_corpus_names_no_language() -> None:
    kb = KnowledgeBase()
    kb.add_text("The invoice is sent to the customer and the contact.", source="en")
    kb.add_text("Die Rechnung wird an den Kunden und die Firma gesendet.", source="de")

    assert kb.language is None


def test_no_match_lists_the_documents_and_asks_for_one_retry_in_the_corpus_language() -> None:
    tool = english().as_tool(frame_output=False)

    text = tool.invoke({"query": "Rechnungskopie"})

    assert text.startswith("No passages match 'Rechnungskopie'.")
    assert "The documents are in English: search once more with English words" in text
    assert "promise nothing they do not support" in text
    assert "- invoicing.md: # Invoicing FAQ" in text
    assert "- returns.md: # Returns" in text
    assert "[1] invoicing.md" in tool.invoke({"query": "invoice copy"})


def test_no_match_within_one_source_lists_only_that_source() -> None:
    text = (
        english()
        .as_tool(frame_output=False)
        .invoke({"query": "Rechnungskopie", "source": "returns.md"})
    )

    assert "- returns.md" in text and "invoicing.md" not in text


FIXTURES = Path(__file__).parents[1] / "fixtures" / "web_search"
HTML, LITE = _search._ENDPOINTS
QUERY = "python programming language"


def page(name: str, status: int = 200, url: str = HTML) -> HttpResponse:
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    body = (FIXTURES / name).read_bytes() if name.endswith(".html") else name.encode()
    return HttpResponse(url, status, "", headers, body, truncated=False)


class Transport:
    """Stands in for `fetch`: answers in turn and records what was sent."""

    def __init__(self, answers: tuple[HttpResponse | Exception, ...]) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []
        self.sleeps: list[float] = []

    def __call__(
        self,
        policy: HttpPolicy,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        **kw: Any,
    ) -> HttpResponse:
        assert host_allowed(urllib.parse.urlsplit(url).hostname or "", policy.allowed_hosts)
        assert not policy.allow_private_networks
        form = dict(urllib.parse.parse_qsl((data or b"").decode()))
        self.requests.append({"url": url, "method": method, "form": form, "policy": policy, **kw})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Transport]]:
    now = [1_000.0]
    installed: list[Transport] = []

    def sleep(seconds: float) -> None:
        installed[-1].sleeps.append(round(seconds, 3))
        now[0] += seconds

    monkeypatch.setattr(_search, "_clock", lambda: now[0])
    monkeypatch.setattr(_search, "_sleep", sleep)
    monkeypatch.setattr(_search, "_PACER", _search._Pacer())

    def install(*answers: HttpResponse | Exception) -> Transport:
        fake = Transport(answers)
        installed.append(fake)
        monkeypatch.setattr(_search, "fetch", fake)
        return fake

    yield install
    assert all(not fake.answers for fake in installed), "unused answers"


def titles(results: list[SearchResult]) -> list[str]:
    return [result.title for result in results]


def test_hits_without_ads_with_redirects_unwrapped_and_duplicates_removed(
    transport: Callable[..., Transport],
) -> None:
    sent = transport(page("html_page1.html"))
    results = DuckDuckGoSearchProvider().search(QUERY, max_results=4)
    assert titles(results) == [
        "Welcome to Python.org",
        "Download Python | Python.org",
        "Python (programming language) - Wikipedia",
        "PEP 20 – The Zen of Python",
    ]
    assert results[3].url == "https://peps.python.org/pep-0020/"
    assert results[0].snippet.startswith("Python is a versatile and easy-to-learn language")
    [request] = sent.requests
    assert request["url"] == HTML and request["method"] == "POST"
    assert request["form"] == {"q": QUERY, "kl": "wt-wt", "kp": "-1"}
    assert request["policy"].user_agent.startswith("awa-agents/")
    assert request["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_further_pages_are_read_up_to_max_results_at_a_polite_pace(
    transport: Callable[..., Transport],
) -> None:
    sent = transport(page("html_page1.html"), page("html_page2.html"))
    results = DuckDuckGoSearchProvider(region="de-de", safe_search="off").search(
        QUERY, max_results=7
    )
    assert len(results) == len({result.url for result in results}) == 7
    assert "Python.org (again)" not in titles(results)  # the same page as python.org/
    second = sent.requests[1]["form"]
    assert second["s"] == "10" and second["q"] == QUERY and second["vqd"]
    assert sent.requests[0]["form"]["kl"] == "de-de" and sent.requests[0]["form"]["kp"] == "-2"
    assert sent.sleeps == [1.0]  # one request per second from the whole process


def test_requests_of_all_providers_share_the_rate_limit(
    transport: Callable[..., Transport],
) -> None:
    sent = transport(*(page("html_page1.html") for _ in range(3)))
    for region in ("wt-wt", "de-de", "us-en"):
        DuckDuckGoSearchProvider(region=region).search(QUERY, max_results=2)
    assert len(sent.requests) == 3 and sent.sleeps == [1.0, 1.0]


def test_a_refused_page_falls_back_to_the_lite_page(transport: Callable[..., Transport]) -> None:
    sent = transport(page("html_blocked.html", 202), page("lite_page1.html", url=LITE))
    results = DuckDuckGoSearchProvider().search(QUERY, max_results=3)
    assert titles(results)[0] == "Welcome to Python.org" and len(results) == 3
    assert [request["url"] for request in sent.requests] == [HTML, LITE]


def test_a_refusal_everywhere_is_one_actionable_error_and_pauses_searching(
    transport: Callable[..., Transport],
) -> None:
    sent = transport(page("html_blocked.html", 202), page("rate limited", 429, LITE))
    provider = DuckDuckGoSearchProvider()
    with pytest.raises(SearchBlockedError) as refused:
        provider.search(QUERY, max_results=3)
    message = str(refused.value)
    assert "refusing automated searches" in message and "paused for 60 seconds" in message
    assert "toolkits.web.search" in message and "WEB_SEARCH_URL" in message
    # The agent is told to make the outage visible, not to report an empty result as news.
    assert "say in your answer that the web search was unavailable" in message
    with pytest.raises(SearchBlockedError, match="paused for 60 seconds"):
        provider.search(QUERY, max_results=3)
    assert len(sent.requests) == 2  # nothing sent while paused
    _search._sleep(61)
    transport(page("html_page1.html"))
    assert len(provider.search(QUERY, max_results=2)) == 2


def test_a_changed_page_and_an_empty_result(transport: Callable[..., Transport]) -> None:
    redesign = "<html><body><main><p>Our new search experience</p></main></body></html>"
    transport(page(redesign), page(redesign, url=LITE))
    with pytest.raises(SearchFormatError, match="may have changed its result page"):
        DuckDuckGoSearchProvider().search(QUERY, max_results=3)
    transport(page("gone", 404), page(redesign, url=LITE))
    with pytest.raises(SearchFormatError, match="HTTP 404"):
        DuckDuckGoSearchProvider().search(QUERY, max_results=3)
    transport(page("html_no_results.html"))
    assert DuckDuckGoSearchProvider().search("zqxj", max_results=3) == []


def test_network_failures_and_server_errors_are_retried(
    transport: Callable[..., Transport],
) -> None:
    sent = transport(
        ToolException("the request timed out after 15 seconds"), page("html_page1.html")
    )
    assert len(DuckDuckGoSearchProvider().search(QUERY, max_results=2)) == 2
    assert len(sent.requests) == 2 and 0.5 in sent.sleeps
    sent = transport(page("busy", 503), page("busy", 503))
    with pytest.raises(SearchError, match="HTTP 503") as failed:
        DuckDuckGoSearchProvider().search(QUERY, max_results=2)
    assert type(failed.value) is SearchError and len(sent.requests) == 2
    transport(ToolException("connection refused"), ToolException("connection refused"))
    with pytest.raises(SearchError, match="could not reach DuckDuckGo: connection refused"):
        DuckDuckGoSearchProvider().search(QUERY, max_results=2)


def test_web_search_works_without_configuration_and_reports_errors_as_results(
    transport: Callable[..., Transport],
) -> None:
    tools = web_toolkit(frame_output=False)
    assert [tool.name for tool in tools] == ["fetch_url", "web_search"]
    search = tools[1]
    transport(page("html_page1.html"))
    text = search.invoke({"query": QUERY, "max_results": 2})
    assert text.startswith("1. Welcome to Python.org\n   https://www.python.org/\n")
    transport(page("html_blocked.html", 202), page("html_blocked.html", 202, LITE))
    refused = search.invoke({"query": QUERY})
    assert refused.startswith("Error: DuckDuckGo is refusing automated searches")


def test_any_provider_results_are_deduplicated() -> None:
    class Repeating:
        def search(self, query: str, *, max_results: int) -> list[SearchResult]:
            return [
                SearchResult("A", "https://www.example.com/a/"),
                SearchResult("A again", "http://example.com/a#top"),
                SearchResult("B", "https://example.com/b?x=1"),
            ]

    [search] = web_toolkit(search=Repeating(), include=["web_search"], frame_output=False)
    text = search.invoke({"query": "a"})
    assert "A again" not in text and "2. B" in text


def test_provider_selection_and_options() -> None:
    explicit = JSONSearchProvider("https://search.example.com/?q={query}")
    assert _search.search_provider(explicit) is explicit
    configured = _search.search_provider({"url_template": "https://s.example.com/?q={query}"})
    assert isinstance(configured, JSONSearchProvider)
    default = _search.search_provider(None, timeout=7, use_env_proxies=True)
    assert isinstance(default, DuckDuckGoSearchProvider)
    assert default.policy.timeout == 7 and default.policy.use_env_proxies
    assert _search.search_provider({"region": "de-de"}).region == "de-de"  # type: ignore[attr-defined]
    with pytest.raises(ConfigurationError, match="invalid search option"):
        web_toolkit(search={"results_path": "x"})
    with pytest.raises(ConfigurationError, match="region"):
        DuckDuckGoSearchProvider(region="Germany")
    with pytest.raises(ConfigurationError, match="safe_search"):
        DuckDuckGoSearchProvider(safe_search="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="negative"):
        DuckDuckGoSearchProvider(retries=-1)


def test_the_catalog_offers_web_search_without_configuration() -> None:
    entry = catalog().get("web_search")
    assert entry.required == () and entry.problems({}) == []
    assert entry.risk_for({}).external_action is False and not entry.approval_for({})
    assert catalog().create("web_search").name == "web_search"
    assert catalog().create("web_search", {"web": {"search": {"region": "fr-fr"}}})


def test_the_search_client_refuses_private_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = DuckDuckGoSearchProvider().policy
    with pytest.raises(ToolException, match="not in the allowed hosts"):
        policy.check_url("https://127.0.0.1/html/")

    def private(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    with pytest.raises(ToolException, match="private or reserved"):
        policy.check_url(HTML)  # a DNS answer pointing inside the network is refused
    fetch_url, _ = web_toolkit()
    refused = fetch_url.invoke({"url": "https://intranet.example.com/salaries"})
    assert refused.startswith("Error: ") and "private or reserved" in refused


@pytest.mark.live_network
def test_live_search_finds_a_well_known_page(request: pytest.FixtureRequest) -> None:
    selection = str(request.config.getoption("markexpr") or "")
    if "live_network" not in selection or "not live_network" in selection:
        pytest.skip("contacts DuckDuckGo: run it with -m live_network")
    results = DuckDuckGoSearchProvider().search(QUERY, max_results=5)
    assert len(results) == 5
    assert any("python.org" in result.url for result in results)
    assert all(result.title and result.url.startswith("http") for result in results)


def test_auto_matching_grants_web_search_and_suggests_open_fetch_url() -> None:
    from awa.blueprints import Blueprint

    text = """
name: research
auto: true
agents:
  researcher: Does online research on the internet about competitors.
"""
    report = Blueprint.from_yaml_text(text).explain()
    [agent] = report.agents
    assert "builtin:web_search" in agent.tools
    [suggested] = [match for match in agent.matches if not match.granted]
    assert suggested.name == "builtin:fetch_url" and "allowed_hosts" in suggested.note
    allowlisted = Blueprint.from_yaml_text(
        text + "toolkits:\n  web: {allowed_hosts: [docs.example.com]}\n"
    ).explain()
    assert "builtin:fetch_url" in allowlisted.agents[0].tools


def test_a_next_page_offset_with_unicode_digits_is_ignored() -> None:
    def parsed(offset: str) -> dict[str, str]:
        page = _search._ResultPage()
        page.parse(
            '<form action="/html/" method="post"><input type="hidden" name="q" value="x">'
            f'<input type="hidden" name="s" value="{offset}"></form>'
        )
        return page.next_form

    assert parsed("30")["s"] == "30"
    assert parsed("\u00b2") == {}  # "superscript two" is a digit to str.isdigit but not to int
