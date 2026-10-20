"""Schema migrations — the step list that carries a database between versions.

Why this module exists
----------------------
``schema_sqlite.sql`` and ``schema_postgres.sql`` are *create-from-scratch*
scripts: every statement in them is ``CREATE ... IF NOT EXISTS``. That makes
them a perfect description of a brand-new database and a **useless** upgrade
path, because ``CREATE TABLE IF NOT EXISTS`` does not add a column to a table
that already exists. A schema change therefore only ever reached a database
that did not yet exist.

The first schema change this repository ever attempted — the
``arrival_seq`` ordering columns, and JSON documents stored as text rather
than ``JSONB`` — found that gap. The version marker existed
(``schema_migrations``), but a marker is not machinery: nothing ever read it
to decide *what* to run. So this module supplies the missing half.

The model
---------
A database's **version** is the highest schema version it conforms to. A
fresh database is created from the ``.sql`` file, which describes the current
shape, and is recorded at the current version. An existing database is
carried forward one version at a time by the :data:`MIGRATIONS` list: each
migration is a set of **steps**, and each step is a set of **guarded
statements**.

A step is *guarded* because a migration must be safe to re-run. The guard is
a statement that returns a row when the guarded work is **still needed** and
no row when it is not; the statements run only when the guard comes back
with a row. So a crash between two statements of a step leaves the step
unapplied, and the next start re-runs it — and re-running it is a no-op for
the parts that already landed. The version row is written only after every
step of the migration has run, so the version is never ahead of the shape.

The two engines spell their DDL differently, so a step carries both halves.
That is not a layering violation: the DDL that carries a schema from one
version to the next *is* engine-specific by nature. What stays shared is the
*decision* — which versions are missing, which statements are guarded, and
what a version means — and that lives here.

One runner, two engines
-----------------------
The *algorithm* — read the version log, decide what the database is, carry it
forward one version at a time, write the version row last — is engine-
independent, and it lives in :class:`MigrationRunner` below. What the two
backends still own is the residue that genuinely differs:

* the DDL text (the ``.sql`` file, and the per-engine halves of a step),
* the version-recording statement,
* the substrate string and the engine name,
* how a DDL script is applied atomically, and how a statement is run outside
  a transaction.

Two copies of the runner would be two copies of the *rules* — and a fix to one
would silently not land in the other, which is the failure mode this module
is supposed to prevent everywhere else.

Telling a fresh database from an old one
-----------------------------------------
A database with no version rows is either brand new or a real database whose
version was never recorded. The old build applied the schema and recorded the
version as two separate statements, so a crash in between left tables with no
version row. Those are two different situations and they must not be
confused: treating a real database as new would stamp it with a version its
shape does not have, and no later start would ever repair it, because the
version it claims is the version that says it needs no repair.

So the runner asks the *catalog* rather than the log, and the catalog answers
in three states:

``FRESH``
    No runtime table at all. The ``.sql`` file is applied, and every version
    up to the current one is recorded.
``V1_COMPLETE``
    Every table the base version declares is present. This is a database at
    the base version whose version was never written down, and it is
    migrated forward.
``INCOMPLETE``
    *Some* of the tables are present. This is not a database at any version:
    it is the shape a schema application leaves behind when it is interrupted
    half-way, and it is refused with a message that names what is missing.
    Migrating it would produce a database whose version log describes a shape
    it does not have.

The expected table set is read from the ``.sql`` file rather than written out
here, so a table added to the schema cannot be forgotten here. The reader
that parses the file lives in :mod:`paa.storage.relational.schema_parser`,
because the runtime and the parity test have to agree on what a table in
the file *is* — the test used to carry its own copy of the parser, and two
copies of a DDL lexer is one copy too many. It is a lexer rather than a
regular expression because a *phantom* name is not a cosmetic error: a
table the reader invented is a table the runtime refuses a whole database
over.

The schema file is applied **atomically**. On PostgreSQL that is free: the
whole file is one statement inside one transaction. On SQLite it is not:
``executescript`` runs the file statement by statement in autocommit, so a
failure half-way through leaves the tables it created behind — and a database
in that state is indistinguishable, to a naive reader, from a database at the
base version. The SQLite backend therefore applies the file inside a
transaction of its own; see
:meth:`paa.storage.relational.database.Database._apply_ddl`.

One connection, one schema, one lock
------------------------------------
The migration runs on **one** connection, and it is not a pooled one.

One connection, because the schema file's ``CREATE TABLE`` statements are
unqualified, and an unqualified name lands in whichever schema the
connection's ``search_path`` resolves to. Resolving the schema on one
connection and applying the file on another is a race with the session
state, and the pool makes it worse rather than better: a pooled connection
is handed back with its session state intact, so one ``SET search_path``
anywhere would move the resolution and the DDL together, into a schema
nobody chose. So the runner resolves the schema on the connection it then
applies the file on, and every statement it issues afterwards either names
the schema explicitly or is the version log's own ``CREATE TABLE`` — which
the same connection places in the schema the runner resolved.

One lock, because two processes may start at once. On PostgreSQL the
transport takes an advisory lock for the whole migration, so the second
process waits rather than re-running the first one's steps underneath it —
a redundant re-run is *meant* to be harmless (every guard tests the effect
of its step, not a counter), but it was not harmless: two processes
migrating the same database at once raced each other onto an identity
column, and the loser failed its start-up. On SQLite no lock is needed:
every write the runtime opens is an exclusive one.

SPEC DEVIATION (docs/adr/0025): the RFC assumed the two substrates were
interchangeable because their DDL was kept column-for-column identical. It
was not sufficient — the divergence is in the SQL dialect, in the value types,
and in the DDL that upgrades one to the next. See
``docs/adr/0025-postgres-dialect-seam.md``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from paa.core.errors import StorageError
from paa.storage.relational.connection import TransactionConnection
from paa.storage.relational.errors import QueryRejected, SchemaVersionError
from paa.storage.relational.like import JSONB_DEFAULT_PATTERN
from paa.storage.relational.schema_parser import (
    SchemaParseError,
    SchemaTable,
    parse_tables,
)

__all__ = [
    "ARRIVAL_SEQUENCES",
    "ARRIVAL_TRIGGERS",
    "BASE_SCHEMA_VERSION",
    "INSERT_VERSION_POSTGRES",
    "INSERT_VERSION_SQLITE",
    "JSONB_COLUMNS",
    "JSONB_COLUMN_COUNT",
    "MIGRATIONS",
    "SCHEMA_PLACEHOLDER",
    "SCHEMA_VERSION",
    "VERSION_LOG_TABLE",
    "ArrivalSequence",
    "Engine",
    "EngineStep",
    "GuardedStatement",
    "Migration",
    "MigrationRunner",
    "MigrationStep",
    "MigrationTransport",
    "SchemaTable",
    "description_for",
    "pending",
    "runtime_tables",
    "schema_ddl",
    "schema_file",
]

#: The two relational engines this module speaks for.
#:
#: A closed set rather than a free string: the engine name picks a ``.sql``
#: file, a version-recording statement and a statement dialect, and a typo
#: in any of them is a runtime failure in the middle of a migration. Naming
#: the two values moves that failure to the type checker.
Engine = Literal["sqlite", "postgres"]

#: The schema version the ``.sql`` files describe.
#:
#: Version 1 is the initial PAA v4.1 schema — the shape the two ``.sql``
#: files were born with. Every version above it is a migration in
#: :data:`MIGRATIONS`.
BASE_SCHEMA_VERSION = 1

#: The table the schema version log lives in.
#:
#: It is the runner's own bookkeeping rather than one of the runtime's
#: tables, so it is not part of the set a database at the base version is
#: expected to have: a database that predates the log is still a database at
#: the base version, and the log is created for it when the first version
#: row is written.
VERSION_LOG_TABLE = "schema_migrations"

#: The token a PostgreSQL statement carries where the target schema goes.
#:
#: The schema is not known when the step is written — it is the
#: connection's ``search_path`` that decides where an unqualified name
#: would land, so it is resolved from the connection when the migration
#: runs. The statement carries this token where the schema belongs, and
#: the runner replaces it with the schema it resolved, so the guard and
#: the statement the guard guards act on the same object.
SCHEMA_PLACEHOLDER = "$schema$"

#: How a version is recorded, per engine.
#:
#: The two spellings differ because the engines' conflict syntax differs, not
#: because the meaning does: one row per version, the version it reached, and
#: what that version changed.
#:
#: The PostgreSQL spelling carries the schema placeholder, like the migration
#: statements do. The log has to be the log of the schema the migration is
#: carrying: a log that lives in another schema describes another schema's
#: shape, and a runner that believed it would skip the migrations that
#: repair this one.
INSERT_VERSION_SQLITE = (
    "INSERT OR IGNORE INTO schema_migrations(version, applied_at, description) "
    "VALUES (?, ?, ?)"
)
INSERT_VERSION_POSTGRES = (
    "INSERT INTO $schema$.schema_migrations(version, applied_at, description) "
    "VALUES (?, ?, ?) ON CONFLICT (version) DO NOTHING"
)

#: The version-recording statement, by engine.
_RECORD_VERSION_SQL: dict[Engine, str] = {
    "sqlite": INSERT_VERSION_SQLITE,
    "postgres": INSERT_VERSION_POSTGRES,
}

#: The statement that lists the tables a database holds, by engine.
#:
#: SQLite has one schema, so the question has one answer; PostgreSQL has as
#: many as the search path names, and the answer is the schema the
#: unqualified names resolve to — the same one the guards ask about and the
#: DDL names.
_LIST_TABLES_SQL: dict[Engine, str] = {
    "sqlite": (
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ),
    "postgres": "SELECT tablename FROM pg_tables WHERE schemaname = ?",
}

#: The statement that reads the version log, by engine.
#:
#: The same on both engines, except that PostgreSQL names the schema the log
#: lives in: the log describes the shape of one schema, and the runner has
#: to read the log of the schema it is carrying.
_VERSION_ROWS_SQL: dict[Engine, str] = {
    "sqlite": "SELECT version FROM schema_migrations ORDER BY version",
    "postgres": "SELECT version FROM $schema$.schema_migrations ORDER BY version",
}

#: The statement that resolves the schema an unqualified name lands in.
#:
#: ``current_schema()`` is the first schema in the search path that exists,
#: which is exactly where an unqualified ``CREATE TABLE`` puts its table and
#: exactly where an unqualified ``SELECT`` reads it from.
_CURRENT_SCHEMA_SQL = "SELECT current_schema()"

#: The schema file each engine's DDL comes from.
_SCHEMA_FILES: dict[Engine, Path] = {
    "sqlite": Path(__file__).with_name("schema_sqlite.sql"),
    "postgres": Path(__file__).with_name("schema_postgres.sql"),
}


@dataclass(frozen=True, slots=True)
class GuardedStatement:
    """Statements that run only when the guard says they are still needed.

    ``guard`` is a statement that returns a row when the guarded work has
    **not** been done yet, and no row when it has. It takes the parameters in
    ``guard_params`` and is expected to take no more than a handful — a
    table name and a column name, usually.

    ``statements`` are applied in order when the guard comes back with a row.
    An empty tuple means "this engine has nothing to do for this part".

    The guard tests the **effect** the statements have, and only that
    effect. A guard that some *other* statement is responsible for
    producing would be satisfied by that statement's work and would stop
    this block from ever running — the failure this rule prevents is
    concrete: a step that adds a column and then fills it has two states to
    repair, "the column does not exist" and "the column exists but was
    never filled", and one guard for both would be satisfied by the
    statement that adds the column, so the backfill would never run.

    Several statements may share **one** guard when they are steps towards
    one effect rather than repairs of separate states, and when the guard
    asks about the *finished* effect rather than an intermediate one. The
    PostgreSQL ``arrival_seq`` step is that case: its six statements — add
    the column, number the rows that are there, forbid nulls, drop any
    identity, attach an identity, and move the sequence past the rows that
    are there — all serve the single effect "this table has a monotonic
    insertion order", and the guard asks whether that effect is present:
    the column is there, it is an identity, it was generated *always*, and
    it is not nullable. The six therefore run together or not at all, and
    they are ordered so that the last of them leaves the finished effect
    the guard asks about. A guard on an *intermediate* state — "the column
    exists" — would be the mistake: the add would satisfy it and the
    backfill would never run.

    ``schema_scoped`` marks a guard that reads the catalog of one named
    schema — every ``information_schema`` guard on PostgreSQL, because that
    catalog spans every schema the user can see, while the statement the
    guard guards acts on one. Such a guard names the schema through
    :data:`SCHEMA_PLACEHOLDER`, exactly as the statement it guards does,
    and the runner renders the schema into it.

    The schema is *in the statement* rather than appended to the
    parameters because a parameter is a **value** and a schema is an
    **identifier**: it cannot be bound, it has to be written in. And a
    guard whose parameters and whose placeholders could disagree — the
    schema last, the guard's own parameters first — is a silent trap: a
    mis-ordered guard matches no row, so the step is skipped, and the
    version row is written anyway. A database that claims a shape it does
    not have is exactly what this module exists to prevent.
    """

    guard: str
    guard_params: tuple[Any, ...]
    statements: tuple[str, ...]
    schema_scoped: bool = False


@dataclass(frozen=True, slots=True)
class EngineStep:
    """One engine's half of a migration step.

    An ordered list of guarded statements. The order matters: a step that
    adds a column and then fills it needs the add to come first.
    """

    guarded: tuple[GuardedStatement, ...] = ()

    def __bool__(self) -> bool:
        """Whether this engine has anything to do for the step."""
        return bool(self.guarded)


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """One unit of work inside a migration.

    ``table`` and ``column`` name what the step is about, and they are
    **interpolated into the statements** — the SQL a step carries is written
    from these two names rather than spelling them out a second time, so
    there is one source of truth for what the step touches and a renamed
    column cannot leave the SQL pointing at the old one. They are also what
    a reader, a test, and the shape comparison use to describe the step.
    """

    description: str
    table: str
    column: str
    sqlite: EngineStep
    postgres: EngineStep

    def for_engine(self, engine: Engine) -> EngineStep:
        """The half of this step that belongs to ``engine``."""
        if engine == "sqlite":
            return self.sqlite
        if engine == "postgres":
            return self.postgres
        raise ValueError(f"unknown relational engine: {engine!r}")


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema version, and the steps that reach it."""

    version: int
    description: str
    steps: tuple[MigrationStep, ...]


