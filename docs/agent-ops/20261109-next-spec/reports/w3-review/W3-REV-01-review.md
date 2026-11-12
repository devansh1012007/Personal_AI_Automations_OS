<!-- ORCH-DIGEST:START -->
agent: W3-REV-01 | scope: Wave 3 S1 / T-N3-1 (commit 2d7f9f6) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 1 | NIT 4
claims_audit: 14 claims checked / 1 unsupported-or-false (the 50/50 count; substance 47/47 holds)
band_aids_found: none
top_3_risks: (1) the 50/50 count is 47 on this host — evidence-citation hygiene, not harness truth; (2) no conventions lock pins the fixture outside testpaths, so a future pyproject change silently pulls the red test into CI; (3) real shell execution + real-model rung remain unproven on any host until the Linux rung runs
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 2
reversal_check: done
confidence: high (fixture, patch, dry-run, git-diff, lint/conventions all re-run by me; only the Linux real-model rung and the N2.3 chaos suite were not re-run — both honestly labelled NOT RUN in the artifact)
<!-- ORCH-DIGEST:END -->
# Code review — W3-REV-01 (T-N3-1, commit 2d7f9f6)

## 0. Method

Blind pass first: read `examples/fixtures/buggy_calc/{calc.py,test_calc.py,fix.patch,README.md,.gitignore}`,
`scripts/drill_kill_resume.py` (full, 572 lines), and
`docs/operations/2026-11-09-next-spec/s1-report.md` *before* opening
`docs/agent-ops/20261109-next-spec/reports/w3-impl/W3-IMPL-01-N3-1.md`.
Then re-ran every runnable claim myself, checked `git diff 2d7f9f6^..2d7f9f6 --stat -- src tests`,
`pyproject.toml:91` (`testpaths = ["tests"]`), and the `MUTATING_RUNTIME_TOOLS` comment at
`src/paa/coder/agent.py:71-80` against the cold-lake behavior at
`src/paa/coder/runtime_tools.py:466`. Spec gate: `docs/2026-11-09-next-spec.md` §3 (S1) and plan
`docs/plans/2026-11-09-next-spec-revised.md` §E N3.1 / §H T-N3-1. Read-only on source and fixture;
only this report was written.

## 1. Blind-pass findings

**What the artifact is.** Three things, correctly scoped: (a) a one-line-bug fixture
(`calc.py:35` floor division `a // b` vs docstring promise of float; `test_calc.py:31-33` exactly
one failing test), (b) a 572-line harness that drives a real `CoderAgent`/`Runtime`/ledger/git
repo with a scripted tool-calling model and asserts the S1 evidence, (c) an S1 report split into
a dry-run rung (done) and a real-model rung explicitly NOT RUN. No `src/` or `tests/` touched —
verified by diff, not by trust.

**Strengths worth naming.** The harness threads one `correlation_id` through mandate + journal
(`drill_kill_resume.py:297-325`) instead of the CLI's current fresh-id-per-build shape, and says
so in the module docstring (`:30-36`) — a real FINDING carried honestly, not hidden. Isolation
assertions bind to *actual* levels (`PROCESS` for `bash`, `NONE` for in-process tools,
`:404-417`), never requested ones, per ADR-0045/0046. The sandbox-exec split is disclosed in
three places (docstring `:38-44`, INFO line `:473-477`, s1-report `:98-103`) rather than buried:
on a pwsh-absent Windows host `bash` children execute 0/2 and the green suite is verified by a
host subprocess (`:490-491`). The `--iterations` kill-drill half is a skeleton that exits 2 with
a NOT-RUN note (`:558-566`) instead of pretending — correct, since N3.2 owns the driver.

**First defect found blind (before any run): the headline count.** Both the s1-report (`:37`,
"exit 0, 50/50 checks held") and the W3-IMPL-01 report ("50 assertions, exit 0") claim 50. My
run prints **47** `[PASS]` lines, 0 `[FAIL]`, exit 0. Counting the `check()` sites: 34 call
sites, 6 of which sit inside a loop over the 6 PROPOSED rows × 3 assertions = 18, plus the
remainder = 47. The implementer appears to have counted call *sites* loosely (or counted 3
interaction groups as 6). Substance is unaffected — everything asserted held — but an evidence
file quoting "50/50" when the instrument prints 47 is exactly the off-by-hunger that makes a
reviewer distrust the rest of the numbers. See §4 MINOR-1.

