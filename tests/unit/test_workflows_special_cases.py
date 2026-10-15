"""Special cases: rows tried in order, unknown goes to `when_unclear`, and boundary probes of the table."""

from __future__ import annotations

import math
import time
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from awa.workflows import CaseAction, SpecialCases, boundary_cases, case_notes


def amount(op: str, value: Any, then: str = "ask", **extra: Any) -> dict[str, Any]:
    return {"if": [{"field": "amount_eur", "op": op, "value": value}], "then": then, **extra}


INVOICE = {
    "cases": [
        amount(">=", 5000),
        {
            "if": [
                {"field": "amount_eur", "op": "<", "value": 5000},
                {"field": "supplier", "op": "one of", "value": ["Acme Tools", "Bolt GmbH"]},
            ],
            "then": "approve",
        },
    ],
    "else": "ask",
}


def table(data: Any = None) -> SpecialCases:
    return SpecialCases.model_validate(INVOICE if data is None else data)


# First match wins, else, unclear ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "source", "row", "then"),
    [
        ({"amount_eur": 5000, "supplier": "Acme Tools"}, "row", 1, "ask"),
        ({"amount_eur": 4999.99, "supplier": "Acme Tools"}, "row", 2, "approve"),
        ({"amount_eur": 4999.99, "supplier": "Other Ltd"}, "else", None, "ask"),
        ({"supplier": "Acme Tools"}, "unclear", 1, "ask"),
        ({"amount_eur": 100, "supplier": None}, "unclear", 2, "ask"),
        ({"amount_eur": "4.999", "supplier": "Acme Tools"}, "unclear", 1, "ask"),
        ({"amount_eur": "4999", "supplier": "Acme Tools"}, "row", 2, "approve"),
    ],
)
def test_first_true_row_decides_unknown_stops_the_table(
    values: dict[str, Any], source: str, row: int | None, then: str
) -> None:
    outcome = table().decide(values)
    assert (outcome.source, outcome.row, outcome.then) == (source, row, then)


def test_unknown_does_not_fall_through_even_when_a_later_row_would_hold() -> None:
    cases = table({"cases": [amount(">=", 5000, "reject"), amount("<", 10**9, "approve")]})
    outcome = cases.decide({"amount_eur": float("nan")})
    assert outcome.source == "unclear" and outcome.row == 1 and outcome.then == "ask"
    assert "not a finite number" in outcome.reason


def test_when_unclear_reject_applies() -> None:
    cases = table({**INVOICE, "when_unclear": "reject"})
    assert cases.decide({}).then == "reject"


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -math.inf,
        Decimal("NaN"),
        [5000],
        {"value": 5000},
        True,
        False,
        "five thousand",
        "",
        "5.000",
        "5,000",
        None,
    ],
    ids=repr,
)
def test_values_that_are_not_one_clear_number_are_unclear(value: Any) -> None:
    outcome = table().decide({"amount_eur": value, "supplier": "Acme Tools"})
    assert outcome.source == "unclear", outcome
    assert outcome.row == 1 and outcome.reason


@pytest.mark.parametrize("value", [[1], {"a": 1}, float("nan")], ids=repr)
def test_text_fields_with_a_list_object_or_nan_are_unclear(value: Any) -> None:
    outcome = table().decide({"amount_eur": 10, "supplier": value})
    assert (outcome.source, outcome.row) == ("unclear", 2)


def test_decimal_values_and_numeric_text_are_read_as_numbers() -> None:
    assert table().decide({"amount_eur": Decimal("5000.00"), "supplier": "x"}).row == 1
    assert table().decide({"amount_eur": "1,250.50", "supplier": "Acme Tools"}).row == 2
    comma = table({**INVOICE, "decimal": ","})
    assert comma.decide({"amount_eur": "4999,99", "supplier": "Acme Tools"}).row == 2


def test_text_compares_exactly_so_other_case_does_not_match() -> None:
    outcome = table().decide({"amount_eur": 10, "supplier": "ACME TOOLS"})
    assert outcome.source == "else"