def pending(current: int) -> tuple[Migration, ...]:
    """The migrations a database at ``current`` still needs.

    Ordered by version, and contiguous: a database at version *n* runs every
    migration above *n* in turn.
    """
    return tuple(m for m in MIGRATIONS if m.version > current)


def description_for(version: int) -> str:
    """What changed in schema version ``version``.

    The description is a property of the *version*, not of the act of
    migrating: a database created fresh from the ``.sql`` file conforms to
    every version at once, and a database that was migrated forward one
    version at a time conforms to the same versions. The two must read the
    same, or the log stops being a description of the database and starts
    being a description of how the database was born.
    """
    if version == BASE_SCHEMA_VERSION:
        return "initial PAA v4.1 schema"
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration.description
    raise KeyError(f"unknown schema version: {version}")


# ---------------------------------------------------------------------------
# The schema files
# ---------------------------------------------------------------------------


def schema_file(engine: Engine) -> Path:
    """The ``.sql`` file that describes a fresh database on ``engine``."""
    try:
        return _SCHEMA_FILES[engine]
    except KeyError:
        raise ValueError(f"unknown relational engine: {engine!r}") from None


@cache
def schema_ddl(engine: Engine) -> str:
    """The DDL that creates a fresh database on ``engine``."""
    return schema_file(engine).read_text(encoding="utf-8")


