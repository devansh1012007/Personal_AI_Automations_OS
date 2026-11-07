# ADR-0036: The allocator is a ledger-wired loop around the ADR-0012 score, off by default

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. The code this
ADR describes — `src/paa/context/allocator.py`, the new `EventType` members,
the drain throttle in `src/paa/memory/creator.py` — is the P-007
implementer's to write. This ADR records the decisions that implementer
must not have to re-derive: the loop's shape, the derivation of its five
inputs, the starvation fallback, the `StreamName` waiver, the drain default,
and the §P01 truncation bound the redaction fix inherits.*

## Context

The attention formula exists and is used by nothing.
`attention_allocation_score` in `src/paa/context/metrics.py` is the
ADR-0012-corrected form, `(K·I)/(F·(1+P)·(1+R))`, pure, exported, unit-tested
— and has zero callers in the runtime. Ranking today is gatherer-local, so
the answer to "why was this retrieved?" is not written down anywhere.

The queue half of the loop is equally unwired. The `workers:alloc` stream is
declared among the eleven `StreamName` channels in
`src/paa/storage/queue/base.py:48`, with the wire value
`src/paa/storage/queue/base.py:60`, and has neither a producer nor a consumer:
the allocator is the first substantive pair the stream will ever carry.

Two of the formula's five inputs have no live producer, and the drain that
would feed the loop is unthrottled. `drain_pending_embeddings` in
`src/paa/memory/creator.py:1497` embeds a batch of 100 per call on three
call sites, with no rate cap and no backpressure log.

**What the redaction item (§P01) leaves behind.** The two unredacted
`str(exc)` paths the plan fixes carry arbitrary exception text to a log line
and to the operator console. The sites themselves have no secrets in scope at
the inspection point, so the plan's posture is best-effort: scrub what the
patterns catch, then bound the rendered text. The bound is 200 characters, and
it lives here rather than silently in a call site, because a bound that nobody
wrote down is a bound that drifts.

## Decision

**A new `src/paa/context/allocator.py` consumes `workers:alloc` (claim-ack),
scores each item with `attention_allocation_score`, and notifies the
controller about the top `max_results` items — 5 by default — sorted by
score.** The gatherer is untouched and never blocks on the queue: when the
queue is empty or its payload is stale the allocator is a no-op (plus an
`allocator.underfed` metric), and ranking falls back to today's
gatherer-local order. `allocator.enabled` defaults to `False`, so a
deployment that never sets the field keeps today's behaviour exactly.

**The five inputs are derived as follows** (no producer is invented
silently; each rule names the input it reads):

* `K` (criticality) is task-level, not fact-level: it comes from the task
  request payload, defaulting to `0.5` when the request names no priority.
  No hot row carries it — assigning a derivation was this ADR's job, and a
  payload default is honest about what is unknown.
* `I` (information density) is the packet density where a packet exists,
  otherwise the item's relevance/confidence projection.
* `F` (cognitive effort) defaults to `1.0` (unit effort) when no plan-cost
  estimate is available; `planning_cost` may supply it when a plan tree
  exists.
* `P` (context pollution) is `context_pollution_ratio` from the enclosing
  packet.
* `R` (hallucination risk) defaults to `0.0` when no risk signal exists for
  the task; the divisor `(1+P)·(1+R)` already guards the zero case, so a
  missing signal never divides by zero and never inflates the score.

The defaults are starting points, not truths: if the allocator's audit log
shows K or R pinned at default on every run, the derivation — not the formula
— is what changes, and it changes here.

**Allocator decisions are ledger events, not side effects.** The run emits an
allocator-decision `EventType` (name chosen by the P-007 implementer) that
maps to `_NO_PHASE_CHANGE` in `src/paa/ledger/replay.py:291`, never to
`_PHASE_MAP`, and is not named in `TERMINAL_EVENTS`. The reasoning is the
same as the tier precedent's: an allocation decision is information about a
task, not a move of the task's lifecycle. Idempotency uses the full ADR-0008
key — `correlation_id`, `event_type`, `attempt`, `discriminator` — emitted
by the orchestrator before any notify side effect.

