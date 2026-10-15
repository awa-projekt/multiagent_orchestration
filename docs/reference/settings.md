# Settings

<!-- Generated from the settings registry by scripts/gen_settings.py: do not edit. -->

Every setting awa reads, with its environment variable and default, generated from the
code.

```console
awa env list                          # every setting, its value and where it comes from
awa env set AWA_TRACE_LEVEL=full      # write one into the project .env
```

A value is taken from the process environment, else from the project `.env`, else the
default ([Configuration](../develop/configuration.md)). awa never shows a secret value.
A setting marked *confirm* decides where requests go or switches a protection:
`awa env set` needs `--confirm` for it.

## Model profiles

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_DEFAULT_MODEL` | Model of the `default` profile (`provider:model`). Unset: chosen from the provider whose credentials are set. | text (`anthropic:claude-sonnet-5-5`) | awa.models |
| `AWA_FAST_MODEL` | Model of the `fast` profile. Unset: chosen from the provider whose credentials are set. | text (`anthropic:claude-haiku-5-5`) | awa.models |
| `AWA_STRONG_MODEL` | Model of the `strong` profile. Unset: chosen from the provider whose credentials are set. | text (`anthropic:claude-opus-5-5`) | awa.models |
| `AWA_OPENAI_RESPONSES_API` | Send OpenAI and Azure OpenAI reasoning models that get an effort (judge and reviewer `low`, worker `medium`) through the Responses API, the API that takes an effort together with tools, with `store=False` (no response kept by the provider; the reasoning travels encrypted). `false`: Chat Completions without an effort. A model's own `use_responses_api` or `store` setting wins. | `true`, `false` (`false`) | awa.models |
| `AWA_VISION_MODEL` | Model that reads scanned PDF pages (a profile or `provider:model`, e.g. `fast`). Unset: the `default` profile; the workspace `ocr` option overrides it. | text | vision OCR of scanned PDF pages, workspace connector |

## Anthropic

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | API key for `anthropic:` models (read by the `anthropic` SDK too). | secret | awa.models |
| `ANTHROPIC_AUTH_TOKEN` | Bearer token used instead of the API key (read by the Anthropic SDK). | secret | - |
| `ANTHROPIC_BASE_URL` | Another address for the Anthropic API, for example a gateway (*confirm*; read by the `anthropic` SDK too). | text | - |
| `ANTHROPIC_CUSTOM_HEADERS` | Extra HTTP headers the Anthropic SDK sends (*confirm*). | secret | - |

## OpenAI

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | API key for `openai:` models (read by the `openai` SDK too). | secret | awa.models |
| `OPENAI_ORG_ID` | OpenAI organization id (read by the `openai` SDK too). | text | - |
| `OPENAI_PROJECT_ID` | OpenAI project id (read by the `openai` SDK too). | text | - |
| `OPENAI_BASE_URL` | Another address for the OpenAI API, for example a gateway (*confirm*; read by the `openai` SDK too). | text | - |
| `OPENAI_API_BASE` | Older name of OPENAI_BASE_URL, still read by LangChain (*confirm*; deprecated: use OPENAI_BASE_URL; read by the `openai` SDK too). | text | - |
| `OPENAI_CUSTOM_HEADERS` | Extra HTTP headers the OpenAI SDK sends (*confirm*). | secret | - |

## Azure OpenAI

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AZURE_OPENAI_API_KEY` | API key for `azure_openai:` models (with AZURE_OPENAI_AUTH=key). | secret | awa.models |
| `AZURE_OPENAI_ENDPOINT` | The Azure OpenAI resource, for example `https://<resource>.openai.azure.com/` (*confirm*). | text | awa.models |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | The deployment every `azure_openai:` model uses (also read as `AZURE_OPENAI_DEPLOYMENT`). | text | awa.models |
| `AZURE_OPENAI_DEPLOYMENT_FAST` | Deployment of the `fast` profile when Azure is chosen automatically (default: AZURE_OPENAI_DEPLOYMENT_NAME). | text | awa.models |
| `AZURE_OPENAI_DEPLOYMENT_STRONG` | Deployment of the `strong` profile when Azure is chosen automatically (default: AZURE_OPENAI_DEPLOYMENT_NAME). | text | awa.models |
| `OPENAI_API_VERSION` | A dated Azure OpenAI API version. Unset: the v1 API, which needs none. | text | awa.models |
| `AZURE_OPENAI_AUTH` | `key` (the API key) or `entra` (a Microsoft Entra ID service principal). | `key`, `entra` (`key`) | awa.models |

