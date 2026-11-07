<!-- ORCH-DIGEST:START -->
agent: REV-W3b | scope: W3 fixes 6464eac..HEAD (037a8d6, f3baf89, efe4207) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 2
claims_audit: 25 claims checked / 1 false (test-count arithmetic only)
band_aids_found: none
top_3_risks: sweeper CID still inflates /health open_tasks + TUI rows (requeue stopped, count not); 4 pre-existing runner logs remain unredacted, no test pins redaction; docs lock green but ~15 recovery/curator citations are now semantically off-by-shift (lock-blind)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: done
confidence: high for code fixes (re-ran suites), med for docs semantics (spot-check, not full-tree sweep)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W3b (fresh independent reviewer, read-only)

Re-review of wave-3 fixes for the prior REJECT (BLOCKER R-W3-001 docs lock,
MAJOR R-W3-002 phantom lineage, MAJOR R-W3-003 run_weekly crash, MINOR R-W3-005
unredacted runner logs). I did not write the wave-3 code or the fixes. I edited
only this report; I did not commit.

## 0. Method

Diff range reviewed: `6464eac..HEAD` — code commit `037a8d6` (W3-FIX-01),
docs `f3baf89` (W3-FIX-DOCS), docs `efe4207` (W3-FIX-DOCS2), plus 3 report
commits. [VERIFIED `git log --oneline 6464eac..HEAD` -> 6 commits; `git diff
--stat` -> 21 files, src changes only in `recovery.py`, `reflection.py`,
`runner.py` plus 5 test files.]

Order followed: blind pass on the full src+test+docs diff (recovery exemption,
run_weekly guard, runner redact, docs re-points, memory.md:91-92) BEFORE
reading the three fix reports; fix reports then audited in §2. Prior findings
read for scope: `reports/w3-review/REV-W3.md` §§4-5, `TREV-W3.md` §6 P1/P2.

Commands run (Windows venv `.venv\Scripts\python.exe`, scoped only):
- [RAN `pytest tests/ledger/test_recovery_tier_phantom_p009.py tests/tui/test_runner_panels.py tests/memory/test_dual_vault_p010.py tests/memory/test_tier_sweeper_p009.py tests/conventions/test_docs_references.py` -> 75 passed]
- [RAN `pytest tests/coder/test_runtime_tools.py::TestRecallVectorFallback::test_vector_limit_one_truncates_when_two_facts_exist` -> exit 0 (1 passed)]
- [RAN `pytest tests/conventions/test_import_conventions.py` -> 21 passed]
- [RAN `ruff check` on 3 changed src + 5 changed test files -> All checks passed]
- [RAN `mypy` on 3 changed src files -> Success: no issues found in 3 source files]
- [RAN fresh-interpreter `import paa.ledger.recovery; import paa.memory.tier; import paa.tui.runner; import paa.improvement.reflection` -> ok]
- [RAN live probe: `ReflectionEngine.apply_to_playbook("<!-- paa:managed:BEGIN reflections -->")` -> plain `ValueError`, NOT `VaultConflictError` — see R-W3b-003]

Full `mypy src` / full `pytest tests/` NOT run (scoped-check rule); implementer
claims about totals are marked unverified in §2 where I did not re-run them.
Last COMMS line read: line 99 (`W3-FIX-DOCS2 DONE`) at start; re-read before
writing — no new lines affecting this diff.

## 1. Blind-pass findings (before reading implementer reports)

### 1.1 Recovery CID exemption (R-W3-002) — correct, exact-match, cycle-safe

- Placement [VERIFIED `src/paa/ledger/recovery.py:288-297`]: after the
  `is_terminal` repair branch, before `PARKED_HUMAN`/retry/reconcile. A
  TIER-only lineage projects `CREATED`/attempts 0, so it can never reach the
  later branches; the early return skips `_reconcile_workspace`, `_requeue`,
  `_record_sweep`, `_abandon` and head-terminal marking. No audit event is
  appended, so repeat sweeps stay no-op (idempotency — pinned by test).
