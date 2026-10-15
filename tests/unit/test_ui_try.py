"""The Try it page: a plain timeline, approval cards for agents and process steps, readable answers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
import httpx
from fastapi.testclient import TestClient

from awa.contracts.events import StepEvent
from awa.errors import ModelProviderError
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.ui import Project, create_ui_app
from awa.ui._try import Conversation, Narrator

TOKEN = "try-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}
SECRET = "sk-secret-value-4711"

BLUEPRINT = """\
name: invoice_desk
description: Releases invoices for payment.
agents:
  - name: clerk
    description: Looks invoices up and releases them.
    instructions: Look the invoice up, then release it.
    tools: [lookup_invoice, release_payment]
approvals:
  - tool: release_payment
    when: "amount_eur >= 5000"
"""

TOOLS = '''\
import asyncio
from pathlib import Path

from langchain_core.tools import tool

FINISHED = Path(__file__).with_name("finished.txt")


@tool
def lookup_invoice(invoice_id: str) -> str:
    """Look up an invoice in the ledger.

    Args:
        invoice_id: The invoice number, for example INV-2042.
    """
    return '{"status": "open"}'


@tool
async def release_payment(invoice_id: str, amount_eur: float, due: str, note: str = "") -> str:
    """Release an invoice for payment.

    Args:
        invoice_id: The invoice number, for example INV-2042.
        amount_eur: The amount in EUR.
        due: Date by which the supplier must be paid.
        note: Free text for the payment run.
    """
    await asyncio.sleep(0.4)
    FINISHED.write_text(invoice_id)
    return "released " + invoice_id
'''

CASES = """\
name: invoice_desk
cases:
  - id: small
    input: Please release invoice INV-2041.
    expected: released
    scorers: [contains]
  - id: large
    input: Please release invoice INV-2042 for payment.
    expected: released
    scorers: [contains]
  - id: again
    input: Please release invoice INV-2041.
    expected: released
    scorers: [contains]
