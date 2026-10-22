# ADR-0032: The blob substrate's failures are named in a vocabulary of their own

## Status

Accepted

## Context

ADR-0025 established the rule for the relational substrates: **the translation
happens at the substrate boundary, once, and the repository layer catches a
*concept* rather than a driver's exception.** The blob substrate never got one.

There was no `storage/errors.py` — the only neutral vocabulary in the tree was
`storage/relational/errors.py`, and it is built from relational concepts
(`UniqueViolation`, `SerializationFailure`, `SchemaVersionError`, …) that cannot
express "absent", "forbidden", "unreachable" or "corrupt". So the S3 path raised
the generic `paa.core.errors.StorageError` with a free-text message and **let the
driver's exception escape** from six sites in
`src/paa/storage/coldlake/minio_backend.py`: the client construction, the
bucket check, the bucket creation, the object write, the body read and the
streaming read.

The consequence was not cosmetic. **The same condition produced two different
exception types depending on which method the caller used.** A missing bucket
reached the caller as a raw `botocore.exceptions.ClientError` from `put()` —
which writes through `put_object`, and re-raised the driver's own error
rather than naming it — and as a generic `StorageError` from `put_stream()`,
which writes through `upload_fileobj` and turned every failure into a
free-text message. A caller could not write a correct `except` clause at
all, and a security-relevant judgement — *is this blob absent, or is the
store refusing?* — was unanswerable.

*(Corrected 2026-10-22, by W2-IMPR-05: this paragraph used to say that
`put_stream` "is therefore handed boto3's `S3UploadFailedError` wrapper".
That was wrong about the mechanism — `upload_fileobj` does **not** wrap; the
divergence came from the two write paths translating the same condition
differently. The wrapper is boto3's `upload_file` wrapper, and decision 4
below is about it. See the Amendment at the end of this record.)*

The filesystem substrate had residue of its own: a raw `OSError` out of the
streaming read, and a raw `zstandard.ZstdError` out of a corrupt frame.

And the `BlobStore` protocol **declared no error behaviour at all**. That is
the reason the divergence went unnoticed: a contract that is silent about
failure cannot be violated by it.

`botocore` is not installed on a laptop, is not in the `dev` extra, and must
never be imported at module scope — the substrate is an optional extra
(`paa[s3]`) and the package has to stay importable without it. So the
translation has to recognise the driver's errors **structurally**.

## Decision

**The blob substrate gets a vocabulary of its own, and the translation happens
at the boundary, once.**

### 1. One vocabulary, in `storage/coldlake/errors.py`

The blob analogue of `storage/relational/errors.py`. Every member is a
`paa.core.errors.StorageError` — so every existing `except StorageError` at the
edges keeps catching what it caught before — and every member carries the
`substrate` that produced it. The members are the judgements a caller actually
has to make:

- **`NotFound`** — the addressed thing is not there. A missing object and a
  missing *bucket* are the same judgement, and the `namespace="bucket"`
  detail says which (amended 2026-10-22 — see the Amendment at the end of
  this record).
- **`AccessDenied`** — the substrate refused: wrong credentials, or not
  permitted. **Not** retryable.
- **`Unreachable`** — the substrate could not be reached, or could not complete
  the request: a refused connection, a timeout, a throttling response, a
  server-side error, a connection that died half-way through a body.
  **Retryable.**
- **`Corrupt`** — the bytes are not what the address says: a hash that does not
  match, a frame that is not a frame. Damage, not connectivity.
- **`NotSupported`** — the substrate cannot serve the request at all: the `s3`
  extra is not installed, or the deployment selected a substrate this build
  cannot construct. Deliberately **not** a subclass of `Unreachable` — an
  unreachable substrate may come back and a missing extra will not.
