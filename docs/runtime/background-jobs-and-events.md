# Background jobs and events

PAA has one background runtime component: the **Daemon** (`src/paa/daemon.py`),
plus the **recovery boot sweep** (`src/paa/ledger/recovery.py`) that runs before
the daemon accepts work. There is no separate worker process, cron scheduler,
or external message broker — the queue lives inside the relational SQLite DB by
default (Redis optional), and the ledger is authoritative over the queue
(`AGENTS.md:33`, `recovery.py:11`).

## 1. The Daemon

`Daemon(runtime)` (`daemon.py:40`) runs three cooperating, independently
supervised loops plus a one-time boot recovery:

| Loop | Interval | What it does |
|---|---|---|
| Boot recovery | once at `start()` | `runtime.boot_recovery()` — replay open lineages, reconcile workspaces, re-queue (`daemon.py:61`) |
| Dispatch | 0.5s | Claims queued tasks from `StreamName.ORCHESTRATOR_CORE` and runs each through `runtime.run(cid)`; `ack` on success, `nack` on failure (`daemon.py:116`) |
| Ingestion | 1.0s | Drains unprocessed signals into hot serving via `MemoryCreator.run_batch(limit=25)`; isolates one poison signal from the batch (`daemon.py:139`) |
| Maintenance | 6h | `DecaySweeper(db).sweep()` — evicts stale facts per domain decay policy (`daemon.py:151`) |

### Supervision
Each loop is wrapped in `_supervise()` (`daemon.py:91`): an unexpected error is
logged and the loop restarts after 1s — one failing loop cannot take down the
others. `asyncio.CancelledError` propagates on shutdown.

### Shutdown
Cooperative: `stop()` sets an `asyncio.Event`; loops check it between cycles
and drain (`daemon.py:70`). `run_forever()` blocks on the event
(`daemon.py:81`).

### Claim semantics
`_claim_and_run()` (`daemon.py:116`) claims messages (not just reads), so a
crash mid-task leaves the message reclaimable — no task is silently dropped.
On exception it `nack`s (with error) rather than `ack`, allowing redelivery up
to `max_delivery_attempts` (`daemon.py:134`).

### Queue dependency
If `runtime.queue is None` (no queue backend), the dispatch loop sleeps out its
interval and drains nothing (`daemon.py:118`).

## 2. Recovery boot sweep

`RecoveryEngine.boot_sweep()` (`recovery.py:222`), invoked by
`ChiefOrchestrator.boot()` (`orchestrator.py:737`) and by `Runtime.build()`
(`runtime.py:331`) and `Daemon.start()` (`daemon.py:61`):

1. `verify_all_chains()` — hash-chain integrity across all lineages; problems
   are **reported, never auto-repaired** (`recovery.py:231`).
2. `open_correlations()` — reads open lineages from the **head projection**
   (`is_terminal = 0`), never from the queue (`store.py:278`).
3. For each head: `project()` → `TaskProjection`:
   - terminal → `ALREADY_TERMINAL` (repair head projection) (`recovery.py:268`).
   - `AWAITING_HUMAN` → `PARKED_HUMAN`; a crash is never interpreted as consent
     (`recovery.py:274`).
   - `attempts >= max_attempts` → `TASK_ABANDONED` (`recovery.py:285`).
   - `_reconcile_workspace()` — compare disk vs checkpoint manifest; on drift,
     `STATE_ROLLBACK_TRIGGERED` + restore + `ROLLED_BACK`; else `RESUMED`
     (`recovery.py:350`).
   - `_requeue()` — enqueue to `StreamName.ORCHESTRATOR_CORE` with `resume_phase`
     and `resume_step_index`; enqueue failure is logged, non-fatal
     (`recovery.py:437`).
   - `_record_sweep()` — appends `RECOVERY_REPLAY_COMPLETED` audit event,
     suppressed if the latest event is already one (idempotent sweeps;
     `recovery.py:306`).

`RecoveryOutcome`: `RESUMED`, `ROLLED_BACK`, `PARKED_HUMAN`, `ABANDONED`,
`UNRECOVERABLE`, `ALREADY_TERMINAL` (`recovery.py:79`).

Idempotent by design — running the sweep twice yields the same end state
(`recovery.py:17`).

## 3. Events as the job backbone

There are no standalone "job" records. Work is represented by ledger event
lineages. The event vocabulary (`src/paa/core/types.py:67`):

