"""`awa.toolkits.documents`: PDF and Word extraction, chunking and text statistics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from awa.errors import ConfigurationError, OptionalDependencyError
from awa.toolkits import chunk_text, document_toolkit, text_statistics


def tools(root: Path | None, **options: Any) -> dict[str, BaseTool]:
    options.setdefault("frame_output", False)
    return {tool.name: tool for tool in document_toolkit(root, **options)}


def call(tool: BaseTool, **args: Any) -> str:
    result = tool.invoke(args)
    assert isinstance(result, str)
    return result


# A 1x1 grey baseline JPEG: the image of a "scanned" page.
SCAN = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b"
    "0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e3334"
    "32ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708"
    "090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114"
    "328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748"
    "494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2"
    "a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9"
    "eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)
Line = tuple[int, int, str]


def make_pdf(path: Path, pages: list[str | list[Line] | bytes]) -> None:
    """A minimal PDF (no extra needed to write it). A page is one line of text, text at
    positions `(x, y, text)`, or the bytes of a JPEG filling the page (a scan, no text)."""

    objects: list[bytes] = [b"", b""]
    kids: list[int] = []
    font = b"/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"
    for page in pages:
        if isinstance(page, bytes):
            objects.append(
                b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
                b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
                % len(page)
                + page
                + b"\nendstream"
            )
            resources = b"/XObject << /Scan %d 0 R >>" % len(objects)
            stream = b"q 612 0 0 792 0 0 cm /Scan Do Q"
        else:
            lines = [(72, 720, page)] if isinstance(page, str) else page
            stream = " ".join(
                f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET" for x, y, text in lines
            ).encode()
            resources = font
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << %s >> >>" % (content, resources)
        )
        kids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start,
    )
    path.write_bytes(bytes(out))


def test_the_toolkit_without_a_root_builds_the_text_tools() -> None:
    assert list(tools(None)) == ["chunk_text", "summarize_stats"]
    with pytest.raises(ConfigurationError, match="need"):
        document_toolkit(include=["extract_pdf_text"])


def test_a_password_protected_pdf_is_named_and_the_others_still_read(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    make_pdf(tmp_path / "open.pdf", ["Open text"])
    writer = pypdf.PdfWriter(clone_from=str(tmp_path / "open.pdf"))
    writer.encrypt(user_password="secret", owner_password="owner")
    with (tmp_path / "locked.pdf").open("wb") as handle:
        writer.write(handle)
    built = tools(tmp_path)

    single = call(built["extract_pdf_text"], path="locked.pdf")
    batch = call(built["extract_pdfs_text"], paths=["locked.pdf", "open.pdf"])

    assert "protected by a password" in single
    assert "[not read: the PDF is protected by a password" in batch and "Open text" in batch


def test_extract_pdf_text(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    make_pdf(tmp_path / "report.pdf", ["First page text", "Second page text", "Third"])
    pdf = tools(tmp_path)["extract_pdf_text"]
    everything = call(pdf, path="report.pdf")
    assert "--- page 1 ---\nFirst page text" in everything
    assert "--- page 3 ---\nThird" in everything
    some = call(pdf, path="report.pdf", pages="2-3")
    assert "page 1" not in some and "Second page text" in some
    assert "invalid page range" in call(pdf, path="report.pdf", pages="x")
    missing_first = call(pdf, path="report.pdf", pages="9,2")
    assert missing_first == "--- page 2 ---\nSecond page text"
    first_two = call(tools(tmp_path, max_pages=2)["extract_pdf_text"], path="report.pdf")
    assert "page 2" in first_two and "Third" not in first_two
    (tmp_path / "notes.docx").write_bytes(b"PK")
    assert "not a .pdf file" in call(pdf, path="notes.docx")
    assert "must not contain '..'" in call(pdf, path="../report.pdf")
    framed = document_toolkit(tmp_path, include=["extract_pdf_text"])[0]
    assert call(framed, path="report.pdf").startswith("<tool_output")


def test_extract_docx_text(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Contract", level=1)
    document.add_paragraph("The term is 12 months.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Party"
    table.cell(0, 1).text = "ACME"
    table.cell(1, 0).text = "Fee"
    table.cell(1, 1).text = "100"
    document.save(str(tmp_path / "contract.docx"))
    text = call(tools(tmp_path)["extract_docx_text"], path="contract.docx")
    assert text.splitlines()[:2] == ["# Contract", "The term is 12 months."]
    assert "Party | ACME" in text and "Fee | 100" in text


def test_missing_extras_are_reported_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.toolkits.documents as documents

    def missing(module: str, package: str, *, feature: str) -> Any:
        raise OptionalDependencyError(f"{feature} needs {package}; install it with pip")

    monkeypatch.setattr(documents, "require_module", missing)
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "a.docx").write_bytes(b"PK")
    built = tools(tmp_path)
    assert "needs pypdf" in call(built["extract_pdf_text"], path="a.pdf")
    assert "needs python-docx" in call(built["extract_docx_text"], path="a.docx")


def test_chunk_text_keeps_paragraphs_and_overlaps() -> None:
    paragraphs = [f"Paragraph {i}. " + "word " * 30 for i in range(6)]
    chunks = chunk_text("\n\n".join(paragraphs), chunk_size=400, overlap=40)
    assert len(chunks) > 1
    assert all(len(chunk) <= 400 for chunk in chunks)
    assert chunks[0].startswith("Paragraph 0.")
    assert "Paragraph 5." in chunks[-1]
    long_word = chunk_text("x" * 250, chunk_size=100, overlap=0)
    assert [len(c) for c in long_word] == [100, 100, 50]
    with pytest.raises(ValueError):
        chunk_text("x", chunk_size=10)
    with pytest.raises(ValueError):
        chunk_text("x", chunk_size=100, overlap=100)
    assert chunk_text("   ") == []


def test_chunk_text_tool() -> None:
    tool = tools(None)["chunk_text"]
    data = json.loads(call(tool, text="One.\n\nTwo.", chunk_size=50, overlap=0))
    assert data == [{"index": 0, "chars": 10, "text": "One.\n\nTwo."}]
    assert call(tool, text="x", chunk_size=100, overlap=200).startswith("Error")


def test_summarize_stats() -> None:
    text = "Invoices are due. Invoices are late!\n\nPay the invoices now?"
    stats = text_statistics(text, top=2)
    assert stats["words"] == 10
    assert stats["sentences"] == 3
    assert stats["paragraphs"] == 2
    assert stats["top_words"][0] == ("invoices", 3)
    data = json.loads(call(tools(None)["summarize_stats"], text=text))
    assert data["reading_time_minutes"] == 0.1
    assert text_statistics("")["words"] == 0


def test_scanned_pages_are_marked_and_read_by_an_ocr_hook(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    import importlib.util

    from awa.toolkits.documents import NO_TEXT_LAYER, ScannedPage, pdf_text

    make_pdf(tmp_path / "mixed.pdf", ["Cover letter", SCAN, "Appendix"])
    plain = call(tools(tmp_path)["extract_pdf_text"], path="mixed.pdf")
    assert f"--- page 2 ---\n{NO_TEXT_LAYER}" in plain
    assert "--- page 1 ---\nCover letter" in plain and "Appendix" in plain
    seen: list[ScannedPage] = []

    def ocr(page: ScannedPage) -> str:
        seen.append(page)
        return "Rechnung Nr. 4711\nBetrag: 1.234,56 EUR"

    read = call(tools(tmp_path, ocr=ocr)["extract_pdf_text"], path="mixed.pdf")
    assert "--- page 2 (no text layer; read by OCR) ---\nRechnung Nr. 4711" in read
    [page] = seen
    assert page.number == 2 and page.pdf.startswith(b"%PDF")
    if importlib.util.find_spec("PIL") is None:
        assert page.images == (("Scan.jpg", SCAN),)
    assert pdf_text(tmp_path / "mixed.pdf", ocr=ocr)[1].startswith("Rechnung")
    assert pdf_text(tmp_path / "mixed.pdf")[1] == ""

    def broken(page: ScannedPage) -> str:
        raise RuntimeError("OCR service unavailable")

    failed = call(tools(tmp_path, ocr=broken)["extract_pdf_text"], path="mixed.pdf", pages="2")
    assert failed == (
        "--- page 2 ---\n[no text layer; ocr failed: RuntimeError: OCR service unavailable]"
    )


def test_layout_mode_keeps_table_columns_apart(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    rows = [("Artikel", "Menge", "Preis"), ("Schrauben", "1500", "0,80"), ("Duebel", "20", "2,25")]
    lines = [
        (x, 700 - 20 * number, cell)
        for number, row in enumerate(rows)
        for x, cell in zip((72, 300, 450), row, strict=True)
    ]
    make_pdf(tmp_path / "invoice.pdf", [lines])
    pdf = tools(tmp_path)["extract_pdf_text"]
    table = call(pdf, path="invoice.pdf", layout=True).splitlines()[1:]
    assert [line.split() for line in table if line.strip()] == [list(row) for row in rows]
    header = next(line for line in table if "Artikel" in line)
    assert header.index("Preis") - header.index("Menge") > 10  # the columns stay apart


def test_docx_text_follows_the_reading_order_with_headers_and_footers(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    section = document.sections[0]
    section.header.paragraphs[0].text = "ACME GmbH - Vertraulich"
    section.footer.paragraphs[0].text = "Seite 1"
    document.add_heading("Angebot 2026-17", level=1)
    document.add_paragraph("Wir bieten an:")
    prices = document.add_table(rows=3, cols=3)
    for row, values in zip(
        prices.rows,
        [("Pos", "Artikel", "Preis"), ("1", "Wartung", "1.200,00 €"), ("2", "Support", "300,00 €")],
        strict=True,
    ):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    document.add_paragraph("Zahlungsbedingungen", style="List Bullet")
    terms = document.add_table(rows=2, cols=2)
    merged = terms.cell(0, 0).merge(terms.cell(0, 1))
    merged.text = "Gültig bis 31.03.2026"
    terms.cell(1, 0).text = "Skonto"
    terms.cell(1, 1).text = "2 %"
    document.add_paragraph("Mit freundlichen Grüßen")
    document.save(str(tmp_path / "angebot.docx"))
    text = call(tools(tmp_path)["extract_docx_text"], path="angebot.docx")
    assert text.splitlines() == [
        "[header] ACME GmbH - Vertraulich",
        "",
        "# Angebot 2026-17",
        "Wir bieten an:",
        "",
        "Pos | Artikel | Preis",
        "1 | Wartung | 1.200,00 €",
        "2 | Support | 300,00 €",
        "",
        "- Zahlungsbedingungen",
        "",
        "Gültig bis 31.03.2026",
        "Skonto | 2 %",
        "",
        "Mit freundlichen Grüßen",
        "",
        "[footer] Seite 1",
    ]
