<!-- ORCH-DIGEST:START -->
agent: W3-REV-03 | scope: wave 3 T-N3-3 cancellation (eb9a68b + 5caddcf) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 1
claims_audit: 14 claims checked / 0 unsupported or false
band_aids_found: none
top_3_risks: NEED-3 frozen "tool failed" literal makes cancel vs exception indistinguishable in ledger; cancel during journal.before() window unhandled; _collect drain-phase cancel swallow (pre-existing)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: reasoned-only (loop, read-only constraint) + executed-evidence (sandbox, W3-IMPR-01)
confidence: high (all 6 brief checks executed or code-traced; Windows host only)
<!-- ORCH-DIGEST:END -->
# Code review — W3-REV-03 (T-N3-3 cancellation, independent)

Code under review: `eb9a68b` (loop + bash cancel seam) and `5caddcf`
(sandbox backend arm). This review is the independent review the
implementer could not run (reviewer runs unavailable to it, per
W3-IMPL-03 §self-review). Blind pass done first: source + tests read
before opening either implementation report.

## 0. Method

- Read `src/paa/coder/loop.py`, `src/paa/coder/shell_tools.py`,
  `src/paa/sandbox/subprocess_backend.py`, `src/paa/coder/journal.py`,
  `tests/coder/test_cancellation.py`, `tests/sandbox/test_cancellation.py`,
  `src/paa/ledger/recovery.py:375-422` (`find_interrupted_calls`).
- Ran the brief's command list myself (see §2). Read-only on source:
  no code edited; the only file written is this report.
- Then read `W3-IMPL-03-N3-3.md` + `W3-IMPR-01-improve.md` and
  cross-checked every claim against the code.

## 1. Blind-pass findings

1. **Loop arm structure is correct.** Steps 3–5
   (`loop.py:258-302`) sit in one `try` with a single
   `except asyncio.CancelledError` arm. The inner
   `except Exception` around `tool.run` (`loop.py:288`) does **not**
   catch `CancelledError` (it is a `BaseException` since Python 3.8),
   so a cancel falls through to the outer arm instead of being
   mis-journalled as a generic tool error and swallowed.
   `[VERIFIED loop.py:266-302]`
2. **The FAILED write is shielded, the cancel is not swallowed.**
   `await asyncio.shield(self._journal.after(ctx, False, None))`
   (`loop.py:300`), then `raise` (`loop.py:302`). A second Ctrl-C
   mid-write cannot drop the row; the caller still sees
   `CancelledError`. `[VERIFIED loop.py:293-302]`
3. **Journal vocabulary is frozen, and the debt is real but
   documented.** `ToolJournal.after` has no reason parameter
   (`loop.py:501-503`); the FAILED payload hardcodes
   `"reason": "tool failed"` (`journal.py:545`). A cancel therefore
   lands in the ledger as `TOOL_CALL_FAILED` indistinguishable from
   a tool exception. This is **not** a defect introduced by this
   task — the vocabulary is frozen by ADR-0046 and the loop comment
   (`loop.py:258-265`) + W3-IMPL-03 NEED-3 state it plainly. The
   recovery-exclusion mechanism does not depend on the reason
   string: `find_interrupted_calls` subtracts any PROPOSED that has
   an outcome row of any kind (`recovery.py:387-406`), so the FAILED
   row excludes the call regardless of its literal reason.
   `[VERIFIED journal.py:545, recovery.py:397-406]`
4. **Bash seam is a pure log-and-reraise.** `shell_tools.py:516-528`
   catches `CancelledError` around `sandbox.run(spec)`, logs
   `coder.bash.cancelled`, re-raises, fabricates nothing. Correct:
   a fabricated result here would journal COMPLETED.
   `[VERIFIED shell_tools.py:514-535]`
5. **Backend arm mirrors TimeoutError + full teardown.**
   `subprocess_backend.py:412-437`: `_kill_tree`, `_reap`, reader
   cancel + suppress-await, `job.terminate()`, `job.close()`,
   `raise`. Never returns a `SandboxResult` on this path.
   `watchdog.stop()` in the `finally` (`:438-439`) still runs.
   `[VERIFIED subprocess_backend.py:395-439]`
