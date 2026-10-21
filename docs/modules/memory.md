# Module: Memory (`src/paa/memory/`)

## Module identity

- **Name:** Memory
- **Path:** `src/paa/memory/`
- **Main responsibility:** 18 memory domains over 6 real mechanisms; decay, contradiction detection, fact/entity persistence, creation from signals, curation, and vault-based world model.
- **Architectural layer:** Data / control
- **Entrypoints:** `FactRepository.add_fact/upsert_entity`, `MemoryCreator.run_batch()`, `DecaySweeper.sweep()`, `WorldModel.update_belief_state()`
- **Public interfaces:** `MemoryDomain`, `MemoryMechanism`, `DomainPolicy`, `DOMAINS`, `narrative_domains()` (`domains.py`); `DecaySweeper`, `effective_confidence()` (`decay.py`); `FactRepository`, `Fact`, `Entity` (`facts.py`); `MemoryCreator` (`creator.py`); `MemoryCurator` (`curator.py`); `WorldModel` (`world_model.py`)
- **Other modules that depend on it:** `agents/context_agents.py` (reads facts), `agents/policy.py` (anti-goals), `daemon.py` (creator + decay), `storage/` (Database)

## Purpose and responsibilities

RFC §4 memory: facts are never overwritten — new information supersedes old and
the old row stays addressable (`facts.py` docstring). Confidence is derived
(`C(t)=C₀·e⁻ˡᵃᵐᵇᵈᵃ·t`), never selected (`facts.py`). 18 RFC domains are
retained as vocabulary but map onto 6 mechanisms (ADR-0014); adding a domain is
a data change, not a new subsystem.

## Internal implementation

### `domains.py` — 18 domains, 6 mechanisms
`MemoryDomain` (18 members: working, long_term_distilled, episodic, semantic,
procedural, task, session, temporal, narrative, reflection, strategic, skill,
tool, relationship, identity, operational, environmental, workspace — `:78`).
`MemoryMechanism`: VOLATILE, RELATIONAL, IMMUTABLE_LOG, VECTOR, GRAPH, DOCUMENT
(`:43`). `DomainPolicy`: decay_lambda, prune_floor, immutable, narrative
(`:98`). `DOMAINS` matrix (`:137`); `domain_policy()` lookup (`:250`);
`narrative_domains()` = {narrative, reflection, strategic, identity} (`:260`).

### `decay.py` — DecaySweeper
`effective_confidence(initial_confidence, last_queried_at, domain)` =
`C₀·e⁻ˡᵃᵐᵇᵈᵃ·t` (`decay.py:57`). `DecaySweeper.sweep()` evicts facts below
`prune_floor` using keyset pagination (`WHERE id > ?` — correct under
deletion; OFFSET would skip rows, a documented fix). Doomed facts archived
to cold lake then deleted. Unknown domain → logged warning, row left
untouched (a typo cannot silently destroy memory).

