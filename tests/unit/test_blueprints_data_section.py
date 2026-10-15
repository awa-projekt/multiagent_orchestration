"""The blueprint's `data:` section: schema, round trip, the checks against real files and their untrusted inputs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from _helpers.data_fixtures import ORDERS_CSV, REPO, full_profile, write_text
from _helpers.data_profiles import ScriptedReader
from awa.blueprints import Blueprint, BlueprintError, BlueprintIssue
from awa.blueprints._check_data import check_data
from awa.blueprints._data_project import check_project, check_project_data
from awa.blueprints._schema_data import DataBlueprint
from awa.data import DataProfile, profile

AGENT = "agents:\n  clerk: Reads the orders.\n"
ORDERS = (
    "Bestellnr;Lieferant;Land;Betrag_EUR;Status\r\n"
    "B-1;Kowalski;PL;4.250,00;offen\r\n"
    "B-2;Müller;DE;5.980,50;offen\r\n"
    "B-3;Müller;DE;1.200,00;bezahlt\r\n"
)


def blueprint(data: str, agents: str = AGENT) -> Blueprint:
    return Blueprint.from_yaml_text(f"name: demo\n{agents}{dedent(data)}")


def project(tmp_path: Path, data: str, *, agents: str = AGENT, orders: str = ORDERS) -> Blueprint:
    write_text(tmp_path, "data/files/orders.csv", orders, "cp1252")
    return blueprint(data, agents)


def messages(issues: list[BlueprintIssue]) -> list[str]:
    return [f"{i.severity}:{i.path}:{i.message}" for i in issues]


ORDERS_ENTRY = """
data:
  - name: orders
    path: data/files/orders.csv
    role: input
    description: Purchase orders.
    expect:
      columns: [Bestellnr, Betrag_EUR]
      key: [Bestellnr]
      decimal: ","
      min_rows: 3
