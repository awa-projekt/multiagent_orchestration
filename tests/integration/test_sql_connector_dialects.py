"""The `sql` connector against real MySQL/MariaDB, SQL Server and PostgreSQL databases.

Each case runs when its variable names a disposable database (skipped otherwise):
`AWA_TEST_MYSQL_DSN` (`mysql://user:password@host:port/db`), `AWA_TEST_MSSQL_DSN`
(`mssql://user:password@host:port/db`), `AWA_TEST_POSTGRES_DSN`.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from _helpers.framing import unframe
from awa.connectors import _local, check_connector, connector_tools

CASES = {
    "mysql": "AWA_TEST_MYSQL_DSN",
    "mssql": "AWA_TEST_MSSQL_DSN",
    "postgresql": "AWA_TEST_POSTGRES_DSN",
}
# Read at import: the suite's fixtures clear AWA_* variables before each test.
URLS = {dialect: os.environ.get(variable) for dialect, variable in CASES.items()}


@pytest.mark.parametrize("dialect", list(CASES))
def test_the_sql_connector_reads_writes_with_approval_off_and_stays_read_only(dialect: str) -> None:
    url = URLS[dialect]
    if not url:
        pytest.skip(f"set {CASES[dialect]} to a disposable database")
    if dialect == "postgresql":
        url = re.sub(r"^postgres(ql)?://", "postgresql://", url)
    env = {"DATABASE_URL": url}
    tools = {
        tool.name: tool
        for tool in connector_tools(
            "sql", env=env, config={"requires_approval": False, "accept_text_check": True}
        )
    }
    execute, query = tools["sql_execute"], tools["sql_query"]
    execute.invoke({"statement": "DROP TABLE IF EXISTS awa_test_orders"})
    created = execute.invoke(
        {
            "statement": "CREATE TABLE awa_test_orders (id INT PRIMARY KEY, customer VARCHAR(40), "
            "total DECIMAL(10, 2))"
        }
    )
    assert "Error" not in created, created
    for row in ([1, "Müller GmbH", 1234.56], [2, "Bäckerei Groß", 45]):
        statement = "INSERT INTO awa_test_orders VALUES (%s, %s, %s)"
        assert "rows_affected" in execute.invoke({"statement": statement, "parameters": row})
    answer = json.loads(
        unframe(query.invoke({"query": "SELECT customer, total FROM awa_test_orders ORDER BY id"}))
    )
    assert answer["columns"] == ["customer", "total"]
    assert answer["rows"][0][0] == "Müller GmbH" and float(answer["rows"][0][1]) == 1234.56
    assert "only SELECT or WITH" in query.invoke({"query": "DELETE FROM awa_test_orders"})
    if dialect != "mssql":  # the database itself refuses changes in the query session
        connection = _local._connector(dialect, url, read_only=True)()
        try:
            cursor = connection.cursor()
            with pytest.raises(Exception, match=r"(?i)read.only"):
                cursor.execute("DELETE FROM awa_test_orders")
        finally:
            connection.rollback()
            connection.close()
    assert check_connector("sql", env=env).ok
    execute.invoke({"statement": "DROP TABLE awa_test_orders"})