"""

LOOKUP = tool_call("lookup_invoice", {"invoice_id": "INV-2042"})
SMALL = tool_call(
    "release_payment", {"invoice_id": "INV-2041", "amount_eur": 90, "due": "2026-10-05"}
)
RELEASE = tool_call(
    "release_payment",
    {"invoice_id": "INV-2042", "amount_eur": 7450, "due": "2026-10-05", "note": SECRET},
)


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = [dict(line.split(": ", 1) for line in f.splitlines()) for f in body.split("\n\n") if f]
    return [(frame["event"], json.loads(frame["data"])) for frame in frames if "event" in frame]


def registry(model: Any) -> ModelRegistry:
    models = ModelRegistry()
    models.register("default", model)
    return models


def write_project(folder: Path) -> None:
    (folder / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
    (folder / "tools.py").write_text(TOOLS, encoding="utf-8")
    (folder / "evals").mkdir()
    (folder / "evals" / "cases.yaml").write_text(CASES, encoding="utf-8")
    (folder / ".env").write_text(f"MY_SECRET={SECRET}\n", encoding="utf-8")


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []
    write_project(tmp_path)

    def make(*replies: Any, **options: Any) -> TestClient:
        if replies:
            options.setdefault("model_registry", registry(FakeChatModel(list(replies))))
        app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ={}, **options)
        client = TestClient(app, base_url=BASE).__enter__()
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def ask(client: TestClient, message: str = "Release INV-2042") -> list[tuple[str, dict[str, Any]]]:
    return events(client.post("/api/run", json={"message": message}, headers=SAFE).text)


# ---------------------------------------------------------------------- the timeline


def test_a_tool_call_is_one_friendly_sentence_with_the_raw_json_as_details(
    make_client: Callable[..., TestClient],
) -> None:
    got = ask(make_client(LOOKUP, "The invoice is open."))

    kinds = [kind for kind, _ in got]
    assert kinds == ["activity", "activity_done", "done"]
    step = got[0][1]
    assert step["text"] == "Look up an invoice in the ledger (Invoice id: INV-2042)"
    assert step["agent"] == "Clerk"
    assert json.loads(step["details"]) == {"invoice_id": "INV-2042"}
    assert "uses" not in step["text"]
    finished = got[1][1]
    assert finished["id"] == step["id"] and finished["ok"] is True
    assert "open" in finished["result"]


def test_a_failing_step_is_reported_as_not_ok(make_client: Callable[..., TestClient]) -> None:
    got = ask(make_client(tool_call("lookup_invoice", {"wrong": 1}), "Could not look it up."))

    assert [kind for kind, _ in got if kind == "activity_done"] == ["activity_done"]
    assert next(data for kind, data in got if kind == "activity_done")["ok"] is False


def test_saved_credentials_never_appear_in_the_timeline_or_the_card(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(RELEASE)

    body = client.post("/api/run", json={"message": "Release"}, headers=SAFE).text

    assert SECRET not in body
    assert "***" in body


# ---------------------------------------------------------------------- the approval card


def test_the_approval_card_says_what_happens_why_and_shows_titled_values(
    make_client: Callable[..., TestClient],
) -> None:
    got = ask(make_client(LOOKUP, RELEASE))

    kind, paused = got[-1]
    assert kind == "approval"
    card = paused["pending"][0]
    assert card["summary"] == "Release an invoice for payment"
    assert card["why"] == "because the amount is 5,000 EUR or more"
    fields = {field["name"]: field for field in card["fields"]}
    assert fields["amount_eur"]["title"] == "Amount (EUR)"
    assert fields["amount_eur"]["value"] == "7,450.00 EUR"
    assert fields["amount_eur"]["description"] == "The amount in EUR."
    assert fields["due"]["value"] == "5 Oct 2026"
    assert fields["invoice_id"]["value"] == "INV-2042"
    assert "7450" in card["args"]  # the raw JSON stays available for the Details toggle
    assert card["tool"] == "release_payment"


def test_approving_reports_the_result_and_the_answer(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(LOOKUP, RELEASE, "Released INV-2042.")
    paused = ask(client)[-1][1]

    resumed = events(
        client.post(
            "/api/run/resume",
            json={
                "thread_id": paused["thread_id"],
                "decisions": [{"id": paused["pending"][0]["id"], "approve": True}],
            },
            headers=SAFE,
        ).text
    )

    finished = [data for kind, data in resumed if kind == "activity_done"]
    assert finished and finished[0]["tool"] == "release_payment" and finished[0]["ok"]
    assert resumed[-1] == (
        "done",
        {"thread_id": paused["thread_id"], "agent": "clerk", "text": "Released INV-2042."},
    )


def test_saying_no_hands_the_note_to_the_agent(make_client: Callable[..., TestClient]) -> None:
    client = make_client(LOOKUP, RELEASE, "Understood, not released.")
    paused = ask(client)[-1][1]

    resumed = events(
        client.post(
            "/api/run/resume",
            json={
                "thread_id": paused["thread_id"],
                "decisions": [
                    {"id": paused["pending"][0]["id"], "approve": False, "note": "Wrong amount"}
                ],
            },
            headers=SAFE,
        ).text
    )

    assert resumed[-1][0] == "done"
    refused = [data for kind, data in resumed if kind == "activity_done"]
    assert [(data["ok"], data["rejected"], data["result"]) for data in refused] == [
        (False, True, "Wrong amount.")
    ]


async def test_a_decision_sent_twice_at_once_runs_only_once(tmp_path: Path) -> None:
    write_project(tmp_path)
    model = FakeChatModel([LOOKUP, RELEASE, "Released."])
    app = create_ui_app(
        tmp_path, token=TOKEN, port=8765, environ={}, model_registry=registry(model)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        first = await client.post("/api/run", json={"message": "Release"}, headers=SAFE)
        paused = events(first.text)[-1][1]
        decision = {
            "thread_id": paused["thread_id"],
            "decisions": [{"id": paused["pending"][0]["id"], "approve": True}],
        }

        answers = await asyncio.gather(
            client.post("/api/run/resume", json=decision, headers=SAFE),
            client.post("/api/run/resume", json=decision, headers=SAFE),
        )

    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [200, 409]
    refused = next(a for a in answers if a.status_code == 409)
    assert "already sent" in refused.json()["error"]["message"]


# ---------------------------------------------------------------------- the answer


def test_a_structured_answer_comes_with_a_readable_view(
    make_client: Callable[..., TestClient],
) -> None:
    answer = json.dumps(
        {
            "invoice_id": "INV-2042",
            "amount_eur": 7450,
            "lines": [{"item": "Pump", "qty": 2}, {"item": "Seal", "qty": 4}],
        }
    )

    kind, data = ask(make_client(answer))[-1]

    assert kind == "done"
    assert data["text"] == answer  # the raw text stays for the Details toggle
    items = {item["title"]: item["value"] for item in data["view"]["items"]}
    assert items["Amount (EUR)"] == "7,450.00 EUR"
    assert items["Lines"]["type"] == "table"


def test_the_readable_view_never_shows_a_saved_credential(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    secret = "test-key-not-real"
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    answer = json.dumps({"invoice_id": "INV-2042", "note": f"key {secret}", secret: 1})

    kind, data = ask(make_client(answer))[-1]

    assert kind == "done" and data["view"]
    assert secret not in json.dumps(data)


def test_a_plain_answer_has_no_view(make_client: Callable[..., TestClient]) -> None:
    kind, data = ask(make_client("Released INV-2042."))[-1]

    assert kind == "done" and "view" not in data


# ---------------------------------------------------------------------- examples and empty states


def test_try_it_offers_the_examples_of_the_test_cases(
    make_client: Callable[..., TestClient],
) -> None:
    data = make_client("x").get("/api/try").json()

    assert data["exists"] is True and data["model_ready"] is True
    # without the repeated one
    assert data["examples"] == [
        "Please release invoice INV-2041.",
        "Please release invoice INV-2042 for payment.",
    ]


def test_try_it_without_a_system_or_a_model_says_so(tmp_path: Path) -> None:
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ={})
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        empty = client.get("/api/try").json()
        write_project(tmp_path)
        no_model = client.get("/api/try").json()

    assert empty == {"exists": False, "model_ready": False, "examples": []}
    assert no_model["exists"] is True and no_model["model_ready"] is False


# ---------------------------------------------------------------------- friendly errors


@pytest.mark.parametrize(
    ("error", "message", "link"),
    [
        (
            ModelProviderError(
                "no API key for anthropic (set ANTHROPIC_API_KEY)", kind="authentication"
            ),
            "No key for the AI model is set up yet.",
            "Set up your model first",
        ),
        (
            ModelProviderError("401 invalid key", kind="authentication"),
            "Your AI provider did not accept the key.",
            "Check your model settings",
        ),
        (
            ModelProviderError("429", kind="rate_limit", retryable=True),
            "The AI service is busy right now (too many requests).",
            "",
        ),
        (
            ModelProviderError("down", kind="connection", retryable=True),
            "awa could not reach the AI service.",
            "Check your model settings",
        ),
        (
            ModelProviderError("404", kind="not_found"),
            "The AI service does not know this model.",
            "Check your model settings",
        ),
        (
            ModelProviderError("too long", kind="context_overflow"),
            "That was too much text for the model at once.",
            "",
        ),
    ],
)
def test_model_problems_are_explained_with_the_fix_and_a_link(
    tmp_path: Path, error: Exception, message: str, link: str
) -> None:
    explained = Project(tmp_path, environ={}).explain(error)

    assert explained.message == message
    assert explained.fix and explained.link_text == link
    assert explained.link == ("#setup" if link else "")
    assert str(error) in explained.detail


def test_a_run_without_a_model_key_leads_to_the_set_up_page(
    make_client: Callable[..., TestClient],
) -> None:
    class NoKey(FakeChatModel):
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            raise ModelProviderError(
                "no API key for anthropic (set ANTHROPIC_API_KEY)", kind="authentication"
            )

    kind, data = ask(make_client(model_registry=registry(NoKey(["unused"]))))[-1]

    assert kind == "error"
    assert data["link"] == "#setup" and data["link_text"] == "Set up your model first"


# ---------------------------------------------------------------------- stopping


async def test_closing_the_stream_stops_the_run(tmp_path: Path) -> None:
    write_project(tmp_path)
    project = Project(tmp_path, model_registry=registry(FakeChatModel([SMALL])), environ={})
    started = asyncio.Event()

    async def consume() -> None:
        async for frame in Conversation(project).chat("Release", "thread-1"):
            if b"activity" in frame:
                started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()  # what a closed browser connection does
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.7)

    assert not (tmp_path / "finished.txt").exists()  # the payment run never finished


BLUEPRINT_PROCESS_CARD = """\
name: invoice_desk
description: Reads an invoice and books it after a person agrees.
limits: {max_requests: 10}
agents:
  - name: reader
    description: Reads the invoice.
    instructions: Read the invoice.
  - name: booker
    description: Books the invoice.
    instructions: Book it.