@cache
def schema_tables(engine: Engine) -> dict[str, SchemaTable]:
    """Every table the engine's schema file declares.

    The reader is :func:`paa.storage.relational.schema_parser.parse_tables`,
    and it is a lexer rather than a regular expression: a string literal, a
    block comment or a nested expression is all a place where a reader can
    mistake text for code, and a *phantom* table is a table the runtime
    would refuse a whole database over.
    """
    path = schema_file(engine)
    return parse_tables(schema_ddl(engine), source=path.name)


@cache
def runtime_tables(engine: Engine) -> frozenset[str]:
    """The tables a database at the base version has.

    Read from the schema file rather than written out here, so that a table
    added to the schema cannot be forgotten here — a forgotten table is a
    database that is refused as incomplete, or one that is migrated as if it
    were whole when it is not.

    The version log is not one of them: it is the runner's own bookkeeping,
    and a database that predates it is still a database at the base version.
    """
    return frozenset(schema_tables(engine)) - {VERSION_LOG_TABLE}


@cache
def version_log_ddl(engine: Engine) -> str:
    """The ``CREATE TABLE`` for the version log, as the schema file writes it.

    Taken from the file — and from the file's *own text*, comments and all
    — so the log the runner writes into is the log the schema describes:
    same columns, same types, same strictness, and the same statement a
    fresh database was built from. A statement read from a stripped copy
    of the file would create a table whose recorded SQL differed from the
    one a fresh database gets: the same table, described two ways.
    """
    return schema_tables(engine)[VERSION_LOG_TABLE].statement


# ---------------------------------------------------------------------------
# Step builders
# ---------------------------------------------------------------------------

#: A note on the ``# noqa: S608`` markers below.
#:
#: Ruff's bandit rule fires on a statement that is *built* rather than
#: written, and a migration's statements are built: a step names its table
#: and its column, and the SQL is written from those two names so a
#: renamed column cannot leave the SQL pointing at the old one. The
#: marker goes where ruff **reports** the finding — which is the first
#: line of the statement, not necessarily the line that does the
#: interpolating. A line ruff does not report needs no marker, and adding
#: one would be reported itself as an unused ``noqa``.


@dataclass(frozen=True, slots=True)
class ArrivalSequence:
    """How one table's insertion-order column is produced.

    ``column`` is the column's name, ``trigger`` is the SQLite trigger that
    fills it, and ``order_column`` is the PostgreSQL column the backfill
    orders the rows that already exist by.

    All three live in one record because they are one fact about one table:
    the column, and the two engine-specific mechanisms that fill it. A
    migration that named the wrong ``order_column`` would still run, and
    would silently number the rows in the wrong order — so the name is not
    a parameter that can be mistyped at a call site, it is a value looked up
    from a table that has to exist.
    """

    column: str
    trigger: str
    order_column: str


#: The tables that carry an insertion-order column, and how each is filled.
#:
#: SQLite has no identity column, so the value is filled by a trigger that
#: copies the row's ``rowid``. A ``rowid`` is reused once the row that held
#: it is deleted — but it is reused only *after* being freed, so no two live
#: rows ever carry the same one, and the order among the live rows is still
#: the order they were written in. The backfill therefore uses ``rowid`` too:
#: the column has to mean the same thing before and after the migration, or
#: a row written before the migration could be given an arrival order that a
#: later row reuses.
#:
#: PostgreSQL has a real sequence behind an identity column, which is
#: monotonic and never reused by construction. That asymmetry is the reason
#: the two engines cannot share one statement, and it is the reason the
#: dialect seam exists at all.
ARRIVAL_SEQUENCES: dict[str, ArrivalSequence] = {
    "cold_lake_signals": ArrivalSequence(
        column="arrival_seq",
        trigger="trg_cold_signals_arrival",
        order_column="received_at",
    ),
    "queue_messages": ArrivalSequence(
        column="arrival_seq",
        trigger="trg_queue_messages_arrival",
        order_column="enqueued_at",
    ),
}

#: The trigger that fills each table's ``arrival_seq``, on SQLite.
#:
#: Named here rather than derived, because the same name appears in
#: ``schema_sqlite.sql`` — the schema file and the migration both create the
#: trigger, and a database created one way and a database migrated the other
#: way have to end up with the same trigger under the same name. The names
#: are also exported so the test that locks the two together reads them from
#: one place.
ARRIVAL_TRIGGERS: dict[str, str] = {
    table: sequence.trigger for table, sequence in ARRIVAL_SEQUENCES.items()
}


