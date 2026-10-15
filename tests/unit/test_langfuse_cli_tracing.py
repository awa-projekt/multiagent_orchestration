"""The `AWA_LANGFUSE_TRACING` switch of the command line and its `awa doctor` check."""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http")

from _helpers.langfuse_receiver import Receiver
from awa import Agent  # noqa: E402
from awa.cli import _doctor as doctor  # noqa: E402
from awa.cli._tracing import TRACED_COMMANDS, langfuse_tracing  # noqa: E402
from awa.core.settings import Settings  # noqa: E402
from awa.errors import ConfigurationError  # noqa: E402
from awa.runtime import global_subscribers  # noqa: E402
from awa.testing import FakeChatModel  # noqa: E402

LANGFUSE_VARIABLES = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
    "AWA_LANGFUSE_TRACING",
    "AWA_LANGFUSE_CAPTURE_CONTENT",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in LANGFUSE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env of the developer's checkout


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    rec = Receiver()
    rec.thread.start()
    yield rec
    rec.server.shutdown()
    rec.server.server_close()


class Args:
    def __init__(self, command: str, blueprint: str | None = None) -> None:
        self.command = command
        self.blueprint = blueprint


def run_traced(args: Args) -> None:
    agent = Agent("solo", model=FakeChatModel(["done"]))
    with langfuse_tracing(args):
        asyncio.run(agent.run("hi"))


