"""A small CRM exposed as an MCP server over stdio, used by `09_mcp_tools.py`.

It stands in for any system a company already runs behind an MCP server.
Start it by hand with `python examples/mcp_crm_server.py` (it then waits for
a client on stdin/stdout).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("crm", log_level="WARNING")

CUSTOMERS = {
    "C-100": {"name": "ACME Corp", "tier": "gold"},
    "C-200": {"name": "Globex", "tier": "silver"},
}
TICKETS = [
    {"id": "T-1", "customer_id": "C-100", "subject": "Invoice shows the wrong VAT rate"},
    {"id": "T-2", "customer_id": "C-100", "subject": "Delivery 4711 is late"},
]


@server.tool()
def find_customer(name: str) -> dict[str, str]:
    """Find a customer by (part of) its name and return its id, name and tier."""

    for customer_id, customer in CUSTOMERS.items():
        if name.lower() in customer["name"].lower():
            return {"customer_id": customer_id, **customer, "region": _region()}
    raise ValueError(f"No customer matches {name!r}")


@server.tool()
def list_open_tickets(customer_id: str) -> dict[str, list[dict[str, str]]]:
    """List the open support tickets of a customer."""

    return {"tickets": [ticket for ticket in TICKETS if ticket["customer_id"] == customer_id]}


@server.tool()
def create_ticket(customer_id: str, subject: str) -> dict[str, str]:
    """Open a support ticket for a customer."""

    if customer_id not in CUSTOMERS:
        raise ValueError(f"Unknown customer {customer_id!r}")
    ticket = {"id": f"T-{len(TICKETS) + 1}", "customer_id": customer_id, "subject": subject}
    TICKETS.append(ticket)
    return ticket


@server.tool()
def delete_customer(customer_id: str) -> str:
    """Delete a customer and all its data."""

    CUSTOMERS.pop(customer_id, None)
    return f"deleted {customer_id}"


def _region() -> str:
    return os.environ.get("CRM_REGION", "unknown")


if __name__ == "__main__":
    server.run()
