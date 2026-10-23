# Backend modules and services

Each backend module's internal implementation, public interfaces, inputs,
outputs, dependencies, consumers, and failure behavior. All paths are under
`src/paa/`.

## 1. Configuration — `config.py`

**Responsibility:** Holds every RFC constant and runtime setting with its RFC
section cited. Pydantic-settings `Settings` with prefix `PAA_`, nested delimiter
`__`, `.env` read from CWD.

**Public interface:** `Settings` (with sub-models `StorageSettings`,
`ContextSettings`, `MemorySettings`, `PolicySettings`, `SandboxSettings`,
`ModelSettings`, `QueueSettings`, `ObservabilitySettings`), `get_settings()`
(`lru_cache`d singleton), `reset_settings_cache()`, `reset_dotenv_cache()`,
`dotenv_value(name)`.

**Key behaviors:**
- `dotenv_value` parses `.env` **without** writing to `os.environ` — provider
  API keys are named per registry row, not stored as settings fields
  (`config.py:460`).
- `_enforce_loopback` refuses non-loopback `api_host` unless
  `PAA_ALLOW_NON_LOOPBACK=1` (`config.py:409`).
- `_propagate_home` re-roots storage paths when `home` is overridden directly
  (`config.py:425`).
- `ensure_directories()` creates every directory the runtime needs (`config.py:450`).

**Configuration precedence:** explicit kwargs > environment (`PAA_*`) >
`.env` file > defaults (`config.py:7`).

**Consumers:** `Runtime.build()` (`runtime.py:256`), `PolicyRiskAgent`
(`policy.py:132`), `BackpressureController` (`backpressure.py:86`), model router
(`router.py:143`), and essentially every subsystem.

**Failure behavior:** Settings validation errors (`ValueError` from pydantic)
surface at `Settings()` construction — e.g. a non-loopback `api_host` without
the override flag prevents boot.

## 2. Ledger — `ledger/`

### `events.py` — event envelope
**Responsibility:** `LedgerEvent` (frozen pydantic model), `canonical_json`,
idempotency key (`SHA-256(correlation_id|event_type|attempt|discriminator)`),
event digest binding each event to its predecessor, `GENESIS_HASH`.

**Public interface:** `LedgerEvent`, `canonical_json`, `compute_idempotency_key`,
`compute_event_digest`, `GENESIS_HASH`.

**Failure behavior:** `idempotency_key` rejects negative `attempt`
(`events.py:99`). `recorded_at` must be tz-aware (`events.py:174`).

### `store.py` — append-only store
**Responsibility:** `LedgerStore.append()` — inside one transaction:
suppress duplicates by idempotency key, assign next per-correlation
`state_version`, link into the hash chain, insert the row, update the head
projection. Atomic (`ledger/store.py:12`).

**Public interface:** `LedgerStore.append/append_many/read_correlation/latest_event/
head/open_correlations/events_since/count/verify_chain/verify_all_chains/
save_snapshot/load_snapshot`; `CorrelationHead`.

**Failure behavior:** Duplicate idempotency key returns the *existing* event
rather than raising (`ledger/store.py:144`). Concurrent-writer race re-reads and
returns whatever landed (`ledger/store.py:174`).

### `replay.py` — deterministic projection
**Responsibility:** Folds events into a `TaskProjection`. Pure and total: never
raises on unknown event types (records them in `unknown_events` instead).

**Public interface:** `TaskProjection`, `TaskPhase`, `apply_event`, `replay`,
`project`.

**Key logic:** `_PHASE_MAP` maps each `EventType` to a `TaskPhase`; event-specific
state (plan steps, policy decision, attempts, completed steps, tokens) is
applied in `apply_event`'s `match` block (`replay.py:170`).

### `recovery.py` — crash recovery
**Responsibility:** `RecoveryEngine.boot_sweep()` — replays open lineages from
the head projection (never the queue), reconciles workspace vs manifest,
restores drift, re-queues resumable tasks, parks human-gated tasks.

**Public interface:** `RecoveryEngine`, `RecoveryReport`, `RecoveryOutcome`,
`WorkspaceSnapshotter`/`Requeuer` protocols.

**Failure behavior:** One bad lineage does not abort the sweep
(`ledger/recovery.py:250`). Recovery is idempotent — running twice yields the same end
state (`recovery.py:17`).