### `facts.py` — FactRepository
`upsert_entity` (idempotent on canonical_name; aliases unioned; importance max)
(`:337`). `add_fact` appends a new version, never overwrites; `supersede`
links versions (refuses cycles) (`:779`, `:850`). `supersession_chain` walks
back with visited-set guard (`:882`). `query_facts` filters decayed confidence
in Python (derived, can't push to SQL), bounded `max_scan=10_000`
(`:904`).

`resolve_entity` (`:456`) is a three-rung ladder — exact → alias → fuzzy —
and the **gate is the authority**: a Python character-trigram Jaccard
similarity (`trigram_similarity`, `:150`) decides, and the database only
decides *which rows the gate gets to look at*. The gate's gram model is
FTS5's — the 3-grams of the raw, case-folded text (`:203`), no padding and
no whitespace-run collapsing — so a pair the gate accepts is a pair the
index can hold.

The candidate set comes from the dialect seam
(`storage/relational/dialect.py::entity_candidates_sql`), which asks one of
two questions: a **phrase** query ("is the term a substring of the row's
text?") and a **broad** query ("does the row share at least one of the
term's grams?"). The broad query asks the gate's own precondition, so for
**ASCII corpora** the candidate set is a superset of the gate's acceptance
set; the phrase query is a precision pass, and the broad one is the fallback
when the phrase rows do not clear the floor. The rows are ordered by a
**portable total order** — the count proxy `DESC`, then the unique `e.id
ASC` — so the `LIMIT` cut is the same cut on both substrates and the gate's
tie-break is deterministic rather than a function of the heap layout. The two
substrates agree on the answer — id, match kind and score — for ASCII
corpora; outside ASCII the parity is **bounded**, and the three mechanisms
that bound it are named in the Known gaps below and in ADR-0029.

A term carrying a NUL is refused before any statement is issued, on both
substrates (`:482`); so is a term with more than `_MAX_TERM_GRAMS` grams
(`:135`, `:508`), because the count proxy's `CASE` sum would otherwise hit
SQLite's expression-depth limit and be refused as "expression tree is too
large" — which is not a `QueryRejected` and would escape the fail-closed
ladder. Recorded in
[**ADR-0029**](../adr/0029-entity-resolution-parity.md); the case fold lives
in the seam (`dialect.py::fold_case`, `:121`) and the seam returns the
candidate query together with the parameters it takes, which amends
ADR-0025.

### `creator.py` — MemoryCreator
Ingests unprocessed signals into hot-serving facts/entities. Called by daemon
ingestion loop (`daemon.py:167`).

### `curator.py` — MemoryCurator
Nightly consolidation (decay sweep interval 6h, `config.py:202`).

### `contradiction.py`
Conflicts above `contradiction_threshold` (0.75) quarantine both records at
`contradiction_degraded_confidence` (0.20), demand human tie-break; never
auto-resolved (`errors.py:279`).

### `world_model.py` — WorldModel
Marker-fenced vault documents (`current_foods.md`… actually `current_focus.md`,
`strategic_risks.md`, `active_constraints.md`, `operating_themes.md`).
Rewrites only the managed `<!-- paa:managed:BEGIN/END -->` section, preserving
human text; atomic writes (temp + `os.replace`) (`world_model.py:95`).

`BeliefDocument` is a `str`-mixin enum, so its inherited string API is part of
its contract: `doc.title` is `str.title` (a *method*) and must stay callable.
The document's human-readable name is therefore `doc.display_title`
("Current Focus"), not `doc.title` — a property named `title` would shadow
`str.title` and turn `doc.title()` into `TypeError: 'str' object is not
callable`.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `FactRepository.add_fact(...)` | entity_id, predicate, value, domain, confidence | fact id | writes `hot_serving_active_facts` |
| `FactRepository.upsert_entity(...)` | class, name, aliases | entity id | writes `hot_serving_entity_index` |
| `DecaySweeper.sweep()` | — | report | deletes/archives stale facts |
| `MemoryCreator.run_batch(limit)` | limit | report | claims signals, writes facts, marks signals |
| `WorldModel.update_belief_state(doc, content, section)` | document content | — | writes vault markdown file |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `FactRepository` → `Database` | SQL INSERT/SELECT | Persist entities/facts | `facts.py` |
| `DecaySweeper` → `Database`, `domains` | decay policy + SQL DELETE | Evict stale facts | `decay.py` |
| `MemoryCreator` → `SignalRepository`, `FactRepository` | claim signals → facts | Ingest signals | `creator.py`, `daemon.py:167` |
| `Context builders` → `Database` | hand-rolled `LIKE` over the fact tables | Read context | `context_agents.py:189` |
| `PolicyRiskAgent` → anti-goals (vault) | read `anti_goals.md` | Semantic block | `runtime.py:463`, `policy.py:262` |

## Runtime behavior

- Decay sweep runs on the daemon's 6h maintenance interval (`daemon.py:151`).
- Confidence computed lazily at read time (not stored) — a two-year-old fact is
  not treated as freshly verified (`context_agents.py:162`).
- Facts are immutable: supersede links versions; chain walkable backwards.

## Failure behavior

- A candidate query the engine refuses → `[]` (failing closed), so the fuzzy
  rung answers "no match" rather than taking the caller with it
  (`facts.py::_entity_candidates`). The FTS5 quoting is an injection
  barrier, so a term carrying `"`, `*`, `AND`, `OR`, `NEAR`, `(` or `)` is
  *answered* as text — it matches nothing and raises nothing.
- A term carrying a NUL → no match, and **no statement is issued at all**
  (`facts.py:482`); the two substrates refuse a NUL in different places, so
  the refusal has to be in one place.
- Unknown memory domain → `KeyError`; never silently defaults to decaying
  (`domains.py:250`, `facts.py::_to_fact`).
- Decay archive failure → logged at error, doesn't block eviction.

## Modification impact

- Adding a domain = add to `DOMAINS` dict (`domains.py:137`) — data change.
- Changing `decay_lambda` values affects eviction; source of truth is
  `domains.py` (not `MemorySettings.decay_lambda` — potential drift).
- Tests: `tests/memory/` (37 tests decay/domains/contradiction; 76
  creator/curator/world-model per README). Entity resolution has its own
  three suites: `tests/memory/test_entity_resolution.py` (the candidate set,
  the gate, the total order — embedded substrate only),
  `tests/storage/test_dialect.py` (the seam's statement, parameters and
  order — no database needed), and
  `tests/storage/test_entity_parity.py` (the same corpus through both
  substrates — `requires_postgres`, skipped without `PAA_TEST_POSTGRES_DSN`).

## Known gaps

- `MemorySettings.prune_confidence_floor` (0.15) appears unused in `src/`
  (decay/curator use domain `prune_floor`).
- `MemorySettings.decay_lambda` duplicates `domains.py` but isn't read by them
  (config/drift question).
- Entity resolution's parity is exact for ASCII and **bounded** outside it.
  The candidate query folds the *column* with the engine's `lower()`
  (ASCII-only on SQLite, locale-aware on PostgreSQL) while the term is
  folded in Python; the alias column is JSON text, so `json.dumps` escapes a
  non-ASCII alias and the count proxy cannot see it; and the exact and alias
  rungs still compare with the engine's `lower()`. All three need a stored
  Python-computed normalisation to close, which is a schema change in both
  DDL files plus a Python-executed backfill. Recorded in
  [**ADR-0029**](../adr/0029-entity-resolution-parity.md).
- The non-ASCII divergence is **one-directional**: PostgreSQL's locale-aware
  `lower()` sees rows the ASCII-only fold cannot, so the two substrates can
  answer differently — and for the same entity they can report a different
  *match kind* (measured: `"İstanbul"` is an `exact` answer at 1.0 on
  PostgreSQL and a `fuzzy` guess at 0.625 on SQLite). The parity suite
  measures this rather than assuming it
  (`tests/storage/test_entity_parity.py::TestTheRecordedDivergence`).

## Related documents

- [`modules/context.md`](context.md) — context consumption of memory.
- [`data/data-lifecycle.md`](../data/data-lifecycle.md) — fact lifecycle.
- [`data/data-models-and-relationships.md`](../data/data-models-and-relationships.md) — `hot_serving_*` schema.
- [`runtime/background-jobs-and-events.md`](../runtime/background-jobs-and-events.md) — decay sweep daemon loop.
- [`docs/adr/0014` memory domains note](../../README.md) — 18 domains over 6 mechanisms.
- [`docs/adr/0029`](../adr/0029-entity-resolution-parity.md) — entity resolution is the same answer on both substrates.
