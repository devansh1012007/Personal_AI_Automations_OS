<!-- ORCH-DIGEST:START -->
agent: W5-IMPL-02 | task: T-C5-2 (C5.2) | status: DONE
code_commits: none (orchestrator commits after review) | report_commit: pending
files_changed: 2 (docs/modules/sandbox.md + this report; no source or test edits —
  the session implementation and its 21 tests were already in-tree from the
  implementing run this task audits, and the audit found them green)
contracts_changed: none (this task is the C5.2 audit + evidence report; the
  SandboxSession/open/read/write/close/run_within contract itself was landed by
  the implementing run; my doc edit documents the contract, it does not alter it)
tests: added 0, changed 0, removed 0; scoped runs all green (sandbox 201,
  test_session 21 incl. the 3 acceptance tests re-run by node id, conventions
  docs-references + markdown-links 13, ruff src+tests clean, mypy 173 files)
ownership_violations: none (write-set per brief: src/paa/sandbox untouched,
  tests/sandbox untouched, docs/modules/sandbox.md + this report only)
deferred_dependencies: none new (out-of-scope findings recorded below with
  dispositions; nothing deferred onto a sibling)
out_of_scope_findings: 5 (cpu_cores enforced only by docker/WSL, not subprocess —
  honest absence, no docstring claims otherwise; run_within's own-process-group
  asymmetry documented in-tree; stale README build-status note in sandbox.md
  known-gaps, README outside write-set; W5-IMPL-01's blank-cite + ASYNC109
  findings re-checked and found already green — their loop closed here)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: the reading of the spec row's "rlimits" — the
  implementation has *no* POSIX setrlimit anywhere (deliberately, with the
  reason documented in the docstring); the verdict "resource limits are
  genuinely applied, via Job Object/watchdog/wall-clock" is an interpretive
  call I defend with the docstring's own reasoning, but it is an
  interpretation of the row's wording
confidence: high (every contract element backed by line-level reading plus a
  green scoped run executed myself; all three acceptance tests re-run by name)
last_comms_line_read: 2026-11-22T00:00:00Z (WAVE-START)
<!-- ORCH-DIGEST:END -->
# SandboxSession audit — W5-IMPL-02 — T-C5-2 (C5.2), wave 5, 2026-11-22

Task: spec `docs/plans/2026-11-21-next-spec-2.md` §4 row **C5.2** — audit the
`SandboxSession` implementation (`src/paa/sandbox/base.py`,
`subprocess_backend.py`, `docker_backend.py`) against the row's contract
(*duplex, long-lived: `open(spec) -> session` with `read/write/close`,
rlimits, wall-clock, process-tree kill, `run_within(session, spec)` for
one-shots; one-shot `run()` kept as a thin wrapper*), verify the row's three
acceptance tests in `tests/sandbox/test_session.py`, fix only real gaps, and
write this evidence report. The implementing run finished the code and the
tests but ran out of context before the report; this task is the audit and
the report. No commit is made here; the orchestrator commits after review.

## 1. Method

- Read `AGENTS.md` first (repo rules), then the spec row, then the sibling
  report format (`W5-IMPL-01-hygiene.md`) this file mirrors.
- Read the implementation end-to-end, not by landmark trust: `base.py` (930
  lines), `subprocess_backend.py` (1396), the `open` refusals in
  `docker_backend.py` / `wsl_backend.py` / `dryrun_backend.py`, `watchdog.py`
  (the POSIX enforcement), `_win32.py` (the Windows door), `__init__.py`
  (exports/factory), and the whole of `tests/sandbox/test_session.py` (747).
- Ran everything I claim: the full sandbox suite, the three acceptance tests
  *by node id*, ruff, mypy, and the docs citation lock (before and after my
  one edit). Verified each cited line's content mechanically (see §4).
- Confirmed via read-only `git diff HEAD` that the wave's write-set is what
  the brief says it is, and used it to separate the wave's new claims from
  pre-existing ones (the "mandatory AST pre-scan" sentence in `base.py:25`
  is pre-existing and untouched by this wave — judged, not re-authored).

## 2. Per-contract-element verification (the C5.2 row, element by element)

