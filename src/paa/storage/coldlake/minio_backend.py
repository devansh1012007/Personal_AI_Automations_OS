"""S3/MinIO-backed cold lake — the RFC's original object substrate.

SPEC DEVIATION REVERSAL (docs/adr/0019): ADR-0004 replaced MinIO with a local
content-addressed directory because the target laptop had no Docker and no RAM
to spare for an S3 server. The Docker deployment restores MinIO. This module is
the server-backed sibling of :class:`~paa.storage.coldlake.cas.ContentAddressedStore`
and presents the *same* interface — ``put``/``get``/``exists``/``delete``/
``stat``/``put_stream``/``get_stream`` returning
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
  raise :class:`~paa.core.errors.StorageError` on mismatch rather than return
  plausible-looking data. Bit-rot in an archive nobody reads until they urgently
  need it is this layer's worst failure mode; S3 durability does not remove the
  obligation to check.
* **Bounded memory.** :meth:`put_stream` spools through a size-capped temp file
  and uploads a file object; :meth:`get_stream` yields fixed chunks straight
  from the response body. A 500 MB artifact never becomes a 500 MB ``bytes``.

``boto3`` is imported lazily so this module stays importable — and the package
installable — without the ``minio`` extra. Only constructing a store that must
create its own client reaches for it; an injected client (the test path) does
not.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from typing import IO, Any, Final

import structlog
import zstandard

from paa.core.errors import StorageError
from paa.storage.coldlake.cas import (
    DEFAULT_CHUNK_SIZE,
    BlobRef,
    _iter_chunks,
    _validate_digest,
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
        raise StorageError(
            "the minio cold-lake backend requires the 'minio' extra "
            "(pip install 'paa[minio]')",
            substrate=_SUBSTRATE,
        ) from exc
    return boto3


def _is_not_found(exc: Exception) -> bool:
    """Whether a boto3/botocore error means "no such key/bucket".

    Read structurally off the error response rather than by class, so a stub
    client that raises a lookalike (the test path) is recognised too.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}:
            return True
    return type(exc).__name__ in {"NoSuchKey", "NoSuchBucket", "NotFound", "404"}


class S3BlobStore:
    """Immutable, compressed, deduplicating blob storage over S3/MinIO.

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
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                use_ssl=secure,
                region_name=region,
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
        try:
            return self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StorageError(
                f"could not stat object: {exc}", substrate=_SUBSTRATE, key=key
            ) from exc

    def _ensure_bucket(self) -> None:  # pragma: no cover - needs a live server
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            self._client.create_bucket(Bucket=self._bucket)
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
            zstandard.ZstdCompressor(level=self._zstd_level).compress(data) if compress else data
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
                        raise StorageError(
                            "declared size does not match the bytes read",
                            substrate=_SUBSTRATE,
                            declared=size,
                            actual=raw_bytes,
                        ) from exc
                    raise StorageError(
                        f"compression failed: {exc}", substrate=_SUBSTRATE
                    ) from exc
            else:
                for chunk in _iter_chunks(source, self._chunk_size):
                    hasher.update(chunk)
                    raw_bytes += len(chunk)
                    spool.write(chunk)

            if size is not None and size != raw_bytes:
                raise StorageError(
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
            if _is_not_found(exc):
                raise
            raise StorageError(
                f"could not write object: {exc}", substrate=_SUBSTRATE, key=key
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
            raise StorageError(
                f"could not upload object: {exc}", substrate=_SUBSTRATE, key=key
            ) from exc

    # -- reads -------------------------------------------------------------

    def get(self, digest: str) -> bytes:
        """Return the raw payload, verifying its hash first."""
        key = self._require(digest)
        raw = self._read_all(key)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != digest:
            raise StorageError(
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
        body = self._body(key)
        try:
            source: IO[bytes]
            if key.endswith(_COMPRESSED_SUFFIX):
                source = zstandard.ZstdDecompressor().stream_reader(body)
            else:
                source = body
            for chunk in _iter_chunks(source, self._chunk_size):
                hasher.update(chunk)
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        actual = hasher.hexdigest()
        if actual != digest:
            raise StorageError(
                "blob failed hash verification; the archive is corrupt",
                substrate=_SUBSTRATE,
                expected=digest,
                actual=actual,
                key=key,
            )

    def _body(self, key: str) -> IO[bytes]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc):
                raise StorageError(
                    "blob not found", substrate=_SUBSTRATE, sha256=key
                ) from exc
            raise StorageError(
                f"could not read object: {exc}", substrate=_SUBSTRATE, key=key
            ) from exc
        return response["Body"]

    def _read_all(self, key: str) -> bytes:
        body = self._body(key)
        try:
            payload = body.read()
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        if not key.endswith(_COMPRESSED_SUFFIX):
            return payload
        try:
            return zstandard.ZstdDecompressor().stream_reader(_BytesReader(payload)).read()
        except zstandard.ZstdError as exc:
            raise StorageError(
                f"blob is not a readable zstd frame: {exc}",
                substrate=_SUBSTRATE,
                key=key,
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
            raise StorageError(
                f"could not delete object: {exc}", substrate=_SUBSTRATE, key=key
            ) from exc
        log.info("s3.deleted", sha256=digest)
        return True

    def _require(self, digest: str) -> str:
        located = self._locate(digest)
        if located is None:
            raise StorageError("blob not found", substrate=_SUBSTRATE, sha256=digest)
        return located[0]

    def close(self) -> None:
        """Close a client we created; an injected one belongs to the caller."""
        if self._owns_client:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()

    def __repr__(self) -> str:
        return f"S3BlobStore(bucket={self._bucket!r}, prefix={self._prefix!r})"


class _BytesReader:
    """Minimal read-only file wrapper over a ``bytes`` object.

    zstd's ``stream_reader`` needs a file-like ``read``; the whole-payload path
    already holds the bytes, so this avoids importing ``io`` just to wrap them
    and keeps the one-shot decode identical to the filesystem store's.
    """

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def _blob_store_from_settings(settings: Any) -> S3BlobStore:
    """Construct an :class:`S3BlobStore` from :class:`StorageSettings`.

    Credentials are read from the environment variables *named* by the settings
    (``minio_access_key_env`` / ``minio_secret_key_env``), never from the config
    object itself — the same indirection the model layer uses for API keys, so a
    DSN or a ledger dump never carries a secret.
    """
    access_key = os.environ.get(settings.minio_access_key_env)
    secret_key = os.environ.get(settings.minio_secret_key_env)
    return S3BlobStore(
        settings.minio_bucket,
        endpoint_url=settings.minio_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=settings.minio_secure,
        zstd_level=settings.zstd_level,
        ensure_bucket=True,
    )
