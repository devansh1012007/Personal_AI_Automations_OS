<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-06 | task: N1.4 | status: DONE
code_commits: 481a8d3 | report_commit: PENDING
files_changed: 3 (src/paa/coder/git_tools.py, tests/coder/test_git_tools.py, reports/w1-impl/W1-IMPL-06-N1-4.md)
contracts_changed: new module paa.coder.git_tools (build_git_tools + GIT_READ_ONLY_TOOLS + GIT_MUTATING_TOOLS); NEED filed for N1.5: +git_stage* in permissions.py _MUTATING_TOOLS + standing-allow wiring in agent.py
tests: added 36, changed 0; scoped run: pass (36/36 test_git_tools; ruff 0 own files; mypy 3 pre-existing only; conventions 33/34 — 1 failure external, see §13)
ownership_violations: none
deferred_dependencies: git_stage* glob (permissions.py) + READ_ONLY standing-allow (agent.py) land in N1.5 — outside write-set; PLAN/ASK matrix tests are written against that post-N1.5 policy
out_of_scope_findings: 2 (conventions lock failure from sibling uncommitted patch.py edit; repo-configured git exec residual filter/fsmonitor)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none material — the one open judgement is enforcing workspace-root == repo-toplevel (subdir workspaces refused), which is the literal brief reading; a one-line removal if reviewers disagree
confidence: high (36 behavior tests against real git + hostile inputs; the 2 untested paths are timeouts and spawn-failure, both unreachable in CI shape)
last_comms_line_read: 2026-11-09T10:25:59Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W1-IMPL-06 — N1.4 Git tools (git_status, git_diff, git_stage, git_commit)

## 1. Task and acceptance criteria (restated)

T-N1-4 (plan §E N1.4, ADR-0045, D-7): `src/paa/coder/git_tools.py` with `git_status`,
`git_diff` (read-only), `git_stage`, `git_commit` (mutating; `git_stage` is fixed-argv
`git add -- <paths>`, jail-then-protected). No push/force/config/hooks — any call
attempting those args rejected at the tool boundary. Workspace root = git repo root
jailed. Tests in `tests/coder/test_git_tools.py` (slow where needed): commit with no
staged changes fails cleanly, no remote operations possible, each tool correct in
PLAN (mutating refused), ASK (prompt for mutating only), LOCKDOWN. Acceptance: tests
pass, ruff 0, mypy 3 pre-existing baseline total (no new errors). Owner fix: name is
`git_stage` (never `git_add` — matches zero PLAN globs [VERIFIED
src/paa/coder/permissions.py:127-137]). The `git_stage*` glob addition itself is
out-of-scope (permissions.py is read-only for me) — NEED to N1.5; my permission tests
are written against the post-N1.5 policy.

(Sections appended as work proceeds.)

## 2. Verification that the problem exists (evidence gathered myself)

- [VERIFIED src/paa/coder/runtime_tools.py:73-82] `READ_ONLY_TOOLS` has 6 ledger/memory
  tools; there is no filesystem/shell/git tool anywhere [VERIFIED grep zero hits for
  `git_status|git_diff` in src/paa]. The coder cannot read, edit, test, or commit in
  a working tree.
- [VERIFIED src/paa/coder/permissions.py:127-137] `_MUTATING_TOOLS` is
  `write*, edit*, bash*, shell*, apply_patch*, *delete*, *write*, send_*, *_commit*`.
  `git_add` matches **zero** of them — the plan's B-3 — so a `git_add` tool would be
  PLAN-exempt. `git_commit` matches `*_commit*`; `git_status`/`git_diff` match nothing.
- [VERIFIED src/paa/coder/workspace.py:96-362] the frozen ADR-0045 jail exists
  (`Workspace{root}`, `assert_inside_workspace` ws-first/Path-only, `is_protected`
  with frozen reason strings). My module is its first consumer.
- [VERIFIED src/paa/coder/loop.py:85-97] the `Tool` protocol is
  `name`, `description`, `schema()`, `async run(arguments) -> str`. No git-shaped
  tool implemented it yet.
