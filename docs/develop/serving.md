# Serving

Serving puts a [system](../reference/glossary.md#system) behind an HTTP API. Other software, such
as a portal or a back-office tool, can then start runs, follow them and answer their approvals.
You need it when the process runs on a server instead of inside your own Python program.

For MCP clients, see [MCP](mcp.md#serve-a-system-over-mcp); for production, see
[Production](production.md).

!!! info "Preview"
    The server (`awa.server`, `awa serve`) and its HTTP API work and are tested, but their API may still change in a minor release ([Stability](../about/stability.md)).

## Serve a system and call it

Create `service.py` (it needs the `server` extra). It defines an agent, the callers with their
keys, and the app:

```python title="service.py"
from awa import Agent, tool
from awa.persistence import StateInMemorySaver
from awa.server import ApiKeyAuthenticator, Principal, RateLimit, ServedSystem, create_app
from awa.testing import FunctionChatModel, ModelCall, tool_call


@tool(approval="amount > 50")
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount:.2f} EUR for {order_id}"


def scripted(call: ModelCall):  # plays the model
    last = call.last_message
    if last.type == "tool":
        return "The refund is issued."
    if "refund" in str(last.content):
        return tool_call("issue_refund", {"order_id": "A-7", "amount": 80.0})
    return "Order 1002 shipped."


support = Agent("support", tools=[issue_refund], model=FunctionChatModel(scripted), checkpointer=StateInMemorySaver())
auth = ApiKeyAuthenticator(
    {
        "acme-portal": Principal("acme-portal", tenant="acme"),
        "acme-ops": Principal("acme-ops", tenant="acme", roles={"approver"}),
        "globex-app": Principal("globex-app", tenant="globex"),
    }
)
app = create_app(
    {"support": ServedSystem(support, approver_roles={"approver"})},
    authenticator=auth,
    rate_limit=RateLimit(3, per=60),
)
```

Start it with `uvicorn service:app --port 8000`. In a test, call it with a test client instead, as
`call_it.py` does:

```python title="call_it.py"
from starlette.testclient import TestClient

from service import app

client = TestClient(app)


def post(path, key=None, **body):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = client.post(path, json=body, headers=headers).json()
    return data.get("error", {}).get("code") or data["status"], data


runs = "/v1/systems/support/runs"
print(post(runs, input="Where is order 1002?")[0])
print(post(runs, "acme-portal", input="Where is order 1002?", thread_id="case-1")[0])
print(post(runs, "globex-app", input="Where is order 1002?", thread_id="case-1")[0])

status, paused = post(runs, "acme-portal", input="Please refund order A-7.")
thread = paused["thread_id"]
pending = client.get(f"/v1/systems/support/threads/{thread}/interrupts", headers={"Authorization": "Bearer acme-ops"})
print(status, pending.json()["interrupts"][0]["value"]["tool"])
resume = f"/v1/systems/support/threads/{thread}/resume"
print(post(resume, "acme-portal", decision={"action": "approve"})[0])
status, done = post(resume, "acme-ops", decision={"action": "approve"})
print(status, done["output"])
print(post(runs, "acme-portal", input="Where is order 1002?")[0])
```

```text title="call_it.out"
awa.authentication
completed
awa.thread_not_found
interrupted issue_refund
awa.permission_denied
completed The refund is issued.
awa.rate_limit_exceeded
```

The output lines, in order:

1. No key: 401.
2. A run of tenant `acme`: it completes.
3. The same thread id from `globex-app`, another organization: 404, as if it did not exist.
4. A refund over 50 EUR: the run pauses for approval.
5. A resume by `acme-portal`, which lacks the approver role: 403.
6. The resume by the approver `acme-ops`: the refund runs.
7. The fourth run or resume of `acme-portal` within a minute: 429.

`create_app` takes one system, a list, or a mapping of name to system. With one shared key,
`create_app(system, api_key=...)` is enough. `serve(systems, host=..., port=...)` runs the app with
uvicorn in one process; on a host other than this computer it needs `api_key` or an
`authenticator`, or `allow_unauthenticated=True`.

### From a blueprint

`awa serve` builds a [blueprint](../reference/glossary.md#blueprint) and serves it. It loads
`tools.py` and `skills/` next to the blueprint:

```console
awa serve examples/blueprints/support_triage.yaml --host 0.0.0.0 --port 8000 --api-key-env SUPPORT_API_KEY
```

The options most readers need:

| Option | Meaning |
| --- | --- |
| `--host`, `--port` | Interface (default `127.0.0.1`) and port (default `8000`). |
| `--api-key-env VAR`, `--api-keys-file FILE` | One bearer key from a variable, or several callers and tenants from a file of key digests. |
| `--approver-role ROLE` | The role needed to answer approvals (repeatable). |
| `--rate-limit N/SECONDS`, `--max-concurrency N` | Runs and resumes per caller, and runs in flight. |
| `--dsn DSN` | PostgreSQL for checkpoints and automation runs; without it they are kept in `.awa/` next to the blueprint. |

The [CLI reference](../reference/cli.md#awa-serve) lists the rest: more tools and skills
(`--tools`, `--skills`), thread scope (`--thread-scope`), body size (default 1 MiB), per-request
limits, extra host names, CORS origins, the OpenAPI document (`--docs`), the PostgreSQL schema and
store file, logging, and the shutdown grace (default 30 seconds).

Good to know:

- Serving on a non-local interface without authentication is refused. Add
  `--allow-unauthenticated` only if anyone who can reach the port may run the system.
- A blueprint with `triggers:` also serves its webhook and process-run endpoints and runs its
  automation. With `--dsn`, the schedules, file drops and mailboxes are left to `awa worker`
  ([Production](production.md)).
- For reload during development, give uvicorn a factory:
  `uvicorn app:create --factory --reload` with `def create(): return build_blueprint_app("blueprint.yaml")`.

## Endpoints

Every endpoint is under `/v1`. With an authenticator, all but `health` and `ready` need
credentials.

| Method and path | What it does |
| --- | --- |
| `GET /v1/health`, `GET /v1/ready` | Liveness; readiness (stores reachable, not draining). |
| `GET /v1/systems`, `GET /v1/systems/{name}` | The systems; one system's card, input and output schema, topology and diagram. |
| `POST /v1/systems/{name}/runs` | Run and return the result; `?stream=true` streams events, `?mode=async` answers 202 with a run id. |
| `POST /v1/systems/{name}/runs/stream` | The same run as a stream of events (as `?stream=true`). |
| `GET /v1/systems/{name}/threads` | The caller's threads. |
| `GET .../threads/{thread_id}/interrupts`, `POST .../threads/{thread_id}/resume` | Pending approvals, and their answer. |
| `GET /v1/runs`, `GET /v1/runs/{run_id}`, `DELETE /v1/runs/{run_id}` | Runs in flight or recently finished; status and result; cancel. |

### A run request and its answer

A run request takes these fields; unknown fields are refused:

- `input`: a prompt, or a list of user and assistant messages.
- `history`, `thread_id`, `usage_limits` and `deps`.

The answer is the `RunResult` as JSON. It has `status` (`completed` or `interrupted`), `output`,
`text`, `thread_id`, `interrupts`, `usage`, `run_id` and `request_id`.

- **Streaming.** `?stream=true` answers with Server-Sent Events, named after each event type. The
  stream ends with exactly one `run_completed` or a top-level `error` event. Idle streams get a
  ping every 15 seconds. A client that disconnects cancels its run. A slow client loses
  `text_delta` events first (`sse_overflow="drop"`), never the final result.
- **Async runs.** `?mode=async` answers 202 with `status_url`. `GET /v1/runs/{run_id}` returns the
  status and, when done, the result. The run id is the run's own, so events, traces and audit
  records carry it.
- **Timeouts and cancel.** `ServedSystem(run_timeout=...)` answers 504 when a run takes longer.
  `DELETE /v1/runs/{run_id}` stops the whole run tree. The client waiting on that run gets
  409, and the cancel is logged with the caller. The registry of runs is per process.
- **OpenAPI.** `/openapi.json` is served only with `docs=True`, and behind the key. Any language
  with an HTTP client can call the API, and the OpenAPI document generates typed clients.

### Approvals over HTTP

A run that pauses for an [approval](../reference/glossary.md#approval) answers
`"status": "interrupted"` with its `thread_id` and the pending `interrupts`. A reviewer then:

1. Reads `GET .../threads/{thread_id}/interrupts`. This comes from the checkpointer, so it
   survives restarts with a durable one.
2. Answers `POST .../resume` with `{"decision": {"action": "approve"}}`, `reject` with a note, or
   `edit` with arguments. Several pending approvals take a mapping of interrupt id to decision.

`approver_roles` limits who may answer; anyone else gets 403. The server writes the authenticated
reviewer into the decision and the audit log. It claims the paused state before it resumes, so a
second resume of the same pause gets 409 instead of running the approved action again.

### Automation endpoints

A blueprint with `triggers:` adds the endpoints of its automation. Like the run endpoints, they
need credentials when the server has an authenticator. The webhook is the exception: it checks the
trigger's own signature or token.

| Method and path | What it does |
| --- | --- |
| `GET /v1/automations` | The automations and their triggers. |
| `POST /v1/automations/{name}/webhooks/{trigger}` | Receive a webhook: 202 with the queued run, 200 for a duplicate or a delivery by `deliver_key`, 401 when the signature or token does not match, 413 for a too-large body. |
| `POST /v1/automations/{name}/events` | Submit an event (`source`, `payload`, optional `dedup_key` or `Idempotency-Key`): 202, 200 for a duplicate. |
| `POST /v1/automations/{name}/deliveries` | Deliver an event to runs waiting on its key: 200 when a run took it, 202 when it is kept. |
| `GET /v1/process-runs`, `GET /v1/process-runs/{run_id}` | The process runs of the caller's tenant (filter by `automation` and `status`), and one run. |
| `GET /v1/process-runs/{run_id}/steps` | The steps of a run in order, with attempts, results and approvals. |
| `POST /v1/process-runs/{run_id}/retry`, `.../cancel` | Retry a failed or dead-lettered run; cancel a run. |
| `POST /v1/process-runs/{run_id}/decision` | Answer the run's pending approval; with `--approver-role`, only callers with that role (403 otherwise). The caller is recorded as the reviewer. |

[Run automatically](../guide/run.md) shows a webhook end to end.

## Who may call and what they see

Every request is made by a [`Principal`](../reference/glossary.md#principal): `id`, `tenant`,
`roles` and `attributes`. The tenant is the organization the caller belongs to. An authenticator
produces the principal:

- `ApiKeyAuthenticator({key: principal})` keeps keys as SHA-256 digests and compares them in
  constant time.
- `ApiKeyAuthenticator.from_digests({sha256_hex: principal})` keeps even the digests out of your
  code. `awa serve --api-keys-file keys.json` reads digests or the names of key variables, never
  keys.
- `BearerTokenAuthenticator(verify)` hands the token to your function. Your function validates it
  (JWT, OIDC) and returns the principal.
- Anything else is a class with `async def authenticate(request)`.

### Threads

The server records who created each [thread](../reference/glossary.md#thread) and lets only the
right callers use it. Everyone else gets the same 404 as for a thread that does not exist.
`thread_scope` decides who the right callers are:

- `"tenant"` (default): anyone in the creator's tenant.
- `"principal"`: only the creator and the approvers.
- `"shared"`: anyone authenticated.

With an authenticator and a durable checkpointer, use `PostgresThreadIndex`. Ownership then
survives restarts and holds across workers. `create_app` refuses the in-memory index in that case,
unless you pass it on purpose.

### What a request may set

| `ServedSystem` option | Effect |
| --- | --- |
| `allowed_deps` | The `deps` keys a request may send; others answer 400. By default none. |
| `principal_deps` | Deps derived from the caller, for example `lambda caller: {"user_id": caller.id}`; a client cannot send these keys. When a reviewer resumes someone else's thread, the owner is known only by `id` and `tenant`, so derive deps from those. |
| `deps_factory` | Builds the run's deps from the accepted keys; a `ValueError` answers 400. |
| `usage_limits` | A ceiling for every run; a request can only tighten it. |
| `approver_roles` | Who may answer approvals. |
| `max_concurrency`, `queue_timeout`, `rate_limit` | Runs at once (503 beyond), waiting time, requests per caller (429). |
| `run_timeout` | Seconds per run (504 beyond). |
| `allow_system_messages` | Accept `system` messages from trusted internal callers; by default a request may contain user and plain assistant messages only, so it cannot replace the system prompt or forge tool results. |

Every served run gets `awa.request_id`, `awa.tenant` and `awa.actor` as run metadata. Tools read
them from the run context, and the audit log records them. Inside a tool,
`awa.server.current_principal()` returns the caller. Tools still check authorization themselves,
for example that an order belongs to the caller.

### Safe retries and errors

Send `Idempotency-Key: <key>` on `POST .../runs` to make retries safe:

- A repeat with the same key and body gets the stored response (`Idempotent-Replayed: true`).
- The same key with another body answers 422.
- A repeat while the first request still runs answers 409.

Use `PostgresIdempotencyCache` with several workers.

Errors are JSON: `{"error": {"type", "code", "message", "retryable", "request_id"}}`. `code` is
stable ([Exceptions](../reference/exceptions.md#error-codes)). Retry only when `retryable` is
true, and after `Retry-After` when it is sent. Unexpected errors are logged with their traceback
and answered with a generic 500.

## Mount the API in your FastAPI app

Use this when you already have a FastAPI app with its own login. `create_router` returns an
`APIRouter` with the same endpoints, and your login decides who the caller is:

```python title="mounted.py"
from fastapi import FastAPI, Header, HTTPException
from starlette.testclient import TestClient

from awa import Agent
from awa.server import Principal, create_router, router_lifespan
from awa.testing import FunctionChatModel

USERS = {"session-ana": ("ana", "acme")}  # stand-in for your own login


async def current_caller(x_session: str = Header()) -> Principal:
    if x_session not in USERS:
        raise HTTPException(status_code=401, detail="log in first")
    user, company = USERS[x_session]
    return Principal(user, tenant=company)


support = Agent("support", model=FunctionChatModel(lambda call: "Order 1002 shipped."))
agents = create_router({"support": support}, auth=current_caller)
app = FastAPI(lifespan=router_lifespan(agents))
app.include_router(agents, prefix="/ai")

client = TestClient(app)
reply = client.post("/ai/agents/systems/support/runs", json={"input": "Where is 1002?"}, headers={"X-Session": "session-ana"})
print(reply.status_code, reply.json()["output"])
print(client.post("/ai/agents/systems/support/runs", json={"input": "?"}, headers={"X-Session": "nobody"}).json())
```

```text title="mounted.out"
200 Order 1002 shipped.
{'detail': 'log in first'}
```

- `auth` is any FastAPI dependency that returns a `Principal`. Your `HTTPException` keeps your
  error format.
- The router's prefix (default `/agents`) and yours add up, so the example serves
  `/ai/agents/...`.
- `router_lifespan` drains runs in flight at shutdown, inside your own lifespan.
- CORS, body limits and security headers are your app's job.
- `create_automation_router` mounts the endpoints of automations the same way.
- The data endpoints are `create_app` only ([Data](data.md#the-data-endpoints)).

The complete example with a session login is `examples/10_serve_api.py` (`--shop`).

## Jobs on your own queue

Use this when you already run a job queue (Celery, RQ, Arq, SQS, a PostgreSQL table) and want its
workers to run your systems. `awa.server.jobs` turns a run into a JSON message any queue can
carry, and one function runs it in the worker:

```python title="jobs.py"
from awa import Agent
from awa.server.jobs import JobRegistry, JobSpec, execute_sync
from awa.testing import FunctionChatModel


def build_checker() -> Agent:
    return Agent("checker", model=FunctionChatModel(lambda call: "Invoice INV-2 is correct."))


registry = JobRegistry({"invoice-check": build_checker})  # the systems a worker offers
message = JobSpec(system="invoice-check", input="Check invoice INV-2.", idempotency_key="invoice-check:INV-2")
payload = message.model_dump(mode="json")  # what goes on your queue

result = execute_sync(payload, registry)  # in the worker
print(result.status, "|", result.text)
refused = execute_sync({"system": "payroll", "input": "Pay everyone."}, registry)
print(refused.status, "|", refused.error["code"], "| retryable:", refused.retryable)
```

```text title="jobs.out"
completed | Invoice INV-2 is correct.
failed | awa.system_not_found | retryable: False
```

- **Only known systems.** A message can only name a system of the worker's registry. Import paths
  are resolved only with `JobRegistry(allow_imports=[...])`.
- **The result.** `JobResult` is JSON too: `status` (`completed`, `interrupted`, `failed`,
  `cancelled`), the result in the shape of the HTTP API, and an `error` with `code` and
  `retryable`. A failing run does not raise; `result.raise_for_status()` hands failures to your
  queue's retry policy.
- **Redelivery.** `execute(..., idempotency_cache=PostgresIdempotencyCache(dsn))` answers a
  redelivered message from the stored result, without a second run.
- **Callbacks.** A `WebhookCallback(url=..., secret_env=...)` on the job posts the result. It is
  signed with `sha256=<HMAC of "<timestamp>.<body>">` in `X-Awa-Signature`. The receiver checks it
  with `verify_webhook(body, headers, secret)` and remembers `X-Awa-Delivery` ids. A redirect
  answer counts as a refusal and is not followed.
- **Approvals.** A run paused for an approval ends `interrupted` with a `thread_id`. The decision
  is another job with `decision` and that `thread_id`. With a PostgreSQL checkpointer, any worker
  can resume it.

awa's own queue is a PostgreSQL table (`PostgresEventQueue`, or `InMemoryEventQueue` in one
process). With it:

- Sign each message with `sign_webhook` and check it with `verify_webhook` before `execute`, so a
  tampered or expired message never runs.
- `nack(message_id, delay=...)` puts a retryable failure back. The queue dead-letters it after
  `max_attempts`.

## What can go wrong

| You see | Why | Fix |
| --- | --- | --- |
| 401 `awa.authentication` | No key, a wrong key, or another header than the authenticator reads | Send `Authorization: Bearer <key>` |
| 404 `awa.thread_not_found` for your own thread | Another tenant or principal, or the server restarted with the in-memory thread index | Use the creator's key; use `PostgresThreadIndex` and a durable checkpointer |
| 400 about `deps` | A key `allowed_deps` does not list, or one `principal_deps` sets | Remove it from the request |
| 403 `awa.permission_denied` | The caller lacks the approver role | Give the principal the role |
| 409 `awa.system_busy` | The thread is still running, or the pause was already answered | Retry later; read the thread |
| 429, 503 | Rate limit or `max_concurrency` reached, or the server is draining | Wait for `Retry-After` |

Reference: [Server](../reference/api/server.md#awa.server).
