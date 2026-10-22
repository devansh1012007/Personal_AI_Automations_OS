# Brief — W2-IMPL-01 (implementer) · wave 2 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** build the **parametrised S3 lifecycle and failure-mode suite** —
the same lifecycle against **two independent S3 servers**, SeaweedFS (the
deployment's) and RustFS (a different implementation), not against a mock.

**Why this task matters:** the S3 path is the substrate the cold lake stores
its signals and its archived artifacts in, and it has **never met a real
server**. Every one of its 23 tests runs against `StubS3Client`, a duck-typed
fake with no `head_bucket`, no `create_bucket` and no `list_objects_v2`. That
is why six raw-exception escape sites and a `put`/`put_stream` divergence
survived until wave 1. The point of the suite is not "does S3 work" — it is
"does *this client* work against a server it was not written against".

**What failure looks like:** a suite that only re-states what the stub
already proved, or one that skips when it should run.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-CONTEXT.md` — **read first**.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
3. `docs/plans/w6-w7-w9.md` — **§2.1** (your task, with the lifecycle and the
   failure modes), **§0** (the evidence), **§6** (D-2, D-3, D-6 — given).
4. `docs/adr/0032-blob-error-vocabulary.md` — **the vocabulary you assert
   against.** The names are the contract: `NotFound`, `AccessDenied`,
   `Unreachable`, `Corrupt`, `NotSupported`, `MalformedRequest`, `BlobFailure`,
   and `translate_blob_error(exc, *, substrate, **details)`.
5. `src/paa/storage/coldlake/minio_backend.py` and `base.py` — the code under
   test. `src/paa/storage/coldlake/errors.py` — the vocabulary.
6. `tests/storage/conftest.py` — **the house pattern to copy**: the
   `PAA_TEST_POSTGRES_DSN` convention (a module-level constant naming a
   *server*, a fixture that makes and drops its own resource, a docstring with
   the operator's exact `docker run` recipe).
7. `tests/storage/test_minio_backend.py` — the stub, and what it does *not*
   model.
8. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md` — your
   report template.

## 3. The task

**T-002.** The S3 path is only ever tested against a stub.

### The suite's shape

* **A test-owned server per session, per implementation.** Two containers with
  **unique names** and **published loopback ports**; a **uniquely-named
  bucket per run** (`paa_s3_test_{pid}_{uuid8}`, mirroring
  `tests/storage/conftest.py:55`); dropped in teardown. **The deployment's
  `paa-stack-seaweedfs-1` and its `paa-cold-lake` bucket are never touched** —
  the deployment stack publishes no host port, so it is unreachable from the
  host anyway, and writing to it would be writing to the operator's data.
* **The env convention**, following the house pattern: `PAA_TEST_S3_ENDPOINT`,
  `PAA_TEST_S3_ACCESS_KEY`, `PAA_TEST_S3_SECRET_KEY`, `PAA_TEST_S3_BUCKET`.
  The variable names the **server**; the fixture makes its own bucket. The
  suite **skips** when the variable is absent — a test that fails for a
  missing server fails for the wrong reason.
* **The marker**: `requires_s3`, registered in `pyproject.toml` (which you own
  for this task), plus an `importlib.util.find_spec("boto3")` guard. And
  `paa[s3]` joins the **`dev`** extra — on the same argument the `api` extra
  made: *"there is no environment in which the gate can pass while the S3
  path is untested."*
* **Parametrised over the two servers**: the same test body, two
  implementations. A test that runs for SeaweedFS but is skipped for RustFS
  because of an implementation difference is a **finding to report**, not a
  thing to paper over.

### The lifecycle (per server)

put → get → get_stream → exists → stat → delete → dedup (a second put is a
no-op) → an empty payload → an uncompressed payload → a payload above the
1 MiB spool boundary → a unicode digest refused → a traversal-shaped digest
refused.

### The failure modes

a missing bucket · a missing object · permission denied (wrong credentials) ·
the endpoint unreachable · corrupt content at the key (overwritten out-of-band
with a **raw** boto3 client) · a zstd frame that is not a frame.

**On the list step (D-6, given):** the `BlobStore` contract has **no** list
operation and this task does not add one. The one assertion that needs to see
the raw object uses a **raw boto3 client**; the contract-level assertions use
`exists()` / `stat()` / `delete()`.

## 4. Acceptance criteria (measurable)

* The suite **skips cleanly** with no `PAA_TEST_S3_*` set and no `boto3`
  installed, and **runs** against both servers when they are.
* Every lifecycle step and every failure mode above is asserted **against a
  real server**, for **both** implementations.
* The error assertions name the **wave-1 vocabulary** — a missing bucket is
  `NotFound` (with `namespace="bucket"`), a missing object is `NotFound`
  (with the object namespace), a wrong credential is `AccessDenied`, an
  unreachable endpoint is `Unreachable`, corrupt bytes are `Corrupt`.
* The suite **makes its own bucket and drops it**, and leaves nothing behind:
  no container, no bucket, no volume. Say how you prove the cleanup.
* The `requires_s3` marker is registered and the `dev` extra resolves
  `paa[s3]`.
* **ADR-0033** records the decision: *the S3 substrate is tested against two
  independent S3 servers, not against a mock*, what the two servers proved
  different from each other, and the honest limits of the suite (it is not a
  conformance test of S3; it is a test of *this client*).
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/storage/test_s3_live.py -q
  .venv/bin/python -m ruff check tests/storage
  .venv/bin/python -m mypy src
  ```
* The wave-1 gate must not regress: ruff 0, mypy 0, and the full suite green
  (the orchestrator runs the full suite).

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
tests/storage/test_s3_live.py      (new)
tests/storage/conftest.py
pyproject.toml
docs/adr/0033-*.md                 (new)
```

**READ-ONLY:** `src/paa/storage/coldlake/**` (wave 1 owns it — **read it, do
not edit it**; if the live suite finds a defect there, report it as a finding
with the reproduction and the exact change required), `tests/storage/{test_minio_backend,test_blobstore_contract,test_blob_errors,test_coldlake}.py`.

**FORBIDDEN:** `src/paa/**` (except that `pyproject.toml` is yours), `tests/**`
outside `tests/storage/`, `deploy/**`, `docker-compose*.yml`, `.env.example`,
`docs/**` except your ADR, `docs/adr/README.md`, `docs/plans/**`,
`docs/MCP_AND_PLUGINS.md`, `AGENTS.md`.

**If the live suite finds a defect in `src/paa/storage/coldlake/`:** do not
fix it. Report it as `FINDING` in COMMS with the reproduction, and mark the
corresponding test `xfail` **with the reason** — or better, leave it failing
and report it, because a red test the orchestrator can see is worth more than
a green one that lies. Your call, stated in the report.

## 6. Environment

* The SeaweedFS image (`chrislusf/seaweedfs:4.48`) and the RustFS image
  (`rustfs/rustfs:latest`, v1.0.3, S3 on **9000**, credentials from
  `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY`, data at `RUSTFS_VOLUMES=/data`)
  are **already in the local image store** — no network pull is needed, and
  you should not need one.
* **Start your own containers with unique names** (e.g. `paa-test-s3-<uuid>`),
  publish a **loopback** port, and remove them in teardown. **Never** touch
  `paa-stack-*` or any other project's container.
* `boto3` is not installed: install it into the venv
  (`uv pip install --python .venv/bin/python boto3`) **or** add it through
  the extra you are changing — and say which you did, and how the next
  developer gets it.
* `pip install` from the network is fragile: the repo's Dockerfiles encode
  the convention (retries, a growing backoff). If you install anything, use
  `PIP_RETRIES=10 PIP_TIMEOUT=120` and a retry loop.
* Scoped checks only. **Never** the full suite.

## 7. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-01-T-002.md`
  — `templates/report-implementation.md`, ORCH-DIGEST first, appended as you
  work.
* **Commits:** code first, then the report in a separate docs-only commit.
  Trailers: `Task: T-002`, `Agent: W2-IMPL-01`, `Wave: 2`, `Report: <path>`.
  Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- tests/storage/test_s3_live.py tests/storage/conftest.py pyproject.toml
  git commit -m "<msg>" -- tests/storage/test_s3_live.py tests/storage/conftest.py pyproject.toml
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.
  **It must not be empty.**
