# Brief — W2-IMPR-01 (fixer) · wave 2 improve · run 20261022-w6-w7-w9

**Task:** fix **F-1** of `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-01-code.md`
(MAJOR · `src/paa/agents/orchestrator.py:561` and `:445`). Read that finding
in full before you start. **You are not the reviewer and you are not the
implementer of the original wave-2 work** — you are the fixer.

## The defect, precisely

Both human-gate emit sites call `self._emit(..., EventType.AWAITING_HUMAN_ATTESTATION, ...)`
**without a discriminator and without an attempt**. The ledger's
`compute_idempotency_key` (`src/paa/ledger/events.py:68`) hashes
`correlation_id | event_type | attempt | discriminator` ⟹ **two gate requests
in one lineage produce the same key** ⟹ the second is *deduplicated away*.

The measured consequence (the reviewer ran it with the real
`PolicyRiskAgent`): a task that parks on the context builder's gate, is
answered by a human, resumes, and then meets the **policy** stage's gate ⟹
that second gate event is swallowed ⟹ the orchestrator sees no pending gate
⟹ the task proceeds to `MUTATION_COMMITTED` **with the policy's risk gate
unanswered**.

This is the same defect class the models layer already fixed in wave 2 (the
escalation ordinal), and the same shape the agents suite already pins for
`CRITIQUE_CONCLUDED` (the critic does not pass an attempt, so a retried
step's second critique is suppressed by the key).

## What to do

1. **Distinguish the gate requests at the emit site.** The ledger's key
   already supports a `discriminator` for exactly this — *"Distinguishes
   same-typed events that are legitimately concurrent"*. Use it. The
   discriminator must be **stable across a redelivery of the same request**
   (so a retried emit is still suppressed) and **distinct across different
   requests** (so a second gate is not swallowed). Decide what identifies a
   request — the stage that asked, plus whatever else is needed to keep two
   requests by the *same* stage distinct — and **write down why**.
2. **Make the orchestrator's "is a gate pending?" logic correct.** A gate
   that was opened and answered must not block the task, and a gate that was
   opened and **not** answered must block it — including a *second* gate
   opened after the first was answered. Read the projection
   (`ledger/replay.py`'s phase map) and the release path
   (`clear_human_gate`, `orchestrator.py:777`) and make the two agree.
3. **Keep the human-only rule.** Nothing in the automated path may clear its
   own gate. That is what makes `AWAITING_HUMAN_ATTESTATION` mean something
   (ADR-0034) — do not weaken it.
4. **Correct ADR-0034.** It currently mis-describes this case (the reviewer
   checked it against the code). Say what the mechanism is, in the ADR's own
   register.

## Acceptance criteria

* A test that parks on the context gate, answers it, resumes, and then meets
  a **policy** gate: the second gate is **recorded** (the ledger holds two
  distinct gate events), the task is **parked again**, and it does **not**
  reach `MUTATION_COMMITTED`.
* A test that a **redelivery** of the *same* gate request is still suppressed
  (the deduplication property must survive your fix).
* The existing wave-2 parking tests still pass — in particular the one that
  asserts nothing in the automated path clears the gate.
* The exact-event-list tests still pass: a **discriminator** changes the
  event's *key*, not the event's *type* or *order*; if it changes an event
  list, that list is wrong and the test must say so.
* Scoped: `.venv/bin/python -m pytest tests/agents tests/integration -q`,
  `ruff check src/paa/agents tests/agents`, `mypy src/paa/agents` — all green,
  and the wave-1/wave-2 gate must not regress.

## Ownership

**WRITE-SET:** `src/paa/agents/orchestrator.py`, `tests/agents/test_orchestrator.py`,
`tests/integration/test_end_to_end.py`, `docs/adr/0034-*.md`.

**READ-ONLY:** `src/paa/ledger/**` (read `events.py` and `replay.py`; if the
fix genuinely needs a change there, post a `NEED` with the exact change and
report `DEFERRED-DEPENDENCY` — do not edit it), `src/paa/agents/{policy,delegation,base}.py`,
`tests/agents/test_policy.py` and `tests/agents/test_delegation.py` (another
agent owns them this wave), `tests/conventions/**`.

**FORBIDDEN:** everything else, including `src/paa/models/**`,
`src/paa/storage/**`, `src/paa/skills/**`, `pyproject.toml`, `docs/adr/README.md`,
`docs/plans/**`, `AGENTS.md`.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-IMPR-01-F-1.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, with trailers
`Task: F-1`, `Agent: W2-IMPR-01`, `Wave: 2-improve`.
Final message ≤ 8 lines: `STATUS | agent | finding | report path | commit SHAs | blockers`.
