"""S3-backed cold lake — the RFC's original object substrate.

SPEC DEVIATION REVERSAL (docs/adr/0019): ADR-0004 replaced MinIO with a local
content-addressed directory because the target laptop had no Docker and no RAM
to spare for an S3 server. The Docker deployment restores an S3 server — the
compose stack runs SeaweedFS. This module is the server-backed sibling of
:class:`~paa.storage.coldlake.cas.ContentAddressedStore` and presents the
*same* interface — ``put``/``get``/``exists``/``delete``/``stat``/``put_stream``/
``get_stream`` returning
:class:`~paa.storage.coldlake.cas.BlobRef` — so the signal and artifact
repositories neither know nor care which one they hold.

The guarantees are preserved exactly, because they are what make the cold lake
trustworthy, not implementation details:

* **Content addressing.** An object's key is derived from the SHA-256 of its
  *raw* bytes, with the same ``blobs/<hh>/<hh>/<hash>`` fan-out the filesystem
  store uses. Two identical payloads converge on one object; a re-upload is a
  no-op. The :class:`BlobRef` URI stays ``cas://<hash>`` so a ``blob_uri``
  column written by one backend resolves against the other unchanged — the two
  stores are drop-in interchangeable.
* **Verify-on-read.** :meth:`get` and :meth:`get_stream` re-hash the bytes and
  raise :class:`~paa.storage.coldlake.errors.Corrupt` on mismatch rather than
  return plausible-looking data. Bit-rot in an archive nobody reads until they
  urgently need it is this layer's worst failure mode; S3 durability does not
  remove the obligation to check.
* **Bounded memory.** :meth:`put_stream` spools through a size-capped temp file
  and uploads a file object; :meth:`get_stream` yields fixed chunks straight
  from the response body. A 500 MB artifact never becomes a 500 MB ``bytes``.

``boto3`` is imported lazily so this module stays importable — and the package
installable — without the ``s3`` extra. Only constructing a store that must
create its own client reaches for it; an injected client (the test path) does
not.

The driver's errors are translated at this boundary, into the vocabulary in
:mod:`paa.storage.coldlake.errors` (ADR-0032). Two consequences are worth
stating here, because they are the reason the translation reads the driver
*structurally*:

* **The driver's exception never reaches a caller.** A caller catches a
  concept — absent, refused, unreachable, corrupt — and never has to import
  ``botocore`` to know which one happened.
* **The same condition reports the same thing on every path.** ``put`` writes
  through ``put_object`` and ``put_stream`` through ``upload_fileobj``, and
  **neither of them wraps**: the driver's error reaches the store with its
  response intact. boto3's *other* upload entry point — ``upload_file``, the
  filename variant — is the one that wraps the driver's error in an
  ``S3UploadFailedError``, and it wraps it **without chaining it**, so the
  driver's error is the wrapper's implicit ``__context__``. The translation
  follows that ``__context__`` as well as any ``__cause__``, so a missing
  bucket is one answer from every path, rather than a raw ``ClientError``
  from one and a generic error from the other.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from typing import IO, Any, Final

import structlog
import zstandard

from paa.core.errors import PaaError
from paa.storage.coldlake.cas import (
    DEFAULT_CHUNK_SIZE,
    BlobRef,
    _compress,
    _iter_chunks,
    _translated_chunks,
    _validate_digest,
)
from paa.storage.coldlake.errors import (
    AccessDenied,
    BlobFailure,
    Corrupt,
    MalformedRequest,
    NotFound,
    NotSupported,
    Unreachable,
    raise_blob_error,
    translate_blob_error,
)

__all__ = ["S3BlobStore", "boto3_available"]

log = structlog.get_logger(__name__)

_SUBSTRATE: Final = "s3"

_ZSTD_SIZE_UNKNOWN: Final = -1
_COMPRESSED_SUFFIX: Final = ".zst"
_RAW_SUFFIX: Final = ".bin"

#: Object metadata key carrying the raw (pre-compression) size, so :meth:`stat`
#: can answer without downloading and decompressing the blob. S3 lower-cases
#: user metadata keys, so this is spelled lower-case to round-trip cleanly.
_META_RAW_SIZE: Final = "raw-size"

#: Spool up to this many bytes in memory during a streamed upload before the
#: temp file spills to disk. Matches one chunk: small streams never touch disk,
#: large ones never blow the heap.
_SPOOL_MAX_BYTES: Final = DEFAULT_CHUNK_SIZE


# ---------------------------------------------------------------------------
# The S3 dialect
# ---------------------------------------------------------------------------
#
# The S3 protocol reports failures in *codes*, and the codes are the portable
# part of the driver's vocabulary: the exception **class** differs between
# botocore versions and between servers, the code does not. So the code is the
# primary signal, the HTTP status is the coarse fallback, and the class names
# are the last resort — they are the only signal a *connection* failure
# carries, because botocore's connection errors have no response at all.
#
# Every one of these tables is read **structurally**. ``botocore`` is an
# optional extra, and importing it to learn a class hierarchy would make a
# minimal install stop importing — and, worse, would make the same exception
# classify differently depending on whether the driver happened to be
# installed, so the unit suite would not be exercising the code the deployment
# runs. The unit tests use a duck-typed stub whose class names and response
# dicts are shaped like the driver's, and the live suite (wave 2) exercises
# the same classifier against a real server.

#: The code the driver reports when the server's answer carried no body of
#: its own and the *status* is all there is.
#:
#: A HEAD response has no body, so a HEAD on a missing key arrives with this
#: code — and a HEAD on a missing **bucket** arrives with exactly the same
#: one. The two are therefore indistinguishable from the HEAD alone, and
#: the store resolves the ambiguity by asking the server about the bucket
#: (see :meth:`S3BlobStore._namespace_is_absent`). A response that *does*
#: carry a body says which of the two it is, in the code.
_S3_STATUS_AS_CODE: Final[str] = "404"

#: S3 error codes that mean the addressed **object** is not there.
#:
#: ``"404"`` is in the list because a HEAD on a missing key answers 404 with
#: an empty body, and botocore then reports the status as the code.
_S3_OBJECT_MISSING_CODES: Final[frozenset[str]] = frozenset(
    {
        _S3_STATUS_AS_CODE,
        "NoSuchKey",
        "NoSuchVersion",
        "NotFound",
    }
)

#: S3 error codes that mean the **bucket** is not there.
#:
#: A bucket-level absence is not an object-level one: the store cannot answer
#: "is this blob there?" at all when the bucket it lives in is gone, and
#: answering "no" would be a lie.
_S3_BUCKET_MISSING_CODES: Final[frozenset[str]] = frozenset(
    {
        "NoSuchBucket",
    }
)

#: S3 error codes that mean the server **refused** the request.
#:
#: The credential failures are in here with the authorisation ones because
#: they are the same judgement for a caller — an operator has to go and fix
#: the credentials or the policy — and the driver's own code, carried in the
#: ``s3_code`` detail, says which of the two it was.
_S3_REFUSED_CODES: Final[frozenset[str]] = frozenset(
    {
        "401",
        "403",
        "AccessDenied",
        "AllAccessDisabled",
        "ExpiredToken",
        "InvalidAccessKeyId",
        "InvalidToken",
        "SignatureDoesNotMatch",
        "Unauthorized",
    }
)

#: S3 error codes that mean the request **could not be completed** and may be
#: retried.
#:
#: ``SlowDown`` is here rather than with the refusals because it is the server
#: asking for less traffic, not refusing this request: the same request, a
#: moment later, will succeed.
_S3_RETRYABLE_CODES: Final[frozenset[str]] = frozenset(
    {
        "500",
        "500InternalError",
        "502",
        "503",
        "InternalError",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "SlowDown",
    }
)

#: botocore exception class names that mean the substrate **could not be
#: reached**.
#:
#: Read off the exception's MRO, so ``IncompleteReadError`` is recognised as
#: the ``ResponseStreamingError`` it inherits from — the hierarchy is exactly
#: what ``isinstance`` consults, and only the names are read.
_S3_UNREACHABLE_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "ConnectTimeoutError",
        "ConnectionClosedError",
        "ConnectionError",
        "EndpointConnectionError",
        "ProxyConnectionError",
        "ReadTimeoutError",
        "ResponseStreamingError",
        "SSLError",
    }
)

#: botocore exception class names that mean the **credentials** are missing or
#: wrong. The server never sees the request, so there is no code to read.
_S3_REFUSED_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "NoCredentialsError",
        "PartialCredentialsError",
    }
)

#: The class names that mark an exception as **the driver's own**, used to
#: decide whether an exception wrapped by another one is worth unwrapping.
_S3_DRIVER_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "BotoCoreError",
        "ClientError",
        *_S3_REFUSED_CLASS_NAMES,
        *_S3_UNREACHABLE_CLASS_NAMES,
    }
)

#: HTTP statuses that mean the server refused, when the driver reported no
#: code of its own.
_S3_REFUSED_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

#: HTTP statuses that mean the request could not be completed, when the driver
#: reported no code of its own.
_S3_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})

#: How many wrapped causes are unwrapped before giving up.
#:
#: boto3's ``upload_file`` — the *filename* variant — wraps the
#: ``ClientError`` of a failed upload in an ``S3UploadFailedError``, and it
#: does so **without chaining it**, so the driver's error is the wrapper's
#: implicit ``__context__`` rather than its ``__cause__``. Without
#: unwrapping it, every upload failure would arrive unclassified while the
#: same condition on the ``put_object`` path arrived classified — the
#: ``put``/``put_stream`` divergence, in its general form. The bound keeps
#: the unwrap from walking a chain of wrappers for ever.
_MAX_CAUSE_DEPTH: Final = 3


def boto3_available() -> bool:
    """Whether the ``boto3`` package is importable. Used to skip tests."""
    try:
        import boto3  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _require_boto3() -> Any:
    try:
        import boto3
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise NotSupported(
            "the S3 cold-lake backend requires the 's3' extra "
            "(pip install 'paa[s3]')",
            substrate=_SUBSTRATE,
            reason="driver_missing",
        ) from exc
    return boto3


def _s3_error_code(exc: BaseException) -> str | None:
    """The driver's error code, read structurally off the response.

    botocore's ``ClientError`` carries the server's answer as a dict, and the
    code is the portable part of it: the exception class differs between
    botocore versions and between servers, the code does not.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    if isinstance(code, str) and code:
        return code
    return None


