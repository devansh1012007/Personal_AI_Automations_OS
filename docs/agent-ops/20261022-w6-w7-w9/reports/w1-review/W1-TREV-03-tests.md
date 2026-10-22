<!-- ORCH-DIGEST:START -->
agent: W1-TREV-03 | scope: wave 1 — the three test suites (T-001 blob vocabulary, T-003 merged compose, T-004 import lock) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 105 (50 test_blob_errors.py + 22 test_minio_backend.py + 6 test_blobstore_contract.py + 20 test_fetch_images.py + 7 test_import_conventions.py)
fail-before/pass-after confirmed: 46 (T-001 26 failures + 1 collection error / T-003 18 of 20 / T-004 2 of 7 locks) | reasoned: 59 | not-possible: 0
findings: BLOCKER 0 | MAJOR 5 | MINOR 9 | NIT 2
flaky_suspects: none — 3 repeat runs of all five files green and timing-stable; a run with hostile PAA_COMPOSE_FILE/PAA_IMAGE_MIRROR/PAA_FETCH_*/STUB_* set is still 55/55 green
missing_edge_cases: P1 6 | P2 7 | P3 6
mutation_survivors: 15 of 36 (14 of 27 in the blob suites, 1 of 9 in the deployment suite) + 5 of the 7 T-004 locks are descriptions that survive a revert by design
changed_behavior_without_tests: (1) a missing S3 *bucket* now raises NotFound from exists()/stat()/delete() — pinned only indirectly, by one parametrised case of the enumeration test; (2) NotSupported is never raised by any test; (3) the streaming path's hash-verification failure (Corrupt) is never produced by a test
claims_audit: 13 claims checked — 11 verified, 1 unverifiable-as-stated ("28 of the new tests fail" is 26 + a collection error), 1 miscount ("137/137 modules" is 136 module names)
confidence: high (every finding below was reproduced by a command in this session, in a scratch tree under /tmp; the only premise I could not execute is botocore's real error codes, which is a code-review matter and does not affect any test conclusion)
<!-- ORCH-DIGEST:END -->

# Test quality review — W1-TREV-03

**Lens:** would these tests catch the defect they claim to prevent, and do they fail when
the fix is reverted?

**What I did, in order.** (1) Blind pass: I read the changed sources (`errors.py`, `base.py`,
`cas.py`, `minio_backend.py`, `deploy/fetch-images.sh`) and the five test files, and formed
my own findings *before* opening any implementer report. (2) Claims audit: I read the three
implementer reports and checked their numbers. (3) Reversal: I rebuilt the pre-change trees
under `/tmp/opencode/tw/` (never in the shared tree — no stash, no checkout, no worktree
registered with git) and re-measured every reversal claim myself. (4) Mutation analysis: I
applied **36 plausible mutations** to the changed code and the script and recorded which
tests notice. (5) Flakiness: three repeat runs of each file plus a hostile-environment run.

**Scratch trees used (all under /tmp, all removed or disposable):** `orig-src` (the three
pre-change coldlake sources grafted onto the current `src`), `full-old-src` (the whole
pre-change `src/paa` plus the new `errors.py`), `semi` (pre-change `cas.py` +
pre-change `minio_backend.py` **with the new S3 dialect and `_translate_s3_error` grafted
in**, so the new test file imports and only the *boundaries* are old), `dep-old` (the whole
pre-change tree with the new deployment test file), `imp-old-src` (the seven pre-change
import-converted modules), plus per-mutation copies of `src`.

**A note on the implementer reports:** W1-REV-01 (code) has already recorded the "26, not 28"
count. I confirm it independently below and add the *why* the number is what it is.

---

## 0. Method

| Step | Command / artefact | Result |
|---|---|---|
| Baseline (all five files) | `.venv/bin/python -m pytest <file> -q` per file | 50 + 36 + 14 + 55 + 7 collected, all green |
| T-001 reversal | `PYTHONPATH=/tmp/opencode/tw/orig-src pytest tests/storage/test_minio_backend.py tests/storage/test_blobstore_contract.py` | **26 failed, 24 passed** [RAN] |
| T-001 reversal (2nd variant) | `PYTHONPATH=/tmp/opencode/tw/full-old-src/src …` (whole pre-change `src/paa` + new `errors.py`) | **26 failed, 24 passed** — same number [RAN] |
| T-001 collection | `PYTHONPATH=…/orig-src pytest tests/storage/test_blob_errors.py` | **1 collection error** (`ImportError: cannot import name '_translate_s3_error'`) [RAN] |
| T-001 "vocabulary only" | `PYTHONPATH=…/semi pytest tests/storage/test_blob_errors.py` (old boundaries, new vocabulary + translator) | **5 failed, 45 passed** [RAN] |
| T-003 reversal | `git archive 02f3636` tree + the new test file → `pytest tests/deployment/test_fetch_images.py` | **18 failed** [RAN] |
| T-004 reversal | `PYTHONPATH=…/imp-old-src pytest tests/conventions/test_import_conventions.py` | **2 of 7 failed** [RAN] |
| Mutation sweep | 27 mutations in the blob sources, 9 in `fetch-images.sh` | 14 + 1 survivors [RAN] |
| Transport instrumentation | a copy of `StubS3Client` that records every client method the store calls | `test_no_driver_exception_escapes_the_store`: **49 `head_object` calls, 0 `put_object` / `upload_fileobj` / `get_object` / `delete_object`** [RAN] |
| Fresh-interpreter sweep | the lock's own probe machinery, 136 module names | 135 import, 1 tolerated extra failure (`kuzu_backend`), 7.5 s wall clock [RAN] |
| Flakiness | 3 × (5 files) + 1 hostile-env run | green every time; timings stable to ±0.1 s [RAN] |

---

## 1. Per test/suite assessment

### 1.1 `tests/storage/test_blob_errors.py` — 50 new tests

| Test | Asserts behaviour? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `TestTheVocabulary` (7) | shape only | n/a (new classes) | yes | n/a | Tautological w.r.t. the old code: 7/7 pass against the pre-change boundaries [RAN]. They *do* pin the hierarchy (`BlobFailure`/`NotSupported` are not `Unreachable`) — M-tests show `issubclass` assertions are the only thing that would catch a re-parenting. |
| `TestTheSharedTranslation` (10) | yes — the translator is new code | no (the function is new) | yes | n/a | A real unit test of `translate_blob_error`. Idempotence is asserted for **one** member (`NotFound`) only. |
| `TestTheS3Translation` (26) | yes — the S3 dialect is new code | no | yes | no — the stub errors are duck-typed exactly like botocore's | The **classification by code is not pinned**: every code can be deleted from every table and the suite stays green (R-W1-204). |
| `TestTheFilesystemBoundaries` (5) | yes — boundary behaviour | **yes** | yes | no | The only 5 of the 50 that pin a *boundary*; they are the file's real regression value. |
| `test_the_driver_is_never_imported_at_module_scope` | yes | no (the old code also did not import boto3 at module scope) | yes | n/a | Subprocess, no timeout — see R-W1-216. |
| `test_the_vocabulary_module_imports_without_the_driver` | yes | no | yes | n/a | Same, no timeout. |
| `test_a_paa_error_is_never_a_blob_error` | yes | no | yes | n/a | — |

### 1.2 `tests/storage/test_minio_backend.py` — 22 new tests (36 total)

| Test | Asserts behaviour? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_a_missing_bucket_is_the_same_error_from_put_and_put_stream` | yes | yes | yes | **the thing under test is short-circuited** | The failure is raised at the **HEAD**; `put_object`/`upload_fileobj` are never called [RAN]. It therefore does *not* exercise the put/put_stream divergence it names (R-W1-202). |
| `test_a_missing_bucket_is_not_found_and_not_a_refusal` | yes | yes | yes | same | Asserts `NotFound` + `s3_code` — it cannot distinguish "bucket missing" from "object missing" (both are `NotFound`). |
| `test_a_refused_request_is_access_denied` | yes | yes | yes | same | Asserts `substrate == "s3"` — a detail only the new code carries. |
| `test_a_refused_bucket_check_is_access_denied` | yes | yes | yes | no | The only test of `ensure_bucket=True`. |
| `test_an_unreachable_endpoint_is_unreachable` | yes | yes | yes | no | Class-name classification. |
| `test_a_missing_object_reports_the_digest_not_the_key` | yes | yes | yes | no | Pins the `sha256=key` mislabelling fix (M11 catches a revert of it). |
| `test_a_missing_object_in_the_streaming_path_reports_the_digest` | yes | yes | yes | no | — |
| `test_a_mid_body_read_failure_is_translated` (+streaming) | yes | yes | yes | no | Both read paths pinned. |
| `test_a_corrupt_frame_is_corrupt_on_both_paths` | yes | yes | yes | no | Frame corruption only — **not** hash mismatch on the streaming path (R-W1-207). |
| `test_the_upload_wrapper_is_unwrapped` | **no** — it does not reach the wrapper | "yes" for the wrong reason | yes | **the unit under test is mocked away** | [RAN] 3 `head_object` calls, 0 `upload_fileobj`. R-W1-202. |
| `test_no_driver_exception_escapes_the_store` (7 × 7) | yes, by enumeration of *methods* | yes | yes | partly | [RAN] 49 `head_object` calls only. The enumeration is over 7 methods but only **one transport path** is exercised (R-W1-201). |
| `test_a_missing_object_is_an_answer_not_a_failure` | yes | yes | yes | no | Pins the object/bucket distinction only in one direction. |
| `test_the_drivers_error_is_kept_as_the_cause` | yes | yes | yes | no | Good — pins `__cause__` and the message/ledger separation. |

### 1.3 `tests/storage/test_blobstore_contract.py` — 6 new tests (14 total)

| Test | Asserts behaviour? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_every_contract_method_states_what_it_raises` | a *source* shape (the docstrings) | yes | yes | n/a | A docstring lock, not a behaviour lock: it proves the contract *says* it, not that the code *does* it. The house has precedent for source-shape tests; this one is sound. |
| `test_a_missing_blob_is_not_found_on_both_substrates` | yes | yes | yes | no | The best test in the wave: the same condition, the same class, the same detail, on both substrates. |
| `…_in_the_streaming_path_on_both` | yes | yes | yes | no | — |
| `test_a_hostile_digest_is_a_malformed_request_on_both` | yes | yes | yes | no | Traversal-shaped input. |
| `test_a_lying_size_hint_is_a_malformed_request_on_both` | yes | yes | yes | no | Compressed path only (R-W1-206). |
| `test_a_corrupt_frame_is_corrupt_on_both` | yes | yes | yes | no | Frame corruption only. |

### 1.4 `tests/deployment/test_fetch_images.py` — 20 new tests (55 total)

| Test | Asserts behaviour? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_the_reader_refuses_the_shapes_it_does_not_understand` (7) | yes | **yes, all 7** | yes | no | Exit code asserted as `!= 0` only; the message word is pinned per shape. |
| `test_the_merged_stack_is_the_stack_the_script_reads` | yes | yes | yes | no | The derivation is **not circular**: the oracle is a second reader written in Python (`_merged_rows`), not the script's own output. |
| `test_an_image_added_to_the_gvisor_override_changes_the_plan` | yes | yes | yes | no | **This is the test that breaks the "the two files happen to agree" accident on purpose** — a hand-written expectation (`("build", "paa/runtime:4.2")`) against the derived plan. The answer to the brief's question is yes: a disagreement in the merged case fails. |
| `test_a_service_only_the_override_declares_is_in_the_plan` | yes | yes | yes | no | — |
| `test_the_merged_plan_is_what_the_fetch_pulls` | yes | yes | yes | no | Reaches the *fetch*, not only the report; asserts the base image was **not** pulled. |
| `test_a_profile_gated_service_is_not_fetched` | yes | yes | yes | no | Also asserts the stderr notice. |
| `test_the_compose_flag_wins_over_the_environment` | yes | yes | yes | no | — |
| `test_a_compose_file_that_does_not_exist_is_refused` | yes | yes | yes | no | Exit code `!= 0` only. |
| `test_trailing_whitespace_does_not_stay_on_the_reference` | yes | yes | yes | no | Asserts on the *pulled* reference, not on the report — the right level. |
| `test_a_service_added_to_the_compose_file_lands_in_the_plan` | yes | **no** (passes against the old script) | yes | no | A guard, not a regression test. |
| `test_the_two_readers_agree_on_every_shape[trailing whitespace after the image]` | yes | **no** (passes against the old script) | yes | no | A guard, not a regression test. |
| `test_every_pulled_image_is_digest_pinned` | yes | yes | yes | no | Asserts over the merged stack; asserts the set is non-empty first. |
| `test_the_image_and_build_exemption_is_deliberate` | yes | yes | yes | no | Asserts in both directions. |

### 1.5 `tests/conventions/test_import_conventions.py` — 7 new tests

| Test | Asserts behaviour? | Fails on revert? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_the_contract_is_not_imported_behind_type_checking` | yes (AST source lock) | **YES** | yes | n/a | A real regression test. |
| `test_every_module_that_imports_the_contract_binds_it_at_runtime` | weakly | **NO** — vacuous on revert | yes | n/a | It *skips* every module that imports the contract behind `TYPE_CHECKING`, so on the reverted tree it has nothing to check (R-W1-210). |
| `test_the_family_resolves_the_contract_one_way` | yes | **YES** | yes | n/a | A real regression test. |
| `test_every_deferral_names_the_task_that_owns_the_file` | a description | NO (by design) | yes | n/a | Does not assert the deferral is still *outstanding* (R-W1-211). |
| `test_every_module_under_src_paa_imports_in_a_fresh_interpreter` | yes | NO (by design) | yes, environment-dependent | n/a | The cycle detector; soundness of the probe is itself tested. |
| `test_every_module_imports_together_in_one_interpreter` | yes | NO (by design) | yes | n/a | 136 imports in one interpreter, reverse-alphabetical. |
| `test_the_probe_would_notice_a_cycle` | yes | NO (by design) | yes | n/a | **Excellent**: it proves the lock can fail, with a real cycle written into `tmp_path`. This is the test that makes the other six descriptions worth having. |

**The seven-lock question, answered:** **2 of 7 are real regression tests** (`…not_imported_behind_type_checking`,
`…the_family_resolves_the_contract_one_way`) — [RAN] reverting the seven conversions fails
exactly those two, and no others. **5 of 7 are descriptions**: they describe the convention, the
deferral policy and the probe, and they pass on the reverted tree by construction. The claim
"reverting the change fails two of them" is **verified exactly**.

**The fresh-interpreter check — bounded?** Yes, but loosely. Each probe has
`timeout=IMPORT_TIMEOUT_SECONDS` (120 s) and `subprocess.run` kills the child on timeout
[VERIFIED `tests/conventions/test_import_conventions.py:250`]; the aggregate has
`timeout=300` [VERIFIED `…:459`]. The pathological worst case is
136 modules ÷ 8 workers × 120 s ≈ **34 minutes**, because the per-module cap is 120 s — an
import that takes 121 s is a hang, not a slow import. **Deterministic?** The *result* is
deterministic given the environment; the environment is an input, and the suite hard-codes
four optional-extra modules [VERIFIED `…:95-102`]. On this host exactly one module fails
(`paa.storage.graph.kuzu_backend`, the `graph` extra) and it is tolerated [RAN]. A new
optional-extra module added without updating that list turns the lock red on a host that
lacks the extra — a coupling worth a comment, not a defect.

---

## 2. Coverage of changed lines/branches, error paths, boundaries

**What the changed code added, and what the tests reach.** The blob change touched six
escaping sites in the S3 path and five in the filesystem path. Measured by mutation, the
tests reach:

| Boundary | Reached by a failing test? | Evidence |
|---|---|---|
| S3 `head_object` | **yes** | M22 (raw error escapes) → 13 failures |
| S3 `get_object` | **yes** | M20 → 1 failure |
| S3 body read (`_read_all`) | **yes** | M21 → 1 failure |
| S3 `put_object` | **NO** | M17 → 0 failures |
| S3 `upload_fileobj` | **NO** | M19 → 0 failures |
| S3 `delete_object` | **NO** | M18 → 0 failures |
| S3 constructor (client build) | **NO** | M23 → 0 failures |
| CAS constructor (`mkdir`) | **NO** | M25 → 0 failures |
| CAS `put` / `_commit` | **NO** | M24 → 0 failures |
| CAS `get_stream` read | **yes** | M27 → 2 failures |
| CAS `stat` / `delete` | **NO** | M26/M27 → 0 failures |

**Untested *branches* in the changed code, beyond the boundaries above:**
the S3 **code tables** (every entry is unpinned — R-W1-204); the **credential class names**
(`NoCredentialsError`, `PartialCredentialsError` — no test names them at all); the
**`NotSupported` production paths** (R-W1-208); the **uncompressed + lying size hint**
combination (R-W1-206); the **streaming hash-mismatch** path (R-W1-207).

**The changed behaviour the implementer flagged** — *a missing S3 bucket now raises `NotFound`
from `exists()`/`stat()`/`delete()`, where it previously answered `False`/`None`/`False`* —
**has no test of its own.** It is caught only indirectly, by the `NoSuchBucket` case of
`test_no_driver_exception_escapes_the_store` (M1 → exactly 1 failure) [RAN]. And the stub
**cannot express a missing bucket**: `StubS3Client.head_object` raises `_StubNotFound`, whose
code is `"404"` [VERIFIED `tests/storage/test_minio_backend.py:49`] — the one code the
translation classifies as *object-missing*. So no test in the suite can tell "the object is
absent" from "the bucket is absent" through the stub's own not-found path; it takes
`client.failure = ClientError("NoSuchBucket", …)` to reach the bucket case at all. Under the
brief's rule, **changed behaviour without a test is a MAJOR** — R-W1-205.

---

## 3. Mutation thinking

**27 mutations of the blob sources, 9 of the script.** Method: copy `src` (or the script)
into a scratch tree, apply one textual mutation, run the relevant test file(s) with
`PYTHONPATH` pointed at the scratch tree, record the failure count. A mutation that produces
**0 failures** is a survivor — a plausible bug the suite would not notice.

| # | Plausible bug | Caught? | By which test |
|---|---|---|---|
| M1 | a missing *bucket* is answered "not there" instead of raising | yes (1) | `test_no_driver_exception_escapes_the_store[failure0]` |
| M2 | `"SlowDown"` dropped from the retryable code table | **NO** | — |
| M3 | `"NoSuchBucket"` dropped from the bucket code table | **NO** | — |
| M4 | the credential class names dropped | **NO** | — |
| M5 | the HTTP-status 404 fallback dropped | yes | `test_a_status_is_the_fallback_when_there_is_no_code` |
| M6 | the streaming hash mismatch reported as `BlobFailure` | **NO** | — |
| M7 | the explicit declared-size check dropped | **NO** | — |
| M8 | the `StorageError` idempotence guard dropped | **NO** | — (the branch is redundant; see R-W1-215) |
| M9 | a codec failure no longer `Corrupt` | yes (4) | `test_a_codec_failure_is_corruption` + the frame tests |
| M10 | a full disk classified `Unreachable` (a retry promise) | yes | `test_a_full_disk_is_not_reachable_and_not_retryable` |
| M11 | the `sha256=<key>` mislabelling returns | yes | `test_a_missing_object_reports_the_digest_not_the_key` |
| M12 | a foreign `PaaError` translated into `BlobFailure` | yes | `test_a_domain_error_is_not_translated` |
| M13 | a missing blob reported as `BlobFailure` | yes (3) | the contract's `TestTheErrorClause` |
| M14 | the HEAD swallows every error | yes (7) | the enumeration test |
| M15 | the wrapper is not unwrapped | yes (2) | `test_a_wrapper_is_unwrapped*` |
| M16 | the echoed digest is no longer bounded to 128 | yes | `test_a_hostile_digest_is_a_malformed_request` |
| M17 | `put_object`'s error is not translated | **NO** | — |
| M18 | `delete_object`'s error is not translated | **NO** | — |
| M19 | `upload_fileobj`'s error is not translated | **NO** | — |
| M20 | `get_object`'s error is not translated | yes (1) | `test_a_missing_object_reports_the_digest_not_the_key` |
| M21 | the mid-body read error is not translated | yes (1) | `test_a_mid_body_read_failure_is_translated` |
| M22 | the HEAD's error is not translated | yes (13) | many |
| M23 | the S3 constructor's client-build error is not translated | **NO** | — |
| M24 | the CAS write path's `OSError` is not translated | **NO** | — |
| M25 | the CAS constructor's `OSError` is not translated | **NO** | — |
| M26 | the CAS `stat`'s `OSError` is not translated | **NO** | — |
| M27 | the CAS `delete`'s `OSError` is not translated | **NO** | — |
| D1 | only the **last** `--compose` file is read (no merge) | yes (4) | `test_the_merged_stack_is_the_stack_the_script_reads` + 3 |
| D2 | the merge precedence reversed (the first file wins) | yes (2) | `test_an_image_added_to_the_gvisor_override_changes_the_plan`, `test_the_merged_plan_is_what_the_fetch_pulls` |
| D3 | a later file's silence about `build:` clears the earlier one | yes (4) | 4 tests |
| D4 | `profiles:` ignored | yes (2) | `test_a_profile_gated_service_is_not_fetched` + the parity corpus |
| D5 | the refusal no longer stops the script (`build_plan \|\| true`) | **NO** | — (see below) |
| D6 | the environment variable beats the `--compose` flag | yes (8) | 8 tests |
| D7 | the empty plan is allowed | yes | `test_an_empty_plan_is_refused` |
| D8 | the `include:` refusal removed | yes | the refused-shapes corpus |
| D9 | the `extends:` refusal removed | yes | the refused-shapes corpus |

**Mutation survivors: 14 of 27 (blob) + 1 of 9 (script) = 15 of 36.**
D5 is behaviourally equivalent in exit status (the empty-plan gate still exits non-zero), and
that is exactly the point it makes: **the refusal's non-zero exit is not tied to the refusal** —
the test asserts `!= 0` and a message word, and the message is printed by the refusal path
whether or not the refusal is what stopped the script (R-W1-213).

---

## 4. Flakiness evidence and test-suite health

* **Three repeat runs of all five files**: green every time; wall clock 1.04–1.05 s
  (`test_blob_errors.py`), 0.65–0.69 s, 0.70–0.79 s, 1.34–1.37 s, and **9.16–9.28 s**
  (`test_import_conventions.py`) [RAN]. No order dependence observed; the deployment file uses
  only `tmp_path`; the conventions file's only module-scoped fixture is read-only.
* **Hostile environment**: with `PAA_COMPOSE_FILE=/nonexistent/compose.yml`,
  `PAA_IMAGE_MIRROR=evil.example.com`, `PAA_FETCH_MAX_ATTEMPTS=1`, `PAA_FETCH_BASE_DELAY=0`,
  `PAA_FETCH_TIMEOUT=1`, `STUB_MODE=wrong`, `STUB_LOG=/tmp/x`, `STUB_STORE=/tmp/y`,
  `STUB_ID=deadbeef` exported — the deployment file is still **55/55 green** [RAN]. The
  scrubbing in `_run` [VERIFIED `tests/deployment/test_fetch_images.py:504-509`] drops every
  variable the script reads (`PAA_FETCH_*`, `PAA_COMPOSE_FILE`, `PAA_IMAGE_MIRROR`, `STUB_*`).
  **The new tests do scrub.**
* **The conventions suite is the slow one** — 9.2 s wall, 57 s CPU (136 subprocesses, 8
  workers) [RAN]. Acceptable for a suite that otherwise runs in ~40 s, but it is the single
  most expensive thing in the wave and it runs on **every** suite run, not on a marker.
* **The two subprocess tests in `test_blob_errors.py` have no timeout**
  [VERIFIED `tests/storage/test_blob_errors.py:521-527` and `538-544`]: a hung child hangs the
  suite. Low probability (the script is three imports), but the rest of the wave is careful
  about exactly this.
* **No clocks, no sleeps, no network, no container, no docker daemon** in any of the five
  files: the deployment suite drives a stub `docker` on a private `PATH` and never touches a
  daemon. Confirmed by reading the stub [VERIFIED `tests/storage/test_minio_backend.py:111-182`].

---

## 5. Findings

### R-W1-201 — MAJOR — the "no driver exception escapes" invariant is tested against one transport path
**Location:** `tests/storage/test_minio_backend.py:503-538` (the enumeration test) and
`tests/storage/test_minio_backend.py:111-183` (the stub).
**Evidence:** the stub's only failure knob is `StubS3Client.failure`, and `head_object`
consults it first [VERIFIED `tests/storage/test_minio_backend.py:164-169`]. Every store method reaches the substrate through
`_locate` → `_head` before it writes, reads or deletes, so the HEAD always fails first.
Instrumented run of the enumeration test: **49 `head_object` calls, 0 `put_object`,
0 `upload_fileobj`, 0 `get_object`, 0 `delete_object`, 0 body reads** [RAN].
Mutating `_put_object`, `_upload_fileobj` and `_delete_object` to `raise exc` (a raw botocore
exception reaching the caller — the exact defect ADR-0032 exists to prevent) leaves the whole
blob suite **green** (M17/M18/M19 → 0 failures) [RAN].
**Why it matters:** the test's own docstring says "every method has to raise — and what it
raises is a member of the blob vocabulary". It enumerates 7 *methods* but exercises 1
*transport path*. The claim it makes is stronger than the evidence.
**Recommended fix:** give the stub per-method failure knobs (`put_failure`,
`upload_failure`, `delete_failure`) and add a test that drives each of the four write/delete
transport calls with a failing driver error, asserting the raised class. The stub is a test
artefact; extending it is the cheapest fix in this review.

### R-W1-202 — MAJOR — `test_the_upload_wrapper_is_unwrapped` does not exercise the upload wrapper
**Location:** `tests/storage/test_minio_backend.py:487-500`.
**Evidence:** instrumented run of that test plus
`test_a_missing_bucket_is_the_same_error_from_put_and_put_stream`: **3 `head_object` calls,
0 `upload_fileobj`** [RAN]. The failure is raised by `head_object`, translated there, and the
test's assertion (`details["s3_code"] == "AccessDenied"`) is satisfied by the HEAD path.
The wrapper-unwrapping *logic* is covered — but only by the two unit tests in
`test_blob_errors.py` that call `_translate_s3_error` directly with a hand-built wrapper
[VERIFIED `tests/storage/test_blob_errors.py:366-397`].
**Why it matters:** the task exists to close the "`put` writes through `put_object`,
`put_stream` writes through `upload_fileobj`, and the transfer manager wraps the second one's
error" divergence [VERIFIED `src/paa/storage/coldlake/minio_backend.py:44-49`]. The test
named for that divergence does not reach either transport call. A reader of the suite would
believe the divergence is closed by test.
**Recommended fix:** as R-W1-201 — a knob that fails only `upload_fileobj`, and an assertion
that `put_stream` and `put` raise the *same* class for the same failure when the failure is
raised at the write rather than at the HEAD.

### R-W1-203 — MAJOR — 45 of the 50 new blob tests would pass against the pre-change boundaries
**Location:** `tests/storage/test_blob_errors.py` (whole file).
**Evidence:** with the **pre-change** `cas.py` and the **pre-change** `minio_backend.py`,
and the new `errors.py` + the new `_translate_s3_error` grafted in so the file still imports
(the only change being that the *boundaries* are old): **5 failed, 45 passed** [RAN]. The five
that fail are exactly `TestTheFilesystemBoundaries`; the 45 that pass are the vocabulary
shape tests (7), the shared translator tests (10), the S3 dialect tests (26) and the two
"the driver is never imported" tests.
**Why it matters:** the implementer's reversal measurement restores the three source files
wholesale, so `test_blob_errors.py` **cannot be collected at all**
(`ImportError: cannot import name '_translate_s3_error'`) [RAN]. That single collection error
is what makes the file's 50 tests "fail without the fix"; it is a *structural* signal (the
module did not exist), not 50 behavioural ones. Forty-five of them are legitimate unit tests
of new code — the vocabulary and the translator — but they cannot fail for any boundary
regression, which is the failure mode the task was opened for.
**Recommended fix:** none is required for correctness, but the report and the gate's evidence
should say what the 50 tests actually pin. If the file is meant to carry the boundary
guarantee, it needs boundary-shaped tests (R-W1-201/202/205).