- Exact match [VERIFIED `recovery.py:502-527`]: `cid != TIER_SWEEPER_CORRELATION_ID`
  is UUID equality against one deterministic `uuid5` [VERIFIED
  `src/paa/memory/tier.py:62-65`]. No prefix/pattern, so no real task can be
  exempted by a near-miss id. The TIER-only conjunction (`bool(events) and
  all(event_type in (TIER_DEMOTED, TIER_ARCHIVED))`) defeats id-reuse: the
  sweeper CID with any task event is treated as a real task. Empty history
  returns False. No exhaustive `match` over `RecoveryOutcome` exists elsewhere
  [VERIFIED grep `RecoveryOutcome\.` -> only recovery.py + tests], so the new
  `SKIPPED_SYSTEM_LINEAGE` member is purely additive; `RecoveryReport.summary()`
  iterates the enum and picks it up automatically.
- Cycle-safe: the CID import is function-local [VERIFIED `recovery.py:518`].
  In fact `memory/tier.py` imports `ledger.store`, `ledger.events`,
  `memory.domains`, `storage.relational.base` — none import `ledger.recovery`,
  so a top-level import would also be safe today; the deferred import is
  harmless belt-and-braces following the `_requeue`/`StreamName` precedent and
  the import-conventions lock stays green [RAN 21 passed]. If
  `store.read_correlation` raises inside the helper, `boot_sweep`'s per-head
  try/except already converts it to `UNRECOVERABLE` — same posture as a
  `project()` failure.
- Residual: the lineage stays in `open_correlations`, so `/health`
  `open_tasks` and the TUI task table still count it (R-W3b-004). The phantom
  *execution* (requeue every boot) is stopped; the phantom *count* is not.
  That half needs `replay.py`/`store.py`, outside the fix write-set.

### 1.2 run_weekly guard (R-W3-003) — correct, fence intact, report preserved

- [VERIFIED `src/paa/improvement/reflection.py:179-200`]: each domain is wrapped
  in `try/except VaultConflictError` around `apply_to_playbook` only. On
  conflict it logs `reflection.playbook_conflict_skipped` (domain, `exc.path`,
  `redact(str(exc))`) and continues; `rules_written` is appended only on
  success (or in the pre-existing `vault is None` path, unchanged). The
  accumulated `ReflectionReport` is returned, not discarded.
- Fence intact: `apply_to_playbook` still raises [VERIFIED
  `reflection.py:224-250`]; `VaultConflictError` is raised by
  `_check_conflict`/`_check_marker_hygiene` before `_atomic_write`
  [VERIFIED `src/paa/memory/world_model.py:298-336`, write at `:273-274`], so a
  skipped rule leaves the file (and the write record) untouched. Catching only
  `VaultConflictError` — not `Exception` — is the correct narrow fix; an
  `OSError` from the atomic write still propagates as before.
- Gap (R-W3b-003): the fence family also raises a *plain* `ValueError` for
  marker-in-content [VERIFIED `world_model.py:246-253`], and `domain` in the
  synthesized rule is a free string from the task payload [VERIFIED
  `reflection.py:260-268`]. A crafted domain containing a fence marker would
  crash `run_weekly` the same way R-W3-003 crashed it. Probed live (see §0).

### 1.3 Runner redact alignment (R-W3-005) — 7/7 in scope, 4 siblings remain

- [VERIFIED `src/paa/tui/runner.py`]: 7 sites now `error=redact(str(exc))` —
  `_decision_tracker:176`, `_autonomy_state:202`, `_agent_monitor:231`,
  `build_panel_fields` ×4 (`:268,274,280,286`) — plus the module import of
  `redact` from `paa.models.base` [VERIFIED `models/base.py:256-275`: pattern
  scrub + literal secrets; `models/base` imports only `core.*` + pydantic/
  structlog, no back-edge to `tui`; fresh import + conventions lock green].
