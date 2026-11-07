# ADR-0043: The graph has 12 edge types; "9 extra" is a phantom

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. There is no
code to write for this item: the decision is a count, and the lock that
holds the count is T13. This ADR is the table T13 pins.*

## Context

The plan's graph item began as "add 9 extra edges" — a claim that matched
neither source it attributed to itself. The code's `EDGE_TYPES` in
`src/paa/storage/graph/base.py:68` holds **12** members. RFC §7.3 lists
**11**. Neither number is "9 extra" over anything: 12 is not 11 plus 9, and
11 is not 12 minus 9. The phrase is unsourced — a phantom that, left open,
would justify adding nine edges nobody specified, into a graph whose
traversal semantics (simple paths, live-only, directed, bounded) are
load-bearing.

## Decision

**The set is 12 = 11 RFC §7.3 + `MUTATES` (RFC §1.3).** The module's own
comment says the same thing in the same place: the eleven are the RFC §7.3
set, and `MUTATES` is the twelfth, declared in RFC §1.3. The table, which is
the lock's reference and must match `EDGE_TYPES` exactly:

| Edge type | Source |
|---|---|
| `DEPENDS_ON` | RFC §7.3 |
| `PART_OF` | RFC §7.3 |
| `DERIVED_FROM` | RFC §7.3 |
| `INVOLVES` | RFC §7.3 |
| `BLOCKS` | RFC §7.3 |
| `CAUSES` | RFC §7.3 |
| `SUPPORTS` | RFC §7.3 |
| `CONTRADICTS` | RFC §7.3 |
| `REFERS_TO` | RFC §7.3 |
| `TRIGGERED_BY` | RFC §7.3 |
| `SIMILAR_TO` | RFC §7.3 |
| `MUTATES` | RFC §1.3 |

**"9 extra edges" is closed as an unsourced phantom.** No code change. No
edge is added, removed or renamed by this ADR. The T13 test asserts set
equality between `EDGE_TYPES` and this table — adding or removing an edge
without updating this ADR fails the build, which is the entire enforcement
mechanism. A future edge, if ever needed, arrives by superseding this ADR
with its RFC section cited, not by drifting the set.

### Alternatives rejected

* **Blind-add 9 edges to reach a number nobody defined.** Rejected: phantom
  edges in a provenance graph are worse than missing edges — traversal
  would offer paths the evidence does not support.
* **Remove `MUTATES` to match §7.3's eleven exactly.** Rejected: it is a
  declared relation in RFC §1.3 and is implemented (both backends declare
  its REL TABLE); removing it to satisfy one section contradicts the other.

## Consequences

**What it costs.** One ADR and one test. The T13 pin is deliberately brittle:
a legitimate edge addition is supposed to hurt (update the table first, then
the test), because an unstated edge is how a graph quietly gains semantics
nobody reviewed.

**What it does not change.** The graph contract, both backends, the
`ck_rel_type` CHECK constraint, and every traversal helper. Twelve is
twelve.

**How to roll it back.** Supersede this ADR with the new table. The test
follows the ADR, not the other way round.
