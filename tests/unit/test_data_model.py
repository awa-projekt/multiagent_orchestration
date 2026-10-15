"""The data profile model, the portable schema and the reader ports of the public API."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

import awa.data
import awa.errors
from _helpers.data_fixtures import full_profile, write_text
from awa.data import (
    Claim,
    ColumnProfile,
    ColumnValues,
    DataError,
    DataProfile,
    DataReader,
    Limits,
    Privacy,
    RowLimitError,
    TableProfile,
    TooLargeError,
    TooManyFilesError,
    ValueSource,
    check_grounding,
    column_key,
    ingest,
    local_reader,
    make_flag,
    profile,
)
from awa.data._model import (
    Cell,
    DataIssue,
    DataRef,
    DocxInfo,
    DocxTable,
    FileProfile,
    Flag,
    Heading,
    JsonInfo,
    ListValidation,
    MailAttachment,
    MailInfo,
    PdfInfo,
    ProfileEvent,
    Relation,
    SheetInfo,
    SqliteInfo,
    XlsxInfo,
)
from awa.data._sampling import spread_indices
from awa.toolkits.data import _key

SCHEMA_FILE = (
    Path(__file__).resolve().parents[2] / "src" / "awa" / "data" / "profile.v1.schema.json"
)

# The names that are frozen for profile v1: removing or renaming one needs a new major version.
FROZEN_FIELDS = {
    DataProfile: "schema awa_version created limits privacy files relations flags summary",
    FileProfile: "id path source sha256 size kind detected_by status role_hint mtime text tables "
    "xlsx pdf docx mail json_doc sqlite children relations flags",
    TableProfile: "id sheet name range header_row header_confidence data_start_row rows rows_exact "
    "sampled rows_seen totals_rows columns key_candidates primary_key sample_rows",
    ColumnProfile: "name key index type declared_type nullable null_share distinct unique format "
    "date_format ambiguous unit min max top examples pii flags",
    Flag: "code severity message where",
    Relation: "from_column to_column kind overlap confidence",
    PdfInfo: "pages per_page document_class encrypted fonts_embedded form_fields metadata language "
    "excerpt key_values",
    DocxInfo: "paragraphs text_length headings tables headers_footers tracked_changes comments "
    "images language excerpt",
    MailInfo: "messages_total messages sampled",
    JsonInfo: "top_level records depth sampled paths key_variants",
    SqliteInfo: "foreign_keys views indexes",
    XlsxInfo: "sheets date_system defined_names macros",
    Limits: "max_files max_file_bytes max_total_bytes max_rows_exact sample_rows max_sheets "
    "max_pdf_pages_profiled max_pdf_pages max_chars_per_cell parse_timeout_s max_zip_entries "
    "max_zip_ratio",
    Privacy: "mask max_sample_rows max_cell_chars include_samples kinds",
    ProfileEvent: "file stage message path kind status flags",
    DataRef: "file sheet columns header_row",
    DataIssue: "code severity path message suggestion",
}


def _aliases(model: type) -> set[str]:
    return {field.alias or name for name, field in model.model_fields.items()}  # type: ignore[attr-defined]


@pytest.mark.parametrize("model", list(FROZEN_FIELDS), ids=lambda m: m.__name__)
def test_every_frozen_field_still_exists(model: type) -> None:
    missing = set(FROZEN_FIELDS[model].split()) - _aliases(model)

    assert missing == set(), f"{model.__name__} lost frozen fields {sorted(missing)}"


def test_the_profile_round_trips_through_json_with_every_block() -> None:
    original = full_profile()

    assert DataProfile.from_json(original.to_json()) == original
    assert DataProfile.from_json(original.to_json(indent=None).encode("utf-8")) == original


def test_the_json_uses_the_schema_key_and_validates_against_the_json_schema() -> None:
    document = json.loads(full_profile().to_json())

    assert document["schema"] == "awa.data/profile/v1"
    jsonschema.Draft202012Validator.check_schema(DataProfile.json_schema())
    jsonschema.validate(document, DataProfile.json_schema(), jsonschema.Draft202012Validator)


def test_the_schema_rejects_a_profile_with_a_wrong_value() -> None:
    document = json.loads(full_profile().to_json())
    document["files"][0]["tables"][0]["columns"][0]["type"] = "money"

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, DataProfile.json_schema(), jsonschema.Draft202012Validator)


def test_the_checked_in_schema_file_is_the_generated_one() -> None:
    assert json.loads(SCHEMA_FILE.read_text(encoding="utf-8")) == DataProfile.json_schema(), (
        "run: python scripts/gen_data_schema.py"
    )


def test_a_field_added_by_a_later_minor_version_is_ignored_when_reading() -> None:
    document = json.loads(full_profile().to_json())
    document["files"][0]["tables"][0]["columns"][0]["future_field"] = {"a": 1}
    document["future_top_level"] = True

    read = DataProfile.from_json(json.dumps(document))

    assert read == full_profile()


def test_code_building_a_model_cannot_misspell_a_field() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ColumnProfile(name="a", key="a", index=0, nmae="typo")  # type: ignore[call-arg]


def test_another_major_version_or_a_non_profile_is_refused_with_the_fix() -> None:
    document = json.loads(full_profile().to_json())
    document["schema"] = "awa.data/profile/v2"

    with pytest.raises(DataError, match="Profile the files again"):
        DataProfile.from_json(json.dumps(document))
    with pytest.raises(DataError, match="invalid JSON"):
        DataProfile.from_json("not json")
    with pytest.raises(DataError, match="schema None"):
        DataProfile.from_json("[]")


def test_the_summary_is_recomputed_from_the_files() -> None:
    profile = full_profile()
    document = json.loads(profile.to_json())
    document["summary"] = {"files": 99, "tables": 99, "rows": 99, "warnings": 99, "errors": 99}

    read = DataProfile.from_json(json.dumps(document))

    assert read.summary == profile.summary
    assert (profile.summary.files, profile.summary.tables) == (7, 3)
    assert profile.summary.rows == 417
    assert profile.summary.warnings == 1
    assert profile.summary.errors == 0


def test_a_failed_file_counts_as_an_error_in_the_summary() -> None:
    profile = full_profile()
    profile.files[0].status = "error"

    assert DataProfile.model_validate(profile.model_dump()).summary.errors == 1


def test_flag_severity_follows_the_code_family() -> None:
    assert make_flag("DF-I-NOTE", "m").severity == "info"
    assert make_flag("DF-W-DECIMAL-COMMA", "m").severity == "warning"
    assert make_flag("DF-E-MISSING-EXTRA", "m", "f1").severity == "error"
    for bad in ("DF-X-OTHER", "decimal", "DF-W-", "df-w-x"):
        with pytest.raises(DataError, match="DF-W-NAME"):
            make_flag(bad, "m")


def test_a_flag_cannot_be_built_with_a_malformed_code() -> None:
    with pytest.raises(ValidationError):
        Flag(code="oops", severity="info", message="m")


def test_data_issue_codes_have_the_validator_form() -> None:
    assert DataIssue(code="DF-V-003", path="f1", message="m").severity == "error"
    with pytest.raises(ValidationError):
        DataIssue(code="DF-V-3", path="f1", message="m")


def test_column_keys_are_the_keys_the_data_tools_use() -> None:
    for name in ["Unit Price", "Betrag_EUR", "  Date (UTC) ", "Straße", "A/B", "", "123"]:
        assert column_key(name) == _key(name)
    assert column_key("Unit Price") == "unit_price"


def test_lookups_find_a_file_by_id_path_or_base_name() -> None:
    profile = full_profile()

    assert profile.find_file("f3").path == "file3.pdf"  # type: ignore[union-attr]
    assert profile.find_file("FILE3.PDF").id == "f3"  # type: ignore[union-attr]
    assert profile.find_file("missing.csv") is None
    profile.files[1].path = "sub/file1.csv"
    profile.files[0].path = "file1.csv"
    assert profile.find_file("file1.csv").id == "f1"  # type: ignore[union-attr]
    profile.files[0].path = "other/file1.csv"
    assert profile.find_file("file1.csv") is None  # two files share the base name


def test_find_column_accepts_the_name_the_key_and_any_case() -> None:
    profile = full_profile()

    for ref in ("Betrag_EUR", "betrag_eur", "BETRAG EUR"):
        column = profile.find_column("file1.csv", ref)
        assert column is not None and column.type == "decimal"
    assert profile.find_column("file1.csv", "Amount") is None
    assert profile.find_column("nope.csv", "Betrag_EUR") is None
    assert profile.find_column("file2.xlsx", "x", sheet="rechnungen") is None
    assert profile.find_table("file2.xlsx", sheet="rechnungen").id == "f2.t1"  # type: ignore[union-attr]
    assert profile.find_table("file2.xlsx", sheet="Other") is None
    assert [(f.id, t.id) for f, t in profile.tables()] == [
        ("f1", "f1.t1"),
        ("f2", "f2.t1"),
        ("f8", "f8.t1"),
    ]


def test_invalid_limits_are_refused_when_created() -> None:
    for bad in ({"max_files": 0}, {"parse_timeout_s": 0}, {"max_zip_ratio": -1}):
        with pytest.raises(ValueError):
            Limits(**bad)
    assert Limits().max_rows_exact == 200_000
    assert Privacy().mask == "none"
    with pytest.raises(ValidationError):
        Privacy(mask="hide")  # type: ignore[arg-type]


def test_without_samples_removes_every_text_taken_from_the_files_and_keeps_the_structure() -> None:
    profile = full_profile()
    secret = "SECRET-VALUE"
    csv, _, pdf, docx, mail, data, _ = profile.files
    csv.tables[0].sample_rows = [[secret, 1]]
    csv.tables[0].columns[0].examples = [secret]
    csv.tables[0].columns[0].top[0].value = secret
    csv.text.head, csv.text.tail = [secret], [secret]  # type: ignore[union-attr]
    pdf.pdf.excerpt = secret  # type: ignore[union-attr]
    pdf.pdf.key_values[0].value = secret  # type: ignore[union-attr]
    pdf.pdf.metadata = {"title": secret}  # type: ignore[union-attr]
    docx.docx.excerpt = secret  # type: ignore[union-attr]
    docx.docx.headings = [Heading(level=1, text=secret)]  # type: ignore[union-attr]
    docx.docx.tables = [DocxTable(rows=2, columns=1, header=[secret])]  # type: ignore[union-attr]
    message = mail.mail.messages[0]  # type: ignore[union-attr]
    message.excerpt, message.headers = secret, {"subject": secret}
    message.attachments = [MailAttachment(name=f"{secret}.pdf")]
    mail.children[0].pdf.excerpt = secret  # type: ignore[union-attr]
    data.json_doc.paths[0].enum = [secret]  # type: ignore[union-attr]
    profile.relations[0].unmatched = [secret]
    csv.relations = [profile.relations[0].model_copy()]
    csv.tables[0].columns.append(
        ColumnProfile(name="Name", key="name", index=4, min=secret, max=secret)
    )
    csv.xlsx = XlsxInfo(
        sheets=[SheetInfo(name="S", validations=[ListValidation(range="A1", values=[secret])])]
    )
    csv.flags.append(
        make_flag(
            "DF-W-FORMULA-NO-CACHE",
            f"sheet 'S': 1 of 1 formulas have no saved result and read as empty (e.g. A1: ={secret}"
            "); open and save the workbook in Excel or LibreOffice to store the results",
        )
    )
    assert secret in profile.to_json()

    stripped = profile.without_samples()

    assert secret not in stripped.to_json()
    assert secret in profile.to_json()  # the original is untouched
    column = stripped.find_column("file1.csv", "Betrag_EUR")
    assert column is not None and (column.type, column.min, column.max) == (
        "decimal",
        12.5,
        98200.0,
    )
    assert stripped.summary == profile.summary
    assert [f.id for f in stripped.files] == [f.id for f in profile.files]


def test_a_profile_of_fifty_files_stays_small() -> None:
    template = full_profile().files[0]
    files = []
    for number in range(50):
        copy = template.model_copy(deep=True)
        copy.id, copy.path = f"f{number + 1}", f"file{number}.csv"
        copy.tables[0].columns = [
            template.tables[0].columns[0].model_copy(update={"key": f"c{i}", "name": f"C{i}"})
            for i in range(12)
        ]
        files.append(copy)
    profile = DataProfile(awa_version="1.1.0", created="2026-10-05T00:00:00Z", files=files)

    assert len(profile.to_json(indent=None)) < 350_000  # about 500 bytes per column


def _patterns(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            else:
                yield from _patterns(value)
    elif isinstance(node, list):
        for item in node:
            yield from _patterns(item)


def test_no_schema_pattern_uses_a_python_only_construct() -> None:
    shipped = json.loads(
        (Path(awa.data.__file__).parent / "profile.v1.schema.json").read_text(encoding="utf-8")
    )
    for document in (shipped, DataProfile.json_schema()):
        patterns = list(_patterns(document))
        assert patterns, "the schema must carry the flag code pattern"
        assert not [p for p in patterns if re.search(r"\(\?P|\(\?[#aiLmsux]", p)], patterns


def test_the_flag_code_pattern_still_accepts_and_rejects_the_same_codes() -> None:
    pattern = DataProfile.json_schema()["$defs"]["Flag"]["properties"]["code"]["pattern"]

    for good in ("DF-I-X", "DF-W-TOTALS-ROW", "DF-E-ZIP-UNSAFE"):
        assert re.match(pattern, good)
        assert make_flag(good, "m").code == good
    for bad in ("DF-X-ROW", "df-w-row", "DF-W-", "DF-W-A--B"):
        assert not re.match(pattern, bad)
        with pytest.raises(DataError):
            make_flag(bad, "m")
    assert isinstance(make_flag("DF-W-A", "m"), Flag)


# --- a reader can be supplied from the public API -------------------------------------------------


class _FixedReader:
    """A reader with fixed rows, written only with public names."""

    def __init__(self, rows: list[list[Cell]]) -> None:
        self.rows_given = rows

    def rows(
        self, file: FileProfile, table: TableProfile, *, max_rows: int
    ) -> Iterator[list[Cell]]:
        for count, row in enumerate(self.rows_given, start=1):
            if count > max_rows:
                raise RowLimitError(f"{file.path} has more than {max_rows} rows")
            yield row

    def text(self, file: FileProfile) -> tuple[str, bool]:
        return "", True


def test_the_reader_ports_are_public_and_a_custom_reader_is_used(tmp_path: Path) -> None:
    for name in ("DataReader", "RowLimitError", "ValueSource", "ColumnValues", "local_reader"):
        assert name in awa.data.__all__
    assert issubclass(RowLimitError, DataError)
    assert awa.errors.RowLimitError is RowLimitError
    p = profile(write_text(tmp_path, "t.csv", "id,amount\nA-1,5\nA-2,6\n"))
    reader: DataReader = _FixedReader([["A-1", 5], ["Z-99", 6]])

    claims = [Claim(path="cases[0]", value="the order Z-99 is open")]

    assert len(check_grounding(p, claims)) == 1  # the file does not hold Z-99
    assert check_grounding(p, claims, reader=reader) == []  # the reader's rows do
    assert isinstance(ColumnValues(frozenset(), 0, True), tuple)
    source: ValueSource = lambda ids: {}  # noqa: E731 - the port is a plain function
    assert source(["f1.t1.id"]) == {}
    assert type(local_reader()).__name__ == "LocalFileReader"


def test_every_public_name_has_exactly_one_group_comment_and_none_is_empty() -> None:
    text = Path(awa.data.__file__).read_text(encoding="utf-8")
    body = text[text.index("__all__ = [") :]
    groups = re.split(r"\n    # [^\n]+\n", body)[1:]
    assert groups
    assert all(group.strip().startswith('"') for group in groups), "an empty group comment"
    names = re.findall(r'^    "([^"]+)",$', body, re.MULTILINE)
    assert sorted(names) == sorted(awa.data.__all__)
    assert len(names) == len(set(names))


# --- a sampled SQLite table does not claim exact distinct counts ----------------------------------


def test_sampled_sqlite_columns_have_no_distinct_count_and_are_not_unique(tmp_path: Path) -> None:
    database = tmp_path / "t.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (id INTEGER, amount INTEGER)")
    connection.executemany("INSERT INTO t VALUES (?, ?)", [(n % 7, n) for n in range(50)])
    connection.commit()
    connection.close()

    sampled = profile(database, limits=Limits(sample_rows=10)).files[0].tables[0]
    whole = profile(database).files[0].tables[0]

    assert sampled.sampled and not whole.sampled
    assert [c.distinct for c in sampled.columns] == [None, None]
    assert [c.unique for c in sampled.columns] == [False, False]
    assert [c.distinct for c in whole.columns] == [7, 50]
    assert [c.unique for c in whole.columns] == [False, True]


# --- one spreading rule ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "wanted", "expected"),
    [
        (0, 3, []),
        (5, 0, []),
        (5, -1, []),
        (1, 3, [0]),
        (5, 1, [0]),
        (3, 5, [0, 1, 2]),
        (10, 4, [0, 3, 6, 9]),
        (10, 2, [0, 9]),
    ],
)
def test_spread_indices_takes_first_last_and_the_even_steps_between(
    count: int, wanted: int, expected: list[int]
) -> None:
    assert spread_indices(count, wanted) == expected


# --- from_json raises DataError for content that is not a profile ---------------------------------


def test_from_json_raises_data_error_for_an_invalid_profile_document() -> None:
    document = json.loads(DataProfile(awa_version="1", created="x").to_json())
    document["limits"]["max_files"] = "many"  # right schema id, invalid content

    with pytest.raises(DataError, match="not a valid data profile"):
        DataProfile.from_json(json.dumps(document))
    with pytest.raises(DataError, match="not a data profile"):
        DataProfile.from_json("{")
    with pytest.raises(DataError, match="schema"):
        DataProfile.from_json(json.dumps({"schema": "other/v9"}))


# --- limit errors name the way out and ingest and profile share the text --------------------------


def test_limit_errors_say_what_to_do(tmp_path: Path) -> None:
    for number in range(3):
        write_text(tmp_path, f"f{number}.csv", "a,b\n1,2\n")

    with pytest.raises(TooManyFilesError, match=r"3 files; at most 2 .*Limits\(max_files\)"):
        profile(tmp_path, limits=Limits(max_files=2))
    with pytest.raises(TooManyFilesError, match=r"3 files; at most 2 .*Limits\(max_files\)"):
        ingest(tmp_path, tmp_path / "out", limits=Limits(max_files=2))
    with pytest.raises(TooLargeError, match=r"together.*Limits\(max_total_bytes\)"):
        profile(tmp_path, limits=Limits(max_total_bytes=10))
    with pytest.raises(
        TooLargeError, match=r"f0\.csv: more than 3 bytes.*Limits\(max_file_bytes\)"
    ):
        ingest(tmp_path / "f0.csv", tmp_path / "out2", limits=Limits(max_file_bytes=3))
