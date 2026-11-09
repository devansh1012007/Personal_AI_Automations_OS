# ORCH-DIGEST

STATUS: COMPLETE | agent W3-IMPR-01 | task N3.3-NEED-1 (wave 3 improve) | code commit 5caddcf | report docs-only pending | blockers: none

Closing NEED-1 from W3-IMPL-03: `SubprocessSandbox.run` now honours
`asyncio.CancelledError` by killing the process tree before re-raising —
mirroring the `TimeoutError` branch (`_kill_tree`, `_reap`), then
re-raising so the loop's FAILED journal write still happens (never a
result), and additionally cancelling the reader tasks and
terminating/closing the Windows Job Object so no task/handle leaks and
the Windows tree is reaped now rather than at host exit.
`tests/sandbox/test_cancellation.py` (new) reuses the heartbeat probe
method: a parent/grandchild pair keeps appending to a heartbeat file;
cancelling the run must stop that growth with no orphan.

## Context report (§1 investigation)

### 1. Root cause / entry points

- **Subprocess backend has no `CancelledError` path.** `SubprocessSandbox.run`
  (`src/paa/sandbox/subprocess_backend.py:395-413`) awaits
  `proc.wait()` (optionally via `asyncio.wait_for`) with only a
  `TimeoutError` arm (kill+reap) and a `finally` (watchdog stop). An
  external cancel (`task.cancel()` / Ctrl-C) unwinds through
  `proc.wait()`, skipping `_kill_tree`, `_reap`, the reader collection
  and even `job.close()` at :421-426. The child (new session / POSIX,
  `start_new_session=True` at :342-347; Windows Job Object `KILL_ON_JOB_CLOSE`
  only fires when the *host* exits) survives as an orphan. The W3-IMPL-03
  probe measured `ORPHAN_ALIVE` (heartbeat grew 210 -> 525 bytes for 2 s
  after cancel). `[VERIFIED subprocess_backend.py:395-426, 510-562;
  reports/w3-impl/W3-IMPL-03-N3-3.md:163-177]`
- **Docker precedent (code-read only).** `_docker_call`
  (`src/paa/sandbox/docker_backend.py:1380-1385`) catches
  `asyncio.CancelledError`, `await self._reap(proc)`, then re-raises —
  the same shape this fix needs for the subprocess backend, which
  additionally needs the tree-kill (`_kill_tree`) the docker client-reap
  does not. The Docker `container` survival is out of scope here (brief:
  do not touch docker_backend.py). `[VERIFIED docker_backend.py:1380-1385]`

### 2. Affected files / consumers

- Write-set only: `src/paa/sandbox/subprocess_backend.py`,
  `tests/sandbox/test_cancellation.py` (new).
- Consumer: `coder/shell_tools.py::BashTool.run` catches
  `asyncio.CancelledError` around `sandbox.run(spec)`, logs
  `coder.bash.cancelled` and re-raises (W3-IMPL-03 Phase 2). The backend's
  fix must re-raise (never return a `SandboxResult`) so the loop's FAILED
  journal write happens; returning a result would journal COMPLETED and
  hide the cancel. `[VERIFIED shell_tools.py — see W3-IMPL-03:111-118]`
- `_WindowsJobObject.terminate()`/`close()` are idempotent and
  suppress internally (:242-255) — safe to call from the cancel arm even
  after `_kill_tree` already terminated. `[VERIFIED
  subprocess_backend.py:242-255]`

### 3. Invariants preserved

- `CancelledError` is re-raised after cleanup — never swallowed or
  converted to a `SandboxResult`.
- TimeoutError arm and normal completion paths are unchanged.
- Reader tasks + Job Object + watchdog must not leak on the cancel path
  (child-tree kill alone would leave pending tasks and, on Windows, the
  job handle — plus unclosed-transport `ResourceWarning` noise seen in
  the W3-IMPL-03 probe).
- The `finally: await watchdog.stop()` keeps running on the cancel path.

### 4. Risks / edge cases

- **Cancel during timeout teardown.** The `CancelledError` arm wraps the
  whole wait region, including the `except TimeoutError` body: a cancel
  arriving mid-`_kill_tree`/`_reap` re-enters the arm, which is
  idempotent (`_kill_tree` early-returns on `proc.returncode is not None`;
  killpg/taskkill/proc.kill are suppress-guarded) and completes the
  remaining cleanup. No double-kill hazard.
