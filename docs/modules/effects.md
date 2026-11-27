# Module: Effects (`src/paa/effects/`)

## Module identity

- **Name:** effects — the effect registry v1 and the rollback service (W9-C9.2, ADR-0054)
- **Path:** `src/paa/effects/`
- **Main responsibility:** The smallest honest slice of README's Temporal composability rule: every journalled side effect is a *record* in the ledger (an `EffectSpec` riding the `PIPELINE_CALL_COMPLETED` row that performed it), the registry is a **reader** that derives a scope's effects and the already-compensated set from those rows, and `paa rollback` settles a scope's uncompensated effects as **new, separately authorized, journalled actions** — never an undo, never history editing.
- **Architectural layer:** Core / ledger reader (no store of its own)
- **Entrypoints:** `EffectSpec.for_call()` (`effects/spec.py:313`); `capture_prior()` (`effects/files.py:96`); `EffectRegistry.effects_for_scope()` / `compensated_effect_ids()` (`effects/registry.py:141`, `effects/registry.py:170`); `rollback_scope()` (`effects/rollback.py:155`); the CLI door `paa rollback <scope>` (`cli/main.py:266`)
- **Public interfaces:** `EffectKind`, `CompensationAction`, `CompensationSpec`, `OwnerRef`, `EffectSpec`, `effect_id_for` (`spec.py`); `CompensationOutcome`, `capture_prior`, `apply_compensation` (`files.py`); `EffectRegistry`, `EffectRegistryError`, `ScopeEffect`, `EFFECT_BEARING_EVENT_TYPES` (`registry.py`); `EffectOutcome`, `RollbackRefused`, `RollbackReport`, `rollback_scope`, the four `STATUS_*` words (`rollback.py`)
- **Other modules that depend on it:** `pipelines/journal.py` (the one writing door, at module scope — the spec imports nothing beyond the stdlib, so no cycle) and `cli/main.py` (the verb, imported inside the command). Nothing else writes or reads effects: the package is deliberately at the edge of the ledger, not woven through it.

## Purpose and responsibilities

The README states the rule this package implements: *"Not every effect is reversible. A delivered email cannot generally be unsent; PAA must preserve those facts in the ledger and treat a correction or compensation as a new, separately authorized action."* The VISION appendix proposes the full object model (owner, operation identity, recovery information, authority provenance, lifecycle state); v1 takes the slice that can be **true rather than aspirational**, and states every boundary:

1. **Effects are ledger rows, not a second store.** An effect is a versioned `effect` field on a `PIPELINE_CALL_COMPLETED` payload (`core/types.py:332` is the event; the field rides it), written through exactly one door — `CallJournal.call_completed(effect=..., compensates=...)` (`pipelines/journal.py:366`). The registry holds no state; a rebuilt process reconstructs the identical view because there is nothing else to reconstruct.
2. **Compensation is data, never code.** A `CompensationSpec` is `remove` or `restore` plus a workspace-relative path, a digest and a blob address (`effects/spec.py:164`) — never a callable, never a model judgment. The rollback is exactly the operation that must be deterministic: no model, no heuristic, no ordering beyond the seq-descending walk.
3. **Rollback is new authorized work.** Each compensation is a fresh journalled run (`run_kind="rollback"`, `effects/rollback.py:71`) whose completion rows carry `compensates: <effect_id>` — the reference the idempotency derivation reads back. A re-run after a crash completes exactly the remainder; a second run after success is an honest no-op.
4. **Irreversible effects are reported, not "fixed".** The report names the effect, its owner call, and states plainly that no compensation exists. A report that overstates recovery is worse than none.

## Internal implementation

### `effects/spec.py` — the record
`EffectSpec` (`effects/spec.py:270`) is the frozen unit the registry indexes and the rollback walks: `effect_id`, `kind`, `scope`, `owner`, optional `compensation`. Three rules make it honest:

