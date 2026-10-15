"""The inline SVG flow diagram and the plain language of the web app."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from awa.blueprints import Blueprint
from awa.ui._diagram import blueprint_svg
from awa.ui._plain import (
    condition_words,
    date_text,
    describe_call,
    first_sentence,
    format_value,
    humanize,
    money,
    tool_label,
    view_of,
)

SVG = "{http://www.w3.org/2000/svg}"


def render(data: dict[str, Any]) -> ET.Element:
    return ET.fromstring(blueprint_svg(Blueprint.from_dict(data)))


def boxes(svg: ET.Element) -> dict[str, tuple[float, float]]:
    """Node name to the centre of its box."""

    found = {}
    for group in svg.iter(f"{SVG}g"):
        rect = group.find(f"{SVG}rect")
        name = group.find(f"{SVG}text")
        assert rect is not None and name is not None and name.text is not None
        x, y = float(rect.get("x", 0)), float(rect.get("y", 0))
        found[name.text] = (
            x + float(rect.get("width", 0)) / 2,
            y + float(rect.get("height", 0)) / 2,
        )
    return found


def classes(svg: ET.Element) -> list[str]:
    return [
        path.get("class", "")
        for path in svg.iter(f"{SVG}path")
        if "flow-edge" in path.get("class", "")
    ]


def agents(*names: str, tools: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    return [{"name": name, "tools": (tools or {}).get(name, [])} for name in names]


def test_a_team_puts_the_orchestrator_above_its_members() -> None:
    svg = render(
        {
            "name": "desk",
            "agents": agents("lead", "intake", "payer", tools={"payer": ["pay"]}),
            "team": {
                "orchestrator": "lead",
                "members": ["intake", "payer"],
                "peers": {"intake": ["payer"]},
                "handoffs": {"lead": ["payer"]},
            },
            "approvals": [{"tool": "pay"}],
        }
    )

    centres = boxes(svg)
    assert centres["lead"][1] < centres["intake"][1] == centres["payer"][1]
    assert svg.get("role") == "img"
    assert svg.findtext(f"{SVG}title") == "Flow of desk"
    assert "approval: pay" in "".join(svg.itertext())
    kinds = classes(svg)
    assert kinds.count("flow-edge flow-solid") == 2
    assert "flow-edge flow-both" in kinds  # peers are symmetric by default
    assert "flow-edge flow-thick" in kinds
    handoff = next(p for p in svg.iter(f"{SVG}path") if "flow-thick" in p.get("class", ""))
    assert " Q" in handoff.get("d", "")  # bent away from the delegation arrow it parallels


def test_a_workflow_runs_left_to_right_with_parallel_steps_side_by_side() -> None:
    svg = render(
        {
            "name": "pipeline",
            "agents": agents("read", "check", "price", "write"),
            "workflow": {"sequential": ["read", {"parallel": ["check", "price"]}, "write"]},
        }
    )

    centres = boxes(svg)
    assert centres["input"][0] < centres["read"][0] < centres["check"][0] < centres["write"][0]
    assert centres["check"][0] == centres["price"][0]
    assert centres["check"][1] != centres["price"][1]
    assert centres["write"][0] < centres["output"][0]


def test_a_loop_is_drawn_as_a_labelled_dotted_back_edge() -> None:
    svg = render(
        {
            "name": "drafting",
            "agents": agents("writer", "critic"),
            "workflow": {
                "loop": {"sequential": ["writer", "critic"]},
                "until_contains": "APPROVED",
                "max_iterations": 3,
            },
        }
    )

    centres = boxes(svg)
    assert centres["writer"][0] < centres["critic"][0]
    assert "flow-edge flow-dotted" in classes(svg)
    assert "until 'APPROVED', max 3" in "".join(svg.itertext())


def test_a_network_lead_plans_for_teammates_who_talk_to_each_other() -> None:
    svg = render(
        {
            "name": "research",
            "agents": agents("lead", "a", "b"),
            "network": {"lead": "lead", "teammates": ["a", "b"], "peers": "all"},
        }
    )

    assert classes(svg).count("flow-edge flow-both") == 1
    assert "tasks" in "".join(svg.itertext())


def test_labels_are_escaped() -> None:
    text = blueprint_svg(
        Blueprint.from_dict(
            {
                "name": "x",
                "agents": [{"name": "clerk", "tools": ["<script>&"]}],
                "approvals": [{"tool": "<script>&"}],
            }
        )
    )

    assert "<script>" not in text
    assert "&lt;script&gt;&amp;" in text
    assert re.search(r'viewBox="0 0 \d+ \d+"', text)


FIELDS = {
    "invoice_id": {"title": "Invoice number", "description": "The number on the invoice."},
    "amount_eur": {"title": "Amount (EUR)", "description": "Amount in EUR."},
}


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("invoice_id", "Invoice id"),
        ("total_eur", "Total (EUR)"),
        ("orderDate", "Order Date"),
        ("to", "To"),
        ("v2_check", "V 2 check"),
    ],
)
def test_names_become_titles(name: str, title: str) -> None:
    assert humanize(name) == title


def test_a_call_becomes_one_sentence_with_the_arguments_in_words() -> None:
    args = {"invoice_id": "INV-2042", "amount_eur": 7450, "query": "SELECT * FROM invoices WHERE x"}

    sentence = describe_call("Look up an invoice in the ledger", args, FIELDS)

    # the SQL stays in the details
    assert sentence == (
        "Look up an invoice in the ledger (Invoice number: INV-2042, Amount (EUR): 7,450.00 EUR)"
    )
    assert describe_call("Look up an invoice", args, FIELDS, listed=False) == "Look up an invoice"


def test_a_label_with_placeholders_is_filled_instead_of_listing_the_arguments() -> None:
    label = "Look up invoice {invoice_id} in the ledger"

    sentence = describe_call(label, {"invoice_id": "INV-2042"}, FIELDS)

    assert sentence == "Look up invoice INV-2042 in the ledger"


def test_a_tool_is_labelled_by_metadata_then_the_connector_then_its_description() -> None:
    assert tool_label("x", "Whatever.", {"label": "Check the stock"}, {}) == "Check the stock"
    meta = {"awa_connector": "ledger.query"}
    assert tool_label(
        "ledger_query", "Run a read-only SQL query.", meta, {"ledger": "Invoice ledger"}
    ) == ("Look something up in the Invoice ledger")
    assert tool_label("lookup", "Look up an order. More text.", {}, {}) == "Look up an order"
    assert tool_label("finance_approval", "", {}, {}) == "Finance approval"


def test_the_first_sentence_is_cut_without_its_full_stop() -> None:
    assert first_sentence("Book a slot.\n\nArgs:\n    slot: x") == "Book a slot"
    assert first_sentence("") == ""


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("total_eur", 7450, "7,450.00 EUR"),
        ("amount", "1250.5", "1,250.50"),
        ("due", "2026-10-05", "5 Oct 2026"),
        ("sent", "2026-10-05T14:30:00Z", "5 Oct 2026, 14:30"),
        ("paid", True, "Yes"),
        ("note", None, "(empty)"),
        ("ids", ["A-1", "A-2"], "A-1, A-2"),
        ("invoice_id", "INV-2042", "INV-2042"),
        ("count", 5000, "5000"),
    ],
)
def test_values_are_written_the_way_people_read_them(
    name: str, value: object, expected: str
) -> None:
    assert format_value(name, value) == expected


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("betrag", "4.250,00", "4,250.00"),
        ("total_eur", "1.234,50", "1,234.50 EUR"),
        ("amount", "1,234.50", "1,234.50"),
        ("amount", "12,5", "12.50"),
        ("amount", "4.25", "4.25"),
        ("amount", "1.234.567,89", "1,234,567.89"),
        ("amount", "1,234,567", "1,234,567.00"),
        ("amount", "1 234,50", "1,234.50"),
        ("amount", "-4.250,00", "-4,250.00"),
        ("amount", "0,500", "0.50"),
        ("amount", "1,234", "1,234"),
        ("amount", "1.234", "1.234"),
        ("amount", "1.2,50", "1.2,50"),
        ("amount", "12,34.5,6", "12,34.5,6"),
        ("amount", "inf", "inf"),
    ],
)
def test_amounts_written_in_german_or_us_style_are_read_right_or_shown_as_written(
    name: str, value: str, expected: str
) -> None:
    assert format_value(name, value) == expected


def test_a_currency_is_found_in_a_sibling_field_or_the_description() -> None:
    assert format_value("amount", 90, siblings={"currency": "usd"}) == "90.00 USD"
    assert format_value("price", 12, description="Price in GBP.") == "12.00 GBP"
    assert money("abc") is None and money(True) is None
    assert date_text("2026-02-31") is None


@pytest.mark.parametrize(
    ("expression", "words"),
    [
        ("amount >= 5000", "the amount is 5,000 or more"),
        ("total_eur >= 5000", "the total is 5,000 EUR or more"),
        (
            "amount > 1000 and country != 'DE'",
            "the amount is more than 1,000 and the country is not DE",
        ),
        (
            "country not in ['DE', 'AT'] or amount > 1000",
            "the country is not one of DE, AT or the amount is more than 1,000",
        ),
        ("not endswith(to, '@example.com')", "the to does not end with @example.com"),
        ("empty(reason)", "the reason is empty"),
        ("not (amount < 100)", "the amount is 100 or more"),
        ("round(amount) ** 2 > 3", 'the rule "round(amount) ** 2 > 3" applies'),
        ("amount >=", 'the rule "amount >=" applies'),
    ],
)
def test_an_approval_rule_is_explained_in_words(expression: str, words: str) -> None:
    assert condition_words(expression) == words


def test_structured_output_becomes_facts_and_tables() -> None:
    view = view_of(
        {
            "invoice_id": "INV-1",
            "total_eur": 5000,
            "paid": False,
            "lines": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 3}],
        }
    )

    assert view["type"] == "facts"
    items = {item["title"]: item["value"] for item in view["items"]}
    assert items["Total (EUR)"] == "5,000.00 EUR" and items["Paid"] == "No"
    assert items["Lines"] == {
        "type": "table",
        "columns": ["Sku", "Qty"],
        "rows": [["A", "2"], ["B", "3"]],
        "more": 0,
    }
    assert view_of(["a", "b"]) == {"type": "list", "items": ["a", "b"]}
    assert view_of("plain") == "plain"


def test_a_long_list_is_cut_and_deep_nesting_stays_text() -> None:
    rows = [{"n": i} for i in range(80)]

    table = view_of(rows)

    assert len(table["rows"]) == 50 and table["more"] == 30
    deep = view_of({"a": {"b": {"c": {"d": 1}}}})
    assert isinstance(deep["items"][0]["value"]["items"][0]["value"]["items"][0]["value"], str)
