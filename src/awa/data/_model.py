"""The data profile model (`awa.data/profile/v1`): fields are only added within v1, ids are
`f1`, `f1.t1`, `f1.t1.<key>`, and text taken from a file is untrusted data."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator

from awa.data._errors import DataError

PROFILE_SCHEMA: Final = "awa.data/profile/v1"
"""The format identifier written into every profile (`DataProfile.schema_id`)."""

Cell = str | int | float | bool | None
"""A sample value: a number stays a number, a date is written as ISO text."""

FileKind = Literal[
    "csv", "xlsx", "pdf", "docx", "mail", "json", "sqlite", "text", "image", "zip", "unknown"
]
"""What a file is, decided by its content first and its name second."""

Severity = Literal["info", "warning", "error"]

ColumnType = Literal[
    "text", "integer", "decimal", "date", "datetime", "time", "bool", "id", "enum", "empty", "mixed"
]
"""The type the profiler gives a column after looking at its values."""

PiiKind = Literal["email", "iban", "card", "ip", "phone", "name", "address", "tax_id", "birthdate"]
"""A kind of personal data a column can hold."""

_FLAG_CODE_PATTERN = (
    r"^DF-[IWE]-[A-Z0-9]+(?:-[A-Z0-9]+)*$"  # portable: valid in JSON Schema (ECMA-262)
)
_FLAG_CODE = re.compile(r"^DF-(?P<family>[IWE])-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_SEVERITY = {"I": "info", "W": "warning", "E": "error"}
_KEY_SEPARATORS = re.compile(r"[^0-9a-z]+")
_FORMULA_EXAMPLES = " (e.g. "
_AFTER_EXAMPLES = "); open and save"


def column_key(name: str) -> str:
    """The normalized key of a column name: lower case, runs of other characters become `_`.

    The data tools find columns by this key, so a profile key can be passed to them unchanged.
    """

    return _KEY_SEPARATORS.sub("_", name.lower()).strip("_")


class DataModel(BaseModel):
    """Base of all profile models: exact on construction, tolerant when reading a file."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _ignore_newer_fields(cls, data: Any, info: ValidationInfo) -> Any:
        if isinstance(data, dict) and info.context and info.context.get("ignore_unknown"):
            return {key: value for key, value in data.items() if key in cls.model_fields}
        return data


class Limits(DataModel):
    """What the profiler may spend per run; zero or negative values raise `ValidationError`."""

    max_files: int = Field(500, ge=1, description="Most files in one run.")
    max_file_bytes: int = Field(100_000_000, ge=1, description="Largest single file.")
    max_total_bytes: int = Field(1_000_000_000, ge=1, description="Largest sum of all files.")
    max_rows_exact: int = Field(200_000, ge=1, description="Up to here rows are counted exactly.")
    sample_rows: int = Field(5_000, ge=1, description="Reservoir sample beyond max_rows_exact.")
    max_sheets: int = Field(50, ge=1, description="Most sheets profiled per workbook.")
    max_pdf_pages_profiled: int = Field(50, ge=1, description="Pages analyzed in detail.")
    max_pdf_pages: int = Field(2_000, ge=1, description="Larger PDFs are refused.")
    max_chars_per_cell: int = Field(200, ge=1, description="Longer values are cut.")
    parse_timeout_s: float = Field(30.0, gt=0, description="Seconds per file.")
    max_zip_entries: int = Field(500, ge=1, description="Most entries in an archive.")
    max_zip_ratio: int = Field(100, ge=1, description="Largest unpack ratio of one entry.")


class Privacy(DataModel):
    """What a model may see: masking and how many sample values leave the profile.

    `mask` applies to model-facing text only; files and statistics are never changed.
    """

    mask: Literal["none", "placeholders", "drop"] = "none"
    max_sample_rows: int = Field(3, ge=0)
    max_cell_chars: int = Field(80, ge=1)
    include_samples: bool = True
    kinds: list[PiiKind] | None = Field(
        None, description="PII kinds to mask; None masks every detected kind."
    )


