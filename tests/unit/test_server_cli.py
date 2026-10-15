"""Tests for `awa serve` (`register_cli`), `build_blueprint_app` and `serve`."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import awa.server._app as server_app
from awa.cli import build_parser
from awa.errors import ConfigurationError, MissingConfigurationError
from awa.models import ModelRegistry
from awa.server import _blueprint_app as server_cli
from awa.server import build_blueprint_app, serve
from awa.testing import FakeChatModel, tool_call

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "blueprints"

TOOLS_MODULE = '''
from langchain_core.tools import tool

CALLS = []


@tool
def pay(order_id: str) -> str:
    """Pay an order."""
    CALLS.append(order_id)
    return f"paid {order_id}"
'''

GATED = """
name: payments
agents:
  - name: cashier
    description: Pays orders.
    tools: [pay]
approvals:
  - tool: pay
"""


class FakeUvicorn:
    """Records what would be served by `Server(Config(...)).run()`."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.servers: list[Any] = []
        self.shutdowns = 0
        fake = self

        class Config:
            def __init__(self, app: Any, **options: Any) -> None:
                self.app = app
                self.options = options

        class Server:
            def __init__(self, config: Config) -> None:
                self.config = config

            def run(self) -> None:
                fake.servers.append(self)
                fake.calls.append((self.config.app, self.config.options))

            async def shutdown(self, sockets: Any = None) -> None:
                fake.shutdowns += 1

        self.Config = Config
        self.Server = Server


@pytest.fixture
def uvicorn(monkeypatch: pytest.MonkeyPatch) -> FakeUvicorn:
    fake = FakeUvicorn()
    monkeypatch.setattr(server_app, "import_uvicorn", lambda: fake)
    return fake


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "serve_test_tools.py").write_text(TOOLS_MODULE, encoding="utf-8")
    (tmp_path / "gated.yaml").write_text(GATED, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "serve_test_tools", raising=False)
    return tmp_path


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


def parser() -> argparse.ArgumentParser:
    return build_parser(["serve"])


def test_register_cli_parses_every_option() -> None:
    args = parser().parse_args(
        [
            "serve",
            "support.yaml",
            "--tools",
            "tools.py",
            "--tools",
            "more",
            "--skills",
            "skills/",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--api-key-env",
            "SUPPORT_KEY",
            "--cors-origin",
            "https://a.example.com",
            "--log-level",
            "debug",
        ]
    )
    assert args.handler.__name__ == "_serve"
    assert (args.blueprint, args.tools, args.skills) == (
        "support.yaml",
        ["tools.py", "more"],
        ["skills/"],
    )
    assert (args.host, args.port, args.api_key_env) == ("0.0.0.0", 9000, "SUPPORT_KEY")
    assert args.cors_origins == ["https://a.example.com"]
    assert args.log_level == "debug"

    defaults = parser().parse_args(["serve", "bp.yaml"])
    assert (defaults.host, defaults.port, defaults.api_key_env) == ("127.0.0.1", 8000, None)
    assert defaults.tools == [] and defaults.cors_origins == []