**Blind-pass nits.** Fixture-drift detection uses bare `assert` (`:232-234`) — drift gives a
traceback, not a `[FAIL]` line; acceptable for a script, worth a line. `ScriptedModel.calls`
(`:206`) is incremented but never asserted — the "six tool calls" guarantee comes from
`result.tool_calls == 6` (`:332-336`) instead, which is fine, but the counter is dead weight.
The script requires the dev venv on `PYTHONPATH` (bare `python` fails with `ModuleNotFoundError:
No module named 'paa'`); the usage line names the venv, so this is a documentation fact, not a
bug — but a bare-`python` reader will trip on it.

## 2. Claims audit

| # | Claim (source) | Verdict |
|---|---|---|
| 1 | Fixture pre-run is 1 failed / 4 passed, exactly `test_divide_returns_fraction` (s1-report:27-31; W3-IMPL-01 §3) | **holds** [RAN `python -m pytest test_calc.py -q` in fixture dir -> `1 failed, 4 passed`, `FAILED test_calc.py::test_divide_returns_fraction`] |
| 2 | `fix.patch` applies to a pristine copy and turns it green (s1-report:94-96; fixture README) | **holds** [RAN: copied fixture to temp dir, `git apply fix.patch` rc 0, `pytest test_calc.py -q` -> `5 passed`] |
| 3 | Dry-run exits 0 with every check held (s1-report:37-39) | **holds in substance** [RAN `.venv/Scripts/python.exe scripts/drill_kill_resume.py --dry-run` -> exit 0, **47 [PASS], 0 [FAIL]**, `dry-run: all checks held`] — the *number* 50 does not hold (MINOR-1) |
| 4 | 13 ledger events, head `MANDATE_GRANTED`, basis `explicit`, assurance `asserted`, sha256 `policy_version` (s1-report:56-63) | **holds** [DRILL output ledger listing v1-v13 matches verbatim; PASS lines for each] |
| 5 | Hash chain verifies (s1-report:66-67) | **holds** [DRILL `[PASS] hash chain verifies — []`] |
| 6 | Per-tool PROPOSED+COMPLETED counts `read_file×1 bash×2 edit_file×1 git_stage×1 git_commit×1` (s1-report:68-70) | **holds** [DRILL PASS with identical dict] |
| 7 | Isolation is honest: `bash`=PROCESS, others=NONE (s1-report:74-77) | **holds** [DRILL: every bash row PROCESS, read_file NONE, edit/git NONE; `sandbox.backend_selected backend=subprocess isolation=PROCESS`] |
| 8 | Edit intent hashes buggy→fixed bytes (s1-report:78-80) | **holds** [DRILL PASS `pre_sha256 hashes the buggy file` / `post_sha256 hashes the fixed file`] |
| 9 | ASK prompts mutating-only, `read_file` never prompted (s1-report:82-86) | **holds** [DRILL `['bash','edit_file','bash','git_stage','git_commit']` + `read_file never prompted`] |
| 10 | Post-run green, byte-exact fix, HEAD = agent commit, clean tree (s1-report:88-93) | **holds** [DRILL PASS ×4, commit subject identical to `COMMIT_MESSAGE`] |
| 11 | Real-model rung NOT RUN — requires Linux/WSL2 + `OPENROUTER_API_KEY`, absent here; no invented run (s1-report:14-16, 112-132) | **holds, honest** [VERIFIED: report labels `NOT RUN` in prose, status table, and reproduction commands; no cost/ledger/model-behavior claims made for it; W3-IMPL-01 §4 agrees] |
| 12 | `pwsh` absent on this host; `bash` spawn failure disclosed, journal still held (s1-report:98-103; drill `:38-44`) | **holds** [DRILL `[INFO] sandbox bash children that actually executed: 0/2 (host shell: pwsh=absent)`; brief's "expect 50/50" is the *count* expectation, not execution] |
| 13 | `MUTATING_RUNTIME_TOOLS` comment stale: says `remember_fact` writes hot-serving tables, but Wave 2 uses cold-lake signal path; behavior right (s1-report:105-110) | **holds** [VERIFIED `src/paa/coder/agent.py:71-73` ("writes rows into the memory tables (`hot_serving_entity_index` / `hot_serving_active_facts`)") vs `src/paa/coder/runtime_tools.py:466` ("``cold_lake_signals`` row first — the same front door every other channel")]. Comment is stale as claimed; fix is ORCH-owned, comment-only] |
| 14 | Fixture outside repo suite; `testpaths = ["tests"]` (s1-report:23-25; calc.py:9-12; README) | **holds** [VERIFIED `pyproject.toml:91`; RAN `pytest tests/ --collect-only` -> 0 hits for `buggy_calc`; fixture `.gitignore` keeps `__pycache__/*.pyc` out of the demo commit] |
| 15 | No `src`/`tests` touched by T-N3-1 (claim 6 in brief) | **holds** [RAN `git diff 2d7f9f6^..2d7f9f6 --stat -- src tests` -> empty; the 45-file `b8570a6..HEAD` src/tests diff is all Wave 1/2 work; T-N3-1 commit touches 9 files: fixture, script, s1-report, W3-IMPL-01 report, work-ledger] |
| 16 | `conventions` green / `ruff` clean (W3-IMPL-01 §3) | **holds** [RAN `pytest tests/conventions` -> **34 passed** in ~152s; RAN `ruff check src tests` -> `All checks passed!`] |
| 17 | Kill-resume / unknown-outcome halves proven by `tests/coder/test_recovery_chaos.py` (s1-report:134-141) | **NOT VERIFIED by me** — cited as a separate rung, suite not re-run in this review (scoped-check rule). The claim structure is honest (it does not attribute this to the dry-run), but the green-chaos half of the S1 exit remains implementer-asserted. Flagged in §9. |

