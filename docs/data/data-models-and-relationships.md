# Data models and relationships

PAA persists state in an **append-only, hash-chained event ledger** (SQLite WAL
by default) plus a set of **hot-serving relational tables** that are derived
from or written alongside the ledger. Optional vector, graph, and cold-lake
substrates are derived views. All 26 tables are defined identically (names and
columns) in `schema_sqlite.sql` and `schema_postgres.sql`; only types differ
(TEXT/INTEGER vs UUID/JSONB/TIMESTAMPTZ). Parity is enforced by
`tests/storage/test_schema_parity.py`.

The source of truth for these schemas is:
- `src/paa/storage/relational/schema_sqlite.sql` (604 lines)
- `src/paa/storage/relational/schema_postgres.sql` (569 lines)

The Python-side data models that drive these tables live in:
- `src/paa/ledger/events.py` — `LedgerEvent`
- `src/paa/ledger/store.py` — `CorrelationHead`
- `src/paa/ledger/replay.py` — `TaskProjection`, `TaskPhase`
- `src/paa/core/types.py` — `EventType`, `Permission`, `PermissionMode`,
  `ComplexityModality`, `AgentRole`, `CorrelationId`, `SessionId`, `TaskId`
- `src/paa/agents/base.py` — `AgentMessage`, `AgentResult`, `AgentContext`
- `src/paa/storage/coldlake/signals.py` — `Signal`
- `src/paa/memory/facts.py` — `Fact`, `Entity`

## 1. The ledger spine (durable)

### `system_state_ledger` — one row per event

This is the only append-only table. Every state transition is a row. Columns
(verified against `schema_sqlite.sql`):

| Column | SQLite type | Postgres type | Notes |
|---|---|---|---|
| `sequence_id` | INTEGER PK AUTOINCREMENT | BIGINT IDENTITY | Global monotonic position (can gap on rollback) |
| `event_id` | TEXT UNIQUE | UUID | UUID of the event |
| `correlation_id` | TEXT | UUID | Lineage key; one task shares one value |
| `session_id` | TEXT | UUID | Optional session isolation |
| `causation_id` | TEXT | UUID | The event that directly caused this one |
| `state_version` | INTEGER | INTEGER | Per-correlation dense counter, starts at 1 |
| `idempotency_key` | TEXT | TEXT | `SHA-256(correlation_id\|event_type\|attempt\|discriminator)` |
| `attempt` | INTEGER DEFAULT 0 | INTEGER | Retry counter |
| `discriminator` | TEXT | TEXT | Distinguishes concurrent same-typed events (e.g. step index) |
| `event_type` | TEXT | TEXT | `EventType` value |
| `execution_mode` | TEXT DEFAULT 'STANDARD' | TEXT | `ComplexityModality` |
| `agent_role` | TEXT | TEXT | `AgentRole` or None |
| `allocated_worker_image` | TEXT | TEXT | Default `paa/base_worker:v4.1` |
| `payload` | TEXT DEFAULT '{}' | JSONB | JSON-serialised event payload |
| `prev_hash` | TEXT | TEXT | Predecessor's `event_hash` |
| `event_hash` | TEXT | TEXT | SHA-256 digest binding this event to its predecessor |
| `recorded_at` | TEXT | TIMESTAMPTZ | UTC timestamp |

The hash chain (`prev_hash` → `event_hash`) makes silent history edits
detectable; verified by `LedgerStore.verify_chain` (`ledger/store.py:385`).

`LedgerStore.append` performs, inside one transaction (`ledger/store.py:116`):
1. Idempotency suppression by `idempotency_key` — returns the existing event on
   duplicate (`ledger/store.py:144`).
2. Assign next per-correlation `state_version` and `prev_hash` from the chain
   tip (`ledger/store.py:146`).
3. Insert the row (`ledger/store.py:158`).
4. Update the head projection (`ledger/store.py:192`).

