# Brief — W2-FIX-03 (fixer) · wave 2, last round · run 20261022-w6-w7-w9

The delta review (`reports/w2-review/W2-REV-03-delta.md`) closed all five
MAJORs and left **4 MINOR + 3 NIT**. Wave 2 cannot close with them open.
Fix five of them; **assess** two; carry one.

## 1. The five to fix

### 1.1 The Anthropic override does not apply the malformed-schema rule (MINOR)

`tests/models/test_model_errors.py:431` — the Anthropic provider's
`complete_structured` **override** sends a broken schema (a `minLength` that
is a string) to the **frontier**, where the base class would have refused it
locally. The base class and the override therefore disagree, so the promise
*"one rule for a malformed schema keyword, in all three validators"* is false
on that path. It is honestly escalated and marked with a strict xfail, but an
xfail is a record of a defect, not a fix.

Make the rule hold on **every** path: the override must apply the **same**
rule as the base — not a special case, and not a second implementation. The
shared keyword table now lives in `src/paa/core/json_schema.py`; find it and
read it. The xfail must go away (or become a test that asserts the *fixed*
behaviour), and the strict xfail's slot must be filled by a real assertion.

### 1.2 The docstring that says the air-gap branch "cannot fire" (MINOR)

`tests/agents/test_policy.py:123` — the companion docstring claims the
air-gap branch "cannot fire"; the `_NET_PATTERNS` half **does** fire; only
the declared-permission half is dead. Correct the docstring to describe the
tree as it is, in the house style (the *why*, not the *what*). Check whether
the same stale claim appears anywhere else in that file or in
`src/paa/agents/policy.py`.

### 1.3 The same edge measured as two numbers (NIT)

`src/paa/core/json_schema.py:15` quotes the import edge as **26 ms**; the
lock's docstring (`tests/conventions/test_import_conventions.py`) quotes the
same edge as **19 ms**. Both were measured; nothing says they are different
edges. Make the record say what is true: either the two numbers measure
different things (say which, and how), or one of them is stale (re-measure
and correct it). **Do not leave two numbers for one edge.**

### 1.4 A human-granted clearance records no risk score (NIT)

`src/paa/agents/orchestrator.py:595` — when a human releases a gate, the
event records that the gate was answered but not **what the policy thought**
(the risk score the policy computed, if it computed one). A gate the human
answered is a decision, and a decision without the number it decided about is
half a record. Add what the lineage already knows, and a test that asserts
it. If the score genuinely does not exist on that path, say so in the report
and leave the code alone — do not invent a value.

### 1.5 The report's summary line contradicts its body (NIT)

`reports/w2-improve/W2-IMPR-02-F-2-F-3.md` — the summary says *"24 silently
dropped keywords"*; the body says **19 ignored + 1 wrong answer**, with
**6 `TypeError` + 1 `AttributeError`** exact. The body is right. Correct the
summary to match the body, and check the rest of that report's summary lines
for the same drift.

## 2. The two to assess, not to decide

Both are **policy**, and the run's scope (the substrate track and ROADMAP
phase 0) does not include changing what the policy refuses. Your job is to
state precisely which kind of thing each one is, and to recommend.

**2.1 The SAFE-mode hole.** W2-IMPR-04 found that `DELETE FROM users` is
**allowed unless it is the last step of the plan** — the rule's answer
depends on the step's *position*. The delta reviewer rated this **MAJOR**:
"deferred correctly but unscheduled".

**2.2 The unreachable egress check.** `_check_lockdown`'s egress check never
runs (W2-FIX-02's finding). W2-FIX-02 deliberately did not patch it,
because making it reachable would change what the policy refuses.

For each, answer three questions, with evidence:
1. Is the **intent** unambiguous — that is, does the code's own prose, the
   ADR, or the pattern's own name say what the correct behaviour is, such
   that a fix is the *implementation of the stated intent* rather than a new
   policy? Or is the correct behaviour genuinely a judgement about what
   should be refused?
2. If it is unambiguous: fix it, with a test that fails without the fix, and
   say in the report that the behaviour changed (a refusal that did not
   happen now happens — that is a **behaviour change on a live path**, so it
   needs an ADR, and the ADR must state the cost and the rollback).
3. If it is a judgement: **do not change the behaviour.** Write the finding
   up precisely — what the rule does now, what the intent appears to be, the
   two or three candidate changes with their consequences, and your
   recommendation — so the operator can decide. Carry it in the report and
   in the ledger's open-items list.

**Do not blur these two.** "It looked like a bug" is not a reason to widen a
security rule; "it is a rule about what counts as production data" is a
reason to leave it and ask.

## 3. Acceptance criteria

* The Anthropic path refuses a malformed schema locally, like the base class;
  the strict xfail is gone and replaced by a real assertion.
* The two docstrings/records (1.2, 1.5) say what is true.
* One number for one import edge (1.3).
* The clearance event carries the policy's number, or the report says why it
  cannot (1.4).
* Items 2.1 and 2.2 are either fixed with a test and an ADR, or written up
  with a recommendation and **not** changed.
* Scoped: `pytest tests/models tests/agents tests/conventions -q`,
  `ruff check src tests`, `mypy src`, and the full gate must not regress.

## 4. Ownership

**WRITE-SET:** `src/paa/models/` (the Anthropic provider and, if the fix
needs it, `base.py` — but **not** `router.py`, which another agent may have
touched), `src/paa/agents/orchestrator.py`, `src/paa/agents/policy.py`
(only if 2.1/2.2 is a defect fix), `src/paa/core/json_schema.py`,
`tests/models/test_model_errors.py`, `tests/agents/test_policy.py`,
`tests/conventions/test_import_conventions.py`,
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-IMPR-02-F-2-F-3.md`,
`docs/agent-ops/20261022-w6-w7-w9/01-work-ledger.md`,
and a new ADR under `docs/adr/` **only if** 2.1/2.2 is a defect fix.

**READ-ONLY:** `docs/adr/README.md` (the orchestrator owns the index — say in
your report which ADR number you used), `docs/plans/**`, `AGENTS.md`,
`src/paa/ledger/**`.

**FORBIDDEN:** `src/paa/**` outside the files listed above, `pyproject.toml`,
`deploy/**`, `docs/**` except a new ADR, `docs/plans/**`, `AGENTS.md`.

## 5. Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-FIX-03-last-round.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: wave-2-last-round`,
`Agent: W2-FIX-03`, `Wave: 2`.
Final message ≤ 8 lines: `STATUS | agent | items | report path | commit SHAs | blockers`.