- [VERIFIED src/paa/sandbox/subprocess_backend.py:350,466-488] the established
  subprocess pattern (capped store/full drain reads, timeout kill). Reused as
  reference, not imported (private internals).
- [RAN `git --version` -> `git version 2.51.0.windows.1`] host git present; tests
  drive real git repos in `tmp_path`.

## 3. Root cause

Architectural entry point: Wave 1 needs five tool modules (read/write/bash/git) that
all mutate a real working tree under one jail. The git pair of vulnerabilities is
twofold: (a) **PLAN exemption by accident** — the mutating vocabulary predates the
tools, so a new mutating name (`git_add`) silently escapes the PLAN guarantee; (b)
**free-form argv as a capability** — any tool that passes model-controlled argv to
git re-introduces push/force/config/hooks (remote effects, impossible history, repo
config with exec hooks), which D-7 forbids. The proper fix is one module whose argv
is a closed set of code-built tuples, whose jail sits inside `run()` below every
permission decision, and whose mutating names are spelled to match the (post-N1.5)
PLAN globs — with the glob fix itself filed as the N1.5 NEED since
permissions.py/agent.py are outside my write-set.

## 4. Approach chosen and why (alternatives rejected)

One new module `src/paa/coder/git_tools.py`, stdlib-only plus the frozen workspace
jail (no optionals, no new deps):

- **Fixed argv tuples in code, model gets only typed args (paths/message/staged).**
  Alternative — a generic `git(args)` passthrough — rejected: it is the exact D-7
  violation (push/force/config reachable). Alternative — fixed argv but model
  selects subcommand from an enum — adds no safety over four honest tools, and fails
  the same way if the enum ever widens. Four tools, each one argv shape.
- **Jail inside `run()` via `_preflight` + `_jail_paths`, not via the permission
  layer.** The permission policy decides *whether*; the jail decides *where*.
  A bypass/allow rule must never make an out-of-jail path stageable. Same ordering
  ADR-0045 freezes for the other work tools.
- **Workspace root must be the git repository root**, enforced per call by
  `git rev-parse --show-toplevel` canonical-compare. Alternative — let git walk up
  (`cwd=ws.root` alone) — rejected: a workspace inside a fixture would commit into
  the nearest enclosing repo (possibly the PAA repo itself), outside the jail.
  Alternative — no preflight — leaves exactly that walk-up hole. Judgement call
  logged; removal is one check if reviewers prefer walk-up semantics.
- **Hooks disabled with internally fixed `-c core.hooksPath=` (never model input).**
  `--no-verify` only skips pre-commit/commit-msg, not post-commit; empty hooksPath
  stops all commit hooks. This is a *disabling* fixed override, not a writable
  config: there is deliberately no config tool and the schema has no argv escape.
  Plus `--no-gpg-sign` (no signing program execution), `--no-pager`,
  `--no-ext-diff`/`--no-textconv` on diff (no repo-configured external drivers).
