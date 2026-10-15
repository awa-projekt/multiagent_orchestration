"""Helpers for the tests of the data endpoints: principals, an app factory, a scripted build engine.

`ScriptedBuildEngine` stands in for `awa.blueprints.build_project`: it follows a script (events,
questions, a failure, a gate to wait on) and writes a small but real project folder, so the endpoints
are tested end to end without a model call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from _helpers.fake_secrets import fake_secret
from awa import Agent
from awa.data import profile
from awa.server import ApiKeyAuthenticator, DataOptions, Principal, ServedSystem, create_app
from awa.server.ports import BuildOutcome, BuildSpec, Emit, Question
from awa.testing import FunctionChatModel

BASE = "http://testserver"
ALICE = Principal("alice", tenant="acme", roles={"builder"})
BOB = Principal("bob", tenant="acme", roles={"builder"})
RITA = Principal("rita", tenant="acme", roles={"builder", "approver"})
GUS = Principal("gus", tenant="globex", roles={"builder"})
VIEWER = Principal("vera", tenant="acme")
KEYS = {"k-alice": ALICE, "k-bob": BOB, "k-rita": RITA, "k-gus": GUS, "k-vera": VIEWER}
CSV = b"id;amount\n1;2,50\n2;3,00\n"
GOOD = BuildOutcome(
    passed=True,
    pass_rate=1.0,
    rounds=1,
    stopped="target_reached",
    target=1.0,
    summary="order_desk: 3 of 3 eval cases pass (100%) after 1 round(s); target reached.",
    flaky=("case_2",),
    assumptions=("Amounts are in euro",),
    usage={"requests": 12, "total_tokens": 3456},
)


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def csv_file(
    name: str = "orders.csv", content: bytes = CSV
) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (name, content, "text/csv"))]


class ScriptedBuildEngine:
    """A `BuildEngine` that follows a script instead of calling a model.

    Each call takes the next entry of `results`: a `BuildOutcome` (written as a project), a list of
    `Question`, an exception to raise, or a function of the `BuildSpec` that returns one of those.
    Before it returns, a call emits `events` and, with `gate`, waits until the test opens it
    (to look at, cancel or overlap a running build).
    """

    def __init__(
        self,
        *results: BuildOutcome | list[Question] | BaseException | Callable[[BuildSpec], Any],
        events: tuple[tuple[str, Mapping[str, Any]], ...] = (
            ("data.profile.file", {"stage": "profile", "name": "orders.csv", "status": "ok"}),
            ("design.acceptance", {"stage": "design", "criteria": 3}),
            ("eval.round.finished", {"stage": "evaluate", "round": 1, "pass_rate": 1.0}),
        ),
        gate: asyncio.Event | None = None,
        emit_paths: bool = False,
    ) -> None:
        self.results = list(results or [GOOD])
        self.events = events
        self.gate = gate
        self.emit_paths = emit_paths
        self.calls: list[BuildSpec] = []
        self.data_seen: list[dict[str, bytes]] = []
        self.started = asyncio.Event()

    async def __call__(self, spec: BuildSpec, emit: Emit) -> BuildOutcome:
        self.calls.append(spec)
        self.data_seen.append(
            {
                p.relative_to(spec.data).as_posix(): p.read_bytes()
                for p in spec.data.rglob("*")
                if p.is_file()
            }
        )
        self.started.set()
        if self.emit_paths:
            emit("build.progress", {"message": f"reading {spec.data}, writing {spec.out_dir}"})
        for event, data in self.events:
            emit(event, data)
            await asyncio.sleep(0)
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if callable(result) and not isinstance(result, BuildOutcome):
            result = result(spec)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, list):
            return BuildOutcome(questions=tuple(result))
        write_project(spec)
        return result


def write_project(spec: BuildSpec) -> None:
    """The files a build leaves behind: a blueprint, a README, a profile, and a secret that must not leak."""

    out = spec.out_dir
    (out / "evals").mkdir(parents=True, exist_ok=True)
    (out / "data" / "files").mkdir(parents=True, exist_ok=True)
    if not (out / "blueprint.yaml").exists():  # a test may have written a real blueprint already
        (out / "blueprint.yaml").write_text("name: order_desk\n", encoding="utf-8", newline="\n")
    (out / "README.md").write_text("# Order desk\n", encoding="utf-8", newline="\n")
    (out / "evals" / "cases.yaml").write_text("cases: []\n", encoding="utf-8", newline="\n")
    (out / ".env.example").write_text("ORDERS_URL=\n", encoding="utf-8", newline="\n")
    (out / ".env").write_text("ORDERS_KEY=sk-live-secret\n", encoding="utf-8", newline="\n")
    (out / "signing.key").write_text(
        fake_secret("-----BEGIN ", "PRIVATE KEY-----"), encoding="utf-8", newline="\n"
    )
    (out / "data" / "PROFILE.json").write_text(
        profile(spec.data, root=spec.data).to_json(), encoding="utf-8"
    )
    for path in spec.data.rglob("*"):
        if path.is_file():
            (out / "data" / "files" / path.name).write_bytes(path.read_bytes())


def echo_system(project: Path) -> ServedSystem:
    """The system every built project runs as in the tests: an agent that echoes its input."""

    return ServedSystem(
        Agent(
            "order_desk", model=FunctionChatModel(lambda call: f"done: {call.last_message.content}")
        )
    )


def make_app(
    root: Path | None = None,
    *,
    engine: Any = None,
    real: bool = False,
    keys: Mapping[str, Principal] = KEYS,
    system_factory: Callable[[Path], ServedSystem] = echo_system,
    options: Mapping[str, Any] | None = None,
    **app_options: Any,
) -> FastAPI:
    """An app with one echo system and the data endpoints on `root` (a temporary folder without it).

    With `real` the data endpoints use their own default engine and system factory.
    """

    return create_app(
        Agent("echo", model=FunctionChatModel(lambda call: f"echo: {call.last_message.content}")),
        authenticator=ApiKeyAuthenticator(dict(keys)),
        data=DataOptions(
            root=root,
            engine=None if real else (engine if engine is not None else ScriptedBuildEngine()),
            system_factory=None if real else system_factory,
            **(options or {}),
        ),
        **app_options,
    )


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield client


async def upload(
    client: httpx.AsyncClient, key: str = "k-alice", name: str = "orders.csv", content: bytes = CSV
) -> str:
    response = await client.post("/v1/uploads", headers=auth(key), files=csv_file(name, content))
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


async def start_build(
    client: httpx.AsyncClient,
    upload_id: str,
    key: str = "k-alice",
    description: str = "Check each order against the price list.",
    **extra: Any,
) -> httpx.Response:
    return await client.post(
        "/v1/builds",
        headers=auth(key),
        json={"description": description, "upload_id": upload_id, **extra},
    )


async def wait_for(
    client: httpx.AsyncClient, build_id: str, status: str, key: str = "k-alice"
) -> dict[str, Any]:
    """Poll a build until it has `status` (the test fails after 5 seconds)."""

    for _ in range(500):
        body = (await client.get(f"/v1/builds/{build_id}", headers=auth(key))).json()
        if body["status"] == status:
            return dict(body)
        await asyncio.sleep(0.01)
    raise AssertionError(f"build {build_id} did not reach {status}: {body}")


def sse_events(text: str) -> list[dict[str, Any]]:
    """The events of an SSE body: each frame's `id`, `event` and decoded `data`."""

    events = []
    for frame in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields:
            events.append(
                {
                    "id": fields.get("id"),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return events
