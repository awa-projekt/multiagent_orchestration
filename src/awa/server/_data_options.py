"""`DataOptions`: what `create_app(data=...)` needs to take uploads, profile files and run builds."""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from awa.server._data_kit import random_id, utcnow
from awa.server._limits import BaseRateLimiter, RateLimit
from awa.server.ports import BuildEngine, BuildStore, ClockFn, IdFn, SystemFactory, UploadStore

if TYPE_CHECKING:
    from awa.data import UploadScanner

MIB = 1024 * 1024
MAX_FILE_BYTES_CEILING = 100 * MIB
"""The largest single file `DataOptions.max_file_bytes` may allow."""


@dataclass(frozen=True)
class DataOptions:
    """Turn on the data endpoints of `create_app(data=DataOptions(root="..."))`.

    Uploads, profiles, builds and runs of what was built, kept per tenant (another tenant's ids
    answer 404). Without `root` or your own `uploads` and `builds` stores everything lives in a
    temporary folder, lost on restart; `require_durable` refuses that fallback.

    Args:
        root: A folder for uploads and builds (one subfolder per tenant); nothing else writes
            to it and it is never served as static files.
        uploads: Your own upload store; defaults to `FileUploadStore` under `root`.
        builds: Your own build store; defaults to `FileBuildStore` under `root`.
        require_durable: Refuse to start with the temporary-folder fallback.
        clock: Returns the current time, aware and in UTC.
        ids: Returns a new unguessable id for a kind of object.
        max_file_bytes: Largest single file; at most 100 MB.
        max_files: Most files in one upload (files inside a zip count).
        max_tenant_bytes: What one tenant may keep stored (uploads and builds); 429 beyond it.
        upload_ttl: Seconds an upload is kept, unless a build waiting for answers needs it.
        build_ttl: Seconds a build is kept.
        idle_timeout: Seconds an upload may send nothing before it is stopped (408).
        upload_timeout: Seconds a whole upload may take.
        max_builds: Builds of one tenant running at once.
        max_requests: Ceiling for the model calls of one build (a build's default is the smaller
            of this and 70).
        models: Model names a build may choose with `options.model`; anything else is refused.
        roles: Roles allowed to upload and build; `None` lets every authenticated caller.
        approver_roles: Roles allowed to answer approvals of runs of built systems.
        run_timeout: Seconds a run of a built system may take; `None` keeps the served default.
        scanner: An `awa.data.UploadScanner` run on every accepted file.
        engine: Designs, evaluates and writes a build; defaults to `awa.blueprints.build_project`.
        event_buffer: Events kept per build, so a client can resume its stream with
            `Last-Event-ID`.
        max_request_bytes: All files of one upload request together.
        rate_limit: Uploads, profiles and builds per principal; `None` disables it.
        system_factory: Builds the served system of a finished build from its project folder;
            the default reads `blueprint.yaml` and `tools.py` like `awa serve`.

    Example:
        ```python
        app = create_app(
            {"support": team},
            authenticator=auth,
            data=DataOptions(root="/var/lib/awa/data", max_tenant_bytes=2 * 1024**3),
        )
        ```
    """

    root: str | os.PathLike[str] | None = None
    uploads: UploadStore | None = None
    builds: BuildStore | None = None
    require_durable: bool = False
    clock: ClockFn = utcnow
    ids: IdFn = random_id
    max_file_bytes: int = 25 * MIB
    max_request_bytes: int = 100 * MIB
    max_files: int = 200
    max_tenant_bytes: int = 1024 * MIB
    upload_ttl: float = 24 * 3600.0
    build_ttl: float = 7 * 24 * 3600.0
    idle_timeout: float = 30.0
    upload_timeout: float = 600.0
    max_builds: int = 2
    max_requests: int = 200
    models: Collection[str] = ("default", "fast", "strong")
    rate_limit: RateLimit | BaseRateLimiter | None = field(
        default_factory=lambda: RateLimit(60, per=60.0)
    )
    roles: Collection[str] | None = None
    approver_roles: Collection[str] | None = None
    run_timeout: float | None = 600.0
    scanner: UploadScanner | None = None
    engine: BuildEngine | None = None
    system_factory: SystemFactory | None = None
    event_buffer: int = 5000

    def __post_init__(self) -> None:
        if self.root is not None and not str(self.root).strip():
            raise ValueError("DataOptions.root must be a folder or None")
        if (
            self.require_durable
            and self.root is None
            and (self.uploads is None or self.builds is None)
        ):
            raise ValueError(
                "DataOptions(require_durable=True) needs root=... or both uploads=... and "
                "builds=...; without them uploads and builds would be lost on restart"
            )
        if not 0 < self.max_file_bytes <= MAX_FILE_BYTES_CEILING:
            raise ValueError(
                f"DataOptions.max_file_bytes must be between 1 and {MAX_FILE_BYTES_CEILING} (100 MB)"
            )
        for name in ("max_request_bytes", "max_files", "max_tenant_bytes", "max_builds"):
            if getattr(self, name) < 1:
                raise ValueError(f"DataOptions.{name} must be at least 1")
        if self.max_request_bytes < self.max_file_bytes:
            raise ValueError("DataOptions.max_request_bytes must not be below max_file_bytes")
        for name in ("upload_ttl", "build_ttl", "idle_timeout", "upload_timeout"):
            if not getattr(self, name) > 0:
                raise ValueError(f"DataOptions.{name} must be positive")
        if self.max_requests < 2:
            raise ValueError("DataOptions.max_requests must be at least 2")
        if self.event_buffer < 10:
            raise ValueError("DataOptions.event_buffer must be at least 10")
        if self.run_timeout is not None and not self.run_timeout > 0:
            raise ValueError("DataOptions.run_timeout must be positive or None")
        for name in ("models", "roles", "approver_roles"):
            value = getattr(self, name)
            if isinstance(value, str):
                raise TypeError(f"DataOptions.{name} must be a collection of names, not a string")
            if value is not None:
                object.__setattr__(self, name, frozenset(value))
        if self.rate_limit is not None and not isinstance(
            self.rate_limit, RateLimit | BaseRateLimiter
        ):
            raise TypeError("DataOptions.rate_limit must be a RateLimit or BaseRateLimiter")
