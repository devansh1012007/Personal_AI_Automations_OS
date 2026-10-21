# Brief — W1-IMPL-01 (DEEP implementer) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** give the blob substrate a *neutral error vocabulary* and translate
the S3 driver's exceptions at the substrate boundary, exactly as ADR-0025
established for the relational substrates — and record the decision in ADR-0032.

**Why this task matters:** the runtime stores cold-lake signals and archived
artifacts in an S3 object store, and the repository layer is supposed to catch
*a concept* ("the object is not there", "the server refused", "the bytes are
corrupt") rather than a driver's exception. Today it catches a
`botocore.exceptions.ClientError` — sometimes — and a `StorageError` —
sometimes else — and a raw `AttributeError` in another place. The **same
condition** (a missing bucket) produces **two different exception types**
depending on which method the caller used, so a caller cannot write a correct
`except` clause at all. A security-relevant judgement (is this blob absent, or
is the store unreachable?) is currently unanswerable.

**What failure looks like:** a reviewer finds another raw `botocore` exception
escaping the boundary, or finds that the new vocabulary is decorative — the
driver's exception still reaches the caller on some path, or the contract still
does not say what a method raises.

**Your role:** deep-dive implementer. You have the richest brief in the wave and
you may take the time you need. **Write the design record before you write the
code.**

## 2. Required reading (in this order)

1. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
2. `AGENTS.md` — the gate rules, the "docstrings carry the reasoning" rule, the
   "never import a heavy dependency at module scope" rule, the ADR rule.
3. `docs/agent-ops/20261022-w6-w7-w9/00-mission.md` — the commands, the
   hotspots, the baseline, the environment rules.
4. `docs/plans/w6-w7-w9.md` — **§0** (the rules), **§3.1** (your task, quoted
   below), and **§6** (the decisions D-1…D-6 that are already taken).
5. `docs/adr/0025-postgres-dialect-seam.md` — **the model you are copying.**
   Read especially "Seam 3 — one error vocabulary" and "the boundary translates
   the driver's errors and nothing else", and
   `src/paa/storage/relational/errors.py` + the two translators
   (`connection.py::translate_sqlite_error`, `postgres.py::_translate_postgres_error`).
   **Your vocabulary is the blob analogue of that file, and your translator is
   the blob analogue of those two functions.** The idempotence rule
   ("an error that is already ours is returned unchanged") is a property of
   theirs; yours must have it.
6. `docs/adr/0026-s3-cold-lake-substrate.md` — the substrate's own ADR (why the
   module is still called `minio_backend.py`; what was deliberately deferred).
7. `src/paa/storage/coldlake/base.py` — the `BlobStore` protocol, the contract
   you are extending with an error clause.
8. `src/paa/storage/coldlake/minio_backend.py` — the six raw-escape sites.
9. `src/paa/storage/coldlake/cas.py` — the filesystem store; the same contract,
   the same failures, a different substrate.
10. `src/paa/core/errors.py` — `PaaError`, `StorageError`, and how `details`
    reach a ledger payload (`PaaError.to_payload` merges `details` **verbatim**).
11. `tests/storage/test_minio_backend.py` and `tests/storage/test_blobstore_contract.py`
    — the existing stub-based tests you will extend.
12. `docs/adr/README.md` — the index format, if you need it (you do **not**
    edit it; the orchestrator does).
