"""The build endpoints: start, follow, answer, cancel, export, and run what was built."""

# No `from __future__ import annotations`: FastAPI reads the endpoint annotations when defined.
import asyncio
import os
import tempfile
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.background import BackgroundTask

from awa.data import DataProfile, TooLargeError, UnsupportedTypeError
from awa.server._auth import Principal
from awa.server._builds import BuildRequest, export_files, hidden, snapshot
from awa.server._config import THREAD_ID_PATTERN
from awa.server._data_api import ID_PATTERN, DataApi, api_base
from awa.server._requests import ResumeRequest, RunRequest
from awa.server._router import IdempotencyKey, error_responses
from awa.server._schemas import InterruptList, RunResponse
from awa.server._serialize import SSE_PING, sse_frame
from awa.server.errors import BuildConflictError, BuildNotFoundError, InvalidRequestError
from awa.server.ports import TERMINAL, BuildRecord

_TEXT_SUFFIXES = frozenset(
    {".yaml", ".yml", ".md", ".json", ".jsonl", ".txt", ".py", ".csv", ".tsv", ".toml", ".example"}
)
_MAX_VIEW_BYTES = 1024 * 1024
_DEFAULT_MAX_REQUESTS = 70


class NoteIn(BaseModel):
    """What must happen with one file."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=260, description="A file name of the upload")
    text: str = Field(min_length=1, max_length=500)


class BuildOptionsIn(BaseModel):
    """Optional settings of a build."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, description="One of the models the server allows")
    rounds: int = Field(default=3, ge=1, le=5, description="Evaluation rounds at most")
    target: float = Field(default=1.0, ge=0.0, le=1.0, description="Pass rate to stop at")
    max_requests: int | None = Field(
        default=None, ge=2, description="Model calls at most (up to the server's ceiling)"
    )
    mask: Literal["none", "placeholders", "drop"] = Field(
        default="none", description="What the model may see of the data"
    )
    guidance: str = Field(default="", max_length=2000, description="Extra requirements")


class BuildCreate(BaseModel):
    """Body of `POST /v1/builds`."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=4000)
    upload_id: str = Field(pattern=ID_PATTERN)
    notes: list[NoteIn] = Field(default_factory=list, max_length=50)
    options: BuildOptionsIn = Field(default_factory=BuildOptionsIn)


class BuildAccepted(BaseModel):
    """Answer of `POST /v1/builds`: where to follow the build."""

    build_id: str
    status: Literal["queued"] = "queued"
    events_url: str
    url: str


class BuildState(BaseModel):
    """A build: where it is, and at the end what came out."""

    build_id: str
    status: Literal[
        "queued",
        "profiling",
        "needs_input",
        "designing",
        "evaluating",
        "repairing",
        "writing",
        "completed",
        "failed",
        "cancelled",
    ]
    stage: str | None = None
    round: int = 0
    pass_rate: float | None = None
    upload_id: str | None = None
    description: str | None = None
    created_at: str
    finished_at: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    questions: list[dict[str, Any]] | None = Field(
        default=None, description="With status needs_input: id, text, options, evidence"
    )
    report: dict[str, Any] | None = Field(
        default=None,
        description="With status completed: summary, passed, pass_rate, rounds, stopped, "
        "flaky, failing, assumptions, files, links",
    )
    error: dict[str, Any] | None = Field(
        default=None, description="With status failed: type, code, message, details"
    )


class BuildList(BaseModel):
    """The caller's builds, newest first."""

    builds: list[BuildState]


