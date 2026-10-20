"""Shared storage-layer fixtures."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from paa.storage.relational.database import Database
from paa.storage.relational.postgres import PostgresDatabase

#: The PostgreSQL server the server-backed tests run against, or ``None``.
#:
#: The environment variable is the house convention for a test-only switch
#: (``PAA_TEST_VERBOSE`` is the existing example). It names the *server*,
#: and the tests below make a database of their own on it — so a run can
#: never write to the database a deployment is using, whatever the DSN
#: points at.
#:
#: Without it, the server-backed tests skip: the default suite has to stay
#: runnable on a laptop with no PostgreSQL, and a test that fails because
#: there is no server is a test that fails for the wrong reason.
POSTGRES_DSN = os.environ.get("PAA_TEST_POSTGRES_DSN")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """A connected, schema-applied database on a throwaway file."""
    database = Database(tmp_path / "paa.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def postgres_dsn() -> AsyncIterator[str]:
    """A DSN for a database of the test's own, on the configured server.

    The database is created before the test and dropped after it, so a run
    never writes to the database the deployment is using — the DSN names
    the *server*, and the tests make their own database on it.
    """
    if POSTGRES_DSN is None:
        pytest.skip("needs PAA_TEST_POSTGRES_DSN")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    info = conninfo_to_dict(POSTGRES_DSN)
    admin = make_conninfo(**{**info, "dbname": "postgres"})
    name = f"paa_storage_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"

    connection = await psycopg.AsyncConnection.connect(admin, autocommit=True)
    async with connection:
        await connection.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(**{**info, "dbname": name})
    finally:
        connection = await psycopg.AsyncConnection.connect(admin, autocommit=True)
        async with connection:
            await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture
async def postgres(postgres_dsn: str) -> AsyncIterator[PostgresDatabase]:
    """A connected database on the throwaway server, with the schema applied."""
    database = PostgresDatabase(postgres_dsn)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
