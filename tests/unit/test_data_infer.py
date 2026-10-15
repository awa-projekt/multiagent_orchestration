"""Column inference (`awa.data._infer`): types, number and date formats decided from the values."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from awa.data import ColumnProfile
from awa.data._infer import cell_kind, infer_column, number_reader, unit_of
from awa.kernel.conditions import as_number


def column(values: list[Any], **kwargs: Any) -> tuple[ColumnProfile, list[tuple[str, str]]]:
    notes: list[tuple[str, str]] = []
    return infer_column(values, notes=notes, **kwargs), notes


def codes(notes: list[tuple[str, str]]) -> list[str]:
    return [code for code, _ in notes]


# --- numbers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "kind", "decimal", "thousands", "low", "high"),
    [
        (
            ["4.250,00", "5.980,50", "1.200,00", "23.400,00", "880,75"],
            "decimal",
            ",",
            ".",
            880.75,
            23400.0,
        ),
        (["1,234.56", "7.5", "1,000,000.00"], "decimal", ".", ",", 7.5, 1000000.0),
        (["1.234,5", "2,25"], "decimal", ",", ".", 2.25, 1234.5),
        (["12,5", "0,75", "3"], "decimal", ",", None, 0.75, 12.5),
        (["12.5", "0.75", "3"], "decimal", ".", None, 0.75, 12.5),
        (["1 234,50", "5,5"], "decimal", ",", " ", 5.5, 1234.5),
        (["1\xa0234,50", "5,5"], "decimal", ",", " ", 5.5, 1234.5),
        (["1'234.50", "5.00"], "decimal", ".", "'", 5.0, 1234.5),
        (["12", "340", "5"], "integer", None, None, 5, 340),
        (["1.234.567", "2.000"], "integer", None, ".", 2000, 1234567),
        (["0.123", "0.456"], "decimal", ".", None, 0.123, 0.456),
        (["-5", "12", "7"], "integer", None, None, -5, 12),
    ],
)
def test_the_number_notation_is_decided_from_the_values(
    values: list[str],
    kind: str,
    decimal: str | None,
    thousands: str | None,
    low: float,
    high: float,
) -> None:
    c, notes = column(values)

    assert c.type == kind and not c.ambiguous
    assert (c.min, c.max) == (low, high)
    if decimal or thousands:
        assert c.format is not None
        assert (c.format.decimal, c.format.thousands) == (decimal, thousands)
    else:
        assert c.format is None
    assert ("DF-W-DECIMAL-COMMA" in codes(notes)) == (decimal == ",")


def test_one_number_that_reads_two_ways_makes_the_column_ambiguous_and_decides_nothing() -> None:
    c, notes = column(["1.234", "2.345", "10.500"])

    assert c.type == "decimal" and c.ambiguous
    assert c.format is None and (c.min, c.max) == (None, None)
    assert codes(notes) == ["DF-W-NUMBER-AMBIGUOUS"]
    assert "'1.234'" in notes[0][1]


def test_a_value_that_only_one_notation_reads_settles_the_ambiguity() -> None:
    german, _ = column(["1.234", "5,5"])
    american, _ = column(["1,234", "5.5"])

    assert (german.format.decimal, german.format.thousands, german.max) == (",", ".", 1234)  # type: ignore[union-attr]
    assert (american.format.decimal, american.format.thousands, american.max) == (".", ",", 1234)  # type: ignore[union-attr]
    assert not german.ambiguous and not american.ambiguous


def test_a_leading_zero_rules_out_a_thousands_separator() -> None:
    c, _ = column(["0,123", "0,456"])

    assert c.type == "decimal" and not c.ambiguous and c.format is not None
    assert c.format.decimal == ","


@pytest.mark.parametrize(
    ("values", "negative", "low"),
    [
        (["(1.234,56)", "10,5"], "parentheses", -1234.56),
        (["12,5-", "7,5"], "trailing_minus", -12.5),
        (["-12,5", "7,5"], "minus", -12.5),
    ],
)
def test_negative_numbers_in_three_styles(values: list[str], negative: str, low: float) -> None:
    c, _ = column(values)

    assert c.format is not None and c.format.negative == negative and c.min == low


def test_percent_and_currency_are_part_of_the_format() -> None:
    percent, _ = column(["12 %", "5,5%", "100 %"])
    euro, _ = column(["€ 5,00", "7,50 €"])
    mixed, notes = column(["12 EUR", "5 USD"])

    assert percent.format is not None and percent.format.percent and percent.max == 100
    assert euro.format is not None and euro.format.currency == "€" and euro.min == 5.0
    assert mixed.format is None  # two currencies: no single one to state
    assert codes(notes) == ["DF-W-MIXED-CURRENCY"]


def test_a_percent_sign_on_some_values_only_is_reported() -> None:
    c, notes = column(["12 %", "5", "7"])

    assert c.format is None or not c.format.percent
    assert "DF-W-MIXED-NUMBER-FORMAT" in codes(notes)


def test_two_notations_in_one_column_are_reported_with_the_losing_count() -> None:
    c, notes = column(["4.250,00", "1.200,00", "3,5", "1,234.56"])

    assert c.type == "decimal" and c.format is not None and c.format.decimal == ","
    assert c.max == 4250.0  # the value of the other notation is not read
    assert "1 of 4 text numbers are written in another notation" in notes[0][1]


def test_digits_that_must_stay_text_are_ids() -> None:
    zero, notes = column(["004711", "000123", "5"])
    long, _ = column(["4006381333931", "12345678901234567"])
    named, _ = column(["10", "20", "30"], name="customer_id")

    assert zero.type == "id" and codes(notes) == ["DF-W-LEADING-ZEROS"]
    assert zero.min is None
    assert long.type == "id"
    assert named.type == "integer"  # a name is not evidence


def test_a_few_values_that_do_not_fit_keep_the_type_and_are_listed() -> None:
    c, notes = column([str(i) for i in range(1, 20)] + ["see note"])

    assert c.type == "integer" and (c.min, c.max) == (1, 19)
    assert codes(notes) == ["DF-W-MIXED-TYPES"]
    assert "1 of 20 values do not fit the integer type (e.g. 'see note')" in notes[0][1]


def test_many_values_that_do_not_fit_make_the_column_mixed() -> None:
    c, notes = column([str(i) for i in range(1, 18)] + ["a", "b", "c"])

    assert c.type == "mixed" and codes(notes) == ["DF-W-MIXED-TYPES"]
    assert "17 numbers, 3 text values" in notes[0][1]


def test_placeholders_in_a_typed_column_count_as_empty() -> None:
    c, notes = column(["12", "n/a", "14", "-", ""])

    assert c.type == "integer" and (c.min, c.max) == (12, 14)
    assert c.null_share == 0.6 and c.nullable
    assert codes(notes) == ["DF-W-NULL-TOKENS"]


def test_placeholders_in_a_text_column_are_values() -> None:
    c, notes = column(["n/a", "red", "n/a", "blue", "n/a", "red"])

    assert c.type == "enum" and c.null_share == 0.0 and not notes


def test_text_that_only_looks_numeric_is_not_a_number() -> None:
    for values in (["1.2.3", "4.5.6"], ["12-34", "56-78"], ["1e5", "2e5"], ["0x1F", "0x2A"]):
        assert column(values)[0].type in ("text", "enum")


# --- dates ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "kind", "pattern", "low", "high"),
    [
        (["2026-09-15", "2026-09-16"], "date", "YYYY-MM-DD", "2026-09-15", "2026-09-16"),
        (
            ["11.11.2024", "03.10.2024", "16.11.2024"],
            "date",
            "DD.MM.YYYY",
            "2024-10-03",
            "2024-11-16",
        ),
        (["12/31/2024", "01/02/2024"], "date", "MM/DD/YYYY", "2024-01-02", "2024-12-31"),
        (["13/12/2024", "01/02/2024"], "date", "DD/MM/YYYY", "2024-02-01", "2024-12-13"),
        (["1.2.2024", "31.12.2024"], "date", "D.M.YYYY", "2024-02-01", "2024-12-31"),
        (["31.12.24", "01.02.70"], "date", "DD.MM.YY", "1970-02-01", "2024-12-31"),
        (["2024/10/05", "2024/11/06"], "date", "YYYY/MM/DD", "2024-10-05", "2024-11-06"),
        (["5 Jan 2024", "12 Feb 2024"], "date", "D MMM YYYY", "2024-01-05", "2024-02-12"),
        (["5. Januar 2024", "12. März 2025"], "date", "D MMMM YYYY", "2024-01-05", "2025-03-12"),
        (["Jan 5, 2024", "Feb 12, 2024"], "date", "MMM D, YYYY", "2024-01-05", "2024-02-12"),
        (
            ["2026-10-02T08:41:00Z", "2026-10-02T09:05:00Z"],
            "datetime",
            "YYYY-MM-DDTHH:MM:SSZ",
            "2026-10-02T08:41:00",
            "2026-10-02T09:05:00",
        ),
        (
            ["2026-10-12 10:00", "2026-10-13 14:00"],
            "datetime",
            "YYYY-MM-DD HH:MM",
            "2026-10-12T10:00:00",
            "2026-10-13T14:00:00",
        ),
        (
            ["2026-10-02 08:41:07.123", "2026-10-02 09:05:00.5"],
            "datetime",
            "YYYY-MM-DD HH:MM:SS.SSS",
            "2026-10-02T08:41:07.123000",
            "2026-10-02T09:05:00.500000",
        ),
        (
            ["31.12.2024 23:59", "01.01.2025 00:00"],
            "datetime",
            "DD.MM.YYYY HH:MM",
            "2024-12-31T23:59:00",
            "2025-01-01T00:00:00",
        ),
    ],
)
def test_the_date_format_is_decided_from_the_values(
    values: list[str], kind: str, pattern: str, low: str, high: str
) -> None:
    c, _ = column(values)

    assert (c.type, c.date_format, c.ambiguous) == (kind, pattern, False)
    assert (c.min, c.max) == (low, high)


def test_a_day_above_twelve_settles_the_order_and_says_so() -> None:
    c, notes = column(["11.11.2024", "16.11.2024"])

    assert codes(notes) == ["DF-I-DATE-FORMAT"]
    assert "a day above 12 rules out MM.DD.YYYY" in notes[0][1]


def test_dates_that_read_both_ways_are_a_question_not_a_guess() -> None:
    c, notes = column(["03.10.2024", "04.10.2024", "05.06.2024"])

    assert c.type == "date" and c.ambiguous and c.date_format is None
    assert (c.min, c.max) == (None, None)
    assert codes(notes) == ["DF-W-DATE-AMBIGUOUS"]
    assert "DD.MM.YYYY or MM.DD.YYYY" in notes[0][1]


def test_a_second_date_format_in_a_column_is_reported() -> None:
    c, notes = column(["2024-01-05", "2024-02-10", "2024-03-15", "16.03.2024"])

    assert c.date_format == "YYYY-MM-DD"
    assert codes(notes) == ["DF-W-MIXED-DATE-FORMATS"]
    assert "1 of 4 dates do not fit the most common format" in notes[0][1]


def test_impossible_dates_are_text() -> None:
    assert column(["31.02.2024", "30.02.2024"])[0].type in ("text", "enum")
    assert column(["2024-13-01", "2024-14-01"])[0].type in ("text", "enum")
    assert column(["25:61", "99:99"])[0].type in ("text", "enum")


def test_times_of_day() -> None:
    short, _ = column(["10:30", "08:05"])
    long, _ = column(["10:30:15", "08:05"])

    assert (short.type, short.date_format) == ("time", "HH:MM")
    assert (long.type, long.date_format) == ("time", "HH:MM:SS")


# --- other kinds ---------------------------------------------------------------------------


def test_yes_no_words_are_a_bool_column_with_their_counts() -> None:
    c, _ = column(["ja", "nein", "ja", "Ja"])

    assert c.type == "bool"
    assert [(t.value, t.count) for t in c.top] == [("ja", 2), ("Ja", 1), ("nein", 1)]


def test_one_yes_word_alone_is_a_value_not_a_bool_column() -> None:
    c, _ = column(["yes", "yes", "yes", "yes"])

    assert c.type == "enum"


def test_few_repeated_values_are_an_enum_with_a_stable_top_list() -> None:
    c, _ = column(["offen", "paid", "offen", "void", "paid", "offen"])

    assert c.type == "enum" and c.distinct == 3 and not c.unique
    assert [(t.value, t.count) for t in c.top] == [("offen", 3), ("paid", 2), ("void", 1)]


def test_many_different_values_are_text_and_keep_three_examples_as_written() -> None:
    c, _ = column(["  Anna Keller ", "Ben Vogt", "Chloe Marin", "Dmitri Petrov"])

    assert c.type == "text" and c.unique and c.distinct == 4
    assert c.examples == ["Anna Keller", "Ben Vogt", "Chloe Marin"]
    assert c.top == []


def test_empty_columns_and_null_share() -> None:
    empty, _ = column(["", None, "   "])
    partly, _ = column(["a", "", "b", None])

    assert (
        empty.type == "empty" and empty.null_share == 1.0 and empty.nullable and empty.distinct == 0
    )
    assert partly.null_share == 0.5 and partly.nullable and partly.type == "text"
    assert column([])[0].type == "empty"


def test_examples_and_top_values_are_cut() -> None:
    c, _ = column(["x" * 500, "y" * 500], cell_chars=40)

    assert [len(e) for e in c.examples] == [40, 40]


@pytest.mark.parametrize(
    ("name", "unit"),
    [
        ("Betrag_EUR", "EUR"),
        ("amount_eur", "EUR"),
        ("Gewicht [kg]", "kg"),
        ("Preis (in USD)", "USD"),
        ("Anteil %", "%"),
        ("Menge St", "st"),
        ("order_id", None),
        ("Status", None),
        ("Name (Vorname)", None),
    ],
)
def test_the_unit_comes_from_the_header(name: str, unit: str | None) -> None:
    assert unit_of(name) == unit
    assert infer_column(["1"], name=name).unit == unit


def test_the_key_follows_the_column_key_rule_unless_given() -> None:
    assert infer_column(["a"], name="Unit Price").key == "unit_price"
    assert infer_column(["a"], name="Unit Price", key="unit_price_2", index=3).key == "unit_price_2"
    assert infer_column(["a"], name="x", index=3).index == 3


# --- typed values (workbooks, databases) -----------------------------------------------------


def test_typed_numbers_keep_their_type_and_have_no_text_format() -> None:
    ints, _ = column([1, 2, None, 40])
    floats, _ = column([1, 2.5, Decimal("3.25")])

    assert (ints.type, ints.min, ints.max, ints.format, ints.null_share) == (
        "integer",
        1,
        40,
        None,
        0.25,
    )
    assert (floats.type, floats.min, floats.max, floats.format) == ("decimal", 1, 3.25, None)


def test_typed_dates_have_no_text_pattern() -> None:
    days, _ = column([datetime(2024, 1, 1), datetime(2024, 2, 1), None])
    stamps, _ = column([datetime(2024, 1, 1, 10, 30), date(2024, 2, 1)])
    clocks, _ = column([time(10, 30), time(8, 5, 7)])

    assert (days.type, days.date_format, days.min, days.max) == (
        "date",
        None,
        "2024-01-01",
        "2024-02-01",
    )
    assert (stamps.type, stamps.min, stamps.max) == (
        "datetime",
        "2024-01-01T10:30:00",
        "2024-02-01T00:00:00",
    )
    assert (clocks.type, clocks.date_format) == ("time", None)


def test_typed_booleans_and_unsupported_floats() -> None:
    assert column([True, False, True])[0].type == "bool"
    assert column([float("nan"), float("inf")])[0].type in ("text", "enum")


def test_text_numbers_next_to_typed_numbers_are_read_together() -> None:
    c, _ = column([1, 2.5, "3,75", "1.000,00"])  # a workbook where a few numbers were typed as text

    assert c.type == "decimal" and c.max == 1000.0 and c.format is not None
    assert c.format.decimal == ","


# --- reading cells back ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("", "empty"),
        (None, "empty"),
        ("Summe", "text"),
        ("1.234,56", "number"),
        ("(12)", "number"),
        ("12.10.2024", "date"),
        ("10:30", "date"),
        ("ja", "bool"),
        ("n/a", "text"),
        (5, "number"),
        (True, "bool"),
        (datetime(2024, 1, 1), "date"),
    ],
)
def test_what_a_cell_looks_like(text: Any, kind: str) -> None:
    assert cell_kind(text) == kind


def test_number_reader_reads_cells_of_a_column_under_its_notation() -> None:
    c, _ = column(["4.250,00", "880,75"])
    read = number_reader(c)

    assert [read(v) for v in ("4.250,00", "880,75", " 1.000,5 ", "x", None, True, 7)] == [
        4250.0,
        880.75,
        1000.5,
        None,
        None,
        None,
        7,
    ]


@pytest.mark.parametrize(
    "text", ["4.250,00", "880,75", "(1.234,56)", "12,5 %", "€ 5,00", "7,50-", "12"]
)
def test_german_numbers_read_like_the_toolkit_reads_them(text: str) -> None:
    # `query_csv` reads numbers with `as_number(value, decimal)`; the profile must agree.
    c, _ = column([text, "1.000,00"])
    read = number_reader(c)

    assert read(text) == as_number(text, ",")


# --- properties ----------------------------------------------------------------------------------


def german(value: Decimal) -> str:
    whole, _, cents = f"{value:.2f}".partition(".")
    groups = f"{int(whole):,}".replace(",", ".")
    return f"{groups},{cents}"


def american(value: Decimal) -> str:
    return f"{value:,.2f}"


amounts = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999999.99"), places=2)


@settings(max_examples=200, deadline=None)
@given(st.lists(amounts, min_size=1, max_size=12))
def test_amounts_written_in_either_notation_read_back_exactly(values: list[Decimal]) -> None:
    for write, decimal in ((german, ","), (american, ".")):
        written = [write(v) for v in values]
        c, _ = column(written)
        read = number_reader(c)

        assert not c.ambiguous
        assert c.format is not None and c.format.decimal == decimal
        assert [read(t) for t in written] == [float(v) for v in values]
        assert (c.min, c.max) == (float(min(values)), float(max(values)))


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.dates(min_value=date(1971, 1, 1), max_value=date(2099, 12, 31)), min_size=1, max_size=10
    )
)
def test_iso_dates_read_back_exactly(days: list[date]) -> None:
    c, _ = column([d.isoformat() for d in days])

    assert (c.type, c.date_format, c.ambiguous) == ("date", "YYYY-MM-DD", False)
    assert (c.min, c.max) == (min(days).isoformat(), max(days).isoformat())


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.dates(min_value=date(1971, 1, 1), max_value=date(2099, 12, 31)), min_size=1, max_size=10
    )
)
def test_german_dates_are_ambiguous_exactly_when_no_day_exceeds_twelve(days: list[date]) -> None:
    c, _ = column([d.strftime("%d.%m.%Y") for d in days])

    undecided = all(d.day <= 12 for d in days)
    assert c.ambiguous == undecided
    if not undecided:
        assert c.date_format == "DD.MM.YYYY" and c.min == min(days).isoformat()


@settings(max_examples=100, deadline=None)
@given(st.lists(st.text(max_size=12), max_size=12))
def test_any_text_gives_a_valid_profile_and_the_same_one_twice(values: list[str]) -> None:
    first, _ = column(values)
    second, _ = column(values)

    assert first == second
    assert 0.0 <= first.null_share <= 1.0
    assert first.distinct is not None and first.distinct <= len(values)


def test_aware_and_naive_dates_in_one_column_give_a_range() -> None:
    from datetime import UTC, datetime

    found, _ = column([datetime(2026, 3, 2, 9, tzinfo=UTC), "2026-03-01", "2026-03-05"])

    assert (found.min, found.max) == ("2026-03-01T00:00:00", "2026-03-05T00:00:00")
