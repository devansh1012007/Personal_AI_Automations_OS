<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-04 | task: T-N1-2 (N1.2) | status: DONE
code_commits: b97ff73a443d90fe843b4785fefc77a81130cb11 | report_commit: (separate docs commit following)
files_changed: 4 (src/paa/coder/fs_write_tools.py, src/paa/validation/patch.py, tests/coder/test_fs_write_tools.py, tests/validation/test_patch.py)
contracts_changed: PatchApplier.apply now refuses stale plans (ValidationError); new tool names write_file/edit_file/apply_patch for N1.5
tests: added 46, changed 1 (xfail removed); scoped run: pass (96 combined: 46 fs_write + 50 patch)
ownership_violations: none
deferred_dependencies: docs path:line re-number (validation.md:46, data-lifecycle.md:191) — NEED posted, ORCH-owned
out_of_scope_findings: 2 (apply path-revalidation gap note; sibling ruff E501 FINDING)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none — all scoped checks green except two documented out-of-scope items
confidence: high (fixes verified by focused tests + pre-fix semantics; reversal reasoning recorded)
last_comms_line_read: 2026-11-09T10:25:59Z (W1-IMPL-04 FINDING git_tools E501; line 42 of COMMS)
<!-- ORCH-DIGEST:END -->
# Implementation report — W1-IMPL-04 — T-N1-2 write tools (N1.2)

Status: DONE. Code commit `b97ff73a443d90fe843b4785fefc77a81130cb11`
(see §17 for the report commit). Brief and standing orders followed;
two items are deliberately deferred (§13): a docs `path:line`
re-number owned by ORCH (NEED posted), and a sibling's ruff finding
(FINDING posted).

## 1. Task and acceptance criteria (restated)

Write `src/paa/coder/fs_write_tools.py` with `write_file`, `edit_file`
(`old_str` exactly once — zero/multi-match fail without write) and
`apply_patch`. All three go `UnifiedDiffValidator.dry_run` →
`PatchApplier.apply(plan)`; each returns the patch SHA matching
`compute_patch_sha256`; journal kept per session in memory
(`PatchJournal`; ledger persistence is Wave 2). Add the prior-hash
re-check in `PatchApplier.apply` (INV: apply never compares
`prior_sha256` today — plans without the check inherit a TOCTOU).
Fix triage TASK-2 phantom newline in `UnifiedDiffValidator._plan_file`
(empty prior file must NOT gain a trailing newline);
`test_a_created_file_contains_exactly_what_the_diff_describes` passes
without xfail (remove the marker in the same commit). Every path is
jail-then-protect (ADR-0045). Tests `tests/coder/test_fs_write_tools.py`:
encoding/CRLF/BOM/binary refused by read side, edit semantics
(0/multi/exact + SHA), limits truncated+marked, protected denied.
Scoped acceptance: the two test files pass, `ruff` 0, `mypy` 3
pre-existing, conventions green.

WRITE-SET honoured: only the four listed files changed. READ-ONLY
files (`workspace.py`, ADR-0045) were read, not edited. FORBIDDEN
files untouched; TASK-3/TASK-4/TASK-5 were not observed in these
files and are noted in §13.

## 2. Verification that the problem exists (evidence gathered myself)

- [VERIFIED src/paa/validation/patch.py:672-727 @ b8570a6] `PatchApplier.apply`
  journal-writes each entry but never compares `entry.prior_sha256`
  against current file bytes — the TOCTOU the brief names is real.
  `prior_sha256` is populated by `_plan_file` (`:534,597`), so the
  re-check has inputs.
- [VERIFIED src/paa/validation/patch.py:567-572 @ b8570a6] `_plan_file`
  models an empty prior as `['']` (`''.split('\n') == ['']`) with no
  pop, so a created file's rebuilt content carries a phantom blank
  line. [VERIFIED tests/validation/test_patch.py:390-419] the strict
  xfail pins exactly this and names the fix site.
- [VERIFIED via `glob`] `src/paa/coder/fs_write_tools.py` and
  `tests/coder/test_fs_write_tools.py` did not exist before this task.
- [VERIFIED src/paa/coder/workspace.py:239-362] frozen API: `Workspace(root)`,
  `assert_inside_workspace(ws, target) -> Path`,
  `is_protected(ws, target) -> str|None` with frozen reason strings,
  jail-then-protect order.