def test_if_expression_rows_use_the_shared_language_in_strict_mode() -> None:
    cases = table(
        {
            "cases": [
                {"if": "supplier in ['Blocked Ltd']", "then": "reject", "message": "Blocked."},
                {"if": "amount_eur >= 20000 or len(lines) > 50", "then": "ask", "by": "board"},
                {"if": "status == 'paid'", "then": "go: archive"},
            ]
        }
    )
    assert cases.decide({"supplier": "Blocked Ltd"}).then == "reject"
    board = cases.decide({"supplier": "A", "amount_eur": 30000})
    assert (board.row, board.action.by) == (2, "board")
    assert cases.decide({"supplier": "A", "amount_eur": 10, "lines": [1] * 60}).row == 2
    paid = cases.decide({"supplier": "A", "amount_eur": 10, "lines": [], "status": "paid"})
    assert (paid.then, paid.action.to) == ("go", "archive")
    assert cases.decide({"supplier": "A", "amount_eur": "n/a"}).source == "unclear"


def test_a_row_that_does_not_give_true_or_false_is_unclear() -> None:
    cases = table({"cases": [{"if": "amount_eur + 1", "then": "approve"}]})
    holds, reason = cases.check_row(1, {"amount_eur": 5})
    assert holds is None and "true or false" in reason


def test_false_before_an_unknown_value_is_still_false() -> None:
    """`amount < 5000 and supplier ...` is false for 6000 whatever the supplier is."""

    cases = table({"cases": [INVOICE["cases"][1]], "else": "reject"})
    assert cases.decide({"amount_eur": 6000}).source == "else"


def test_check_row_counts_from_one() -> None:
    with pytest.raises(IndexError, match="2 row"):
        table().check_row(3, {})


# Validation: what the table refuses, with the fix ----------------------------------------------


@pytest.mark.parametrize("when", ["approve", "go: archive", {"then": "approve"}])
def test_when_unclear_approve_or_go_is_refused(when: Any) -> None:
    with pytest.raises(ValidationError, match="when_unclear cannot be .*use 'ask'"):
        table({**INVOICE, "when_unclear": when})


def test_else_is_required_in_the_model_and_defaults_to_ask() -> None:
    cases = table({"cases": INVOICE["cases"]})
    assert cases.else_ == CaseAction(then="ask")
    assert cases.when_unclear == CaseAction(then="ask")
    assert cases.warnings == []


def test_else_approve_is_allowed_with_a_warning() -> None:
    cases = table({**INVOICE, "else": "approve"})
    assert cases.decide({"amount_eur": 1, "supplier": "x"}).then == "approve"
    assert len(cases.warnings) == 1 and "else: ask" in cases.warnings[0]


