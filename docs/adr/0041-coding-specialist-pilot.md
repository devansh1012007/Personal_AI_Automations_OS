# ADR-0041: One coding specialist first; the depth-block emits the existing rollback event

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. The role
member, the registration, the specialist package and the orchestrator emit
site are the P-011 implementer's to write. This ADR closes two phantom counts
and settles the one decision the plan text gets wrong about the rollback
event — it records what `STATE_ROLLBACK_TRIGGERED` already does, instead of
claiming a new terminal event.*

## Context

RFC §11.8 lists **10 example specialists**. The plan's "21 specialists" is
therefore a verified phantom — an unsourced count — and the 21-at-once build
it justified is unreviewable. The codebase agrees that nothing is built:
`AgentRole` in `src/paa/core/types.py:424` holds the nine core hierarchy
roles plus the optional router, and the specialists package is an empty
`__init__.py`. Delegation, by contrast, is built: `mediate_delegation` is
the only path by which one agent causes another to run, the delegation graph
enforces depth, cycle and node-count guards, and the recursion ceiling is 2
at the COMPLEX modality — a depth-above-2 delegation today returns a
failure.

**What the rollback event already does — and the correction.** The plan text
says the pilot's block path needs a "terminal in replay" rollback event. That
is not true of the tree. `STATE_ROLLBACK_TRIGGERED` is already defined in
`src/paa/core/types.py:165`; it maps to `TaskPhase.ROLLED_BACK` in
`src/paa/ledger/replay.py:167`, which sits in the **resumable** phases, and
it is excluded from `TERMINAL_EVENTS` in `src/paa/core/types.py:187`. The
recovery engine already emits it on post-crash workspace drift. Wiring a
second, terminal rollback event would contradict the replay tests that assert
resumability: a rolled-back lineage is meant to be resumed. This ADR records
that the event exists, is resumable, and is reused — and that the actual
gap is a missing emitter site, not a missing event.

## Decision

**Pilot-first: exactly one specialist, `coding`.** A new
`AgentRole.CODING_SPECIALIST = "coding_specialist"` member (additive, no
removals), registered in the runtime's agent dict as the eighth entry, routed
only through `mediate_delegation`, with the recursion caps asserted in tests.
The specialist's tool names must match the policy globs
(`DEFAULT_MUTATING_TOOLS`) or be appended to the mutating tuple the coder
build passes, or plan mode silently exempts the state-changing calls. The
tool surface goes through the constrained-JSON adapter (tool names in a
schema enum), so the model physically cannot emit a tool outside the enum.

`specialists.coding.enabled` is a **nested** sub-model
(`specialists` -> `coding` -> `enabled`, env
`PAA_SPECIALISTS__CODING__ENABLED`) so that the four gated specialists grow
one sub-model each with the same shape instead of a flat field renamed on
every addition. The default is `False`.

**The depth-block emits `STATE_ROLLBACK_TRIGGERED` — the existing event.**
When `propose` refuses a delegation (depth above the ceiling, cycle, or node
count), the orchestrator's block path emits `STATE_ROLLBACK_TRIGGERED` for
the task's `correlation_id`, before returning the failure, idempotent by the
full ADR-0008 key. The payload carries additive fields only (`reason`,
`depth`, `ceiling`) alongside the existing drift/restore keys — the
`reason` distinguishes a delegation block from a crash-drift recovery. No new
`EventType` member, no replay change. The event stays resolvable through
`replay.py:167` to `ROLLED_BACK`.

**The gate and the deferrals.** The other four specialists
(research/writing/data-analysis/ops) ship only if the coding pilot's routing
precision reaches **90% over 50 scripted tasks** — the plan's threshold,
adopted here as stated: it is the [ASSUMPTION] figure the plan labels as
such, adequate for a pilot gate, and this ADR adopts it rather than
inflating it silently; the pilot implementer may raise it by superseding this
ADR, not by editing it. The remaining five RFC examples are explicitly
deferred, and the "21" remains closed as unsourced.

### Alternatives rejected

* **A new terminal rollback event.** Rejected: it contradicts the existing
  resumable mapping and would break the replay contract that a
  rolled-back lineage resumes.
* **21 (or 10) specialists at once.** Rejected: unreviewable, and built on a
  count the RFC does not contain.
* **Flat `specialists_coding_enabled` setting.** Rejected: it breaks the
  pilot→4→5 symmetry and forces a rename when the second specialist ships.

## Consequences

**What it costs.** One enum member, one agent registration, one specialist
package, one emit site in the orchestrator's block path, and the 50-task
routing test. The gated four and the deferred five are named in this ADR so
a future task can cite them without re-deriving the counts.

**What it does not change.** `AgentRole` values (additive only),
`mediate_delegation`'s signature, `STATE_ROLLBACK_TRIGGERED`'s payload shape
(additive fields), and the replay phase mapping. Removing the coding entry
makes `mediate_delegation` return "no agent registered" — a failure, not a
crash.

**How to roll it back.** `specialists.coding.enabled=False`, or delete the
package. The ledger events remain readable as `ROLLED_BACK`.
