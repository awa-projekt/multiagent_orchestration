"""`awa.toolkits.files` and `awa.toolkits.communication`."""

from __future__ import annotations

import json
import os
import socketserver
import sys
import threading
from collections.abc import Iterator
from email import message_from_bytes
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent
from awa._internal.upstream.tools import ToolException
from awa.errors import ConfigurationError
from awa.testing import FakeChatModel, tool_call
from awa.toolkits import Sandbox, email_toolkit, file_toolkit, template_toolkit, webhook_toolkit
from awa.tools.approval import APPROVAL_METADATA_KEY
from awa.tools.hardening import FRAMED_METADATA_KEY
from awa.tools.risk import declared_tool_risk


def tools_by_name(root: Path, **options: Any) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in file_toolkit(root, **options)}


def call(tool: BaseTool, **args: Any) -> str:
    result = tool.invoke(args)
    assert isinstance(result, str)
    return result


@pytest.fixture
def area(tmp_path: Path) -> Path:
    root = tmp_path / "area"
    (root / "reports").mkdir(parents=True)
    (root / "reports" / "q1.txt").write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    (root / "notes.md").write_text("# Notes\nhello", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("top secret", encoding="utf-8")
    return root


def link(target: Path, name: Path) -> None:
    """A symbolic link, or a junction on Windows without symlink rights."""

    try:
        os.symlink(target, name, target_is_directory=target.is_dir())
    except OSError:
        if sys.platform != "win32" or not target.is_dir():
            pytest.skip("cannot create links here")
        import _winapi

        _winapi.CreateJunction(str(target), str(name))


def test_move_acts_on_a_link_not_on_the_folder_it_points_to(area: Path) -> None:
    link(area / "reports", area / "alias")
    tools = tools_by_name(area, requires_approval=False)

    tools["move_file"].invoke({"source": "alias", "destination": "moved"})

    assert (area / "reports" / "q1.txt").is_file() and (area / "moved" / "q1.txt").is_file()
    assert not (area / "alias").exists()


def test_delete_acts_on_a_link_not_on_the_file_it_points_to(area: Path) -> None:
    link(area / "notes.md", area / "alias.md")

    tools_by_name(area, requires_approval=False)["delete_file"].invoke({"path": "alias.md"})

    assert (area / "notes.md").is_file() and not (area / "alias.md").is_symlink()


def test_the_toolkit_builds_six_tools_and_read_only_two(area: Path) -> None:
    assert list(tools_by_name(area)) == [
        "list_files",
        "read_file",
        "write_file",
        "append_file",
        "move_file",
        "delete_file",
    ]
    assert list(tools_by_name(area, read_only=True)) == ["list_files", "read_file"]
    assert list(tools_by_name(area, include=["read_file"])) == ["read_file"]
    with pytest.raises(ConfigurationError, match="no tool"):
        file_toolkit(area, include=["format_disk"])
    with pytest.raises(ConfigurationError, match="not an existing directory"):
        file_toolkit(area / "missing")
    with pytest.raises(ConfigurationError, match="max_output_chars"):
        file_toolkit(area, max_output_chars=0)


def test_writing_tools_need_approval_by_default_and_declare_their_risk(area: Path) -> None:
    tools = tools_by_name(area)
    for name in ("write_file", "append_file", "move_file", "delete_file"):
        assert (tools[name].metadata or {})[APPROVAL_METADATA_KEY] is True
        risk = declared_tool_risk(tools[name])
        assert risk is not None and risk.external_action is True
        assert "Needs human approval" in tools[name].description
    reader = declared_tool_risk(tools["read_file"])
    assert reader is not None and reader.untrusted_input and reader.private_data
    assert (tools["read_file"].metadata or {})[FRAMED_METADATA_KEY] is True
    ungated = tools_by_name(area, requires_approval=False)
    assert APPROVAL_METADATA_KEY not in (ungated["write_file"].metadata or {})


def test_list_files_hides_hidden_entries(area: Path) -> None:
    tools = tools_by_name(area)
    listing = call(tools["list_files"])
    assert "notes.md" in listing and "reports/" in listing
    assert ".env" not in listing
    recursive = call(tools["list_files"], recursive=True, pattern="*.txt")
    assert "reports/q1.txt" in recursive and "notes.md" not in recursive
    assert "No entries" in call(tools["list_files"], pattern="*.pdf")


def test_read_file_returns_framed_text_and_windows(area: Path) -> None:
    tools = tools_by_name(area)
    framed = call(tools["read_file"], path="notes.md")
    assert framed.startswith("<tool_output") and "hello" in framed
    plain = tools_by_name(area, frame_output=False)["read_file"]
    assert call(plain, path="reports/q1.txt", start_line=2, max_lines=1) == "line 2\n"
    assert call(plain, path="reports\\q1.txt").startswith("line 1")


def test_read_file_caps_size_and_refuses_binary(area: Path) -> None:
    (area / "big.txt").write_text("x" * 5_000, encoding="utf-8")
    (area / "blob.bin").write_bytes(b"\x00\x01\x02")
    tool = tools_by_name(area, max_read_bytes=100, frame_output=False)["read_file"]
    text = call(tool, path="big.txt")
    assert text.startswith("x" * 100) and "the rest was not read" in text
    assert call(tool, path="blob.bin").startswith("Error: ") and "binary" in call(
        tool, path="blob.bin"
    )


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "reports/../../outside.txt",
        "..\\outside.txt",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "c:outside.txt",
        "~/outside.txt",
        "notes.md:stream",
        "NUL",
        "reports/con.txt",
        ".env",
        "reports/./../.env",
        "notes.md\x00.txt",
    ],
)
def test_traversal_and_special_paths_are_refused(area: Path, path: str) -> None:
    tool = tools_by_name(area)["read_file"]
    result = call(tool, path=path)
    assert result.startswith("Error: ")
    assert "top secret" not in result and "SECRET=1" not in result


