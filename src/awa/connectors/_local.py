"""Connectors wrapping the builtin toolkits and non-HTTP protocols (`workspace`, `sql`, `web`, ...).

Their tools are named and gated like every connector tool; variables are read when they are built.
"""

from __future__ import annotations

import base64
import contextlib
import email
import email.policy
import imaplib
import importlib
import importlib.util
import json
import re
import smtplib
import ssl
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, cast

from pydantic import Field, SecretStr

from awa._internal.install import install_hint
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.connectors._engine import CONNECTOR_METADATA_KEY, READ_RISK, ConnectorContext, tool_name
from awa.connectors._spec import ConnectorSpec, EnvVar, Operation
from awa.connectors._transfer import workspace_root
from awa.errors import ConfigurationError, OptionalDependencyError
from awa.kernel.redaction import redact_values
from awa.toolkits import DuckDuckGoSearchProvider, JSONSearchProvider, SearchProvider
from awa.toolkits._common import ToolArgs, build_tool, tool_json
from awa.tools.approval import APPROVAL_METADATA_KEY, create_tool_approval_wrapper

_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def require_configured(ctx: ConnectorContext) -> None:
    """Raise unless every required variable is set.

    Raises:
        ConfigurationError: Naming the missing variables (never values).
    """

    missing = ctx.missing()
    if missing:
        raise ConfigurationError(f"{ctx.spec.title} is not configured: set {', '.join(missing)}")


def _flag(ctx: ConnectorContext, name: str, default: bool = False) -> bool:
    return bool(ctx.option(name, default))


def _selected(
    ctx: ConnectorContext, available: Callable[[Operation], str | None]
) -> list[Operation]:
    """The instance's operations; unavailable ones are dropped, or refused when asked for."""

    explicit = ctx.option("include") is not None
    chosen = []
    for operation in ctx.operations():
        problem = available(operation)
        if problem is None:
            chosen.append(operation)
        elif explicit:
            raise ConfigurationError(f"{ctx.name}.{operation.name}: {problem}")
    return chosen


_WORKSPACE: dict[str, tuple[str, str]] = {
    "list_files": ("files", "list_files"),
    "read_file": ("files", "read_file"),
    "write_file": ("files", "write_file"),
    "append_file": ("files", "append_file"),
    "read_csv": ("data", "read_csv"),
    "query_csv": ("data", "query_csv"),
    "write_csv": ("data", "write_csv"),
    "read_excel": ("data", "read_excel"),
    "query_excel": ("data", "query_excel"),
    "fill_excel": ("data", "fill_excel"),
    "read_pdf": ("documents", "extract_pdf_text"),
    "read_pdfs": ("documents", "extract_pdfs_text"),
    "read_docx": ("documents", "extract_docx_text"),
    "search": ("knowledge", "search_knowledge"),
    "schema": ("sql", "sql_schema"),
    "query": ("sql", "sql_query"),
    "execute": ("sql", "sql_execute"),
}
_TABLE_READERS = frozenset({"read_csv", "query_csv", "read_excel", "query_excel"})
"""The tools that also get the `description` option (tables and columns), as the SQL tools do."""


def workspace_database(ctx: ConnectorContext) -> Path | None:
    """The SQLite file: the `database` option or `AWA_WORKSPACE_DB` (relative to the
    folder), else the only `.db`/`.sqlite` file in the folder."""

    root = workspace_root(ctx)
    configured = ctx.option("database") or ctx.value("AWA_WORKSPACE_DB")
    if configured:
        return root / Path(str(configured))  # an absolute path stays as it is
    found = sorted(p for p in root.glob("*") if p.suffix.lower() in _DATABASE_SUFFIXES)
    return found[0] if len(found) == 1 else None


_CREATE_TABLE = re.compile(r"\s*CREATE\s+TABLE\s", re.IGNORECASE)


def _ensure_database(ctx: ConnectorContext, database: Path | None) -> None:
    """Create a missing SQLite file from the `schema` option (`CREATE TABLE` statements only)."""

    import sqlite3

    schema = ctx.option("schema")
    if schema is None or schema == []:
        return
    if not isinstance(schema, list) or not all(isinstance(item, str) for item in schema):
        raise ConfigurationError("the schema option is a list of CREATE TABLE statements")
    bad = [item for item in schema if not _CREATE_TABLE.match(item)]
    if bad:
        raise ConfigurationError(
            f"the schema option takes CREATE TABLE statements only, got {bad[0][:40]!r}"
        )
    if database is None:
        raise ConfigurationError(
            "the schema option needs the database option (or AWA_WORKSPACE_DB): "
            "name the SQLite file to create, for example database: workspace.db"
        )
    if database.is_file():
        return
    if not database.resolve().is_relative_to(workspace_root(ctx).resolve()):
        raise ConfigurationError(
            f"the schema option creates the database inside the workspace folder only, not "
            f"{str(database)!r}"
        )
    try:
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            for statement in schema:
                connection.execute(statement)
    except sqlite3.Error as exc:
        database.unlink(missing_ok=True)  # no half-made database stays
        raise ConfigurationError(f"the schema option failed: {exc}") from None


