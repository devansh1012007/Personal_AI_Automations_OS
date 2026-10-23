# Workflow 02 — Human-attestation gate (park and resume)

Traces a task parked on `AWAITING_HUMAN_ATTESTATION` and later cleared via
`paa gate` / `POST /tasks/{id}/gate`. Verified against
`src/paa/agents/orchestrator.py`, `src/paa/ledger/replay.py`,
`src/paa/ledger/recovery.py`.

## How a task gets parked

Two gate points in `ChiefOrchestrator.run()`:

1. **Context hydration** (`orchestrator.py:467`): if the planner context packet's
   `routing_directive` is `HARD_STOP_ESCALATE_TO_USER` (density < 0.40), emit
   `AWAITING_HUMAN_ATTESTATION` with `{reason: "insufficient context density",
   vacant_slots: [...]}` and return (`:379`).

2. **Policy check** (`orchestrator.py:461`): if `PolicyRiskAgent` returns
   `requires_human_gate` (e.g. SUPERVISED mode + mutating step, risk ≥ 0.5 in
   ASK, `always_gate_risk_profile ≥ 0.90`, `policy.py:318`), emit
   `AWAITING_HUMAN_ATTESTATION` with `{reason, ...}` and return (`orchestrator.py:909`).

3. **Post-execution** (`orchestrator.py:515`): after `_execute_steps`, if
   `state.phase is AWAITING_HUMAN`, return (`:507`).

The `TaskProjection.phase` becomes `AWAITING_HUMAN`; `is_resumable` is
**False** (`replay.py:62`) — recovery will park it, never auto-clear
(`recovery.py:274`).

## Events emitted

| Event | Payload | File |
|---|---|---|
| `AWAITING_HUMAN_ATTESTATION` | `{reason, vacant_slots?}` | `orchestrator.py:380` / `:497` |
| `HUMAN_GATE_CLEARED` | `{resume_phase, note}` | `orchestrator.py:712` |
| `HUMAN_GATE_REJECTED` | `{resume_phase, note}` | `orchestrator.py:712` |

## Clearing the gate (external only)

`ChiefOrchestrator.clear_human_gate(cid, approved, resume_phase=EXECUTING,
note=None)` (`orchestrator.py:1015`):
1. `project()`; raises `PaaError` if phase is not `AWAITING_HUMAN` (`:703`).
2. Emit `HUMAN_GATE_CLEARED` (approved) or `HUMAN_GATE_REJECTED` (rejected)
   with `{resume_phase, note}` (`:712`).
3. Return re-projected `TaskProjection`.

**Nothing in the automated path can clear its own gate** (`orchestrator.py:699`)
— a crash is never interpreted as consent (`replay.py:62`).

Callers:
- CLI `paa gate <id> --approve/--reject` (`cli/main.py:164`)
- API `POST /tasks/{id}/gate` (`api/app.py:126`)

## Replay semantics

`apply_event()` on `HUMAN_GATE_CLEARED` (`replay.py:261`):
- clears `awaiting_reason`;
- sets phase to `TaskPhase(payload.get("resume_phase", EXECUTING))` — resumes
  where the gate interrupted, defaulting to execution.

On `HUMAN_GATE_REJECTED` → `policy_decision` set, phase `BLOCKED` via
`_PHASE_MAP` (`replay.py:163`).

## Recovery behavior

`RecoveryEngine.recover_correlation()` (`recovery.py:263`):
- `projection.phase is AWAITING_HUMAN` → `PARKED_HUMAN`, left waiting
  (`:274`). Never auto-cleared.

## Sequence diagram

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant CLI as paa submit / paa gate
    participant RT as Runtime
    participant ORC as ChiefOrchestrator
    participant LED as LedgerStore
    participant POL as PolicyRiskAgent

    User->>CLI: submit "goal requiring gate"
    CLI->>RT: submit_and_run(goal)
    RT->>ORC: submit() → run()
    ORC->>POL: POLICY_CHECK
    POL-->>ORC: verdict {requires_human_gate: true}
    ORC->>LED: append(AWAITING_HUMAN_ATTESTATION {reason})
    LED-->>ORC: sealed event
    ORC-->>RT: TaskOutcome (phase=AWAITING_HUMAN, not ok)
    RT-->>CLI: phase: AWAITING_HUMAN
    CLI-->>User: task parked awaiting gate

    Note over User,CLI: (time passes; operator reviews /paa ledger <id>)

    User->>CLI: gate <id> --approve --note "looks good"
    CLI->>RT: clear_human_gate(cid, approved=True)
    RT->>ORC: clear_human_gate()
    ORC->>LED: project() (confirm phase == AWAITING_HUMAN)
    LED-->>ORC: TaskProjection
    ORC->>LED: append(HUMAN_GATE_CLEARED {resume_phase: EXECUTING})
    LED-->>ORC: sealed event
    ORC->>LED: project() → phase EXECUTING
    ORC-->>RT: TaskProjection (phase EXECUTING)
    RT-->>CLI: phase: EXECUTING
    CLI-->>User: resumed; continues execution

    Note right of ORC: Remaining plan steps execute normally → MUTATION_COMMITTED
```

## Failure paths

| Condition | Behavior | Source |
|---|---|---|
| Gate cleared on non-parked task | `PaaError` "task is not awaiting human attestation" → API 409 | `orchestrator.py:703`, `api/app.py:136` |
| Recovery encounters AWAITING_HUMAN | `PARKED_HUMAN`; left across restart | `recovery.py:274` |
| Automated path tries to clear | Not possible — no internal caller | `orchestrator.py:699` |

## Persistence

| Event | Store | Table |
|---|---|---|
| `AWAITING_HUMAN_ATTESTATION` | LedgerStore | `system_state_ledger` |
| `HUMAN_GATE_CLEARED` / `REJECTED` | LedgerStore | `system_state_ledger` |
| Head `is_terminal` | stays 0 (not terminal) | `system_state_correlation_head` |

## Related documents

- [`backend/request-lifecycle.md`](../backend/request-lifecycle.md) — `_check_policy`, `_hydrate`.
- [`modules/ledger.md`](../modules/ledger.md) — `TERMINAL_EVENTS` excludes gate events.
- [`modules/agents.md`](../modules/agents.md) — `PolicyRiskAgent`.
- [`runtime/background-jobs-and-events.md`](../runtime/background-jobs-and-events.md) — recovery parks gates.
- [`flows/workflow-01-submit-and-run.md`](workflow-01-submit-and-run.md) — the main path.
- [`flows/workflow-03-crash-recovery.md`](workflow-03-crash-recovery.md) — crash + parked task.
