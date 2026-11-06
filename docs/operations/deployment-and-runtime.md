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
   raise (`runtime.py:392`–`:398`).
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

`Daemon(runtime)` (`src/paa/daemon.py:44`):
- Boot recovery first, before any new work is accepted (`daemon.py:64`).
- Dispatch loop (0.5s): claims `StreamName.ORCHESTRATOR_CORE` messages, runs via
  `runtime.run(cid)`, `ack`/`nack` (`daemon.py:116`).
- Ingestion loop (1.0s): `MemoryCreator.run_batch(limit=25)` drains signals
  into hot serving (`daemon.py:139`).
- Maintenance loop (6h): `DecaySweeper(db).sweep()` evicts stale facts
  (`daemon.py:151`).
- Supervised: one loop crashing doesn't stop others (`daemon.py:94`).
- Cooperative shutdown via `asyncio.Event` (`daemon.py:70`).

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
