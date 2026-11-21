# ADR-0050: Pipeline resumption is host-side call-journal replay, with divergence detection

## Status

Accepted

*Recorded 2026-11-21 for plan item P0.1 of
[docs/plans/2026-11-21-pipeline-platform-spec.md](../plans/2026-11-21-pipeline-platform-spec.md).
This ADR fixes the resumption semantics, the journal record shape, and the
divergence and unknown-outcome rules for every pipeline the platform runs.
It deliberately does not implement the journal — the in-process extraction is
P1, the generalised `CallJournal` and the new `PIPELINE_*` event types are P2 —
so that the semantics are frozen before the first producer is wired in, which
is the same order [ADR-0046](0046-coder-event-forward-compatibility.md) froze
the coder event vocabulary before `coder/journal.py` existed.*

## Context

A pipeline is an arbitrary loop in arbitrary code, possibly in another process
and another language. When the host (or the machine) dies mid-run, the run has
to be resumable **without weakening any PAA guarantee** — the ledger is the
truth, and recovery replays it. Four resumption models were on the table
(spec §2.3):

| Model | How it resumes | Verdict |
|---|---|---|
| R1 restart-from-scratch | Re-run; tell the model what was interrupted | Repeats work; unsafe for effects — read-only pipelines only |
| R2 host-owned state machine | The task state is the projection of ledger events; a stage guard (`context_packet is not None`, `completed_steps`) decides what is left | What the built-in orchestrator already is: `src/paa/agents/orchestrator.py:405` runs five idempotent stage guards over `project()` output |
| R3 durable replay with a host-side call journal | Re-run the pipeline; the host answers already-completed calls from the record, executes only new ones | What the coder tool loop already has at tool-call granularity: `coder/journal.py:116` mints `PROPOSED`/`COMPLETED`/`FAILED`/`DENIED` rows keyed by `session:call_id:kind` |
| R4 process snapshot | CRIU / VM snapshot | Not portable, not Windows — rejected |