## Microsoft Entra ID and Microsoft 365 (Outlook, SharePoint and Excel, Teams)

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AZURE_TENANT_ID` | Directory (tenant) ID of the one app registration (Entra admin center > App registrations > Overview), also for Azure OpenAI with AZURE_OPENAI_AUTH=entra. Permission: none itself; each service below names its own. | text | awa.models, connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `AZURE_CLIENT_ID` | Application (client) ID (a GUID) of that app. Permission: none itself; its API permissions are listed per service below. Check everything with `awa check microsoft`. Finding people (`awa people`, role: recipients): application User.Read.All with admin consent. | text | awa.models, connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `AZURE_CLIENT_SECRET` | A client secret of the app: Certificates & secrets > Client secrets, copy the VALUE, not the Secret ID. Permission: none itself. Not needed with a certificate. | secret | awa.models, connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `AZURE_CLIENT_CERTIFICATE_PATH` | Recommended instead of a secret: a PEM file with the private key and the certificate, or a .pfx/.p12 file, whose public part is uploaded under Certificates & secrets > Certificates; it wins over AZURE_CLIENT_SECRET. Permission: none itself. Needs pip install "awa-agents[m365]". | text | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `AZURE_CLIENT_CERTIFICATE_PASSWORD` | The password of that key file, if it has one. Permission: none. | secret | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `AZURE_AUTHORITY_HOST` | Entra sign-in host, only for sovereign clouds or a local test server (default `https://login.microsoftonline.com`). Permission: none (*confirm*). | text | awa.models, connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `MS_GRAPH_URL` | Microsoft Graph host, only for sovereign clouds or a local test server (default `https://graph.microsoft.com`). Permission: none (*confirm*). | text | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `MS_MAILBOX` | Outlook: the ONE mailbox the process works in (a shared mailbox works too). Permission: application Mail.ReadWrite (plus Mail.Send only to send), limited to this mailbox with Exchange RBAC for Applications; then remove the tenant-wide Mail.* consent. | text | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `MS_SITE_URL` | SharePoint and Excel: the ONE site, such as `https://company.sharepoint.com/sites/sales`. Permission: application Sites.Selected, then an administrator grants 'read' or 'write' on this site. | text | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `MS_CHECK_WORKBOOK` | awa check: the workbook (path in the site's library) the Excel check reads (default: the first .xlsx at the top). Permission: as MS_SITE_URL. | text | awa check |
| `TEAMS_WEBHOOK_URL` | Teams: a Workflows webhook (Teams > channel menu > Workflows > 'Send webhook alerts to a channel'; the whole link with sig=). Permission: none in Entra; the link itself is the secret. | secret | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `TEAMS_TEAM_ID` | Teams as a signed-in user (`awa connect microsoft --teams`): the ONE team the agents may use (the groupId= part of the team's link). Permission: delegated ChannelMessage.Send, ChannelMessage.Read.All and User.Read; the user is a member. | text | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `TEAMS_CHANNEL_ID` | The ONE channel of that team (19:...@thread.tacv2, from the channel's link). Permission: as TEAMS_TEAM_ID. | text | connectors outlook, sharepoint, teams, microsoft_graph, awa check, awa people |
| `AWA_CHECK_NEGATIVE_MAILBOX` | awa check: a mailbox the app must NOT be able to read; the check expects 403 (empty skips the negative test). Permission: none. | text | awa check |
| `AWA_CHECK_ALLOW_POST` | awa check: `teams` lets it post ONE test card; empty: it never posts. Permission: as TEAMS_WEBHOOK_URL or the Teams user. | `teams` | awa check |

## Other model providers

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `GOOGLE_API_KEY` | API key for `google_genai:` models. | secret | awa.models |
| `GEMINI_API_KEY` | API key for `google_genai:` models when GOOGLE_API_KEY is unset. | secret | awa.models |
| `MISTRAL_API_KEY` | API key for `mistralai:` models. | secret | awa.models |
| `GROQ_API_KEY` | API key for `groq:` models. | secret | awa.models |
| `OLLAMA_HOST` | Where Ollama listens, for example `http://localhost:11434` (`ollama:` models) (*confirm*). | text | awa.models |

