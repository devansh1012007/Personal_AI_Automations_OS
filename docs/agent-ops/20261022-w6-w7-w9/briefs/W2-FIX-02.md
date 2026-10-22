# Brief — W2-FIX-02 (close-out fixer) · wave 2 · run 20261022-w6-w7-w9

**You are the close-out fixer for wave 2.** Five improve fixers have landed
(F-1, F-2, F-3, F-4, F-7, R-W2-201, R-W2-202, R-W2-203). The gate is green
(ruff 0, mypy 0, 2851 passed / 235 skipped / 4 xfailed). Four items are
**open**, and wave 2 cannot close until they are resolved: three NEEDs posted
by the fixers and one architectural deferral they could not make themselves.

Read the fixers' reports first — they are precise about what they left and
why:
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-IMPR-{01,02,03,04,05}-*.md`.

---

## Item 1 — the shared keyword table's home (the DEFERRED-DEPENDENCY)

W2-IMPR-02 made the malformed-keyword rule consistent across the tree's three
JSON-Schema validators by giving them **one table of keyword shapes**. The
table currently lives in `src/paa/validation/schema_validator.py`, and
`src/paa/models/base.py` imports it from there.

**The cost, measured by W2-IMPR-02:** the model layer's import graph now
pulls in the whole `paa.validation` package **and** `paa.sandbox` —
**+26.4 ms** and **16 new modules** on the model layer's import. The model
layer is imported by nearly everything, so this is paid by the whole process.
The report calls the edge "safe" (no cycle, no optional extra) and still
posts the deferral, because safe is not the same as free.

**The task:** move the table to a **leaf** module that all three validators
can import without either of them importing the other, and re-measure the
import cost. `src/paa/core/` is the candidate the report names — but the
placement is yours to argue: the requirement is that the new home is a
**leaf** (no heavy imports, no cycle, no optional extra), that the three
validators import it without importing each other, and that the import cost
of the model layer returns to roughly what it was.

**Requirements:**
* The table is **one** table, still — the point of the change was that a
  keyword malformed in one validator is malformed in all three.
* It is **public** where it is consumed (the report says the prior draft's
  private `_KEYWORD_SPECS` import was already discarded — keep it that way).
* **Re-measure** the model layer's import cost before and after, with the
  same method the repo's lock file uses (`tests/conventions/test_import_conventions.py`
  times imports in fresh interpreters and takes the **minimum** of five runs —
  read it, and follow its method). Report the actual numbers, not an estimate.
* Every test that pins the rule must still pass, and the rule's behaviour
  must be **unchanged** — this is a move, not a redesign.

---

## Item 2 — the router must get the deployment's mode (IMPR-03's NEED)

`src/paa/models/router.py` now reads the policy mode so its healthcheck can
refuse to probe the frontier under LOCKDOWN — but the mode is only known
**at the call site**. `src/paa/models/__init__.py::get_model_router` is that
call site, and it is **not** passing the mode ⟹ the router falls back to its
fail-closed default (LOCKDOWN), which is safe but wrong for a deployment
that is not in LOCKDOWN.

Wire the mode through, from the settings the factory already has. The
readers' report §F-7 says the router uses `Settings.policy.mode` and
otherwise **fails closed**; that fail-closed default must survive your
change — a caller that cannot name a mode gets the safe one.

---

## Item 3 — the redaction helper's home (IMPR-02's NEED)

W2-IMPR-02 applied the redaction rule to both sibling errors, and posted a
NEED about the router's `_redact_detail` helper. Read the NEED in the
report and resolve it: one helper, one rule, used by both layers, in the
place the house style would put it — and **not** by widening what is
redacted without saying so. The rule that must hold: a provider's own
output never reaches a log line, a ledger payload, or an exception's message
and details.

---

## Item 4 — ADR-0033's title and its false claim (IMPR-05's NEED)

`docs/adr/0033-s3-live-server-suite.md` repeats the claim W2-IMPR-05
measured to be false (that the wrapper carries neither response nor cause),
and its **title** says "two independent servers" — which the live suite
cannot assert by itself. W2-IMPR-05's report §6 has the exact replacement
wording. Apply it, and make the ADR consistent with ADR-0032's corrected
decision 4.

---

## Item 5 — the dead branch in the policy agent (IMPR-04's finding)

W2-IMPR-04 found that `_check_lockdown`'s **egress check is unreachable**,
and reported five pattern-semantics findings (the delete rule's answer
depends on the step's *position* in the plan; `git push -f`, `rm -rfv`,
`DELETE FROM public.users` and `TRUNCATE` without `TABLE` are all allowed)
**deliberately unpatched**, because each widens a security rule and that is a
policy decision beyond a test-coverage task.

**Your scope is only the first of those.** Decide, and say which:
* If the unreachable egress check is a **defect** — a check that never runs —
  then it is not a policy decision, it is a broken check: fix it and add the
  test that proves the check now runs. But if making it reachable would
  **change what the policy refuses**, that is a policy change and you must
  **not** make it silently: report it with the proposed change and the
  consequence, and leave the behaviour as it is.
* The **five pattern-semantics findings** are a policy decision and are
  **out of scope**: carry them forward as a FINDING in your report, with
  each one's reproduction and your recommendation, so the operator can decide
  whether a later task widens the rule. **Do not patch them.**

---

## Acceptance criteria

* The keyword table has **one** home, in a leaf module, and the model
  layer's import cost is **re-measured** before and after with the repo's own
  method. The actual numbers are in your report.
* `get_model_router` passes the deployment's mode, and the router's
  fail-closed default survives for a caller that cannot name one.
* The redaction rule has **one** helper and **one** rule, in both layers.
* ADR-0033's title and claim are corrected, and it agrees with ADR-0032.
* The dead branch is either fixed with a test that proves it now runs, or
  reported with the exact proposed change and its consequence — and the five
  pattern findings are carried forward, unpatched, with reproductions.
* The rule's behaviour is **unchanged** by the move.
* Scoped: the suites that cover the touched code, `ruff check src tests`,
  `mypy src`, and the full gate must not regress.

## Ownership

**WRITE-SET:** `src/paa/core/**` (the table's new home — a new module if
that is what it takes), `src/paa/validation/schema_validator.py`,
`src/paa/models/base.py`, `src/paa/models/router.py`,
`src/paa/models/__init__.py`, `src/paa/skills/contracts.py`,
`src/paa/agents/policy.py` (only if item 5's fix is a defect fix),
`tests/**` for the code you touch, `docs/adr/0033-s3-live-server-suite.md`.

**READ-ONLY:** `docs/adr/0032-blob-error-vocabulary.md` (it is corrected
already), `docs/adr/0034-*.md`, `docs/adr/README.md` (the orchestrator owns
the index), `src/paa/ledger/**`, `docs/plans/**`, `AGENTS.md`.

**FORBIDDEN:** `src/paa/**` outside the files listed above, `pyproject.toml`,
`deploy/**`, `docs/**` except ADR-0033, `docs/plans/**`, `AGENTS.md`.

**Note:** `tests/conventions/test_import_conventions.py` is a **lock** — if
your move changes what the tree looks like (a new module, a new import
edge), the lock may need a new row, and the lock is the thing that keeps the
convention true. Read it before you move anything, and if it needs a change,
make it **in the same commit** as the move it describes, and say why in the
report.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-FIX-02-closeout.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: wave-2-closeout`,
`Agent: W2-FIX-02`, `Wave: 2`.
Final message ≤ 8 lines: `STATUS | agent | items | report path | commit SHAs | blockers`.
