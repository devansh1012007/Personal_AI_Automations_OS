# Data lifecycle

This document traces data through PAA: where it originates, how it is created,
validated, transformed, persisted, retrieved, mutated, invalidated, and deleted,
and what happens on failure. All paths are verified against source where cited.

## 1. Task lifecycle data (the ledger is truth)

### Creation
A task originates from a caller (CLI, API, daemon, or coder agent tool).
`ChiefOrchestrator.submit()` (`orchestrator.py:188`) mints a `CorrelationId`
and writes `TASK_REQUESTED` then `TASK_QUEUED` to `system_state_ledger` before
enqueuing to `queue_messages` (`StreamName.ORCHESTRATOR_CORE`). Submission is
deliberately cheap and always succeeds so the request is durable before any
work (`orchestrator.py` docstring).

### Transformation
Each phase of `_hydrate` → `_plan` → `_check_policy` → `_execute_steps` →
`_commit` emits its own event with a payload:
- `CONTEXT_HYDRATED` carries `context_packet` (dict from the context builder).
- `PLAN_COMPILED` carries `execution_steps` (array of step dicts; each may
  carry `command`, `required_permissions`, `path`, `risk_profile`,
  `always_human_gate` — passthrough keys preserved by `StrategicPlanner.
  _normalise`, `reasoning.py:155`).
- `POLICY_CLEARED` / `POLICY_BLOCKED` / `SECURITY_VIOLATION` carry `decision`
  and `reason`.
- `EXECUTION_STARTED`/`COMPLETED`/`FAILED` carry `step_index`, `attempt`,
  `tokens_consumed`.
- `CRITIQUE_CONCLUDED` carries `verdict`.
- `MUTATION_COMMITTED` carries `steps_completed`, `tokens_consumed`.

### Persistence
Every event is persisted by `LedgerStore.append` into `system_state_ledger`
(one transaction). The head projection (`system_state_correlation_head`) is
updated in the same transaction. `state_version` is per-correlation and dense;
`sequence_id` is global and may gap on rollback (`ledger/store.py:12`).

### Retrieval
`LedgerStore.read_correlation()` returns all events for a lineage ordered by
`state_version` (`ledger/store.py:149`). `project()` (`replay.py:322`) folds them into
a `TaskProjection`. `LedgerStore.head()` reads the head projection for status
(`ledger/store.py:341`).

### Mutation
Tasks are not mutated in place. New information is a new event; the lineage is
append-only. The only "mutation" of the head row is the upsert in
`_update_head` (`ledger/store.py:292`).

### Invalidation / deletion
- Terminal events close a lineage (`TERMINAL_EVENTS`, `types.py:132`); recovery
  skips `is_terminal = 1` rows (`ledger/store.py:357`).
- `STATE_ROLLBACK_TRIGGERED` restores a workspace to a checkpoint manifest and
  resets `completed_steps` to steps before `restored_step_index`
  (`replay.py:266`).
- `TASK_ABANDONED` closes a lineage that exceeded `max_delivery_attempts`
  (`recovery.py:285`).
- There is no explicit "delete a task" operation; ledger rows are append-only.

### Failure halfway through
- A crash between event and work is recoverable: replay knows the work was
  intended (`AGENTS.md:36`).
- A crash between work and event is not recoverable — by design, this is the
  only non-survivable failure and it is avoided by ordering.
- `RecoveryEngine.boot_sweep()` replays open lineages, reconciles the workspace
  against `hot_serving_recovery_marks`, restores drift (`STATE_ROLLBACK_
  TRIGGERED`), re-queues resumable phases, parks `AWAITING_HUMAN`
  (`recovery.py:263`).

### Can a retry cause duplication?
Idempotency keys (`SHA-256(correlation_id|event_type|attempt|discriminator)`)
make redelivery of the same attempt a no-op (`ledger/store.py:144`), while a genuine
retry bumps `attempt` (`events.py:68`). ADR-0008 fixed the RFC's original
scheme (which made retries impossible under a UNIQUE constraint).

## 2. Memory / fact lifecycle

### Creation
`MemoryCreator.run_batch()` (`memory/creator.py`) claims unprocessed signals
(`SignalRepository.claim_unprocessed`, `signals.py:212`) and writes entities
and facts via `FactRepository.upsert_entity` / `add_fact` (`memory/facts.py:347`,
`:789`) into `hot_serving_entity_index` and `hot_serving_active_facts`.

Facts are **never overwritten**: `add_fact` appends a new version; `supersede`
links `old → new` (`memory/facts.py:860`, refuses cycles). `supersession_chain`
walks back with a visited-set guard (`memory/facts.py:892`).

Confidence stored is `initial_confidence` (C₀); the effective confidence
`C(t) = C₀·e⁻ˡᵃᵐᵇᵈᵃ·t` is computed lazily at read time
(`memory/decay.py::effective_confidence`, `decay.py:57`).

