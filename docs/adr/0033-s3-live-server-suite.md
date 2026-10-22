# ADR-0033: The S3 substrate is tested against two S3 servers, not against a mock

## Status

Accepted

## Context

Every test of the S3 path ran against `StubS3Client` — a duck-typed fake in
`tests/storage/test_minio_backend.py` with no `head_bucket`, no
`create_bucket` and no `list_objects_v2`. Twenty-three tests, one stub, and
the stub was shaped like the code rather than like the server.

That is a good proof of the **mechanism** and a proof of nothing at all
about the **table**. ADR-0032 gave the blob substrate a neutral error
vocabulary and a classifier that reads the driver's answer structurally —
its error code, its HTTP status, the class names off the exception's MRO.
The unit suite proves the classifier reads the shape it was written against.
It cannot prove that the **codes a server actually answers with** are the
codes the table names, because the stub's codes were chosen by the same
hand that wrote the table.

The consequences were real and they were invisible:

* **Six raw-exception escape sites** in `src/paa/storage/coldlake/minio_backend.py`
  — the client construction, the bucket check, the bucket creation, the
  object write, the body read and the streaming read. A stub cannot raise
  a driver's connection error, so none of the six was reachable from a test.
* **The `put` / `put_stream` divergence** — the same condition (a missing
  bucket) arrived as a raw `ClientError` from one write path and as a
  generic `StorageError` from the other, so no caller could write a correct
  `except` clause at all.
* **The bucket-level absence was assumed, not earned.** A HEAD carries no
  body, so the driver reports the status as the code and a HEAD on a
  missing *key* and a HEAD on a missing *bucket* are the same 404. The
  store learned to ask the server about the bucket instead — and nothing
  tested that against a server that could answer.

ADR-0032 said, in its own closing paragraph, that the live suite was
"the only way to know that the codes the servers answer with are the codes
in the table", and left it to the next wave. This is that wave.

## Decision

