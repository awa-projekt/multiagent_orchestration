"""`awa.toolkits.data`: CSV reading, querying and writing, Excel, JSON queries."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool, ToolException

from awa.errors import ConfigurationError, OptionalDependencyError
from awa.kernel.conditions import predicate
from awa.toolkits import data_toolkit
from awa.toolkits._jsonquery import json_query
from awa.tools.approval import APPROVAL_METADATA_KEY

SALES = """region,product,Unit Price,amount,rep
EU,widget,2.5,100,Ann
EU,gadget,10,40,Bob
US,widget,2.5,300,Cid
US,gadget,10,,Dee
APAC,widget,3,50,Ann
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "sales.csv").write_text(SALES, encoding="utf-8")
    (tmp_path / "semi.csv").write_text("a;b\n1;2\n3;4\n", encoding="utf-8")
    return tmp_path


def tools(root: Path | None, **options: Any) -> dict[str, BaseTool]:
    options.setdefault("frame_output", False)
    return {tool.name: tool for tool in data_toolkit(root, **options)}


def call_json(tool: BaseTool, **args: Any) -> Any:
    result = tool.invoke(args)
    assert isinstance(result, str)
    assert not result.startswith("Error"), result
    return json.loads(result)


def call(tool: BaseTool, **args: Any) -> str:
    result = tool.invoke(args)
    assert isinstance(result, str)
    return result


def test_the_toolkit_and_its_defaults(root: Path) -> None:
    built = tools(root)
    assert list(built) == [
        "read_csv",
        "query_csv",
        "write_csv",
        "read_excel",
        "query_excel",
        "json_query",
        "fill_excel",
    ]
    assert (built["write_csv"].metadata or {})[APPROVAL_METADATA_KEY] is True
    assert list(tools(None)) == ["json_query"]
    with pytest.raises(ConfigurationError, match="need"):
        data_toolkit(include=["read_csv"])
    framed = data_toolkit(root, include=["read_csv"])[0]
    assert call(framed, path="sales.csv").startswith("<tool_output")


def test_read_csv_windows_and_sniffs_delimiters(root: Path) -> None:
    read = tools(root)["read_csv"]
    data = call_json(read, path="sales.csv", offset=1, limit=2)
    assert data["columns"] == ["region", "product", "Unit Price", "amount", "rep"]
    assert data["rows"] == [
        ["EU", "gadget", "10", "40", "Bob"],
        ["US", "widget", "2.5", "300", "Cid"],
    ]
    assert data["total_rows"] == 5
    assert call_json(read, path="semi.csv")["rows"] == [["1", "2"], ["3", "4"]]
    assert call_json(tools(root, max_rows=1)["read_csv"], path="sales.csv")["returned"] == 1
    assert call(read, path="../sales.csv") == "Error: path '../sales.csv' must not contain '..'"


def test_blank_lines_of_a_csv_are_no_rows(root: Path) -> None:
    (root / "gaps.csv").write_text("a;b\n1;2\n\n;\n3;4\n\n", encoding="utf-8")
    built = tools(root)

    read = call_json(built["read_csv"], path="gaps.csv")
    counted = call_json(built["query_csv"], path="gaps.csv", aggregates=["count()"])

    assert read["rows"] == [["1", "2"], ["3", "4"]] and read["total_rows"] == 2
    assert counted["rows"] == [[2]]


def test_query_csv_filters_and_selects(root: Path) -> None:
    query = tools(root)["query_csv"]
    data = call_json(
        query, path="sales.csv", where="region == 'EU' and amount > 50", columns=["rep", "amount"]
    )
    assert data["columns"] == ["rep", "amount"]
    assert data["rows"] == [["Ann", "100"]]
    by_alias = call_json(query, path="sales.csv", where="unit_price >= 10", columns=["rep"])
    assert by_alias["rows"] == [["Bob"], ["Dee"]]
    fuzzy = call_json(query, path="sales.csv", where="contains(rep, 'an') or empty(amount)")
    assert [row[4] for row in fuzzy["rows"]] == ["Ann", "Dee", "Ann"]
    listed = call_json(query, path="sales.csv", where="region in ['EU', 'APAC']", limit=2)
    assert listed["matched_rows"] == 3 and listed["truncated"] is True


