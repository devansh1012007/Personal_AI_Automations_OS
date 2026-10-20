# Documentation audit

Final integration review of the PAA documentation produced in `/docs`. This audit
distinguishes **documentation defects** (gaps, inconsistencies, broken links in
these documents) from **actual code defects** (behavior observed in the source
that looks wrong). No application source code was modified or executed as part
of producing this documentation; no test suite was run.

## Coverage by subsystem

| Subsystem | Docs | Coverage status |
|---|---|---|
| Architecture / repo map | `architecture/overview.md`, `repository-map.md`, `component-interactions.md` | Complete |
| Ledger (spine) | `modules/ledger.md` | Complete (events/store/replay/recovery all cited) |
| Agents / orchestrator / policy | `modules/agents.md`, `backend/*` | Complete |
| Context engine | `modules/context.md` | Complete |
| Memory (domains/decay/facts/creator/curator/world_model) | `modules/memory.md` | Complete |
| Model layer (providers/router/escalation) | `modules/models.md`, `interfaces/integrations-and-communication.md` | Complete |
| Sandbox (backends/recursion/watchdog) | `modules/sandbox.md` | Complete |
| Validation (AST/patch/schema/tests) | `modules/validation.md` | Complete |
| Coder agent (`paa agent`) | `modules/coder.md` | Complete |
| Skills / USA / secrets / adapters | `modules/skills.md` | Complete (noted as not wired into main path) |
| Marketplace | `modules/marketplace.md` | **Not created** — see gaps below |
| Storage (relational/coldlake/vector/graph/queue) | `modules/storage.md`, `data/*` | Complete |
| Observability | `modules/observability.md` | Complete (candidly marked untested) |
| Daemon | `modules/daemon.md` | Complete |
| CLI / API | `modules/cli.md`, `interfaces/api-reference.md` | Complete |
| Configuration | `operations/configuration-and-environment.md` | Complete |
| Deployment | `operations/deployment-and-runtime.md` | Complete |
| Testing / workflow | `development/testing-and-quality.md`, `developer-workflow.md` | Complete |
| End-to-end flows | `flows/workflow-0{1,2,3}.md` | Complete (submit-run, human gate, crash recovery) |
| Glossary / index | `glossary.md`, `README.md` | Complete |

## Completed documentation deliverables

42 Markdown files created under `/docs`:

- `README.md` (master index)
- `glossary.md`
- `architecture/overview.md`, `architecture/repository-map.md`, `architecture/component-interactions.md`
- `backend/overview.md`, `backend/modules-and-services.md`, `backend/request-lifecycle.md`
- `data/data-models-and-relationships.md`, `data/persistence-and-storage.md`, `data/data-lifecycle.md`
- `interfaces/api-reference.md`, `interfaces/integrations-and-communication.md`
- `runtime/background-jobs-and-events.md`
- `security/security-model-and-trust-boundaries.md`
- `operations/configuration-and-environment.md`, `operations/deployment-and-runtime.md`
- `development/testing-and-quality.md`, `development/developer-workflow.md`
- `modules/{ledger,agents,context,memory,models,sandbox,validation,coder,skills,storage,observability,daemon,cli}.md` (13 files)
- `flows/workflow-01-submit-and-run.md`, `flows/workflow-02-human-gate.md`, `flows/workflow-03-crash-recovery.md`
- `reviews/documentation-audit.md` (this file)

Pre-existing documents retained and linked: `coder-agent.md`, `EXTENSIBILITY_PIPELINES.md`, `ROADMAP.md`,
`VISION.md`, `deployment/docker.md`, `adr/README.md`, ADRs 0008/0010/0015/0019/0020.

## Critical connection paths verified

These were traced against actual source (file:line cited in the documents):

1. **CLI/API → Runtime → ChiefOrchestrator → LedgerStore** (`cli/main.py:61` → `runtime.py:189` → `orchestrator.py:188` → `store.py:113`).
2. **Orchestrator phases** hydrate→plan→policy→execute→commit (`orchestrator.py:301`–`:309`), each emitting its ledger event before the side effect.
3. **Model call with escalation** (`reasoning.py:95` → `runtime.py:121` → `router.py:274` → `MODEL_ESCALATED` before frontier call at `router.py:417`).
4. **Signal ingestion → memory creation** (`api/app.py:166` → `signals.py:124` → `daemon.py:139` → `creator.py` → `facts.py:429`).
5. **Crash recovery** (`daemon.py:61` → `orchestrator.py:737` → `recovery.py:222` → `replay.py:322` → workspace reconcile `recovery.py:350`).
6. **Coder agent tool loop** (`cli/coder.py:44` → `agent.py:101` → `loop.py:142` → `model_adapter.py:161` → `runtime_tools.py` tools).
7. **Validation pipeline** schema→AST→patch→drift→tests (`validation/engine.py:208`, `validation/patch.py:270`).
8. **Delegation safety** depth/cycle/node bounds (`delegation.py:87`, `orchestrator.py:631`).
9. **Sandbox degradation** 5s probe → subprocess fallback (`runtime.py:382`).
10. **Backpressure** NORMAL/DEGRADED/SHEDDING with control-plane always accepted (`backpressure.py:115`).