The two loops that exist today share no interface, and that is the evidence
this ADR rests on: R2 is proven at task granularity (the orchestrator's stage
guards), R3 is proven at tool-call granularity (the coder journal and
`ledger/recovery.py:375`'s interrupted-call sweep). Neither alone covers the
other's case: an arbitrary loop is opaque to the host, so the host cannot
re-derive "where it was" from events alone (R2's requirement), and stage
guards cannot be written by a third party in another language (R2's cost).
[VISION.md](../VISION.md) constraint 3 ("recorded results — replay reads the
record and never re-executes") already commits the runtime to R3's shape for
anything non-deterministic; this ADR extends that commitment from one model
call and one tool call to every capability call a pipeline makes.

## Decision

**R3 — durable replay with a host-side call journal — is the default
resumption model for code pipelines.** R2 remains the built-in's own form (it
is already correct for it, and P1.2 extracts it without changing it) and is
the natural form a declarative pipeline compiles to; R1 is permitted only for
pipelines whose manifests declare read-only effect classes; R4 is rejected.

The journal is **host-side**: the pipeline process never writes it, never
sees it, and cannot lie to it. Every capability call a pipeline makes carries
a monotonic per-run sequence number `seq` and a fingerprint
`sha256(canonical_json({method, params}))` — the same canonicalisation
`ledger/events.py:40` already uses for every payload hash, so the journal's
identity rules cannot drift from the ledger's.

The host answers a call as follows (spec §3.3, frozen here):

```text
on call(seq, method, params):
    fp = fingerprint(method, params)
    rec = journal.get(run_id, seq)
    if rec exists:
        if rec.fingerprint != fp:                -> emit PIPELINE_RUN_DIVERGED; fail run
        if rec.outcome in (COMPLETED, FAILED):   -> return recorded result (NO effect)
        if rec.outcome == PROPOSED (no outcome): -> classify (applied / not_applied / unknown)
                                                    unknown + mutating -> error -32008 OutcomeUnknown
    else:
        append PROPOSED(seq, fp, redacted args)  # ledger BEFORE effect
        check capability token, budget, mode
        execute
        append COMPLETED or FAILED(seq, result_sha, blob?)
        return result
```

Three rules follow from that block and are as load-bearing as the block
itself:

1. **Ledger-before-effect, inherited unchanged.** `PROPOSED` is appended
   before execution — the same ordering `coder/loop.py:218` already applies to
   every tool call and the same ordering the whole runtime rests on. A crash
   between `PROPOSED` and the outcome is therefore *recoverable in principle*
   and *classifiable in fact*, because the `PROPOSED` row carries the
   persisted intent hashes, exactly as ADR-0046 pins them.
2. **Divergence stops the run.** If the re-run pipeline issues, at some `seq`,
   a call whose fingerprint differs from the record, the host appends
   `PIPELINE_RUN_DIVERGED` and fails the run. It does not "resync", skip, or
   fast-forward: a pipeline that is not replaying its own history is a
   pipeline the record can no longer speak for, and every guarantee below it
   (no duplicate effects, honest budgets) would be a guess. This is the
   property the conformance harness (P3.5) enforces by running every pipeline
   twice and diffing the call sequence.
3. **Unknown outcomes are never auto-repeated.** A `PROPOSED` row with no
   outcome is classified `applied` / `not_applied` / `unknown` by the same
   decision table `ledger/recovery.py:307` already implements for tool calls,
   and a mutating call classified `unknown` is reported to the pipeline as
   `-32008 OutcomeUnknown` — the pipeline decides, the host never re-executes
   on its behalf. This is D-6, lifted from the coder journal
   (`ledger/recovery.py:145`) to the platform, unchanged: repeating a shell or
   git effect whose outcome is unknown can duplicate a commit; asking is
   cheaper than duplicating.

**Cost of a replay.** Replaying N completed calls is N cheap RPC round trips
against the host's in-memory per-run journal view — no model call and no tool
execution is re-performed, because the recorded result (or its blob
reference) is the answer. The journal is a ledger lineage like any other, so
it inherits the store's idempotency (a redelivered `PROPOSED` returns the
existing row, `ledger/store.py:116`) and the hash chain.

**What the pipeline must guarantee for this to work.** Outside host calls the
pipeline must be deterministic — no clock, randomness, environment, or network
except through `ctx.now`, `ctx.random`, and the capabilities, all of which are
journalled. This is the determinism contract; the conformance harness's
double-run is its enforcement, and ADR-0051's facade is where the recorded
surfaces (`now`, `random`) live so a compliant pipeline never has to reach
outside the door to be replayable.

### Event vocabulary this decision implies (P2, not yet landed)

`PIPELINE_RUN_STARTED` / `PIPELINE_RUN_COMPLETED` / `PIPELINE_RUN_FAILED` /
`PIPELINE_RUN_DIVERGED`, and `PIPELINE_CALL_PROPOSED` / `PIPELINE_CALL_COMPLETED`
/ `PIPELINE_CALL_FAILED` per call. All are informational — no phase change, no
terminal set membership — following the coder-event precedent
(`ledger/replay.py:407` `_NO_PHASE_CHANGE`), and all carry `v` payload
versions per ADR-0046. `PIPELINE_CALL_*` records the capability call; a
`use_skill` call **instead** writes the existing `TOOL_CALL_*` rows — that
mapping is ADR-0051's decision, not a second vocabulary — and the spec §3.3
shorthand `PIPELINE_DIVERGED` names the same event as `PIPELINE_RUN_DIVERGED`
above. Nothing in this ADR lands the vocabulary:
P2 does, with replay property tests over all orderings including the new
types, in the house style of `tests/ledger/test_replay.py`.

## Consequences

**What it costs.** Pipeline authors write ordinary code but must keep it
deterministic outside host calls — a real constraint, softened by
`ctx.now`/`ctx.random` and by R2's declarative easy mode. Every capability
call is one more ledger row, so a 400-call run is 400 `PROPOSED` rows plus
outcomes (bounded by the manifest's `max_calls`); the row payloads are
redacted previews and hashes, not arguments, so secrets never land in the
journal. Crash recovery now has two layers — the projection sweep the
runtime already runs, and the per-run journal view the host reconstructs on
restart — and P2 must prove with a property test that they never disagree
about what happened.

**What it does not change.** The built-in orchestrator's resumption story:
P1.2 extracts its stages onto the facade with the stage guards intact, so the
built-in keeps resuming by projection (R2) — correct, already tested by the
chaos suite, and untouched by this ADR's default for *code* pipelines. The
coder journal's protocol and rows: frozen by ADR-0046, reused by
`ctx.use_skill` (ADR-0051), not migrated.

**What remains open, owned elsewhere.** The wire encoding of `seq`,
fingerprints, and outcome codes is `paa-pipeline/1` (P3), which stays draft
until three pipelines run on it. External harnesses (`observe`, `acp-client`,
`mcp-host`) **cannot** honour this contract — the host does not control their
loop — so their manifests declare `resume = "none"` and the host refuses to
mark such a run recoverable; that honesty rule is the spec's §6, not this
ADR's to decide.

**What would change our mind.** If the validation trial (VISION Horizon 5
gate) shows every real pipeline is either the built-in (R2 already covers it)
or declarative (compiles to R2), the journal becomes dead weight carried for
no caller — at which point the correct move is to keep the semantics frozen
here and not build P2's producer side, not to delete the rules that already
matched the coder's proven behavior.

**Alternatives rejected.** R1 restart-with-note, because repeating an effect
the record cannot see is the one failure this runtime treats as unrecoverable
by policy. R4 snapshots, because a boundary that does not survive an OS
change or a Windows host is not a boundary. "Divergence auto-resync by
skipping to the next matching fingerprint", because a silent skip is exactly
the forged-history class of bug the hash chain exists to prevent.