- [VERIFIED src/paa/coder/loop.py:85-97] `Tool` protocol;
  [VERIFIED src/paa/coder/permissions.py:127-137] mutating globs name
  `write*`/`edit*`/`apply_patch*` already, so the new names match
  `DEFAULT_MUTATING_TOOLS` with no config change.
- [VERIFIED via `grep` in `src/paa`] no production caller of
  `PatchApplier.apply` exists (definitions + `__init__` only); only
  `tests/validation/test_patch.py` calls it. The refusal therefore has
  no blast radius in `src/`.

## 3. Root cause

(1) The applier is hash-*recorded* but not hash-*verified* at apply
time, so a file changed between `dry_run` and `apply` is silently
overwritten — any tool built on it inherits the window; fixed at the
source per ADR-0045 D-3. (2) The planner's line model has no
empty-file case, so a created (or empty-existing) file gains a
phantom line and the ledger records the sha256 of bytes nobody wrote.

## 4. Context report (authored before any change)

- Root cause / entry points: `PatchApplier.apply`
  (`src/paa/validation/patch.py:672`), `_plan_file` split
  (`:567-572`), new `src/paa/coder/fs_write_tools.py`.
- Affected files/interfaces/downstream: `apply` contract gains a
  refusal (stale plan → `ValidationError` + unwind);
  `prior_sha256` becomes load-bearing at apply; new names
  `write_file/edit_file/apply_patch` for N1.5 registration; no other
  `apply` callers in `src/` (verified).
- Invariants: dry_run computes full post-patch content before any
  write; multi-file apply unwinds via journal; `compute_patch_sha256`
  normalises `\r\n`→`\n`; journal holds exact prior bytes;
  jail-before-protect, protection non-overrideable.
- Risks/edge cases: CRLF new-file normalisation by the diff wire
  format; BOM-in-first-line for edits; `\ No newline` marker for
  no-trailing-newline content; empty file/create edges; binary /
  non-UTF-8 priors must refuse, not corrupt; over-limit inputs must
  refuse, not truncate silently; docs `path:line` refs to
  `patch.py:270,467,672` must stay line-stable or be NEED-ed.
- Expected behaviour / tests: see §7.

## 5. What changed (per file)

### `src/paa/validation/patch.py` (+59 lines, two fixes only)

- **TASK-2** (`_plan_file`, `:567-572`): `original_lines =
  normalised.split("\n") if normalised else []` — an empty prior is
  zero lines, not one phantom line. +3 comment lines. Reversal:
  reverting this one expression reintroduces the phantom blank line.
- **Prior-hash re-check** (`apply`, `:675ff` + `_verify_prior`): for
  each entry, the bytes just read for the journal are compared with
  `entry.prior_sha256` (CREATE: target must still not exist;
  MODIFY/DELETE with a recorded hash: bytes must match; missing
  target: stale). A mismatch raises `ValidationError`, caught by
  the *existing* `except` (widened `OSError` → `(OSError,
  ValidationError)`), so the journal unwinds and the same
  "patch application failed and was rolled back" refusal fires —
  preserving `test_a_failure_mid_patch_unwinds_the_whole_patch`'s
  "rolled back" assertion. Hand-built plans with no `prior_sha256`
  skip the comparison (nothing to compare against; the journal
  still holds prior bytes for rollback). No existing behaviour
  changes for unstale plans.

### `src/paa/coder/fs_write_tools.py` (new, ~570 lines)

- `FsWriteSession`: workspace + validator/applier + in-memory
  `journals: list[PatchJournal]` (Wave-1 scope per brief; ledger
  persistence is Wave 2), `last_patch_text` for SHA verification,
  `record()` and reverse-order `rollback_all()`.
- `WriteFileTool` (`write_file`), `EditFileTool` (`edit_file`),
  `ApplyPatchTool` (`apply_patch`): each implements
  `name/description/schema()/async run(Mapping)->str` per
  `loop.py:85-97`; `run` returns JSON strings (`{"ok": ...}`),
  matching `runtime_tools`' errors-as-values convention.
