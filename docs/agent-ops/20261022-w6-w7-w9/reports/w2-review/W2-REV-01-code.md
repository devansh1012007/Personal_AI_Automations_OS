<!-- ORCH-DIGEST:START -->
agent: W2-REV-01 | task: independent code review of wave 2 | status: DONE
scope: `git diff 4c9300d..HEAD -- src/ docs/adr/ pyproject.toml` (wave 2: T-002, T-005, T-006, T-007, T-008, plus the W2-FIX-01 lock removal)
code_commits_reviewed: 96a6e60..HEAD (7ca81c8 8527fe7 9a92f19 4808982 514cfad 487c6d5 9eced6f be649ad 8c189d1 80ba09b)
findings: 7 (2 MAJOR, 3 MINOR, 2 NIT)
files_written: this report only — no source file was edited
commands_run: ruff (0), mypy (0), the six scoped suites, the full suite, the S3 live suite's skip, two lock mutations, five schema-validator probes, three router probes
unverified: the live S3 suite against real servers (no server configured on this host; the operator's `paa-stack-*` containers were not touched)
<!-- ORCH-DIGEST:END -->

# W2-REV-01 — independent code review of wave 2

I did not write any of this code. I tried to break it.

---

## VERDICT

**Not safe to keep exactly as it is.** The wave is careful, the gate is green, the
reports are unusually honest about their own limits — and two of the five tasks
are **incomplete in the exact way the brief asked me to look for**: a fix for
the one instance the agent found rather than for the class (T-005), and a
security gate that is *not* a gate on the second question (T-006). Both are
fixable in small, well-understood edits; neither is a rewrite.

The rest of the wave holds up under inspection: the privacy promise is closed
on the paths the wave claims, the six observability fixes are root-cause fixes
and not symptom moves, the import lock still has teeth (I mutated it twice),
and the S3 live suite really does drive the real path.

---

## FINDINGS

### F-1 · MAJOR · `src/paa/agents/orchestrator.py:561` (and `:445`) · a second human-gate request in one lineage is silently swallowed, and the task commits with it unanswered

**What is wrong.** `AWAITING_HUMAN_ATTESTATION` carries no `discriminator` and
no `attempt`, so the ledger's idempotency key
(`correlation | event_type | attempt | discriminator`,
`src/paa/ledger/events.py:101`) is **identical for every gate request in a
lineage**. The second request in a lineage is therefore suppressed as a
duplicate, the projection never sees it, the phase stays `EXECUTING`, and the
orchestrator walks straight past the gate into execution and
`MUTATION_COMMITTED`.

This is *precisely* the defect class T-007 fixed in the same wave for
`MODEL_ESCALATED` (the per-router-instance `escalation_seq` counter): the
wave gave one event type a per-lineage discriminator and left the other
alone — and the other one is the *security* one.

**Evidence** (my probe, real `PolicyRiskAgent`, real `LedgerStore`; the plan
step carries `risk_profile=0.95`, which the real policy gates with *"step
exceeds the always-gate risk profile"*):

```
phase after first run: TaskPhase.AWAITING_HUMAN
[debug] ledger.duplicate_suppressed  event_type=AWAITING_HUMAN_ATTESTATION
                                      idempotency_key=a134c77e462ccdc2
events: [TASK_REQUESTED, TASK_QUEUED, CONTEXT_HYDRATION_REQUESTED,
         AWAITING_HUMAN_ATTESTATION, HUMAN_GATE_CLEARED, PLAN_COMPILED,
         EXECUTION_STARTED, CRITIQUE_CONCLUDED, EXECUTION_COMPLETED,
         MUTATION_COMMITTED]
phase after resume: TaskPhase.COMMITTED
```

A human answered question #1 (*"is the context sufficient?"*). The policy
agent then asked question #2 (*"is this plan risky enough to need a human?"*).
Question #2 was **dropped by the ledger** and the task committed. The ledger
trace shows no second question at all, so the audit trail says the policy
never asked.

**The report and the ADR characterise this wrongly.** `W2-IMPL-03-T-006.md`
§13.1 and `docs/adr/0034-hard-stop-escalation-parks-the-task.md` §"What this
ADR does not decide" both say the release *"works only because the policy
agent happens to be deterministic"* and that the risk is a non-deterministic
verdict *blocking* a task a human had approved. The measured behaviour is the
opposite and worse: the second gate is dropped and the task **commits**,
whether the policy is deterministic or not, and whether the second question is
the same question asked twice or a **different** question asked by a
**different** agent. (The policy-gate-only path — the pre-existing case — is
benign-ish because the second ask is the *same* question. ADR-0034's own
change is what makes the two-question case reachable.)

**Suggested fix.** Give the gate event a discriminator that identifies the
*question* (a per-lineage gate ordinal, or the gate's identity —
`"context"` vs `"policy"`), and record the policy's verdict so
`_check_policy` is not re-consulted after a release. Both halves are needed:
the discriminator stops the silent drop, the recorded decision stops the
re-ask. The `_last_gate_event` helper the wave already added is the natural
place to read the *open* question from.

---

### F-2 · MAJOR · `src/paa/models/base.py:442` (and `:430, :432, :451, :453, :420`) · the crash-fix was applied to one of three JSON-Schema validators; the model path still raises a raw `TypeError`

**What is wrong.** T-005's requirement was that *"a malformed schema keyword
crashes the gate instead of rejecting it"*. There are **three** JSON-Schema
validators in this tree and the wave fixed one. `paa/models/base.py`'s
`validate_against_schema` — the validator the **model** layer actually uses —
still raises.

**Evidence** (my probe, every keyword the module understands, malformed value):

```
CRASH type:      TypeError: '<' not supported between instances of 'int' and 'str'
CRASH minLength: TypeError: '<' not supported between instances of 'int' and 'str'
CRASH maxLength: TypeError: '>' not supported between instances of 'int' and 'str'
CRASH minItems:  TypeError: '<' not supported between instances of 'int' and 'str'
CRASH maxItems:  TypeError: '>' not supported between instances of 'int' and 'str'
CRASH minimum:   TypeError: '<' not supported between instances of 'int' and 'str'
CRASH maximum:   TypeError: '>' not supported between instances of 'int' and 'str'
CRASH properties: AttributeError: 'str' object has no attribute 'items'
```

and end-to-end through the real provider and the real router:

```
provider raised: TypeError '<' not supported between instances of 'int' and 'str'
router raised:   TypeError '<' not supported between instances of 'int' and 'str'
```

The consequences are worse than a crash in a validation helper: the
`TypeError` is **not** a `StructuredOutputError`, so it is not a *routable*
failure and the router's escalation policy never sees it; and it is **not** a
`PaaError`, so the orchestrator's `except PaaError` in `run()` does not catch
it either — no `EXECUTION_FAILED` is written and the task dies unhandled.
Both call sites are live: `ModelProvider.complete_structured`
(`src/paa/models/base.py:662`) and the Anthropic tool path
(`src/paa/models/anthropic_provider.py:186`).

The wave **disclosed** this, accurately, as `W2-IMPL-02-T-005.md` §13 F-5
("a different validator … has **no** guards and will raise `TypeError`").
That is to the agent's credit. It is still the *requirement* left un-met, and
the brief's instruction is explicit: *"a fix for the one the agent found is
not a fix."*

**Suggested fix.** Apply the same shape table to
`models/base.py::validate_against_schema`, or make it delegate to
`paa.validation.schema_validator.validate_instance` so there is one policy
rather than three. Raise the malformed keyword as a `StructuredOutputError` so
the router can route it and the orchestrator can record it.
(`src/paa/models/base.py` is T-007's file, and T-005's write-set was
`src/paa/validation/schema_validator.py` — so this needs an owner.)

---

### F-3 · MINOR · `src/paa/models/base.py:81` · the redaction rule was applied to `StructuredOutputError` and not to its sibling `ModelUnavailableError`

**What is wrong.** The wave's fix redacts `StructuredOutputError`'s
`schema_errors` **at construction**, and the class docstring now states the
rule: *"The scrub happens here, at construction, because 'remember to redact
at the raise site' is a rule that holds right up until the one path nobody
tested."* `ModelUnavailableError` has the identical structure and was not
given the treatment: it redacts its *message* (`base.py:82`) and carries
`**details` **verbatim**, and `PaaError.to_payload()` merges details
**verbatim** into a ledger payload (`src/paa/core/errors.py:52-54`).

**Evidence.** No leak is reachable *today* — every raiser in the tree happens
to scrub its own details (the HTTP layer scrubs `body_excerpt` with the
literal secret, `src/paa/models/_http.py:313,329`; the others carry only
names and key lists). But the escalation path's only remaining guard is
`redact(str(local_error))` (`src/paa/models/router.py:683`) — a *generic*
pattern match over the rendered text — and the local-only path's guard is
`_redact_detail` (`router.py:125-150`), the same generic patterns. A
credential in a format those three patterns do not know (a Gemini `AIza…`
key, a bare token) survives both.

**Suggested fix.** Scrub `**details` in `ModelUnavailableError.__init__` the
same way `StructuredOutputError` now does, so the guarantee lives at the
construction boundary on both classes; or state in the class docstring that
the details are the raiser's responsibility and make the router's scrub the
guarantee.

---

### F-4 · MINOR · `src/paa/models/router.py:714` and `:774` · the escalation ordinal is a decorative extra read, and a new failure point

**What is wrong.** The discriminator is now
`escalation:<ordinal>:<nonce>`, and the **nonce** is what guarantees
uniqueness; the ordinal is a human-readable label. But the ordinal is
*computed* by reading the whole lineage from the ledger on every escalation
(`router.py:714` → `router.py:774`).

Two consequences, both measured:

* **A new failure point.** A store whose `read_correlation` raises now aborts
  the escalation *before* the frontier call:
  `raised: RuntimeError db down`. That is fail-closed (the right direction) —
  but before this wave only a *write* error could do it. A transient read
  error now stops a call that would have succeeded, for a value that carries
  no information the event needs.
* **The ordinal is not a sequence under concurrency.** Two routers escalating
  the same correlation at the same moment both read ordinal 1:
  `['escalation:1:980eeb3d…', 'escalation:1:891eea28…']`. Both records
  survive (the nonce does its job — the audit trail is complete, which is
  the property that matters), but the "per-correlation ordinal" the change
  was made to establish holds only for *sequential* escalations. The two
  tests that assert `[1, 2]` (`test_escalation_audit.py:187, :229`) are
  sequential. The docstring says this honestly; the tests do not.

**Suggested fix.** Either drop the read and use `escalation:<nonce>` alone,
or read the ordinal best-effort and say in the docstring that the label may
repeat (which it already does) without making the read a hard dependency of
the write.

---

### F-5 · MINOR · `tests/conventions/test_import_conventions.py:43` · the lock's own docstring still documents the class that was removed

**What is wrong.** The wave removed class `"F"` (the family), the
`DEFERRED` map and the check that iterated it, and updated the file's
*body* (`TYPE_CHECKING_CLASS_KEYS` is now `{A,B,C,D,E}`; the "F" description
and the deferral comment are gone; the "Four of the five are mechanical"
wording was corrected). The **module docstring** was not:

```
* the **classification** half says every ``if TYPE_CHECKING:`` block left in
  the package is **classified** — a cycle, an optional extra, deliberate
  laziness, a guard that buys nothing, a cheap first-party import, or the
  family — so a block that was left behind was left behind for a stated
```

"or the family" names a class that no longer exists. The brief's own
standard applies verbatim: *"a lock that documents a deferral that no longer
exists is the drift the file exists to catch."*

**Suggested fix.** Drop `, or the family` from that bullet.

---

### F-6 · NIT · `docs/adr/0032-blob-error-vocabulary.md:138-148` and `src/paa/storage/coldlake/minio_backend.py:45-47, :249-254` · ADR-0032's decision 4 mis-states the mechanism, and the code repeats the error in two comments

**What is wrong.** ADR-0032 decision 4 says boto3's transfer manager *"catches
the driver's `ClientError` and raises an `S3UploadFailedError` instead"*.
That is true of `upload_file` (the *filename* variant) and **false of
`upload_fileobj`**, which is what `put_stream` calls. Independently
confirmed from the installed driver:

* `.venv/.../boto3/s3/inject.py:618-676` — `upload_fileobj` calls
  `future.result()` with **no** `ClientError` catch → the raw `ClientError`
  propagates, response intact.
* `.venv/.../boto3/s3/transfer.py:452-459` — only `S3Transfer.upload_file`
  wraps it.

The same claim is repeated in the code's own module docstring
(`minio_backend.py:44-48`) and in the `_MAX_CAUSE_DEPTH` comment
(`minio_backend.py:249-254`).

**The outcome ADR-0032 wanted still holds.** The translation reads the error
*itself* first and only unwraps a wrapper if one is there
(`_driver_answer`, `minio_backend.py:340-361`), so `put` and `put_stream`
do agree — a right outcome by a different route, which is a wrong record.

This is a **wave-1** file and a **wave-1** defect; wave 2's T-002 agent
found it and described it correctly (`W2-IMPL-01-T-002.md` §13.1). It is
listed here so the orchestrator routes it to the records task rather than
losing it.

**Suggested fix.** Amend ADR-0032's decision 4 to name `upload_file` rather
than "the transfer manager", and correct the two comments.

---

### F-7 · MINOR · `src/paa/models/router.py:266-296` · the LOCKDOWN-safe healthcheck is opt-in, and the mode was available to the router and thrown away

**What is wrong.** The wave added `healthcheck(*, permission_mode=ASK)` and
correctly refuses to probe the frontier under `LOCKDOWN`. But the **default
is `ASK`** — the *permissive* mode — and the docstring says the caller must
pass the mode: *"A deployment in `LOCKDOWN` must pass its mode; the router
does not know it."* The router **does** know it in the common construction:
`EscalatingModelRouter.__init__` accepts the full `Settings`
(`router.py:202-227`), and `Settings.policy.mode` carries the deployment's
mode — but the constructor narrows it to `ModelSettings` and discards it.
`Runtime.build` likewise hands the mode to `RouterModelAdapter`
(`src/paa/runtime.py:299`) and not to the router.

There is no in-tree caller of `router.healthcheck()` today (I checked:
`grep -rn "healthcheck" src/` finds only the providers' and the sandbox's),
so this is not an active leak. It is a *footgun with a default that re-opens
the hole the change was made to close*, on the one method whose whole
purpose is to look safe.

**Suggested fix.** Default to the most restrictive mode, or derive the mode
from the `Settings` the router was built with when it is handed the full
object, and fail closed when the mode is not known.

---

## VERIFIED

Everything below was run by me, on this host, against this tree. The output
is quoted, not paraphrased.

### The gate

```
$ .venv/bin/python -m ruff check src tests
All checks passed!

$ .venv/bin/python -m mypy src
Success: no issues found in 137 source files
```

Zero findings, zero errors. No `# noqa`, no `# type: ignore`, no widened ruff
ignore and no relaxed `pyproject.toml` rule appears in the wave's diff
(`git diff 4c9300d..HEAD -- pyproject.toml` adds exactly one extra to the
`dev` extra — `paa[s3]` — and one marker, `requires_s3`).

### The scoped suites

```
$ .venv/bin/python -m pytest tests/observability tests/models tests/agents \
      tests/validation tests/conventions tests/integration -q
[100%]  (all dots; 3 xfailed, no failures)
```

The whole suite, for completeness (no writer was active; the operator's
containers and database were not touched):

```
$ .venv/bin/python -m pytest tests/
2678 passed, 235 skipped, 3 xfailed, 1 warning in 159.39s (0:02:39)
```

The one warning is `StarletteDeprecationWarning` from `fastapi.testclient` —
third-party, not `paa`, so the `filterwarnings = ["error::DeprecationWarning:paa.*"]`
rule does not catch it. Not a wave-2 defect.

### The S3 live suite's skip behaviour

```
$ .venv/bin/python -m pytest tests/storage/test_s3_live.py -q -rs
SKIPPED [1] tests/storage/test_s3_live.py:106: needs a live S3 server: set
PAA_TEST_S3_ENDPOINT (and its two credentials), or a labelled
PAA_TEST_S3_<LABEL>_ENDPOINT
```

The skip is **module-level** and it fires for the *right* reason: `boto3`
**is** installed in this `.venv` (1.43.117) because the wave added `paa[s3]`
to the `dev` extra, so the `importorskip("boto3")` guard does not fire and
the *server* guard does. A missing driver and a missing server are two
different things and the suite tells them apart. It leaves nothing behind
because it never ran.

### The privacy promise (T-007)

* **The leak is closed on the paths the wave claims.** The redaction is at
  construction (`src/paa/models/base.py:117`), the escalation payload is
  scrubbed (`src/paa/models/router.py:683`), and the log line and the ledger
  payload are *the same dict* (`router.py:660-755`), so one fix closes both.
  `tests/models/test_key_secrecy.py` drives the real
  `ModelProvider.complete_structured` → `validate_against_schema` →
  `StructuredOutputError` chain rather than hand-writing the exception, which
  is the right way to test it.
* **The third path the brief asked about does not exist as a leak.** I
  checked the candidates one by one: `PaaError.__str__` renders the
  *redacted* message and the details (`core/errors.py:46-50`); `repr(exc)`
  uses `args`, which is the message only; `PaaError.to_payload()` merges
  details verbatim, but every *raiser* in the tree scrubs its own details
  before constructing the error; the `model.escalated` /
  `model.escalation_unlogged` / `model.escalation_unaudited` log lines all
  splat the scrubbed payload; a traceback renders `str(exc)`. The one
  residual gap is F-3 (the *class* does not enforce it) and the pattern
  coverage noted there.
* **The discriminator still makes exactly one record per escalation.** Two
  routers over the same ledger and the same correlation → **two** events
  (measured, and pinned by
  `test_escalation_audit.py::test_two_escalations_in_one_lineage_are_two_events`
  and `::test_the_escalation_ordinal_survives_a_router_restart`). Across a
  restart: also two. The ledger's own "suppressed as duplicate" branch
  (`store.py:136-144`) is now reachable only on a uuid4 collision, and the
  router says so loudly when it happens (`router.py:730-748`).
* **Concurrency:** two routers escalating the same correlation
  simultaneously → **two** events, both recorded, both with distinct
  nonces. Measured: `escalations: 2 ['escalation:1:980eeb3d…',
  'escalation:1:891eea28…']`. The ordinal label repeats (F-4); the audit
  trail does not.
* **The semaphore is one for the whole router and is exception-safe.**
  `router.py:594` is `async with self._semaphore:` — the context manager
  releases on `BaseException` too, so there is no deadlock and no double
  release. `tests/models/test_router_concurrency.py` asserts
  `max_in_flight == the cap` (not `<=`) across *both* tiers with a shared
  spy, which is the assertion that distinguishes a cap of two from a
  serialiser. I read it and it is the right test.

### The parking (T-006) — the claim about `replay.py` checked

* **The agent's claim that `src/paa/ledger/replay.py` needed no change is
  correct.** `_PHASE_MAP` maps
  `AWAITING_HUMAN_ATTESTATION → TaskPhase.AWAITING_HUMAN`
  (`replay.py:165`), and in a parked lineage the escalation *is* the last
  event (the orchestrator returns immediately after it), so the projection
  lands in the right phase and the phase the caller was told is the phase a
  replay projects. I verified the projection and the outcome agree.
* `TaskPhase.AWAITING_HUMAN` is excluded from `_RESUMABLE_PHASES`
  (`replay.py:60-68`), so a crash is never read as consent — a parked task
  stays parked across a restart. ✓
* **The first gate holds.** A task whose context builder escalates now
  ends `AWAITING_HUMAN` with exactly four events and neither
  `EXECUTION_STARTED` nor `MUTATION_COMMITTED`. A re-run while the question
  is open stays parked, and the builder is **not** asked again (so a builder
  that changes its mind cannot walk the task past the gate). ✓
* **The release is honoured on the escalation path** and the builder is not
  asked a second time. ✓
* **But the second gate is not** — F-1.
* ADR-0034 is honest about its cost (the resumed task plans from an empty
  context packet), about the rollback, and about what it does not decide.
  Its *characterisation* of what it does not decide is wrong in the
  direction that matters — F-1.

### The crash-fix (T-005)

* **The keyword table is complete and uniform.** I read the constrained
  keyword set out of the module's own source
  (`_KEYWORD_SPECS` + `_ANNOTATION_KEYWORDS`) and checked **every** one
  with a malformed value: 27 constrained keywords, **27/27** reported as a
  `schema_error=True` and **none** raised — including `pattern` (a bad
  regex), `multipleOf`, `enum`, `required`, `type`, `nullable`,
  `uniqueItems`, `additionalProperties`, `prefixItems`, `not`, `definitions`
  and `$defs`. The union of the two tables is exactly `SUPPORTED_KEYWORDS`
  (asserted by the module's own test). The rule is one function
  (`_value_defects`) used both to *report* and to *decide to apply*, so they
  cannot drift. This is the shape a "consistent rule" should have.
* **A valid schema still validates what it validated before.** I re-ran the
  agent's differential harness (`/tmp/opencode/w2impl02/diff_valid.py`,
  which still exists) **and** wrote my own against
  `4c9300d:src/paa/validation/schema_validator.py`:
  `compared 902 (schema, payload) pairs over 41 valid schemas / differences: 0`
  — twice, independently.
* **It is a schema error, not a payload error.** The errors carry
  `schema_error=True`, the offending `keyword`, and a path that points at
  the **schema** node (`$.properties.a.minLength`), not at the payload.
* The one behaviour that got *more* permissive is none; the one pre-existing
  assertion that changed (`multipleOf: 0` was a silent no-op) is stated in
  the report with its reasoning.
* **But the third validator still crashes** — F-2.

### The six observability fixes (T-008) — root cause or symptom?

All six are **root-cause** fixes, and I checked each against the code rather
than against the report:

1. `finish_run`'s `COALESCE` promise was false for `exit_code` — fixed at
   the source (`runs.py:350`). `exit_code` is `INTEGER` in **both** schemas
   (`schema_sqlite.sql:439`, `schema_postgres.sql:395`), so
   `COALESCE(?, exit_code)` is portable. The status is derived from
   `exit_code` (`runs.py:113-115`), so the old `exit_code = ?` genuinely
   turned a recorded success into an `ERROR` when the caller omitted the
   field. The two columns that are *not* coalesced (`ended_at`,
   `duration_ms`) are documented as deliberate.
2. `query_runs`'s `LIMIT` cut had no total order — fixed by adding the
   primary key as the secondary sort (`runs.py:432`:
   `ORDER BY started_at DESC, trace_id ASC`). This satisfies the house rule
   (*"a `LIMIT` cut needs a unique secondary key"*), and the two substrates
   **agree**: `trace_id` is stored canonically
   (`_canonical_trace_id` → `str(uuid.UUID(...))`,
   `runs.py:525-542`), and for a canonical lowercase hyphenated UUID the
   string order and Postgres's UUID byte order are the same order.
3. The empty summary was missing `window` — fixed at the source
   (`metrics.py:322-345`); the six numeric fields are now always present.
4. The poisoned histogram — fixed at the source: `Histogram.observe` refuses
   a non-finite running total (`metrics.py:296-306`) and `counter` refuses a
   non-finite increment or total (`metrics.py:462-474`). I checked the
   remaining path: `gauge` **already** had a finiteness check
   (`metrics.py:480-481`), and `summary()`'s `min`/`max`/`mean`/percentiles
   are all derived from finite inputs. **The telemetry is writable in every
   case** — I did not find a new unwritable one.
5. `percentile` validated the quantile only for a non-empty series — fixed by
   reordering the check ahead of the early return (`metrics.py:246-249`).
   Verified by hand: `percentile([1..5], 0.95) == 4.8` (the ROADMAP's own
   figure), `percentile([], 1.5)` raises, `percentile([], 0.5) == 0.0`.
6. `capacity=0` reached `observe` as an `IndexError` — fixed at construction
   (`Histogram.__post_init__`, `metrics.py:280-290`) **and** at the registry's
   parameter (`metrics.py:413`), so the refusal happens where the decision
   is made rather than when the first observation arrives.

The `BaseException` asymmetry in `Tracer._safely` is now stated, argued and
pinned rather than changed — the wave's reading (a `KeyboardInterrupt` /
`SystemExit` / `CancelledError` from an exporter must propagate, because a
rule that telemetry can never be interrupted by has stopped being a safety
rule) is defensible, and the docstring says what it costs (the exporter's
`BaseException` replaces the body's own exception on the way out of the
`finally`).

### The lock's removal (W2-FIX-01) — I ran two of the mutations myself

The tree's `src/paa` was copied to `/tmp/opencode/mut/src` and mutated there;
the repository's own sources were never edited.

* **Mutation 1 — revert `paa/observability/runs.py` to the
  `if TYPE_CHECKING:` form.** The lock goes **red**, five tests:
  `test_the_contract_is_not_imported_behind_type_checking`,
  `test_every_module_that_imports_the_contract_binds_it_at_runtime`,
  `test_the_family_resolves_the_contract_one_way`,
  `test_the_family_can_have_its_annotations_read`,
  `test_every_type_checking_block_is_classified`.
* **Mutation 2 — add a new, unclassified `if TYPE_CHECKING:` block to
  `paa/observability/metrics.py`.** The lock goes **red**, three tests:
  `test_the_contract_is_not_imported_behind_type_checking`,
  `test_every_module_that_imports_the_contract_binds_it_at_runtime`,
  `test_every_type_checking_block_is_classified`.

**The lock still has teeth.** Nothing of value was lost by removing the
deferral facility: every check that mentioned `DEFERRED` is now *stronger*
(the `DEFERRED` exemptions were removed from the source half, the runtime
half, the family half and the annotation half, and the "deferral must still
be deferred" self-invalidation check went with the map it policed). The
removed `family == set(DEFERRED)` cross-check had nothing left to
cross-check. The file's own position — *"a module that genuinely cannot
import the contract at runtime has to bring the machinery back, with a check
of its own, rather than inherit a hole that is no longer there"* — is
self-consistent. The one thing left behind is the docstring drift (F-5).

### The live S3 suite (T-002)

* **It exercises the real path.** `S3BlobStore` (the production class) with
  a real boto3 client; the only "fake" is a second, raw boto3 client used to
  look at the bucket, because the `BlobStore` contract has no list operation
  (D-6, recorded in ADR-0032). No mocking of the unit under test.
* **The error vocabulary is right.** The suite asserts `NotFound` with
  `namespace="bucket"` for a missing bucket, `NotFound` *without* it for a
  missing object, `AccessDenied` for wrong credentials, `Unreachable` for a
  dead endpoint and `Corrupt` for both an out-of-band corruption and a
  zstd frame that is not a frame — wave 1's vocabulary, asserted through the
  production classifier.
* **The teardown leaves nothing behind.** The bucket is per *run*, emptied
  between tests, and dropped in teardown; buckets a *write* created on
  SeaweedFS are tracked in `extra_buckets` and dropped too
  (`tests/storage/conftest.py`).
* **The ADR-0032 finding is correct** — see F-6. I confirmed it from the
  installed driver rather than from the report.

### Scope

Every source file the wave touched is inside some task's write-set, with two
exceptions that are *plan* defects rather than wave defects: T-008's
declared write-set was `src/paa/observability/{runs,logging}.py`, and the
wave also changed `metrics.py` and `tracing.py` — which is where four of the
six defects the plan itself names (§4, 0.4) live. The plan's write-set was
too narrow; the wave's scope was right. `docs/plans/w6-w7-w9.md` was edited
in a docs-only commit to correct two measured figures — a correction, not a
rewrite. `docs/adr/README.md` does not yet index 0032, 0033 or 0034; that is
T-012's job in wave 3 and the T-006 report flags it.

---

## UNVERIFIED

* **The live S3 suite against real servers.** No `PAA_TEST_S3_*` variable is
  set on this host and the operator's `paa-stack-*` containers were not
  started, stopped or touched, as instructed. The suite's *behaviour* was
  verified (module-level skip for the right reason, `boto3` present); its
  *assertions* against SeaweedFS and RustFS were verified by reading them,
  not by running them. The agent's own measurements of the two servers'
  differing answers (`InvalidBucketName` on both, `InvalidAccessKeyId` on
  SeaweedFS vs `UnauthorizedAccess` on RustFS) are **UNVERIFIED** by me.
* **The PostgreSQL parity of the `query_runs` total order and of
  `finish_run`'s `COALESCE`** — I verified the *schema* types and the
  canonical trace-id spelling by reading both schema files, and I reasoned
  the UUID/text order equivalence out, but I did not run the query against a
  live PostgreSQL (`PAA_TEST_POSTGRES_DSN` is not set and the operator's
  database was not touched).
* **The wave's own reversal measurements.** The T-005 report claims "81
  measured failures" under the reversal and the T-007 report claims the
  pre-fix leak reproduces. I did not re-run either reversal: both would have
  required editing `src/`, which the brief forbids me from doing. I verified
  the *current* behaviour directly instead, which is the stronger check.
* **The T-006 report's "mutation battery" (eleven more mutations).** I ran
  the two mutations that cover the lock and the parking's core claim; I did
  not reproduce the rest of the battery.

---

## NOT A FINDING

Things I looked at and am satisfied with, so the orchestrator knows they were
looked at.

* **The discriminator change is a real fix.** Replacing a per-process counter
  with a per-lineage ordinal plus a per-escalation nonce is the right shape,
  the docstring explains *why* in the terms the ledger's own idempotency key
  uses, and the "suppressed as a duplicate" branch is now loud rather than
  silent. The restart case is pinned.
* **The redaction does not swallow the diagnosis.** The wave's balance is
  right: a payload that says nothing is not an audit record, so the scrub
  replaces the secret with `***REDACTED***` and keeps the rest. The
  operator can still read *which* field failed and *how*.
* **The `healthcheck` change is correct in intent** (probe the frontier only
  when escalation is permitted; report the frontier as *absent* rather than
  `False` when it was not asked, because absent and "asked and cannot serve"
  are different facts). Only its default is wrong — F-7.
* **ADR-0034 is a good ADR.** It states the cost, the rollback, the
  blast radius (a task that declared no required slots has density 1.0 and
  never escalates), and what it does not change. It argues the phase map
  correctly: changing the map would have made the projection *lie* about a
  lineage that went on to commit, rather than fix anything.
* **The `HUMAN_GATE_REJECTED` path is a real terminal.** It lands in
  `BLOCKED`, and a re-run of a rejected lineage does not resume.
* **The `T-005` policy decision (`multipleOf: 0` and `minLength: -1` are
  schema errors, not silently ignored)** is the right reading — it is the
  keyword's own JSON-Schema type, applied uniformly — and the one
  pre-existing assertion it changed is explained in the report.
* **The S3 suite's per-run bucket** is not a shortcut: SeaweedFS maps a
  bucket onto a filer collection with a volume of its own and cannot
  allocate one per test, so a per-test bucket exhausted the server in a
  dozen tests. One bucket per run, emptied between tests, is the correct
  design and the reason is recorded.
* **The `dev` extra now carries `paa[s3]`**, on the same argument the
  `paa[api]` widening already made: *there is no environment in which the
  gate can pass while the S3 path is untested.* Verified: `boto3` 1.43.117
  is present in this `.venv` and the suite's skip is for the *server*, not
  for the driver.
* **`tests/observability/test_logging.py` is substantive** (five classes
  over the correlation-id contextvar, the call-site-wins rule, the binding
  restoration, the rendering and the `logging` name collision), and
  `src/paa/observability/logging.py` needed no change to pass it.
* **The concurrency test's assertion shape** (`== the cap`, not `<=`) is the
  one that distinguishes a cap from a serialiser, and the shared spy across
  both tiers is what proves "one semaphore for the whole router".
* **The `MODEL_ESCALATED` payload still satisfies ADR-0021/ADR-0015's
  promise**: `from_model`/`from_provider`/`from_tier`, the same for `to_*`,
  `reason`, `trigger`, `modality`, `permission_mode` and `leaves_machine`
  are all present and unchanged.
* **`_route` still writes the audit record *before* the frontier call**, and
  `test_the_audit_record_is_written_before_the_frontier_call` proves it from
  inside the frontier call rather than by inspection. That is the property
  that makes a failed escalation visible.
* **No secrets, tokens or PII appear anywhere in the wave's diff** — the
  test key is a fake in `tests/models/conftest.py`, and the S3 suite's
  credentials come from the environment.

---

*Reviewer's note for the orchestrator: the two MAJOR findings are the ones
worth a decision. F-1 is a one-or-two-line fix in the same area the wave
already touched, and it is the same defect class the wave fixed in T-007 —
so the consistency argument is strong. F-2 is a one-module fix that needs an
owner for `src/paa/models/base.py` (T-007's file), and it can be done by
porting the shape table the wave already wrote.*