| Event | Meaning | Terminal? |
|---|---|---|
| `TASK_REQUESTED` / `TASK_QUEUED` | Intake | No |
| `CONTEXT_HYDRATED` / `PLAN_COMPILED` | Planning | No |
| `POLICY_CLEARED` / `POLICY_BLOCKED` / `SECURITY_VIOLATION` | Policy | Blocked ones are terminal |
| `EXECUTION_STARTED` | A step begins | No |
| `EXECUTION_COMPLETED` | A step **completed** (per step, not per task) | No (`ADR-0010`) |
| `EXECUTION_FAILED` | Execution failed | Terminal |
| `CRITIQUE_CONCLUDED` | Validation review done | No |
| `MUTATION_COMMITTED` | Task committed | Terminal |
| `AWAITING_HUMAN_ATTESTATION` | Parks on human gate | No (never auto-clears) |
| `STATE_ROLLBACK_TRIGGERED` | Workspace drift restored | No |
| `RECOVERY_REPLAY_COMPLETED` | Recovery audit | No |

`TERMINAL_EVENTS` (`types.py:132`): `MUTATION_COMMITTED`, `EXECUTION_FAILED`,
`POLICY_BLOCKED`, `SECURITY_VIOLATION`, `HUMAN_GATE_REJECTED`, `TASK_ABANDONED`.

## 4. Status tracking and retries

- Task status = replayed `TaskProjection.phase` (`TaskPhase`, `replay.py:34`).
- `attempts` incremented on each `EXECUTION_STARTED` (`replay.py:219`).
- Per-step retry budget `max_step_retries` (default 2) in
  `ChiefOrchestrator._execute_steps` (`orchestrator.py:523`).
- Queue `max_delivery_attempts` (default 3) governs message redelivery
  (`config.py:349`).
- Recovery `max_attempts` (default 3, from `QueueSettings.max_delivery_attempts`)
  governs re-queue retries; beyond it → `TASK_ABANDONED` (`recovery.py:285`).

## 5. Concurrency controls

- Daemon `claim_batch=4` (`daemon.py:49`).
- Model router semaphore: `max_concurrent_streams=2` (`router.py:150`) — memory
  bound on constrained hardware.
- Delegation graph: `max_depth` (modality `recursion_ceiling`), `max_nodes`
  (`max_plan_nodes()`), cycle detection (`delegation.py:48`).
- Sandbox: per-modality `memory_mb`, `cpu_cores`, `timeout_seconds`; watchdog
  RSS sampling; heartbeat liveness (`sandbox/watchdog.py`).

## 6. Failure handling

| Failure | Handling |
|---|---|
| Daemon loop exception | Logged, loop restarted after 1s (`daemon.py:100`) |
| Task run exception | Logged, message nacked (redelivery) (`daemon.py:134`) |
| Orchestrator `PaaError` | `EXECUTION_FAILED` event, re-project, return error (`orchestrator.py:310`) |
| Sandbox step failure | `VALIDATION_FAILED`; retry up to budget (`orchestrator.py:556`) |
| Recovery single lineage failure | Logged as `UNRECOVERABLE`; sweep continues (`recovery.py:246`) |
| Hash-chain problem | Reported in `RecoveryReport.chain_problems`, never repaired (`recovery.py:231`) |
| Queue enqueue failure in recovery | Logged, non-fatal; ledger already records the task as open (`recovery.py:444`) |

## 7. What is NOT present (honest gaps)

- No separate worker process or worker pool.
- No cron/scheduled jobs beyond the 6h decay sweep.
- No external message broker (Redis optional; SQLite default).
- No webhooks, no ingestion bridges.
- No task priority beyond `QueueMessage.priority` (default 100).
- No exactly-once at the queue layer — exactly-once processing is via ledger
  idempotency keys (`queue/base.py` docstring).
- `tests/observability/` does not exist; `src/paa/observability/` is unexercised
  (`AGENTS.md:97`) — observability loops are not part of the daemon.

## Related documents

- [`modules/daemon.md`](modules/daemon.md) — daemon deep dive.
- [`modules/ledger.md`](modules/ledger.md) — event vocabulary and store.
- [`backend/request-lifecycle.md`](backend/request-lifecycle.md) — the task lifecycle the daemon drives.
- [`data/data-lifecycle.md`](data/data-lifecycle.md) — queue message and signal lifecycles.
- [`operations/configuration-and-environment.md`](operations/configuration-and-environment.md) — `QueueSettings` and daemon intervals.
