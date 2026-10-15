"""Reference validators DF-V-001..012, and the validators, digest and fit check on the real benchmark files."""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import pytest

from _helpers.data_fixtures import ORDERS_CSV, REPO, computed, write_text
from _helpers.data_profiles import column, digest_blocks
from awa.blueprints import Blueprint
from awa.blueprints._data_project import check_project, check_project_data
from awa.blueprints._fit import fit_check
from awa.data import (
    DataProfile,
    DataRef,
    Privacy,
    check_case_files,
    check_instruction_text,
    check_outputs,
    check_references,
    check_scan_route,
    grounded,
    profile,
    reads_pdf_text,
    render_for_model,
    routes_scans,
)
from awa.data._model import FileProfile, NumberFormat, PdfInfo, SheetInfo, TableProfile, XlsxInfo
from awa.data._render import estimate_tokens


def codes(issues: list) -> list[str]:  # type: ignore[type-arg]
    return [issue.code for issue in issues]


def workbook() -> DataProfile:
    """The invoice workbook of the document-intake benchmark: header-less Kopf, table Leistungen."""

    kopf = TableProfile(
        id="f2.t1",
        sheet="Kopf",
        header_row=0,
        header_confidence=0.9,
        columns=[column("A"), column("B")],
    )
    leistungen = TableProfile(
        id="f2.t2",
        sheet="Leistungen",
        header_row=1,
        columns=[column("Pos"), column("Betrag", type="decimal", format=NumberFormat(decimal=","))],
    )
    sheets = [
        SheetInfo(name="Kopf"),
        SheetInfo(name="Leistungen"),
        SheetInfo(name="Notizen", visible=False),
    ]
    xlsx = FileProfile(
        id="f2",
        path="inbox/ln-2024-17_meyer.xlsx",
        sha256="0" * 64,
        size=1,
        kind="xlsx",
        tables=[kopf, leistungen],
        xlsx=XlsxInfo(sheets=sheets),
    )
    scan = FileProfile(
        id="f3",
        path="inbox/scan_0001.pdf",
        sha256="0" * 64,
        size=1,
        kind="pdf",
        pdf=PdfInfo(pages=1, document_class="scanned"),
    )
    mixed = FileProfile(
        id="f4",
        path="inbox/mixed.pdf",
        sha256="0" * 64,
        size=1,
        kind="pdf",
        pdf=PdfInfo(pages=2, document_class="mixed"),
    )
    born = FileProfile(
        id="f5",
        path="inbox/2024-0412_mueller.pdf",
        sha256="0" * 64,
        size=1,
        kind="pdf",
        pdf=PdfInfo(pages=1, document_class="born_digital"),
    )
    orders = profile(ORDERS_CSV)
    files = [orders.files[0].model_copy(update={"id": "f1"}), xlsx, scan, mixed, born]
    return DataProfile(awa_version="1.2.0", created="x", files=files)


# ------------------------------------------------------------------ V001, V002, V003, V005


def test_a_path_with_many_double_stars_is_matched_quickly() -> None:
    from awa.data._validate import path_matches

    pattern = "a/" + "**/" * 12 + "z.csv"
    path = "a/" + "b/" * 25 + "c.csv"
    started = time.perf_counter()

    assert not path_matches(pattern, path)
    assert path_matches(pattern, "a/b/c/z.csv") and path_matches("data/**/*.csv", "data/x/y.csv")
    assert time.perf_counter() - started < 1.0


def test_an_unknown_file_is_v001_with_the_closest_name() -> None:
    p = profile(ORDERS_CSV)

    (issue,) = check_references(p, [DataRef(file="order.csv")])

    assert (issue.code, issue.severity, issue.path) == ("DF-V-001", "error", "refs[0].file")
    assert issue.suggestion == "orders.csv" and "'orders.csv'" in issue.message


@pytest.mark.parametrize(
    "name", ["orders.csv", "ORDERS.CSV", "f1", "data/files/orders.csv", "./orders.csv"]
)
def test_a_file_is_found_by_id_name_or_a_longer_path(name: str) -> None:
    assert check_references(profile(ORDERS_CSV), [DataRef(file=name)]) == []


def test_a_file_that_could_not_be_read_cannot_be_used() -> None:
    p = profile(ORDERS_CSV)
    p.files[0].status = "unsupported"

    (issue,) = check_references(p, [DataRef(file="orders.csv", columns=["Land"])])

    assert issue.code == "DF-V-001" and "cannot be used" in issue.message


