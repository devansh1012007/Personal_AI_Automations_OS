# Glossary

Project-specific terminology, domain concepts, and acronyms used across the PAA
codebase and this documentation. Definitions are grounded in the implementation;
where a meaning is inferred rather than verified, it is labelled.

## Identifiers

- **CorrelationId** — A UUID (`NewType` over `uuid.UUID`) that is the lineage key
  for one task from request through commit. Every ledger event of a task shares
  this value. `src/paa/core/types.py:43`.
- **SessionId** — A UUID isolation boundary for one workspace/session.
  `src/paa/core/types.py:44`.
- **TaskId** — A UUID per agent invocation/task within a lineage.
  `src/paa/core/types.py:45`.
- **Causation id** — The `event_id` of the event that directly caused another
  event; enables walking the causal chain backwards ("explain why it did that").
  `src/paa/ledger/events.py:159`.
- **Idempotency key** — `SHA-256(correlation_id|event_type|attempt|discriminator)`;
  deduplicates at-least-once redelivery while permitting genuine retries via the
  `attempt` counter. `src/paa/ledger/events.py:68`.

## Ledger concepts

- **Ledger event** — One immutable state transition (`LedgerEvent`), with chain
  fields (`sequence_id`, `state_version`, `prev_hash`, `event_hash`) assigned by
  the store at append time. `src/paa/ledger/events.py:132`.
- **state_version** — Per-correlation monotonic counter starting at 1; replay folds
  events in `state_version` order (dense), not global `sequence_id` order (which
  can gap on rollback). `src/paa/ledger/store.py:13`.
- **Hash chain / tamper evidence** — Each event commits to its predecessor's digest
  (`prev_hash` → `event_hash`); altering any historical row invalidates every
  digest after it. Verified by `LedgerStore.verify_chain`.
  `src/paa/ledger/events.py:105`, `src/paa/ledger/store.py:315`.
- **GENESIS_HASH** — `"0" * 64`, the `prev_hash` of a lineage's first event.
  `src/paa/ledger/events.py:37`.
- **canonical_json** — Sorted-key, whitespace-free JSON used for payload hashing;
  any serialization change breaks the hash chain (a data migration).
  `src/paa/ledger/events.py:40`.
- **Correlation head / head projection** — One row per lineage in
  `system_state_correlation_head` tracking the latest state version, event type,
  and whether the lineage is terminal. Recovery scans only `is_terminal = 0`
  rows. `src/paa/ledger/store.py:69`.
- **TERMINAL_EVENTS** — Events after which a correlation is closed and needs no
  recovery sweep: `MUTATION_COMMITTED`, `EXECUTION_FAILED`, `POLICY_BLOCKED`,
  `SECURITY_VIOLATION`, `HUMAN_GATE_REJECTED`, `TASK_ABANDONED`. Note
  `EXECUTION_COMPLETED` is deliberately *not* terminal (it fires per step).
  `src/paa/core/types.py:132`.
- **Task projection / TaskPhase** — The deterministic left-fold projection of a
  lineage's events into coarse lifecycle phase (`CREATED`, `QUEUED`,
  `PLANNING`, `POLICY_REVIEW`, `EXECUTING`, `VALIDATING`, `AWAITING_HUMAN`,
  `COMMITTED`, `FAILED`, `BLOCKED`, `ROLLED_BACK`, `ABANDONED`).
  `src/paa/ledger/replay.py:34`.
- **Snapshot** — A checkpointed projection (`system_state_snapshots`) used as a
  replay optimisation; safe to delete, never truth.
  `src/paa/ledger/store.py:364`.

## Task lifecycle events (in order)

`TASK_REQUESTED → TASK_QUEUED → CONTEXT_HYDRATION_REQUESTED → CONTEXT_HYDRATED →
PLAN_COMPILED → POLICY_CLEARED → EXECUTION_STARTED → (per step)
EXECUTION_COMPLETED / VALIDATION_FAILED → CRITIQUE_CONCLUDED → MUTATION_COMMITTED`.
Branches: `POLICY_BLOCKED / SECURITY_VIOLATION` (terminal blocked),
`AWAITING_HUMAN_ATTESTATION` (parks), `STATE_ROLLBACK_TRIGGERED` (drift → restore).
`src/paa/core/types.py:67`; `src/paa/ledger/replay.py:149`.

## Permission model

- **Permission** — Capability tokens (`PERM_SANDBOX_RUN`, `PERM_WRITE_HOT`,
  `PERM_NET_EGRESS`, `PERM_EXTERNAL_WRITE`, `PERM_FILE_DELETE`,
  `PERM_SCHEMA_MIGRATE`, `PERM_SECRET_READ`, `PERM_MODEL_ESCALATE`,
  `PERM_CROSS_SESSION_READ`). `src/paa/core/types.py:149`.
- **PermissionMode** — System-wide autonomy posture: `AUTO`, `ASK`,
  `SUPERVISED`, `LOCKDOWN`, `SAFE`, `HIGH_RISK`. Each grants a fixed permission
  set without a human gate. `src/paa/core/types.py:169`.
