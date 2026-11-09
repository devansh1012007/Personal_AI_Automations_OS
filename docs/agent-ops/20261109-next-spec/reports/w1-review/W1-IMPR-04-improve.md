<!-- ORCH-DIGEST:START -->
agent: W1-IMPR-04 | task: T-N1-2/4-IMPR | status: DONE
code_commits: 1c3d0bf60113f0782f148941d6611bca46011b07 | report_commit: d5c5606 (docs-only; this file's first commit)
files_changed: 6 (src/paa/coder/git_tools.py, src/paa/coder/fs_write_tools.py, src/paa/validation/patch.py, tests/coder/test_git_tools.py, tests/coder/test_fs_write_tools.py, tests/validation/test_patch.py)
contracts_changed: git_stage/git_diff refuse directories and glob/magic pathspecs (reason strings "directory not allowed" and "pathspec magic or glob is not allowed"); EditFileTool refuses over-limit results with limit_exceeded payload shape (same as write_file); PatchPlan entries for MODIFY now describe the new-file trailing newline from the patch's new side
tests: added 12, changed 0; scoped run: pass (144 combined: 49 fs_write + 43 git + 52 patch)
ownership_violations: none
deferred_dependencies: docs path:line re-number after patch.py +18-line shift (docs/modules/validation.md:46 :675->:693 :841->:859; docs/data/data-lifecycle.md:191 :675->:693) — NEED posted, ORCH-owned; MAJOR-3 git_stage PLAN glob N1.5-owned per brief (NOT added; NEED note below)
out_of_scope_findings: 2 (NEED docs re-number; edited-file line-shift confirmed non-blank so docs-references lock still green)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: full conventions-34 suite not run (times out on this host per W1-REV-03 §9); only test_docs_references.py (5 passed)
confidence: high (all three fixes mutation-verified fail-before/pass-after; scoped gates green)
last_comms_line_read: 2026-11-09T16:30:00Z (W1-REV-03 DONE)
<!-- ORCH-DIGEST:END -->
# Implementation report — W1-IMPR-04 — T-N1-2/4-IMPR

## 1. Task and acceptance criteria (restated)

Fix three review findings from W1-REV-03, in one agent to keep the tool
files coherent (brief §3):

- **BLOCKER-1** (`git_stage`/`git_diff` pathspec expansion): git expands
  directory pathspecs recursively, `.` to the whole tree, and
  glob/magic pathspecs to whatever they match, while `_jail_paths`
  checked only the named path. Either refuse directory/`.`.glob/magic,
  or expand via `git ls-files` and run `is_protected` on every
  expansion all-or-nothing. Tests for the 4 probe shapes
  (`sub`, `.`, `:/`, `*.txt`) that fail before (staged `.env`) and pass
  after (error, stage nothing). No push/force/config/hooks regression.
- **MAJOR-1** (`write_file` MODIFY trailing newline): planner
  `wants_trailing` inherited the prior file's trailing state; fix in
  the planner (treat the new side as the trailer) or in synthesis.
  Overwrite-empty and overwrite-no-trailing tests fail before / pass
  after; CREATE behaviour (TASK-2 test) stays green.
- **MAJOR-2** (`edit_file` result cap): compute the resulting body's
  UTF-8 length and refuse with the `limit_exceeded` payload shape if
  it exceeds `MAX_CONTENT_BYTES`, before synthesis. A 1-char → 300 KiB
  test fails before / passes after.

Acceptance: 3 test groups fail-before/pass-after; scoped pytest of the
three files passes; ruff 0; mypy 3 (pre-existing baseline); conventions
34. Explicitly NOT attempted: MAJOR-3 (`git_stage*` glob — N1.5-owned;
NEED note instead), MINOR/NITs, hand-built-plan revalidation
(defence-in-depth, deferred per IMPL-04 §13.3).

## 2. Verification that the problems exist (evidence gathered myself)

- **BLOCKER-1.** [VERIFIED `src/paa/coder/git_tools.py:314-333` @ work
  from brief] `_jail_paths` validates each named path as one fs path,
  then `git add -- <paths>` at `:516` hands git the relative form. Git
  expands directory/glob/magic pathspecs, and `is_protected`
  (workspace.py:279) inspects only the named path's segments. Re-ran
  the reasoning against W1-REV-03's recorded probes P4–P7 [VERIFIED
  reports/w1-review/W1-REV-03-review.md §5]: `["sub"]`, `["."]`,
  `[":/"]`, `["*.txt"]` all staged protected descendants in a fixture
  repo. Additionally verified [VERIFIED workspace.py:239-278] that
  `Path("*.txt")` canonicalises as a literal in-workspace path, so no
  jail layer catches the metacharacter — the check is missing at the
  boundary, not inside it.
