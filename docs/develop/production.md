# Production

This page shows how to run an awa project for real work: as a service that keeps running,
answers requests, fires its triggers and keeps its data safe. Start with one process on one
machine. Move to the Docker Compose setup when you need PostgreSQL, several processes or a TLS
edge in a container.

The HTTP API itself is on [Serving](serving.md). The safety controls are on [Safety](safety.md).

## Start with one process and SQLite

The simplest setup is one `awa serve` process on one machine. It answers the HTTP API, fires the
triggers and keeps runs, approvals and checkpoints in SQLite files in `.awa/` next to the
blueprint. There is nothing else to install.

1. Set the model key and an API key for your callers in the service's environment (for example
   `ANTHROPIC_API_KEY` and `AWA_API_KEY`). Keep them out of the blueprint.
2. Check the setup from the project folder. `awa doctor` names anything that is missing.
3. Start the server. It listens on `127.0.0.1:8000` and asks callers for the key as a bearer token.
4. Put a reverse proxy with TLS in front of it (see [Network](#network-proxies-and-offline-installs)).
5. Point your health checks at `/v1/health` and `/v1/ready`.
6. Back up `.awa/` regularly with `awa store backup`. It is safe while awa runs.

```console
awa doctor
awa serve myproject --api-key-env AWA_API_KEY --log-format json
awa store backup backups/2026-10-11 myproject
```

If the project needs no web API, only its triggers, run `awa worker myproject` instead.

Only one process runs the triggers of a local store: a second `awa serve` or `awa worker` on the
same `.awa/` refuses to start. When you need more processes or machines, move to PostgreSQL with
`awa store use postgres` ([The data store](#the-data-store)).

## Reference setup with Docker Compose

The `deploy` folder of the repository holds a starting point to adapt. It runs awa's API and worker
with PostgreSQL and a TLS edge, and Langfuse for tracing in a separate file. It was validated with
`docker compose config`, not load-tested.

Prepare it once:

1. Build awa's wheel with `uv build --wheel`. The image installs the one wheel in `dist/`.
2. Copy `deploy/.env.example` to `.env` and `deploy/awa.env.example` to `awa.env`, both in
   `deploy/`. Fill in every empty value: an empty secret stops the start.
3. Copy your project (the folder with `blueprint.yaml`) into `deploy/project/`.
4. Put `tls.crt` and `tls.key` into `deploy/certs/`. When your own CA signed that certificate, put
   the CA certificate into `deploy/ca/` as well, so awa trusts the Langfuse endpoint behind the
   edge.

Then start it and check it:

```console
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml -f deploy/compose.tracing.yaml up -d --build
docker compose -f deploy/compose.yaml --profile worker up -d
docker compose -f deploy/compose.yaml exec awa-serve awa-entrypoint awa doctor --check-postgres --check-langfuse
```

- The first `up` starts PostgreSQL, the API and the TLS edge. The second adds Langfuse. Langfuse
  needs at least 4 GB of memory for Docker.
- The `worker` profile starts `awa worker`. With PostgreSQL, the schedules, file drops, mailboxes
  and queues run there, not in the API.
- Run every `docker compose exec` command through `awa-entrypoint`, as above, so it trusts the same
  certificates.
- Only port 443 is published. The Langfuse UI is bound to `127.0.0.1:3000`.
- `stop_grace_period` (45 seconds) is above the drain timeout, so runs in flight can finish.

| File | Content |
| --- | --- |
| `deploy/Dockerfile` | awa installed from the one wheel in `dist/`, user id 10001 |
| `deploy/compose.yaml` | `postgres`, `awa-serve`, `caddy` (the TLS edge); profile `worker`: `awa-worker`; profile `mcp`: `awa-mcp` |
| `deploy/compose.tracing.yaml` | Langfuse with its PostgreSQL, ClickHouse, Redis and MinIO |
| `deploy/Caddyfile` | The API on 443, Langfuse for awa's span export on 8443 (internal) |
| `deploy/entrypoint.sh` | Adds the CA files of `deploy/ca/` to the bundle of every HTTP client awa uses |
| `deploy/.env.example`, `deploy/awa.env.example` | Values Compose substitutes; settings of the awa containers |
| `deploy/clickhouse-low-overhead.xml` | Switches ClickHouse's own diagnostic logs off, so a small machine keeps up |

## Health checks, logs and shutdown

Every server answers two probes and tags every response with a request id:

```python title="probes.py"
from starlette.testclient import TestClient

from awa import Agent
from awa.persistence import StateInMemorySaver
from awa.server import create_app
from awa.testing import FunctionChatModel

support = Agent("support", model=FunctionChatModel(lambda call: "Order 1002 shipped."), checkpointer=StateInMemorySaver())
client = TestClient(create_app({"support": support}))

print(client.get("/v1/health").json())
print(client.get("/v1/ready").json())
reply = client.get("/v1/systems/support/threads/case-9/interrupts", headers={"X-Request-ID": "trace-77"})
print(reply.status_code, reply.headers["X-Request-ID"])
print(reply.json())
```

```text title="probes.out"
{'status': 'ok'}
{'status': 'ready', 'checks': {'checkpointer:support': 'ok', 'thread_index': 'ok', 'idempotency': 'ok'}}
404 trace-77
{'error': {'type': 'ThreadNotFoundError', 'code': 'awa.thread_not_found', 'message': "System 'support' has no thread 'case-9'", 'retryable': False, 'request_id': 'trace-77'}}
```

- **Liveness.** `GET /v1/health` answers while the process runs.
- **Readiness.** `GET /v1/ready` checks every checkpointer (the store of conversation state, see
  the [glossary](../reference/glossary.md#checkpointer)), the thread index, the idempotency cache
  and your `readiness_checks`. It answers 503 with the failing checks, or with
  `"server": "draining"` during shutdown. The checks run at most once a second. On a server with
  authentication, only an admin sees the checkpointers by system name; other callers see them
  numbered.
- **Request ids.** Every response carries `X-Request-ID`: the client's own when it sends a valid
  one, otherwise a new `req_...`. The id is in run answers, error bodies and streams.
  `current_request_id()` returns it inside a run.
- **Logs.** `awa serve --log-format json` (or `RequestContextFilter` and `JsonLogFormatter`) writes
  one JSON object per line with `request_id`, `principal` and `tenant`, plus an access log
  (`awa.server.access`). Approval decisions, cancellations and the actions of the data endpoints go
  to `awa.server.audit`. `create_app(audit_log=AuditLog(sink))` also writes them to the hash-chained
  audit trail ([Observability](observability.md#audit-trail)).
- **Graceful drain.** On shutdown, new runs get 503 and `ready` reports `draining`. Runs in flight
  get `drain_timeout` seconds (default 30), then they are cancelled. `serve()` and `awa serve` drain
  on SIGTERM. Under your own uvicorn, set `--timeout-graceful-shutdown` above the drain timeout.

## Configuration

A value comes from the process environment, else the project's `.env`, else the default. In
containers, set everything as environment variables. The most important groups are below. Every
variable is in the generated [settings reference](../reference/settings.md);
[Configuration](configuration.md) has the rules.

| Group | Variables |
| --- | --- |
| Model (one provider) | `ANTHROPIC_API_KEY`, or the Azure OpenAI endpoint, deployment and key or Entra settings; `AWA_DEFAULT_MODEL`, `AWA_FAST_MODEL`, `AWA_STRONG_MODEL` set explicitly ([Models](models.md)) |
| Store | `AWA_POSTGRES_DSN`; `AWA_POSTGRES_ADMIN_DSN` (setup locks), `AWA_POSTGRES_POOL_MAX` (10), `AWA_POSTGRES_POOLER=pgbouncer` |
| People | `AWA_PSEUDONYM_KEY`, the same in every process |
| Server | the variable of `--api-key-env`, or `--api-keys-file` |
| Content and retention | `AWA_CAPTURE_CONTENT` (`false`), `AWA_TRACE_LEVEL` (`summary`), `AWA_RUN_RETENTION_DAYS` |
| Limits | `AWA_RUN_MAX_*`, `AWA_TOOL_PROFILE`, `AWA_TOOL_MAX_OUTPUT_CHARS` ([Safety](safety.md#limits)) |
| Tracing | `AWA_LANGFUSE_TRACING`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` ([Observability](observability.md#langfuse)) |
| Caching | `AWA_CACHE_SALT`, so prompt cache keys stay the same across processes |
| Strictness | `AWA_STRICT_SETTINGS=true` turns an unknown `AWA_*` name into an error |

**Secrets.** Every credential is a `SecretStr` and prints as `**********`. Keep secrets out of
blueprints and inject them from a secret store. `awa env set NAME` asks for a value hidden (or reads
`--stdin`). Settings that change where requests go need `--confirm`. `awa env list` shows each
setting with its source; for secrets it shows only whether they are set.

**Checks at start.** Before they bind a port or read files, `run`, `chat`, `eval`, `build`,
`worker` and `serve` check that every model has credentials, every variable the triggers read is
set, the `--api-key-env` variable is set and PostgreSQL answers. A missing part stops the command
with exit code 78. Your orchestrator should treat 78 as a configuration error and not retry it in
a tight loop. `awa doctor` checks the same and more; `--check-models`, `--check-postgres` and
`--check-langfuse` make live calls.

## The data store

awa keeps the runs of automations, their steps, attempts, results and approvals in one of two
stores:

- **SQLite** in `.awa/awa.db`: nothing to install, one machine.
- **PostgreSQL**: several workers or machines, PgBouncer.

| What | SQLite | PostgreSQL |
| --- | --- | --- |
| Process runs | `.awa/awa.db` (`process_runs`) | `process_runs` |
| Steps, attempts, results, approvals | `run_step`, `step_attempt`, `step_result`, `approval` | the same tables |
| Prompts and answers (only with `AWA_CAPTURE_CONTENT=true`) | `captured_content` | `captured_content` |
| Thread owners of a server, answered approvals | `server_threads`, `server_decisions` | the same |
| Conversation checkpoints | `.awa/checkpoints.db` | the checkpointer's tables |
| Run traces | `.awa/traces/<run>.a<N>.jsonl` | the same folder |

Every read and write names a tenant and a project. People are linked to their runs only as an HMAC
of their principal id (`AWA_PSEUDONYM_KEY`). With capture off (the default), the run log keeps the
length of prompts and answers, not their text. `awa runs show <run> --steps` and
`GET /v1/process-runs/{run_id}/steps` show the steps of a run.

```console
awa store status                                                       # where, schema, rows
awa store use postgres                                                 # move, verify, switch
awa store migrate --to postgres --dsn-env AWA_POSTGRES_DSN --dry-run   # count only
awa store backup backups/2026-10-11                                    # SQLite online backup
awa store sweep --days 365                                             # delete old finished runs
awa store erase --automation invoices --run proc_1a2b3c                # delete one run for good
```

- **Moving to PostgreSQL.** Stop the server first. `awa store use postgres` copies runs, steps,
  results, approvals, person links, thread owners and checkpoints. It compares the row counts and a
  SHA-256 per table, and only then writes `AWA_DATA_BACKEND=postgres` into `.env`. The SQLite files
  stay as a backup; `awa store use sqlite` goes back. You can resume an interrupted move. Different
  data under the same key stops it with `StoreConflictError`.
- **Schema upgrades.** Tables come from versioned SQL files per dialect. SQLite upgrades on open,
  after a backup into `.awa/backups/` (the newest three are kept). On PostgreSQL the first server or
  worker applies pending migrations under a lock; `awa store upgrade` does it by hand. A changed
  applied file stops the start with `SchemaVersionError`.
- **Backups.** Back up SQLite with `awa store backup` (the online backup API, safe while awa runs).
  Back up PostgreSQL with its own tools (`pg_dump --schema=awa --format=custom`, plus the
  checkpointer's tables). Back up the project folder (blueprint, `tools.py`, cases, `.awa/traces/`)
  like code plus data.
- **Deletion** is physical: SQLite runs with `secure_delete=ON`, PostgreSQL runs `VACUUM`. Backups
  keep the data until they are deleted. Erasing a person and retention for the other stores are on
  [Safety](safety.md#data-protection).

## Upgrades

1. Read the release notes of the new version. Build or get its wheel and record its checksum.
2. Back up the store.
3. Build the new image, stop the awa processes (they drain) and start the new ones. Schema
   migrations run as described [above](#the-data-store).
4. When the blueprint changed, `awa runs migrate <project> --dry-run` lists runs that wait under the
   older version. Without `--dry-run` it moves them.
5. Check with `awa doctor`, `/v1/ready` and one known run.

## Checklist before real work

- [ ] **Models.** Profiles are set explicitly; `ModelSettings(timeout=..., max_retries=...)` on the
      profiles that serve users; `awa doctor --check-models` passes in the target environment.
- [ ] **Limits.** `limits:` in every blueprint (with `prices:` for `max_cost`), `max_steps` that fit
      each agent, `TeamLimits` and `NetworkLimits` from the process, a timeout on every external tool
      and on every top-level run.
- [ ] **Approvals.** Every irreversible or costly tool is gated, with `expires_after` and a durable
      checkpointer; `approver_roles` set; `awa validate` reports no risky tool combination.
- [ ] **Side effects.** Tools that may be retried are idempotent; tools check authorization through
      `deps`, never through prompts.
- [ ] **Callers.** An authenticator on every reachable server; principals carry a tenant;
      `thread_scope` chosen; `principal_deps` supplies user ids; `allowed_deps` minimal.
- [ ] **Durable state.** PostgreSQL for checkpoints, thread index and idempotency with several
      workers; `thread_id`s stored with the business object they belong to.
- [ ] **Server.** Probes on `/v1/health` and `/v1/ready`; `drain_timeout` below the orchestrator's
      grace period; `run_timeout` below the proxy timeout (or `?mode=async`); `rate_limit` and
      `max_concurrency` sized to the provider's quota; TLS, HSTS and unbuffered streams at the edge;
      exact CORS origins; `docs=False` on a public API.
- [ ] **Data and files.** `awa data check <project>` passes in CI; masks chosen on purpose ([what the mask covers](../guide/files.md#what-the-mask-covers)); writes to
      `data/output/` approved; for uploads a durable `root`, `require_durable=True`, roles, quotas,
      retention and a `scanner`.
- [ ] **Observability.** JSON logs with request ids; an `AuditLog` with a durable sink and a
      scheduled `verify_chain`; alerts on `limit_exceeded` and `guardrail_blocked`; usage per agent
      recorded for cost attribution.
- [ ] **Privacy.** `subject_index=` on the server, `erase_subject` and `apply_retention` scheduled,
      content capture off unless the data release allows it.
- [ ] **Tests.** Real model requests blocked in the suite; every process path covered offline;
      `awa eval --baseline ... --min-pass-rate ...` before each release; `awa validate` in CI.

## Several processes and machines

You need this section only when one process is not enough.

### Processes and layout

| Component | Command | Listens on (default) | State |
| --- | --- | --- | --- |
| HTTP API | `awa serve <project>` | `127.0.0.1:8000` | checkpoints, run store, `.awa/traces/` |
| Worker | `awa worker <project>` | nothing | run store, checkpoints, `.awa/traces/` |
| MCP endpoint | `awa mcp serve <project> --transport http` | `127.0.0.1:8000`, path `/mcp` | as the HTTP API |
| Web app | `awa ui --project DIR` | `127.0.0.1:8765` | the project folder |
| Store | SQLite file or PostgreSQL | PostgreSQL `5432` | runs, steps, approvals, checkpoints |
| Tracing | self-hosted Langfuse (optional) | `3000` | Langfuse's own stores |

With a shared PostgreSQL store, run one or more `awa serve` and one or more `awa worker`. The
servers receive webhooks and events. The workers run schedules, file drops, mailboxes and queues;
each queued run goes to one worker.

A worker also executes queued runs, retries, takes over runs of crashed workers and sends
reminders. It claims a run with a lease (`--lease`, default 300 seconds) and renews it every third
of that time. A worker that cannot renew stops its own attempt before the lease ends, and the store
refuses outcomes of an older claim. `--no-triggers` only executes queued runs; `--once` handles
what is ready and exits.

### Several server processes

When you run the API in several processes (for example `uvicorn --workers 4`, or several
replicas), keep in mind:

- Each process has its own copy of every system, so in-memory team state (blackboard, task board,
  bus) is per process: keep it per run.
- Rate limits, the 409 and 503 checks and the registry of async runs are per process. Route
  `GET`/`DELETE /v1/runs/{run_id}` to the process that started the run, or poll the thread.
- Use a PostgreSQL checkpointer, `PostgresThreadIndex` and `PostgresIdempotencyCache`, opened in
  the app's lifespan. Serve the data endpoints (`data=`) from one process.
- Run servers on Linux. On Windows, PostgreSQL needs the selector event loop; the `awa` commands
  choose it themselves.

```python
import os
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from awa import Agent
from awa.server import PostgresIdempotencyCache, PostgresThreadIndex, create_app

dsn = os.environ["AWA_POSTGRES_DSN"]
pool = AsyncConnectionPool(dsn, open=False, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
checkpointer = AsyncPostgresSaver(pool)
thread_index = PostgresThreadIndex(dsn)
idempotency = PostgresIdempotencyCache(dsn)


@asynccontextmanager
async def lifespan(app):
    await pool.open()
    await checkpointer.setup()
    yield
    await thread_index.close()
    await idempotency.close()
    await pool.close()


support = Agent("support", checkpointer=checkpointer)
app = create_app({"support": support}, api_key=os.environ["AWA_API_KEY"], thread_index=thread_index,
                 idempotency_cache=idempotency, lifespan=lifespan)
```

Run it with `uvicorn service:app --workers 4 --timeout-graceful-shutdown 40` after
`pip install ".[server,postgres]"` in the awa folder.

## Network, proxies and offline installs

**Outbound hosts.** Allow outbound HTTPS to the hosts you use: the model provider
(`api.anthropic.com`, or your Azure OpenAI endpoint), `login.microsoftonline.com` for Entra
sign-in, `graph.microsoft.com` and your SharePoint hosts for Microsoft 365, `html.duckduckgo.com`
and `lite.duckduckgo.com` for the keyless web search, your Langfuse host, and the base URLs of the
blueprint's connectors.

**TLS at the edge.** Terminate TLS for the API at a reverse proxy and add HSTS there. Turn off
response buffering for `text/event-stream`, or streams arrive in one piece.

**Proxies.** The model SDKs use `HTTPS_PROXY` and `NO_PROXY`. awa's connectors and web tools ignore
them unless a connector sets `use_env_proxies: true`. The `awa` command line never exports proxy
variables from a project `.env`: set them in the service's environment.

**Certificate authorities.** httpx and urllib read `SSL_CERT_FILE`, requests reads
`REQUESTS_CA_BUNDLE`, the OTLP exporter reads `OTEL_EXPORTER_OTLP_CERTIFICATE`. Point all three at
one bundle with the public CAs plus your own. Never switch verification off.

**Private package index.** Set `UV_INDEX_URL` (uv) or `PIP_INDEX_URL` (pip). `UV_NATIVE_TLS=1`
trusts the operating system's store behind a TLS-inspecting proxy.

**Offline install.** For a machine without internet access, build a wheelhouse on a connected
machine with the same operating system, CPU architecture and Python version, and install from it:

```console
uv build --wheel -o wheelhouse
python -m pip download "wheelhouse/awa_agents-<version>-py3-none-any.whl[anthropic]" -d wheelhouse
uv pip install --offline --no-index --find-links wheelhouse "awa-agents[anthropic]"
```

## Troubleshooting

| You see | Cause | Fix |
| --- | --- | --- |
| Exit code 78 at start, `problem: ... -- fix: ...` | A model key, a trigger variable, the API key variable or the database is missing | Set what the line names; `awa doctor` lists it |
| `CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate` | The CA of an inspecting proxy or internal service is not in the bundle | Add it to the bundle ([Network](#network-proxies-and-offline-installs)) |
| `CERTIFICATE_VERIFY_FAILED ... Hostname mismatch` | A proxy answers for the model host with another certificate | Check `HTTPS_PROXY` and `NO_PROXY`; `openssl s_client -connect <host>:443 -servername <host>` shows who answers |
| `ready` answers 503 with `checkpointer:...: timeout` | PostgreSQL does not answer in time | Check the database and `AWA_POSTGRES_DSN` |
| `Another awa process ... already runs the triggers` | Two processes on one local store | One process with triggers, `awa worker --no-triggers`, or PostgreSQL |
| Runs stay `queued` | No worker runs, or every worker is at its concurrency | Start `awa worker`; check `--concurrency` |
| Runs end with 503 or `cancelled` when a pod stops | The run needed longer than `drain_timeout` | Raise it, or use `?mode=async` with a durable checkpointer |
| `GET /v1/runs/{run_id}` answers 404 on some requests | The run lives on another server process | Route to that process, or poll the thread |
| A stream arrives in one piece | A proxy buffers `text/event-stream` | Turn buffering off for that route |
| `SchemaVersionError` at start | A changed applied migration, or a newer awa's breaking change | Restore the matching version, or follow the release notes |
| `Filename too long` or an `ImportError` on Windows | A path over 260 characters | Install into a short folder, or enable long paths |
| Langfuse drops spans or answers slowly | ClickHouse is busy with its own logs | Mount `deploy/clickhouse-low-overhead.xml`, or raise `AWA_TRACE_EXPORT_TIMEOUT` |

Example: `examples/10_serve_api.py` serves a refund desk with per-customer keys, identity-derived
deps, an approver role and limits.
