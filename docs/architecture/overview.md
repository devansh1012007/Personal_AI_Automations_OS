# Architecture overview

PAA (Personal Autonomous Cognitive Operating System Runtime) is a local-first,
event-sourced substrate for running autonomous agents on personal hardware.
Implemented in Python (≥3.11) with a `src`-layout under `src/paa/`, roughly
28,000 lines with ~755 tests.

This document explains the architecture as it is actually implemented — not as
a README or an RFC might describe it. Where the implementation deliberately
deviates from the RFC, the deviation is cited (`docs/adr/` or a `SPEC DEVIATION`
note in source).

## The one invariant: the ledger is the truth

The append-only, hash-chained event ledger (SQLite WAL by default) is the **only
source of truth**. Every state transition is recorded as a ledger event *before*
the side effect it describes. Redis, queues, vector/graph indexes, and the
filesystem are all **derived views** rebuilt by replay (`src/paa/ledger/replay.py`).

Two rules follow and are not negotiable (`AGENTS.md`):

1. **Ledger event before side effect, always.** A crash between the event and
   the work is recoverable; replay knows the work was intended. A crash between
   the work and the event is not recoverable. The system chooses the survivable
   failure mode.
2. **Everything else must be replayable.** Any state not derivable from events
   needs a snapshot *and* a recovery rule (`src/paa/ledger/recovery.py`).

The ledger layer (`src/paa/ledger/`):

| Module | Responsibility |
|---|---|
| `events.py` | Event envelope, idempotency key, canonical JSON hashing, tamper-evident hash chain |
| `store.py` | Append-only store: idempotency suppression, per-correlation `state_version`, hash-chain linking, head projection, snapshots, chain verification |
| `replay.py` | Deterministic left-fold projection of events into a `TaskProjection`; pure and total (never raises on unknown events) |
| `recovery.py` | Post-crash boot sweep: replay open lineages, reconcile workspace vs manifest, restore drift, re-queue resumable tasks |

See [`modules/ledger.md`](../modules/ledger.md).

## High-level data flow

```
ingestion ─────────► cold_lake (immutable CAS blobs)
                          │
                          ▼
              system_state.ledger  ◄── SOURCE OF TRUTH
              append-only, hash-chain
                          │
                    replay (left fold)
                          │
                    TaskProjection
                          │
      ┌─────────────┬─────┼─────┬───────────────┬──────────────┐
      ▼             ▼     ▼     ▼               ▼              ▼
 hot_serving   vector index  graph edges     queue         workspaces
 (truth)       (recall)      (provenance)    (volatile)      (host disk)
```

This diagram is reproduced from `README.md:94` and reflects the actual
substrate topology. `hot_serving` is the relational mutable read path; the
other three are derived/caches that degrade when their optional extras are
absent.

## Core architectural patterns

### 1. Composition root with injected dependencies

`src/paa/runtime.py::Runtime` is the single place the whole runtime is wired
together. `Runtime.build()` constructs the ledger, storage substrates, model
router, agent hierarchy, and orchestrator, injecting each collaborator. Every
collaborator is overridable, so tests swap real Qdrant/Kuzu/Redis for fakes
without touching wiring.

```text
Runtime.build()
  ├─ Database (SQLite WAL)
  ├─ LedgerStore
  ├─ model router (EscalatingModelRouter) → RouterModelAdapter → ModelLike
  ├─ storage substrates (vector, graph, queue) — degrade, never crash
  ├─ agents (planner, policy, worker, critic, context builders, router)
  ├─ RecoveryEngine
  └─ ChiefOrchestrator
```

`src/paa/runtime.py:251`.

### 2. Optional backends degrade; they never raise

Heavy dependencies (qdrant, kuzu, sentence-transformers, redis, psycopg, boto3)
are optional extras and are **never imported at module scope in a core path**.
Each is attempted inside `_try_vector` / `_try_graph` / `_try_queue` /
`_acquire_sandbox`; on `ImportError` or connection failure it is replaced by an
in-process fallback with a logged warning. The runtime must boot on a laptop
with only core dependencies installed.

`src/paa/runtime.py:358`, `:371`, `:382`, `:392`.

