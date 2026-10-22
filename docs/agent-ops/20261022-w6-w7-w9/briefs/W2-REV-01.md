# Brief — W2-REV-01 (independent code reviewer) · wave 2 · run 20261022-w6-w7-w9

## 1. Role

You are an **independent reviewer**. You did not write any of this code and
you are not the orchestrator. Your job is to **try to break the change**, not
to confirm it. You **do not edit code**. You report findings; the orchestrator
decides what to do with them.

Do not be agreeable. A wave that produced 12k lines and 539 new passing tests
is a wave where something is wrong, and the most valuable thing you can do is
find it.

## 2. The diff under review

```bash
git log --oneline 4c9300d..HEAD          # wave 2's commits
git diff 4c9300d..HEAD -- src/           # the code
git diff 4c9300d..HEAD -- pyproject.toml
git diff 4c9300d..HEAD -- docs/adr/
```

The five tasks, in the plan (`docs/plans/w6-w7-w9.md` §4) and in the reports
under `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/`:

| Task | The requirement | The change |
|---|---|---|
| T-002 | the S3 path has never met a real server; build a live suite over two independent S3 servers | `tests/storage/test_s3_live.py` (new), `tests/storage/conftest.py`, `pyproject.toml`, ADR-0033 |
| T-005 | the validation layer's three headline claims have no tests; and a malformed schema keyword **crashes** the gate instead of rejecting it | `src/paa/validation/schema_validator.py` (+418 lines), `tests/validation/*` |
| T-006 | assert the orchestrator's **exact** event sequence; a **real** three-node delegation cycle; the anti-goal **0.82 cosine path**; and **park** the task on `HARD_STOP_ESCALATE_TO_USER` (decision **D-4**) | `src/paa/agents/orchestrator.py` (+74), `tests/agents/*`, `tests/integration/test_end_to_end.py`, ADR-0034 |
| T-007 | the escalation path leaks the API key; the LOCKDOWN spy, the semaphore, and the restart-surviving escalation ordinal are unasserted | `src/paa/models/{base,router}.py`, `tests/models/*` |
| T-008 | percentiles are labelled but not valued; `finish_run` is never asserted; `logging.py` has no tests | `src/paa/observability/{runs,logging,metrics,tracing}.py`, `tests/observability/*` |
| the lock | the deferral of `paa.observability.runs` was fulfilled; the fulfilled entry (and the emptied facility) had to go | `tests/conventions/test_import_conventions.py` |

## 3. What to check, in priority order

### 3.1 The privacy promise (T-007) — the one that matters most

* **Is the leak actually closed on every path?** `StructuredOutputError`
  redacts its `schema_errors` at construction, and the router redacts the
  provider diagnosis it copies into the `MODEL_ESCALATED` payload and the
  `model.escalated` log line. Is there a **third** path — an exception type
  that carries a key and is not redacted, a `repr`, a `details` dict that
  reaches a log line, a traceback?
* **Does the redaction break anything that legitimately needs the detail?**
  A redaction that swallows the diagnosis makes the escalation unactionable.
  Read the diff and judge the balance.
* **The discriminator change:** it is now
  `escalation:<per-correlation ordinal read from the ledger>:<the event's uuid4>`.
  The old one was `escalation:<per-router-instance counter>`. Does the new one
  still make **exactly one** `MODEL_ESCALATED` record per escalation? What
  happens if the ledger read fails, if two routers escalate in the same
  correlation concurrently, or if the ledger is empty? Does ADR-0021's promise
  about the `MODEL_ESCALATED` payload still hold?
* **The semaphore:** is the cap still one semaphore for the whole router, and
  is the release path exception-safe? A semaphore released in the wrong place
  is a deadlock; released twice is a `ValueError`.

### 3.2 The parking (T-006, D-4) — a behaviour change on a live path

* **Does the task really park?** Read `orchestrator.run()` and the diff. Can
  the task reach `EXECUTION_STARTED` / `MUTATION_COMMITTED` while an
  escalation is unanswered? Is there a path that **clears its own gate**?
  That is the property that makes the event mean something, and a
  self-clearing gate is the same as no gate.
* **Does the projection agree with the intent?** The agent says
  `src/paa/ledger/replay.py` needed no change. **Check that claim** against
  the phase map: after a park, what phase does a replay project? Does it
  match what the caller was told?
* **Is the release correct?** `clear_human_gate(approved=…)` must release the
  parked task and only that task. What happens on a **re-run** of a parked
  task? On a **replay**? On a task parked twice?
* **Is the ADR-0034 record honest** — does it state the cost, the rollback,
  and what it does *not* cover?

### 3.3 The crash-fix (T-005)

