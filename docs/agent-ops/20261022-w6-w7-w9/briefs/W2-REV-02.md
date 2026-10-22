# Brief — W2-REV-02 (independent test reviewer) · wave 2 · run 20261022-w6-w7-w9

## 1. Role

You are an **independent test reviewer**. You did not write these tests and
you are not the orchestrator. Your question is **not** "are the tests
passing" — they pass; the gate says so. Your question is:

> **Would each test fail if the fix it covers were reverted?**

A test that passes for the wrong reason is worse than no test, because it
discharges the obligation the test exists to discharge. Your job is to find
the ones that do.

You **do not edit code**. You report findings.

## 2. The tests under review

```bash
git diff --stat 4c9300d..HEAD -- tests/
git diff 4c9300d..HEAD -- tests/
```

The five suites the wave added or extended:

| Suite | Files | What it claims to cover |
|---|---|---|
| the validation suite | `tests/validation/{test_schema_validator,test_ast_scanner,test_patch,test_workspace,test_engine,test_test_runner}.py` (417 new tests) | the three headline claims of `src/paa/validation/`: unparseable source is rejected, `safe_relative_path` is correct, determinism is the contract — plus the malformed-keyword crash fix |
| the observability suite | `tests/observability/{test_metrics,test_runs,test_tracing,test_logging}.py` (50 → 136) | percentiles against **hand-computed** values, the run repository's round-trips, a failing exporter never breaking the traced operation, and `logging.py`'s five claims |
| the models suite | `tests/models/{test_router_lockdown,test_router_concurrency,test_key_secrecy}.py` (new) + `test_router_policy.py`, `test_escalation_audit.py` | the LOCKDOWN **spy** (zero frontier calls), the **semaphore**'s cap, the escalation ordinal across a restart, and **no API key** in any log or exception |
| the agents suite | `tests/agents/{test_orchestrator,test_delegation,test_policy,test_reasoning}.py` + `tests/integration/test_end_to_end.py` | the **exact** event sequence, a **mediated** three-node cycle, the anti-goal **0.82 cosine path**, the critic's halves, and the parked task |
| the live S3 suite | `tests/storage/test_s3_live.py` (new) + `tests/storage/conftest.py` | the S3 path against **two real servers** |

## 3. What to check

### 3.1 Would the test fail without the fix?

For each behaviour the wave claims to have pinned, **revert the fix in a
scratch copy and run the test**. The agents claim mutation results — check a
**sample** of them yourself rather than all of them; pick the ones where the
claim sounds strongest, because that is where a rigged claim would be.