- **Env allowlist + forced `GIT_TERMINAL_PROMPT=0`.** Rejected: pass-through of the
  ambient env (a `GIT_SSH_COMMAND`/`GIT_CONFIG_COUNT` in the process env would run
  or re-root); rejected: `Sandbox.run` (git is not a shell workload — no shell,
  no timeout-tree story needed, and the sandbox's `SandboxSpec` contract is for
  N1.3's bash). The allowlist is the narrow, testable equivalent.
- **Errors as JSON values** (runtime_tools convention), not raises. A refusal
  (protected path, no staged changes, not a repo) is *information* for the model,
  in the same channel as successes.
- **Output capped at 64 KB store/full drain, `truncated` marked** (ADR-0045's
  frozen agent-facing cap; sandbox pattern reused, not duplicated from a private
  module by import).

## 5. What changed (per file)

### src/paa/coder/git_tools.py (new, 625 lines)

- Exports: `build_git_tools(ws) -> dict[str, Tool]` (keys `git_status`, `git_diff`,
  `git_stage`, `git_commit`), `GIT_READ_ONLY_TOOLS` (frozenset of the two read-only),
  `GIT_MUTATING_TOOLS` (tuple `("git_stage", "git_commit")`). N1.5 wires these into
  `CoderAgent.build` exactly like `READ_ONLY_TOOLS`/`MUTATING_RUNTIME_TOOLS`.
- `git_status`: `git --no-pager status --porcelain=v1 --untracked-files=normal`.
  Payload `{status, exit_code, truncated}`.
- `git_diff`: `git --no-pager diff --no-ext-diff --no-textconv [--cached] -- [paths…]`;
  `paths` jail-then-protected (all-or-nothing), `staged` bool. Payload
  `{diff, staged, exit_code, truncated}`.
- `git_stage`: `git --no-pager add -- <paths…>`; `paths` required, 1–100 strings;
  each validated → jail → protect, whole batch refused on first violation. Payload
  `{staged, output, exit_code}`.
- `git_commit`: `git --no-pager -c core.hooksPath= commit --no-gpg-sign -m <message>`;
  `message` required, non-blank, ≤2048 chars; success payload
  `{commit, message, output, exit_code}` with full SHA from `rev-parse HEAD`
  (failure of that lookup degrades to the commit stdout — never a re-raise over an
  already-committed result).
- Shared: `_preflight` (per-call rev-parse canonical check: not-in-repo /
  not-repo-root refusals, with `toplevel`/`workspace` detail); subprocess caps
  (64 KB store/full drain, 120 s kill-timeout, 5 s drain bound); env allowlist;
  `OSError` on spawn → `{"error": "git executable could not be started: …"}`;
  timeout → `{"error": "...", "timed_out": true}`. Errors never raise.

### tests/coder/test_git_tools.py (new, 36 tests, all `pytest.mark.slow`)

Permission matrix (against the post-N1.5 policy shape
`mutating_tools=(*DEFAULT_MUTATING_TOOLS, "git_stage*")` — see NEED below):
PLAN allows `git_status`/`git_diff`, denies both mutating tools; ASK prompts only
for mutating; LOCKDOWN-as-deny-all (`deny=["git_*"]`) denies all four; plus an
`AgentLoop` PLAN end-to-end test asserting HEAD is unchanged after a denied
commit. Execution: status shows untracked/modified/staged; diff shows the change
and respects `paths` scoping + `staged`; stage→status→diff cross-checked; 200 KB
rewrite diff truncated+marked at 64 KB; commit advances HEAD with full SHA;
commit-with-nothing-staged → `{"error", exit_code: 1}`, HEAD unchanged; empty
repo → clean error; non-repo → refused `not inside a git repository`; subdir of
repo → refused `not the git repository root` with toplevel detail. Jail:
`.git/config` → `protected path: git`, `.env` → `protected path: env`, `..` escape
refused, batch with one protected path stages nothing, `git_diff` cannot reach
protected paths. Hostile: `--upload-pack=…`/`--force` as pathspec are files not
options (no marker file); `{"message": "--amend"}` is a subject (commit count 2,
subject literal); `remote`/`refspec` keys ignored; hooks: a failing pre-commit
does not gate the commit and a post-commit `touch` never executes; ambient
`GIT_DIR` (bogus) + `GIT_SSH_COMMAND` never redirect/run anything. Remote: a bare
`origin` gains zero refs after stage+commit (no push possible, not just untested).
Validation: empty/blank/non-string/over-100 paths, missing/blank/over-long
message all → error payloads.

## 6. Blast radius and contracts

New module + new test file only; no existing file touched (scratch-verified below:
`git status` shows only my two untracked files + pre-existing sibling WIP).
`__all__` is exactly the three names N1.5 needs. No ledger, schema, config, API,
or CLI change. The PLAN/ASK guarantee for `git_stage` depends on the N1.5 NEED
(`+git_stage*` in `_MUTATING_TOOLS`); `git_commit` already matches `*_commit*`
under today's literal, so PLAN refusal for at least the commit half holds today.
Schemas are JSON-Schema with `additionalProperties: False` (a model proposing
extra keys violates its own advertised schema; `run()` ignores unknown keys
defensively regardless).

## 7. Tests (how each confirmed to fail without fix)

36/36 pass [RAN `pytest tests/coder/test_git_tools.py -q` -> 36 passed].
Mutation reasoning, one per mechanism (verified by the test design, not by
committing a broken variant):

- Preflight removed → `test_workspace_below_repo_root_is_refused` fails (status
  would walk up and report the parent repo).
- `core.hooksPath=` removed → `test_hooks_never_execute` fails (failing pre-commit
  gates the commit → exit 1).
- Env allowlist removed → `test_ambient_git_env_is_never_inherited` fails (bogus
  GIT_DIR makes rev-parse fail → status errors).
- `_jail_paths` cwd-relative bug (the actual incident below) → 13 tests failed
  with `path escapes the workspace jail` for every in-repo path — observed red
  before the fix, green after. This is the one mutation I genuinely witnessed.
- `--` after `add`/`diff` removed → dash-pathspec tests still error (unknown
  option), so they pin *no flag executed*, not the `--` itself; the `--` is
  defense-in-depth that keeps git's pathspec parser on the right side of the
  boundary.
- Remote tests removed → nothing catches a future `push` regression; the bare-repo
  `for-each-ref == ""` assertion is the behavioral proof of "no remote ops".
- Permission matrix tests against the bare default policy would have *false-passed*
  PLAN for git_stage — the suite deliberately constructs the post-N1.5 policy, so
  the test is written for the contract, not for today's accident.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- Source: one canonicalisation per path per call (O(path length), stat-only) +
  one rev-parse subprocess per tool call (~10 ms). No indexes, no state to keep
  consistent; workspace is immutable input.
- Load (100x, concurrency, partial failure): concurrent tool calls share nothing
  but short-lived subprocesses; git's own lock handles index races (a second
  `git add` overlapping the first would get git's own "lock" error as a clean
  payload — exit 128 path). No retries, no sleeps. A timeout kills the child and
  reports `timed_out` — never retries a git operation (an add/commit retry could
  double-effect).
