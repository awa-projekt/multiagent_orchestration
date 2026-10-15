"""The upload and profile endpoints, and `mount_data_api`, the one place that picks the backends."""

# No `from __future__ import annotations`: FastAPI reads the endpoint annotations when defined.
import asyncio
import json
import shutil
import tempfile
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from awa._internal.logging import get_logger
from awa.data import DataProfile, FileProfile, Limits, Privacy, UnsupportedTypeError, aprofile
from awa.data._model import FileKind
from awa.server._auth import Principal
from awa.server._builds import BuildService
from awa.server._data_kit import SAFE_ID, STALE_SECONDS
from awa.server._data_options import DataOptions
from awa.server._limits import as_rate_limiter
from awa.server._multipart import ReceiveLimits, receive_files
from awa.server._router import AgentRoute, IdempotencyKey, error_responses
from awa.server._server import Server
from awa.server._stores import IdempotencyClaim, fingerprint, scope
from awa.server.errors import (
    BuildConflictError,
    BuildNotFoundError,
    InvalidRequestError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitExceededError,
    UploadNotFoundError,
)
from awa.server.ports import BuildRecord, BuildStore, SystemFactory, UploadRecord, UploadStore

_log = get_logger("server.data")

UPLOAD_KINDS: frozenset[FileKind] = frozenset(
    {"csv", "xlsx", "pdf", "docx", "mail", "json", "sqlite", "text"}
)
"""The file kinds an upload accepts (decided by content, see `awa.data.detect_kind`)."""

MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "mail": "message/rfc822",
    "json": "application/json",
    "sqlite": "application/vnd.sqlite3",
    "text": "text/plain",
}
"""The media type the content of each accepted kind has (`sniffed_type` of an uploaded file)."""

ID_PATTERN = SAFE_ID.pattern
_JSON_BODY_LIMIT = 64 * 1024


FileId = Annotated[str, StringConstraints(pattern=r"^f[0-9]{1,6}$")]


class ProfileRequest(BaseModel):
    """Body of `POST /v1/data/profile`.

    Attributes:
        mask: What the model view may show (`none`, `placeholders`, `drop`); `drop` also removes
            sample values from the profile. `mask` or `budget_tokens` adds a `model_view`.
        files: Only these files (`f1`, `f2`, ...), keeping their ids of the whole upload.
        budget_tokens: Size of the model view in tokens (default 6000).
    """

    model_config = ConfigDict(extra="forbid")

    upload_id: str = Field(pattern=ID_PATTERN)
    mask: Literal["none", "placeholders", "drop"] | None = None
    budget_tokens: int | None = Field(default=None, ge=200, le=12000)
    files: list[FileId] | None = Field(default=None, max_length=500)


class UploadedFileOut(BaseModel):
    """One accepted file of an upload."""

    id: str = Field(description="`f1`, `f2`, ...: the id the file has in a profile of the upload")
    name: str = Field(description="The safe name the file is kept under")
    size: int
    kind: str = Field(description="csv, xlsx, pdf, docx, mail, json, sqlite or text (by content)")
    sniffed_type: str = Field(description="The media type of the content (not the client's claim)")
    sha256: str
    status: Literal["ok"] = "ok"


class UploadResponse(BaseModel):
    """What `POST /v1/uploads` and `GET /v1/uploads/{id}` return."""

    upload_id: str
    files: list[UploadedFileOut]
    total_bytes: int
    created_at: str
    expires_at: str


class UploadDeleted(BaseModel):
    """What `DELETE /v1/uploads/{id}` returns."""

    upload_id: str
    deleted: Literal[True] = True


class ProfileResponse(BaseModel):
    """What `POST /v1/data/profile` returns."""

    upload_id: str | None = Field(
        description="The upload profiled; `null` when the files were sent with the request "
        "without `?keep=true` (they are profiled and not kept)"
    )
    profile: dict[str, Any] = Field(
        description="A DataProfile: schema `awa.data/profile/v1` (see profile.v1.schema.json)"
    )
    model_view: str | None = Field(
        default=None,
        description="The digest a model would see, when mask or budget_tokens was sent",
    )


