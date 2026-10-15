"""Document tools: text of PDF and Word files (`files` extra), chunking and text statistics.

PDF pages without a text layer (scans) are marked as such; an optional OCR hook (`ocr=`, any
function from a `ScannedPage` to text) reads them. Word documents are read in reading order,
tables between the paragraphs, with headers and footers.
"""

from __future__ import annotations

import contextvars
import io
import os
import re
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from awa._internal.install import require_module
from awa._internal.sandbox import Sandbox, check_zip_bounds
from awa._internal.text import STOPWORDS
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.errors import ConfigurationError, UsageLimitExceededError
from awa.toolkits._common import ToolArgs, ToolSpecs, positive, selected, tool_json, toolkit_tools
from awa.tools.result_cache import DATA_ROOT_METADATA_KEY

DOCUMENT_TOOLS = (
    "extract_pdf_text",
    "extract_pdfs_text",
    "extract_docx_text",
    "chunk_text",
    "summarize_stats",
)
"""Names of the tools `document_toolkit` builds, in order."""

_FILE_TOOLS = ("extract_pdf_text", "extract_pdfs_text", "extract_docx_text")
_OCR_WORKERS = 8
"""Most scanned pages of one document handed to the OCR hook at once (the hook may allow
fewer at a time, see `vision_ocr(max_concurrency=...)`)."""
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)
_STOPWORDS = STOPWORDS | frozenset(
    (  # noqa: SIM905
        "been but he her his no not she were yes der die das und ist ein eine einer eines zu den "
        "dem des mit von im auf für nicht sich auch als es wie bei oder aus nach wir sie ihr"
    ).split()
)


