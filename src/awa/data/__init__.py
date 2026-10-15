"""Data profiles: what awa knows about your files before it designs anything.

Preview: it works and is tested, but its API may still change in a minor release.

`profile()` turns files, folders and globs into a deterministic, offline `DataProfile` with format
decisions and quality flags; files are only read (PDF, Excel and Word need the `files` extra).
"""

from __future__ import annotations

from awa.data._detect import Detection, detect_kind
from awa.data._errors import (
    DataError,
    PathNotAllowedError,
    RowLimitError,
    TooLargeError,
    TooManyFilesError,
    TypeMismatchError,
    UnsafeArchiveError,
    UnsupportedTypeError,
)
from awa.data._flags import FLAGS, fix_hint
from awa.data._infer import infer_column
from awa.data._ingest import IngestedFile, ingest, safe_name
from awa.data._injection import injection_flags
from awa.data._model import (
    ColumnProfile,
    DataIssue,
    DataProfile,
    DataRef,
    DocxInfo,
    FileProfile,
    Flag,
    JsonInfo,
    Limits,
    MailInfo,
    PdfInfo,
    Privacy,
    ProfileEvent,
    Relation,
    SqliteInfo,
    TableProfile,
    TextInfo,
    XlsxInfo,
    column_key,
    make_flag,
)
from awa.data._parse import ParseContext, parsers_override, register_parser, unregister_parser
from awa.data._pii import classify_pii
from awa.data._profile import aprofile, profile
from awa.data._reference import (
    Claim,
    ReferenceCheck,
    check_grounding,
    check_reference_values,
    grounded,
)
from awa.data._relations import relations
from awa.data._render import estimate_tokens, render_for_model
from awa.data._validate import (
    check_case_files,
    check_instruction_text,
    check_outputs,
    check_references,
    check_scan_route,
    reads_pdf_text,
    routes_scans,
)
from awa.data.ports import (
    ColumnValues,
    DataReader,
    Parser,
    UploadScanner,
    ValueSource,
    local_reader,
)

__stability__ = "preview"

__all__ = [
    # profiling (docs/reference/data.md: Profiling)
    "profile",
    "aprofile",
    "DataProfile",
    "FileProfile",
    "TableProfile",
    "ColumnProfile",
    "Flag",
    "Relation",
    "Limits",
    "Privacy",
    "ProfileEvent",
    "FLAGS",
    "fix_hint",
    "column_key",
    "make_flag",
    # reading each kind
    "detect_kind",
    "Detection",
    "infer_column",
    "classify_pii",
    "injection_flags",
    "relations",
    "XlsxInfo",
    "PdfInfo",
    "DocxInfo",
    "MailInfo",
    "JsonInfo",
    "SqliteInfo",
    "TextInfo",
    # bringing files in
    "ingest",
    "IngestedFile",
    "safe_name",
    # what a model sees, and checks against the files
    "render_for_model",
    "estimate_tokens",
    "DataRef",
    "DataIssue",
    "Claim",
    "ReferenceCheck",
    "check_references",
    "check_instruction_text",
    "check_case_files",
    "check_outputs",
    "check_scan_route",
    "check_grounding",
    "check_reference_values",
    "grounded",
    "reads_pdf_text",
    "routes_scans",
    # adding a parser or a reader (the backend protocols)
    "Parser",
    "ParseContext",
    "register_parser",
    "unregister_parser",
    "parsers_override",
    "UploadScanner",
    "DataReader",
    "local_reader",
    "ValueSource",
    "ColumnValues",
    # errors
    "DataError",
    "PathNotAllowedError",
    "RowLimitError",
    "TooLargeError",
    "TooManyFilesError",
    "TypeMismatchError",
    "UnsafeArchiveError",
    "UnsupportedTypeError",
]
