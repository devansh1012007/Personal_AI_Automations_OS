# ADR-0035: The unqualified-delete rule's answer must not depend on where the step sits

## Status

Accepted

*Recorded 2026-10-22 by W2-FIX-03 (wave 2, last round). The change is one
regular expression and two tests. The decision it records is not about the
regular expression: it is that the rule the pattern's own label names —
"unqualified delete" — is the rule the gate must apply, and that a rule whose
answer depends on the *position* of the step it judges is not that rule.*

## Context

`src/paa/agents/policy.py` holds the gate's table of irreversible
operations. One row of that table is the **unqualified delete**:

```python
(r"\bDELETE\s+FROM\s+\w+\s*(;|$)", "unqualified delete"),
```

and the table's own prose says what the rows are for:

> Operations that destroy data or are otherwise unrecoverable. Blocked
> outright under SAFE; gated elsewhere.

`_check_irreversible` says the same thing again — *"SAFE mode hard-blocks
anything unrecoverable"* — and the near-miss test that pins the rule's scope
says it a third time: *"the pattern's label is 'unqualified delete' and that is
what it matches. A rule that refused every `DELETE FROM` would be a different
and broader rule"*.

**What the pattern actually did.** The terminator set is `;` or the end of
the **text**. The gate does not scan one command: it renders the whole plan —
the goal and every step — into one string, joined by **newlines**
(`PolicyRiskAgent._render`), and scans that. So "the end of the text" is the
end of the *plan*, and a statement that ends anywhere else never ends. The
same delete was therefore refused or allowed depending on the steps that
happened to follow it.

Measured through the gate's own verdict (SAFE mode):

```
DELETE FROM users                       (the only step)          -> STATUS_BLOCKED
DELETE FROM users                       (followed by a step)     -> STATUS_APPROVED
DELETE FROM users;                      (followed by a step)     -> STATUS_BLOCKED
```

The second line is the defect: the *same* command, refused when it is last
and allowed when it is not. The third line is the tell — the semicolon is
what saves it, and a semicolon is a statement terminator; a newline is the
other one, and the pattern did not know it.

**Why it was not merely untidy.** The same pattern feeds the **risk score**
(`_risk_score` calls the same matcher on the same blob — "One matcher,
shared by the SAFE refusal and the risk score, so the two can never disagree
about what a pattern means"). A delete in a non-final position therefore
scored **0.0**, not 0.8. AUTO mode's confidence floor is 0.80
(`PolicySettings.auto_confidence_floor`), so a plan whose only risky step was
an unqualified delete looked *harmless* and was committed without asking
anybody. The hole was not only under SAFE; it was under every mode.

**Is the intent unambiguous?** Yes, on the evidence the tree itself carries:

* the pattern's **label** is "unqualified delete" — a rule about the absence
  of a `WHERE` clause, with no reference to position;
* the table's **prose** says such operations are "Blocked outright under SAFE;
  gated elsewhere" — no positional exception;
* `_check_irreversible`'s **docstring** says SAFE hard-blocks anything
  unrecoverable — no positional exception;
* **no prose anywhere in the tree** says the last step of a plan is judged by
  a different rule from the same command in the middle of it.

The defect is an **anchor that does not match the renderer's own separator**,
not a policy about which deletes may run. That is the difference between a
defect and a decision, and it is why this is fixed here rather than deferred
to the operator: deferring it would leave the gate applying a rule nobody
wrote, in the direction that lets data be destroyed without a human.

## Decision

**The delete pattern's terminator set becomes the renderer's own: a semicolon,
a newline, or the end of the text.**

```python
(r"\bDELETE\s+FROM\s+\w+\s*(;|\n|$)", "unqualified delete"),
```

The rule is unchanged. A `DELETE FROM <table>` that does not narrow itself is
unqualified, and it is refused under SAFE and gated elsewhere — now wherever
it sits in the plan.

**What the change deliberately does not do.** It does not become a rule about
the *absence of a WHERE clause*, and it does not widen to schema-qualified
table names. A `DELETE FROM audit_log WHERE id = 1` removes one row and stays
allowed, in a final position and in a non-final one; the near-miss test now
pins it in both. The change is the terminator set, and nothing else.

### Alternatives rejected

* **`\bDELETE\s+FROM\s+[\w.]+\b(?!\s+WHERE)`** — the rule as "absence of a
  WHERE clause" (W2-FIX-02's recommendation). This is a *different* rule, and
  it is a wider one: it matches the phrase "delete from the old notes" in a
  **goal**, so a plan whose goal merely mentions deleting something would be
  refused under SAFE and would take the risk score to 0.8 in every other
  mode. It also widens the rule to `DELETE FROM public.users`, which is a
  separate defect with its own consequences. Adjudicating that rule needs a
  corpus of real command spellings, and it is not this change.
* **Per-step rendering** — scan each step's own text, so "the end of the
  statement" is "the end of the step". A bigger blast radius: every rule that
  scans the blob (the path guard, the operator's regex rules, the anti-goal
  match) would change meaning, and the goal would stop being scanned by the
  irreversible rule at all — fewer refusals, which is the unsafe direction.
* **Leave it and report it.** The gate would keep applying a rule nobody
  wrote, in the direction that lets a plan destroy a table without a human.
* **Delete the rule.** Not what any of the prose says.

### What this ADR does not decide

The other four pattern-semantics findings W2-IMPR-04 reported are **not**
decided here and remain open: `git push -f` (the pattern wants `--force`),
`rm -rfv` and `rm --recursive --force` (the flag spellings), `DELETE FROM
public.users` (the table name is not one word) and `TRUNCATE events` without
`TABLE`. Each is a *different* pattern from the one this ADR changes, each
widens a rule over prose, and each is the operator's call.

## Consequences

**What it costs.**

* **A refusal that did not happen now happens.** A plan that contains an
  unqualified delete which is not the last part of the rendered plan is now
  refused under SAFE, where it was approved. This is a behaviour change on a
  live path, and it is the point of the change.
* **More plans park on a human.** The same pattern feeds the risk score, so
  the same plan now scores 0.8 instead of 0.0 — which is a gate under AUTO
  (confidence 0.20 against a 0.80 floor) and under ASK (risk ≥ 0.5). A
  deployment that runs in AUTO will see more human gates on plans that
  contain an unqualified delete.
* **The over-refusal class.** A rendered line that *ends* with "delete from
  <one word>" is now a delete statement — including a goal that says "delete
  from customers". The gate already refuses prose that carries a command
  pattern (a goal containing `rm -rf` is refused under SAFE today), so this
  is the same class of over-refusal the gate has always accepted; but the
  class is **wider** than before, and that is the cost of closing the hole.
  The narrower alternative (the `WHERE`-absence rule) is *worse* on this
  count, not better.

**What it does not change.**

* A qualified delete stays allowed, in any position.
* The other eight patterns, the table's shape, the gate's verdict shape, the
  event vocabulary and every ledger payload are untouched.
* No stored data depends on the change: the rule is a live check, not state.
  A lineage written before this change replays exactly as it did.

**How to roll it back.**

Revert the one pattern in `src/paa/agents/policy.py` — the terminator set
back to `(;|$)` — and drop the two tests this change added to
`tests/agents/test_policy.py`. The behaviour returns to position-dependent:
a delete in a non-final position is approved under SAFE and scores 0.0. No
migration, no data repair, nothing else in the tree depends on the change.