def _workspace_tools(ctx: ConnectorContext) -> list[BaseTool]:
    from awa.toolkits import catalog
    from awa.toolkits.data import data_toolkit
    from awa.toolkits.documents import document_toolkit
    from awa.toolkits.files import file_toolkit
    from awa.toolkits.knowledge import knowledge_toolkit
    from awa.toolkits.sql import sql_toolkit

    root = workspace_root(ctx)
    if not root.is_dir():
        raise ConfigurationError(
            f"the workspace folder {str(root)!r} does not exist; create it or set the root option"
        )
    database = workspace_database(ctx)
    _ensure_database(ctx, database)
    entries = catalog()

    def available(operation: Operation) -> str | None:
        group, name = _WORKSPACE[operation.name]
        if group == "sql" and (database is None or not database.is_file()):
            return (
                "no SQLite database in the workspace; set the database option (or AWA_WORKSPACE_DB)"
            )
        entry = entries.get(name)
        return None if entry.installed else f"needs the {entry.extra} extra"

    operations = _selected(ctx, available)
    wanted: dict[str, list[str]] = {}
    for operation in operations:
        group, name = _WORKSPACE[operation.name]
        wanted.setdefault(group, []).append(name)
    about = str(ctx.option("description") or "").strip()
    toolkits: dict[str, Callable[[list[str]], list[BaseTool]]] = {
        "files": lambda names: file_toolkit(root, include=names, requires_approval=False),
        "data": lambda names: data_toolkit(
            root,
            include=names,
            requires_approval=False,
            source_root=str(ctx.option("sources")) if ctx.option("sources") else None,
        ),
        "documents": lambda names: document_toolkit(root, include=names, ocr=_ocr(ctx)),
        "knowledge": lambda names: knowledge_toolkit(root),
        "sql": lambda names: sql_toolkit(
            cast(Path, database),  # `available` refused sql operations without a database
            include=names,
            requires_approval=False,
            description=about or None,
        ),
    }
    built = {tool.name: tool for group, names in wanted.items() for tool in toolkits[group](names)}
    for name in _TABLE_READERS.intersection(built) if about else ():
        text = f"{built[name].description} {about}"
        built[name] = built[name].model_copy(update={"description": text})
    return [adopt(ctx, built[_WORKSPACE[op.name][1]], op) for op in operations]


def _ocr(ctx: ConnectorContext) -> Any:
    """The OCR hook for scanned PDF pages from the `ocr` and `ocr_concurrency` options, or none."""

    from awa.toolkits import vision_ocr

    choice = ctx.option("ocr", True)
    if choice is False or str(choice).lower() in ("false", "off", "none", "no"):
        return None
    try:
        concurrency = int(ctx.option("ocr_concurrency", 4))
    except (TypeError, ValueError):
        raise ConfigurationError("ocr_concurrency is a whole number of pages") from None
    model = None if choice is True or str(choice).lower() == "true" else str(choice)
    return vision_ocr(model, max_concurrency=concurrency)


def adopt(ctx: ConnectorContext, tool: BaseTool, operation: Operation) -> BaseTool:
    """`tool` renamed to `<instance>_<operation>`, tagged with its reference, gated if a write."""

    metadata = {**(tool.metadata or {}), CONNECTOR_METADATA_KEY: f"{ctx.name}.{operation.name}"}
    metadata.pop(APPROVAL_METADATA_KEY, None)
    gated = ctx.requires_approval(operation)
    description = tool.description + (" Needs human approval." if gated else "")
    renamed = tool.model_copy(
        update={
            "name": tool_name(ctx.name, operation.name),
            "description": description,
            "metadata": metadata,
        }
    )
    if not gated:
        return renamed
    wrapped = create_tool_approval_wrapper(renamed)
    return wrapped.model_copy(
        update={
            "metadata": {**metadata, APPROVAL_METADATA_KEY: True},
            "tags": list(renamed.tags or ()),
        }
    )


