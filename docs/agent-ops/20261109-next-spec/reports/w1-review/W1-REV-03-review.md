<!-- ORCH-DIGEST:START -->
agent: W1-REV-03 | scope: wave 1b T-N1-2 (b97ff73) + T-N1-4 (481a8d3) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 1 | MAJOR 3 | MINOR 2 | NIT 4
claims_audit: 14 claims checked / 4 unsupported or false (3 false + 1 half-true)
band_aids_found: path-check-only (git_stage): _jail_paths checks the named pathspec as one path while git expands it recursively/glob/magic, so protected descendants stage (see BLOCKER-1)
top_3_risks: (1) git_stage(["sub"|.|:/|*.txt]) stages .env secrets into the index despite jail-then-protect; (2) git_stage is PLAN-allowed under today's DEFAULT_MUTATING_TOOLS (latent until N1.5 wiring — must land the glob before exposing the tools); (3) write_file MODIFY silently drops a trailing newline when the prior file is empty/no-trailing-newline
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 2
reversal_check: done
confidence: high (<all scoped tests re-run by reviewer; 10 red-team probes executed; full conventions suite timed out, see §9)
<!-- ORCH-DIGEST:END -->
# Code review — W1-REV-03

Scope: T-N1-2 write tools (`b97ff73`: `src/paa/coder/fs_write_tools.py`,
`src/paa/validation/patch.py` prior-hash + TASK-2, tests) and T-N1-4 git
tools (`481a8d3`: `src/paa/coder/git_tools.py`, tests). Read-only review;
the only file written is this report. Impl reports
`reports/w1-impl/W1-IMPL-04-N1-2.md` and `W1-IMPL-06-N1-4.md` were read
**after** the blind pass, per brief.

## 0. Method

- Blind pass first: `git show` of both code commits, full read of
  `fs_write_tools.py` (572 lines), `git_tools.py` (625), the
  `patch.py` diff, both test files, plus `permissions.py`,
  `workspace.py` jail/protect, `agent.py` wiring.
- Then ran, myself: the three scoped test files in batches, `ruff`
  and `mypy` on the touched sources, `tests/conventions/test_docs_references.py`.
- Then a 10-probe red-team script (temp dir outside the repo) exercising
  stale plans, bad prior hashes, trailing newlines, oversized edits,
  git push-shaped args, directory/glob/magic pathspecs, and the default
  PLAN policy. Probes P1–P10 quoted in §5.
- Claims audit (§2) against both impl reports.

## 1. Blind-pass findings

What is genuinely solid (verified before reading the impl reports):

1. **Prior-hash re-check is at the source, not per tool.**
   `PatchApplier.apply` records journal bytes, then `_verify_prior`
   compares them to `entry.prior_sha256` (`patch.py:743-782`):
   CREATE refuses if the target now exists, MODIFY/DELETE refuse on
   changed/deleted target, mismatch raises `ValidationError` caught
   by the widened `except (OSError, ValidationError)` (`patch.py:723`)
   so the journal unwinds. DELETE plans do carry a hash
   (`patch.py:534`), so the stale-delete path is covered (probe P9).
2. **TASK-2 phantom fix is correct for CREATE.**
   `original_lines = normalised.split("\n") if normalised else []`
   (`patch.py:573`) makes an empty prior zero lines; `wants_trailing`
   is forced true for CREATE (`patch.py:588`), so a created file is
   exactly the diff content. The strict xfail is removed and the
   test passes (re-run, §11).
3. **`patch_sha256` is the plan's, computed from the applied text.**
   `dry_run` sets `patch_sha256=compute_patch_sha256(patch_text)`
   (`patch.py:477`); `_execute` surfaces it verbatim and tests pin
   it against `compute_patch_sha256` of both synthesized and
   caller-supplied patches. No recomputation drift.
4. **edit exactly-once, no-write on failure.** `run()` counts
   occurrences on normalized text before synthesizing anything
   (`fs_write_tools.py:287-293`); 0/multi raise `_ToolError` before
   `_execute`, so nothing reaches disk. Tests assert byte-identical
   files and empty journals. Correct.