- **Second cancel during cancel-cleanup.** POSIX `_kill_tree` never
  yields (killpg is synchronous) and Windows `job.terminate()` runs
  synchronously before the only suspending step (`taskkill` wait), so the
  tree is already dead before any interruptible await. The only yielding
  cleanup step is `_reap`'s `proc.wait()` on an already-killed process —
  an interrupted wait there is a benign, init/GC-reaped zombie, not an
  orphan. Plain awaits mirror the `TimeoutError` arm and the docker
  precedent; no shield-loop needed.
- **Cancel during drain (`_collect`) or during `watchdog.stop()`'s
  internal suppress.** Exotic windows after the process already exited
  (tree already dead). `_collect`'s existing `except CancelledError:
  return b"", True` (:505) can convert a cancel into a result in that
  window. Out of this brief's tree-kill scope — recorded as a FINDING,
  not silently fixed.
- **Cancel before/during spawn** (`create_subprocess_exec` at :350): no
  child exists yet, so there is no tree to kill; the pre-existing
  `except (OSError, ValueError)` skips `job.close()` on that path (handle
  leak on Windows). Out of scope — FINDING.

### 5. Expected behavior + required tests (`tests/sandbox/test_cancellation.py`)

- Cancel a long-running run: `CancelledError` re-raised, promptly (<5 s),
  child tree verified dead via heartbeat probe (file stops growing),
  no orphan leak (non-vacuous: heartbeat > 0 before cancel). Both wait
  shapes: bounded (`timeout_seconds=60`, the `wait_for` arm) and unbounded
  (`timeout_seconds=None`, the bare `await proc.wait()` arm) — two probe
  runs cover both code paths.
- Promptness + re-raise pinned by a fast non-heartbeat test (sleep 60,
  cancel at 0.3 s, assert `CancelledError`, elapsed < 5 s).
- TimeoutError arm and normal completion: covered by existing
  `tests/sandbox/test_subprocess_backend.py` (must stay green).

## Phased changes (append as implemented)

### Phase 1 — the `CancelledError` arm in `run()` (done)

`src/paa/sandbox/subprocess_backend.py` (+26 lines): an
`except asyncio.CancelledError` arm wrapping the whole wait region —
including the `TimeoutError` handler body, so a cancel landing *during*
timeout teardown re-enters the arm and is idempotent. The arm mirrors the
TimeoutError branch (`_kill_tree`, `_reap`), additionally cancels+awaits
the two reader tasks, terminates/closes the Job Object, then re-raises.
Key decisions:

- **Re-raise, never a result.** Returning a `SandboxResult` would let
  the coder loop journal COMPLETED and hide the cancel. Re-raising keeps
  `ShellTool`'s `coder.bash.cancelled` log + the loop's FAILED journal
  write reachable (W3-IMPL-03's seam).
- **Reader/job teardown in the arm.** The tree-kill alone would leave
  two pending reader tasks and, on Windows, the job handle (whose
  `KILL_ON_JOB_CLOSE` only fires at host exit — the brief requires the
  arm to reap *now*, not wait for exit). `job.terminate()`/`close()` are
  idempotent and self-suppressing (`:242-255`), safe after `_kill_tree`
  already terminated. Reader awaits use `_collect`'s exact
  `suppress(CancelledError, Exception)` idiom.
- **Plain awaits, no shield-loop** (mirrors the TimeoutError arm and the
  docker precedent at `docker_backend.py:1380-1385`). Justification:
  the tree-kill precedes the first yielding await in the arm — POSIX
  `_kill_tree` never yields at all (synchronous `killpg`), and on
  Windows `job.terminate()` is a synchronous ctypes call that runs first
  (taskkill is second). By any point a second cancel could be delivered,
  the tree is already dead; the only subsequent yielding step is
  `_reap`'s wait on an already-killed process.

### Phase 2 — `tests/sandbox/test_cancellation.py` (new, 4 tests)

Two heartbeat-probe tests (bounded `timeout=60` covering the
`wait_for(proc.wait(), …)` arm; unbounded `timeout=None` covering the
bare `await proc.wait()` arm) reusing the probe's oracle exactly —
parent/grandchild scripts with the first-byte handshake (mirrors
`test_subprocess_backend.py:190-257`), cancel, then two `asyncio.sleep`
samples 1.5 s apart; the file must stop growing. One fast test pins
re-raise + promptness on a plain sleeper. One fast test asserts no
`ResourceWarning` after cancel (unreaped transports warn at GC).

## Self-review audit (append)

- **Correctness / root cause.** The arm is at the source: `run()` now
  owns its cancelled-run cleanup instead of leaving it to callers. Tree-kill
  on cancel goes through the same `_kill_tree` the timeout path uses —
  one killer, not a second competing implementation; no pid-guessing, no
  psutil. The arm wraps the TimeoutError-handler body too, so a cancel
  landing *during* timeout teardown re-enters and is idempotent.
- **CRITICAL:** none. `CancelledError` re-raises, so no caller can
  mistake a cancel for completion; `kill_tree`/`reap` are idempotent
  (`returncode` guard + suppresses), and `job.terminate/close` are
  idempotent self-suppressing calls.
- **MAJOR:** none. `exit_code`/`timed_out`/`killed_reason` are never
  read on the cancel path (re-raise before the result build); mypy
  confirms no unbound-variable or type issue in the arm.
- **MINOR (accepted, documented).** (1) A *second* cancel delivered
  inside the arm's `_reap` wait skips the reader/job teardown below it —
  the tree is already dead by then (kill is issued synchronously before
  the first yielding await); residual is a task/handle leak in the
  double-cancel window only. (2) Reader-teardown completeness is
  structural: no test asserts "no pending tasks" (no non-flaky oracle
  without reaching into loop internals); the `ResourceWarning` test
  covers the observable half (unreaped transports).
- **Mutation mental check — executed, not assumed.** I reverted the arm
  by hand (stash/checkout are forbidden), re-ran
  `test_cancel_kills_the_tree_leaving_no_orphan` -> FAILED with
  `heartbeat grew from 31 to 61 bytes after the cancel — an orphaned
  grandchild survived the cancel path`, plus unclosed-transport
  `ResourceWarning` noise (exactly the W3-IMPL-03 probe's stderr
  fallout). The orphan was then reaped at pytest exit by the Job
  Object's `KILL_ON_JOB_CLOSE` — independently confirming the brief's
  "fires only at host exit" claim. Restored the fix byte-identical
  (`Compare-Object`: no differences) and re-ran green. If the arm
  returned a result instead of raising, all four tests' `pytest.raises`
  would fail. Reversal set: `test_cancel_kills_the_tree_leaving_no_orphan`
  and `test_cancel_kills_the_tree_without_a_timeout` (heartbeat grows),
  `test_cancel_reaps_the_process_without_resource_warnings`
  (unclosed-transport warnings of the reverted run). The promptness test
  passes on both and pins behaviour the arm must not disturb.

## Verification evidence (append)

- [RAN .venv/Scripts/python.exe -m pytest tests/sandbox/test_cancellation.py -q -> 4 passed]
- [RAN .venv/Scripts/python.exe -m pytest tests/sandbox/test_cancellation.py tests/sandbox/test_subprocess_backend.py -q -> 25 passed, 1 skipped (win32 POSIX-PATH contract skip, same as baseline)]
- [RAN .venv/Scripts/python.exe -m pytest tests/coder/test_cancellation.py -q -> 5 passed (loop seam unaffected, still green)]
- [RAN .venv/Scripts/python.exe -m pytest tests/sandbox/test_cancellation.py::TestCancelKillsTheTree::test_cancel_kills_the_tree_leaving_no_orphan -q (reverted code) -> 1 FAILED, heartbeat 31 -> 61 bytes = ORPHAN_ALIVE (mutation check; fix re-applied and green)]
- [RAN .venv/Scripts/python.exe -m ruff check src/paa/sandbox/subprocess_backend.py tests/sandbox/test_cancellation.py -> All checks passed!]
- [RAN .venv/Scripts/python.exe -m mypy src -> Found 3 errors in 1 file (checked 149 source files), all pre-existing Windows killpg/getpgid/SIGKILL attr errors in _kill_tree (untouched code), line-shifted 548 -> 574 by the +26 insert; 0 new]
- [RAN .venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q -> 1 FAILED: 3 blank cites, ALL in loop.py (coder.md:100/109, MCP_AND_PLUGINS.md:80) = W3-IMPL-03's NEED-2, pre-existing and unrelated (this diff touches no coder files; grep verifies no non-excluded doc cites a shifted subprocess_backend.py line)]

Not run (out of scope, per standing orders §16): full test suite,
container builds, POSIX behaviour (no Linux run available on this
Windows host).

## Long-run test

- **Source.** The fix is at the source: `run()`'s wait region now has a
  complete teardown shape (timeout/cancel/complete all accounted), not
  a callsite workaround. One tree-killer (`_kill_tree`) shared by
  timeout and cancel.
- **Load (100x, concurrency, partial failure).** Cancel arms are
  per-run, stateless, and idempotent; concurrent cancels of distinct
  runs touch distinct handles. Partial failures (killpg
  `ProcessLookupError` because the tree exited first, taskkill absent,
  Job Object creation failed → `job_active` False with watchdog-only)
  are handled inside `_kill_tree`'s existing suppresses and are shared
  unchanged. NOT verified at load — single-cancel runs only.
- **Hostile input.** No new input surface: the arm takes no workload
  strings; the log carries `pid` and the first 3 command items (same
  truncation as the timeout log). No injection path added.
- **Operability (observe, roll back, migrate).** `sandbox.subprocess.cancelled`
  (structured, pid + command prefix) makes cancels observable; rollback
  is a single-file revert with no schema/event/migration change; ledger
  vocabulary unchanged. POSIX behaviour is [ASSUMED by code symmetry]:
  killpg against a session leader (`start_new_session=True`) is the
  documented strongest-first mechanism #3; untested on a POSIX host.
- **Reversal (which test fails if the fix is reverted?).**
  `test_cancel_kills_the_tree_leaving_no_orphan` (heartbeat grows —
  proven by execution above, 31 → 61 bytes),
  `test_cancel_kills_the_tree_without_a_timeout` (same failure shape on
  the bare-`wait()` arm), and
  `test_cancel_reaps_the_process_without_resource_warnings`
  (unclosed-transport warnings of the reverted run).
  `test_cancel_reraises_promptly_without_a_result` passes on both and
  pins behaviour the arm must not disturb.

## Out-of-scope findings / NEEDs (append)

- FINDING F1 (follow-up, `_collect` drain-phase swallow; same file,
  pre-existing): a cancel landing during `_collect` (post-exit drain,
  :505) returns `(b"", True)` instead of propagating, so `run()` returns
  a `SandboxResult` and the loop would journal COMPLETED — the cancel is
  hidden, though the returned exit code is real (process had exited), so
  the harm is a missed cancel, not a fabricated result. Exact re-raise
  semantics there are subtle (the `reader.cancel()` + suppress pattern
  makes an external cancel indistinguishable from the reader's own), so
  this deserves its own reviewed change; the tree is dead in that
  window, so no orphan risk.
- FINDING F2 (follow-up, spawn-window job handle; same file,
  pre-existing): a cancel during `create_subprocess_exec` (:350) skips
  `job.close()` (`except (OSError, ValueError)` doesn't cover
  `CancelledError`) — a Windows job-handle leak in a sub-millisecond
  pre-child window. One-liner fix (`except BaseException: job.close();
  raise`); left out to keep this diff minimal, flagged for the next
  sandbox touch.
- FINDING F3 (not mine, docker backend — recorded by W3-IMPL-03):
  `_docker_call` reaps the `docker` *client* on cancel but leaves the
  container running (`docker_backend.py:1380-1385`). Still open;
  outside this brief's write-set (docker_backend.py forbidden), so a
  NEED to the docker owner.
- NEED check: **NEED-1 (W3-IMPL-03) is CLOSED by this task** (arm +
  tests above). W3-IMPL-03 NEED-2 (3 loop.py doc cites) remains open —
  conventions stays red until the docs owner re-points; my change adds
  no new broken cites.

## Git state (append)

- Branch: `ops/20261109-next-spec` (verified before committing).
- Code commit: `5caddcf` on `ops/20261109-next-spec` —
  `src/paa/sandbox/subprocess_backend.py`,
  `tests/sandbox/test_cancellation.py` (explicit paths).
- Report commit: this file (docs-only commit listing code commit `5caddcf`).
- COMMS.md: appended START + DONE lines, **left uncommitted** (only
  ORCH commits COMMS at wave boundaries, COMMS.md rule 8); the file
  also carries other agents' appended lines — not mine, untouched.
- Sibling working-tree changes (briefs, gates, uv.lock, untracked docs)
  are not mine and were not touched. No probe artifacts in the working
  tree (mutation-check orphan reaped by Job Object at pytest exit;
  verified no `grandchild.py`/`parent.py` python processes remain).