### 3. No generative reasoning in the control path

Routing and gating decisions are made by Python, never by a model. A model
deciding "should this task proceed?" can be prompt-injected into proceeding.
LLMs are used for *content* (planning, critique) and never for *control*.

`src/paa/agents/orchestrator.py:9`, `src/paa/agents/policy.py:7`.

### 4. Deterministic validation outside the model loop

The critic runs deterministic validation first; the model's verdict can only
downgrade, never upgrade, a failing check. `StructuredOutputError` and AST
rejections are authoritative. "No model sits in the security loop."

`src/paa/agents/reasoning.py:194`, `src/paa/validation/engine.py:10`.

### 5. Pluggable sandbox with honest containment

`IsolationLevel` (NONE/PROCESS/NAMESPACE/VM) is declared per-backend, never
aspirational. gVisor (`runsc`, VM-level) is unavailable on the target Windows
machine, so the default is a workspace jail + Job Object caps + AST pre-scan,
documented as materially weaker than gVisor. A caller can branch on
`isolation_level.is_real_containment`.

`src/paa/sandbox/base.py:54`, `docs/adr/0006`.

### 6. Provider-agnostic model layer with explicit escalation

Local model first; frontier model on failure when policy permits. Every
escalation writes a `MODEL_ESCALATED` ledger event *before* the frontier call,
so the privacy boundary is auditable. The model seam is the narrow
`ModelLike.complete_structured(prompt, schema)` protocol; `RouterModelAdapter`
is the only place that knows about `EscalatingModelRouter`.

`src/paa/models/router.py:123`, `src/paa/runtime.py:98`.

## The task lifecycle (every arrow writes a ledger event first)

```
TASK_REQUESTED → TASK_QUEUED → CONTEXT_HYDRATED → PLAN_COMPILED
  → POLICY_CLEARED → EXECUTION_STARTED → CRITIQUE_CONCLUDED
  → EXECUTION_COMPLETED → MUTATION_COMMITTED
                │
                ├── POLICY_BLOCKED / SECURITY_VIOLATION  (terminal)
                ├── AWAITING_HUMAN_ATTESTATION           (parks, never auto-clears)
                └── STATE_ROLLBACK_TRIGGERED             (drift → restore)
```

`README.md:114`. `EXECUTION_COMPLETED` fires **per step**, not per task, and is
not terminal (`docs/adr/0010`).

Orchestrator phases (`src/paa/agents/orchestrator.py:292`):

1. `run` — project the lineage; if terminal, return immediately.
2. `_hydrate` — build the context packet (planner context builder); may park on
   `AWAITING_HUMAN_ATTESTATION` if density is insufficient.
3. `_plan` — compile execution steps (planner); SIMPLE bypasses the model.
4. `_check_policy` — deterministic risk gate; may block or park on human gate.
5. `_execute_steps` — per step: `EXECUTION_STARTED` → worker runs in sandbox →
   critic reviews → `EXECUTION_COMPLETED` or `VALIDATION_FAILED` (retry up to
   `max_step_retries`).
6. `_commit` — `MUTATION_COMMITTED` (terminal).

`src/paa/agents/orchestrator.py:467`–`:811`.

## Delegation safety

Agents may delegate to one another, but only through the orchestrator's
`DelegationGraph` per correlation, which enforces depth, cycle, and node
bounds before the callee runs. The wait graph is a DAG, so no cycle of
waiters can exist — the RFC's safety property is preserved by construction,
not by prohibiting peer calls.

`src/paa/agents/base.py:22`, `src/paa/agents/delegation.py:48`,
`src/paa/agents/orchestrator.py:833`.

## Configuration philosophy

- `PAA_HOME` roots all state; default `~/.paa`, deliberately outside the repo.
- Settings are pydantic-settings with prefix `PAA_` and nested delimiter `__`
  (e.g. `PAA_STORAGE__POSTGRES_DSN`); `.env` is read from the current working
  directory.
- Provider API keys are **not** `Settings` fields; each registry row names its
  env var (`api_key_env`), resolved via `dotenv_value` without mutating
  `os.environ`.
- `get_settings()` is `lru_cache`d; callers changing `PAA_HOME` must call
  `reset_settings_cache()`.
