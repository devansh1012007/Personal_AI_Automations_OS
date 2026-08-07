"""Content-addressed blob store — the cold lake's object substrate.

SPEC DEVIATION (docs/adr/0004): RFC §1.2 specifies MinIO for the immutable
archive. MinIO is an S3 server: a container, a process, a port, credentials, and
several hundred MB of resident memory, to serve a single-user runtime on a
machine with ~3.5 GB free and no Docker. This module provides the same logical
contract — immutable, addressable, compressed object storage — as a directory.

Content addressing
------------------
A blob's name *is* the SHA-256 of its raw bytes, so:

* **Deduplication is free and automatic.** Two signals carrying the same
  attachment, or a file archived on every run of a task, converge on one blob
  with no reference counting and no comparison pass. :meth:`put` notices the
  path already exists and does no work at all.
* **Corruption is detectable.** The name is a checksum, so every read can verify
  what it got. This is not paranoia: the cold lake is *the* immutable history —
  the ledger's hash chain proves what the runtime believed, and these blobs are
  what it believed it about. Silent bit-rot in an archive nobody reads until
  they urgently need it is the worst failure mode this layer has, so a mismatch
  raises :class:`StorageError` rather than returning plausible-looking bytes.

Fan-out
-------
Blobs live at ``blobs/<hash[:2]>/<hash[2:4]>/<hash>.zst``. Two levels of 256
gives 65,536 leaf directories, which keeps any single directory small enough for
Windows' NTFS enumeration to stay fast as the archive grows.

Memory
------
:meth:`put_stream` and :meth:`get_stream` move data in fixed-size chunks and
never hold a whole payload. That is the RFC §11.2 mitigation made real: this
runtime targets a machine where a single 500 MB archived artifact read into a
``bytes`` would matter.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Final

import structlog
import zstandard
from pydantic import BaseModel, ConfigDict, Field, computed_field

from paa.core.errors import StorageError

__all__ = ["CAS_URI_SCHEME", "BlobRef", "ContentAddressedStore"]

log = structlog.get_logger(__name__)

_SUBSTRATE: Final = "cas"

CAS_URI_SCHEME: Final = "cas"

#: zstandard's sentinel for "content size not known in advance"
#: (``ZSTD_CONTENTSIZE_UNKNOWN``). The C extension requires an int here, so
#: ``None`` cannot be forwarded.
_ZSTD_SIZE_UNKNOWN: Final = -1

#: 1 MiB. Large enough that syscall overhead is negligible, small enough that a
#: dozen concurrent streams cost megabytes rather than gigabytes.
DEFAULT_CHUNK_SIZE: Final = 1024 * 1024

_HASH_LENGTH: Final = 64
_COMPRESSED_SUFFIX: Final = ".zst"
_RAW_SUFFIX: Final = ".bin"


class BlobRef(BaseModel):
    """Handle to a stored blob.

    ``size_bytes`` is the raw payload; ``compressed_bytes`` is what the disk
    actually gave up. Both are recorded because the ratio is the only way to
    tell whether zstd is earning its CPU on a given channel's payloads.
    """

    model_config = ConfigDict(frozen=True)

    sha256: str = Field(min_length=_HASH_LENGTH, max_length=_HASH_LENGTH)
    size_bytes: int = Field(ge=0)
    compressed_bytes: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uri(self) -> str:
        """Stable reference stored in ``blob_uri`` columns: ``cas://<hash>``."""
        return f"{CAS_URI_SCHEME}://{self.sha256}"

    @property
    def compression_ratio(self) -> float:
        return self.compressed_bytes / self.size_bytes if self.size_bytes else 1.0

    @staticmethod
    def parse_uri(uri: str) -> str:
        """Extract the hash from a ``cas://`` URI.

        Rejects anything else loudly. A ``blob_uri`` that is not a CAS URI means
        a row was written by something with a different idea of where blobs live,
        and quietly treating the tail as a hash would turn that into a confusing
        "blob not found" much later.
        """
        prefix = f"{CAS_URI_SCHEME}://"
        if not uri.startswith(prefix):
            raise StorageError(
                f"not a CAS uri: {uri!r}", substrate=_SUBSTRATE, uri=uri
            )
        digest = uri[len(prefix) :]
        if len(digest) != _HASH_LENGTH:
            raise StorageError(
                f"malformed CAS uri: {uri!r}", substrate=_SUBSTRATE, uri=uri
            )
        return digest