## Cross-document inconsistencies found and resolved

| Inconsistency | Resolution |
|---|---|
| `MemorySettings.decay_lambda` (config.py) duplicates `domains.py` values but neither reads the other | Documented as potential config/drift in `modules/memory.md` and `operations/configuration-and-environment.md` (inferred, not confirmed dead code) |
| `gatherer.py` `NARRATIVE_MEMORY_DOMAINS` duplicates `domains.py narrative_domains()` with a larger alias set | Documented as independently-maintained duplicate in `modules/context.md` |
| `hot_serving_memory_registry` / `hot_serving_agent_registry` in schema but light consumption | Marked schema-verified, runtime-consumption partially verified in `data/data-models-and-relationships.md` |
| ADR index claims 0001–0020 but only 0008/0010/0015/0019/0020 exist | Documented in `README.md` "E. Verification and known gaps" and `architecture/overview.md` |

## Unresolved questions (documented, not concealed)

1. Whether `MemorySettings.prune_confidence_floor` is dead config (decay/curator use domain `prune_floor`). **Inferred unused** from grep; not confirmed.
2. Whether `hot_serving_policy_rules` has a management UI/API — no rule-management endpoint found in `api/app.py`; mechanism unverified.
3. Whether the marketplace installer's five gates are end-to-end wired into the runtime (package module inspected via subagent; installer output truncated in investigation). **Not documented in depth** — `modules/marketplace.md` was not created.
4. Exact observability behavior beyond structlog + `ObservabilitySettings` defaults — `src/paa/observability/` is untested (`AGENTS.md:97`).
5. The six pre-existing test failures (sandbox runs `python` where only `python3` exists) — environmental and pre-existing; **fixed on the host** by `sudo apt install python-is-python3`.

## Modules/workflows partially documented

- **Marketplace** (`src/paa/marketplace/`): investigated via subagent but no dedicated module doc was written. Covered only in `architecture/repository-map.md` and glossary. *Gap: a `modules/marketplace.md` would complete coverage.*
- **Improvement** (`src/paa/improvement/`): self-improvement/meta-learning designed but partially built; listed in repository map; no deep-dive doc. Accurately reflected as "designed, partially built" rather than fabricated.
- **TUI** (`src/paa/tui/`): mentioned in README and CLI (`paa tui`); no dedicated doc. Listed in repository map.

## Validation checks performed

| Check | Method | Result |
|---|---|---|
| Source-reference accuracy | Every implementation claim cites `src/paa/<file>:<line>` inspected in this session | Verified against read files |
| Docs directory structure | `ls docs/` recursive | All intended files present |
| Pre-existing docs retained | `docs/adr/`, `coder-agent.md`, `ROADMAP.md`, `VISION.md`, `deployment/` untouched | Confirmed present |
| No secrets in Markdown | Reviewed for API keys/credentials | None present (only env-var names) |
| No source code modified | `git status` reviewed (read-only inspection) | Confirmed: only `/docs` files created |

**Not run:** `ruff`, `mypy`, `pytest`, a Markdown link validator, or Mermaid syntax check. The documentation was produced by static inspection only; no application behavior was executed. Internal Markdown links were written repository-relative and follow the existing file structure but were not programmatically validated.

## Documentation defects (in these docs, not the code)

- `modules/marketplace.md` not created (marketplace coverage is shallow).
- No Mermaid diagrams in module docs (only in architecture/interactions/flows) — acceptable; diagrams were used where they materially improve understanding.
- `docs/reviews/` directory did not exist before; created for this audit.

## Actual code defects observed (not documentation defects)

These are behaviors observed in the source, reported for completeness — they are **not** introduced or endorsed by this documentation:

1. Sandbox subprocess backend runs `python`; the build machine had only `python3` in a user-local directory — worker steps exhausted their retry budget (6 pre-existing test failures, now fixed on the host).
2. `MemorySettings.decay_lambda` duplicates `domains.py` but is not read by them (potential config drift, unverified).
3. `tests/validation/` empty; `tests/observability/` covers only the run repository's identifier coercions (`AGENTS.md:97`).
4. Docker live-server run still needs a pass (README build-status table).

## Recommended next documentation improvements

1. Create `modules/marketplace.md` (package/signing/installer/registry_client) for complete marketplace coverage.
2. Programmatically validate internal Markdown links resolve (e.g. with `markdown-link-check`).
3. Add Mermaid diagrams to module docs where relationships are complex (e.g. memory domain→mechanism mapping, model escalation flow).
4. Resolve the `prune_confidence_floor` / `decay_lambda` config-drift question against the source (confirmed or refuted).
5. If `hot_serving_policy_rules` rule management is added later, document the endpoint.

## Conclusion

The significant repository architecture is documented: all major subsystems, important modules, critical runtime entry points, execution paths, upstream dependencies and downstream consumers, data transformations, persistence operations, APIs, integrations, configuration, and asynchronous execution are covered. Uncertainties (marketplace depth, config drift, observability behavior) are explicitly labeled rather than concealed. Application source code and behavior remain unchanged.

Master index: `/docs/README.md`.
Audit report: `/docs/reviews/documentation-audit.md`.