5. **Jail-then-protect order is right where it is applied.**
   `_jail_resolved` asserts containment then checks protection
   (`fs_write_tools.py:380-389`); `_execute` re-jails *every* plan
   entry before `apply` (`:530-531`), so a multi-file patch touching
   a protected file refuses as a whole. Git's `_jail_paths` also
   checks in the right order (`git_tools.py:324-332`) — the order is
   fine; the *unit of check* is not (BLOCKER-1).
6. **Git fixed argv is really fixed.** Every invocation is a literal
   tuple in code (`git_tools.py:377-386,447-456,516,571-580,602`);
   model input can only fill `paths` (after `--`), `staged`, or the
   `-m` value. `--amend`-shaped messages are subjects, extra
   `remote`/`refspec` keys are never read, no `push`/`fetch`/`config`
   token exists in the module (grep-verified). Hooks are disabled by
   internally fixed `-c core.hooksPath=` plus `--no-gpg-sign`;
   env is allowlisted with `GIT_TERMINAL_PROMPT=0` forced, dropping
   all ambient `GIT_*`. Commit with nothing staged returns git's
   nonzero exit as a clean JSON value (`git_tools.py:587-597`).
7. **Naming/PLAN for the write tools holds today.**
   `write_file`/`edit_file`/`apply_patch` match `write*`/`edit*`/
   `apply_patch*` in `DEFAULT_MUTATING_TOOLS` (`permissions.py:127-137`)
   — verified by the `TestNaming` tests against the *default* policy,
   unlike the git tests (MAJOR-3).

Blind-pass suspicions that probes then confirmed or cleared are in
§5; two (directory pathspec staging, MODIFY trailing-newline loss)
were found by reading the git/planner semantics before running
anything.

## 2. Claims audit

14 material claims checked across both impl reports; 3 false or
unsupported, 1 half-true (disclosed in passing).

| # | Claim (source) | Verdict |
|---|---|---|
| 1 | Prior-hash re-check refuses changed/created/deleted targets and unwinds mid-apply staleness (IMPL-04 §7) | **TRUE** [VERIFIED `patch.py:743-782`; RAN tests 5/5; probe P9 stale DELETE refused with rollback] |
| 2 | TASK-2: created file contains exactly the diff content; xfail removed (IMPL-04 §7) | **TRUE** [RAN `test_patch.py` 50 pass incl. un-xfailed test] |
| 3 | "Content *with* trailing newline stays" (IMPL-04 §9); write "exactly as given" (`fs_write_tools.py:27-28,205`) | **FALSE for MODIFY** — true for CREATE. Probes P1/P2: overwriting an empty or no-trailing-newline file with `"hello\n"`/`"new\n"` yields `b"hello"`/`b"new"` (MAJOR-1). IMPL-04 §14 discloses line-ending *inheritance* in one sentence, but §9's flat "stays" is wrong and no test covers MODIFY-newline-on. |
| 4 | "over-limit inputs (content, prior, patch) refused" (IMPL-04 §8, §9) | **FALSE for edit results.** `write_file` checks content and `apply_patch` checks patch size, but `edit_file` never checks the *resulting* size: probe P3 replaced 1 char with 300 KiB and got `ok: true`, file 307,201 bytes > `MAX_CONTENT_BYTES` (MAJOR-2). |
| 5 | patch SHA payload equals `compute_patch_sha256` of the applied patch (IMPL-04 §5) | **TRUE** [VERIFIED `patch.py:477,537`; RAN SHA tests] |
| 6 | edit 0/multi-match fails with no write and empty journal (IMPL-04 §7) | **TRUE** [VERIFIED `fs_write_tools.py:287-293`; RAN tests] |
| 7 | Hand-built plans without `prior_sha256` skip the check; dry-run plans always carry it (IMPL-04 §5) | **TRUE** [VERIFIED `patch.py:534` DELETE, `:600` MODIFY set it; `patch.py:762-766` skip] |
| 8 | Fixed argv: no push/force/config/hooks reachable; extra args dropped (IMPL-06 §5, §8) | **TRUE for argv** [VERIFIED literal tuples; RAN hooks/remote/dash-pathspec tests; probe P10 commit with `remote: origin` committed locally only]. But see BLOCKER-1: argv being fixed does not make *pathspec expansion* safe. |
| 9 | "protected staging (`.git`/`.env`/PAA_HOME)" all-or-nothing, batch cannot stage the safe half (IMPL-06 §8, module docstring `git_tools.py:25-27`) | **FALSE.** True only for explicitly named files. Probes P4–P7: `["sub"]`, `["."]`, `[":/"]`, `["*.txt"]` all staged protected descendants (BLOCKER-1). Tests (`TestJailAndProtection`) never stage a directory. |
| 10 | PLAN allows reads, denies mutating; ASK prompts only for mutating; LOCKDOWN denies all (IMPL-06 §5, §7) | **HALF-TRUE.** True against the *post-N1.5* policy the tests construct (`mutating_tools=(*DEFAULT_MUTATING_TOOLS, "git_stage*")`, `test_git_tools.py:103-112`). Under **today's** default policy `git_stage` is PLAN-**allowed** (probe P8). IMPL-06 §13/§17 discloses the NEED, so this is not a hidden falsehood — but the brief's "(now git_stage matches)" verification item does **not** hold on this branch (MAJOR-3). |
| 11 | `git_commit` already matches `*_commit*` today (IMPL-06 §6) | **TRUE** [VERIFIED `permissions.py:136`; probe P8: PLAN denies `git_commit`] |
| 12 | Commit with nothing staged fails cleanly; remote stays empty (IMPL-06 §5) | **TRUE** [RAN `TestExecution`/`TestNoRemoteSurface`; probe P10] |
| 13 | mypy 3 pre-existing baseline, ruff 0 on own files (IMPL-04 §11, IMPL-06 §11) | **TRUE as re-run by me scoped**: ruff clean on all 6 touched files; mypy clean on the 3 touched sources. Full-`mypy src` not re-run (scoped rule) — see §9. |
| 14 | Conventions 33/34, sole failure external (sibling patch.py blank-citation) (both reports) | **SUPERSEDED/now green:** after ORCH's re-point `353e2c4`, [RAN `test_docs_references.py` → 5 passed]. Full conventions suite timed out for me (§9), so I do not claim the whole directory. |