def test_an_unknown_column_is_v003_with_did_you_mean_and_the_real_columns() -> None:
    p = profile(ORDERS_CSV)
    refs = [DataRef(file="orders.csv", columns=["Betrag", "Land", "Amount", "betrag eur"])]

    amount, plain = check_references(p, refs)

    assert (amount.code, amount.path, amount.suggestion) == (
        "DF-V-003",
        "refs[0].columns[0]",
        "Betrag_EUR",
    )
    assert "Bestellnr" in amount.message and "orders.csv" in amount.message
    assert plain.path == "refs[0].columns[2]" and plain.suggestion is None  # nothing is close to it


def test_columns_of_a_file_without_a_table_are_v003() -> None:
    (issue,) = check_references(workbook(), [DataRef(file="scan_0001.pdf", columns=["Betrag"])])

    assert issue.code == "DF-V-003" and "without columns" in issue.message


def test_the_sheets_of_a_workbook_are_checked_exactly() -> None:
    p = workbook()

    assert check_references(p, [DataRef(file="ln-2024-17_meyer.xlsx", sheet="Kopf")]) == []
    (typo,) = check_references(p, [DataRef(file="ln-2024-17_meyer.xlsx", sheet="Leistung")])
    assert (typo.code, typo.suggestion, typo.path) == ("DF-V-002", "Leistungen", "refs[0].sheet")
    (case,) = check_references(p, [DataRef(file="ln-2024-17_meyer.xlsx", sheet="kopf")])
    assert case.suggestion == "Kopf" and "exact name" in case.message  # read_excel needs it exactly
    (csv,) = check_references(p, [DataRef(file="orders.csv", sheet="Orders")])
    assert csv.code == "DF-V-002" and "has none" in csv.message


def test_columns_are_looked_up_in_the_named_sheet_only() -> None:
    p = workbook()
    ok = DataRef(file="ln-2024-17_meyer.xlsx", sheet="Leistungen", columns=["Pos", "betrag"])
    wrong_sheet = DataRef(file="ln-2024-17_meyer.xlsx", sheet="Kopf", columns=["Betrag"])

    assert check_references(p, [ok]) == []
    assert codes(check_references(p, [wrong_sheet])) == ["DF-V-003"]


def test_a_header_row_that_differs_from_the_profile_is_v005() -> None:
    p = workbook()
    wrong = DataRef(file="ln-2024-17_meyer.xlsx", sheet="Kopf", header_row=1)
    right = DataRef(file="ln-2024-17_meyer.xlsx", sheet="Kopf", header_row=0)
    other = DataRef(file="ln-2024-17_meyer.xlsx", sheet="Leistungen", header_row=3)

    first, second = check_references(p, [wrong, right, other])[0], check_references(p, [other])[0]

    assert (first.code, first.path, first.suggestion) == ("DF-V-005", "refs[0].header_row", "0")
    assert "no header" in first.message and "confidence 0.90" in first.message
    assert second.code == "DF-V-005" and "row 1" in second.message
    assert check_references(p, [right]) == []


def test_the_location_prefix_can_be_chosen() -> None:
    (issue,) = check_references(profile(ORDERS_CSV), [DataRef(file="x")], where="data[2]")

    assert issue.path == "data[2][0].file"


# ------------------------------------------------------------------ V006


def test_reading_scans_as_text_without_a_route_is_v006() -> None:
    p = workbook()

    (issue, mixed) = check_scan_route(p, None, routed=False, path="agents[0].instructions")

    assert (issue.code, issue.severity) == (
        "DF-V-006",
        "error",
    ) and "scan_0001.pdf" in issue.message
    assert (mixed.severity, "mixed.pdf" in mixed.message) == ("warning", True)
    assert "2024-0412_mueller.pdf" not in issue.message + mixed.message  # born digital: fine
    assert check_scan_route(p, None, routed=True, path="x") == []
    only_born = check_scan_route(
        p, ["inbox/2024-0412_mueller.pdf", "orders.csv", "unknown.pdf"], routed=False, path="x"
    )
    assert only_born == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("A scan has no text layer: hold it for manual entry", True),
        ("Use OCR for scanned pages", True),
        ("Send them to vision", True),
        ("Escalate unreadable documents to a clerk", True),
        ("Extract the invoice number and total from every PDF", False),
        ("Scan the folder every night", False),
    ],
)
def test_the_wording_of_a_scan_route(text: str, expected: bool) -> None:
    assert routes_scans(text) is expected


