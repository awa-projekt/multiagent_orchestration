"""Tools for the lead-qualification recipe, backed by small in-memory sample systems.

The tools stand in for the CRM (inbound leads and their qualification fields), a company
data provider (firmographics), a news search, the ICP rubric maintained by sales ops, and
the email gateway. Replace each body with a call to your system; keep the names,
signatures and docstrings, which the agents rely on.
"""

from __future__ import annotations

import json
from typing import Any, Literal

_LEADS: dict[str, dict[str, Any]] = {
    "L-301": {
        "contact": "Alex Sample",
        "title": "Head of Operations",
        "email": "alex.sample@sample-freight.example",
        "company": "Sample Freight GmbH",
        "domain": "sample-freight.example",
        "source": "demo request form",
        "message": "We are evaluating tools to automate dispatch planning for our depots.",
        "account_status": "prospect",
    },
    "L-302": {
        "contact": "Jo Example",
        "title": "Owner",
        "email": "jo@tiny-bakery.example",
        "company": "Tiny Bakery",
        "domain": "tiny-bakery.example",
        "source": "newsletter sign-up",
        "message": "Do you have an app to plan my bakery's delivery bike route?",
        "account_status": "prospect",
    },
    "L-303": {
        "contact": "Kim Placeholder",
        "title": "Operations Manager",
        "email": "kim@stealth-startup.example",
        "company": "Stealth Startup",
        "domain": "stealth-startup.example",
        "source": "webinar",
        "message": "Interested in route optimization.",
        "account_status": "prospect",
    },
    "L-304": {
        "contact": "Lee Demo",
        "title": "Logistics Director",
        "email": "lee.demo@sample-wholesale.example",
        "company": "Sample Wholesale AG",
        "domain": "sample-wholesale.example",
        "source": "demo request form",
        "message": "We would like to add two more warehouses to our plan.",
        "account_status": "customer",
        "account_manager": "Morgan Account",
    },
}

_COMPANIES: dict[str, dict[str, Any]] = {
    "sample-freight.example": {
        "name": "Sample Freight GmbH",
        "industry": "logistics",
        "employees": 420,
        "country": "DE",
        "business_model": "B2B",
    },
    "tiny-bakery.example": {
        "name": "Tiny Bakery",
        "industry": "food retail",
        "employees": 6,
        "country": "DE",
        "business_model": "B2C",
    },
    "sample-wholesale.example": {
        "name": "Sample Wholesale AG",
        "industry": "wholesale",
        "employees": 1800,
        "country": "AT",
        "business_model": "B2B",
    },
}

_NEWS: dict[str, list[dict[str, str]]] = {
    "Sample Freight GmbH": [
        {
            "id": "N-1",
            "date": "2026-09-10",
            "headline": "Sample Freight opens two new depots in Leipzig and Hanover",
        },
        {
            "id": "N-2",
            "date": "2026-08-28",
            "headline": "Sample Freight hires a VP Digital to modernise dispatch",
        },
    ]
}

ICP_RUBRIC = """Ideal customer profile (max 100 points)
1. Industry (30): logistics, wholesale or manufacturing = 30; other B2B = 10; B2C = 0.
2. Company size (25): 200-5,000 employees = 25; 50-199 = 10; otherwise 0.
3. Region (15): EU, UK or Switzerland = 15; otherwise 0.
4. Buying signal (20): evaluation stated in the request or expansion/funding news in the
   last 6 months = 20; one weak signal = 10; none = 0.
5. Contact seniority (10): head, director, VP or C-level = 10; manager = 5; other = 0.
Disqualifiers (tier "disqualified" regardless of points): consumer business (B2C), fewer
than 20 employees, student or job seeker, competitor.
Tiers: A >= 75, B 50-74, C 25-49, disqualified < 25. Only A and B leads get a first email;
C leads go to nurture campaigns. Unknown firmographics: score only what is known, tier C,
and say in the summary which facts are unknown and need manual research."""

CRM_UPDATES: list[dict[str, Any]] = []
"""Qualification results written to the CRM."""
OUTBOX: list[dict[str, str]] = []
"""Emails sent."""


def reset_sample_data() -> None:
    """Restore the sample systems to their initial state (used by tests)."""

    CRM_UPDATES.clear()
    OUTBOX.clear()


def get_lead(lead_id: str) -> str:
    """Return an inbound lead from the CRM: contact, title, email, company, domain, message.

    Includes `account_status` ("prospect" or "customer") and the account manager of existing
    customers. In production: read the lead from your CRM.
    """

    lead = _LEADS.get(lead_id.strip().upper())
    return json.dumps({"lead_id": lead_id, **lead}) if lead else f"No lead {lead_id!r} in the CRM."


def get_company_profile(domain: str) -> str:
    """Return firmographics for a company domain: industry, employees, country, business model.

    In production: query your company data provider.
    """

    profile = _COMPANIES.get(domain.strip().lower())
    return json.dumps({"domain": domain, **profile}) if profile else f"No profile for {domain!r}."


def search_company_news(company: str) -> str:
    """Return news items about a company from the last 6 months, each with id and date.

    In production: call your news or web search API.
    """

    return json.dumps({"company": company, "items": _NEWS.get(company.strip(), [])})


def get_icp_rubric() -> str:
    """Return the ideal-customer-profile scoring rubric, disqualifiers and tier limits.

    In production: load the rubric sales operations maintain (document or CRM settings).
    """

    return ICP_RUBRIC


def update_crm_lead(
    lead_id: str,
    score: int,
    tier: Literal["A", "B", "C", "disqualified", "not_scored"],
    status: Literal["sales_ready", "nurture", "disqualified", "existing_customer"],
    notes: str,
) -> str:
    """Write the qualification result to the lead in the CRM (score, tier, status, notes).

    In production: update the lead through your CRM's API; status changes trigger the CRM's
    own routing (for example nurture campaigns or the account manager's queue).
    """

    if lead_id.strip().upper() not in _LEADS:
        return f"No lead {lead_id!r} in the CRM."
    CRM_UPDATES.append(
        {"lead_id": lead_id, "score": score, "tier": tier, "status": status, "notes": notes}
    )
    return f"Updated {lead_id}: tier {tier}, status {status}."


def send_lead_email(to: str, subject: str, body: str) -> str:
    """Send an email from the sales rep's mailbox. External: a human approves every email.

    In production: send through your email gateway or sales engagement tool, and log the
    email on the CRM lead.
    """

    OUTBOX.append({"to": to, "subject": subject, "body": body})
    return f"Sent {subject!r} to {to}."


__all__ = [
    "get_company_profile",
    "get_icp_rubric",
    "get_lead",
    "search_company_news",
    "send_lead_email",
    "update_crm_lead",
]
