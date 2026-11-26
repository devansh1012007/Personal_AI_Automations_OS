# Module: The guarded self-improvement loop (`src/paa/improve/`)

## Module identity

- **Name:** improve — `improve.reflection-loop`, `paa improve`
- **Path:** `src/paa/improve/`
- **Main responsibility:** The guarded self-improvement cycle (SPEC 2 Wave 8, row C8.1): reflection over the real ledger → a deterministic patch proposal → deterministic validation → a human gate → a commit that lands only as an attested change bundle (C7.2).
- **Architectural layer:** Control / edge
- **Entrypoints:** `run_improve_pipeline()` (`cli/improve.py:57`); the `improve` verb registered at `cli/main.py:928`; `paa pipeline run improve.reflection-loop`
- **Public interfaces:** `ImproveFacade`, `ImproveReflectionLoop` (`pipeline.py`); `PatchProposal`, `FileChange`, `proposal_from_report` (`proposal.py`); `run_validation`, `ValidationVerdict`, `CommandOutcome` (`validation.py`); `Approver`, `HumanDecision`, `refuse_all`, `terminal_approver` (`gate.py`); `guarded_git_commit`, `is_git_repository` (`gitcommit.py`)
- **Other modules that depend on it:** `cli/improve.py`, `cli/pipeline.py` (the registry door), `pipelines/conformance.py` (the six-check runner)

## Purpose and responsibilities

The owner's "after MVP" item, made concrete: a loop that improves the system
using its own recorded history, guarded at every step that could touch
anything. Five journalled stages on one facade (`improve/pipeline.py:116`), each a
typed operation through the call journal (PROPOSED before any effect, then
COMPLETED or FAILED) and each replay-aware:

1. **Reflection** — the existing Weekly Reflection Engine reads the real
   ledger: `ReflectionEngine.analyze_window` (`improvement/reflection.py:270`)
   is called unchanged, not forked. The finding is a reflection finding
   because the engine produced it.
2. **Patch proposal** — a deterministic artifact, never auto-applied:
   `proposal_from_report` (`proposal.py:173`) synthesises the change from the
   engine's own anti-pattern rule (`improvement/reflection.py:355`) and the
   friction numbers, written to disk as JSON. Every later stage consumes it
   by its SHA-256.
3. **Deterministic validation** — the proposal is applied to a scratch copy
   of the repository and the operator's own gate commands run as plain
   subprocesses (`validation.py:266`); exit codes are the verdict. The real
   tree is never touched; command outputs are journalled by digest only.
4. **Human gate** — the one decision the loop refuses to make itself. The
   decision arrives through the `Approver` seam (`gate.py:134` for the
   terminal prompt); approval *and* refusal are recorded as a
   `HumanDecision` (`gate.py:65`) with identity and time. An unwired gate
   refuses (`gate.py:116`).
5. **Commit** — apply, then attest, then git: the change lands as a C7.2
   attested change bundle (`attest/bundle.py:521`) and the git commit runs
   behind the host-git integrity gate (`gitcommit.py:211`), never around it.

Two lines the module holds at all costs:

- **No gate is ever model-decided** (SPEC 2 §9 rule 2). The declaration
  (`pipelines/slots.py:365`) asks for `commit` and nothing else — no
  `think`, no `use_skill`, no `recall` — and the facade's `think` door is
  closed by the grant set. There is no model anywhere in the loop, so
  validation is exit codes and the only approval that exists is a human's.
- **Self-modification inherits the evidence chain.** The commit stage has
  exactly one door — the attested bundle — carrying the mandate, the diff,
  the observed validation commands and the human approval, verifiable
  offline by the existing C7.2 verify path (`attest/verify.py:296`).

**The honesty line (ADR-0017):** prototypical few-shot classification ≠
gradient meta-learning. This loop is not meta-learning at all — it is a
deterministic function of the ledger's friction numbers driving a guarded
change process. The prototypical classifier the ADR speaks of lives in
`paa.improvement.meta` and stays analysis-only.

## Internal implementation

### `improve/pipeline.py` — ImproveFacade and ImproveReflectionLoop

`ImproveReflectionLoop` (`improve/pipeline.py:830`) drives the five stages in the
row's own order; `ImproveFacade` implements each one — `reflect`
(`:247`), `propose` (`:290`), `validate` (`:338`), `ask_human` (`:402`),
`commit_proposal` (`:430`). The gate's decision reaches the commit stage
through `carry_approval` (`:549`), and the commit stage refuses without a
carried *approval* — the guard runs before any effect, so an unguarded call
leaves the repository byte-identical. Refusal paths are first-class
outcomes: validation failure and human refusal both end the run `failed`
with the reason in the metrics, the proposal artifact on disk, and nothing
applied. A clean history completes and proposes nothing.

