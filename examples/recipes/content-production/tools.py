"""Tools for the content-production recipe, backed by small in-memory sample systems.

The tools stand in for the marketing request queue (content calendar), the brand
guidelines, the approved product facts, the compliance rulebook and consent records, and
the content management system (CMS). Replace each body with a call to your system; keep
the names, signatures and docstrings, which the agents rely on.

`blueprint.yaml` produces a compliance-checked CMS draft and publishes it after the content
owner approves (`publish_article` is approval-gated).
"""

from __future__ import annotations

import json
from typing import Any, Literal

_REQUESTS: dict[str, dict[str, Any]] = {
    "REQ-201": {
        "type": "blog post",
        "channel": "blog",
        "topic": "How predictive maintenance cuts unplanned downtime",
        "audience": "plant and maintenance managers at mid-sized manufacturers",
        "goal": "demo requests",
        "product": "Sample Maintenance Cloud",
        "length": "400-600 words",
        "keywords": ["predictive maintenance", "unplanned downtime"],
    },
    "REQ-202": {
        "type": "social post",
        "channel": "social",
        "topic": "Our promise: 50% lower maintenance costs, guaranteed",
        "audience": "operations directors",
        "goal": "webinar sign-ups",
        "product": "Sample Maintenance Cloud",
        "length": "at most 80 words",
        "keywords": ["maintenance costs"],
    },
    "REQ-203": {
        "type": "newsletter item",
        "channel": "newsletter",
        "topic": "Customer story: how Sample Foods GmbH avoided a line stoppage",
        "audience": "newsletter subscribers in manufacturing",
        "goal": "case study downloads",
        "product": "Sample Maintenance Cloud",
        "length": "150-200 words",
        "keywords": ["customer story"],
        "customer": "Sample Foods GmbH",
    },
}

BRAND_GUIDELINES = """Brand voice: clear, practical, confident; speak to the reader as "you".
Banned words: revolutionary, game-changing, best-in-class, world-leading.
Every piece has one call to action that matches the request's goal.
Headlines: at most 70 characters, no exclamation marks."""

_PRODUCT_FACTS: dict[str, list[str]] = {
    "Sample Maintenance Cloud": [
        "Reduced unplanned downtime by up to 30% in a 2025 pilot with 12 plants.",
        "Connects to existing machines through standard OPC UA gateways.",
        "Hosted in the EU; setup for one production line takes about two weeks.",
    ]
}

COMPLIANCE_RULES = """CR-1 No guarantees or absolute claims ("guaranteed", "always", "never fails").
CR-2 Every figure names its basis (for example "in a 2025 pilot with 12 plants") and uses
     "up to" for best results.
CR-3 Blog posts and newsletter items that cite results end with: "Results depend on the
     plant and its machines."
CR-4 A named customer may only appear with documented consent (check_reference_consent)."""

_CONSENTS: dict[str, bool] = {"Sample Foods GmbH": False, "Sample Metals AG": True}

CMS: dict[str, dict[str, Any]] = {}
"""Drafts and published articles in the CMS, by draft id."""
PUBLISHED: list[dict[str, str]] = []
"""Articles made public."""


def reset_sample_data() -> None:
    """Restore the sample systems to their initial state (used by tests)."""

    CMS.clear()
    PUBLISHED.clear()


def get_content_request(request_id: str) -> str:
    """Return a content request from the marketing queue: type, channel, topic, audience,
    goal, product, length, keywords (and the customer for customer stories).

    In production: read the request from your content calendar or marketing work queue.
    """

    request = _REQUESTS.get(request_id.strip().upper())
    return (
        json.dumps({"request_id": request_id, **request})
        if request
        else f"No request {request_id!r}."
    )


def get_brand_guidelines() -> str:
    """Return the brand voice, banned words and headline rules.

    In production: load the brand guide from your brand or design system.
    """

    return BRAND_GUIDELINES


def get_product_facts(product: str) -> str:
    """Return the approved, sourced statements about a product; nothing else may be claimed.

    In production: read the approved claims from your product marketing or PIM system.
    """

    facts = _PRODUCT_FACTS.get(product.strip())
    return (
        json.dumps({"product": product, "facts": facts}) if facts else f"No facts for {product!r}."
    )


def get_compliance_rules(channel: Literal["blog", "social", "newsletter"]) -> str:
    """Return the marketing compliance rules (CR-1 to CR-4) that apply to a channel.

    In production: load the current rulebook from your legal or compliance system.
    """

    return f"Channel: {channel}\n{COMPLIANCE_RULES}"


def check_reference_consent(customer: str) -> str:
    """Return whether a customer has given documented consent to be named in marketing.

    In production: look the consent up in your CRM or reference program.
    """

    consent = _CONSENTS.get(customer.strip())
    if consent is None:
        return f"No consent record for {customer!r}: treat as no consent."
    return f"{customer}: consent {'on file' if consent else 'NOT on file'}."


def save_cms_draft(request_id: str, title: str, body: str, channel: str) -> str:
    """Save a cleared article as a CMS draft with status "ready_for_publication".

    Drafts are not public. In production: create the draft through your CMS's API.
    """

    draft_id = f"D-{9001 + len(CMS)}"
    CMS[draft_id] = {
        "request_id": request_id,
        "title": title,
        "body": body,
        "channel": channel,
        "status": "ready_for_publication",
    }
    return f"Saved {draft_id} ({channel}) as ready_for_publication."


def get_cms_draft(draft_id: str) -> str:
    """Return a CMS draft: request id, title, body, channel and status.

    In production: read the draft from your CMS.
    """

    draft = CMS.get(draft_id.strip().upper())
    return json.dumps({"draft_id": draft_id, **draft}) if draft else f"No CMS draft {draft_id!r}."


def publish_article(draft_id: str) -> str:
    """Publish a CMS draft on its channel. Public and hard to take back: the content owner
    approves every publication.

    In production: publish through your CMS (and scheduling tool for social posts).
    """

    draft = CMS.get(draft_id.strip().upper())
    if draft is None:
        return f"Not published: no CMS draft {draft_id!r}."
    if draft["status"] != "ready_for_publication":
        return f"Not published: {draft_id} is {draft['status']}."
    url = f"https://www.company.example/{draft['channel']}/{draft_id.lower()}"
    draft["status"] = "published"
    PUBLISHED.append({"draft_id": draft_id, "url": url})
    return f"Published {draft_id} at {url}."


__all__ = [
    "check_reference_consent",
    "get_brand_guidelines",
    "get_cms_draft",
    "get_compliance_rules",
    "get_content_request",
    "get_product_facts",
    "publish_article",
    "save_cms_draft",
]