- Hostile input: traversal/absolute paths (jail), protected staging (`.git`/`.env`/
  PAA_HOME), flag-shaped pathspecs (paths after `--`), subcommand-shaped extra
  keys (ignored), `--amend`-shaped message (fixed `-m` pair), hook scripts
  (hooksPath empty), repo-configured external diff/textconv/pager drivers
  (flags), signing programs (`--no-gpg-sign`), `GIT_*` env overrides (env
  allowlist), over-long messages/path lists (bounds), over-large output (cap).
- Operability: every refusal is a stable JSON payload (`error`, `reason` where
  protected, `toplevel`/`workspace` where root-mismatched, `exit_code`,
  `detail` truncated to 500 chars) — safe to log, no secrets, no env values.
  `str`-detail truncation avoids dumping 64 KB through the transcript.
- Reversal: delete the module + revert the test file; no schema/ledger/config
  change anywhere. N1.5's two-line registration would also just lose its imports.

## 9. Edge cases and abuse scenarios

Covered: repo root≠workspace (subdir), non-repo, bare-repo workspace (rev-parse
errors → "not inside a git repository"), unborn HEAD, nothing staged, empty
message, over-long message, >100 paths, blank/non-string paths, dash pathspecs,
worktree `.git` file (top-level part == `.git` protected), symlinked path into
`.git` (canonical → protected), case variants `.GIT`/`.ENV` (workspace casefold
matching, inherited), huge diffs (capped+drained, `truncated` marked), timeout
(kill + `timed_out` value), spawn failure (`OSError` → payload). TOCTOU note:
a symlink swapped between the per-path jail and the git spawn is git's own
hash-check domain (the same best-effort caveat ADR-0045 records for the jail).

## 10. Performance and security