- All writes synthesize (write/edit) or pass through (apply_patch)
  a unified diff, then share one `_execute`: `dry_run` →
  jail-then-protect **every** entry → `apply` → `record`. Success
  carries `patch_sha256` (= `compute_patch_sha256` of the applied
  patch, verified by tests), `files_changed`, `touched_paths`, and
  (single-file tools) `path/action/prior_sha256/new_sha256`.
- `edit_file`: prior read (stat size-checked first), binary
  (NUL) / non-UTF-8 refused, BOM stripped for matching and
  re-added on write, `old_str` counted on normalised text —
  0 or >1 matches refuse without writing. `new_str` may be empty.
- Encoding/limits: new content must be NUL-free UTF-8 text ≤
  `MAX_CONTENT_BYTES` (256 KiB); priors over the limit refused
  before reading; patch text ≤ `MAX_PATCH_BYTES`. Refusals carry
  `{"limit_exceeded": true, "limit": ..., "size": ...}` markers —
  never silent truncation (truncating user content would be
  corruption; see §12 deviation).
- Paths: `ws.root / Path(raw)` → `assert_inside_workspace` →
  `is_protected` (frozen order), refusing with the exact frozen
  reason string; the canonical relative POSIX path is derived from
  the *resolved* path (a raw `a/../b` is inside but not a legal
  diff path). `build_fs_write_tools(workspace, *, session=None)`
  accepts `Workspace | Path | str` (`str` via `resolve_workspace`)
  and shares one session across the three tools by default.
- Whole-file diff synthesis: single hunk, removed = full prior,
  added = full new; `\ No newline at end of file` preserves
  intentional no-trailing-newline content.

### `tests/validation/test_patch.py` (one change)

Removed the strict xfail above
`test_a_created_file_contains_exactly_what_the_diff_describes`
(TASK-2 verifier, per brief; deleted by line range after the
`edit` tool failed to match the block's escaped/unicode literals —
verified by read-back and `git diff`).

### `tests/coder/test_fs_write_tools.py` (new, 46 tests)

Written byte-exactly (all fixtures via `write_bytes` — see §10
for the `write_text` newline-translation trap that initially
failed 5 tests on Windows). Classes: `TestWriteFile` (exact
bytes/SHA, nested, overwrite, empty file, no-trailing-newline,
unicode, BOM, CRLF-modify preserved, CRLF-create→LF documented,
`rollback_all`, write-to-directory refused),
`TestEditFile` (exact-once+SHA, 0/multi/empty/missing/directory
failures without write, deletion, CRLF preserved, BOM preserved),
`TestJailAndProtect` (traversal, absolute, symlink escape,
`.git`, any-depth `.env*`, `PAA_HOME` via `monkeypatch` +
`reset_settings_cache`, edit-of-protected, whole-patch refusal
for protected entry, traversal patch rejected),
`TestBinaryRefused` (NUL prior, non-UTF-8 prior, NUL content),
`TestLimits` (content/prior/patch over limit refused+marked),
`TestApplyPatch` (SHA equals `compute_patch_sha256` of supplied
patch, multi-file, hunk mismatch refused without write),
`TestPriorHashRecheck` (changed/created/deleted/stale-second-
unwinds-first, unmutated still applies),
`TestNaming` (names match `DEFAULT_MUTATING_TOOLS`, PLAN denies
all three, schemas `additionalProperties: false`).

## 6. Blast radius and contracts

- `PatchApplier.apply` gains exactly one new refusal shape
  (stale plan). Its jq-level contract for clean, unstale plans is
  unchanged: same journal, same rollback, same "rolled back"
  wording for mid-apply failures. No `src/` caller exists, so
  blast radius is tests + the new tools.
- `PatchPlanEntry.prior_sha256` is now load-bearing at apply;
  hand-built plans without it still apply (documented skip).
- New public module `paa.coder.fs_write_tools` (seven `__all__`
  names). N1.5 registers `write_file/edit_file/apply_patch` —
  names already match the mutating globs; no `agent.py`,
  `runtime_tools.py`, or `__init__.py` change was needed (or made).
- No EventType, schema, config, or dependency change. No new
  `if TYPE_CHECKING:` block (import-conventions lock stays green
  except the unrelated docs-ref failure — see §13).
- Docs `path:line` refs below line 567 in `patch.py` shifted by
  the +59 (approval apply 672→675, sha function 786→841);
  refs to `:270`/`:467` are untouched and still correct. The
  re-number is ORCH-owned (NEED posted; editing docs is outside
  the write-set).

