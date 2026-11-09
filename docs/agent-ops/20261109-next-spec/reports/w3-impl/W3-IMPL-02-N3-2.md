<!-- ORCH-DIGEST:START -->
agent: W3-IMPL-02 | task: T-N3-2 | status: DONE
code_commits: 4c999a7 | report_commit: this report's own docs commit (next commit)
files_changed: 2 (scripts/drill_kill_resume.py, docs/agent-ops/20261109-next-spec/reports/w3-impl/W3-IMPL-02-N3-2.md)
contracts_changed: none
tests: added 0, changed 0; scoped run: pass (drill itself is the harness; conventions 34 pass; dry-run 47/47)
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 1 (COMMS.md modified by others — not mine, not committed by me)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none — drill ran green on two seeds; Linux POSIX half cannot run on this host
confidence: high (5/5 default seed + 6/6 seed-14 full matrix both exit 0; dry-run byte-identical)
last_comms_line_read: [2026-11-09T16:36:18Z] W3-IMPL-01 START
<!-- ORCH-DIGEST:END -->
# Implementation report — W3-IMPL-02 — T-N3-2 N3.2 kill drill driver

## 1. Task and acceptance criteria (restated)

Implement the real `--iterations N` driver in `scripts/drill_kill_resume.py`:
launch the ScriptedModel-driven agent in a subprocess, hard-kill it
(POSIX `kill -9` / Windows `TerminateProcess`) at a seeded-random tool
call, restart the session with the CLI's `--resume` path, and verify
zero duplicated committed effects plus a correct tree with a shadow-tree
diff oracle. File-tool classification gates the verdict; `bash`/`git`
interruptions report `unknown` and are never auto-retried (D-6).

Acceptance: `--iterations 5` on this (Windows) host completes with
tree-correct + zero-duplicate verdicts; `--dry-run` still 47/47;
`--iterations 20` recorded as Linux-gated NOT-RUN; ruff 0, mypy 3
(pre-existing baseline), conventions 34. Write-set:
`scripts/drill_kill_resume.py` only (prefer script-side). Do not touch
`src/` (N3.3 parallel would conflict; recovery behavior already proven
by N2.3). Commits code-then-report with explicit paths; final message
at most 8 lines.

## 2. Verification that the problem exists (evidence gathered myself)

[RAN `python scripts/drill_kill_resume.py --dry-run` -> 47 PASS lines,
`dry-run: all checks held`]
[RAN `python scripts/drill_kill_resume.py --iterations 20` -> exit 2 +
`NOT-RUN: the N3.2 kill drill driver is a skeleton ...` — the exact
unimplemented skeleton the brief names]
[VERIFIED scripts/drill_kill_resume.py:46-55 (pre-change) — the
`--iterations` branch printed the NOT-RUN note and returned 2;
`dry_run()` (lines 215-535 pre-change) held the complete S1 harness
and was not to be regressed]
[VERIFIED tests/coder/test_recovery_chaos.py:649-851 — N2.3 resume
recovery is pinned by test (sweep closure `FAILED(reason=interrupted)`,
`classify_interrupted` applied/not_applied/unknown, `--resume` note
injection via `Session.inject_recovery_note`), so the drill consumes
that behaviour as a proven primitive instead of re-proving it]
[VERIFIED src/paa/coder/agent.py:262-268 — `CoderAgent.build` runs
`_inject_resume_recovery_note` whenever `session.turns` is non-empty
(the `--resume` branch), which is the path the resume child uses]
[VERIFIED src/paa/ledger/recovery.py:307-361 — `classify_interrupted`:
file tools decided from persisted `pre`/`post` hashes vs live bytes;
everything else returns `UNKNOWN` when uncheckable (D-6)]

## 3. Root cause

The N3.1 delivery left the drill as a documented skeleton: the
script's `main()` short-circuited `--iterations N` to exit 2 because no
subprocess driver, kill injection, resume continuation, or oracle
existed. The gap was not recovery logic (N2.3 proves it) but a
deterministic way to crash a real agent mid-tool-call and then prove
convergence from the other side. The driver had to solve three hard
sub-problems: (a) landing a hard kill provably *inside* a tool call
(after `TOOL_CALL_PROPOSED` is durable, before the outcome row);
(b) a resume continuation that a `ScriptedModel` can execute without
blind-repeating an interrupted call; (c) an oracle independent enough
that a cheating/crashing child cannot fake its verdicts.

