# Brief — W2-IMPR-05 (fixer) · wave 2 improve · run 20261022-w6-w7-w9

**Task:** fix **R-W2-202**, **F-6**, **F-5** and the S3-suite MINOR of the
wave-2 reviews. Read each in full before you start:

* `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-02-tests.md`
  ⟹ **R-W2-202** (MAJOR) and the MINOR about *"two independent
  implementations"*.
* `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-01-code.md`
  ⟹ **F-6** (NIT, ADR-0032's decision 4 and two comments) and **F-5** (MINOR,
  the lock's own docstring still documents the removed class "F").

**You are the fixer**, not the original implementer.

## R-W2-202 — the record is false in the operative respect (MAJOR)

The wave-2 T-002 report (§8, §13.1) claims that the driver error is *not*
reachable from the wrapper, and proposes wording for ADR-0032 that says the
wrapper *"carries neither response nor cause"*. The reviewer measured the
opposite: the driver error **is** the wrapper's `__context__`, and
`_driver_cause` follows it — so the wrapper **does** carry the cause. On that
measurement the report's mutation 11 (`put_stream` via the wrapping
`upload_file`) **survives all 40 live tests**, and the proposed ADR wording
is false.

So two things are wrong: the **report's claim**, and the **ADR-0032 wording**
it proposes. And the *code* has to be judged against ADR-0032's **intent**,
not against the report's description of it.

**Do this, in this order:**

1. **Measure.** On the two live servers (the images are in the local image
   store; the env convention is in the suite's header and in ADR-0033), find
   out **exactly** what `put_stream` raises for: a missing bucket, a missing
   object, wrong credentials, and a corrupt payload — and what the
   *wrapper* (`S3UploadFailedError`) carries in `__cause__` / `__context__` /
   `.details`. Put the raw measurements in your report.
2. **Judge the code against ADR-0032's intent** — *"every blob error that
   crosses the boundary is a `BlobError`"* (read ADR-0032). If
   `put_stream` leaks a raw botocore `ClientError` where the vocabulary
   promises a `BlobError`, **fix the code** so the boundary holds, and add
   the test that pins it. If the boundary does hold and only the *record* is
   wrong, fix the record.
3. **Correct ADR-0032's decision 4** (and the two comments in
   `src/paa/storage/coldlake/minio_backend.py:45-47` and `:249-254` that
   repeat the error) so that they say what is **true**. An ADR whose
   mechanism is wrong is a false record, and a false record is what the
   reviewer's mutation was really about.
4. **Correct the wave-2 report** where it states the false claim — the
   report is part of the run's written trail and it is currently wrong.

## The S3 suite MINOR — "two independent implementations" is a convention, not an assertion

`tests/storage/test_s3_live.py:115` calls the two servers "independent
implementations", but nothing **asserts** it: two containers of the same
image would pass all 40 tests. Make the claim either **true by
construction** (assert the two servers are different images/implementations —
e.g. from the endpoint labels the suite already knows) or **narrow the
wording** to what the suite actually guarantees. Say which, and why.

## F-5 — the lock's own docstring still documents the class that was removed

`tests/conventions/test_import_conventions.py:43` still lists the removed
class "F" (*"or the family"*). The file's own philosophy: *"no note may
outlive its block"* — a lock that documents a class the tree has outgrown is
the drift the lock exists to catch. Fix the docstring so it describes the
tree as it is. (One line. Do it properly, and check whether the same stale
sentence appears elsewhere in the file.)

## Constraints and environment

* **Never** touch the operator's `paa-stack-*` containers or the operator's
  database. Your two servers must be your own, uniquely named, and removed in
  teardown — and the proof of the cleanup belongs in your report.
* Both server images are already in the local image store; no network pull
  should be needed. If you install anything, use the repo's retry convention
  (`PIP_RETRIES=10`, `PIP_TIMEOUT=120`, and a retry loop).
* Scoped checks only: never the full suite.

## Ownership

**WRITE-SET:** `docs/adr/0032-blob-error-vocabulary.md`,
`src/paa/storage/coldlake/minio_backend.py`, `tests/storage/test_s3_live.py`,
`tests/conventions/test_import_conventions.py`,
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-01-T-002.md`.

**READ-ONLY:** `src/paa/storage/coldlake/{errors,base,cas}.py` (read them;
if a fix needs a change there, post a `NEED` with the exact change),
`src/paa/storage/**` otherwise, `tests/storage/conftest.py` (read it),
`docs/adr/README.md` (the orchestrator owns the index).

**FORBIDDEN:** `src/paa/**` outside `storage/coldlake/minio_backend.py`,
`tests/**` outside `tests/storage/test_s3_live.py` and
`tests/conventions/test_import_conventions.py`, `pyproject.toml`, `deploy/**`,
`docs/**` except ADR-0032 and the T-002 report, `docs/plans/**`, `AGENTS.md`.

## Acceptance criteria

* The measurement of `put_stream`'s error behaviour on **both** servers is in
  the report, with the raw output.
* The code and ADR-0032 **agree**, and ADR-0032's decision 4 is true.
* The wave-2 T-002 report no longer states the false claim.
* The "two independent implementations" claim is either asserted or narrowed.
* The lock's docstring matches the tree; the conventions suite is green.
* Scoped: `.venv/bin/python -m pytest tests/storage tests/conventions -q`,
  `ruff check src/paa/storage tests/storage tests/conventions`,
  `mypy src/paa/storage` — green, and the gate must not regress.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-IMPR-05-R-W2-202-F-5-F-6.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: R-W2-202,F-5,F-6`,
`Agent: W2-IMPR-05`, `Wave: 2-improve`.
Final message ≤ 8 lines: `STATUS | agent | findings | report path | commit SHAs | blockers`.