## 7. Tests (how each confirmed to fail without fix)

- TASK-2: `test_a_created_file_contains_exactly_what_the_diff_describes`
  was a strict xfail (i.e. failing) before; it now passes
  (verified in the 50-test `test_patch.py` run). Reverting the
  one-expression fix makes it fail again — the tripwire is direct.
- Prior-hash (`TestPriorHashRecheck`, 5 tests): the pre-fix
  `apply` had no comparison, so `pytest.raises(ValidationError)`
  cannot pass against it — the file changed underneath would
  simply be overwritten (changed-file test asserts the concurrent
  bytes survive; file-created test asserts the interloper survives).
  This is reversal-by-construction; the pre-fix behaviour is the
  documented INV the brief cites.
- All other fs tests fail pre-implementation by import error
  (module did not exist) and assert on payload + on-disk bytes
  (not mocks), so passing proves both the contract and the disk
  outcome. Edit 0/multi-match tests assert `session.journals ==
  []` and byte-identical files — the "fail without write"
  half is asserted, not assumed.
- SHA tests: `apply_patch` payload `patch_sha256 ==
  compute_patch_sha256(supplied_patch)` exact; write/edit payload
  equals `compute_patch_sha256(session.last_patch_text)` plus
  journal SHA equality.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- **Source**: one transactional primitive (planner+applier) is
  reused; no second write path introduced. The TOCTOU fix sits
  at the source (applier), not per tool, so future tools inherit
  it. TASK-2 fixes the planner's line model, not the test.
