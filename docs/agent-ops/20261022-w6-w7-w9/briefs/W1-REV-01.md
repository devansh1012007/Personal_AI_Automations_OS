# Brief — W1-REV-01 (code reviewer) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** audit the wave-1 code changes for correctness, root cause, and
contract soundness — and try to break them.

**Your lens:** **correctness and design.** You are the reviewer who asks "is
this the right fix, and does it hold under conditions the tests do not
reach?"

**Your role:** code reviewer. **Read-only on source.** You never fix; the
improvement step is a different agent, with traceable dispositions. You write
only your report.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W1-REVIEW-CONTEXT.md` — **the
   shared context: the wave, the diff range, the gate, and the claims to
   audit.** Read it first.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding
   (the review-specific parts are in §15 of the protocol; the rest still
   applies: ownership, evidence tags, no secrets, the report shape).
3. `docs/agent-ops/20261022-w6-w7-w9/templates/review-code.md` — **your report
   template.** Use it.
4. `AGENTS.md` — the house rules a reviewer is asked to hold (in the shared
   context).
5. `docs/plans/w6-w7-w9.md` — §0 (the evidence), §3.1/§2.2/§3.2 (each task's
   design), §6 (the decisions, which are *given*).
6. `docs/adr/0025-postgres-dialect-seam.md` — the model T-001 copies.
7. `docs/adr/0030-deployment-image-contract.md` — the decision T-003 amends.
8. The diff: `git diff 02f3636..3ac9d6b -- . ':!docs/agent-ops'`

## 3. Your task

**The order matters: blind pass first.**

1. **Blind pass.** Read the diff and the surrounding code. Form your own
   findings **before** you read any implementer report. Write them down.
2. **Claims audit.** Now read the implementer reports (paths in the shared
   context) and check **every** claim in the list. An unsupported claim is a
   finding.
3. **Try to break it.** Propose concrete failing inputs and interleavings;
   run them if you can do so cheaply (a scoped test, a small script, a
   `git stash`-free experiment in a temp worktree — **not** the shared tree).
4. **The long-run test**, answered independently: Source / Load / Hostile /
   Operability / Reversal.
5. **The optimisation pass**: concrete opportunities with location, evidence,
   impact, effort, risk, and a verdict `DO NOW` / `DO LATER` / `NOT WORTH IT`.
   The "not worth it" list matters — it stops speculative over-engineering.

**Specifically, and at minimum:**

* **T-001.** Is the vocabulary *complete* — is there a path where a driver
  exception, an `OSError`, a `zstandard.ZstdError` or a `TypeError` can still
  reach the caller? Is the translation **idempotent**, and does it swallow
  anything it should not (a `PaaError` that is not ours must survive)? Does
  the protocol's new error clause match what the implementations actually do
  — **not** what the tests say? Is the `exists()` / `stat()` / `delete()`
  behaviour change (a missing bucket now raises) *coherent* with that clause,
  and with the CAS implementation?
  **Watch the two substrates agreeing**: the CAS and the S3 store must fail
  the same way for the same reason, or the "one contract" claim is false.
* **T-003.** Does the merge implement the subset it claims, and does it
  **refuse** what it does not support? Is a refusal *loud* (non-zero exit,
  message naming the file and the shape)? Does the flag actually beat
  `PAA_COMPOSE_FILE`, and does the environment still work when the flag is
  absent? Is the digest pinning still enforced for every image the merged
  stack runs — including an image that appears **only** in the override?
* **T-004.** Is the conversion safe, or did it trade a lazy import for an
  eager one that costs something (import time, an optional dependency)? Does
  the fresh-interpreter check actually cover the modules it claims? Are the
  two locks real regression tests — do they **fail** when the change is
  reverted? And: the implementer says the plan's "nine cycle-load-bearing
  blocks" is wrong and only **one** is. **Verify that measurement**, because
  the plan and the records carry the wrong number.

## 4. Ownership

**You may write exactly one file:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-REV-01-code.md`
(and append to `COMMS.md`, which the orchestrator commits).

**READ-ONLY:** everything else, including all source. **You never fix.** If you
find a defect, you describe it and the improvement agent applies it.

## 5. Deliverables

* **Report:** `reports/w1-review/W1-REV-01-code.md`, using
  `templates/review-code.md`. The ORCH-DIGEST block first: agent, scope,
  verdict (`APPROVE` / `APPROVE-WITH-CHANGES` / `REJECT`), counts by severity,
  claims checked, band-aids found, top 3 risks, optimisation verdicts,
  reversal check, confidence.
* **Findings** with an ID (`R-W1-001`, …), severity (`BLOCKER` / `MAJOR` /
  `MINOR` / `NIT`), `file:line`, the evidence, why it matters, and a
  recommended fix **at the root-cause level**.
* **COMMS:** at most 6 lines, `FINDING` for anything a peer should know,
  `QUERY` for a question you cannot resolve. Never wait for a reply.
* **Final message (≤ 8 lines):** `STATUS | agent | scope | report path | verdict | findings by severity | blockers`.

## 6. Notes

* **A finding you cannot evidence is not a finding.** Tag claims
  `[VERIFIED path:line]`, `[RAN cmd -> result]`, `[ASSUMED]`, `[UNKNOWN]`.
* **"Missing tests for a behaviour change" is an automatic MAJOR.**
* The implementers are competent and the gate is green. That is what makes
  your job possible, not what makes it unnecessary: every defect in this
  repository's history was invisible to a green suite.
