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

### Loops (`src/paa/daemon.py:119`–`:182`)
- **Dispatch** (0.5s interval): `claim_batch=4` messages from
  `StreamName.ORCHESTRATOR_CORE`; runs each via `runtime.run(cid)`; `ack` on
  success, `nack` on failure (`src/paa/daemon.py:146`). If `runtime.queue is None`, sleeps
  (`src/paa/daemon.py:121`).
- **Ingestion** (1.0s): `MemoryCreator.run_batch(limit=25)` drains unprocessed
  signals into hot serving (`src/paa/daemon.py:156`); also drains the
  pending-embedding backlog (`src/paa/daemon.py:165`).
- **Maintenance** (6h): `MemoryCurator.run_maintenance` — decay, prune-edges,
  consolidate, orphans, reconcile-vectors, analyze — after one initial
  interval (`src/paa/daemon.py:200`). The first tick is a dry-run preview;
  only a clean complete preview consumes the flag so the next tick deletes
  (`src/paa/daemon.py:356`, `src/paa/daemon.py:403`). Curator construction
  failure falls back to a bare `DecaySweeper.sweep()` honouring the same
  preview discipline (`src/paa/daemon.py:286`). The operator runbook —
  first-tick preview, snapshot-before-delete, cold-lake replay restore —
  lives in `docs/operations/deployment-and-runtime.md` (*Memory curation*).

### Supervision (`src/paa/daemon.py:102`)
`_supervise(name, loop)` restarts on unexpected error after 1s; `CancelledError`
propagates.

### Shutdown (`src/paa/daemon.py:81`)
`stop()` sets `self._stop`, cancels tasks, awaits drain. `run_forever()` blocks
on the event (`src/paa/daemon.py:92`).

### Collaborators (`src/paa/daemon.py:186`)
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
| `Daemon` → `Runtime` | `boot_recovery()`, `run()`, `queue`, `agents` | Drive the runtime | `src/paa/daemon.py:48` |
| `Daemon` → `MemoryCreator` | `run_batch()` | Ingest signals | `src/paa/daemon.py:156` |
| `Daemon` → `MemoryCurator` | `run_maintenance()` | Curate memory (decay, prune, consolidate, orphans, reconcile, analyze) | `src/paa/daemon.py:200` |
| `cli.main serve` → `Daemon` | `Daemon(rt)`, `start()`/`stop()` | `paa serve` | `cli/main.py:311` |
| `Runtime.build` → boot recovery | `orchestrator.boot()` | Recovery at boot | `runtime.py:326` |

## Runtime behavior

- Boot recovery is a precondition of serving, not a background chore (`src/paa/daemon.py:6`).
- Claiming (not just reading) means a crash mid-task leaves the message
  reclaimable (`src/paa/daemon.py:10`).
- Each loop is supervised independently (`src/paa/daemon.py:17`).

## Failure behavior

- Loop exception → logged, restarted after 1s (`src/paa/daemon.py:121`).
- Task run exception → logged, message nacked (redelivery up to
  `max_delivery_attempts`) (`src/paa/daemon.py:146`).
- Ingestion failure → logged, skipped (`src/paa/daemon.py:169`).
- Maintenance failure → logged, skipped (`src/paa/daemon.py:181`).
- Recovery enqueue failure → logged, non-fatal (`recovery.py:444`).

## Modification impact

- Loop intervals are constructor params (`src/paa/daemon.py:52`–`:55`).
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