### `system_state_correlation_head` — one row per lineage

| Column | Type | Notes |
|---|---|---|
| `correlation_id` | TEXT PK / UUID | Lineage key |
| `session_id` | TEXT / UUID | |
| `latest_sequence_id` | INTEGER / BIGINT | |
| `latest_state_version` | INTEGER | |
| `latest_event_type` | TEXT | |
| `latest_event_hash` | TEXT | |
| `execution_mode` | TEXT | |
| `is_terminal` | INTEGER DEFAULT 0 / SMALLINT | 1 if latest event is in `TERMINAL_EVENTS` |
| `opened_at` | TEXT / TIMESTAMPTZ | |
| `updated_at` | TEXT / TIMESTAMPTZ | |

Recovery scans `WHERE is_terminal = 0 ORDER BY updated_at ASC` (`ledger/store.py:357`).
This replaces the RFC's partial-index recovery (which never sheds rows) per
ADR-0010.

### `system_state_snapshots` — replay optimisation checkpoints

| Column | Type | Notes |
|---|---|---|
| `correlation_id` | TEXT / UUID | |
| `state_version` | INTEGER | |
| `projection` | TEXT / JSONB | Serialised `TaskProjection` |
| `created_at` | TEXT / TIMESTAMPTZ | |

PK `(correlation_id, state_version)`. Snapshots are safe to delete; never truth
(`replay.py:92`).

## 2. Hot-serving relational tables (mutable read path)

These 12 tables hold the mutable working state (entities, facts, relationships,
sessions, recovery marks, policy rules, skill registry, etc.). They are written
by the memory creator/curator, policy agent, and validation, and read by the
context gatherer.

| Table | Purpose | Key columns |
|---|---|---|
| `hot_serving_entity_index` | Entity directory (fuzzy-resolvable) | id, class, canonical_name (UNIQUE), aliases, importance_weight, confidence_rating, attributes |
| `hot_serving_active_facts` | Fact store; facts are never overwritten, only superseded | id, entity_id (FK CASCADE), predicate, object_value, memory_domain, initial_confidence, importance, use_count, source_signal_id, superseded_by (FK SET NULL), created_at, last_queried_at |
| `hot_serving_entity_fts` | FTS5 trigram index over entities (SQLite) / pg_trgm GIN (Postgres) | — |
| `hot_serving_relationships` | Provenance/graph edges | from_entity_id, to_entity_id, rel_type (CHECK enum), weight, confidence_decay, evidence_count, contradiction_score |
| `hot_serving_unresolved_buffer` | Contradictions awaiting resolution | entity_id, predicate, incumbent_fact, challenger_fact, conflict_score, correlation_id, resolved_at |
| `hot_serving_sessions` | Session records | session_id PK, session_name, status, permission_mode, workspace_path |
| `hot_serving_task_workspaces_vault` | Task workspace file contents | task_id FK, filename (UNIQUE per task), file_content, checksum, revision |
| `hot_serving_skill_registry` | Registered skills | skill_name UNIQUE, provider, version, input_schema, output_schema, risk_profile, required_permissions, is_active, signature |
| `hot_serving_agent_registry` | Agent registrations | agent_name UNIQUE, role, allowed_tools, granted_permissions, recursion_ceiling, token_ceiling |
| `hot_serving_execution_runs` | Execution trace records | trace_id PK, correlation_id, agent_role, modality, duration_ms, tokens_consumed, escalated, error_detail |
| `hot_serving_evaluation_results` | Evaluation results | correlation_id, evaluator, verdict, score, findings |
| `hot_serving_recovery_marks` | Workspace checkpoints for crash recovery | correlation_id, state_version, workspace_path, manifest, manifest_hash, UNIQUE(correlation_id, state_version) |
| `hot_serving_policy_rules` | Operator-authored regex policy rules | rule_name UNIQUE, rule_kind, pattern, severity, threshold, is_active |
| `hot_serving_memory_registry` | Memory domain registry | domain PK, storage_engine, decay_lambda, prune_floor, retrieval_protocol, is_immutable |