def _workspace_check(ctx: ConnectorContext) -> str:
    import sqlite3

    root = workspace_root(ctx)
    if not root.is_dir():
        raise ToolException(
            f"the workspace folder {str(root)!r} does not exist; fix: create it "
            f"(mkdir {root.name}) or set AWA_WORKSPACE_DIR to an existing folder"
        )
    files = sum(1 for p in root.rglob("*") if p.is_file())
    message = f"folder {root} with {files} file(s)"
    database = workspace_database(ctx)
    if database is not None and database.is_file():
        with contextlib.closing(
            sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            tables = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        message += f"; database {database.name} with {tables} table(s)"
    return message


WORKSPACE = ConnectorSpec(
    name="workspace",
    title="Local workspace",
    category="data",
    summary=(
        "A local project folder: files, CSV and Excel tables, PDF and Word documents, document "
        "search and a SQLite database (sample data until the external systems are connected)."
    ),
    env=(
        EnvVar(
            "AWA_WORKSPACE_DIR",
            "Folder with the data.",
            secret=False,
            required=False,
            default="data",
        ),
        EnvVar("AWA_WORKSPACE_DB", "SQLite file in that folder.", secret=False, required=False),
    ),
    operations=(
        Operation("list_files", "List files and folders."),
        Operation("read_file", "Read a text file."),
        Operation("read_csv", "Read the columns and rows of a CSV file."),
        Operation("query_csv", "Filter, group, aggregate and sort a CSV file."),
        Operation("read_excel", "Read a sheet of an Excel workbook."),
        Operation("query_excel", "Filter, group, aggregate and sort an Excel sheet."),
        Operation(
            "read_pdf", "Extract the text of a PDF; scanned pages are read by a vision model."
        ),
        Operation(
            "read_pdfs",
            "Extract the text of many PDFs in one call (paths or patterns like scans/*.pdf); "
            "scanned pages are read in parallel.",
        ),
        Operation("read_docx", "Extract the text of a Word document."),
        Operation("search", "Search the folder's documents and cite sources."),
        Operation("schema", "Describe the SQLite database: tables, columns, keys."),
        Operation("query", "Run a read-only SQL query on the SQLite database."),
        Operation("write_file", "Write a text file.", write=True),
        Operation("append_file", "Append text to a file.", write=True),
        Operation("write_csv", "Write or append rows to a CSV file.", write=True),
        Operation("execute", "Run one SQL statement that changes the database.", write=True),
        Operation(
            "fill_excel",
            "Add columns to an Excel sheet and fill them per row matched by a key (e.g. an "
            "invoice number); saved as a new workbook, the source is never changed; keys in no "
            "row are kept on its sheet 'Unmatched keys'. Call it once with all rows.",
            write=True,
        ),
    ),
    factory=_workspace_tools,
    check=_workspace_check,
    options=("root", "database", "description", "schema", "sources", "ocr", "ocr_concurrency"),
    setup=(
        "Put sample files (and optionally a SQLite database) in the project's data/ folder; no "
        "credentials are needed. Scanned PDF pages are read by the default model's vision (the ocr "
        "option or AWA_VISION_MODEL names another model, e.g. ocr: fast; ocr: false turns it off; "
        "ocr_concurrency bounds the pages read at once, default 4). The sources option is the "
        "folder fill_excel reads its source workbook from. The schema option (a list of CREATE "
        "TABLE statements) creates the database in its folder when it does not exist yet."
    ),
)


def _redacting(connect: Callable[[], Any], *secrets: str) -> Callable[[], Any]:
    def guarded() -> Any:
        try:
            return connect()
        except Exception as exc:
            text = redact_values(
                f"{type(exc).__name__}: {exc}", list(secrets), replacement="<DATABASE_URL>"
            )
            raise ToolException(f"cannot connect to the database: {text}") from None

    return guarded


_DIALECTS = {
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "mysql": "mysql",
    "mysql+pymysql": "mysql",
    "mariadb": "mysql",
    "mssql": "mssql",
    "mssql+pymssql": "mssql",
    "sqlserver": "mssql",
}
_DRIVERS = {  # dialect: (module, extra, name)
    "postgresql": ("psycopg", "postgres", "PostgreSQL"),
    "mysql": ("pymysql", "sql", "MySQL"),
    "mssql": ("pymssql", "sql", "SQL Server"),
}


def database_url(ctx: ConnectorContext) -> tuple[str, str, Path | None]:
    """`(dialect, url, SQLite path)` of DATABASE_URL."""

    require_configured(ctx)
    return _parse_database_url(ctx.require("DATABASE_URL"))


def _parse_database_url(url: str) -> tuple[str, str, Path | None]:
    lowered = url.lower()
    if lowered.startswith("sqlite:///"):
        return "sqlite", url, Path(url[len("sqlite:///") :])
    if "://" not in url and lowered.endswith(_DATABASE_SUFFIXES):
        return "sqlite", url, Path(url)
    dialect = _DIALECTS.get(lowered.partition("://")[0])
    if dialect is None:
        raise ConfigurationError(
            "DATABASE_URL must start with sqlite:///, postgresql://, mysql:// or mssql://; "
            "for other databases use awa.toolkits.sql_toolkit(connect=...) in Python"
        )
    return dialect, url, None


def _driver_hint(dialect: str) -> str:
    module, extra, _ = _DRIVERS[dialect]
    return f"{module}: {install_hint(extra)}"


def describe_database(url: str) -> tuple[str, str | None]:
    """`(kind, problem)` of a database address: its kind (`"PostgreSQL"`, ...) and what keeps
    it from working here (a missing driver or file, an unknown scheme), else `None`.

    Nothing is connected to and the address is never part of the result."""

    try:
        dialect, _, path = _parse_database_url(url)
    except ConfigurationError as exc:
        return "unknown", str(exc)
    if path is not None:
        return "SQLite", None if path.is_file() else f"the file {path.name} does not exist"
    kind = _DRIVERS[dialect][2]
    if importlib.util.find_spec(_DRIVERS[dialect][0]) is None:
        return kind, f"needs {_driver_hint(dialect)}"
    return kind, None


_STATEMENT_SECONDS = 30
_CHECK_NAMES = 25
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def database_connector(dialect: str, url: str, *, read_only: bool) -> Callable[[], Any]:
    """A function opening a time-limited connection; TLS is verified for a non-loopback host."""

    module, _, name = _DRIVERS[dialect]
    try:
        driver = importlib.import_module(module)
    except ImportError:
        raise OptionalDependencyError(
            f"a {name} DATABASE_URL needs {_driver_hint(dialect)}"
        ) from None
    parts = urllib.parse.urlsplit(url)
    options = dict(urllib.parse.parse_qsl(parts.query))
    user = urllib.parse.unquote(parts.username or "")
    password = urllib.parse.unquote(parts.password or "")
    database = urllib.parse.unquote(parts.path.lstrip("/"))

    remote = (parts.hostname or "localhost").lower() not in _LOOPBACK
    tls = remote and options.get("tls") != "off"

    def connect() -> Any:
        if dialect == "postgresql":
            settings = {key: value for key, value in options.items() if key != "tls"}
            if tls and "sslmode" not in settings:
                settings["sslmode"] = "verify-full"
                settings.setdefault("sslrootcert", "system")  # the system's certificate store
            connection = driver.connect(
                urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(settings))),
                connect_timeout=10,
                options=f"-c statement_timeout={_STATEMENT_SECONDS * 1000}",
            )
            connection.read_only = read_only
            return connection
        if dialect == "mysql":
            connection = driver.connect(
                host=parts.hostname or "localhost",
                port=parts.port or 3306,
                user=user,
                password=password,
                database=database or None,
                charset=options.get("charset", "utf8mb4"),
                connect_timeout=10,
                read_timeout=_STATEMENT_SECONDS,
                ssl_ca=options.get("ssl_ca"),
                ssl_verify_cert=options.get("ssl_verify_cert", "true" if tls else "") == "true"
                or None,
                ssl_verify_identity=options.get("ssl_verify_identity", "true" if tls else "")
                == "true"
                or None,
            )
            if read_only:  # max_execution_time only limits SELECT
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                    for variable, value in (
                        ("max_execution_time", _STATEMENT_SECONDS * 1000),  # MySQL, ms
                        ("max_statement_time", _STATEMENT_SECONDS),  # MariaDB, seconds
                    ):
                        with contextlib.suppress(Exception):  # the other server's variable
                            cursor.execute(f"SET SESSION {variable} = {value}")
            return connection
        return driver.connect(
            server=parts.hostname or "localhost",
            port=str(parts.port or 1433),
            user=user,
            password=password,
            database=database,
            login_timeout=10,
            timeout=_STATEMENT_SECONDS,
            encryption=options.get("encrypt") or ("require" if tls else None),
        )

    return _redacting(connect, url, password)