def test_links_that_leave_the_root_are_refused(area: Path, tmp_path: Path) -> None:
    secret_dir = tmp_path / "secret_dir"
    secret_dir.mkdir()
    (secret_dir / "keys.txt").write_text("private key", encoding="utf-8")
    link(secret_dir, area / "escape")
    tools = tools_by_name(area, requires_approval=False)

    read = call(tools["read_file"], path="escape/keys.txt")
    assert "outside the allowed directory" in read and "private key" not in read
    listing = call(tools["list_files"], recursive=True)
    assert "escape" not in listing
    written = call(tools["write_file"], path="escape/new.txt", content="x")
    assert written.startswith("Error: ")
    assert not (secret_dir / "new.txt").exists()


def test_a_link_inside_the_root_is_followed(area: Path) -> None:
    link(area / "reports", area / "alias")
    tool = tools_by_name(area, frame_output=False)["read_file"]
    assert call(tool, path="alias/q1.txt").startswith("line 1")


def test_a_link_to_a_hidden_entry_is_refused(area: Path) -> None:
    (area / ".git").mkdir()
    (area / ".git" / "config").write_text("token=abc", encoding="utf-8")
    link(area / ".git", area / "repo")
    tool = tools_by_name(area, frame_output=False)["read_file"]
    result = call(tool, path="repo/config")
    assert "hidden" in result and "token=abc" not in result
    allowed = tools_by_name(area, frame_output=False, allow_hidden=True)["read_file"]
    assert call(allowed, path="repo/config") == "token=abc"


def test_a_recursive_listing_stops_at_its_entry_limit(area: Path) -> None:
    for index in range(5):
        (area / "reports" / f"r{index}.txt").write_text("x", encoding="utf-8")
    listing = call(tools_by_name(area)["list_files"], recursive=True, max_entries=3)
    assert listing.count("\n") == 3 and "stopped after 3 entries" in listing


def test_a_listing_that_fits_its_limit_exactly_is_not_marked_cut(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"r{index}.txt").write_text("x", encoding="utf-8")

    listing = call(tools_by_name(tmp_path)["list_files"], max_entries=3)

    assert listing.count("\n") == 2 and "stopped after" not in listing


