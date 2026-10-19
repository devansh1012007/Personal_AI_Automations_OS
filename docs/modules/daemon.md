# Module: Daemon (`src/paa/daemon.py`)

## Module identity

- **Name:** Daemon
- **Path:** `src/paa/daemon.py`
- **Main responsibility:** The background runtime — boot recovery plus three supervised loops (dispatch, ingestion, maintenance) over one shared `Runtime`.
- **Architectural layer:** Runtime / background
- **Entrypoints:** `Daemon.start()`, `Daemon.stop()`, `Daemon.run_forever()`
- **Public interfaces:** `Daemon`
- **Other modules that depend on it:** `cli/main.py` (`paa serve`), `runtime.py` (`Runtime.boot_recovery`)

## Purpose and responsibilities

Makes the runtime *ambient* rather than one-shot (`daemon.py` module docstring).
Boot recovery runs first, before any new work is accepted (RFC §7). Loops are
independently supervised; one failing loop does not stop the others. Shutdown is
cooperative via an `asyncio.Event`.

## Internal implementation

### Loops (`daemon.py:110`–`:163`)
- **Dispatch** (0.5s interval): `claim_batch=4` messages from
  `StreamName.ORCHESTRATOR_CORE`; runs each via `runtime.run(cid)`; `ack` on
  success, `nack` on failure (`:116`). If `runtime.queue is None`, sleeps
  (`:118`).
- **Ingestion** (1.0s): `MemoryCreator.run_batch(limit=25)` drains unprocessed
  signals into hot serving; isolates one poison signal from the batch (`:139`).
- **Maintenance** (6h): `DecaySweeper(db).sweep()` after one initial interval
  (`:151`).

### Supervision (`:91`)
`_supervise(name, loop)` restarts on unexpected error after 1s; `CancelledError`
propagates.

### Shutdown (`:70`)
`stop()` sets `self._stop`, cancels tasks, awaits drain. `run_forever()` blocks
on the event (`:81`).

### Collaborators (`:167`)
`_memory_creator()` builds `MemoryCreator(self._rt.db)`; unavailable degrades
to `None` (ingestion skipped).

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `Daemon.start()` | Runtime | — | runs boot recovery; launches 3 loops |
| `_dispatch_loop()` | — | claimed count | runs tasks via `Runtime.run()` |
| `_ingestion_loop()` | — | processed count | creates facts/entities from signals |
| `_maintenance_loop()` | — | — | evicts stale facts |
| `Daemon.stop()` | — | — | cancels loops, awaits drain |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `Daemon` → `Runtime` | `boot_recovery()`, `run()`, `queue`, `agents` | Drive the runtime | `daemon.py:42` |
| `Daemon` → `MemoryCreator` | `run_batch()` | Ingest signals | `daemon.py:167` |
| `Daemon` → `DecaySweeper` | `sweep()` | Evict stale facts | `daemon.py:160` |
| `cli.main serve` → `Daemon` | `Daemon(rt)`, `start()`/`stop()` | `paa serve` | `cli/main.py:263` |
| `Runtime.build` → boot recovery | `orchestrator.boot()` | Recovery at boot | `runtime.py:331` |

## Runtime behavior

- Boot recovery is a precondition of serving, not a background chore (`:9`).
- Claiming (not just reading) means a crash mid-task leaves the message
  reclaimable (`:12`).
- Each loop is supervised independently (`:18`).

## Failure behavior

- Loop exception → logged, restarted after 1s (`:100`).
- Task run exception → logged, message nacked (redelivery up to
  `max_delivery_attempts`) (`:134`).
- Ingestion failure → logged, skipped (`:147`).
- Maintenance failure → logged, skipped (`:162`).
- Recovery enqueue failure → logged, non-fatal (`recovery.py:444`).

## Modification impact

- Loop intervals are constructor params (`:46`–`:49`).
- Adding a loop means adding to `start()`'s task list and `stop()` handling.
- Tests: daemon exercised via `tests/integration/` (16 tests per README).

## Known gaps

- No dedicated daemon test suite beyond integration.
- No observable loop health endpoints beyond `/health`.

## Related documents

- [`runtime/background-jobs-and-events.md`](../runtime/background-jobs-and-events.md) — daemon loops and recovery.
- [`modules/ledger.md`](ledger.md) — recovery sweep.
- [`modules/memory.md`](memory.md) — MemoryCreator, DecaySweeper.
- [`operations/deployment-and-runtime.md`](../operations/deployment-and-runtime.md) — `paa serve`.