def test_tools_that_read_pdf_text() -> None:
    assert reads_pdf_text(["validate", "builtin:extract_pdf_text"])
    assert reads_pdf_text(["connector:files.read_pdf"])
    assert not reads_pdf_text(["builtin:read_excel", "extract_docx_text"])


# ------------------------------------------------------------------ V009, V012


def test_a_case_with_an_unknown_input_file_is_v009() -> None:
    p = profile(ORDERS_CSV)

    (issue,) = check_case_files(p, {"cases[2]": ["orders.csv", "ordres.csv"]})

    assert (issue.code, issue.path, issue.suggestion) == (
        "DF-V-009",
        "cases[2].files[1]",
        "orders.csv",
    )
    assert check_case_files(p, {"cases[0]": ["orders.csv"]}) == []


def test_an_output_that_is_an_input_file_is_v012() -> None:
    p = workbook()

    got = check_outputs(
        p,
        {
            "a": "inbox/scan_0001.pdf",
            "b": "data/files/inbox/*.xlsx",
            "c": "data/output/report.csv",
            "d": "inbox",
            "e": "orders.csv",
        },
    )

    assert [(i.path, i.code) for i in got] == [
        ("a", "DF-V-012"),
        ("b", "DF-V-012"),
        ("d", "DF-V-012"),
        ("e", "DF-V-012"),
    ]
    assert "never overwrite" in got[0].message


# ------------------------------------------------------------------ V011


BENCHMARK_TEXT = (
    "Read it: extract_pdf_text for .pdf, read_excel for .xlsx (an invoice workbook has the sheets "
    '"Kopf" (header, read it with header_row 0) and "Leistungen"). The documents are German.'
)


def test_the_handwritten_benchmark_instructions_agree_with_the_profile() -> None:
    assert check_instruction_text(workbook(), BENCHMARK_TEXT, path="agents[0].instructions") == []


def test_a_wrong_header_row_in_instructions_is_v011() -> None:
    text = BENCHMARK_TEXT.replace("header_row 0", "header_row=1")

    (issue,) = check_instruction_text(workbook(), text, path="agents[0].instructions")

    assert (issue.code, issue.severity, issue.suggestion) == ("DF-V-011", "error", "0")
    assert (
        "sheet 'Kopf' of 'inbox/ln-2024-17_meyer.xlsx'" in issue.message
        and "no header row" in issue.message
    )


def test_the_header_row_is_compared_with_the_sheet_named_right_before_it() -> None:
    text = (
        "For Leistungen use header_row=1; for Kopf use header_row: 0. Then Leistungen header_row=4."
    )

    (issue,) = check_instruction_text(workbook(), text, path="p")

    assert "header_row 4 for sheet 'Leistungen'" in issue.message and "row 1" in issue.message


def test_a_header_row_without_a_named_sheet_or_file_is_not_judged() -> None:
    assert check_instruction_text(workbook(), "use header_row=7 for the table", path="p") == []


def test_an_unknown_sheet_or_path_in_instructions() -> None:
    text = "call read_excel with sheet='Leistung' and path='inbox/ln-2024-17_meyer.xlsx'; also path=\"data/vendors.csv\""

    sheet, path = check_instruction_text(workbook(), text, path="agents[1].instructions")

    assert (sheet.code, sheet.severity, sheet.suggestion) == ("DF-V-011", "error", "Leistungen")
    assert (path.code, path.severity) == ("DF-V-011", "warning") and "vendors.csv" in path.message
    assert (
        check_instruction_text(workbook(), "write path=out/report.txt and sheet=Kopf", path="p")
        == []
    )


# ------------------------------------------------------------------ real files


def test_a_designer_draft_against_the_real_orders_file() -> None:
    p = profile(ORDERS_CSV)
    draft = [
        DataRef(
            file="orders.csv", columns=["Bestellnr", "Betrag", "Wunschliefertermin"], header_row=1
        )
    ]

    (issue,) = check_references(p, draft)

    assert issue.suggestion == "Betrag_EUR" and issue.path == "refs[0].columns[1]"


def test_the_repo_files_are_known_to_the_validators(tmp_path: Path) -> None:
    folder = REPO / "examples" / "benchmarks" / "core" / "04_document_intake" / "data"
    p = profile(folder / "vendors.csv")
    refs = [DataRef(file="vendors.csv", columns=["Lieferantennr", "IBAN", "Bestelllimit"])]

    (issue,) = check_references(p, refs)

    assert issue.suggestion == "Bestelllimit_EUR"
    write_text(tmp_path, "x.csv", "a;b\r\n1;2\r\n", "cp1252")  # a second profile does not interfere
    assert (
        check_references(profile(tmp_path / "x.csv"), [DataRef(file="x.csv", columns=["a"])]) == []
    )


