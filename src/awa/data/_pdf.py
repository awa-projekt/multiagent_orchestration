"""PDF files (`pdf`): text layer or scan, page facts, excerpt and key-value hints; runs in the
killable worker, checks the page count first and never runs scripts or embedded files."""

from __future__ import annotations

import re
from itertools import islice
from pathlib import Path
from typing import Any

from awa.data._injection import guess_language, injection_flags
from awa.data._model import FileProfile, KeyValue, PdfInfo, PdfPage
from awa.data._parse import ParseContext, ParseTimeout
from awa.data._sampling import spread_indices
from awa.kernel.redaction import PII_PATTERNS, iban_valid

KINDS = ("pdf",)

EXCERPT_CHARS = 1500
MAX_KEY_VALUES = 40
_MIN_TEXT_CHARS = 30
_MAX_GARBLE_SHARE = 0.3
_MAX_CONTENT_BYTES = 8_000_000
_MAX_FONTS = 200
_MAX_RULES = 5000

_ACTIVE = (
    ("JavaScript", re.compile(rb"/JavaScript|/JS\b")),
    ("launch actions", re.compile(rb"/Launch\b")),
    ("embedded files", re.compile(rb"/EmbeddedFile\b")),
)
_METADATA = {
    "/Title": "title",
    "/Author": "author",
    "/Subject": "subject",
    "/Keywords": "keywords",
    "/Creator": "creator",
    "/Producer": "producer",
    "/CreationDate": "created",
    "/ModDate": "modified",
}
_NUMBER = rb"(-?\d+\.?\d*|-?\.\d+)"
_RECT = re.compile(
    _NUMBER + rb"\s+" + _NUMBER + rb"\s+" + _NUMBER + rb"\s+" + _NUMBER + rb"\s+re\b"
)
_LINE = re.compile(
    _NUMBER + rb"\s+" + _NUMBER + rb"\s+m\s+" + _NUMBER + rb"\s+" + _NUMBER + rb"\s+l\b"
)
_INVISIBLE = re.compile(rb"(?<![\d.])3\s+Tr\b")
_WHITE = re.compile(rb"(?<![\d.])1\s+1\s+1\s+rg\b|(?<![\d.])1\s+g\b")
_FILL = re.compile(rb"\s(?:f|F|f\*|B|b|B\*|b\*)\s")
_INLINE_IMAGE = re.compile(rb"\sBI\s")