6. **Two residual cancel windows the arm does not cover**
   (both pre-existing, both honestly recorded by W3-IMPR-01 as
   F1/F2, neither in this task's acceptance criteria):
   - Cancel during `_collect` drain (`subprocess_backend.py:531-532`,
     `except CancelledError: return b"", True`) — process already
     exited, so the result is real; the harm is a missed cancel, not
     a fabricated one, and the loop would journal COMPLETED.
   - Cancel during `create_subprocess_exec` (`:350-366`) skips
     `job.close()` on Windows — a job-handle leak in a sub-millisecond
     pre-child window.
   `[VERIFIED subprocess_backend.py:350-366, 531-532]` — MINOR-2/3.
7. **Cancel during `journal.before()` is outside the arm.**
   `before()` (`loop.py:255-256`) runs before the `try` at `:266`,
   so a cancel delivered while PROPOSED is being appended writes no
   outcome. The ledger append is transactional (either the row
   landed or it did not), so the practical bad case is narrow, but
   it is the same outcome-less-PROPOSED shape the seam exists to
   prevent. MINOR-4 (accepted residual; the before-write is one
   await and the store dedupe makes redelivery safe).
   `[VERIFIED loop.py:251-266]`
8. **Second cancel inside the backend arm's `_reap` skips
   reader/job teardown.** The tree is already dead by then (kill is
   issued synchronously before the first yielding await), so the
   residual is a pending-task/handle leak in the double-cancel
   window only. Documented by W3-IMPR-01 §self-review MINOR-1;
   I concur it is acceptable. MINOR-1.
   `[VERIFIED subprocess_backend.py:427-437]`

## 2. Claims audit

Brief checks (1)–(6), each executed or code-traced by me:

1. **Cancel during long bash returns <5s, tree dead.**
   `[RAN .venv/Scripts/python -m pytest tests/coder/test_cancellation.py tests/sandbox/test_cancellation.py tests/coder/test_loop.py tests/coder/test_shell_tools.py -v -> 44 passed, 7 skipped in 24.39s]`
   The two heartbeat tree tests assert `elapsed < 5.0` internally
   and sample the heartbeat file twice 1.5s apart after cancel;
   the heartbeat handshake (`first > 0`) makes them non-vacuous.
   `[RAN ... --durations=10 -> tree tests 8.28s/8.24s total (3s of
   that is the post-cancel sampling), coder tests ~0.11s each]`
   **VERIFIED**, on the Windows host (POSIX killpg path not run —
   no Linux host; code-symmetric, `start_new_session` at
   `subprocess_backend.py:342-347`).
2. **CancelledError re-raised not swallowed in loop; FAILED row
   journalled via existing `after()` under shield; recovery does
   NOT classify cancel as interrupted.**
   `[VERIFIED loop.py:293-302]` — shield + reraise as in finding 2.
   The real-ledger test asserts the exact row list
   `[TOOL_CALL_PROPOSED, TOOL_CALL_FAILED]`,
   `result_sha256 is None`, and `find_interrupted_calls(rows) == []`.
   `[RAN tests/coder/test_cancellation.py -> 5 passed]` **VERIFIED.**
3. **Backend arm mirrors TimeoutError (kill_tree+reap+reader
   cancel+job close) and re-raises, never returns a result.**
   `[VERIFIED subprocess_backend.py:412-437]` side-by-side with the
   TimeoutError arm at `:401-411`: same `_kill_tree`/`_reap`
   helpers (one killer, not a second implementation), plus the
   reader/job teardown the timeout path does not need because it
   proceeds to `_collect`. The re-raise is the last statement;
   `exit_code` is unbound on this path and never read.
   **VERIFIED.**
4. **Normal path unchanged.**
   `[RAN tests/coder/test_loop.py + test_shell_tools.py (in the
   44-passed run above) -> all green]`, including
   `test_normal_completion_is_unchanged` (COMPLETED + result hash)
   and `test_cancel_before_any_call_writes_no_outcome` (no forged
   outcome for a call that never started). **VERIFIED.**
5. **NEED-3 vocabulary debt honestly documented.**
   `[VERIFIED journal.py:545]` — literal `"tool failed"`, and
   `ToolJournal.after` carries no reason (`loop.py:501-503`).
   The loop comment (`loop.py:258-265`) names the frozen-vocabulary
   constraint and why FAILED is the only reachable cancel record;
   W3-IMPL-03 NEED-3 records the ADR-0046-v2 follow-up. This is
   design debt, not a defect in this task (changing it needs
   types+replay+journal+ADR, outside both write-sets).
   **VERIFIED as honestly documented, not a defect.**
6. **No mutation test weakened; real-ledger test fails pre-fix.**
   Sandbox: W3-IMPR-01 **executed** the revert (hand-reverted by
   hand, stash forbidden) — heartbeat grew `31 -> 61` bytes,
   `test_cancel_kills_the_tree_leaving_no_orphan` FAILED, garden
   `ResourceWarning` noise reproduced; fix restored byte-identical
   and green. I did not re-execute the revert (read-only mandate),
   but the recorded evidence is specific and matches the probe
   shape in W3-IMPL-03. Loop: **reasoned-only** — pre-fix, the
   inner `except Exception` cannot catch `CancelledError`, so
   `journal.after_calls` would be `[]` (test asserts
   `after_calls[0][1] is False`) and the real-ledger row list
   would be `[TOOL_CALL_PROPOSED]` only (test asserts the exact
   two-row list + `find_interrupted_calls == []`, whereas
   `recovery.py:403-406` would return the PROPOSED as interrupted).
   Either assertion fails pre-fix for the right reason.
   `[VERIFIED by code trace; loop revert NOT executed — reasoned-only]`

Additional gates:

- `[RAN .venv/Scripts/python -m ruff check src tests -> All checks passed!]`
- `[RAN .venv/Scripts/python -m mypy src -> Found 3 errors in 1 file,
  all subprocess_backend.py:574 (killpg/getpgid/SIGKILL attr-defined),
  line-shifted 548 -> 574 by the +26 insert, 0 new]`
- `[RAN .venv/Scripts/python -m pytest tests/conventions -q -> 34 passed]`
  (NEED-2 doc re-points landed in `d578a30`; conventions is green.)

**Docker-backend failures — confirmed pre-existing, not filed as
new findings.** `[RAN tests/sandbox/test_docker_backend.py -q -> 9
FAILED]`, all in untouched code: 1× `os.getuid` (POSIX-only) and
8× Windows path-separator expectations (`str(Path)` yields backslashes
vs `/` literals) plus the derived `--volume`/`HOME=` cases.
`git diff b8570a6..HEAD -- src/paa/sandbox/` shows only
`subprocess_backend.py` changed; `docker_backend.py` and
`test_docker_backend.py` are untouched since `b8570a6` (last touch
`4a0f761`). The brief said "4 pre-existing failures"; I count **9**
on this Windows host — the discrepancy is in the brief's count, not
in the code. All 9 are Windows path/getuid expectations in code
this task never touched. Recorded here; not new findings.

## 3. Long-run test (independent answers)

- **Source.** Both fixes are at the source: the loop seam (the
  outcome-less-PROPOSED gap) and the backend arm (the orphan gap)
  each live where the child/journal handles are owned. No
  callsite workarounds, no pid-guessing, no psutil. One shared
  tree-killer (`_kill_tree`) for timeout and cancel.
- **Load (100x, concurrency, partial failure).** Per-run,
  stateless, idempotent arms; concurrent cancels of distinct runs
  touch distinct handles. Store dedupe-by-discriminator makes a
  double FAILED write safe. NOT verified at load — only
  single-cancel runs were executed by implementer or reviewer.
- **Hostile input.** No new input surface: the cancel path takes
  no model-controlled strings; the log carries pid + first 3
  command items (same truncation as the timeout log); ledger
  preview/hash are computed from already-redacted args by the
  existing journal. No injection path added.
- **Operability (observe, roll back, migrate).**
  `sandbox.subprocess.cancelled` and `coder.bash.cancelled` make
  cancels observable; rollback is a revert of `eb9a68b`/`5caddcf`
  with no schema, event-type, or migration change; old/new
  binaries read each other's rows. POSIX behaviour is
  [ASSUMED by code symmetry] (killpg against the session leader).
- **Reversal (which test fails if the fix is reverted?).**
  Loop: `test_cancel_during_tool_run_journals_failed_and_reraises`
  + `test_cancel_writes_a_failed_row_to_the_real_ledger`
  (reasoned-only, §2.6). Sandbox: the two heartbeat tree tests +
  the ResourceWarning test (executed by W3-IMPR-01).

## 4. Findings

- **MINOR-1** `subprocess_backend.py:427-437` — second cancel inside
  the arm's `_reap` skips reader/job teardown (tree already dead;
  leak-only residual in the double-cancel window). Accepted,
  documented by the implementer; no action required for this task.
