# Brief — W2-IMPR-02 (fixer) · wave 2 improve · run 20261022-w6-w7-w9

**Task:** fix **F-2** and **F-3** of
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-01-code.md`
(MAJOR · `src/paa/models/base.py:442`; MINOR · `src/paa/models/base.py:81`).
Read both findings in full before you start. **You are the fixer**, not the
original implementer.

## F-2 — the rule was applied to one of three JSON-Schema validators

Wave 2's T-005 fixed `src/paa/validation/schema_validator.py` so that a
**malformed keyword value** (a schema whose `minLength` is `"abc"`) is a
*schema error* — reported, never raised. The rule was **not** applied to the
other two validators, which implement JSON Schema independently:

1. `src/paa/validation/schema_validator.py` — **fixed** by wave 2.
2. `src/paa/models/base.py::validate_against_schema` (with `_validate_string`,
   `_validate_number`, `_validate_array`, …) — a hand-rolled validator.
   `if (minimum := schema.get("minLength")) is not None and len(instance) < minimum:`
   compares an `int` with a `str` ⟹ **a raw `TypeError** escapes
   `complete_structured`. It is not a `PaaError`, so the ledger records no
   `EXECUTION_FAILED` and the failure is invisible to the run.
   The keywords the reviewer names: `minLength`, `maxLength`, `minItems`,
   `maxItems`, `minimum`, `maximum`, `properties` — **enumerate them from the
   source yourself** and check every one.
3. `src/paa/skills/contracts.py::validate_json_schema_shape` — this one does
   **not** crash: it guards with `isinstance(minimum := schema.get("minimum"), int)`
   and so **silently ignores** a malformed keyword. That is the third
   instance of the same defect, and it is worse than a crash in one way: the
   instance is validated against a schema whose constraint was **dropped**, so
   the answer is "valid" against a schema the caller never wrote.

**The rule, once, everywhere:** a malformed keyword value is a **schema
error** — reported to the caller like any other invalid schema, never raised
as a `TypeError`, and never silently ignored. The fix must be *consistent*
across all three validators; a fix for the one keyword that was found is not
a fix.

Decide, per validator, what "reported" means, and be consistent: in
`models/base.py` the natural answer is that `complete_structured` raises a
`PaaError`-family error (not a bare `TypeError`), so the run records the
failure; in `skills/contracts.py` the module already raises `ValueError` for a
bad schema *shape*, and the caller turns that into `SkillContractError` —
follow that existing convention rather than inventing a second one.

**A valid schema must behave exactly as before.** The wave-2 report claims
902 (schema, payload) pairs were compared with 0 differences; the reviewer
re-ran the differential and confirmed it. Your change must keep that true —
**measure it again** for the code you touch.

## F-3 — the redaction rule was applied to one sibling and not the other

`StructuredOutputError` scrubs its `schema_errors` at construction.
`ModelUnavailableError.__init__` does `super().__init__(redact(message),
provider=provider, **details)` — the **message** is redacted and the
**details** are not. A provider's "connection refused: key=sk-…" detail
therefore survives into `error.details`, and from there into the ledger
payload or a log line. Apply the same rule to the sibling: the *reason* for
the redaction is identical — the details are built out of the provider's own
output.

Be careful about **what** is redacted: `details` is a free-form mapping and
the redaction helper works on strings. Decide and write down the rule (e.g.
every string *value* in `details` is redacted, the keys are not) and apply it
consistently.

## Acceptance criteria

* No raw `TypeError` escapes `complete_structured` for **any** malformed
  keyword of **any** of the three validators — a test per validator, and per
  keyword family, that would fail without the fix.
* A malformed keyword is **reported**, not ignored: `skills/contracts.py`
  must not accept an instance against a schema whose constraint it dropped.
* A valid schema's behaviour is **unchanged**: re-run the differential (the
  902-pair harness the wave-2 report describes, or a documented equivalent) and
  report the actual numbers.
* `ModelUnavailableError`'s details are redacted, and the test proves it.
* Scoped: `.venv/bin/python -m pytest tests/models tests/skills tests/validation -q`,
  `ruff check src/paa/models src/paa/skills tests/models tests/skills`,
  `mypy src/paa/models src/paa/skills` — green, and the gate must not regress.

## Ownership

**WRITE-SET:** `src/paa/models/base.py`, `src/paa/skills/contracts.py`,
`tests/models/test_schema_crash.py` (new — find the right name for it if the
directory's convention says otherwise), `tests/skills/test_contracts.py` (or
whichever file covers `contracts.py` — **find it**, and if none exists, create
`tests/skills/test_contracts.py`).

**READ-ONLY:** `src/paa/models/router.py` and every other file in
`src/paa/models/` (another agent owns `router.py` this wave — **do not edit
it**), `src/paa/validation/**` (read `schema_validator.py` for the rule you
are mirroring), `tests/models/conftest.py` (another agent owns it — you may
*read* `assert_no_key_leak`, but do not edit it), the other `tests/models/*`
files.

**FORBIDDEN:** `src/paa/**` outside `models/base.py` and `skills/contracts.py`,
`tests/**` outside `tests/models/test_schema_crash.py` and `tests/skills/`,
`pyproject.toml`, `deploy/**`, `docs/**` (no ADR is needed for this — say so
in your report; if you think one is, `ESCALATE` and explain),
`docs/adr/README.md`, `docs/plans/**`, `AGENTS.md`.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-IMPR-02-F-2-F-3.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: F-2,F-3`,
`Agent: W2-IMPR-02`, `Wave: 2-improve`.
Final message ≤ 8 lines: `STATUS | agent | findings | report path | commit SHAs | blockers`.
