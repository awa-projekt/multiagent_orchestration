"""Scripted models for the data-first build tests (repository only, never shipped).

`scripted_build_model` plays the test designer, the architect, the fit checker and the designed
agent from prepared answers, so a whole `awa build --data` runs offline on real example files.
Every answer is plain data the tests can change: the first draft of a designer may be wrong on
purpose, the next one (sent after the problems) right.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from awa.blueprints import AcceptanceDesign, BlueprintDesign
from awa.blueprints._fit import FitVerdict
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

REPO = Path(__file__).resolve().parents[2]
ORDERS_CSV = REPO / "examples/benchmarks/core/02_supplier_orders/data/orders.csv"
PAYMENTS_CSV = REPO / "examples/usecases/order-to-cash-exceptions/data/payments.csv"

PROCESS = (
    "Purchase orders over 20,000 EUR need the purchasing manager's approval before they are "
    "released."
)

ACCEPTANCE: dict[str, Any] = {
    "criteria": ["Open orders over 20,000 EUR are flagged for the purchasing manager."],
    "data_notes": [
        {
            "file": "orders.csv",
            "role": "input",
            "description": "The open and cancelled purchase orders.",
            "handling": "Flag every open order over 20,000 EUR.",
        }
    ],
    "rules": [
        {
            "name": "needs_manager_approval",
            "description": "Whether an order amount needs the purchasing manager.",
            "arguments": [{"name": "amount_eur", "type": "number", "description": "EUR"}],
            "expression": "amount_eur > 20000",
            "examples": [
                {"arguments": '{"amount_eur": 23400}', "result": "true"},
                {"arguments": '{"amount_eur": 4250}', "result": "false"},
            ],
        }
    ],
    "cases": [
        {
            "id": "big-order",
            "input": "Check order B-1004 in orders.csv.",
            "files": ["orders.csv"],
            "expectations": [
                {"kind": "contains", "value": "B-1004"},
                {"kind": "tool_called", "value": "files_query_csv"},
            ],
        },
        {
            "id": "open-total",
            "input": "What is the total of all open orders in orders.csv?",
            "files": ["orders.csv"],
            "expectations": [
                {
                    "kind": "contains",
                    "value": "46.161,25",
                    "check": "sum(Betrag_EUR where Status == 'offen')",
                },
                {"kind": "tool_called", "value": "files_query_csv"},
            ],
        },
    ],
}

BLUEPRINT: dict[str, Any] = {
    "name": "order_screening",
    "description": "Flags purchase orders that need the purchasing manager.",
    "agents": [
        {
            "name": "screener",
            "description": "Screens purchase orders.",
            "instructions": (
                "Read orders.csv with files_query_csv path='orders.csv' delimiter=';' "
                "decimal=','. Use the columns `Bestellnr`, `Betrag_EUR` and `Status`. Ask "
                "needs_manager_approval for an amount."
            ),
            "tools": ["connector:files.query_csv", "needs_manager_approval"],
        }
    ],
    "entrypoint": "agent",
}


INVENTED: dict[str, Any] = {
    "criteria": ["Orders are checked."],
    "cases": [
        {
            "id": "check",
            "input": "Check order 7.",
            "expectations": [
                {"kind": "regex", "value": "7"},
                {"kind": "judge", "value": "answers politely"},
            ],
        }
    ],
}
"""An acceptance design without files or data notes."""

PLAIN_BLUEPRINT: dict[str, Any] = {
    "name": "order_helper",
    "description": "Answers questions about orders.",
    "agents": [{"name": "helper", "description": "Helps.", "instructions": "Answer politely."}],
    "entrypoint": "agent",
}


def with_changes(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A deep copy of `base` with top-level keys replaced."""

    return {**copy.deepcopy(base), **changes}


def case(acceptance: dict[str, Any], case_id: str) -> dict[str, Any]:
    """The case `case_id` of an acceptance dict (to change it in place)."""

    return next(c for c in acceptance["cases"] if c["id"] == case_id)


def system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def screener(call: ModelCall) -> Any:
    """The designed agent: it queries the orders file, then answers from the result."""

    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
    if "files_query_csv" not in call.tool_names:
        return "I cannot read the orders."
    if not results:
        arguments: dict[str, Any] = {"path": "orders.csv", "delimiter": ";", "decimal": ","}
        if "total" in request:
            arguments |= {"aggregates": ["sum(Betrag_EUR)"], "where": "Status == 'offen'"}
        else:
            arguments |= {"where": "Bestellnr == 'B-1004'"}
        return tool_call("files_query_csv", arguments)
    if "total" in request:
        return "The open orders total 46.161,25 EUR."
    return "Order B-1004 (23.400,00 EUR) needs the purchasing manager."


