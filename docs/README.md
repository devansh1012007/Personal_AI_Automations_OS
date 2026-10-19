# PAA Documentation Index

PAA — Personal Autonomous Cognitive Operating System Runtime. A local-first,
event-sourced substrate for running autonomous agents on personal hardware.

> **Status of this documentation:** It describes the implementation as it exists
> in `src/paa/`. Every implementation claim carries a repository-relative source
> reference (file and, where practical, line). Behaviours marked *inferred* or
> *unresolved* are not verified against the code — treat them as questions, not
> facts. The application source code and behaviour were **not** modified while
> producing these documents.

## A. Repository overview

PAA is a Python (≥3.11) application with `src`-layout under `src/paa/`. Its
spine is an **append-only, hash-chained event ledger** (SQLite WAL by default)
that is the only source of truth; every other substrate (queues, caches, vector
and graph indexes, the filesystem) is a derived view rebuilt by replay.

Major subsystems:

| Subsystem | Home | What it owns |
|---|---|---|
| Ledger | `src/paa/ledger/` | Event envelope, append-only store, deterministic replay, crash recovery |
| Runtime / composition root | `src/paa/runtime.py` | Wires DB, ledger, storage, model router, agents, orchestrator |
| Orchestrator | `src/paa/agents/orchestrator.py` | Task lifecycle: submit → hydrate → plan → policy → execute → commit |
| Agents (base) | `src/paa/agents/base.py` | `Agent`, `AgentContext`, `AgentMessage`, `AgentResult`, delegation |
| Reasoning agents | `src/paa/agents/reasoning.py` | Strategic planner, critic reviewer, task router, worker cell |
| Policy gate | `src/paa/agents/policy.py` | Deterministic risk gating (no model consulted) |
| Context engine | `src/paa/context/`, `src/paa/memory/` | Bounded context gatherer, memory domains, decay, creator/curator |
| Storage | `src/paa/storage/` | Relational, cold lake, vector, graph, queue (all pluggable, degrading) |
| Model layer | `src/paa/models/` | Provider registry, escalation router, structured output |
| Sandbox | `src/paa/sandbox/` | Pluggable containment backends (subprocess, WSL, Docker, dryrun) |
| Validation | `src/paa/validation/` | AST scan, patch dry-run, schema, drift, pytest |
| Coder agent | `src/paa/coder/` | Tool-use loop over the runtime (permissions, hooks, sessions) |
| Skills | `src/paa/skills/` | Unified Skill Adapter, contracts, secrets broker, adapters |
| Marketplace | `src/paa/marketplace/` | Signed packages, five-gate installer, registry clients |
| Observability | `src/paa/observability/` | Logging, metrics, tracing, execution runs (untested) |
| Daemon | `src/paa/daemon.py` | Dispatch, ingestion, maintenance loops |
| CLI | `src/paa/cli/main.py` | `paa submit/run/status/ledger/recover/doctor/serve/agent/mode/tui` |
| API | `src/paa/api/` | FastAPI ingestion edge (loopback only, port 8787) |
| TUI | `src/paa/tui/` | Interactive dashboard |

Primary entry points:

* `paa submit "goal"` — submit and run one task to completion (`src/paa/cli/main.py:61`)
* `paa serve` — FastAPI API + background daemon (`src/paa/cli/main.py:248`)
* `paa doctor` — report live backends/sandbox (`src/paa/cli/main.py:208`)
* `paa recover` — post-crash boot sweep (`src/paa/cli/main.py:190`)
* `paa ledger <id>` — walk a task's event chain (`src/paa/cli/main.py:134`)
* `paa agent "prompt"` — tool-using coder loop (`src/paa/cli/coder.py:44`)

The composition root is `Runtime.build()` (`src/paa/runtime.py:231`).

## B. Start here

- [Architecture overview](architecture/overview.md) — design patterns, the ledger-is-truth invariant, degradation philosophy.
- [Repository map](architecture/repository-map.md) — directory tree, module ownership, entry points.
- [Component interactions](architecture/component-interactions.md) — how major subsystems connect and depend on one another.
- [Configuration and environment](operations/configuration-and-environment.md) — every `PAA_*` setting, defaults, precedence, consumers.
- [Developer setup and workflow](development/developer-workflow.md) — install, lint/typecheck/test commands.
- [Execution flows](flows/) — traced workflows: submit-and-run, human gate, crash recovery.

## C. Documentation map (by area)

Architecture
- [architecture/overview.md](architecture/overview.md)
- [architecture/repository-map.md](architecture/repository-map.md)
- [architecture/component-interactions.md](architecture/component-interactions.md)

