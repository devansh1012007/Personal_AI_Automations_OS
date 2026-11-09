# W3-REV-02 review — T-N3-2 kill drill (commit 4c999a7)

## ORCH-DIGEST

- VERDICT: APPROVE (no BLOCKER/MAJOR; 2 MINOR + 2 NIT below)
- Claims verified y own runs — all 7 hold:
  - (1) `--iterations 5` -> 5/5 PASS, exit 0, default seed 20261109 [RAN]
  - (2) `--seed 14 --iterations 6` -> 6/6 PASS, exit 0 [RAN]; kill tools
    read_file, bash, edit_file, git_stage, bash, git_commit — all six
    tool classes covered (both before- and after-effect windows)
  - (3) `--dry-run` -> 47 PASS / 0 FAIL [RAN]
  - (4) Real subprocess kill mid-call [VERIFIED scripts/drill_kill_resume.py:1021-1031,
    :766-795, :1208-1209] — window parked inside `tool.run()` after
    `journal.before` (src/paa/coder/loop.py:255) and before `journal.after`
    (:279-285); parent `fresh_proc.kill()` with `rc != 0` checked
  - (5) Real shadow-tree oracle [VERIFIED scripts/drill_kill_resume.py:629-660,
    :1279-1299] — SHA-256 manifest diff + host pytest + git counts +
    full ledger oracle (hash chains, exactly-one interrupted close, exact
    classification tokens). Not exit-0-only.
  - (6) Linux 20x NOT-RUN honestly recorded [VERIFIED scripts/drill_kill_resume.py:1432-1444]
  - (7) `dry_run` byte-identical vs 2d7f9f6 [RAN AST-extracted comparison:
    identical, sha256 prefix 3934c955651c4903 both sides]; all pre-existing
    helpers (`sha256_hex`, `is_hex64`, `run_pytest`, `git`,
    `git_apply_patch`, `ScriptedModel`) likewise identical [RAN]
- Scoped gates [RAN]: `pytest tests/conventions -q` -> 34 passed (exit 0);
  `ruff check src tests` -> All checks passed (exit 0).
  `mypy src` -> 3 errors, all src/paa/sandbox/subprocess_backend.py:548
  (Windows sys.platform folding), pre-existing and untouched by this
  commit (scripts-only diff) — not a finding against 4c999a7.
- §16 self-review verdict: HONEST — see section below. One sloppy phrase
  noted as NIT, no overclaim of substance.
