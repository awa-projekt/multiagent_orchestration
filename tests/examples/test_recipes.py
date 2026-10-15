"""Offline tests for the recipes in `examples/recipes/`.

Every recipe is checked the same way:

- its blueprint validates against its tools with no issue at all, and its README shows the
  blueprint's current Mermaid diagram;
- one realistic end-to-end case runs on scripted models (asserting tool calls,
  delegations or handoffs, the approval pause and resume, and the final output);
- `awa.evals.evaluate` runs the recipe's `cases.yaml` on the same scripted models and every
  case passes.

The scripted models are `FunctionChatModel`s: one `Script` per recipe plays every agent,
recognized by its instructions in the system prompt, and decides from the conversation
what the agent does next, much like a careful model would.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import json
import re
import shutil
import sys
import urllib.parse
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.conversation import (
    called,
    delegated_results,
    delegation_targets,
    results,
    system_text,
    tool_names,
)
from _helpers.framing import tool_content
from awa import Agent, AgentNetwork, Blueprint, Team
from awa.blueprints._schema_agent import CompactionBlueprint
from awa.blueprints.json_schema import model_from_json_schema
from awa.connectors import mock_response
from awa.contracts import DelegationResultEvent, HandoffEvent, ToolCallEvent, ToolResultEvent
from awa.contracts.protocols import AgentLike
from awa.evals import Dataset, EvalReport, JudgeVerdict, evaluate, isolated_run
from awa.memory import RECALLED_MEMORIES_ID_PREFIX
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

RECIPES_DIR = Path(__file__).resolve().parents[2] / "examples" / "recipes"
RECIPES = sorted(path.name for path in RECIPES_DIR.iterdir() if (path / "blueprint.yaml").is_file())
TEMPLATE_RECIPES = ("contract-review", "invoice-processing")
"""Recipes held to the template standard: 8 to 12 cases, two of them at a boundary."""


# ---------------------------------------------------------------------- loading recipes


@dataclass
class Recipe:
    slug: str
    blueprint: Blueprint
    tools_module: ModuleType
    directory: Path
    """A temporary copy of the recipe's folder: runs write their files (reports) there."""

    @property
    def tools_args(self) -> list[str]:
        """The `--tools` arguments of the CLI: none for a recipe that has no `tools.py`."""

        tools = self.directory / "tools.py"
        return ["--tools", str(tools)] if tools.is_file() else []

    @property
    def tools(self) -> list[Any]:
        return [getattr(self.tools_module, name) for name in self.tools_module.__all__]

    def output_tool(self, agent: str) -> str:
        schema = self.blueprint.agent(agent).output_schema
        assert schema is not None, f"{agent} has no output_schema"
        return model_from_json_schema(schema, name=f"{agent}_output").__name__

    def build(
        self, script: Script, blueprint: Blueprint | None = None, *, reset: bool = True
    ) -> AgentLike:
        blueprint = blueprint or self.blueprint
        if reset:
            self.tools_module.reset_sample_data()
        # Every entrypoint (workflows and networks included) pauses for approvals.
        return blueprint.build(
            tools=self.tools,
            model_registry=script.registry(),
            checkpointer=InMemorySaver() if blueprint.approval_agents() else None,
            allow_imports=True,  # approval predicates are `tools:<function>` references
        )

    def build_resumable(
        self, script: Script, blueprint: Blueprint | None = None, *, reset: bool = True
    ) -> Team | Agent[Any, Any]:
        """Build an entrypoint that pauses for approvals (a team or an agent)."""

        system = self.build(script, blueprint, reset=reset)
        assert isinstance(system, Team | Agent)
        return system


def _import(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@functools.cache
def _tools_module(slug: str) -> ModuleType:
    path = RECIPES_DIR / slug / "tools.py"
    if not path.is_file():  # a recipe of builtin tools only has nothing to reset
        module = ModuleType(f"awa_recipe_{slug.replace('-', '_')}_tools")
        module.__all__ = []  # type: ignore[attr-defined]
        module.reset_sample_data = lambda: None  # type: ignore[attr-defined]
        return module
    return _import(f"awa_recipe_{slug.replace('-', '_')}_tools", path)


@pytest.fixture
def load_recipe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str], Recipe]:
    """Load a copy of a recipe in a temporary folder; its tools module is also importable as
    `tools`, like under the CLI."""

    def load(slug: str) -> Recipe:
        module = _tools_module(slug)
        monkeypatch.setitem(sys.modules, "tools", module)
        module.reset_sample_data()
        directory = tmp_path / slug
        shutil.copytree(RECIPES_DIR / slug, directory, ignore=shutil.ignore_patterns("__pycache__"))
        blueprint = Blueprint.from_yaml(directory / "blueprint.yaml")
        return Recipe(slug, blueprint, module, directory)

    return load


# ---------------------------------------------------------------------- conversation helpers


def request(call: ModelCall) -> str:
    """The first user message: the task, brief or customer message the agent works on."""

    return next(
        str(m.content)
        for m in call.messages
        if isinstance(m, HumanMessage)
        and not (m.id or "").startswith(RECALLED_MEMORIES_ID_PREFIX)  # auto_recall context
    )


def first_json(text: str) -> dict[str, Any]:
    """The first JSON object embedded in `text`."""

    start = text.index("{")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    assert isinstance(value, dict)
    return value


def handoff_targets(events: list[Any]) -> list[str]:
    return [event.target for event in events if isinstance(event, HandoffEvent)]


# ---------------------------------------------------------------------- scripted models


class Script:
    """Plays every agent of a recipe; one method per agent name."""

    def __init__(self, recipe: Recipe) -> None:
        self.recipe = recipe
        self.markers = {
            spec.name: spec.instructions.strip().splitlines()[0] for spec in recipe.blueprint.agents
        }
        assert len(set(self.markers.values())) == len(self.markers), "instructions must differ"

    def registry(self) -> ModelRegistry:
        registry = ModelRegistry()
        registry.register("default", FunctionChatModel(self))
        return registry

    def agent_of(self, call: ModelCall) -> str:
        prompt = system_text(call)
        found = [name for name, marker in self.markers.items() if marker in prompt]
        assert found, f"cannot tell the agent from the prompt: {prompt[:200]!r}"
        return max(found, key=lambda name: len(self.markers[name]))

    def __call__(self, call: ModelCall) -> Any:
        if "JudgeVerdict" in call.tool_names:
            return structured_output(
                JudgeVerdict, {"score": 0.9, "reasoning": "scripted judge: criteria met"}
            )
        return getattr(self, self.agent_of(call))(call)

    def finish(self, agent: str, data: dict[str, Any]) -> Any:
        return structured_output(self.recipe.output_tool(agent), data)


# ---------------------------------------------------------------------- 1 invoice processing


INVOICE_PATTERN = re.compile(
    r"INVOICE (?P<number>\S+)\nFrom: (?P<vendor>.+), VAT ID (?P<vat>\S+)\n"
    r"Invoice date: (?P<date>\S+) \| Due date: (?P<due>\S+)\nYour order: (?P<po>.+)\n"
    r"(?P<item>.+) \| (?P<qty>[\d.]+) x (?P<price>[\d.]+) EUR \| (?P<amount>[\d.]+) EUR\n"
    r"Net (?P<net>[\d.]+) EUR \| VAT \d+% (?P<tax>[\d.]+) EUR \| Total (?P<gross>[\d.]+) EUR\n"
    r"Pay to IBAN (?P<iban>\S+)"
)


class InvoiceScript(Script):
    def ap_coordinator(self, call: ModelCall) -> Any:
        document = re.search(r"DOC-\d+", request(call))
        assert document is not None
        checks = delegated_results(call)
        if "extractor" not in checks:
            return tool_call(
                "delegate_task",
                {
                    "agent": "extractor",
                    "task": f"Extract the fields of document {document.group()}.",
                },
            )
        invoice = first_json(checks["extractor"])
        if invoice["missing_fields"]:
            return self.hold_for_missing_fields(call, invoice)
        if "validator" not in checks:
            brief = f"Check this invoice:\n{json.dumps(invoice)}"
            return [
                tool_call("delegate_task", {"agent": "validator", "task": brief}),
                tool_call("delegate_task", {"agent": "policy_checker", "task": brief}),
            ]
        validation = first_json(checks["validator"])
        policy = first_json(checks["policy_checker"])
        vendor_id = validation.get("vendor_id")
        booked, held = results(call, "book_invoice"), results(call, "hold_invoice")
        reasons = [
            f"{d['field']}: invoiced {d['invoice_value']}, expected {d['expected_value']}"
            for d in validation["differences"]
        ]
        reasons += [
            f"{f['rule']}: {f['detail']}" for f in policy["findings"] if f["severity"] == "block"
        ]
        if booked and "rejected" in booked[0]:
            reasons.append(booked[0])
        clean = validation["status"] == "match" and policy["compliant"]
        if clean and not booked:
            return tool_call(
                "book_invoice",
                {
                    "vendor_id": vendor_id,
                    "invoice_number": invoice["invoice_number"],
                    "po_number": invoice["po_number"],
                    "net_amount": invoice["net_amount"],
                    "tax_amount": invoice["tax_amount"],
                    "gross_amount": invoice["gross_amount"],
                    "currency": invoice["currency"],
                    "due_date": invoice["due_date"],
                },
            )
        is_booked = bool(booked) and booked[0].startswith("Booked")
        if not is_booked and not held:
            return tool_call(
                "hold_invoice",
                {
                    "document_id": invoice["document_id"],
                    "invoice_number": invoice["invoice_number"],
                    "vendor_id": vendor_id or "unknown",
                    "reasons": reasons,
                },
            )
        price_issue = any("unit_price" in reason for reason in reasons)
        notified = called(call, "notify_vendor")
        if not notified and (is_booked or price_issue):
            args = {"vendor_id": vendor_id, "invoice_number": invoice["invoice_number"]}
            if is_booked:
                return tool_call("notify_vendor", {**args, "template": "booked"})
            note = "Please issue a credit note for the price difference on the chairs (PO price 180.00 EUR)."
            return tool_call("notify_vendor", {**args, "template": "on_hold", "note": note})
        hold_case = re.search(r"APX-\d+", held[0]) if held else None
        reference = re.search(r"FI-\d{4}-\d+", booked[0]) if is_booked else None
        return self.finish(
            "ap_coordinator",
            {
                "document_id": invoice["document_id"],
                "invoice_number": invoice["invoice_number"],
                "vendor_id": vendor_id,
                "decision": "booked" if is_booked else "on_hold",
                "booking_reference": reference.group() if reference else None,
                "hold_case": hold_case.group() if hold_case else None,
                "gross_amount": invoice["gross_amount"],
                "currency": invoice["currency"],
                "reasons": reasons,
                "vendor_notified": bool(notified) and "Sent" in results(call, "notify_vendor")[0],
            },
        )

    def hold_for_missing_fields(self, call: ModelCall, invoice: dict[str, Any]) -> Any:
        reasons = [
            f"Missing on the invoice: {', '.join(invoice['missing_fields'])}; ask the vendor."
        ]
        held = results(call, "hold_invoice")
        if not held:
            return tool_call(
                "hold_invoice",
                {
                    "document_id": invoice["document_id"],
                    "invoice_number": invoice["invoice_number"],
                    "vendor_id": "unknown",
                    "reasons": reasons,
                },
            )
        hold_case = re.search(r"APX-\d+", held[0])
        return self.finish(
            "ap_coordinator",
            {
                "document_id": invoice["document_id"],
                "invoice_number": invoice["invoice_number"],
                "decision": "on_hold",
                "hold_case": hold_case.group() if hold_case else None,
                "gross_amount": invoice["gross_amount"],
                "currency": invoice["currency"],
                "reasons": reasons,
                "vendor_notified": False,
            },
        )

    def extractor(self, call: ModelCall) -> Any:
        document = re.search(r"DOC-\d+", request(call))
        assert document is not None
        fetched = results(call, "fetch_invoice_document")
        if not fetched:
            return tool_call("fetch_invoice_document", {"document_id": document.group()})
        match = INVOICE_PATTERN.search(fetched[0])
        assert match is not None, fetched[0]
        field = match.groupdict()
        has_po = field["po"].startswith("PO-")
        return self.finish(
            "extractor",
            {
                "document_id": document.group(),
                "vendor_name": field["vendor"],
                "vendor_vat_id": field["vat"],
                "invoice_number": field["number"],
                "invoice_date": field["date"],
                "due_date": field["due"],
                **({"po_number": field["po"]} if has_po else {}),
                "currency": "EUR",
                "net_amount": float(field["net"]),
                "tax_amount": float(field["tax"]),
                "gross_amount": float(field["gross"]),
                "iban": field["iban"],
                "lines": [
                    {
                        "description": field["item"],
                        "quantity": float(field["qty"]),
                        "unit_price": float(field["price"]),
                        "amount": float(field["amount"]),
                    }
                ],
                "missing_fields": [] if has_po else ["po_number"],
            },
        )

    def validator(self, call: ModelCall) -> Any:
        invoice = first_json(request(call))
        if not results(call, "get_vendor"):
            return [
                tool_call("get_vendor", {"query": invoice["vendor_vat_id"]}),
                tool_call("get_purchase_order", {"po_number": invoice["po_number"]}),
                tool_call("get_goods_receipt", {"po_number": invoice["po_number"]}),
            ]
        vendor = first_json(results(call, "get_vendor")[0])
        order = first_json(results(call, "get_purchase_order")[0])
        receipt = first_json(results(call, "get_goods_receipt")[0])
        differences = []
        for line, ordered, received in zip(
            invoice["lines"], order["lines"], receipt["receipts"], strict=True
        ):
            if abs(line["unit_price"] - ordered["unit_price"]) > 0.02 * ordered["unit_price"]:
                differences.append(
                    {
                        "field": f"lines[0].unit_price ({line['description']})",
                        "invoice_value": f"{line['unit_price']:.2f}",
                        "expected_value": f"{ordered['unit_price']:.2f}",
                    }
                )
            if line["quantity"] > received["quantity_received"]:
                differences.append(
                    {
                        "field": "lines[0].quantity",
                        "invoice_value": str(line["quantity"]),
                        "expected_value": str(received["quantity_received"]),
                    }
                )
        return self.finish(
            "validator",
            {
                "status": "mismatch" if differences else "match",
                "vendor_id": vendor["vendor_id"],
                "differences": differences,
            },
        )

    def policy_checker(self, call: ModelCall) -> Any:
        invoice = first_json(request(call))
        if not results(call, "get_ap_policy"):
            return [
                tool_call("get_ap_policy"),
                tool_call("get_vendor", {"query": invoice["vendor_vat_id"]}),
            ]
        vendor = first_json(results(call, "get_vendor")[0])
        duplicates = results(call, "find_booked_invoices")
        if not duplicates:
            return tool_call(
                "find_booked_invoices",
                {"vendor_id": vendor["vendor_id"], "invoice_number": invoice["invoice_number"]},
            )
        findings = []
        if first_json(duplicates[0])["matches"]:
            findings.append(
                {
                    "rule": "AP-2",
                    "severity": "block",
                    "detail": "The invoice number is already booked.",
                }
            )
        if invoice["iban"] != vendor["iban"]:
            findings.append(
                {
                    "rule": "AP-3",
                    "severity": "block",
                    "detail": "The IBAN differs from the vendor master; verify it with the "
                    "vendor through the contact on file.",
                }
            )
        large = invoice["gross_amount"] > 10_000
        if large:
            findings.append(
                {
                    "rule": "AP-4",
                    "severity": "warn",
                    "detail": "Above 10,000 EUR: manager approval.",
                }
            )
        compliant = not any(f["severity"] == "block" for f in findings)
        return self.finish(
            "policy_checker",
            {"compliant": compliant, "needs_manager_approval": large, "findings": findings},
        )