def test_query_csv_groups_aggregates_and_orders(root: Path) -> None:
    query = tools(root)["query_csv"]
    data = call_json(
        query,
        path="sales.csv",
        group_by=["product"],
        aggregates=["count()", "sum(amount)", "avg(Unit Price)", "count(amount)", "max(rep)"],
        order_by=["-sum(amount)"],
    )
    assert data["columns"] == [
        "product",
        "count()",
        "sum(amount)",
        "avg(Unit Price)",
        "count(amount)",
        "max(rep)",
    ]
    assert data["rows"] == [
        ["widget", 3, 450, 2.6666666667, 3, "Cid"],
        ["gadget", 2, 40, 10, 1, "Dee"],
    ]
    total = call_json(query, path="sales.csv", aggregates=["sum(amount)", "count_distinct(rep)"])
    assert total["rows"] == [[490, 4]]
    ordered = call_json(query, path="sales.csv", columns=["rep"], order_by=["rep", "-amount"])
    assert [row[0] for row in ordered["rows"]] == ["Ann", "Ann", "Bob", "Cid", "Dee"]


def test_query_csv_reports_mistakes(root: Path) -> None:
    query = tools(root)["query_csv"]
    assert "unknown column 'colour'" in call(query, path="sales.csv", where="colour == 'red'")
    assert "invalid aggregate" in call(query, path="sales.csv", aggregates=["median(amount)"])
    assert "cannot order by" in call(query, path="sales.csv", order_by=["nope"])
    assert "invalid expression" in call(query, path="sales.csv", where="amount >")


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hacked')",
        "amount.__class__",
        "[x for x in rep]",
        "(lambda: 1)()",
        "open('secret.txt')",
        "rep[0]",
        "eval('1')",
        "{'a': 1}",
        "f'{rep}'",
    ],
)
def test_filter_expressions_cannot_escape(root: Path, expression: str) -> None:
    result = call(tools(root)["query_csv"], path="sales.csv", where=expression)
    assert result.startswith("Error: ")


def test_predicates_compare_numeric_text_as_numbers() -> None:
    assert predicate("a > 9").evaluate(lambda name: "10") is True
    assert predicate("a == 2").evaluate(lambda name: "2.0") is True
    assert predicate("a < 'b'").evaluate(lambda name: "a") is True
    assert predicate("a > 1").evaluate(lambda name: "") is False
    assert predicate("not a == 'x'").evaluate(lambda name: "y") is True
    assert predicate("round(a, 1) == 2.3 and abs(a) > 2").evaluate(lambda name: "2.26") is True
    with pytest.raises(ToolException):
        predicate("a * 'x' > 1").evaluate(lambda name: "2")


def test_write_csv_modes_and_formula_escaping(root: Path) -> None:
    write = tools(root, requires_approval=False)["write_csv"]
    rows = [['=HYPERLINK("http://evil")', -5, None], ["+cmd", "-3.5", True]]
    assert "Wrote 2 rows" in call(write, path="out/a.csv", columns=["x", "y", "z"], rows=rows)
    text = (root / "out" / "a.csv").read_text(encoding="utf-8")
    assert text.splitlines() == ["x,y,z", '"\'=HYPERLINK(""http://evil"")",-5,', "'+cmd,-3.5,True"]
    assert "exists" in call(write, path="out/a.csv", columns=["x", "y", "z"], rows=[])
    call(write, path="out/a.csv", columns=["x", "y", "z"], rows=[["1", "2", "3"]], mode="append")
    assert (root / "out" / "a.csv").read_text(encoding="utf-8").count("\n") == 4
    assert "do not match" in call(
        write, path="out/a.csv", columns=["other"], rows=[["1"]], mode="append"
    )
    assert "expected 3" in call(write, path="b.csv", columns=["x", "y", "z"], rows=[["1"]])
    call(write, path="out/a.csv", columns=["q"], rows=[["1"]], mode="overwrite")
    assert (root / "out" / "a.csv").read_text(encoding="utf-8").splitlines() == ["q", "1"]
    assert call(write, path="../x.csv", columns=["q"], rows=[]).startswith("Error")


