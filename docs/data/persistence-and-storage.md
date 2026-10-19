# Persistence and storage

PAA is a **polyglot storage substrate** with a parity lock: SQLite (default,
mandatory) is the relational spine; Qdrant/Kuzu/Redis/MinIO are optional and
degrade to in-process fallbacks. Every substrate has an embedded default and a
server variant selected by `PAA_STORAGE__backend_*` settings. Heavy dependencies
are **never imported at module scope in a core path** (`AGENTS.md:59`).

## Substrates at a glance

| Substrate | Default | Optional server | Fallback when absent | Factory |
|---|---|---|---|---|
| Relational (ledger + hot serving) | SQLite WAL | PostgreSQL | — (SQLite is the only alternative) | `storage/relational/__init__.py:get_database` |
| Vector (fact recall) | Qdrant embedded if available | Qdrant server | Numpy brute-force | `storage/vector/__init__.py:get_vector_store` |
| Graph (provenance) | Kuzu if available | — | SQLite recursive CTE | `storage/graph/__init__.py:get_graph_store` |
| Queue (durable messages) | SQLite | Redis | — (SQLite is the only alternative) | `storage/queue/__init__.py:get_queue` |
| Cold lake (immutable blobs) | Filesystem CAS | MinIO/S3 | — (filesystem is the only alternative) | `storage/coldlake/__init__.py:get_blob_store` |

Settings (`src/paa/config.py:55`):
- `backend_relational` (`sqlite`\|`postgres`, default `sqlite`)
- `backend_vector` (`auto`\|`qdrant_local`\|`qdrant_server`\|`numpy`, default `auto`)
- `backend_graph` (`auto`\|`kuzu`\|`sqlite`, default `sqlite`)
- `backend_queue` (`sqlite`\|`redis`, default `sqlite`)
- `backend_coldlake` (`filesystem`\|`minio`, default `filesystem`)

## 1. Relational — `src/paa/storage/relational/database.py`

**`Database`** is an async SQLite access layer:
- Single write connection guarded by `asyncio.Lock`; `BEGIN IMMEDIATE`
  transactions (`transaction()`, `database.py:214`).
- Pooled read-only connections (size 4) via `_reader()` (`database.py:230`).
- WAL mode; applies `schema_sqlite.sql` idempotently on `connect()`
  (`database.py:107`).
- `close()` runs `PRAGMA wal_checkpoint(TRUNCATE)` so a single `.db` file copy
  is a complete backup (`database.py:132`).
- Helpers: `fetch_all`/`fetch_one`/`fetch_value` (reads), `execute`/`execute_many`
  (writes), `utc_now`/`to_iso`/`from_iso` (tz-aware ISO timestamps), `dumps`/
  `loads` (compact JSON for storage columns).

Configuration: `StorageSettings.sqlite_path` (default `~/.paa/state/paa.db`),
`StorageSettings.sqlite_busy_timeout_ms` (default 10000), `StorageSettings.
postgres_dsn` (used only when `backend_relational == "postgres"`).

Failure: `transaction()`/`_reader()` raise `StorageError` if not connected
(`database.py:214`/`230`). Schema DDL failure raises `StorageError` with
substrate="sqlite" (`database.py`).

**Consumers:** `LedgerStore`, `RecoveryEngine`, cold lake signals/artifacts,
graph SQLite backend, queue SQLite/Redis backends, policy agent, context
agents, memory (facts/creator/decay/curator/contradiction), observability runs,
skills registry, improvement modules, marketplace installer, `Runtime`.

## 2. Cold lake — `src/paa/storage/coldlake/`

### `ContentAddressedStore` (`cas.py:122`)
Immutable, zstd-compressed, deduplicating blob store on the local filesystem.
- Blob path: `blobs/<sha256[:2]>/<sha256[2:4]>/<sha256>.zst`.
- Address = SHA-256 of raw bytes; immutable by design.
- Verifies SHA-256 on every read (`get`/`get_stream`); mismatch → `StorageError`
  ("blob failed hash verification").
- Writes go to `staging/` then `os.replace` (atomic same-volume rename); stray
  `*.part` files cleaned by `clear_staging()`.
- Synchronous by design; callers on the event loop must use `asyncio.to_thread`
  (`cas.py` docstring).

Configuration: `StorageSettings.cold_lake_path` (default `~/.paa/cold_lake`),
`StorageSettings.zstd_level` (default 3, range 1–22).

### `SignalRepository` (`signals.py:99`)
Write-once intake for raw external events:
- Idempotent on `(channel, external_id)`; a pre-existing row returns the
  incumbent via the `duplicate` flag (`api/app.py:179`).
- Oversized payloads (> `DEFAULT_INLINE_THRESHOLD_BYTES = 65536`) offloaded to
  CAS with a `cas://` stub in `raw_payload`.
- `record()` validates UTF-8/JSON before INSERT (`StorageError` on invalid;
  `signals.py:388`).
- `claim_unprocessed(limit)` atomically claims + flips status to `processing`
  (`signals.py:209`).
- Status transitions: `mark_processed`, `mark_malformed`, `mark_quarantined`,
  `release` (back to `unprocessed` only if currently `processing`).

Consumers: API `POST /signals` (`api/app.py:166`), daemon ingestion loop
(`daemon.py:139` → `MemoryCreator`).

## 3. Vector — `src/paa/storage/vector/`

