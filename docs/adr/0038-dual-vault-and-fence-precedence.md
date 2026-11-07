# ADR-0038: The dual vault is 15 managed files plus 2 governance files, fenced by conflict error, never last-writer

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. The enum
extension, the governance subdir support, the conflict check and the
reflection delegation are the P-010 implementer's to write. This ADR settles
the file arithmetic, the conflict definition, the dead-writer wiring decision
and the placement of the governance files — including which part of the
placement is plan-original rather than RFC-derived.*

## Context

The vault the code governs today is four files wide. `BeliefDocument` in
`src/paa/memory/world_model.py:69` names `current_focus.md`,
`strategic_risks.md`, `active_constraints.md` and `operating_themes.md` — and
`WorldModel.update_belief_state` in `src/paa/memory/world_model.py:218` has
**zero production callers**. The one live writer of a vault file is not this
writer at all: the reflection engine writes `playbooks.md` through a second,
duplicate fence implementation, for a file the enum does not govern.

The RFC §9 set is thirteen files. The overlap between the live four and the
RFC thirteen is two (`current_focus`, `operating_themes`). The arithmetic
follows and is verified, not assumed: 13 RFC files + 2 live-only files
(`strategic_risks`, `active_constraints`) = **15 managed files**; the RFC-only
eleven are the new files to add. And the fence today is marker-only:
`WorldModel._replace_block` is an unconditional substitution, so a human edit
*inside* the managed block is silently overwritten, and no conflict error
exists anywhere in the code.

## Decision

**The managed set is the 15-file union: the thirteen RFC §9 files plus the
two live-only files.** `BeliefDocument` grows by the eleven RFC-only names
(`constitution`, `principles`, `goals`, `anti_goals`, `projects`, `skills`,
`playbooks`, `reflections`, `risk_rules`, `agent_hierarchy`,
`prompt_contracts`); no member is removed, so the CLI's enum coercion accepts
strictly more names than before. `anti_goals.md` becomes governed under a
flat path — the runtime already reads it directly, and governing it must not
move it.

**Governance is plan-original, and the ADR says so.** The two governance-only
files, `identity.md` and `strategy.md`, are created in a `vault/governance/`
subdirectory. Neither name appears in RFC §9; both appear in the RFC only as
domain-table rows. The placement is the plan's design, not an RFC reading —
honesty about which of the 17 files (15 managed + 2 governance) is RFC-derived
matters because a future reader will otherwise go looking for an RFC section
that does not exist. Supporting the subdir requires a named
`WorldModel`/`path_for` task: `path_for` is currently a flat join
(`src/paa/memory/world_model.py:186`) and gains a subdirectory branch for the
governance files only. Constitution and principles are the *same* RFC files,
not copies — one truth per name.

**Precedence is a chain, not last-writer:** human-edited > governance >
managed block. Sync direction is human-to-agent. The conflict definition is
exact: a **human edit inside the managed block since the last atomic write
is a conflict** and raises a named-path error — the path is named, the
files are not overwritten. A human edit *outside* the managed block is
preserved, as today. `vault_sync.enabled` defaults to `False` until one
supervised manual sync has passed.

**The dead writer gets wired or the files are orphans.** This ADR decides the
question the plan leaves open: `update_belief_state` will have a production
caller (the sync loop the P-010 design names), and the reflection playbook
writer is refactored in the same task to delegate to it instead of running
its duplicate fence implementation. Two fence implementations that must agree
on marker spelling byte-for-byte are one edit away from fighting. As part of
the same task, the `source` argument the writer takes is written into the
generated document header — today it is accepted and ignored, and "who wrote
this block?" is unanswerable when debugging a sync conflict.

### Alternatives rejected

* **Last-writer-wins sync.** Rejected: it silently overwrites human edits —
  the exact data-loss failure the fence exists to prevent.
* **Dropping either set (pick-one).** Rejected: it orphans the other set's
  readers and writers, including the runtime's direct `anti_goals` read.
* **Governance files flat in the vault root.** Rejected: it contradicts the
  plan's named subdir task and stores operator-governance files where
  managed-sync tooling will pick them up.
* **Leaving the reflection shadow writer in place.** Rejected: two fence
  implementations is the drift that already produced one stale docstring in
  this area.

## Consequences

**What it costs.** Eleven new vault files, two governance files, an enum
extension, a `path_for` subdir branch, a conflict check (read-compare against
the last atomic write), and one delegation refactor. The vault contains no
secrets — that is stated here as a constraint on everything written into it,
not a discovery about its current contents.

**What it does not change.** Today's four files keep their names and paths;
`WorldModel(vault_path)`'s constructor is unchanged; the atomic write
(temp-file plus `os.replace`) stays. The runtime's `anti_goals` line reader
keeps working and must agree with the fence (fence-aware reading or an
explicit note that the managed block is ignored by the policy reader — the
implementer chooses and documents).

**How to roll it back.** `vault_sync.enabled=False`; the files stay. Removing
the new enum members restores the four-file CLI behaviour exactly — nothing
reads the new files yet.
