<!-- ORCH-DIGEST:START -->
agent: W1-REV-05 | task: audit T-N1-5 (0d02ea8) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 2 | NIT 2
claims_audit: 9/9 primary claims verified; 2 impl-report caveats (collision-untested, conventions 33/34) now closed/overcome
redteam: PLAN git_stage DENY; ASK read no-prompt; collision PaaError (new probe); journal guard pre-build; probe-name escapes both nets
tests_ran: test_new_integration 13 passed; runtime_tools+specialist 23 passed; agent+permissions 40 passed; runtime_tools full 70 passed; conventions 34 passed; ruff 0; mypy scoped 0
top_risks: (1) build() docstring stale vs journal-raise; (2) policy-matrix tests use helper policy, not the real agent's — drift risk
confidence: high
<!-- ORCH-DIGEST:END -->
# Code review — W1-REV-05 (T-N1-5 registration audit)

Scope: commit `0d02ea8` (8 files) + ORCH follow-up `ded7cf4` (docs re-point + runtime_tools pin).
Blind pass first (diff + full `agent.py`/`permissions.py`/`runtime_tools.py`/`cli/coder.py` reads),
then impl report `W1-IMPL-07-N1-5.md` and W1-REV-03 MAJOR-3. Then re-ran suites and red-team probes myself.

## Verdict rationale

All nine brief verification items hold against the code and re-run tests. The two caveats the
impl report disclosed (collision branch untested; conventions 33/34 + 1 runtime_tools failure)
are both closed at current HEAD (`ded7cf4` re-point + re-pin; conventions now 34/34, runtime_tools
70/70 — re-run by me). My new collision probe closes the last unverified branch. Two MINORs and
two NITs are doc/test-fidelity items, none blocking. REV-03 BLOCKER-1 (pathspec staging) was fixed
in `1c3d0bf` (Wave 1b improve) *before* this wiring landed, so the newly-reachable `git_stage`
inherits the fixed jail — no latent exposure.

## 1. Blind-pass verification (per brief item)

| # | Claim | Verdict + evidence |
|---|---|---|
| 1 | `build_work_tools` merges without collision, raises `PaaError` on collision | TRUE. [VERIFIED agent.py:251-289] parts built in fixed order, `tools.keys() & part.keys()` checked before each `update`, `PaaError` names keys. [RAN probe: patched `build_git_tools`→`{'read_file':…}` → `PaaError: duplicate tool name(s)…['read_file']`] — the branch impl §14 called untested is now exercised. Real-build test proves no false positive (20 keys). |
| 2 | `Workspace\|None`, `None` ⇒ 9 tools | TRUE. [VERIFIED agent.py:219,251-277] `workspace=None` skips all four work factories; `test_build_without_workspace_offers_runtime_tools_only` pins the exact 9-name set. No CWD fallback. |
| 3 | `--workspace` resolves at CLI boundary | TRUE. [VERIFIED cli/coder.py:175-183] `resolve_workspace` → `Workspace(root=…)`; `PaaError` → `invalid workspace` + `Exit(1)`, before `CoderAgent.build`. [RAN `resolve_workspace('does-not-exist-zzz-123')` → `PaaError: workspace does not exist`] |
| 4 | `READ_ONLY` union 6→11 | TRUE. [VERIFIED runtime_tools.py:86-97] `{6} \| FS_READ_TOOLS \| GIT_READ_ONLY_TOOLS`. [RAN `len==11`] = 6 runtime + `read_file/glob/grep` + `git_status/git_diff`. `build_runtime_tools` still builds only 9 (test asserts `set(tools)>=READ_ONLY` re-pinned by `ded7cf4`; full file 70 passed). |
| 5 | `git_stage` matches `DEFAULT_MUTATING_TOOLS`, PLAN denies | TRUE. [VERIFIED permissions.py:137] `"git_stage*"`. [RAN default-policy probe: PLAN `git_stage`→DENY (`plan mode forbids mutating tools`), `git_status`→ALLOW; ASK+allow `read_file`→ALLOW (no prompt), `git_stage`→ASK] — REV-03 MAJOR-3 closed, and the red-team "PLAN-allow before fix" now denies. |
| 6 | Mutating-coverage test fails on probe mutation | TRUE (logic + impl witness + my probe). Test derives over `CODING_TOOL_NAMES` by glob-or-tuple then pins exact value [VERIFIED test_new_integration.py:100-113]. [RAN `probe_uncovered_mutator` escapes both nets → True, so the derived≠pinned assert fails; impl §7 witnessed 4 failures with the probe in `EXPECTED_MUTATING`] |
| 7 | Specialist pin 3→9 | TRUE. [VERIFIED test_coding_specialist_p011.py:175-188] exact 9-name mutating pin + `read_only == READ_ONLY_TOOLS` (11==11). [RAN file: passed] |
| 8 | Journal guard fails before build | TRUE. [VERIFIED agent.py:180-184] guard precedes `Runtime.build` (line 188) — no runtime/DB touched. [RAN `CoderAgent.build(None, journal=object())` → `NotImplementedError`] |
| 9 | `--list-tools` with workspace prints 20 | TRUE by code path. [VERIFIED cli/coder.py:185-207] build (with workspace) precedes `_print_tools(built.tools)`; without workspace, 9. Not CLI-executed by me (no model spend; path is linear). |