def chunk_text(text: str, *, chunk_size: int = 1_000, overlap: int = 100) -> list[str]:
    """Split `text` into chunks of at most about `chunk_size` characters.

    Paragraphs stay together where they fit; long paragraphs are split at sentence ends, very long
    sentences at word boundaries. Each chunk after the first repeats up to `overlap` characters of
    the previous one. `chunk_size` must be at least 50 and larger than `overlap`.
    """

    if chunk_size < 50:
        raise ValueError("chunk_size must be at least 50")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must be between 0 and chunk_size")
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= chunk_size:
            pieces.append(paragraph)
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            pieces.extend(_hard_split(sentence, chunk_size))
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        tail = _tail(current, overlap) if current else ""
        current = (
            f"{tail} {piece}".strip() if tail and len(tail) + len(piece) < chunk_size else piece
        )
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > size:
            if current:
                parts.append(current)
                current = ""
            parts.append(word[:size])
            word = word[size:]
        candidate = f"{current} {word}".strip()
        if len(candidate) > size:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _tail(text: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail


def text_statistics(text: str, *, top: int = 10) -> dict[str, Any]:
    """Count characters, words, sentences and paragraphs, and find the most frequent words."""

    words = _WORD.findall(text)
    lowered = [word.lower() for word in words]
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+|\n\s*\n", text.strip()) if _WORD.search(s)]
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    frequent = Counter(w for w in lowered if len(w) > 2 and w not in _STOPWORDS and not w.isdigit())
    return {
        "characters": len(text),
        "characters_without_spaces": len(re.sub(r"\s", "", text)),
        "words": len(words),
        "unique_words": len(set(lowered)),
        "sentences": len(sentences),
        "paragraphs": len(paragraphs),
        "lines": len(text.splitlines()),
        "average_word_length": round(sum(map(len, words)) / len(words), 2) if words else 0,
        "average_sentence_words": round(len(words) / len(sentences), 2) if sentences else 0,
        "reading_time_minutes": round(len(words) / 200, 1),
        "top_words": frequent.most_common(top),
    }


@dataclass(frozen=True)
class ScannedPage:
    """A PDF page without a text layer, handed to an OCR hook.

    `number` is 1-based; `pdf` is the page alone as a one-page PDF; `images` holds the page's
    images as `(file name, bytes)`: JPEG and JPEG 2000 always, other formats with Pillow.
    """

    number: int
    pdf: bytes = field(repr=False)
    images: tuple[tuple[str, bytes], ...] = field(default=(), repr=False)


OcrHook = Callable[[ScannedPage], str]
"""Reads the text of a scanned page (an OCR engine or a vision model); returns the text."""

NO_TEXT_LAYER = (
    "[no text layer: this page is probably a scanned image; its text cannot be read without "
    "OCR (see document_toolkit(ocr=...))]"
)
"""What `extract_pdf_text` says about a page without text."""


def pdf_text(
    path: str | os.PathLike[str] | bytes,
    *,
    pages: Sequence[int] | None = None,
    max_pages: int | None = None,
    max_chars: int | None = None,
    layout: bool = False,
    ocr: OcrHook | None = None,
) -> list[str]:
    """Return the text of each page of a PDF file (or of PDF `bytes`).

    `pages` (1-based) selects pages; `max_pages` caps how many are read and `max_chars` stops once
    that much text is collected. `layout` keeps columns and tables aligned; `ocr` reads pages
    without a text layer, which otherwise give `""`.

    Raises:
        OptionalDependencyError: If `pypdf` is not installed.
    """

    found = _pdf_pages(path, pages, max_pages, layout=layout, ocr=ocr, max_chars=max_chars)
    return [page.text for page in found]


@dataclass(frozen=True)
class _Page:
    number: int
    text: str
    source: str  # "text", "ocr", "none" or "ocr failed: ..."


def _pdf_pages(
    path: str | os.PathLike[str] | bytes,
    pages: Sequence[int] | None,
    limit: int | None = None,
    *,
    layout: bool = False,
    ocr: OcrHook | None = None,
    max_chars: int | None = None,
) -> list[_Page]:
    """The selected pages that exist, at most `limit` of them."""

    pypdf = require_module("pypdf", "pypdf", feature="PDF text extraction")
    try:
        reader = pypdf.PdfReader(io.BytesIO(path) if isinstance(path, bytes) else str(path))
    except Exception:
        name = "the PDF" if isinstance(path, bytes) else repr(os.path.basename(path))
        raise ToolException(
            f"{name} is not a readable PDF; check the file or export it again as PDF"
        ) from None
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("") != pypdf.PasswordType.NOT_DECRYPTED
        except Exception:  # noqa: BLE001 - an unsupported encryption is refused like a password
            opened = False
        if not opened:
            raise ToolException("the PDF is protected by a password; save an unprotected copy")
    count = len(reader.pages)
    numbers = [n for n in pages if 1 <= n <= count] if pages is not None else range(1, count + 1)
    chosen = list(numbers)[:limit] if limit is not None else list(numbers)
    result: list[_Page] = []
    pending: list[tuple[int, ScannedPage]] = []
    collected = 0
    for number in chosen:
        if max_chars is not None and collected >= max_chars:
            break
        page = reader.pages[number - 1]
        if layout:
            raw = page.extract_text(extraction_mode="layout") or ""
            text = "\n".join(line.rstrip() for line in raw.splitlines()).strip("\n")
        else:
            text = (page.extract_text() or "").strip()
        if text.strip():
            result.append(_Page(number, text, "text"))
        elif ocr is None:
            result.append(_Page(number, "", "none"))
        elif max_chars is None:  # read below, several pages at a time
            pending.append((len(result), _scanned(pypdf, page, number)))
            result.append(_Page(number, "", "ocr"))
        else:
            result.append(_read_scan(ocr, _scanned(pypdf, page, number)))
        collected += len(result[-1].text)
    if ocr is not None and pending:
        done = _in_parallel(lambda scan: _read_scan(ocr, scan), [scan for _, scan in pending])
        for (index, _), page_read in zip(pending, done, strict=True):
            result[index] = page_read
    return result


def _read_scan(ocr: OcrHook, page: ScannedPage) -> _Page:
    try:
        read = ocr(page).strip()
    except UsageLimitExceededError:
        raise  # the run's limit ends the run; it is not a page that could not be read
    except Exception as exc:
        return _Page(page.number, "", f"ocr failed: {type(exc).__name__}: {exc}")
    return _Page(page.number, read, "ocr")


def _in_parallel(function: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
    """`function` of every item, in order; several at a time (at most `_OCR_WORKERS`), each in a
    copy of the caller's context so the run's accounting and tracing see every call. The OCR
    hook bounds how many pages are read at once (`vision_ocr(max_concurrency=...)`)."""

    if len(items) <= 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(_OCR_WORKERS, len(items))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, function, item) for item in items]
        return [future.result() for future in futures]


def _render_pages(pages: list[_Page]) -> str:
    """The pages as the extraction tools return them: one `--- page N ---` section each."""

    sections = []
    for page in pages:
        if page.source == "text":
            heading, text = "", page.text
        elif page.source == "ocr":
            heading, text = " (no text layer; read by OCR)", page.text or "[OCR found no text]"
        elif page.source == "none":
            heading, text = "", NO_TEXT_LAYER
        else:
            heading, text = "", f"[no text layer; {page.source}]"
        sections.append(f"--- page {page.number}{heading} ---\n{text}")
    return "\n\n".join(sections) if sections else "The PDF has no readable pages."