| # | Contract element | Verdict | Landmarks (file:line, all read) | Witness tests (all re-run) |
|---|---|---|---|---|
| a | Duplex long-lived session: `open(spec) -> session` with `read`/`write`/`close` | **VERIFIED** | ABC `SandboxSession` `base.py:491` (abstract `read` `:544`, `write` `:564`, `close` `:577`); `Sandbox.open` abstract `:753`; impl `SubprocessSandbox.open` `subprocess_backend.py:1010` — spawn with `stdin=PIPE` `:1068-1080` + `start_new_session=True` `:1059-1065`; `read` `:428-449` (one 64 KiB pipe-read, `b""`=EOF, `TimeoutError` on an empty wait only); `write` `:451-469` (`stdin.drain()` = real backpressure, typed error on a gone pipe); `close` `:471-493` (idempotent under `_close_lock`, returns the final `SandboxResult`) | `TestDuplexEcho::test_lines_written_are_echoed_back_under_the_resource_envelope`; `TestReadSemantics` ×4 |
| b | rlimits actually applied to the child | **VERIFIED — as Job Object (Windows) + ResourceWatchdog (POSIX) + wall-clock; there is no setrlimit, deliberately** | The brief said "find the preexec/setrlimit site": there is **none** in `src/paa` (grep over the tree). The ceiling is applied at spawn: `_WindowsJobObject` (kernel `VirtualAlloc`-time refusal, `PROCESS_MEMORY`+`JOB_MEMORY`+`KILL_ON_JOB_CLOSE`+`ACTIVE_PROCESS` flags) `subprocess_backend.py:161-284`, created `:1056-1057`, assigned `:1090-1091`; POSIX `ResourceWatchdog` `:1096-1106` with `on_breach` killing the **whole tree** (`_note_memory` `:589-598`), started in `_start` `:399-415` (watchdog first — the docstring records the audit that once found it missing). The absence is a documented decision, not a gap: `:303-307` — `preexec_fn` is unsafe in a threaded parent and the session must not drift from `run()`'s containment story. (The one place real rlimits *are* applied is the WSL one-shot: `ulimit -v/-t/-c 0`, `wsl_backend.py:254-259` — whose session is refused, see (g).) | `TestMemoryCeiling::test_a_breaching_child_is_killed_by_the_envelope_not_the_clock` (`killed_reason="memory"`, peak reported, wall-clock set far beyond the breach); `TestDuplexEcho` runs under `memory_mb=64` |
| c | Wall-clock timeout honoured | **VERIFIED** | `_deadline` `:550-567` — a **lifetime** budget from open (silence ≠ idleness), `None` arms nothing; armed in `_start` `:412-415`; the wrapper's `_await_exit_under_budget` `:1201-1230` uses the spec's own number (pinned by `test_the_wait_is_bounded_by_the_work_budget_alone`) and races the deadline idempotently | `TestWallClockDeadline::test_an_idle_session_tree_times_out` (deadline the only actor; tree, not just root); `TestRunIsStillASessionWrapper::test_run_with_no_wall_clock_waits_for_the_work` (`None` = wait forever, not zero) |
| d | Process-tree kill on cancel/close | **VERIFIED** | `_terminate` `:629-691`: stdin sealed **first** `:649`; SIGTERM grace (`_CLOSE_GRACE_SECONDS=1.0`, `:93`, bounded under the 5 s cancel contract) to the root pgid **and every registered `run_within` child's group** `:654/:680`; SIGKILL after; Windows Job `TerminateJobObject` + `taskkill /F /T` `:664-678`; `_reap_all` bounded `:693-702`; belt-and-braces root `proc.kill()` `:688-690`. `_watch_exit` `:497-548` reaps the group the *root itself* leaves behind (the external-kill case), with the pgid remembered `:367` because `os.getpgid` dies with the leader | `TestKillNineMidStream::test_kill_nine_reaps_the_tree_and_close_is_a_no_op`; `TestCloseCancels::test_close_returns_promptly_and_kills_the_tree` (asserts `elapsed < 5.0`); `tests/sandbox/test_cancellation.py` ×4 (cancel of `run()` through the same wrapper) |
| e | `run_within(session, spec)` for one-shots in an open session | **VERIFIED** | Base refusal `base.py:767-779` (no session ⇒ no envelope — the same loud-refusal rule); `SubprocessSandbox.run_within` `subprocess_backend.py:1111-1125` (foreign-session `isinstance` guard); the real one `SubprocessSandboxSession.run_within` `:807-982`: envelope jail via `assert_inside_workspace` `:835`, same env construction `:850`, child registered on the session's kill list `:881`, own watchdog + Job Object `:853-854/:889-897`, own wall clock `:903-922`, session-kill attribution `:953-959`, cancellation reaps and re-raises `:923-943` | `TestRunWithin` ×4 — one-shot ok *and the session stays duplex after it*; workspace outside the envelope refused; closed session refuses; foreign session refused |
| f | One-shot `run()` a thin wrapper | **VERIFIED** | `run` `:1127-1199` contains **no spawn logic**: `open` `:1160` → `_seal_stdin` `:1161` (the DEVNULL-equivalent EOF) → drain task (`_drain_stdout` `:615-625`) → `_await_exit_under_budget` `:1164` → bounded drain `:1166-1179` → `close` `:1180`; CancelledError closes the session and re-raises `:1181-1199`. All env/jail/watchdog/job/deadline machinery lives in `open`/the session | `TestRunIsStillASessionWrapper` ×2 (stdin-reading child still sees EOF); the whole pre-existing `tests/sandbox/test_subprocess_backend.py` suite (23 tests) is the byte-compat witness and is inside the 201 |
| g | Every backend implements or honestly refuses (subprocess = jail + rlimits + AST-scan honesty; docker/wsl/dryrun; win32) | **VERIFIED** | subprocess implements (above) and keeps the AST/network honesty paragraphs: `run` docstring `:1137-1158` ("network denial … is a code-review property, not a containment property" — policy `PERM_NET_EGRESS` + `AstSecurityScanner` are the real layers). docker **refuses** `docker_backend.py:1028-1051` (untested plumbing would be a worse claim than the one-shot ever makes); wsl **refuses** `wsl_backend.py:272-291` (no host exists on which the session path could run once); dryrun **refuses** `dryrun_backend.py:127-141` (nothing executes — a fake session would be plausible-looking lies); the three inherit the base `run_within` refusal. win32: `_win32.py`'s `IS_WINDOWS`/`win_dll`/`last_error` keep the Windows branches checkable; Job Object + taskkill paths sit behind them | `TestHonestRefusals` ×5 (`test_dryrun_refuses_to_open`, `test_wsl_refuses_to_open`, `test_docker_refuses_to_open`, `test_refusing_backends_refuse_run_within`, `test_the_session_contract_is_abstract`); Windows branches pinned by `tests/sandbox/test_win32.py` (footer note at `test_session.py:742-747`) |
| h | Docstrings do not over-claim containment | **VERIFIED** | The module table `subprocess_backend.py:16-31` answers Yes/No per property including three bold **No**s (filesystem, network, syscall); `SandboxSession`'s docstring states what it does **not** guarantee (idle timeout) `base.py:514-519`; `SubprocessSandboxSession` states the deliberate no-`setrlimit` and the "same facts as run(), held open" rule `:293-343`; `open`/`run` carry the verbatim network-denial paragraphs. The one pre-existing phrase audited and left alone: `base.py:25`'s "mandatory AST pre-scan" — pre-existing (this wave's diff does not touch it), and the scan layers it names are real (`validation/engine.py:328`, `marketplace/installer.py:163`); changing a defensible pre-existing sentence would be churn, not honesty | No test can pin a docstring; the *behavioural* honesty is pinned by (d)/(g)'s tests, and `IsolationLevel.PROCESS` is asserted in every session result test |
| i | `docs/modules/sandbox.md` matches the implementation | **WAS A REAL GAP — FIXED HERE** | The in-tree doc (re-numbered by the wave's numbering pass) contained **no mention of `SandboxSession`, `Sandbox.open`, or `run_within`** — the module's new public contract was absent from the module page — and carried a stale "Tests: (200)" (201 since the auditor's test). Fixed: session contract added to the `base.py` section with exact line cites; the `SubprocessSandbox` bullet now describes the session, `run_within`, `run()`-as-wrapper and the no-`setrlimit` reason; docker/wsl/dryrun bullets state their refusals; session rows added to the I/O table; count → 201; a Known-gaps bullet states duplex sessions are subprocess-only for now | `tests/conventions/test_docs_references.py` + `test_markdown_links.py` = **13 passed** after the edit; every new citation's line content verified mechanically (§4) |

The row's three named acceptance tests all exist **by name** and pass:

1. *echo child over duplex pipe under rlimits* →
   `tests/sandbox/test_session.py::TestDuplexEcho::test_lines_written_are_echoed_back_under_the_resource_envelope`
2. *kill -9 mid-stream reaps the tree* →
   `tests/sandbox/test_session.py::TestKillNineMidStream::test_kill_nine_reaps_the_tree_and_close_is_a_no_op`
3. *cancellation < 5 s* →
   `tests/sandbox/test_session.py::TestCloseCancels::test_close_returns_promptly_and_kills_the_tree`
   (asserts `elapsed < 5.0`; the SIGTERM grace is 1.0 s, bounded under it on purpose)

## 3. What changed (per file)

- `docs/modules/sandbox.md` (+~40 lines net): the C5.2 mismatch fix of §2(i)
  — `SandboxSession`/`open`/`run_within` now documented with verified
  line cites (`sandbox/base.py:491/544/564/577/753/767`,
  `subprocess_backend.py:292/303/1111/1127`, `docker_backend.py:1028`,
  `wsl_backend.py:272`, `dryrun_backend.py:127`); session rows in the I/O
  table; test count 200 → 201; new Known-gaps bullet (duplex is
  subprocess-only for now). No other doc touched.
- This report (new).
- **No source or test changes.** The audit found the implementation and its
  21 tests complete and green; the only real gap was the doc mismatch, and
  the brief's fix list named exactly that category.

## 4. Commands run and results (verbatim summaries)

| Command | Result |
|---|---|
| `python -m pytest tests/sandbox -p no:cacheprovider` | **201 passed in 47.16s** (pre-edit) / **201 passed in 50.03s** (post-edit) |
| `python -m pytest tests/sandbox --collect-only -q` | per-file: `test_base.py: 13`, `test_cancellation.py: 4`, `test_docker_backend.py: 107`, `test_recursion.py: 20`, `test_session.py: 21`, `test_subprocess_backend.py: 23`, `test_win32.py: 12`, `test_wsl_backend.py: 1` — sum 201 |
| `python -m pytest tests/sandbox/test_session.py -o addopts="--strict-markers --strict-config" -v -p no:cacheprovider` | **21 passed in 6.15s** — all names listed; the 21 are `TestDuplexEcho` (1), `TestReadSemantics` (4), `TestKillNineMidStream` (1), `TestCloseCancels` (1), `TestWallClockDeadline` (1), `TestMemoryCeiling` (1), `TestEnvironmentIsolation` (1), `TestRunWithin` (4), `TestHonestRefusals` (5), `TestRunIsStillASessionWrapper` (2) |
| the three acceptance tests by node id (same flags) | **3 passed in 3.55s** — one PASSED line each, names as quoted in §2 |
| `python -m pytest tests/conventions/test_docs_references.py tests/conventions/test_markdown_links.py` | **13 passed in 7.57s** (5 passed on docs-references alone before my edit; both green after) |
| `python -m ruff check src tests` | All checks passed |
| `python -m mypy src/paa` | Success: no issues found in 173 source files |
| per-citation line check (sed) | all 13 new cites land on the exact `class`/`def` lines quoted above |

## 5. Out-of-scope findings (recorded, not fixed, per brief)

1. **`cpu_cores` is enforced by no host-process path.** Docker passes
   `--cpus` (`docker_backend.py:807`); WSL applies CPU-*seconds*
   (`ulimit -t`, not cores, `wsl_backend.py:256`); the subprocess backend —
   session and one-shot both — ignores the field entirely. No docstring
   claims otherwise (the module table makes no CPU claim, and `SandboxSpec`
   itself documents only that the field exists), so this is an honest
   absence rather than a lie. It matters the day C5.3 sizes an MCP server
   session by CPU and nothing enforces it — that consumer should size by
   `memory_mb` + `timeout_seconds`, or file the enforcement as its own task.
2. **`run_within` children lead groups of their own** (the POSIX
   `setpgid`-across-sessions refusal documented at
   `subprocess_backend.py:807-819`), which is why `_terminate` walks a target
   list instead of one group. Correct as built; a future "one group per
   session" refactor would simplify the kill path and is worth a task the
   next time the session machinery is opened (e.g. W6/P3 or W7/C7.6).
3. **Stale known-gap note in `docs/modules/sandbox.md`**: "Six pre-existing
   test failures: sandbox runs `python` where only `python3` exists (README
   build-status table)". It cites the README's *build-status table* (a
   recorded historical build), this tree's sandbox suite is 201/201, and
   `README.md` is outside my write-set — left as-is for whoever next touches
   the README or that table.
4. **W5-IMPL-01's blank-cite finding is already green.** Its report recorded 8
   `test_every_referenced_line_is_not_blank` failures, 4 attributed to this
   wave's `base.py` line shifts; the current tree passes that test (5 passed
   before my edit). The renumber landed after that report was written —
   recorded here so the orchestrator's timeline has the closing entry.
5. **W5-IMPL-01's in-flight ruff `ASYNC109` ×3 finding is already green.**
   Its report flagged `base.py:544`, `subprocess_backend.py:405`,
   `tests/coder/test_cancellation.py:129`; the current `ruff check src tests`
   is "All checks passed!" — resolved before my run, recorded for the same
   timeline reason.

## 6. Deviations

- None on scope or write-set. No `git add`/`git commit`/`git push`/`git
  stash`/`git checkout --` was run; `git diff`/`git status` were used
  read-only to scope the wave's own claims against pre-existing ones.
- One interpretive deviation from the brief's *method*, recorded rather than
  papered over: the brief told me to find "the preexec/setrlimit site" for
  the rlimits element — there is none, and the verdict in §2(b) is therefore
  "resource limits are genuinely applied, by different mechanisms, with the
  absence of `setrlimit` itself a documented decision" rather than a
  citation of a `setrlimit` call. The docstring's stated reasons (preexec_fn
  unsafe in a threaded parent; no containment-story drift between `run()`
  and the session) were checked against the spawn sites (`:1059-1065` —
  `start_new_session` chosen over `preexec_fn=os.setsid` for exactly that
  safety reason) and are sound.