### R-W1-204 — MAJOR — the S3 error-code tables are not pinned by any test
**Location:** `src/paa/storage/coldlake/minio_backend.py:129-234` (the five tables);
`tests/storage/test_blob_errors.py:287-339` (the parametrized cases).
**Evidence:** every parametrized code case is constructed **with an HTTP status that maps to
the same class** (`NoSuchKey`/`404` with 404, `AccessDenied` with 403, `SlowDown` with 503)
[VERIFIED `…:291-329`]. The status fallback therefore produces the expected class on its own.
Deleting `"SlowDown"` from `_S3_RETRYABLE_CODES` → **0 failures** (M2); deleting
`"NoSuchBucket"` from `_S3_BUCKET_MISSING_CODES` → **0 failures** (M3); deleting the two
credential class names → **0 failures** (M4) [RAN]. Four of the nine retryable codes
(`"500InternalError"`, `"502"`, `"503"`, `"RequestTimeoutException"`), two of the eight
refused codes (`"401"`, `"403"`), the whole bucket table and both credential class names are
**never distinguished from the status fallback** by any test.
**Why it matters:** the code is the *primary* signal in the dialect's own design ("the code
is the portable part of the driver's vocabulary"), and the status is the coarse fallback. A
table that drifts is a silent misclassification of exactly the codes that do **not** come with
a matching status in production (AWS answers `RequestTimeoutException` with 400, for instance).
**Recommended fix:** for each table, a case built **without** an HTTP status (or with a status
that contradicts the code), so the code path alone decides. One parametrized case per table
member, asserting the class — a table-driven test over `_S3_*` constants.

