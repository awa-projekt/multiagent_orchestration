"""Fixtures of the integration tests; the PostgreSQL ones need AWA_TEST_POSTGRES_DSN."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import uuid4

import pytest


@pytest.fixture
async def schema_dsn(postgres_dsn: str) -> AsyncIterator[str]:
    """A DSN whose tables land in a fresh, empty schema (dropped afterwards)."""

    import psycopg

    schema = f"awa_it_{uuid4().hex[:10]}"
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in postgres_dsn else "?"
    yield f"{postgres_dsn}{separator}options={quote(f'-csearch_path={schema}')}"
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA {schema} CASCADE")