def test_an_append_to_a_file_without_a_final_newline_starts_a_new_line(root: Path) -> None:
    (root / "open.csv").write_bytes(b"x,y\r\n1,2")
    write = tools(root, requires_approval=False)["write_csv"]

    call(write, path="open.csv", columns=["x", "y"], rows=[["3", "4"]], mode="append")

    assert (root / "open.csv").read_bytes().splitlines() == [b"x,y", b"1,2", b"3,4"]


def test_write_csv_for_excel_with_semicolons_and_a_byte_order_mark(root: Path) -> None:
    write = tools(root, requires_approval=False)["write_csv"]
    columns = ["Kunde", "Betrag"]
    call(
        write,
        path="x.csv",
        columns=columns,
        rows=[["Müller", "1.234,56"]],
        delimiter=";",
        excel=True,
    )
    assert (root / "x.csv").read_bytes() == "﻿Kunde;Betrag\r\nMüller;1.234,56\r\n".encode()
    call(write, path="x.csv", columns=columns, rows=[["Groß", "2"]], mode="append")
    assert (root / "x.csv").read_bytes().endswith("Groß;2\r\n".encode())
    assert (root / "x.csv").read_bytes().count(b"\xef\xbb\xbf") == 1
    (root / "legacy.csv").write_bytes("Kunde;Ort\r\nA;Köln\r\n".encode("cp1252"))
    call(write, path="legacy.csv", columns=["Kunde", "Ort"], rows=[["B", "Jülich"]], mode="append")
    assert (root / "legacy.csv").read_bytes().decode("cp1252").endswith("B;Jülich\r\n")
    read = call_json(tools(root)["read_csv"], path="x.csv")
    assert read["columns"] == columns and read["encoding"] == "utf-8-sig"


