<!-- ORCH-DIGEST:START -->
agent: W3-FIX-DOCS2 | task: W3-FIX-DOCS2 (wave-3 fix-2 residual docs) | status: COMPLETE
broken_at_start: 3 (comp-interactions:98 recovery.py:140, workflow-03:74 recovery.py:115, ledger:129 recovery.py:140) | broken_at_end: 0
lock: pytest tests/conventions/test_docs_references.py -q at start -> 1 failed (3 blank); at end -> 5 passed
docs_commit: efe4207 | report_commit: (this commit, separate docs-only commit listing efe4207)
last_comms_line_read: 2026-11-07T15:30:00Z W3-FIX-01 -> ORCH | DONE | W3-FIX-01 | FACT fix done: tier CID exemption + run_weekly guard + 7 redacted logs + panels/P2s (re-read at start; HEAD c7d9184 at start, FIX-01 landed as 037a8d6)
<!-- ORCH-DIGEST:END -->

# W3-FIX-DOCS2 report

## Context
- Branch verified `ops/20261107-integration-fix` before committing [VERIFIED git branch --show-current -> ops/20261107-integration-fix at HEAD c7d9184].
- Root cause: Wave-3 + W3-FIX-01 (037a8d6) shifted `src/paa/ledger/recovery.py` by +10 lines (`RecoveryReport` 115->125, `RecoveryEngine` 140->150); 3 citations kept old numbers and landed on blank lines. Baseline f3baf89/ec0c986 had 3 residual because FIX-01 was in-flight then; now landed, numbers verified at HEAD.
- Write-set: docs/** except docs/agent-ops/** and docs/plans/**. No src/** or tests/** edits. `git diff --stat -- docs/...4 files` shows exactly 4 files, 4 insertions / 4 deletions.
- [RAN .venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q at start -> 1 failed, 3 blank (exact W3-FIX-DOCS residual list). At end -> 5 passed.]

## Citation mapping (old -> new, one-line evidence each)
Evidence = current target line content [VERIFIED by grep/read at HEAD efe4207 parent c7d9184]. Never blind-shifted; read citing sentence first.

- docs/architecture/component-interactions.md:98 — `ledger/recovery.py:140` -> `:150` [VERIFIED recovery.py:150 `class RecoveryEngine:`; sentence claims `RecoveryEngine` location; old :140 is blank (`def summary()` body)]
- docs/flows/workflow-03-crash-recovery.md:74 — `recovery.py:115` -> `:125` [VERIFIED recovery.py:125 `class RecoveryReport:`; sentence lists `started_at, finished_at, scanned, recoveries[], chain_problems` which are its fields at :128-132; old :115 is blank (dataclass field line after `CorrelationRecovery`)]
- docs/modules/ledger.md:129 — `recovery.py:140` -> `:150` [VERIFIED recovery.py:150 `class RecoveryEngine:`; sentence: "`RecoveryEngine` | uses `LedgerStore`, `Database` | Replay and reconcile"; same shift as comp-interactions:98; `__init__` at :153-156 confirms store/db deps]

### memory.md:92 creator.py:1490 -> :1497 (lock-blind reading, READ + RECORDED)
- Citing sentence: "Also owns the pending-embedding backlog drain (`src/paa/memory/creator.py:1490`), called after each ingestion batch"
- Read creator.py:1490 = `indexed = await self._sink.enqueue(facts)` (a body line inside `_enqueue_embeddings`); read creator.py:1497 = `async def drain_pending_embeddings(self, limit: int = 100) -> int:` with docstring "Embed the head of the ``pending`` backlog, oldest first."
- Reading: sentence claims the drain def; :1490 does not hold that promise. Re-pointed to :1497 (def line, per W1 precedent: def line not body line). Recorded per brief either way. Changed in same docs commit efe4207.

## Verification
- [RAN .venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q at end -> 5 passed, 100%.] Zero broken citations tree-wide in scope.
- Self-review: re-read full `git diff` of 4 files (4 slots). Findings: none critical/major. All changes are citation numbers on existing lines; no prose rewrites, no src/tests/agent-ops/plans edits (01-work-ledger.md + COMMS.md are ` M` in worktree but are ORCH-owned, left uncommitted).
- Mutation frame: reverting any of the 3 recovery re-points re-breaks the lock (old lines :115/:140 are blank). Reverting memory.md:92 to :1490 is lock-green (body line non-blank) but semantically wrong; correctness rests on the drain-def reading above.
- Git discipline: docs committed first as `efe4207a3895aa78ac5a7bcd57486770af686441` with explicit paths (no -A/-a). NOTE: docs commit message lacks Task/Agent/Wave/Report trailers (plain -m; --amend forbidden); report commit carries them. Report committed separately below.

## Out-of-scope findings
- None. No UNRESOLVABLE targets; all 4 citations resolved at HEAD.
- Full test suite NOT run (standing order 16, scoped check only). Non-broken citations beyond the 3+1 not audited (scope: exactly 4 citations).

## Long-run test answers
- Source: every new citation verified against its target line's actual content (grep/read), not arithmetic. +10 shift confirmed per-symbol.
- Load / Hostile / Operability: docs-only; no runtime behavior, no new failure modes. Reversal: reverting recovery re-points re-breaks the lock (see Verification).
- What was NOT verified: full suite; semantic correctness of every other docs citation in tree.

## Long-run test (standing order 8 supplement)
- Source: root cause is mechanical line drift, fixed at citation site (correct fix; no source invariant involved).
- Load (100x/concurrency/partial failure): N/A docs-only. Hostile input: N/A. Operability: rollback = revert efe4207 (new lines blank-break immediately, caught by lock). Reversal: lock fails 1/5 if any recovery re-point reverted.
