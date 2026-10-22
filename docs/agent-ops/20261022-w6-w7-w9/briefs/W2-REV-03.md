# Brief — W2-REV-03 (delta reviewer) · wave 2 improve · run 20261022-w6-w7-w9

## 1. Role

You are a **fresh independent reviewer** of the wave-2 **improve cycle**. You
did not write any of it. You are **not** the orchestrator and you are **not**
either of wave 2's first reviewers — their findings are the *input* to this
review, and your job is to check whether the fixes for those findings are
real.

**You do not edit code.** You report findings.

The single most useful thing you can do is find the thing that is still
wrong. Six agents have touched this code in the last hour; the gate is green
(ruff 0, mypy 0, 2859 passed); and **a green gate says nothing about whether
the five MAJORs are actually closed.**

## 2. The diff under review

```bash
git log --oneline 96a6e60..HEAD          # the improve cycle
git diff 96a6e60..HEAD -- src/ tests/ docs/adr/
```

Six agents: W2-IMPR-01 (F-1), W2-IMPR-02 (F-2, F-3), W2-IMPR-03 (R-W2-203,
F-4, F-7), W2-IMPR-04 (R-W2-201), W2-IMPR-05 (R-W2-202, F-5, F-6), W2-FIX-02
(the close-out: the keyword table's leaf home, the router's mode, one
redaction helper, ADR-0033's title and claim, the unreachable egress check).

Their reports are in `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/`
— read them, and treat their numbers as **claims**.

The findings they were fixing are in
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/` (W2-REV-01-code.md,
W2-REV-02-tests.md). Read the MAJORs there first: you are checking the fixes
against the requirement those findings stated.

## 3. What to check

### 3.1 Is each MAJOR actually closed? (the core of this review)

For **each** of the five MAJORs, answer: *would the new test fail if the fix
were reverted?* Run the reversal yourself, in a scratch copy, for at least
**three** of the five — and say which you ran and what actually happened.

* **F-1** — a second human-gate request in one lineage was swallowed by the
  ledger's idempotency key; the task committed with the policy's risk gate
  unanswered. The fix gives a gate request an identity
  (`gate:<role>:<ordinal>`). **Check:** is the identity *stable across a
  redelivery* of the same request and *distinct across different* requests?
  Can any automated path clear its own gate? Does a task that parks, is
  answered, resumes and meets a **second** gate now park again? And does
  ADR-0034 now say what the code does — the reviewer said the old ADR
  mis-described it.
* **F-2** — the malformed-keyword rule reached one of three JSON-Schema
  validators. The fix moved the table to a **leaf** home. **Check:** all
  three validators read **one** table; a malformed keyword is *reported* in
  all three (never a raw `TypeError`, never silently dropped); a **valid**
  schema's behaviour is unchanged; and the import cost the close-out measured
  is real (re-measure it yourself — the method is in
  `tests/conventions/test_import_conventions.py`: fresh interpreters, five
  runs, take the **minimum**).
* **R-W2-201** — six of the nine irreversible-operation patterns were
  uncovered. **Check:** all nine have a case that fails when that pattern is
  removed; the inventory is derived from the source, not hand-copied; and the
  tests go through the **policy agent's own decision**.
* **R-W2-202** — the S3 report's mutation 11 did not reproduce and the
  ADR-0032 correction it proposed was false. **Check:** ADR-0032 and ADR-0033
  now say what is **true**; the *code* matches their intent; and the two
  live tests that were added actually fail when the thing they cover breaks.
* **R-W2-203** — the key survived on the exception's `__cause__` and in the
  traceback. The fix: the router's raised errors carry **no chain at all**,
  built in a helper and raised *outside* the handler with `from None`.
  **Check:** `__cause__` and `__context__` are empty, the rendered traceback
  is clean, and the claim in the test file matches what is true. Also judge
  the **cost**: the provider's stack frames are lost for diagnosis — was that
  a deliberate, documented trade (the fixer's report says it was stated), and
  is it the right call?

### 3.2 Did the improve cycle break anything?

* The whole diff, read as a diff: regressions, scope creep, a fix that
  changed a behaviour nobody asked to change, a test that was weakened to
  make a fix pass.
* **Concurrency:** the router's discriminator is now the event's own uuid4
  and the lineage's order is the ledger's `state_version` — check the
  claim that the ledger still cannot suppress a second audit record, and
  that the ordering is well-defined under concurrency.
* **The lock:** `tests/conventions/test_import_conventions.py` is the thing
  that keeps the import convention true, and the close-out moved a module
  into the tree. Does the lock still describe the tree? Does it still have
  teeth — **run one of its mutations yourself**.
* **The xfails:** there are now **five** strict xfails. For each: is the
  reproduction real, is the reason accurate, and would it fail loudly when
  the defect is fixed? One of them is the **unreachable egress check** in
  `_check_lockdown`, which the close-out deliberately did not patch because
  making it reachable would change what the policy refuses — judge whether
  that call is right, and whether leaving it is honest.
* **The five pattern-semantics findings** W2-IMPR-04 carried forward
  unpatched: are they accurately reported, and is "not a test-coverage
  task's decision" the right call?

### 3.3 Are the reports' claims true?

The fixers claim: 11 tests fail against the pre-fix tree (F-1); 7 mutations
each caught; 9/9 patterns with the reviewer's own A13 mutation now failing
14 tests; 6 targeted mutations each caught by exactly one test; 230 passed ×5
consecutive runs for `tests/models`; 2496 (schema, payload) pairs with 0
differences; 172 golden rows matching the pre-fix source; 126 of 147 cases
failing without the fix; a 27-keyword matrix where the pre-fix code gave 6
raw `TypeError`s, 1 `AttributeError`, 19 silently ignored keywords and 1
answered wrongly (27 constrained keywords in all); and an
import-cost measurement of **+26.4 ms and 16 new modules** before the move.

**Spot-check these.** Pick the ones that matter most for the findings and
verify them yourself. A claim you did not check is not a claim you can rely
on.

## 4. What you must run

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/agents tests/models tests/validation tests/skills tests/observability tests/storage tests/conventions tests/integration -q
```

Do **not** take the reports' numbers on trust — report the **actual** output
of what you ran. Do **not** start or stop the operator's `paa-stack-*`
containers and do not touch the operator's database. If you cannot verify
something, mark it UNVERIFIED rather than assuming.

## 5. The report

Write `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-03-delta.md`:

* **VERDICT** — one line: is wave 2 now safe to close?
* **PER-MAJOR** — for each of the five MAJORs: **CLOSED** / **PARTLY
  CLOSED** / **OPEN**, with the evidence you produced.
* **FINDINGS** — severity (**BLOCKER** / **MAJOR** / **MINOR** / **NIT**), the
  file and line, what is wrong, the evidence, the suggested fix.
* **CLAIMS CHECKED** — the fixers' numbers you verified, with the actual
  output, and the ones you did not check.
* **VERIFIED** / **UNVERIFIED**.
* **NOT A FINDING** — what you looked at and are satisfied with.

Commit **only** that report, docs-only, explicit path, on branch
`ops/20261022-w6-w7-w9`:

```bash
test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
git commit -m "docs(agent-ops): the wave-2 delta review" -- docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-03-delta.md
```

Your final message ≤ 12 lines: VERDICT, then one line per MAJOR
(CLOSED/PARTLY/OPEN), then each finding as `SEVERITY | file:line | one line`,
then the report path and the commit SHA. **It must not be empty.**
