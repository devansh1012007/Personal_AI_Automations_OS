# Backend request lifecycle

This document traces the lifecycle of a single task from submission to terminal
outcome through the backend. It reflects the actual execution order in
`src/paa/agents/orchestrator.py`.

## Entry points

| Trigger | File | Function |
|---|---|---|
| `paa submit "goal"` | `cli/main.py:73` | `submit` → `Runtime.submit()` |
| `paa run <id>` | `cli/main.py:89` | `run` → `Runtime.run()` |
| API `POST /tasks` (`?run=true`) | `api/app.py:85` | `submit_task` → `Runtime.submit()` then `Runtime.run()` |
| API `POST /tasks` (`?run=false`) | `api/app.py:85` | Submit + queue only; daemon drains later |
| Daemon dispatch loop | `daemon.py:116` | `_claim_and_run()` → `Runtime.run(cid)` |
| Recovery requeue | `recovery.py:437` | `_requeue()` → queue → daemon dispatch |

All converge on `ChiefOrchestrator.run()` (`orchestrator.py:292`).

## Phase 1 — Project existing state

`run(correlation_id)` calls `project(self._ledger, correlation_id)`
(`orchestrator.py:396`). If the projection is terminal, it returns immediately
(`orchestrator.py:295`). Otherwise it builds an `AgentContext` from the
modality profile (`_context_for`, `orchestrator.py:327`).

## Phase 2 — Hydrate context

`_hydrate()` (`orchestrator.py:467`):
- If a context packet already exists, skip.
- Emits `CONTEXT_HYDRATION_REQUESTED` (`orchestrator.py:357`).
- Sends `CONTEXT_REQUEST` to the planner context builder
  (`context_agents.py:241`).
- If the result is `HARD_STOP_ESCALATE_TO_USER` (insufficient density), emits
  `AWAITING_HUMAN_ATTESTATION` and returns (`orchestrator.py:379`).
- Otherwise emits `CONTEXT_HYDRATED` with the packet (`orchestrator.py:499`).

Every emit re-projects (`project()`) so the next phase reads current state.

## Phase 3 — Plan

`_plan()` (`orchestrator.py:542`):
- If steps already exist or no planner, skip.
- SIMPLE modality bypasses the model: emits `PLAN_COMPILED` with a single
  step (`execution_steps: [{action: goal, agent: worker}]`) and
  `bypassed_planner: true` (`orchestrator.py:408`).
- Otherwise sends `PLAN_PROPOSAL` to the strategic planner
  (`reasoning.py:78`). The planner returns `execution_steps` (and
  `step_requirements`); each step may carry passthrough keys (`command`,
  `required_permissions`, `path`, `risk_profile`, `always_human_gate`) which
  are preserved by `_normalise` (`reasoning.py:155`).
- Validates `len(steps) <= profile.max_plan_nodes()`; raises
  `RecursionGuardError` if exceeded (`orchestrator.py:441`).
- Emits `PLAN_COMPILED` (`orchestrator.py:448`).

## Phase 4 — Policy check

`_check_policy()` (`orchestrator.py:461`):
- If a policy decision already exists or no policy agent, skip.
- Sends `POLICY_CHECK` to `PolicyRiskAgent` (`policy.py:139`).
- On blocked verdict (`STATUS_BLOCKED`) or `result.ok == False`:
  - anti-goal match → `SECURITY_VIOLATION`; else `POLICY_BLOCKED`
    (`orchestrator.py:474`).
  - Returns; phase becomes `BLOCKED` (terminal).
- On `requires_human_gate`: emits `AWAITING_HUMAN_ATTESTATION`; phase parks
  (`orchestrator.py:494`). Never auto-clears across restart
  (`replay.py:62`).
- Otherwise emits `POLICY_CLEARED` with `risk_score` (`orchestrator.py:505`);
  phase becomes `EXECUTING`.

## Phase 5 — Execute steps

`_execute_steps()` (`orchestrator.py:515`). For each step not already in
`completed_steps` (resume support):

```
for attempt in 0..max_step_retries:
    emit EXECUTION_STARTED {step_index, step, attempt, discriminator="step-N"}
    → WorkerCell.handle (reasoning.py:431)
        → SandboxSpec from modality profile (memory_mb, cpu_cores, timeout)
        → sandbox.run(spec) → SandboxResult
        → if exit_code != 0 or timed_out: AgentResult.failure
        → else: AgentResult.success({patch, stdout, exit_code})
    → critic.review (if critic configured):
        → deterministic validation first (validation/engine.py:208)
        → model review second (can only downgrade)
        → emit CRITIQUE_CONCLUDED {step_index, verdict}
    → if passed: emit EXECUTION_COMPLETED {step_index, tokens}; break
    → else: emit VALIDATION_FAILED {step_index, attempt}; retry
else (budget exhausted):
    raise PaaError("step exhausted its retry budget") → EXECUTION_FAILED
```

