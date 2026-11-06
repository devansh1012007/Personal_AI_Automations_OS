# PAA — Personal Autonomous Cognitive Operating System Runtime

**A local-first, event-sourced runtime for authority, evidence, and recoverable work across AI agents.**

PAA sits beneath and beside agent harnesses. It does not try to out-reason frontier models or replace every agent with one universal loop. Its job is to provide the durable record, authority checks, execution boundaries, validation, and recovery that remain useful when the model, harness, provider, or interface changes.

> **The ledger is truth. Models propose; deterministic policy decides. Every managed effect has an owner, an authority trail, and a truthful recovery or disposal path.**

**Status:** See [Build status](#build-status) for the repository's recorded test snapshot and implementation boundaries. Planned architecture in this README and in `docs/VISION.md` is not a claim that those interfaces are already implemented.

## What PAA is

Most agent systems keep important task state inside a running process or in a vendor-specific session. When that process disappears, the task history, authority behind actions, and evidence of what actually ran can become fragmented.

PAA starts from a different premise: **the append-only event ledger is the source of truth**. Queues, caches, projections, indexes, and workspaces are derived state that can be reconstructed, reconciled, or checked against recorded checkpoints.

| Property | How PAA approaches it |
|---|---|
| Crash recovery | Replays the ledger, checks workspace state against recorded manifests, and resumes only work that is safe to resume. |
| Tamper evidence | Events are hash-chained; modifying history invalidates subsequent digests. |
| Authority | A task carries a mandate recording who or what requested it, the applicable policy, and its permitted scope. |
| Deterministic safety | Policy, schema checks, sandbox boundaries, and validation gate managed mutations; a model cannot override those decisions. |
| Bounded context | The context gatherer enforces a token ceiling and selects information through deterministic rules. |
| Explainability | Causal links connect actions to the task, mandate, decisions, and prior events that led to them. |
| Portability | Model providers, storage backends, sandbox implementations, and agent harnesses are intended to be replaceable behind narrow interfaces. |

PAA's central distinction is **authority plus evidence**: who permitted the work, under which rules, what crossed the execution boundary, and what verification actually passed.

## What PAA is not

- Not another general-purpose agent framework or a replacement for every agent harness.
- Not a promise to outperform frontier models at reasoning.
- Not a prompt-based safety layer. Models may propose actions; deterministic runtime policy authorizes them.
- Not an unrestricted plugin host where third-party code can mutate trusted runtime state.
- Not a claim that external actions can always be undone or that a third-party agent's hidden reasoning can be resumed after a crash.

PAA should integrate with front-half systems that handle intake, routing, and agent interaction. It should focus on the back half: containment, authorization, verification, evidence, and recovery.

## Quick start

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest tests/ -q
```

Run it from the CLI (for example, with a local model via Ollama, or `echo` when offline):

```bash
paa doctor                       # inspect configured backends and sandbox
paa submit "summarise my notes"  # submit a task
paa ledger <correlation-id>      # inspect the event chain and causal history
paa recover                      # run the post-crash recovery sweep
paa serve                        # FastAPI ingestion + background daemon (loopback only)
```

Optional extras, each with a fallback if skipped:

```bash
uv pip install -e ".[vector]"       # Qdrant embedded, otherwise NumPy brute force
uv pip install -e ".[graph]"        # KuzuDB, otherwise SQLite recursive CTEs
uv pip install -e ".[embeddings]"   # real embeddings, otherwise a hash embedder
uv pip install -e ".[api]"          # FastAPI ingestion
```

No Docker, server, or GPU is required for the lightweight local setup. Stronger isolation backends may require WSL2 or Docker; see the platform limitations below.

## Architecture

PAA is designed as a **narrow waist** between front-half agent systems and the machine capabilities they use. The core owns the guarantees. Harnesses and providers attach through explicit, versioned boundaries.

```text
  Front-half systems / clients
  Editors · chat · event sources · agent harnesses · custom UIs
                 │
                 │  ACP · HTTP · CLI · webhooks · Executor port
                 ▼
  ┌────────────────────────────────────────────────────────────┐
  │                         PAA CORE                            │
  │                                                            │
  │  mandates · deterministic policy · sandbox · validation   │
  │  event ledger · replay · recovery · watchdog · budgets     │
  │  memory · bounded context · provenance · evidence export   │
  └──────────────────────────────┬─────────────────────────────┘
                                 │
                   policy-gated, recorded operations
                                 │
  ┌──────────────────────────────▼─────────────────────────────┐
  │                 CAPABILITY / PROVIDER BOUNDARY              │
  │  filesystem · shell · git · browser · search · model APIs  │
  │  MCP and other adapters · sandboxed local/remote providers │
  └────────────────────────────────────────────────────────────┘
```

The ledger remains the source of truth. The cold lake holds immutable inputs and content-addressed blobs; replay builds task projections and derived indexes. Queues, caches, vector/graph indexes, and workspace metadata are not competing authorities.

```text
                    ingestion / external inputs
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │ cold_lake (immutable blobs) │
                 └──────────────┬──────────────┘
                                │
                 ┌──────────────▼──────────────┐
                 │ system_state.ledger         │  SOURCE OF TRUTH
                 │ append-only, hash-chained   │
                 └──────────────┬──────────────┘
                                │ replay
                 ┌──────────────▼──────────────┐
                 │ task / effect projections  │
                 └──────────────┬──────────────┘
                                │
        ┌─────────────┬──────────┼─────────┬──────────────┐
        ▼             ▼          ▼         ▼              ▼
   hot serving    vector     graph     queue        workspaces
   projections    index      edges     (derived)    (checked state)
```

The task lifecycle is event-led. The exact event sequence depends on the operation, but the important rule is that intent, authority, execution results, and verification are recorded around every managed effect. Replay reconstructs recorded state; it does not repeat external actions.

## Task-specific composition, not a global plugin container

Different tasks need different execution strategies and different subsets of capabilities. A coding task may need repository access, a shell, and tests. A research task may need search, document retrieval, and evidence storage. They should not load every integration into one global agent environment.

PAA's direction is to assemble a **task-scoped runtime context** from a shared trusted kernel and a validated set of dependencies. A task can use a native PAA loop or an external harness, provided that operations for which PAA claims authority, containment, or evidence cross PAA's instrumented boundaries.

### Temporal composability — safe lifecycle over time

Temporal composability means that components can be started, suspended, replaced, cancelled, or removed while PAA accounts for the effects they introduced.

Each managed effect should have an explicit owner (task, harness generation, pipeline, workspace, or capability lease), a durable identity, and a truthful disposal or recovery path. Examples include unregistering an event listener, terminating a process, releasing a lease, or restoring an isolated workspace to a verified checkpoint.

Not every effect is reversible. A delivered email cannot generally be unsent; a payment cannot be treated as undone because a process was unloaded. PAA must preserve those facts in the ledger and treat a correction or compensation as a new, separately authorized action.

The lifecycle vocabulary matters:

| Operation | Meaning |
|---|---|
| Cancel | Stop further task work; does not undo prior effects. |
| Quiesce | Stop admitting new work and drain or cancel in-flight work. |
| Dispose / unload | Release resources and registrations owned by a component scope. |
| Roll back | Reverse eligible changes within an explicit transaction boundary. |
| Compensate | Perform a new authorized action to mitigate an irreversible effect. |
| Replay | Reconstruct state from events without re-executing external work. |
| Recover | Reconcile effects and restore a known checkpoint before safe resumption. |

A crash between a provider's successful action and PAA's acknowledgement creates an **unknown outcome**, not proof that the action failed. Recovery must reconcile using provider state or idempotency keys; it must not blindly repeat the action.

### Spatial composability — task-specific dependency resolution

Spatial composability means that a harness or pipeline declares what it requires, while PAA determines which eligible providers can satisfy those requirements under the current mandate and policy.

A component declares contracts such as `filesystem.read`, `filesystem.write`, `shell.execute`, `tests.run`, `web.search`, or `evidence.store`. It should not hardcode a specific operating system, provider, or integration unless the task requires one.

PAA resolves each requirement against contract compatibility, version, trust, isolation, availability, locality, and resource constraints. The task receives a bounded, revocable **lease** or client for the selected provider—not ambient access to a global service container. Finding a provider does not grant permission: every effect still passes mandate and deterministic policy checks.

When a required provider disappears, becomes unhealthy, changes contract, or loses authorization, only affected work should be suspended or moved into an explicitly declared degraded mode. Provider changes are recorded and re-authorized where needed; PAA must not silently weaken security, change data handling, or exceed the task's original mandate.

### Keep three graphs distinct

| Graph | What it answers | What it records |
|---|---|---|
| Dependency graph | What must be available for a component to activate? | Requirements, provider bindings, compatibility, and resolution decisions. |
| Execution DAG | What work must happen, in which order, with which branches and approvals? | Steps, dependencies, retries, checkpoints, and outcomes. |
| Effect-ownership graph | Which scope created each resource or side effect, and how can it be disposed or reconciled? | Scope ownership, resource handles, inverse/compensation strategy, and lifecycle state. |

These graphs connect through stable IDs and ledger events, but they are not interchangeable. See the [Temporal and spatial composability addendum in VISION](docs/VISION.md#appendix-temporal-and-spatial-composability-for-paa) for the proposed object model, lifecycle invariants, event candidates, and validation scenarios.

**Implementation status:** This is a proposed architectural direction, not a claim that the dependency resolver, effect registry, universal harness protocol, or hot replacement system is already implemented. The first step is to prove the smallest lifecycle and provider contracts against real integrations before generalising them.

## Front-half / back-half boundary

PAA does not need to own every chat surface, event router, worker scheduler, or agent loop. Those systems can provide intake and reasoning. PAA's value is the layer that can independently answer:

- Who requested this work, and what mandate allowed it?
- Which policy version and constraints applied?
- Which harness, provider, and capability implementation actually ran?
- What crossed the sandbox or external boundary?
- Which commands, mutations, approvals, and tests have recorded evidence?
- What is recoverable after a crash, and which effects remain unresolved?

The intended seams include ACP for client/editor interaction, an Executor port for front-half systems that can hand a task and mandate to PAA, and MCP or other adapters for tool interoperability. These are integration seams, not permission to bypass PAA's policy and ledger.

## This machine vs. the original RFC

The RFC assumes a CUDA workstation. The original development environment is a Ryzen 5 7535HS laptop with 13.3 GB RAM (about 3.5 GB free most of the time), a Radeon 660M iGPU with 2 GB VRAM, Windows 11, and no Docker. The implementation has been adapted to that constrained machine rather than pretending the target environment is the RFC's ideal hardware.

| RFC assumption | Development environment constraint | Adaptation |
|---|---|---|
| vLLM + Llama-3-8B-Q8 at high VRAM utilisation | Windows laptop with Radeon 660M iGPU and 2 GB VRAM; vLLM's CUDA/Linux assumptions do not fit | Provider-agnostic layer, local-first with logged escalation (ADR-0007, ADR-0015) |
| Concurrent Mistral-7B critic + 8B curator | Insufficient memory headroom | One configurable provider; critical authorization remains deterministic rather than model-generated |
| gVisor `runsc` sandbox | gVisor is a Linux userspace kernel and is not a native Windows backend | Pluggable sandbox; Windows uses Job Object caps, workspace jail, and AST pre-scan. This is materially weaker than gVisor (ADR-0006). |
| PostgreSQL + Qdrant + Redis + S3 | No Docker in the original development environment; high idle memory for the proposed stack | SQLite (WAL), embedded Qdrant, SQLite queue, content-addressed blob store (ADR-0001–0005) |
| KuzuDB embedded | Compatible Windows wheel available | Used as specified |
| 384-dimensional embeddings | MiniLM-L6-v2 runs on CPU | Used as specified |

The goal of these substitutions is to preserve logical contracts so a backend can be changed without rewriting the system. The Windows sandbox is not equivalent to gVisor; use WSL2 or Docker when stronger isolation is required.

## Implementation lessons captured in the ADRs

Implementing the original RFC literally would have introduced deadlocks, leaks, and silent work loss. These corrections were found during implementation and testing; their rationale is recorded in `docs/adr/`.

1. **Retry identity (ADR-0008):** The original idempotency key made `EXECUTION_STARTED` unique per task, preventing a failed task from starting another attempt. An attempt counter and discriminator make retries distinct while preserving idempotency.
2. **Bounded recovery index (ADR-0010):** A partial index over an append-only event table did not shrink as completed events accumulated. A bounded head projection keeps one row per lineage.
3. **Step completion vs. task completion (ADR-0010):** `EXECUTION_COMPLETED` can occur per step and must not be treated as a terminal event for the whole task.
4. **Sandbox memory units (ADR-0009):** Integrating memory usage over time produces MB-seconds, not a memory ceiling. The implementation uses peak RSS, matching the resource that cgroups and Job Objects constrain.
5. **Bounded scoring (ADR-0012):** A score that divides by factors allowed to reach zero can become unbounded. The formula was rewritten with `(1 + x)` denominators while preserving monotonic behavior.
6. **Temporal decay precedence (ADR-0013):** Decay originally applied only to the graph term, not the full hybrid retrieval score. The expression was corrected.
7. **Zero-success domains (ADR-0016):** The friction score divided by successful commits, which is zero for a domain that has only failed. The formula was adjusted for that case.
8. **Pydantic v2 compatibility:** The `SkillContractModel` used v1-only `regex=` and `@validator` syntax and was updated.

Additional defects found through tests included OFFSET pagination skipping rows during deletion, recovery-sweep audit events that grew the ledger on every boot, an incorrect zstd unknown-size sentinel (`-1` is required), and nondeterministic tie ordering when same-time arrivals were sorted by random UUID.

## Design decisions worth explaining

- **The router is optional.** Routing should be used when multiple eligible agents or strategies make it useful, not inserted into every task.
- **Delegation is mediated.** Agent interaction is allowed through the orchestrator, which owns the delegation graph and rejects cycles rather than relying on a blanket ban on peer interaction (ADR-0018).
- **Context is partly model-assisted, deterministically assembled.** A model may propose queries or slots; deterministic ranking and token ceilings decide what enters context. Untrusted source text cannot directly choose the planner's packet.
- **Memory domains share a small number of mechanisms.** The 18-domain vocabulary maps onto six storage/retrieval mechanisms rather than requiring 18 independent subsystems (ADR-0014).
- **Meta-learning is staged honestly.** Prototypical few-shot classification is designed (ADR-0017), not a claim of gradient-based MAML or a validated self-improving harness.
- **A stronger harness cannot erase a model-capability gap.** PAA's advantage is intended to be recovery, durable memory, deterministic safety, authority, and evidence—not an unsupported claim that a small local model reasons like a frontier model.

## Why this over existing tools

PAA is not a claim that other systems cannot perform the same individual features. The intended distinction is the combination of a vendor-neutral, local-first runtime with an independent record of authority and execution evidence.

- **Compared with a frontier coding agent:** PAA does not claim superior reasoning. It aims to make work recoverable, auditable, and subject to deterministic policy across sessions and providers.
- **Compared with an agent/skill ecosystem:** PAA treats third-party capabilities as untrusted until their operations cross an explicit permission and execution boundary.
- **Compared with an ambient assistant:** PAA is not yet an always-on, multimodal personal assistant. Its potential future interfaces should inherit the same mandate, context, and evidence model rather than inventing separate trust rules.

The bet is that people will use more than one agent system, and will want a neutral system of record for what those agents were authorized to do and what they actually did. That is a hypothesis to validate, not a guaranteed market outcome.

## Known limitations and planned work

The following are gaps or proposed work, not completed features. The detailed, ordered plan and exit criteria live in [`docs/VISION.md`](docs/VISION.md) and [`docs/ROADMAP.md`](docs/ROADMAP.md).

| Area | Current limitation / next proof |
|---|---|
| Real-world capabilities | The coder loop exists, but the permissioned file, shell, and Git tool path must be completed and proven end to end through the ledger and sandbox. |
| External harnesses | ACP server is experimental; running an external harness behind PAA's sandbox and measuring what activity remains outside observable boundaries is planned. |
| Lifecycle accounting | A durable, scope-owned effect registry and crash reconciliation for provider outcomes are proposed; they must be validated before PAA claims general temporal composability. |
| Dependency resolution | Task-specific declarations, provider resolution, bounded leases, and dependency-loss behavior are proposed; no universal resolver is claimed. |
| Observability | Metrics and tracing are built but require dedicated validation. |
| Codebase intelligence | Full repository indexing and LSP/symbol-aware context are not yet complete. |
| Git workflow | Branch-per-task, conflict awareness, and verified commits remain work to complete. |
| Ingestion | Minimal event-source integration is planned; broad Telegram/email/Slack and ambient bridges are not a current commitment. |
| Multimodal | Vision, voice, and general document/audio interaction are parked. |
| Evaluation | Agent comparison on the user's own tasks with deterministic verification is planned; self-improvement claims are not yet validated. |
| Product surface | CLI and loopback HTTP exist; a web dashboard, mobile access, and multi-user/team mode are later and conditional. |
| Marketplace | Signed package metadata and installer work exist, but publishing and a marketplace economy are conditional platform work, not the immediate product. |
| Security | The Windows sandbox is weaker than gVisor; stronger isolation and independent security review remain future work. |

## Roadmap at a glance

The current plan is horizon-based. Existing ideas are retained, but sequenced by dependency and evidence rather than treated as commitments to build every surface immediately.

1. **Horizon 0 — Foundation:** event-sourced ledger, replay and recovery, context and memory, storage, sandbox, policy, model providers, Unified Skill Adapter, API/daemon/CLI, router-backed coder loop, experimental ACP server.
2. **Horizon 1 — Hands, authority, and honesty:** permissioned file/shell/Git capabilities, coder-to-ledger wiring, mandate events, cancellation, and a real end-to-end task using a frontier model.
3. **Horizon 2 — Seams:** harden ACP, version the event envelope, add out-of-process hooks and event export, run the first external harness inside the sandbox, test the Executor-port idea, and add a credential broker.
4. **Horizon 3 — Evidence and attention:** offline-verifiable change bundles, observe mode, watchdog, attention measurement, replay-based regression, and agent evaluation on real repositories.
5. **Horizon 4 — Reach (conditional):** minimal event intake if needed, dashboard, durable long-horizon task graphs, speculative sandboxed branches, and team support.
6. **Horizon 5 — Platform (conditional):** out-of-process pipeline SDK, executable package loading, marketplace publishing, stronger default isolation, and third-party security review.

Parked until the earlier horizons validate the thesis: a full in-house router/worker/panel stack, broad ambient and multimodal bridges, local fine-tuning, cloud-hosted remote-machine orchestration, and a skill economy with payments or reputation.

This sequencing does **not** delete earlier ideas. It prevents a marketplace, always-on assistant, or universal harness system from outrunning the runtime guarantees that would make those products trustworthy.

## Build status

"Tested" means tests exist and pass. "Untested" means code may exist but has not been verified; treat it as unfinished. The status below is the repository's recorded snapshot, not a guarantee about a later checkout. Re-run the commands in [Testing](#testing) before relying on it.

| Subsystem | State | Tests / evidence |
|---|---|---:|
| Core types, config, permissions | Tested | Via ledger suite |
| Event ledger + replay + recovery | Tested | 71 |
| Bounded context engine (metrics, budget, gatherer, compaction) | Tested | 353 |
| Storage (vector, graph, cold lake, queue, relational) | Tested | 255 (+5 skipped, Redis) |
| Sandbox (subprocess/WSL/Docker/dryrun + recursion guard) | Tested | 39 |
| Memory decay + domains + contradiction | Tested | 37 |
| Agents (orchestrator, delegation, policy, context, planner, critic, router, worker) | Tested | 70 |
| Composition root + end-to-end integration | Tested | 16 |
| Deterministic validation (AST, patch, workspace, tests) | Exercised via agents; no dedicated suite | — |
| Model providers + escalation router + platform registry | Tested (OpenAI/Groq/Gemini/etc. + localhost) | 71 |
| Memory creator/curator + world model | Tested | 76 |
| Unified Skill Adapter (Claw Hub + MCP) | Tested | 51 |
| Marketplace (signed packages, 5-gate installer) | Tested | 18 |
| Self-improvement + meta-learning | Tested | 22 |
| Claude-Code features (permissions, hooks, loop, sessions) | Tested | 39 |
| Coder agent (`paa agent` — router-backed tool loop) | Tested | 91 |
| FastAPI ingestion + daemon + CLI | Tested | 27 |
| Docker deployment + Postgres/S3 backends | Built; live-server run still needs a pass | Parity + backend |
| Observability (metrics, tracing, runs, logging) | Built; untested | — |

**Recorded test snapshot:** 1,185 collected — 1,104 passing, 75 skipped, 6 failing. The existing project note attributes the six failures to an environment-specific `python`/`python3` executable mismatch in sandbox worker steps. Verify that diagnosis on the current checkout rather than treating it as a permanent property of the code.

The CLI includes `paa submit/status/ledger/recover/gate/doctor/serve/agent`; the HTTP interface is loopback FastAPI. The background daemon runs boot recovery, drains the task queue, ingests signals, and runs the decay sweep. `Runtime.build()` in `src/paa/runtime.py` is the composition root. The existing integration suite records coverage for full task lifecycle, crash-and-recover, human-gate persistence, deterministic security blocks, and the token ceiling; consult the tests and roadmap for the exact current proof.

## Layout

```text
src/paa/
├── core/          types, errors — imported by everything, depends on nothing
├── config.py      RFC constants and configuration
├── ledger/        events, store, replay, recovery       ← the spine
├── storage/
│   ├── relational/  SQLite WAL + schema (Postgres-portable)
│   ├── vector/      Qdrant embedded / NumPy fallback
│   ├── graph/       Kuzu / SQLite recursive CTE
│   ├── coldlake/    content-addressed blobs + zstd
│   └── queue/       durable SQLite queue / Redis
├── context/       metrics, budget, gatherer, compaction
├── memory/        domains, decay, contradiction
├── agents/        base, delegation, orchestrator, policy, context, reasoning
├── sandbox/       pluggable containment backends
├── validation/    AST scan, patch apply/rollback, workspace manifests
├── models/        provider abstraction + escalation router
├── coder/         coding-agent loop, permissions, hooks, sessions, tools
├── skills/        Unified Skill Adapter
└── observability/ metrics, tracing, execution runs
```

`coder/` is the package that consumes the runtime and is driven by a model directly; `coder/agent.py` is its composition root. See `docs/coder-agent.md`.

## Testing

```bash
.venv/Scripts/python.exe -m pytest tests/ -q                 # everything
.venv/Scripts/python.exe -m pytest tests/ledger -q           # the spine
.venv/Scripts/python.exe -m pytest -m chaos -q               # crash injection
```

Uses Hypothesis for properties that should hold universally, including the gatherer's token ceiling, replay robustness under event orderings, and bounded confidence values. Crash tests exercise database shutdown and reopening around a lineage; extend these with provider-boundary fault injection before claiming that external effects are exactly-once or generally reversible.

## Further reading

- [`docs/VISION.md`](docs/VISION.md) — thesis, non-goals, horizons, validation/kill criteria, and the detailed temporal/spatial composability proposal.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — implementation status, work items, and exit criteria.
- [`docs/EXTENSIBILITY_PIPELINES.md`](docs/EXTENSIBILITY_PIPELINES.md) — proposed strategy/pipeline seam.
- [`docs/coder-agent.md`](docs/coder-agent.md) — the current coder-agent loop.
- [`docs/adr/`](docs/adr/) — architectural decisions and implementation corrections.

## License

MIT. See [`LICENSE`](LICENSE).
