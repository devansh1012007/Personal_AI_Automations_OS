# ADR-0054: Effect registry v1 — effects as ledger rows, compensation as new authorized work

## Status

Accepted

*Recorded 2026-11-27 by Wave 9 task C9.2 (the W9-MARKETPLACE board's
core row) over the feat/effects tree. It stands on ADR-0050 (the
pipeline call journal whose `PIPELINE_CALL_COMPLETED` rows carry the
effect field this record adds), ADR-0046 (the v-field
forward-compatibility rule the effect payload follows, and the payload
discipline — data, never prose), and ADR-0045 (the jail-then-protect
workspace order the compensation executor reuses rather than
restates). The VISION appendix's proposed object model is the shape
this record takes the smallest honest slice of.*

## Context

README's Temporal composability section states the rule that motivated
the row: *"Not every effect is reversible. A delivered email cannot
generally be unsent; a payment cannot be treated as undone because a
process was unloaded. PAA must preserve those facts in the ledger and
treat a correction or compensation as a new, separately authorized
action."* The VISION appendix proposes the full object model — effect
records with an owner, an operation identity, recovery information,
authority provenance and a lifecycle state. Between the rule and the
model sits the question Wave 9 asked: what is the smallest slice that
is *true* rather than aspirational?

Four wrong shapes were available, and each had already been a bug
somewhere in this tree's history:

* a **second store** — an effects table the ledger does not know about
  breaks the one invariant ("the ledger is the truth"); a rebuild from
  events would resurrect effects the table had settled, or lose ones it
  had not;
* **compensation as code** — a callable smuggles a judgment ("how to
  undo this") into the ledger's read path, where it cannot be
  inspected, replayed, or refused;
* **a model deciding how to undo** — an LLM asked "reverse this" is a
  heuristic wearing an authorization, and the rollback is exactly the
  operation that must be deterministic;
* **an overstated report** — a rollback that says "done" while an
  effect it could not compensate sits unmentioned is worse than no
  rollback, because the operator stops checking.

And one structural temptation: a new `EFFECT_*` event family, which
would mean new ceremony — v=1 goldens, ADR-0008 discriminator scheme
extensions, a replay-total fold property, boot-sweep exemptions — for
rows whose information content is already carried by the call that
performed the effect.

Where effects actually happen today is also where the decision had to
sit: pipeline runs journal their calls through ADR-0050's
`CallJournal`, and the mutating stages that produce workspace file
effects ride those lineages. An effect registry that did not read
*those* rows would be indexing a world the platform does not run in.

## Decision

1. **Effects ride existing ledger rows — no new `EventType`.** An
   effect is a versioned `effect` field on the
   `PIPELINE_CALL_COMPLETED` payload, written through exactly one
   door: `CallJournal.call_completed(effect=..., compensates=...)`.
   The door aborts before appending if the spec's owner is not the
   call it rides (the same abort rule as a non-monotonic seq), and the
   field is absent on every row written before the door existed or by
   call sites with no effect to declare. No new event type means no
   new ceremony: the v=1 goldens, the `call:<seq>:COMPLETED`
   discriminator, the replay-total fold and the boot-sweep exemptions
   keep holding unchanged, and `tests/effects/test_journal_door.py`
   pins each half of that claim.

2. **The registry is a reader, never a second store.**
   `paa.effects.registry.EffectRegistry` holds no state; every call
   re-reads. `effects_for_scope` derives a scope's effects
   **newest-first** (the LIFO disposal rule — unwinding happens in
   reverse application order); `compensated_effect_ids` derives the
   idempotency set from `compensates` references on completed rows
   across *all* lineages. Blobbed rows resolve through the blob store;
   a row that cannot resolve **refuses** the derivation, because an
   effect that reads as absent is one a rollback would re-compensate,
   and a compensation that reads as absent is one it would
   double-compensate. The scan saturating its row limit refuses too —
   a truncated set is the double-compensation bug wearing a success
   exit code.

3. **Compensation is data, never code.** A `CompensationSpec` is
   `remove` (the effect created the file; nothing existed to restore)
   or `restore` (the prior bytes, attested by digest, at a
   content-addressed blob ref) plus a workspace-relative, normalized,
   length-bounded path. The effect id is deterministic —
   `sha256("<lineage>|<seq>")` — so a compensation row's reference
   names exactly one effect with no join table, and the compensated
   set needs no counter. Payload discipline is ADR-0046's: a `v`
   field, closed vocabularies, digests, never file content. Readers
   are total: a malformed field reads as *no effect* and the row stays
   in the ledger, hash chain intact.

4. **The new-authorized-action rule is mechanical, not rhetorical.**
   `paa rollback <scope>` never edits history and never "undoes". Each
   uncompensated effect is settled as a fresh journalled run —
   `run_kind="rollback"` on a fresh lineage uuid — through the same
   PROPOSED → work → COMPLETED vocabulary every pipeline call uses,
   with the completion row carrying `compensates: <effect_id>`. A
   rollback's own file writes register **their own** effects (the
   restore is itself a file mutation with its own captured prior), so
   the ledger's account stays complete: rolling back a rollback
   reverses it. Because the compensated set is derived from the ledger
   at every run — never held in process state — a re-run after a crash
   completes exactly the remainder, and a second run after success is
   an honest no-op (exit 1, every effect reported
   `already_compensated`).