- 4 unredacted `error=str(exc)` sites remain in the same file: `:72`
  (`tui.project_failed` in `_gather_rows`), `:93` (`tui.rows_failed`), `:395`
  (`tui.status_failed`), `:403` (`tui.ledger_failed`). All pre-existing, all
  disclosed. No test pins redaction anywhere in `tests/tui` [VERIFIED grep
  `redact|project_failed|panel_failed` -> no matches], so reverting the redact
  hunk passes every test (see §3 Reversal).

### 1.4 Docs re-points — changed citations semantically correct; shift-blind residuals remain

Spot-read of the citations the fix commits actually changed (≥8, semantic —
target line read, not just non-blank):

| # | Citation (HEAD) | Target line | Verdict |
|---|---|---|---|
| 1 | `runtime.py:271` (×7 files) | `async def build(` | ✓ |
| 2 | `runtime.py:152` | `class Runtime:` | ✓ |
| 3 | `overview.md:96` → `:412/:425/:435/:509` | `_try_vector`/`_try_graph`/`_try_queue`/`_acquire_sandbox` defs | ✓ |
| 4 | `world_model.py:69` / `:218` / `:186` | `class BeliefDocument` / `def update_belief_state` / `def path_for` | ✓ |
| 5 | `world_model.py:413` | `os.replace(tmp, path)` inside `_atomic_write` (def :402) | ✓ |
| 6 | `recovery.py:150` (×2) / `:125` | `class RecoveryEngine:` / `class RecoveryReport:` | ✓ |
| 7 | `creator.py:1497` + `daemon.py:165` (memory.md:91-92) | `async def drain_pending_embeddings` / `await creator.drain_pending_embeddings()` | ✓ both halves of the sentence |
| 8 | `curator.py:154` (×2) / `:64` | `async def run_maintenance` / `_PHASE_ORDER` | ✓ |
| 9 | `comp-interactions.md:98` `boot_sweep()` (`recovery.py:226`) | line 226 is now `return None` (def at :236) | ✗ — was correct at `6464eac` (def at 226), shifted +10 by FIX-01, lock-green |
| 10 | `memory.md:147` `curator.py:141` | line 141 is the comment `# Tier sweep runs inside the DECAY phase...` (`run_maintenance` now at :154) | ✗ — was correct at `f31fb4f` (def at 141), shifted by W3, lock-green |
| 11 | `workflow-03.md:25,57,146-150`, `ledger.md:97,106,148,150`, `background-jobs-and-events.md:44-126` recovery cites (`:231/:250/:444/:366/:371`...) | each now lands ~10 lines before its base target (e.g. `:231` is now `"manifest_hash":...`, `:250` a dict-comp line) | ✗ — lock-green, semantically off; several were already loose at base (`:222`,`:263`,`:306`) but the +10 shift made exactly-correct ones wrong |

