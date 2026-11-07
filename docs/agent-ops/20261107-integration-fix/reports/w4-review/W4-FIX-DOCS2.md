<!-- ORCH-DIGEST:START -->
agent: W4-FIX-DOCS2 | task: W4-FIX-DOCS2 (residual orchestrator refs after W4-FIX-01) | status: DONE
broken_at_start: 55 blank (all orchestrator.py) | broken_at_end: 0 | lock 5 passed
62 citations re-pointed (55 lock-broken + 7 same-sentence companions), 16 docs files, numbers-only diff
docs_commit: 9e8e688 | report_commit: (this commit)
last_comms_line_read: 2026-11-07T19:10:00Z W4-FIX-01 -> ORCH | DONE | W4-FIX-01 (re-read at start; appended START+CLAIM)
<!-- ORCH-DIGEST:END -->

# W4-FIX-DOCS2 report

## Context
- Branch verified `ops/20261107-integration-fix` before each commit [VERIFIED git branch --show-current].
- HEAD at start = e3e7b68 (W4-FIX-01 landed: 8971e83 + report). W4-FIX-01's orchestrator diff is a pure +1: one
  `TYPE_CHECKING` import (`from paa.context.allocator import AllocationResult`) at orchestrator.py:46, plus a
  same-line signature change at old :1114 [VERIFIED git diff dc06ed5 8971e83 -- src/paa/agents/orchestrator.py].
  Every orchestrator.py citation at line >= 47 therefore points one line early.
- [RAN `.venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q` at start -> 1 FAILED:
  `test_every_referenced_line_is_not_blank` lists 55 citations, all `src/paa/agents/orchestrator.py` blank lines
  (the brief's "~56" is 55; my independent extraction via the lock's own module reproduced the list exactly —
  55 = 55, no diff). The other 4 lock tests (within_file, names_exactly_one, lock_not_vacuous, catches_broken)
  pass at start.]
- W4-FIX-01 also shifted allocator.py (+17), runner.py (+9 net at top), migrations.py (+12), sqlite_backend.py
  (+17), dashboard.py (+8 after `_reflection_doctor_value`) [W4-FIX-01 §4]. I enumerated docs citations to all
  five: **zero references exist** to any of them in the lock's scope [VERIFIED via lock-module enumeration].
  There is no sibling residual — every broken citation is orchestrator.py.
