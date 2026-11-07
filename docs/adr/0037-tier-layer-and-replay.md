# ADR-0037: The T0–T4 tier layer is fact state folded by a tier-dict helper, never by TaskProjection

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. The schema
columns, the v5 migration, the `TIER_DEMOTED` / `TIER_ARCHIVED` members and
the sweeper are the P-009 implementer's to write. The decision this ADR
exists to settle — the **projection target** — is the one the investigation
found the plan silent on, and it is settled here so the sweeper and the
replay test are built against the same target.*

## Context

Retention today is binary: a fact is in the hot set or it is deleted by the
decay sweep below its domain `prune_floor`. A 4-hour tool output and a
90-day semantic memory decay identically once both cross the floor, and
nothing distinguishes *survival* (how long a fact may live) from *hot-set
membership* (whether it is served now).

The plan adds a T0–T4 ladder with per-tier TTLs. The hard question is not
the ladder but the replay rule: "replay rebuilds tier state" cannot be
satisfied by adding two members to `_NO_PHASE_CHANGE` in
`src/paa/ledger/replay.py:291` alone, because there is nowhere for rebuilt
tier to live. `TaskProjection` is task-lineage state, folded by
`correlation_id` — tier is **fact-scoped**, moving per `fact_id`, and an
event fold keyed by task cannot carry it without polluting the task
projection with memory state.

## Decision

**Tier state is folded by a minimal tier-dict helper, not by
`TaskProjection`.** The P-009 implementer adds a pure helper of the shape
`apply_tier_event(state, event)` where `state` is a `dict` of
`fact_id -> tier`, and the events carry `fact_id`, `from_tier`, `to_tier`
and `reason`. The helper is used by the sweeper, by recovery verification
and by the T10b replay test to prove the rebuilt tier equals the live hot
column. `TaskProjection` gains no tier fields, and the tier events do not
appear in `_PHASE_MAP` — they are fact lifecycle, not task lifecycle, and
they join `_NO_PHASE_CHANGE` so replay stays total for old lineages that
never emitted them.

This is the explicit rejection of the plan's implicit alternative: **no
`TaskProjection` tier fields.** A task projection that carried per-fact tier
would need one entry per fact per task, would have to be invalidated when a
fact moved through a different task than the one projecting it, and would
replay identically only if every fact's moves were replayed inside the
task that happened to reference it. The tier-dict helper has none of those
problems: it is keyed by the thing that moves.

**The layer in full, as the P-009 implementer builds it:**

* Columns `tier` and `tier_updated_at` on `hot_serving_active_facts` **only**,
  in both the SQLite and Postgres schemas, in one commit. `Fact` gains no
  field — the sweeper `SELECT`s `tier` itself, so task-lineage reads are
  unaffected.
* One `MigrationRunner` step (v5) whose step itself backfills every existing
  row to **T2** (the 90-day default). The backfill is deterministic, in the
  migration, with **no Settings flag** — a flag would let two deployments
  backfill differently and call it the same schema.
* `TierPolicy.ttl_hours = {T0: 4, T1: 24, T2: 2160, T3: 17520, T4: infinity}`.
  The sweep runs after the curator's DECAY phase in the same tick — no
  parallel prune race.
* **Union precedence, stated as a rule not a hope:** retention decides
  *survival* (does the fact still exist?), `prune_floor` decides *hot-set
  membership* (is it served?). A fact below its floor leaves the hot set
  even when its tier TTL has not expired; a fact past its tier TTL is
  archived even when its decayed confidence is above the floor. Neither
  rule may shorten the other's guarantee — the Hypothesis property in the
  decay suite pins "tier TTL never shortens decay guarantee".
* The sweeper writes the `TIER_DEMOTED` / `TIER_ARCHIVED` event **before**
  the hot-row move, idempotent by the full ADR-0008 key with
  `discriminator = fact_id + to_tier`. A crash between event and move leaves
  `TIER_ARCHIVED` without the row move, which the sweeper treats as
  already-done — the check lives in the sweeper, not in a recovery sweep,
  because the sweeper is the only writer that can be in that state.
  `TIER_ARCHIVED` without a following row move must never re-emit and never
  double-archive.
* `TierSettings.dry_run` defaults to `True` (log only: no events, no row
  mutations). The first non-dry-run tick is the point of no return, and it
  is gated on the R3 snapshot-restore check passing first — reverting
  afterwards means flag off and the column ignored, never "column removed".

### Alternatives rejected

* **`TaskProjection` tier fields.** Rejected for the keying reason above;
  it folds the wrong entity.
* **Tier as derived-from-clock state (no ledger events).** Rejected: wall-clock
  truth in the hot table breaks replayability — the same disease the
  query-time-derivation item exists to kill, in a worse form (deletable
  rows).
* **A separate `memory_tier_state` table fed by events.** Rejected: an extra
  join on every fact read to store what two columns on the fact row carry.
* **Backfill behind a Settings flag.** Rejected: two deployments would
  backfill differently under one schema version.

## Consequences

**What it costs.** Two columns on the hot table (one-way schema), a v5
migration, two event members, one helper, one sweeper, and the discipline
that the sweeper runs after the DECAY phase. The tier columns are stored
state updated by sweeper ticks — the same shape as the TTL problem — which
is exactly why the event-before-row-move rule and the full idempotency key
are load-bearing here.

**What it does not change.** `DomainPolicy` decay coefficients and
`prune_floor` values, `EffectiveConfidence` derivation, and every task-phase
mapping. A lineage written before the tier exists replays identically: old
events lacking tier default down the T2 path.

**How to roll it back.** `TierSettings.dry_run=True` (or the sweeper off).
The columns stay — one-way by design — and are ignored. The events stay
readable through `_NO_PHASE_CHANGE` and the tier-dict helper.