## Storage and runs

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_POSTGRES_DSN` | PostgreSQL database of runs, queues, checkpoints and stores (postgresql://user:password@host:5432/db). Unset: files next to the blueprint. | secret | awa serve, awa worker, awa runs, awa.persistence |
| `AWA_POSTGRES_ADMIN_DSN` | Direct PostgreSQL connection (not through PgBouncer) for setup locks. Unset: AWA_POSTGRES_DSN. | secret | awa serve, awa worker, awa store |
| `AWA_POSTGRES_POOL_MAX` | Connections of the one PostgreSQL pool each process shares. | a number (`10`) | awa serve, awa worker |
| `AWA_POSTGRES_POOLER` | `pgbouncer` when connections go through PgBouncer's transaction pooling (then no prepared statements). | `none`, `pgbouncer` (`none`) | awa serve, awa worker |
| `AWA_DATA_BACKEND` | Where runs, steps and results are kept: `sqlite` (.awa/awa.db) or `postgres`. Unset: `postgres` when AWA_POSTGRES_DSN is set. `awa store use` changes it. | `sqlite`, `postgres` | awa serve, awa worker, awa runs, awa store |
| `AWA_CAPTURE_CONTENT` | Keep prompts, answers and tool bodies of process runs (read only by the auditor role). Off: only their length. | `true`, `false` (`false`) | awa serve, awa worker |
| `AWA_RUN_RETENTION_DAYS` | Days a finished process run is kept before `awa store sweep` deletes it. Unset: kept until deleted. | a number | awa store sweep |
| `AWA_PSEUDONYM_KEY` | Secret that links people to their runs as an HMAC, for erasure per person (written into .env on first local use). | secret | awa serve, awa worker, awa store erase |
| `AWA_PSEUDONYM_KEY_ID` | Version of AWA_PSEUDONYM_KEY, kept with each link. | text (`1`) | awa serve, awa worker |
| `AWA_CHECKPOINTER_BACKEND` | Checkpointer of `awa.persistence.open_checkpointer` (library use; the CLI chooses its own). | `memory`, `sqlite`, `postgres` (`memory`) | awa.persistence (library) |
| `AWA_CHECKPOINTER_PATH` | File of the `sqlite` checkpointer (default .awa/checkpoints.db). | text | awa.persistence (library) |
| `AWA_ARTIFACT_BACKEND` | Artifact repository of `awa.persistence.open_artifact_repository`. | `memory`, `file`, `postgres` (`memory`) | awa.persistence (library) |
| `AWA_ARTIFACT_DIR` | Folder of the `file` artifact backend (default .awa_artifacts). | text | awa.persistence (library) |
| `AWA_STORE_BACKEND` | Store of `awa.persistence.open_store`. | `memory`, `postgres` (`memory`) | awa.persistence (library) |

## Automations and triggers

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_TRACE_LEVEL` | How much each process run's trace records (default summary; `full` keeps message texts). | `off`, `summary`, `full` | awa worker, awa serve, awa.automation |
| `AWA_IMAP_USER` | Default user name of a mailbox trigger without `username_env`. | text | mailbox trigger |
| `AWA_IMAP_PASSWORD` | Default password (an app password) of a mailbox trigger without `password_env`. | secret | mailbox trigger |
| `AWA_HOOK_SECRET` | Signing secret of job callbacks, when a callback names it in `secret_env`. | secret | awa.server.jobs (examples) |

## Server

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_API_KEY` | API key callers of `awa serve --api-key-env AWA_API_KEY` must send. | secret | awa serve --api-key-env |
| `AWA_MCP_API_KEY` | API key callers of `awa mcp serve --api-key-env AWA_MCP_API_KEY` must send. | secret | awa mcp serve --api-key-env |
| `AWA_ALLOWED_HOSTS` | Extra Host names (comma-separated) `awa serve` answers besides its own; `--allowed-host` replaces them (*confirm*). | text | awa serve --allowed-host |
| `AWA_LOG_LEVEL` | Log level an application reads as `Settings.log_level`; awa itself sets no log level. | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (`INFO`) | your application (Settings.log_level) |
| `AWA_ENV` | Deployment label, attached as the `env:<env>` trace tag; changes no behavior. | `local`, `test`, `staging`, `production` (`local`) | trace tags (library) |

## Tracing and observability

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `LANGSMITH_TRACING` | Send LangChain runs to LangSmith (read by the `langsmith` SDK too). | `true`, `false` (`false`) | LangChain |
| `LANGSMITH_API_KEY` | LangSmith API key (read by the `langsmith` SDK too). | secret | - |
| `LANGSMITH_PROJECT` | LangSmith project name (read by the `langsmith` SDK too). | text | - |
| `LANGSMITH_ENDPOINT` | Another LangSmith server (*confirm*; read by the `langsmith` SDK too). | text | - |
| `AWA_LANGFUSE_TRACING` | Send every run of the awa commands to Langfuse (needs the `langfuse` extra). | `true`, `false` (`false`) | awa CLI commands that run systems |
| `AWA_LANGFUSE_STRICT` | Stop a command when Langfuse tracing cannot start (off: warn once, run without it). | `true`, `false` (`false`) | Langfuse tracing |
| `AWA_TRACE_EXPORT_TIMEOUT` | Seconds one span export or score request to Langfuse may take; a failed one is tried again with backoff, then its spans are dropped with one warning. | a number (`30.0`) | Langfuse tracing |
| `AWA_LANGFUSE_CAPTURE_CONTENT` | Also send prompts, replies and tool data, redacted (off: names, ids, tokens, cost, timing). | `true`, `false` (`false`) | Langfuse tracing |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key. | secret | Langfuse tracing |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key. | secret | Langfuse tracing |
| `LANGFUSE_BASE_URL` | Langfuse server (default: Langfuse Cloud, EU) (*confirm*; also read as `LANGFUSE_HOST`). | text | Langfuse tracing |
| `LANGFUSE_RELEASE` | Release shown on every Langfuse trace, for example a git tag. | text | Langfuse tracing |
| `LANGFUSE_TRACING_ENVIRONMENT` | Langfuse environment of every trace (`development`, `production`, ...). | text | Langfuse tracing |

## Workspace

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_WORKSPACE_DIR` | The workspace folder of the `workspace` connector (default data). | text | workspace connector |
| `AWA_WORKSPACE_DB` | SQLite file in the workspace folder. | text | workspace connector |

