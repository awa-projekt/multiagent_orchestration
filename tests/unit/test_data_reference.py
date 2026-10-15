"""Grounding values in the real data and computing reference results over the real rows."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from _helpers.data_fixtures import ORDERS_CSV, REPO, computed, write_text
from awa.data import (
    Claim,
    DataProfile,
    Limits,
    ReferenceCheck,
    check_grounding,
    check_reference_values,
    grounded,
    profile,
)
from awa.data._model import ColumnProfile, DataIssue, FileProfile, JsonField, JsonInfo, TableProfile
from awa.data._reference import data_tokens


def orders() -> DataProfile:
    return profile(ORDERS_CSV)


def issue_texts(issues: list[DataIssue]) -> list[str]:
    return [f"{i.code} {i.path}" for i in issues]


# ------------------------------------------------------------------ grounded


@pytest.mark.parametrize(
    "value, expected",
    [
        ("4.250,00", True),  # as written
        ("4250", True),  # the same number
        ("4250.00", True),
        ("4.250", True),  # read both ways
        ("880,75", True),
        ("B-1004", True),
        ("kowalski stahl", True),  # a part of a cell, any case
        ("11.11.2024", True),
        ("2024-11-11", True),  # the same day in ISO
        ("B-9999", False),
        ("4251", False),
        ("Nowak", False),
        ("2024-12-31", False),
        ("", True),
    ],
)
def test_grounded_finds_values_in_any_notation_of_the_real_file(value: str, expected: bool) -> None:
    assert grounded(value, orders()) is expected


def test_an_absent_value_is_not_rejected_when_the_data_was_not_read_in_full() -> None:
    small = profile(ORDERS_CSV, limits=Limits(max_rows_exact=3))  # 8 rows: the rest stays unseen

    assert grounded("B-1001", small) and grounded("B-9999", small)  # cannot be disproved
    assert check_grounding(small, [Claim(path="c", value="B-9999")]) == []


def test_a_scan_or_unreadable_file_leaves_the_answer_open() -> None:
    p = orders()
    p.files.append(
        FileProfile(
            id="f2", path="scan.pdf", sha256="0" * 64, size=1, kind="image", status="unsupported"
        )
    )

    assert grounded("B-9999", p) is True
    assert grounded("B-1001", p) is True


def test_a_value_is_found_in_the_cells_of_a_workbook(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Kunde", "Betrag", "Datum"])
    sheet.append(["Meyer GmbH", 4250.0, datetime.date(2026, 3, 5)])
    sheet.append(["Schulz AG", 17, datetime.datetime(2026, 4, 1, 9, 0)])
    path = tmp_path / "w.xlsx"
    workbook.save(path)
    table = TableProfile(
        id="f1.t1",
        sheet=workbook.active.title,
        header_row=1,
        data_start_row=2,
        rows=2,
        columns=[
            ColumnProfile(name=n, key=n.lower(), index=i, type=t)
            for i, (n, t) in enumerate(
                [("Kunde", "text"), ("Betrag", "decimal"), ("Datum", "date")]
            )
        ],
    )
    file = FileProfile(
        id="f1",
        path="w.xlsx",
        source=str(path),
        sha256="0" * 64,
        size=1,
        kind="xlsx",
        tables=[table],
    )
    p = DataProfile(awa_version="1", created="x", files=[file])

    assert grounded("4.250,00", p) and grounded("4250", p)  # a typed number
    assert grounded("2026-03-05", p) and grounded("05.03.2026", p)  # a date cell, either notation
    assert grounded("schulz", p)
    assert not grounded("5000", p) and not grounded("Weber", p)


@pytest.mark.parametrize(
    "dates",
    [["15 Jan 2026", "27 Feb 2026"], ["15.01.26", "27.02.26"], ["Jan 15, 2026", "Feb 27, 2026"]],
)
def test_a_text_date_is_found_whatever_the_column_writes(tmp_path: Path, dates: list[str]) -> None:
    rows = "".join(f'"{day}";{n}\n' for n, day in enumerate(dates))
    p = profile(write_text(tmp_path, "d.csv", f"Datum;Nr\n{rows}"))

    assert grounded("2026-01-15", p) and grounded("27.02.2026", p)
    assert not grounded("2026-03-09", p)


def test_a_windows_1252_text_is_read_in_its_own_encoding(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_bytes("Kunde: Müller GmbH\n".encode("cp1252"))

    p = profile(tmp_path / "note.txt")

    assert grounded("Müller GmbH", p) and not grounded("Schäfer AG", p)


def test_a_value_is_found_in_the_text_of_a_json_file(tmp_path: Path) -> None:
    path = write_text(tmp_path, "po.json", '{"orders": [{"id": "PO-4711", "total": 1428.0}]}')
    file = FileProfile(
        id="f1", path="po.json", source=str(path), sha256="0" * 64, size=1, kind="json"
    )
    p = DataProfile(awa_version="1", created="x", files=[file])

    assert grounded("PO-4711", p) and grounded("1.428,00", p)
    assert not grounded("PO-4712", p)


# ------------------------------------------------------------------ check_grounding (V008)


def test_data_like_parts_of_an_expectation() -> None:
    assert data_tokens(
        "Order B-1004 for 4.250,00 EUR was held on 11.11.2024; 3 orders, 12 items"
    ) == ["B-1004", "4.250,00", "11.11.2024"]
    assert data_tokens("approved by the manager") == []
    assert data_tokens("LN-2024-17, 2024-0412.") == ["LN-2024-17", "2024-0412"]


def test_an_invented_value_in_an_expectation_is_v008_with_the_closest_real_value() -> None:
    p = orders()
    claims = [
        Claim(path="cases[0].expect[0]", value="Order B-1004 over 4.250,00 is released"),
        Claim(path="cases[0].expect[1]", value="Order B-9999 over 4.250,00 is released"),
        Claim(path="cases[1].expect[0]", value="order B-1O01 needs approval"),
        Claim(
            path="cases[2].expect[0]",
            value="the order B-9999 is new",
            request="Please check B-9999",
        ),
    ]

    got = check_grounding(p, claims)

    assert issue_texts(got) == ["DF-V-008 cases[0].expect[1]", "DF-V-008 cases[1].expect[0]"]
    assert (
        "'B-9999'" in got[0].message
        and "orders.csv" in got[0].message
        and got[0].suggestion is None
    )
    assert got[1].suggestion == "B-1001"


def test_grounding_costs_one_pass_and_reports_each_claim_once_per_value() -> None:
    claims = [Claim(path=f"c[{i}]", value="B-9999 and B-9998") for i in range(3)]

    assert len(check_grounding(orders(), claims)) == 6


# ------------------------------------------------------------------ computed values


# The file: 8 orders, 7 "offen" (4250 + 5980.5 + 1200 + 23400 + 880.75 + 7150 + 3300 = 46161.25),
# one "storniert" (9900), 5 in Germany (27530.5), total 56061.25.
@pytest.mark.parametrize(
    "expression, expected",
    [
        ("sum(Betrag_EUR where Status == 'offen')", 46161.25),
        ("sum(Betrag_EUR where Land == 'DE')", 27530.5),
        ("count()", 8),
        ("count(Bestellnr)", 8),
        ("count(where Betrag_EUR > 10000)", 1),
        ("count(Status where Status == 'offen' and Land == 'DE')", 4),
        ("count(where Land == 'DE' and Status == 'offen')", 4),
        ("max(Betrag_EUR)", 23400),
        ("min(Betrag_EUR)", 880.75),
        ("count_distinct(Lieferant)", 4),
        ("sum(Betrag_EUR) / count()", 7007.65625),
        ("round(sum(Betrag_EUR) / count(), 2)", 7007.66),
        (
            "sum(Betrag_EUR where Status == 'offen') + sum(Betrag_EUR where Status != 'offen')",
            56061.25,
        ),
        ("sum(Betrag_EUR where contains(Lieferant, 'ller'))", 7180.5),
        ("avg(Betrag_EUR where Land == 'DE')", 5506.1),
        ("min(3, 4)", 3),
        ("sum(Betrag_EUR where Land == 'XX')", 0),
        ("count(where Land == 'XX')", 0),
        ("sum(Betrag_EUR where Status == 'a where b')", 0),  # the word where inside a string
    ],
)
def test_reference_values_over_the_real_rows(expression: str, expected: float | int) -> None:
    assert computed(expression, orders(), file="orders.csv") == pytest.approx(expected)


def test_text_columns_have_a_smallest_and_a_largest_value() -> None:
    p = orders()

    assert computed("min(Wunschliefertermin)", p, file="orders.csv") == "03.10.2024"
    assert computed("max(Land)", p, file="orders.csv") == "PL"


def test_what_cannot_be_computed_is_none() -> None:
    p = orders()

    for expression in [
        "sum(Betrag)",  # unknown column
        "sum(Lieferant)",  # text
        "sum(Betrag_EUR) / 0",  # division by zero
        "avg(Betrag_EUR where Land == 'XX')",  # no row
        "sum(Betrag_EUR",  # unbalanced
        "Betrag_EUR + 1",  # a column outside an aggregate
        "sum(Betrag_EUR) > 3",  # a comparison is not a value
        "sum(__import__('os'))",
        "sum()",
        "sum(Betrag_EUR where Nope == 1)",
        "9**9**9 + count()",
    ]:
        assert computed(expression, p, file="orders.csv") is None, expression
    assert computed("count()", p, file="nope.csv") is None
    assert computed("count()", p, file="orders.csv", sheet="Tabelle1") is None


def test_a_check_expression_cannot_run_code() -> None:
    p = orders()

    assert (
        computed("count(where __import__('os').system('echo hi') == 0)", p, file="orders.csv")
        is None
    )
    assert computed("count(where open('x') == 1)", p, file="orders.csv") is None
    assert computed("count(where Land.__class__ == 'x')", p, file="orders.csv") is None


def test_a_join_check_counts_the_rows_that_match_another_file(tmp_path: Path) -> None:
    write_text(
        tmp_path, "pay.csv", "ref;amount\r\nINV-1;10,00\r\nINV-2;20,00\r\nXYZ;5,00\r\n", "cp1252"
    )
    write_text(
        tmp_path, "inv.csv", "nr;total\r\nINV-1;10,00\r\nINV-2;20,00\r\nINV-3;30,00\r\n", "cp1252"
    )
    p = profile(tmp_path)

    matched = computed("count(ref where ref in values('inv.csv', 'nr'))", p, file="pay.csv")
    unmatched = computed("sum(amount where ref not in values('inv.csv', 'nr'))", p, file="pay.csv")

    assert matched == 2 and unmatched == 5
    assert computed("count(ref where ref in values('nope.csv', 'nr'))", p, file="pay.csv") is None
    assert computed("count(ref where ref in values('inv.csv', 'zzz'))", p, file="pay.csv") is None


def test_a_real_benchmark_file_with_a_decimal_comma() -> None:
    folder = REPO / "examples" / "benchmarks" / "core" / "04_document_intake" / "data"
    p = profile(folder / "vendors.csv")

    assert computed("max(Bestelllimit_EUR)", p, file="vendors.csv") == 50000
    assert computed("count(where Status == 'aktiv')", p, file="vendors.csv") is not None


@pytest.mark.slow
def test_a_hundred_thousand_rows_are_read_in_one_pass(tmp_path: Path) -> None:
    rows = "\r\n".join(f"{n};{n % 7};{n},50" for n in range(100_000))
    write_text(tmp_path, "big.csv", "id;grp;amount\r\n" + rows + "\r\n", "cp1252")
    p = profile(tmp_path / "big.csv")

    assert computed("count(where grp == 3)", p, file="big.csv") == len(
        [n for n in range(100_000) if n % 7 == 3]
    )
    assert computed("count()", p, file="big.csv") == 100_000


def test_more_rows_than_the_limit_cannot_be_computed() -> None:
    p = profile(ORDERS_CSV, limits=Limits(max_rows_exact=3))

    assert computed("count()", p, file="orders.csv") is None


# ------------------------------------------------------------------ check_reference_values (DF-V-001..010)


def check(expression: str, expected: str, **extra: str) -> ReferenceCheck:
    return ReferenceCheck(
        path="cases[0].check", file="orders.csv", expression=expression, expected=expected, **extra
    )


def test_a_result_that_agrees_in_any_notation_has_no_issue() -> None:
    p = orders()
    checks = [
        check("sum(Betrag_EUR where Status == 'offen')", "46.161,25"),
        check("sum(Betrag_EUR where Status == 'offen')", "46161.25"),
        check("sum(Betrag_EUR where Status == 'offen')", "46,161.25"),
        check("count()", "8"),
        check("max(Betrag_EUR)", "23.400,00"),
        check("sum(Betrag_EUR) / count()", "7007,66"),
    ]

    assert check_reference_values(p, checks) == []


def test_a_contradicted_expectation_is_v010_with_the_computed_value() -> None:
    p = orders()

    (issue,) = check_reference_values(
        p, [check("sum(Betrag_EUR where Status == 'offen')", "28.000,00")]
    )

    assert (issue.code, issue.severity, issue.path) == ("DF-V-010", "error", "cases[0].check")
    assert (
        "46161.25 over 8 rows of 'orders.csv'" in issue.message and "'28.000,00'" in issue.message
    )
    assert issue.suggestion == "46161.25"
    (count,) = check_reference_values(p, [check("count(where Status == 'offen')", "9")])
    assert "= 7 over 8 rows" in count.message and count.suggestion == "7"


def test_the_other_failures_have_their_own_codes() -> None:
    p = orders()

    got = check_reference_values(
        p,
        [
            ReferenceCheck(path="a", file="nope.csv", expression="count()", expected="1"),
            ReferenceCheck(
                path="b", file="orders.csv", sheet="S", expression="count()", expected="1"
            ),
            check("sum(Betrag)", "1"),
            check("sum(Lieferant)", "1"),
            check("count(where Nope == 1)", "1"),
            check("count(", "1"),
            check("avg(Betrag_EUR where Land == 'XX')", "1"),
        ],
    )

    assert [(i.code, i.path) for i in got] == [
        ("DF-V-001", "a.file"),
        ("DF-V-002", "b.sheet"),
        ("DF-V-003", "cases[0].check"),
        ("DF-V-004", "cases[0].check"),
        ("DF-V-003", "cases[0].check"),
        ("DF-V-010", "cases[0].check"),
        ("DF-V-010", "cases[0].check"),
    ]
    assert got[2].suggestion == "Betrag_EUR" and "needs numbers" in got[3].message


def test_data_that_could_not_be_read_in_full_is_a_warning_not_a_contradiction() -> None:
    p = profile(ORDERS_CSV, limits=Limits(max_rows_exact=3))

    (issue,) = check_reference_values(p, [check("count()", "99")])

    assert (issue.code, issue.severity) == (
        "DF-V-010",
        "warning",
    ) and "could not be verified" in issue.message


# ------------------------------------------------------------------ JSON records


def json_profile(tmp_path: Path, name: str, text: str, top_level: str) -> DataProfile:
    path = write_text(tmp_path, name, text)
    paths = [
        JsonField(path="id", types=["string"]),
        JsonField(path="total", types=["number"]),
        JsonField(path="deliveryAddress", types=["string", "null"], optional_share=0.2),
        JsonField(path="customer.city", types=["string"]),
        JsonField(path="lines[].sku", types=["string"]),
    ]
    file = FileProfile(
        id="f1",
        path=name,
        source=str(path),
        sha256="0" * 64,
        size=1,
        kind="json",
        json_doc=JsonInfo(top_level=top_level, records=4, paths=paths),  # type: ignore[arg-type]
    )
    return DataProfile(awa_version="1", created="x", files=[file])


RECORDS = [
    '{"id": "O-1", "total": 10.5, "deliveryAddress": "Hauptstr. 1", "customer": {"city": "Bonn"}, "lines": [{"sku": "A"}]}',
    '{"id": "O-2", "total": 20, "deliveryAddress": null, "customer": {"city": "Köln"}, "lines": []}',
    '{"id": "O-3", "total": 5, "customer": {"city": "Bonn"}}',
    '{"id": "O-4", "total": 4.5, "deliveryAddress": null, "customer": {"city": "Köln"}}',
]


def test_a_jsonl_file_is_computed_over_like_a_table(tmp_path: Path) -> None:
    p = json_profile(tmp_path, "shop.jsonl", "\n".join(RECORDS) + "\n", "jsonl")

    def value(expression: str) -> object:
        return computed(expression, p, file="shop.jsonl")

    assert value("count(id where deliveryAddress == null)") == 3  # null and absent
    assert value("count(where customer_city == 'Bonn')") == 2  # a nested path by its key
    assert value("count(where customer.city == 'Bonn')") is None  # attributes are not allowed
    assert value("sum(total)") == pytest.approx(40)
    assert value("count()") == 4
    assert value("max(id)") == "O-4"
    assert value("count(id where id in values('shop.jsonl', 'id'))") == 4


def test_lines_that_are_not_objects_are_no_records(tmp_path: Path) -> None:
    p = json_profile(
        tmp_path, "shop.jsonl", RECORDS[0] + "\n42\n[1, 2]\n" + RECORDS[1] + "\n", "jsonl"
    )

    assert computed("count()", p, file="shop.jsonl") == 2


def test_a_json_array_and_a_single_object_are_records_too(tmp_path: Path) -> None:
    array = json_profile(tmp_path, "shop.json", "[" + ",".join(RECORDS) + "]", "array")
    single = json_profile(tmp_path, "one.json", RECORDS[0], "object")

    assert computed("count()", array, file="shop.json") == 4
    assert computed("sum(total)", single, file="one.json") == pytest.approx(10.5)


def test_json_references_are_checked_and_unreadable_json_is_a_warning(tmp_path: Path) -> None:
    p = json_profile(tmp_path, "shop.jsonl", RECORDS[0] + "\nnot json\n", "jsonl")

    ok = check_reference_values(
        p, [ReferenceCheck(path="c", file="shop.jsonl", expression="sum(total)", expected="10,5")]
    )
    typo = check_reference_values(
        p,
        [
            ReferenceCheck(
                path="c",
                file="shop.jsonl",
                expression="count(where deliveryAdress == null)",
                expected="1",
            )
        ],
    )

    # the second line is damaged
    assert [(i.code, i.severity) for i in ok] == [("DF-V-010", "warning")]
    assert typo[0].code == "DF-V-003" and typo[0].suggestion == "deliveryAddress"
