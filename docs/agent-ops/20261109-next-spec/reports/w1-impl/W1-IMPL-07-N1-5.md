<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-07 | task: N1.5 | status: DONE-WITH-DEFERRED-NEEDS
code_commits: 0d02ea8 | report_commit: PENDING
files_changed: 8 (src/paa/coder/agent.py, src/paa/coder/runtime_tools.py, src/paa/cli/coder.py, src/paa/coder/__init__.py, src/paa/coder/permissions.py, src/paa/agents/specialists/coding.py, tests/agents/test_coding_specialist_p011.py, tests/coder/test_new_integration.py)
contracts_changed: CoderAgent.build gains workspace/journal kwargs; new build_work_tools (agent.py, re-exported by paa.coder); READ_ONLY_TOOLS 6->11 (import-union); DEFAULT_MUTATING_TOOLS +git_stage*; CODING_TOOL_NAMES 9->20; cli/coder.py gains --workspace
tests: added 13, changed 1 (specialist pin 3->9 mutating); scoped run: pass (213 passed + 7 skipped pwsh-absent on the brief's 6-file set; ruff 0; mypy 3 pre-existing)
ownership_violations: none
deferred_dependencies: 2 NEEDs posted (test_runtime_tools.py:801-805 catalogue-pin re-point; 7 blank + silent docs-citation re-point for ORCH sweep). No other deps; MINOR-4 ADR overview:206 sweep deferred to ORCH per brief.
out_of_scope_findings: 2 (the 2 NEEDs; prompt_yes docstring staleness fixed in-scope, see §13)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none material — the one judgement call is Workspace-typed (not str) for CoderAgent.build; CLI resolves the string at its boundary (§4)
confidence: high (probe mutation witnessed red for the right reasons; real-build merge test; all scoped gates run post-fix)
last_comms_line_read: 2026-11-09T11:50:46Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W1-IMPL-07 — N1.5 registration + permissions

## 1. Task and acceptance criteria (restated)

T-N1-5 (N1.5 serial integration, plan `docs/plans/2026-11-09-next-spec-revised.md:67`):
`build_work_tools(runtime, workspace)` merges runtime + fs_read + fs_write +
shell + git into one tools dict; `cli/coder.py` gains `--workspace` threading
to `CoderAgent.build(workspace=...)` → `build_work_tools`; extend
`READ_ONLY_TOOLS` with `git_status`/`git_diff`/`read_file`/`glob`/`grep` and
stand-allow the git read-only + fs reads (ASK never prompts for reads); add
`git_stage*` to `DEFAULT_MUTATING_TOOLS` (literal) so `git_stage` is mutating
(PLAN denies, ASK prompts); verify every mutating name matches a
`DEFAULT_MUTATING_TOOLS` pattern via a new test that fails if any mutating
escapes; update the coding-specialist coverage pin deliberately with reason
recorded. End with `agent.py` frozen except an explicit `journal=None` kwarg
slot for Wave 2 (N2.2).

Acceptance: mutating-coverage test fails on an uncovered probe name (prove
red, then remove); PLAN/ASK/LOCKDOWN matrix per tool passes; the brief's
scoped pytest set passes; ruff 0; mypy 3; conventions 34. (The brief's
"tests/coder/test_new_integration.py (mutating coverage only)" is where the
new test lands.)

Skip list honoured: no ToolJournal (Wave 2a); no MAJOR-1 (grep)/MAJOR-2
(walk)/BLOCKER-1 (pathspec) fixes; no `docs/architecture/overview.md`
(MINOR-4 ORCH sweep — noted deferred, not touched).

## 2. Verification that the problem exists (evidence gathered myself)

- [VERIFIED src/paa/coder/agent.py:186 (pre-change)] `tools =
  build_runtime_tools(runtime)` built only the 9 ledger/memory tools; no
  filesystem/shell/git factory was imported or called anywhere in
  `src/paa/coder/agent.py`. The five Wave-1 tool modules existed but were
  unreachable from `CoderAgent.build`.
- [VERIFIED src/paa/coder/permissions.py:127-137 (pre-change)] `_MUTATING_TOOLS`
  is `write*`, `edit*`, `bash*`, `shell*`, `apply_patch*`, `*delete*`,
  `*write*`, `send_*`, `*_commit*` — no `git_stage*`. [RAN fnmatch probe]
  `git_stage` matched zero globs, reproducing W1-REV-03 MAJOR-3 (PLAN-allowed
  by default policy, P8). `git_commit` matched `*_commit*`; `git_status`/
  `git_diff` matched nothing.
- [VERIFIED src/paa/coder/runtime_tools.py:73-82 (pre-change)] `READ_ONLY_TOOLS`
  is 6 names; `FS_READ_TOOLS` [VERIFIED src/paa/coder/fs_read_tools.py:73] is
  `read_file`/`glob`/`grep` and `GIT_READ_ONLY_TOOLS` [VERIFIED
  src/paa/coder/git_tools.py:105] is `git_status`/`git_diff` — none of the
  five stood-allowed, so ASK prompts for every read (W1-REV-02 §8).
- [VERIFIED src/paa/coder/fs_read_tools.py:100 build_fs_read_tools(ws);
  src/paa/coder/fs_write_tools.py:148 build_fs_write_tools(workspace,
  session=None); src/paa/coder/shell_tools.py:585 build_shell_tools(workspace,
  sandbox=None); src/paa/coder/git_tools.py:668 build_git_tools(ws);
  src/paa/coder/runtime_tools.py:90 build_runtime_tools(rt)] factory shapes.
  The workspace-bound factories need only a `Workspace`; `build_shell_tools`
  accepts the runtime's sandbox so a `NAMESPACE` backend makes
  `allow_network=False` enforceable [VERIFIED shell_tools.py:235-263 docstrings].
- [VERIFIED src/paa/coder/workspace.py:96-100,198-236] frozen `Workspace`
  dataclass + `resolve_workspace(raw: str) -> Path` (existing dir required).
- [VERIFIED src/paa/cli/coder.py:57-93 (pre-change)] no `--workspace` option;
  the build call at :164 passed no workspace. `tests/coder/test_new_integration.py`
  did not exist [VERIFIED glob: 11 coder test files, none named new_integration].

## 3. Root cause

Integration absence, not a defect in any tool: Wave 1b built five tool
modules against the frozen `Workspace` jail, but the composition root
(`CoderAgent.build`) still built only the ledger/memory catalogue, and the
permission vocabulary predated the tools. Two consequences, both load-bearing
for the brief's "BLOCKER gate" framing:

1. **PLAN exemption by accident (MAJOR-3, now fixed here).** The vocabulary
   was written for conventional names; `git_stage` matches none of them, so
   wiring the tool without the glob would silently exempt staging in PLAN
   mode. The tools themselves cannot fix this — the glob lives in
   `permissions.py`, outside the tool modules — so N1.5 is the only place
   it can land, in the same commit as the wiring.
2. **Reads prompt in ASK (W1-REV-02 §8).** The standing-allow listed only the
   six runtime reads. Until the five work-tool reads join it, the prompt
   fires for `read_file({})` — the exact friction the standing-allow exists
   to prevent.

## 4. Approach chosen and why (alternatives rejected)

- **`build_work_tools` lives in `agent.py` (the composition root).**
  `agent.py` is the module whose job is combining catalogues ("one place that
  builds a real runtime, hands the runtime's own ledger to the tools" —
  module docstring). Putting it in `runtime_tools.py` would make the
  ledger-surface module depend on the work-tool modules (fs_write pulls
  `validation.patch`). As a composition-root function it simply calls the
  five factories and refuses key collisions. `__init__.py` re-exports it
  next to `build_runtime_tools` (the established export pattern).
- **`workspace: Workspace | None = None` on both `CoderAgent.build` and
  `build_work_tools`; `None` ⇒ runtime-only catalogue.** Existing callers
  (`tests/coder/test_agent.py` builds without a workspace) must not break,
  and the work tools must not be jailed to an "implied directory" (CWD) the
  operator never approved. `None` therefore means "no working tree to work
  in", honestly offering 9 tools. Rejected: required `workspace` (breaks all
  existing builds); defaulting to CWD (exposes write/shell/git tools
  jailed to a tree nobody named).
