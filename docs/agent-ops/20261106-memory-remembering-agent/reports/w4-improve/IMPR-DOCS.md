<!-- ORCH-DIGEST:START -->
agent: W4-IMPR-DOCS | scope: wave 4 docs-reference fix | status: DONE
findings: 2 broken citations fixed
blockers_major_open: none
code_commits: 51d5eba | report_commit: pending
tests_added_or_strengthened: none (docs-only re-derivation)
ownership_violations: none
confidence: high (conventions 5 passed post-fix; see §4)
last_comms_line_read: [2026-11-07T00:00:00Z] W4-IMPR-01 -> ORCH | DONE | T-007 | FACT W4 improvement done: 3 stale daemon tests + R-W4-001..006 + T-01/T-02/T-04/T-05 fixed, T-03 + docs NEED deferred | ev: reports/w4-improve/IMPR-01.md
<!-- ORCH-DIGEST:END -->

# Improvement report — W4-IMPR-DOCS

## 1. What was broken

`tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank` failed with 2 citations landing on blank lines (re-derived from test output; full transcript in §4). Root cause: two shifts in `src/paa/memory/creator.py` stacked above both citations: +8 lines from the IGNORED `log.info` block in commit 326b16d [VERIFIED via `git diff 326b16d^..326b16d -- src/paa/memory/creator.py`], and +1 line (`outcome="processed"`) in commit d7801a8 [VERIFIED via `git diff d7801a8^..d7801a8 -- src/paa/memory/creator.py`]. Total +9 lines, so each citation is stale by exactly 9 lines. Cross-checked against the pre-shift tree at 7da607f: line 1453 was `async def _enqueue_embeddings...` and line 1273 was `async def _claim...` [VERIFIED `git show 7da607f:src/paa/memory/creator.py` lines 1453 and 1273].

## 2. Old → new citation table (with target-line verification)

| Doc | Claim (sentence subject) | Old citation | New citation | New target lines hold the claim? |
|---|---|---|---|---|
| `docs/data/persistence-and-storage.md:102` | "`MemoryCreator` (embeds new facts)" | `creator.py:1453` | `creator.py:1462` | YES — line 1462 is `async def _enqueue_embeddings(self, fact_ids: Sequence[str]) -> None:` and 1463-1469 docstring/hand-off: "Hand new facts to the vector substrate, if one is attached... `if self._sink is None or not fact_ids: return`" [VERIFIED src/paa/memory/creator.py:1462-1469]; the blank line 1454 that the test flagged sits just above the `_enqueue_embeddings` owner context (address `persisted` UPDATE at 1455-1459 prev block) |
| `docs/memory-polish/plans/Plan1.md:203` | "Concurrent drain + daemon drain \| `creator.py:1273-1300` (`_claim`) \| Atomic claim; second caller gets none" | `creator.py:1273-1300` | `creator.py:1282-1309` | YES — line 1282 is `async def _claim(self, limit: int) -> list[SignalEnvelope]:` and 1283-1288 docstring: "Move up to `limit` unprocessed signals to `processing`. Select and update share one transaction. Under `BEGIN IMMEDIATE` that makes the claim atomic against any other writer, so two creators cannot both believe they own the same signal." [VERIFIED src/paa/memory/creator.py:1282-1308]; range end 1309 mirrors old 1300 (blank line before `return` at 1310), preserving the exact span |

Content no longer exists: none — both targets verified present, no source edit needed.

## 3. Changes

- `docs/data/persistence-and-storage.md`: `creator.py:1453` → `creator.py:1462`.
- `docs/memory-polish/plans/Plan1.md`: `creator.py:1273-1300` → `creator.py:1282-1309`.
- Docs-only; no source, config, or test edits (out of write-set). Per AGENTS.md docs-references rule ("read, not re-number"), each new citation was read in the current source and confirmed to hold the claimed content, not merely re-numbered by offset.
- DECISION: kept the original span length for the `_claim` range (28 lines old → 28 lines new) and the single def-line for `_enqueue_embeddings`, rather than widening to the full method body, because the prose claims the entry point, not the whole body.

## 4. Commands run and results (scoped)

- [RAN `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (pre-fix) -> 4 passed, 1 failed: blank-line failures only `docs/data/persistence-and-storage.md:102: 'creator.py:1453'` and `docs/memory-polish/plans/Plan1.md:203: 'creator.py:1273-1300'`]
- [RAN `git log --oneline -15 -- src/paa/memory/creator.py` -> d7801a8 (top), 326b16d (2nd)]
- [RAN `git diff d7801a8^..d7801a8 -- src/paa/memory/creator.py` -> +1 `outcome="processed"` line at PROCESSED branch]
- [RAN `git diff 326b16d^..326b16d -- src/paa/memory/creator.py` -> +8 IGNORED `log.info` block + pending_count plumbing]
- [RAN `git show 7da607f:src/paa/memory/creator.py` (lines 1445-1465, 1265-1305) -> confirmed pre-shift line content]
- Post-fix: [RAN `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` -> 5 passed]

## 5. Long-run test (standing orders #8)

- Source: root cause is stacked line drift from two prior commits; re-derived by reading current source, not by mechanical +1 search-and-replace.
- Load/concurrency: docs-only, no runtime change; no new queries, loops, or state.
- Hostile input: no code paths touched; citations are inert text. A bogus offset would fail the conventions lock, which is exactly the reversal probe.
- Operability: rollback = revert the two doc lines + this report; no migration.
- Reversal: revert either citation and the conventions blank-line test fails again on that file (proves the test guards the fix).

## 6. Residual risks

- Not verified: full suite (scoped only per brief). Daemon citations near 233-320 and creator 1200-1560 flagged in IMPR-01 §6 as potentially non-blank-but-skewed were NOT re-derived — out of this brief's task list; the conventions lock only guarantees non-blank, so content skew elsewhere remains UNVERIFIED.
- Not verified: whether `creator.py:1034` / `creator.py:1216-1271` / `creator.py:1400-1451` in Plan1.md still hold their claims (they were non-blank at test time, so not in scope; flagged here for the reviewer, not changed).

## 7. Commits

docs: 51d5eba | report: pending
