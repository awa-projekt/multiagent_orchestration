"""The web app in a real headless browser (Chrome or Edge), offline with scripted models: every page
in both themes at desktop and phone width, the click-through of each flow, keyboard focus, delete
with undo, empty and error states. A console error or an uncaught exception fails the test."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from _helpers.devtools import Chrome, Tab, free_port, launch

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("websockets")

import uvicorn

from awa import blueprints
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, FunctionChatModel, tool_call
from awa.ui import create_ui_app

pytestmark = pytest.mark.slow

PAGES = [
    "setup",
    "data",
    "describe",
    "review",
    "connect",
    "test",
    "run",
    "deploy",
    "inbox",
    "assistant",
]

BLUEPRINT = """\
name: bookings
description: Books appointments after a human approves.
agents:
  - name: clerk
    description: Books appointments.
    instructions: Book what is asked.
    tools: [book]
approvals:
  - tool: book
"""
TOOLS = '''\
from langchain_core.tools import tool


@tool
def book(slot: str) -> str:
    """Book an appointment slot."""
    return f"booked {slot}"
'''
CASES = """\
name: bookings
cases:
  - id: greets
    input: Hello
    expected: Hello back
    scorers: [contains]
  - id: fails
    input: Something else
    expected: never said
    scorers: [contains]
