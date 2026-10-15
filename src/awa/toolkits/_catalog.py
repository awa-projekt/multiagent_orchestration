"""The catalog of builtin tools: what exists, what it risks, and how to create it.

`catalog()` returns a `CatalogEntry` per tool (category, toolkit, default risk and approval,
optional extra, required configuration); blueprints reference them as `builtin:<name>`.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from awa._internal.upstream.tools import BaseTool
from awa.errors import ConfigurationError, OptionalDependencyError, ToolNotFoundError
from awa.toolkits._common import extra_installed, toolkits_install_hint
from awa.toolkits.communication import email_toolkit, template_toolkit, webhook_toolkit
from awa.toolkits.data import data_toolkit
from awa.toolkits.documents import document_toolkit
from awa.toolkits.files import file_toolkit
from awa.toolkits.knowledge import knowledge_toolkit
from awa.toolkits.sql import sql_toolkit
from awa.toolkits.utilities import utility_toolkit
from awa.toolkits.web import web_toolkit
from awa.tools.risk import ToolRisk

CATEGORIES = ("web", "files", "data", "documents", "communication", "utilities", "knowledge")
"""The categories of the catalog, in display order."""

ToolkitConfig = Mapping[str, Mapping[str, Any]]
"""Toolkit options by toolkit name, as in a blueprint's `toolkits:` section."""


@dataclass(frozen=True)
class Toolkit:
    """A configuration section: the factory that builds its tools and its fallbacks.

    `selects` says whether the factory accepts `include=`; `inherits` names options taken from
    another section when not set here (`data` and `documents` use `files.root`).
    """

    name: str
    factory: Callable[..., list[BaseTool]]
    selects: bool = True
    inherits: Mapping[str, str] = field(default_factory=dict)

    def options(self) -> set[str]:
        """Keyword options the factory accepts."""

        parameters = inspect.signature(self.factory).parameters
        return {name for name in parameters if name != "include"}

    def resolved(self, config: ToolkitConfig | None) -> dict[str, Any]:
        """This section's options, with inherited ones filled in."""

        sections = config or {}
        options = dict(sections.get(self.name) or {})
        for option, source in self.inherits.items():
            if option not in options and option in (sections.get(source) or {}):
                options[option] = sections[source][option]
        return options