### R-W1-205 — MAJOR — the flagged behaviour change (a missing bucket raises) has no test of its own
**Location:** `src/paa/storage/coldlake/base.py:150-185` (the new error clause);
`tests/storage/test_minio_backend.py:39-50` (`_StubNotFound`, code `"404"`).
**Evidence:** mutating `_S3_OBJECT_MISSING_CODES` to include `"NoSuchBucket"` — i.e. undoing
the behaviour change, so that a missing bucket answers `False`/`None`/`False` again — is
caught by exactly **one** test, `test_no_driver_exception_escapes_the_store[failure0]`
(M1 → 1 failure) [RAN]. There is no test that says "a missing bucket raises `NotFound` from
`exists()`, `stat()` and `delete()`", and the stub's own not-found error carries the code
`"404"` [VERIFIED `tests/storage/test_minio_backend.py:49`] — the *object*-missing code — so
the stub cannot express the bucket case except through `client.failure`.
**Why it matters:** the brief's rule is explicit ("the changed behaviour without a test — an
automatic MAJOR"). This is the one behaviour change T-001 made to the substrate's contract,
and it is a *behavioural break* for any caller that relied on `exists()` answering `False`
for a missing bucket. It deserves a named test on each of the three methods, with a stub that
can say `NoSuchBucket`.
**Recommended fix:** a stub knob `bucket_missing` (head raises `ClientError("NoSuchBucket",
status=404)`) and a test asserting `NotFound` from `exists`, `stat` and `delete`, plus the
details carrying `bucket=`.