# ------------------------------------------------------------------ JSON fields and database tables


def shop_json() -> DataProfile:
    from awa.data._model import JsonField, JsonInfo, SqliteInfo

    orders = FileProfile(
        id="f1",
        path="shop_orders.jsonl",
        sha256="0" * 64,
        size=1,
        kind="json",
        json_doc=JsonInfo(
            top_level="jsonl",
            records=200,
            paths=[
                JsonField(path="id"),
                JsonField(path="deliveryAddress", optional_share=0.015),
                JsonField(path="lines[].sku"),
                JsonField(path="createdAt", format="epoch_ms"),
            ],
        ),
    )
    db = FileProfile(
        id="f2",
        path="o2c.db",
        sha256="0" * 64,
        size=1,
        kind="sqlite",
        sqlite=SqliteInfo(),
        tables=[
            TableProfile(
                id="f2.t1", name="orders", columns=[column("order_id"), column("customer_id")]
            ),
            TableProfile(id="f2.t2", name="customers", columns=[column("id"), column("name")]),
        ],
    )
    return DataProfile(awa_version="1", created="x", files=[orders, db])


def test_a_typo_in_a_json_field_is_v003_with_the_real_path() -> None:
    p = shop_json()

    typo, nope = check_references(
        p, [DataRef(file="shop_orders.jsonl", columns=["deliveryAdress", "nope"])]
    )

    assert (typo.code, typo.path, typo.suggestion) == (
        "DF-V-003",
        "refs[0].columns[0]",
        "deliveryAddress",
    )
    assert "field 'deliveryAdress'" in typo.message and "'lines[].sku'" in typo.message
    assert nope.suggestion is None


def test_json_fields_are_named_by_path_or_by_their_last_part() -> None:
    p = shop_json()
    names = ["deliveryAddress", "DeliveryAddress", "lines[].sku", "sku", "createdAt", "id"]

    assert check_references(p, [DataRef(file="shop_orders.jsonl", columns=names)]) == []
    snake = check_references(p, [DataRef(file="shop_orders.jsonl", columns=["created_at"])])
    # tools need it exactly
    assert [(i.code, i.suggestion) for i in snake] == [("DF-V-003", "createdAt")]


def test_json_files_have_no_sheet_and_other_documents_no_columns() -> None:
    p = shop_json()

    (sheet,) = check_references(p, [DataRef(file="shop_orders.jsonl", sheet="Orders")])
    (doc,) = check_references(workbook(), [DataRef(file="scan_0001.pdf", columns=["x"])])

    assert sheet.code == "DF-V-002" and doc.code == "DF-V-003" and "without columns" in doc.message


def test_the_tables_of_a_database_are_its_sheets() -> None:
    p = shop_json()

    ok = check_references(p, [DataRef(file="o2c.db", sheet="orders", columns=["order_id"])])
    wrong_table = check_references(
        p, [DataRef(file="o2c.db", sheet="customers", columns=["order_id"])]
    )
    typo_table = check_references(p, [DataRef(file="o2c.db", sheet="order")])
    anywhere = check_references(p, [DataRef(file="o2c.db", columns=["name", "order_id"])])

    assert ok == [] and anywhere == []
    assert [i.code for i in wrong_table] == ["DF-V-003"]
    assert (typo_table[0].code, typo_table[0].suggestion) == ("DF-V-002", "orders")


def test_a_reference_computation_over_a_file_without_rows_says_so() -> None:
    from awa.data import ReferenceCheck, check_reference_values

    check = ReferenceCheck(path="c", file="scan_0001.pdf", expression="count()", expected="1")

    (issue,) = check_reference_values(workbook(), [check])

    assert issue.code == "DF-V-010" and "has no rows to compute over" in issue.message


PATH = "agents[0].instructions"


# ------------------------------------------------------------------ instructions: header_row after ';'


def test_a_header_row_after_a_semicolon_is_compared_with_the_file_named_before_it() -> None:
    text = "Read the export with read_excel path='inbox/ln-2024-17_meyer.xlsx'; header_row=1; then book it."

    (issue,) = check_instruction_text(workbook(), text, path=PATH)

    assert issue.code == "DF-V-011" and "header_row 1" in issue.message
    assert "inbox/ln-2024-17_meyer.xlsx" in issue.message and issue.suggestion == "0"