## 3. Long-run test (independent answers)

- **Source.** The artifact changes no control flow in `src/`; it adds a fixture, a harness
  script, and docs. Single source of truth for the bug is `calc.py:35`; the patch and the
  harness's byte-oracles both derive from it without a second copy (harness recomputes
  `fixed_bytes` from the shipped bytes + line substitution, `:231-234`, then cross-checks
  against `fix.patch` on a pristine copy). This is the correct topology: fixture owns the bug,
  patch owns the fix, harness owns the assertions.
- **Load (100×, concurrency, partial failure).** The dry-run is single-shot, deterministic, and
  throwaway (`tempfile.mkdtemp`, `shutil.rmtree` in `finally` + except path, `:226-228,516-527`);
  running it N times leaves no residue in the repo. Partial failure is handled: a failed check
  accumulates into `failures` and returns 1 (`:529-535`), an unexpected harness crash cleans up
  then re-raises (`:521-527`) so the traceback survives. What is *not* load-tested: the
  kill-at-random-call half — by design (N3.2 skeleton, exit 2, `:46-55`). That is an honest
  boundary, but it means the "kill -9 loses/repeats nothing" S1 exit item is carried entirely
  by the N2.3 chaos suite, which I did not re-run (§2 claim 17).
- **Hostile input.** The harness never lets the scripted model choose paths or commands freely:
  the 6 turns are fixed literals (`:267-288`); workspace is `resolve_workspace(str(ws))` on a
  `mkdtemp` dir; git goes through fixed argv helpers (`:153-174`) with repo-local identity
  (`:253-255`) so commits never depend on host global config. The fixture patch is a fixed
  one-line diff with `a/calc.py`/`b/calc.py` paths — `git apply` on a pristine copy was verified
  to land exactly where claimed. No injection surface: the only free text is the prompt string
  `"fix the failing test"`.
- **Operability.** `--keep` (`:551-555`) retains the throwaway workspace + `PAA_HOME` for
  inspection on failure; `root` is printed. The INFO line (`:473-477`) names the host shell
  presence so a future "0/N executed" line is interpretable without this report. Rate/cost: zero
  model spend (scripted), ~seconds of host CPU per run. Repro requires the dev venv for `paa`
  import (bare `python` fails — see NIT-2), which the usage block states.
- **Reversal.** If `fix.patch` were reverted (or the fixture "fixed" in place), which check
  fails? `pre-run suite is red` (`:242`), `exactly the divide test fails` (`:243-247`),
  `fixture carries the buggy line` (`:235`), and `patch applies + pristine green` would all fail
  — the harness is genuinely pinned to the buggy state, not vacuously passing. Likewise if the
  isolation attestation regressed to requested-not-actual levels, `bash rows attest actual
  PROCESS isolation` would fail on any host where the level differs. The fixture's red test is
  load-bearing; no test in this artifact is decorative.

## 4. Findings

- **MINOR-1 — Evidence count overstated (50 vs 47).** `docs/operations/2026-11-09-next-spec/s1-report.md:37`
  and `W3-IMPL-01-N3-1.md:53` claim "50/50". Independent re-run prints 47 `[PASS]` / 0 `[FAIL]` /
  exit 0. All checks held; the harness is honest, the arithmetic is not. This matters because the
  s1-report's entire value is that its numbers are checkable. **Fix:** ORCH edits s1-report to
  "47/47 checks held" (or the implementer recounts with the loop expanded) and notes the
  correction. One line; must land before the S1 exit is quoted.