13. `docs/agent-ops/20261022-w6-w7-w9/COMMS.md` — from the `WAVE-START` line.
14. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md` and
    `templates/report-deepdive-addendum.md` — the report shapes you must use.

## 3. Task and acceptance criteria

**Task T-001.** The blob substrate translates nothing.

**Root cause (verified in the plan's §0 evidence):** there is no blob error
vocabulary — `src/paa/storage/` has no `errors.py`, and the only neutral
vocabulary, `relational/errors.py`, is built from relational concepts
(`UniqueViolation`, `SerializationFailure`, `SchemaVersionError`, …) that
cannot express "absent", "forbidden", "unreachable" or "corrupt". So the S3
path raises the generic `paa.core.errors.StorageError` with a free-text message,
and **lets the driver's exception escape** from six sites. The `BlobStore`
protocol declares no error behaviour at all — which is why the divergence went
unnoticed.

**The recommended approach (you may deviate with evidence):**

1. A new `src/paa/storage/coldlake/errors.py` holding the blob vocabulary, in
   the style of `storage/relational/errors.py`: every member a
   `paa.core.errors.StorageError`; each carries the `substrate` that produced
   it; the docstring states *why* the vocabulary exists and what it must be
   able to express.
2. A translation function at the boundary — the blob analogue of
   `translate_sqlite_error` — that maps a botocore/boto3 exception to a
   concept, is **idempotent** (an error that is already one of ours comes back
   unchanged), and does **not** swallow a `PaaError` that is not ours.
3. The six raw-escape sites closed, and the `put()` / `put_stream()` divergence
   on a missing bucket resolved to **one** answer.
4. The `BlobStore` protocol gains an **error clause** — what each method raises
   when the object is absent, when the server refuses, when the bytes are
   corrupt. A contract that is silent about failure is what made the divergence
   invisible.
5. The CAS's residue closed too: a raw `OSError` out of `get_stream`, and a raw
   `zstandard.ZstdError` out of a corrupt frame.
6. The `sha256=key` mislabelling fixed — `_body`'s not-found path passes the
   **S3 key** where the contract says the **digest**.
7. `docs/adr/0032-blob-error-vocabulary.md` — the ADR.

**Acceptance criteria (measurable):**

* Every exception that can leave `S3BlobStore` and `ContentAddressedStore` is
  one of: a member of the new vocabulary, or a `PaaError` that is not ours
  (a programming error, which must not be translated). **No `botocore`
  exception escapes** — assert it, do not assert it in a comment.
* `put()` and `put_stream()` on a missing bucket raise **the same** type.
* The translation is **idempotent**: passing an error that is already one of
  ours returns it unchanged.
* The `BlobStore` protocol's docstring states the error behaviour, and a test
  asserts that the two implementations agree with it (the existing
  `test_blobstore_contract.py` is the right home).
* **The tests that must exist and fail before the fix:**
  * a missing bucket raises the neutral "not found"-class error from **both**
    `put` and `put_stream` (today: a raw `ClientError` from `put`, a
    `StorageError` from `put_stream`);
  * a missing object raises the neutral "not found" error with the **digest**
    in the details (today: `sha256=key` — the S3 key);
  * a botocore `AccessDenied` is translated (today: a raw `ClientError` from
    `_ensure_bucket`);
  * an unreachable endpoint is translated (today: a raw
    `EndpointConnectionError`);
  * a mid-body read failure is translated (today: a raw botocore exception
    out of `_read_all` / `get_stream`);
  * a corrupt zstd frame raises the neutral "corrupt" error from **both** the
    streaming and the non-streaming path (today: a raw `zstandard.ZstdError`
    from the streaming path);
  * the translation is idempotent.
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/storage/test_blob_errors.py tests/storage/test_minio_backend.py tests/storage/test_blobstore_contract.py tests/storage/test_coldlake.py -q
  .venv/bin/python -m ruff check src/paa/storage/coldlake tests/storage
  .venv/bin/python -m mypy src/paa/storage/coldlake
  ```
* The gate must not regress: `ruff` 0 and `mypy` 0 across `src` and `tests`.

**A note on what NOT to do.** Do not add a `list` operation to the protocol
(decision D-6). Do not rename `minio_backend.py` (ADR-0026 defers it
deliberately). Do not change the `BlobStore` method signatures — the
signatures are already correct and pinned by `test_blobstore_contract.py`. Do
not touch `src/paa/config.py` (it is a hotspot and no task in this run owns it).