def _scanned(pypdf: Any, page: Any, number: int) -> ScannedPage:
    writer = pypdf.PdfWriter()
    writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    images: list[tuple[str, bytes]] = []
    try:
        images = [(image.name, image.data) for image in page.images]
    except ImportError:  # Pillow is not installed: take the images stored as files
        objects = (page.get("/Resources") or {}).get("/XObject") or {}
        for name, reference in objects.items():
            image = reference.get_object()
            filters = image.get("/Filter")
            kind = filters[-1] if isinstance(filters, list) else filters
            suffix = {"/DCTDecode": ".jpg", "/JPXDecode": ".jp2"}.get(str(kind))
            if image.get("/Subtype") == "/Image" and suffix is not None:
                images.append((name.lstrip("/") + suffix, image._data))
    except Exception:  # an image pypdf cannot decode; the one-page PDF remains
        images = []
    return ScannedPage(number, buffer.getvalue(), tuple(images))


def docx_text(path: str | os.PathLike[str]) -> str:
    """Return the text of a Word document in reading order.

    Headings become `#` lines, list items `- `, table cells are separated by ` | `; page headers
    come first as `[header] ...` lines and footers last as `[footer] ...` lines.

    Raises:
        OptionalDependencyError: If `python-docx` is not installed.
    """

    docx = require_module("docx", "python-docx", feature="Word text extraction")
    check_zip_bounds(path)
    try:
        document = docx.Document(str(path))
    except Exception:
        raise ToolException(
            f"{os.path.basename(path)!r} is not a readable .docx document; "
            "check the file or save it as .docx"
        ) from None
    headers: list[str] = []
    footers: list[str] = []
    for section in document.sections:
        for parts, target, label in (
            (
                (section.header, section.first_page_header, section.even_page_header),
                headers,
                "header",
            ),
            (
                (section.footer, section.first_page_footer, section.even_page_footer),
                footers,
                "footer",
            ),
        ):
            for part in parts:
                if part.is_linked_to_previous:
                    continue
                for line in _docx_lines(part):
                    if line and f"[{label}] {line}" not in target:
                        target.append(f"[{label}] {line}")
    body = _docx_lines(document)
    lines = [*headers, *([""] if headers else []), *body, *([""] if footers else []), *footers]
    return "\n".join(lines).strip()


def _docx_lines(container: Any) -> list[str]:
    lines: list[str] = []
    for block in container.iter_inner_content():
        if hasattr(block, "rows"):  # a table
            if lines and lines[-1]:
                lines.append("")
            for row in block.rows:
                cells: list[Any] = []
                for cell in row.cells:  # a merged cell repeats; keep it once
                    if not any(cell._tc is seen._tc for seen in cells):
                        cells.append(cell)
                lines.append(" | ".join(" ".join(c.text.split()) for c in cells))
            lines.append("")
            continue
        text = block.text.strip()
        if not text:
            continue
        style = (block.style.name if block.style is not None else "") or ""
        heading = re.match(r"Heading (\d)", style)
        if heading:
            lines.append(f"{'#' * int(heading.group(1))} {text}")
        elif style.startswith("List"):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _page_numbers(spec: str | None, limit: int) -> list[int] | None:
    if spec is None or not spec.strip():
        return None
    numbers: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        match = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", part)
        if match is None:
            raise ToolException(f"invalid page range {part!r}; use e.g. '1-3,7'")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if last < first:
            raise ToolException(f"invalid page range {part!r}")
        numbers.extend(range(first, min(last, first + limit) + 1))
        if len(numbers) >= limit:
            break
    return numbers[:limit]


class ExtractPdfArgs(ToolArgs):
    path: str = Field(description="PDF file, relative to the root.")
    pages: str | None = Field(default=None, description="Pages to read, e.g. '1-3,7'; default all.")
    layout: bool = Field(
        default=False,
        description="Keep the page layout (columns and tables aligned with spaces); use it "
        "for tables, invoices and forms.",
    )


class ExtractPdfsArgs(ToolArgs):
    paths: list[str] = Field(
        min_length=1,
        description="PDF files or patterns relative to the root, e.g. ['scans/*.pdf'] or "
        "['a.pdf', 'b.pdf'].",
    )
    pages: str | None = Field(
        default=None, description="Pages to read in every file, e.g. '1-2'; default all."
    )
    layout: bool = Field(
        default=False, description="Keep the page layout (columns and tables aligned with spaces)."
    )


class ExtractDocxArgs(ToolArgs):
    path: str = Field(description="Word document (.docx), relative to the root.")