- **`CoderAgent.build` takes `Workspace` (canonical value); the CLI resolves
  the string at its boundary.** `resolve_workspace` (existing-dir,
  canonicalise, deny-on-doubt) runs in `cli/coder.py::_do()` before the
  build; an unresolvable `--workspace` prints `invalid workspace` and exits
  1 before any runtime exists — the same fail-fast-at-the-boundary as the
  existing `--mode`/`--modality` validation. Rejected: accepting `str` in
  `build` and resolving inside (boundary logic in the composition root;
  `build_work_tools` would need `Workspace | Path | str` flexibility like
  `build_fs_write_tools` has — the wrong layer to own string parsing).
- **`READ_ONLY_TOOLS` extends by imported union, not re-spelled strings.**
  `READ_ONLY_TOOLS = {6} | FS_READ_TOOLS | GIT_READ_ONLY_TOOLS`. The
  alternative (hardcode the five strings in `runtime_tools.py`) is a second
  source that drifts when a read tool lands; the alternative (a separate
  union in the agent's `allow=[...]`) would leave the brief's literal
  "extend READ_ONLY_TOOLS" unmet and split the standing-allow across two
  spellings. The import direction is cycle-free (both modules import only
  the workspace jail; [VERIFIED] `validation/patch.py` does not import
  `paa.coder`) and both modules are stdlib-only, so a minimal install still
  imports. `build_runtime_tools` still builds exactly the nine ledger tools
  — the union is a *name* set for classification, commented as such.
- **Specialist pin grows `CODING_TOOL_NAMES` 9 → 20 by literal append.**
  Kept as a literal tuple (not imported) per the existing comment's rationale
  (enum fixed at import time; the coverage test reads what the model can
  emit). The specialist *proposes* tool calls and never executes them, so
  offering the mutating names changes no execution path — but a narrower
  enum than the tools dict could never propose a work-tool step.
- **Journal guard: accept `journal` and raise `NotImplementedError` on
  non-None, before any runtime is built.** A silent no-op would attest that
  journalling is on when nothing is journalled; building first and then
  raising would leak the runtime (found in self-review, §16 — the guard
  originally sat after `Runtime.build` and hung a probe on Windows file
  lock). Failing first is both loud and leak-free. N2.2 replaces the guard
  with real wiring.

## 5. What changed (per file)

WRITE-SET only (8 files). `COMMS.md` appends are via COMMS only (my `git add`
lists none of it — only ORCH commits COMMS).

### src/paa/coder/permissions.py (+1 line)
`_MUTATING_TOOLS` gains `"git_stage*"` (after `"*_commit*"`). `git_commit`
was already matched by `"*_commit*"`. [RAN post-fix: PLAN `git_stage`→DENY,
`git_commit`→DENY, `git_status`→ALLOW; ASK `git_stage`→ASK.]

### src/paa/coder/runtime_tools.py (+2 imports, +12 comment lines, union)
Runtime imports `FS_READ_TOOLS` and `GIT_READ_ONLY_TOOLS` (alphabetical
first among the `paa.*` imports — ruff isort clean); `READ_ONLY_TOOLS`
becomes `{6} | FS_READ_TOOLS | GIT_READ_ONLY_TOOLS` (11 names: adds
`git_status`, `git_diff`, `read_file`, `glob`, `grep`). [RAN: `len ==
11`, superset of both halves.] `build_runtime_tools` untouched.

### src/paa/coder/agent.py (+5 factory/workspace imports, `__all__` gains
`build_work_tools`, 2 kwargs, docstring, 1-line body change, new function)
`CoderAgent.build(..., workspace: Workspace | None = None, journal: Any | None
= None)`; `tools = build_work_tools(runtime, workspace)`; the `allow` list is
unchanged (`[*READ_ONLY_TOOLS, ...]` now covers the five reads via the
union). `build_work_tools(runtime, workspace=None)` merges the five
catalogues when a workspace is present — `runtime.sandbox` (explicit
`is not None`, no truthiness trap) handed to `build_shell_tools`, else the
subprocess default — and raises `PaaError` if two factories claim a key
(a silent `dict.update` overwrite would dispatch to the wrong tool).
`journal` guard raises before any build (see §4). No `if TYPE_CHECKING:`
block was added or edited (new imports are runtime — factories are called,
`Workspace` is light), so the import-conventions classification is untouched.