`EXECUTION_COMPLETED` fires **per step**, not per task, and is not terminal
(`ADR-0010`). After each step, the orchestrator re-projects
(`orchestrator.py:577`).

## Phase 6 — Commit

`_commit()` (`orchestrator.py:609`) emits `MUTATION_COMMITTED`
{steps_completed, tokens_consumed}; phase becomes `COMMITTED` (terminal).

## Alternative and failure paths

| Condition | Event emitted | Phase | Recoverable? |
|---|---|---|---|
| Policy blocked | `POLICY_BLOCKED` | BLOCKED | Terminal |
| Anti-goal match | `SECURITY_VIOLATION` | BLOCKED | Terminal |
| Human gate required | `AWAITING_HUMAN_ATTESTATION` | AWAITING_HUMAN | Parks; cleared only by external `clear_human_gate` |
| Step exceeds retries | `EXECUTION_FAILED` | FAILED | Via recovery sweep (re-queue) |
| Crash mid-execution | (event already written) | per projection | Via recovery sweep |
| Workspace drift on recovery | `STATE_ROLLBACK_TRIGGERED` | ROLLED_BACK | Restored + re-queued |
| Attempts >= max | `TASK_ABANDONED` | ABANDONED | Terminal |
| Human gate rejected | `HUMAN_GATE_REJECTED` | BLOCKED | Terminal |

`EXECUTION_FAILED` is emitted by the `except PaaError` branch in `run()`
(`orchestrator.py:310`), which re-projects and returns the error payload.

## Sequence diagram

```mermaid
sequenceDiagram
    participant C as CLI / API / Daemon
    participant R as Runtime
    participant O as ChiefOrchestrator
    participant L as LedgerStore
    participant CB as ContextBuilder
    participant P as StrategicPlanner
    participant Pol as PolicyRiskAgent
    participant W as WorkerCell
    participant S as Sandbox
    participant Cr as Critic

    C->>R: submit(goal) / run(cid)
    R->>O: submit() / run()
    O->>L: append(TASK_REQUESTED)
    L-->>O: sealed event
    O->>L: append(TASK_QUEUED)
    L-->>O: sealed event
    O->>O: project() → TaskProjection
    O->>CB: CONTEXT_REQUEST
    CB-->>O: context packet
    O->>L: append(CONTEXT_HYDRATED)
    O->>P: PLAN_PROPOSAL
    P-->>O: execution_steps
    O->>L: append(PLAN_COMPILED)
    O->>Pol: POLICY_CHECK
    Pol-->>O: verdict
    alt blocked / human gate
        O->>L: append(POLICY_BLOCKED / AWAITING_HUMAN_ATTESTATION)
        O-->>C: TaskOutcome (terminal / parked)
    else cleared
        O->>L: append(POLICY_CLEARED)
        loop for each step
            O->>L: append(EXECUTION_STARTED)
            O->>W: EXECUTION_REQUEST {step}
            W->>S: run(SandboxSpec)
            S-->>W: SandboxResult
            W-->>O: AgentResult {patch, stdout}
            O->>Cr: REVIEW_RESULT {output}
            Cr-->>O: verdict
            O->>L: append(CRITIQUE_CONCLUDED)
            alt pass
                O->>L: append(EXECUTION_COMPLETED)
            else fail
                O->>L: append(VALIDATION_FAILED)
                loop retry
            end
        end
        O->>L: append(MUTATION_COMMITTED)
        O-->>C: TaskOutcome (COMMITTED)
    end
```

## Persistence points

| What | Where | Table |
|---|---|---|
| Every event | `LedgerStore.append` | `system_state_ledger` |
| Head projection | `LedgerStore._update_head` | `system_state_correlation_head` |
| Queue message | `queue.enqueue(StreamName.ORCHESTRATOR_CORE)` | `queue_messages` |
| Recovery mark | `RecoveryEngine.checkpoint` | `hot_serving_recovery_marks` |

## Related documents

- [`backend/modules-and-services.md`](modules-and-services.md) — each module's internals.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — verified connections.
- [`modules/ledger.md`](../modules/ledger.md) — event vocabulary and store.
- [`flows/workflow-01-submit-and-run.md`](../flows/workflow-01-submit-and-run.md) — end-to-end trace with a concrete goal.