**The `StreamName` StrEnum breach is waived, not fixed.** `StreamName` is
`enum.StrEnum` in `src/paa/storage/queue/base.py:48`, where the house
convention is `class X(str, enum.Enum)` and `UP042` is deliberately ignored
in the project configuration. The waiver is recorded here rather than
"fixed" by re-declaring the enum, because the values are wire keys (stream
name suffixes and stored rows) — renaming the declaration form for style
while the values are already on disk buys nothing and risks the values, and
the lint ignore already documents the exception. Do not "fix" the enum; fix
the ignore if the convention ever changes.

**Drain throttle.** `drain_max_per_second` is declared in `MemorySettings`
in `src/paa/config.py` (the memory domain owns the drain) and enforced by
P-007, its sole owner. The plan names the Setting but no default; the
default here is **100**, the current batch size
(`src/paa/memory/creator.py:1497`) at the daemon's ~1 Hz drain cadence, so a
deployment that never sets the field sees exactly today's throughput. Above
the cap the drain defers and logs backpressure — never drops silently.

### §P01 — the 200-character bound (specification for the P-001 code)

`redact` in `src/paa/models/base.py:256` keeps its semantics unchanged: the
literal-secrets pass and the three generic patterns, no truncation inside.
`redact_detail` in `src/paa/models/base.py:278` keeps its rule: string values
scrubbed at any depth, keys never rewritten. When a call site has no secrets
in scope — the two plan P-001 sites — the posture is:

* run `redact(str(exc))` (or `redact_detail` on the value) over the text;
* truncate the scrubbed text to **200 characters**;
* append an explicit truncation marker so a truncated error is visibly
  truncated, never silently short.

Per-site applicability, stated so the next implementer does not generalise it: the 200-character bound applies to the rendered operator-console text at the CLI drain site (`src/paa/cli/main.py:635`, `_MAX_DRAIN_ERROR_CHARS`), where the truncation marker is inside the 200-character budget — the rendered error is at most 200 characters including the marker. The runtime site (`src/paa/runtime.py:482`) is a structlog keyword argument, not rendered console text; it is redact-only and unbounded, mirroring the existing `src/paa/runtime.py:474` site, because truncating a structured log field would silently drop diagnostic detail without protecting a terminal.

200 is the house excerpt bound (sibling constants in the tree use the same
value), not a new invention; the point of this section is that the number is
written down where both fix sites can cite it.

### Alternatives rejected

* **Gatherer-inline allocator (no queue, rank in `compile`).** Rejected: the
  ranking decision would be a non-replayable side effect, and the plan's
  pre-mortem (allocator starves gatherer) becomes possible by construction.
  The queue + ledger shape is the replayable one.
* **Fix the `StrEnum` to `class X(str, enum.Enum)`.** Rejected: the values
  are already wire keys on disk; the lint ignore documents the exception and
  the risk is all downside.
* **A separate `DrainSettings` sub-model for `drain_max_per_second`.** Rejected:
  the drain options already live in `MemorySettings`, and one more Settings
  prefix for one field is a second place to look for the same knob.
* **Leave the 200-char bound in the call sites.** Rejected: it is the exact
  drift this run is cleaning up — two sites, two memories of a number.

## Consequences

**What it costs.** A new module, two new `EventType` members (allocator
decision and drain-backpressure telemetry as the implementer splits them),
one `Settings` sub-model, and the K/F/R defaults above to defend in review.
The allocator adds a claim-ack consumer to a stream that has never carried
traffic, so early runs are where bugs will surface — which is why the flag
ships `False`.

**What it does not change.** The ADR-0012 formula, the gatherer, the `redact`
semantics, and `StreamName` wire values are untouched. A lineage written
before the allocator exists replays exactly as it did: the new members are
additive, and old events never name them.

**How to roll it back.** Set `allocator.enabled=False` — the loop stops
consuming, the gatherer ranks as before. The events in the ledger remain
readable (they map to `_NO_PHASE_CHANGE`); the drain cap reverts by setting
`drain_max_per_second` high. Nothing is migrated.
