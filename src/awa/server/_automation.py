"""HTTP endpoints of process automation: webhooks, events, deliveries and process runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from pydantic import BaseModel, Field

from awa._internal.logging import get_logger
from awa.automation import Automation, ProcessRun, TriggerEvent, Webhook
from awa.automation.automation import Submission
from awa.automation.templates import render
from awa.errors import (
    InvalidRequestError,
    OptionalDependencyError,
    PermissionDeniedError,
    RunNotFoundError,
    SystemNotFoundError,
    WebhookVerificationError,
)
from awa.server._auth import ANONYMOUS, Authenticator, Principal
from awa.server._config import API_PREFIX
from awa.server._web import TOO_LARGE, error_json, server_extra_message

try:
    from fastapi import APIRouter, Depends, Header, Query, Request
    from fastapi.responses import JSONResponse, Response
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from awa.server._router import AgentRoute, error_responses
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise OptionalDependencyError(server_extra_message()) from exc

_log = get_logger("server.automation")


class EventSubmission(BaseModel):
    """Body of `POST /v1/automations/{name}/events`."""

    payload: Any = Field(default_factory=dict, description="The event's data")
    dedup_key: str | None = Field(
        default=None, max_length=255, description="Business key; one run per key"
    )
    source: str = Field(default="api", max_length=100, description="Name of the event source")


class Delivery(BaseModel):
    """Body of `POST /v1/automations/{name}/deliveries`."""

    key: str = Field(min_length=1, max_length=500, description="Correlation key of the wait")
    payload: Any = Field(default=None, description="Returned by wait_for_event")
    require_waiting: bool = Field(
        default=False, description="404 instead of keeping the event when no run waits yet"
    )


class DecisionBody(BaseModel):
    """Body of `POST /v1/process-runs/{run_id}/decision`."""

    decision: Any = Field(description='For approvals {"action": "approve"} and so on')
    interrupt_id: str | None = None


class CancelBody(BaseModel):
    """Body of `POST /v1/process-runs/{run_id}/cancel`."""

    reason: str = Field(default="cancelled", max_length=500)


_REFUSED = "Webhook verification failed"


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The request body, or `None` once it exceeds `limit` (declared or streamed, chunked too)."""

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return None
    except StarletteHTTPException:  # the app's own body limit was hit first
        return None
    return bytes(body)


def _submitted(submission: Submission) -> JSONResponse:
    return JSONResponse(
        {**submission.run.summary(), "duplicate": not submission.created},
        status_code=202 if submission.created else 200,
    )