process:
  - name: read
    title: Read the invoice
    agent: reader
    checks:
      - kind: approval
        when: "contains(text, 'LARGE')"
        reason: Large invoices are decided by a person.
        by: accounts_manager
  - name: book
    agent: booker
"""


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    (tmp_path / "blueprint.yaml").write_text(BLUEPRINT_PROCESS_CARD, encoding="utf-8")
    models = ModelRegistry()

    def reader_or_booker(call: ModelCall) -> str:
        booker = "Book it." in str(call.messages[0].content)
        return "Booked." if booker else "LARGE invoice, total 6200"

    models.register("default", FunctionChatModel(reader_or_booker))
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ={}, model_registry=models)
    with TestClient(app, base_url=BASE) as test_client:
        test_client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield test_client


def test_the_card_names_the_step_and_the_agent_and_says_why(client: TestClient) -> None:
    got = events(client.post("/api/run", json={"message": "Check INV-1"}, headers=SAFE).text)

    kind, paused = got[-1]
    assert kind == "approval"
    card = paused["pending"][0]
    assert card["label"] == "Step 'Read the invoice' (Reader)"
    assert (
        "made by Reader" in card["summary"]
        and "continues with the next step only if" in card["summary"]
    )
    assert card["why"] == "Large invoices are decided by a person. Who decides: accounts_manager."
    assert card["step"] == "read" and card["agent"] == "reader" and card["kind"] == "step_approval"
    assert card["tool"] == "step:read"
    assert {f["name"] for f in card["fields"]} == {"text"}
    assert "LARGE invoice, total 6200" in card["fields"][0]["value"]


def test_the_timeline_has_one_sentence_per_step_status_and_approving_continues(
    client: TestClient,
) -> None:
    first = events(client.post("/api/run", json={"message": "Check INV-1"}, headers=SAFE).text)
    notes = [d["text"] for k, d in first if k == "activity"]
    assert "Step 1 of 2, Read the invoice (Reader), is being checked" in notes
    assert "Step 1 of 2, Read the invoice (Reader), waits for a person to decide" in notes
    paused = first[-1][1]

    resumed = events(
        client.post(
            "/api/run/resume",
            json={
                "thread_id": paused["thread_id"],
                "decisions": [{"id": paused["pending"][0]["id"], "approve": True}],
            },
            headers=SAFE,
        ).text
    )

    assert resumed[-1][0] == "done" and resumed[-1][1]["text"] == "Booked."


def test_a_rejected_step_is_reported_with_the_note_of_the_person(client: TestClient) -> None:
    paused = events(client.post("/api/run", json={"message": "Check INV-1"}, headers=SAFE).text)[
        -1
    ][1]

    resumed = events(
        client.post(
            "/api/run/resume",
            json={
                "thread_id": paused["thread_id"],
                "decisions": [
                    {"id": paused["pending"][0]["id"], "approve": False, "note": "Wrong supplier"}
                ],
            },
            headers=SAFE,
        ).text
    )

    assert resumed[-1][0] == "error"
    assert "Rejected by accounts_manager: Wrong supplier" in json.dumps(resumed[-1][1])


def test_the_step_card_hides_credentials_in_the_result(tmp_path: Path) -> None:
    (tmp_path / "blueprint.yaml").write_text(BLUEPRINT_PROCESS_CARD, encoding="utf-8")
    (tmp_path / ".env").write_text("MY_SECRET=sk-secret-value-4711\n", encoding="utf-8")
    narrator = Narrator(Project(tmp_path, environ={}))

    card = narrator.card(
        {
            "kind": "step_approval",
            "step": "read",
            "title": "Read",
            "agent": "reader",
            "reason": "r",
            "output": {"note": "key sk-secret-value-4711"},
        }
    )

    assert "sk-secret-value-4711" not in json.dumps(card)


def test_started_events_say_nothing_and_a_failed_step_says_why(tmp_path: Path) -> None:
    (tmp_path / "blueprint.yaml").write_text(BLUEPRINT_PROCESS_CARD, encoding="utf-8")
    narrator = Narrator(Project(tmp_path, environ={}))
    ids: dict[str, Any] = {"run_id": "r", "root_run_id": "r", "agent": "reader"}

    assert narrator.frame(StepEvent(step="read", index=0, total=2, status="started", **ids)) is None
    frame = narrator.frame(
        StepEvent(
            step="read", index=0, total=2, status="failed", message="The total is zero.", **ids
        )
    )

    assert frame is not None and b"failed a check: The total is zero." in frame
