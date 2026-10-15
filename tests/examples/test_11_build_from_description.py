"""Offline test for `examples/11_build_from_description.py`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from _helpers.examples import load_example
from awa._internal.upstream.checkpoint import InMemorySaver
from awa.blueprints import AcceptanceDesign, BlueprintDesign, load
from awa.blueprints._build_loop import model_registry
from awa.blueprints._resolve import load_module_file
from awa.cli import main
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

ACCEPTANCE: dict[str, Any] = {
    "criteria": [
        "A matching invoice up to 10,000 EUR is approved for payment.",
        "An invoice above 10,000 EUR waits for a manager's approval.",
    ],
    "systems": [
        {
            "name": "ERP",
            "key": "erp",
            "description": "Purchase orders and supplier invoices.",
            "kind": "odata",
            "writes": True,
            "tables": [
                {
                    "name": "purchase_orders",
                    "description": "One purchase order.",
                    "columns": [
                        {"name": "po_number"},
                        {"name": "vendor"},
                        {"name": "amount", "type": "real"},
                    ],
                    "rows": ["PO-7, Nordwind GmbH, 1000", "PO-8, Nordwind GmbH, 12000"],
                },
                {
                    "name": "invoices",
                    "description": "One supplier invoice.",
                    "columns": [
                        {"name": "invoice_id"},
                        {"name": "po_number"},
                        {"name": "amount", "type": "real"},
                        {"name": "status"},
                    ],
                    "rows": ["INV-1, PO-7, 1005, new", "INV-2, PO-8, 12000, new"],
                },
            ],
        }
    ],
    "rules": [
        {
            "name": "within_tolerance",
            "description": "Whether an invoice amount is within 1 % of the purchase order.",
            "arguments": [{"name": "invoice_amount"}, {"name": "po_amount"}],
            "expression": "abs(invoice_amount - po_amount) <= po_amount * 0.01",
            "examples": [
                {"arguments": '{"invoice_amount": 1005, "po_amount": 1000}', "result": "true"},
                {"arguments": '{"invoice_amount": 1100, "po_amount": 1000}', "result": "false"},
            ],
        }
    ],
    "cases": [
        {
            "id": "matching-invoice",
            "input": "Check invoice INV-1.",
            "expectations": [
                {"kind": "tool_called", "value": "within_tolerance"},
                {"kind": "contains", "value": "approved"},
            ],
        },
        {
            "id": "large-invoice",
            "input": "Check invoice INV-2.",
            "expectations": [{"kind": "approval", "value": "erp_execute"}],
        },
    ],
}


def blueprint(tools: list[str]) -> dict[str, Any]:
    return {
        "name": "invoice_desk",
        "description": "Checks supplier invoices against purchase orders.",
        "entrypoint": "agent",
        "agents": [
            {
                "name": "clerk",
                "description": "Checks one invoice and approves or holds it.",
                "instructions": "Read the invoice and its purchase order in the ERP tables.",
                "tools": tools,
            }
        ],
    }


def respond(call: ModelCall) -> Any:
    system = str(call.messages[0].content) if isinstance(call.messages[0], SystemMessage) else ""
    if system.startswith("You write the acceptance tests"):
        return structured_output(AcceptanceDesign, ACCEPTANCE)
    if system.startswith("You design multi-agent systems"):
        repair = "## Failing eval cases" in str(call.last_message.content)
        tools = [
            "connector:erp.query",
            "connector:erp.execute",
            *(["within_tolerance"] if repair else []),
        ]
        return structured_output(BlueprintDesign, blueprint(tools))
    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
    invoice = "INV-2" if "INV-2" in request else "INV-1"
    if not results:
        sql = "SELECT * FROM invoices JOIN purchase_orders USING (po_number) WHERE invoice_id = ?"
        return tool_call("erp_query", {"query": sql, "parameters": [invoice]})
    if invoice == "INV-2":
        update = "UPDATE invoices SET status = 'approved' WHERE invoice_id = 'INV-2'"
        return tool_call("erp_execute", {"statement": update})
    if "within_tolerance" in call.tool_names and len(results) == 1:
        return tool_call("within_tolerance", {"invoice_amount": 1005, "po_amount": 1000})
    return "INV-1 matches PO-7 within 1 % and is approved for payment."


async def test_build_repairs_the_design_and_writes_a_working_project(tmp_path: Path) -> None:
    out = tmp_path / "invoice_desk"
    report = await load_example("11_build_from_description").build_invoice_desk(
        out, model=FunctionChatModel(respond), environment=None
    )

    assert report.passed and report.rounds == 2 and report.stopped == "target_reached"
    assert "within_tolerance" in report.design.blueprint.agent("clerk").tools
    assert [a.tool for a in report.design.blueprint.approvals] == ["connector:erp.execute"]
    [step] = report.design.connections
    assert (step.status, step.connector, step.env_vars, step.login) == (
        "map",
        "odata",
        ("ERP_URL",),
        ("ERP_TOKEN",),
    )
    assert step.switch["connector:erp.execute"] == ("connector:erp.create", "connector:erp.update")
    with sqlite3.connect(out / "data" / "erp.db") as db:
        assert db.execute("SELECT amount FROM purchase_orders WHERE po_number = 'PO-8'").fetchone()
    assert main(["validate", str(out / "blueprint.yaml"), "--tools", str(out / "tools.py")]) == 0
    # The written project runs from its folder: workspace paths are relative to blueprint.yaml.
    desk = load(
        out / "blueprint.yaml",
        tools=[load_module_file(out / "tools.py").within_tolerance],
        model_registry=model_registry(FunctionChatModel(respond)),
        checkpointer=InMemorySaver(),
    )
    assert "approved" in str((await desk.run("Check invoice INV-1.")).output)
    assert "2 of 2 cases pass" in (out / "README.md").read_text(encoding="utf-8")
    assert "TODO" not in (out / "tools.py").read_text(encoding="utf-8")
    assert "within_tolerance" in (out / "tests" / "test_tools.py").read_text(encoding="utf-8")
    assert json.loads(report.eval_report.to_json())["pass_rate"] == 1.0