def test_serve_command_builds_the_blueprint_and_runs_uvicorn(
    workspace: Path, uvicorn: FakeUvicorn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAY_KEY", "k-123")
    args = parser().parse_args(
        ["serve", "gated.yaml", "--tools", "serve_test_tools.py", "--api-key-env", "PAY_KEY"]
    )
    assert args.handler(args) == 0
    [(app, options)] = uvicorn.calls
    assert isinstance(app, FastAPI) and app.title == "payments"
    assert options == {
        "host": "127.0.0.1",
        "port": 8000,
        "log_level": "info",
        "timeout_graceful_shutdown": 35,
    }


def test_missing_api_key_variable_is_a_configuration_error(
    workspace: Path, uvicorn: FakeUvicorn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    args = parser().parse_args(["serve", "gated.yaml", "--api-key-env", "NO_SUCH_KEY"])
    from awa.cli._preflight import PreflightFailed

    # The pre-flight stops `awa serve` (exit code 78) before the port is bound.
    with pytest.raises(PreflightFailed, match="NO_SUCH_KEY is not set"):
        args.handler(args)
    assert uvicorn.calls == []
    assert issubclass(PreflightFailed, Exception) and MissingConfigurationError


def test_public_host_without_key_is_refused_unless_allowed(
    workspace: Path, uvicorn: FakeUvicorn, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["serve", "gated.yaml", "--tools", "serve_test_tools.py", "--host", "0.0.0.0"]
    args = parser().parse_args(argv)
    with pytest.raises(ConfigurationError, match="--allow-unauthenticated"):
        args.handler(args)
    assert uvicorn.calls == []

    args = parser().parse_args([*argv, "--allow-unauthenticated"])
    assert args.handler(args) == 0
    assert "without an API key" in capsys.readouterr().err


async def test_blueprint_app_serves_approvals_with_an_in_memory_checkpointer(
    workspace: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"}), "Paid."]))
    app = build_blueprint_app("gated.yaml", tools=["serve_test_tools.py"], api_key="key")
    headers = {"Authorization": "Bearer key"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers=headers
    ) as client:
        description = (await client.get("/v1/systems/cashier")).json()
        paused = (await client.post("/v1/systems/cashier/runs", json={"input": "Pay 9"})).json()
        thread = paused["thread_id"]
        pending = (await client.get(f"/v1/systems/cashier/threads/{thread}/interrupts")).json()
        done = await client.post(
            f"/v1/systems/cashier/threads/{thread}/resume", json={"decision": {"action": "approve"}}
        )
    assert description["threads"] is True and description["mermaid"].startswith("flowchart")
    assert paused["status"] == "interrupted"
    assert pending["interrupts"][0]["value"]["tool"] == "pay"
    assert done.json()["output"] == "Paid."
    assert sys.modules["serve_test_tools"].CALLS == ["9"]


async def test_a_blueprint_whose_policy_pauses_calls_is_served_with_threads(
    workspace: Path, models: ModelRegistry
) -> None:
    (workspace / "asking.yaml").write_text(
        "name: asking\nagents:\n  - name: cashier\n    tools: [pay]\n"
        "tool_risks: {pay: [external_action]}\npolicies:\n  - ask: [external_action]\n",
        encoding="utf-8",
    )
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"})]))
    app = build_blueprint_app("asking.yaml", tools=["serve_test_tools.py"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        paused = (await client.post("/v1/systems/cashier/runs", json={"input": "Pay 9"})).json()
    assert paused["status"] == "interrupted", paused
    assert sys.modules["serve_test_tools"].CALLS == []


def test_example_team_blueprint_is_served_as_a_team() -> None:
    app = build_blueprint_app(
        EXAMPLES / "support_triage.yaml", tools=[str(EXAMPLES / "support_tools.py")]
    )
    system = app.state.awa_server.systems["support-triage"]
    assert type(system.system).__name__ == "Team"


def test_serve_runs_the_app_with_uvicorn(uvicorn: FakeUvicorn) -> None:
    from awa import Agent

    serve(Agent("echo", model=FakeChatModel(["hi"])), host="0.0.0.0", port=9001, api_key="k")
    [(app, options)] = uvicorn.calls
    assert isinstance(app, FastAPI)
    assert options == {
        "host": "0.0.0.0",
        "port": 9001,
        "log_level": "info",
        "timeout_graceful_shutdown": 35,
    }


def test_serve_refuses_an_open_host_without_authentication_unless_allowed(
    uvicorn: FakeUvicorn,
) -> None:
    from awa import Agent

    def echo() -> Agent:
        return Agent("echo", model=FakeChatModel(["hi"]))

    with pytest.raises(ConfigurationError, match="allow_unauthenticated"):
        serve(echo(), host="0.0.0.0")  # noqa: S104
    assert uvicorn.calls == []
    serve(echo(), host="0.0.0.0", allow_unauthenticated=True)  # noqa: S104
    serve(echo(), host="127.0.0.1")
    assert [options["host"] for _, options in uvicorn.calls] == ["0.0.0.0", "127.0.0.1"]  # noqa: S104


def test_register_cli_parses_the_operations_options() -> None:
    args = parser().parse_args(
        [
            "serve",
            "bp.yaml",
            "--api-keys-file",
            "keys.json",
            "--thread-scope",
            "principal",
            "--rate-limit",
            "60/30",
            "--max-concurrency",
            "8",
            "--approver-role",
            "approver",
            "--drain-timeout",
            "12.5",
            "--max-body-bytes",
            "2048",
            "--log-format",
            "json",
        ]
    )
    assert (args.api_keys_file, args.thread_scope, args.rate_limit) == (
        "keys.json",
        "principal",
        "60/30",
    )
    assert (args.max_concurrency, args.approver_roles, args.drain_timeout) == (
        8,
        ["approver"],
        12.5,
    )
    assert (args.max_body_bytes, args.log_format) == (2048, "json")
    defaults = parser().parse_args(["serve", "bp.yaml"])
    assert (defaults.thread_scope, defaults.rate_limit, defaults.log_format) == (
        "tenant",
        None,
        "text",
    )
    for bad in (["--rate-limit", "fast"], ["--max-concurrency", "0"]):
        with pytest.raises(SystemExit):
            parser().parse_args(["serve", "bp.yaml", *bad])
    with pytest.raises(SystemExit):  # one way to authenticate
        parser().parse_args(["serve", "bp.yaml", "--api-key-env", "K", "--api-keys-file", "f"])


def test_parse_rate_limit() -> None:
    limit = server_cli.parse_rate_limit(" 10 / 1.5 ")
    assert (limit.requests, limit.per) == (10, 1.5)
    with pytest.raises(ValueError, match="REQUESTS/SECONDS"):
        server_cli.parse_rate_limit("10 per minute")


async def test_api_keys_file_maps_digests_and_variables_to_principals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from awa.server import Principal

    monkeypatch.setenv("OPS_KEY", "ops-secret")
    keys = tmp_path / "keys.json"
    keys.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "id": "billing",
                        "tenant": "acme",
                        "sha256": hashlib.sha256(b"bill-secret").hexdigest(),
                    },
                    {"id": "ops", "tenant": "acme", "roles": ["approver"], "key_env": "OPS_KEY"},
                ]
            }
        ),
        encoding="utf-8",
    )
    authenticator = server_cli.load_api_keys(keys)

    class Request:
        def __init__(self, key: str) -> None:
            self.headers = {"authorization": f"Bearer {key}"}

    assert await authenticator.authenticate(Request("bill-secret")) == Principal(
        "billing", tenant="acme"
    )
    ops = await authenticator.authenticate(Request("ops-secret"))
    assert ops.roles == frozenset({"approver"})

    monkeypatch.delenv("OPS_KEY")
    with pytest.raises(MissingConfigurationError, match="OPS_KEY"):
        server_cli.load_api_keys(keys)
    keys.write_text('{"keys": [{"id": "x"}]}', encoding="utf-8")
    with pytest.raises(MissingConfigurationError, match="sha256"):
        server_cli.load_api_keys(keys)
    with pytest.raises(MissingConfigurationError, match="Invalid API keys file"):
        server_cli.load_api_keys(tmp_path / "missing.json")
    keys.write_text('{"keys": [{"id": "x", "sha256": "not-hex"}]}', encoding="utf-8")
    with pytest.raises(MissingConfigurationError, match="64 hex characters"):
        server_cli.load_api_keys(keys)


def test_serve_command_applies_operations_options(workspace: Path, uvicorn: FakeUvicorn) -> None:
    import hashlib

    keys = workspace / "keys.json"
    digest = hashlib.sha256(b"k").hexdigest()
    keys.write_text(json.dumps([{"id": "svc", "tenant": "acme", "sha256": digest}]), "utf-8")
    args = parser().parse_args(
        [
            "serve",
            "gated.yaml",
            "--tools",
            "serve_test_tools.py",
            "--api-keys-file",
            str(keys),
            "--host",
            "0.0.0.0",
            "--rate-limit",
            "5/60",
            "--max-concurrency",
            "2",
            "--approver-role",
            "approver",
            "--drain-timeout",
            "3",
            "--log-format",
            "json",
        ]
    )
    assert args.handler(args) == 0
    [(app, options)] = uvicorn.calls
    served = app.state.awa_server.systems["cashier"]
    assert served.max_concurrency == 2 and served.approver_roles == frozenset({"approver"})
    assert app.state.awa_server.drain_timeout == 3
    assert options["log_config"]["formatters"]["json"]
    assert options["timeout_graceful_shutdown"] == 8

    # Shutting the server down drains the app first.
    import asyncio

    [server] = uvicorn.servers
    asyncio.run(server.shutdown())
    assert app.state.awa_server.runs.draining and uvicorn.shutdowns == 1


def test_serve_accepts_create_app_options_and_json_logs(uvicorn: FakeUvicorn) -> None:
    from awa import Agent
    from awa.server import RateLimit

    serve(
        Agent("echo", model=FakeChatModel(["hi"])),
        json_logs=True,
        drain_timeout=None,
        rate_limit=RateLimit(1),
    )
    [(app, options)] = uvicorn.calls
    assert "timeout_graceful_shutdown" not in options and "log_config" in options
    assert app.state.awa_server.drain_timeout is None


MEMORY_BLUEPRINT = """
name: concierge
agents:
  - name: concierge
    description: Remembers preferences.
    memory:
      scope: user
      user_id: {source}
"""


async def test_blueprint_memory_user_id_source_configures_the_served_deps(
    tmp_path: Path, models: ModelRegistry
) -> None:
    from awa.server import ApiKeyAuthenticator, Principal

    models.register("default", FakeChatModel(["noted"], repeat_last=True))
    principal_file = tmp_path / "principal.yaml"
    principal_file.write_text(MEMORY_BLUEPRINT.format(source="principal"), encoding="utf-8")
    deps_file = tmp_path / "deps.yaml"
    deps_file.write_text(MEMORY_BLUEPRINT.format(source="deps"), encoding="utf-8")

    keys = ApiKeyAuthenticator({"k": Principal("u-1", tenant="acme")})
    from_principal = build_blueprint_app(principal_file, authenticator=keys)
    served = from_principal.state.awa_server.systems["concierge"]
    assert served.allowed_deps == frozenset() and served.principal_deps is not None
    assert served.principal_deps(Principal("u-1")) == {"user_id": "u-1"}
    transport = httpx.ASGITransport(app=from_principal)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        ok = await client.post(
            "/v1/systems/concierge/runs",
            json={"input": "hi"},
            headers={"Authorization": "Bearer k"},
        )
        spoofed = await client.post(
            "/v1/systems/concierge/runs",
            json={"input": "hi", "deps": {"user_id": "u-2"}},
            headers={"Authorization": "Bearer k"},
        )
    assert ok.status_code == 200 and spoofed.status_code == 400

    from_deps = build_blueprint_app(deps_file).state.awa_server.systems["concierge"]
    assert from_deps.allowed_deps == frozenset({"user_id"}) and from_deps.principal_deps is None


async def _status(app: FastAPI, host: str, path: str = "/v1/systems") -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
        return (await client.get(path)).status_code


async def test_serve_without_an_api_key_only_answers_its_own_host_names(
    workspace: Path, uvicorn: FakeUvicorn, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel(["hi"]))
    argv = ["serve", "gated.yaml", "--tools", "serve_test_tools.py"]
    assert parser().parse_args(argv).handler(parser().parse_args(argv)) == 0
    args = parser().parse_args(
        [*argv, "--host", "10.0.0.5", "--allowed-host", "awa.internal", "--allow-unauthenticated"]
    )
    assert args.handler(args) == 0
    [(local, _), (bound, _)] = uvicorn.calls

    # DNS rebinding: the page's own name arrives in the Host header.
    assert await _status(local, "evil.example") == 421
    assert await _status(local, "localhost:8000") == 200
    assert await _status(local, "[::1]:8000") == 200
    assert await _status(bound, "10.0.0.5:8000") == 200
    assert await _status(bound, "awa.internal") == 200
    assert await _status(bound, "evil.example") == 421


async def test_serve_documents_its_api_only_on_request_and_never_from_a_cdn(
    workspace: Path, uvicorn: FakeUvicorn, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel(["hi"]))
    off = ["serve", "gated.yaml", "--tools", "serve_test_tools.py"]
    for argv in (off, [*off, "--docs"]):
        args = parser().parse_args(argv)
        assert args.handler(args) == 0
    [(plain, _), (documented, _)] = uvicorn.calls

    assert await _status(plain, "localhost", "/openapi.json") == 404
    assert await _status(documented, "localhost", "/openapi.json") == 200
    assert await _status(documented, "localhost", "/docs") == 404  # no viewer page, no CDN script


async def test_openapi_document_needs_the_api_key(
    workspace: Path, uvicorn: FakeUvicorn, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVE_TEST_KEY", "k-1")
    args = parser().parse_args(
        ["serve", "gated.yaml", "--tools", "serve_test_tools.py", "--docs"]
        + ["--api-key-env", "SERVE_TEST_KEY"]
    )
    assert args.handler(args) == 0
    [(app, _)] = uvicorn.calls
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://anything.example") as http:
        assert (await http.get("/openapi.json")).status_code == 401
        keyed = await http.get("/openapi.json", headers={"Authorization": "Bearer k-1"})
    assert keyed.status_code == 200


def test_the_host_check_follows_the_keys_and_the_allowed_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVE_TEST_KEY", "k-1")
    base = {"blueprint": "b.yaml", "tools": [], "skills": [], "cors_origins": []}

    def hosts(**options: Any) -> list[str] | None:
        return server_cli.ServeConfig(**base, **options).app_options().get("allowed_hosts")  # type: ignore[arg-type]

    assert hosts(api_key_env="SERVE_TEST_KEY") is None  # a key protects it, any Host will do
    assert hosts(api_key_env=None) == ["localhost", "127.0.0.1", "[::1]", "127.0.0.1"]
    assert hosts(api_key_env=None, host="0.0.0.0") == ["localhost", "127.0.0.1", "[::1]"]
    assert hosts(api_key_env=None, host="::2") == ["localhost", "127.0.0.1", "[::1]", "[::2]"]
    keyed = hosts(api_key_env="SERVE_TEST_KEY", allowed_hosts=["awa.internal"])
    assert keyed is not None and keyed[0] == "awa.internal" and "localhost" in keyed


async def test_the_allowed_hosts_variable_reaches_the_host_check(
    workspace: Path, uvicorn: FakeUvicorn, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    models.register("default", FakeChatModel(["hi"]))
    monkeypatch.setenv("AWA_ALLOWED_HOSTS", "awa.internal")
    args = parser().parse_args(
        [
            "serve",
            "gated.yaml",
            "--tools",
            "serve_test_tools.py",
            "--host",
            "10.0.0.5",
            "--allow-unauthenticated",
        ]
    )
    assert args.handler(args) == 0
    [(bound, _)] = uvicorn.calls

    assert await _status(bound, "awa.internal") == 200
    assert await _status(bound, "evil.example") == 421


def test_serve_types_every_keyword_of_create_app() -> None:
    keywords = {
        name
        for name, parameter in inspect.signature(server_app.create_app).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }

    assert set(server_app._AppKw.__annotations__) == keywords | set(
        server_app.ServingKw.__annotations__
    )