def test_a_bare_list_is_the_short_form_of_cases() -> None:
    assert table(INVOICE["cases"]).cases == table().cases


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (amount("gt", 5), "op 'gt' is not known; use one of: =, !=, <"),
        (amount(">=", "5000"), "needs a number .* without quotes"),
        (amount(">=", "high"), "needs a number"),
        (amount(">=", True), "needs a number"),
        (amount(">=", float("nan")), "not a finite number"),
        (amount("=", [1, 2]), "for a list use 'one of'"),
        (amount("one of", 5), "needs a list"),
        (amount("one of", []), "needs a list"),
        (amount("=", "5.000"), "two different numbers"),
        (amount("=", 1, then="go"), "'go' needs the step"),
        (amount("=", 1, then="ask", to="archive"), "'to' belongs to 'go' only"),
        (amount("=", 1, then="reject", by="finance"), "'by' .* 'ask' only"),
        (amount("=", 1, then="pay"), "then"),
        (amount("=", 1, then="go: 9lives"), "not a step name"),
        ({"if": [{"field": "amount eur", "op": "=", "value": 1}], "then": "ask"}, "usable name"),
        ({"if": [{"field": "null", "op": "=", "value": 1}], "then": "ask"}, "usable name"),
        ({"if": [], "then": "ask"}, "no conditions"),
        ({"if": "  ", "then": "ask"}, "'if' is empty"),
        ({"if": "amount_eur >=", "then": "ask"}, "row 1: the condition cannot be read"),
        ({"if": "open('x')", "then": "ask"}, "row 1: .*unknown function"),
        ({"then": "ask"}, "if"),
    ],
)
def test_invalid_rows_are_refused_with_the_fix(row: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        table({"cases": [row]})


def test_operator_aliases_are_read() -> None:
    cases = table({"cases": [amount("==", 5), amount("IN", [6, 7]), amount("not  in", [1])]})
    assert [c.op for row in cases.cases for c in row.if_ if not isinstance(c, str)] == [
        "=",
        "one of",
        "not one of",
    ]


# Plain words --------------------------------------------------------------------------------------


def test_describe_reads_like_the_rule_table() -> None:
    cases = table(
        {
            "cases": [
                amount(">=", 5000, by="finance"),
                {**INVOICE["cases"][1], "message": "Known supplier."},
                {"if": "status == 'paid'", "then": "go", "to": "archive"},
                {"if": "len(lines) > 50 or urgent == true", "then": "ask"},
            ],
            "when_unclear": "reject",
        }
    )
    assert cases.describe() == [
        "If amount is 5,000 EUR or more: a person decides (finance)",
        'If amount is less than 5,000 EUR and supplier is one of "Acme Tools" or "Bolt GmbH": '
        "approved without asking a person. Known supplier.",
        'If status is "paid": continue at step archive',
        "If len(lines) > 50 or urgent == true: a person decides",
        "Otherwise: a person decides",
        "When a value is missing or unclear: rejected",
    ]


def test_describe_uses_the_decimal_setting() -> None:
    cases = table({"cases": [amount(">", 1234.5)], "decimal": ","})
    assert cases.describe()[0] == "If amount is more than 1.234,5 EUR: a person decides"


def test_outcome_describe_names_the_row_and_the_reason() -> None:
    assert table().decide({}).describe() == (
        "unclear at row 1 (amount_eur has no value): a person decides"
    )
    assert table().decide({"amount_eur": 1, "supplier": "x"}).describe() == (
        "else: a person decides"
    )


# Round trip and the expression text -----------------------------------------------------------


def test_round_trip_keeps_if_and_else_spelled_as_in_the_file() -> None:
    cases = table({**INVOICE, "else": {"then": "go", "to": "archive"}, "decimal": ","})
    dumped = cases.model_dump(mode="json", exclude_defaults=True)
    assert "if" in dumped["cases"][0] and dumped["else"] == "go: archive"
    assert SpecialCases.model_validate(dumped) == cases


def test_every_row_compiles_to_one_expression() -> None:
    assert [row.expression() for row in table().cases] == [
        "amount_eur >= 5000",
        "amount_eur < 5000 and supplier in ['Acme Tools', 'Bolt GmbH']",
    ]
    assert table().fields() == ["amount_eur", "supplier"]


# Speed -------------------------------------------------------------------------------------------


def test_a_thousand_rows_decide_in_under_50_ms() -> None:
    """Timing check, not a unit test of behavior: lenient, best of five runs, measured in
    CPU time of this process so other processes on a busy machine do not count."""

    rows = [amount("=", n, "approve") for n in range(1000)]
    cases = table({"cases": rows})
    values = {"amount_eur": -1}  # no row holds: all 1,000 rows are tried, then else
    best = math.inf
    for _ in range(5):
        started = time.process_time()
        outcome = cases.decide(values)
        best = min(best, time.process_time() - started)
    assert outcome.source == "else"
    assert best < 0.050, f"{best * 1000:.1f} ms for 1,000 rows"


def table_of(rows: list[dict[str, Any]], **settings: Any) -> SpecialCases:
    return SpecialCases.model_validate({"cases": rows, **settings})


BOUNDARY_TABLE = table_of(
    [
        amount(">=", 5000),
        {
            "if": [
                {"field": "amount_eur", "op": "<", "value": 5000},
                {"field": "supplier", "op": "one of", "value": ["Acme Tools", "Bolt GmbH"]},
            ],
            "then": "approve",
        },
    ],
    **{"else": "ask"},
)

INVOICE_SNAPSHOT = """\
1 | amount_eur just below 5,000 EUR | {'amount_eur': 4999, 'supplier': 'other value'} | else: a person decides
1 | amount_eur exactly 5,000 EUR | {'amount_eur': 5000, 'supplier': 'other value'} | row 1: a person decides
1 | amount_eur just above 5,000 EUR | {'amount_eur': 5001, 'supplier': 'other value'} | row 1: a person decides
1 | amount_eur missing | {'supplier': 'other value'} | unclear at row 1 (amount_eur has no value): a person decides
1 | amount_eur two-way number 5.000 | {'amount_eur': '5.000', 'supplier': 'other value'} | unclear at row 1 (the number '5.000' in amount_eur can be read two ways (decimal or thousands)): a person decides
2 | amount_eur just below 5,000 EUR | {'amount_eur': 4999, 'supplier': 'Acme Tools'} | row 2: approved without asking a person
2 | amount_eur exactly 5,000 EUR | {'amount_eur': 5000, 'supplier': 'Acme Tools'} | row 1: a person decides
2 | amount_eur just above 5,000 EUR | {'amount_eur': 5001, 'supplier': 'Acme Tools'} | row 1: a person decides
2 | supplier not in the list | {'amount_eur': 4999, 'supplier': 'other value'} | else: a person decides
2 | supplier "Acme Tools" in other case | {'amount_eur': 4999, 'supplier': 'aCME tOOLS'} | else: a person decides
2 | amount_eur missing | {'supplier': 'Acme Tools'} | unclear at row 1 (amount_eur has no value): a person decides
2 | amount_eur two-way number 5.000 | {'amount_eur': '5.000', 'supplier': 'Acme Tools'} | unclear at row 1 (the number '5.000' in amount_eur can be read two ways (decimal or thousands)): a person decides
2 | supplier missing | {'amount_eur': 4999} | unclear at row 2 (supplier has no value): a person decides
2 | supplier two-way number 5.000 | {'amount_eur': 4999, 'supplier': '5.000'} | else: a person decides
"""


def test_invoice_boundary_table_snapshot() -> None:
    lines = [
        f"{c.row} | {c.probe} | {c.values} | {c.expected.describe()}"
        for c in boundary_cases(BOUNDARY_TABLE)
    ]
    assert "\n".join(lines) + "\n" == INVOICE_SNAPSHOT


def test_every_probe_carries_what_the_table_decides() -> None:
    for case in boundary_cases(BOUNDARY_TABLE):
        assert BOUNDARY_TABLE.decide(case.values) == case.expected


def test_decimal_limits_are_probed_in_their_own_steps() -> None:
    probes = {
        c.probe: c.values.get("amount_eur")
        for c in boundary_cases(table_of([amount("<", 4999.995)]))
    }
    assert probes["amount_eur just below 4,999.995 EUR"] == pytest.approx(4999.994)
    assert probes["amount_eur just above 4,999.995 EUR"] == pytest.approx(4999.996)
    cents = {
        c.probe: c.values.get("amount_eur") for c in boundary_cases(table_of([amount(">=", 50.5)]))
    }
    assert cents["amount_eur just below 50.5 EUR"] == pytest.approx(50.49)


def test_a_field_compared_with_a_decimal_anywhere_is_probed_in_cents() -> None:
    cases = boundary_cases(table_of([amount(">=", 5000), amount("<", 10.25, "approve")]))
    below = next(c for c in cases if c.probe == "amount_eur just below 5,000 EUR")
    assert below.values["amount_eur"] == pytest.approx(4999.99)


def test_the_two_way_number_follows_the_decimal_setting() -> None:
    comma = boundary_cases(table_of([amount(">=", 20000)], decimal=","))
    probe = next(c for c in comma if "two-way" in c.probe)
    assert probe.values["amount_eur"] == "20,000" and probe.expected.source == "unclear"


def test_text_probes_exact_other_and_other_case() -> None:
    rows = [{"if": [{"field": "status", "op": "=", "value": "paid"}], "then": "go: archive"}]
    probes = {
        c.probe: (c.values.get("status"), c.expected.source) for c in boundary_cases(table_of(rows))
    }
    assert probes == {
        'status exactly "paid"': ("paid", "row"),
        "status another text": ("other value", "else"),
        'status "paid" in other case': ("PAID", "else"),
        "status missing": (None, "unclear"),
        "status two-way number 5.000": ("5.000", "else"),
    }


def test_true_false_and_number_lists_are_probed() -> None:
    rows = [
        {"if": [{"field": "urgent", "op": "=", "value": True}], "then": "ask"},
        {
            "if": [{"field": "country_code", "op": "not one of", "value": [49, 43]}],
            "then": "reject",
        },
    ]
    probes = {(c.row, c.probe): c.values for c in boundary_cases(table_of(rows))}
    assert probes[(1, "urgent true")]["urgent"] is True
    assert probes[(1, "urgent false")]["urgent"] is False
    assert probes[(2, "country_code in the list")]["country_code"] == 49
    assert probes[(2, "country_code not in the list")]["country_code"] == 50


def test_if_text_rows_are_probed_like_condition_rows() -> None:
    written = table_of([{"if": "amount_eur >= 5000 and supplier in ['Acme Tools']", "then": "ask"}])
    listed = table_of(
        [
            {
                "if": [
                    {"field": "amount_eur", "op": ">=", "value": 5000},
                    {"field": "supplier", "op": "one of", "value": ["Acme Tools"]},
                ],
                "then": "ask",
            }
        ]
    )
    assert [(c.probe, c.values) for c in boundary_cases(written)] == [
        (c.probe, c.values) for c in boundary_cases(listed)
    ]


def test_complex_if_rows_get_missing_and_two_way_probes_for_every_name() -> None:
    cases = boundary_cases(
        table_of([{"if": "len(lines) > 3 or contains(note, 'urgent')", "then": "ask"}])
    )
    assert {c.probe for c in cases} >= {"lines missing", "note missing"}
    assert all(c.expected.source == "unclear" for c in cases if c.probe.endswith("missing"))


# Notes ---------------------------------------------------------------------------------------


def test_a_row_covered_by_one_earlier_row_is_shadowed() -> None:
    notes = case_notes(table_of([amount(">=", 5000), amount(">=", 20000, "reject")]))
    assert [(n.kind, n.row) for n in notes] == [("shadowed", 2), ("gap", None)]
    assert notes[0].text == "Row 2 never applies: row 1 already decides every amount it covers."


def test_a_row_covered_only_by_several_earlier_rows_together_is_shadowed() -> None:
    rows = [amount("<", 100), amount(">=", 100), amount("=", 7, "approve")]
    notes = [n for n in case_notes(table_of(rows)) if n.kind == "shadowed"]
    assert [n.row for n in notes] == [3] and "rows 1 and 2" in notes[0].text


@pytest.mark.parametrize(
    "rows",
    [
        [amount(">=", 20000), amount(">=", 5000)],  # tiers from the top: both apply
        [amount("<", 5000), amount("<=", 5000)],  # exactly 5,000 is left for row 2
        [amount(">", 5000), amount(">=", 5000)],
        [amount(">=", 5000), {"if": "amount_eur >= 6000 or vip == true", "then": "ask"}],
        [
            amount(">=", 5000),
            {
                "if": [
                    {"field": "amount_eur", "op": ">=", "value": 6000},
                    {"field": "supplier", "op": "=", "value": "x"},
                ],
                "then": "ask",
            },
        ],
    ],
)
def test_rows_that_can_apply_are_not_reported(rows: list[dict[str, Any]]) -> None:
    assert [n for n in case_notes(table_of(rows)) if n.kind != "gap"] == []


def test_shadowing_matches_what_the_evaluator_does() -> None:
    cases = table_of([amount(">=", 5000), amount(">=", 20000, "reject")])
    assert not any(c.expected.row == 2 for c in boundary_cases(cases))


def test_contradicting_conditions_are_reported() -> None:
    row = {
        "if": [
            {"field": "amount_eur", "op": ">", "value": 5000},
            {"field": "amount_eur", "op": "<", "value": 1000},
        ],
        "then": "reject",
    }
    notes = case_notes(table_of([row]))
    assert ("contradiction", 1) in [(n.kind, n.row) for n in notes]


def test_numbers_no_row_decides_are_named_for_a_one_field_table() -> None:
    notes = case_notes(table_of([amount(">=", 20000), amount(">", 5000), amount("<", 0, "reject")]))
    assert [n.text for n in notes if n.kind == "gap"] == [
        "No row decides amount from 0 EUR and up to 5,000 EUR; these go to else (a person decides)."
    ]


def test_no_gap_note_when_the_rows_cover_every_number() -> None:
    assert case_notes(table_of([amount("<", 5000), amount(">=", 5000)])) == []