## 3. Agents — `agents/`

### `base.py` — agent foundation
**Responsibility:** `Agent` ABC (`handle` abstract), `AgentContext` (budget,
permissions, recursion depth), `AgentMessage` (typed envelope), `AgentResult`
(structured outcome), `MessageType`, `RiskLevel`. `Agent.run()` applies
cross-cutting guarantees: permission checks, expiry, timing, budget accounting,
exception→structured-failure conversion. `Agent.delegate()` routes through the
orchestrator's `DelegationRegistry` (never peer-to-peer).

**Public interface:** `Agent`, `AgentContext`, `AgentMessage`, `AgentResult`,
`MessageType`, `RiskLevel`.

**Failure behavior:** Missing permissions → `AgentResult.failure` with
`PermissionDeniedError` (`agents/base.py:333`). Unexpected exceptions are caught and
returned as structured failures (`agents/base.py:355`). Token overspend raises
`BudgetExceededError` (`agents/base.py:245`).

### `orchestrator.py` — Chief Orchestrator
**Responsibility:** Owns the task lifecycle. Emits ledger events before every
side effect. Phases: `_hydrate` → `_plan` → `_check_policy` →
`_execute_steps` → `_commit`. Mediates delegation (`mediate_delegation`),
clears human gates (`clear_human_gate`), records corrections (`record_correction`),
boots recovery (`boot`).

**Public interface:** `ChiefOrchestrator`, `TaskRequest`, `TaskOutcome`.

**Key logic:**
- `submit()` writes `TASK_REQUESTED` then `TASK_QUEUED`, applies backpressure,
  enqueues to `StreamName.ORCHESTRATOR_CORE` (`orchestrator.py:188`).
- `run()` drives a submitted task to terminal; returns early if already
  terminal (`orchestrator.py:292`).
- `_execute_steps` retries each step up to `max_step_retries` (default 2)
  (`orchestrator.py:523`).
- Delegation graph enforces depth/cycle/node bounds per correlation
  (`orchestrator.py:631`, `delegation.py:87`).

**Failure behavior:** `PaaError` during execution → `EXECUTION_FAILED` event,
then re-project (`orchestrator.py:310`).

### `reasoning.py` — planner, critic, router, worker
**Responsibility:** `StrategicPlanner` compiles goals into steps (model or
deterministic fallback). `CriticReviewer` reviews output (deterministic first,
model second, model can only downgrade). `TaskRouter` optional request router
(bypassed when a target agent is named or <3 eligible agents). `WorkerCell`
executes one step in a sandbox.

**Public interface:** `ModelLike` protocol, `StrategicPlanner`, `CriticReviewer`,
`TaskRouter`, `WorkerCell`.

**Failure behavior:** Planner retries once on schema failure then gives up
(`reasoning.py:93`). Critic short-circuits on deterministic failure without
consulting the model (`reasoning.py:226`). Model outage degrades to
deterministic path (`reasoning.py:262`).

### `policy.py` — PolicyRiskAgent
**Responsibility:** Deterministic risk gate; **no model consulted**. Checks:
permissions, lockdown (network under LOCKDOWN), irreversible operations under
SAFE, path traversal/forbidden roots, operator rules (regex), anti-goals
(cosine similarity or phrase match). Scores risk [0,1]; decides human gate need.

**Public interface:** `PolicyRiskAgent`, `PolicyVerdict`.

**Failure behavior:** Malformed operator rules are logged and skipped, not
fatal (`policy.py:243`). Anti-goal embedding failure → blocked rather than
proceeding unchecked (`policy.py:286`).

### `delegation.py` — Delegation graph
**Responsibility:** `DelegationGraph` per correlation enforces depth, cycle,
node bounds. `DelegationRegistry` holds graphs keyed by correlation.

**Public interface:** `DelegationEdge`, `DelegationGraph`, `DelegationRegistry`.

**Failure behavior:** `propose()` raises `RecursionGuardError` on any breach
(`delegation.py:87`); `release()` removes completed edges so stale edges
don't cause false cycles (`delegation.py:123`).

### `context_agents.py` — context builders
**Responsibility:** `PlannerContextBuilder` (strategic context, all domains)
and `WorkerContextBuilder` (execution context, narrative domains blocked,
no-holes hydration). Both use `BoundedContextGatherer` + `QueryExpander`.