def test_read_excel(root: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from datetime import datetime

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Orders"
    sheet.append(["id", "placed", "total"])
    sheet.append([1, datetime(2026, 3, 1, 9, 30), 12.5])
    sheet.append([None, None, None])
    sheet.append([2, datetime(2026, 3, 2), 7])
    workbook.create_sheet("Empty")
    workbook.save(root / "orders.xlsx")
    read = tools(root)["read_excel"]
    data = call_json(read, path="orders.xlsx")
    assert data["sheets"] == ["Orders", "Empty"] and data["sheet"] == "Orders"
    assert data["columns"] == ["id", "placed", "total"]
    assert data["rows"] == [[1, "2026-03-01T09:30:00", 12.5], [2, "2026-03-02", 7]]
    assert "note" not in data
    assert "no sheet" in call(read, path="orders.xlsx", sheet="Nope")
    raw = call_json(read, path="orders.xlsx", header_row=0, limit=1)
    assert raw["columns"] == ["A", "B", "C"] and raw["rows"] == [["id", "placed", "total"]]


def german_workbook(path: Path) -> None:
    """An ERP-style export: a title block above the header, dates, formulas without results."""

    openpyxl = pytest.importorskip("openpyxl")
    from datetime import date

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Umsatz"
    sheet.append(["Umsatzbericht März 2026"])
    sheet.append(["Erstellt von: ERP"])
    sheet.append([])
    sheet.append(["Datum", "Kunde", "Menge", "Preis", "Summe", "Bemerkung"])
    lines = [
        (date(2026, 3, 1), "Müller GmbH", 1500, 0.8, "1.200,00 €"),
        (date(2026, 3, 2), "Bäckerei Groß", 20, 2.25, "45,00 €"),
        (date(2026, 3, 5), "Müller GmbH", 300, 0.8, "240,00 €"),
    ]
    for number, (day, customer, quantity, price, note) in enumerate(lines, start=5):
        sheet.append([day, customer, quantity, price, f"=C{number}*D{number}", note])
    workbook.save(path)


def test_query_excel_with_a_header_row_dates_and_uncalculated_formulas(root: Path) -> None:
    german_workbook(root / "umsatz.xlsx")
    built = tools(root)
    read = call_json(built["read_excel"], path="umsatz.xlsx", header_row=4)
    assert read["columns"] == ["Datum", "Kunde", "Menge", "Preis", "Summe", "Bemerkung"]
    assert read["rows"][0] == ["2026-03-01", "Müller GmbH", 1500, 0.8, None, "1.200,00 €"]
    assert read["formulas_without_values"] == 3
    assert "E5: =C5*D5" in read["note"] and "open and save it in Excel" in read["note"]
    query = built["query_excel"]
    grouped = call_json(
        query,
        path="umsatz.xlsx",
        header_row=4,
        where="Datum >= '2026-03-02'",
        group_by=["Kunde"],
        aggregates=["sum(Menge)", "sum(Bemerkung)"],
        order_by=["-sum(Menge)"],
    )
    assert grouped["rows"] == [["Müller GmbH", 300, 240], ["Bäckerei Groß", 20, 45]]
    assert grouped["decimal"] == "," and grouped["sheet"] == "Umsatz"
    assert "unknown column" in call(query, path="umsatz.xlsx", where="Kunde == 'x'")


GERMAN_CSV = (
    "Belegdatum;Kunde;Artikel;Menge;Betrag;Rabatt\r\n"
    "01.03.2026;Müller GmbH;Schrauben M6;1.500;1.234,56 €;12,5 %\r\n"
    "02.03.2026;Bäckerei Groß;Dübel 8 mm;20;(45,00) €;0 %\r\n"
    "05.03.2026;Müller GmbH;Muttern M6;300;99,90 €;5 %\r\n"
    "07.03.2026;Schäfer & Söhne;Gutschrift;1;1.000,00- €;0 %\r\n"
)


def test_a_german_excel_export_in_cp1252_with_semicolons_and_decimal_commas(root: Path) -> None:
    (root / "export.csv").write_bytes(GERMAN_CSV.encode("cp1252"))
    built = tools(root)
    read = call_json(built["read_csv"], path="export.csv", limit=1)
    assert read["encoding"] == "cp1252" and read["delimiter"] == ";"
    assert read["rows"] == [
        ["01.03.2026", "Müller GmbH", "Schrauben M6", "1.500", "1.234,56 €", "12,5 %"]
    ]
    query = built["query_csv"]
    big = call_json(query, path="export.csv", where="Betrag > 100", columns=["Kunde"])
    assert big["rows"] == [["Müller GmbH"]] and big["decimal"] == ","
    totals = call_json(
        query,
        path="export.csv",
        group_by=["Kunde"],
        aggregates=["sum(Betrag)", "sum(Menge)", "max(Rabatt)"],
        order_by=["Kunde"],
    )
    assert totals["rows"] == [
        ["Bäckerei Groß", -45, 20, 0],
        ["Müller GmbH", 1334.46, 1800, 12.5],
        ["Schäfer & Söhne", -1000, 1, 0],
    ]
    ordered = call_json(query, path="export.csv", columns=["Menge"], order_by=["-Menge"])
    assert [row[0] for row in ordered["rows"]] == ["1.500", "300", "20", "1"]
    english = call_json(query, path="export.csv", where="Menge > 2", decimal=".")
    assert english["matched_rows"] == 2  # 1.500 reads as 1.5 when the decimal is '.'


def test_utf8_with_bom_and_english_number_formats(root: Path) -> None:
    text = 'Region,Revenue,Margin\nNorth,"1,234.50",12.5%\nSouth,(250.00),-3%\nEast,$99,7%\n'
    (root / "en.csv").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    query = tools(root)["query_csv"]
    data = call_json(query, path="en.csv", aggregates=["sum(Revenue)", "avg(Margin)"])
    assert data["rows"] == [[1083.5, 5.5]]
    assert data["encoding"] == "utf-8-sig" and data["delimiter"] == "," and data["decimal"] == "."
    assert call_json(query, path="en.csv", where="Revenue < 0")["rows"][0][0] == "South"


def test_a_utf16_export_with_a_byte_order_mark_is_read(root: Path) -> None:
    from awa.toolkits.data import detect_encoding

    (root / "wide.csv").write_bytes("Kunde\tOrt\r\nA\tKöln\r\n".encode("utf-16"))
    read = call_json(tools(root)["read_csv"], path="wide.csv")
    assert detect_encoding(root / "wide.csv") == "utf-16"
    assert read["columns"] == ["Kunde", "Ort"] and read["rows"] == [["A", "Köln"]]


@pytest.mark.parametrize(
    ("text", "dot", "comma"),
    [
        ("1.234,56 €", 1234.56, 1234.56),
        ("€ -12.50", -12.5, -12.5),
        ("(1,234.56) USD", -1234.56, -1234.56),
        ("1.234,56-", -1234.56, -1234.56),
        ("12,5 %", 12.5, 12.5),
        ("1 234,56", 1234.56, 1234.56),
        ("1.234", 1.234, 1234),
        ("1,234", 1234, 1.234),
        ("1.234.567", 1234567, 1234567),
        ("01.03.2026", None, None),
        ("0171 2345678", None, None),
        ("1e3", 1000.0, 1000.0),
        ("12-34", None, None),
        ("--1", None, None),
        ("+-1", None, None),
        ("-1-", None, None),
    ],
)
def test_numbers_as_reports_write_them(text: str, dot: Any, comma: Any) -> None:
    from awa.kernel.conditions import as_number

    assert as_number(text) == dot
    assert as_number(text, ",") == comma


def test_the_delimiter_is_not_fooled_by_decimal_commas() -> None:
    from awa.toolkits.data import sniff_delimiter

    assert sniff_delimiter("Kunde;Betrag\nA;1,5\nB;2,25\n") == ";"
    assert sniff_delimiter("a,b\n1,2\n") == ","
    assert sniff_delimiter("a\tb\n1\t2\n") == "\t"
    assert sniff_delimiter("only\none\n") == ","


def test_read_excel_without_the_extra(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import awa.toolkits.data as data_module

    def missing(module: str, package: str, *, feature: str) -> Any:
        raise OptionalDependencyError(f"{feature} needs {package}; install it with pip")

    monkeypatch.setattr(data_module, "require_module", missing)
    (root / "x.xlsx").write_bytes(b"PK")
    result = call(tools(root)["read_excel"], path="x.xlsx")
    assert result.startswith("Error: Excel reading needs openpyxl")


# ---------------------------------------------------------------------------- JSON


DOC = {
    "customer": {"name": "ACME", "postal code": "10115"},
    "orders": [
        {"id": 1, "total": 50, "status": "open", "tags": ["a"]},
        {"id": 2, "total": 150, "status": "open", "tags": []},
        {"id": 3, "total": 300, "status": "closed"},
    ],
}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("customer.name", "ACME"),
        ('customer."postal code"', "10115"),
        ("customer['postal code']", "10115"),
        ("orders[0].id", 1),
        ("orders[-1].id", 3),
        ("orders[*].id", [1, 2, 3]),
        ("orders[].total", [50, 150, 300]),
        ("orders[?total > 100].id", [2, 3]),
        ("orders[?total > 100 and status == 'open'].id", [2]),
        ("orders[?contains(status, 'clo')].id", [3]),
        ("orders[*].total | sum", 500),
        ("orders[*].total | avg", 500 / 3),
        ("orders | length", 3),
        ("customer | keys", ["name", "postal code"]),
        ("orders[*].status | unique", ["open", "closed"]),
        ("orders[*].status | sort | first", "closed"),
        ("orders[*].tags[0]", ["a"]),
        ("customer.*", ["ACME", "10115"]),
        ("missing.path", None),
        ("orders[9].id", None),
        ("@", DOC),
    ],
)
def test_json_query_language(query: str, expected: Any) -> None:
    assert json_query(DOC, query) == expected


