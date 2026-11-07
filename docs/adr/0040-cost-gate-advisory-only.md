# ADR-0040: The memory-cost gate advises and never deletes

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. The pure
function and the curator suggestion are the P-008 implementer's to write.
This ADR records the one rule that governs both, the two source gaps the
implementer must close rather than paper over, and the meaning of the
threshold.*

## Context

The curator has advisory signals (eviction floors) but no cost signal. The
cost model from the vision document was never ported, and the temptation it
creates — "this fact costs more than it is worth, delete it" — is exactly
the `never-auto-resolve` invariant the curator holds everywhere else: exact
duplicates merge, value-differing rows go to contradiction and then to a
human, and nothing else decides.

The cost function's five terms do not have five live producers. Retrieval,
noise and utility have nearest live metrics; storage and maintenance do
not. A formula whose inputs are invented is a stored second truth wearing a
pure function's clothes.

## Decision

**Pure function, advisory only.** `total_memory_cost(storage, retrieval,
noise, maintenance, utility)` is a pure function of five named terms, wired
into the curator's ANALYZE phase (the last phase, after reconciliation) as a
**logged suggestion** when the total crosses `cost_gate.threshold`. The
suggestion never deletes, never quarantines and never marks a fact stale —
the fact stays in the hot set either way, and a fact whose cost has been
logged as too high still recurs in recall. `cost_gate.enabled` defaults to
`False`.

**`utility=0` returns infinity, never a guard.** Division by utility is the
same shape as the ADR-0012 `P/R=0` handling: the raw form is never evaluated.
A fact with zero utility has infinite cost, and the curator logs the
suggestion. Zero utility must say "infinite", not crash and not quietly
become a large finite number the threshold can wave through.

**The two source gaps are named, and closing them is the implementer's
first job:**

* **Storage** has no metric — no byte or row-count cost exists in the tree.
  The implementer supplies a named derivation (bytes or row share of the
  hot set) or marks the term **unavailable** and excludes it from the total
  loudly. What the implementer must not do is substitute `0` or `1` silently,
  which would make the total a function of a made-up number.
* **Maintenance** has only an adjacency (the decay sweep's own eviction
  counts). Same rule: a named derivation or an explicit unavailable marker.

Until both terms are derived or marked, the gate is a computed number with a
documented hole — and the ADR-0040 record is that document.

**The threshold is a positive scale.** `cost_gate.threshold` defaults to
`1.0` and must be positive: a zero or negative threshold would suggest on
every tick, which is the same as having no gate.

### Alternatives rejected

* **Auto-delete above the threshold.** Rejected: it violates
  never-auto-resolve directly — the cost gate would become a second,
  arithmetic curator that deletes without a human.
* **Payload sweep-refresh of the cost (store the total on the fact).** Rejected:
  a stored cost is a second truth that decays off the clock; the function is
  pure precisely so the number is recomputed, never believed.

## Consequences

**What it costs.** One pure function, one ANALYZE-phase suggestion block, one
`Settings` sub-model, and the two source-gap derivations to defend. The
suggestion is a log line (`cost_gate.suggestions`) — no event, so replay is
untouched.

**What it does not change.** The curator's no-auto-resolve behaviour, the
eviction floors, and every ledger payload. Order of phases is preserved:
the suggestion is computed after reconciliation, never before decay.

**How to roll it back.** `cost_gate.enabled=False` — the function is never
called and nothing is logged. Nothing is migrated, because nothing was ever
stored.