### `improve/proposal.py` — the deterministic middle artifact

A `PatchProposal` (`improve/proposal.py:90`) is described change: files, intent,
rationale — the content is the whole file, not a diff, and `apply` refuses
a target that changed since the proposal was synthesised (the
`prior_sha256` jail). The default target is the playbook surface
(`improve/proposal.py:60`), appended inside a managed marker fence that preserves
all human text outside it.

### `improve/validation.py` — exit codes, nothing else

`run_validation` (`improve/validation.py:266`) copies the repository into a
scratch root (skipping `.git` and caches), applies the proposal there, and
runs the operator's commands with the scratch copy as their working
directory. Zero commands is a refusal, not a pass. Each command's streams
are read with a store-cap-and-drain reader (`CommandOutcome`,
`improve/validation.py:78`, cap at `:71`) so the journal carries digests only
while the evidence file carries the capped text the bundle later
attests.

### `improve/gate.py` — the human decision

`HumanDecision` (`improve/gate.py:65`) is the recorded artifact: who, when, what,
why — the vocabulary is closed (`approved`/`rejected`), an anonymous
approval is refused, and `refuse_all` (`improve/gate.py:116`) is the default when
a host wires no seam: a loop that cannot ask must not silently approve.

### `improve/gitcommit.py` — host-side git behind the integrity gate

`guarded_git_commit` (`improve/gitcommit.py:211`) probes the repository's local
config against the coder package's deny table (ADR-0047) before anything
is staged, stages named files only (jailed inside the repository, never
protected paths), commits with fixed argv under a hardened child
environment, and reads the new HEAD — all under a wall budget
(`gitcommit.py:84`).

## Inputs and outputs

**In:** the repository to improve (a git working-tree root), the ledger
(the reflection input), the gate commands (`--gate`, shell-split at the
door, `cli/improve.py:157`), and the approver seam.

**Out:** artifacts under `PAA_HOME/improve/<run-id-hex>/` —
`proposal.json` (the proposal artifact), `validation/repo/` (the scratch
copy), `evidence/validation-NN.json` (per-command records, capped outputs
included), `attest-key.json`, `attest-bundle/` (the C7.2 bundle:
`manifest.json` + `blobs/`), and the git commit in the target repository
itself, whose subject names the bundle id.

## The two doors, and their parity

`paa improve` and `paa pipeline run improve.reflection-loop` are the same
cycle through one shared driver (`cli/improve.py:57`), so both journal
identically and leave the same artifacts — the observe pattern, restated.
Exit codes follow the observe convention (the subject's fate, not the
observation's): 0 the cycle completed (or found nothing to propose), 1 it
stopped by its own rules (validation refused, or the human did), 2 the
door refused (no repository, no gate commands, an unparseable command).

**One deliberate asymmetry:** the direct verb carries
`--insecure-fallback-signing`; the registry door does not, and that is the
honest direction for it. The registry's `pipeline run` is the shared
surface every pipeline runs through — it should not grow a flag whose only
effect is to *lower* an attestation guarantee. A commit that lands through
the registry door requires the real signing backend; on an install without
one the commit stage refuses by name (the attest kit's own rule for
generating under the HMAC dev fallback) rather than silently degrading the
evidence chain. The direct verb is the operator's front door, where the
human who types the flag is the same human who reads its warning.
The asymmetry is pinned by test (`tests/cli/test_improve_cli.py:254`).

## Failure behavior

- A red or timed-out gate command stops the cycle before the human is
  asked; the journal stays coherent (no broken trace) and nothing is
  applied.
- A refusing human is a recorded outcome, not an error: the proposal and
  the decision are both on the record, the tree untouched.
- A crash mid-cycle is recoverable by replay: a re-drive of the lineage
  serves every completed call from the record (the human is never asked
  twice, the commit never lands twice); an unclassifiable outcome refuses
  with `-32008` and the pipeline decides.
- A proposal whose target changed underneath it refuses at apply, naming
  the file.

## Known gaps

- The loop proposes for one high-friction domain per run (the first);
  there is no revision pass — `changes_requested` is recorded as a refusal
  with a note.
- The proposal surface is the playbook file only; code changes are future
  work and would need the same evidence chain.

## Related documents

- `docs/adr/README.md` — the ADR index (ADR-0017 honesty line, ADR-0046
  event goldens, ADR-0047 host-git integrity gate, ADR-0050 pipeline host
  and call journal)
- `docs/modules/coder.md` — the preliminary loop whose git discipline
  `gitcommit.py` adopts
- `docs/modules/ledger.md` — the call journal every stage writes through
- The done-when as tests: `tests/improve/test_cycle.py:87` (the happy
  path, including offline bundle verification)