def _sql_tools(ctx: ConnectorContext) -> list[BaseTool]:
    from awa.toolkits.sql import sql_toolkit

    dialect, url, path = database_url(ctx)
    operations = ctx.operations()
    names = {"schema": "sql_schema", "query": "sql_query", "execute": "sql_execute"}
    about = str(ctx.option("description") or "").strip()
    if (
        dialect == "mssql"
        and not ctx.option("accept_text_check")
        and any(op.name == "query" for op in operations)
    ):
        raise ConfigurationError(
            "SQL Server has no read-only session, so sql_query is only checked by its text; "
            "give the login read-only rights and set the option accept_text_check: true "
            "(or include only the execute operation)"
        )
    tools: list[BaseTool] = []
    for operation in operations:
        options: dict[str, Any] = {
            "include": [names[operation.name]],
            "requires_approval": False,
            "description": about or None,
        }
        if path is not None:
            [tool] = sql_toolkit(path, **options)
        else:
            reads = operation.name != "execute"
            connect = database_connector(dialect, url, read_only=reads)
            [tool] = sql_toolkit(connect=connect, dialect=dialect, **options)
        tools.append(adopt(ctx, tool, operation))
    return tools


def schema_tool(dialect: str, url: str, path: Path | None) -> BaseTool:
    """The unframed `sql_schema` tool of a database address, on a read-only connection."""

    from awa.toolkits.sql import sql_toolkit

    if path is not None:
        [tool] = sql_toolkit(path, include=["sql_schema"], frame_output=False)
    else:
        connect = database_connector(dialect, url, read_only=True)
        [tool] = sql_toolkit(
            connect=connect, dialect=dialect, include=["sql_schema"], frame_output=False
        )
    return tool


