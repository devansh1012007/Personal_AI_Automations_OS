"""The dialect seam — where SQLite and PostgreSQL genuinely differ.

Two relational backends, one repository layer. The repository layer writes
SQL that both engines accept, with a handful of exceptions; this module is
where those exceptions live, so that no repository has to ask which engine it
is talking to.

Why a module and not ``if backend == "postgres"`` at the call sites: the
repository layer is where the business logic lives, and a branch there is a
branch that has to be re-tested for every new backend. Here, each divergence
is a small, named, unit-testable value — the same pattern
``storage/relational/like.py`` already uses for ``LIKE`` patterns.

What belongs here, and what does not:

* **Value coercion** (how a column's value arrives) — a timestamp column is
  TEXT on SQLite and ``TIMESTAMPTZ`` on PostgreSQL, so the same row hands
  back a ``str`` on one and a ``datetime`` on the other. The coercion is
  *value-level* and lives in the shared helpers (``from_iso``, ``loads``)
  rather than here, because a repository reads a column, it does not execute
  a query about it. The UUID coercion lives beside it, in
  ``paa.core.types`` (``as_uuid`` and ``as_text``), for the same reason: it
  is a *value* coercion, and both substrates' rows go through the same
  helper so they produce the same value. Neither belongs to the seam.
* **SQL fragments** — a handful of expressions that have no portable
  spelling. Each is a short string, assembled once, and asserted in a test
  that needs no database.
* **Locking** — how a transaction serialises writers for one lineage. This
  is the one place the *semantics* differ rather than the spelling, and it is
  the reason the ledger needs a hook rather than a rewritten query.

SPEC DEVIATION (docs/adr/0025): the RFC assumed the two substrates were
interchangeable because their DDL was kept column-for-column identical. It
was not sufficient — the divergence is in the SQL dialect and in the value
types, and a repository that knows neither is the only way to keep them
interchangeable in practice. See ``docs/adr/0025-postgres-dialect-seam.md``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Dialect",
    "PostgresDialect",
    "SqliteDialect",
]


@runtime_checkable
class Dialect(Protocol):
    """The per-engine differences a repository is allowed to know about."""

    #: Short, stable name of the substrate — ``"sqlite"`` / ``"postgres"``.
    name: str

    # -- SQL fragments -----------------------------------------------------

    def scalar_max(self, expression: str, parameter: str) -> str:
        """The two-argument maximum, spelled for this engine.

        SQLite's ``MAX`` is scalar with two arguments; PostgreSQL's ``MAX``
        is strictly an aggregate, and the two-argument form is ``GREATEST``.
        """
        ...

    def alias_values(self, column: str) -> str:
        """Expand a JSON array column into rows, one element per row.

        SQLite has ``json_each``; PostgreSQL has
        ``jsonb_array_elements_text``. Both name their output column
        ``value``, so the caller's ``a.value`` reference works either way.
        """
        ...

    def entity_candidates_sql(self, query: str, limit: int) -> str:
        """The candidate-selection query for entity resolution.

        SQLite has FTS5's trigram matcher and ranks by ``bm25``;
        PostgreSQL has ``pg_trgm`` and ranks by ``similarity``. Neither
        ranks the way the other does, so the *candidate set* is
        engine-specific — the final answer is not, because the Python
        trigram gate re-scores every candidate and keeps the best.

        The engine's own ranking is used only to decide which candidates
        survive the cut, so the SQL is ordered by that engine's metric.

        The returned statement must take exactly the parameters the caller
        supplies — ``(query, limit)`` — so the two engines are
        interchangeable at the call site. The term is bound *once*, through
        a CTE, because the PostgreSQL form needs it in both the predicate
        and the ordering.
        """
        ...

    def arrival_seq(self) -> str:
        """The column that carries insertion order.

        The tiebreak of last resort for two rows written in the same clock
        tick. It is a column on both engines — SQLite's is filled from
        ``rowid`` by a trigger, PostgreSQL's is an identity column — so the
        ordering is a property of the *schema* rather than of the engine's
        internals.
        """
        ...

    # -- locking ----------------------------------------------------------

    async def lock_correlation(self, conn: Any, correlation_id: str) -> None:
        """Serialise writers for one lineage, for the life of the transaction.

        The ledger assigns ``state_version`` by reading the current tip and
        writing ``tip + 1``. That read-then-write is only atomic if two
        writers for the same lineage cannot interleave — on SQLite it is,
        because the backend holds a process-wide write lock for the whole
        transaction. On a server substrate the lock has to be taken in the
        server, or the second writer collides with the first and the append
        fails where SQLite would have succeeded.

        Takes the correlation id so the lock is per-lineage: unrelated
        lineages must not block each other.
        """
        ...


class SqliteDialect:
    """The embedded engine's dialect."""

    name = "sqlite"

    def scalar_max(self, expression: str, parameter: str) -> str:
        return f"MAX({expression}, {parameter})"

    def alias_values(self, column: str) -> str:
        return f"json_each({column})"

    def entity_candidates_sql(self, query: str, limit: int) -> str:
        # FTS5's trigram matcher, ranked by bm25. The FTS5 table is a
        # virtual table, so it is joined by rowid — the entity index's
        # rowid, which is the FTS table's content rowid.
        return (
            "SELECT e.* FROM hot_serving_entity_fts f "
            "JOIN hot_serving_entity_index e ON e.rowid = f.rowid "
            "WHERE hot_serving_entity_fts MATCH ? "
            "ORDER BY bm25(hot_serving_entity_fts) LIMIT ?"
        )

    def arrival_seq(self) -> str:
        return "arrival_seq"

    async def lock_correlation(self, conn: Any, correlation_id: str) -> None:
        """No-op: the write lock is already held for the whole transaction.

        :meth:`paa.storage.relational.database.Database.transaction` takes a
        process-wide write lock before it yields, so two writers in one
        process cannot interleave. Two writers in *two* processes are
        serialised by SQLite's own file locking, which is what
        ``busy_timeout`` waits on.
        """
        return None