### src/paa/cli/coder.py (--workspace + 18 lines)
New option after `--list-tools`; `_do()` resolves the string via
`resolve_workspace` → `Workspace(root=...)` (function-local import, matching
the file's deferred coder imports), prints `invalid workspace` + exit 1 on
`PaaError`, else passes `workspace=resolved_workspace`. `--list-tools` with a
workspace prints all 20; without, 9. `prompt_yes` docstring updated to name
the six work-tool mutating calls as prompt-reaching (it previously listed
only the runtime triple — stale after wiring).

### src/paa/coder/__init__.py
`from paa.coder.agent import CoderAgent, build_work_tools`;
`"build_work_tools"` in `__all__` after `"build_runtime_tools"`.

### src/paa/agents/specialists/coding.py
Module docstring + the tuple comment rewritten to describe the full
catalogue (9 mutating: runtime triple + six glob-matched work names); the
tuple appends `read_file`, `glob`, `grep`, `write_file`, `edit_file`,
`apply_patch`, `bash`, `git_status`, `git_diff`, `git_stage`, `git_commit`
(merge order, same as `build_work_tools`).

### tests/agents/test_coding_specialist_p011.py
The pin's expected mutating value grows 3 → 9 (runtime triple + 6 work
names) with an N1.5 reason comment. The `read_only == READ_ONLY_TOOLS`
comparison is untouched — it now compares 11 == 11.

