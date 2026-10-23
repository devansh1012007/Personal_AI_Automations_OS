# Backend overview

PAA's "backend" is not a separate service — it is the in-process cognitive
runtime exposed through a CLI, a loopback FastAPI ingestion API, and a programmatic
`Runtime`. There is no separate backend/frontend split in the traditional sense:
the CLI and API are both thin edges over the same `Runtime` object
(`src/paa/runtime.py:140`).

## Backend entry points

| Entry point | File | What it does |
|---|---|---|
| `paa submit "goal"` | `src/paa/cli/main.py:73` | Builds a `Runtime`, submits a task, optionally runs it to completion, then closes |
| `paa run <id>` | `src/paa/cli/main.py:89` | Drives an already-submitted task to a terminal state |
| `paa status <id>` | `src/paa/cli/main.py:118` | Shows the replayed projection of a task |
| `paa ledger <id>` | `src/paa/cli/main.py:134` | Walks a task's event chain (explainability) |
| `paa gate <id> --approve/--reject` | `src/paa/cli/main.py:164` | Clears a task parked on a human gate |
| `paa recover` | `src/paa/cli/main.py:190` | Runs the post-crash boot sweep |
| `paa doctor` | `src/paa/cli/main.py:224` | Reports which backends/sandbox are actually live |
| `paa serve [host] [port]` | `src/paa/cli/main.py:248` | Starts FastAPI API + background daemon over one shared runtime |
| `paa agent "prompt"` | `src/paa/cli/coder.py:44` | Tool-using coder loop over the runtime |
| `paa tui` | `src/paa/cli/main.py:307` | Interactive dashboard |
| `Runtime.build()` | `src/paa/runtime.py:251` | Programmatic composition root (used by all edges) |

## Backend module responsibilities

| Module | Responsibility | Layer |
|---|---|---|
| `src/paa/runtime.py` | Composition root; wires DB, ledger, storage, model router, agents, orchestrator | Composition |
| `src/paa/agents/orchestrator.py` | Chief Orchestrator — drives the task lifecycle and writes every ledger event | Control |
| `src/paa/agents/base.py` | Agent base classes, message protocol, delegation entry point | Control |
| `src/paa/agents/reasoning.py` | Planner, critic, router, worker cell — the model-using agents | Control / reasoning |
| `src/paa/agents/policy.py` | Deterministic risk gate (no model consulted) | Control / security |
| `src/paa/agents/delegation.py` | Delegation graph with depth/cycle/node bounds | Control / safety |
| `src/paa/agents/context_agents.py` | Planner and worker context builders | Control / context |
| `src/paa/ledger/store.py` | Append-only event store with idempotency, hash chain, head projection | Data (spine) |
| `src/paa/ledger/replay.py` | Deterministic event replay into `TaskProjection` | Data (spine) |
| `src/paa/ledger/recovery.py` | Post-crash boot sweep and workspace reconciliation | Data (recovery) |
| `src/paa/validation/engine.py` | Deterministic validation (schema → AST → patch → drift → tests) | Security / validation |
| `src/paa/models/router.py` | Escalating model router (local → frontier) | Integration |
| `src/paa/storage/relational/database.py` | Async SQLite access layer | Data |

The backend is **single-process, single-user, single-machine**. There is no
authentication, no multi-tenancy, no distributed consensus. `api_host` is
enforced loopback unless `PAA_ALLOW_NON_LOOPBACK=1`
(`config.py:409`).

## Request flow into the backend

There is no HTTP "business" API surface beyond ingestion. The backend is driven
by:

1. **CLI one-shot commands** — each builds a `Runtime` (`Runtime.build()`),
   performs one operation, prints, and closes (`cli/main.py:51`).
2. **`paa serve`** — builds one `Runtime`, starts a `Daemon` and a FastAPI app
   over it (`cli/main.py:254`). The API delegates to the same `Runtime`.
3. **Programmatic `Runtime`** — `async with await Runtime.build() as rt:
   cid = await rt.submit(...); outcome = await rt.run(cid)`
   (`runtime.py:144`).

All three converge on `ChiefOrchestrator` (`agents/orchestrator.py:188`).

## What the backend does NOT do (honest gaps)

- No user authentication or authorization beyond the single permission mode
  (`PermissionMode`, `core/types.py:169`).
- No multi-user or multi-tenancy.
- No built-in concrete tools (file edit, bash, web, git) wired into the
  orchestrator's main task path (`README.md:160`).
- No web dashboard or streaming terminal output (`README.md:161`).
- Observability (metrics, tracing, runs, logging) is built but untested
  (`AGENTS.md:97`).

## Related documents

- [`backend/modules-and-services.md`](modules-and-services.md) — each backend module's internals, inputs, outputs, and consumers.
- [`backend/request-lifecycle.md`](request-lifecycle.md) — the submit→run execution path traced step by step.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — how backend modules connect to storage, model, and sandbox.
- [`modules/ledger.md`](../modules/ledger.md) — the ledger spine.
