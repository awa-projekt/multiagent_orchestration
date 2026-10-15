"""The FastAPI application behind `awa ui`: one inline page and a small JSON/SSE API."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi import Path as UrlPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from awa.blueprints import DEFAULT_MAX_REQUESTS, MAX_BLUEPRINT_BYTES
from awa.data import DataError
from awa.errors import AwaError
from awa.plugins.switches import security_off
from awa.server._serialize import jsonable, sse_frame
from awa.ui._data import DATA_STATUS
from awa.ui._examples import example_target, list_examples, open_example, project_process
from awa.ui._patterns import MAX_REQUEST_CAP, Patterns
from awa.ui._project import Project, issue_json
from awa.ui._security import BodyLimits, Guard, new_token
from awa.ui._try import Conversation, clip, explained, text_of

if TYPE_CHECKING:
    from awa._internal.upstream.models import BaseChatModel
    from awa.connectors import Transport
    from awa.contracts.protocols import AgentLike
    from awa.evals import EvalCase
    from awa.evals.report import CaseResult
    from awa.models import ModelRegistry

_MAX_TEXT = 20_000
_MAX_BODY = 2 * MAX_BLUEPRINT_BYTES
_TEST_CONCURRENCY = 4
_TEST_TIMEOUT = 300.0
_UPLOAD_SLACK = 1_000_000
_BUSY = "Something is still running; wait until it has finished"
SystemKey = Annotated[str, UrlPath(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class _Settings(BaseModel):
    values: dict[str, str] = Field(max_length=64)


class _Values(BaseModel):
    values: dict[str, Annotated[str, Field(max_length=8192)]] = Field(
        default_factory=dict, max_length=32
    )


class _FileName(BaseModel):
    name: str = Field(min_length=1, max_length=512)


class _Note(_FileName):
    text: str = Field(default="", max_length=2000)


class _Mask(BaseModel):
    mask: str = Field(max_length=32)


class _Sample(BaseModel):
    trigger: str = Field(max_length=64)
    values: dict[str, Annotated[str, Field(max_length=5000)]] = Field(
        default_factory=dict, max_length=16
    )


class _RunId(BaseModel):
    run: str = Field(max_length=256)


class _SystemKind(BaseModel):
    connector: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)


class _System(_SystemKind):
    values: dict[str, Annotated[str, Field(max_length=8192)]] = Field(
        default_factory=dict, max_length=32
    )
    samples: bool = False


class _CaseChange(BaseModel):
    input: str = Field(min_length=1, max_length=_MAX_TEXT)
    systems: dict[str, Annotated[str, Field(max_length=16)]] = Field(
        default_factory=dict, max_length=32
    )


class _CaseId(BaseModel):
    id: str = Field(min_length=1, max_length=256)


class _ExampleId(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")


class _Describe(BaseModel):
    process: str = Field(max_length=_MAX_TEXT)
    thorough: bool = True


class _Suggest(BaseModel):
    process: str = Field(max_length=_MAX_TEXT)
    allow_model: bool = False


class _PatternTest(_Suggest):
    ids: list[Annotated[str, Field(max_length=40)]] = Field(min_length=1, max_length=3)
    approval_gate: bool = False
    max_requests: int = Field(default=DEFAULT_MAX_REQUESTS, ge=1, le=MAX_REQUEST_CAP)


class _PatternId(BaseModel):
    id: str = Field(max_length=40)


class _BriefWrite(BaseModel):
    force: bool = False


class _Blueprint(BaseModel):
    yaml: str = Field(max_length=MAX_BLUEPRINT_BYTES)
    save: bool = False


class _Message(BaseModel):
    message: str = Field(min_length=1, max_length=_MAX_TEXT)
    thread_id: str | None = Field(default=None, max_length=64)


class _Decision(BaseModel):
    id: str = Field(max_length=256)
    approve: bool
    note: str = Field(default="", max_length=2000)


class _Resume(BaseModel):
    thread_id: str = Field(max_length=64)
    decisions: list[_Decision] = Field(min_length=1, max_length=32)


class _Trigger(BaseModel):
    kind: str = Field(max_length=32)
    name: str = Field(default="", max_length=64)
    settings: dict[str, str] = Field(default_factory=dict, max_length=16)


class _Approval(BaseModel):
    run: str = Field(max_length=256)
    interrupt_id: str = Field(max_length=256)
    approve: bool
    note: str = Field(default="", max_length=2000)
    args: str = Field(default="", max_length=_MAX_TEXT)


class _Job:
    """A background task whose events are kept, so a reloaded page can follow it again."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.running = True
        self._loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()
        self._wake = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def emit(self, event: str, data: dict[str, Any]) -> None:
        if threading.get_ident() != self._thread:
            self._loop.call_soon_threadsafe(self.emit, event, data)
            return
        self.frames.append(sse_frame(event, jsonable(data)))
        self._wake.set()
        self._wake = asyncio.Event()

    def finish(self) -> None:
        self.running = False
        self._wake.set()

    async def follow(self) -> AsyncIterator[bytes]:
        sent = 0
        while True:
            wake = self._wake
            while sent < len(self.frames):
                yield self.frames[sent]
                sent += 1
            if not self.running:
                return
            await wake.wait()


