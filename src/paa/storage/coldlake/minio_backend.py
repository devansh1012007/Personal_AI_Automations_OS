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
  boto3's transfer manager wraps the second one's error in an
  ``S3UploadFailedError``. The translation unwraps that wrapper, so a missing
  bucket is one answer from both, rather than a raw ``ClientError`` from one
  and a generic error from the other.
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
    BlobError,
    BlobFailure,
    Corrupt,
    MalformedRequest,
    NotFound,
    NotSupported,
    Unreachable,
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

#: S3 error codes that mean the addressed **object** is not there.
#:
#: ``"404"`` is in the list because a HEAD on a missing key answers 404 with
#: an empty body, and botocore then reports the *status* as the code.
_S3_OBJECT_MISSING_CODES: Final[frozenset[str]] = frozenset(
    {
        "404",
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
#: boto3's transfer manager wraps the ``ClientError`` of a failed upload in an
#: ``S3UploadFailedError``, and that wrapper is what the caller sees. Without
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


def _is_missing_object(exc: BaseException) -> bool:
    """Whether the driver's error says the addressed **object** is not there.

    The bucket-level codes are excluded on purpose: a missing bucket is not a
    missing object, and the store cannot answer the question at all.
    """
    code = _s3_error_code(exc)
    if code is not None:
        return code in _S3_OBJECT_MISSING_CODES
    return _s3_status(exc) == 404


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
    if isinstance(exc, BlobError):
        return exc
    if isinstance(exc, PaaError):
        # A domain error raised by the code above the substrate. It is not the
        # driver's to relabel, and it is not unwrapped either: its own
        # ``__context__`` may be anything.
        return exc

    current = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        cause = _driver_cause(current)
        if cause is None:
            break
        current = cause

    code = _s3_error_code(current)
    status = _s3_status(current)
    names = _s3_class_names(current)

    common: dict[str, Any] = {**details, "driver_error": type(current).__name__}
    if code is not None:
        common["s3_code"] = code
    if status is not None:
        common["http_status"] = status

    if code is not None and code in _S3_BUCKET_MISSING_CODES:
        return NotFound("bucket not found", substrate=substrate, **common)
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
                raise _translate_s3_error(
                    exc, substrate=_SUBSTRATE, bucket=bucket, op="client"
                ) from exc

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
        So the bucket-level absence raises, and it raises the *same* error
        from every method, because every method reaches the store through
        this one.
        """
        try:
            # The client is untyped on purpose: ``boto3`` is an optional extra
            # and must not be imported at module scope, so the shape of what
            # the client returns is stated here rather than checked.
            head: dict[str, Any] = self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_missing_object(exc):
                return None
            raise _translate_s3_error(
                exc, substrate=_SUBSTRATE, bucket=self._bucket, key=key, op="head"
            ) from exc
        return head

    def _ensure_bucket(self) -> None:  # pragma: no cover - needs a live server
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            translated = _translate_s3_error(
                exc, substrate=_SUBSTRATE, bucket=self._bucket, op="head_bucket"
            )
            if not isinstance(translated, NotFound):
                raise translated from exc
            # The bucket is not there: create it. A cold lake whose bucket is
            # missing fails on its first write with an opaque NoSuchBucket,
            # long after the deployment was declared healthy.
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except Exception as create_exc:
                raise _translate_s3_error(
                    create_exc, substrate=_SUBSTRATE, bucket=self._bucket, op="create_bucket"
                ) from create_exc
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
                compressed_bytes=int(head.get("ContentLength", 0)),
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
                        compressed_bytes=int(head.get("ContentLength", compressed_bytes)),
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
            raise translate_blob_error(
                exc, substrate=_SUBSTRATE, op="write"
            ) from exc

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
            raise _translate_s3_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=sha256,
                op="put",
            ) from exc

    def _upload_fileobj(self, key: str, fileobj: IO[bytes], *, raw_size: int, sha256: str) -> None:
        try:
            self._client.upload_fileobj(
                fileobj,
                self._bucket,
                key,
                ExtraArgs={"Metadata": {_META_RAW_SIZE: str(raw_size), "sha256": sha256}},
            )
        except Exception as exc:
            # boto3's transfer manager wraps the driver's own error in an
            # ``S3UploadFailedError``, and the wrapper is what reaches here.
            # The translation unwraps it, so a failed upload reports the same
            # concept as the same failure on the ``put_object`` path — which
            # is the whole of the put/put_stream divergence.
            raise _translate_s3_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=sha256,
                op="upload",
            ) from exc

    # -- reads -------------------------------------------------------------

    def get(self, digest: str) -> bytes:
        """Return the raw payload, verifying its hash first."""
        key = self._require(digest)
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
        key = self._require(digest)
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
                translate=lambda exc: _translate_s3_error(
                    exc,
                    substrate=_SUBSTRATE,
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
            raise _translate_s3_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=digest,
                op="get",
            ) from exc
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
            raise _translate_s3_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=digest,
                op="read",
            ) from exc
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
        return self._locate(digest) is not None

    def stat(self, digest: str) -> BlobRef | None:
        """Describe a blob without downloading it, or ``None`` if absent.

        The raw size comes from the object metadata written at upload time. If a
        legacy object lacks it, it falls back to streaming the blob through the
        decompressor to count — correct, but O(size), which is why the metadata
        exists.
        """
        located = self._locate(digest)
        if located is None:
            return None
        key, head = located
        compressed_bytes = int(head.get("ContentLength", 0))
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
            return False
        key, _head = located
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise _translate_s3_error(
                exc,
                substrate=_SUBSTRATE,
                bucket=self._bucket,
                key=key,
                sha256=digest,
                op="delete",
            ) from exc
        log.info("s3.deleted", sha256=digest)
        return True

    def _require(self, digest: str) -> str:
        located = self._locate(digest)
        if located is None:
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
