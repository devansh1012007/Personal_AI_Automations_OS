# Module: Storage (`src/paa/storage/`)

## Module identity

- **Name:** Storage (polyglot substrates)
- **Path:** `src/paa/storage/`
- **Main responsibility:** Pluggable persistence substrates — relational (SQLite/Postgres), cold lake (CAS + signals), vector, graph, queue — all degrading when optional extras are absent.
- **Architectural layer:** Data
- **Entrypoints:** `Database.connect()`, `LedgerStore.append()` (via ledger), `get_vector_store()`, `get_graph_store()`, `get_queue()`, `get_content_store()`, `SignalRepository.record()`
- **Public interfaces:** `Database`, `utc_now/to_iso/from_iso/dumps/loads` (`relational/database.py`); `ContentAddressedStore`, `BlobRef` (`coldlake/cas.py`); `SignalRepository`, `Signal` (`coldlake/signals.py`); `VectorStore`, `get_vector_store`, `get_embedder` (`vector/`); `GraphStore`, `get_graph_store` (`graph/`); `MessageQueue`, `DistributedLock`, `get_queue`, `get_lock`, `StreamName`, `BackpressureController` (`queue/`)
- **Other modules that depend on it:** `ledger/store.py`, `ledger/recovery.py`, `runtime.py`, `agents/policy.py`, `agents/context_agents.py`, `memory/*`, `observability/runs.py`, `skills/registry.py`, `marketplace/installer.py`, `api/app.py`

## Purpose and responsibilities

Polyglot with a parity lock: `schema_sqlite.sql` and `schema_postgres.sql` stay
table- and column-identical (types may differ); `tests/storage/test_schema_parity.py`
fails the build on divergence (`AGENTS.md:51`). Every substrate has an embedded
default and a server variant selected by `PAA_STORAGE__backend_*`; factories
degrade, never raise (`AGENTS.md:59`).

## Internal implementation

### `storage/relational/database.py` — Database
Async SQLite access layer: single write connection (`asyncio.Lock`, `BEGIN
IMMEDIATE`), pooled read-only connections (size 4), WAL mode, applies
`schema_sqlite.sql` on `connect()`, `close()` checkpoints WAL (TRUNCATE) so a
file copy is a complete backup (`storage/relational/database.py:224`). Helpers `fetch_all/fetch_one/
fetch_value/execute/execute_many`, `utc_now/to_iso/from_iso`, `dumps/loads`.

### Schemas (26 business tables + `schema_migrations`)
`system_state_ledger`, `system_state_correlation_head`, `system_state_snapshots`,
`cold_lake_signals`, `cold_lake_artifacts_archive`, `hot_serving_entity_index`,
`hot_serving_entity_fts` (FTS5 SQLite / pg_trgm Postgres),
`hot_serving_active_facts`, `hot_serving_unresolved_buffer`,
`hot_serving_relationships`, `hot_serving_sessions`,
`hot_serving_task_workspaces_vault`, `hot_serving_skill_registry`,
`hot_serving_agent_registry`, `hot_serving_execution_runs`,
`hot_serving_evaluation_results`, `hot_serving_recovery_marks`,
`hot_serving_memory_registry`, `hot_serving_policy_rules`, `queue_messages`,
`queue_locks`, `marketplace_packages`, `marketplace_trusted_keys`,
`improvement_reflections`, `improvement_skill_weights`. Full column reference:
`data/data-models-and-relationships.md`.

### `coldlake/cas.py` — ContentAddressedStore
Immutable zstd-compressed blobs addressed by SHA-256; hash-verified on read;
atomic staging→rename; `put_stream` never materialises full payload.

### `coldlake/signals.py` — SignalRepository
Idempotent on `(channel, external_id)`; oversized payloads offloaded to CAS;
`claim_unprocessed` atomically claims + flips to `processing`.

### `vector/` — Qdrant embedded/server or numpy brute-force fallback.
### `graph/` — Kuzu or SQLite recursive-CTE fallback.
### `queue/` — SQLite or Redis durable queue; `BackpressureController` NORMAL/
DEGRADED/SHEDDING; 11 `StreamName`s; `ORCHESTRATOR_CORE` is control plane.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `Database.connect()` | path | — | creates schema, opens connections |
| `LedgerStore.append(event)` | `LedgerEvent` | sealed event | writes `system_state_ledger` + head |
| `SignalRepository.record(channel, payload, external_id?)` | signal | `Signal` | writes `cold_lake_signals` (+ CAS blob) |
| `ContentAddressedStore.put(data)` | bytes | `BlobRef` | writes blob file |
| `get_vector_store(settings)` | settings | `VectorStore` | opens store on first op |
| `get_queue(settings, db)` | settings + db | `MessageQueue` | shares ledger DB |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `LedgerStore` → `Database` | SQL INSERT/SELECT | Ledger persistence | `ledger/store.py:30` |
| `RecoveryEngine` → `Database` | recovery marks SQL | Workspace checkpoints | `recovery.py:37` |
| `PolicyRiskAgent` → `Database` | policy rules SQL | Operator rules | `policy.py:255` |
| `Context builders` → `Database` | facts SQL | Context candidates | `context_agents.py:141` |
| `Memory/*` → `Database` | entities/facts SQL | Memory persistence | `memory/facts.py:342` |
| `SignalRepository` → `Database` + `CAS` | signal row + blob | Signal intake | `storage/coldlake/signals.py:114` |
| `Observability runs` → `Database` | run rows | Execution traces | `runs.py` |
| `Skills registry` → `Database` | skill rows | Skill catalog | `skills/registry.py:95` |
| `Marketplace installer` → `Database` | package rows | Package registry | `marketplace/installer.py:75` |

## Runtime behavior

- Optional backends constructed in `Runtime._try_vector/_try_graph/_try_queue`
  — catch `Exception`, return `None`/fallback, log warning (`runtime.py:398`).
- `_acquire_sandbox` 5s probe → subprocess fallback (`runtime.py:431`).
- `Database` is single-writer; WAL allows concurrent readers.

## Failure behavior

- Missing optional backend in `auto` mode → fallback, never raises
  (`AGENTS.md:59`).
- `postgres_dsn` missing with `backend_relational=postgres` → `StorageError`
  (`relational/__init__.py`).
- `queue_messages` SQLite backend requires connected `Database`
  (`StorageError` if `db is None`).
- Hash mismatch on CAS read → `StorageError` (`storage/coldlake/cas.py:396`).

## Modification impact

- Adding a column: add to both `schema_sqlite.sql` and `schema_postgres.sql`
  (`AGENTS.md:54`); parity test enforces.
- Changing `StreamName` set affects orchestrator/daemon/recovery/API
  (`storage/queue/base.py:48`).
- Tests: `tests/storage/` (255 + 5 skipped per README).

## Known gaps

- `hot_serving_memory_registry` / `hot_serving_agent_registry` defined in
  schema but consumption lightly verified.
- Docker/Postgres/S3 live-server run still needs a pass (README build-status).

## Related documents

- [`data/data-models-and-relationships.md`](../data/data-models-and-relationships.md) — full schema reference.
- [`data/persistence-and-storage.md`](../data/persistence-and-storage.md) — substrates + degradation.
- [`modules/ledger.md`](ledger.md) — the ledger spine.
- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_STORAGE__*` settings.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — degradation diagram.
