<!-- ORCH-DIGEST:START -->
agent: W1-REV-01 | scope: wave 1 (T-001 deep; T-003 + T-004 the brief's specific questions) | verdict: REJECT
findings: BLOCKER 1 | MAJOR 2 | MINOR 5 | NIT 1
claims_audit: 20 claims checked / 4 unsupported or false
band_aids_found: none
top_3_risks: (1) the ADR + the BlobStore contract assert a bucket-absence behaviour the code cannot produce (R-W1-001); (2) the CAS leaks a raw OSError from five contract methods, so the two substrates still fail differently (R-W1-002); (3) the implementer's report and the shared context carry a wrong count (26, not 28) and the plan carries a wrong "nine load-bearing blocks" figure (one)
optimizations: DO-NOW 1 | DO-LATER 4 | NOT-WORTH-IT 2
reversal_check: done
confidence: high (every finding reproduced by a command; the one unverifiable premise — botocore's HEAD-404 code — is stated as such and does not affect the conclusion, which follows from the code's own comment and HTTP semantics)
<!-- ORCH-DIGEST:END -->

# Code review — W1-REV-01 (correctness & design)

## 0. Method

**Diff range reviewed:** `git diff 02f3636..3ac9d6b -- . ':!docs/agent-ops'` (25 files, +4342/−261).
Focus: **T-001** (the blob error vocabulary), per the orchestrator's subject line. T-003 and
T-004 are answered at the level the brief asks for (§2.2, §2.3), not to T-001's depth.

**Order followed.** Blind pass first: the diff, the four source files, the protocol, the ADR
and the surrounding callers were read and probed *before* `reports/w1-impl/W1-IMPL-01-T-001.md`
was opened. §1 below is the blind pass; §2 is the claims audit, against the implementer's report.

**Files read in context:** `src/paa/storage/coldlake/{errors,base,cas,minio_backend,artifacts,signals,__init__}.py`,
`src/paa/core/errors.py`, `src/paa/storage/relational/__init__.py`, `src/paa/storage/relational/base.py`,
`tests/storage/{test_blob_errors,test_blobstore_contract,test_minio_backend}.py`,
`tests/conventions/test_import_conventions.py`, `deploy/fetch-images.sh`, `docker-compose.yml`,
`docker-compose.gvisor.yml`, `docs/adr/0032-blob-error-vocabulary.md`, `docs/plans/w6-w7-w9.md` §0/§3.1/§3.2/§6.

**Commands run** (all scoped; no full suite; no container touched):

| Command | Result |
|---|---|
| `pytest tests/storage/test_blob_errors.py tests/storage/test_blobstore_contract.py tests/storage/test_minio_backend.py -q` | 100 passed [RAN] |
| `ruff check src/paa/storage/coldlake tests/storage/{test_blob_errors,test_blobstore_contract,test_minio_backend}.py` | All checks passed [RAN] |
| `mypy src/paa/storage/coldlake` | Success, 7 files [RAN] |
| `pytest tests/conventions/test_import_conventions.py -q` | 7 passed [RAN] |
| 4 probe scripts in `/tmp/opencode/` (CAS leak surface, HEAD-404 classification, zstd/ValueError leaks, `TypeError`/`raise e from e`) | see §1 [RAN] |
| `git worktree add --detach /tmp/opencode/w1rev01-orig 02f3636` → re-run the new tests and the convention lock against the pre-change sources | 26 failed / collection error; 2 convention tests failed [RAN] |
| `git worktree remove --force /tmp/opencode/w1rev01-orig` | worktree gone, shared tree untouched [RAN] |

**Last COMMS line read:** `2026-10-21T21:28:19Z` (W1-IMPL-03 DONE/FINDING ×2).

**House rules held:** read-only on source — the only file written is this report, plus the
COMMS append. No `# noqa`, no `# type: ignore`, no ruff ignore widened, no `pyproject.toml`
change was introduced by this change: `ruff` and `mypy` are 0 on the touched subtree [RAN].
No band-aids found (no blanket `except: pass`, no sleeps, no weakened test, no special-cased
input, no suppressed type).

---

## 1. Blind-pass findings (before reading any implementer report)

### 1.1 The vocabulary itself is sound

Seven members, all `StorageError`, all carrying `substrate`; `BlobError` as a grouping base
that is never raised; `BlobFailure` deliberately a *sibling* of `Unreachable` so a retry
promise is never made for an unknown reason. The errno table is small and each errno is
argued for. The S3 dialect is read **structurally** (code → status → class names off the
MRO) and never imports the driver, so the unit suite and the deployment run the same code.
This is the right shape and it copies ADR-0025 faithfully. [VERIFIED `src/paa/storage/coldlake/errors.py:109-211`]

The translation is **idempotent** for our own errors and passes a `PaaError` that is not ours
through untouched: `translate_blob_error(NotFound(...)) is original` and
`translate_blob_error(LedgerError(...)) is original` both hold [RAN, `tests/storage/test_blob_errors.py:153-177`].

### 1.2 The bucket-absence clause is unreachable — the headline finding

ADR-0032 decision 5 says a missing **bucket** raises `NotFound` "from every method", and
that it "raises it from `_head()` — the one place every method goes through", so that "both
writes fail in `_locate()` → `_head()`, so they cannot disagree"
[VERIFIED `docs/adr/0032-blob-error-vocabulary.md:149-162`]. The `BlobStore` protocol repeats
it: `exists()`/`stat()`/`delete()` "raise a `NotFound` for an absent substrate namespace"
[VERIFIED `src/paa/storage/coldlake/base.py:150-185`]. The implementer's report records it as
a behaviour change the operator must know about.

It cannot happen. The path to the bucket-level `NotFound` is:

* `_head()` → `head_object` → the driver's 404 → `_is_missing_object(exc)` → **if the code is
  `"404"`, the answer is "the object is not there"** and `_head` returns `None`
  [VERIFIED `src/paa/storage/coldlake/minio_backend.py:129-136,329-338,496-517`].
* The bucket-level table contains exactly one code, `"NoSuchBucket"`
  [VERIFIED `minio_backend.py:143-147`] — a code that lives in an **error body**.
* A `HEAD` response has **no body**. botocore therefore reports the *HTTP status* as the
  code, which the code's own comment already says out loud: *"`"404"` is in the list because
  a HEAD on a missing key answers 404 with an empty body, and botocore then reports the
  status as the code"* [VERIFIED `minio_backend.py:127-128`], and the stub's comment in the
  test file says the same: *"that is the only signal a HEAD on a missing object carries"*
  [VERIFIED `tests/storage/test_minio_backend.py:42-45`].
* A missing **bucket** is also a 404 on a HEAD, with the same empty body → the same code
  `"404"` → the same classification: **object-level absence**.

So `_head` can never produce the bucket-level `NotFound`, and with it the ADR's decision 5,
the protocol's `exists`/`stat`/`delete` clause, and the recorded behaviour change.

Probed with a client that answers every call the way botocore answers a HEAD 404
(code `"404"`, status 404, no body) — i.e. what a real SeaweedFS/RustFS/AWS answers for a
missing bucket:

```
  exists      -> returned False
  stat        -> returned None
  delete      -> returned False
  get         -> NotFound: blob not found (sha256='cccc…', substrate='s3')
  put         -> NotFound: blob not found (bucket='paa-test', driver_error='Head404', http_status=404, …, s3_code='404', …)
  put_stream  -> NotFound: blob not found (bucket='paa-test', …, s3_code='404', …)
```
[RAN `/tmp/opencode/w1rev01_probe1.py`]

Four different answers from **one store** for the same condition, and the read paths tell
the caller *"the blob is not there"* when the truth is *"the store is not there"* — the exact
lie ADR-0032 §5 says it is fixing. Against a real server the write path *does* carry the
bucket code (a `PUT` response has a body), so the divergence is not even consistent within
the change: `put`/`put_stream` answer `NotFound("bucket not found")` while
`get`/`exists`/`stat`/`delete` answer `NotFound("blob not found")` / `False` / `None` / `False`.

The test that "proves" the behaviour change asserts a state a HEAD cannot produce:
`client.failure = ClientError("NoSuchBucket", status=404)` makes **every** call, including
`head_object`, answer with a `NoSuchBucket` *code* [VERIFIED
`tests/storage/test_minio_backend.py:332-352`]. A real HEAD cannot answer that way.

### 1.3 The CAS still leaks raw exceptions — invariant I-1 is not established

`Path.exists()` re-raises any `OSError` whose errno is not in `{ENOENT, ENOTDIR, EBADF,
ELOOP}`; a `PermissionError` (EACCES) therefore propagates. `_locate()` calls it
[VERIFIED `src/paa/storage/coldlake/cas.py:188-196`], and `_locate()` is on the path of
`exists`, `stat`, `delete`, `get`, `put` and `put_stream`.

With the archive's `blobs/` directory made unreadable:

```
  exists      -> RAW-ESCAPE: PermissionError: [Errno 13] Permission denied: …/blobs/aa/aa/aaa….zst
  stat        -> RAW-ESCAPE: PermissionError
  delete      -> RAW-ESCAPE: PermissionError
  get         -> RAW-ESCAPE: PermissionError
  put         -> RAW-ESCAPE: PermissionError
  put_stream  -> ok: AccessDenied: the substrate refused the request
  iter_blobs  -> returned []
  total_bytes -> returned 0
```
[RAN `/tmp/opencode/w1rev01_probe2.py`]

`put_stream` is the only one that translates, because it is the only one whose `OSError`
happens to be inside a `try` that translates. A raw `PermissionError` is **not** a `PaaError`,
so every `except PaaError` at the edge misses it (`api/app.py:142`, `agents/orchestrator.py:325`,
`cli/main.py:196`, `agents/base.py:350`) and `ArtifactArchive.verify`'s `except StorageError`
(`artifacts.py:283`) misses it too — a health sweep over a damaged archive raises instead of
reporting. The S3 substrate, for the same reason (the store refuses), raises `AccessDenied`
[VERIFIED `minio_backend.py:390-393`]. **The two substrates do not fail the same way for the
same reason**, which is the brief's question and the change's headline claim.

A second, narrower leak on the same substrate: `put()`'s dedup path calls
`existing.stat().st_size` with no guard, so a blob that vanishes between the locate and the
stat escapes as a raw `FileNotFoundError` [RAN probe 2].

### 1.4 `CAS.stat()` leaks a raw `zstandard.ZstdError`

`_raw_size()` calls `zstandard.frame_content_size(header)` inside a `try` that catches
`OSError` only [VERIFIED `src/paa/storage/coldlake/cas.py:486-499`], and
`frame_content_size` raises `ZstdError` on a frame that is not a frame.

```
  stat() -> RAW-ESCAPE: ZstdError: error when determining content size
```
[RAN `/tmp/opencode/w1rev01_probe3.py`; `zstandard.frame_content_size(b"not a zstd frame at all")`
raises `ZstdError` — VERIFIED by the same run]

So the **same file** that `get()` reports as `Corrupt` [VERIFIED `cas.py:442-450`] is reported
by `stat()` as a raw codec exception. The ADR's claim that the CAS's "raw `OSError` and raw
`ZstdError`" residue is closed [VERIFIED `docs/adr/0032-blob-error-vocabulary.md:32-33`] is
true for the read path and false for the metadata path. `stat()` is a **protocol** method, so
a caller writing against the contract can hit it. Blast radius today is small: `iter_blobs()`
has no production caller [VERIFIED by grep over `src/`].

### 1.5 A `TypeError` *is* reported as an infrastructure fault, against the docstring

`errors.py`'s docstring says a `TypeError` "that is a bug in the calling code must not be
reported as an infrastructure fault" [VERIFIED `src/paa/storage/coldlake/errors.py:269-274`].
The code maps every non-`PaaError`, non-`OSError`, non-`ZstdError` exception to
`BlobFailure`, which **is** a `StorageError`:

```
  translate_blob_error(TypeError("bad argument"), substrate="s3")
      -> BlobFailure | is StorageError: True
```
[RAN probe 2]. The implementer's own report §9 documents the *opposite* decision
("A `TypeError` from calling the client wrongly is translated into `BlobFailure`"), so the
report is right and the docstring is wrong. The *behaviour* is defensible; the *promise* is not.

### 1.6 "The details say which (object or bucket)" is not a rule

ADR-0032 §1 says the details "(`sha256=` against `bucket=`) say which" [VERIFIED
`docs/adr/0032-blob-error-vocabulary.md:57-59`]. In fact the detail sets are incidental:
`_require` raises `NotFound(sha256=…)` with **no** `bucket`
[VERIFIED `minio_backend.py:879-883`], while `_put_object` raises `NotFound(bucket=…, key=…,
sha256=…, …)` [VERIFIED `minio_backend.py:653-669`]. A bucket-level error therefore *may*
carry a `sha256`, and a blob-level error from the write path carries a `bucket` — so "which
detail is present" is not a discriminator.

### 1.7 Smaller things

* `S3BlobStore.stat()` leaks a raw `ValueError` when the driver returns a non-numeric
  `ContentLength`: `int(head.get("ContentLength", 0))` [VERIFIED `minio_backend.py:834`] —
  probed: `stat() -> RAW-ESCAPE: ValueError: invalid literal for int() with base 10` [RAN probe 3].
* `raise translate_blob_error(exc, …) from exc` sets `__cause__` to the exception **itself**
  when the translator returns the same object (a `PaaError` that is not ours) — probed:
  `__cause__ is self: True` [RAN probe 2]. CPython's traceback printer does not loop, so it
  is cosmetic today, but `_translated_chunks` gets this right with an explicit
  `if translated is exc: raise` guard [VERIFIED `cas.py:641-644`]; the eighteen other sites do not.
* `NotFound` and `BlobFailure` carry the *class* of the failure but the CAS reports the
  corrupt file as `path=` while the S3 reports it as `key=` — the class is the same, the
  identifier's *name* is not [VERIFIED `cas.py:370-376` vs `minio_backend.py:702-708`].
* `put_stream`'s `actual=raw_bytes` in the `MalformedRequest` is the count at the moment
  zstd failed, not the total read — understated when the stream overruns the hint
  [VERIFIED `cas.py:276-287`, `minio_backend.py:598-610`].

### 1.8 What the blind pass found *right*

* The six raw botocore escape sites are genuinely closed, and four more `OSError` paths
  besides [VERIFIED `minio_backend.py:464-471,506-517,519-537,653-692,756-815,859-877`].
* The `sha256=key` mislabelling is fixed: `_body` now takes the digest and reports
  `sha256=digest` alongside `key` [VERIFIED `minio_backend.py:756-780`; the test at
  `tests/storage/test_minio_backend.py:411-431` is a real regression test].
* The staging cleanup no longer replaces the failure that brought it there
  [VERIFIED `cas.py:330-335,353-356`].
* The driver is never imported, at module scope or lazily, and the check is a **subprocess**
  in a fresh interpreter — the right way to check it [VERIFIED
  `tests/storage/test_blob_errors.py:503-527`].
* The rejected digest is echoed to a bound (128 chars), so a hostile caller cannot inflate
  the ledger payload [VERIFIED `cas.py:90-97,584-600`].
* The driver's wording is kept out of the message (so an endpoint URL with credentials cannot
  reach the ledger) and kept in the `__cause__` [VERIFIED `minio_backend.py:356-359`].