async def scenario_invoice_processing(recipe: Recipe) -> None:
    tools = recipe.tools_module
    team = recipe.build_resumable(InvoiceScript(recipe))
    run = await isolated_run(team, "Process invoice document DOC-1002 from the AP inbox.")
    paused = run.result
    assert paused is not None and paused.is_interrupted, run.error
    first, *checks = delegation_targets(run.events)
    assert (first, sorted(checks)) == ("extractor", ["policy_checker", "validator"])
    pending = paused.interrupts[0]["value"]
    assert pending["tool"] == "book_invoice"
    assert pending["args"]["gross_amount"] == 23800.0
    assert len(tools.LEDGER) == 1  # nothing booked before the approval

    assert paused.thread_id is not None
    done = await team.resume(paused.thread_id, {"action": "approve"})

    outcome = done.output
    assert outcome.decision == "booked"
    assert outcome.booking_reference == tools.LEDGER[-1]["booking_reference"]
    assert outcome.vendor_notified is True
    assert tools.LEDGER[-1]["invoice_number"] == "88231"
    assert tools.OUTBOX == [
        {"vendor_id": "V-200", "invoice_number": "88231", "template": "booked", "note": ""}
    ]


# ---------------------------------------------------------------------- 2 customer support desk


def all_text(call: ModelCall) -> str:
    return "\n".join(str(m.content) for m in call.messages)


class SupportScript(Script):
    def customer(self, call: ModelCall) -> str:
        found = re.search(r"C-\d{4}", request(call))
        assert found is not None
        return found.group()

    def triage(self, call: ModelCall) -> Any:
        text, customer = request(call).lower(), self.customer(call)
        if not results(call, "lookup_customer"):
            return [
                tool_call("lookup_customer", {"customer_id": customer}),
                tool_call("recall", {"query": customer}),
            ]
        if re.search(r"lawyer|manager|a person", text):
            ticket = results(call, "escalate_to_human")
            if not ticket:
                summary = f"{customer} asks for a manager after repeated contacts."
                return tool_call(
                    "escalate_to_human",
                    {"customer_id": customer, "summary": summary, "priority": "high"},
                )
            return f"I'm sorry this took so long. {ticket[0]}"
        if "news on my refund" in text:
            recalled = re.search(r"refunded (\d+\.\d\d)", all_text(call), re.IGNORECASE)
            assert recalled is not None, "the earlier refund should be recalled from memory"
            return (
                f"Your refund of {recalled.group(1)} was issued and arrives within 5 business days."
            )
        if "password" in text:
            articles = results(call, "search_knowledge_base")
            if not articles:
                return tool_call("search_knowledge_base", {"query": "reset password"})
            steps = first_json(articles[0])["articles"][0]["body"]
            return f"Here is how to reset your password: {steps}"
        for pattern, target in (
            (r"charged twice|invoice|charge", "billing_specialist"),
            (r"sync|outage|error", "technical_specialist"),
            (r"broken|damaged|return|money back", "returns_specialist"),
        ):
            if re.search(pattern, text):
                return tool_call(f"transfer_to_{target}", {"reason": f"{target} topic"})
        return "Could you tell me a bit more about what you need?"

    def billing_specialist(self, call: ModelCall) -> Any:
        customer = self.customer(call)
        payments = results(call, "lookup_invoices")
        if not payments:
            return tool_call("lookup_invoices", {"customer_id": customer})
        charges = first_json(payments[0])["payments"]
        refunded = results(call, "issue_refund")
        if not refunded:
            duplicate = charges[-1]
            return tool_call(
                "issue_refund",
                {
                    "customer_id": customer,
                    "amount": duplicate["amount"],
                    "reason": "duplicate charge",
                },
            )
        if not called(call, "remember"):
            note = f"{customer}: refunded {charges[-1]['amount']:.2f} duplicate charge on {charges[-1]['invoice']}."
            return tool_call("remember", {"content": note})
        return (
            f"You were charged twice for {charges[-1]['invoice']}. {refunded[0]} "
            "Sorry for the trouble!"
        )

    def technical_specialist(self, call: ModelCall) -> Any:
        status = results(call, "get_service_status")
        if not status:
            return tool_call("get_service_status", {"component": "sync"})
        state = first_json(status[0])
        return (
            f"There is a known incident ({state['incident']}): {state['detail']} "
            f"Next update: {state['next_update']}. No action is needed on your side."
        )

    def returns_specialist(self, call: ModelCall) -> Any:
        customer = self.customer(call)
        order_id = re.search(r"O-\d{4}", request(call))
        assert order_id is not None
        orders = results(call, "lookup_order")
        if not orders:
            return tool_call("lookup_order", {"order_id": order_id.group()})
        order = first_json(orders[0])
        refunded = results(call, "issue_refund")
        if not refunded:
            return tool_call(
                "issue_refund",
                {
                    "customer_id": customer,
                    "amount": order["total"],
                    "reason": "damaged on delivery",
                    "order_id": order_id.group(),
                },
            )
        if "rejected" in refunded[0]:
            return f"I could not refund the order: {refunded[0]}"
        if not called(call, "remember"):
            note = f"{customer}: refunded {order['total']:.2f} for damaged {order['item']} ({order_id.group()})."
            return tool_call("remember", {"content": note})
        return f"I'm sorry your {order['item']} arrived damaged. {refunded[0]}"


async def scenario_customer_support_desk(recipe: Recipe) -> None:
    tools = recipe.tools_module
    desk = recipe.build_resumable(SupportScript(recipe))
    message = "Hi, this is customer C-1001. My blender from order O-5001 arrived broken, I want my money back."
    run = await isolated_run(desk, message)
    paused = run.result
    assert paused is not None and paused.is_interrupted, run.error
    assert handoff_targets(run.events) == ["returns_specialist"]
    assert tool_names(run.events)[:3] == [
        "lookup_customer",
        "recall",
        "transfer_to_returns_specialist",
    ]
    pending = paused.interrupts[0]["value"]
    assert (pending["tool"], pending["args"]["amount"]) == ("issue_refund", 89.0)
    assert tools.REFUNDS == []

    assert paused.thread_id is not None
    done = await desk.resume(paused.thread_id, {"action": "approve"})
    assert done.agent == "returns_specialist"
    assert "Refunded 89.00" in done.output
    assert [refund["order_id"] for refund in tools.REFUNDS] == ["O-5001"]

    # A new conversation: the desk recalls the refund from its shared memory.
    later = await desk.run("C-1001 here again: any news on my refund?")
    assert later.handoff_path == ("triage",)
    assert "89.00" in later.output
    assert len(tools.REFUNDS) == 1


# ---------------------------------------------------------------------- 3 lead qualification


class LeadScript(Script):
    def sdr_lead(self, call: ModelCall) -> Any:
        lead_id = re.search(r"L-\d{3}", request(call))
        assert lead_id is not None
        records = results(call, "get_lead")
        if not records:
            return tool_call("get_lead", {"lead_id": lead_id.group()})
        lead = first_json(records[0])
        updates = called(call, "update_crm_lead")
        if lead["account_status"] == "customer":
            if not updates:
                note = f"Existing customer; route to {lead['account_manager']}: {lead['message']}"
                return tool_call(
                    "update_crm_lead",
                    {
                        "lead_id": lead["lead_id"],
                        "score": 0,
                        "tier": "not_scored",
                        "status": "existing_customer",
                        "notes": note,
                    },
                )
            return self.finish(
                "sdr_lead",
                {
                    "lead_id": lead["lead_id"],
                    "company": lead["company"],
                    "score": 0,
                    "tier": "not_scored",
                    "next_step": "route_to_account_manager",
                    "summary": f"Existing customer; {lead['account_manager']} takes it over.",
                },
            )
        reports = delegated_results(call)
        if "researcher" not in reports:
            brief = (
                f"Research {lead['company']} ({lead['domain']}). Their message: {lead['message']}"
            )
            return tool_call("delegate_task", {"agent": "researcher", "task": brief})
        if "qualifier" not in reports:
            brief = (
                f"Score this lead.\nLead: {json.dumps(lead)}\nResearch:\n{reports['researcher']}"
            )
            return tool_call("delegate_task", {"agent": "qualifier", "task": brief})
        score = first_json(reports["qualifier"])
        status = {"A": "sales_ready", "B": "sales_ready", "C": "nurture"}.get(
            score["tier"], "disqualified"
        )
        if not updates:
            return tool_call(
                "update_crm_lead",
                {
                    "lead_id": lead["lead_id"],
                    "score": score["score"],
                    "tier": score["tier"],
                    "status": status,
                    "notes": score["summary"],
                },
            )
        sent = results(call, "send_lead_email")
        if status == "sales_ready" and not sent:
            body = (
                f"Hi {lead['contact'].split()[0]},\n\ncongratulations on the two new depots in "
                "Leipzig and Hanover. You mentioned automating dispatch planning: which part "
                "takes your planners the most time today? A 20-minute call next week would be "
                "enough to show how other freight companies plan their tours.\n\nThe Sales Team"
            )
            return tool_call(
                "send_lead_email",
                {
                    "to": lead["email"],
                    "subject": "Dispatch planning for your new depots",
                    "body": body,
                },
            )
        if status == "sales_ready":
            next_step = "email_rejected" if "rejected" in sent[0] else "email_sent"
        else:
            next_step = status if status == "disqualified" else "nurture"
        return self.finish(
            "sdr_lead",
            {
                "lead_id": lead["lead_id"],
                "company": lead["company"],
                "score": score["score"],
                "tier": score["tier"],
                "next_step": next_step,
                "summary": score["summary"],
            },
        )

    def researcher(self, call: ModelCall) -> Any:
        brief = request(call)
        domain = re.search(r"\(([\w.-]+\.example)\)", brief)
        company = re.search(r"Research (.+?) \(", brief)
        assert domain is not None and company is not None
        if not results(call, "get_company_profile"):
            return [
                tool_call("get_company_profile", {"domain": domain.group(1)}),
                tool_call("search_company_news", {"company": company.group(1)}),
            ]
        profile = results(call, "get_company_profile")[0]
        facts = first_json(profile) if profile.startswith("{") else {}
        news = first_json(results(call, "search_company_news")[0])["items"]
        lines = [
            f"- {key}: {facts.get(key, 'unknown')}"
            for key in ("industry", "employees", "country", "business_model")
        ]
        lines += [f"- news {item['id']} ({item['date']}): {item['headline']}" for item in news]
        return "\n".join(lines or ["- nothing found"])

    def qualifier(self, call: ModelCall) -> Any:
        if not results(call, "get_icp_rubric"):
            return tool_call("get_icp_rubric")
        brief = request(call)
        lead = first_json(brief)
        fact = dict(re.findall(r"- (industry|employees|country|business_model): (.+)", brief))
        industry, model = fact.get("industry", "unknown"), fact.get("business_model", "unknown")
        employees = int(fact["employees"]) if fact.get("employees", "unknown").isdigit() else None
        signals = int("evaluating" in lead["message"]) + brief.count("- news ")
        criteria = [
            ("Industry", 30 if industry in ("logistics", "wholesale", "manufacturing") else 0, 30),
            ("Company size", 25 if employees and 200 <= employees <= 5000 else 0, 25),
            ("Region", 15 if fact.get("country") in ("DE", "AT", "FR", "UK", "CH") else 0, 15),
            ("Buying signal", 20 if signals >= 2 else 10 if signals else 0, 20),
            (
                "Seniority",
                10
                if re.search(r"head|director|vp|chief", lead["title"], re.I)
                else 5
                if "manager" in lead["title"].lower()
                else 0,
                10,
            ),
        ]
        total = sum(points for _, points, _ in criteria)
        disqualifiers = []
        if model == "B2C":
            disqualifiers.append("consumer business (B2C)")
        if employees is not None and employees < 20:
            disqualifiers.append("fewer than 20 employees")
        if disqualifiers:
            tier = "disqualified"
        elif employees is None:
            tier = "C"
        else:
            tier = (
                "A"
                if total >= 75
                else "B"
                if total >= 50
                else "C"
                if total >= 25
                else "disqualified"
            )
        summary = {
            "A": f"Strong ICP fit ({total}/100): {industry}, {employees} employees, active buying signals.",
            "C": f"Firmographics unknown; scored {total}/100 on what is known. Needs manual research.",
            "disqualified": f"Disqualified: {', '.join(disqualifiers) or 'too few points'}.",
        }.get(tier, f"Moderate fit ({total}/100).")
        return self.finish(
            "qualifier",
            {
                "score": total,
                "tier": tier,
                "criteria": [
                    {"criterion": name, "points": points, "max_points": top, "evidence": "research"}
                    for name, points, top in criteria
                ],
                "disqualifiers": disqualifiers,
                "summary": summary,
            },
        )