* **Is the rule consistent across every keyword?** A malformed
  `minLength` must be handled the same way as a malformed `maxItems`,
  `minimum`, `multipleOf`, or a `pattern` that is not a valid regex.
  **Enumerate the keywords the validator understands from the source** and
  check each one — a fix for the one the agent found is not a fix.
* **Does a valid schema still behave exactly as before?** The agent claims
  902 (schema, payload) pairs were compared with 0 differences. **Re-run that
  comparison** — or a substantial sample of it — yourself.
* **Is the error a *schema* error rather than a payload error?** The gate must
  say which one it is, and the model must be able to act on it.

### 3.4 The observability fixes (T-008)

Six source defects were fixed: `finish_run`'s `COALESCE` promise was false
for `exit_code`; `query_runs` had no total order for its `LIMIT` cut; the
empty summary was missing `window`; a poisoned histogram made the telemetry
unwritable (`json_valid` rejects `Infinity`); `percentile` validated the
quantile only for a non-empty series; `capacity=0` reached `observe` as an
`IndexError`.

* For each: is the fix at the **root cause**, or does it move the symptom?
* `query_runs`'s total order — does it satisfy the house rule (*"a `LIMIT`
  cut needs a unique secondary key"*)?
* The `json_valid`/`Infinity` fix — is the telemetry now **writable** in every
  case, or did it become unwritable in a new one?

### 3.5 The lock's removal (W2-FIX-01)

The deferral facility (the `DEFERRED` map, class `"F"`, its key, the check
that iterated it, every `DEFERRED` condition) was **removed** because the
eighth deferral was fulfilled and the file's own philosophy refuses a class
with no member and a check that cannot fail.

* **Does the lock still have teeth?** The agent claims four mutations (a
  reverted family module, a reverted `observability.runs`, a new unclassified
  `TYPE_CHECKING:` block) each turn it red. **Run at least one yourself.**
* **Was anything of value removed that should have stayed?** The checks that
  mentioned `DEFERRED` also enforce other things — read each one and confirm
  it still enforces it.
* **Is the docstring now telling the truth** about the tree? A lock that
  documents a deferral that no longer exists is the drift the file exists to
  catch.

### 3.6 The live S3 suite (T-002)

* **Does it exercise the real path**, or does it mock the thing under test?
* **Are the assertions about wave 1's error vocabulary right** — `NotFound`
  with `namespace="bucket"` for a missing bucket, `NotFound` with the object
  namespace for a missing object, `AccessDenied` for wrong credentials,
  `Unreachable` for a dead endpoint, `Corrupt` for bad bytes?
* **The FINDING about ADR-0032:** the agent measured that `upload_fileobj`
  (what `put_stream` calls) propagates the raw `ClientError` and does
  **not** wrap it in `S3UploadFailedError`, so ADR-0032's decision 4
  mis-states the mechanism. **Read ADR-0032 and judge whether the record is
  wrong** — and whether the *code* still meets the intent, since a right
  outcome by a different route is still a wrong record.
* **Does the suite leave anything behind** — buckets, containers, state?

### 3.7 Everything else

* **Root cause or symptom?** For each of the six observability fixes, the
  validation crash-fix, the leak fix and the parking fix: is the cause
  addressed, or is the symptom moved?
* **Security.** Any new place a key, a path, or an untrusted body can reach.
* **Concurrency.** The semaphore; the parking under concurrent gates; the
  `query_runs` pagination.
* **Conventions.** `-> None` on every test; `asyncio_mode = "auto"` needs no
  decorator; a docstring that says *why*; no `# noqa`, no `# type: ignore`,
  no widened ruff ignore, no relaxed `pyproject.toml` rule.
* **Dead code** and **scope creep** — the wave touched 37 files; is every one
  of them in its task's write-set?

## 4. What you must run yourself

Do not take the reports' numbers on trust. Run them yourself and report the
**actual** output:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/observability tests/models tests/agents tests/validation tests/conventions tests/integration -q
.venv/bin/python -m pytest tests/storage/test_s3_live.py -q -rs
```

If you want the live S3 suite to actually run, the env convention is in the
suite's header and in ADR-0033 — but **do not** start or stop the operator's
`paa-stack-*` containers, and do not use the operator's database. If you
cannot verify a claim, say so explicitly and mark it UNVERIFIED.

## 5. The report

Write `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-01-code.md`.
Structure it as:

* **VERDICT** — one line: is this wave safe to keep as it is?
* **FINDINGS** — each with: severity (**BLOCKER** / **MAJOR** / **MINOR** /
  **NIT**), the file and line, what is wrong, **the evidence** (the command
  output or the code), and the **suggested fix** in one or two sentences.
* **VERIFIED** — what you ran and what it actually said.
* **UNVERIFIED** — what you could not check and why.
* **NOT A FINDING** — anything you looked at and are satisfied with, so the
  orchestrator knows it was looked at.

Do not edit code. Do not fix what you find. Report it.
