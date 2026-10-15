"""A small headless-browser driver for tests of the web app: Chrome or Edge over the DevTools
protocol (no extra dependency beyond `websockets`), with the console and network watched.

`launch()` returns `None` when no browser is installed, so tests skip instead of failing
(unless `AWA_TEST_REQUIRE_BROWSER` is set, as in CI).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def find_browser() -> str | None:
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return next((path for path in CANDIDATES if Path(path).is_file()), None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Tab:
    """One page. Console errors, uncaught exceptions and failed requests are collected."""

    def __init__(self, url: str) -> None:
        from websockets.sync.client import connect

        self._ws = connect(url, max_size=None, open_timeout=30)
        self._id = 0
        self.errors: list[str] = []
        self.requests: list[str] = []
        for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
            self.send(method)
        self.send("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def _event(self, message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params", {})
        if method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            self.errors.append(
                " ".join(str(a.get("value", a.get("description", ""))) for a in params["args"])
            )
        elif method == "Runtime.exceptionThrown":
            details = params["exceptionDetails"]
            self.errors.append(
                str(details.get("exception", {}).get("description") or details.get("text"))
            )
        elif method == "Log.entryAdded" and params["entry"]["level"] == "error":
            entry = params["entry"]
            self.errors.append(f"{entry['text']} {entry.get('url', '')}")
        elif method == "Network.requestWillBeSent":
            self.requests.append(params["request"]["url"])

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._ws.recv(timeout=60))
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return dict(message.get("result", {}))
            self._event(message)

    def drain(self) -> None:
        with contextlib.suppress(TimeoutError):
            while True:
                self._event(json.loads(self._ws.recv(timeout=0.05)))

    def viewport(self, width: int, height: int = 900) -> None:
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

    def media(self, **features: str) -> None:
        self.send(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": k.replace("_", "-"), "value": v} for k, v in features.items()]},
        )

    def js(self, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JavaScript failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait(self, condition: str, what: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            with contextlib.suppress(RuntimeError):  # the page may be between two documents
                if self.js(f"Boolean({condition})"):
                    return
            time.sleep(0.1)
        body = self.js("document.body.innerText")
        raise AssertionError(
            f"Timed out waiting for {what}. Errors: {self.errors}. Page: {body[:600]!r}"
        )

    def goto(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait(
            f"location.href.startsWith({json.dumps(url.split('?')[0])}) && document.readyState === 'complete'",
            url,
        )

    def click(self, selector: str) -> None:
        self.wait(f"document.querySelector({json.dumps(selector)})", f"{selector} to exist")
        self.js(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); el.scrollIntoView({{block: 'center'}}); el.click(); }})()"
        )

    def fill(self, selector: str, text: str) -> None:
        self.wait(f"document.querySelector({json.dumps(selector)})", f"{selector} to exist")
        self.js(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); el.focus(); el.value = {json.dumps(text)};"
            " el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )

    def key(self, key: str, code: str, vk: int, shift: bool = False) -> None:
        modifiers = 8 if shift else 0
        for kind in ("keyDown", "keyUp"):
            self.send(
                "Input.dispatchKeyEvent",
                {
                    "type": kind,
                    "key": key,
                    "code": code,
                    "windowsVirtualKeyCode": vk,
                    "modifiers": modifiers,
                },
            )

    def tab(self, shift: bool = False) -> None:
        self.key("Tab", "Tab", 9, shift)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


class Chrome:
    def __init__(self, executable: str) -> None:
        import httpx

        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="awa-test-browser-")
        self.process = subprocess.Popen(
            [
                executable, "--headless=new", f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}", "--remote-allow-origins=*", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", "--lang=en-US",
                "--force-color-profile=srgb", *_container_flags(), "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self._http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=5)
        deadline = time.monotonic() + 30
        while True:
            try:
                self._http.get("/json/version").raise_for_status()
                break
            except Exception:
                if time.monotonic() > deadline:
                    self.close()
                    raise
                time.sleep(0.3)

    def tab(self) -> Tab:
        target = self._http.put("/json/new?about:blank").json()
        return Tab(target["webSocketDebuggerUrl"])

    def close(self) -> None:
        self.process.terminate()
        with contextlib.suppress(Exception):
            self.process.wait(10)
        shutil.rmtree(self.profile, ignore_errors=True)


def _container_flags() -> list[str]:
    # Chromium refuses to start its sandbox as root, which is how CI containers run.
    running_as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    return ["--no-sandbox"] if running_as_root else []


def launch() -> Chrome | None:
    executable = find_browser()
    return Chrome(executable) if executable else None
