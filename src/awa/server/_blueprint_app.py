"""Serve a blueprint file as an HTTP API: the app `awa serve` runs (see `build_blueprint_app`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa._internal.logging import get_logger
from awa.contracts.usage import UsageLimits
from awa.core.config import env_value
from awa.errors import MissingConfigurationError
from awa.server._auth import ApiKeyAuthenticator, Authenticator, Principal
from awa.server._limits import RateLimit

if TYPE_CHECKING:
    from fastapi import FastAPI

    from awa._internal.upstream.checkpoint import BaseCheckpointSaver
    from awa.automation import Automation, RunStore
    from awa.blueprints import Blueprint
    from awa.contracts.protocols import AgentLike
    from awa.persistence import SqliteCheckpointer
    from awa.persistence.runlog import RunLog
    from awa.server._data_store import LocalData, PostgresData

_log = get_logger("server.blueprint")

_RATE_PATTERN = re.compile(r"^\s*(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class ServeConfig:
    """The options of `awa serve` that build the app."""

    blueprint: str
    tools: list[str]
    skills: list[str]
    api_key_env: str | None
    cors_origins: list[str]
    api_keys_file: str | None = None
    thread_scope: str = "tenant"
    rate_limit: str | None = None
    max_concurrency: int | None = None
    approver_roles: list[str] = field(default_factory=list)
    drain_timeout: float | None = 30.0
    max_body_bytes: int | None = None
    json_logs: bool = False
    dsn: str | None = None
    schema: str = "awa"
    store_path: str | None = None
    host: str = "127.0.0.1"
    allowed_hosts: list[str] = field(default_factory=list)
    docs: bool = False
    usage_limits: dict[str, Any] | None = None

    def api_key(self) -> str | None:
        if self.api_key_env is None:
            return None
        key = (env_value(self.api_key_env) or "").strip()
        if not key:
            raise MissingConfigurationError(
                f"--api-key-env names {self.api_key_env}, but that environment variable is "
                "not set or empty"
            )
        return key

    def authenticator(self) -> Authenticator | None:
        return load_api_keys(self.api_keys_file) if self.api_keys_file is not None else None

    def app_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "api_key": self.api_key(),
            "authenticator": self.authenticator(),
            "cors_origins": self.cors_origins,
            "thread_scope": self.thread_scope,
            "rate_limit": parse_rate_limit(self.rate_limit) if self.rate_limit else None,
            "max_concurrency": self.max_concurrency,
            "approver_roles": self.approver_roles or None,
            "drain_timeout": self.drain_timeout,
            "access_log": self.json_logs,
            "docs": self.docs,
        }
        hosts = list(self.allowed_hosts)
        if hosts or (options["api_key"] is None and options["authenticator"] is None):
            # Without a key only this machine's own names (or a web page could rebind its name here).
            hosts += ["localhost", "127.0.0.1", "[::1]"]
            hosts += (
                []
                if self.host in ("0.0.0.0", "::")
                else [f"[{self.host}]" if ":" in self.host else self.host]
            )
        if hosts:
            options["allowed_hosts"] = hosts
        if self.max_body_bytes is not None:
            options["max_body_bytes"] = self.max_body_bytes
        if self.usage_limits is not None:
            options["usage_limits"] = self.usage_limits
        return options


def parse_rate_limit(text: str) -> RateLimit:
    """Parse `REQUESTS/SECONDS` (e.g. `60/60`) into a `RateLimit`."""

    match = _RATE_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(f"Invalid rate limit {text!r}: use REQUESTS/SECONDS, e.g. 60/60")
    return RateLimit(int(match.group(1)), per=float(match.group(2)))


def load_api_keys(path: str | Path) -> ApiKeyAuthenticator:
    """Build an `ApiKeyAuthenticator` from a JSON file of principals and key digests.

    Each entry holds the principal and either the key's hex SHA-256 digest (`sha256`) or an
    environment variable holding the key (`key_env`), so the file holds no secret:

    ```json
    {"keys": [
        {"id": "billing-svc", "tenant": "acme", "sha256": "9f86d0..."},
        {"id": "ops", "tenant": "acme", "roles": ["approver"], "key_env": "OPS_API_KEY"}
    ]}
    ```

    Raises:
        MissingConfigurationError: If the file is invalid or a `key_env`
            variable is unset.
    """

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = raw["keys"] if isinstance(raw, dict) else raw
        if not isinstance(entries, list) or not entries:
            raise ValueError("expected a non-empty list of keys")
        digests: dict[str, Principal] = {}
        from_env: dict[str, Principal] = {}
        for entry in entries:
            principal = Principal(
                str(entry["id"]), tenant=entry.get("tenant"), roles=list(entry.get("roles", []))
            )
            if "sha256" in entry:
                digests[str(entry["sha256"]).lower()] = principal
            elif "key_env" in entry:
                value = (env_value(str(entry["key_env"])) or "").strip()
                if not value:
                    raise MissingConfigurationError(
                        f"{path}: environment variable {entry['key_env']} is not set or empty"
                    )
                from_env[hashlib.sha256(value.encode()).hexdigest()] = principal
            else:
                raise ValueError(f"entry {entry.get('id')!r} needs 'sha256' or 'key_env'")
        return ApiKeyAuthenticator.from_digests({**digests, **from_env})
    except MissingConfigurationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise MissingConfigurationError(f"Invalid API keys file {path}: {exc}") from exc


def served_name(spec: Blueprint) -> str:
    """The name `/v1/systems/{name}` serves `spec` under: the team, workflow or network name,
    or for a single agent the agent's name (not the blueprint's)."""

    entry = spec.resolved_entrypoint()
    names = {"team": spec.team_name, "workflow": spec.workflow_name, "network": spec.network_name}
    return names[entry]() if entry in names else str(entry)


def build_blueprint(
    blueprint: str | Path,
    tools: Sequence[str],
    skills: Sequence[str],
    *,
    local_imports: bool = False,
    automation_threads: bool = False,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> tuple[Blueprint, AgentLike, list[Any], BaseCheckpointSaver[Any] | None]:
    """Build a blueprint: its spec, system, loaded tools and checkpointer.

    A blueprint with approvals (with `automation_threads`, also one with an automation, except a
    workflow) gets a checkpointer, in memory unless `checkpointer` is given.
    """

    from awa._internal.upstream.checkpoint import StateInMemorySaver
    from awa.blueprints import Blueprint
    from awa.blueprints._resolve import allow_local_imports, load_tools, skill_registry, tools_files

    spec = Blueprint.from_yaml(Path(blueprint))
    loaded = load_tools(tools_files(list(tools), blueprint))
    if local_imports:
        allow_local_imports()
    # Every entrypoint (workflows and networks included) pauses for approvals.
    threads = bool(spec.needs_checkpointer()) or (
        automation_threads
        and spec.automation_section() is not None
        and spec.resolved_entrypoint() != "workflow"
    )
    checkpointer = (checkpointer or StateInMemorySaver()) if threads else None
    options: dict[str, Any] = {
        "tools": loaded,
        "skill_registry": skill_registry(list(skills), blueprint),
        "checkpointer": checkpointer,
        "allow_imports": True,
    }
    if spec.active_mcp_servers():
        # MCP tools open a session per call, so tools loaded here keep working
        # on the server's event loop.
        from awa._internal import aio as asyncio_compat

        system = asyncio_compat.run(spec.abuild(**options))
    else:
        system = spec.build(**options)
    return spec, system, list(loaded), checkpointer


def build_blueprint_app(
    blueprint: str | Path,
    *,
    tools: Sequence[str] = (),
    skills: Sequence[str] = (),
    api_key: str | None = None,
    cors_origins: Sequence[str] = (),
    max_concurrency: int | None = None,
    approver_roles: Collection[str] | None = None,
    dsn: str | None = None,
    schema: str = "awa",
    store_path: str | os.PathLike[str] | None = None,
    usage_limits: UsageLimits | Mapping[str, Any] | None = None,
    **options: Any,
) -> FastAPI:
    """Build a blueprint file and return the app serving it.

    Tools load like `awa run` does (only serve trusted blueprints). A blueprint with triggers
    also serves its automation endpoints and executes its runs in a worker tied to the app's
    lifespan; without `dsn` that worker also runs schedules, file drops, mailboxes and queues,
    with `dsn` those are left to `awa worker`, so they fire once however many servers run. An
    agent memory with `user_id: principal` gets each run's `user_id` from the caller.

    Args:
        tools: Modules or `.py` files providing tools (comma-separated entries allowed).
        dsn: PostgreSQL DSN of the automation runs and checkpoints (as `awa worker --dsn`).
        store_path: Without `dsn`: the local SQLite data file, with `checkpoints.db` beside it;
            `None` keeps everything in memory.
        usage_limits: Limits for requests and automation runs, never looser than the
            blueprint's `limits:`.
        **options: Further `create_app` options.

    Raises:
        BlueprintError: If the blueprint cannot be loaded or built.
        OptionalDependencyError: If `dsn` is given without the `postgres` extra.
    """

    from awa.contracts.usage import merge_limits
    from awa.core.settings import get_settings
    from awa.server._app import create_app
    from awa.server._config import ServedSystem
    from awa.server._data_store import (
        PostgresData,
        ensure_pseudonym_key,
        open_local_data,
        uses_postgres,
    )

    settings = get_settings()
    postgres = PostgresData(dsn, schema, settings=settings) if dsn and uses_postgres(dsn) else None
    local = None if postgres is not None or store_path is None else Path(store_path)
    store: RunStore | None = postgres.runs if postgres is not None else None
    run_log: RunLog | None = postgres.log if postgres is not None else None
    saver: BaseCheckpointSaver[Any] | None = postgres.checkpointer if postgres else None
    files: _LocalFiles | None = None
    if local is not None:
        from awa.persistence import SqliteCheckpointer

        data = open_local_data(local, pseudonym=ensure_pseudonym_key(Path(blueprint).parent))
        store, run_log = data.runs, data.log
        saver = SqliteCheckpointer(local.with_name("checkpoints.db"))
        files = _LocalFiles(data, saver)
    spec, system, _, checkpointer = build_blueprint(
        blueprint, tools, skills, automation_threads=True, checkpointer=saver
    )
    sources = {agent.memory.user_id for agent in spec.agents if agent.memory is not None}
    if isinstance(usage_limits, Mapping):
        usage_limits = UsageLimits.model_validate(usage_limits)
    limits = merge_limits(usage_limits, spec.usage_limits())
    served = ServedSystem(
        system,
        usage_limits=limits,
        diagram=spec.to_mermaid(),
        max_concurrency=max_concurrency,
        approver_roles=approver_roles,
        # Memory scoped per user reads `deps["user_id"]`: from the caller's
        # identity, or (only when declared) from the request.
        principal_deps=_user_id_from_principal if "principal" in sources else None,
        allowed_deps={"user_id"} if "deps" in sources and "principal" not in sources else (),
    )
    section = spec.automation_section()
    if section is not None and "automations" not in options:
        from awa.automation import build_automation

        base_dir = spec.source.parent if spec.source is not None else None
        automation = build_automation(
            spec,
            system,
            spec=section,
            base_dir=base_dir,
            store=store,
            dsn=dsn,
            usage_limits=limits,
            run_log=run_log,
            capture=settings.capture_content,
        )
        options["automations"] = [automation]
        options["lifespan"] = _executing(
            [automation],
            postgres=postgres,
            files=files,
            drain_timeout=options.get("drain_timeout", 30.0),
            inner=options.get("lifespan"),
        )
    elif postgres is not None or files is not None:
        options["lifespan"] = _executing(
            [], postgres=postgres, files=files, drain_timeout=None, inner=options.get("lifespan")
        )
    if "thread_index" not in options and (postgres is not None or files is not None):
        # Owners survive a restart: in the database next to the checkpoints, or the local file.
        options["thread_index"] = postgres.threads if postgres is not None else files.data.threads  # type: ignore[union-attr]
    if "idempotency_cache" not in options and postgres is not None:
        options["idempotency_cache"] = postgres.idempotency
    app = create_app(
        {system.name: served},
        checkpointer=checkpointer,
        api_key=api_key,
        cors_origins=cors_origins,
        title=spec.name,
        **options,
    )
    return app


def _user_id_from_principal(principal: Principal) -> dict[str, Any]:
    return {"user_id": principal.id}


_Lifespan = Callable[["FastAPI"], AbstractAsyncContextManager[Any]]


@dataclass
class _LocalFiles:
    """The local data (run store, run log, thread index) and checkpoint database of
    `awa serve` without a database."""

    data: LocalData
    saver: SqliteCheckpointer

    @property
    def store(self) -> Any:
        return self.data.runs

    async def close(self) -> None:
        await self.data.close()
        await asyncio.to_thread(self.saver.close)


def _executing(
    automations: Sequence[Automation],
    *,
    postgres: PostgresData | None,
    files: _LocalFiles | None = None,
    drain_timeout: float | None,
    inner: _Lifespan | None,
) -> _Lifespan:
    """A lifespan executing the automations' queued runs; with local files it holds the triggers."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
        from datetime import timedelta

        from awa.automation import Worker
        from awa.automation.store import hold_triggers

        async with inner(app) if inner is not None else nullcontext() as state:
            if postgres is not None:
                await postgres.open()
            claim = ExitStack()
            try:
                if not automations:
                    yield state
                    return
                if files is not None:
                    claim.enter_context(hold_triggers(files.store.path))
                worker = Worker(
                    automations,
                    concurrency=sum(automation.concurrency for automation in automations),
                    drain_timeout=None
                    if drain_timeout is None
                    else timedelta(seconds=drain_timeout),
                    # Without a shared database this process is the only one: it runs every
                    # trigger. With one, schedules and the like are left to `awa worker`.
                    triggers=postgres is None,
                )
                task = asyncio.create_task(worker.run(), name="awa-serve-worker")
                task.add_done_callback(_report_stopped_worker)
                try:
                    yield state
                finally:
                    worker.stop()
                    await asyncio.gather(task, return_exceptions=True)
            finally:
                claim.close()
                if postgres is not None:
                    await postgres.close()
                if files is not None:
                    await files.close()

    return lifespan


def _report_stopped_worker(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _log.error("The automation worker of the server stopped", exc_info=task.exception())