The claims to test (the agents' own numbers are in their reports under
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/`):

* T-005: **81** tests fail without the malformed-keyword fix; a valid schema
  is unaffected (902 (schema, payload) pairs, 0 differences).
* T-006: **13/13** mutations caught.
* T-007: **7** new tests fail against the pre-fix sources; **7** mutations of
  the fixed sources each caught.
* T-008: **17** mutations run, **17** caught.
* T-002: **13** runtime mutations — 10 caught, 3 explained.

### 3.2 Is the assertion the right one?

The traps that make a suite *look* covered:

* **A subsequence where the requirement is a sequence.** The exact event list
  must be a **list equality**; a `⊆` assertion passes for a wrong order and
  for missing events.
* **`<=` where the requirement is `==`.** The semaphore's cap must be
  `max_in_flight == 2`; `<=` passes with a semaphore that serialises
  everything.
* **A guard asserted indirectly.** The mediated three-node cycle must prove
  the **cycle** guard fired (the error's `cycle` field), not merely that an
  error was raised — and it must run at **`MAX`** modality, because at
  STANDARD the depth ceiling fires first and the test would pass even if
  `would_cycle` were deleted.
* **A threshold that is not read.** The anti-goal path must go through the
  **cosine** branch (an injected embedder), not the keyword fallback, and
  must assert **both** sides of 0.82.
* **The function against itself.** A percentile test that compares
  `percentile(x, 0.95)` to `percentile(x, 0.95)` proves nothing. The values
  must be **hand-computed** (p95 of `[1..5]` = 4.8) and the **algorithm**
  pinned by a property over `rank = (n-1)·q`.
* **A test that only re-states the implementation.** A `finish_run` test
  whose expected row is built by calling `finish_run` is a tautology.
* **Setup used as assertion.** The baseline said `finish_run`'s returned row
  was used as *setup* and never asserted — that is the shape to look for.

### 3.3 Hypothesis, honestly used

* Does every property test **draw** the adversarial value from the strategy?
  A strategy that *might* produce the raising input makes the test a coin
  flip.
* Is `pytest.mark.filterwarnings` respected — a `DeprecationWarning` from
  `paa` fails the test?
* Are the property tests **deterministic** (no clock, no sockets, no
  randomness beyond the strategy)?

### 3.4 The xfails

Three tests are `xfail(strict=True)`. Each is a defect the agent found in a
file **outside its write-set**. For each one: is the reproduction real, is the
reason accurate, and would the test **fail loudly** when the defect is fixed
(strict=True) rather than silently pass? An `xfail` with a vague reason, or
one that would still pass after the fix, is a finding.

### 3.5 The live S3 suite

* Does it exercise the **real** path, or does it mock the thing under test?
  A "live" suite that stubs the client is a unit test with extra steps.
* Are the two servers genuinely **independent implementations**, or two
  containers of the same image?
* Is the suite **hermetic** — a unique bucket per run, cleanup on failure as
  well as success, no state left behind?
* Does it **skip** cleanly (and *say* why) when no server is configured? A
  suite that silently skips is the quietest way to not test something.
* Do its assertions name wave 1's error vocabulary correctly (ADR-0032)?
* Is there anything in it that would **pass against a broken backend**?

### 3.6 The suite as a whole

* **Order-independence** — the house rule is function-scoped fixtures writing
  into `tmp_path`. Any test that depends on another's state is a finding.
* **Duplication** — 417 new tests in one suite invites copy-paste. Look for
  near-identical bodies that assert the same thing twice, and for a
  parametrised table where a parametrised table belongs.
* **Flakiness** — anything timing-, ordering- or concurrency-dependent. The
  models agent reported a "measured flakiness range"; read it and judge.
* **The `assert_no_key_leak` helper** — is it *used* everywhere it should
  be, and is it strong enough (does it look at the log *records*, or just at
  the rendered strings)?

## 4. What you must run yourself

```bash
.venv/bin/python -m pytest tests/validation tests/observability tests/models tests/agents tests/integration tests/conventions -q
.venv/bin/python -m pytest tests/storage/test_s3_live.py -q -rs
.venv/bin/python -m pytest tests/ -q -p no:randomly 2>/dev/null || .venv/bin/python -m pytest tests/ -q
```

**Do not** take a report's numbers on trust: pick the mutation checks
described in §3.1 and run them yourself, in a scratch copy, and report the
actual output.

Do **not** start or stop the operator's `paa-stack-*` containers and do not
touch the operator's database. If you cannot verify the live S3 suite, say
so and mark it UNVERIFIED.

## 5. The report

Write `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-02-tests.md`.
Structure it as:

* **VERDICT** — one line.
* **FINDINGS** — severity (**BLOCKER** / **MAJOR** / **MINOR** / **NIT**), the
  file and line, what is wrong, **the evidence** (the command output), and
  the suggested fix.
* **MUTATIONS YOU RAN** — the ones you actually performed, with the actual
  output, and which ones **survived** (a surviving mutation is a finding
  about the test, not about the code).
* **VERIFIED** / **UNVERIFIED**.
* **COVERAGE LEDGER** — for each of the ROADMAP's clauses in 0.2, 0.3, 0.4
  and 0.5: which test file and test name now pins it, or the honest answer
  "still uncovered".

Do not edit code. Report what you find.