- **The id is deterministic**: `sha256("<lineage>|<seq>")` (`effect_id_for`, `effects/spec.py:108`), so a compensation row's reference names exactly one effect with no join table, and construction *checks* the invariant — an id that does not derive from the owner's `(lineage, seq)` is refused, because it would silently break the compensation join and the crash-resume derivation.
- **The owner is a reference, not a copy**: `OwnerRef` (`effects/spec.py:239`) is `(lineage, seq, event_type)` resolving to exactly one row through the journal's ADR-0008 discriminator `call:<seq>:COMPLETED`. The owner vocabulary is closed at `OWNER_EVENT_TYPES` (`:65`) — `PIPELINE_CALL_COMPLETED` alone in v1; widening to `TOOL_CALL_*` is a v2 change that arrives with its own producer.
- **Payload discipline is the journal's, restated** (`to_payload`/`from_payload`, `:336`, `:354`): a `v` field (currently `1`, `:59`), closed vocabularies (`EffectKind` `file_write`/`file_edit`/`irreversible`, `:74`; `CompensationAction` `remove`/`restore`, `:92`), a workspace-relative path bounded at 1024 chars (`:71`), digests — never file content, never free text. Reading is total: a malformed `effect` field reads as *no effect* and the row stays in the ledger, hash chain intact.

The v1 scope rule is checked at construction: owner lineage and scope must coincide (`effects/spec.py:298`) — that is the lineage the registry can derive today; the task-to-runs resolution is a named v2 boundary (ADR-0054).

### `effects/files.py` — the v1 compensable surface
Two halves, both deterministic. `capture_prior` (`effects/files.py:96`) is the call-site half, called *before* a mutating effect lands: prior bytes go to the content-addressed blob store and come back as the `CompensationSpec` to attach — `remove` for a creation, `restore` (digest + blob address) for a replacement. `apply_compensation` (`effects/files.py:141`) is the executor half: jailed through the same workspace rules any file op passes (`_jailed_target`, `:80`, over `sandbox/base.py:628`), restoring only bytes that verify against the recorded digest (`:193`), and **idempotent in outcome** — a restore onto matching bytes reports `already_prior`, a remove of an absent file reports `absent`. That is what makes crash-resume safe: a kill between the file work and the completion row re-runs into a no-op, not a double-write.

### `effects/registry.py` — the reader
`EffectRegistry` (`effects/registry.py:95`) is constructed with the store (and the blob store when the deployment has one) and holds nothing else. `effects_for_scope` (`:141`) reads the scope's lineage, keeps `PIPELINE_CALL_COMPLETED` rows (the one effect-bearing type, `EFFECT_BEARING_EVENT_TYPES`, `:73`), parses the `effect` field totally, and returns the effects **newest-first** (`:167`) — the LIFO disposal rule: unwinding happens in reverse application order. `compensated_effect_ids` (`:170`) scans completed rows across *all* lineages for `compensates` references: the idempotency oracle, recomputed from the ledger on every run. Two refusals rather than degradations: a blobbed row that cannot be resolved raises `EffectRegistryError` (`:76`) — an effect reading as absent is one a rollback would re-compensate, a compensation reading as absent is one it would double-compensate — and a scan that saturates its 10,000-row ceiling (`:63`) refuses rather than read a prefix (`:183`), because a truncated set is the double-compensation bug wearing a success exit code.

### `effects/rollback.py` — the service behind `paa rollback`
`rollback_scope` (`effects/rollback.py:155`) derives effects and the compensated set, then walks newest-first. Per uncompensated compensable effect: `call_proposed` → capture the compensation's **own** prior (`:254` — the restore is itself a file write with its own effect, so rolling back a rollback lineage reverses it) → the file work → `call_completed` carrying both the run's own effect and `compensates` (`:272`). One failing compensation does not stop the walk: it is journalled `call_failed` and reported, and a re-run retries exactly the unsettled remainder. `RollbackReport` (`:106`) never overstates — four status words (`:75`): `compensated`, `already_compensated`, `irreversible`, `failed` — and `exit_code` (`:136`) is derived from the entries, so the summary and the shell code cannot disagree. A compensable effect with no workspace root or no blob store is refused up front (`RollbackRefused`, `:81`, `:184`): a rollback that silently skipped file compensations would be the exact overstatement this module refuses to make.