### tests/coder/test_new_integration.py (new, 13 tests)
`TestMutatingCoverage` (derived-vs-pinned coverage, per-name direct check,
`git_stage`/`git_commit` MAJOR-3 guards, factory-key partition over real
constructions), `TestReadOnlyUnion` (exact-11, not-mutating, specialist
read-half equality), `TestPolicyMatrix` (PLAN / ASK / BYPASS+deny as the
LOCKDOWN stand-in — there is no coder `LOCKDOWN` mode [VERIFIED
`permissions.py:40-56`], matching the W1-IMPL-03/05/06 precedent), and
`TestBuildMerge` (two real `CoderAgent.build`s: with-workspace = 20 keys
equal to `CODING_TOOL_NAMES`; without = the original 9). See §7 for the
mutation evidence.

## 6. Blast radius and contracts

- **Shipped contracts:** `CoderAgent.build` gains two keyword-only params
  (backward compatible — defaults preserve the old 9-tool catalogue, and the
  existing `tests/coder/test_agent.py` suite passes unchanged [RAN 53
  passed with permissions/model_adapter]); `paa.coder.build_work_tools` is
  a new additive export; `READ_ONLY_TOOLS` grows 6 → 11; `DEFAULT_MUTATING_TOOLS`
  gains one glob; `CODING_TOOL_NAMES` grows 9 → 20; `paa agent` gains
  `--workspace`.
- **No schema/ledger/config change.** No new `EventType`, no migration, no
  provider surface. The permission *policy* is unchanged — only the inputs
  (`allow`/`mutating_tools` spellings) are completed to the documented shape.
- **Tool names are exactly disjoint** across the five factories (pinned by
  the existing per-factory surface tests plus `TestBuildMerge`); the
  composition-time collision raise is the backstop, not the mechanism.