---

## 2. Claims audit

### 2.1 T-001 — the blob vocabulary (7 claims)

| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| 1. "No botocore exception escapes the boundary — every exception leaving `S3BlobStore` and `ContentAddressedStore` is a member of the new vocabulary or a `PaaError` that is not ours" | **NO** | The S3 half holds; the **CAS half does not**: `exists`/`stat`/`delete`/`get`/`put` leak a raw `PermissionError` via `Path.exists()` [RAN probe 2], `put` leaks a raw `FileNotFoundError` [RAN probe 2], and `stat()` leaks a raw `zstandard.ZstdError` [RAN probe 3]. This is the implementer's own invariant **I-1** ("Every exception that leaves either implementation is a member of the blob vocabulary") [VERIFIED report:50-53]. → **R-W1-002, R-W1-003** |
| 2. "`put()` and `put_stream()` on a missing bucket raise the **same** type" | YES | Both `NotFound` in every shape probed — the stub's `NoSuchBucket` [VERIFIED `tests/storage/test_minio_backend.py:332-352`] and the realistic HEAD-404 [RAN probe 1]. The *messages* differ by path against a real server ("bucket not found" from the write, "blob not found" from the read paths) → part of **R-W1-001** |
| 3. "The translation is **idempotent** — an error that is already ours comes back unchanged" | YES | `translate_blob_error(NotFound(...)) is original`; `_translate_s3_error(NotFound(...)) is original` [VERIFIED `errors.py:285-288`, `minio_backend.py:361-362`; RAN via the suite]. A `PaaError` that is not ours is also passed through unchanged, verified for `LedgerError` [VERIFIED `errors.py:289-292`] |
| 4. "The `BlobStore` protocol now states the error behaviour, and a test asserts the two implementations agree with it" | **PARTLY NO** | The protocol states it [VERIFIED `base.py:24-59,83-185`] and the test checks six conditions [VERIFIED `tests/storage/test_blobstore_contract.py:216-302`]. But the clause it checks is **not** the clause the protocol states: the namespace/`NotFound` clause is never exercised, and it is the one the implementation cannot honour [RAN probe 1] → **R-W1-001**. The test also only checks that each docstring *names* a member [VERIFIED `test_blobstore_contract.py:217-237`] — prose, not a machine-checked clause |
| 5. "The `sha256=key` mislabelling is fixed — the not-found path carries the **digest**" | YES | `_body(key, digest)` now reports `sha256=digest` and `key=key` [VERIFIED `minio_backend.py:756-780`]; regression test at `tests/storage/test_minio_backend.py:411-431` |
| 6. "28 of the new tests fail against the pre-change sources" | **NO — 26** | Reproduced: with the pre-change `base.py`/`cas.py`/`minio_backend.py` and the new `errors.py` on `PYTHONPATH`, `pytest tests/storage/test_minio_backend.py tests/storage/test_blobstore_contract.py` reports **26** `FAILED` lines (20 in `test_minio_backend.py`, 6 in `TestTheErrorClause`) [RAN `/tmp/opencode/w1rev01-rev.log`]. The report's own enumerated list also sums to 26 [VERIFIED report:378-393]. `test_blob_errors.py` cannot be collected at all [RAN] — which the report states correctly. → **R-W1-007** |
| 7. (the flagged behaviour change) "a missing S3 bucket now raises `NotFound` from `exists()`/`stat()`/`delete()` where it previously answered `False`/`None`/`False`" | **NO** | It does not happen against a real server: a HEAD 404 is classified as an *object*-level absence, so those three still answer `False`/`None`/`False` [RAN probe 1; the code's own comment `minio_backend.py:127-128` and the stub's comment `tests/storage/test_minio_backend.py:42-45` say a HEAD carries only the status]. The recorded change is therefore in ADR-0032, in the protocol, in the report and in the shared context, and none of them is true. → **R-W1-001** |