async def scenario_lead_qualification(recipe: Recipe) -> None:
    tools = recipe.tools_module
    team = recipe.build_resumable(LeadScript(recipe))
    run = await isolated_run(team, "Qualify inbound lead L-301.")
    paused = run.result
    assert paused is not None and paused.is_interrupted, run.error
    assert delegation_targets(run.events) == ["researcher", "qualifier"]
    assert tools.CRM_UPDATES == [
        {
            "lead_id": "L-301",
            "score": 100,
            "tier": "A",
            "status": "sales_ready",
            "notes": "Strong ICP fit (100/100): logistics, 420 employees, active buying signals.",
        }
    ]
    email = paused.interrupts[0]["value"]
    assert email["tool"] == "send_lead_email"
    assert email["args"]["to"] == "alex.sample@sample-freight.example"
    assert "new depots" in email["args"]["body"] and tools.OUTBOX == []

    assert paused.thread_id is not None
    done = await team.resume(paused.thread_id, {"action": "approve"})
    assert (done.output.tier, done.output.next_step) == ("A", "email_sent")
    assert [mail["to"] for mail in tools.OUTBOX] == ["alex.sample@sample-freight.example"]


# ---------------------------------------------------------------------- task-board teams and networks


FINAL_TURN = "Write the final answer for the user."
TASK_LINE = re.compile(r"Task (task_\d+): (.+)")


def task_results(call: ModelCall) -> dict[str, str]:
    """Team mode: the result of every delegated board task, by task id."""

    tasks = {
        item["id"]: TASK_LINE.search(str(item["args"]["task"]))
        for m in call.messages
        if isinstance(m, AIMessage)
        for item in m.tool_calls
        if item["name"] == "delegate_task"
    }
    found: dict[str, str] = {}
    for m in call.messages:
        if isinstance(m, ToolMessage) and m.name == "delegate_task":
            task = tasks.get(m.tool_call_id)
            body = re.search(r"<result[^>]*>\s*(.*?)\s*</result>", str(m.content), re.S)
            if task is not None and body is not None:
                found[task.group(1)] = body.group(1)
    return found


def conversation(call: ModelCall) -> str:
    """Everything but the system prompt."""

    return "\n".join(str(m.content) for m in call.messages if not isinstance(m, SystemMessage))


class BoardScript(Script):
    """Shared behavior of a lead that plans on the task board.

    As a network lead it plans once and waits for the handbacks; as a team orchestrator
    (the blueprint's own entrypoint) it delegates every task whose dependencies are done, a batch at a time.
    """

    def plan(self, call: ModelCall) -> Any:
        raise NotImplementedError

    def follow_up(self, call: ModelCall) -> Any:
        """Tasks to add once earlier results are in (none by default)."""

        return None

    def lead_turn(self, call: ModelCall, final: Callable[[], Any]) -> Any:
        created = results(call, "create_tasks")
        if not created:
            return self.plan(call)
        if "delegate_task" not in call.tool_names:  # network: teammates claim the tasks
            last = call.last_message
            if isinstance(last, ToolMessage) and last.name == "create_tasks":
                return "Planned; the teammates take it from here."
            extra = self.follow_up(call)
            if extra is not None:
                return extra
            return final() if FINAL_TURN in all_text(call) else "Noted."
        planned: list[tuple[str, dict[str, Any], list[str]]] = []
        for args, result in zip(called(call, "create_tasks"), created, strict=True):
            ids = first_json(result)
            for task in args["tasks"]:
                depends = [ids.get(key, key) for key in task.get("depends_on", [])]
                planned.append((ids[task["key"]], task, depends))
        done = task_results(call)
        ready = [
            (task_id, task, depends)
            for task_id, task, depends in planned
            if task_id not in done and set(depends) <= set(done)
        ]
        if not ready:
            extra = self.follow_up(call)
            return extra if extra is not None else final()
        briefs = []
        for task_id, task, depends in ready:
            brief = f"Task {task_id}: {task['title']}\n{task['description']}"
            if depends:
                brief += "\n\nResults of the tasks this builds on:\n" + "\n".join(
                    f"- {dep}: {done[dep]}" for dep in depends
                )
            agent = self.owner(task["required_capabilities"][0])
            briefs.append(tool_call("delegate_task", {"agent": agent, "task": brief}))
        return briefs

    def owner(self, capability: str) -> str:
        return next(
            spec.name for spec in self.recipe.blueprint.agents if capability in spec.capabilities
        )

    def teammate(self, call: ModelCall, act: Callable[[str, str], Any]) -> Any:
        """Claim (team mode), act on the task, then complete it (team mode) or answer."""

        brief = request(call)
        found = TASK_LINE.search(brief)
        assert found is not None, brief
        task_id, title = found.groups()
        if "claim_task" in call.tool_names and not called(call, "claim_task"):
            return tool_call("claim_task", {"task_id": task_id})
        outcome = act(title.strip(), brief)
        if not isinstance(outcome, str):
            return outcome  # a tool call
        if "complete_task" in call.tool_names and not called(call, "complete_task"):
            return tool_call("complete_task", {"task_id": task_id, "result": outcome})
        return outcome


# ---------------------------------------------------------------------- 4 employee onboarding


FIRST_WEEK = [
    "Mon 09:00 welcome with the manager",
    "Mon 11:00 IT setup",
    "Tue 12:30 lunch with the buddy",
    "Wed 10:00 team introduction",
    "Fri 15:00 check-in with the manager",
]


class OnboardingScript(BoardScript):
    def hire(self, call: ModelCall) -> dict[str, Any] | None:
        record = results(call, "get_new_hire")
        return first_json(record[0]) if record and record[0].startswith("{") else None

    def plan(self, call: ModelCall) -> Any:
        hire = self.hire(call)
        facts = (
            f"Hire {hire['hire_id']}: {hire['name']}, {hire['role']}, {hire['department']}, "
            f"starts {hire['start_date']}, location {hire['location']}, {hire['employment_type']}."
            if hire
            else ""
        )

        def task(key: str, title: str, capability: str, *depends: str) -> dict[str, Any]:
            return {
                "key": key,
                "title": title,
                "description": facts,
                "required_capabilities": [capability],
                "depends_on": list(depends),
            }

        office = hire is not None and hire["location"] != "remote"
        tasks = [
            task("accounts", "Create accounts", "it"),
            task("hardware", "Order hardware", "it"),
        ]
        if hire is not None and hire["employment_type"] == "employee":
            tasks.append(task("payroll", "Create payroll record", "hr"))
        tasks.append(task("buddy", "Assign buddy", "hr"))
        if office:
            tasks += [
                task("desk", "Reserve desk", "facilities"),
                task("badge", "Issue badge", "facilities", "desk"),
            ]
        tasks.append(
            task(
                "week",
                "Plan the first week",
                "hr",
                "accounts",
                "buddy",
                *(["desk"] if office else []),
            )
        )
        return tool_call("create_tasks", {"tasks": tasks})

    def onboarding_lead(self, call: ModelCall) -> Any:
        hire_id = re.search(r"H-\d{4}", request(call))
        assert hire_id is not None
        if not results(call, "get_new_hire"):
            return tool_call("get_new_hire", {"hire_id": hire_id.group()})
        hire = self.hire(call)
        if hire is None:
            return f"There is no new hire {hire_id.group()} in the HR system; nothing was planned."

        def final() -> Any:
            text = all_text(call)
            references = sorted(
                set(re.findall(r"[\w.]+@company\.example|PR-\d+|B-3\.\d+|BD-\d+|\w+ Mentor", text))
            )
            summary = f"Onboarding of {hire['name']} ({hire['hire_id']}): {', '.join(references)}."
            if not called(call, "send_manager_summary"):
                return tool_call(
                    "send_manager_summary", {"hire_id": hire["hire_id"], "summary": summary}
                )
            waiting = ["procurement approval of the hardware"]
            if "payroll" in text.lower() and hire["employment_type"] == "employee":
                waiting.append("payroll approval")
            return f"{summary}\nWaiting for people: {', '.join(waiting)}."

        return self.lead_turn(call, final)

    def hire_of(self, brief: str) -> str:
        found = re.search(r"H-\d{4}", brief)
        assert found is not None
        return found.group()

    def act(self, call: ModelCall, actions: dict[str, tuple[str, dict[str, Any]]]) -> Any:
        def work(title: str, brief: str) -> Any:
            name, extra = actions[title]
            done = results(call, name)
            if done:
                return done[0]
            return tool_call(name, {"hire_id": self.hire_of(brief), **extra})

        return self.teammate(call, work)

    def it_specialist(self, call: ModelCall) -> Any:
        ship_to = "home" if "location remote" in request(call) else "office"
        return self.act(
            call,
            {
                "Create accounts": ("create_accounts", {}),
                "Order hardware": ("order_hardware", {"ship_to": ship_to}),
            },
        )

    def hr_specialist(self, call: ModelCall) -> Any:
        return self.act(
            call,
            {
                "Create payroll record": ("create_payroll_record", {}),
                "Assign buddy": ("assign_buddy", {}),
                "Plan the first week": ("schedule_first_week", {"sessions": FIRST_WEEK}),
            },
        )

    def facilities_coordinator(self, call: ModelCall) -> Any:
        return self.act(
            call, {"Reserve desk": ("assign_desk", {}), "Issue badge": ("issue_badge", {})}
        )


async def scenario_employee_onboarding(recipe: Recipe) -> None:
    tools = recipe.tools_module
    script = OnboardingScript(recipe)
    network = recipe.build(script)  # the blueprint's `network:` entrypoint
    assert isinstance(network, AgentNetwork)
    run = await isolated_run(network, "Onboard new hire H-2001.")
    result = run.result
    assert result is not None, run.error
    assert result.metadata["tasks"]["completed"] == 7
    assert result.metadata["stop_reason"] == "completed"
    owners = {task.title: task.owner for task in network.task_board.list()}
    assert owners == {
        "Create accounts": "it_specialist",
        "Order hardware": "it_specialist",
        "Create payroll record": "hr_specialist",
        "Assign buddy": "hr_specialist",
        "Reserve desk": "facilities_coordinator",
        "Issue badge": "facilities_coordinator",
        "Plan the first week": "hr_specialist",
    }
    assert sorted(set(delegation_targets(run.events))) == [
        "facilities_coordinator",
        "hr_specialist",
        "it_specialist",
    ]
    # Dependencies held: the badge came after the desk, the first week after everything it needs.
    calls = tool_names(run.events)
    assert calls.index("issue_badge") > calls.index("assign_desk")
    assert calls.index("schedule_first_week") > max(
        calls.index("create_accounts"), calls.index("assign_buddy"), calls.index("assign_desk")
    )
    assert tools.PAYROLL_DRAFTS == [{"hire_id": "H-2001", "status": "awaiting approval"}]
    assert tools.MANAGER_NOTES[0]["to"] == "Chris Lead"
    assert "maria.sample@company.example" in result.output and "PR-2001" in result.output
    assert "payroll approval" in result.output


# ---------------------------------------------------------------------- 5 contract review


FINDING = re.compile(
    r"(LEGAL|COMPLIANCE|COMMERCIAL) \| (high|medium|low) \| clause (\S+) \| (.+?) \| (\w+) \| (.+)"
)
EU_LAW = ("German", "Irish", "French", "Dutch", "Austrian", "Swiss")