New attack surface: one subprocess per call plus a preflight rev-parse.
Child env allowlist (PATH/home/SystemRoot/TEMP+TMP only, every `GIT_*` dropped,
`GIT_TERMINAL_PROMPT=0` forced) — kills the env-injection family at the spawn
boundary. argv is provably fixed (constructed from literals + validated
path/message values only). Hooks: empty hooksPath means *no* hook file ever runs
(pre-commit/commit-msg/post-commit). Signing: `--no-gpg-sign`. Diff drivers:
`--no-ext-diff --no-textconv`. Pager: `--no-pager`. Jail+protection sit inside
`run()`, below permissions. Residual stated in the module docstring: repo-config
filter drivers (`filter.*.clean` on add) and `core.fsmonitor` (on status) are
repo-controlled executables inherent to driving git; `.git` is protected from
agent writes and there is no config tool, but a workspace handed over with
hostile `.git/config` is a hostile input to any git client — filed as an
out-of-scope FINDING for ORCH.

## 11. Commands run and results (verbatim summaries)

- [RAN `.venv\Scripts\python.exe -m pytest tests/coder/test_git_tools.py -q` ->
  36 passed] (after two self-review fix rounds; intermediate red: 13 failed on
  the cwd-relative jail bug, fixed; 1 truncation-test premise error, fixed).
- [RAN `.venv\Scripts\python.exe -m ruff check src/paa/coder/git_tools.py
  tests/coder/test_git_tools.py` (and over final versions) -> All checks passed!]
- [RAN `.venv\Scripts\python.exe -m mypy src/paa/coder/git_tools.py` -> Success:
  no issues found in 1 source file]
- [RAN `.venv\Scripts\python.exe -m mypy src` -> Found 3 errors in 1 file
  (subprocess_backend.py:548), checked 147 source files] — exactly the baseline
  3 pre-existing Windows errors; no new errors.
- [RAN `.venv\Scripts\python.exe -m pytest tests/conventions -q` -> 33/34 pass;
  one failure `test_every_referenced_line_is_not_blank` on
  `docs/modules/validation.md:46` citing `src/paa/validation/patch.py:786`
  blank — caused by the sibling's uncommitted patch.py edits (see §13).
  Retried once after a wait: still red. With that file ignored:
  29/29 pass.]
- Full suite NOT run (scoped-checks rule). No docker. No background processes.

## 12. Deviations from plan/brief

None from ADR-0045 or D-7. Judgement calls ADR/brief left open, all documented in
code + here:

1. **Repo-root enforcement** (`ws.root` must equal `rev-parse --show-toplevel`):
   the literal reading of the brief's "Workspace root = git repo root jailed".
   A subdir workspace is refused with toplevel detail instead of silently
   operating on the enclosing repo.
