"""Shared storage-layer fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from paa.storage.relational.database import Database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """A connected, schema-applied database on a throwaway file."""
    database = Database(tmp_path / "paa.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