class Flag(DataModel):
    """A fact or decision the profiler wants a person (or a validator) to know about.

    `code` is `DF-<family>-<NAME>`; the severity follows the family, so build flags with
    `make_flag`.
    """

    code: str = Field(pattern=_FLAG_CODE_PATTERN)
    severity: Severity
    message: str
    where: str | None = Field(None, description="Id of the file, table or column concerned.")


def make_flag(code: str, message: str, where: str | None = None) -> Flag:
    """A `Flag` whose severity follows the family letter of `code` (`DF-W-...` is a warning).

    Raises:
        DataError: If `code` is not of the form `DF-I-NAME`, `DF-W-NAME` or `DF-E-NAME`.
    """

    match = _FLAG_CODE.match(code)
    if match is None:
        raise DataError(f"flag code {code!r} must look like DF-W-NAME (family I, W or E)")
    return Flag(code=code, severity=_SEVERITY[match["family"]], message=message, where=where)


class NumberFormat(DataModel):
    """How the numbers of a column are written."""

    decimal: str | None = Field(None, description="Decimal separator, `,` or `.`.")
    thousands: str | None = Field(None, description="Thousands separator, if any.")
    currency: str | None = None
    percent: bool = False
    negative: Literal["minus", "parentheses", "trailing_minus"] = "minus"


class PiiInfo(DataModel):
    """Personal data found in a column: which kinds, and the share of non-empty values."""

    kinds: list[PiiKind] = Field(default_factory=list)
    share: float = Field(0.0, ge=0.0, le=1.0)


class TopValue(DataModel):
    """One frequent value of a low-cardinality column."""

    value: str
    count: int


class ColumnProfile(DataModel):
    """One column of a table, with the decisions the profiler took about its values."""

    name: str = Field(description="The header cell as written (or `A`, `B`, ... without header).")
    key: str = Field(description="`column_key(name)`, made unique within the table.")
    index: int = Field(ge=0, description="0-based position in the table.")
    type: ColumnType = "text"
    declared_type: str | None = Field(None, description="Type declared by the source (SQLite).")
    nullable: bool = False
    null_share: float = Field(0.0, ge=0.0, le=1.0)
    distinct: int | None = None
    unique: bool = False
    format: NumberFormat | None = None
    date_format: str | None = Field(None, description="Pattern such as `DD.MM.YYYY`.")
    ambiguous: bool = Field(False, description="True when no value rules out the other reading.")
    unit: str | None = Field(None, description="Unit hint from the header (`EUR`, `kg`, `%`).")
    min: float | int | str | None = None
    max: float | int | str | None = None
    top: list[TopValue] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list, description="Values as written in the file.")
    pii: PiiInfo = Field(default_factory=PiiInfo)
    flags: list[str] = Field(
        default_factory=list, description="Codes of the flags that concern this column."
    )


class TableProfile(DataModel):
    """A table: a CSV file, a sheet (or a block of a sheet), a SQLite table.

    `rows` is exact when `rows_exact`; `sampled` means the statistics come from `rows_seen` rows.
    """

    id: str
    sheet: str | None = Field(None, description="Worksheet name (xlsx).")
    name: str | None = Field(None, description="Table name (SQLite).")
    range: str | None = Field(None, description="Cell range of the table on its sheet, `A4:F28`.")
    header_row: int = Field(1, ge=0, description="1-based row of the column names; 0: none.")
    header_confidence: float = Field(1.0, ge=0.0, le=1.0)
    data_start_row: int = Field(2, ge=1)
    rows: int = Field(0, ge=0)
    rows_exact: bool = True
    sampled: bool = False
    rows_seen: int | None = None
    totals_rows: list[int] = Field(default_factory=list, description="1-based; not in statistics.")
    columns: list[ColumnProfile] = Field(default_factory=list)
    key_candidates: list[list[str]] = Field(
        default_factory=list, description="Column keys that are unique and never empty."
    )
    primary_key: list[str] = Field(default_factory=list, description="Declared key (SQLite).")
    sample_rows: list[list[Cell]] = Field(default_factory=list)

    def column(self, name: str) -> ColumnProfile | None:
        """The column called `name` (as written, or by key, ignoring case and punctuation)."""

        wanted = column_key(name)
        for column in self.columns:
            if column.name == name or column.key == wanted:
                return column
        return None


