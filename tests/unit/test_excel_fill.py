"""`fill_excel`: new columns filled per row matched by a key, saved as a new workbook."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from awa._internal.upstream.tools import ToolException  # noqa: E402
from awa.toolkits import data_toolkit  # noqa: E402
from awa.toolkits._excel_fill import FillRow, fill_workbook, match_key  # noqa: E402


def register(path: Path, rows: int = 5000) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Register"
    sheet.append(["Invoice no", "Supplier", "Gross"])
    sheet["A1"].font = openpyxl.styles.Font(bold=True)
    sheet["C1"].font = openpyxl.styles.Font(bold=True)
    for number in range(1, rows + 1):
        sheet.append([f"INV-{number:05d}", f"Supplier {number % 7}", number * 1.5])
    sheet.append(["INV-00007", "duplicate", 1])
    notes = book.create_sheet("Notes")
    notes["A1"] = "keep me"
    book.save(path)


def test_fills_new_columns_by_key_and_keeps_the_source(tmp_path: Path) -> None:
    source, output = tmp_path / "register.xlsx", tmp_path / "out" / "enriched.xlsx"
    register(source)
    before = source.read_bytes()
    started = time.perf_counter()
    summary = fill_workbook(
        source,
        output,
        key_column="invoice NO",
        columns=["Invoice date", "Net"],
        rows=[
            FillRow("inv 00002", ["2026-03-01", 100.5]),
            FillRow("INV-00007", ["2026-03-02", None], review="net illegible"),
            FillRow("INV-99999", ["2026-03-03", 1.0]),
        ],
    )
    assert time.perf_counter() - started < 30
    assert source.read_bytes() == before
    assert summary["rows_matched"] == 2
    assert summary["unmatched_keys"] == ["INV-99999"]
    assert summary["keys_in_several_rows"] == ["INV-00007"]
    book = openpyxl.load_workbook(output)
    sheet = book["Register"]
    assert [c.value for c in sheet[1]] == [
        "Invoice no",
        "Supplier",
        "Gross",
        "Invoice date",
        "Net",
        "Review",
    ]
    assert sheet["D1"].font.bold
    assert sheet["D3"].value.date() == date(2026, 3, 1) and sheet["E3"].value == 100.5
    assert sheet["F8"].value == "net illegible; key INV-00007 is in 2 rows"
    assert sheet["F5002"].value is not None  # the duplicate row at the end
    assert book["Notes"]["A1"].value == "keep me"
    assert [c.value for c in book["Unmatched keys"][2]][:2] == ["INV-99999", "2026-03-03"]


def test_append_continues_and_refuses_source_columns(tmp_path: Path) -> None:
    source, output = tmp_path / "r.xlsx", tmp_path / "o.xlsx"
    register(source, rows=20)
    fill_workbook(
        source, output, key_column="Invoice no", columns=["Net"], rows=[FillRow("INV-00001", [1])]
    )
    fill_workbook(
        source,
        output,
        key_column="Invoice no",
        columns=["Net"],
        rows=[FillRow("INV-00002", [2])],
        append=True,
    )
    sheet = openpyxl.load_workbook(output).active
    assert (sheet["D2"].value, sheet["D3"].value) == (1, 2)
    with pytest.raises(ToolException, match="o.xlsx exists; set append"):
        fill_workbook(source, output, key_column="Invoice no", columns=["Net"], rows=[])
    with pytest.raises(ToolException, match="exist in the source"):
        fill_workbook(
            source, output, key_column="Invoice no", columns=["Gross"], rows=[], overwrite=True
        )
    with pytest.raises(ToolException, match="new file"):
        fill_workbook(source, source, key_column="Invoice no", columns=["Net"], rows=[])
    with pytest.raises(ToolException, match="values"):
        fill_workbook(
            source,
            output,
            key_column="Invoice no",
            columns=["Net"],
            rows=[FillRow("x", [])],
            overwrite=True,
        )
    fill_workbook(source, output, key_column="Invoice no", columns=["Net"], rows=[], overwrite=True)
    assert openpyxl.load_workbook(output).active["D2"].value is None  # started again


def test_formula_text_is_stored_as_text_and_keys_normalize(tmp_path: Path) -> None:
    source, output = tmp_path / "r.xlsx", tmp_path / "o.xlsx"
    register(source, rows=3)
    fill_workbook(
        source,
        output,
        key_column="Invoice no",
        columns=["Note"],
        rows=[FillRow("INV00001", ["=1+1"])],
    )
    cell = openpyxl.load_workbook(output).active["D2"]
    assert cell.value == "=1+1" and cell.data_type == "s"
    assert match_key(4711.0) == "4711" and match_key(" inv-0042/a ") == "INV0042A"


def test_a_zip_bomb_workbook_is_refused_before_it_is_loaded(tmp_path: Path) -> None:
    import zipfile

    source = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"\0" * 50_000_000)

    with pytest.raises(ToolException, match="unreasonable size"):
        fill_workbook(source, tmp_path / "o.xlsx", key_column="Key", columns=["New"], rows=[])


def test_no_written_text_becomes_a_formula(tmp_path: Path) -> None:
    source, output = tmp_path / "r.xlsx", tmp_path / "o.xlsx"
    register(source, rows=3)

    fill_workbook(
        source,
        output,
        key_column="Invoice no",
        columns=["=HYPERLINK(1)"],
        rows=[
            FillRow("INV-00001", ["x"], review="=cmd|'/c calc'!A1"),
            FillRow("@SUM(1)", ["+1+1"]),
        ],
    )

    book = openpyxl.load_workbook(output)
    written = [book.active["D1"], book.active["E2"], *book["Unmatched keys"][2]]
    assert [c.value for c in written][:4] == [
        "=HYPERLINK(1)",
        "=cmd|'/c calc'!A1",
        "@SUM(1)",
        "+1+1",
    ]
    assert all(c.data_type == "s" for c in written if isinstance(c.value, str))


def test_column_names_in_any_script_are_told_apart(tmp_path: Path) -> None:
    source, output = tmp_path / "r.xlsx", tmp_path / "o.xlsx"
    book = openpyxl.Workbook()
    book.active.append(["Номер", "Поставщик"])
    book.active.append(["A-1", "Иванов"])
    book.save(source)

    summary = fill_workbook(
        source, output, key_column="номер", columns=["Сумма"], rows=[FillRow("a-1", [5])]
    )

    assert summary["rows_matched"] == 1
    assert [c.value for c in openpyxl.load_workbook(output).active[1]][:3] == [
        "Номер",
        "Поставщик",
        "Сумма",
    ]


def test_the_tool_reads_from_sources_and_writes_into_its_root(tmp_path: Path) -> None:
    files, output = tmp_path / "files", tmp_path / "output"
    files.mkdir()
    output.mkdir()
    register(files / "register.xlsx", rows=3)
    [tool] = data_toolkit(
        output, include=["fill_excel"], requires_approval=False, source_root=files
    )
    result = tool.invoke(
        {
            "path": "register.xlsx",
            "output": "enriched.xlsx",
            "key_column": "Invoice no",
            "columns": ["Net"],
            "rows": [{"key": "INV-00003", "values": [9.5]}],
        }
    )
    assert json.loads(result[result.index("{") : result.rindex("}") + 1])["rows_matched"] == 1
    assert (output / "enriched.xlsx").is_file() and not (files / "enriched.xlsx").exists()