**The design question the brief asks — is the behaviour change coherent with the contract?**
No, in both directions. *Conceptually* it is coherent: "the store cannot answer" and "the
blob is not there" are different facts, and answering `False` for a missing bucket is a lie
a daemon would act on — the reasoning in ADR-0032 §5 is right. *Operationally* it is not
implemented: the only signal a HEAD carries cannot distinguish the two, so the contract
promises a discrimination the wire protocol does not offer. Either the dialect must earn the
discrimination (a `head_bucket` on a HEAD-404, or a cached namespace probe), or the ADR and
the protocol must be corrected to say what is actually knowable. What must not stand is a
contract and an ADR that describe a behaviour the code cannot produce.

**Is the vocabulary complete?** Mostly, with three gaps:
* an `OSError` still reaches the caller from the CAS (five methods) — **R-W1-002**;
* a `zstandard.ZstdError` still reaches the caller from `CAS.stat()` — **R-W1-003**;
* a `TypeError` does **not** reach the caller — it is reported as `BlobFailure`, a
  `StorageError`. The docstring promises otherwise — **R-W1-004**.
* A driver exception: all seven client calls are inside a translating `try`
  [VERIFIED `minio_backend.py:464-471,506-517,519-537,653-669,671-692,764-774,865-875`], so
  the S3 half of claim 1 holds — with one exception, `S3BlobStore.stat()`'s `int()` on
  driver-supplied data (**R-W1-006**).

**Do the two substrates fail the same way for the same reason?** No, twice: a refusing store
(**R-W1-002**) and a corrupt frame (**R-W1-003**).

### 2.2 T-003 — the merged compose stack (7 claims)