def create_automation_router(
    automations: Sequence[Automation] | Mapping[str, Automation],
    *,
    authenticator: Authenticator | None = None,
    operator_roles: Sequence[str] | None = None,
    approver_roles: Sequence[str] | None = None,
    prefix: str = API_PREFIX,
) -> APIRouter:
    """Build the automation endpoints as a FastAPI `APIRouter`.

    Under `/v1`: `POST automations/{name}/webhooks/{trigger}` (verified by the trigger's scheme,
    no bearer token), `POST automations/{name}/events` and `.../deliveries`, `GET automations`,
    `GET process-runs[/{run_id}[/steps]]` and `POST process-runs/{run_id}/retry|cancel|decision`.
    Every endpoint sees only the caller's tenant; a decision records the caller as reviewer.

    Args:
        authenticator: For every endpoint but the webhooks; default: the `create_app` app's,
            else anonymous access.
        operator_roles: Roles allowed to retry, cancel and (without `approver_roles`) decide
            runs; default: any caller the app authenticated.
        approver_roles: Roles allowed to answer a run's pending approval.

    Example:
        ```python
        app = create_app({"invoices": invoice_team}, authenticator=auth)
        app.include_router(create_automation_router([invoices_automation]))
        ```
    """

    served: dict[str, Automation] = (
        dict(automations)
        if isinstance(automations, Mapping)
        else {automation.name: automation for automation in automations}
    )
    roles = tuple(operator_roles or ())
    approvers = tuple(approver_roles or roles)
    operated = error_responses(400, 403, 409)
    router = APIRouter(
        prefix=prefix,
        tags=["automation"],
        route_class=AgentRoute,
        responses=error_responses(401, 404),
    )

    async def caller(request: Request) -> Principal:
        if authenticator is not None:
            principal = await authenticator.authenticate(request)
        else:
            server = getattr(request.app.state, "awa_server", None)
            principal = await server.authenticate(request) if server is not None else ANONYMOUS
        if not isinstance(principal, Principal):
            raise TypeError("authenticate must return a Principal")
        return principal

    authenticated = Depends(caller)

    def automation_named(name: str) -> Automation:
        automation = served.get(name)
        if automation is None:
            raise SystemNotFoundError(f"No automation {name!r}")
        return automation

    def visible(run: ProcessRun, principal: Principal) -> bool:
        # No tenant means the runs without tenant, never every tenant's runs.
        return run.tenant == principal.tenant

    async def own_runs(
        target: Automation, principal: Principal, status: list[str] | None, limit: int
    ) -> list[ProcessRun]:
        """The newest `limit` runs of `target` the caller may see.

        A store lists every tenant's runs for `tenant=None`, so for a caller without a tenant
        the window grows until it holds `limit` runs without tenant or the store has no more.
        """

        window = limit
        while True:
            found = await target.store.list(
                automation=target.name, status=status, tenant=principal.tenant, limit=window
            )
            mine = [run for run in found if visible(run, principal)]
            if len(mine) >= limit or len(found) < window:
                return mine[:limit]
            window *= 4

    async def find_run(run_id: str, principal: Principal) -> tuple[Automation, ProcessRun]:
        for automation in served.values():
            run = await automation.store.get(run_id)
            if run is not None and run.automation == automation.name and visible(run, principal):
                return automation, run
        raise RunNotFoundError(f"No process run {run_id!r}")

    def require_operator(principal: Principal, needed: tuple[str, ...] = roles) -> None:
        if needed and not principal.has_role(*needed):
            raise PermissionDeniedError(
                f"Operating process runs needs one of the roles {list(needed)}"
            )

    @router.get("/automations", summary="List the automations and their triggers")
    async def list_automations(principal: Principal = authenticated) -> Response:
        return JSONResponse(
            {
                "automations": [
                    {
                        "name": name,
                        "version": automation.version,
                        "triggers": [
                            trigger.describe()
                            if hasattr(trigger, "describe")
                            else {"name": trigger.name}
                            for trigger in automation.triggers
                        ],
                    }
                    for name, automation in served.items()
                ]
            }
        )

    @router.post(
        "/automations/{name}/webhooks/{trigger}",
        summary="Receive a signed webhook",
        status_code=202,
        responses={200: {"description": "A duplicate, or a delivery"}, **error_responses(413)},
    )
    async def webhook(name: str, trigger: str, request: Request) -> Response:
        try:
            automation = automation_named(name)
            source = next(
                (t for t in automation.triggers if t.name == trigger and isinstance(t, Webhook)),
                None,
            )
            if source is None:
                raise SystemNotFoundError(f"Automation {name!r} has no webhook {trigger!r}")
            body = await _read_capped(request, source.max_bytes)
            if body is None:
                return JSONResponse(error_json(TOO_LARGE), status_code=413)
            event = source.verify(
                body,
                dict(request.headers),
                now=automation.clock.now(),
                query=dict(request.query_params),
            )
            if source.deliver_key is not None:
                context = {"payload": event.payload, "event": event.model_dump(mode="json")}
                key = str(render(source.deliver_key, context, event))
                runs = await automation.deliver(key, event.payload)
                return JSONResponse(
                    {"key": key, "delivered": [run.id for run in runs], "kept": not runs},
                    status_code=200,
                )
            return _submitted(await automation.submit(event, trigger=source))
        except WebhookVerificationError as exc:
            # The reason (missing header, wrong signature, old timestamp) goes to the log only:
            # a sender without the secret learns nothing about which check failed.
            _log.warning("Webhook %s/%s refused: %s", name, trigger, exc)
            raise WebhookVerificationError(_REFUSED) from None

    @router.post(
        "/automations/{name}/events",
        summary="Submit an event",
        status_code=202,
        responses={200: {"description": "A duplicate event"}},
    )
    async def submit_event(
        name: str,
        body: EventSubmission,
        principal: Principal = authenticated,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=255)
        ] = None,
    ) -> Response:
        automation = automation_named(name)
        event = TriggerEvent(
            source=body.source,
            payload=body.payload,
            received_at=automation.clock.now(),
            dedup_key=body.dedup_key or idempotency_key,
            metadata={"actor": principal.id},
        )
        return _submitted(
            await automation.submit(event, actor=principal.id, tenant=principal.tenant)
        )

    @router.post(
        "/automations/{name}/deliveries",
        summary="Deliver an event to waiting runs",
        responses={202: {"description": "Kept: no run waits on the key"}},
    )
    async def deliver(name: str, body: Delivery, principal: Principal = authenticated) -> Response:
        automation = automation_named(name)
        # A caller of a tenant answers (and keeps events for) its tenant's runs only.
        runs = await automation.deliver(
            body.key,
            body.payload,
            actor=principal.id,
            require_waiting=body.require_waiting,
            tenant=principal.tenant,
        )
        return JSONResponse(
            {"key": body.key, "delivered": [run.id for run in runs], "kept": not runs},
            status_code=200 if runs else 202,
        )

    @router.get("/process-runs", summary="List process runs")
    async def list_runs(
        principal: Principal = authenticated,
        automation: str | None = None,
        status: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> Response:
        try:
            targets = [automation_named(automation)] if automation else list(served.values())
            runs: list[ProcessRun] = []
            for target in targets:
                runs.extend(await own_runs(target, principal, status, limit))
            runs.sort(key=lambda run: run.created_at, reverse=True)
            return JSONResponse({"runs": [run.summary() for run in runs[:limit]]})
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc

    @router.get("/process-runs/{run_id}", summary="Get a process run")
    async def get_run(run_id: str, principal: Principal = authenticated) -> Response:
        _, run = await find_run(run_id, principal)
        return JSONResponse(run.model_dump(mode="json", exclude={"payload"}))

    @router.get(
        "/process-runs/{run_id}/steps",
        summary="Get the steps of a process run in order",
        description="Each step by position, with its attempts (status, time, tokens, cost, the "
        "agent run id of its trace span), their results (structured output, tool calls, "
        "checks) and the step's approvals (reviewer, reason). Prompts and answers are named "
        "by reference only. 404 when the run is not the caller's tenant's.",
    )
    async def get_run_steps(run_id: str, principal: Principal = authenticated) -> Response:
        from awa.automation.run_view import run_scope, run_steps

        automation, run = await find_run(run_id, principal)
        if automation.run_log is None:
            return JSONResponse({"run_id": run.id, "recorded": False, "steps": []})
        scope = run_scope(run, principal=principal.id)
        return JSONResponse(await run_steps(automation.run_log, scope, run.id))

    @router.post(
        "/process-runs/{run_id}/retry",
        summary="Retry a failed or dead-lettered run",
        responses=operated,
    )
    async def retry_run(run_id: str, principal: Principal = authenticated) -> Response:
        require_operator(principal)
        automation, _ = await find_run(run_id, principal)
        run = await automation.retry(run_id, actor=principal.id)
        return JSONResponse(run.summary())

    @router.post(
        "/process-runs/{run_id}/cancel", summary="Cancel a process run", responses=operated
    )
    async def cancel_run(
        run_id: str, body: CancelBody, principal: Principal = authenticated
    ) -> Response:
        require_operator(principal)
        automation, _ = await find_run(run_id, principal)
        run = await automation.cancel(run_id, reason=body.reason, actor=principal.id)
        return JSONResponse(run.summary())

    @router.post(
        "/process-runs/{run_id}/decision",
        summary="Answer a run's pending approval",
        responses=operated,
    )
    async def decide(
        run_id: str, body: DecisionBody, principal: Principal = authenticated
    ) -> Response:
        require_operator(principal, approvers)
        automation, _ = await find_run(run_id, principal)
        run = await automation.decide(
            run_id, body.decision, interrupt_id=body.interrupt_id, actor=principal.id
        )
        return JSONResponse(run.summary())

    return router
