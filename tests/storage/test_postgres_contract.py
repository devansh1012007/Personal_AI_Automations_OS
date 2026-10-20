"""The contract the two relational substrates must keep with each other.

Why this file exists
--------------------
Everything the storage layer promises is written down in *one* place — the
neutral error vocabulary in ``paa.storage.relational.errors`` — and the
promise is that a repository above it does not have to know which substrate
produced the row it is looking at, or which substrate failed.

That promise was broken in three ways, and all three were invisible to the
suite because the suite only ever ran the SQLite half:

* the server substrate's transaction boundary translated **every**
  exception that came out of a transaction body, including the runtime's
  own — so a lost race for a ledger version arrived as a
  ``StorageError`` there and as a ``LedgerError`` on SQLite, and a caller
  that catches ``LedgerError`` to recognise the race caught nothing on the
  server;
* a corrupt row in the entity table raised its error from the *alias*
  lookup, and only the candidate lookup had a fail-closed handler — so one
  unreadable row took down entity resolution for every term on the server,
  while the SQLite half failed closed;
* a NUL byte in a term was refused by SQLite's full-text matcher (which
  the candidate lookup catches) and by psycopg at the *parameter*, before
  any statement was sent — so the same term failed closed on one substrate
  and raised on the other.

Each of those is a divergence between two substrates that are supposed to
be interchangeable, and each is exactly the kind of thing the SQLite-only
suite cannot see. The fix for all three is in the substrate layer; this
file is what proves the fix.

Pointing the suite at a server
------------------------------
The tests need a PostgreSQL server, named by ``PAA_TEST_POSTGRES_DSN``:

.. code-block:: bash

    docker run -d --name paa-test-pg \\
        -e POSTGRES_USER=paa -e POSTGRES_PASSWORD=paa \\
        -e POSTGRES_DB=postgres -p 127.0.0.1:55460:5432 postgres:16
    export PAA_TEST_POSTGRES_DSN="postgresql://paa:paa@127.0.0.1:55460/postgres"
    pytest tests/storage/test_postgres_contract.py

Without that variable the module **skips**: the default suite has to stay
runnable on a laptop with no server.

The DSN names a *server*, not a database to write to. Every test here
creates a database of its own and drops it when it is over — so a run can
never touch the database the deployment is using.

The SQLite half of each contract lives in the ordinary suite
(``tests/storage/test_errors.py``, ``tests/memory/test_facts.py``,
``tests/ledger/test_store.py``); the assertions here are the *same* ones,
run against the other substrate.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from paa.core.errors import LedgerError
from paa.core.types import new_correlation_id
from paa.ledger.events import EventType, LedgerEvent
from paa.ledger.store import LedgerStore
from paa.memory.facts import FactRepository
from paa.storage.relational.postgres import PostgresDatabase

#: The server the tests run against, or ``None`` when there is none.
DSN = os.environ.get("PAA_TEST_POSTGRES_DSN")

HAS_PSYCOPG = (
    importlib.util.find_spec("psycopg") is not None
    and importlib.util.find_spec("psycopg_pool") is not None
)

pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.skipif(
        not HAS_PSYCOPG or not DSN,
        reason="needs the 'postgres' extra and PAA_TEST_POSTGRES_DSN",
    ),
]


class TestTheExceptionVocabulary:
    """What a transaction body raises is what comes out of it.

    The transaction boundary is the one place where the boundary is wider
    than a single statement: the block it wraps contains the repository's
    own code. So the exception that comes out may be the driver's, one of
    ours, or a plain programming error — and the last two are not the
    driver's to translate.
    """

    async def test_the_ledgers_own_error_keeps_its_type(self, postgres: PostgresDatabase) -> None:
        """A lost race is a ``LedgerError`` on both substrates.

        The ledger raises ``LedgerError`` when an insert is refused by a
        constraint that is not the idempotency key — another writer took
        the version first. A caller that catches that class to recognise
        the race has to catch it on the server substrate too, or the two
        report the same domain failure with different types and the caller
        retries forever.
        """
        store = LedgerStore(postgres)
        correlation = new_correlation_id()

        with pytest.raises(LedgerError):
            async with postgres.transaction():
                raise LedgerError(
                    "ledger append violated an integrity constraint that is not "
                    "the idempotency key",
                    correlation_id=str(correlation),
                )

        # The store still works, and the lineage is untouched.
        assert await store.count() == 0
        assert await store.read_correlation(correlation) == []

    async def test_a_programming_error_keeps_its_type(self, postgres: PostgresDatabase) -> None:
        """A ``TypeError`` in the body is a bug in the repository.

        Reported as an infrastructure fault it sends the operator looking
        in the wrong place — and it makes the two substrates disagree about
        the same code, which is the one thing the neutral vocabulary
        exists to prevent.
        """
        with pytest.raises(TypeError):
            async with postgres.transaction():
                raise TypeError("a repository bug")

    async def test_a_driver_error_is_still_translated(self, postgres: PostgresDatabase) -> None:
        """The boundary is not closed: the driver's errors are still ours.

        A statement the server refuses is a storage failure, and it has to
        arrive as one — otherwise a repository that catches
        ``StorageError`` to report "the database is unhappy" catches
        nothing on the server.
        """
        from paa.core.errors import StorageError

        with pytest.raises(StorageError):
            async with postgres.transaction() as conn:
                await conn.execute("SELECT * FROM a_table_that_is_not_there")


class TestTheFailClosedContract:
    """A term the engine cannot answer resolves to no match.

    The repository layer is written to fail closed: the ingestion batch
    that asked the question must not go down because the question was
    malformed or the table holds a row it cannot read. The contract is
    cross-substrate — a term that resolves to nothing on SQLite has to
    resolve to nothing on the server — and the two engines refuse the
    same inputs in different places, so the refusal has to happen in the
    repository rather than in the driver.
    """

    async def test_a_nul_byte_in_a_term_resolves_to_no_match(
        self, postgres: PostgresDatabase
    ) -> None:
        """The refusal is in the repository, so it is the same on both.

        SQLite truncates a string at a NUL; psycopg refuses the bound
        parameter outright, with an error that carries no SQLSTATE and so
        no classification a caller could catch. Refused in one place, the
        behaviour is the same everywhere.
        """
        repo = FactRepository(postgres)
        await repo.upsert_entity("project", "Widget Service")

        assert await repo.resolve_entity("a\x00b") is None
        assert await repo.resolve_entity("\x00") is None
        # And the refusal did not leave the repository broken.
        resolved = await repo.resolve_entity("Widget Service")
        assert resolved is not None and resolved.canonical_name == "Widget Service"

    async def test_a_corrupt_row_resolves_to_no_match(self, postgres: PostgresDatabase) -> None:
        """One unreadable row does not break resolution for every term.

        The alias column is JSON held as *text* on both substrates, and the
        server dialect casts it back to ``jsonb`` to expand it. A row
        holding something that is not JSON makes that cast fail — and
        because the expansion scans the table, one corrupt row breaks the
        lookup for *every* term.
        """
        repo = FactRepository(postgres)
        await repo.upsert_entity("project", "Widget Service")
        await postgres.execute(
            "UPDATE hot_serving_entity_index SET aliases = 'not json at all' "
            "WHERE canonical_name = 'Widget Service'"
        )

        assert await repo.resolve_entity("Gadget Assembly") is None

    async def test_the_ledger_suppresses_a_redelivered_event(
        self, postgres: PostgresDatabase
    ) -> None:
        """The idempotency contract holds on the server substrate.

        The same event delivered twice is written once, and the second
        delivery is told what the first one wrote — which is what makes a
        redelivery safe.
        """
        store = LedgerStore(postgres)
        correlation = new_correlation_id()
        event = LedgerEvent.create(correlation, EventType.TASK_REQUESTED)

        first = await store.append(event)
        second = await store.append(LedgerEvent.create(correlation, EventType.TASK_REQUESTED))

        assert second.event_id == first.event_id
        assert second.state_version == first.state_version
        assert await store.count() == 1


class TestTheIdentifierShape:
    """A row that comes back from the server carries the types the model wants.

    The models declare their identifiers as text; the server hands back
    ``uuid.UUID`` for a ``UUID`` column. The reader coerces, and the two
    substrates produce the same value.
    """

    async def test_an_entity_id_arrives_as_text(self, postgres: PostgresDatabase) -> None:
        """The coercion is the contract, not an accident of the substrate."""
        repo = FactRepository(postgres)
        entity_id = await repo.upsert_entity("project", "Widget Service")

        assert isinstance(entity_id, str)
        resolved = await repo.resolve_entity("Widget Service")
        assert resolved is not None
        assert isinstance(resolved.id, str)
        assert resolved.id == entity_id
