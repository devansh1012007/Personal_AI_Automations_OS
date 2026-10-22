<!-- ORCH-DIGEST:START -->
agent: W2-REV-03 | scope: wave-2 **improve** cycle, delta review — the six agents' work at `git diff 96a6e60..HEAD -- src/ tests/ docs/adr/` (W2-IMPR-01 F-1, W2-IMPR-02 F-2/F-3, W2-IMPR-03 R-W2-203/F-4/F-7, W2-IMPR-04 R-W2-201, W2-IMPR-05 R-W2-202/F-5/F-6, W2-FIX-02 close-out) | status: DONE
verdict: SAFE TO CLOSE WAVE 2 WITH FOUR OPEN ITEMS NAMED — all five MAJORs are CLOSED by reversal, and the four findings below are MINOR/NIT and none of them is a defect in the shipped code
per_major: F-1 CLOSED | F-2 CLOSED | R-W2-201 CLOSED | R-W2-202 CLOSED (the two live tests UNVERIFIED — no live S3 server on this host) | R-W2-203 CLOSED
findings: 0 BLOCKER | 0 MAJOR | 4 MINOR | 3 NIT
reversals_run_myself: 9 — F-1 (4: full pre-fix revert, no-discriminator, role-only discriminator, fresh-uuid4 discriminator), F-2 (3: one per validator), R-W2-201 (1: the reviewer's A13 mutation + 9 single-pattern removals), R-W2-203 (1: the chain restored), plus the import lock (3 mutations) and R-W2-202's `__context__` unwrapping (1)
commands_run: `ruff check src tests` -> 0 | `mypy src` -> 0 errors, 138 files | `pytest tests/agents tests/models tests/validation tests/skills tests/observability tests/storage tests/conventions tests/integration -p no:randomly` -> 1733 passed, 234 skipped, 5 xfailed | `pytest tests/models` x5 -> 262 passed, 1 xfailed each time | import-cost measurement (18 fresh interpreters per tree, interleaved, minimum and median) | 6 ad-hoc probes (the wrapper's shape, the concurrency race, the LOCKDOWN branches, the five pattern semantics, the exception chain, the golden rows)
ownership_violations: none — no file in `src/` or `tests/` was edited; every mutation was made in a scratch copy under `/tmp/w2rev03/` and the repository's tree is byte-identical to `HEAD` (`git status --short` shows only the untracked brief)
not_verified: the two new **live** S3 tests (no `PAA_TEST_S3_ENDPOINT` on this host; the operator's `paa-stack-*` containers and database were not touched) | the 2496-pair differential harness and the 172 golden rows of the report (the harness is not in the tree; the **committed** 91-row golden pin was re-checked against the pre-fix source and matches) | the 126-of-147 reversal tally | PostgreSQL parity
independent_review: this report IS the independent review — I wrote none of the code under review, and I am neither the orchestrator nor either of wave 2's first reviewers
<!-- ORCH-DIGEST:END -->

# W2-REV-03 — the wave-2 delta review (improve cycle)

I did not write any of this. I am not the orchestrator and I am not either of
wave 2's first reviewers. My job was to find the thing that is still wrong.

**The short answer: the improve cycle did what it said.** All five MAJORs
survive a reversal, the two things that could have been faked (the gate's
identity and the keyword table) are pinned by tests that fail when the fix is
reverted, the lock still has teeth, and the deferrals are honest. What is
still wrong is four small things — none of them a defect in the shipped code,
three of them an inaccurate sentence in a test's docstring and one of them an
open MAJOR that the wave discovered about itself and did not schedule.

---

## VERDICT

**Wave 2 is safe to close.** Every one of the five MAJORs is closed *by
evidence*, not by a green gate: I reverted each fix in a scratch copy and
watched the new tests go red. Nothing I found is a BLOCKER or a MAJOR against
the code as it stands.

Four items must not be lost on the way out, and they are named below with the
file and the line: an inaccurate sentence about a security branch
(`tests/agents/test_policy.py:123`), the Anthropic path's still-open
pre-flight refusal (`tests/models/test_model_errors.py:431`), a MAJOR that this
very cycle discovered about the policy gate and left unpatched *and
unscheduled* (`reports/w2-improve/W2-IMPR-04-R-W2-201.md:337`), and the run's
work ledger, which does not yet list any of the open items.

---

## PER-MAJOR

### F-1 · MAJOR · a second human-gate request in one lineage was swallowed by the ledger's idempotency key · **CLOSED**

**The requirement.** A gate request must have an identity: *stable* across a
redelivery of the same request, *distinct* across different requests, and a
task that parks on a second gate must park again. ADR-0034 must say what the
code does.

**What I did.** Four reversals, in a scratch copy of the tree
(`/tmp/w2rev03/scratch`, `PYTHONPATH` pointed at the scratch's `src`; the
repository was never edited).

| # | the reversal | result |
|---|---|---|
| F-1-R1 | the **full pre-fix** `src/paa/agents/orchestrator.py` (from `96a6e60`) restored | **11 failed** — `TestGateIdentity` (7), `TestExactEventSequence::test_the_released_gate_path_emits_the_exact_event_sequence`, `tests/integration/test_end_to_end.py::TestHumanGate` (2). This matches W2-IMPR-01's claim of "11 tests fail against the pre-fix sources" exactly. |
| F-1-R2 | only the discriminator removed from the emit (`discriminator=None`) — the pre-fix *shape* | **8 failed**, and the ledger log shows the original defect verbatim: `ledger.duplicate_suppressed event_type=AWAITING_HUMAN_ATTESTATION` followed by `EXECUTION_STARTED` … `MUTATION_COMMITTED`. The task committed with the policy's gate unanswered. |
| F-1-R3 | the discriminator keeps the role but loses the ordinal (`gate:<role>`) | **1 failed** (`test_the_gate_request_minted_for_a_question_is_the_same_request`) — the "same stage asks a *new* question" half. Note: the F-1 scenario itself (two *different* stages) would still have been caught by the role alone, so the ordinal is what carries the second property, not the first. |
| F-1-R4 | the discriminator is a fresh uuid4 per write (unstable across a redelivery) | **3 failed**, including `test_a_redelivery_of_the_same_gate_request_is_suppressed` — the stability half is pinned. |

**The five properties the brief asked me to check, each answered:**

* *stable across a redelivery* — **yes.** The ordinal counts the questions of
  that stage that have been **answered**, so a redelivery recomputes the same
  number. Reversal R4 turns the redelivery test red.
* *distinct across different requests* — **yes.** Two stages are two different
  questions by construction; a stage's second question gets the next ordinal.
  Reversal R2 turns eight tests red.
* *can any automated path clear its own gate?* — **no.** `clear_human_gate`
  is called only from `src/paa/api/app.py:139`, `src/paa/tui/runner.py:182`
  and `src/paa/cli/main.py:192` — all human-initiated — and
  `test_a_task_parked_on_a_second_gate_is_not_walked_past` asserts that the
  automated path wrote exactly one `HUMAN_GATE_CLEARED` (the human's).
* *does a task that parks, is answered, resumes and meets a second gate park
  again?* — **yes**, and the *integration* test drives it through the real
  wiring: the real context builder, the real `PolicyRiskAgent`, the real
  ledger (`tests/integration/test_end_to_end.py::TestHumanGate`).
* *does ADR-0034 say what the code does?* — **yes.** The "what this ADR does
  not decide" section now carries the measured correction (the second gate was
  **dropped** and the task **committed**, not "the release works only because
  the policy agent happens to be deterministic"), and the Amendment states the
  mechanism, the two properties and the rollback. I re-read it against the
  code; the record and the code agree.

**One behaviour change that nobody asked for, which I judged acceptable:**
`clear_human_gate`'s `resume_phase` default moved from `TaskPhase.EXECUTING`
to *derived from the stage* — a context-builder release now resumes at
`PLANNING`. It is in the ADR's Amendment and pinned by
`test_the_release_resumes_the_task_where_it_parked`, and it is a strict
improvement (the release now agrees with the phase map). I note it because it
is a change to the operator-visible payload, and because the *pre-fix*
default was itself the behaviour the old ADR-0034 described. Not a finding.

### F-2 · MAJOR · the malformed-keyword rule reached one of three JSON-Schema validators · **CLOSED**

**The requirement.** All three validators read **one** table; a malformed
keyword is *reported* in all three (never a raw `TypeError`, never silently
dropped); a **valid** schema's behaviour is unchanged.

**One table — structurally verified.** `paa.core.json_schema` imports nothing
from this project (only `re`, `dataclasses`, `typing`), and all three
validators import from *it*:

```
src/paa/models/base.py:76              from paa.core.json_schema import SchemaError, schema_shape_errors
src/paa/skills/contracts.py:44         from paa.core.json_schema import JsonSchema, schema_shape_errors
src/paa/validation/schema_validator.py:58  from paa.core.json_schema import (KEYWORD_SPECS, JsonSchema, SchemaError, ...)
```

No validator imports another. The lock at
`tests/conventions/test_import_conventions.py:1467` pins all of it, and I
mutated it three ways (below).

**One reversal per validator** — the rule is independently pinned three times:

| # | the reversal | result |
|---|---|---|
| F-2-R1 | `models/base.py::validate_against_schema` stops checking the schema's shape | **70 failed** — every `test_a_malformed_keyword_is_reported_and_never_raised_as_a_type_error` case, all 27 `test_all_three_validators_refuse_the_same_malformed_keyword` cases, the Anthropic path, and the whole skills/validation suites' malformed-keyword cases. |
| F-2-R2 | `skills/contracts.py::schema_errors` stops refusing a malformed keyword | **56 failed** — the skills suite's per-keyword cases, `test_the_malformed_keyword_is_named_in_the_error`, `test_a_contract_turns_a_malformed_keyword_into_a_contract_error`. |
| F-2-R3 | `validation/schema_validator.py::validate_instance` stops reporting the schema's shape | **110 failed** — including `test_a_malformed_keyword_is_reported_and_not_applied` and `test_a_schema_with_a_non_string_key_is_reported_not_raised`. |

**A valid schema's behaviour is unchanged — verified against the pre-fix
source, not against a claim.** The committed golden pin in
`tests/models/test_model_errors.py` holds **80** `(schema, payload, expected)`
rows and **11** minimal-instance rows. I ran every one of them against
`96a6e60:src/paa/models/base.py`:

```
golden schema rows: 80
golden minimal rows: 11
schema rows differing from the pre-fix source: 0
minimal rows differing from the pre-fix source: 0
```

**The import cost — re-measured myself, with the method the brief names**
(fresh interpreters, minimum taken). I built a *pre-move* tree by pointing the
model layer's import at `paa.validation.schema_validator` (one line) and a
*pre-F-2* tree by making that import lazy:

| tree | `paa.*` modules after `import paa.models.base` |
|---|---|
| pre-F-2 (the table not read by the model layer) | **16** |
| pre-move (the table read from `paa.validation`) | **33** |
| HEAD (the table read from the leaf) | **17** |

16 modules removed, 1 added — **exactly** W2-IMPR-02's and W2-FIX-02's
figure, module for module. The time, 18 fresh interpreters per tree,
interleaved:

| measurement | min | median |
|---|---|---|
| the **move** (pre-move → HEAD), `paa.models.base` | **−20.1 ms** | −22.8 ms |
| the **move** (pre-move → HEAD), `paa.models` | **−18.3 ms** | −16.0 ms |
| the **edge** (pre-F-2 → pre-move), `paa.models.base` | **+19.7 ms** | +23.2 ms |

W2-FIX-02 reported "−19.6 to −20.4 ms on `paa.models.base`"; I measured
−20.1 ms. **The claim is real.**

### R-W2-201 · MAJOR · six of the nine irreversible-operation patterns were uncovered · **CLOSED**

**The requirement.** All nine have a case that fails when that pattern is
removed; the inventory is derived from the source; the tests go through the
**policy agent's own decision**.

* **The reviewer's own A13 mutation** (six patterns deleted at once) now
  fails **21** tests in `tests/agents` + `tests/integration`, naming the
  pattern that was removed in each id. Before the fix it survived with the
  suite green.
* **9 of 9 individually.** I removed each pattern from
  `_IRREVERSIBLE_PATTERNS` in turn and ran the agents + integration suites
  each time. **Every one of the nine produced at least one failure**, and
  the failing set always named the pattern that was removed — e.g. removing
  `\bgit\s+push\s+.*--force\b` fails
  `test_the_pattern_refuses_the_command_that_trips_it[force push]`,
  `test_the_pattern_raises_the_risk_outside_safe[force push]` and
  `test_the_inventory_is_complete`. This reproduces W2-IMPR-04's 9/9
  independently.
* **The inventory is derived from the source**, by an AST sweep of the policy
  module's own text (`_patterns_in_source`), and the sweep *refuses* rather
  than skips: a row that is not a literal `(pattern, label)` pair raises.
  `test_the_sweep_reads_the_table_the_gate_runs` asserts the sweep's result
  **is** the imported table, so the check cannot pass against a table the
  gate does not run.
* **The tests go through the gate's own verdict** — `PolicyRiskAgent.handle`
  and the verdict's *reason* names the pattern's label, and every command is
  checked to trip exactly **one** pattern (using the gate's own `_matches`,
  not a copy). The alternations carry a command per branch
  (`DROP TABLE` / `DROP DATABASE` / `DROP SCHEMA`), and the SQL rules carry
  both cases.

**One thing the reviewer should know about the near-miss case.** The
`test_a_qualified_delete_is_not_an_unqualified_delete` test asserts that
`DELETE FROM audit_log WHERE id = 1` is *allowed* under SAFE. That is the
current rule, and it is pinned — but see F-4 below: the rule it pins has a
hole in a different direction.

### R-W2-202 · MAJOR · the S3 report's mutation 11 did not reproduce and the ADR-0032 correction it proposed was false · **CLOSED (the record) — the two new live tests are UNVERIFIED**

**The requirement.** ADR-0032 and ADR-0033 now say what is **true**; the
*code* matches their intent; the two live tests that were added actually fail
when the thing they cover breaks.

* **The record is now true.** ADR-0032's decision 4 says plainly that
  `upload_fileobj` (the call `put_stream` makes) **does not wrap**, that
  boto3's `upload_file` (the filename variant) does, that it does so
  **without chaining**, and that the driver's error is therefore the
  wrapper's implicit `__context__`. The Context section no longer claims
  `put_stream` was handed a wrapper. ADR-0033's section agrees, and its
  title no longer claims "two **independent** servers" — a claim R-W2-204
  showed the suite cannot make. Three comments in `minio_backend.py` now say
  the same true thing. The ADR carries a dated Amendment that says what was
  wrong and what changed, and states plainly that **no behaviour changed**:
  the code was already right.
* **The code matches their intent — measured, without a server.** I built
  the wrapper exactly the way boto3 builds it (raised inside an
  `except ClientError` block, no `from`) and put it through the store's own
  translator:

  ```
  wrapper.__cause__ : None
  wrapper.__context__: ClientError
  wrapper.response  : None
  raw      -> AccessDenied  s3_code='AccessDenied' http=403
  wrapper  -> AccessDenied  s3_code='AccessDenied' http=403
  ```

  The two shapes answer **alike**, which is what the corrected decision 4
  says and what the old record got wrong.
* **The reversal: the mechanism the live test covers is load-bearing.**
  Stopping `_driver_cause` from following the `__context__` degrades the
  wrapper to `BlobFailure` (`s3_code=None`) and turns **5** unit tests red —
  `test_a_wrapper_is_unwrapped_through_its_context`,
  `test_the_upload_wrapper_is_unwrapped`,
  `test_the_two_write_paths_report_the_same_failure`,
  `test_every_transport_path_reports_the_vocabulary`,
  `test_a_missing_bucket_raises_from_every_method`. So the *property* the
  live test asserts is genuinely pinned by the tree; the live test itself is
  an end-to-end confirmation of it against a real server.
* **UNVERIFIED: the two new live tests.** `test_the_wrapping_upload_route_answers_alike`
  and `test_the_two_servers_are_distinguishable` need a live S3 server; no
  `PAA_TEST_S3_ENDPOINT` is set on this host and I did not start or stop the
  operator's `paa-stack-*` containers. I read both, and the first one's
  assertions are load-bearing on the code I mutated above (it asserts the two
  routes answer alike, and the wrapped route's answer depends on the
  `__context__` — which is exactly what degrades when I remove it). The
  second one is R-W2-204's answer, and its assertion shape (two header-name
  fingerprints must differ) is the only claim in the file that can be false.

### R-W2-203 · MAJOR · the key survived on the exception's `__cause__` and in the traceback · **CLOSED**

**The requirement.** `__cause__` and `__context__` are empty, the rendered
traceback is clean, and the claim in the test file matches what is true.

**The reversal.** Restoring the chain (`raise … from failure` /
`from local_error` on both router paths) turns **2** tests red:
`test_the_local_only_path_still_carries_no_key` and
`test_the_no_frontier_path_carries_no_key_either`.

**Measured on the current tree** (the router's own error, rendered):

```
cause   : None
context : None
suppress_context__: True
key in traceback  : False
key in str(e)     : False
key in details    : False
```

and the rendered traceback ends
`… underlying_body_excerpt='invalid api key: ***REDACTED***', underlying_error='ModelUnavailableError', underlying_message='model endpoint returned HTTP 401' …`
— the diagnosis survives as **data**, and no exception rides along.

**The claim in the test file matches what is true.** The file's docstring was
rewritten to say exactly what is claimed (no key reaches a log line, a ledger
payload, or *an error the router raises* — its message, its details, its
chain and its rendered traceback) and, importantly, to say what is **not**
claimed: *"a provider that raises an error carrying its own key still has
that key in that error. What the router guarantees is about the errors **it**
raises."* That is the honest boundary, and I could not find a path that
breaks it: the router catches only `ModelUnavailableError` /
`StructuredOutputError` (both `PaaError`s that scrub their own details at
construction), and anything else propagates unchanged — the provider's own
exception, not the router's.

**The cost, judged.** The provider's **stack frames** are lost; its type
name, message and every detail are kept, in the scrubbed details. The
trade is deliberate, documented in the method's docstring, and stated in the
agent's report. I agree with it: a chain is the one object in this story that
can hold a credential the router has no way to recognise, and a guarantee the
router cannot keep is not a guarantee. One honest note — **the key no longer
leaks through the chain even if the chain came back**, because F-3's fix
scrubs `ModelUnavailableError`'s details at construction. So the chain
removal is *structural* insurance rather than the thing that is currently
carrying the leak; the test still pins it (the `__cause__ is None`
assertion is what fails under the reversal, not a key leak), and that is the
right thing for it to pin.

---

## FINDINGS

### 1 · MINOR · `tests/agents/test_policy.py:123` · the companion test's docstring says a live security branch "cannot fire"; it does

> "The air-gap branch in ``_check_lockdown`` **cannot fire**."

Only the **declared-permission** half of that branch is unreachable. The
`_NET_PATTERNS` half fires, and it is the half that carries the air-gap
promise:

```
declared egress (no net token)   -> STATUS_BLOCKED permissions not granted in LOCKDOWN: ['PERM_NET_EGRESS']
declared external-write          -> STATUS_BLOCKED permissions not granted in LOCKDOWN: ['PERM_EXTERNAL_WRITE']
undeclared net command (curl)    -> STATUS_BLOCKED network egress attempted under LOCKDOWN
```

The xfail's own `reason` string is **precise** ("a plan that declares an
egress or external-write permission"), so the inaccuracy is only in the
sibling test's docstring — but it is the wrong way round for a reader. A
reader who believes it would conclude that the LOCKDOWN air-gap promise rests
on the planner's honesty, which is the exact opposite of what
`src/paa/agents/policy.py:211` says ("Trusting the declaration alone would
make the guarantee a matter of the planner's honesty"). **Fix:** reword to
"the **declared-permission** half of the air-gap branch cannot fire".

### 2 · MINOR · `tests/models/test_model_errors.py:431` · the Anthropic path still sends a broken schema to the frontier model, and the cycle is what made the two disagree

`AnthropicProvider.complete_structured` (`src/paa/models/anthropic_provider.py:152`)
overrides the base and does **not** refuse pre-flight, so the malformed
schema is sent as the tool's `input_schema` and the frontier model is asked
about it. The base now refuses pre-flight
(`src/paa/models/base.py:888`), so the two implementations of
`complete_structured` — one method, two behaviours — now disagree, and the
disagreement was **introduced by this improve cycle**.

This is honestly handled: the defect is reported (COMMS 07:22:09Z and
08:33:46Z), it is pinned by a **strict** xfail that will go red the day the
override refuses pre-flight, and the exact three-line fix is named. I judge
the deferral correct (the file was outside the write-set, and the fix is
someone else's file). I record it as a finding because it is an **open item
that must land in wave 3**, not because the handling is wrong.

### 3 · MINOR · `reports/w2-improve/W2-IMPR-04-R-W2-201.md:337` · a MAJOR hole in the SAFE gate, discovered by this cycle, deferred correctly but not scheduled

The rule `\bDELETE\s+FROM\s+\w+\s*(;|$)` is anchored on `;` or the **end of
the rendered plan**, so the same command is refused or allowed depending on
what follows it:

```
DELETE FROM users as the ONLY step          -> STATUS_BLOCKED  (unqualified delete)
DELETE FROM users followed by another step  -> STATUS_APPROVED
DELETE FROM users; followed by another step -> STATUS_BLOCKED  (unqualified delete)
```

A whole-table delete in SAFE mode is allowed if it is not the last step of
the plan. **The report is accurate** — I reproduced every one of its cases
(F-1 to F-5 below) — and **the deferral is the right call**: each candidate
patch trades a false negative for a false positive over prose, the gate is a
security boundary, and the ADR that would record a new rule shape is out of
the write-set. My concern is the *scheduling*, not the judgement: this is a
MAJOR by the report's own rating, it was found by this wave, and nothing in
the run's ledger asks for the task that fixes it. **Fix:** a plan item for
"the policy gate's rule set", with a corpus of real command spellings to
adjudicate against — which is what the report itself recommends.

### 4 · MINOR · `docs/agent-ops/20261022-w6-w7-w9/01-work-ledger.md` · the run's ledger does not list any of the four open items

The wave-2 improve cycle left four open items — the Anthropic pre-flight
refusal, the dead egress branch, the five pattern-semantics findings, and
the MAJOR in finding 3 above. All four are recorded in the agents' reports
and in `COMMS.md`, and the close-out's digest carries them. The run's
**"Deferred / open items"** section does not mention any of them, and wave 2's
section of the ledger is still the one-line placeholder. A reader of the
ledger — which is what a wave-3 agent will be pointed at — would not know any
of it is open. **Fix:** four lines in the ledger's deferred section when
wave 2 is closed.

### 5 · NIT · `src/paa/core/json_schema.py:15` (and `src/paa/models/base.py:59,352`) · the same import edge is quoted as "26 ms" and as "19 ms"

`src/paa/core/json_schema.py:15` and `src/paa/models/base.py:59` say the edge
cost *"16 extra modules and about 26 ms"*; the lock's own test docstring
(`tests/conventions/test_import_conventions.py:1510`) says the same edge was
*"16 modules and about 19 ms"*. Both numbers were measured — mine: the edge
costs **+19.7 ms** (min) / **+23.2 ms** (median) and the move gives back
**−20.1 ms** (min) / **−22.8 ms** (median) — and they are measurements of two
*different* edges by two different methods, so neither is wrong. What is
missing is a sentence saying so; a reader who finds "26" and "19" for the
same edge in the same tree concludes that one of them is a stale number.
**Fix:** one clause in each — "about 26 ms as the edge was added, about 20 ms
as the move gave it back".

### 6 · NIT · `src/paa/agents/orchestrator.py:595` · the human-released clearance records no risk score

The policy stage's normal clearance records `{"decision": "STATUS_APPROVED",
"risk_score": …}`; the clearance a *human* grants records
`{"decision": "STATUS_APPROVED", "reason": "human attestation granted",
"released_by": "human", "gate": …}`. The score that caused the gate is not in
the lineage at all — the policy's `reason` is, so a reader can tell *why* the
plan was gated, but not *how risky* it was judged. Nothing in the tree reads
`risk_score` off the payload, so this is a record-completeness nit, not a
defect. **Fix:** carry the gate's risk score into the clearance payload if
the audit is meant to answer "how risky was the plan a human approved".

### 7 · NIT · (the brief's summary of W2-IMPR-02's claim) · "24 silently dropped keywords" is not the report's number

The brief's §3.3 summary says *"24 silently dropped keywords"*. The report
says **20**, and I measured the pre-fix source myself, keyword by keyword,
with a payload that reaches each keyword:

```
TypeError                    n=  6  ['minItems','maxItems','minLength','maxLength','minimum','maximum']
AttributeError               n=  1  ['properties']
no-error (silently ignored)  n= 19
error (a WRONG answer)       n=  1  enum -> "$: 'a string' is not one of 'abc'"   (a substring test)
```

6 + 1 + 20 = 27 ✓. The report's table and my measurement agree; the brief's
"24" is the only number that does not. (The report lists `enum` in both the
"silently ignored" row and the "wrong answer" row — a small looseness in the
report, not a wrong count.)

---

## CLAIMS CHECKED

Every claim below was re-measured on this host. The output is quoted, not
paraphrased.

| the claim | where | verdict | what I measured |
|---|---|---|---|
| **11 tests fail against the pre-fix tree** (F-1) | W2-IMPR-01 | **CONFIRMED** | Full `96a6e60:src/paa/agents/orchestrator.py` restored in a scratch copy → **11 failed**. |
| **7 mutations each caught** (F-1) | W2-IMPR-01 | **PARTLY CONFIRMED** | I ran 4 of the 7 (full revert, no-discriminator, role-only, fresh-uuid4). All 4 caught. The other 3 are not claimed as checked. |
| **9/9 patterns, the reviewer's A13 mutation now failing 14 tests** | W2-IMPR-04 | **CONFIRMED, with a different number** | A13 (six patterns deleted) → **21 failed** in `tests/agents` + `tests/integration` (the report counted 14 in a narrower scope). **Each of the nine patterns removed one at a time → at least one failure every time**, the failing id naming the pattern removed. |
| **6 targeted mutations each caught by exactly one test** | W2-IMPR-03 | **PARTLY CONFIRMED** | I ran 1 (the exception chain restored) → caught by exactly 2 tests. The other 5 are not claimed as checked. |
| **230 passed × 5 consecutive runs for `tests/models`** | W2-IMPR-03 | **CONFIRMED in substance, the number is stale** | 5 consecutive runs → **262 passed, 1 xfailed** each time (3.30–3.68 s). The tree has 32 more tests than the report's figure. |
| **2496 (schema, payload) pairs with 0 differences** | W2-IMPR-02 | **UNVERIFIED** | The harness is not in the tree. What *is* in the tree — the 91 committed golden rows — I ran against `96a6e60:src/paa/models/base.py`: **0 differences**. |
| **172 golden rows matching the pre-fix source** | W2-IMPR-02 | **PARTLY CONFIRMED** | The committed pin is **91** rows (80 schema + 11 minimal-instance), not 172; all 91 match the pre-fix source exactly. The 172 are the harness's, which is not committed (the earlier R-W2-205 finding still stands). |
| **126 of 147 cases failing without the fix** | W2-IMPR-02 | **UNVERIFIED** | The harness is not in the tree and the case list is not either. What I can say: the *committed* tests that cover the same ground fail in bulk under each of the three reversals (70 / 56 / 110). |
| **a 27-keyword matrix: 6 raw `TypeError`s, 1 `AttributeError`, 24 silently dropped** | W2-IMPR-02 | **CONFIRMED except the "24"** | Measured against the pre-fix source: **6 TypeError** (minItems, maxItems, minLength, maxLength, minimum, maximum), **1 AttributeError** (properties), **19 silently ignored** + **1 wrong answer** (enum → substring test). The report says 20; the brief says 24. 6+1+20 = 27 ✓. |
| **an import-cost measurement of +26.4 ms and 16 new modules** | W2-IMPR-02 | **CONFIRMED** | 16 modules removed, 1 added (16 → 33 → 17 `paa.*` modules) — exact. Time: the edge costs **+19.7 ms** (min) / **+23.2 ms** (median) on this host, interleaved, 18 fresh interpreters per tree. The move gives back **−20.1 ms** (min) / **−22.8 ms** (median), which matches W2-FIX-02's −19.6/−20.4 ms. |
| **the ledger cannot suppress a second audit record** (F-4) | W2-IMPR-03 | **CONFIRMED** | Two routers, one correlation, `asyncio.gather` → **2** `MODEL_ESCALATED` events, 2 distinct discriminators (`escalation:<event uuid4 hex>`), 2 distinct `state_version` (1 and 2), 2 distinct idempotency keys. |
| **the lineage's order is the ledger's `state_version`** | W2-IMPR-03 | **CONFIRMED** | `src/paa/ledger/store.py:134` takes `lock_correlation` before reading the tip, and `:149` assigns `state_version = version + 1` inside that lock — so it is dense, per-correlation and a **total order** under concurrency. The old read-then-count ordinal had no such property (W2-REV-01 measured two routers taking ordinal 1). |
| **the five strict xfails are real, accurate and loud** | this cycle + W2-REV-02 | **CONFIRMED, with one inaccuracy in a sibling docstring** | All five are `strict=True` and all five **xfail** (not xpass). See finding 1 for the one inaccurate sentence. Each names the file and the function that must change; each would turn the suite **red** the day the defect is fixed, because `strict=True` makes an XPASS a failure. |
| **the five pattern-semantics findings are accurate and correctly deferred** | W2-IMPR-04 | **CONFIRMED** | Every reproduction in §4 holds exactly as written (measured through the policy agent's own verdict): `DELETE FROM users` + a following step → **STATUS_APPROVED**; `git push -f` → **APPROVED**; `rm -rfv` → **APPROVED**; `rm --recursive --force` → **APPROVED**; `DELETE FROM public.users;` → **APPROVED**; `TRUNCATE events;` → **APPROVED**; `os.removedirs` → **APPROVED**; `git clean -fd` → **APPROVED**; `DROP MATERIALIZED VIEW` → **APPROVED**. "Not a test-coverage task's decision" is the right call for the *patching*; see finding 3 for the scheduling. |
| **the import lock still describes the tree and still has teeth** | W2-FIX-02 | **CONFIRMED** | The lock derives its module list from the tree (`_module_files()`), so `paa.core.json_schema` was picked up with no edit. Three mutations, three red: the leaf gaining a first-party import → 1 failed; the skills validator reading the table through the model layer → 5 failed; the model layer reading the table from `paa.validation` → 2 failed, with the error naming all 16 dragged-in modules. |
| **ADR-0034 now says what the code does** | W2-IMPR-01 | **CONFIRMED** | The "what this ADR does not decide" section now states the *measured* behaviour (dropped gate → committed task) and the Amendment states the mechanism, both properties, the rollback and the cost. It agrees with `src/paa/agents/orchestrator.py`. |
| **ADR-0032 / ADR-0033 now say what is true** | W2-IMPR-05, W2-FIX-02 | **CONFIRMED** | Both are dated and attributed; the mechanism is stated correctly; the three comments in `minio_backend.py` agree; and the *code* was already right (my probe: raw and wrapped shapes both → `AccessDenied`, `s3_code='AccessDenied'`, `http=403`). |

---

## VERIFIED

Everything below was run by me, on this host, against this tree.

**The gate.**

```
$ .venv/bin/python -m ruff check src tests
All checks passed!

$ .venv/bin/python -m mypy src
Success: no issues found in 138 source files
```

**The scoped suites** (the brief's list, `pytest-randomly` disabled so the
order is fixed):

```
$ .venv/bin/python -m pytest tests/agents tests/models tests/validation \
      tests/skills tests/observability tests/storage tests/conventions \
      tests/integration -p no:randomly
1733 passed, 234 skipped, 5 xfailed in 56.47s
```

**`tests/models`, five consecutive runs** (the "five green runs" claim):

```
262 passed, 1 xfailed in 3.30s
262 passed, 1 xfailed in 3.47s
262 passed, 1 xfailed in 3.68s
262 passed, 1 xfailed in 3.42s
262 passed, 1 xfailed in 3.36s
```

**The five xfails**, by name (`pytest -rx`): the LOCKDOWN air-gap egress
check, the Anthropic pre-flight refusal, the scanner's `RecursionError`, the
created file's trailing newline, and the empty artifact's PASS. All strict,
all reproducing, all with an accurate `reason`.

**The reversals** — nine, all in a scratch copy under `/tmp/w2rev03/`, the
repository's `src/` and `tests/` **never** edited (`git status --short` shows
only the untracked brief, and `git diff --stat HEAD -- src tests` is empty):

| # | the mutation | red |
|---|---|---|
| F-1-R1 | the full pre-fix orchestrator | 11 tests |
| F-1-R2 | the gate event's discriminator removed | 8 tests |
| F-1-R3 | the discriminator's ordinal removed | 1 test |
| F-1-R4 | the discriminator is a fresh uuid4 | 3 tests |
| F-2-R1 | the model layer stops checking the schema's shape | 70 tests |
| F-2-R2 | the skills layer stops refusing a malformed keyword | 56 tests |
| F-2-R3 | the runtime gate stops reporting the schema's shape | 110 tests |
| R-W2-201 | the reviewer's A13 (six patterns deleted) | 21 tests |
| R-W2-203 | the router chains the provider's exception again | 2 tests |
| R-W2-202 | `_driver_cause` stops following the `__context__` | 5 tests |
| lock-L1 | the leaf gains a first-party import | 1 test |
| lock-L2 | the model layer reads the table from `paa.validation` | 2 tests |
| lock-L3 | the skills validator reads the table through the model layer | 5 tests |

**The import-cost measurement** (fresh interpreters, interleaved, 18 runs per
tree):

```
paa.models.base: pre  min=215.7 med=222.6   (pre-move)
paa.models.base: post min=195.6 med=199.8   (HEAD)      delta(min)=-20.1 ms  delta(med)=-22.8 ms
paa.models:     pre  min=214.4 med=218.2
paa.models:     post min=196.1 med=202.2                delta(min)=-18.3 ms  delta(med)=-16.0 ms
paa.* modules:  16 (pre-F-2) -> 33 (pre-move) -> 17 (HEAD)
```

**The concurrency claim**, measured (two routers, one correlation, gathered):

```
escalation events: 2
  state_version= 1  discriminator= escalation:6905ef9a…  event_id= 6905ef9a…
  state_version= 2  discriminator= escalation:78846e7a…  event_id= 78846e7a…
distinct discriminators: 2   distinct state_versions: 2   distinct idempotency keys: 2
```

**The diff, read as a diff.** Every changed file is inside some task's
write-set; there is no scope creep in the file list. The only **removed**
assertions in the whole test diff are four, and each is a consequence of an
intended change, not a weakening: three are the escalation ordinal's
(`assert _ordinal(...) == 1`, and `== [1, 2]` twice — the ordinal is gone),
and one is
`assert _string_constants(_module_level_value("SUPPORTED_KEYWORDS")) == set(SUPPORTED_KEYWORDS)`,
which was **re-pointed** at the module that now holds the supported-keyword
set (`tests/validation/test_schema_validator.py:583`) with its meaning
unchanged. No test was deleted, no `skip` was added, no assertion was
loosened. `policy.py`'s change is a pure refactor (`re.search(...) is not
None` → `_matches(...)`), with identical semantics.

---

## UNVERIFIED

* **The two new live S3 tests** — `test_the_wrapping_upload_route_answers_alike`
  and `test_the_two_servers_are_distinguishable`. No `PAA_TEST_S3_ENDPOINT` is
  set on this host; the operator's `paa-stack-*` containers and database were
  not started, stopped or touched, as instructed. I verified the *mechanism*
  the first one covers (the `__context__` unwrapping) without a server, and
  the first one's assertions are load-bearing on it; the second one's
  assertion (two header-name fingerprints must differ) can only be checked
  against two real servers.
* **The 2496-pair differential harness and the 126-of-147 reversal tally** —
  the harness is not in the tree. The *committed* 91-row golden pin was
  re-checked against the pre-fix source and matches exactly.
* **W2-IMPR-03's other five targeted mutations and W2-IMPR-01's other three**
  — not run by me.
* **PostgreSQL parity** — no `PAA_TEST_POSTGRES_DSN`; the operator's database
  was not touched. The `state_version` ordering claim was verified by reading
  `src/paa/ledger/store.py` (the per-lineage lock, the dense assignment) and
  by measuring it on SQLite, not on a live PostgreSQL.
* **The full repository suite** — the standing orders forbid it and five
  agents were active in this tree during the improve cycle; I ran the brief's
  eight scoped directories.

---

## NOT A FINDING

Things I looked at and am satisfied with, so the orchestrator knows they were
looked at.

* **The gate's identity scheme is right.** `gate:<role>:<answered-ordinal>`
  satisfies both halves of the ledger's idempotency key at once — stable
  across a redelivery (the ordinal counts *answered* questions) and distinct
  across requests (the role distinguishes stages, the ordinal distinguishes
  a stage's second question). Counting the questions *asked* instead would
  have broken the first half, and the docstring says so.
* **"Each stage consults its own question" is the right design.** A stage
  that asks again while its question is open would replace a question for a
  human with a second opinion — either a block or a clearance no human ever
  saw. `test_the_open_policy_gate_holds_the_task_against_a_policy_agent_that_changes_its_mind`
  is exactly the right test for it.
* **The release naming the question it answers** is the right shape: without
  it, "the gate" is ambiguous as soon as a lineage has asked twice, and two
  releases would collide on the idempotency key.
* **`clear_human_gate` refusing to release a gate nobody asked** is right:
  the projection says the task is waiting but the lineage holds no open
  question, and inventing one would be a lie the ledger would record. The
  API route turns it into a 409 (`src/paa/api/app.py:143`), the CLI prints it
  and exits 1, and the TUI catches broadly (`src/paa/tui/runner.py:227`).
* **The one redaction rule.** `redact_detail` now exists once, in
  `src/paa/models/base.py:275`, and the router calls it (`router.py:625`,
  `router.py:676`); the router's private copy is gone, and
  `test_the_model_layer_and_the_router_share_one_redaction_policy` pins that
  there is only one.
* **`MalformedSchemaError` is deliberately *not* a `StructuredOutputError`**,
  so a broken contract cannot travel the escalation path and record a
  `MODEL_ESCALATED` for reasoning material that never left the machine. That
  is a good call and the class docstring argues it.
* **The router's fail-closed default** (`LOCKDOWN` when the mode is not
  known) is the right default for a method whose whole purpose is to look
  safe, and `get_model_router` now hands the *whole* `Settings` over so a
  real deployment's mode is the one that is used. `test_the_factory_hands_the_deployment_mode_to_the_router`
  and `test_the_factory_fails_closed_when_the_caller_names_no_mode` pin both
  ends.
* **The dead egress branch was left unpatched, and that call is right.**
  Making it reachable means reordering the checks tuple
  (`src/paa/agents/policy.py:156`), which changes the *reason* the ledger
  records for a LOCKDOWN plan that declares an ungranted egress permission.
  The plans are refused either way; only the recorded reason differs, and
  which refusal should win is an operator's policy decision, not a coverage
  fix. It is reported (COMMS 09:23:03Z), the exact change and its
  consequence are written down, a companion test pins today's reason, and a
  strict xfail fails the day the branch starts answering. That is the honest
  shape of a deferral. (Finding 1 is about a *sentence* in the companion
  test's docstring, not about the decision.)
* **The `state_version` ordering under concurrency is well-defined** — the
  store takes the per-lineage lock *before* reading the tip and assigns the
  version inside it (`src/paa/ledger/store.py:134,149`). The discriminator
  no longer needs a lineage read at all, so the audit write has no new
  failure point and no O(lineage) cost, and the "two writers took the same
  ordinal" race is gone.
* **The `POLICY_CLEARED` the human grants** is the right shape for the
  projection: `decision = "STATUS_APPROVED"`, `released_by = "human"`, and
  the gate it answers — so the stage is skipped from then on and the ledger
  says who decided, not which agent.
* **The three pre-existing xfails are unaffected by this cycle** —
  `src/paa/validation/{ast_scanner,patch,engine}.py` are not in the improve
  cycle's diff at all, so their reasons (and the line numbers they cite:
  `ast_scanner.py:364-392`, `UnifiedDiffValidator._plan_file`,
  `DeterministicValidationEngine.validate`) still describe the code.
* **No secrets, tokens or PII in the diff** — the test key is the fake in
  `tests/models/conftest.py`, and the S3 credentials come from the
  environment.
* **The scope of the whole diff** — every file changed is inside some task's
  write-set, with the two `NEED`s and two `DEFERRED-DEPENDENCY`s (the
  `models/__init__.py` re-export, the router's private redaction copy, the
  Anthropic pre-flight refusal, the table's leaf home) all resolved by
  W2-FIX-02 or honestly deferred.