"""


@pytest.fixture(scope="module")
def chrome() -> Iterator[Chrome]:
    browser = launch()
    if browser is None:
        if os.environ.get("AWA_TEST_REQUIRE_BROWSER"):
            pytest.fail("AWA_TEST_REQUIRE_BROWSER is set but no Chrome, Chromium or Edge was found")
        pytest.skip("needs Chrome or Edge")
    yield browser
    browser.close()


class Site:
    def __init__(self, folder: Path, tab: Tab, port: int, token: str) -> None:
        self.folder, self.tab, self.port, self.token = folder, tab, port, token

    def open(self, page: str) -> None:
        self.tab.goto(f"http://127.0.0.1:{self.port}/?token={self.token}#{page}")
        self.tab.wait(f"!document.getElementById('{page}').hidden", f"the {page} page")


@contextlib.contextmanager
def site(chrome: Chrome, folder: Path, *, model: Any = None, system: bool = True) -> Iterator[Site]:
    if system:
        (folder / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
        (folder / "tools.py").write_text(TOOLS, encoding="utf-8")
        (folder / "evals").mkdir(exist_ok=True)
        (folder / "evals" / "cases.yaml").write_text(CASES, encoding="utf-8")
    registry = ModelRegistry()
    registry.register("default", model or FunctionChatModel(lambda call: "Hello back to you"))
    port = free_port()
    app = create_ui_app(folder, port=port, model_registry=registry, environ={})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    tab = chrome.tab()
    try:
        yield Site(folder, tab, port, app.state.token)
        tab.drain()
        assert tab.errors == []
        outside = [
            u for u in tab.requests if not u.startswith((f"http://127.0.0.1:{port}", "data:"))
        ]
        assert outside == []
    finally:
        tab.close()
        server.should_exit = True
        thread.join(10)


def focus_ring(tab: Tab) -> dict[str, Any]:
    return tab.js(
        "(() => { const el = document.activeElement; const s = getComputedStyle(el);"
        " return {tag: el.tagName, outline: s.outlineStyle, width: parseFloat(s.outlineWidth)}; })()"
    )


@pytest.mark.parametrize(
    ("theme", "width"), [("light", 1280), ("dark", 1280), ("light", 390), ("dark", 390)]
)
def test_every_page_renders_cleanly_in_both_themes_and_sizes(
    chrome: Chrome, tmp_path: Path, theme: str, width: int
) -> None:
    with site(chrome, tmp_path) as s:
        s.tab.viewport(width, 800)
        s.tab.media(prefers_color_scheme=theme)
        for page in PAGES:
            s.open(page)
            s.tab.wait("document.querySelector('nav a[aria-current=step]')", "the current step")
            assert s.tab.js("document.documentElement.dataset.theme") == theme
            assert s.tab.js(f"document.querySelector('#{page} h1').offsetHeight") > 0
            assert s.tab.js("document.documentElement.scrollWidth <= innerWidth"), page
            assert s.tab.js(
                "[...document.querySelectorAll('button:not([hidden]), .button, nav a')]"
                ".filter((b) => b.offsetParent && b.classList.contains('small') === false && b.offsetHeight < 44 && !b.closest('details > .collapse'))"
                ".map((b) => b.textContent.trim()).length === 0"
            ), page


def test_keyboard_alone_reaches_the_steps_and_every_stop_shows_a_ring(
    chrome: Chrome, tmp_path: Path
) -> None:
    with site(chrome, tmp_path) as s:
        s.open("setup")
        s.tab.js("document.querySelector('#setup-title').focus()")
        seen = []
        for _ in range(8):  # the page's own controls; the last one is the save button
            s.tab.tab()
            ring = focus_ring(s.tab)
            assert ring["outline"] != "none" and ring["width"] >= 2, ring
            seen.append(
                s.tab.js("document.activeElement.textContent.trim() || document.activeElement.id")
            )
            if seen[-1] == "Save and continue":
                break
        assert "Save and continue" in seen
        s.tab.js("document.querySelector('nav a[href=\"#data\"]').focus()")
        s.tab.key("Enter", "Enter", 13)
        s.tab.js("document.querySelector('nav a[href=\"#data\"]').click()")
        s.tab.wait("!document.getElementById('data').hidden", "the data page by keyboard")


def test_the_theme_toggle_switches_and_is_remembered(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("setup")
        s.tab.click("#theme")
        dark = s.tab.js("document.documentElement.dataset.theme")
        assert (
            s.tab.js("document.querySelector('#theme').getAttribute('aria-pressed')")
            == str(dark == "dark").lower()
        )
        s.tab.click("#theme")
        assert s.tab.js("document.documentElement.dataset.theme") != dark
        assert s.tab.js("document.querySelector('#theme .ic') !== null")


def test_motion_is_reduced_when_the_system_asks_for_it(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.tab.media(prefers_reduced_motion="reduce")
        s.open("describe")
        s.tab.js("document.querySelector('#describe').append(tick())")
        duration = s.tab.js(
            "getComputedStyle(document.querySelector('#describe > .tick .ring')).animationDuration"
        )
        assert duration == "0.001s"
        assert (
            s.tab.js("getComputedStyle(document.querySelector('#describe')).animationDuration")
            == "0.001s"
        )


def test_setup_saves_a_key_and_shows_it_as_saved(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("setup")
        s.tab.wait("document.querySelectorAll('#providers details').length > 0", "the providers")
        s.tab.js(
            "document.querySelector('input[data-env=ANTHROPIC_API_KEY]').closest('details').open = true"
        )
        s.tab.fill("input[data-env=ANTHROPIC_API_KEY]", "not-a-real-key")
        s.tab.click("#save-setup")
        s.tab.wait("!document.getElementById('data').hidden", "the data page after saving")
        assert "Saved: ANTHROPIC_API_KEY" in s.tab.js("document.querySelector('#toasts').innerText")
        s.open("setup")
        s.tab.wait(
            "document.querySelector('#providers .badge.saved, #setup-status .alert.ok')",
            "the saved mark",
        )
        assert s.tab.js(
            "document.querySelector('#setup-status .alert.ok .tick, #providers .badge.ready .tick') !== null"
        )
        assert "not-a-real-key" not in s.tab.js("document.body.innerText")


def test_data_upload_remove_and_undo(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("data")
        s.tab.wait(
            "document.querySelector('#data-cards .empty-state')", "the empty state of the files"
        )
        for name in ("keep.csv", "drop.csv"):
            s.tab.js(
                f"fetch('/api/data/files?name={name}', {{method: 'PUT', headers: AUTH, body: 'a,b\\n1,2\\n'}}).then((r) => r.ok)"
            )
        s.tab.js("api('/api/data/analyse', {}).catch(() => 0)")
        s.open("data")
        s.tab.wait(
            "document.querySelectorAll('#data-cards .card.file').length === 2", "two file cards"
        )
        # Undo: the card comes back and nothing is deleted.
        s.tab.js(
            "[...document.querySelectorAll('#data-cards .card.file')].find((c) => c.textContent.includes('keep.csv')).querySelector('button.ghost').click()"
        )
        s.tab.wait("document.querySelector('#toasts .toast button')", "the undo toast")
        assert "Removed keep.csv" in s.tab.js("document.querySelector('#toasts').innerText")
        s.tab.click("#toasts .toast button")
        s.tab.wait(
            "document.querySelectorAll('#data-cards .card.file').length === 2 && !document.querySelector('#data-cards .collapse')",
            "the card back",
        )
        # Remove: leaving the page makes it final.
        s.tab.js(
            "[...document.querySelectorAll('#data-cards .card.file')].find((c) => c.textContent.includes('drop.csv')).querySelector('button.ghost').click()"
        )
        s.tab.wait("document.querySelector('#data-cards .collapse.leave')", "the collapsing card")
        s.tab.js("location.hash = '#describe'")
        s.tab.wait("!document.getElementById('describe').hidden", "the describe page")
        s.open("data")
        s.tab.wait(
            "document.querySelectorAll('#data-cards .card.file').length === 1", "one card left"
        )
        assert "keep.csv" in s.tab.js("document.querySelector('#data-cards').innerText")
        assert "drop.csv" not in s.tab.js("document.querySelector('#data-cards').innerText")


def test_an_empty_upload_is_an_error_with_the_fix(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path, system=False) as s:
        s.open("data")
        s.tab.wait("document.querySelector('#data-cards .empty-state')", "the empty state")
        s.tab.js("uploadFiles([{file: new File([], 'empty.csv'), name: 'empty.csv'}])")
        s.tab.wait("document.querySelector('#upload-list .alert.error')", "the upload error")
        assert "empty" in s.tab.js("document.querySelector('#upload-list .alert.error').innerText")
        assert s.tab.js("document.querySelector('#upload-list .alert.error .ic') !== null")


def test_describe_builds_and_shows_the_success(
    chrome: Chrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Report:
        passed = True
        pass_rate = 1.0

        def summary(self) -> str:
            return "Ready: 2 of 2 eval cases pass."

        def to_dict(self) -> dict[str, Any]:
            return {
                "name": "bookings",
                "cases": [{"id": "a", "status": "passed"}],
                "data_map": None,
            }

    async def build(process: str, *, out_dir: Path, on_progress: Any, **options: Any) -> Report:
        on_progress(type("Progress", (), {"message": "Round 1: 1 of 1 pass", "round": 1})())
        (out_dir / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
        return Report()

    monkeypatch.setattr(blueprints, "build_project", build)
    with site(chrome, tmp_path, system=False) as s:
        s.open("describe")
        s.tab.click("#create")
        s.tab.wait(
            "document.querySelector('#describe-result .alert.error')", "the empty-description error"
        )
        s.tab.fill("#process", "Book appointments after a manager approves.")
        s.tab.click("#create")
        s.tab.wait(
            "document.querySelector('#describe-result .alert.ok .tick')", "the success with a check"
        )
        assert s.tab.js("document.querySelectorAll('#stages .step.ok .mark .ic').length") >= 1
        assert "1 of 1 eval cases pass" in s.tab.js(
            "document.querySelector('#describe-result').innerText"
        )
        s.tab.click("#describe-result a.button.primary")
        s.tab.wait("!document.getElementById('review').hidden", "the review page")
        s.tab.wait("document.querySelector('#review-body .diagram svg.flow')", "the diagram")
        assert s.tab.js(
            "document.querySelector('nav a[href=\"#review\"]').parentElement.classList.contains('is-done')"
        )


def test_review_connect_and_empty_states_without_a_system(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path, system=False) as s:
        for page, body in [
            ("review", "#review-body"),
            ("connect", "#connect-body"),
            ("test", "#test-results"),
            ("run", "#run-notice"),
        ]:
            s.open(page)
            s.tab.wait(
                f"document.querySelector('{body} .empty-state a.button.primary')",
                f"the empty state of {page}",
            )
            assert s.tab.js(f"document.querySelector('{body} .empty-state .ic.big') !== null")
            if page == "test":
                assert s.tab.js("document.querySelector('#run-tests').hidden")
        s.open("inbox")
        s.tab.wait("document.querySelector('#inbox-body .empty-state')", "the empty approvals")
        s.open("deploy")
        s.tab.wait("document.querySelector('#deploy-body .empty-state')", "the empty runs")


def test_review_shows_agents_and_checks_and_loading_is_a_skeleton(
    chrome: Chrome, tmp_path: Path
) -> None:
    with site(chrome, tmp_path) as s:
        s.open("review")
        s.tab.wait("document.querySelector('#review-body .grid .card h3')", "the agents")
        assert "clerk" in s.tab.js("document.querySelector('#review-body').innerText")
        assert s.tab.js("document.querySelector('#review-body .alert.ok .tick') !== null")
        s.tab.js("$('#review-body').replaceChildren(skeleton(2))")
        assert s.tab.js("document.querySelectorAll('#review-body .skeleton').length") == 3


def test_test_page_runs_all_cases_and_marks_pass_and_fail(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("test")
        s.tab.wait(
            "document.querySelectorAll('#test-results .case-edit').length === 2", "the case cards"
        )
        s.tab.click("#run-tests")
        s.tab.wait("document.querySelector('#test-summary .meter')", "the result")
        assert "1 of 2 eval cases passed" in s.tab.js(
            "document.querySelector('#test-summary').innerText"
        )
        assert s.tab.js("document.querySelectorAll('#test-results .case-edit.pass').length") == 1
        assert s.tab.js("document.querySelectorAll('#test-results .case-edit.fail').length") == 1
        assert s.tab.js(
            "document.querySelector('#test-results .case-edit.pass .badge.passed .tick') !== null"
        )
        assert s.tab.js(
            "document.querySelector('#test-results .case-edit.fail .badge.failed .ic') !== null"
        )


def test_try_it_pauses_for_approval_and_continues(chrome: Chrome, tmp_path: Path) -> None:
    model = FakeChatModel([tool_call("book", {"slot": "Mon 9:00"}), "Booked Monday 9:00."])
    with site(chrome, tmp_path, model=model) as s:
        s.open("run")
        s.tab.wait("document.querySelector('#conversation .empty')", "the empty conversation")
        s.tab.wait("document.querySelector('#examples button.example')", "the examples")
        s.tab.fill("#message", "Book Monday.")
        s.tab.click("#send")
        s.tab.wait("document.querySelector('#approvals .approval')", "the approval card")
        assert s.tab.js("document.querySelector('#approvals .approval h3 .ic') !== null")
        assert "Mon 9:00" in s.tab.js("document.querySelector('#approvals').innerText")
        assert s.tab.js("document.querySelector('#conversation .step.waiting .mark .ic') !== null")
        assert s.tab.js(
            "document.querySelector('#composer').hidden && document.querySelector('#send').hidden"
        )
        s.tab.click("#approvals button.primary")
        assert s.tab.js(
            "!document.querySelector('#composer').hidden && !document.querySelector('#send').hidden"
        )
        s.tab.wait("document.querySelector('#conversation .decision.ok')", "the decision")
        s.tab.wait(
            "document.querySelector('#conversation').innerText.includes('Booked Monday 9:00.')",
            "the answer",
        )
        s.tab.wait("document.querySelector('#conversation .step.ok .mark .ic')", "the done step")
        s.tab.wait(
            "!document.querySelector('#working') || document.querySelector('#working').hidden",
            "the end of the run",
        )


def test_enter_during_a_running_turn_starts_no_second_run(chrome: Chrome, tmp_path: Path) -> None:
    calls: list[int] = []

    def slow(call: Any) -> str:
        calls.append(call.index)
        time.sleep(1.0)
        return "Done."

    with site(chrome, tmp_path, model=FunctionChatModel(slow)) as s:
        s.open("run")
        s.tab.wait("document.querySelector('#conversation .empty')", "the empty conversation")
        enter = (
            "document.querySelector('#message').dispatchEvent("
            "new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}))"
        )
        s.tab.fill("#message", "First.")
        s.tab.js(enter)
        s.tab.fill("#message", "Second.")
        s.tab.js(enter)
        s.tab.wait(
            "document.querySelector('#conversation').innerText.includes('Done.')", "the answer"
        )
        s.tab.wait(
            "!document.querySelector('#working') || document.querySelector('#working').hidden",
            "the end of the run",
        )

        assert s.tab.js("document.querySelectorAll('#conversation .you').length") == 1
        assert len(calls) == 1


MARKDOWN_ANSWER = (
    "Hello back: **INV-2042** is *released* with `po_number`.\n\n"
    "- PO-7725 matched\n- 7,450.00 EUR\n\n"
    "```\nINV-2042,released\n```\n"
    "[The guide](https://example.org/guide) <img src=x onerror=alert(1)> [no](javascript:alert(1))"
)


def test_answers_are_shown_as_markdown_and_never_as_markup(chrome: Chrome, tmp_path: Path) -> None:
    model = FunctionChatModel(lambda call: MARKDOWN_ANSWER)
    with site(chrome, tmp_path, model=model) as s:
        s.open("run")
        s.tab.wait("document.querySelector('#examples button.example')", "the examples")
        s.tab.fill("#message", "Release INV-2042.")
        s.tab.click("#send")
        s.tab.wait("document.querySelector('#conversation .doc strong')", "the rendered answer")
        answer = "document.querySelector('#conversation .doc')"
        assert s.tab.js(f"{answer}.querySelector('strong').textContent") == "INV-2042"
        assert s.tab.js(f"{answer}.querySelector('em').textContent") == "released"
        assert s.tab.js(f"{answer}.querySelector('p code').textContent") == "po_number"
        assert s.tab.js(f"{answer}.querySelectorAll('ul li').length") == 2
        assert s.tab.js(f"{answer}.querySelector('pre code').textContent") == "INV-2042,released"
        assert s.tab.js(f"{answer}.querySelectorAll('a').length") == 1
        assert s.tab.js(f"{answer}.querySelector('a').rel") == "noopener noreferrer"
        assert s.tab.js(f"{answer}.querySelector('img')") is None
        assert "<img src=x" in s.tab.js(f"{answer}.textContent")
        assert "**" not in s.tab.js(f"{answer}.innerText")

        s.open("test")
        s.tab.wait(
            "document.querySelectorAll('#test-results .case-edit').length === 2", "the case cards"
        )
        s.tab.click("#run-tests")
        s.tab.wait("document.querySelector('#test-results .answer .doc strong')", "the answers")
        assert s.tab.js("document.querySelector('#test-results .answer img')") is None


def test_use_it_shows_the_status_and_the_copy_button_works(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("deploy")
        s.tab.wait("document.querySelector('#health .badge')", "the status")
        assert s.tab.js("document.querySelector('#download .ic') !== null")
        s.tab.js("document.querySelector('#more-ways').open = true")
        s.tab.wait("document.querySelector('#more-ways .command button')", "a command card")
        assert "Copy" in s.tab.js("document.querySelector('#more-ways .command button').innerText")
        assert s.tab.js("document.querySelector('#more-ways .command button .ic') !== null")
        s.tab.js("document.querySelector('#more-ways .command button').click()")
        s.tab.wait(
            "document.querySelector('#more-ways .command button').innerText.match(/Copied|Selected/)",
            "the copy answer",
        )


def test_an_unreachable_server_is_explained_not_blank(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("setup")
        s.tab.js("window.fetch = () => Promise.reject(new TypeError('offline'))")
        s.tab.js("loadSetup()")
        s.tab.wait("document.querySelector('#setup-status .alert.error')", "the error")
        text = s.tab.js("document.querySelector('#setup-status .alert.error').innerText")
        assert "not reachable" in text and "reload" in text
        assert json.dumps(text)


@pytest.mark.parametrize(
    ("theme", "width"), [("light", 1280), ("dark", 1280), ("light", 390), ("dark", 390)]
)
def test_patterns_are_suggested_tested_and_used(
    chrome: Chrome, tmp_path: Path, theme: str, width: int
) -> None:
    from test_blueprints_auto import PROCESS, scripted

    with site(chrome, tmp_path, model=scripted(), system=False) as s:
        s.tab.viewport(width, 900)
        s.tab.media(prefers_color_scheme=theme)
        s.open("describe")
        s.tab.fill("#process", PROCESS)
        s.tab.click("#suggest-patterns")
        s.tab.wait(
            "document.querySelector('#patterns-result .alert.ok .tick')", "proposals with a check"
        )
        assert s.tab.js("document.querySelector('#patterns-result .pattern .diagram svg') !== null")
        assert s.tab.js("document.documentElement.scrollWidth <= innerWidth")
        s.tab.js(
            "document.querySelectorAll('[data-test-pattern]').forEach((c, i) => { c.checked = i < 2; })"
        )
        s.tab.click("#test-patterns")
        s.tab.wait("document.querySelector('#patterns-test-result table')", "the comparison")
        assert (
            s.tab.js("document.querySelectorAll('#patterns-test-result .badge.ready').length") >= 1
        )
        assert s.tab.js("document.documentElement.scrollWidth <= innerWidth")
        s.tab.click("[data-use]")
        s.tab.wait("document.querySelector('#patterns-use-result a.button')", "the saved system")
        assert s.tab.js("document.querySelector('#toasts .toast .ic') !== null")
        assert (tmp_path / "blueprint.yaml").is_file()


def test_the_suggestion_needs_a_description(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path, system=False) as s:
        s.open("describe")
        s.tab.click("#suggest-patterns")
        s.tab.wait("document.querySelector('#patterns-result .alert.error')", "the explanation")
        assert "What to do" in s.tab.js("document.querySelector('#patterns-result').textContent")


@pytest.mark.parametrize(
    ("theme", "width"), [("light", 1280), ("dark", 1280), ("light", 390), ("dark", 390)]
)
def test_assistant_page_copies_downloads_and_saves_the_brief(
    chrome: Chrome, tmp_path: Path, theme: str, width: int
) -> None:
    with site(chrome, tmp_path) as s:
        s.tab.viewport(width, 900)
        s.tab.media(prefers_color_scheme=theme)
        s.open("assistant")
        s.tab.wait("!document.getElementById('assistant-body').hidden", "the brief")
        assert s.tab.js("document.querySelector('#brief-text').textContent.length") > 100
        assert s.tab.js("document.documentElement.scrollWidth <= innerWidth")
        s.tab.js(
            "window.copied = null; navigator.clipboard.writeText = async (t) => { window.copied = t; }"
        )
        s.tab.click("#copy-brief")
        s.tab.wait("window.copied !== null", "the copy")
        assert s.tab.js("window.copied") == s.tab.js(
            "document.querySelector('#brief-text').textContent"
        )
        assert s.tab.js("document.querySelector('#assistant-note .alert.ok') !== null")
        s.tab.click("#download-brief")
        s.tab.wait("document.querySelectorAll('#toasts .toast').length >= 2", "the download toast")
        s.tab.click("#save-brief")
        s.tab.wait("document.querySelector('#assistant-note .alert.ok')", "the saved note")
        assert (tmp_path / "BRIEF.md").is_file()
        s.tab.click("#save-brief")
        s.tab.wait("document.querySelector('#replace-brief')", "the replace offer")


def test_assistant_page_without_a_system_is_an_empty_state(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path, system=False) as s:
        s.open("assistant")
        s.tab.wait("document.querySelector('#assistant-empty .empty-state')", "the empty state")
        assert s.tab.js("document.getElementById('assistant-body').hidden")


def test_long_folder_names_and_wide_lists_fit_a_phone(chrome: Chrome, tmp_path: Path) -> None:
    folder = tmp_path / ("a-rather-long-project-folder-name-" * 3)
    folder.mkdir()
    with site(chrome, folder) as s:
        (folder / "evals" / "cases.yaml").write_text(
            "name: bookings\ncases:\n  - id: do-not-pay-already-paid-invoice\n    input: Hello\n"
            "    scorers: [approval_requested:head_of_finance_approval, no_tool:output_write_csv]\n",
            encoding="utf-8",
        )
        s.tab.viewport(390, 800)
        for page in ("setup", "data", "deploy", "review"):
            s.open(page)
            s.tab.wait(f"document.querySelector('#{page}').innerText.length > 300", page)
            if page == "review":
                s.tab.wait("document.querySelector('#review table')", "the cases")
            assert s.tab.js("document.documentElement.scrollWidth <= innerWidth"), page
        s.open("data")
        s.tab.wait("document.querySelectorAll('#example-pick option').length", "the examples")
        assert s.tab.js(
            "document.querySelector('#example-pick').getBoundingClientRect().right <= innerWidth"
        )


def test_an_opened_example_becomes_the_project_everywhere(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("data")
        s.tab.wait("document.querySelectorAll('#example-pick option').length", "the examples")
        s.tab.js("document.querySelector('#example-box').open = true")
        s.tab.click("#open-example")
        s.tab.wait("document.querySelector('#example-status .alert.ok')", "the opened example")
        folder = s.tab.js("document.querySelector('#setup-folder').textContent")
        assert folder != str(tmp_path)
        s.tab.wait(
            f"document.querySelector('#folder-chip').textContent === {json.dumps(folder)}",
            "the folder in the header",
        )
        s.tab.wait("!document.querySelector('#data-status .working')", "the analysis")
        s.open("describe")
        assert s.tab.js("document.querySelector('#process').value.length") > 20


def test_review_checks_and_saves_an_edited_design(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("review")
        s.tab.wait("document.querySelector('#yaml')", "the design editor")
        s.tab.js("document.querySelector('#review-body details.card').open = true")
        s.tab.js(
            "{ const y = document.querySelector('#yaml'); y.value = y.value.replace('tools: [book]', 'tools: [missing_tool]') }"
        )
        s.tab.click("#review-body details.card button:last-child")
        s.tab.wait("document.querySelector('#checks .alert.warning')", "the refusal")
        assert "Must fix" in s.tab.js("document.querySelector('#checks').innerText")
        assert "missing_tool" not in (tmp_path / "blueprint.yaml").read_text(encoding="utf-8")
        s.tab.js(
            "{ const y = document.querySelector('#yaml'); y.value = y.value.replace('tools: [missing_tool]', 'tools: [book]').replace('Books appointments after', 'Books dentist appointments after') }"
        )
        s.tab.click("#review-body details.card button:last-child")
        s.tab.wait(
            "document.querySelector('#checks .alert.ok') && document.querySelector('#checks').innerText.includes('Saved.')",
            "the save",
        )
        assert "dentist" in (tmp_path / "blueprint.yaml").read_text(encoding="utf-8")


def test_connect_tests_saves_and_returns_to_the_stand_in(chrome: Chrome, tmp_path: Path) -> None:
    from test_ui_connect import database_url, make_project, sql_step

    make_project(tmp_path, [sql_step("ledger", "Invoice ledger")])
    with site(chrome, tmp_path, system=False) as s:
        s.open("connect")
        s.tab.wait("document.querySelector('#system-ledger')", "the system card")
        assert "0 of 1 live" in s.tab.js("document.querySelector('#connect-summary').innerText")
        s.tab.click("#system-ledger input[value=real]")
        s.tab.fill("#system-ledger input[data-env=LEDGER_DATABASE_URL]", database_url(tmp_path))
        s.tab.click("#system-ledger button.test-connection")
        s.tab.wait("document.querySelector('#system-ledger .result .alert')", "the test answer")
        assert s.tab.js("document.querySelector('#system-ledger .result .alert.ok') !== null")
        s.tab.click("#system-ledger button.save-connection")
        s.tab.wait(
            "document.querySelector('#connect-summary').innerText.includes('1 of 1 live')",
            "the live system",
        )
        assert database_url(tmp_path) not in s.tab.js("document.body.innerText")
        s.tab.click("#system-ledger .mode button")
        s.tab.wait(
            "document.querySelector('#connect-summary').innerText.includes('0 of 1 live')",
            "the stand-in again",
        )
        assert "stand-in again" in s.tab.js("document.querySelector('#system-ledger').innerText")


def test_a_changed_case_is_saved_and_run_alone(chrome: Chrome, tmp_path: Path) -> None:
    with site(chrome, tmp_path) as s:
        s.open("test")
        s.tab.wait(
            "document.querySelectorAll('#test-results .case-edit').length === 2", "the cases"
        )
        s.tab.fill("#case-0-input", "Hello there")
        s.tab.click("#test-results .case-edit .actions button:last-child")
        s.tab.wait(
            "document.querySelector('#case-0-title').closest('.case-edit').classList.contains('pass')",
            "the case result",
        )
        assert "Saved in evals/cases.yaml" in s.tab.js(
            "document.querySelector('#test-results .case-edit').innerText"
        )
        assert "Hello there" in (tmp_path / "evals" / "cases.yaml").read_text(encoding="utf-8")
        # One case is no test run: the run's summary and the Test step stay as they were.
        assert "eval cases passed" not in s.tab.js(
            "document.querySelector('#test-summary').innerText"
        )


def test_use_it_adds_a_trigger_and_a_sample_run_is_decided_on_approvals(
    chrome: Chrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_ui_operate import ORDERS
    from test_ui_operate import TOOLS as PAY_TOOLS

    monkeypatch.delitem(sys.modules, "tools", raising=False)
    (tmp_path / "blueprint.yaml").write_text(ORDERS, encoding="utf-8")
    (tmp_path / "tools.py").write_text(PAY_TOOLS, encoding="utf-8")
    model = FakeChatModel([tool_call("pay", {"order_id": "9", "amount_eur": 450}), "Paid order 9."])
    with site(chrome, tmp_path, model=model, system=False) as s:
        s.open("deploy")
        s.tab.wait("document.querySelector('#add-trigger')", "the trigger form")
        s.tab.js("document.querySelector('#add-trigger').open = true")
        s.tab.js(
            "const k = document.querySelector('#trigger-kind'); k.value = 'queue'; k.dispatchEvent(new Event('change'))"
        )
        s.tab.fill("#trigger-name", "orders-queue")
        s.tab.click("#add-trigger button.primary")
        s.tab.wait("document.querySelector('#trigger-orders-queue-title')", "the new trigger")
        assert "orders-queue" in (tmp_path / "blueprint.yaml").read_text(encoding="utf-8")
        card = "#trigger-shop-title"
        s.tab.js(
            f"document.querySelector('{card}').closest('section').querySelector('details').open = true"
        )
        s.tab.fill("#sample-shop-order", "9")
        s.tab.fill("#sample-shop-amount", "450")
        s.tab.click("section.trigger:has(#trigger-shop-title) button.run-sample")
        s.tab.wait(
            "document.querySelector('#use-inbox .approval')", "the waiting approval on Use it"
        )
        s.open("inbox")
        s.tab.wait("document.querySelector('#inbox-body .approval')", "the approval card")
        assert "450" in s.tab.js("document.querySelector('#inbox-body').innerText")
        s.tab.click("#inbox-body .approval button.primary")
        s.tab.wait(
            "document.querySelector('#inbox-body .result button.primary')", "Continue it now"
        )
        s.tab.click("#inbox-body .result button.primary")
        s.tab.wait("document.querySelector('#inbox-body .result .alert.ok')", "the finished run")
        s.tab.wait("document.querySelector('#inbox-body .empty-state')", "the empty approvals")
        s.open("deploy")
        s.tab.wait("document.querySelector('table.runs .badge')", "the runs list")
        assert "Paid order 9." in s.tab.js("document.querySelector('table.runs').innerText")
        assert s.tab.js("document.querySelector('table.runs td .doc p') !== null")


def test_a_database_is_added_under_your_data_and_read_live(chrome: Chrome, tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE invoices (invoice_id TEXT, total REAL)")
    with site(chrome, tmp_path, system=False) as s:
        s.open("data")
        s.tab.wait("document.querySelector('#system-cards .empty-state')", "no systems yet")
        s.tab.js("document.querySelector('#add-system').open = true")
        s.tab.wait("document.querySelectorAll('#system-type option').length > 1", "the kinds")
        s.tab.js(
            "{ const t = document.querySelector('#system-type'); t.value = 'sql'; t.dispatchEvent(new Event('change')); }"
        )
        s.tab.fill("#system-name", "Invoice ledger")
        s.tab.wait("document.querySelector('#system-fields input[data-env]')", "the settings")
        s.tab.fill("#system-fields input[data-env$='_DATABASE_URL']", str(ledger))
        s.tab.click("#save-system")
        s.tab.wait("document.querySelector('#system-cards .card.system')", "the system card")
        assert "invoices" in s.tab.js("document.querySelector('#system-cards').innerText")
        assert s.tab.js("document.querySelector('#system-result .alert.ok') !== null")
        assert (
            s.tab.js("document.querySelector('#data-next').textContent") == "Continue to Describe"
        )
