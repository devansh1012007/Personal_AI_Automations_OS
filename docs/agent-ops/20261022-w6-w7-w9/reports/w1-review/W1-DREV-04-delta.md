<!-- ORCH-DIGEST:START -->
agent: W1-DREV-04 | scope: wave-1 delta review of ddef4cc (T-001 blob substrate), 257b80f (T-003 deploy image contract), a3e6819 (T-004 import lock) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 1 | MINOR 8 | NIT 0
claims_audit: 48 claims checked / 11 unsupported or false
band_aids_found: none
top_3_risks: the new wrong-indent refusal covers only indent > 4, so a 3-space service Compose accepts is dropped silently and an unpinned image passes the gate with exit 0; a raw RecursionError can leave the substrate on the probe path; ADR-0032 and the protocol docstring state a write path that does not probe, and the code probes
optimizations: DO-NOW 3 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: done
confidence: med-high (the diff's behaviour was measured directly — script, reader, probes and reversals; the live-S3 and real-botocore error shapes were not)
<!-- ORCH-DIGEST:END -->

# Code review — W1-DREV-04 (wave-1 delta)

## 0. Method

**Diff under review:** `git diff 3ac9d6b..HEAD -- . ':!docs/agent-ops'` — three code commits
(`ddef4cc` coldlake, `257b80f` deploy, `a3e6819` imports) plus the three improvement reports.
The question this review answers is narrow: *did the improvements close the wave-1 findings
they claimed, and did they introduce anything new?* It is not a second wave-1 review.

**Read in full:** `deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`,
`src/paa/storage/coldlake/{minio_backend,base,errors,cas}.py`,
`tests/storage/{test_blob_errors,test_minio_backend,test_blobstore_contract}.py`,
`src/paa/storage/relational/__init__.py`, `tests/conventions/test_import_conventions.py`,
`docs/adr/0030-deployment-image-contract.md`, `docs/adr/0032-blob-error-vocabulary.md`,
`docs/deployment/docker.md`, `docs/plans/w6-w7-w9.md`, the three `reports/w1-improve/*` reports
and `COMMS.md`.

**Commands run (all scoped; the full suite was not run):**

```
[RAN] .venv/bin/python -m ruff check src tests          -> All checks passed!
[RAN] .venv/bin/python -m mypy src                      -> Success: no issues found in 137 source files
[RAN] .venv/bin/python -m pytest tests/storage/test_blob_errors.py test_minio_backend.py test_blobstore_contract.py -q
        -> 169 passed, exit 0
[RAN] .venv/bin/python -m pytest tests/deployment -q    -> 106 passed, exit 0
[RAN] .venv/bin/python -m pytest tests/conventions -q   -> 13 passed, exit 0
[RAN] bash deploy/fetch-images.sh --compose docker-compose.yml --compose docker-compose.gvisor.yml --list
        -> 6 rows (2 build, 4 pull), exit 0
[RAN] bash deploy/fetch-images.sh --compose /tmp/opencode/drev/IND3.yml --list   -> only redis planned, exit 0
[RAN] bash deploy/fetch-images.sh --compose /tmp/opencode/drev/IND5.yml --list   -> refused, exit 1
[RAN] docker compose -f /tmp/opencode/drev/IND3.yml config -> postgres.image: nvidia/cuda:latest   (Compose v5.6.0)
[RAN] .venv/bin/python -m pytest tests/deployment/test_fetch_images.py -q   (HEAD test file, 3ac9d6b script)
        -> 26 failed
[RAN] PYTHONPATH=<scratch>/pre/src .venv/bin/python -m pytest tests/storage/test_blob{_errors,_store_contract}.py test_minio_backend.py -q
        -> 12 failed
[RAN] .venv/bin/python /tmp/opencode/drev/probe_coldlake.py -> A) write path probes, head_bucket calls=1
                                                          -> B) read/write split on a status-only 404
                                                          -> C) RecursionError, head_bucket calls=98
[RAN] .venv/bin/python -c "import typing; from paa.storage.relational import get_database; typing.get_type_hints(get_database)"
        -> {'settings': Settings | StorageSettings, 'return': Database | PostgresDatabase}
```

**Order.** The diff and the code were read and probed **first**; the implementer reports were
read afterwards, for the claims audit (§2) only. Nothing below depends on the implementers'
account of their own work.

**No band-aids were introduced.** `git diff 3ac9d6b..HEAD -- src tests pyproject.toml` contains
no added `# noqa`, no added `type: ignore`, and `pyproject.toml` is untouched. The gate stays
zero: ruff 0, mypy 0.

## 1. Blind-pass findings (before reading implementer reports)

Probing the diff on its own terms produced five defects, of which the first is MAJOR:

| # | What the blind pass found | Where |
|---|---|---|
| 1 | The **new** wrong-indent refusal fires only when `indent > 4`. A service whose keys sit at **three** spaces — valid YAML, accepted by Compose — is neither read nor refused: the service is dropped from the plan and the script exits **0**. An unpinned image in that service is never checked. | `deploy/fetch-images.sh:444-448`, `:450`, `:645-653` |
| 2 | The write path **does** probe the bucket on an ambiguous 404, which is what three durable records say it does not. | `src/paa/storage/coldlake/minio_backend.py:682-683`, `:704` |
| 3 | The read path and the write path classify the *same* 404 differently: read says "bucket not found" (+`namespace`), write says "blob not found" with no namespace and no probe. | `minio_backend.py:357-367` vs `:604-656` |
| 4 | `_translate_failure` and `_namespace_is_absent` call each other with no re-entrancy guard; a probe failure whose cause carries a 404 recurses until `RecursionError`, which leaves the substrate raw. | `minio_backend.py:688-706`, `:649-655` |
| 5 | The protocol's `stat()` docstring states that the S3 store "does not read the blob, so it does not see the damage" — contradicted by the code and by a test added in the same commit. | `src/paa/storage/coldlake/base.py:194-196` |

Reading the implementer reports afterwards produced four more, all of them *records* rather
than behaviour: the refusal-shape count is stated three different ways (R-W1-306), the script's
own header no longer names the `skip` action it emits (R-W1-307), and the two deferrals are
still open with their NEEDs unanswered (R-W1-308, R-W1-309).

## 2. Claims audit

The three improvement reports' disposition tables carry 36 claims (T-001: 20, T-003: 8, T-004: 8).
I re-derived them **group by group** — claims that share one mechanism were checked by one
measurement each, and the table below names the rows I checked on their own — alongside 12
statements in the durable records the improvements edited (ADR-0030, ADR-0032, the protocol
docstring) and in COMMS.md. **48 claims checked, 11 unsupported or false** (the ✘ rows; the last
row of the table restates three of them and is not counted twice).

| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| T-001: R-W1-001 — a missing bucket raises `NotFound("bucket not found")` with `namespace="bucket"` from **every** method | ✔ | All seven methods reach the same raise; `[RAN]` the reversal against 3ac9d6b fails `test_a_missing_bucket_raises_from_every_method` and `test_the_namespace_detail_is_present_only_for_a_missing_namespace`. ADR-0032 §1/§6 amended to match. |
| T-001: R-W1-002/003 — the filesystem substrate translates its raw `OSError`s and its codec errors | ✔ | `[RAN]` the reversal fails `test_an_unreadable_archive_is_a_refusal` and `test_a_corrupt_frame_is_corrupt_in_the_metadata_path`. |
| T-001: R-W1-201 — every transport path reports the vocabulary | ✔ | `tests/storage/test_minio_backend.py:608` drives head/put/upload/get/delete to a driver failure each. |
| T-001: R-W1-204 — the S3 code tables are pinned against literals | ✔ | `[RAN]` mutating `NoSuchBucket` → `NoSuchBucketXX` fails 2 cases. |
| T-001: R-W1-216 — "FIXED (my half)": the two blob subprocess probes have `timeout=60` | ✔ | `tests/storage/test_blob_errors.py`. The other half is deferred and the deferral is honest — but it is still open at HEAD (R-W1-308). |
| T-001 (COMMS.md:69): "The probe costs one request on the miss path only, **never on a write**" | ✘ | **False.** `[RAN]` a `put` whose driver error is a bodyless 404 → `head_bucket calls=1`. The write path probes on the ambiguous failure. R-W1-302. |
| T-001 (COMMS.md:73): "the S3 store's `stat()` … does not read the blob, so it cannot see a damaged object" | ✘ | **False for legacy objects.** `minio_backend.py:1047-1049` falls back to streaming when the recorded size is missing; the same commit's own test `tests/storage/test_blobstore_contract.py:417-433` asserts `Corrupt` for exactly that. R-W1-305. |
| T-003: R-W1-101/102 — the merge key and the flow `include:` are refused | ✔ | `[RAN]` `<<:` at four spellings and `include: [other.yml]` are refused with exit 1, the message naming the file and the shape. |
| T-003: R-W1-103 — a profile-gated unpinned image is refused | ✔ | `[RAN]` exit 1, "not digest-pinned". |
| T-003: R-W1-104/105/106 — the profile-gated service stays in the plan and the fetch decides; `COMPOSE_PROFILES` honoured; profiles of several files merged | ✔ | `[RAN]` the plan keeps the gated service; `docker compose --profile p` and `--profile q` agree with the script's union `p,q`. |
| T-003: R-W1-107 — "the corpus is now a shape with a list of spellings … it went from 7 shapes / 7 spellings to **11 shapes / 20 spellings**" | ✘ | The restructure and the growth are real (`[RAN]` 7 shapes at 954705d → 12 shapes / 20 spellings at 257b80f), but the end figure is **12**, not 11. The same wrong figure is repeated in COMMS.md:78. R-W1-306. |
| T-003: R-W1-213 | ✔ | The three "further shapes" of §2.2 of that report are refused. The refusal for the wrong-indent shape is, however, one-sided — R-W1-301. |
| T-004: R-W1-110 — `typing.get_type_hints(get_database)` resolves | ✔ | `[RAN]` it returns the two annotations; the reversal of `src/paa/storage/relational/__init__.py` fails 2 cases. |
| T-004: R-W1-113/211 — a deferral that has been acted on fails the lock | ✔ | The lock asserts the deferral still resolves the contract. |
| T-004: R-W1-112 — DEFERRED (NEED posted, no answer) | ✔ (as a deferral) | The deferral is honestly recorded, but the finding's substance is unchanged at HEAD and the plan's figures are still wrong: R-W1-309. |
| ADR-0030:421 — "Ten shapes are outside the subset" | ✘ | **12** shapes. R-W1-306. |
| ADR-0030:518 — "Ten shapes are refused today" | ✘ | **12** shapes. R-W1-306. |
| ADR-0030:444-447 — "a service written any other way could hide an `image:` … the reader refuses it rather than dropping the service from the plan" | ✘ | **False for three-space indent** — the reader drops the service and exits 0. R-W1-301. |
| ADR-0030:410-414 — the merge subset was "checked against the real thing — `docker compose config`, v5.6.0" | ✔ | `[RAN]` the repo's own merged stack plans 6 rows and exits 0; compose v5.6.0 is on this host. |
| ADR-0032:310 — "**The write path does not ask.**" | ✘ | The write path asks on the ambiguous failure. R-W1-302. |
| ADR-0032:391-392 — "**The write path was not restructured.** … it does not probe." | ✘ | `[RAN]` `head_bucket calls=1` on a write. R-W1-302. |
| ADR-0032:322-330 — "`namespace="bucket"` is present **if and only if** the substrate's namespace is what is missing" | ✘ | The write path violates it for a status-only 404: "blob not found", no namespace. R-W1-303. |
| ADR-0032:375-379 — a 403 on the probe falls back to an object-level absence | ✔ | `minio_backend.py:643`, `:647`. |
| `base.py:194-196` — "The S3 store … does not read the blob, so it does not see the damage." | ✘ | **False for legacy objects.** R-W1-305. |
| COMMS.md:69, :73, :78 | ✘ (as above) | The trail repeats two false claims and one wrong figure; the three statements are counted above, not here. |

## 3. Long-run test (independent answers)

**Source.** The compose file is operator input. The reader is handed file paths as `awk`
*arguments* (`deploy/fetch-images.sh:674`), never interpolated into the program text, so a path
cannot inject program text; every shell expansion is quoted. The reader treats the file as text
and refuses constructs, which is the right posture — but see R-W1-301: one construct (a
three-space service) is neither read nor refused, and that is a *source* problem, because the
file is where the unpinned image comes from. The blob substrate's boundary is the driver error
itself: `_translate_s3_error` reads the code, the status and the class names structurally and
never imports `botocore` at module scope, so the unit suite exercises the code the deployment
runs.

**Load.** The plan is a handful of rows; the merge is O(services × files) inside one `awk`. The
shell accumulates rows by string concatenation in `build_plan` (`:742`, `:788`), which is O(n²)
in the number of services — bounded by the size of a compose file, so not a real risk. The blob
store's new cost is **one extra request on the miss path**, and one more on the ambiguous
*write-failure* path (R-W1-302): the hot path — a successful write — pays nothing.

**Hostile.** A compose file that hides an image is the hostile input, and the diff closes three
of the four ways to hide one (merge key, flow mapping, deeper indent) and leaves the fourth
(three-space indent) open: `[RAN]` the file is accepted by Compose, the service is dropped, the
unpinned image is never checked, exit 0. On the blob side, a hostile server answer is a driver
error whose shape the store does not expect; the classifier handles the codes it names and
falls back to "the substrate failed" — except in the recursion of R-W1-304.

**Operability.** Exit codes are consistent: usage errors exit 2 (`--compose` with no path,
`--load` with no directory, an unknown argument), refusals exit 1 (an unpinned image, an
unreadable construct), and a missing compose file exits 1 — pre-existing, verified identical at
3ac9d6b, and defensible (a missing file is a refusal, not a misuse). `--list` needs no daemon
and is a gate; the repository's own stack still plans correctly (6 rows, exit 0). The one
operability regression is the row format, which the script's own header no longer describes
(R-W1-307).

**Reversal.** Both halves of the delta were reversed and both fail loudly:
`[RAN]` the HEAD deployment test file against the 3ac9d6b script → **26 failed**; the HEAD
coldlake test files against the 3ac9d6b sources → **12 failed**, including
`test_a_missing_bucket_raises_from_every_method`,
`test_the_namespace_detail_is_present_only_for_a_missing_namespace` and
`test_a_corrupt_frame_is_corrupt_in_the_metadata_path`. The 26 matches the corrected figure in
COMMS.md:78; the report's original "22" was corrected there.

## 4. Findings

### R-W1-301 [MAJOR] The new wrong-indent refusal is one-sided: a three-space service is dropped silently, and an unpinned image passes the gate with exit 0
- **Location:** `deploy/fetch-images.sh:444-448` (the "deep" test), `:450` (the four-space key
  rule), `:645-653` (the refusal itself); the same rule in the parity oracle at
  `tests/deployment/test_fetch_images.py:883-895`; the claim it was meant to satisfy at
  `docs/adr/0030-deployment-image-contract.md:444-447`; the corpus entry at
  `tests/deployment/test_fetch_images.py:577-590`.
- **Evidence:** `[VERIFIED deploy/fetch-images.sh:448]` the deep flag is set only
  `if (indent > 4 && …)`; `[VERIFIED deploy/fetch-images.sh:450]` a key is read only at
  `/^    [^[:space:]#]/`; `[VERIFIED deploy/fetch-images.sh:645]` the refusal fires only
  `if (!saw_key && deep)`. A service whose keys sit at **three** spaces therefore sets neither
  flag: it is not read and not refused. `[RAN]` `bash deploy/fetch-images.sh --compose
  IND3.yml --list` → one row (`pull redis:7@sha256:… (redis)`), **exit 0**, while
  `IND3.yml`'s `postgres` names `nvidia/cuda:latest`; `[RAN]` `docker compose -f IND3.yml
  config` (v5.6.0) → `postgres: image: nvidia/cuda:latest`, i.e. **Compose accepts the file**.
  The same file at four spaces → refused, exit 1 ("not digest-pinned"); at five spaces →
  refused, exit 1 ("has keys this reader does not understand"); at one and two spaces Compose
  itself refuses the file (invalid YAML / "services.image must be a mapping"), so by ADR-0030's
  own line those are not hazards. **Three spaces is the only indent that Compose accepts and the
  reader drops.** `[VERIFIED tests/deployment/test_fetch_images.py:577-590]` the corpus carries
  **one** spelling of this shape (six spaces); `[VERIFIED
  tests/deployment/test_fetch_images.py:883-895]` the second reader has the same `indent > 4`
  rule, so the two readers agree and the lock cannot see the hole. `[VERIFIED
  docs/adr/0030-deployment-image-contract.md:444-447]` the ADR claims the reader "refuses it
  rather than dropping the service from the plan".
- **Why it matters:** this is the failure the whole T-003 improvement exists to prevent — an
  image the compose file names, that the deployment runs, that the script never checks, and a
  zero exit status saying the plan is fine. The pin rule is the gate; a construct that hides an
  image from the plan hides it from the gate. The pre-change script had the same hole
  (`[RAN]` at 3ac9d6b: exit 0, service dropped), so this is not a regression — but the
  improvement's explicit job was to close the class, and it closed half of it.
- **Recommended fix (root cause):** the test for "the reader cannot see this service's keys"
  must not be "deeper than four". It must be "**no key at four spaces, but at least one key at
  some other indent**" — i.e. track `saw_key_any_indent` alongside `saw_key`, and refuse when
  `!saw_key && saw_key_any_indent`. Implement it **once** and teach both readers the same
  rule, then add the three-space spelling to the corpus (the shape's other spelling), which
  makes the lock able to see the hole it now has. The empty-plan refusal is not a substitute:
  it only fires when the hidden service is the *only* one.

### R-W1-302 [MINOR] Three durable records say the write path does not probe; the code probes
- **Location:** `src/paa/storage/coldlake/minio_backend.py:682-683` ("The write path does not
  ask"), `docs/adr/0032-blob-error-vocabulary.md:310` ("**The write path does not ask.**"),
  `docs/adr/0032-blob-error-vocabulary.md:391-392` ("**The write path was not
  restructured.** … it does not probe."), `docs/agent-ops/20261022-w6-w7-w9/COMMS.md:69`
  ("never on a write").
- **Evidence:** `[VERIFIED minio_backend.py:704]` `_translate_failure` probes the bucket when
  the translated error is an ambiguous absence, and `_translate_failure` is the translate hook
  of the write paths (`[VERIFIED minio_backend.py:721]`, `:734`, `:868`, `:892`, `:974`,
  `:998`, `:1084`). `[RAN]` a `put` whose driver error is a bodyless 404 → `head_bucket
  calls=1`, and the error raised is `NotFound: bucket not found … namespace='bucket'`. The
  improvement's own report is the one accurate statement:
  `[VERIFIED reports/w1-improve/W1-IMPR-01-T-001.md]` "The write path *does* resolve an
  **ambiguous** 404 — a bodyless one — through the same probe, on the failure path only."
- **Why it matters:** the ADR is the durable record a cold reader trusts; it currently
  understates the cost of the mechanism and mis-describes where it applies. A future
  maintainer who believes "the write path never probes" will not think to bound the probe
  (R-W1-304) or to keep the write path and the read path answering alike (R-W1-303).
- **Recommended fix:** state the qualified truth in all four places — "the write path does not
  probe a **successful** write; on an **ambiguous** failure it resolves the absence with the
  same probe, so a write can cost one extra request" — and keep the parenthetical from the
  report, which is already correct.

### R-W1-303 [MINOR] The read path and the write path answer differently about the same 404
- **Location:** `src/paa/storage/coldlake/minio_backend.py:357-367`
  (`_is_ambiguous_absence`) vs `:604-656` (`_namespace_is_absent`).
- **Evidence:** `[VERIFIED minio_backend.py:367]` the write-side predicate is
  `translated.details.get("s3_code") == _S3_STATUS_AS_CODE` — it needs the *code*; the
  read-side predicate at `[VERIFIED minio_backend.py:645]` is
  `code == _S3_STATUS_AS_CODE or status == 404` — it also accepts the *status*.
  `[RAN]` a driver error that carries a status 404 and no `Error.Code`:
  the write path (`put`) → `NotFound: blob not found`, details **without** `namespace`, and
  `head_bucket calls=0`; the read path (`stat`) → `NotFound: bucket not found`, details **with**
  `namespace='bucket'`, `head_bucket calls=1`.
- **Why it matters:** ADR-0032 §1's rule is that `namespace="bucket"` is present **if and only
  if** the namespace is what is missing, and that the two paths answer alike. For this shape
  the write path names the *wrong* condition (the blob, when the bucket is gone) and carries no
  namespace detail, so a caller that tests the detail gets a different answer from the same
  underlying failure depending on which path it took. **Reachability is narrow**: botocore
  synthesises `Error.Code="404"` for a bodyless HEAD, so this shape is synthetic — but the two
  predicates disagree, which is the defect.
- **Recommended fix:** one predicate, used by both paths — "the answer does not say which
  absence it is" is true when the code is the status-as-code **or** when the status is 404 and
  no qualified code is present. Derive both call sites from it rather than each deciding on
  its own.

### R-W1-304 [MINOR] The bucket probe and the failure translator call each other with no bound
- **Location:** `src/paa/storage/coldlake/minio_backend.py:688-706` (`_translate_failure`, the
  probe at `:704`) and `:649-655` (`raise_blob_error(…, translate=self._translate_failure)`).
- **Evidence:** `[VERIFIED minio_backend.py:704]` `_translate_failure` calls
  `_namespace_is_absent()`, which on an unrecognised failure calls `raise_blob_error` with
  `translate=self._translate_failure` — the same translator. `[RAN]` with a probe failure whose
  *cause* carries a 404 (the shape boto3's `S3UploadFailedError` has): `RecursionError:
  maximum recursion depth exceeded`, with **98** `head_bucket` calls at `recursionlimit=300`.
  The `RecursionError` is not a `PaaError`, so it leaves the substrate raw — the invariant
  ADR-0032 exists to keep ("a raw exception leaving the substrate is the invariant the
  vocabulary exists to keep").
- **Why it matters:** a store that dies with `RecursionError` instead of `Unreachable` tells
  the caller nothing and misses every `except PaaError` at the edge. **Reachability is narrow**:
  a plain botocore `ClientError("404")` short-circuits at `:645` and never recurses; the
  recursion needs the probe's failure to carry the 404 in its cause chain while presenting no
  code of its own.
- **Recommended fix:** bound the probe by construction — resolve the ambiguity at most once per
  failure (a re-entrancy flag on the store, or an explicit "already probed" argument threaded
  into `_namespace_is_absent`), so the translator cannot re-enter the probe.

### R-W1-305 [MINOR] The protocol's `stat()` docstring denies what the same commit's test asserts
- **Location:** `src/paa/storage/coldlake/base.py:194-196` (added in `ddef4cc`).
- **Evidence:** `[VERIFIED base.py:194-196]` "The S3 store answers from the size recorded when
  the object was written and does not read the blob, so it does not see the damage." But
  `[VERIFIED minio_backend.py:1047-1049]` the recorded size is optional: when it is missing the
  store falls back to `sum(len(chunk) for chunk in self.get_stream(digest))`. And the same
  commit added `[VERIFIED tests/storage/test_blobstore_contract.py:417-433]`
  `test_a_corrupt_frame_is_corrupt_when_the_recorded_size_is_missing`, which asserts `Corrupt`
  for exactly that case. The same claim is repeated in `COMMS.md:73`.
- **Why it matters:** the protocol docstring is where a caller decides what `stat()` can cost
  and what it can detect. As written it says "S3 never detects a damaged blob"; the code says
  "S3 detects it for every object written before the metadata existed".
- **Recommended fix:** one sentence that says both halves — "answers from the size recorded at
  upload; an object written before that record existed is re-read to count its bytes, so a
  damaged legacy object is found (`Corrupt`) at O(size)".

### R-W1-306 [MINOR] The refusal set has three different sizes in three records
- **Location:** `docs/adr/0030-deployment-image-contract.md:421` and `:518`;
  `docs/agent-ops/20261022-w6-w7-w9/reports/w1-improve/W1-IMPR-02-T-003.md` §7 row 7;
  `docs/agent-ops/20261022-w6-w7-w9/COMMS.md:78`.
- **Evidence:** `[RAN]` the corpus at `tests/deployment/test_fetch_images.py:489`
  (`_REFUSED_SHAPES`) holds **12** shapes and **20** spellings at HEAD. The growth claim is
  real: the same count at `954705d` is **7 shapes / 7 spellings**, and at `257b80f` it is
  **12 / 20**. The records say "Ten shapes" (twice) and "11 shapes / 20 spellings" (twice).
  The ADR's own bullet list is neither: it enumerates nine prose bullets that cover the twelve
  shapes (the bullet "a YAML anchor or an alias" covers three of them).
- **Why it matters:** the count *is* the discipline — ADR-0030's own argument is that the
  refusal set is a list of **shapes**, each with several spellings, and a count that does not
  match the corpus is a number nobody can check. This is the same class as R-W1-111/212 in the
  wave-1 review.
- **Recommended fix:** derive the figure instead of typing it — the corpus's own length, or a
  sentence with no number in it ("every shape the corpus carries").

### R-W1-307 [MINOR] The script's own header no longer describes the rows it emits
- **Location:** `deploy/fetch-images.sh:266-271` (the header), `:781` (`action="skip"`),
  `:988-989` (the printed bracket).
- **Evidence:** `[VERIFIED deploy/fetch-images.sh:267-269]` "The output is one tab-separated row
  per service: `action<TAB>image<TAB>service<TAB>profiles`, **where the action is "pull" or
  "build"**" — but the plan now carries a third action, `skip` (`:781`), and the printed row
  for it ends in `[behind a compose profile that is not enabled: …]` (`:988-989`). The
  pre-change header did mention the skip row; the rewritten header dropped it.
- **Why it matters:** the header is the file's contract with its reader, and the reader (the
  test's `_LIST_ROW` at `tests/deployment/test_fetch_images.py:315`) has already been taught
  the new shape — the header is the part that is behind. `docs/deployment/docker.md:423-427`
  describes `--list`'s gate behaviour but not its row format, so there is no second record.
- **Recommended fix:** name the three actions and the bracket in the header, and state in
  `docs/deployment/docker.md` that a `skip` row carries the profile names in a trailing
  bracket (the improvement's own residual risk #5 says the format changed; the durable records
  did not follow).

### R-W1-308 [MINOR] R-W1-216's second half is still open at HEAD, and its NEED is unanswered
- **Location:** `tests/conventions/test_import_conventions.py:622-627`.
- **Evidence:** `[VERIFIED …:622-627]` the aggregate import probe passes `timeout=300` and the
  `subprocess.run` call is **not** wrapped in `except subprocess.TimeoutExpired`, so a timeout
  surfaces as an error rather than as a verdict — the finding as filed
  (`reports/w1-review/W1-TREV-03-tests.md:504-515`). `[VERIFIED COMMS.md:72]` the NEED was
  posted at 2026-10-21T23:15:53Z; `[VERIFIED COMMS.md:68]` W1-IMPR-03 — which owned and edited
  that very file in this wave (`a3e6819`) — closed its task without answering it.
- **Why it matters:** a hung import probe is exactly the failure the timeout was added to
  catch, and it now reports as an infrastructure error instead of a verdict. The wave had both
  halves in hand and answered only one.
- **Recommended fix:** catch `TimeoutExpired` and fail with a message naming the timeout
  (three lines, in a file the wave already edited).

### R-W1-309 [MINOR] R-W1-112's classification is still only in a run-scoped report, and the plan's figures are still wrong
- **Location:** `docs/plans/w6-w7-w9.md:66-70`; the classification itself is in
  `reports/w1-improve/W1-IMPR-03-T-004.md` §7.
- **Evidence:** `[VERIFIED COMMS.md:64]` the NEED was posted at 2026-10-21T22:54:02Z and no
  answer arrived. `[VERIFIED docs/plans/w6-w7-w9.md:66]` the plan still says "**48** `if
  TYPE_CHECKING:` blocks … **nine** are load-bearing"; `[RAN]` the measurement at HEAD is **33**
  blocks in 33 files, and exactly **one** is load-bearing for a real cycle
  (`src/paa/storage/coldlake/cas.py:62`) — the figure the improvement already published
  ([VERIFIED COMMS.md:65]).
- **Why it matters:** the plan is the record a later reader will find; it carries numbers
  that are wrong by 15 blocks and by eight cycles. The classification the plan asked for is
  real and correct, and it is in a directory named for this run, which the next run will not
  read.
- **Recommended fix:** either correct the plan's two figures, or take up the offer in the NEED
  and put a *checkable* table in `tests/conventions/test_import_conventions.py` beside
  `DEFERRED` — the file the wave already owned.

## 5. Attempts to break it

Concrete inputs tried, with the outcome:

1. **A service whose keys are at three spaces** (Compose-valid) → dropped silently, unpinned
   image unchecked, **exit 0**. The pin gate is bypassed. [R-W1-301]
2. **The same file at five and six spaces** → refused with the right message, exit 1. The
   refusal works; it is one-sided. [R-W1-301]
3. **The same file at one and two spaces** → Compose itself refuses the file (invalid YAML;
   "services.image must be a mapping"), so by ADR-0030's own line this is not a hazard — but
   the script still exits 0 having dropped the service. [not a finding; recorded]
4. **The pre-change script on the same three-space file** → identical behaviour (dropped,
   exit 0), so R-W1-301 is a half-closed hole, not a regression. [R-W1-301]
5. **A `put` whose driver error is a bodyless 404** → one extra `head_bucket` request and a
   `NotFound("bucket not found")` with `namespace='bucket'`. The write path probes. [R-W1-302]
6. **A driver error carrying a status 404 and no `Error.Code`** → the read path and the write
   path give different answers for the same condition, and the write path carries no namespace
   detail. [R-W1-303]
7. **A probe failure whose cause carries a 404** → `RecursionError` after 98 probes, escaping
   raw. [R-W1-304]
8. **A legacy S3 object with a damaged frame and no recorded size** → `Corrupt`, which is what
   the protocol docstring says cannot happen. [R-W1-305]
9. **Mutating the S3 code table** (`NoSuchBucket` → `NoSuchBucketXX`) → 2 test failures: the
   tables are pinned by their members against literals, so a code change is caught.
10. **Reverting `src/paa/storage/relational/__init__.py`** → `typing.get_type_hints(
    get_database)` raises `NameError` and 2 tests fail: the import-lock fix is load-bearing.
11. **The repository's own merged stack** (`docker-compose.yml` + gvisor) → 6 rows, exit 0. No
    regression for the deployment this repository actually ships.
12. **Usage errors and a missing compose file** → exit 2 / exit 2 / exit 2 / exit 1, identical
    at 3ac9d6b. No change.

## 6. Dimension checklist

| Dimension | Verdict | Notes |
|---|---|---|
| Correctness | **MIXED** | The three improvements close their own findings (measured). One new rule is half-applied (R-W1-301); two classifiers disagree (R-W1-303). |
| Root cause vs symptom | **MIXED** | T-001 and T-004 changed the code, not the symptom (the probe is at the boundary; `get_type_hints` resolves). T-003's indent refusal is a symptom-level patch: the reader's "cannot see" test is the wrong predicate. |
| Regression / compatibility | **OK** | The repo's own stack still plans (exit 0); exit codes unchanged; the FS store's answers unchanged. R-W1-301 is pre-existing behaviour, not a regression. |
| Concurrency / idempotency | **OK** | The probe is not cached, by design (ADR-0032:317-320); no new shared state. The re-entrancy in R-W1-304 is within one call. |
| Error handling | **MIXED** | The vocabulary is applied at the boundary and the `__cause__` chain is correct (`[RAN]` `__cause__ is self? False` in all three probe cases). But a `RecursionError` and a wrong-condition `NotFound` still leave the substrate (R-W1-303, R-W1-304). |
| Security / abuse | **OK** | The reader never interpolates a path into the `awk` program; the pin gate is the security control and it is bypassable in one construct (R-W1-301). The blob side gains no new trust boundary. |
| Performance / scalability | **OK** | One extra request on the miss path and on the ambiguous write-failure path; nothing on the success path. The O(n²) row accumulation is bounded by a compose file. |
| Migration / rollback | **OK** | Both reversals fail loudly (26 and 12), so a revert is caught by the suite rather than discovered in a deployment. |
| Observability | **MIXED** | The refusals name the file and the shape; the profile skip is reported on stderr and in the report. The `--list` row format changed and the durable records did not follow (R-W1-307). |
| Maintainability / conventions | **MIXED** | Docstrings carry the "why" throughout, as the house requires. Four records now state something the code contradicts (R-W1-302, R-W1-305) or a number that is wrong (R-W1-306). No `# noqa`, no `type: ignore`, no relaxed rule: the gate stays zero. |
| Docs impact | **MIXED** | ADR-0030 and ADR-0032 both needed amendments and both got partial ones; ADR-0032's §1 rule is not yet true on the write path (R-W1-303). |

## 7. Optimization and simplification opportunities

| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | `deploy/fetch-images.sh:444-448` + `tests/deployment/test_fetch_images.py:883-895` | R-W1-301 | The pin gate stops being bypassable by a three-space service; the ADR's claim becomes true | S (one predicate in each reader + one corpus spelling) | Low — a refusal is loud, never silent | **DO NOW** |
| 2 | `minio_backend.py:357-367`, `:604-656` | R-W1-303 | One classifier for "the answer does not say which absence" — the read and write paths answer alike, and ADR-0032 §1's rule holds | S | Low | **DO NOW** |
| 3 | `minio_backend.py:649-655`, `:704` | R-W1-304 | A probe failure is a `Unreachable`, not a `RecursionError` | S (one re-entrancy flag) | Low | **DO NOW** |
| 4 | ADR-0030, ADR-0032, `base.py:194-196`, `COMMS.md:69/73/78` | R-W1-302, R-W1-305, R-W1-306 | The durable records stop stating things the code contradicts | S (prose) | None | **DO LATER** (same edit pass as R-W1-301/303/304) |
| 5 | `tests/conventions/test_import_conventions.py:622`, `docs/plans/w6-w7-w9.md:66` | R-W1-308, R-W1-309 | Two open deferrals close | S each, but both need an orchestrator decision | None | **DO LATER** |
| 6 | Making `--list` compare against real `docker compose config` | ADR-0030:533-537 | Closes the last reader gap | L, and it makes `--list` require the Docker CLI — which the ADR already rejects for a good reason | Medium | **NOT WORTH IT** (already decided, correctly, in ADR-0030) |

## 8. Cross-agent / cross-task interactions observed

1. **Two NEEDs, both unanswered, both still open.** `COMMS.md:64` (R-W1-112's durable home,
   22:54:02Z) and `COMMS.md:72` (R-W1-216's second half, 23:15:53Z). The second one was raised
   *because* the file was another agent's write-set — and that agent (`W1-IMPR-03`) was editing
   the same file in the same wave and closed its task without answering. Both findings are
   therefore carried into the next wave as open (R-W1-308, R-W1-309).
2. **The improvement reports and the durable records disagree.** `W1-IMPR-01` §1.1 carries the
   accurate statement about the write path, with a parenthetical that is exactly right; ADR-0032
   and the protocol docstring carry the unqualified version. The accuracy lives in the
   run-scoped record, which the next run will not read (R-W1-302, R-W1-305).
3. **COMMS.md repeats two false statements** (`never on a write` at `:69`; `does not read the
   blob` at `:73`) and one wrong figure (`:78`). COMMS is the trail the orchestrator reads to
   decide what is left, so a false line there propagates.
4. **The T-003 change was never reviewed by a second pair of eyes** (`COMMS.md:76`: every
   subagent spawn failed with "Subagent depth limit reached (1)"). The change is large — 580
   lines in the script and 727 in its test — and the one MAJOR finding here (R-W1-301) sits in
   exactly the part that was added last and least exercised.
5. **The deploy improvement's own residual-risk list already flagged the row-format change**
   (`W1-IMPR-02` §6.5) but the durable records were not updated to match (R-W1-307). The risk
   was known and the follow-through did not happen.
6. **The corpus lock and the script share a blind spot** (R-W1-301): both readers use
   `indent > 4`, so the lock confirms the script rather than checking it. The parity lock is a
   lock between two hand-rolled readers — ADR-0030:533-537 says so — and this is the shape of
   hole that two agreeing readers cannot see.

## 9. What I could not verify

- **The full suite was not run** — out of scope by the brief. The figures I can stand behind
  are the scoped ones: 169 + 106 + 13 passed, ruff 0, mypy 0 over 137 source files. The
  repository-wide totals (and the 232 skipped storage cases) are `[UNKNOWN]` to me.
- **No live S3 server and no real botocore.** `boto3` is an optional extra and was not
  installed. R-W1-303 and R-W1-304 are therefore reachable only through a stub shaped like the
  driver, and I have said so in each finding; whether a real server or a real botocore can
  produce those exact shapes is `[UNKNOWN]`. The wave-2 live suite is where that belongs.
- **No container was started, stopped or pruned**, and no daemon was required by any command
  I ran. `docker compose config` was used only to *parse* files (v5.6.0), never to run a
  stack.
- **The wave-2 work was not reviewed** — the next wave's live-S3 suite, and anything T-005
  onward, are outside this diff.
- **The two deferrals were not resolved**, because answering them needs an orchestrator
  decision rather than a code change; they are reported as open (R-W1-308, R-W1-309) with the
  NEEDs cited.
- **`docs/deployment/docker.md` and `README.md` were read for the row format only**; I did not
  audit the deployment documentation as a whole.
