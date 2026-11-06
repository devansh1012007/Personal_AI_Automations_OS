<!-- ORCH-DIGEST:START -->
agent: W4-DELTA-REV | scope: Wave 4 delta (d7801a8 + 51d5eba vs c8892df) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 0 | NIT 1
claims_audit: 14 claims checked / 0 unsupported or false
band_aids_found: none
top_3_risks: per-phase tracking still absent (preview-forever for any erroring phase, now loud); failure COUNT can raise; T-03 note still stale (deferred)
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 1
reversal_check: done (rely on IMPR-01 pre-fix worktree evidence, consistent with code read)
confidence: high
<!-- ORCH-DIGEST:END -->

# Code review — W4-DELTA-REV

## 0. Method
Diff reviewed: `git diff c8892df..HEAD -- src/ tests/ docs/` excluding docs/agent-ops (6 files, +475/-27) [VERIFIED via `git diff --stat`]. Blind pass on src diff first, then IMPR-01/IMPR-DOCS reports. Scoped checks: e2e+daemon suites, docs-refs, ruff on touched files. Read COMMS.md through line 58.

## 1. Blind-pass findings (before implementer reports)
- R-W4-001: fallback now consumes `_curate_preview_done` only after successful `sweep(dry_run=True)`; a raising sweep propagates before the flag write (no swallow). Correct.
- R-W4-002: `errors==0` gate retained, plus loud `daemon.curation_preview_incomplete` warning under `elif dry_run`. Safe direction, honest docstring.
- R-W4-003/006: idle COUNT skipped via same-predicate argument; failure COUNT added on cold path only. Predicate parity verified in facts.py (see §2).
- R-W4-004: `outcome="processed"` additive, symmetric with IGNORED `outcome="ignored"` [VERIFIED creator.py:1174,1208]. No other findings at src level.

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| R-W4-001 chosen = "still eventually deletes" (preview-then-delete on fallback) | YES | [VERIFIED src/paa/daemon.py:279-290] consumes flag after successful dry-run sweep; constructor failure re-warns every tick via `daemon.curator_unavailable` daemon.py:276. Permanently-broken curator therefore deletes from tick 2 AND stays loud — safe, correct choice between the two brief options. |
| R-W4-002 kept safe preview-forever + warning (no per-phase fields) | YES | [VERIFIED src/paa/memory/curator.py:72-85] `CurationReport` has no per-phase outcome fields; [VERIFIED curator.py:214-219] per-phase try/except increments `errors` and continues while `completed_fully` (stopped_early_at is None) stays True. Relaxing the gate would delete phases never previewed. Warning fires every dry-run tick [VERIFIED daemon.py:304-319]. |
| Stale daemon tests aligned (preview contract) | YES | Two fallback tests now drive two real ticks, assert flag + remaining 1→0 [VERIFIED git diff tests/daemon/test_maintenance_loop.py]. Orphan test corrected to refined guard semantics (NULL-provenance dummy swept). |
| Orphan semantics correct | YES | Docstring cites provenanced-skip leg owned by tests/memory/test_curator.py; daemon test now asserts the swept leg (orphans_removed 1, remaining 0). Keeps daemon suite honest about W3 delta. |
| R-W4-003 idle skip: same predicate, count==0 by definition | YES | [VERIFIED src/paa/memory/facts.py:1066-1073] `pending_embeddings` WHERE `embedding_status='pending' AND superseded_by IS NULL` + LIMIT; [VERIFIED creator.py:1565-1571] COUNT has identical predicate. Empty fetch ⇒ 0 exact, even with LIMIT. Non-empty ⇒ COUNT is the true total. |
| R-W4-004 outcome symmetry | YES | Both branches share `outcome/facts/entities/dropped/drop_counts`; PROCESSED now carries `outcome="processed"` [VERIFIED creator.py:1205-1213]. |
| R-W4-005/P1 backlog leg + T-01/T-02/T-04 tests exist | YES | 8 new tests in test_e2e_remember_recall.py [VERIFIED git diff def test_ listing]. |
| R-W4-006 failure line carries pending_count | YES | [VERIFIED creator.py:1540-1548] COUNT once on exception path, `backlog_remaining=pending_count>0`. |
| Docs 1453→1462, 1273-1300→1282-1309 hold claims (not just non-blank) | YES | [VERIFIED creator.py:1462] is `_enqueue_embeddings` def (embeds new facts, matches persistence-and-storage claim); [VERIFIED creator.py:1282-1288] is `_claim` with atomic-claim docstring + single-transaction body through 1309 (matches Plan1 claim). |
| Reversal evidence (5 e2e + 2 daemon tests fail pre-fix) | PLAUSIBLE | Consistent with code read: each new assertion targets the exact changed line. Not re-run (read-only review, no pre-fix worktree). |
| T-03 deferred (runtime_tools note stale) | ACKNOWLEDGED | Out of improver write-set; NEED posted. Test pins only `note` existence. Still stale — carried as known residual, not a new defect. |
| Docs-content-skew beyond the 2 blank citations (IMPR-DOCS §6) | UNVERIFIED | Lock guarantees non-blank only; daemon 233-320 / creator 1200-1560 neighbours not re-derived. No evidence of skew found in this pass. |

