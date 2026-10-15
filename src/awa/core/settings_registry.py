"""The settings registry: every environment variable awa reads, in one list.

`REGISTRY` feeds `awa env`, `awa doctor`, the `.env` loader, the typo check, `.env.example` and
the settings reference; `declared_env(project)` adds the `*_env` names a blueprint declares.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from awa.core._envparse import ENV_NAME

SettingType = Any
"""A type `pydantic.TypeAdapter` accepts: `str`, `bool`, `int` or a `Literal[...]`."""

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_DSN", "_CUSTOM_HEADERS")
_TARGET_SUFFIXES = ("_BASE_URL", "_ENDPOINT", "_HOST", "_CUSTOM_HEADERS", "_API_BASE")


@dataclass(frozen=True)
class SettingSpec:
    """One environment variable awa (or a provider SDK awa builds) reads.

    Attributes:
        name: The variable name.
        type: The value type, checked with `pydantic.TypeAdapter`.
        default: The value when unset (`None`: no value).
        secret: The value is never printed; names ending in `_KEY`, `_SECRET`, `_TOKEN`,
            `_PASSWORD`, `_DSN` or `_CUSTOM_HEADERS` are secret automatically.
        sensitive_target: The value says where requests go; changing it through `awa env set` or
            the server needs a confirmation.
        security: Turning it off weakens a protection.
        group: The settings group (`awa env list --group`).
        used_by: The parts of awa that read it.
        doc: One line for the settings reference.
        aliases: Other names it is read under.
        deprecated: What to use instead of an outdated name (shown in the settings reference).
        sdk: The third-party SDK that reads it itself (`openai`, `anthropic`, `langsmith`, ...).
        choices: The values it accepts, when there is a fixed set.
    """

    name: str
    type: SettingType = str
    default: Any = None
    secret: bool = False
    sensitive_target: bool = False
    security: bool = False
    group: str = "general"
    used_by: tuple[str, ...] = ()
    doc: str = ""
    aliases: tuple[str, ...] = ()
    deprecated: str | None = None
    sdk: str | None = None
    choices: tuple[str, ...] = ()

    @property
    def is_secret(self) -> bool:
        """Whether the value must never be shown (declared, or by its name)."""

        return self.secret or is_secret_name(self.name)

    @property
    def needs_confirmation(self) -> bool:
        """Whether changing it needs `--confirm` (a security switch or a request target)."""

        return self.security or self.sensitive_target or self.name.endswith(_TARGET_SUFFIXES)


def is_secret_name(name: str) -> bool:
    """Whether a variable name looks secret (`*_KEY`, `*_SECRET`, `*_TOKEN`, ...)."""

    upper = name.upper()
    return upper.endswith(_SECRET_SUFFIXES) or "PASSWORD" in upper


GROUPS: tuple[tuple[str, str], ...] = (
    ("models", "Model profiles"),
    ("anthropic", "Anthropic"),
    ("openai", "OpenAI"),
    ("azure", "Azure OpenAI"),
    ("microsoft", "Microsoft Entra ID and Microsoft 365 (Outlook, SharePoint and Excel, Teams)"),
    ("providers", "Other model providers"),
    ("storage", "Storage and runs"),
    ("automation", "Automations and triggers"),
    ("server", "Server"),
    ("tracing", "Tracing and observability"),
    ("workspace", "Workspace"),
    ("caching", "Caching"),
    ("limits", "Run limits and loop protection"),
    ("services", "Local services (awa setup)"),
    ("general", "General"),
)
"""The groups of the generated reference and `.env.example`, in order, with their titles."""


def _choices(*values: str) -> tuple[Any, tuple[str, ...]]:
    return Literal[values], values


def _s(name: str, doc: str, **fields: Any) -> SettingSpec:
    return SettingSpec(name, doc=doc, **fields)


def _group(group: str, *specs: SettingSpec) -> tuple[SettingSpec, ...]:
    return tuple(replace(item, group=group) for item in specs)


_BACKEND, _BACKENDS = _choices("memory", "sqlite", "postgres")
_TOOL_PROFILE, _TOOL_PROFILES = _choices("cost", "quality")
_ARTIFACTS, _ARTIFACT_CHOICES = _choices("memory", "file", "postgres")
_STORES, _STORE_CHOICES = _choices("memory", "postgres")
_ENVS, _ENV_CHOICES = _choices("local", "test", "staging", "production")
_LEVELS, _LEVEL_CHOICES = _choices("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_TRACE, _TRACE_CHOICES = _choices("off", "summary", "full")
_AUTH, _AUTH_CHOICES = _choices("key", "entra")
_CACHE_PROMPT, _CACHE_PROMPT_CHOICES = _choices("auto", "off", "1h")
_POOLER, _POOLER_CHOICES = _choices("none", "pgbouncer")
_DATA, _DATA_CHOICES = _choices("sqlite", "postgres")
_CACHE_STORE, _CACHE_STORES = _choices("memory", "sqlite")

_MODEL = ("awa.models",)
_CLI = ("awa CLI",)
_MS = ("connectors outlook, sharepoint, teams, microsoft_graph", "awa check", "awa people")
_ENTRA = (*_MODEL, *_MS)

_SPECS: tuple[SettingSpec, ...] = (
    *_group(
        "models",
        _s(
            "AWA_DEFAULT_MODEL",
            "Model of the `default` profile (`provider:model`). Unset: chosen from the "
            "provider whose credentials are set.",
            default="anthropic:claude-sonnet-5-5",
            used_by=_MODEL,
        ),
        _s(
            "AWA_FAST_MODEL",
            "Model of the `fast` profile. Unset: chosen from the provider whose credentials "
            "are set.",
            default="anthropic:claude-haiku-5-5",
            used_by=_MODEL,
        ),
        _s(
            "AWA_STRONG_MODEL",
            "Model of the `strong` profile. Unset: chosen from the provider whose credentials "
            "are set.",
            default="anthropic:claude-opus-5-5",
            used_by=_MODEL,
        ),
        _s(
            "AWA_OPENAI_RESPONSES_API",
            "Send OpenAI and Azure OpenAI reasoning models that get an effort (judge and "
            "reviewer `low`, worker `medium`) through the Responses API, the API that takes an "
            "effort together with tools, with `store=False` (no response kept by the provider; "
            "the reasoning travels encrypted). `false`: Chat Completions without an effort. A "
            "model's own `use_responses_api` or `store` setting wins.",
            type=bool,
            default=False,
            used_by=_MODEL,
        ),
        _s(
            "AWA_VISION_MODEL",
            "Model that reads scanned PDF pages (a profile or `provider:model`, e.g. `fast`). "
            "Unset: the `default` profile; the workspace `ocr` option overrides it.",
            used_by=("vision OCR of scanned PDF pages", "workspace connector"),
        ),
    ),
    *_group(
        "anthropic",
        _s(
            "ANTHROPIC_API_KEY", "API key for `anthropic:` models.", used_by=_MODEL, sdk="anthropic"
        ),
        _s(
            "ANTHROPIC_AUTH_TOKEN",
            "Bearer token used instead of the API key (read by the Anthropic SDK).",
            sdk="anthropic",
        ),
        _s(
            "ANTHROPIC_BASE_URL",
            "Another address for the Anthropic API, for example a gateway.",
            sdk="anthropic",
            sensitive_target=True,
        ),
        _s(
            "ANTHROPIC_CUSTOM_HEADERS",
            "Extra HTTP headers the Anthropic SDK sends.",
            sdk="anthropic",
        ),
    ),
    *_group(
        "openai",
        _s("OPENAI_API_KEY", "API key for `openai:` models.", used_by=_MODEL, sdk="openai"),
        _s("OPENAI_ORG_ID", "OpenAI organization id.", sdk="openai"),
        _s("OPENAI_PROJECT_ID", "OpenAI project id.", sdk="openai"),
        _s(
            "OPENAI_BASE_URL",
            "Another address for the OpenAI API, for example a gateway.",
            sdk="openai",
            sensitive_target=True,
        ),
        _s(
            "OPENAI_API_BASE",
            "Older name of OPENAI_BASE_URL, still read by LangChain.",
            sdk="openai",
            sensitive_target=True,
            deprecated="use OPENAI_BASE_URL",
        ),
        _s("OPENAI_CUSTOM_HEADERS", "Extra HTTP headers the OpenAI SDK sends.", sdk="openai"),
    ),
    *_group(
        "azure",
        _s(
            "AZURE_OPENAI_API_KEY",
            "API key for `azure_openai:` models (with AZURE_OPENAI_AUTH=key).",
            used_by=_MODEL,
        ),
        _s(
            "AZURE_OPENAI_ENDPOINT",
            "The Azure OpenAI resource, for example `https://<resource>.openai.azure.com/`.",
            used_by=_MODEL,
            sensitive_target=True,
        ),
        _s(
            "AZURE_OPENAI_DEPLOYMENT_NAME",
            "The deployment every `azure_openai:` model uses.",
            used_by=_MODEL,
            aliases=("AZURE_OPENAI_DEPLOYMENT",),
        ),
        _s(
            "AZURE_OPENAI_DEPLOYMENT_FAST",
            "Deployment of the `fast` profile when Azure is chosen automatically (default: "
            "AZURE_OPENAI_DEPLOYMENT_NAME).",
            used_by=_MODEL,
        ),
        _s(
            "AZURE_OPENAI_DEPLOYMENT_STRONG",
            "Deployment of the `strong` profile when Azure is chosen automatically (default: "
            "AZURE_OPENAI_DEPLOYMENT_NAME).",
            used_by=_MODEL,
        ),
        _s(
            "OPENAI_API_VERSION",
            "A dated Azure OpenAI API version. Unset: the v1 API, which needs none.",
            used_by=_MODEL,
        ),
        _s(
            "AZURE_OPENAI_AUTH",
            "`key` (the API key) or `entra` (a Microsoft Entra ID service principal).",
            type=_AUTH,
            default="key",
            choices=_AUTH_CHOICES,
            used_by=_MODEL,
        ),
    ),
    *_group(
        "microsoft",
        _s(
            "AZURE_TENANT_ID",
            "Directory (tenant) ID of the one app registration (Entra admin center > App "
            "registrations > Overview), also for Azure OpenAI with AZURE_OPENAI_AUTH=entra. "
            "Permission: none itself; each service below names its own.",
            used_by=_ENTRA,
        ),
        _s(
            "AZURE_CLIENT_ID",
            "Application (client) ID (a GUID) of that app. Permission: none itself; its API "
            "permissions are listed per service below. Check everything with `awa check "
            "microsoft`. Finding people (`awa people`, "
            "role: recipients): application User.Read.All with admin consent.",
            used_by=_ENTRA,
        ),
        _s(
            "AZURE_CLIENT_SECRET",
            "A client secret of the app: Certificates & secrets > Client secrets, copy the "
            "VALUE, not the Secret ID. Permission: none itself. Not needed with a certificate.",
            used_by=_ENTRA,
        ),
        _s(
            "AZURE_CLIENT_CERTIFICATE_PATH",
            "Recommended instead of a secret: a PEM file with the private key and the "
            "certificate, or a .pfx/.p12 file, whose public part is uploaded under "
            "Certificates & secrets > Certificates; it wins over AZURE_CLIENT_SECRET. "
            'Permission: none itself. Needs pip install "awa-agents[m365]".',
            used_by=_MS,
        ),
        _s(
            "AZURE_CLIENT_CERTIFICATE_PASSWORD",
            "The password of that key file, if it has one. Permission: none.",
            used_by=_MS,
        ),
        _s(
            "AZURE_AUTHORITY_HOST",
            "Entra sign-in host, only for sovereign clouds or a local test server (default "
            "`https://login.microsoftonline.com`). Permission: none.",
            used_by=_ENTRA,
            sensitive_target=True,
        ),
        _s(
            "MS_GRAPH_URL",
            "Microsoft Graph host, only for sovereign clouds or a local test server (default "
            "`https://graph.microsoft.com`). Permission: none.",
            used_by=_MS,
            sensitive_target=True,
        ),
        _s(
            "MS_MAILBOX",
            "Outlook: the ONE mailbox the process works in (a shared mailbox works too). "
            "Permission: application Mail.ReadWrite (plus Mail.Send only to send), limited to "
            "this mailbox with Exchange RBAC for Applications; then remove the tenant-wide "
            "Mail.* consent.",
            used_by=_MS,
        ),
        _s(
            "MS_SITE_URL",
            "SharePoint and Excel: the ONE site, such as "
            "`https://company.sharepoint.com/sites/sales`. Permission: application "
            "Sites.Selected, then an administrator grants 'read' or 'write' on this site.",
            used_by=_MS,
        ),
        _s(
            "MS_CHECK_WORKBOOK",
            "awa check: the workbook (path in the site's library) the Excel check reads "
            "(default: the first .xlsx at the top). Permission: as MS_SITE_URL.",
            used_by=("awa check",),
        ),
        _s(
            "TEAMS_WEBHOOK_URL",
            "Teams: a Workflows webhook (Teams > channel menu > Workflows > 'Send webhook "
            "alerts to a channel'; the whole link with sig=). Permission: none in Entra; the "
            "link itself is the secret.",
            used_by=_MS,
            secret=True,
        ),
        _s(
            "TEAMS_TEAM_ID",
            "Teams as a signed-in user (`awa connect microsoft --teams`): the ONE team the "
            "agents may use (the groupId= part of the team's link). Permission: delegated "
            "ChannelMessage.Send, ChannelMessage.Read.All and User.Read; the user is a member.",
            used_by=_MS,
        ),
        _s(
            "TEAMS_CHANNEL_ID",
            "The ONE channel of that team (19:...@thread.tacv2, from the channel's link). "
            "Permission: as TEAMS_TEAM_ID.",
            used_by=_MS,
        ),
        _s(
            "AWA_CHECK_NEGATIVE_MAILBOX",
            "awa check: a mailbox the app must NOT be able to read; the check expects 403 "
            "(empty skips the negative test). Permission: none.",
            used_by=("awa check",),
        ),
        _s(
            "AWA_CHECK_ALLOW_POST",
            "awa check: `teams` lets it post ONE test card; empty: it never posts. Permission: "
            "as TEAMS_WEBHOOK_URL or the Teams user.",
            used_by=("awa check",),
            choices=("teams",),
        ),
    ),
    *_group(
        "providers",
        _s("GOOGLE_API_KEY", "API key for `google_genai:` models.", used_by=_MODEL),
        _s(
            "GEMINI_API_KEY",
            "API key for `google_genai:` models when GOOGLE_API_KEY is unset.",
            used_by=_MODEL,
        ),
        _s("MISTRAL_API_KEY", "API key for `mistralai:` models.", used_by=_MODEL),
        _s("GROQ_API_KEY", "API key for `groq:` models.", used_by=_MODEL),
        _s(
            "OLLAMA_HOST",
            "Where Ollama listens, for example `http://localhost:11434` (`ollama:` models).",
            used_by=_MODEL,
            sensitive_target=True,
        ),
    ),
    *_group(
        "storage",
        _s(
            "AWA_POSTGRES_DSN",
            "PostgreSQL database of runs, queues, checkpoints and stores "
            "(postgresql://user:password@host:5432/db). Unset: files next to the blueprint.",
            used_by=("awa serve", "awa worker", "awa runs", "awa.persistence"),
        ),
        _s(
            "AWA_POSTGRES_ADMIN_DSN",
            "Direct PostgreSQL connection (not through PgBouncer) for setup locks. "
            "Unset: AWA_POSTGRES_DSN.",
            used_by=("awa serve", "awa worker", "awa store"),
        ),
        _s(
            "AWA_POSTGRES_POOL_MAX",
            "Connections of the one PostgreSQL pool each process shares.",
            type=int,
            default=10,
            used_by=("awa serve", "awa worker"),
        ),
        _s(
            "AWA_POSTGRES_POOLER",
            "`pgbouncer` when connections go through PgBouncer's transaction pooling (then no "
            "prepared statements).",
            type=_POOLER,
            default="none",
            choices=_POOLER_CHOICES,
            used_by=("awa serve", "awa worker"),
        ),
        _s(
            "AWA_DATA_BACKEND",
            "Where runs, steps and results are kept: `sqlite` (.awa/awa.db) or `postgres`. "
            "Unset: `postgres` when AWA_POSTGRES_DSN is set. `awa store use` changes it.",
            type=_DATA,
            choices=_DATA_CHOICES,
            used_by=("awa serve", "awa worker", "awa runs", "awa store"),
        ),
        _s(
            "AWA_CAPTURE_CONTENT",
            "Keep prompts, answers and tool bodies of process runs (read only by the auditor "
            "role). Off: only their length.",
            type=bool,
            default=False,
            used_by=("awa serve", "awa worker"),
        ),
        _s(
            "AWA_RUN_RETENTION_DAYS",
            "Days a finished process run is kept before `awa store sweep` deletes it. Unset: "
            "kept until deleted.",
            type=int,
            used_by=("awa store sweep",),
        ),
        _s(
            "AWA_PSEUDONYM_KEY",
            "Secret that links people to their runs as an HMAC, for erasure per person "
            "(written into .env on first local use).",
            secret=True,
            used_by=("awa serve", "awa worker", "awa store erase"),
        ),
        _s(
            "AWA_PSEUDONYM_KEY_ID",
            "Version of AWA_PSEUDONYM_KEY, kept with each link.",
            default="1",
            used_by=("awa serve", "awa worker"),
        ),
        _s(
            "AWA_CHECKPOINTER_BACKEND",
            "Checkpointer of `awa.persistence.open_checkpointer` (library use; the CLI chooses "
            "its own).",
            type=_BACKEND,
            default="memory",
            choices=_BACKENDS,
            used_by=("awa.persistence (library)",),
        ),
        _s(
            "AWA_CHECKPOINTER_PATH",
            "File of the `sqlite` checkpointer (default .awa/checkpoints.db).",
            used_by=("awa.persistence (library)",),
        ),
        _s(
            "AWA_ARTIFACT_BACKEND",
            "Artifact repository of `awa.persistence.open_artifact_repository`.",
            type=_ARTIFACTS,
            default="memory",
            choices=_ARTIFACT_CHOICES,
            used_by=("awa.persistence (library)",),
        ),
        _s(
            "AWA_ARTIFACT_DIR",
            "Folder of the `file` artifact backend (default .awa_artifacts).",
            used_by=("awa.persistence (library)",),
        ),
        _s(
            "AWA_STORE_BACKEND",
            "Store of `awa.persistence.open_store`.",
            type=_STORES,
            default="memory",
            choices=_STORE_CHOICES,
            used_by=("awa.persistence (library)",),
        ),
    ),
    *_group(
        "automation",
        _s(
            "AWA_TRACE_LEVEL",
            "How much each process run's trace records (default summary; `full` keeps message "
            "texts).",
            type=_TRACE,
            choices=_TRACE_CHOICES,
            used_by=("awa worker", "awa serve", "awa.automation"),
        ),
        _s(
            "AWA_IMAP_USER",
            "Default user name of a mailbox trigger without `username_env`.",
            used_by=("mailbox trigger",),
        ),
        _s(
            "AWA_IMAP_PASSWORD",
            "Default password (an app password) of a mailbox trigger without `password_env`.",
            used_by=("mailbox trigger",),
        ),
        _s(
            "AWA_HOOK_SECRET",
            "Signing secret of job callbacks, when a callback names it in `secret_env`.",
            used_by=("awa.server.jobs (examples)",),
        ),
    ),
    *_group(
        "server",
        _s(
            "AWA_API_KEY",
            "API key callers of `awa serve --api-key-env AWA_API_KEY` must send.",
            used_by=("awa serve --api-key-env",),
        ),
        _s(
            "AWA_MCP_API_KEY",
            "API key callers of `awa mcp serve --api-key-env AWA_MCP_API_KEY` must send.",
            used_by=("awa mcp serve --api-key-env",),
        ),
        _s(
            "AWA_ALLOWED_HOSTS",
            "Extra Host names (comma-separated) `awa serve` answers besides its own; "
            "`--allowed-host` replaces them.",
            used_by=("awa serve --allowed-host",),
            security=True,
        ),
        _s(
            "AWA_LOG_LEVEL",
            "Log level an application reads as `Settings.log_level`; awa itself sets no log level.",
            type=_LEVELS,
            default="INFO",
            choices=_LEVEL_CHOICES,
            used_by=("your application (Settings.log_level)",),
        ),
        _s(
            "AWA_ENV",
            "Deployment label, attached as the `env:<env>` trace tag; changes no behavior.",
            type=_ENVS,
            default="local",
            choices=_ENV_CHOICES,
            used_by=("trace tags (library)",),
        ),
    ),
    *_group(
        "tracing",
        _s(
            "LANGSMITH_TRACING",
            "Send LangChain runs to LangSmith.",
            type=bool,
            default=False,
            sdk="langsmith",
            used_by=("LangChain",),
        ),
        _s("LANGSMITH_API_KEY", "LangSmith API key.", sdk="langsmith"),
        _s("LANGSMITH_PROJECT", "LangSmith project name.", sdk="langsmith"),
        _s(
            "LANGSMITH_ENDPOINT",
            "Another LangSmith server.",
            sdk="langsmith",
            sensitive_target=True,
        ),
        _s(
            "AWA_LANGFUSE_TRACING",
            "Send every run of the awa commands to Langfuse (needs the `langfuse` extra).",
            type=bool,
            default=False,
            used_by=("awa CLI commands that run systems",),
        ),
        _s(
            "AWA_LANGFUSE_STRICT",
            "Stop a command when Langfuse tracing cannot start (off: warn once, run without it).",
            type=bool,
            default=False,
            used_by=("Langfuse tracing",),
        ),
        _s(
            "AWA_TRACE_EXPORT_TIMEOUT",
            "Seconds one span export or score request to Langfuse may take; a failed one is "
            "tried again with backoff, then its spans are dropped with one warning.",
            type=float,
            default=30.0,
            used_by=("Langfuse tracing",),
        ),
        _s(
            "AWA_LANGFUSE_CAPTURE_CONTENT",
            "Also send prompts, replies and tool data, redacted (off: names, ids, tokens, "
            "cost, timing).",
            type=bool,
            default=False,
            used_by=("Langfuse tracing",),
        ),
        _s("LANGFUSE_PUBLIC_KEY", "Langfuse public key.", used_by=("Langfuse tracing",)),
        _s("LANGFUSE_SECRET_KEY", "Langfuse secret key.", used_by=("Langfuse tracing",)),
        _s(
            "LANGFUSE_BASE_URL",
            "Langfuse server (default: Langfuse Cloud, EU).",
            used_by=("Langfuse tracing",),
            sensitive_target=True,
            aliases=("LANGFUSE_HOST",),
        ),
        _s(
            "LANGFUSE_RELEASE",
            "Release shown on every Langfuse trace, for example a git tag.",
            used_by=("Langfuse tracing",),
        ),
        _s(
            "LANGFUSE_TRACING_ENVIRONMENT",
            "Langfuse environment of every trace (`development`, `production`, ...).",
            used_by=("Langfuse tracing",),
        ),
    ),
    *_group(
        "workspace",
        _s(
            "AWA_WORKSPACE_DIR",
            "The workspace folder of the `workspace` connector (default data).",
            used_by=("workspace connector",),
        ),
        _s(
            "AWA_WORKSPACE_DB",
            "SQLite file in the workspace folder.",
            used_by=("workspace connector",),
        ),
    ),
    *_group(
        "caching",
        _s(
            "AWA_CACHE_ENABLED",
            "Master switch of every awa cache; the AWA_CACHE_* values win over a blueprint's "
            "`defaults.cache`.",
            type=bool,
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_PROMPT",
            "Provider prompt caching: `auto`, `off` or `1h`.",
            type=_CACHE_PROMPT,
            choices=_CACHE_PROMPT_CHOICES,
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_PROMPT_PAD",
            "Lengthen an OpenAI/Azure prompt prefix that is just short of the 1,024-token "
            "caching minimum, so repeated calls read it from the cache (default on).",
            type=bool,
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_TOOLS",
            "Reuse the results of read-only tools.",
            type=bool,
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_TOOLS_TTL",
            "Seconds a read-only tool result is kept across runs (unset: for one run).",
            type=float,
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_STEPS",
            "Reuse deterministic process steps.",
            type=bool,
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_STORE",
            "Where the caches are kept: `memory` or `sqlite`.",
            type=_CACHE_STORE,
            choices=_CACHE_STORES,
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_PATH",
            "File of the `sqlite` cache store (default .awa/cache.db).",
            used_by=("awa.runtime.cache",),
        ),
        _s(
            "AWA_CACHE_SALT",
            "Project secret that makes the cache keys sent to a provider opaque and stable "
            "(`awa cache salt` writes one).",
            secret=True,
            used_by=("awa.runtime.cache", "awa cache salt"),
        ),
    ),
    *_group(
        "limits",
        _s(
            "AWA_RUN_MAX_REQUESTS",
            "Model calls one run tree may make when its `UsageLimits` set no `max_requests` (a "
            "whole number; a ceiling cannot be switched off, only raised).",
            type=int,
            default=400,
            used_by=("awa.runtime.termination",),
        ),
        _s(
            "AWA_RUN_MAX_TOOL_CALLS",
            "Tool calls one run tree may make when its `UsageLimits` set no `max_tool_calls`.",
            type=int,
            default=200,
            used_by=("awa.runtime.termination",),
        ),
        _s(
            "AWA_RUN_MAX_TOKENS",
            "Input plus output tokens one run tree may use when its `UsageLimits` set no "
            "`max_total_tokens`.",
            type=int,
            default=1000000,
            used_by=("awa.runtime.termination",),
        ),
        _s(
            "AWA_RUN_MAX_SECONDS",
            "Wall-clock seconds of a run tree that has no `timeout` and no "
            "`UsageLimits.max_duration`.",
            type=int,
            default=3600,
            used_by=("awa.runtime.termination",),
        ),
        _s(
            "AWA_RUN_MAX_DEPTH",
            "How deep runs may nest (agents delegating to or calling agents); the top-level "
            "run has depth 0.",
            type=int,
            default=16,
            used_by=("awa.runtime.termination",),
        ),
        _s(
            "AWA_DELEGATION_MAX_REQUESTS",
            "Model calls one team delegation may make, its own delegations included, when "
            "`TeamLimits.delegation_limits` is not set; beyond it only that delegation stops.",
            type=int,
            default=50,
            used_by=("awa.team",),
        ),
        _s(
            "AWA_MAX_IDENTICAL_CALLS",
            "Identical tool calls (same tool, arguments and result) in one agent run before "
            "the run is stopped for making no progress (at least 2).",
            type=int,
            default=5,
            used_by=("awa.runtime.termination",),
        ),
        _s(
            "AWA_MAX_TOOL_ERRORS",
            "Failures in a row of one tool in one agent run before the run is stopped for "
            "making no progress.",
            type=int,
            default=5,
            used_by=("awa.runtime.termination",),
        ),
        _s(
            "AWA_MAX_IDENTICAL_OUTPUTS",
            "Identical results in a row after which a `Loop` stops, and identical drafts after "
            "which a `Checked` revision stops (at least 2).",
            type=int,
            default=2,
            used_by=("awa.workflows",),
        ),
        _s(
            "AWA_MAX_PAGES_PER_CALL",
            "Pages one connector list call reads before it returns a `next_page` to continue.",
            type=int,
            default=20,
            used_by=("awa.connectors",),
        ),
        _s(
            "AWA_TOOL_PROFILE",
            "Defaults of the tool layer: `cost` (results above 6,000 characters saved aside "
            "with a preview, clearing from 10,000 tokens) or `quality` (results cut at 40,000 "
            "characters, clearing from 40,000 tokens).",
            type=_TOOL_PROFILE,
            default="cost",
            choices=_TOOL_PROFILES,
            used_by=("awa.tools.offload", "awa.memory.context_editing"),
        ),
        _s(
            "AWA_TOOL_MAX_OUTPUT_CHARS",
            "Longest text result a tool hands to the model; unset: the profile's value. In the "
            "`cost` profile a longer result is saved aside and the model gets a preview and a "
            "reference it can read on with `read_tool_result`.",
            type=int,
            used_by=("awa.tools.offload",),
        ),
        _s(
            "AWA_TOOL_CIRCUIT_BREAKER",
            "Failures in a row after which a tool answers 'unavailable in this run' without "
            "being called (0: never).",
            type=int,
            default=3,
            used_by=("awa.tools.resilience",),
        ),
        _s(
            "AWA_TOOL_RETRIES",
            "Retries, with jittered backoff, of a call of a tool that does not act outside the "
            "agent and raised a network error.",
            type=int,
            default=2,
            used_by=("awa.tools.resilience",),
        ),
        _s(
            "AWA_CONTEXT_EDITING",
            "Replace older tool results with a short note naming the call in what the model "
            "sees once a request grows past `AWA_CONTEXT_EDITING_TRIGGER` (the run's state "
            "keeps them).",
            type=bool,
            default=True,
            used_by=("awa.memory.context_editing",),
        ),
        _s(
            "AWA_CONTEXT_EDITING_TRIGGER",
            "Approximate request size in tokens above which older tool results are cleared; "
            "unset: the profile's value (10,000 or 40,000).",
            type=int,
            used_by=("awa.memory.context_editing",),
        ),
        _s(
            "AWA_CONTEXT_EDITING_KEEP",
            "Most recent tool results that are never cleared (up to keep + step - 1 stay); "
            "unset: the profile's value (3 or 4).",
            type=int,
            used_by=("awa.memory.context_editing",),
        ),
        _s(
            "AWA_CONTEXT_EDITING_STEP",
            "Tool results cleared together, so the cached prompt prefix changes only every few "
            "calls.",
            type=int,
            default=4,
            used_by=("awa.memory.context_editing",),
        ),
        _s(
            "AWA_CONTEXT_EDITING_CLEAR_AT_LEAST",
            "Fewest tokens a clearing step must free, else nothing is cleared (a clearing "
            "costs one prompt-cache miss); unset: the profile's value (3,000 or 0).",
            type=int,
            used_by=("awa.memory.context_editing",),
        ),
        _s(
            "AWA_MAX_LEASE_LOSSES",
            "Lost worker leases in a row (a worker crashed or hung on the run) after which an "
            "automation run is dead-lettered instead of taken over again.",
            type=int,
            default=3,
            used_by=("awa.automation",),
        ),
    ),
    *(
        SettingSpec(
            name,
            type=int if name.endswith("_PORT") else str,
            group="services",
            used_by=("awa setup services",),
            secret=not name.endswith(("_PORT", "_EMAIL")),
            doc=doc,
        )
        for name, doc in (
            ("AWA_SETUP_DB_PORT", "Port of the local PostgreSQL on 127.0.0.1."),
            ("AWA_SETUP_DB_PASSWORD", "Password of the local PostgreSQL (generated)."),
            ("AWA_SETUP_LANGFUSE_PORT", "Port of the local Langfuse web UI on 127.0.0.1."),
            ("AWA_SETUP_LANGFUSE_ADMIN_EMAIL", "Sign-in email of the local Langfuse."),
            ("AWA_SETUP_LANGFUSE_ADMIN_PASSWORD", "Sign-in password of the local Langfuse."),
            ("AWA_SETUP_LANGFUSE_NEXTAUTH_SECRET", "Session secret of the local Langfuse."),
            ("AWA_SETUP_LANGFUSE_SALT", "Salt of the local Langfuse."),
            ("AWA_SETUP_LANGFUSE_ENCRYPTION_KEY", "Encryption key of the local Langfuse."),
            ("AWA_SETUP_LANGFUSE_DB_PASSWORD", "Password of the local Langfuse's PostgreSQL."),
            ("AWA_SETUP_LANGFUSE_CLICKHOUSE_PASSWORD", "Password of its ClickHouse."),
            ("AWA_SETUP_LANGFUSE_REDIS_PASSWORD", "Password of its Redis."),
            ("AWA_SETUP_LANGFUSE_MINIO_PASSWORD", "Password of its MinIO object store."),
        )
    ),
    *_group(
        "general",
        _s(
            "AWA_PROJECT_DIR",
            "The project folder (default: found from the current folder, see --project).",
            used_by=_CLI,
        ),
        _s(
            "AWA_STRICT_SETTINGS",
            "Stop with exit code 78 on an unknown AWA_* name or an unconfirmed security switch.",
            type=bool,
            default=False,
            used_by=_CLI,
        ),
    ),
)

REGISTRY: Mapping[str, SettingSpec] = MappingProxyType({spec.name: spec for spec in _SPECS})
"""Every setting by name (aliases are not keys; see `spec`)."""

_ALIASES: Mapping[str, str] = MappingProxyType(
    {alias: spec.name for spec in _SPECS for alias in spec.aliases}
)

DENYLIST: tuple[str, ...] = (
    "PATH",
    "PYTHON*",
    "LD_*",
    "DYLD_*",
    "*_PROXY",
    "SSL_CERT_*",
    "REQUESTS_CA_BUNDLE",
    "GIT_*",
    "DOCKER_*",
    "COMPOSE_*",
    "NODE_OPTIONS",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMROOT",
    "APPDATA",
    "USERPROFILE",
    "HOME",
    "BASH_ENV",
    "ENV",
    "OPENSSL_CONF",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "PERL5OPT",
    "PERL5LIB",
    "RUBYOPT",
    "RUBYLIB",
    "NODE_PATH",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "CLASSPATH",
    "SHELL",
    "LOCALAPPDATA",
    "SSLKEYLOGFILE",
    "PIP_*",
    "UV_*",
    "TEMP",
    "TMP",
    "TMPDIR",
)
"""Names a `.env` file can never export into the process environment, whatever declares them."""

PROJECT_ENV = "AWA_PROJECT_DIR"
"""Names the project folder when `--project` is not given."""

STRICT_ENV = "AWA_STRICT_SETTINGS"
"""`1` turns unknown `AWA_*` names and unconfirmed security switches into exit code 78."""

SETUP_ENV: Mapping[str, str] = MappingProxyType(
    {
        "dsn": "AWA_POSTGRES_DSN",
        "default_model": "AWA_DEFAULT_MODEL",
        "fast_model": "AWA_FAST_MODEL",
        "strong_model": "AWA_STRONG_MODEL",
        "langfuse_tracing": "AWA_LANGFUSE_TRACING",
        "db_port": "AWA_SETUP_DB_PORT",
        "db_password": "AWA_SETUP_DB_PASSWORD",
        "langfuse_port": "AWA_SETUP_LANGFUSE_PORT",
        "langfuse_email": "AWA_SETUP_LANGFUSE_ADMIN_EMAIL",
        "langfuse_password": "AWA_SETUP_LANGFUSE_ADMIN_PASSWORD",
        "langfuse_nextauth": "AWA_SETUP_LANGFUSE_NEXTAUTH_SECRET",
        "langfuse_salt": "AWA_SETUP_LANGFUSE_SALT",
        "langfuse_encryption": "AWA_SETUP_LANGFUSE_ENCRYPTION_KEY",
        "langfuse_db_password": "AWA_SETUP_LANGFUSE_DB_PASSWORD",
        "langfuse_clickhouse_password": "AWA_SETUP_LANGFUSE_CLICKHOUSE_PASSWORD",
        "langfuse_redis_password": "AWA_SETUP_LANGFUSE_REDIS_PASSWORD",
        "langfuse_minio_password": "AWA_SETUP_LANGFUSE_MINIO_PASSWORD",
    }
)
"""The awa settings `awa setup` writes, by role (one place for their names)."""

DOCTOR_SHOWN = (
    "AWA_DEFAULT_MODEL",
    "AWA_FAST_MODEL",
    "AWA_STRONG_MODEL",
    "AWA_TRACE_LEVEL",
    "AWA_POSTGRES_DSN",
    "AWA_WORKSPACE_DIR",
)
"""The settings `awa doctor` always lists (the model profiles and where runs go)."""

TYPO_PREFIXES = ("AWA_",)
"""Prefixes whose unknown names are reported in the environment and in `.env`."""

DOTENV_TYPO_PREFIXES = ("AWA_", "ANTHROPIC_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_")
"""Prefixes whose unknown names are reported when they appear in `.env`."""

_ENV_KEY = re.compile(r"_env$")
_MAX_BLUEPRINT_BYTES = 1_000_000
"""The size `awa.blueprints` accepts; a larger file declares no variables here."""


def spec(name: str) -> SettingSpec | None:
    """The spec of `name`, also when `name` is an alias; `None` for an unknown name."""

    return REGISTRY.get(name) or REGISTRY.get(_ALIASES.get(name, ""))


def is_denied(name: str) -> bool:
    """Whether `name` is on the `DENYLIST` (never exported from `.env`)."""

    upper = name.upper()
    return any(fnmatch.fnmatchcase(upper, pattern) for pattern in DENYLIST)


@dataclass(frozen=True)
class DeclaredEnv:
    """A variable a project names itself (for example a trigger's `secret_env`).

    Attributes:
        where: Where it is named (`blueprint.yaml: triggers[0].webhook.secret_env`).
        secret: Every declared name is secret unless it is a known setting that is not.
    """

    name: str
    where: str
    secret: bool = True


def declared_env(
    project: str | Path | None, *, files: Iterable[str | Path] = ()
) -> dict[str, DeclaredEnv]:
    """The variables `blueprint.yaml` of `project` and the extra `files` name in a `*_env` key.

    An unreadable file declares nothing; the commands report it themselves.
    """

    paths: list[Path] = []
    if project is not None:
        paths.append(Path(project) / "blueprint.yaml")
    paths.extend(Path(item) for item in files)
    found: dict[str, DeclaredEnv] = {}
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        data = _load(resolved)
        for name, where in _env_keys(data, path.name):
            known = spec(name)
            secret = known.is_secret if known is not None else True
            found.setdefault(name, DeclaredEnv(name, where, secret))
    return found


def _load(path: Path) -> Any:
    from awa._internal.serialize import load_yaml

    try:
        if path.stat().st_size > _MAX_BLUEPRINT_BYTES:
            return None
        return load_yaml(path.read_bytes(), max_bytes=_MAX_BLUEPRINT_BYTES, what="blueprint")
    except (OSError, ValueError, RecursionError):
        return None


def _env_keys(data: Any, where: str) -> Iterator[tuple[str, str]]:
    stack: list[tuple[Any, str]] = [(data, where)]
    while stack:
        node, path = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}"
                if (
                    isinstance(key, str)
                    and _ENV_KEY.search(key)
                    and isinstance(value, str)
                    and ENV_NAME.match(value)
                ):
                    yield value, here
                else:
                    stack.append((value, here))
        elif isinstance(node, list):
            stack.extend((item, f"{path}[{index}]") for index, item in enumerate(node))


def known_names() -> frozenset[str]:
    """Every registry name and alias."""

    return frozenset(REGISTRY) | frozenset(_ALIASES)