### R-W1-206 — MINOR — the declared-size check is load-bearing only where no test looks
**Location:** `src/paa/storage/coldlake/cas.py:297-303` (the explicit check);
`src/paa/storage/coldlake/minio_backend.py:617-623`; `tests/storage/test_blob_errors.py:492-495`.
**Evidence:** the existing test uses the **compressed** path
(`put_stream(io.BytesIO(b"twelve bytes"), size=999)`), where zstd's own frame check raises
first — removing the explicit check changes nothing there (M7 → 0 failures) [RAN]. With the
check removed, `put_stream(io.BytesIO(b"twelve bytes"), compress=False, size=3)` writes the
blob and returns a reference with **no error at all** [RAN]; with the check present it raises
`MalformedRequest` [RAN]. So the check is load-bearing exactly on the uncompressed path, and
no test exercises the uncompressed path with a lying hint on either substrate.
**Recommended fix:** parametrise the lying-size test over `compress=(True, False)` and over
both an over-declared and an under-declared size, on both substrates.

### R-W1-207 — MINOR — the streaming path's hash-verification failure is never produced
**Location:** `src/paa/storage/coldlake/cas.py:419-427`; `src/paa/storage/coldlake/minio_backend.py:746-754`.
**Evidence:** changing the `Corrupt` raised after the final chunk of `get_stream` to
`BlobFailure` → **0 failures** (M6) [RAN]. Every "corrupt" test in the suite corrupts the
*frame* (so zstd raises), never the *hash* of a valid frame; `test_corruption_is_detected`
tampers with the bytes but calls `get`, the whole-payload path.
**Why it matters:** the contract makes a point of it — "`Corrupt` can only be raised *after*
the final chunk" [`src/paa/storage/coldlake/base.py:137-148`] — and that is precisely the
case nothing tests.
**Recommended fix:** store a blob, replace the stored bytes with different bytes of the same
length (so the frame still decodes), then assert `Corrupt` after exhausting `get_stream`, on
both substrates.

