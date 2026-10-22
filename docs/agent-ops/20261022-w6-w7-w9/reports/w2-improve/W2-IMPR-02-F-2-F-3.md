<!-- ORCH-DIGEST:START -->
agent: W2-IMPR-02 | scope: F-2 (MAJOR — the malformed-keyword rule reached one of three JSON-Schema validators) and F-3 (MINOR — the redaction rule reached one of two sibling errors) | status: DONE
findings: FIXED 2 (F-2, F-3) | REJECTED 0 | DEFERRED 1 (the shared table cannot move to paa/core without an owner for src/paa/core/** and src/paa/validation/**)
code_commits: 4a5bac0 (the fix, inherited from the prior dispatch and audited), 77195f7 (the strengthened tests, inherited), d484c04 (this dispatch: the whole shared table, and the second complete_structured) | report_commit: (this commit)
tests_added_or_strengthened: 147 cases across the two files (99 in tests/models/test_model_errors.py, 48 in tests/skills/test_contracts.py); 126 of them fail against the pre-fix source (95 + 31), measured
ownership_violations: none — src/paa/models/router.py, src/paa/models/__init__.py, src/paa/models/anthropic_provider.py and src/paa/validation/** were read, never edited; four NEEDs posted for them
claims_audited: 9 from the prior dispatch's report; 7 confirmed, 1 confirmed-with-a-different-number (+40 ms import → measured +26.4 ms min / +35.1 ms median, and 16 new modules, which the prior report did not state), 1 not reproducible (the 1092-pair harness was lost with the host; rebuilt and re-measured at 2496 pairs, 0 differences)
gaps_found_and_closed_by_this_dispatch: 2 — the keyword inventory in the model suite covered the 16 keywords the model validator *reads*, not the 27 the shared table constrains; and complete_structured has two implementations, and the rule reached one of them
confidence: high on F-2 and F-3 (the rule is one rule in one table, the reversal is measured against the pre-fix source, and a valid schema's answers are byte-identical); medium on the import direction, whose cost is real and is now measured rather than asserted
corrections (2026-10-22, W2-FIX-03): 2 — the import figure quoted in the digest and in §4.2 measured the same edge the record now quotes as "about 20 ms" (re-measured once, 60 interleaved pairs: +20.6 ms median of the pairwise differences); and §3.3's table carried no counts, so a reader counting the names in its "silently ignored" row got 20 where the matrix says 19 ignored + 1 wrong answer (6 TypeError + 1 AttributeError + 19 + 1 = 27). The "24 silently dropped keywords" summary this was reported under is in briefs/W2-REV-03.md, not in this report
<!-- ORCH-DIGEST:END -->

# Improvement report — W2-IMPR-02 (F-2, F-3)

**Agent:** W2-IMPR-02 (fixer, wave 2 improve) — **third dispatch**.
**Findings:** F-2 (MAJOR), `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-01-code.md:94`;
F-3 (MINOR), same file, `:147`.
**Write-set:** `src/paa/models/base.py`, `src/paa/skills/contracts.py`,
`tests/models/test_model_errors.py` (new), `tests/skills/test_contracts.py`.
**Branch:** `ops/20261022-w6-w7-w9` [VERIFIED `git branch --show-current` → `ops/20261022-w6-w7-w9`].
**Last COMMS line read at start:** `[2026-10-22T07:35:26Z] W2-IMPR-02 -> ORCH | DONE | W2-IMPR-02,F-2,F-3`
(`docs/agent-ops/20261022-w6-w7-w9/COMMS.md:136`) — the prior dispatch's own
DONE line. **The second dispatch's DONE line was already in the log when this
dispatch began**, which is the single most important fact about this report.

---

## 0. The short version, and the thing the orchestrator needs to know first

The tree was **not** in the state my brief described. The brief says the
interrupted dispatch "left uncommitted work in your write-set"
(`src/paa/models/base.py` +224/−16 and `src/paa/skills/contracts.py` +91/−6).
It was not uncommitted: `4a5bac0` had the fix, `77195f7` had the strengthened
tests and `01c1484` had an 878-line report, and the COMMS log carried the
prior dispatch's own `DONE` line at `07:35:26Z` [VERIFIED `COMMS.md:136`].
The drafts in `/tmp/paa-w2-improve-backup/` are the *interrupted* dispatch's
files; the commits are a **later** state of the same work.

So this dispatch had a different job from the one the brief describes. The
brief's instruction still governs — *"read the draft, judge it on its merits,
keep what is right, change what is wrong, and say in your report what you
kept, what you changed and why, and what you discarded"* — and the job
became: **audit a completed fix and its report, independently, and finish the
parts that are not finished.**

What I found:

* **The fix is sound.** F-2 and F-3 are genuinely closed, the shared table is
  the right design, and the import direction is safe. I re-measured every
  number the prior dispatch reported and the results hold, with one
  correction (§4).
* **Two gaps were open, and I closed them** (§5): the keyword inventory in
  the model suite covered the **16** keywords the model validator *reads*,
  not the **27** the shared table constrains — so nothing pinned the
  consistency the whole fix rests on; and `complete_structured` has **two**
  implementations, and the rule had reached one of them.
* **Three NEEDs from the prior dispatch are still outstanding** (§8). The
  Anthropic one was posted as a `FINDING`, not a `NEED`, so it had no owner;
  it now has one.
* **The one design cost is real and was under-reported**: the new import
  edge adds **16 modules** to the model layer's import graph — the whole
  validation package *and* the whole sandbox package — for **+26.4 ms**
  (min). The prior report said "+40 ms" and did not mention the modules
  (§4.2).

**The rule now holds in all three validators, a valid schema answers
exactly as it did before (2496 (schema, payload) pairs compared, **0
differences**), and 126 of the 147 test cases fail without the fix.**

---

## 1. What the draft was, and what I kept

The interrupted dispatch's draft introduced a `MalformedSchemaError` and made
the model layer import the validation layer's keyword table by its **private**
name (`paa.validation.schema_validator._KEYWORD_SPECS`). That draft was
never committed as such; the committed version differs from it, and the
committed version is what I audited.

### 1.1 Kept, unchanged, because it is right

| What | Where | Why it is right |
|---|---|---|
| `MalformedSchemaError` as a `PaaError` | `src/paa/models/base.py:187` | The orchestrator's `except PaaError` did not catch a `TypeError`, so no `EXECUTION_FAILED` was written and the run died unrecorded. Being a `PaaError` is what makes the run record it. |
| `MalformedSchemaError` **not** a `StructuredOutputError` | `src/paa/models/base.py:199-206` | `StructuredOutputError` means "the provider answered, and the answer was wrong"; the router escalates on it, and the `MODEL_ESCALATED` record would then say reasoning material left the machine. A malformed schema is the *caller's* mistake and no better model can repair it. [VERIFIED `src/paa/models/router.py:500` escalates only on `StructuredOutputError` and `:502` on `ModelUnavailableError`; `MalformedSchemaError` is neither, so it propagates.] |
| The **shared table**, read through a public entry point | `src/paa/models/base.py:76`, `:334-359` | One definition of "malformed" for three validators, so they cannot drift. The public entry point (`validate_instance`) rather than the private `_KEYWORD_SPECS` — reaching into another module's private name binds this module to that module's internals. |
| The malformed keyword is reported at the **schema node** | `src/paa/models/base.py:326` | A schema error that points at the payload sends the author to the wrong place. `$.properties.a.minLength`. |
| The whole schema is walked, not only the branch the payload reaches | `src/paa/models/base.py:354` | A broken constraint in an untouched branch is still a broken contract. |
| `skills/contracts.py` refuses with a **`ValueError`** | `src/paa/skills/contracts.py:205-208` | The module already refuses a bad schema *shape* with a `ValueError`; the caller (`SkillContract.validate_output`, `:537`) turns it into a `SkillContractError`. Following the existing convention rather than inventing a second one is what the brief asked for. |
| The registration-time shape check refuses it too | `src/paa/skills/contracts.py:152-156` | A contract that parses is a contract every downstream layer may rely on; a broken one never reaches the adapter. |
| `ModelUnavailableError` scrubs `**details` at construction | `src/paa/models/base.py:128-134` | The reason is identical to its sibling's: the details are built out of the provider's own output, and `PaaError.to_payload()` merges them into a ledger payload verbatim. |
| The free-form policy written down once | `src/paa/models/base.py:275-308` (`redact_detail`) | "What *redacted* means for a free-form mapping" had to be decided and stated: every **string value** is scrubbed at any depth; the **keys** are not (they name a field and callers are written against them); a non-string, non-container value passes through unchanged so the details stay JSON-serialisable for the ledger. |

### 1.2 Changed by the prior dispatch (audited, kept)

The prior dispatch changed three things in the interrupted draft, and each is
right:

1. **The private import became a public entry point.** The draft reached into
   `paa.validation.schema_validator._KEYWORD_SPECS`. The committed code calls
   `validate_instance(None, schema)` and keeps the errors marked
   `schema_error` [VERIFIED `src/paa/models/base.py:359`]. The cost of the
   public route is that the instance half of the call is thrown away; the
   benefit is that the module binds to a published contract, and that the
   *same* function answers for all three validators.
2. **The docstrings that claimed a private import were corrected** — the
   committed docstrings say "through its public entry point", which is true.
   A docstring that describes an import the code does not make is worse than
   no docstring, because a reader checks the code and finds a lie.
3. **The details scrub was added to the sibling class** — the draft had it
   only on `StructuredOutputError`.

### 1.3 Discarded

Nothing in the committed fix was discarded. Two things in the *draft* were
discarded by the prior dispatch and I agree with both discards:

* the private-name import (above);
* the draft's framing of the model-layer error as a `StructuredOutputError`
  subclass, which would have put a caller's broken schema on the escalation
  path.

### 1.4 Changed by **this** dispatch

Only tests. See §5. The source files are byte-identical to `4a5bac0`:
[VERIFIED `git diff 4a5bac0..d484c04 -- src/paa/models/base.py src/paa/skills/contracts.py` → empty; the other three source files that differ across that range belong to other agents].

---

## 2. The rule, stated once, and where it lives

> A malformed keyword value is a **schema error**: reported to the caller,
> never raised as a `TypeError`, never silently ignored.

Three validators, three conventions for *saying* it, one verdict:

| Validator | Says it as | Where |
|---|---|---|
| the runtime's output gate | a `SchemaError` with `schema_error=True`, located at the schema node | `src/paa/validation/schema_validator.py:449` |
| the model layer | `MalformedSchemaError` (a `PaaError`) | `src/paa/models/base.py:187` |
| the skills layer | a `ValueError` → `SkillContractError` | `src/paa/skills/contracts.py:205` |

The keyword shapes live in one place — `paa.validation.schema_validator`'s
`_KEYWORD_SPECS` — and all three read them through its public entry point.

---

## 3. The measurement the brief asked for: a valid schema behaves exactly as before

### 3.1 The differential, rebuilt and re-run

The prior dispatch's harness (`/tmp/opencode/w2impl02/diff_valid.py`) was
lost with the host [VERIFIED `/tmp/opencode/` contains only `paa-stack-data`].
I rebuilt one from scratch (`/tmp/paa-w2impr02/harness.py`), which loads the
**pre-fix** and the **post-fix** modules *in the same process* and compares
their answers:

* corpus: **48 valid schemas** × **26 payloads**, spanning every keyword the
  model validator reads and every keyword the shared table constrains,
  including the boolean schemas, the unknown keywords (`$ref`,
  `patternProperties`, `if`), the annotation keywords, `$defs`/`definitions`,
  the three combinators, and the numeric bounds;
* three APIs compared: `validate_against_schema`, `minimal_instance_for_schema`
  and `paa.skills.contracts.schema_errors`;
* a schema is *valid* iff the shared table reports no defect for it — which
  is checked, not assumed.

**Result: 2496 (schema, payload) pairs compared, 0 differences**, plus 48
schema-level comparisons of `minimal_instance_for_schema`, also 0
differences.
[RAN `/tmp/paa-w2impr02/harness.py diff` → `{"pairs": 2496, "schemas": 48}`,
`total differences: 0`]

The prior dispatch reported **1092 pairs, 0 differences**. Its harness is
gone and its number is not reproducible; mine is a different, larger corpus
and reaches the same verdict. I am not claiming the prior number was wrong
— I am saying it cannot be re-run, which is why I rebuilt it.

### 3.2 The frozen golden rows, checked against the pre-fix source

The suite freezes 80 model rows, 11 minimal-instance rows and 81 skills rows
("what the validators answered *before* the change"). A frozen row is only
worth something if it is what the **old** code answered, so I checked all
**172** of them against the pre-fix modules loaded by path:

**172 rows checked, 0 that do not match the pre-fix source.**
[RAN `/tmp/paa-w2impr02/verify_golden.py` → `model golden rows: 80 model
minimal rows: 11 skills rows: 81` / `rows that do NOT match the pre-fix
source: 0`]

### 3.3 What *did* change, and it is exactly the finding

The set of schemas whose answer changed is the set of schemas with a
malformed keyword — and nothing else. The matrix below is the whole
constrained table, one row per malformed value, with an instance that
reaches the keyword.

**Before the fix** (measured on the pre-fix source, both validators):

| Behaviour | Count | Keywords |
|---|---|---|
| raw `TypeError` escaped the model validator | 6 | `minLength`, `maxLength`, `minItems`, `maxItems`, `minimum`, `maximum` |
| raw `AttributeError` escaped the model validator | 1 | `properties` (a string or a list) |
| **silently ignored** — the constraint was *dropped* and the instance came back "valid" | 19 | `type`, `required`, `additionalProperties`, `items`, `prefixItems`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `pattern`, `uniqueItems`, `minProperties`, `maxProperties`, `anyOf`, `oneOf`, `allOf`, `not`, `nullable`, `definitions`, `$defs` |
| a **wrong** answer rather than a crash | 1 | `enum: "abc"` → a substring test |

**6 + 1 + 19 + 1 = 27**, one row for each constrained keyword — and the
counts are stated here so that a reader cannot get them from counting names.
(A malformed *value* can behave in more than one way, which is why the
"wrong answer" row used to list `required: [1, 2]` and `type: 5` beside
`enum: "abc"`: those keywords are dropped when the value is malformed in one
way and answered wrongly when it is malformed in another. The **27-keyword
matrix** measured one malformed value per keyword, and there `enum` is the
one that was answered wrongly — so the matrix reads 19 dropped, not 20.)

**After the fix**: every one of the **27** constrained keywords is
**reported** — `MalformedSchemaError` in the model layer, `ValueError` in the
skills layer — in all **32** malformed cases I exercised, and the shared
table reports a defect for all 32.
[RAN `/tmp/paa-w2impr02/harness.py matrix` → 32 rows, 27 distinct keywords,
`rows with NO shared-table defect: none`]

Two behaviours worth naming, because they are *not* "unchanged":

* `minLength: -1` and `multipleOf: 0` are now schema errors everywhere. That
  is wave 2's T-005 policy applied uniformly
  (`src/paa/validation/schema_validator.py:128-132`: the ranges JSON Schema
  itself imposes are part of the shape), and the model layer now agrees with
  the gate instead of applying a constraint that can never fire.
* A schema carrying a keyword the model validator does not **read**
  (`pattern`, `multipleOf`, `uniqueItems`, …) is refused if that keyword's
  value is malformed, even though the model layer would never have applied
  it. That is the cost of one table, and it is the point of one table: the
  rule is the same in all three validators. §5.1 pins it.

---

## 4. The audit of the prior dispatch's claims

The wave-1 precedent says an interrupted dispatch's claims are suspect, and
the brief says to treat the draft as untrusted input. The same applies to
the *completed* dispatch I found. Nine claims, checked:

| # | Claim (prior report / COMMS `DONE`) | Verdict | Evidence |
|---|---|---|---|
| 1 | A malformed keyword is a schema error in all three validators | **confirmed** | §3.3 matrix; 27/27 keywords reported by all three. |
| 2 | `MalformedSchemaError` is a `PaaError` and not a `StructuredOutputError` | **confirmed** | `src/paa/models/base.py:187`, `:229`; the router escalates only on `StructuredOutputError`/`ModelUnavailableError` (`router.py:500,502`). |
| 3 | `ModelUnavailableError`'s details are scrubbed at construction | **confirmed** | Measured: the credential `sk-abcdef0123456789abcdef` survives into `error.details` and `to_payload()` on the pre-fix source, and is `***REDACTED***` after. §4.1. |
| 4 | The shared table is read through the **public** entry point, not the private name | **confirmed** | `src/paa/models/base.py:76`, `:359`. |
| 5 | "No cycle, no optional extra" | **confirmed** | Nothing under `paa/validation/` imports `paa.models` [VERIFIED by grep over `src/paa/validation/`]; nothing in the 14 modules the edge pulls in imports `docker`, `kuzu`, `qdrant`, `sentence-transformers`, `redis`, `psycopg` or `boto3`; the lock's fresh-interpreter sweep is green (§6). |
| 6 | "98 of 118 new tests fail against `4c9300d`" | **confirmed, and extended** | I measured **126 of 147** against the immediate pre-fix state (`4a5bac0^`). 126 − my **28** new *failing* cases = **98**, which matches exactly — the prior number is right, and its baseline (`4c9300d`) is two commits earlier than mine (`4a5bac0^`). (My 29th new case is the strict xfail, which the pre-fix tree reports as *xfail*, not *failed*.) §7. |
| 7 | "1092 (schema, payload) pairs compared with 0 differences" | **not reproducible**; re-measured at **2496 pairs, 0 differences** | The harness was lost with the host. §3.1. |
| 8 | "the shared table … **+40ms measured**" | **directionally right, under-specified, and the number is not reproducible** | §4.2. |
| 9 | Three NEEDs posted | **confirmed; all three still outstanding**, and the Anthropic one was filed as a `FINDING`, not a `NEED` | §8. |

### 4.1 F-3, measured rather than asserted

The pre-fix source, one construction, the same credential:

```
old: details["body_excerpt"] = "auth failed: key=sk-abcdef0123456789abcdef"
     details["headers"]      = "{'x-key': 'sk-abcdef0123456789abcdef'}"
     to_payload() contains the key                          -> leak: True
new: details["body_excerpt"] = "auth failed: key=***REDACTED***"
     details["headers"]      = "{'x-key': '***REDACTED***'}"
     to_payload() contains no key                           -> leak: False
```

`status_code: 401`, `attempts: 2` and `provider` pass through unchanged, and
the **keys** are unchanged — the policy in `redact_detail` is applied
consistently, and the operator keeps the diagnosis.

### 4.2 The import cost — the prior report's "+40 ms" and the fact it missed

The prior report claimed the new edge "costs about 40 ms of import time,
measured", and said the edge is safe because nothing under `paa.validation`
imports `paa.models` and nothing under it imports an optional extra. Both
halves are right; the number is not reproducible and it omits the more
useful measurement.

**The module delta** [VERIFIED by diffing `sys.modules` before and after
`import paa.models.base`, in the two trees]:

| | pre-fix | post-fix |
|---|---|---|
| new `paa.*` modules pulled in | 16 | 32 |

The **16** new ones are `paa.validation` (+5 submodules) and the **whole
sandbox package** (+9 submodules: `docker_backend`, `dryrun_backend`,
`recursion`, `subprocess_backend`, `watchdog`, `wsl_backend`, `_win32`,
`base`). The sandbox arrives because `paa/validation/__init__.py` imports
`paa.validation.engine`, which imports `paa.sandbox.base` — and importing
the *submodule* runs the *package* `__init__`, which imports every backend.
[VERIFIED `src/paa/validation/__init__.py:28`, `src/paa/validation/engine.py:46`,
`src/paa/sandbox/__init__.py:29-41`]

**The time delta** [11 runs per tree, fresh interpreters, minimum taken]:

| import | before (min) | after (min) | delta |
|---|---|---|---|
| `paa.models.base` | 216.65 ms | 243.02 ms | **+26.4 ms** |
| `paa.models` | 221.96 ms | 248.71 ms | **+26.8 ms** |

(medians: 223.9 → 259.0 ms, +35.1 ms). The prior report's "about 40 ms" is
the right order of magnitude and is not reproducible on this host; the
module delta is the number that matters, and it was not stated.

> **Corrected 2026-10-22, by W2-FIX-03.** These two figures measured **this
> same edge** — the model layer reading the keyword table through
> `paa.validation.schema_validator` — on an earlier host, and they are not
> the edge's figure of record any more. The edge was re-measured once, on the
> wave-2 host, over **60 interleaved pairs of fresh interpreters** (the
> pre-move tree against the current one): **+20.6 ms** median of the pairwise
> differences, **+20.4 ms** as the difference of the two medians. The figure
> of record is therefore **about 20 ms**, and it is quoted in exactly three
> places — `src/paa/core/json_schema.py` (which carries the method),
> `src/paa/models/base.py` and the import lock's own docstring — all three of
> which the lock now checks to agree. The table above is left as it was
> measured: it is a record of what this dispatch saw on that host, and
> rewriting a historical measurement would be worse than superseding it.

The cost is paid once per process and `paa.cli.main` does **not** import
`paa.models` [VERIFIED: `src/paa/cli/main.py` imports `paa.sandbox.base`
directly, and `grep -rn "paa.models" src/paa/cli/` finds only a docstring],
so `paa --version` is unaffected.

---

## 5. The two gaps this dispatch found and closed

### 5.1 The keyword inventory was a *subset*, and the subset is what drifts

`tests/models/test_model_errors.py`'s `MALFORMED_KEYWORDS` enumerated the
**16** keywords `validate_against_schema` *reads*
(`src/paa/models/base.py:541-545` documents that set). The shared table
constrains **27**. The model layer refuses a malformed value for all 27, and
**nothing pinned that**.

That is precisely the failure mode the finding is about: "the rule reached
one of three validators" is a statement about *coverage*, and a test that
enumerates the keywords one implementation happens to read cannot hold a
coverage claim. A later "tighten the check to the keywords we actually use"
would have looked like a refinement, and would have broken the consistency
the fix exists to create.

The new test asks **all three validators at once, for every constrained
keyword** — the output gate reports it, the model layer raises
`MalformedSchemaError`, the skills layer raises `ValueError`:
[VERIFIED `tests/models/test_model_errors.py:284-372`]
`test_all_three_validators_refuse_the_same_malformed_keyword`, 27 cases.

It fails without the fix for two of the three validators (the third was
already fixed by wave 2), which is the shape the finding asks for.

### 5.2 `complete_structured` has two implementations, and the rule reached one

`ModelProvider.complete_structured` refuses a malformed schema **before** the
provider is called (`src/paa/models/base.py:884-889`). But
`AnthropicProvider.complete_structured` **overrides** it
(`src/paa/models/anthropic_provider.py:152`) and calls
`validate_against_schema` itself, at `:186` — after `_send`.

Measured, with the transport stubbed:

```
base:      raised MalformedSchemaError | transport calls made: 0
anthropic: raised MalformedSchemaError | transport calls made: 1
```

Two things follow, and they are different in kind:

* **F-2's literal criterion is met on that path too**: the raw `TypeError` is
  now a `MalformedSchemaError`. The reviewer's end-to-end probe ("provider
  raised: TypeError … router raised: TypeError") no longer reproduces. That
  was untested, and it is now: `test_the_anthropic_path_also_refuses_a_malformed_schema`.
* **A gap remains**: the Anthropic path still asks the frontier model about
  a broken schema, so the broken contract **leaves the machine** — the exact
  thing the base implementation's docstring says must not happen
  (`src/paa/models/base.py:878-882`). The prior dispatch found this and
  posted it as a `FINDING` (`COMMS.md:126`), which gave it no owner. It is
  now a `NEED` (§8) **and** a `strict=True` xfail, so the suite goes **red**
  the day the override refuses pre-flight and the marker cannot outlive the
  fix — the convention this repo already uses for a known defect in a
  read-only file (`tests/validation/test_ast_scanner.py:449`).

---

## 6. The import direction — the question the brief asks me to answer

**Is `paa.models` → `paa.validation` safe? My answer: yes, and it is the
right design — with one cost I would remove if I owned the files, and I have
posted the NEED that would.**

The argument, in the order the brief asks for it:

**1. Against the repo's import conventions.** The lock at
`tests/conventions/test_import_conventions.py` is about *how a contract is
resolved* (runtime, never behind `if TYPE_CHECKING:`), not about *which
subsystem may depend on which*. The new edge satisfies the convention as
written: the table is a contract, and it is resolved at runtime, at module
scope, in all three consumers. The lock's own sweep — every module under
`src/paa` imported in a fresh interpreter, which is the only check that can
see a cycle — is green: **17 passed**
[RAN `.venv/bin/python -m pytest tests/conventions -q` → `17 passed`].

**2. Against the AGENTS.md rule.** The rule is: *"never import one at module
scope in a core path"* — and the "one" is a **heavy or optional** dependency
(the sentence names qdrant, kuzu, sentence-transformers, redis, psycopg,
boto3). `paa.validation` is neither: it is first-party, pure-Python, and
imports only the standard library, `pydantic`, `structlog` and
`paa.core.*`/`paa.sandbox.base` — of which none is an extra. I read the
import lines of all 14 modules the edge pulls in [VERIFIED by grep over
`src/paa/validation/**` and `src/paa/sandbox/**`]. So the rule is not
violated.

**3. The dependency *kind* is not new.** `paa.skills.contracts` and
`paa.skills.usa` already import `paa.validation.schema_validator`
[VERIFIED `src/paa/skills/contracts.py:45`, `src/paa/skills/usa.py:48`], and
`paa.validation` is the *deterministic, host-side* layer — the one the
runtime treats as a security decision rather than an opinion
(`src/paa/validation/__init__.py:1-13`). A validator reading a validator's
table is a dependency from a higher layer to a lower one, which is the
direction the tree already runs in.

**4. The direction is acyclic.** Nothing under `paa/validation/` imports
`paa.models` [VERIFIED by grep over `src/paa/validation/` → no match]. The
edge cannot close a cycle that does not exist, and the fresh-interpreter
sweep would fail loudly if it did.

**5. The alternative I would defend, and why I did not do it.** The
alternative is to move the keyword table out of the `paa.validation`
*package* into a module both subsystems already depend on —
`paa/core/json_schema.py`, say — and have
`paa/validation/schema_validator.py` import it from there. That removes the
sandbox from the model's import graph entirely (the `paa.core` package
imports nothing heavy) and puts the table in the layer that is *already*
below both consumers. It is the design I would defend on import-cost
grounds.

**Why I did not do it:** it needs edits to `src/paa/core/**` **and**
`src/paa/validation/schema_validator.py`, and both are outside this task's
write-set — `src/paa/validation/**` is explicitly read-only, and
`src/paa/core/**` is explicitly forbidden. So it is a `NEED` with the exact
change (§8.1), and this dispatch reports **DEFERRED-DEPENDENCY** for it.

**What I would *not* accept as the alternative:** a second copy of the
keyword table in the model layer, or a copy in the skills layer. That is
what the review rejected ("a fix for the one keyword that was found is not a
fix") and it is exactly the drift the repo's culture forbids: three tables
that agree today and diverge on the next keyword.

---

## 7. The reversal — which test fails if the fix is reverted

Measured on **this** tree, against the pre-fix source, with `PYTHONPATH`
pointing at a copy of `src/paa` whose `models/base.py` and
`skills/contracts.py` are the pre-fix files:

| Suite | cases | **fail without the fix** |
|---|---|---|
| `tests/models/test_model_errors.py` | 99 | **95** |
| `tests/skills/test_contracts.py` | 48 | **31** |
| **total** | **147** | **126** |

[RAN `PYTHONPATH=/tmp/paa-w2impr02/revcompat .venv/bin/python -m pytest
tests/models/test_model_errors.py -q` → `exit=1`, 95 `FAILED` lines, 14
distinct failing test functions;
`PYTHONPATH=/tmp/paa-w2impr02/rev .venv/bin/python -m pytest
tests/skills/test_contracts.py -q` → `exit=1`, 31 `FAILED`]

Two details about the method, because they decide whether the number means
anything:

* The skills file exists in the pre-fix tree, so it **collects** and the
  failures are *behavioural* — 29 of the 31 are `DID NOT RAISE ValueError`
  and 2 are `DID NOT RAISE SkillContractError`, i.e. the constraint was silently
  dropped — not an import error.
* The model file does not exist in the pre-fix tree, so it cannot collect
  against the pre-fix source. I built a **name-only shim** — the pre-fix
  `base.py` plus a `MalformedSchemaError` class and a `redact_detail` that
  returns its argument unchanged — so the suite collects and every assertion
  that expects the *behaviour* fails. The shim supplies the **names**, never
  the fix: the pre-fix code still never raises `MalformedSchemaError` and
  still carries the details verbatim.
* The prior dispatch's number, 98, is confirmed: 126 − my 28 new *failing* cases = 98.
  The 29th of my new cases is the strict xfail, which the pre-fix tree reports as
  *xfail* rather than *failed*, so it is not in the 126.

A second, independent measurement: the behavioural matrix in §3.3 is
produced by a probe that runs the pre-fix and the post-fix code **in the
same process**, so it measures the *behaviour* and not the test suite's
ability to notice it.

---

## 8. The NEEDs

### 8.1 The shared table could live in `paa/core/`, and the import cost with it — DEFERRED-DEPENDENCY

**What:** move the keyword-shape table out of the `paa.validation` *package*
into a module under `paa/core/` (e.g. `src/paa/core/json_schema.py`), and
have `src/paa/validation/schema_validator.py` import it from there.

**Why it is worth doing:** it removes `paa.sandbox.*` (9 modules) and
`paa.validation.*` (6 modules) from the model layer's import graph, and with
them the **+26.4 ms**.

**The exact change:**

1. New file `src/paa/core/json_schema.py` holding the *contents* of
   `paa.validation.schema_validator`'s `_KeywordSpec`, `_KIND_*`,
   `_KEYWORD_SPECS` and `_ANNOTATION_KEYWORDS`, plus the `_value_defects`
   helper — or, more cheaply, a module that holds the table and a public
   `keyword_defects(schema) -> list[SchemaError]` and that
   `paa.validation.schema_validator` then uses.
2. `src/paa/validation/schema_validator.py`: replace its private table with
   an import from `paa.core.json_schema`, keeping `_KEYWORD_SPECS` as a
   re-export so its own test (which asserts the union of the two tables is
   `SUPPORTED_KEYWORDS`) keeps working.
3. `src/paa/models/base.py:76` and `src/paa/skills/contracts.py:45`: import
   from `paa.core.json_schema` instead of from `paa.validation.schema_validator`.

**Who:** an owner of `src/paa/core/**` and `src/paa/validation/**`. **Both
are outside this task's write-set** (`src/paa/validation/**` is read-only;
`src/paa/core/**` is forbidden). **This dispatch reports
DEFERRED-DEPENDENCY**: the fix is complete and correct without it, and the
cost it removes is measured in §4.2.

### 8.2 `paa.models.MalformedSchemaError` and `paa.models.redact_detail` do not exist — outstanding from the prior dispatch

**What:** `src/paa/models/__init__.py` re-exports a fixed name list from
`base.py`, and the two new public names are not in it
[VERIFIED: `grep -n "MalformedSchemaError\|redact_detail"
src/paa/models/__init__.py` → no match; the file's `__all__` lists
`redact` but not `redact_detail`].

**The exact change:** add `MalformedSchemaError` and `redact_detail` to the
`from paa.models.base import (...)` list at `src/paa/models/__init__.py:28`
and to its `__all__`.

**Why it matters:** a caller who wants to catch the new error will look at
`paa.models` first, and the package that owns every other model error does
not offer it. The suite works around this today
(`tests/models/test_model_errors.py::test_the_malformed_schema_error_is_part_of_the_module_s_public_surface`
reaches through the module that owns it), which is a sign of the gap rather
than a proof against it.

**Who:** an owner of `src/paa/models/__init__.py`. Note that file already has
another open NEED from W2-IMPR-03 (`get_model_router` narrowing `Settings`
to `ModelSettings`, `COMMS.md:120`), so it needs one owner, not two.

### 8.3 The router still carries a private copy of the redaction policy — outstanding from the prior dispatch

**What:** `src/paa/models/router.py:125` still defines `_redact_detail`,
which is **byte-equivalent** to `paa.models.base.redact_detail` today — I
compared the two bodies and they are the same rule: strings scrubbed at any
depth, keys untouched, everything else passed through
[VERIFIED `src/paa/models/router.py:125-150` against
`src/paa/models/base.py:275-308`]. Its two call sites are
`router.py:630` and `router.py:681`.

**The exact change:** delete `_redact_detail` from `router.py` and import
`redact_detail` from `paa.models.base`, using it at both call sites.

**Why it matters:** two copies of one policy are two policies the day one of
them is edited. The suite already checks they agree
(`test_the_model_layer_and_the_router_share_one_redaction_policy`), and it
tolerates the copy being retired — but a *tolerance* is not a *fix*.

**Who:** the owner of `src/paa/models/router.py` (W2-IMPR-03's file).

### 8.4 `AnthropicProvider.complete_structured` asks the frontier about a broken schema — outstanding, now with an owner

**What:** `src/paa/models/anthropic_provider.py:152` overrides the generic
loop; it calls `_send` at `:176` and only then `validate_against_schema` at
`:186`. A malformed schema is therefore sent to a frontier model as the
tool's `input_schema` before it is refused. Measured: **1 transport call**
against the base's **0**.

**The exact change:** copy the base's pre-flight refusal into the override —
before `self._send(...)`:

```python
if defects := _malformed_keywords(schema):
    raise MalformedSchemaError(
        "the schema carries a keyword whose value cannot be used",
        malformed_keywords=defects,
        provider=self.name,
    )
```

with `_malformed_keywords` and `MalformedSchemaError` imported from
`paa.models.base`. (`_malformed_keywords` is private; the alternative is to
make it public, which is a change to `base.py` — mine — and can be made
with the fix that lands.)

**Who:** an owner of `src/paa/models/anthropic_provider.py`. **This dispatch
reports DEFERRED-DEPENDENCY** for it, and has left a `strict=True` xfail in
the suite so the gap cannot be forgotten.

---

## 9. The long-run test

**Source.** The defect was a *divergence between three implementations of one
rule*. The fix puts the rule's **data** in one place (the shared keyword
table) and each validator's *reporting convention* in its own module. The
data cannot drift; the conventions are allowed to differ, because each
module's caller expects its own. The remaining drift risk — a fourth
validator appearing without the table — is now visible because the table
lives next to `SUPPORTED_KEYWORDS`, the set a new validator has to choose
from.

**Load (100x, concurrency, partial failure).** The change adds one schema
walk per `validate_against_schema` call, on top of the instance walk that was
already there — the walk is O(schema size), not O(instance size), and the
schema is the caller's and is reused across attempts. The pre-flight
refusal in `complete_structured` **removes** work in the failure case: a
malformed schema no longer costs a provider call and up to `max_retries`
repairs. Concurrency is unaffected: the shared table is read-only and
immutable after import, and `MalformedSchemaError` carries per-call state
only. Partial failure is the point of the fix rather than a hazard: the
failure is now a `PaaError`, so the orchestrator writes `EXECUTION_FAILED`
and the ledger can replay the lineage.

**Hostile input.** The hostile input here is the **schema**, and it is
model-supplied in one of the paths. Every malformed value the table knows is
reported rather than applied: the string keyword, the wrong container, the
negative bound, the regex that does not compile, the object where a list
belongs. The two holes the pre-fix code had — a raw `TypeError` that escaped
as an exception rather than a verdict, and a constraint that was *dropped* so
the instance was judged against a schema nobody wrote — are both closed, and
the matrix in §3.3 shows every one of the 27 constrained keywords closes.
The other hostile input is the **provider's own output**, which is where a
credential lands; F-3 closes the sibling that carried it verbatim into
`error.details` and from there into a ledger payload.

**Operability (observe, roll back, migrate).** *Observe:* the defect is
reported with the **schema** path (`$.properties.a.minLength`) and the
keyword name, so the author knows where to look, and the error is a
`PaaError` so it appears in the run's failure record. The skills path
turns it into a `SkillContractError` with the field name, so a broken
contract is refused at **registration** rather than at invocation. *Roll
back:* the change is two modules and one new error class; reverting the two
files restores the old behaviour and the reversal is measured in §7, so a
revert is a known quantity rather than an experiment. *Migrate:* nothing on
disk changes — no schema, no payload, no ledger record format. The only
migration-shaped consequence is for callers that were **relying** on the old
silence: a schema that used to be accepted with a dropped constraint is now
refused. That is the point, and it is loud rather than silent.

**Reversal (which test fails if the fix is reverted?).** §7: **126 of 147**
cases. The three that matter most, and the defect each one catches:
`test_all_three_validators_refuse_the_same_malformed_keyword` (the coverage
claim), `test_no_raw_type_error_escapes_complete_structured` (the escape),
and `test_model_unavailable_error_redacts_its_details` (the leak). The
behavioural matrix in §3.3 is the same measurement without the test suite
in the loop.

---

## 10. What I did NOT verify

* **The live S3 suite** was not run (no server configured on this host; the
  operator's `paa-stack-*` containers were not touched). It is outside this
  task's scope and its marker was not selected.
* **PostgreSQL parity** — `PAA_TEST_POSTGRES_DSN` is not set here, and the
  `requires_postgres` tests were skipped. Nothing in this change touches
  SQL.
* **The full suite** was run once (`pytest tests/ --ignore=tests/storage/test_s3_live.py`,
  exit 0, no failures) rather than the five consecutive runs the models
  layer's owner reports; the change touches no shared state, and the
  suite is green, but I am not claiming a flakiness rate.
* **The prior dispatch's 1092-pair harness** is gone and I could not re-run
  it. §3.1 is a *different, larger* corpus, not a re-run of theirs; the
  verdict agrees, the number does not exist any more.
* **The router's private `_redact_detail` retirement** (§8.3) is not done —
  `router.py` is another agent's file.
* **Whether `paa.validation`'s own table is itself complete** — I took it as
  the source of truth, as wave 2's T-005 established, and checked that the
  model's own keyword set is a subset of it. I did not re-audit T-005's
  choice of shapes.

---

## 11. The commands, for reproduction

```bash
# the differential (2496 pairs, 0 differences)
/tmp/paa-w2impr02/harness.py diff

# the malformed-keyword matrix (32 cases, 27 keywords)
/tmp/paa-w2impr02/harness.py matrix

# the frozen golden rows against the pre-fix source (172 rows, 0 mismatches)
/tmp/paa-w2impr02/verify_golden.py

# the reversal (126 of 147 cases fail without the fix)
PYTHONPATH=/tmp/paa-w2impr02/revcompat .venv/bin/python -m pytest \
    tests/models/test_model_errors.py -q
PYTHONPATH=/tmp/paa-w2impr02/rev .venv/bin/python -m pytest \
    tests/skills/test_contracts.py -q

# the gate
.venv/bin/python -m ruff check src tests          # All checks passed!
.venv/bin/python -m mypy src                      # Success: no issues found in 137 source files
.venv/bin/python -m pytest tests/conventions -q    # 17 passed
.venv/bin/python -m pytest tests/models tests/skills tests/validation -q
```

The harness lives under `/tmp` and is not committed: it is a measuring
instrument for this report, and the *contract* it measures is frozen into
the suite as the 172 golden rows and the 27-case three-validator test. The
reversal trees under `/tmp/paa-w2impr02/` are copies; the repository's
sources were never edited by them.

---

## 12. ADR

**No ADR is needed, and none is wanted.** The change is a bug fix that
restores a rule the tree already states in three places, and it introduces no
new architecture, no new dependency kind, no new substrate and no new
persistence. The one decision with a shape — *one table of keyword shapes,
read by three validators* — is recorded in the module docstrings
(`src/paa/models/base.py:30-60`,
`src/paa/skills/contracts.py:88-110`) and enforced by the three-validator
test, which is a stronger record than an ADR nobody reads. The one thing an
ADR *would* record — that the keyword table belongs in `paa/core/` rather
than in the `paa.validation` package — is §8.1, and it is a **NEED** with an
exact change, not a decision I am entitled to take: both files are outside
this task's write-set.
