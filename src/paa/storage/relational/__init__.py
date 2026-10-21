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
it.

**Both backend names are bound at module scope, and the reason is the
annotation itself.** Under ``from __future__ import annotations`` every
annotation is a *string*, so a name that is bound only for the type checker is
**absent** from the module's namespace — and every runtime consumer of that
annotation then fails: ``typing.get_type_hints`` raises ``NameError``,
FastAPI's dependency resolution raises it at route-registration time, and a
pydantic model annotating the name raises ``PydanticUndefinedAnnotation`` at
class-creation time. The factory's return type names the two backends it can
hand back, so both names are real names here.

This is **not** a cost paid for the ``postgres`` extra.
:mod:`paa.storage.relational.postgres` imports ``psycopg`` and ``psycopg_pool``
lazily — inside ``_require_psycopg()`` — so this package, and that module
itself, import cleanly on a machine with no ``postgres`` extra installed. An
earlier version of this docstring gave the extra as the reason the name was
left unbound, and the reason was false: the extra was never the constraint.
The constraint was the annotation, and unbinding the name is what made it
unreadable.

What *is* lazy is the **client**. The factory builds a
:class:`~paa.storage.relational.postgres.PostgresDatabase` only when
``backend_relational`` asks for one, so a laptop install never touches the
server backend, and the factory stays side-effect-free: it returns an
*unconnected* backend either way.
"""

from __future__ import annotations

import structlog

from paa.config import Settings, StorageSettings
from paa.storage.relational.database import (
    SCHEMA_VERSION,
    Database,
    from_iso,
    to_iso,
    utc_now,
)
from paa.storage.relational.postgres import PostgresDatabase

__all__ = [
    "SCHEMA_VERSION",
    "Database",
    "PostgresDatabase",
    "from_iso",
    "get_database",
    "to_iso",
    "utc_now",
]

log = structlog.get_logger(__name__)


def get_database(settings: Settings | StorageSettings) -> Database | PostgresDatabase:
    """Build the relational backend named by ``backend_relational``.

    Accepts either the top-level :class:`~paa.config.Settings` or a
    :class:`~paa.config.StorageSettings` directly, so callers that only hold the
    storage sub-model (most of them) need not reach for the whole object.

    Returns an *unconnected* backend — the caller owns the lifecycle and calls
    ``await db.connect()``, exactly as the composition root already does for
    SQLite. This keeps the factory synchronous and side-effect-free, matching
    :func:`paa.storage.queue.get_queue` and
    :func:`paa.storage.coldlake.get_content_store`.

    The return type is the **union of the two backends the factory can
    build**, not ``Any`` and not the abstract contract. ``Any`` would hand
    every caller a backend whose method names are unchecked — a typo at the
    call site would pass the type checker and fail at runtime — and the
    protocol in :mod:`paa.storage.relational.base` would throw away the one
    fact the factory exists to establish: *which* substrate was selected. The
    union says it, and it is still assignable to that protocol, so a
    repository written against the contract is unaffected.
    """
    # The two accepted inputs are the whole settings object and the storage
    # sub-model alone, and the sub-model is what carries the answers. The
    # two are told apart by *kind* rather than by the presence of an
    # attribute: ``getattr(settings, "storage", settings)`` would do the same
    # job at runtime, but it hides from the type checker which of the two it
    # is holding, and a checker that cannot see the difference cannot check
    # the field accesses below. The narrowing is a real one — the two
    # classes are unrelated, and the sub-model is a field of the whole
    # object — so it costs nothing and states the contract the signature
    # already states.
    storage = settings if isinstance(settings, StorageSettings) else settings.storage

    if storage.backend_relational == "postgres":
        if not storage.postgres_dsn:
            from paa.core.errors import StorageError

            raise StorageError(
                "backend_relational='postgres' requires storage.postgres_dsn to be set "
                "(e.g. PAA_STORAGE__POSTGRES_DSN=postgresql://...)",
                substrate="postgres",
            )
        # The client is built only here, and only when it was asked for: the
        # factory is lazy about the *backend*, not about the import. Nothing
        # about constructing it needs psycopg — the pool is created at
        # ``connect()`` — so the import above is not a cost either.
        log.info("relational.backend_selected", backend="postgres")
        return PostgresDatabase(storage.postgres_dsn)

    log.info("relational.backend_selected", backend="sqlite")
    return Database(
        storage.sqlite_path,
        busy_timeout_ms=storage.sqlite_busy_timeout_ms,
    )