**Public interface:** `PlannerContextBuilder`, `WorkerContextBuilder`,
`QueryExpander`.

**Failure behavior:** Expander failure degrades to deterministic `_KeywordExpander`
(`context_agents.py:122`). Missing required primitives → `hydration_required`
failure rather than dispatching an unprepared worker (`context_agents.py:303`).

## 4. Storage — `storage/`

### `storage/relational/database.py` — Database
**Responsibility:** Async SQLite access layer. Single write connection guarded
by `asyncio.Lock`; pooled read-only connections (size 4); WAL mode; applies
`schema_sqlite.sql` on connect; tracks `schema_migrations`.

**Public interface:** `Database`, `connect/close/transaction/fetch_all/fetch_one/
fetch_value/execute/execute_many/vacuum/analyze/integrity_check/table_names`;
`utc_now`, `to_iso`, `from_iso`, `dumps`, `loads`.

**Failure behavior:** `transaction()`/`_reader()` require a connected
connection or raise `StorageError` (`database.py:214`). `close()` runs
`PRAGMA wal_checkpoint(TRUNCATE)` so a single `.db` file copy is a complete
backup (`database.py:132`).

### `storage/coldlake/` — content-addressed blob store + signals
**Responsibility:** `ContentAddressedStore` — immutable zstd-compressed blobs
addressed by SHA-256; verifies hash on read. `SignalRepository` — raw signal
intake, idempotent on `(channel, external_id)`, oversized payloads offloaded to
CAS.

**Public interface:** `ContentAddressedStore.put/get/stat/exists/delete`,
`SignalRepository.record/claim_unprocessed/mark_processed/mark_malformed/
mark_quarantined/release`.

**Failure behavior:** Hash mismatch on `get` → `StorageError` (`cas.py:311`).
Non-JSON/non-UTF-8 payload → `StorageError` before INSERT
(`storage/coldlake/signals.py:410`). Idempotency race on `record()` re-fetches incumbent
(`storage/coldlake/signals.py:142`).

### `storage/vector/` — vector recall
**Responsibility:** `get_vector_store(settings)` selects Qdrant (embedded or
server) or brute-force numpy. `"auto"` degrades to numpy with a warning.

**Public interface:** `get_vector_store`, `get_embedder`, `VectorStore` protocol.

**Failure behavior:** Never raises in `auto` mode (`storage/vector/__init__.py:141`).
O(n) per query in numpy fallback; fine to ~100k points.

### `storage/graph/` — graph store
**Responsibility:** `get_graph_store(settings, db)` selects Kuzu or SQLite
recursive-CTE fallback.

**Public interface:** `get_graph_store`, `GraphStore` protocol.

**Failure behavior:** `"auto"` falls back to SQLite if kuzu unavailable; explicit
`"kuzu"` raises `ImportError` if absent (`storage/graph/__init__.py:99`).

### `storage/queue/` — durable queue + locks
**Responsibility:** `get_queue`/`get_lock` — SQLite default, Redis optional.
At-least-once delivery; exactly-once processing via ledger idempotency keys.

**Public interface:** `MessageQueue`/`DistributedLock` ABCs, `QueueMessage`,
`StreamName` (11 streams), `BackpressureController`.

**Failure behavior:** SQLite backend requires a connected `Database`
(`StorageError` if `db is None`). Redis backend imported lazily.

### `storage/queue/backpressure.py` — backpressure
**Responsibility:** Pure decision logic: NORMAL / DEGRADED (degrade modality one
rung) / SHEDDING (refuse discretionary ingestion). Control-plane streams always
accepted.

**Public interface:** `BackpressureController.assess/plan/should_accept/
degrade_modality`.

## 5. Context & memory — `context/`, `memory/`

### `context/gatherer.py` — BoundedContextGatherer
**Responsibility:** Enforces the hard token ceiling on context packets. Ranks
candidates by `(tier, -(relevance*confidence), -importance, id)` (deterministic
tiebreak on `id`). Packs until budget exhausted; invariants are gated on
`try_consume`. Hard assertion `allocated_tokens <= ceiling`.

**Public interface:** `BoundedContextGatherer.compile/compile_worker_packet`.

**Failure behavior:** Raises `AssertionError` if the ceiling is exceeded
(belt-and-braces on the DoD bound) (`gatherer.py:565`).