class Script:
    """The answers of a scripted build and a record of what was asked.

    Attributes:
        acceptances: What the test designer answers, one per call (the last one repeats).
        blueprints: What the architect answers, one per call (the last one repeats).
        fit: What the fit checker answers (`None`: it must not be asked).
        agent: How the designed system answers.
        prompts: The last message of every test designer, architect and fit call, by role.
        instructions: The system prompt of the first call of each role.
    """

    def __init__(
        self,
        acceptances: Sequence[dict[str, Any]] = (ACCEPTANCE,),
        blueprints: Sequence[dict[str, Any]] = (BLUEPRINT,),
        fit: dict[str, Any] | None = None,
        agent: Callable[[ModelCall], Any] = screener,
    ) -> None:
        self.acceptances = list(acceptances)
        self.blueprints = list(blueprints)
        self.fit = fit
        self.agent = agent
        self.prompts: dict[str, list[str]] = {"designer": [], "architect": [], "fit": []}
        self.instructions: dict[str, str] = {}

    def model(self) -> FunctionChatModel:
        return FunctionChatModel(self._respond)

    def _respond(self, call: ModelCall) -> Any:
        system = system_prompt(call)
        last = str(call.last_message.content)
        if system.startswith("You write the acceptance tests"):
            return self._answer("designer", self.acceptances, AcceptanceDesign, system, last)
        if system.startswith("You design multi-agent systems"):
            return self._answer("architect", self.blueprints, BlueprintDesign, system, last)
        if system.startswith("You check whether a process description fits"):
            assert self.fit is not None, "the fit checker must not be asked here"
            self.prompts["fit"].append(last)
            return structured_output(FitVerdict, self.fit)
        return self.agent(call)

    def _answer(
        self, role: str, answers: list[dict[str, Any]], schema: type, system: str, last: str
    ) -> Any:
        self.instructions.setdefault(role, system)
        self.prompts[role].append(last)
        return structured_output(
            schema, answers[min(len(self.prompts[role]) - 1, len(answers) - 1)]
        )

    def calls(self, role: str) -> int:
        return len(self.prompts[role])


class BytesStaged:
    """A `StagedFiles` that holds the files as bytes in memory and writes them on `materialize`
    (a stand-in for storage that is not the local file system, e.g. an upload store)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.calls: list[tuple[str, bool, bool]] = []

    def materialize(self, dest: Path, *, share: bool = True, replace: bool = False) -> None:
        self.calls.append((dest.name, share, replace))
        for name, content in self.files.items():
            target = dest / "files" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (dest / "output").mkdir(parents=True, exist_ok=True)


OUTBOX_SYSTEM: dict[str, Any] = {
    "name": "Outbound e-mail",
    "key": "outbound_email",
    "description": "Replies to customers.",
    "kind": "email",
    "writes": True,
    "tables": [
        {
            "name": "outgoing_messages",
            "description": "One reply.",
            "columns": [{"name": "recipient"}, {"name": "body"}],
            "rows": [],
        }
    ],
}


def mailer(call: Any) -> Any:
    """The designed agent: it sends one reply through the outbox when asked, else screens."""

    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    if "Send" not in request and "Process" not in request:
        return screener(call)
    if any(isinstance(m, ToolMessage) for m in call.messages):
        return "Reply sent."
    statement = (
        "INSERT INTO outgoing_messages VALUES ('kunde@example.org', 'Your order is released')"
    )
    return tool_call("outbound_email_execute", {"statement": statement})


def with_outbox() -> Script:
    acceptance = with_changes(ACCEPTANCE, systems=[OUTBOX_SYSTEM], rules=[])
    acceptance["cases"].append(
        {
            "id": "send-reply",
            "input": "Send the customer of order B-1004 a short note.",
            "files": ["orders.csv"],
            "expectations": [
                {"kind": "approval", "value": "outbound_email_execute"},
                {"kind": "tool_called", "value": "outbound_email_execute"},
            ],
            "decisions": [{"action": "approve", "tool": "outbound_email_execute"}],
        }
    )
    blueprint = with_changes(BLUEPRINT)
    blueprint["agents"][0]["tools"] = [
        "connector:files.query_csv",
        "connector:outbound_email.execute",
    ]
    return Script(acceptances=[acceptance], blueprints=[blueprint], agent=mailer)