2. **`LOCKDOWN` mapping**: the coder `ToolPermissionPolicy` (permissions.py)
   has no LOCKDOWN mode (that enum is `core/types.py`'s system posture).
   "LOCKDOWN" in T-N1-9 is tested as deny-rules (`deny=["git_*"]`), which is
   precedence 1 and mode-independent in the coder policy — the operator
   lockdown mechanism that actually exists for the loop.
3. **Hooks suppression via fixed `-c core.hooksPath=`**: an internally fixed
   disabling override, not a writable config; D-7's "no hooks" is satisfied
   literally (no hook ever executes) and there is no config surface.
4. **Bare worktree handling**: ws-root `.git` *file* (linked worktrees) resolves
   via `--show-toplevel` to the worktree root; staging `.git` itself is refused
   as `protected path: git` (top-level clause, inherited).
5. **Timeout (120 s) / path cap (100) / message cap (2048)**: new bounds where
   the ADR froze only the 64 KB output cap; chosen to match the sandbox default
   timeout and to keep argv/jail cost bounded. None retries.

## 13. Out-of-scope findings

1. **`tests/conventions` 33/34 — external failure, not fixed (outside write-set).**
   `test_every_referenced_line_is_not_blank` fails on
   `docs/modules/validation.md:46` citing `src/paa/validation/patch.py:786`,
   blank because the sibling W1-IMPL-04's uncommitted patch.py edits shifted the
   file (their NEED in COMMS confirms the renumbering WIP). My write-set cannot
   touch patch.py or validation.md; retried once, still red. Gate will be green
   once the sibling's lines settle. No conventions failure is attributable to my
   files (both files pass ruff; conventions with the docs-references file
   excluded: 29/29).
2. **Repo-configured git executables (filters/fsmonitor)** — residual exposure
   documented in the module docstring. Mitigations in place (`.git` protected,
   env allowlisted, no config tool); if ORCH wants them closed it needs policy
   at the workspace/config level (e.g. refusing workspaces whose `.git/config`
   defines filters/fsmonitor), not per-tool argv flags. Not implemented here
   (no flag exists for filters on `git add`).

## 14. Known limitations, risks, rollback notes

- `test_stage_with_*_flag` tests pin "no flag executed / clean error", not the
  `--` specifically (removing `--` still errors on unknown option). The `--`
  remains as boundary defense.
- Timeout and spawn-failure (missing git) paths are structurally mapped but
  unexecuted (no watcher tool in CI shape; git is required for the tests
  themselves). A host without git cannot run this suite — same as the N1.0a
  host dependency class (documented in mission baseline R8-style risks).
- `core.fsmonitor`/filter-driver executables run from repo config on add/status
  (see §13.2). Acceptable by D-7's fixed-surface threat model; stated openly.
- Rollback: delete `src/paa/coder/git_tools.py` + `tests/coder/test_git_tools.py`
  (two-way, no schema/ledger/config change). N1.5's registration imports
  `build_git_tools`, so landing order matters (this commit first).

## 15. Where I am least confident

Lowest confidence: repo-root enforcement (judgement call §12.1). If S1's fixture
workspace is intended to be a subdirectory of a larger repo (e.g.
`examples/fixtures/buggy_calc/` inside the PAA repo without its own `.git`),
`git_commit` will refuse and S1 will need the fixture initialized as its own
repository (or the check dropped). The brief says "Workspace root = git repo
root", so I implemented the strict reading; the operator can confirm at N1.5.

## 16. Self-review checklist

- Diff re-read from scratch (full file): only write-set files; no sibling work
  touched (sibling WIP in patch.py/fs_write_tools.py verified untouched by
  `git status` inspection; my code commit lists only my two files).
- Self-review MAJOR (fixed pre-commit): first draft's `_jail_paths` passed bare
  `Path(raw)` — cwd-relative — so the jail refused every in-repo path (13 red
  tests). Root-caused with a repro script before fixing; fix joins `ws.root /
  Path(raw)` (pathlib semantics keep absolute-inside paths on the same jail
  terms). Documented in the function docstring.
- Self-review MAJOR (fixed pre-commit): `_collect` had a bare `raise` with no
  active exception in the `CancelledError` path, and the dash-flag tests'
  truncation premise was wrong (staged-then-tweaked diff is tiny). Both fixed.
- Self-review MINOR (fixed pre-commit): dead `_git_error` helper removed;
  post-commit `rev-parse` OSError guarded so a successful commit is never
  reported as failed; `_relative_paths` resolves the root defensively.
- Self-review MINOR (fixed): E501 line length in the rev-parse guard; unused
  `Mapping` import/type-ignore in tests removed.
- Mutation mentalist: §7 lists each mechanism and the test that would catch its
  removal; one (the cwd-relative jail) was caught by real red tests, not by
  inspection — the suite as a whole has teeth.
- Facts tagged [VERIFIED]/[RAN]; no full-suite claim; scoped-checks rule honoured;
  explicit-path commits only; branch `ops/20261109-next-spec` throughout.

## 17. Commits

Code commit: `481a8d3` (files: src/paa/coder/git_tools.py,
tests/coder/test_git_tools.py) — explicit paths, trailers Task/Agent/Wave/Report.
Report commit: PENDING (this file; lists the code SHA).
Predecessor anchor: N1.0a code `7c33723`, review APPROVE `a0a27ff` (workspace
jail this module imports).
NEED for N1.5 (outside write-set, filed in COMMS): add `git_stage*` to
`_MUTATING_TOOLS` in `src/paa/coder/permissions.py:127` (owner fix verbatim)
and standing-allow `GIT_READ_ONLY_TOOLS` in `CoderAgent.build` (agent.py)
with `mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS)` —
PLAN then denies `git_stage`/`git_commit`, ASK prompts only for them. My
permission tests are written against exactly that policy and stay green when
the NEED lands.
