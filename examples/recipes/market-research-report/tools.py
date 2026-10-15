"""Tools for the market-research-report recipe, backed by a small in-memory research corpus.

The tools stand in for your research sources (a document search over analyst reports,
surveys and news), a market statistics database, a competitor intelligence database and the
internal knowledge base where finished reports are stored. Replace each body with a call to
your system; keep the names, signatures and docstrings, which the agents rely on.
"""

from __future__ import annotations

import json
from typing import Any

_MARKET = "predictive maintenance software"

_SOURCES: dict[str, dict[str, str]] = {
    "S-1": {
        "title": "Industrial software market outlook 2026",
        "publisher": "Sample Analyst Group",
        "date": "2026-03",
        "text": "The DACH market for predictive maintenance software reached 1.9 bn EUR in 2025 "
        "and is expected to grow by 14% a year until 2030. Discrete manufacturing accounts "
        "for 58% of spending, process industries for 42%.",
    },
    "S-2": {
        "title": "Survey: maintenance digitalisation in mid-sized manufacturing",
        "publisher": "Sample Industry Association",
        "date": "2026-05",
        "text": "38% of manufacturers with 100-999 employees use condition monitoring today; "
        "61% plan to invest within two years. The top barrier is integration with existing "
        "machines (54%), followed by a lack of data skills (41%).",
    },
    "S-3": {
        "title": "Energy prices push factories to cut unplanned downtime",
        "publisher": "Sample Business News",
        "date": "2026-07",
        "text": "Plant managers report that one hour of unplanned downtime costs mid-sized "
        "plants 10,000 to 50,000 EUR; energy prices make idle lines more expensive.",
    },
    "S-4": {
        "title": "Competitor landscape predictive maintenance DACH 2026",
        "publisher": "Sample Competitive Intelligence",
        "date": "2026-06",
        "text": "Vendor A (global industrial suite) 22% share, sells to large enterprises. "
        "Vendor B (sensor and software bundle) 15% share, hardware-led. Vendor C (SaaS for "
        "mid-sized plants) 6% share, fastest growing. Long tail of integrators: 57%.",
    },
}

_STATISTICS: dict[tuple[str, str], dict[str, Any]] = {
    (_MARKET, "dach"): {
        "market_size_2025_eur_bn": 1.9,
        "cagr_2025_2030_pct": 14,
        "segments_pct": {"discrete manufacturing": 58, "process industries": 42},
        "adoption_mid_sized_pct": 38,
        "source": "S-1, S-2",
    }
}

_COMPETITORS: dict[str, list[dict[str, Any]]] = {
    _MARKET: [
        {"name": "Vendor A", "share_pct": 22, "focus": "large enterprises, full suite"},
        {"name": "Vendor B", "share_pct": 15, "focus": "sensor and software bundles"},
        {"name": "Vendor C", "share_pct": 6, "focus": "SaaS for mid-sized plants, fastest growing"},
    ]
}

KNOWLEDGE_BASE: list[dict[str, str]] = []
"""Reports saved to the internal knowledge base."""


def reset_sample_data() -> None:
    """Restore the sample systems to their initial state (used by tests)."""

    KNOWLEDGE_BASE.clear()


def _matches(query: str, source: dict[str, str]) -> bool:
    words = [w for w in query.casefold().split() if len(w) > 3]
    haystack = f"{source['title']} {source['text']}".casefold()
    return sum(word in haystack for word in words) >= 2


def search_sources(query: str) -> str:
    """Search the research corpus; returns matching sources with id, title, publisher and date.

    In production: query your research library, analyst-report subscriptions or a web
    search with an allow-list of publishers.
    """

    hits = [
        {"id": sid, **{k: v for k, v in source.items() if k != "text"}}
        for sid, source in _SOURCES.items()
        if _matches(query, source)
    ]
    return json.dumps({"query": query, "sources": hits})


def read_source(source_id: str) -> str:
    """Return the full text of a source with its title, publisher and date.

    In production: fetch the document from your research library.
    """

    source = _SOURCES.get(source_id.strip().upper())
    return json.dumps({"id": source_id, **source}) if source else f"No source {source_id!r}."


def get_market_statistics(market: str, region: str) -> str:
    """Return market size, growth, segment split and adoption for a market and region, with
    the source ids the figures come from.

    In production: query your market statistics database.
    """

    figures = _STATISTICS.get((market.strip().casefold(), region.strip().casefold()))
    if figures is None:
        return f"No statistics for {market!r} in {region!r}."
    return json.dumps({"market": market, "region": region, **figures})


def get_competitor_profiles(market: str) -> str:
    """Return the main competitors of a market with share and focus, and the source id.

    In production: query your competitive intelligence database or CRM win/loss data.
    """

    profiles = _COMPETITORS.get(market.strip().casefold())
    if profiles is None:
        return f"No competitor profiles for {market!r}."
    return json.dumps({"market": market, "competitors": profiles, "source": "S-4"})


def save_report_to_knowledge_base(title: str, report: str) -> str:
    """Save a finished, reviewed report to the internal knowledge base; returns its link.

    In production: create the page in your wiki or document management system.
    """

    link = f"kb://reports/{len(KNOWLEDGE_BASE) + 1}"
    KNOWLEDGE_BASE.append({"title": title, "report": report, "link": link})
    return f"Saved {title!r} as {link}."


__all__ = [
    "get_competitor_profiles",
    "get_market_statistics",
    "read_source",
    "save_report_to_knowledge_base",
    "search_sources",
]
