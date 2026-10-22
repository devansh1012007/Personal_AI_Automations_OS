# Brief — W1-IMPR-01 (improver) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** fix the wave-1 review's findings on the **blob substrate** — one
BLOCKER and several MAJORs — and close the other findings that belong to the
same write-set.

**Your role:** improver. You are the **last change of wave 1**. You act on a
reviewer's findings; you do not re-litigate them. Every finding you were given
ends **FIXED**, **REJECTED** (with evidence), or **DEFERRED** (with a reason).

**You are not the implementer.** You did not write this code. That is the
point: a fresh agent reading the findings and the code cannot be accused of
defending its own work.

## 2. Required reading (in this order)

1. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
2. **The findings you must address, in full:**
   `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-REV-01-code.md`
   §4 — findings **R-W1-001** (BLOCKER), R-W1-002, R-W1-003 (MAJOR),
   R-W1-004, R-W1-005, R-W1-006, R-W1-007, R-W1-009 (MINOR/NIT);
   and `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-TREV-03-tests.md`
   §5 — findings **R-W1-201, R-W1-202, R-W1-203, R-W1-204, R-W1-205** (MAJOR),
   R-W1-206, R-W1-207, R-W1-208, R-W1-209, R-W1-214 (MINOR), R-W1-215,
   R-W1-216 (NIT).
   **Read the findings in full — each carries the evidence, the reproduction
   and the recommended fix.** Do not work from this brief's summary.
3. `docs/adr/0032-blob-error-vocabulary.md` — the decision you are correcting.
   The ADR is part of your write-set, and **it is where the intent lives**: the
   BLOCKER is that the ADR, the protocol and the tests assert a behaviour the
   code cannot produce.
4. `docs/adr/0025-postgres-dialect-seam.md` — the model (Seam 3: one error
   vocabulary, translated at the boundary, idempotently).
5. `docs/plans/w6-w7-w9.md` §3.1 — the design the task was given.
6. `src/paa/storage/coldlake/errors.py`, `minio_backend.py`, `base.py`,
   `cas.py` — the code.
7. `tests/storage/test_blob_errors.py`, `test_minio_backend.py`,
   `test_blobstore_contract.py` — the tests.
8. `docs/agent-ops/20261022-w6-w7-w9/templates/report-improvement.md` — **your
   report template.**

## 3. The findings, and what "done" means for each

Every BLOCKER and MAJOR must be FIXED, or REJECTED with evidence.

| Finding | Severity | What "done" means |
|---|---|---|
| **R-W1-001** | **BLOCKER** | The bucket-absence clause is **reachable**. A HEAD 404 is currently classified as an *object*-level absence, so `exists()` / `stat()` / `delete()` answer `False` / `None` / `False` while ADR-0032 §5, the `BlobStore` protocol and the implementer's own flagged behaviour change all assert that a missing **bucket** raises. **Fix the classification so the contract, the ADR and the code agree** — and then make the ADR and the protocol say what the code now does. Whichever way you resolve it, the three must not disagree. |
| **R-W1-002** | MAJOR | The CAS must not leak a raw `OSError` from five contract methods. **The two substrates must fail the same way for the same reason** — that is the whole claim of "one contract". |
| **R-W1-003** | MAJOR | `CAS.stat()` must not leak a raw `zstandard.ZstdError`; the same corrupt frame that `get()` reports as `Corrupt` must be reported the same way by `stat()`. |
| **R-W1-201** | MAJOR | The "no driver exception escapes" invariant must be tested against **every transport path** — the enumeration test currently exercises `head_object` and nothing else. `put_object`, `upload_fileobj`, `get_object` and `delete_object` must each be driven to a driver failure. |
| **R-W1-202** | MAJOR | `test_the_upload_wrapper_is_unwrapped` must actually reach the upload wrapper. |
| **R-W1-203** | MAJOR | 45 of the 50 new blob tests would pass against the pre-change boundaries — they do not discriminate. **Strengthen the ones that carry the invariant**, or delete the ones that carry nothing. A test that passes either way is worse than no test: it looks like coverage. |
| **R-W1-204** | MAJOR | The S3 error-code tables must be **pinned** by a test: today every code can be deleted from the table and the suite stays green. |
| **R-W1-205** | MAJOR | The missing-bucket behaviour change needs a test of its own, and the stub must be able to express it. |
| R-W1-004, R-W1-005, R-W1-006, R-W1-009 | MINOR/NIT | Fix each as the finding says. R-W1-004 is a docstring/code disagreement — decide which is right and make them agree. |
| R-W1-206…R-W1-209, R-W1-214…R-W1-216 | MINOR/NIT | Fix as the finding says. **R-W1-214 is a factual error in the T-001 report** (the count is 26, not 28) — correct the report, which is a run-folder file you may edit. |

**Also, because it is in your write-set and it is a record of your own
decision:** ADR-0032 must be **true** when you are done. If the BLOCKER's
resolution changes what the ADR says, amend the ADR — with an "Amendment"
section, the way ADR-0030 was amended, not a silent rewrite.

## 4. Constraints

