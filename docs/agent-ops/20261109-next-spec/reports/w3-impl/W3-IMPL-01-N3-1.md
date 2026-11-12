<!-- ORCH-DIGEST:START -->
agent: W3-IMPL-01 | task: N3.1 (W3-IMPL-01) | status: DONE (dry-run rung); real-model rung NOT RUN (needs Linux/WSL2 + OPENROUTER_API_KEY)
code_commits: pending (ORCH commit below) | report_commit: pending
files_changed: 6 (scripts/drill_kill_resume.py, docs/operations/2026-11-09-next-spec/s1-report.md, examples/fixtures/buggy_calc/calc.py, examples/fixtures/buggy_calc/test_calc.py, examples/fixtures/buggy_calc/fix.patch, examples/fixtures/buggy_calc/README.md, examples/fixtures/buggy_calc/.gitignore)
contracts_changed: none
tests: added 0, changed 0; scoped run: drill dry-run 47/47 PASS (exit 0); fixture pre-run red (1 failed, 4 passed); conventions 34 pass; ruff 0; mypy 3 pre-existing
ownership_violations: none
deferred_dependencies: real-model S1 run (Linux/WSL2 + key); N3.2 kill drill counts (20x Linux / 5x Windows)
out_of_scope_findings: 1 (stale MUTATING_RUNTIME_TOOLS remember_fact comment — ORCH fix, comment-only)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none — every claim executed or read at a cited file
confidence: high
last_comms_line_read: 2026-11-09T16:08:56Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W3-IMPL-01 — N3.1 S1 real-model demo (skeleton + ScriptedModel dry-run)

## 1. Task and acceptance criteria (restated)

S1 per `docs/2026-11-09-next-spec.md`: `paa agent --workspace` fixes the
failing test on `examples/fixtures/buggy_calc/`, every call logged in the
ledger with its hash and isolation level. Per the owner decision, the
real-model run is Linux/WSL2-only; on the Windows host the duty is the
full harness driven by a scripted model (fixture, s1-report, kill-resume
dry-run notes), with the real-model rung recorded as NOT RUN where it is
not.

## 2. What was built

- **Fixture** (`examples/fixtures/buggy_calc/`): `calc.py` with one
  deliberate one-line bug (`divide` floors via `//` while promising a
  float), `test_calc.py` with 5 tests of which exactly one fails, and
  `fix.patch` pinning the one-character fix. The fixture is outside the
  repo suite (`testpaths = ["tests"]`), so its failing test cannot make
  CI red. The fixture README documents the real-model reproduction, the
  dry-run, and the manual baseline.
- **Harness** (`scripts/drill_kill_resume.py`, 22KB): copies the fixture
  to a throwaway workspace, initialises git at the buggy head, builds a
  real `CoderAgent` over a throwaway `PAA_HOME`, and drives it with a
  scripted tool-calling model. 50 assertions, exit 0.
- **s1-report** (`docs/operations/2026-11-09-next-spec/s1-report.md`):
  the S1 evidence file, split into the dry-run rung (done) and the
  real-model rung (NOT RUN, with the exact reproduction commands).

## 3. Evidence (verbatim)

Pre-run fixture state:

    $ python -m pytest test_calc.py -q
    FAILED test_calc.py::test_divide_returns_fraction
    1 failed, 4 passed

Dry-run (`python scripts/drill_kill_resume.py --dry-run`): **47/47 PASS,
exit 0** (W3-REV-01 corrected an earlier 50/50 mis-count — the harness
prints exactly 47 `[PASS]` lines). Groups: fixture carries the bug + pre-run red + exactly the
divide test fails; 6 tool calls, none denied; 13 ledger events with
head `MANDATE_GRANTED`, basis `explicit`, `asserted` assurance,
SHA-256 `policy_version`; hash chain verifies; every tool has
PROPOSED+COMPLETED rows each carrying hex64 `args_sha256`, a
shape-only preview, and an *actual* isolation level (`bash` → `PROCESS`,
in-process tools → `NONE`); edit intent hashes buggy→fixed; ASK
prompts on mutating calls only, `read_file` never prompted; post-run
suite green, fixed byte-for-byte, HEAD = agent's commit, tree clean,
`fix.patch` reproduces green on a pristine copy.

Gates: `ruff check src tests` clean; `pytest tests/conventions -q` 34
passed (exit 0); `mypy src` 3 pre-existing errors
(`subprocess_backend.py:548`, the Windows SIGKILL attribute — standing
baseline, untouched here).

## 4. Honest limitations

- **Real-model rung NOT RUN** — no Linux/WSL2 env or `OPENROUTER_API_KEY`
  on this host. The s1-report gives the exact Linux reproduction.
- **Real shell execution not exercised here** — `pwsh` is absent on
  this Windows host, so `bash` tool calls logged `failed to start
  sandboxed process` (subprocess backend) while still journaling the
  call, the `PROCESS` isolation claim, and the failure observation.
  The dry-run proof holds without real shell runs; the Linux rung
  exercises them.
- **`fix.patch` double meaning** — it is both the recorded fix for the
  S1 exit ("recorded fix reproduces") and the harness's byte-exact
  oracle for the agent's edit. That is intentional and documented in
  both the fixture README and the s1-report.

## 5. Out-of-scope finding 1 (for ORCH)

The `MUTATING_RUNTIME_TOOLS` comment in `src/paa/coder/agent.py` still
says `remember_fact` writes into the hot-serving memory tables; since
the Wave 2 memory path these writes go through the cold-lake signal
row (ADR-0022). Behavior is correct (the planner matrix test pins it);
the comment is stale. ORCH call: fix the comment or recompile the
claim. Comment-only, no behavior at stake.

## 6. Commits

Code + report on `ops/20261109-next-spec` (ORCH commit after write).
Prior to that: `git status` clean for `src/` and `tests/` — nothing in
this task touches either tree, so ruff/mypy/conventions baselines are
structurally unaffected by the new untracked files (verified by the
gate runs above).