- Write-set respected: 16 files under docs/ only; no src/**, tests/**, docs/agent-ops/** or docs/plans/**
  edits. `docs/modules/agents.md` and `docs/modules/AGENTS.md` are the same bytes on this host (case-insensitive
  filesystem) and only `docs/modules/agents.md` is git-tracked [VERIFIED `git ls-files docs/modules/` +
  `fc.exe`], so one file covers the content.

## How each holder was chosen (the rule, applied uniformly)
FIX-01's +1 is mechanical, but the old cites were not (many were stale-at-base, pointing at a different symbol
than the citing sentence names). Per the brief ("verify numbers at HEAD, never blind-shift") and the W4-FIX-DOCS
precedent, each re-point was decided by reading the citing sentence first:
1. **Sentence names a symbol** (`submit()`, `run()`, `_classify()`, `_emit()`, `_hydrate`, `_plan`,
   `_execute_steps()`, `boot()`, `clear_human_gate`, `mediate_delegation`, class `ChiefOrchestrator`,
   `register`) -> that symbol's **def line at HEAD**.
2. **Sentence describes a specific statement/behavior** (terminal-return, `max_plan_nodes` validation, gate
   emit, per-step re-project, density reason, SIMPLE bypass, post-execution dispatch) -> the line holding
   that statement at HEAD.
3. **Ranges/regions** (`:467`–`:811`, `:292-324`, `:542-811`) -> endpoints re-pointed to the region the
   sentence covers (first phase def .. last phase def; run()'s body for "run() invokes...").
Rule 1 vs 2 never conflicted: where the sentence names a symbol at the start and a behavior in the
parenthetical, the parenthetical's behavior is what the number anchors (see :400 for "if terminal, returns
immediately" and :397 for the adjacent "run(correlation_id) calls project" which anchors the function itself).
All new target lines were verified non-blank and holding the claimed content by direct read/grep.

## Citation mapping (old -> new, one-line evidence each)
Evidence = orchestrator.py line content at HEAD, verified by read/grep.

### Lock-visible (55)
- docs/architecture/component-interactions.md:87 `agents/orchestrator.py:188` -> `:189` [L189 `class ChiefOrchestrator:`; sentence names the class]
- docs/architecture/component-interactions.md:87 `orchestrator.py:396` -> `:397` [L397 `async def run(`; sentence names `run()`]
- docs/architecture/component-interactions.md:98 `orchestrator.py:1104` -> `:1105` [L1105 `async def boot(`]
- docs/architecture/component-interactions.md:215 `orchestrator.py:467`–`:811` -> `:468`–`:812` [L468 `async def _hydrate(` .. L812 `async def _commit(`; "Every orchestrator phase" region]
- docs/architecture/component-interactions.md:220 `orchestrator.py:833` -> `:834` [L834 `async def mediate_delegation(`]
- docs/architecture/overview.md:149 `src/paa/agents/orchestrator.py:292` -> `:397` [L397 `async def run(`; heading "Orchestrator phases" whose list item 1 is `run` — old :292 was submit's def in the current layout, not the phases]
- docs/architecture/overview.md:161 `src/paa/agents/orchestrator.py:467`–`:811` -> `:468`–`:812`
- docs/architecture/overview.md:172 `src/paa/agents/orchestrator.py:833` -> `:834`
- docs/backend/modules-and-services.md:119 `orchestrator.py:188` -> `:293` [sentence names `submit()`; old :188 was the class]
- docs/backend/modules-and-services.md:121 `orchestrator.py:292` -> `:397` [sentence names `run()`; old :292 was `submit`]
- docs/backend/overview.md:61 `agents/orchestrator.py:188` -> `:189` [names `ChiefOrchestrator`]
- docs/backend/request-lifecycle.md:18 `orchestrator.py:292` -> `:397` [names `run()`]
- docs/backend/request-lifecycle.md:23 `orchestrator.py:396` -> `:397` [names `run(correlation_id)`]
- docs/backend/request-lifecycle.md:24 `orchestrator.py:295` -> `:400` [L400 `if state.is_terminal:`; claim is the terminal-return behavior]
- docs/backend/request-lifecycle.md:29 `orchestrator.py:467` -> `:468` [names `_hydrate()`]
- docs/backend/request-lifecycle.md:42 `orchestrator.py:542` -> `:543` [names `_plan()`]
- docs/backend/request-lifecycle.md:53 `orchestrator.py:441` -> `:581` [L581 `if len(steps) > profile.max_plan_nodes():`; claim is the validation + RecursionGuardError]
- docs/backend/request-lifecycle.md:66 `orchestrator.py:494` -> `:910` [L910 `EventType.AWAITING_HUMAN_ATTESTATION,` in `_open_gate`; the only emission site of that event]
- docs/backend/request-lifecycle.md:73 `orchestrator.py:515` -> `:718` [L718 `async def _execute_steps(`; sentence names it]
- docs/backend/request-lifecycle.md:96 `orchestrator.py:577` -> `:780` [L780 `state = await project(...)` inside the `_execute_steps` loop; claim is "re-projects after each step"]
- docs/data/data-lifecycle.md:11 `orchestrator.py:188` -> `:293` [names `submit()`]
- docs/flows/workflow-01-submit-and-run.md:18 `orchestrator.py:188` -> `:293` [names `submit()`]
- docs/flows/workflow-01-submit-and-run.md:20 `orchestrator.py:247` -> `:352` [L352 `def _classify(`; sentence names `_classify()`]
- docs/flows/workflow-01-submit-and-run.md:23 `orchestrator.py:263` -> `:264` [L264 `async def _emit(`; sentence names `_emit()`]
- docs/flows/workflow-01-submit-and-run.md:36 `orchestrator.py:292` -> `:397` [names `run(cid)`]
- docs/flows/workflow-01-submit-and-run.md:44 `orchestrator.py:467` -> `:468`
- docs/flows/workflow-01-submit-and-run.md:60 `orchestrator.py:542` -> `:543`
- docs/flows/workflow-01-submit-and-run.md:90 `orchestrator.py:515` -> `:718`
- docs/flows/workflow-01-submit-and-run.md:112 `orchestrator.py:577` -> `:780`
- docs/flows/workflow-02-human-gate.md:12 `orchestrator.py:467` -> `:468` ["Context hydration" = `_hydrate`]
- docs/flows/workflow-02-human-gate.md:22 `orchestrator.py:515` -> `:422` [L422 `if state.phase is TaskPhase.AWAITING_HUMAN:` directly after the `_execute_steps` call at :421; sentence: "Post-execution ... after `_execute_steps`, if AWAITING_HUMAN"]
- docs/flows/workflow-02-human-gate.md:40 `orchestrator.py:1015` -> `:1016` [L1016 `async def clear_human_gate(`]
- docs/interfaces/api-reference.md:61 `orchestrator.py:188` -> `:293` [step describes `submit()`'s writes/enqueue/return]
- docs/interfaces/api-reference.md:101 `orchestrator.py:1015` -> `:1016` ["can clear its own gate" = `clear_human_gate`]
- docs/interfaces/integrations-and-communication.md:103 `orchestrator.py:467`–`:811` -> `:468`–`:812`
- docs/modules/agents.md:45 `orchestrator.py:295` -> `:400` [same terminal-return sentence as request-lifecycle:24]
- docs/modules/agents.md:46 `:467` -> `:468`
- docs/modules/agents.md:48 `:542` -> `:543`
- docs/modules/agents.md:49 `:441` -> `:581`
- docs/modules/agents.md:53 `:515` -> `:718`
- docs/modules/agents.md:109 `:471` -> `:667` [row: "calls `agent.run(message, ctx)` | Drive lifecycle phases" — holder is the policy phase's call site, see companions below]
- docs/modules/agents.md:110 `orchestrator.py:833` -> `:834`
- docs/modules/agents.md:141 `orchestrator.py:247` -> `:248` [L248 `def register(`; claim is "orchestrator registration" — same old number as :150 below, different sentence, different holder]
- docs/modules/agents.md:150 `orchestrator.py:247` -> `:352` [sentence names `_classify()`; old :247 was `register`]
- docs/modules/observability.md:28 `orchestrator.py:50` -> `:51` [L51 `log = structlog.get_logger(__name__)`; identical to the cited siblings]
- docs/plans and audits/flexability-audit.md:22 `292-324` -> `397-440` [L397 `async def run(` .. L440 `return TaskOutcome(...)`; sentence: "`ChiefOrchestrator.run()` directly owns hydrate → plan → policy → execute/critique/retry → commit" — old range covered submit, i.e. stale-at-base]
- docs/plans and audits/flexability-audit.md:24 `542-811` -> `543-812` [_plan def .. _commit def, "planner/worker/policy/critic chain"]
- docs/plans and audits/flexability-audit.md:54 `292-324` -> `397-440` [sentence: "`run()` directly invokes `_hydrate`, `_plan`, `_check_policy`, `_execute_steps`, `_critique`, and `_commit`"]
- docs/reviews/documentation-audit.md:59 `orchestrator.py:188` -> `:189` [chain node `ChiefOrchestrator`]
- docs/reviews/documentation-audit.md:63 `orchestrator.py:1104` -> `:1105` [crash-recovery chain node = `boot`]
- docs/runtime/background-jobs-and-events.md:45 `orchestrator.py:737` -> `:1105` [names `ChiefOrchestrator.boot()`; old :737 was a `_message` line inside `_execute_steps`]
- docs/security/security-model-and-trust-boundaries.md:153 `orchestrator.py:1015` -> `:1016` [names `clear_human_gate`]

### Same-sentence companions (7)
- docs/modules/agents.md:47 `:379` -> `:527` [L527 `"reason": "insufficient context density",`; "...parks on AWAITING_HUMAN_ATTESTATION if density insufficient" — same bullet as :46's `_hydrate` cite, wrapped over lines 46-47]
- docs/modules/agents.md:48 `:408` -> `:548` [L548 `if ctx.modality is ComplexityModality.SIMPLE:`; "SIMPLE bypasses model"]
- docs/modules/agents.md:109 `orchestrator.py:374` -> `:512` [L512 `self._context_builder.run(message, ctx)`]
- docs/modules/agents.md:109 `:434` -> `:574` [L574 `self._planner.run(message, ctx)`]
- docs/modules/agents.md:109 `:538` -> `:741` [L741 `self._worker.run(message, ctx.child())`]
- docs/modules/agents.md:109 `:594` -> `:797` [L797 `self._critic.run(message, ctx.child())`]
- docs/flows/workflow-02-human-gate.md:15 `:379` -> `:527` [same density reason string; "emit AWAITING_HUMAN_ATTESTATION with {reason: "insufficient context density",...} and return" — same list item as :12's cite]
- docs/flows/workflow-02-human-gate.md:23 `:507` -> `:423` [L423 `return TaskOutcome(...)` immediately after the :422 check; same sentence as :22's Post-execution item, wrapped over lines 22-23]

Note on agents.md:109: the five old numbers (374/434/471/538/594) mapped in order to the five phase agent
call sites (512/574/667/741/797 = context_builder, planner, policy, worker, critic — the only
`agent-ish.run(message, ctx...)` calls in `_hydrate`, `_plan`, `_check_policy`, `_execute_steps`, `_critique`
[VERIFIED grep of `\.run\(`: exactly 6 matches; the 6th, :885 `agent.run(message, child_ctx)` inside
`mediate_delegation`, is the delegation path, not a lifecycle phase]. Order-pairing keeps the row's
anchor order and makes each citation land on a line that literally calls an agent's `run`.
The mechanical +1 alternative (375/435/472/539/595: `try:` / `project` / `gate_history` / role kwarg / `},`)
would have left all five false for the row's claim.

## Verification
- [RAN `.venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q` at start ->
  1 failed (not_blank, 55) + 4 passed.]
- [RAN the same command after edits -> `5 passed`.]
- Post-fix enumeration of all 127 orchestrator.py refs in scope: every re-pointed line's current content
  verified to hold the sentence's claim (table in evidence script output kept with this session; key
  instances: :400 `if state.is_terminal:`, :581 `if len(steps) > profile.max_plan_nodes():`,
  :910 `EventType.AWAITING_HUMAN_ATTESTATION,`, :780 `state = await project(...)`,
  :527 `"reason": "insufficient context density",`, :422-423 post-execution check/return,
  :512/:574/:667/:741/:797 agent `.run(...)` call sites).
- Self-review of the full diff (stranger's-PAre audit): 16 docs files, 54 lines changed, every hunk
  number-only (no prose touched), no files outside the write-set; COMMS.md not staged (sibling's lines +
  my 2 lines stay uncommitted for ORCH to sweep). Scratch scripts under the session temp dir (outside
  the repo); nothing to delete in-tree. Mutation frame: reverting any single re-point re-breaks the
  lock (every old number is blank at HEAD) or restores same-content-wrong text (e.g. :523, :649-style
  stale anchors stay lock-green only where they were non-blank — those were deliberately not touched,
  see Findings).

## Residual / blockers
- None for the RUN list: 55/55 lock-broken citations re-pointed, lock green. No UNRESOLVABLE (every
  target found). No NEED (no src change required).
- Independent re-review: per brief §5, REV-W4b/TREV-W4b run after me — this report is submitted for
  that re-review, not self-approved.

## Out-of-scope findings
- FINDING 1 (lock-blind stale, NOT fixed per §6 "exactly the RUN-list"): 65 standalone non-blank
  orchestrator citations remain. Two sub-classes:
  (a) Pure +1 shifts (correct pre-FIX-01, now one line early; e.g. workflow-02:20 `:909` -> emit at
    :910, workflow-01:56 `:534` -> :535 `EventType.CONTEXT_HYDRATED`, agents.md:56 `:609` -> `_commit`
    :812, ledger.md:127 `:276` -> :277, workflow-01:84 `:673` -> :674, request-lifecycle:36 `:499`
    -> :535). For most, mechanical +1 restores the holder.
  (b) Stale-at-base (wrong holder even before FIX-01; mechanical +1 does NOT fix them, need the same
    sentence-reading treatment as here): request-lifecycle:25 `:327` (claims `_context_for`, at :442),
    request-lifecycle:31 `:357` (claims CONTEXT_HYDRATION_REQUESTED emit, :495), request-lifecycle:35
    `:379` (claims gate emit+return, :910/:917), request-lifecycle:54 `:448` (claims PLAN_COMPILED,
    :549), request-lifecycle:58 `:461` / workflow-01:76 `:461` / agents.md:50 `:461` (claim
    `_check_policy`, :601), request-lifecycle:68 `:505` / agents.md:52 `:505` (claim POLICY_CLEARED, ~:712),
    request-lifecycle:100 `:609` / data-lifecycle:195 `:609` / workflow-01:116 `:609` (claim `_commit`, :812),
    modules-and-services:123 `:523` + background-jobs:100 `:523` (claim `_execute_steps` retries, :718),
    modules-and-services:125 `:631` / documentation-audit:66 `:631` / agents.md:58 `:631` (claim delegation
    bounds, :834), modules-and-services:128 `:310` / request-lifecycle:117 `:310` / background-jobs:122
    `:310` (claim re-project, :435/:780), workflow-01:26 `:264` (claims `_apply_backpressure`, :369),
    agents.md:55 `:108` (claims max_step_retries default — a module-docstring comment, unshifted < 46),
    agents.md:58 `:631`, agents.md:59 (see FINDING 2), persistence:178 `:740`,
    workflow-02:41-46/:113/:115 table+list cites (:699/:703/:712), documentation-audit:60 `:301`-`:309`.
  Five refs to `orchestrator.py:9` (module docstring, line < 46, unshifted) and `integrations:124 :108`
  are still correct and need no sweep. A follow-up semantic-sweep task owns these; DOCS's
  daemon/facts/dialect lock-blind staleness (W4-FIX-DOCS §Out-of-scope) is the same class and should be
  swept together.
- FINDING 2 (lock extraction blind spot, wrong-without-broken AND invisible): `docs/modules/agents.md:59`
  cites ``:688` and ``:737` are NOT extracted by the lock because the wrapping `clear_human_\ngate()` on
  lines 58-59 shifts the backtick pairing (verified by probing `find_references`: line 59 yields zero
  refs, while line 58's `:631` is extracted). The :737 target (blank `message = self._message` region,
  actually line 737 is now blank inside `_execute_steps`) is exactly the failure the lock exists to catch
  but cannot see. Semantic staleness: :631 (mediate_delegation, -> :834), :688 (clear_human_gate, -> :1016),
  :737 (boot, -> :1105). Note `docs/modules/AGENTS.md` on this host is the same file, but on a
  case-sensitive checkout the untracked-but-possibly-copied name could carry the blind spot too; worth a
  look when the sweep runs. Fixing the wrap would be a prose edit (joining the word) — outside this task.
- FINDING 3 (noted, not touched): `workflow-01:38`'s ``:295` and `:327` resolve via carried_path to
  `replay.py`, not orchestrator.py (verified with the lock's resolver) — a reader could mistake them for
  orchestrator cites. No action required.

## Long-run test answers
- Source: every new number verified against its target line's actual content and the citing sentence
  (mapping + evidence above); nothing was shifted blindly.
- Load/Hostile/Operability: docs-only; no runtime behavior, no new failure modes. Rollback = revert 9e8e688.
- Reversal: reverting any re-point re-breaks the lock (all 55 old numbers are blank at HEAD) or restores
  same-content-wrong text for the 7 companions (each companion's old line now holds different content —
  e.g. agents.md:48's :408 is now the `_apply_backpressure`-adjacent escalation comment, not the SIMPLE
  bypass).

## Git state
- Branch `ops/20261107-integration-fix` [VERIFIED]. No protected-branch commit; explicit paths only
  (no add -A/a/stash/reset/checkout/push/--no-verify).
- Docs commit: 9e8e688 (16 files, 54 insertions / 54 deletions). Report commit: this commit (docs-only,
  lists 9e8e688).
- COMMS.md uncommitted (sibling lines + my 2 lines) for ORCH to sweep at the wave boundary.
