"""Tools for the it-incident-triage recipe, backed by small in-memory sample systems.

The tools stand in for the service catalog, monitoring (health, logs), the deployment
system, the runbook wiki, the incident management and paging system, the public status
page and the post-incident notes space. Replace each body with a call to your system;
keep the names, signatures and docstrings, which the agents rely on.

`paging_needs_approval` and `status_update_needs_approval` decide which calls wait for a
human (see `approvals:` in `blueprint.yaml`).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any, Literal

Severity = Literal["SEV1", "SEV2", "SEV3", "SEV4"]

_SERVICES: dict[str, dict[str, Any]] = {
    "checkout": {
        "tier": 1,
        "customer_facing": True,
        "owner": "payments-oncall",
        "runbook": "RB-12",
    },
    "payments-api": {
        "tier": 1,
        "customer_facing": True,
        "owner": "payments-oncall",
        "runbook": "RB-14",
    },
    "search": {"tier": 2, "customer_facing": True, "owner": "discovery-oncall", "runbook": "RB-7"},
    "reporting": {"tier": 3, "customer_facing": False, "owner": "data-oncall", "runbook": "RB-3"},
}

_HEALTH: dict[str, dict[str, Any]] = {
    "checkout": {"status": "down", "error_rate_pct": 38.0, "p95_ms": 4200},
    "payments-api": {"status": "degraded", "error_rate_pct": 2.1, "p95_ms": 1800},
    "search": {"status": "healthy", "error_rate_pct": 0.1, "p95_ms": 180},
    "reporting": {"status": "degraded", "error_rate_pct": 0.0, "p95_ms": 9100},
}

_LOGS: dict[str, list[str]] = {
    "checkout": [
        "09:14:02 ERROR db: connection pool exhausted (max=20, waiting=412)",
        "09:14:05 ERROR order-service: timeout acquiring connection after 3000 ms",
    ],
    "payments-api": ["10:02:11 WARN card-network: upstream timeout after 1500 ms (retrying)"],
    "reporting": ["08:55:40 WARN query reports_daily running for 312 s (full table scan)"],
}

_DEPLOYS: dict[str, list[dict[str, str]]] = {
    "checkout": [{"id": "D-881", "at": "09:12", "change": "lower DB connection pool to 20"}],
    "payments-api": [],
    "reporting": [{"id": "D-870", "at": "yesterday 17:40", "change": "new daily revenue report"}],
}

_RUNBOOKS: dict[str, str] = {
    "RB-12": "Checkout down: 1. Check deploys of the last 2 hours; if one matches the start, "
    "roll it back (deploy tool, one command). 2. Check DB connection pool usage. 3. Fail over "
    "to the secondary region if errors stay above 20% for 10 minutes.",
    "RB-14": "Payments latency: 1. Check card network status. 2. If upstream timeouts, enable "
    "the retry circuit breaker. 3. Inform the payment provider's support.",
    "RB-7": "Search degraded: 1. Check the index cluster. 2. Restart the slowest node.",
    "RB-3": "Reporting slow: 1. Find long-running queries. 2. Cancel them and add the missing "
    "index; reports are not customer-facing, fix within business hours.",
}

_SEED_INCIDENTS: dict[str, dict[str, Any]] = {
    "INC-7001": {
        "title": "Search results empty for 25 minutes",
        "service": "search",
        "severity": "SEV2",
        "status": "resolved",
        "timeline": [
            "14:02 alert: search error rate 60%",
            "14:05 incident opened, discovery-oncall paged",
            "14:11 cause found: index alias pointed to an empty index after re-indexing job",
            "14:19 alias switched back, error rate normal",
            "14:27 resolved; re-indexing job paused",
        ],
    }
}

INCIDENTS: dict[str, dict[str, Any]] = copy.deepcopy(_SEED_INCIDENTS)
PAGES: list[dict[str, Any]] = []
STATUS_UPDATES: list[dict[str, Any]] = []
NOTES: list[dict[str, str]] = []


def reset_sample_data() -> None:
    """Restore the sample systems to their initial state (used by tests)."""

    INCIDENTS.clear()
    INCIDENTS.update(copy.deepcopy(_SEED_INCIDENTS))
    PAGES.clear()
    STATUS_UPDATES.clear()
    NOTES.clear()


def paging_needs_approval(args: Mapping[str, Any]) -> bool:
    """A SEV1 page wakes the whole major-incident rotation, so the on-duty engineer confirms it."""

    return str(args.get("severity", "")).upper() == "SEV1"


def status_update_needs_approval(args: Mapping[str, Any]) -> bool:
    """Public status updates are read by customers, so a human confirms them."""

    return args.get("audience") == "public"


def get_service(service: str) -> str:
    """Return a service's tier (1 = most critical), whether customers use it directly, the
    on-call rotation that owns it and its runbook id. `service` is the catalog id; an
    unknown name returns the ids of the catalog to choose from.

    In production: read the service catalog (CMDB).
    """

    record = _SERVICES.get(service.strip().lower())
    if record is None:
        return f"Unknown service {service!r}. Catalog ids: {', '.join(_SERVICES)}."
    return json.dumps({"service": service, **record})


def get_service_health(service: str) -> str:
    """Return a service's live status, error rate (%) and p95 latency (ms).

    In production: query your monitoring system.
    """

    health = _HEALTH.get(service.strip().lower())
    return json.dumps({"service": service, **health}) if health else f"No metrics for {service!r}."


def query_logs(service: str, minutes: int = 30) -> str:
    """Return the warnings and errors a service logged in the last `minutes` minutes.

    In production: run a query against your log platform.
    """

    return json.dumps(
        {"service": service, "minutes": minutes, "lines": _LOGS.get(service.strip().lower(), [])}
    )


def get_recent_deploys(service: str) -> str:
    """Return the deployments of a service in the last 24 hours (id, time, change).

    In production: read your deployment or CI/CD system.
    """

    return json.dumps({"service": service, "deploys": _DEPLOYS.get(service.strip().lower(), [])})


def lookup_runbook(runbook_id: str) -> str:
    """Return the steps of a runbook.

    In production: fetch the page from your runbook wiki.
    """

    steps = _RUNBOOKS.get(runbook_id.strip().upper())
    return steps if steps else f"No runbook {runbook_id!r}."


def open_incident(title: str, service: str, severity: Severity) -> str:
    """Open an incident record and return its id.

    In production: create the incident in your incident management or ticketing system.
    """

    incident_id = f"INC-{7101 + len(INCIDENTS) - len(_SEED_INCIDENTS)}"
    INCIDENTS[incident_id] = {
        "title": title,
        "service": service,
        "severity": severity,
        "status": "investigating",
        "timeline": [f"incident opened as {severity}"],
    }
    return f"Opened {incident_id} ({severity}) for {service}."


def get_incident(incident_id: str) -> str:
    """Return an incident's title, service, severity, status and timeline.

    In production: read the incident from your incident management system.
    """

    incident = INCIDENTS.get(incident_id.strip().upper())
    if incident is None:
        return f"No incident {incident_id!r}."
    return json.dumps({"incident_id": incident_id, **incident})


def page_responders(incident_id: str, service: str, severity: Severity, summary: str) -> str:
    """Page the on-call rotation that owns the service (SEV1 also pages the major-incident
    rotation and the incident manager). A SEV1 page waits for the on-duty engineer's approval.

    In production: trigger the page in your paging system.
    """

    record = _SERVICES.get(service.strip().lower())
    if record is None:
        return f"Not paged: unknown service {service!r}."
    targets = [
        record["owner"],
        *(["major-incident", "incident-manager"] if severity == "SEV1" else []),
    ]
    PAGES.append(
        {"incident_id": incident_id, "severity": severity, "targets": targets, "summary": summary}
    )
    return f"Paged {', '.join(targets)} for {incident_id}."


def post_status_update(
    incident_id: str, audience: Literal["internal", "public"], message: str
) -> str:
    """Post an incident update to the internal incident channel or the public status page.

    Public updates wait for a human's approval. In production: post through your status page
    and chat integrations.
    """

    if incident_id.strip().upper() not in INCIDENTS:
        return f"Not posted: no incident {incident_id!r}."
    STATUS_UPDATES.append({"incident_id": incident_id, "audience": audience, "message": message})
    return f"Posted the {audience} update for {incident_id}."


def save_post_incident_notes(incident_id: str, notes: str) -> str:
    """Save the post-incident notes (summary, timeline, cause, decisions, action items with
    owners) to the incident record and the notes space.

    In production: write the page to your wiki and link it from the incident.
    """

    if incident_id.strip().upper() not in INCIDENTS:
        return f"Not saved: no incident {incident_id!r}."
    NOTES.append({"incident_id": incident_id, "notes": notes})
    return f"Saved the post-incident notes of {incident_id}."


__all__ = [
    "get_incident",
    "get_recent_deploys",
    "get_service",
    "get_service_health",
    "lookup_runbook",
    "open_incident",
    "page_responders",
    "post_status_update",
    "query_logs",
    "save_post_incident_notes",
]