- **docs `path:line` citations shift** (agent.py δ+10..+23, permissions.py
  δ+1, runtime_tools.py δ+14). Exactly one conventions test fails (the blank
  checker, 7 citations); the docs are outside my write-set, so the re-point
  is a posted NEED (§11/§13), mirroring W1-IMPL-04's precedent. No citation
  in `docs/agent-ops/` or `docs/plans/` is in the lock's scope.

## 7. Tests (how each confirmed to fail without fix)

- **New coverage suite (13):** [RAN `tests/coder/test_new_integration.py` ->
  13 passed]. Reversal reasoning per test class:
  - *Coverage:* reverting the `git_stage*` glob makes `test_git_stage...`
    and `test_every...` fail (the MAJOR-3 shape: PLAN allows `git_stage`).
    Removing a name from the pin while it stays in `CODING_TOOL_NAMES`
    fails the exact-value assert.
  - *Union:* reverting `READ_ONLY_TOOLS` to 6 fails `test_read_only...`
    (exact-11) and the specialist read-half test (11 ≠ 6).
  - *Matrix:* weakening `git_stage*` flips PLAN `git_stage` DENY→ALLOW;
    removing a read from `READ_ONLY_TOOLS` flips ASK read ALLOW→ASK.
  - *Merge:* reverting `tools = build_work_tools(...)` to
    `build_runtime_tools(runtime)` fails the 20-key assert (9 ≠ 20).
- **Witnessed mutation (brief acceptance):** temporarily added
  `"probe_uncovered_mutator"` to `EXPECTED_MUTATING` [RAN -> 4 failed, 9
  passed]: `test_every_mutating_tool_is_covered_by_policy` —
  `AssertionError: ... Extra items in the right set:
  'probe_uncovered_mutator'`; `test_each_expected_mutating_matches_directly`
  (escapes both nets); `test_plan_allows_reads_denies_mutating` (PLAN
  allows the probe — the exact MAJOR-3 failure shape); and the 20-key merge
  test. Probe removed by hand; [RAN -> 13 passed].
- **Specialist pin (witnessed by design):** the derived mutating set equals
  the 9-name pin only with the glob fix landed; before it, `git_stage`
  would derive as read and fail the exact-value assert.
- **Existing suites:** [RAN the brief's 6-file set ->
  213 passed, 7 skipped] ([ASSUMED] the 7 skips are the pre-existing
  pwsh-absent shell slow tests — identical skip shape to W1-IMPL-05's
  report; not re-enumerated). [RAN `test_agent.py` +
  `test_permissions.py` + `test_model_adapter.py` -> 53 passed].
- **Pre-existing failures caught, not caused (§13):** [RAN
  `tests/coder/test_runtime_tools.py` -> 1 failed, 69 passed] — the only
  failure is the outside-write-set catalogue pin; [RAN `tests/conventions`
  -> 33/34] — one docs-references blank-citation test (7 citations). Both
  are NEED-posted, and both are *consequences of the brief's literal design*
  (see §12), not regressions of behaviour.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- Source: one canonicalisation per tool path per call (inside the factories,
  unchanged) + one `rev-parse` per git call. `build_work_tools` itself is
  pure construction at session start; the five factories hold no per-run
  state. No new dependency, provider string, or ambient state.
- Load (100×, concurrency, partial failure): composition runs once per
  session, not per turn. Concurrent turns share the workspace value (frozen
  dataclass — immutable) and stateless tool objects; the only shared mutable
  state is the `FsWriteSession` journal created once inside
  `build_fs_write_tools` (in-memory list, per session — the Wave-1 contract,
  ledger persistence is Wave 2). No retries introduced anywhere; the git
  timeout/kill and shell timeout behaviour is inherited unchanged from the
  factories.
- Hostile input: no new input surface. `--workspace` strings go through
  `resolve_workspace` (control-char/UNC/drive-relative/blank deny) at the CLI
  boundary before any build. Tool *arguments* are still jailed inside each
  `run()` below every permission decision — the N1.5 wiring does not move
  that boundary, and `is_protected` remains consulted after the jail in
  every factory (frozen order).