Note: `hot_serving_memory_registry` and `hot_serving_agent_registry` appear to
be part of the RFC's data model; grep confirms they are referenced in schema
and by `policy.py` (`hot_serving_policy_rules`) and `context_agents.py`
(`hot_serving_active_facts`). Some tables may be defined in schema but lightly
consumed — marked *partially verified* where the consuming code path could not
be fully established.

## 3. Queue tables

| Table | Purpose |
|---|---|
| `queue_messages` | Durable at-least-once messages; status `ready`/`claimed`/`done`/`dead`; attempts/max_attempts; visible_after |
| `queue_locks` | Distributed locks; lock_key PK, holder, expires_at |

11 streams are defined in `StreamName` (`storage/queue/base.py:48`):
`RAW_TELEMETRY`, `ORCHESTRATOR_CORE`, `PLANNER_STRAT`, `WORKERS_ALLOC`,
`MEMORY_CURATION`, `SANDBOX_RUN`, `CRITIC_AUDIT`, `WATCHDOG_MONITOR`,
`HEARTBEAT_PING`, `RETRY_FAILED`, `DEAD_LETTER_POISON`.

`ORCHESTRATOR_CORE` is the control-plane stream the orchestrator and daemon use
(`orchestrator.py:233`, `daemon.py:130`).

## 4. Marketplace tables

| Table | Purpose |
|---|---|
| `marketplace_packages` | Registered `.paapkg` packages; UNIQUE(package_name, version); signature_state |
| `marketplace_trusted_keys` | Trusted public keys for package signature verification |

## 5. Improvement tables

| Table | Purpose |
|---|---|
| `improvement_reflections` | Reflection windows: friction_score, corrections, rollbacks, successes, extracted_rule |
| `improvement_skill_weights` | Per-skill reliability weights (EWMA) |

## 6. Migration table

| Table | Purpose |
|---|---|
| `schema_migrations` | version PK, applied_at, description |

## 7. Python-side domain models

### `LedgerEvent` (`ledger/events.py:132`)
Frozen pydantic model. Chain fields (`sequence_id`, `state_version`, `prev_hash`,
`event_hash`) assigned by the store at append time. Caller-supplied fields:
`event_id`, `correlation_id`, `session_id`, `causation_id`, `event_type`,
`execution_mode`, `agent_role`, `attempt`, `discriminator`, `payload`,
`recorded_at`. `idempotency_key` is a computed property; `is_terminal` checks
membership in `TERMINAL_EVENTS`.

### `EventType` (`core/types.py:67`)
22 members ordered along the canonical task lifecycle:
`TASK_REQUESTED`, `TASK_QUEUED`, `CONTEXT_HYDRATION_REQUESTED`,
`CONTEXT_HYDRATED`, `PLAN_COMPILED`, `POLICY_CLEARED`, `POLICY_BLOCKED`,
`SECURITY_VIOLATION`, `EXECUTION_STARTED`, `EXECUTION_COMPLETED`,
`EXECUTION_FAILED`, `AWAITING_HUMAN_ATTESTATION`, `HUMAN_GATE_CLEARED`,
`HUMAN_GATE_REJECTED`, `CRITIQUE_CONCLUDED`, `VALIDATION_FAILED`,
`MUTATION_COMMITTED`, `STATE_ROLLBACK_TRIGGERED`, `RECOVERY_REPLAY_COMPLETED`,
`USER_CORRECTION`, `MODEL_ESCALATED`, `TASK_ABANDONED`.

### `TERMINAL_EVENTS` (`core/types.py:132`)
`MUTATION_COMMITTED`, `EXECUTION_FAILED`, `POLICY_BLOCKED`, `SECURITY_VIOLATION`,
`HUMAN_GATE_REJECTED`, `TASK_ABANDONED`. Notably **not** `EXECUTION_COMPLETED`
(per-step, non-terminal).

