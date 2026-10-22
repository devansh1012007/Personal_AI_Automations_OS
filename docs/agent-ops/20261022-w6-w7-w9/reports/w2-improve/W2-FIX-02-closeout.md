<!-- ORCH-DIGEST:START -->
agent: W2-FIX-02 | scope: wave-2 close-out — five open items: (1) the shared keyword table's home (the DEFERRED-DEPENDENCY W2-IMPR-02 posted), (2) the router's mode (IMPR-03's NEED on get_model_router), (3) the redaction helper's home (IMPR-02's NEED on the router's private copy, + IMPR-02's NEED 8.2 on the package's re-exports), (4) ADR-0033's title and its false wrapper claim (IMPR-05's NEED), (5) the dead egress branch in the policy agent (IMPR-04's finding) | status: DONE (4 fixed, 1 reported-not-patched)
findings: FIXED 4 | REJECTED 0 | DEFERRED 1 (item 5 — the dead egress branch: making it reachable changes the refusal the ledger records, so it is a policy change and is reported, not made silently) | CARRIED FORWARD 5 (IMPR-04's pattern-semantics findings — reproduced, recommended, NOT patched)
blockers_major_open: none
code_commits: a70d62f | report_commit: (the docs-only commit that follows the code commit)
tests_added_or_strengthened: 11 new (3 lock tests on the table's home; 4 on the factory and the mode; 1 the redaction policy is now ONE copy; 1 the package's public surface; 1 the LOCKDOWN refusal precedence + 1 strict xfail pinning the deferred policy decision) | 1 strengthened (the validation suite's source-derived keyword inventory now reads the leaf's source)
ownership_violations: none
measurements: the model layer's import cost, before vs after, min of five fresh interpreters, interleaved, three independent sets: paa.models.base 230-232 ms -> 210-211 ms (-19.6 / -20.4 / -20.4 ms) and paa.models 230-232 ms -> 210-211 ms (-19.2 / -19.5 / -22.3 ms); the paa.* modules the import drags in 32 -> 17 (paa.sandbox.* x9 and paa.validation.* x7 gone, paa.core.json_schema x1 added). A 1490-case differential over every supported keyword (valid and malformed), nested schemas, all three validators and the minimal-instance builder: byte-identical output before and after the move
mutations: 3 of the new lock tests reverted to the pre-move tree -> 3 failed (the leaf test, the three-validators test, the model-layer test); the factory's wiring reverted in a shadow copy -> 2 of the 4 new model tests failed; the pre-fix tree -> the redaction test and the public-surface test failed
commands: ruff check src tests -> 0 | MYPYPATH=src mypy src -> 0 errors, 138 files | scoped pytest (models, skills, validation, conventions, agents, integration, coder, core, api, cli) -> 1185 passed, 5 xfailed
confidence: high on items 1-4 (measured, differential-proved, reversal-proved) | high on item 5's DIAGNOSIS (measured directly) | the item-5 DECISION is a judgement and is argued in §5 with the exact change and its consequence
independent_review: NOT RUN — the review subagent could not be spawned ("Subagent depth limit reached (1)", the same blocker the other five wave-2 agents hit). The review was done by me, by hand, with the mechanical checks in §9.1 — treat this as ONE pair of eyes
last_comms_line_read: 2026-10-22T08:33:46Z (W2-IMPR-02 -> ORCH | DONE | W2-IMPR-02,F-2,F-3)
<!-- ORCH-DIGEST:END -->

# Close-out report — W2-FIX-02 (wave 2)

**Agent:** W2-FIX-02 (close-out fixer, wave 2).
**Brief:** `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-FIX-02.md`.
**Write-set:** `src/paa/core/**`, `src/paa/validation/schema_validator.py`,
`src/paa/models/{base,router,__init__}.py`, `src/paa/skills/contracts.py`,
`src/paa/agents/policy.py` *(not touched — see §5)*, `tests/**` for the code I
touched, `docs/adr/0033-s3-live-server-suite.md`, this report.
**READ-ONLY (honoured):** `docs/adr/0032-blob-error-vocabulary.md`,
`docs/adr/README.md`, `docs/adr/0034-*.md`, `src/paa/ledger/**`,
`docs/plans/**`, `AGENTS.md`.
**Branch:** `ops/20261022-w6-w7-w9` [RAN `git branch --show-current` →
`ops/20261022-w6-w7-w9`].
**Last COMMS line read at start:** `2026-10-22T08:33:46Z W2-IMPR-02 -> ORCH |
DONE | W2-IMPR-02,F-2,F-3`.

---

## 0. The five items, in one paragraph each

1. **The keyword table's home.** The table moved to
   `src/paa/core/json_schema.py` — a leaf that imports nothing from this
   project — and the three validators now read it from there and **not from
   each other**. The model layer's import cost was re-measured with the
   repo's own method (fresh interpreter, five runs, minimum taken): **−19 to
   −20 ms**, and **15 fewer `paa.*` modules** (32 → 17). The rule's behaviour
   is unchanged, proved by a **1490-case differential** whose output is byte
   identical before and after. Three new tests in the import-conventions lock
   hold the property, and all three fail when the move is reverted.
2. **The router's mode.** `get_model_router` hands the router the **whole**
   `settings` instead of narrowing it to the model half, so the router reads
   `Settings.policy.mode` and its healthcheck stops probing the frontier
   under a mode that forbids escalation. The **fail-closed default survives**
   for a caller that cannot name a mode. A new public `permission_mode`
   property makes the mode observable.
3. **The redaction helper.** There is now **one** implementation of the rule:
   `paa.models.base.redact_detail`. The router's private copy is deleted and
   the router calls the shared one. The rule is **unchanged** — same
   recursion, same treatment of keys, same pass-through — proved by a
   16-value differential against the old copy. `paa.models` now exports both
   `redact_detail` and `MalformedSchemaError` (NEED 8.2 closed).
4. **ADR-0033.** The title no longer claims "two **independent** S3 servers";
   the false "opaque wrapper / the answer degrades" claim is replaced with
   the wording W2-IMPR-05 §6 gives; and the ADR now agrees with ADR-0032's
   **corrected** decision 4 (it quoted the pre-correction text as if it were
   current).
5. **The dead egress branch.** **Diagnosed, reported, not patched** — and the
   reason is in §5: the branch is genuinely unreachable, but the only way to
   make it reachable changes the refusal the ledger records, which is a
   policy decision. I added a lock test that pins today's precedence and a
   **strict xfail** that pins the proposed end state, so the deferral cannot
   be forgotten and cannot be made silently. **The five pattern-semantics
   findings are carried forward unpatched**, with reproductions (§5.3).

---

## 1. Item 1 — the keyword table's home

### 1.1 What was wrong, and what "the table's home" has to satisfy

W2-IMPR-02 put one table of keyword shapes behind the three JSON-Schema
validators, and read it out of `paa.validation.schema_validator`. The rule
was then consistent — a keyword malformed in one validator is malformed in
all three — at the price of an import edge the model layer did not want:
`paa.models.base` imported `paa.validation.schema_validator`, which runs
`paa/validation/__init__.py`, which imports `paa.validation.engine`, which
imports `paa.sandbox.base`, which runs `paa/sandbox/__init__.py`, which
imports **every** sandbox backend. [VERIFIED by the module census in §1.4.]

The requirement is therefore fourfold:

* the table is **one** table (the point of the whole exercise);
* it lives in a **leaf** — no heavy imports, no cycle, no optional extra;
* the three validators import it **without importing each other**;
* the model layer's import cost **returns to roughly what it was**.

### 1.2 The decision: `src/paa/core/json_schema.py`

The new module holds the table and the machinery that reads it:

| moved out of `paa.validation.schema_validator` | now in `paa.core.json_schema` |
|---|---|
| `SchemaError` (the dict subclass) | `SchemaError` |
| `JsonSchema` (the type alias) | `JsonSchema` |
| `_KeywordSpec` | `KeywordSpec` (public) |
| `_KEYWORD_SPECS` | `KEYWORD_SPECS` (public) |
| `_ANNOTATION_KEYWORDS` | `ANNOTATION_KEYWORDS` (public) |
| `_value_defects` | `value_defects` (public) |
| `_schema_shape_errors` / `_walk_schema_shape` | `schema_shape_errors` (public) |
| `_type_name` | `json_type_name` (public) |
| `_KIND_*`, `_is_number`, `_is_schema`, `_sub_schemas` | private to the leaf |

**What stayed in the validator, and why.** `SUPPORTED_KEYWORDS` — the set of
keywords *this implementation claims to support* — is the validator's own
statement, and it stayed. So did the instance-validation half
(`_matches_type`, `_keyword_value`, `_validate_instance`) and the
`SchemaValidator` class. The split is now: **the leaf says what a keyword's
value must look like; the validator says which keywords it implements and
what to do with the answer.** The union of the leaf's two tables is still
exactly `SUPPORTED_KEYWORDS`, and the test that says so still runs — it now
reads the two halves from their two sources (`tests/validation/test_schema_validator.py`).

**Why `paa/core/`, and why a new module rather than `paa/core/types.py`.**
`paa.core` is the lowest layer in the tree and `paa/core/__init__.py` is
empty, so a module in it is a leaf by construction.
`paa.core.types` is *the runtime's own vocabulary* — roles, event types,
permissions, modalities — and its docstring says so; a table of **JSON
Schema** keyword shapes is a different subject and putting it there would
make that module two vocabularies. A separate `paa.core.json_schema` is the
same layer, the same leaf property, one subject.

**The alternatives I rejected, and why.**

* *Keep the table in `paa.validation` and accept the cost.* The edge was
  "safe" (no cycle, no optional extra) and still cost every process ~20 ms
  and 15 modules for a table of data. Safe is not free.
* *A second copy in the model layer.* Explicitly rejected by the wave-2
  review and by W2-IMPR-02: three tables drift, and the drift is the defect
  the finding was about.
* *A new top-level package (`paa/jsonschema/`).* A package for one module
  is a namespace with a single tenant; `paa.core` is where the tree already
  puts things nothing else may depend on.
* *Keeping `SchemaError` in the validator and having the leaf import it.*
  That would make the leaf depend on `paa.validation` — the leaf would stop
  being a leaf. So `SchemaError` moved with the table, and
  `paa.validation.schema_validator` **re-exports** it, so
  `paa.validation.SchemaError` and `paa.validation.schema_validator.SchemaError`
  keep working and are the *same object*.

### 1.3 The three validators, and the edge they no longer have

* `src/paa/models/base.py:76` — `from paa.core.json_schema import SchemaError, schema_shape_errors`.
  **The model layer no longer imports `paa.validation` at all.**
* `src/paa/skills/contracts.py:44` — `from paa.core.json_schema import JsonSchema, schema_shape_errors`.
  **The skills layer no longer imports `paa.validation` either.**
* `src/paa/validation/schema_validator.py:58` — imports the leaf.

So the three validators are **peers**: each reads the table from the leaf and
none of them imports another. The skills layer's *other* reason to import
`paa.validation` (the `validate_instance` entry point) went away with
`_malformed_keywords`, because the leaf answers the shape question directly.

### 1.4 The measurement — before and after, the repo's own method

**The method** is the one the import-conventions lock records in
`TYPE_CHECKING_NOTES` (`tests/conventions/test_import_conventions.py:204`):
*"each block was converted to a runtime import in a copy of the tree, and the
importing module's own import time was timed in a fresh interpreter, five
times, with the minimum taken (minimum rather than mean, because a scheduler
hiccup can only ever add)."*

So: a **fresh interpreter** per run, the module's own import timed, **five
runs, minimum taken**. The two trees were measured **interleaved** (before,
after, before, after …) so that a drift in the host's load hits both equally,
and the whole thing was repeated as **three independent sets of five**.

| | before (min of 5) | after (min of 5) | Δ (min) |
|---|---|---|---|
| `paa.models.base` — set 1 | 230.24 ms | 210.61 ms | **−19.63 ms** |
| `paa.models.base` — set 2 | 231.15 ms | 210.79 ms | **−20.36 ms** |
| `paa.models.base` — set 3 | 231.62 ms | 211.24 ms | **−20.38 ms** |
| `paa.models` — set 1 | 230.18 ms | 210.96 ms | **−19.22 ms** |
| `paa.models` — set 2 | 229.94 ms | 210.40 ms | **−19.54 ms** |
| `paa.models` — set 3 | 232.17 ms | 209.88 ms | **−22.29 ms** |

**The number: −19.6 to −20.4 ms on `paa.models.base` and −19.2 to −22.3 ms
on `paa.models`**, median of the three minima **−20.4 ms** and **−19.5 ms**.
The absolute figures on this host are ~230–232 ms before and ~210–211 ms
after. [RAN `/tmp/w2fix02/ab.py` — the harness times the import in a fresh
interpreter, asserts the tree it measured is the tree it meant to measure
(the child's `paa.__file__` must be under the tree's `src`), and takes the
minimum of five.]

For comparison, the number W2-IMPR-02 measured for the *whole* edge it
added was **+26.4 ms**; the move gives back **~20 ms** of it on this host.
The two figures are not the same measurement (that one was a before/after
across two different moments, mine is interleaved), so the honest statement
is the one above: **the model layer's import is about 20 ms cheaper than it
was, and 15 modules lighter.**

**The module delta** — the number that does not move with the host. `sys.modules`
after `import paa.models.base`, in a fresh interpreter:

| | before | after |
|---|---|---|
| `paa.*` modules loaded | **32** | **17** |

Removed (16): `paa.sandbox`, `paa.sandbox._win32`, `paa.sandbox.base`,
`paa.sandbox.docker_backend`, `paa.sandbox.dryrun_backend`,
`paa.sandbox.recursion`, `paa.sandbox.subprocess_backend`,
`paa.sandbox.watchdog`, `paa.sandbox.wsl_backend`, `paa.validation`,
`paa.validation.ast_scanner`, `paa.validation.engine`, `paa.validation.patch`,
`paa.validation.schema_validator`, `paa.validation.test_runner`,
`paa.validation.workspace`.
Added (1): `paa.core.json_schema`.

**+26.4 ms and 16 new modules** was W2-IMPR-02's figure for the edge it
added; the move removes **all sixteen** and adds **one**.

### 1.5 The behaviour did not change — a differential, not an assertion

The move had to be a *move*. So the old implementation and the new one were
run over the same corpus and their **outputs compared byte for byte**:

* every keyword in `SUPPORTED_KEYWORDS`, in a **valid** and an **invalid**
  shape, at the root of a schema;
* the same, against 13 payloads (including the ones that do not reach the
  keyword, which is the case the finding was about);
* 14 nested schemas (a malformed constraint inside a property, inside an
  `anyOf` branch, inside a `definitions` map; the two boolean schemas; an
  empty schema);
* all **three** validators on every one of them — the runtime's output gate
  (`validate_instance`), the model layer (`validate_against_schema`, and the
  `MalformedSchemaError` it raises, with its details and its message) and the
  skills layer (`schema_errors`, and the `ValueError` it raises);
* the minimal-instance builder.

**1490 cases. The two dumps are byte identical.** [RAN
`/tmp/w2fix02/rev/differential.py` under `PYTHONPATH=<before tree>` and
without it; `diff` reports no difference.] So the defect messages, the
paths, the `schema_error` marker, the **order** of the errors, the set of
constrained keywords, the "a malformed keyword is reported and **not
applied**" behaviour and the minimal-instance output are all unchanged.

### 1.6 The lock — three new tests, and their teeth

`tests/conventions/test_import_conventions.py` gains a section, *"The keyword
table's home"*:

* **`test_the_keyword_table_lives_in_a_leaf`** — the leaf imports nothing
  from this project (read off the AST, so a lazy import would not hide it),
  and the module exists at all (otherwise the check would be vacuous).
* **`test_the_three_validators_read_the_table_from_the_leaf`** — each of the
  three imports `paa.core.json_schema` and **none of them imports another
  of the three**; and the table is the **same object** the validator uses
  (`schema_validator.KEYWORD_SPECS is KEYWORD_SPECS`), so a validator that
  re-declared the table could not satisfy the structural check.
* **`test_the_model_layer_does_not_pull_in_the_validation_or_sandbox_packages`**
  — the *measurement*, as a lock: a fresh interpreter imports
  `paa.models.base` and reports the `paa.*` modules it brought in; none of
  them may be under `paa.validation` or `paa.sandbox`. A fresh interpreter
  and not an import in the test session, because a test session's
  `sys.modules` is already populated and the sandbox would be in it whether
  or not this import pulled it in.

**The teeth, measured:** run against the **pre-move tree**
(`PYTHONPATH=<the HEAD copy of src>`), **all three fail**; against the tree as
it stands, all three pass. [RAN `PYTHONPATH=/tmp/w2fix02/before/src pytest
tests/conventions` → exit 1, three FAILED; `pytest tests/conventions` → exit 0,
20 passed.]

**One more lock change, in the same commit:** the *existing* lock's
source-derived keyword inventory
(`tests/validation/test_schema_validator.py::_constrained_keywords`) read
`_KEYWORD_SPECS` out of the validator's source. That name has moved with the
table, so the helper now reads **the leaf's source** for the two tables and
**the validator's source** for `SUPPORTED_KEYWORDS` — the check is now
explicitly a *join* across the two modules that own the two halves, which is
what it always was.

**No row was needed in `TYPE_CHECKING_CLASSES`:** the new module has no
`if TYPE_CHECKING:` block, so the classification table is unchanged and
`test_every_type_checking_block_is_classified` still passes with the same
33 entries. [VERIFIED `pytest tests/conventions` → 20 passed, and the
classification check is one of them.]

### 1.7 What I did not do

* I did **not** change what the rule *does*. The differential in §1.5 is the
  evidence.
* I did **not** touch `paa.validation`'s own API: `JsonSchema`,
  `SchemaError`, `SchemaValidator`, `validate_instance` and
  `SUPPORTED_KEYWORDS` are all still importable from where they were, and
  `SchemaError` is the same object it was (it moved, and the validator
  re-exports it).
* I did **not** make the leaf public *API* of `paa.core` — `paa/core/__init__.py`
  is still empty and still not imported by the leaf.

---

## 2. Item 2 — the router's mode

### 2.1 The defect

`EscalatingModelRouter` reads the deployment's permission mode when it was
given the whole `Settings` (`settings.policy.mode`), and falls back to the
most restrictive mode there is — `LOCKDOWN` — when it was not. That
fail-closed default is right and it survived.

But `get_model_router` — the **call site**, and the only production one
(`src/paa/runtime.py:297`) — narrowed its argument to the model half before
handing it over:

```python
models = resolve_model_settings(settings)
return EscalatingModelRouter(..., settings=models, ...)
```

`ModelSettings` carries no mode at all, so the production router **never saw
the deployment's mode** and always fell back to `LOCKDOWN`. Safe, and wrong
for every deployment that is not in LOCKDOWN: the frontier was never probed
(`healthcheck` is a network call), and a deployment could not tell.

### 2.2 The change

One line, plus the package's public surface:

* `src/paa/models/__init__.py:273` — `settings=settings`. The whole
  `settings` goes to the router; `resolve_model_settings` still supplies the
  two provider halves. The router's constructor already accepted
  `ModelSettings | Settings | None` and already resolved the mode from it, so
  no router logic changed.
* `src/paa/models/router.py:257` — a new **public, read-only** property
  `permission_mode`. It is the mode the router acts on, and a caller has to
  be able to ask: the healthcheck's answer depends on it, and a deployment
  that cannot see the mode cannot tell whether its frontier is being probed.
  It is deliberately **not** in `describe()` — a router built from the whole
  `Settings` and one built from the model half describe themselves
  identically today, and the mode is the one thing that genuinely differs
  between them; putting it in `describe()` would break
  `test_both_shapes_of_configuration_resolve_identically` for no gain.

**The fail-closed default survives.** A caller that hands over only
`ModelSettings` — which carries no mode — still gets `LOCKDOWN`. That is
asserted directly
(`test_the_factory_fails_closed_when_the_caller_names_no_mode`).

### 2.3 The tests, and their teeth

Four new tests in `tests/models/test_settings_resolution.py`:

| test | what it pins | fails without the fix? |
|---|---|---|
| `test_the_factory_hands_the_deployment_mode_to_the_router` | the factory hands the mode over | **yes** |
| `test_the_factory_fails_closed_when_the_caller_names_no_mode` | the safe default survives | no (characterisation) |
| `test_the_factory_s_router_probes_the_frontier_when_it_may` | the end of the chain: under ASK the frontier **is** probed | **yes** |
| `test_the_factory_s_router_does_not_probe_it_under_lockdown` | under LOCKDOWN it is **not** | no (characterisation) |

The two behavioural tests use the **offline** provider pair — a local `echo`
and an `anthropic` frontier — because `AnthropicProvider.healthcheck` is
credential-presence only and makes **no network call** by design
(`src/paa/models/anthropic_provider.py:273`). The frontier's key is
**absent** from the healthcheck report when it was not probed, and absent
means "not asked", which is not the same as "asked, and it cannot serve".

**The reversal, measured:** with the factory's wiring reverted in a *shadow
copy* of the tree (everything else identical), the two "fix" tests fail and
the two characterisation tests still pass. [RAN `PYTHONPATH=<shadow>
pytest tests/models/test_settings_resolution.py` → 2 FAILED:
`assert <PermissionMode.LOCKDOWN> is <PermissionMode.ASK>` and
`assert 'anthropic' in {'echo': True}`.]

### 2.4 Alternatives rejected

* *Pass the mode as a separate argument* (`EscalatingModelRouter(local,
  frontier, settings=models, mode=settings.policy.mode)`). More explicit,
  but it adds a second way to say the same thing — and the router's
  constructor already reads the mode off the `Settings` it was given, so the
  second way would be the one a caller forgets.
* *Have the router read the global settings itself* (`get_settings()`).
  That would make the router's behaviour depend on process-global state, and
  it is exactly what the constructor's `settings` parameter exists to avoid.
* *Put the mode in `describe()`*. Breaks the "both shapes resolve
  identically" contract for no operational gain (§2.2).

---

## 3. Item 3 — the redaction helper

### 3.1 The decision: one helper, in `paa.models.base`

The rule, in one sentence: **a provider's own output never reaches a log
line, a ledger payload, or an exception's message and details.**

There were **two** implementations of "what does redacting a free-form detail
mean": `paa.models.base.redact_detail` (used by the three error classes) and
`paa.models.router._redact_detail` (used by the router on both paths that
copy a provider's diagnosis). They were byte-equivalent *today* — which is
exactly the state in which two copies drift.

**Now there is one.** `src/paa/models/router.py` imports `redact_detail`
from `paa.models.base` and calls it at both call sites
(`router.py:625` and `router.py:676`); the private copy is deleted.

**Why `paa.models.base` is the house-style home.** The rule's *subject* is a
provider's own output, and the model layer is where providers live. The
string scrubber `redact` is already there, and the three error classes that
scrub **at construction** are there. The router already imports from
`base`. The alternative — a new `paa/core/redaction.py` — would put a
*model-layer* rule in the tree's lowest layer for no other consumer, and
the alternative of a new `paa/models/redaction.py` would add a module to the
model package for one function that `base` already hosts. Neither is
"one helper in the place the house style puts it"; `base` is.

**What was NOT widened.** The rule is exactly what it was: every **string
value** scrubbed at any depth (inside lists, tuples, nested mappings); the
**keys** untouched (they name fields, they are written by the code that
raised the error, and rewriting one would break the contract without
removing a leak); everything that is not a string, a list/tuple or a mapping
passed through unchanged (it cannot carry a credential, and the details must
stay JSON-serialisable for the ledger). The differential in §3.3 is the
evidence.

### 3.2 The public surface (NEED 8.2, closed)

`src/paa/models/__init__.py` re-exports a fixed name list, and the two new
public names were not in it — W2-IMPR-02's NEED 8.2. They are now:
`paa.models.MalformedSchemaError` and `paa.models.redact_detail`, in the
import list and in `__all__`.

The reason is the same reason the redaction rule has one implementation: a
caller who has to guess which module owns the helper will write a second
one. `paa.models` is the package a caller reaches for first, and every other
model error is exported there.

### 3.3 The rule did not change — a differential

Sixteen values (strings with three different credential shapes, plain text,
the empty string, `None`, ints, floats, bools, a list, a tuple, three
nested mappings, a mapping whose *key* names a credential, and a list of
nested dicts) were run through the old private copy and the shared helper.

**Every value the old copy produced is exactly what the shared helper
produces** — the only difference in the diff is the disappearance of the
`private` key, because the copy is gone. [RAN `/tmp/w2fix02/rev/redaction.py`
under the pre-fix tree and against the tree as it stands; `diff` shows only
the removed `private` lines, and every `shared` line is unchanged.]

### 3.4 The test, and its teeth

`tests/models/test_model_errors.py::test_the_model_layer_and_the_router_share_one_redaction_policy`
used to compare the two copies' *behaviour* and **tolerate** the copy being
retired. A behavioural comparison passes while two copies sit side by side,
which is the state the test exists to end. It now asserts the **copy**:

```python
assert not hasattr(router_module, "_redact_detail"), ...
assert getattr(router_module, "redact_detail", None) is redact_detail
```

Against the pre-fix tree both this test and the public-surface test fail.
[RAN `PYTHONPATH=<the HEAD tree> pytest tests/models/test_model_errors.py` →
2 FAILED.]

**The end-to-end guarantee is already covered** and still is:
`tests/models/test_key_secrecy.py` is the file whose docstring states the
rule this brief quotes — *"no API key reaches a log line, a ledger payload,
or an error the router raises — its message, its details, its exception chain
and the rendered traceback"* — and it exercises the router's two paths
(`underlying_*` details) and the escalation payload. It is green.

---

## 4. Item 4 — ADR-0033

### 4.1 What was false, and what replaced it

W2-IMPR-05 measured the wrapper's shape on two live servers and found the
ADR repeating a claim that is false. The ADR had three such places:

| place | was | now |
|---|---|---|
| the **title** | "…tested against two **independent** S3 servers…" | "…tested against **two S3 servers**…" |
| the **Decision** | "tested against two **independent** S3 servers" | "two S3 servers", with a paragraph stating what "two servers" is worth and what it is not |
| the **table** | `upload_file` — "carries a cause: **no**" | a four-column table: response of its own / `__cause__` / `__context__`, with the driver's error as the implicit `__context__` |
| the **wrapper paragraph** | "it is **opaque** — no response, no cause, and therefore nothing for the translation to read", and "the answer **degrades** from `NotFound` to the unclassified `BlobFailure`" | the replacement wording W2-IMPR-05 §6 gives, verbatim in substance |

The replacement text, as applied:

> The wrapper is not empty: it carries no response *of its own* and no
> `__cause__`, but boto3 raises it inside an `except ClientError` block and
> without a `from`, so the driver's error is the wrapper's implicit
> `__context__` — and `minio_backend.py::_driver_cause` reads the
> `__context__` as well as the `__cause__`. The store's answer on the
> wrapping route is therefore **the same** answer, not a degraded one.
> `upload_fileobj` — the call `put_stream` makes — does not wrap at all.

### 4.2 Agreement with ADR-0032's corrected decision 4

ADR-0032 was corrected on 2026-10-22 (its Amendment, at the end of the
record). ADR-0033 still quoted the **pre-correction** text as if it were
current — "ADR-0032's decision 4 **says** that boto3's transfer manager
'catches the driver's `ClientError` and raises an `S3UploadFailedError`
instead'" — which is no longer what the record says.

Both records now say the same four things:

| ADR-0032 (corrected decision 4) | ADR-0033 (now) |
|---|---|
| "`upload_fileobj` **does not wrap**. It propagates the driver's `ClientError` with its response intact, so the store reads the answer directly." | "**For `upload_fileobj` — the call `put_stream` makes — it does not wrap.**" and the table's first row: "the driver's `ClientError` (`NoSuchBucket`) … **yes** … unset … unset" |
| "boto3's `upload_file` … catches the driver's `ClientError` and raises an `S3UploadFailedError` instead, and it does so **without chaining it explicitly**: the wrapper's `__cause__` is unset, and the driver's error is the wrapper's implicit `__context__`." | "the wrapper … carries no response *of its own* and no `__cause__`, but boto3 raises it inside an `except ClientError` block and without a `from`, so the driver's error is the wrapper's implicit `__context__`" |
| "**The wrapper is not opaque.** … the translation reads it there." | "The wrapper is not a fiction, though: it is what `upload_file` produces, and it is **not empty**." |
| "The live suite pins the answer under both shapes (`…::test_the_wrapping_upload_route_answers_alike`)" | "The suite pins it: `tests/storage/test_s3_live.py::test_the_wrapping_upload_route_answers_alike` runs the wrapping route against a real server … and asserts that the two routes answer alike." |

The section's heading changed with it — "What ADR-0032's decision 4 got
wrong" → "**What ADR-0032's decision 4 got wrong, and what it says now**" —
because the record it refers to has since been corrected, and an ADR that
quotes a superseded decision as the current one is the same defect as the
one it is correcting.

### 4.3 The title, and what the live suite can actually assert

The title's claim was the one W2-IMPR-05 narrowed in the live suite's own
docstring: the suite cannot establish *independence* — nothing in the
environment says which programs the two endpoints are, and two containers of
one image would have satisfied every assertion in the file. What the suite
**does** establish is that the two **answer differently**:
`test_the_two_servers_are_distinguishable` reads what each server says about
itself and fails if the two answers are the same.

ADR-0033 now says the same, in its Decision section, and attributes the
independence to **the operator's choice of labels**:

> **What "two servers" is worth, and what it is not.** The independence of the
> two is the **operator's choice of labels**: nothing in the environment says
> which programs the two endpoints are, and this suite does not establish
> independence. What it does establish is that the two **answer differently** …
> The labels name the implementations the operator pointed the suite at; the
> suite checks that the two are not the same one.

**What I did not change in ADR-0033:** the Context, the seven numbered
decisions, the measured error-code table, the "one behavioural difference"
section, the "honest limits of the suite" and the Consequences. The ADR is
not mine to re-argue; the brief asked for the false claim and the title.

---

## 5. Item 5 — the dead egress branch in the policy agent

### 5.1 The finding, re-measured (not taken on trust)

`src/paa/agents/policy.py:216-221`, inside `_check_lockdown`:

```python
for step in steps:
    perms = set(step.get("required_permissions", []))
    if {Permission.NET_EGRESS.value, Permission.EXTERNAL_WRITE.value} & perms:
        return PolicyVerdict.blocked("egress/external-write requested under LOCKDOWN")
```

**It cannot fire.** `PermissionMode.LOCKDOWN` grants exactly
`{Permission.SANDBOX_RUN}` (`src/paa/core/types.py:265`, a hard-coded
matrix, not configurable), so a step declaring `PERM_NET_EGRESS` declares a
permission the mode does not grant — and `_check_permissions` refuses that
**first**: the `checks` tuple at `policy.py:156` is
`(_check_permissions, _check_lockdown, _check_irreversible, _check_paths,
_check_rules, _check_anti_goals)` and the first non-`None` verdict wins.

Measured, through the agent's own `handle()` path:

```
PERM_NET_EGRESS        -> STATUS_BLOCKED | permissions not granted in LOCKDOWN: ['PERM_NET_EGRESS']
PERM_EXTERNAL_WRITE    -> STATUS_BLOCKED | permissions not granted in LOCKDOWN: ['PERM_EXTERNAL_WRITE']
```

The air-gap refusal is **never** the one that answers.

### 5.2 The decision: **report it, do not patch it** — and why

The brief's rule: *if the unreachable egress check is a defect (a check that
never runs) fix it — but if making it reachable would change what the policy
refuses, that is a policy change and you must report it with the proposed
change and the consequence rather than make it silently.*

**The check is a defect** — a check that never runs, reading as a guarantee
it does not provide.

**But making it reachable is a policy change.** The only way to reach it is
to move `_check_lockdown` ahead of `_check_permissions` in the `checks`
tuple — the tuple's order *is* the precedence, and both checks are already
evaluated eagerly, so nothing else changes. And the two checks produce
**different refusals**:

| | today | after the reorder |
|---|---|---|
| a LOCKDOWN plan declaring `PERM_NET_EGRESS` | `permissions not granted in LOCKDOWN: ['PERM_NET_EGRESS']` | `egress/external-write requested under LOCKDOWN` |
| a LOCKDOWN plan whose command trips the network patterns *and* declaring an ungranted permission | `permissions not granted in LOCKDOWN: [...]` | `network egress attempted under LOCKDOWN` |
| a LOCKDOWN plan with an **unknown** permission name *and* a network command | `unknown permission requested: 'PERM_MADE_UP'` | `network egress attempted under LOCKDOWN` |

**The set of refused plans is identical** — the branch is strictly subsumed
by `_check_permissions`, so no plan that is refused today becomes allowed and
none that is allowed becomes refused. The **security posture does not
change**. What changes is the **reason**, and the reason is not decoration:
`src/paa/agents/orchestrator.py:639` writes it into the `POLICY_BLOCKED`
ledger payload (`"reason": verdict.get("reason")`), so historical
`POLICY_BLOCKED` records and new ones would carry different strings for the
same class of event.

**Which refusal *should* win is a policy question** — it is the question the
brief told me not to answer silently, and it is the question the previous
report (W2-IMPR-04 §5.1) also declined to answer.

**The proposed change, exactly:**

```diff
--- a/src/paa/agents/policy.py
+++ b/src/paa/agents/policy.py
@@ -156,8 +156,8 @@
     checks = (
-        self._check_permissions(steps, ctx),
         self._check_lockdown(blob, steps, ctx),
+        self._check_permissions(steps, ctx),
         self._check_irreversible(blob, ctx),
```

**The consequence, exactly:** under `LOCKDOWN` only, a plan that both
declares an ungranted permission and trips the air-gap check is refused with
a different reason, and that reason is what the `POLICY_BLOCKED` payload
records. No other mode is affected (`_check_lockdown` returns `None`
immediately outside LOCKDOWN), and **no plan changes from refused to allowed
or the other way round**.

**The alternative the operator may prefer instead: delete the branch.**
Deleting it is behaviour-preserving in the strictest sense (it cannot fire,
so removing it changes nothing observable) and it removes the false promise.
I did not do it, for two reasons: it removes the only *statement* of the
air-gap guarantee from the place where the air-gap is checked — which is a
statement about the gate's rule set, and the gate's rule set is what the
previous report called "a policy decision" — and the brief's instruction for
this branch is to report it rather than to resolve it by hand. The
reproduction above is what the operator needs to choose between the two.

### 5.3 What I did instead: two tests, so the deferral cannot be forgotten

Both in `tests/agents/test_policy.py`, in `TestLockdown`:

* **`test_a_declared_egress_permission_is_refused_by_the_permission_check`**
  — a **lock**. It pins today's behaviour *and* names the dead branch where
  the next maintainer will look: the verdict is blocked, the reason is
  exactly `permissions not granted in LOCKDOWN: ['PERM_NET_EGRESS']`, and the
  air-gap reason is **not** the one that answered. If someone reorders the
  checks without a decision, this goes red.
* **`test_the_lockdown_air_gap_refusal_names_the_egress`** — a **strict
  xfail** pinning the *proposed* end state: the reason is
  `egress/external-write requested under LOCKDOWN`. `strict=True` means the
  marker **cannot outlive the decision**: the day the branch starts
  answering, the test XPASSes and the suite goes red until the marker is
  removed. This is the repo's own convention for a known defect in a file
  the fixer does not own (`tests/validation/test_ast_scanner.py:449`, and
  W2-IMPR-02's Anthropic path).

  **This is the one test in the change that does not fail before the fix**,
  and deliberately so: it is a *lock*, not a fix. The lock test above is
  likewise a characterisation, and both exist to make a deferred **policy**
  decision impossible to make by accident.

### 5.4 The five pattern-semantics findings — CARRIED FORWARD, UNPATCHED

W2-IMPR-04 reported five pattern-semantics findings and deliberately left
them unpatched, because each one **widens a security rule** and that is a
policy decision. The brief puts them out of my scope. **I did not patch
them.** They are reproduced here with a recommendation, so the operator can
decide whether a later task widens the rule. The reproductions are
W2-IMPR-04's, through the gate's own verdict; I did not re-run them (they
are not my scope and I did not modify the gate's rule set).

**F-1 (MAJOR) — the "unqualified delete" rule's answer depends on the
*position* of the step in the plan.** `\bDELETE\s+FROM\s+\w+\s*(;|$)` is
anchored on `;` or end-of-**string**, and the renderer joins the plan's parts
with newlines, so a delete that is not the last step is invisible:

```
DELETE FROM users as the ONLY step          -> STATUS_BLOCKED
DELETE FROM users followed by another step  -> STATUS_APPROVED
DELETE FROM users; followed by another step -> STATUS_BLOCKED
```

*Recommendation:* `\bDELETE\s+FROM\s+[\w.]+\b(?!\s+WHERE)` — the rule is about
the **absence of a WHERE clause**, not about a terminator. The reason it was
not applied is the trade W2-IMPR-04 named: that pattern also matches the
phrase "delete from the old notes" in a *goal*, and the gate scans prose as
well as commands, so a plan whose goal merely mentions deleting something
would be blocked under SAFE and would take the risk score to 0.8 in every
other mode. **A corpus of real command spellings is needed to adjudicate
this**, which is why it is a task of its own.

**F-2 (MAJOR) — `git push -f` is not refused.** The rule requires the long
option: `\bgit\s+push\s+.*--force\b`. The short one is the same operation.

```
git push -f origin main   -> ALLOWED
```

*Recommendation:* the narrow form `\bgit\s+push\b(?=[^;&|]*\s-f\b)` — the
option must belong to the git push command itself. **Not** `.*-f\b`, which
false-positives on a chained command (`git push origin main && ls -f` is
not a force push).

**F-3 (MINOR) — the `rm` rule misses flag spellings a real `rm` accepts.**
`\brm\s+-[rRf]{1,2}\b` does not match `-rfv` (the `\b` fails against the
trailing `v`) and knows nothing of the long options:

```
rm -rfv /data                        -> ALLOWED
rm --recursive --force /data         -> ALLOWED
rm --preserve-root -rf /             -> ALLOWED
```

*Recommendation:* scan the command line for the long-option family rather
than matching adjacency — a different rule shape, and a scan across a
command line is where "rm" in prose or in a path starts to cost.

**F-4 (MINOR) — the SQL rules miss spellings a planner emits.**

```
psql -c 'DELETE FROM public.users;'   -> ALLOWED   (\w+ stops at the dot)
psql -c 'DELETE FROM "users";'        -> ALLOWED   (\w+ stops at the quote)
psql -c 'TRUNCATE events;'            -> ALLOWED   (\bTRUNCATE\s+TABLE\b)
```

*Recommendation:* `\w+` → `[\w."]+` for the delete is a one-character fix.
The `TRUNCATE` one is not: `\bTRUNCATE\b` alone would match the English word
"truncate" in a goal.

**F-5 (MINOR) — the deletion rules are anchored on `os.`, and the table is
not exhaustive of irreversible operations.**

```
python -c "import os; os.removedirs('/data/d')"  -> ALLOWED
python -c "from pathlib import Path; Path('/data/x').unlink()" -> ALLOWED
git clean -fd                                     -> ALLOWED
git checkout .                                    -> ALLOWED
psql -c 'DROP MATERIALIZED VIEW daily_totals'     -> ALLOWED
```

*Recommendation:* this is a **scope statement**, not a defect in the nine:
the honest form of the ROADMAP's phrase is *"SAFE blocks these nine
irreversible operations"*. Either the ADR says that, or the table grows —
and the table growing is a policy change like the other four.

**Common to all five:** each fix is one line of regex and a behaviour change
to a security gate whose input includes prose. Each trades a false negative
for a false positive, and the trade is the operator's to make. The
recommended next task is a dedicated one — *"the policy gate's rule set"*,
with a corpus of real command spellings to adjudicate against — rather than
five regex patches smuggled into a coverage fix.

---

## 6. The long-run test (standing orders §8)

* **Source.** The defect was *one rule expressed in three places*, and the
  fix puts the rule's **data** in one leaf and each validator's **reporting
  convention** in its own module. The data cannot drift; the conventions are
  allowed to differ, because each module's caller expects its own. The
  remaining drift risk — a fourth validator appearing without the table —
  is now visible, because the leaf is the place a new validator has to read
  from, and the lock test names all three by name.
* **Load.** No new I/O. The leaf imports nothing from the project; the model
  layer's import is ~20 ms **cheaper** than before, not dearer. The
  measurements are of import cost, which is paid once per process and by
  every process.
* **Hostile input.** The keyword table is reached by *untrusted* schemas —
  a skill contract from a marketplace package, a planner's output. The
  malformed-value path is unchanged and still returns, never raises, a
  `TypeError`; the 1490-case differential includes a malformed constraint in
  a branch the payload never reaches, a `pattern` that is not a regex, a
  `multipleOf` that is a string, a `properties` that is a list, and both
  boolean schemas.
* **Operability.** The redaction rule has one implementation and one public
  name (`paa.models.redact_detail`), so an operator reading a leak can find
  the rule. The router's mode is now *observable*
  (`router.permission_mode`), so "is my frontier being probed?" is a
  question with an answer. The ADR no longer tells a maintainer the wrapper
  is opaque, which would have led them to delete the unwrapping.
* **Reversal — which test fails if the fix is reverted:**
  * the move reverted → **3** of the conventions lock's new tests fail, and
    the validation suite's inventory check fails with it (the table is no
    longer where the source says it is);
  * the factory's wiring reverted → **2** of the 4 new model tests fail
    (the mode, and the frontier probe);
  * the router's copy restored → the redaction-policy test fails (the copy
    is back) and the public-surface test fails (the package no longer
    exports the names);
  * ADR-0033 reverted → no test fails, because an ADR is a record and its
    truth is checked by reading it; the two records it must agree with are
    quoted side by side in §4.2 so a reviewer can check them.

---

## 7. What I did NOT verify

* **The independent-review subagent could not be run.** Every attempt
  returned `Subagent depth limit reached (1)` — the same blocker the other
  five wave-2 agents hit. **This change is reviewed by ONE pair of eyes**
  (mine), with the reviewer's checks performed by hand (§9.1). A delta
  reviewer should read it as unreviewed.
* **The full suite was not run** (standing order 16). The scoped run covers
  the touched code: `tests/models`, `tests/skills`, `tests/validation`,
  `tests/conventions`, `tests/agents`, `tests/integration`, `tests/coder`,
  `tests/core`, `tests/api`, `tests/cli` → **1185 passed, 5 xfailed**. The
  orchestrator's gate is the thing that covers the rest.
* **The live S3 suite was not run** — it needs two servers, and ADR-0033's
  correction is a *record* correction resting on W2-IMPR-05's measurement
  (two servers, boto3 1.43.117), which I did not repeat. I read W2-IMPR-05
  §1.2 and §5.2 and checked the correction against ADR-0032's Amendment; I
  did not re-measure the wrapper.
* **`paa.models.anthropic_provider`'s pre-flight refusal** is still open
  (W2-IMPR-02's third NEED): the Anthropic path still asks the frontier
  about a broken schema. That file is outside my write-set, and the strict
  xfail that pins it is already in the suite. **Not fixed here.**
* **The five pattern findings of §5.4 were not re-measured** — they are
  W2-IMPR-04's reproductions, carried forward.
* **`describe()` was deliberately left out of the router's mode** (§2.2), so
  the two shapes of configuration still describe themselves identically.

---

## 8. The commands, for reproduction

```bash
# the gate
.venv/bin/python -m ruff check src tests
MYPYPATH=src .venv/bin/python -m mypy src

# the scoped suites
.venv/bin/python -m pytest tests/models tests/skills tests/validation \
  tests/conventions tests/agents tests/integration tests/coder tests/core \
  tests/api tests/cli

# the import measurement (the repo's method: fresh interpreter, 5 runs, min)
#   harness: /tmp/w2fix02/ab.py  (before = the HEAD tree, after = the working tree)
.venv/bin/python /tmp/w2fix02/ab.py paa.models.base paa.models

# the module census
PYTHONPATH=<tree>/src .venv/bin/python -c \
  "import importlib,sys; importlib.import_module('paa.models.base'); \
   print(len([m for m in sys.modules if m.startswith('paa')]))"

# the behaviour differential (before vs after)
PYTHONPATH=/tmp/w2fix02/before/src .venv/bin/python /tmp/w2fix02/rev/differential.py > before.json
.venv/bin/python /tmp/w2fix02/rev/differential.py > after.json
diff before.json after.json        # byte identical, 1490 cases

# the reversals
PYTHONPATH=/tmp/w2fix02/before/src .venv/bin/python -m pytest tests/conventions      # 3 FAILED
PYTHONPATH=/tmp/w2fix02/before/src .venv/bin/python -m pytest tests/models/test_model_errors.py  # 2 FAILED
PYTHONPATH=/tmp/w2fix02/partial/src .venv/bin/python -m pytest tests/models/test_settings_resolution.py  # 2 FAILED
```

---

## 9. The review

### 9.1 What I ran by hand, in place of the subagent

The subagent could not be spawned, so the review's checks were performed
mechanically instead:

| check | result |
|---|---|
| the 1490-case differential, before vs after | **byte identical** (§1.5) |
| the 16-value redaction differential, old copy vs shared | identical (§3.3) |
| fresh-interpreter import of `paa.core.json_schema`, `paa.models.base`, `paa.skills.contracts`, `paa.validation.schema_validator`, `paa.validation`, `paa.models`, `paa.core` | **all import cleanly — no cycle** |
| the three new lock tests, reverted to the pre-move tree | **3 fail** (§1.6) |
| the factory's wiring, reverted in a shadow copy | **2 of 4 fail** (§2.3) |
| the redaction/public-surface tests against the pre-fix tree | **2 fail** (§3.4) |
| item 5's claim ("the branch cannot fire"), re-measured directly | **confirmed** (§5.1) |
| ADR-0033 against ADR-0032's corrected decision 4 | **agree**, quoted side by side (§4.2) |
| `ruff check src tests` | **0 findings** |
| `MYPYPATH=src mypy src` | **0 errors, 138 files** |
| `# noqa` / `type: ignore` / ruff per-file ignore / `pyproject.toml` added | **none** — the diff adds no suppression of any kind (an earlier draft of mine carried one `type: ignore` in a test; it was removed and the code re-typed instead) |

### 9.2 What a reviewer should look at hardest

* The **`_type_name` → `json_type_name`** rename in the validator: it touches
  the *messages* of the instance errors, and the differential covers those
  messages, but a reader should confirm the rename did not leave a stale
  private name anywhere (grep: `_type_name` no longer appears in `src/`).
* The **`SchemaError` move**: the class is now defined in the leaf and
  re-exported by the validator. `isinstance` still works across the two
  names because they are the same object — worth a reader's own check,
  because "the same object" is the whole trick.
* The **lock test's third half** (`test_the_model_layer_does_not_pull_in_the_validation_or_sandbox_packages`):
  it is the only check in the file that shells out to measure an import
  graph, and a reviewer should decide whether the cost of that probe is
  acceptable in the conventions suite (it is one interpreter, and the
  suite already probes 137 modules that way).

---

## 10. The commits

* **Code — `a70d62f`** — `src/paa/core/json_schema.py` (new),
  `src/paa/validation/schema_validator.py`, `src/paa/models/base.py`,
  `src/paa/skills/contracts.py`, `src/paa/models/__init__.py`,
  `src/paa/models/router.py`, `tests/conventions/test_import_conventions.py`,
  `tests/validation/test_schema_validator.py`,
  `tests/models/test_model_errors.py`,
  `tests/models/test_settings_resolution.py`,
  `tests/agents/test_policy.py`.
* **Docs — the commit that follows this one** —
  `docs/adr/0033-s3-live-server-suite.md` and this report.

The lock change (`tests/conventions/test_import_conventions.py`) is in the
**same commit** as the move it describes, as the brief requires.

`docs/agent-ops/20261022-w6-w7-w9/COMMS.md` is **not** in either commit:
the COMMS protocol says only ORCH commits that file.