class TextInfo(DataModel):
    """How a text file is encoded and laid out (CSV, plain text, Markdown, logs)."""

    encoding: str | None = Field(None, description="`utf-8`, `utf-8-sig`, `cp1252`, ...")
    newline: Literal["lf", "crlf", "cr", "mixed"] | None = None
    delimiter: str | None = None
    quote: str | None = None
    bom: bool = False
    lines: int | None = None
    head: list[str] = Field(default_factory=list, description="First lines (text kind).")
    tail: list[str] = Field(default_factory=list, description="Last lines (text kind).")


class ListValidation(DataModel):
    """A dropdown list of a worksheet: the allowed values are a free enum definition."""

    range: str
    values: list[str]


class SheetInfo(DataModel):
    """One worksheet of a workbook, also when it holds no table (hidden, empty, notes)."""

    name: str
    visible: bool = True
    used_range: str | None = Field(None, description="Recomputed from the cells, not trusted.")
    rows: int = 0
    columns: int = 0
    tables: list[str] = Field(default_factory=list, description="Ids of the tables found.")
    merged_ranges: list[str] = Field(default_factory=list)
    hidden_rows: int = 0
    hidden_columns: int = 0
    formulas: int = 0
    formulas_without_cache: int = Field(0, description="Formulas whose value was never saved.")
    comments: int = 0
    validations: list[ListValidation] = Field(default_factory=list)


class XlsxInfo(DataModel):
    """Workbook-level facts of an Excel file."""

    sheets: list[SheetInfo] = Field(default_factory=list)
    date_system: Literal["1900", "1904"] = "1900"
    defined_names: list[str] = Field(default_factory=list)
    macros: bool = Field(False, description="The file carries a macro project (never run).")


class PdfPage(DataModel):
    """One page of a PDF."""

    page: int = Field(ge=1)
    chars: int = Field(0, ge=0)
    text_layer: bool = False
    image_only: bool = False
    tables_hint: int = Field(0, ge=0, description="Ruled table candidates, not extracted.")


class KeyValue(DataModel):
    """A recognized field of a document: an IBAN, a date, an amount, an invoice number."""

    kind: str
    key: str | None = None
    value: str
    page: int | None = None


class PdfInfo(DataModel):
    """Facts of a PDF: text layer versus scan, per-page figures, an excerpt, key values."""

    pages: int = Field(0, ge=0)
    per_page: list[PdfPage] = Field(default_factory=list, description="Only profiled pages.")
    document_class: Literal["born_digital", "scanned", "mixed", "empty"] = "empty"
    encrypted: bool = False
    fonts_embedded: bool | None = None
    form_fields: int = 0
    metadata: dict[str, str] = Field(default_factory=dict, description="Producer, title, ...")
    language: str | None = None
    excerpt: str = Field("", description="Reading-order text at the start; untrusted data.")
    key_values: list[KeyValue] = Field(default_factory=list)


class Heading(DataModel):
    """One heading of a Word document."""

    level: int = Field(ge=1)
    text: str


class DocxTable(DataModel):
    """A table inside a Word document."""

    rows: int = 0
    columns: int = 0
    header: list[str] = Field(default_factory=list)