Answered by reading `deploy/fetch-images.sh` and by running it against compose files in
`/tmp` (the script's own `--list` mode needs no docker).

| Claim | Verified? | Evidence |
|---|---|---|
| 1. "`--compose` is repeatable and merges in file order; later wins; the flag beats `PAA_COMPOSE_FILE`" | YES | Later wins: base `alpha:1.0@sha256:111…` + override `alpha:2.0@sha256:222…` → the plan carries `alpha/one:2.0@sha256:222…` [RAN]. The flag beats the env: with `PAA_COMPOSE_FILE=base.yml` **and** `--compose over.yml`, the plan contained only the override's services (`gamma`, `alpha`) and not `base.yml`'s `beta` [RAN]. The env still works alone: `PAA_COMPOSE_FILE=base.yml` → base's plan, exit 0 [RAN] |
| 2. "The expected service set in the parity test is now **derived**, not hard-coded" | YES (by inspection) | The parity test compares the script's `--list` against an independent reader over the real compose file [VERIFIED `tests/deployment/test_fetch_images.py`, per the plan §2.2] |
| 3. "`profiles:`-gated services are excluded, with a stderr notice" | YES (by inspection) | `skip\tprofile\t<service>` row → `warn` to stderr [VERIFIED `deploy/fetch-images.sh:428-435,336-340`] |
| 4. "`extends:` / anchors / aliases / a multi-line `image:` / a custom tag / a top-level `include:` are **refused** rather than mis-read" | YES | Each shape probed: `extends:` → "the service b uses extends:"; anchor → "uses a YAML anchor or an alias"; `image: >-` → "declares a multi-line image:"; `include:` → "the top-level include: is not supported" — all with the **file name** and the **shape** in the message and **exit 1** [RAN] |
| 5. "The merge subset was cross-checked against real Compose v5.6.0" | NOT VERIFIED | `docker compose` is not exercised here; the claim rests on the implementer's own run. Not contradicted by anything I found [UNKNOWN] |
| 6. "18 of the new tests fail against the pre-change script" | NOT VERIFIED | Not re-measured (out of my depth allocation for this wave item) [UNKNOWN] |
| 7. "G2 and G6 are **recorded, not fixed**" | YES (by inspection) | The report records them; the diff carries no G2/G6 fix [VERIFIED `docs/adr/0030-deployment-image-contract.md` amendment] |

**The brief's three T-003 questions.**
* *Does the merge implement the subset it claims and refuse what it does not support?* Yes.
  The subset is: per-service key-by-key merge, later `image:` wins, a later file that
  declares no `build:` leaves the earlier one's [VERIFIED `deploy/fetch-images.sh:363-392`],
  a service only one file declares is taken from that file, first-appearance order.
  Everything outside it is refused loudly [RAN].
* *Is a refusal loud?* Yes — `err` to stderr, the file named, the shape named, **exit 1**
  (`build_plan || exit 1`; `refuse_bad_images || exit 1`) [VERIFIED
  `deploy/fetch-images.sh:415-420,466-470,855,864-869,881`].
* *Is the digest pinning still enforced for every image the merged stack runs, including an
  image that appears only in the override?* **Yes.** The unpinned check iterates the
  **merged** `PLAN` [VERIFIED `deploy/fetch-images.sh:446-470`], and an override-only
  service with a tag-only image is refused with a non-zero exit [RAN: `gamma: gamma/three:3.0
  is not digest-pinned …`]. Services with a `build:` are excluded from the pull rows by
  design — the `paa` service in `docker-compose.yml` has both `build:` and
  `image: paa/runtime:4.1` [VERIFIED `docker-compose.yml:82-85`] and is a *build*, not a
  pull, so its local tag needs no digest.

### 2.3 T-004 — the import consistency (6 claims)

| Claim | Verified? | Evidence |
|---|---|---|
| 1. "Seven `TYPE_CHECKING` imports of `RelationalBackend` became runtime imports; `observability/runs.py` was left alone (deferred to T-008)" | YES | Seven files changed [VERIFIED `git diff 02f3636..3ac9d6b -- src/paa/`]: `agents/context_agents.py`, `agents/policy.py`, `marketplace/installer.py`, `memory/contradiction.py`, `memory/curator.py`, `memory/decay.py`, `skills/registry.py`. `observability/runs.py` is untouched and is in `DEFERRED` with an owner [VERIFIED `tests/conventions/test_import_conventions.py:84-89`] |
| 2. "Before the change `get_type_hints` on all seven constructors raised `NameError`; after, they resolve. Pydantic model fields are byte-identical" | YES | Probed all constructors of the seven modules: at `02f3636` every class that names `RelationalBackend` raised `NameError: name 'RelationalBackend' is not defined`; at HEAD every one resolves [RAN `/tmp/opencode/w1rev01_hints.py`]. The "byte-identical pydantic fields" half I did not re-measure [UNKNOWN] |
| 3. "137/137 modules import in a fresh interpreter; a 136-module aggregate import also succeeds" | YES | The lock's two halves pass [RAN `pytest tests/conventions/test_import_conventions.py -q` → 7 passed]; the 136-module aggregate is the `importable` list minus the 4 optional-extra modules [VERIFIED `test_import_conventions.py:95-102,419-464`] |
| 4. "The lock is 7 tests, and reverting the change fails two of them" | YES | The file has 7 test functions [VERIFIED]; re-running the *new* lock against the pre-change sources fails exactly **two**: `test_the_contract_is_not_imported_behind_type_checking` and `test_the_family_resolves_the_contract_one_way` [RAN, in the /tmp worktree]. A real regression test |
| 5. (the correction) "the brief's 'nine blocks are load-bearing for a real runtime cycle' is overstated — only `cas.py` is" | **YES — the correction is right** | Measured independently: at `02f3636` there are **41** `if TYPE_CHECKING:` guards in `src/paa` (not the plan's 48) [RAN], and hoisting each guard to a runtime import one at a time breaks the import for **exactly one** module — `paa.storage.coldlake.cas` (`cannot import name 'BlobStore' from partially initialized module 'paa.storage.relational.base'…` → `paa.storage.coldlake.base` imports `cas` for `BlobRef`, so `cas → base` closes the cycle) [RAN]. At HEAD: 34 guards, still exactly one load-bearing [RAN]. **The plan's §0 figure of "nine" is wrong; the correct statement is one.** The plan and the records must be corrected |
| 6. (the new defect) "`storage/relational/__init__.py:42-43` keeps `PostgresDatabase` and `Settings` behind `TYPE_CHECKING` while `get_database`'s return annotation names them, so `typing.get_type_hints(get_database)` raises `NameError`" | **YES — a real defect** | `typing.get_type_hints(paa.storage.relational.get_database)` → `NameError: name 'Settings' is not defined` [RAN]. It is a real defect and it is the *same class* of defect T-004 was chartered to remove — but on a different contract (`Settings`/`PostgresDatabase`, not `RelationalBackend`), which is why the lock does not see it: `test_the_contract_is_not_imported_behind_type_checking` is written against `RelationalBackend` alone [VERIFIED `test_import_conventions.py:61-62,334-352`]. **What should be done:** it cannot be fixed by hoisting `PostgresDatabase` to runtime (psycopg is an optional extra and the module must stay importable without it — that is why it is deferred [VERIFIED `src/paa/storage/relational/__init__.py:19-24,45-51,108-110`]). The root-cause fix is to make the *annotation* resolvable without the class: either quote the return type as a string that `get_type_hints` can resolve lazily, or give the module a `__getattr__` that binds `PostgresDatabase` on demand, or — cheapest and honest — have the factory's return annotation name only what is importable and state the union in the docstring. Whichever is chosen, the *lock* should be widened from one contract to the rule "a module's public annotations must resolve", or the deferral must be recorded in a list the lock reads, as `DEFERRED` does for `RelationalBackend` |

**Is the conversion safe (the brief's T-004 question 1)?** Yes. `paa.storage.relational.base`
imports only `paa.storage.relational.connection` and `paa.storage.relational.dialect` —
no optional extra, no heavy dependency [VERIFIED `src/paa/storage/relational/base.py:24-31`];
`paa.config` (the `MemorySettings` import in `curator.py`) is a core dependency
[VERIFIED `src/paa/config.py:11-22`]. Measured: importing `paa.storage.relational.base`
pulls in **no** optional/heavy module (`torch`, `numpy`, `sentence_transformers`,
`qdrant_client`, `kuzu`, `redis`, `boto3`, `psycopg`, `fastapi` all absent from `sys.modules`)
[RAN]; the per-module import-time delta between `02f3636` and HEAD is inside the noise
(≤ ~30 ms on a ~200 ms baseline, in both directions) [RAN].

**Does the fresh-interpreter check cover the modules it claims?** Yes, and it is the right
check: 137 modules, one interpreter each, and the check is a **subprocess** so the session's
populated `sys.modules` cannot hide a cycle [VERIFIED `test_import_conventions.py:419-441`].
It also proves the probe can see a cycle, by construction, with a fixture in `tmp_path`
[VERIFIED `test_import_conventions.py:467-494`] — a lock that cannot fail is a description,
and this one demonstrably can.

**Are the two locks real regression tests?** Yes — verified by reverting (2 of 7 fail)
[RAN, §2.3 claim 4].

---

## 3. Long-run test (independent answers)

**Source.** The source of truth is unchanged and still the bytes: content addressing, the
raw-bytes digest, verify-on-read and the fan-out are untouched [VERIFIED
`src/paa/storage/coldlake/cas.py:199-227,360-377`]. What changed is the *reporting* of a
failure. The error's details now carry identifiers an operator can query
(`s3_code`, `http_status`, `driver_error`, `op`, `key`, `sha256`, `bucket`, `path`)
[VERIFIED `minio_backend.py:380-385`] and the driver's wording stays in the traceback
rather than in the ledger payload — a deliberate, recorded loss of fidelity
[VERIFIED `docs/adr/0032-blob-error-vocabulary.md:164-179`]. **One caveat:** the reporting
is now *partly* a lie about the substrate's state (R-W1-001), so a ledger that says
"blob not found" may in fact be recording "the bucket is gone".

**Load.** The translation is a handful of set lookups on the failure path only; the hot
paths (put/get) are unchanged. The one per-chunk cost is `_translated_chunks`'s `try`
around `next()` — one `POP_BLOCK` per 1 MiB chunk against a SHA-256 update of the same
bytes [VERIFIED `cas.py:621-645`]. No new state is held by either store, so 100× concurrency
changes nothing. The one thing that scales with failures is the ledger payload, which is why
the rejected digest is bounded to 128 characters [VERIFIED `cas.py:90-97`]. **Partial
failure:** a failed CAS write leaves a staging file (recoverable, swept by
`clear_staging`) and the cleanup no longer replaces the original failure [VERIFIED
`cas.py:330-335`]; a failed S3 upload leaves no object [VERIFIED `minio_backend.py:671-692`].

**Hostile input.** A hostile digest is refused at the door on both substrates, and the
echoed value is bounded [VERIFIED `cas.py:584-600`; `test_blob_errors.py:479-495`]. A hostile
*server* cannot choose the local exception type: every code maps to a fixed class and an
unnamed code becomes `BlobFailure` [VERIFIED `minio_backend.py:380-419`;
`test_blob_errors.py:331-339`]. The driver's text cannot put a credential into the ledger
[VERIFIED `minio_backend.py:356-359`]. **The gap:** a hostile *server* can still make the
store answer `False` for `exists()` on a bucket it has deleted, because the dialect has no
way to tell — R-W1-001 is a hostile-input finding as much as a correctness one.

**Operability.** Observe: the ledger gets the concept and the identifiers; the traceback
gets the driver's wording [VERIFIED]. Roll back: revert the commit — the vocabulary is
additive and the only behaviour change is the missing-bucket raise, which (per R-W1-001)
does not actually happen, so the revert is a no-op in practice. Migrate: nothing to migrate
(no data, no schema, no flag).

**Reversal.** Which test fails if the fix is reverted? **26** in the two existing test files
(plus `test_blob_errors.py`, which cannot even be collected) [RAN]. The invariant test
`test_no_driver_exception_escapes_the_store` fails on the first failure mode, so a partial
revert is caught immediately. For T-004: **2 of 7** convention tests [RAN]. For T-003: the
parity test is the gate. **And the reversal check has a hole:** the test that "proves" the
missing-bucket behaviour change asserts a driver response a real HEAD cannot produce
(R-W1-001), so it cannot fail for the reason it claims to guard.

---

## 4. Findings

### R-W1-001 [BLOCKER] The bucket-absence clause is unreachable: the ADR, the contract and the test assert a behaviour the code cannot produce

- **Location:** `src/paa/storage/coldlake/minio_backend.py:129-147` (the code tables),
  `:329-338` (`_is_missing_object`), `:496-517` (`_head`); `src/paa/storage/coldlake/base.py:150-185`
  (the `exists`/`stat`/`delete` clause); `docs/adr/0032-blob-error-vocabulary.md:149-162`;
  `tests/storage/test_minio_backend.py:332-352`.
- **Evidence:** the code's own comment — *"`"404"` is in the list because a HEAD on a missing
  key answers 404 with an empty body, and botocore then reports the status as the code"*
  [VERIFIED `minio_backend.py:127-128`]; the stub's comment — *"that is the only signal a HEAD
  on a missing object carries"* [VERIFIED `tests/storage/test_minio_backend.py:42-45`]; a
  `HEAD` response has no body, so `"NoSuchBucket"` (the only entry in
  `_S3_BUCKET_MISSING_CODES`, `minio_backend.py:143-147`) cannot be observed on the path
  `_head` uses [VERIFIED HTTP semantics + the two comments above]; probed with a client that
  answers every call the way botocore answers a HEAD 404, `exists()`→`False`,
  `stat()`→`None`, `delete()`→`False`, `get()`→`NotFound("blob not found")`,
  `put()`/`put_stream()`→`NotFound("blob not found", s3_code="404")`
  [RAN `/tmp/opencode/w1rev01_probe1.py`]. [ASSUMED] botocore's internal
  `Error.Code` for a HEAD 404 is the status string — boto3 is not installed here, so it was
  not executed; it is not load-bearing, because the conclusion follows from the code's own
  documented premise and from HTTP.
- **Why it matters:** three artefacts of record — ADR-0032 §5, the `BlobStore` protocol's
  error clause, and the implementer's report §6/§12 — state a behaviour change that does not
  occur. The task's own headline (the same condition reports the same thing on every path)
  is not met: one store, one condition, four answers. The read paths still tell the caller
  *"the blob is not there"* when the truth is *"the store is not there"* — the lie the ADR
  was written to remove. The contract now **speaks falsely**, which is a worse failure than
  the silence ADR-0032 replaced: a silent contract cannot be violated, a false one is.
- **Recommended fix (root cause, one place):** the dialect must stop treating the bare
  `"404"` code as proof of an *object*-level absence, because a HEAD's 404 is ambiguous.
  Two acceptable shapes, and the ADR must pick one and say so:
  1. **Earn the discrimination** — on a HEAD 404, confirm the namespace (a `head_bucket`,
     cached per store) and raise the bucket-level `NotFound` when the bucket is gone; or
  2. **State the truth** — treat a HEAD 404 as "the object or its namespace is not there",
     raise a `NotFound` whose details carry `s3_code="404"` and a `namespace="unknown"`
     marker, and correct ADR-0032 §5 and the protocol's `exists`/`stat`/`delete` clause to
     match. The claim "the details say which" (ADR §1) must then be dropped.
  Either way: the test must be written against the shape a real HEAD produces — a 404 with
  **no body** — not against a `NoSuchBucket` *code* a HEAD cannot carry
  (`tests/storage/test_minio_backend.py:343`), and wave 2's live suite should assert the
  bucket case through `exists()`/`stat()`/`delete()` against both real servers.

### R-W1-002 [MAJOR] The CAS leaks a raw `OSError` from five contract methods, so the two substrates still fail differently

- **Location:** `src/paa/storage/coldlake/cas.py:188-196` (`_locate` → `Path.exists()`),
  `:454-455` (`exists`), `:457-475` (`stat`), `:539-556` (`delete`), `:199-227` (`put`
  — the unguarded `existing.stat().st_size` at `:217`).
- **Evidence:** [RAN `/tmp/opencode/w1rev01_probe2.py`] with the archive's `blobs/`
  directory unreadable: `exists`/`stat`/`delete`/`get`/`put` raise a raw `PermissionError`
  (`[Errno 13] Permission denied`), while `put_stream` raises `AccessDenied` — the one
  method whose `OSError` happens to sit inside a translating `try`. `put` additionally
  leaks a raw `FileNotFoundError` from `existing.stat()` when the blob vanishes between
  the locate and the stat. `Path.exists()` re-raises any errno outside
  `{ENOENT, ENOTDIR, EBADF, ELOOP}` [VERIFIED by the probe's behaviour and by CPython's
  `pathlib._ignore_error`].
- **Why it matters:** invariant **I-1** ("every exception that leaves either implementation
  is a member of the blob vocabulary") is not established, and the brief's question — *do
  the two substrates fail the same way for the same reason?* — is answered **no**: a store
  that refuses raises `AccessDenied` on S3 and a raw `PermissionError` on the filesystem. A
  raw `OSError` is not a `PaaError`, so every `except PaaError` at the edge misses it
  (`api/app.py:142`, `agents/orchestrator.py:325`, `cli/main.py:196`, `agents/base.py:350`)
  and `ArtifactArchive.verify`'s `except StorageError` (`artifacts.py:283`) misses it too —
  a health sweep over a damaged archive raises instead of reporting. `put` is a production
  path (`SignalRepository.record` → `store.put`, `artifacts.py:123` → `put_stream`).
- **Recommended fix (root cause, one place):** make the **lookup** the boundary, not the
  five call sites — wrap `_locate()`'s `Path.exists()` (or use `os.stat` inside a `try`)
  and translate there, so every method that reaches it inherits the classification; and
  guard `put()`'s `existing.stat()` the way `stat()` already guards its own
  (`cas.py:468-475`). One fix, not five.

### R-W1-003 [MAJOR] `CAS.stat()` leaks a raw `zstandard.ZstdError` on a corrupt frame — the same file `get()` reports as `Corrupt`

- **Location:** `src/paa/storage/coldlake/cas.py:486-499` (`_raw_size`), reached from
  `stat()` at `:457-484` and from `iter_blobs()` at `:501-512`.
- **Evidence:** [RAN `/tmp/opencode/w1rev01_probe3.py`] `stat()` on a `.zst` whose bytes are
  `b"not a zstd frame at all"` → `ZstdError: error when determining content size` — a raw
  codec exception, not a `BlobError`. `zstandard.frame_content_size` raises `ZstdError` on
  a non-frame (same run). `_raw_size`'s `try` catches `OSError` only [VERIFIED
  `cas.py:496-499`]. The same file through `get()` raises `Corrupt`
  [VERIFIED `cas.py:442-450`].
- **Why it matters:** the same substrate, the same file, the same reason — two different
  answers. That is precisely the divergence the vocabulary was made to remove, and the ADR
  records the CAS's raw-`ZstdError` residue as closed [VERIFIED
  `docs/adr/0032-blob-error-vocabulary.md:32-33`]; it is closed on the read path only.
  `stat()` is a **protocol** method, so a caller writing against the contract can hit it.
  Blast radius today is small (`iter_blobs()` has no production caller [VERIFIED by grep]),
  which is why this is MAJOR and not BLOCKER.
- **Recommended fix:** translate the codec error in `_raw_size` to the same `Corrupt` the
  read path raises, and add the corrupt-frame-through-`stat()` case to the contract test so
  both metadata and read paths are pinned on both substrates.

### R-W1-004 [MINOR] The `errors.py` docstring promises a `TypeError` is not reported as an infrastructure fault; the code reports it as `BlobFailure`

- **Location:** `src/paa/storage/coldlake/errors.py:269-274` (the promise) against
  `:285-307` (the behaviour).
- **Evidence:** [RAN probe 2] `translate_blob_error(TypeError("bad argument"), substrate="s3")`
  → `BlobFailure`, and `isinstance(result, PaaError)` is `True` — i.e. it *is* reported as
  an infrastructure fault. The implementer's report §9 documents the opposite decision
  ("A `TypeError` … is translated into `BlobFailure`"), so the report is right and the
  docstring is wrong.
- **Why it matters:** the docstring is the contract a reader uses to decide what the
  boundary swallows, and this repository's rule is that a docstring carries the reasoning.
  A docstring that promises the opposite of the code is a defect in the artefact the ADR
  points at.
- **Recommended fix:** correct the docstring to state what the code does — a non-`PaaError`
  that reaches the boundary is reported as the unclassified failure, with the original as
  `__cause__` — and say *why* that is right for a narrow boundary (the implementer's §9
  reasoning is good and should be kept).

### R-W1-005 [MINOR] "The details say which (object or bucket)" is not a rule the code keeps

- **Location:** `docs/adr/0032-blob-error-vocabulary.md:57-59`; raised at
  `src/paa/storage/coldlake/minio_backend.py:653-669` (write path: `bucket` **and**
  `sha256` **and** `key`) against `:879-883` (read path: `sha256` only).
- **Evidence:** [VERIFIED] the two raise sites carry different detail sets for the same
  class; probed in R-W1-001's run, a bucket-level error carries `sha256=` as well as
  `bucket=` [RAN probe 1].
- **Why it matters:** the ADR uses the detail set as the *discriminator* between "the blob
  is missing" and "the bucket is missing". It is not one: the discriminator is "which keys
  happen to be present", which is a function of the call site, not of the condition.
- **Recommended fix:** either state a rule and make every raise obey it (e.g. a
  `namespace="bucket"` detail present ⇔ the bucket is the thing that is missing), or drop
  the claim from the ADR and from `errors.py`'s `NotFound` docstring.

### R-W1-006 [MINOR] `S3BlobStore.stat()` leaks a raw `ValueError` on a non-numeric `ContentLength`

- **Location:** `src/paa/storage/coldlake/minio_backend.py:834` and `:846-855`.
- **Evidence:** [RAN probe 3] a client returning `{"ContentLength": "not-a-number"}` →
  `ValueError: invalid literal for int() with base 10` escapes `stat()` untranslated.
- **Why it matters:** it is a raw exception leaving the substrate — the invariant the change
  exists to establish — and the CAS's equivalent (a file that vanished between the locate
  and the stat) *is* translated, so the two substrates differ here too. The trigger is a
  broken server, so the severity is low, but the rule is the rule.
- **Recommended fix:** translate the conversion failure as the substrate's malformed answer
  — `BlobFailure` carrying the *type* of the value, never the value.

### R-W1-007 [MINOR] The report and the shared context carry the wrong failure count (26, not 28)

- **Location:** `reports/w1-impl/W1-IMPL-01-T-001.md:393` ("28 failed") and
  `reports/w1-impl/W1-IMPL-01-T-001.md:501-505` ("Twenty-eight of them");
  `briefs/W1-REVIEW-CONTEXT.md:55` ("28 of the new tests fail against the pre-change sources").
- **Evidence:** [RAN] with the pre-change `base.py`/`cas.py`/`minio_backend.py` on
  `PYTHONPATH` and the new `errors.py` present, `pytest tests/storage/test_minio_backend.py
  tests/storage/test_blobstore_contract.py` yields **26** `FAILED` lines (20 + 6); the
  report's own enumerated list [VERIFIED report:378-392] also sums to 26.
- **Why it matters:** the number is the *reversal* evidence — "which test fails if the fix
  is reverted" — and it is quoted in the shared context as a claim to audit. It is off by
  two and, more importantly, it is quoted without the method, so a reader cannot reproduce it.
- **Recommended fix:** correct the number in the report and in the records, and state the
  method (the pre-change three files + the new `errors.py`, on `PYTHONPATH`) so it is
  reproducible.

### R-W1-008 [MINOR] The plan's "nine `TYPE_CHECKING` blocks are load-bearing" is wrong; the correct number is one, and the plan's "48 blocks" is 41

- **Location:** `docs/plans/w6-w7-w9.md` §0 ("**48 `if TYPE_CHECKING:` blocks** in
  `src/paa/`: nine are load-bearing (real runtime cycles, proved two ways), nine are
  load-bearing for another reason (optional extras, deliberate laziness), and about thirty
  are vestigial") and §3.2.
- **Evidence:** [RAN] at `02f3636` there are **41** `if TYPE_CHECKING:` guards in `src/paa`;
  hoisting each guard to a runtime import *one at a time* and importing that module in a
  fresh interpreter breaks the import for **exactly one** module,
  `paa.storage.coldlake.cas` (`cannot import name 'BlobStore' from partially initialized
  module 'paa.storage.coldlake.base'`) — because `base.py` imports `cas` for `BlobRef`, so
  `cas → base` closes the cycle. At HEAD: 34 guards, still exactly one load-bearing. This
  **confirms the implementer's correction** [VERIFIED report
  `W1-IMPL-03-T-004.md` §"FINDING" and the COMMS line of 21:28:19Z].
- **Why it matters:** the plan and the records carry a wrong piece of evidence, and the
  plan is what the next waves are written against — a wave that "must not undo" nine
  load-bearing blocks will avoid converting one, and the "nine load-bearing for another
  reason" figure is likewise not supported (only `paa.api.app`'s `from fastapi import
  FastAPI` is load-bearing for an optional-extra reason, and the lock already tolerates
  that module).
- **Recommended fix:** correct the plan's §0 and §3.2 to "**one** block is load-bearing for a
  real runtime cycle (`paa.storage.coldlake.cas`); 41 blocks at the baseline, 34 after
  T-004; at least one (`paa.api.app`) is load-bearing for an optional extra", and say *why*
  `cas.py` is the one (the `base → cas` import for `BlobRef`), so the next wave knows the
  constraint it must not break.

### R-W1-009 [NIT] `raise translate_blob_error(exc, …) from exc` sets `__cause__` to the exception itself when the translator returns the same object

- **Location:** every `raise translate_blob_error(exc, …) from exc` /
  `raise _translate_s3_error(exc, …) from exc` site in `cas.py` and `minio_backend.py`;
  contrast `_translated_chunks`, which gets it right [VERIFIED `src/paa/storage/coldlake/cas.py:641-644`].
- **Evidence:** [RAN probe 2] `raise e from e` sets `e.__cause__ is e`; CPython's traceback
  printer does not loop, so nothing breaks today. Reachable only when the caught exception
  is a `PaaError` that is not ours — i.e. when the translator's own "pass it through" rule
  fires.
- **Why it matters:** a self-referential `__cause__` is a latent trap, and the codebase
  already contains the correct spelling of the guard — in the same package.
- **Recommended fix:** use the same `if translated is exc: raise` guard the chunks helper
  uses, at the boundary sites.

---

## 5. Attempts to break it

| # | Attack | Outcome |
|---|---|---|
| 1 | A missing **bucket**, answered the way a real S3 server answers a HEAD (404, no body) | **Broke it.** `exists`→`False`, `stat`→`None`, `delete`→`False`, `get`→`NotFound("blob not found")`; the read paths report a blob absence that is really a namespace absence [RAN probe 1] → R-W1-001 |
| 2 | A missing bucket answered the way the *stub* answers it (`NoSuchBucket` code on every call) | The advertised behaviour appears — the test passes. This is the shape the unit suite pins and the shape a real HEAD cannot produce → R-W1-001 |
| 3 | The same missing bucket, but only on the write path (`put_object` returns a body) | `put`/`put_stream` → `NotFound("bucket not found")`; the read paths → `NotFound("blob not found")`/`False`/`None`/`False`. **The two write paths agree with each other and disagree with the four read paths** → R-W1-001 |
| 4 | The CAS archive made unreadable (EACCES on the fan-out) | **Broke it.** Five contract methods leak a raw `PermissionError`; only `put_stream` translates → R-W1-002 |
| 5 | A CAS blob deleted between the locate and the stat (dedup path) | **Broke it.** `put` leaks a raw `FileNotFoundError` → R-W1-002 |
| 6 | A corrupt `.zst` read through `stat()` | **Broke it.** Raw `zstandard.ZstdError`; the same file through `get()` → `Corrupt` → R-W1-003 |
| 7 | A `TypeError` at the boundary | Does not escape — it becomes `BlobFailure`, i.e. it *is* reported as an infrastructure fault, against the docstring → R-W1-004 |
| 8 | A `LedgerError` at the boundary | Survives unchanged ✓ (idempotence and the "not ours" rule both hold) |
| 9 | A driver error the table does not name | `BlobFailure` with `s3_code` in the details ✓ — the hostile server cannot pick the local type |
| 10 | A driver error wrapped by boto3's `S3UploadFailedError` (implicit `__context__`) | Unwrapped and classified ✓ — the `put`/`put_stream` divergence really is closed on the *write* path |
| 11 | A body that dies mid-read | `Unreachable` on both the whole-payload and the streaming path ✓ |
| 12 | A digest of 10 000 characters, a traversal-shaped digest, a lying size hint | Refused at the door on both substrates; the echoed value is bounded to 128 ✓ |
| 13 | A missing blob, both substrates, both read paths | `NotFound` with the **digest** in the details on both ✓ |
| 14 | A non-numeric `ContentLength` from the driver | **Broke it.** Raw `ValueError` out of `stat()` → R-W1-006 |
| 15 | `--compose` given twice, with `PAA_COMPOSE_FILE` also set; the flag's precedence; the env alone; `extends:` / anchor / multi-line `image:` / `include:`; an override-only unpinned image | All behaved as claimed: later wins, the flag beats the env, the env still works, every unsupported shape is refused with the file and the shape named and exit 1, and an override-only unpinned image is refused → T-003 verified |
| 16 | Hoisting every `if TYPE_CHECKING:` guard to a runtime import, one at a time, in a copy of the source | Exactly one breaks (`paa.storage.coldlake.cas`) → R-W1-008 (the implementer's correction is right) |
| 17 | Reverting T-004 and re-running the new convention lock | Exactly 2 of 7 fail → the lock is a real regression test |

---

## 6. Dimension checklist

| Dimension | Verdict | Notes |
|---|---|---|
| Correctness | **FAIL** | The bucket-absence clause is unreachable (R-W1-001) and the CAS leaks raw exceptions from five contract methods and from `stat()` (R-W1-002/003). The vocabulary, the idempotence rule and the structural classifier are otherwise correct. |
| Root cause vs symptom | **PASS** | The change is a seam, not a patch: one vocabulary, one shared translator, one dialect beside the client, one clause in the contract. That is the right shape and it should be kept. The defects are in the *reach* of the seam, not in its idea. |
| Regression / compatibility | **PASS** | Every new member is a `StorageError`, so `except StorageError`/`except PaaError` keep working [VERIFIED `errors.py:71-73`; `artifacts.py:283` keeps working]. No signature changed. No caller in `src/` depends on `exists()`/`stat()`/`delete()`'s old answer [VERIFIED by grep: no production caller of the blob store's `exists`/`stat`/`delete`]. |
| Concurrency / idempotency | **PASS** | The translation reads per-exception attributes only; the stores hold no new state; the translation is idempotent. |
| Error handling | **FAIL** | R-W1-002/003/006 — the invariant the change exists to establish is not established on the filesystem substrate. |
| Security / abuse | **MIXED** | Strong: bounded echoes, no driver's text in the ledger, the server cannot pick the local type, the path-traversal guard is intact. Weak: a hostile server can make `exists()` answer `False` for a bucket it deleted (R-W1-001), and the raw `PermissionError` bypasses the edge handlers that would have recorded the fault in the ledger (R-W1-002). |
| Performance / scalability | **PASS** | The translation is on the failure path; the only per-chunk cost is one `try` per 1 MiB. Measured import-time delta for T-004 is inside the noise. |
| Migration / rollback | **PASS** | Additive; no flag, no data, no schema. Reverting is clean. |
| Observability | **PASS** | The concept plus the identifiers reach the ledger; the driver's wording stays in the traceback. The `op` detail is a genuine improvement over the old prose. |
| Maintainability / conventions | **PASS with notes** | House style honoured: long "why" docstrings, `Final` constants, no `# noqa`, no `# type: ignore`, ruff and mypy 0 on the subtree. Two docstrings now promise more than the code does (R-W1-004, R-W1-005). |
| Docs impact | **FAIL** | ADR-0032 §5 and the protocol's error clause state a behaviour the code cannot produce (R-W1-001); the plan's §0/§3.2 carry a wrong measurement (R-W1-008); the report carries a wrong count (R-W1-007). |

---

## 7. Optimization and simplification opportunities

| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | `src/paa/storage/coldlake/cas.py:188-196` | five methods leak a raw `OSError` through one call site [RAN probe 2] | closes R-W1-002 at the source; one place, not five | S | L | **DO NOW** — make the *lookup* the boundary |
| 2 | `src/paa/storage/coldlake/minio_backend.py:341-419` | six near-identical `if code in TABLE: return X(...)` branches | readability only; the failure path is cold and the explicit branches are the readable form | S | L | **DO LATER** — a table-driven dispatch would be shorter but less legible; not worth the churn now |
| 3 | `src/paa/storage/coldlake/errors.py:285-292` vs `minio_backend.py:361-367` | the same "ours / not ours" guard written twice | 4 duplicated lines | S | L | **DO LATER** — one shared `_is_ours(exc)` helper, when the next change touches either |
| 4 | `src/paa/storage/coldlake/minio_backend.py:841` | `stat()`'s fallback re-streams the whole blob (`sum(... self.get_stream(digest))`) for a legacy object with no `raw-size` metadata | O(size) per `stat()` — a latent stall on a large legacy blob, called from `iter_blobs`-style sweeps | M | L | **DO LATER** — cap it, or record the size once at first read |
| 5 | `src/paa/storage/coldlake/base.py:150-185` | the error clause is prose; the test only checks that each docstring *names* a member | the clause cannot be checked mechanically, and the one clause that is wrong (the namespace) is exactly the one a mechanical check would have caught | M | M | **DO LATER** — a machine-readable (method, condition, class) table is the right long-term shape; the implementer's own follow-up 4 says so. It would have caught R-W1-001 |
| 6 | `src/paa/storage/coldlake/errors.py:121-133` | splitting `NotFound` into `NotFound` + `NamespaceMissing` | two types to catch where the judgement is one | M | M | **NOT WORTH IT** — the plan's judgement is right; the missing-bucket problem is a *detection* problem (R-W1-001), not a *naming* problem |
| 7 | `src/paa/storage/coldlake/minio_backend.py:129-187` | growing the code tables to cover more server codes | — | S | M | **NOT WORTH IT** — an unnamed code already becomes `BlobFailure`, which is honest; wave 2's live suite should *measure* the tables before anyone grows them by hand |

---

## 8. Cross-agent / cross-task interactions observed

* **T-004 ↔ T-001 (ownership).** The one genuinely load-bearing `TYPE_CHECKING` block in the
  tree is `src/paa/storage/coldlake/cas.py:62` — a file **T-001 owns** (the whole
  `coldlake/` package). T-004 correctly declined to touch it and reported it
  (COMMS 21:28:19Z). The block must stay: `base.py:67` imports `cas` for `BlobRef`, so
  `cas → base` closes the cycle [VERIFIED by the hoist experiment]. It is a *permanent*
  deferral, not a handover to wave 2 — it should be recorded as such, or the cycle broken
  (e.g. `base.py` could stop importing `cas` at runtime by moving `BlobRef` into its own
  module), which would let the last `TYPE_CHECKING` block in the tree go.
* **T-001 ↔ T-003 (the "one contract, one way" theme).** Both changes are the same shape:
  a *dialect* (the S3 error codes; the Compose file shapes) read beside the client, with
  everything it does not model refused rather than guessed. T-003's discipline —
  "a script that silently mis-merges is worse than one that says 'I do not understand this
  file'" — is exactly the discipline R-W1-001 says T-001 should have applied to the HEAD
  404: the dialect should have said "I cannot tell whether the object or the bucket is
  missing" instead of guessing "the object".
* **The plan's §0 evidence is shared by two tasks.** The "nine load-bearing blocks" figure
  is used by T-004 to justify *not* converting; the "28 failing tests" figure is used by
  T-001 as its reversal evidence. Both are wrong (one, and 26). The plan is the artefact
  the next waves are written against, so its correction matters beyond this wave.
* **Wave 2's live S3 suite (T-002) is the natural home for R-W1-001's fix and its
  verification.** ADR-0032 already names it: *"The live S3 suite … is the only way to know
  that the error codes they answer with are the codes in the table."* The bucket case must
  be asserted there, through `exists()`/`stat()`/`delete()`, against both real servers.

---

## 9. What I could not verify

* **botocore's internal `Error.Code` for a HEAD 404.** `boto3` is not installed in this
  venv and the environment forbids installing it for the review. [ASSUMED] it is the
  status string (`"404"`). Not load-bearing for R-W1-001: the conclusion follows from the
  code's own comment (`minio_backend.py:127-128`), the stub's comment
  (`test_minio_backend.py:42-45`) and HTTP's rule that a HEAD response carries no body.
  A wave-2 live assertion would close it.
* **The real error codes SeaweedFS and RustFS answer with.** Wave 2's business; not
  exercised here (no server was started, no container touched).
* **The full test suite.** Not run — scoped runs only, per the brief. I ran the three
  T-001 test files (100 passed), the convention lock (7 passed) and the T-003 script
  against fixtures in `/tmp`.
* **The T-003 claims 5 and 6** (the cross-check against real Compose v5.6.0, and the "18
  failing tests") were not re-measured — they are outside the depth this review allocated
  to T-003.
* **T-004 claim 2's second half** ("Pydantic model fields are byte-identical") was not
  re-measured; the first half (`get_type_hints`) was, and holds.
* **Whether any *deployment* outside this tree depends on `exists()` answering `False` for
  a missing bucket.** No caller in `src/paa` does [VERIFIED by grep], but an external
  consumer of the `BlobStore` protocol cannot be ruled out from here — which is another
  reason R-W1-001's fix should be a *correction of the record* as well as a code change:
  the behaviour change that was never happening cannot be relied upon by anyone.
* **Windows behaviour.** The review ran on Linux; the `pathlib` errno behaviour that
  R-W1-002 rests on is POSIX-specific in its *trigger* (an unreadable directory), but the
  `PermissionError`/`OSError` classification in `errors.py` is errno-driven and portable.