### `TaskProjection` / `TaskPhase` (`replay.py:88`, `:34`)
The derived read model of a lineage. Fields include `phase`, `modality`,
`state_version`, `request`, `context_packet`, `plan_steps`,
`current_step_index`, `completed_steps`, `policy_decision`, `policy_reason`,
`workspace_path`, `checkpoint_manifest_hash`, `applied_patch_sha256`,
`validation_verdict`, `attempts`, `rollback_count`, `escalation_count`,
`tokens_consumed`, `user_corrections`, `errors`, `awaiting_reason`,
`created_at`, `updated_at`, `unknown_events`.

### `Fact` / `Entity` (`memory/facts.py:278`, `:252`)
`Fact`: id, entity_id, predicate, object_value, domain, scope,
`initial_confidence` (C₀), `confidence` (decayed at read time), importance,
use_count, source_signal_id, provenance, embedding_status, created_at,
last_queried_at, superseded_by. Facts are **never overwritten** — new
information supersedes old (`facts.py` docstring). `Entity`: id, class,
canonical_name, aliases, importance, confidence, attributes, timestamps.

### `Signal` (`storage/coldlake/signals.py:71`)
Frozen pydantic: id, received_at, channel, external_id, raw_payload,
content_hash, blob_uri, sync_status, processed_at, error_detail.

## 8. Relationships (ER-ish summary)

```
system_state_ledger 1:N system_state_correlation_head (correlation_id)
system_state_ledger 1:N system_state_snapshots (correlation_id)

hot_serving_entity_index 1:N hot_serving_active_facts (entity_id FK CASCADE)
hot_serving_active_facts 1:N hot_serving_active_facts (superseded_by FK SET NULL)
hot_serving_entity_index 1:N hot_serving_relationships (from/to entity_id FK CASCADE)
hot_serving_entity_index 1:N hot_serving_unresolved_buffer (entity_id)

hot_serving_sessions 1:N hot_serving_task_workspaces_vault (session via task)
hot_serving_task_workspaces_vault 1:N (task_id, filename UNIQUE)

marketplace_trusted_keys 1:N marketplace_packages (publisher_key)
```

Queue tables and `cold_lake_signals`/`cold_lake_artifacts_archive` are
independent of the ledger FK structure (signals reference `signal_id`→
`cold_lake_artifacts_archive`, `correlation_id`→ledger).

## 9. Data model verification status

| Model | Verified against | Status |
|---|---|---|
| `system_state_ledger` | `schema_sqlite.sql`, `store.py`, `events.py` | Verified |
| `system_state_correlation_head` | `schema_sqlite.sql`, `store.py` | Verified |
| `system_state_snapshots` | `schema_sqlite.sql`, `store.py` | Verified |
| `hot_serving_*` tables | `schema_sqlite.sql`, consuming code | Verified (schema); consumption partially verified |
| `queue_messages` / `queue_locks` | `schema_sqlite.sql`, `queue/base.py` | Verified |
| `marketplace_*`, `improvement_*` | `schema_sqlite.sql` | Schema verified; runtime consumption partially verified |
| `LedgerEvent` / `EventType` | `events.py`, `types.py` | Verified |
| `TaskProjection` / `TaskPhase` | `replay.py` | Verified |
| `Fact` / `Entity` | `facts.py` | Verified |

## Related documents

- [`data/persistence-and-storage.md`](persistence-and-storage.md) — how data is written, read, and validated.
- [`data/data-lifecycle.md`](data-lifecycle.md) — creation, mutation, invalidation, deletion.
- [`modules/ledger.md`](../modules/ledger.md) — the ledger spine deep dive.
- [`modules/storage.md`](../modules/storage.md) — storage substrates deep dive.