_SEGMENTS = re.compile(r"\s{2,}|\s\|\s")
_PAIR = re.compile(r"^([^\W\d_][^:]{0,38}?[^\W\d])\s*:\s*(\S.*)$")  # a key ends in a letter
_AMOUNT = re.compile(r"^(.{2,30}?)\s+([-+]?\d[\d.,']*\d)\s*(EUR|€|USD|CHF|GBP|\$)$")
_DATE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}")
_TITLE = re.compile(
    r"^(RECHNUNG|INVOICE|Rechnung|Invoice|GUTSCHRIFT|Credit Note|Lieferschein)\s+"
    r"(?:Nr\.?\s*|No\.?\s*|#\s*)?([A-Z0-9][\w\-/]{3,})$"
)
_VAT = re.compile(r"[A-Z]{2}\d{8,12}")
_REFERENCE = re.compile(
    r"(?:nr|nummer|number|no|id|bestell|order|auftrag|referenz|reference)\b", re.I
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_IBAN = next(pattern.compiled for pattern in PII_PATTERNS if pattern.kind == "iban")


def profile_pdf(path: Path, ctx: ParseContext) -> FileProfile:
    """Profile a PDF: document class, per-page facts, excerpt, key-value and table hints."""

    if ctx.library_missing("pypdf"):
        return ctx.profile
    raw = path.read_bytes()
    if b"%PDF-" not in raw[:1024]:
        return ctx.fail("DF-E-PARSE-FAILED", "the file is not a PDF (no %PDF header)")
    found = [name for name, rule in _ACTIVE if rule.search(raw)]
    if found:
        ctx.flag(
            "DF-W-PDF-ACTIVE-CONTENT", f"the PDF contains {', '.join(found)}; never run or opened"
        )
    from pypdf import PdfReader
    from pypdf.errors import DependencyError

    info = ctx.profile.pdf = PdfInfo()
    try:
        reader = PdfReader(str(path), strict=False)
        info.encrypted = bool(reader.is_encrypted)
        if info.encrypted and not reader.decrypt(""):
            return ctx.fail("DF-E-ENCRYPTED", "the PDF is password protected")
        claimed = _claimed_pages(reader)
        if claimed > ctx.limits.max_pdf_pages:
            return ctx.fail(
                "DF-E-TOO-LARGE", f"{claimed} pages; the limit is {ctx.limits.max_pdf_pages}"
            )
        info.pages = len(reader.pages)
        _read_pages(reader, info, ctx)
        _document_facts(reader, info, ctx)
    except ParseTimeout:
        raise
    except DependencyError:
        return ctx.fail(
            "DF-E-ENCRYPTED", "the PDF uses encryption that needs the cryptography package"
        )
    except Exception as error:  # pypdf raises many types for damaged files
        return ctx.fail(
            "DF-E-PARSE-FAILED", f"the PDF is damaged or cannot be read ({type(error).__name__})"
        )
    return ctx.profile


def _claimed_pages(reader: Any) -> int:
    """What the page tree root claims (`/Count`, `/Kids`), read without walking the tree."""

    try:
        root = _resolve(reader.root_object["/Pages"])
        return max(int(root.get("/Count", 0)), len(_resolve(root.get("/Kids", []))))
    except (KeyError, TypeError, ValueError, AttributeError):
        return 0


def _read_pages(reader: Any, info: PdfInfo, ctx: ParseContext) -> None:
    numbers = [i + 1 for i in spread_indices(info.pages, ctx.limits.max_pdf_pages_profiled)]
    texts: list[tuple[int, str]] = []
    fonts: dict[int, bool] = {}
    hidden: list[int] = []
    unreadable = unusable = 0
    for number in numbers:
        ctx.check_deadline()
        try:
            page = reader.pages[number - 1]
            text = (page.extract_text() or "").strip()
            content = _content(page)
            image = _has_image(page, content)
            _note_fonts(page, fonts)
        except ParseTimeout:
            raise
        except Exception:
            unreadable += 1
            info.per_page.append(PdfPage(page=number))
            continue
        garbled = len(text) >= _MIN_TEXT_CHARS and _garbled(text)
        layer = len(text) >= _MIN_TEXT_CHARS and not garbled
        info.per_page.append(
            PdfPage(
                page=number,
                chars=len(text),
                text_layer=layer,
                image_only=image and not layer,
                tables_hint=_ruled(content),
            )
        )
        if not garbled:
            texts.append((number, text))
        if garbled or (image and not layer):
            unusable += 1
        if _hides_text(content, image, bool(text)):
            hidden.append(number)
    with_text = sum(page.text_layer for page in info.per_page)
    if with_text:
        info.document_class = "mixed" if unusable else "born_digital"
    else:
        info.document_class = "scanned" if unusable else "empty"
    if unusable:
        ctx.flag(
            "DF-W-NO-TEXT-LAYER",
            f"{unusable} of {len(numbers)} analyzed pages have no usable text layer; they need OCR",
        )
    info.fonts_embedded = all(fonts.values()) if fonts else None
    readable = "\n".join(text for _, text in texts)
    info.excerpt = re.sub(r"\n\s*\n+", "\n\n", readable)[:EXCERPT_CHARS]
    info.language = guess_language(readable)
    info.key_values = _key_values(texts, ctx.limits.max_chars_per_cell)
    if hidden:
        ctx.flag("DF-W-HIDDEN-TEXT", f"text is drawn invisibly or in white on pages {hidden[:10]}")
    if unreadable:
        ctx.flag("DF-W-PDF-PAGE-UNREADABLE", f"{unreadable} analyzed pages could not be read")
    if len(numbers) < info.pages:
        ctx.profile.status = "partial"
        ctx.flag(
            "DF-W-SAMPLED",
            f"{len(numbers)} of {info.pages} pages are analyzed (evenly spread, first and last included)",
        )
    ctx.profile.flags.extend(injection_flags(readable, ctx.file_id))


def _note_fonts(page: Any, fonts: dict[int, bool]) -> None:
    for font in list(_resources(page, "/Font").values())[:_MAX_FONTS]:
        if len(fonts) < _MAX_FONTS:
            reference = getattr(font, "idnum", id(font))
            if reference not in fonts:
                try:
                    fonts[reference] = _embedded(font)
                except (KeyError, TypeError, IndexError, AttributeError):
                    fonts[reference] = False


def _document_facts(reader: Any, info: PdfInfo, ctx: ParseContext) -> None:
    limit = ctx.limits.max_chars_per_cell
    try:
        metadata = reader.metadata or {}
        for key, name in _METADATA.items():
            if metadata.get(key) is not None:
                info.metadata[name] = " ".join(str(metadata[key]).split())[:limit]
    except Exception:  # broken document information: the other facts still stand
        info.metadata.clear()
    info.metadata["pdf_version"] = str(reader.pdf_header).removeprefix("%PDF-")[:10]
    try:
        info.form_fields = len(reader.get_fields() or {})
    except Exception:
        info.form_fields = 0
    ctx.profile.flags.extend(injection_flags(" ".join(info.metadata.values()), ctx.file_id))


def _resolve(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def _content(page: Any) -> bytes:
    try:
        contents = page.get_contents()
        return bytes(contents.get_data())[:_MAX_CONTENT_BYTES] if contents is not None else b""
    except ParseTimeout:
        raise
    except Exception:
        return b""


def _resources(page: Any, name: str) -> dict[Any, Any]:
    try:
        return dict(_resolve(_resolve(page["/Resources"])[name]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return {}


def _has_image(page: Any, content: bytes) -> bool:
    if _INLINE_IMAGE.search(content):
        return True
    for value in list(_resources(page, "/XObject").values())[:_MAX_FONTS]:
        image = _resolve(value)
        if hasattr(image, "get") and image.get("/Subtype") == "/Image":
            return True
    return False


def _ruled(content: bytes) -> int:
    """1 when the page draws three or more horizontal rules (a ruled table is likely), else 0."""

    heights: set[int] = set()
    for match in islice(_RECT.finditer(content), _MAX_RULES):
        _, y, width, height = (float(value) for value in match.groups())
        if abs(height) <= 2 and abs(width) >= 20:
            heights.add(round(y))
    for match in islice(_LINE.finditer(content), _MAX_RULES):
        x1, y1, x2, y2 = (float(value) for value in match.groups())
        if round(y1) == round(y2) and abs(x2 - x1) >= 20:
            heights.add(round(y1))
    return int(len(heights) >= 3)


def _hides_text(content: bytes, image: bool, has_text: bool) -> bool:
    """Text in render mode 3, or white text on a page with no picture and no filled shape."""

    if image or not has_text:
        return False
    return bool(_INVISIBLE.search(content)) or bool(
        _WHITE.search(content) and not _FILL.search(content)
    )


def _embedded(font: Any) -> bool:
    font = _resolve(font)
    if font.get("/Subtype") == "/Type3":
        return True
    if "/DescendantFonts" in font:
        font = _resolve(_resolve(font["/DescendantFonts"])[0])
    descriptor = _resolve(font.get("/FontDescriptor") or {})
    return any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))


def _garbled(text: str) -> bool:
    bad = sum(
        "" <= char <= "" or char == "�" or (char < " " and char not in "\n\r\t") for char in text
    )
    return bad > _MAX_GARBLE_SHARE * len(text)


def _key_values(pages: list[tuple[int, str]], limit: int) -> list[KeyValue]:
    """Candidate facts of an invoice-like text: IBANs, dates, amounts, references, fields."""

    found: list[KeyValue] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, key: str | None, value: str, page: int) -> None:
        value = value.strip()[:limit]
        if value and (kind, value) not in seen and len(found) < MAX_KEY_VALUES:
            seen.add((kind, value))
            found.append(
                KeyValue(
                    kind=kind, key=key.strip(" :")[:limit] if key else None, value=value, page=page
                )
            )

    for page, text in pages:
        for line in text.splitlines():
            title = _TITLE.match(line.strip())
            if title:
                add("invoice_number", title[1], title[2], page)
            for segment in (s.strip() for s in _SEGMENTS.split(line)):
                iban = _IBAN.search(segment)
                if iban:
                    kind = "iban" if iban_valid(iban[0]) else "iban_invalid"
                    add(kind, segment[: iban.start()].strip(" :") or None, iban[0], page)
                    continue
                pair = _PAIR.match(segment)
                amount = _AMOUNT.match(segment)
                if amount:
                    add("amount", amount[1], f"{amount[2]} {amount[3]}", page)
                elif pair:
                    key, value = pair[1].strip(), pair[2].strip()
                    add(_field_kind(key, value), key, value, page)
    return found


def _field_kind(key: str, value: str) -> str:
    if _DATE.fullmatch(value):
        return "date"
    if _VAT.fullmatch(value):
        return "vat_id"
    if _EMAIL.fullmatch(value):
        return "email"
    if _REFERENCE.search(key):
        return "reference"
    return "field"