## 3. Long-run test (independent answers)
- Source: root-cause fixes at call sites (flag consumption, warning, idle skip, additive key, cold COUNT). No invariant bypassed.
- Load (100x): R-W4-003 removes ~1 Hz idle COUNT on daemon drain; R-W4-006 adds 1 indexed COUNT on cold error path only. No N+1, no unbounded fetch, no new loop.
- Hostile input: no new SQL interpolation or LIKE; COUNT constant; synonym/hash paths byte-identical. No injection surface added.
- Operability: rollback = revert commits, no migration. Observe via `pending_count` (INFO+ERROR), `outcome` on both branches, `curation_preview_incomplete` WARNING. Permanent phase error now pages visibly (warning every tick) while retention stays stalled — correct fail-safe.
- Reversal: reverting src makes the 5 e2e / 2 daemon assertions fail (IMPR-01 evidence); reverting either doc citation re-trips docs-refs blank-line test. Suite guards the fix both ways.

## 4. Findings
### R-W4-D-001 [NIT] Failure-path COUNT can raise and mask the original error
- Location: `src/paa/memory/creator.py:1540` · Evidence: the COUNT sits inside the `except` block before `log.error`; if it raises, the function propagates the COUNT error instead of returning 0 with the sink error logged. · Why: minor — COUNT failure is rare and arguably surfaces a real DB problem loudly, but the comment's intent (operator parity) is defeated if the gauge query is the thing that fails (e.g. transient connection loss that also killed the sink). · Recommended fix (root-cause level, not a blocker): none required — either accept as loud-is-better, or guard the COUNT so the original sink error always logs. Do not wrap blindly without logging the secondary failure.

No BLOCKER / MAJOR / MINOR. No new defects introduced by the delta.

## 5. Attempts to break it (concrete inputs/interleavings)
1. Fallback sweep raises on preview tick → flag NOT consumed (exception propagates before line 289) → next tick still previews. Safe.
2. Permanently erroring curator phase → every dry-run tick warns `daemon.curation_preview_incomplete` and keeps fact (test `test_errored_preview_blocks_deletes_and_warns`). Retention stalls but never deletes unpreviewed data. Safe/loud as designed.
3. Budget stop (no errors) → same warning; resumes via `_curate_next_phase` and can still complete later. Warning is slightly noisy for benign budget stops, but it is information-true (preview incomplete).
4. `pending_embeddings` returns empty while LIMIT small → COUNT unreachable, 0. Correct.
5. Sink present, sink fails, concurrent drain marks rows between fetch and COUNT → `pending_count` reflects post-drain truth; `backlog_remaining` derived from it. More honest than prior hardcoded True.
6. Sink absent + backlog → COUNT runs (pending non-empty), gauge shows exact backlog. Correct.
7. PROCESSED vs IGNORED log consumers → additive key only; no schema/contract break.

## 6. Dimension checklist
Correctness ✅ · root cause vs symptom ✅ · regression/compat ✅ (additive key; gauge semantics preserved except failure-path backlog now derived, strictly more truthful) · concurrency/idempotency ✅ (no new shared state; flag write is single-threaded daemon tick) · error handling ✅ (falling sweep never consumes preview; per-phase isolation unchanged) · security/abuse ✅ (no new surface) · performance ✅ (net removal of ~1 Hz idle COUNT) · migration/rollback ✅ (no schema; revert-safe) · observability ✅ (new warning + gauges) · maintainability/conventions ✅ (docstrings carry the trade-off; no noqa/type-ignore added) · docs impact ✅ (2 citations re-derived and content-verified).

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | Per-phase outcome tracking in CurationReport (R-W4-002 proper fix) | curator.py:72-85 no per-phase fields | Lets a clean-decay/erroring-reconcile pass still delete | Medium, cross-file | Changing delete semantics | DO-LATER (correctly deferred; warning makes current behaviour loud) |
| 2 | Move pending COUNT into facts.py repository | IMPR-01 §4 declined | None measurable | Low | Parity churn | NOT-WORTH-IT (agreed) |

## 8. Cross-agent / cross-task interactions observed
No conflicts in delta. T-03 NEED (runtime_tools.py stale note) and non-blank docs content-skew risk remain open operator items, both already NEED-posted and flagged in IMPR-DOCS §6. No new cross-task coupling introduced by the delta.

## 9. What I could not verify
- Full suite (scoped only per standing orders).
- Reversal in a pre-fix worktree (rely on IMPR-01 §5 transcripts + code-read consistency).
- Content-skew of docs citations outside the two blank-line failures (non-blank lock limitation).
- Live Qdrant / real embedder fidelity.

## 10. Commands run and results (scoped)
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py -q -> 29 passed (18 e2e + 11 daemon, 100%)]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q -> 5 passed]`
- `[RAN .venv\Scripts\python.exe -m ruff check src/paa/daemon.py src/paa/memory/creator.py tests/memory/test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py -> All checks passed!]`