### R-W1-208 — MINOR — `NotSupported` is never raised by a test
**Location:** `src/paa/storage/coldlake/minio_backend.py:256-266` (`_require_boto3`) and
`896-917` (`_blob_store_from_settings`); `tests/storage/test_blob_errors.py:61-69` (the
vocabulary tuple).
**Evidence:** `NotSupported` appears in the tests only as a *shape* assertion
(`issubclass(NotSupported, Unreachable)`, `tests/storage/test_blob_errors.py:135`) and in the
contract's docstring-member list. Every `S3BlobStore` in the test suite is constructed with an
injected client [RAN over `tests/storage/*.py`: 10 constructions, all with `client=`], so
`_require_boto3()` is never reached; and `_blob_store_from_settings` is never called. `boto3`
is **not installed** in this venv [RAN: `ModuleNotFoundError: No module named 'boto3'`], so
`S3BlobStore("bucket")` without a client raises `NotSupported` today, for free.
**Why it matters:** the brief asks "is there a test per vocabulary member?" — for
`NotSupported` the answer is **no**: it is a member of the vocabulary that no test has ever
produced. One of the seven members of the new vocabulary is untested in the only way that
matters.
**Recommended fix:** two lines — `pytest.raises(NotSupported)` around
`S3BlobStore("bucket")` with no client (guarded by `boto3_available()` for a full install),
and one around `_blob_store_from_settings` with an empty `s3_endpoint`.

