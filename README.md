# PAA — Personal Autonomous Cognitive Operating System Runtime

A local-first, event-sourced substrate for running autonomous agents on personal
hardware. Built from the v4.0/v4.1 RFC, adapted where the specification could not
run on the target machine or could not work as written.

**Status:** ~28,000 lines, **755 tests passing**. The event-sourced core, context
engine, storage layer and sandbox are complete and tested. Some subsystems are
written but untested — see [Build status](#build-status), which is deliberately
honest about which is which.

---

## What this is

Most agent frameworks keep task state in process memory. Kill the process and the
task is gone. PAA's premise, taken from the RFC, is that **the ledger is the only
truth** — everything else (queues, caches, even the filesystem) is a derived view
that can be rebuilt by replaying events.

That single decision buys the properties that matter:

| Property | How |
|---|---|
| **Crash recovery** | Boot sweep replays the ledger, reconciles the workspace against a checksum manifest, rolls back drift, re-queues what is safe |
| **Tamper evidence** | Each event commits to its predecessor's digest; editing history invalidates every digest after it |
| **Bounded context** | Hard token ceiling enforced by the gatherer, with a property test asserting it is unbreakable |
| **Deterministic safety** | AST scan, schema validation and real test runs gate every mutation — no model sits in the security loop |
| **Explainability** | Causation links let you walk backwards from any action to the event that caused it |

---

## Quick start

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest tests/ -q      # 1138 passed, 6 skipped
```

Drive it from the command line (with a local model via Ollama, or `echo` offline):

```bash
paa doctor                       # what backends/sandbox are actually live
paa submit "summarise my notes"  # run a task to a terminal state
paa ledger <correlation-id>      # walk the event chain — "why did it do that?"
paa recover                      # post-crash boot sweep
paa serve                        # FastAPI ingestion + background daemon (loopback)
```

Optional extras, each with a working fallback if you skip it:

```bash
uv pip install -e ".[vector]"       # Qdrant embedded (else: numpy brute force)
uv pip install -e ".[graph]"        # KuzuDB        (else: SQLite recursive CTEs)
uv pip install -e ".[embeddings]"   # real embeddings (else: hash embedder; ~2GB torch)
uv pip install -e ".[api]"          # FastAPI ingestion
```

Nothing requires Docker, a server, or a GPU.

---

## Reality check: this machine vs. the RFC

The RFC assumes a CUDA workstation. Your README says *"for potato laptop and PC."*
Those are different machines, and the gap had to be resolved rather than papered
over. Verified on this hardware (Ryzen 5 7535HS, 13.3 GB RAM / ~3.5 GB free,
Radeon 660M iGPU with 2 GB VRAM, Windows 11, no Docker):

| RFC specifies | Reality | What PAA does instead |
|---|---|---|
| vLLM + Llama-3-8B-Q8 at 85% VRAM | vLLM ships **Linux-only wheels**; needs CUDA/ROCm; 2 GB VRAM vs ~8.5 GB needed | Provider-agnostic layer, local-first with logged escalation (ADR-0007, ADR-0015) |
| Concurrent Mistral-7B critic + 8B curator | Would need ~20 GB more | One configurable provider; the critic's authority is deterministic, not generative |
| gVisor `runsc` sandbox | Linux user-space kernel; does not exist on Windows | Pluggable `Sandbox`; Windows backend uses Job Object caps + workspace jail + AST pre-scan. **Materially weaker — stated plainly, not disguised** (ADR-0006) |
| PostgreSQL + Qdrant + Redis + MinIO | No Docker; ~4–6 GB RAM idle | SQLite (WAL), Qdrant embedded, SQLite queue, content-addressed blob store — ~0 MB idle (ADR-0001–0005) |
| KuzuDB embedded | `win_amd64` wheel exists ✓ | **Used as specified** |
| 384-dim embeddings | MiniLM-L6-v2 is 384-dim, CPU ✓ | **Used as specified** |

Every substitution keeps the RFC's *logical* contract. Moving back to Postgres or
gVisor is a backend swap, not a rewrite — that is what the ADRs are for.

---

## Bugs found in the specification

Implementing the RFC literally would produce a system that deadlocks, leaks
memory, or silently loses work. Each of these is fixed, with the reasoning
recorded in `docs/adr/`.

**1. The idempotency scheme makes retries impossible** — ADR-0008
`SHA256(correlation_id + event_type + state_version)` under a UNIQUE constraint
means `EXECUTION_STARTED` can be written at most once per task, ever. A task whose
first attempt failed can never emit a second — so **recovery deadlocks on exactly
the tasks it exists to rescue**. Fixed with an attempt counter and a discriminator.

**2. The recovery index grows forever** — ADR-0010
A partial index excluding terminal events never sheds rows on an append-only
table, so every event any completed task emitted stays indexed permanently. Boot
recovery would get slower for the life of the system. Replaced with a bounded head
projection: one row per lineage.

**3. `EXECUTION_COMPLETED` cannot be terminal** — ADR-0010
It fires once *per step*. Treating it as closing the task makes recovery mark a
half-finished plan as done and **silently skip every remaining step**. Caught by a
test, not by inspection.

**4. The sandbox kill rule has wrong dimensions** — ADR-0009
`∫MemoryUsage(t)dt > Ceiling` yields MB·seconds. It never fires on a genuine spike
and always fires on a long benign run. Replaced with peak RSS, which is what
cgroups and Job Objects actually measure.

**5. `A_score` divides by numbers that can be zero** — ADR-0012
Dividing by `pollution · hallucination_risk`, both in [0,1], sends the score to
infinity as either approaches zero. Rewritten with `(1 + x)` denominators — still
monotonically correct, now bounded.

**6. Temporal decay applies to the wrong term** — ADR-0013
Operator precedence in `R_hybrid` decays only the graph term, leaving stale
semantic matches undamped. Applied to the whole score.

**7. Friction score divides by zero for the worst domains** — ADR-0016
`F_ops` divides by "Total Successful Commits", which is zero for a domain that has
only ever failed — precisely the domain most in need of reflection.

**8. Pydantic v1 syntax** — the RFC's `SkillContractModel` uses `regex=` and
`@validator`, which do not run on Pydantic v2.

Four more were found *by tests* during the build, in code written here:

- **Keyset vs OFFSET pagination** — deleting while paginating with OFFSET silently
  skips rows; those facts would never be evicted, and nothing would report it.
- **Recovery-sweep idempotency** — keying the audit event on a version the event
  itself bumps grows the ledger by one row per boot, forever.
- **zstd size sentinel** — `stream_writer(size=None)` raises; the library wants
  `-1`, and a declared-size mismatch fails inside the write, making the explicit
  check below it unreachable.
- **Non-deterministic claim order** — tiebreaking "oldest first" on a random UUID
  shuffles same-instant arrivals, so ingestion order quietly stops being true
  under load, which is exactly when it matters.

---

## Architecture

```
                        ┌──────────────────────────┐
   ingestion ──────────►│  cold_lake (immutable)   │  raw signals, CAS blobs
                        └────────────┬─────────────┘
                                     │
                        ┌────────────▼─────────────┐
                        │  system_state.ledger     │  ◄── SOURCE OF TRUTH
                        │  append-only, hash-chain │      everything else derives
                        └────────────┬─────────────┘
                                     │ replay (left fold)
                        ┌────────────▼─────────────┐
                        │   TaskProjection         │
                        └────────────┬─────────────┘
                                     │
   ┌─────────────┬───────────────────┼───────────────┬──────────────┐
   ▼             ▼                   ▼               ▼              ▼
hot_serving   vector index      graph edges      queue          workspaces
(truth)       (recall)          (provenance)     (volatile)     (host disk)
```

Task lifecycle, every arrow writing a ledger event first:

```
TASK_REQUESTED → TASK_QUEUED → CONTEXT_HYDRATED → PLAN_COMPILED
      → POLICY_CLEARED → EXECUTION_STARTED → CRITIQUE_CONCLUDED
      → EXECUTION_COMPLETED → MUTATION_COMMITTED
                    │
                    ├── POLICY_BLOCKED / SECURITY_VIOLATION  (terminal)
                    ├── AWAITING_HUMAN_ATTESTATION           (parks; never auto-clears)
                    └── STATE_ROLLBACK_TRIGGERED             (drift → restore)
```

**Ledger before side effect, always.** A crash between the event and the work is
recoverable — replay knows the work was intended. A crash between the work and the
event is not. The ordering makes the survivable failure the only possible one.

---

## Design decisions worth knowing

### The router is optional

You asked why a router is needed at all. Mostly it isn't, so `should_route()`
returns `False` when you name a target agent, or when fewer than 3 agents are
eligible. When it does run it uses an LLM, not a small classifier — your reasoning
was right: a compact intent model sees the request string but not the project
state, so it cannot distinguish "fix the login bug" (one file) from the same words
meaning an auth rewrite.

### Agents can call each other, without deadlock

The RFC forbids peer calls; you asked for them. Both are satisfiable, because the
RFC's justification is about *blocking* call graphs, not interaction. Delegation is
mediated by the orchestrator, which owns the delegation graph and refuses any edge
that would close a cycle. Real multi-agent interaction, with the safety property
preserved by construction rather than by prohibition. (ADR-0018)

### The context builder uses AI — for half the job

AI proposes **what to look for** (query expansion, slot inference). Deterministic
code decides **what gets in** (ranking, ceiling, routing). If a model chose packet
contents, text retrieved from an untrusted source could argue its way into the
planner's context, and packets would stop being reproducible — breaking replay.

### 18 memory domains, 6 mechanisms

The RFC's table lists 18 rows, but most differ only by label — "Skill Memory" and
"Procedural Memory" are the same table with the same policy. All 18 names are kept
as vocabulary; each maps to one of six real mechanisms (volatile, relational,
immutable log, vector, graph, document). Adding a domain is a data change, not a
new subsystem. (ADR-0014)

### Meta-learning: what is real and what isn't

You asked about MAML, prototypical networks, and memory-augmented nets. Honestly:
gradient-based MAML needs a differentiable model, a labelled task distribution with
support/query splits, and training infrastructure this runtime does not have.
Shipping a module named `maml.py` that does not do MAML would be worse than not
shipping it.

**Prototypical few-shot classification** is the member of that family that genuinely
fits — prototypes as mean embeddings of past task exemplars, classify by nearest
prototype with a confidence margin, return "unknown" rather than guessing when the
margin is thin. No gradients, no training loop, and it delivers the "adapt from few
examples" property that was actually wanted. (ADR-0017, design recorded; module not
yet built.)

### On matching Claude Code

Worth saying plainly: **architecture cannot close a model-capability gap.** Agentic
coding performance is dominated by the reasoning model. A perfectly engineered
harness around an 8B model produces a well-behaved 8B agent.

What this design *does* beat a stateless coding agent at: crash recoverability,
durable cross-session memory, deterministic validation, and auditability. The honest
framing is **equal or better on recoverability, memory and safety; dependent on the
configured model for raw reasoning** — which is exactly why the model layer is
swappable. (ADR-0015)

---

## Build status

Honest accounting. "Tested" means tests exist and pass; "untested" means the code
imports cleanly but is unverified — treat it as unfinished.

| Subsystem | State | Tests |
|---|---|---|
| Core types, config, permissions | ✅ tested | via ledger suite |
| **Event ledger + replay + recovery** | ✅ tested | **71** |
| Bounded context engine (metrics, budget, gatherer, compaction) | ✅ tested | **353** |
| Storage (vector, graph, cold lake, queue, relational) | ✅ tested | **255** (+5 skipped: Redis) |
| Sandbox (subprocess/WSL/docker/dryrun + recursion guard) | ✅ tested | **39** |
| Memory decay + domains + contradiction | ✅ tested | **37** |
| **Agents (orchestrator, delegation, policy, context, planner, critic, router, worker)** | ✅ tested | **70** |
| **Composition root + end-to-end integration** | ✅ tested | **16** |
| Deterministic validation (AST, patch, workspace, tests) | ⚠️ exercised via agents; no dedicated suite | — |
| Model providers + escalation router + platform registry | ✅ tested (OpenAI/Groq/Gemini/… + localhost) | 71 |
| Memory creator/curator + world model | ✅ tested | 76 |
| Unified Skill Adapter (Claw Hub + MCP) | ✅ tested | 51 |
| Marketplace (signed packages, 5-gate installer) | ✅ tested | 18 |
| Self-improvement + meta-learning | ✅ tested | 22 |
| Claude-Code features (permissions, hooks, loop, sessions) | ✅ tested | 39 |
| **FastAPI ingestion + daemon + CLI** | ✅ tested | 27 |
| Docker deployment + Postgres/MinIO backends | ✅ built (live-server run needs a follow-up pass) | parity + backend |
| Observability (metrics, tracing, runs, logging) | ⚠️ built, untested | — |

**Total: 1138 passing, 6 skipped.**

The runtime is now driven from the command line (`paa submit/status/ledger/
recover/gate/doctor/serve`) and over HTTP (loopback FastAPI ingestion), with a
background daemon that runs boot recovery, drains the task queue, ingests
signals, and runs the decay sweep.

The runtime now **runs end to end**: `Runtime.build()` (in
[`src/paa/runtime.py`](src/paa/runtime.py)) wires the whole system, and
[`tests/integration/`](tests/integration/) proves all five DoD items in one run
— full lifecycle to `MUTATION_COMMITTED`, crash-and-recover, human-gate parking
across a restart, deterministic security blocks, and the token ceiling holding.

The remaining untested subsystems (validation, model providers, observability)
are exercised indirectly through the agent and integration suites but still lack
dedicated coverage; observability is the only one with no exercise at all. The
roadmap ([`docs/ROADMAP.md`](docs/ROADMAP.md)) tracks what's next.

---

## Layout

```
src/paa/
├── core/          types, errors — imported by everything, depends on nothing
├── config.py      every RFC constant, with its section cited
├── ledger/        events, store, replay, recovery      ← the spine
├── storage/
│   ├── relational/  SQLite WAL + schema (Postgres-portable)
│   ├── vector/      Qdrant embedded / numpy fallback
│   ├── graph/       Kuzu / SQLite recursive CTE
│   ├── coldlake/    content-addressed blobs + zstd
│   └── queue/       durable SQLite queue / Redis
├── context/       metrics, budget, gatherer, compaction
├── memory/        domains, decay, contradiction
├── agents/        base, delegation, orchestrator, policy, context, reasoning
├── sandbox/       pluggable containment backends
├── validation/    AST scan, patch apply/rollback, workspace manifests
├── models/        provider abstraction + escalation router
├── skills/        Unified Skill Adapter
└── observability/ metrics, tracing, execution runs
```

---

## Testing

```bash
.venv/Scripts/python.exe -m pytest tests/ -q                 # everything
.venv/Scripts/python.exe -m pytest tests/ledger -q           # the spine
.venv/Scripts/python.exe -m pytest -m chaos -q               # crash injection
```

The suite leans on `hypothesis` for properties that must hold universally — the
gatherer never exceeding its ceiling, replay never raising on any event ordering,
confidence never leaving [0,1].

Crash tests simulate power loss by closing the database mid-lineage and reopening:
committed transactions survive, uncommitted ones vanish, exactly as SQLite behaves
under WAL.

---

## Next

In priority order:

1. **Test coverage for the four untested subsystems** — especially agents and
   validation, which carry security properties currently asserted only in docstrings.
2. **Memory creator/curator + world model** — the ingestion and nightly maintenance loop.
3. **FastAPI ingestion + CLI** — the actual entry points; there is no way to drive
   the system from outside Python yet.
4. **End-to-end integration test** proving all five DoD items in one run.
5. **Marketplace and self-improvement loops.**
6. **Long-horizon tasks** (your stated v-next focus) — the ledger and snapshot
   machinery is the right foundation, since a task spanning weeks is exactly a
   lineage that must survive many restarts.