- **MAJOR-1.** [VERIFIED `src/paa/validation/patch.py:588-590`]
  `wants_trailing = had_trailing_newline or action is CREATE`, then
  `if any(h.new_no_newline): wants_trailing = False`. For MODIFY the
  new side's own trailer is never consulted: overwriting `b""` or
  `b"old"` with `*_newline` content cannot be represented, because the
  synthesis (`fs_write_tools.py:489,498`) only encodes the no-newline
  marker on the new side. Matches W1-REV-03 probes P1/P2 (`b"hello"`,
  `b"new"` instead of newline-terminated).
- **MAJOR-2.** [VERIFIED `src/paa/coder/fs_write_tools.py:272-299`]
  `EditFileTool.run` computes `new_body` by string replacement and goes
  straight to `_split_lines`/synthesis — no size check on the result.
  `WriteFileTool` checks content (`:217` via `_encode_new_content`)
  and `ApplyPatchTool` checks patch size (`:335`), but the edit
  result path has no gate. Matches W1-REV-03 probe P3 (1 char → 300
  KiB, file 307,201 bytes written).

## 3. Root cause

1. **Checked unit ≠ acted-on unit (git).** The path check was
   source-level for a namespace (one named path) that git does not
   use: git's pathspec engine rewrites directories to recursive globs
   and treats metacharacters as pattern syntax. The invariant "every
   path git touches passed the jail" was false at the git boundary.
2. **Planner heuristic described the wrong side (trailing).** The
   trailing-newline decision described the *prior* file for MODIFY,
   while the only representation in the diff wire format is the *new*
   side's marker state. When prior and new trailers disagree in the
   "gaining" direction, the planner silently dropped the difference.
3. **Result body never passed through the size gate (edit).** The cap
   was enforced per-input (`content` for write, `patch` for apply)
   but never on the composed result, so the one tool that manufactures
   new content by substitution could bypass `MAX_CONTENT_BYTES`, the
   memory bound, and the journal bound in a single call.

## 4. Approach chosen and why (alternatives rejected)

- **BLOCKER-1 → refuse, don't expand.** The brief allowed either.
  Expansion (`git ls-files` + `is_protected` on every expansion,
  all-or-nothing) was rejected: `git ls-files` enumerates the *index*,
  not the worktree paths git would stage from globs (and untracked
  files a directory stages are not all in the index); the expansion
  would need `git ls-files --others --cached` plus pathname matching
  of the pattern itself. Refusal is smaller, exact (git only ever
  receives the paths we checked), and matches the tool's contract:
  paths are *file names* to stage, not patterns. The refusal is
  documented in the module docstring, both tool descriptions, and the
  error payload reason.