_MULTIPART_FILES = {
    "multipart/form-data": {
        "schema": {
            "type": "object",
            "required": ["files"],
            "properties": {
                "files": {"type": "array", "items": {"type": "string", "format": "binary"}}
            },
        }
    }
}


class DataApi:
    """Uploads and profiles for the endpoints (the builds are `BuildService`)."""

    def __init__(
        self,
        server: Server,
        options: DataOptions,
        uploads: UploadStore,
        builds: BuildStore,
        service: BuildService,
        system_factory: SystemFactory,
        audit: Callable[..., None],
    ) -> None:
        self.server = server
        self.options = options
        self.uploads = uploads
        self.builds = builds
        self.service = service
        self.system_factory = system_factory
        self.audit = audit
        self._limiter = as_rate_limiter(options.rate_limit)
        self._receive = ReceiveLimits(
            options.max_file_bytes,
            options.max_request_bytes,
            options.max_files,
            options.idle_timeout,
            options.upload_timeout,
        )
        self._ingest = Limits(
            max_files=options.max_files,
            max_file_bytes=options.max_file_bytes,
            max_total_bytes=options.max_request_bytes,
            max_zip_entries=options.max_files,
        )

    def require_builder(self, principal: Principal) -> None:
        """Raise `PermissionDeniedError` unless the caller may upload and build."""

        roles = self.options.roles
        if roles and not principal.has_role(*roles):
            raise PermissionDeniedError(
                f"Uploading and building needs one of the roles {', '.join(sorted(roles))}"
            )

    async def check_rate(self, principal: Principal) -> None:
        """Raise `RateLimitExceededError` when the caller sent too many data requests."""

        if self._limiter is None:
            return
        wait = await self._limiter.acquire(f"{principal.tenant or ''}/{principal.id}/data")
        if wait is not None:
            raise RateLimitExceededError(
                "Too many requests; slow down and retry later", retry_after=wait
            )

    def owned(self, tenant: str | None, owner: str, principal: Principal) -> bool:
        """Whether `principal` may use what `owner` of `tenant` created (the app's thread scope)."""

        return self.server.owns(tenant, owner, principal)

    def upload_of(self, principal: Principal, upload_id: str) -> UploadRecord:
        """The upload, if the caller may see it.

        Raises:
            UploadNotFoundError: Unknown, expired, or of another tenant or principal.
        """

        record = self.uploads.get(principal.tenant, upload_id)
        if not self.owned(record.tenant, record.principal, principal):
            raise UploadNotFoundError(f"No upload {upload_id!r} is known here")
        return record

    def sees_tenant(self, principal: Principal) -> bool:
        """Whether the caller may see what other principals of its tenant created."""

        return self.owned(principal.tenant, "", principal)

    def build_of(self, principal: Principal, build_id: str) -> BuildRecord:
        """The build, if the caller may see it.

        Raises:
            BuildNotFoundError: Unknown, expired, or of another tenant or principal.
        """

        record = self.service.get(principal.tenant, build_id)
        if not self.owned(record.tenant, record.principal, principal):
            raise BuildNotFoundError(f"No build {build_id!r} is known here")
        return record

    async def aclose(self) -> None:
        """Stop the builds that still run (the app shuts down)."""

        await self.service.close()

    def _quota(self, tenant: str | None) -> int:
        quota = self.options.max_tenant_bytes - self.builds.used_bytes(tenant)
        if quota <= 0:
            raise QuotaExceededError(
                "The storage quota is used up by builds; delete uploads or wait for them to expire",
                retry_after=60.0,
            )
        return quota

    async def receive(self, request: Request, principal: Principal) -> UploadRecord:
        """Take the files of a multipart request as a new upload of the caller."""

        self.service.maintain()
        with self.uploads.begin(
            principal.tenant, principal.id, quota=self._quota(principal.tenant)
        ) as session:
            await receive_files(request, session, self._receive)
            record = await asyncio.to_thread(
                session.commit, self._ingest, kinds=UPLOAD_KINDS, scanner=self.options.scanner
            )
        self.audit(
            "upload_created",
            actor=principal.id,
            tenant=principal.tenant,
            upload_id=record.id,
            files=len(record.files),
            bytes=record.bytes,
        )
        return record

    def describe(self, record: UploadRecord) -> dict[str, Any]:
        """An upload as the API shows it."""

        return {
            "upload_id": record.id,
            "files": [
                {
                    "id": file.id,
                    "name": file.name,
                    "size": file.size,
                    "kind": file.kind,
                    "sniffed_type": MEDIA_TYPES.get(file.kind, "application/octet-stream"),
                    "sha256": file.sha256,
                    "status": "ok",
                }
                for file in record.files
            ],
            "total_bytes": record.bytes,
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
        }

    async def create_upload(
        self, request: Request, principal: Principal, key: str | None, location: str
    ) -> Response:
        """`POST /v1/uploads`: receive, and with an `Idempotency-Key` never keep a duplicate."""

        self.require_builder(principal)
        await self.check_rate(principal)
        record = await self.receive(request, principal)
        headers = {"Location": f"{location}/{record.id}"}
        claim: IdempotencyClaim | None = None
        if key is not None:
            claim = IdempotencyClaim(
                self.server.idempotency,
                scope(principal.tenant, principal.id, "uploads"),
                key,
                fingerprint([[file.name, file.sha256] for file in record.files]),
                "an upload with an Idempotency-Key",
            )
            try:
                stored = await claim.begin(
                    self.server.idempotency_ttl,
                    mismatch="This Idempotency-Key was used for different files; use a new key",
                    conflict="An upload with this Idempotency-Key is still being processed; retry later",
                )
            except BaseException:
                self.uploads.delete(record)
                raise
            if stored is not None:  # the same upload was kept before: answer with that one
                self.uploads.delete(record)
                return JSONResponse(
                    stored.body,
                    status_code=stored.status or 201,
                    headers={**stored.headers, "Idempotent-Replayed": "true"},
                )
        body = self.describe(record)
        if claim is not None:
            await claim.finish(201, body, headers)
        return JSONResponse(body, status_code=201, headers=headers)

    def delete_upload(self, principal: Principal, upload_id: str) -> dict[str, Any]:
        """`DELETE /v1/uploads/{id}` (not while a build still needs the upload)."""

        self.require_builder(principal)
        record = self.upload_of(principal, upload_id)
        if record.id in self.service.needed_uploads():
            raise BuildConflictError(
                f"A build still needs upload {upload_id!r}; cancel the build first"
            )
        self.uploads.delete(record)
        self.audit(
            "upload_deleted", actor=principal.id, tenant=principal.tenant, upload_id=upload_id
        )
        return {"upload_id": upload_id, "deleted": True}

    async def profile(
        self,
        principal: Principal,
        record: UploadRecord,
        *,
        mask: str | None,
        budget_tokens: int | None,
        files: list[str] | None,
    ) -> dict[str, Any]:
        """The profile of an upload as the API returns it (no server paths, no samples with
        `mask="drop"`, a `model_view` when `mask` or `budget_tokens` was given)."""

        options = self.options
        limits = Limits(
            max_files=options.max_files,
            max_file_bytes=options.max_file_bytes,
            max_total_bytes=options.max_request_bytes,
        )
        with self.uploads.open_files(record) as folder:
            found = await aprofile(
                folder, root=folder, limits=limits, privacy=Privacy(mask=mask or "none")
            )
        found = _select(found, files)
        for file in found.files:
            _forget_source(file)
        body: dict[str, Any] = {"upload_id": record.id}
        if mask is not None or budget_tokens is not None:
            from awa.data import render_for_model

            body["model_view"] = render_for_model(
                found, budget_tokens or 6000, privacy=Privacy(mask=mask or "none")
            )
        shown = found.without_samples() if mask == "drop" else found
        body["profile"] = json.loads(shown.to_json(indent=None))
        self.audit(
            "profile_requested",
            actor=principal.id,
            tenant=principal.tenant,
            upload_id=record.id,
            files=len(found.files),
            mask=mask or "none",
        )
        return body

    async def profile_request(self, request: Request, principal: Principal) -> Response:
        """`POST /v1/data/profile`: a JSON body naming an upload, or the files themselves."""

        self.require_builder(principal)
        await self.check_rate(principal)
        kind = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if kind == "multipart/form-data":
            try:
                asked = _ProfileQuery.model_validate(dict(request.query_params))
            except ValidationError as error:
                raise RequestValidationError(error.errors()) from error
            record = await self.receive(request, principal)
            # Files sent with the request are profiled and then removed, so previews never hold
            # the quota; `?keep=true` keeps them as an upload a build can use.
            try:
                shown = await self.profile(
                    principal,
                    record,
                    mask=asked.mask,
                    budget_tokens=asked.budget_tokens,
                    files=None,
                )
            finally:
                if not asked.keep:
                    self.uploads.delete(record)
            if not asked.keep:
                shown["upload_id"] = None
            return JSONResponse(shown)
        if kind != "application/json":
            raise UnsupportedTypeError(
                'Send JSON ({"upload_id": ...}) or the files as multipart/form-data'
            )
        try:
            body = ProfileRequest.model_validate_json(await _read_json(request))
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error
        record = self.upload_of(principal, body.upload_id)
        return JSONResponse(
            await self.profile(
                principal,
                record,
                mask=body.mask,
                budget_tokens=body.budget_tokens,
                files=body.files,
            )
        )


