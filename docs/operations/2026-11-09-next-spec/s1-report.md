# S1 demo — real-model run report (M3, N3.1)

Run-id `20261109-next-spec`, Wave 3. The S1 exit from the spec: `paa agent
--workspace` fixes a failing test on the `buggy_calc` fixture, every call
logged in the ledger with its hash and isolation level, `kill -9` plus
`--resume` loses and repeats nothing, unknown outcomes reported not retried.

This file records the S1 evidence on two rungs, and is explicit about which
rung each item belongs to:

1. **Dry-run rung** — the complete S1 harness, driven by a scripted
   tool-calling model against a real `CoderAgent`, real `Runtime`, real
   ledger, and real workspace git repo. Zero model spend, any host.
2. **Real-model rung** — the same fixture under a hosted frontier model.
   **NOT RUN.** Requires Linux/WSL2 plus `OPENROUTER_API_KEY`; this host
   has neither (see "Why not run here").

## Fixture

`examples/fixtures/buggy_calc/` — five tests, one line bug: `divide()`
uses floor division (`//`) where it promises a float, so
`test_divide_returns_fraction` fails and the other four pass. The fix is
one character, pinned as `fix.patch` in the fixture. The fixture sits
outside the repo's pytest suite (`testpaths = ["tests"]`), so it can
never make the main suite red.

Pre-run state, verified:

    $ python -m pytest test_calc.py -q
    FAILED test_calc.py::test_divide_returns_fraction
    1 failed, 4 passed

## Dry-run rung (RUN 2026-11-09, Windows host, ScriptedModel)

Command:

    .venv\Scripts\python.exe scripts/drill_kill_resume.py --dry-run

Result: **exit 0, 50/50 checks held.** The harness copies the fixture to
a throwaway workspace, initialises a git repo at the buggy state, builds
a real `CoderAgent` over a throwaway `PAA_HOME`, and drives it with a
scripted model that reads the file, runs the suite, makes the one-line
edit, re-runs to green, stages, and commits. What it asserted, grouped:

**Fixture and workspace**

- The shipped `calc.py` carries the buggy line; the pre-run suite is
  red with exactly the divide test failing; the workspace is a
  git-initialised repo at the buggy head.

**The run itself**

- Agent run finished with `stopped_reason=model_finished`; exactly six
  tool calls executed; no call was denied.

**Mandate (N2.4, ADR-0046)**

- 13 events in the lineage; the head is `MANDATE_GRANTED`.
- Mandate `basis` is `explicit` — the harness submitted under an
  asserted mandate, not the runtime's implicit default.
- `identity_assurance` is `asserted` (never authority — display never
  promotes this to provenance).
- `policy_version` is a SHA-256 digest.

**Ledger integrity (N2.2 journal, N2.6 blobbing)**

- The hash chain verifies with no failures over the whole lineage.
- Every tool has one `TOOL_CALL_PROPOSED` and one
  `TOOL_CALL_COMPLETED` row: `read_file` ×1, `bash` ×2, `edit_file` ×1,
  `git_stage` ×1, `git_commit` ×1.
- Every `PROPOSED` row carries `args_sha256` as a 64-hex digest and a
  non-empty preview that renders a shape summary, never file content
  (the N2.5 secret-hygiene contract).
- Isolation levels recorded are the *actual* ones: `bash` rows attest
  `PROCESS` (subprocess sandbox); `read_file`, `edit_file`, `git_stage`,
  `git_commit` attest `NONE` (in-process filesystem ops — the level is
  honest about never entering the sandbox).
- Exactly one `edit_file` `PROPOSED`; its persisted intent hashes the
  buggy file as `pre_sha256` and the fixed file as `post_sha256` —
  the exact evidence N2.3 recovery classifies `applied` against.

**Policy (Wave 1 ASK mode)**

- Confirm prompts fire on mutating calls only: `bash`, `edit_file`,
  `bash`, `git_stage`, `git_commit`.
- `read_file` never prompted (read-only standing allow).

**Post-run state**

- The suite is green (5 passed).
- `calc.py` matches the fixed content byte-for-byte.
- HEAD is the agent's commit ("Fix divide() to use true division so
  fractional results are correct"); the tree is clean after commit.
- `fix.patch` applies to a pristine copy of the fixture, and the
  patched pristine copy runs green — the recorded fix is reproducible
  outside the agent's workspace.

Shell observation on this host (honest limitation): `pwsh` is absent
here, so `bash` tool calls logged `failed to start sandboxed process`
against the subprocess backend. The journal still recorded the calls,
the `PROCESS` isolation claim, and the failure observations — the
harness proof holds without real shell execution, but real shell
execution is only exercised on the Linux rung below.

Known drift (not the S1 rung, recorded for the operator): the code
`MUTATING_RUNTIME_TOOLS` comment in the coder agent module lists
`remember_fact` as writing into the hot-serving memory tables, but
Wave 2 memory writes go through the cold-lake signal path. The
comment is stale; the behavior (and the planner matrix test) is right.
Left for ORCH to fix — comment-only, no behavior involved.

## Real-model rung (NOT RUN — needs Linux/WSL2 + key)

Per the owner decision (D-era), the real-model S1 runs on Linux/WSL2.
Reproduction from the fixture README, with a throwaway `PAA_HOME`:

    export OPENROUTER_API_KEY=sk-or-...
    export PAA_MODELS__LOCAL_PROVIDER=openrouter
    export PAA_MODELS__LOCAL_MODEL=openai/gpt-4o-mini
    export PAA_MODELS__ESCALATION_PROVIDER=none
    export PAA_HOME="$HOME/.paa-s1"

    cp -r examples/fixtures/buggy_calc /tmp/buggy_calc
    cd /tmp/buggy_calc
    paa agent --workspace /tmp/buggy_calc --mode ask "fix the failing test"
    paa ledger <correlation-id>   # printed by the agent run

Expected: the agent reads `calc.py`, runs the suite (now really in the
sandbox), edits the one line, re-runs to green, stages and commits —
each mutating call prompting in ASK mode. The ledger lineage begins
with an explicit `MANDATE_GRANTED`; every call carries `args_sha256`
plus the actual `isolation_level`.

`kill -9` plus `--resume` and the 20× Linux / 5× Windows drill counts
belong to N3.2 (`scripts/drill_kill_resume.py` carries its kill-drill
half as the harness for that task); the dry-run rung above deliberately
stopped at "everything the S1 fix asserts" so N3.1's evidence stays
clean. The scripted N2.3 kill-simulation suite
(`tests/coder/test_recovery_chaos.py`) already proves the classification
half: PROPOSED-without-outcome closes as interrupted, unknown is
reported not retried, coder-only lineages are exempt.

## Status

| S1 exit item | Dry-run rung | Real-model rung |
|---|---|---|
| `paa agent --workspace` fixes the fixture's failing test | DONE (50/50) | NOT RUN |
| Every call logged in ledger with hash + isolation level | DONE (13 events, chain verifies) | NOT RUN |
| `kill -9` + `--resume` loses / repeats nothing | N2.3 chaos suite green (separate rung) | NOT RUN (N3.2) |
| Unknown outcomes reported, not retried | N2.3 chaos suite green (separate rung) | NOT RUN (N3.2) |