### Retrieval
- `FactRepository.query_facts()` filters by `min_confidence` on **decayed**
  confidence in Python (cannot push into SQL because it's derived), bounded by
  `max_scan=10_000`, sorted by `(-confidence, -importance, created_at)`
  (`facts.py:904`).
- `resolve_entity()` (`facts.py:456`): a three-rung ladder — exact → alias →
  fuzzy — in which the **Python gate is the authority**. The gate is a
  character-trigram Jaccard similarity over the 3-grams of the raw,
  case-folded text (`facts.py:157`, `:203`) — FTS5's own gram model, so a
  pair the gate accepts is a pair the index can hold. The database only
  decides *which rows the gate gets to look at*, and it returns a candidate
  set that is a **superset** of the gate's acceptance set for **ASCII**
  corpora: a **phrase**
  query ("is the term a substring of the row's text?") with a **broad**
  fallback ("does the row share at least one of the term's grams?"), which
  is the gate's own precondition. The phrase query is a precision pass; the
  broad one runs when the phrase rows do not clear the floor. Candidates are
  ordered by a **portable total order** — the count proxy `DESC`, then the
  unique `e.id ASC` — so the `LIMIT` cut is the same cut on both substrates
  and the gate's tie-break is deterministic. For an ASCII corpus SQLite and
  PostgreSQL therefore
  return the same entity, the same match kind and the same score for the
  same term; the details, and the recorded non-ASCII limitations, are in
  [**ADR-0029**](../adr/0029-entity-resolution-parity.md). The parity is
  exact for **ASCII** corpora and bounded outside it: the *column* is folded
  by the engine, so PostgreSQL's locale-aware `lower()` sees rows SQLite
  cannot, and the two substrates can answer differently for the same
  non-ASCII name.
- Context gatherer reads hot facts with decayed confidence at read time
  (`context_agents.py:223`).

### Mutation / invalidation
- `DecaySweeper.sweep()` (`memory/decay.py`): per-domain `decay_lambda`; facts
  below `prune_floor` are archived to cold lake then deleted. Uses keyset
  pagination (`WHERE id > ?`) so deletion during scan doesn't skip rows
  (OFFSET would skip — documented bug fix, `README.md:87`).
- `ContradictionError`: conflicts above `contradiction_threshold` (0.75)
  quarantine both records at `contradiction_degraded_confidence` (0.20) and
  demand human tie-break; never auto-resolved (`core/errors.py:279`, `memory/
  contradiction.py`).
- `touch`/`touch_many` reset `last_queried_at` and increment `use_count`
  (`decay.py`).

### Deletion
Only the decay sweep deletes facts (after archiving). Immutable domains
(`episodic`, `strategic`, `identity`) are never pruned (`domains.py`). There is
no user-facing fact deletion.

## 3. Context packet lifecycle

### Creation
`PlannerContextBuilder.handle()` (`context_agents.py:241`):
1. Expand goal → search terms + required slots (`QueryExpander`, deterministic
   `_KeywordExpander` fallback).
2. `_hot_facts()` — relational candidates from `hot_serving_active_facts`,
   confidence decayed at read time (`effective_confidence`).
3. `_semantic()` — vector search over `active_facts` (if vector store +
   embedder available).
4. `_graph_neighbours()` — bounded traversal by `graph_hops` (modality profile).
5. `BoundedContextGatherer.compile()` packs within `token_ceiling`
   (1500 planner / 1000 worker default), ranking by
   `(tier, -(relevance*confidence), -importance, id)`.

### Output
A packet with `routing_directive`:
- `density >= density_proceed (0.85)` → `PROCEED_TO_PLANNER`.
- `density >= density_hydrate (0.40)` → `TRIGGER_BACKGROUND_HYDRATION`.
- else → `HARD_STOP_ESCALATE_TO_USER` (`ContextInsufficientError`).
Also `vacant_slots`, `density`, `allocated_tokens`, `token_ceiling`.

Hard guarantee: `allocated_tokens <= ceiling` (assertion, `gatherer.py:565`).

### Consumption
Orchestrator `_hydrate()` reads `routing_directive`; on HARD_STOP it parks the
task on `AWAITING_HUMAN_ATTESTATION` (`orchestrator.py:379`).

### Worker packet
`WorkerContextBuilder.handle()` (`context_agents.py:285`) drops all narrative
domains (`narrative_domains()` = `{narrative, reflection, strategic, identity}`)
and fails with `hydration_required` if any required slot is vacant
(`context_agents.py:303`). This is a security boundary: strategy/identity never
enters the sandbox.

## 4. Workspace / patch lifecycle

### Creation
`WorkerCell.handle()` runs a sandboxed command; `SandboxResult.stdout` becomes
the `patch` in the step output (`reasoning.py:469`).

### Validation
`CriticValidatorAdapter` (`runtime.py:65`) adapts `DeterministicValidationEngine`
(`validation/engine.py:208`) to the critic's call shape. Checks in order:
schema → AST security scan → patch dry-run (`UnifiedDiffValidator.dry_run`,
`validation/patch.py:467`) → workspace drift → tests (runs last, only inside
sandbox). Binary PASS/FAIL; deterministic failure short-circuits before the
model is consulted (`engine.py:226`).

Patch dry-run computes full post-patch content before any write; `safe_
relative_path` blocks traversal/absolute/symlink/NUL escapes
(`validation/patch.py:270`). `PatchApplier.apply` is atomic (temp + fsync +
rename) with in-memory rollback journal (`validation/patch.py:672`).

### Commit
On `CRITIQUE_CONCLUDED` verdict `PASS`, orchestrator emits `EXECUTION_COMPLETED`;
after all steps, `MUTATION_COMMITTED` (`orchestrator.py:609`). The worker never
applies its own patch — the host validates and commits (`reasoning.py` docstring,
RFC §14.3).

## 5. Signal / cold-lake lifecycle

### Creation
External event → API `POST /signals` → `SignalRepository.record()` (idempotent
on `channel`+`external_id`) → row in `cold_lake_signals`; oversized payloads
offloaded to CAS blobs (`cold_lake/blobs/<sha>/<sha>.zst`) with `cas://` stub.

### Consumption
Daemon ingestion loop (`daemon.py:139`) → `MemoryCreator.run_batch()` claims
`unprocessed` signals → creates facts/entities → marks signals `processed`,
`malformed`, or `quarantined`.

### Retention
No explicit TTL or archival policy for signals found in code. `sync_status`
tracks `unprocessed`/`processing`/`processed`/`malformed`/`quarantined`
(`storage/coldlake/signals.py:62`).

## 6. Queue message lifecycle

### Creation
Orchestrator `submit()` enqueues `{correlation_id, goal}` to
`StreamName.ORCHESTRATOR_CORE` (`orchestrator.py:233`). Recovery `_requeue()`
re-enqueues recovered lineages (`recovery.py:437`).

### Consumption
Daemon `_claim_and_run()` claims messages (`queue.claim`), runs each via
`Runtime.run(cid)`, then `ack` on success or `nack` on failure
(`daemon.py:128`).

### Failure / dead letter
`max_delivery_attempts` (default 3); exhausted messages go to
`DEAD_LETTER_POISON` (`queue/base.py`). `visibility_timeout_seconds` (300)
governs redelivery of claimed-but-unacked messages. Exactly-once processing is
provided by ledger idempotency, not the queue.

## 7. Configuration data lifecycle

`Settings` is built once (`get_settings()`, `lru_cache`d, `config.py:489`).
`.env` is read from CWD at construction; provider API keys resolved via
`dotenv_value` without touching `os.environ` (`config.py:460`). `PAA_HOME`
changes require `reset_settings_cache()`. `ensure_directories()` creates all
state directories (`config.py:450`).

## 8. Summary of persistence boundaries

| Data | Durable? | Store | Derived from | Deletion |
|---|---|---|---|---|
| Ledger events | Yes | `system_state_ledger` | — | None (append-only) |
| Head projection | Yes (derived cache) | `system_state_correlation_head` | Ledger | None |
| Task projection | No | In memory (replay) | Ledger | Recomputed |
| Snapshots | No (cache) | `system_state_snapshots` | Projection | Safe to delete |
| Entities/facts | Yes | `hot_serving_*` | Signals | Decay sweep |
| Vectors | No (derived) | Vector store | Facts | Rebuildable |
| Graph edges | No (derived) | Graph store | Facts | Rebuildable |
| Queue messages | Yes (SQLite) / volatile (Redis) | `queue_messages` | Orchestrator/recovery | ack/nack |
| CAS blobs | Yes | `cold_lake/blobs` | Signals | None (immutable) |
| Recovery marks | Yes | `hot_serving_recovery_marks` | Checkpoint | None |
| Session transcripts | Yes | JSONL (`PAA_HOME/sessions`) | Coder loop | None |

## Related documents

- [`data/data-models-and-relationships.md`](data-models-and-relationships.md) — table/column reference.
- [`data/persistence-and-storage.md`](persistence-and-storage.md) — substrates and degradation.
- [`modules/ledger.md`](../modules/ledger.md) — the ledger spine.
- [`modules/memory.md`](../modules/memory.md) — memory domains and decay.
- [`flows/workflow-03-crash-recovery.md`](../flows/workflow-03-crash-recovery.md) — crash recovery data flow.
