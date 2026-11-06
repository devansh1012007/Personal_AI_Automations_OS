<!-- ORCH-DIGEST:START -->
agent: W1-IMPR-02 | scope: T-001 R-W1-DOCS fix (17 path:line blank citations) | status: DONE
findings: FIXED 17 | REJECTED 0 | DEFERRED 0 (+3 out-of-scope observations)
blockers_major_open: none
code_commits: 5b8d6ee (docs) | report_commit: (this file's commit)
tests_added_or_strengthened: 0
ownership_violations: none
confidence: high
<!-- ORCH-DIGEST:END -->

# Improvement report — W1-IMPR-02 R-W1-DOCS

## 1. Table (17 citations)

| # | Doc location | Old ref → New ref | What the new line holds [VERIFIED] | Verified |
|---|---|---|---|---|
| 1 | component-interactions.md:226 | `runtime.py:431` → `runtime.py:496` | `async def _acquire_sandbox(settings: Settings) -> Any:` [src/paa/runtime.py:496] — `_acquire_sandbox` definition; bounded probe described in following docstring | yes |
| 2 | overview.md:59 | `README.md:94` → `README.md:174` | ```` ``` ```` fence opening the Architecture diagram (heading at :172) [README.md:174] — reproduced diagram start; old :94 was fence start pre-f8c12a6 [git show f8c12a6^:README.md:94] | yes |
| 3 | overview.md:96 | `:383` → `:423` | `def _try_queue(settings: Settings, db: RelationalBackend) -> Any:` [src/paa/runtime.py:423] — third function in the list (`_try_vector/_try_graph/_try_queue/_acquire_sandbox`) | yes |
| 4 | overview.md:96 | `:393` → `:496` | `async def _acquire_sandbox(settings: Settings) -> Any:` [src/paa/runtime.py:496] — fourth function in the list | yes |
| 5 | overview.md:146 | `README.md:114` → `README.md:194` | `Task lifecycle. Every arrow writes a ledger event first:` [README.md:194] — old :114 was same sentence pre-f8c12a6 | yes |
| 6 | data-lifecycle.md:126 | `README.md:87` → `README.md:351` | `Defects found by tests rather than by reading: OFFSET pagination skipping rows while` [README.md:351] — OFFSET/keyset defect; old :87 was `- Keyset vs OFFSET pagination ...` pre-f8c12a6 | yes |
| 7 | persistence-and-storage.md:154 | `runtime.py:392` → `runtime.py:394` | `# ---------------------------------------------------------------------------` header start of Optional-backend construction block [src/paa/runtime.py:394] — range start of `_try_vector..` block; old :392 was same header | yes |
| 8 | persistence-and-storage.md:154 | `:398` → `:400` | `def _try_vector(settings: Settings) -> tuple[Any, Any]:` [src/paa/runtime.py:400] — range end; old :398 was same def | yes |
| 9 | Plan1.md:119 | `daemon.py:179` → `daemon.py:189` | `return self._rt.memory_creator or MemoryCreator(self._rt.db)` [src/paa/daemon.py:189] — MemoryCreator construction inside `_memory_creator()` (def at :180, import at :187); old :179 was the import inside same builder [git show c5766c8^:src/paa/daemon.py:179]. Chose construction line, not blind +8 to import line, per AGENTS "read not re-number" | yes |
| 10 | daemon.md:60 | `daemon.py:163` → `daemon.py:173` | `await DecaySweeper(self._rt.db).sweep()` [src/paa/daemon.py:173] — `sweep()` call; old :163 was `from paa.memory.decay import DecaySweeper` inside `_maintenance_loop` [git show c5766c8^:src/paa/daemon.py:163] | yes |
| 11 | sandbox.md:114 | `runtime.py:431` → `runtime.py:496` | `async def _acquire_sandbox(settings: Settings) -> Any:` [src/paa/runtime.py:496] — section header target | yes |
| 12 | sandbox.md:151 | `runtime.py:431` → `runtime.py:496` | same def [src/paa/runtime.py:496] — table Source column | yes |
| 13 | storage.md:85 | `runtime.py:398` → `runtime.py:400` | `def _try_vector(settings: Settings) -> tuple[Any, Any]:` [src/paa/runtime.py:400] — optional-backends construction; old :398 was same def | yes |
| 14 | storage.md:86 | `runtime.py:431` → `runtime.py:496` | `async def _acquire_sandbox(settings: Settings) -> Any:` [src/paa/runtime.py:496] | yes |
| 15 | validation.md:83 | `runtime.py:495` → `runtime.py:571` | `from paa.validation.engine import DeterministicValidationEngine` [src/paa/runtime.py:571] inside `try:` at :570 / `except` at :577–:578 with `runtime.validator_unavailable` warning — lazy import + graceful degrade claimed. Old :495 was a `)` closing the sandbox log line [git show c5766c8^:src/paa/runtime.py:495] — already stale at doc time; new line genuinely holds the claim | yes |
| 16 | deployment-and-runtime.md:37 | `runtime.py:392` → `runtime.py:394` | header start [src/paa/runtime.py:394] — same as #7 | yes |
| 17 | deployment-and-runtime.md:37 | `:398` → `:400` | `def _try_vector` [src/paa/runtime.py:400] — same as #8 | yes |

Method per AGENTS.md rule: for each citation I read doc context, read target file around cited line, used `git show` on pre-Wave-1 (`c5766c8^`) and pre-f8c12a6 (`f8c12a6^`) to recover what the old line held, then located the current line holding the same claim — not a uniform delta (deltas varied: +65 for sandbox def 431→496, +2 for header 392→394, +8 for daemon import, +257 for README diagram 94→174).

## 2. Root cause and context

- Root cause: `src/paa/runtime.py` `_build_memory_creator` grew to 61 lines (new kwargs + sink wiring) shifting everything after it by ~33–65 lines; `src/paa/daemon.py` drain block (+8) shifted maintenance/creator builders; operator commit f8c12a6 rewrote README.md (+624/−246) moving diagram/lifecycle/defect sections by ~+80/+80/+264. Citations were not updated in either step, landing on blank lines (the exact signature the lock's docstring describes).
- Affected files: 10 docs in WRITE-SET only. No source edits. No public interfaces/invariants touched.
- COMMS last line read at start and before finishing: `[2026-11-06T18:30:00Z] W1-IMPL-01 -> ORCH | DONE | T-001` [docs/agent-ops/20261106-memory-remembering-agent/COMMS.md:27] — no ORCH directive lines affecting this diff.

## 3. Changes executed (atomic, by file)

- component-interactions.md (1 cite), overview.md (4 cites), data-lifecycle.md (1), persistence-and-storage.md (2), Plan1.md (1), daemon.md (1), sandbox.md (2), storage.md (2), validation.md (1), deployment-and-runtime.md (2). Each edit was a single-line citation number change; no prose, formatting, or other files touched. `git diff` inspected and confirms only those lines (see §5).
- `DECISION:` Plan1 chose construction line :189 over import-line shift :187 because the sentence claims "Creator is built" — construction, not import, holds that claim. `INVARIANT:` all 17 new lines are non-blank and name the claimed symbol/section; test `test_every_referenced_line_is_not_blank` now passes.

## 4. Review audit

- Self-review of diff: [MINOR, noted] overview.md:96 siblings `:359`/`:372` are non-blank (not in broken set) but do not hold `_try_vector/_try_graph` (they are `recovery=recovery,` and `"runtime.built",`) — they were already approximate at doc time [git show 3c500de:src/paa/runtime.py:359,372]. Left unchanged per brief scope (17 broken only); reported here as follow-up. Similarly component-interactions.md:223 `runtime.py:387` is non-blank but now points inside `log.info` (`graph=...`), not the `_try_*` factories — not in broken set, left unchanged, flagged below.
- [MINOR, noted] Several docs say the sandbox probe is bounded at 5s (component-interactions:225, storage:86, deployment:40) but `_SANDBOX_PROBE_BOUND_SECONDS = 30.0` [src/paa/runtime.py:63] and sandbox.md:115–118 correctly says 30s. Prose value mismatch is separate from line-reference breakage; left untouched (out of scope) and flagged below.
- No independent reviewer available (nested spawn would hit depth limit, same as IMPR-01); self-review + test run substituted. Fresh-context reviewer should still audit.

## 5. Verification evidence (scoped only)

- [RAN `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank -q` (pre-fix) → FAILED with exactly the 17 listed in brief §3]
- [RAN `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (post-fix) → `..... [100%]`, `EXIT:0`, 5 passed]
- Full suite NOT run (STANDING-ORDERS §16 scoped-checks rule). No ruff/mypy needed — docs-only change, no src edits.
- Reversal: reverting any single citation to its old number re-fails the blank-line test for that reference (old lines :383/:431/:495 etc. are blank in current tree [VERIFIED by pre-fix failure list]).

## 6. Long-run test (Standing Orders §8)

- Source: docs only, no runtime behavior — no load/concurrency/partial-failure surface. Source-of-truth (ledger, runtime, README) untouched.
- Load/Hostile: a stale citation is documentation drift, not executable input; the lock (`tests/conventions/test_docs_references.py`) is the guardrail and it now passes — future shifts will fail fast there, not silently.
- Operability: rollback = revert the docs commit; no migration.
- Reversal: reintroducing any old number fails the blank test. Conversely if a fix were reverted, only the docs-reference gate fails — no test for runtime behavior changes (none made).

## 7. Unverified assumptions & remaining risks / out-of-scope findings

- [ASSUMED] None that affect the gate. Every new line was read in the current tree and holds the claimed content (table above).
- FINDING (non-blocking, needs ORCH decision — FINDING only, not fixed): overview.md:96 `:359`/`:372` and component-interactions.md:223 `runtime.py:387` are non-blank but semantically stale for `_try_*` claims; a fuller sweep would update them to `:400`/`:413` (and `:400–:430` range) respectively.
- FINDING (non-blocking): 5s vs 30s probe-bound prose mismatch noted in §4 (sandbox.md is correct at 30s). Fixing prose is outside this line-reference brief.
- No other findings. Ownership violations: none. Forbidden files untouched (`git status` shows only WRITE-SET docs + this report + brief).

## 8. Git state

- Branch: `ops/20261106-memory-remembering-agent` [VERIFIED `git branch --show-current`]
- Commits: docs commit (next), then this report in a separate docs-only commit with explicit paths. Trailers `Task: T-001` / `Agent: W1-IMPR-02` / `Wave: 1` / `Report: <path>` on both.