def test_write_append_move_delete(area: Path) -> None:
    tools = tools_by_name(area, requires_approval=False)
    assert "Wrote 5 bytes" in call(tools["write_file"], path="out/new.txt", content="hello")
    assert (area / "out" / "new.txt").read_text(encoding="utf-8") == "hello"
    assert "exists" in call(tools["write_file"], path="out/new.txt", content="again")
    call(tools["write_file"], path="out/new.txt", content="again", overwrite=True)
    assert (area / "out" / "new.txt").read_text(encoding="utf-8") == "again"
    call(tools["append_file"], path="out/new.txt", content="!")
    assert (area / "out" / "new.txt").read_text(encoding="utf-8") == "again!"
    assert "Moved" in call(tools["move_file"], source="out/new.txt", destination="archive/a.txt")
    assert (area / "archive" / "a.txt").exists()
    assert "exists" in call(tools["move_file"], source="notes.md", destination="archive/a.txt")
    assert "only files" in call(tools["delete_file"], path="archive")
    assert "Deleted" in call(tools["delete_file"], path="archive/a.txt")
    assert not (area / "archive" / "a.txt").exists()
    assert "cannot be moved" in call(tools["move_file"], source=".", destination="x")
    assert not any(p.name.startswith(".awa-") for p in (area / "out").iterdir())


def test_writes_are_bounded_and_cannot_escape(area: Path, tmp_path: Path) -> None:
    tools = tools_by_name(area, requires_approval=False, max_write_bytes=10)
    assert "limit" in call(tools["write_file"], path="a.txt", content="x" * 11)
    call(tools["write_file"], path="a.txt", content="x" * 8)
    assert "grow" in call(tools["append_file"], path="a.txt", content="xxx")
    assert call(tools["write_file"], path="../evil.txt", content="x").startswith("Error")
    assert call(tools["move_file"], source="a.txt", destination="../evil.txt").startswith("Error")
    assert call(tools["delete_file"], path="../outside.txt").startswith("Error")
    assert (tmp_path / "outside.txt").exists() and not (tmp_path / "evil.txt").exists()


def test_a_missing_file_names_the_files_that_start_like_it(area: Path) -> None:
    (area / "notes.csv").write_text("a,b", encoding="utf-8")
    sandbox = Sandbox(area)
    with pytest.raises(ToolException) as near:
        sandbox.resolve("notes", must_exist=True)
    assert str(near.value) == "'notes' does not exist (did you mean 'notes.csv', 'notes.md'?)"
    with pytest.raises(ToolException) as gone:
        sandbox.resolve("reports/q2.txt", must_exist=True)
    assert str(gone.value) == "'reports/q2.txt' does not exist"
    with pytest.raises(ToolException) as missing_folder:
        sandbox.resolve("nothing/at-all.txt", must_exist=True)
    assert str(missing_folder.value) == "'nothing/at-all.txt' does not exist"


def test_sandbox_allows_hidden_entries_when_asked(area: Path) -> None:
    sandbox = Sandbox(area, allow_hidden=True)
    assert sandbox.resolve(".env").name == ".env"
    tool = tools_by_name(area, allow_hidden=True, frame_output=False)["read_file"]
    assert call(tool, path=".env") == "SECRET=1"


async def test_an_agent_writes_a_file_only_after_approval(area: Path) -> None:
    model = FakeChatModel(
        [tool_call("write_file", {"path": "memo.txt", "content": "approved text"}), "Saved."]
    )
    agent = Agent(
        "clerk",
        instructions="File memos.",
        model=model,
        tools=file_toolkit(area),
        checkpointer=InMemorySaver(),
    )

    paused = await agent.run("Save the memo.")

    assert paused.is_interrupted
    assert paused.interrupts[0]["value"]["tool"] == "write_file"
    assert not (area / "memo.txt").exists()
    assert paused.thread_id is not None
    resumed = await agent.resume(paused.thread_id, {"action": "approve"})
    assert resumed.output == "Saved."
    assert (area / "memo.txt").read_text(encoding="utf-8") == "approved text"


async def test_a_rejected_delete_leaves_the_file(area: Path) -> None:
    model = FakeChatModel([tool_call("delete_file", {"path": "notes.md"}), "Kept."])
    agent = Agent("clerk", model=model, tools=file_toolkit(area), checkpointer=InMemorySaver())
    paused = await agent.run("Delete the notes.")
    assert paused.thread_id is not None
    await agent.resume(paused.thread_id, {"action": "reject", "note": "keep it"})
    assert (area / "notes.md").exists()


# ---------------------------------------------------------------------------- a local SMTP server


