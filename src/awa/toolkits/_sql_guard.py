"""The text check of `sql_query` for databases without an engine-enforced read-only mode.

The statement is lexed under every plausible reading of its database (string and comment rules,
`NO_BACKSLASH_ESCAPES`, `standard_conforming_strings`) and refused unless clean under all of
them. A safety net only; it never replaces a read-only role or session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from awa._internal.upstream.tools import ToolException

DIALECTS = ("sqlite", "postgresql", "mysql", "mssql")
"""The dialects the check knows; `None` means every one of them."""

_NAME_CHARS = re.compile(r"[A-Za-z0-9_$\u0080-\U0010ffff]")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")
_STARTS = re.compile(r"[(\s]*(?:select|with)\b")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUOTED = "\x01"  # marks a word that came from a quoted name; refused in the input itself


@dataclass(frozen=True)
class _Lexing:
    """How one database reading ends strings, names and comments."""

    backslash_in_strings: bool = False  # `'a\'b'` is one string (MySQL default)
    e_strings: bool = False  # `E'..'` strings use backslash escapes (PostgreSQL)
    dollar_quotes: bool = False  # `$tag$ .. $tag$` (PostgreSQL)
    hash_comments: bool = False  # `# ..` to the end of the line (MySQL)
    dash_needs_space: bool = False  # `--` starts a comment only before a space (MySQL)
    nested_comments: bool = False  # `/* /* */ */` (PostgreSQL, SQL Server)
    brackets: bool = False  # `[name]` quotes a name (SQL Server, SQLite)
    backticks: bool = False  # `` `name` `` quotes a name (MySQL, SQLite)


_READINGS: dict[str, tuple[_Lexing, ...]] = {
    "sqlite": (_Lexing(brackets=True, backticks=True),),
    "postgresql": (
        _Lexing(e_strings=True, dollar_quotes=True, nested_comments=True),
        _Lexing(
            backslash_in_strings=True, e_strings=True, dollar_quotes=True, nested_comments=True
        ),
    ),
    "mysql": (
        _Lexing(
            backslash_in_strings=True, hash_comments=True, dash_needs_space=True, backticks=True
        ),
        _Lexing(hash_comments=True, dash_needs_space=True, backticks=True),
    ),
    "mssql": (_Lexing(brackets=True, nested_comments=True),),
}

_WRITE_WORDS = frozenset(  # inside one SELECT/WITH these can only mean a change, a lock or a script
    """
    insert update delete merge upsert drop alter create truncate grant revoke call exec execute
    copy into lock vacuum attach detach pragma load_file prepare use declare dbcc backup restore
    kill shutdown reconfigure deny revert setuser bulk writetext updatetext checkpoint
    openquery openrowset opendatasource waitfor updlock xlock tablockx holdlock readpast
    """.split()  # noqa: SIM905 - a word list is easier to read and extend
)
# Words such as replace, do, comment or analyze stay allowed (set too, outside SQL Server): a
# statement must start with SELECT/WITH and be single, where they can only be function names or
# identifiers.
_BLOCKED_PREFIXES = (  # server-side file, network and procedure access, wherever they appear
    "xp_", "sp_executesql", "sp_configure", "sp_oa", "dblink", "lo_", "pg_read_", "pg_ls_",
)  # fmt: skip
_BLOCKED_CALLS = (
    frozenset(  # functions that change state, lock, wait or reach outside; blocked when called
        """
    nextval setval set_config pg_notify pg_reload_conf pg_rotate_logfile pg_promote pg_stat_file
    pg_create_restore_point pg_import_system_collations pg_extension_config_dump
    pg_stat_statements_reset pg_prewarm lowrite loread pg_export_snapshot txid_current
    txid_current_if_assigned pg_current_xact_id pg_current_xact_id_if_assigned
    sleep benchmark get_lock release_lock release_all_locks master_pos_wait source_pos_wait
    wait_for_executed_gtid_set wait_until_sql_thread_after_gtids sys_exec sys_eval sys_get sys_set
    sys_bineval
    load_extension readfile writefile edit fts3_tokenizer
    fn_get_audit_file fn_dump_dblog fn_trace_gettable
    """.split()  # noqa: SIM905
    )
)
_BLOCKED_CALL_PREFIXES = (  # the same, as families of functions
    "pg_advisory", "pg_try_advisory", "pg_sleep", "pg_terminate", "pg_cancel", "pg_switch_",
    "pg_create_", "pg_drop_", "pg_replication_", "pg_logical_", "pg_file_", "pg_backup_",
    "pg_start_backup", "pg_stop_backup", "pg_wal_replay_", "pg_stat_reset", "pg_log_backend_",
    "query_to_xml", "cursor_to_xml", "binary_upgrade_", "fn_xe_file_",
    "pg_copy_", "brin_summarize", "brin_desummarize", "gin_clean_pending",
)  # fmt: skip
_ROW_LOCK = re.compile(r"\bfor\s+(?:no\s+key\s+)?(?:key\s+)?share\b")
_NEXT_VALUE = re.compile(r"\bnext\s+value\s+for\b")
# SQL Server needs no `;` between statements: `SELECT 1 COMMIT` is two of them. These words begin
# a statement there and are refused outside parentheses; `end` only when no CASE is open.
_MSSQL_STARTS = frozenset(
    "commit rollback begin save disable enable send receive end move open close set".split()  # noqa: SIM905
)
_SET_OPERATORS = frozenset(("union", "intersect", "except", "all", "distinct"))
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\S")
_UNICODE_NAME = re.compile(r"\bu\s*&\s*$", re.IGNORECASE)


def _skip_string(text: str, start: int, *, backslash: bool) -> int:
    """Index after the string whose opening quote is at `start`; raise when it never ends."""

    quote = text[start]
    i = start + 1
    n = len(text)
    while i < n:
        char = text[i]
        if backslash and char == "\\":
            i += 2
        elif char == quote:
            if text[i + 1 : i + 2] == quote:
                i += 2
            else:
                return i + 1
        else:
            i += 1
    raise ToolException(
        "a quoted string is not closed (or ends differently in another SQL dialect); "
        "write a quote inside text as two quotes ('') and avoid backslashes"
    )


def _skip_block_comment(text: str, start: int, *, nested: bool) -> int:
    depth = 1
    i = start + 2
    n = len(text)
    while i < n:
        if nested and text.startswith("/*", i):
            depth += 1
            i += 2
        elif text.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise ToolException("a comment is not closed; close every /* with */")


def _quoted_name(text: str, start: int, close: str) -> tuple[str, int]:
    """Content and end index of a quoted name that doubles `close` to write it literally."""

    i = start + 1
    n = len(text)
    while i < n:
        if text[i] == close:
            if text[i + 1 : i + 2] == close:
                i += 2
                continue
            return text[start + 1 : i], i + 1
        i += 1
    raise ToolException("a quoted name is not closed (a missing quote, bracket or backtick)")


def _blank(statement: str, lexing: _Lexing) -> str:
    """The statement with literals and comments replaced by spaces, one reading of it."""

    out: list[str] = []
    i = 0
    n = len(statement)
    while i < n:
        char = statement[i]
        previous = statement[i - 1] if i else " "
        after_name = bool(_NAME_CHARS.match(previous))
        if char == "'":
            e_string = (
                lexing.e_strings
                and previous in "eE"
                and not (i >= 2 and _NAME_CHARS.match(statement[i - 2]))
            )
            i = _skip_string(statement, i, backslash=lexing.backslash_in_strings or e_string)
            out.append(" ")
        elif char == '"':
            if _UNICODE_NAME.search(statement[:i]):
                raise ToolException('quoted names with unicode escapes (U&"..") are not allowed')
            if lexing.backslash_in_strings:  # MySQL: a double-quoted text, or a name
                end = _skip_string(statement, i, backslash=True)
                out.append(_exposed(statement[i + 1 : end - 1]))
            else:
                content, end = _quoted_name(statement, i, '"')
                out.append(_exposed(content))
            i = end
        elif char == "`" and lexing.backticks:
            content, end = _quoted_name(statement, i, "`")
            out.append(_exposed(content))
            i = end
        elif char == "[" and lexing.brackets:
            content, end = _quoted_name(statement, i, "]")
            out.append(_exposed(content))
            i = end
        elif char == "$" and lexing.dollar_quotes and not after_name:
            tag = _DOLLAR_TAG.match(statement, i)
            if tag is None:
                out.append(char)
                i += 1
                continue
            close = statement.find(tag.group(0), tag.end())
            if close < 0:
                raise ToolException("a $$ quoted string is not closed")
            i = close + len(tag.group(0))
            out.append(" ")
        elif char == "-" and statement.startswith("--", i):
            after = statement[i + 2 : i + 3]
            if lexing.dash_needs_space and after and not (after.isspace() or ord(after) < 32):
                out.append("--")  # two minus signs: `1--1` is arithmetic in MySQL
                i += 2
                continue
            end = statement.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif char == "#" and lexing.hash_comments:
            end = statement.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif char == "/" and statement.startswith("/*", i):
            if statement.startswith(("/*!", "/*M!"), i):
                raise ToolException("executable comments (/*! ... */) are not allowed")
            i = _skip_block_comment(statement, i, nested=lexing.nested_comments)
            out.append(" ")
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _exposed(content: str) -> str:
    """A plain quoted name stays visible, marked, so a call such as `"pg_sleep"(1)` is found."""

    return f" {_QUOTED}{content} " if _PLAIN_NAME.fullmatch(content) else " "


def _is_quoted(lowered: str, match: re.Match[str]) -> bool:
    return match.start() > 0 and lowered[match.start() - 1] == _QUOTED


def _is_blocked(word: str, lowered: str, match: re.Match[str], *, quoted: bool) -> bool:
    """Whether the word is a write word, a blocked name (quoted or not), or a blocked call."""

    if word.startswith(_BLOCKED_PREFIXES) or (not quoted and word in _WRITE_WORDS):
        return True
    called = lowered[match.end() :].lstrip().startswith("(")
    return called and (word in _BLOCKED_CALLS or word.startswith(_BLOCKED_CALL_PREFIXES))


def _mssql_blocked(lowered: str) -> set[str]:
    """Words that start a second SQL Server statement: one top-level SELECT/WITH is allowed."""

    blocked: set[str] = set()
    depth = cases = selects = 0
    with_query = lowered.lstrip("( \t\r\n").startswith("with")
    previous = ""
    for token in _TOKEN.finditer(lowered):
        text = token.group(0)
        quoted = token.start() > 0 and lowered[token.start() - 1] == _QUOTED
        if text == "(":
            depth += 1
        elif text == ")":
            depth = max(depth - 1, 0)
        elif quoted or previous == ".":
            pass
        elif text == "case":
            cases += 1
        elif text == "end" and cases:
            cases -= 1
        elif depth == 0 and text in _MSSQL_STARTS:
            blocked.add(text)
        elif depth == 0 and text == "select":
            selects += 1
            follows_cte = with_query and selects == 1 and previous == ")"
            if selects > 1 and previous not in _SET_OPERATORS and not follows_cte:
                blocked.add("a second select")
        previous = text
    return blocked


def _check_reading(statement: str, lexing: _Lexing, *, mssql: bool = False) -> None:
    stripped = _blank(statement, lexing).strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if ";" in stripped:
        raise ToolException("only a single statement is allowed")
    lowered = stripped.lower()
    matches = list(_WORD.finditer(lowered))
    if not _STARTS.match(lowered):
        raise ToolException(
            "only SELECT or WITH queries are allowed; use sql_execute to change data"
        )
    blocked: set[str] = set()
    for match in matches:
        word = match.group(0)
        if _is_blocked(word, lowered, match, quoted=_is_quoted(lowered, match)):
            blocked.add(word)
    if _ROW_LOCK.search(lowered):
        blocked.add("for share")
    if _NEXT_VALUE.search(lowered):
        blocked.add("next value for")
    if mssql:
        blocked |= _mssql_blocked(lowered)
    if blocked:
        raise ToolException(
            f"the query contains {', '.join(w.upper() for w in sorted(blocked))}, "
            "which may change data"
        )


def clean_dialects(statement: str) -> tuple[str, ...]:
    """The dialects under whose readings `statement` is a clean read-only query.

    Raises:
        ToolException: If no dialect accepts it (the first reason is given).
    """

    clean: list[str] = []
    first: ToolException | None = None
    for name in DIALECTS:
        try:
            check_read_only_sql(statement, name)
        except ToolException as exc:
            first = first or exc
        else:
            clean.append(name)
    if not clean:
        assert first is not None
        raise first
    return tuple(clean)


def check_read_only_sql(statement: str, dialect: str | None = None) -> None:
    """The SQL check of `sql_query`, as the project's component switch `guard.sql_guard` has it.

    On (the default) it is `builtin_read_only_check`; replaced (`register_check`) it is that
    function, and any exception it raises refuses the statement; switched off, only the database's
    own rights and SQLite's read-only mode apply.
    """

    from awa.plugins.switches import choice, replacement

    picked = choice("guard.sql_guard")
    if picked.is_off:
        return
    own = replacement(picked)
    if own is None:
        builtin_read_only_check(statement, dialect)
        return
    try:
        own(statement, dialect)
    except ToolException:
        raise
    except Exception as exc:  # noqa: BLE001 - every refusal of the own check is a tool error
        raise ToolException(f"the SQL check refused the statement: {exc}") from None


def builtin_read_only_check(statement: str, dialect: str | None = None) -> None:
    """Refuse `statement` unless it is a single `SELECT`/`WITH` that cannot change state.

    The statement must be clean under every reading `dialect` (`"sqlite"`, `"postgresql"`,
    `"mysql"`, `"mssql"`) can be switched into; `None` checks all four. A safety net; it never
    replaces a read-only database role or session.
    """

    if dialect is not None and dialect not in _READINGS:
        raise ValueError(f"dialect must be one of {', '.join(DIALECTS)} or None, not {dialect!r}")
    if chr(0) in statement or _QUOTED in statement:
        raise ToolException("control characters are not allowed in SQL")
    for name in DIALECTS if dialect is None else (dialect,):
        for lexing in _READINGS[name]:
            _check_reading(statement, lexing, mssql=name == "mssql")