Backend (agents, orchestration, policy, validation)
- [backend/overview.md](backend/overview.md)
- [backend/modules-and-services.md](backend/modules-and-services.md)
- [backend/request-lifecycle.md](backend/request-lifecycle.md)

Module-specific deep dives
- [modules/ledger.md](modules/ledger.md)
- [modules/agents.md](modules/agents.md)
- [modules/context.md](modules/context.md)
- [modules/memory.md](modules/memory.md)
- [modules/models.md](modules/models.md)
- [modules/sandbox.md](modules/sandbox.md)
- [modules/validation.md](modules/validation.md)
- [modules/coder.md](modules/coder.md)
- [modules/skills.md](modules/skills.md)
- [modules/marketplace.md](modules/marketplace.md)
- [modules/storage.md](modules/storage.md)
- [modules/observability.md](modules/observability.md)
- [modules/daemon.md](modules/daemon.md)
- [modules/cli.md](modules/cli.md)

Data and storage
- [data/data-models-and-relationships.md](data/data-models-and-relationships.md)
- [data/persistence-and-storage.md](data/persistence-and-storage.md)
- [data/data-lifecycle.md](data/data-lifecycle.md)

APIs and integrations
- [interfaces/api-reference.md](interfaces/api-reference.md)
- [interfaces/integrations-and-communication.md](interfaces/integrations-and-communication.md)

Execution flows
- [flows/workflow-01-submit-and-run.md](flows/workflow-01-submit-and-run.md)
- [flows/workflow-02-human-gate.md](flows/workflow-02-human-gate.md)
- [flows/workflow-03-crash-recovery.md](flows/workflow-03-crash-recovery.md)

Security
- [security/security-model-and-trust-boundaries.md](security/security-model-and-trust-boundaries.md)

Operations
- [operations/configuration-and-environment.md](operations/configuration-and-environment.md)
- [operations/deployment-and-runtime.md](operations/deployment-and-runtime.md)

Testing and development
- [development/testing-and-quality.md](development/testing-and-quality.md)
- [development/developer-workflow.md](development/developer-workflow.md)

Review
- [reviews/documentation-audit.md](reviews/documentation-audit.md)

Existing (pre-existing) documents retained in `docs/`:
- [docs/coder-agent.md](../coder-agent.md) — the `paa agent` tool loop.
- [docs/EXTENSIBILITY_PIPELINES.md](../EXTENSIBILITY_PIPELINES.md) — pluggable agentic loops design.
- [docs/ROADMAP.md](../ROADMAP.md), [docs/VISION.md](../VISION.md)
- [docs/deployment/docker.md](../deployment/docker.md)
- [docs/adr/README.md](../adr/README.md) and ADRs 0008, 0010, 0015, 0019, 0020.

## D. How to trace a feature

1. Find the entry point: a CLI command (`src/paa/cli/main.py`), an API route
   (`src/paa/api/app.py`), or `Runtime.submit`/`Runtime.run` (`src/paa/runtime.py`).
2. Follow the call into `ChiefOrchestrator` (`src/paa/agents/orchestrator.py`), which writes
   the ledger event *before* doing the work.
3. Identify which agent handles the phase (`context_builder`, `planner`, `policy`,
   `worker`, `critic`, `router`) and what it calls downstream (storage, model router,
   sandbox, validator).
4. Note what is persisted (ledger event, recovery mark, queue message) and what is
   derived (context packet, projection, indexes).
5. Follow errors: every `PaaError` subclass carries structured `details` written to the
   ledger; terminal events close a lineage (`core/types.py:132`).

When modifying a component, check its [module page](modules/) for upstream callers and
downstream consumers, then update the corresponding flow trace and this index.

## E. Verification and known gaps

- Subsystems with **zero test coverage**: `src/paa/observability/` (exercised nowhere),
  `tests/validation/` (empty package). See AGENTS.md and
  [development/testing-and-quality.md](development/testing-and-quality.md).
- `docs/adr/README.md` indexes ADRs 0001–0020 but only 0008, 0010, 0015, 0019, 0020
  exist on disk (AGENTS.md "Known-stale references").
- `src/paa/agents/specialists/` is an empty package.
- The marketplace/skills/coder layers import cleanly but are not yet wired into the
  orchestrator's main task path (no built-in file-edit/bash/web skills).
- Six pre-existing environmental test failures (sandbox runs `python` where only
  `python3` exists); all other tests pass.

The [documentation audit](reviews/documentation-audit.md) records coverage, unresolved
questions, and remaining gaps.
