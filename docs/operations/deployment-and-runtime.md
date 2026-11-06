# Deployment and runtime

PAA has two deployment topologies documented and implemented:

1. **Laptop/default** — SQLite WAL + embedded fallbacks (Qdrant embedded,
   Kuzu/SQLite CTE, SQLite queue, local CAS). Idles at ~0MB RAM. No Docker
   required (`README.md:59`).
2. **Docker/server** — Postgres + Qdrant + Redis + S3 via compose; only the
   ingestion API on `127.0.0.1:8787` publishes a port (`docs/deployment/docker.md`,
   `AGENTS.md:132`).

## Docker deployment (verified against docs/deployment/docker.md)

```
cp .env.example .env && docker compose up -d --build
```

- Postgres, Redis, Qdrant and the object store publish **no host ports**.
- gVisor opt-in, Linux-only:
  `docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d`.
- Runtime reports `NAMESPACE` instead of `VM` when `runsc` isn't registered
  (`AGENTS.md:135`).

The Docker topology is **built** but, per the README build-status table, the
live-server run still needs a pass (parity + backend verification pending).

## Runtime startup

Programmatic: `Runtime.build()` (`src/paa/runtime.py:260`):
1. `Settings()` → `ensure_directories()` creates all state dirs
   (`config.py:450`).
2. `Database` connects + applies `schema_sqlite.sql`.
3. `LedgerStore(db)`.
4. Model router built (or a `model_adapter` injected); `RouterModelAdapter`
   wraps it.
5. Optional backends: `_try_vector`, `_try_graph`, `_try_queue` — degrade, never
   raise (`runtime.py:394`–`:400`).
6. `_build_agents()` constructs planner, policy, worker, critic, context
   builders, router (`runtime.py:407`).
7. `_acquire_sandbox()` — 5s probe, falls back to subprocess (`runtime.py:382`).
8. `ChiefOrchestrator` assembled with all collaborators (`runtime.py:302`).
9. `boot_recovery()` runs `RecoveryEngine.boot_sweep()` before accepting work
   (`runtime.py:331`).

CLI one-shot commands build a `Runtime`, do one thing, print, and close
(`cli/main.py:53`). `paa serve` builds one `Runtime`, starts the `Daemon` and
FastAPI app over it (`cli/main.py:254`).

## Daemon loops

`Daemon(runtime)` (`src/paa/daemon.py:48`):
- Boot recovery first, before any new work is accepted (`src/paa/daemon.py:72`).
- Dispatch loop (0.5s): claims `StreamName.ORCHESTRATOR_CORE` messages, runs via
  `runtime.run(cid)`, `ack`/`nack` (`src/paa/daemon.py:136`).
- Ingestion loop (1.0s): `MemoryCreator.run_batch(limit=25)` drains signals
  into hot serving (`src/paa/daemon.py:156`). After each batch it also drains
  the pending-embedding backlog (`src/paa/daemon.py:165`).
- Maintenance loop (6h): `MemoryCurator.run_maintenance` — decay, prune-edges,
  consolidate, orphans, reconcile-vectors, analyze (`src/paa/daemon.py:200`,
  `src/paa/memory/curator.py:141`). This is the **runbook home** for the
  curator: first-tick preview, snapshot, and rollback live here (see
  *Memory curation* below); `docs/modules/daemon.md` and
  `docs/modules/memory.md` link to it.
- Supervised: one loop crashing doesn't stop others (`src/paa/daemon.py:102`).
- Cooperative shutdown via `asyncio.Event` (`src/paa/daemon.py:81`).

## Memory curation (authoritative runbook)

The daemon maintenance loop runs the full curator
(`src/paa/daemon.py:200`, `src/paa/memory/curator.py:141`), not a bare decay
sweep. It fires once per `maintenance_interval` (6h default): the loop sleeps
one interval first, so the first tick runs ~6h after boot
(`src/paa/daemon.py:176`).

**First tick is a dry-run preview; the second deletes.** The first tick runs
with `dry_run=True` — counts are logged, nothing is deleted — and only a
clean, complete, error-free preview consumes the flag so the *next* tick
actually deletes (`src/paa/daemon.py:265`,
`src/paa/daemon.py:312`). Watch `daemon.curation_started` and
`daemon.curation_completed(dry_run=True)` for the would-delete counts
(`src/paa/daemon.py:302`, `src/paa/daemon.py:330`). A budget stop or a
phase error keeps the daemon in preview (the safe direction) and logs
`daemon.curation_preview_incomplete` loudly — that is not a stuck daemon,
it is the liveness trap firing because one phase is erroring; fix the phase
rather than disabling preview (`src/paa/daemon.py:321`).