- **Load (100x, concurrency, partial failure)**: whole-file hunks
  are O(file), capped at 256 KiB; journals hold ≤256 KiB prior
  bytes per write within a session (Wave-1 session ≤ `max_steps`
  writes). Concurrent modification between `dry_run` and `apply`
  refuses and unwinds (mid-apply staleness rolls back
  already-written files — tested). A concurrent change in the
  tiny read→replace window remains a best-effort race (same
  documented limitation as the jail's symlink check); the
  journal still restores exact prior bytes.
- **Hostile input**: traversal/absolute/symlink/UNC/control paths
  denied by the jail (frozen module) and by `safe_relative_path`
  (patch); protected `.git`/`.env*`/`PAA_HOME` denied even if
  explicitly named; binary (NUL) and non-UTF-8 priors refused;
  over-limit inputs (content, prior, patch) refused with markers
  before any read/write; empty `old_str` refused; surrogates in
  JSON strings refused on `encode`. No injection surface: paths
  never reach SQL or a shell; diff headers are built from the
  canonical relative path only.
- **Operability (observe, roll back, migrate)**: every success
  logs `coder.fs_write.applied` (tool/files/patch-sha prefix,
  never content); `rollback_all` unwinds a session newest-first;
  each `run` returns the patch SHA plus `prior/new` SHAs for
  ledger journaling in Wave 2. No env values, keys, or PII are
  logged or emitted.
- **Reversal**: revert the `_plan_file` expression → TASK-2 test
  fails again. Revert `_verify_prior` call → all four stale tests
  fail (apply succeeds and clobbers). Delete `fs_write_tools.py`
  → 46 tests import-fail. No test was weakened to pass.

## 9. Edge cases and abuse scenarios

- Empty new file (`content=""`) creates `b""` (phantom-free).
- Content without trailing newline preserved via the
  `\ No newline` marker; content *with* trailing newline stays.
- Overwrite of a CRLF file keeps CRLF (planner re-joins with the
  file's newline); a *new* file from CRLF input is LF — a diff
  line cannot carry a trailing CR through the wire format
  (documented limitation, tested as such).
- Edit line endings: `old_str`/`new_str` normalised with the
  prior, so LF model text matches a CRLF file and the result
  stays CRLF (tested).
- BOM: matched in BOM-free space, preserved byte-exactly on
  edit; BOM in new content written as BOM bytes.
- Empty-existing-file MODIFY: prior `b""` plans as zero lines
  (TASK-2) — whole-file hunk inserts at position 0 (pure-insertion
  clamp in `_locate`).
- write to a directory: CREATE synthesis → planner refuses
  "already exists"; edit of a directory: "path is not a file".
- `apply_patch` deleting a protected file or touching any
  protected entry refuses the whole patch (no partial writes).
- Patch with path escape: `dry_run` marks `rejected`; tool
  refuses before apply. Malformed hunks: refused before write.
- Deeply nested new paths create parents (atomic write does
  `mkdir(parents=True)`).
- Non-string arguments (numbers, lists) refused with a JSON
  error; missing/blank `path` refused; unknown extra keys are
  not read (schemas say `additionalProperties: false`, and `run`
  reads only named fields).
- Surrogate code points in JSON (`"\ud800"`): `encode` raises
  → refused as non-UTF-8 text.
- `PAA_HOME` inside the workspace protects everything beneath
  it (tested); unset PAA_HOME stays the operator default outside
  test workspaces.

## 10. Performance and security

- Per-write cost: one prior read (stat + ≤256 KiB read), one
  whole-file diff synthesis O(file), one dry_run (parse + H / _locate
  O(file)), one atomic write + fsync. Negligible vs model latency.
- The stat-before-read guard bounds hostile-file memory; the
  256 KiB ceilings bound the hunk and journal sizes.
- `Path.write_text` newline-translation trap: the initial test
  run failed 5 tests on Windows because `write_text` wrote CRLF
  while assertions read bytes — a test-only defect (the repo's
  own conftest docstring warns about it). Fixed by switching
  byte-asserting fixtures to `write_bytes`; the implementation
  was correct throughout (it preserved the CRLF it was given).
- Security posture: jail-then-protect inside `run`, below every
  permission decision, per ADR-0045 — bypass/allow rules cannot
  override a protected refusal. No shell, SQL, or network access
  added; no secrets read. The known residual is the
  documented best-effort symlink race (check-vs-use) shared
  with the frozen jail module.

## 11. Commands run and results (verbatim summaries)

| Command | Result |
|---|---|
| `pytest tests/validation/test_patch.py` | 50 passed (incl. the un-xfailed TASK-2 test; single-test run 1 passed) |
| `pytest tests/coder/test_fs_write_tools.py` | 46 passed |
| `pytest tests/coder/test_fs_write_tools.py tests/validation/test_patch.py` | **96 passed in 3.01s** |
| `ruff check src/paa/coder/fs_write_tools.py src/paa/validation/patch.py tests/coder/test_fs_write_tools.py tests/validation/test_patch.py` | All checks passed! |
| `ruff check src tests` | 1 error in sibling's `src/paa/coder/git_tools.py:604` E501 (not mine — FINDING posted; my four files clean in the same run earlier: "All checks passed!") |
| `mypy src` | 3 errors, all `sandbox/subprocess_backend.py:548` (pre-existing Windows baseline A-001; 0 new) |
| `pytest tests/conventions -q` | 1 failure: `test_every_referenced_line_is_not_blank` — `docs/modules/validation.md:46` cites `patch.py:786`, now blank after the +59 (NEED posted, DEFERRED — see §13). All other conventions tests pass incl. import conventions |
| Full suite / containers / ports | NOT RUN (scoped-only per brief/standing orders) |

## 12. Deviations from plan/brief

1. **Limits refused + marked, not truncated.** The brief's
   "limits truncated+marked" is inherited from the read/output
   cap (T-N1-6). Truncating *written* content would silently
   corrupt the tree, so over-limit writes/patches refuse with a
   `limit_exceeded` marker instead. Reads inside edit still
   never truncate. This is a safer analogue, flagged for N1.5.
2. **CRLF new-file normalization.** New files are created LF
   even from CRLF content (see §9); CRLF is preserved for
   modifies. Exact CRLF creates need a planner/wire-format
   change outside the `prior-hash + TASK-2 only` allowance —
   filed as a NEED-grade finding in §13, not a hidden fix.
3. **`edit_file` argument names** are `old_str`/`new_str`
   (brief wording) with `path`. No aliases — one spelling.
4. **Errors as JSON values, journal as `FsWriteSession.journals`**
   with `last_patch_text` for SHA verification — the brief's
   in-memory-journal intent with a test-observable seam.

## 13. Out-of-scope findings

1. **DEFERRED-DEPENDENCY — docs re-number (NEED posted).**
   `tests/conventions/test_docs_references.py` fails: `docs/modules/validation.md:46`
   cites `src/paa/validation/patch.py:786` (`compute_patch_sha256`,
   now at `:841`) and `PatchApplier.apply` moved `:672`→`:675`
   (also cited by `docs/data/data-lifecycle.md:191`). The cited
   *sentences* are still true; only the numbers moved (+59 lines
   from the mandatory prior-hash fix). Editing either doc is
   outside the write-set; NEED posted in COMMS per procedure
   (corroborated by W1-IMPL-03's CONFLICT on the same line).
   ORCH to re-point `672`→`675`, `786`→`841` after reading (the
   lock only checks non-blank; the `:672` ref is currently
   wrong-but-non-blank). The `:270`/`:467` refs are untouched
   and still correct.
2. **FINDING — sibling ruff.** `src/paa/coder/git_tools.py:604`
   E501 (W1-IMPL-06, untracked, in progress) makes the repo-wide
   `ruff check src tests` red at this snapshot; my four files are
   clean and the finding was posted without editing their file.
3. **FINDING (note, not fixed) — `apply` trusts hand-built
   plans' paths.** `PatchApplier.apply` re-verifies hashes but
   does not re-run `safe_relative_path` on `entry.relative_path`.
   All workspace entry goes through `dry_run` (which does) and
   the new tools jail before apply, so the live path is safe; a
   hand-built `PatchPlan` with a traversing relative path written
   by a future caller would not be re-checked. Adding the check
   here would exceed the `prior-hash + TASK-2 only` allowance for
   `patch.py`; recorded for the orchestrator to schedule.
4. **TASK-3/TASK-4/TASK-5** (engine/ast-scanner/policy-lock) were
   not observed in the write-set files and are untouched.
5. **CRLF-create wire-format limit** (§12.2): if exact CRLF
   creation is ever required, the fix is in the patch wire
   format/planner (a line representation that can carry CR),
   not in the tools.

## 14. Known limitations, risks, rollback notes

- New-file CRLF normalization (§9) and the prior-file line-ending
  inheritance on overwrite (overwrite keeps the *file's* newline,
  not necessarily the content's). Documented in the module
  docstring; N1.5 should not promise byte-exact line endings.
- Hand-built plans without `prior_sha256` skip the re-check by
  necessity (documented in `apply`'s docstring).
- `rollback_all` is in-memory only; a process crash loses the
  journal (Wave 2 makes it ledger-durable). The session is the
  recoverable record until then (brief's Wave-1 scope).
- Rollback of this task: `git revert b97ff73` restores the
  pre-fix applier/planner (reinstating the TOCTOU + phantom
  newline) and removes the tools/tests. No schema/ledger change,
  so the revert is two-way.

## 15. Where I am least confident

- Nothing load-bearing is unverified in my scope. The one
  judgement call (limits-refuse vs truncate, §12.1) is
  deliberate and flagged — if the operator wants truncation
  semantics for writes, that is a spec change, not a bug fix.

## 16. Self-review checklist

- [x] Re-read the full diff from scratch (`git diff` + files).
- [x] Prior-hash check defeats silent clobber in all planner
  shapes (CREATE/MODIFY/DELETE, missing/changed/created),
  unwinds mid-apply staleness, and preserves the existing
  "rolled back" contract.
- [x] TASK-2 fix is one expression; no other planner behaviour
  changed (50/50 `test_patch.py` green).
- [x] Every disk-touching path is jailed before protected
  before read;/write; errors carry frozen reason strings;
  0/multi-match writes nothing.
- [x] No `any` casts, no `type: ignore`, no `# noqa`, no broad
  excepts; mypy strict clean on new/changed files.
- [x] No forbidden/ownership violations (only 4 write-set files
  committed; sibling's E501 and docs re-number handled via
  FINDING/NEED, not edits).
- [x] Tests are behaviour-focused (payload + on-disk bytes) and
  fail without the fix (§7).
- [x] Conventions failure and sibling ruff failure disclosed,
  not hidden (§11/§13).

## 17. Commits

- Code: `b97ff73a443d90fe843b4785fefc77a81130cb11` (explicit
  paths: the 4 write-set files) on `ops/20261109-next-spec`.
- Report: this file, committed separately (docs-only commit
  following; SHA filled here after the fact if needed — reports
  are excluded from the `path:line` lock, so the hash need not
  appear above its own commit).
