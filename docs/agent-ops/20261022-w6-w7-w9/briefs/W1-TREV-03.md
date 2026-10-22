# Brief — W1-TREV-03 (test reviewer) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** judge the wave-1 **tests** as work product — would they catch the
defect they claim to prevent, and do they fail when the fix is reverted?

**Your lens:** test quality. You are the reviewer who asks "if the code were
wrong, would this suite notice?"

**Your role:** test reviewer. **Read-only on source.** You never fix; the
improvement step is a different agent. You write only your report.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W1-REVIEW-CONTEXT.md` — **the
   shared context: the wave, the diff range, the gate, and the claims to
   audit.** Read it first.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
3. `docs/agent-ops/20261022-w6-w7-w9/templates/review-tests.md` — **your report
   template.** Use it.
4. `AGENTS.md` — the house rules, and the testing conventions section
   (`asyncio_mode = "auto"`, `filterwarnings = ["error::DeprecationWarning:paa.*"]`,
   the extras-gated marker + `skipif`/`importorskip` rule, the hypothesis
   convention, and the rule that a hypothesis test must *draw* the adversarial
   value from the strategy).
5. `docs/plans/w6-w7-w9.md` — §0 (the evidence), §2.2/§3.1/§3.2.
6. The tests: `tests/storage/test_blob_errors.py`, `tests/storage/test_minio_backend.py`,
   `tests/storage/test_blobstore_contract.py`, `tests/deployment/test_fetch_images.py`,
   `tests/conventions/test_import_conventions.py`
7. The diff: `git diff 02f3636..3ac9d6b -- . ':!docs/agent-ops'`

## 3. Your task

**The order matters: blind pass first.**

1. **Blind pass.** Read the tests and the code they test. Form your own
   findings **before** you read the implementer reports.
2. **Claims audit.** Read the implementer reports and check the reversal
   claims specifically: "28 of the new tests fail against the pre-change
   sources" (T-001), "18 of the new tests fail against the pre-change script"
   (T-003), "reverting the change fails two of the [seven] locks" (T-004).
   **Verify them yourself** — in a **temporary worktree** (never the shared
   tree), or by a targeted check. A claimed reversal that is not reproducible
   is a finding.
3. **Per-test assessment.** For each new test: does it assert a **behaviour**
   or an implementation detail? Is it deterministic and isolated? Is anything
   over-mocked — is the thing under test mocked away? Does the mock match the
   real contract (this matters most for the `StubS3Client`, whose exceptions
   are duck-typed because `boto3` is not installed)?
4. **Mutation thinking.** Name **at least five** plausible bugs in the changed
   code — off-by-one, inverted condition, missing `await`, a dropped error, a
   wrong default, a swallowed exception, a wrong exception type — and say
   whether the tests would catch each. Report the count of plausible bugs the
   suite does **not** catch.
5. **Flakiness.** Are the new tests order-dependent, clock-dependent,
   filesystem-dependent, or sensitive to the developer's environment? The
   fetch-images tests must scrub the developer's `PAA_*` and `STUB_*`; check
   the new ones do too. Is the new `tests/conventions/` package's
   subprocess-based import check slow, and is it bounded?
6. **Missing edge cases**, as a **prioritised** list (P1 must add / P2 should
   add / P3 nice to have), each with a concrete case and where it belongs.
   Consider: boundaries, empty/null/duplicate input, unicode/encoding,
   oversized payloads, malformed/hostile input, concurrency/races,
   idempotency/retry/replay, partial failure/timeouts, resource exhaustion,
   authz bypass, backward compatibility, clock/timezone.
7. **The changed behaviour without a test** — an automatic MAJOR.

**Specifically, and at minimum:**

* **T-001's suite.** The vocabulary has seven members. Is there a **test per
  member**, or are some only reachable through a path nothing exercises? Is
  the **idempotence** property tested with a *value* that is already one of
  ours, and is it tested for **every** member, or for one? Is the
  "no driver exception escapes" claim tested by **enumeration** (every
  method, every failure) or by a few examples? The implementer says 28 of the
  new tests fail against the pre-change sources — check the other new tests
  (there are 50 in `test_blob_errors.py`) and say whether any of them would
  have passed before, and therefore prove nothing.
* **T-003's suite.** The expected service set is now **derived** — check that
  the derivation is not circular. Does a test exist that would fail if the
  script and the compose file disagreed *in the merged case* (an image in the
  override only)? Is the "refuses what it does not support" behaviour tested
  for **each** refused shape, and is the refusal's **exit code** asserted?
* **T-004's suite.** The lock has seven tests. Which of them are **real
  regression tests** (fail on revert) and which are descriptions? Is the
  "137/137 modules import in a fresh interpreter" check **bounded** (a
  timeout, a cap) and **deterministic**?

## 4. Ownership

**You may write exactly one file:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-TREV-03-tests.md`
(and append to `COMMS.md`, which the orchestrator commits).

**READ-ONLY:** everything else, including all source and all tests. **You never
fix.** If you want a new test written, describe it in the report.

## 5. Deliverables

* **Report:** `reports/w1-review/W1-TREV-03-tests.md`, using
  `templates/review-tests.md`. The ORCH-DIGEST block first: agent, scope,
  verdict, tests reviewed, fail-before/pass-after counts, findings by severity,
  flaky suspects, missing edge cases by priority, mutation survivors,
  changed-behaviour-without-tests.
* **Findings** with an ID (`R-W1-…`; keep yours distinct from the code
  reviewers' — use `R-W1-2xx` if you need to), severity, `file:line`,
  evidence, and a recommended fix.
* **The missing-edge-case list** as a table: scenario, input, expected
  behaviour, where it belongs, and a ready-to-write sketch.
* **COMMS:** at most 6 lines.
* **Final message (≤ 8 lines):** `STATUS | agent | scope | report path | verdict | findings by severity | mutation survivors | blockers`.

## 6. Notes

* **A finding you cannot evidence is not a finding.**
* If you use a temporary worktree to test a reversal, create it **outside**
  the repository's working tree (e.g. under `/tmp`), and remove it when you
  are done. Never `git stash`, `reset` or `checkout` in the shared tree.
* The house has a precedent for a **source-shape** test (W4's
  `test_the_script_carries_no_hand_copied_image_table`), so T-004's AST-based
  lock is a legitimate kind of test — **provided** it fails on the reverted
  tree. That is the thing to check.
