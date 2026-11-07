# Brief — W2-GEN-01 (GEN) · wave 2 · run 20261107-integration-fix

## 1. Role and mission
Apply the two MINOR test additions from the wave-1 re-reviews (TREV-W1b R-W1b-01/R-W1b-02) to `tests/agents/test_semantic_derivation_p002.py`: per-hit redaction pin + duplicate-hit-IDs dedup test (TREV P2-c). Why: close the last full mutation survivor in the P-002 file and pin the second redacted emission point from R-W1-002.
Your role: general agent (minor fixes only — test additions, no logic changes). You edit only your WRITE-SET (one test file). Commit only your file.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md` (you are the `general` agent of Step 5 — minor means trivial test additions here; NO behavior/logic/interface changes; if a finding needs logic, post NEED and stop)
2. `AGENTS.md` (zero-baseline)
3. `docs/agent-ops/20261107-integration-fix/reports/w1-review/TREV-W1b.md` §§5–6 (R-W1b-01 exact sketch: secret-shaped corrupt `last_queried_at` e.g. `sk-ABCDEF1234567890` as the corrupt value — `from_iso`'s ValueError embeds the input, so the per-hit `redact()` becomes testable; mirror the degrade test's `***REDACTED***` + token-absent assertions on the per-hit entry; R-W1b-02/TREV P2-c sketch: two `_Hit`s same id → 1 `fetch_all` with 1 placeholder or result+count-1)
4. `docs/agent-ops/20261107-integration-fix/reports/w1-review/TREV-W1.md` §6 P2-c (unchanged sketch)
5. Code: `tests/agents/test_semantic_derivation_p002.py` in full (existing corrupt-row test + degrade test + `_FakeVectors`/`_Hit` patterns — extend, never rewrite), `src/paa/agents/context_agents.py` per-hit log (~:318, read-only — do NOT edit).

## 3. Task and acceptance criteria
Task W2-GEN-01. Specifically:
- (a) Modify the existing corrupt-row test (or add the sibling the sketch describes — either is fine if behavior-pinning): corrupt `last_queried_at` = secret-shaped string; assert the per-hit `context.hydration_failed` entry carries `***REDACTED***` and no raw token substring. Must FAIL if per-hit `redact()` is deleted (reason it in your report; run a mental revert, do not modify src to prove it).
- (b) New duplicate-IDs test per sketch. Must FAIL if the `dict.fromkeys` dedup is removed (placeholder count > 1 or query count > 1 — pin whichever the code exposes).
- Both tests deterministic (injected `now`, `_FakeVectors`, no RNG), no src edits, no other test files touched. If either test cannot pass WITHOUT a src change, stop that half, post NEED, and report DEFERRED-DEPENDENCY — do not change src.
- Acceptance: `pytest tests/agents/test_semantic_derivation_p002.py -q` → 16 passed (14 + 2), 3 repeat runs green (flakiness check); `ruff check` on the test file 0.

## 4. Ownership
WRITE-SET (only this): `tests/agents/test_semantic_derivation_p002.py`. READ-ONLY: everything else. FORBIDDEN: `src/**`, any other test file, docs/**.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory.

## 5. Parallel siblings this wave
W2-IMPL-01..04 (touch/tier vocab/DDL/embed image — none edits your file; W2-IMPL-01 edits `context_agents.py` SOURCE, you edit only its TEST file — if your test run fails due to their in-flight source edit, wait briefly, retry once, then FINDING per standing order 18).

## 6. Skip list and known traps
- R-W1b-03 (TypeError path) is a NIT the reviewer explicitly marked non-gating — DO NOT implement it here (out of your minor scope creep; record it in your report as seen-and-skipped).
- Hostile payload scalar crash + lock-blind off-by-one citations are pre-existing/out-of-scope for you.

## 7. Environment and tests
Scoped only: the one test file (×3 repeats) + ruff scoped. No full suite.
Commit protocol: test commit first, then report in a separate docs-only commit. Trailers `Task: W2-GEN-01`, `Agent: W2-GEN-01`, `Wave: W2`, `Report: docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-GEN-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-GEN-01.md` (ORCH-DIGEST first; long-run test answers for the 2 tests). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