class ContentAddressedStore:
    """Immutable, compressed, deduplicating blob storage on the local filesystem.

    Synchronous by design. Every operation is a bounded file read or write, and
    wrapping them in coroutines would buy nothing but the illusion that they
    yield. Callers on the event loop that expect large payloads should use
    :func:`asyncio.to_thread`, exactly as the kuzu backend does.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        zstd_level: int = 3,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._root = Path(root)
        self._blobs = self._root / "blobs"
        self._staging = self._root / "staging"
        self._zstd_level = zstd_level
        self._chunk_size = chunk_size
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._staging.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def _leaf(self, digest: str, *, compressed: bool) -> Path:
        suffix = _COMPRESSED_SUFFIX if compressed else _RAW_SUFFIX
        return self._blobs / digest[:2] / digest[2:4] / f"{digest}{suffix}"

    def _locate(self, digest: str) -> Path | None:
        """Find a stored blob whichever way it was written."""
        _validate_digest(digest)
        for compressed in (True, False):
            candidate = self._leaf(digest, compressed=compressed)
            if candidate.exists():
                return candidate
        return None

    # -- writes ------------------------------------------------------------

    def put(self, data: bytes, *, compress: bool = True) -> BlobRef:
        """Store ``data`` and return its reference.

        The hash is taken over the *raw* bytes, never the compressed frame, so
        the address of a payload does not depend on the zstd level in effect
        when it happened to be written. Re-compressing the archive at a
        different level would otherwise rename every blob in it.
        """
        digest = hashlib.sha256(data).hexdigest()
        target = self._leaf(digest, compressed=compress)

        existing = self._locate(digest)
        if existing is not None:
            # Dedup: identical content is already here, by definition of the
            # address. Rewriting it would be pure I/O for an identical result.
            return BlobRef(
                sha256=digest,
                size_bytes=len(data),
                compressed_bytes=existing.stat().st_size,
            )

        payload = (
            zstandard.ZstdCompressor(level=self._zstd_level).compress(data) if compress else data
        )
        self._commit(target, payload)
        log.debug("cas.put", sha256=digest, size=len(data), stored=len(payload))
        return BlobRef(sha256=digest, size_bytes=len(data), compressed_bytes=len(payload))

    def put_stream(
        self,
        source: IO[bytes],
        *,
        compress: bool = True,
        size: int | None = None,
    ) -> BlobRef:
        """Store a payload read in chunks, never materialising it in memory.

        Content addressing forces a two-phase write: the destination path is not
        known until the last byte has been hashed. So the payload is compressed
        into ``staging/`` under a random name and atomically renamed into place
        once its address is known. A crash mid-write therefore leaves a stray
        staging file — recoverable, and never a corrupt blob at a valid address.

        ``size`` is an optional hint. When supplied it is written into the zstd
        frame header, which lets :meth:`stat` report the raw size later without
        decompressing the blob to count.
        """
        scratch = self._staging / f"{uuid.uuid4().hex}.part"
        hasher = hashlib.sha256()
        raw_bytes = 0

        # zstandard signals "content size unknown" with -1, not None: passing
        # None raises TypeError from the C extension.
        declared_size = _ZSTD_SIZE_UNKNOWN if size is None else size

        try:
            with scratch.open("wb") as sink:
                if compress:
                    compressor = zstandard.ZstdCompressor(level=self._zstd_level)
                    # closefd=False: the writer must flush its frame epilogue
                    # without closing the file we still need to fsync-adjacent.
                    try:
                        with compressor.stream_writer(
                            sink, size=declared_size, closefd=False
                        ) as encoder:
                            for chunk in _iter_chunks(source, self._chunk_size):
                                hasher.update(chunk)
                                raw_bytes += len(chunk)
                                encoder.write(chunk)
                    except zstandard.ZstdError as exc:
                        # A declared size that disagrees with the stream is
                        # caught by zstd *during* the write, before the
                        # explicit check below can run. Translate it here so
                        # callers get the same structured error either way,
                        # rather than an opaque "Src size is incorrect".
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
                        sink.write(chunk)

            # Still needed: an under-run (fewer bytes than declared) leaves zstd
            # content to flush and may not trip the frame check, and the
            # uncompressed path has no frame check at all.
            if size is not None and size != raw_bytes:
                raise StorageError(
                    "declared size does not match the bytes read",
                    substrate=_SUBSTRATE,
                    declared=size,
                    actual=raw_bytes,
                )

            digest = hasher.hexdigest()
            compressed_bytes = scratch.stat().st_size

            existing = self._locate(digest)
            if existing is not None:
                return BlobRef(
                    sha256=digest,
                    size_bytes=raw_bytes,
                    compressed_bytes=existing.stat().st_size,
                )

            target = self._leaf(digest, compressed=compress)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(scratch, target)
            log.debug("cas.put_stream", sha256=digest, size=raw_bytes, stored=compressed_bytes)
            return BlobRef(
                sha256=digest, size_bytes=raw_bytes, compressed_bytes=compressed_bytes
            )
        finally:
            scratch.unlink(missing_ok=True)

    def _commit(self, target: Path, payload: bytes) -> None:
        """Write via a staging file and rename, so a blob is never half-there.

        ``os.replace`` is atomic on both POSIX and Windows for same-volume
        renames, and staging lives under the same root precisely so it is the
        same volume.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = self._staging / f"{uuid.uuid4().hex}.part"
        try:
            scratch.write_bytes(payload)
            os.replace(scratch, target)
        except OSError as exc:
            raise StorageError(
                f"could not write blob: {exc}", substrate=_SUBSTRATE, path=str(target)
            ) from exc
        finally:
            scratch.unlink(missing_ok=True)

    # -- reads -------------------------------------------------------------

    def get(self, digest: str) -> bytes:
        """Return the raw payload, verifying its hash first.

        Verification happens before returning, so a caller of :meth:`get` can
        never act on corrupted bytes.
        """
        path = self._require(digest)
        raw = self._read_all(path)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != digest:
            raise StorageError(
                "blob failed hash verification; the archive is corrupt",
                substrate=_SUBSTRATE,
                expected=digest,
                actual=actual,
                path=str(path),
            )
        return raw

    def get_stream(self, digest: str) -> Iterator[bytes]:
        """Yield the payload in chunks, verifying the hash as it goes.

        The mismatch can only be raised *after* the final chunk — a hash over
        partial data means nothing — so a consumer that writes chunks straight
        to disk must treat its output as unverified until the iterator is
        exhausted. That is the unavoidable price of not buffering the whole
        payload; :meth:`get` is the verified-before-use alternative for anything
        that fits in memory comfortably.
        """
        path = self._require(digest)
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            source: IO[bytes]
            if path.suffix == _COMPRESSED_SUFFIX:
                source = zstandard.ZstdDecompressor().stream_reader(handle)
            else:
                source = handle
            for chunk in _iter_chunks(source, self._chunk_size):
                hasher.update(chunk)
                yield chunk
        actual = hasher.hexdigest()
        if actual != digest:
            raise StorageError(
                "blob failed hash verification; the archive is corrupt",
                substrate=_SUBSTRATE,
                expected=digest,
                actual=actual,
                path=str(path),
            )

    def _read_all(self, path: Path) -> bytes:
        try:
            with path.open("rb") as handle:
                if path.suffix != _COMPRESSED_SUFFIX:
                    return handle.read()
                # stream_reader rather than one-shot decompress(): a frame
                # written by put_stream without a size hint carries no declared
                # content size, and the one-shot API refuses those outright.
                return zstandard.ZstdDecompressor().stream_reader(handle).read()
        except OSError as exc:
            raise StorageError(
                f"could not read blob: {exc}", substrate=_SUBSTRATE, path=str(path)
            ) from exc
        except zstandard.ZstdError as exc:
            # A truncated or scrambled frame is corruption too; report it the
            # same way rather than leaking a codec-specific exception.
            raise StorageError(
                f"blob is not a readable zstd frame: {exc}",
                substrate=_SUBSTRATE,
                path=str(path),
            ) from exc

    # -- metadata ----------------------------------------------------------

    def exists(self, digest: str) -> bool:
        return self._locate(digest) is not None

    def stat(self, digest: str) -> BlobRef | None:
        """Describe a blob without reading it, or ``None`` if absent.

        The raw size comes from the zstd frame header when it is declared
        (everything :meth:`put` writes, and any :meth:`put_stream` given a size
        hint). Otherwise it has to be counted by streaming the blob through the
        decompressor — correct, but O(size), which is why the hint exists.
        """
        path = self._locate(digest)
        if path is None:
            return None
        compressed_bytes = path.stat().st_size
        if path.suffix != _COMPRESSED_SUFFIX:
            return BlobRef(
                sha256=digest, size_bytes=compressed_bytes, compressed_bytes=compressed_bytes
            )
        return BlobRef(
            sha256=digest,
            size_bytes=self._raw_size(path),
            compressed_bytes=compressed_bytes,
        )

    def _raw_size(self, path: Path) -> int:
        with path.open("rb") as handle:
            header = handle.read(18)  # a zstd frame header is at most 18 bytes
            declared = zstandard.frame_content_size(header)
            if declared >= 0:
                return declared
            handle.seek(0)
            reader = zstandard.ZstdDecompressor().stream_reader(handle)
            return sum(len(chunk) for chunk in _iter_chunks(reader, self._chunk_size))

    def iter_blobs(self) -> Iterator[BlobRef]:
        """Walk the archive. Sorted, so two runs enumerate in the same order."""
        for path in sorted(self._blobs.rglob(f"*{_COMPRESSED_SUFFIX}")) + sorted(
            self._blobs.rglob(f"*{_RAW_SUFFIX}")
        ):
            digest = path.stem
            if len(digest) != _HASH_LENGTH:
                log.warning("cas.foreign_file", path=str(path))
                continue
            ref = self.stat(digest)
            if ref is not None:
                yield ref

    def total_bytes(self) -> int:
        """Bytes the archive occupies on disk, i.e. *compressed* size.

        Deliberately the on-disk figure rather than the sum of raw sizes: this
        answers "how much disk is the cold lake using", which is the question
        with an operational consequence. It is also exact and cheap, where the
        raw total would mean decompressing blobs whose frames declare no size.
        """
        return sum(
            path.stat().st_size for path in self._blobs.rglob("*") if path.is_file()
        )

    # -- deletion ----------------------------------------------------------

    def delete(self, digest: str) -> bool:
        """Remove a blob. Returns whether anything was there.

        Deleting from an immutable archive is a deliberate act — retention
        policy, or a GC sweep after nothing references the blob. Nothing here
        counts references; the caller must know.
        """
        path = self._locate(digest)
        if path is None:
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise StorageError(
                f"could not delete blob: {exc}", substrate=_SUBSTRATE, path=str(path)
            ) from exc
        log.info("cas.deleted", sha256=digest)
        return True

    def clear_staging(self) -> int:
        """Sweep partial writes abandoned by a crash. Returns how many were removed."""
        removed = 0
        for leftover in self._staging.glob("*.part"):
            leftover.unlink(missing_ok=True)
            removed += 1
        return removed

    def _require(self, digest: str) -> Path:
        path = self._locate(digest)
        if path is None:
            raise StorageError("blob not found", substrate=_SUBSTRATE, sha256=digest)
        return path

    def __repr__(self) -> str:
        return f"ContentAddressedStore(root={str(self._root)!r}, level={self._zstd_level})"


