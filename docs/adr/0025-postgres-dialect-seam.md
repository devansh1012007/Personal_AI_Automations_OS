# ADR-0025: The relational substrates are interchangeable only through a dialect seam

## Status

Accepted

## Context

The RFC specified PostgreSQL as the relational substrate. ADR-0001 replaced it with
embedded SQLite, because the target laptop had no Docker and ~3.5 GB of RAM. ADR-0019
then restored the server for the Docker deployment, so the repository now ships two
relational substrates and one set of repositories that has to run on both.

The assumption behind that arrangement was that keeping the two DDL files
column-for-column identical made the substrates interchangeable. It does not, and the
gap was not hypothetical — it was already in the tree:

- **The composition root hardcoded the SQLite class.** `PostgresDatabase` was never
  type-checked against anything, never called by the runtime, and had drifted: its
  `transaction()` yielded a different kind of object, a column's value arrived in a
  different Python type, and a failure arrived as a different exception class.
- **A value's type depended on the substrate.** A timestamp column is `TEXT` on SQLite
  and `TIMESTAMPTZ` on PostgreSQL, so the same row handed back a `str` on one and a
  `datetime` on the other. An id column is `TEXT` on one and `UUID` on the other, so the
  same row handed back a `str` and a `uuid.UUID` — and a `uuid.UUID` is not a `str`, so
  a model field declared `str` silently received something else, and a pydantic model
  declared `str` raised `ValidationError`.
- **A failure's class depended on the substrate.** `sqlite3.IntegrityError` and
  `psycopg.errors.UniqueViolation` are different types with different hierarchies. A
  repository that caught one of them had to import the driver — and
  `src/paa/ledger/store.py` is imported on every laptop install, where psycopg is not
  installed at all.
- **The DDL was create-from-scratch.** Every statement in `schema_sqlite.sql` and
  `schema_postgres.sql` is `CREATE ... IF NOT EXISTS`. That is a perfect description of
  a brand-new database and a useless upgrade path: `CREATE TABLE IF NOT EXISTS` does
  not add a column to a table that already exists. `SCHEMA_VERSION` existed and was
  compared at connect time, but the only thing that could happen when the numbers
  differed was "apply the whole DDL file" — which, on an existing database, changed
  nothing. A schema change therefore only ever reached a database that did not yet
  exist. This repository had never attempted one; the first attempt (the `arrival_seq`
  ordering columns, and JSON documents stored as text) found the gap.
- **One engine's JSON column silently corrupted the other.** `JSONB` re-serialises its
  value: the key order is normalised, the numeric literals are re-rendered, and the
  whitespace is the server's. The ledger hashes its payload and re-verifies it on read,
  so a round trip through `JSONB` would make `verify_chain` report false tampering —
  for every event whose payload does not re-serialise to the same bytes, which is
  every payload carrying a number the server renders differently (`1e16` becomes
  `10000000000000000`) or a control character the server escapes. The cold lake scans
  `raw_payload`'s raw bytes for hostile control characters, and `JSONB` would have
  re-serialised them out of sight. Both columns are document-shaped, and both need the
  bytes a writer produced rather than an interpretation of them.

## Decision

**The two substrates are interchangeable only through three seams, and nothing else.**

**1. One contract, in `storage/relational/base.py`.** `RelationalBackend` is the set of
operations the repository layer actually performs — not a wish list. A member no
repository calls does not belong there; an earlier draft carried a `vacuum()` that
PostgreSQL could not honour without running outside a transaction, and a `path`
property with no meaning on a server substrate. `Runtime.build()` holds one of them
behind the protocol, so a repository written against it runs on either engine without
knowing which.

**2. One dialect seam, in `storage/relational/dialect.py`.** The repository layer writes
SQL that both engines accept, with a handful of exceptions. Those exceptions live in
the dialect, as small named values, rather than as `if backend == "postgres"` branches
at the call sites — a branch in the repository layer is a branch that has to be
re-tested for every new backend. The seam covers three kinds of difference:

