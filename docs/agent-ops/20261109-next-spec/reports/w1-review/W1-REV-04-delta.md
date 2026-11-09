# W1 REV-04 delta — code review of W1-IMPR-03 + W1-IMPR-04 (dbff769 + 1c3d0bf)

```yaml
ORCH-DIGEST: |
  agent: W1-REV-04 | task: T-N1-delta-review | status: DONE
  code_commits: dbff769, 1c3d0bf
  report_commit: PENDING
  verdict: APPROVE-DICTATED-SCOPE (all 6 claims verified; zero new defects found; both MAJOR-3s correctly left untouched)
  tests: scoped 207 passed (63 fs_read + 144 write/git/patch); conventions 34 passed; ruff 0; mypy 3 pre-existing
  blockers: none
```

## Method

Blind pass first: only the two code diffs + current source were inspected and probed (§1 claims 1–6) before re-reading `W1-IMPR-03-improve.md` and `W1-IMPR-04-improve.md`. Every probe below is a fresh [RAN] run this session, not a copy of the implementers' tables. Conventions/ruff/mypy results are from runs at HEAD `48df671` (current branch tip) on the unmodified tree.

## Blind-pass verification (independent probes)

### (1) grep single-file

[VERIFIED src/paa/coder/fs_read_tools.py:550-560] `_Grep.run` now branches on `root.is_file()` and searches the one jailed file via `candidates = iter([(single_rel, root)])` instead of `os.walk` on a file (which yields nothing). The candidate still flows through the normal per-entry loop (glob-filter, re-jail `assert_inside_workspace`, `is_protected`, capped read, binary skip) — [VERIFIED :566-596].

[RAN probe: temp workspace, only.txt=`first\nneedle here\nlast needle\n` -> `count 2, files_scanned 1, matches[0]={path: only.txt, line: 2, text: needle here}` -> **PROBE1-PASS**] — >0 matches, exactly the brief's assertion.

### (2) walk-cap marking

[VERIFIED src/paa/coder/fs_read_tools.py:636-687] `_walk` takes `cap_hit: list[bool] | None = None`; both budget sites (`seen > _MAX_WALK_ENTRIES`) now set `cap_hit[0]=True` before `return`. Both callers fold it in: `_Glob` [VERIFIED :445,461-475] and `_Grep` [VERIFIED :549,616-631] emit `"truncated": truncated or walk_hit` and `"note": _truncation_note(...)` with the shared helper [VERIFIED :214-233] producing `"walk capped at {N} entries; results are incomplete"`. The out-param is the right call for a generator boundary (a `return` value rides `StopIteration.value`, invisible to `for`); the helper is the single source for the walk phrase so both tools cannot drift.

[RAN probe: `_MAX_WALK_ENTRIES=2`, 3 needle files -> grep `count 2, files_scanned 2, truncated True, note "walk capped at 2 entries; results are incomplete"`; glob `count 2, truncated True, same note` -> **PROBE2-PASS**] — the budget number is interpolated, so under production defaults it truthfully reads `10000`.

### (3) pathspec refusal

[VERIFIED src/paa/coder/git_tools.py:126-134] `_PATHSPEC_META_CHARS = ("*", "?", "[", ":")`; [VERIFIED :348-376] `_jail_paths` refuses any raw path containing a metacharacter (before jail), any `resolved.is_dir()`, and any canonical relative form git will actually receive (catches a literally meta-named directory like `*.txt/ok.txt`). Refusals are all-or-nothing (first refusal returns before git runs) [VERIFIED :346-376] and are shared by both consumers: `_GitStage` [VERIFIED :559-562] and `_GitDiff` [VERIFIED :486-489]. Tool descriptions document the new refusal [VERIFIED :451-458,525-533]; the module docstring states the checked-unit ≡ acted-on-unit invariant [VERIFIED :25-32].

[RAN probe: temp git repo seeded with sub/.env.local, sub/ok.txt -> `git_stage(sub)` -> `directory not allowed`, staged index empty; `git_stage(.)` -> `directory not allowed`, empty; `git_stage(:/)` -> `pathspec magic or glob is not allowed`, empty; `git_stage(*.txt)` -> `pathspec magic or glob is not allowed`, empty; `git_diff(sub)` and `git_diff(*.txt)` -> same errors -> **PROBE3-PASS**] — all four brief shapes error and stage nothing, diff identical.

Trade-off noted, accepted: POSIX-legal `?`/`[` filenames and Windows `C:/...` drive paths are now refused on the colon scan. The safe direction, and documented [VERIFIED git_tools.py:131-133]. No live caller exists yet (N1.5 wiring not landed — confirmed by absence of git-tool registration in `coder/agent.py`), so the refusal cannot break a shipped caller.

### (4) MODIFY trailing newline

[VERIFIED src/paa/validation/patch.py:588-608] the planner now decides the trailer from the new side: CREATE → absence of the new-side marker; MODIFY with any new-side marker → no trailer; MODIFY reaching the new end of file (or an explicit old-side marker) → trailer; otherwise inherit the prior state. The unconditional prior-inheritance heuristic is gone.