class SMTPHandler(socketserver.StreamRequestHandler):
    """Just enough SMTP to accept messages: EHLO, MAIL, RCPT, DATA, QUIT."""

    inbox: list[dict[str, Any]]

    def reply(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())

    def handle(self) -> None:
        envelope: dict[str, Any] = {"to": []}
        self.reply("220 test ESMTP")
        while True:
            line = self.rfile.readline().decode().rstrip("\r\n")
            if not line:
                return
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self.reply("250-test")
                self.reply("250 SIZE 100000")
            elif verb == "MAIL":
                envelope["from"] = line.split(":", 1)[1].strip()
                self.reply("250 OK")
            elif verb == "RCPT":
                envelope["to"].append(line.split(":", 1)[1].strip())
                self.reply("250 OK")
            elif verb == "DATA":
                self.reply("354 go ahead")
                data = bytearray()
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    data += chunk
                envelope["message"] = message_from_bytes(bytes(data))
                self.inbox.append(envelope)
                envelope = {"to": []}
                self.reply("250 queued")
            elif verb == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("502 not implemented")


@pytest.fixture(autouse=True)
def _no_smtp_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("SMTP_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    inbox: list[dict[str, Any]] = []
    handler = type("BoundSMTP", (SMTPHandler,), {"inbox": inbox})
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(server.server_address[1]))
    try:
        yield inbox
    finally:
        server.shutdown()
        server.server_close()


def test_send_email_delivers_over_smtp(smtp: list[dict[str, Any]]) -> None:
    [send] = email_toolkit(
        sender="Support <support@example.com>", security="none", requires_approval=False
    )
    result = call(
        send,
        to=["ann@example.com"],
        cc=["bob@example.org"],
        subject="Your ticket",
        body="Hello Ann",
        html_body="<p>Hello Ann</p>",
        reply_to="help@example.com",
    )
    assert result.startswith("Sent the email 'Your ticket' to ann@example.com, bob@example.org")
    [envelope] = smtp
    assert envelope["to"] == ["<ann@example.com>", "<bob@example.org>"]
    message = envelope["message"]
    assert message["From"] == "Support <support@example.com>"
    assert message["Reply-To"] == "help@example.com"
    assert message.is_multipart()


def test_send_email_is_approval_gated_and_an_external_action() -> None:
    [send] = email_toolkit(sender="a@example.com")
    assert (send.metadata or {})[APPROVAL_METADATA_KEY] is True
    risk = declared_tool_risk(send)
    assert risk is not None and risk.external_action is True
    assert "Needs human approval" in send.description


def test_recipients_are_validated_and_allowlisted() -> None:
    sent: list[EmailMessage] = []
    [send] = email_toolkit(
        sender="a@example.com",
        allowed_recipients=["@example.com", "partner.example", "boss@other.example"],
        max_recipients=3,
        requires_approval=False,
        transport=sent.append,
    )
    ok = call(
        send,
        to=["x@example.com", "y@eu.partner.example", "boss@other.example"],
        subject="s",
        body="b",
    )
    assert ok.startswith("Sent")
    assert "not allowed: z@evil.example" in call(send, to=["z@evil.example"], subject="s", body="b")
    assert "not allowed" in call(send, to=["x@sub.example.com"], subject="s", body="b")
    assert "at most 3" in call(send, to=["x@example.com"] * 4, subject="s", body="b")
    assert "not a valid" in call(send, to=["not-an-address"], subject="s", body="b")
    injected = call(send, to=["x@example.com\r\nBcc: z@evil.example"], subject="s", body="b")
    assert injected.startswith("Error") and "line break" in injected
    assert "line break" in call(
        send, to=["x@example.com"], subject="Hi\r\nBcc: z@evil.example", body="b"
    )
    assert len(sent) == 1


def test_smtp_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    [send] = email_toolkit(requires_approval=False)
    monkeypatch.delenv("SMTP_SENDER", raising=False)
    assert "SMTP_SENDER is not set" in call(send, to=["a@example.com"], subject="s", body="b")
    monkeypatch.setenv("SMTP_SENDER", "bot@example.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert "SMTP_HOST is not set" in call(send, to=["a@example.com"], subject="s", body="b")


def test_credentials_are_never_sent_without_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_USERNAME", "bot")
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2")
    [send] = email_toolkit(
        sender="a@example.com", host="mail.example.com", security="none", requires_approval=False
    )
    result = call(send, to=["b@example.com"], subject="s", body="b")
    assert "without TLS" in result and "hunter2" not in result


def test_starttls_is_the_default(smtp: list[dict[str, Any]]) -> None:
    [send] = email_toolkit(sender="a@example.com", requires_approval=False, timeout=5)
    result = call(send, to=["b@example.com"], subject="s", body="b")
    assert result.startswith("Error: sending failed")  # the test server offers no STARTTLS
    assert smtp == []


def test_email_configuration_is_validated() -> None:
    with pytest.raises(ConfigurationError, match="security"):
        email_toolkit(security="plain")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="not a valid"):
        email_toolkit(sender="nobody")


