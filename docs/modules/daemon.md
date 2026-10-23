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

### Loops (`daemon.py:113`–`:166`)
- **Dispatch** (0.5s interval): `claim_batch=4` messages from
  `StreamName.ORCHESTRATOR_CORE`; runs each via `runtime.run(cid)`; `ack` on
  success, `nack` on failure (`daemon.py:136`). If `runtime.queue is None`, sleeps
  (`daemon.py:121`).
- **Ingestion** (1.0s): `MemoryCreator.run_batch(limit=25)` drains unprocessed
  signals into hot serving; isolates one poison signal from the batch (`daemon.py:150`).
- **Maintenance** (6h): `DecaySweeper(db).sweep()` after one initial interval
  (`daemon.py:158`).

### Supervision (`daemon.py:94`)
`_supervise(name, loop)` restarts on unexpected error after 1s; `CancelledError`
propagates.

### Shutdown (`daemon.py:73`)
`stop()` sets `self._stop`, cancels tasks, awaits drain. `run_forever()` blocks
on the event (`daemon.py:88`).

### Collaborators (`daemon.py:170`)
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
| `Daemon` → `MemoryCreator` | `run_batch()` | Ingest signals | `daemon.py:148` |
| `Daemon` → `DecaySweeper` | `sweep()` | Evict stale facts | `daemon.py:163` |
| `cli.main serve` → `Daemon` | `Daemon(rt)`, `start()`/`stop()` | `paa serve` | `cli/main.py:308` |
| `Runtime.build` → boot recovery | `orchestrator.boot()` | Recovery at boot | `runtime.py:326` |

## Runtime behavior

- Boot recovery is a precondition of serving, not a background chore (`daemon.py:9`).
- Claiming (not just reading) means a crash mid-task leaves the message
  reclaimable (`daemon.py:12`).
- Each loop is supervised independently (`daemon.py:18`).

## Failure behavior

- Loop exception → logged, restarted after 1s (`daemon.py:103`).
- Task run exception → logged, message nacked (redelivery up to
  `max_delivery_attempts`) (`daemon.py:138`).
- Ingestion failure → logged, skipped (`daemon.py:151`).
- Maintenance failure → logged, skipped (`daemon.py:165`).
- Recovery enqueue failure → logged, non-fatal (`recovery.py:444`).

## Modification impact

- Loop intervals are constructor params (`daemon.py:49`–`:51`).
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