* **Do not change the contract's *shape*** to make a test pass. The
  `BlobStore` protocol's method signatures are correct and pinned; the
  error *clause* is what you are fixing.
* **Every new error is a `StorageError`**, so the existing `except PaaError`
  handlers keep working. Do not introduce a new exception base.
* **Do not import `botocore` at module scope.** The translator recognises a
  driver exception *structurally*, and `boto3` is not installed in this
  environment — the whole test suite must keep passing without it.
* **Do not swallow a `PaaError` that is not ours.** A `TypeError` from our own
  code is a programming error and must survive as one (R-W1-004 is about the
  docstring and the code disagreeing — resolve the disagreement, do not hide
  the error).
* **The gate must not regress.** `ruff` 0, `mypy src` 0, and the full suite
  green — the orchestrator runs the full suite; you run your scoped files.
* **Every fix needs a test that fails without it.** For a finding that is
  already a test-strength finding, the "test" is the strengthened test.
* **No new dependencies**, no `pyproject.toml` change, no `src/paa/config.py`
  change, no `docs/` outside your ADR.

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/storage/coldlake/errors.py
src/paa/storage/coldlake/minio_backend.py
src/paa/storage/coldlake/base.py
src/paa/storage/coldlake/cas.py
src/paa/storage/coldlake/__init__.py
tests/storage/test_blob_errors.py
tests/storage/test_minio_backend.py
tests/storage/test_blobstore_contract.py
docs/adr/0032-blob-error-vocabulary.md
docs/agent-ops/20261022-w6-w7-w9/reports/w1-impl/W1-IMPL-01-T-001.md   (the factual correction only)
```

**READ-ONLY:** everything else. In particular `tests/storage/test_s3_live.py`
does not exist yet (wave 2), `docs/agent-ops/.../ownership/W1.tsv` is
orchestrator-owned, and the review reports are read-only to you.

**FORBIDDEN:** `deploy/**`, `docker-compose*.yml`, `src/paa/models/**`,
`src/paa/agents/**`, `src/paa/memory/**`, `src/paa/validation/**`,
`src/paa/observability/**`, `src/paa/marketplace/**`, `src/paa/skills/**`,
`pyproject.toml`, `docs/adr/README.md`, `docs/plans/**`,
`docs/MCP_AND_PLUGINS.md`, `AGENTS.md`, and the other two review reports.

## 6. Siblings in this wave

`W1-IMPR-02` (the deployment findings, `deploy/fetch-images.sh` +
`tests/deployment/` + the ADR-0030 amendment + `docs/deployment/docker.md` +
`.env.example`) and `W1-IMPR-03` (the import-lock findings,
`src/paa/memory/*`, `src/paa/marketplace/installer.py`,
`src/paa/skills/registry.py`, `src/paa/agents/{policy,context_agents}.py`,
`tests/conventions/`). **No overlap with your write-set.** You cannot see
their uncommitted work.

**One interaction matters:** `W1-IMPR-03` converts imports to runtime across
`src/paa/`, and `coldlake/cas.py` is the **one** module whose
`TYPE_CHECKING` block is load-bearing for a real cycle (`base.py` imports
`cas` for `BlobRef`, so a runtime `cas → base` import closes it). **Do not
make that import real.** If you add an import to `cas.py` or `errors.py`,
check the cycle.

## 7. Environment and tests

* **Scoped commands only:**
  ```bash
  .venv/bin/python -m pytest tests/storage/test_blob_errors.py tests/storage/test_minio_backend.py tests/storage/test_blobstore_contract.py tests/storage/test_coldlake.py -q
  .venv/bin/python -m ruff check src/paa/storage/coldlake tests/storage
  .venv/bin/python -m mypy src/paa/storage/coldlake
  ```
* **Never** the full suite, never a container, never a port, never `boto3`.
* `boto3` is not installed and must stay optional.
* Kill any process you start.

## 8. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-improve/W1-IMPR-01-T-001.md`
  — using `templates/report-improvement.md`. **Create it early and append as
  you work.** Section 1 is the **finding-by-finding disposition table**:
  every finding ID, its severity, FIXED / REJECTED / DEFERRED, what you did
  (or the evidence for a rejection), and the commit.
* **Commits:** code first, then the report in a **separate docs-only commit**
  listing the code commit's SHA. Trailers: `Task: T-001`, `Agent: W1-IMPR-01`,
  `Wave: 1`, `Report: <path>`. Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/storage/coldlake/errors.py …
  git commit -m "<msg>" -- src/paa/storage/coldlake/errors.py …
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | findings FIXED/REJECTED/DEFERRED | blockers`.

## 9. Notes

* **A finding you reject must be rejected with evidence, not with taste.** If
  you believe R-W1-001's fix would be worse than the current behaviour, say
  so, show the alternative, and **defer it with a reason** — the orchestrator
  decides. Silently ignoring a BLOCKER is the one unacceptable outcome.
* **The BLOCKER is the most important thing in this wave.** A contract, an ADR
  and a test suite that all assert a behaviour the code cannot produce is
  worse than no vocabulary at all: it is a vocabulary that lies.
* **Answer the long-run test** in your report: Source / Load / Hostile /
  Operability / Reversal.