class ChunkTextArgs(ToolArgs):
    text: str = Field(description="The text to split.")
    chunk_size: int = Field(default=1_000, ge=50, le=20_000, description="Characters per chunk.")
    overlap: int = Field(default=100, ge=0, description="Characters repeated between chunks.")


class StatsArgs(ToolArgs):
    text: str = Field(description="The text to analyze.")
    top_words: int = Field(default=10, ge=0, le=50, description="How many frequent words to list.")


@dataclass(frozen=True)
class _Documents:
    sandbox: Sandbox | None
    max_pages: int
    max_file_bytes: int
    ocr: OcrHook | None = None
    max_files: int = 100  # most files one extract_pdfs_text call reads

    def _file(self, path_text: str, suffixes: tuple[str, ...]) -> Path:
        if self.sandbox is None:
            raise ToolException("no document directory is configured for this tool (root)")
        path = self.sandbox.resolve(path_text, must_exist=True)
        if not path.is_file():
            raise ToolException(f"{path_text!r} is not a file")
        if path.suffix.lower() not in suffixes:
            raise ToolException(f"{path_text!r} is not a {' or '.join(suffixes)} file")
        if path.stat().st_size > self.max_file_bytes:
            raise ToolException(f"{path_text!r} is larger than {self.max_file_bytes} bytes")
        return path

    def extract_pdf_text(self, args: ExtractPdfArgs) -> str:
        path = self._file(args.path, (".pdf",))
        numbers = _page_numbers(args.pages, self.max_pages)
        pages = _pdf_pages(path, numbers, self.max_pages, layout=args.layout, ocr=self.ocr)
        return _render_pages(pages)

    def _pdf_files(self, sandbox: Sandbox, patterns: list[str]) -> list[Path]:
        """The PDF files the paths and patterns name, in order, each once."""

        found: list[Path] = []
        for pattern in patterns:
            text = pattern.strip().replace("\\", "/")
            if not any(char in text for char in "*?["):
                found.append(self._file(text, (".pdf",)))
                continue
            sandbox.resolve(re.split(r"[*?\[]", text, maxsplit=1)[0] or ".")  # stays inside
            matches = sorted(
                path.resolve()
                for path in sandbox.root.glob(text)
                if path.is_file() and path.suffix.lower() == ".pdf"
            )
            usable = [p for p in matches if sandbox.contains(p) and not sandbox.hidden(p)]
            if not usable:
                raise ToolException(f"no PDF file matches {pattern!r}")
            found += [self._file(sandbox.relative(path), (".pdf",)) for path in usable]
        unique = list(dict.fromkeys(found))
        if len(unique) > self.max_files:
            raise ToolException(
                f"{len(unique)} PDF files named; read at most {self.max_files} per call"
            )
        return unique

    def extract_pdfs_text(self, args: ExtractPdfsArgs) -> str:
        sandbox = self.sandbox
        if sandbox is None:
            raise ToolException("no document directory is configured for this tool (root)")
        files = self._pdf_files(sandbox, args.paths)
        numbers = _page_numbers(args.pages, self.max_pages)

        def read(path: Path) -> str:
            try:
                pages = _pdf_pages(path, numbers, self.max_pages, layout=args.layout, ocr=self.ocr)
            except ToolException as exc:
                return f"[not read: {exc}]"
            except Exception as exc:  # noqa: BLE001 - one damaged file must not fail the batch
                return f"[not read: {type(exc).__name__}]"
            return _render_pages(pages)

        texts = _in_parallel(read, files)
        return "\n\n".join(
            f"=== {sandbox.relative(path)} ===\n{text}"
            for path, text in zip(files, texts, strict=True)
        )

    def extract_docx_text(self, args: ExtractDocxArgs) -> str:
        return docx_text(self._file(args.path, (".docx",))) or "The document has no text."

    @staticmethod
    def chunk(args: ChunkTextArgs) -> str:
        try:
            chunks = chunk_text(args.text, chunk_size=args.chunk_size, overlap=args.overlap)
        except ValueError as exc:
            raise ToolException(str(exc)) from None
        return tool_json(
            [{"index": i, "chars": len(chunk), "text": chunk} for i, chunk in enumerate(chunks)]
        )

    @staticmethod
    def stats(args: StatsArgs) -> str:
        return tool_json(text_statistics(args.text, top=args.top_words))