- *SQL fragments* with no portable spelling: `scalar_max` (`MAX(a, b)` is scalar on
  SQLite and an aggregate on PostgreSQL, where the two-argument form is `GREATEST`),
  `alias_values` (`json_each` against `jsonb_array_elements_text`), and
  `entity_candidates_sql` (FTS5's trigram matcher against `pg_trgm`).
- *Transaction semantics*, not spellings: `lock_correlation` takes a per-lineage
  advisory lock on the server, because the ledger assigns `state_version` by reading
  the current tip and writing `tip + 1`, and that read-then-write is only atomic if
  two writers for one lineage cannot interleave.
- *Value coercion*, which lives with the column rather than with the statement — a
  repository reads a column, it does not execute a query about it.

**3. One error vocabulary, in `storage/relational/errors.py`.** The translation happens
at the substrate boundary, once, so the repository layer catches a *concept* rather
than a driver's exception. The vocabulary is built to distinguish the kinds of
constraint violation, because the ledger has two unique constraints that mean
different things: a duplicate idempotency key is a suppressed retry, and a duplicate
`state_version` is a concurrency collision. Collapsing both into one exception is how a
benign retry becomes a hard failure. (The vocabulary has the kinds; telling them
apart needs the *constraint* that was violated rather than the SQLSTATE that reported
it, and that is a separate change.)

**The vocabulary has one member that is the data's fault, and it says so.**
`QueryRejected` — "the statement was refused; degrade gracefully" — is for a problem
with the *query*. A row whose JSON is corrupt is a problem with the *data*, and it
gets its own class (`MalformedDocument`). But it is a *subclass* of the rejected-query
class, so the repository layer's one fail-closed handler still catches it: the
ingestion batch that asked the question must not go down because one row in the table
is unreadable. A subclass rather than a sibling, because a sibling would have needed a
second handler in every fail-closed path, and a second handler is a second place to
forget one. (`ParameterCountMismatch` is a subclass for a different reason: a
placeholder count that disagrees with the parameter count is our SQL's bug, and it is
the one class of bug the placeholder translator can introduce.)

**The boundary translates the driver's errors and nothing else.** The transaction
boundary is the one place where the boundary is wider than a single statement — the
block it wraps contains the repository's own code — so the exception that comes out
may be the driver's, one of ours, or a plain programming error in the repository. Only
the first is translated. A `LedgerError` stays a `LedgerError` on the server substrate
as it does on SQLite, and a `TypeError` stays a `TypeError` rather than being
reported as an infrastructure fault. This is not a nicety: the two substrates
reporting the *same* domain failure with *different* exception types is the exact
divergence this ADR exists to remove, and a caller that catches `LedgerError` to
recognise a lost race would catch nothing on the server.

**A term the engine cannot answer resolves to no match.** The refusal is in the
repository layer, not in the driver, because the two engines refuse the same input in
different places: SQLite's full-text matcher rejects the *statement* (which the
repository catches), while psycopg refuses the *bound parameter* before any statement
is sent, with an error that carries no SQLSTATE and therefore no classification a
caller could catch. Refused in one place, the behaviour is the same on both
substrates.

**4. JSON documents are `TEXT` on both engines.** Not `JSONB`, and not because
`JSONB` is worse in general: because the ledger hashes its payload and the cold lake
scans its raw bytes, and both need the bytes rather than an interpretation of them.
The cost is real and is recorded here: the server no longer checks JSON validity for
us. SQLite still does, through `json_valid`. The writers are the check on the server
side, and the migration that converts the columns restates each default so a migrated
database and a fresh one are identical down to the catalog.

**The upgrade stops new events from being corrupted; it does not repair the old
ones.** A payload that was written while the column was `JSONB` was already
re-serialised on the way in — the bytes the server normalised are the bytes that were
stored, and the text column preserves them faithfully. An upgraded ledger therefore
still fails `verify_chain` for the events written before the upgrade. The migration
makes the ledger *verifiable from here on*; it does not make it verifiable.

**5. Schema changes go through a versioned migration list, in
`storage/relational/migrations.py`.** The `.sql` files stay what they were — the
description of a brand-new database — and the *delta* from one version to the next
lives in a list of guarded steps. A step is guarded because a migration must be safe to
re-run: a *guarded block* carries one guard, and the guard says whether the block's
**effect** is still outstanding. The block runs only when it is. A crash part-way
through a block leaves the block unapplied, the next start re-runs it, and the re-run
does the whole block again.