- The doc fix (§2(i)) is inside the brief's named fix categories
  ("docs/modules/sandbox.md mismatch"); nothing else in the write-set was
  touched, because nothing else had a real gap.

## 7. Checklist

- [x] (a) duplex `open`/`read`/`write`/`close` — VERIFIED (landmarks + `TestDuplexEcho`, `TestReadSemantics` ×4).
- [x] (b) resource limits applied to the child — VERIFIED as
      Job Object + watchdog + wall-clock; no-`setrlimit` decision audited and
      endorsed (`TestMemoryCeiling`; `TestDuplexEcho` under `memory_mb=64`).
- [x] (c) wall-clock honoured — VERIFIED (`TestWallClockDeadline`;
      `test_run_with_no_wall_clock_waits_for_the_work`).
- [x] (d) process-tree kill on cancel/close — VERIFIED
      (`TestKillNineMidStream`; `TestCloseCancels` < 5 s; test_cancellation ×4).
- [x] (e) `run_within` — VERIFIED (`TestRunWithin` ×4).
- [x] (f) `run()` thin wrapper — VERIFIED (`TestRunIsStillASessionWrapper` ×2
      + 22-test byte-compat suite inside the 201).
- [x] (g) every backend implements or refuses loudly — VERIFIED
      (`TestHonestRefusals` ×5; win32 pinned by `test_win32.py`).
- [x] (h) docstrings honest — VERIFIED (no over-claim found; the one
      pre-existing "mandatory AST pre-scan" phrase judged defensible and left).
- [x] (i) `docs/modules/sandbox.md` matches the implementation — FIXED here;
      citation lock green after the edit (13 passed).
- [x] Three acceptance tests exist by name and pass (re-run by node id: 3 passed).
- [x] `tests/sandbox` = 201 passed (verbatim, twice); `test_session.py` = 21.
- [x] ruff clean (`src tests`), mypy 0 issues in 173 files.
- [x] No source/test changes made (none needed); write-set per brief; no
      commit performed.