def document_toolkit(
    root: str | os.PathLike[str] | None = None,
    *,
    include: Sequence[str] | None = None,
    max_pages: int = 200,
    max_file_bytes: int = 50_000_000,
    max_output_chars: int = 50_000,
    allow_hidden: bool = False,
    frame_output: bool = True,
    ocr: OcrHook | None = None,
) -> list[BaseTool]:
    """Build the document tools.

    | Tool | Does | Extra |
    | --- | --- | --- |
    | `extract_pdf_text` | text of a PDF, page by page | `pypdf` |
    | `extract_pdfs_text` | text of many PDFs in one call, scans read in parallel | `pypdf` |
    | `extract_docx_text` | text of a Word document, with headings and tables | `python-docx` |
    | `chunk_text` | splits text into overlapping chunks | — |
    | `summarize_stats` | word, sentence and paragraph counts, frequent words | — |

    The extraction tools report a missing extra when called.

    Args:
        root: Directory the extraction tools read from. Without it only
            `chunk_text` and `summarize_stats` are built.
        include: Build only these tools (names from `DOCUMENT_TOOLS`).
        max_output_chars: Cap on the text returned to the model (four times as much for
            `extract_pdfs_text`, which returns many files).
        frame_output: Label extracted text as external data.
        ocr: Reads PDF pages without a text layer: a function from a `ScannedPage` to text,
            such as an OCR engine or a vision model; without it such pages are marked
            "no text layer".

    Raises:
        ConfigurationError: If `root` is not a directory, a limit is not positive, or
            `include` names an unknown tool or an extraction tool without a `root`.

    Example:
        ```python
        [pdf] = document_toolkit("./invoices", include=["extract_pdf_text"])
        ```
    """

    positive(max_pages, "max_pages")
    positive(max_file_bytes, "max_file_bytes")
    if root is None and include is not None:
        missing = [name for name in include if name in _FILE_TOOLS]
        if missing:
            raise ConfigurationError(f"{', '.join(missing)} need(s) a root directory")
    available = tuple(n for n in DOCUMENT_TOOLS if root is not None or n not in _FILE_TOOLS)
    names = selected(available, include, "documents")
    impl = _Documents(
        Sandbox(root, allow_hidden=allow_hidden) if root is not None else None,
        max_pages=max_pages,
        max_file_bytes=max_file_bytes,
        ocr=ocr,
    )
    specs: ToolSpecs = {
        "extract_pdf_text": (
            impl.extract_pdf_text,
            ExtractPdfArgs,
            "Extract the text of a PDF file, page by page (optionally only some pages, "
            "e.g. '1-3'; layout=true keeps tables aligned). Pages without a text layer "
            "(scans) are marked. Use it to read invoices, contracts and reports."
            + (
                " Scanned pages are read by a vision model; a legibility note heads pages it "
                "could not read well."
                if ocr is not None
                else ""
            ),
        ),
        "extract_pdfs_text": (
            impl.extract_pdfs_text,
            ExtractPdfsArgs,
            "Extract the text of many PDF files in one call (paths or patterns such as "
            "'scans/*.pdf'), each file headed '=== <path> ===' and read page by page; scanned "
            "pages of all files are read in parallel. Use it instead of one extract_pdf_text "
            "call per file when several PDFs are needed."
            + (
                " A legibility note heads scanned pages the vision model could not read well."
                if ocr is not None
                else ""
            ),
        ),
        "extract_docx_text": (
            impl.extract_docx_text,
            ExtractDocxArgs,
            "Extract the text of a Word document (.docx) in reading order: headings marked "
            "'#', tables in place with cells separated by '|', headers and footers.",
        ),
        "chunk_text": (
            _Documents.chunk,
            ChunkTextArgs,
            "Split a long text into overlapping chunks of about chunk_size characters, "
            "keeping paragraphs and sentences together. Use it before processing a long "
            "document piece by piece.",
        ),
        "summarize_stats": (
            _Documents.stats,
            StatsArgs,
            "Count characters, words, sentences and paragraphs of a text, estimate its "
            "reading time and list its most frequent words. Use it to check the length or "
            "focus of a text before or after writing it, for example against a word limit. "
            "Returns the counts as JSON.",
        ),
    }
    data_root = {DATA_ROOT_METADATA_KEY: str(Path(root).expanduser().resolve())} if root else None
    return toolkit_tools(
        "documents",
        names,
        specs,
        framed=_FILE_TOOLS if frame_output else (),
        limits={"extract_pdfs_text": max_output_chars * 4},
        metadata=data_root,
        max_output_chars=max_output_chars,
    )


__all__ = [
    "DOCUMENT_TOOLS",
    "NO_TEXT_LAYER",
    "OcrHook",
    "ScannedPage",
    "chunk_text",
    "document_toolkit",
    "docx_text",
    "pdf_text",
    "text_statistics",
]