- `api_host` is enforced loopback unless `PAA_ALLOW_NON_LOOPBACK=1`.

`src/paa/config.py:1`, `:381`, `:460`, `:489`.

## Known deviations from the RFC (selected)

These are deliberate, documented, and cited in source:

| Deviation | Where | Rationale |
|---|---|---|
| Embedded SQLite/Qdrant/Kuzu instead of Postgres/Redis/S3 | ADR-0001–0005, `config.py:58` | Target machine has ~3.5 GB free RAM, no Docker |
| gVisor unavailable; pluggable sandbox | ADR-0006, `config.py:281` | Windows target, no runsc |
| Provider-agnostic models; explicit escalation | ADR-0007/0015, `config.py:476` | vLLM needs CUDA + 8.5 GB VRAM |
| Idempotency key includes `attempt` + `discriminator` | ADR-0008, `events.py:68` | Retries must be possible |
| Head projection replaces partial recovery index | ADR-0010, `ledger/store.py:48` | Partial index never shrinks |
| `EXECUTION_COMPLETED` is per-step, not terminal | ADR-0010, `core/types.py:187` | Treating it as terminal skips remaining steps |
| Optional router; bypassed when a target agent is named | ADR-0011, `reasoning.py:303` | Explicit user direction |
| Peak RSS, not ∫Memory·dt | ADR-0009, `config.py:338` | The RFC's rule is dimensionally wrong |
| 18 memory domains over 6 mechanisms | ADR-0014, `memory/domains.py:78` | Most RFC rows differ only by label |

Full ADR list that exists on disk: `0008`, `0010`, `0015`, `0019`, `0020`.
`docs/adr/README.md` claims 0001–0020 but those files are absent
(`AGENTS.md:323`).

## Module boundary summary (dependency direction)

```
core/types, core/errors   ← imported everywhere, depend on nothing
config.py                 ← reads core types; nothing in paa imports settings
  down to storage.relational.database
ledger/                   ← storage.relational.database; core types/errors
agents/                   ← ledger, core, storage (db), context, memory, models
context/                  ← storage (db), memory, models (embedder)
memory/                   ← storage (db), context (budget), models (embedder)
models/                   ← core errors; config (for timeouts/retries); httpx
sandbox/                  ← core errors/types; validation (ast_scanner)
validation/               ← sandbox, core errors
coder/                    ← runtime, agents (base), models (router), validation
skills/                   ← validation, sandbox, storage (db), core types
marketplace/              ← core errors, skills contracts
observability/            ← storage (relational database)
daemon.py                 ← runtime, memory (creator, decay)
api/                      ← runtime
cli/                      ← runtime, coder, config
```

Nothing in `core/` imports anything else in `paa` — it is the true foundation.
`storage/relational/database.py` is imported by nearly every subsystem that
persists state, making it a high-coupling point (but it is a thin async SQLite
access layer, not business logic).

## What is NOT in the architecture (honest gaps)

- No UI beyond CLI; a TUI exists (`src/paa/tui/`) but there is no web dashboard
  and no streaming terminal output (`README.md:161`).
- No built-in concrete tools for real work (no file edit, bash, web search,
  git) wired into the orchestrator yet (`README.md:160`).
- Observability code exists but is untested (`AGENTS.md:97`).
- `src/paa/agents/specialists/` is an empty package (`AGENTS.md:146`).
- No auth or multi-user; single-user, single-machine (`README.md:175`).

## Related documents

- [`architecture/repository-map.md`](repository-map.md) — directory
  and module map.
- [`architecture/component-interactions.md`](component-interactions.md)
  — verified connections between subsystems.
- [`modules/ledger.md`](../modules/ledger.md) — ledger deep dive.
- [`modules/agents.md`](../modules/agents.md) — agent hierarchy deep dive.
- [`modules/models.md`](../modules/models.md) — model layer deep dive.
- [`modules/storage.md`](../modules/storage.md) — storage substrates deep dive.
- [`docs/adr/README.md`](../adr/README.md) — design reasoning and RFC deviations.
- [`README.md`](../../README.md) — project README, build status, layout.