class DocxInfo(DataModel):
    """Facts of a Word document: outline, tables, review marks and an excerpt."""

    paragraphs: int = 0
    text_length: int = 0
    headings: list[Heading] = Field(default_factory=list)
    tables: list[DocxTable] = Field(default_factory=list)
    headers_footers: bool = False
    tracked_changes: int = 0
    comments: int = 0
    images: int = 0
    language: str | None = None
    excerpt: str = Field("", description="Text at the start; untrusted data.")


class MailAttachment(DataModel):
    """An attachment of a message; its own profile is a child of the mail file."""

    name: str
    content_type: str | None = None
    size: int = 0
    file_id: str | None = Field(None, description="Id of the child FileProfile, if profiled.")


class MailMessage(DataModel):
    """One message of an `.eml` or `.mbox` file. Header values and the body are untrusted."""

    index: int = Field(ge=1)
    headers: dict[str, str] = Field(
        default_factory=dict, description="from, to, cc, subject, date, message_id, in_reply_to"
    )
    content_type: str | None = None
    body_kind: Literal["plain", "html", "both", "none"] = "none"
    attachments: list[MailAttachment] = Field(default_factory=list)
    language: str | None = None
    excerpt: str = ""
    injection_suspected: bool = False


class MailInfo(DataModel):
    """Facts of a mail file: the messages (at most a sample of a large mailbox)."""

    messages_total: int = 0
    messages: list[MailMessage] = Field(default_factory=list)
    sampled: bool = False


class JsonField(DataModel):
    """A path of a JSON document with the types and optionality seen over the records."""

    path: str = Field(description="`lines[].sku`: `[]` marks array items.")
    types: list[str] = Field(default_factory=list, description="`string`, `number`, `null`, ...")
    optional_share: float = Field(0.0, ge=0.0, le=1.0, description="Share of records without it.")
    enum: list[str] = Field(default_factory=list)
    format: str | None = Field(None, description="`iso_datetime`, `epoch_ms`, ...")


class JsonInfo(DataModel):
    """Schema inferred over the records of a JSON or JSONL file."""

    top_level: Literal["object", "array", "jsonl", "scalar"] = "object"
    records: int | None = None
    depth: int = 0
    sampled: bool = False
    paths: list[JsonField] = Field(default_factory=list)
    key_variants: list[list[str]] = Field(
        default_factory=list, description="Spellings of one name, `customerId` and `customer_id`."
    )


class ForeignKey(DataModel):
    """A declared foreign key of a SQLite database."""

    table: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


class SqliteInfo(DataModel):
    """Facts of a SQLite database beyond its tables (opened read-only)."""

    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    views: list[str] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)


