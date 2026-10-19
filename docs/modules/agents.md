# Module: Agents (`src/paa/agents/`)

## Module identity

- **Name:** Agents (agent hierarchy + message protocol)
- **Path:** `src/paa/agents/`
- **Main responsibility:** The typed agent message protocol, the Chief Orchestrator task lifecycle, the reasoning agents (planner/critic/router/worker), the deterministic policy gate, the context builders, and the delegation safety graph.
- **Architectural layer:** Control / business logic
- **Entrypoints:** `ChiefOrchestrator.run()`, `ChiefOrchestrator.submit()`, `Agent.run()`, `Agent.delegate()`
- **Public interfaces:** `Agent`, `AgentContext`, `AgentMessage`, `AgentResult`, `MessageType`, `RiskLevel` (`base.py`); `ChiefOrchestrator`, `TaskRequest`, `TaskOutcome` (`orchestrator.py`); `ModelLike`, `StrategicPlanner`, `CriticReviewer`, `TaskRouter`, `WorkerCell` (`reasoning.py`); `PolicyRiskAgent`, `PolicyVerdict` (`policy.py`); `DelegationEdge`, `DelegationGraph`, `DelegationRegistry` (`delegation.py`); `PlannerContextBuilder`, `WorkerContextBuilder`, `QueryExpander` (`context_agents.py`)
- **Other modules that depend on it:** `runtime.py` (builds all agents), `api/app.py` (via Runtime), `daemon.py` (via Runtime.run), `cli/` (via Runtime), `coder/` (indirectly via Runtime)

## Purpose and responsibilities

Agents exchange typed messages rather than prose (RFC §13). The base class owns
cross-cutting concerns — permission checks, budget accounting, timing, ledger
emission — so no agent can skip them (`base.py:270`).

The Chief Orchestrator owns the task lifecycle: hydrate → plan → policy →
execute → commit, writing a ledger event before every side effect
(`orchestrator.py:9`).

No generative reasoning in the control path: routing/gating are Python, never a
model (`orchestrator.py:9`, `policy.py:7`).

## Internal implementation

### `base.py` — foundation
- `Agent` ABC: `handle(message, ctx)` abstract; `run()` applies permission
  checks, expiry, timing, budget, exception→structured-failure conversion
  (`base.py:299`).
- `AgentContext`: correlation/session/task ids, modality, permission mode,
  recursion depth, token budget; `spend()` raises `BudgetExceededError` on
  overspend (`base.py:243`); `child()` halves token budget per depth
  (`base.py:246`).
- `AgentMessage`: frozen typed envelope with `MessageType` intent
  (`base.py:109`).
- `AgentResult`: ok, value, error, tokens_consumed, latency_ms, model_used,
  escalated, confidence (`base.py:174`).
- `Agent.delegate(target, intent, payload, ctx)` routes through the
  orchestrator's `DelegationRegistry` — never peer-to-peer (`base.py:347`).

### `orchestrator.py` — Chief Orchestrator
`run(cid)` projects the lineage; if terminal, returns immediately
(`orchestrator.py:295`). Phases:
- `_hydrate` (`:353`) — context builder; parks on `AWAITING_HUMAN_ATTESTATION`
  if density insufficient (`:379`).
- `_plan` (`:403`) — planner; SIMPLE bypasses model (`:408`); validates
  `max_plan_nodes()` (`:441`).
- `_check_policy` (`:461`) — policy agent; blocked → `POLICY_BLOCKED`/
  `SECURITY_VIOLATION`; human gate → `AWAITING_HUMAN_ATTESTATION`; else
  `POLICY_CLEARED` (`:505`).
- `_execute_steps` (`:515`) — per step: `EXECUTION_STARTED` → worker → critic
  → `EXECUTION_COMPLETED`/`VALIDATION_FAILED`; retry up to `max_step_retries`
  (default 2, `:108`).
- `_commit` (`:609`) — `MUTATION_COMMITTED` (terminal).

`mediate_delegation()` (`:631`) enforces delegation graph bounds. `clear_human_
gate()` (`:688`) — only external callers clear gates. `boot()` (`:737`) runs
recovery.

### `reasoning.py` — planner, critic, router, worker
- `ModelLike` protocol (`reasoning.py:26`): `complete_structured(prompt,
  schema)` — the narrow model seam agents depend on.
- `StrategicPlanner` (`:62`) — compiles goal+context into steps; deterministic
  fallback when no model; `_normalise` preserves passthrough keys
  (`:155`) — a security requirement (dangerous `command`/`required_permissions`
  must reach policy + sandbox).
- `CriticReviewer` (`:194`) — deterministic validation first, model second;
  model can only downgrade (`:226`).
- `TaskRouter` (`:303`) — optional; `should_route()` bypasses when a target
  agent is named or <3 eligible agents (`:331`, ADR-0011).
- `WorkerCell` (`:413`) — runs one step in a sandbox; `required_permissions =
  (SANDBOX_RUN,)` (`:429`); returns patch/stdout, never applies its own patch.

