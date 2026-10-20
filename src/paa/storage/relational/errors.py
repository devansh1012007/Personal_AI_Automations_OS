"""Backend-neutral storage errors for the relational substrates.

The two relational engines report failures in their own vocabulary:
SQLite raises :class:`sqlite3.IntegrityError` and
:class:`sqlite3.OperationalError`, PostgreSQL raises a hierarchy rooted at
:class:`psycopg.Error` with a distinct class per SQLSTATE. A repository that
catches either has to import the driver — and ``src/paa/ledger/store.py`` is
imported on every laptop install, where psycopg is not installed at all.

So the translation happens at the substrate boundary, once, and the
repository layer catches a *concept* rather than a driver's exception:

``ConstraintViolation``
    A write violated a declared constraint. The subclasses name which
    constraint, because the ledger's two unique constraints mean different
    things: a duplicate idempotency key is a **suppressed retry** (the
    normal outcome of at-least-once delivery), while a duplicate
    ``state_version`` is a **concurrency collision** — two writers that
    raced. Collapsing them into one exception is how a benign retry
    becomes a hard failure.

``SerializationFailure``
    The transaction was aborted because it could not proceed — a deadlock
    was detected, or the engine would have to abort to avoid an anomaly.
    This is *retryable*, and the distinction from a constraint violation
    matters: a caller that retries on this one makes progress, a caller
    that retries on a unique violation does not.

``QueryRejected``
    The statement could not be answered as written — a missing table, a
    missing function, a malformed full-text expression, a placeholder count
    that disagrees with the parameters supplied. It is a *query* problem,
    and a caller that catches it can degrade gracefully (resolve nothing)
    rather than treating it as an infrastructure failure. It is
    deliberately distinct from a constraint violation so that a query bug
    can never be mistaken for a data problem, or the other way round.

    Two subclasses narrow the family, and both are subclasses rather than
    siblings, for reasons stated on each of them:
    :class:`ParameterCountMismatch` (the statement and the parameters
    disagree about how many there are) and :class:`MalformedDocument` (a
    column holds a document the engine cannot read — the one member of the
    family that *is* the data's fault).

Every one of these is a :class:`paa.core.errors.StorageError`, so the
existing ``except PaaError`` handlers at the edges keep working, and each
carries the ``substrate`` that produced it.
"""

from __future__ import annotations

from paa.core.errors import StorageError

__all__ = [
    "CheckViolation",
    "ConstraintViolation",
    "ForeignKeyViolation",
    "MalformedDocument",
    "NotNullViolation",
    "ParameterCountMismatch",
    "QueryRejected",
    "SchemaVersionError",
    "SerializationFailure",
    "UniqueViolation",
]


class ConstraintViolation(StorageError):
    """A write violated a declared constraint."""

    def __init__(self, message: str, *, substrate: str, **details: object) -> None:
        super().__init__(message, substrate=substrate, **details)


class UniqueViolation(ConstraintViolation):
    """A unique constraint was violated."""

class CheckViolation(ConstraintViolation):
    """A check constraint was violated."""


class ForeignKeyViolation(ConstraintViolation):
    """A foreign key constraint was violated."""


class NotNullViolation(ConstraintViolation):
    """A NOT NULL constraint was violated."""


class SerializationFailure(StorageError):
    """The transaction could not proceed and was aborted.

    Retryable: the same work, re-run, may well succeed. Distinct from a
    constraint violation, which is a statement about the *data* and will
    fail identically however many times it is retried.
    """


class QueryRejected(StorageError):
    """The statement could not be answered as written.

    A missing table, a missing function, a malformed full-text
    expression, a placeholder count that disagrees with the parameters
    supplied. The engine could not even begin to answer the question, so
    there is nothing to retry.

    The class is deliberately distinct from a constraint violation so that
    a query bug can never be mistaken for a data problem, or the other way
    round — with one member of the family that *is* a data problem, and
    that member is named: :class:`MalformedDocument`. It is a subclass
    rather than a sibling because the query genuinely cannot be answered
    in that case too, and because the one caller written to fail closed on
    a bad query catches this family and must keep catching it. A caller
    that *can* tell the difference between "the query is wrong" and "the
    row is wrong" now can, because the two are different classes.
    """


class ParameterCountMismatch(QueryRejected):
    """The statement and the parameters disagree about how many there are.

    This is the one class of statement bug the ``?``-to-``%s`` placeholder
    translator can introduce, and it is named rather than left to the
    generic case because it is a *bug in this codebase's SQL* rather than
    anything the engine or the data did: SQLite reports it as
    "Incorrect number of bindings supplied", psycopg as "the query has N
    placeholders but M parameters were passed".

    A subclass of :class:`QueryRejected` because the statement cannot be
    answered at all — and because the repository path written to fail
    closed on a bad query catches that family. A miscounted placeholder
    reaching the caller as a generic storage error would take the whole
    ingestion batch down, which is exactly what the fail-closed contract
    exists to prevent.
    """


class MalformedDocument(QueryRejected):
    """A column holds a document the engine cannot read.

    The JSON columns are text on both engines, and the server dialect
    casts them back to ``jsonb`` to expand them — so a row whose column
    holds something that is not JSON raises ``22P02
    invalid_text_representation``. That is the *data's* fault, and this
    class is where that fact is recorded rather than being hidden inside
    the generic query-rejected case.

    It is a subclass of :class:`QueryRejected` because the query still
    cannot be answered, and because the repository path written to fail
    closed on a bad query must keep catching it: entity resolution has to
    degrade to "no match" rather than take the ingestion batch down, and
    a corrupt row must not be the thing that stops it. The subclass is
    what lets a caller that *wants* to treat a corrupt row differently
    from a broken query still say so.
    """


class SchemaVersionError(StorageError):
    """The database's schema version is not one this build can work with.

    Three ways to get here, and they are not the same problem:

    * the database is **newer** than the build. Refused rather than opened,
      because an older build writing to a newer schema is how a ledger is
      corrupted — the build does not know which columns the newer version
      added, so it will write a row the newer version cannot read.
    * the version log itself is inconsistent — a gap in it, or a version
      recorded that the shape does not have. Refused, because the highest
      version in the log is the only thing the build would otherwise use to
      decide what to run, and a log that lies about it is worse than no log.
    * the **shape** is incomplete — some of the tables the schema declares
      are there and some are not, so the schema was applied only part of
      the way. The database is not at any version: it is not the base
      version, and it is not a version the log records. Refused by name, so
      the operator is told which tables are missing rather than being left
      to meet a missing table in some later query.

    All three are the same class because they are the same *decision*: the
    build cannot describe this database's shape, so it refuses it rather
    than guessing.
    """
