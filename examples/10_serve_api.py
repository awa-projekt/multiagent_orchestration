"""Serve an agent system as an HTTP API, on its own or inside an existing FastAPI app.

A refunds desk runs as a web service. Every caller authenticates with an API
key that maps to a `Principal`: two customers and a reviewer of the same
shop. The customer id the tools use comes from the caller's identity
(`principal_deps`), never from the request, so a customer can only see and
refund their own orders. Threads belong to the customer who started them
(`thread_scope="principal"`); refunds above EUR 100 pause until a principal
with the `refund-approver` role answers them, and the server records who did.

Clients start a run with `POST /v1/systems/refunds/runs` (`?stream=true` for
Server-Sent Events, `?mode=async` for a run id to poll at `/v1/runs/{id}`).
Usage, request rate and concurrency are capped on the server.

The same desk also mounts into a shop's existing FastAPI app with
`create_router`: its endpoints live under `/ai/agents/...` next to the shop's
own routes, the shop's own login (a session header) becomes the `Principal`,
and `router_lifespan` drains runs still in flight inside the app's own
lifespan.

Run:
    pip install -e ".[anthropic,server]"
    export ANTHROPIC_API_KEY=...
    export REFUNDS_CUSTOMER_KEY=c7-change-me REFUNDS_REVIEWER_KEY=rev-change-me
    python examples/10_serve_api.py          # the desk on its own
    python examples/10_serve_api.py --shop   # the desk inside the shop's app
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_10_serve_api.py

Then, in another terminal:
    curl -s localhost:8000/v1/systems/refunds/runs -H "Authorization: Bearer c7-change-me" \\
        -H "Content-Type: application/json" \\
        -d '{"input": "Refund order A-1002, it never arrived."}'
    # -> {"status": "interrupted", "thread_id": "thread_...", "interrupts": [...], ...}
    curl -s localhost:8000/v1/systems/refunds/threads/<thread_id>/resume \\
        -H "Authorization: Bearer rev-change-me" -H "Content-Type: application/json" \\
        -d '{"decision": {"action": "approve"}}'

    # with --shop: the shop's own route and the desk under /ai, both behind its login
    curl -s localhost:8000/orders -H "X-Session: s-c7"
    curl -s localhost:8000/ai/agents/systems/refunds/runs -H "X-Session: s-c7" \\
        -H "Content-Type: application/json" -d '{"input": "Where is my order A-1002?"}'

The OpenAPI docs are at http://localhost:8000/docs.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from langchain_core.tools import BaseTool

from _support import require_model_credentials
from awa import Agent, UsageLimits, current_run_context, tool
from awa.persistence import StateInMemorySaver
from awa.server import (
    ApiKeyAuthenticator,
    Principal,
    RateLimit,
    ServedSystem,
    create_app,
    create_router,
    router_lifespan,
    serve,
)
from awa.tools import create_tool_approval_wrapper

APPROVAL_THRESHOLD_EUR = 100.0
APPROVER_ROLE = "refund-approver"

ORDERS: dict[str, dict[str, Any]] = {
    "A-1001": {"customer": "c-7", "total": 25.0, "status": "delivered"},
    "A-1002": {"customer": "c-7", "total": 480.0, "status": "lost in transit"},
    "B-2001": {"customer": "c-9", "total": 60.0, "status": "delivered"},
}


@dataclass
class PaymentsGateway:
    """In-memory stand-in for a payments API."""

    refunds: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True)
class CustomerContext:
    """What a run knows about the caller: set by the server from the caller's identity."""

    customer_id: str


def build_tools(gateway: PaymentsGateway) -> list[BaseTool]:
    """The order lookup (scoped to the calling customer) and the approval-gated refund."""

    @tool
    def lookup_order(order_id: str) -> str:
        """Return the status and total of one of the customer's orders."""

        ctx = current_run_context()
        customer = ctx.deps.customer_id if ctx is not None and ctx.deps is not None else None
        order = ORDERS.get(order_id)
        if order is None or order["customer"] != customer:
            return f"No order {order_id!r} found for this customer."
        return json.dumps({"order_id": order_id, **order})

    @tool
    def issue_refund(order_id: str, amount_eur: float, reason: str) -> str:
        """Refund part or all of an order to the customer's original payment method."""

        gateway.refunds.append((order_id, amount_eur))
        return f"Refunded EUR {amount_eur:.2f} for order {order_id}."

    def needs_approval(args: dict[str, Any]) -> bool:
        return float(args["amount_eur"]) > APPROVAL_THRESHOLD_EUR

    return [
        lookup_order,
        create_tool_approval_wrapper(issue_refund, requires_approval=needs_approval),
    ]