That is deliberately *not* a per-statement guard. A guard on an intermediate state —
"does the column exist?" — is satisfied by a block that has added the column but not
yet backfilled it, and the rest of the block never runs, so the column exists and is
empty forever. The guard is therefore on the finished effect: the column exists, is
not null, and is generated. Several statements may share one guard when they are steps
towards one effect, and then a re-run redoes all of them; that is the correct trade,
because the alternative is a step that can half-land and never finish.

The version row is written last, so the version is never ahead of the shape it
describes. The version log is also *checked*, not just written: a gap in it, or a
database whose log claims a shape the database does not have, is refused rather
than believed, because the highest version in the log is the only thing the build
would otherwise use to decide what to run.

**The DDL is atomic; the version rows follow it.** They are *not* one
transaction, and this record used to read as if they were. The real invariant is
narrower, and the gap it leaves is deliberate:

* the schema file is applied in a transaction of its own — on SQLite
  `executescript("BEGIN IMMEDIATE; … COMMIT;")`, on PostgreSQL one statement
  inside one transaction — so a failure half-way through leaves **none** of the
  schema behind, and the database is exactly as it was;
* the version rows are written in a **following** transaction. A version row
  is a claim about a shape, so it must never be written *ahead* of the shape
  it describes — and the log the rows go into is itself created by the
  schema, so the rows cannot be written before it either. (This is the
  *initial* application; a migration's own version row is written in the
  same transaction as its steps — see below.)

A crash in the window between the two leaves a database that has all the tables
and no version rows. That is not a mystery state: it is the *"tables present, log
empty"* case, which the runner treats as a database at the base version and
migrates forward. What makes that safe is two properties the rest of the design
already has. The runner asks the **catalog** rather than the log, so what it finds
in the window is a shape it can reason about rather than an absence it has to
interpret. And every guard tests the *effect* of its step rather than a counter,
so re-running a step that has already landed changes nothing, and the version
rows that were never written are written then — the migration converges on the
same shape it would have reached without the crash.

For a *migration* the gap does not exist at all: a version's steps and its
version row are written in the **same** transaction, so a step that fails leaves
the database at the version it was at, with no row claiming otherwise. The window
is only ever between the *initial* application of the schema and the first
version row.

**The schema is rendered into the guard, not bound into it.** The guards that
read one schema's catalog — every `information_schema` guard on PostgreSQL —
carry the schema as a **rendered literal**, `AND table_schema = '<schema>'`,
written in by the runner through the `SCHEMA_PLACEHOLDER` token. They do **not**
carry it as a bound `?` parameter, and there are two reasons, the first of which
decides it:

* a schema is an **identifier**, and an identifier cannot be a parameter. The one
  place the schema appears as a *value* rather than as a name — the comparison
  against the catalog's `table_schema` column — is therefore written in as a
  quoted literal, because `"paa"` there would name a *column* called `paa`, and
  the comparison would fail with "column does not exist" rather than match.
* a parameter's position is a promise. A guard whose own parameters come first
  and whose schema comes last is a silent trap: a mis-ordered guard matches no
  row, so the step is skipped, and the version row is written anyway — a
  database claiming a shape it does not have, which is exactly what this
  machinery exists to prevent. Writing the schema into the *statement* removes
  the ordering rule altogether: the placeholder count and the parameter list
  cannot drift out of step, because the schema is not on that side of the
  ledger at all.

The version log is read and written through a **name-qualified** identifier —
`"paa".schema_migrations` on PostgreSQL — and the qualification is rendered by
the same mechanism. (The log's own `CREATE TABLE` is the schema file's own
statement, unqualified, and lands in the schema the runner resolved on the
connection it applies the file on.) The per-engine driver files never query the
log at all: the log is the runner's own bookkeeping, the runner is shared, and
so the log's spelling is the runner's business rather than the driver's.

A database with the tables but *no* log is not refused — it is **migrated**. That is
the case of a database created before the version log existed, and it is a real
database with a real shape; refusing it would strand every deployment that predates
the mechanism. The distinction is drawn from the *catalog* rather than from the log:
all the tables the schema declares means the database is at the base version, and
some of them means the schema was applied only part of the way, which is refused by
name.

Each step carries both engines' halves, because the DDL that carries a schema from one
version to the next is engine-specific by nature. What stays shared is the *decision*:
which versions are missing, which statements are guarded, and what a version means.

