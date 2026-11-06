# PAA — Personal Autonomous Cognitive Operating System

**A local-first, event-sourced runtime for running, governing, and auditing autonomous agents.**

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-alpha-orange)

PAA records every agent action in a tamper-evident ledger, gates every mutation
through deterministic checks, and can rebuild its state from that ledger after a
crash. It is designed to answer, for any change an agent made: *what ran, what
authorised it, what the agent knew, and whether verification actually passed.*

> **Status: alpha.** The runtime core is built and tested. The pieces that make it a
> daily-driver coding tool (file, shell, and git tools) and the ability to host
> third-party agent harnesses are **not built yet**. See
> [Status](#status) and [Known limitations](#known-limitations) before relying on it.
> Direction and sequencing live in [docs/VISION.md](docs/VISION.md).

---

## Contents

- [Why PAA](#why-paa)
- [Guarantees](#guarantees)
- [Quick start](#quick-start)
- [Using PAA from an editor (ACP)](#using-paa-from-an-editor-acp)
- [Architecture](#architecture)
- [Extension model](#extension-model)
- [Where PAA fits](#where-paa-fits)
- [Status](#status)
- [Known limitations](#known-limitations)
- [Security model](#security-model)
- [Design decisions and RFC deviations](#design-decisions-and-rfc-deviations)
- [Repository layout](#repository-layout)
- [Testing](#testing)
- [Documentation](#documentation)
- [License](#license)

---

## Why PAA

Most agent tooling keeps task state in process memory and trusts the model's own
account of what it did. Kill the process and the task is gone. Ask what happened
and you get a summary written by the thing being audited.

PAA starts from the opposite premise: **the ledger is the only source of truth.**
Queues, caches, vector indexes, and even the workspace on disk are derived views
that can be rebuilt by replaying events. That single decision gives you:

- recovery after a crash,
- evidence that cannot be quietly rewritten,
- a causal chain you can walk backwards from any action,
- and a place to attach deterministic safety checks that no model can talk its way past.

PAA does **not** try to out-reason frontier models. Agentic coding quality is
mostly the model; architecture cannot close that gap
([ADR-0015](docs/adr/0015-capability-ceiling.md)). PAA's job is to make whatever
model you point at it *recoverable, auditable, and bounded*.

## Guarantees

| Property | Mechanism |
|---|---|
| **Crash recovery** | Boot sweep replays the ledger, verifies the workspace against a checksum manifest, rolls back drift, and re-queues work that is safe to retry. |
| **Tamper evidence** | Each event commits to the previous event's hash; editing history invalidates every later digest. |
| **Bounded context** | A hard token ceiling is enforced by the context gatherer and covered by a property test. |
| **Deterministic safety** | AST scan, schema validation, and real test runs gate every mutation. No model sits in the security loop; a deterministic critic FAIL cannot be overridden by a model PASS. |
| **Explainability** | Causation links let you walk from any action back to the event that caused it (`paa ledger`). |
| **Controlled egress** | Escalation from a local model to a hosted one is a logged event. In `LOCKDOWN` mode it never happens. |

Ordering rule: **ledger before side effect, always.** A crash between "event
written" and "work done" is recoverable, because replay knows the work was
intended. The reverse order would not be.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or plain `pip`).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Run the suite:

```bash
# Linux / macOS
.venv/bin/python -m pytest tests/ -q
# Windows
.venv\Scripts\python.exe -m pytest tests\ -q
```

Drive the runtime from the CLI. A local model (Ollama) or the offline `echo`
provider works with no API keys:

```bash
paa doctor                        # which backends and sandbox are actually live
paa submit "summarise my notes"   # run a task to a terminal state
paa status <correlation-id>       # replayed state of a task
paa ledger <correlation-id>       # walk the causal event chain
paa gate <correlation-id> --approve   # clear a human-attestation gate
paa recover                       # post-crash boot sweep
paa agent                         # tool-using agent loop over the runtime
paa serve                         # loopback HTTP API + background daemon
```

Hosted models are configuration, not code. For example, set
`PAA_MODELS__LOCAL_PROVIDER=openrouter` and put the key in `.env`
([ADR-0020](docs/adr/0020-provider-keys-from-dotenv.md)).

Optional extras, each with a fallback if omitted:

| Extra | Adds | Fallback |
|---|---|---|
| `.[vector]` | Qdrant embedded | NumPy brute force |
| `.[graph]` | KuzuDB | SQLite recursive CTEs |
| `.[embeddings]` | Real embeddings (large download) | Hash embedder |
| `.[api]` | FastAPI ingestion | CLI only |
| `.[postgres]`, `.[redis]` | Postgres and Redis backends | SQLite (WAL) and SQLite queue |
| `.[otel]` | OpenTelemetry exporters | In-process metrics |

No Docker, server, or GPU is required for the default configuration. A Docker
deployment with Postgres and S3-compatible storage is available; see
[docs/deployment/docker.md](docs/deployment/docker.md).

### HTTP API (loopback only)

| Route | Purpose |
|---|---|
| `GET /health` | Liveness and backend report |
| `POST /tasks` | Submit a task |
| `GET /tasks/{id}` | Replayed task state |
| `POST /tasks/{id}/gate` | Approve or reject a parked task |
| `POST /tasks/{id}/correction` | Submit a human correction |
| `GET /ledger/{id}` | Event chain for a task |
| `POST /signals` | Ingest a signal (backpressure returns HTTP 429) |

## Using PAA from an editor (ACP)

**Experimental.** PAA ships an [Agent Client Protocol](https://agentclientprotocol.com)
server so ACP-capable editors can use it as an agent. Permission prompts from the
runtime's gate are shown in the editor and **fail closed**: anything other than an
explicit "Allow" is a denial.

```bash
uv pip install agent-client-protocol
```

Zed — Settings → AI → External Agents → Add Custom Agent:

```json
"agent_servers": {
  "PAA": {
    "type": "custom",
    "command": "/path/to/repo/.venv/bin/python",
    "args": ["-m", "paa.acp_server"],
    "env": { "PAA_HOME": "/path/to/.paa" }
  }
}
```

What this gives you today is a chat and approval interface over PAA's
ledger-facing tools (`submit_task`, `run_task`, `read_ledger`, `recall_facts`, …).
It does **not** edit files in your open project, because PAA has no file or shell
tools yet. Responses are not streamed, cancel is not implemented, and editor
context (`@`-mentions, working directory, MCP servers) is ignored. Coder-loop turns
are recorded to session transcripts rather than the ledger; only mutating tool
calls reach the ledger.

## Architecture

```
                      ┌──────────────────────────┐
   ingestion ────────►│  cold lake (immutable)   │  raw signals, content-addressed blobs
                      └────────────┬─────────────┘
                                   │
                      ┌────────────▼─────────────┐
                      │  ledger (append-only,    │  ◄── SOURCE OF TRUTH
                      │  hash-chained)           │      everything else is derived
                      └────────────┬─────────────┘
                                   │ replay (left fold)
                      ┌────────────▼─────────────┐
                      │     TaskProjection       │
                      └────────────┬─────────────┘
                                   │
   ┌────────────┬──────────────────┼──────────────┬──────────────┐
   ▼            ▼                  ▼              ▼              ▼
hot_serving  vector index     graph edges      queue        workspaces
 (truth)      (recall)       (provenance)    (volatile)     (host disk)
```

Task lifecycle. Every arrow writes a ledger event first:

```
TASK_REQUESTED → TASK_QUEUED → CONTEXT_HYDRATED → PLAN_COMPILED
      → POLICY_CLEARED → EXECUTION_STARTED → CRITIQUE_CONCLUDED
      → EXECUTION_COMPLETED → MUTATION_COMMITTED
                  │
                  ├── POLICY_BLOCKED / SECURITY_VIOLATION   (terminal)
                  ├── AWAITING_HUMAN_ATTESTATION            (parks; never auto-clears)
                  └── STATE_ROLLBACK_TRIGGERED              (drift → restore)
```

Key design choices:

- **Optional router.** `should_route()` returns `False` when a target agent is named
  or fewer than three agents are eligible. When routing runs, it uses an LLM,
  because a small intent classifier cannot see project state.
- **Delegation without deadlock.** Agents may delegate to each other, but only
  through the orchestrator, which owns the delegation graph and refuses any edge
  that would close a cycle ([ADR-0018](docs/adr/README.md)).
- **AI proposes, code decides.** In the context builder, a model may expand queries
  and infer slots; deterministic code does the ranking and enforces the ceiling.
  This keeps packets reproducible for replay and keeps untrusted text out of the
  planner's context by construction.
- **Eighteen memory domains, six mechanisms.** Domains are vocabulary; each maps to
  one of volatile, relational, immutable log, vector, graph, or document storage.
  Adding a domain is a data change ([ADR-0014](docs/adr/README.md)).
- **Pluggable infrastructure.** SQLite/Postgres, embedded/Qdrant vectors,
  Kuzu/SQLite graph, SQLite/Redis queue, local/S3 blob store, and
  subprocess/WSL/Docker/gVisor sandboxes all sit behind interfaces.

## Extension model

PAA's extension surface is deliberately **narrow and protocol-based** rather than an
in-process plugin API. The rule: *an extension may attach wherever its inputs and
outputs cross a boundary that is recorded in the ledger and gated by policy.*

| Seam | Direction | Interface | State |
|---|---|---|---|
| Tools and skills | Runtime → tools | MCP and Claw Hub via the Unified Skill Adapter | Built |
| Models | Runtime → providers | Provider registry (Anthropic, Gemini, Ollama, OpenAI-compatible, echo) | Built |
| Editors and UIs | Client → PAA | ACP server (`paa.acp_server`) | Experimental |
| Lifecycle hooks | In coder loop | Pre/post tool, prompt submit | Built in-process; out-of-process JSON hooks planned |
| External agent harnesses | PAA → agents | ACP client under the sandbox | Planned |
| Event export | Ledger → observers | JSONL / OpenTelemetry | Planned |
| Custom agentic loops | Pluggable pipelines | `Pipeline` + `RuntimeContext` | Design only ([design](docs/EXTENSIBILITY_PIPELINES.md), [audit](docs/flexability-audit.md)) |

Constraints that any extension must satisfy: it runs out of process; it can tighten
policy but never loosen it; its results are recorded as ledger events so replay reads
the record rather than re-executing it; and it speaks a versioned event envelope.
Rationale is in [docs/VISION.md](docs/VISION.md#extension-model).

## Where PAA fits

PAA is infrastructure beneath agents, not another agent.

- **Agent harnesses** (terminal and editor coding agents) decide *how a model works
  on a task*. PAA does not replace them. Hosting them under PAA's ledger and sandbox
  is the main planned direction and is **not implemented**.
- **Multi-session managers and orchestrators** run agents in parallel. PAA's focus is
  a durable, verifiable record of what happened, not session management.
- **Governance gateways** provide organisation-wide identity and policy. PAA is
  local-first and single-user today; team features are future work.

## Status

Measured on a clean Linux checkout (Python 3.12, `.[dev]` only, no Docker) at commit
`4ac96db`. Run the suite yourself for current numbers; they drift.

| Metric | Value |
|---|---|
| Python source (`src/`) | ~52,000 lines |
| Tests collected | 3,145 |
| Result | 2,899 passed · 241 skipped · 4 xfailed · 1 failed |

Skips are tests that need optional extras or live services (Postgres, S3, Redis,
Docker). The 4 xfails are strict: three known validation defects and one open policy decision, each with its reason stated in the test. The
single failure, `test_user_is_the_workspace_owner_not_root`, is environmental: it
assumes a non-root host user and a Docker daemon. CI is defined in
[.github/workflows/ci.yml](.github/workflows/ci.yml); check the Actions tab for its
current state rather than trusting this file.

| Area | State |
|---|---|
| Event ledger, replay, recovery | Built and tested |
| Context engine (budget, gatherer, compaction) | Built and tested |
| Storage (relational, vector, graph, cold lake, queue) | Built and tested; Postgres/S3 have live-server suites that run when services are supplied |
| Sandbox (subprocess, WSL, Docker, dry-run, recursion guard) | Built and tested |
| Deterministic validation (AST, patch, workspace, test runner) | Built and tested; 3 known defects tracked as xfail |
| Agents (orchestrator, delegation, policy, planner, critic, router, worker) | Built and tested |
| Model providers and escalation router | Built and tested; **no frontier provider has driven a full task end to end** |
| Memory (creator, curator, decay, contradiction, world model) | Built and tested |
| Unified Skill Adapter (MCP, Claw Hub) | Built and tested |
| Marketplace (signed packages, five-gate installer) | Metadata path built; executable package loading is not |
| Observability (metrics, tracing, runs) | Built and tested |
| Coder agent (`paa agent`) | Alpha; tools are ledger-facing only |
| ACP server | Experimental |
| Docker deployment | Built; operators should run the live-stack suites themselves |
| File, shell, web, and git tools | **Not built** |
| Codebase intelligence (indexing, LSP) | Not built |
| Web UI | Not built (a terminal UI module exists in `paa/tui`) |
| Hosting external agent harnesses | Not built |

## Known limitations

These block day-to-day use and are listed first on purpose.

1. **No concrete work tools.** There is no file edit, shell, web search, or git tool.
   The agent can drive the runtime but cannot yet change your project.
2. **The coder loop is not ledger-integrated.** Its turns are written to session
   transcripts, not ledger events, so it does not get crash recovery or hash-chained
   history. Mutating runtime tools are the exception.
3. **No external-harness hosting.** There is no adapter for third-party agents, and no
   supported way to install a replacement agentic loop without modifying core
   ([audit](docs/flexability-audit.md)).
4. **Weaker sandbox on Windows.** The Windows backend uses Job Object limits, a
   workspace jail, and an AST pre-scan. That is materially weaker than gVisor. Use the
   WSL2 or Docker backend for untrusted code ([ADR-0006](docs/adr/README.md)).
5. **Single user, single machine.** No authentication, multi-user isolation, or remote
   access.
6. **No cost or budget dashboard**, and no evaluation harness for measuring whether
   self-improvement loops help.
7. **Multimodal input** (vision, voice, documents) is not supported.

## Security model

- Safety decisions are deterministic: permissions, protected paths, irreversible-
  operation patterns, network egress, anti-goals, AST rules. Models propose; they do
  not decide.
- API keys must not appear in logs, ledger payloads, or router-raised exceptions; this
  is asserted by tests. A provider that raises an error containing its own key is not
  covered.
- The AST scanner is a **screening layer, not an isolation boundary.** Isolation comes
  from the sandbox backend.
- The HTTP API binds to loopback only.
- Marketplace packages pass signature, hash, AST, permission, and smoke gates, but
  executable package loading is not implemented, so third-party code does not run today.

Details and trust boundaries:
[docs/security/security-model-and-trust-boundaries.md](docs/security/security-model-and-trust-boundaries.md).

## Design decisions and RFC deviations

PAA was built from an internal RFC (v4.0/v4.1). Implementing it literally would have
produced deadlocks, unbounded growth, or silently dropped work, so several rules were
corrected. Each is recorded in `docs/adr/`.

| Defect in the spec | Correction | ADR |
|---|---|---|
| Idempotency key made retries impossible (`EXECUTION_STARTED` writable once per task) | Attempt counter plus discriminator | [0008](docs/adr/0008-idempotency-key-with-attempt.md) |
| Recovery index never shrank on an append-only table | Bounded head projection, one row per lineage | [0010](docs/adr/0010-head-projection-not-partial-index.md) |
| `EXECUTION_COMPLETED` is per step, not per task, so it cannot be terminal | Terminal state derived from plan completion | [0010](docs/adr/0010-head-projection-not-partial-index.md) |
| Sandbox kill rule integrated memory over time (MB·s) | Peak RSS, which cgroups and Job Objects measure | [0009](docs/adr/README.md) |
| `A_score` divided by terms that can be zero | `(1 + x)` denominators, same monotonicity, bounded | [0012](docs/adr/README.md) |
| Temporal decay applied to one term only | Applied to the whole hybrid score | [0013](docs/adr/README.md) |
| Friction score divided by zero for domains with no successful commits | Zero-safe formulation | [0016](docs/adr/README.md) |

Defects found by tests rather than by reading: OFFSET pagination skipping rows while
deleting, a recovery sweep that grew the ledger by one row per boot, an incorrect
zstd size sentinel, and non-deterministic claim order from random-UUID tiebreaks.

Hardware reality also shaped the defaults. The reference machine is a Ryzen 5 7535HS
with ~13 GB RAM and no usable GPU, so the default stack is SQLite, embedded Qdrant,
and a provider-agnostic model layer rather than vLLM, Postgres, Qdrant, and Redis.
Every substitution keeps the logical contract, so moving back is a backend change.

## Repository layout

```
src/paa/
├── core/           types and errors; imported by everything, depends on nothing
├── config.py       every RFC constant, with its section cited
├── ledger/         events, store, replay, recovery          ← the spine
├── storage/        relational · vector · graph · coldlake · queue
├── context/        metrics, budget, gatherer, compaction
├── memory/         domains, decay, contradiction, creator, curator, world model
├── agents/         orchestrator, delegation, policy, reasoning, specialists
├── sandbox/        pluggable containment backends
├── validation/     AST scan, patch apply/rollback, workspace manifests
├── models/         provider abstraction and escalation router
├── skills/         Unified Skill Adapter (MCP, Claw Hub)
├── marketplace/    signed packages and the five-gate installer
├── improvement/    reflection, distillation, optimiser, meta-learning
├── coder/          permissions, hooks, tool loop, sessions
├── api/            FastAPI ingestion (loopback)
├── cli/            `paa` command
├── tui/            terminal dashboard
├── observability/  metrics, tracing, execution runs
├── runtime.py      composition root: Runtime.build()
├── daemon.py       background loop: recovery, queue drain, ingestion, decay sweep
└── acp_server.py   ACP server (experimental)
```

`Runtime.build()` wires everything together. `tests/integration/` exercises the full
lifecycle to `MUTATION_COMMITTED`, crash-and-recover, a human gate that parks across
a restart, deterministic security blocks, and the token ceiling.

## Testing

```bash
python -m pytest tests/ -q                  # everything
python -m pytest tests/ledger -q            # the spine
python -m pytest -m chaos -q                # crash injection
python -m ruff check src tests
python -m mypy src                          # strict
```

Property tests (Hypothesis) assert invariants that must hold universally: the
gatherer never exceeds its ceiling, replay never raises on any event ordering, and
confidence stays in [0, 1]. Crash tests simulate power loss by closing the database
mid-lineage and reopening it.

Important caveat: end-to-end runs use an **injected model**. Passing tests establish
that the wiring and guarantees hold, not that any particular model performs well.

## Documentation

| Document | Contents |
|---|---|
| [docs/VISION.md](docs/VISION.md) | Thesis (authority and evidence), landscape, extension model, horizons, kill criteria |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Ordered delivery plan with an exit test per item and decision gates |
| [docs/research/](docs/research/2026-11-deepseek-super-agent.md) | Research notes, starting with DeepSeek's Super-agent design |
| [docs/architecture/](docs/architecture/) | Overview, component interactions, repository map |
| [docs/adr/](docs/adr/README.md) | Architecture decision records |
| [docs/security/](docs/security/security-model-and-trust-boundaries.md) | Security model and trust boundaries |
| [docs/coder-agent.md](docs/coder-agent.md) | The `paa agent` tool loop and its permission model |
| [docs/EXTENSIBILITY_PIPELINES.md](docs/EXTENSIBILITY_PIPELINES.md) | Pluggable-loop design (not implemented) |
| [docs/flexability-audit.md](docs/flexability-audit.md) | What can and cannot be extended today |
| [docs/MCP_AND_PLUGINS.md](docs/MCP_AND_PLUGINS.md) | Skill adapter and plugin notes |
| [docs/deployment/docker.md](docs/deployment/docker.md) | Docker deployment |
| [AGENTS.md](AGENTS.md) | Conventions for contributors and coding agents |

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Devansh Gaur.