def test_json_query_reads_large_json_and_json_lines_files(root: Path) -> None:
    orders = [{"id": i, "total": i % 100, "paid": i % 3 == 0} for i in range(50_000)]
    (root / "orders.json").write_text(json.dumps({"orders": orders}), encoding="utf-8")
    assert (root / "orders.json").stat().st_size > 2_000_000
    (root / "events.jsonl").write_text('{"n": 1}\n\n{"n": 2}\n', encoding="utf-8")
    tool = tools(root)["json_query"]
    assert call_json(tool, path="orders.json", query="orders[?paid] | length") == 16667
    assert call_json(tool, path="orders.json", query="orders[*].total | max") == 99
    assert call_json(tool, path="events.jsonl", query="[*].n | sum") == 3
    assert "give either data or path" in call(tool, query="a")
    assert "give either data or path" in call(tool, query="a", data="{}", path="orders.json")
    (root / "bad.json").write_text("{", encoding="utf-8")
    assert "bad.json is not valid JSON" in call(tool, path="bad.json", query="a")
    assert "no data directory" in call(tools(None)["json_query"], path="orders.json", query="a")
    framed = data_toolkit(root, include=["json_query"])[0]
    assert call(framed, path="events.jsonl", query="[0]").startswith("<tool_output")


def test_json_query_tool_reports_errors() -> None:
    tool = tools(None)["json_query"]
    assert call_json(tool, data=json.dumps(DOC), query="orders[?id == 2].total") == [150]
    assert "not valid JSON" in call(tool, data="{nope", query="a")
    assert "unknown function" in call(tool, data="[]", query="@ | explode")
    assert "unbalanced" in call(tool, data="[]", query="a[0")
    assert call(tool, data="[]", query="a[?__import__('os')]").startswith("Error")