class PostgresDialect:
    """The server engine's dialect."""

    name = "postgres"

    def scalar_max(self, expression: str, parameter: str) -> str:
        return f"GREATEST({expression}, {parameter})"

    def alias_values(self, column: str) -> str:
        # The column holds JSON as text on both engines, so the server form
        # casts it to jsonb before expanding it.
        return f"jsonb_array_elements_text(({column})::jsonb)"

    def entity_candidates_sql(self, query: str, limit: int) -> str:
        # pg_trgm's similarity over the entity name and its alias array. The
        # term is bound once, through a CTE, because it appears in both the
        # predicate and the ordering — and the statement has to take exactly
        # the two parameters the caller supplies, so that the two engines are
        # interchangeable at the call site.
        return (
            "WITH q AS (SELECT ?::text AS term) "
            "SELECT e.* FROM hot_serving_entity_index e, q "
            "WHERE similarity(e.canonical_name, q.term) > 0 "
            "   OR similarity(e.aliases, q.term) > 0 "
            "ORDER BY GREATEST("
            "  similarity(e.canonical_name, q.term), "
            "  similarity(e.aliases, q.term)) DESC LIMIT ?"
        )

    def arrival_seq(self) -> str:
        return "arrival_seq"

    async def lock_correlation(self, conn: Any, correlation_id: str) -> None:
        """Take a transaction-scoped advisory lock for this lineage.

        ``pg_advisory_xact_lock`` is held until the transaction ends — commit
        or rollback — so it cannot leak. The key is the correlation id
        hashed to a bigint: a hash collision over-serialises two lineages,
        which costs a little throughput and corrupts nothing.

        The lock is taken *before* the tip is read, so the read-then-write
        that assigns ``state_version`` is atomic per lineage.
        """
        # The cursor is closed: an advisory lock takes no result set, and an
        # unclosed cursor is a connection resource held for the life of the
        # transaction — one per ledger append.
        async with conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))", (correlation_id,)
        ):
            pass