## 3. Long-run test (independent answers)

- **Source.** The right primitive is reused: the TOCTOU fix sits in
  `PatchApplier.apply` so all current and future tools inherit it;
  the phantom fix sits in the planner's line model. Nothing was
  patched per call-site. The one place the source-level fix is
  *incomplete* is pathspec expansion for `git_stage` — the check is
  source-level for a namespace (one path) that git does not use
  (recursive pathspecs), so the invariant breaks at the boundary,
  not inside the jail.
- **Load (100×, concurrency, partial failure).** Bounded: 256 KiB
  write/patch caps, 64 KiB output cap with store-capped/drain-full
  reads, 120 s git kill-timeout, 100-path argv cap, 2048-char
  message cap. Journals hold one prior copy per write per session.
  Concurrency: git subprocesses share no state; concurrent `git
  add` gets git's index lock as a clean payload. Partial failure:
  multi-file apply unwinds via journal; mid-apply staleness is the
  tested case. One hole: because `edit_file` skips the result-size
  check, a session can grow a file past the read cap and then never
  edit it again (over-size prior refused) — a self-inflicted
  permanent bloat (MAJOR-2).
- **Hostile input.** Traversal/absolute/symlink/protected paths,
  NUL/non-UTF-8, oversized inputs, flag-shaped pathspecs,
  `--amend`-shaped messages, ambient `GIT_*` — all handled and
  tested. Not handled: pathspec magic/glob/directory expansion
  (BLOCKER-1); a patch whose hunk uses pure-insertion at EOF is
  clamped, not rejected (by design, pre-existing). ReDoS: no
  untrusted regex (permission patterns are escaped globs).
- **Operability (observe, roll back, migrate).** Every success logs
  tool/files/sha-prefix (no content, no secrets); failures are
  JSON values, never tracebacks. `rollback_all` unwinds a session
  newest-first. `patch_sha256` + per-file prior/new SHAs in the
  payload are Wave-2 ledger-ready. Nothing is migrated or persisted
  yet — in-memory journals are lost on process crash, which the
  brief scopes to Wave 1.