- **NIT-1 — Fixture drift crashes instead of failing.** `scripts/drill_kill_resume.py:232-234`
  uses bare `assert` for "buggy line present / fix is non-trivial". Drift yields a traceback +
  non-zero exit (still detectable) but outside the `check()`/`failures` accounting. Convert to
  `check(...)` with the same labels, or document the split. No behavior change, same verdicts.
- **NIT-2 — Bare `python` invocation fails.** `python scripts/drill_kill_resume.py --dry-run`
  dies at `:79` (`ModuleNotFoundError: No module named 'paa'`) because the script assumes the
  dev venv on `PYTHONPATH`. The usage block (`:59`) does show the venv path, so this is a
  reader-trap, not a bug. A one-line "run with the dev venv" note at the top of the fixture
  README would save the next operator a cycle.
- **NIT-3 — Dead counter.** `ScriptedModel.calls` (`:206-211`) is incremented but never read;
  `result.tool_calls == 6` carries the guarantee. Either assert `model.calls == 7` (6 turns + 1
  finish) or drop the counter. Cosmetic; either is fine.
- **NIT-4 — No lock pins the fixture outside the suite.** The isolation of the red test rests
  solely on `pyproject.toml:91` (`testpaths = ["tests"]`). Nothing in `tests/conventions`
  asserts that `examples/fixtures/buggy_calc/test_calc.py` is never collected. A future
  `testpaths` edit that adds `examples` would silently make CI red for a test that is *supposed*
  to fail. DO-LATER: a 5-line conventions test asserting collection of `tests/` matches no
  fixture file. (Also explains why `git status` in the fixture dir shows `__pycache__` noise —
  handled by the fixture `.gitignore`, fine.)

Tally: BLOCKER 0, MAJOR 0, MINOR 1, NIT 4. Nothing here blocks the S1 dry-run rung; MINOR-1 is
a documentation-accuracy fix owed before the exit is quoted.

## 5. Attempts to break it

All attempts were run, not reasoned:

1. Ran the fixture suite in place: `python -m pytest test_calc.py -q` -> 1 failed / 4 passed,
   the failure exactly `test_divide_returns_fraction` (`0 == 0.5`). A fixture that is secretly
   green or multi-red cannot produce this; it is not.
2. Copied the fixture to a temp dir, `git apply fix.patch` (rc 0), re-ran: 5 passed. Then
   *also* ran the harness's own pristine-copy check (same result in the drill output). The
   recorded fix reproduces outside the agent's workspace, twice, by two paths.
3. Ran the full dry-run three times (two via `.venv/Scripts/python.exe`, once counting lines):
   every run exit 0, 47 PASS / 0 FAIL, identical 13-event ledger (v1 `MANDATE_GRANTED` … v13
   `TOOL_CALL_COMPLETED git_commit`). Deterministic across runs on this host.
4. Checked the "pwsh absent" claim is not hiding a skip: the drill's PASS for the post-run
   green suite comes from a *host* subprocess (`:490-491`), and the INFO line proves the
   sandbox children did not run (0/2) — the harness declares this gap rather than laundering it
   into a shell PASS. A dishonest harness would have asserted shell execution; this one asserts
   the opposite and still passes.
5. Verified isolation honesty in the printed ledger: `bash` rows say `PROCESS` while children
   executed 0/2 — correct, because `PROCESS` attests the *backend class*, and the spawn-failure
   observation is separately journalled as the tool result. No requested-level forgery.
6. Tried to break the boundary: `git diff 2d7f9f6^..2d7f9f6 --stat -- src tests` -> empty. No
   Wave 1/2 file was smuggled into the T-N3-1 commit. `pytest tests/ --collect-only` -> 0
   `buggy_calc` hits. The red test cannot redden the suite.
7. Tried the bare-`python` path: fails fast at import (`ModuleNotFoundError`) — loud, not silent
   (NIT-2). No accidental partial run.

Could not break: the harness. It is deterministic, self-cleaning, and its honesty
mechanisms (INFO line, exit-2 skeleton, NOT-RUN rung) all fired as designed on this host.

## 6. Dimension checklist

- **Correctness.** Fixture, patch, harness assertions, and report all agree where verified.
  One arithmetic error (MINOR-1). No logic defect found in the harness.
- **Security.** No new trust surface: fixed-argv git, jailed workspace (reused Wave 1
  machinery), repo-local git identity, no secrets in fixture/script/report, no network. The
  fixture README's real-model reproduction correctly uses a throwaway `PAA_HOME` and env-var
  key, never values in files.
- **Error handling.** Check failures accumulate and exit 1; harness crashes clean up and
  re-raise; `--iterations` refuses loudly (exit 2) instead of faking a drill. Bare asserts
  (NIT-1) are the only rough edge.