### R-W1-209 — MINOR — idempotence is tested for one member, and the guard it names is not the one that works
**Location:** `tests/storage/test_blob_errors.py:153-162` and `399-401`.
**Evidence:** idempotence is asserted for `NotFound` only, in both translators. The
`translate_blob_error` guard that makes it work is `isinstance(exc, StorageError)`
[VERIFIED `src/paa/storage/coldlake/errors.py:285-292`]; deleting that branch changes nothing,
because the very next branch — `isinstance(exc, PaaError)` — catches every `StorageError`
(`StorageError` is a `PaaError` subclass [VERIFIED `src/paa/core/errors.py:112`]), so M8
produces **0 failures** [RAN]. The same holds in `_translate_s3_error`, where the
`BlobError` branch is subsumed by the `PaaError` branch.
**Why it matters:** the *documented* rule ("an error that is already one of ours comes back
unchanged") is tested, but through a path that does not distinguish "ours" from "any domain
error". If someone later changed the `PaaError` branch to translate, the idempotence test
would still be the only thing standing between the boundary and a flattened failure — and it
would still pass for the wrong reason today.
**Recommended fix:** either delete the redundant branches (a code change — not mine) or
assert idempotence for **every** member of `VOCABULARY` (a two-line parametrisation), so the
property is pinned per member rather than per class.

### R-W1-210 — MINOR — one of the seven locks is vacuous on revert
**Location:** `tests/conventions/test_import_conventions.py:355-381`.
**Evidence:** the test skips every module whose `RelationalBackend` import is *not* in the
runtime set [VERIFIED `…:369`]. On the reverted tree, all seven converted modules are behind
`TYPE_CHECKING`, so they are all skipped, and the test passes — [RAN] reverting the
conversion fails only `test_the_contract_is_not_imported_behind_type_checking` and
`test_the_family_resolves_the_contract_one_way`.
**Recommended fix:** make the runtime half cover every module that *references* the contract
in any import form (runtime **or** type-checking) and assert that each of them binds the
class at runtime. That turns it into a third regression test rather than a description.

### R-W1-211 — MINOR — a stale deferral is silently accepted
**Location:** `tests/conventions/test_import_conventions.py:84-89` (`DEFERRED`) and the three
tests that consult it.
**Evidence:** all three convention tests skip every module named in `DEFERRED`
[VERIFIED `…:345`, `…:369`, `…:394`]. When wave 2 converts `paa/observability/runs.py`, every
test will keep skipping it — so forgetting to delete the `DEFERRED` entry (which the entry's
own docstring says is what happens: "the entry is deleted when that task lands") is invisible.
**Recommended fix:** one assertion — every module in `DEFERRED` must **still** import the
contract behind `if TYPE_CHECKING:`. A deferral that is no longer needed then fails, and the
entry must be deleted.

### R-W1-212 — MINOR — "137/137 modules" is a file count; the lock probes 136
**Location:** `tests/conventions/test_import_conventions.py:167-180` (`_module_files`).
**Evidence:** the enumerator skips a package's own `__init__.py` (`if not parts: continue`,
[VERIFIED `tests/conventions/test_import_conventions.py:175-178`]), so it yields **136** module names [RAN]. `find src/paa -name "*.py"`
returns **137** files [RAN]. The implementer's report and its own sweep script both print
"modules checked: 137" [VERIFIED `reports/w1-impl/W1-IMPL-03-T-004.md:569`], and the report
itself says elsewhere "the sweep found 136 importable modules and one tolerated extras
failure" [VERIFIED `…:583`]. On this host: 136 probed, 135 import cleanly, 1 tolerated
(`paa.storage.graph.kuzu_backend` — the `graph` extra) [RAN].
**Why it matters:** minor, but the number is quoted in the COMMS line and in the gate
summary, and it is wrong in the way that matters to a reader ("every module in the package
is covered" is not literally true: `paa/__init__.py` is never a probe *target*, although it
is imported as the parent of every probe).
**Recommended fix:** either count files and say so, or include the package itself as a
target.