# ---------------------------------------------------------------------------- webhooks


class HookHandler(BaseHTTPRequestHandler):
    received: list[dict[str, Any]]

    def log_message(self, *args: Any) -> None:
        return None

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.received.append(
            {"path": self.path, "json": json.loads(body), "headers": dict(self.headers)}
        )
        status = 500 if self.path.endswith("/fail") else 200
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture
def hook() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []
    handler = type("BoundHook", (HookHandler,), {"received": received})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


def test_post_webhook_sends_text_or_payloads(
    hook: tuple[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, received = hook
    monkeypatch.setenv("TEAM_HOOK", f"{base}/hooks/T0K3N")
    monkeypatch.setenv("HOOK_SECRET", "sig")
    [post] = webhook_toolkit(
        url_env="TEAM_HOOK",
        name="post_to_team",
        headers_env={"X-Signature": "HOOK_SECRET"},
        allow_private_networks=True,
        requires_approval=False,
    )
    assert post.name == "post_to_team"
    assert call(post, text="Deploy finished") == "Posted to the webhook (HTTP 200)."
    assert call(post, payload={"blocks": [1]}).startswith("Posted")
    assert [r["json"] for r in received] == [{"text": "Deploy finished"}, {"blocks": [1]}]
    assert received[0]["headers"]["X-Signature"] == "sig"
    assert "T0K3N" not in post.description
    assert "either text or payload" in call(post)


def test_webhook_errors_hide_the_url(hook: tuple[str, list[dict[str, Any]]]) -> None:
    base, _ = hook
    [post] = webhook_toolkit(
        url=f"{base}/fail", allow_private_networks=True, requires_approval=False
    )
    assert "HTTP 500" in call(post, text="x")
    [blocked] = webhook_toolkit(url=f"{base}/hooks/T0K3N", requires_approval=False)
    result = call(blocked, text="x")
    assert result.startswith("Error") and "T0K3N" not in result


def test_webhooks_need_approval_by_default() -> None:
    [post] = webhook_toolkit(url="https://hooks.example.com/x")
    assert (post.metadata or {})[APPROVAL_METADATA_KEY] is True
    with pytest.raises(ConfigurationError, match="either url or url_env"):
        webhook_toolkit()
    with pytest.raises(ConfigurationError, match="not an http"):
        webhook_toolkit(url="ftp://x")


# ---------------------------------------------------------------------------- templates


def test_render_template_fills_placeholders_safely() -> None:
    [render] = template_toolkit({"shipped": "Hello $name, order ${order} has shipped. $$5 off!"})
    assert "Configured templates: shipped" in render.description
    text = call(render, name="shipped", values={"name": "Ann", "order": 7})
    assert text == "Hello Ann, order 7 has shipped. $5 off!"
    assert "no value for placeholder(s): order" in call(
        render, name="shipped", values={"name": "A"}
    )
    assert call(render, template="Hi $who", values={}, strict=False) == "Hi $who"
    attack = call(render, template="${x.__class__}", values={"x": "1"})
    assert "invalid placeholder" in attack
    assert call(render, template="{0.__class__} $x", values={"x": "1"}) == "{0.__class__} 1"
    assert "unknown template" in call(render, name="nope")
    assert "either template or name" in call(render)


@pytest.mark.parametrize("root", ["", "  "])
def test_an_empty_root_is_refused_not_the_working_directory(root: str) -> None:
    from awa.errors import ConfigurationError
    from awa.toolkits import data_toolkit, document_toolkit, file_toolkit

    for build in (file_toolkit, data_toolkit, document_toolkit):
        with pytest.raises(ConfigurationError, match="root is empty"):
            build(root)