def _s3_status(exc: BaseException) -> int | None:
    """The HTTP status the driver saw, when it saw one."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, dict):
        return None
    status = metadata.get("HTTPStatusCode")
    if isinstance(status, int):
        return status
    return None


def _s3_class_names(exc: BaseException) -> set[str]:
    """The class names of the exception and of every base it inherits from.

    The MRO is what ``isinstance`` consults; only the *names* are read,
    because ``botocore`` is an optional dependency and importing it to learn
    a class hierarchy would make a minimal install stop importing.
    """
    return {klass.__name__ for klass in type(exc).__mro__}


def _driver_cause(exc: BaseException) -> BaseException | None:
    """The driver's own error behind a wrapper, if there is one.

    Both ``__cause__`` and ``__context__`` are followed, and the
    ``__context__`` is not the lesser of the two: boto3's ``upload_file``
    raises its ``S3UploadFailedError`` **inside** an ``except ClientError``
    block and without a ``from``, so the driver's error is the wrapper's
    implicit ``__context__`` and its ``__cause__`` is unset. A reader that
    looked only at the ``__cause__`` would conclude that the wrapper carries
    nothing, and would be right about the wrapper and wrong about the
    translation — which is what reads the ``__context__``.

    The cause is only followed when it *looks* like a driver error — one
    that carries an S3 response, or one whose class names are botocore's —
    so an unrelated ``__context__`` is never mistaken for the real reason.
    """
    cause = exc.__cause__ or exc.__context__
    if cause is None or cause is exc:
        return None
    if _s3_error_code(cause) is not None:
        return cause
    if _s3_class_names(cause) & _S3_DRIVER_CLASS_NAMES:
        return cause
    return None


def _driver_answer(exc: BaseException) -> tuple[BaseException, str | None, int | None]:
    """What the driver said: the error itself, its **code** and its **status**.

    The answer is read off the **unwrapped** error, for the same reason the
    translation unwraps: boto3's ``upload_file`` raises a wrapper around
    the driver's error, and the wrapper carries no response of its own, so
    reading the wrapper is reading *no* answer at all.

    It is read in ONE place, and every reader of it uses this function, for
    a reason that is not tidiness. The reader of the answer and the
    translator of the answer have to read the **same** answer: when the
    probe read the wrapper and the translator read the cause, the probe did
    not recognise a 404 the translator could see, asked again, and the two
    called each other until the recursion limit ended the story.
    """
    current = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        cause = _driver_cause(current)
        if cause is None:
            break
        current = cause
    return current, _s3_error_code(current), _s3_status(current)


def _does_not_say_which_absence(code: str | None, status: int | None) -> bool:
    """Whether the driver's answer does not say **which** absence it is.

    A response that carries a body names its condition: ``NoSuchKey`` for an
    object, ``NoSuchBucket`` for the namespace. A response with **no body**
    does not — it says the addressed thing is not there and stops there.
    That is true of the code the driver synthesises for a bodyless answer
    (the status reported as the code) and of a status with no code at all,
    which is what a server that answers a request without a body carries.

    Both are the same condition, so they are read by **one** predicate. Two
    predicates are two answers: the read path resolved the first and not the
    second, and a caller could not tell which answer it had been given.
    """
    if code is not None:
        return code == _S3_STATUS_AS_CODE
    return status == 404


def _is_missing_object(exc: BaseException) -> bool:
    """Whether the driver's error says the addressed **object** is not there.

    The bucket-level codes are excluded on purpose: a missing bucket is not
    a missing object, and the store cannot answer the question at all.

    The ambiguous answer — the status reported as the code, which is all a
    HEAD carries — counts as an object-level absence here, and the *caller*
    resolves the ambiguity: it asks the server about the bucket before it
    reports an absence (see :meth:`S3BlobStore._require_namespace`).
    """
    _error, code, status = _driver_answer(exc)
    if code is not None:
        return code in _S3_OBJECT_MISSING_CODES
    # No code of its own: the status is all the driver has, and a 404 is an
    # absence that does not say which one.
    return _does_not_say_which_absence(code, status)


def _absence_qualification(details: dict[str, Any]) -> tuple[str | None, int | None]:
    """The driver's code and status, as a translated error carries them.

    The translation puts the driver's own answer in the details, so the
    ambiguity of an answer can be read off a translated error without the
    driver's exception being in hand.
    """
    code = details.get("s3_code")
    status = details.get("http_status")
    return (
        code if isinstance(code, str) else None,
        status if isinstance(status, int) else None,
    )


def _is_ambiguous_absence(translated: Exception) -> bool:
    """Whether a translated error is an absence the driver did not qualify.

    The driver's code says which of the two absences it is — ``NoSuchKey``
    for an object, ``NoSuchBucket`` for the namespace — except when the
    answer carries no body, and then it says only that the addressed thing
    is not there. That one is ambiguous, and the store resolves it.

    The predicate is the shared one (:func:`_does_not_say_which_absence`),
    so the write path and the read path agree about what an ambiguous
    answer is.
    """
    if not isinstance(translated, NotFound):
        return False
    code, status = _absence_qualification(translated.details)
    return _does_not_say_which_absence(code, status)


def _translate_s3_error(
    exc: BaseException, *, substrate: str = _SUBSTRATE, **details: Any
) -> Exception:
    """Turn an S3 driver's error into one of the neutral blob errors.

    The S3 dialect of :func:`paa.storage.coldlake.errors.translate_blob_error`:
    the driver's error codes are read structurally and mapped onto the
    vocabulary, and anything this table does not name is handed to the shared
    translator.

    The translation is **idempotent**: an error that is already one of ours is
    returned unchanged, so a boundary that runs twice cannot flatten a
    specific failure into a vaguer one.

    The details are **identifiers** — ``key``, ``sha256``, ``bucket``,
    ``s3_code``, ``http_status``, ``driver_error`` — because
    ``PaaError.to_payload()`` merges them verbatim into a ledger payload.
    The driver's own wording is not among them: it is the ``__cause__``,
    where it stays available to whoever reads the traceback.
    """
    if isinstance(exc, PaaError):
        # Already neutral — ours, or another domain's. Either way it is not
        # the driver's to relabel, and it is not unwrapped either: its own
        # ``__context__`` may be anything.
        return exc

    current, code, status = _driver_answer(exc)
    names = _s3_class_names(current)

    common: dict[str, Any] = {**details, "driver_error": type(current).__name__}
    if code is not None:
        common["s3_code"] = code
    if status is not None:
        common["http_status"] = status

    if code is not None and code in _S3_BUCKET_MISSING_CODES:
        # The namespace is the thing that is missing, and the detail says
        # so: ``namespace="bucket"`` is present if and only if the bucket
        # is what is not there.
        return NotFound(
            "bucket not found", substrate=substrate, **{**common, "namespace": "bucket"}
        )
    if code is not None and code in _S3_OBJECT_MISSING_CODES:
        return NotFound("blob not found", substrate=substrate, **common)
    if code is not None and code in _S3_REFUSED_CODES:
        return AccessDenied(
            "the substrate refused the request", substrate=substrate, **common
        )
    if code is not None and code in _S3_RETRYABLE_CODES:
        return Unreachable(
            "the substrate could not be reached", substrate=substrate, **common
        )
    if names & _S3_REFUSED_CLASS_NAMES:
        return AccessDenied(
            "the substrate refused the request", substrate=substrate, **common
        )
    if names & _S3_UNREACHABLE_CLASS_NAMES:
        return Unreachable(
            "the substrate could not be reached", substrate=substrate, **common
        )
    if status in _S3_REFUSED_STATUSES:
        return AccessDenied(
            "the substrate refused the request", substrate=substrate, **common
        )
    if status in _S3_RETRYABLE_STATUSES:
        return Unreachable(
            "the substrate could not be reached", substrate=substrate, **common
        )
    if status == 404:
        return NotFound("blob not found", substrate=substrate, **common)
    # Nothing this table names. The shared translator has the last word: it
    # knows the codec's and the filesystem's failures, and it reports the
    # rest as the unclassified failure.
    return translate_blob_error(current, substrate=substrate, **common)


def _content_length(
    head: dict[str, Any], *, key: str, sha256: str, default: int = 0
) -> int:
    """The object's length on the wire, as the driver reported it.

    A broken server can answer with something that is not a number, and the
    conversion used to let the raw ``ValueError`` escape the store — a raw
    exception leaving the substrate, which is the invariant the vocabulary
    exists to keep, and one the filesystem substrate does not have (its
    equivalent race is translated).

    It is reported as the substrate's malformed answer. The *type* of the
    value is kept, because the operator needs to know the server sent a
    string where a number belongs; the value itself is not, because it
    came from the server and the details are merged verbatim into a ledger
    payload.
    """
    value = head.get("ContentLength", default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BlobFailure(
            "the substrate answered with a length that is not a number",
            substrate=_SUBSTRATE,
            driver_error=type(exc).__name__,
            content_length_type=type(value).__name__,
            key=key,
            sha256=sha256,
        ) from exc


class S3BlobStore:
    """Immutable, compressed, deduplicating blob storage over S3.

    Synchronous, exactly like :class:`ContentAddressedStore`: every operation is
    one bounded S3 request. Callers on the event loop that move large payloads
    should wrap calls in :func:`asyncio.to_thread`, as the kuzu backend does.
    """

    def __init__(
        self,
        bucket: str,
        *,
        client: Any | None = None,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        secure: bool = False,
        region: str | None = None,
        prefix: str = "blobs",
        zstd_level: int = 3,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        ensure_bucket: bool = False,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._zstd_level = zstd_level
        self._chunk_size = chunk_size
        self._owns_client = client is None

        if client is not None:
            self._client = client
        else:
            boto3 = _require_boto3()
            try:
                self._client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    use_ssl=secure,
                    region_name=region,
                )
            except Exception as exc:
                # Building the client is a substrate boundary too: a bad
                # endpoint, a profile that does not exist, a region the driver
                # rejects. It used to raise the driver's own exception out of
                # the constructor.
                raise_blob_error(
                    exc,
                    substrate=_SUBSTRATE,
                    bucket=bucket,
                    op="client",
                    translate=self._translate_failure,
                )

        if ensure_bucket:
            self._ensure_bucket()

    # -- keys --------------------------------------------------------------

    @property
    def bucket(self) -> str:
        return self._bucket

    def _key(self, digest: str, *, compressed: bool) -> str:
        suffix = _COMPRESSED_SUFFIX if compressed else _RAW_SUFFIX
        return f"{self._prefix}/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"

    def _locate(self, digest: str) -> tuple[str, dict[str, Any]] | None:
        """Find a stored object whichever way it was written, with its head."""
        _validate_digest(digest)
        for compressed in (True, False):
            key = self._key(digest, compressed=compressed)
            head = self._head(key)
            if head is not None:
                return key, head
        return None

    def _head(self, key: str) -> dict[str, Any] | None:
        """The object's head, or ``None`` if the object is not there.

        A missing **object** is an ordinary answer — the blob is not stored —
        and a missing **bucket** is not: the store cannot answer the question
        at all, and answering "no" would be a lie that a daemon would act on.

        The head alone cannot tell the two apart: a HEAD response has no
        body, so both arrive as the same 404 (see
        :data:`_S3_STATUS_AS_CODE`). So this method reports the *absence* and
        leaves the question of which absence it is to the caller, which
        asks the server about the bucket before it tells a caller that
        something is not there (see :meth:`_require_namespace`).
        """
        try:
            # The client is untyped on purpose: ``boto3`` is an optional extra
            # and must not be imported at module scope, so the shape of what
            # the client returns is stated here rather than checked.
            head: dict[str, Any] = self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_missing_object(exc):
                return None
            raise_blob_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                op="head",
                translate=self._translate_failure,
            )
        return head

    # -- the namespace -----------------------------------------------------
    #
    # The bucket is the substrate's namespace: the thing the objects live in,
    # and the thing that can be gone. A HEAD cannot say whether the bucket
    # or the key is missing, so the store asks the server about the bucket
    # itself before it reports an absence to a caller. That is the whole of
    # the "a missing bucket raises" clause: the discrimination is *earned*
    # by a second request, not assumed from a code a HEAD cannot carry.

    def _namespace_is_absent(self) -> bool:
        """Whether the server says the bucket is not there.

        The question is asked of ``head_bucket`` and of nothing else,
        because that is the request whose answer is about the bucket: a
        HeadBucket's 404 means the bucket does not exist, and a 403 means it
        does and this principal may not list it.

        The order of the answers is the dialect's own: the **code** decides
        when it is one this dialect knows — ``NoSuchBucket`` for a bucket
        that is not there, a refusal code for a bucket that is there and may
        not be listed — and the **status** decides otherwise, because a
        status is the coarse fallback. The status reported as the code
        (``"404"``) is the answer a HEAD gives, and it is a 404.

        A refusal is therefore read as "the bucket is there" — which is
        what S3's own rule says, since it answers 404 (not 403) for a
        bucket that does not exist. It is the one case the store cannot
        be sure about, and it is the *old* behaviour: the store reports an
        object-level absence, and the next write reports the missing bucket
        in as many words.

        Any other failure is a failure of the store and is raised: "the
        store is broken" and "the blob is not there" must not be confused,
        and a caller that cannot tell them apart will act on the wrong one.

        The answer is **not** cached. A cached "the bucket was there" is exactly
        the answer that is wrong when the bucket is deleted under a running
        daemon, which is the case this exists to catch; and a cached "the
        bucket was not there" would be wrong the moment somebody creates it.

        The answer is read off the **unwrapped** error, exactly as the
        translation reads it. That is not tidiness: when the probe read the
        wrapper and the translator read the cause, a 404 the probe could not
        see became a question the probe asked again, and the two called each
        other until the recursion limit ended the story with a
        ``RecursionError`` — which is not a member of the vocabulary and
        tells a caller nothing.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            _error, code, status = _driver_answer(exc)
            if code in _S3_BUCKET_MISSING_CODES:
                return True
            if code is not None and code in _S3_REFUSED_CODES:
                return False
            if code == _S3_STATUS_AS_CODE or status == 404:
                return True
            if status in _S3_REFUSED_STATUSES:
                return False
            raise_blob_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                op="head_bucket",
                translate=self._translate_probe_failure,
            )
        return False

    def _bucket_missing(self, details: dict[str, Any], *, substrate: str = _SUBSTRATE) -> NotFound:
        """The error for a bucket that is not there.

        The ``namespace="bucket"`` detail is the machine-readable half of
        the answer: it is present if and only if the substrate's namespace
        is the thing that is missing, so a caller can tell "the store is
        gone" from "the blob is gone" without parsing a message.
        """
        return NotFound(
            "bucket not found",
            substrate=substrate,
            **{**details, "bucket": self._bucket, "namespace": "bucket"},
        )

    def _require_namespace(self, digest: str, *, op: str) -> None:
        """Make sure an absence is an absence and not a broken store.

        Called by every method that is about to tell a caller that
        something is not there. If the bucket is gone, the store cannot
        answer the question at all, and it says so — it does not answer
        "no" about a store that is not there.

        The cost is one request, and it is paid only on the path that has
        already decided that something is missing. A hit costs nothing.
        """
        if self._namespace_is_absent():
            raise self._bucket_missing({"sha256": digest, "op": op})

    def _translate_failure(
        self,
        exc: BaseException,
        *,
        substrate: str = _SUBSTRATE,
        resolve_absence: bool = True,
        **details: Any,
    ) -> Exception:
        """The S3 dialect's translation, with an ambiguous absence resolved.

        A response that carries a body names its condition: ``NoSuchKey``
        for an object, ``NoSuchBucket`` for the namespace. A response with
        no body does not — it says the addressed thing is not there and
        stops there. That answer is resolved by asking the server about
        the bucket, so the store reports "the bucket is not there" as such
        rather than as "the blob is not there".

        The resolution happens on the failure path only, so it costs
        nothing when the store is working — and it is **bounded**: a
        failure that is already being resolved is not resolved again (see
        :meth:`_translate_probe_failure`).

        The write path pays for it too. A write's own response usually
        carries a body, and a body says which of the two absences it is —
        so a *successful* write never asks. A write that fails with an
        answer that carries no body asks, because that answer does not say
        which absence it is, and a write can cost one extra request.
        """
        translated = _translate_s3_error(exc, substrate=substrate, **details)
        if resolve_absence and _is_ambiguous_absence(translated) and self._namespace_is_absent():
            return self._bucket_missing(details, substrate=substrate)
        return translated

    def _translate_probe_failure(
        self, exc: BaseException, *, substrate: str = _SUBSTRATE, **details: Any
    ) -> Exception:
        """The translation of the **probe's own** failure, which does not probe.

        The probe asks the server about the bucket. When that request
        fails, the failure is translated like any other — but the
        translation must not ask the server about the bucket **again**: the
        answer would come from the same broken request, and the two would
        call each other until the interpreter's recursion limit ended the
        story with a ``RecursionError``, which is not a member of the
        vocabulary and misses every ``except PaaError`` at the edge.

        So the probe is asked at most once per failure, and its own failure
        is reported as what it is: the store could not find out.
        """
        return self._translate_failure(
            exc, substrate=substrate, resolve_absence=False, **details
        )

    def _ensure_bucket(self) -> None:  # pragma: no cover - needs a live server
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            translated = _translate_s3_error(
                exc, substrate=_SUBSTRATE, bucket=self._bucket, op="head_bucket"
            )
            if not isinstance(translated, NotFound):
                raise_blob_error(
                    exc,
                    substrate=_SUBSTRATE,
                    bucket=self._bucket,
                    op="head_bucket",
                    translate=self._translate_failure,
                )
            # The bucket is not there: create it. A cold lake whose bucket is
            # missing fails on its first write with an opaque NoSuchBucket,
            # long after the deployment was declared healthy.
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except Exception as create_exc:
                raise_blob_error(
                    create_exc,
                    substrate=_SUBSTRATE,
                    bucket=self._bucket,
                    op="create_bucket",
                    translate=self._translate_failure,
                )
            log.info("s3.bucket_created", bucket=self._bucket)

    # -- writes ------------------------------------------------------------

    def put(self, data: bytes, *, compress: bool = True) -> BlobRef:
        """Store ``data`` and return its reference.

        The hash is over the raw bytes, never the compressed frame, so the
        address does not depend on the zstd level in force when it was written.
        """
        digest = hashlib.sha256(data).hexdigest()

        existing = self._locate(digest)
        if existing is not None:
            _key, head = existing
            return BlobRef(
                sha256=digest,
                size_bytes=len(data),
                compressed_bytes=_content_length(head, key=_key, sha256=digest),
            )

        payload = (
            _compress(data, level=self._zstd_level, substrate=_SUBSTRATE)
            if compress
            else data
        )
        key = self._key(digest, compressed=compress)
        self._put_object(key, payload, raw_size=len(data), sha256=digest)
        log.debug("s3.put", sha256=digest, size=len(data), stored=len(payload))
        return BlobRef(sha256=digest, size_bytes=len(data), compressed_bytes=len(payload))

    def put_stream(
        self,
        source: IO[bytes],
        *,
        compress: bool = True,
        size: int | None = None,
    ) -> BlobRef:
        """Store a payload read in chunks, never materialising it in memory.

        Content addressing forces a two-phase write: the key is unknown until the
        last byte is hashed. The payload is compressed into a size-capped spool
        file while hashing, then uploaded once the address is known — the S3
        analogue of the filesystem store's staging-then-rename.
        """
        hasher = hashlib.sha256()
        raw_bytes = 0
        declared_size = _ZSTD_SIZE_UNKNOWN if size is None else size

        try:
            with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as spool:
                if compress:
                    compressor = zstandard.ZstdCompressor(level=self._zstd_level)
                    try:
                        with compressor.stream_writer(
                            spool, size=declared_size, closefd=False
                        ) as encoder:
                            for chunk in _iter_chunks(source, self._chunk_size):
                                hasher.update(chunk)
                                raw_bytes += len(chunk)
                                encoder.write(chunk)
                    except zstandard.ZstdError as exc:
                        if size is not None:
                            raise MalformedRequest(
                                "declared size does not match the bytes read",
                                substrate=_SUBSTRATE,
                                declared=size,
                                actual=raw_bytes,
                            ) from exc
                        raise BlobFailure(
                            "the payload could not be compressed",
                            substrate=_SUBSTRATE,
                            driver_error="ZstdError",
                        ) from exc
                else:
                    for chunk in _iter_chunks(source, self._chunk_size):
                        hasher.update(chunk)
                        raw_bytes += len(chunk)
                        spool.write(chunk)

                if size is not None and size != raw_bytes:
                    raise MalformedRequest(
                        "declared size does not match the bytes read",
                        substrate=_SUBSTRATE,
                        declared=size,
                        actual=raw_bytes,
                    )

                digest = hasher.hexdigest()
                compressed_bytes = spool.tell()

                existing = self._locate(digest)
                if existing is not None:
                    _key, head = existing
                    return BlobRef(
                        sha256=digest,
                        size_bytes=raw_bytes,
                        compressed_bytes=_content_length(
                            head, key=_key, sha256=digest, default=compressed_bytes
                        ),
                    )

                spool.seek(0)
                key = self._key(digest, compressed=compress)
                self._upload_fileobj(key, spool, raw_size=raw_bytes, sha256=digest)
        except OSError as exc:
            # The spool write, the spill to the temporary file, the flush on
            # close, or the read from the caller's stream. The spool is a
            # *host* resource rather than the substrate, but it is still an
            # exception that would leave the store, and a temporary directory
            # that cannot hold the payload is a failure of this machine.
            raise_blob_error(exc, substrate=_SUBSTRATE, op="write")

        log.debug("s3.put_stream", sha256=digest, size=raw_bytes, stored=compressed_bytes)
        return BlobRef(sha256=digest, size_bytes=raw_bytes, compressed_bytes=compressed_bytes)

    def _put_object(self, key: str, payload: bytes, *, raw_size: int, sha256: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                Metadata={_META_RAW_SIZE: str(raw_size), "sha256": sha256},
            )
        except Exception as exc:
            raise_blob_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=sha256,
                op="put",
                translate=self._translate_failure,
            )

    def _upload_fileobj(self, key: str, fileobj: IO[bytes], *, raw_size: int, sha256: str) -> None:
        try:
            self._client.upload_fileobj(
                fileobj,
                self._bucket,
                key,
                ExtraArgs={"Metadata": {_META_RAW_SIZE: str(raw_size), "sha256": sha256}},
            )
        except Exception as exc:
            # ``upload_fileobj`` — this call — propagates the driver's own
            # error with its response intact; boto3's *filename* variant
            # (``upload_file``) is the one that wraps it in an
            # ``S3UploadFailedError``, and it puts the driver's error in the
            # wrapper's ``__context__`` rather than in its ``__cause__``. The
            # translation follows that context, so a failed upload reports
            # the same concept as the same failure on the ``put_object``
            # path — which is the whole of the put/put_stream divergence.
            raise_blob_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=sha256,
                op="upload",
                translate=self._translate_failure,
            )

    # -- reads -------------------------------------------------------------

    def get(self, digest: str) -> bytes:
        """Return the raw payload, verifying its hash first."""
        key = self._require(digest, op="get")
        raw = self._read_all(key, digest)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != digest:
            raise Corrupt(
                "blob failed hash verification; the archive is corrupt",
                substrate=_SUBSTRATE,
                expected=digest,
                actual=actual,
                key=key,
            )
        return raw

    def get_stream(self, digest: str) -> Iterator[bytes]:
        """Yield the payload in chunks, verifying the hash as it goes.

        The mismatch can only be raised after the final chunk — a hash over
        partial data means nothing — so a consumer writing chunks straight to
        disk must treat its output as unverified until the iterator is
        exhausted, exactly as with the filesystem store.
        """
        key = self._require(digest, op="get_stream")
        hasher = hashlib.sha256()
        body = self._body(key, digest)
        try:
            source: IO[bytes]
            if key.endswith(_COMPRESSED_SUFFIX):
                source = zstandard.ZstdDecompressor().stream_reader(body)
            else:
                source = body
            chunks = _translated_chunks(
                _iter_chunks(source, self._chunk_size),
                translate=lambda exc: self._translate_failure(
                    exc,
                    bucket=self._bucket,
                    key=key,
                    sha256=digest,
                    op="read",
                ),
            )
            for chunk in chunks:
                hasher.update(chunk)
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        actual = hasher.hexdigest()
        if actual != digest:
            raise Corrupt(
                "blob failed hash verification; the archive is corrupt",
                substrate=_SUBSTRATE,
                expected=digest,
                actual=actual,
                key=key,
            )

    def _body(self, key: str, digest: str) -> IO[bytes]:
        """The object's body, as a reader.

        The digest is passed in because the contract's not-found answer names
        the **digest**, not the key: the key is a fan-out path
        (``blobs/ab/cd/<digest>.zst``) and the digest is the address the
        caller asked about.
        """
        try:
            response: dict[str, Any] = self._client.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise_blob_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=digest,
                op="get",
                translate=self._translate_failure,
            )
        # The response body is a streaming reader, not a ``bytes``; it is
        # handed back as the ``IO[bytes]`` the contract promises and consumed
        # through :func:`_iter_chunks` and zstd's own reader, both of which
        # only ever call ``read``.
        body: IO[bytes] = response["Body"]
        return body

    def _read_all(self, key: str, digest: str) -> bytes:
        body = self._body(key, digest)
        try:
            payload = body.read()
        except Exception as exc:
            # A body that dies half-way through is a connection that failed
            # mid-flight, not a corrupt archive: the two have different
            # remedies, and the driver used to answer with its own exception.
            raise_blob_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=digest,
                op="read",
                translate=self._translate_failure,
            )
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        if not key.endswith(_COMPRESSED_SUFFIX):
            return payload
        try:
            # zstd's ``stream_reader`` accepts the frame bytes directly, so the
            # whole-payload path needs no reader wrapper of its own.
            return zstandard.ZstdDecompressor().stream_reader(payload).read()
        except zstandard.ZstdError as exc:
            raise Corrupt(
                "blob is not a readable zstd frame",
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=digest,
            ) from exc

    # -- metadata ----------------------------------------------------------

    def exists(self, digest: str) -> bool:
        if self._locate(digest) is not None:
            return True
        # The blob is not there. That is an answer — unless the *bucket* is
        # not there, in which case the store cannot answer at all.
        self._require_namespace(digest, op="exists")
        return False

    def stat(self, digest: str) -> BlobRef | None:
        """Describe a blob without downloading it, or ``None`` if absent.

        The raw size comes from the object metadata written at upload time. If a
        legacy object lacks it, it falls back to streaming the blob through the
        decompressor to count — correct, but O(size), which is why the metadata
        exists.
        """
        located = self._locate(digest)
        if located is None:
            self._require_namespace(digest, op="stat")
            return None
        key, head = located
        compressed_bytes = _content_length(head, key=key, sha256=digest)
        if not key.endswith(_COMPRESSED_SUFFIX):
            return BlobRef(
                sha256=digest, size_bytes=compressed_bytes, compressed_bytes=compressed_bytes
            )
        raw_size = self._raw_size_from_head(head)
        if raw_size is None:
            raw_size = sum(len(chunk) for chunk in self.get_stream(digest))
        return BlobRef(
            sha256=digest, size_bytes=raw_size, compressed_bytes=compressed_bytes
        )

    @staticmethod
    def _raw_size_from_head(head: dict[str, Any]) -> int | None:
        meta = head.get("Metadata") or {}
        value = meta.get(_META_RAW_SIZE)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None

    # -- deletion ----------------------------------------------------------

    def delete(self, digest: str) -> bool:
        """Remove a blob. Returns whether anything was there."""
        located = self._locate(digest)
        if located is None:
            self._require_namespace(digest, op="delete")
            return False
        key, _head = located
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise_blob_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=digest,
                op="delete",
                translate=self._translate_failure,
            )
        log.info("s3.deleted", sha256=digest)
        return True

    def _require(self, digest: str, *, op: str) -> str:
        """The key of the blob, or the error that says why there is none.

        The store has concluded that the object is not there, and before it
        says so it checks that the *bucket* is there — because a store that
        cannot answer must not answer "no".
        """
        located = self._locate(digest)
        if located is None:
            self._require_namespace(digest, op=op)
            raise NotFound("blob not found", substrate=_SUBSTRATE, sha256=digest)
        return located[0]

    def close(self) -> None:
        """Close a client we created; an injected one belongs to the caller."""
        if self._owns_client:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()

    def __repr__(self) -> str:
        return f"S3BlobStore(bucket={self._bucket!r}, prefix={self._prefix!r})"