### `pipelines/journal.py` — the one door (ADR-0054)
`call_completed` (`pipelines/journal.py:366`) accepts an optional `EffectSpec` and a `compensates` effect-id reference. The door **aborts before appending** when the spec's owner is not the call it rides (`:393`) — the same abort rule as a non-monotonic seq — because an effect ridden by the wrong row would misattribute ownership. No new `EventType` was needed and none was added: the v=1 goldens, the ADR-0008 discriminator scheme, the replay-total fold and the boot-sweep exemptions keep holding unchanged, and a rollback lineage — written through this same journal — inherits the pipeline lineage's sweep exemption for free (`SKIPPED_PIPELINE_LINEAGE`, `ledger/recovery.py:154`).

## The CLI door

`paa rollback <scope> [--workspace DIR] [--json]` (`cli/main.py:266`) maps the report to the operator's shell: exit **0** when this run compensated at least one effect (irreversible effects may be reported alongside), **1** for nothing to roll back (unknown scope, no effects, nothing compensable, all already settled, or every attempt failed), **2** for usage — a scope that is not an id (checked before the runtime is built) or compensable file effects without `--workspace`, the named refusal. `--json` prints the report verbatim; the human table colour-codes the four statuses and names each effect's owner call.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `EffectSpec.for_call(lineage, seq, kind, compensation)` | a call about to complete | `EffectSpec` | none (pure) |
| `capture_prior(root, rel_path, blob_store)` | workspace + path | `CompensationSpec` | prior bytes to the blob store |
| `EffectRegistry.effects_for_scope(scope)` | a lineage uuid | `list[ScopeEffect]` newest-first | none (read) |
| `EffectRegistry.compensated_effect_ids()` | — | `set[str]` | none (read) |
| `apply_compensation(root, spec, blob_store)` | workspace + spec | `CompensationOutcome` | one jailed file write/unlink |
| `rollback_scope(store, scope, …)` | ledger + workspace | `RollbackReport` | a fresh rollback lineage of journalled calls |

## Failure behavior

- Malformed `effect` field on a ledger row → reads as *no effect*; the row stays, the hash chain still attests it.
- A blobbed row that cannot be resolved (no blob store, or the blob fails) → `EffectRegistryError` refusing the derivation — never a silent absence.
- Compensated-set scan at the 10,000-row ceiling → refusal naming the limit, never a prefix read.
- Compensable effects without `--workspace` / blob store → `RollbackRefused` (CLI exit 2), before any effect is walked.
- One compensation failing (digest mismatch, unwritable target) → `call_failed` + a `failed` report row; the walk continues.
- A kill mid-walk → the boot sweep closes the dangling `PROPOSED` row as `interrupted`; the re-run derives the remainder from the ledger and settles each effect exactly once (the kill-9 drill pins all four walk boundaries).

## Known gaps

- v1 compensates **workspace file effects only**. External effects default to the `irreversible` marker unless the call site declared a compensation the data vocabulary can carry — an email that was sent is *reported*, not reversed.
- The scope is the effect-bearing call's own lineage; resolving a *task* to its runs' lineages is left to the operator in v1 (ADR-0054 states the boundary).
- The compensated-set scan refuses at ten thousand rows; a ledger past that size owes the derivation an index before rollback works at all — a loud refusal in place of a silent truncation.
- No producer inside the platform mints effect rows yet beyond the rollback itself and the tests: the door exists, the call sites arrive with their own waves.

## Related documents

- [`docs/adr/0054-effect-registry-v1.md`](../adr/0054-effect-registry-v1.md) — the decision this module implements
- [`docs/adr/0050-pipeline-host-and-call-journal.md`](../adr/0050-pipeline-host-and-call-journal.md) — the journal whose rows carry the effect field
- [`pipelines.md`](pipelines.md) — the call journal, replay and the vocabulary the rollback rides
- [`ledger.md`](ledger.md) — the store under both readers
- [`sandbox.md`](sandbox.md) — the workspace jail the compensation executor reuses
- [`cli.md`](cli.md) — the verb's exit contract in the CLI surface
- The done-when as tests: `tests/effects/test_spec.py` (44), `tests/effects/test_journal_door.py` (6), `tests/effects/test_rollback.py` (8), `tests/effects/test_rollback_chaos.py` (4 — the kill-9 drill), `tests/cli/test_rollback_cli.py` (9)