- Findings: 2 MINOR (log-fd leak; oracle omits isolation_level), 2 NIT
  (stale docstring "only mode that runs today"; §5 "_fixture_pair-adjacent
  helpers" phrasing). None block acceptance.
- Report: docs/agent-ops/20261109-next-spec/reports/w3-review/W3-REV-02-review.md
- Code audited: 4c999a7 (1 file, scripts/drill_kill_resume.py, +989/-19 per
  `git show 4c999a7 --stat`); report by W3-IMPL-02 commit e2b0e39.

## Scope and method (blind pass first)

I inspected `scripts/drill_kill_resume.py` in full (1542 lines) and ran
all three modes BEFORE reading W3-IMPL-02-N3-2.md, per brief order. Then
read the impl report (esp. §16), plan N3.2
(docs/2026-11-09-next-spec.md:187), and the s1-report kill-resume
section (docs/operations/2026-11-09-next-spec/s1-report.md:134-141).
All factual claims below are tagged per standing orders.

## Claims, one by one

### (1) `--iterations 5` 5/5 PASS — VERIFIED [RAN]

`[RAN .venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 5
-> 5/5 iterations passed, exit 0]`
Kill positions observed: bash(before), edit_file(after), bash(before),
bash(after), bash(before) — matches W3-IMPL-02 §7 listing. Note the
default seed only draws 2 of 6 tool classes; the report says so openly
(§12) and relies on seed 14 for the full matrix. Not a defect — the
brief's acceptance is "5 on Windows".

### (2) `--seed 14` 6/6 PASS, different tool classes — VERIFIED [RAN]

`[RAN .venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 6
--seed 14 -> 6/6 iterations passed, exit 0]`
Kill positions: read_file(before), bash(after), edit_file(before),
git_stage(after), bash(before), git_commit(after) — every TOOL_ORDER
class hit, both windows. Matches §7. The git_commit(after) iteration
passed with zero resumed commits (the already-committed no-duplicate
path), consistent with the signal-ordering fix described in §7.

### (3) `--dry-run` 47/47 — VERIFIED [RAN]

`[RAN .venv/Scripts/python.exe scripts/drill_kill_resume.py --dry-run ->
47 [PASS] lines, 0 [FAIL], "dry-run: all checks held", exit 0]`
Counted programmatically (Select-String), not eyeballed. 47 agrees with
W3-REV-01's corrected count and the impl report.

### (4) Real subprocess kill mid-call — VERIFIED [VERIFIED]

- Child launch is a genuine subprocess:
  `asyncio.create_subprocess_exec(*argv, stdout=log_fh, stderr=log_fh)`
  [scripts/drill_kill_resume.py:1021-1031], argv = sys.executable +
  this script + `--child fresh ...` (:1173-1195).
- The kill window is inside `tool.run()`:
  `_KillWindowTool.run` parks on `asyncio.Event().wait()`
  (:766-795). The loop calls `tool.run` at src/paa/coder/loop.py:276,
  after `journal.before` (:255, PROPOSED durable) and before
  `journal.after` (:279-285) — so the crash lands mid-call in the only
  sense the ledger can observe. [VERIFIED src/paa/coder/loop.py:255,276]
- The kill itself is `fresh_proc.kill()` (:1208) with
  `check(rc != 0, "fresh child died by hard kill")` — observed
  returncode=1 on this host (Windows TerminateProcess); POSIX -9 is
  NOT-RUN here, honestly stated (:1434-1439, report §14).
- The "window widening" design decision (park instead of racing the
  kill against fast in-process tools) is documented at the decision
  point (:696-728) with the watchdog backstop (`_SIGNAL_TIMEOUT_S`
  kills and fails the iteration if no signal arrives, :1196-1220).
  A child that never reaches the window cannot pass. This is a real
  kill, not a simulation flag. The parent cannot fake it: the ledger
  oracle independently demands exactly one
  `FAILED(reason="interrupted")` close for the killed tool on the
  fresh lineage (:1318-1327), which only the recovery path produces
  (src/paa/coder/agent.py:278-313 [VERIFIED]).

One disclosure gap to record, not a defect: the park is at a tool
boundary (before-delegate or after-delegate), so a true mid-statement
crash (torn write inside `edit_file`) cannot be produced through it.
The report states this at §14 ("Kill windows are parked tool-boundary
points ... handled by the oracle but cannot be produced through the
parked windows"). Honest.

### (5) Shadow-tree oracle is real — VERIFIED [VERIFIED]

The verdict is a four-conjunct check (:1127-1138 docstring, implemented
at :1279-1394):

1. `tree_manifest` (:629-645) hashes every file except `.git`/
   `__pycache__`; `expected_manifest` (:648-660) builds the shadow from
   the fixture's own files with `calc.py` replaced by fixed bytes;
   the parent diffs them and demands `diff == []` (:1280-1284), plus
   host-run pytest green (:1285-1286).
2. Git: exactly 2 subjects, fix commit count == 1, HEAD == fix,
   clean porcelain (:1289-1299) — zero duplicated committed effects,
   by count, not by exit code.
3. Ledger (parent re-opens the throwaway DB read-only, no Runtime
   boot, :1061-1121): both hash chains verify, exactly one interrupted
   close of the killed tool, none left open (:1308-1327).
4. Classification gate: parent reads post-kill live bytes itself
   (:1237-1243) and demands the exact resume-note token `: <state> —`
   (:1337-1377) — exact-token matching precisely because `applied`
   is a substring of `not_applied` (comment at :1329-1334). D-6 holds:
   non-file tools must classify `unknown` and resume never emits a
   `bash` turn (:915-957).

None of this is "exit 0" passthrough: the resume child's result file
is one input among several independently-sourced ones.

### (6) Linux 20x NOT-RUN honestly stated — VERIFIED [VERIFIED]

`drill_iterations` prints "Windows TerminateProcess subset (Linux/WSL2
20x kill -9: NOT-RUN here)" on Windows (:1432-1439); impl report
§§11/14/15/17 repeat it. My own runs printed the same header. No
POSIX counts invented anywhere. Consistent with plan N3.2's
acceptance ("20 of 20 ... or the failures are filed",
docs/2026-11-09-next-spec.md:187) remaining unmet on this host —
the Windows 5x subset is the documented interim gate, and the code
makes it structurally impossible to read a Windows pass as the 20x
result. [ASSUMED: acceptance intent — plan text quoted above supports
it; I did not re-litigate N3.2 acceptance scope, which is ORCH's call.]

### (7) `dry_run` byte-identical vs 2d7f9f6 — VERIFIED [RAN]

AST-extracted function text comparison old-vs-new:
`dry_run` identical (13252 bytes both, sha256 prefix 3934c955651c4903).
All other pre-existing top-level defs identical too: `sha256_hex`,
`is_hex64`, `run_pytest`, `git`, `git_apply_patch`, `ScriptedModel`.
`git diff 2d7f9f6 4c999a7` hunks confirm: module docstring, imports,
appended drill section, `main()` dispatch — no pre-existing body
modified. New-only defs are exactly the drill machinery (list in
evidence). No regression surface in the N3.1 evidence.

## §16 self-review assessment: HONEST

Read after the blind pass. Checked each box against my own evidence:

- "Diff re-read ... write-set respected; src/ and tests/ untouched;
  dry_run/helpers/ScriptedModel byte-identical" — TRUE [RAN §(7)]. The
  `git show --stat` proves write-set (1 code file). One sloppy phrase
  (§5 "...`_fixture_pair`-adjacent helpers..."): `_fixture_pair` is
  NEW, not pre-existing, so it cannot be "identical" — but no
  pre-existing helper was changed, so the substance is right. NIT.
- "after-effect signal race root-caused and fixed by construction" —
  TRUE [VERIFIED scripts/drill_kill_resume.py:772-790]. The seed-14
  git_commit(after) behaviour in my own run (zero resumed commits)
  corroborates the fix.
- "Findings none above MINOR" — I concur (2 MINOR below, both
  cosmetic/robustness, neither affecting verdict validity).
- "Independent review ... performed structured self-review instead and
  state the gap openly" — disclosed, and this review now closes it.
  Not disguised as a completed review.
- §7 sabotage paragraph plainly says checks were "verified by reading
  the check, not by sabotaging the script" — the opposite of
  overclaiming; and the one live mutation (signal ordering) WAS
  exercised end-to-end per §7, confirmed by my seed-14 run.
- §14 limitations (POSIX unexercised, parked windows cannot tear
  bytes, grandchildren can briefly outlive the kill) — all accurate;
  the grandchildren point is real (proc.kill kills the agent only)
  and correctly scoped as out of scope for the Windows subset.

The self-review does not overclaim. Its one factual soft spot is the
implementation report §11's "`mypy src` -> 3 errors, all pre-existing"
line — I reproduced exactly those 3 errors [RAN], so even that is
accurate (host-folded Windows substitutes, source untouched).

## Findings

### MINOR-1: `_spawn_child` leaks log file handles (one per child)
[VERIFIED scripts/drill_kill_resume.py:1021-1031] `log_fh` is opened
and passed as the child's stdout/stderr but never stored or closed
(2 per iteration, forever in the parent). Passes still removed their
tree on this host (no error surfaced in my runs), but on platforms
that refuse unlinking open files `shutil.rmtree` in the `finally`
(:1403-1409) could fail on a passing iteration and abort the drill
loop with a traceback. Fix: keep the handle on a small closeable
object and close it after `proc.wait()`, or open the log file in
`child_fresh`/`child_resume` directly. Not a verdict-integrity issue.

### MINOR-2: Drill oracle never asserts `isolation_level`
[VERIFIED scripts/drill_kill_resume.py:1061-1121] `_read_ledger_verdict`
summarizes tool/call_id but not `isolation_level`, and
`_one_drill_iteration` never checks it. The `_KillWindowTool`
`__getattr__` delegation (:759-764) is therefore load-bearing but
unpinned on the drill path: a regression that made wrapped shell
calls attest `NONE` would still pass every iteration (the dry-run
pins isolation for the unwrapped path only). Given ADR-0046 ("never
the requested level") is the point of the delegation comment
(:721-727), the oracle should assert bash PROPOSED rows attest
`PROCESS_ISOLATION` on the fresh lineage, mirroring the dry-run
check (:422-425). Small, targeted addition; suggested, not required
for N3.2 acceptance.

### NIT-1: Stale module-docstring claim
[VERIFIED scripts/drill_kill_resume.py:5-6] "`--dry-run` (default) —
the only mode that runs today." `4c999a7` made `--iterations` a
running mode; the sentence is now wrong. The §5 "docstring scrubbed"
claim covers the first line only. One-line fix; no behavioural impact.

### NIT-2: `--child --kill-at` unvalidated (hidden flags only)
[VERIFIED scripts/drill_kill_resume.py:1481] A hand-invoked
`--child` with a negative or oversized `--kill-at` parks nothing and
the parent watchdog fails the iteration loudly — the failure mode is
safe, so this is unreachable in practice (parent always generates
`randrange(len(TOOL_ORDER))`). Not worth more than a note.

## Out-of-scope observations (not defects of this commit)

- `mypy src` 3 errors at src/paa/sandbox/subprocess_backend.py:548
  (Windows host folding of `killpg`/`getpgid`/`SIGKILL`). Matches
  impl report §11 verbatim [RAN]. AGENTS.md's "baseline zero" is
  stated for the CI platform; on this Windows host mypy reports 3.
  Touches nothing in 4c999a7's diff. Filed for ORCH visibility only.
- Working tree currently shows COMMS.md modified + untracked docs
  (see `git status`) — not mine, not this commit's; impl report §13
  already records it. I touched only my report file.

## Long-run test (standing order 8)

- Source: driver reads fixture, throwaway ledger, git state it made.
  No external input; `--seed` any int safe (`randrange` bounds the
  index); `--iterations < 1` refused exit 2 (:1530-1533) [VERIFIED].
- Load: ~10-20 s/iteration observed (my 5- and 6-iteration runs:
  each child boots a Runtime + pytest). Bounded watchdogs (120/180 s)
  kill hung children; logs are files, not pipes, so no block risk.
  MINOR-1's fd leak grows at 2 handles/iteration — negligible at 20,
  unbounded only for absurd N.
- Hostile input: signal/marker/result JSON validated by checks
  before use (correlation match, tool/index match); a lying child
  cannot pass — the oracle re-derives tree, git, and ledger state
  itself. Non-JSON marker would crash with traceback + kept roots
  (loud failure, desired).
- Operability: failing iterations print workspace/home paths and
  keep roots (:1396-1409); `--keep` preserves everything; rollback
  is `git revert` of one script file (skeleton restored verbatim,
  since pre-change behaviour is the only thing reverted).
- Reversal: revert 4c999a7 -> `--iterations` exits 2 again (drill
  absent). Break the wrap seam (e.g. dispatch no longer via
  `agent.tools`) -> "fresh child reached the kill window" FAIL
  every iteration by construction (:1196-1220). Break the token
  gate -> classification FAIL. Break oracle conjuncts -> targeted
  FAILs named in impl §7, which I confirmed by reading the checks.
  Which test fails if the fix is reverted: the drill itself — it
  ceases to exist.

## Commands run (verbatim results)

- `[RAN .venv/Scripts/python.exe scripts/drill_kill_resume.py --dry-run -> 47 PASS, 0 FAIL, exit 0]`
- `[RAN .venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 5 -> 5/5 passed, exit 0]`
- `[RAN .venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 6 --seed 14 -> 6/6 passed, exit 0]`
- `[RAN .venv/Scripts/python.exe -m pytest tests/conventions -q -> 34 passed, exit 0]`
- `[RAN .venv/Scripts/python.exe -m ruff check src tests -> All checks passed!, exit 0]`
- `[RAN .venv/Scripts/python.exe -m mypy src -> 3 errors, all subprocess_backend.py:548 (pre-existing, outside diff), exit 1]`
- NOT-RUN: Linux/WSL2 `--iterations 20` (no Linux host here; driver
  prints NOT-RUN itself), full pytest suite (standing order 16 —
  scoped checks only). [UNKNOWN]: POSIX SIGKILL returncode -9 behaviour.