[RAN probe: prior `b"a\nb\nc"` + `@@ -3,1 +3,1 @@ -c +d` -> `new_content = b"a\nb\nd\n"`; prior `b""` + insert-hunk `+hello` -> `b"hello\n"` -> **PROBE4-PASS**] — overwriting empty/no-trailing files with newline-terminated content preserves the newline exactly; the complement (mid-file hunk on a no-trailing prior stays no-trailing) is pinned by the implementer's negative-control test in the suite re-run below.

### (5) edit result cap

[VERIFIED src/paa/coder/fs_write_tools.py:296-306] `EditFileTool.run` now calls `_encode_new_content(new_full, relative)` on the composed result before synthesis — the same helper/gate `write_file` uses — so the journal/memory bound applies to edit results too (and NUL-composed content is refused on the same gate).

[RAN probe: `b"x\n"` + edit 1 char -> 300 KiB -> `{ok: False, limit_exceeded: True, size: 307201}`, file bytes unchanged, journals empty -> **PROBE5-PASS**]

### (6) no regressions

[RAN `.venv\Scripts\python -m pytest tests/coder/test_fs_read_tools.py` -> **63 passed in 2.00s**]
[RAN `.venv\Scripts\python -m pytest tests/coder/test_fs_write_tools.py tests/coder/test_git_tools.py tests/validation/test_patch.py` -> **144 passed in 44.29s**] (63+144 = 207; matches the brief's split exactly)
[RAN `.venv\Scripts\python -m ruff check src tests` -> **All checks passed!**]
[RAN `.venv\Scripts\python -m mypy src` -> **3 errors in 1 file**, all `src/paa/sandbox/subprocess_backend.py:548` (`killpg`/`getpgid`/`SIGKILL` Windows-fold), the declared pre-existing baseline, none in any touched file]
[RAN `.venv\Scripts\python -m pytest tests/conventions` -> **34 passed in 152.02s**]

## MAJOR-3 check — both correctly deferred, none leaked

Two different findings carry the same label in the two source reviews; the brief asks for both:

1. **W1-REV-03 MAJOR-3 (`git_stage` PLAN-glob, N1.5-owned): correctly NOT fixed here.** [VERIFIED `git diff --name-only dbff769^..dbff769` -> only fs_read_tools + its test; `1c3d0bf^..1c3d0bf` -> git_tools/fs_write_tools/patch + 3 test files] — `permissions.py` is untouched by both commits and still has no `git_stage*` entry [VERIFIED grep `src/paa/coder/permissions.py` -> only `DEFAULT_MUTATING_TOOLS` at :31,:140, no `git_stage`]. The implementer states the deferral plainly (W1-IMPR-04 §13.2) rather than smuggling the glob in. This is the correct posture: the PLAN-glob belongs to the policy-merge commit with the N1.5 wiring, and adding it here would have widened a security rule without the owner fix context.
2. **W1-REV-02 MAJOR-3 (default PROCESS shell uncontained, documented): correctly NOT leaked.** Neither diff touches any shell module [VERIFIED name-only lists above contain zero shell files]; the fs_read diff is pure stdlib file I/O, the git/patch diffs add no subprocess calls. W1-IMPR-03 §13 explicitly excludes MAJOR-3 (shell PROCESS) and W1-IMPR-04 §13.2 excludes MAJOR-3 (git_stage glob) — the two findings were never confused with each other, and the shell's documented `filesystem_confined: False` / `isolation_level: PROCESS` posture is left exactly as recorded.

## Post-blind comparison with implementer reports

No material disagreements found. Where the reports claim fail-before mutation evidence I did not re-mutate (the 18 new regression tests are in the green runs above, and their assertions name the bug values, e.g. `count == 2`/`files_scanned == 1`, `limit_exceeded is True` + unchanged bytes + empty journals, empty-index after every refusal); where the reports note boundaries (exact-cap `>` vs `>=`, limit-beats-walk note join, `>=` EOF-coverage) I confirmed the same readings in the source. One point of report hygiene: W1-IMPR-04 §11 claims conventions-5-only (host timeout); this review's full conventions run (34 passed) supersedes that — no action needed.

## Long-run test

- **Source:** each fix sits at the source — candidate-set shape in grep, the generator signal in `_walk`, the shared `_jail_paths`, the planner trailer rule, the shared `_encode_new_content` gate — not per test site.
- **Load (100×):** walk budget unchanged and now labelled; single-file grep is one capped read; pathspec checks add two `in` scans + one stat per path next to an existing canonicalisation; the edit cap restores the 256 KiB journal bound. [ASSUMED from code; no load test run]
- **Hostile input:** `.`/`sub`/`:*`/`*.txt` shapes, meta-named directories, batch atomicity, protected single file (jail-then-protect precedes `is_file()`) all covered by probes/tests above. [VERIFIED via probes + suite]
- **Operability:** refusals are frozen-shaped JSON error values with greppable reason strings; rollback is `git revert` of two single-hunk commits; no migrations. [ANALYSED, no rollback executed this session]
- **Reversal:** reverting any one fix fails its targeted tests for the right reason (implementers' documented mutations, consistent with the passing assertions seen here).

## Verdict

**APPROVE.** All six claims verified independently; zero new defects; scope discipline perfect on the two MAJOR-3s; no regressions in the scoped suites or the repo gates (ruff 0, mypy 3-pre-existing, conventions 34). No changes requested.