### `context/budget.py` — TokenBudget
**Responsibility:** Token budget with sub-budget halving per delegation depth
(RFC §15.7: `allocated >> depth`), capped at parent `remaining`.

**Public interface:** `TokenBudget.try_consume/child/remaining`.

### `memory/domains.py` — 18 domains over 6 mechanisms
**Responsibility:** `MemoryDomain` enum (18 members), `MemoryMechanism`
(VOLATILE/RELATIONAL/IMMUTABLE_LOG/VECTOR/GRAPH/DOCUMENT), `DomainPolicy`
(decay_lambda, prune_floor, immutable, narrative). `DOMAINS` matrix
(RFC §4.1). `narrative_domains()` returns `{narrative, reflection, strategic,
identity}`.

**Public interface:** `domain_policy()`, `narrative_domains()`, `DOMAINS`.

### `memory/decay.py` — DecaySweeper
**Responsibility:** `C(t) = C₀·e⁻ˡᵃᵐᵇᵈᵃ·t` decay; evicts facts below prune floor
using keyset pagination (correct under concurrent deletion — OFFSET would skip
rows). Archives doomed facts to cold lake before deleting.

**Public interface:** `DecaySweeper.sweep()`, `effective_confidence()`.

### `memory/facts.py` — FactRepository
**Responsibility:** `upsert_entity`, `add_fact` (appends, never overwrites;
`supersede` links versions), `resolve_entity` (exact → alias → fuzzy),
`query_facts` (decayed confidence filtered in Python), supersession chains.

`resolve_entity` is a three-rung ladder in which the **Python gate is the
authority**: a character-trigram Jaccard similarity over the 3-grams of the
raw, case-folded text — FTS5's own gram model — decides, and the database
only decides which rows the gate gets to look at. For **ASCII corpora** the
candidate set is a
**superset** of the gate's acceptance set: a **phrase** query (the term is a
substring of the row's text) with a **broad** fallback (the row shares at
least one of the term's grams — the gate's own precondition), the broad one
running when the phrase rows do not clear the floor. Candidates are ordered
by a **portable total order** — the count proxy `DESC`, then the unique
`e.id ASC` — so the cut is the same on both substrates and the tie-break is
deterministic. For an ASCII corpus SQLite and PostgreSQL return the same
entity, match kind and score; outside it the parity is **bounded** — see the
known bound below ([**ADR-0029**](../adr/0029-entity-resolution-parity.md)).

**Public interface:** `FactRepository`, `Fact`, `Entity`.

**Failure behavior:** a candidate query the engine refuses → returns `[]`
(failing closed), so the fuzzy rung answers "no match" (`facts.py`). A term
carrying a NUL is refused before any statement is issued, on both
substrates (`facts.py:482`); so is a term with more than `_MAX_TERM_GRAMS`
grams (`facts.py:135`, refused at `facts.py:508`), because the count proxy's
`CASE` sum would otherwise hit SQLite's expression-depth limit and be refused
as "expression tree is too large" — which is not a `QueryRejected` and would
escape the ladder.
`supersede` refuses cycles (`memory/facts.py:881`).

**Known bound:** the parity is exact for ASCII and **bounded** outside it.
The candidate query folds the *column* with the engine's `lower()`
(ASCII-only on SQLite, locale-aware on PostgreSQL), so the divergence is
one-directional — PostgreSQL sees rows SQLite cannot — and for the same
entity the two substrates can report a different *match kind* and score
(measured: `"İstanbul"` is `exact` at 1.0 on PostgreSQL, `fuzzy` at 0.625 on
SQLite). The alias column is JSON text, so `json.dumps` escapes a non-ASCII
alias and the count proxy cannot see it. All three need a stored
Python-computed normalisation to close
([**ADR-0029**](../adr/0029-entity-resolution-parity.md)).

### `memory/creator.py`, `curator.py`, `contradiction.py`, `world_model.py`
- `MemoryCreator.run_batch()` — ingests signals into facts (`daemon.py:148`).
- `MemoryCurator` — nightly consolidation.
- `contradiction.py` — quarantines conflicting facts above threshold; demands
  human tie-break (never auto-resolves).
- `WorldModel` — marker-fenced vault documents (`current_focus.md`, etc.);
  atomic writes; rewrites only its managed section.

