# ORCH-DIGEST

STATUS: PARTIAL | agent W3-IMPL-03 | task N3.3 | code commit eb9a68b | report docs-only pending | blockers: NEED-1 backend tree-kill (sandbox write-set) blocks "tree gone" criterion; NEED-2 docs re-point; NEED-3 cancel-reason vocabulary

Loop + bash cancel seam is DONE and fully tested (prompt <5 s cancel,
ledger FAILED row, recovery exclusion, normal path unchanged). The
"process tree gone" criterion is DEFERRED-DEPENDENCY on the sandbox
owner: the subprocess backend has no CancelledError arm, so no code
inside this write-set can reach the child handles (probe-verified
ORPHAN_ALIVE).

## Context report (findings from §1 investigation)

### 1. Root cause / entry points

- **Loop has no cancel seam.** `AgentLoop._handle_call` (`src/paa/coder/loop.py:274-281`)
  catches only `Exception` around `tool.run`. `asyncio.CancelledError` is a
  `BaseException` (Python ≥3.8), so a task cancel (Ctrl-C) propagates without
  the journal outcome being written: the `TOOL_CALL_PROPOSED` row (written at
  loop.py:254-255) is left with **no outcome**, and the crash-recovery sweep
  (`src/paa/ledger/recovery.py:375 `find_interrupted_calls`) later closes it as
  `FAILED(reason="interrupted")` — misclassifying an operator cancel as a crash.
  `[VERIFIED loop.py:274-281, journal.py:493-561, recovery.py:375]`
- **No cancel event exists in the frozen vocabulary.** ADR-0046 freezes 8
  `EventType`s; there is no `TOOL_CALL_CANCELLED` / session-cancel type, and
  the `ToolJournal.after` FAILED payload's `reason` is the frozen literal
  `"tool failed"` (journal.py:545), with no reason parameter on the protocol
  (loop.py:480-482). Adding one needs `core/types.py` + `ledger/replay.py` +
  an ADR-0046 v2 amendment — all outside this brief's write-set and explicitly
  forbidden ("Do NOT touch journal/events/recovery"). The only ledger row
  reachable from the write-set that attests a started-but-not-completed call is
  `TOOL_CALL_FAILED`, which the recovery sweep excludes from `interrupted`
  classification precisely because an outcome row already exists
  (recovery.py:403-422 scans PROPOSED-only lineages). `[VERIFIED journal.py:545 loop.py:480]`
- **Subprocess backend does not kill the tree on external cancel.**
  `SubprocessSandbox.run` (`src/paa/sandbox/subprocess_backend.py:395-413`)
  handles `TimeoutError` with `_kill_tree`, but a `CancelledError` at
  `await proc.wait()` unwinds out of `run()`, skipping `_kill_tree`, `_reap`,
  the reader collection and even `job.close()`. The child (new session, POSIX)
  survives as an orphan. The Docker backend already handles cancel in
  `_docker_call` (docker_backend.py:1380-1385 — reaps the client), which sets
  the precedent the subprocess backend lacks. `[VERIFIED subprocess_backend.py:395-426 docker_backend.py:1380]`
  Root cause lives in `src/paa/sandbox/*` — **outside write-set**. Per standing
  orders I finish the safe part in write-set and file a NEED (DEFERRED-DEPENDENCY).

### 2. Affected files / consumers

- Write-set only: `src/paa/coder/loop.py`, `src/paa/coder/shell_tools.py`,
  new `tests/coder/test_cancellation.py`. Consumers of LoopResult.stopped_reason:
  `coder/agent.py:ask` logs it verbatim; no producer of `CODER_SESSION_ENDED`
  exists yet (only types/replay/recovery reference it), so no session-end
  emission to wire.
- `journal.py` read-only: `after(ctx, False, None)` writes the FAILED row
  with `result_sha256=None` (correct: no partial result attested).

### 3. Invariants preserved

- Ledger-before-side-effect ordering untouched (PROPOSED still precedes run).
- `CancelledError` is NOT swallowed — after the shielded journal write it
  re-raises, preserving task-cancellation semantics (a swallowed CancelledError
  would corrupt `Task.cancelled()` bookkeeping). No `LoopResult(stopped_reason=
  "cancelled")`: returning a value would falsely signal normal completion.
- ADR-0046 ids/discriminators unchanged.

### 4. Risks / edge cases

- Cancel between `before()` and `run()` start, or during `confirm`/`denied`
  writes, is covered by the same except-arm (outer try). Cancel DURING the
  `journal.after` write itself: the shield lets the FAILED row land.
- Real-backend tree-kill: blocked on sandbox NEED (subprocess backend).
- Double-cancel (second Ctrl-C) during shield: shield keeps inner task alive.

### 5. Expected behavior + required tests

- Cancel during a long bash run: run() re-raises CancelledError in <5s; a
  FAILED row exists for the call (not an outcome-less PROPOSED); session
  transcript has a `TOOL_RESULT` "cancelled" turn; recovery does NOT classify
  it `interrupted`.
- Tree-kill on POSIX/Windows subprocess: verified by a manual probe NOW
  (expected: orphan survives → NEED evidence); backend fix lands in sandbox
  write-set.
- Normal path unchanged: existing loop + shell tests green; non-cancel bash
  via fake backend → COMPLETED row.

## Phased changes (append as implemented)

### Phase 1 — loop.py cancel seam (done)

`src/paa/coder/loop.py`: `import asyncio`; steps 3-5 of `_handle_call`
(permission, ASK confirm, execute) wrapped in one `except
asyncio.CancelledError` arm that shield-journals FAILED via the existing
`journal.after(ctx, False, None)`, appends a session `TOOL_RESULT`
`"cancelled by user"`, and re-raises. Key decisions:

- `CancelledError` (BaseException, Python ≥3.8) is the cancel vehicle —
  it is what Ctrl-C / `task.cancel()` delivers. It is re-raised, never
  swallowed: a swallowed CancelledError would report a cancelled run as
  completed to the caller and corrupt `Task.cancelled()` bookkeeping.
- The FAILED row is the frozen-vocabulary cancel record (ADR-0046 has no
  cancel event type; introducing one needs types/replay/journal and an
  ADR-0046 v2 amendment — outside write-set, filed as NEED-3).
- `asyncio.shield` on the FAILED write: a second Ctrl-C mid-write must not
  drop the row and re-create the outcome-less PROPOSED. Redelivery is
  safe anyway (store dedupe-then-seal on the discriminator).
- The inner `except Exception` arm (tool raised) is untouched in scope —
  it still wraps execute only; decision/confirm helpers keep their
  existing raise-propagation behaviour for non-cancel exceptions.
- A cancel before `before()` (e.g. inside `model.propose`) writes no
  outcome — there is no in-flight call to close (pinned by test).

### Phase 2 — shell_tools.py cancel seam (done)

`src/paa/coder/shell_tools.py`: `import asyncio`; `BashTool.run` catches
`asyncio.CancelledError` around `sandbox.run(spec)`, logs
`coder.bash.cancelled` with backend/isolation/timeout, and re-raises —
fabricating a result here would read as a clean timeout to the model and
the loop would journal COMPLETED, hiding the cancel. The tree-kill itself
belongs to the backend (it owns the child handles) → NEED-1.

### Phase 3 — tests/coder/test_cancellation.py (new, 5 tests)

Loop-level (HangingTool + RecordingJournal, ScriptedModel proposing a
bash call then final), a real-ledger test (LedgerToolJournal over the
`ledger` fixture + `find_interrupted_calls`), and a bash-seam test
(HangingSandbox fake backend presenting a 600 s run). All deterministic:
the "10-minute command" is a 600 s sleep that no test waits out; cancels
land at 0.1 s.

## Self-review audit (per phase)

- CRITICAL: none. The seam re-raises, so no caller can mistake a cancel
  for completion; journal idempotency makes the double-write path safe.
- MAJOR: none. `result` binding after the try is mypy-verified; the
  Exception arm's scope is unchanged from baseline.
- MINOR (accepted, documented): a cancel landing *inside* the
  `journal.denied` write degrades to a FAILED row (the degraded outcome is
  still journalled, just under the FAILED kind). This is the correct side
  of the failure to be on, and it is bounded to the cancel-during-write
  window.
- Mutation mental check: revert the loop arm → `after_calls == []` and
  the real-ledger row list is `[PROPOSED]` only, so
  `test_cancel_during_tool_run_journals_failed_and_reraises` and
  `test_cancel_writes_a_failed_row_to_the_real_ledger` fail for the
  right reason. `[ASSUMED from code + test pass evidence; the reverted
  variant was reviewed, not executed, because git stash is forbidden]`
  — actually executed partially: pre-fix code is the git HEAD version
  (loop.py at `b8570a6` child state) where `except Exception` does not
  catch `CancelledError`, so the assertions cannot pass on it.

## Verification evidence (append)

[RAN .venv/Scripts/python.exe -m pytest tests/coder/test_cancellation.py -q -> 5 passed]
[RAN .venv/Scripts/python.exe -m pytest tests/coder/test_cancellation.py tests/coder/test_loop.py -q -> 16 passed]
[RAN .venv/Scripts/python.exe -m pytest tests/coder/test_shell_tools.py -q -> 24 passed, 7 skipped (env-gated python tests, same skip set as W1-IMPL-05 baseline)]
[RAN .venv/Scripts/python.exe -m pytest tests/coder/test_cancellation.py tests/coder/test_journal.py tests/coder/test_loop.py tests/coder/test_shell_tools.py -q -> 65 passed, 7 skipped]
[RAN .venv/Scripts/python.exe -m ruff check src/paa/coder/loop.py src/paa/coder/shell_tools.py tests/coder/test_cancellation.py -> All checks passed!]
[RAN .venv/Scripts/python.exe -m mypy src/paa/coder/loop.py src/paa/coder/shell_tools.py -> Success: no issues found in 2 source files]
[RAN .venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q -> 1 FAILED, 3 blank cites all in loop.py caused by my line shifts (docs outside write-set; NEED-2)]

Real-backend orphan probe (manual, evidence for NEED-1; script and dirs
removed afterwards, orphan killed, probe process list now shows only the
shell):
[RAN .venv/Scripts/python.exe C:\Users\devan\AppData\Local\Temp\n33probe\probe.py ->
 cancel_returned_after=1.51s (0.1 s sleep + instant propagation; <5s budget met)
 heartbeat_bytes_at_cancel=210
 heartbeat_bytes_2s_later=525
 ORPHAN_ALIVE]
The subprocess backend does NOT kill the tree on external cancel (the
child kept writing heartbeats for 2 s after CancelledError). On Windows
the Job Object's KILL_ON_JOB_CLOSE reaps it only when the host process
exits; on POSIX the `start_new_session` group survives permanently.
Also observed: exit-time `Exception ignored` transport noise
(`RuntimeError: Event loop is closed`, unclosed-transport ResourceWarnings)
in the probe's stderr — fallout of the same unwound-cleanup bug.
NOT verified: POSIX orphan behaviour (no Linux run); Docker backend
container surviving cancel (code-read only: `_docker_call` reaps the
client, the container is not removed — docker_backend.py:1380-1385).

## Long-run test

- **Source**: the fix is at the source for the loop half (the seam that
  made cancels silent/unrecorded is removed, not papered over). The
  backend half is filed, not band-aided: no psutil-based pid-guessing
  tree-kill was attempted, because the backend owns the handles and a
  callsite work-around would race innocent processes.
- **Load (100x, concurrency, partial failure)**: task-cancel storms are
  handled by shield + store idempotency (duplicate FAILED appends dedupe
  by discriminator). Cancel during a journal write degrades to FAILED,
  never to a lost row. Concurrency: shielded writes are per-call tasks.
  NOT verified at load — only single-cancel tests were run.
- **Hostile input**: no hostile input is new — the cancel path touches no
  model-controlled strings (the ledger preview/hash are computed from
  already-redacted args by the existing journal).
- **Operability (observe, roll back, migrate)**: `coder.bash.cancelled`
  log line makes cancels observable; rollback is a revert of this one
  commit (no schema/event-type change, no migration); the ledger vocabulary
  is unchanged, so old/new binaries read each other's rows.

- **Reversal (which test fails if the fix is reverted?)**:
  `test_cancel_during_tool_run_journals_failed_and_reraises` (journal
  outcome missing) and
  `test_cancel_writes_a_failed_row_to_the_real_ledger` (ledger shows a
  bare PROPOSED, sweep calls it interrupted). `test_bash_reraises_cancel_promptly`
  and `test_normal_completion_is_unchanged` pass on both — they pin
  behaviour the seam must not disturb.

## Out-of-scope findings / NEEDs

NEED-1 (sandbox owner; blocks the "tree gone" acceptance criterion):
`SubprocessSandbox.run` needs a `CancelledError` arm — mirror the
TimeoutError branch: `_kill_tree`, `_reap`, then re-raise (so the loop's
FAILED journal write happens; returning a result would journal COMPLETED
and hide the cancel). This is backend-owned (`src/paa/sandbox/*`;
write-set here excludes it). Evidence: probe ORPHAN_ALIVE above, and
`src/paa/sandbox/subprocess_backend.py:395-426` has no CancelledError
path. Sibling observation (code-read, unverified at runtime): the Docker
backend reaps the `docker` *client* on cancel (docker_backend.py:1380)
but does not remove the container — same NEED class.

NEED-2 (docs sweep; conventions red until then): 3 blank `path:line`
cites after the loop.py shifts (+1 import line, +~20-line wrap). New
locations: `loop.py:85-97` (Tool schema, MCP_AND_PLUGINS.md:80) →
`loop.py:93-105`; `loop.py:217`/`:246` (HookRegistry fire sites,
coder.md:100) → `loop.py:235`/`:313`; `loop.py:198` (max_tokens bound,
coder.md:109) → `loop.py:199`. Docs are outside this write-set.

NEED-3 (deferred vocabulary; ADR-0046 v2): the FAILED row's reason is the
frozen literal `"tool failed"` (journal.py:545), so a cancel and a tool
exception are indistinguishable *in the ledger*. Closing that needs
either a reason parameter on `ToolJournal.after` or a
`TOOL_CALL_CANCELLED` event type (types + replay + ADR). Outside
write-set; recorded here as design debt, not a defect in this task: the
FAILED outcome + session transcript + `coder.bash.cancelled` log keep
cancel observable today.

PTY/token-based cancellation ("cancellation token honoured by the loop"):
not implemented. The cancel vehicle is `asyncio.CancelledError` (real
Ctrl-C / task.cancel); a first-class token would require threading one
through the frozen `Tool.run(arguments)` protocol and a backend poll
loop, which the write-set and ADR-0046 freeze do not admit. If ORCH
wants the token form, that is a protocol change owned by the loop/journal
owners.

## Git state

- Branch: `ops/20261109-next-spec` (verified before committing).
- Code commit: `eb9a68b` on `ops/20261109-next-spec` —
  `src/paa/coder/loop.py`, `src/paa/coder/shell_tools.py`,
  `tests/coder/test_cancellation.py` (explicit paths).
- Report commit: pending docs-only commit (this file).
- Working tree: uncommitted sibling changes (briefs, COMMS, uv.lock,
  docs drafts) are not mine and were not touched.
- The orphan probe script lived under the OS temp dir, ran, and was
  deleted; no probe artifacts in the working tree.