- **MINOR-2** `subprocess_backend.py:531-532` — `_collect` swallows
  a drain-phase cancel into `(b"", True)`; the loop would journal
  COMPLETED for a cancelled run whose process had already exited.
  Pre-existing; recorded as W3-IMPR-01 FINDING F1. Follow-up needs
  its own reviewed change (re-raise semantics are subtle there).
- **MINOR-3** `subprocess_backend.py:350-366` — cancel during spawn
  skips `job.close()` on Windows (handle leak, sub-ms window).
  Pre-existing; W3-IMPR-01 FINDING F2, one-liner deferred to the
  next sandbox touch.
- **MINOR-4** `loop.py:255-266` — cancel during `journal.before()`
  is outside the arm (no outcome written). Narrow transactional
  window; accepted residual, noted here for the record.
- **NIT-1** `tests/coder/test_cancellation.py:17-21` — module
  docstring still says the backend tree-kill is "verified by an
  external probe … filed as a NEED", which was true at `eb9a68b`
  but is stale now that `5caddcf` + `tests/sandbox/test_cancellation.py`
  verify it in-suite. Doc-only; not worth a code commit alone.

No BLOCKER, no MAJOR. No band-aids found: no swallowed errors
(both arms re-raise), no sleeps/retries to mask races (the only
sleeps are the test's cancel-delivery delay and heartbeat
sampling, both honest oracles), no weakened tests (every new test
asserts the fix's own behaviour), no type/lint suppression.

## 5. Attempts to break it

- **Cancel, then check the session transcript is not a lie:**
  the loop writes `TOOL_RESULT "cancelled by user"` (`loop.py:301`)
  before re-raising, so a transcript reader sees the cancel named,
  not a bare gap. Pinned by test. Held.
- **Cancel during ASK confirm / permission check:** inside the
  outer try (`loop.py:266-302`), so it journals FAILED and
  re-raises rather than hanging on the prompt. Held by code trace
  (no dedicated test — the arm's coverage is structural).
- **Cancel during the FAILED journal write itself:** `asyncio.shield`
  (`loop.py:300`) keeps the inner append alive through a second
  cancel; store dedupe makes a redelivered FAILED append a no-op.
  Held.
- **Cancel during timeout teardown:** the backend arm wraps the
  TimeoutError body too (`subprocess_backend.py:412` wraps `:401-411`),
  and `_kill_tree` early-returns on `returncode is not None`, so
  re-entry is idempotent. Held by code trace.
- **Return-a-result instead of raising (the COMPLETED trap):**
  both arms re-raise as their last act; the promptness tests'
  `pytest.raises(CancelledError)` would fail if either returned.
  Held.
- **Vacuous tree test (grandchild never started):** the handshake
  asserts `first > 0` before comparing samples, and
  `_wait_for_heartbeat` fails the test if the run completes before
  the cancel. Held — the test cannot pass by the grandchild never
  running.

## 6. Dimension checklist

Correctness ✓ | error handling ✓ (re-raise, suppress idiom matches
`_collect`) | security ✓ (no new input surface) | performance ✓
(cancel path is O(1) awaits; no polling added) | resource cleanup ✓
(readers, job, watchdog, reap all accounted; MINOR-1/3 residuals
are double-cancel/spawn windows only) | test quality ✓ (non-vacuous
heartbeat oracle, real-ledger end-to-end, boundary: cancel-before-
call and normal-completion pinned) | observability ✓ (two structured
log lines) | docs ✓ (loop comment + NEED-3 + NIT-1 staleness only).

## 7. Optimization and simplification opportunities

- DO-NOW: none.
- DO-LATER: (a) ADR-0046-v2 cancel vocabulary (NEED-3) — a
  `TOOL_CALL_CANCELLED` type or a reason parameter on
  `ToolJournal.after`; (b) the `_collect` re-raise fix (MINOR-2),
  which deserves its own reviewed change.
- NOT-WORTH-IT: shielding the backend arm's `_reap` — the tree is
  dead before any yielding await, so a shield loop would add
  complexity for no orphan-risk reduction. The implementer's
  plain-await choice mirrors the docker precedent and is right.

## 8. Cross-agent interactions

`eb9a68b` (W3-IMPL-03) and `5caddcf` (W3-IMPR-01) compose cleanly:
the backend re-raises, the bash tool logs and re-raises, the loop
journals FAILED and re-raises — each layer's contract is "cleanup,
never fabricate". NEED-1 is closed by `5caddcf`; NEED-2 by
`d578a30` (conventions green, verified above); NEED-3 remains open
by design. No interface changed across the seam: `SandboxResult`,
`ToolJournal`, and the ledger vocabulary are untouched, so both
commits are independently revertable.

## 9. What I could not verify

- POSIX behaviour of the backend arm (killpg path): no Linux host
  available; [ASSUMED by code symmetry] with `start_new_session`.
- The loop mutation by execution (read-only mandate): reasoned
  from the `BaseException` trace, not run. The sandbox mutation
  was executed by W3-IMPR-01, not re-executed by me.
- Load/concurrency: single-cancel runs only, by implementer and
  reviewer alike.
- Docker-backend container survival on cancel (W3-IMPR-01 F3):
  code-read only by the implementer; outside both write-sets.
- Full test suite: out of scope per standing orders §16; the
  scoped runs above are the brief's list.