- **Resource use.** Temp dirs reclaimed; 120s subprocess timeouts on all host-side runs
  (`:143-150,163-171,182-191`); no unbounded loops. Deterministic, no sleeps.
- **Test quality.** The fixture test is behavior-focused (`divide(1,2) == 0.5`, `divide(7,2) ==
  3.5`, zero-division raises) and fails *only* on the planted bug. The harness is not a repo
  test (by design — outside `testpaths`), so no coverage gate is bypassed; conventions + ruff
  re-verified green (34 passed / clean).
- **Docs.** Fixture README reproduces on a fresh checkout (verified patch path); s1-report
  separates rungs honestly; W3-IMPL-01's out-of-scope cold-lake finding matches the code I
  read. The 50/50 line contradicts the instrument (MINOR-1). `docs/agent-ops/` is excluded
  from the `path:line` lock, and the conventions run (34 passed) confirms no new citation broke.
- **Operability.** `--keep` for post-mortem, INFO shell line, zero spend. The real-model rung
  is reproducible from the README alone.

## 7. Optimization and simplification opportunities

- **DO-NOW 0.** Nothing here is slow, complex-for-complexity, or reducible without losing
  evidence value. The script is 572 lines but each check maps to a distinct S1 exit clause;
  collapsing them would destroy the audit granularity that is the point of the artifact.
- **DO-LATER 1 (NIT-4): conventions lock for fixture isolation.** ~5 lines, prevents a future
  silent-CI-red class of accident. Belongs with whoever owns `pyproject.toml` next.
- **DO-LATER 2 (NIT-1):** convert the two bare asserts to `check()`. Trivial, makes drift a
  verdict instead of a traceback.
- **NOT-WORTH-IT 1:** a repo pytest wrapper around the dry-run. The script *is* the test
  harness for S1; wrapping it in pytest adds a runner without adding a signal, and would drag
  the fixture's red test toward the suite it must stay out of.
- **NOT-WORTH-IT 2:** parameterizing the scripted turns to "wander" or fuzz the model. The
  scripted model's job is to make the *harness* deterministic so the ledger evidence is
  reproducible; model nondeterminism is what the Linux real-model rung is for.

## 8. Cross-agent interactions

- **T-N3-1 commit scope:** clean. The 45-file `src`/`tests` diff against `b8570a6` is entirely
  Wave 1/2 (work tools, journal, mandate, recovery, blobbing, plus their tests). The T-N3-1
  diff alone adds only the fixture, the script, the s1-report, W3-IMPL-01, and a work-ledger
  touch. No conflict with parallel Wave 3 agents expected.
- **W3-IMPL-01 out-of-scope finding (stale comment) → ORCH:** confirmed real (§2 claim 13).
  ORCH should schedule the one-line comment fix in `src/paa/coder/agent.py:71-73`; it is
  comment-only and must not be bundled into any behavioral change.
- **N3.2 handoff:** the `--iterations` skeleton (`:46-55`) already names the design (subprocess
  driver, kill at random call, `--resume`, shadow-tree oracle, 20× Linux / 5× Windows) and
  defers to `tests/coder/test_recovery_chaos.py` for the classification half. The s1-report's
  status table (`:147-150`) correctly leaves kill-resume + unknown-outcome rows as
  "N2.3 suite green (separate rung)" / "NOT RUN (N3.2)" — no over-claim to collide with N3.2.
- No COMMS conflict observed.

## 9. What I could not verify

- **The Linux real-model rung** (cannot run here: no WSL2/Linux, no `OPENROUTER_API_KEY`).
  I verified only that the artifact *claims* NOT RUN everywhere it should and provides
  reproductions consistent with the fixture README. Whether a frontier model actually fixes
  the fixture under `--workspace` --mode ask is unproven by this artifact — and the artifact
  says so.
- **Real shell execution** through the sandbox on this host (`pwsh` absent; children 0/2).
  The harness proves journal-before-side-effect + isolation attestation without real shell
  runs; real shell runs are a Linux-rung property.
- **The N2.3 chaos suite** (`tests/coder/test_recovery_chaos.py`) that carries the
  kill-resume S1 exit items. Not re-run (out of scoped scope). The s1-report's reliance on it
  is explicit and honestly labelled as a separate rung; treat "kill -9 + --resume green" as
  implementer-asserted, not review-verified.
- **mypy**: not re-run (brief required `ruff` + `conventions`; W3-IMPL-01 reports 3
  pre-existing errors at `subprocess_backend.py:548`, untouched by this commit — consistent
  with the empty src/tests diff, so the baseline cannot have moved due to T-N3-1).