`get_vector_store(settings)` (`vector/__init__.py:95`):
- `"auto"`: Qdrant if `qdrant-client` importable, else `NumpyVectorStore` with
  a warning. Never raises in auto mode.
- Explicit `"qdrant_server"` without `qdrant_url` → `StorageError`.
- Numpy backend: brute-force exact recall, O(n) per query, ~1.5 KB per
  384-dim point, fine to ~100k points.

Embeddings (`models/embeddings.py` → `storage/vector/embeddings.py`):
- `SentenceTransformerEmbedder` (384-dim MiniLM) or `HashEmbedder`
  (deterministic fallback, warns loudly) selected by
  `ModelSettings.allow_hash_embedder_fallback` (default `true`).

Collections (`store.py`): `ACTIVE_FACTS` and `RFC_COLLECTIONS`.

Consumers: `Runtime._try_vector` (`runtime.py:351`), `MemoryCreator` (embeds
new facts, `creator.py:654`), context gatherer semantic search
(`context_agents.py:182`).

## 4. Graph — `src/paa/storage/graph/`

`get_graph_store(settings, db)` (`graph/__init__.py:63`):
- `"auto"`: Kuzu if importable, else `SqliteGraphStore(db)` with a log line.
- Explicit `"kuzu"`: raises `ImportError` if kuzu absent.
- `"sqlite"`: always succeeds (recursive CTE traversal).

Consumers: `Runtime._try_graph` (`runtime.py:364`), context builder graph
neighbours (`context_agents.py:205`, bounded by `graph_hops` from modality
profile).

## 5. Queue — `src/paa/storage/queue/`

`get_queue(settings, db)` / `get_lock(settings, db)` (`queue/__init__.py:58`):
- `"redis"`: Redis-backed queue/lock; otherwise `SqliteMessageQueue` /
  `SqliteDistributedLock` sharing the ledger DB (db required).
- At-least-once delivery; exactly-once processing via ledger idempotency keys.
- `StreamName` defines 11 streams (`queue/base.py:48`); `ORCHESTRATOR_CORE` is
  the control-plane stream.

Configuration: `StorageSettings.backend_queue`, `StorageSettings.redis_url`
(default `redis://127.0.0.1:6379/0`), `QueueSettings` (`backpressure_depth=10`,
`shed_load_depth=50`, `max_delivery_attempts=3`,
`visibility_timeout_seconds=300`, `entity_lock_ttl_seconds=60`).

`BackpressureController` (`backpressure.py:77`): pure NORMAL / DEGRADED /
SHEDDING decision on queue depth; control-plane streams always accepted.

Consumers: `Runtime` (`runtime.py:373`), `ChiefOrchestrator.submit` enqueues
(`orchestrator.py:233`), daemon dispatch claims (`daemon.py:125`),
recovery requeue (`recovery.py:437`), API backpressure gate
(`api/app.py:198`).

## 6. Writes, transactions, and durability

- **Ledger writes** are the spine: `LedgerStore.append` is one SQLite
  transaction assigning `state_version`, linking `prev_hash`, inserting, and
  updating the head projection atomically (`store.py:12`).
- **Database transactions**: `Database.transaction()` uses `BEGIN IMMEDIATE`
  (single writer). WAL allows concurrent readers.
- **Snapshot durability**: `close()` checkpoints WAL (TRUNCATE), making a file
  copy a complete backup (`database.py:132`).
- **CAS durability**: atomic staging→rename; hash-verified reads.

## 7. Degradation and fallback behavior

Every optional-backend factory (`_try_vector`, `_try_graph`, `_try_queue`,
`get_blob_store`) catches `Exception` and returns `None`/fallback with a logged
warning — never raises for a missing optional backend in `auto` mode
(`runtime.py:349`–`:379`). The runtime must boot on a laptop with only core
dependencies installed (`AGENTS.md:60`).

Degradation impact:
- No vector → context gatherer semantic search returns `[]` (`context_agents.py:183`).
- No graph → graph neighbours returns `[]` (`context_agents.py:214`).
- No queue → orchestrator submit skips enqueue (`orchestrator.py:230`); daemon dispatch sleeps (`daemon.py:118`).
- No MinIO → cold lake uses local CAS (`coldlake/__init__.py:66`).

## 8. Consistency and parity

- `schema_sqlite.sql` and `schema_postgres.sql` are table- and column-
  identical (26 business tables + `schema_migrations`); types may differ.
- `tests/storage/test_schema_parity.py` parses both with a DDL lexer and fails
  the build on divergence (`AGENTS.md:51`).
- If a column is added, it must be added to both files (`AGENTS.md:54`).

## 9. What is NOT persisted

- Context packets are rebuilt per hydration from the ledger + hot serving; not
  stored as truth (`context_agents.py`).
- Task projections are derived by replay; snapshots are a cache optimisation
  (`replay.py:92`).
- Queue (Redis) is volatile; the ledger is authoritative for recovery
  (`recovery.py:11`, `orchestrator.py:740`).

## Related documents

- [`data/data-models-and-relationships.md`](data/data-models-and-relationships.md) — table/column reference.
- [`data/data-lifecycle.md`](data/data-lifecycle.md) — creation, mutation, invalidation, deletion.
- [`modules/ledger.md`](modules/ledger.md) — the ledger spine.
- [`modules/storage.md`](modules/storage.md) — storage substrate deep dives.
- [`operations/configuration-and-environment.md`](operations/configuration-and-environment.md) — `PAA_STORAGE__*` settings.