### R-W1-213 — MINOR — the deployment refusals assert "non-zero", not a code
**Location:** `tests/deployment/test_fetch_images.py:788` and every other refusal assertion.
**Evidence:** every refusal is asserted as `result.returncode != 0` [VERIFIED `…:788`, `969`,
`1170`, `1195`, `1300`, `1341`, `1368`, `1428`, `1442`, `1455`, `1470`, `1497`, `1520`]. The
script distinguishes **exit 1** (a refusal) from **exit 2** (a usage error:
`--compose needs a path`, `--load needs a directory`, `unknown argument` —
[VERIFIED `deploy/fetch-images.sh:796-815`]), and **no test covers the usage errors
at all**. Mutation D5 (`build_plan || true`) leaves the whole file green because the
empty-plan gate still exits non-zero [RAN] — the non-zero exit is not tied to the refusal.
**Recommended fix:** assert `== 1` for each refusal shape, and add three small cases for the
usage errors (`--compose` with no path, `--load` with no directory, an unknown argument) each
asserting `== 2`.

### R-W1-214 — MINOR — the "28 of the new tests fail" figure is 26
**Location:** `reports/w1-impl/W1-IMPL-01-T-001.md:371-395`.
**Evidence:** with the implementer's own method (the three pre-change source files restored
into a scratch `src`, `PYTHONPATH` pointed at it): **26 failed, 24 passed** [RAN]; with a
second variant (the whole pre-change `src/paa` plus the new `errors.py`): **26 failed, 24
passed** [RAN]. `test_blob_errors.py` cannot be collected at all [RAN]. The report's own
`FAILED` listing sums to 26 as well (13 single lines + the 7-case parametrized line + the
6-case contract line) — so the summary line "28 failed" does not match its own evidence.
W1-REV-01 has recorded the same discrepancy independently; this confirms it and adds the
measurement.
**Recommended fix:** correct the report and the gate summary to "26 failures + one collection
error", and — more usefully — restate the claim as it is true: *the new tests fail against the
pre-change sources, and the file of 50 fails to even import*.

### R-W1-215 — NIT — two equivalent branches in each translator
**Location:** `src/paa/storage/coldlake/errors.py:285-292`; `src/paa/storage/coldlake/minio_backend.py:361-367`.
**Evidence:** `StorageError` and `BlobError` are subclasses of `PaaError`, so the first branch
of each translator is subsumed by the second, and both return `exc` [RAN: M8 → 0 failures;
`VERIFIED src/paa/core/errors.py:112`]. The tests cannot tell the branches apart.
**Recommended fix:** a code change (one branch per translator) — out of my write-set; recorded
here because the *test* that was supposed to pin the guard (R-W1-209) cannot.

### R-W1-216 — NIT — two subprocess tests without a timeout; the aggregate's timeout is uncaught
**Location:** `tests/storage/test_blob_errors.py:521-527` and `538-544`;
`tests/conventions/test_import_conventions.py:459`.
**Evidence:** the two `subprocess.run` calls in `test_blob_errors.py` pass no `timeout`; a
hung child hangs the suite [VERIFIED by reading the call sites]. The conventions aggregate
passes `timeout=300` but does not catch `TimeoutExpired`, so a timeout surfaces as an ERROR
rather than an assertion [VERIFIED `…:456-464`]. The per-probe timeout of 120 s
[VERIFIED `…:250`] makes the pathological fixture wall clock ≈ 34 minutes.
**Recommended fix:** `timeout=60` on the two blob subprocesses, and catch `TimeoutExpired` in
the aggregate so the failure reads as a verdict.

---

## 6. Missing edge cases and tests (prioritised)

### P1 — must add

| # | Scenario | Input / case | Expected behaviour | Where it belongs | Sketch |
|---|---|---|---|---|---|
| 1 | A driver error that fails **only** `put_object` | stub knob `put_failure = ClientError("AccessDenied", status=403)` | `store.put(b"x")` raises `AccessDenied` (not a raw `ClientError`) | `tests/storage/test_minio_backend.py` | `client.put_failure = ClientError("AccessDenied", status=403)`; `with pytest.raises(AccessDenied): store.put(b"x")` |
| 2 | A driver error that fails **only** `upload_fileobj`, wrapped in `S3UploadFailedError` | stub knob `upload_failure` | `store.put_stream(...)` raises the **same class** as `store.put(...)` for the same condition | same | `client.upload_failure = ...`; assert `type(put_err) is type(stream_err)` |
| 3 | A **missing bucket** (code `NoSuchBucket`) | stub knob `bucket_missing` | `exists`/`stat`/`delete` raise `NotFound` (details carry `bucket`); `put`/`put_stream` raise `NotFound` too | same | `with pytest.raises(NotFound): store.exists(digest)` |
| 4 | `NotSupported` when the `s3` extra is absent | `S3BlobStore("bucket")` with no client (boto3 not installed here) | raises `NotSupported`, `reason="driver_missing"` | `tests/storage/test_minio_backend.py` | `@pytest.mark.skipif(boto3_available())`; `pytest.raises(NotSupported)` |
| 5 | A lying size hint on the **uncompressed** path | `put_stream(io.BytesIO(b"twelve bytes"), compress=False, size=3)` | `MalformedRequest` on both substrates | `tests/storage/test_blobstore_contract.py` | parametrise the existing test over `compress` and over an over- and an under-declared size |
| 6 | `get_type_hints` resolves the contract on the eight converted constructors, and `get_database` | `typing.get_type_hints(X.__init__)` for the 8 constructors; `typing.get_type_hints(get_database)` | no `NameError` | `tests/conventions/test_import_conventions.py` | `assert typing.get_type_hints(cls.__init__)` — this is the property the implementer measured by hand, and it is what would catch the `get_database` defect (see §7) |