**Snapshot before the first deleting tick.** The curator's delete phases have
no cold-lake summary in production — the daemon wires `archive_writer=None`
(`src/paa/daemon.py:273`), so decay/prune/consolidate/orphan deletions write
no summary of their own. Before the second (deleting) tick, snapshot the
cold lake: copy `PAA_HOME` (`state/paa.db` plus `cold_lake/`), or a
Postgres dump plus the S3 bucket snapshot on the Docker topology. There is
a real window to do this — the first preview tick plus ~6h of cadence —
but only if you know to look.

**Restore is cold-lake replay, not code revert.** Reverting the curator code
changes future behaviour; it does not bring deleted facts back. To restore a
deleted fact, re-run the creator over its persisted signal: facts carry
`source_signal_id` into `cold_lake_signals` (`src/paa/memory/facts.py:295`),
and `MemoryCreator.run_batch` claims `unprocessed` signals
(`src/paa/memory/creator.py:1282`). The operator steps are: set the affected
signal(s) back to `unprocessed`, then `paa memory drain` (or
`POST /memory/drain`, `src/paa/api/app.py:272`). The creator is
deterministic and replay-stable (`src/paa/memory/creator.py:185`), so the
fact is recreated from the same payload; contradiction and quarantine rules
still apply on replay. Code revert restores neither the facts nor the
retention clocks — `use_count`/`last_queried_at` reset on replay.

**Backlog watch (no throttle dial).** `drain_pending_embeddings` moves one
batch of 100 per call (`src/paa/memory/creator.py:1490`) and the daemon
drain-calls back-to-back while signals keep arriving
(`src/paa/daemon.py:165`), so a large backlog drains at embedder speed with
no rate dial — observe `memory.embedding_drain` `pending_count`
(`src/paa/memory/creator.py:1556`) for backlog size and batch/indexed
progress. If ingestion starves under a backlog, the backlog size and the
embedder speed bound the drain time; there is currently no sleep/backoff
knob to turn.

## Process model

- Single process, single user, single machine.
- Asyncio throughout (`asyncio_mode = "auto"` in pytest; `async def` everywhere
  in the runtime).
- No thread pool for CPU-bound work; sandbox subprocesses are awaited via
  `asyncio.create_subprocess_exec`-style patterns.
- The model router semaphore (`max_concurrent_streams=2`) bounds concurrent
  model memory (`models/router.py:226`).

## State location

All state under `PAA_HOME` (default `~/.paa`, outside the repo):
- `state/paa.db` — SQLite (ledger + hot serving + queue + marketplace + improvement)
- `state/qdrant` — embedded Qdrant
- `state/graph` — Kuzu/SQLite graph
- `cold_lake/` — CAS blobs
- `vault/` — markdown world-model documents
- `workspaces/` — sandbox workspaces
- `sessions/` — coder agent JSONL transcripts

## Health / observability

- `paa doctor` reports live backends and sandbox isolation level
  (`cli/main.py:225`).
- `GET /health` returns backend type names and open task count
  (`api/app.py:68`).
- `src/paa/observability/` (logging, metrics, tracing, runs) is built but
  **untested** — exercised nowhere (`AGENTS.md:97`).

## Gaps / unverified

- Docker live-server run still needs a pass (README build-status table).
- No CI config, Makefile, or pre-commit hook (`AGENTS.md:28`).
- No systemd unit, no Kubernetes manifests.
- `PAA_ALLOW_NON_LOOPBACK=1` is the only escape hatch for the loopback API; no
  production-grade TLS/auth story exists (single-user by design).

## Related documents

- [`docs/deployment/docker.md](../deployment/docker.md) — Docker deployment details.
- [`operations/configuration-and-environment.md`](configuration-and-environment.md) — all `PAA_*` settings.
- [`architecture/overview.md`](../architecture/overview.md) — degradation philosophy.
- [`runtime/background-jobs-and-events.md`](../runtime/background-jobs-and-events.md) — daemon loops.