5. **The v1 compensable/not boundary, stated.** Compensable in v1:
   workspace file effects — `file_write`/`file_edit`, compensated by
   restore-with-digest-verification or remove, jailed by the same
   workspace rules any file op passes (`assert_inside_workspace`, both
   sides fully resolved). Everything else is the honest marker:
   `irreversible`, reported with its owner call and the plain
   statement that no compensation exists, never "fixed". The v1 scope
   is the effect-bearing call's own lineage — owner lineage and scope
   must coincide, checked at construction — because that is the
   lineage the registry can derive today; resolving a *task* to its
   runs' lineages, and widening the owner set to `TOOL_CALL_*`, are
   v2 changes that arrive with their own producers.

6. **Crash safety is inherited, not rebuilt.** A rollback lineage *is*
   a pipeline-run lineage, so the boot sweep already skips it
   (`SKIPPED_PIPELINE_LINEAGE`) and already closes a `PROPOSED` row a
   kill left dangling as `interrupted`. The executor is idempotent in
   outcome: a restore onto bytes that already match reports
   `already_prior` and writes nothing; a remove of an absent file
   reports `absent`. A kill between the file work and the completion
   row re-runs into a no-op, not a double-write. The kill-9-mid-rollback
   drill (`tests/effects/test_rollback_chaos.py`) pins all four walk
   boundaries: before the run row, between effects, between an
   effect's file work and its completion row, and after the last
   completion.

7. **Determinism is total — logic, not LLM.** No model, no heuristic,
   no ordering beyond the seq-descending walk, no judgment about what
   "undo" might mean. One compensation failing does not stop the walk:
   the failure is journalled (`call_failed`) and reported, and a
   re-run retries exactly the effects whose compensation is not
   recorded as completed. The CLI exit contract states the same rule
   for the operator: 0 iff this run compensated at least one effect;
   1 nothing to roll back (unknown scope, no effects, nothing
   compensable, all settled, or every attempt failed); 2 the named
   usage refusals — a scope that is not an id, or compensable file
   effects without the workspace jail they are owed.

## Consequences

Costs, stated: v1 compensates only workspace files — an email that was
sent, a process that was spawned, an API call that was made is
*reported*, not reversed, unless the call site declares a compensation
the data vocabulary can carry. The task-level scope is deferred: an
operator rolling back "the task" rolls back each run lineage it
comprises, which v1 leaves to the operator. The compensated-set scan
refuses at ten thousand rows rather than reading a prefix, so a ledger
past that size owes the derivation an index before rollback works at
all — a loud refusal, deliberately, in place of a silent truncation.

The honesty line, stated rather than implied: the registry and the
rollback are readers over the append-only ledger, and their truth is
replay's truth — a rebuilt process reconstructs the identical view
because there is nothing else to reconstruct. A rollback that cannot
run honestly as asked refuses and names why; irreversible effects are
counted in the exit path and reported with their owner, never folded
into a green checkmark. And the door stays single: a future writer
that mints effect rows outside `CallJournal.call_completed` is the
drift this record exists to prevent, and the owner check is what
catches it.

To move back: widening to `TOOL_CALL_*` owners, splitting scope from
owner lineage, and new compensation actions (a lease release, a
process kill) are each v2 changes that must arrive with their own
producer, their own payload version bump, and their own replay-total
readers — the v=1 readers refuse them loudly rather than guessing.
The two structural decisions — effects as fields on the rows that
performed them, and compensation as new authorized work on fresh
lineages — should not move at all; reverting either reopens the
second store or the history edit this record exists to prevent.