def test_the_structure_of_the_text_decides_not_its_punctuation() -> None:
    for glue in ("; ", ". ", ", ", " and ", "\n", " (", " -> "):
        text = f"sheet 'Kopf'{glue}header_row=1"

        (issue,) = check_instruction_text(workbook(), text, path=PATH)

        assert "sheet 'Kopf'" in issue.message, glue


def test_a_file_name_stands_for_the_first_table_of_the_file_not_the_last() -> None:
    # the workbook has Kopf (header row 0) first and Leistungen (header row 1) last
    assert (
        check_instruction_text(workbook(), "ln-2024-17_meyer.xlsx with header_row=0", path=PATH)
        == []
    )
    (issue,) = check_instruction_text(
        workbook(), "ln-2024-17_meyer.xlsx with header_row=1", path=PATH
    )
    assert "sheet 'Kopf'" in issue.message


def test_a_header_row_binds_to_the_nearest_mention_and_each_mention_serves_one_header_row() -> None:
    both = "Kopf: header_row=0, Leistungen: header_row=1, then Kopf: header_row=1"
    unbound = "Kopf header_row=0 and again header_row=7"
    far = "Kopf is the first sheet." + " Some words." * 30 + " Use header_row=5."
    other_paragraph = "Kopf is the first sheet.\n\nUse header_row=5 for the table."

    (issue,) = check_instruction_text(workbook(), both, path=PATH)
    assert "header_row 1 for sheet 'Kopf'" in issue.message  # only the third is wrong
    assert check_instruction_text(workbook(), unbound, path=PATH) == []  # the 7 has no mention left
    assert check_instruction_text(workbook(), far, path=PATH) == []  # too far away to mean it
    assert check_instruction_text(workbook(), other_paragraph, path=PATH) == []


EXAMPLE = REPO / "examples" / "benchmarks" / "core" / "04_document_intake"
INBOX = EXAMPLE / "data"
WORKBOOK = "ln-2024-17_meyer.xlsx"


@pytest.fixture(scope="module")
def intake() -> DataProfile:
    return profile(INBOX)


def test_the_header_less_sheet_of_the_real_workbook_is_v005_when_a_header_row_is_claimed(
    intake: DataProfile,
) -> None:
    wrong = DataRef(file=WORKBOOK, sheet="Kopf", header_row=1)
    right = DataRef(file=WORKBOOK, sheet="Kopf", header_row=0)
    leistungen = DataRef(
        file=WORKBOOK, sheet="Leistungen", header_row=1, columns=["Datum", "Betrag_EUR"]
    )

    (issue,) = check_references(intake, [wrong])

    assert issue.code == "DF-V-005" and issue.suggestion == "0" and "no header" in issue.message
    assert check_references(intake, [right, leistungen]) == []
    typo = DataRef(file=WORKBOOK, sheet="Leistungen", columns=["Betrag"])
    assert check_references(intake, [typo])[0].suggestion == "Betrag_EUR"


def test_the_handwritten_instructions_of_the_benchmark_agree_with_the_real_profile(
    intake: DataProfile,
) -> None:
    blueprint = Blueprint.from_yaml_file(EXAMPLE / "blueprint.yaml")
    text = blueprint.agents[0].instructions

    assert check_instruction_text(intake, text, path="agents[0].instructions") == []
    wrong = text.replace("header_row 0", "header_row 1")
    (issue,) = check_instruction_text(intake, wrong, path="agents[0].instructions")
    assert issue.code == "DF-V-011" and "header_row 1" in issue.message


def test_only_the_scanned_pdf_has_no_text_layer(intake: DataProfile) -> None:
    (issue,) = check_scan_route(intake, None, routed=False, path="p")

    assert issue.code == "DF-V-006" and "scan_0001.pdf" in issue.message
    assert "2024-0412_mueller.pdf" not in issue.message
    assert check_scan_route(intake, ["inbox/2024-0412_mueller.pdf"], routed=False, path="p") == []


def test_values_of_the_real_documents_are_grounded_and_invented_ones_are_not(
    intake: DataProfile,
) -> None:
    found = [
        "2024-0412",
        "LN-2024-17",
        "1.428,00",
        "1428",
        "DE811111111",
        "V-100",
        "Zürcher",
        "PO-4500",
    ]

    assert [v for v in found if not grounded(v, intake)] == []
    assert [v for v in ("2024-9999", "LN-2099-1", "PO-9999") if grounded(v, intake)] == []


