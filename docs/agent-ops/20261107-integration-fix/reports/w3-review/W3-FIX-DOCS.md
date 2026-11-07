<!-- ORCH-DIGEST:START -->
agent: W3-FIX-DOCS | task: W3-FIX-DOCS (R-W3-001 — 11 W3-shifted citations) | status: COMPLETE (residual 3, FIX-01-in-flight)
broken_at_start: 11 | broken_at_end: 3 residual (all recovery.py, FIX-01 in-flight, not landed at HEAD 6464eac)
lock: pytest tests/conventions/test_docs_references.py -q at start -> 1 failed (11 blank-line citations, exact REV-W3 §4 R-W3-001 list); at end -> 1 failed (3 blank, all recovery.py, FIX-01's write-set, uncommitted at re-run)
docs_commit: f3baf89 | report_commit: (this commit, next) — note: docs commit message lacks Task/Agent/Wave/Report trailers (plain -m; commit --amend is forbidden); this report commit carries them
last_comms_line_read: 2026-11-07T11:30:47Z ORCH -> ALL CORRECTION W3 (2 fix tasks opened: W3-FIX-01, W3-FIX-DOCS); re-read before commit: HEAD still 6464eac, FIX-01 not landed, recovery.py modified in worktree by FIX-01
<!-- ORCH-DIGEST:END -->

# W3-FIX-DOCS report

## Context
- Branch verified `ops/20261107-integration-fix` before committing [VERIFIED git branch --show-current].
- [RAN .venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q -> FAILED 1, 11 blank-line citations at start]. Same command at end -> FAILED 1, 3 blank (recovery.py only). Detail below.
- Root cause (per REV-W3 §4 R-W3-001): Wave 3 insertions (runtime +12, curator +20, world_model +254) shifted target lines; 11 citations kept their numbers and now land on blank lines. Baseline f31fb4f was green.
- Write-set: docs/** except docs/agent-ops/** and docs/plans/**. No src/** or tests/** edits made. `src/paa/ledger/recovery.py` (` M` at commit time) is FIX-01's concurrent in-flight edit, not touched by me.
- Sequencing: FIX-01 concurrently edits recovery.py, reflection.py, runner.py, test files — `git log --oneline -3` before final re-run showed HEAD still 6464eac (no FIX-01 commit landed), and recovery.py is ` M` in the worktree. Residual = exact list below for the orchestrator's final gate.
- No scratch files found (`w3-check.txt` etc. absent; `git status --short` shows only agent-ops report/ledger/COMMS + my 8 docs + FIX-01's recovery.py/W3-FIX-01.md).

## Citation mapping (old → new, one-line evidence each)
Evidence = current target line content [VERIFIED by grep/read at final line numbers]. Base lines read via `git show f31fb4f`. Same-content re-point by symbol; never blind +N.

### runtime.py → `async def build(` at 271 (base 260)

All seven claim the `Runtime.build()` composition root/entry. Base 260 = `async def build(` (verified by git show); current 271 = `async def build(` (the `@classmethod` at 270 is the decorator, so 271 is the def line per the brief's rule). W3's +11 insertion lands between current lines 326 and 359 (base-326 vs current-326 identical; base-359 `router=` vs current-370), plus the `embedder=embedder` kwarg after current 383 — so the +12 total is file-end only above 383.
- docs/README.md:53 — `src/paa/runtime.py:260` → `:271` [VERIFIED runtime.py:271 `async def build(`; sentence: "composition root is `Runtime.build()`"]
- docs/architecture/component-interactions.md:86 — `runtime.py:260` → `:271` [same; table entry point `Runtime.build()`]
- docs/architecture/component-interactions.md:232 — `runtime.py:260` → `:271` [sentence: "`Runtime.build` accepts `model_adapter`, `model_router` overrides..." → the build def line]
- docs/architecture/overview.md:85 — `src/paa/runtime.py:260` → `:271` [stands under the `Runtime.build()` construction tree, lines 74-83]
- docs/architecture/repository-map.md:126 — `src/paa/runtime.py:260` → `:271` ["Programmatic runtime | `Runtime.build()`"]
- docs/backend/overview.md:23 — `src/paa/runtime.py:260` → `:271` ["`Runtime.build()` | Programmatic composition root"]
- docs/operations/deployment-and-runtime.md:29 — `src/paa/runtime.py:260` → `:271` ["Programmatic: `Runtime.build()`"]

### runtime.py overview:96 — four-slot citation line (see note)
- `src/paa/runtime.py:359` → `:412` [VERIFIED runtime.py:412 `def _try_vector(settings...)`; sentence §2 names `_try_vector` first of the four degrade functions]
- `:372` → `:425` [VERIFIED runtime.py:425 `def _try_graph(settings...)`; sentence names `_try_graph` second]
- `:423` → `:435` [VERIFIED runtime.py:435 `def _try_queue(settings...)`; base 423 was already this def (+12)]
- `:497` → `:509` [VERIFIED runtime.py:509 `async def _acquire_sandbox(settings...)`; base 497 was already this def (+12)]

NOTE — slots 1-2 were pre-existing wrong-without-broken at base f31fb4f (base 359 = `router=agents.get("router"),`, base 372 = `model_router=model_router,` — router wiring, unrelated to §2's claim), exposed because the W3 shift landed slot 1 on a blank line. Per the W1 precedent ("where the sentence names a symbol, the target is that symbol's def line, even when the base line was unrelated"), the four slots map in order onto the four symbols the sentence names. Slots 3-4 confirm the line's purpose (base 423/497 were already `_try_queue`/`_acquire_sandbox` defs). The alternative (preserve base targets 370/383) would keep citing lines the sentence never mentions. Documented here so the mapping can be audited, not just trusted.

### Same-line lock-blind companion (DECISION)
- docs/architecture/component-interactions.md:86 — `runtime.py:151` → `:152` [VERIFIED runtime.py:152 `class Runtime:`; base 151 was the class line, current 151 is `@dataclass` decorator. Same line as the broken `:260`, so fixed with it, per the brief's "def line not decorator" rule — the table row claims the `Runtime` class. W1 precedent: cli decorator→def re-points.]

### curator.py
- docs/modules/memory.md:100 — `curator.py:62` → `:64` [VERIFIED curator.py:64 `_PHASE_ORDER: tuple[CurationPhase, ...] = (`; base 62 was the same line; sentence: "Phases in order ...: decay, prune-edges, consolidate, orphans, reconcile-vectors, analyze"]
- docs/modules/memory.md:99 — `curator.py:141` → `:154`, docs/modules/memory.md:102 — `curator.py:141` → `:154` [lock-blind companions in the same paragraph as broken :62; base 141 = `async def run_maintenance(`; current 141 is the comment `# Tier sweep runs inside the DECAY phase (ADR-0037...)` and the def is at 154 (VERIFIED curator.py:154 `async def run_maintenance(`; sentence claims daemon-cadence full consolidation + dry_run delete-phase semantics — run_maintenance's claim, not a comment's].]

### world_model.py
- docs/modules/memory.md:116 — `memory/world_model.py:185` → `:413` [VERIFIED world_model.py:413 `os.replace(tmp, path)` inside `_atomic_write` (def 402); base 185 was the same call line; sentence: "atomic writes (temp + `os.replace`)"]

## ADR-0037/0038/0039 citations lock-blind staleness scan
- ADR-0037 (`0037-tier-layer-and-replay.md:25`): `replay.py:379` VERIFIED still correct — current replay.py:379 = `_NO_PHASE_CHANGE = frozenset(` [VERIFIED via grep]. Unchanged.
- ADR-0038 (`0038-dual-vault-and-fence-precedence.md`), three citations stale after W3's world_model rewrite (+254 lines), all fixed:
  - `:17` — `world_model.py:42` → `:69` [VERIFIED :69 `class BeliefDocument(str, enum.Enum):`; sentence: "`BeliefDocument` in world_model.py:42 names `current_focus.md`, ..." — W1 accepted the class line as the citation with members following; same shape here. Current :42 is module docstring text.]
  - `:19` — `world_model.py:107` → `:218` [VERIFIED :218 `def update_belief_state(`; sentence: "`WorldModel.update_belief_state` in ...:107 has zero production callers". Current :107 is docstring text about `doc.title`.]
  - `:52` — `world_model.py:89` → `:186` [VERIFIED :186 `def path_for(self, doc...) -> Path:` + :187 `return self._vault / doc.value` (flat join); sentence: "`path_for` is currently a flat join". Current :89 is the `SKILLS = "skills.md"` enum member.]
- ADR-0039: no path:line citations (grep: no matches). Nothing to fix.
- W2 lesson applied: citations to files edited this wave (world_model.py) drift non-blank — caught all three via content verification, fixed all three.

## Verification
- [RAN .venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q at start -> FAILED, 11 blank (REV-W3 list, exact match). At end (after `git log --oneline -3` check: HEAD 6464eac, FIX-01 not landed) -> FAILED, 3 blank, all recovery.py (see Residual). NO other blank citations; every one of my 11 is green.]
- Self-review: re-read full git diff of my 8 files (13 slots changed). Findings: none critical/major. All changes are citation numbers on existing lines; no prose rewrites, no src/tests edits, no agent-ops/plans edits (COMMS.md + 01-work-ledger.md are ` M` in the worktree but are NOT mine — left uncommitted for ORCH; recovery.py ` M` is FIX-01's).
- Mutation mental frame: reverting any single re-point to its old number re-breaks the lock (old lines are blank), except the same-line companions (`runtime.py:151→152`, `overview:96 :372→425` — current :372 is `backpressure=backpressure,` and current :497 is a comment `# Plain missing-backends case...`, both wrong for the sentences; documented above) whose correctness rests on the symbol-mapping reading, not the lock. curator `:141→154` companions: reverting re-points at a Tier-Sweeper comment — wrong but lock-green; correctness rests on the run_maintenance reading.

## Residual / blockers
RESIDUAL (exact list, for the orchestrator's final gate — caused by FIX-01's concurrent in-flight edits to its write-set `src/paa/ledger/recovery.py`, which is ` M` uncommitted at my re-run; FIX-01 has NOT landed at HEAD 6464eac; pinning these now would commit guesses against a moving file):
1. docs/architecture/component-interactions.md:98 — `ledger/recovery.py:140` (claims the `RecoveryEngine` class location; base 140 = `class RecoveryEngine:`; current 140 is blank, class now at 150 in the worktree).
2. docs/flows/workflow-03-crash-recovery.md:74 — `recovery.py:115` (claims `RecoveryReport` fields; base 115 = `class RecoveryReport:`; current 115 is blank, class now at 125 in the worktree).
3. docs/modules/ledger.md:129 — `recovery.py:140` (claims `RecoveryEngine` uses LedgerStore/Database; same shift as 1).

Best-effort targets at my re-run (FIX-01 may move them before landing — re-derive at the final gate): `RecoveryReport` class at current worktree 125, `RecoveryEngine` class at current worktree 150. When FIX-01 lands, the fix is: 115→125, 140→150 (class lines, verified by grep at their current positions). I did NOT apply these: per the brief's sequencing rule, residual failures are an exact list for the final gate, and pinning docs to FIX-01's in-flight lines would be obsolete the moment FIX-01 commits its next edit. [RAN lock at end -> these 3 remain; everything else 0.]

## Out-of-scope findings (not fixed; for orchestrator)
- FINDING (W1-wave, verified stale, different section of a file I otherwise edited — brief's lock-blind mandate covers ADR-0037/0038/0039 only): docs/modules/memory.md:92 cites `creator.py:1490` for "the pending-embedding backlog drain". Current creator.py:1490 = `indexed = await self._sink.enqueue(facts)` (a body line); `drain_pending_embeddings` def is at 1497. W1-FIX-DOCS moved creator.py 1490→1497 and fixed the two ADR-0036 citations but missed this one. Fix = `creator.py:1490` → `:1497`. Left untouched per scope (not broken, not in the ADR-0037/0038/0039 scan, not on a line I edited). Recommend a one-line docs follow-up.
- Verified NOT stale (checked since they sit near my edits): memory.md:95 `creator.py:185` is correct — current 185 is the `ExtractionDropCounts` docstring stating exactly the claim ("Deterministic and replay-stable: every field is a pure function of the signal payload"). Left unchanged. Runtime citations at 109/132/288/326 are unaffected (base and current lines identical — W3's insertion lands between lines 326 and 359), so comp-interactions:97, ledger.md:128 companions are green for cause, not luck.
- NOT audited (scope): non-broken citations elsewhere in the tree to shifted files (the W1 FINDING class — a full semantic sweep is a separate task). I audited only the 11 broken + same-line/same-paragraph companions + ADR-0037/0038/0039.

## Long-run test answers
- Source: every new citation verified against its target line's actual content (grep/read evidence above), not arithmetic. Per-file shift sizes differ (runtime +11 above 359, +12 by EOL; curator +2 at _PHASE_ORDER, +13 at run_maintenance; world_model +184 at os.replace) — REV's single "+N" per file is an average, which is exactly why blind +N is banned.
- Load / Hostile / Operability: docs-only change; no runtime behavior, no new failure modes. Reversal: reverting a re-point re-breaks the lock (except the companions, reasoned above).
- What was NOT verified: full test suite (standing order 16, scoped check only); FIX-01's final recovery.py line numbers (in-flight — residual list covers it); non-broken citations' semantics beyond the 11 + companions + ADR-0037/0038/0039 scan (see Out-of-scope FINDINGs).
