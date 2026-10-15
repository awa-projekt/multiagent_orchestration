"""Bypass corpus for the SQL read-only text check.

The cases come from the read-only bypass classes published for SQL tool servers and text-to-SQL
agents (2025 and 2026): the string-ending disagreement between a scanner and MySQL or PostgreSQL
(backslash escapes, `E'..'`), MySQL's `--` rule (`SELECT 1--1;`), writable CTEs, state-changing
functions reachable through SELECT, file and command access, locks, stacked statements without a
semicolon (SQL Server), executable comments, quoted function names and unicode-escaped names.
Each refused case must be refused whatever the dialect; the text check is a safety net and never
replaces database rights, so a case here is not a promise that nothing else can happen.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from awa._internal.upstream.tools import ToolException
from awa.toolkits import check_read_only_sql, sql_toolkit

_REFUSED: list[tuple[str, str | None]] = [
    # SQL Server runs several statements without a semicolon
    ("SELECT 1 DISABLE TRIGGER audit ON orders COMMIT", None),
    ("SELECT 1 DISABLE TRIGGER audit ON orders COMMIT", "mssql"),
    ("SELECT 1 SELECT 2", "mssql"),
    ("SELECT 1 BEGIN TRAN SAVE TRANSACTION a", "mssql"),
    ("SELECT 1 SET IDENTITY_INSERT t ON", "mssql"),
    ("SELECT 1 OPEN c", "mssql"),
    ("SELECT CASE WHEN 1 = 1 THEN 1 END END", "mssql"),
    ("SELECT NEXT VALUE FOR seq", "mssql"),
    ("SELECT lo_from_bytea(0, 'x')", "postgresql"),
    ("SELECT pg_export_snapshot()", "postgresql"),
    ("SELECT txid_current()", "postgresql"),
    # string-ending disagreement
    (
        r"WITH d AS (SELECT E'a\'' AS a), e AS (DELETE FROM t RETURNING 1) SELECT 1 WHERE '' = ''",
        None,
    ),
    (r"SELECT 'a\'', 1 INTO OUTFILE '/tmp/x' , 'b'", None),
    (r"SELECT 'a\''; DROP TABLE t; --'", None),
    (r"SELECT 'a\''; DROP TABLE t; --'", "postgresql"),
    (r"SELECT E'\'' ; DELETE FROM t; SELECT E'", "postgresql"),
    (r"SELECT 'x\' ; DROP TABLE t; -- '", "mysql"),
    ('SELECT "a\\" ; DROP TABLE t; -- "', "mysql"),
    (r"SELECT e'\'' || (SELECT 1); DROP TABLE t; SELECT '", None),
    ("SELECT 'unclosed", None),
    ('SELECT "unclosed', None),
    ("SELECT `unclosed", "mysql"),
    ("SELECT [unclosed", "mssql"),
    ("SELECT $$ unclosed", "postgresql"),
    ("SELECT 1 /* unclosed ; DROP TABLE t", None),
    # comment disagreement
    ("SELECT 1--1;INSERT INTO users VALUES ('x','y');", "mysql"),
    ("SELECT 1--1;INSERT INTO users VALUES ('x','y');", None),
    ("SELECT 1--1; DROP TABLE users", None),
    ("SELECT 1 # '\n; DROP TABLE t; SELECT '", None),
    ("SELECT 1 /* a /* b */ ; DROP TABLE t; /* c */ */", None),
    ("SELECT 1 /* a /* b */ ; DROP TABLE t; SELECT 1 */", "mysql"),
    ("SELECT 1 /*! ; DROP TABLE t */", None),
    ("SELECT 1 /*M!100000 ; DROP TABLE t */", None),
    ("SELECT 1 /*!50000 INTO OUTFILE '/tmp/x' */", None),
    # stacked and unseparated statements
    ("SELECT 1; DROP TABLE t", None),
    ("SELECT 1;\nDELETE FROM t", None),
    ("SELECT 1 DROP TABLE t", "mssql"),
    ("SELECT 1 EXEC xp_cmdshell 'dir'", "mssql"),
    ("SELECT 1 INSERT INTO t VALUES (1)", "mssql"),
    ("SELECT 1 USE master", "mssql"),
    ("SELECT 1 DECLARE @a INT", "mssql"),
    ("SELECT 1 SHUTDOWN", "mssql"),
    ("SELECT 1 BACKUP DATABASE x TO DISK = 'y'", "mssql"),
    ("SELECT 1 DBCC FREEPROCCACHE", "mssql"),
    ("SELECT 1 KILL 55", "mssql"),
    ("SELECT 1 RECONFIGURE", "mssql"),
    ("SELECT 1 WAITFOR DELAY '00:10'", "mssql"),
    ("SELECT 1 UNION SELECT 2 UPDATE t SET a = 1", None),
    ("SELECT 1;;DROP TABLE t", None),
    ("SELECT 1; SELECT 2", None),
    # writable CTEs and statements that start with a CTE or a bracket
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "postgresql"),
    ("WITH i AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM i", None),
    ("WITH u AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM u", None),
    ("WITH c AS (SELECT 1) UPDATE users SET role = 'admin'", None),
    ("WITH c AS (SELECT 1) DELETE FROM users", "sqlite"),
    ("WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c", "mysql"),
    ("WITH m AS (SELECT 1) MERGE INTO t USING m ON 1=1 WHEN MATCHED THEN DELETE", None),
    ("(DELETE FROM t)", None),
    ("TABLE t", None),
    ("VALUES (1)", None),
    ("EXPLAIN ANALYZE DELETE FROM t", None),
    ("SELECT * INTO backup FROM t", "mssql"),
    ("SELECT * INTO TEMP backup FROM t", "postgresql"),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", "mysql"),
    ("SELECT '<?php ?>' INTO DUMPFILE '/var/www/x.php'", "mysql"),
    ("SELECT 1 INTO @a", "mysql"),
    # state-changing and waiting functions
    ("SELECT setval('s', 1)", "postgresql"),
    ("SELECT nextval('s')", "postgresql"),
    ("SELECT pg_catalog.nextval('s')", "postgresql"),
    ("SELECT NEXTVAL  ('s')", "postgresql"),
    ("SELECT nextval/**/('s')", "postgresql"),
    ("SELECT pg_advisory_lock(1)", "postgresql"),
    ("SELECT pg_try_advisory_xact_lock(1)", "postgresql"),
    ("SELECT set_config('role', 'postgres', false)", "postgresql"),
    ("SELECT pg_notify('c', 'x')", "postgresql"),
    ("SELECT pg_sleep(100)", "postgresql"),
    ("SELECT pg_sleep_for('1 hour')", "postgresql"),
    ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity", "postgresql"),
    ("SELECT pg_cancel_backend(1)", "postgresql"),
    ("SELECT pg_reload_conf()", "postgresql"),
    ("SELECT pg_copy_logical_replication_slot('a', 'b')", "postgresql"),
    ("SELECT brin_summarize_new_values('idx')", "postgresql"),
    ("SELECT brin_desummarize_range('idx', 0)", "postgresql"),
    ("SELECT gin_clean_pending_list('idx')", "postgresql"),
    ("SELECT pg_rotate_logfile()", "postgresql"),
    ("SELECT pg_switch_wal()", "postgresql"),
    ("SELECT pg_create_logical_replication_slot('a', 'b')", "postgresql"),
    ("SELECT pg_drop_replication_slot('a')", "postgresql"),
    ("SELECT pg_stat_reset()", "postgresql"),
    ("SELECT pg_promote()", "postgresql"),
    ("SELECT pg_file_write('/tmp/x', 'y', false)", "postgresql"),
    ("SELECT query_to_xml('delete from t returning *', false, false, '')", "postgresql"),
    ("SELECT cursor_to_xml('c', 1, false, false, '')", "postgresql"),
    ("SELECT lo_export(1234, '/tmp/file')", "postgresql"),
    ("SELECT lo_import('/etc/passwd')", "postgresql"),
    ("SELECT lowrite(0, 'x')", "postgresql"),
    ("SELECT pg_read_file('/etc/passwd')", "postgresql"),
    ("SELECT pg_read_binary_file('/etc/passwd')", "postgresql"),
    ("SELECT pg_ls_dir('.')", "postgresql"),
    ("SELECT * FROM pg_ls_waldir()", "postgresql"),
    ("SELECT dblink_exec('host=x', 'DROP TABLE t')", "postgresql"),
    ("SELECT * FROM dblink('host=x', 'select 1') AS t(a int)", "postgresql"),
    ('SELECT "pg_sleep"(100)', "postgresql"),
    ('SELECT pg_catalog."pg_sleep"(100)', "postgresql"),
    ("SELECT \"nextval\"('s')", "postgresql"),
    ('SELECT U&"pg_sl\\0065ep"(100)', "postgresql"),
    ('SELECT U&"pg_sl\\0065ep"(100)', None),
    ("SELECT sleep(100)", "mysql"),
    ("SELECT `sleep`(100)", "mysql"),
    ("SELECT benchmark(100000000, md5('a'))", "mysql"),
    ("SELECT get_lock('a', 100)", "mysql"),
    ("SELECT release_all_locks()", "mysql"),
    ("SELECT load_file('/etc/passwd')", "mysql"),
    ("SELECT sys_exec('id')", "mysql"),
    ("SELECT sys_eval('id')", "mysql"),
    ("SELECT load_extension('/tmp/x.so')", "sqlite"),
    ("SELECT writefile('/tmp/x', 'y')", "sqlite"),
    ("SELECT readfile('/etc/passwd')", "sqlite"),
    ("SELECT edit('x')", "sqlite"),
    ("SELECT fts3_tokenizer('x', x'00')", "sqlite"),
    ("ATTACH DATABASE '/tmp/x.db' AS x", "sqlite"),
    ("WITH a AS (SELECT 1) SELECT 1 FROM a, (ATTACH DATABASE 'x' AS y)", "sqlite"),
    ("PRAGMA user_version(1337)", "sqlite"),
    ("PRAGMA user_version = 1337", "sqlite"),
    ("PRAGMA writable_schema = ON", "sqlite"),
    ("SELECT 1 PRAGMA user_version(5)", "sqlite"),
    # file, network and command access in SQL Server
    ("EXEC xp_cmdshell 'dir'", "mssql"),
    ("SELECT * FROM OPENROWSET(BULK 'C:\\x.txt', SINGLE_CLOB) AS a", "mssql"),
    ("SELECT * FROM OPENQUERY(srv, 'DELETE FROM t')", "mssql"),
    ("SELECT * FROM OPENDATASOURCE('a', 'b').db.dbo.t", "mssql"),
    ("SELECT * FROM master..xp_dirtree", "mssql"),
    ("SELECT * FROM sys.fn_get_audit_file('C:\\*', NULL, NULL)", "mssql"),
    ("SELECT * FROM fn_xe_file_target_read_file('a', NULL, NULL, NULL)", "mssql"),
    ("SELECT * FROM t WITH (UPDLOCK)", "mssql"),
    ("SELECT * FROM t WITH (XLOCK, HOLDLOCK)", "mssql"),
    ("EXECUTE sp_executesql N'DELETE FROM t'", "mssql"),
    # locks
    ("SELECT * FROM t FOR UPDATE", None),
    ("SELECT * FROM t FOR NO KEY UPDATE", "postgresql"),
    ("SELECT * FROM t FOR SHARE", "postgresql"),
    ("SELECT * FROM t FOR KEY SHARE", "postgresql"),
    ("SELECT * FROM t LOCK IN SHARE MODE", "mysql"),
    ("LOCK TABLE t IN ACCESS EXCLUSIVE MODE", None),
    # statements that are not queries
    ("DELETE FROM t", None),
    ("CALL do_things()", None),
    ("SET ROLE admin", None),
    ("COPY t TO '/tmp/x'", "postgresql"),
    ("COPY (SELECT 1) TO PROGRAM 'id'", "postgresql"),
    ("REPLACE INTO t VALUES (1)", "mysql"),
    ("DO $$ BEGIN DELETE FROM t; END $$", "postgresql"),
    ("PREPARE s AS DELETE FROM t", "postgresql"),
    ("SELECT 1 \x00; DROP TABLE t", None),
    ("SELECT 1 \x01select_pg_sleep(1)", None),
    ("", None),
    ("   ;  ", None),
    ("SELECT \"pg_read_file\"('/etc/passwd')", "postgresql"),
    ("SELECT pg_catalog.\"pg_ls_dir\"('.')", "postgresql"),
    ("SELECT \"lo_export\"(1, '/tmp/x')", "postgresql"),
    ("SELECT \"dblink_exec\"('h', 'DROP TABLE t')", "postgresql"),
    ("SELECT `xp_cmdshell`('dir')", "mysql"),
    ("SELECT [sp_executesql]", "mssql"),
]

_ACCEPTED: list[tuple[str, str | None]] = [
    ("SELECT * FROM t", None),
    ("  select a, 'DELETE' as label from t -- DROP TABLE t", None),
    ("WITH x AS (SELECT 1) SELECT * FROM x;", None),
    ('SELECT "update" FROM t', None),
    ('SELECT "from", "select" FROM t', "postgresql"),
    ("SELECT [update], [into] FROM t", "mssql"),
    ("SELECT `update` FROM t", "mysql"),
    ("SELECT $$ DELETE $$ AS text", "postgresql"),
    ("SELECT $tag$ it's ; DROP $tag$ AS text", "postgresql"),
    ("SELECT replace(name, 'a', 'b') AS name FROM t", None),
    ("SELECT comment, set, do FROM notes", "postgresql"),
    ("SELECT CASE WHEN a = 1 THEN 'x' ELSE 'y' END AS c FROM t", None),
    ("WITH x AS (SELECT 1 AS a) SELECT a FROM x UNION ALL SELECT 2", "mssql"),
    ("SELECT t.[close], t.[open] FROM prices t", "mssql"),
    ("SELECT t.comment FROM notes t WHERE t.do = 1 ORDER BY t.set", None),
    ("SELECT analyze, load, import FROM t", None),
    ("SELECT 'it''s fine' FROM t", None),
    ("SELECT 'a;b' FROM t", None),
    ("SELECT 'a -- b' AS c, 1 FROM t", None),
    ("SELECT 1 -- trailing comment\n", None),
    ("SELECT 1 /* a */ + /* b */ 2", None),
    ("SELECT /*+ INDEX(t i) */ * FROM t", None),
    ("SELECT 1 - -1", None),
    ("SELECT 1 -- 1; fine in a comment", "mysql"),
    ("SELECT x # a mysql comment\n FROM t", "mysql"),
    (r"SELECT 'a\\' FROM t", None),
    (r"SELECT 'C:\temp' FROM t", "sqlite"),
    (r"SELECT E'a\nb'", "postgresql"),
    ("SELECT a FROM t WHERE b = $1 AND c = %s AND d = ?", "postgresql"),
    ("SELECT sleep_hours, benchmark_score, edit_count, nextval_x FROM t", None),
    ("SELECT upsert_count, copyright, updated_at, created_at FROM t", None),
    ("SELECT updatexml_count FROM t", None),
    ("SELECT * FROM t ORDER BY id OFFSET 5 ROWS FETCH NEXT 5 ROWS ONLY", "mssql"),
    ("SELECT (SELECT max(id) FROM t) AS m", None),
    ("(SELECT 1) UNION (SELECT 2)", None),
    ("SELECT * FROM pragma_table_info('t')", "sqlite"),
    ("SELECT market_share FROM t", None),
    (r"SELECT E'it\'s' FROM t", "postgresql"),
    ("SELECT sleep, benchmark, nextval, edit FROM t", None),
    ("SELECT t.sleep, s.pg_sleep FROM t, s", None),
    ("SELECT current_setting('server_version')", "postgresql"),
    ("SELECT 'x' || E'' FROM t WHERE a = 'b'", "postgresql"),
]


@pytest.mark.parametrize(("statement", "dialect"), _REFUSED)
def test_bypass_corpus_is_refused(statement: str, dialect: str | None) -> None:
    with pytest.raises(ToolException):
        check_read_only_sql(statement, dialect)
    with pytest.raises(ToolException):
        check_read_only_sql(statement)  # no dialect: refused under every reading


@pytest.mark.parametrize(("statement", "dialect"), _ACCEPTED)
def test_ordinary_queries_still_pass(statement: str, dialect: str | None) -> None:
    check_read_only_sql(statement, dialect)
    if dialect is None:
        check_read_only_sql(statement)


def test_the_corpus_is_large_enough() -> None:
    assert len(_REFUSED) >= 60 and len(_ACCEPTED) >= 20


def test_names_quoted_for_one_dialect_need_that_dialect() -> None:
    with pytest.raises(ToolException):
        check_read_only_sql("SELECT $$ DELETE $$ AS text")  # a plain word in three dialects
    check_read_only_sql("SELECT $$ DELETE $$ AS text", "postgresql")


def test_the_two_statements_of_the_audit_are_refused() -> None:
    for statement in (
        r"WITH d AS (SELECT E'a\'' AS a), e AS (DELETE FROM t RETURNING 1) SELECT 1 WHERE '' = ''",
        r"SELECT 'a\'', 1 INTO OUTFILE '/tmp/x' , 'b'",
    ):
        with pytest.raises(ToolException):
            check_read_only_sql(statement)


def test_a_backslash_quote_is_refused_when_readings_disagree_and_fine_when_doubled() -> None:
    with pytest.raises(ToolException, match="not closed"):
        check_read_only_sql(r"SELECT 'it\'s' FROM t", "mysql")
    check_read_only_sql("SELECT 'it''s' FROM t", "mysql")


def test_an_unknown_dialect_is_an_error() -> None:
    with pytest.raises(ValueError, match="dialect"):
        check_read_only_sql("SELECT 1", "oracle")


@given(st.text(max_size=160), st.sampled_from([None, "sqlite", "postgresql", "mysql", "mssql"]))
def test_the_check_only_raises_tool_exceptions(statement: str, dialect: str | None) -> None:
    with contextlib.suppress(ToolException):
        check_read_only_sql(statement, dialect)


_WRITES = ["insert into t values (1)", "update t set a = 1", "delete from t", "drop table t"]
_WRAPPERS = [
    "{w}",
    "SELECT 1; {w}",
    "SELECT 1 /* x */ {w}",
    "WITH x AS ({w}) SELECT 1",
    "SELECT 'a\\'' ; {w} ; SELECT '",
    "SELECT 1--1; {w}",
    "SELECT 1 # '\n; {w}; SELECT '",
]


@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_no_wrapper_lets_a_write_through(wrapper: str, write: str) -> None:
    with pytest.raises(ToolException):
        check_read_only_sql(wrapper.format(w=write))


class _Cursor:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.description = [("n",)]
        self.rowcount = 1

    def execute(self, statement: str, parameters: Any = None) -> None:
        self.log.append(statement)

    def fetchmany(self, size: int) -> list[tuple[int]]:
        return [(1,)][:size]

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_sql_query_uses_the_dialect_given_to_the_toolkit() -> None:
    log: list[str] = []
    tools = {
        t.name: t
        for t in sql_toolkit(connect=lambda: _Connection(log), dialect="mysql", frame_output=False)
    }
    out = tools["sql_query"].invoke({"query": r"SELECT 'a\'', 1 INTO OUTFILE '/tmp/x' , 'b'"})
    assert out.startswith("Error") and log == []
    ok = tools["sql_query"].invoke({"query": "SELECT n FROM t"})
    assert '"rows"' in ok and log == ["SELECT n FROM t"]


def test_sql_query_without_a_known_dialect_checks_every_reading() -> None:
    log: list[str] = []
    tools = {t.name: t for t in sql_toolkit(connect=lambda: _Connection(log), frame_output=False)}
    assert (
        tools["sql_query"]
        .invoke({"query": "SELECT 1--1;INSERT INTO t VALUES (1)"})
        .startswith("Error")
    )
    assert log == []


def test_the_sqlite_path_still_runs_on_the_engine_rules(tmp_path: Path) -> None:
    database = tmp_path / "a.db"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE t (a INTEGER)")
        connection.commit()
    tool = {t.name: t for t in sql_toolkit(database, frame_output=False)}["sql_query"]
    assert tool.invoke({"query": "PRAGMA user_version(1337)"}).startswith("Error")
    assert tool.invoke({"query": "SELECT count(*) FROM t"}).startswith("{")


def test_a_dialect_bound_query_needs_a_driver_that_matches() -> None:
    log: list[str] = []
    tools = {t.name: t for t in sql_toolkit(connect=lambda: _Connection(log), frame_output=False)}
    # clean for PostgreSQL only: `$$` strings; the fake driver is no known driver
    out = tools["sql_query"].invoke({"query": "SELECT $$ DELETE $$ AS a"})
    assert out.startswith("Error") and "dialect" in out
    assert log == []
    assert "SELECT" not in "".join(log)


def test_a_plain_query_is_clean_in_every_dialect() -> None:
    from awa.toolkits._sql_guard import DIALECTS, clean_dialects

    assert clean_dialects("SELECT a FROM t WHERE b = 'x'") == DIALECTS
    assert clean_dialects("SELECT $$ DELETE $$ AS a") == ("postgresql",)
    with pytest.raises(ToolException):
        clean_dialects("DELETE FROM t")


class _AutocommitConnection(_Connection):
    def __init__(self, log: list[str], *, sticky: bool) -> None:
        super().__init__(log)
        self._autocommit = True
        self._sticky = sticky

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        if not self._sticky:
            self._autocommit = value


@pytest.mark.parametrize("sticky", [False, True])
def test_autocommit_is_switched_off_or_the_query_is_refused(sticky: bool) -> None:
    log: list[str] = []
    connection = _AutocommitConnection(log, sticky=sticky)
    tools = {
        t.name: t
        for t in sql_toolkit(connect=lambda: connection, dialect="postgresql", frame_output=False)
    }
    out = tools["sql_query"].invoke({"query": "SELECT n FROM t"})
    if sticky:
        assert "autocommit" in out and log == []
    else:
        assert '"rows"' in out and connection.autocommit is False


class _MethodAutocommitConnection(_Connection):
    """A driver whose `autocommit` is a method, as in PyMySQL."""

    def __init__(self, log: list[str], *, sticky: bool, readable: bool = True) -> None:
        super().__init__(log)
        self.state = True
        self._sticky = sticky
        if not readable:
            self.get_autocommit = None

    def autocommit(self, value: bool) -> None:
        if not self._sticky:
            self.state = value

    def get_autocommit(self) -> bool:
        return self.state


@pytest.mark.parametrize(("sticky", "readable"), [(False, True), (True, True), (False, False)])
def test_an_autocommit_method_is_called_and_its_state_confirmed(
    sticky: bool, readable: bool
) -> None:
    log: list[str] = []
    connection = _MethodAutocommitConnection(log, sticky=sticky, readable=readable)
    [query] = sql_toolkit(
        connect=lambda: connection, dialect="mysql", include=["sql_query"], frame_output=False
    )
    out = query.invoke({"query": "SELECT n FROM t"})
    if sticky or not readable:
        assert "autocommit" in out and log == []
    else:
        assert '"rows"' in out and connection.state is False