def _blob_store_from_settings(settings: Any) -> S3BlobStore:
    """Construct an :class:`S3BlobStore` from :class:`StorageSettings`.

    Credentials are read from the environment variables *named* by the settings
    (``s3_access_key_env`` / ``s3_secret_key_env``), never from the config
    object itself — the same indirection the model layer uses for API keys, so a
    DSN or a ledger dump never carries a secret.

    The bucket is created if it does not exist. A cold lake whose bucket is
    missing would otherwise fail on the *first write* with an opaque
    ``NoSuchBucket``, long after the deployment was declared healthy — so the
    store makes the bucket part of its own construction.
    """
    access_key = os.environ.get(settings.s3_access_key_env)
    secret_key = os.environ.get(settings.s3_secret_key_env)
    if not settings.s3_endpoint:
        raise NotSupported(
            "backend_coldlake='s3' requires storage.s3_endpoint to be set "
            "(e.g. PAA_STORAGE__S3_ENDPOINT=http://seaweedfs:8333)",
            substrate=_SUBSTRATE,
            reason="endpoint_missing",
        )
    return S3BlobStore(
        settings.s3_bucket,
        endpoint_url=settings.s3_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=settings.s3_secure,
        zstd_level=settings.zstd_level,
        ensure_bucket=True,
    )