## 6. Model layer — `models/`

### `models/base.py` — ModelProvider contract
**Responsibility:** `CompletionRequest`/`CompletionResponse`/`Message` wire
types; `ModelProvider` ABC (`complete`, `healthcheck`, `complete_structured` with
validate-and-repair); `validate_against_schema` (pure-Python JSON Schema
subset); `redact` (secret scrubbing); `extract_json_object`; `minimal_instance_for_schema`.

**Public interface:** `ModelProvider`, `CompletionRequest`, `Message`,
`ModelTier`, `validate_against_schema`, `redact`.

**Failure behavior:** `healthcheck()` must never raise (`models/base.py:897`).
`complete_structured` retries up to `max_retries`, then raises
`StructuredOutputError` with schema errors (`models/base.py:969`).

### `models/registry.py` — Provider registry
**Responsibility:** `ProviderSpec` rows for ~20 providers (OpenAI family +
ollama/anthropic/gemini/echo). `build_provider(name, ...)` constructs a
provider; `build_local_inference()` for localhost backends. API keys resolved
via `resolved_api_key()` (env first, then `.env` via `dotenv_value`, never
`os.environ`).

**Public interface:** `build_provider`, `build_local_inference`, `resolve_spec`,
`KNOWN_PROVIDERS`, `provider_names`.

**Failure behavior:** `UnknownProviderError` lists valid names on typo
(`models/registry.py:67`).

### `models/router.py` — EscalatingModelRouter
**Responsibility:** Local-first, escalates to frontier on failure when policy
permits. Semaphore caps `max_concurrent_streams` (2 default). Writes
`MODEL_ESCALATED` ledger event **before** the frontier call.

**Public interface:** `EscalatingModelRouter.complete/complete_structured`,
`evaluate(modality, permission_mode)`, `EscalationDecision`.

**Failure behavior:** Escalation forbidden (LOCKDOWN) or below modality floor →
`ModelUnavailableError`, never quietly escalates (`router.py:356`).
Escalation without a ledger store logs a warning (unaudited escalation)
(`router.py:460`).

## 7. Sandbox — `sandbox/`

### `sandbox/base.py` — contract
**Responsibility:** `IsolationLevel` (NONE/PROCESS/NAMESPACE/VM), `SandboxSpec`
(explicit env allowlist; `build_child_env` builds from empty, never inherits
`os.environ`), `SandboxResult` (total outcome incl. kills/timeouts), `Sandbox`
ABC, `resolve_workspace`/`assert_inside_workspace` (workspace jail).

**Public interface:** `SandboxSpec`, `SandboxResult`, `IsolationLevel`, `Sandbox`.

**Failure behavior:** `resolve_workspace` raises `SandboxError` if the directory
doesn't exist (`sandbox/base.py:512`). `SandboxSpec._reject_ambient_env_smell` trips on
`env=dict(os.environ)` (`sandbox/base.py:337`).

### `sandbox/subprocess_backend.py` — SubprocessSandbox
**Responsibility:** PROCESS isolation. Windows Job Object memory caps +
kill-on-job-close; POSIX `ResourceWatchdog` sampled RSS. `asyncio.wait_for`
timeout. Output capped at `max_capture_bytes`. Network NOT enforced (policy +
AST scanner handle it).

**Failure behavior:** Job Object creation failure degrades to watchdog, never
takes down the run (`subprocess_backend.py`). Process start failure →
`SandboxError`.

### `sandbox/wsl_backend.py` — WslSandbox
**Responsibility:** NAMESPACE isolation via WSL2 (`ulimit -v/-t/-c 0`,
`unshare --net/--pid/--mount-proc` when available).

**Failure behavior:** `unshare` missing → `ulimit`-only isolation with warning
(`wsl_backend.py`).

### `sandbox/docker_backend.py` — DockerSandbox
**Responsibility:** NAMESPACE (or VM if `runsc` registered). `--read-only`,
tmpfs, cap-drop ALL, no-new-privileges, pids-limit, memory/CPU cgroups.

**Failure behavior:** Docker CLI/daemon unreachable → `healthcheck()` returns
`False` (cached), never raises (`docker_backend.py`).

### `sandbox/dryrun_backend.py` — DryRunSandbox
**Responsibility:** NONE — records what would run, executes nothing. Used for
planning-only mode and tests.