def _arrival_seq_step(table: str) -> MigrationStep:
    """The step that gives ``table`` a monotonic insertion-order column.

    The backfill numbers the rows that already exist, so that the column
    means the same thing for a row written before the migration as for one
    written after it. It cannot reconstruct the original insertion order —
    no column records it — so each engine uses its own native proxy for it:
    the rowid on SQLite, and the timestamp the row carries on PostgreSQL.
    What the backfill guarantees, on both engines, is a *total* order over
    the rows that exist, which is what the ``LIMIT`` cut needs.

    Three guarded statements on SQLite, one on PostgreSQL. The split matters:
    a column that exists but was never backfilled is a different state from
    a column that does not exist, and only the first of the three repairs
    the second.

    The PostgreSQL half names the schema through :data:`SCHEMA_PLACEHOLDER`: the
    statements act on one schema, and the guard that decides whether they
    are needed reads that same schema's catalog.

    Its six statements share **one** guard, and the guard asks about the
    finished effect — "this table has a monotonic insertion order" — rather
    than about an intermediate state. See :class:`GuardedStatement` for why
    that is the rule and what the alternative would break.

    The PostgreSQL half adds the column with ``ALTER TABLE ... ADD
    COLUMN``, which appends it. The migrated table's column order therefore
    matches the fresh schema's only because ``arrival_seq`` is the *last*
    column declared in ``schema_postgres.sql``; a column added to the schema
    file above it would leave a migrated database with a different column
    order from a fresh one. A test holds that position down.
    """
    sequence = ARRIVAL_SEQUENCES[table]
    column = sequence.column
    trigger = sequence.trigger
    return MigrationStep(
        description=f"{table} gains a monotonic insertion-order column",
        table=table,
        column=column,
        sqlite=EngineStep(
            guarded=(
                GuardedStatement(
                    guard=(
                        "SELECT 1 WHERE NOT EXISTS ("
                        "  SELECT 1 FROM pragma_table_info(?) WHERE name = ?"
                        ")"
                    ),
                    guard_params=(table, column),
                    statements=(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0",
                    ),
                ),
                GuardedStatement(
                    guard=f"SELECT 1 FROM {table} WHERE {column} = 0 LIMIT 1",  # noqa: S608
                    guard_params=(),
                    statements=(f"UPDATE {table} SET {column} = rowid",),  # noqa: S608
                ),
                GuardedStatement(
                    guard=(
                        "SELECT 1 WHERE NOT EXISTS ("
                        "  SELECT 1 FROM sqlite_master "
                        "  WHERE type = 'trigger' AND name = ?"
                        ")"
                    ),
                    guard_params=(trigger,),
                    statements=(
                        f"DROP TRIGGER IF EXISTS {trigger}",
                        (
                            f"CREATE TRIGGER {trigger} "  # noqa: S608
                            f"AFTER INSERT ON {table} "
                            "BEGIN "
                            f"  UPDATE {table} SET {column} = NEW.rowid "
                            "WHERE rowid = NEW.rowid; "
                            "END"
                        ),
                    ),
                ),
            )
        ),
        postgres=EngineStep(
            guarded=(
                GuardedStatement(
                    guard=(
                        "SELECT 1 WHERE NOT EXISTS ("  # noqa: S608
                        "  SELECT 1 FROM information_schema.columns "
                        "  WHERE table_name = ? AND column_name = ? "
                        f"    AND table_schema = {SCHEMA_PLACEHOLDER} "
                        "    AND is_identity = 'YES' AND is_nullable = 'NO' "
                        "    AND identity_generation = 'ALWAYS'"
                        ")"
                    ),
                    guard_params=(table, column),
                    schema_scoped=True,
                    statements=(
                        f"ALTER TABLE {SCHEMA_PLACEHOLDER}.{table} "
                        f"ADD COLUMN IF NOT EXISTS {column} BIGINT",
                        (
                            f"UPDATE {SCHEMA_PLACEHOLDER}.{table} AS s "  # noqa: S608
                            f"SET {column} = ranked.rn "
                            "FROM ("
                            "  SELECT id, ROW_NUMBER() OVER (ORDER BY "
                            f"{sequence.order_column}, id) AS rn"
                            f"  FROM {SCHEMA_PLACEHOLDER}.{table}"
                            ") AS ranked "
                            "WHERE ranked.id = s.id"
                        ),
                        f"ALTER TABLE {SCHEMA_PLACEHOLDER}.{table} "
                        f"ALTER COLUMN {column} SET NOT NULL",
                        f"ALTER TABLE {SCHEMA_PLACEHOLDER}.{table} "
                        f"ALTER COLUMN {column} DROP IDENTITY IF EXISTS",
                        (
                            f"ALTER TABLE {SCHEMA_PLACEHOLDER}.{table} "
                            f"ALTER COLUMN {column} ADD GENERATED ALWAYS AS IDENTITY"
                        ),
                        (
                            "SELECT setval("  # noqa: S608
                            "  pg_get_serial_sequence('"
                            f"{SCHEMA_PLACEHOLDER}.{table}'"
                            f", '{column}'),"
                            f"  (SELECT COALESCE(MAX({column}), 0) + 1 "
                            f"  FROM {SCHEMA_PLACEHOLDER}.{table}),"
                            "  false)"
                        ),
                    ),
                ),
            )
        ),
    )


#: The JSON columns that were ``JSONB`` in schema version 1.
#:
#: A ``JSONB`` column re-serialises its value: the key order is normalised,
#: the numeric literals are re-rendered, and the whitespace is the server's.
#: The ledger hashes its payload and re-verifies it on read, so a round trip
#: through ``JSONB`` would make ``verify_chain`` report false tampering on
#: every event. The cold lake scans ``raw_payload``'s raw bytes for hostile
#: control characters, and ``JSONB`` would have re-serialised them out of
#: sight. Both columns are therefore text on both engines, and the bytes a
#: writer produced are the bytes that are stored.
#:
#: The third element of each row is the column's default, in the text form
#: the new schema uses. The type conversion carries the old default across
#: as a ``'{}'::jsonb`` literal, which casts to the right value but leaves
#: the catalog describing a default in terms of a type the column no longer
#: has. The default is therefore restated, so a migrated database and a
#: fresh one are identical down to the catalog.
#:
#: A column with no default has no default to restate, and gets no second
#: guarded block for one: a guard that can never fire is a statement that
#: can never run, and a statement that can never run is a place a bug can
#: hide.
JSONB_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("system_state_ledger", "payload", "{}"),
    ("system_state_snapshots", "projection", None),
    ("cold_lake_signals", "raw_payload", None),
    ("hot_serving_entity_index", "aliases", "[]"),
    ("hot_serving_entity_index", "attributes", "{}"),
    ("hot_serving_active_facts", "provenance", "{}"),
    ("hot_serving_unresolved_buffer", "incumbent_fact", None),
    ("hot_serving_unresolved_buffer", "challenger_fact", None),
    ("hot_serving_sessions", "metadata", "{}"),
    ("hot_serving_skill_registry", "input_schema", None),
    ("hot_serving_skill_registry", "output_schema", None),
    ("hot_serving_skill_registry", "required_permissions", "[]"),
    ("hot_serving_skill_registry", "invocation", "{}"),
    ("hot_serving_agent_registry", "allowed_tools", "[]"),
    ("hot_serving_agent_registry", "granted_permissions", "[]"),
    ("hot_serving_execution_runs", "telemetry", "{}"),
    ("hot_serving_evaluation_results", "findings", "[]"),
    ("hot_serving_recovery_marks", "manifest", None),
    ("queue_messages", "payload", None),
    ("marketplace_packages", "manifest", None),
)