class Relation(DataModel):
    """A likely link between two columns (a join candidate). Use it only above 0.8 confidence."""

    from_column: str = Field(description="Column id `f1.t1.supplier`.")
    to_column: str
    kind: Literal["foreign_key", "value_overlap", "name_match"]
    overlap: float | None = Field(None, ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    unmatched: list[str] = Field(
        default_factory=list,
        description="A few values of `from_column` that have no counterpart in `to_column` "
        "(sample values: `without_samples` removes them).",
    )


class FileProfile(DataModel):
    """One file: identity, status, the tables found and one facts block for its kind.

    `status` is `ok`, `partial` (a limit cut the profile), `error` (see `flags`) or `unsupported`.
    `path` is relative to the profiled root; `source` is the path the file was read from.
    """

    id: str
    path: str
    source: str | None = None
    sha256: str
    size: int = Field(ge=0)
    kind: FileKind
    detected_by: Literal["content", "extension"] = "content"
    status: Literal["ok", "partial", "error", "unsupported"] = "ok"
    role_hint: str | None = None
    mtime: str | None = None
    text: TextInfo | None = None
    tables: list[TableProfile] = Field(default_factory=list)
    xlsx: XlsxInfo | None = None
    pdf: PdfInfo | None = None
    docx: DocxInfo | None = None
    mail: MailInfo | None = None
    json_doc: JsonInfo | None = None
    sqlite: SqliteInfo | None = None
    children: list[FileProfile] = Field(
        default_factory=list, description="Profiles of embedded files (mail attachments)."
    )
    relations: list[Relation] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)

    def has_flag(self, code: str) -> bool:
        """Whether a flag with this code is set on the file."""

        return any(flag.code == code for flag in self.flags)

    def without_samples(self) -> Self:
        """A copy without any text taken from the file (see `DataProfile.without_samples`)."""

        copy = self.model_copy(deep=True)
        for table in copy.tables:
            table.sample_rows = []
            for column in table.columns:
                column.examples, column.top = [], []
                if column.type not in ("date", "datetime", "time"):  # text, ids: values of the file
                    column.min = None if isinstance(column.min, str) else column.min
                    column.max = None if isinstance(column.max, str) else column.max
        if copy.xlsx is not None:
            for sheet in copy.xlsx.sheets:
                for validation in sheet.validations:
                    validation.values = []
        copy.flags = [_without_examples(flag) for flag in copy.flags]
        if copy.text is not None:
            copy.text.head, copy.text.tail = [], []
        if copy.pdf is not None:
            copy.pdf.excerpt, copy.pdf.key_values, copy.pdf.metadata = "", [], {}
        if copy.docx is not None:
            copy.docx.excerpt = ""
            for heading in copy.docx.headings:
                heading.text = ""
            for docx_table in copy.docx.tables:
                docx_table.header = []
        if copy.mail is not None:
            for message in copy.mail.messages:
                message.excerpt, message.headers = "", {}
                for attachment in message.attachments:
                    attachment.name = ""
        if copy.json_doc is not None:
            for field in copy.json_doc.paths:
                field.enum = []
        copy.children = [child.without_samples() for child in copy.children]
        copy.relations = [_without_values(relation) for relation in copy.relations]
        return copy


class Summary(DataModel):
    """Counts over the whole profile, always recomputed from `files`."""

    files: int = 0
    tables: int = 0
    rows: int = 0
    warnings: int = Field(
        0, description="Flags of severity warning on the profile and on each top-level file."
    )
    errors: int = Field(0, description="Top-level files with status error.")