def _table_names(dialect: str, url: str, path: Path | None) -> str:
    """The tables and views for the health check message: names only, never data."""

    answer = schema_tool(dialect, url, path).invoke({})
    try:
        found = json.loads(answer)
    except ValueError:  # the tool reports a failure as text; the connection itself worked
        return f"; the tables could not be listed ({str(answer).removeprefix('Error:').strip()})"
    names = [t["name"] for t in found["tables"]] + found.get("more_tables", [])
    shown = ", ".join(names[:_CHECK_NAMES])
    more = f" (and {len(names) - _CHECK_NAMES} more)" if len(names) > _CHECK_NAMES else ""
    return f"; {len(names)} table(s) and view(s): {shown}{more}" if names else "; no tables yet"


def _sql_check(ctx: ConnectorContext) -> str:
    import sqlite3

    dialect, url, path = database_url(ctx)
    if path is not None:
        if not path.is_file():
            raise ToolException(f"the SQLite file {str(path)!r} does not exist")
        with contextlib.closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            connection.execute("SELECT 1").fetchone()
        return f"SQLite database {path.name} is readable{_table_names(dialect, url, path)}"
    connection = database_connector(dialect, url, read_only=True)()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
    finally:
        connection.close()
    return f"{_DRIVERS[dialect][2]} answered SELECT 1{_table_names(dialect, url, None)}"


SQL = ConnectorSpec(
    name="sql",
    title="SQL database",
    category="data",
    summary=(
        "Read-only SQL queries (and approved changes) on SQLite, PostgreSQL, MySQL/MariaDB or "
        "SQL Server."
    ),
    env=(
        EnvVar(
            "DATABASE_URL",
            "sqlite:///path/to/file.db, postgresql://, mysql:// or "
            "mssql://user:password@host:port/database.",
            example="postgresql://reporting@db.example.com/sales",
        ),
    ),
    operations=(
        Operation("schema", "Describe the database: tables, columns, keys, approximate sizes."),
        Operation("query", "Run a read-only SQL query."),
        Operation("execute", "Run one SQL statement that changes the database.", write=True),
    ),
    factory=_sql_tools,
    check=_sql_check,
    options=("description", "accept_text_check"),
    setup=(
        "Set DATABASE_URL; give the database user read-only rights unless changes are wanted "
        "(PostgreSQL needs the postgres extra, MySQL and SQL Server the sql extra). "
        "SQL Server queries need accept_text_check: true, as only the query text is checked."
    ),
)


_WEB_NAMES = {"fetch_url": "fetch_url", "search": "web_search"}


def _web_search_provider(ctx: ConnectorContext) -> SearchProvider:
    """The configured search endpoint (`search_url` or WEB_SEARCH_URL), else the keyless search."""

    template = ctx.option("search_url") or ctx.value("WEB_SEARCH_URL")
    timeout = float(ctx.option("timeout", 15.0))
    if not template:
        return DuckDuckGoSearchProvider(
            region=str(ctx.option("region", "wt-wt")),
            safe_search=ctx.option("safe_search", "moderate"),
            timeout=timeout,
            use_env_proxies=_flag(ctx, "use_env_proxies"),
        )
    key = ctx.value("WEB_SEARCH_API_KEY")
    return JSONSearchProvider(
        str(template),
        results_path=str(ctx.option("results_path", "results")),
        title_field=str(ctx.option("title_field", "title")),
        url_field=str(ctx.option("url_field", "url")),
        snippet_field=str(ctx.option("snippet_field", "snippet")),
        headers={str(ctx.option("key_header", "X-Api-Key")): key} if key else None,
        timeout=timeout,
    )


def _web_tools(ctx: ConnectorContext) -> list[BaseTool]:
    from awa.toolkits.web import web_toolkit

    operations = ctx.operations()
    hosts = ctx.option("allowed_hosts")
    tools = web_toolkit(
        allowed_hosts=[hosts] if isinstance(hosts, str) else hosts,
        search=_web_search_provider(ctx),
        include=[_WEB_NAMES[op.name] for op in operations],
        timeout=float(ctx.option("timeout", 20.0)),
        allow_private_networks=_flag(ctx, "allow_private_networks"),
        allow_http=_flag(ctx, "allow_http"),
        use_env_proxies=_flag(ctx, "use_env_proxies"),
    )
    by_name = {tool.name: tool for tool in tools}
    return [adopt(ctx, by_name[_WEB_NAMES[op.name]], op) for op in operations]