**6. A database newer than the build is refused.** Not opened, not written to: an older
build writing to a newer schema is how a ledger is corrupted, because the build does
not know which columns the newer version added and will write a row the newer version
cannot read. The refusal names both versions.

**7. The seam's members are differences that *have* to be somewhere.** Each
one is a value a backend cannot spell portably, and each is asserted in a test that
needs no database. Two of them are not SQL at all — the value coercions — and those
live in `paa.core.types` instead, because they are *value* coercions and both
substrates' rows go through the same helper so they produce the same value.
`arrival_seq` is the column that records arrival order, and it is a seam member
because the column's *name* is a constant the two engines spell the same way but
which a repository has to name without knowing which engine it is talking to: the
alternative is a literal at each call site, and a literal at a call site is a
place the two engines can drift.

**`Dialect.as_uuid` was never a member of the dialect protocol, and nothing was
removed from it.** The protocol was introduced with the `as_uuid` **function** in
`paa.core.types` — the coercion that turns a row's id into a `uuid.UUID` whichever
substrate the row came from — and the protocol has never had an `as_uuid` member.
A record that says the protocol "lost" the member, or that the member "was removed
from the dialect", is describing a removal that never happened: the coercion was
never in the seam to begin with, and the seam's own documentation says so. The
distinction matters because the two are different failures: a coercion that *was* in
the seam and was moved out is a change to the seam, and one that was never in it is
not.

**8. The two DDL files stay table- and column-identical.**
`tests/storage/test_schema_parity.py` parses both with a small DDL lexer and fails the
build on any divergence. Only *types* differ, and each difference is a deliberate use
of a native PostgreSQL type the SQLite dialect had to emulate.

## Consequences

**What this costs.**

- The dialect seam is a second place to look for engine behaviour, and it is a place
  that can rot: a divergence that is not in the seam shows up as a repository that
  works on one engine and not the other. The seam is kept small on purpose — every
  member is a difference that *has* to be somewhere — and each is a named, testable
  value.
- Two engines means two things to keep in step, and the DDL files are the worst of it:
  a duplicated 600-line artefact whose divergence fails a test rather than being
  impossible. The migration list has the same property, and the same mitigation: a
  database created fresh and a database migrated forward are compared — table by
  table, column by column and in order, index by index, foreign key by foreign key,
  check constraint by check constraint, and trigger by trigger — in
  `tests/storage/test_migrations.py`. The trigger and the DDL file's copy of it are
  duplicated for the same reason, and the same test is what keeps them from drifting.
- The JSON-as-text decision gives up the server's JSON validity check, and gives up
  `JSONB`'s operators. The one place that needs to expand a JSON array
  (`alias_values`) casts the text back to `jsonb` before expanding it.
- The migration machinery is a new thing to maintain, and it exists because the
  alternative — a marker with no machinery — is worse.

**What it would take to move back to the RFC's design.** One relational substrate. The
seam would collapse to a handful of `if` statements and the migration list to a single
`SCHEMA_VERSION` constant, and the server topology would go with it.

**What is still open.** The two engines' *answers* are not yet identical everywhere.
Entity resolution uses FTS5's trigram matcher on one engine and `pg_trgm` on the
other, and the candidate sets the two produce are not the same set — the dialect seam
covers the SQL, not the semantics, and the semantics are the subject of a separate
change. (That change is W3 in the substrate track in `docs/MCP_AND_PLUGINS.md`: the
decision is taken — the database must return a candidate set that is a *proven
superset* of the set the Python acceptance gate would accept, and the gate then
decides in Python, identically on both engines — and the implementation has not
started.)

The representation of an id is the other half, and it is closed: the coercion lives
in `paa.core.types` and is applied at the read sites, so a repository never has to
know which substrate produced the row. The trace id is coerced at the *write* site
as well, so a row carries the canonical hyphenated form whichever substrate wrote
it, and the migration rewrites the rows that were written before that.

**The suites that would have caught this are the point.** The whole change set was
invisible to the suite, because the suite ran one substrate. The live-server
contract suite (`tests/storage/test_postgres_contract.py`, gated on
`PAA_TEST_POSTGRES_DSN`) is what makes the *contract* — not the mechanics —
testable on both substrates, and `tests/storage/test_postgres_migrations.py` is
what makes the migration machinery testable at all.
