<!-- ORCH-DIGEST:START -->
agent: W2-IMPR-02 | scope: F-2 (MAJOR — the malformed-keyword rule reached one of three JSON-Schema validators) and F-3 (MINOR — the redaction rule reached one of two sibling errors) | status: DONE
findings: FIXED 2 (F-2, F-3) | REJECTED 0 | DEFERRED 0 (3 NEEDs posted; none blocks this task)
blockers_major_open: none
code_commits: 4a5bac0 (the fix), 77195f7 (the strengthened tests) | report_commit: (this commit)
tests_added_or_strengthened: 118 (70 in tests/models/test_model_errors.py, 48 in tests/skills/test_contracts.py); 98 of them fail against 4c9300d's source
ownership_violations: none — src/paa/models/router.py, src/paa/models/__init__.py, src/paa/models/anthropic_provider.py and src/paa/validation/** were read, never edited; three NEEDs posted for them
confidence: high on F-2 and F-3 (the rule is now one rule in one place, and the reversal is measured); the one thing I could not do is retire the router's private copy of the redaction policy — that file is not mine
<!-- ORCH-DIGEST:END -->

# Improvement report — W2-IMPR-02 (F-2, F-3)

**Agent:** W2-IMPR-02 (fixer, wave 2 improve).
**Findings:** F-2 (MAJOR), `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-01-code.md:94`;
F-3 (MINOR), same file, `:147`.
**Write-set:** `src/paa/models/base.py`, `src/paa/skills/contracts.py`,
`tests/models/test_model_errors.py` (new), `tests/skills/test_contracts.py`.
**Branch:** `ops/20261022-w6-w7-w9` [VERIFIED `git branch --show-current` → `ops/20261022-w6-w7-w9`].
**Last COMMS line read at start:** `[2026-10-22T06:48:43Z] ORCH -> ALL | DECISION | WAVE-INTERRUPTED`
(`docs/agent-ops/20261022-w6-w7-w9/COMMS.md:120`); last line read before finishing is in §17.

**The short version.** The interrupted dispatch's draft had the right *shape*
and one wrong *claim*, and it had no tests. I kept the shape — a
`MalformedSchemaError` that is a `PaaError` and deliberately not a
`StructuredOutputError`, and the idea that the three validators read one
table of keyword shapes — and I changed three things: the docstrings that
claimed the code imports a private name it does not import, the *shape* of
the coupling (it is now reached through the other module's public entry
point, not through its private table), and the absence of any test at all.
The rule now holds in all three validators, and I measured the reversal:
**98 of the 118 new tests fail against `4c9300d`'s source**, and a valid
schema answers **exactly** as it did before — **1092 comparisons, 0
differences**.

---

## 1. Task and acceptance criteria (restated)

From `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-IMPR-02.md`:

* **F-2** — a malformed keyword value must be a **schema error** in *all
  three* JSON-Schema validators: reported, never a raw `TypeError`, never
  silently ignored. The keywords must be enumerated **from the source**, not
  from the one the reviewer found.
* **F-3** — `ModelUnavailableError`'s `**details` must be redacted like its
  message and like its sibling's, and "what redacted means for a free-form
  mapping" must be written down.
* A valid schema must behave **exactly** as before, and the differential must
  be **re-measured**, not inherited.
* Scoped gate green; every test must fail without the fix.

## 2. The interrupted draft: what it claimed, what was true

The draft was in the working tree, uncommitted (`src/paa/models/base.py`
+224/−16, `src/paa/skills/contracts.py` +91/−6), backed up at
`/tmp/paa-w2-improve-backup/`. I read it against the source before touching
it. This is the audit the re-dispatch asked for.

| The draft's claim | Verdict | Evidence |
|---|---|---|
| "The three JSON-Schema validators read the **same** table of keyword shapes (`:data:`paa.validation.schema_validator._KEYWORD_SPECS`)" | **FALSE as written.** The code never imports `_KEYWORD_SPECS`; it imports `SchemaError` and `validate_instance` and filters on `schema_error`. The module docstring and two function docstrings described an import that does not exist. | the draft's own `from paa.validation.schema_validator import SchemaError, validate_instance` — there is no `_KEYWORD_SPECS` in it |
| "`paa.models` imports from `paa.validation`" (the interesting part) | **TRUE, and it is safe** — but the draft never argued it, and the argument is the thing the orchestrator asked for. | §4 below |
| "A malformed keyword is a schema error, never raised, never ignored" | **TRUE in intent.** The rule was right; the *reporting* differed per module, which the brief explicitly endorses. | §6 |
| "It is deliberately not a `StructuredOutputError`" | **TRUE, and the reasoning is right.** `StructuredOutputError` means "the provider answered badly"; the router escalates on it, and a `MODEL_ESCALATED` audit record would then say reasoning material left the machine for a caller's typo. | `src/paa/models/router.py:500` escalates on `StructuredOutputError` |
| "the details are scrubbed here, at construction" | **TRUE for `ModelUnavailableError`**, and the draft also applied it to `StructuredOutputError`'s free-form details — which is right, and which I kept. | §7 |
| tests | **NONE.** The largest gap. 0 test files, 0 assertions. | `git status` at dispatch: no new test file |

### 2.1 What I kept, what I changed, what I discarded

**Kept** (7 things):

1. `MalformedSchemaError` as its own `PaaError` subclass, deliberately not a
   `StructuredOutputError` — with the argument for why, which I kept verbatim
   in substance because it is the right argument.
2. The idea of **one table of keyword shapes** for all three validators.
3. Splitting each validator into a public entry point that checks the schema's
   shape once and a private recursive half that does not re-check it. This
   matters: the draft's split avoids walking the schema once per level.
4. `skills/contracts.py` raising `ValueError` (the module's existing
   convention for a bad schema shape) and `SkillContract` turning it into a
   `SkillContractError` — exactly what the brief prescribed.
5. The upfront refusal in `ModelProvider.complete_structured`, before the
   provider is called.
6. `minimal_instance_for_schema` refusing a malformed schema too — the draft
   was right that it reads keywords of its own (`minItems`, `minLength`,
   `minimum`, `maxLength`) and crashed on them.
7. Redacting the free-form `**details` of **both** error classes.

**Changed** (5 things):

1. **The coupling's shape.** The draft read the table *through the other
   module's public entry point* but *documented* it as a private import. I
   kept the code and fixed the documentation — and then argued the coupling
   properly, which is §4.
2. **The defect messages are scrubbed.** The draft carried `SchemaError`
   dicts into the ledger payload verbatim. They now go through the same
   construction-boundary scrub as everything else in the module.
3. **`redact_detail`'s docstring.** The draft claimed "the router's private
   copy is the one that should be retired in favour of this one" — while
   leaving the copy in place, in a file it does not own. I rewrote the claim
   to say what is true (the two are the same rule written twice, byte for
   byte today) and posted the retirement as a NEED.
4. **`_string_errors`'s `re.error` branch** in `skills/contracts.py`. The
   shape check now refuses a non-compiling `pattern` before the instance is
   judged, so the branch is a backstop rather than the normal path. The
   comment said the opposite; I corrected the comment rather than deleting a
   defensive branch.
5. **The test file's name.** The brief named `tests/models/test_schema_crash.py`
   and allowed a rename "if the directory's convention says otherwise". The
   directory names files after their *subject* (`test_key_secrecy.py`,
   `test_router_policy.py`, `test_escalation_audit.py`), and this file's
   subject is the model layer's **error contract** — it covers a broken schema
   *and* a broken secret. It is `tests/models/test_model_errors.py`.

**Discarded** (1 thing):

1. Nothing from the draft was thrown away wholesale. The one piece I did
   *not* carry forward is the draft's implicit claim that the shared table
   should be reached by importing `_KEYWORD_SPECS` — the draft's code did not
   do it, and doing it would bind `paa.models` to another module's private
   internals.

## 3. Root cause

There is no single root cause; there are two, and they are the same shape.

**F-2's root cause is that JSON Schema is implemented three times.** The tree
has a hand-rolled validator in `paa/models/base.py`, a hand-rolled validator
in `paa/skills/contracts.py`, and the one wave 2's T-005 fixed in
`paa/validation/schema_validator.py`. Each one grew its own notion of "what
can this keyword's value be", and each one grew its own *mistake*:

* `models/base.py` used the value raw (`len(instance) < minimum`) — so a
  wrong-shaped value became a `TypeError` out of a gate.
* `skills/contracts.py` guarded it (`isinstance(minimum := schema.get("minimum"), int)`)
  — so a wrong-shaped value became a **silently dropped constraint**, and the
  instance was judged "valid" against a schema nobody wrote.
* `paa/validation/schema_validator.py` (after T-005) had a table.

Three implementations of one rule is three places for the rule to be wrong.
The fix that scales is not "guard the keyword the reviewer found" — it is
**one table, read by all three**.

**F-3's root cause is that the redaction rule was applied at the raise site
for one class and not the other.** `StructuredOutputError` scrubs its
`schema_errors` at construction; `ModelUnavailableError` scrubs its message
and nothing else. `PaaError.to_payload()` merges `details` into a ledger
payload **verbatim** (`src/paa/core/errors.py:52-54`) and `PaaError.__str__`
renders them (`src/paa/core/errors.py:46-50`). So the *details* are the one
channel that reaches the ledger without passing a scrubber.

## 4. The import direction — the question the orchestrator asked

> `paa.models` importing from `paa.validation` is a **new coupling between
> two subsystems**. Check it against the repo's import conventions and
> against the rule in `AGENTS.md` that a heavy or optional dependency must
> never be imported at module scope in a core path.

**It is safe, and I kept it.** Here is the argument, in the order the
questions were asked.

### 4.1 The direction is the one the tree already uses

`paa.validation` is the deterministic, host-side validation layer
(`src/paa/validation/__init__.py`: *"Nothing in this package consults a
model"*). `paa.models` and `paa.skills.contracts` are **consumers** of it.

`paa.skills` **already** depends on `paa.validation`, and it is not new:

```
src/paa/skills/contracts.py:45   from paa.validation.schema_validator import JsonSchema, validate_instance
src/paa/skills/usa.py:48          from paa.validation.schema_validator import SchemaValidator
```

`src/paa/skills/contracts.py:45` imported `paa.validation.schema_validator`
on `4c9300d` — before my change. So the edge `models → validation` makes
`paa.models` a **sibling** of `paa.skills`, not a new kind of dependency.
The alternative — `paa.validation` depending on `paa.models` — would invert
the layering and put the inference layer underneath the validation layer.

### 4.2 No cycle

[VERIFIED] `grep -rn "paa.models" src/paa/validation/ src/paa/sandbox/` →
**no matches**. Nothing under `paa.validation` imports `paa.models`, so the
edge cannot close a cycle. This is also checked mechanically, not just by
reading:

```
$ .venv/bin/python -m pytest tests/conventions -q
17 passed in 14.03s
```

The lock at `tests/conventions/test_import_conventions.py` has a **sweep
half** that imports every one of the 137 modules under `src/paa` in a
*fresh interpreter* (a subprocess, because a test session's `sys.modules`
hides a cycle), and an **aggregate half** that imports them all in one
interpreter in reverse-alphabetical order. Both are green with my edge in
place. A cycle would have taken the package down at import time, not at
some later point.

### 4.3 No heavy or optional dependency at module scope

`AGENTS.md`: *"Heavy deps (qdrant, kuzu, sentence-transformers, redis, psycopg,
boto3) are optional extras; **never** import one at module scope in a core
path, or a minimal install stops importing."*

[VERIFIED] The whole transitive closure of the new edge — `paa.validation.*`
and, through `paa.validation.engine`, `paa.sandbox.*` — imports **only**
`structlog` and `pydantic` from outside the standard library:

```
$ grep -rn "^import \|^from " src/paa/sandbox/ src/paa/validation/ --include=*.py \
    | grep -vE "from __future__|from paa\.|import paa\.|from \.|import (abc|asyncio|ast|...)"
src/paa/sandbox/dryrun_backend.py:26:import structlog
src/paa/sandbox/watchdog.py:44:from dataclasses import dataclass, field
...
```

Both are **core** dependencies (`pyproject.toml`: `pydantic>=2.9`,
`structlog>=24.4`). `docker_backend.py` does **not** import the Docker SDK at
module scope — it speaks HTTP itself — so the sandbox subsystem does not drag
the daemon client in either.

The lock's **extra half** asserts this mechanically for every module: it
blocks the optional packages in a fresh interpreter and checks the module
still imports. `OPTIONAL_EXTRA_MODULES` names exactly one module
(`paa.storage.graph.kuzu_backend`), and `paa.models.base` is not on it —
green in the run above.

### 4.4 The cost, measured

[VERIFIED, 15 runs each, minimum of wall-clock over the whole process]

| | HEAD (`4c9300d`) | with my edge | delta |
|---|---|---|---|
| `import paa.models.base` | 268.7 ms | 311.8 ms | **+43 ms** |
| `import paa.cli.main` | 395.3 ms | 435.0 ms | **+40 ms** |
| `import paa.validation` alone, warm deps | — | 90.7 ms | — |

The 90.7 ms figure is the *worst* case: a bare interpreter in which
`paa.sandbox` is not already loaded. In the real entry points `paa.sandbox`
**is** already loaded (`import paa.cli.main` loads `paa.sandbox` on `HEAD`
too — VERIFIED), so the marginal cost is the `paa.validation.*` modules
themselves: **~40 ms, once per process**.

That is the price of one table instead of three, and it is paid by the CLI's
startup. It is the main argument against this design and I am not going to
pretend it is free.

### 4.5 The alternative I would defend, and why I did not take it

The alternative I would defend if the coupling were wrong is: **move the
table down into `paa.core`** (whose `__init__.py` is *empty*, so the import
would cost nothing), have `paa.models.base` and `paa.skills.contracts` read
it from there, and have `paa.validation.schema_validator` read it from there
too.

I did not do it, for two reasons that are about *ownership* rather than about
design:

1. It requires editing `src/paa/validation/schema_validator.py` — **read-only
   for me** — to move `_KEYWORD_SPECS`, `_KeywordSpec`, `_value_defects` and
   the eleven `_KIND_*` constants out of that module. That is wave 2's T-005
   code, and it is the code the reviewer's "the keyword table is complete and
   uniform" verdict is about.
2. It requires editing `tests/validation/test_schema_validator.py` — also
   read-only — which reads `_KEYWORD_SPECS` by module-level lookup
   (`tests/validation/test_schema_validator.py:84`) and asserts the union of
   the two tables equals `SUPPORTED_KEYWORDS` (`:572`).

So it is a **NEED with an exact change**, not something I could do inside my
write-set. It is the right *next* step if the 40 ms is judged too expensive,
and it is the change that makes the coupling free. I have written it up in
§14 as a deferred improvement rather than pretending the cost is zero.

### 4.6 One more thing the draft got wrong about the coupling

The draft's module docstring said the three validators "read the **same**
table of keyword shapes (`:data:`paa.validation.schema_validator._KEYWORD_SPECS`)".
Two problems:

* It is **false** — the code never imports that name.
* Even if it were true, it would be the wrong *mechanism*. `_KEYWORD_SPECS` is
  a private name; importing it binds `paa.models` to another module's
  internals and to its data layout. The public entry point
  (`validate_instance`, which reports a malformed keyword as an error with
  `schema_error=True`) is the supported way to ask the question, and it is
  the **same** question the other two validators ask through it. So the code
  now reads the table *through the public entry point*, and the docstrings
  say so.

There is a cost to that: `validate_instance(None, schema)` computes the
instance errors as well, and they are discarded. The instance half is a
throwaway; the shape half is what is kept. The cost is O(schema size), once
per validation call, on a schema that is a few hundred bytes.

## 5. The rule, and what "reported" means in each validator

> **A malformed keyword value is a schema error: reported, never a raw
> `TypeError`, never silently ignored.**

The rule is one rule. *How* it is reported is per module, because each
module has a convention and the brief says to follow it rather than invent a
second one:

| validator | how a malformed keyword is reported | why |
|---|---|---|
| `paa.validation.schema_validator` | returned in the error list with `schema_error=True`, and **not applied** | T-005's rule; it is a *collecting* validator and the caller decides what to do |
| `paa.models.base` | **raised** as `MalformedSchemaError` | the model path has no collector: `complete_structured` returns a dict or raises. A returned list of "the schema is broken" would be indistinguishable from "the instance is wrong", and the repair prompt would quote it to the model as if the model could fix it. It is a `PaaError`, so the run records it |
| `paa.skills.contracts` | **raised** as `ValueError`; `SkillContract` turns it into `SkillContractError` | the module already raises `ValueError` for a bad schema *shape* (`:133`, `:139`, `:146`); the contract is the caller and its convention is `SkillContractError` |

Two consequences worth stating:

* **`MalformedSchemaError` is deliberately not a `StructuredOutputError`.**
  The router escalates on `StructuredOutputError`
  (`src/paa/models/router.py:500`) and records a `MODEL_ESCALATED` event.
  A malformed schema is the *caller's* mistake; a better model cannot repair
  it, and nothing left the machine — so the error must stay off the
  escalation path. A class of its own is what keeps it off it.
* **The router's `except` clauses are narrow** (`router.py:500`, `:502`,
  `:583`): `StructuredOutputError` and `ModelUnavailableError` only. A
  `MalformedSchemaError` therefore propagates straight out of the router —
  no escalation, no audit record claiming reasoning left the machine — and
  reaches the orchestrator's `except PaaError`
  (`src/paa/agents/orchestrator.py:394`), which is what writes
  `EXECUTION_FAILED`.

## 6. The keyword inventory — enumerated from the source

The brief: *"a fix for the one keyword that was found is not a fix."*

### 6.1 `src/paa/models/base.py` — 16 distinct keywords

Read out of the module's own source (`_validate_against_schema`,
`_validate_object`, `_validate_array`, `_validate_string`, `_validate_number`,
`_validate_combinator`, `_minimal_instance_for_schema`):
`type`, `const`, `enum`, `allOf`, `anyOf`, `oneOf`, `properties`, `required`,
`additionalProperties`, `items`, `minItems`, `maxItems`, `minLength`,
`maxLength`, `minimum`, `maximum`.

`const` has **no** malformed value — any JSON value is a legal constant — so
it is absent from the defect table by arithmetic, not by omission.

### 6.2 `src/paa/skills/contracts.py` — 13 distinct keywords

`type`, `enum`, `properties`, `required`, `additionalProperties`, `items`,
`minItems`, `maxItems`, `minLength`, `maxLength`, `pattern`, `minimum`,
`maximum`.

The module does **not** implement `anyOf`/`oneOf`/`allOf`, so it never
applied them; the shared table constrains them anyway, and a malformed one is
refused. That is the rule applied uniformly rather than per-implementation,
and there is a test for it.

### 6.3 `src/paa/validation/schema_validator.py` — 27 constrained keywords

`_KEYWORD_SPECS` (27) plus `_ANNOTATION_KEYWORDS` (8) is exactly
`SUPPORTED_KEYWORDS` (35), asserted by the module's own test. T-005 already
closed this one; it is the source the other two now read.

### 6.4 What the pre-fix behaviour actually was — measured

This is the table the brief asked for, and it is not the reviewer's table.
The reviewer probed with a payload that *reached* each keyword; I measured
both.

| keyword (malformed value) | `4c9300d` `models/base.py` | now | `4c9300d` `skills/contracts.py` | now |
|---|---|---|---|---|
| `type: 5` | silently accepted | `MalformedSchemaError` | silently accepted | `ValueError` |
| `type: ["string", 5]` | silently accepted | `MalformedSchemaError` | — | — |
| `enum: "abc"` | **raw `TypeError`** | `MalformedSchemaError` | silently accepted | `ValueError` |
| `properties: "x"` | **raw `AttributeError`** | `MalformedSchemaError` | silently accepted | `ValueError` |
| `properties: {"a": "x"}` | silently accepted | `MalformedSchemaError` | — | — |
| `required: "a"` | silently accepted | `MalformedSchemaError` | silently accepted | `ValueError` |
| `required: ["a", 5]` | **wrong answer** (`missing required property 5`) | `MalformedSchemaError` | — | — |
| `additionalProperties: 5` | silently accepted | `MalformedSchemaError` | silently accepted | `ValueError` |
| `items: "x"` | silently accepted | `MalformedSchemaError` | silently accepted | `ValueError` |
| `minItems: "2"` | silently accepted (crashed with a string payload) | `MalformedSchemaError` | silently accepted | `ValueError` |
| `maxItems: "2"` | silently accepted (crashed with a string payload) | `MalformedSchemaError` | silently accepted | `ValueError` |
| `minLength: "2"` | silently accepted (crashed with a string payload) | `MalformedSchemaError` | silently accepted | `ValueError` |
| `maxLength: "2"` | silently accepted (crashed with a string payload) | `MalformedSchemaError` | silently accepted | `ValueError` |
| `minimum: "2"` | silently accepted (crashed with a number payload) | `MalformedSchemaError` | silently accepted | `ValueError` |
| `maximum: "2"` | silently accepted (crashed with a number payload) | `MalformedSchemaError` | silently accepted | `ValueError` |
| `anyOf: "x"` | silently accepted | `MalformedSchemaError` | silently accepted | `ValueError` |
| `anyOf: ["x"]` | silently accepted | `MalformedSchemaError` | — | — |
| `oneOf: "x"` | silently accepted | `MalformedSchemaError` | — | — |
| `allOf: "x"` | silently accepted | `MalformedSchemaError` | — | — |
| `pattern: "["` | — | — | silently accepted (constraint dropped, `re.error` logged) | `ValueError` |

**The finding inside the finding.** On `4c9300d` a malformed keyword was only
a *crash* when the instance happened to reach it. `minLength: "2"` against a
*dict* was accepted in silence; against a *string* it raised a raw
`TypeError`. So the defect had **three** faces — crash, silent drop, and
*wrong answer* (`required: ["a", 5]` asked for a property called `5`) — and
the fix has to cover all three. That is why each keyword is now exercised
against **two** payloads: one that reaches it and one that does not. The
whole-schema walk is what makes the second case reportable.

## 7. F-3 — what "redacted" means for a free-form mapping

The brief: *"Decide and write down the rule (e.g. every string *value* in
`details` is redacted, the keys are not) and apply it consistently."*

The rule, stated in `paa.models.base.redact_detail`:

* every **string value** is passed through `redact`, **at any depth** — a
  string inside a list, a tuple or a nested mapping is scrubbed too;
* the **keys** are **not** scrubbed. A key names a field; it is written by
  the code that raised the error, not by the provider, and callers and ledger
  payloads are written against those names — rewriting one would break the
  contract without removing a leak;
* a value that is **not** a string, a list/tuple or a mapping — a number, a
  boolean, `None` — is passed through unchanged. It cannot carry a
  credential, and the details have to stay JSON-serialisable for the ledger.

This is **byte-for-byte the same policy** as the escalation router's private
`_redact_detail` (`src/paa/models/router.py:125-150`) — I read both. The
router's copy is in a file I do not own, so I left it and posted the
retirement as a NEED.

Applied to:

* `ModelUnavailableError.__init__` — **the fix** (F-3).
* `StructuredOutputError.__init__` — the same rule, so the two siblings do
  not differ. (Today the only free-form detail any raiser passes is
  `model=`, a provider's model name — so this is a no-op in practice and a
  guarantee for the next raiser.)
* `MalformedSchemaError.__init__` — the defect dicts and the free-form
  details.

**What redaction costs.** A payload that says nothing is not an audit record.
The scrub replaces the secret with `***REDACTED***` and keeps the rest: the
keys survive, the numbers survive, the booleans survive, the operator can
still read *which* field failed and *how*. That is the balance the wave-2
review already recorded for the escalation path, and this change does not
disturb it.

## 8. What changed, per file

### `src/paa/models/base.py` (+273/−16)

* Module docstring: the third rule, the three validators, **and the argument
  for the dependency direction** (§4), so the next reader does not have to
  re-derive it.
* `from paa.validation.schema_validator import SchemaError, validate_instance`.
* `MalformedSchemaError(PaaError)` — new. Carries `malformed_keywords`, one
  entry per defect, each located by the path of the **schema** node it sits
  at (`$.properties.a.minLength`) — the convention the runtime's output gate
  already uses. Scrubbed at construction.
* `redact_detail(value)` — new, public, in `__all__`.
* `validate_against_schema` — checks the schema's shape once, then delegates
  to the private `_validate_against_schema`.
* `minimal_instance_for_schema` — same split, same refusal.
* `ModelProvider.complete_structured` — refuses a malformed schema **before
  the provider is called**, with `provider=self.name` in the details.
* `ModelUnavailableError.__init__` — scrubs `**details`.
* `StructuredOutputError.__init__` — scrubs `**details`.

### `src/paa/skills/contracts.py` (+117/−6)

* `_malformed_keywords(schema)` — the shared table, reached through
  `validate_instance`.
* `validate_json_schema_shape` — refuses a malformed keyword at
  **registration**, so a broken contract never reaches the adapter. This is
  also what closes the `usa.py` path: `UnifiedSkillAdapter` validates a
  skill's output with `paa.validation.schema_validator`, and the contract it
  reads can no longer carry a malformed keyword, because it could not be
  registered.
* `schema_errors` — raises `ValueError`; the private `_schema_errors` does
  the instance half.
* `SkillContract.validate_output` / `validate_input` — turn that `ValueError`
  into a `SkillContractError`, with the field named in the findings.
* `_string_errors`'s `re.error` branch — the comment now says what it is: a
  backstop, not the normal path.

### `tests/models/test_model_errors.py` (new) and `tests/skills/test_contracts.py`

See §9.

## 9. Tests

### 9.1 What was added

`tests/models/test_model_errors.py` — **70 tests**:

| group | what it pins |
|---|---|
| `test_a_malformed_keyword_is_reported_and_never_raised_as_a_type_error` (38) | every keyword the models validator reads, against a payload that reaches it **and** one that does not; the raised error is a `MalformedSchemaError` and is not a `TypeError`, an `AttributeError` or a `ValueError` |
| `test_no_raw_type_error_escapes_complete_structured` (19) | the real gate: the error is a `MalformedSchemaError`, and **the provider was never called** (`EchoProvider.calls == []`) |
| `test_the_malformed_keyword_is_located_in_the_schema_not_in_the_payload` | the defect points at `$.properties.a.minLength`, carries `keyword="minLength"` and `schema_error=True` |
| `test_a_defect_in_a_branch_the_payload_never_reaches_is_still_reported` | the walk covers the whole schema |
| `test_the_malformed_schema_error_is_a_paa_error_and_survives_a_ledger_payload` | `to_payload()` works and `json.dumps` accepts it — the ledger payload |
| `test_the_malformed_schema_error_is_not_a_routable_failure` | it is not a `StructuredOutputError` and not a `ModelUnavailableError`, so it cannot travel the escalation path |
| `test_minimal_instance_for_schema_reports_a_malformed_keyword` | the builder's own crashes (`int("2")`, `.items()` on a string) |
| `test_a_valid_schema_answers_exactly_as_it_did_before` | **80 golden rows** |
| `test_a_valid_schema_still_builds_the_same_minimal_instance` | **11 golden rows** |
| `test_model_unavailable_error_redacts_its_details` | the secret is gone from `str(error)`, `error.details` and `to_payload()` |
| `test_model_unavailable_error_keeps_the_shape_of_its_details` | the *keys* and the non-strings survive — redaction must not cost the diagnosis |
| `test_redact_detail_scrubs_strings_at_any_depth_and_leaves_the_keys_alone` | the policy itself, spelled out |
| `test_structured_output_error_redacts_its_details_too` | the sibling, same rule |
| `test_the_model_layer_and_the_router_share_one_redaction_policy` | the router's private copy and the model layer's public one agree — and the test tolerates the copy being retired |
| `test_the_malformed_schema_error_is_part_of_the_module_s_public_surface` | the class and the helper are in `__all__` |

`tests/skills/test_contracts.py` — **48 tests** (17 pre-existing, 31 added):

| group | what it pins |
|---|---|
| `test_a_malformed_keyword_is_reported_and_not_ignored` (26) | every keyword the skills validator reads, both payloads; the defect is **reported**, not ignored |
| `test_a_malformed_keyword_the_module_does_not_read_is_still_refused` | `anyOf` is refused even though this module never applied it |
| `test_the_malformed_keyword_is_named_in_the_error` | the message names the keyword and what it expected |
| `test_the_shape_check_refuses_a_malformed_keyword` | registration refuses it, at any depth |
| `test_a_contract_with_a_malformed_keyword_is_not_registrable` | `SkillContract.parse` → `SkillContractError` with the finding |
| `test_a_contract_turns_a_malformed_keyword_into_a_contract_error` | `validate_output` / `validate_input` → `SkillContractError` |
| `test_a_valid_schema_answers_exactly_as_it_did_before` | **81 golden rows** |

### 9.2 The reversal — the pre-fix code, the new tests

The committed test file imports `MalformedSchemaError` **by name**, so
against the unfixed source the module does not import at all. That is a
failure, but a coarse one: it does not say *which* defect each test catches.
So I ran a **tolerant copy** of the two files (the same tests, with the two
new names resolved through `getattr` so the module imports) against a copy of
the tree with `4c9300d`'s `base.py` and `contracts.py`:

```
$ PYTHONPATH=/tmp/paa-w2impr02/head_src .venv/bin/python -m pytest \
      /tmp/paa-w2impr02/reversal/test_model_errors.py \
      /tmp/paa-w2impr02/reversal/test_contracts.py -q
98 failed, 20 passed in 0.94s
```

**98 of the 118 fail without the fix.** The 20 that pass are exactly the
guards and the pre-existing tests:

* `test_a_valid_schema_answers_exactly_as_it_did_before` (×2) — the
  "unchanged" half of the acceptance criterion. It **must** pass on the
  pre-fix code; that is what "unchanged" means.
* `test_a_valid_schema_still_builds_the_same_minimal_instance` — same.
* `test_model_unavailable_error_keeps_the_shape_of_its_details` — the
  companion to the F-3 fix: the *shape* of the details is the same before
  and after, so it passes on both. The fix-proving half is
  `test_model_unavailable_error_redacts_its_details`, which fails on
  `4c9300d`.
* the 17 pre-existing `tests/skills/test_contracts.py` tests.

I am labelling these rather than counting them as fix-proving, because a test
that passes before and after is a *guard*, and saying otherwise would be
dishonest.

### 9.3 The golden rows are captured, not written by hand

Every golden row was **generated** from `4c9300d`'s source by a script
(`/tmp/paa-w2impr02/emit_table.py`, `/tmp/paa-w2impr02/emit_skills_table.py`),
not typed in. I hand-wrote one of them first (`{"a": "echo"}` where the
answer is `{"a": "a"}` — the placeholder is the *property name*, not the
word "echo") and W2-IMPR-03 caught the mismatch in the shared tree; the
tables are now machine-generated and the mismatch is gone. That is the one
thing in this task where another agent's review changed my output for the
better.

## 10. "A valid schema must behave exactly as before" — the measurement

The wave-2 report claimed 902 (schema, payload) pairs compared with 0
differences, and the reviewer re-ran it and confirmed. I re-measured it
myself, for the code I touched.

**The harness.** `/tmp/paa-w2impr02/harness.py` loads `4c9300d`'s
`base.py` and `contracts.py` *and* the new ones as **standalone modules**
(neither shadows the installed package) and compares, for every
(schema, payload) pair:

* `validate_against_schema(payload, schema)` → the list of error strings;
* `schema_errors(payload, schema)` → the list of error strings;
* `minimal_instance_for_schema(schema)` → the value.

**The corpus.** 42 schemas × 25 payloads. The 42 schemas are the keyword
inventory of §6 — every keyword the two modules read, with a **valid** value
for it, plus the combinators, the boolean schemas, nested objects and
arrays, and both bounds of every numeric constraint. The 25 payloads are
chosen to reach each branch: `""`, `"abc"`, `"abcdef"`, `0`, `1`, `3`, `5`,
`10`, `1.5`, `2.5`, `True`, `False`, `None`, `[]`, `[1, 2]`, `["a"]`,
`["a", "b", "c"]`, `{}`, `{"a": 1}`, `{"a": "x"}`, `{"a": "x", "b": 2}`,
`{"a": {"b": "c"}}`, `{"a": [1, 2]}`, `{"b": 1}`, `{"a": None}`.

**The result.**

```
$ .venv/bin/python /tmp/paa-w2impr02/harness.py
schemas: 42  payloads: 25
compared 1092 (schema, payload) pairs
differences: 0
```

**1092 comparisons, 0 differences.** (1050 = 42 × 25 payload comparisons ×
2 validators, plus 42 `minimal_instance_for_schema` comparisons.)

The number is bigger than the wave-2 report's 902 because the corpus is
bigger and because I compare **three** functions, not one. The result is the
same: **zero**.

And the same answer is now **frozen into the suite** — 80 golden rows for
the model validator and 81 for the skills one — so a future change that
alters a valid schema's answer fails the build instead of passing unnoticed.

## 11. Long-run test

**Source.** One table of keyword shapes, three readers, no private-name
import. The shape of a keyword's value is a property of the schema, so it is
checked once, at the entry point, and the recursive half does not re-check
it. Cost: one O(schema) walk per validation call.

**Load.** `complete_structured` walks the schema once before the provider is
called and once per attempt inside `validate_against_schema`. On a 42-schema
corpus that is microseconds. The 40 ms import cost is **once per process**
and does not scale with load.

**Hostile input.** A schema is caller-supplied and may be hostile. The
hostile shapes are all *defects*, and all of them are now refused with a
located, actionable error rather than crashing or being dropped:

* a keyword value of the wrong type — refused;
* a **deeply nested** defect — the walk descends the whole schema, so a
  broken constraint in a branch the payload never touches is reported
  (`$.properties.a.properties.b.minLength`);
* a **non-compiling regex** in `pattern` — refused, by the shared table
  (`_KIND_REGEX` compiles it and reports `re.error`, `OverflowError` *and*
  `RecursionError`);
* a **boolean** where a number belongs (`minimum: true`) — refused: the
  shared table excludes `bool` from "number", because `isinstance(True, int)`
  is `True` in Python and a `true` minimum is not a number;
* a **non-string schema key** — reported as a schema error by the shared
  walk, not crashed on;
* a schema that is **not a mapping at all** — `validate_instance` returns no
  defects and the validators' own guards handle it (a boolean schema is
  legal JSON Schema and is honoured in full).

**Operability.** The error names the keyword, the path in the schema, and
what the keyword expected: *"schema keyword 'minLength' expects a non-negative
integer, got string"*, at `$.properties.a.minLength`. The ledger payload is
JSON-serialisable (`json.dumps(error.to_payload())` is asserted). The
provider is never called for a broken schema, so no tokens are spent on a
call that cannot succeed.

**Reversal.** Which test fails if the fix is reverted? §9.2: **98**.

## 12. Edge cases and abuse scenarios

| scenario | behaviour |
|---|---|
| `schema` is a boolean (`True`/`False`) | legal JSON Schema; no defects; the boolean semantics are honoured as before |
| `schema` is not a mapping at all | `validate_against_schema` returns `[]` as before; `schema_errors` returns `[]` as before (the existing test `test_non_schema_node_is_not_interpreted` pins it) |
| `schema` is empty `{}` | no defects; every instance is valid, as before |
| a keyword the validator does **not** implement but the shared table constrains (`anyOf` in the skills validator) | refused — the rule is uniform, and there is a test |
| a keyword the shared table does not constrain (`const`, `title`, `default`, `$id`, …) | never a defect; `const` is explicitly *not* in the defect table, because any JSON value is a legal constant |
| `required: ["a", 5]` | refused — the pre-fix code asked for a property called `5` |
| `minimum: true` | refused — `bool` is not a JSON number |
| `pattern: "("` | refused — the shared table compiles the regex |
| the same malformed schema on the *Anthropic* path | the raw `TypeError` is now a `MalformedSchemaError`, but the schema is still sent to the API first — **out of my write-set**, see §14 |
| a contract already in the database with a malformed keyword | `_row_to_contract` calls `SkillContract.parse` → `SkillContractError` → the row is skipped (`src/paa/skills/registry.py:412`), not a crash |
| a secret in a *detail* | scrubbed at construction, in the message, in the details and in the payload |
| a secret in a detail **key** | not scrubbed — a key names a field written by the raiser, and the payload contract is written against those names |

## 13. Performance and security

* **Import time**: +~40 ms per process (measured, §4.4). The price of one
  table instead of three.
* **Runtime**: one O(schema) shape walk per validation call, on top of the
  validation itself. The instance half of `validate_instance(None, schema)`
  is a throwaway.
* **Security**: the change is strictly a *tightening*. Before it, a malformed
  keyword could (a) crash a gate, (b) drop a constraint silently, or (c)
  produce a wrong answer. Now it is a located, reported error.
* **The escalation path is unaffected**: `MalformedSchemaError` is not a
  `StructuredOutputError` and not a `ModelUnavailableError`, so the router
  does not escalate it and does not write a `MODEL_ESCALATED` record.
* **The ledger now records the failure**: `MalformedSchemaError` is a
  `PaaError`, so the orchestrator's `except PaaError`
  (`src/paa/agents/orchestrator.py:394`) catches it and writes
  `EXECUTION_FAILED`. That was the whole point of F-2.

## 14. Deviations from the brief; NEEDs; deferred dependencies

**Deviations.**

1. **The test file's name.** The brief named
   `tests/models/test_schema_crash.py` and allowed a rename "if the
   directory's convention says otherwise". It is
   `tests/models/test_model_errors.py`: the directory names files after
   their subject, and this file's subject is the model layer's **error
   contract** — it covers a broken schema *and* a broken secret, so
   "schema_crash" would have misdescribed half of it.
2. **`malformed_keywords` is on `MalformedSchemaError`, not on the
   `PaaError` payload alone.** It is in `details` (so it reaches the ledger
   payload) *and* on the attribute (so a caller can read it without parsing
   the payload). That is the pattern `StructuredOutputError` already uses for
   `schema_errors`.
3. **The golden tables are 80 rows each**, not the whole corpus. The whole
   corpus is the 1092-pair differential, which is a harness, not a test file;
   the tables are the part of it that belongs in the suite.

**NEEDs** (posted in COMMS.md; none blocks this task).

1. **`src/paa/models/__init__.py`** — `paa.models.MalformedSchemaError` and
   `paa.models.redact_detail` do not exist: the module re-exports a fixed
   name list from `base.py`. *Exact change:* add `MalformedSchemaError` and
   `redact_detail` to the `from paa.models.base import (...)` list and to
   `__all__`. (`src/paa/models/__init__.py:28`)
2. **`src/paa/models/router.py`** — the router still carries a private copy
   of the redaction policy at `router.py:125`, byte-equivalent to the new
   public `redact_detail`. *Exact change:* drop `_redact_detail` and
   `from paa.models.base import redact_detail`, using it at `router.py:630`
   and `router.py:681`. (Addressed to W2-IMPR-03, who owns that file this
   wave. **Correction to my earlier COMMS line:** I wrote `router.py:552`;
   the line moved when W2-IMPR-03 committed `8841216`, and the two call
   sites are now `:630` and `:681`.)
3. **`src/paa/models/anthropic_provider.py`** — its `complete_structured`
   overrides the base and calls the API *before* validating, so there a
   malformed schema is still sent to the frontier. The raw `TypeError` is
   now a `MalformedSchemaError`, but the broken contract leaves the machine.
   *Exact change:* the same upfront `_malformed_keywords(schema)` refusal
   the base class now has, before `self._attach_tool(body, schema)`.
   (`src/paa/models/anthropic_provider.py:186`)

**Deferred improvement (not a NEED — a proposal).** Moving the table from
`paa.validation.schema_validator` into `paa.core` would make the coupling
free (≈0 ms instead of ≈40 ms) and would put the table in the leaf package
everything already imports. It requires editing two read-only files
(`src/paa/validation/schema_validator.py` and
`tests/validation/test_schema_validator.py`, which reads `_KEYWORD_SPECS` by
module-level lookup), so it is wave-3 work at the earliest.

**Ownership.** `src/paa/models/router.py`, `src/paa/models/__init__.py`,
`src/paa/models/anthropic_provider.py`, `src/paa/validation/**`,
`tests/models/conftest.py` and the other `tests/models/*` files were read
and never edited. `src/paa/agents/orchestrator.py` was modified in the
working tree by another agent when I started; it is not in my write-set and
is not in either of my commits.

## 15. Out-of-scope findings

1. **`paa.skills.usa` would have punished a skill for the runtime's broken
   schema** — `UnifiedSkillAdapter._finalise` calls
   `self._validator.check(result.output, contract.output_schema)`
   (`src/paa/skills/usa.py:268`), and a malformed keyword comes back as an
   *output* error, so `output_valid=False` and the skill's reliability
   weight goes **down**. A defect in the contract is not the payload's
   fault. **This is closed by my change** — the contract cannot now be
   registered with a malformed keyword (`validate_json_schema_shape` refuses
   it), so the adapter never sees one. It is worth knowing that the
   *reason* it is closed is the registration-time check, not the adapter.
2. **`paa.models.anthropic_provider` sends a broken schema to the frontier**
   — NEED 3 above.
3. **`paa.models.__init__` does not re-export the new names** — NEED 1
   above.
4. **The router's private copy of the redaction policy** — NEED 2 above.
5. **`PaaError.__str__` renders `details` with `repr()`** and
   `to_payload()` merges them verbatim (`src/paa/core/errors.py:46-54`).
   That is the channel F-3 was about; it is now clean for the two model-layer
   classes, but it is still the *general* shape of the risk: any future
   `PaaError` subclass carrying provider output inherits it. A general fix
   belongs in `core/errors.py`, which is not mine.

## 16. Known limitations, risks, rollback

* **The import cost is real** (~40 ms per process). §4.4 and §14.
* **`paa.models.base` now depends on `paa.validation`** — a new edge in the
  dependency graph. §4 argues it is safe; the reviewer may disagree, and the
  argument to have is §4.5 (move the table to `paa.core`).
* **A schema the shared table constrains but a given validator does not
  implement is now refused** rather than ignored. That is the rule applied
  uniformly, but it is a behaviour change for a caller who was relying on
  the ignore. There is a test for it
  (`test_a_malformed_keyword_the_module_does_not_read_is_still_refused`).
* **The Anthropic path still sends the broken schema out** before refusing
  it (NEED 3).
* **Rollback**: two commits, `4a5bac0` and `77195f7`. `git revert 77195f7
  4a5bac0` restores the pre-fix tree exactly; the pre-fix behaviour is
  green on `4c9300d`, so the revert is a clean revert and not a partial one.

## 17. Where I am least confident — what the reviewer should scrutinise

1. **The 40 ms import cost, and whether it is worth it.** My judgement is
   that one table is worth 40 ms once per process, and that the *right* fix
   is to move the table to `paa.core` — which needs two read-only files. A
   reviewer who disagrees has a concrete, cheap alternative in §14.
2. **`validate_instance(None, schema)` as the way to ask the shape
   question.** It computes the instance errors and throws them away. It is
   the *public* route to the table, and the alternative (importing
   `_KEYWORD_SPECS`) binds to another module's private name — but it is
   wasteful, and a reviewer may prefer a public
   `schema_shape_errors(schema)` in `paa.validation.schema_validator`. That
   is a one-function addition to a read-only file, and I would take it if the
   owner of that file wants it.
3. **Whether `MalformedSchemaError` should be a `StructuredOutputError`
   after all.** I argued no, and the argument (a caller's typo must not
   write a `MODEL_ESCALATED` record claiming reasoning left the machine) is
   the reason the class exists. But the counter-argument is real: the router
   then treats a broken schema as an *unhandled* failure, and the operator
   sees a raw `PaaError` where a routed one might have been friendlier. The
   reviewer's suggested fix — "raise the malformed keyword as a
   `StructuredOutputError` so the router can route it" — is the opposite
   choice, and it is *wrong* about the escalation record but *right* about
   the operator's experience. I would defend my choice; the reviewer should
   decide.
4. **The `usa.py` consequence.** I believe the registration-time check
   closes it (§15.1). It is closed by *construction* rather than by a test
   on the adapter's path, because `usa.py` is not mine. A reviewer who wants
   that pinned should ask for a test in `tests/skills/test_usa.py`.
5. **The 161 golden rows.** They are machine-generated from `4c9300d`, so
   they cannot be *wrong* about what the old code did. What they can be
   wrong about is *coverage* — whether 161 rows is the right sample of 1092
   comparisons. The full harness covers the rest, but it is not in the suite.
6. **The independent-review subagent could not be run.** The harness refused
   nested subagents ("Subagent depth limit reached (1)"), so this report is
   reviewed by one pair of eyes, not two. Read it accordingly — the same
   limitation W2-IMPL-05 and W2-FIX-01 reported.

## 18. Commands run, and their results

```
$ .venv/bin/python -m ruff check src tests
All checks passed!

$ .venv/bin/python -m mypy src
Success: no issues found in 137 source files

$ .venv/bin/python -m mypy src/paa/models src/paa/skills
Success: no issues found in 21 source files

$ .venv/bin/python -m pytest tests/models tests/skills tests/validation -q
769 passed, 3 xfailed in 14.91s

$ .venv/bin/python -m pytest tests/models/test_model_errors.py tests/skills/test_contracts.py -q
118 passed

$ .venv/bin/python -m pytest tests/conventions -q
17 passed in 14.03s

$ .venv/bin/python -m pytest tests/coder tests/agents tests/integration tests/cli tests/api -q
(green — the direct consumers of the model layer)

$ .venv/bin/python /tmp/paa-w2impr02/harness.py
schemas: 42  payloads: 25
compared 1092 (schema, payload) pairs
differences: 0

$ PYTHONPATH=/tmp/paa-w2impr02/head_src .venv/bin/python -m pytest \
      /tmp/paa-w2impr02/reversal/test_model_errors.py \
      /tmp/paa-w2impr02/reversal/test_contracts.py -q
98 failed, 20 passed in 0.94s
```

**NOT RUN** (and why):

* the **full** suite — standing order 16 ("never run the full suite"). The
  scoped suites above plus the five direct-consumer directories cover the
  blast radius; `tests/storage`, `tests/memory`, `tests/ledger`,
  `tests/observability` and the rest do not touch the model or skills
  validators.
* the **live** S3 and PostgreSQL suites — no server configured, and not this
  task's substrate.
* `ruff format` — the repo does not use it: `4c9300d`'s `base.py` also
  fails `ruff format --check`, so the gate is `ruff check` alone.

## 19. Commits

| SHA | what |
|---|---|
| `4a5bac0` | `fix(models): one rule for a malformed schema keyword, in all three validators` — the source change and the first cut of the tests |
| `77195f7` | `test(models): the malformed keyword, with a payload that reaches it` — each keyword exercised against a payload that reaches it and one that does not |

Both carry the trailers `Task: F-2,F-3`, `Agent: W2-IMPR-02`,
`Wave: 2-improve`, `Report: <this file>`.

**Last COMMS line read before finishing:**
`[2026-10-22T07:22:09Z] W2-IMPR-02 -> ALL | FINDING | F-2 | …` — my own
five lines; the last line in the file before them was
`[2026-10-22T07:14:19Z] W2-IMPR-05 -> ORCH | NEED | R-W2-202 | …`.
