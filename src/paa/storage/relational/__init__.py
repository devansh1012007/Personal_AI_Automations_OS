"""Relational substrate — one contract, two dialects.

The relational layer is the ledger's home and the hot-serving operational store.
It exists in two interchangeable forms, chosen by
``StorageSettings.backend_relational``:

``"sqlite"`` (default, the laptop topology, ADR-0001)
    :class:`~paa.storage.relational.database.Database` — embedded, zero-server,
    WAL-mode SQLite. ~0 MB resident overhead.
``"postgres"`` (the Docker topology, ADR-0019)
    :class:`~paa.storage.relational.postgres.PostgresDatabase` — the RFC's
    original server, restored for multi-process/containerised deployment.

Both present the identical async surface (connect/close/transaction/
fetch_all/fetch_one/fetch_value/execute/execute_many), and their DDL is kept
column-for-column in lockstep by ``tests/storage/test_schema_parity.py``.

The SQLite :class:`Database` is imported directly across the codebase as
``from paa.storage.relational.database import Database``. That import path is
unchanged; this package only *adds* the :func:`get_database` factory alongside
it. ``PostgresDatabase`` is imported lazily inside the factory so a laptop
install without the ``postgres`` extra (psycopg) stays importable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from paa.storage.relational.database import (
    SCHEMA_VERSION,
    Database,
    from_iso,
    to_iso,
    utc_now,
)

if TYPE_CHECKING:
    from paa.config import Settings, StorageSettings

__all__ = [
    "SCHEMA_VERSION",
    "Database",
    "from_iso",
    "get_database",
    "to_iso",
    "utc_now",
]

log = structlog.get_logger(__name__)


def get_database(settings: Settings | StorageSettings) -> Any:
    """Build the relational backend named by ``backend_relational``.

    Accepts either the top-level :class:`~paa.config.Settings` or a
    :class:`~paa.config.StorageSettings` directly, so callers that only hold the
    storage sub-model (most of them) need not reach for the whole object.

    Returns an *unconnected* backend — the caller owns the lifecycle and calls
    ``await db.connect()``, exactly as the composition root already does for
    SQLite. This keeps the factory synchronous and side-effect-free, matching
    :func:`paa.storage.queue.get_queue` and
    :func:`paa.storage.coldlake.get_content_store`.
    """
    storage = getattr(settings, "storage", settings)

    if storage.backend_relational == "postgres":
        if not storage.postgres_dsn:
            from paa.core.errors import StorageError

            raise StorageError(
                "backend_relational='postgres' requires storage.postgres_dsn to be set "
                "(e.g. PAA_STORAGE__POSTGRES_DSN=postgresql://...)",
                substrate="postgres",
            )
        # Lazy: importing postgres.py is cheap, but keeping it here documents
        # that the psycopg dependency is only reached on the server topology.
        from paa.storage.relational.postgres import PostgresDatabase

        log.info("relational.backend_selected", backend="postgres")
        return PostgresDatabase(storage.postgres_dsn)

    log.info("relational.backend_selected", backend="sqlite")
    return Database(
        storage.sqlite_path,
        busy_timeout_ms=storage.sqlite_busy_timeout_ms,
    )
