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
`effective_confidence(initial, idle_days, domain)` = `C₀·e⁻ˡᵃᵐᵇᵈᵃ·t`
(`domains.py:120`). `DecaySweeper.sweep()` evicts facts below `prune_floor`
using keyset pagination (`WHERE id > ?` — correct under deletion; OFFSET would
skip rows, a documented fix). Doomed facts archived to cold lake then deleted.
Unknown domain → logged warning, row left untouched (a typo cannot silently
destroy memory).

### `facts.py` — FactRepository
`upsert_entity` (idempotent on canonical_name; aliases unioned; importance max)
(`:199`). `add_fact` appends a new version, never overwrites; `supersede`
links versions (refuses cycles) (`:429`, `:500`). `supersession_chain` walks
back with visited-set guard (`:532`). `query_facts` filters decayed confidence
in Python (derived, can't push to SQL), bounded `max_scan=10_000`
(`:554`). `resolve_entity`: exact → alias → fuzzy FTS5 (`:299`).

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
| `Context builders` → `FactRepository` | `query_facts` | Read context | `context_agents.py:147` |
| `PolicyRiskAgent` → anti-goals (vault) | read `anti_goals.md` | Semantic block | `runtime.py:463`, `policy.py:262` |

## Runtime behavior

- Decay sweep runs on the daemon's 6h maintenance interval (`daemon.py:151`).
- Confidence computed lazily at read time (not stored) — a two-year-old fact is
  not treated as freshly verified (`context_agents.py:162`).
- Facts are immutable: supersede links versions; chain walkable backwards.

## Failure behavior

- FTS5 malformed MATCH → `[]` (failing closed) (`facts.py`).
- Unknown memory domain → `KeyError`; never silently defaults to decaying
  (`domains.py:250`, `facts.py::_to_fact`).
- Decay archive failure → logged at error, doesn't block eviction.

## Modification impact

- Adding a domain = add to `DOMAINS` dict (`domains.py:137`) — data change.
- Changing `decay_lambda` values affects eviction; source of truth is
  `domains.py` (not `MemorySettings.decay_lambda` — potential drift).
- Tests: `tests/memory/` (37 tests decay/domains/contradiction; 76
  creator/curator/world-model per README).

## Known gaps

- `MemorySettings.prune_confidence_floor` (0.15) appears unused in `src/`
  (decay/curator use domain `prune_floor`).
- `MemorySettings.decay_lambda` duplicates `domains.py` but isn't read by them
  (config/drift question).

## Related documents

- [`modules/context.md`](context.md) — context consumption of memory.
- [`data/data-lifecycle.md`](../data/data-lifecycle.md) — fact lifecycle.
- [`data/data-models-and-relationships.md`](../data/data-models-and-relationships.md) — `hot_serving_*` schema.
- [`runtime/background-jobs-and-events.md`](../runtime/background-jobs-and-events.md) — decay sweep daemon loop.
- [`docs/adr/0014` memory domains note](../../README.md) — 18 domains over 6 mechanisms.
