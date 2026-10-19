# Module: Ledger (`src/paa/ledger/`)

## Module identity

- **Name:** Ledger
- **Path:** `src/paa/ledger/`
- **Main responsibility:** The append-only, tamper-evident event log — the runtime's only source of truth. Every state transition is recorded here before any side effect.
- **Architectural layer:** Data (spine)
- **Entrypoints:** `LedgerStore.append()`, `LedgerStore.read_correlation()`, `project()` (replay), `RecoveryEngine.boot_sweep()`
- **Public interfaces:** `LedgerEvent`, `canonical_json`, `compute_idempotency_key`, `compute_event_digest`, `GENESIS_HASH` (`events.py`); `LedgerStore`, `CorrelationHead` (`store.py`); `TaskProjection`, `TaskPhase`, `apply_event`, `replay`, `project` (`replay.py`); `RecoveryEngine`, `RecoveryReport`, `RecoveryOutcome` (`recovery.py`)
- **Other modules that depend on it:** `agents/orchestrator.py` (emits every event), `runtime.py` (builds `LedgerStore`, `RecoveryEngine`), `api/app.py` (`/ledger/{id}`, `/tasks/{id}`), `cli/main.py` (`paa ledger`, `paa recover`, `paa status`), `daemon.py` (boot recovery), `ledger/recovery.py`, `ledger/replay.py`

## Purpose and responsibilities

The ledger is the spine. Redis, queues, vector/graph indexes, and the
filesystem are all derived views rebuilt by replay (`AGENTS.md:33`). Two rules:

1. **Ledger event before side effect, always.** A crash between the event and
   the work is recoverable; the reverse is not.
2. **Everything else must be replayable.** State not derivable from events needs
   a snapshot and a recovery rule.

Responsibilities:
- Immutable, ordered event storage with per-correlation `state_version`.
- Idempotent append (at-least-once → effectively-once).
- Hash chain for tamper evidence.
- Head projection for efficient recovery scans.
- Deterministic replay into a `TaskProjection`.
- Post-crash boot sweep with workspace reconciliation.

## Internal implementation

### `events.py` — event envelope
`LedgerEvent` is a frozen pydantic model (`events.py:132`). Chain fields
(`sequence_id`, `state_version`, `prev_hash`, `event_hash`) are `None` until
the store assigns them inside the append transaction; the sealed copy is
returned (`sealed()`, `events.py:201`).

`canonical_json()` (`events.py:40`): sorted keys, no whitespace, non-ASCII
preserved — any serialization change breaks the hash chain (data migration).

`compute_idempotency_key()` (`events.py:68`):
`SHA-256(correlation_id|event_type|attempt|discriminator)`. `attempt` permits
genuine retries; `discriminator` distinguishes concurrent same-typed events
(e.g. step index). This is ADR-0008 — the RFC's original key (without attempt)
made retries impossible under a UNIQUE constraint.

`compute_event_digest()` (`events.py:105`): binds `prev_hash`,
`correlation_id`, `event_type`, `state_version`, `payload`, `recorded_at`.

`is_terminal` property (`events.py:196`): membership in `TERMINAL_EVENTS`
(`core/types.py:132`).

### `store.py` — append-only store
`LedgerStore.append()` (`store.py:113`) inside one transaction:
1. Suppress duplicates by idempotency key → return existing event (`store.py:122`).
2. Read chain tip (highest `state_version` + its `event_hash`) → `state_version
   + 1`, `prev_hash` (`store.py:134`).
3. `event.sealed()` then `INSERT` (`store.py:142`).
4. On `sqlite3.IntegrityError` (concurrent writer in another process), re-read
   by key and return whatever landed (`store.py:147`).
5. `sequence_id = cursor.lastrowid`; `_update_head()` upserts
   `system_state_correlation_head` (`store.py:158`).

`read_correlation()` orders by `state_version` (dense), not `sequence_id`
(which can gap on rollback) (`store.py:254`).

`open_correlations()` (`store.py:278`) reads `WHERE is_terminal = 0` from the
head projection — the recovery sweep's entry point; replaces the RFC's partial
index (ADR-0010).

`verify_chain()` (`store.py:315`): per event, checks digest recomputes,
`prev_hash` matches predecessor, versions dense/monotonic.

Snapshots: `save_snapshot`/`load_snapshot` (`store.py:364`) checkpoint a
`TaskProjection` as a replay optimisation; safe to delete.

### `replay.py` — deterministic projection
`TaskProjection` (`replay.py:88`) is the derived read model. `TaskPhase`
(`replay.py:34`) is the coarse lifecycle position (`CREATED`…`ABANDONED`).

`apply_event()` (`replay.py:170`) is pure and total: never raises on unknown
events (records them in `unknown_events`). `_PHASE_MAP` (`replay.py:149`) maps
each `EventType` to a phase; event-specific state (plan steps, policy decision,
attempts, completed steps, tokens) applied in the `match` block.

`EXECUTION_COMPLETED` fires per step and updates `completed_steps`; the phase
stays `EXECUTING` until all steps are done (`replay.py:239`). This is ADR-0010
— treating `EXECUTION_COMPLETED` as terminal would skip remaining steps.

