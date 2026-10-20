"""The cold-lake blob-store contract.

Two implementations satisfy it: the local content-addressed store
(:class:`paa.storage.coldlake.cas.ContentAddressedStore`, ADR-0004) and the
S3 client (:class:`paa.storage.coldlake.minio_backend.S3BlobStore`, ADR-0019,
ADR-0026). The repositories depend on this contract rather than on either
class, which is what lets a deployment choose the substrate by configuration
instead of by code.

The contract is deliberately narrow — the operations the cold lake actually
performs. It is *not* the union of both classes' methods: ``iter_blobs`` and
``total_bytes`` are filesystem bookkeeping with no S3 counterpart that means
the same thing, and a protocol that included them would force the S3
implementation to fake them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import IO, Protocol, runtime_checkable

from paa.storage.coldlake.cas import BlobRef

__all__ = ["BlobStore"]


@runtime_checkable
class BlobStore(Protocol):
    """Immutable, content-addressed blob storage.

    Every method is synchronous in both implementations: a store that is
    local is a bounded file read or write, and a store that is S3 is a
    bounded HTTP request. Wrapping them in coroutines would buy nothing but
    the illusion that they yield. A caller on the event loop that expects
    large payloads should use :func:`asyncio.to_thread`.
    """

    def put(self, data: bytes, *, compress: bool = True) -> BlobRef:
        """Store ``data`` and return a reference to it.

        The digest is over the *raw* bytes, never the compressed frame, so
        the reference is the same whichever way the blob was written.
        """
        ...

    def put_stream(
        self,
        stream: IO[bytes],
        *,
        compress: bool = True,
        chunk_size: int | None = None,
    ) -> BlobRef:
        """Store the contents of ``stream`` and return a reference to it."""
        ...

    def get(self, digest: str) -> bytes:
        """The stored bytes for ``digest``, verified against it."""
        ...

    def get_stream(self, digest: str) -> Iterator[bytes]:
        """The stored bytes for ``digest``, in chunks, verified against it."""
        ...

    def exists(self, digest: str) -> bool:
        """Whether a blob with this digest is stored."""
        ...

    def stat(self, digest: str) -> BlobRef | None:
        """The reference for ``digest``, or ``None`` if it is not stored."""
        ...

    def delete(self, digest: str) -> bool:
        """Remove the blob. Returns whether there was one to remove."""
        ...

    def __repr__(self) -> str: ...