def test_a_reference_computation_over_a_real_sheet_skips_the_totals_rows(
    intake: DataProfile,
) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    sheet = openpyxl.load_workbook(INBOX / "inbox" / WORKBOOK, data_only=True)["Leistungen"]
    # the work lines are the rows that start with a date; the rows below them are totals
    rows = [r for r in sheet.iter_rows(min_row=2, values_only=True) if isinstance(r[0], datetime)]
    hours = [r[2] for r in rows]

    def value(expression: str) -> object:
        return computed(expression, intake, file=WORKBOOK, sheet="Leistungen")

    table = intake.find_table(WORKBOOK, "Leistungen")
    assert table is not None and table.totals_rows
    assert value("count()") == table.rows
    assert value("sum(Stunden)") == pytest.approx(sum(hours[: table.rows]))
    assert value("max(Betrag_EUR)") == pytest.approx(max(r[4] for r in rows[: table.rows]))


def test_the_digest_of_the_whole_folder_fits_the_default_budget_and_shows_each_kind(
    intake: DataProfile,
) -> None:
    text = render_for_model(intake)

    shown = dict(digest_blocks(text))
    assert estimate_tokens(text) <= 6000 and set(shown) >= {"inventory", "f1", "f2", "f3", "f6"}
    assert any("no header row" in line and "Kopf" in line for line in shown["f3"])
    assert any("no text layer: needs OCR or manual entry" in line for line in shown.get("f4", []))
    assert not any("source" in line.lower() for line in text.splitlines())
    masked = render_for_model(intake, privacy=Privacy(mask="placeholders"))
    assert "DE02100100100000123456" not in masked and "DE44500105175407324931" not in masked
    assert "<IBAN_1>" in masked


def test_a_data_section_for_the_real_example_is_valid_and_a_broken_one_is_found(
    tmp_path: Path,
) -> None:
    shutil.copytree(EXAMPLE, tmp_path / "p")
    path = tmp_path / "p" / "blueprint.yaml"
    section = dedent(
        """
        data:
          - name: vendors
            path: data/vendors.csv
            role: reference
            expect: {columns: [Lieferantennr, Name, IBAN, Bestelllimit_EUR], key: [Lieferantennr], decimal: ","}
          - name: meyer
            path: data/inbox/ln-2024-17_meyer.xlsx
            expect: {sheet: Kopf, header_row: 0}
          - name: mueller
            path: data/inbox/2024-0412_mueller.pdf
          - name: scan
            path: data/inbox/scan_0001.pdf
          - name: zuercher
            path: data/inbox/88231_zuercher.docx
          - name: orders
            path: data/purchase_orders.json
            role: reference
        """
    )
    path.write_text(path.read_text(encoding="utf-8") + section, encoding="utf-8")

    issues = check_project_data(path)

    assert [i for i in issues if i.severity == "error"] == []
    # the intake clerk reads PDFs and says a scan is held: no V006
    assert not any("DF-V-006" in i.message for i in issues)
    path.write_text(
        path.read_text(encoding="utf-8").replace("header_row: 0", "header_row: 1"), encoding="utf-8"
    )
    (broken,) = [i for i in check_project_data(path) if i.severity == "error"]
    assert broken.path == "data[1].expect.header_row" and "[DF-V-005]" in broken.message
    blueprint = Blueprint.from_yaml_file(path)
    assert [i.severity for i in check_project(blueprint)].count("error") == 1


async def test_the_fit_check_of_the_real_folder(intake: DataProfile) -> None:
    fits = await fit_check(
        "Supplier invoices arrive as PDF, Word or Excel. Check each one against the vendor "
        "master and the purchase orders, book the clean ones and hold the rest.",
        intake,
    )
    cvs = await fit_check("Screen the incoming CVs and schedule interviews.", intake)

    assert (fits.verdict, fits.model_calls) == ("ok", 0)
    assert cvs.verdict == "question" and cvs.questions[0].evidence


def test_a_column_name_of_punctuation_is_no_field_of_an_array_path() -> None:
    from awa.data._model import JsonField, JsonInfo

    tags = FileProfile(
        id="f1",
        path="tags.json",
        sha256="0" * 64,
        size=1,
        kind="json",
        json_doc=JsonInfo(top_level="array", records=2, paths=[JsonField(path="tags[]")]),
    )
    profile = DataProfile(awa_version="1", created="x", files=[tags])

    [issue] = check_references(profile, [DataRef(file="tags.json", columns=["--"])])

    assert issue.code == "DF-V-003"