- **`MalformedRequest`** — the request was refused as malformed: a digest that
  is not a digest, a size hint that disagrees with the bytes. The caller's
  mistake, and it will fail identically on a retry. The relational seam made
  the same call for the same reason (`ParameterCountMismatch` is "a bug in this
  codebase's SQL rather than anything the engine or the data did").
- **`BlobFailure`** — the substrate failed and the reason is not one this
  vocabulary names. The fallback. Deliberately a **sibling** of the named
  concepts rather than their parent: a caller that retries on `Unreachable` must
  not be told to retry on a failure whose reason nobody knows, and a caller
  that treats `Corrupt` as archive damage must not treat this as damage.

`BlobError` is the base of the family and is **never raised directly** — the
same arrangement as `relational/errors.py::ConstraintViolation`, a grouping base
whose subclasses do the naming. The base exists so that a caller who wants
"anything the blob substrate said" has one name to catch, and so that the
translation can recognise an error that is already ours without enumerating the
members.

### 2. The translation happens at the boundary, and it is idempotent

`storage/coldlake/errors.py::translate_blob_error` is the blob analogue of
`relational/connection.py::translate_sqlite_error`. It:

- **returns an error that is already ours unchanged** — the boundaries nest, and
  without the guard a specific failure would be flattened into a vaguer one on
  its way out;
- **does not swallow a `PaaError` that is not ours** — a `LedgerError` stays a
  `LedgerError` and a domain error stays a domain error, exactly as ADR-0025
  requires;
- turns a codec error into `Corrupt` and an `OSError` into the concept its
  errno implies (`ENOENT` → `NotFound`, `EACCES` → `AccessDenied`, a failed
  connection → `Unreachable`);
- and reports anything it cannot classify as `BlobFailure` rather than letting
  it escape, because a raw exception reaching a caller breaks the contract just
  as surely as a misclassified one does.

The S3 dialect — the driver's error codes — lives beside the S3 client in
`minio_backend.py`, for the same reason ADR-0025's SQL dialects live beside
their engines. `minio_backend.py::_translate_s3_error` is its translator, and
it delegates to the shared one for everything it does not name.

### 3. The driver is recognised structurally, and never imported

The classifier reads three signals, in this order: the driver's **error code**
(`response["Error"]["Code"]`), the **HTTP status**
(`response["ResponseMetadata"]["HTTPStatusCode"]`), and the exception's
**class names, read off its MRO**.

The MRO is what `isinstance` consults; only the *names* are read. That is what
makes `IncompleteReadError` recognisable as the `ResponseStreamingError` it
inherits from, without importing `botocore` and without hard-coding the
hierarchy. The class names are also the only signal a *connection* failure
carries: `EndpointConnectionError` and `NoCredentialsError` have no response at
all.

**The driver is not imported, at module scope or lazily.** A lazy
`import botocore.exceptions` inside the classifier was considered and rejected:
it would make the classification *depend on whether the driver is installed*, so
the same exception would be classified by two different code paths in the unit
suite (no botocore) and in the live suite (botocore present). The unit suite
would not then be exercising the code the deployment runs. The structural
classifier is the same code in both, and it recognises a duck-typed stub as
readily as the real thing — which is what lets the unit suite test the failure
modes without `boto3` installed at all.

### 4. boto3's upload wrapper is unwrapped, so the two write paths agree

`put()` writes through `put_object` and `put_stream()` through
`upload_fileobj`. **`upload_fileobj` does not wrap.** It propagates the
driver's `ClientError` with its response intact, so the store reads the
answer directly.

The **other** upload entry point does wrap. boto3's `upload_file` — the
*filename* variant — catches the driver's `ClientError` and raises an
`S3UploadFailedError` instead, and it does so **without chaining it
explicitly**: the wrapper's `__cause__` is unset, and the driver's error is
the wrapper's implicit `__context__`.

The translation follows that wrapper (bounded, and only when the cause
*looks* like a driver's error), and it follows the `__context__` as well as
the `__cause__`, because that is where the driver put it. So a missing
bucket is one answer from every path rather than a raw `ClientError` from
one and a generic error from the other. The same unwrap makes every *other*
code agree between the two paths, not just the missing bucket.

**The wrapper is not opaque.** It carries no response *of its own* and no
`__cause__`, but the driver's error is its `__context__`, and the
translation reads it there. A reader who took the wrapper for an empty box
would delete the unwrapping — and the deletion would be invisible on the
path the store takes today, because `upload_fileobj` never wraps, so the
*other* path's answer would quietly become the unclassified failure. The
live suite pins the answer under both shapes
(`tests/storage/test_s3_live.py::test_the_wrapping_upload_route_answers_alike`),
against a real server and with the driver's own exceptions.

*(Corrected 2026-10-22, by W2-IMPR-05: this decision used to say that
"boto3's transfer manager catches the driver's `ClientError` and raises an
`S3UploadFailedError` instead", which is true of `upload_file` and false of
the `upload_fileobj` the store actually calls. See the Amendment at the end
of this record.)*

### 5. A missing bucket raises, from every method

A missing **object** is an ordinary answer — the blob is not stored, so
`exists()` says `False` and `stat()` says `None`. A missing **bucket** is not:
the store cannot answer the question at all, and answering "no" would be a lie
that a daemon would act on. So the bucket-level absence raises `NotFound` from
every method, and it raises it from `_head()` — the one place every method goes
through. That is what makes the `put`/`put_stream` divergence disappear **at the
source** rather than at two call sites: both writes fail in `_locate()` →
`_head()`, so they cannot disagree.

The cost is recorded: `exists()`, `stat()` and `delete()` raise `NotFound` on a
missing bucket where they used to return `False`/`None`/`False`. That is a
behaviour change, and it is the honest one.

**Amended 2026-10-22** — the *how* changed, and the Amendment at the end of
this record says how. A HEAD response carries no body, so the driver has no
code to report and reports the status as the code: a HEAD's 404 is the same
404 whether the key or the bucket is missing, and the bucket-level code lives
in a body a HEAD never has. The discrimination is therefore now **earned** by
a `head_bucket` probe rather than assumed from a code the HEAD cannot carry.
The decision above — a missing bucket raises, from every method — stands, and
the code now does it.

### 6. The message names the concept; the driver's wording is the cause

`PaaError.to_payload()` merges an error's `details` **verbatim** into a ledger
payload, and the message with them. The details therefore carry **identifiers**
only — `key`, `sha256`, `bucket`, `path`, `errno`, `s3_code`, `http_status`,
`driver_error` — and never text that could carry a credential. The driver's own
wording is **not** in the message either: botocore's connection errors quote
the **endpoint URL**, which an operator may have written with credentials in
it, and the endpoint URL has no business being in the ledger. The wording is
the `__cause__`, where it stays available to whoever reads the traceback.

This is stricter than the code it replaces, which interpolated the driver's text
into the message, and stricter than `translate_sqlite_error`, which does the
same. The cost is recorded: `str(exc)` no longer contains the driver's text, so
the ledger records the *concept* and the *identifiers* rather than a free-text
message.

A caller-supplied value that is rejected is echoed back only up to a bound: a
digest is caller-supplied, and a caller-supplied value has no business being
an unbounded string in a ledger payload.

### 7. The contract declares its error behaviour

`BlobStore`'s docstring now states what each method raises, and
`tests/storage/test_blobstore_contract.py` checks that the two implementations
agree with it — the same condition, the same class, on either substrate, and a
test that every method's docstring names at least one member of the vocabulary.
A contract that is silent about failure is what made the divergence invisible,
so the silence is the thing this decision removes.

## Consequences

**What this costs.**

- A second error vocabulary in the tree. It is not the second half of one: the
  relational vocabulary is about *statements and constraints*, and this one is
  about *objects and substrates*. Neither can express the other.
- The vocabulary is a compatibility surface. Every member is a `StorageError`,
  so `except StorageError` keeps working — but a caller that starts catching
  `NotFound` is depending on a class that did not exist before, and the ADR is
  what makes that dependency safe to take.
- The S3 error-code table is a table of strings that has to be kept in step
  with the servers. It is deliberately small: the codes it names are the ones
  the S3 protocol documents and the ones the deployment's servers answer with,
  and a code it does not name becomes `BlobFailure` rather than a guess.
- The `exists()`/`stat()`/`delete()` behaviour change on a missing bucket (see
  decision 5). A caller that relied on `False` for a missing bucket now sees
  `NotFound` — which is the point, but it is a change.

**What it would take to move back.** A substrate that has its own error
vocabulary and its own translator — which is what the relational seam already
is. The repository layer depends on the `BlobStore` protocol, so the change is
confined to the cold lake package.

**What was deliberately *not* done.**

- **The `BlobStore` contract did not grow a `list` operation** (decision D-6 of
  the plan). Enumeration is not part of a content-addressed store's contract;
  the live S3 suite uses a raw client for its one "is it really there" assertion
  and this ADR records the asymmetry.
- **`minio_backend.py` was not renamed** (ADR-0026 defers it deliberately).
- **No method signature changed** — the signatures were already correct and are
  pinned by `tests/storage/test_blobstore_contract.py`.
- **The two implementations' internals were not rewritten** beyond their error
  paths. The addressing, the fan-out, the dedup and the verify-on-read are
  untouched.
- **No `boto3` extra, no marker, no dependency change.** The live S3 suite and
  the `paa[s3]` extra belong to the next wave.

**What is still open.** The live S3 suite (the next wave) will exercise this
classifier against two real servers — SeaweedFS and RustFS — which is the only
way to know that the error codes they answer with are the codes in the table.
The unit suite proves the classifier against a stub shaped like the driver;
that is a strong proof of the *mechanism*, and not a proof of the *table*.

---

## Amendment — the bucket-absence clause, earned rather than assumed

*Amended 2026-10-22, by W1-IMPR-01 (T-001), after the wave-1 review. Everything
above still stands: one vocabulary, the translation at the boundary and it is
idempotent, a domain error is not the substrate's to relabel, the driver is
recognised structurally and is never imported, boto3's upload wrapper is
unwrapped, the message names the concept and the driver's wording is the
cause, and the contract declares its error behaviour. This amendment changes
**decision 5** — how the discrimination is made — and corrects two claims the
review found to be untrue. It adds nothing to the rest of the vocabulary.*

### What was wrong

Decision 5 said a missing **bucket** raises `NotFound` "from every method", and
that it raises it from `_head()` — "the one place every method goes through" —
so that the two write paths "cannot disagree".

**It could not happen.** A HEAD response has **no body**, so the driver has no
code to report and reports the *status* as the code. A HEAD on a missing key
and a HEAD on a missing bucket are therefore the same `"404"`, and the
bucket-level code (`NoSuchBucket`) lives in an error *body* that a HEAD never
has. The store classified every HEAD 404 as an *object*-level absence, and
one condition — a missing bucket — got four different answers from one store:

| method | what it answered | what it answers now |
|---|---|---|
| `exists` | `False` | `NotFound("bucket not found")` |
| `stat` | `None` | `NotFound("bucket not found")` |
| `delete` | `False` | `NotFound("bucket not found")` |
| `get` / `get_stream` | `NotFound("blob not found")` | `NotFound("bucket not found")` |
| `put` / `put_stream` | `NotFound("bucket not found")` | `NotFound("bucket not found")` |

The read paths told the caller "the blob is not there" when the truth was "the
store is not there" — the exact lie this ADR was written to remove. The
decision's *reasoning* was right and its *implementation* was absent, and the
consequence was worse than the silence decision 5 removed: a contract, an ADR
and a test suite all asserted a behaviour the code could not produce. A
contract that lies is worse than a contract that is silent, because a silent
one cannot be violated.

### What changed

**The discrimination is now earned.** When the store has concluded that
something is missing, it asks the server about the *bucket* — a
`head_bucket` — before it reports an absence:

* the bucket is there → the blob is not there → `NotFound("blob not found")`,
  and `exists()` / `stat()` / `delete()` answer `False` / `None` / `False` as
  they always did;
* the bucket is not there → `NotFound("bucket not found")`, carrying a
  `namespace="bucket"` detail, from **every** method.

A `head_bucket` 404 means the bucket does not exist; a 403 means it does and
this principal may not list it, which is the one answer the store cannot act
on, and it falls back to the object-level absence — the behaviour before this
amendment. Any other failure of the probe is a failure of the *store* and is
raised, because "the store is broken" and "the blob is not there" must not be
confused, and a caller that cannot tell them apart will act on the wrong one.

**The write path does not ask on a successful write.** A write's own response
carries a body, and the body says which of the two is missing (`NoSuchBucket`
against `NoSuchKey`), so the write path already knew. Asking again would have cost a
request on every first write of every blob — the hot path — to learn what the
write was about to say anyway. The probe is paid only where the answer is
load-bearing: on the path that has already decided that something is missing.

**Amended 2026-10-22** — a write that **fails** with an answer that carries no body
is the other case: that answer does not say which of the two absences it is, so the
write path asks as well, on the failure path only. The second Amendment at the end
of this record says which failures ask, and corrects the claim that the write path
never asks.

**The answer is not cached.** A cached "the bucket was there" is exactly the
answer that is wrong when the bucket is deleted under a running daemon, which
is the case this exists to catch; and a cached "the bucket was not there"
would be wrong the moment somebody creates it.

**`namespace="bucket"` is the machine-readable half of the answer.** Decision 1
said the details "(`sha256=` against `bucket=`) say which". They did not:
which identifiers a raise site happens to carry is a function of the *call
site*, not of the condition — the write path's bucket-level error carried a
`sha256=`, and the read path's object-level error carried none. The rule now
is one detail with one meaning: `namespace="bucket"` is present **if and only
if** the substrate's namespace is what is missing. A detail that is present ⇔
a condition holds is a rule a caller can test; "the details that happen to be
there" is not one.

### The rest of the review's corrections

* **The filesystem substrate leaked raw exceptions.** `Path.exists()` re-raises
  every errno except the four that mean "not there", so an unreadable archive
  leaked a raw `PermissionError` out of `exists`, `stat`, `delete`, `get` and
  `put` alike, and the `put` dedup path leaked a raw `FileNotFoundError` from
  a blob that vanished between the lookup and the stat. The **lookup** is now
  the boundary: one translation, and every method that reaches it inherits the
  classification. That is what makes the two substrates fail the same way for
  the same reason — a store that refuses raises `AccessDenied` on both — and a
  raw `OSError` is not a `PaaError`, so every `except PaaError` at the edge
  (`api/app.py`, `agents/orchestrator.py`, `cli/main.py`, `agents/base.py`) and
  `ArtifactArchive.verify`'s `except StorageError` used to miss it.
* **`CAS.stat()` leaked a raw `zstandard.ZstdError`** on a corrupt frame that
  `get()` reported as `Corrupt`. It is now the same class, the same message and
  the same details — the same file, the same damage, one answer.
* **`S3BlobStore.stat()` leaked a raw `ValueError`** when the driver answered
  with a `ContentLength` that is not a number. It is now the substrate's
  malformed answer, carrying the *type* of the value and never the value.
* **The `__cause__` was sometimes the exception itself.** `raise translated
  from exc` sets `__cause__` to the exception when the translator returned the
  same object — which it does for an error that is already ours. The boundary
  sites now go through one helper that spells the two cases apart: a new
  translation is raised with the original as its cause, and an error that is
  already ours is re-raised unchanged.
* **The translators had two equivalent branches.** `StorageError` and
  `PaaError` are both caught by the second, and both returned the exception
  unchanged, so the first was dead. It is gone, and the comment now says what
  the one branch means.
* **The `errors.py` docstring promised more than the code did.** It said a
  `TypeError` from the calling code "must not be reported as an infrastructure
  fault"; the code reported it as the unclassified `BlobFailure`. The
  *behaviour* is right for a boundary this narrow — the exception that
  arrives is the substrate's, and the only question is which of the named
  judgements it is — and the docstring now says what the code does and why.

### What it costs

* **One extra request on the miss path** of the read and metadata methods, and
  on the **ambiguous write-failure** path. `exists()` / `stat()` / `delete()`
  have no production caller, and a read of a blob that is not there is an
  error case, so the cost is a request in a path that is already exceptional.
  A *hit* costs nothing, and a **successful write** costs nothing. A write that
  fails with an answer that carries no body costs one request, because that
  answer does not say which of the two absences it is — the second Amendment
  below says how that is now decided.
* **One request whose answer may be refused.** `head_bucket` wants
  `s3:ListBucket`; a deployment that grants only object-level permissions gets
  a 403, and the store then reports an object-level absence — the behaviour
  before this amendment. The write path still reports the missing bucket in
  as many words, so the condition is not lost, only deferred to the write.
* **A vocabulary detail with a rule attached.** `namespace="bucket"` has to
  mean one thing, and it does.

### What was deliberately *not* changed

* **The vocabulary did not grow.** A missing bucket and a missing object are
  the same judgement, and `NotFound` says so; the *detail* says which, not a
  second class.
* **The contract's shape did not change.** No signature moved, and the
  methods that answer "is it there?" still answer `False` / `None` / `False`
  for a blob that is not there.
* **The write path was not restructured.** It reads the code in the response
  body it already receives, and it does not probe — **except on the failure
  path**, where an answer that carries no body says nothing about which
  absence it is, and the same probe the read path uses resolves it. That is
  the second Amendment's change, and it changes *when* the write path asks,
  not how it writes.
* **The live S3 suite is still the next wave's job.** It is the only way to
  know that the codes the servers answer with are the codes in the table, and
  it is where the bucket case must be asserted against both servers.

---

## Amendment — one answer for one absence, and a probe that is asked once

*Amended 2026-10-22, by W1-IMPR-04 (the wave-1 delta review), after the delta
review of the wave-1 improvements. Everything above still stands: one vocabulary,
the translation at the boundary and it is idempotent, a domain error is not the
substrate's to relabel, the driver is recognised structurally and is never
imported, boto3's upload wrapper is unwrapped, the message names the concept
and the driver's wording is the cause, and the contract declares its error
behaviour. This amendment changes **how an unqualified absence is recognised**,
and it corrects three claims the delta review found to be untrue. It adds nothing
to the rest of the vocabulary.*

### What was wrong

**Two predicates decided what an unqualified answer is.** The read path
accepted an answer that carried a **status** and no code; the write path wanted
the **code**. So the same underlying failure — a 404 that says the addressed
thing is not there and stops there — got two answers:

| path | what it answered | the `namespace` detail | the probe |
|---|---|---|---|
| `stat` (read) | `NotFound("bucket not found")` | `namespace="bucket"` | asked |
| `put` (write) | `NotFound("blob not found")` | *absent* | **not asked** |

The write path named the **wrong** condition — the blob, when the bucket was
gone — and carried no namespace detail at all, so a caller that tests the detail
got a different answer from the same failure depending on which method it
called. The rule this record states — `namespace="bucket"` is present **if and
only if** the substrate's namespace is what is missing — was therefore not a rule
on the write path.

**The probe and the translator called each other with no bound.** The probe
(`head_bucket`) and the failure translator each ask the other: the translator
resolves an unqualified absence by asking the probe, and the probe translates
its own failure with the same translator. When the probe's failure carried a 404
that only the **cause** held — the shape boto3's upload wrapper has — the probe
did not recognise it (it read the wrapper, which carries no answer of its own)
and the translator did (it follows the cause), so the two called each other
until the interpreter's recursion limit ended the story: `RecursionError`, 97
probes deep, and an exception that is **not** a member of the vocabulary and
misses every `except PaaError` at the edge.

**Three records said the write path does not probe.** This record said it twice
("**The write path does not ask.**" and "**The write path was not
restructured.** … it does not probe."), and the improvement's own COMMS line
said "never on a write". The code probed: a write whose failure carried an
unqualified 404 was resolved by the same probe, so a write **could** cost one
extra request. The records understated the mechanism, and a maintainer who
believed them would not have thought to bound the probe or to keep the two
paths answering alike.

### What changed

**One predicate, used by both paths.** "The answer does not say which absence
it is" is now decided in one place: the code is the status reported as the
code, **or** the status is a 404 and no qualified code is present. The read
path's classifier and the write path's classifier are the same function, so
the two paths cannot answer alike-and-differently about the same 404.

**One reader of the driver's answer.** The code, the status and the error the
answer came from are now read in one function, and the probe and the
translator both use it. The probe used to read the **wrapper** and the
translator the **cause**, which is what let the recursion start: the probe
could not see a 404 the translator could see, so it asked again.

**The probe is asked at most once per failure.** The probe's own failure is
translated by a translator that does **not** resolve an absence, so a failure
that is being resolved cannot be resolved again. The bound is the invariant
that makes the recursion impossible even if the two readers of the answer ever
drift apart again.

**The write path's probe is stated, not denied.** A **successful** write never
asks: its response carries a body, and a body says which absence it is. A write
that **fails** with an answer that carries no body asks, on the failure path
only, and the cost is one extra request on a path that has already failed.

### What it costs

* **One extra request on the ambiguous write-failure path.** Only on a write
  that has already failed, and only when the failure's answer does not say
  which absence it is. The success path is untouched.
* **A predicate in one place instead of two.** The two call sites that decide
  "is this answer unqualified" now call one function, and the test that says
  the two paths answer alike is the test that would fail if they drifted.
* **The probe's answer is read one level deeper.** The probe now reads the
  cause of a wrapped failure, so a `head_bucket` that fails with a wrapped 404
  is read as what it says — the bucket is not there — instead of as a failure
  of the probe. That is a *better* answer, and it is the answer the same
  request would have given unwrapped.

### What was deliberately *not* changed

* **The vocabulary did not grow.** The two absences are one judgement and one
  class, and the detail says which.
* **The probe was not cached and was not made optional.** A cached "the bucket
  was there" is the answer that is wrong when the bucket is deleted under a
  running daemon, and the probe's refusal (a 403) is the one case the store
  cannot act on.
* **The live S3 suite is still the next wave's job.** The status-only 404 the
  two paths disagreed about is a shape botocore does not produce — it reports
  the status as the code for a bodyless answer — so it is exercised through a
  stub. Whether a real server answers a request with a status and no code is
  `[UNKNOWN]` here, and the live suite is where that belongs.

---

## Amendment — the wrapper is not an empty box, and `upload_fileobj` does not wrap

*Amended 2026-10-22, by W2-IMPR-05 (the wave-2 improve phase), after the
wave-2 test review measured the wrapper's shape against two real servers.
Everything above still stands: one vocabulary, the translation at the
boundary and it is idempotent, a domain error is not the substrate's to
relabel, the driver is recognised structurally and is never imported, boto3's
upload wrapper is unwrapped, the message names the concept and the driver's
wording is the cause, and the contract declares its error behaviour. This
amendment changes **what decision 4 says the mechanism is** — and the
Context above, which repeated the same error. It adds nothing to the
vocabulary and it changes no behaviour: the code was already right.*

### What was wrong

Decision 4 said that "boto3's transfer manager catches the driver's
`ClientError` and raises an `S3UploadFailedError` instead", and that the
translation "follows that wrapper". The wave-2 implementation report that
reviewed this decision went further and said the wrapper was **opaque** —
"no response, no cause, and therefore nothing for the translation to read" —
and concluded that the store's answer "degrades from `NotFound` to the
unclassified `BlobFailure`" on the wrapping route.

**Two of those three claims are false.**

1. **`upload_fileobj` — the call `put_stream` makes — does not wrap.** It
   propagates the driver's `ClientError` with its response intact. The
   wrapping entry point is boto3's `upload_file`, the *filename* variant.
2. **The wrapper is not empty.** It carries no response *of its own* and no
   `__cause__` — but boto3 raises it **inside** an `except ClientError`
   block and without a `from`, so the driver's error is the wrapper's
   implicit `__context__`. The translation follows the `__context__` as well
   as the `__cause__`, so it finds the answer.

Measured against SeaweedFS and RustFS, on boto3 1.43.117:

| call | raises | a response of its own | `__cause__` | `__context__` |
|---|---|---|---|---|
| `upload_fileobj` | the driver's `ClientError` | yes | unset | unset |
| `upload_file` | `S3UploadFailedError` | **no** | unset | **the driver's `ClientError`** |

And through the store, the wrapping route and the plain route give the **same
answer**: `NotFound("bucket not found")` with `namespace="bucket"`,
`s3_code="NoSuchBucket"` and `http_status=404` on both.

### What changed

* **Decision 4 now names `upload_file`** rather than "the transfer manager",
  and says plainly that `upload_fileobj` does not wrap.
* **Decision 4 now says the wrapper is not opaque**, and says where the
  driver's error is: the implicit `__context__`.
* **The Context no longer claims `put_stream` was handed a wrapper.** It
  says what actually happened: `put()` re-raised the driver's own error and
  `put_stream()` turned the same condition into a free-text `StorageError`.
* **Three comments in `minio_backend.py`** that repeated the mechanism —
  the module docstring, the `_MAX_CAUSE_DEPTH` note and the note in
  `_upload_fileobj` — say the same true thing.

### What it costs

* **Nothing at runtime.** No behaviour changed: the boundary already held,
  and the measurement says so.
* **One live test that had to be written**, because nothing in the tree
  pinned the claim decision 4 makes: the unit suite's stub has no
  `upload_file` at all, and the live path never produces a wrapper. The
  wave-2 report's own mutation battery **recorded** that mutation as
  caught; measured, it **survives** every live test.
* **A record that had to be corrected in three places** (this ADR, the code
  comments, and the wave-2 report), because a record whose mechanism is
  wrong is a record a maintainer will act on.

### What was deliberately *not* changed

* **The vocabulary did not grow** and **no error class was added**. The
  boundary held; only the record about it was wrong.
* **The unwrapping was not removed.** It is load-bearing — for the
  connection-error chain, where the cause is the raw `ConnectionRefusedError`
  — and it is now *pinned* rather than merely present.
* **The store still writes through `upload_fileobj`.** The wrapping route is
  not "better"; it is a different entry point with a different error shape,
  and the store's answer is the same either way, which is the property the
  new test checks.