"""


def test_a_data_section_round_trips_and_defaults_to_nothing() -> None:
    bp = blueprint(ORDERS_ENTRY)

    (entry,) = bp.data
    assert (entry.name, entry.role, entry.expect.key, entry.expect.decimal) == (
        "orders",
        "input",
        ["Bestellnr"],
        ",",
    )
    assert Blueprint.from_yaml_text(bp.to_yaml()) == bp
    assert "data" in bp.to_dict()
    assert "data" not in blueprint("").to_dict()  # older blueprints are written unchanged
    assert blueprint("").data == [] and blueprint("").check() == []


@pytest.mark.parametrize(
    "entry, fragment",
    [
        ("name: Bank Lines\n    path: a.csv", "snake_case"),
        ("name: a\n    path: /etc/passwd", "inside the project folder"),
        ("name: a\n    path: C:\\x\\a.csv", "inside the project folder"),
        ("name: a\n    path: data/../../secret.csv", "inside the project folder"),
        ("name: a\n    path: a.csv\n    role: sink", "input"),
        ("name: a\n    path: a.csv\n    colour: red", "colour"),
        ("name: a\n    path: a.csv\n    expect: {decimal: ';'}", "decimal"),
        ("name: a\n    path: a.csv\n    expect: {header_row: -1}", "header_row"),
        ("name: a\n    path: a.csv\n    description: " + "x" * 301, "300"),
    ],
)
def test_an_invalid_data_entry_is_refused_when_the_blueprint_is_read(
    entry: str, fragment: str
) -> None:
    with pytest.raises(BlueprintError) as error:
        blueprint(f"\ndata:\n  - {entry}\n")

    assert fragment in str(error.value)


def test_a_file_stem_that_starts_with_a_digit_is_a_valid_name() -> None:
    bp = blueprint(
        "data:\n  - {name: 2024_0412_mueller, path: 'data/files/2024-0412_mueller.pdf'}\n"
    )

    assert bp.data[0].name == "2024_0412_mueller"


def test_the_blueprint_json_schema_describes_the_data_section() -> None:
    schema = Blueprint.model_json_schema()

    entry = schema["$defs"]["DataBlueprint"]
    assert set(entry["properties"]) == {
        "name",
        "path",
        "role",
        "description",
        "expect",
        "handling",
        "request",
        "mask",
        "digest_tokens",
    }
    assert entry["properties"]["role"]["enum"] == ["input", "reference", "template", "output"]
    assert "DataExpect" in schema["$defs"] and schema["properties"]["data"]["type"] == "array"
    assert DataBlueprint.model_json_schema()["additionalProperties"] is False


def test_a_matching_section_has_no_issue(tmp_path: Path) -> None:
    assert check_project(project(tmp_path, ORDERS_ENTRY), tmp_path) == []
    matching = ORDERS_ENTRY.replace("min_rows: 3", "min_rows: 3\n      header_row: 1")
    assert check_project(project(tmp_path, matching), tmp_path) == []


def test_a_path_that_matches_no_file_is_an_error(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("orders.csv", "ordres.csv"))

    issue, stray = check_project(bp, tmp_path)

    assert (issue.severity, issue.path) == ("error", "data[0].path")
    assert "matches no file" in issue.message
    assert stray.severity == "warning" and "orders.csv" in stray.message  # nothing names it


def test_an_unknown_column_is_an_error_with_the_closest_name(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("Betrag_EUR]", "Betrag, Lieferant]"))

    (issue,) = check_project(bp, tmp_path)

    assert issue.path == "data[0].expect.columns[1]"
    assert "column 'Betrag' does not exist" in issue.message
    assert "did you mean 'Betrag_EUR'" in issue.message and "[DF-V-003]" in issue.message
    assert "Lieferant" in issue.message  # the real columns are listed


def test_the_spelling_of_a_column_may_differ_in_case_and_punctuation(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("Betrag_EUR]", "betrag eur]"))

    assert check_project(bp, tmp_path) == []


def test_an_unknown_key_column_points_at_the_key(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("key: [Bestellnr]", "key: [Bestell]"))

    (issue,) = check_project(bp, tmp_path)

    assert issue.path == "data[0].expect.key[0]" and "did you mean 'Bestellnr'" in issue.message


def test_a_key_that_is_not_unique_is_an_error(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("key: [Bestellnr]", "key: [Lieferant]"))

    (issue,) = check_project(bp, tmp_path)

    assert issue.path == "data[0].expect.key" and "not unique" in issue.message
    assert "2 distinct values in 3 rows" in issue.message


def test_a_composite_key_is_checked_against_the_rows(tmp_path: Path) -> None:
    ok = project(tmp_path, ORDERS_ENTRY.replace("key: [Bestellnr]", "key: [Lieferant, Status]"))
    assert check_project(ok, tmp_path) == []

    bad = project(tmp_path, ORDERS_ENTRY.replace("key: [Bestellnr]", "key: [Lieferant, Land]"))
    (issue,) = check_project(bad, tmp_path)
    assert "('Müller', 'DE') occurs more than once" in issue.message


def test_a_wrong_decimal_is_an_error_and_a_missing_one_a_warning(tmp_path: Path) -> None:
    wrong = project(tmp_path, ORDERS_ENTRY.replace('decimal: ","', 'decimal: "."'))
    (issue,) = check_project(wrong, tmp_path)
    assert issue.severity == "error" and issue.path == "data[0].expect.decimal"
    assert "'Betrag_EUR'" in issue.message and "4.250,00" in issue.message

    unstated = project(tmp_path, ORDERS_ENTRY.replace('      decimal: ","\n', ""))
    (warning,) = check_project(unstated, tmp_path)
    assert warning.severity == "warning" and "[DF-V-007]" in warning.message
    assert "Betrag_EUR" in warning.message


def test_min_rows_and_header_row_are_compared_with_the_profile(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("min_rows: 3", "min_rows: 4\n      header_row: 3"))

    got = messages(check_project(bp, tmp_path))

    assert any("has 3 rows but min_rows is 4" in m for m in got)
    assert any("data[0].expect.header_row" in m and "row 1" in m for m in got)


def test_a_sheet_on_a_csv_file_is_an_error(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("min_rows: 3", "sheet: Orders"))

    (issue,) = check_project(bp, tmp_path)

    assert issue.path == "data[0].expect.sheet" and "[DF-V-002]" in issue.message


def test_duplicate_names_are_an_error(tmp_path: Path) -> None:
    entry = ORDERS_ENTRY.split("data:\n")[1]
    bp = project(tmp_path, "data:\n" + entry + entry)

    got = messages(check_project(bp, tmp_path))

    assert any("data[1].name" in m and "duplicate data name 'orders'" in m for m in got)


def test_a_glob_checks_every_matched_file_and_caps_the_noise(tmp_path: Path) -> None:
    for number in range(30):
        write_text(tmp_path, f"data/files/in/{number}.csv", "a;b\r\n1;2\r\n", "cp1252")
    bp = blueprint(
        "data:\n  - {name: batch, path: 'data/files/in/*.csv', expect: {columns: [zzz]}}\n"
    )

    issues = check_project(bp, tmp_path)

    assert 8 <= len(issues) <= 9 and "more problems follow" in issues[-1].message
    assert all("column 'zzz' does not exist" in i.message for i in issues[:8])


def test_a_folder_path_means_every_file_below_it(tmp_path: Path) -> None:
    write_text(tmp_path, "data/files/in/a.csv", "a;b\r\n1;2\r\n", "cp1252")
    write_text(tmp_path, "data/files/in/sub/b.csv", "a;b\r\n1;2\r\n", "cp1252")
    bp = blueprint("data:\n  - {name: batch, path: data/files/in, expect: {columns: [a]}}\n")

    assert check_project(bp, tmp_path) == []
    bp = blueprint("data:\n  - {name: batch, path: data/files/in, expect: {columns: [c]}}\n")
    assert len(check_project(bp, tmp_path)) == 2


def test_the_output_must_be_inside_data_and_never_an_input(tmp_path: Path) -> None:
    out = ORDERS_ENTRY + "  - {name: results, path: data/output/results.csv, role: output}\n"
    assert check_project(project(tmp_path, out), tmp_path) == []  # does not exist yet: fine

    outside = ORDERS_ENTRY + "  - {name: results, path: results/out.csv, role: output}\n"
    (issue,) = check_project(project(tmp_path, outside), tmp_path)
    assert "must be inside the project's data/ folder" in issue.message

    same = ORDERS_ENTRY + "  - {name: results, path: data/files/orders.csv, role: output}\n"
    (clash,) = check_project(project(tmp_path, same), tmp_path)
    assert clash.path == "data[1].path" and "[DF-V-012]" in clash.message

    folder = ORDERS_ENTRY + "  - {name: results, path: data/files, role: output}\n"
    got = messages(check_project(project(tmp_path, folder), tmp_path))
    assert any("[DF-V-012]" in m for m in got)


def test_a_file_without_an_entry_is_a_warning(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY)
    write_text(tmp_path, "data/files/extra.csv", "a;b\r\n1;2\r\n", "cp1252")

    (issue,) = check_project(bp, tmp_path)

    assert issue.severity == "warning" and "data/files/extra.csv" in issue.message


def test_a_backticked_name_that_is_no_column_is_a_warning(tmp_path: Path) -> None:
    agents = (
        "agents:\n  clerk:\n    description: Reads orders.\n"
        "    instructions: |\n"
        "      Sum the column `Summe` and compare `Betrag_EUR` with `Betrag_Eur2`.\n"
        "      Use `Bestellnr` as the key and `orders.csv` as the source.\n"
    )
    bp = project(tmp_path, ORDERS_ENTRY, agents=agents)

    got = [i.message for i in check_project(bp, tmp_path)]

    assert got == [
        "`Betrag_Eur2` is not a column of any declared data file; did you mean 'Betrag_EUR'?",
        "`Summe` is not a column of any declared data file",
    ]


def test_data_profile_json_is_used_when_the_files_match_and_ignored_when_they_changed(
    tmp_path: Path,
) -> None:
    bp = project(tmp_path, ORDERS_ENTRY.replace("Betrag_EUR]", "Preis]"))
    saved = profile(tmp_path / "data" / "files", root=tmp_path)
    cached = saved.model_copy(deep=True)
    cached.files[0].tables[0].columns[3].name = "Preis"  # only the saved profile knows it
    (tmp_path / "data" / "PROFILE.json").write_text(cached.to_json(), encoding="utf-8")

    assert [i for i in check_project(bp, tmp_path) if i.severity == "error"] == []

    write_text(tmp_path, "data/files/orders.csv", ORDERS + "B-4;X;DE;1,00;offen\r\n", "cp1252")
    got = messages(check_project(bp, tmp_path))
    assert any(
        "data changed since the profile was written: 'data/files/orders.csv' differs from" in m
        for m in got
    )
    assert any("column 'Preis' does not exist" in m for m in got)  # profiled again


def test_an_unreadable_data_profile_is_a_warning_and_the_files_are_profiled(tmp_path: Path) -> None:
    bp = project(tmp_path, ORDERS_ENTRY)
    (tmp_path / "data" / "PROFILE.json").write_text('{"schema": "other/v9"}', encoding="utf-8")

    (issue,) = check_project(bp, tmp_path)

    assert issue.severity == "warning" and "PROFILE.json cannot be read" in issue.message


def test_a_given_profile_is_checked_without_files_on_disk() -> None:
    given = full_profile()
    bp = blueprint(
        """
        data:
          - name: bank
            path: data/files/file1.csv
            expect: {columns: [Betrag_EUR, Betrag], decimal: ','}
          - name: invoices
            path: data/files/file2.xlsx
            expect: {sheet: Rechnungen, header_row: 1}
          - name: lost
            path: data/files/nothing.csv
        """
    )

    got = messages(check_data(bp, given))

    assert len(got) == 3
    assert "data[0].expect.columns[1]" in got[0] and "did you mean 'Betrag_EUR'" in got[0]
    assert "data[1].expect.header_row" in got[1] and "row 4" in got[1] and "[DF-V-005]" in got[1]
    assert "data[2].path" in got[2] and "matches no file in the profile" in got[2]


def test_a_given_profile_ignores_the_files_on_disk(tmp_path: Path) -> None:
    write_text(tmp_path, "data/files/extra.csv", "a;b\r\n1;2\r\n", "cp1252")
    bp = blueprint("data:\n  - {name: bank, path: file1.csv}\n")

    assert check_data(bp, full_profile()) == []  # no stray-file warning


def test_a_column_the_profile_cannot_pin_down_does_not_contradict_the_declared_decimal() -> None:
    given = full_profile()
    # its format says "," but nothing proves it
    given.files[0].tables[0].columns[0].ambiguous = True
    bp = blueprint("data:\n  - {name: bank, path: file1.csv, expect: {decimal: '.'}}\n")

    assert check_data(bp, given) == []


def test_a_given_profile_reports_the_sheets_of_a_workbook() -> None:
    bp = blueprint("data:\n  - {name: inv, path: file2.xlsx, expect: {sheet: Rechnung}}\n")

    (issue,) = check_data(bp, full_profile())

    assert "no sheet 'Rechnung'" in issue.message and "did you mean 'Rechnungen'" in issue.message


def test_a_date_format_that_differs_is_an_error_and_an_ambiguous_one_is_not() -> None:
    given = full_profile()
    column = given.files[0].tables[0].columns[0]
    column.type, column.date_format, column.format = "date", "DD.MM.YYYY", None
    entry = "data:\n  - {name: bank, path: file1.csv, expect: {date_format: 'MM/DD/YYYY'}}\n"

    (issue,) = check_data(blueprint(entry), given)
    assert issue.path == "data[0].expect.date_format" and "'DD.MM.YYYY'" in issue.message

    column.ambiguous = True  # nothing rules the other reading out: the declaration decides
    assert check_data(blueprint(entry), given) == []

    unstated = "data:\n  - {name: bank, path: file1.csv, expect: {columns: [Betrag_EUR]}}\n"
    got = check_data(blueprint(unstated.replace("Betrag_EUR", column.name)), given)
    assert [i.severity for i in got] == ["warning"] and "[DF-V-007]" in got[0].message


def test_the_project_check_resolves_the_entries_relative_to_the_blueprint_file(
    tmp_path: Path,
) -> None:
    write_text(tmp_path, "data/files/orders.csv", ORDERS, "cp1252")
    broken = ORDERS_ENTRY.replace("Betrag_EUR]", "Betrag]")
    (tmp_path / "blueprint.yaml").write_text(f"name: demo\n{AGENT}{broken}", encoding="utf-8")

    bp = Blueprint.from_yaml_file(tmp_path / "blueprint.yaml")

    got = messages(check_project(bp))  # no folder given: the blueprint's own
    assert len(got) == 1 and "data[0].expect.columns[1]" in got[0]
    assert check_project_data(tmp_path)[0].path == "data[0].expect.columns[1]"
    assert check_project_data(tmp_path / "blueprint.yaml") == check_project_data(tmp_path)


def test_blueprint_check_checks_the_section_against_the_profile_it_is_given(tmp_path: Path) -> None:
    write_text(tmp_path, "data/files/orders.csv", ORDERS, "cp1252")
    broken = blueprint(ORDERS_ENTRY.replace("Betrag_EUR]", "Betrag]"))
    p = profile(tmp_path / "data" / "files", root=tmp_path)

    (issue,) = broken.check(data=p)

    assert issue.path == "data[0].expect.columns[1]" and "[DF-V-003]" in issue.message
    assert broken.check() == []  # without a profile the section is not checked: no file is read
    assert blueprint(ORDERS_ENTRY).check(data=p) == []


def two_column_profile(*paths: str) -> DataProfile:
    from awa.data._model import ColumnProfile, FileProfile, TableProfile

    columns = [ColumnProfile(name=n, key=n, index=i) for i, n in enumerate(["a", "b"])]
    files = [
        FileProfile(
            id=f"f{number}",
            path=path,
            sha256="0" * 64,
            size=1,
            kind="csv",
            tables=[TableProfile(id=f"f{number}.t1", columns=columns, rows=2)],
        )
        for number, path in enumerate(paths, 1)
    ]
    return DataProfile(awa_version="1", created="x", files=files)


def test_the_pure_check_reads_rows_only_through_the_reader_it_is_given() -> None:
    bp = blueprint("data:\n  - {name: t, path: data/files/t.csv, expect: {key: [a, b]}}\n")
    reader = ScriptedReader()

    (issue,) = check_data(bp, two_column_profile("data/files/t.csv"), reader=reader)

    assert reader.asked == ["data/files/t.csv"]
    assert "the combination ('A', 'x') occurs more than once" in issue.message


def test_a_glob_does_not_cross_folders_but_a_double_star_does() -> None:
    given = two_column_profile("data/files/a.csv", "data/files/sub/b.csv")

    def issues(path: str) -> list[str]:
        bp = blueprint(f"data:\n  - {{name: x, path: '{path}'}}\n")
        return [i.path for i in check_data(bp, given)]

    assert issues("data/files/*.csv") == [] and issues("data/files/**/b.csv") == []
    assert issues("data/files/*/a.csv") == ["data[0].path"]
    assert issues("data/files/a.csv") == [] and issues("data/files") == []


def test_check_project_data_without_a_section_or_blueprint(tmp_path: Path) -> None:
    (tmp_path / "blueprint.yaml").write_text(f"name: demo\n{AGENT}", encoding="utf-8")
    (issue,) = check_project_data(tmp_path)
    assert issue.severity == "warning" and "no `data:` section" in issue.message

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BlueprintError, match="holds no blueprint"):
        check_project_data(empty)


def test_a_real_example_blueprint_with_a_data_section(tmp_path: Path) -> None:
    source = REPO / "examples" / "benchmarks" / "core" / "04_document_intake"
    shutil.copytree(source, tmp_path / "p")
    path = tmp_path / "p" / "blueprint.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + dedent(
            """
            data:
              - name: vendors
                path: data/vendors.csv
                role: reference
                description: Vendor master data.
                expect:
                  columns: [Lieferantennr, Name, IBAN, Bestelllimit_EUR]
                  key: [Lieferantennr]
                  decimal: ","
            """
        ),
        encoding="utf-8",
    )

    assert check_project_data(path) == []

    path.write_text(path.read_text(encoding="utf-8").replace("Bestelllimit_EUR", "Bestelllimit"))
    (issue,) = check_project_data(path)
    assert "did you mean 'Bestelllimit_EUR'" in issue.message
    assert issue.path == "data[0].expect.columns[3]"


def test_the_orders_file_of_the_benchmark_agrees_with_its_section(tmp_path: Path) -> None:
    write_text(tmp_path, "data/files/orders.csv", ORDERS_CSV.read_text("cp1252"), "cp1252")
    bp = blueprint(
        """
        data:
          - name: orders
            path: data/files/orders.csv
            expect: {columns: [Bestellnr, Betrag_EUR, Status], key: [Bestellnr], decimal: ",", min_rows: 8}
        """
    )

    assert check_project(bp, tmp_path) == []


def test_a_glob_cannot_leave_the_project_folder(tmp_path: Path) -> None:
    outside = tmp_path / "secret"
    outside.mkdir()
    write_text(outside, "x.csv", "secret;value\r\n1;2\r\n", "cp1252")
    inner = tmp_path / "project"
    (inner / "data").mkdir(parents=True)
    link = inner / "data" / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):  # no privilege on Windows: a junction is a link too
        made = subprocess.run(  # noqa: S603 - fixed arguments, a test helper
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
        )
        if made.returncode != 0:
            pytest.skip("directory links are not available here")
    bp = blueprint("data:\n  - {name: leak, path: 'data/link/*.csv', expect: {columns: [zzz]}}\n")

    (issue,) = check_project(bp, inner)

    assert "matches no file" in issue.message and "secret" not in issue.message


def intake_profile() -> DataProfile:
    """The workbook and PDFs of the document-intake benchmark, as the profile describes them."""

    from awa.data._model import (
        ColumnProfile,
        FileProfile,
        PdfInfo,
        SheetInfo,
        TableProfile,
        XlsxInfo,
    )

    def col(name: str) -> ColumnProfile:
        return ColumnProfile(name=name, key=name.lower(), index=0)

    kopf = TableProfile(id="f1.t1", sheet="Kopf", header_row=0, columns=[col("A"), col("B")])
    workbook = FileProfile(
        id="f1",
        path="inbox/ln-2024-17_meyer.xlsx",
        sha256="0" * 64,
        size=1,
        kind="xlsx",
        tables=[kopf],
        xlsx=XlsxInfo(sheets=[SheetInfo(name="Kopf")]),
    )
    scan = FileProfile(
        id="f2",
        path="inbox/scan_0001.pdf",
        sha256="0" * 64,
        size=1,
        kind="pdf",
        pdf=PdfInfo(pages=1, document_class="scanned"),
    )
    return DataProfile(awa_version="1", created="x", files=[workbook, scan])


INBOX = "data:\n  - {name: inbox, path: 'inbox/*', role: input}\n"


def intake(instructions: str, tools: str = "[builtin:read_excel]", other: str = "") -> Blueprint:
    agents = f"agents:\n  clerk:\n    instructions: {instructions!r}\n    tools: {tools}\n{other}"
    return blueprint(INBOX, agents)


def test_a_wrong_header_row_in_the_instructions_is_an_error_at_the_agent() -> None:
    bp = intake('Read sheet "Kopf" of the workbook with header_row 1.')

    (issue,) = check_data(bp, intake_profile())

    assert (issue.severity, issue.path) == ("error", "agents[0].instructions")
    assert (
        "header_row 1" in issue.message
        and "no header row" in issue.message
        and "[DF-V-011]" in issue.message
    )
    assert check_data(intake('Read "Kopf" with header_row 0.'), intake_profile()) == []


def test_reading_pdf_text_without_a_scan_route_is_an_error() -> None:
    given = intake_profile()
    reader = intake("Extract the invoice number from every PDF.", "[builtin:extract_pdf_text]")

    (issue,) = check_data(reader, given)

    assert (issue.severity, issue.path) == ("error", "agents[0].instructions")
    assert "scan_0001.pdf" in issue.message and "[DF-V-006]" in issue.message


def test_a_scan_route_anywhere_in_the_blueprint_is_enough() -> None:
    given = intake_profile()
    routed = intake(
        "Extract the invoice number from every PDF.",
        "[builtin:extract_pdf_text]",
        other="  holder:\n    instructions: 'A scan has no text layer: hold it for manual entry.'\n",
    )
    no_pdf_tool = intake("Extract the invoice number from every PDF.", "[builtin:read_excel]")

    assert check_data(routed, given) == []
    assert check_data(no_pdf_tool, given) == []  # nothing assumes a text layer


def test_the_scan_check_applies_to_the_files_on_disk_too(tmp_path: Path) -> None:
    write_text(tmp_path, "data/files/a.csv", "a;b\r\n1;2\r\n", "cp1252")
    bp = blueprint(
        "data:\n  - {name: files, path: 'data/files/*'}\n",
        "agents:\n  clerk:\n    instructions: 'Read sheet=Rechnung with path=data/files/b.csv'\n",
    )

    got = [i.message for i in check_project(bp, tmp_path)]

    assert any("which is not one of the profiled files" in m for m in got)  # path=b.csv: a warning


def test_awa_validate_checks_the_data_section_against_the_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    write_text(tmp_path, "data/files/orders.csv", ORDERS, "cp1252")
    path = tmp_path / "blueprint.yaml"
    path.write_text(f"name: demo\n{AGENT}{ORDERS_ENTRY}", encoding="utf-8")

    assert main(["validate", str(path)]) == 0
    capsys.readouterr()

    path.write_text(
        path.read_text(encoding="utf-8").replace("Betrag_EUR]", "Betrag]"), encoding="utf-8"
    )
    assert main(["validate", str(path)]) == 1
    printed = capsys.readouterr().out
    assert "data[0].expect.columns[1]" in printed and "did you mean 'Betrag_EUR'" in printed


def test_the_source_path_inside_a_saved_profile_is_never_followed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_text(project, "data/files/t.csv", "a;b\r\n1;x\r\n1;x\r\n", "cp1252")  # duplicates
    outside = write_text(tmp_path, "elsewhere.csv", "a;b\r\n1;x\r\n2;y\r\n", "cp1252")  # unique
    saved = profile(project / "data" / "files", root=project)
    hostile = saved.model_copy(deep=True)
    hostile.files[0].source = str(outside)  # a profile file can say anything
    (project / "data" / "PROFILE.json").write_text(hostile.to_json(), encoding="utf-8")
    bp = blueprint("data:\n  - {name: t, path: data/files/t.csv, expect: {key: [a, b]}}\n")

    (issue,) = check_project(bp, project)

    assert "occurs more than once" in issue.message  # the real file was read, not the planted path


def test_a_blueprint_cannot_name_files_outside_its_folder() -> None:
    for path in (
        "../secret.csv",
        "data/../../x",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "data\\..\\x",
    ):
        with pytest.raises(BlueprintError):
            blueprint(f"data:\n  - {{name: leak, path: '{path}'}}\n")


def test_a_directory_link_that_leaves_the_project_is_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    write_text(secret, "x.csv", "token;value\r\nhunter2;1\r\n", "cp1252")
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    link = project / "data" / "link"
    try:
        os.symlink(secret, link, target_is_directory=True)
    except (OSError, NotImplementedError):  # no privilege on Windows: a junction is a link too
        made = subprocess.run(  # noqa: S603 - fixed arguments, a test helper
            ["cmd", "/c", "mklink", "/J", str(link), str(secret)], capture_output=True
        )
        if made.returncode != 0:
            pytest.skip("directory links are not available here")
    bp = blueprint("data:\n  - {name: leak, path: 'data/link', expect: {columns: [zzz]}}\n")

    issues = check_project(bp, project)

    assert [i.path for i in issues] == ["data[0].path"] and "hunter2" not in str(issues)


def test_entries_that_match_too_many_files_are_refused_without_profiling_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(12):
        write_text(tmp_path, f"data/files/{number}.csv", "a;b\r\n1;2\r\n", "cp1252")
    monkeypatch.setattr("awa.blueprints._data_project._MAX_FILES", 5)
    bp = blueprint("data:\n  - {name: batch, path: 'data/files/*.csv'}\n")

    (issue,) = check_project(bp, tmp_path)

    assert issue.severity == "error" and "more than 5 files" in issue.message


def test_a_saved_profile_that_is_not_a_profile_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    write_text(tmp_path, "data/files/t.csv", "a;b\r\n1;2\r\n", "cp1252")
    for text in ("not json", '{"schema": "awa.data/profile/v9"}', "[]", "\x00" * 10):
        (tmp_path / "data" / "PROFILE.json").write_text(text, encoding="utf-8")
        bp = blueprint("data:\n  - {name: t, path: data/files/t.csv}\n")

        (issue,) = check_project(bp, tmp_path)

        assert issue.severity == "warning" and "PROFILE.json cannot be read" in issue.message