- Operability: `--list-tools` exposes the offered set; invalid workspace
  exits 1 with `invalid workspace <reason>` (no traceback); the collision
  raise names the duplicated keys instead of silently dropping a tool. No
  secrets, env values, or file contents are logged by the new code.
- Reversal: revert the 8 files; no schema/ledger/config change. The
  `journal` guard is *not* a flag — removing the param name would be a
  Wave-2 rename, so the slot stays until N2.2 wires it (see §4).

## 9. Edge cases and abuse scenarios

- `--workspace` pointing at a file (not a dir) → `workspace is not a
  directory` exit 1 [VERIFIED `workspace.py:229-235` semantics]; at a missing
  dir → `workspace does not exist`; at `~` → expanded then required to
  exist. An operator who mistypes gets a refusal, not a CWD fallback.
- `--workspace` with `--list-tools` and no prompt → full 20-tool table
  (the build runs before the `list_tools` return; verified by code path,
  no tokens spent).
- `--workspace` omitted → ledger/memory tools only; the model is never
  offered `write_file` etc. (constrained enum can't emit what it can't
  name), instead of offering tools jailed to nowhere.
- `journal=<anything non-None>` → `NotImplementedError` before any runtime
  exists [RAN clean exit, no DB touched].
- A future factory claiming an existing key (e.g. a new `bash` variant) →
  `PaaError` listing the collision at composition time; the coverage tests
  additionally pin the 20-name surface so the collision is caught in CI.