def playbook_redline(playbook: str, rule: str) -> str:
    """The fallback redline the playbook quotes for `rule`, or "no fallback"."""

    paragraph = re.search(rf"^{rule} (.*?)(?=^\w\d |\Z)", playbook, re.S | re.M)
    assert paragraph is not None, rule
    quoted = re.search(r'Fallback(?: redline)?: "([^"]+)"', paragraph.group(1))
    return " ".join(quoted.group(1).split()) if quoted else "no fallback"


class ContractScript(Script):
    def contract_id(self, call: ModelCall) -> str:
        found = re.search(r"CT-\d+", request(call))
        assert found is not None
        return found.group()

    def intake(self, call: ModelCall) -> Any:
        contract = results(call, "get_contract")
        if not contract:
            return tool_call("get_contract", {"contract_id": self.contract_id(call)})
        if not contract[0].startswith("{"):
            return "CONTRACT NOT FOUND"
        data = first_json(contract[0])
        party = results(call, "get_counterparty")
        if not party:
            return tool_call("get_counterparty", {"name": data["counterparty"]})
        record = first_json(party[0])
        clauses = "\n".join(f"- {n}: {text[:60]}" for n, text in data["clauses"].items())
        return (
            f"{data['contract_id']}: {data['title']}\nCounterparty: {data['counterparty']} "
            f"({record['relationship']}, risk {record['risk_rating']})\n"
            f"Annual value: {data['annual_value_eur']} EUR\nClauses:\n{clauses}"
        )

    def review(
        self,
        call: ModelCall,
        area: str,
        checks: Callable[[dict[str, str], int], list[tuple[str, str, str, str]]],
    ) -> Any:
        label = area.upper()
        if "CONTRACT NOT FOUND" in request(call):
            return f"{label} | not reviewed"
        if not results(call, "get_playbook"):
            return [
                tool_call("get_contract", {"contract_id": self.contract_id(call)}),
                tool_call("get_playbook", {"area": area}),
            ]
        contract = first_json(results(call, "get_contract")[0])
        playbook = results(call, "get_playbook")[0]
        lines = [
            f"{label} | {severity} | clause {clause} | {issue} | {rule} | {playbook_redline(playbook, rule)}"
            for severity, clause, issue, rule in checks(
                contract["clauses"], contract["annual_value_eur"]
            )
        ]
        return "\n".join(lines) or f"{label} | no findings"

    def legal_reviewer(self, call: ModelCall) -> Any:
        def checks(clauses: dict[str, str], _value: int) -> list[tuple[str, str, str, str]]:
            found = []
            if "Customer's liability is unlimited" in clauses["4"]:
                found.append(
                    ("high", "4", "Unlimited liability for us, supplier capped at 5,000 EUR", "L1")
                )
            if "Customer indemnifies" in clauses.get("5", ""):
                found.append(
                    ("medium", "5", "One-sided indemnity for all third-party claims", "L2")
                )
            if "may not terminate" in clauses["3"]:
                found.append(("medium", "3", "No termination for convenience for us", "L3"))
            elif "terminate for convenience" not in " ".join(clauses.values()):
                found.append(("medium", "-", "No termination for convenience clause", "L3"))
            if not any(law in clauses["6"] for law in EU_LAW):
                found.append(("high", "6", "Governing law outside the EU", "L4"))
            return found

        return self.review(call, "legal", checks)

    def compliance_reviewer(self, call: ModelCall) -> Any:
        def checks(clauses: dict[str, str], _value: int) -> list[tuple[str, str, str, str]]:
            text = " ".join(clauses.values())
            found = []
            if "personal data" in text and "data processing agreement" not in text:
                found.append(("high", "5", "Personal data processed without a DPA", "C1"))
            if "any region" in text:
                found.append(("high", "5", "Personal data may be hosted outside the EU/EEA", "C2"))
            if "subcontractors at its discretion" in text:
                found.append(
                    ("medium", "5", "Sub-processors without notice or objection right", "C3")
                )
            return found

        return self.review(call, "compliance", checks)

    def commercial_reviewer(self, call: ModelCall) -> Any:
        def checks(clauses: dict[str, str], value: int) -> list[tuple[str, str, str, str]]:
            text = " ".join(clauses.values())
            found = []
            if "increase prices at any time" in text:
                found.append(("medium", "2", "Unilateral price increases at any time", "M2"))
            if "within 14 days" in text:
                found.append(("medium", "3", "Payment term of 14 days", "M1"))
            if "5% per month" in text:
                found.append(("medium", "3", "Late interest of 5% per month", "M3"))
            notice = re.search(r"renewing .* (\d+) days' notice", text)
            if notice and int(notice.group(1)) < 60:
                found.append(("low", "4", f"Renewal notice of only {notice.group(1)} days", "M4"))
            if value > 250_000:
                found.append(("high", "-", "Annual value above 250,000 EUR", "M5"))
            return found

        return self.review(call, "commercial", checks)

    def editor(self, call: ModelCall) -> Any:
        text = request(call)
        contract_id = self.contract_id(call)
        order = {"high": 0, "medium": 1, "low": 2}
        areas = {"LEGAL": 0, "COMPLIANCE": 1, "COMMERCIAL": 2}
        rows = sorted(FINDING.findall(text), key=lambda r: (order[r[1]], areas[r[0]]))
        findings = [
            {
                "priority": index,
                "severity": severity,
                "area": area.lower(),
                "clause": clause,
                "issue": issue,
                "playbook_rule": rule,
                "suggested_redline": None if redline.startswith("no fallback") else redline,
            }
            for index, (area, severity, clause, issue, rule, redline) in enumerate(rows, 1)
        ]
        if "CONTRACT NOT FOUND" in text:
            verdict, summary = "cannot_review", f"{contract_id} is not in the contract repository."
        elif not any(f["severity"] != "low" for f in findings):
            verdict, summary = "sign", "The contract meets all three playbooks."
        elif any(f["suggested_redline"] is None or "Unlimited" in f["issue"] for f in findings):
            verdict, summary = (
                "escalate_to_legal",
                "Positions outside the playbook need legal counsel.",
            )
        else:
            verdict, summary = "sign_with_changes", "Sign after the suggested redlines are agreed."
        risk = next(
            (s for s in ("high", "medium", "low") if any(f["severity"] == s for f in findings)),
            "low",
        )
        return self.finish(
            "editor",
            {
                "contract_id": contract_id,
                "verdict": verdict,
                "risk": risk,
                "summary": summary,
                "findings": findings,
            },
        )


async def scenario_contract_review(recipe: Recipe) -> None:
    workflow = recipe.build(ContractScript(recipe))
    run = await isolated_run(workflow, "Review contract CT-103 before we sign it.")
    result = run.result
    assert result is not None, run.error
    calls = tool_names(run.events)
    assert calls[:2] == ["get_contract", "get_counterparty"]
    assert calls.count("get_playbook") == 3  # three reviewers, in parallel
    verdict = result.output
    assert (verdict.contract_id, verdict.verdict, verdict.risk) == (
        "CT-103",
        "sign_with_changes",
        "high",
    )
    assert [(f.priority, f.severity, f.playbook_rule) for f in verdict.findings] == [
        (1, "high", "C1"),
        (2, "high", "C2"),
        (3, "medium", "C3"),
    ]
    assert verdict.findings[0].suggested_redline.startswith(
        "The parties' data processing agreement"
    )


# ---------------------------------------------------------------------- 6 IT incident triage


SERVICES = ("payments-api", "checkout", "reporting", "search")


class IncidentScript(Script):
    def incident_commander(self, call: ModelCall) -> Any:
        text = request(call)
        reports = delegated_results(call)
        resolved = re.search(r"(INC-\d+) is resolved", text)
        if resolved:
            incident_id = resolved.group(1)
            if "communications_lead" not in reports:
                brief = f"Write the post-incident notes for {incident_id}; it is resolved."
                return tool_call("delegate_task", {"agent": "communications_lead", "task": brief})
            saved = reports["communications_lead"]
            facts = re.search(r"Service: (\S+), (SEV\d)\. Root cause: (.+?)\. Action: (.+)", saved)
            assert facts is not None, saved
            return self.finish(
                "incident_commander",
                {
                    "incident_id": incident_id,
                    "service": facts.group(1),
                    "severity": facts.group(2),
                    "status": "resolved",
                    "paged": False,
                    "likely_cause": facts.group(3),
                    "next_steps": [facts.group(4)],
                    "public_update_posted": False,
                },
            )
        service = next(name for name in SERVICES if name in text)
        records = results(call, "get_service")
        if not records:
            return tool_call("get_service", {"service": service})
        record = first_json(records[0])
        if record["tier"] == 1 and re.search(r"down|cannot", text):
            severity = "SEV1"
        elif record["tier"] == 1 or (record["tier"] == 2 and "down" in text):
            severity = "SEV2"
        else:
            severity = "SEV3"
        opened = results(call, "open_incident")
        if not opened:
            return tool_call(
                "open_incident",
                {
                    "title": text.split(":", 1)[-1].strip()[:80],
                    "service": service,
                    "severity": severity,
                },
            )
        incident_id = re.search(r"INC-\d+", opened[0])
        assert incident_id is not None
        urgent = severity in ("SEV1", "SEV2")
        paged = results(call, "page_responders")
        if urgent and not paged:
            return tool_call(
                "page_responders",
                {
                    "incident_id": incident_id.group(),
                    "service": service,
                    "severity": severity,
                    "summary": text,
                },
            )
        if "diagnostics_engineer" not in reports:
            brief = f"Diagnose {incident_id.group()} on {service}: {text}"
            return tool_call("delegate_task", {"agent": "diagnostics_engineer", "task": brief})
        diagnosis = reports["diagnostics_engineer"]
        public = urgent and record["customer_facing"]
        if public and "communications_lead" not in reports:
            brief = (
                f"Post a public status update for {incident_id.group()} ({severity}, {service}). "
                f"Diagnosis:\n{diagnosis}"
            )
            return tool_call("delegate_task", {"agent": "communications_lead", "task": brief})
        cause = re.search(r"Likely cause: (.+)", diagnosis)
        mitigation = re.search(r"Mitigation: (.+)", diagnosis)
        assert cause is not None and mitigation is not None
        return self.finish(
            "incident_commander",
            {
                "incident_id": incident_id.group(),
                "service": service,
                "severity": severity,
                "status": "mitigating" if urgent else "investigating",
                "paged": bool(paged) and paged[0].startswith("Paged"),
                "likely_cause": cause.group(1),
                "next_steps": [mitigation.group(1)],
                "public_update_posted": "Posted the public"
                in reports.get("communications_lead", ""),
            },
        )

    def diagnostics_engineer(self, call: ModelCall) -> Any:
        service = next(name for name in SERVICES if name in request(call))
        if not results(call, "get_service_health"):
            runbook = {"checkout": "RB-12", "payments-api": "RB-14", "reporting": "RB-3"}[service]
            return [
                tool_call("get_service_health", {"service": service}),
                tool_call("query_logs", {"service": service, "minutes": 30}),
                tool_call("get_recent_deploys", {"service": service}),
                tool_call("lookup_runbook", {"runbook_id": runbook}),
            ]
        health = first_json(results(call, "get_service_health")[0])
        logs = first_json(results(call, "query_logs")[0])["lines"]
        deploys = first_json(results(call, "get_recent_deploys")[0])["deploys"]
        causes = {
            "checkout": (
                "deploy D-881 lowered the DB connection pool to 20",
                "RB-12 step 1: roll back D-881",
            ),
            "payments-api": (
                "upstream card network timeouts",
                "RB-14 step 2: enable the retry circuit breaker",
            ),
            "reporting": (
                "full table scan in reports_daily after D-870",
                "RB-3 step 2: cancel the query and add the index",
            ),
        }
        cause, mitigation = causes[service]
        evidence = "; ".join([*logs, *(f"deploy {d['id']} at {d['at']}" for d in deploys)])
        return (
            f"Impact: {service} {health['status']} ({health['error_rate_pct']}% errors, "
            f"p95 {health['p95_ms']} ms)\nEvidence: {evidence}\nLikely cause: {cause}\n"
            f"Mitigation: {mitigation}"
        )

    def communications_lead(self, call: ModelCall) -> Any:
        brief = request(call)
        incident_id = re.search(r"INC-\d+", brief)
        assert incident_id is not None
        incident = results(call, "get_incident")
        if not incident:
            return tool_call("get_incident", {"incident_id": incident_id.group()})
        record = first_json(incident[0])
        if "post-incident notes" in brief:
            saved = results(call, "save_post_incident_notes")
            if not saved:
                notes = (
                    f"Summary: {record['title']}.\nTimeline:\n"
                    + "\n".join(record["timeline"])
                    + "\nRoot cause: index alias pointed to an empty index.\n"
                    "Action items: alias switch guard in the re-indexing job (discovery team, 2026-10-10)."
                )
                return tool_call(
                    "save_post_incident_notes", {"incident_id": incident_id.group(), "notes": notes}
                )
            return (
                f"{saved[0]} Service: {record['service']}, {record['severity']}. Root cause: index "
                "alias pointed to an empty index after re-indexing. Action: add an alias switch "
                "guard to the re-indexing job (discovery team)"
            )
        posted = results(call, "post_status_update")
        if not posted:
            message = (
                f"We are investigating problems with {record['service']}: some customers cannot "
                "complete their purchase. Our engineers are working on a fix. Next update in 30 minutes."
            )
            return tool_call(
                "post_status_update",
                {"incident_id": incident_id.group(), "audience": "public", "message": message},
            )
        return posted[0]


