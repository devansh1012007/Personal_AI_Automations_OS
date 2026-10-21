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
which writes through `put_object` — and as a generic `StorageError` from
`put_stream()`, which writes through `upload_fileobj` and is therefore handed
boto3's `S3UploadFailedError` wrapper. A caller could not write a correct
`except` clause at all, and a security-relevant judgement — *is this blob
absent, or is the store refusing?* — was unanswerable.

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
  missing *bucket* are the same judgement, and the details (`sha256=` against
  `bucket=`) say which.
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
`upload_fileobj`, and boto3's transfer manager catches the driver's
`ClientError` and raises an `S3UploadFailedError` instead — without chaining
it explicitly, so the original is the implicit `__context__`. The translation
follows that wrapper (bounded, and only when the cause *looks* like a driver's
error), so a missing bucket is one answer from both paths rather than a raw
`ClientError` from one and a generic error from the other. The same unwrap
makes every *other* code agree between the two paths, not just the missing
bucket.

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