def create_ui_app(
    project: str | Path | Project = ".",
    *,
    token: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    model: str | BaseChatModel | None = None,
    model_registry: ModelRegistry | None = None,
    environ: MutableMapping[str, str] | None = None,
    transport: Transport | None = None,
    examples: str | Path | None = None,
) -> FastAPI:
    """Build the local web app for a project folder.

    Open `http://<host>:<port>/?token=<token>` in a browser; other options are those of `Project`.

    Args:
        token: The access token; a random one when `None` (in `app.state.token`).
        host: The address served; only requests for it (on loopback also `localhost`) pass.
        examples: The examples folder "Open an example" offers (default: a source checkout's,
            else `./examples`).

    Example:
        ```python
        app = create_ui_app("my-process", token="...", port=8765)
        # uvicorn.run(app, host="127.0.0.1", port=8765)
        ```
    """
    from awa.models.replay import refuse_replay

    refuse_replay("The web app")

    workspace = (
        project
        if isinstance(project, Project)
        else Project(
            project,
            model=model,
            model_registry=model_registry,
            environ=environ,
            transport=transport,
        )
    )
    secret = token or new_token()
    names = {host, "localhost", "127.0.0.1", "[::1]"} if _loopback(host) else {host}
    hosts = {f"{name}:{port}" for name in names}
    jobs: dict[str, _Job] = {}
    conversation = Conversation(workspace)
    patterns = Patterns(workspace)
    page = page_source()

    app = FastAPI(title="awa", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = secret
    app.state.project = workspace

    def failure(exc: BaseException, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": explained(workspace, exc, "request")}, status_code=status)

    async def handled(request: Request, exc: Exception) -> JSONResponse:
        return failure(exc)

    async def invalid(request: Request, exc: Exception) -> JSONResponse:
        # Never the default body: it would echo the submitted values back.
        errors = exc.errors() if isinstance(exc, RequestValidationError) else []
        fields = sorted({".".join(map(str, error["loc"][1:])) for error in errors})
        message = f"The request is incomplete or too long ({', '.join(fields) or 'body'})"
        return JSONResponse({"error": {"message": message, "fix": "Check the form."}}, 422)

    async def refused(request: Request, exc: Exception) -> JSONResponse:
        code = getattr(exc, "code", "")
        return JSONResponse(
            {"error": {**explained(workspace, exc, "request"), "code": code}},
            status_code=DATA_STATUS.get(code, 400),
        )

    for kind in (AwaError, ValueError, OSError, ImportError):
        app.add_exception_handler(kind, handled)
    app.add_exception_handler(DataError, refused)
    app.add_exception_handler(RequestValidationError, invalid)

    def busy() -> JSONResponse | None:
        """The 409 answer while a background task runs (the project must not change under it)."""

        if any(job.running for job in jobs.values()):
            return failure(RuntimeError(_BUSY), 409)
        return None

    def start(kind: str, work: Callable[[_Job], Awaitable[dict[str, Any]]]) -> Response:
        current = jobs.get(kind)
        if current is not None and current.running:
            return failure(RuntimeError("This is still running; wait until it has finished"), 409)
        job = jobs[kind] = _Job()

        async def run() -> None:
            try:
                job.emit("done", await work(job))
            except Exception as exc:  # noqa: BLE001 - shown on the page with the fix
                job.emit("error", explained(workspace, exc, kind))
            finally:
                job.finish()

        job.task = asyncio.create_task(run(), name=f"awa-ui-{kind}")
        return _stream(job.follow())

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        nonce = request.scope["state"]["awa_ui_nonce"]
        return HTMLResponse(page.replace("{{nonce}}", nonce))

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {
            "folder": str(workspace.directory),
            "has_system": workspace.blueprint_file.is_file(),
            "has_cases": workspace.cases_file.is_file(),
            "jobs": {kind: job.running for kind, job in jobs.items()},
            "security_off": security_off(workspace.directory),
            "process": project_process(workspace.directory),
        }

    @app.get("/api/examples")
    async def examples_list() -> dict[str, Any]:
        return {"examples": await asyncio.to_thread(list_examples, examples)}

    @app.post("/api/examples/open")
    async def examples_open(body: _ExampleId) -> Response:
        # The page works on one project at a time: the example becomes the project.
        nonlocal workspace, conversation, patterns
        if (refusal := busy()) is not None:
            return refusal
        target = example_target(workspace.directory, body.id, beside=True)
        opened = await asyncio.to_thread(
            open_example, body.id, target, examples=examples, settings_from=workspace.directory
        )
        workspace = Project(
            target,
            model=workspace.model,
            model_registry=workspace.model_registry,
            environ=workspace.environ,
            transport=workspace.transport,
            data_limits=workspace.data_limits,
        )
        conversation = Conversation(workspace)
        patterns = Patterns(workspace)
        jobs.clear()
        app.state.project = workspace
        return JSONResponse(opened, status_code=201)

    @app.get("/api/setup")
    async def setup() -> dict[str, Any]:
        return workspace.setup()

    @app.post("/api/setup")
    async def save_setup(body: _Settings) -> dict[str, Any]:
        written = workspace.save_settings(body.values)  # a ValueError is answered by `handled`
        return {"written": written, "setup": workspace.setup()}

    @app.post("/api/build")
    async def build(body: _Describe) -> Response:
        return start(
            "build",
            lambda job: workspace.create(body.process, thorough=body.thorough, emit=job.emit),
        )

    @app.post("/api/patterns/suggest")
    async def suggest_patterns(body: _Suggest) -> Response:
        return start(
            "patterns", lambda job: patterns.suggest(body.process, allow_model=body.allow_model)
        )

    @app.post("/api/patterns/test")
    async def test_patterns(body: _PatternTest) -> Response:
        return start(
            "patterns-test",
            lambda job: patterns.test(
                body.process,
                body.ids,
                approval_gate=body.approval_gate,
                allow_model=body.allow_model,
                max_requests=body.max_requests,
                emit=job.emit,
            ),
        )

    @app.post("/api/patterns/use")
    async def use_pattern(body: _PatternId) -> Response:
        if (refusal := busy()) is not None:
            return refusal
        return JSONResponse(await asyncio.to_thread(patterns.use, body.id))

    @app.get("/api/brief")
    async def brief() -> dict[str, Any]:
        from awa.blueprints import BRIEF_FILE, assistant_brief

        if not (workspace.directory / "blueprint.yaml").is_file():
            return {"brief": None, "file": BRIEF_FILE, "exists": False}
        text = await asyncio.to_thread(assistant_brief, workspace.directory)
        return {
            "brief": text,
            "file": BRIEF_FILE,
            "exists": (workspace.directory / BRIEF_FILE).exists(),
        }

    @app.post("/api/brief")
    async def write_brief(body: _BriefWrite) -> dict[str, Any]:
        from awa.blueprints import write_brief as write

        path = await asyncio.to_thread(write, workspace.directory, force=body.force)
        return {"written": path.name}

    @app.get("/api/jobs/{kind}")
    async def follow(kind: str) -> Response:
        job = jobs.get(kind)
        if job is None:
            return JSONResponse({"error": {"message": "Nothing is running", "fix": ""}}, 404)
        return _stream(job.follow())

    @app.get("/api/data")
    async def data() -> dict[str, Any]:
        return await asyncio.to_thread(workspace.data.state)

    @app.put("/api/data/files")
    async def upload(
        request: Request, name: Annotated[str, Query(min_length=1, max_length=512)]
    ) -> JSONResponse:
        stored = await workspace.data.receive(name, request.stream())
        return JSONResponse({"stored": stored}, status_code=201)

    @app.post("/api/data/analyse")
    async def analyse() -> Response:
        return start("data", lambda job: asyncio.to_thread(workspace.data.analyse, job.emit))

    @app.post("/api/data/remove")
    async def remove(body: _FileName) -> dict[str, Any]:
        return await asyncio.to_thread(workspace.data.remove, body.name)

    @app.post("/api/data/note")
    async def note(body: _Note) -> dict[str, bool]:
        workspace.data.set_note(body.name, body.text)
        return {"saved": True}

    @app.get("/api/systems")
    async def systems() -> dict[str, Any]:
        return await asyncio.to_thread(workspace.systems.state)

    @app.post("/api/systems/form")
    async def system_form(body: _SystemKind) -> dict[str, Any]:
        return await asyncio.to_thread(workspace.systems.form, body.connector, body.name)

    @app.post("/api/systems/test")
    async def test_system(body: _System) -> dict[str, Any]:
        return await asyncio.to_thread(
            workspace.systems.test, body.connector, body.name, body.values
        )

    @app.post("/api/systems")
    async def save_system(body: _System) -> dict[str, Any]:
        return await asyncio.to_thread(
            lambda: workspace.systems.save(
                body.connector, body.name, body.values, samples=body.samples
            )
        )

    @app.post("/api/systems/{key}/refresh")
    async def refresh_system(key: SystemKey) -> dict[str, Any]:
        return await asyncio.to_thread(workspace.systems.refresh, key)

    @app.post("/api/systems/{key}/remove")
    async def remove_system(key: SystemKey) -> dict[str, Any]:
        return await asyncio.to_thread(workspace.systems.remove, key)

    @app.get("/api/datamap")
    async def datamap() -> dict[str, Any]:
        return await asyncio.to_thread(workspace.data_map)

    @app.get("/api/cases")
    async def cases() -> dict[str, Any]:
        return await asyncio.to_thread(workspace.case_editor.view)

    @app.post("/api/cases/{case_id}")
    async def save_case(
        case_id: Annotated[str, UrlPath(min_length=1, max_length=256)], body: _CaseChange
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            workspace.case_editor.save, case_id, body.input, body.systems
        )

    @app.post("/api/data/mask")
    async def mask(body: _Mask) -> dict[str, str]:
        workspace.data.set_mask(body.mask)
        return {"mask": body.mask}

    @app.get("/api/try")
    async def try_it() -> dict[str, Any]:
        return workspace.try_it()

    @app.get("/api/project")
    async def review() -> dict[str, Any]:
        return workspace.review()

    @app.post("/api/project")
    async def check(body: _Blueprint) -> dict[str, Any]:
        if body.save:
            issues, saved = workspace.save_yaml(body.yaml)
        else:
            issues, saved = workspace.check(body.yaml), False
        return {"issues": [issue_json(issue) for issue in issues], "saved": saved}

    @app.get("/api/connections")
    async def connections() -> dict[str, Any]:
        return workspace.connections.systems()

    @app.post("/api/connections/{key}/test")
    async def test_connection(key: SystemKey, body: _Values) -> dict[str, Any]:
        return await asyncio.to_thread(workspace.connections.test, key, body.values)

    @app.post("/api/connections/{key}/stand-in")
    async def back_to_stand_in(key: SystemKey) -> dict[str, Any]:
        return await asyncio.to_thread(workspace.connections.back_to_stand_in, key)

    @app.post("/api/connections/{key}")
    async def save_connection(key: SystemKey, body: _Values) -> dict[str, Any]:
        return await workspace.connections.save(key, body.values)

    @app.post("/api/test")
    async def test() -> Response:
        return start("test", lambda job: _run_tests(workspace, job))

    @app.post("/api/test/case")
    async def test_case(body: _CaseId) -> Response:
        return start("test", lambda job: _run_tests(workspace, job, only=body.id))

    @app.post("/api/run")
    async def run(body: _Message) -> Response:
        return _stream(conversation.chat(body.message, body.thread_id or uuid.uuid4().hex))

    @app.post("/api/run/resume")
    async def resume(body: _Resume) -> Response:
        if not conversation.claim(body.thread_id):  # a double click: the first one is on its way
            return failure(RuntimeError("This decision was already sent; wait for the answer"), 409)
        decisions = [(item.id, item.approve, item.note) for item in body.decisions]
        return _stream(conversation.decide(body.thread_id, decisions))

    @app.get("/api/deploy")
    async def deploy() -> dict[str, Any]:
        return workspace.deploy()

    @app.post("/api/trigger")
    async def add_trigger(body: _Trigger) -> dict[str, Any]:
        return workspace.add_trigger(body.kind, body.name, body.settings)

    @app.post("/api/trigger/run")
    async def run_sample(body: _Sample) -> dict[str, Any]:
        return await workspace.operation.run_sample(body.trigger, body.values)

    @app.get("/api/runs")
    async def runs() -> dict[str, Any]:
        return await workspace.operation.overview()

    @app.post("/api/runs/continue")
    async def continue_run(body: _RunId) -> dict[str, Any]:
        return await workspace.operation.continue_run(body.run)

    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {"approvals": await workspace.approvals()}

    @app.post("/api/approvals")
    async def decide(body: _Approval) -> dict[str, Any]:
        return await workspace.decide(
            body.run, body.interrupt_id, approve=body.approve, note=body.note, args=body.args
        )

    @app.get("/api/export")
    async def export() -> Response:
        name = workspace.directory.name or "awa-project"
        return Response(
            workspace.export_zip(),
            media_type="application/zip",
            headers={"Content-Disposition": _attachment(f"{name}.zip")},
        )

    upload_limit = workspace.data.store.max_file_bytes + _UPLOAD_SLACK
    app.add_middleware(BodyLimits, limit=_MAX_BODY, larger={"/api/data/files": upload_limit})
    app.add_middleware(Guard, token=secret, hosts=hosts)
    return app


def page_source() -> str:
    """The page: `page.html` with the stylesheet `page.css` and the scripts of `js/` (in file-name
    order) inlined."""

    package = files("awa.ui")
    scripts = sorted(
        (part for part in package.joinpath("js").iterdir() if part.name.endswith(".js")),
        key=lambda part: part.name,
    )
    script = "\n".join(part.read_text(encoding="utf-8") for part in scripts)
    style = package.joinpath("page.css").read_text(encoding="utf-8")
    page = package.joinpath("page.html").read_text(encoding="utf-8")
    return page.replace("{{style}}", style).replace("{{script}}", script)


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def _stream(frames: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(
        frames, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


async def _run_tests(project: Project, job: _Job, *, only: str | None = None) -> dict[str, Any]:
    from awa.evals import evaluate

    cases = project.dataset().cases
    if only is not None:
        cases = tuple(case for case in cases if case.id == only)
        if not cases:
            raise ValueError(f"There is no eval case {only!r}")
    project.build_system()  # blueprint and tool errors surface before any run
    limits = project.usage_limits()
    editor = project.case_editor
    job.emit("started", {"total": len(cases)})
    gate = asyncio.Semaphore(_TEST_CONCURRENCY)

    async def one(case: EvalCase) -> bool:
        variant = editor.blueprint_for(case)  # the case asked for a stand-in

        def build() -> AgentLike:
            return project.build_system(variant)

        async with gate:
            report = await evaluate(build, [case], timeout=_TEST_TIMEOUT, usage_limits=limits)
        result = report.cases[0]
        job.emit("case", _case_json(case, result, project))
        return result.status == "passed"

    passed = sum(await asyncio.gather(*(one(case) for case in cases)))
    return {"passed": passed, "total": len(cases)}


def _case_json(case: EvalCase, result: CaseResult, project: Project) -> dict[str, Any]:
    error = project.explain(RuntimeError(result.error)).message if result.error else ""
    output = clip(project.redacted(text_of(result.output))) if result.output is not None else ""
    return {
        "id": case.id,
        "status": result.status,
        "input": clip(project.redacted(text_of(case.input))),
        "output": output,
        "failed": [
            {"check": score.name, "reason": project.redacted(score.error or score.reason)}
            for score in result.scores
            if not score.passed
        ],
        "error": error,
    }


def _attachment(filename: str) -> str:
    """A `Content-Disposition` header for any file name: an ASCII fallback plus the UTF-8 name."""

    fallback = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\%' else "_" for c in filename
    )
    return f"attachment; filename=\"{fallback}\"; filename*=utf-8''{quote(filename)}"