## 4. Approach chosen and why (alternatives rejected)

**Kill-window tool wrapper (chosen).** The fresh child wraps every
tool in `agent.tools` with `_KillWindowTool`. The wrapper parks on
`asyncio.Event().wait()` inside `tool.run()` — the one point that is
provably after `journal.before` (PROPOSED durable) and before
`journal.after` (COMPLETED/FAILED). The parent `proc.kill()`s the
process during the park (`SIGKILL` on POSIX, `TerminateProcess` on
Windows). The signal file is written *after* the delegate completes in
after-effect mode and *before* any delegation in before-effect mode, so
the effect has fully landed or never started — deterministic on both
sides of the crash for every tool (see §7 for the race the first
version lost). Delegation via `__getattr__` preserves `schema`,
`name`, `description`, `isolation_level`, and `preflight`, so the
journal still attests the real isolation level. If the wrap seam ever
breaks (the loop stops dispatching through `agent.tools`), the child
exits 0 without a signal and the parent fails the iteration loudly —
the drill never passes an uninterrupted run.

**Alternatives rejected.**
- *Signal from `ScriptedModel.propose` and race the parent kill:*
  rejected — proposing precedes journaling, so many kills would land
  between calls (no interrupted call, no proof), and fast in-process
  edits would often COMPLETE before the parent's kill arrived. Flaky by
  design; window timing would decide the verdict instead of recovery.
- *PRE/POST hook signals:* rejected — PRE fires *before* journaling
  (kill would precede the PROPOSED row), POST fires *after* the outcome
  row. Neither brackets the crash the way `tool.run()` does.
- *Journal-wrapper parking (`journal=` injection):* rejected — the
  child cannot construct a `LedgerToolJournal` before `CoderAgent.build`
  (the runtime ledger only exists inside the build), so no public seam
  was available.
- *Replay the same six scripted turns on resume:* rejected — blind
  replay re-executes unknown calls (D-6 violation) and attempts a
  second `edit_file` on an already-fixed file. The resume child instead
  inspects (live file bytes, `git log`, `git diff --cached`) and builds
  continuation turns from observations: edit only if still buggy, never
  `bash`, stage/commit only if inspection proves no fix commit exists.

**Oracles (parent-side, independent of the child).** Each iteration
checks the conjunction of: empty shadow-tree diff against the fixture
with the fix applied (manifest of SHA-256 over every non-`.git`,
non-`__pycache__` file), host-run pytest green, git history of exactly
buggy-head + one fix commit with a clean tree, both ledger lineages
(hash chain verifies, exactly one `FAILED(reason="interrupted")` for
the killed tool, none left open), and the classification gate — the
parent reads the post-kill live bytes itself and compares the expected
`applied`/`not_applied`/`unknown` against the exact resume-note token
`: <state> —` (exact, because the note trailer always mentions
`unknown` and `applied` is a substring of `not_applied`, so substring
checks would pass vacuously). Zero-duplicate is enforced by git commit
counts (== 1 fix commit) plus ledger COMPLETED counts (≤ 1 resumed
`git_commit`, an `applied` edit never re-COMPLETED).

## 5. What changed (per file)

Only `scripts/drill_kill_resume.py` (plus this report):

- Module docstring: the `--iterations` paragraph now describes the real
  driver (kills, resume, oracle) and records the 47-check dry-run is
  unchanged. First line "skeleton" scrubbed. No behavioural doc claim
  added beyond what the code does.
- Imports: `random`, `Mapping` (ABC), `Session`, `TurnRole`,
  `find_interrupted_calls`. Nothing else.
- New constants: `TOOL_ORDER` (the six calls in execution order; the
  kill index addresses it), `DRILL_SEED` (20261109), watchdog timeouts
  (120 s signal / 180 s resume), `_ORACLE_EXCLUDED_PARTS`.
