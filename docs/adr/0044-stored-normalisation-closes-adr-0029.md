# ADR-0044: Stored normalisation closes ADR-0029

## Status

Accepted. Amends [ADR-0029](0029-entity-resolution-parity.md): limitations
(a), (c) and (d) are closed by this change, and (b) remains recorded.

## Context

ADR-0029 left three limitations open because the column was folded by the
engine and the term by Python, the alias blob escaped non-ASCII text, and
the exact and alias rungs still compared with the engine's lower(). The
stored-normalisation closure (PLAN:integration-fix:P003, migration v6)
replaces the engine fold with a stored Python-computed fold for the entity
table, so both substrates read the same folded text and the same folded
term. [VERIFIED src/paa/storage/relational/schema_sqlite.sql] The two
nullable columns are `canonical_name_norm` and `aliases_norm` on
`hot_serving_entity_index` in both DDL files.

## Decision

**The entity table stores its own fold.** `upsert_entity` writes
`canonical_name_norm = fold_case(canonical_name)` and `aliases_norm` as
folded JSON with `ensure_ascii=False` in the same statement as the row, via
`_aliases_norm_value` (`src/paa/memory/facts.py:247`). The exact rung reads
`canonical_name_norm = fold_case(term)` (`src/paa/memory/facts.py:594`), the
alias rung expands `aliases_norm` (`src/paa/memory/facts.py:662`), and the
dialect seam counts over `e.canonical_name_norm` and `e.aliases_norm`
(`src/paa/storage/relational/dialect.py:388` and
`src/paa/storage/relational/dialect.py:459`). FTS5 sync indexes the
normalised text.

**Migration v6 backfills in Python.** A Python-executed loop reads the
NULL-norm rows, folds in Python, and UPDATEs; never an SQL lower(). The
scan is idempotent, and corrupt alias blobs backfill to `[]` while the raw
blob stays escaped under the existing `dumps()` behaviour.

**The graph backend is a second writer and honours the same fold.** The
SQLite graph upsert writes `canonical_name_norm` from `fold_case` and
`aliases_norm` as `[]`, and its DO UPDATE refreshes the canonical norm
without erasing facts-written aliases.

## Alternatives rejected

* **Collation or `COLLATE` per engine.** Rejected: it keeps the fold in
  the engine, so the U+0130 three-way split survives.
* **SQL lower() backfill.** Rejected: SQLite lower() folds ASCII only,
  which re-introduces the divergence the stored fold exists to remove.
* **Space-joined aliases text.** Rejected: it cannot be expanded per alias
  for the alias rung, so folded JSON is kept.

## Consequences

**What it closes.** ADR-0029 limitations (a), (c) and (d) are closed:
the column and the term now share one fold, the alias gate no longer
depends on the blob escape, and the exact and alias rungs read the same
key set on both substrates. Limitation (b) (U+0130) converges to identical
answers on both substrates (fuzzy 0.625 for İstanbul, no match for İZMİR
with the row visible to both broad queries).

**What it costs.** Two columns in both DDL files, a Python backfill, and
per-row UPDATEs that rebuild FTS row by row. The PG half is gated behind a
live server.

**What remains open.** The three other LIKE sites — `search_facts_by_text`,
the gatherer, and skills — still fold the raw column in the engine and
need stored-normalised columns of their own, which is a second schema
change.

**How to roll it back.** Revert the write path and the v6 backfill reads the
raw columns again; the norm columns are ignored one-way and the raw blob
and row data are untouched.