@dataclass(frozen=True)
class CatalogEntry:
    """One builtin tool of the catalog.

    Attributes:
        required: Options it needs; each inner tuple is satisfied by any one of its options
            (`("allowed_hosts", "base_url")`).
        module: The optional module the extra provides.
        keywords: Task words that call for the tool (multi-word ones match as a phrase), for
            `awa.blueprints.auto`; empty: its name words.
    """

    name: str
    summary: str
    category: str
    toolkit: Toolkit
    risk: ToolRisk
    requires_approval: bool = False
    extra: str | None = None
    required: tuple[tuple[str, ...], ...] = ()
    module: str | None = None
    keywords: tuple[str, ...] = ()

    @property
    def reference(self) -> str:
        """The blueprint reference, `builtin:<name>`."""

        return f"builtin:{self.name}"

    @property
    def installed(self) -> bool:
        """Whether the optional dependency (if any) is installed."""

        return self.module is None or extra_installed(self.module)

    def risk_for(self, config: ToolkitConfig | None = None) -> ToolRisk:
        """The tool's risk under `config` (an allowlist or read-only mode lowers it)."""

        options = self.toolkit.resolved(config)
        if self.name in ("fetch_url", "verify_quote") and options.get("allowed_hosts"):
            return self.risk.model_copy(update={"external_action": False})
        return self.risk

    def approval_for(self, config: ToolkitConfig | None = None) -> bool:
        """Whether the tool created from `config` waits for approval."""

        return (
            self.requires_approval
            and self.toolkit.resolved(config).get("requires_approval") is not False
        )

    def problems(self, config: ToolkitConfig | None = None) -> list[str]:
        """Why the tool cannot be created from `config` (empty when it can).

        Checks the options offline: unknown options, missing required
        options and a missing optional extra. Values are validated when the
        tool is created.
        """

        issues: list[str] = []
        section = (config or {}).get(self.toolkit.name)
        if section is not None and not isinstance(section, Mapping):
            return [f"toolkits.{self.toolkit.name} must be a mapping of options"]
        options = self.toolkit.resolved(config)
        for alternatives in self.required:
            if not any(options.get(option) is not None for option in alternatives):
                wanted = " or ".join(alternatives)
                also = [
                    f"toolkits.{self.toolkit.inherits[option]}.{option}"
                    for option in alternatives
                    if option in self.toolkit.inherits
                ]
                issues.append(
                    f"{self.name} needs toolkits.{self.toolkit.name}.{wanted}"
                    + (f" (or {', '.join(also)})" if also else "")
                    + " to be configured"
                )
        if not self.installed:
            issues.append(f"{self.name} needs the {self.extra} extra: {toolkits_install_hint()}")
        return issues

    def create(
        self,
        config: ToolkitConfig | None = None,
        *,
        requires_approval: bool | None = None,
        **options: Any,
    ) -> BaseTool:
        """Create the tool from toolkit configuration and/or keyword options.

        Args:
            config: Options by toolkit name (a blueprint's `toolkits:`).
            requires_approval: Override the approval default (`None` keeps it).
            **options: Options of this tool's toolkit; they win over `config`.

        Raises:
            ConfigurationError: If required options are missing or invalid.
            OptionalDependencyError: If the tool's optional extra is missing.
        """

        own = self._switched(config, requires_approval)
        if own is not None:
            return own
        merged = {**self.toolkit.resolved(config), **options}
        if requires_approval is not None and "requires_approval" in self.toolkit.options():
            merged["requires_approval"] = requires_approval
        missing = [
            " or ".join(group)
            for group in self.required
            if not any(merged.get(option) is not None for option in group)
        ]
        if missing:
            raise ConfigurationError(
                f"{self.name} needs the option(s) {', '.join(missing)} of the "
                f"{self.toolkit.name} toolkit"
            )
        if not self.installed:
            raise OptionalDependencyError(
                f"{self.name} needs the {self.extra} extra; install it with {toolkits_install_hint()}"
            )
        unknown = sorted(set(merged) - self.toolkit.options())
        if unknown:
            raise ConfigurationError(
                f"unknown option(s) {', '.join(unknown)} for the {self.toolkit.name} toolkit "
                f"(known: {', '.join(sorted(self.toolkit.options()))})"
            )
        if self.toolkit.selects:
            merged["include"] = [self.name]
        try:
            tools = self.toolkit.factory(**merged)
        except TypeError as exc:
            raise ConfigurationError(f"invalid options for {self.name}: {exc}") from None
        for tool in tools:
            if tool.name == self.name or len(tools) == 1:
                return tool
        raise ConfigurationError(f"the {self.toolkit.name} toolkit did not build {self.name}")

    @property
    def switched_off(self) -> str | None:
        """`tool.<name>` or `toolkit.<section>` when the project switched it off, else `None`."""

        from awa.plugins.switches import is_off

        for key in (f"toolkit.{self.toolkit.name}", f"tool.{self.name}"):
            if is_off(key):
                return key
        return None

    def _switched(
        self, config: ToolkitConfig | None, requires_approval: bool | None
    ) -> BaseTool | None:
        """The project's own tool when `tool.<name>` is replaced; refuses a switched-off one."""
        from awa.core.config import current
        from awa.plugins import replacement_for
        from awa.tools.approval import create_tool_approval_wrapper

        if (off := self.switched_off) is not None:
            raise ConfigurationError(f"tool {self.name} is switched off ({off}); see components:")
        if (own := replacement_for("tools", self.name, project=current().project_dir)) is None:
            return None
        if len(own) != 1:
            raise ConfigurationError(f"the replacement of tool {self.name} must be one tool")
        tool: BaseTool = own[0]
        wait = self.approval_for(config) if requires_approval is None else requires_approval
        return create_tool_approval_wrapper(tool) if wait else tool