### `policy.py` — PolicyRiskAgent
Deterministic risk gate, **no model consulted** (`policy.py:7`). Checks:
permissions (`:174`), LOCKDOWN network (`:194`), irreversible ops under SAFE
(`:213`, `_IRREVERSIBLE_PATTERNS` at `:37`), paths (`:222`), operator rules
from DB (`:234`), anti-goals (cosine ≥0.82 or phrase match, `:262`). Risk score
[0,1]; `_needs_gate()` (`:318`) decides human-gate need by mode.

### `delegation.py` — Delegation graph
`DelegationGraph` per correlation enforces depth, cycle (iterative DFS
`would_cycle`, `:62`), and node bounds (`max_plan_nodes()`); `propose()` raises
`RecursionGuardError` on breach (`:87`); `release()` removes completed edges
(`:123`). `DelegationRegistry` holds graphs keyed by correlation (`:140`).

### `context_agents.py` — context builders
`PlannerContextBuilder` (`:236`) — all domains; `WorkerContextBuilder` (`:271`)
— drops narrative domains, fails on vacant slots (`hydration_required`,
`:303`). Both use `BoundedContextGatherer` + `QueryExpander`.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `ChiefOrchestrator.submit(request)` | `TaskRequest` | `CorrelationId` | writes `TASK_REQUESTED`/`TASK_QUEUED`; enqueues |
| `ChiefOrchestrator.run(cid)` | correlation id | `TaskOutcome` | writes phase events; may run sandbox |
| `Agent.run(message, ctx)` | `AgentMessage`, `AgentContext` | `AgentResult` | permission check; token spend |
| `PolicyRiskAgent.handle(message, ctx)` | plan + goal | `PolicyVerdict` dict | reads DB rules + vectors |
| `WorkerCell.handle(message, ctx)` | step | `AgentResult` {patch, stdout} | runs sandboxed process |
| `StrategicPlanner.handle(message, ctx)` | goal + context | plan dict | model call (or deterministic) |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `ChiefOrchestrator` → all agents | calls `agent.run(message, ctx)` | Drive lifecycle phases | `orchestrator.py:374`, `:434`, `:471`, `:538`, `:594` |
| `Agent` → `DelegationRegistry` | `delegate()` | Orchestrator owns delegation graph | `base.py:367`, `orchestrator.py:631` |
| `Reasoning agents` → `ModelLike` | `complete_structured()` | Model seam | `reasoning.py:95`, `:246`, `:353` |
| `RouterModelAdapter` | wraps `EscalatingModelRouter` | Bridge `ModelLike` → router | `runtime.py:98` |
| `PolicyRiskAgent` → `Database` | `fetch_all(hot_serving_policy_rules)` | Operator rules | `policy.py:255` |
| `Context builders` → `Database`/vector/graph | fetch facts/semantic/graph | Build context packet | `context_agents.py:147`, `:182`, `:205` |
| `Runtime.build` | constructs all agents | Composition root | `runtime.py:407` |

## Runtime behavior

- Initialized by `Runtime.build()` → `_build_agents()` (`runtime.py:407`).
- Each `Agent.run()` is async, applies cross-cutting guarantees, returns
  `AgentResult`.
- Delegation is mediated, non-blocking at topology level (DAG wait graph).

## Failure behavior

- Missing permissions → `AgentResult.failure` (`PermissionDeniedError`)
  (`base.py:305`).
- Unexpected exception → structured failure (`base.py:329`).
- Planner schema failure → retries once, then gives up (`reasoning.py:93`).
- Critic deterministic failure → short-circuits, no model call
  (`reasoning.py:226`).
- Policy block → `POLICY_BLOCKED`/`SECURITY_VIOLATION` event; terminal phase
  (`orchestrator.py:474`).
- Step exhaustion → `PaaError` → `EXECUTION_FAILED` (`orchestrator.py:570`).

## Modification impact

- Changing an agent's `handle()` signature breaks the `Agent` ABC contract
  (`base.py:296`).
- Adding a delegation requires `can_delegate = True` and orchestrator
  registration (`base.py:282`, `orchestrator.py:119`).
- Changing `TERMINAL_EVENTS` affects recovery scanning (`types.py:132`,
  `store.py:287`).
- Tests: `tests/agents/` (70 tests per README).

## Known gaps

- `src/paa/agents/specialists/` is an empty package (`AGENTS.md:146`).
- Router is optional; when absent, `_classify()` heuristic picks modality
  (`orchestrator.py:247`).
- Deterministic planner fallback is crude (splits on conjunctions).

## Related documents

- [`backend/modules-and-services.md`](../backend/modules-and-services.md) — agent internals.
- [`backend/request-lifecycle.md`](../backend/request-lifecycle.md) — task lifecycle phases.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — agent connections.
- [`modules/ledger.md`](ledger.md) — events agents emit.
- [`modules/models.md`](models.md) — model seam agents use.
- [`modules/sandbox.md`](sandbox.md) — WorkerCell's sandbox.
- [`modules/validation.md`](validation.md) — Critic's validation engine.