#: How many columns version 3 converts.
#:
#: A fact about schema version 1 — the shape the repository was born with —
#: and not a fact about the list above: the two are held together by a test
#: that asserts the list has exactly this many entries and that every one
#: of them names a column the current schema still has.
JSONB_COLUMN_COUNT: Final[int] = 20


def _json_to_text_step(table: str, column: str, default: str | None) -> MigrationStep:
    """The step that retypes one ``JSONB`` column to text.

    SQLite's half is empty: its columns were always text, and its
    ``json_valid`` CHECK is the validity guarantee the server column no
    longer has. The server's half converts the column in place, and the
    default with it — a ``'{}'::jsonb`` default becomes a ``'{}'::text``
    default, which is the same value written the same way.

    Two guarded blocks, because they repair two different states: a column
    that is still ``jsonb``, and a column that is text but whose default is
    still expressed in terms of a type the column no longer has. One guard
    for both would let the first satisfy the second, and the default would
    keep naming ``jsonb`` forever.

    A column with no default has only the first state, and so only the
    first block.

    The second block's guard looks for ``::jsonb`` in the catalog's
    description of the default. That is a LIKE pattern, and it is bound as
    a parameter rather than written into the statement — the pattern itself
    lives in :mod:`paa.storage.relational.like` with every other one. The
    schema is named in the statement rather than appended to the
    parameters, for the reason :class:`GuardedStatement` gives.
    """
    guarded: list[GuardedStatement] = [
        GuardedStatement(
            guard=(
                "SELECT 1 WHERE NOT EXISTS ("  # noqa: S608
                "  SELECT 1 FROM information_schema.columns "
                "  WHERE table_name = ? AND column_name = ? "
                f"    AND table_schema = {SCHEMA_PLACEHOLDER} AND data_type = 'text'"
                ")"
            ),
            guard_params=(table, column),
            schema_scoped=True,
            statements=(
                f"ALTER TABLE {SCHEMA_PLACEHOLDER}.{table} "
                f"ALTER COLUMN {column} TYPE TEXT USING {column}::text",
            ),
        )
    ]
    if default is not None:
        guarded.append(
            GuardedStatement(
                guard=(
                    "SELECT 1 FROM information_schema.columns "  # noqa: S608
                    "WHERE column_default LIKE ? "
                    "  AND table_name = ? AND column_name = ? "
                    f"  AND table_schema = {SCHEMA_PLACEHOLDER} "
                    "LIMIT 1"
                ),
                guard_params=(JSONB_DEFAULT_PATTERN, table, column),
                schema_scoped=True,
                statements=(
                    f"ALTER TABLE {SCHEMA_PLACEHOLDER}.{table} "
                    f"ALTER COLUMN {column} SET DEFAULT '{default}'",
                ),
            )
        )
    return MigrationStep(
        description=f"{table}.{column} stores its JSON as text, not as a parsed document",
        table=table,
        column=column,
        sqlite=EngineStep(),
        postgres=EngineStep(guarded=tuple(guarded)),
    )


def _trace_id_step(table: str, column: str) -> MigrationStep:
    """The step that puts every trace id in the same form on both substrates.

    A trace id is a UUID. SQLite stores it as text — whatever text it was
    given — and PostgreSQL's ``UUID`` column normalises whatever it is
    given to the hyphenated form. So a trace id written as 32 hex
    characters was stored in two different forms by the two substrates, and
    a join between a run row and the span that belongs to it matched on one
    and not on the other.

    The fix is at the source: trace ids are *written* in the hyphenated
    form, which both substrates then store identically. This step brings
    the rows that predate that in line — on SQLite, where the unhyphenated
    form is what is on disk. PostgreSQL needs nothing: its column has
    always normalised.
    """
    return MigrationStep(
        description="trace ids are stored in the canonical hyphenated form",
        table=table,
        column=column,
        sqlite=EngineStep(
            guarded=(
                GuardedStatement(
                    guard=(
                        f"SELECT 1 FROM {table} "  # noqa: S608
                        f"WHERE length({column}) = 32 LIMIT 1"
                    ),
                    guard_params=(),
                    statements=(
                        (
                            f"UPDATE {table} SET {column} = "  # noqa: S608
                            f"  substr({column}, 1, 8) || '-' || "
                            f"substr({column}, 9, 4) || '-' || "
                            f"substr({column}, 13, 4) || '-' || "
                            f"substr({column}, 17, 4) || '-' || "
                            f"substr({column}, 21, 12) "
                            f"WHERE length({column}) = 32"
                        ),
                    ),
                ),
            )
        ),
        postgres=EngineStep(),
    )


def _span_parent_id_step(table: str, column: str) -> MigrationStep:
    """The step that makes a parent *span* id storable on the server.

    A span id is 64 bits — sixteen hex characters — and is not a UUID. The
    column that holds it was a ``UUID`` on PostgreSQL, which refused the
    only value it was ever given. It is text on both substrates now.
    """
    return MigrationStep(
        description="a parent span id is text, because a span id is not a UUID",
        table=table,
        column=column,
        sqlite=EngineStep(),
        postgres=EngineStep(
            guarded=(
                GuardedStatement(
                    guard=(
                        "SELECT 1 WHERE NOT EXISTS ("  # noqa: S608
                        "  SELECT 1 FROM information_schema.columns "
                        "  WHERE table_name = ? AND column_name = ? "
                        f"    AND table_schema = {SCHEMA_PLACEHOLDER} "
                        "    AND data_type = 'text'"
                        ")"
                    ),
                    guard_params=(table, column),
                    schema_scoped=True,
                    statements=(
                        (
                            f"ALTER TABLE {SCHEMA_PLACEHOLDER}.{table} "
                            f"ALTER COLUMN {column} TYPE TEXT "
                            f"USING {column}::text"
                        ),
                    ),
                ),
            )
        ),
    )


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=2,
        description="insertion-order columns on the cold lake and the queue",
        steps=(
            _arrival_seq_step("cold_lake_signals"),
            _arrival_seq_step("queue_messages"),
        ),
    ),
    Migration(
        version=3,
        description="JSON documents stored as text rather than as parsed documents",
        steps=tuple(
            _json_to_text_step(table, column, default) for table, column, default in JSONB_COLUMNS
        ),
    ),
    Migration(
        version=4,
        description="the run record's identifiers, in one form on both substrates",
        steps=(
            _trace_id_step("hot_serving_execution_runs", "trace_id"),
            _span_parent_id_step("hot_serving_execution_runs", "span_parent_id"),
        ),
    ),
)