- **coder PermissionMode** — Separate session posture for `paa agent`:
  `ask`, `accept_edits`, `plan` (read-only), `bypass`. Precedence: deny >
  allow > mode default. `src/paa/coder/permissions.py:40`.

## Complexity modalities

- **ComplexityModality** — `SIMPLE`, `STANDARD`, `COMPLEX`, `MAX`. Bounds graph
  hops, recursion ceiling, token ceiling, memory, CPU, timeout, branch factor via
  `MODALITY_PROFILES`. `src/paa/core/types.py:223`.
- **ModalityProfile** — Hard resource envelope per modality; nothing in it is
  advisory. `src/paa/core/types.py:232`.
- **Escalation** — Local model first, frontier model on failure (only when
  modality ≥ `escalate_at_or_above` and the permission mode allows it). Every
  escalation writes a `MODEL_ESCALATED` ledger event *before* the frontier call.
  `src/paa/models/router.py:123`.

## Storage

- **Cold lake** — Content-addressed, zstd-compressed blob store; SHA-256 of raw
  bytes is the address; immutable; verifies hash on read. `src/paa/storage/coldlake/cas.py`.
- **Signals** — Raw external events ingested verbatim, idempotent on
  `(channel, external_id)`; oversized payloads offloaded to the cold lake.
  `src/paa/storage/coldlake/signals.py`.
- **Hot serving** — Relational tables (`hot_serving_*`) holding entities, facts,
  relationships, sessions, recovery marks, policy rules — the mutable read path.
- **Queue** — Durable at-least-once message queue (SQLite by default, Redis
  optional); exactly-once processing via ledger idempotency keys.
  `src/paa/storage/queue/base.py`.

## Sandbox / containment

- **IsolationLevel** — Honest ranking of what a backend delivers: `NONE`
  (dryrun), `PROCESS` (subprocess), `NAMESPACE` (WSL/Docker), `VM` (gVisor,
  unavailable on target hardware). Only `NAMESPACE` and above is "real
  containment". `src/paa/sandbox/base.py:54`.
- **Workspace jail** — `resolve_workspace` / `assert_inside_workspace` resolve
  paths (collapse `..`, follow symlinks) and confine writes to one directory.
  `src/paa/sandbox/base.py:282`.
- **AST pre-scan** — Static rejection of dangerous Python before execution;
  unparseable source is rejected, never skipped. `src/paa/validation/ast_scanner.py`.

## Validation

- **Deterministic validation** — Binary (PASS/FAIL) host-side checks:
  schema → AST security scan → patch dry-run → workspace drift → tests (runs
  last, only executes the artifact). No model consulted for security decisions.
  `src/paa/validation/engine.py`.

## Coder agent

- **ToolCallingModel** — Narrow protocol (`propose(window, tools) -> ModelTurn`)
  the loop depends on, never a provider. `src/paa/coder/loop.py:75`.
- **RouterToolCallingModel** — Bridges the loop protocol to the escalation router
  via constrained JSON (`{text, tool_calls[]}` with tool names in an enum).
  `src/paa/coder/model_adapter.py:118`.
- **ToolPermissionPolicy** — Allow/deny/ask per tool call; deny always wins
  (even under bypass); PLAN mode refuses all mutating tools.
  `src/paa/coder/permissions.py:143`.

## Skills / marketplace

- **Unified Skill Adapter (USA)** — Single dispatch state machine:
  discover → compile → authorize → sandbox-mount → invoke → bound-secret proxy →
  output validation → reliability adjustment. `src/paa/skills/usa.py:106`.
- **Skill contract** — The only thing trusted about a skill before execution;
  input/output schemas, required permissions, risk profile, signature.
  `src/paa/skills/contracts.py:343`.
- **Five-gate installer** — Runs gates in order; a failing package leaves zero
  trace. `src/paa/marketplace/installer.py`.
- **SignatureState** — `unverified`, `valid`, `invalid`, `untrusted_key`.
  `src/paa/marketplace/signing.py:40`.

## Runtime operations

- **Backpressure** — Queue-depth based admission: NORMAL / DEGRADED (degrade
  modality) / SHEDDING (refuse discretionary ingestion, HTTP 429). Control-plane
  streams always accepted. `src/paa/storage/queue/backpressure.py:77`.
- **Daemon** — Dispatch, ingestion, and maintenance loops over one shared runtime;
  boot recovery runs first. `src/paa/daemon.py:40`.
- **Recovery / boot sweep** — Replays open lineages from the ledger (never the
  queue), reconciles the workspace against a checksum manifest, restores drift,
  re-queues resumable tasks, parks human-gated tasks. `src/paa/ledger/recovery.py`.

## Acronyms

- **CAS** — Content-addressed storage (the cold lake).
- **CQRS-ish read model** — The task projection is a derived read model over the
  append-only ledger.
- **FTS5** — SQLite full-text search used for fuzzy entity resolution.
- **WAL** — SQLite Write-Ahead Logging mode (single writer, concurrent readers).
- **RFC** — The v4.0/v4.1 design document PAA was built from; deviations recorded
  in `docs/adr/` and as `SPEC DEVIATION` notes in source.
