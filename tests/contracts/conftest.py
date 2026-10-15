"""Fixtures of the contract tests: where the PostgreSQL backends keep their data."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from awa.testing.contracts import Place

SCHEMA_PREFIX = "awa_test_c"
"""Every schema this suite creates starts like this, so it never touches anything else."""


@pytest.fixture
async def pg_place(tmp_path: Path, postgres_dsn: str) -> AsyncIterator[Place]:
    """A `Place` with its own schema in the test database (tests marked `postgres`).

    The schema is created here and dropped, with its tables, after the test: only the schema
    this fixture made is ever dropped, and the database server is left running.
    """

    import psycopg

    schema = f"{SCHEMA_PREFIX}{uuid4().hex[:10]}"
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield Place(tmp_path, schema, postgres_dsn)
    finally:
        async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