### P2 — should add

| # | Scenario | Input / case | Expected behaviour | Where it belongs | Sketch |
|---|---|---|---|---|---|
| 7 | Each S3 code table, decided by the **code alone** | every member of `_S3_*_CODES`, built with **no** `ResponseMetadata` | the expected class | `tests/storage/test_blob_errors.py` | `@pytest.mark.parametrize("code", sorted(_S3_RETRYABLE_CODES))` with `ClientError(code)` and no status |
| 8 | The streaming path's **hash mismatch** (a valid frame, wrong bytes) | replace the stored bytes with same-length different bytes | `Corrupt` after the final chunk, on both substrates | `tests/storage/test_blobstore_contract.py` | corrupt after `put`, then `list(store.get_stream(digest))` raises `Corrupt` |
| 9 | The CAS write path's `OSError` | a read-only root (chmod) or a full disk | `AccessDenied` / `BlobFailure` (errno in the details), never a raw `OSError` | `tests/storage/test_blob_errors.py` | construct the store over a read-only parent; assert `pytest.raises(BlobError)` |
| 10 | The exact exit code of each refusal, and of each usage error | `--compose` with no path; `--load` with no directory; an unknown argument | `2` for usage, `1` for a refusal | `tests/deployment/test_fetch_images.py` | `assert result.returncode == 2` |
| 11 | A **relative** `--compose` path | `--compose docker-compose.yml` with `cwd=REPO_ROOT` | the same plan as the absolute path | same | compare the two `--list` outputs |
| 12 | A deferral that is no longer needed | every module in `DEFERRED` | must still import the contract behind `TYPE_CHECKING` | `tests/conventions/test_import_conventions.py` | `assert (CONTRACT_MODULE, CONTRACT_NAME) in _imports_of(path)[1]` for each deferred module |
| 13 | The `BlobFailure` path **through the store** | a full disk (ENOSPC) on the CAS write path | `BlobFailure` with `errno` in the details | `tests/storage/test_blob_errors.py` | the translator unit test exists; the *store* path does not |

### P3 — nice to have

| # | Scenario | Input / case | Expected behaviour | Where it belongs | Sketch |
|---|---|---|---|---|---|
| 14 | A service name or image value with non-ASCII characters | `image: café/img:1@sha256:…` | the reference is passed through unchanged | `tests/deployment/test_fetch_images.py` | add to `_PARITY_SHAPES` |
| 15 | A registry with a **port** in the host under a mirror | `registry:5000/img:tag@sha256:…` with `PAA_IMAGE_MIRROR` | the tag is split off the last path segment only | same | mirror the existing mirror test with a port |
| 16 | The same file given twice (`--compose a --compose a`) | idempotent merge | the same plan, no duplicated rows | same | compare with the single-file plan |
| 17 | A tab in a service name | `image:` values are tab-separated rows | the row does not shift its columns | same | the reader's "-" convention exists for the empty field; a tab is the same class of bug |
| 18 | A compose file that is a **directory** | `--compose /tmp` | refused, not read as empty | same | `test_a_compose_file_that_does_not_exist_is_refused` is the sibling |
| 19 | The CAS `stat`/`delete` `OSError` paths | a file that vanishes between the locate and the stat | `NotFound`/`BlobFailure`, never a raw `OSError` | `tests/storage/test_blob_errors.py` | monkeypatch `Path.stat` |

---

## 7. What I could not verify

* **The real botocore error codes.** `boto3` is not installed and there is no server, so every
  claim about what a real S3 server sends on a missing bucket, a refused request or a
  throttled pull is a code-review premise, not a test premise. This does not affect any
  finding above: every one of them is measured against the code in the tree.
* **The Compose cross-check.** The implementer states the merge subset was checked against
  real Compose v5.6.0 by hand [VERIFIED `reports/w1-impl/W1-IMPL-02-T-003.md:255-258`]. I
  could not repeat it (no `docker compose` here, and the standing orders forbid touching
  containers). The *automated* part of that claim — the parity corpus and the merged-stack
  tests — I did verify by mutation (D1/D2/D3 all caught).
* **The full suite.** Per the standing orders I ran **single files only**; I never ran
  `pytest tests/`. The gate's "2019 passed / 233 skipped" is therefore taken from the gate
  document, not from me.
* **The `observability/runs.py` deferral.** I did not convert it (not my write-set) and did
  not measure whether its conversion would break an import; the implementer's classification
  says it would not (it is a first-party, inert block).
* **The T-004 measurement I did check.** I confirmed the two claims I could execute:
  converting `cas.py`'s `TYPE_CHECKING` block breaks **19** modules with
  `ImportError: cannot import name 'BlobStore' from partially initialized module` [RAN], and
  `typing.get_type_hints` on the eight converted constructors raises `NameError` before the
  change and resolves after it [RAN]. The implementer's correction to the plan's "nine
  load-bearing blocks" figure is therefore **sound as far as the cas.py case goes**, and the
  plan's §0/§3.2 evidence should be corrected (the code reviewers have the same point).
* **The `get_database` defect the implementer reported is real** — `typing.get_type_hints
  (paa.storage.relational.get_database)` raises `NameError: name 'Settings' is not defined`
  [RAN] — and **no test catches it**, because the fresh-interpreter lock only proves the
  module *imports*. It is the same class of defect the T-004 lock was built to prevent, one
  module over: P1 item 6 is the test that would catch it.
