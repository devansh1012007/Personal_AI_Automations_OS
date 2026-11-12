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
   **RUN 2026-11-10 on Linux/WSL2 with the operator's real
   `OPENROUTER_API_KEY`; the first live attempt under the free frontier
   model (`nvidia/nemotron-3-ultra-550b:a55b:free`) **SUCCEEDED** (fix
   applied, suite green, ledger chain ok). Later live attempts under paid
   models were **BLOCKED by the provider** (HTTP 403, key limit exceeded).
   See "Real-model rung — RUN 2026-11-10".

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

## Dry-run rung — Linux/WSL2 real bash (RUN 2026-11-10)

Command (WSL2 Ubuntu 22.04, Python 3.12 venv, dev extra):

    python scripts/drill_kill_resume.py --dry-run

Result: **exit 0, 47/47 checks held**, with real `bash` execution
through the subprocess sandbox
(`[INFO] sandbox bash children that actually executed: 2/2
(host shell: bash=found: /usr/bin/bash)`).

The harness copies the fixture to a throwaway workspace, initialises a
git repo at the buggy state, builds a real `CoderAgent` over a throwaway
`PAA_HOME`, and drives it with a scripted model that reads the file, runs
the suite, makes the one-line edit, re-runs to green, stages, and commits.
What it asserted, grouped:

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

Shell observation on this host (honest limitation): real `bash`
execution through the subprocess sandbox was confirmed on the Linux
rung (`bash=found: /usr/bin/bash`); the earlier Windows-host degraded
rung (pwsh absent) is superseded.

Known drift (not the S1 rung, recorded for the operator): the code
`MUTATING_RUNTIME_TOOLS` comment in the coder agent module lists
`remember_fact` as writing into the hot-serving memory tables, but
Wave 2 memory writes go through the cold-lake signal path. The
comment is stale; the behavior (and the planner matrix test) is right.
Left for ORCH to fix — comment-only, no behavior involved.

## Kill drill — Linux/WSL2 POSIX `kill -9` (NOT RUN — Windows subset executed)

Command:

    python scripts/drill_kill_resume.py --iterations 20

Result: **NOT-RUN on Linux/WSL2** — the Linux/WSL2 POSIX `kill -9` kill drill
has not been executed on a Linux host; the Linux-gated 20-iteration matrix
is NOT-RUN. Verified on this host: the Windows TerminateProcess subset ran
`--iterations 5` → 5/5 passed, `--seed 14 --iterations 6` → 6/6 passed.
Each iteration launches a real subprocess, sends `kill -9` at a randomly
chosen tool call (bash/edit_file/git_stage/git_commit/read_file), restarts
with `--resume`, and verifies: zero duplicated committed effects, correct
tree, hash chain, recovery classification (bash/git → unknown by design,
D-6; edit → applied/not_applied), recovery note names the interrupted
call.

## Real-model rung — RUN 2026-11-10 (SUCCESS with the free frontier model; paid attempts BLOCKED by provider key limit)

### Live run under the free frontier model — SUCCESS