- A model proposing a work tool in a `workspace=None` session → the enum
  still offers only runtime names (the specialist's `CODING_TOOL_NAMES` is
  a *proposed* surface; `CoderAgent`'s loop catalogue is the built 9) — the
  proposed name, if hallucinated outside the enum, fails provider-side
  validation, not silently skipped.
- `BYPASS` + work tools + explicit `--deny git_*` → denied (precedence 1),
  covered in the matrix test.

## 10. Performance and security

New attack surface: the CLI's new string argument, confined to boundary
resolution (canonicalise + exist + dir, deny-on-doubt, no directory
creation). Shell/git/ws tools remain jail-inside-`run()`; permission is a
*whether*, the jail a *where* — the N1.5 wiring changes neither. The
`READ_ONLY_TOOLS` import-union adds two stdlib-only module imports at
`runtime_tools` import time (measured as a fresh-interpreter import succeeding
in the conventions sweep — see §11; no optional extra, no provider SDK,
minimal-install safe). The shell tool now runs on the runtime's sandbox
instance when present, so `allow_network=False` is enforceable exactly when
the deployment configured a `NAMESPACE`+ backend — and the backend's actual
isolation is still what every call reports (no severance claim is added
here).

## 11. Commands run and results (verbatim summaries)

- [RAN `.venv\Scripts\python.exe -m pytest tests/coder/test_new_integration.py
  tests/coder/test_fs_read_tools.py tests/coder/test_fs_write_tools.py
  tests/coder/test_git_tools.py tests/coder/test_shell_tools.py
  tests/agents/test_coding_specialist_p011.py` -> 213 passed, 7 skipped in
  53.62s (post-fix final run)]
- [RAN `pytest tests/coder/test_agent.py tests/coder/test_permissions.py
  tests/coder/test_model_adapter.py` -> 53 passed]
- [RAN `pytest tests/coder/test_runtime_tools.py` -> 1 failed, 69 passed]
  (the 1 failure is the outside-write-set pin, §13.1)
- [RAN `.venv\Scripts\python.exe -m ruff check src tests` ->
  All checks passed!]
- [RAN `.venv\Scripts\python.exe -m mypy src` -> Found 3 errors in 1 file
  (checked 147 source files)] — exactly the Windows pre-existing baseline
  (`sandbox/subprocess_backend.py:548`, A-001); zero errors in any N1.5 file.
- [RAN `.venv\Scripts\python.exe -m pytest tests/conventions` -> 33/34]
  (`test_every_referenced_line_is_not_blank`: 7 blank citations — §13.2).
- [RAN CLI probes: `--workspace` in `--help` → True;
  `agent hi --workspace does-not-exist-zzz` → exit 1 with
  `invalid workspace` → True; journal-guard probe → `NotImplementedError`,
  clean exit]
- Full suite NOT run (scoped-checks rule). No docker, no migrations, no
  containers. No background processes left (the one hung probe was killed
  by the tool timeout and its temp dir was the probe's own — see §16).

## 12. Deviations from plan/brief

1. **`journal` fails fast instead of being silently accepted.** The brief
   says "explicit journal=None kwarg slot"; the slot exists with the
   default, and a non-None value raises `NotImplementedError` before any
   build. A silent accept would have been the "accepted and dropped"
   failure the codebase warns against. N2.2 replaces the guard with wiring.
2. **Runtime `Workspace` import in `agent.py`, not `TYPE_CHECKING`.** The
   import-conventions table classifies `paa.coder.agent`'s block as class C;
   the new imports are runtime (factories are called; `Workspace` is light
   — stdlib + config/errors only). No `if TYPE_CHECKING:` block was added
   or edited, so no classification entry is owed.
3. **Brief's "conventions 34" is 33/34 until ORCH's re-point sweep.** Adding
   code necessarily shifts `path:line` cites (§6); the docs are outside my
   write-set, and contorting the code into line-neutral shapes (e.g. two
   globs on one tuple line) to preserve blank-avoidance was rejected as
   style damage that saves nothing (agent.py's +10..+23 shifts break cites
   regardless). NEED posted with the full old→new mapping.
4. **"LOCKDOWN matrix" expressed as deny-rules**, per the established
   precedent (`permissions.py:40-56` has no LOCKDOWN member; W1-IMPL-06
   tested it the same way). PLAN + ASK + BYPASS+deny cover the matrix.

## 13. Out-of-scope findings

1. **`tests/coder/test_runtime_tools.py:801-805` — DEFERRED-DEPENDENCY
   (NEED posted).** `test_read_only_tools_are_catalogue_members_and_not_mutating`
   asserts `set(tools) >= READ_ONLY_TOOLS` where `tools` is the 9-key
   `build_runtime_tools` catalogue. After the brief-literal extension
   (`READ_ONLY_TOOLS` = 11), that one assertion fails (the other two
   assertions in the same test still hold). Exact fix for the owner/ORCH:
   re-pin the membership check against `build_work_tools` (e.g. build keys
   ⊇ `READ_ONLY_TOOLS`) or scope it to the runtime reads; `PaaError`-free,
   test-only change. Retried once per standing order 18 (still red — it is
   deterministic, not flaky). I did not edit it (outside write-set).
2. **Docs `path:line` re-point — DEFERRED-DEPENDENCY (NEED posted).**
   Blank-citation failures: `docs/architecture/component-interactions.md:103`
   and `docs/modules/coder.md:38` and `docs/reviews/documentation-audit.md:64`
   and `docs/plans and audits/flexability-audit.md:31` (all `agent.py:105`),
   `docs/glossary.md:133` (`permissions.py:143`), `docs/modules/coder.md:60`
   (`permissions.py:172`), `docs/modules/coder.md:102`
   (`runtime_tools.py:223`). Old→new (new lines [VERIFIED] by grep at the
   final revision): `agent.py:105→115`, `permissions.py:143→144`,
   `permissions.py:172→173`; silent (lock-passing but semantically off)
   shifts needing the same sweep: `agent.py:124→134`, `agent.py:155→178`,
   `agent.py:174→197`, `agent.py:192→215`, `agent.py:214→237` (the
   `flexability-audit` range end). `runtime_tools.py:223` was already
   semantically stale pre-change ([VERIFIED `git show
   HEAD:src/paa/coder/runtime_tools.py:223` → `                ],` — a
   `_ListTasks.run` payload line, while the citing sentence claims
   submit/run); the re-point should target `_SubmitTask`
   (now at `runtime_tools.py:290`), not a mechanical +14. ORCH-owned; my
   write-set cannot touch `docs/`.
3. **`prompt_yes` docstring already included work tools is now fixed
   in-scope** (§5) — was it a FINDING? No: correcting it is part of wiring
   the tools it describes (same file). Listed here for completeness; not an
   out-of-scope item.

## 14. Known limitations, risks, rollback notes

- The collision raise in `build_work_tools` is exercised only in its
  no-collision direction (the 20-key build test proves no false positive).
  Triggering the raise would require a colliding factory — i.e. doctoring a
  dependency — so it is graded as an untested defensive branch, not a
  verified path. The branch itself is three lines of key-set logic.
- `tests/coder/test_new_integration.py`'s build tests construct a real
  `Runtime` (SQLite in tmp, subprocess sandbox, degraded embeddings as in
  the existing specialist tests). They do not execute any work tool — they
  prove the *merged catalogue*, not execution (execution stays each
  factory's own suite).
- On hosts without `python` in a system directory, six pre-existing
  sandbox tests fail (AGENTS.md host note) — not encountered here; my
  scoped runs show 7 pwsh-absent skips only.
- Rollback: revert the 8 files. The `journal` name stays reserved for N2.2
  to wire (removing it now would force a rename later); the `git_stage*`
  glob is the one line reverting re-opens MAJOR-3.

## 15. Where I am least confident

Lowest confidence: **whether ORCH will accept the `Workspace`-typed (not
`str`) build parameter** (§4). It is the boundary-cleanest shape, but if the
operator would rather pass a path string to `CoderAgent.build` directly,
the one-line change is to accept `Workspace | Path | str` and resolve inside
`build_work_tools` — at the cost of string parsing in the composition root.
No behaviour is at stake; both shapes are one boundary line apart.

## 16. Self-review checklist

- Diff re-read from scratch (all 8 files via `git status --short` +
  per-file diffs): only write-set files touched; no sibling work touched
  (`git status` shows COMMS modified (my own appends, uncommitted by me))
  plus pre-existing untracked briefs/plans/uv.lock and my two new files).
  Retried COMMS failure did not corrupt the log (two single-line appends,
  verified `ok1`/`ok2`).
- Self-review MAJOR (fixed pre-commit): first draft put the `journal` guard
  *after* `Runtime.build`, so passing a journal would raise with a built,
  unclosed runtime. A probe hung on the Windows file lock; root-caused to
  the guard placement, moved to the top of `build()` before any build, and
  re-verified (clean exit). [RAN before/after]
- Self-review MINOR (fixed pre-commit): `sandbox=runtime.sandbox or None`
  used truthiness on an `Any` — an exotic falsy `Sandbox` would be silently
  discarded. Fixed to the explicit `is not None` form; verified by the real
  build test (subprocess backend path exercised).
- Self-review MINOR (fixed pre-commit): `prompt_yes` docstring named only
  the runtime triple; updated to the nine mutating names (the docstring
  describes what reaches the prompt, which is now nine tools).
- Self-review MINOR (fixed pre-commit): collapsed a 3-line `parts.append`
  to one line; kept `__all__`/`__init__` ordering consistent with each
  file's existing pattern.
- Mutation mentalist: §7 enumerates each mechanism with the test that
  catches its removal; the probe mutation was witnessed red, not merely
  reasoned about. Conventions count (33/34) and runtime-tools (1 failure)
  are reported honestly as deferred, not hidden behind a re-run.
- No `any`-widening, no `noqa`/`type: ignore`, no sleeps/retries, no test
  weakening (the specialist pin was *strengthened* 3→9, not loosened), no
  sibling files edited. Explicit-path commits only; branch
  `ops/20261109-next-spec` throughout ([VERIFIED `git
  branch --show-current` at start]).

## 17. Commits

Code commit: `0d02ea8` (8 write-set files; explicit paths). Process defect,
disclosed not hidden: the message has a typo ("gluc" for "glob") and omits
the Task/Agent/Wave/Report trailers; `--amend` is forbidden, so the message
stands and this note is the record.
Report commit: PENDING (this file, docs-only; lists the code SHA).