def test_write_csv_create_never_replaces_a_file_that_appeared_meanwhile(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = tools(root, requires_approval=False)["write_csv"]
    (root / "race.csv").write_text("mine", encoding="utf-8")
    monkeypatch.setattr(Path, "exists", lambda self: False)  # the check ran before it appeared
    assert "exists" in call(write, path="race.csv", columns=["q"], rows=[["1"]])
    assert (root / "race.csv").read_text(encoding="utf-8") == "mine"


def test_read_excel_stops_reading_at_the_window_and_counts_formulas_cheaply(root: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["n", "calc"])
    for number in range(2, 32):
        sheet.append([number, f"=A{number}*2"])
    workbook.save(root / "long.xlsx")
    read = tools(root)["read_excel"]
    window = call_json(read, path="long.xlsx", limit=3, offset=1)
    assert [row[0] for row in window["rows"]] == [3, 4, 5]
    assert window["has_more"] is True and "total_rows" not in window
    assert window["formulas_without_values"] == 5  # only the rows read were looked at
    whole = call_json(read, path="long.xlsx", limit=500)
    assert whole["total_rows"] == 30 and "has_more" not in whole
    assert whole["formulas_without_values"] == 30
    assert "B11:" in whole["note"] and "B12:" not in whole["note"]  # ten examples


def zip_bomb(path: Path) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", b"\0" * 5_000_000)  # ~5 KB packed


def test_office_files_that_unpack_to_a_bomb_are_refused(root: Path) -> None:
    pytest.importorskip("openpyxl")
    zip_bomb(root / "bomb.xlsx")
    assert "unreasonable size" in call(tools(root)["read_excel"], path="bomb.xlsx")


def test_unique_keeps_order_handles_objects_and_is_fast() -> None:
    from awa.toolkits._jsonquery import json_query

    data = {"rows": [{"a": 1}, {"a": 1}, [1], [1], 1, 1.0, "1"], "big": list(range(100_000)) * 2}
    started = time.perf_counter()

    assert json_query(data, "rows | unique") == [{"a": 1}, [1], 1, "1"]
    assert len(json_query(data, "big | unique")) == 100_000
    assert time.perf_counter() - started < 2.0