def build_agent(gateway: PaymentsGateway | None = None) -> Agent:
    """Build the refunds agent; its checkpointer keeps paused runs between requests."""

    return Agent(
        "refunds",
        description="Handles customer refund requests for their own orders.",
        instructions=(
            "Look the order up with lookup_order before refunding it, and refund at most the "
            "order total with issue_refund. If a refund is rejected by a reviewer, tell the "
            "customer it could not be approved and why."
        ),
        tools=build_tools(gateway or PaymentsGateway()),
        checkpointer=StateInMemorySaver(),
    )


def build_authenticator() -> ApiKeyAuthenticator:
    """API keys of the shop's callers. In production, load key digests from your secret store
    (`ApiKeyAuthenticator.from_digests`) or validate tokens with `BearerTokenAuthenticator`."""

    return ApiKeyAuthenticator(
        {
            os.environ.get("REFUNDS_CUSTOMER_KEY", "c7-change-me"): Principal("c-7", tenant="shop"),
            os.environ.get("REFUNDS_OTHER_CUSTOMER_KEY", "c9-change-me"): Principal(
                "c-9", tenant="shop"
            ),
            os.environ.get("REFUNDS_REVIEWER_KEY", "rev-change-me"): Principal(
                "rev-1", tenant="shop", roles={APPROVER_ROLE}
            ),
        }
    )


def build_systems(agent: Agent | None = None) -> dict[str, ServedSystem]:
    """What the server serves: the refunds agent with identity-derived deps and caps."""

    return {
        "refunds": ServedSystem(
            agent or build_agent(),
            principal_deps=lambda caller: {"customer_id": caller.id},
            deps_factory=lambda derived: CustomerContext(customer_id=derived["customer_id"]),
            usage_limits=UsageLimits(max_requests=12),
            approver_roles={APPROVER_ROLE},
            max_concurrency=16,
        )
    }


def build_app(agent: Agent | None = None) -> FastAPI:
    """The ASGI app, e.g. for `uvicorn` with several options of your own."""

    return create_app(
        build_systems(agent),
        authenticator=build_authenticator(),
        thread_scope="principal",
        rate_limit=RateLimit(30, per=60),
    )


@dataclass
class ShopDatabase:
    """Stand-in for the shop's own database: its users by session id."""

    sessions: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "s-c7": {"id": "c-7", "name": "Alice"},
            "s-c9": {"id": "c-9", "name": "Bob"},
            "s-rev": {"id": "rev-1", "name": "Rita", "roles": [APPROVER_ROLE]},
        }
    )
    connected: bool = False


SHOP_DB = ShopDatabase()


async def current_user(x_session: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """The shop's own login (in a real app: a cookie, an OAuth token, ...)."""

    user = SHOP_DB.sessions.get(x_session or "")
    if user is None:
        raise HTTPException(status_code=401, detail="Please log in")
    return user


async def agent_caller(user: Annotated[dict[str, Any], Depends(current_user)]) -> Principal:
    """Who calls the desk: the logged-in user, as an awa `Principal`."""

    return Principal(
        user["id"],
        tenant="shop",
        roles=set(user.get("roles", ())),
        attributes={"name": user["name"]},
    )


@asynccontextmanager
async def shop_database(app: FastAPI) -> AsyncIterator[None]:
    """The shop's own lifespan: connect on startup, disconnect on shutdown."""

    SHOP_DB.connected = True
    try:
        yield
    finally:
        SHOP_DB.connected = False


def build_shop_app(agent: Agent | None = None) -> FastAPI:
    """The shop's app with its own `/orders` route and the refunds desk under `/ai`."""

    agents = create_router(build_systems(agent), auth=agent_caller, thread_scope="principal")
    app = FastAPI(title="Shop", lifespan=router_lifespan(agents, lifespan=shop_database))

    @app.get("/orders")
    async def orders(user: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
        mine = [{"order_id": key, **order} for key, order in ORDERS.items()]
        return {"orders": [order for order in mine if order["customer"] == user["id"]]}

    app.include_router(agents, prefix="/ai")
    return app


def main() -> None:
    require_model_credentials()
    if "--shop" in sys.argv[1:]:
        import uvicorn

        print("Serving the shop on http://127.0.0.1:8000 (desk under /ai/agents, docs at /docs)")
        uvicorn.run(build_shop_app(), host="127.0.0.1", port=8000)
        return
    print("Serving the refunds desk on http://127.0.0.1:8000 (docs at /docs)")
    serve(
        build_systems(),
        port=8000,
        authenticator=build_authenticator(),
        thread_scope="principal",
        rate_limit=RateLimit(30, per=60),
    )


if __name__ == "__main__":
    main()
