"""Shared helpers for the `awa.data` tests: a hand-built profile that uses every part of the model,
and small file writers. Import with `from _helpers.data_fixtures import full_profile, write_text`."""

from __future__ import annotations

from pathlib import Path

from awa.data import (
    ColumnProfile,
    DataProfile,
    DocxInfo,
    FileProfile,
    JsonInfo,
    MailInfo,
    PdfInfo,
    Relation,
    SqliteInfo,
    TableProfile,
    XlsxInfo,
    make_flag,
)
from awa.data._model import (
    DocxTable,
    ForeignKey,
    Heading,
    JsonField,
    KeyValue,
    ListValidation,
    MailAttachment,
    MailMessage,
    NumberFormat,
    PdfPage,
    PiiInfo,
    SheetInfo,
    TextInfo,
    TopValue,
)

REPO = Path(__file__).resolve().parents[2]
ORDERS_CSV = (
    REPO / "examples" / "benchmarks" / "core" / "02_supplier_orders" / "data" / "orders.csv"
)


def computed(
    expression: str, profile: DataProfile, *, file: str, sheet: str | None = None
) -> float | int | str | None:
    """The value `expression` computes to over the rows of `file`, or `None` when it cannot."""
    from awa.data import local_reader
    from awa.data._errors import DataError
    from awa.data._reference import _compute, _Failure

    try:
        return _compute(expression, profile, file, sheet, local_reader())[0]
    except (_Failure, DataError):
        return None


def write_text(folder: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    """Write `text` to `folder/name` byte-exact (no newline translation) and return the path."""

    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def _file(number: int, kind: str, **parts: object) -> FileProfile:
    return FileProfile.model_validate(
        {
            "id": f"f{number}",
            "path": f"file{number}.{kind}",
            "sha256": "0" * 64,
            "size": 100 * number,
            "kind": kind,
            **parts,
        }
    )


def full_profile() -> DataProfile:
    """A profile with one file of every kind, every block filled, one relation and flags."""

    amount = ColumnProfile(
        name="Betrag_EUR",
        key="betrag_eur",
        index=3,
        type="decimal",
        nullable=False,
        distinct=398,
        format=NumberFormat(decimal=",", thousands="."),
        unit="EUR",
        min=12.5,
        max=98200.0,
        examples=["4.250,00", "5.980,50"],
        pii=PiiInfo(kinds=["iban"], share=0.5),
        flags=["DF-W-DECIMAL-COMMA"],
        top=[TopValue(value="open", count=3)],
    )
    csv = _file(
        1,
        "csv",
        text=TextInfo(encoding="cp1252", newline="crlf", delimiter=";", quote='"'),
        tables=[
            TableProfile(
                id="f1.t1",
                rows=412,
                columns=[amount],
                key_candidates=[["bestellnr"]],
                sample_rows=[["B-1", 4250.0, None, True]],
            )
        ],
        flags=[make_flag("DF-W-DECIMAL-COMMA", "decimal comma in all 412 numeric cells", "f1.t1")],
    )
    xlsx = _file(
        2,
        "xlsx",
        tables=[TableProfile(id="f2.t1", sheet="Rechnungen", range="A4:F28", header_row=4)],
        xlsx=XlsxInfo(
            sheets=[
                SheetInfo(
                    name="Rechnungen",
                    tables=["f2.t1"],
                    merged_ranges=["A1:F1"],
                    validations=[ListValidation(range="E5:E28", values=["open", "paid"])],
                )
            ],
            defined_names=["Total"],
        ),
    )
    pdf = _file(
        3,
        "pdf",
        pdf=PdfInfo(
            pages=1,
            per_page=[PdfPage(page=1, chars=900, text_layer=True)],
            document_class="born_digital",
            excerpt="Rechnung 2024-0412",
            key_values=[KeyValue(kind="invoice_no", value="2024-0412", page=1)],
        ),
    )
    docx = _file(
        4,
        "docx",
        docx=DocxInfo(
            paragraphs=12,
            headings=[Heading(level=1, text="Leistungen")],
            tables=[DocxTable(rows=3, columns=2, header=["Pos", "Betrag"])],
        ),
    )
    child = _file(6, "pdf", id="f5.a1", pdf=PdfInfo(pages=1, document_class="scanned"))
    mail = _file(
        5,
        "mail",
        mail=MailInfo(
            messages_total=1,
            messages=[
                MailMessage(
                    index=1,
                    headers={"subject": "Rechnung"},
                    body_kind="plain",
                    attachments=[MailAttachment(name="r.pdf", size=10, file_id="f5.a1")],
                    injection_suspected=True,
                )
            ],
        ),
        children=[child],
    )
    data = _file(
        7,
        "json",
        json_doc=JsonInfo(
            top_level="array",
            records=200,
            paths=[JsonField(path="lines[].sku", types=["string"], optional_share=0.0)],
        ),
    )
    db = _file(
        8,
        "sqlite",
        tables=[TableProfile(id="f8.t1", name="orders", primary_key=["id"], rows=5)],
        sqlite=SqliteInfo(
            foreign_keys=[
                ForeignKey(table="orders", columns=["cid"], ref_table="c", ref_columns=["id"])
            ]
        ),
    )
    return DataProfile(
        awa_version="1.1.0",
        created="2026-10-05T09:12:44Z",
        files=[csv, xlsx, pdf, docx, mail, data, db],
        relations=[
            Relation(
                from_column="f1.t1.betrag_eur",
                to_column="f2.t1.betrag",
                kind="value_overlap",
                overlap=0.92,
                confidence=0.8,
            )
        ],
    )