## Caching

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_CACHE_ENABLED` | Master switch of every awa cache; the AWA_CACHE_* values win over a blueprint's `defaults.cache`. | `true`, `false` | awa.runtime.cache |
| `AWA_CACHE_PROMPT` | Provider prompt caching: `auto`, `off` or `1h`. | `auto`, `off`, `1h` | awa.runtime.cache |
| `AWA_CACHE_PROMPT_PAD` | Lengthen an OpenAI/Azure prompt prefix that is just short of the 1,024-token caching minimum, so repeated calls read it from the cache (default on). | `true`, `false` | awa.runtime.cache |
| `AWA_CACHE_TOOLS` | Reuse the results of read-only tools. | `true`, `false` | awa.runtime.cache |
| `AWA_CACHE_TOOLS_TTL` | Seconds a read-only tool result is kept across runs (unset: for one run). | a number | awa.runtime.cache |
| `AWA_CACHE_STEPS` | Reuse deterministic process steps. | `true`, `false` | awa.runtime.cache |
| `AWA_CACHE_STORE` | Where the caches are kept: `memory` or `sqlite`. | `memory`, `sqlite` | awa.runtime.cache |
| `AWA_CACHE_PATH` | File of the `sqlite` cache store (default .awa/cache.db). | text | awa.runtime.cache |
| `AWA_CACHE_SALT` | Project secret that makes the cache keys sent to a provider opaque and stable (`awa cache salt` writes one). | secret | awa.runtime.cache, awa cache salt |

## Run limits and loop protection

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_RUN_MAX_REQUESTS` | Model calls one run tree may make when its `UsageLimits` set no `max_requests` (a whole number; a ceiling cannot be switched off, only raised). | a number (`400`) | awa.runtime.termination |
| `AWA_RUN_MAX_TOOL_CALLS` | Tool calls one run tree may make when its `UsageLimits` set no `max_tool_calls`. | a number (`200`) | awa.runtime.termination |
| `AWA_RUN_MAX_TOKENS` | Input plus output tokens one run tree may use when its `UsageLimits` set no `max_total_tokens`. | a number (`1000000`) | awa.runtime.termination |
| `AWA_RUN_MAX_SECONDS` | Wall-clock seconds of a run tree that has no `timeout` and no `UsageLimits.max_duration`. | a number (`3600`) | awa.runtime.termination |
| `AWA_RUN_MAX_DEPTH` | How deep runs may nest (agents delegating to or calling agents); the top-level run has depth 0. | a number (`16`) | awa.runtime.termination |
| `AWA_DELEGATION_MAX_REQUESTS` | Model calls one team delegation may make, its own delegations included, when `TeamLimits.delegation_limits` is not set; beyond it only that delegation stops. | a number (`50`) | awa.team |
| `AWA_MAX_IDENTICAL_CALLS` | Identical tool calls (same tool, arguments and result) in one agent run before the run is stopped for making no progress (at least 2). | a number (`5`) | awa.runtime.termination |
| `AWA_MAX_TOOL_ERRORS` | Failures in a row of one tool in one agent run before the run is stopped for making no progress. | a number (`5`) | awa.runtime.termination |
| `AWA_MAX_IDENTICAL_OUTPUTS` | Identical results in a row after which a `Loop` stops, and identical drafts after which a `Checked` revision stops (at least 2). | a number (`2`) | awa.workflows |
| `AWA_MAX_PAGES_PER_CALL` | Pages one connector list call reads before it returns a `next_page` to continue. | a number (`20`) | awa.connectors |
| `AWA_TOOL_PROFILE` | Defaults of the tool layer: `cost` (results above 6,000 characters saved aside with a preview, clearing from 10,000 tokens) or `quality` (results cut at 40,000 characters, clearing from 40,000 tokens). | `cost`, `quality` (`cost`) | awa.tools.offload, awa.memory.context_editing |
| `AWA_TOOL_MAX_OUTPUT_CHARS` | Longest text result a tool hands to the model; unset: the profile's value. In the `cost` profile a longer result is saved aside and the model gets a preview and a reference it can read on with `read_tool_result`. | a number | awa.tools.offload |
| `AWA_TOOL_CIRCUIT_BREAKER` | Failures in a row after which a tool answers 'unavailable in this run' without being called (0: never). | a number (`3`) | awa.tools.resilience |
| `AWA_TOOL_RETRIES` | Retries, with jittered backoff, of a call of a tool that does not act outside the agent and raised a network error. | a number (`2`) | awa.tools.resilience |
| `AWA_CONTEXT_EDITING` | Replace older tool results with a short note naming the call in what the model sees once a request grows past `AWA_CONTEXT_EDITING_TRIGGER` (the run's state keeps them). | `true`, `false` (`true`) | awa.memory.context_editing |
| `AWA_CONTEXT_EDITING_TRIGGER` | Approximate request size in tokens above which older tool results are cleared; unset: the profile's value (10,000 or 40,000). | a number | awa.memory.context_editing |
| `AWA_CONTEXT_EDITING_KEEP` | Most recent tool results that are never cleared (up to keep + step - 1 stay); unset: the profile's value (3 or 4). | a number | awa.memory.context_editing |
| `AWA_CONTEXT_EDITING_STEP` | Tool results cleared together, so the cached prompt prefix changes only every few calls. | a number (`4`) | awa.memory.context_editing |
| `AWA_CONTEXT_EDITING_CLEAR_AT_LEAST` | Fewest tokens a clearing step must free, else nothing is cleared (a clearing costs one prompt-cache miss); unset: the profile's value (3,000 or 0). | a number | awa.memory.context_editing |
| `AWA_MAX_LEASE_LOSSES` | Lost worker leases in a row (a worker crashed or hung on the run) after which an automation run is dead-lettered instead of taken over again. | a number (`3`) | awa.automation |

## Local services (awa setup)

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_SETUP_DB_PORT` | Port of the local PostgreSQL on 127.0.0.1. | a number | awa setup services |
| `AWA_SETUP_DB_PASSWORD` | Password of the local PostgreSQL (generated). | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_PORT` | Port of the local Langfuse web UI on 127.0.0.1. | a number | awa setup services |
| `AWA_SETUP_LANGFUSE_ADMIN_EMAIL` | Sign-in email of the local Langfuse. | text | awa setup services |
| `AWA_SETUP_LANGFUSE_ADMIN_PASSWORD` | Sign-in password of the local Langfuse. | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_NEXTAUTH_SECRET` | Session secret of the local Langfuse. | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_SALT` | Salt of the local Langfuse. | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_ENCRYPTION_KEY` | Encryption key of the local Langfuse. | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_DB_PASSWORD` | Password of the local Langfuse's PostgreSQL. | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_CLICKHOUSE_PASSWORD` | Password of its ClickHouse. | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_REDIS_PASSWORD` | Password of its Redis. | secret | awa setup services |
| `AWA_SETUP_LANGFUSE_MINIO_PASSWORD` | Password of its MinIO object store. | secret | awa setup services |

## General

| Variable | What it does | Values (default) | Used by |
| --- | --- | --- | --- |
| `AWA_PROJECT_DIR` | The project folder (default: found from the current folder, see --project). | text | awa CLI |
| `AWA_STRICT_SETTINGS` | Stop with exit code 78 on an unknown AWA_* name or an unconfirmed security switch. | `true`, `false` (`false`) | awa CLI |

## Names your project declares

A blueprint names further variables in its `*_env` keys: a webhook trigger's
`secret_env`, a mailbox's `username_env` and `password_env`, `api_key_env`, and the
variables of your connectors. awa treats them as secret, reads them from the project
`.env` like the settings above, and `awa doctor` lists them.