Command (WSL2 Ubuntu 22.04, Python 3.12 venv, dev extra; env taken from
the operator's `.env`):

    export OPENROUTER_API_KEY=sk-or-...
    export PAA_MODELS__LOCAL_PROVIDER=openrouter
    export PAA_MODELS__LOCAL_MODEL=nvidia/nemotron-3-ultra-550b:a55b:free
    export PAA_MODELS__ESCALATION_PROVIDER=none
    export PAA_HOME=/home/devansh/.paa-s1

    cp -r examples/fixtures/buggy_calc /tmp/buggy_calc
    cd /tmp/buggy_calc
    paa agent --workspace /tmp/buggy_calc --mode ask --yes "fix the failing test"
    paa ledger <correlation-id>   # printed by the agent run

Result: **SUCCESS** — the run exited 0 and produced a fix.

- Router resolved `openrouter / nvidia/nemotron-3-ultra-550b:a55b:free`
  (**tier FRONTIER**).
- Mandate lineage begins `MANDATE_GRANTED` (correlation
  `867f446c-f829-4bd4-9e44-d6441ceb96a4`, state_version 1); head verifies,
  `basis=explicit`, `policy=ASK`.
- Tool ledger (`paa ledger a7bf9e0f-307e-45b7-8c85-62b19b173249`) holds 12
  `TOOL_CALL_PROPOSED` + 12 `TOOL_CALL_COMPLETED` events with
  `chain integrity: ok`; the bash calls attest `PROCESS` (subprocess
  backend), in-process calls attest `NONE`.
- The single edit (`edit_file` on `calc.py`, `//` → `/`) was planned,
  validated via the patch dry-run, and applied — `validation.patch.applied files=1
  patch_sha256=53de650bf588`
  — and recorded with `pre_sha256`/`post_sha256` intent.
- Final `bash` re-ran the suite: **5 passed**.

Post-run state: fix present in `/tmp/buggy_calc/calc.py`
(`return a / b`); suite green (re-verified: 5 passed). The agent did not
stage or commit (no `git_stage`/`git_commit` calls were made in this live
run); the fix sits as an uncommitted working-tree change in the throwaway
workspace, alongside untracked `.cache/` and `.local/`.

### Live attempts under paid models — BLOCKED by provider key limit

Result: **BLOCKED.** Two live attempts, both at the provider call, both
exit 1. Neither reached a tool call, so no fix was attempted and the
workspace was left pristine.

- Attempt 1 (2026-11-10 ~12:48 UTC): `error: local provider failed and
  escalation is not permitted` → `• escalation blocked:
  modality_below_escalation_floor:COMPLEX` → `• escalation unavailable:
  no_frontier_configured` → `• provider failure: model endpoint returned
  HTTP 403 (ModelUnavailableError)` → `• provider status: 403` → provider
  body excerpt: `{"error":{"message":"Key limit exceeded (total limit). ...`
  → exit 1; session id + resume instructions printed; clean close.
- Attempt 2 (2026-11-10 ~12:49 UTC): identical failure (same 403, same
  key-limit body).

What the live path got right before the failure:

- Runtime built; sandbox auto-selected `subprocess` (`isolation=PROCESS`);
  relational sqlite; vector degraded to numpy (qdrant absent); graph
  sqlite.
- Model router resolved a frontier-tier model for the attempted task
  (tier FRONTIER).
- Mandate appended to the throwaway ledger before any model turn:
  `MANDATE_GRANTED` (correlation `9e4debb4-f231-47d5-afef-62784cb3ce2f`,
  state_version 1); `paa ledger <correlation>` then showed the head as
  `MANDATE_GRANTED` with `chain integrity: ok`.
- The CLI rendered the failure cleanly (structured error bullets, no
  traceback) and printed the session id plus `--resume` /
  `--continue` instructions.

Post-attempt state: `/tmp/buggy_calc` left pristine — clean tree at the
buggy head, `return a // b` present, suite red (1 failed, 4 passed). The
runs died at the model layer, before any tool call, so no side effect
was possible.

Blocked-by: the operator-wired OpenRouter key reports
`Key limit exceeded (total limit)` (`limit_source: openrouter_key_limit`,
remedy: raise or remove the limit at the OpenRouter key settings). The
escalation arm was also unavailable (`PAA_MODELS__ESCALATION_PROVIDER=none`;
the task's modality came back STANDARD, below the COMPLEX floor).

**Honest gap:** the ledger export and cost figures were not recorded in
this report (pending operator capture).

## Status

| S1 exit item | Dry-run rung | Kill drill rung | Real-model rung |
|---|---|---|---|
| `paa agent --workspace` fixes the fixture's failing test | DONE (47/47, real bash) | n/a | **DONE — SUCCESS with the free frontier model `nvidia/nemotron-3-ultra-550b:a55b:free` (tier FRONTIER); fix applied, suite green (5 passed), 12 tool calls, 0 denied, ledger chain ok** |
| Every call logged in ledger with hash + isolation level | DONE (13 events, chain verifies) | DONE (Windows subset 5/5 + 6/6; Linux/WSL2 POSIX 20× NOT-RUN, hash chains verify) | DONE — successful live run logged 12 TOOL_CALL_PROPOSED + 12 TOOL_CALL_COMPLETED rows (chain ok, isolation PROCESS/NONE); mandate MANDATE_GRANTED (basis=explicit) |
| `kill -9` + `--resume` loses / repeats nothing | N2.3 chaos suite green | DONE (Windows subset 5/5 + 6/6; Linux/WSL2 POSIX 20× NOT-RUN, zero duplicate effects) | n/a (kill drill is harness-driven, not live-model-driven) |
| Unknown outcomes reported, not retried | N2.3 chaos suite green | DONE (Windows subset 5/5 + 6/6; Linux/WSL2 POSIX 20× NOT-RUN, unknown by design) | n/a (live run produced no unknown outcome) |