async def scenario_it_incident_triage(recipe: Recipe) -> None:
    tools = recipe.tools_module
    team = recipe.build_resumable(IncidentScript(recipe))
    run = await isolated_run(team, "ALERT checkout: error rate 38%, customers cannot pay.")
    paused = run.result
    assert paused is not None and paused.is_interrupted, run.error
    page = paused.interrupts[0]["value"]
    assert (page["tool"], page["args"]["severity"]) == ("page_responders", "SEV1")
    assert tool_names(run.events) == ["get_service", "open_incident", "page_responders"]
    assert tools.PAGES == []

    assert paused.thread_id is not None
    second = await team.resume(paused.thread_id, {"action": "approve"})
    assert second.is_interrupted  # the public status update waits for its own approval
    update = second.interrupts[0]["value"]
    assert (update["tool"], update["args"]["audience"]) == ("post_status_update", "public")
    assert tools.PAGES[0]["targets"] == ["payments-oncall", "major-incident", "incident-manager"]

    done = await team.resume(paused.thread_id, {"action": "approve"})
    record = done.output
    assert (record.incident_id, record.severity, record.status) == (
        "INC-7101",
        "SEV1",
        "mitigating",
    )
    assert record.paged and record.public_update_posted
    assert "D-881" in record.likely_cause and record.next_steps == ["RB-12 step 1: roll back D-881"]
    assert [u["audience"] for u in tools.STATUS_UPDATES] == ["public"]


# ---------------------------------------------------------------------- 7 market research report


GOAL = re.compile(
    r"report(?P<scope>, competitors only)?: (?P<market>.+?)(?: for (?P<segment>.+?))? "
    r"in (?P<region>[\w-]+)\.?$"
)
REPORT = re.compile(r"# Market report:.*?## Sources\n(?:- S-\d+: [^\n]*(?:\n|$))*", re.S)
UNSOURCED = "The market will double by 2028."
SOURCE_LINE = re.compile(r"^- (S-\d+): (.+)$", re.M)
STATISTIC_LINES = ("- Market size", "- Growth", "- Segments", "- Adoption")


def fact(brief: str, name: str) -> str:
    found = re.search(rf"^{name}: (.+)$", brief, re.M)
    assert found is not None, brief
    return found.group(1).strip()


class MarketScript(BoardScript):
    def plan(self, call: ModelCall) -> Any:
        goal = request(call).strip()
        parsed = GOAL.search(goal)
        assert parsed is not None, goal
        description = (
            f"Question: {goal}\nMarket: {parsed['market']}\nRegion: {parsed['region']}\n"
            f"Segment: {parsed['segment'] or parsed['market']}"
        )

        def task(key: str, title: str, capability: str, *depends: str) -> dict[str, Any]:
            return {
                "key": key,
                "title": title,
                "description": description,
                "required_capabilities": [capability],
                "depends_on": list(depends),
            }

        if parsed["scope"]:
            tasks = [
                task("competitors", "Competitor research", "competitor-research"),
                task("draft", "Draft report", "report-writing", "competitors"),
            ]
        else:
            tasks = [
                task("market", "Market research", "market-research"),
                task("competitors", "Competitor research", "competitor-research"),
                task("analysis", "Market analysis", "data-analysis", "market", "competitors"),
                task(
                    "draft", "Draft report", "report-writing", "market", "competitors", "analysis"
                ),
            ]
        tasks.append(task("review", "Review report", "quality-review", "draft"))
        return tool_call("create_tasks", {"tasks": tasks})

    def follow_up(self, call: ModelCall) -> Any:
        text = conversation(call)
        if "VERDICT: REVISE" not in text or len(called(call, "create_tasks")) > 1:
            return None
        ids = first_json(results(call, "create_tasks")[0])
        issues = text.rsplit("VERDICT: REVISE", 1)[1].strip().split("\n\n")[0]
        plan = called(call, "create_tasks")[0]["tasks"][0]["description"]
        return tool_call(
            "create_tasks",
            {
                "tasks": [
                    {
                        "key": "revise",
                        "title": "Revise report",
                        "description": f"{plan}\nIssues to fix:\n{issues}",
                        "required_capabilities": ["report-writing"],
                        "depends_on": [ids["draft"], ids["review"]],
                    },
                    {
                        "key": "rereview",
                        "title": "Review revision",
                        "description": plan,
                        "required_capabilities": ["quality-review"],
                        "depends_on": ["revise"],
                    },
                ]
            },
        )

    def research_lead(self, call: ModelCall) -> Any:
        def final() -> Any:
            text = conversation(call)
            reports = REPORT.findall(text)
            assert reports, "the writer's report should be in the results"
            report = reports[-1].strip()
            if not called(call, "save_report_to_knowledge_base"):
                title = report.splitlines()[0].removeprefix("# ")
                return tool_call(
                    "save_report_to_knowledge_base", {"title": title, "report": report}
                )
            link = re.search(r"kb://reports/\d+", results(call, "save_report_to_knowledge_base")[0])
            assert link is not None
            approved = text.rfind("VERDICT: APPROVED") > text.rfind("VERDICT: REVISE")
            note = "" if approved else "\nThe review did not approve this report."
            return f"{report}\n\nSaved to the knowledge base: {link.group()}{note}"

        return self.lead_turn(call, final)

    def market_researcher(self, call: ModelCall) -> Any:
        def work(_title: str, brief: str) -> Any:
            market, region, segment = (fact(brief, key) for key in ("Market", "Region", "Segment"))
            searches = results(call, "search_sources")
            if not searches:
                return [
                    tool_call("search_sources", {"query": f"{market} {region}"}),
                    tool_call("search_sources", {"query": f"{segment} adoption"}),
                ]
            ids = sorted({s["id"] for found in searches for s in first_json(found)["sources"]})
            if not ids:
                return "- no source found"
            read = results(call, "read_source")
            if not read:
                return [tool_call("read_source", {"source_id": sid}) for sid in ids]
            sources = [first_json(text) for text in read]
            return "\n".join(
                [f"- {s['text']} [{s['id']}]" for s in sources]
                + ["Sources:"]
                + [f"- {s['id']}: {s['title']}, {s['publisher']}, {s['date']}" for s in sources]
            )

        return self.teammate(call, work)

    def competitor_researcher(self, call: ModelCall) -> Any:
        def work(_title: str, brief: str) -> Any:
            market, region = fact(brief, "Market"), fact(brief, "Region")
            profiles = results(call, "get_competitor_profiles")
            if not profiles:
                return [
                    tool_call("get_competitor_profiles", {"market": market}),
                    tool_call(
                        "search_sources", {"query": f"competitor landscape {market} {region}"}
                    ),
                ]
            if not profiles[0].startswith("{"):
                return "- no source found"
            data = first_json(profiles[0])
            meta = first_json(results(call, "search_sources")[0])["sources"]
            return "\n".join(
                [
                    f"- {c['name']}: {c['share_pct']}% share, {c['focus']} [{data['source']}]"
                    for c in data["competitors"]
                ]
                + ["Sources:"]
                + [f"- {s['id']}: {s['title']}, {s['publisher']}, {s['date']}" for s in meta]
            )

        return self.teammate(call, work)

    def analyst(self, call: ModelCall) -> Any:
        def work(_title: str, brief: str) -> Any:
            market, region = fact(brief, "Market"), fact(brief, "Region")
            statistics = results(call, "get_market_statistics")
            if not statistics:
                return tool_call("get_market_statistics", {"market": market, "region": region})
            if not statistics[0].startswith("{"):
                return f"- no reliable data for {market} in {region}"
            figures = first_json(statistics[0])
            size, growth = figures["market_size_2025_eur_bn"], figures["cagr_2025_2030_pct"]
            projected = size * (1 + growth / 100) ** 5
            segments = ", ".join(f"{k} {v}%" for k, v in figures["segments_pct"].items())
            return (
                f"- Market size 2025: {size} bn EUR [S-1]\n- Growth: {growth}% a year to 2030 [S-1]\n"
                f"- Segments: {segments} [S-1]\n"
                f"- Adoption among mid-sized manufacturers: {figures['adoption_mid_sized_pct']}% [S-2]\n"
                f"- Projection: {size} x 1.{growth}^5 = {projected:.1f} bn EUR in 2030 (projection from [S-1])"
            )

        return self.teammate(call, work)

    def writer(self, call: ModelCall) -> Any:
        def work(title: str, brief: str) -> Any:
            if title == "Revise report":
                draft = REPORT.findall(brief)[-1]
                return draft.replace(f" {UNSOURCED}", "")
            market, region = fact(brief, "Market"), fact(brief, "Region")
            # Dependency results arrive as "- task_3 (Market analysis): <result>".
            lines = [re.sub(r"^- task_\d+ \([^)]*\): ", "", line) for line in brief.splitlines()]
            competitors = [line for line in lines if re.match(r"- Vendor \w+: ", line)]
            figures = [line for line in lines if line.startswith(STATISTIC_LINES)]
            projection = [line for line in lines if line.startswith("- Projection")]
            sources = sorted(set(SOURCE_LINE.findall(brief)))
            gap = "- no reliable data"
            outlook = " ".join(projection) or gap
            if projection:
                outlook += f" {UNSOURCED}"
            summary = (figures[:2] + competitors[:1]) or [gap]
            return "\n".join(
                [
                    f"# Market report: {market} ({region})",
                    "",
                    "## Executive summary",
                    *summary,
                    "## Market",
                    *(
                        figures
                        or ([gap] if "Market research" in brief else ["- not part of this study"])
                    ),
                    "## Competitors",
                    *(competitors or [gap]),
                    "## Outlook",
                    outlook,
                    "## Risks and data gaps",
                    "- Integration with existing machines is the top adoption barrier [S-2]"
                    if any(sid == "S-2" for sid, _ in sources)
                    else gap,
                    "## Sources",
                    *[f"- {sid}: {meta}" for sid, meta in sources],
                ]
            )

        return self.teammate(call, work)

    def reviewer(self, call: ModelCall) -> Any:
        def work(_title: str, brief: str) -> Any:
            report = REPORT.findall(brief)[-1]
            cited = sorted(set(re.findall(r"\[(S-\d+)", report)))
            if cited and not results(call, "read_source"):
                return [tool_call("read_source", {"source_id": sid}) for sid in cited]
            body = report.split("## Sources", 1)[0]
            issues = [
                line.strip()
                for line in body.splitlines()
                if re.search(r"\d", line) and not line.startswith("#")
                for sentence in [line]
                if "[S-" not in sentence or UNSOURCED in sentence
            ]
            if not issues:
                return "VERDICT: APPROVED\nEvery figure is sourced and all sections are present."
            numbered = "\n".join(
                f"{n}. '{UNSOURCED}' has no source; remove it or cite one."
                for n, _ in enumerate(issues, 1)
            )
            return f"VERDICT: REVISE\n{numbered}"

        return self.teammate(call, work)


async def scenario_market_research_report(recipe: Recipe) -> None:
    tools = recipe.tools_module
    script = MarketScript(recipe)
    network = recipe.build(script)  # the blueprint's `network:` entrypoint
    assert isinstance(network, AgentNetwork)
    goal = "Market research report: predictive maintenance software for mid-sized manufacturers in DACH."
    run = await isolated_run(network, goal)
    result = run.result
    assert result is not None, run.error
    assert result.metadata["tasks"]["completed"] == 7
    owners = [(task.title, task.owner) for task in network.task_board.list()]
    assert owners == [
        ("Market research", "market_researcher"),
        ("Competitor research", "competitor_researcher"),
        ("Market analysis", "analyst"),
        ("Draft report", "writer"),
        ("Review report", "reviewer"),
        ("Revise report", "writer"),
        ("Review revision", "reviewer"),
    ]
    reviews = [task.result or "" for task in network.task_board.list() if task.owner == "reviewer"]
    assert reviews[0].startswith("VERDICT: REVISE") and reviews[1].startswith("VERDICT: APPROVED")
    [saved] = tools.KNOWLEDGE_BASE
    assert (
        UNSOURCED not in saved["report"]
        and "[S-1]" in saved["report"]
        and "[S-4]" in saved["report"]
    )
    assert "3.7 bn EUR in 2030" in saved["report"]
    assert result.output.startswith("# Market report: predictive maintenance software (DACH)")
    assert result.output.endswith("Saved to the knowledge base: kb://reports/1")


# ---------------------------------------------------------------------- 8 content production


BANNED = ("revolutionary", "game-changing", "best-in-class", "world-leading")
CLOSING = "Results depend on the plant and its machines."