# ---------------------------------------------------------------------------


def _validate_digest(digest: str) -> None:
    """Reject anything that is not a SHA-256 hex digest.

    This is a path-traversal guard as much as a typo check: a digest becomes
    directory components, so a value containing separators or ``..`` must never
    reach :meth:`_leaf`.
    """
    if len(digest) != _HASH_LENGTH or not all(c in "0123456789abcdef" for c in digest):
        raise StorageError(
            f"not a sha256 hex digest: {digest!r}", substrate=_SUBSTRATE, value=digest
        )


def _iter_chunks(source: IO[bytes], chunk_size: int) -> Iterator[bytes]:
    """Read a stream in fixed-size pieces until it is exhausted."""
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            return
        yield chunk


def copy_file_into(store: ContentAddressedStore, path: Path, *, compress: bool = True) -> BlobRef:
    """Archive a host file without loading it.

    Lives here rather than in :mod:`paa.storage.coldlake.artifacts` so that
    anything needing to intern a file — an artifact, a large signal payload, a
    workspace snapshot — shares one implementation of "stream a file in".
    """
    try:
        with path.open("rb") as handle:
            return store.put_stream(handle, compress=compress, size=path.stat().st_size)
    except OSError as exc:
        raise StorageError(
            f"could not read host file: {exc}", substrate=_SUBSTRATE, path=str(path)
        ) from exc


def export_to_file(store: ContentAddressedStore, digest: str, destination: Path) -> Path:
    """Materialise a blob onto the filesystem in chunks.

    Written to a sibling temp file and renamed, so a verification failure
    part-way through never leaves a plausible-looking truncated file at the
    destination path.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_suffix(destination.suffix + f".{uuid.uuid4().hex[:8]}.part")
    try:
        with scratch.open("wb") as sink:
            for chunk in store.get_stream(digest):
                sink.write(chunk)
        os.replace(scratch, destination)
        return destination
    finally:
        scratch.unlink(missing_ok=True)


def purge(store: ContentAddressedStore) -> None:
    """Delete the entire archive. Tests and explicit operator intent only."""
    shutil.rmtree(store.root, ignore_errors=True)