class ToolCatalog:
    """The builtin tools, searchable by name, words and category."""

    def __init__(self, entries: Sequence[CatalogEntry]) -> None:
        self._entries = {entry.name: entry for entry in entries}
        if len(self._entries) != len(entries):
            raise ConfigurationError("two catalog entries share a name")

    def __iter__(self) -> Iterator[CatalogEntry]:
        """Iterate over the entries, by category then name."""

        return iter(self.entries())

    def __len__(self) -> int:
        """Return the number of builtin tools."""

        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        """Return whether a builtin tool is named `name`."""

        return name in self._entries

    def entries(self) -> list[CatalogEntry]:
        """Every entry, ordered by category (as in `CATEGORIES`) then catalog order."""

        order = {category: index for index, category in enumerate(CATEGORIES)}
        return sorted(self._entries.values(), key=lambda entry: order.get(entry.category, 99))

    def names(self) -> list[str]:
        """The names of every builtin tool, in `entries()` order."""

        return [entry.name for entry in self.entries()]

    def toolkits(self) -> dict[str, Toolkit]:
        """The configuration sections, by name."""

        return {entry.toolkit.name: entry.toolkit for entry in self.entries()}

    def get(self, name: str) -> CatalogEntry:
        """Return the entry of the builtin tool `name` (a `builtin:` prefix is accepted).

        Raises:
            ToolNotFoundError: If there is no such builtin tool.
        """

        key = name.removeprefix("builtin:")
        try:
            return self._entries[key]
        except KeyError:
            raise ToolNotFoundError(
                f"Unknown builtin tool {name!r} (known: {', '.join(self.names())})"
            ) from None

    def find(self, query: str) -> list[CatalogEntry]:
        """Return the entries matching the words of `query`, best matches first.

        A word matches the name, summary, category or toolkit; entries that
        match more words, or match in the name, rank higher.
        """

        words = [word for word in query.lower().replace("_", " ").split() if word]
        if not words:
            return self.entries()
        scored: list[tuple[int, int, CatalogEntry]] = []
        for position, entry in enumerate(self.entries()):
            name = entry.name.replace("_", " ")
            text = f"{entry.summary} {entry.category} {entry.toolkit.name}".lower()
            score = sum(3 if word in name else int(word in text) for word in words)
            if score:
                scored.append((-score, position, entry))
        return [entry for _, _, entry in sorted(scored, key=lambda item: item[:2])]

    def by_category(self) -> dict[str, list[CatalogEntry]]:
        """The entries grouped by category, in `CATEGORIES` order."""

        groups: dict[str, list[CatalogEntry]] = {}
        for entry in self.entries():
            groups.setdefault(entry.category, []).append(entry)
        return groups

    def create(
        self,
        name: str,
        config: ToolkitConfig | None = None,
        *,
        requires_approval: bool | None = None,
        **options: Any,
    ) -> BaseTool:
        """Create the builtin tool `name`; see `CatalogEntry.create`.

        Raises:
            ToolNotFoundError: If there is no such builtin tool.
            ConfigurationError: If its configuration is missing or invalid.
            OptionalDependencyError: If its optional extra is missing.
        """

        return self.get(name).create(config, requires_approval=requires_approval, **options)

    def config_problems(self, config: ToolkitConfig) -> list[tuple[str, str]]:
        """Check a `toolkits:` section offline; returns `(path, message)` pairs.

        Reports unknown sections, sections that are not mappings, and
        unknown options.
        """

        problems: list[tuple[str, str]] = []
        known = self.toolkits()
        for section, options in config.items():
            path = f"toolkits.{section}"
            toolkit = known.get(section)
            if toolkit is None:
                problems.append(
                    (path, f"unknown toolkit {section!r} (known: {', '.join(sorted(known))})")
                )
                continue
            if not isinstance(options, Mapping):
                problems.append((path, "must be a mapping of options"))
                continue
            for option in options:
                if option not in toolkit.options():
                    problems.append(
                        (
                            f"{path}.{option}",
                            f"unknown option {option!r} of the {section} toolkit (known: "
                            f"{', '.join(sorted(toolkit.options()))})",
                        )
                    )
        return problems