class ContentScript(Script):
    def strategist(self, call: ModelCall) -> Any:
        request_id = re.search(r"REQ-\d+", request(call))
        assert request_id is not None
        found = results(call, "get_content_request")
        if not found:
            return [
                tool_call("get_content_request", {"request_id": request_id.group()}),
                tool_call("get_brand_guidelines"),
            ]
        if not found[0].startswith("{"):
            return "REQUEST NOT FOUND"
        job = first_json(found[0])
        lines = [
            f"Request: {job['request_id']}",
            f"Channel: {job['channel']}",
            f"Audience: {job['audience']}",
            f"Goal: {job['goal']}",
            f"Key message: {job['topic']}.",
            f"Must include: {', '.join(job['keywords'])}",
            f"Product: {job['product']}",
            f"Length: {job['length']}",
            f"Call to action: {'Book a 20-minute demo' if job['goal'] == 'demo requests' else 'Sign up now'}",
        ]
        if "customer" in job:
            lines.append(f"Customer: {job['customer']}")
        return "\n".join(lines)

    def writer(self, call: ModelCall) -> Any:
        brief = request(call)
        if "VERDICT: REVISE" in brief:
            draft = brief.split("## Draft", 1)[1].strip()
            for word in BANNED:
                draft = draft.replace(f"{word} ", "").replace(f"{word.capitalize()} ", "")
            return draft
        if not results(call, "get_product_facts"):
            return tool_call("get_product_facts", {"product": fact(brief, "Product")})
        facts = first_json(results(call, "get_product_facts")[0])["facts"]
        channel, message = fact(brief, "Channel"), fact(brief, "Key message")
        action = fact(brief, "Call to action")
        if channel == "social":
            return f"Title: 50% lower maintenance costs\n{message} {facts[0]} {action}."
        if channel == "newsletter":
            customer = fact(brief, "Customer")
            return (
                f"Title: {customer} avoided a line stoppage\nWhen a bearing on the filling line "
                f"started to fail, {customer} replaced it during a planned stop. {facts[0]} {action}."
            )
        return (
            "Title: Predictive maintenance: fewer unplanned stops\n"
            f"Unplanned downtime is the most expensive hour in a plant. Our revolutionary "
            f"platform spots failing machines early. {facts[0]} {facts[1]} {facts[2]} {action}."
        )

    def editor(self, call: ModelCall) -> Any:
        text = request(call)
        if not results(call, "get_brand_guidelines"):
            return tool_call("get_brand_guidelines")
        draft = text.rsplit("[writer]: ", 1)[1].strip()
        brief_text = (
            text.split("## Brief", 1)[1].split("## Draft", 1)[0] if "## Brief" in text else text
        )
        brief = "\n".join(
            line for line in brief_text.splitlines() if re.match(r"^[A-Z][\w ]+: ", line)
        )
        banned = [word for word in BANNED if word in draft.lower()]
        feedback = "\n".join(f"{n}. Remove '{w}' (banned word)." for n, w in enumerate(banned, 1))
        verdict = "VERDICT: REVISE" if banned else "VERDICT: APPROVED"
        return f"{verdict}\nFeedback:\n{feedback}\n## Brief\n{brief}\n## Draft\n{draft}"

    def compliance_reviewer(self, call: ModelCall) -> Any:
        text = request(call)
        request_id = re.search(r"REQ-\d+", text)
        assert request_id is not None
        draft = text.split("## Draft", 1)[1].strip()
        title, _, body = draft.partition("\n")
        channel = fact(text, "Channel")
        customer = re.search(r"^Customer: (.+)$", text, re.M)
        if not results(call, "get_compliance_rules"):
            checks = [tool_call("get_compliance_rules", {"channel": channel})]
            if customer:
                checks.append(tool_call("check_reference_consent", {"customer": customer.group(1)}))
            return checks
        issues = []
        if "VERDICT: APPROVED" not in text:
            issues.append(
                {"rule": "editorial", "passage": title, "fix": "Finish the editorial review first."}
            )
        if "guaranteed" in body.lower():
            issues.append(
                {
                    "rule": "CR-1",
                    "passage": body.split(".")[0],
                    "fix": "Up to 30% less unplanned downtime in a 2025 pilot with 12 plants.",
                }
            )
        consent = results(call, "check_reference_consent")
        if consent and "NOT on file" in consent[0]:
            issues.append(
                {
                    "rule": "CR-4",
                    "passage": customer.group(1) if customer else "",
                    "fix": "Get documented consent or anonymise the customer ('a food producer').",
                }
            )
        if channel in ("blog", "newsletter") and CLOSING not in body:
            body = f"{body}\n{CLOSING}"
        saved = results(call, "save_cms_draft")
        if not issues and not saved:
            return tool_call(
                "save_cms_draft",
                {
                    "request_id": request_id.group(),
                    "title": title.removeprefix("Title: "),
                    "body": body,
                    "channel": channel,
                },
            )
        draft_id = re.search(r"D-\d+", saved[0]) if saved else None
        return self.finish(
            "compliance_reviewer",
            {
                "request_id": request_id.group(),
                "status": "changes_required" if issues else "cleared",
                "title": title.removeprefix("Title: "),
                "draft_id": draft_id.group() if draft_id else None,
                "issues": issues,
            },
        )

    def publisher(self, call: ModelCall) -> Any:
        review = first_json(request(call))
        if review["status"] != "cleared":
            issues = "; ".join(
                f"{issue['rule']}: {issue['passage']} -> {issue['fix']}"
                for issue in review["issues"]
            )
            return f"Not published: {review['request_id']} needs changes. {issues}"
        draft_id = re.search(r"D-\d+", str(review["draft_id"]))
        assert draft_id is not None
        drafts = results(call, "get_cms_draft")
        if not drafts:
            return tool_call("get_cms_draft", {"draft_id": draft_id.group()})
        published = results(call, "publish_article")
        if not published:
            if first_json(drafts[0])["status"] != "ready_for_publication":
                return f"{draft_id.group()} is not ready for publication."
            return tool_call("publish_article", {"draft_id": draft_id.group()})
        return published[0]


async def scenario_content_production(recipe: Recipe) -> None:
    tools = recipe.tools_module
    script = ContentScript(recipe)
    workflow = recipe.build(script)
    run = await isolated_run(workflow, "Produce content request REQ-201.")
    paused = run.result
    assert paused is not None, run.error
    started = [e.agent for e in run.events if type(e).__name__ == "AgentStartedEvent"]
    assert [a for a in started if a in ("writer", "editor")] == ["writer", "editor"] * 2
    # The workflow saved the cleared draft, then paused at the approval-gated publish step.
    body = tools.CMS["D-9001"]["body"]
    assert "revolutionary" not in body and body.endswith(CLOSING)
    assert paused.is_interrupted and paused.thread_id is not None
    [pending] = paused.interrupts
    assert (pending["agent"], pending["value"]["tool"]) == ("publisher", "publish_article")
    assert tools.PUBLISHED == []

    done = await workflow.resume(paused.thread_id, {"action": "approve"})
    assert done.output == "Published D-9001 at https://www.company.example/blog/d-9001."
    assert tools.CMS["D-9001"]["status"] == "published"


# ---------------------------------------------------------------------- 9 deep research

# The recipe uses the builtin web tools only: the test answers their HTTP requests from these pages.
DEEP_PAGES: dict[str, tuple[str, str]] = {
    "python": (
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "Python is a high-level programming language. It first appeared on 20 February 1991. "
        "It was designed by Guido van Rossum at CWI in the Netherlands.",
    ),
    "java": (
        "https://en.wikipedia.org/wiki/Java_(programming_language)",
        "Java is a class-based, object-oriented programming language. It first appeared on "
        "23 May 1995. It was designed by James Gosling at Sun Microsystems.",
    ),
    # The overview of Go states the year only; the designers are on the other page, which a
    # search for "designed by" finds: the first round has a gap, the second closes it.
    "go": (
        "https://go.dev/doc/",
        "Go is a statically typed, compiled programming language. It first appeared on "
        "10 November 2009.",
    ),
    "go-designers": (
        "https://en.wikipedia.org/wiki/Go_(programming_language)",
        "Go was designed at Google by Robert Griesemer, Rob Pike and Ken Thompson.",
    ),
}


def deep_search_page(query: str) -> str:
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    designers = bool(words & {"designed", "designer", "designers"})
    hits = []
    for name in ("python", "java", "go"):
        if name in words:
            url, text = DEEP_PAGES[f"{name}-designers" if designers and name == "go" else name]
            hits.append(
                f'<div class="result"><a class="result__a" href="{url}">{name.title()}</a>'
                f'<a class="result__snippet">{text[:60]}</a></div>'
            )
    body = "".join(hits) or '<div class="no-results">No results.</div>'
    return f"<html><body>{body}</body></html>"


@contextlib.contextmanager
def deep_research_web() -> Iterator[None]:
    """Answer the requests of `web_search`, `fetch_url` and `verify_quote` from `DEEP_PAGES`."""

    def fake_fetch(
        policy: Any,
        url: str,
        *,
        method: str = "GET",
        headers: Any = None,
        secret_headers: Any = (),
        data: bytes | None = None,
    ) -> Any:
        parts = urllib.parse.urlsplit(url)
        if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
            form = urllib.parse.parse_qs((data or b"").decode() or parts.query)
            page = deep_search_page(form.get("q", [""])[0])
            return mock_response(text=page, content_type="text/html", url=url)
        for known, text in DEEP_PAGES.values():
            if url == known:
                title = known.rstrip("/").rsplit("/", 1)[-1]
                body = f"<html><head><title>{title}</title></head><body><p>{text}</p></body></html>"
                return mock_response(text=body, content_type="text/html", url=url)
        return mock_response(404, text="not found", url=url)

    patched = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("awa.")
        and getattr(getattr(module, "fetch", None), "__module__", "").endswith("_internal.http")
    ]
    assert patched, "no awa module uses the HTTP client any more"
    saved = [(module, module.fetch) for module in patched]
    for module in patched:
        module.fetch = fake_fetch
    try:
        yield
    finally:
        for module, original in saved:
            module.fetch = original
        # A module imported inside the block took the fake from the patched client: reset it too.
        real = sys.modules["awa._internal.http"].fetch
        for module in list(sys.modules.values()):
            if getattr(module, "fetch", None) is fake_fetch:
                module.fetch = real


def test_a_module_imported_on_the_fake_web_gets_the_real_client_back() -> None:
    import awa._internal.http as http

    real = http.fetch
    late = ModuleType("awa_late_import")
    with deep_research_web():
        exec("from awa._internal.http import fetch", late.__dict__)
        sys.modules[late.__name__] = late
    try:
        assert late.fetch is real and http.fetch is real
    finally:
        del sys.modules[late.__name__]


@pytest.fixture
def deep_web() -> Iterator[None]:
    """The fake web."""

    with deep_research_web():
        yield


@pytest.fixture(autouse=True)
def _deep_research_offline(request: pytest.FixtureRequest) -> None:
    """The parametrized tests of the deep-research recipe run on the fake web."""

    callspec = getattr(request.node, "callspec", None)
    if callspec is not None and callspec.params.get("slug") == "deep-research":
        request.getfixturevalue("deep_web")


def shared_value(text: str) -> Any:
    """The value of a `read_shared` result."""

    return json.loads(text[text.index("{") : text.rindex("}") + 1])["value"]


def last_turn(call: ModelCall) -> AIMessage | None:
    """The agent's latest message with tool calls (its previous step)."""

    turns = [m for m in call.messages if isinstance(m, AIMessage) and m.tool_calls]
    return turns[-1] if turns else None


def answers(call: ModelCall, turn: AIMessage) -> list[tuple[dict[str, Any], str]]:
    """Arguments and result of each tool call of `turn`."""

    content = {m.tool_call_id: str(m.content) for m in call.messages if isinstance(m, ToolMessage)}
    return [(dict(item["args"]), content.get(item["id"] or "", "")) for item in turn.tool_calls]


def visible_plan(call: ModelCall) -> dict[str, Any] | None:
    """The newest plan in the conversation (a `read_shared` result); `None` once trimmed away."""

    for text in reversed(results(call, "read_shared")):
        value = shared_value(text) if "<shared_entry" in text else None
        if isinstance(value, dict) and "aspects" in value:
            return value
    return None


def slug_of(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:40] + ".md"


