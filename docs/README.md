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
| Observability | `src/paa/observability/` | Logging, metrics, tracing, execution runs |
| Daemon | `src/paa/daemon.py` | Dispatch, ingestion, maintenance loops |
| CLI | `src/paa/cli/main.py` | `paa submit/run/status/ledger/recover/doctor/serve/agent/mode/tui` |
| API | `src/paa/api/` | FastAPI ingestion edge (loopback only, port 8787) |
| TUI | `src/paa/tui/` | Interactive dashboard |

Primary entry points:

* `paa submit "goal"` — submit and run one task to completion (`src/paa/cli/main.py:76`)
* `paa serve` — FastAPI API + background daemon (`src/paa/cli/main.py:292`)
* `paa doctor` — report live backends/sandbox (`src/paa/cli/main.py:227`)
* `paa recover` — post-crash boot sweep (`src/paa/cli/main.py:209`)
* `paa ledger <id>` — walk a task's event chain (`src/paa/cli/main.py:147`)
* `paa agent "prompt"` — tool-using coder loop (`src/paa/cli/coder.py:56`)

The composition root is `Runtime.build()` (`src/paa/runtime.py:260`).

## B. Start here

- [Architecture overview](architecture/overview.md) — design patterns, the ledger-is-truth invariant, degradation philosophy.
- [Repository map](architecture/repository-map.md) — directory tree, module ownership, entry points.
- [Component interactions](architecture/component-interactions.md) — how major subsystems connect and depend on one another.
- [Configuration and environment](operations/configuration-and-environment.md) — every `PAA_*` setting, defaults, precedence, consumers.
- [Developer setup and workflow](development/developer-workflow.md) — install, lint/typecheck/test commands.
- [Execution flows](flows) — traced workflows: submit-and-run, human gate, crash recovery.

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
- [modules/memory.md](modules/memory.md) — VectorEmbeddingSink wired with drain of the pending-embedding backlog, touch-on-recall retention (`last_queried_at`/`use_count`), `MemoryCurator` scheduled in the daemon, extractor drop counters, explicit-only `prune_confidence_floor` / deprecated `decay_lambda`; end-to-end (restart + paraphrase recall) proof in `tests/memory/test_e2e_remember_recall.py`. Run summary in [SUMMARY.md](agent-ops/20261106-memory-remembering-agent/SUMMARY.md); operator runbook *Memory curation* in [deployment-and-runtime](operations/deployment-and-runtime.md).
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
- [operations/deployment-and-runtime.md](operations/deployment-and-runtime.md) — authoritative *Memory curation* runbook (full curator, first-tick preview, snapshot-before-delete, cold-lake replay restore).
- [runtime/background-jobs-and-events.md](runtime/background-jobs-and-events.md) — daemon loops and event wiring.

Glossary
- [glossary.md](glossary.md)

Testing and development
- [development/testing-and-quality.md](development/testing-and-quality.md)
- [development/developer-workflow.md](development/developer-workflow.md)

Review
- [reviews/documentation-audit.md](reviews/documentation-audit.md)

Existing (pre-existing) documents retained in `docs/`:
- [coder-agent.md](coder-agent.md) — the `paa agent` tool loop.
- [EXTENSIBILITY_PIPELINES.md](EXTENSIBILITY_PIPELINES.md) — pluggable agentic loops design.
- [ROADMAP.md](ROADMAP.md), [VISION.md](../VISION.md) (`VISION.md` lives at the repository root).
- [deployment/docker.md](deployment/docker.md)
- [adr/README.md](adr/README.md) — ADR index (0001–0018 and 0019–0026, 0028–0035; 0027 reserved); bodies on disk: 0008, 0010, 0015, 0019–0026, 0028–0035.
- [memory-polish/plans/Plan1.md](memory-polish/plans/Plan1.md) — dated memory-polish proposal (in scope for the `path:line` lock).
- [MCP_AND_PLUGINS.md](<plans and audits/MCP_AND_PLUGINS.md>), [flexability-audit.md](<plans and audits/flexability-audit.md>), [w3-w5.md](<plans and audits/w3-w5.md>), [w6-w7-w9.md](<plans and audits/w6-w7-w9.md>) — dated proposals/audits (in scope for the `path:line` lock).

Aspirational RFC proposals at `docs/` root (vision docs, not normative build specs; no ADR adopts them):
- [A Disciplined System for Computational Attention Allocation.md](<A Disciplined System for Computational Attention Allocation.md>) — attention-allocation vision; its scorer formula deviates from the live implementation per ADR-0012 (index-only; see [SUMMARY.md](agent-ops/20261106-memory-remembering-agent/SUMMARY.md) §8, do not treat the doc's formula as normative).
- [Core Architecture Specification and Production Runtime RFC.md](<Core Architecture Specification and Production Runtime RFC.md>) — runtime RFC v4.0+v4.1 in one file; not fully integrated (21 specialists empty, markdown vault 11/13 missing; see [SUMMARY.md](agent-ops/20261106-memory-remembering-agent/SUMMARY.md) §8, do not treat the doc as a normative spec).

Agent-ops run 20261106-memory-remembering-agent (run trail, excluded from the `path:line` conventions lock):
- [SUMMARY.md](agent-ops/20261106-memory-remembering-agent/SUMMARY.md) — run outcome (C01–C07 closed; §K install/launch not done at summary time).
- [01-work-ledger.md](agent-ops/20261106-memory-remembering-agent/01-work-ledger.md) — per-wave dispositions; gates under `agent-ops/20261106-memory-remembering-agent/gates/`.

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
   ledger; terminal events close a lineage (`core/types.py:176`).

When modifying a component, check its [module page](modules) for upstream callers and
downstream consumers, then update the corresponding flow trace and this index.

## E. Verification and known gaps

- Test suites on disk: `tests/observability/` (`test_logging`, `test_metrics`, `test_runs`, `test_tracing`) and `tests/validation/` (suite including the 19-test schema validator) — the former zero-coverage note is stale; `docs/operations/deployment-and-runtime.md` still describes `src/paa/observability/` as untested.
- `docs/adr/README.md` indexes ADRs 0001–0018 and 0019–0026, 0028–0035 (0027 reserved) but only 0008, 0010, 0015, 0019–0026 and 0028–0035 have bodies on disk ("Known-stale references" in AGENTS.md).
- `src/paa/agents/specialists/` is an empty package.
- The marketplace/skills/coder layers import cleanly but are not yet wired into the
  orchestrator's main task path (no built-in file-edit/bash/web skills).
- Windows-host baseline for the 20261106-memory-remembering-agent run: `mypy` 3 pre-existing errors (`sandbox/subprocess_backend.py:548`, win32) and 12 pre-existing pytest failures (see `00-mission.md` in the run folder; Linux CI gate is zero per AGENTS.md). Scoped gates for C01–C07 were green; the full `pytest tests/` suite was not run to completion in the final gate (see the run [SUMMARY.md](agent-ops/20261106-memory-remembering-agent/SUMMARY.md) §5/§14).

The [documentation audit](reviews/documentation-audit.md) records coverage, unresolved
questions, and remaining gaps.