- **Reversal.** Revert the `_plan_file` expression → the un-xfailed
  TASK-2 test fails. Revert `_verify_prior` → all 4 stale tests fail
  (apply clobbers). Delete `fs_write_tools.py`/`git_tools.py` → the
  new suites import-fail. No test was weakened: the only test change
  in `test_patch.py` is the brief-mandated xfail removal. For the
  BLOCKER: no existing test fails if directory-staging of a
  protected descendant is *allowed* — that is the gap; the fix needs
  new tests (stage `["sub"]` / `["."]` / pathspec-magic must error
  and stage nothing).

## 4. Findings

### BLOCKER-1 — `git_stage` (and `git_diff`) stage/show protected files via directory, `.`, glob, and pathspec-magic expansion

`_jail_paths` (`git_tools.py:314-333`) validates each named path as a
single filesystem path, then passes the *relative form* to
`git add -- <paths>` (`git_tools.py:516`). Git expands pathspecs
recursively and interprets glob/magic: a directory stages everything
under it, `.` stages the whole tree, `:/` is top-level magic, `*.txt`
is a glob. `is_protected` only inspects the *named* path's segments
(`workspace.py:279ff`), so descendants are never checked. Module
docstring promises the opposite (`git_tools.py:25-27`).

Evidence [RAN probe script → results]: in a repo with
`sub/.env.local`, `sub/ok.txt`, `.env`:

- `git_stage(["sub"])` → staged `sub/.env.local | sub/ok.txt`
- `git_stage(["."])` → staged `.env | sub/.env.local | sub/ok.txt`
- `git_stage([":/"])` → staged `.env | sub/.env.local | sub/ok.txt`
- `git_stage(["*.txt"])` → staged `sub/ok.txt` (beyond the named file)

So secrets the fs tools refuse to *write* can be staged and then
committed into history by `git_commit` (which commits the index as
staged). `git_diff(["sub"])` leaks protected diffs the same way.
**Fix required before N1.5 wires these tools into the agent:**
either refuse directory/`"."` paths and any path containing
pathspec-magic/glob metacharacters (`* ? [ :`), or expand each
pathspec against the index/worktree (`git ls-files`) and run
`is_protected` on every expansion, all-or-nothing. Add tests for
all four probe shapes. Severity BLOCKER as a gating item for wiring;
note the tools are not yet exposed by `CoderAgent.build`
(`agent.py:186-202` builds only runtime tools), so this is latent,
not live exploitation — but the invariant as advertised is false.

### MAJOR-1 — `write_file` MODIFY silently drops the trailing newline when the prior file has none

`_plan_file` computes `wants_trailing = had_trailing_newline or
action is CREATE` (`patch.py:588`); for MODIFY it inherits the
*prior* file's trailing-newline status and ignores the patch body's
own marker state. `write_file` synthesizes the marker only when the
new content *lacks* a trailing newline (`fs_write_tools.py:489,498`),
so the converse — new content *gains* one — has no representation.