class _ProfileQuery(BaseModel):
    """The query string of a multipart `POST /v1/data/profile`."""

    model_config = ConfigDict(extra="forbid")

    mask: Literal["none", "placeholders", "drop"] | None = None
    budget_tokens: int | None = Field(default=None, ge=200, le=12000)
    keep: bool = False


async def _read_json(request: Request) -> bytes:
    """The request body, at most 64 KiB (a profile request is a few lines)."""

    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _JSON_BODY_LIMIT:
            raise InvalidRequestError("The request body is too large for a JSON request")
    return bytes(body)


def _select(found: DataProfile, ids: list[str] | None) -> DataProfile:
    """The profile restricted to the files `ids` (their ids and relations stay as they are)."""

    if ids is None:
        return found
    wanted = set(ids)
    unknown = sorted(wanted - {file.id for file in found.files})
    if unknown:
        raise InvalidRequestError(f"The upload has no file {', '.join(unknown)}")
    document = found.model_dump(by_alias=True)
    document["files"] = [file for file in document["files"] if file["id"] in wanted]
    document["relations"] = [
        relation
        for relation in document["relations"]
        if relation["from_column"].split(".")[0] in wanted
        and relation["to_column"].split(".")[0] in wanted
    ]
    return DataProfile.model_validate(document)