def _web_check(ctx: ConnectorContext) -> str:
    if not any(op.name == "search" for op in ctx.operations()):
        return "fetch_url needs no configuration"
    provider = _web_search_provider(ctx)
    hits = provider.search("weather", max_results=1)
    source = (
        "the configured endpoint"
        if isinstance(provider, JSONSearchProvider)
        else "the built-in keyless search (DuckDuckGo)"
    )
    return f"web search answered through {source} with {len(hits)} hit(s)"


WEB = ConnectorSpec(
    name="web",
    title="Web pages and search",
    category="web",
    summary=(
        "Read public web pages as text and search the web (built-in, no API key; or your "
        "own search endpoint)."
    ),
    env=(
        EnvVar(
            "WEB_SEARCH_URL",
            "Your own search endpoint answering JSON, with {query} in the URL (optional: "
            "without it, search uses the built-in keyless search).",
            secret=False,
            required=False,
            example="https://search.example.com/api?q={query}&count={max_results}",
        ),
        EnvVar("WEB_SEARCH_API_KEY", "API key of your search endpoint.", required=False),
    ),
    operations=(
        Operation("fetch_url", "Read a web page as text."),
        Operation("search", "Search the web."),
    ),
    factory=_web_tools,
    check=_web_check,
    options=(
        "region",
        "safe_search",
        "search_url",
        "results_path",
        "title_field",
        "url_field",
        "snippet_field",
        "key_header",
    ),
    setup=(
        "Nothing to set up: reading pages and searching work without a key (search uses "
        "DuckDuckGo; set region, e.g. de-de, for local results). Set WEB_SEARCH_URL (and "
        "WEB_SEARCH_API_KEY) to search through your own search API instead."
    ),
)


def _smtp_port(ctx: ConnectorContext) -> int | None:
    text = ctx.value("SMTP_PORT")
    if text is None:
        return None
    if not (text.isascii() and text.isdigit()):
        raise ConfigurationError("SMTP_PORT is not a port number")
    return int(text)


def _smtp_tools(ctx: ConnectorContext) -> list[BaseTool]:
    from awa.toolkits.communication import email_toolkit

    require_configured(ctx)
    password = ctx.value("SMTP_PASSWORD")
    allowed = ctx.option("allowed_recipients")
    [tool] = email_toolkit(
        sender=ctx.require("SMTP_SENDER"),
        host=ctx.require("SMTP_HOST"),
        port=_smtp_port(ctx),
        username=ctx.value("SMTP_USERNAME"),
        password=SecretStr(password) if password else None,
        security=ctx.option("security", "starttls"),
        allowed_recipients=[allowed] if isinstance(allowed, str) else allowed,
        timeout=float(ctx.option("timeout", 30.0)),
        requires_approval=False,
    )
    return [adopt(ctx, tool, op) for op in ctx.operations()]


def _smtp_check(ctx: ConnectorContext) -> str:
    from awa.toolkits.communication import check_smtp_security

    require_configured(ctx)
    security = str(ctx.option("security", "starttls"))
    host = ctx.require("SMTP_HOST")
    username, password = ctx.value("SMTP_USERNAME"), ctx.value("SMTP_PASSWORD")
    check_smtp_security(security, host, username)
    port = _smtp_port(ctx) or {"ssl": 465, "none": 25}.get(security, 587)
    context = ssl.create_default_context()
    try:
        server: smtplib.SMTP = (
            smtplib.SMTP_SSL(host, port, timeout=15, context=context)
            if security == "ssl"
            else smtplib.SMTP(host, port, timeout=15)
        )
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if username:
                server.login(username, password or "")
            server.noop()
    except smtplib.SMTPAuthenticationError:
        raise ToolException("the SMTP server rejected SMTP_USERNAME/SMTP_PASSWORD") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise ToolException(ctx.redact(f"SMTP check failed: {type(exc).__name__}: {exc}")) from None
    return f"SMTP server {host}:{port} accepted the connection" + (" and login" if username else "")


SMTP = ConnectorSpec(
    name="smtp",
    title="Email sending (SMTP)",
    category="communication",
    summary="Send emails over SMTP from a configured sender address.",
    env=(
        EnvVar("SMTP_HOST", "SMTP server name.", secret=False, example="smtp.example.com"),
        EnvVar(
            "SMTP_SENDER", "From address.", secret=False, example="Support <support@example.com>"
        ),
        EnvVar("SMTP_USERNAME", "Login name (no login when unset).", secret=False, required=False),
        EnvVar("SMTP_PASSWORD", "Login password.", required=False),
        EnvVar("SMTP_PORT", "Port (587 for STARTTLS by default).", secret=False, required=False),
    ),
    operations=(Operation("send_email", "Send an email.", write=True),),
    factory=_smtp_tools,
    check=_smtp_check,
    options=("security", "allowed_recipients"),
    setup="Use the SMTP relay of your mail provider; an app password is safer than your own.",
)