#: The schema version this build expects a database to be at.
SCHEMA_VERSION = MIGRATIONS[-1].version if MIGRATIONS else BASE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class MigrationTransport(Protocol):
    """What the shared runner needs from a relational backend.

    The runner's rules are engine-independent, so what it asks of a backend
    is engine-independent too: a way to read a statement's rows outside a
    write transaction, a way to open an all-or-nothing write transaction,
    a way to apply a DDL script all-or-nothing, and a way to release
    whatever the transport held while the migration ran. Everything else —
    the SQL, the version bookkeeping, the refusal rules — lives in the
    runner, so the two backends cannot drift apart on them.

    A transport is expected to hold **one** connection for the whole
    migration, and to keep the migration serialised against other
    processes while it does. PostgreSQL's transport takes an advisory lock
    for the duration; SQLite's needs none, because every write it opens
    is an exclusive one.
    """

    async def read(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Run one statement outside a write transaction, and return its rows."""
        ...

    def transaction(self) -> AbstractAsyncContextManager[TransactionConnection]:
        """Open an all-or-nothing write transaction."""
        ...

    async def apply_ddl(self, ddl: str) -> None:
        """Apply a DDL script — all of it, or none of it."""
        ...

    async def aclose(self) -> None:
        """Release whatever the transport held for the migration."""
        ...


def _quote_identifier(name: str) -> str:
    """A PostgreSQL identifier, quoted.

    A schema name is an identifier, not a value, so it cannot be bound as a
    parameter — it has to be written into the statement, and the only safe
    way to write one is to quote it.
    """
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """A PostgreSQL string literal, quoted.

    The counterpart of :func:`_quote_identifier`, for the one place the
    schema appears as a **value** rather than as a name: a guard that
    compares the catalog's ``table_schema`` column against the schema it
    means. There, ``"paa"`` would name a *column* called ``paa``, and the
    comparison would fail with "column does not exist" rather than match.
    """
    return "'" + value.replace("'", "''") + "'"


def _applied_at() -> str:
    """The timestamp a version row carries.

    The format is :func:`paa.storage.relational.database.to_iso`'s, and it
    is borrowed rather than re-spelled: that module imports this one, so
    the import has to happen at call time rather than at import time, or
    the two would be an import cycle.
    """
    from paa.storage.relational.database import to_iso, utc_now

    return to_iso(utc_now())


class MigrationRunner:
    """Carries a database to the schema version this build expects.

    One algorithm, two engines. The runner reads the version log, decides
    what the database is, and carries it forward one version at a time. The
    engine-specific parts are the DDL, the version-recording statement, the
    substrate string and the engine name; everything else is shared, so a
    change to the rules lands on both substrates at once.

    The rules, in the order they are checked. There are **six** outcomes,
    and each one says what the database *is* rather than what the log says:

    1. the recorded version is **newer** than this build's — refused. An
       older build writing to a newer schema is how a ledger is corrupted,
       and the refusal says so rather than failing later in a way that
       blames the data.
    2. the recorded versions are **not contiguous** — refused. A gap means
       the highest version no longer describes the shape, and taking it at
       face value would skip the migration that would have repaired the
       database.
    3. the recorded version is **not the shape's** — refused. The log
       claims a version the catalog does not corroborate: the database
       records version 4 and is missing a table the runtime needs. The log
       is a claim about the database, and a claim is only worth as much as
       the evidence behind it.
    4. there are **no version rows** and the database holds **no runtime
       table** — a fresh database. The ``.sql`` file is applied, and every
       version up to the current one is recorded.
    5. there are **no version rows** and the database holds **every**
       runtime table — a database at the base version whose version was
       never recorded down. It is migrated forward.
    6. there are **no version rows** and the database holds **some** of
       the runtime tables — an incomplete schema, which is refused by name
       rather than migrated. Migrating it would produce a version log that
       describes a shape the database does not have.

    Cases 4, 5 and 6 are the three answers the catalog can give to a
    database with no version log, and they are the reason the runner asks
    the catalog rather than the log: a log that is empty is not the same
    thing as a database that is empty. Case 3 is the same question asked
    of a database that *does* have a log.

    Every refusal is a :class:`~paa.storage.relational.errors.SchemaVersionError`,
    so a caller that catches that class catches every way the runner can
    refuse a database.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        substrate: str,
        log_prefix: str,
        transport: MigrationTransport,
        log: Any,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._engine = engine
        self._substrate = substrate
        self._log_prefix = log_prefix
        self._transport = transport
        self._log = log
        self._details = dict(details or {})
        self._schema: str | None = None

    # -- the whole job -----------------------------------------------------

    async def run(self) -> None:
        """Bring the database to the schema version this build expects."""
        try:
            await self._run()
        finally:
            # The transport holds a connection and a lock for the duration
            # of the migration; a refusal must release them too, or the
            # next start finds the database locked by a process that has
            # already gone.
            await self._transport.aclose()

    async def _run(self) -> None:
        """The migration itself, with the transport's resources held."""
        self._schema = await self._target_schema()
        versions = await self._version_rows()
        current = versions[-1] if versions else 0

        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema version {current} is newer than the "
                f"{SCHEMA_VERSION} this build understands; upgrade the "
                "runtime, or point it at a database it can read",
                substrate=self._substrate,
                schema_version=current,
                **self._error_details(),
            )
        if versions and versions != list(range(1, current + 1)):
            # A gap in the log means a version was recorded without the
            # migration that produced it — or the log was edited. Either
            # way the highest version no longer describes the shape, and
            # taking it at face value would skip the migration that would
            # have repaired the database.
            raise SchemaVersionError(
                f"the schema version log has a gap: it records {versions}, "
                f"which is not a contiguous run from 1 to {current}. The "
                "database's shape is not described by its version, so it is "
                "not migrated",
                substrate=self._substrate,
                schema_version=current,
                **self._error_details(),
            )

        if versions:
            # The log is not the only evidence. A log that claims a
            # version the *shape* does not have is refused as well: the
            # highest version in the log is the only thing the build would
            # otherwise use to decide what to run, and a log that lies
            # about it is worse than no log.
            await self._refuse_an_incomplete_shape(self._expected_tables(), current)
            for migration in pending(current):
                await self._run_migration(migration)
            return

        # No version rows. Either the database is brand new, or it was
        # created by a build that applied the schema and was interrupted
        # before it recorded the version. The second case is a real
        # database, and treating it as new would stamp it with a version
        # its shape does not have — so the tables are asked about directly.
        present = await self._present_tables()
        expected = self._expected_tables()

        if not present & expected:
            await self._create_schema()
            await self._record_versions(range(1, SCHEMA_VERSION + 1))
            self._log.info(f"{self._log_prefix}.schema_applied", version=SCHEMA_VERSION)
            return

        missing = expected - present
        if missing:
            # Some of the tables are there and some are not. This is what a
            # schema application leaves behind when it is interrupted half-
            # way, and it is not a database at any version: migrating it
            # would produce a version log that describes a shape the
            # database does not have.
            #
            # The refusal is a ``SchemaVersionError`` like the other two:
            # a caller that catches that class catches every way the
            # runner can refuse a database, rather than having to know
            # that one of the three arrives as a bare storage error.
            raise SchemaVersionError(
                "the database's schema is incomplete: "
                f"{len(missing)} of the {len(expected)} tables the runtime "
                "needs are missing ("
                + ", ".join(sorted(missing))
                + "). The schema was applied only part of the way, so the "
                "database is not at any schema version and is not migrated. "
                "Restore it from a backup, or point the runtime at a "
                "database it can create from scratch",
                substrate=self._substrate,
                missing_tables=sorted(missing),
                **self._error_details(),
            )

        # Every runtime table is present and the log is empty: a database at
        # the base version whose version was never written down.
        await self._record_versions(range(1, BASE_SCHEMA_VERSION + 1))
        for migration in pending(BASE_SCHEMA_VERSION):
            await self._run_migration(migration)

    def _expected_tables(self) -> frozenset[str]:
        """The tables a database at the base version has.

        Read from the schema file rather than written out, so a table
        added to the schema cannot be forgotten here.

        A schema file the reader cannot understand is a defect in the
        build, and it is reported as one: a ``StorageError``, so a caller
        that catches ``PaaError`` catches it, carrying the reader's own
        message about which file and which construct.
        """
        try:
            return runtime_tables(self._engine)
        except SchemaParseError as exc:
            raise StorageError(
                f"the {self._engine} schema file could not be read: {exc}",
                substrate=self._substrate,
                **self._error_details(),
            ) from exc

    async def _refuse_an_incomplete_shape(self, expected: frozenset[str], current: int) -> None:
        """Refuse a database whose shape is not the one its version claims.

        The version log is a claim about the database, and a claim is only
        worth as much as the evidence behind it. The evidence here is the
        catalog: a database that records version 4 but is missing a table
        the runtime needs is not a database at version 4, and migrating it
        would write a version row over a shape that does not exist.
        """
        present = await self._present_tables()
        missing = expected - present
        if not missing:
            return
        raise SchemaVersionError(
            f"the schema version log records version {current}, but the "
            f"database does not have the shape that version describes: "
            f"{len(missing)} of the {len(expected)} tables the runtime needs "
            "are missing (" + ", ".join(sorted(missing)) + "). The log is not "
            "believed, and the database is not migrated",
            substrate=self._substrate,
            schema_version=current,
            missing_tables=sorted(missing),
            **self._error_details(),
        )

    def _error_details(self) -> dict[str, Any]:
        """The details every refusal carries.

        The schema the runner resolved is one of them: a server holds
        several databases and each of them several schemas, and a refusal
        that does not say which one it is about leaves the operator
        looking at the wrong database.
        """
        details = dict(self._details)
        if self._schema is not None:
            details.setdefault("schema", self._schema)
        return details

    # -- the pieces --------------------------------------------------------

    async def _target_schema(self) -> str | None:
        """The schema an unqualified name in this database resolves to.

        SQLite has one schema, so there is nothing to resolve. PostgreSQL
        has as many as the search path names, and a name written without a
        schema lands in the first one that exists — which is therefore the
        schema the guards must ask about and the DDL must name, so that
        the guard and the statement it guards act on the same object.

        The schema is read on the transport's own connection, and so is
        everything the migration then does: the DDL is applied on the same
        connection, which is what makes the unqualified ``CREATE TABLE`` in
        the schema file land in the schema this method resolved. A
        *pooled* connection would not do: the pool does not reset a
        connection's session state when it hands it back, so one
        ``SET search_path`` anywhere would move the resolution and the DDL
        together, into a schema nobody chose.
        """
        if self._engine != "postgres":
            return None
        rows = await self._transport.read(_CURRENT_SCHEMA_SQL)
        if not rows or rows[0][0] is None:
            raise StorageError(
                "the database's search path names no schema that exists, so "
                "the tables the runtime needs cannot be found",
                substrate=self._substrate,
                **self._error_details(),
            )
        return str(rows[0][0])

    async def _present_tables(self) -> set[str]:
        """Every table the database holds."""
        if self._engine == "postgres":
            rows = await self._transport.read(
                _LIST_TABLES_SQL["postgres"],
                (self._schema,) if self._schema is not None else (),
            )
        else:
            rows = await self._transport.read(_LIST_TABLES_SQL["sqlite"])
        return {str(row[0]) for row in rows}

    async def _version_rows(self) -> list[int]:
        """Every version the log records, in order.

        A database with no version log at all is not an error: the log is
        created by the schema, and a database that predates it — or one
        whose schema was applied and then interrupted before the log was
        written — has none. Only the *absence of the table* is read as "no
        log"; every other failure is a real failure and propagates, because
        a database that cannot be read is not a database that needs
        creating.

        The read happens on the transport's own connection, outside a write
        transaction, and that is deliberate:

        * the read is finished before any transaction is opened, so the two
          are never held at once and there is nothing to deadlock over;
        * every statement the migration issues names the schema explicitly
          — the placeholder is rendered into it — and the base DDL is
          applied on the *same* connection the schema was resolved on, so
          the unqualified names in it land in the schema the runner
          resolved whatever the session's ``search_path`` says;
        * under READ COMMITTED the read can only be *stale* if another
          process is migrating the same database at the same moment. The
          transport holds an advisory lock for the whole migration, so the
          two cannot be inside it at once: a process that read the log
          before the other committed waits, and then re-runs only the steps
          whose guards say the work is still outstanding. Every guard tests
          the *effect* of its step rather than a counter, so a re-run of a
          step another process has already completed changes nothing, and
          the version row is written with ``ON CONFLICT DO NOTHING``. A
          stale read therefore costs a redundant re-run, not a wrong shape
          — and the re-run cannot fail the second process's start-up,
          because the other process's work has already committed by the
          time the second one looks.
        """
        try:
            rows = await self._transport.read(self._render(_VERSION_ROWS_SQL[self._engine]))
        except QueryRejected:
            return []  # the table is absent => no version log
        versions: list[int] = []
        for row in rows:
            try:
                versions.append(int(row[0]))
            except (TypeError, ValueError) as exc:
                # The log is a file an operator can edit. A row that is not
                # a version is not a version this build can reason about,
                # and reading it as one would be a guess.
                raise StorageError(
                    f"the schema version log holds a row that is not a "
                    f"version: {row[0]!r}. The log is not read, and the "
                    "database is not migrated",
                    substrate=self._substrate,
                    **self._error_details(),
                ) from exc
        return versions

    async def _create_schema(self) -> None:
        """Create the database's tables from the engine's ``.sql`` file.

        The whole file, all of it or none of it: a schema that was applied
        half-way is not a database at any version, and the only way to be
        sure the file was applied whole is to apply it whole.
        """
        await self._transport.apply_ddl(schema_ddl(self._engine))

    async def _record_versions(self, versions: Iterable[int]) -> None:
        """Write a version row for each of ``versions``."""
        async with self._transport.transaction() as conn:
            await self._ensure_version_log(conn)
            for version in versions:
                await self._record_version(conn, version, description_for(version))

    async def _ensure_version_log(self, conn: TransactionConnection) -> None:
        """Make sure the version log exists before a row is written to it.

        The log is created by the schema file, but a database that predates
        the log has the tables and not the log — and the log is what says
        which version the database is at, so it has to be there before the
        first version can be recorded. The statement is the one the schema
        file uses, so the table it creates is the table the schema
        describes.
        """
        await self._execute(conn, version_log_ddl(self._engine))

    async def _run_migration(self, migration: Migration) -> None:
        """Carry the database one version forward.

        The whole migration runs in one transaction, so a step that fails
        leaves the database at the version it was at — the version row is
        written last, and a database whose version row is behind its shape
        is re-migrated rather than trusted.

        A failure is reported as a :class:`~paa.core.errors.StorageError`.
        The driver error that caused it is the *cause*, not the message a
        caller sees: a caller that catches ``PaaError`` at the edge of the
        runtime must not have to know that SQLite and PostgreSQL report
        their failures in different vocabularies.
        """
        try:
            async with self._transport.transaction() as conn:
                for step in migration.steps:
                    await self._run_step(conn, step.for_engine(self._engine))
                await self._record_version(conn, migration.version, migration.description)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"schema migration to version {migration.version} failed: {exc}",
                substrate=self._substrate,
                **self._error_details(),
            ) from exc
        self._log.info(f"{self._log_prefix}.schema_migrated", version=migration.version)

    async def _run_step(self, conn: TransactionConnection, step: EngineStep) -> None:
        for guarded in step.guarded:
            if not await self._still_needed(conn, guarded):
                continue
            for statement in self._statements_for(guarded):
                await self._execute(conn, statement)

    async def _still_needed(
        self, conn: TransactionConnection, guarded: GuardedStatement
    ) -> bool:
        """Whether the guarded work has not been done yet.

        The guard returns a row when the work is still outstanding. Anything
        else — no rows — means it is done, or was never applicable.
        """
        guard, params = self._guard_for(guarded)
        async with conn.execute(guard, params) as cur:
            return await cur.fetchone() is not None

    async def _execute(self, conn: TransactionConnection, sql: str) -> None:
        """Run one statement.

        Nothing is fetched: a migration's statements are DDL, and a DDL
        cursor has no rows to fetch — asking for them is an error on one of
        the two engines.
        """
        async with conn.execute(sql):
            pass

    async def _record_version(
        self, conn: TransactionConnection, version: int, description: str
    ) -> None:
        """Write the version row.

        It is written last, after the shape it describes — a version row is
        a claim about the database, and a claim made before the fact is a
        lie the next start believes.
        """
        async with conn.execute(
            self._render(_RECORD_VERSION_SQL[self._engine]),
            (version, _applied_at(), description),
        ):
            pass

    # -- rendering ---------------------------------------------------------

    def _render(self, sql: str) -> str:
        """Write the resolved schema into a statement that *names* an object.

        A statement that names a table has to name the *schema* the table is
        in, or it names a table in whichever schema the search path happens
        to resolve — and the guard that decides whether the statement is
        needed at all reads one schema's catalog, which spans all of them.

        The schema is an identifier here, so it is quoted as one.
        """
        if self._schema is None:
            return sql
        return sql.replace(SCHEMA_PLACEHOLDER, _quote_identifier(self._schema))

    def _render_comparison(self, sql: str) -> str:
        """Write the resolved schema into a statement that *compares* one.

        The counterpart of :meth:`_render`, for the one place the schema
        appears as a value rather than as a name: a guard that compares the
        catalog's ``table_schema`` column against the schema it means. A
        quoted *identifier* would name a column there, and the comparison
        would fail rather than match.

        It is written into the statement rather than bound as a parameter
        because a parameter's position is a promise — the guard's own
        parameters come first, the schema last — and a guard that breaks
        that promise matches no row and skips its step in silence.
        """
        if self._schema is None:
            return sql
        return sql.replace(SCHEMA_PLACEHOLDER, _quote_literal(self._schema))

    def _guard_for(self, guarded: GuardedStatement) -> tuple[str, tuple[Any, ...]]:
        """The guard's SQL and parameters, with the schema written in if it needs one.

        A guard that reads the catalog of one schema *names* that schema
        in its statement: the catalog spans every schema the user can see,
        so a guard that does not say which schema it means is satisfied by
        another schema's table — and the migration it guards is then
        skipped while the version row is written anyway.

        The schema is written in rather than bound, because a schema is an
        identifier and an identifier cannot be a parameter. It is written
        into the *statement* rather than appended to the parameter list
        because the two can then never disagree: a guard whose ``?``
        placeholders and whose parameters did not line up would match no
        row, and a guard that matches no row skips its step in silence.
        """
        if not guarded.schema_scoped:
            return guarded.guard, guarded.guard_params
        if self._schema is None:
            raise StorageError(
                "a schema-scoped guard cannot run without a target schema",
                substrate=self._substrate,
                **self._error_details(),
            )
        return self._render_comparison(guarded.guard), guarded.guard_params

    def _statements_for(self, guarded: GuardedStatement) -> tuple[str, ...]:
        """The statements of a guarded block, with the schema written in.

        A PostgreSQL statement names the schema it acts on, so that it
        acts on the same object the guard asked about.
        """
        return tuple(self._render(statement) for statement in guarded.statements)