## 4. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/storage/coldlake/errors.py                (new)
src/paa/storage/coldlake/minio_backend.py
src/paa/storage/coldlake/base.py
src/paa/storage/coldlake/cas.py
src/paa/storage/coldlake/__init__.py
tests/storage/test_blob_errors.py                  (new)
tests/storage/test_minio_backend.py
tests/storage/test_blobstore_contract.py
docs/adr/0032-blob-error-vocabulary.md            (new)
```

**READ-ONLY:** everything else — `src/paa/core/errors.py`,
`src/paa/storage/relational/**`, `docs/adr/0025-*`, `docs/adr/0026-*`.

**FORBIDDEN:** `docs/deployment/docker.md` (W1-IMPL-02 owns it), `pyproject.toml`
(T-002 owns it in wave 2), `src/paa/config.py`, `src/paa/agents/**`,
`src/paa/memory/**`, `src/paa/marketplace/**`, `src/paa/skills/**`,
`docs/adr/README.md` (the orchestrator owns it), `docs/MCP_AND_PLUGINS.md`,
`AGENTS.md`.

**Need something outside?** Do not edit it. Post `NEED` in `COMMS.md`, finish
the safe part, and report `DEFERRED-DEPENDENCY` with the exact change required.

## 5. Parallel siblings this wave

| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| `W1-IMPL-02` | T-003 — the fetch-images ↔ compose parity | `deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`, `docker-compose*.yml`, `docs/adr/0030-*`, `docs/deployment/docker.md`, `.env.example` | the script's CLI and its env surface |
| `W1-IMPL-03` | T-004 — the `TYPE_CHECKING` leftover | `src/paa/memory/{contradiction,decay,curator}.py`, `src/paa/marketplace/installer.py`, `src/paa/skills/registry.py`, `src/paa/agents/{policy,context_agents}.py`, `tests/conventions/**` | the import graph of eight modules |

**Known interactions.** W1-IMPL-03 converts imports to runtime across
`src/paa/`; your new `errors.py` will be imported by `minio_backend.py` and
`cas.py` at runtime, so **the import graph changes under you**. Make sure your
module imports cleanly in a **fresh interpreter** (not just inside the test
session, where other modules are already imported). Neither sibling touches
your files. You cannot see their uncommitted work.

## 6. Skip list and known traps

* **Already fixed — do not redo:** the `BlobStore` **signatures** already match
  (fixed in `03f635a`); `test_blobstore_contract.py` already pins them. The
  W7 sentence "the `BlobStore` protocol and the cold lake's own store disagree"
  is **stale** at the signature level — the disagreement that remains is
  behavioural.
* **The MinIO rename is done.** Every `minio` occurrence in the tree is
  deliberate (a compatibility alias, a historical reference, or the
  deliberately-un-renamed filename). Do not "tidy" the alias; do not rename the
  module.
* **Traps:**
  * `_is_not_found` is **structural**, not class-based: it reads
    `exc.response["Error"]["Code"]` and falls back to the exception's *class
    name*. `botocore` is never imported. Your translator will need a robust
    way to recognise a not-found — and it must not import `botocore` at module
    scope (the whole point of the structural check is that `boto3` may be
    absent). **Consider carefully how your translator identifies a driver
    exception without importing the driver.**
  * `botocore` is **not installed** in this venv and **not** in the `dev`
    extra. Your tests must work **without** boto3 — the existing suite's
    `StubS3Client` raises a *duck-typed* exception whose `response` is a dict
    and whose class name is the signal. **Do not make the test suite require
    boto3.** (Wave 2's live suite is the one that installs it.)
  * `PaaError.to_payload()` merges `details` **verbatim** into a ledger
    payload. If your error carries a detail that could hold a credential or a
    secret, it is now in the ledger. Keep the details to identifiers.
  * `put_stream` spools to a `SpooledTemporaryFile`; a spool write failure is
    a raw `OSError` — decide deliberately whether that is *ours* to translate
    (it is a host resource failure, not a substrate failure) and say so in the
    report.
  * The `upload_fileobj` path raises `boto3.exceptions.S3UploadFailedError`
    (already wrapped at `minio_backend.py:323-326`) — the *inner* cause may be
    a botocore exception; decide whether to unwrap it and translate the cause.
* **Flaky / pre-existing failures:** none. The suite is green at the base SHA.
* **Do not run the full suite.** The gate is the orchestrator's job.

## 7. Environment and tests

* **Scoped commands only** (the ones in §3). Never the full suite, never a
  container restart, never a migration, never a fixed port.
* **No container is needed for your task.** The live S3 suite is wave 2's
  (T-002) and it owns the `pyproject.toml` and the `boto3` extra. Do not
  install `boto3`, do not add the marker, do not add the extra.
* `boto3` must never be imported at module scope in `src/paa/` — the substrate
  is optional and `boto3_available()` exists for exactly this reason.
* Kill any process you start.

## 8. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-impl/W1-IMPL-01-T-001.md`
  — using `templates/report-implementation.md`, **plus** the deep-dive
  addendum `templates/report-deepdive-addendum.md`. **Create it at the start
  and append as you work** — it protects against a crash and it is the record
  of the design decision you make *before* the code.
* **The design record comes first.** Section D1 of the addendum — the options
  you considered and the one you chose, with the failure modes of each — must be
  written **before** you edit `minio_backend.py`. If you change the design as
  you implement, say so in the addendum and say why.
* **Commits:** code first, then the report in a **separate docs-only commit**
  that lists the code commit's SHA. Trailers: `Task: T-001`, `Agent: W1-IMPL-01`,
  `Wave: 1`, `Report: <path>`. Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/storage/coldlake/errors.py src/paa/storage/coldlake/minio_backend.py …
  git commit -m "<msg>" -- src/paa/storage/coldlake/errors.py …
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.

## 9. Role-specific notes (deep dive)

* **Design first.** Write D1 before the code. The decision here has real
  trade-offs — how much of botocore's vocabulary to mirror, whether a
  translation is a *mapping* or a *classification*, what a caller can do with
  each error — and a reviewer will want to see the alternatives you rejected.
* **No unbounded recursion.** You may not spawn further agents. If the work is
  too large for your context, return `ESCALATE` with a proposed decomposition.
* **Answer the long-run test** (section 8 of the standing orders) in your
  report: Source / Load / Hostile / Operability / Reversal.
* **The blast radius is the boundary.** A caller that catches a concept today
  will catch it tomorrow; a caller that catches `StorageError` keeps working
  because every new member **is** a `StorageError`. State that compatibility
  argument explicitly in the ADR — it is the reason this change is safe.