class DeepScript(Script):
    """Plays the lead, the researchers and the citation agent of the deep-research recipe.

    The lead decides each step from its previous step and from what it reads on the blackboard
    (plan and evidence), never from remembering earlier turns; the researcher and the citation
    agent work from their briefs and the tools' results. Flags let a test break one thing.
    """

    summaries = 0  # conversations the lead's compaction asked the model to summarize
    invent_claim = False  # the lead adds a sentence to the draft that no evidence states
    runaway = False  # the lead keeps delegating after its plan says it is done
    fabricate_quote = False  # a researcher records a quote the page does not contain

    def __call__(self, call: ModelCall) -> Any:
        if any("Context Extraction Assistant" in str(m.content) for m in call.messages):
            self.summaries += 1  # the lead's compaction: older turns become a short summary
            return "Summary: the plan is on the blackboard under the key plan, evidence under evidence/."
        return super().__call__(call)

    # ------------------------------------------------------------------ the lead

    def research_lead(self, call: ModelCall) -> Any:
        turn = last_turn(call)
        if turn is None:
            return self.plan(request(call))
        if "citation_agent" in delegated_results(call):  # cited: save the report and answer
            if visible_plan(call) is None:  # trimmed conversation: the plan is on the blackboard
                return tool_call("read_shared", {"key": "plan"})
            if not called(call, "workspace_write_file"):
                return self.save(call)
            return self.finish_with(call)
        names = [item["name"] for item in turn.tool_calls]
        if names == ["write_shared"] and turn.tool_calls[0]["args"]["key"] == "plan":
            plan = json.loads(turn.tool_calls[0]["args"]["value"])
            return self.fan_out(plan["question"], plan["aspects"])
        if "delegate_task" in names and any(
            item["args"].get("agent") == "researcher" for item in turn.tool_calls
        ):
            return [
                tool_call("read_shared", {"key": "plan"}),
                tool_call("list_shared", {"prefix": "evidence/"}),
            ]
        if names == ["read_shared", "list_shared"]:
            listing = json.loads(results(call, "list_shared")[-1])
            return [tool_call("read_shared", {"key": entry["key"]}) for entry in listing]
        if names and set(names) == {"read_shared"}:
            return self.after_reading(call)
        if names == ["write_shared"]:  # the draft is saved: the citation agent adds citations
            return tool_call(
                "delegate_task",
                {
                    "agent": "citation_agent",
                    "task": "Add citations to the draft (shared key draft) from the evidence/* entries.",
                },
            )
        raise AssertionError(f"the lead has no next step after {names}")

    def plan(self, question: str) -> Any:
        if ":" in question:
            items = re.split(r",\s*|\s+and\s+", question.split(":")[0])
            aspects = [
                {"key": f"a{n}", "aspect": f"{item}: first appearance year and designers"}
                for n, item in enumerate(items, start=1)
            ]
        else:
            aspects = [{"key": "a1", "aspect": question}]
        plan = {
            "question": question,
            "aspects": aspects,
            "round": 1,
            "extra_rounds_left": 2,
            "gaps": [],
        }
        return tool_call("write_shared", {"key": "plan", "value": json.dumps(plan)})

    def fan_out(self, question: str, aspects: list[dict[str, str]]) -> list[Any]:
        return [
            tool_call(
                "delegate_task",
                {
                    "agent": "researcher",
                    "task": f"Question: {question}\nAspect: {aspect['aspect']}\n"
                    f"Evidence key: evidence/{aspect['key']}\nStop after two searches.",
                },
            )
            for aspect in aspects
        ]

    def after_reading(self, call: ModelCall) -> Any:
        plan = visible_plan(call)
        if plan is None:  # the conversation was trimmed: the plan is on the blackboard
            return tool_call("read_shared", {"key": "plan"})
        # The evidence of the latest reading, compared with the aspects of the plan.
        reading = next(
            t
            for t in reversed([m for m in call.messages if isinstance(m, AIMessage)])
            if any(str(c["args"].get("key", "")).startswith("evidence/") for c in t.tool_calls)
        )
        claims = [c["claim"] for _, text in answers(call, reading) for c in shared_value(text)]
        missing: list[tuple[str, str]] = []
        for aspect in plan["aspects"]:
            name = aspect["aspect"].split(":")[0].strip()
            if ":" not in aspect["aspect"]:  # a question without items: any claim answers it
                missing += [] if claims else [(name, name)]
                continue
            for label, word in (
                ("first appearance year", "first appeared"),
                ("designers", "designed"),
            ):
                if not any(claim.startswith(f"{name} ") and word in claim for claim in claims):
                    missing.append((name, label))
        if self.runaway and plan["round"] < 3:
            missing = [("Python", "designers")] * 3
        if missing and (plan["extra_rounds_left"] > 0 or (self.runaway and plan["round"] < 3)):
            gaps = [
                {
                    "key": f"g{plan['round'] + 1}{n}",
                    "aspect": name if what == name else f"{name}: {what}",
                }
                for n, (name, what) in enumerate(missing, start=1)
            ]
            update = {
                **plan,
                "round": plan["round"] + 1,
                "extra_rounds_left": max(plan["extra_rounds_left"] - 1, 0),
                "gaps": [gap["aspect"] for gap in gaps],
            }
            return [
                tool_call("write_shared", {"key": "plan", "value": json.dumps(update)}),
                *self.fan_out(plan["question"], gaps),
            ]
        open_questions = "; ".join(f"{name}: {what}" for name, what in missing) or "none"
        lines = [f"# {plan['question']}", *claims]
        if self.invent_claim:
            lines.append("Python is the most widely used language of all.")
        lines.append(f"Open questions: {open_questions}")
        return tool_call("write_shared", {"key": "draft", "value": "\n".join(lines)})

    def save(self, call: ModelCall) -> Any:
        cited = first_json(delegated_results(call)["citation_agent"])
        question = visible_plan(call)
        assert question is not None
        sources = "\n".join(f"[{ref['n']}] {ref['url']}" for ref in cited["references"])
        return tool_call(
            "workspace_write_file",
            {
                "path": slug_of(question["question"]),
                "content": f"{cited['report']}\n\n## Sources\n{sources}\n",
                "overwrite": True,
            },
        )

    def finish_with(self, call: ModelCall) -> Any:
        plan = visible_plan(call)
        assert plan is not None
        cited = first_json(delegated_results(call)["citation_agent"])
        draft = next(
            a["value"] for a in reversed(called(call, "write_shared")) if a["key"] == "draft"
        )
        open_line = draft.splitlines()[-1].removeprefix("Open questions: ")
        saved = results(call, "workspace_write_file")
        return self.finish(
            "research_lead",
            {
                "question": plan["question"],
                "report": cited["report"],
                "references": cited["references"],
                "unsupported": cited["unsupported"],
                "gaps": [] if open_line == "none" else open_line.split("; "),
                "rounds": plan["round"],
                "saved_to": slug_of(plan["question"])
                if saved and "rejected" not in saved[-1].lower()
                else None,
            },
        )

    # ------------------------------------------------------------------ a researcher

    def researcher(self, call: ModelCall) -> Any:
        brief = request(call)
        aspect, key = fact(brief, "Aspect"), fact(brief, "Evidence key")
        name = aspect.split(":")[0].strip()
        wants_year, wants_designers = "year" in aspect, "designers" in aspect
        if not called(call, "web_search"):
            how = "first appeared" if wants_year else "designed by" if wants_designers else ""
            query = f"{name} programming language {how}" if how else name
            return tool_call("web_search", {"query": query})
        hits = re.findall(r"https?://\S+", tool_content(results(call, "web_search")[0]))
        if hits and not called(call, "fetch_url"):
            return tool_call("fetch_url", {"url": hits[0]})
        pages = [tool_content(page) for page in results(call, "fetch_url")]
        if not called(call, "write_shared"):
            findings, text = [], " ".join(pages)
            url = hits[0] if hits else ""
            for sentence in re.split(r"(?<=\.) ", text.split("\n\n", 1)[-1].strip()):
                year = re.search(r"first appeared on .*?(\d{4})", sentence)
                by = re.search(r" by (.+?)(?: at [A-Z].*)?\.$", sentence)
                if wants_year and year:
                    findings.append(
                        {
                            "claim": f"{name} first appeared in {year.group(1)}.",
                            "url": url,
                            "quote": sentence,
                        }
                    )
                if wants_designers and by and "designed" in sentence:
                    findings.append(
                        {
                            "claim": f"{name} was designed by {by.group(1)}.",
                            "url": url,
                            "quote": sentence,
                        }
                    )
            if self.fabricate_quote:
                findings = [
                    {**f, "quote": "It was designed by a secret committee."} for f in findings
                ]
            return tool_call("write_shared", {"key": key, "value": json.dumps(findings)})
        findings = json.loads(called(call, "write_shared")[-1]["value"])
        lines = [f"- {finding['claim']}" for finding in findings]
        for wanted, label in (
            (wants_year, "first appearance year"),
            (wants_designers, "designers"),
        ):
            word = "first appeared" if label.startswith("first") else "designed"
            if wanted and not any(word in f["claim"] for f in findings):
                lines.append(f"not found: {label}")
        if not wants_year and not wants_designers and not findings:
            lines.append(f"not found: {aspect}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ the citation agent

    def citation_agent(self, call: ModelCall) -> Any:
        listed = results(call, "list_shared")
        if not listed:
            return [
                tool_call("list_shared", {"prefix": "evidence/"}),
                tool_call("read_shared", {"key": "draft"}),
            ]
        reads = results(call, "read_shared")
        keys = [entry["key"] for entry in json.loads(listed[0])]
        if len(reads) == 1:
            return [tool_call("read_shared", {"key": k}) for k in keys]
        draft = shared_value(reads[0])
        evidence = [e for text in reads[1:] for e in shared_value(text)]
        claims = [
            line.strip()
            for line in draft.splitlines()
            if line.strip() and not line.startswith(("#", "Open questions"))
        ]
        proofs = {claim: e for claim in claims for e in evidence if e["claim"] == claim}
        checks = list(dict.fromkeys((e["url"], e["quote"]) for e in proofs.values()))
        if checks and not results(call, "verify_quote"):
            return [
                tool_call("verify_quote", {"url": url, "quote": quote}) for url, quote in checks
            ]
        verdicts = dict(zip(checks, results(call, "verify_quote"), strict=False))
        numbers: dict[tuple[str, str], int] = {}
        lines: list[str] = []
        unsupported: list[dict[str, str]] = []
        for claim in claims:
            proof = proofs.get(claim)
            if proof is None:
                unsupported.append({"claim": claim, "reason": "no evidence states this claim"})
            elif not verdicts[(proof["url"], proof["quote"])].startswith("VERIFIED"):
                unsupported.append({"claim": claim, "reason": "the quote is not on the page"})
            else:
                n = numbers.setdefault((proof["url"], proof["quote"]), len(numbers) + 1)
                lines.append(f"{claim} [{n}]")
        return self.finish(
            "citation_agent",
            {
                "report": "\n".join(lines),
                "references": [
                    {"n": n, "url": url, "quote": quote} for (url, quote), n in numbers.items()
                ],
                "unsupported": unsupported,
            },
        )


async def scenario_deep_research(recipe: Recipe) -> None:
    team = recipe.build_resumable(DeepScript(recipe))
    question = "Python, Java and Go: the year each first appeared and who designed it."
    run = await isolated_run(team, question)
    paused = run.result
    assert paused is not None and paused.is_interrupted, run.error  # saving waits for a person
    assert paused.thread_id is not None
    pending = paused.interrupts[0]["value"]
    assert pending["tool"] == "workspace_write_file"
    reports = recipe.directory / "reports"
    assert not (reports / pending["args"]["path"]).exists()  # nothing saved before the approval
    done = await team.resume(paused.thread_id, {"action": "approve"})

    # Fan out in one step: the three researchers started before the first one returned.
    kinds = [type(e).__name__ for e in run.events if "Delegation" in type(e).__name__]
    assert kinds[:3] == ["DelegationEvent"] * 3 and kinds[3] == "DelegationResultEvent"
    # The plan was saved first, and read again before the synthesis; the evidence came from
    # the researchers via the blackboard.
    names = tool_names(run.events)
    assert names[0] == "write_shared"
    assert names.index("read_shared") > names.index("delegate_task")
    assert names.count("web_search") == 4 and names.count("verify_quote") == 6
    # The bounded loop: round 2 delegated only the gap (Go's designers), then the draft.
    assert delegation_targets(run.events).count("researcher") == 4
    assert delegation_targets(run.events)[-1] == "citation_agent"

    out = done.output
    assert out.rounds == 2 and out.gaps == [] and out.unsupported == []
    assert out.saved_to == slug_of(question)
    assert "[3]" in out.report and "[6]" in out.report and "[7]" not in out.report
    assert [ref.n for ref in out.references] == [1, 2, 3, 4, 5, 6]
    assert out.references[5].url == "https://en.wikipedia.org/wiki/Go_(programming_language)"
    saved = (reports / out.saved_to).read_text(encoding="utf-8")
    assert saved.startswith("Python first appeared in 1991. [1]") and "## Sources" in saved


async def test_a_recipe_runs_in_a_temporary_copy_and_the_saved_report_stays_there(
    load_recipe: Callable[[str], Recipe], deep_web: None
) -> None:
    recipe = load_recipe("deep-research")
    assert not recipe.directory.is_relative_to(RECIPES_DIR)
    await scenario_deep_research(recipe)
    assert any(path.suffix == ".md" for path in (recipe.directory / "reports").iterdir())
    assert [p.name for p in (RECIPES_DIR / "deep-research" / "reports").iterdir()] == ["README.md"]


# ---------------------------------------------------------------------- registry of recipes


SCRIPTS: dict[str, type[Script]] = {
    "invoice-processing": InvoiceScript,
    "customer-support-desk": SupportScript,
    "lead-qualification": LeadScript,
    "employee-onboarding": OnboardingScript,
    "contract-review": ContractScript,
    "it-incident-triage": IncidentScript,
    "market-research-report": MarketScript,
    "content-production": ContentScript,
    "deep-research": DeepScript,
}
SCENARIOS: dict[str, Callable[[Recipe], Coroutine[Any, Any, None]]] = {
    "invoice-processing": scenario_invoice_processing,
    "customer-support-desk": scenario_customer_support_desk,
    "lead-qualification": scenario_lead_qualification,
    "employee-onboarding": scenario_employee_onboarding,
    "contract-review": scenario_contract_review,
    "it-incident-triage": scenario_it_incident_triage,
    "market-research-report": scenario_market_research_report,
    "content-production": scenario_content_production,
    "deep-research": scenario_deep_research,
}


async def run_cases(recipe: Recipe, factory: Callable[[], AgentLike], script: Script) -> None:
    dataset = Dataset.from_yaml(recipe.directory / "cases.yaml")
    if recipe.directory.name in TEMPLATE_RECIPES:
        assert 8 <= len(dataset.cases) <= 12
        assert sum("boundary" in case.tags for case in dataset.cases) >= 2
    else:
        assert 4 <= len(dataset.cases) <= 6
    report: EvalReport = await evaluate(
        factory,
        dataset,
        concurrency=1,  # the sample systems are module state shared by the runs
        judge_model=FunctionChatModel(script),
    )
    assert report.pass_rate == 1.0, report.to_markdown()


# ---------------------------------------------------------------------- tests


def test_every_recipe_is_covered() -> None:
    assert RECIPES == sorted(SCRIPTS) == sorted(SCENARIOS)


@pytest.mark.parametrize("slug", RECIPES)
def test_recipe_blueprint_is_valid_without_warnings(
    slug: str, load_recipe: Callable[[str], Recipe]
) -> None:
    recipe = load_recipe(slug)
    assert recipe.blueprint.check(tools=recipe.tools) == []
    assert recipe.blueprint.name == slug
    readme = (recipe.directory / "README.md").read_text(encoding="utf-8")
    assert recipe.blueprint.to_mermaid() in readme, "README diagram is out of date"
    for tool in recipe.tools:
        assert (tool.__doc__ or "").strip(), f"{tool.__name__} needs a docstring"
    # Any other blueprint in the folder is a variant of the recipe: same rules.
    for path in sorted(recipe.directory.glob("*.yaml")):
        if path.name not in ("blueprint.yaml", "cases.yaml"):
            variant = Blueprint.from_yaml(path)
            assert variant.check(tools=recipe.tools) == [], path.name
            assert variant.to_mermaid() in readme, f"README diagram of {path.name} is out of date"


@pytest.mark.parametrize("slug", RECIPES)
def test_recipe_cli_validates(
    slug: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.cli import main

    # The CLI imports the tools file as module `tools` and puts its folder on sys.path.
    monkeypatch.setitem(sys.modules, "tools", None)
    monkeypatch.setattr(sys, "path", list(sys.path))

    directory = RECIPES_DIR / slug
    has_tools = (directory / "tools.py").is_file()
    tools = ["--tools", str(directory / "tools.py")] if has_tools else []
    code = main(["validate", str(directory / "blueprint.yaml"), *tools])
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"{slug}: valid" if has_tools else out.startswith(f"{slug}: valid")


@pytest.mark.parametrize("slug", RECIPES)
async def test_recipe_end_to_end(slug: str, load_recipe: Callable[[str], Recipe]) -> None:
    await SCENARIOS[slug](load_recipe(slug))


@pytest.mark.parametrize("slug", RECIPES)
async def test_recipe_cases_pass(slug: str, load_recipe: Callable[[str], Recipe]) -> None:
    """The cases pass on the blueprint's own entrypoint, as `awa eval` runs them."""

    recipe = load_recipe(slug)
    script = SCRIPTS[slug](recipe)
    await run_cases(recipe, lambda: recipe.build(script), script)


# Instructions that keep a real model on the recipe's rules, each with the mistake it prevents.
RULE_PHRASES = [
    # The policy check blocked clean invoices on the three-way match it cannot see.
    ("invoice-processing", "policy_checker", "leave them out, and never block an invoice"),
    # Invoices above the limit were held instead of booked, so no manager was ever asked.
    ("invoice-processing", "policy_checker", "AP-4 is never a finding"),
    ("invoice-processing", "ap_coordinator", "never hold an invoice because it needs approval"),
    # The validator was asked again for "confirmed amounts" it does not return, then held a
    # clean invoice; and reasons dropped what the rule says must happen next.
    ("invoice-processing", "ap_coordinator", "A match confirms the extracted fields"),
    ("invoice-processing", "policy_checker", "what the rule says must happen next"),
    # "L1 Liability" instead of the rule id.
    ("contract-review", "legal_reviewer", "<playbook rule id, e.g. L1>"),
    ("contract-review", "compliance_reviewer", "<playbook rule id, e.g. C1>"),
    ("contract-review", "commercial_reviewer", "<playbook rule id, e.g. M1>"),
    # Two high legal findings came in no particular order.
    ("contract-review", "editor", "and then in playbook rule order (L1 before L2)"),
    # A brief that breaks a rule never got past the editor, and the rules were not checked.
    ("content-production", "compliance_reviewer", "Check the rules also when the editor's"),
    # Dozens of searches for a market the corpus does not cover.
    ("market-research-report", "market_researcher", "Search at most three times"),
    ("market-research-report", "competitor_researcher", "Search at most three times"),
]


@pytest.mark.parametrize(("slug", "agent", "phrase"), RULE_PHRASES)
def test_recipe_instructions_hold_the_rule_phrases(
    slug: str, agent: str, phrase: str, load_recipe: Callable[[str], Recipe]
) -> None:
    instructions = " ".join(load_recipe(slug).blueprint.agent(agent).instructions.split())

    assert phrase in instructions


def test_a_duplicate_check_with_a_vat_id_is_an_error_not_an_empty_result(
    load_recipe: Callable[[str], Recipe],
) -> None:
    # The ledger is searched by vendor id: a VAT id is an error, not an empty result.
    tools = load_recipe("invoice-processing").tools_module

    answer = tools.find_booked_invoices("DE100000001", "2026-0398")

    assert answer.startswith("Unknown vendor id 'DE100000001'")
    assert '"matches": [{' in tools.find_booked_invoices("V-100", "2026-0398")


def test_an_unknown_service_lists_the_catalog_ids(load_recipe: Callable[[str], Recipe]) -> None:
    tools = load_recipe("it-incident-triage").tools_module

    answer = tools.get_service("internal reporting dashboard")

    assert answer.startswith("Unknown service 'internal reporting dashboard'.")
    assert "Catalog ids: checkout, payments-api, search, reporting." in answer


def test_a_lower_case_customer_id_refunds_its_own_order(
    load_recipe: Callable[[str], Recipe],
) -> None:
    tools = load_recipe("customer-support-desk").tools_module

    answer = tools.issue_refund("c-1001", 10.0, "damaged", order_id="o-5001")

    assert not answer.startswith("Refund refused")


def test_a_badge_follows_a_desk_reserved_under_another_spelling(
    load_recipe: Callable[[str], Recipe],
) -> None:
    tools = load_recipe("employee-onboarding").tools_module
    tools.assign_desk("h-2001")

    answer = tools.issue_badge("H-2001")

    assert answer.startswith("Badge BD-2001 is ready")


@pytest.mark.parametrize("slug", RECIPES)
def test_recipe_cli_eval_passes(
    slug: str,
    load_recipe: Callable[[str], Recipe],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`awa eval` as documented in each README: the CLI imports `tools.py` itself (approval
    predicates resolve as `tools:<function>`) and runs the cases concurrently."""

    from awa.cli import main

    recipe = load_recipe(slug)
    monkeypatch.setattr(ModelRegistry, "_default", SCRIPTS[slug](recipe).registry())
    monkeypatch.setattr(sys, "path", list(sys.path))
    directory = recipe.directory
    code = main(
        [
            "eval",
            str(directory / "blueprint.yaml"),
            str(directory / "cases.yaml"),
            "--format",
            "markdown",
            *recipe.tools_args,
        ]
    )
    assert code == 0, capsys.readouterr().out
    assert "- Pass rate: **100.0%**" in capsys.readouterr().out


# ---------------------------------------------------------------------- deep research: the promises


async def run_deep(
    recipe: Recipe, script: DeepScript, question: str, action: str = "approve"
) -> tuple[Any, Any]:
    """Run the recipe until the save waits for a person, then answer that approval."""

    team = recipe.build_resumable(script)
    run = await isolated_run(team, question)
    paused = run.result
    assert paused is not None and paused.is_interrupted, run.error
    assert paused.thread_id is not None
    return run, await team.resume(paused.thread_id, {"action": action})


FACTS = "Python and Java: the year each first appeared and who designed it."


async def test_deep_research_cites_only_claims_the_evidence_states(
    load_recipe: Callable[[str], Recipe], deep_web: None
) -> None:
    class Sloppy(DeepScript):
        invent_claim = True  # the lead slips a claim into the draft that no researcher found

    recipe = load_recipe("deep-research")
    run, done = await run_deep(recipe, Sloppy(recipe), FACTS)

    out = done.output
    invented = "Python is the most widely used language of all."
    assert [(u.claim, u.reason) for u in out.unsupported] == [
        (invented, "no evidence states this claim")
    ]
    assert invented not in out.report
    cited = out.report.splitlines()
    assert len(cited) == 4 and all(re.search(r" \[\d\]$", line) for line in cited)
    assert [ref.n for ref in out.references] == [1, 2, 3, 4]
    assert tool_names(run.events).count("verify_quote") == 4  # every cited quote was checked


async def test_deep_research_refuses_a_quote_the_page_does_not_contain(
    load_recipe: Callable[[str], Recipe], deep_web: None
) -> None:
    class Inventing(DeepScript):
        fabricate_quote = True  # the researchers record a quote that is on no page

    recipe = load_recipe("deep-research")
    _, done = await run_deep(recipe, Inventing(recipe), FACTS)

    out = done.output
    assert out.references == [] and out.report == ""
    assert {u.reason for u in out.unsupported} == {"the quote is not on the page"}
    assert len(out.unsupported) == 4


async def test_deep_research_stops_at_two_extra_rounds(
    load_recipe: Callable[[str], Recipe], deep_web: None
) -> None:
    recipe = load_recipe("deep-research")
    question = "Python, Java and Zorblang: the year each first appeared and who designed it."
    run, done = await run_deep(recipe, DeepScript(recipe), question)

    out = done.output
    assert out.rounds == 3  # the first round and two extra ones
    assert out.gaps == ["Zorblang: first appearance year", "Zorblang: designers"]
    assert len(out.references) == 4 and "Zorblang" not in out.report
    # Three aspects, then the two gaps in each of the two extra rounds.
    assert delegation_targets(run.events).count("researcher") == 3 + 2 + 2
    plans = [
        json.loads(event.arguments["value"])
        for event in run.events
        if isinstance(event, ToolCallEvent)
        and event.tool_name == "write_shared"
        and event.arguments["key"] == "plan"
    ]
    assert [(p["round"], p["extra_rounds_left"]) for p in plans] == [(1, 2), (2, 1), (3, 0)]


async def test_the_team_limits_stop_a_lead_that_ignores_its_rounds(
    load_recipe: Callable[[str], Recipe], deep_web: None
) -> None:
    class Runaway(DeepScript):
        runaway = True  # asks for three more researchers after every round

    recipe = load_recipe("deep-research")
    question = "Python, Java and Go: the year each first appeared and who designed it."
    run, done = await run_deep(recipe, Runaway(recipe), question)  # 3 + 3 + 3 requests

    refusals = [
        event.content
        for event in run.events
        if isinstance(event, ToolResultEvent) and "max_calls_per_member=8" in event.content
    ]
    assert refusals and "Not started" in refusals[0]
    started = [
        event
        for event in run.events
        if isinstance(event, DelegationResultEvent) and event.target == "researcher"
    ]
    assert len(started) == 8  # the code, not the model, set the bound
    assert done.output.report  # and the run still ended with a cited report


async def test_the_plan_on_the_blackboard_survives_a_trimmed_conversation(
    load_recipe: Callable[[str], Recipe], deep_web: None
) -> None:
    recipe = load_recipe("deep-research")
    trimmed = recipe.blueprint.model_copy(deep=True)  # the same blueprint, a tiny context budget
    trimmed.agent("research_lead").compaction = CompactionBlueprint(
        max_tokens=500, keep_last_messages=8
    )
    script = DeepScript(recipe)

    team = recipe.build_resumable(script, trimmed)
    run = await isolated_run(
        team, "Python, Java and Go: the year each first appeared and who designed it."
    )
    paused = run.result
    assert paused is not None and paused.is_interrupted, run.error
    assert paused.thread_id is not None
    done = await team.resume(paused.thread_id, {"action": "approve"})

    assert script.summaries >= 1  # older turns were replaced by a summary
    out = done.output
    # The lead lost the earlier turns, read the plan and the evidence again and finished.
    assert out.question.startswith("Python, Java and Go") and out.rounds == 2
    assert len(out.references) == 6 and out.unsupported == []


def test_deep_research_instructions_keep_claims_cited_and_quotes_self_contained(
    load_recipe: Callable[[str], Recipe],
) -> None:
    """The deep-research instructions forbid uncited sentences about missing facts, keep the
    "Open questions" line out of the claims, make a researcher quote a sentence that states the
    claim on its own (pieces from separate places joined by an ellipsis), and send the researcher
    to the encyclopedia article when the keyless search is refused."""

    recipe = load_recipe("deep-research")
    lead = " ".join((recipe.blueprint.agent("research_lead").instructions or "").split())
    assert '"Open questions: ..."' in lead
    assert "never write a sentence about a missing fact" in lead
    assert "no list item that joins several facts" in lead
    citation = " ".join((recipe.blueprint.agent("citation_agent").instructions or "").split())
    assert '"Open questions: ..."' in citation and "never list it as unsupported" in citation
    researcher = " ".join((recipe.blueprint.agent("researcher").instructions or "").split())
    assert "state the claim on its own" in researcher and "names the subject" in researcher
    assert "join the pieces with an ellipsis" in researcher
    assert "do not search again" in researcher and "en.wikipedia.org/wiki/<Title>" in researcher
    cases = Dataset.from_yaml(recipe.directory / "cases.yaml").cases
    case = next(c for c in cases if c.id == "three-languages-cited")
    judge = next(s for s in case.scorers if str(s).startswith("judge:"))
    assert "Open questions" in str(judge)