`project()` (`replay.py:322`) loads a snapshot (if compatible) then replays
from that version.

### `recovery.py` — crash recovery
`RecoveryEngine.boot_sweep()` (`recovery.py:222`):
1. `verify_all_chains()` — reports chain problems, never auto-repairs
   (`recovery.py:231`).
2. `open_correlations()` from head projection (never the queue — `recovery.py:
   11`).
3. Per lineage: `project()`; terminal → `ALREADY_TERMINAL`;
   `AWAITING_HUMAN` → `PARKED_HUMAN` (crash is never consent);
   `attempts >= max` → `TASK_ABANDONED`; else reconcile workspace, re-queue.
4. `_reconcile_workspace()` compares disk vs `hot_serving_recovery_marks`
   checkpoint manifest; drift → `STATE_ROLLBACK_TRIGGERED` + restore.
5. `_requeue()` enqueues to `StreamName.ORCHESTRATOR_CORE`; failure logged,
   non-fatal (`recovery.py:444`).
6. `_record_sweep()` appends `RECOVERY_REPLAY_COMPLETED`, suppressed if the
   latest event is already one (idempotent sweeps; `recovery.py:306`).

`RecoveryOutcome`: `RESUMED`, `ROLLED_BACK`, `PARKED_HUMAN`, `ABANDONED`,
`UNRECOVERABLE`, `ALREADY_TERMINAL` (`recovery.py:79`).

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `LedgerStore.append(event)` | `LedgerEvent` | sealed `LedgerEvent` | writes `system_state_ledger` + `system_state_correlation_head` |
| `read_correlation(cid, from_version)` | correlation id | list of `LedgerEvent` | read only |
| `project(store, cid)` | store + correlation id | `TaskProjection` | read only (snapshot load optional) |
| `RecoveryEngine.boot_sweep()` | — | `RecoveryReport` | writes recovery events; restores workspace; re-queues |
| `apply_event(state, event)` | projection + event | new projection | pure |

## Dependencies and connections

| Component | Relationship | Why it connects | Source |
|---|---|---|---|
| `ChiefOrchestrator` | calls `LedgerStore.append` | Emits every state transition | `orchestrator.py:160` |
| `Runtime.build` | constructs `LedgerStore`, `RecoveryEngine` | Composition root | `runtime.py:264`, `:294` |
| `RecoveryEngine` | uses `LedgerStore`, `Database` | Replay and reconcile | `recovery.py:142` |
| `replay.project` | reads `LedgerStore.read_correlation` | Build TaskProjection | `replay.py:347` |
| `API /ledger/{id}` | calls `read_correlation` | Explainability endpoint | `api/app.py:149` |
| `CLI paa ledger` | calls `read_correlation` + `verify_chain` | Terminal explainability | `cli/main.py:140` |
| `Database` | used by `LedgerStore` | SQLite persistence | `store.py:31` |

## Runtime behavior

- Initialized by `Runtime.build()`; `boot_recovery()` runs at startup
  (`runtime.py:331`).
- All writes are async, inside `Database.transaction()` (`BEGIN IMMEDIATE`).
- Termination: append returns the sealed event; no long-running loop.

## Failure behavior

- Duplicate idempotency key → returns existing event (not an error)
  (`store.py:122`).
- Concurrent writer race → re-read by key, return what landed
  (`store.py:147`).
- Chain verification failure → reported, not repaired (`recovery.py:231`).
- One bad lineage in recovery → logged as `UNRECOVERABLE`, sweep continues
  (`recovery.py:246`).

## Modification impact

- Adding an `EventType`: touch `core/types.py` (`EventType`, `TERMINAL_EVENTS`
  if terminal), `ledger/replay.py` (`_PHASE_MAP` / `_NON_PHASE_EVENTS`), and
  `agents/orchestrator.py` emitter (`AGENTS.md:41`).
- Changing `canonical_json` serialization breaks the hash chain — data
  migration (`events.py:40`).
- Tests: `tests/ledger/` (71 tests per README).

## Known gaps

- `EXECUTION_COMPLETED` is per-step, not terminal — a common misreading
  (`ADR-0010`, `types.py:132`).
- `open_correlations` reads the head projection, which can lag if a crash
  lands between ledger insert and head upsert; recovery repairs it
  (`recovery.py:269`).

## Related documents

- [`architecture/overview.md`](../architecture/overview.md) — ledger-is-truth invariant.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — ledger connections.
- [`data/data-models-and-relationships.md`](../data/data-models-and-relationships.md) — `system_state_ledger` schema.
- [`runtime/background-jobs-and-events.md`](../runtime/background-jobs-and-events.md) — recovery sweep.
- [`flows/workflow-03-crash-recovery.md`](../flows/workflow-03-crash-recovery.md) — recovery trace.
- [`docs/adr/0008-idempotency-key-with-attempt.md`](../adr/0008-idempotency-key-with-attempt.md)
- [`docs/adr/0010-head-projection-not-partial-index.md`](../adr/0010-head-projection-not-partial-index.md)