class AnswerIn(BaseModel):
    """The answer to one question."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)


class AnswersIn(BaseModel):
    """Body of `POST /v1/builds/{id}/answers`."""

    model_config = ConfigDict(extra="forbid")

    answers: list[AnswerIn] = Field(min_length=1, max_length=50)


class BuildRunIn(BaseModel):
    """Body of `POST /v1/builds/{id}/runs`: what to process (a request, a file, or both)."""

    model_config = ConfigDict(extra="forbid")

    input: str | None = Field(default=None, min_length=1, max_length=20000)
    file: str | None = Field(
        default=None, max_length=260, description="A file of the build's profile: id or name"
    )
    thread_id: Annotated[str | None, StringConstraints(pattern=THREAD_ID_PATTERN)] = None


def _confined(folder: Path, relative: str) -> Path:
    """The file `relative` inside `folder`, or an error; never a path outside it.

    Raises:
        InvalidRequestError: The path is empty, absolute or climbs out of the folder.
        BuildNotFoundError: There is no such file (or it is not one that may be shown).
    """

    parts = PurePosixPath(relative).parts
    if (
        not relative
        or "\\" in relative
        or "\x00" in relative
        or relative.startswith("/")
        or ":" in parts[0]
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise InvalidRequestError("The path must be a relative path inside the project")
    target = folder.joinpath(*parts)
    if (
        target.is_symlink()
        or not target.is_file()
        or hidden(Path(*parts))
        or not target.resolve().is_relative_to(folder.resolve())
    ):
        raise BuildNotFoundError(f"The project has no file {relative!r}")
    return target


def _zip_project(folder: Path) -> Path:
    """The project as a zip file in a temporary location (the caller removes it)."""

    handle, name = tempfile.mkstemp(prefix="awa-project-", suffix=".zip")
    os.close(handle)
    try:
        with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as archive:
            for relative in export_files(folder):
                archive.write(folder / relative, relative.as_posix())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def _links(base: str, build_id: str) -> dict[str, str]:
    root = f"{base}/builds/{build_id}"
    return {
        "events": f"{root}/events",
        "project": f"{root}/project.zip",
        "files": f"{root}/files/{{path}}",
        "runs": f"{root}/runs",
    }


def _shaped(record: BuildRecord, base: str) -> dict[str, Any]:
    body = snapshot(record)
    if "report" in body:
        body["report"] = {**body["report"], "links": _links(base, record.id)}
    return body


class _BuiltSystems:
    """The systems of finished builds, on a sibling server, never reachable through `/v1/systems`."""

    def __init__(self, api: DataApi) -> None:
        self.api = api
        self.runs = api.server.sibling()
        self._tenants: dict[str, str | None] = {}  # served name -> tenant of its build
        self._loading: dict[str, asyncio.Lock] = {}

    async def name(self, record: BuildRecord) -> str:
        """The name a build's system is served under here; it is built on first use.

        Raises:
            BuildConflictError: The build has not completed.
        """

        if record.status != "completed":
            raise BuildConflictError(f"Build {record.id!r} is {record.status}, not completed")
        name = f"build-{record.id}"
        if name in self.runs.systems:
            return name
        self._evict_purged()
        # One load per build: concurrent first runs share one system (and its checkpointer).
        lock = self._loading.setdefault(name, asyncio.Lock())
        async with lock:
            if name not in self.runs.systems:
                project = self.api.builds.project_dir(record.tenant, record.id)
                served = await asyncio.to_thread(self.api.system_factory, project)
                options = self.api.options
                self.runs.add_system(
                    name,
                    replace(
                        served,
                        approver_roles=options.approver_roles
                        if options.approver_roles is not None
                        else served.approver_roles,
                        run_timeout=options.run_timeout
                        if options.run_timeout is not None
                        else served.run_timeout,
                    ),
                )
                self._tenants[name] = record.tenant
        self._loading.pop(name, None)
        return name

    def _evict_purged(self) -> None:
        """Stop serving the systems of builds that are gone (deleted, or purged when expired)."""

        for name, tenant in list(self._tenants.items()):
            try:
                self.api.builds.get(tenant, name.removeprefix("build-"))
            except BuildNotFoundError:
                self.runs.remove_system(name)
                del self._tenants[name]


def builds_routes(router: APIRouter, api: DataApi, caller: Callable[..., Any], prefix: str) -> None:
    """Add the build endpoints to `router` (prefix `prefix`)."""

    BuildId = Annotated[str, PathParam(pattern=ID_PATTERN)]
    ThreadId = Annotated[str, PathParam(pattern=THREAD_ID_PATTERN)]
    systems = _BuiltSystems(api)
    service = api.service
    ping = api.server.ping_interval

    def events(principal: Principal, record: BuildRecord, after: int) -> AsyncIterator[bytes]:
        async def frames() -> AsyncIterator[bytes]:
            async for event in service.events(principal.tenant, record.id, after, ping):
                yield SSE_PING if event is None else sse_frame(event["type"], event, event["seq"])

        return frames()

    def stream(principal: Principal, record: BuildRecord, after: int) -> Response:
        return StreamingResponse(
            events(principal, record, after),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Build-Id": record.id,
            },
        )

    @router.post(
        "/builds",
        summary="Start a build from an upload and a description",
        status_code=202,
        response_model=BuildAccepted,
        responses={
            **error_responses(400, 401, 403, 404, 422, 429),
            200: {"description": "With `?stream=true`: the event stream itself"},
        },
    )
    async def create_build(
        request: Request,
        body: BuildCreate,
        principal: Annotated[Principal, Depends(caller)],
        stream_events: Annotated[bool, Query(alias="stream")] = False,
    ) -> Response:
        api.require_builder(principal)
        await api.check_rate(principal)
        options = api.options
        model = body.options.model
        if model is not None and model not in options.models:
            raise InvalidRequestError(
                f"The model {model!r} is not allowed here; choose one of "
                f"{', '.join(sorted(options.models))}"
            )
        ceiling = options.max_requests
        asked = body.options.max_requests or min(_DEFAULT_MAX_REQUESTS, ceiling)
        if asked > ceiling:
            raise InvalidRequestError(f"options.max_requests may be at most {ceiling}")
        api.upload_of(principal, body.upload_id)
        record = service.start(
            principal.tenant,
            principal.id,
            BuildRequest(
                description=body.description,
                upload_id=body.upload_id,
                notes={note.file: note.text for note in body.notes},
                model=model,
                rounds=body.options.rounds,
                target=body.options.target,
                max_requests=asked,
                mask=body.options.mask,
                guidance=body.options.guidance,
            ),
        )
        if stream_events:
            return stream(principal, record, 0)
        links = _links(api_base(request, prefix), record.id)
        return JSONResponse(
            {
                "build_id": record.id,
                "status": "queued",
                "events_url": links["events"],
                "url": f"{api_base(request, prefix)}/builds/{record.id}",
            },
            status_code=202,
            headers={"Location": f"{api_base(request, prefix)}/builds/{record.id}"},
        )

    @router.get(
        "/builds",
        summary="List the caller's builds",
        response_model=BuildList,
        responses=error_responses(401),
    )
    async def list_builds(
        request: Request,
        principal: Annotated[Principal, Depends(caller)],
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> Response:
        only_own = None if api.sees_tenant(principal) else principal.id
        records = service.list_builds(principal.tenant, owner=only_own, limit=limit)
        return JSONResponse(
            {"builds": [_shaped(record, api_base(request, prefix)) for record in records]}
        )

    @router.get(
        "/builds/{build_id}",
        summary="Get a build's status and report",
        response_model=BuildState,
        responses=error_responses(401, 404),
    )
    async def get_build(
        request: Request, build_id: BuildId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return JSONResponse(_shaped(api.build_of(principal, build_id), api_base(request, prefix)))

    @router.get(
        "/builds/{build_id}/events",
        summary="Follow a build as Server-Sent Events",
        response_class=StreamingResponse,
        responses={
            **error_responses(401, 404),
            200: {
                "description": "Events; `id:` is the sequence number",
                "content": {"text/event-stream": {}},
            },
            204: {"description": "The build is over and every event was sent already"},
        },
    )
    async def build_events(
        build_id: BuildId,
        principal: Annotated[Principal, Depends(caller)],
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
        record = api.build_of(principal, build_id)
        after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
        if record.status in TERMINAL:
            frames = events(principal, record, after)
            first = await anext(frames, None)
            if first is None:  # nothing left to send: 204 tells a browser to stop reconnecting
                return Response(status_code=204)

            async def again() -> AsyncIterator[bytes]:
                yield first
                async for frame in frames:
                    yield frame

            return StreamingResponse(
                again(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
            )
        return stream(principal, record, after)

    @router.post(
        "/builds/{build_id}/answers",
        summary="Answer the questions of a build that waits for input",
        status_code=202,
        response_model=BuildState,
        responses=error_responses(400, 401, 403, 404, 409, 422, 429),
    )
    async def answer_build(
        request: Request,
        build_id: BuildId,
        body: AnswersIn,
        principal: Annotated[Principal, Depends(caller)],
    ) -> Response:
        api.require_builder(principal)
        await api.check_rate(principal)
        api.build_of(principal, build_id)
        record = service.answer(
            principal.tenant, build_id, {answer.id: answer.value for answer in body.answers}
        )
        return JSONResponse(_shaped(record, api_base(request, prefix)), status_code=202)

    @router.delete(
        "/builds/{build_id}",
        summary="Cancel a build",
        response_model=BuildState,
        responses=error_responses(401, 403, 404),
    )
    async def cancel_build(
        request: Request, build_id: BuildId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        api.require_builder(principal)
        api.build_of(principal, build_id)
        record = await service.cancel(principal.tenant, build_id, actor=principal.id)
        return JSONResponse(_shaped(record, api_base(request, prefix)))

    @router.get(
        "/builds/{build_id}/project.zip",
        summary="Download the project of a finished build",
        response_class=FileResponse,
        responses={
            **error_responses(401, 404, 409, 413),
            200: {"content": {"application/zip": {}}},
        },
    )
    async def project_zip(
        build_id: BuildId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        record = api.build_of(principal, build_id)
        if record.status != "completed":
            raise BuildConflictError(f"Build {build_id!r} is {record.status}, not completed")
        folder = api.builds.project_dir(record.tenant, record.id)
        path = await asyncio.to_thread(_zip_project, folder)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"{record.id}.zip",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @router.get(
        "/builds/{build_id}/files/{path:path}",
        summary="Read one text file of the project",
        response_class=Response,
        responses={
            **error_responses(400, 401, 404, 413, 415),
            200: {"content": {"text/plain": {}}},
        },
    )
    async def project_file(
        build_id: BuildId, path: str, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        record = api.build_of(principal, build_id)
        folder = api.builds.project_dir(record.tenant, record.id)
        target = _confined(folder, path)
        if target.suffix.lower() not in _TEXT_SUFFIXES:
            raise UnsupportedTypeError(
                "Only text files can be read here; download project.zip for the rest"
            )
        if target.stat().st_size > _MAX_VIEW_BYTES:
            raise TooLargeError(f"{path} is larger than {_MAX_VIEW_BYTES} bytes; use project.zip")
        text = target.read_bytes().decode("utf-8", errors="replace")
        return Response(
            text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": "inline"}
        )

    @router.post(
        "/builds/{build_id}/runs",
        summary="Run the built process",
        response_model=RunResponse,
        responses=error_responses(400, 401, 403, 404, 409, 422, 429, 500),
    )
    async def create_run(
        request: Request,
        build_id: BuildId,
        body: BuildRunIn,
        principal: Annotated[Principal, Depends(caller)],
        stream_run: Annotated[bool, Query(alias="stream")] = False,
        mode: Literal["sync", "async"] = "sync",
        idempotency_key: IdempotencyKey = None,
    ) -> Response:
        api.require_builder(principal)
        record = api.build_of(principal, build_id)
        text = _run_input(api, record, body)
        name = await systems.name(record)
        return await systems.runs.start_run(
            name,
            RunRequest(input=text, thread_id=body.thread_id),
            principal,
            stream=stream_run,
            mode=mode,
            idempotency_key=idempotency_key,
            runs_path=f"{api_base(request, prefix)}/runs",
        )

    @router.get(
        "/builds/{build_id}/threads/{thread_id}/interrupts",
        summary="List the pending approvals of a run of a build",
        response_model=InterruptList,
        responses=error_responses(401, 404),
    )
    async def list_interrupts(
        build_id: BuildId, thread_id: ThreadId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        name = await systems.name(api.build_of(principal, build_id))
        return JSONResponse(await systems.runs.interrupts(name, thread_id, principal))

    @router.post(
        "/builds/{build_id}/threads/{thread_id}/resume",
        summary="Answer an approval of a run of a build",
        response_model=RunResponse,
        responses=error_responses(400, 401, 403, 404, 409, 422, 429, 500),
    )
    async def resume_run(
        build_id: BuildId,
        thread_id: ThreadId,
        body: ResumeRequest,
        principal: Annotated[Principal, Depends(caller)],
    ) -> Response:
        name = await systems.name(api.build_of(principal, build_id))
        return await systems.runs.run_resume(name, thread_id, body, principal)


def _run_input(api: DataApi, record: BuildRecord, body: BuildRunIn) -> str:
    """The text a run starts from: the caller's request, a file of the build's profile, or both.

    Raises:
        InvalidRequestError: Neither is given, or the file is not one of the build's files.
    """

    if body.file is None:
        if body.input is None:
            raise InvalidRequestError("Send an input, a file, or both")
        return body.input
    profile = api.builds.project_dir(record.tenant, record.id) / "data" / "PROFILE.json"
    try:
        found = DataProfile.from_json(profile.read_text(encoding="utf-8")).find_file(body.file)
    except (OSError, ValueError):
        found = None
    if found is None:
        raise InvalidRequestError(f"The build has no file {body.file!r}")
    return f"Process {found.path}" if body.input is None else f"{body.input}\n\nFile: {found.path}"