Evidence [RAN probes]: overwrite empty file with `"hello\n"` →
`b"hello"`; overwrite `b"old"` with `"new\n"` → `b"new"`. Both
return `ok: true`. `write_file`'s schema/docstring promise content
"written exactly as given" (`fs_write_tools.py:27-28,205`); CREATE
honours it, MODIFY-on-no-trailing-prior does not. IMPL-04 §14
discloses line-ending inheritance, but not this silent byte loss,
and nothing tests it. Fix in the planner (treat absence of the
no-newline marker on the new side as the new trailer, rather than
the prior's) or in the synthesis (encode the new trailer
explicitly); add overwrite-empty and overwrite-no-trailing tests.
(The same heuristic affects `apply_patch` MODIFY hunks that add a
trailing newline — pre-existing planner behaviour, now surfaced by
the new tool's exact-bytes contract.)

### MAJOR-2 — `edit_file` can grow a file past `MAX_CONTENT_BYTES` (result ceiling unchecked)

`MAX_CONTENT_BYTES` is documented as "size ceiling for one file's
new content" (`fs_write_tools.py:81-86`). `WriteFileTool` enforces
it on content (`:217`), `ApplyPatchTool` enforces patch size
(`:335`), but `EditFileTool.run` never checks the resulting body
before synthesis. Evidence [RAN probe P3]: `edit_file` replacing
`x` with 300 KiB → `ok: true`, file 307,201 bytes. Consequences:
the cap's memory/journal bound is bypassed, and the grown file is
thereafter uneditable (prior over the limit is refused by
`_read_prior_text`). Fix: compute the new body's UTF-8 length and
refuse with the same `limit_exceeded` payload shape; add a test.

### MAJOR-3 — `git_stage` does not match `DEFAULT_MUTATING_TOOLS` today (PLAN-exempt by default policy)

Under the current literal (`permissions.py:127-137`, no `git_stage*`
glob), [RAN probe P8] `ToolPermissionPolicy(mode=PLAN).evaluate(
"git_stage")` → **ALLOW**, while `git_commit` → DENY (matches
`*_commit*`). The git tests pass only because they construct the
post-N1.5 policy manually (`test_git_tools.py:103-112`), and the
`git_stage`-specific test asserts the name matches the *proposed*
pattern, not the real one (`:147-152`). IMPL-06 disclosed this as a
NEED for N1.5 and the brief explicitly allows the coverage test to
land in N1.5 — so this is a deferred dependency, not an ownership
violation. It becomes a MAJOR the moment N1.5 wires `build_git_tools`
into `CoderAgent.build` without also (a) adding `git_stage*` to
`_MUTATING_TOOLS` and (b) standing-allowing `GIT_READ_ONLY_TOOLS`.
The review brief's "(now git_stage matches)" item is **not
satisfied on this branch** — the rename to `git_stage` is done;
the glob is not. N1.5 must not land without it.

### MINOR-1 — `TestPermissions` proves the PLAN matrix against a policy production does not build

`test_git_tools.py:103-112` faithfully encodes the intended N1.5
shape, but the suite contains no test that fails against today's
default policy — the exact false-pass IMPL-06 §7 says it avoided.
When the glob lands, `test_git_commit_matches_default_mutating_globs_today`
should be extended with the `git_stage` assertion against
`DEFAULT_MUTATING_TOOLS` itself (the fs-write suite's `TestNaming`
is the model). Until then the regression ("someone wires the tools
without the glob") is unguarded.

### MINOR-2 — `TestPriorHashRecheck` does not pin the DELETE-stale case

The planner's DELETE path carries `prior_sha256` (`patch.py:534`)
and `_verify_prior` handles it (my probe P9 confirms refusal), but
no test deletes a file whose plan then goes stale. One test —
plan a delete, mutate the target, assert apply refuses and the
mutated bytes survive — would lock the third action shape.

### NIT-1 — `_relative_paths` computed twice per stage

`git_tools.py:516` builds argv paths, `:533` calls it again for the
payload. Trivial cost, but hoist to one local for clarity.

### NIT-2 — `_encode_new_content` result discarded

`fs_write_tools.py:217` calls it purely as a validator. Fine
behaviourally; a name/behaviour comment would stop the next reader
"fixing" it into use.

### NIT-3 — Mismatch plan entry drops empty-file hashes

`patch.py:585` uses `if prior_bytes` (bytes truthiness), so a
hunk-mismatch entry for an *empty* prior file records
`prior_sha256=None` instead of the empty-file hash. Only affects
diagnostics on an already-failing entry; use `is not None`.

### NIT-4 — `git_status` silently ignores all arguments

`_GitStatus.run` never reads `arguments`, so
`git_status({"paths": [...]})` succeeds rather than erroring.
Defensive and safe (nothing extra reaches git), but inconsistent
with the other tools' validation; at least document it in the schema
(which already has no properties + `additionalProperties: False`).

## 5. Attempts to break it

All [RAN via probe script in a temp dir outside the repo; each probe
independent, workspace under `TemporaryDirectory`]. Results:

| Probe | Attempt | Result |
|---|---|---|
| P1 | Overwrite **empty** existing file with `"hello\n"` | `ok: true`, bytes `b"hello"` — trailing newline lost (MAJOR-1) |
| P2 | Overwrite no-trailing-newline file with `"new\n"` | `ok: true`, bytes `b"new"` — same loss (MAJOR-1) |
| P3 | `edit_file` expanding 1 char → 300 KiB | `ok: true`, 307,201 bytes written — cap bypassed (MAJOR-2) |
| P4 | `git_stage(["sub"])` where `sub/.env.local` exists | staged `.env.local` + `ok.txt` — protected descendant staged (BLOCKER-1) |
| P5 | `git_stage(["."])` | staged top-level `.env` + all — (BLOCKER-1) |
| P6 | `git_stage([":/"])` pathspec magic | staged `.env` + all — (BLOCKER-1) |
| P7 | `git_stage(["*.txt"])` glob | staged `sub/ok.txt` beyond the named file (BLOCKER-1) |
| P8 | Default-policy PLAN for `git_stage`/`git_commit`/`git_status` | stage **ALLOW**, commit DENY, status ALLOW (MAJOR-3) |
| P9 | Stale DELETE: plan a delete, mutate target, apply | refused, "patch plan is stale", mutated bytes survive, rollback logged — **holds** |
| P10 | `git_commit` with `remote: origin` + nothing staged (after probes) | extra keys ignored, commit local only, no push argv exists — **holds** |

Also attempted by reading and cleared: stale CREATE (existing test
`test_file_created_after_planning_refuses`, re-run green), stale
MODIFY + mid-apply unwind (re-run green), `git push` via message /
paths / extra keys (no argv surface; dash-pathspec tests re-run
green), symlink escape and traversal (re-run green), hooks
(re-run green), ambient `GIT_DIR`/`GIT_SSH_COMMAND` (re-run green).
One residual I did **not** probe: swapping a file for a symlink
between jail-check and apply (check-vs-use race) — same documented
limitation as the frozen jail; P9-style content staleness is caught
by the hash, a same-content symlink swap is not.

## 6. Dimension checklist

- **Correctness:** core mechanisms verified (§1); two silent-data
  defects (MAJOR-1/2) and one boundary-expansion defect (BLOCKER-1).
- **Security:** jail-then-protect order correct; argv closed; env
  allowlisted; hooks/signing/pager/external-diff disabled. Broken
  only at pathspec expansion (BLOCKER-1) and latently at PLAN
  classification (MAJOR-3, pre-wiring).
- **Error handling:** failures are JSON values throughout; no
  tracebacks out of `run()`; mid-apply failures unwind; post-commit
  `rev-parse` failure degrades without re-raising over a committed
  result (`git_tools.py:598-611`). `rollback` swallows per-file
  `OSError` and keeps unwinding (deliberate, logged).
- **Performance:** bounded everywhere except MAJOR-2's edit
  expansion. No unbounded loops/queues.
- **Tests:** behaviour-focused (payload + on-disk bytes + real
  git), fail-without-fix by construction or by observed red
  (IMPL-06's cwd-relative jail incident). Missing: directory/magic
  pathspecs, MODIFY trailing-newline-on, edit result-size,
  stale-DELETE (MINOR-2).
- **Conventions:** no `# noqa`, no `type: ignore`, no widened
  `Any`, ruff/mypy scoped-clean; `class X(str, enum.Enum)` etc.
  untouched; docstrings carry ADR citations. Docs `path:line` refs
  re-pointed by ORCH (`353e2c4`) — docs-references lock green.

## 7. Optimization and simplification opportunities

- **DO-LATER (1):** `_execute` reads every prior file twice per
  write (tool read → `dry_run` read; `apply` re-reads for the hash
  check). That repetition *is* the TOCTOU defence, so do not
  "optimise" it away; at most, document the double-read as load-
  bearing (IMPL-04 §8 already does).
- **DO-LATER (2):** `git_tools` runs a `_preflight` rev-parse
  subprocess on *every* call (~10 ms). Deliberate (repo can be
  re-rooted mid-session) — keep; no action.
- **NOT-WORTH-IT (1):** whole-file hunk synthesis is O(file) but
  capped at 256 KiB — negligible vs model latency.
- **NOT-WORTH-IT (2):** hoisting `_relative_paths` (NIT-1) is
  cosmetic.

## 8. Cross-agent interactions

- The conventions docs-ref failure both impls reported is resolved:
  ORCH re-pointed the cites in `353e2c4` (after both code commits);
  my [RAN `test_docs_references.py` → 5 passed] confirms.
- N1.5 wiring checklist (from `agent.py:186-202`, which today builds
  only runtime tools): register `build_fs_write_tools` +
  `build_git_tools`; extend `mutating_tools` with the `git_stage*`
  glob (MAJOR-3); stand-allow `GIT_READ_ONLY_TOOLS` alongside
  `READ_ONLY_TOOLS`; and do **not** wire `git_stage` until
  BLOCKER-1 is fixed — the tools are currently reachable only by
  direct construction, which is why BLOCKER-1 is latent.
- IMPL-04 §13.3's hand-built-plan path-revalidation gap
  (`PatchApplier.apply` trusts `entry.relative_path`) is real but
  out of that task's write-set; worth scheduling: every live path
  goes through `dry_run` + the tools' jail, so it is defence-in-depth.
- Sibling N1.1/N1.3 tools were not in scope; no interaction found
  beyond the shared `posixpath`/jail contracts, which both suites
  exercise consistently.

## 9. What I could not verify

- **Full conventions suite:** [RAN `pytest tests/conventions -q` →
  timed out (>120 s) on this Windows host]. Only
  `test_docs_references.py` (5 passed) completed. I do not claim the
  import-conventions lock or the full 34.
- **Full test suite:** not run (scoped-checks standing order).
- **Full `mypy src`:** mypy re-run only on the three touched
  sources (clean); the "3 pre-existing" baseline is the impls'
  claim, not independently re-verified here.
- **Timeout/spawn-failure git paths** (120 s kill, missing `git`):
  structurally mapped in code (`git_tools.py:231-250`), unreachable
  in this environment — same caveat as IMPL-06 §14.
- **Symlink check-vs-use race** between jail and apply (§5): not
  probed; hash check covers content change, not same-content swaps.
- Probe script left at
  `C:\Users\devan\AppData\Local\Temp\opencode\w1_rev03_probe.py`
  (outside the repo; not committed). No processes started; nothing
  to kill.

## 10. Verdict rationale

The two core mechanisms this wave exists for — hash-verified apply
with unwind, and a closed git argv surface — are implemented at the
right layer and re-verified green by me. The defects are real but
bounded and all fixable without redesign: pathspec expansion must
be checked, not just named (BLOCKER-1); the planner's trailing-
newline heuristic must describe the *new* side for MODIFY
(MAJOR-1); `edit_file` must enforce the result ceiling it
advertises (MAJOR-2); and N1.5 must land the `git_stage*` glob
before it wires anything (MAJOR-3). Because the git tools are not
yet reachable from `CoderAgent.build`, none of the git findings is
live today — hence APPROVE-WITH-CHANGES (fix BLOCKER-1 + MAJORS,
or explicitly accept the MODIFY newline semantics in the contract)
rather than REJECT. The fs-write exact-bytes claim should be
corrected to match whichever semantics survives.

## 11. Commands run (reviewer, verbatim)

| Command | Result |
|---|---|
| `.venv/Scripts/python -m pytest tests/coder/test_fs_write_tools.py tests/validation/test_patch.py -q` | 96 passed (46 fs-write + 50 patch) |
| `.venv/Scripts/python -m pytest tests/coder/test_git_tools.py -q` | 36 passed |
| `ruff check` on all 6 touched files | All checks passed! |
| `mypy` on `fs_write_tools.py`, `git_tools.py`, `patch.py` | no issues found in 3 source files |
| `pytest tests/conventions/test_docs_references.py -q` | 5 passed |
| `pytest tests/conventions -q` | timed out >120 s (see §9) |
| red-team probe script (P1–P10, §5) | 7 holds, 3 confirmed defects |