## 2. Claims audit (impl report)

9 material claims checked; all TRUE except two caveats now resolved:
- "Collision branch untested" (§14) → overcome: my probe raises `PaaError` as designed (§1#1).
- "Conventions 33/34, runtime_tools 1 failure, NEEDs posted" (§11/§13) → closed by `ded7cf4`:
  [RAN tests/conventions → 34 passed; tests/coder/test_runtime_tools.py → 70 passed]. No open NEED.
- "mypy 3 pre-existing" → not re-verified at full `mypy src` level (scoped-checks rule);
  [RAN mypy on the 5 touched sources → 0 issues].

## 3. Long-run test

- **Source:** factories unchanged; change is composition-only. `build_work_tools` is pure construction
  (no per-turn state); `Workspace` frozen so concurrent turns share it safely.
- **Load:** runs once per session. Descriptor-only aside: `build_fs_write_tools` creates one
  in-memory session journal per build (Wave-1 contract, impl §8) — unchanged.
- **Hostile input:** no new tool-argument surface. `--workspace` goes through `resolve_workspace`
  (deny-on-doubt) at the CLI boundary; tool args stay jailed inside each `run()` (frozen order).
  [RAN missing-path probe → `PaaError`].
- **Operability:** collision raise names keys (not silent overwrite); invalid workspace exits 1 with
  `invalid workspace <reason>`; `--list-tools` exposes the 9-vs-20 offered set.
- **Reversal:** revert `0d02ea8` (+`ded7cf4` test/docs) — no schema/ledger/config change. Reverting
  only the `git_stage*` line re-opens MAJOR-3 and fails `test_git_stage_matches_default_mutating_globs`.

## 4. Findings

### MINOR-1 — `build()` docstring contradicts the journal guard [agent.py:170-173]
Docstring: "the value is accepted and ignored." Code: non-None raises `NotImplementedError`
(agent.py:180-184 — the right behaviour, per impl §4/§12). A reader trusting the docstring will
believe journalling is silently accepted. Fix: reword to "accepted as a reserved slot; a non-None
value fails fast with `NotImplementedError` until N2.2 wires it."

### MINOR-2 — Policy-matrix tests assert against a copied policy, not the built agent's [test_new_integration.py:87-96]
`_policy()` re-spells `ToolPermissionPolicy(...)` with the claim "exactly as `CoderAgent.build`
constructs it." If `build`'s call (agent.py:224-236: `mutating_tools=(*DEFAULT_MUTATING_TOOLS,
*MUTATING_RUNTIME_TOOLS)`, `allow=[*READ_ONLY_TOOLS,…]`) ever drifts, the matrix tests stay green
while the agent's real policy changes. `TestBuildMerge` asserts the tools dict, not the loop's
policy. Fix (follow-up): one test that builds a PLAN agent with workspace, asserts
`agent.loop.permissions` (or equivalent) denies `git_stage` and allows `read_file`.

### NIT-1 — Redundant ternary [agent.py:275]
`sandbox = runtime.sandbox if runtime.sandbox is not None else None` — else-branch is `None`
anyway. Harmless; `sandbox=runtime.sandbox` alone is identical and cannot discard a falsy backend
(the `or None` trap the impl fixed in self-review is avoided either way).

### NIT-2 — `__init__.py` export ordering [__init__.py:85]
`"build_work_tools"` after `"build_runtime_tools"` — consistent with file convention. No action.

## 5. Red-team probes (all [RAN] by reviewer)

| Probe | Result |
|---|---|
| PLAN `git_stage` (pre-fix would ALLOW) | DENY — MAJOR-3 closed |
| PLAN `git_status` / ASK `read_file` | ALLOW, no prompt — reads never prompt |
| ASK `git_stage` | ASK (prompt) — correct mutating handling |
| Collision: git factory claims `read_file` | `PaaError` naming `['read_file']` — no silent overwrite |
| `journal=<obj>` | `NotImplementedError` before any build — no leak |
| `probe_uncovered_mutator` vs both nets | escapes → coverage test fails — lock is real |
| Missing `--workspace` path | `PaaError: workspace does not exist` — boundary refusal |
| REV-03 BLOCKER-1 exposure check | `1c3d0bf` predates wiring; `pathspec` handling present in `git_tools.py` — no latent staging escape via the new agent path |

## 6. Commands run (reviewer)

| Command | Result |
|---|---|
| `pytest tests/coder/test_new_integration.py -q` | 13 passed |
| `pytest tests/coder/test_runtime_tools.py::TestReadOnlyAndMutatingSets tests/agents/test_coding_specialist_p011.py -q` | 23 passed |
| `pytest tests/coder/test_runtime_tools.py -q` | 70 passed |
| `pytest tests/coder/test_agent.py tests/coder/test_permissions.py -q` | 40 passed |
| `pytest tests/conventions -q` | 34 passed |
| `ruff check` (8 touched files) | All checks passed |
| `mypy` (5 touched sources) | no issues in 5 files |
| Red-team probes (§5) | all hold |

Not run: full `mypy src`, full test suite (scoped-checks standing order); CLI `--list-tools`
end-to-end (verified by code path only, §1#9).