def _forget_source(file: FileProfile) -> None:
    """Remove the path on the server's disk from a file profile and from the files inside it."""

    file.source = None
    for child in file.children:
        _forget_source(child)


def api_base(request: Request, prefix: str) -> str:
    """Where the data endpoints are, seen from the client (`root_path` and `prefix`)."""

    return f"{request.scope.get('root_path', '')}{prefix}"


def data_routes(api: DataApi, caller: Callable[..., Any], prefix: str) -> APIRouter:
    """The upload and profile endpoints (and, through `builds_routes`, the build endpoints)."""

    router = APIRouter(prefix=prefix, route_class=AgentRoute, tags=["data"])
    UploadId = Annotated[str, PathParam(pattern=ID_PATTERN)]

    @router.post(
        "/uploads",
        summary="Upload files",
        status_code=201,
        response_model=UploadResponse,
        responses=error_responses(400, 401, 403, 408, 413, 415, 422, 429),
        openapi_extra={"requestBody": {"required": True, "content": _MULTIPART_FILES}},
    )
    async def create_upload(
        request: Request,
        principal: Annotated[Principal, Depends(caller)],
        idempotency_key: IdempotencyKey = None,
    ) -> Response:
        return await api.create_upload(
            request, principal, idempotency_key, f"{api_base(request, prefix)}/uploads"
        )

    @router.get(
        "/uploads/{upload_id}",
        summary="Describe an upload",
        response_model=UploadResponse,
        responses=error_responses(401, 404),
    )
    async def get_upload(
        upload_id: UploadId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return JSONResponse(api.describe(api.upload_of(principal, upload_id)))

    @router.delete(
        "/uploads/{upload_id}",
        summary="Delete an upload",
        response_model=UploadDeleted,
        responses=error_responses(401, 403, 404, 409),
    )
    async def delete_upload(
        upload_id: UploadId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return JSONResponse(api.delete_upload(principal, upload_id))

    @router.post(
        "/data/profile",
        summary="Profile the files of an upload",
        response_model=ProfileResponse,
        responses=error_responses(400, 401, 403, 404, 413, 415, 422, 429),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": ProfileRequest.model_json_schema()},
                    **_MULTIPART_FILES,
                },
            }
        },
    )
    async def profile_data(
        request: Request, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return await api.profile_request(request, principal)

    from awa.server._builds_api import builds_routes

    builds_routes(router, api, caller, prefix)
    return router


def mount_data_api(app: FastAPI, server: Server, options: DataOptions, prefix: str) -> DataApi:
    """Choose the backends, build the services and add the data endpoints to `app`.

    Backends: the stores of `options`, else folders under `options.root`, else a temporary folder
    (lost on restart; logged as a warning). Call `aclose()` on the result when the app stops.
    """

    from awa.server._data_blueprints import BlueprintsEngine, blueprint_system

    uploads, builds = options.uploads, options.builds
    if uploads is None or builds is None:
        from awa.server._data_filesystem import FileBuildStore, FileUploadStore

        if options.root is not None:
            root = Path(options.root).resolve()
            _log.info("Uploads and builds are kept under %s", root)
        else:
            root = Path(tempfile.mkdtemp(prefix="awa-data-"))
            weakref.finalize(app, shutil.rmtree, root, True)
            _log.warning(
                "Uploads and builds are kept in a temporary folder and are lost when the server "
                "restarts; pass DataOptions(root=...) or your own stores to keep them"
            )
        uploads = uploads or FileUploadStore(
            root,
            ttl=options.upload_ttl,
            clock=options.clock,
            ids=options.ids,
            # A receive may take upload_timeout: its staging folder outlives it by a minute.
            stale_after=max(STALE_SECONDS, options.upload_timeout + 60),
        )
        builds = builds or FileBuildStore(root, ttl=options.build_ttl, clock=options.clock)
    audit = server.record
    service = BuildService(
        store=builds,
        uploads=uploads,
        engine=options.engine or BlueprintsEngine(),
        clock=options.clock,
        ids=options.ids,
        max_builds=options.max_builds,
        event_buffer=options.event_buffer,
        audit=audit,
    )
    service.recover()
    api = DataApi(
        server, options, uploads, builds, service, options.system_factory or blueprint_system, audit
    )

    async def caller(request: Request) -> Principal:
        return await server.authenticate(request)

    app.include_router(data_routes(api, caller, prefix))
    return api