class DataProfile(DataModel):
    """The profile of a set of files (JSON schema `awa.data/profile/v1`).

    Example:
        ```python
        p = profile(["orders.csv"])
        assert DataProfile.from_json(p.to_json()) == p
        print(p.find_column("orders.csv", "Amount").format.decimal)
        ```
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["awa.data/profile/v1"] = Field(PROFILE_SCHEMA, alias="schema")
    awa_version: str
    created: str = Field(description="UTC time of the run, ISO 8601.")
    limits: Limits = Field(default_factory=Limits)
    privacy: Privacy = Field(default_factory=Privacy)
    files: list[FileProfile] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)

    @model_validator(mode="after")
    def _count(self) -> Self:
        tables = [table for file in self.files for table in file.tables]
        self.summary = Summary(
            files=len(self.files),
            tables=len(tables),
            rows=sum(table.rows for table in tables),
            warnings=sum(
                flag.severity == "warning"
                for flag in [*self.flags, *(f for file in self.files for f in file.flags)]
            ),
            errors=sum(file.status == "error" for file in self.files),
        )
        return self

    def to_json(self, *, indent: int | None = 2) -> str:
        """The stable JSON text: fields in declaration order, every field present."""

        return self.model_dump_json(by_alias=True, indent=indent)

    @classmethod
    def from_json(cls, text: str | bytes) -> Self:
        """Read a profile written by this or a later 1.x awa (unknown fields are ignored).

        Raises:
            DataError: If the text is not a valid profile or was written in another major version.
        """

        try:
            document = json.loads(text)
        except ValueError as error:
            raise DataError(f"not a data profile: invalid JSON ({error})") from error
        found = document.get("schema") if isinstance(document, dict) else None
        if found != PROFILE_SCHEMA:
            raise DataError(
                f"cannot read a data profile with schema {found!r}; this awa reads {PROFILE_SCHEMA!r}. "
                "Profile the files again with this version of awa."
            )
        try:
            return cls.model_validate(document, context={"ignore_unknown": True})
        except ValidationError as error:
            raise DataError(f"not a valid data profile: {error}") from error

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """The JSON Schema (draft 2020-12) of `to_json()`, as shipped in `profile.v1.schema.json`."""

        schema = cls.model_json_schema(by_alias=True, mode="serialization")
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}

    def tables(self) -> Iterator[tuple[FileProfile, TableProfile]]:
        """Every (file, table) pair in profile order."""

        for file in self.files:
            for table in file.tables:
                yield file, table

    def find_file(self, ref: str) -> FileProfile | None:
        """The file with this id, relative path or base name (case-insensitive); `None` if none
        or if a base name matches several files."""

        wanted = ref.replace("\\", "/").lower()
        by_id = [file for file in self.files if file.id == ref]
        by_path = [file for file in self.files if file.path.lower() == wanted]
        by_name = [file for file in self.files if file.path.lower().rsplit("/", 1)[-1] == wanted]
        for found in (by_id, by_path, by_name):
            if len(found) == 1:
                return found[0]
        return None

    def find_table(self, file: str, sheet: str | None = None) -> TableProfile | None:
        """The table of `file` for `sheet` (case-insensitive); the first table without `sheet`."""

        target = self.find_file(file)
        if target is None:
            return None
        for table in target.tables:
            if sheet is None or (table.sheet or table.name or "").lower() == sheet.lower():
                return table
        return None

    def find_column(
        self, file: str, column: str, *, sheet: str | None = None
    ) -> ColumnProfile | None:
        """The column `column` (name or key) of the table of `file`, or `None`."""

        table = self.find_table(file, sheet)
        return None if table is None else table.column(column)

    def without_samples(self) -> Self:
        """A copy without any text taken from the files; structure and statistics stay."""

        return self.model_copy(
            update={
                "files": [f.without_samples() for f in self.files],
                "relations": [_without_values(r) for r in self.relations],
            },
            deep=True,
        )


class ProfileEvent(DataModel):
    """A progress report of `profile()`, without file content.

    Per file: `start`, `detect`, `parse`, then `done` or `error`; the run sends `start` and `done`
    with `file=None`.
    """

    file: str | None = Field(None, description="File id; None for the whole run.")
    stage: Literal["start", "detect", "parse", "done", "error"]
    message: str = ""
    path: str | None = None
    kind: str | None = None
    status: str | None = None
    flags: int = 0


class DataRef(DataModel):
    """A reference a design makes to the data: a file, optionally a sheet and columns."""

    file: str
    sheet: str | None = None
    columns: list[str] = Field(default_factory=list)
    header_row: int | None = None


class DataIssue(DataModel):
    """A reference-check problem (`DF-V-NNN`); `suggestion` is the closest real name."""

    code: str = Field(pattern=r"^DF-V-\d{3}$")
    severity: Literal["error", "warning"] = "error"
    path: str
    message: str
    suggestion: str | None = None


def _without_examples(flag: Flag) -> Flag:
    """The flag without the formulas it quotes from the file (`DF-W-FORMULA-NO-CACHE`)."""

    head, found, rest = flag.message.partition(_FORMULA_EXAMPLES)
    if flag.code != "DF-W-FORMULA-NO-CACHE" or not found or _AFTER_EXAMPLES not in rest:
        return flag
    tail = rest.rpartition(_AFTER_EXAMPLES)[2]
    return flag.model_copy(update={"message": f"{head}; open and save{tail}"})


def _without_values(relation: Relation) -> Relation:
    return relation.model_copy(update={"unmatched": []})