The docs lock itself is green now [RAN `test_docs_references.py` as part of
the 75-pass run -> 5 passed]. The residuals above are the known lock-blind
class (AGENTS.md: "the lock cannot see a reference that is wrong without being
broken"); they are docs-only and do not break the gate, but they are real
misdirections introduced by the W3/FIX-01 shifts in files the fix commits
touched.

### 1.5 New tests (blind read)

- `test_recovery_tier_phantom_p009.py` (5): TIER-only skip + ledger-count
  frozen + idempotent second sweep + mixed TIER_DEMOTED/ARCHIVED + real-task
  control in the same sweep (RESUMED, requeue payload names the real CID) +
  non-TIER event on the sweeper CID not exempted. The control and the reuse
  test are exactly the guards a prefix/pattern exemption would fail.
- `test_runner_panels.py` (2): real `LedgerStore` + real events on two open
  lineages; asserts all three ledger panels non-empty and correctly shaped,
  plus the POLICY-less empty case (empty is data-driven, not a stub). Kills
  the TREV-W3 P1 mutation survivor by construction.
- `TestRunWeeklyConflictHandling` (2): real in-block edit (both domains skip,
  2 warning logs, human edit stands, `rules_written == []`) + failure-injected
  first domain (second lands, counted). The second test is the honest way to
  prove per-domain continuation against a single `playbooks.md`.
- P2 pins: exploding tier sweeper (curator phase isolation), per-call
  `dry_run=True` on a live sweeper (zero writes + live control), invalid tier
  (schema CHECK refusal + `_due_move('TX') == ('T2','T3',TIER_DEMOTED)` when
  due / `None` when fresh), two-fact `limit=1` truncation. All behavior
  assertions, no mocks of the unit under test (the exploding/flaky doubles
  target collaborators, legitimately).

## 2. Claims audit

| # | Implementer claim | Verified? | Evidence / finding |
|---|---|---|---|
| 1 | FIX-01: exact-CID + TIER-only exemption; no requeue/audit/resume | Yes | [VERIFIED recovery.py:288-297,502-527; RAN phantom suite 5 passed] |
| 2 | FIX-01: real task still RESUMED+requeued; id-reuse not exempted | Yes | Tests pin both; [RAN 5 passed] |
| 3 | FIX-01: `run_weekly` survives conflict, skips per-domain, report kept, fence unchanged | Yes | [VERIFIED reflection.py:179-200,224-250; RAN dual-vault run_weekly 2 passed] |
| 4 | FIX-01: 7 new runner logs redacted; 4 pre-existing remain (FINDING-2) | Yes | [VERIFIED runner.py grep: 7 redact sites, 4 surviving str(exc)] |
| 5 | FIX-01: cycle-safe imports (fresh import + conventions) | Yes | [RAN fresh imports ok; conventions 21 passed] |
| 6 | FIX-01: panel happy-path tests fail on empty-forever builders | Yes | Assertions require len 2 / 1 gate / 2 roles; [RAN 2 passed] |
| 7 | FIX-01: two-fact limit=1 discriminates the clamp | Yes | [RAN single test -> exit 0] |
| 8 | FIX-01: curator/dry-run/invalid-tier P2 pins | Yes | [RAN tier P2 classes within 75-pass run] |
| 9 | FIX-01: code commit = 8 files, +672/−10 | Yes | [VERIFIED `git show --stat 037a8d6` -> exact] |
| 10 | FIX-01: "13 new cases" (§3/§4 heading) | **No — false** | Actual: 5 phantom + 2 panels + 2 dual-vault + 5 tier-P2 + 1 coder = **15**. Reporting arithmetic only; no code/test impact (R-W3b-005). |
| 11 | FIX-01: ruff 0 (full `src tests`), mypy total 3 pre-existing | Unverified (scoped) | [RAN scoped ruff 0 on changed files; scoped mypy 0 on 3 src files]. Full-tree totals not re-run by me; no reason to doubt, recorded as not independently verified. |
| 12 | FIX-01: `tests/tui/` 1 failure = pre-existing strftime, untouched | Consistent | `dashboard.py` untouched by the diff [VERIFIED `git diff --stat`]; failure not re-run by me (out of scoped set). |
| 13 | FIX-01: FINDING-1 open_tasks inflation residual | Yes — real | Sweeper CID still in `open_correlations` [VERIFIED phantom test asserts it]; see R-W3b-004. |
| 14 | FIX-01: reversal of redact passes all tests (honestly disclosed) | Yes | No test pins runner log content [VERIFIED grep tests/tui -> 0 matches]; see R-W3b-002. |
| 15 | FIX-DOCS: 11 blank citations re-pointed + companions, lock 11→3 | Yes | [RAN docs lock at HEAD -> 5 passed]; changed cites spot-verified §1.4 #1-8. |
| 16 | FIX-DOCS: ADR-0038 ×3 re-pointed (69/218/186) | Yes | [VERIFIED §1.4 #4] |
| 17 | FIX-DOCS: overview:96 slots re-mapped by symbol (incl. pre-existing slot 1-2 wrongness) | Yes | Honestly documented in report; [VERIFIED §1.4 #3]. Mapping choice auditable. |
| 18 | FIX-DOCS: residual = exactly 3 recovery cites, FIX-01 in-flight | Yes | Matches `efe4207` diff (3 recovery slots + memory.md:91). |
| 19 | FIX-DOCS: memory.md:95 `creator.py:185` NOT stale | Yes | [VERIFIED creator.py:185 is the determinism docstring] |
| 20 | FIX-DOCS2: 3 recovery re-points (150/125/150), lock 0 | Yes | [VERIFIED recovery.py:150/125; RAN lock 5 passed] |
| 21 | FIX-DOCS2: memory.md:92 `creator.py:1490→1497` reading | Yes (substance) | [VERIFIED creator.py:1490 is a body line, :1497 the def; daemon.py:165 is the drain call]. Label off-by-one: the creator cite is on memory.md:91, line 92 cites daemon:165 (also correct). Substance holds. |
| 22 | FIX-DOCS2: docs commit 4 files, 4+/4− | Yes | [VERIFIED `git show --stat efe4207` -> exact] |
| 23 | FIX-DOCS2: "no other citations audited" (scope honesty) | Yes | Explicitly scoped; my spot-read found the out-of-scope class (§1.4 #9-11) — disclosed as my finding, not their false claim. |
| 24 | FIX-01: no `TYPE_CHECKING` additions, no schema/migration touched | Yes | [VERIFIED diff file list; conventions 21 passed] |
| 25 | FIX-01: pre-existing `test_high_friction...` green unchanged | Consistent | Existing tests untouched in diff (additive only) [VERIFIED diff]; not in my scoped run. |

25 claims checked, 1 false (test-count arithmetic, #10). No band-aids: the
recovery exemption is the root-cause fix REV-W3 recommended; the reflection
guard is narrow (only `VaultConflictError`) with the fence itself unchanged;
redaction reuses the single `models.base.redact` implementation.

## 3. Long-run test (independent answers)

- **Source:** the exemption derives from the ledger's own events (all-`TIER_*`
  on the deterministic CID), not a registry/flag; panel values derive from
  replayed `POLICY_CLEARED` events, never synthesized. Held.
- **Load (100×):** exemption adds O(1) CID compare per lineage + one full
  history read *only* for the sweeper CID per sweep (every other lineage
  returns after the CID compare). It appends nothing, so 100 boots add 0
  events. The sweeper lineage grows unboundedly over time, so the helper's
  `read_correlation` is O(n) per boot — but `project()` already reads the
  same history in the same sweep, so the incremental cost is one extra read,
  not a new asymptotic (DO-LATER: an `EXISTS non-TIER` query, §7).
  `run_weekly` conflict path is O(1)/domain; redact is O(1)/exception.
- **Hostile input:** hostile ids cannot be exempted (exact `uuid5` + TIER-only
  history, both pinned); the one hostile gap found is the fence-marker
  `ValueError` via a crafted `domain` (R-W3b-003, probed live). Panel values
  render via `Text` (unchanged). The conflict log carries `exc.path` +
  redacted error, never file content.
- **Operability:** new observable lines `recovery.skipped_system_lineage`
  (info) and `reflection.playbook_conflict_skipped` (warning, domain+path+
  redacted error). Rollback is a plain revert (no migration, no schema, no
  new persisted state; `SKIPPED_SYSTEM_LINEAGE` is reporting-only). The
  residual count inflation (R-W3b-004) is the main operability wart: an
  operator reading `/health` sees one phantom "open task" forever.
- **Reversal:** revert `recovery.py` → all 5 phantom tests fail
  (RESUMED/requeue/audit-event/ledger growth). Revert the `try` in
  `reflection.py` → both run_weekly tests error with `VaultConflictError`.
  Revert any panel builder to `[]`/`0` → panel tests fail (len/gates/roles).
  Revert the limit clamp → limit test fails (2 at `limit=1`). Revert runner
  redact → **no test fails** (R-W3b-002). Revert any docs re-point → lock
  fails, except the lock-blind companions (see §1.4 #9-11), whose correctness
  rests on symbol reading.

## 4. Findings

### R-W3b-001 [MINOR] Lock-green but semantically off-by-shift docs citations in touched files
- Location: `docs/architecture/component-interactions.md:98` (`boot_sweep()`
  `recovery.py:226`), `docs/modules/memory.md:147` (`curator.py:141`),
  `docs/flows/workflow-03-crash-recovery.md:19,25,35,57,146-150`,
  `docs/modules/ledger.md:95-108,148-150`,
  `docs/runtime/background-jobs-and-events.md:44-126` (recovery cites).
- Evidence: `boot_sweep` def is at `recovery.py:236`, line 226 is
  `return None` [VERIFIED]; `run_maintenance` is at `curator.py:154`, line
  141 is the tier-sweep comment [VERIFIED]. FIX-01 added +10 lines above
  `boot_sweep` and W3 added +13 above `run_maintenance`; citations that were
  correct at `6464eac`/`f31fb4f` now land on non-blank wrong lines, so the
  lock (non-blank check) cannot see them [RAN lock -> 5 passed].
- Why it matters: docs-only, gate stays green — but a reader following
  `boot_sweep() (recovery.py:226)` lands inside `_latest_mark`. This is the
  exact lock-blind class FIX-DOCS handled for its companions and missed here.
- Recommended fix: docs-only follow-up re-pointing by symbol (recovery cites
  that were exact at base: +10; `memory.md:147` → `curator.py:154` or `:64`).
  Not a gate blocker: the lock is the gate and it is green.

### R-W3b-002 [MINOR] Runner redaction incomplete and unpinned
- Location: `src/paa/tui/runner.py:72,93,395,403` (4 surviving
  `error=str(exc)`), plus no test in `tests/tui/` asserting redacted output.
- Evidence: [VERIFIED grep — 7 redact sites in scope, 4 unredacted siblings
  remain in the same file]; [VERIFIED grep tests/tui for redact/log-event
  names -> 0 matches]. The fix report discloses both (FINDING-2 + §5 reversal).
- Why it matters: the 4 siblings are the same leak shape R-W3-005 flagged
  ("align ... with the other known siblings"); exception strings today carry
  ledger/DB messages, not payload secrets, so this is hygiene, not an active
  leak. Unpinned means the next edit can silently un-redact.
- Recommended fix: redact the 4 siblings the same way + one
  `capture_logs` test per surface asserting a secret-shaped exception string
  is scrubbed (mirror the reflection conflict-log pattern).

### R-W3b-003 [MINOR] `run_weekly` still crashes on fence-marker `ValueError` (hostile domain)
- Location: `src/paa/improvement/reflection.py:184-186` (catches only
  `VaultConflictError`) × `src/paa/memory/world_model.py:246-253` (plain
  `ValueError`) × `reflection.py:260-268` (free-form `domain`).
- Evidence: [RAN probe — `apply_to_playbook("<!-- paa:managed:BEGIN
  reflections -->")` raises plain `ValueError: managed content ...
  contains fence markers`, which `run_weekly` does not catch]. A
  `TASK_REQUESTED` with `request.domain` containing a marker string, at high
  friction, makes `synthesize_rule` emit a marker-carrying rule and aborts
  the weekly pass — the same crash class as R-W3-003, via a different fence
  exception.
- Why it matters: `run_weekly` is not yet scheduled (W4/T-013 per REV-W3), so
  this is latent, not live. The fix must land with the W4 wiring.
- Recommended fix: catch the fence family (`ValueError` — `VaultConflictError`
  is a subclass, and the other plain-`ValueError` sites are unreachable with
  the fixed `playbooks.md` filename) or sanitize/stamp the domain before
  synthesis; add a hostile-domain test mirroring the in-block-edit test.

### R-W3b-004 [MINOR] Sweeper CID still counted as an open task (phantom count, not phantom execution)
- Location: `src/paa/ledger/recovery.py:288-297` (skip only) ×
  `open_correlations` head projection × `/health` `open_tasks` + TUI
  `_gather_rows`.
- Evidence: [VERIFIED phantom test asserts the projection stays `CREATED`/
  non-terminal and the CID stays in `open_correlations`]. The MAJOR's core
  (requeue every boot) is fixed; the count inflation REV-W3 cited as part of
  R-W3-002's impact is not.
- Why it matters: one permanent phantom in operator-facing counts/rows.
  Cosmetic, disclosed, outside the fix write-set (`replay.py`/`store.py`).
- Recommended fix: filter the system lineage in `open_correlations` consumers
  or teach the head projection — natural home is R-W3-006's shared-projection
  work. Track as follow-up; do not hold the R3 gate on it beyond noting it.

### R-W3b-005 [NIT] Fix-report test count arithmetic
- Location: `reports/w3-review/W3-FIX-01.md` §3 ("13 new cases").
- Evidence: diff adds 5 + 2 + 2 + 5 + 1 = 15 new tests [VERIFIED collect-only:
  phantom 5, panels 2, run_weekly 2, tier P2 5; plus 1 coder]. No code impact.

### R-W3b-006 [NIT] Recovery helper does a full history read per sweep
- Location: `src/paa/ledger/recovery.py:502-527` (`store.read_correlation(cid)`
  + `all(...)` over every event).
- Evidence: sweeper lineage grows by one event per moved fact per tick,
  forever. `project()` in the same sweep already reads the full history, so
  this doubles one read rather than adding an asymptotic — but a
  `WHERE event_type NOT IN ('TIER_DEMOTED','TIER_ARCHIVED') LIMIT 1` existence
  query would make the check O(1)-ish. DO-LATER (§7).

## 5. Attempts to break it

1. **Prefix/pattern exemption swallowing a real task** — reasoned: UUID
   equality, no prefix path exists in the helper; the same-sweep real-task
   control test pins RESUMED+requeue for the real CID. Held.
2. **Id-reuse on the sweeper CID** — pinned by test (non-TIER event → RESUMED).
   Held.
3. **Empty-history sweeper CID** — `bool(events)` guard; unreachable via
   `open_correlations` (heads exist only with events) but correct if called
   directly. Held.
4. **`read_correlation` raising inside the helper** — `boot_sweep` per-head
   except converts to `UNRECOVERABLE`; sweep continues. Held by construction.
5. **In-block human edit vs `run_weekly`** — real-file test (both domains
   skip, edit stands, report returned) + failure-injected continuation test.
   [RAN both passed.] Held.
6. **Fence-marker smuggling via crafted domain** — [RAN probe] plain
   `ValueError`, uncaught → R-W3b-003. **Broke** (latent; weekly unscheduled).
7. **Import cycle via `models.base` / `memory.tier`** — [RAN fresh imports +
   conventions 21 passed]. Held.
8. **Empty-forever panel builders** — panel tests require len 2 / 1 gate /
   2 roles by CID. [RAN passed.] Held.
9. **Unredacted runner logs** — 4 siblings survive; no test pins redaction.
   Not a break of the fix's stated scope (7 new lines), but the file is not
   fully aligned → R-W3b-002.
10. **Docs lock** — [RAN 5 passed]. Held as a gate; semantic residuals are
    R-W3b-001.

## 6. Dimension checklist

| Dimension | Verdict | Notes |
|---|---|---|
| Correctness | PASS | Exemption, guard, redact, panels all do what REV-W3/TREV-W3 asked |
| Root cause vs symptom | PASS | Recovery-side exemption (not a sentinel/per-fact CID); narrow catch, fence unchanged |
| Regression/compat | PASS | 75 scoped passed by me; existing tests additive-only; additive enum member, no exhaustive match to break |
| Concurrency/idempotency | PASS | Exemption appends nothing (repeat sweeps no-op, pinned); fence single-writer unchanged |
| Error handling | PASS with note | Per-domain skip + per-panel isolation hold; plain-`ValueError` fence path uncaught (R-W3b-003) |
| Security/abuse | PASS with note | Exact-match exemption; redacted new logs; 4 siblings + hostile-domain gap are MINOR (R-W3b-002/003) |
| Performance/scalability | PASS with note | One extra full-history read per sweep (R-W3b-006); panel 3×50 cost is R-W3-006, untouched |
| Migration/rollback | PASS | No schema/event/persisted-state change; plain revert |
| Observability | PASS with note | Two new structured log lines; phantom count residual (R-W3b-004) |
| Maintainability/conventions | PASS | `str,enum.Enum`, no `TYPE_CHECKING` additions, single `redact` impl, line-length ok (ruff 0) |
| Docs impact | MINOR | Lock green (5 passed); semantic residuals R-W3b-001 |
| Scope creep | NONE | 8-file code commit matches write-set; docs commits docs-only |

## 7. Optimization and simplification opportunities

| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| O-1 | `recovery.py:_is_system_tier_lineage` | Full `read_correlation` + `all()` per sweep; lineage grows forever | Small (1 extra read/boot today, grows with history) | Small (existence query) | Low | DO LATER |
| O-2 | Panels 3×50 projections (R-W3-006) | Unchanged by this fix; pre-existing | Latency under poll/load | Small (shared pass) | Low | DO LATER (already tracked) |
| O-3 | Top-level `tier` import in `recovery.py` | Deferred import works; no cycle exists today | None measurable | Trivial | Low | NOT WORTH IT (deferral is fine) |

## 8. Cross-agent / cross-task interactions observed

- **FIX-01 × FIX-DOCS/DOCS2 sequencing worked as designed:** the 3 blank
  recovery citations were left as an exact residual list and closed by
  DOCS2 once FIX-01 landed (`efe4207`, 4+/4−). The +10 shift's lock-blind
  wake (R-W3b-001) is the part the sequencing could not see.
- **Sweeper × recovery × observability:** stopping the requeue without
  touching `open_correlations` leaves the count phantom (R-W3b-004) — the
  natural fix point is the R-W3-006 shared-projection work, which already
  owns the panel/health path.
- **Reflection × fence:** the guard propagates the fence's *conflict*
  semantics to the caller; the fence's other `ValueError` shape was not
  propagated (R-W3b-003). W4/T-013 wiring should take both.

## 9. What I could not verify

- Full `pytest tests/` and full `mypy src` (scoped-check rule). My evidence
  covers every file the fix touched plus the import/docs locks and the
  immediate neighbors I read. Implementer full-tree claims (ruff 0 tree-wide,
  mypy total 3, 115 scoped tests) are consistent with my scoped runs but not
  independently re-run.
- PostgreSQL/S3 live paths, Qdrant backend, Docker/CI — no server env; tests
  skip by design. The fix touches none of the server-backed paths.
- Fail-before by actual revert (read-only role) — assessed by construction +
  the implementers' honest reversal notes (redact revert passes, see §3).
- Semantic correctness of every docs citation in the tree — spot-checked the
  fix-touched files (§1.4) plus the recovery-cite grep; a full semantic sweep
  remains a separate task (as FIX-DOCS stated).
- Load/performance measurement — all load statements are reasoned from code,
  not measured.