_IMAP_TEXT = re.compile(r'^[^"\\\r\n]*$')
_IMAP_CLASS: Callable[..., imaplib.IMAP4] = imaplib.IMAP4_SSL


class SearchMessagesArgs(ToolArgs):
    folder: str = Field(default="INBOX", description="Mailbox folder.")
    sender: str | None = Field(default=None, description="Only messages from this address or name.")
    subject: str | None = Field(
        default=None, description="Only messages whose subject contains this."
    )
    text: str | None = Field(default=None, description="Only messages containing this text.")
    since: str | None = Field(
        default=None, description="Only messages since this date (YYYY-MM-DD)."
    )
    unseen: bool = Field(default=False, description="Only unread messages.")
    limit: int = Field(
        default=20, ge=1, le=100, description="Most messages returned (newest first)."
    )


class ReadMessageArgs(ToolArgs):
    uid: str = Field(pattern=r"^\d+$", description="UID of the message (from search_messages).")
    folder: str = Field(default="INBOX", description="Mailbox folder.")


class _Imap:
    def __init__(self, ctx: ConnectorContext) -> None:
        self.ctx = ctx

    def _open(self, folder: str) -> imaplib.IMAP4:
        ctx = self.ctx
        if not _IMAP_TEXT.fullmatch(folder):
            raise ToolException("the folder name contains quotes or line breaks")
        port = ctx.value("IMAP_PORT") or "993"
        client: imaplib.IMAP4 | None = None
        try:
            client = _IMAP_CLASS(
                ctx.require("IMAP_HOST"),
                int(port),
                ssl_context=ssl.create_default_context(),
                timeout=float(ctx.option("timeout", 30.0)),
            )
            client.login(ctx.require("IMAP_USERNAME"), ctx.require("IMAP_PASSWORD"))
            if "UTF8=ACCEPT" in client.capabilities:
                client.enable("UTF8=ACCEPT")
            name = folder if client.utf8_enabled else _modified_utf7(folder)
            status, _ = client.select(f'"{name}"', readonly=True)
            if status != "OK":
                raise ToolException(f"cannot open the folder {folder!r}")
        except BaseException as exc:
            if client is not None:
                _logout(client)  # no connection stays open after a failure
            if isinstance(exc, imaplib.IMAP4.error):
                raise ToolException(ctx.redact(f"the IMAP server refused: {exc}")) from None
            if isinstance(exc, OSError | ValueError):
                raise ToolException(ctx.redact(f"IMAP connection failed: {exc}")) from None
            raise
        return client

    def search(self, args: SearchMessagesArgs) -> str:
        criteria: list[str] = []
        for word, value in (("FROM", args.sender), ("SUBJECT", args.subject), ("TEXT", args.text)):
            if value:
                if not _IMAP_TEXT.fullmatch(value):
                    raise ToolException(f"{word.lower()} contains quotes or line breaks")
                criteria.append(f'{word} "{value}"')
        if args.since:
            try:
                day = datetime.strptime(args.since, "%Y-%m-%d")
            except ValueError:
                raise ToolException("since must be a date like 2026-09-30") from None
            criteria.append("SINCE " + day.strftime("%d-%b-%Y"))
        if args.unseen:
            criteria.append("UNSEEN")
        client = self._open(args.folder)
        try:
            if not client.utf8_enabled and not all(c.isascii() for c in criteria):
                raise ToolException(
                    "this IMAP server takes only ASCII search text; search without umlauts or "
                    "other non-ASCII letters"
                )
            _, found = client.uid("SEARCH", *(criteria or ["ALL"]))
            uids = (found[0] or b"").split()[-args.limit :][::-1] if found else []
            messages = []
            for uid in uids:
                _, data = client.uid(
                    "FETCH", uid.decode(), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                )
                header = next((part[1] for part in data if isinstance(part, tuple)), b"")
                parsed = email.message_from_bytes(header, policy=email.policy.default)
                messages.append(
                    {
                        "uid": uid.decode(),
                        "from": str(parsed.get("From", "")),
                        "subject": str(parsed.get("Subject", "")),
                        "date": str(parsed.get("Date", "")),
                    }
                )
        finally:
            _logout(client)
        return tool_json({"messages": messages, "count": len(messages)})

    def read(self, args: ReadMessageArgs) -> str:
        from awa.toolkits.web import html_to_text

        client = self._open(args.folder)
        try:
            _, data = client.uid("FETCH", args.uid, "(BODY.PEEK[])")
        finally:
            _logout(client)
        raw = next((part[1] for part in data if isinstance(part, tuple)), None)
        if raw is None:
            raise ToolException(f"no message with UID {args.uid} in {args.folder!r}")
        message = email.message_from_bytes(raw, policy=email.policy.default)
        assert isinstance(message, EmailMessage)
        body = message.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body is not None else ""
        if body is not None and body.get_content_subtype() == "html":
            text = html_to_text(text)[1]
        attachments = [part.get_filename() for part in message.iter_attachments()]
        return tool_json(
            {
                "from": str(message.get("From", "")),
                "to": str(message.get("To", "")),
                "cc": str(message.get("Cc", "")),
                "subject": str(message.get("Subject", "")),
                "date": str(message.get("Date", "")),
                "text": text,
                "attachments": [name for name in attachments if name],
            }
        )