- New helpers: `_fixture_pair`, `_pytest_command`, `_scripted_turns`
  (the full S1 sequence shared with the dry-run), `tree_manifest`,
  `expected_manifest`, `_git_subjects`, `_git_staged`,
  `_init_buggy_repo`, `_write_json` (closed-file writes so the session
  marker survives `kill -9`).
- New child machinery: `_KillWindowTool` (park-in-`run` wrapper with
  before/after-effect windows, isolation delegation), `_child_settings`
  (subprocess sandbox, per the dry-run), `_child_append_mandate`
  (pre-work explicit mandate, mirroring dry-run and `cli/coder.py`),
  `child_fresh` (marker file, mandate, full script, wrapped tools),
  `child_resume` (`Session.resume` + continuation from inspection,
  fresh correlation, mandate, chain-verify, result file carrying the
  recovery notes and the inspection the continuation was chosen from),
  `_resume_inspection` / `_resume_turns` (inspector → continuation
  turns; never `bash`; never works from an `other` tree).
- New parent machinery: `_spawn_child` (output to log files),
  `_wait_for_signal_or_exit` (signal-vs-exit-vs-timeout watchdog),
  `_read_ledger_verdict` (opens the throwaway ledger database via
  `Database`+`LedgerStore` directly — no `Runtime` boot, so no sweep
  appends; summarizes PROPOSED/COMPLETED/interrupted/still-open and
  the verified chain for both lineages; database path is
  `settings.storage.sqlite_path` (`<home>/state/paa.db`), `_one_drill_iteration`
  (fixture + buggy git head, seeded kill index, alternating
  before/after windows, hard kill, parent-reads-live-bytes gate,
  resume, then the four-conjunct verdict; failures and `--keep` keep
  the iteration root, passes are removed), `_IterationAborted`,
  `drill_iterations` (prints the Windows-subset/Linux-gated header so a
  Windows pass can never be read as the 20x Linux result).
- `main()`: `--iterations N` now dispatches to the driver (validated
  `>= 1`), `--seed` added, hidden `--child` mode validates required
  args and dispatches `child_fresh` / `child_resume`. `--dry-run`
  dispatch unchanged.
- `dry_run()`, `_fixture_pair`-adjacent helpers, and `ScriptedModel`
  are byte-identical to the pre-change file [VERIFIED: byte-level
  segment comparison of `dry_run`, helpers `sha256_hex`→`dry_run`, and
  `ScriptedModel`→`dry_run` between `git show HEAD:` and the working
  copy — all identical; the only dry-run-adjacent change is the module
  docstring].

No `src/` file was touched; `tests/coder/test_recovery_chaos.py` was
not needed (the drill is operator-side harness and its assertions are
runtime verdicts, not library behaviour).

## 6. Blast radius and contracts

- Zero production-code blast radius: one script file, no `src/`
  changes, so N3.3's parallel `loop.py`/`shell_tools.py` work cannot
  conflict. The only runtime seam touched is the public-ish
  `agent.tools` dict, used exactly as construction hands it to the
  loop (verified aliasing in the constructor); any future change that
  breaks it fails the drill loudly rather than silently (see §4).
- No contract changes: no new event types, no ledger payload changes,
  no CLI surface changes beyond the documented `--iterations`/`--seed`
  behaviour of this script (the `--child` flags are hidden).
- The drill never writes outside its throwaway `tempfile` roots
  (workspace, `PAA_HOME`, signal/marker/result/log files), never
  touches the real `~/.paa`, never spends API tokens (ScriptedModel),
  never binds ports.

## 7. Tests (how each confirmed to fail without fix)

The drill itself is the test harness; each verdict is an
independently-evidenced check. Evidence, observed on this host:

- [RAN `--iterations 5` (default seed 20261109) -> 5/5 PASS, exit 0;
  kill positions: bash(before), edit_file(after), bash(before),
  bash(after), bash(before)] — acceptance run.
- [RAN `--iterations 6 --seed 14` -> 6/6 PASS, exit 0; kill positions
  read_file(before), bash(after), edit_file(before), git_stage(after),
  bash(before), git_commit(after)] — full-tool validation seed chosen
  because it is the only small-seed draw that covers every tool. Keyword
  results behind the summaries: edit `not_applied` with exactly one
  retried COMPLETED edit (seed14 iter3); edit `applied` with zero
  re-COMPLETED edits (default iter2); `git_commit(after)` with **zero**
  resumed commits (the already-committed no-duplicate path, iter6: `[]`
  completed); all other kills classify `: unknown —` with `git_commit`
  completed only after no-fix-commit inspection.
- Mutation checks (first version of the driver, seed 14): the signal
  was written before the after-effect delegate, so the parent's kill
  raced inside subprocess delegates; `git_commit(after)` landed
  mid-`git` (no commit existed at resume inspection) while the label
  claimed "after-effect". The iteration still passed — the outcome was
  correct, but the window label overclaimed determinism. Root-caused,
  fixed (signal after delegate in after-mode; docstrings updated), and
  re-verified: iter6 now shows the commit made *before* the kill and
  the resume issuing none. This is the self-review catch recorded
  because it demonstrates the drill's failure mode is a loud oracle
  mismatch, not a silent pass.
- Oracle strictness spot-checks by construction: reverting the wrap
  (child exits 0, no signal) -> `fresh child reached the kill window`
  FAIL; making the resume always re-run the edit -> an `applied` edit
  yields a COMPLETED edit -> FAIL; a second fix commit -> git history
  count 3 -> FAIL; a misclassified note -> exact-token gate FAIL. (Each
  was verified by reading the check, not by sabotaging the script; the
  sabotage space is covered by the seed matrix above.)
- No new tests were added to `tests/coder/test_recovery_chaos.py`:
  N2.3 pins the library semantics, and the drill pins end-to-end
  convergence. Adding a pytest that shells the script would duplicate
  the script's own verdict surface.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- Source: the driver reads only the fixture, the ledger the children
  wrote, and git state the children produced; a hostile scripted
  turn cannot push the parent anywhere it doesn't already derive from
  the fixture. `--seed` accepts any int (negative included — indices
  still derive from `Integer` only); `--iterations < 1` is refused with
  exit 2.
- Load: per-iteration cost is a few subprocess agent runs (~10-20 s on
  this host including runtime boots and pytest); the 20x Linux matrix
  is 20x that, unattended and clean-up-on-pass. Watchdogs (120 s/180 s)
  kill a hung child instead of hanging the operator; the parent never
  waits unboundedly.
- Hostile input: kill positions are bounded by `randrange(6)`;
  signal/marker/result files are validated JSON whose fields are
  type-checked before use (`marker["session_id"]` is `str()`-coerced
  from parsed JSON; a wrong correlation id fails its check). Child
  stdout is a log file, never a pipe, so log volume cannot block a
  child.
- Operability: `--keep` and any failing iteration print the workspace
  and `PAA_HOME` paths; child logs are `child-fresh.log` /
  `child-resume.log` beside them. Reversal: `git revert` of the single
  script commit restores the exit-2 skeleton verbatim; no data
  migration, no ledger writes outside throwaway homes.
- Reversal (which check fails if the fix is reverted?): reverting the
  whole change restores exit 2 for `--iterations` (trivially "fails"
  the drill), and breaking any oracle conjunct (wrap, continuation,
  token gate) fails the affected iteration as §7 describes.

## 9. Edge cases and abuse scenarios

- Kill at 0 (`read_file`): read-only, classifies `unknown`; resume
  proceeds from inspection to edit/stage/commit. Covered by seed 14.
- Kill at 5 (`git_commit`) before: nothing committed; resume stages
  only if inspection shows it unstaged, then commits once. Covered by
  every pre-commit kill in both seed runs.
- Kill at 5 after (effect landed): inspection sees the fix commit,
  resume emits only the final text; zero resumed commits; no duplicate.
  Covered by seed 14 iter 6 after the signal fix.
- Torn `calc.py` (neither buggy nor fixed): the resume child refuses
  to edit/stage/commit (`other` -> final text only); the parent's
  shadow-tree diff and classification gate (`unknown` expected from
  live bytes) fail the iteration. (Unreachable through the parked
  windows — the tool either ran to completion or not at all — but the
  oracle handles a mid-byte tear if a real SIGKILL ever lands inside
  the write on Linux.)
- Torn transcript line: `Session.resume` skips unparseable lines
  ([VERIFIED src/paa/coder/session.py:211-222]); the recovery note
  matching falls back to `unknown` for the affected call.
- Killed child leaves the DB in WAL: SQLite WAL recovery on the next
  `Database.connect()` replays it; committed rows survive, the
  interrupted PROPOSED is still the open call for resume to close.
- Signal/marker never arriving: watchdog kills and fails loudly with
  iteration roots kept.
- Operator error: `--iterations 0`/negative -> exit 2 with a message;
  `--child` without required args -> exit 2.

## 10. Performance and security

- No secrets are handled: no API keys, no env values written anywhere
  (the script reads none); PAA_HOME and workspaces are `tempfile`
  throwaways outside the repo; nothing is committed but the script and
  this report.
- The child runs with the same sandbox posture as the dry-run
  (subprocess backend, ASK mode with auto-yes confirm, explicit
  mandate) — assertions about isolation are not repeated here
  (dry-run pins them), and the wrapper delegates isolation attributes
  so the journal cannot under-attest.
- Timeouts are bounded and kill-gated (watchdog, not retry): the
  parent kills any child that misses the signal window or the resume
  deadline, and `run_pytest` keeps its 120 s cap. The park event is
  never set; only a hard kill ends a kill-window child.

## 11. Commands run and results (verbatim summaries)

[RAN `.venv/Scripts/python.exe scripts/drill_kill_resume.py --dry-run`
-> 47 PASS, `dry-run: all checks held` (before and after the change)]
[RAN `.venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 20`
-> exit 2 + skeleton NOT-RUN note (pre-change baseline)]
[RAN `.venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 1 --keep`
-> 1/1 PASS, exit 0 (smoke)]
[RAN `.venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 5`
-> 5/5 PASS, exit 0 (acceptance; default seed 20261109)]
[RAN `.venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 6 --seed 14`
-> 6/6 PASS, exit 0 (full-tool matrix after the signal-ordering fix)]
[RAN `.venv/Scripts/python.exe -m ruff check src tests` -> `All checks passed!`]
[RAN `.venv/Scripts/python.exe -m ruff check scripts/drill_kill_resume.py`
-> `All checks passed!` (7 initial findings fixed)]
[RAN `.venv/Scripts/python.exe -m mypy src` -> 3 errors, all pre-existing
Windows `subprocess_backend.py:548` POSIX attributes (the documented baseline)]
[RAN `.venv/Scripts/python.exe -m pytest tests/conventions -q` -> 34 passed]
[NOT-RUN `scripts/drill_kill_resume.py --iterations 20` on Linux/WSL2 —
this host is Windows; the driver is platform-generic (`proc.kill()` =
SIGKILL there) and the header records the 20x matrix as NOT-RUN here
per the brief.]

## 12. Deviations from plan/brief

- The brief's "random tool call" is implemented as seeded-random
  (`--seed`, default 20261109, `Random(seed + i)`) rather than ambient
  randomness, and the after-effect window alternates by iteration
  index. Both are operator-visible in the summary line and change with
  `--seed`; the alternation exists so the two classification branches
  are exercised deterministically per run instead of by coin flip. The
  acceptance run covers the `applied` edit branch; seed 14 covers the
  `not_applied` branch and every tool.
- `bash` interruptions cause the resume child to emit no `bash` turn
  at all (the host pytest is the suite check). The brief says
  "bash/git reported-not-retried (unknown by design, D-6)" — this is
  the maximal reading of that (not re-run even after inspection);
  git stage/commit *are* completed after a proving inspection, which
  is the D-6 "inspect first" path and is evidenced in the result file.
- No tests were added to `tests/coder/test_recovery_chaos.py` (the
  brief preferred script-side; nothing in the recovery library needed
  pinning).
- Independent reviewer subagent could not be dispatched (subagent depth
  limit in this environment; see §16). Structured self-review replaced
  it.

## 13. Out-of-scope findings

- 1 finding, not mine, not fixed: `git status` shows
  `docs/agent-ops/20261109-next-spec/COMMS.md` modified by others
  (and several untracked docs). My commits use explicit paths for my
  two files only; I did not touch COMMS or any other file. Filed here
  per standing orders rather than as a COMMS FINDING, because the fix
  (their commit) is not mine and COMMS is ORCH-committed at wave
  boundaries (rule 8).
- Noted (no action): the `pwsh`-absent shell observation from N3.1
  (bash tools spawn-fail on this host) recurs inside the drill; the
  dry-run already documents this split and the drill's suite evidence
  is the host pytest, identical in kind.

## 14. Known limitations, risks, rollback notes

- Linux/WSL2 20x NOT-RUN here (no Linux host; Windows CI is the
  5-iteration TerminateProcess subset per plan §E/N3.2). The driver is
  platform-generic, but POSIX SIGKILL returncode (`-9`) was not
  executed on this host — only TerminateProcess returncode (1).
- Kill windows are parked tool-boundary points (before/after
  delegates). A mid-byte torn file write from a real SIGKILL is
  handled by the oracle (`other` path) but cannot be produced through
  the parked windows; true mid-statement crashes are what the N2.3
  chaos suite's close-and-reopen test pins at the ledger layer.
- `proc.kill()` kills the agent process only, not its sandbox
  grandchildren; a delegate that spawned a helper (bash tool) can
  briefly outlive the killed agent. The helper is short-lived
  (bounded pytest run) and its orphaned output cannot affect the
  workspace (the sandbox writes nowhere but the workspace, and the
  run has passed the window). A process-tree kill is out of scope for
  the Windows subset.
- Rollback: `git revert` of the single code commit restores the exit-2
  skeleton. No schema, ledger, or data impact (all state is in
  throwaway temp dirs).
- If `CoderAgent`'s tool-dispatch construction ever stops sharing the
  `agent.tools` dict with the loop, the wrap silently stops firing —
  and the parent then fails every iteration loudly (no signal arrives
  within the watchdog). There is no silent-pass path.

## 15. Where I am least confident

The POSIX half (SIGKILL semantics, `asyncio` subprocess behaviour
under the Linux event loop, `bash` really executing inside the
sandbox there) is implemented but unexercised on this Windows host;
I have no reason to expect divergence (same `proc.kill()` surface,
same agent/loop code), but the 20x Linux evidence remains
NOT-RUN by construction, not by passing.

## 16. Self-review checklist

- [x] Diff re-read from scratch against the brief: write-set respected
  (only `scripts/drill_kill_resume.py` + this report); `src/` and
  `tests/` untouched; `dry_run`/helpers/`ScriptedModel` byte-identical
  [VERIFIED: byte-level segment comparison vs `HEAD:`].
- [x] Correctness/root cause: the dead skeleton is replaced by the
  real driver; the after-effect signal race found during validation
  was root-caused (signal before delegate) and fixed by construction
  (signal after delegate in after-mode) — not papered over.
- [x] Security/edge cases: bounded time, no secrets, validated
  `--iterations`, JSON-file validation, `other`-state refusal, torn
  transcript fallback, WAL recovery. Findings none above MINOR; the
  grandchildren note (§14) is documented, not fixed (out of scope for
  the Windows subset).
- [x] Maintainability: kill-window semantics, D-6 inspection
  discipline, and oracle independence documented in the code where the
  decision is made; the wrap's isolation delegation is called out.
- [x] Test rigor (mutation mentality): §7 lists what fails without
  the fix; the exact-token gate, interrupted-close count, git-count,
  and completed-count checks each detect their target break.
- [x] Independent review: reviewer subagent unavailable (subagent
  depth limit, same limitation recorded by W2-IMPR-06); performed the
  structured self-review above instead and state the gap openly.

## 17. Commits

- (code) `4c999a7` — `scripts/drill_kill_resume.py` only, message with
  `Task: T-N3-2 / Agent: W3-IMPL-02 / Wave: 3 / Report: ...`.
- (report) this report's own docs commit (the commit after `4c999a7`).
- Explicit-path commits on `ops/20261109-next-spec`; verified with
  `git branch --show-current` (ops/20261109-next-spec, as received).
- NOT-RUN: Linux/WSL2 `--iterations 20` (see §§9/11/14).