- **MAJOR-1 → fix in the planner (root-cause layer), not synthesis.**
  The brief allowed either. Synthesis-only would need a new
  old-side-marker signal threaded through `_whole_file_patch` (which
  doesn't currently receive the old trailing flag) *and* a planner
  rule for it anyway. Planner rule: for MODIFY, if any hunk carries a
  new-side no-newline marker → no trailer (unchanged); else if a
  hunk's new side reaches the new end of file → newline-terminated
  (this is the correct unified-diff reading of marker absence at
  EOF, and it covers the whole-file synthesized hunks byte-for-byte);
  an explicit old-side marker without a new-side marker also means
  gaining the trailer; otherwise inherit the prior state (mid-file
  hunks that never touch EOF keep the old behavior — including the
  no-trailing-preserved case, pinned by a new negative-control test).
  `CREATE` is unchanged in effect (always terminated unless the new
  side carries the marker — the previous expression computed the same
  truth table since `had_trailing_newline` is False for CREATE).
  Alternatives rejected: "absence of marker ⇒ trailer for all MODIFY"
  (too strong — would append a newline to mid-file edits of
  no-trailing files); synthesis-only (thicker plumbing, same rule
  needed in the planner regardless).
- **MAJOR-2 → reuse `_encode_new_content` on the composed result.**
  Same helper `WriteFileTool` already calls (`:217`), same payload
  shape (`limit`/`size`/`limit_exceeded`), before synthesis per the
  brief. This also refuses a NUL byte composed by the edit — free and
  consistent (the read side would refuse the resulting file as
  binary, and write_file refuses NUL new content). No new error path.

## 5. What changed (per file)

**`src/paa/coder/git_tools.py`** (+~45 lines)
- `_PATHSPEC_META_CHARS` constant (`("*", "?", "[", ":")`) with rationale.
- `_jail_paths`: refuses a raw path containing any metacharacter
  (before jail), refuses `resolved.is_dir()` (after jail-then-protect,
  before git), and scans the canonical relative form (what git
  actually receives — catches a directory literally named with
  metacharacters). Reason strings follow the house frozen style:
  `"pathspec magic or glob is not allowed"` and
  `"directory not allowed"`. All-or-nothing preserved: first refusal
  aborts before any git invocation.
- Module docstring §2 and `_GitStage`/`_GitDiff` descriptions document
  the new refusals. `_relative_paths` is reused for the canonical
  scan (no new path arithmetic).

**`src/paa/validation/patch.py`** (+18 lines in `_plan_file`)
- New trailing-newline rule for MODIFY (§4 above), replacing the
  inherit-prior heuristic. Comment records the DECISION and why a
  non-EOF hunk still inherits.

**`src/paa/coder/fs_write_tools.py`** (+9 lines in `EditFileTool.run`)
- `_encode_new_content(new_full, relative)` on the composed result
  before `_split_lines`/synthesis, with the DECISION comment (§4).

**Tests** (12 new, 0 changed)
- `tests/coder/test_git_tools.py` — `TestPathspecExpansion` (7):
  stage-directory, stage-`.`, stage-pathspec-magic (`:/`),
  stage-glob (`*.txt`), named-file-inside-directory control,
  batch all-or-nothing (`sub/ok.txt` + `sub`), diff-directory. Every
  refusal asserts `git diff --cached --name-only` is empty afterwards.
- `tests/coder/test_fs_write_tools.py` (3): `TestWriteFile` gains
  `test_overwrite_empty_file_with_trailing_newline_is_exact` and
  `test_overwrite_no_trailing_file_gains_newline`; `TestEditFile`
  gains `test_edit_result_over_size_is_refused` (asserts
  `limit_exceeded is True`, `size > MAX_CONTENT_BYTES`, bytes
  unchanged, journals empty).
- `tests/validation/test_patch.py` (2): planner-level
  `test_a_modify_gaining_a_trailing_newline_keeps_it` and the
  negative control `test_a_modify_not_reaching_eof_inherits_trailing_state`.

## 6. Blast radius and contracts

- **git tools**: `_GitStage` and `_GitDiff` share `_jail_paths`, so both
  are fixed by one change; `_GitStatus` (takes `paths`? no — it ignores
  arguments) and `_GitCommit` (no paths) are untouched. Live callers
  within the repo: none yet (N1.5 wiring not landed — confirmed at
  review time; `CoderAgent.build` does not wire git tools). The new
  refusals are new error values, not new success shapes; staged
  payload shape unchanged for named files.
- **Planner**: `_plan_file` is the single source through which all
  three write tools (and any future tool) plan MODIFY. The rule change
  alters `new_content` bytes for patches that gain a trailer at EOF;
  mid-file-only patches on no-trailing files are byte-identical to
  before. No public interface changed (same `PatchPlanEntry` fields,
  same `patch_sha256` definition).
- **Edit tool**: refusal payload shape matches the pre-existing
  `_encode_new_content` shape exactly (N1.2-SIZE-LIMIT); callers
  already handle `limit_exceeded` from write_file.
- **Known approximation (documented in §14):** EOF-coverage is computed
  from declared hunk headers (`new_start - 1 + len(target_lines) >=
  len(new_lines)`). The `>=` (not `==`) deliberately covers
  clamped pure-insertion hunks whose declared position is past EOF;
  well-formed hunks compute exactly. A hunk whose *declared* position
  overshoots due to author error could claim EOF coverage it doesn't
  have — but that hunk would already fail to locate in `_apply_hunks`
  unless it's a pure insertion (and clamped insertion IS at EOF).

## 7. Tests (how each confirmed to fail without the fix)

Fail-before was verified by hand-reverting each fix in turn (git
checkout/restore is forbidden, so the hunks were restored by editing
the files back — the mutation was then re-applied and the suite
re-run green; see §11):

- **BLOCKER-1** — neutralized `_PATHSPEC_META_CHARS` to `()` and the
  `is_dir` check to `if False`, ran `TestPathspecExpansion`:
  **6 failed, 1 passed** (the passing one is
  `test_stage_named_file_inside_directory_still_stages`, a regression
  control that must pass both ways). Failure mechanism: `payload["reason"]`
  assertions tripped and (in the defect's real form per W1-REV-03
  P4–P7) the index would have accumulated protected paths — the new
  tests assert the empty index explicitly.
- **MAJOR-1** — restored the pre-fix 3-line heuristic, ran the
  newline tests: `test_overwrite_empty_file_with_trailing_newline_is_exact`,
  `test_overwrite_no_trailing_file_gains_newline`, and
  `test_a_modify_gaining_a_trailing_newline_keeps_it` **failed**;
  the negative control
  (`test_a_modify_not_reaching_eof_inherits_trailing_state`) passed
  both ways by construction.
- **MAJOR-2** — removed the `_encode_new_content` call, ran
  `test_edit_result_over_size_is_refused`: **failed** (300 KiB result
  written; `limit_exceeded` absent).

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- **Source.** Fixes sit at the sources, not per call site: pathspec
  refusal in the shared `_jail_paths` (both git consumers inherit
  it), trailing state in the planner (all three write tools
  inherit it), the result gate in `EditFileTool.run` via the shared
  `_encode_new_content` helper. Nothing was patched per test.
- **Load (100×, concurrency, partial failure).** Git bounds unchanged
  (64 KiB output cap, 120 s kill, 100-path argv cap). The new per-path
  work is two `in` scans and one `is_dir` stat — negligible next to the
  existing per-path canonicalisation. `edit_file` result cap restores
  the 256 KiB journal/memory bound the probe violated; the bloated-file
  self-deadlock (uneditable after) is now impossible via this tool.
  Partial failure: refusal happens before any subprocess/apply, so
  batch semantics are "stage nothing", never "stage half".
- **Hostile input.** `sub`/`./sub`/`sub/.`/`sub/` variants all resolve
  to the directory → refused. Magic hidden in a directory name
  (`*.txt/ok.txt`) is caught by the canonical-relative scan (the raw
  string is clean there). Absolute in-workspace paths containing a
  drive `:` (`C:/...`) are refused by the metacharacter scan — a
  deliberate, documented trade-off (Windows pathspec colon); git
  receives only canonical relative forms anyway. Filenames that
  genuinely contain `?`/`[` on POSIX are refused (safe direction;
  Windows forbids `?`/`*` outright). A non-existent *file* path still
  falls through to git's clean nonzero-exit error (unchanged).
- **Operability (observe, roll back, migrate).** All refusals are JSON
  error values with frozen reason strings (model-reasonable, greppable
  in logs). `git_commit` continues to commit only the checked index;
  the no-push/no-hooks surface is untouched (all pre-existing hostile
  tests re-run green). Seed values recorded: reason strings for the
  two new refusals.
- **Reversal.** Revert any one fix and its targeted tests fail for the
  right reason (§7, verified). Revert the planner rule and the two
  overwrite tests + one planner test fail; revert the edit gate and
  the cap test fails; revert the pathspec guards and 6 of 7 expansion
  tests fail. The negative controls (named subdir file, mid-file
  no-trailer) pass before and after, proving the refusal is the
  narrowest consistent one.

## 9. Edge cases and abuse scenarios

- `git_stage(["sub/"])`, `["sub/."]`, `[":/."]` — canonicalise to a
  directory or carry `:`, refused either way.
- `git_stage(["sub/.env.local"])` — explicitly protected before the
  dir check (unchanged behavior, still refused).
- `git_stage(["*.txt", "ok.txt"])` — first path trips the magic scan;
  batch all-or-nothing stages nothing.
- `git_stage` of a symlink-to-directory — `resolved` is canonical, so
  `is_dir()` follows it → refused. Symlink-to-file inside the jail
  still stages (named file; jail resolves it).
- `write_file` overwrite of a CRLF file gaining a newline: planner
  joins with the file's own `\r\n`, so the gained trailer is CRLF —
  consistent with the pre-existing "preserve the file's newline"
  semantics; no behavior change on that axis.
- `edit_file` result exactly at 256 KiB passes (`>` comparison in the
  helper, unchanged); the test uses 300 KiB.

## 10. Performance and security

- No new subprocess calls (unlike the rejected expansion alternative,
  which would have added a `git ls-files` per call). One `is_dir`
  stat per path is the only new I/O, dwarfed by the existing
  canonicalisation per path.
- Security posture: the jail-then-protect *order* is unchanged; the
  two new checks are layered onto the same function so no future
  tool reusing `_jail_paths` can forget them. `MAX_CONTENT_BYTES` is
  now enforced on all three ways content reaches the tree (write
  content, apply patch text, edit result).

## 11. Commands run and results (verbatim summaries)

| Command | Result |
|---|---|
| `pytest tests/coder/test_git_tools.py tests/coder/test_fs_write_tools.py tests/validation/test_patch.py -q` | 144 passed in 38.41s (49 + 43 + 52) |
| Mutation: pathspec guards neutralized → `pytest tests/coder/test_git_tools.py::TestPathspecExpansion -q` | 6 failed, 1 passed (control) |
| Mutation: pre-fix planner heuristic restored → newline subset runs | 3 failed (2 fs_write + 1 planner), negative control passed |
| Mutation: `_encode_new_content` call removed → `-k over_size` | 1 failed |
| `ruff check` on all 6 touched files | All checks passed! |
| `mypy` on `git_tools.py`, `fs_write_tools.py`, `patch.py` | Success: no issues found in 3 source files |
| `pytest tests/conventions/test_docs_references.py -q` | 5 passed (machine check green; see §13 for the semantic re-number) |

Full conventions-34 suite: NOT-RUN (timed out >120 s for W1-REV-03 on
this host; standing orders forbid the full test suite and this suite
brings a docs-ref dependency outside my write-set).

## 12. Deviations from plan/brief

1. **Metacharacter scan also runs on the canonical relative form.**
   The brief's probe shapes are all raw-string cases; scanning the
   canonical form is a strict addition (catches metacharacter-named
   directories) with no new refusal for the brief's shapes.
2. **`>=` instead of `==` for EOF coverage** (§6/§14) — covers the
   clamped-insertion case the codebase already accepts.
3. **MAJOR-2 rides the NUL check** in `_encode_new_content` (§4) —
   an edit composing a NUL via `new_str` is now refused like
   `write_file` refuses NUL content. Not in the brief; same gate,
   same shape, no test-time behavior relied on by existing tests
   (suite green).

## 13. Out-of-scope findings

1. **(NEED, posted) docs `path:line` re-number.** The `patch.py`
   change adds 18 lines at ~:588, shifting `PatchApplier.apply`
   (:675 → :693) and `compute_patch_sha256` (:841 → :859) cited by
   `docs/modules/validation.md:46` and `docs/data/data-lifecycle.md:191`
   — both outside my WRITE-SET. The conventions machine lock still
   passes (lines are non-blank), but the citations' semantics are
   stale; ORCH owns the re-number at the wave boundary (same pattern
   as `353e2c4` for W1-IMPL-04's shift).
2. **MAJOR-3 (`git_stage*` glob)** is N1.5-owned per the brief — not
   attempted; the git tools remain reachable only by direct
   construction, so BLOCKER-1's fix is what gates wiring.

## 14. Known limitations, risks, rollback notes

- **Files with literal `?`, `[`, or `:` in their names** (legal on
  POSIX) cannot be staged/diffed through these tools. Documented in
  the module docstring; a future literal-escaping pathspec would
  need git's `:(literal)` magic, which the same refusal currently
  excludes — that is a deliberate conservative choice.
- **Hand-built plans without `prior_sha256` still skip the TOCTOU
  re-check** (IMPL-04 §13.3 defence-in-depth, deferred per brief §6).
  Not touched here.
- **EOF-coverage heuristic** uses declared hunk headers; for
  sequentially applied multi-hunk patches `new_start` is in
  as-it-stands coordinates (per `_apply_hunks` docstring), so the
  comparison against the final length is exact for well-formed
  patches and conservative for drifted ones.
- **Rollback:** all three changes are single-hunk reversions in
  three files; the reversal test mapping is §8/§7. Nothing is
  persisted or migrated by this change.

## 15. Where I am least confident

The full conventions-34 suite (not run — host timeout + scoped-check
rule); I verified the affected lock (docs-references, 5 passed) but
the import-conventions sweep is outside what I can execute here.

## 16. Self-review checklist

- [x] Re-read the diff from scratch (3 src files + 3 test files);
  root-cause framing checked against W1-REV-03 §4 findings — all
  three fixed at the source layer, none masked.
- [x] Security: jail-then-protect order preserved; no new subprocess;
  no secret material in payloads; argv surface untouched.
- [x] Edge cases: §9 list re-checked against the code (directory
  spellings, metacharacter directories, symlink-to-dir, batch
  atomicity, exact-cap boundary).
- [x] Maintainability: reuses `_encode_new_content`,
  `_relative_paths`, house error shapes; no new helpers or
  abstractions.
- [x] Test rigor: mutation-verified (§7); negative controls included
  for both refusal directions.
- [x] No leftover artifacts: mutation edits restored and re-verified
  by the 144-test scoped run + ruff + mypy *after* restoration.
- [x] Ownership: only the 6 WRITE-SET files changed (sibling
  `fs_read_tools` edits by W1-IMPR-03 untouched).

## 17. Commits

- Code: `1c3d0bf60113f0782f148941d6611bca46011b07` (6 files).
- Report: this file's commit (docs-only; its SHA is recorded in the
  digest as backfilled per this run's precedent).