_WEB = Toolkit("web", web_toolkit)
_FILES = Toolkit("files", file_toolkit)
_DATA = Toolkit("data", data_toolkit, inherits={"root": "files"})
_SQL = Toolkit("sql", sql_toolkit)
_DOCUMENTS = Toolkit("documents", document_toolkit, inherits={"root": "files"})
_EMAIL = Toolkit("email", email_toolkit, selects=False)
_WEBHOOK = Toolkit("webhook", webhook_toolkit, selects=False)
_TEMPLATES = Toolkit("templates", template_toolkit, selects=False)
_UTILITIES = Toolkit("utilities", utility_toolkit)
_KNOWLEDGE = Toolkit("knowledge", knowledge_toolkit, selects=False)


def _risk(*flags: str) -> ToolRisk:
    return ToolRisk(
        untrusted_input="untrusted_input" in flags,
        private_data="private_data" in flags,
        external_action="external_action" in flags,
    )


_ROOT = (("root",),)
_EXTRA = "files"

_ENTRIES = (
    CatalogEntry(
        "fetch_url",
        "Read a web page as plain text (scripts, styles, hidden elements removed).",
        "web",
        _WEB,
        _risk("untrusted_input", "external_action"),
        keywords=(
            *("web page", "webpage", "website", "url", "urls", "scrape", "link"),
            *("internet", "online research", "web research", "research online"),
        ),
    ),
    CatalogEntry(
        "web_search",
        "Search the web (built-in, no API key; or your own search API); title, URL, snippet.",
        "web",
        _WEB,
        _risk("untrusted_input"),
        keywords=(
            *("web search", "search web", "internet", "search online", "online research"),
            *("web research", "research online", "look up online"),
        ),
    ),
    CatalogEntry(
        "verify_quote",
        "Check that a quote occurs on a web page (VERIFIED or NOT FOUND) before citing it.",
        "web",
        _WEB,
        _risk("untrusted_input", "external_action"),
        keywords=("citation", "citations", "cite sources", "verify quote"),
    ),
    CatalogEntry(
        "list_files",
        "List files and folders inside the sandboxed root directory.",
        "files",
        _FILES,
        _risk("private_data"),
        required=_ROOT,
        keywords=("folder", "directory", "files"),
    ),
    CatalogEntry(
        "read_file",
        "Read a text file inside the root (line window, size cap).",
        "files",
        _FILES,
        _risk("untrusted_input", "private_data"),
        required=_ROOT,
        keywords=("read file", "text file", "files"),
    ),
    CatalogEntry(
        "write_file",
        "Write a text file atomically inside the root.",
        "files",
        _FILES,
        _risk("external_action"),
        requires_approval=True,
        required=_ROOT,
        keywords=("write file", "save file", "save to file"),
    ),
    CatalogEntry(
        "append_file",
        "Append text to a file inside the root.",
        "files",
        _FILES,
        _risk("external_action"),
        requires_approval=True,
        required=_ROOT,
        keywords=("append", "log file"),
    ),
    CatalogEntry(
        "move_file",
        "Move or rename a file or folder inside the root.",
        "files",
        _FILES,
        _risk("external_action"),
        requires_approval=True,
        required=_ROOT,
        keywords=("move file", "rename file", "archive file"),
    ),
    CatalogEntry(
        "delete_file",
        "Delete one file inside the root.",
        "files",
        _FILES,
        _risk("external_action"),
        requires_approval=True,
        required=_ROOT,
        keywords=("delete file", "remove file"),
    ),
    CatalogEntry(
        "read_csv",
        "Read the columns and a window of rows of a CSV file.",
        "data",
        _DATA,
        _risk("untrusted_input", "private_data"),
        required=_ROOT,
        keywords=("csv", "spreadsheet"),
    ),
    CatalogEntry(
        "query_csv",
        "Filter, group, aggregate and sort a CSV file with a safe expression language.",
        "data",
        _DATA,
        _risk("untrusted_input", "private_data"),
        required=_ROOT,
        keywords=("csv", "filter rows", "aggregate", "group by"),
    ),
    CatalogEntry(
        "write_csv",
        "Write or append rows to a CSV file (formula cells escaped).",
        "data",
        _DATA,
        _risk("external_action"),
        requires_approval=True,
        required=_ROOT,
        keywords=("write csv", "export csv", "save csv"),
    ),
    CatalogEntry(
        "read_excel",
        "Read a sheet of an .xlsx workbook.",
        "data",
        _DATA,
        _risk("untrusted_input", "private_data"),
        extra=_EXTRA,
        required=_ROOT,
        module="openpyxl",
        keywords=("excel", "xlsx", "workbook"),
    ),
    CatalogEntry(
        "query_excel",
        "Filter, group, aggregate and sort a sheet of an .xlsx workbook.",
        "data",
        _DATA,
        _risk("untrusted_input", "private_data"),
        extra=_EXTRA,
        required=_ROOT,
        module="openpyxl",
        keywords=("excel", "xlsx", "spreadsheet totals", "sum excel"),
    ),
    CatalogEntry(
        "fill_excel",
        "Add columns to an .xlsx sheet and fill them per row matched by a key; saves a new "
        "workbook (formatting kept, source cells never changed).",
        "data",
        _DATA,
        _risk("private_data", "external_action"),
        requires_approval=True,
        extra=_EXTRA,
        required=_ROOT,
        module="openpyxl",
        keywords=(
            "add columns",
            "enrich excel",
            "fill excel",
            "update excel",
            "write excel",
            "excel output",
            "match by key",
        ),
    ),
    CatalogEntry(
        "json_query",
        "Extract values from JSON text or a .json/.jsonl file with paths and filters.",
        "data",
        _DATA,
        _risk("untrusted_input", "private_data"),
        keywords=("json", "payload"),
    ),
    CatalogEntry(
        "sql_schema",
        "Describe a database: tables, columns, keys, approximate sizes (read-only catalog).",
        "data",
        _SQL,
        _risk("untrusted_input", "private_data"),
        required=(("database", "connect"),),
        keywords=("database schema", "table structure", "list tables", "tables and columns"),
    ),
    CatalogEntry(
        "sql_query",
        "Run a read-only SQL query (SQLite enforced read-only; other DBs via connect=).",
        "data",
        _SQL,
        _risk("untrusted_input", "private_data"),
        required=(("database", "connect"),),
        keywords=("sql", "database", "query database"),
    ),
    CatalogEntry(
        "sql_execute",
        "Run one SQL statement that changes the database.",
        "data",
        _SQL,
        _risk("private_data", "external_action"),
        requires_approval=True,
        required=(("database", "connect"),),
        keywords=("update database", "insert row", "sql write"),
    ),
    CatalogEntry(
        "extract_pdf_text",
        "Extract the text of a PDF, page by page; scanned pages (no text layer) through an "
        "OCR hook such as a vision model.",
        "documents",
        _DOCUMENTS,
        _risk("untrusted_input", "private_data"),
        extra=_EXTRA,
        required=_ROOT,
        module="pypdf",
        keywords=("pdf", "pdfs", "scan", "scanned", "ocr", "image-only pdf"),
    ),
    CatalogEntry(
        "extract_pdfs_text",
        "Extract the text of many PDFs in one call (paths or patterns); scanned pages of all "
        "files are read in parallel through the OCR hook.",
        "documents",
        _DOCUMENTS,
        _risk("untrusted_input", "private_data"),
        extra=_EXTRA,
        required=_ROOT,
        module="pypdf",
        keywords=("many pdfs", "all pdfs", "batch pdf", "scans folder", "read all scans"),
    ),
    CatalogEntry(
        "extract_docx_text",
        "Extract the text of a Word document, with headings and tables.",
        "documents",
        _DOCUMENTS,
        _risk("untrusted_input", "private_data"),
        extra=_EXTRA,
        required=_ROOT,
        module="docx",
        keywords=("docx", "word document"),
    ),
    CatalogEntry(
        "chunk_text",
        "Split a long text into overlapping chunks.",
        "documents",
        _DOCUMENTS,
        _risk(),
        keywords=("chunk", "long text", "split text"),
    ),
    CatalogEntry(
        "summarize_stats",
        "Count words, sentences and paragraphs; list frequent words.",
        "documents",
        _DOCUMENTS,
        _risk(),
        keywords=("word count", "text statistics", "word frequency"),
    ),
    CatalogEntry(
        "send_email",
        "Send an email over SMTP (STARTTLS; server and credentials from env vars).",
        "communication",
        _EMAIL,
        _risk("external_action"),
        requires_approval=True,
        keywords=("email", "e-mail", "mail", "emails"),
    ),
    CatalogEntry(
        "post_webhook",
        "Post a message or JSON payload to a webhook (chat channels, automations).",
        "communication",
        _WEBHOOK,
        _risk("external_action"),
        requires_approval=True,
        required=(("url", "url_env"),),
        keywords=("webhook", "slack", "chat channel", "teams channel"),
    ),
    CatalogEntry(
        "render_template",
        "Fill $placeholders of a text template (nothing is evaluated).",
        "communication",
        _TEMPLATES,
        _risk(),
        keywords=("template", "templates", "placeholder", "form letter"),
    ),
    CatalogEntry(
        "calculator",
        "Evaluate arithmetic exactly (safe parser, no eval).",
        "utilities",
        _UTILITIES,
        _risk(),
        keywords=("calculate", "calculation", "arithmetic", "totals", "total", "sum", "vat"),
    ),
    CatalogEntry(
        "datetime_now",
        "Current date and time in a time zone.",
        "utilities",
        _UTILITIES,
        _risk(),
        keywords=("today", "current date", "current time", "timestamp"),
    ),
    CatalogEntry(
        "date_math",
        "Add durations, date differences and business days (holidays aware).",
        "utilities",
        _UTILITIES,
        _risk(),
        keywords=("due date", "deadline", "deadlines", "business day", "overdue", "days between"),
    ),
    CatalogEntry(
        "search_knowledge",
        "Search a folder of documents (BM25, optional embeddings) and cite sources.",
        "knowledge",
        _KNOWLEDGE,
        _risk("untrusted_input", "private_data"),
        required=(("path",),),
        keywords=("knowledge base", "handbook", "faq", "manual", "policy documents"),
    ),
)

_CATALOG = ToolCatalog(_ENTRIES)


def builtin_tools() -> list[tuple[str, CatalogEntry, str]]:
    """The built-in tools as `(name, entry, summary)` rows, the provider of the plugin registry."""
    return [(entry.name, entry, entry.summary) for entry in catalog().entries()]


def catalog() -> ToolCatalog:
    """Return the catalog of builtin tools.

    Example:
        ```python
        for entry in catalog().find("pdf"):
            print(entry.name, entry.summary, entry.extra)
        ```
    """

    return _CATALOG