def test_tracing_is_off_unless_the_setting_is_on(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_BASE_URL", receiver.url)
    before = len(global_subscribers())

    run_traced(Args("run"))

    assert receiver.bodies == []
    assert len(global_subscribers()) == before


def test_the_setting_sends_every_run_of_a_traced_command_and_cleans_up(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AWA_LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_BASE_URL", receiver.url)
    blueprint = tmp_path / "blueprint.yaml"
    blueprint.write_text("name: refund-desk\nagents: []\n", encoding="utf-8")
    before = len(global_subscribers())

    run_traced(Args("run", str(blueprint)))

    spans = receiver.spans()
    assert [s.name for s in spans] == ["chat", "invoke_agent solo"]
    root = next(s for s in spans if s.name == "invoke_agent solo")
    assert root.attributes.get("langfuse.observation.type") == "agent"
    assert root.attributes.get("langfuse.trace.metadata.blueprint") == "refund-desk"
    assert root.attributes.get("langfuse.trace.metadata.command") == "run"
    assert list(root.attributes.get("langfuse.trace.tags")) == [
        "awa",
        "awa run",
        "blueprint:refund-desk",
    ]
    assert "gen_ai.input.messages" not in root.attributes  # content is off by default
    assert len(global_subscribers()) == before


def test_content_capture_is_its_own_setting(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in (
        ("AWA_LANGFUSE_TRACING", "true"),
        ("AWA_LANGFUSE_CAPTURE_CONTENT", "true"),
        ("LANGFUSE_PUBLIC_KEY", "pk"),
        ("LANGFUSE_SECRET_KEY", "sk"),
        ("LANGFUSE_BASE_URL", receiver.url),
    ):
        monkeypatch.setenv(name, value)

    run_traced(Args("chat"))

    root = next(s for s in receiver.spans() if s.name == "invoke_agent solo")
    assert "hi" in root.attributes.get("gen_ai.input.messages")


def test_commands_that_run_no_system_are_never_traced(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWA_LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_BASE_URL", receiver.url)

    run_traced(Args("validate"))

    assert receiver.bodies == []
    assert {"run", "chat", "serve", "worker"} <= TRACED_COMMANDS
    assert {"build", "data"} <= TRACED_COMMANDS
    assert "doctor" not in TRACED_COMMANDS and "validate" not in TRACED_COMMANDS


def test_the_setting_without_keys_warns_once_and_runs_without_tracing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AWA_LANGFUSE_TRACING", "true")
    before = len(global_subscribers())

    run_traced(Args("run"))  # the run itself is not blocked

    err = capsys.readouterr().err
    assert err.count("warning: Langfuse tracing is off") == 1
    assert "LANGFUSE_PUBLIC_KEY" in err and "AWA_LANGFUSE_STRICT" in err
    assert len(global_subscribers()) == before


def test_the_strict_setting_stops_a_command_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_LANGFUSE_TRACING", "true")
    monkeypatch.setenv("AWA_LANGFUSE_STRICT", "true")

    with pytest.raises(ConfigurationError, match="LANGFUSE_PUBLIC_KEY"):
        run_traced(Args("run"))


def test_the_keys_can_come_from_the_env_file(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        f"AWA_LANGFUSE_TRACING=true\nLANGFUSE_PUBLIC_KEY=pk\nLANGFUSE_SECRET_KEY=sk\n"
        f"LANGFUSE_BASE_URL={receiver.url}\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")  # the suite turns it off

    run_traced(Args("serve"))

    assert any(s.name == "invoke_agent solo" for s in receiver.spans())


def lf_settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def test_doctor_has_no_langfuse_check_while_tracing_is_off(tmp_path: Path) -> None:
    report = doctor.run_doctor(settings=lf_settings(), cwd=tmp_path)

    assert report.check("langfuse") is None


@pytest.mark.slow
def test_doctor_fails_tracing_without_keys_and_needs_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = doctor.safe_find_spec
    monkeypatch.setattr(
        doctor,
        "safe_find_spec",
        lambda name: None if name.startswith("opentelemetry") else real(name),
    )

    report = doctor.run_doctor(settings=lf_settings(langfuse_tracing=True), cwd=tmp_path)

    check = report.check("langfuse")
    assert check is not None and check.status == "warn" and "keys" in check.summary
    assert "AWA_LANGFUSE_STRICT" in (check.fix or "")
    strict = doctor.run_doctor(
        settings=lf_settings(langfuse_tracing=True, langfuse_strict=True), cwd=tmp_path
    ).check("langfuse")
    assert strict is not None and strict.status == "fail"
    extra = report.check("extra:langfuse")
    assert extra is not None and extra.status == "fail"
    assert "Langfuse tracing" in extra.summary
    assert not report.usable


@pytest.mark.slow
def test_doctor_contacts_langfuse_only_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def contact(base: str, public: str, secret: str) -> str | None:
        calls.append(base)
        return None

    monkeypatch.setattr(doctor, "_contact_langfuse", contact)
    configured = lf_settings(
        langfuse_tracing=True,
        langfuse_public_key="pk-lf-1",
        langfuse_secret_key="sk-lf-1",
        langfuse_base_url="http://127.0.0.1:3000/",
    )

    passive = doctor.run_doctor(settings=configured, cwd=tmp_path).check("langfuse")
    assert passive is not None and passive.status == "ok" and calls == []
    active = doctor.run_doctor(settings=configured, cwd=tmp_path, check_langfuse=True)
    check = active.check("langfuse")
    assert check is not None and check.status == "ok" and check.data["reachable"] is True
    assert calls == ["http://127.0.0.1:3000"]


def test_doctor_reports_an_unreachable_langfuse_without_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor, "_contact_langfuse", lambda base, public, secret: f"refused with {secret}"
    )
    configured = lf_settings(
        langfuse_tracing=True, langfuse_public_key="pk-lf-1", langfuse_secret_key="sk-lf-xyz9"
    )

    report = doctor.run_doctor(settings=configured, cwd=tmp_path, check_langfuse=True)

    check = report.check("langfuse")
    assert check is not None and check.status == "fail"
    assert "sk-lf-xyz9" not in json.dumps(report.to_dict())
    assert not report.usable


def test_doctor_contact_tells_rejected_keys_from_a_missing_server() -> None:
    received: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            received.append(self.path)
            status = 401 if self.path.endswith("/projects") else 200
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert doctor._contact_langfuse(base, "pk", "sk") == (
            "/api/public/projects: the keys were rejected"
        )
        assert received == ["/api/public/health", "/api/public/projects"]
    finally:
        server.shutdown()
        server.server_close()
    message = doctor._contact_langfuse(base, "pk", "sk")
    assert message is not None and message.startswith("/api/public/health:")