### `sandbox/recursion.py` — RecursionGuard
**Responsibility:** Depth, node, cycle bounds for delegation (RFC §11). Cycle
detection via iterative DFS (`would_cycle` searches target→source).

**Public interface:** `RecursionGuard.register/check_depth/check_nodes/check_cycle`.

### `sandbox/watchdog.py` — ResourceWatchdog + HeartbeatTracker
**Responsibility:** RSS sampling (psutil → Windows native → /proc → unavailable)
with peak-RSS enforcement (ADR-0009); heartbeat liveness tracking (miss
tolerance → declared dead).

## 8. Validation — `validation/`

### `validation/engine.py` — DeterministicValidationEngine
**Responsibility:** Binary PASS/FAIL. Ordered checks: schema → AST security →
patch dry-run → workspace drift → tests (runs last, only if above cleared).
Short-circuits on CRITICAL finding or structural failure.

**Public interface:** `validate(artifact) -> ValidationReport`; `ValidationArtifact`;
`ValidationReport`.

**Failure behavior:** No sandbox/workspace + tests requested → FAIL (unrun
suite is not passing) (`engine.py:377`). Patch without workspace → structural
failure (`engine.py:328`).

### `validation/ast_scanner.py` — AstSecurityScanner
**Responsibility:** Deny-by-default static scan. Denied modules/attributes/
builtins; sensitive paths; obfuscation (decoder + dynamic exec). Unparseable
source → CRITICAL finding, never skipped.

**Public interface:** `AstSecurityScanner.scan/scan_or_raise`; `Severity`;
`Finding`.

### `validation/patch.py` — UnifiedDiffValidator
**Responsibility:** Pure-Python unified-diff parse/dry-run/apply/rollback with
workspace jail (`safe_relative_path` blocks traversal/absolute/symlink/NUL).
Transactional apply with in-memory rollback journal.

**Public interface:** `UnifiedDiffValidator.parse/dry_run`; `PatchApplier.apply/
rollback`; `safe_relative_path`; `compute_patch_sha256`.

### `validation/schema_validator.py` — SchemaValidator
**Responsibility:** Pure-Python JSON Schema subset; `$ref`/`if`/`then`/`else`
always rejected; unsupported keywords ignored unless `strict_keywords`.

**Public interface:** `SchemaValidator.validate/check`.

### `validation/test_runner.py` — PytestRunner
**Responsibility:** Runs `python -m pytest` inside a sandbox; parses results;
`ok` requires positive evidence (pytest present, tests collected, exit 0,
passed>0).

**Public interface:** `PytestRunner.run()`.

## 9. Failure handling summary

| Failure type | Where handled | Behavior |
|---|---|---|
| Duplicate ledger event | `ledger/store.py:144` | Return existing event (idempotent) |
| Model unavailability | `router.py:335` | Escalate if permitted; else `ModelUnavailableError` |
| Structured output invalid | `router.py:333` | Escalate trigger; `StructuredOutputError` after retries |
| Policy violation | `policy.py:153` | `STATUS_BLOCKED` verdict → `POLICY_BLOCKED`/`SECURITY_VIOLATION` event |
| Missing permission | `agents/base.py:333` | `AgentResult.failure` (`PermissionDeniedError`) |
| Sandbox step failure | `orchestrator.py:556` | `VALIDATION_FAILED`; retry up to `max_step_retries` |
| Validation failure | `validation/engine.py:236` | Binary FAIL; critic can only downgrade |
| Sandbox memory breach | `watchdog.py` | Kill; `killed_reason="memory"` |
| Queue depth (backpressure) | `backpressure.py:115` | Degrade modality / shed discretionary ingestion (HTTP 429) |
| Crash mid-task | `recovery.py:222` | Boot sweep replays lineage, reconciles workspace, re-queues |

## Related documents

- [`backend/request-lifecycle.md`](request-lifecycle.md) — the submit→run path traced.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — verified inter-module connections.
- [`modules/ledger.md`](../modules/ledger.md), [`modules/agents.md`](../modules/agents.md), [`modules/validation.md`](../modules/validation.md), [`modules/sandbox.md`](../modules/sandbox.md), [`modules/models.md`](../modules/models.md) — deep dives.
- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — all `PAA_*` settings.