**The S3 substrate is tested against two S3 servers — SeaweedFS (the
deployment's) and RustFS (a different implementation) — and the same test
body runs against both.**

**What "two servers" is worth, and what it is not.** The independence of the
two is the **operator's choice of labels**: nothing in the environment says
which programs the two endpoints are, and this suite does not establish
independence. What it does establish is that the two **answer differently** —
`test_the_two_servers_are_distinguishable` reads what each server says about
itself and fails if the two answers are the same, so two containers of one
image cannot pass it. The labels name the implementations the operator
pointed the suite at; the suite checks that the two are not the same one.
(The title used to say "two **independent** S3 servers", which is the claim
the suite cannot make; it is narrowed here to the claim it can.)

The point is not "does S3 work". The point is: **does *this client* work
against a server it was not written against?** One server cannot answer
that question, because a client and a server written by the same mind
agree about everything that matters — including the things that are wrong.

### 1. The suite is a suite of servers, not a suite of buckets

The environment names a **server**; the fixture makes a **bucket** of its
own on it. This is the house convention, the one `PAA_TEST_POSTGRES_DSN`
established: the variable names a server, and the test makes a resource of
its own on it, so a run can never write to what a deployment is using,
whatever the endpoint points at.

```
PAA_TEST_S3_ENDPOINT        the server to run against
PAA_TEST_S3_ACCESS_KEY      its access key
PAA_TEST_S3_SECRET_KEY      its secret key
PAA_TEST_S3_BUCKET          the BASE name of the test's bucket
```

The bucket is `paa-s3-test-{pid}-{uuid8}`. The pid so two runs on one host
cannot collide, the uuid so two runs in one process cannot either, and the
base name so the operator can see at a glance what the run left behind.

A **second** server is named the same way with a label in front of the last
word — `PAA_TEST_S3_SEAWEEDFS_ENDPOINT`, `PAA_TEST_S3_RUSTFS_ENDPOINT` —
so the same four names describe any number of implementations. The
unlabelled spelling is the label-less member of that one rule rather than a
second convention, and the label is what a failing test id names, which is
the whole reason for running the same body twice: **a difference between
two implementations has to be visible, and a difference that only shows up
in one implementation's output is invisible without it.**

The bucket name is spelled with **hyphens**, and that is not a style
choice. S3's own naming rules forbid underscores, and **both** servers
enforce them: a name carrying an underscore is refused with
`InvalidBucketName` before the bucket exists — on SeaweedFS *and* on
RustFS, measured, not assumed.

### 2. The suite skips, and a skip is not a failure

With no `PAA_TEST_S3_*` set, the module skips. With no `boto3` installed,
the module skips. A test that fails because there is no server fails for
the wrong reason, and a suite that skips loudly and for a stated reason is
telling the truth about what it did not cover.

The `requires_s3` marker is registered, and `paa[s3]` joins the `dev`
extra, on the argument ADR-0032's sibling decision made for `paa[api]`: a
documented `pip install -e ".[dev]"` must not produce an environment in
which the gate passes while the S3 path is untested. The `dev` extra
resolving `paa[s3]` is what makes the skip mean "no server" rather than
"no driver".

### 3. The `BlobStore` contract did not grow a list operation

The contract has no list operation and still has none (decision D-6 of
the plan). Enumeration is not part of a content-addressed store's
contract. The one assertion that needs to see the raw object uses a **raw
boto3 client**, and the contract-level assertions use `exists()` /
`stat()` / `delete()`. ADR-0032 records the asymmetry.

### 4. Two claims are observed in the **request**, not in the answer

Two of the store's claims are invisible from the server's answers, and the
suite says so rather than pretending otherwise:

* **The dedup is a no-op.** Two identical payloads share one key, so a
  second *write* would land on the very same object and the bucket's
  listing would look identical either way. The listing proves the
  *convergence* claim and is blind to the *no-op* one. The suite observes
  the **request** through a spy in front of a real client, over a real
  server, and asserts that the second put looked before it wrote and did
  not write.
* **`stat` answers from the head.** The store has a fallback — an object
  written before the raw-size record existed is re-read to count its bytes
  — and the fallback *hides* a broken metadata round-trip, because the
  size comes out right either way. The suite asserts that `stat` did not
  download the body.

The spy is a wrapper around a **real** boto3 client, not a fake: every
request reaches the server and every answer comes back unchanged.

## What the two servers proved different from each other

Everything below was measured against both servers, on the same run, with
the same test bodies. The full table is in the implementation report.

### The one behavioural difference: a write to a missing bucket

| | SeaweedFS | RustFS |
|---|---|---|
| `put_object` to a bucket that does not exist | **succeeds**, and creates the bucket | **refused**: `NoSuchBucket` (404) |

This is a real difference between two implementations of the same
protocol, and it is a difference about *the server*, not about the client.
The suite does not paper over it and does not skip a branch: it asserts
the invariant that holds on both, which is that **both write paths answer
alike** — `NotFound` with `namespace="bucket"` on a server that refuses,
and a blob that is really there on a server that creates. The ADR-0032
divergence is closed on the server that refuses, and is a no-op on the
server that creates.

### The error-code surface: identical

Every code either server answers with is already in ADR-0032's table:

| request | code (both servers) | in the table as |
|---|---|---|
| GET a missing key | `NoSuchKey` | an object-level absence |
| GET / DELETE / list a missing bucket | `NoSuchBucket` | a bucket-level absence |
| HEAD a missing key or bucket | `404` (the bodyless status, reported as the code) | an ambiguous absence, resolved by the bucket probe |
| HEAD with wrong credentials | `403` (bodyless) | a refusal |
| PUT with wrong credentials | `InvalidAccessKeyId` | a refusal |
| PUT to a missing bucket (RustFS) | `NoSuchBucket` | a bucket-level absence |
| DELETE a missing key | *no error* (204) | — |
| a bucket name with an underscore | `InvalidBucketName` | not named; see below |

Two of those are worth a note.

**`InvalidBucketName` is not in the table.** It cannot be: it is a code
about a *name*, and the store never invents a bucket name — the name comes
from the deployment's configuration. If it ever were raised, the status
(400) is in none of the table's buckets and the answer would be the
unclassified `BlobFailure`, which is the honest answer for "the request
was refused and this vocabulary does not name the reason".

**`UnauthorizedAccess` is not in the table either** — RustFS's answer to an
empty access key, where SeaweedFS answers `InvalidAccessKeyId`. The
*status* is 403 on both, and the status fallback is what classifies the
refusal. So the table is deliberately small and the classification does
not depend on it being complete: a code the table does not name is still
classified when the **status** names the judgement.

### The unreachable answer is covered twice

A connection failure carries no code and no status, so the class names of
the exception are the only signal — and they are the signal the code
reads off the exception's **whole MRO**, not off its own class. The
unwrapped cause of a refused connection is the `ConnectionRefusedError`
itself, and the MRO of that carries the builtin name `ConnectionError`,
which the table names. The **errno** path covers the same case a second
time (`ECONNREFUSED`).

Breaking either half alone leaves the answer right; breaking both turns
it into the unclassified failure. The suite catches the answer being
wrong, which is the right granularity — and it is worth recording that
the answer has two independent supports, because a suite that asserted
*one* of them would be asserting an implementation detail.

### What ADR-0032's decision 4 got wrong, and what it says now

ADR-0032's decision 4 **used to** say that boto3's transfer manager "catches
the driver's `ClientError` and raises an `S3UploadFailedError` instead", and
that the translation unwraps it. That was wrong about the *mechanism*, and
ADR-0032 was corrected on 2026-10-22 (see its Amendment). This section is
the measurement that correction rests on, and it now agrees with the
corrected decision.

**For `upload_fileobj` — the call `put_stream` makes — it does not wrap.**
Measured against both servers, on boto3 1.43.117:

| call | raises | carries a response of its own | `__cause__` | `__context__` |
|---|---|---|---|---|
| `upload_fileobj` (single-part and multipart) | the driver's `ClientError` (`NoSuchBucket`) | **yes** | unset | unset |
| `upload_file` (the filename variant) | `S3UploadFailedError` | **no** | unset | **the driver's `ClientError`** |

So on the path the store actually takes, there is **no wrapper to unwrap**:
the raw `ClientError` carries the answer, and the store reads it directly.
The *outcome* is the one ADR-0032 wanted — one answer from both write
paths — but it is reached by a different route than the old record
described.

The wrapper is not a fiction, though: it is what `upload_file` produces, and
it is **not empty**. It carries no response *of its own* and no `__cause__`,
but boto3 raises it inside an `except ClientError` block and without a
`from`, so the driver's error is the wrapper's implicit `__context__` — and
`minio_backend.py::_driver_cause` reads the `__context__` as well as the
`__cause__`. The store's answer on the wrapping route is therefore **the
same** answer, not a degraded one. `upload_fileobj` — the call `put_stream`
makes — does not wrap at all.

The suite pins it: `tests/storage/test_s3_live.py::test_the_wrapping_upload_route_answers_alike`
runs the wrapping route against a real server, with the driver's own
exceptions, and asserts that the two routes answer alike.

*(Corrected 2026-10-22, by W2-FIX-02: this section used to say that the
wrapper carried no cause and was therefore "opaque — no response, no cause,
and therefore nothing for the translation to read", and that the answer
"degrades from `NotFound` to the unclassified `BlobFailure`". Measured on
both servers, the wrapper carries the driver's error as its implicit
`__context__` and the store answers alike — `NotFound`, `namespace="bucket"`,
`s3_code="NoSuchBucket"` — which is what ADR-0032's corrected decision 4 now
says. See the Amendment at the end of ADR-0032.)*

**The record was wrong; the code is not.** The unwrapping machinery is
still load-bearing — for the connection-error chain, where the cause is
the raw `ConnectionRefusedError` — and the translation is correct on the
path the store takes. What ADR-0032's decision 4 mis-stated was the
*mechanism*, and a maintainer who believed it would not have thought to
check whether the wrapper it describes is ever produced. That check is
this ADR's, and the answer is: not on this path, in this boto3.

## The honest limits of the suite

**It is not a conformance test of S3.** It does not check that the
servers implement the specification; it checks that **this client**
answers the right thing on top of them. A server that is wrong in a way
this client never asks about is not caught, and a client that is wrong in
a way these servers happen to agree with is not caught either.

**It covers the operations the cold lake performs, and no more.** `put`,
`put_stream`, `get`, `get_stream`, `exists`, `stat`, `delete`. Not
listing, not copying, not multipart uploads of the store's own, not
range reads, not presigned URLs, not lifecycle rules, not versioning, not
encryption, not replication. The client uses nine S3 operations in all;
those nine are covered.

**Two implementations are not a population.** Two servers agreeing proves
that the client's answers are not an artefact of one server's habits. It
does not prove they are right. A third implementation would be a third
data point, not a proof.

**The suite is skipped, not run, on a developer laptop.** That is the
honest state of it, and the `dev` extra is what makes the *skip* mean
"no server" rather than "no driver". A CI job that runs the suite against
both servers is the way to make it a gate; the suite is written to be
pointed at one, and the marker and the env convention are how.

## Consequences

**What it costs.**

* Two servers to run, and the suite skips without them.
* A spy in front of a real client for the two claims that are invisible
  in the answers. It is a wrapper, not a fake, and it is confined to the
  two tests that need it.
* A second bucket-naming rule to remember (hyphens), and the reason is
  written down.

**What it buys.**

* The error-code table is now checked against two servers, and the codes
  they answer with are in it.
* The `put` / `put_stream` divergence is closed against a real driver,
  not against a stub shaped like one.
* The bucket-level absence is *earned* by a probe against a server that
  can answer it, on both implementations.
* The per-server difference in "a write to a missing bucket" is now
  recorded, with the invariant that holds across both.

**What it would take to move back.** Delete the file and the fixture, and
the S3 path goes back to being tested only against a stub. The stub stays
for the *unit* tests either way — it is a good proof of the mechanism and
a fast one — and this suite is the thing that would have caught the six
raw-escape sites.

**What was deliberately *not* done.**

* **No list operation was added to the `BlobStore` contract** (D-6). The
  raw client is used for the one assertion that needs it, and the
  asymmetry is recorded in ADR-0032.
* **No defect in `src/paa/storage/coldlake/**` was found, so none was
  fixed and none is deferred.** The live suite ran green against both
  servers on the first run of the final form. The one thing it *did*
  find is a mis-statement in ADR-0032's decision 4, which is recorded
  above and reported as a finding; ADR-0032 is wave 1's record and is
  not this wave's to edit.
* **No error was added to the vocabulary.** The two servers answered with
  codes the vocabulary already names, or with codes whose *status* names
  the judgement, so the vocabulary did not grow.
* **The stub was not removed.** It is the right tool for the mechanism,
  and this suite is the right tool for the table.