def _modified_utf7(name: str) -> str:
    """A folder name in the modified UTF-7 of IMAP (RFC 3501), for servers without UTF-8."""

    parts: list[str] = []
    wide: list[str] = []

    def close() -> None:
        if wide:
            encoded = base64.b64encode("".join(wide).encode("utf-16-be")).rstrip(b"=")
            parts.append("&" + encoded.replace(b"/", b",").decode() + "-")
            wide.clear()

    for char in name:
        if " " <= char <= "~":
            close()
            parts.append("&-" if char == "&" else char)
        else:
            wide.append(char)
    close()
    return "".join(parts)


def _logout(client: imaplib.IMAP4) -> None:
    with contextlib.suppress(imaplib.IMAP4.error, OSError):
        client.logout()


def _imap_tools(ctx: ConnectorContext) -> list[BaseTool]:
    require_configured(ctx)
    impl = _Imap(ctx)
    tools: dict[str, tuple[Callable[..., str], str, type[ToolArgs]]] = {
        "search_messages": (
            impl.search,
            "Find emails in a mailbox folder (sender, subject, text, date, unread); "
            "returns UID, sender, subject and date, newest first.",
            SearchMessagesArgs,
        ),
        "read_message": (
            impl.read,
            "Read one email (headers, plain text, attachment names) without marking it as read.",
            ReadMessageArgs,
        ),
    }
    built: list[BaseTool] = []
    for op in ctx.operations():
        handler, description, schema = tools[op.name]
        built.append(
            build_tool(
                handler,
                name=tool_name(ctx.name, op.name),
                description=description,
                args_schema=schema,
                toolkit=f"connector:{ctx.name}",
                risk=READ_RISK,
                frame=True,
                source=f"connector:{ctx.name}",
                metadata={CONNECTOR_METADATA_KEY: f"{ctx.name}.{op.name}"},
            )
        )
    return built


def _imap_check(ctx: ConnectorContext) -> str:
    require_configured(ctx)
    client = _Imap(ctx)._open("INBOX")
    _logout(client)
    return "the IMAP server accepted the login and opened INBOX"


IMAP = ConnectorSpec(
    name="imap",
    title="Email reading (IMAP)",
    category="communication",
    summary="Search and read emails of a mailbox over IMAP (read-only, nothing marked read).",
    env=(
        EnvVar("IMAP_HOST", "IMAP server name.", secret=False, example="imap.example.com"),
        EnvVar("IMAP_USERNAME", "Login name of the mailbox.", secret=False),
        EnvVar("IMAP_PASSWORD", "Password or app password."),
        EnvVar("IMAP_PORT", "Port (993 by default).", secret=False, required=False),
    ),
    operations=(
        Operation("search_messages", "Find emails."),
        Operation("read_message", "Read one email."),
    ),
    factory=_imap_tools,
    check=_imap_check,
    setup="Enable IMAP for the mailbox and create an app password if the provider offers one.",
)


def _webhook_tools(ctx: ConnectorContext) -> list[BaseTool]:
    from awa.toolkits.communication import webhook_toolkit

    require_configured(ctx)
    [tool] = webhook_toolkit(
        url=ctx.require("WEBHOOK_URL"),
        description=ctx.option("description"),
        timeout=float(ctx.option("timeout", 15.0)),
        requires_approval=False,
        allow_private_networks=_flag(ctx, "allow_private_networks"),
        allow_http=_flag(ctx, "allow_http"),
    )
    return [adopt(ctx, tool, op) for op in ctx.operations()]


def _webhook_check(ctx: ConnectorContext) -> str:
    require_configured(ctx)
    return "the webhook URL is set; posting is not tried because it would send a message"


WEBHOOK = ConnectorSpec(
    name="webhook",
    title="Webhook",
    category="communication",
    summary="Post a message or JSON payload to one webhook (chat channel, automation).",
    env=(EnvVar("WEBHOOK_URL", "The webhook URL (it usually contains a token)."),),
    operations=(Operation("post", "Post a message or JSON payload.", write=True),),
    factory=_webhook_tools,
    check=_webhook_check,
    options=("description",),
    setup="Create an incoming webhook in the chat or automation tool and copy its URL.",
)


LOCAL = (WORKSPACE, SQL, WEB, SMTP, IMAP, WEBHOOK)
"""The wrapper connectors, in catalog order."""
